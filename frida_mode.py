# -*- coding: utf-8 -*-
"""
Frida 动态 Hook 模式（第 3 层）。

适用：重打包失败的包（签名校验 / 加固壳 / VMP），或不想改包的场景。
原理：不改 APK，运行时用 Frida 把订阅 SDK 的判断方法 hook 成恒 true。

依赖策略（面向"同事零安装"）：
- frida Python 客户端：打包进 exe（build 脚本里 pip install frida），运行时代码 lazy import；
  若未打包，降级为「生成脚本 + 给出命令行」。
- frida-server（设备端）：按设备 ABI 自动从 GitHub 下载、解压、adb 推送、启动，无需手工。
- 设备需要 root（frida-server 的必要条件），工具会检测并提示。
"""
import os
import re
import json
import lzma
import shutil
import subprocess
import threading
import time
import urllib.request

import ai_patch

FRIDA_RELEASE_API = "https://api.github.com/repos/frida/frida/releases/latest"
FRIDA_SERVER_TMPL = ("https://github.com/frida/frida/releases/download/"
                     "{ver}/frida-server-{ver}-android-{arch}.xz")

# 每个 SDK 的 hook 表：类名 → 方法列表（实例/静态通用）
SDK_HOOKS = {
    "Adapty": [
        ("com.adapty.models.AdaptyProfile$AccessLevel", ["isActive", "getIsActive"]),
        ("com.adapty.models.AdaptyProfile$Subscription", ["isActive", "getIsActive"]),
    ],
    "RevenueCat": [
        ("com.revenuecat.purchases.EntitlementInfo", ["isActive"]),
    ],
    "Superwall": [
        ("com.superwall.sdk.models.subscription.SubscriptionStatus", ["isActive"]),
    ],
    "Qonversion": [
        ("com.qonversion.android.sdk.dto.entitlements.Entitlement", ["isActive"]),
    ],
    "Apphud": [
        ("com.apphud.sdk.Apphud", ["hasActiveSubscription"]),
        ("com.apphud.sdk.domain.ApphudUser", ["hasActiveSubscription"]),
    ],
    "Purchasely": [
        ("com.purchasely.sdk.models.PLYPlan", ["isSubscribed"]),
    ],
}

# 激进 hook：SharedPreferences / RemoteConfig 的 premium key 读点恒 true
PREFS_KEY_RE_JS = r"/premium|pro|vip|unlock|subscri|paid|entitle|active|adfree|ads/i"

_SCRIPT_TEMPLATE = """// 竞品订阅解锁 Frida 脚本（自动生成，{note}）
// 用法（手动）：frida -U -f {pkg} -l 本文件 --no-pause
Java.perform(function () {{
  var hooks = {hooks_json};

  hooks.forEach(function (h) {{
    try {{
      var cls = Java.use(h.cls);
      h.methods.forEach(function (m) {{
        try {{
          var found = false;
          (cls[m].overloads || []).forEach(function (o) {{
            o.implementation = function () {{
              console.log('[PU] ' + h.cls + '.' + m + ' -> true');
              return true;
            }};
            found = true;
          }});
          if (!found) {{ console.log('[PU] 方法不存在: ' + h.cls + '.' + m); }}
        }} catch (e) {{ console.log('[PU] 方法 hook 失败: ' + h.cls + '.' + m + ' ' + e); }}
      }});
    }} catch (e) {{ console.log('[PU] 类不存在，跳过: ' + h.cls); }}
  }});

  // 通用缓存层：SharedPreferences 读 premium key 恒 true
  try {{
    var SP = Java.use('android.app.SharedPreferencesImpl');
    SP.getBoolean.overload('java.lang.String', 'boolean').implementation = function (key, def) {{
      if ({prefs_re}.test(String(key))) {{
        console.log('[PU] SharedPreferences.getBoolean("' + key + '") -> true');
        return true;
      }}
      return this.getBoolean(key, def);
    }};
  }} catch (e) {{ console.log('[PU] SharedPreferences hook 失败: ' + e); }}

  // 通用缓存层：Firebase Remote Config 读 premium key 恒 true
  try {{
    var RC = Java.use('com.google.firebase.remoteconfig.FirebaseRemoteConfig');
    RC.getBoolean.overload('java.lang.String').implementation = function (key) {{
      if ({prefs_re}.test(String(key))) {{
        console.log('[PU] RemoteConfig.getBoolean("' + key + '") -> true');
        return true;
      }}
      return this.getBoolean(key);
    }};
  }} catch (e) {{ console.log('[PU] RemoteConfig hook 失败: ' + e); }}

  console.log('[PU] 全部 hook 注入完成。若某条类不存在属正常（SDK 未接入）。');
}});
"""


