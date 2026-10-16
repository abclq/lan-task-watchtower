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

# 用 json.dumps 生成，值里有引号/反斜杠也不会坏；先写 .tmp 再 mv，读方不会读到半截文件
TMP="${OUT}.tmp.$$"
python3 - "$TMP" \
  "$(hostname)" \
  "$(date '+%F %T')" \
  "$STATUS" \
  "$ALERT" \
  "$(free -m | awk '/Mem:/ {printf "%.0f%%", $3/$2*100}')" \
  "$(df -h / | awk 'NR==2 {print $5}')" \
  "$(cut -d' ' -f1-3 /proc/loadavg)" <<'PY' || { rm -f "$TMP"; exit 1; }
import json, sys
path, host, time, status, alert, mem, disk, load = sys.argv[1:9]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"host": host, "time": time, "status": status, "alert": alert,
               "mem": mem, "disk": disk, "load": load}, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
mv -f "$TMP" "$OUT"
