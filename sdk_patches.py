# -*- coding: utf-8 -*-
"""
订阅解锁确定性补丁库（三层：SDK 模板 / 缓存层规则 / 判断链扫描）。

1. SDK 模板层：已知订阅 SDK 的 getter / 判断方法 → 恒 true
   （Adapty / RevenueCat / Superwall / Qonversion / Apphud）
2. 缓存层规则：SharedPreferences / Firebase Remote Config 的 premium 布尔读点 → 恒 true
3. 判断链扫描：业务代码里返回 boolean 的 premium 判断方法 → 恒 true
   （比 AI 兜底快且确定；AI 只兜底模板覆盖不到的）

原则：只做文本级确定性替换；改不动的地方宁可跳过并提示，不赌。
性能：apply_all 单次遍历全部 smali，每个文件只读一遍全文、做所有检查。
"""
import os
import re

# 已应用补丁的原文件备份（path -> 原内容），build 失败可回滚（对齐 ai_patch 的机制）
BACKUP = {}


def _backup(path):
    """写文件前备份原内容（只备份一次）。"""
    if path in BACKUP:
        return
    try:
        with open(path, encoding="utf-8") as f:
            BACKUP[path] = f.read()
    except Exception:
        pass


def rollback(log=None):
    """回滚所有确定性补丁（SDK 模板 / 缓存层 / 判断链）。返回回滚文件数。"""
    if not BACKUP:
        return 0
    n = 0
    for path, original in list(BACKUP.items()):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)
            n += 1
        except Exception:
            pass
    BACKUP.clear()
    if log:
        log("[补丁] 已回滚 %d 个确定性补丁文件（补丁导致 smali 错误）" % n)
    return n

# ---------------------------------------------------------------- SDK 模板

# 每个模板的补丁规则：
#   getters:      类文件 + 类描述符 + 字段名 + 方法名列表（iget-boolean 读点 → const true）
#   body_true:    方法整体重写为 return true（sealed class / 静态方法这类没法打 getter 的）
#   null_signatures: 调用点签名片段（用于 null 分支扫描）
SDK_TEMPLATES = {
    "adapty": {
        "getters": [
            {"file": "AdaptyProfile$AccessLevel.smali",
             "class": "Lcom/adapty/models/AdaptyProfile$AccessLevel;",
             "field": "isActive", "methods": ["isActive", "getIsActive"]},
            {"file": "AdaptyProfile$Subscription.smali",
             "class": "Lcom/adapty/models/AdaptyProfile$Subscription;",
             "field": "isActive", "methods": ["isActive", "getIsActive"]},
        ],
        "null_signatures": [
            "Lcom/adapty/models/AdaptyProfile$AccessLevel;->isActive()Z",
            "Lcom/adapty/models/AdaptyProfile$Subscription;->isActive()Z",
        ],
    },
    "revenuecat": {
        "getters": [
            {"file": "EntitlementInfo.smali",
             "class": "Lcom/revenuecat/purchases/EntitlementInfo;",
             "field": "isActive", "methods": ["isActive"]},
        ],
        "null_signatures": [
            "Lcom/revenuecat/purchases/EntitlementInfo;->isActive()Z",
        ],
    },
    "superwall": {
        # sealed class：isActive()Z 在各子类/基类里是 when 判断，直接整方法重写
        "body_true": [
            {"file": "SubscriptionStatus", "method_re": r"\.method\s+[\w\s$]*?\s(isActive)\(\)Z"},
        ],
        "null_signatures": [
            "SubscriptionStatus;->isActive()Z",
        ],
    },
    "qonversion": {
        "getters": [
            {"file": "Entitlement.smali",
             "class": "Lcom/qonversion/android/sdk/dto/entitlements/Entitlement;",
             "field": "isActive", "methods": ["isActive"]},
        ],
        "null_signatures": [
            "Lcom/qonversion/android/sdk/dto/entitlements/Entitlement;->isActive()Z",
        ],
    },
    "apphud": {
        # 静态方法 hasActiveSubscription()Z，直接整方法重写
        "body_true": [
            {"file": "Apphud.smali",
             "method_re": r"\.method\s+[\w\s$]*?\s(hasActiveSubscription)\(\)Z"},
            {"file": "ApphudUser.smali",
             "method_re": r"\.method\s+[\w\s$]*?\s(hasActiveSubscription)\(\)Z"},
        ],
        "null_signatures": [
            "Lcom/apphud/sdk/Apphud;->hasActiveSubscription()Z",
        ],
    },
}

SDK_MARKERS = {
    "adapty": ["com/adapty"],
    "revenuecat": ["com/revenuecat"],
    "superwall": ["com/superwall"],
    "qonversion": ["com/qonversion"],
    "apphud": ["com/apphud"],
}


