#!/usr/bin/env bash
# check.sh —— 巡检入口（兼容旧的 cron / watcher-serve 路径），实际逻辑在 check.py
# 部署：/opt/watcher/check.sh + crontab "*/5 * * * * /opt/watcher/check.sh"
# 探测项在 /etc/watcher/probes.json 里声明，示例见 deploy/probes.example.json
set -uo pipefail

# ══════════ 配置区（按实际修改；也可用同名环境变量覆盖）══════════
CONFIG="${WATCHER_PROBES:-/etc/watcher/probes.json}"   # 探测配置
OUT="${WATCHER_STATUS:-/var/lib/watcher/status.json}"  # 结果输出路径
PYTHON="${WATCHER_PYTHON:-python3}"                     # 需要 3.10+
# ════════════════════════════════════════════════════════════════

# cron 环境里没有 user bus，补上才能查 systemctl --user 的服务（仍需 linger）
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -S "$XDG_RUNTIME_DIR/bus" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$PYTHON" "$HERE/check.py" --config "$CONFIG" --out "$OUT" "$@"
