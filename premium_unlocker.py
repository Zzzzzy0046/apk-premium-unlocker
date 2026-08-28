# -*- coding: utf-8 -*-
"""
竞品订阅解锁工具 (Adapty Premium Unlocker)
==========================================
一键完成：解包 → pairip 绕过 → Adapty 订阅解锁 → 重打包 → 重签 → 安装 → 验证。

用法：选择 APK / XAPK / APKPure ZIP，点「解锁并安装」。
产物：输入文件同目录 <名字>_unlocked/signed/ 下为重签好的包（可复装）。
"""
import os
import re
import sys
import json
import queue
import struct
import shutil
import tempfile
import threading
import traceback
import subprocess
import time
import urllib.request
import zipfile
from datetime import datetime

import ai_patch
import sdk_patches
import frida_mode

VERSION = "1.3"
APKTOOL_URL = "https://github.com/iBotPeaches/Apktool/releases/download/v2.9.3/apktool_2.9.3.jar"
APKTOOL_FILENAME = "apktool.jar"
LOG_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "PremiumUnlocker", "logs", "latest.log")


def resource_dir():
    """内置运行时资源目录。PyInstaller 打包后 = _internal/runtime；开发模式 = 项目 runtime/"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, "runtime")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")


INSTALLED_RUNTIME = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PremiumUnlocker", "runtime")
PAYLOAD_MAGIC = b"PUZL"
_RUNTIME_MARKER = ("jre", "bin", "java.exe")


def _runtime_ready(path):
    return bool(path) and os.path.isfile(os.path.join(path, *_RUNTIME_MARKER))


def _extract_payload(log=None):
    """从 exe 尾部提取内置 runtime 到 %LOCALAPPDATA%（首次运行一次）。"""
    exe = getattr(sys, "executable", None)
    if not exe or not os.path.isfile(exe):
        return False
    try:
        with open(exe, "rb") as f:
            f.seek(-12, os.SEEK_END)
            tail = f.read(12)
            if tail[:4] != PAYLOAD_MAGIC:
                return False
            offset = struct.unpack("<Q", tail[4:12])[0]
            size = f.tell() - offset - 12
            f.seek(offset)
            tmp_zip = os.path.join(tempfile.gettempdir(), "puzl_runtime_payload.zip")
            with open(tmp_zip, "wb") as out:
                shutil.copyfileobj(f, out, size)
        if log:
            log("首次运行：解压内置组件到 %s ..." % INSTALLED_RUNTIME)
        os.makedirs(os.path.dirname(INSTALLED_RUNTIME), exist_ok=True)
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(os.path.dirname(INSTALLED_RUNTIME))
        os.remove(tmp_zip)
        return True
    except Exception:
        return False


def ensure_runtime(log=None):
    """确保内置运行时可用；必要时从 exe 尾部解压。"""
    if _runtime_ready(INSTALLED_RUNTIME):
        return INSTALLED_RUNTIME
    if _extract_payload(log) and _runtime_ready(INSTALLED_RUNTIME):
        return INSTALLED_RUNTIME
    r = resource_dir()
    return r if _runtime_ready(r) else None


def write_log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# 全局日志缓冲（Web UI 轮询读取）
LOG_LOCK = threading.Lock()
LOG_BUFFER = []
LOG_SEQ = [0]


def emit_log(msg):
    """写日志文件 + 进全局缓冲（Web UI 用）。"""
    write_log(msg)
    with LOG_LOCK:
        LOG_SEQ[0] += 1
        LOG_BUFFER.append((LOG_SEQ[0], msg))
        if len(LOG_BUFFER) > 2000:
            del LOG_BUFFER[:1000]


# ---------------------------------------------------------------- 运行状态（Web UI 步进器）

# 主流程阶段（key, 显示名）。set_stage 只允许推进，错误时 mark_failed。
STAGES = [
    ("init", "初始化"),
    ("unpack", "解包"),
    ("decompile", "反编译"),
    ("patch", "补丁"),
    ("build", "重打包"),
    ("sign", "签名"),
    ("install", "安装"),
    ("verify", "验证"),
]

# Frida 模式的阶段（不反编译/重打包，走动态 hook）
FRIDA_STAGES = [
    ("init", "初始化"),
    ("unpack", "解包"),
    ("detect", "检测 SDK"),
    ("script", "生成脚本"),
    ("deploy", "部署 frida-server"),
    ("run", "注入运行"),
    ("verify", "验证"),
]

STATE = None          # None = 空闲；dict = 一次运行的状态快照
_STATE_LOCK = threading.Lock()
_STATE_LOG = None     # 当前运行的日志函数（set_stage 时打印阶段提示）


def _default_state_log(msg):
    emit_log(msg)


def get_state_snapshot():
    """线程安全地返回当前状态副本。空闲时返回 idle 快照。"""
    with _STATE_LOCK:
        if not STATE:
            return {"app": "premium-unlocker", "running": False, "finished": None,
                    "stage": None, "stage_idx": 0,
                    "stage_names": [n for _, n in STAGES],
                    "stage_status": ["pending"] * len(STAGES),
                    "progress": 0, "elapsed": 0, "stage_elapsed": 0, "error": None}
        now = time.time()
        stage_idx = STATE["stage_idx"]
        st = {
            "app": "premium-unlocker",
            "running": STATE["running"],
            "finished": STATE.get("finished"),
            "stage": STATE["stage_keys"][stage_idx],
            "stage_idx": stage_idx,
            "stage_names": list(STATE["stage_names"]),
            "stage_status": [STATE["stages"][k] for k in STATE["stage_keys"]],
            "progress": STATE["progress"],
            "elapsed": max(0, int(now - STATE["start"])),
            "stage_elapsed": max(0, int(now - STATE.get("stage_start", STATE["start"]))),
            "error": STATE.get("error"),
        }
        return st


def _reset_state(stages=None):
    global STATE
    with _STATE_LOCK:
        st = list(stages) if stages else STAGES
        STATE = {
            "running": True,
            "finished": None,
            "stage_idx": 0,
            "stage_start": time.time(),
            "start": time.time(),
            "stage_keys": [k for k, _ in st],
            "stage_names": [n for _, n in st],
            "stages": {k: "pending" for k, _ in st},
            "progress": 0,
            "error": None,
        }


def set_stage(stage_key, log=None):
    """推进到指定阶段。之前的阶段标记为 done（除非已 failed/skipped）。"""
    global STATE, _STATE_LOG
    if log is not None:
        _STATE_LOG = log
    if not STATE:
        _reset_state()
    with _STATE_LOCK:
        try:
            idx = STATE["stage_keys"].index(stage_key)
        except ValueError:
            return
        # 当前阶段（若仍 running/pending）→ done；中间阶段 → done；目标阶段 → running
        cur = STATE["stage_keys"][STATE["stage_idx"]]
        if STATE["stage_idx"] != idx and STATE["stages"].get(cur) in ("running", "pending"):
            STATE["stages"][cur] = "done"
        for i in range(STATE["stage_idx"] + 1, idx):
            k = STATE["stage_keys"][i]
            if STATE["stages"][k] == "pending":
                STATE["stages"][k] = "done"
        STATE["stages"][stage_key] = "running"
        STATE["stage_idx"] = idx
        STATE["stage_start"] = time.time()
        STATE["progress"] = 0
        total = len(STATE["stage_keys"])
        name = STATE["stage_names"][idx]
    (log or _STATE_LOG or _default_state_log)("[阶段 %d/%d] %s" % (idx + 1, total, name))


def mark_stage(stage_key, status):
    """把某阶段标记为 done / failed / skipped。"""
    global STATE
    with _STATE_LOCK:
        if not STATE:
            return
        STATE["stages"][stage_key] = status
        if status == "failed" and not STATE["error"]:
            STATE["error"] = stage_key


def report_progress(pct):
    """当前阶段进度 0-100（如 apktool 无法给进度，UI 显示动画）。"""
    global STATE
    with _STATE_LOCK:
        if STATE:
            STATE["progress"] = max(0, min(100, int(pct)))


def _set_error(msg):
    """记录错误文案（UI 状态栏显示）。"""
    global STATE
    with _STATE_LOCK:
        if STATE:
            STATE["error"] = msg


def _finish_state(ok):
    global STATE
    with _STATE_LOCK:
        if not STATE:
            return
        keys = STATE["stage_keys"]
        names = STATE["stage_names"]
        cur = keys[STATE["stage_idx"]]
        if ok:
            if STATE["stages"].get(cur) == "running":
                STATE["stages"][cur] = "done"
            STATE["finished"] = "ok"
        else:
            # 找到 failed 的阶段名生成错误信息；当前阶段仍 running 则标记 failed
            failed_name = None
            for k, n in zip(keys, names):
                if STATE["stages"].get(k) == "failed":
                    failed_name = n
                    break
            if STATE["stages"].get(cur) == "running":
                STATE["stages"][cur] = "failed"
                failed_name = failed_name or names[STATE["stage_idx"]]
            if not STATE["error"]:
                STATE["error"] = "错误：" + (failed_name or names[STATE["stage_idx"]]) + "阶段失败"
            STATE["finished"] = "failed"
        STATE["running"] = False

# ---------------------------------------------------------------- 工具定位

# Windows 下子进程一律不弹控制台窗口（adb/java 等控制台子程序的黑框来源）
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _decode(b):
    """子进程输出解码：utf-8 失败回退 gbk（中文 Windows 下 adb 等输出 GBK）。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _run(cmd, timeout=None, text=True, check=False):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                       creationflags=NO_WINDOW, check=check)
    r.stdout = _decode(r.stdout)
    r.stderr = _decode(r.stderr)
    return r


