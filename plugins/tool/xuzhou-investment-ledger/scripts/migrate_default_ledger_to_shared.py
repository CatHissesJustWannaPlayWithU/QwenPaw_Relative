#!/usr/bin/env python3
"""把默认智能体的空正式证据链台账一次性复制到徐州招商团队共享目录。

本脚本只处理本机已知的默认工作区，并且只允许当前没有正式线索和正式证据的场景：
现有候选、候选观察、原始候选快照及其审计会被保留，正式 evidence/lead 一旦存在即拒绝。
因为 HMAC 密钥按目录派生，复制后必须由已认证管理员调用服务器现有的“空台账重签”接口；
脚本不会接收用户 ID、目标目录、来源 URL、证据字段或密钥，也绝不删除旧工作区数据。
"""

from __future__ import annotations

import copy
import getpass
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# 以下位置由服务器安装结构固定：迁移不接受命令行或前端传入的任意工作区/目录，避免把
# “共享台账迁移”变成跨用户复制数据的通道。共享目标实际由插件的 shared_ledger_root()
# 决定，仍会遵守部署环境中的 XUZHOU_INVESTMENT_LEDGER_SHARED_DIR 覆盖值。
DEFAULT_SOURCE_WORKSPACE_DIR = Path.home() / ".qwenpaw/workspaces/default"
AGENT_ID = "default"
API_BASE_URL = "http://127.0.0.1:8088"
REQUEST_TIMEOUT_SECONDS = 30
LEDGER_DIRECTORY_NAME = "investment_attraction"
LEDGER_FILE_NAME = "lead_evidence_ledger.json"
SNAPSHOT_DIRECTORY_NAMES = (
    "candidate_observation_snapshots",
    "evidence_snapshots",
)
MIGRATION_AUDIT_FIELD = "shared_ledger_migrations"
MIGRATION_OPERATION_SCOPE = "local_fixed_default_to_shared_copy_then_admin_reseal_only"
RESEAL_NOTE = (
    "交互式本机共享台账迁移：已核对固定默认工作区的完整性校验与文件哈希；"
    "当前不存在正式线索和正式证据，仅将候选、观察和快照复制到固定团队共享目录后重签。"
)


class UserFacingError(RuntimeError):
    """表示可以直接显示给本机运维人员的安全校验或服务调用失败。"""


def _utc_now() -> str:
    """返回迁移审计使用的 UTC 时间，不接受本机命令行或客户端提供的时间值。"""
    return datetime.now(timezone.utc).isoformat()


