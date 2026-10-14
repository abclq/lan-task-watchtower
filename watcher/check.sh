#!/usr/bin/env bash
# check.sh —— 内网巡检：查进程在不在、记资源指标、写 status.json
# 部署：/opt/watcher/check.sh + crontab "*/5 * * * * /opt/watcher/check.sh"
set -uo pipefail

# ══════════ 配置区（按实际修改）══════════
WATCH="nginx redis-server myapp"     # 待监控进程名，空格分隔
OUT="/var/lib/watcher/status.json"   # 结果输出路径
# ════════════════════════════════════════

mkdir -p "$(dirname "$OUT")"

ALERT=""
for p in $WATCH; do
  pgrep -x "$p" >/dev/null 2>&1 || ALERT="${ALERT}${p} 缺失; "
done
STATUS=$([ -n "$ALERT" ] && echo "ALERT" || echo "OK")

cat > "$OUT" <<EOF
{
  "host": "$(hostname)",
  "time": "$(date '+%F %T')",
  "status": "${STATUS}",
  "alert": "${ALERT}",
  "mem": "$(free -m | awk '/Mem:/ {printf "%.0f%%", $3/$2*100}')",
  "disk": "$(df -h / | awk 'NR==2 {print $5}')",
  "load": "$(cut -d' ' -f1-3 /proc/loadavg)"
}
EOF
