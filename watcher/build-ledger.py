#!/usr/bin/env python3
"""
build-ledger.py —— 把巡检 JSON 转成人类可读的《任务台账》

输入：status.json（watcher-serve.py 定期刷新）
输出：任务台账.md（异常置顶）
用法：python3 build-ledger.py [输出路径]
数据不符合 schema 时照样出台账（缺的字段显示「—」），顶部写明哪里不对
"""
import json, os, datetime, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema

# ══════════ 配置区（输入路径也可用环境变量 WATCHER_STATUS 覆盖）══════════
SRC = os.environ.get("WATCHER_STATUS", "/var/lib/watcher/status.json")
OUT = "/var/lib/watcher/任务台账.md"
# ════════════════════════════

def md_table(headers, rows):
    if not rows:
        return "_（无）_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("—" if c is None or c == "" else str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out) + "\n"

def fmt_resources(r):
    if not isinstance(r, dict):
        return "—"
    load = r.get("load")
    load = " / ".join(str(x) for x in load) if isinstance(load, list) else "—"
    pct = lambda v: f"{v}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else "—"
    return f"内存 {pct(r.get('mem_pct'))} ｜ 磁盘 {pct(r.get('disk_pct'))} ｜ 负载 {load}"

SKIP_TEXT = {"not_configured": "未配置", "disabled": "已停用（enabled:false）"}

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    if not os.path.exists(SRC):
        print(f"❌ 找不到 {SRC}", file=sys.stderr); sys.exit(1)
    try:
        with open(SRC, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print(f"❌ 读不了 {SRC}: {e}", file=sys.stderr); sys.exit(1)
    if not isinstance(d, dict):
        d = {}
    errs = schema.validate(d)
    age = schema.age_s(d)
    now = datetime.datetime.now().astimezone()

    L, A = [], None
    def A(x): L.append(x)

    stale = f" ⚠️ 数据已过期 {int(age // 60)} 分钟" if age is not None and schema.is_stale(d) else ""
    A("# 📋 任务台账" + stale)
    A("")
    if errs:
        A(f"> ⚠️ **数据格式不对**（期望 schema v{schema.SCHEMA_VERSION}，可能是旧版巡检写的）："
          + "；".join(errs[:5]) + (f" 等 {len(errs)} 处" if len(errs) > 5 else ""))
        A("")
    A(f"> **数据时间**：{d.get('checked_at','—')} ｜ **台账生成**：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    healthy = d.get("healthy")
    A(f"> **主机**：{d.get('host','—')} ｜ **巡检项**：{d.get('total_checked','—')} ｜ "
      f"**健康**：{'✅ 全部正常' if healthy is True else '❌ 有异常' if healthy is False else '— 未知'}")
    A(f"> **结论**：{d.get('summary') or '—'}")
    A(f"> **资源**：{fmt_resources(d.get('resources'))}")
    A("")
    A("---")
    A("")
    A("## 🚨 当前异常")
    A("")
    probs = schema.items(d, "problems")
    if probs:
        A(md_table(["类型", "名称", "范围", "详情"],
                   [[p.get("kind"), p.get("name"), p.get("scope"), p.get("detail")] for p in probs]))
    elif "problems" not in d:
        A("_（数据里没有 problems 字段）_")
    else:
        A("**无异常** ✅")
    A("")
    A("## 🔗 关键链路")
    A("")
    A(md_table(["链路", "状态", "详情"],
               [[l.get("name"), "✅" if l.get("ok") else "❌", l.get("detail")] for l in schema.items(d, "links")]))
    A("")
    A("## ⚙️ 常驻服务")
    A("")
    A(md_table(["服务", "级别", "状态", "开机自启"],
               [[s.get("name"), s.get("scope"),
                 ("✅ " + str(s.get("state"))) if s.get("ok")
                 else ("👻 单元不存在" if s.get("ghost") else "❌ " + str(s.get("state"))),
                 s.get("enabled")]
                for s in schema.items(d, "services")]))
    A("")
    cron = schema.items(d, "crontab_jobs")
    A(f"## 📜 crontab 任务（{len(cron)} 个）")
    A("")
    A(md_table(["", "任务", "周期", "日志", "详情"],
               [["✅" if j.get("ok") else "❌", j.get("name"),
                 (f"{j.get('period_min')}min" if j.get("period_min") is not None else None),
                 j.get("log"), j.get("detail")] for j in cron]))
    A("")
    act = schema.items(d, "active_hermes_jobs")
    A(f"## ⏰ 定时任务 · 活跃（{len(act)} 个）")
    A("")
    A(md_table(["", "任务", "来源", "周期", "上次跑", "结果"],
               [["✅" if j.get("ok") else "❌", j.get("name"), j.get("profile"), j.get("schedule"),
                 (f"{j.get('age_h')}h 前" if j.get("age_h") is not None else "—"),
                 j.get("last_status") or "—"] for j in act]))
    bad = [j for j in act if not j.get("ok")]
    if bad:
        A("")
        A("异常说明：" + "；".join(f"**{j.get('name')}** {j.get('detail') or ''}" for j in bad))
    A("")
    pau = schema.items(d, "paused_hermes_jobs")
    A(f"## ⏸ 暂停中的任务（{len(pau)} 个 · 不报警）")
    A("")
    A(md_table(["任务", "来源", "周期"],
               [[j.get("name"), j.get("profile"), j.get("schedule")] for j in pau]))
    A("")
    sk = schema.items(d, "skipped")
    A(f"## ⏭ 未执行的探测（{len(sk)} 项）")
    A("")
    A(md_table(["类型", "名称", "原因"],
               [[x.get("kind"), x.get("name") or "（整类）",
                 SKIP_TEXT.get(x.get("reason"), x.get("reason"))] for x in sk]))
    A("")
    A("---")
    A("")
    A("## 🔧 自动处置规则")
    A("")
    A(md_table(["问题类型", "谁动手", "怎么修"], [
        ["常驻服务 inactive", "自动", "重启（仅白名单内；动手前复检）"],
        ["服务不在白名单", "人工", "只报告"],
        ["单元不存在（幽灵项）/ 被主动停用", "不动", "跳过，不重启也不推送"],
        ["抓包/代理链路断了", "自动（间接）", "其服务在白名单内则重启服务；链路本身只报告"],
        ["DNS 解析失败 / 探测出错", "人工", "只报告"],
        ["备份失败", "人工", "读日志定位 → 重跑（不自动，怕越修越坏）"],
        ["定时任务逾期", "人工", "查日志找原因"],
        ["改配置/凭据/密码/重启整机", "**必须问人**", "⚠️ 绝不自动"],
    ]))
    A("")
    A("## 📐 判定口径")
    A("")
    A("- **定时任务**：超过「自身周期 × 2.5」没跑 → 逾期；上次结果 error → 异常")
    A("- **crontab**：日志文件修改时间超过「周期 × 2.5」 → 逾期")
    A("- **备份链**：日志超 30h 没更新 → 挂；「失败 N 条」> 2 → 异常")
    A("- **抓包类**：文件 6 秒内没变大 → 断了")
    A("- **代理类**：HTTP 401 或 200 → 活")
    A("- **DNS**：解析不出来 → 坏")
    A("- **常驻服务**：systemd ActiveState 不是 active → 挂")
    A("- 以上阈值都可在 probes.json 里按项覆盖；没配置的探测列在「未执行的探测」")
    A("- ⚠️ **已知软肋**：依赖日志文件时间，任务不写日志会误判")
    A("")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".tmp"
    open(tmp, "w", encoding="utf-8").write("\n".join(L))
    os.replace(tmp, out)
    print(f"✅ 台账已生成：{out}")

if __name__ == "__main__":
    main()