# ---------------------------------------------------------------- 脚本生成

def detect_from_apk(apk_path):
    """不解包直接扫 dex 找订阅 SDK（复用 ai_patch 的签名表）。"""
    return ai_patch.detect_sdks_from_apk(apk_path)


def _hooks_for_sdks(sdks):
    hooks = []
    for sdk in sdks:
        for cls, methods in SDK_HOOKS.get(sdk, []):
            hooks.append({"cls": cls, "methods": list(methods)})
    return hooks


def generate_script(sdks, pkg="<包名>", note="工具生成"):
    """生成 Frida JS 脚本内容。"""
    hooks = _hooks_for_sdks(sdks)
    return _SCRIPT_TEMPLATE.format(
        note=note, pkg=pkg or "<包名>",
        hooks_json=json.dumps(hooks, ensure_ascii=False),
        prefs_re=PREFS_KEY_RE_JS)


def write_script(sdks, pkg, out_path, log):
    js = generate_script(sdks, pkg)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(js)
    log("Frida 脚本已生成: %s（含 %d 个 SDK hook + 通用缓存 hook）"
        % (out_path, len(_hooks_for_sdks(sdks))))
    return out_path


# ---------------------------------------------------------------- 客户端运行

def find_frida_cli():
    return shutil.which("frida")


def frida_available():
    """frida Python 包是否可用（打包进 exe 时为 True）。"""
    try:
        import frida  # noqa: F401
        return True
    except Exception:
        return False


def frida_version():
    try:
        import frida
        return getattr(frida, "__version__", None)
    except Exception:
        return None


# 活跃 session 的全局持有（防止 GC 导致 hook 失效；stop_hook 时释放）
_ACTIVE_SESSION = None


def run_hook(pkg, script_path, log):
    """spawn 目标 app 并注入脚本。注入成功即返回 True（hook 持续生效，
    app 保持前台运行）；失败返回 False 并在日志给出原因。"""
    try:
        import frida
    except Exception:
        log("[Frida] 未找到 frida Python 包。"
            "手动方式：pip install frida-tools，然后跑：\n"
            "    frida -U -f %s -l \"%s\" --no-pause" % (pkg, script_path))
        return False
    try:
        log("[Frida] 连接 USB 设备 ...")
        device = frida.get_usb_device(timeout=10)
    except Exception as e:
        log("[Frida] 连接设备失败: %s。检查：USB 调试、frida-server 是否在设备上运行"
            "（本工具会自动部署，需 root）。" % e)
        return False
    try:
        log("[Frida] 启动 %s ..." % pkg)
        pid = device.spawn([pkg])
        session = device.attach(pid)
    except Exception as e:
        log("[Frida] 启动失败: %s（app 是否已安装？包名是否正确？）" % e)
        return False

    def on_message(message, data):
        if message.get("type") == "send":
            log("   [app] %s" % message.get("payload"))
        elif message.get("type") == "error":
            log("   [app][error] %s" % message.get("description"))

    try:
        with open(script_path, encoding="utf-8") as f:
            src = f.read()
        script = session.create_script(src)
        script.on("message", on_message)
        script.load()
        device.resume(pid)
    except Exception as e:
        log("[Frida] 脚本注入失败: %s" % e)
        try:
            session.detach()
        except Exception:
            pass
        return False
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = session
    log("[Frida] Hook 已注入（pid=%s），app 正在前台运行。关闭工具即结束 hook。" % pid)
    return True


