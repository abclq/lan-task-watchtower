# 🗼 LAN Task Watchtower

<p align="center">
  <img src="https://img.shields.io/badge/内网巡检-零依赖-blue?style=flat-square" alt="内网巡检">
  <img src="https://img.shields.io/badge/frp-透传-orange?style=flat-square" alt="frp">
  <img src="https://img.shields.io/badge/SSH-不需要-success?style=flat-square" alt="no ssh">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/协议-MIT-green?style=flat-square" alt="MIT">
</p>

<p align="center">
  <b>内网服务器任务巡检 + 台账 + 自愈 + 公网只读接口</b><br>
  <sub>不用 SSH 私钥 · 不引入登录凭据 · 不新开公网端口 · 复用已有 frp 通道</sub>
</p>

---

## 📖 这是什么

一套**内网机器「有没有按时干活」**的监控方案。

**跟常见的进程监控不一样**：它不看「nginx 进程还在不在」，它看「**昨晚的备份跑完了吗、失败几条**」。

> 进程活着但三天没跑，等于没干 —— 这种才是真问题。

**典型场景**：家里/公司内网有一台跑各种定时任务的机器（备份、爬虫、同步、服务），你想知道它们**有没有按时干活**，出问题**第一时间知道**，但：

- ❌ 机器**没有公网 IP**，外面连不进来
- ❌ **不想交 SSH 私钥**（凭据外流风险）
- ❌ **不想为了监控再开一个公网端口**（攻击面变大）
- ✅ 机器**能主动出网**
- ✅ **已经有 frp 通道**

这套方案就是为这种场景写的。

---

## 🏗 架构

```
        内网机器（无公网 IP）                    公网 VPS
┌──────────────────────────────┐        ┌────────────────────────┐
│                              │        │                        │
│  ① 巡检脚本（cron 每 5 分钟）│        │   frps :7000           │
│      ↓ 写 status.json        │        │   :43154 映射端口       │
│                              │        │                        │
│  ② 只读 HTTP 服务             │        │                        │
│     127.0.0.1:18080          │        │                        │
│     只放行 /s/<TOKEN>.json   │        │                        │
│     其余一律 404              │        │                        │
│          ▲                   │        │                        │
│          │                   │        │                        │
│  ③ frpc ─────────────────────┼────────┼─→ frps                 │
│     主动连出去，不用别人进来  │        │                        │
│                              │        │                        │
│  ④ 自愈脚本（cron 每 10 分钟）│        │                        │
│     发现服务死了 → 自己重启   │        │                        │
│                              │        │                        │
│  ⑤ 台账生成（cron 每 15 分钟）│        │                        │
└──────────────────────────────┘        └───────────┬────────────┘
                                                    │
                                    HTTP GET http://VPS:43154/s/<TOKEN>.json
                                                    │
                                        ┌───────────▼────────────┐
                                        │  公网侧消费方            │
                                        │  （定时任务 / 脚本 /     │
                                        │    自动化平台 / 告警系统）│
                                        │  → 有异常就通知你         │
                                        └────────────────────────┘
```

**数据流**：

1. 内网 cron 每 5 分钟跑巡检，把结果写成 `status.json`
2. 本地只读 HTTP 服务把**单个带令牌的路径**暴露在 `127.0.0.1:18080`
3. frpc **主动**连到公网 frps，把 18080 映射成公网 43154
4. 公网侧定时 GET，解析 `healthy` / `problems` 决定是否告警
5. 内网自愈脚本每 10 分钟看一眼，**服务死了自己重启**，不用人管

---

## ✨ 特性

| 特性 | 说明 |
|---|---|
| 🔒 **不用 SSH** | 全程不需要私钥、不需要搬运凭据、不需要公网 IP |
| 🚪 **不新开端口** | 复用已有 frp 通道，只加一条 proxy 映射 |
| 🎯 **单路径暴露** | 只放行 `/s/<TOKEN>.json`，目录枚举/错误令牌一律 404 |
| 🧠 **查「有没有干活」** | 按任务自身周期判定逾期，不是简单查进程存活 |
| 🔧 **自愈** | 白名单内的死服务自动重启，改配置/凭据一律不碰 |
| 📋 **台账** | 自动生成人类可读的 Markdown 台账，异常置顶 |
| 🤫 **静默设计** | 一切正常时零输出、零打扰；只有真动手或需决策才吭声 |
| 📧 **告警闭环** | 公网侧发告警邮件 → 内网侧自动读信 → **立刻再修一次**（可选组件） |
| 🏷 **不打扰归档** | 告警邮件自动归入标签、移出收件箱，收着但不吵人 |
| 🕐 **失联检测** | 靠时间戳新鲜度判断链路断没断（链路断了 JSON 不会变） |

