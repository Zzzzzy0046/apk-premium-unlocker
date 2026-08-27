# -*- coding: utf-8 -*-
r"""
打包器：把 PyInstaller onefile 的 exe 与 runtime 目录合并成单个自解压 exe。

结构: [PyInstaller exe][runtime 的 zip 压缩包][footer]
footer = b"PUZL" + <8字节小端 = zip 起始偏移>
运行时首次启动检测 footer，解压到 %LOCALAPPDATA%\PremiumUnlocker\runtime（只解一次）。

用法: python make_exe.py [src_exe] [runtime_dir] [out_exe]
"""
import os
import struct
import sys
import zipfile
import shutil
import tempfile

MAGIC = b"PUZL"


def pack(src_exe, runtime_dir, out_exe, log=print):
    if not os.path.isfile(src_exe):
        log("源 exe 不存在: %s" % src_exe)
        return 1
    if not os.path.isdir(runtime_dir):
        log("runtime 目录不存在: %s" % runtime_dir)
        return 1

    # 1. runtime 打成 zip
    zip_path = os.path.join(tempfile.gettempdir(), "puzl_runtime_payload.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    log("压缩 runtime -> %s ..." % zip_path)
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _, files in os.walk(runtime_dir):
            for f in files:
                p = os.path.join(root, f)
                arc = os.path.relpath(p, os.path.dirname(runtime_dir))
                z.write(p, arc)
                n += 1
    log("   %d 个文件" % n)

    # 2. exe + zip + footer
    os.makedirs(os.path.dirname(out_exe) or ".", exist_ok=True)
    with open(out_exe, "wb") as out:
        with open(src_exe, "rb") as f:
            shutil.copyfileobj(f, out, 1024 * 1024)
        offset = out.tell()
        with open(zip_path, "rb") as f:
            shutil.copyfileobj(f, out, 1024 * 1024)
        out.write(MAGIC + struct.pack("<Q", offset))
    os.remove(zip_path)
    log("完成: %s (%.1f MB)" % (out_exe, os.path.getsize(out_exe) / 1048576))
    return 0


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "dist_onefile/竞品订阅解锁工具.exe"
    rt = sys.argv[2] if len(sys.argv) > 2 else "runtime"
    out = sys.argv[3] if len(sys.argv) > 3 else "dist/竞品订阅解锁工具.exe"
    sys.exit(pack(src, rt, out))
