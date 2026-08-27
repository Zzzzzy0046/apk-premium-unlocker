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
import proxy_mode
import frida_mode

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

/* ---------- 阶段步进器 + 进度条 ---------- */
#stage-panel { display: none; }
#stage-panel.show { display: block; }
.steps { display: flex; align-items: flex-start; }
.step { flex: 1; text-align: center; position: relative; min-width: 0; }
.step .circle { width: 26px; height: 26px; line-height: 24px; border-radius: 50%; border: 2px solid #d1d5db; color: #9ca3af; font-size: 12px; font-weight: 600; background: #fff; margin: 0 auto; transition: all .3s; }
.step .label { font-size: 12px; color: var(--muted); margin-top: 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.step.done .circle { background: #dcfce7; border-color: var(--ok); color: var(--ok); }
.step.done .label { color: var(--ok); }
.step.run .circle { background: var(--accent); border-color: var(--accent); color: #fff; animation: pulse 1.2s infinite; }
.step.run .label { color: var(--accent); font-weight: 600; }
.step.failed .circle { background: #fee2e2; border-color: var(--err); color: var(--err); }
.step.failed .label { color: var(--err); }
.step.skipped .circle { background: #f3f4f6; border-color: #d1d5db; color: #9ca3af; }
.step.skipped .label { color: #9ca3af; text-decoration: line-through; }
.step .link { position: absolute; top: 13px; left: calc(50% + 15px); width: calc(100% - 30px); height: 2px; background: #e5e7eb; }
.step:last-child .link { display: none; }
.step.done .link { background: #86efac; }
@keyframes pulse { 50% { transform: scale(1.12); box-shadow: 0 0 0 5px rgba(37,99,235,.15); } }
.progress-wrap { margin-top: 14px; }
.progress-meta { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }
#stage-name { font-size: 13px; font-weight: 600; color: var(--text); }
#stage-pct { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.progress-track { height: 8px; background: #eef0f3; border-radius: 999px; overflow: hidden; }
.progress-bar { height: 100%; width: 0; background: var(--accent); border-radius: 999px; transition: width .6s ease; }
.progress-bar.indet { width: 30% !important; animation: slide 1.3s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
#stage-hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
#stage-hint.err { color: var(--err); font-weight: 500; }
#stage-hint.ok { color: var(--ok); }
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
    <label class="check"><input type="checkbox" id="frida"> Frida 模式（root 设备动态 hook，不重打包——重打包失败/加固包用这个）</label>
    <div class="row" id="frida-row" style="margin-top:10px; display:none">
      <input type="text" id="pkg" placeholder="包名（如 com.xxx.yyy），Frida 模式必填；xapk 会自动读取">
    </div>
    <div class="row" style="margin-top:16px">
      <button class="btn primary" id="start">解锁并安装</button>
      <span id="state" class="hint" style="margin-top:0">空闲</span>
    </div>
    <div class="hint">产物生成在安装包同目录 <b>xxx_unlocked/signed/</b>，可复装。首次运行会自动解压内置组件（约 30 秒）。</div>
  </div>

  <div class="card" id="stage-panel">
    <div class="loghead">
      <h2>处理进度</h2>
      <span class="spacer"></span>
      <span id="stage-pct" class="hint" style="margin-top:0"></span>
    </div>
    <div class="steps" id="steps"></div>
    <div class="progress-wrap">
      <div class="progress-meta">
        <span id="stage-name"></span>
        <span id="stage-elapsed"></span>
      </div>
      <div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div>
      <div id="stage-hint"></div>
    </div>
  </div>

  <div class="card">
    <div class="loghead">
      <h2>AI 订阅解锁（DeepSeek）</h2>
      <span class="spacer"></span>
      <span id="ai-status" class="hint" style="margin-top:0"></span>
    </div>
    <div class="hint" style="margin-bottom:10px">遇到确定性补丁覆盖不到的订阅 SDK 时，自动调用 DeepSeek 分析并生成补丁方案（每次约几毛钱）。Key 只保存在本机。</div>
    <div class="row">
      <input type="password" id="ai-key" placeholder="DeepSeek API Key（sk-...）">
      <button class="btn" id="ai-save">保存</button>
      <label class="check" style="margin:0"><input type="checkbox" id="ai-enable" checked> 启用 AI</label>
    </div>
  </div>

  <div class="card">
    <div class="loghead">
      <h2>代理模式（服务端 entitlement 专用）</h2>
      <span class="spacer"></span>
      <span id="proxy-status" class="hint" style="margin-top:0"></span>
    </div>
    <div class="hint" style="margin-bottom:10px">premium 状态由服务端下发、客户端无判断点的 app（改包无效），用 MITM 把 API 响应里的订阅字段改成已订阅。设备需装一次 CA 证书（启动后日志会给逐步指引）。</div>
    <div class="row">
      <input type="text" id="proxy-port" value="8099" style="max-width:100px" title="代理端口">
      <input type="text" id="proxy-patterns" placeholder="改写字段规则，英文逗号分隔（留空用默认：premium,isActive,entitlement…）">
    </div>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="proxy-start">启动代理</button>
      <button class="btn" id="proxy-stop">停止代理</button>
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

const STEP_ICONS = { done: '✓', failed: '✗', skipped: '—', pending: '•', running: '…' };

function fmtTime(sec) {
  if (sec == null) return '';
  const m = Math.floor(sec / 60), s = sec % 60;
  return (m > 0 ? m + ' 分 ' : '') + s + ' 秒';
}

function renderState(d) {
  const btn = document.getElementById('start');
  const panel = document.getElementById('stage-panel');
  const hint = document.getElementById('stage-hint');
  if (d.running) {
    btn.disabled = true;
    btn.textContent = '处理中…';
    panel.classList.add('show');
    // 阶段步进器
    const steps = document.getElementById('steps');
    let html = '';
    d.stage_names.forEach((name, i) => {
      const st = d.stage_status[i];
      html += '<div class="step ' + st + '"><div class="link"></div><div class="circle">' +
              STEP_ICONS[st] + '</div><div class="label">' + name + '</div></div>';
    });
    steps.innerHTML = html;
    // 进度条：有数值百分比则按宽度，否则动画条
    const bar = document.getElementById('progress-bar');
    const hasPct = (d.stage_status[d.stage_idx] === 'running' && d.progress > 0) ||
                   d.stage_status[d.stage_idx] === 'done';
    if (hasPct) {
      bar.classList.remove('indet');
      bar.style.width = (d.progress > 0 ? d.progress : 100) + '%';
    } else {
      bar.classList.add('indet');
      bar.style.width = '';
    }
    document.getElementById('stage-pct').textContent =
      hasPct && d.stage_status[d.stage_idx] === 'running' ? d.progress + '%' : '';
    document.getElementById('stage-name').textContent =
      '阶段 ' + (d.stage_idx + 1) + '/' + d.stage_names.length + '：' + d.stage_names[d.stage_idx];
    document.getElementById('stage-elapsed').textContent =
      '已用时 ' + fmtTime(d.stage_elapsed) + ' / 总计 ' + fmtTime(d.elapsed);
    hint.className = 'hint';
    hint.textContent = '';
  } else {
    btn.disabled = false;
    btn.textContent = '解锁并安装';
    panel.classList.toggle('show', d.finished !== null);
    hint.className = 'hint';
    if (d.finished === 'ok') {
      hint.classList.add('ok');
      hint.textContent = '✔ 全部完成（耗时 ' + fmtTime(d.elapsed) + '），补丁包在 xxx_unlocked/signed/';
    } else if (d.finished === 'failed') {
      hint.classList.add('err');
      hint.textContent = '✗ ' + d.error;
    } else {
      hint.textContent = '';
    }
  }
}

async function pollState() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    renderState(d);
  } catch (e) {}
}
setInterval(pollState, 1000);
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
  const frida = document.getElementById('frida').checked;
  const pkg = document.getElementById('pkg').value.trim();
  if (frida && !pkg && !/\.(xapk|zip)$/i.test(path)) {
    alert('Frida 模式需要包名，请在输入框填写（如 com.xxx.yyy）');
    return;
  }
  document.getElementById('start').disabled = true;
  document.getElementById('start').textContent = '处理中…';
  try {
    await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, install: document.getElementById('install').checked, force_single: document.getElementById('force').checked, frida: frida, pkg: pkg })
    });
  } catch (e) {}
};

document.getElementById('frida').onchange = () => {
  document.getElementById('frida-row').style.display =
    document.getElementById('frida').checked ? 'flex' : 'none';
};

async function pollProxy() {
  try {
    const r = await fetch('/api/proxy/status');
    const d = await r.json();
    const el = document.getElementById('proxy-status');
    if (d.running) { el.textContent = '运行中（端口 ' + d.port + '）'; el.style.color = 'var(--ok)'; }
    else { el.textContent = '未启动'; el.style.color = 'var(--muted)'; }
  } catch (e) {}
}
setInterval(pollProxy, 2000);
pollProxy();

document.getElementById('proxy-start').onclick = async () => {
  const port = document.getElementById('proxy-port').value.trim();
  const patterns = document.getElementById('proxy-patterns').value.trim();
  try {
    await fetch('/api/proxy/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port: port, patterns: patterns })
    });
  } catch (e) {}
};

document.getElementById('proxy-stop').onclick = async () => {
  try { await fetch('/api/proxy/stop', { method: 'POST' }); } catch (e) {}
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
    server_version = "PremiumUnlocker/" + pu.VERSION

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
            self._json(pu.get_state_snapshot())
        elif path == "/api/proxy/status":
            self._json({"running": proxy_mode.is_running(),
                        "port": proxy_mode.current_port() or proxy_mode.DEFAULT_PORT})
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
            if pu.get_state_snapshot()["running"]:
                self._json({"ok": False, "error": "已有任务在运行"}, 409)
                return
            threading.Thread(
                target=_run_task,
                args=(data.get("path", ""), bool(data.get("install", True)),
                      bool(data.get("force_single", False)),
                      bool(data.get("frida", False)),
                      (data.get("pkg") or "").strip() or None),
                daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/proxy/start":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                data = {}
            try:
                port = int(data.get("port") or proxy_mode.DEFAULT_PORT)
            except (TypeError, ValueError):
                port = proxy_mode.DEFAULT_PORT
            port = max(1024, min(65535, port))
            pats_raw = (data.get("patterns") or "").strip()
            pats = [p.strip() for p in pats_raw.split(",") if p.strip()] \
                if pats_raw else list(proxy_mode.DEFAULT_PATTERNS)
            addon = os.path.join(os.path.dirname(os.path.dirname(pu.LOG_FILE)),
                                 "proxy_addon.py")
            proxy_mode.make_addon(pats, addon)
            pu.emit_log("[代理] 改写规则已生成: %s（%d 条）" % (addon, len(pats)))
            ok = proxy_mode.start(port, addon, pu.emit_log)
            if ok:
                adb, serial = pu.get_device()
                if adb and serial:
                    proxy_mode.set_device_proxy(adb, serial, port, pu.emit_log)
                    proxy_mode.cert_instructions(pu.emit_log)
                else:
                    pu.emit_log("[代理] 未检测到设备：代理已启动。可手动设设备全局代理"
                                " http_proxy=127.0.0.1:%d（需先 adb reverse）" % port)
            self._json({"ok": ok})
        elif path == "/api/proxy/stop":
            proxy_mode.stop(pu.emit_log)
            adb, serial = pu.get_device()
            if adb and serial:
                proxy_mode.clear_device_proxy(adb, serial, pu.emit_log)
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

            def _bye():
                try:
                    proxy_mode.stop()
                    frida_mode.stop_hook()
                except Exception:
                    pass
                os._exit(0)

            threading.Timer(0.5, _bye).start()
        else:
            self._send(404, "not found")

    def log_message(self, *args):
        pass


def _run_task(path, install, force_single=False, frida=False, pkg=None):
    try:
        pu.Unlocker(pu.emit_log).run(path, install, force_single,
                                     pu.report_progress, frida=frida, pkg=pkg)
    except Exception:
        pu.emit_log("发生异常:\n" + pu.traceback.format_exc())
        pu._finish_state(False)


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
