#!/usr/bin/env bash
# ============================================================================
#  Oracle Cloud 「VM.Standard.A1.Flex」免费 ARM 实例 自动抢机脚本
#
#  轮询同一区域的多个可用域，反复尝试创建 Always Free 额度的 ARM 实例，
#  一旦某个可用域放出容量就立刻抓住，并（可选）推送通知到微信。
#
#  依赖：oci-cli（见 README 的安装章节）
#  用法：./grab.sh          （参数全部来自 config.sh）
#
#  许可证：MIT
# ============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${A1GRAB_HOME:-$SCRIPT_DIR}"

# ---- 定位 oci 可执行文件：优先 PATH，其次本仓库约定的 venv 安装位置 ----
if command -v oci >/dev/null 2>&1; then
  OCI="$(command -v oci)"
elif [ -x "$HOME/ocicli/bin/oci" ]; then
  OCI="$HOME/ocicli/bin/oci"
else
  echo "错误：找不到 oci 命令。请先按 README 安装 oci-cli。" >&2
  exit 1
fi

# ---- 单次 API 调用的固定参数与硬超时 ----
# --no-retry  ：CLI 默认对 500 做指数退避重试，一次 "Out of host capacity"
#               会被拖到 100+ 秒；关掉后只要 ~3 秒。
# --read-timeout：oci-cli 默认读超时 60s，这里收紧到 45s，把「一轮」的最坏
#               耗时钉在 可用域个数 × 45s 以内，避免个别请求慢把整轮拖长。
OCI_FLAGS=(--no-retry --connection-timeout 10 --read-timeout 45)

LOG="$BASE/grab.log"
FLAG="$BASE/SUCCESS"
RESULT="$BASE/instance.json"

mkdir -p "$BASE"

# ---- 载入用户配置 ----
if [ -f "$BASE/config.sh" ]; then
  # shellcheck source=/dev/null
  . "$BASE/config.sh"
else
  echo "错误：找不到 $BASE/config.sh。请复制 config.example.sh 并填写。" >&2
  exit 1
fi

# ---- 默认值（config.sh 里没写就取这里）----
: "${MODEL:=VM.Standard.A1.Flex}"
: "${OCPUS:=1}"
: "${MEMGB:=6}"
: "${INTERVAL:=540}"     # 常规间隔均值（秒），实际取 INTERVAL±JITTER
: "${JITTER:=60}"        # 抖动幅度（秒）：常规区间 480~600 秒
: "${REST_PCT:=5}"       # 长休概率（%）：模拟"人不会一直守在电脑前"
: "${REST_MIN:=900}"     # 长休最短（秒）= 15 分钟
: "${REST_MAX:=2700}"    # 长休最长（秒）= 45 分钟
: "${NAME:=a1-free}"     # 实例显示名
: "${IMAGE:=auto}"       # auto = 自动挑该区域最新的 ARM 镜像
: "${IMAGE_OS:=Oracle Linux}"
: "${ADS:=}"             # 留空 = 自动发现全部可用域
: "${SSH_PUBKEY_FILE:=$HOME/.ssh/id_rsa.pub}"
: "${USER_NAME:=opc}"    # 登录用户名（Oracle Linux / Ubuntu 默认都是 opc）

log() { echo "[$(date '+%F %T')] $*" >>"$LOG"; }

# ---- 日志瘦身：每轮一行（约 1 行/分钟），超 8000 行只留最后 2000 行 ----
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 8000 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# ---- 幂等：已经抢到过就直接退出，避免重复创建 ----
if [ -f "$FLAG" ]; then
  log "已存在成功标记，退出。"
  exit 0
fi

# ---- 校验必填项 ----
if [ -z "${COMPARTMENT:-}" ]; then
  log "致命：config.sh 里没有设置 COMPARTMENT。"
  exit 1
fi

SSHKEY="$(cat "$SSH_PUBKEY_FILE" 2>/dev/null)"
if [ -z "$SSHKEY" ]; then
  log "致命：读不到公钥 $SSH_PUBKEY_FILE，无法注入登录密钥。"
  exit 1
