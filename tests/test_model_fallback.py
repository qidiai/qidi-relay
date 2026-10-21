# -*- coding: utf-8 -*-
"""模型兜底（config.model_fallbacks）回归测试，全程零网络。

设计契约（2026-10-21 方案 C）：
1. 只在凭证/额度类失败上兜底：401/402/403/429/503；
2. 请求形态类 4xx（400/404/422）**不得**兜底——换模型救不了，还会掩盖客户端 bug；
3. 兜底目标必须是 providers.match 命中的外部通道（池模型不参与）；
4. 显式配置驱动：无 model_fallbacks 配置 → 行为与升级前逐字节一致（原样返回）；
5. 有环/超深必须终止（seen 去重 + 深度上限 3）。

跑法：py -3.12 -m pytest tests/test_model_fallback.py -v
"""

import json
import os
import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("CODELY_CLI_VERSION", "0.0.0-test")

TMP = pathlib.Path(tempfile.mkdtemp(prefix="qidi-fallback-test-"))
CFG = TMP / "config.json"
CFG.write_text(json.dumps({
    "port": 0,
    "pool_models": ["GLM-5.3"],
    "model_groups": {},
    "providers": {},
    "roles": {},
    "vision_model": "GLM-5.3",
    "vision_capable_models": [],
    "pricing": {},
}), encoding="utf-8")
(TMP / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")

import proxy_core.settings as S  # noqa: E402

S.BASE_DIR = TMP
S.CONFIG_FILE = CFG
S.ACCOUNTS_FILE = TMP / "accounts.json"
S.STATS_FILE = TMP / "token_stats.json"
S.WATER_FILE = TMP / "water_check.json"
S.LOG_FILE = TMP / "proxy.log"

import proxy_core.accounts as A  # noqa: E402
import proxy_core.providers as PV  # noqa: E402
import proxy_core.stats as ST  # noqa: E402
import proxy_core.water as WA  # noqa: E402

A.ACCOUNTS_FILE = S.ACCOUNTS_FILE
PV.CONFIG_FILE = S.CONFIG_FILE
ST.STATS_FILE = S.STATS_FILE
WA.WATER_FILE = S.WATER_FILE

import routes.proxy as RP  # noqa: E402


class FakeResp:
    """最小 Response 替身：只有 status_code / body。"""

    def __init__(self, status, model):
        self.status_code = status
        self.model = model
        self.body = json.dumps({"model": model, "choices": []})


def _patch(monkeypatch, *, status_by_model, fallbacks, external=frozenset()):
    """装配：假转发 + 假 match + 假 config。external = 命中的外部模型名集合。"""

    def fake_forward(data, model, prov):
        return FakeResp(status_by_model.get(model, 200), model)

    monkeypatch.setattr(RP, "_forward_external", fake_forward)
    monkeypatch.setattr(RP.providers, "match",
                        lambda m: {"name": "fake"} if m in external else None)
    cfg = dict(json.loads(CFG.read_text(encoding="utf-8")))
    if fallbacks is not None:
        cfg["model_fallbacks"] = fallbacks
    monkeypatch.setattr(RP, "load_config", lambda: cfg)


# ── 1. 触发面 ────────────────────────────────────────────────────────
def test_success_no_fallback(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 200}, fallbacks={"A": ["B"]},
           external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 200 and r.model == "A"


def test_429_falls_back(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 429, "B": 200},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 200 and r.model == "B"


def test_401_falls_back(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 401, "B": 200},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    assert RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"}).model == "B"


def test_400_does_not_fall_back(monkeypatch):
    """请求形态错误不兜底——否则会掩盖客户端 bug。"""
    _patch(monkeypatch, status_by_model={"A": 400, "B": 200},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 400 and r.model == "A"


def test_404_does_not_fall_back(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 404, "B": 200},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    assert RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"}).status_code == 404


# ── 2. 配置与目标约束 ────────────────────────────────────────────────
def test_no_config_returns_original(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 429, "B": 200}, fallbacks=None,
           external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 429 and r.model == "A"


def test_non_external_target_skipped(monkeypatch):
    """备用模型不在外部通道（如池模型）→ 跳过，不冒险进账号池路径。"""
    _patch(monkeypatch, status_by_model={"A": 429, "B": 200},
           fallbacks={"A": ["B"]}, external={"A"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 429 and r.model == "A"


def test_chain_second_target_wins(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 429, "B": 429, "C": 200},
           fallbacks={"A": ["B", "C"]}, external={"A", "B", "C"})
    assert RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"}).model == "C"


def test_all_targets_fail_returns_last(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 429, "B": 429},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 429


# ── 3. 终止性 ────────────────────────────────────────────────────────
def test_cycle_terminates(monkeypatch):
    _patch(monkeypatch, status_by_model={"A": 429, "B": 429},
           fallbacks={"A": ["B"], "B": ["A"]}, external={"A", "B"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 429          # 不抛、不死循环


def test_depth_cap(monkeypatch):
    """A→B→C→D 是 3 跳（上限内）→ D 生效；A→B→C→D→E 在第 3 跳后停止。"""
    _patch(monkeypatch, status_by_model={"A": 429, "B": 429, "C": 429, "D": 200},
           fallbacks={"A": ["B"], "B": ["C"], "C": ["D"]},
           external={"A", "B", "C", "D"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 200 and r.model == "D"


def test_depth_cap_stops_after_three_hops(monkeypatch):
    _patch(monkeypatch,
           status_by_model={"A": 429, "B": 429, "C": 429, "D": 429, "E": 200},
           fallbacks={"A": ["B"], "B": ["C"], "C": ["D"], "D": ["E"]},
           external={"A", "B", "C", "D", "E"})
    r = RP._forward_with_fallbacks({"model": "A"}, "A", {"name": "fake"})
    assert r.status_code == 429 and r.model == "D"


def test_request_data_not_mutated(monkeypatch):
    """降级必须复制请求体，不能污染调用方 data（后续日志/语料会用）。"""
    _patch(monkeypatch, status_by_model={"A": 429, "B": 200},
           fallbacks={"A": ["B"]}, external={"A", "B"})
    data = {"model": "A", "messages": [{"role": "user", "content": "hi"}]}
    RP._forward_with_fallbacks(data, "A", {"name": "fake"})
    assert data["model"] == "A"
