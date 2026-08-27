# -*- coding: utf-8 -*-
"""
Web UI：本地 HTTP 服务 + 浏览器页面。
双击 exe → 自动打开浏览器 → 网页里选包、点解锁、看实时日志。
纯标准库，无第三方依赖。
"""
import os
import json
import queue
import socket
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import premium_unlocker as pu

PORT_START = 18789

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>竞品订阅解锁工具</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --border: #e5e7eb; --text: #1f2937;
  --muted: #6b7280; --accent: #2563eb; --accent-hover: #1d4ed8;
  --ok: #16a34a; --err: #dc2626; --radius: 8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 28px 20px 40px; }
header { display: flex; align-items: center; gap: 10px; margin-bottom: 20px; }
.logo { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); }
h1 { font-size: 18px; font-weight: 600; }
.ver { font-size: 12px; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; }
.spacer { flex: 1; }
.badge { font-size: 12px; padding: 3px 10px; border-radius: 999px; background: #f3f4f6; color: var(--muted); }
.badge.on { background: #ecfdf5; color: var(--ok); }
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.row { display: flex; gap: 10px; align-items: center; }
input[type=text] { flex: 1; padding: 9px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; outline: none; }
input[type=text]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.btn { border: 1px solid var(--border); background: #fff; padding: 9px 16px; border-radius: 6px; font-size: 14px; cursor: pointer; white-space: nowrap; }
.btn:hover { background: #f9fafb; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 500; padding: 10px 22px; }
.btn.primary:hover { background: var(--accent-hover); }
.btn.primary:disabled { background: #93c5fd; border-color: #93c5fd; cursor: not-allowed; }
.check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); margin: 12px 0 0; }
.check input { accent-color: var(--accent); }
.hint { font-size: 12px; color: var(--muted); margin-top: 10px; }
.loghead { display: flex; align-items: center; margin-bottom: 10px; }
.loghead h2 { font-size: 14px; font-weight: 600; }
pre.log { background: #0f172a; color: #e2e8f0; border-radius: 6px; padding: 14px; height: 340px; overflow-y: auto; font: 12px/1.7 Consolas, "Courier New", monospace; white-space: pre-wrap; word-break: break-all; }
footer { text-align: center; color: var(--muted); font-size: 12px; margin-top: 8px; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 5px; background: #9ca3af; }
.dot.run { background: var(--ok); animation: blink 1.2s infinite; }
@keyframes blink { 50% { opacity: .3; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="logo"></span>
    <h1>竞品订阅解锁工具</h1>
    <span class="ver">v__VER__</span>
    <span class="spacer"></span>
    <span id="device" class="badge"><span class="dot"></span>检测设备中…</span>
  </header>

  <div class="card">
    <div class="row">
      <input type="text" id="path" placeholder="输入 APK / XAPK 路径，或点「选择文件」">
      <button class="btn" id="pick">选择文件</button>
    </div>
    <label class="check"><input type="checkbox" id="install" checked> 自动安装到设备并启动验证</label>
    <label class="check"><input type="checkbox" id="force"> 强制单包模式（AAB 缺 split 时兜底，原生库缺失可能崩溃）</label>
    <div class="row" style="margin-top:16px">
      <button class="btn primary" id="start">解锁并安装</button>
      <span id="state" class="hint" style="margin-top:0">空闲</span>
    </div>
    <div class="hint">产物生成在安装包同目录 <b>xxx_unlocked/signed/</b>，可复装。首次运行会自动解压内置组件（约 30 秒）。</div>
  </div>

  <div class="card">
    <div class="loghead">
      <h2>AI 订阅解锁（DeepSeek）</h2>
      <span class="spacer"></span>
      <span id="ai-status" class="hint" style="margin-top:0"></span>
    </div>
    <div class="hint" style="margin-bottom:10px">遇到非 Adapty 的订阅 SDK 时，自动调用 DeepSeek 分析并生成补丁方案（每次约几毛钱）。Key 只保存在本机。</div>
    <div class="row">
      <input type="password" id="ai-key" placeholder="DeepSeek API Key（sk-...）">
      <button class="btn" id="ai-save">保存</button>
      <label class="check" style="margin:0"><input type="checkbox" id="ai-enable" checked> 启用 AI</label>
    </div>
  </div>

  <div class="card">
    <div class="loghead">
      <h2>运行日志</h2>
      <span class="spacer"></span>
      <button class="btn" id="clear" style="padding:4px 12px;font-size:12px">清空</button>
    </div>
    <pre class="log" id="log"></pre>
  </div>

  <footer>pairip 许可绕过 + Adapty 订阅解锁 · 仅供内部验证与竞品拆解 · <a href="http://127.0.0.1:__PORT__/" style="color:var(--muted)">刷新页面</a> · <a href="#" id="quit" style="color:var(--muted)">关闭工具</a></footer>
</div>
<script>
const logEl = document.getElementById('log');
let lastSeq = 0;

async function pollLogs() {
  try {
    const r = await fetch('/api/logs?since=' + lastSeq);
    const d = await r.json();
    for (const [seq, msg] of d.logs) {
      logEl.textContent += msg + '\\n';
      lastSeq = Math.max(lastSeq, seq);
    }
    logEl.scrollTop = logEl.scrollHeight;
  } catch (e) {}
}
setInterval(pollLogs, 1000);
pollLogs();

async function pollDevice() {
  try {
    const r = await fetch('/api/device');
    const d = await r.json();
    const b = document.getElementById('device');
    if (d.connected) { b.className = 'badge on'; b.innerHTML = '<span class="dot" style="background:var(--ok)"></span>设备已连接: ' + d.serial; }
    else { b.className = 'badge'; b.innerHTML = '<span class="dot"></span>未检测到设备（将只生成补丁包）'; }
  } catch (e) {}
}
setInterval(pollDevice, 3000);
pollDevice();

async function pollState() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    const btn = document.getElementById('start');
    const st = document.getElementById('state');
    if (d.running) { btn.disabled = true; btn.textContent = '处理中…'; st.innerHTML = '<span class="dot run"></span>运行中'; }
    else { btn.disabled = false; btn.textContent = '解锁并安装'; st.textContent = '空闲'; }
  } catch (e) {}
}
setInterval(pollState, 1500);
pollState();

document.getElementById('pick').onclick = async () => {
  try {
    const r = await fetch('/api/pick-file', { method: 'POST' });
    const d = await r.json();
    if (d.path) document.getElementById('path').value = d.path;
  } catch (e) {}
};

document.getElementById('start').onclick = async () => {
  const path = document.getElementById('path').value.trim();
  if (!path) { alert('先选择或输入 APK / XAPK 文件路径'); return; }
  document.getElementById('start').disabled = true;
  document.getElementById('start').textContent = '处理中…';
  try {
    await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, install: document.getElementById('install').checked, force_single: document.getElementById('force').checked })
    });
  } catch (e) {}
};

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    document.getElementById('ai-status').textContent = d.has_key ? ('DeepSeek ' + d.key + ' · 模型 ' + d.model) : '未配置';
    document.getElementById('ai-enable').checked = d.ai_enabled;
  } catch (e) {}
}
loadConfig();

