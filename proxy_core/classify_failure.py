"""失败面 × hard/transient 分类（吸收-1/2 §R-D，仅 R-D 依赖）。

用途：会话粘性 rebind 分级。quota 档（config `routing.rebind_on` =
"quota"）要按失败**性质**决定绑定处置：

- ``TRANSIENT``：本次 failover 换号，但会话绑定**保持**（保 prompt
  cache——原号只是这一笔不顺，粘回去还有缓存收益）；
- ``HARD``：**持久清除**会话绑定（配额/认证族错误，粘回原号只会
  重复撞同一堵墙）。

默认档（rebind_on 缺省）不查本表——现状语义零改动（失败一律不动
绑定），本模块的存在不改变任何既有行为。

完整映射表（规格 §R-D v2 表，v3 修正 F-2/F-3/F-7）。"来源"列如实
标注判据出处：既有库内语义（FAILOVER_UPSTREAM_STATUSES /
_penalize_account）优先，CPA（CLIProxyAPI）语义引入的新判据逐行
标注——库内既有的 429「按 Retry-After 等待后原样重发」路径与这里的
"429 属 hard"并存不冲突（前者管"这一笔怎么重试"，后者管"绑定是否
保留"）。

======================  ==================  ==============================
失败面                    归类                 依据/来源
======================  ==================  ==============================
peek inband_error        二次归类：            proxy.py:_gate_classify（R-D
                         429/401/402/403/404  F-2 改造：载荷随 reason 传出）
                         →HARD；5xx/其余
                         →TRANSIENT
peek empty_stream        TRANSIENT             proxy.py:691
peek budget_exhausted    HARD（配额族）        proxy.py:694
peek no_content_timeout  TRANSIENT             proxy.py:705
peek_error ReadTimeout   TRANSIENT（CPA 源）   proxy.py:727
peek_error 连接重置/其它  TRANSIENT             proxy.py:727
OAuth invalid_grant      HARD（认证族，       CPA 源（cline/minicode token
                         cline/minicode 语义） 刷新失败语义）
流中途截断 saw_done=False TRANSIENT             proxy.py:883-886
非流式 401/402/403       HARD                  FAILOVER_UPSTREAM_STATUSES
                                               （proxy.py:339）/ _penalize
非流式 429               HARD（CPA 源新增判据） proxy.py:339 对照（库内 429
                                               走"等 Retry-After 重发"）
非流式 404               HARD（CPA 源新增判据） proxy.py:339 对照（404 不在
                                               FAILOVER 集合）
非流式 5xx               TRANSIENT             proxy.py:339 对照
非流式 408               TRANSIENT（CPA 源）   proxy.py:339 对照
_stream_open 连接失败    TRANSIENT             proxy.py:464
======================  ==================  ==============================
"""

import re

HARD = "hard"
TRANSIENT = "transient"

# ── HTTP 形状态码归类 ────────────────────────────────────────────
# 401/402/403：库内既有"凭证/配额故障"语义（FAILOVER_UPSTREAM_STATUSES）
# 429/404：CPA 源新增硬判据（429=配额族、404=该模型在该账号上不存在）
HARD_STATUSES = frozenset({401, 402, 403, 404, 429})
# 408：CPA 源标注的瞬时超时
TRANSIENT_STATUSES = frozenset({408})

# ── peek 判负 reason ─────────────────────────────────────────────
INBAND_ERROR = "inband_error"
# 静态 reason → 归类（不带载荷的形态）
PEEK_REASON_MAP = {
    "empty_stream": TRANSIENT,        # proxy.py:691
    "budget_exhausted": HARD,         # proxy.py:694（配额族）
    "no_content_timeout": TRANSIENT,  # proxy.py:705
}
# "peek_error: <异常类名>"：读超时 / 连接重置 / 其它读流异常一律瞬时
# （CPA 源）——peek 只看流开头，单账号流内死亡不代表账号坏了
PEEK_ERROR_PREFIX = "peek_error:"

# OAuth 刷新失败语义（cline/minicode 通道）：token 换发被拒属于认证族
_INVALID_GRANT_HINTS = (
    "invalid_grant",
    "invalid grant",
    "invalid_refresh_token",
    "refresh token has been revoked",
    "token has been revoked",
    "refresh token expired",
)


