"""模型闸门套件（P1-4）：配置层 + 接线层。

    python tests/_test_model_gates.py

配置层（本轮）：_clean_model_patterns / key_allows_model / _clean_key_entry 保留 models /
set_api_keys 往返 / banned_models 读写。
接线层（等 qoder_proxy.py 空出后补）：G1 封禁模型 400+两层计数 0、G2 Key 白名单外
400+计数 0、G3 对照组、G4 默认不变、G7 fnmatch 边界。
「不发上游」拦两层：P.open_upstream 桩（出站层）+ P.urllib.request.urlopen 桩
（网络层）；测试自身用 http.client 发请求，避免被自桩。
"""
import os
import sys
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_settings as S

PASS = FAIL = 0


def check(label, ok, extra=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


D = tempfile.mkdtemp(prefix="qd-gates-")
_clean = getattr(S, "_clean_model_patterns", None)
_allows = getattr(S, "key_allows_model", None)
_banned = getattr(S, "banned_models", None)
_set_banned = getattr(S, "set_banned_models", None)

# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_settings.py"])
print("[model-gates] P1-4 配置层")

check("S1 清洗：字符串按 逗号 分号 换行 切分 + lower + 去重 + 去空",
      _clean is not None
      and _clean("GPT-4*, qwen3-max;gpt-4*\n  ") == ["gpt-4*", "qwen3-max"],
      _clean("GPT-4*, qwen3-max;gpt-4*\n  ") if _clean else "<missing>")
check("S1b 兼容 list/set，非法类型 -> []（不抛）",
      _clean is not None
      and _clean(["A", "", None, "a"]) == ["a"] and _clean(42) == []
      and _clean(None) == [], None)
check("S2 空列表/字段缺失 = 不受限（True）",
      _allows is not None and _allows({}, "gpt-4o") is True
      and _allows({"models": []}, "anything") is True, None)
check("S2b fnmatch 通配 + 大小写不敏感：命中 True / 未命中 False",
      _allows is not None
      and _allows({"models": ["gpt-4*"]}, "GPT-4o") is True
      and _allows({"models": ["gpt-4*"]}, "qwen3-max") is False, None)
check("S2c 受限 key + 空模型 = 拒绝（wb 同语义）",
      _allows is not None and _allows({"models": ["gpt-4*"]}, "") is False
      and _allows({"models": ["gpt-4*"]}, None) is False, None)

_ce = S._clean_key_entry
entry = _ce({"key": "k-1234", "name": "n", "models": "gpt-4*, QWEN3-MAX"})
check("S3 _clean_key_entry 保留 models（白名单重建最易漏点）",
      entry.get("models") == ["gpt-4*", "qwen3-max"], entry)

S.set_api_keys(D, [{"id": "k1", "key": "secret-1234", "name": "t",
                    "models": ["gpt-4*"]}])
back = S.api_keys(D)
check("S4 set_api_keys -> api_keys 往返保留 models",
      bool(back) and back[0].get("models") == ["gpt-4*"], back)

check("S5 banned_models 默认 []（未配置 = 不封禁）",
      _banned is not None and _banned(D) == [],
      _banned(D) if _banned else "<missing>")
check("S5b set 后读回：清洗 + 去重（字符串输入）",
      _set_banned is not None
      and _set_banned(D, "GPT-4*, o1-*; gpt-4*") == ["gpt-4*", "o1-*"]
      and _banned(D) == ["gpt-4*", "o1-*"], None)
check("S5c 清空 = 恢复不封禁",
      _set_banned is not None and _set_banned(D, []) == []
      and _banned(D) == [], None)

shutil.rmtree(D, ignore_errors=True)

# ================= 接线层（G1-G8）：真 handler + 两层计数 =================
# 判定「不发上游」拦两层：P.open_upstream 桩（出站层）+ P.urllib.request.urlopen
# 桩（网络层）；测试自身用 http.client 发请求，避免被自桩。装置照 _test_auth_matrix。
import http.client
import http.server
import json as _json
import threading
import types

print()
print("[model-gates] P1-4 接线层（真 handler；拦两层）")

import qoder_proxy as P

_G = tempfile.mkdtemp(prefix="qd-gates-live-")
_orig_accounts = P.ACCOUNTS_DIR
_orig_open = P.open_upstream
_orig_urllib = P.urllib
outbound = {"open": 0, "net": 0}


def _fake_open_upstream(*a, **k):
    outbound["open"] += 1
    raise RuntimeError("stub: must not reach upstream")


def _fake_urlopen(*a, **k):
    outbound["net"] += 1
    raise RuntimeError("stub: network layer must not be reached")


P.ACCOUNTS_DIR = _G
S.set_api_keys(_G, [
    {"id": "gate1", "name": "闸门Key", "key": "gate-key-1234",
     "models": "gpt-4*"},
    {"id": "plain1", "name": "不受限Key", "key": "plain-key-1234"},
])
P.open_upstream = _fake_open_upstream
P.urllib = types.SimpleNamespace(
    request=types.SimpleNamespace(Request=_orig_urllib.request.Request,
                                  urlopen=_fake_urlopen),
    error=_orig_urllib.error,
    parse=_orig_urllib.parse)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
_PORT = srv.server_address[1]


def _post(path, body, key="plain-key-1234"):
    conn = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=20)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        conn.request("POST", path, body=_json.dumps(body).encode("utf-8"),
                     headers=headers)
        resp = conn.getresponse()
        return int(resp.status), resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _reset():
    outbound["open"] = 0
    outbound["net"] = 0


