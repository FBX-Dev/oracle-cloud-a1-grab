#!/usr/bin/env bash
# ============================================================================
#  Oracle Cloud A1 抢机 —— 一键部署脚本
#
#  做四件事：
#    1. 在低内存小机器上装 oci-cli（走 venv，绝不用 dnf —— 会 OOM）
#    2. 生成 / 校验 ~/.oci/config（自动把没冒号的 fingerprint 补成正确格式）
#    3. 生成 config.sh 并做鉴权自检
#    4. 安装 systemd 常驻服务
#
#  用法：  ./setup.sh
#
#  许可证：MIT
# ============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${A1GRAB_HOME:-$SCRIPT_DIR}"
OCI_DIR="$HOME/.oci"
CONFIG="$OCI_DIR/config"
KEY_FILE="$OCI_DIR/oci_api_key.pem"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. 环境检查
# ---------------------------------------------------------------------------
say "环境检查"
command -v curl >/dev/null 2>&1 || die "缺少 curl"
command -v python3 >/dev/null 2>&1 || die "缺少 python3"

MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
[ "$MEM_MB" -gt 0 ] && ok "物理内存 ${MEM_MB} MB"
if [ "$MEM_MB" -gt 0 ] && [ "$MEM_MB" -lt 1024 ]; then
  warn "内存不足 1GB，本脚本会用 venv+pip 安装（dnf 在这个内存下会被 OOM 杀掉）"
fi

# ---------------------------------------------------------------------------
# 1. 安装 oci-cli
# ---------------------------------------------------------------------------
say "检查 oci-cli"
if command -v oci >/dev/null 2>&1; then
  ok "已安装：$(oci --version 2>/dev/null)"
elif [ -x "$HOME/ocicli/bin/oci" ]; then
  ok "已安装（venv）：$("$HOME/ocicli/bin/oci" --version 2>/dev/null)"
else
  warn "未安装，开始安装到 ~/ocicli"
  warn "注意：这里【故意不用 dnf install python3-pip】——"
  warn "      在 500MB 级别的机器上 dnf 会被 OOM Killer 杀掉（rc=137）。"
  warn "      RHEL/OL 9 的 python3 venv 自带 ensurepip，能直接产出 pip。"

  python3 -m venv "$HOME/ocicli" || die "创建 venv 失败（可能需要 sudo dnf install python3-libs）"
  ok "venv 创建完成"

  "$HOME/ocicli/bin/pip" install --no-cache-dir --upgrade pip -q || die "升级 pip 失败"
  ok "pip 已就绪"

  echo "  正在安装 oci-cli（约 65MB，需要几分钟）..."
  if "$HOME/ocicli/bin/pip" install --no-cache-dir oci-cli -q; then
    ok "oci-cli 安装完成：$("$HOME/ocicli/bin/oci" --version)"
  else
    die "oci-cli 安装失败"
  fi
fi

# 保证后续代码能找到 oci
if command -v oci >/dev/null 2>&1; then
  OCI="$(command -v oci)"
else
  OCI="$HOME/ocicli/bin/oci"
fi

# ---------------------------------------------------------------------------
# 2. 生成 ~/.oci/config
# ---------------------------------------------------------------------------
# 把控制台复制出来的「无冒号指纹」自动补成 OCI 要求的冒号格式。
# 这是第一次配置时最高频的错误：
#   ERROR: The config file at ~/.oci/config is invalid:
#   | fingerprint | malformed |
normalize_fingerprint() {
  local fp
  fp="$(printf '%s' "$1" | tr -d ' \t\r\n')"
  case "$fp" in
    *:*) printf '%s' "$fp" ;;                                   # 已带冒号
    *)
      if printf '%s' "$fp" | grep -qE '^[0-9a-fA-F]{32}$'; then
        printf '%s' "$fp" | sed 's/../&:/g; s/:$//'              # 两位一插冒号
      else
        printf '%s' "$fp"                                          # 交给 OCI 自己报错
      fi
      ;;
  esac
}

say "配置 OCI 凭据"
mkdir -p "$OCI_DIR" && chmod 700 "$OCI_DIR"

if [ -f "$CONFIG" ]; then
  ok "已存在 $CONFIG，跳过写入"
  warn "如需重配，先备份再删除该文件后重跑本脚本"
