# -*- coding: utf-8 -*-
"""
AI 补丁引擎（DeepSeek 规划 + 工具执行）。

确定性补丁（Adapty/byelab）覆盖不了的订阅 SDK，把 smali 候选代码发给 DeepSeek，
让它输出 JSON 补丁方案：{patches: [{file, find, replace, reason}], summary}。
工具只做机械执行：精确字符串替换 + 验证 + 写回，不给 AI 文件权限。
"""
import os
import re
import json
import urllib.request

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
CONFIG_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "PremiumUnlocker", "config.json")

# 订阅 SDK 特征签名（dex 字节里搜这些 ASCII 片段）
SDK_SIGNATURES = [
    ("adaptytech", "Adapty"),
    ("revenuecat", "RevenueCat"),
    ("qonversion", "Qonversion"),
    ("apphud", "Apphud"),
    ("purchasely", "Purchasely"),
    ("glassfy", "Glassfy"),
    ("billingclient", "Google Play Billing"),
]


def detect_sdks_from_apk(apk_path):
    """不解包反编译，直接扫 dex 字节找订阅 SDK 签名（秒级）。"""
    found = []
    if not apk_path or not os.path.isfile(apk_path):
        return found
    try:
        with zipfile.ZipFile(apk_path) as z:
            for n in z.namelist():
                if not n.endswith(".dex"):
                    continue
                data = z.read(n)
                for sig, name in SDK_SIGNATURES:
                    if name not in found and sig.encode() in data:
                        found.append(name)
    except Exception:
        pass
    return found


def classify_sdks(found):
    """把 SDK 检测结果翻译成解锁建议。"""
    if not found:
        return "未识别出订阅 SDK，可能服务端 entitlement 或自研——AI 兜底（可能解不了）"
    if "Adapty" in found:
        return "Adapty 系：内置确定性补丁，直接解锁"
    managers = [f for f in found if f != "Google Play Billing"]
    if managers:
        return "%s：非 Adapty，走 AI 兜底分析补丁" % "、".join(managers)
    return "仅直连 Google Play Billing：走 AI 兜底分析补丁"

# 收集上下文时的 smali 命中模式（方法名/字段名层面）
INTEREST_PATTERNS = [
    "isPremium", "isSubscribed", "hasActiveSub", "isActiveSubscription",
    "getIsPremium", "getPremium", "premium_active", "is_premium",
    "queryPurchases", "queryPurchasesAsync", "getPurchaseHistory",
    "hasEntitlement", "isEntitled", "checkEntitlement",
    "isPro", "isVip", "isUnlocked", "isUnlock", "isAdFree", "isPaid",
    "launchBillingFlow", "querySkuDetails", "getPurchase",
]
# 候选片段里必须出现的"实质"关键词（避免噪音）
MUST_HAVE = ("premium", "purchase", "subscription", "entitle", "isactive", "paid",
             "pro", "vip", "unlock", "reward", "billing", "sku", "adfree")

# 字符串字面量关键词（const-string 里的付费信号；避免 pro/vip 这类短词的噪音）
STRING_KEYWORDS = ("premium", "unlock", "subscribe", "subscription",
                   "is_premium", "is_pro", "is_vip", "entitle", "reward",
                   "purchase", "sku")

SYSTEM_PROMPT = """你是 Android smali 逆向补丁专家。目标：绕过 app 的客户端订阅/premium 限制（竞品拆解用途）。

你会收到：app 订阅相关 SDK 信息、premium 相关字符串、smali 候选代码片段。请找出 premium/订阅判断链，输出最小 smali 补丁方案，让判断恒为 true。

注意：app 的 premium 可能是多种形态——本地购买查询（BillingClient/queryPurchases）、SharedPreferences 布尔标志（is_premium/pro 等 key）、Firebase remote config 布尔、每日看广告解锁状态、或服务端 entitlement 下发。请综合判断，找到真正的客户端读点。

规则（必须遵守）：
1. 输出 JSON：{"patches":[{"file":"相对路径（/ 分隔，如 smali/com/x/a.smali）","find":"文件中精确存在的一段文本","replace":"替换后的文本","reason":"一句话说明"}],"summary":"总体说明"}
2. find 必须与文件内容逐字符精确匹配（含缩进）；不确定就放弃该补丁
3. 每次补丁最小化：优先改 getter/判断函数的返回值（const/4 vX, 0x1 + return），或把 false 分支改成 true 分支
4. 只 patch 客户端读点（isActive/premium 判断、SharedPreferences 读、布尔字段 getter）；不要伪造购买流程、不要改网络请求
5. 注意 null 分支：判断链里 "level != null && level.isActive()" 这类短路，null 时也要恒 true
6. 若 premium 状态是服务端下发的（客户端没有可 patch 的本地判断点），patches 输出空数组，summary 说明原因
7. smali 语法必须严格合法（否则重打包会失败）：
   - 十六进制/数值字面量必须带 0x 前缀，如 const/4 v0, 0x1；禁止 1b、0x10b 这类非法后缀
   - 只允许修改现有指令的操作数，或把某个 false 分支（move vX, 0）改成 true（move vX, 1）
   - 不要凭空新增寄存器、指令、label；寄存器命名沿用片段里已有的 v0/v1/...
   - find 和 replace 都必须逐字符来自片段文本，不要臆造
8. 只输出 JSON，不要任何其他文字"""


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def has_ai():
    cfg = load_config()
    return bool(cfg.get("deepseek_key")) and cfg.get("ai_enabled", True)