def detect_sdks(outdir):
    """按 smali 目录树检测订阅 SDK。"""
    found = []
    for root, _dirs, _files in os.walk(outdir):
        base = root.replace(os.sep, "/").lower()
        for sdk, marks in SDK_MARKERS.items():
            if sdk not in found and any(m in base for m in marks):
                found.append(sdk)
    return found


# ---------------------------------------------------------------- 基础补丁原语

_FILE_CACHE = {}


def _iter_smali_files(outdir):
    """列出全部 .smali 路径并缓存（单次遍历共享一份，省重复 os.walk I/O）。"""
    if outdir not in _FILE_CACHE:
        files = []
        for root, _dirs, fs in os.walk(outdir):
            for fn in fs:
                if fn.endswith(".smali"):
                    files.append(os.path.join(root, fn))
        _FILE_CACHE[outdir] = files
    return _FILE_CACHE[outdir]


def _split_methods(txt):
    """切方法。返回 (head, [(header, body), ...])；head 为第一个 .method 之前的内容
    （.class / .super / .source 等，写回时必须保留）。"""
    m = re.search(r"\.method[^\n]*\n", txt)
    if not m:
        return txt, []
    head = txt[:m.start()]
    parts = re.split(r"(\.method[^\n]*\n)", txt[m.start():])
    methods = []
    for i in range(1, len(parts), 2):
        methods.append((parts[i].rstrip("\n"),
                        parts[i + 1] if i + 1 < len(parts) else ""))
    return head, methods


def _join_methods(head, methods):
    """head + 方法列表拼回文件内容。"""
    return head + "".join(h + "\n" + b for h, b in methods)


def _method_true_body(header):
    """把方法体重写为恒 true，返回 (header, body)。abstract/native 返回 None。"""
    if re.search(r"\b(abstract|native)\b", header):
        return None
    return header, "\n    .locals 1\n\n    const/4 v0, 0x1\n\n    return v0\n"


# ---------------------------------------------------------------- 单文件补丁（纯函数，接受 txt 返回 (补丁数, new_txt)）

def _patch_getter_text(txt, g, log, tag, rel):
    """把 isActive()Z 类 getter 的 iget-boolean 读点改成 const/4 1。"""
    lines = txt.splitlines(keepends=True)
    patched = 0
    in_method = False
    method_re = re.compile(
        r"\.method.*\s(" + "|".join(re.escape(m) for m in g["methods"]) + r")\(\)Z")
    for i, line in enumerate(lines):
        if method_re.search(line):
            in_method = True
            continue
        if in_method and line.strip() == ".end method":
            in_method = False
            continue
        if in_method:
            m = re.match(r"(\s*)iget-boolean\s+(\w+),\s*p\d+,\s*"
                         + re.escape(g["class"]) + r"->" + re.escape(g["field"]) + r":Z",
                         line)
            if m:
                lines[i] = "%sconst/4 %s, 0x1\n" % (m.group(1), m.group(2))
                patched += 1
                log("[%s] getter 恒 true: %s 行 %d" % (tag, rel, i + 1))
    if patched:
        return patched, "".join(lines)
    return 0, txt


def _patch_body_true_text(txt, method_re, log, tag, rel):
    """把匹配的方法整体重写为恒 true。"""
    mre = re.compile(method_re)
    head, methods = _split_methods(txt)
    changed = False
    new_methods = []
    for header, body in methods:
        if mre.search(header):
            nb = _method_true_body(header)
            if nb is None:
                new_methods.append((header, body))
                continue
            new_methods.append(nb)
            changed = True
            log("[%s] 方法恒 true: %s %s" % (tag, rel, header.strip()[:90]))
        else:
            new_methods.append((header, body))
    if changed:
        return 1, _join_methods(head, new_methods)
    return 0, txt


