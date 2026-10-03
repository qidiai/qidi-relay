# -*- coding: utf-8 -*-
"""语料脱敏（阶段 E / 方案 §7.3）：纯函数零 IO。

采集语料落盘前在 QueueWorker 线程内执行（§7.3 红线：
请求线程只入队，绝不在请求线程脱敏）。

类目（每类在 tests/test_redact.py 至少 1 正 1 负用例）：
  平台 token 前缀族：sk-（含变体前缀，长度阈值 {20,}
    起步——magpie 同阈值，本仓自主选择）/ ghp_ gho_
    ghu_ ghs_ ghr_ / github_pat_ / glpat- / AIza /
    xox[abposr]- / hf_ / gsk_ / xai- / npm_ / pypi- /
    dop_v1_ / SG. / sk_live_ rk_live_ / AKIA ASIA
  私钥块：PEM BEGIN/END 整块抹除
  URL 内嵌凭证：user:password@host
  按字段名结构化探测：password / api_key / access_token /
    client_secret / secret / token 类键的值整值抹除
    （对 dict/list 递归——比纯正则强，magpie
    redact.go:73-79 同思路）
  个人数据：邮箱（默认打码）/ 手机号 / 身份证（含校验
    位——简化为位数规则）/ 银行卡（luhn 可选简化——
    按 13-19 位数字规则）/ 内网 IPv4 / IPv6 link-local
  路径：整段绝对路径打码至盘符（云端实测语料含
    G:\\qidicode 工作区路径与 AGENTS.md 全文，不止
    用户目录形态）
  用户自定义规则：config corpus.redact_extra（词/正
    则列表）——本模块纯函数零 IO，规则由调用方解析
    后作为 extra 传入（parse_extra_rules）

接口：
  redact_text(s, extra=None) -> (s, hits)
  redact_messages(messages, extra=None) -> (messages, hits)
"""

import re

__all__ = ["MASK", "redact_text", "redact_messages",
           "parse_extra_rules", "is_sensitive_key"]

# 抹除占位符（全部规则共用）
MASK = "[REDACTED]"

# ─── 平台 token 前缀族（§7.3）─────────────────────────────
# sk- 长度阈值按 {20,} 起步（magpie 同款阈值；本仓自主
# 选择——8899 实测无合法短前缀 token 会被误伤）。
_TOKEN_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|gsk_[A-Za-z0-9_-]{20,}"
    r"|xai-[A-Za-z0-9_-]{20,}"
    r"|npm_[A-Za-z0-9]{20,}"
    r"|pypi-[A-Za-z0-9_-]{20,}"
    r"|dop_v1_[A-Za-z0-9]{20,}"
    r"|SG\.[A-Za-z0-9_-]{20,}"
    r"|(?:sk|rk)_live_[A-Za-z0-9]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16,}"
    r")")

# ─── PEM 私钥块：整块抹除 ────────────────────────────────
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r".*?"
    r"-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL)

# ─── URL 内嵌凭证 user:password@host（整段 userinfo 抹除；
# host 允许域名/IPv4/localhost——内网 IP URL 的凭证明文
# 残留同样要打掉，IP 本体由内网规则另行处理）──
_URL_CREDS_RE = re.compile(
    r"\b[A-Za-z0-9%._+\-]+:[A-Za-z0-9%._+\-]+@"
    r"([A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    r"|\d{1,3}(?:\.\d{1,3}){3}"
    r"|localhost)")

# ─── 个人数据 ────────────────────────────────────────────
# 邮箱（默认打码——§7.3 删原"可选"）
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# 手机号（大陆 11 位，1[3-9] 号段）
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
# 身份证（18 位含校验位——简化为位数规则：首位非零、末位可 X）
_ID_CARD_RE = re.compile(r"\b[1-9]\d{16}[0-9Xx]\b")
# 银行卡（luhn 可选简化——按 13-19 位数字规则；排在身份证
# 之后，18 位身份证不会被重复计数）
_BANK_CARD_RE = re.compile(r"\b\d{13,19}\b")
# 内网 IPv4：10/8、172.16/12、192.168/16、127/8、169.254/16
_PRIVATE_V4_RE = re.compile(
    r"\b("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|169\.254\.\d{1,3}\.\d{1,3}"
    r")")
# IPv6 link-local：fe80::/10（前缀 fe80-febf）
_IPV6_LL_RE = re.compile(
    r"\bfe[89ab][0-9a-fA-F]:(?:[0-9a-fA-F]{0,4}:){0,6}"
    r"[0-9a-fA-F]{0,4}")

# ─── 绝对路径：整段打码至盘符（保留盘符，其余抹除）──────────
_ABS_PATH_RE = re.compile(r"\b([A-Za-z]):[\\/][^\s\"'<>|]*")
# POSIX 绝对路径：/Users/…、/home/…（保留根前缀，其余抹除）
_POSIX_ABS_PATH_RE = re.compile(
    r"(?<![\w/])(/(?:Users|home)/)[^\s\"'<>|]*")

# 按字段名结构化探测的敏感键（词边界匹配——旧子串包含过宽，
# max_tokens/token_count 之类被误伤）：
# - 键名整体等于集合成员（大小写不敏感）；
# - 键名按 _/- 与驼峰边界分词后，任意位置含完整词
#   password/secret/credential（password_hash 等仍命中）；
# - 分词后词尾（头名词位置）为 token/key
#   （auth_token/openai_api_key 命中，token_count 不误伤）。
_SENSITIVE_KEY_EXACT = frozenset({
    "password", "api_key", "access_token",
    "client_secret", "secret", "token",
})
_SENSITIVE_KEY_ANYWHERE = frozenset(
    {"password", "secret", "credential"})
