# -*- coding: utf-8 -*-
"""认证模块：密码哈希、JWT token 与 FastAPI 中间件。

登录默认关闭，只有环境变量 ``QWENPAW_AUTH_ENABLED`` 为真值
（``true``、``1``、``yes``）时才启用。账号通过网页注册流程创建，
而不是从环境变量读取明文密码，避免进程中的 agent 读取密码。

认证数据支持多用户。旧版单用户 ``auth.json`` 第一次读取时会迁移为
管理员账号。

只使用 Python 标准库（hashlib、hmac、secrets），不额外增加依赖。
密码以带随机盐的 SHA-256 摘要形式保存到 ``SECRET_DIR`` 下的
``auth.json``。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..constant import SECRET_DIR, EnvVarLoader
from ..security.secret_store import (
    AUTH_SECRET_FIELDS,
    decrypt_dict_fields,
    encrypt_dict_fields,
    is_encrypted,
)

logger = logging.getLogger(__name__)

AUTH_FILE = SECRET_DIR / "auth.json" #文件路径
#这是一个零依赖的设计决策，只用标准库。所以数据存储就是一个 JSON 文件。
MAX_AVATAR_LENGTH = 2_000_000
MIN_PASSWORD_LENGTH = 8
USER_ROLES = frozenset({"admin", "user"}) #校验角色合法性
USER_STATUSES = frozenset({"active", "disabled"}) #校验状态合法性

# token 默认有效期：7 天
TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600

# token 最大有效期：100 年，用于“永久 token”
TOKEN_EXPIRY_MAX = 100 * 365 * 24 * 3600

# 不需要认证的完整路径
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/status",
        "/api/auth/register",
        "/api/version",
        "/api/settings/language",
        "/api/settings/upload-limit",
        "/api/frontend_plugin",
    },
)

# 不需要认证的路径前缀，主要用于静态资源
# /api/frontend_plugin/ 只注册只读 GET 接口，包含列表与静态文件读取。
# 写操作仍在需要认证的 /api/plugins/ 下。
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/assets/",
    "/logo.png",
    "/qwenpaw-symbol.svg",
    "/api/frontend_plugin/",
)


# ---------------------------------------------------------------------------
# 工具函数：复用 envs/store.py 中 SECRET_DIR 的处理方式
# ---------------------------------------------------------------------------

#"文件权限怎么控制？" → _chmod_best_effort + _prepare_secret_parent
# auth.json 里存着密码哈希和 JWT 密钥。如果权限是 644（所有人可读）
# 任何登录到服务器的用户都能 cat auth.json 拿到哈希去离线暴力破解。
#目录设 0o700：只有文件 owner 能进入目录
#文件设 0o600（在 _save_auth_data 里）：只有 owner 能读写
#为什么叫 "best_effort"？ 因为在 Windows 上 os.chmod 基本无效，在 Docker 里可能以 root 运行。
# 开发者选择"尽力设权限，设不了就算了"，而不是直接崩溃。
# 这是一个可用性优先于安全性的权衡——程序不能因为权限设不上就启动失败。
#设文件权限 0o600
def _chmod_best_effort(path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass

#创建目录+设权限
def _prepare_secret_parent(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_best_effort(path.parent, 0o700)


# ---------------------------------------------------------------------------
# 密码哈希：带随机盐的 SHA-256，不依赖第三方库
# ---------------------------------------------------------------------------


def _hash_password(
    password: str,
    salt: Optional[str] = None,
) -> tuple[str, str]:
    """使用随机盐计算密码摘要，返回 ``(hash_hex, salt_hex)``。"""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return h, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """用保存的摘要和随机盐验证输入密码。"""
    h, _ = _hash_password(password, salt)
    return hmac.compare_digest(h, stored_hash)


# ---------------------------------------------------------------------------
# token 创建与验证：使用 HMAC-SHA256，不需要 PyJWT
# ---------------------------------------------------------------------------


def _get_jwt_secret() -> str:
    """获取 JWT 签名密钥；不存在时创建并保存。"""
    data = _load_auth_data()
    secret = data.get("jwt_secret", "")
    if not secret:
        secret = secrets.token_hex(32)
        data["jwt_secret"] = secret
        _save_auth_data(data)
    return secret


def create_token(username: str, expiry_seconds: Optional[int] = None) -> str:
    """创建 HMAC 签名的 token，格式为 ``base64(payload).signature``。

    ``username`` 会写入 token。``expiry_seconds`` 是自定义有效期秒数；
    传入 -1 或 0 表示永久 token，未传时默认 7 天。
    """
    import base64

    if expiry_seconds is None:
        expiry_seconds = TOKEN_EXPIRY_SECONDS
    elif expiry_seconds <= 0:
        # 永久 token 实际按 100 年计算，避免无过期时间的特殊分支
        expiry_seconds = TOKEN_EXPIRY_MAX
    else:
        # 自定义有效期不能超过上限
        expiry_seconds = min(expiry_seconds, TOKEN_EXPIRY_MAX)

    secret = _get_jwt_secret()
    # 每个 token 都有独立 jti，退出登录时可只撤销这一张 token
    token_id = secrets.token_hex(16)
    payload = json.dumps(
        {
            "sub": username,
            "exp": int(time.time()) + expiry_seconds,
            "iat": int(time.time()),
            "jti": token_id,  # 用于单个 token 撤销的唯一编号
        },
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_token(token: str) -> Optional[str]:
    """验证 token；有效时返回用户名，无效时返回 ``None``。

    同时检查 token 是否已进入撤销列表。
    """
    import base64

    try:
        parts = token.split(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        secret = _get_jwt_secret()
        expected_sig = hmac.new(
            secret.encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None

        # 签名和有效期通过后，还要检查这张 token 是否已经被退出登录撤销
        jti = payload.get("jti")
        if jti and _is_token_revoked(jti):
            return None

        return payload.get("sub")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.debug("Token verification failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 认证数据读写：auth.json 保存在 SECRET_DIR 下
# ---------------------------------------------------------------------------

#是_ensure_user_schema的唯一调用者
def _load_auth_data() -> dict:
    """从 ``SECRET_DIR`` 读取 ``auth.json``。

    文件存在但无法读取或解析时，返回带 ``_auth_load_error`` 的标记，
    让调用方拒绝认证请求，而不是悄悄绕过认证。

    ``jwt_secret`` 等敏感字段会自动解密；旧的明文值会触发重新加密。
    """
    if AUTH_FILE.is_file(): #文件存在时才读取
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            needs_rewrite = any(
                isinstance(data.get(field), str)
                and data.get(field)
                and not is_encrypted(data[field]) #判断是否需要重新加密
                for field in AUTH_SECRET_FIELDS
            )
            data = decrypt_dict_fields(data, AUTH_SECRET_FIELDS) #解密敏感字段
            if _ensure_user_schema(data):
                try:
                    _save_auth_data(data) #迁移后回写
                except Exception as migration_err:
                    logger.debug(
                        "Deferred auth user schema migration: %s",
                        migration_err,
                    )
            if needs_rewrite:
                try:
                    _save_auth_data(data) #迁移后回写
                except Exception as enc_err:
                    logger.debug(
                        "Deferred plaintext→encrypted migration for"
                        " auth.json: %s",
                        enc_err,
                    )
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load auth file %s: %s", AUTH_FILE, exc)
            return {"_auth_load_error": True}
    return {}


def _save_auth_data(data: dict) -> None:
    """以严格权限把认证数据保存到 ``SECRET_DIR`` 下的 ``auth.json``。

    写入前会加密 ``jwt_secret`` 等敏感字段。
    """
    _prepare_secret_parent(AUTH_FILE)
    encrypted_data = encrypt_dict_fields(data, AUTH_SECRET_FIELDS)
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(encrypted_data, f, indent=2, ensure_ascii=False)
    _chmod_best_effort(AUTH_FILE, 0o600)

#生成时间戳
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

#生成用户ID
def _new_user_id() -> str:
    return f"user_{secrets.token_hex(8)}"

#这个函数负责把旧版单用户数据转换为新版多用户数据
def _ensure_user_schema(data: dict) -> bool:
    """把旧版认证数据迁移为多用户结构，并补齐用户默认字段。"""
    changed = False
    raw_users = data.get("users")
    users: list[dict] = []

    if isinstance(raw_users, dict):
        for user_id, user in raw_users.items():
            if isinstance(user, dict):
                users.append({"id": str(user.get("id") or user_id), **user})
        changed = True
    elif isinstance(raw_users, list):
        users = [user for user in raw_users if isinstance(user, dict)]
    else: #如果既不是字典也不是列表，
          #就去原来的老数据（data）里找找看有没有一个叫 user 的字段（这是旧版本的存储方式，以前只支持单用户）。
        legacy = data.get("user")
        if isinstance(legacy, dict) and legacy.get("username"):
            users = [
                {
                    "id": "admin",
                    "username": legacy.get("username", ""),
                    "name": legacy.get("name") #用于显示右上角的姓名
                    or legacy.get("username", ""),
                    "avatar": legacy.get("avatar", ""), #用于右上角显示的头像数据
                    "role": "admin",
                    "status": "active",
                    "created_at": legacy.get("created_at") or _now_iso(),
                    "last_login": legacy.get("last_login", ""),
                    "password_hash": legacy.get("password_hash", ""),
                    "password_salt": legacy.get("password_salt", ""),
                },
            ]
            changed = True

    normalized: list[dict] = []
    for user in users:
        record = dict(user)
        record.setdefault("id", _new_user_id())
        record.setdefault("name", record.get("username", ""))
        record.setdefault("avatar", "")
        record.setdefault("role", "user")
        record.setdefault("status", "active")
        record.setdefault("created_at", _now_iso())
        record.setdefault("last_login", "")
        if record["role"] not in USER_ROLES:
            record["role"] = "user"
        if record["status"] not in USER_STATUSES:
            record["status"] = "active"
        normalized.append(record)
        if record != user:
            changed = True

    if normalized != raw_users:
        data["users"] = normalized
        changed = True

    # 保留旧版单用户字段，兼容命令行重置密码和旧安装目录。
    primary = next(
        (user for user in normalized if user.get("role") == "admin"),
        normalized[0] if normalized else None,
    )
    if primary:
        legacy = {
            key: primary.get(key, "")
            for key in (
                "username",
                "password_hash",
                "password_salt",
                "name",
                "avatar",
                "created_at",
                "last_login",
            )
        }
        if data.get("user") != legacy:
            data["user"] = legacy
            changed = True
    elif "user" in data:
        data.pop("user", None)
        changed = True

    return changed


def _users(data: dict) -> list[dict]:
    # 每次取用户列表前先迁移旧数据，保证下面的代码始终处理 users 列表。
    _ensure_user_schema(data)
    return data.setdefault("users", [])


def _find_user(
    data: dict,
    *,
    user_id: str | None = None,
    username: str | None = None,
) -> dict | None:
    # 用户名比较忽略大小写，避免 Alice 和 alice 被当成两个账号。
    normalized_username = username.casefold() if username else None
    for user in _users(data):
        if user_id is not None and user.get("id") == user_id:
            return user
        if (
            normalized_username is not None
            and str(user.get("username", "")).casefold() == normalized_username
        ):
            return user
    return None


def public_user(user: dict) -> dict:
    """返回可给接口使用的用户资料，不包含密码摘要和随机盐。"""
    return {
        "id": user.get("id", ""),
        "username": user.get("username", ""),
        "name": user.get("name") or user.get("username", ""),
        "avatar": user.get("avatar", ""),
        "role": user.get("role", "user"),
        "status": user.get("status", "active"),
        "created_at": user.get("created_at", ""),
        "last_login": user.get("last_login", ""),
    }


def get_user_by_username(username: str) -> dict | None:
    return _find_user(_load_auth_data(), username=username)


def get_user_by_id(user_id: str) -> dict | None:
    return _find_user(_load_auth_data(), user_id=user_id)


def list_public_users() -> list[dict]: #被list_users()调用，它返回所有用户的安全资料，不会返回密码摘要。
    return [public_user(user) for user in _users(_load_auth_data())]


def is_admin_user(username: str) -> bool:
    # 管理员必须同时满足“账号启用”和“角色为 admin”。
    user = get_user_by_username(username)
    return bool(user and user.get("status") == "active" and user.get("role") == "admin")


def _sync_legacy_user(data: dict, users: list[dict]) -> None:
    # 把优先管理员同步回旧 user 字段，供旧命令行功能继续使用。
    primary = next(
        (user for user in users if user.get("role") == "admin"),
        users[0] if users else None,
    )
    if primary:
        data["user"] = {
            key: primary.get(key, "")
            for key in (
                "username",
                "password_hash",
                "password_salt",
                "name",
                "avatar",
                "created_at",
                "last_login",
            )
        }
    else:
        data.pop("user", None)


# ---------------------------------------------------------------------------
# token 撤销：管理已撤销 token 的黑名单
# ---------------------------------------------------------------------------


def _is_token_revoked(jti: str) -> bool:
    """检查 token 的 jti 是否在撤销列表中。

    使用 revoked_tokens_meta 字典进行 O(1) 查询。
    """
    data = _load_auth_data()
    meta = data.get("revoked_tokens_meta", {})
    return jti in meta


def _add_to_revocation_list(jti: str, exp: int) -> None:
    """把 token 的 jti 与过期时间加入撤销列表。

    revoked_tokens_meta 字典用于 O(1) 查询；revoked_tokens 列表只为兼容旧数据，
    不再用于判断成员是否存在。
    """
    data = _load_auth_data()
    if data.get("_auth_load_error"):
        return

    # 旧认证文件中可能没有该字段，第一次撤销时再创建。
    if "revoked_tokens_meta" not in data:
        data["revoked_tokens_meta"] = {}

    # 用字典判断 jti 是否存在，查询复杂度为 O(1)。
    if jti not in data["revoked_tokens_meta"]:
        data["revoked_tokens_meta"][jti] = exp

        # 同时写入旧列表，兼容依赖该字段的旧版本。
        if "revoked_tokens" not in data:
            data["revoked_tokens"] = []
        data["revoked_tokens"].append(jti)

    _save_auth_data(data)


def _clean_expired_revocations() -> None:
    """清理已过期的撤销记录，避免撤销列表持续增长。"""
    data = _load_auth_data()
    if data.get("_auth_load_error"):
        return

    revoked = data.get("revoked_tokens", [])
    meta = data.get("revoked_tokens_meta", {})
    current_time = int(time.time())

    # 只保留仍在有效期内的撤销记录。
    cleaned_revoked = []
    cleaned_meta = {}

    for jti in revoked:
        exp = meta.get(jti, 0)
        if exp > current_time:
            cleaned_revoked.append(jti)
            cleaned_meta[jti] = exp

    if len(cleaned_revoked) < len(revoked):
        data["revoked_tokens"] = cleaned_revoked
        data["revoked_tokens_meta"] = cleaned_meta
        _save_auth_data(data)
        logger.info(
            "Cleaned %d expired tokens from revocation list",
            len(revoked) - len(cleaned_revoked),
        )


def is_auth_enabled() -> bool:
    """根据环境变量判断是否启用认证。

    ``QWENPAW_AUTH_ENABLED`` 为 ``true``、``1``、``yes`` 时返回 ``True``。
    是否已有用户由中间件单独检查，保证第一个用户仍能访问注册接口。
    """
    env_flag = EnvVarLoader.get_str("QWENPAW_AUTH_ENABLED", "").strip().lower()
    return env_flag in ("true", "1", "yes")


def has_registered_users() -> bool:
    """判断系统中是否至少已有一个用户。"""
    return bool(_users(_load_auth_data()))


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register_user(
    username: str,
    password: str,
    expiry_seconds: Optional[int] = None,
    *,
    name: str | None = None,
    avatar: str = "",
    role: str | None = None,
) -> Optional[str]:
    """注册账号；成功时返回 token，用户名已存在时返回 ``None``。"""
    data = _load_auth_data()
    users = _users(data)

    #检查用户名是否重复，检查密码至少 8 位，检查头像长度
    username = username.strip()
    password = password.strip()
    if not username or not password or len(password) < MIN_PASSWORD_LENGTH:
        return None
    if _find_user(data, username=username):
        return None
    if len(avatar) > MAX_AVATAR_LENGTH:
        return None

    assigned_role = (
        role if role in USER_ROLES else ("admin" if not users else "user")
        #如果系统里还没有用户 → 当前注册者是 admin；如果已经有用户 → 当前注册者是 user
    )

    #密码生成 hash 和 salt
    pw_hash, salt = _hash_password(password)
    #创建用户对象
    user = {
        "id": _new_user_id() if users else "admin",
        "username": username, 
        "name": (name or username).strip(), #用于右上角显示的姓名
        "avatar": avatar, #用于右上角显示的头像数据
        "role": assigned_role, #管理员or用户
        "status": "active", #active 或 disabled，决定是否允许登录和访问
        "created_at": _now_iso(), #现在的时间戳
        "last_login": "",
        "password_hash": pw_hash, #密码摘要，不是明文密码
        "password_salt": salt, #参与密码摘要计算的随机盐值
    }
    users.append(user)

    # 所有 token 都用 jwt_secret 签名；首次注册时必须先创建它。
    if not data.get("jwt_secret"):
        data["jwt_secret"] = secrets.token_hex(32)

    _sync_legacy_user(data, users)
    _save_auth_data(data)
    logger.info("User '%s' registered", username)
    return create_token(username, expiry_seconds)


def auto_register_from_env() -> None:
    """根据环境变量自动注册管理员。

    应用启动时调用一次。认证已启用且同时设置
    ``QWENPAW_AUTH_USERNAME``、``QWENPAW_AUTH_PASSWORD`` 时自动创建管理员。
    Docker、Kubernetes、服务器面板等无法交互式网页注册的部署场景会用到它。

    认证未开启、已有用户、环境变量缺失或为空时直接跳过。
    """
    if not is_auth_enabled():
        return
    if has_registered_users():
        return

    username = EnvVarLoader.get_str("QWENPAW_AUTH_USERNAME", "").strip()
    password = EnvVarLoader.get_str("QWENPAW_AUTH_PASSWORD", "").strip()
    if not username or not password:
        return

    token = register_user(username, password)
    if token:
        logger.info(
            "Auto-registered user '%s' from environment variables",
            username,
        )

#被修改个人资料接口调用
def update_credentials(
    current_password: str,
    new_username: Optional[str] = None,
    new_password: Optional[str] = None,
    expiry_seconds: Optional[int] = None,
    *,
    username: str | None = None,
    name: str | None = None,
    avatar: str | None = None,
) -> Optional[str]:
    """修改当前用户自己的资料，并返回新的 token。

    修改前必须验证 current_password。成功后重新签发 token，因为用户名可能已变化；
    密码验证失败时返回 ``None``。
    """
    data = _load_auth_data()
    users = _users(data)
    user = _find_user(data, username=username) if username else None
    if user is None and users:
        user = next(
            (item for item in users if item.get("role") == "admin"),
            users[0],
        )
    if not user:
        return None

    stored_hash = user.get("password_hash", "")
    stored_salt = user.get("password_salt", "")
    if not verify_password(current_password, stored_hash, stored_salt):
        return None

    if new_username and new_username.strip():
        candidate = new_username.strip()
        duplicate = _find_user(data, username=candidate)
        if duplicate and duplicate.get("id") != user.get("id"):
            return None
        user["username"] = candidate

    if new_password:
        if len(new_password.strip()) < MIN_PASSWORD_LENGTH:
            return None
        pw_hash, salt = _hash_password(new_password)
        user["password_hash"] = pw_hash
        user["password_salt"] = salt
        # 修改密码后轮换签名密钥，使旧 token 全部失效。
        data["jwt_secret"] = secrets.token_hex(32)

    if name is not None:
        user["name"] = name.strip() or user.get("username", "")
    if avatar is not None:
        if len(avatar) > MAX_AVATAR_LENGTH:
            return None
        user["avatar"] = avatar

    _sync_legacy_user(data, users)
    _save_auth_data(data)
    logger.info("Credentials updated for user '%s'", user["username"])
    return create_token(user["username"], expiry_seconds)


# ---------------------------------------------------------------------------
# 登录认证
# ---------------------------------------------------------------------------


def authenticate(
    username: str,
    password: str,
    expiry_seconds: Optional[int] = None,
) -> Optional[str]:
    """验证用户名与密码；成功时返回 token。"""
    data = _load_auth_data()
    user = _find_user(data, username=username)
    if not user:
        return None
    if user.get("status") != "active": #禁用账号不能登录
        return None
    stored_hash = user.get("password_hash", "")
    stored_salt = user.get("password_salt", "")
    if (
        stored_hash
        and stored_salt
        and verify_password(password, stored_hash, stored_salt)
    ):
        user["last_login"] = _now_iso()
        _sync_legacy_user(data, _users(data))
        _save_auth_data(data)
        return create_token(username, expiry_seconds)
    return None

#被create_user()调用
def create_managed_user(
    *,
    username: str,
    password: str,
    name: str,
    avatar: str = "",
    role: str = "user",
    status: str = "active",
) -> dict:
    """按管理员提交的数据创建用户，并返回安全用户资料。"""
    data = _load_auth_data()
    users = _users(data)
    username = username.strip()
    password = password.strip()
    if not username or not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("Username and password are required")
    if role not in USER_ROLES:
        raise ValueError("Invalid user role")
    if status not in USER_STATUSES:
        raise ValueError("Invalid user status")
    if len(avatar) > MAX_AVATAR_LENGTH:
        raise ValueError("Avatar is too large")
    if _find_user(data, username=username):
        raise ValueError("Username already exists")

    # 管理员设置的初始密码同样只保存摘要和随机盐。
    pw_hash, salt = _hash_password(password)
    user = {
        "id": _new_user_id(),
        "username": username,
        "name": name.strip() or username,
        "avatar": avatar,
        "role": role,
        "status": status,
        "created_at": _now_iso(),
        "last_login": "",
        "password_hash": pw_hash,
        "password_salt": salt,
    }
    users.append(user)
    _sync_legacy_user(data, users)
    _save_auth_data(data)
    return public_user(user)

#被update_user()调用
def update_managed_user(user_id: str, updates: dict) -> dict | None:
    """执行管理员修改，并返回修改后的安全用户资料。"""
    data = _load_auth_data()
    users = _users(data)
    user = _find_user(data, user_id=user_id)
    if not user:
        return None

    next_username = updates.get("username")
    if next_username is not None:
        next_username = next_username.strip()
        if not next_username:
            raise ValueError("Username cannot be empty")
        duplicate = _find_user(data, username=next_username)
        if duplicate and duplicate.get("id") != user_id:
            raise ValueError("Username already exists")
        user["username"] = next_username

    for key in ("name", "avatar", "role", "status"):
        if key not in updates or updates[key] is None:
            continue
        value = updates[key]
        if key == "role" and value not in USER_ROLES:
            raise ValueError("Invalid user role")
        if key == "status" and value not in USER_STATUSES:
            raise ValueError("Invalid user status")
        if key == "avatar" and len(value) > MAX_AVATAR_LENGTH:
            raise ValueError("Avatar is too large")
        if key == "name" and isinstance(value, str):
            user[key] = value.strip() or user.get("username", "")
        else:
            user[key] = value.strip() if isinstance(value, str) else value

    password = updates.get("password")
    if password:
        if len(password.strip()) < MIN_PASSWORD_LENGTH:
            raise ValueError("Password must be at least 8 characters")
        pw_hash, salt = _hash_password(password)
        user["password_hash"] = pw_hash
        user["password_salt"] = salt
        # 管理员重置密码后，旧 token 全部失效，避免旧会话继续使用。
        data["jwt_secret"] = secrets.token_hex(32)

    _sync_legacy_user(data, users)
    _save_auth_data(data)
    return public_user(user)

#被delete_user()调用
def delete_managed_user(user_id: str, current_username: str) -> bool:
    """删除用户，但不能删除当前用户或最后一个管理员。"""
    data = _load_auth_data()
    users = _users(data)
    user = _find_user(data, user_id=user_id)
    if not user:
        return False
    if user.get("username") == current_username: #当前管理员不能删除自己
        raise ValueError("Cannot delete the current user")
    if user.get("role") == "admin": #删除管理员前必须确认系统还会保留其他管理员
        admin_count = sum(item.get("role") == "admin" for item in users)
        if admin_count <= 1:
            raise ValueError("Cannot delete the last administrator")
    users.remove(user)
    _sync_legacy_user(data, users)
    _save_auth_data(data)
    return True

#token 内部有唯一编号 jti。退出登录时，后端把这个 jti 放进已撤销列表。
#之后再有人拿这个 token 请求接口：验证签名通过 -> 检查 jti 是否在撤销列表 -> 在 → token 无效，返回 401
def revoke_token(token: str) -> bool:
    """把单个 token 的 jti 加入黑名单，实现退出登录。

    成功返回 ``True``，失败返回 ``False``。
    """
    import base64

    try:
        # 从 token 中取出唯一编号和过期时间，撤销记录只需保留到过期为止。
        parts = token.split(".", 1)
        if len(parts) != 2:
            return False

        payload_b64 = parts[0]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        jti = payload.get("jti")
        exp = payload.get("exp", 0)

        if not jti:
            logger.warning("Token has no jti, cannot revoke individually")
            return False

        _add_to_revocation_list(jti, exp)
        logger.info("Token %s revoked", jti[:8])

        # 每次撤销时顺便清理已过期的撤销记录。
        _clean_expired_revocations()

        return True
    except Exception as exc:
        logger.error("Failed to revoke token: %s", exc)
        return False


def revoke_all_tokens() -> bool:
    """通过轮换 JWT 签名密钥撤销所有 token。

    调用前签发的 token 会全部失效，同时清空不再需要的撤销列表。
    成功返回 ``True``，失败返回 ``False``。
    """
    try:
        data = _load_auth_data()
        if data.get("_auth_load_error"):
            return False

        # 旧 token 的签名密钥不再匹配，因此会全部失效。
        data["jwt_secret"] = secrets.token_hex(32)

        # 所有旧 token 已失效，无需再保留单独撤销记录。
        data["revoked_tokens"] = []
        data["revoked_tokens_meta"] = {}

        _save_auth_data(data)
        logger.info("All tokens revoked (JWT secret rotated)")
        return True
    except Exception as exc:
        logger.error("Failed to revoke tokens: %s", exc)
        return False


# ---------------------------------------------------------------------------
# FastAPI 中间件：解析客户端 IP，并验证可信代理
# ---------------------------------------------------------------------------

_LOOPBACK = frozenset({"127.0.0.1", "::1"})
_BRACKETED = re.compile(r"^\[([^\]]+)\](?::\d+)?$")
_V4_PORT = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d+$")

_MAX_WARN_IPS = 1024
_warned_untrusted_ips: set[str] = set()


def _normalize_ip(raw: str) -> str | None:
    """移除括号、端口、zone-id 后验证 IP；失败时返回 ``None``。"""
    if not raw:
        return None
    s = raw.strip()
    m = _BRACKETED.match(s) or _V4_PORT.match(s)
    if m:
        s = m.group(1)
    if "%" in s:
        s = s.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def _parse_networks(entries: list[str]) -> list:
    """把 CIDR/IP 字符串解析为网络对象。"""
    nets = []
    for entry in entries:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return nets


def _ip_in_networks(ip_str: str, networks: list) -> bool:
    """检查规范化后的 IP 是否属于任一网络。"""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in networks:
        if addr.version == net.version and addr in net:
            return True
    return False


# 缓存认证高频路径需要的配置，避免每个请求都读取磁盘
_auth_config_cache: tuple = (0, None, [])


def _get_config_cached():
    """使用文件修改时间缓存，返回 ``(config, trusted_networks)``。"""
    global _auth_config_cache  # noqa: PLW0603
    from ..config import load_config
    from ..config.utils import get_config_path

    config_path = get_config_path()
    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    if mtime_ns != _auth_config_cache[0] or _auth_config_cache[1] is None:
        cfg = load_config()
        nets = _parse_networks(cfg.security.trusted_proxies)
        _auth_config_cache = (mtime_ns, cfg, nets)
    return _auth_config_cache[1], _auth_config_cache[2]


def _resolve_client_ip(request: Request) -> str:
    """返回真实客户端 IP。

    只有直连节点属于配置的可信代理网络时，才信任 X-Forwarded-For；
    XFF 从右向左解析并跳过可信代理 IP。
    """
    direct_raw = request.client.host if request.client else ""
    direct_ip = _normalize_ip(direct_raw) or direct_raw

    _cfg, networks = _get_config_cached()
    if not networks or not _ip_in_networks(direct_ip, networks):
        # 每个不可信来源只记录一次，避免日志被大量请求淹没。
        has_proxy_hdr = request.headers.get(
            "x-forwarded-for",
        ) or request.headers.get("x-real-ip")
        if (
            has_proxy_hdr
            and direct_ip not in _warned_untrusted_ips
            and len(_warned_untrusted_ips) < _MAX_WARN_IPS
        ):
            _warned_untrusted_ips.add(direct_ip)
            logger.warning(
                "Ignoring proxy headers from untrusted source"
                " %s (add to security.trusted_proxies if"
                " legitimate)",
                direct_ip,
            )
        return direct_ip

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        for token in reversed(xff.split(",")):
            norm = _normalize_ip(token)
            if norm is None:
                break
            if not _ip_in_networks(norm, networks):
                return norm

    real_ip = _normalize_ip(
        request.headers.get("x-real-ip", ""),
    )
    return real_ip or direct_ip


resolve_client_ip = _resolve_client_ip


class AuthMiddleware(BaseHTTPMiddleware):
    """为受保护路由检查 Bearer token 的中间件。"""

    async def dispatch(self, request: Request, call_next):
        if self._should_skip_auth(request):
            return await call_next(request)

        token = self._extract_token(request)
        if not token:
            return Response(
                content='{"detail":"Not authenticated"}',
                status_code=401,
                media_type="application/json",
            )

        user = verify_token(token)
        if user is None:
            return Response(
                content='{"detail":"Invalid or expired token"}',
                status_code=401,
                media_type="application/json",
            )

        # token 的有效期可能长于账号状态变化，所以每次都重新读取账号状态。
        # 这样管理员禁用账号后，旧 token 也会立刻失效。
        #token 本身有效，不代表用户一定还能使用系统
        user_record = get_user_by_username(user) 
        if not user_record or user_record.get("status") != "active":
            return Response(
                content='{"detail":"Account is disabled or unavailable"}',
                status_code=401,
                media_type="application/json",
            )

        request.state.user = user
        return await call_next(request)

    @staticmethod
    def _should_skip_auth(  # pylint: disable=too-many-return-statements
        request: Request,
    ) -> bool:
        if not is_auth_enabled() or not has_registered_users():
            return True

        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in _PUBLIC_PATHS
            or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
            or not path.startswith("/api/")
        ):
            return True

        cfg, _ = _get_config_cached()
        allowed = cfg.security.allow_no_auth_hosts
        client_ip = resolve_client_ip(request)
        norm = _normalize_ip(client_ip) or client_ip
        if norm not in allowed:
            return False

        # 多一层保护：回环地址白名单还要求直连 TCP 节点也是回环地址。
        if norm in _LOOPBACK:
            peer = _normalize_ip(
                request.client.host if request.client else "",
            )
            if peer not in _LOOPBACK:
                logger.warning(
                    "Auth skip blocked: client_ip=%s but"
                    " direct peer %s is not loopback",
                    norm,
                    peer,
                )
                return False
        return True

    @staticmethod
    def _extract_token(request: Request) -> Optional[str]:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        conn = request.headers.get("connection", "")
        if "upgrade" in conn.lower():
            return request.query_params.get("token")
        return request.query_params.get("token") or None


def check_proxy_config_sanity() -> None:
    """启动时检查代理配置是否可疑，必要时写入警告日志。"""
    try:
        cfg, _ = _get_config_cached()
    except (OSError, ValueError):
        return
    sec = cfg.security
    has_non_loopback = any(h not in _LOOPBACK for h in sec.allow_no_auth_hosts)
    if has_non_loopback and not sec.trusted_proxies:
        logger.warning(
            "allow_no_auth_hosts contains non-loopback entries"
            " but trusted_proxies is empty. If behind a reverse"
            " proxy, add proxy IPs to"
            " security.trusted_proxies.",
        )