def _chat(model, key="plain-key-1234"):
    _reset()
    return _post("/v1/chat/completions",
                 {"model": model,
                  "messages": [{"role": "user", "content": "hi"}]}, key)


def _anthropic(model, key="plain-key-1234"):
    _reset()
    return _post("/v1/messages",
                 {"model": model, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "hi"}]}, key)


try:
    # G1 全局封禁（对不受限 key 也生效）-> 400 + 文案 + 两层计数 0
    S.set_banned_models(_G, "qwen3-max")
    code, text = _chat("qwen3-max")
    check("G1 封禁模型 -> 400 + 文案 + open/网络计数均为 0",
          code == 400 and "封禁" in text and outbound["open"] == 0
          and outbound["net"] == 0, (code, text[:140], dict(outbound)))

    # G2 Key 白名单外 -> 400 + 文案 + 两层计数 0（先清空全局封禁）
    S.set_banned_models(_G, [])
    code, text = _chat("qwen3-max", key="gate-key-1234")
    check("G2 Key 白名单外 -> 400 + 文案 + 两层计数 0",
          code == 400 and "模型限制" in text and outbound["open"] == 0
          and outbound["net"] == 0, (code, text[:140], dict(outbound)))

    # G3 对照组：命中白名单 -> 放行到出站口（open >= 1）
    code, text = _chat("gpt-4o", key="gate-key-1234")
    check("G3 对照组：白名单内模型 -> 放行（open >= 1）",
          outbound["open"] >= 1, (code, dict(outbound)))

    # G4 默认（无封禁、key 不受限）-> 放行（行为不变）
    code, text = _chat("qwen3-max", key="plain-key-1234")
    check("G4 默认空配置 -> 放行（open >= 1，行为与改前一致）",
          outbound["open"] >= 1, (code, dict(outbound)))

    # G7 fnmatch 边界：封禁 gpt-4* 不误伤 qwen3-max；命中 gpt-4o 必拒
    S.set_banned_models(_G, "gpt-4*")
    code_ok, _t1 = _chat("qwen3-max", key="plain-key-1234")
    ok_passed = outbound["open"] >= 1
    code_bad, text_bad = _chat("gpt-4o", key="plain-key-1234")
    check("G7 边界：gpt-4* 不误伤 qwen3-max（放行）、命中 gpt-4o 必拒（400+计数0）",
          ok_passed and code_bad == 400 and outbound["open"] == 0
          and outbound["net"] == 0, (code_ok, code_bad, text_bad[:100]))
    S.set_banned_models(_G, [])

    # G8 Anthropic 入口同覆盖（chat 被拦、messages 不许绕过）
    S.set_banned_models(_G, "qwen3-max")
    code, text = _anthropic("qwen3-max")
    check("G8 /v1/messages 同覆盖：封禁模型 -> 400 + 两层计数 0",
          code == 400 and outbound["open"] == 0 and outbound["net"] == 0,
          (code, text[:140], dict(outbound)))
    S.set_banned_models(_G, [])
finally:
    try:
        srv.shutdown()
    except Exception:
        pass
    P.open_upstream = _orig_open
    P.urllib = _orig_urllib
    P.ACCOUNTS_DIR = _orig_accounts
    shutil.rmtree(_G, ignore_errors=True)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