---

## 📁 文件结构

```
.
├── README.md
├── watcher/
│   ├── check.sh              # ① 巡检入口（cron / serve 调它，兼容旧路径）
│   ├── check.py              # ① 巡检本体：按 probes.json 声明执行探测，写 status.json
│   ├── schema.py             # status.json 字段约定 + 校验（三个脚本共用）
│   ├── watcher-serve.py      # ② 只读 HTTP 服务（令牌路径）
│   ├── autofix.py            # ④ 自愈（白名单 + 存活复检）
│   ├── build-ledger.py       # ⑤ 台账生成
│   └── alert-mail-watch.py   # ⑥ 告警邮件监听（收信 → 立刻再修 → 归档）
├── deploy/
│   ├── watcher-serve.service # systemd 单元
│   ├── frpc-watcher.toml     # frpc 映射配置示例
│   ├── probes.example.json   # 探测配置示例（复制到 /etc/watcher/probes.json）
│   └── crontab.example       # crontab 示例
├── tests/
│   ├── run_tests.py          # 端到端测试：python3 tests/run_tests.py
│   ├── fixtures/             # 健康 / 有问题 / 旧格式 三份 status.json
│   ├── fake-hermes-adapter.py
│   └── bin/                  # 测试用假 systemctl / sudo
└── docs/
    └── 方案说明.md            # 设计取舍与踩坑记录
```

---

## 🚀 部署

### 一、公网 VPS：frps（已有可跳过）

```toml
# frps.toml
bindPort = 7000
auth.method = "token"
auth.token = "<强随机串>"
```

### 二、内网机器

**1. 装巡检脚本**

需要 Python 3.10+，只用标准库。

```bash
sudo mkdir -p /opt/watcher /var/lib/watcher /etc/watcher
sudo cp watcher/*.py watcher/check.sh /opt/watcher/      # check.py 依赖同目录的 schema.py
sudo chmod +x /opt/watcher/check.sh /opt/watcher/check.py

# 声明要巡检什么（见下文「🧩 探测配置」）
sudo cp deploy/probes.example.json /etc/watcher/probes.json
sudo vim /etc/watcher/probes.json

# 先手动跑一次，看输出对不对（- 表示打到屏幕，不写文件）
/opt/watcher/check.py --out -

# 每 5 分钟跑一次
( crontab -l 2>/dev/null; echo "*/5 * * * * /opt/watcher/check.sh" ) | crontab -
```

> 退出码：0 健康 / 1 有问题 / 2 配置写错了。配置写错时 status.json 照样会写（`exit_code: 2`），
> 公网侧能看到，不会悄悄变成「一切正常」。

**2. 起只读 HTTP 服务**

```bash
# 生成访问令牌
TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(16))")
echo "$TOKEN" | sudo tee /var/lib/watcher/.token
sudo chmod 600 /var/lib/watcher/.token

# 装 systemd 单元
sudo cp deploy/watcher-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watcher-serve

# 本机自测
curl "http://127.0.0.1:18080/s/$TOKEN.json"     # → 200
curl "http://127.0.0.1:18080/"                  # → 404（正常）
```

**3. frpc 加一条映射**

```toml
# frpc.toml 追加
[[proxies]]
name = "watcher-http"
type = "tcp"
localIP = "127.0.0.1"      # ⚠️ 只映射回环，别映射整机
localPort = 18080
remotePort = 43154          # ⚠️ 确认你的 frps 放行这个端口
```

```bash
sudo systemctl restart frpc
```

**4. 挂上自愈、台账、告警监听（可选）**

