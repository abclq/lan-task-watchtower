#!/usr/bin/env python3
"""
check.py —— 内网巡检（声明式探测）：读 probes.json → 并发跑各探测 → 原子写 status.json

  · 探测项全部来自配置（/etc/watcher/probes.json），代码里不写死任何环境路径
  · 某种探测一项都没配 → skipped[] 里写 not_configured；enabled:false → disabled
  · 单项探测出错/超时 → problems[] 里一条 probe-error，不影响其它项
  · 配置本身有错（文件缺失、JSON 坏、未知 kind、缺必填参数）→ exit_code=2，status.json 照写

用法：python3 check.py [--config PATH] [--out PATH|-]
退出码：0 健康 / 1 有问题 / 2 配置或框架错误（与 status.json 的 exit_code 一致）
要求：Python 3.10+，只用标准库
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("❌ check.py 需要 Python 3.10+")

import argparse, concurrent.futures as cf, datetime, glob, json, os, re, shutil, socket, \
    subprocess, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema

# ══════════ 配置区 ══════════
CONFIG         = "/etc/watcher/probes.json"
OUT            = "/var/lib/watcher/status.json"
ROUND_BUDGET_S = 110      # 整轮硬上限；watcher-serve 调 check 的 timeout 是 120
DEFAULTS       = {"timeout_s": 10, "grace": 2.5}
# ════════════════════════════


class ConfigError(Exception):
    """配置写错了（归 exit_code=2）"""


class MissingTool(Exception):
    """探测依赖的命令没装（记 skipped + probe-error）"""
    def __init__(self, tool):
        super().__init__(f"缺少命令 {tool}")
        self.tool = tool


# ───────────── 小工具 ─────────────

def run(cmd, timeout):
    """cmd 是参数列表（不经 shell）；超时/找不到命令都抛异常，由框架记 probe-error"""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout or "", r.stderr or ""


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def need(p, key, typ, positive=False):
    v = p.get(key)
    ok = is_num(v) if typ == "num" else isinstance(v, typ)
    if not ok or (positive and v <= 0):
        want = {"num": "正数" if positive else "数字", str: "字符串", list: "数组"}.get(typ, str(typ))
        raise ConfigError(f"参数 {key} 应为{want}，实际是 {v!r}")
    return v


def opt(p, key, default, typ):
    if key not in p:
        return default
    return need(p, key, typ, positive=(typ == "num"))


# ───────────── 各 kind 探测 ─────────────
# 每个函数返回 {桶名: [条目, ...]}；条目里 ok=False 的由框架统一转成 problems

def probe_service(p, ctx):
    unit  = need(p, "name", str)
    scope = p.get("scope", "system")
    if scope not in ("system", "user"):
        raise ConfigError(f"scope 只能是 system/user，实际是 {scope!r}")
    if shutil.which("systemctl") is None:
        raise MissingTool("systemctl")
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + \
          ["show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState"]
    rc, out, err = run(cmd, ctx["timeout"])
    if rc != 0:
        raise RuntimeError(f"systemctl show 退出码 {rc}: {err.strip()[:200]}")
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    state   = props.get("ActiveState", "") or "unknown"
    enabled = props.get("UnitFileState", "")
    ghost   = props.get("LoadState") == "not-found"
    detail  = state + (" · 单元不存在（幽灵项）" if ghost else "") + \
              (f" · {enabled}" if enabled in ("disabled", "masked") else "")
    return {"services": [{"name": unit, "scope": scope, "state": state, "ok": state == "active",
                          "enabled": enabled, "ghost": ghost, "detail": detail}]}


def probe_hermes_jobs(p, ctx):
    cmd = need(p, "source_cmd", list)
    if not cmd or not all(isinstance(x, str) for x in cmd):
        raise ConfigError("source_cmd 应为非空字符串数组（不经 shell）")
    grace = opt(p, "grace", ctx["grace"], "num")
    bad   = [s.lower() for s in opt(p, "error_statuses", ["error", "failed"], list)]
    rc, out, err = run(cmd, ctx["timeout"])
    if rc != 0:
        raise RuntimeError(f"适配器退出码 {rc}: {err.strip()[:200]}")
    try:
        jobs = json.loads(out)
    except ValueError as e:
        raise RuntimeError(f"适配器输出不是 JSON: {e}")
    if not isinstance(jobs, list):
        raise RuntimeError("适配器输出应为 JSON 数组")

    now, active, paused = ctx["now"], [], []
    for i, j in enumerate(jobs):
        if not isinstance(j, dict) or not isinstance(j.get("name"), str):
            raise RuntimeError(f"适配器第 {i} 项缺 name")
        period = j.get("period_min")
        last   = j.get("last_run_epoch")
        status = j.get("last_status")
        age_h  = round((now - last) / 3600, 1) if is_num(last) else None
        item = {"name": j["name"], "profile": str(j.get("profile") or ""),
                "schedule": str(j.get("schedule") or ""),
                "period_min": period if is_num(period) else None,
                "age_h": age_h, "last_status": status if isinstance(status, str) else None}
        if j.get("enabled", True) is False:
            paused.append({**item, "ok": True, "detail": "已暂停"})
            continue
        why = []
        if not is_num(period) or period <= 0:
            why.append("缺 period_min，无法判逾期（请在适配器里补）")
        if age_h is None:
            why.append("从没跑过（无 last_run_epoch）")
        elif is_num(period) and period > 0 and (now - last) > period * 60 * grace:
            why.append(f"逾期：{age_h}h 前跑过（周期 {period}min × {grace}）")
        if isinstance(status, str) and status.lower() in bad:
            why.append(f"上次结果 {status}")
        ok = not why
        detail = "；".join(why) if why else f"{age_h}h 前跑过"
        active.append({**item, "ok": ok, "detail": detail})
    return {"active_hermes_jobs": active, "paused_hermes_jobs": paused}


def probe_crontab_log(p, ctx):
    name   = need(p, "name", str)
    log    = need(p, "log", str)
    period = need(p, "period_min", "num", positive=True)
    grace  = opt(p, "grace", ctx["grace"], "num")
    item = {"name": name, "log": log, "period_min": period, "age_min": None}
    try:
        age_min = round((ctx["now"] - os.stat(log).st_mtime) / 60, 1)
    except FileNotFoundError:
        return {"crontab_jobs": [{**item, "ok": False, "detail": "日志文件不存在（任务没写过日志？）"}]}
    ok = age_min <= period * grace
    detail = f"{age_min}min 前更新" if ok else f"逾期：{age_min}min 没更新（周期 {period}min × {grace}）"
    return {"crontab_jobs": [{**item, "age_min": age_min, "ok": ok, "detail": detail}]}


def probe_backup_log(p, ctx):
    name     = need(p, "name", str)
    log      = need(p, "log", str)
    max_age  = opt(p, "max_age_h", 30, "num")
    regex    = opt(p, "fail_regex", r"失败\s*(\d+)\s*条", str)
    max_fail = opt(p, "max_fail", 2, "num")
    tail     = int(opt(p, "tail_bytes", 65536, "num"))
    try:
        pat = re.compile(regex)
    except re.error as e:
        raise ConfigError(f"fail_regex 不是合法正则: {e}")
    if pat.groups < 1:
        raise ConfigError("fail_regex 需要一个捕获组来取失败条数")
    link = {"kind": "link", "type": "backup", "name": name}
    try:
        st = os.stat(log)
    except FileNotFoundError:
        return {"links": [{**link, "ok": False, "detail": "日志文件不存在"}]}
    age_h = round((ctx["now"] - st.st_mtime) / 3600, 1)
    with open(log, "rb") as f:
        f.seek(max(0, st.st_size - tail))
        text = f.read().decode("utf-8", errors="replace")
    hits = pat.findall(text)
    fails = int(hits[-1]) if hits and str(hits[-1]).isdigit() else None

    why = []
    if age_h > max_age:
        why.append(f"{age_h}h 没更新（上限 {max_age}h）")
    if fails is not None and fails > max_fail:
        why.append(f"失败 {fails} 条（上限 {max_fail}）")
    if why:
        return {"links": [{**link, "ok": False, "detail": "；".join(why)}]}
    tail_note = f"失败 {fails} 条" if fails is not None else "日志里没找到失败条数"
    return {"links": [{**link, "ok": True, "detail": f"{age_h}h 前跑过，{tail_note}"}]}


def probe_capture_file(p, ctx):
    name   = need(p, "name", str)
    pat    = need(p, "glob", str)
    window = opt(p, "window_s", 6, "num")

    def newest():
        best = None
        for f in glob.glob(pat):
            try:
                st = os.stat(f)
            except FileNotFoundError:      # 刚好被轮转删掉
                continue
            if best is None or st.st_mtime > best[2]:
                best = (f, st.st_size, st.st_mtime)
        return best

    link = {"kind": "link", "type": "capture", "name": name}
    a = newest()
    if a is None:
        return {"links": [{**link, "ok": False, "detail": f"没有匹配 {pat} 的文件"}]}
    time.sleep(window)
    b = newest()
    if b and b[0] != a[0]:
        return {"links": [{**link, "ok": True, "detail": f"{window}s 内轮转到新文件 {os.path.basename(b[0])}"}]}
    if b and b[1] > a[1]:
        return {"links": [{**link, "ok": True, "detail": f"{window}s 内增长 {b[1] - a[1]} 字节"}]}
    return {"links": [{**link, "ok": False, "detail": f"{window}s 内没变大（{os.path.basename(a[0])}）"}]}


def probe_http_proxy(p, ctx):
    name  = need(p, "name", str)
    url   = need(p, "url", str)
    codes = opt(p, "ok_codes", [200, 401], list)
    if not url.startswith(("http://", "https://")):
        raise ConfigError("url 只支持 http:// 或 https://")
    link = {"kind": "link", "type": "proxy", "name": name}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 不走环境变量里的代理
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=ctx["timeout"]) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return {"links": [{**link, "ok": False, "detail": f"连不上：{reason}"}]}
    return {"links": [{**link, "ok": code in codes, "detail": f"HTTP {code}"}]}


def probe_dns(p, ctx):
    name   = need(p, "name", str)
    query  = need(p, "query", str)
    server = p.get("server")
    link = {"kind": "link", "type": "dns", "name": name}
    if server is not None:
        if not isinstance(server, str) or not server:
            raise ConfigError("server 应为字符串")
        if shutil.which("dig") is None:
            raise MissingTool("dig")       # 配了 server 就必须真问它，不偷偷换成系统解析器
        rc, out, err = run(["dig", "+short", "+time=3", "+tries=1", f"@{server}", query], ctx["timeout"])
        answers = [l for l in out.splitlines() if l.strip() and not l.startswith(";")]
        if rc != 0 or not answers:
            why = (out.strip() or err.strip() or f"dig 退出码 {rc}")[:120]
            return {"links": [{**link, "ok": False, "detail": f"@{server} 解析 {query} 失败：{why}"}]}
        return {"links": [{**link, "ok": True, "detail": f"@{server} {query} → {answers[0]}"}]}
    try:
        infos = socket.getaddrinfo(query, None)
    except OSError as e:
        return {"links": [{**link, "ok": False, "detail": f"系统解析器解析 {query} 失败：{e}"}]}
    return {"links": [{**link, "ok": True, "detail": f"{query} → {infos[0][4][0]}（系统解析器）"}]}


KINDS = {
    "service":      probe_service,
    "hermes_jobs":  probe_hermes_jobs,
    "crontab_log":  probe_crontab_log,
    "backup_log":   probe_backup_log,
    "capture_file": probe_capture_file,
    "http_proxy":   probe_http_proxy,
    "dns":          probe_dns,
}

# 桶 → problems 里的 kind
BUCKET_KIND = {"services": "service", "crontab_jobs": "crontab", "active_hermes_jobs": "hermes-cron"}
BUCKETS = ("links", "services", "crontab_jobs", "active_hermes_jobs", "paused_hermes_jobs")


# ───────────── 框架 ─────────────

def load_config(path):
    """返回 (probes, defaults)；整体读不了抛 ConfigError"""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在：{path}")
    except ValueError as e:
        raise ConfigError(f"配置文件不是合法 JSON：{e}")
    if not isinstance(cfg, dict) or cfg.get("version") != 1 or not isinstance(cfg.get("probes"), list):
        raise ConfigError('配置顶层应为 {"version": 1, "probes": [...]}')
    defaults = {**DEFAULTS, **(cfg.get("defaults") or {})}
    return cfg["probes"], defaults


def resources():
    r = {"mem_pct": None, "disk_pct": None, "load": None}
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        r["mem_pct"] = round((mem["MemTotal"] - mem["MemAvailable"]) * 100 / mem["MemTotal"])
    except Exception as e:
        print(f"check: 读内存失败: {e}", file=sys.stderr)
    try:
        du = shutil.disk_usage("/")
        r["disk_pct"] = round(du.used * 100 / (du.used + du.free))     # 与 df 的 Use% 口径一致
    except Exception as e:
        print(f"check: 读磁盘失败: {e}", file=sys.stderr)
    try:
        r["load"] = [round(x, 2) for x in os.getloadavg()]
    except Exception as e:
        print(f"check: 读负载失败: {e}", file=sys.stderr)
    return r


def collect(config_path):
    t0, now = time.monotonic(), time.time()
    st = {b: [] for b in BUCKETS}
    st["skipped"], problems = [], []
    total, config_error = 0, False

    def perr(name, detail, probe, scope=None):
        problems.append({"kind": "probe-error", "name": name, "scope": scope, "detail": detail, "probe": probe})

    try:
        probes, defaults = load_config(config_path)
    except ConfigError as e:
        probes, defaults, config_error = [], dict(DEFAULTS), True
        perr("config", str(e), "config")
        print(f"check: {e}", file=sys.stderr)

    # 1) 先静态检查每一项，挑出要跑的
    todo, seen, used_kinds = [], set(), set()
    for i, p in enumerate(probes):
        pid = p.get("id") if isinstance(p, dict) else None
        label = pid if isinstance(pid, str) and pid else f"probes[{i}]"
        if not isinstance(p, dict):
            config_error = True; perr(label, "探测项应为 object", label); continue
        kind = p.get("kind")
        if kind in KINDS:
            used_kinds.add(kind)
        if not (isinstance(pid, str) and pid):
            config_error = True; perr(label, "缺 id", label); continue
        if pid in seen:
            config_error = True; perr(label, "id 重复", label); continue
        seen.add(pid)
        if kind not in KINDS:
            config_error = True; perr(label, f"未知 kind {kind!r}（可用：{', '.join(KINDS)}）", pid); continue
        if p.get("enabled", True) is False:
            st["skipped"].append({"kind": kind, "name": p.get("name") or pid, "reason": "disabled"}); continue
        todo.append(p)

    for kind in KINDS:
        if kind not in used_kinds:
            st["skipped"].append({"kind": kind, "name": None, "reason": "not_configured"})

    # 2) 并发跑
    def one(p):
        timeout = p.get("timeout_s", defaults["timeout_s"])
        grace = p.get("grace", defaults["grace"])
        if not is_num(timeout) or timeout <= 0 or not is_num(grace) or grace <= 0:
            raise ConfigError("timeout_s / grace 应为正数")
        return KINDS[p["kind"]](p, {"now": now, "timeout": timeout, "grace": grace})

    pool = cf.ThreadPoolExecutor(max_workers=max(1, min(16, len(todo))))
    futs = {pool.submit(one, p): p for p in todo}
    done, pending = cf.wait(futs, timeout=ROUND_BUDGET_S)
    for fut in pending:
        p = futs[fut]
        total += 1
        perr(p.get("name") or p["id"], f"超出整轮预算 {ROUND_BUDGET_S}s", p["id"])
    for fut in done:
        p = futs[fut]
        label = p.get("name") if isinstance(p.get("name"), str) else p["id"]
        try:
            res = fut.result()
        except ConfigError as e:
            config_error = True; total += 1
            perr(label, f"配置错误：{e}", p["id"]); continue
        except MissingTool as e:
            total += 1
            st["skipped"].append({"kind": p["kind"], "name": label, "reason": f"missing_tool:{e.tool}"})
            perr(label, str(e), p["id"]); continue
        except subprocess.TimeoutExpired as e:
            total += 1
            perr(label, f"超时（{e.timeout}s）", p["id"]); continue
        except Exception as e:
            total += 1
            perr(label, f"{type(e).__name__}: {e}", p["id"]); continue
        for bucket, rows in res.items():
            for row in rows:
                st[bucket].append(row)
                if bucket == "paused_hermes_jobs":
                    continue
                total += 1
                if not row["ok"]:
                    kind = row["type"] if bucket == "links" else BUCKET_KIND[bucket]
                    scope = row.get("scope") if bucket == "services" else \
                        (row.get("profile") or None) if bucket == "active_hermes_jobs" else None
                    problems.append({"kind": kind, "name": row["name"], "scope": scope,
                                     "detail": row["detail"], "probe": p["id"]})
    pool.shutdown(wait=False, cancel_futures=True)

    # 3) 结果按配置顺序排，输出稳定、diff 友好
    order = {p["id"]: i for i, p in enumerate(todo)}
    problems.sort(key=lambda x: (x["kind"] == "probe-error", order.get(x["probe"], -1)))

    exit_code = 2 if config_error else (1 if problems else 0)
    if exit_code == 0:
        summary = f"✅ 全部正常（{total} 项）"
    else:
        names = ", ".join(f"{x['kind']}:{x['name']}" for x in problems[:5])
        summary = f"❌ {len(problems)} 个问题：{names}" + (" 等" if len(problems) > 5 else "")
        if config_error:
            summary = "⚠️ 巡检配置有误；" + summary

    d = {
        "schema_version":   schema.SCHEMA_VERSION,
        "service":          schema.SERVICE_NAME,
        "host":             socket.gethostname(),
        "checked_at":       datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "checked_at_epoch": int(now),
        "duration_ms":      int((time.monotonic() - t0) * 1000),
        "healthy":          exit_code == 0,
        "exit_code":        exit_code,
        "problem_count":    len(problems),
        "total_checked":    total,
        "summary":          summary,
        "problems":         problems,
        **st,
        "resources":        resources(),
    }
    return d, bool(pending)


def write_atomic(d, out):
    text = json.dumps(d, ensure_ascii=False, indent=2) + "\n"
    if out == "-":
        sys.stdout.write(text); return
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = f"{out}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, out)        # 读方不会读到半截文件
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main():
    ap = argparse.ArgumentParser(description="内网巡检：按 probes.json 声明执行探测，写 status.json")
    ap.add_argument("--config", default=os.environ.get("WATCHER_PROBES", CONFIG))
    ap.add_argument("--out", default=os.environ.get("WATCHER_STATUS", OUT), help="输出路径，- 表示打到 stdout")
    a = ap.parse_args()

    d, hung = collect(a.config)
    errs = schema.validate(d)
    if errs:   # 自己产出的都不合 schema = 代码 bug，别写出去污染下游
        print("check: 产出不符合 schema（代码 bug）：" + "；".join(errs[:5]), file=sys.stderr)
        sys.exit(2)
    write_atomic(d, a.out)
    sys.stdout.flush()
    if hung:   # 有探测线程卡住：结果已写完，直接退出，不等它
        os._exit(d["exit_code"])
    sys.exit(d["exit_code"])


if __name__ == "__main__":
    main()
