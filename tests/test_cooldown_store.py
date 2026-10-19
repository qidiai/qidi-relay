# -*- coding: utf-8 -*-
"""冷却状态持久化（吸收-1 / 规格 §R-A）测试。

零网络、零真实数据文件：COOLDOWNS_FILE 注入 TMP（§7.7 纪律）。
覆盖规格 §R-A 测试清单：
1. persist→load 往返；load 丢弃过期
2. 原子替换中断不破坏旧文件；双实例 pid-tmp 并存
3. JSON 损坏 → 空 dict + warning 日志
4. clear 路径：成功解除 → persist → 重载后不再拦截（状态保真）
5. persist=false 分支：不读不写
6. channels 节：cline 冷却恢复（重载回灌）
7. 路径隔离：不写真实仓库根
另补：持久化失败保留 dirty、下轮重试（R-004 失败可见）

跑法：python -m pytest tests/test_cooldown_store.py -q
"""

import json
import logging
import pathlib
import sys
import time

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import proxy_core.settings as S            # noqa: E402
import proxy_core.cooldown_store as CS     # noqa: E402
import proxy_core.cline as cline           # noqa: E402


def _reset_store():
    """清空内存态 + 复位懒加载（切路径后强制重读）。"""
    with CS._LOCK:
        CS._providers.clear()
        CS._channels.clear()
        CS._DIRTY = False
        CS._LOADED = False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """COOLDOWNS_FILE + CONFIG_FILE 均注入本用例专属 TMP（用例后还原）。

    审计 I-8：`_enabled()` 走 settings.CONFIG_FILE，若指向仓库真实配置，
    测试结果就随机器上的 cooldown.persist 漂移。这里显式给出
    persist=true，让持久化语义在测试内确定。
    """
    path = tmp_path / "cooldowns.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"cooldown": {"persist": True}}), encoding="utf-8")
    monkeypatch.setattr(S, "COOLDOWNS_FILE", path)
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)
    _reset_store()
    yield path
    _reset_store()


# ─── 1. 往返 + 过期丢弃 ──────────────────────────────────────────────

def test_persist_load_roundtrip_and_expiry(env):
    now = time.time()
    CS.set_provider("pA", now + 900)
    CS.set_channel("clp", "主号", now + 21600)
    assert CS.persist() is True

    raw = json.loads(env.read_text(encoding="utf-8"))
    assert raw["version"] == 1 and isinstance(raw["saved_at"], (int, float))
    assert raw["providers"]["pA"] == pytest.approx(now + 900)
    assert raw["channels"]["clp|主号"] == pytest.approx(now + 21600)

    # 模拟重启：内存清空后 load 回灌
    _reset_store()
    data = CS.load()
    assert data["providers"]["pA"] > time.time()
    assert data["channels"]["clp|主号"] > time.time()
    assert CS.get_provider("pA") == pytest.approx(now + 900)
    assert CS.get_channel("clp", "主号") == pytest.approx(now + 21600)

    # 过期条目在 load 时丢弃（不写即不复活）
    env.write_text(json.dumps({
        "version": 1,
        "providers": {"old": time.time() - 10},
        "channels": {"x|y": time.time() - 1},
    }), encoding="utf-8")
    _reset_store()
    assert CS.load() == {"providers": {}, "channels": {}}
    assert CS.get_provider("old") is None
    assert CS.get_channel("x", "y") is None


# ─── 2. 原子写：中断不破坏旧文件 + pid-tmp ───────────────────────────

def test_atomic_write_failure_keeps_old_file(env, monkeypatch):
    CS.set_provider("p", time.time() + 900)
    assert CS.persist() is True
    good = env.read_text(encoding="utf-8")

    # 替换阶段炸掉：主文件必须仍完好，写一半的 tmp 清理干净
    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(CS.os, "replace", _boom)
    CS.set_provider("q", time.time() + 900)
    assert CS.persist() is False
    assert env.read_text(encoding="utf-8") == good          # 旧文件未被破坏
    assert not list(env.parent.glob("cooldowns.json.*.tmp"))