fi

# ---------------------------------------------------------------------------
#  参数自动发现
#  （把可用域/子网/镜像写死在脚本里既容易被误提交、也换区域就失效，
#    所以默认全部运行时查询。只在服务启动时查一次，开销可忽略。）
# ---------------------------------------------------------------------------

# 可用域
if [ -z "$ADS" ] || [ "$ADS" = "auto" ]; then
  AD_LIST="$($OCI "${OCI_FLAGS[@]}" iam availability-domain list \
              --compartment-id "$COMPARTMENT" \
              --query 'data[].name' --raw-output 2>/dev/null \
              | awk -F'"' '/"/{print $2}')"
  if [ -z "$AD_LIST" ]; then
    log "致命：拿不到可用域列表，检查 region / 凭据是否正确。"
    exit 1
  fi
  # shellcheck disable=SC2206
  ADS=($AD_LIST)
fi

# 子网：config.sh 里没写就自动挑（只有一个才敢自动挑）
if [ -z "${SUBNET:-}" ]; then
  SUBNET_COUNT="$($OCI "${OCI_FLAGS[@]}" network subnet list --compartment-id "$COMPARTMENT" \
                    --query 'length(data)' --raw-output 2>/dev/null)"
  if [ "$SUBNET_COUNT" = "1" ]; then
    SUBNET="$($OCI "${OCI_FLAGS[@]}" network subnet list --compartment-id "$COMPARTMENT" \
                --query 'data[0].id' --raw-output 2>/dev/null)"
    log "自动选用唯一子网：$SUBNET"
  else
    log "致命：检测到 ${SUBNET_COUNT:-0} 个子网，无法自动选择。请在 config.sh 里指定 SUBNET。"
    log "  列出子网：oci network subnet list --compartment-id \$COMPARTMENT --query 'data[].{name:\"display-name\",id:id}' --output table"
    exit 1
  fi
fi

# 镜像：自动挑最新的 ARM 镜像
if [ -z "$IMAGE" ] || [ "$IMAGE" = "auto" ]; then
  IMAGE="$($OCI "${OCI_FLAGS[@]}" compute image list \
            --compartment-id "$COMPARTMENT" \
            --operating-system "$IMAGE_OS" \
            --shape "$MODEL" \
            --sort-by TIMECREATED --sort-order DESC \
            --query 'data[0].id' --raw-output 2>/dev/null)"
  if [ -z "$IMAGE" ] || [ "$IMAGE" = "null" ]; then
    log "致命：找不到 $IMAGE_OS 的 ARM 镜像，可用 IMAGE_OS 改成 'Canonical Ubuntu' 等再试。"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
notify() {
  # 参数：标题 正文。交给 notify.sh 处理，未配置渠道时静默跳过。
  if [ -x "$BASE/notify.sh" ]; then
    local r
    r="$("$BASE/notify.sh" "$1" "$2" 2>&1)"
    log "通知：$r"
  else
    log "通知：未找到 notify.sh，跳过。"
  fi
}