# ---------------------------------------------------------------- 上下文收集

def detect_sdks(apktool_out, log):
    """在 smali 目录树里找订阅相关 SDK。"""
    found = []
    markers = {
        "adapty": "Adapty",
        "revenuecat": "RevenueCat",
        "billingclient": "Google Play Billing",
        "apphud": "Apphud",
        "qonversion": "Qonversion",
        "adaptytech": "Adapty(api)",
    }
    for root, dirs, _files in os.walk(apktool_out):
        base = root.replace(os.sep, "/").lower()
        for k, v in markers.items():
            if k in base and v not in found:
                found.append(v)
    log("[AI] 检测到 SDK: %s" % ("、".join(found) if found else "未知（直连 Billing 或自研）"))
    return found


def _iter_smali(apktool_out):
    for root, _dirs, files in os.walk(apktool_out):
        for fn in files:
            if fn.endswith(".smali"):
                yield os.path.join(root, fn)


def _premium_strings(apktool_out, limit=60):
    """从 strings.xml 提取 premium/pro/unlock/subscribe 相关文案。"""
    out = []
    for root, _dirs, files in os.walk(os.path.join(apktool_out, "res")):
        for fn in files:
            if fn != "strings.xml":
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            for m in re.finditer(r'<string[^>]*name="([^"]+)"[^>]*>(.*?)</string>',
                                 content, re.S):
                name, val = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
                if any(k in name.lower() or k in val.lower()
                       for k in ("premium", "unlock", "subscribe", "subscription",
                                 "reward", "entitle", "purchase", "is_pro",
                                 "is_vip", "is_premium", "go_premium")):
                    out.append('%s = %s' % (name, val[:80]))
                    if len(out) >= limit:
                        return out
    return out


def collect_candidates(apktool_out, log, max_files=16, ctx_bytes=70000):
    """多信号源收集订阅/premium 判断候选片段（限长）。"""
    parts = []
    total = 0

    def add(title, text):
        nonlocal total
        if not text:
            return True
        block = "### %s\n%s\n" % (title, text)
        if total + len(block) > ctx_bytes:
            return False
        parts.append(block)
        total += len(block)
        return True

    # 1) premium 相关字符串
    strs = _premium_strings(apktool_out)
    if strs:
        add("premium 相关字符串（strings.xml）", "\n".join(strs))
        log("[AI] 提取 premium 字符串 %d 条" % len(strs))

    # 2) smali 多信号扫描，收集 (path, line, reason)
    hits = []  # (path, line_no, reason)
    log("[AI] 扫描 smali 订阅判断候选（多信号）...")
    for p in _iter_smali(apktool_out):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    low = line.lower()
                    reason = None
                    if any(k.lower() in low for k in INTEREST_PATTERNS) \
                            and any(k in low for k in MUST_HAVE):
                        reason = "方法名/字段命中"
                    elif "const-string" in low and any(
                            k in low for k in STRING_KEYWORDS):
                        reason = "字符串字面量"
                    elif "querypurchases" in low or "launchbillingflow" in low \
                            or "billingclient" in low or "/purchase" in low \
                            or "skudetails" in low:
                        reason = "billing 调用"
                    elif "sharedpreferences;->getboolean" in low \
                            or "sharedpreferences;->getstring" in low:
                        reason = "SharedPreferences 读"
                    elif "remoteconfig" in low or "getboolean" in low and "firebase" in low:
                        reason = "remote config"
                    if reason:
                        hits.append((p, i, reason))
                        break
        except Exception:
            continue
        if len(hits) >= 400:
            break
    if not hits:
        log("[AI] 未命中任何订阅候选（可能纯服务端 entitlement）")
        if not strs:
            return None, "未找到订阅判断候选，且无 premium 字符串"
        return parts, None

    # 按文件聚合，取命中最多/最靠前的文件，截取相关方法
    files = {}
    for p, ln, _r in hits:
        files.setdefault(p, []).append(ln)
    picked = sorted(files.items(), key=lambda kv: -len(kv[1]))[:max_files]
    snippets = 0
    for p, lines in picked:
        with open(p, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        for ln in lines:
            start = max(0, ln - 30)
            for j in range(ln, start, -1):
                if content[j].startswith(".method"):
                    start = j
                    break
            end = min(len(content), ln + 50)
            for j in range(ln, end):
                if content[j].strip() == ".end method":
                    end = j + 1
                    break
            rel = os.path.relpath(p, apktool_out).replace(os.sep, "/")
            block = "".join(content[start:end])
            if not add("候选 %s (行 %d)" % (rel, ln + 1), block):
                break
            snippets += 1
            break
        if total >= ctx_bytes:
            break
    if snippets == 0:
        return None, "候选片段提取失败"
    log("[AI] 提取 %d 个候选片段，共 %.0f KB" % (snippets, total / 1024))
    return parts, None


# ---------------------------------------------------------------- DeepSeek 调用

def ask_deepseek(key, model, user_content, log):
    body = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        })
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        log("[AI] DeepSeek 请求失败 HTTP %s: %s" % (e.code, e.read()[:200]))
        return None
    except Exception as e:
        log("[AI] DeepSeek 请求异常: %s" % e)
        return None


