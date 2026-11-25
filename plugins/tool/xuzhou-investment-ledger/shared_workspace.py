"""徐州招商团队共享台账目录与成员名册授权。

台账从“每个智能体工作区一份”改为“全团队共享一份”：所有被授权的登录用户经本模块
解析到同一个服务器共享根目录，线索、证据、快照、锁与 HMAC 密钥都随该目录走，审计仍
按 ``principal.user_id`` 逐人记录。成员名册 ``team_members.json`` 保存在共享根目录而
不是台账 JSON 内部，避免“查权限先要读台账”的循环依赖；名册只记录成员与追加式授权
审计，不保存线索、证据、来源 URL、评分或任何业务事实。本模块不联网、不读取网页、
不修改台账业务数据，也不向客户端暴露可绕过授权的路径参数。
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status

from qwenpaw.app.principal import Principal, get_current_principal
from qwenpaw.app.agent_context import get_current_user_id
from qwenpaw.app.auth import get_user_by_id
from qwenpaw.constant import WORKING_DIR

from .storage import _ensure_private_workspace_directory


# 共享根目录只能由服务器常量或部署环境变量决定，绝不由请求、模型或前端传入；
# 环境变量仅供测试隔离和部署迁移使用，运行时业务代码不接受任何目录参数。
SHARED_LEDGER_DIR_ENV = "XUZHOU_INVESTMENT_LEDGER_SHARED_DIR"
SHARED_LEDGER_DIR_NAME = "xuzhou-investment-ledger"
TEAM_ROSTER_FILE_NAME = "team_members.json"
TEAM_ROSTER_LOCK_NAME = "team_members.lock"
TEAM_ROSTER_OPERATION_SCOPE = "admin_team_roster_management_only"
_USER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}")


def _utc_now() -> str:
    """返回名册授权审计统一使用的 UTC 时间，不接受客户端传入时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """为名册审计事件生成稳定短 ID，客户端不能指定或覆盖历史审计标识。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: object) -> str:
    """稳定序列化名册记录，供保存前后逐字比对，防止授权操作夹带其他改动。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_user_id(value: object, field_name: str) -> str:
    """校验平台用户 ID 的长度与字符集，拒绝空白、超长或含特殊字符的身份。

    输入只能来自服务端认证主体或平台用户目录，输出为原样字符串。平台用户 ID 形如
    ``admin`` 或 ``user_<hex>``；本函数不查询用户目录、不判断角色，也不接受请求体
    直接声明的身份之外任何隐含来源。
    """
    if not isinstance(value, str) or not _USER_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name}必须是1到100个字母、数字、下划线或连字符")
    return value


def _validate_username(value: object) -> str:
    """校验平台登录用户名，拒绝空白、超长或含控制字符的输入。

    输入是管理员经接口提交的用户名，输出为去空白后的原文。用户名可含中文等平台允许
    的字符，但控制字符会破坏名册 JSON 的逐字比对与日志可读性，因此明确拒绝；本函数
    不检查用户是否真实存在，存在性只能由服务端平台用户目录确认。
    """
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError("username不能为空")
    if len(normalized) > 100:
        raise ValueError("username不能超过100个字符")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in normalized):
        raise ValueError("username不能包含控制字符")
    return normalized


def shared_ledger_root() -> Path:
    """返回全团队共享的台账根目录，并按 0700 权限与符号链接防护创建。

    默认位置是平台 ``WORKING_DIR`` 下的 ``shared/xuzhou-investment-ledger``；环境变量
    ``XUZHOU_INVESTMENT_LEDGER_SHARED_DIR`` 可覆盖为其他绝对路径，仅供测试隔离与部署
    迁移。目录名固定，函数逐层复用 ``storage`` 的私有目录范式：拒绝符号链接与普通文件、
    新建目录即设为 0700。函数不接受请求、模型或前端提供的任何路径片段，也不读取、
    修改或删除台账、快照与名册内容。
    """
    override = os.environ.get(SHARED_LEDGER_DIR_ENV, "").strip()
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            raise ValueError("共享台账目录覆盖值必须是绝对路径")
    else:
        root = WORKING_DIR / "shared" / SHARED_LEDGER_DIR_NAME
    return _ensure_private_workspace_directory(root.parent, Path(root.name))


def _team_roster_path(root: Path) -> Path:
    """返回共享根目录内固定名册文件路径，文件名不接受任何调用方输入。

    输入是已由本模块防护创建的共享根目录，输出为 ``team_members.json`` 的固定路径。
    名册与台账 JSON 分文件保存，授权判断不需要读取业务证据链；函数不读写文件内容。
    """
    return root / TEAM_ROSTER_FILE_NAME