# ---------------------------------------------------------------------------
#  DRY_RUN=1 只做参数发现与校验，不会创建任何实例。
#  第一次部署时强烈建议先跑一次，确认参数都对：
#      DRY_RUN=1 ./grab.sh
# ---------------------------------------------------------------------------
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "===== DRY RUN（不会创建实例）====="
  echo "oci 可执行文件 : $OCI"
  echo "compartment    : $COMPARTMENT"
  echo "可用域         : ${#ADS[@]} 个 → ${ADS[*]}"
  echo "子网           : $SUBNET"
  echo "镜像           : $IMAGE"
  echo "规格           : $MODEL  ${OCPUS} OCPU / ${MEMGB} GB"
  echo "公钥文件       : $SSH_PUBKEY_FILE"
  echo "轮询间隔       : ${INTERVAL}s + 0~${JITTER}s"
  echo
  echo "--- 试跑一次创建请求（预期会因无容量失败）---"
  _dry=$($OCI "${OCI_FLAGS[@]}" compute instance launch \
    --compartment-id "$COMPARTMENT" \
    --availability-domain "${ADS[0]}" \
    --shape "$MODEL" \
    --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMGB}" \
    --image-id "$IMAGE" \
    --subnet-id "$SUBNET" \
    --assign-public-ip true \
    --display-name "$NAME" \
    --metadata "{\"ssh_authorized_keys\":\"$SSHKEY\"}" \
    --query 'data.id' --raw-output 2>&1)
  if printf '%s' "$_dry" | grep -q '^ocid1\.instance\.'; then
    echo "⚠️  意外真的创建成功了！实例 OCID: $_dry"
    echo "    请立刻去控制台确认，若不需要请自行终止。"
  else
    echo "$_dry" | grep -o '"message": *"[^"]*"' | head -1 || echo "$_dry"
    echo
    echo "↑ 出现 \"Out of host capacity.\" 属于正常，说明鉴权/参数全部正确。"
  fi
  exit 0
fi

# ---- 内存守卫：小机器（如 E2.1.Micro 只有 ~500MB）别被 Python 进程压垮 ----
avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 999)
if [ "${avail:-0}" -lt 110 ]; then
  log "可用内存仅 ${avail}MB，本轮跳过，等下一轮。"
  exit 0
fi

log "===== 抢机启动：$MODEL ${OCPUS}OCPU/${MEMGB}GB，间隔 ${INTERVAL}s±${JITTER}s，${REST_PCT}% 概率长休 ${REST_MIN}~${REST_MAX}s，可用域 ${#ADS[@]} 个 ====="

round=0
last_msg=""
extra=0

