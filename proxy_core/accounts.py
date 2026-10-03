"""账号池管理：多账号加载、轮询、启停、GLM-5.3 能力标记、配额/鉴权冷却。"""

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime

from .settings import ACCOUNTS_FILE, load_config

log = logging.getLogger("qidi-relay")

# 402（team 预算耗尽）后的冷却窗口：窗口内选号跳过该账号，到点自动恢复可用。
# 官方 CLI 的真实请求即"半开探测"，不额外发任何探针请求。
QUOTA_COOLDOWN_S = 600
# 401/403（凭证失效、签名被上游安全门拒绝）冷却窗口。这类错误不是账号侧偶发，
# 上游版本门不会在 10 分钟内自己变好——窗口取更长，把"每次都撞门"压成"偶尔探一次"。
AUTH_COOLDOWN_S = 1800


class AccountManager:
    """管理主账号池（上游账号）的 token 和轮询状态。"""

    def __init__(self):
        self.accounts = []
        self.index = 0
        # 0906: 状态变更/落盘互斥锁（RLock 可重入，_save 在持锁段内调用安全）
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        """从 accounts.json 加载账号列表。"""
        if not ACCOUNTS_FILE.exists():
            log.warning("accounts.json 不存在，请添加账号")
            self.accounts = []
            return
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            accounts = data.get("accounts", []) if isinstance(data, dict) else data
            self.accounts = []
            for acc in accounts:
                aid = acc.get("user_id", "")
                token = acc.get("access_token", "")
                if not aid or not token:
                    continue
                self.accounts.append({
                    "user_id": aid,
                    "username": acc.get("username", ""),
                    "org_id": acc.get("org_id", ""),
                    "access_token": token,
                    "last_used": acc.get("last_used", ""),
                    "use_count": acc.get("use_count", 0),
                    "enabled": acc.get("enabled", True),
                    "has_glm53": acc.get("has_glm53", False),
                    "budget_exceeded": acc.get("budget_exceeded", False),
                    "budget_cooldown_until": acc.get("budget_cooldown_until", 0) or 0,
                    "cooldown_reason": acc.get("cooldown_reason", ""),
                    "quota_team": acc.get("quota_team", ""),
                    "group": acc.get("group", ""),
                })
            groups = sorted({a.get("group") or "默认" for a in self.accounts})
            log.info("已加载 %d 个账号（分组: %s）", len(self.accounts), ", ".join(groups))
        except Exception as e:
            log.error("读取 accounts.json 失败: %s", e)

    def _save(self):
        """持久化账号状态到 accounts.json（0906: 锁 + tmp+replace 原子写，
        消除 threaded 并发全量写盘的截断交错损坏风险）。"""
        try:
            with self._lock:
                tmp = str(ACCOUNTS_FILE) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"accounts": self.accounts}, f, ensure_ascii=False, indent=2)
                os.replace(tmp, str(ACCOUNTS_FILE))
        except Exception as e:
            log.error("保存 accounts.json 失败: %s", e)

    def _clear_expired_cooldowns(self):
        """冷却到期的账号恢复可用（选号时惰性清理，无后台线程）。

        旧数据里只有 budget_exceeded 标记、没有冷却时间戳的（永久禁用时代
        留下的），一律视为已到期直接放行。
        """
        with self._lock:
            now = time.time()
            recovered = []
            for a in self.accounts:
                if a.get("budget_exceeded") and now >= (a.get("budget_cooldown_until") or 0):
                    a["budget_exceeded"] = False
                    a["budget_cooldown_until"] = 0
                    a["cooldown_reason"] = ""
                    recovered.append(a["user_id"])
            if recovered:
                self._save()
                log.info("冷却到期，账号恢复可用: %s", ", ".join(recovered))

    def mark_cooldown(self, user_id: str, seconds: int, reason: str = "",
                      team: str | None = None) -> list:
        """把账号放进冷却窗口（不永久禁用，到点自愈，无需人工去面板点启用）。

        team 非空时，凡是已学到归属该 team 的账号一起冷却：LiteLLM 的预算挂在
        team 上而非账号上，同队换号必然还是同一个 402，提前拦掉就省掉这些
        注定失败的上游请求。归属关系是从错误体逐步学到的，所以每队首次
        命中仍会各打一次上游——这是不额外发探针的代价。
        返回实际被冷却的 uid 列表。
        """
        with self._lock:
            now = time.time()
            cooled = []
            for a in self.accounts:
                same_team = bool(team) and a.get("quota_team") == team
                if a["user_id"] != user_id and not same_team:
                    continue
                a["budget_exceeded"] = True
                a["budget_cooldown_until"] = now + seconds
                a["cooldown_reason"] = reason
                if team:
                    a["quota_team"] = team
                if a["user_id"] not in cooled:
                    cooled.append(a["user_id"])
            if cooled:
                self._save()
                log.warning("账号 %s 冷却 %d 秒（%s%s）", ", ".join(cooled), seconds, reason,
                            f"，team={team}" if team else "")
        return cooled

    # ── 402 渐进冷却（0905 吸收 ProxyDeck BudgetResetAt 思路）──────────────
    # 官网 period_end 需要每账号 access_token（池内只有 cli_api_key 拿不到），
    # 用阶梯逼近代替：第1次 30min → 第2次 90min → 第3次起 5h（Codely 5h 消费
    # 窗口的量级）。既避免 600s 固定冷却反复打 402 白烧上游，也不会过冷却太久。
    _402_strikes: dict = {}
    _402_last: dict = {}
    BUDGET_402_LADDER = (1800, 5400, 18000)

    def mark_budget_402(self, user_id: str, team: str | None = None):
        """402 配额耗尽的阶梯冷却，返回 (冷却秒数, 被冷却的 uid 列表）。"""
        with self._lock:
            now = time.time()
            if now - self._402_last.get(user_id, 0) > 6 * 3600:
                self._402_strikes[user_id] = 0  # 距上次 402 已超 6h：新窗口从头计
            strikes = self._402_strikes.get(user_id, 0)
            secs = self.BUDGET_402_LADDER[min(strikes, len(self.BUDGET_402_LADDER) - 1)]
            self._402_strikes[user_id] = strikes + 1
            self._402_last[user_id] = now
            cooled = self.mark_cooldown(user_id, secs,
                                        f"配额耗尽（402 第{strikes + 1}次）", team=team)
        return secs, cooled

    def next_recovery_in(self):
        """冷却中账号里最早恢复的剩余秒数；没有冷却中的账号返回 None。"""
        now = time.time()
        left = [(a.get("budget_cooldown_until") or 0) - now for a in self.accounts
                if a.get("budget_exceeded")]
        left = [x for x in left if x > 0]
        return round(min(left), 1) if left else None

    def get_next(self, model: str = None, loads: dict = None):
        """轮询获取下一个可用账号；请求 GLM 系模型时优先有 GLM-5.3 的账号。

        loads: {user_id: 当前进行中请求数}；提供时在候选中优先选负载最低的
        （智能路由：避免把新的长请求压到已在扛 3 分钟长流的账号上）。
        """
        if not self.accounts:
            return None
        # 冷却到期的账号先放回来，再构建候选
        self._clear_expired_cooldowns()
        enabled = [a for a in self.accounts
                   if a.get("enabled", True) and not a.get("budget_exceeded")]
        if not enabled:
            return None

        # ── 模型→账号分组路由（0831 定稿，0901 增 pro 档）──
        # 账号三档：group="" = Lite 订阅 / "free" = 无订阅免费号 / "pro" = Pro 订阅号
        # 规则：
        #   GLM-5.3-FLASH / KIMI-K3 → Lite 订阅池（未分组账号），不回退；
        #   其余模型 → free+pro 免费优先（Pro 套餐额度含免费模型用量），
        #   free+pro 全冷却后回退 Lite 订阅池。
        enabled_all = enabled  # 分组过滤前的全量快照（GLM 回退用）
        if model:
            mg = (load_config().get("model_groups") or {})
            group = mg.get(model) or mg.get(model.lower())
            if group == "free":
                # free 档：free + pro 一起算（pro 套餐含免费模型额度）
                in_group = [a for a in enabled
                            if (a.get("group") or "") in ("free", "pro")]
                if in_group:
                    enabled = in_group
                    log.info("分组路由: %s → free+pro 池（%d 个账号）",
                             model, len(in_group))
                else:
                    log.warning("分组回退: %s 的 free+pro 账号已全部冷却/禁用，回退订阅池 %s",
                                model, [a["user_id"] for a in enabled])
            elif group:
                in_group = [a for a in enabled if (a.get("group") or "") == group]
                if in_group:
                    enabled = in_group
                    log.info("分组路由: %s → %s 组（%d 个账号）",
                             model, group, len(in_group))
                else:
                    # 目标组全灭：回退订阅池（enabled 保持全量）
                    log.warning("分组回退: %s 的 %s 组账号已全部冷却/禁用，回退订阅池 %s",
                                model, group, [a["user_id"] for a in enabled])
            else:
                # 反向隔离：未绑定分组的模型（订阅池模型 GLM-5.3-FLASH/KIMI-K3）
                # 只用未分组（Lite 订阅）账号，不回退 free/pro 组。
                # 0905 修正：KIMI-K3 的直连权重账号横跨 lite+pro（has_glm53 集，
                # <账号ID>(lite) + <账号ID>(pro)），pro 不能被一刀切排除——否则 Lite
                # 订阅号全部冷却/额度耗尽时 K3 会整体 503。KIMI 先按 has_glm53
                # 锁定候选，锁定失败再走原隔离逻辑。
                if model and 'kimi' in model.lower():
                    kimi_accounts = [a for a in enabled if a.get('has_glm53', False)]
                    if kimi_accounts:
                        log.info("KIMI 直连路由: %s → has_glm53 账号 %s",
                                 model, [a["user_id"] for a in kimi_accounts])
                        enabled = kimi_accounts
                        ungrouped = kimi_accounts  # 与 enabled 相等，下方隔离分支不再改写
                    else:
                        ungrouped = [a for a in enabled if not (a.get("group") or "")]
                else:
                    ungrouped = [a for a in enabled if not (a.get("group") or "")]
                if ungrouped and len(ungrouped) < len(enabled):
                    log.info("分组隔离: %s 为订阅池模型，排除 %d 个 free/pro 组账号，仅用 %s",
                             model, len(enabled) - len(ungrouped),
                             [a["user_id"] for a in ungrouped])
                    enabled = ungrouped
                elif not ungrouped and any(not (a.get("group") or "") for a in self.accounts):
                    # 订阅账号全在冷却/被禁用：free 组账号请求订阅模型是必然 401，
                    # 回退全池只会白打上游并把这些账号一起冷却掉（0829/0830 两次
                    # 整池 503 就是这么来的）。宁可如实 503 + Retry-After。
                    log.warning("分组隔离: %s 已无可用订阅账号，不回退 free 组", model)
                    return None

        if model and 'glm' in model.lower():
            glm_accounts = [a for a in enabled if a.get('has_glm53', False)]
            if glm_accounts:
                enabled = glm_accounts
            else:
                # 候选组里已无 GLM 直连权限账号（free 组里 <账号ID>/<账号ID> 只有
                # codely-* 别名），但全池还有（订阅池）→ 回退，避免 GLM 请求
                # 被派给 alias-only 账号白吃 403。
                glm_all = [a for a in enabled_all if a.get('has_glm53', False)]
                if glm_all:
                    log.warning("GLM 回退: %s 候选组无 GLM 权限账号，改用 %s",
                                model, [a["user_id"] for a in glm_all])
                    enabled = glm_all

        # 负载感知：候选里负载最低的优先（同负载保持轮询顺序）
        if loads:
            min_load = min(loads.get(a["user_id"], 0) for a in enabled)
            if min_load > 0 or any(loads.get(a["user_id"], 0) > 0 for a in enabled):
                light = [a for a in enabled if loads.get(a["user_id"], 0) <= min_load]
                if light:
                    enabled = light

        # 候选已过滤（启用 + 未冷却），按游标轮转取一个（0906: 尾部状态变更包锁）
        with self._lock:
            self.index %= len(enabled)
            acc = enabled[self.index]
            self.index = (self.index + 1) % len(enabled)

            acc["last_used"] = datetime.now().isoformat()
            acc["use_count"] = acc.get("use_count", 0) + 1
            self._save()
        return acc

    def get_all_status(self):
        """返回所有账号摘要（含 JWT 过期时间解析）。"""
        result = []
        for a in self.accounts:
            token = a.get("access_token", "")
            try:
                payload_b64 = token.split(".")[1]
                payload_bytes = base64.urlsafe_b64decode(
                    payload_b64 + "=" * (4 - len(payload_b64) % 4) if len(payload_b64) % 4 else payload_b64
                )
                payload = json.loads(payload_bytes)
                exp = payload.get("exp", 0)
                now = int(time.time())
                remaining = max(0, exp - now)
                expires_at = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M") if exp > 0 else "未知"
                # 0831：上游 /key/info 已 403（key_alias 邮箱拿不到），
                # 改从 JWT payload 本地提取 email（零网络开销）。
                email = payload.get("email") or ""
            except Exception:
                remaining = 0
                expires_at = "未知"
                email = ""
            result.append({
                "user_id": a["user_id"],
                "username": a.get("username", ""),
                "email": email,
                "org_id": a.get("org_id", ""),
                "enabled": a.get("enabled", True),
                "use_count": a.get("use_count", 0),
                "last_used": a.get("last_used", ""),
                "token_expires": expires_at,
                "token_remaining_hours": round(remaining / 3600, 1),
                "login_status": "已登录" if remaining > 0 else "已过期",
                "has_glm53": a.get("has_glm53", False),
                "budget_exceeded": a.get("budget_exceeded", False),
                "quota_cooldown_left": max(0, round(
                    (a.get("budget_cooldown_until") or 0) - time.time()))
                    if a.get("budget_exceeded") else 0,
                "cooldown_reason": a.get("cooldown_reason", ""),
                "group": a.get("group", ""),
            })
        return result

    def toggle_account(self, user_id: str, enabled: bool):
        for a in self.accounts:
            if a["user_id"] == user_id:
                a["enabled"] = enabled
                a["budget_exceeded"] = False  # 重新启用时清除配额标记
                a["budget_cooldown_until"] = 0
                self._save()
                return True
        return False

    def update_glm_status(self, user_id: str, has_glm53: bool):
        for a in self.accounts:
            if a["user_id"] == user_id:
                a["has_glm53"] = has_glm53
                self._save()

    def add_account(self, user_id: str, token: str, username: str = "", org_id: str = ""):
        """新增账号；已存在返回 False。"""
        if any(a["user_id"] == user_id for a in self.accounts):
            return False
        self.accounts.append({
            "user_id": user_id,
            "username": username,
            "org_id": org_id,
            "access_token": token,
            "last_used": "",
            "use_count": 0,
            "enabled": True,
            "has_glm53": False,
            "budget_exceeded": False,
            "group": "",
        })
        self._save()
        return True

    def remove_account(self, user_id: str):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a["user_id"] != user_id]
        if len(self.accounts) != before:
            self._save()
            return True
        return False
