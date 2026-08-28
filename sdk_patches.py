# -*- coding: utf-8 -*-
"""
订阅解锁确定性补丁库（三层：SDK 模板 / 缓存层规则 / 判断链扫描）。

1. SDK 模板层：已知订阅 SDK 的 getter / 判断方法 → 恒 true
   （Adapty / RevenueCat / Superwall / Qonversion / Apphud）
2. 缓存层规则：SharedPreferences / Firebase Remote Config 的 premium 布尔读点 → 恒 true
3. 判断链扫描：业务代码里返回 boolean 的 premium 判断方法 → 恒 true
   （比 AI 兜底快且确定；AI 只兜底模板覆盖不到的）

原则：只做文本级确定性替换；改不动的地方宁可跳过并提示，不赌。
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

def _iter_smali_files(outdir):
    for root, _dirs, files in os.walk(outdir):
        for fn in files:
            if fn.endswith(".smali"):
                yield os.path.join(root, fn)


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


def _patch_getter_file(smali_path, class_desc, field, method_names, log, tag):
    """把 isActive()Z 类 getter 的 iget-boolean 读点改成 const/4 1。"""
    if not os.path.isfile(smali_path):
        log("[%s] 文件不存在，跳过" % tag)
        return 0
    with open(smali_path, encoding="utf-8") as f:
        lines = f.readlines()
    patched = 0
    in_method = False
    method_re = re.compile(
        r"\.method.*\s(" + "|".join(re.escape(m) for m in method_names) + r")\(\)Z")
    for i, line in enumerate(lines):
        if method_re.search(line):
            in_method = True
            continue
        if in_method and line.strip() == ".end method":
            in_method = False
            continue
        if in_method:
            m = re.match(r"(\s*)iget-boolean\s+(\w+),\s*p\d+,\s*"
                         + re.escape(class_desc) + r"->" + re.escape(field) + r":Z",
                         line)
            if m:
                lines[i] = "%sconst/4 %s, 0x1\n" % (m.group(1), m.group(2))
                patched += 1
                log("[%s] getter 恒 true: 行 %d" % (tag, i + 1))
    if patched:
        _backup(smali_path)
        with open(smali_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    else:
        log("[%s] 未找到 getter 读点（SDK 版本差异？）" % tag)
    return patched


def _patch_body_true_files(outdir, file_frag, method_re, log, tag):
    """按文件名片段找 smali，把匹配的方法整体重写为恒 true。"""
    patched = 0
    mre = re.compile(method_re)
    for p in _iter_smali_files(outdir):
        fn = os.path.basename(p)
        if file_frag not in fn:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
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
                patched += 1
                changed = True
                log("[%s] 方法恒 true: %s %s" % (tag, os.path.basename(p), header.strip()[:90]))
            else:
                new_methods.append((header, body))
        if changed:
            _backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_join_methods(head, new_methods))
    if not patched:
        log("[%s] 未找到匹配方法（SDK 版本差异？）" % tag)
    return patched


def patch_null_branches(outdir, signatures, log, tag="null 分支"):
    """
    通用 null 分支补丁：
    对每个含调用签名（如 AccessLevel;->isActive()Z）的方法，找 move-result 寄存器，
    在调用点后的窗口内找"null 路径"的赋值（move V, W / const/4 V, 0x0，W 为方法内
    赋过 0 的寄存器），改成 const/4 V, 0x1。
    """
    frags = [s.lstrip("L") for s in signatures]
    patched_files = 0
    for p in _iter_smali_files(outdir):
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        if not any(frag in txt for frag in frags):
            continue
        head, methods = _split_methods(txt)
        changed = False
        out_methods = []
        for header, body in methods:
            if not any(frag in body for frag in frags):
                out_methods.append((header, body))
                continue
            body_lines = body.splitlines(keepends=True)
            # 本方法内被赋过 0 的寄存器
            zero_regs = set(re.findall(r"const/4\s+(\w+),\s*0x?0\b", body))
            hit = False
            for i, line in enumerate(body_lines):
                if not any(frag in line for frag in frags):
                    continue
                # move-result 寄存器
                res_reg = None
                for j in range(i + 1, min(i + 6, len(body_lines))):
                    m2 = re.match(r"\s*move-result\s+(\w+)", body_lines[j])
                    if m2:
                        res_reg = m2.group(1)
                        break
                if not res_reg:
                    continue
                # 赋值目标寄存器：Boolean.valueOf 的入参（若有），否则 move-result 寄存器
                target_reg = res_reg
                for j in range(i + 1, min(i + 45, len(body_lines))):
                    if "Boolean;->valueOf" in body_lines[j]:
                        m7 = re.search(r"\{([^}]*)\}", body_lines[j])
                        if m7:
                            args = [a.strip() for a in m7.group(1).split(",") if a.strip()]
                            if args:
                                target_reg = args[0]
                        break
                # 窗口：调用点后 45 行（覆盖 valueOf / 跳转 / label）
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
                    # label 后 3 行内找 null 赋值
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
                    log("[%s] 恒 true: %s 行 %d（调用 %s）"
                        % (tag, os.path.relpath(p, outdir), j + 2,
                           [f for f in frags if f in line][0][:50]))
                    break
            if hit:
                changed = True
            out_methods.append((header, "".join(body_lines)))
        if changed:
            _backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_join_methods(head, out_methods))
            patched_files += 1
    if not patched_files:
        log("[%s] 未定位到 null 分支（新用户 profile 无 premium level 时可能仍显示未订阅）" % tag)
    return patched_files


# ---------------------------------------------------------------- SDK 模板应用

def apply_sdk_templates(outdir, log):
    """按检测到的 SDK 应用确定性模板。返回 (sdk 列表, 补丁数)。"""
    found = detect_sdks(outdir)
    if not found:
        return found, 0
    log("[SDK] 检测到订阅 SDK: %s" % "、".join(found))
    total = 0
    for sdk in found:
        tpl = SDK_TEMPLATES.get(sdk)
        if not tpl:
            continue
        for g in tpl.get("getters", []):
            # 按文件名找，且文件内容含类描述符（避免同名类误伤）
            for p in _iter_smali_files(outdir):
                if os.path.basename(p) != g["file"]:
                    continue
                try:
                    with open(p, encoding="utf-8") as f:
                        head = f.read(8192)
                except Exception:
                    continue
                if g["class"] not in head:
                    continue
                total += _patch_getter_file(p, g["class"], g["field"],
                                            g["methods"], log, sdk)
        for b in tpl.get("body_true", []):
            total += _patch_body_true_files(outdir, b["file"], b["method_re"], log, sdk)
        if tpl.get("null_signatures"):
            total += patch_null_branches(outdir, tpl["null_signatures"], log, sdk)
    return found, total


# ---------------------------------------------------------------- 缓存层规则（第 5 层）

PREMIUM_KEY_RE = re.compile(
    r"(premium|is_?pro|vip|unlock|subscri|paid|entitle|active_?sub|adfree|"
    r"ads_?free|remove_?ads|purchase|member|gold|silver)",
    re.I)


def patch_cache_flags(outdir, log):
    """
    缓存层确定性补丁：
    const-string vS, "premium 相关 key" + SharedPreferences/FirebaseRemoteConfig
    的 getBoolean(...)Z + move-result vN → 把 vN 改成 const/4 1。
    """
    patched = 0
    # SharedPreferences: getBoolean(Ljava/lang/String;Z)Z（双参）
    # FirebaseRemoteConfig: getBoolean(Ljava/lang/String;)Z（单参）
    getbool_re = re.compile(r"->getBoolean\(Ljava/lang/String;Z?\)Z")
    for p in _iter_smali_files(outdir):
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        changed = False
        i = 0
        while i < len(lines):
            m = re.match(r'(\s*)const-string\s+(\w+),\s*"([^"]+)"', lines[i])
            if not m or not PREMIUM_KEY_RE.search(m.group(3)):
                i += 1
                continue
            key = m.group(3)
            sreg = m.group(2)
            # 之后 8 行内找 getBoolean 调用，且第一参数是 sreg
            done = False
            for j in range(i + 1, min(i + 9, len(lines))):
                if not getbool_re.search(lines[j]):
                    continue
                args = re.search(r"\{([^}]*)\}", lines[j])
                if not args:
                    continue
                arg_list = [a.strip() for a in args.group(1).split(",") if a.strip()]
                # key 寄存器出现在参数列表任一位置即可（invoke-interface/static 是第一个，
                # invoke-virtual 时 this 占第一个、key 是第二个）
                if not arg_list or sreg not in arg_list:
                    continue
                # 之后 4 行内找 move-result
                for k in range(j + 1, min(j + 5, len(lines))):
                    m2 = re.match(r"(\s*)move-result\s+(\w+)", lines[k])
                    if m2:
                        lines[k] = "%sconst/4 %s, 0x1\n" % (m2.group(1), m2.group(2))
                        patched += 1
                        changed = True
                        log("[缓存] %s -> true: %s 行 %d"
                            % (key, os.path.relpath(p, outdir), k + 1))
                        done = True
                        break
                if done:
                    break
            i += 1
        if changed:
            _backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(lines)
    if not patched:
        log("[缓存] 未发现 premium 相关本地缓存读点")
    return patched


# ---------------------------------------------------------------- 判断链扫描（第 2 层）

BUSINESS_METHOD_RE = re.compile(
    r"\.method\s+[\w\s$]*?\s(isPremium|getIsPremium|isPremiumUser|isSubscribed|"
    r"getIsSubscribed|hasActiveSub|isActiveSubscription|hasEntitlement|isEntitled|"
    r"checkEntitlement|isPro|isVip|isUnlocked|isUnlock|isAdFree|isPaid|isPurchased|"
    r"hasPro|canAccessPremium|premiumEnabled|isPremiumActive|isUserPremium)"
    r"\(([^)]*)\)Z", re.I)

# 业务判断方法体内必须出现的"实质信号"（SDK 调用 / billing / 缓存读 / RC）
SIGNAL_RE = re.compile(
    r"->(isActive|getIsActive|isSubscribed|hasActiveSubscription|isActiveSubscription|"
    r"isPremium|getIsPremium|hasPremium|isUnlocked|isAdFree)\(\)Z"
    r"|queryPurchases|getCustomerInfo|EntitlementInfo|SubscriptionStatus"
    r"|->getBoolean\(|launchBillingFlow|onPurchaseUpdated|BillingResult|getPurchases",
    re.I)

# 判断链扫描跳过的目录（SDK 自身 / 系统 / 广告 SDK / 加固）
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


def scan_business_reads(outdir, log, progress=None):
    """
    判断链扫描：业务代码（非 SDK/系统目录）里返回 boolean 的 premium 判断方法，
    方法体短且含实质信号（SDK getter / billing / 缓存读）→ 恒 true。
    """
    patched = 0
    files = [p for p in _iter_smali_files(outdir)
             if not any(f in p.replace(os.sep, "/").lower() for f in SKIP_DIR_FRAGS)]
    total = max(1, len(files))
    for fi, p in enumerate(files, 1):
        if progress and (fi == total or fi % 100 == 0):
            progress(int(fi * 100 / total))
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        if not SIGNAL_RE.search(txt):
            continue
        head, methods = _split_methods(txt)
        changed = False
        new_methods = []
        for header, body in methods:
            m = BUSINESS_METHOD_RE.search(header)
            if not m or not SIGNAL_RE.search(body):
                new_methods.append((header, body))
                continue
            if _count_instructions(body.splitlines()) > MAX_SCAN_METHOD_INSTRUCTIONS:
                log("[判断链] 跳过（方法过长）: %s %s"
                    % (os.path.relpath(p, outdir), header.strip()[:80]))
                new_methods.append((header, body))
                continue
            nb = _method_true_body(header)
            if nb is None:
                new_methods.append((header, body))
                continue
            new_methods.append(nb)
            patched += 1
            changed = True
            log("[判断链] %s -> 恒 true: %s" % (m.group(1), os.path.relpath(p, outdir)))
        if changed:
            _backup(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(_join_methods(head, new_methods))
    if not patched:
        log("[判断链] 未发现可补丁的业务判断方法")
    return patched


# ---------------------------------------------------------------- 总入口

def apply_all(outdir, log, progress=None):
    """三层确定性补丁。返回 (检测到的 SDK 列表, 总补丁数)。"""
    found, n1 = apply_sdk_templates(outdir, log)
    if progress:
        progress(40)
    n2 = patch_cache_flags(outdir, log)
    if progress:
        progress(60)
    n3 = scan_business_reads(outdir, log, progress)
    return found, n1 + n2 + n3
