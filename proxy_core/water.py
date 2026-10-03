"""注水检测：识别上游是否悄悄降级了模型（挂顶级模型的名、跑便宜模型）。

两条检测线，互为补充：

一、被动观测（零成本，每次转发自动进行）
  - 模型名校验：上游响应里的 model 字段与请求模型名不符 → 立即记录「替换嫌疑」。
    归一化规则：小写、去掉 "z-ai/" 之类路由前缀；数字/preview 后缀视为同系版本，
    其余（如 -air / -flash / 完全不同的名字）判为不符。
  - 性能画像：每（账号, 模型）滚动记录 TTFT 与 tokens/s，仅作参考展示，不单独告警
    （延迟受网络影响太大，误报率高）。

二、主动金丝雀探针（管理面板手动触发，小成本）
  - tokenizer 指纹（最强信号）：同一段固定提示词，对同一个模型，上游返回的
    prompt_tokens 每次都必须完全一致——分词器是确定性的。漂移超过阈值说明
    分词器变了 = 后面大概率换模型了。跨账号指纹不一致同样可疑。
  - 金丝雀问答：固定算术题 / 复读题 / 常识题，答案错误 = 能力降级。
  - 探针逐账号直连上游（绕过轮询负载均衡），精确定位哪个账号被注水。

状态持久化到 water_check.json，重启不丢基线。
"""

import json
import logging
import threading
import time
from datetime import datetime

import httpx

from .keystore import build_litellm_headers
from .settings import LITELLM_API_BASE, WATER_FILE

log = logging.getLogger("qidi-relay")

# 归一化后视为「同系版本」的后缀（版本号 / 快照 / 预览），不算换模型
_VERSION_SUFFIXES = ("preview", "snapshot", "latest")
# 指纹漂移阈值：prompt_tokens 偏移超过 8% 判定分词器变了
FINGERPRINT_DRIFT_PCT = 8.0
# 两次探针的最小间隔（防误触连点）
PROBE_MIN_INTERVAL_S = 30
HISTORY_KEEP = 20          # 每个探针目标保留的历史次数
PASSIVE_KEEP = 50          # 被动事件保留条数
PERF_SAMPLES = 12          # 性能画像滚动样本数

# ─── 金丝雀题库（固定不动！prompt_tokens 指纹依赖题面逐字节稳定） ───────────
CANARIES = [
    {
        "id": "repeat",
        "prompt": "请原样输出以下内容，不要添加任何解释或标点：QX7-8842 光子 torpedo 0.73 E=mc^2",
        "expect_contains": "QX7-8842",
        "title": "指令复读",
    },
    {
        "id": "math",
        "prompt": "计算 17×23+5 的值。只输出最终数字，不要输出任何其他字符。",
        "expect_contains": "396",
        "title": "算术",
    },
    {
        "id": "knowledge",
        "prompt": "中国的陆地国土面积大约是多少万平方公里？只输出数字，保留整数。",
        "expect_contains": "960",
        "title": "常识",
    },
]


def normalize_model(name: str) -> str:
    """模型名归一化：小写、去路由前缀（z-ai/、openai/ 等）、去空白。"""
    if not isinstance(name, str):
        return ""
    n = name.strip().lower()
    if "/" in n:
        n = n.rsplit("/", 1)[1]
    return n


def classify_model_name(requested: str, returned: str) -> str:
    """比较请求模型与上游返回的模型名。

    返回 "match" / "variant"（同系版本）/ "mismatch"（名实不符）。
    """
    rq, rt = normalize_model(requested), normalize_model(returned)
    if not rq or not rt:
        return "match"  # 信息缺失时不冤枉上游
    if rq == rt:
        return "match"
    # 0928: model_aliases 别名转发（intern-glm-5.3 -> glm-5.3 等）：上游
    # 回显真实 ID 属预期行为，判 match 而非注水嫌疑。延迟导入防环。
    try:
        from .providers import match as _provider_match
        _req = (requested or "").strip()
        _prov = _provider_match(_req)
        _amap = (_prov.get("model_aliases") or {}) if _prov else {}
        _target = _amap.get(_req)
        if _target and normalize_model(_target) == rt:
            return "match"
    except Exception:
        pass
    if rt.startswith(rq + "-"):
        suffix = rt[len(rq) + 1:]
        if suffix.isdigit() or suffix in _VERSION_SUFFIXES:
            return "variant"
    return "mismatch"