def test_tmp_name_carries_pid(env, monkeypatch):
    """tmp 名带本进程 pid（affinity 同款）——双实例各写各的，
    os.replace 原子替换主文件。"""
    seen = {}
    real = CS.os.replace

    def _spy(src, dst):
        seen["src"] = src
        seen["dst"] = dst
        return real(src, dst)

    monkeypatch.setattr(CS.os, "replace", _spy)
    CS.set_provider("p", time.time() + 900)
    assert CS.persist() is True
    assert seen["src"] == f"{env}.{CS.os.getpid()}.tmp"
    assert seen["dst"] == str(env)


def test_dual_instance_pid_tmp_coexist(env):
    """另一实例（别的 pid）崩溃残留的半截 tmp 绝不污染主文件。"""
    CS.set_provider("p", time.time() + 900)
    assert CS.persist() is True

    (env.parent / "cooldowns.json.999999.tmp").write_text(
        '{"version": 1, "providers": {"p": ', encoding="utf-8")
    _reset_store()
    assert CS.get_provider("p") > time.time()     # 主文件完好，半截 tmp 被忽略


# ─── 3. JSON 损坏容错 ───────────────────────────────────────────────

def test_corrupt_json_degrades_with_warning(env, caplog):
    env.write_text("{truncated json", encoding="utf-8")
    _reset_store()
    with caplog.at_level(logging.WARNING, logger="qidi-relay"):
        data = CS.load()
    assert data == {"providers": {}, "channels": {}}
    assert any("cooldowns.json 读取失败" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ─── 4. clear 路径：解除后重载不复活 ────────────────────────────────

def test_clear_then_reload_does_not_resurrect(env):
    CS.set_provider("p", time.time() + 900)
    CS.set_channel("c", "l", time.time() + 900)
    assert CS.persist() is True
    assert CS.get_provider("p") and CS.get_channel("c", "l")

    CS.clear_provider("p")
    CS.clear_channel("c", "l")
    assert CS.persist() is True
    raw = json.loads(env.read_text(encoding="utf-8"))
    assert "p" not in raw["providers"]
    assert "c|l" not in raw["channels"]

    # 重载（= 重启）后解除状态保真：不再拦截
    _reset_store()
    assert CS.get_provider("p") is None
    assert CS.get_channel("c", "l") is None
    assert CS.load() == {"providers": {}, "channels": {}}


# ─── 5. persist=false：不读不写 ─────────────────────────────────────

def test_persist_false_no_read_no_write(env, monkeypatch):
    cfg = env.parent / "config.json"
    cfg.write_text(json.dumps({"cooldown": {"persist": False}}), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)
    _reset_store()

    # 预置一个合法文件：persist=false 时不得读
    env.write_text(json.dumps({
        "version": 1, "providers": {"p": time.time() + 900}, "channels": {}}),
        encoding="utf-8")
    before = env.read_text(encoding="utf-8")
    assert CS.get_provider("p") is None            # 不读
    assert CS.load() == {"providers": {}, "channels": {}}

    # 写入全部空操作：磁盘不变
    CS.set_provider("q", time.time() + 900)
    CS.set_channel("a", "b", time.time() + 900)
    assert CS.persist() is False
    assert CS.snapshot() == {"providers": {}, "channels": {}}
    assert env.read_text(encoding="utf-8") == before


def test_sweep_skips_disk_when_persist_disabled(env, monkeypatch):
    """审计 I-2：sweep 必须尊重 persist=false（dirty 已置位也不落盘）。"""
    CS.set_provider("p", time.time() + 900)
    assert CS.persist() is True                    # 先留下基线文件
    baseline = env.read_text(encoding="utf-8")

    cfg = env.parent / "config.json"
    cfg.write_text(json.dumps({"cooldown": {"persist": False}}), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)

    CS._DIRTY = True                               # 模拟"关闭前已置脏"
    CS._sweep_once()                               # persist=false：不得落盘
    assert CS._DIRTY is True                       # 且不清 dirty（既有语义）
    assert env.read_text(encoding="utf-8") == baseline

    env.unlink()                                   # 关闭期绝不（重）建文件
    CS._sweep_once()
    assert not env.exists()
    assert CS._DIRTY is True


# ─── 5b. persist 值防御式解析（审计 S-1）───────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("false", False), ("FALSE", False), (" 0 ", False), ("no", False), ("off", False),
    ("true", True), ("True", True), (" 1 ", True), ("yes", True), ("on", True),
    (1, True), (0, False), (1.0, True), (0.0, False),
    (None, True),                                 # 显式 null = 未配置 → 默认 true
])
def test_persist_flag_defensive_parse(env, monkeypatch, value, expected):
    """字符串 "false"/"0"/"no" 不得被裸 bool() 判真。"""
    cfg = env.parent / "config.json"
    cfg.write_text(json.dumps({"cooldown": {"persist": value}}), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)
    assert CS._enabled() is expected