while :; do
  [ -f "$FLAG" ] && { log "检测到成功标记，退出循环。"; exit 0; }
  round=$((round + 1))
  t_round=$(date +%s)

  # 日志瘦身：启动时那次检查在"长期不重启"的场景下永远不会再执行
  # （实测已连续运行 24 天），所以每 500 轮补检一次，保住原设计意图。
  if [ $((round % 500)) -eq 0 ] && [ "$(wc -l <"$LOG")" -gt 8000 ]; then
    tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
  summary=""
  extra=0

  for AD in "${ADS[@]}"; do
    [ -f "$FLAG" ] && exit 0

    # --no-retry 是关键：CLI 默认对 500 错误做指数退避重试，
    # 一次 "Out of host capacity" 会被拖到 100+ 秒；关掉后只要 ~4 秒。
    out=$($OCI "${OCI_FLAGS[@]}" compute instance launch \
      --compartment-id "$COMPARTMENT" \
      --availability-domain "$AD" \
      --shape "$MODEL" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMGB}" \
      --image-id "$IMAGE" \
      --subnet-id "$SUBNET" \
      --assign-public-ip true \
      --display-name "$NAME" \
      --metadata "{\"ssh_authorized_keys\":\"$SSHKEY\"}" \
      --query 'data.id' --raw-output 2>&1)
    rc=$?

    if [ $rc -eq 0 ] && printf '%s' "$out" | grep -q '^ocid1\.instance\.'; then
      # ================== 成功 ==================
      IID="$out"
      log "★★★ 抢到了！AD=$AD  实例ID=$IID"
      $OCI "${OCI_FLAGS[@]}" compute instance get --instance-id "$IID" >"$RESULT" 2>/dev/null

      # 等网卡就绪，取公网 IP
      MIP="(等待中)"
      for _ in $(seq 1 40); do
        MIP=$($OCI "${OCI_FLAGS[@]}" compute instance list-vnics --instance-id "$IID" \
              --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
        case "$MIP" in
          ""|"null"|"None") sleep 6 ;;
          *) break ;;
        esac
      done
      log "公网 IP：$MIP"

      {
        echo "抢机时间: $(date '+%F %T')"
        echo "可用域  : $AD"
        echo "实例ID  : $IID"
        echo "公网IP  : $MIP"
        echo "规格    : $MODEL  ${OCPUS} OCPU / ${MEMGB} GB"
        echo "登录    : ssh -i <你的私钥> ${USER_NAME:-opc}@$MIP"
      } >"$BASE/RESULT.txt"

      echo "SUCCESS $(date '+%F %T') AD=$AD IP=$MIP ID=$IID" >"$FLAG"
      notify "🎉 Oracle A1 抢到了！" \
        "可用域：$AD<br>公网 IP：<b>$MIP</b><br>实例 ID：$IID<br>规格：$MODEL ${OCPUS} OCPU / ${MEMGB} GB<br>登录：ssh -i <你的私钥> ${USER_NAME:-opc}@$MIP"
      log "===== 结束 ====="
      exit 0
    fi

    # ================== 失败 ==================
    # 只取 JSON 里的 message 字段，失败就退回截断的原文
    msg=$(printf '%s' "$out" | grep -o '"message": *"[^"]*"' | head -1 | sed 's/.*: *"//; s/"$//')
    [ -z "$msg" ] && msg=$(printf '%s' "$out" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-160)

    if [ "$msg" = "Out of host capacity." ]; then
      # 正常情况：容量没放出来，压缩成一个短标记
      summary="$summary ${AD##*:}=无容量"
    elif printf '%s' "$msg" | grep -qiE "TooManyRequests|Too many requests"; then
      # 被限流：本轮结束后多等一会儿再打，别硬顶着继续。
      # 注意 OCI 有两种措辞，必须都认：
      #   "TooManyRequests"              —— 错误码形式（429）
      #   "Too many requests for the user" —— 用户级限流，带空格、仅首字母大写
      # 2026-10-09 实测：只匹配前者会漏判后者，被当成普通异常、退避不生效。
      summary="$summary ${AD##*:}=限流"
      extra=300
      log "⚠️ 触发限流（$msg），本轮结束后额外等待 $((extra / 60)) 分钟"
    elif printf '%s' "$msg" | grep -q "LimitExceeded"; then
      # 服务上限：再重试也不会成功，停下来通知人来看
      log "⚠️ 已达服务上限 LimitExceeded：$msg"
      log "   继续轮询不会有结果，已自动停止。请检查免费额度是否已用满。"
      notify "⚠️ OCI 抢机已停止" "遇到 LimitExceeded（服务上限），轮询已自动停止。<br>原因：$msg"
      exit 1
    else
      # 非预期错误，完整记一次，方便排查
      summary="$summary ${AD##*:}=异常"
      if [ "$msg" != "$last_msg" ]; then
        log "AD=$AD 出现非容量错误：$msg"
        last_msg="$msg"
      fi
    fi
  done

  # 休眠时长 = INTERVAL±JITTER 的均匀随机，另有 REST_PCT% 概率叠加一次长休。
  # 为什么要有长休：均匀抖动会被一天上百轮的累加平均掉，日总量永远卡在同一个
  # 数字上（实测连续 11 天都是 402~405 次）；只有低频、大幅度的抖动才能真正
  # 拉开日间差异，也更像「人在盯，但要吃饭睡觉」。
  nap=$((INTERVAL - JITTER + RANDOM % (JITTER * 2 + 1) + extra))
  rest_tag=""
  if [ "$extra" -eq 0 ] && [ "${REST_PCT:-0}" -gt 0 ] \
     && [ $((RANDOM % 100)) -lt "${REST_PCT}" ]; then
    nap=$((nap + REST_MIN + RANDOM % (REST_MAX - REST_MIN + 1)))
    rest_tag="（含长休）"
  fi
  [ "$nap" -lt 60 ] && nap=60
  log "第 $round 轮 →$summary ｜ 耗时 $(( $(date +%s) - t_round ))s，休眠 ${nap}s${rest_tag}"

  sleep "$nap"
done
