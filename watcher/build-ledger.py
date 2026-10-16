#!/usr/bin/env python3
"""
build-ledger.py —— 把巡检 JSON 转成人类可读的《任务台账》

输入：status.json（watcher-serve.py 定期刷新）
输出：任务台账.md（异常置顶）
用法：python3 build-ledger.py [输出路径]
"""
import json, os, datetime, sys

# ══════════ 配置区 ══════════
SRC = "/var/lib/watcher/status.json"
OUT = "/var/lib/watcher/任务台账.md"
CST = datetime.timezone(datetime.timedelta(hours=8))
# ════════════════════════════

def md_table(headers, rows):
    if not rows:
        return "_（无）_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out) + "\n"

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else OUT
    if not os.path.exists(SRC):
        print(f"❌ 找不到 {SRC}", file=sys.stderr); sys.exit(1)
    d = json.load(open(SRC, encoding="utf-8"))
    now = datetime.datetime.now(CST)

    L, A = [], None
    def A(x): L.append(x)

    A("# 📋 任务台账")
    A("")
    A(f"> **数据时间**：{d.get('checked_at','?')} ｜ **台账生成**：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"> **主机**：{d.get('host','?')} ｜ **巡检项**：{d.get('total_checked',0)} ｜ "
      f"**健康**：{'✅ 全部正常' if d.get('healthy') else '❌ 有异常'}")
    A("")
    A("---")
    A("")
    A("## 🚨 当前异常")
    A("")
    probs = d.get("problems") or []
    if probs:
        A(md_table(["类型", "名称", "范围"], [[p.get("kind"), p.get("name"), p.get("scope")] for p in probs]))
    else:
        A("**无异常** ✅")
    A("")
    A("## 🔗 关键链路")
    A("")
    A(md_table(["链路", "状态", "详情"],
               [[l.get("name"), "✅" if l.get("ok") else "❌", l.get("detail")] for l in (d.get("links") or [])]))
    A("")
    A("## ⚙️ 常驻服务")
    A("")
    A(md_table(["服务", "级别", "状态"],
               [[s.get("name"), s.get("scope"),
                 ("✅ " + str(s.get("state"))) if s.get("ok") else ("❌ " + str(s.get("state")))]
                for s in (d.get("services") or [])]))
    A("")
    act = d.get("active_hermes_jobs") or []
    A(f"## ⏰ 定时任务 · 活跃（{len(act)} 个）")
    A("")
    A(md_table(["", "任务", "来源", "周期", "上次跑", "结果"],
               [["✅" if j.get("ok") else "❌", j.get("name"), j.get("profile"), j.get("schedule"),
                 (f"{j.get('age_h')}h 前" if j.get("age_h") is not None else "—"),
                 j.get("last_status") or "—"] for j in act]))
    A("")
    pau = d.get("paused_hermes_jobs") or []
    A(f"## ⏸ 暂停中的任务（{len(pau)} 个 · 不报警）")
    A("")
    A(md_table(["任务", "来源", "周期"],
               [[j.get("name"), j.get("profile"), j.get("schedule")] for j in pau]))
    A("")
    A("---")
    A("")
    A("## 🔧 自动处置规则")
    A("")
    A(md_table(["问题类型", "谁动手", "怎么修"], [
        ["常驻服务 inactive", "自动", "重启（白名单内）"],
        ["抓包/代理服务挂了", "自动", "重启"],
        ["备份失败", "人工", "读日志定位 → 重跑（不自动，怕越修越坏）"],
        ["定时任务逾期", "人工", "查日志找原因"],
        ["改配置/凭据/密码/重启整机", "**必须问人**", "⚠️ 绝不自动"],
    ]))
    A("")
    A("## 📐 判定口径")
    A("")
    A("- **定时任务**：超过「自身周期 × 2.5」没跑 → 逾期；退出码 error → 异常")
    A("- **备份链**：日志超 30h 没更新 → 挂；「失败 N 条」> 2 → 异常")
    A("- **抓包类**：文件 6 秒内没变大 → 断了")
    A("- ⚠️ **已知软肋**：依赖日志文件时间，任务不写日志会误判")
    A("")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".tmp"
    open(tmp, "w", encoding="utf-8").write("\n".join(L))
    os.replace(tmp, out)
    print(f"✅ 台账已生成：{out}")

if __name__ == "__main__":
    main()
