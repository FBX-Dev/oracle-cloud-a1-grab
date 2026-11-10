# Oracle Cloud A1 免费实例 自动抢机脚本

> 在 Oracle Cloud（OCI）上自动轮询，抢 **`VM.Standard.A1.Flex`**（ARM，Always Free 免费额度）实例。
> 抢到后自动推送微信通知，附公网 IP 和可直接复制的登录命令。

Oracle 的免费 ARM 实例（Always Free，永久免费，最高 **4 OCPU / 24 GB**）是白嫖圈里最值钱的东西，
但热门区域常年"没容量"，手点控制台基本抢不到 —— 只能靠脚本 7×24 挂着轮询。

这个脚本就是干这个的。它跑了很久，把路上所有坑都趟平了，你现在可以直接用。

---

## 目录

- [核心亮点](#核心亮点)
- [调频与风控](#调频与风控)
- [效果实测](#效果实测)
- [前置条件](#前置条件)
- [快速开始（一键部署）](#快速开始一键部署)
- [手工部署详细步骤](#手工部署详细步骤)
- [使用方式](#使用方式)
- [每日 HTML 日报](#每日-html-日报)
- [配置项说明](#配置项说明)
- [工作原理](#工作原理)
- [常见问题与排错](#常见问题与排错)
- [安全提醒](#安全提醒)
- [免责声明](#免责声明)
- [English TL;DR](#english-tldr)

---

## 核心亮点

### ★ `--no-retry`：把抢机效率提升近 30 倍

这是本项目最重要的一个细节，也是绝大多数同类脚本都忽略的。

OCI CLI **默认对 5xx 错误做指数退避重试**。而"没容量"恰好被 OCI 包成了 500 错误：

```json
{ "code": "InternalError", "status": 500, "message": "Out of host capacity." }
```

于是每一次尝试，CLI 都会傻等重试：

| | 单次尝试耗时 | 一轮 3 个可用域 |
|---|---|---|
| 默认（带重试） | **104 秒** | **5 分钟+** |
| 加 `--no-retry` | **3.7 秒** | **约 12 秒** |

**效率差近 30 倍，直接决定你抢不抢得到。**

> 小贴士：`InternalError` 不一定是故障。对 A1 免费额度来说，
> `message` 是 `Out of host capacity.` 就是正常的"没抢到"，继续轮询即可。

### 其它设计

- **三个可用域全试** —— 容量是按可用域独立释放的，只盯一个等于把概率砍到 2/3。
- **参数自动发现** —— 可用域、子网、镜像全部运行时查询，不用你手工填一堆 OCID，换区域也不用改脚本。
- **小内存机器友好** —— 专门适配 500MB 级别的免费小机器（`VM.Standard.E2.1.Micro`），
  避开 `dnf` OOM 坑，并给 systemd 服务加了内存上限。
- **通知逻辑独立可测** —— `notify.sh` 可以单独跑，
  不用等真抢到才发现推送配置是错的。
- **抢到即止** —— 幂等设计 + `SUCCESS` 标记，绝不会重复创建实例（收费风险为零）。
- **自动退避** —— 一旦被限流（`TooManyRequests`）会自动多等 5 分钟再继续；
  遇到 `LimitExceeded`（额度用满）则**主动停下并通知你**，不会无意义空转。

---

## 调频与风控

**默认间隔是 10 分钟，这是刻意保守的，请不要一上手就调到很密。**

### 为什么要保守

这不是杞人忧天，有公开的实测记录：

- 有人用比这更密的节奏连跑，**两周约 1500 次尝试后开始收到 `429 TooManyRequests`**，
  结论是"5~10 分钟是间隔的现实下限"。
- 最流行的同类项目之一（hitrov 的 PHP 版）用 cron **每 5 分钟**执行，
  作者明确写"不要更频繁，否则有被 Oracle 封的风险"。
- 另一个 Python 项目在免责声明里直接写明：
  **激进的长时间自动轮询可能被 Oracle 的 abuse-detection 系统标记，导致账号被审查甚至暂停。**

所以「5~10 分钟」是社区公认的安全区间。

### 本项目的默认策略

| 设置 | 值 | 实际请求量 |
|---|---|---|
| `INTERVAL=600` + `JITTER=60` | 10~11 分钟一轮 | 约 **430 次/天**（3 可用域 × 144 轮） |
| 对比：`45` 秒间隔 | 约 1 分钟一轮 | 约 **2900 次/天** |

后者一天要打近 3000 次 API 请求 —— 快到该被限流的量级了。

### 脚本内置的三层保护

1. **随机抖动** —— 实际间隔是 `INTERVAL + rand(0..JITTER)` 秒，不是固定节奏。
2. **限流退避** —— 一旦收到 `TooManyRequests`，本轮结束后**自动多等 5 分钟**，
   不会顶着限流继续打（这是很多同类脚本缺的）。
3. **服务上限急停** —— 收到 `LimitExceeded` 说明额度已用满或配置有误，
   再重试也不会有结果，脚本会**主动退出并发通知**，而不是永远空转刷日志。

### 想调快怎么改

编辑 `config.sh`：

```bash
INTERVAL=60     # 1 分钟一轮（约 4300 次/天，风险自负）
JITTER=30
```

```bash
sudo systemctl restart a1grab.service
```

### 怎么确认节奏真的生效了

每轮的日志都带上了**本轮耗时**和**实际休眠秒数**，不用自己推算：

```
[2026-09-22 09:08:18] 第 3 轮 → PHX-AD-1=无容量 PHX-AD-2=无容量 PHX-AD-3=无容量 ｜ 耗时 36s，休眠 611s
[2026-09-22 09:18:39] 第 4 轮 → ... ｜ 耗时 9s，休眠 634s
```

看到 `休眠 6xx` 就说明 10 分钟间隔在按预期工作。想更直接地确认，可以看服务下面的子进程：

```bash
systemctl status a1grab | grep sleep
#   └─725614 sleep 611        ← 正在睡 611 秒，落在 600+0~60 区间内
```

### 单次请求的硬超时

`grab.sh` 里所有 `oci` 调用统一走 `OCI_FLAGS`：

```bash
OCI_FLAGS=(--no-retry --connection-timeout 10 --read-timeout 45)
```

`--no-retry` 关掉 CLI 对 500 的指数退避重试（否则一次「无容量」要等 100+ 秒）；
`--read-timeout 45` 把读超时从 CLI 默认的 60 秒收紧到 45 秒，保证**一轮的最坏耗时
≈ 可用域个数 × 45 秒**，不会因为某个请求慢把整轮拖长。

**权衡很清楚**：调快能提高抢中概率，但会显著增加被限流的风险。
如果账号对你有其他用途，建议保持默认。

---

## 效果实测

在 `us-phoenix-1`（美国凤凰城）的一台 `VM.Standard.E2.1.Micro` 上：

```
[2026-09-22 08:29:14] ===== 抢机启动：VM.Standard.A1.Flex 1OCPU/6GB，间隔 600s(+0~60s)，可用域 3 个 =====
[2026-09-22 08:29:27] 第 1 轮 → PHX-AD-1=无容量 PHX-AD-2=无容量 PHX-AD-3=无容量
[2026-09-22 08:39:41] 第 2 轮 → PHX-AD-1=无容量 PHX-AD-2=无容量 PHX-AD-3=无容量
[2026-09-22 08:50:02] 第 3 轮 → PHX-AD-1=无容量 PHX-AD-2=无容量 PHX-AD-3=无容量
```

**长期显示"无容量"是正常状态，不是故障。** 热门区域的 A1 免费额度极难抢，
同类脚本普遍要跑**几天到几周**。挂着别管，抢到了会给你发微信。

> ### 📌 如果长时间完全抢不到，值得看看这条
>
> 有实测记录：某账号在东京区域用免费层跑了 **2 周、约 1500 次尝试，成功 0 次**
> （2 OCPU/12GB 试了约 800 次、1 OCPU/6GB 试了 160+ 次，全部 `Out of host capacity`）。
> 免费层账号在很多热门区域的 A1 容量**事实上不存在**。
>
> 同一篇文章的作者随后把账号**升级为按量付费（Pay As You Go）**，
> 结果**第一次请求就成功**拿到了 4 OCPU / 24 GB。
>
> 关键几点：
> - **升级后 Always Free 额度依然保留**，只要用量不超免费额度，**账单就是 0**。
> - 升级后 ARM 免费额度回到 **4 OCPU / 24 GB**（免费层账号自 2026-06-15 起被静默削减为 2 OCPU / 12 GB）。
> - 升级前**务必先设好预算告警**（Budget Alert）当保险丝：
>   `oci budgets budget create` + `oci budgets budget alert-rule create`。
> - ⚠️ **升级后要记得停掉这个脚本**。那篇文章的作者就踩了坑：
>   升级一激活，还在后台跑的抢机脚本立刻又抢了一台 2 OCPU/12GB，
>   超出免费额度，**多付了约 1 美元**。本脚本的 `SUCCESS` 标记能防重复创建，
>   但只要你之前没抢到过、脚本还在跑，升级瞬间它就会立刻成功 —— 这是特性，不是 bug。
>
> 这一条不是让你非升级不可，只是把"免费层抢不到"这个问题**存在的正解**告诉你。


---

## 前置条件

| 项 | 要求 |
|---|---|
| Oracle Cloud 账号 | 已开通 Always Free（免费层） |
| 一台常驻机器 | 用来跑脚本。**用你自己已有的 OCI 实例最省事**（免费层自带 1 台 AMD 小机器） |
| 系统 | 任意 Linux（Oracle Linux / Ubuntu / Debian 都行），需有 `bash`、`curl`、`python3` |
| 权限 | 在 OCI 控制台能创建 API Key（自己的账号就总有） |

> **不建议**在 Windows / macOS 本地跑。用 OCI 自带的免费小机器最合适：
> 本来就要常开，不额外花钱，而且和 OCI 网络同在机房内。

---

## 快速开始（一键部署）

```bash
git clone https://github.com/FBX-Dev/oracle-cloud-a1-grab.git
cd oracle-cloud-a1-grab
chmod +x *.sh
./setup.sh
```

`setup.sh` 会自动完成：装 OCI CLI（低内存机器走 venv，绕开 `dnf` OOM）→ 引导你填 OCI 凭据 →
**自动把没冒号的 fingerprint 补成正确格式** → 鉴权自检 → 生成 `config.sh` → 装 systemd 服务。

跑完看一眼进展：

```bash
tail -f grab.log
```

**抢到之后**收到微信通知，然后：

```bash
cat RESULT.txt                                   # 公网 IP 和登录命令
sudo systemctl disable --now a1grab.service      # 停掉抢机服务
```

> 部署前你需要在 OCI 控制台创建一把 API Key 并下载私钥。
> 见下面的 [Step 1](#step-1在-oci-控制台创建-api-key)。

---

## 手工部署详细步骤

不想用一键脚本、或者想搞清楚每一步在做什么，就跟着走一遍。
（下面命令假设装在 `~/a1grab`，你可以换成任意目录。）

### Step 1：在 OCI 控制台创建 API Key

OCI **没有**类似 GitHub PAT 的单个 access token，用的是 **API Key 签名认证**，
需要凑齐四要素 + 一个私钥文件。

1. 登录 [cloud.oracle.com](https://cloud.oracle.com)，点右上角头像 → **My profile**
2. 记下 **User OCID**（页面上直接显示，`ocid1.user.oc1..` 开头）
3. 点 **API keys** → **Add API key** → 选 **Generate API key pair** → **Download private key**
   - ⚠️ 私钥**只能下载一次**，保存好
4. 点 **Add**，页面上会出现 **Fingerprint**（一串十六进制）
5. 回到头像菜单 → **Tenancy: `<你的租户名>`** → 记下 **Tenancy OCID**（`ocid1.tenancy.oc1..` 开头）
6. 记下你的 **Region**（如 `us-phoenix-1`、`ap-singapore-1`），在控制台 URL 或区域选择器里能看到

### Step 2：在机器上安装 OCI CLI

> **⚠️ 关键坑：在低内存小机器上千万不要用 `dnf install python3-pip`。**
>
> Oracle 免费层的 AMD 小机器 `VM.Standard.E2.1.Micro` 物理内存**只有约 498MB**
> （虽然套餐页写 1GB）。实测：
> ```
> sudo dnf install -y python3-pip
> → Killed  (rc=137)     # 被 OOM Killer 干掉
> ```
>
> **正确做法**：RHEL / Oracle Linux 9 的 `python3 -m venv` 自带 `ensurepip`，
> 能直接产出 pip，全程不碰 dnf。

```bash
python3 -m venv ~/ocicli
~/ocicli/bin/pip install --no-cache-dir --upgrade pip
~/ocicli/bin/pip install --no-cache-dir oci-cli

# 验证（约 3.93.0）
~/ocicli/bin/oci --version
```

> 装包要几分钟。**建议丢到后台跑，避免 SSH 会话超时把安装掐断：**
> ```bash
> setsid nohup ~/install.sh > ~/install.log 2>&1 < /dev/null &
> ```

### Step 3：写 `~/.oci/config`

> **⚠️ 头号高频错误：`fingerprint` 必须是冒号分隔的十六进制。**
>
> 从控制台复制出来的通常**没有冒号**，直接填会报：
> ```
> ERROR: The config file at ~/.oci/config is invalid:
> +-------------+-----------+----------------------------------------+
> | fingerprint | malformed | openssl rsa -pubout -outform DER ...   |
> +-------------+-----------+----------------------------------------+
> ```
>
> ✅ 正确：`aa:bb:11:22:cc:dd:33:44:ee:ff:55:66:77:88:00:99`
> ❌ 错误：`aabb1122ccdd3344eeff556677880099`
>
> `setup.sh` 会自动帮你补冒号。手工配置的话注意一下。

```bash
mkdir -p ~/.oci && chmod 700 ~/.oci
cp /path/to/刚下载的私钥.pem ~/.oci/oci_api_key.pem
chmod 600 ~/.oci/oci_api_key.pem

cat > ~/.oci/config <<'EOF'
[DEFAULT]
user=ocid1.user.oc1..你的User OCID
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
tenancy=ocid1.tenancy.oc1..你的Tenancy OCID
region=us-phoenix-1
key_file=/home/你的用户名/.oci/oci_api_key.pem
EOF
chmod 600 ~/.oci/config
```

**不确定指纹对不对？用私钥反算，和 config 里比对：**

```bash
openssl rsa -pubout -outform DER -in ~/.oci/oci_api_key.pem | openssl md5 -c
```

### Step 4：鉴权自检

```bash
~/ocicli/bin/oci --no-retry iam region-subscription list --output table
```

正常应输出类似：

```
+----------------+------------+--------------+--------+
| is-home-region | region-key | region-name  | status |
+----------------+------------+--------------+--------+
| True           | PHX        | us-phoenix-1 | READY  |
+----------------+------------+--------------+--------+
```

报错的话对照 [常见问题](#常见问题与排错)。

### Step 5：部署抢机脚本

```bash
mkdir -p ~/a1grab && cd ~/a1grab

# 把仓库里的文件拷过来
cp /path/to/oracle-cloud-a1-grab/{grab.sh,notify.sh,status.sh,a1grab.service,config.example.sh} .
chmod +x grab.sh notify.sh status.sh

# 生成配置并填写
cp config.example.sh config.sh
vi config.sh          # 至少填 COMPARTMENT
```

`config.sh` 里**大部分项可以留空自动发现**，见 [配置项说明](#配置项说明)。

**先跑一次空转检查**，确认参数和鉴权都对（不会创建任何实例）：

```bash
DRY_RUN=1 ./grab.sh
```

正常输出形如：

```
===== DRY RUN（不会创建实例）=====
oci 可执行文件 : /home/opc/ocicli/bin/oci
compartment    : ocid1.tenancy.oc1..xxxx
可用域 (3)     : xxxx:PHX-AD-1 xxxx:PHX-AD-2 xxxx:PHX-AD-3
子网           : ocid1.subnet.oc1.phx.xxxx
镜像           : ocid1.image.oc1.phx.xxxx
规格           : VM.Standard.A1.Flex  1 OCPU / 6 GB

--- 试跑一次创建请求（预期会因无容量失败）---
    "message": "Out of host capacity.",
↑ 出现 "Out of host capacity." 属于正常，说明鉴权/参数全部正确。
```

看到 `Out of host capacity.` 就说明一切正常 —— 网络通、鉴权过、参数对，只是没容量。

确认无误后正式跑：

```bash
./grab.sh
cat grab.log
```

### Step 6：配置成常驻服务

`a1grab.service` 里用的是 `%h/a1grab`（`%h` 会被 systemd 展开成 `User=` 的 home 目录），
所以**只要改一下 `User=`** 就能适配你的机器：

```bash
# 把 User=opc 改成你自己的用户名
sed "s/^User=.*/User=$(id -un)/" a1grab.service | sudo tee /etc/systemd/system/a1grab.service

sudo systemctl daemon-reload
sudo systemctl enable --now a1grab.service

systemctl status a1grab.service
```

> **`a1grab.service` 里的资源上限别删**（`MemoryHigh=220M` / `MemoryMax=300M`）。
> 在小内存机器上，这是防止 `oci` 这个 Python 进程把系统压到 OOM 的保险丝。
> 脚本本身只需约 100MB，300M 上限很宽松。

### Step 7：配置通知（可选但强烈建议）

抢到了却不知道，等于白抢。通知逻辑在独立的 `notify.sh` 里，**可以单独测**：

```bash
./notify.sh "测试标题" "这是一条测试消息<br>支持 html"
```

**渠道一：PushPlus（推到微信，国内推荐）**

```bash
echo '你的PushPlus token' > pushplus_token
./notify.sh "测试" "通知链路测试"
```

token 在 [pushplus.plus](https://www.pushplus.plus) 个人中心 → 消息 token 获取，并关注它的微信公众号。

> **注意别搞混**：PushPlus 发消息**只需要 token**。
> 它的 `secretKey` 是「开放接口」（查群组/好友/消息记录）用的，
> **发通知用不上，也不需要加签**。所以这里只存 token。

**渠道二：任意 GET 型 webhook**

```bash
echo 'https://你的webhook地址' > notify_url
```

企业微信群机器人、Bark、Server酱、钉钉机器人都行 —— 只要是访问该 URL 就发消息的。

两个文件都不存在时，脚本会**静默跳过通知**，不影响抢机本身。

---

## 使用方式

```bash
./status.sh            # 一键查看：服务状态 / 是否抢到 / 最近日志
tail -f grab.log       # 实时盯日志（约每分钟一行）
DRY_RUN=1 ./grab.sh    # 空转检查：只做参数发现与鉴权自检，不创建实例
./grab.sh              # 手工跑一轮
./notify.sh "标题" "内容"   # 手动测通知
```

**抢到之后：**

```bash
cat RESULT.txt
# 抢机时间: 2026-09-21 13:45:02
# 可用域  : xxxx:PHX-AD-1
# 实例ID  : ocid1.instance.oc1.phx.xxxx
# 公网IP  : 1.2.3.4
# 规格    : VM.Standard.A1.Flex  1 OCPU / 6 GB
# 登录    : ssh -i <你的私钥> opc@1.2.3.4

sudo systemctl disable --now a1grab.service   # 停掉，别让它再抢
```

同时你会收到一条微信推送，内容是同样的信息。

---

## 每日 HTML 日报

抢机是长期挂机的事，光看 `grab.log` 很难判断「今天到底跑了多少、哪里在跑、有没有被限流」。
`report.py` 把日志汇总成一份**自包含的 HTML 日报**（内联 CSS/SVG，无任何外部依赖，断网也能打开）。

### 它长什么样

一份日报包含 5 块：

| 区块 | 内容 |
|---|---|
| 今日概览 | 轮询轮次、请求次数、抢到次数、限流次数、异常次数、平均间隔 |
| 轮询时段分布 | 24 小时柱状图，一眼看出哪个时段在跑、有没有断档 |
| 轮询目标 | 每个可用域各发了多少请求、各自的结果分布 |
| 结果分布 | 容量不足 / 限流 / 异常 / 成功的次数与占比 |
| 运行健康检查 | 节奏是否落在设计区间、有没有超长间隔、全天是否零限流 |
| 最近 12 轮明细 | 每轮的三个可用域结果、单轮耗时、实际休眠秒数 |

外加一个 `index.html` 目录页，按日期列出所有日报和累计数据。

### 用法

```bash
python3 report.py                  # 生成今天 + 昨天
python3 report.py --date 2026-11-09
python3 report.py --days 14        # 回填最近 14 天
python3 report.py --all            # 全部历史
```

输出到 `./reports/report-YYYY-MM-DD.html` 与 `./reports/index.html`。

### 设为每日自动生成

```bash
sudo cp a1grab-report.service a1grab-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now a1grab-report.timer

systemctl list-timers a1grab-report.timer      # 看下次执行时间
```

每天 00:05 生成**昨天**的完整日报（带 2 分钟随机偏移）。
今天的半日数据不自动生成，因为整天没跑完就出报表没意义——想看当日进度就手动跑 `report.py --days 2`。

### 设计取舍：为什么解析日志而不是另写 CSV

常见做法是让脚本每轮往 CSV 追加一行，再单独出报表。本项目**没有**这么做，原因是：

1. **不动正在跑的抢机脚本**。改 `grab.sh` 是最高风险的操作，一旦写出语法错误，抢机就悄悄停了，而你还以为它在跑。
2. **日志里本来就有全部信息**。每轮一行的 `第 N 轮 → PHX-AD-1=无容量 ... ｜ 耗时 13s，休眠 628s`
   已经包含时间、轮次、三个可用域结果、耗时、休眠时长，解析比双写更省事。
3. **历史数据免回填**。日志里已有的几十天记录，一条命令就能生成过去的日报，不需要等数据慢慢攒。

代价是解析依赖日志格式，所以 `grab.sh` 改动日志格式时必须同步改 `report.py` 的正则。
目前日志只有 5 种行类型（轮次汇总 / 启动标记 / 限流警告 / 非容量错误 / 成功），解析是稳定的。

### 对服务器压力有多大

实测（498MB 的 E2.1.Micro）：

| 指标 | 数值 |
|---|---|
| 单份报表耗时 | **0.34 秒** |
| 内存峰值 | **18.3 MB** |
| CPU 时间 | 0.19 秒 |
| 磁盘（每份） | 约 13 KB |
| 每日总开销 | 一次 0.34 秒的进程，占全天 0.0004% |

unit 里仍然给了 `MemoryMax=150M` / `CPUQuota=30%` 的保险丝，但正常情况下远用不到。
**结论：这个日报对服务器压力基本可以忽略**，比 `dnf-makecache` 那种定时任务轻了几个数量级。

### 怎么看

三种方式，按省事程度排：

1. **本地拉取**（最安全，推荐）：用 `scp` 把 `reports/*.html` 拉到本机，双击打开。
   仓库外配套的 `a1-reports.bat` 就是干这个的：先让服务器刷新今天+昨天的报表，再 `scp` 全量下载，最后自动打开目录页。
2. **内网/跳板机访问**：如果这台机器本来就有内网入口，直接用现成的即可。
3. **HTTP 托管**（谨慎）：`python3 -m http.server` 或 Caddy 都能托管，但**必须**加认证：

```bash
# 仅监听本机 + basic auth，不要裸奔到公网
caddy file-server --root ~/a1grab/reports --listen 127.0.0.1:8080
```

⚠️ 报表里含服务器 IP、可用域、实例规格等信息。**不要把这个目录暴露到公网**，
也不要给它套一个容易被扫到的路径名——那等于把自己的抢机节奏公开出去。

### 历史数据怎么清理

一份日报 13 KB，一年 365 份约 4.7 MB，**基本不用担心**。
真要清理就删旧的 `.html`，但保留 `index.html` 会自动重建：

```bash
cd ~/a1grab/reports && ls -1 report-*.html | head -n -90 | xargs -r rm
python3 ../report.py --index-only
```

---

## 配置项说明

全部写在 `config.sh` 里，对照 `config.example.sh` 的注释即可。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `COMPARTMENT` | ✅ | — | compartment OCID。个人账号直接填 tenancy OCID |
| `SUBNET` | ⭕ | 自动 | 子网 OCID。**留空时只在检测到恰好 1 个子网才自动选中**，多个会报错 |
| `ADS` | ⭕ | 自动 | 可用域列表。留空 = 自动发现全部 |
| `IMAGE` | ⭕ | `auto` | `auto` = 自动挑该区域**最新的** ARM 镜像（实测会拿到 Oracle Linux 10.x）。想固定版本见下方 FAQ |
| `IMAGE_OS` | ⭕ | `Oracle Linux` | 也可填 `Canonical Ubuntu` |
| `OCPUS` / `MEMGB` | ⭕ | `1` / `6` | 免费层额度 2/12、PAYG 为 4/24，但**小规格更容易捡到漏** |
| `INTERVAL` / `JITTER` | ⭕ | `600` / `60` | 每轮间隔 = `INTERVAL + rand(0..JITTER)` 秒。默认 10 分钟，见 [调频与风控](#调频与风控) |
| `NAME` | ⭕ | `a1-free` | 实例显示名 |
| `SSH_PUBKEY_FILE` | ✅ | `~/.ssh/id_rsa.pub` | 注入到新实例 `authorized_keys` 的公钥 |
| `USER_NAME` | ⭕ | `opc` | 登录用户名（OL / Ubuntu 官方镜像都是 `opc`） |

---

## 工作原理

```
每轮循环：
  for 每个可用域:
      oci --no-retry compute instance launch   # 4 秒
        ├─ 成功 ──────────────→ 记录实例ID/公网IP → 发通知 → 写 SUCCESS 标记 → 退出
        ├─ Out of host capacity → 记"无容量"，继续下一个可用域
        ├─ TooManyRequests ───→ 记"限流"，本轮结束后额外等 5 分钟
        ├─ LimitExceeded ─────→ 发通知并主动退出（再试也没用）
        └─ 其它错误 ──────────→ 记"异常"，完整记录一次便于排查
  log 一行汇总
  sleep INTERVAL + rand(0..JITTER) + 额外退避
```

- **`--no-retry`** 是效率核心，见 [核心亮点](#核心亮点)。
- **错误分类**是第二个关键设计：只有"容量类"错误才该无限重试。
  `LimitExceeded`（额度用满/配置错）继续重试永远不会有结果，
  所以脚本选择**停下并通知你**，而不是刷满一屏幕同样的错误。
- **`--no-retry` 之外的调用**（查可用域/子网/镜像）也都加了它，避免偶发 5xx 拖慢启动。
- **幂等**：启动先检查 `SUCCESS` 文件，存在就直接退出。
  所以 systemd 的 `Restart=always` 在抢到之后只会每 120 秒空跑一次，**不会重复创建实例**。
- **内存守卫**：每轮开始先看 `MemAvailable`，低于 110MB 就跳过本轮，让系统先缓缓。
- **日志瘦身**：每轮一行，超过 8000 行自动只留最后 2000 行，不会撑爆小磁盘。
- **参数自动发现**：可用域 / 子网 / 镜像都运行时查，不写死在脚本里。

---

## 常见问题与排错

<details>
<summary><b>日志一直显示"无容量"，是不是脚本坏了？</b></summary>

不是。这是**最常见也最正常**的状态。热门区域（us-phoenix-1、ap-singapore-1 等）
的 A1 免费额度长期处于满负荷，容量只在有人释放时短暂出现。

同类脚本普遍要跑**几天到几周**才能抢到。挂了就别管了，抢到会发微信。

想提高概率：确保三个可用域都在跑（日志里应出现 `PHX-AD-1/2/3` 三个）、
用 1 OCPU/6GB 小规格、别把 `INTERVAL` 调得太激进（会被限流）。
</details>

<details>
<summary><b>日志里出现"限流"或脚本突然停了，怎么办？</b></summary>

分两种情况：

- **`=限流`**（对应 `TooManyRequests`）：说明轮询太密被服务端限速了。
  脚本会自动多等 5 分钟再继续。如果频繁出现，把 `INTERVAL` 调大：

  ```bash
  INTERVAL=900     # 改成 15 分钟
  ```

- **脚本退出，日志里是 `LimitExceeded`**：这不是限流，而是**服务额度用满**
  （或配置有问题，比如镜像架构不对）。再重试也不会有结果，所以脚本主动停了。
  去控制台确认一下 A1 额度是不是已经用完，或者是不是已经抢到过实例了。

  想重新开始跑：

  ```bash
  sudo systemctl start a1grab.service
  ```
</details>

<details>
<summary><b>怎么确认自己有没有被限流/风控？</b></summary>

正常的"没容量"返回长这样，**不用管**：

```json
{ "code": "InternalError", "message": "Out of host capacity." }
```

需要警惕的是这几种：

- `TooManyRequests` —— 被限速，说明频率太高
- `Too many requests for the user` —— **同一个意思的另一种措辞**（用户级限流，
  带空格、仅首字母大写）。⚠️ 用 `grep "TooManyRequests"` 是**抓不到**这一条的，
  脚本里必须用 `grep -qiE "TooManyRequests|Too many requests"` 才算认全
- 收到 Oracle 的邮件，提到账号审查 / abuse —— 立刻停掉脚本，检查邮箱

命中的话，脚本会在本轮结束后**自动多等 5 分钟**再继续（日志记 `=限流`）。
如果在日志里看到 `=异常` 且原文是上面两条之一，说明你的版本判定写窄了，去更新 `grab.sh`。

本项目默认 10 分钟间隔就是为了避开这个区间。如果账号还有别的用途，
**不要**为了抢机把频率拉到分钟级以下。
</details>

<details>
<summary><b>运行几天后，发现轮询间隔有时变成几十分钟，是什么原因？</b></summary>

**先怀疑本机的系统定时任务，别急着怪 Oracle。**（2026-09-24 实测踩坑）

Oracle Linux 默认开着 `dnf-makecache.timer`，每约 2 小时跑一次，会吃掉约 **360MB** 内存。
在 498MB 的 E2.1.Micro 上这足以把整机打到 OOM —— 实测 **95 次 OOM 全部是它**，
每次卡死十几分钟才被内核杀掉。**光白等就吃掉了 11.5% 的运行时间**（55.9 小时里白等 6.4 小时，
最长一次 71 分钟没发请求）。

自己复查：

```bash
sudo systemctl disable --now dnf-makecache.timer
sudo dmesg -T | grep "Out of memory: Killed process" | tail      # 看历史
grep -c "Out of memory" <(sudo dmesg -T)                         # 记住这个数当基线
```

`setup.sh` 从 2026-09-24 起会**自动关闭**这个定时器（以及 PCP 的 `pmlogger_*` / `pmie_*`
这些每 30 分钟跑一次、对抢机毫无用处的监控任务）。副作用仅「以后跑 `dnf install` 会慢一点」。

还有两个连带症状，认识了就不会误判：

- **`Nice=19` + `IOSchedulingClass=idle` 会让情况更糟。** 内存抖动时脚本的日志写入
  被压到最低 I/O 优先级，会被饿住，间隔进一步虚增。本项目的 `a1grab.service` 已改为
  `Nice=10` + `best-effort`（**如果你是从旧版本安装的，请更新这个文件**）。
- **日志里可能出现 `Date ... is not within allowed clock skew`。** 这不是时钟配置问题
  （`chronyc tracking` 平时精确到微秒），而是机器刚卡过、时钟落后了，OCI 于是拒签请求。
  它和长间隔是同一个根因的两个症状。
</details>

<details>
<summary><b>我一条通知都没收到，是通知渠道坏了吗？</b></summary>

大概率不是。**脚本只在两种情况发通知**：① 抢到了实例；② 遇到 `LimitExceeded`
（额度用满或配置有误）需要人工介入。

所以**长期没有通知 = 还没抢到**，属于正常状态。

想确认渠道是否可用，直接手工发一条：

```bash
~/a1grab/notify.sh "测试" "这是一条测试消息"
```

期望输出 `PushPlus 推送成功`。

注意 PushPlus 的**默认渠道是微信**，不是邮件。想收邮件需要在
[pushplus.plus](https://www.pushplus.plus) 账号里先**绑定并验证邮箱**，
否则接口会返回 `{"code":600,"data":"接收用户邮箱为空"}`。
</details>

<details>
<summary><b>报 <code>Config Errors | fingerprint | malformed</code></b></summary>

指纹格式不对。必须是**冒号分隔**的 16 组两位十六进制：

```bash
# 用私钥反算正确指纹
openssl rsa -pubout -outform DER -in ~/.oci/oci_api_key.pem | openssl md5 -c
```

把结果填进 `~/.oci/config` 的 `fingerprint=`。
</details>

<details>
<summary><b><code>dnf install python3-pip</code> 被 Killed（rc=137）</b></summary>

内存不够，被 OOM Killer 杀了。**别用 dnf**，改用 venv：

```bash
python3 -m venv ~/ocicli
~/ocicli/bin/pip install --no-cache-dir --upgrade pip
~/ocicli/bin/pip install --no-cache-dir oci-cli
```
</details>

<details>
<summary><b>报 <code>NotAuthenticated</code> / <code>NotAuthorizedOrNotFound</code></b></summary>

按顺序排查：
1. `~/.oci/config` 里的 user / tenancy OCID 有没有粘错（多空格、换行）
2. `key_file` 路径对不对，权限是不是 600
3. 私钥和 fingerprint 是不是配套的（用上面的 openssl 命令核对）
4. 刚创建的 API Key 可能要等约 1 分钟才生效
</details>

<details>
<summary><b>报 <code>LexerError: Bad jmespath expression</code></b></summary>

`--query` 里用了非 ASCII 键名。**JMESPath 的键名必须用 ASCII**：

```bash
# ❌ 会报错
--query 'data[].{名称:"display-name"}'
# ✅ 正确
--query 'data[].{name:"display-name"}'
```
</details>

<details>
<summary><b>服务起不来 / 反复重启</b></summary>

```bash
sudo journalctl -u a1grab.service -n 50 --no-pager
```

常见原因：`a1grab.service` 里的 `User=` 没改成你的用户名、
`config.sh` 里 `COMPARTMENT` 没填、`SSH_PUBKEY_FILE` 指向的公钥不存在。
</details>

<details>
<summary><b>怎么改抢的规格（比如改成 4 OCPU / 24GB）？</b></summary>

编辑 `config.sh`：

```bash
OCPUS=4
MEMGB=24
```

然后 `sudo systemctl restart a1grab.service`。

不过**小规格更容易抢到**（4/24 是很多人盯着的目标）。建议先抢到 1/6，用起来再说。

> **额度到底是多少？这里容易搞混：**
>
> | 账号类型 | A1 免费额度 |
> |---|---|
> | 免费层（Free Tier） | **2 OCPU / 12 GB**（2026-06-15 起被静默削减） |
> | 按量付费（PAYG） | **4 OCPU / 24 GB**，且免费额度内账单为 0 |
>
> 另外有个反直觉的点：**控制台滑块卡在 1 OCPU / 6 GB 只是免费层的 UI 限制**，
> 不是真实的服务上限。查一下就知道：
>
> ```bash
> oci limits value list --service-name compute --name standard-a1-core-count
> # 可能返回 41 之类的大数字 —— 那是你这个 tenancy 的服务上限
> ```
>
> 也就是说 API 会**接受** 2/12 甚至更大的请求，只是照样回你 `Out of host capacity`。
> 抢到 1/6 之后，剩下的额度理论上还能再开实例，但受同一个容量池限制。
</details>

<details>
<summary><b>怎么换成指定版本的系统镜像（比如不用最新的 Oracle Linux 10）？</b></summary>

`IMAGE="auto"` 会挑**最新**的 ARM 镜像（实测是 Oracle Linux 10.x）。
想固定版本，先列出可选镜像：

```bash
oci compute image list --compartment-id "$COMPARTMENT" \
  --operating-system 'Oracle Linux' --shape VM.Standard.A1.Flex \
  --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0:10].{name:"display-name",id:id}' --output table
```

把想要的镜像 OCID 填进 `config.sh`：

```bash
IMAGE="ocid1.image.oc1.xxxxx..xxxxx"
```

然后 `sudo systemctl restart a1grab.service`。
</details>

<details>
<summary><b>能改成抢别的区域吗？</b></summary>

只能抢你账号已订阅的区域。`config.sh` 不用改区域 —— 区域由 `~/.oci/config` 里的
`region=` 决定。想抢别的区域，在那个区域有订阅的前提下改 `region=` 即可。
</details>

---

## 安全提醒

1. **API Key 权限极高。** 它等同于你账号的完整 API 权限（能创建/删除任何资源）。
   抢到实例后，建议去控制台删掉这把 Key：
   `右上角头像 → My profile → API keys → 删除对应指纹`。
   删掉**不影响已经建好的实例**，只是这台机器以后不能再调 OCI API。

2. **私钥别提交进 Git。** 本仓库的 `.gitignore` 已经排除了 `config.sh`、
   `*.pem`、`*.key`、`pushplus_token`、`notify_url` 等敏感文件。

3. **`pushplus_token` 泄露风险较低** —— 最多被人拿去给你发骚扰微信，读不到你的账号数据。
   但仍建议当作凭据对待，不要公开。

4. **不要把私钥放到服务器上再删本地。** 本地留一份，服务器上的用完就删。

---

## 免责声明

- 本项目仅用于自动化**你自己账号**的合法操作，请遵守
  [Oracle Cloud 服务协议](https://www.oracle.com/legal/cloud-services-agreements/)。
- 免费额度政策由 Oracle 决定并可能变化，本项目不保证一定能抢到，也不保证额度长期有效。
- 请勿用于批量注册、薅羊毛、转卖等滥用行为。
- 使用本项目产生的任何后果由使用者自行承担。

## 贡献

欢迎提 Issue / PR。如果你在某个区域实测抢到了，也欢迎回来分享一下区域和等待时长，
对其他人是很有用的参考。

## License

[MIT](LICENSE)

---

## English TL;DR

An auto-retry script for grabbing Oracle Cloud's **Always Free `VM.Standard.A1.Flex`** (ARM) instance.

**The single most important detail:** OCI CLI retries 5xx errors with exponential backoff,
and "no capacity" is reported as a 500 (`InternalError` / `Out of host capacity.`).
A single attempt therefore takes **104 seconds** by default, but only **3.7 seconds**
with the global `--no-retry` flag — roughly **30× faster**, which is what actually
determines whether you win the race.

```bash
git clone https://github.com/FBX-Dev/oracle-cloud-a1-grab.git
cd oracle-cloud-a1-grab && chmod +x *.sh && ./setup.sh
tail -f grab.log
```

Features: rotates across all availability domains · auto-discovers ADs/subnet/image ·
low-memory friendly (avoids the `dnf` OOM trap on 498 MB free-tier VMs) ·
idempotent (never creates a duplicate instance) · optional WeChat push via PushPlus ·
systemd service with memory caps · one log line per round.

Works on any Linux with `bash`, `curl` and `python3`.
