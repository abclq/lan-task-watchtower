#!/usr/bin/env python3
"""
alert-mail-watch.py —— 告警邮件监听（告警闭环的内网侧一半）

公网侧拉接口发现异常 → 发告警邮件 → 本脚本读到 → 立刻再修一次 → 归档留痕。

设计要点：
  1. 搜【全邮箱】含垃圾箱 —— 机器发的邮件很容易被判垃圾邮件，只搜收件箱必漏
  2. 主题标记 + 发件人白名单 双重识别，防冒用
  3. 只做「重启」这类可逆操作（复用 autofix.py 的红线）
  4. 归入标签 + 移出收件箱 —— 邮件收着留痕，但不推送通知（不吵人）
  5. 没有告警时【零输出】，配合 cron 看门狗模式静默

输出：只有真收到告警时才打印（供 cron 推送）；否则什么都不输出
日志：/var/log/watcher/alert-mail.log
"""
import os
import email.utils
import subprocess
import sys
import datetime

# ══════════════════ 配置区（按你的环境改） ══════════════════
WORKDIR = "/opt/watcher"

# 邮件操作命令：这里以 gws（Google Workspace CLI）为例，换成你用的即可
# 需要支持：search / send / label 三个子命令
MAIL_CMD = [f"{WORKDIR}/google_api.py", "gmail"]   # 参数列表，不经 shell

# 主题标记：公网侧发告警邮件时，主题必须含其中之一
SUBJECT_PATTERNS = ["本机告警", "内网告警", "巡检告警"]

# 发件人白名单：只认这些地址发来的告警（精确匹配，不区分大小写；留空 = 不校验）
ALLOWED_SENDERS = ["alerts@example.com"]

# 归档标签名
LABEL_NAME = "本机告警"

# 自愈脚本
AUTOFIX = f"{WORKDIR}/autofix.py"

LOG = "/var/log/watcher/alert-mail.log"

# 代理（按需）
PROXY = os.environ.get("https_proxy", "")
# ═══════════════════════════════════════════════════════════


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def run(cmd, timeout=120):
    """cmd 是参数列表（不经 shell）"""
    env = dict(os.environ)
    if PROXY:
        env.setdefault("https_proxy", PROXY)
        env.setdefault("http_proxy", PROXY)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=WORKDIR)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        print(f"alert-mail-watch: 执行失败 {cmd!r}: {e}", file=sys.stderr)
        return 999, str(e)


def sender_allowed(from_header):
    """解析 From 头里的地址再精确比较，不做子串匹配"""
    _, addr = email.utils.parseaddr(from_header or "")
    addr = addr.strip().lower()
    return bool(addr) and addr in {s.strip().lower() for s in ALLOWED_SENDERS}


def find_alerts():
    """搜全邮箱（含垃圾箱）的未读告警邮件，返回 [(id, subject, sender, snippet)]"""
    import json
    import re

    seen, out = set(), []
    for pat in SUBJECT_PATTERNS:
        rc, res = run(MAIL_CMD + ["search", f"in:anywhere is:unread subject:{pat}", "--max", "5"])
        if rc != 0:
            print(f"alert-mail-watch: 搜索「{pat}」失败 rc={rc}: {res.strip()[:200]}", file=sys.stderr)
            continue
        try:
            msgs = json.loads(res)
        except Exception as e:
            print(f"alert-mail-watch: 搜索「{pat}」返回不是 JSON: {e}", file=sys.stderr)
            continue
        for m in msgs if isinstance(msgs, list) else []:
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append((mid, m.get("subject", ""), m.get("from", ""),
                        (m.get("snippet") or "")[:200]))

    # 发件人白名单（防冒用主题触发内网动作）
    if ALLOWED_SENDERS:
        out = [a for a in out if sender_allowed(a[2])]
    return out


def archive_to_label(mid):
    """归入标签 + 移出收件箱/垃圾箱/未读：收着但不打扰"""
    rc, _ = run(MAIL_CMD + ["modify", mid,
                            "--add-label", LABEL_NAME,
                            "--remove-label", "INBOX", "--remove-label", "UNREAD",
                            "--remove-label", "SPAM"])
    return rc == 0


def main():
    alerts = find_alerts()
    if not alerts:
        return                      # 静默：没告警就什么都不输出

    lines = [f"📬 收到告警邮件 {len(alerts)} 封"]
    for _, subj, sender, snip in alerts:
        lines.append(f"  · 主题：{subj}")
        lines.append(f"    发件：{sender}")
        if snip:
            lines.append(f"    摘要：{snip}")

    # 立刻跑一次自愈：把能修的先修了
    lines.append("")
    lines.append("  ── 立刻跑一次自愈 ──")
    rc, out = run([sys.executable, AUTOFIX], timeout=300)
    fixed = out.strip() or "✅ 当前无需维修（巡检全绿）"
    lines += [f"  {x}" for x in fixed.splitlines()]

    # 归档（收着但不打扰）
    n = sum(1 for mid, _, _, _ in alerts if archive_to_label(mid))
    lines.append("")
    lines.append(f"  ✅ 已归入「{LABEL_NAME}」标签并移出收件箱：{n} 封（不打扰模式）")

    log(f"ALERT {len(alerts)} mail(s), archived={n}, autofix_rc={rc}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
