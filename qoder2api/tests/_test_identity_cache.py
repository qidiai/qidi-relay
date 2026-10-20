# -*- coding: utf-8 -*-
"""迁移自 _test_qoder.py 的 [35] 段（P0-2 测试工程化）。

段内注释与「为什么这么测」的理由原样保留 —— 那是前面几十轮踩坑换来的。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403
declare_suite(__file__, ["qoder_accounts.py"])

P, A = bootstrap()

print("[35] 机器身份落盘缓存")
print()
print("[35] 机器身份落盘缓存（task-48 · 设计 v1.2.13 第七节：12 条离线）")
import shutil as _sh35
import tempfile as _tf35
import threading as _th35

_IDC_KEYS = ("ACCOUNTS_DIR", "QD_MACHINE_IDENTITY_CACHE",
             "QD_MACHINE_IDENTITY_CACHE_TTL", "QD_MACHINE_IDENTITY_RESET",
             "QD_MACHINE_IDENTITY_VOTE")
_IDC_ORIG_ENV = {_k: os.environ.get(_k) for _k in _IDC_KEYS}
_IDC_ORIG_RUN = A.run_runtime_info
_IDC_TMP = _tf35.mkdtemp(prefix="qd-idcache-")
_IDC_FILE = os.path.join(_IDC_TMP, "machine_identity.json")


def _idc_env(**over):
    """切换 ACCOUNTS_DIR 与三个开关，并清内存缓存与落盘文件。"""
    os.environ["ACCOUNTS_DIR"] = over.get("accounts_dir") or _IDC_TMP
    for _k in _IDC_KEYS[1:]:
        if over.get(_k) is not None:
            os.environ[_k] = str(over[_k])
        else:
            os.environ.pop(_k, None)
    A._native_ident_cache.clear()
    try:
        os.remove(_IDC_FILE)
    except OSError:
        pass


def _idc_stub(calls, fail=False, seq=None):
    """替换组件入口：记录调用次数并返回**恒定**身份（保证投票 3/3 一致）。

    seq 给定时按调用序号循环取样本（用于构造多数派/全分歧场景）。
    """
    def _f(realm, account_id=""):
        calls.append((realm, account_id))
        if fail:
            return {}
        if seq:
            s = seq[(len(calls) - 1) % len(seq)]
            return dict(s)
        return {"machineToken": "tok-1", "machineType": "ty1",
                "machineCode": "co1", "vmInfo": {"isVm": False}}
    return _f


def _idc_read():
    try:
        with open(_IDC_FILE, encoding="utf-8") as _fh:
            return json.load(_fh)
    except Exception:
        return None


def _idc_tok(blob):
    try:
        return (blob.get("realm") or {}).get("cn", {}).get("machineToken")
    except Exception:
        return None


try:
    _idc_env()
    _c1 = []
    A.run_runtime_info = _idc_stub(_c1)
    _i1 = A.native_machine_identity("cn", "u1")
    _f1 = _idc_read()
    check("#48-1 首次调用：无缓存 → 组件被调 **3** 次（首次表决）并落盘",
          len(_c1) == 3 and _i1.get("machineToken") == "tok-1"
          and isinstance(_f1, dict) and _f1.get("version") == 1
          and _idc_tok(_f1) == "tok-1"
          and ((_f1.get("realm") or {}).get("cn", {}).get("machineType") == "ty1")
          and ((_f1.get("realm") or {}).get("cn", {}).get("machineCode") == "co1"),
          (_c1, _f1))

    A._native_ident_cache.clear()
    _c2 = []
    A.run_runtime_info = _idc_stub(_c2)
    _i2 = A.native_machine_identity("cn", "u2")
    check("#48-2 【命门】落盘缓存命中：第二次调用**不触发组件**（桩计数=0）且身份逐字节相同",
          len(_c2) == 0 and _i2.get("machineToken") == "tok-1"
          and _i2.get("machineType") == "ty1", (_c2, _i2))

    _c3 = []
    A.run_runtime_info = _idc_stub(_c3)
    _i3 = A.native_machine_identity("cn", "u3", force=True)
    check("#48-3 force=True：必调组件并覆盖落盘缓存",
          len(_c3) == 1 and _i3.get("machineToken") == "tok-1"
          and _idc_tok(_idc_read()) == "tok-1", (_c3, _idc_read()))

    # 设计 §3.2 行 3 的场景：缓存【已过期】→ 调组件 → 组件失败 → 回退到过期缓存。
    # （缓存未过期时按行 2 根本不会调组件，那种构造断言不到"回退"路径。）
    _idc_env(QD_MACHINE_IDENTITY_CACHE_TTL="1")
    _c4a = []
    A.run_runtime_info = _idc_stub(_c4a)
    _i4a = A.native_machine_identity("cn", "u4")
    _f4 = _idc_read()
    try:
        _f4["realm"]["cn"]["cached_at"] = time.time() - 10      # 人为过期
        with open(_IDC_FILE, "w", encoding="utf-8") as _fh:
            json.dump(_f4, _fh, ensure_ascii=False)
    except Exception:
        pass
    A._native_ident_cache.clear()
    _c4b = []
    A.run_runtime_info = _idc_stub(_c4b, fail=True)
    _i4b = A.native_machine_identity("cn", "u4b")
    check("#48-4 组件失败但落盘有（过期）缓存 → 仍返回缓存身份，且不写坏缓存文件"
          "（首次表决 3 次、TTL 轮换 1 次）",
          len(_c4a) == 3 and len(_c4b) == 1
          and _i4b.get("machineToken") == _i4a.get("machineToken")
          and _idc_tok(_idc_read()) == _i4a.get("machineToken"),
          (_c4b, _i4b, _idc_read()))

    _idc_env()
    _c5 = []
    A.run_runtime_info = _idc_stub(_c5, fail=True)
    check("#48-5 组件失败且无缓存 → 表决 3 次全空 → 返回 {}（现状不变）",
          A.native_machine_identity("cn", "u5") == {} and len(_c5) == 3)

    _idc_env(QD_MACHINE_IDENTITY_CACHE="off")
    _c6 = []
    A.run_runtime_info = _idc_stub(_c6)
    A.native_machine_identity("cn", "u6")
    check("#48-6 QD_MACHINE_IDENTITY_CACHE=off → 不落盘（行为同旧）",
          _idc_read() is None and len(_c6) == 1, _idc_read())

    _idc_env(QD_MACHINE_IDENTITY_CACHE_TTL="1")
    _c7 = []
    A.run_runtime_info = _idc_stub(_c7)
    A.native_machine_identity("cn", "u7")
    _f7 = _idc_read()
    try:
        _f7["realm"]["cn"]["cached_at"] = time.time() - 10
        with open(_IDC_FILE, "w", encoding="utf-8") as _fh:
            json.dump(_f7, _fh, ensure_ascii=False)
    except Exception:
        pass
    A._native_ident_cache.clear()
    A.native_machine_identity("cn", "u7b")
    check("#48-7 TTL 正数：过期后重新调组件（首次表决 3 + 轮换 1 = 4）",
          len(_c7) == 4, len(_c7))

    _idc_env()
    _c8a = []
    A.run_runtime_info = _idc_stub(_c8a)
    A.native_machine_identity("cn", "u8")
    os.environ["QD_MACHINE_IDENTITY_RESET"] = "1"
    A._native_ident_cache.clear()
    _c8b = []
    A.run_runtime_info = _idc_stub(_c8b)
    _i8 = A.native_machine_identity("cn", "u8b")
    check("#48-8 QD_MACHINE_IDENTITY_RESET=1 → 清空缓存并重新取身份",
          len(_c8b) >= 1 and bool(_i8.get("machineToken")), (len(_c8a), len(_c8b)))

    _idc_env()
    with open(_IDC_FILE, "w", encoding="utf-8") as _fh:
        _fh.write('{"version": 1, "realm": {"cn": {"machineToken": "trunc')
    _c9 = []
    A.run_runtime_info = _idc_stub(_c9)
    _err9 = None
    try:
        _i9 = A.native_machine_identity("cn", "u9")
    except Exception as _e9:
        _err9 = _e9
        _i9 = {}
    check("#48-9 缓存文件损坏（截断 JSON）→ 不抛异常，回退表决 3 次",
          _err9 is None and len(_c9) == 3 and bool((_i9 or {}).get("machineToken")),
          (_err9, len(_c9)))

    _idc_env()
    _c10 = []
    _lk10 = _th35.Lock()

    def _slow10(realm, account_id=""):
        with _lk10:
            _c10.append(1)
            _n = len(_c10)
        time.sleep(0.05)
        return {"machineToken": "tok-%d" % _n, "machineType": "ty", "machineCode": "co"}

    A.run_runtime_info = _slow10
    _th35.Thread(target=lambda: A.native_machine_identity("cn", "t1")).start()
    _th35.Thread(target=lambda: A.native_machine_identity("cn", "t2")).start()
    time.sleep(0.6)
    check("#48-10 并发首次：文件仍可解析（不损坏），且至少写入一次",
          isinstance(_idc_read(), dict) and _idc_tok(_idc_read()) is not None,
          _idc_read())

    _idc_env()
    _c11 = []
    # 桩要"两次采样给不同身份"：首次表决 3 次得 tok-a、force 刷新得 tok-b，
    # 才能区分"只更新内存"与"落盘也被更新"。
    A.run_runtime_info = _idc_stub(_c11, seq=[
        {"machineToken": "tok-a", "machineType": "ty-a", "machineCode": "co-a",
         "vmInfo": {"isVm": False}}] * 3
        + [{"machineToken": "tok-b", "machineType": "ty-b", "machineCode": "co-b",
            "vmInfo": {"isVm": False}}])
    A.native_machine_identity("cn", "u11")
    _tok_before11 = _idc_tok(_idc_read())
    _acc11 = A.Account({"uid": "heal48", "realm": "cn", "accessToken": "dt-x"})
    _acc11.machine_identity_source = "runtime-info"
    _orig_get11 = A.Account._campaigns_get
    _seq11 = []

    def _cget11(self):
        _seq11.append(1)
        return (({"showCampaign": False} if len(_seq11) == 1
                 else {"showCampaign": True}), 200, "")

    A.Account._campaigns_get = _cget11
    try:
        _acc11.campaigns(force=True)
    finally:
        A.Account._campaigns_get = _orig_get11
    _tok_after11 = _idc_tok(_idc_read())
    check("#48-11 自愈路径：列表被拒 → force 刷新 → **落盘被更新**（不只更新内存）",
          len(_c11) >= 2 and _tok_before11 != _tok_after11,
          (_tok_before11, _tok_after11, len(_c11)))

    _idc_env()
    with open(_IDC_FILE, "w", encoding="utf-8") as _fh:
        json.dump({"version": 1, "realm": {"cn": {"machineToken": ""}}}, _fh)
    _c12 = []
    A.run_runtime_info = _idc_stub(_c12)
    _i12 = A.native_machine_identity("cn", "u12")
    check("#48-12 缓存字段缺失（machineToken 空）→ 视为无缓存，回退表决 3 次",
          len(_c12) == 3 and (_i12 or {}).get("machineToken") == "tok-1",
          (len(_c12), _i12))

    check("#48-13a 表决上限常量：IDENTITY_VOTE_ROUNDS=3 + EXTEND=2 → 上限 5（延迟有界）",
          getattr(A, "IDENTITY_VOTE_ROUNDS", None) == 3
          and getattr(A, "IDENTITY_VOTE_EXTEND_ROUNDS", None) == 2,
          (getattr(A, "IDENTITY_VOTE_ROUNDS", None),
           getattr(A, "IDENTITY_VOTE_EXTEND_ROUNDS", None)))

    # ---- 13 表决：3 次同值 → 组件被调 3 次并采纳（不补投） ----
    _idc_env()
    _c13 = []
    A.run_runtime_info = _idc_stub(_c13)
    _i13 = A.native_machine_identity("cn", "v13")
    check("#48-13 表决·**3/3 一致时不补投**（延迟有界：11s 档）→ 组件恰好被调 3 次并采纳",
          len(_c13) == 3 and _i13.get("machineToken") == "tok-1", (len(_c13), _i13))

    # ---- 14 表决：含分歧 → 采纳多数派【完整样本】(自适应实现会补投到 5 次) ----
    _idc_env()
    _maj14 = {"machineToken": "tok-maj", "machineType": "ty-maj", "machineCode": "co-maj",
              "vmInfo": {"isVm": True, "brand": "KVM", "vmTypeCode": 13}}
    _min14 = {"machineToken": "tok-min", "machineType": "ty-min", "machineCode": "co-min",
              "vmInfo": {"isVm": True, "brand": "Docker", "vmTypeCode": 50}}
    _c14 = []
    A.run_runtime_info = _idc_stub(_c14, seq=[_maj14, _maj14, _min14, _min14, _maj14])
    _i14 = A.native_machine_identity("cn", "v14")
    _vm14 = _i14.get("vm_info") or {}
    check("#48-14 表决·前 3 轮有分歧 → **补投到 5 次**，采纳多数派**完整样本**"
          "（type/code/vmInfo 自洽不杂交）",
          _i14.get("machineToken") == "tok-maj"
          and _i14.get("machineType") == "ty-maj"
          and _i14.get("machineCode") == "co-maj"
          and _vm14.get("brand") == "KVM" and _vm14.get("vmTypeCode") == 13
          and len(_c14) == 5, (len(_c14), _i14, _c14))

    # ---- 15 表决：全分歧 → 取首个样本（确定性） ----
    _idc_env()
    _s15 = [{"machineToken": "t-a", "machineType": "ty-a", "machineCode": "co-a",
             "vmInfo": {"isVm": True, "brand": "KVM", "vmTypeCode": 13}},
            {"machineToken": "t-b", "machineType": "ty-b", "machineCode": "co-b",
             "vmInfo": {"isVm": True, "brand": "Docker", "vmTypeCode": 50}},
            {"machineToken": "t-c", "machineType": "ty-c", "machineCode": "co-c",
             "vmInfo": {"isVm": True, "brand": "Xen", "vmTypeCode": 7}}]
    _c15 = []
    A.run_runtime_info = _idc_stub(_c15, seq=_s15)
    _i15 = A.native_machine_identity("cn", "v15")
    check("#48-15 表决·全分歧：取**首个**样本（确定性，不杂交）",
          _i15.get("machineToken") == "t-a" and _i15.get("machineType") == "ty-a",
          (len(_c15), _i15))

    # ---- 16 VOTE=0 → 跳过表决，只调一次 ----
    _idc_env(QD_MACHINE_IDENTITY_VOTE="0")
    _c16 = []
    A.run_runtime_info = _idc_stub(_c16)
    _i16 = A.native_machine_identity("cn", "v16")
    check("#48-16 QD_MACHINE_IDENTITY_VOTE=0 → 跳过表决，组件只被调 1 次",
          len(_c16) == 1 and _i16.get("machineToken") == "tok-1", (len(_c16), _i16))

    # ---- 17 force → 不表决（单次） ----
    _idc_env()
    _c17 = []
    A.run_runtime_info = _idc_stub(_c17)
    _i17 = A.native_machine_identity("cn", "v17", force=True)
    check("#48-17 force=True：不表决（单次），但仍写入两份缓存",
          len(_c17) == 1 and _i17.get("machineToken") == "tok-1"
          and _idc_tok(_idc_read()) == "tok-1", (len(_c17), _idc_read()))

    def _s18(_tag, _brand, _type_code):
        return {"machineToken": "tok-" + _tag, "machineType": "ty-" + _tag,
                "machineCode": "co-" + _tag,
                "vmInfo": {"isVm": True, "brand": _brand, "vmTypeCode": _type_code}}

    # ---- 18 补投·4:1：前 3 轮分歧 → 补到 5，采纳 4 票多数派 ----
    _idc_env()
    _A18, _B18 = _s18("a", "KVM", 13), _s18("b", "Docker", 50)
    _c18 = []
    A.run_runtime_info = _idc_stub(_c18, seq=[_A18, _A18, _B18, _A18, _A18])
    _i18 = A.native_machine_identity("cn", "v18")
    check("#48-18 补投·4:1 → 组件被调 **5** 次、采纳 4 票的完整样本（A）",
          len(_c18) == 5 and _i18.get("machineToken") == "tok-a"
          and (_i18.get("vm_info") or {}).get("brand") == "KVM", (len(_c18), _i18))

    # ---- 19 补投·3:2：同样补到 5，采纳 3 票多数派 ----
    _idc_env()
    _c19 = []
    A.run_runtime_info = _idc_stub(_c19, seq=[_A18, _A18, _B18, _A18, _B18])
    _i19 = A.native_machine_identity("cn", "v19")
    check("#48-19 补投·3:2 → 组件被调 **5** 次、采纳 3 票的完整样本（A）",
          len(_c19) == 5 and _i19.get("machineToken") == "tok-a"
          and (_i19.get("machine_code") or _i19.get("machineCode")) == "co-a",
          (len(_c19), _i19))

    # ---- 20 补投·平票：2/2/1 无多数 → 取首个样本 ----
    _idc_env()
    _C20 = _s18("c", "WSL", 7)
    _c20 = []
    A.run_runtime_info = _idc_stub(_c20, seq=[_A18, _A18, _B18, _B18, _C20])
    _i20 = A.native_machine_identity("cn", "v20")
    check("#48-20 补投·平票(2/2/1) → 组件被调 **5** 次、无多数时取**首个**样本（A）",
          len(_c20) == 5 and _i20.get("machineToken") == "tok-a"
          and (_i20.get("vm_info") or {}).get("brand") == "KVM", (len(_c20), _i20))
finally:
    A.run_runtime_info = _IDC_ORIG_RUN
    A._native_ident_cache.clear()
    for _k in _IDC_KEYS:
        if _IDC_ORIG_ENV[_k] is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _IDC_ORIG_ENV[_k]
    _sh35.rmtree(_IDC_TMP, ignore_errors=True)

finish()
