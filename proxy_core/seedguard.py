"""种子卫兵（0905 吸收自 ProxyDeck seedcheck.go 的思路）。

官方侧封禁反代的核心轴心是请求签名（X-Codely-Signature）。本中转签名方案的
种子（keystore.SIGNING_ROOT_KEY）是逆向官方 CLI 得到并硬编码的。官方 CLI 轮换
种子/更换签名方案后，本中转的签名会被"安全门"整体拒绝——2026-09-05 账号
<账号ID> 的 401"上游安全门拒绝签名/请升级到最新版 Codely"就是此类事件。

双保险监测线：
  本地层：扫描本机已装的 Tuanjie Cowork / codely-cli 的 bundle（gemini.js），
          核对签名特征串是否仍存在（本机不升级就永远看旧包）。
  在线层：查 npm registry @unity-china/codely-cli 的 dist-tags.latest，版本有
          变化才下载该版本 tgz、解出 bundle/gemini.js 做同样特征核对——官方
          当前真实在用的签名方案。扫描/下载失败只记状态，不算告警（网络问题
          ≠ 官方换锁），与本地层独立；任一层特征消失即告警。

告警出口：log.warning + seed_state.json + GET /api/seed-alert（管理面板）。
"""

import gzip
import io
import json
import logging
import os
import tarfile
import threading
import time

import httpx

from .keystore import SIGNING_ROOT_KEY

try:
    from .settings import BASE_DIR
except ImportError:  # 兜底：直接取仓库根
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("qidi-relay")

# ── 特征串（任一从官方 bundle 里消失 = 签名方案变了）─────────────────────
FEATURES = {
    "signing_seed": SIGNING_ROOT_KEY.hex(),
    "header_name": "X-Codely-Signature",
    "sign_info": "codely-signing-v1",
}

REGISTRY_URL = "https://registry.npmjs.org/@unity-china/codely-cli"
BUNDLE_MEMBER = "bundle/gemini.js"
CHECK_INTERVAL = 3600          # 每小时一查
TARBALL_MAX = 64 * 1024 * 1024  # tgz 下载上限 64MB（ProxyDeck 实测 16MB 级）
BUNDLE_READ_MAX = 64 * 1024 * 1024

_state_path = os.path.join(str(BASE_DIR), "seed_state.json")
_lock = threading.Lock()
_started = False

_state = {
    "last_check_at": 0,
    "alert": False,
    "alert_detail": "",
    "checked_version": "",   # 在线层已核验过的 npm 版本（防重复下载 16MB）
    "local": {"found": False, "path": "", "ok": None, "missing": []},
    "online": {"version": "", "checked": False, "ok": None, "missing": []},
    "history": [],
}


def _load_state():
    global _state
    try:
        with open(_state_path, encoding="utf-8") as f:
            saved = json.load(f)
        _state.update({k: saved[k] for k in ("checked_version",) if k in saved})
    except Exception:
        pass


def _save_state():
    try:
        with open(_state_path, "w", encoding="utf-8") as f:
            json.dump({k: _state[k] for k in
                       ("checked_version", "alert", "alert_detail",
                        "last_check_at")}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.debug("seed_state.json 写入失败: %s", e)


def _local_bundle_candidates():
    """本机 bundle 候选：Tuanjie Cowork 安装目录（找 gemini.js）+ npm 全局路径。"""
    cands = []
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        base = os.path.join(la, "Programs", "Tuanjie Cowork", "cli")
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d.lower() not in ("node_modules",)]
                for fn in files:
                    if fn.lower() == "gemini.js":
                        cands.append(os.path.join(root, fn))
                if len(cands) >= 3:
                    break
        if not cands:
            # 桌面端也可能把 bundle 放在安装根（逐层找一次）
            root_dir = os.path.join(la, "Programs", "Tuanjie Cowork")
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs
                           if d.lower() not in ("node_modules", "logs", "cache")]
                for fn in files:
                    if fn.lower() == "gemini.js":
                        cands.append(os.path.join(root, fn))
                if len(cands) >= 3:
                    break
    npm = os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules",
                       "@unity-china", "codely-cli", "bundle", "gemini.js")
    if os.path.isfile(npm):
        cands.append(npm)
    return cands


def _check_features(data: bytes):
    """核对特征串。返回 (ok, missing_keys)。"""
    missing = [k for k, v in FEATURES.items() if v.encode("utf-8") not in data]
    return (not missing), missing


def check_local() -> dict:
    """本地层：读本机 bundle 核对特征。找不到 bundle 只记 found=False 不告警。"""
    res = {"found": False, "path": "", "ok": None, "missing": []}
    for cand in _local_bundle_candidates():
        try:
            if os.path.getsize(cand) > BUNDLE_READ_MAX:
                continue
            with open(cand, "rb") as f:
                data = f.read(BUNDLE_READ_MAX)
        except Exception as e:
            log.debug("种子卫兵读本地 bundle 失败 %s: %s", cand, e)
            continue
        ok, missing = _check_features(data)
        res.update(found=True, path=cand, ok=ok, missing=missing)
        break
    return res


