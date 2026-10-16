#!/usr/bin/env python3
"""
autofix.py —— 巡检自愈

原则：
  1. 只动已经死的，绝不动活的
  2. 只做「重启」这类可逆操作
  3. 改配置 / 改凭据 / 改密码 / 重启整机 —— 一律不碰，只报告
  4. 没干活就零输出（配 cron 用，静默）
  5. 只认白名单：没登记的服务死了只报告，不自动重启
  6. 幽灵项（单元不存在）、被主动停用的（disabled/masked）不碰，也不吵

输出：只有真修了东西、或需要人工，才打印（供 cron 推送）
日志：/var/log/watcher/autofix.log
用法：python3 autofix.py [--dry-run]    # --dry-run：只打印打算做什么，不复检、不重启、不写日志
退出码：0 正常 / 2 status.json 不符合 schema（不动手）
"""
import json, os, subprocess, datetime, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema

# ══════════ 配置区（路径也可用环境变量 WATCHER_STATUS / WATCHER_AUTOFIX_LOG 覆盖）══════════
STATUS = os.environ.get("WATCHER_STATUS", "/var/lib/watcher/status.json")
LOG    = os.environ.get("WATCHER_AUTOFIX_LOG", "/var/log/watcher/autofix.log")

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
    """cmd 是参数列表（不经 shell）"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        print(f"autofix: 执行失败 {cmd!r}: {e}", file=sys.stderr)
        return 999, str(e)

def is_active(name, scope):
    cmd = ["systemctl", "--user", "is-active", name] if scope == "user" else ["systemctl", "is-active", name]
    return run(cmd, 20)[1].strip() == "active"

def restart(name, scope):
    cmd = ["systemctl", "--user", "restart", name] if scope == "user" else ["sudo", "-n", "systemctl", "restart", name]
    rc, out = run(cmd, 60)
    return rc == 0, out.strip()

# 只报告、不自动处理的问题类型（服务类在 services[] 里单独处理）
REPORT_ONLY = {
    "hermes-cron": "定时任务", "crontab": "crontab 任务", "backup": "备份",
    "capture": "抓包链路", "proxy": "代理链路", "dns": "DNS", "probe-error": "巡检探测出错",
}

def main():
    dry = "--dry-run" in sys.argv[1:]
    if not os.path.exists(STATUS):
        return
    try:
        with open(STATUS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print(f"autofix: 读不了 {STATUS}: {e}", file=sys.stderr)
        return

    errs = schema.validate(d)
    if errs:
        print(f"autofix: {STATUS} 不符合 schema v{schema.SCHEMA_VERSION}，本轮不动手：" + "；".join(errs[:5]),
              file=sys.stderr)
        sys.exit(2)
    age = schema.age_s(d)
    if schema.is_stale(d):
        print(f"⚠️ status.json 已 {int(age // 60)} 分钟没更新（巡检可能没在跑），本轮不自动处理")
        return

    actions, skipped = [], []
    do_log = (lambda m: None) if dry else log

    for s in schema.items(d, "services"):
        if s.get("ok"):
            continue
        name = s.get("name")
        if s.get("ghost"):
            do_log(f"SKIP {name}: 单元不存在（幽灵项）")
            continue
        if s.get("enabled") in ("disabled", "masked"):
            do_log(f"SKIP {name}: 已被主动停用（{s.get('enabled')}）")
            continue
        if name not in WHITELIST:
            skipped.append(f"服务 {name} 不在白名单（需人工判断）")
            continue
        scope = WHITELIST[name]
        if dry:
            actions.append(f"[dry-run] 将复检并重启服务 {name}（{scope}）")
            continue
        if is_active(name, scope):     # 快照可能是几分钟前的，先确认
            continue
        ok, out = restart(name, scope)
        if ok:
            actions.append(f"重启服务 {name}（{scope}）")
            log(f"FIX restart {name} scope={scope} OK")
        else:
            skipped.append(f"服务 {name} 重启失败：{out[:120]}")
            log(f"FIX restart {name} FAILED {out[:200]}")

    for p in schema.items(d, "problems"):
        label = REPORT_ONLY.get(p.get("kind"))
        if label:
            skipped.append(f"{label} {p.get('name')}：{p.get('detail') or '异常'}（需人工判断）")

    if actions or skipped:
        out = []
        if actions:
            out.append("🔧 已自动处理：")
            out += [f"  • {a}" for a in actions]
        if skipped:
            out.append("⚠️ 需要人工（未自动动）：")
            out += [f"  • {s}" for s in skipped[:8]]
            if len(skipped) > 8:
                out.append(f"  • …另有 {len(skipped) - 8} 条，见台账")
        print("\n".join(out))

if __name__ == "__main__":
    main()
