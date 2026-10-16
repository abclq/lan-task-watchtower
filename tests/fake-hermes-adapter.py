#!/usr/bin/env python3
"""测试用假 hermes 适配器：按 probes.json 里 hermes_jobs.source_cmd 约定，向 stdout 输出 JSON 数组。

真实环境的适配器契约（每项）：
  name:str  profile:str  schedule:str  period_min:number  last_run_epoch:number|null
  last_status:str|null   enabled:bool（缺省 true）
模式（第一个参数）：ok（默认，全部健康）/ mixed（各种异常）/ broken（输出坏 JSON）/ fail（退出码 3）
"""
import json, sys, time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
now = time.time()
if mode == "broken":
    print("{not json"); sys.exit(0)
if mode == "fail":
    print("hermes: 连不上状态库", file=sys.stderr); sys.exit(3)

jobs = [{"name": "日报", "profile": "default", "schedule": "0 9 * * *", "period_min": 1440,
         "last_run_epoch": now - 3600 * 5, "last_status": "ok", "enabled": True}]
if mode == "mixed":
    jobs += [
        {"name": "逾期任务", "profile": "default", "schedule": "*/30 * * * *", "period_min": 30,
         "last_run_epoch": now - 60 * 80, "last_status": "ok"},                      # 80 > 30×2.5
        {"name": "报错任务", "profile": "ops", "schedule": "0 * * * *", "period_min": 60,
         "last_run_epoch": now - 600, "last_status": "error"},
        {"name": "没跑过", "profile": "ops", "schedule": "0 3 * * *", "period_min": 1440,
         "last_run_epoch": None, "last_status": None},
        {"name": "缺周期", "profile": "ops", "schedule": "0 3 * * 1-5",
         "last_run_epoch": now - 600, "last_status": "ok"},
        {"name": "暂停的", "profile": "default", "schedule": "0 10 * * 1", "period_min": 10080,
         "last_run_epoch": now - 86400 * 30, "last_status": "error", "enabled": False},
    ]
print(json.dumps(jobs, ensure_ascii=False))