def stop_hook():
    """结束 hook（关闭工具时调用）。"""
    global _ACTIVE_SESSION
    try:
        if _ACTIVE_SESSION:
            _ACTIVE_SESSION.detach()
    except Exception:
        pass
    _ACTIVE_SESSION = None


# ---------------------------------------------------------------- frida-server 设备端自动部署

def detect_arch(adb, serial):
    """设备 ABI → frida-server 的 arch 名。"""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    r = subprocess.run(cmd + ["shell", "getprop", "ro.product.cpu.abilist"],
                       capture_output=True, timeout=15)
    out = (r.stdout or b"").decode("utf-8", "replace")
    for abi, arch in (("arm64-v8a", "arm64"), ("armeabi-v7a", "arm"),
                      ("x86_64", "x86_64"), ("x86", "x86")):
        if abi in out:
            return arch
    return None


def _latest_release_tag(log):
    try:
        with urllib.request.urlopen(FRIDA_RELEASE_API, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data.get("tag_name")
    except Exception as e:
        log("[Frida] 获取 frida 最新版本失败: %s" % e)
        return None


def deploy_frida_server(adb, serial, log):
    """下载 → 解压 → 推送 → 启动 frida-server。成功返回 (True, 说明)。"""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    # 已运行则直接返回
    r = subprocess.run(cmd + ["shell", "ps", "-A"], capture_output=True, timeout=15)
    if b"frida-server" in (r.stdout or b""):
        log("[Frida] 设备上 frida-server 已在运行")
        return True, "已在运行"
    arch = detect_arch(adb, serial)
    if not arch:
        log("[Frida] 无法识别设备 ABI，跳过自动部署。"
            "请手动下载 frida-server 并推送到 /data/local/tmp/ 后启动。")
        return False, "未知 ABI"
    ver = _latest_release_tag(log)
    if not ver:
        return False, "获取版本失败"
    url = FRIDA_SERVER_TMPL.format(ver=ver, arch=arch)
    tmp_xz = os.path.join(tempdir(), "frida-server.xz")
    try:
        log("[Frida] 下载 frida-server %s (%s) ..." % (ver, arch))
        urllib.request.urlretrieve(url, tmp_xz)
        log("[Frida] 解压 ...")
        tmp_bin = os.path.join(tempdir(), "frida-server")
        with lzma.open(tmp_xz, "rb") as fi:
            with open(tmp_bin, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        os.remove(tmp_xz)
    except Exception as e:
        log("[Frida] 下载/解压失败: %s" % e)
        return False, "下载失败"
    remote = "/data/local/tmp/frida-server"
    log("[Frida] 推送到设备 ...")
    r = subprocess.run(cmd + ["push", tmp_bin, remote],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        log("[Frida] 推送失败: %s" % r.stderr.decode("utf-8", "replace")[-300:])
        return False, "推送失败"
    subprocess.run(cmd + ["shell", "chmod", "755", remote],
                   capture_output=True, timeout=15)
    log("[Frida] 启动 frida-server（需要 root）...")
    r = subprocess.run(
        cmd + ["shell", "su", "-c", "nohup %s > /dev/null 2>&1 &" % remote],
        capture_output=True, timeout=20)
    time.sleep(2)
    r2 = subprocess.run(cmd + ["shell", "ps", "-A"], capture_output=True, timeout=15)
    if b"frida-server" in (r2.stdout or b""):
        log("[Frida] frida-server 启动成功")
        return True, "启动成功"
    log("[Frida] 启动失败（设备未 root 或 su 不可用）。"
        "手动方式：adb shell → su → /data/local/tmp/frida-server")
    return False, "启动失败"


def tempdir():
    import tempfile
    return tempfile.gettempdir()