def _empty_team_roster() -> dict[str, Any]:
    """返回不含任何成员和审计的空名册结构，仅在名册文件不存在时使用。"""
    return {"schema_version": 1, "members": [], "audit": []}


def _validate_member_record(member: object) -> dict[str, str]:
    """严格校验一条名册成员记录，结构异常时失败关闭而不是猜测放行。

    输入是名册文件中的单个元素，输出为原记录。服务器只接受本模块写入的四字段结构；
    任何多余、缺失或类型异常的字段都说明文件被外部改写，调用方必须拒绝继续授权。
    """
    if not isinstance(member, dict) or set(member) != {
        "user_id",
        "username",
        "added_by_user_id",
        "added_at",
    }:
        raise ValueError("招商团队名册成员记录格式异常")
    _validate_user_id(member.get("user_id"), "名册成员user_id")
    _validate_username(member.get("username"))
    _validate_user_id(member.get("added_by_user_id"), "名册added_by_user_id")
    if not isinstance(member.get("added_at"), str) or not member["added_at"]:
        raise ValueError("招商团队名册成员记录缺少有效 added_at")
    return member


def _validate_audit_record(audit: object) -> dict[str, Any]:
    """严格校验一条名册授权审计，防止伪造或夹带的审计进入授权判断。"""
    if not isinstance(audit, dict):
        raise ValueError("招商团队名册审计记录格式异常")
    required_keys = {
        "audit_id",
        "action",
        "target_user_id",
        "target_username",
        "operated_by_user_id",
        "operated_at",
        "operation_scope",
    }
    if not required_keys.issubset(audit.keys()):
        raise ValueError("招商团队名册审计记录缺少必要字段")
    if audit.get("action") not in {"add", "remove"}:
        raise ValueError("招商团队名册审计动作不受支持")
    _validate_user_id(audit.get("target_user_id"), "审计target_user_id")
    _validate_username(audit.get("target_username"))
    _validate_user_id(audit.get("operated_by_user_id"), "审计operated_by_user_id")
    return audit


