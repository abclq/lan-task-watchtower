#!/usr/bin/env python3
"""
schema.py —— status.json 的字段约定 + 校验（check / autofix / build-ledger 共用）

只用标准库；改字段时同步改 README「接口返回」一节，并把 SCHEMA_VERSION 加 1。
"""
import time

SCHEMA_VERSION = 1
SERVICE_NAME   = "local-tasks-health"
STALE_SEC      = 15 * 60          # 超过 15 分钟没更新 = 数据失联

PROBLEM_KINDS = ("service", "hermes-cron", "crontab", "backup", "capture", "proxy", "dns", "probe-error")
LINK_TYPES    = ("backup", "capture", "proxy", "dns")
SKIP_REASONS  = ("not_configured", "disabled")       # 另有 "missing_tool:<名字>"

NUM = (int, float)

# 顶层字段：名字 → 类型
TOP = {
    "schema_version":     int,
    "service":            str,
    "host":               str,
    "checked_at":         str,
    "checked_at_epoch":   int,
    "duration_ms":        int,
    "healthy":            bool,
    "exit_code":          int,
    "problem_count":      int,
    "total_checked":      int,
    "summary":            str,
    "problems":           list,
    "links":              list,
    "services":           list,
    "crontab_jobs":       list,
    "active_hermes_jobs": list,
    "paused_hermes_jobs": list,
    "skipped":            list,
    "resources":          dict,
}

# 各列表里每一项的必填字段（可为 null 的字段不在这里强校验）
ITEM = {
    "problems":           {"kind": str, "name": str, "detail": str, "probe": str},
    "links":              {"kind": str, "type": str, "name": str, "ok": bool, "detail": str},
    "services":           {"name": str, "scope": str, "state": str, "ok": bool,
                           "enabled": str, "ghost": bool, "detail": str},
    "crontab_jobs":       {"name": str, "log": str, "period_min": NUM, "ok": bool, "detail": str},
    "active_hermes_jobs": {"name": str, "ok": bool, "detail": str},
    "paused_hermes_jobs": {"name": str},
    "skipped":            {"kind": str, "reason": str},
}


def _is(v, t):
    """isinstance，但 bool 不算 int（JSON 的 true 不能冒充数字）"""
    if isinstance(v, bool):
        return t is bool or (isinstance(t, tuple) and bool in t)
    return isinstance(v, t)


def _tname(t):
    if isinstance(t, tuple):
        return "number"
    return {int: "int", str: "str", bool: "bool", list: "list", dict: "object"}.get(t, str(t))


def validate(d):
    """返回错误清单（空 = 通过）。不抛异常，什么都能喂。"""
    if not isinstance(d, dict):
        return [f"顶层应为 object，实际是 {type(d).__name__}"]
    errs = []
    if d.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version={d.get('schema_version')!r}，期望 {SCHEMA_VERSION}（旧格式？）")
    for k, t in TOP.items():
        if k == "schema_version":
            continue
        if k not in d:
            errs.append(f"缺字段 {k}")
        elif not _is(d[k], t):
            errs.append(f"{k} 类型应为 {_tname(t)}，实际是 {type(d[k]).__name__}")
    for lst, fields in ITEM.items():
        v = d.get(lst)
        if not isinstance(v, list):
            continue
        for i, it in enumerate(v):
            if not isinstance(it, dict):
                errs.append(f"{lst}[{i}] 应为 object")
                continue
            for f, t in fields.items():
                if f not in it:
                    errs.append(f"{lst}[{i}].{f} 缺失")
                elif not _is(it[f], t):
                    errs.append(f"{lst}[{i}].{f} 类型应为 {_tname(t)}")
    if isinstance(d.get("problems"), list) and _is(d.get("problem_count"), int) \
            and d["problem_count"] != len(d["problems"]):
        errs.append(f"problem_count={d['problem_count']} 与 problems 条数 {len(d['problems'])} 不一致")
    if _is(d.get("healthy"), bool) and _is(d.get("exit_code"), int) \
            and d["healthy"] != (d["exit_code"] == 0):
        errs.append("healthy 与 exit_code 不一致")
    return errs


def items(d, key):
    """安全取列表字段：不是 list 返回 []，里面不是 object 的项丢掉。消费方一律用它。"""
    v = d.get(key) if isinstance(d, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def age_s(d, now=None):
    """数据距今多少秒；没有可用的 checked_at_epoch 返回 None"""
    e = d.get("checked_at_epoch") if isinstance(d, dict) else None
    if not _is(e, NUM):
        return None
    return (time.time() if now is None else now) - e


def is_stale(d, now=None, limit=STALE_SEC):
    a = age_s(d, now)
    return a is None or a > limit