else
  echo "  在 OCI 控制台准备这些信息（右上角头像 → My profile）："
  echo "    · User OCID      (My profile 页面上)"
  echo "    · Tenancy OCID   (My profile 页面上，或头像菜单里的 Tenancy)"
  echo "    · Region         (如 us-phoenix-1、ap-singapore-1)"
  echo "    · API Key        (My profile → API keys → Add API key → 生成并下载私钥)"
  echo
  printf '  User OCID:    '; read -r USER_OCID
  printf '  Tenancy OCID: '; read -r TENANCY_OCID
  printf '  Region:       '; read -r REGION
  printf '  Fingerprint:  '; read -r FP_RAW
  printf '  私钥文件路径 (刚下载的 .pem): '; read -r PEM_PATH

  [ -n "$USER_OCID" ]    || die "User OCID 不能为空"
  [ -n "$TENANCY_OCID" ] || die "Tenancy OCID 不能为空"
  [ -n "$REGION" ]       || die "Region 不能为空"
  [ -n "$FP_RAW" ]       || die "Fingerprint 不能为空"
  [ -f "$PEM_PATH" ]     || die "找不到私钥文件：$PEM_PATH"

  FP="$(normalize_fingerprint "$FP_RAW")"
  [ "$FP" != "$FP_RAW" ] && ok "指纹已自动补全冒号：$FP"

  cp "$PEM_PATH" "$KEY_FILE" || die "复制私钥失败"
  chmod 600 "$KEY_FILE"

  cat >"$CONFIG" <<EOF
[DEFAULT]
user=$USER_OCID
fingerprint=$FP
tenancy=$TENANCY_OCID
region=$REGION
key_file=$KEY_FILE
EOF
  chmod 600 "$CONFIG"
  ok "已写入 $CONFIG"
fi

# ---------------------------------------------------------------------------
# 3. 鉴权自检
# ---------------------------------------------------------------------------
say "鉴权自检"
if "$OCI" --no-retry iam region-subscription list --output table 2>/dev/null | grep -q READY; then
  ok "凭据有效"
  TENANCY_OCID="$("$OCI" --no-retry iam region-subscription list \
      --query 'data[0]."tenancy-id"' --raw-output 2>/dev/null)"
else
  warn "鉴权失败。常见原因："
  echo "    1. fingerprint 格式不对（应为 16 组两位十六进制，用冒号分隔）"
  echo "    2. 私钥与控制台上那把不匹配"
  echo "    3. API key 还没在控制台生效（等待约 1 分钟）"
  echo "    可手工核对指纹（应与 config 里的 fingerprint 一致）："
  echo "      openssl rsa -pubout -outform DER -in $KEY_FILE | openssl md5 -c"
  die "请修正后重跑本脚本"
fi

# ---------------------------------------------------------------------------
# 4. 生成 config.sh
# ---------------------------------------------------------------------------
say "生成抢机配置"
if [ -f "$BASE/config.sh" ]; then
  ok "已存在 config.sh，保留不动"
else
  if [ -f "$BASE/config.example.sh" ]; then
    cp "$BASE/config.example.sh" "$BASE/config.sh"
  else
    die "找不到 config.example.sh"
  fi

  # 用当前 tenancy 作为默认 compartment
  if [ -n "${TENANCY_OCID:-}" ]; then
    sed -i "s|^COMPARTMENT=.*|COMPARTMENT=\"$TENANCY_OCID\"|" "$BASE/config.sh"
    ok "已把 COMPARTMENT 预填为你的 tenancy"
  fi

  # 只有一个子网就自动填上
  SUBNET_COUNT="$("$OCI" --no-retry network subnet list \
      --compartment-id "$TENANCY_OCID" --query 'length(data)' --raw-output 2>/dev/null)"
  if [ "$SUBNET_COUNT" = "1" ]; then
    SN="$("$OCI" --no-retry network subnet list \
        --compartment-id "$TENANCY_OCID" --query 'data[0].id' --raw-output 2>/dev/null)"
    sed -i "s|^# SUBNET=.*|SUBNET=\"$SN\"|" "$BASE/config.sh"
    ok "已自动填入唯一子网"
  elif [ "${SUBNET_COUNT:-0}" -gt 1 ] 2>/dev/null; then
    warn "检测到 $SUBNET_COUNT 个子网，需要你手工填 SUBNET，可选："
    "$OCI" --no-retry network subnet list --compartment-id "$TENANCY_OCID" \
      --query 'data[].{name:"display-name",id:id}' --output table 2>/dev/null
  fi

  ok "已生成 config.sh，建议打开检查一遍：$BASE/config.sh"
fi

# ---------------------------------------------------------------------------
# 4.5 关掉会拖垮小机器的系统定时任务（2026-09-24 新增，重要）
# ---------------------------------------------------------------------------
# 实测教训：Oracle Linux 默认开着的 dnf-makecache.timer 每约 2 小时跑一次，
# 在 498MB 的小机器上要吃 ~360MB 内存，会把整机打到 OOM、卡死十几分钟才恢复。
# 实测记录：95 次 OOM 全是它，光白等就吃掉 11.5% 的轮询时间（最长一次 71 分钟）。
# 这台机器只需要网络和 oci-cli，这些定时任务纯属拖累。
# 副作用仅「以后跑 dnf install 会慢一点」，完全可逆。
say "关掉对小机器无益的系统定时任务"
if command -v systemctl >/dev/null 2>&1; then
  for U in dnf-makecache.timer \
           pmlogger_check.timer pmlogger_farm_check.timer \
           pmie_check.timer pmie_farm_check.timer \
           pmlogger_daily.timer pmie_daily.timer; do
    if systemctl is-enabled "$U" >/dev/null 2>&1; then
      if sudo systemctl disable --now "$U" >/dev/null 2>&1; then
        ok "已关闭 $U"
      else
        warn "关闭 $U 失败（可忽略）"
      fi
    fi
  done
  echo "    （想恢复：sudo systemctl enable --now dnf-makecache.timer）"
