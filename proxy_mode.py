# -*- coding: utf-8 -*-
"""
MITM 代理模式（第 4 层）：服务端 entitlement 的解锁方式。

适用：Air Tag Detector 这类「premium 状态由服务端下发、客户端无本地判断点」的 app，
改 APK 永远解不了，只能劫持 API 响应把 isPremium 改 true。

实现：生成 mitmproxy addon（JSON 响应深改）→ 启动 mitmdump → adb reverse +
设置设备代理。SSL Pinning 的 app 代理无效（需配合 Frida unpinning）。

依赖策略（面向"同事零安装"）：
- mitmdump 不在 PATH 时自动探测常见位置；都没有则给出一次性安装命令，不崩。
- 证书安装（设备端一次性）提供逐步指引。
"""
import os
import re
import json
import shutil
import subprocess
import threading

DEFAULT_PORT = 8099

# 默认改写字段（JSON key 命中即翻转）。注意不要放 status 这类宽泛词。
DEFAULT_PATTERNS = [
    "premium", "is_premium", "isPremium",
    "is_active", "isActive", "hasActiveSubscription", "has_active_subscription",
    "has_subscription", "isSubscribed", "is_subscribed", "subscriptionActive",
    "subscription_active", "entitlement", "entitlements",
    "accessLevel", "access_level", "is_trial", "isTrial",
    "subscription_status", "subscriptionStatus", "plan_status", "planStatus",
    "isPro", "is_pro", "vip", "unlocked", "isUnlocked", "remove_ads",
]

BAD_STR_VALUES = ("false", "inactive", "none", "expired", "trial", "0",
                  "canceled", "cancelled", "free", "never", "not_active")

ADDON_TEMPLATE = '''# -*- coding: utf-8 -*-
"""竞品订阅解锁 - 响应改写 addon（自动生成，勿手改）。

规则：JSON 响应里 key 命中 PATTERNS 的值会被翻转：
  bool false -> true / int 0 -> 1 / "inactive" 等坏状态 -> "active"
"""
import json
import re

PATTERNS = [re.compile(p, re.I) for p in {patterns!r}]
BAD = {bad!r}


def flip(key, value):
    if not isinstance(key, str):
        return value
    if not any(p.search(key) for p in PATTERNS):
        return value
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)) and value == 0:
        return 1
    if isinstance(value, str) and value.strip().lower() in BAD:
        return "active"
    return value


def walk(node, depth=0):
    if depth > 15:
        return node
    if isinstance(node, dict):
        return {{k: flip(k, walk(v, depth + 1)) for k, v in node.items()}}
    if isinstance(node, list):
        return [walk(v, depth + 1) for v in node]
    return node


def response(flow):
    try:
        ct = flow.response.headers.get("content-type", "")
        if "json" not in ct:
            return
        data = json.loads(flow.response.get_text())
        new = walk(data)
        if new != data:
            flow.response.set_text(json.dumps(new))
    except Exception:
        pass
'''


def make_addon(patterns, out_path):
    """生成 mitmproxy addon 文件。patterns: list[str]"""
    pats = [p for p in (patterns or []) if p and p.strip()]
    if not pats:
        pats = DEFAULT_PATTERNS
    content = ADDON_TEMPLATE.format(patterns=pats, bad=BAD_STR_VALUES)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


# ---------------------------------------------------------------- mitmdump 探测与运行

def find_mitmdump():
    """PATH → 常见安装位置。找不到返回 None。"""
    p = shutil.which("mitmdump")
    if p:
        return p
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Roaming", "Python",
                     "Python3*", "Scripts", "mitmdump.exe"),
        os.path.join(home, "AppData", "Local", "Programs", "Python",
                     "Python3*", "Scripts", "mitmdump.exe"),
    ]
    for c in candidates:
        import glob
        for g in glob.glob(c):
            if os.path.isfile(g):
                return g
    return None


INSTALL_GUIDE = (
    "未找到 mitmdump。一次性安装（需要网络，约 1 分钟）：\n"
    "  1. 打开 CMD 或 PowerShell\n"
    "  2. 运行: pip install mitmproxy\n"
    "  3. 装好后回到本页重新点「启动代理」\n"
    "（如果电脑没装过 Python：先在 python.org 下载安装，勾选 Add to PATH）")


_proc = None
_stop_event = None
_port = None


def is_running():
    return _proc is not None and _proc.poll() is None


def current_port():
    return _port


def start(port, addon_path, log):
    """启动 mitmdump。成功返回 True；失败/未安装返回 False 并给出指引。"""
    global _proc, _stop_event, _port
    if is_running():
        log("[代理] 已在运行")
        return True
    mitmdump = find_mitmdump()
    if not mitmdump:
        log("[代理] " + INSTALL_GUIDE)
        return False
    _stop_event = threading.Event()
    _port = port
    try:
        _proc = subprocess.Popen(
            [mitmdump, "--listen-host", "127.0.0.1", "-p", str(port),
             "-s", addon_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as e:
        log("[代理] 启动失败: %s" % e)
        return False
    log("[代理] mitmdump 启动中（端口 %d）..." % port)

    def _reader():
        for line in iter(_proc.stdout.readline, b""):
            try:
                log("   [mitm] " + line.decode("utf-8", "replace").rstrip())
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()
    threading.Timer(2.0, _check_started, args=(log, port)).start()
    return True


def _check_started(log, port):
    if _proc is not None and _proc.poll() is not None:
        log("[代理] mitmdump 启动失败（端口被占？换一个端口重试）")
    else:
        log("[代理] 运行中。下一步：设备设代理 + 装证书（见日志指引）")


def stop(log=None):
    global _proc, _stop_event
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None
    if _stop_event:
        _stop_event.set()
        _stop_event = None
    if log:
        log("[代理] 已停止")


# ---------------------------------------------------------------- 设备侧设置

def set_device_proxy(adb, serial, port, log):
    """adb reverse + 设备全局代理指向本机。"""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    r = subprocess.run(cmd + ["reverse", "tcp:%d" % port, "tcp:%d" % port],
                       capture_output=True, timeout=20)
    if r.returncode != 0:
        log("[代理] adb reverse 失败: %s" % r.stderr.decode("utf-8", "replace")[-200:])
        return False
    subprocess.run(cmd + ["shell", "settings", "put", "global", "http_proxy",
                          "127.0.0.1:%d" % port],
                   capture_output=True, timeout=20)
    log("[代理] 设备代理已指向本机 127.0.0.1:%d（adb reverse）" % port)
    return True


def clear_device_proxy(adb, serial, log):
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    subprocess.run(cmd + ["shell", "settings", "put", "global", "http_proxy", ":0"],
                   capture_output=True, timeout=20)
    subprocess.run(cmd + ["reverse", "--remove-all"], capture_output=True, timeout=20)
    if log:
        log("[代理] 设备代理已清除")


def cert_instructions(log):
    log("=" * 56)
    log("设备装证书（每个设备只需一次）：")
    log("  1. 证书已生成: %s/.mitmproxy/mitmproxy-ca-cert.cer" % os.path.expanduser("~"))
    log("  2. adb push 该文件到 /sdcard/（可用工具外命令，或直接把文件拷进手机）")
    log("  3. 手机设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书 → 选该文件")
    log("  4. 装好后重启目标 app 即可看到 premium 生效")
    log("注意：如果 app 做了 SSL Pinning，代理会失效——需配合 Frida 模式 unpinning")
    log("=" * 56)
