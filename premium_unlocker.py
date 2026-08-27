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

VERSION = "1.1"
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

def unpack_input(path, workdir, log):
    """返回 (base_apk, splits_dict)。splits_dict: {id: path}"""
    low = path.lower()
    if low.endswith((".xapk", ".zip")):
        log("解包 %s ..." % os.path.basename(path))
        with zipfile.ZipFile(path) as z:
            z.extractall(workdir)
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
        return base, splits
    else:
        dest = os.path.join(workdir, "base.apk")
        shutil.copyfile(path, dest)
        log("单 APK 输入: %s" % os.path.basename(path))
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


def decompile(apktool, base_apk, outdir, log):
    log("apktool 反编译（大包需要几分钟）...")
    return run_cmd([find_java(), "-Xmx4g", "-jar", apktool, "d", base_apk, "-o", outdir, "-f"],
                   log, timeout=900)


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


def build(apktool, outdir, repack, log):
    log("apktool 重打包 ...")
    for attempt in range(6):
        r = _run([find_java(), "-Xmx4g", "-jar", apktool, "b", outdir, "-o", repack],
                 timeout=900)
        if r.returncode == 0:
            log("   已生成: %s" % repack)
            return True
        out = (r.stdout or "") + (r.stderr or "")
        # AI 补丁写出非法 smali → 回滚 AI 补丁后重试（pairip/资源补丁不受影响）
        if ("Could not smali" in out or "no viable alternative" in out) and ai_patch.rollback(log):
            log("[资源修复] 已回滚 AI 补丁，重试重打包")
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


# ---------------------------------------------------------------- patch: Adapty