@pytest.mark.parametrize("value", ["bogus", 2, 2.5, [], {}])
def test_persist_flag_invalid_warns_and_defaults_true(env, monkeypatch, caplog, value):
    cfg = env.parent / "config.json"
    cfg.write_text(json.dumps({"cooldown": {"persist": value}}), encoding="utf-8")
    monkeypatch.setattr(S, "CONFIG_FILE", cfg)
    with caplog.at_level(logging.WARNING, logger="qidi-relay"):
        assert CS._enabled() is True
    assert any("cooldown.persist 配置值非法" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ─── 6. channels 节：cline 冷却恢复 ─────────────────────────────────

def test_channel_cooldown_restored_on_reload(env, tmp_path):
    auth = tmp_path / "providers.json"
    auth.write_text("{}", encoding="utf-8")
    cfg = {"providers": {"clp": {
        "type": "cline", "models": ["cl-model"],
        "auth_paths": [{"path": str(auth), "label": "主号"}],
    }}}

    cline.reload_channels_from_config(cfg)
    ch = cline.channels_for("clp")[0]
    assert ch.label == "主号"
    assert ch.cooldown_until == 0.0

    ch.set_cooldown(3600, "日配额耗尽")
    recorded = ch.cooldown_until
    # 键按 auth 路径（稳定），label 不再是键的一部分
    assert CS.get_channel("clp", "主号", auth_path=str(auth)) == pytest.approx(recorded)

    # 重建（config mtime 变化 / 重启）：冷却回灌到新实例
    cline.reload_channels_from_config(cfg)
    ch2 = cline.channels_for("clp")[0]
    assert ch2 is not ch
    assert ch2.cooldown_until == pytest.approx(recorded)
    assert not ch2.available and ch2.cooldown_until > time.time()

    # 清理全局通道注册表，避免污染后续用例
    cline._channels.clear()


# ─── 6b. 稳定通道键：重启 + label 变化 / 账号顺序变化（审计 I-1）───

def test_channel_key_stable_across_restart_label_and_order(env, tmp_path):
    """模拟重启：配置账号顺序颠倒、label 全部去掉（自动拼装 label 变了），
    冷却仍按同一 auth 路径恢复；未被冷却的账号不被误挂。"""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")

    def _cfg(entries):
        return {"providers": {"clp": {"type": "cline", "models": ["cl-model"],
                                      "auth_paths": entries}}}

    cline.reload_channels_from_config(_cfg([
        {"path": str(a), "label": "主号"},
        {"path": str(b), "label": "备用"},
    ]))
    ch_a = next(c for c in cline.channels_for("clp") if c.auth_path == str(a))
    ch_a.set_cooldown(6 * 3600, "日配额耗尽")
    recorded = ch_a.cooldown_until
    assert CS.persist() is True
    keys = json.loads(env.read_text(encoding="utf-8"))["channels"]
    assert list(keys) and all(k.startswith("clp|path:") for k in keys), keys

    # 重启形态：内存全清 + 配置顺序颠倒且不带 label（自动 label 与旧值不同）
    _reset_store()
    cline._channels.clear()
    cline.reload_channels_from_config(_cfg([
        {"path": str(b)},
        {"path": str(a)},
    ]))
    chs = cline.channels_for("clp")
    new_a = next(c for c in chs if c.auth_path == str(a))
    new_b = next(c for c in chs if c.auth_path == str(b))
    assert new_a.label != "主号"              # label 已漂移（自动拼装）
    assert new_a.cooldown_until == pytest.approx(recorded)   # 仍按路径恢复
    assert new_b.cooldown_until == 0.0        # 另一账号不受牵连
    cline._channels.clear()


def test_minicode_channel_key_stable_across_restart_label_change(env, tmp_path):
    """minicode 同款：label 改名后仍按 auth 路径恢复冷却。"""
    import proxy_core.minicode as mc

    a = tmp_path / "mc_auth.json"
    a.write_text("{}", encoding="utf-8")

    def _cfg(entry):
        return {"providers": {"mcp": {"type": "minicode",
                                      "auth_paths": [entry]}}}

    mc.reload_channels_from_config(_cfg({"path": str(a), "label": "旧标签"}))
    ch = mc.channels_for("mcp")[0]
    ch.set_cooldown(6 * 3600, "429 日配额")
    recorded = ch.cooldown_until
    assert CS.persist() is True

    _reset_store()
    mc._channels.clear()
    mc.reload_channels_from_config(_cfg({"path": str(a)}))   # 无 label → 自动拼装
    ch2 = mc.channels_for("mcp")[0]
    assert ch2.label != "旧标签"
    assert ch2.cooldown_until == pytest.approx(recorded)
    mc._channels.clear()


# ─── 7. 路径隔离 ────────────────────────────────────────────────────

def test_path_isolation_never_writes_repo_root(env):
    repo_file = BASE / "cooldowns.json"
    existed = repo_file.exists()
    CS.set_provider("p", time.time() + 900)
    CS.set_channel("a", "b", time.time() + 900)
    assert CS.persist() is True
    assert env.exists()                            # 写到了注入的 TMP
    assert repo_file.exists() is existed           # 仓库根未被本测试触碰
    assert S.COOLDOWNS_FILE != repo_file


# ─── 补：失败保留 dirty、下轮重试（R-004 失败可见）──────────────────

def test_worker_keeps_dirty_on_failure_and_retries(env, monkeypatch):
    calls = {"n": 0}
    real = CS._save_to_disk

    def _flaky():
        calls["n"] += 1
        return False if calls["n"] == 1 else real()

    monkeypatch.setattr(CS, "_save_to_disk", _flaky)
    CS.set_provider("p", time.time() + 900)
    assert CS._DIRTY is True

    CS._sweep_once()                # 第一轮：失败
    assert CS._DIRTY is True        # dirty 保留（affinity 会清，本模块不清）
    assert not env.exists()

    CS._sweep_once()                # 第二轮：成功落盘
    assert CS._DIRTY is False
    assert json.loads(env.read_text(encoding="utf-8"))["providers"]["p"] > time.time()


# ─── 补：proxy 读点预热（5 触点之读检查）────────────────────────────

def test_proxy_read_point_warms_up_from_store(env):
    """重启形态：内存 `_prov_quota_cooldown` 空、store 有持久化冷却 →
    读检查点预热回灌，窗口内请求快速失败（零上游）。"""
    import routes.proxy as RP
    from flask import Flask

    CS.set_provider("warmprov", time.time() + 900)
    assert CS.persist() is True
    RP._prov_quota_cooldown.pop("warmprov", None)          # 模拟重启内存空

    prov = {"name": "warmprov", "base_url": "http://127.0.0.1:1/v1",
            "keys": [{"key": "k", "label": "l"}], "models": ["m"]}
    app = Flask(__name__)
    with app.test_request_context("/v1/chat/completions"):
        r = RP._forward_external(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            "m", prov)
    assert r.status_code == 503
    assert r.get_json()["error"]["code"] == "provider_quota_cooldown"
    assert "warmprov" in RP._prov_quota_cooldown            # 已回灌内存表
    RP._prov_quota_cooldown.pop("warmprov", None)