def _load_plugin_module(module_name: str):
    """从本插件目录加载存储与共享目录模块，不依赖已安装插件的缓存版本。

    输入是本脚本写死的插件内部模块名，输出是相对导入可用的模块对象。加载目的仅是复用
    ``storage.load_ledger`` 对源台账的 HMAC/快照校验，以及 ``shared_ledger_root`` 对固定
    目标路径的权限与符号链接防护；模块名、源目录和目标目录均不能由操作者、模型或前端
    指定。函数不读写业务数据、也不注册 HTTP 路由或执行工具。
    """
    plugin_root = Path(__file__).resolve().parents[1]
    package_name = "xuzhou_shared_ledger_migration_runtime"
    sys.modules.pop(package_name, None)
    for loaded_module_name in list(sys.modules):
        if loaded_module_name.startswith(f"{package_name}."):
            sys.modules.pop(loaded_module_name, None)
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_root / "xuzhou_investment_ledger.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise UserFacingError("无法加载徐州招商插件存储模块，已停止共享台账迁移。")
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return importlib.import_module(f"{package_name}.{module_name}")


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """向固定本机服务发送最小 JSON 请求，令牌只在内存中使用一次。

    输入只能是写死的本机 API 相对路径、受限 JSON 请求体与管理员登录后短期令牌；输出为
    服务端 JSON 对象。请求不会传递 user_id、签名、密钥、工作区或证据字段，管理员身份
    必须由服务端登录和路由再次验证。网络、HTTP 或响应格式异常都会失败关闭，不会把令牌
    写入日志、文件或输出，也不会在失败后重试复制/重签。
    """
    if not path.startswith("/"):
        raise UserFacingError("内部安全校验失败：接口路径必须以 / 开头。")
    headers = {"Accept": "application/json", "X-Agent-Id": AGENT_ID}
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(
        f"{API_BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw_response = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        reason = detail if isinstance(detail, str) and detail else "未返回可用原因"
        raise UserFacingError(f"服务器拒绝请求（HTTP {exc.code}）：{reason}") from exc
    except (URLError, TimeoutError) as exc:
        raise UserFacingError("无法连接或等待本机 QwenPaw 服务，请确认服务已启动后重试。") from exc
    try:
        response_payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise UserFacingError("本机服务返回的不是有效 JSON，已停止迁移。") from exc
    if not isinstance(response_payload, dict):
        raise UserFacingError("本机服务返回格式异常，已停止迁移。")
    return response_payload


def _authenticate_existing_admin() -> str:
    """让本机操作者登录，并只在内存中返回已验证管理员的短期令牌。

    输入来自终端的用户名和隐藏密码；输出为服务端签发的令牌。脚本不接受命令行 user_id、
    角色或令牌，不打印密码/令牌，且明确检查登录响应中的 admin 角色；普通用户、禁用账号
    或异常响应均不能继续复制或重签共享台账。
    """
    username = input("现有管理员用户名：").strip()
    if not username:
        raise UserFacingError("管理员用户名不能为空。")
    password = getpass.getpass("管理员密码（输入时不会显示）：")
    if not password:
        raise UserFacingError("管理员密码不能为空。")
    response = _request_json(
        "/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )
    token = response.get("token")
    user = response.get("user")
    if not isinstance(token, str) or not token or not isinstance(user, dict):
        raise UserFacingError("登录响应缺少必要字段，已停止迁移。")
    if user.get("role") != "admin":
        raise UserFacingError("当前登录用户不是管理员，不能迁移共享招商台账。")
    return token


def _require_enabled_authentication() -> None:
    """确认本机服务已启用且存在登录账号；不复制、重签或修改任何文件。"""
    auth_status = _request_json("/api/auth/status")
    if auth_status.get("enabled") is not True:
        raise UserFacingError("QwenPaw 登录验证未启用，已停止共享台账迁移。")
    if auth_status.get("has_users") is not True:
        raise UserFacingError("尚未创建登录用户，无法确认管理员身份，已停止迁移。")


def _safe_snapshot_files(directory: Path) -> list[Path]:
    """列出固定快照目录中的普通 JSON 文件，拒绝链接、嵌套和非预期文件。

    输入是源工作区中两个服务器固定快照目录之一，输出按文件名排序的可复制 JSON 文件。
    文件名、目录名和层级不来自 URL、模型或用户输入；任何符号链接、子目录、非文件或非
    JSON 项都会使迁移失败，避免复制时跟随链接读出其他用户/系统数据。函数只读目录项，
    不修改快照、台账或证据链。
    """
    if directory.is_symlink() or not directory.is_dir():
        raise UserFacingError(f"快照目录异常：{directory.name}")
    files: list[Path] = []
    for item in directory.iterdir():
        if item.is_symlink() or not item.is_file() or item.suffix != ".json":
            raise UserFacingError(f"快照目录包含非预期文件，已停止迁移：{directory.name}")
        files.append(item)
    return sorted(files, key=lambda item: item.name)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以临时文件和原子替换写入目标迁移审计，不覆盖任何既有路径。

    输入是新建暂存目录里的固定台账路径和服务器已验证过的源载荷副本；输出为空。函数只
    在尚未公开的暂存目录中使用，文件名不能由调用方指定；写入时仅追加本次迁移来源审计，
    不改变原台账、原始 evidence（本迁移要求为空）、来源 URL、摘录、采集时间或评分依据。
    """
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".shared_ledger_migration_",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _preflight_source() -> tuple[object, object, Path, bytes, dict[str, Any], dict[str, int]]:
    """验证固定旧台账的完整性与空正式证据链条件，并返回不可变复制输入。

    输入为空；输出为插件 storage/shared 模块、固定源工作区、原始台账字节、JSON 载荷及
    两类快照数量。函数先调用 ``storage.load_ledger``，因此会验证源台账的 HMAC、空/正式
    evidence 链及正式证据快照；随后只允许 ``leads`` 和 ``evidence`` 均为空。候选与候选
    快照可保留，这是早期公开信号而非已确认招商事实。任何路径、链接、格式或完整性异常
    都失败关闭，且本函数绝不写源目录或共享目录。
    """
    storage = _load_plugin_module("storage")
    shared_workspace = _load_plugin_module("shared_workspace")
    source_workspace = DEFAULT_SOURCE_WORKSPACE_DIR
    source_data_dir = source_workspace / LEDGER_DIRECTORY_NAME
    source_ledger_path = source_data_dir / LEDGER_FILE_NAME
    if (
        source_workspace.is_symlink()
        or source_data_dir.is_symlink()
        or source_ledger_path.is_symlink()
        or not source_ledger_path.is_file()
    ):
        raise UserFacingError("固定默认台账路径不存在、不是普通文件或含符号链接，已停止迁移。")
    try:
        # 不使用返回值写回；此调用只验证源目录仍是可信的可迁移起点。
        storage.load_ledger(source_workspace)
        source_bytes = source_ledger_path.read_bytes()
        source_ledger = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        raise UserFacingError("默认招商台账完整性或格式校验失败，已停止迁移。") from exc
    if not isinstance(source_ledger, dict):
        raise UserFacingError("默认招商台账根节点不是对象，已停止迁移。")
    if source_ledger.get("schema_version") != 1:
        raise UserFacingError("默认招商台账版本不受支持，已停止迁移。")
    leads = source_ledger.get("leads")
    evidence = source_ledger.get("evidence")
    if not isinstance(leads, list) or not isinstance(evidence, list):
        raise UserFacingError("默认招商台账缺少有效 leads/evidence 列表，已停止迁移。")
    if leads or evidence:
        raise UserFacingError(
            "默认台账已有正式线索或证据。本脚本绝不重新签名已有事实，请联系工程师执行受控迁移。"
        )
    snapshot_counts: dict[str, int] = {}
    for directory_name in SNAPSHOT_DIRECTORY_NAMES:
        directory = source_data_dir / directory_name
        if directory.is_symlink():
            raise UserFacingError(f"快照目录不能是符号链接，已停止迁移：{directory_name}")
        snapshot_counts[directory_name] = (
            len(_safe_snapshot_files(directory)) if directory.exists() else 0
        )
    return storage, shared_workspace, source_workspace, source_bytes, source_ledger, snapshot_counts


def _copy_fixed_source_to_new_shared_target(
    *,
    shared_workspace: object,
    source_workspace: Path,
    source_bytes: bytes,
    source_ledger: dict[str, Any],
    snapshot_counts: dict[str, int],
) -> dict[str, Any]:
    """把已经验证的固定源目录原子发布到尚不存在的共享业务目录，原目录保持不变。

    输入只来自 ``_preflight_source`` 的固定路径与已验证载荷；输出为目标台账 SHA-256、路径
    和快照数量。函数先在共享根目录的私有临时目录复制，再以一次目录重命名发布；如果目标
    ``investment_attraction`` 已存在、是符号链接或复制中出现异常则拒绝，不覆盖、合并或
    删除任何既有共享数据。目标载荷只追加一条来源哈希/数量审计，正式线索和证据在前后均
    保持空；因目录切换会让 HMAC 必然失效，调用方必须立即用管理员接口按返回 SHA-256
    重签。函数从不删除旧默认工作区，也不接受任意来源/目标路径或用户 ID。
    """
    shared_root = shared_workspace.shared_ledger_root()
    if not isinstance(shared_root, Path):
        raise UserFacingError("共享台账根目录解析异常，已停止迁移。")
    destination_data_dir = shared_root / LEDGER_DIRECTORY_NAME
    if destination_data_dir.exists() or destination_data_dir.is_symlink():
        raise UserFacingError(
            "共享业务台账目录已存在，拒绝覆盖。若上次复制后尚未重签，请使用 --resume-reseal。"
        )
    source_data_dir = source_workspace / LEDGER_DIRECTORY_NAME
    staging_root = Path(tempfile.mkdtemp(dir=shared_root, prefix=".shared_ledger_migration_"))
    try:
        os.chmod(staging_root, 0o700)
        staging_data_dir = staging_root / LEDGER_DIRECTORY_NAME
        staging_data_dir.mkdir(mode=0o700)
        staged_ledger_path = staging_data_dir / LEDGER_FILE_NAME
        shutil.copy2(source_data_dir / LEDGER_FILE_NAME, staged_ledger_path)
        os.chmod(staged_ledger_path, 0o600)
        for directory_name in SNAPSHOT_DIRECTORY_NAMES:
            source_directory = source_data_dir / directory_name
            if source_directory.is_symlink():
                raise UserFacingError(f"快照目录不能是符号链接，已停止迁移：{directory_name}")
            if not source_directory.exists():
                continue
            destination_directory = staging_data_dir / directory_name
            destination_directory.mkdir(mode=0o700)
            copied_count = 0
            for source_file in _safe_snapshot_files(source_directory):
                target_file = destination_directory / source_file.name
                shutil.copy2(source_file, target_file)
                os.chmod(target_file, 0o600)
                copied_count += 1
            if copied_count != snapshot_counts[directory_name]:
                raise RuntimeError("快照复制数量与迁移前核验不一致")

        # 仅对暂存副本追加可审查的复制来源信息。这里不写操作者身份：管理员身份、时间与
        # 重签权限必须由后续服务器接口产生的 reseal 审计记录承担，不能由本机脚本伪造。
        target_ledger = copy.deepcopy(source_ledger)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        migrations = target_ledger.get(MIGRATION_AUDIT_FIELD)
        if migrations is None:
            migrations = []
            target_ledger[MIGRATION_AUDIT_FIELD] = migrations
        if not isinstance(migrations, list):
            raise UserFacingError("台账中的共享迁移审计字段格式异常，已停止迁移。")
        migrations.append(
            {
                "migration_id": "shared-ledger-copy-" + source_sha256[:16],
                "copied_at": _utc_now(),
                "source_workspace": str(source_workspace),
                "source_ledger_sha256": source_sha256,
                "source_snapshot_counts": dict(snapshot_counts),
                "operation_scope": MIGRATION_OPERATION_SCOPE,
                "server_authenticated_operator_record": "pending_admin_reseal_audit",
            }
        )
        # 此迁移类型明确没有正式事实；二次检查确保上方审计追加没有意外带入业务内容。
        if target_ledger.get("leads") != [] or target_ledger.get("evidence") != []:
            raise RuntimeError("共享台账复制不得修改正式线索或证据字段")
        _atomic_write_json(staged_ledger_path, target_ledger)
        copied_target_bytes = staged_ledger_path.read_bytes()
        # 在公开发布前再比对原文件，防止长复制期间源台账被后台任务更新而静默丢失变化。
        if (source_data_dir / LEDGER_FILE_NAME).read_bytes() != source_bytes:
            raise UserFacingError("复制期间默认台账已变化，已停止发布共享副本；原台账未被修改。")
        os.replace(staging_data_dir, destination_data_dir)
        os.chmod(destination_data_dir, 0o700)
    except Exception:
        # 临时目录由 mkdtemp 在本函数中创建且尚未公开；仅清理该确定目标，绝不触及源目录
        # 或已经存在的共享业务目录。若 os.replace 已成功，staging_root 只会遗留空目录。
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        if staging_root.exists():
            staging_root.rmdir()
    return {
        "shared_ledger_path": str(destination_data_dir / LEDGER_FILE_NAME),
        "shared_ledger_sha256_before_reseal": hashlib.sha256(copied_target_bytes).hexdigest(),
        "source_ledger_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_snapshot_counts": dict(snapshot_counts),
    }


def _load_resumable_shared_ledger_sha256(shared_workspace: object) -> str:
    """只读验证上次复制留下的共享空台账，返回管理员重签所需文件哈希。

    输入是插件固定共享目录解析模块；输出是共享台账当前原始字节 SHA-256。该恢复路径只
    允许 ``shared_ledger_migrations`` 含本脚本固定操作范围、且 leads/evidence 仍为空的
    普通文件；它不重新复制、不删除目标、不读取旧默认台账，也不接受目录、哈希、签名或
    用户 ID 参数。是否可重签及管理员身份仍由服务器接口再次校验。
    """
    shared_root = shared_workspace.shared_ledger_root()
    if not isinstance(shared_root, Path):
        raise UserFacingError("共享台账根目录解析异常，无法继续重签。")
    ledger_path = shared_root / LEDGER_DIRECTORY_NAME / LEDGER_FILE_NAME
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise UserFacingError("没有可恢复重签的共享台账副本。")
    try:
        raw_ledger = ledger_path.read_bytes()
        ledger = json.loads(raw_ledger.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserFacingError("共享台账副本无法读取，拒绝重签。") from exc
    if not isinstance(ledger, dict) or ledger.get("leads") != [] or ledger.get("evidence") != []:
        raise UserFacingError("共享台账副本含正式事实或格式异常，拒绝使用恢复重签。")
    migrations = ledger.get(MIGRATION_AUDIT_FIELD)
    if not isinstance(migrations, list) or not any(
        isinstance(record, dict) and record.get("operation_scope") == MIGRATION_OPERATION_SCOPE
        for record in migrations
    ):
        raise UserFacingError("共享台账副本缺少固定迁移审计，拒绝重签。")
    return hashlib.sha256(raw_ledger).hexdigest()


def _request_admin_reseal(*, token: str, ledger_sha256: str) -> dict[str, Any]:
    """以已认证管理员身份请求服务器重签共享目录的空正式证据链台账。

    输入是仅在内存中的管理员令牌和本机刚读取的 64 位 SHA-256；输出为服务端产生的脱敏
    重签审计。请求体不含操作者、密钥、旧/新签名、目录、线索、证据、URL 或摘录；服务器
    会在锁内再次比较哈希、确认台账确实为空且当前 HMAC 因目录迁移不匹配，随后仅追加
    重签审计和新 HMAC。若服务器拒绝，本函数不自行改写文件或降级为离线重签。
    """
    response = _request_json(
        "/api/xuzhou-investment-ledger/evidence-integrity/reseal-empty-ledger",
        method="POST",
        bearer_token=token,
        payload={
            "expected_ledger_sha256": ledger_sha256,
            "verification_note": RESEAL_NOTE,
        },
    )
    reseal = response.get("reseal")
    if not isinstance(reseal, dict):
        raise UserFacingError("服务器响应缺少重签审计；请勿重复复制，请联系工程师核验。")
    return reseal


def migrate_default_ledger_to_shared() -> dict[str, Any]:
    """执行“验证源→管理员登录→原子复制→服务器重签”的完整一次性迁移。

    输入为空；输出仅含固定源/目标路径、哈希、快照数和服务器重签审计 ID。先完成只读
    源校验和认证，避免未授权用户创建副本；复制从不删除旧默认台账，目标存在时拒绝覆盖。
    复制后 HMAC 会因目录变化预期失效，只有服务器管理员接口能完成重签；所有正式证据字段
    在本迁移前后均为空，候选与快照只按字节复制而不被解释为企业事实或自动推进线索。
    """
    _storage, shared_workspace, source_workspace, source_bytes, source_ledger, snapshot_counts = (
        _preflight_source()
    )
    _require_enabled_authentication()
    token = _authenticate_existing_admin()
    try:
        copy_result = _copy_fixed_source_to_new_shared_target(
            shared_workspace=shared_workspace,
            source_workspace=source_workspace,
            source_bytes=source_bytes,
            source_ledger=source_ledger,
            snapshot_counts=snapshot_counts,
        )
        reseal = _request_admin_reseal(
            token=token,
            ledger_sha256=str(copy_result["shared_ledger_sha256_before_reseal"]),
        )
    finally:
        token = ""
    return {
        "status": "默认招商台账已复制到团队共享目录并完成服务器重签",
        **copy_result,
        "reseal_id": reseal.get("reseal_id"),
        "operation_scope": reseal.get("operation_scope"),
    }


def resume_shared_ledger_reseal() -> dict[str, Any]:
    """仅在复制已完成但服务器重签未完成时恢复最后一步，绝不再次复制或覆盖数据。

    输入为空；输出为服务端重签审计。函数只读检查固定共享目录中的迁移审计和空正式证据链，
    再要求管理员登录；不触碰默认源台账、候选快照或目标业务内容。此路径用于网络/服务短暂
    失败后的安全恢复，不能绕过管理员认证或把含正式事实的台账重新签名。
    """
    shared_workspace = _load_plugin_module("shared_workspace")
    _require_enabled_authentication()
    ledger_sha256 = _load_resumable_shared_ledger_sha256(shared_workspace)
    token = _authenticate_existing_admin()
    try:
        reseal = _request_admin_reseal(token=token, ledger_sha256=ledger_sha256)
    finally:
        token = ""
    return {
        "status": "共享台账复制副本已完成服务器重签",
        "reseal_id": reseal.get("reseal_id"),
        "operation_scope": reseal.get("operation_scope"),
    }


def main() -> int:
    """只接受明确迁移或恢复参数，避免无参数意外复制或重签本地招商数据。"""
    try:
        arguments = sys.argv[1:]
        if arguments == ["--copy-and-reseal"]:
            result = migrate_default_ledger_to_shared()
        elif arguments == ["--resume-reseal"]:
            result = resume_shared_ledger_reseal()
        else:
            raise UserFacingError(
                "用法：--copy-and-reseal 执行一次复制并重签；"
                "复制已完成但重签失败时使用 --resume-reseal。"
            )
    except KeyboardInterrupt:
        print("\n已取消；未执行后续迁移步骤。", file=sys.stderr)
        return 1
    except UserFacingError as exc:
        print(f"未迁移：{exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"未迁移：本机文件操作失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