def _patch_null_text(txt, frags, log, tag, rel):
    """
    通用 null 分支补丁（单文件）：
    对含调用签名（如 AccessLevel;->isActive()Z）的方法，找 move-result 寄存器，
    在调用点后的窗口内找"null 路径"的赋值，改成 const/4 V, 0x1。
    """
    if not any(frag in txt for frag in frags):
        return 0, txt
    head, methods = _split_methods(txt)
    changed = False
    out_methods = []
    for header, body in methods:
        if not any(frag in body for frag in frags):
            out_methods.append((header, body))
            continue
        body_lines = body.splitlines(keepends=True)
        zero_regs = set(re.findall(r"const/4\s+(\w+),\s*0x?0\b", body))
        hit = False
        for i, line in enumerate(body_lines):
            if not any(frag in line for frag in frags):
                continue
            res_reg = None
            for j in range(i + 1, min(i + 6, len(body_lines))):
                m2 = re.match(r"\s*move-result\s+(\w+)", body_lines[j])
                if m2:
                    res_reg = m2.group(1)
                    break
            if not res_reg:
                continue
            target_reg = res_reg
            for j in range(i + 1, min(i + 45, len(body_lines))):
                if "Boolean;->valueOf" in body_lines[j]:
                    m7 = re.search(r"\{([^}]*)\}", body_lines[j])
                    if m7:
                        args = [a.strip() for a in m7.group(1).split(",") if a.strip()]
                        if args:
                            target_reg = args[0]
                    break
            end = min(i + 45, len(body_lines))
            labels = set()
            for wl in body_lines[i:end]:
                m3 = re.match(r"\s*(:\w+)\s*$", wl)
                if m3:
                    labels.add(m3.group(1))
            patched_line = False
            for j in range(i, end - 1):
                wl = body_lines[j]
                m4 = re.match(r"\s*(:\w+)\s*$", wl)
                if not m4 or m4.group(1) not in labels:
                    continue
                for k in range(j + 1, min(j + 4, len(body_lines))):
                    t = body_lines[k]
                    m5 = re.match(r"(\s*)move\s+(\w+),\s*(\w+)", t)
                    if m5 and m5.group(2) == target_reg and m5.group(3) in zero_regs:
                        body_lines[k] = "%sconst/4 %s, 0x1\n" % (m5.group(1), target_reg)
                        patched_line = True
                        break
                    m6 = re.match(r"(\s*)const/4\s+(\w+),\s*0x?0\b", t)
                    if m6 and m6.group(2) == target_reg:
                        body_lines[k] = "%sconst/4 %s, 0x1\n" % (m6.group(1), target_reg)
                        patched_line = True
                        break
                if patched_line:
                    break
            if patched_line:
                hit = True
                log("[%s] 恒 true: %s 行 %d" % (tag, rel, j + 2))
                break
        out_methods.append((header, "".join(body_lines)))
        if hit:
            changed = True
    if changed:
        return 1, _join_methods(head, out_methods)
    return 0, txt


PREMIUM_KEY_RE = re.compile(
    r"(premium|is_?pro|vip|unlock|subscri|paid|entitle|active_?sub|adfree|"
    r"ads_?free|remove_?ads|purchase|member|gold|silver)",
    re.I)


def _patch_cache_text(txt, log, rel):
    """
    缓存层补丁（单文件）：
    const-string vS, "premium 相关 key" + SharedPreferences/FirebaseRemoteConfig
    的 getBoolean(...)Z + move-result vN → 把 vN 改成 const/4 1。
    """
    if "getBoolean" not in txt:
        return 0, txt
    getbool_re = re.compile(r"->getBoolean\(Ljava/lang/String;Z?\)Z")
    lines = txt.splitlines(keepends=True)
    patched = 0
    i = 0
    while i < len(lines):
        m = re.match(r'(\s*)const-string\s+(\w+),\s*"([^"]+)"', lines[i])
        if not m or not PREMIUM_KEY_RE.search(m.group(3)):
            i += 1
            continue
        key = m.group(3)
        sreg = m.group(2)
        done = False
        for j in range(i + 1, min(i + 9, len(lines))):
            if not getbool_re.search(lines[j]):
                continue
            args = re.search(r"\{([^}]*)\}", lines[j])
            if not args:
                continue
            arg_list = [a.strip() for a in args.group(1).split(",") if a.strip()]
            if not arg_list or sreg not in arg_list:
                continue
            for k in range(j + 1, min(j + 5, len(lines))):
                m2 = re.match(r"(\s*)move-result\s+(\w+)", lines[k])
                if m2:
                    lines[k] = "%sconst/4 %s, 0x1\n" % (m2.group(1), m2.group(2))
                    patched += 1
                    log("[缓存] %s -> true: %s 行 %d" % (key, rel, k + 1))
                    done = True
                    break
            if done:
                break
        i += 1
    if patched:
        return patched, "".join(lines)
    return 0, txt


BUSINESS_METHOD_RE = re.compile(
    r"\.method\s+[\w\s$]*?\s(isPremium|getIsPremium|isPremiumUser|isSubscribed|"
    r"getIsSubscribed|hasActiveSub|isActiveSubscription|hasEntitlement|isEntitled|"
    r"checkEntitlement|isPro|isVip|isUnlocked|isUnlock|isAdFree|isPaid|isPurchased|"
    r"hasPro|canAccessPremium|premiumEnabled|isPremiumActive|isUserPremium)"
    r"\(([^)]*)\)Z", re.I)

SIGNAL_RE = re.compile(
    r"->(isActive|getIsActive|isSubscribed|hasActiveSubscription|isActiveSubscription|"
    r"isPremium|getIsPremium|hasPremium|isUnlocked|isAdFree)\(\)Z"
    r"|queryPurchases|getCustomerInfo|EntitlementInfo|SubscriptionStatus"
    r"|->getBoolean\(|launchBillingFlow|onPurchaseUpdated|BillingResult|getPurchases",
    re.I)