```bash
# 见 deploy/crontab.example
*/10 * * * * /usr/bin/python3 /opt/watcher/autofix.py
*/15 * * * * /usr/bin/python3 /opt/watcher/build-ledger.py >/dev/null 2>&1
*/5  * * * * /usr/bin/python3 /opt/watcher/alert-mail-watch.py
```

**5. 告警闭环（可选）**

见上文「🔔 告警闭环」一节。改 `alert-mail-watch.py` 顶部配置区的
`MAIL_CMD` / `SUBJECT_PATTERNS` / `ALLOWED_SENDERS`，跟公网侧对齐即可。

### 三、公网侧消费

```
GET http://<VPS_IP>:43154/s/<TOKEN>.json
```

| 情况 | 处理 |
|---|---|
| `healthy == true` | 正常，静默 |
| `healthy == false` | 列出 `problems` 数组每一条 |
| `exit_code == 2` | 报「巡检配置有误」（`problems` 里是 `probe-error`） |
| HTTP 失败 / 超时 | 报「拉取失败，链路可能断了」 |
| `now - checked_at_epoch > 900` | 报「数据失联」（**关键**：链路断了 JSON 不会变，只能靠时间戳） |
| 没有 `schema_version` 或不等于 1 | 报「格式不对」，别按旧字段硬解析 |

---

## 🔔 告警闭环（可选）

**问题**：公网侧发现异常 → 通知你 → 但你未必马上看到；而内网侧的自愈可能刚好没覆盖到。

**做法**：让**公网侧发一封告警邮件**，内网侧**自动读信 → 立刻再修一次**。

```
公网侧（定时任务 / 自动化平台）
   │  拉接口 → 命中规则 → 发告警邮件（主题带固定标记）
   ▼
告警邮箱
   │  内网侧 cron 每 5 分钟读一次未读
   ▼
内网侧 alert-mail-watch.py
   ├─ 认出告警邮件（主题标记 + 发件人白名单）
   ├─ 立刻跑一次自愈（把能修的先修了）
   ├─ 记事件日志
   └─ 归入标签 + 移出收件箱（收着但不打扰）
```

**设计要点**：

| 要点 | 为什么 |
|---|---|
| **主题固定标记** | 如 `【本机告警】`，是识别告警邮件的唯一约定，必须和公网侧对齐 |
| **发件人白名单** | 防止别人冒用主题触发内网动作 |
| **搜全邮箱**（含垃圾箱） | 机器发的邮件**很容易被判垃圾邮件**，只搜收件箱会漏 |
| **归标签 + 移出收件箱** | 邮件收着留痕，但不进收件箱、不推送通知（不吵人） |
| **只做重启** | 和自愈同一条红线：改配置/凭据/密码一律不碰 |

> **为什么不用邮箱过滤器？**
> Gmail 过滤器（`settings.filters`）需要额外授权 scope。如果 token 没有这个权限，
> 用「定时脚本搬邮件」替代，效果等价，代价是最多延迟一个巡检周期。

---

## 📡 接口返回（schema v1）

接口原样返回 `status.json`。字段定义在 `watcher/schema.py`，改字段要同步改这里并把 `schema_version` 加 1。

```json
{
  "schema_version": 1,
  "service": "local-tasks-health",
  "host": "app-node-01",
  "checked_at": "2026-10-14T21:12:04+08:00",
  "checked_at_epoch": 1791983524,
  "duration_ms": 6210,
  "healthy": true,
  "exit_code": 0,
  "problem_count": 0,
  "total_checked": 58,
  "summary": "✅ 全部正常（58 项）",
  "problems": [],
  "links": [
    { "kind": "link", "type": "backup", "name": "外接盘备份", "ok": true, "detail": "19.4h 前跑过，失败 1 条" }
  ],
  "services": [
    { "name": "my-app", "scope": "system", "state": "active", "ok": true, "enabled": "enabled", "ghost": false, "detail": "active" }
  ],
  "crontab_jobs": [],
  "active_hermes_jobs": [],
  "paused_hermes_jobs": [],
  "skipped": [ { "kind": "dns", "name": null, "reason": "not_configured" } ],
  "resources": { "mem_pct": 41, "disk_pct": 63, "load": [0.21, 0.3, 0.28] }
}
```

<details><summary>字段说明</summary>

