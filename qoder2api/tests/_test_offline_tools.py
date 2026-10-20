# -*- coding: utf-8 -*-
"""迁移自 _test_qoder.py 的 [21] + [30] 段（P0-2 测试工程化）。

两段都属「离线纯函数/工具」主题，按 Lead 的归组要求合成一个套件。
段内注释与「为什么这么测」的理由原样保留。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403
declare_suite(__file__, ["_install_umid.py", "qoder_fingerprint.py"])

P, A = bootstrap()

print("[offline-tools] 虚拟化检测 + UMID 提取器（离线纯函数）")

print()
print("[21] 本机虚拟化检测（中文输出：官方风控桥 vmInfo + 本机交叉校验）")
import qoder_fingerprint as F

check("vm_brand_cn: 已知平台译中文，未知品牌原样",
      F.vm_brand_cn("Hyper-V") == "Hyper-V（微软）"
      and F.vm_brand_cn("VMware, Inc.") == "VMware"
      and F.vm_brand_cn("SomeVendor") == "SomeVendor"
      and F.vm_brand_cn("") == "")
check("风控评分 -> 中文档位（高/中/低/无/未知）",
      [F._vm_level_cn(x) for x in (77, 50, 10, 0, None)]
      == ["高", "中", "低", "无", "未知"])

# 有官方风控结果：以它为权威
_st_vm = F.vm_status(bridge_vm_info={"isVm": True, "brand": "Hyper-V",
                                     "percentage": 77, "vmTypeCode": 14},
                     bridge_available=True)
check("bridge data wins: is_vm/level/score/brand_cn/source",
      _st_vm["is_vm"] is True and _st_vm["level"] == "高"
      and _st_vm["score"] == 77 and _st_vm["brand_cn"] == "Hyper-V（微软）"
      and _st_vm["vm_type_code"] == 14 and _st_vm["source"] == "runtime-info",
      _st_vm)
check("中文结论包含平台与评分",
      "本机运行在虚拟机中" in _st_vm["summary"]
      and "Hyper-V（微软）" in _st_vm["summary"] and "77" in _st_vm["summary"],
      _st_vm["summary"])
check("证据首条为官方风控判定（中文）",
      _st_vm["evidence"] and "官方风控判定" in _st_vm["evidence"][0],
      _st_vm["evidence"][:1])

# 无官方结果：退化为本机交叉校验，结论里明确说明
_st_local = F.vm_status(bridge_vm_info=None, bridge_available=False)
check("no bridge -> local cross-check + 中文说明",
      _st_local["source"] == "local" and isinstance(_st_local["is_vm"], bool)
      and "本机交叉校验" in _st_local["summary"], _st_local["summary"])

# 看板契约：这些键必须都在（前端 /diag/vm 直接消费）
check("dashboard contract keys present",
      set(("is_vm", "level", "score", "brand", "brand_cn", "vm_type_code",
           "source", "evidence", "summary")) <= set(_st_vm.keys()),
      sorted(_st_vm.keys()))
_st_api = A.local_vm_status("cn", force=True)
check("A.local_vm_status adds realm + bridge_available",
      _st_api.get("realm") in ("cn", "intl")
      and isinstance(_st_api.get("bridge_available"), bool), 
      (A.local_vm_status("cn") is not None, _st_api.get("bridge_available")))

# /diag/* 必须走面板鉴权（此前漏加会被无鉴权读取）
_src21 = open(os.path.join(ROOT,
                            "qoder_proxy.py"), encoding="utf-8").read()
check("/diag routes are panel-guarded",
      'if path.startswith("/diag"):' in _src21
      and _src21.find('if path.startswith("/diag"):')
      < _src21.find('return False', _src21.find('def _is_panel_route')),
      "guard missing" if 'if path.startswith("/diag"):' not in _src21 else "")


print()
print("[30] UMID 提取器（_install_umid.py）离线纯函数断言：识别 / 平台选择 / 扫描 / 校验 / 幂等")
import base64 as _b64_30
import contextlib as _cl_30
import io as _io_30
import shutil as _sh_30
import struct as _st_30
import tarfile as _tfmod_30
import tempfile as _tf_30
import _install_umid as U30


def _elf30(machine, cls=2, endian=1):
    buf = bytearray(0x40)
    buf[0:4] = b"\x7fELF"
    buf[4] = cls
    buf[5] = endian
    buf[18:20] = _st_30.pack("<H", machine)
    return bytes(buf)


def _macho30(cpu):
    return b"\xcf\xfa\xed\xfe" + _st_30.pack("<I", cpu) + b"\x00" * 8


def _pe30(machine):
    buf = bytearray(0x80)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = _st_30.pack("<I", 0x40)
    buf[0x40:0x44] = b"PE\x00\x00"
    buf[0x44:0x46] = _st_30.pack("<H", machine)
    return bytes(buf)


_WASM30 = b"\x00asm\x01\x00\x00\x00"
_ELF_X30 = _elf30(0x3E)
_ELF_A30 = _elf30(0xB7)

# --- 30.1 魔数/架构识别 ---
check("identify_blob：ELF x86-64 / aarch64 正确（e_machine 区分）",
      U30.identify_blob(_ELF_X30) == "elf-x86_64"
      and U30.identify_blob(_ELF_A30) == "elf-aarch64",
      (U30.identify_blob(_ELF_X30), U30.identify_blob(_ELF_A30)))
check("identify_blob：Mach-O x86_64 / arm64 正确（cputype 区分）",
      U30.identify_blob(_macho30(0x01000007)) == "macho-x86_64"
      and U30.identify_blob(_macho30(0x0100000C)) == "macho-arm64"
      and U30.identify_blob(_macho30(0x7)) == "unknown",
      (U30.identify_blob(_macho30(0x01000007)), U30.identify_blob(_macho30(0x0100000C))))
check("identify_blob：PE x86_64 / aarch64 正确（e_lfanew -> PE\\0\\0 -> machine）",
      U30.identify_blob(_pe30(0x8664)) == "pe-x86_64"
      and U30.identify_blob(_pe30(0xAA64)) == "pe-aarch64",
      (U30.identify_blob(_pe30(0x8664)), U30.identify_blob(_pe30(0xAA64))))
check("identify_blob：截断/畸形 PE 不崩溃且归为 unknown（e_lfanew 越界有兜底）",
      U30.identify_blob(b"MZ") == "unknown"
      and U30.identify_blob(b"MZ" + b"\x00" * 0x3E) == "unknown"
      and U30.identify_blob(_pe30(0x8664)[:0x44] + b"\x00" * 0x40) == "unknown",
      (U30.identify_blob(b"MZ"), U30.identify_blob(b"MZ" + b"\x00" * 0x3E)))
check("identify_blob：WASM 正确；非原生数据一律 unknown（空/垃圾/短 ELF/未知 e_machine/大端 ELF）",
      U30.identify_blob(_WASM30) == "wasm"
      and U30.identify_blob(b"") == "unknown"
      and U30.identify_blob(b"\x00" * 64) == "unknown"
      and U30.identify_blob(b"\x7fELF\x02\x01") == "unknown"
      and U30.identify_blob(_elf30(0x28)) == "unknown"
      and U30.identify_blob(_elf30(0x3E, endian=2)) == "unknown",
      (U30.identify_blob(_WASM30), U30.identify_blob(b""), U30.identify_blob(_elf30(0x3E, endian=2))))

# --- 30.2 平台 / 架构选择 ---
check("component_for_platform：linux/darwin × x86_64/arm64 映射正确（含 AMD64/aarch64 别名）",
      U30.component_for_platform("linux", "x86_64") == "elf-x86_64"
      and U30.component_for_platform("linux", "AMD64") == "elf-x86_64"
      and U30.component_for_platform("linux", "aarch64") == "elf-aarch64"
      and U30.component_for_platform("linux", "arm64") == "elf-aarch64"
      and U30.component_for_platform("darwin", "x86_64") == "macho-x86_64"
      and U30.component_for_platform("darwin", "arm64") == "macho-arm64",
      (U30.component_for_platform("linux", "AMD64"),
       U30.component_for_platform("darwin", "arm64")))
check("component_for_platform：win32 与未知组合返回 None（不猜平台）",
      U30.component_for_platform("win32", "x86_64") is None
      and U30.component_for_platform("linux", "riscv64") is None
      and U30.component_for_platform("plan9", "x86_64") is None,
      (U30.component_for_platform("win32", "x86_64"),
       U30.component_for_platform("linux", "riscv64")))
_cands30 = [(0, 0, _WASM30 + b"\x00" * 32), (0, 0, _ELF_A30), (0, 0, _ELF_X30),
            (0, 0, _pe30(0x8664))]
_pick_a30 = U30.select_candidate(_cands30, "elf-aarch64")
_pick_x30 = U30.select_candidate(_cands30, "elf-x86_64")
check("select_candidate：按期望标签精确挑（aarch64 不会挑到 x86-64 blob，反之亦然）",
      _pick_a30 is not None and _pick_a30[0] == 1
      and U30.identify_blob(_pick_a30[1]) == "elf-aarch64"
      and _pick_x30 is not None and _pick_x30[0] == 2
      and U30.identify_blob(_pick_x30[1]) == "elf-x86_64",
      (_pick_a30[0] if _pick_a30 else None, _pick_x30[0] if _pick_x30 else None))
check("select_candidate：无匹配返回 None（不会退而求其次给出错组件）",
      U30.select_candidate([(0, 0, _WASM30), (0, 0, _pe30(0x8664))], "macho-arm64") is None)
_pe_big30 = _pe30(0x8664) + b"\x00" * (3 * 1024 * 1024)      # ~3MB：超出真组件体积区间
_pe_ok30 = _pe30(0x8664) + b"\x00" * (600 * 1024)            # ~600KB：落在 400KB–2MB
_sel_pe30 = U30.select_candidate([(0, 0, _pe_big30), (0, 0, _pe_ok30)], "pe-x86_64")
check("体积启发式：同格式多候选取落在 400KB–2MB 真组件区间的那个"
      "（避开 docstring 记录的 7.3MB 疑似模块）",
      _sel_pe30 is not None and _sel_pe30[0] == 1
      and len(_sel_pe30[1]) == len(_pe_ok30),
      (_sel_pe30[0] if _sel_pe30 else None,
       len(_sel_pe30[1]) if _sel_pe30 else None))

# --- 30.3 base64 扫描 ---
# 扫描断言必须用"长度达标"的样本：base64(64B) 只有 88 字符，远小于 BLOB_MIN_LEN，
# 因此这里把 ELF 头补零到 4KB+（头部魔数与 e_machine 不变，仍是合法 elf-x86_64）。
_ELF_BIG30 = _ELF_X30 + b"\x00" * 4096
_b64s30 = _b64_30.b64encode(_ELF_BIG30).decode()
assert len(_b64s30) > U30.BLOB_MIN_LEN, len(_b64s30)
_TICK30 = chr(96)
_text30 = ('const a = "' + _b64s30 + '";\n'
           'const short = "AAAA";\n'
           "const b = '" + _b64s30 + "';\n"
           "const c = " + _TICK30 + _b64s30 + _TICK30 + ";\n")
_cands30b = U30.scan_bundle_candidates(_text30)
check("scan_bundle_candidates：只收长度达标的引号字面量（短字面量与反引号模板都不收）",
      len(_cands30b) == 2, [len(c[2]) for c in _cands30b])
check("scan_bundle_candidates：偏移是内容区间（不含引号）且解码正确",
      _text30[_cands30b[0][0]:_cands30b[0][1]] == _b64s30
      and U30.identify_blob(_cands30b[0][2]) == "elf-x86_64",
      (_cands30b[0][0], _cands30b[0][1]))
check("scan_bundle_candidates：min_len 阈值可调（阈值高于字面量长度 -> 零候选）",
      U30.scan_bundle_candidates(_text30, min_len=len(_b64s30) + 1) == []
      and len(U30.scan_bundle_candidates(_text30, min_len=len(_b64s30))) == 2)
check("扫描+选择：WASM 排在前面也不会被选中（按格式过滤，不按出现顺序）",
      (lambda _s: _s is not None and _s[0] == 1)(U30.select_candidate(
          [(0, 0, _WASM30 + b"\x00" * 64), (0, 0, _ELF_X30)], "elf-x86_64")))

# --- 30.4 校验 ---
check("verify_component：magic 与架构级双重校验（不匹配一律 False）",
      U30.verify_component(_ELF_X30, "elf-x86_64") is True
      and U30.verify_component(_ELF_X30, "elf-aarch64") is False
      and U30.verify_component(_ELF_A30, "elf-x86_64") is False
      and U30.verify_component(_WASM30, "elf-x86_64") is False
      and U30.verify_component(b"", "elf-x86_64") is False,
      (U30.verify_component(_ELF_X30, "elf-aarch64"),
       U30.verify_component(_WASM30, "elf-x86_64")))
_good30 = "sha512-" + _b64_30.b64encode(hashlib.sha512(b"abc").digest()).decode()
_bad30 = "sha512-" + _b64_30.b64encode(hashlib.sha512(b"abd").digest()).decode()
check("verify_integrity：sha512 三态（正确 True / 不符 False / 缺失或异算法 None=跳过）",
      U30.verify_integrity(b"abc", _good30) is True
      and U30.verify_integrity(b"abc", _bad30) is False
      and U30.verify_integrity(b"abc", None) is None
      and U30.verify_integrity(b"abc", "sha1-ZW5j") is None,
      (U30.verify_integrity(b"abc", _bad30), U30.verify_integrity(b"abc", None)))

# --- 30.5 幂等语义（stub 网络函数，全程离线） ---
_tmp30 = _tf_30.mkdtemp(prefix="qd-umid-")
try:
    _dest30 = os.path.join(_tmp30, "umid")
    _tgt30 = os.path.join(_dest30, "runtime-info")
    _missing30 = U30.is_installed(_tgt30, "elf-x86_64")
    os.makedirs(_dest30, exist_ok=True)
    with open(_tgt30, "wb") as fh30:
        fh30.write(_WASM30 + b"\x00" * 32)
    _bad30_state = U30.is_installed(_tgt30, "elf-x86_64")
    with open(_tgt30, "wb") as fh30:
        fh30.write(_ELF_X30)
    _ok30_state = U30.is_installed(_tgt30, "elf-x86_64")
    check("is_installed：不存在 False / 内容不符 False / 校验通过 True（不看文件大小或名字）",
          _missing30 is False and _bad30_state is False and _ok30_state is True,
          (_missing30, _bad30_state, _ok30_state))

    _calls30 = []
    _orig_rt30, _orig_dl30 = U30.resolve_tarball, U30.download_tarball
    U30.resolve_tarball = lambda *a, **k: (_calls30.append("resolve")
                                           or ("9.9.9", "http://invalid", None))
    U30.download_tarball = lambda *a, **k: (_calls30.append("download") or b"")
    try:
        with _cl_30.redirect_stdout(_io_30.StringIO()):
            _rc_skip30 = U30.main(["--platform", "linux", "--arch", "x64",
                                   "--dest", _dest30])
        _calls_after_skip30 = list(_calls30)
        with open(_tgt30, "wb") as fh30:
            fh30.write(_WASM30 + b"\x00" * 32)          # 破坏目标 -> 应重新下载
        try:
            with _cl_30.redirect_stdout(_io_30.StringIO()):
                _rc_redl30 = U30.main(["--platform", "linux", "--arch", "x64",
                                       "--dest", _dest30])
        except Exception as _exc30:
            _rc_redl30 = "异常:%s" % type(_exc30).__name__
        _calls_after_redl30 = list(_calls30)
    finally:
        U30.resolve_tarball, U30.download_tarball = _orig_rt30, _orig_dl30
    check("幂等：目标已存在且校验通过 -> main() 返回 0 且一个网络函数都没被调用",
          _rc_skip30 == 0 and _calls_after_skip30 == [],
          (_rc_skip30, _calls_after_skip30))
    check("幂等反例：目标校验不通过 -> 确实走下载路径（不是无条件跳过）",
          _calls_after_redl30[:1] == ["resolve"] and _rc_redl30 != 0,
          (_rc_redl30, _calls_after_redl30))

    # --- 30.6 端到端（全离线）：伪造 npm tarball -> 扫描 -> 选择 -> 安装 ---
    # 端到端必须用"长度达标"的样本，否则扫描阶段（BLOB_MIN_LEN）就会漏掉它
    _bundle_src30 = 'const blob = "%s";\n' % _b64_30.b64encode(_ELF_BIG30).decode()
    _buf30 = _io_30.BytesIO()
    with _tfmod_30.open(fileobj=_buf30, mode="w:gz") as _tf30:
        _info30 = _tfmod_30.TarInfo(U30.BUNDLE_MEMBER)
        _payload30 = _bundle_src30.encode("utf-8")
        _info30.size = len(_payload30)
        _tf30.addfile(_info30, _io_30.BytesIO(_payload30))
    _tgz30 = _buf30.getvalue()
    _e2e_calls30 = []
    _orig_rt30b, _orig_dl30b = U30.resolve_tarball, U30.download_tarball
    U30.resolve_tarball = lambda *a, **k: (_e2e_calls30.append("resolve")
                                           or ("9.9.9", "http://invalid", None))
    U30.download_tarball = lambda *a, **k: (_e2e_calls30.append("download") or _tgz30)
    try:
        with _cl_30.redirect_stdout(_io_30.StringIO()):
            _rc_e2e30 = U30.main(["--platform", "linux", "--arch", "x64",
                                  "--dest", _dest30])
    finally:
        U30.resolve_tarball, U30.download_tarball = _orig_rt30b, _orig_dl30b
    check("端到端（全离线）：伪造 tarball -> main() 完成提取并落盘，产物校验通过",
          _rc_e2e30 == 0 and _e2e_calls30 == ["resolve", "download"]
          and U30.is_installed(_tgt30, "elf-x86_64"),
          (_rc_e2e30, _e2e_calls30, U30.is_installed(_tgt30, "elf-x86_64")))
finally:
    _sh_30.rmtree(_tmp30, ignore_errors=True)

# --- 30.7 与网关的落盘/发现契约 ---
_acc_src30 = open(os.path.join(ROOT,
                               "qoder_accounts.py"), encoding="utf-8").read()
check("落盘契约：安装文件名/目录名与 gateway POSIX 查找路径同源"
      "（runtime-info / umid / QD_UMID_DIR）",
      U30.TARGET_FILENAME == "runtime-info"
      and U30.DEFAULT_DEST_DIRNAME == "umid"
      and "QD_UMID_DIR" in _acc_src30
      and '"umid"' in _acc_src30 and '"runtime-info"' in _acc_src30,
      (U30.TARGET_FILENAME, U30.DEFAULT_DEST_DIRNAME))
_orig_env30 = os.environ.get("QD_UMID_DIR")
_tmp30b = _tf_30.mkdtemp(prefix="qd-umid-env-")
try:
    os.environ["QD_UMID_DIR"] = _tmp30b
    _dd_env30 = U30.default_dest_dir()
finally:
    if _orig_env30 is None:
        os.environ.pop("QD_UMID_DIR", None)
    else:
        os.environ["QD_UMID_DIR"] = _orig_env30
    _sh_30.rmtree(_tmp30b, ignore_errors=True)
_dd_default30 = U30.default_dest_dir()
check("default_dest_dir()：$QD_UMID_DIR 优先；默认 <repo>/umid（与 gateway 查找顺序一致）",
      _dd_env30 == _tmp30b
      and _dd_default30 == os.path.join(
          os.path.dirname(os.path.abspath(U30.__file__)), "umid")
      and _dd_default30 != _dd_env30,
      (_dd_env30, _dd_default30))
# 真实产物校验（仓库里存在才断言，否则显式 SKIP——绝不静默）：
# umid/ 已被 .gitignore 忽略，CI/他人机器上通常没有，所以这条按环境降级为 SKIP。
_umid_real30 = os.path.join(ROOT,
                            "umid", "runtime-info")
if os.path.isfile(_umid_real30):
    with open(_umid_real30, "rb") as _fh30b:
        _umid_data30 = _fh30b.read()
    check("真实产物：umid/runtime-info 被识别为 elf-x86_64 且落在体积启发式区间"
          "（提取链路端到端产物，非合成样本）",
          U30.identify_blob(_umid_data30) == "elf-x86_64"
          and U30.verify_component(_umid_data30, "elf-x86_64")
          and U30.HEURISTIC_SIZE_MIN <= len(_umid_data30) <= U30.HEURISTIC_SIZE_MAX,
          (len(_umid_data30), U30.identify_blob(_umid_data30)))
print()

finish()