def parse_plan(text, log):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        log("[AI] 返回内容不是合法 JSON，放弃应用")
        return None


# ---------------------------------------------------------------- 补丁应用

# 已应用补丁的原文件备份（path -> 原内容），build 失败时可回滚
BACKUP = {}


def rollback(log=None):
    """回滚所有 AI 已应用的补丁（重打包因 AI 补丁失败时调用）。"""
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
        log("[AI] 已回滚 %d 个 AI 补丁文件（补丁导致 smali 错误）" % n)
    return n


def apply_patches(apktool_out, plan, log):
    patches = plan.get("patches") or []
    if not patches:
        log("[AI] AI 未能给出补丁方案: %s" % plan.get("summary", "无"))
        return 0
    applied = 0
    for p in patches:
        rel = (p.get("file") or "").replace("\\", "/")
        find = p.get("find") or ""
        replace = p.get("replace") or ""
        if not rel or not find:
            continue
        path = os.path.normpath(os.path.join(apktool_out, *rel.split("/")))
        if not os.path.isfile(path):
            log("[AI] 跳过（文件不存在）: %s" % rel)
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        if content.count(find) != 1:
            log("[AI] 跳过（find 匹配 %d 次，需唯一）: %s — %s"
                % (content.count(find), rel, p.get("reason", "")))
            continue
        if path not in BACKUP:
            BACKUP[path] = content
        content = content.replace(find, replace)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        applied += 1
        log("[AI] 已应用补丁: %s — %s" % (rel, p.get("reason", "")))
    return applied


def run(apktool_out, log):
    """AI 解锁入口：收集上下文 → DeepSeek 规划 → 应用补丁。"""
    cfg = load_config()
    key = cfg.get("deepseek_key")
    if not key:
        log("[AI] 未配置 DeepSeek Key，跳过")
        return False
    log("[AI] 开始 AI 订阅解锁分析（DeepSeek %s）..." % (cfg.get("model") or DEFAULT_MODEL))
    sdks = detect_sdks(apktool_out, log)
    parts, err = collect_candidates(apktool_out, log)
    if err:
        log("[AI] %s" % err)
        return False
    user = ("App 反编译目录: apktool_out\n"
            "检测到的订阅相关 SDK: %s\n"
            "以下是从 smali/strings.xml 提取的订阅/premium 判断线索（片段不完整，补丁时 find 必须与文件精确匹配）：\n\n%s\n\n"
            "请按规则输出补丁 JSON 方案。"
            % ("、".join(sdks) if sdks else "未知（可能直连 Billing / 服务端 entitlement / 广告解锁）",
               "\n".join(parts)))
    log("[AI] 请求 DeepSeek 规划补丁 ...")
    raw = ask_deepseek(key, cfg.get("model"), user, log)
    if raw is None:
        return False
    plan = parse_plan(raw, log)
    if plan is None:
        return False
    n = apply_patches(apktool_out, plan, log)
    log("[AI] AI 补丁完成: 应用 %d 处。%s" % (n, plan.get("summary", "")))
    return n > 0