| 字段 | 类型 | 含义 |
|---|---|---|
| `schema_version` | int | 固定 1 |
| `service` | str | 固定 `local-tasks-health` |
| `host` | str | 主机名 |
| `checked_at` | str | ISO 8601，带时区偏移 |
| `checked_at_epoch` | int | Unix 秒；**失联判断用它** |
| `duration_ms` | int | 本轮耗时 |
| `healthy` | bool | `exit_code == 0` |
| `exit_code` | int | 0 健康 / 1 有问题 / 2 配置或框架错误 |
| `problem_count` | int | `problems` 条数 |
| `total_checked` | int | 实际判定的条目数（服务、活跃任务、链路各算一条；暂停任务和 skipped 不算） |
| `summary` | str | 一句话结论 |
| `problems[]` | `{kind, name, scope, detail, probe}` | kind ∈ `service` `hermes-cron` `crontab` `backup` `capture` `proxy` `dns` `probe-error`；probe = 配置里的探测 id |
| `links[]` | `{kind:"link", type, name, ok, detail}` | type ∈ `backup` `capture` `proxy` `dns` |
| `services[]` | `{name, scope, state, ok, enabled, ghost, detail}` | state = ActiveState；enabled = UnitFileState；ghost = 单元不存在 |
| `crontab_jobs[]` | `{name, log, period_min, age_min, ok, detail}` | age_min 为 null = 日志不存在 |
| `active_hermes_jobs[]` | `{name, profile, schedule, period_min, age_h, last_status, ok, detail}` | 启用中的任务 |
| `paused_hermes_jobs[]` | 同上，`ok` 恒为 true | 暂停的任务，不报警 |
| `skipped[]` | `{kind, name, reason}` | reason ∈ `not_configured`（整类没配，name 为 null）`disabled` `missing_tool:<命令>` |
| `resources` | `{mem_pct, disk_pct, load}` | 取不到的值为 null |

</details>

---

## 🧭 判定口径

| 检查项（kind） | 怎么判 | 可调参数 |
|---|---|---|
| **常驻服务**（`service`） | `systemctl [--user] show` 的 ActiveState 不是 `active` → 挂；LoadState 为 `not-found` → 幽灵项 | `scope` |
| **定时任务**（`hermes_jobs`） | 超过「**自身周期 × 2.5**」没跑 → 逾期；上次结果 `error` → 异常；从没跑过 / 缺周期 → 异常 | `grace` `error_statuses` |
| **crontab**（`crontab_log`） | 靠对应**日志文件的修改时间**判逾期（同样 2.5 倍宽限）；日志不存在 → 异常 | `period_min` `grace` |
| **备份链**（`backup_log`） | 日志超 **30h** 没更新 → 挂；日志里「失败 N 条」> 2 → 异常 | `max_age_h` `fail_regex` `max_fail` |
| **抓包类**（`capture_file`） | 最新的抓包文件 **6 秒内没变大** → 断了（轮转出新文件算活） | `glob` `window_s` |
| **代理类**（`http_proxy`） | HTTP 401 或 200 → 活（401 = 活着但没给 key） | `ok_codes` |
| **DNS**（`dns`） | 用它解析一个域名，答不上来 → 坏；配了 `server` 就用 `dig @server`，没装 dig 直接报错，不偷偷换系统解析器 | `server` `query` |

所有探测并发执行，单项有超时（默认 10s），整轮上限 110s。

## 🧩 探测配置

巡检什么全部写在 `/etc/watcher/probes.json`，代码里不写死任何路径（完整示例：`deploy/probes.example.json`）：

```json
{
  "version": 1,
  "defaults": { "timeout_s": 10, "grace": 2.5 },
  "probes": [
    { "id": "svc-app", "kind": "service", "name": "my-app", "scope": "system" },
    { "id": "hermes",  "kind": "hermes_jobs", "source_cmd": ["/opt/watcher/adapters/hermes-jobs.sh"] },
    { "id": "bk-usb",  "kind": "backup_log", "name": "外接盘备份", "log": "/var/log/backup.log" }
  ]
}
```

