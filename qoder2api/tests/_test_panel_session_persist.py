# -*- coding: utf-8 -*-
"""面板登录会话持久化：重启网关后浏览器不必重新登录。

    python tests/_test_panel_session_persist.py

钉住的判据：
  ① token 自校验（签名 + 过期），服务端不存会话表；
  ② **换一个 PanelSessions 实例（= 网关重启）后旧 token 仍有效**；
  ③ 篡改签名 / 改过期时间 / 空串一律拒绝；
  ④ revoke_all() 换密钥后，跨实例的旧 token 全部失效（改密码的踢出语义）；
  ⑤ 密钥持久化在 settings.json 且稳定生成，不会每次重启换新。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *        # noqa: F401,F403

P, A = bootstrap()
import qoder_settings as S

declare_suite(__file__, ["qoder_settings.py", "qoder_proxy.py"])

tmp = tempfile.mkdtemp(prefix="qd-sess-")
try:
    # ---- ① 基本签发与校验 ----
    p1 = S.PanelSessions(lambda: tmp)
    tok = p1.create()
    check("create -> valid", p1.valid(tok))
    check("token 是三段式（过期.nonce.签名）", tok.count(".") == 2, tok[:40])
    check("密钥已落盘 settings.json",
          len(str(S.load(tmp).get(S.PANEL_SECRET_KEY) or "")) >= 32)

    # ---- ② 模拟重启：新实例、同一 accounts_dir ----
    p2 = S.PanelSessions(lambda: tmp)
    check("重启（新实例）后旧 token 仍有效", p2.valid(tok))

    # ---- ③ 篡改一律拒绝 ----
    expiry, nonce, sig = tok.split(".")
    check("篡改签名被拒", not p2.valid(expiry + "." + nonce + "." + "0" * len(sig)))
    check("把过期时间改远仍被拒（签名覆盖 exp）",
          not p2.valid("9999999999." + nonce + "." + sig))
    check("换 nonce 被拒", not p2.valid(expiry + "." + nonce + "x." + sig))
    check("空串 / 乱码 / 段数不对被拒",
          not p2.valid("") and not p2.valid("abc") and not p2.valid("1.2")
          and not p2.valid("a.b.c.d"))

    # ---- ④ revoke 单个（本进程内）----
    p3 = S.PanelSessions(lambda: tmp)
    keep, drop = p3.create(), p3.create()
    p3.revoke(drop)
    check("revoke 后该 token 失效", not p3.valid(drop))
    check("revoke 不影响其它 token", p3.valid(keep))

    # ---- ⑤ revoke_all 换密钥：跨实例失效 ----
    p4 = S.PanelSessions(lambda: tmp)
    before = p4.create()
    key_before = S.panel_session_secret(tmp)
    p4.revoke_all()
    after = p4.create()
    key_after = S.panel_session_secret(tmp)
    p5 = S.PanelSessions(lambda: tmp)
    check("revoke_all 后旧 token 跨实例失效", not p5.valid(before))
    check("revoke_all 后新 token 跨实例有效", p5.valid(after))
    check("revoke_all 确实换了新密钥并落盘",
          key_before != key_after and len(key_after) >= 32,
          (len(key_before), len(key_after)))

    # ---- ⑥ 过期 ----
    p6 = S.PanelSessions(lambda: tmp, ttl=-1)
    check("已过期 token 被拒（ttl<0）", not p6.valid(p6.create()))

    # ---- ⑦ 密钥稳定：重复调用不会每次换新 ----
    k1 = S.panel_session_secret(tmp)
    k2 = S.panel_session_secret(tmp)
    check("密钥重复读取保持一致", k1 == k2 and len(k1) >= 32)

    # ---- ⑦b settings.json 损坏时拒绝回写（否则会抹掉密码哈希与全部 API Key）----
    tmp_bad = tempfile.mkdtemp(prefix="qd-badsess-")
    try:
        S.set_panel_password(tmp_bad, "keepme123")
        bad_path = S.settings_path(tmp_bad)
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write('{"panel_password_hash": "deadbeef", "api_keys": [{"id":"k0"}')
        pb = S.PanelSessions(lambda: tmp_bad)
        tokb = pb.create()
        content = open(bad_path, encoding="utf-8").read()
        check("损坏的 settings.json 不被覆盖", "deadbeef" in content and "api_keys" in content,
              content[:60])
        check("损坏时密钥退到进程内（ephemeral）", pb._ephemeral is True)
        check("损坏时面板仍可登录（临时密钥有效）", pb.valid(tokb))
        # 恢复成合法文件后应能重新落盘
        S.save(tmp_bad, {"panel_password_hash": "x"})
        pc = S.PanelSessions(lambda: tmp_bad)
        pcc = pc.create()
        check("文件恢复合法后密钥重新落盘", pc._ephemeral is False
              and len(S.load(tmp_bad).get(S.PANEL_SECRET_KEY) or "") >= 32)
        check("恢复后的 token 跨实例有效", S.PanelSessions(lambda: tmp_bad).valid(pcc))
    finally:
        shutil.rmtree(tmp_bad, ignore_errors=True)

    # ---- ⑧ 后端 PANEL 绑的是运行时 ACCOUNTS_DIR（不是启动时的快照）----
    src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("PANEL 用 callable 延迟绑定 ACCOUNTS_DIR",
          "PanelSessions(lambda: ACCOUNTS_DIR)" in src)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

finish()
