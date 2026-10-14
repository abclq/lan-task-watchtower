#!/usr/bin/env python3
"""
autofix.py —— 巡检自愈

原则：
  1. 只动已经死的，绝不动活的
  2. 只做「重启」这类可逆操作
  3. 改配置 / 改凭据 / 改密码 / 重启整机 —— 一律不碰，只报告
  4. 没干活就零输出（配 cron 用，静默）

输出：只有真修了东西、或需要人工，才打印（供 cron 推送）
日志：/var/log/watcher/autofix.log
"""
import json, os, subprocess, datetime, sys

# ══════════ 配置区 ══════════
STATUS = "/var/lib/watcher/status.json"
LOG    = "/var/log/watcher/autofix.log"

# 白名单：哪些服务死了可以自动重启（scope: user / system）
WHITELIST = {
    "my-app":       "system",
    "capture-svc": "system",
    "embed-proxy":  "user",
}
# ════════════════════════════

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 999, str(e)

def is_active(name, scope):
    cmd = f"systemctl --user is-active {name}" if scope == "user" else f"systemctl is-active {name}"
    return run(cmd, 20)[1].strip() == "active"

def restart(name, scope):
    cmd = f"systemctl --user restart {name}" if scope == "user" else f"sudo -n systemctl restart {name}"
    rc, out = run(cmd, 60)
    return rc == 0, out.strip()

def main():
    if not os.path.exists(STATUS):
        return
    try:
        d = json.load(open(STATUS, encoding="utf-8"))
    except Exception:
        return

    actions, skipped = [], []

    for s in d.get("services", []):
        if s.get("ok"):
            continue
        name = s.get("name")
        if name not in WHITELIST:
            skipped.append(f"服务 {name} 不在白名单（需人工判断）")
            continue
        scope = WHITELIST[name]
        if is_active(name, scope):     # 快照可能是几分钟前的，先确认
            continue
        ok, out = restart(name, scope)
        if ok:
            actions.append(f"重启服务 {name}（{scope}）")
            log(f"FIX restart {name} scope={scope} OK")
        else:
            skipped.append(f"服务 {name} 重启失败：{out[:120]}")
            log(f"FIX restart {name} FAILED {out[:200]}")

    for p in d.get("problems", []):
        if p.get("kind") in ("hermes-cron", "crontab"):
            skipped.append(f"{p.get('name')} 逾期/报错（需人工判断）")

    if actions or skipped:
        out = []
        if actions:
            out.append("🔧 已自动处理：")
            out += [f"  • {a}" for a in actions]
        if skipped:
            out.append("⚠️ 需要人工（未自动动）：")
            out += [f"  • {s}" for s in skipped[:8]]
        print("\n".join(out))

if __name__ == "__main__":
    main()