document.getElementById('ai-save').onclick = async () => {
  const key = document.getElementById('ai-key').value.trim();
  if (!key) { alert('请输入 DeepSeek API Key'); return; }
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deepseek_key: key, ai_enabled: document.getElementById('ai-enable').checked })
    });
    document.getElementById('ai-key').value = '';
    loadConfig();
  } catch (e) {}
};

document.getElementById('ai-enable').onchange = async () => {
  try {
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_enabled: document.getElementById('ai-enable').checked })
    });
  } catch (e) {}
};

document.getElementById('clear').onclick = () => {
  logEl.textContent = '';
  fetch('/api/logs?since=' + (lastSeq + 1));
};

document.getElementById('quit').onclick = async () => {
  if (confirm('确定关闭工具？')) {
    try { await fetch('/api/shutdown', { method: 'POST' }); } catch (e) {}
    document.body.innerHTML = '<div style="text-align:center;padding-top:80px;color:#6b7280">工具已关闭，可以关闭此页面。</div>';
  }
};
</script>
</body>
</html>"""


# ---------------------------------------------------------------- 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "PremiumUnlocker/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path == "/":
            html = INDEX_HTML.replace("__VER__", pu.VERSION).replace("__PORT__", str(server_port()))
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/logs":
            since = int(qs.get("since", ["0"])[0])
            entries = [(s, m) for s, m in pu.LOG_BUFFER if s > since]
            self._json({"logs": entries[-500:], "max": pu.LOG_SEQ[0]})
        elif path == "/api/device":
            adb, serial = pu.get_device()
            self._json({"connected": bool(serial), "serial": serial or ""})
        elif path == "/api/state":
            self._json({"running": STATE["running"], "app": "premium-unlocker"})
        elif path == "/api/config":
            cfg = pu.ai_patch.load_config()
            key = cfg.get("deepseek_key", "")
            self._json({"key": "已配置 (%s)" % key[:6] if key else "",
                        "has_key": bool(key),
                        "model": cfg.get("model", pu.ai_patch.DEFAULT_MODEL),
                        "ai_enabled": cfg.get("ai_enabled", True)})
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/pick-file":
            try:
                p = pick_file()
                self._json({"path": p})
            except Exception as e:
                self._json({"path": "", "error": str(e)}, 500)
        elif path == "/api/start":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            if STATE["running"]:
                self._json({"ok": False, "error": "已有任务在运行"}, 409)
                return
            STATE["running"] = True
            threading.Thread(
                target=_run_task,
                args=(data.get("path", ""), bool(data.get("install", True)),
                      bool(data.get("force_single", False))),
                daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/config":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            cfg = pu.ai_patch.load_config()
            if "deepseek_key" in data and data["deepseek_key"]:
                cfg["deepseek_key"] = data["deepseek_key"]
            if "ai_enabled" in data:
                cfg["ai_enabled"] = bool(data["ai_enabled"])
            pu.ai_patch.save_config(cfg)
            self._json({"ok": True})
        elif path == "/api/shutdown":
            self._json({"ok": True})
            threading.Timer(0.5, lambda: os._exit(0)).start()
        else:
            self._send(404, "not found")

    def log_message(self, *args):
        pass


STATE = {"running": False}


def _run_task(path, install, force_single=False):
    try:
        pu.Unlocker(pu.emit_log).run(path, install, force_single)
    except Exception:
        pu.emit_log("发生异常:\n" + pu.traceback.format_exc())
    finally:
        STATE["running"] = False


# ---------------------------------------------------------------- 原生文件选择

_tk = None
_tk_queue = None


def pick_file():
    """在服务线程里通过 tkinter 弹原生文件对话框（跨线程用 after 转主线程）。"""
    global _tk, _tk_queue
    import tkinter as tk
    from tkinter import filedialog
    if _tk is None:
        _tk = tk.Tk()
        _tk.withdraw()
        _tk_queue = queue.Queue()
    def do():
        p = filedialog.askopenfilename(
            title="选择 APK / XAPK / APKPure ZIP",
            filetypes=[("Android 包", "*.apk *.xapk *.zip"), ("所有文件", "*.*")])
        _tk_queue.put(p or "")
    _tk.after(0, do)
    try:
        return _tk_queue.get(timeout=180)
    except queue.Empty:
        return ""


# ---------------------------------------------------------------- 启动

_server = None


def server_port():
    return _server.server_address[1] if _server else PORT_START


def _find_port():
    for port in range(PORT_START, PORT_START + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return PORT_START


def _probe_existing():
    """探测是否已有一个实例在跑；有则返回其端口。
    用裸 socket（不走系统代理，避免 Clash 等代理拖慢/卡死探测）。"""
    for port in range(PORT_START, PORT_START + 20):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.3)
            s.sendall(b"GET /api/state HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            s.settimeout(0.5)
            data = s.recv(2048).decode("utf-8", "replace")
            s.close()
            body = data.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in data else ""
            if json.loads(body).get("app") == "premium-unlocker":
                return port
        except Exception:
            continue
    return None


def run_server():
    global _server
    existing = _probe_existing()
    if existing:
        webbrowser.open("http://127.0.0.1:%d" % existing)
        os._exit(0)
    port = _find_port()
    _server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    pu.emit_log("服务已启动: http://127.0.0.1:%d" % port)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    webbrowser.open("http://127.0.0.1:%d" % port)
    # 文件对话框的 tk 环境放在最后初始化；失败不致命（页面可手动输入路径）
    try:
        ensure_tk_thread()
        _tk.mainloop()
    except Exception:
        pu.emit_log("（原生文件选择框不可用，请在页面手动输入路径）")
        while True:
            threading.Event().wait(3600)


def ensure_tk_thread():
    """提前初始化 tk（主线程），后续 pick_file 只做 after 投递。"""
    import tkinter as tk
    global _tk, _tk_queue
    _tk = tk.Tk()
    _tk.withdraw()
    _tk_queue = queue.Queue()