_SENSITIVE_KEY_HEAD = frozenset({"token", "key"})
_KEY_WORD_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[A-Z]+")

# 自定义规则"正则"的启发式判据：含任一元字符即按正则处理
_REGEX_METACHARS = set(".*+?[](){}|^$\\")


def _key_words(key) -> list:
    """键名按 _/- 与驼峰边界分词并小写。"""
    return [w.lower() for w in _KEY_WORD_RE.findall(key)]


def is_sensitive_key(key) -> bool:
    """字段名结构化探测（§7.3）：词边界/精确集合匹配——
    键名等于敏感词集合成员，或分词后含完整敏感词
    （password/secret/credential 任意位置；token/key
    仅词尾位置）。max_tokens/token_count 不误伤。"""
    if not isinstance(key, str):
        return False
    k = key.strip().lower()
    if k in _SENSITIVE_KEY_EXACT:
        return True
    words = _key_words(key)
    if not words:
        return False
    if any(w in _SENSITIVE_KEY_ANYWHERE for w in words):
        return True
    return words[-1] in _SENSITIVE_KEY_HEAD


def parse_extra_rules(extra) -> list:
    """解析 config corpus.redact_extra（词/正则列表）为
    已编译规则列表 [(kind, compiled)]。

    - 纯词（不含正则元字符）→ 字面量词，大小写不敏感；
    - 含元字符 → 按正则原样编译（大小写敏感）；
    - dict 形态 {"regex": "..."} → 显式按正则处理。
    坏型条目（非 str/dict、空串、编译失败）跳过。
    """
    rules = []
    if not isinstance(extra, list):
        return rules
    for item in extra:
        if isinstance(item, dict):
            pat = item.get("regex")
            if isinstance(pat, str) and pat:
                try:
                    rules.append(("regex", re.compile(pat)))
                except re.error:
                    continue
            continue
        if not isinstance(item, str) or not item:
            continue
        if any(c in _REGEX_METACHARS for c in item):
            try:
                rules.append(("regex", re.compile(item)))
            except re.error:
                continue
        else:
            rules.append(("word", re.compile(
                re.escape(item), re.IGNORECASE)))
    return rules


def redact_text(s, extra=None):
    """文本脱敏：全部类目规则依次执行。

    返回 (脱敏后文本, 命中数)；非字符串原样返回（防御）。
    extra：parse_extra_rules 的输出（corpus.redact_extra
    自定义规则——调用方解析传入，保持本模块纯函数零 IO）。
    """
    if not isinstance(s, str) or not s:
        return s, 0
    text = s
    hits = 0

    def _apply(pattern, repl):
        nonlocal text, hits
        text, n = pattern.subn(repl, text)
        hits += n

    # 顺序有语义：URL 凭证先于邮箱（否则 "pass@host"
    # 会被邮箱规则半截命中）；身份证先于银行卡（18 位
    # 身份证同时满足 13-19 位银行卡规则，先命中先抹除
    # 不重复计数）。
    _apply(_PEM_RE, MASK)
    _apply(_URL_CREDS_RE, MASK + r"@\1")
    _apply(_TOKEN_RE, MASK)
    _apply(_EMAIL_RE, MASK)
    _apply(_PHONE_RE, MASK)
    _apply(_ID_CARD_RE, MASK)
    _apply(_BANK_CARD_RE, MASK)
    _apply(_PRIVATE_V4_RE, MASK)
    _apply(_IPV6_LL_RE, MASK)
    # 整段绝对路径打码至盘符（保留盘符 + 根分隔符）
    _apply(_ABS_PATH_RE, r"\1:\\***")
    # POSIX 绝对路径打码至根前缀（保留 /Users/、/home/）
    _apply(_POSIX_ABS_PATH_RE, r"\1***")
    for _kind, rule in (extra or ()):
        _apply(rule, MASK)
    return text, hits


def redact_messages(messages, extra=None):
    """结构化消息脱敏（对 dict/list 递归）。

    - 敏感键（is_sensitive_key）→ 值整值抹除（§7.3 按
      字段名结构化探测，比纯正则强）；
    - 其余 str 值 → redact_text 全类目规则；
    - 其余类型原样保留。
    返回 (脱敏后结构, 命中数)。
    """
    if isinstance(messages, dict):
        out = {}
        hits = 0
        for k, v in messages.items():
            if isinstance(k, str) and is_sensitive_key(k):
                out[k] = MASK
                hits += 1
            elif isinstance(v, (dict, list)):
                nv, h = redact_messages(v, extra)
                out[k] = nv
                hits += h
            elif isinstance(v, str):
                nv, h = redact_text(v, extra)
                out[k] = nv
                hits += h
            else:
                out[k] = v
        return out, hits
    if isinstance(messages, list):
        out = []
        hits = 0
        for item in messages:
            if isinstance(item, (dict, list)):
                ni, h = redact_messages(item, extra)
                out.append(ni)
                hits += h
            elif isinstance(item, str):
                ni, h = redact_text(item, extra)
                out.append(ni)
                hits += h
            else:
                out.append(item)
        return out, hits
    if isinstance(messages, str):
        return redact_text(messages, extra)
    return messages, 0
