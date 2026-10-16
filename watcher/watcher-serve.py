#!/usr/bin/env python3
"""
watcher-serve.py —— 只读状态服务

安全设计：
  1. 只监听 127.0.0.1（不对外）
  2. 只放行 /s/<TOKEN>.json 单一路径，其余一律 404
  3. 只读，不支持任何写操作
  4. token 从文件读，不硬编码

部署：systemd（见 deploy/watcher-serve.service）
"""
import http.server, socketserver, subprocess, os, sys, time, threading, json, datetime

# ══════════ 配置区 ══════════
BIND        = "127.0.0.1"
PORT        = 18080
TOKEN_FILE  = "/var/lib/watcher/.token"
CHECK_CMD   = "/opt/watcher/check.sh"          # 巡检脚本
CACHE       = "/var/lib/watcher/status.json"   # 巡检输出
REFRESH_SEC = 300                              # 刷新间隔
# ════════════════════════════

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except Exception as e:
        print(f"❌ 读不到 token: {e}", file=sys.stderr)
        sys.exit(1)
    if len(token) < 32:
        # 空文件 / 过短 token 会让 /s/.json 之类的路径变成公开地址
        print(f"❌ token 太短（{len(token)} 字符，至少 32）：{TOKEN_FILE}", file=sys.stderr)
        sys.exit(1)
    return token

TOKEN = load_token()

def refresh_loop():
    """后台线程：定期跑巡检脚本"""
    while True:
        try:
            r = subprocess.run([CHECK_CMD], timeout=120, capture_output=True, text=True)
            if r.returncode not in (0, 1):     # 1 = 巡检发现问题（正常结果），2+ = 配置/框架错误
                print(f"巡检脚本退出码 {r.returncode}: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"巡检失败: {e}", file=sys.stderr)
        time.sleep(REFRESH_SEC)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # 只认 /s/<TOKEN>.json
        if self.path != f"/s/{TOKEN}.json":
            self.send_error(404)
            return
        try:
            with open(CACHE, "rb") as f:
                body = f.read()
        except Exception as e:
            print(f"读取 {CACHE} 失败: {e}", file=sys.stderr)
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):   self.send_error(405)
    def do_PUT(self):    self.send_error(405)
    def do_DELETE(self): self.send_error(405)

    def log_message(self, *a):
        pass   # 静默，不刷日志

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    with Server((BIND, PORT), Handler) as httpd:
        print(f"✅ watcher-serve 在 {BIND}:{PORT}（只放行 /s/<TOKEN>.json）")
        httpd.serve_forever()