SKIP_DIR_FRAGS = (
    "com/adapty/", "com/revenuecat/", "com/superwall/", "com/qonversion/",
    "com/apphud/", "com/google/", "com/android/", "androidx/", "kotlin/",
    "com/pairip/", "com/facebook/", "com/applovin/", "com/bytedance/",
    "com/adcolony/", "com/ironsource/", "com/unity3d/", "com/vungle/",
    "com/mintegral/", "com/chartboost/", "com/mopub/", "com/yandex/",
)

MAX_SCAN_METHOD_INSTRUCTIONS = 60


def _count_instructions(body_lines):
    n = 0
    for ln in body_lines:
        s = ln.strip()
        if not s or s.startswith(".") or s.startswith(":") or s.startswith("#"):
            continue
        n += 1
    return n


def _patch_business_text(txt, log, rel):
    """判断链扫描（单文件）：返回 boolean 的 premium 判断方法 → 恒 true。"""
    if not SIGNAL_RE.search(txt):
        return 0, txt
    head, methods = _split_methods(txt)
    changed = False
    new_methods = []
    for header, body in methods:
        m = BUSINESS_METHOD_RE.search(header)
        if not m or not SIGNAL_RE.search(body):
            new_methods.append((header, body))
            continue
        if _count_instructions(body.splitlines()) > MAX_SCAN_METHOD_INSTRUCTIONS:
            log("[判断链] 跳过（方法过长）: %s %s" % (rel, header.strip()[:80]))
            new_methods.append((header, body))
            continue
        nb = _method_true_body(header)
        if nb is None:
            new_methods.append((header, body))
            continue
        new_methods.append(nb)
        changed = True
        log("[判断链] %s -> 恒 true: %s" % (m.group(1), rel))
    if changed:
        return 1, _join_methods(head, new_methods)
    return 0, txt


# ---------------------------------------------------------------- 总入口

def apply_all(outdir, log, progress=None):
    """单次遍历做全部确定性补丁（SDK 模板 / 缓存层 / 判断链），每个文件只读一遍。"""
    found = detect_sdks(outdir)
    if found:
        log("[SDK] 检测到订阅 SDK: %s" % "、".join(found))
    files = _iter_smali_files(outdir)
    total = max(1, len(files))
    log("[补丁] 开始单次遍历扫描 %d 个 smali 文件 ..." % len(files))

    # 预收集 SDK 模板参数
    getter_targets = []   # (path, g, sdk)
    body_targets = []     # (file_frag, method_re, sdk)
    null_frags = []
    for sdk in found:
        tpl = SDK_TEMPLATES.get(sdk)
        if not tpl:
            continue
        for g in tpl.get("getters", []):
            for p in files:
                if os.path.basename(p) == g["file"]:
                    getter_targets.append((p, g, sdk))
        body_targets.extend((b["file"], b["method_re"], sdk)
                            for b in tpl.get("body_true", []))
        null_frags.extend(s.lstrip("L") for s in tpl.get("null_signatures", []))

    total_patched = 0
    for fi, p in enumerate(files, 1):
        if progress and fi % 200 == 0:
            progress(int(fi * 100 / total))
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        fn = os.path.basename(p)
        rel = os.path.relpath(p, outdir).replace(os.sep, "/")
        new_txt = txt
        changed = False

        # 1. SDK getter（按文件名 + class 描述符）
        for path, g, sdk in getter_targets:
            if path == p and g["class"] in new_txt[:8192]:
                n, new_txt = _patch_getter_text(new_txt, g, log, sdk, rel)
                if n:
                    changed = True
                    total_patched += n
        # 2. SDK body_true（按文件名片段）
        for frag, mre, sdk in body_targets:
            if frag in fn:
                n, new_txt = _patch_body_true_text(new_txt, mre, log, sdk, rel)
                if n:
                    changed = True
                    total_patched += n
        # 3. null 分支
        if null_frags and any(frag in new_txt for frag in null_frags):
            n, new_txt = _patch_null_text(new_txt, null_frags, log, "null 分支", rel)
            if n:
                changed = True
                total_patched += n
        # 4. 缓存层
        n, new_txt = _patch_cache_text(new_txt, log, rel)
        if n:
            changed = True
            total_patched += n
        # 5. 判断链（跳过 SDK/系统/广告目录）
        if not any(f in rel.lower() for f in SKIP_DIR_FRAGS):
            n, new_txt = _patch_business_text(new_txt, log, rel)
            if n:
                changed = True
                total_patched += n

        if changed:
            _backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(new_txt)

    if not total_patched:
        log("[补丁] 确定性补丁无命中")
    else:
        log("[补丁] 确定性补丁完成: %d 处（SDK: %s）"
            % (total_patched, "、".join(found) if found else "无"))
    return found, total_patched