def _coerce_status(v):
    """把候选取值收成 100..599 的 int；不合形返回 None（bool 不算数字）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 100 <= v <= 599 else None
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit() and 100 <= int(s) <= 599:
            return int(s)
    return None


def classify_http_status(status) -> str:
    """HTTP 形状态码 → hard/transient（非流式路径与 inband 二次归类共用）。

    未列出的状态码（400/422 等请求体类错误）保守归 TRANSIENT——它们
    本来就不换号、也不该清绑定；"宁可保留绑定"不会造成重复撞墙（真撞
    墙的是 401/402/403/429 这些已归 HARD 的）。
    """
    st = _coerce_status(status)
    if st is None:
        return TRANSIENT
    if st in HARD_STATUSES:
        return HARD
    if st in TRANSIENT_STATUSES or 500 <= st <= 599:
        return TRANSIENT
    return TRANSIENT


def inband_status(reason):
    """从 peek 判负 reason 里取 inband error 的 HTTP 形状态码（取不到 None）。"""
    if isinstance(reason, tuple):
        if reason and reason[0] == INBAND_ERROR:
            return _coerce_status(reason[1] if len(reason) > 1 else None)
        return None
    s = str(reason or "")
    if s.startswith(INBAND_ERROR + ":"):
        return _coerce_status(s.split(":", 1)[1])
    return None


def classify_peek_reason(reason) -> str:
    """peek 判负 reason → hard/transient（规格表逐行）。

    收两种 reason 形态，消费方无需区分：
    - 元组 ("inband_error", status)：R-D F-2 改造后的主形态（带载荷）；
    - 字符串："inband_error"（无载荷→TRANSIENT 保守）/ "inband_error:429" /
      "empty_stream" / "budget_exhausted" / "no_content_timeout" /
      "peek_error: ReadTimeout" 等。
    未知形态一律 TRANSIENT（保守，绝不误清绑定）。
    """
    if isinstance(reason, tuple):
        if reason and reason[0] == INBAND_ERROR:
            return classify_http_status(reason[1] if len(reason) > 1 else None)
        return TRANSIENT
    s = str(reason or "").strip()
    if s == INBAND_ERROR:
        return TRANSIENT            # 无载荷：无从二次归类，保守
    if s.startswith(INBAND_ERROR + ":"):
        return classify_http_status(s.split(":", 1)[1])
    if s in PEEK_REASON_MAP:
        return PEEK_REASON_MAP[s]
    if s.startswith(PEEK_ERROR_PREFIX):
        return TRANSIENT            # ReadTimeout / 连接重置 / 其它读流异常
    return TRANSIENT


def classify_stream_end(saw_done: bool) -> str | None:
    """流收尾状态 → 归类；None = 无失败（正常以 [DONE] 收尾）。"""
    return None if saw_done else TRANSIENT


def is_invalid_grant(message) -> bool:
    """消息里是否出现 OAuth 刷新失败语义（cline/minicode token 语义）。"""
    low = str(message or "").lower()
    return any(h in low for h in _INVALID_GRANT_HINTS)


def classify_credential_error(message) -> str:
    """凭证/刷新类错误消息 → 归类。

    invalid_grant（刷新令牌被撤销/过期）属认证族 → HARD：粘回原号
    只会再撞一次同样的 401，必须清绑定换号。
    """
    return HARD if is_invalid_grant(message) else TRANSIENT


def classify_open_error(exc=None) -> str:
    """_stream_open / 发送阶段的连接类异常 → 归类（transient）。

    连接失败（ConnectError/ReadTimeout/SSL 瞬断）是网络层瞬时面；
    唯一例外是异常消息里带 OAuth 刷新失败语义（认证族 → HARD）。
    """
    if exc is not None and is_invalid_grant(exc):
        return HARD
    m = re.search(r"([1-5]\d\d)", str(exc or ""))
    if m and "connect" not in str(exc or "").lower():
        return classify_http_status(m.group(1))
    return TRANSIENT


# 供测试逐行对账的映射表（(失败面, 归类)），顺序与上方文档表一致。
MAPPING_TABLE = (
    # (失败面描述, 归类 or "inband" 标记)
    ("peek.inband_error(429)", HARD),
    ("peek.inband_error(401)", HARD),
    ("peek.inband_error(402)", HARD),
    ("peek.inband_error(403)", HARD),
    ("peek.inband_error(404)", HARD),
    ("peek.inband_error(5xx)", TRANSIENT),
    ("peek.inband_error(other/none)", TRANSIENT),
    ("peek.empty_stream", TRANSIENT),
    ("peek.budget_exhausted", HARD),
    ("peek.no_content_timeout", TRANSIENT),
    ("peek.peek_error.ReadTimeout", TRANSIENT),
    ("peek.peek_error.connection_reset", TRANSIENT),
    ("oauth.invalid_grant", HARD),
    ("stream.truncated(saw_done=False)", TRANSIENT),
    ("nonstream.401", HARD),
    ("nonstream.402", HARD),
    ("nonstream.403", HARD),
    ("nonstream.429", HARD),
    ("nonstream.404", HARD),
    ("nonstream.500", TRANSIENT),
    ("nonstream.502", TRANSIENT),
    ("nonstream.503", TRANSIENT),
    ("nonstream.504", TRANSIENT),
    ("nonstream.408", TRANSIENT),
    ("open.connection_failure", TRANSIENT),
)