def _packument_latest() -> tuple:
    """查 npm registry。返回 (latest_version, tarball_url)。"""
    with httpx.Client(timeout=15) as c:
        r = c.get(REGISTRY_URL, headers={"User-Agent": "qidi-relay-seedguard"})
    r.raise_for_status()
    doc = r.json()
    latest = (doc.get("dist-tags") or {}).get("latest", "")
    tarball = ((doc.get("versions") or {}).get(latest) or {}).get(
        "dist", {}).get("tarball", "")
    return latest, tarball


def _check_online() -> dict:
    """在线层：版本变化才下载 tgz 核对（16MB 级）。"""
    res = {"version": "", "checked": False, "ok": None, "missing": []}
    try:
        latest, tarball = _packument_latest()
    except Exception as e:
        log.info("[种子卫兵] npm registry 查询失败（不算告警）: %s", e)
        return res
    res["version"] = latest
    if not latest or latest == _state.get("checked_version") or not tarball:
        # 版本没变：沿用上次在线结论（防告警一小时后自动清零）
        prev = _state.get("online", {})
        if prev.get("version") == latest and prev.get("checked"):
            res["checked"] = True
            res["ok"] = prev.get("ok")
            res["missing"] = prev.get("missing", [])
        return res
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0)) as c:
            with c.stream("GET", tarball) as r:
                r.raise_for_status()
                chunks, total = [], 0
                for chunk in r.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > TARBALL_MAX:
                        raise ValueError("tarball 超过 64MB 上限")
                    chunks.append(chunk)
                raw = b"".join(chunks)
        data = None
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            member = None
            for m in tf.getmembers():
                if m.name.endswith(BUNDLE_MEMBER) or m.name.endswith("/gemini.js"):
                    member = m
                    if m.name == BUNDLE_MEMBER:
                        break
            if member is not None:
                fobj = tf.extractfile(member)
                if fobj is not None:
                    data = fobj.read(BUNDLE_READ_MAX)
        if data is None:
            log.warning("[种子卫兵] npm %s 的 tgz 里没有 %s，无法核对", latest, BUNDLE_MEMBER)
            res["checked"] = True
            return res
        ok, missing = _check_features(data)
        res.update(checked=True, ok=ok, missing=missing)
        log.info("[种子卫兵] 在线核对 npm %s 完成：%s",
                 latest, "特征齐全" if ok else f"缺失 {missing}")
    except Exception as e:
        log.info("[种子卫兵] npm tgz 下载/解包失败（不算告警）: %s", e)
    return res


def run_check() -> dict:
    """跑一轮（本地 + 在线），更新告警状态并持久化。"""
    now = time.time()
    local = check_local()
    online = _check_online()

    alert = False
    details = []
    if local["found"] and local["ok"] is False:
        alert = True
        details.append("本机 CLI bundle 缺失签名特征: " + ", ".join(local["missing"]))
    if online["checked"] and online["ok"] is False:
        alert = True
        details.append(
            f"npm {online['version']} bundle 缺失签名特征: "
            + ", ".join(online["missing"]) + "（官方可能已更换签名方案）")

    detail = "；".join(details)
    with _lock:
        _state["last_check_at"] = now
        _state["alert"] = alert
        _state["alert_detail"] = detail
        if online["checked"] and online["version"]:
            _state["checked_version"] = online["version"]  # 仅成功核对才记
        _state["local"] = local
        _state["online"] = online
        _state["history"] = ([{
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "alert": alert,
            "detail": detail,
            "version": online["version"],
        }] + _state["history"])[:20]
        _save_state()

    if alert:
        log.warning("[种子卫兵告警] %s —— 本中转的 X-Codely-Signature 将被安全门"
                    "整体拒绝（参考 <账号ID> 事件），请立即逆向新种子更新 "
                    "keystore.SIGNING_ROOT_KEY！", detail)
    else:
        log.info("[种子卫兵] 核对完成：本地=%s（%s），npm=%s",
                 "找到bundle" if local["found"] else "未找到bundle",
                 "特征齐全" if local["ok"] else "未核对/不适用",
                 online["version"] or "未知")
    return _state.copy()


def state() -> dict:
    """给 /api/seed-alert 的快照。"""
    with _lock:
        return json.loads(json.dumps(_state, ensure_ascii=False))


def start_background():
    """启动后台卫兵线程：启动 20s 后先查一轮，之后每小时一查。防重复启动。"""
    global _started
    if _started:
        return
    _started = True
    _load_state()

    def _loop():
        time.sleep(20)
        while True:
            try:
                run_check()
            except Exception:
                log.exception("[种子卫兵] 检查异常（下一轮继续）")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=_loop, name="seedguard", daemon=True)
    t.start()
    log.info("[种子卫兵] 后台线程已启动（20 秒后首查，之后每小时一查）")
