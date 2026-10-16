#!/usr/bin/env python3
"""
tests/run_tests.py —— 巡检三件套的端到端测试（只用标准库）

  python3 tests/run_tests.py           # 全部
  python3 tests/run_tests.py -v        # 详细

用 tests/bin 里的假 systemctl / sudo 顶替真命令，不碰本机任何服务；
check.py / autofix.py / build-ledger.py 都以子进程方式跑，和 cron 下的调用方式一致。
"""
import http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

HERE    = os.path.dirname(os.path.abspath(__file__))
ROOT    = os.path.dirname(HERE)
WATCHER = os.path.join(ROOT, "watcher")
FIX     = os.path.join(HERE, "fixtures")
ADAPTER = os.path.join(HERE, "fake-hermes-adapter.py")
sys.path.insert(0, WATCHER)
import schema  # noqa: E402

PY = sys.executable


def rd(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def wr(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)


def load_fixture(name, fresh=True):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        d = json.load(f)
    if fresh and "checked_at_epoch" in d:
        d["checked_at_epoch"] = int(time.time())
    return d


class Env:
    """每个测试一个临时目录 + 指向假命令的 PATH"""
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="watcher-test-")
        self.status = os.path.join(self.dir, "status.json")
        self.config = os.path.join(self.dir, "probes.json")
        self.sysctl_log = os.path.join(self.dir, "systemctl.log")
        self.env = {**os.environ,
                    "PATH": os.path.join(HERE, "bin") + os.pathsep + os.environ.get("PATH", ""),
                    "WATCHER_STATUS": self.status,
                    "WATCHER_PROBES": self.config,
                    "WATCHER_AUTOFIX_LOG": os.path.join(self.dir, "autofix.log"),
                    "FAKE_SYSTEMCTL_LOG": self.sysctl_log}

    def path(self, *p):
        return os.path.join(self.dir, *p)

    def write_json(self, path, d):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)

    def run(self, *cmd, timeout=60):
        return subprocess.run(list(cmd), env=self.env, capture_output=True, text=True, timeout=timeout)

    def check(self, probes=None, raw=None):
        if raw is not None:
            wr(self.config, raw)
        elif probes is not None:
            self.write_json(self.config, {"version": 1, "probes": probes})
        r = self.run(PY, os.path.join(WATCHER, "check.py"))
        d = json.loads(rd(self.status)) if os.path.exists(self.status) else None
        return r, d

    def sysctl_calls(self):
        return rd(self.sysctl_log).splitlines() if os.path.exists(self.sysctl_log) else []

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ProxyStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401 if self.path == "/v1/models" else 404)
        self.end_headers()

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()


# ═════════════════════════ schema.py ═════════════════════════

class TestSchema(unittest.TestCase):
    def test_fixtures(self):
        self.assertEqual(schema.validate(load_fixture("healthy.json")), [])
        self.assertEqual(schema.validate(load_fixture("problems.json")), [])
        errs = schema.validate(load_fixture("legacy.json"))
        self.assertTrue(any("schema_version" in x for x in errs))
        self.assertTrue(any("缺字段 problems" in x for x in errs))

    def test_garbage_never_raises(self):
        for g in (None, [], "x", 1, {"problems": "x"}, {"services": [1, None, {"name": 3}]},
                  {"schema_version": 1, "healthy": 1, "exit_code": True}):
            self.assertIsInstance(schema.validate(g), list)
            self.assertIsInstance(schema.items(g if isinstance(g, dict) else {}, "services"), list)
        self.assertEqual(schema.items({"services": [1, {"name": "a"}]}, "services"), [{"name": "a"}])

    def test_bool_is_not_int(self):
        d = load_fixture("healthy.json"); d["problem_count"] = False
        self.assertTrue(any("problem_count" in x for x in schema.validate(d)))

    def test_consistency(self):
        d = load_fixture("healthy.json"); d["problem_count"] = 3
        self.assertTrue(any("不一致" in x for x in schema.validate(d)))
        d = load_fixture("healthy.json"); d["exit_code"] = 1
        self.assertTrue(any("healthy 与 exit_code" in x for x in schema.validate(d)))

    def test_age(self):
        d = {"checked_at_epoch": 1000}
        self.assertEqual(schema.age_s(d, now=1060), 60)
        self.assertFalse(schema.is_stale(d, now=1000 + 15 * 60))
        self.assertTrue(schema.is_stale(d, now=1000 + 15 * 60 + 1))
        self.assertIsNone(schema.age_s({"checked_at_epoch": True}))
        self.assertTrue(schema.is_stale({}))