def _which(name):
    """在 PATH 中找可执行文件。"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _first_existing(candidates):
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _sdk_dir():
    home = os.path.expanduser("~")
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v and os.path.isdir(v):
            return v
    local = os.path.join(home, "AppData", "Local", "Android", "Sdk")
    if os.path.isdir(local):
        return local
    return None


def find_adb():
    rt = ensure_runtime()
    bundled = os.path.join(rt, "adb", "adb.exe") if rt else None
    if bundled and os.path.isfile(bundled):
        return bundled
    sdk = _sdk_dir()
    if sdk:
        p = os.path.join(sdk, "platform-tools", "adb.exe")
        if os.path.isfile(p):
            return p
    return _which("adb.exe") or _which("adb")


def find_apksigner():
    rt = ensure_runtime()
    bundled = os.path.join(rt, "apksigner.jar") if rt else None
    if bundled and os.path.isfile(bundled):
        return bundled
    sdk = _sdk_dir()
    if sdk:
        bt = os.path.join(sdk, "build-tools")
        if os.path.isdir(bt):
            vers = sorted(os.listdir(bt), reverse=True)
            for v in vers:
                p = os.path.join(bt, v, "apksigner.bat")
                if os.path.isfile(p):
                    return p
    return None


def find_apktool(log):
    rt = ensure_runtime()
    bundled = os.path.join(rt, APKTOOL_FILENAME) if rt else None
    if bundled and os.path.isfile(bundled):
        return bundled
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), APKTOOL_FILENAME),
        os.path.join(home, APKTOOL_FILENAME),
        os.path.join(home, "tools", APKTOOL_FILENAME),
        os.path.join(home, "Desktop", APKTOOL_FILENAME),
    ]
    p = _first_existing(candidates)
    if p:
        # 校验 jar 有效性
        try:
            r = _run([find_java(), "-jar", p, "--version"], timeout=60)
            if r.returncode == 0:
                return p
        except Exception:
            pass
    # 下载
    log("未找到可用的 apktool，从 GitHub 下载（约 22MB）...")
    dest = os.path.join(home, APKTOOL_FILENAME)
    try:
        urllib.request.urlretrieve(APKTOOL_URL, dest)
        r = _run([find_java(), "-jar", dest, "--version"], timeout=60)
        if r.returncode == 0:
            log("apktool 下载完成: %s" % dest)
            return dest
    except Exception as e:
        log("下载失败: %s" % e)
    return None


def find_java():
    rt = ensure_runtime()
    bundled = os.path.join(rt, "jre", "bin", "java.exe") if rt else None
    if bundled and os.path.isfile(bundled):
        return bundled
    return _which("java.exe") or _which("java")


def ensure_debug_keystore(log):
    """优先用内置 keystore，缺失则生成。"""
    rt = ensure_runtime()
    bundled = os.path.join(rt, "debug.keystore") if rt else None
    if bundled and os.path.isfile(bundled):
        return bundled
    home = os.path.expanduser("~")
    ks = os.path.join(home, ".android", "debug.keystore")
    if os.path.isfile(ks):
        return ks
    log("生成 debug keystore ...")
    os.makedirs(os.path.dirname(ks), exist_ok=True)
    kt = os.path.join(resource_dir(), "jre", "bin", "keytool.exe") if resource_dir() else None
    if not kt or not os.path.isfile(kt):
        kt = _which("keytool.exe") or "keytool"
    _run([
        kt, "-genkeypair", "-v", "-keystore", ks,
        "-storepass", "android", "-alias", "androiddebugkey",
        "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048",
        "-validity", "10000",
        "-dname", "CN=Android Debug,O=Android,C=US",
    ], timeout=120)
    return ks


# ---------------------------------------------------------------- 设备

def get_device():
    adb = find_adb()
    if not adb:
        return None, None
    try:
        r = _run([adb, "devices"], timeout=15)
        lines = [l.split()[0] for l in r.stdout.strip().splitlines()[1:]
                 if l.strip() and l.split()[-1] == "device"]
        return adb, (lines[0] if lines else None)
    except Exception:
        return None, None


def device_props(adb, serial):
    def prop(name):
        cmd = [adb]
        if serial:
            cmd += ["-s", serial]
        cmd += ["shell", "getprop", name]
        try:
            r = _run(cmd, timeout=15)
            return r.stdout.strip()
        except Exception:
            return ""
    abilist = prop("ro.product.cpu.abilist")
    density = prop("ro.sf.lcd_density")
    return abilist, density


def pick_splits(splits, abilist, density):
    """按设备 ABI/密度/语言挑选 split。splits: {id: path}"""
    chosen = []
    # ABI
    for abi, want in (("arm64_v8a", "arm64-v8a" in abilist),
                      ("armeabi_v7a", "armeabi-v7a" in abilist),
                      ("x86_64", "x86_64" in abilist),
                      ("x86", "x86" in abilist.split(",")[0] if abilist else False)):
        if want and ("config." + abi) in splits:
            chosen.append(splits["config." + abi])
            break
    # 密度
    try:
        d = int(density)
        dpi = ("xxxhdpi" if d >= 560 else "xxhdpi" if d >= 420 else "xhdpi" if d >= 300
               else "hdpi" if d >= 210 else "mdpi" if d >= 140 else "ldpi")
        if ("config." + dpi) in splits:
            chosen.append(splits["config." + dpi])
    except Exception:
        pass
    # 语言：en + zh
    for lang in ("en", "zh"):
        if ("config." + lang) in splits:
            chosen.append(splits["config." + lang])
    return chosen


# ---------------------------------------------------------------- 解包

def unpack_input(path, workdir, log, progress=None):
    """返回 (base_apk, splits_dict)。splits_dict: {id: path}"""
    low = path.lower()
    if low.endswith((".xapk", ".zip")):
        log("解包 %s ..." % os.path.basename(path))
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            total = len(names)
            for i, n in enumerate(names, 1):
                z.extract(n, workdir)
                if progress and (i == total or i % max(1, total // 20) == 0):
                    progress(int(i * 100 / total))
        manifest = os.path.join(workdir, "manifest.json")
        base = None
        if os.path.isfile(manifest):
            with open(manifest, encoding="utf-8") as f:
                mj = json.load(f)
            for s in mj.get("split_apks", []):
                if s.get("id") == "base":
                    base = os.path.join(workdir, s["file"])
        if not base:
            apks = [f for f in os.listdir(workdir) if f.endswith(".apk")]
            base = max((os.path.join(workdir, f) for f in apks),
                       key=os.path.getsize, default=None)
        splits = {}
        for f in os.listdir(workdir):
            if f.startswith("config.") and f.endswith(".apk"):
                splits[f[:-4]] = os.path.join(workdir, f)
        log("base: %s，split 数: %d" % (os.path.basename(base) if base else "无", len(splits)))
        if progress:
            progress(100)
        return base, splits
    else:
        dest = os.path.join(workdir, "base.apk")
        shutil.copyfile(path, dest)
        log("单 APK 输入: %s" % os.path.basename(path))
        if progress:
            progress(100)
        return dest, {}


# ---------------------------------------------------------------- 反编译 / 打包

def run_cmd(cmd, log, timeout=None):
    log(">> " + " ".join(cmd) if len(" ".join(cmd)) < 200 else ">> " + cmd[0] + " ...")
    try:
        r = _run(cmd, timeout=timeout)
        if r.stdout.strip():
            for line in r.stdout.strip().splitlines()[-8:]:
                log("   " + line)
        if r.returncode != 0:
            for line in r.stderr.strip().splitlines()[-8:]:
                log("   [err] " + line)
            return False
        return True
    except subprocess.TimeoutExpired:
        log("   [超时]")
        return False
    except Exception as e:
        log("   [异常] %s" % e)
        return False


def _run_stream(cmd, log, timeout=None):
    """流式运行子进程：逐行实时 log（apktool 进度可见），同时收集完整输出。
    返回 (returncode, stdout+stderr 合并文本)。"""
    echo = ("I:", "W:", "error", "Built apk", "Baksmaling", "Smaling",
            "Checking", "Building", "Decoding", "not found")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=NO_WINDOW)
    out = []
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip()
        if line:
            out.append(line)
            if any(k in line for k in echo):
                log("   " + line[:120])
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        log("   [超时]")
        return -1, "\n".join(out)
    return rc, "\n".join(out)


def decompile(apktool, base_apk, outdir, log):
    log("apktool 反编译（大包需要几分钟）...")
    rc, out = _run_stream([find_java(), "-Xmx4g", "-jar", apktool, "d", base_apk,
                           "-o", outdir, "-f"], log, timeout=900)
    if rc == 0:
        return True
    for line in out.splitlines()[-6:]:
        log("   [err] " + line[-140:])
    return False


def _inject_missing_attrs(outdir, names, log):
    """apktool 对 material 1.10+ 的 state_* 属性解码时会丢定义，重打包报
    "attribute X not found"。把缺失的 attr 补进 attrs.xml（默认 boolean）。"""
    attrs = os.path.join(outdir, "res", "values", "attrs.xml")
    if not os.path.isfile(attrs):
        return False
    with open(attrs, encoding="utf-8") as f:
        content = f.read()
    added = []
    for n in names:
        if re.search(r'<attr name="%s"' % re.escape(n), content):
            continue
        content = content.replace("</resources>",
                                  '    <attr name="%s" format="boolean"/>\n</resources>' % n, 1)
        added.append(n)
    if added:
        with open(attrs, "w", encoding="utf-8") as f:
            f.write(content)
        log("已补充 apktool 丢失的属性定义: %s" % ", ".join(sorted(added)))
    return bool(added)


def build(apktool, outdir, repack, log, progress=None):
    log("apktool 重打包 ...")
    for attempt in range(6):
        rc, out = _run_stream([find_java(), "-Xmx4g", "-jar", apktool, "b", outdir,
                               "-o", repack], log, timeout=900)
        if rc == 0:
            log("   已生成: %s" % repack)
            if progress:
                progress(100)
            return True
        # 补丁写出非法 smali → 回滚 AI 补丁 + 确定性补丁后重试（pairip/资源补丁不受影响）
        if "Could not smali" in out or "no viable alternative" in out:
            n = ai_patch.rollback(log) + sdk_patches.rollback(log)
            if n:
                log("[资源修复] 已回滚 %d 个补丁文件，重试重打包" % n)
                continue
        missing = set(re.findall(r'attribute\s+([a-zA-Z0-9_]+)\s+\(aka', out))
        if missing and _inject_missing_attrs(outdir, missing, log):
            log("[资源修复] 补 %d 个缺失属性，重试 %d/%d" % (len(missing), attempt + 1, 6))
            continue
        if attempt < 1:
            log("重打包失败（可能为一次性错误），重试一次 ...")
            continue
        for line in out.splitlines()[-6:]:
            log("   [err] " + line[-140:])
        return False
    return False


# ---------------------------------------------------------------- patch: pairip

def patch_pairip(outdir, log):
    manifest = os.path.join(outdir, "AndroidManifest.xml")
    if not os.path.isfile(manifest):
        log("[pairip] 未找到 AndroidManifest.xml，跳过")
        return
    with open(manifest, encoding="utf-8") as f:
        content = f.read()

    if "com.pairip.application.Application" in content:
        # 形态 A：Application 包装 → 找 .super 换回业务 Application
        target = None
        for root, _, files in os.walk(outdir):
            for fn in files:
                if fn == "Application.smali" and "pairip" in root.replace(os.sep, "/"):
                    p = os.path.join(root, fn)
                    with open(p, encoding="utf-8") as f:
                        txt = f.read()
                    m = re.search(r"\.super\s+(L[^;]+;)", txt)
                    if m:
                        target = m.group(1)[1:-1].replace("/", ".")
                        break
            if target:
                break
        if target:
            content = content.replace("com.pairip.application.Application", target)
            with open(manifest, "w", encoding="utf-8") as f:
                f.write(content)
            log("[pairip] 形态A：application 已替换为 %s" % target)
        else:
            log("[pairip] 形态A：未找到业务 Application，跳过（风险：app 仍可能被拦）")

    if "com.pairip.licensecheck.LicenseContentProvider" in content:
        # 形态 B：Provider 触发 → 删掉 provider 声明
        new = re.sub(
            r"<provider[^>]*com\.pairip\.licensecheck\.LicenseContentProvider[^>]*>(?:(?!</provider>).)*?</provider>|<provider[^>]*com\.pairip\.licensecheck\.LicenseContentProvider[^>]*/>",
            "", content, flags=re.S)
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(new)
        log("[pairip] 形态B：LicenseContentProvider 已删除")

    if "pairip" not in content:
        log("[pairip] 未检测到 pairip")


def patch_force_single(outdir, log):
    """强制单包模式：删除 manifest 的 splits-required 声明，让 base 单装。
    警告：AAB 拆分的原生库/资源在 split 里，单装可能崩溃，仅作兜底。"""
    manifest = os.path.join(outdir, "AndroidManifest.xml")
    if not os.path.isfile(manifest):
        return
    with open(manifest, encoding="utf-8") as f:
        content = f.read()
    if "com.android.vending.splits.required" not in content:
        log("[单包] 未发现 splits-required 声明")
        return
    new = re.sub(r"\s*<meta-data\s+android:name=\"com\.android\.vending\.splits\.required\"[^>]*/>",
                 "", content)
    with open(manifest, "w", encoding="utf-8") as f:
        f.write(new)
    log("[单包] 已删除 splits-required 声明，base 可单装")
    lib_dir = os.path.join(outdir, "lib")
    has_lib = any(fn.endswith(".so") for root, _, files in os.walk(lib_dir)
                  for fn in files) if os.path.isdir(lib_dir) else False
    if not has_lib:
        log("[单包] ⚠ 警告：base 内无原生库。若该 app 有 JNI（广告 SDK/引擎），"
            "单装后可能崩溃，强烈建议改用 xapk 完整包重跑")


# ---------------------------------------------------------------- 签名 / 安装 / 验证

def sign_all(apksigner, keystore, files, log, progress=None):
    ok = True
    total = len(files)
    for i, f in enumerate(files, 1):
        log("签名 %s ..." % os.path.basename(f))
        if apksigner.lower().endswith(".jar"):
            base_cmd = [find_java(), "-jar", apksigner, "sign"]
        else:
            base_cmd = [apksigner, "sign"]
        r = run_cmd(base_cmd + ["--ks", keystore,
                                "--ks-pass", "pass:android",
                                "--ks-key-alias", "androiddebugkey",
                                "--key-pass", "pass:android", f],
                    log, timeout=300)
        ok = ok and r
        if progress:
            progress(int(i * 100 / total))
    return ok


def get_package_name(outdir):
    manifest = os.path.join(outdir, "AndroidManifest.xml")
    if os.path.isfile(manifest):
        with open(manifest, encoding="utf-8") as f:
            m = re.search(r'package="([^"]+)"', f.read())
            if m:
                return m.group(1)
    yml = os.path.join(outdir, "apktool.yml")
    if os.path.isfile(yml):
        with open(yml, encoding="utf-8") as f:
            m = re.search(r"renameManifestPackage:\s*(\S+)", f.read())
            if m:
                return m.group(1)
    return None


def install(adb, serial, pkg, signed_dir, log, progress=None):
    files = sorted(os.path.join(signed_dir, f) for f in os.listdir(signed_dir)
                   if f.endswith(".apk"))
    if not files:
        log("signed 目录没有 APK")
        return False
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["uninstall", pkg]
    _run(cmd, timeout=120)
    log("卸载旧版完成（如存在）")
    if progress:
        progress(20)
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["install-multiple"] + files
    log("安装 %d 个 APK ..." % len(files))
    ok = run_cmd(cmd, log, timeout=600)
    if progress:
        progress(ok and 100 or 90)
    if not ok:
        r2 = _run(cmd, timeout=600)
        out = (r2.stdout or "") + (r2.stderr or "")
        if "MISSING_SPLIT" in out:
            log("=" * 56)
            log("检测到 INSTALL_FAILED_MISSING_SPLIT：该包是 AAB 拆分包，只有 base 装不上。")
            log("处理办法（任选）：")
            log("  1. 从 APKPure/APKCombo 下载该 app 的 xapk 完整包，用它重跑本工具")
            log("  2. 勾选「强制单包模式」重跑（工具会删除 splits-required 声明；")
            log("     注意：原生库/资源在缺失的 split 里，app 可能崩溃，仅作兜底）")
            log("=" * 56)
    return ok


def launch_verify(adb, serial, pkg, log, progress=None):
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    _run(cmd + ["shell", "monkey", "-p", pkg,
              "-c", "android.intent.category.LAUNCHER", "1"], timeout=60)
    log("已启动，等待 12 秒观察 ...")
    for i in range(12):
        time.sleep(1)
        if progress:
            progress(int((i + 1) * 100 / 12))
    r = _run(cmd + ["shell", "pidof", pkg], timeout=30)
    alive = bool(r.stdout.strip())
    log("进程存活: %s" % ("是" if alive else "否"))
    r = _run(cmd + ["logcat", "-d"], timeout=60)
    hits = [l for l in r.stdout.splitlines()
            if re.search(r"premium|PremiumHelper|pairip", l)][-10:]
    for h in hits:
        log("   " + h[-180:])
    if any("because user is premium" in h or "hasAccessLevel" in h for h in hits):
        log("✔ 检测到 premium 生效日志")
    else:
        log("（日志中未见 premium 关键字，请在设备上人工确认界面）")
    return alive


# ---------------------------------------------------------------- 主流程

class Unlocker:
    def __init__(self, log):
        self.log = log

    def _fail(self, stage_key, msg):
        self.log(msg)
        _set_error(msg)
        mark_stage(stage_key, "failed")

    def _setup_dirs(self, input_path):
        base_dir = os.path.dirname(os.path.abspath(input_path))
        stem = os.path.splitext(os.path.basename(input_path))[0]
        out_root = os.path.join(base_dir, stem + "_unlocked")
        workdir = os.path.join(out_root, "work")
        for d in (workdir, os.path.join(out_root, "signed")):
            os.makedirs(d, exist_ok=True)
        return out_root, workdir

    def _pkg_from_manifest(self, workdir):
        m = os.path.join(workdir, "manifest.json")
        if os.path.isfile(m):
            try:
                with open(m, encoding="utf-8") as f:
                    mj = json.load(f)
                pn = mj.get("package_name") or mj.get("packageName")
                if pn:
                    self.log("[Frida] 从 manifest.json 取到包名: %s" % pn)
                    return pn
            except Exception:
                pass
        return None

    def _pkg_installed(self, adb, serial, pkg):
        cmd = [adb] + (["-s", serial] if serial else [])
        r = _run(cmd + ["shell", "pm", "list", "packages", pkg], timeout=30)
        return pkg in r.stdout

    # ---------------- Frida 模式（第 3 层，动态 hook，不改包） ----------------

    def _run_frida(self, input_path, install_flag, pkg, progress):
        _reset_state(FRIDA_STAGES)
        set_stage("init", self.log)
        adb = find_adb()
        if not adb:
            self._fail("init", "错误：未找到 adb（Frida 模式需要设备连接）")
            _finish_state(False)
            return
        progress(50)

        set_stage("unpack", self.log)
        out_root, workdir = self._setup_dirs(input_path)
        base_apk, _splits = unpack_input(input_path, workdir, self.log, progress)
        if not base_apk or not os.path.isfile(base_apk):
            self._fail("unpack", "错误：解包失败，未找到 base APK")
            _finish_state(False)
            return
        if not pkg:
            pkg = self._pkg_from_manifest(workdir)
        if not pkg:
            self._fail("unpack", "错误：Frida 模式需要包名，请在页面输入框填写（如 com.xxx.yyy）")
            _finish_state(False)
            return

        set_stage("detect", self.log)
        sdks = frida_mode.detect_from_apk(base_apk)
        if sdks:
            self.log("[Frida] dex 检测到 SDK: %s" % "、".join(sdks))
        else:
            self.log("[Frida] 未识别订阅 SDK，将只用通用缓存 hook（SharedPreferences/RemoteConfig）")
        progress(70)

        set_stage("script", self.log)
        script_path = os.path.join(out_root, "frida_script.js")
        frida_mode.write_script(sdks, pkg, script_path, self.log)
        progress(100)

        if not frida_mode.frida_available():
            self.log("[Frida] 本机未打包 frida 客户端。脚本已生成，手动跑法：")
            self.log("    pip install frida-tools")
            self.log('    frida -U -f %s -l "%s" --no-pause' % (pkg, script_path))
            mark_stage("deploy", "skipped")
            mark_stage("run", "skipped")
            mark_stage("verify", "skipped")
            self.log("完成（仅生成脚本）。")
            _finish_state(True)
            return

        set_stage("deploy", self.log)
        adb2, serial = get_device()
        if not adb2 or not serial:
            self._fail("deploy", "未检测到设备（Frida 模式需要 USB 连接且设备已 root）")
            _finish_state(False)
            return
        ok, note = frida_mode.deploy_frida_server(adb2, serial, self.log)
        if not ok:
            self._fail("deploy", "frida-server 部署失败（%s）" % note)
            _finish_state(False)
            return
        progress(100)

        set_stage("run", self.log)
        if install_flag and not self._pkg_installed(adb2, serial, pkg):
            self.log("[Frida] 设备上未安装 %s，先装原始 APK" % pkg)
            if not install(adb2, serial, pkg, workdir, self.log, progress):
                self._fail("run", "错误：安装原始 APK 失败")
                _finish_state(False)
                return
        if not frida_mode.run_hook(pkg, script_path, self.log):
            self._fail("run", "错误：Frida 注入失败（详见日志）")
            _finish_state(False)
            return

        set_stage("verify", self.log)
        self.log("已启动，等待 12 秒观察 ...")
        for i in range(12):
            time.sleep(1)
            progress(int((i + 1) * 100 / 12))
        cmd = [adb2] + (["-s", serial] if serial else [])
        r = _run(cmd + ["shell", "pidof", pkg], timeout=30)
        alive = bool(r.stdout.strip())
        self.log("进程存活: %s" % ("是" if alive else "否"))
        self.log("完成。Hook 保持运行中，关闭工具即结束。")
        _finish_state(True)

    # ---------------- 主流程（重打包） ----------------

    def run(self, input_path, install_flag, force_single=False, progress=None,
            frida=False, pkg=None):
        progress = progress or report_progress
        self.log("=" * 56)
        self.log("开始处理: %s%s" % (input_path, "（Frida 模式）" if frida else ""))
        if frida:
            self._run_frida(input_path, install_flag, pkg, progress)
            return
        _reset_state()
        if not os.path.isfile(input_path):
            self._fail("init", "文件不存在: %s" % input_path)
            _finish_state(False)
            return

        set_stage("init", self.log)
        ensure_runtime(self.log)
        progress(15)

        java = find_java()
        if not java:
            self._fail("init", "错误：未找到 Java（需要 Java 17+）")
            _finish_state(False)
            return
        apktool = find_apktool(self.log)
        if not apktool:
            self._fail("init", "错误：apktool 不可用")
            _finish_state(False)
            return
        progress(45)
        apksigner = find_apksigner()
        if not apksigner:
            self._fail("init", "错误：未找到 apksigner（Android SDK build-tools）")
            _finish_state(False)
            return
        keystore = ensure_debug_keystore(self.log)
        progress(70)

        res = (ensure_runtime() or "").replace("\\", "/")
        def src(p):
            return "内置" if p and res and res in p.replace("\\", "/") else "系统"
        self.log("运行时解析: java[%s] apktool[%s] apksigner[%s] keystore[%s]"
                 % (src(java), src(apktool), src(apksigner), src(keystore)))
        progress(100)

        out_root, workdir = self._setup_dirs(input_path)
        apk_out = os.path.join(out_root, "apktool_out")
        signed_dir = os.path.join(out_root, "signed")
        repack = os.path.join(out_root, "base_patched.apk")

        # 1 解包
        set_stage("unpack", self.log)
        base_apk, splits = unpack_input(input_path, workdir, self.log, progress)
        if not base_apk or not os.path.isfile(base_apk):
            self._fail("unpack", "错误：解包失败，未找到 base APK")
            _finish_state(False)
            return

        # 2 反编译
        set_stage("decompile", self.log)
        if not decompile(apktool, base_apk, apk_out, self.log):
            self._fail("decompile", "错误：反编译失败")
            _finish_state(False)
            return

        # 3 patch：pairip → 三层确定性补丁（SDK 模板 / 缓存层 / 判断链）→ AI 兜底
        set_stage("patch", self.log)
        patch_pairip(apk_out, self.log)
        progress(15)
        if force_single:
            patch_force_single(apk_out, self.log)
        sdks, patched = sdk_patches.apply_all(apk_out, self.log, progress)
        if patched == 0 and ai_patch.has_ai():
            self.log("[AI] 确定性补丁无命中，调用 DeepSeek 分析补丁方案")
            ai_patch.run(apk_out, self.log, progress)
        elif patched == 0:
            self.log("[补丁] 确定性补丁无命中；未配置 DeepSeek Key，跳过订阅补丁"
                     "（仅 pairip 绕过）。可在页面下方配置 Key 后重跑")
        else:
            self.log("[补丁] 确定性补丁完成: %d 处（SDK: %s）"
                     % (patched, "、".join(sdks) if sdks else "无"))
        pkg = get_package_name(apk_out)

        # 4 重打包（失败自动生成 Frida 脚本备用）
        set_stage("build", self.log)
        if not build(apktool, apk_out, repack, self.log, progress):
            try:
                sdks_dex = frida_mode.detect_from_apk(base_apk)
                fpath = os.path.join(out_root, "frida_script.js")
                frida_mode.write_script(sdks_dex, pkg or "", fpath, self.log)
                self.log("[Frida] 重打包失败的包可在 root 设备上动态 hook"
                         "（页面勾选 Frida 模式一键跑，包名 %s）" % (pkg or "待填"))
            except Exception as e:
                self.log("[Frida] 生成备用脚本失败: %s" % e)
            self._fail("build", "错误：重打包失败（可能签名校验/加固，可勾选 Frida 模式重试）")
            _finish_state(False)
            return

        # 5 签名
        set_stage("sign", self.log)
        shutil.copyfile(repack, os.path.join(signed_dir, "base.apk"))
        for sid, sp in splits.items():
            shutil.copyfile(sp, os.path.join(signed_dir, sid + ".apk"))
        sign_files = [os.path.join(signed_dir, f) for f in os.listdir(signed_dir)
                      if f.endswith(".apk")]
        if not sign_all(apksigner, keystore, sign_files, self.log, progress):
            self._fail("sign", "错误：签名失败")
            _finish_state(False)
            return
        self.log("✔ 补丁包已生成: %s" % signed_dir)

        # 6 安装 + 验证
        if install_flag:
            set_stage("install", self.log)
            adb, serial = get_device()
            if not adb or not serial:
                self.log("未检测到设备，跳过安装。可手动: adb install-multiple signed/*.apk")
                mark_stage("install", "skipped")
                mark_stage("verify", "skipped")
            else:
                abilist, density = device_props(adb, serial)
                if not install(adb, serial, pkg, signed_dir, self.log, progress):
                    self._fail("install", "错误：安装失败")
                    _finish_state(False)
                    return
                set_stage("verify", self.log)
                launch_verify(adb, serial, pkg, self.log, progress)
        else:
            self.log("（跳过安装，仅生成补丁包）")
            mark_stage("install", "skipped")
            mark_stage("verify", "skipped")
        self.log("完成。")
        _finish_state(True)


# ---------------------------------------------------------------- GUI
class CLILog:
    """CLI 模式的日志：有 stdout 就打印（编码失败不致命），同时进缓冲和日志文件。"""
    def __call__(self, msg):
        emit_log(msg)
        out = getattr(sys, "stdout", None)
        if out is not None:
            try:
                print(msg, flush=True)
            except Exception:
                pass


def main():
    if "--cli" in sys.argv:
        idx = sys.argv.index("--cli")
        if idx + 1 >= len(sys.argv):
            return
        path = sys.argv[idx + 1]
        install = "--no-install" not in sys.argv
        force = "--force-single" in sys.argv
        frida = "--frida" in sys.argv
        pkg = None
        if "--pkg" in sys.argv:
            p = sys.argv.index("--pkg")
            if p + 1 < len(sys.argv):
                pkg = sys.argv[p + 1]
        Unlocker(CLILog()).run(path, install, force, frida=frida, pkg=pkg)
        return
    ensure_runtime(None)
    emit_log("工具启动 v%s（Web UI）" % VERSION)
    from webui import run_server
    run_server()


if __name__ == "__main__":
    main()