def _patch_getter(smali_path, class_desc, log, tag):
    """把 isActive()Z / getIsActive()Z getter 改成恒 true。"""
    if not os.path.isfile(smali_path):
        log("[%s] 文件不存在，跳过" % tag)
        return 0
    with open(smali_path, encoding="utf-8") as f:
        lines = f.readlines()
    patched = 0
    in_method = False
    for i, line in enumerate(lines):
        if re.search(r"\.method.*\s(isActive|getIsActive)\(\)Z", line):
            in_method = True
            continue
        if in_method and line.strip() == ".end method":
            in_method = False
            continue
        if in_method:
            m = re.match(r"(\s*)iget-boolean\s+(\w+),\s*p0,\s*" + re.escape(class_desc)
                         + r"->isActive:Z", line)
            if m:
                lines[i] = "%sconst/4 %s, 0x1\n" % (m.group(1), m.group(2))
                patched += 1
                log("[%s] getter 恒 true: 行 %d" % (tag, i + 1))
    if patched:
        with open(smali_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    else:
        log("[%s] 未找到 isActive getter（SDK 版本差异？）" % tag)
    return patched


def _patch_null_branch(outdir, log):
    """
    修 map builder 的 null 分支：
    找到含 `AccessLevel;->isActive()Z` 的方法；在 isActive 的 move-result 与
    Boolean.valueOf 之间，若存在条件跳转到某个 label，且该 label 下第一句是
    `move V, W`（W 在本方法中被 const/4 赋过 0），则把该句改成 const/4 V, 0x1。
    """
    patched_files = 0
    for root, _, files in os.walk(outdir):
        for fn in files:
            if not fn.endswith(".smali"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    txt = f.read()
            except Exception:
                continue
            if "AccessLevel;->isActive()Z" not in txt:
                continue
            # 切方法
            methods = re.split(r"(\.method[^\n]*\n)", txt)
            changed = False
            for mi in range(1, len(methods), 2):
                header = methods[mi]
                body = methods[mi + 1]
                if "isActive()Z" not in body or "Boolean;->valueOf" not in body:
                    continue
                # 本方法内被赋过 0 的寄存器
                zero_regs = set(re.findall(r"const/4\s+(\w+),\s*0x?0\b", body))
                if not zero_regs:
                    continue
                body_lines = body.splitlines(keepends=True)
                # 找 isActive 的 move-result 寄存器 → valueOf 寄存器
                for i, line in enumerate(body_lines):
                    m = re.search(r"AccessLevel;->isActive\(\)Z", line)
                    if not m:
                        continue
                    res_reg = None
                    for j in range(i + 1, min(i + 6, len(body_lines))):
                        m2 = re.match(r"\s*move-result\s+(\w+)", body_lines[j])
                        if m2:
                            res_reg = m2.group(1)
                            break
                    if not res_reg:
                        continue
                    # valueOf 用同一个寄存器？
                    valof_idx = None
                    for j in range(i + 1, min(i + 40, len(body_lines))):
                        if "Boolean;->valueOf" in body_lines[j]:
                            valof_idx = j
                            break
                    if valof_idx is None:
                        continue
                    # 收集 isActive → valueOf 窗口内的跳转 label
                    window = body_lines[i:valof_idx + 1]
                    labels = set()
                    for wl in window:
                        m3 = re.match(r"\s*(:[\w_]+)", wl)
                        if m3:
                            labels.add(m3.group(1))
                    if not labels:
                        continue
                    # 找 label 下紧跟的 move V, W
                    for j, wl in enumerate(body_lines[i:valof_idx + 1], start=i):
                        m4 = re.match(r"(\s*)(:[\w_]+)\s*$", wl)
                        if not m4 or m4.group(2) not in labels:
                            continue
                        if j + 1 >= len(body_lines):
                            continue
                        m5 = re.match(r"(\s*)move\s+(\w+),\s*(\w+)", body_lines[j + 1])
                        if not m5:
                            continue
                        if m5.group(2) != res_reg or m5.group(3) not in zero_regs:
                            continue
                        body_lines[j + 1] = "%sconst/4 %s, 0x1\n" % (m5.group(1), res_reg)
                        changed = True
                        methods[mi + 1] = "".join(body_lines)
                        log("[Adapty] null 分支恒 true: %s 行 %d (方法 %s)"
                            % (os.path.relpath(p, outdir), j + 2, header.strip()[:80]))
                        break
            if changed:
                with open(p, "w", encoding="utf-8") as f:
                    f.write("".join(methods))
                patched_files += 1
    if not patched_files:
        log("[Adapty] 未定位到 null 分支（新用户 profile 无 premium level 时可能仍显示未订阅）")
    return patched_files


def patch_adapty(outdir, log):
    for root, _, files in os.walk(outdir):
        for fn in files:
            if fn == "AdaptyProfile$AccessLevel.smali" and "adapty" in root.replace(os.sep, "/"):
                _patch_getter(os.path.join(root, fn),
                              "Lcom/adapty/models/AdaptyProfile$AccessLevel;",
                              log, "Adapty/AccessLevel")
            if fn == "AdaptyProfile$Subscription.smali" and "adapty" in root.replace(os.sep, "/"):
                _patch_getter(os.path.join(root, fn),
                              "Lcom/adapty/models/AdaptyProfile$Subscription;",
                              log, "Adapty/Subscription")
    _patch_null_branch(outdir, log)


def has_adapty(outdir):
    for root, _, files in os.walk(outdir):
        if "adapty" in root.replace(os.sep, "/").lower() and files:
            return True
    return False


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

def sign_all(apksigner, keystore, files, log):
    ok = True
    for f in files:
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


def install(adb, serial, pkg, signed_dir, log):
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
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["install-multiple"] + files
    log("安装 %d 个 APK ..." % len(files))
    ok = run_cmd(cmd, log, timeout=600)
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


def launch_verify(adb, serial, pkg, log):
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    _run(cmd + ["shell", "monkey", "-p", pkg,
              "-c", "android.intent.category.LAUNCHER", "1"], timeout=60)
    log("已启动，等待 12 秒观察 ...")
    time.sleep(12)
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

    def run(self, input_path, install_flag, force_single=False):
        self.log("=" * 56)
        self.log("开始处理: %s" % input_path)
        if not os.path.isfile(input_path):
            self.log("文件不存在")
            return

        ensure_runtime(self.log)

        java = find_java()
        if not java:
            self.log("错误：未找到 Java（需要 Java 17+）")
            return
        apktool = find_apktool(self.log)
        if not apktool:
            self.log("错误：apktool 不可用")
            return
        apksigner = find_apksigner()
        if not apksigner:
            self.log("错误：未找到 apksigner（Android SDK build-tools）")
            return
        keystore = ensure_debug_keystore(self.log)

        res = (ensure_runtime() or "").replace("\\", "/")
        def src(p):
            return "内置" if p and res and res in p.replace("\\", "/") else "系统"
        self.log("运行时解析: java[%s] apktool[%s] apksigner[%s] keystore[%s]"
                 % (src(java), src(apktool), src(apksigner), src(keystore)))

        base_dir = os.path.dirname(os.path.abspath(input_path))
        stem = os.path.splitext(os.path.basename(input_path))[0]
        out_root = os.path.join(base_dir, stem + "_unlocked")
        workdir = os.path.join(out_root, "work")
        apk_out = os.path.join(out_root, "apktool_out")
        signed_dir = os.path.join(out_root, "signed")
        repack = os.path.join(out_root, "base_patched.apk")
        for d in (workdir, signed_dir):
            os.makedirs(d, exist_ok=True)

        # 1 解包
        base_apk, splits = unpack_input(input_path, workdir, self.log)
        if not base_apk or not os.path.isfile(base_apk):
            self.log("错误：解包失败，未找到 base APK")
            return

        # 2 反编译
        if not decompile(apktool, base_apk, apk_out, self.log):
            self.log("错误：反编译失败")
            return

        # 3 patch
        patch_pairip(apk_out, self.log)
        if force_single:
            patch_force_single(apk_out, self.log)
        if has_adapty(apk_out):
            self.log("[Adapty] 检测到 Adapty SDK，应用订阅解锁补丁")
            patch_adapty(apk_out, self.log)
        else:
            self.log("[Adapty] 未检测到 Adapty SDK")
            if ai_patch.has_ai():
                self.log("[AI] 订阅 SDK 非 Adapty，调用 DeepSeek 分析补丁方案")
                ai_patch.run(apk_out, self.log)
            else:
                self.log("[AI] 未配置 DeepSeek Key，跳过订阅补丁（仅 pairip 绕过）。"
                         "可在页面下方配置 Key 后重跑，解锁非 Adapty 订阅")

        # 4 重打包
        if not build(apktool, apk_out, repack, self.log):
            self.log("错误：重打包失败")
            return

        # 5 签名
        shutil.copyfile(repack, os.path.join(signed_dir, "base.apk"))
        for sid, sp in splits.items():
            shutil.copyfile(sp, os.path.join(signed_dir, sid + ".apk"))
        sign_files = [os.path.join(signed_dir, f) for f in os.listdir(signed_dir)
                      if f.endswith(".apk")]
        if not sign_all(apksigner, keystore, sign_files, self.log):
            self.log("错误：签名失败")
            return
        self.log("✔ 补丁包已生成: %s" % signed_dir)

        # 6 安装 + 验证
        pkg = get_package_name(apk_out)
        if install_flag:
            adb, serial = get_device()
            if not adb or not serial:
                self.log("未检测到设备，跳过安装。可手动: adb install-multiple signed/*.apk")
            else:
                abilist, density = device_props(adb, serial)
                if not install(adb, serial, pkg, signed_dir, self.log):
                    self.log("错误：安装失败")
                    return
                launch_verify(adb, serial, pkg, self.log)
        else:
            self.log("（跳过安装，仅生成补丁包）")
        self.log("完成。")


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
        Unlocker(CLILog()).run(path, install, force)
        return
    ensure_runtime(None)
    emit_log("工具启动 v%s（Web UI）" % VERSION)
    from webui import run_server
    run_server()


if __name__ == "__main__":
    main()