class WaterCheck:
    """注水检测器：被动观测 + 主动探针，状态持久化到 water_check.json。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.probes = {}    # "uid|model" -> {"last": {...}, "history": [...]}
        self.passive = []   # 被动捕获的事件（模型名不符等）
        self.perf = {}      # "uid|model" -> {"ttft": [...], "tps": [...]}
        self._last_probe_ts = 0.0
        self._load()

    # ─── 持久化 ─────────────────────────────────────────────
    def _load(self):
        if not WATER_FILE.exists():
            return
        try:
            with open(WATER_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.probes = raw.get("probes", {})
            self.passive = raw.get("passive", [])
            self.perf = raw.get("perf", {})
        except Exception as e:
            log.error("读取 water_check.json 失败: %s", e)

    def _save(self):
        try:
            with open(WATER_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "probes": self.probes,
                    "passive": self.passive[-PASSIVE_KEEP:],
                    "perf": self.perf,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error("保存 water_check.json 失败: %s", e)

    # ─── 被动观测：每次转发调用 ─────────────────────────────
    def observe(self, uid: str, model_requested: str, model_returned: str,
                usage: dict | None, ttft_ms: int | None = None,
                total_ms: int | None = None):
        """记录一次真实请求的指纹信号。异常绝不能影响转发主流程。"""
        try:
            self._observe_inner(uid, model_requested, model_returned,
                                usage, ttft_ms, total_ms)
        except Exception as e:
            log.error("water.observe 异常（忽略）: %s", e)

    def _observe_inner(self, uid, model_requested, model_returned,
                       usage, ttft_ms, total_ms):
        verdict = classify_model_name(model_requested, model_returned)
        key = f"{uid}|{model_requested}"
        dirty = False
        with self._lock:
            if verdict == "mismatch":
                # 同一目标 10 分钟内只记一次，避免刷屏
                recent = any(p.get("key") == key for p in self.passive[-10:])
                if not recent:
                    self.passive.append({
                        "time": datetime.now().strftime("%m-%d %H:%M:%S"),
                        "key": key,
                        "user_id": uid,
                        "model": model_requested,
                        "returned": model_returned or "(空)",
                        "note": f"请求 {model_requested}，上游返回 {model_returned}",
                    })
                    self.passive = self.passive[-PASSIVE_KEEP:]
                    dirty = True
                    log.warning("注水嫌疑（模型名不符）: 账号 %s 请求 %s 返回 %s",
                                uid, model_requested, model_returned)
            # 性能画像（仅展示参考）
            if usage and (ttft_ms or total_ms):
                p = self.perf.setdefault(key, {"ttft": [], "tps": []})
                if ttft_ms:
                    p["ttft"].append(int(ttft_ms))
                    p["ttft"] = p["ttft"][-PERF_SAMPLES:]
                    dirty = True
                ct = usage.get("completion_tokens", 0)
                if ct and total_ms and total_ms > 0:
                    tps = ct / (total_ms / 1000.0)
                    p["tps"].append(round(tps, 1))
                    p["tps"] = p["tps"][-PERF_SAMPLES:]
                    dirty = True
            if dirty:
                self._save()

    # ─── 主动探针 ───────────────────────────────────────────
    def run_probe(self, model: str, accounts: list, get_key, providers_map=None) -> dict:
        """对每个启用账号直连上游发金丝雀探针（绕过轮询，定位到账号）。

        get_key: api_cache.get；providers_map: 外部 provider 名->prov 字典（可选）。
        返回 {"results": [...], "time": ...}；间隔过近抛 RuntimeError。
        """
        now = time.time()
        if now - self._last_probe_ts < PROBE_MIN_INTERVAL_S:
            raise RuntimeError(f"探针间隔需 ≥{PROBE_MIN_INTERVAL_S}s，请稍后再试")
        self._last_probe_ts = now

        results = []
        targets = []
        # 模型命中外部 provider（如 agnes-*）→ 只探该 provider；
        # 主账号池不可能服务外部模型，拖上只会产生 401 噪音
        provider_hit = None
        if providers_map:
            for pname, prov in providers_map.items():
                if normalize_model(model) in {normalize_model(m) for m in prov.get("models", [])}:
                    provider_hit = (pname, prov)
                    break
        if provider_hit:
            targets.append(("provider", provider_hit[0], provider_hit[1]))
        else:
            for acc in accounts:
                if acc.get("enabled", True):
                    targets.append(("pool", acc.get("user_id", "?"), acc))

        for kind, tid, obj in targets:
            results.append(self._probe_one(kind, tid, obj, model, get_key))

        with self._lock:
            for r in results:
                key = f"{r['user_id']}|{model}"
                slot = self.probes.setdefault(key, {"last": None, "history": []})
                if slot["last"]:
                    slot["history"].append(slot["last"])
                    slot["history"] = slot["history"][-HISTORY_KEEP:]
                slot["last"] = r
            self._save()
        return {"time": datetime.now().strftime("%m-%d %H:%M:%S"), "results": results}

    def _probe_one(self, kind: str, tid: str, obj, model: str, get_key) -> dict:
        r = {
            "user_id": tid, "kind": kind, "model": model,
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "severity": "unknown", "notes": [], "canaries": [],
            "fingerprint": None, "fingerprint_prev": None, "drift_pct": None,
        }
        # 请求头与地址
        if kind == "pool":
            key = get_key(obj)
            if not key:
                r["error"] = "获取 API Key 失败"
                return r
            headers = build_litellm_headers(key)
            url = f"{LITELLM_API_BASE}/v1/chat/completions"
        else:
            if obj.get("type") == "atomcode":
                # AtomCode 通道：sign_v1 按每个探针请求体字节单独签名（循环内逐题处理）
                headers = None
                url = f"{obj['base_url']}/chat/completions"
            else:
                headers = {
                    "Authorization": f"Bearer {obj['api_key']}",
                    "Content-Type": "application/json",
                }
                url = f"{obj['base_url']}/chat/completions"

        prev = None
        with self._lock:
            slot = self.probes.get(f"{tid}|{model}")
            if slot and slot.get("last") and slot["last"].get("fingerprint"):
                prev = slot["last"]["fingerprint"]

        mismatched_names, prompts_tokens = set(), []
        try:
            with httpx.Client() as client:
                for c in CANARIES:
                    body = {
                        "model": model,
                        "messages": [{"role": "user", "content": c["prompt"]}],
                        "max_tokens": 512,  # 推理模型的思考过程会消耗大量 token，太小会截断成空答案
                        "stream": False,
                    }
                    if kind == "provider" and headers is None:
                        # AtomCode：该题请求体按字节签名
                        from .atomcode import sign_headers_for
                        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                        try:
                            hdrs = sign_headers_for(tid, "POST", "/chat/completions", raw)
                        except Exception as e:
                            r["error"] = f"凭证不可用: {e}"
                            return r
                        t0 = time.perf_counter()
                        resp = client.post(url, content=raw, headers=hdrs, timeout=60)
                    else:
                        t0 = time.perf_counter()
                        resp = client.post(url, json=body, headers=headers, timeout=60)
                    ms = int((time.perf_counter() - t0) * 1000)
                    if resp.status_code != 200:
                        r["canaries"].append({
                            "title": c["title"], "passed": False,
                            "actual": f"HTTP {resp.status_code}",
                            "latency_ms": ms,
                        })
                        r["notes"].append(f"{c['title']}探针 HTTP {resp.status_code}")
                        continue
                    data = resp.json()
                    mismatched_names.add(
                        classify_model_name(model, data.get("model", "")))
                    u = data.get("usage") or {}
                    pt = u.get("prompt_tokens")
                    if c["id"] == "repeat" and isinstance(pt, int):
                        r["fingerprint"] = pt
                    if isinstance(pt, int):
                        prompts_tokens.append(pt)
                    text = ""
                    try:
                        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
                        text = msg.get("content") or ""
                        # 推理模型可能把答案留在思考过程里而 content 为空
                        if not text.strip():
                            text = msg.get("reasoning_content") or ""
                    except Exception:
                        pass
                    passed = c["expect_contains"] in text
                    r["canaries"].append({
                        "title": c["title"], "passed": passed,
                        "actual": text.strip()[:40], "latency_ms": ms,
                    })
                    if not passed:
                        r["notes"].append(
                            f"{c['title']}未通过（期望含 {c['expect_contains']}，实际 {text.strip()[:24]!r}）")
        except Exception as e:
            r["error"] = str(e)[:160]
            return r

        # ── 评分 ──
        severity = "ok"
        if "mismatch" in mismatched_names:
            severity = "watered"
            r["notes"].append("上游返回的模型名与请求不符（替换实锤）")
        if r["fingerprint"] is not None and prev is not None and prev > 0:
            r["fingerprint_prev"] = prev
            r["drift_pct"] = round(abs(r["fingerprint"] - prev) / prev * 100, 1)
            if r["drift_pct"] > FINGERPRINT_DRIFT_PCT:
                severity = "watered"
                r["notes"].append(
                    f"tokenizer 指纹漂移 {r['drift_pct']}%（{prev}→{r['fingerprint']}），疑似换模型")
        if any(not c["passed"] for c in r["canaries"]) and severity == "ok":
            severity = "suspect"
        if all("HTTP" in c.get("actual", "") for c in r["canaries"]) and r["canaries"]:
            severity = "unknown"
        r["severity"] = severity
        if not r["notes"]:
            r["notes"].append("模型名、指纹、金丝雀全部通过")
        return r

    # ─── 面板数据 ───────────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            probes = [v["last"] for v in self.probes.values() if v.get("last")]
            probes.sort(key=lambda x: (x.get("model", ""), x.get("user_id", "")))
            perf = []
            for key, p in self.perf.items():
                uid, model = key.split("|", 1)
                ttft = p.get("ttft", [])
                tps = p.get("tps", [])
                perf.append({
                    "user_id": uid, "model": model,
                    "ttft_ms_avg": int(sum(ttft) / len(ttft)) if ttft else None,
                    "tps_avg": round(sum(tps) / len(tps), 1) if tps else None,
                    "samples": len(ttft) + len(tps),
                })
            return {
                "probes": probes,
                "passive": list(reversed(self.passive[-20:])),
                "perf": perf,
                "canary_count": len(CANARIES),
            }


water = WaterCheck()