# ═════════════════════════ check.py ═════════════════════════

class TestCheck(Base):
    def full_probes(self, port):
        e = self.e
        now = time.time()
        # crontab：一个新鲜、一个逾期
        wr(e.path("fresh.log"), "ok\n")
        wr(e.path("stale.log"), "ok\n")
        os.utime(e.path("stale.log"), (now - 3600 * 3, now - 3600 * 3))          # 180min > 60×2.5
        # 备份：正常 / 失败过多 / 过期
        wr(e.path("bk-ok.log"), "备份完成，失败 3 条\n备份完成，失败 1 条\n")
        wr(e.path("bk-fail.log"), "备份完成，失败 5 条\n")
        wr(e.path("bk-old.log"), "备份完成，失败 0 条\n")
        os.utime(e.path("bk-old.log"), (now - 3600 * 31, now - 3600 * 31))
        # 抓包：一个在长、一个不动
        os.makedirs(e.path("cap-live")); os.makedirs(e.path("cap-dead"))
        wr(e.path("cap-live", "a.pcap"), b"x")
        wr(e.path("cap-dead", "a.pcap"), b"x")
        return [
            {"id": "svc-app",  "kind": "service", "name": "my-app"},
            {"id": "svc-cap",  "kind": "service", "name": "capture-svc"},
            {"id": "svc-gh",   "kind": "service", "name": "ghost-svc"},
            {"id": "svc-old",  "kind": "service", "name": "old-svc"},
            {"id": "svc-emb",  "kind": "service", "name": "embed-proxy", "scope": "user"},
            {"id": "hermes",   "kind": "hermes_jobs", "source_cmd": [PY, ADAPTER, "mixed"]},
            {"id": "cron-ok",  "kind": "crontab_log", "name": "新鲜", "log": e.path("fresh.log"), "period_min": 60},
            {"id": "cron-old", "kind": "crontab_log", "name": "逾期", "log": e.path("stale.log"), "period_min": 60},
            {"id": "cron-no",  "kind": "crontab_log", "name": "没日志", "log": e.path("nope.log"), "period_min": 60},
            {"id": "bk-ok",    "kind": "backup_log", "name": "备份正常", "log": e.path("bk-ok.log")},
            {"id": "bk-fail",  "kind": "backup_log", "name": "备份失败多", "log": e.path("bk-fail.log")},
            {"id": "bk-old",   "kind": "backup_log", "name": "备份过期", "log": e.path("bk-old.log")},
            {"id": "cap-live", "kind": "capture_file", "name": "抓包活", "glob": e.path("cap-live", "*.pcap"), "window_s": 2},
            {"id": "cap-dead", "kind": "capture_file", "name": "抓包死", "glob": e.path("cap-dead", "*.pcap"), "window_s": 2},
            {"id": "cap-none", "kind": "capture_file", "name": "抓包无文件", "glob": e.path("nothing", "*.pcap"), "window_s": 2},
            {"id": "px-ok",    "kind": "http_proxy", "name": "代理401", "url": f"http://127.0.0.1:{port}/v1/models"},
            {"id": "px-404",   "kind": "http_proxy", "name": "代理404", "url": f"http://127.0.0.1:{port}/other"},
            {"id": "px-down",  "kind": "http_proxy", "name": "代理不通", "url": "http://127.0.0.1:1/", "timeout_s": 3},
            {"id": "dns-sys",  "kind": "dns", "name": "系统DNS", "query": "localhost"},
            {"id": "dns-off",  "kind": "dns", "name": "停用DNS", "enabled": False, "server": "10.0.0.1", "query": "x"},
        ]

    def test_full_run(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyStub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        probes = self.full_probes(srv.server_address[1])

        stop = threading.Event()
        def grow():   # 抓包文件持续变大
            while not stop.is_set():
                with open(self.e.path("cap-live", "a.pcap"), "ab") as f:
                    f.write(b"y" * 100)
                time.sleep(0.2)
        threading.Thread(target=grow, daemon=True).start()
        t0 = time.time()
        try:
            r, d = self.e.check(probes)
        finally:
            stop.set(); srv.shutdown()
        elapsed = time.time() - t0

        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(schema.validate(d), [])
        self.assertEqual(d["exit_code"], 1)
        self.assertFalse(d["healthy"])
        self.assertLess(elapsed, 5, "三个 2s 抓包探测应并发，而不是串行累加")

        bad = {(p["kind"], p["name"]) for p in d["problems"]}
        expect_bad = {
            ("service", "capture-svc"), ("service", "ghost-svc"), ("service", "old-svc"), ("service", "embed-proxy"),
            ("hermes-cron", "逾期任务"), ("hermes-cron", "报错任务"), ("hermes-cron", "没跑过"), ("hermes-cron", "缺周期"),
            ("crontab", "逾期"), ("crontab", "没日志"),
            ("backup", "备份失败多"), ("backup", "备份过期"),
            ("capture", "抓包死"), ("capture", "抓包无文件"),
            ("proxy", "代理404"), ("proxy", "代理不通"),
        }
        self.assertEqual(bad, expect_bad)
        self.assertEqual(d["problem_count"], len(expect_bad))

        svc = {s["name"]: s for s in d["services"]}
        self.assertTrue(svc["my-app"]["ok"])
        self.assertTrue(svc["ghost-svc"]["ghost"])
        self.assertEqual(svc["old-svc"]["enabled"], "disabled")
        self.assertEqual(svc["embed-proxy"]["scope"], "user")
        self.assertIn("--user show embed-proxy -p LoadState -p ActiveState -p UnitFileState", self.e.sysctl_calls())

        links = {l["name"]: l for l in d["links"]}
        self.assertTrue(links["备份正常"]["ok"]); self.assertIn("失败 1 条", links["备份正常"]["detail"])
        self.assertTrue(links["抓包活"]["ok"])
        self.assertTrue(links["代理401"]["ok"]); self.assertEqual(links["代理401"]["detail"], "HTTP 401")
        self.assertTrue(links["系统DNS"]["ok"])

        self.assertEqual([j["name"] for j in d["paused_hermes_jobs"]], ["暂停的"])
        self.assertNotIn("暂停的", [p["name"] for p in d["problems"]])
        self.assertEqual(d["total_checked"], 5 + 5 + 3 + 3 + 3 + 3 + 1)   # 暂停的不计
        self.assertIn({"kind": "dns", "name": "停用DNS", "reason": "disabled"}, d["skipped"])
        self.assertFalse([s for s in d["skipped"] if s["reason"] == "not_configured"])
        self.assertTrue(isinstance(d["resources"]["mem_pct"], int))
        self.assertFalse([f for f in os.listdir(self.e.dir) if ".tmp." in f], "不应残留 .tmp 文件")

    def test_unconfigured_kinds_are_listed(self):
        wr(self.e.path("x.log"), "x")
        r, d = self.e.check([{"id": "c", "kind": "crontab_log", "name": "x", "log": self.e.path("x.log"), "period_min": 5}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(d["healthy"]); self.assertEqual(d["total_checked"], 1)
        nc = {s["kind"] for s in d["skipped"] if s["reason"] == "not_configured"}
        self.assertEqual(nc, {"service", "hermes_jobs", "backup_log", "capture_file", "http_proxy", "dns"})
        self.assertEqual(d["summary"], "✅ 全部正常（1 项）")

    def test_missing_config(self):
        r, d = self.e.check()     # 没写配置文件
        self.assertEqual(r.returncode, 2)
        self.assertEqual(schema.validate(d), [])
        self.assertEqual(d["exit_code"], 2)
        self.assertEqual(d["problems"][0]["kind"], "probe-error")
        self.assertIn("配置文件不存在", d["problems"][0]["detail"])
        self.assertEqual(len(d["skipped"]), 7)

    def test_bad_json_config(self):
        r, d = self.e.check(raw="{oops")
        self.assertEqual(r.returncode, 2)
        self.assertIn("不是合法 JSON", d["problems"][0]["detail"])

    def test_config_errors_dont_stop_other_probes(self):
        wr(self.e.path("x.log"), "x")
        r, d = self.e.check([
            {"id": "a", "kind": "crontab_log", "name": "好的", "log": self.e.path("x.log"), "period_min": 5},
            {"id": "b", "kind": "no_such_kind", "name": "未知"},
            {"id": "a", "kind": "dns", "name": "重复id", "query": "localhost"},
            {"kind": "dns", "name": "缺id", "query": "localhost"},
            {"id": "c", "kind": "crontab_log", "name": "缺周期", "log": self.e.path("x.log")},
            {"id": "d", "kind": "http_proxy", "name": "坏url", "url": "ftp://x"},
            {"id": "e", "kind": "hermes_jobs", "source_cmd": "echo 字符串命令"},
        ])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(schema.validate(d), [])
        self.assertTrue(d["crontab_jobs"][0]["ok"])            # 好的那项照常跑了
        errs = {p["probe"]: p["detail"] for p in d["problems"]}
        self.assertIn("未知 kind", errs["b"])
        self.assertIn("id 重复", errs["a"])
        self.assertIn("缺 id", errs["probes[3]"])
        self.assertIn("period_min", errs["c"])
        self.assertIn("http://", errs["d"])
        self.assertIn("source_cmd", errs["e"])
        self.assertTrue(d["summary"].startswith("⚠️ 巡检配置有误"))

    def test_hermes_adapter_failures(self):
        r, d = self.e.check([{"id": "h1", "kind": "hermes_jobs", "source_cmd": [PY, ADAPTER, "broken"]},
                             {"id": "h2", "kind": "hermes_jobs", "source_cmd": [PY, ADAPTER, "fail"]},
                             {"id": "h3", "kind": "hermes_jobs", "source_cmd": ["/no/such/adapter"]}])
        self.assertEqual(r.returncode, 1)
        errs = {p["probe"]: p["detail"] for p in d["problems"]}
        self.assertEqual({p["kind"] for p in d["problems"]}, {"probe-error"})
        self.assertIn("不是 JSON", errs["h1"])
        self.assertIn("退出码 3", errs["h2"])
        self.assertIn("FileNotFoundError", errs["h3"])

    def test_hermes_ok(self):
        r, d = self.e.check([{"id": "h", "kind": "hermes_jobs", "source_cmd": [PY, ADAPTER, "ok"]}])
        self.assertEqual(r.returncode, 0, d["problems"])
        self.assertEqual(d["active_hermes_jobs"][0]["age_h"], 5.0)

    def test_dns_server_needs_dig(self):
        env_path = self.e.env["PATH"]
        self.e.env["PATH"] = os.path.join(HERE, "bin")      # 只留假命令：一定没有 dig
        try:
            r, d = self.e.check([{"id": "d", "kind": "dns", "name": "指定DNS", "server": "127.0.0.1", "query": "x"}])
        finally:
            self.e.env["PATH"] = env_path
        self.assertIn({"kind": "dns", "name": "指定DNS", "reason": "missing_tool:dig"}, d["skipped"])
        self.assertEqual(d["problems"][0]["kind"], "probe-error")
        self.assertEqual(d["links"], [])                      # 没有偷偷改用系统解析器

    def test_check_sh_entry_compatible(self):
        wr(self.e.path("x.log"), "x")
        self.e.write_json(self.e.config, {"version": 1, "probes": [
            {"id": "c", "kind": "crontab_log", "name": "x", "log": self.e.path("x.log"), "period_min": 5}]})
        r = self.e.run(os.path.join(WATCHER, "check.sh"))     # cron / watcher-serve 就是这么调的
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")                        # 正常时静默
        d = json.loads(rd(self.e.status))
        self.assertEqual(schema.validate(d), [])
        self.assertTrue(d["healthy"])

    def test_output_feeds_consumers(self):
        """check.py 的真实产出喂给 autofix / build-ledger"""
        srv_probes = [{"id": "s", "kind": "service", "name": "capture-svc"},
                      {"id": "h", "kind": "hermes_jobs", "source_cmd": [PY, ADAPTER, "mixed"]}]
        r, d = self.e.check(srv_probes)
        self.assertEqual(r.returncode, 1)
        a = self.e.run(PY, os.path.join(WATCHER, "autofix.py"))
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertIn("重启服务 capture-svc", a.stdout)
        self.assertIn("逾期任务", a.stdout)
        out = self.e.path("台账.md")
        b = self.e.run(PY, os.path.join(WATCHER, "build-ledger.py"), out)
        self.assertEqual(b.returncode, 0, b.stderr)
        md = rd(out)
        self.assertNotIn("数据格式不对", md)
        self.assertIn("报错任务", md)


# ═════════════════════════ autofix.py ═════════════════════════

class TestAutofix(Base):
    def autofix(self, d, *args):
        self.e.write_json(self.e.status, d)
        return self.e.run(PY, os.path.join(WATCHER, "autofix.py"), *args)

    def test_problems(self):
        r = self.autofix(load_fixture("problems.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.e.sysctl_calls()
        self.assertIn("restart capture-svc", calls)              # system：经 sudo -n
        self.assertIn("--user restart embed-proxy", calls)       # user
        self.assertFalse([c for c in calls if any(x in c for x in ("ghost-svc", "old-svc", "legacy-app", "my-app"))])
        self.assertIn("重启服务 capture-svc（system）", r.stdout)
        self.assertIn("重启服务 embed-proxy（user）", r.stdout)
        self.assertIn("legacy-app 不在白名单", r.stdout)
        self.assertNotIn("ghost-svc", r.stdout)                  # 幽灵项不吵
        self.assertNotIn("old-svc", r.stdout)                    # 主动停用不吵
        self.assertIn("备份 外接盘备份", r.stdout)
        self.assertIn("定时任务 日报", r.stdout)
        log = rd(self.e.env["WATCHER_AUTOFIX_LOG"])
        self.assertIn("SKIP ghost-svc", log); self.assertIn("SKIP old-svc", log)

    def test_recheck_before_restart(self):
        d = load_fixture("problems.json")
        for s in d["services"]:
            if s["name"] == "capture-svc":
                s["name"] = "my-app"          # 快照说挂了，复检发现 active → 不动
        d["services"] = [s for s in d["services"] if s["name"] == "my-app"]
        r = self.autofix(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is-active my-app", self.e.sysctl_calls())
        self.assertNotIn("restart my-app", self.e.sysctl_calls())

    def test_healthy_is_silent(self):
        r = self.autofix(load_fixture("healthy.json"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(self.e.sysctl_calls(), [])

    def test_legacy_refuses(self):
        r = self.autofix(load_fixture("legacy.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("不符合 schema", r.stderr)
        self.assertEqual(self.e.sysctl_calls(), [])

    def test_garbage_no_keyerror(self):
        d = load_fixture("problems.json"); d["services"] = "oops"; d["problems"] = [1, None]
        r = self.autofix(d)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_stale_refuses(self):
        r = self.autofix(load_fixture("problems.json", fresh=False))
        self.assertEqual(r.returncode, 0)
        self.assertIn("分钟没更新", r.stdout)
        self.assertEqual(self.e.sysctl_calls(), [])

    def test_dry_run(self):
        r = self.autofix(load_fixture("problems.json"), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[dry-run] 将复检并重启服务 capture-svc", r.stdout)
        self.assertEqual(self.e.sysctl_calls(), [])
        self.assertFalse(os.path.exists(self.e.env["WATCHER_AUTOFIX_LOG"]))

    def test_missing_status_is_silent(self):
        r = self.e.run(PY, os.path.join(WATCHER, "autofix.py"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))


# ═════════════════════════ build-ledger.py ═════════════════════════

class TestLedger(Base):
    def ledger(self, d):
        self.e.write_json(self.e.status, d)
        out = self.e.path("台账.md")
        r = self.e.run(PY, os.path.join(WATCHER, "build-ledger.py"), out)
        return r, (rd(out) if os.path.exists(out) else "")

    def test_healthy(self):
        r, md = self.ledger(load_fixture("healthy.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("**无异常** ✅", md)
        self.assertIn("内存 41%", md)
        self.assertIn("未配置", md)
        self.assertNotIn("数据格式不对", md)
        self.assertNotIn("数据已过期", md)

    def test_problems(self):
        r, md = self.ledger(load_fixture("problems.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| service | capture-svc | system | inactive |", md)
        self.assertIn("👻 单元不存在", md)
        self.assertIn("逾期：62.0h", md)

    def test_stale_marked(self):
        r, md = self.ledger(load_fixture("problems.json", fresh=False))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("数据已过期", md.splitlines()[0])

    def test_legacy(self):
        r, md = self.ledger(load_fixture("legacy.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("数据格式不对", md)
        self.assertIn("— 未知", md)

    def test_garbage_no_keyerror(self):
        for g in ({"problems": "x", "services": [1, None], "links": {"a": 1}, "resources": [1]}, [], "str"):
            r, md = self.ledger(g)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("数据格式不对", md)

    def test_unreadable(self):
        wr(self.e.status, "{broken")
        r = self.e.run(PY, os.path.join(WATCHER, "build-ledger.py"), self.e.path("台账.md"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("读不了", r.stderr)


if __name__ == "__main__":
    unittest.main()
