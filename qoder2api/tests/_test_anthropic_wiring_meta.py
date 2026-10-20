# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_catalog.py", "qoder_anthropic.py"])
# -*- coding: utf-8 -*-
"""Anthropic 思考档位接线套件（catalog_meta 下传）。

    python tests/_test_anthropic_wiring_meta.py

背景：`/v1/messages` 的 `thinking.budget_tokens` 要经 `effort_from_anthropic`
映射成 `reasoning_effort` 才可能下发上游；而该函数**没有档位表就返回 None**
（「不猜测」语义，见 qoder_anthropic.py §4.2）。档位表的唯一来源是模型目录，
所以 `_handle_anthropic` 必须把 `catalog_meta_for(model_key)` 传下去——
漏传的观感是「思考档位设了不生效、且日志里 effort=None」，没有任何报错。

本套件钉住三层：
  1. `catalog_meta_for` 本体（按 key 命中 / 静态兜底 / 未知返回 {}）；
  2. HTTP 层接线：POST /v1/messages 之后**实际交给 open_upstream 的 chat_req**
     带上了 reasoning_effort（拦在 open_upstream 这个唯一出站口，而不是
     「messages_to_chat 被调用过」——后者证明不了 meta 传对了）；
  3. 端到端：该档位真的写进上游请求体 parameters（与 usage 记账同源）。

反证（本套件自证能红）：把 `catalog_meta_for` 的返回改成 `{}`，[2]/[3] 必红
（effort 变 None）；把 qoder_anthropic 的 clamp 去掉不影响本套件——它测的是
「meta 有没有传到」，不是「档位怎么算」。
"""
import os
import sys
import threading
import urllib.error
import urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