def _load_team_roster(root: Path) -> dict[str, Any]:
    """读取并严格校验共享根目录中的成员名册，文件不存在时返回空名册。

    输入是共享根目录，输出为满足固定结构的名册。文件为符号链接、JSON 损坏、根节点
    不是对象、members/audit 不是列表或任一记录异常时都明确报错，绝不会把异常内容当作
    “全员放行”或“空名册”继续授权；本函数不创建文件、不修改任何业务台账数据。
    """
    path = _team_roster_path(root)
    if path.is_symlink():
        raise ValueError("招商团队名册文件不能是符号链接")
    if not path.exists():
        return _empty_team_roster()
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError("招商团队名册不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("招商团队名册根节点必须是对象")
    members = payload.get("members")
    audit = payload.get("audit")
    if not isinstance(members, list) or not isinstance(audit, list):
        raise ValueError("招商团队名册中的 members 和 audit 必须是列表")
    return {
        "schema_version": 1,
        "members": [_validate_member_record(member) for member in members],
        "audit": [_validate_audit_record(record) for record in audit],
    }


def _save_team_roster(root: Path, roster: dict[str, Any]) -> Path:
    """以临时文件加原子替换保存名册，权限 0600，拒绝符号链接与半截写入。

    输入是共享根目录和已完成单次授权变更的名册，输出为固定名册路径。保存只发生在
    排他锁内；函数不接受客户端提供的文件名或内容，也不会读取或修改台账业务数据。
    """
    path = _team_roster_path(root)
    if path.is_symlink():
        raise ValueError("招商团队名册文件不能是符号链接")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=root,
        prefix=".team_members_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(roster, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


@contextmanager
def _exclusive_roster_lock(root: Path):
    """在名册“读取-变更-保存”的临界区持有排他文件锁，防止并发授权互相覆盖。

    输入是共享根目录，输出为 ``with`` 范围内有效的固定锁。锁文件与名册同目录、权限
    0600，文件名不接受调用方输入；锁只协调保存顺序，不承载业务数据，也不替代管理员
    角色校验。进程异常退出后操作系统会释放 ``flock``，无需清理“陈旧锁”。
    """
    lock_path = root / TEAM_ROSTER_LOCK_NAME
    if lock_path.is_symlink():
        raise ValueError("招商团队名册锁文件不能是符号链接")
    lock_flags = os.O_RDWR | os.O_CREAT
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        lock_flags |= nofollow
    descriptor = os.open(lock_path, lock_flags, 0o600)
    lock_file = None
    try:
        os.chmod(lock_path, 0o600)
        lock_file = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if lock_file is not None:
            lock_file.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _append_roster_audit(
    roster: dict[str, Any],
    *,
    action: str,
    target_user_id: str,
    target_username: str,
    operated_by_user_id: str,
) -> dict[str, object]:
    """向名册追加一条授权审计，并逐字确认既有审计未被改写。

    输入是受控动作、目标用户与服务器认证管理员 ID，输出为服务器生成的审计记录。
    函数不接受客户端提交的时间、审计 ID 或历史审计内容；追加前后比对既有审计快照，
    保证授权管理不能成为静默删改名册历史的旁路。
    """
    audit_records = roster["audit"]
    audit_before = [_stable_json(record) for record in audit_records]
    operated_at = _utc_now()
    audit = {
        "audit_id": _stable_id(
            "team-roster-audit",
            {
                "action": action,
                "target_user_id": target_user_id,
                "operated_by_user_id": operated_by_user_id,
                "operated_at": operated_at,
            },
        ),
        "action": action,
        "target_user_id": target_user_id,
        "target_username": target_username,
        "operated_by_user_id": operated_by_user_id,
        "operated_at": operated_at,
        "operation_scope": TEAM_ROSTER_OPERATION_SCOPE,
    }
    audit_records.append(audit)
    if [_stable_json(record) for record in audit_records[: len(audit_before)]] != audit_before:
        raise RuntimeError("名册授权审计只能追加，不能改写历史记录")
    return audit


def is_team_allowed(principal: Principal) -> bool:
    """判断认证主体是否可访问共享台账：admin 恒放行，否则按名册精确匹配。

    输入只能是服务端认证中间件写入的 ``Principal``，输出为布尔授权结论。管理员由平台
    角色决定、不依赖名册，因此名册文件不存在或为空时仅管理员可用；普通用户只在名册
    ``members`` 中存在相同 ``user_id`` 记录时放行。名册损坏或异常时底层校验会明确报错，
    本函数不会把异常状态解释为放行；函数不修改名册、台账或任何业务数据。
    """
    if principal.is_admin:
        return True
    root = shared_ledger_root()
    if not _team_roster_path(root).exists():
        return False
    roster = _load_team_roster(root)
    return any(member["user_id"] == principal.user_id for member in roster["members"])


def shared_ledger_dir_for_request(request: Request) -> Path:
    """业务路由统一入口：认证主体必须是管理员或名册成员，否则拒绝并返回 403。

    输入是已认证 HTTP 请求，输出为团队共享台账根目录，供全部业务核心函数作为
    ``workspace_dir`` 使用。身份只从认证中间件写入的 ``Principal`` 读取，请求头、请求体、
    模型或前端都不能自称成员或指定其他目录；非成员访问只得到拒绝结果，不会创建名册、
    读取台账内容或泄露共享目录中的任何业务数据。
    """
    principal = get_current_principal(request)
    if not is_team_allowed(principal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅徐州招商团队成员或管理员可以访问团队共享台账",
        )
    return shared_ledger_root()


def shared_ledger_dir_for_current_execution() -> Path:
    """按平台执行上下文的可信用户身份授权工具或 Cron 访问团队共享台账。

    输入为空：普通对话的 ``current_user_id`` 由请求生命周期在认证后写入，Cron 的同一值由
    平台从已登记的固定投递目标写入；函数绝不读取模型参数、前端字段、工具参数或工作区
    名称。输出为固定共享根目录。它会再从平台用户目录读取账号的当前状态和角色，并按
    ``is_team_allowed`` 核验管理员或成员名册；上下文缺失、账号被禁用/删除、角色异常或
    成员已被移出时一律失败关闭。由此后台任务在成员撤销后不能继续读写线索、证据、快照、
    评分或复核，且工具无法通过自称 user_id 绕过名册。本函数只做授权和目录解析，不修改
    名册、业务台账或原始证据链。
    """
    execution_user_id = get_current_user_id()
    normalized_user_id = _validate_user_id(execution_user_id, "当前执行用户ID")
    user = get_user_by_id(normalized_user_id)
    if not isinstance(user, dict) or user.get("status") != "active":
        raise PermissionError("当前执行用户不存在或已被停用，拒绝访问招商团队共享台账")
    if user.get("id") != normalized_user_id:
        raise PermissionError("当前执行用户目录记录与服务器上下文不一致，拒绝访问共享台账")
    username = user.get("username")
    role = user.get("role")
    if not isinstance(username, str) or not username.strip() or role not in {"admin", "user"}:
        raise PermissionError("当前执行用户记录不完整，拒绝访问招商团队共享台账")
    principal = Principal(
        user_id=normalized_user_id,
        username=username,
        role=role,
    )
    if not is_team_allowed(principal):
        raise PermissionError("当前执行用户不是徐州招商团队成员，拒绝访问共享台账")
    return shared_ledger_root()


def list_team_members() -> dict[str, object]:
    """返回名册成员与授权审计的副本，供管理员查看，不修改任何数据。

    输出为深拷贝的成员列表和追加式审计列表；名册文件不存在时返回空名册。函数不返回
    台账业务数据，也不接受筛选、分页或身份参数——调用路由必须先完成管理员校验。
    """
    roster = _load_team_roster(shared_ledger_root())
    return {
        "members": copy.deepcopy(roster["members"]),
        "audit": copy.deepcopy(roster["audit"]),
    }


def add_team_member(
    *,
    user_id: str,
    username: str,
    added_by_user_id: str,
) -> dict[str, object]:
    """由管理员把一名平台用户加入招商团队名册，并追加不可覆盖的授权审计。

    输入是服务端平台用户目录解析出的用户 ID 与用户名、以及认证管理员 ID；输出为新成员
    记录和审计。客户端不能指定成员 ID、加入时间或操作者；同一 user_id 或用户名已在名册
    时明确拒绝。保存在排他锁内完成，保存前后逐字比对既有成员与审计，确保授权变更不会
    夹带对其他成员、历史审计或台账业务数据的改动。
    """
    normalized_user_id = _validate_user_id(user_id, "user_id")
    normalized_username = _validate_username(username)
    normalized_admin_user_id = _validate_user_id(added_by_user_id, "added_by_user_id")
    root = shared_ledger_root()
    with _exclusive_roster_lock(root):
        roster = _load_team_roster(root)
        members = roster["members"]
        if any(member["user_id"] == normalized_user_id for member in members):
            raise ValueError("该用户已在招商团队名册中")
        if any(
            str(member["username"]).casefold() == normalized_username.casefold()
            for member in members
        ):
            raise ValueError("该用户名已在招商团队名册中")
        members_before = [_stable_json(member) for member in members]
        member = {
            "user_id": normalized_user_id,
            "username": normalized_username,
            "added_by_user_id": normalized_admin_user_id,
            "added_at": _utc_now(),
        }
        members.append(member)
        if [_stable_json(record) for record in members[: len(members_before)]] != members_before:
            raise RuntimeError("新增名册成员不得改写既有成员记录")
        audit = _append_roster_audit(
            roster,
            action="add",
            target_user_id=normalized_user_id,
            target_username=normalized_username,
            operated_by_user_id=normalized_admin_user_id,
        )
        _save_team_roster(root, roster)
    return {"member": dict(member), "audit": audit}


def remove_team_member(
    *,
    username: str,
    removed_by_user_id: str,
) -> dict[str, object]:
    """由管理员按用户名把一名成员移出名册，并追加不可覆盖的授权审计。

    输入是待移除的平台用户名和认证管理员 ID；输出为被移除的成员记录和审计。用户名
    大小写不敏感地与平台账号规则保持一致；名册中不存在该成员时明确拒绝，不猜测、不
    按 user_id 模糊匹配。移除只影响目标成员这一条记录，保存前后逐字比对其余成员与
    全部历史审计；本函数不删除台账、快照或该成员已产生的任何业务审计。
    """
    normalized_username = _validate_username(username)
    normalized_admin_user_id = _validate_user_id(removed_by_user_id, "removed_by_user_id")
    root = shared_ledger_root()
    with _exclusive_roster_lock(root):
        roster = _load_team_roster(root)
        members = roster["members"]
        target = next(
            (
                member
                for member in members
                if str(member["username"]).casefold() == normalized_username.casefold()
            ),
            None,
        )
        if target is None:
            raise ValueError("该用户不在招商团队名册中")
        members_before = [_stable_json(member) for member in members]
        target_index = members.index(target)
        members.remove(target)
        # 只允许移除目标这一条记录；其余成员的逐字内容与顺序必须保持不变。
        remaining = [_stable_json(member) for member in members]
        if remaining != members_before[:target_index] + members_before[target_index + 1 :]:
            raise RuntimeError("移除名册成员不得改写其他成员记录")
        audit = _append_roster_audit(
            roster,
            action="remove",
            target_user_id=str(target["user_id"]),
            target_username=str(target["username"]),
            operated_by_user_id=normalized_admin_user_id,
        )
        _save_team_roster(root, roster)
    return {"member": dict(target), "audit": audit}
