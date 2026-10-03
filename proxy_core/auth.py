"""管理密码哈希与鉴权装饰器。"""

import hashlib
import hmac
from functools import wraps

from flask import jsonify, request

from .settings import load_config


def sha256(s: str) -> str:
    """SHA256 哈希（旧格式，兼容读）。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_password(pwd: str) -> str:
    """加盐哈希（新格式 salt$hash）。"""
    if not pwd:
        return ""
    salt = hmac.new(__import__("os").urandom(16), b"", hashlib.sha256).hexdigest()[:16]
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 60000).hex()
    return f"{salt}${h}"


def verify_password(pwd: str, stored: str) -> bool:
    """校验密码。兼容旧无盐格式（64 hex）与新格式（salt$hash）。"""
    if not stored:
        return True  # 未设置密码 = 不鉴权
    if "$" in stored:
        salt, h = stored.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 60000).hex()
        return hmac.compare_digest(calc, h)
    return hmac.compare_digest(sha256(pwd), stored)


def session_token(pwd_hash: str) -> str:
    """由密码哈希派生的会话令牌：不泄露密码本身，改密码即全部失效。"""
    return hmac.new(pwd_hash.encode("utf-8"), b"qidi-relay-session-v1", hashlib.sha256).hexdigest()


def is_authorized() -> bool:
    """未设置密码视为已授权；否则校验 admin_pwd cookie。

    cookie 值为 HMAC 会话令牌（新）；兼容旧明文密码 cookie（verify_password
    或直接比对，用于升级期间老会话平滑过渡）。
    """
    cfg = load_config()
    pwd_hash = cfg.get("admin_password")
    if not pwd_hash:
        return True
    cookie_pwd = request.cookies.get("admin_pwd")
    if not cookie_pwd:
        return False
    if hmac.compare_digest(cookie_pwd, session_token(pwd_hash)):
        return True
    # 旧 cookie 兼容：存的是明文密码或旧哈希
    return bool(verify_password(cookie_pwd, pwd_hash) or
                hmac.compare_digest(cookie_pwd, pwd_hash))


def check_admin_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        cfg = load_config()
        pwd_hash = cfg.get("admin_password")
        if not pwd_hash:
            return f(*args, **kwargs)  # 未设置密码，不鉴权
        ip = request.remote_addr
        allowed = cfg.get("allowed_ips", ["*"])
        if "*" not in allowed and ip not in allowed:
            return jsonify({"error": "IP 未授权"}), 403
        if not is_authorized():
            return jsonify({"error": "密码错误"}), 401
        return f(*args, **kwargs)

    return wrapper