- 每项必须有唯一的 `id` 和 `kind`；`"enabled": false` 可临时停用（记入 `skipped`）
- 某种 kind **一项都没配** → `skipped` 里写 `not_configured`，不会静默当成正常
- 配置写错（未知 kind、缺必填参数、id 重复）→ 该项记 `probe-error`，`exit_code = 2`，其它项照常跑
- **hermes 适配器**：`source_cmd` 是参数数组（不经 shell），向 stdout 输出 JSON 数组，每项
  `{name, profile, schedule, period_min, last_run_epoch, last_status, enabled}`。
  hermes 的状态存在哪、什么格式由你的环境决定，适配器负责翻译成这个格式；`tests/fake-hermes-adapter.py` 可作参考

**为什么用「周期 × 2.5」而不是严格按周期？** 因为任务偶尔晚几分钟是正常的，报假警比漏报更烦人。

---

## 🔧 自愈规则

**原则**：只动**已经死的**，绝不动活的；只做「重启」这类**可逆**操作。

**只认白名单**：只有 `autofix.py` 顶部 `WHITELIST` 里登记过的服务才会被自动重启；
没登记的服务死了只报告。新加一个巡检项不会自动获得重启权限 —— 这和「保守、只做可逆操作」的定位一致，
代价是新服务要记得手动登记。

| 问题 | 自动处理 |
|---|---|
| 白名单内的服务 `inactive` | ✅ 重启它（**动手前先复检**，快照可能是几分钟前的） |
| 白名单外的服务挂了 | ❌ 只报告 |
| 服务/单元**根本不存在**（幽灵项） | ❌ 跳过，只记日志 + 台账标 👻，**不重启也不推送**（避免天天误报） |
| 抓包 / 代理服务挂了 | ✅ 重启（服务在白名单内时）；链路探测本身只报告 |
| 备份失败 | ❌ **不自动**（怕越修越坏），只报告 |
| 定时任务逾期 | ❌ **不自动**，只报告 |
| **被用户主动停用的服务** | ❌ **不碰**：`UnitFileState` 为 `disabled` / `masked` 即视为主动停用，只记日志 |
| status.json 格式不对 / 超过 15 分钟没更新 | ❌ 本轮**不动手**（格式不对退出码 2；过期打印一行提示） |
| **改配置 / 改凭据 / 改密码 / 重启整机** | ❌ **绝不碰**，只报告 |

---

## ❓ FAQ

**Q：为什么不用 SSH？**
A：内网机器没有公网 IP，要么在外面开端口等连（攻击面大），要么搬私钥（凭据外流风险）。这套方案**机器主动出网**，全程不需要任何登录凭据。

**Q：为什么要单独起个 HTTP 服务？**
A：frp 只能映射 TCP，所以本地得有个监听。用 Python 内置的 `http.server` 就够，**只监听回环 + 只放行单一路径**。

**Q：token 泄露了怎么办？**
A：改文件里的 token，重启服务即可（`systemctl restart watcher-serve`）。token 只读，拿不到任何写权限。

**Q：frp 端口被拒（`port not allowed`）？**
A：免费 frp 站通常只放行**特定端口段**。换个端口试，或者用自己搭的 frps。

**Q：链路断了怎么知道？**
A：看 `checked_at`。链路断了以后 JSON 内容**不会变**，所以**必须**靠时间戳新鲜度判断 —— 这是整套方案最容易漏掉的一点。

**Q：告警邮件被判定成垃圾邮件怎么办？**
A：机器发出的邮件（尤其是 QQ/企业邮箱发件人）**很容易进垃圾箱**。两个办法：
① 有权限的话，在邮箱里建过滤器，把该发件人/主题永久放行；
② 没权限就**让监听脚本搜全邮箱**（`in:anywhere`），认出后顺手移出垃圾箱 + 归标签。
**只搜收件箱一定会漏。**

**Q：怎么防止别人伪造告警邮件？**
A：加**发件人白名单**。只认约定的发件地址，主题对了但发件人不对的一律忽略。
内网侧只做「重启」这类可逆操作，风险本来就有限，白名单是最后一道门。

**Q：会不会误报？**
A：会。它依赖「日志文件的修改时间」，如果某个任务**根本不写日志**，就会被误判。这是已知软肋。

---

## 📄 协议

MIT