fi

# ---------------------------------------------------------------------------
# 5. 安装 systemd 服务
# ---------------------------------------------------------------------------
say "安装 systemd 服务"
if ! command -v systemctl >/dev/null 2>&1; then
  warn "没有 systemd，跳过。可自行用 nohup 常驻运行："
  echo "    setsid nohup $BASE/grab.sh > $BASE/nohup.log 2>&1 < /dev/null &"
else
  CUR_USER="$(id -un)"
  UNIT_SRC="$BASE/a1grab.service"
  if [ ! -f "$UNIT_SRC" ]; then
    die "找不到 a1grab.service"
  fi

  TMP_UNIT="$(mktemp)"
  sed "s|^User=.*|User=$CUR_USER|" "$UNIT_SRC" >"$TMP_UNIT"

  sudo cp "$TMP_UNIT" /etc/systemd/system/a1grab.service || die "写入 unit 失败（需要 sudo 权限）"
  rm -f "$TMP_UNIT"
  ok "已写入 /etc/systemd/system/a1grab.service（User=$CUR_USER）"

  chmod +x "$BASE/grab.sh" "$BASE/notify.sh" 2>/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable a1grab.service >/dev/null 2>&1
  sudo systemctl restart a1grab.service
  sleep 3
  if systemctl is-active --quiet a1grab.service; then
    ok "服务已启动并设为开机自启"
  else
    warn "服务未能启动，查看日志：sudo journalctl -u a1grab.service -n 30"
  fi
fi

# ---------------------------------------------------------------------------
# 6. 安装每日日报定时器（可选，但很实用）
# ---------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -f "$BASE/report.py" ] \
   && [ -f "$BASE/a1grab-report.service" ] && [ -f "$BASE/a1grab-report.timer" ]; then
  say "安装每日日报定时器"
  CUR_USER="$(id -un)"
  TMP_SVC="$(mktemp)"
  sed "s|^User=.*|User=$CUR_USER|" "$BASE/a1grab-report.service" >"$TMP_SVC"

  if sudo cp "$TMP_SVC" /etc/systemd/system/a1grab-report.service 2>/dev/null \
     && sudo cp "$BASE/a1grab-report.timer" /etc/systemd/system/a1grab-report.timer 2>/dev/null; then
    rm -f "$TMP_SVC"
    # unit 里的路径是 /home/opc/a1grab，换成实际目录
    if [ "$BASE" != "/home/opc/a1grab" ]; then
      sudo sed -i "s|/home/opc/a1grab|$BASE|g; s|^User=.*|User=$CUR_USER|" \
        /etc/systemd/system/a1grab-report.service
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable --now a1grab-report.timer >/dev/null 2>&1
    if systemctl is-active --quiet a1grab-report.timer; then
      ok "日报定时器已启用：每天 08:00 生成昨天的日报"
      echo "    配了 pushplus_token 的话，同一时刻还会推一条微信摘要"
      echo "    立刻生成一份试试： python3 $BASE/report.py --days 2"
      echo "    输出目录：         $BASE/reports/"
    else
      warn "定时器未能启用，手动试：sudo systemctl enable --now a1grab-report.timer"
    fi
  else
    rm -f "$TMP_SVC"
    warn "写入日报 unit 失败，跳过（不影响抢机）"
  fi
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
say "部署完成"
cat <<EOF
  查看进展：   tail -f $BASE/grab.log
  一键状态：   $BASE/status.sh
  手动跑一轮： $BASE/grab.sh
  生成日报：   python3 $BASE/report.py --days 2   → $BASE/reports/index.html

  【下一步：配置抢到后的通知（可选）】
    PushPlus（推到微信，推荐）：
      echo '<你的token>' > $BASE/pushplus_token
      $BASE/notify.sh "测试" "通知链路测试"      # 立刻测一条

    其它 webhook（企业微信机器人 / Bark / Server酱 / 钉钉）：
      echo '<webhook地址>' > $BASE/notify_url

  【抢到之后别忘了】
    1. cat $BASE/RESULT.txt                   # 拿公网 IP
    2. sudo systemctl disable --now a1grab.service
    3. 去 OCI 控制台删掉这把 API Key（它有整个 tenancy 的权限）
EOF