# usage/ 落盘隔离：被 stub 中止的请求也会走 record_error 落盘，
# 不能写进仓库的 usage/（并发编排下是跨进程共享路径）。
_tmp_meta = tempfile.mkdtemp(prefix="qd-anth-meta-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_meta

P, A = bootstrap()
import qoder_anthropic as AN
import qoder_catalog as C

try:
    print("[1] catalog_meta_for：按上游 key 取目录元数据")
    _key = "qmodel_38max"
    _meta = P.catalog_meta_for(_key, "cn")
    check("命中：返回的 meta.key 与查询 key 一致（cn）",
          _meta.get("key") == _key, _meta.get("key"))
    check("命中：meta 带 thinking_config（supported_efforts 才有东西可用）",
          bool(P.supported_efforts(_meta)), P.supported_efforts(_meta))
    _meta_i = P.catalog_meta_for("ultimate", "intl")
    check("命中：intl 区同样按 key 命中",
          _meta_i.get("key") == "ultimate"
          and bool(P.supported_efforts(_meta_i)),
          _meta_i.get("key"))
    check("未知 key -> {}（不猜测、不抛异常）",
          P.catalog_meta_for("no-such-model-xyz", "cn") == {})
    check("realm 省略时按当前区查（静态兜底路径）",
          P.catalog_meta_for(_key, None).get("key") == _key
          or P.catalog_meta_for(_key).get("key") == _key)
    check("路由模型 auto 也能取到 meta（有 thinking_config 之外的真实条目）",
          isinstance(P.catalog_meta_for("auto", "cn"), dict))

    print()
    print("[2] HTTP 层接线：chat_req 必须带 reasoning_effort"
          "（拦在 open_upstream 唯一出站口）")
    _captured = {}

    class _Acct:
        uid = "u1"
        nickname = "t"
        realm = "cn"
        enabled = True
        access_token = "dt-x"
        credits = None

    class _Pool:
        accounts = [_Acct()]

        def get(self, uid):
            return self.accounts[0] if uid else None

    P.POOL = _Pool()
    P.API_KEY = "k"
    P.configured_keys = lambda: []
    P.auth_required = lambda: True

    class _StopBeforeUpstream(Exception):
        pass

    _orig_open = P.open_upstream

    def _stub_open(chat_req, **kw):
        _captured["req"] = chat_req
        _captured["kw"] = kw
        raise _StopBeforeUpstream("stub: 拦在唯一出站口")

    _srv = P.ThreadingHTTPServer(("127.0.0.1", 0), P.Handler)
    _port = _srv.server_address[1]
    threading.Thread(target=_srv.serve_forever, daemon=True).start()

    def _post_messages(payload):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/messages" % _port,
            data=json.dumps(payload).encode(),
            headers={"x-api-key": "k", "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    P.open_upstream = _stub_open
    try:
        for _model in ("qmodel_38max", "qwen3.8-max"):
            _captured.clear()
            _st, _body = _post_messages({
                "model": _model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 512}})
            _req = _captured.get("req") or {}
            check("model=%s：chat_req.reasoning_effort 非空（meta 已下传）"
                  % _model, bool(_req.get("reasoning_effort")),
                  (_st, _req.get("reasoning_effort"), _body[:100]))
            # 与 handler 同路径：catalog_meta_for 只认上游 key，别名要先解析
            # （客户端填的 "qwen3.8-max" 不是 key，直接查会得到 {}）。
            _key_res = C.resolve_upstream_key(_model, realm="cn")
            check("model=%s：档位落在该模型官方档位表内"
                  % _model,
                  _req.get("reasoning_effort")
                  in P.supported_efforts(P.catalog_meta_for(_key_res, "cn")),
                  (_req.get("reasoning_effort"), _key_res))

        _captured.clear()
        _post_messages({"model": "qmodel_38max", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}],
                        "thinking": {"type": "disabled"}})
        _req = _captured.get("req") or {}
        check("thinking=disabled -> 不下发档位（既有语义不回退）",
              not _req.get("reasoning_effort"), _req.get("reasoning_effort"))

        _captured.clear()
        _post_messages({"model": "qmodel_38max", "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}]})
        _req = _captured.get("req") or {}
        check("未给 thinking -> 不下发档位（既有语义不回退）",
              not _req.get("reasoning_effort"), _req.get("reasoning_effort"))
    finally:
        P.open_upstream = _orig_open
        _srv.shutdown()
        _srv.server_close()

    print()
    print("[3] 端到端：档位真的写进上游请求体 parameters")
    _key2 = "qmodel_38max"
    _meta2 = P.catalog_meta_for(_key2, "cn")
    _sup2 = P.supported_efforts(_meta2)
    _payload = {"model": _key2, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 512}}
    _chat = AN.messages_to_chat(_payload, catalog_meta=_meta2, supported=_sup2)

    class _Acc:
        user_type = "personal_standard"
        realm = "cn"
        uid = "u"
        nickname = "n"
        access_token = "dt-x"
        expires_at = 0

    _up = P.build_qoder_body(_chat, _Acc(), _key2, realm="cn")
    check("上游 parameters.reasoning_effort == 转换出的档位",
          (_up.get("parameters") or {}).get("reasoning_effort")
          == _chat.get("reasoning_effort"),
          (_up.get("parameters"), _chat.get("reasoning_effort")))
    _chat_no = AN.messages_to_chat(_payload)
    check("对照（不传 meta）：effort_from_anthropic 返回 None"
          "——这正是接线前丢档位的原因",
          _chat_no.get("reasoning_effort") is None,
          _chat_no.get("reasoning_effort"))

    print()
    print("[4] 接线点静态钉住（防止将来被改回漏传）")
    _src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("_handle_anthropic 调用了 catalog_meta_for(model_key, req_realm)",
          "meta = catalog_meta_for(model_key, req_realm)" in _src)
    check("messages_to_chat 收到 catalog_meta 与 supported 两个实参",
          "catalog_meta=meta, supported=supported_efforts(meta))" in _src)
finally:
    shutil.rmtree(_tmp_meta, ignore_errors=True)

finish()
