#!/usr/bin/env python3
"""以现有管理员身份迁移默认工作区的空旧版招商台账。

本脚本是给本机运维人员使用的一次性辅助工具，不是招商业务工具，也不采集或修改
任何线索。它只调用已部署插件的受保护迁移接口；服务器仍会独立校验当前登录身份和
管理员角色，脚本本身不能绕过权限判断。
"""

from __future__ import annotations

import getpass
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# 固定为本机 QwenPaw 服务，避免把管理员口令或迁移请求发送到未知远程地址。
API_BASE_URL = "http://127.0.0.1:8088"
# 固定为默认工作区，禁止本脚本被用于跨用户或任意路径的台账迁移。
DEFAULT_LEDGER_PATH = (
    Path.home()
    / ".qwenpaw/workspaces/default/investment_attraction/lead_evidence_ledger.json"
)
AGENT_ID = "default"
REQUEST_TIMEOUT_SECONDS = 15
MIGRATION_NOTE = (
    "交互式本机管理员迁移：已核对固定默认工作区旧台账 SHA-256，"
    "确认当前无正式线索和正式证据，仅补齐完整性元数据与追加式迁移审计。"
)
# 整本签名恢复与旧版未签名迁移是两个严格分开的管理员操作。此说明仅用于服务端追加
# 审计，不能由命令行传入，以免模型/前端/脚本调用者借备注混入身份、证据或密钥信息。
RESEAL_NOTE = (
    "交互式本机管理员恢复：已核对固定默认工作区当前空台账 SHA-256，"
    "仅在服务端确认整本 HMAC 不匹配且正式线索、正式证据均为空时重新签名并追加恢复审计。"
)
EVIDENCE_INTEGRITY_SCHEME = "hmac-sha256-evidence-chain-v2"
LEDGER_INTEGRITY_SCHEME = "hmac-sha256-ledger-v1"


class UserFacingError(RuntimeError):
    """表示可直接告知操作者的安全校验或服务调用失败。"""


def _request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """向固定本机服务发起 JSON 请求并返回对象结果。

    输入仅限服务器相对路径、可序列化 JSON 请求体及内存中的短期令牌；输出为服务端
    JSON 对象。若需要认证，令牌只放在本次 HTTP 请求头中，既不打印也不写入磁盘。
    迁移的管理员身份由服务器登录接口签发并由迁移接口再次验证，绝不接受脚本参数或
    请求体中伪造的 ``reviewer_user_id`` / ``admin_user_id``。
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
        # 服务端错误正文可能包含解释，但绝不回显请求头，以免意外暴露令牌。
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            detail = error_body.get("detail") if isinstance(error_body, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        detail_text = detail if isinstance(detail, str) and detail else "未返回可用原因"
        raise UserFacingError(f"服务器拒绝请求（HTTP {exc.code}）：{detail_text}") from exc
    except URLError as exc:
        raise UserFacingError(
            "无法连接本机 QwenPaw 服务（http://127.0.0.1:8088）。"
            "请确认 QwenPaw 已启动后重试。"
        ) from exc

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise UserFacingError("本机服务返回的不是有效 JSON，已停止迁移。") from exc
    if not isinstance(result, dict):
        raise UserFacingError("本机服务返回格式异常，已停止迁移。")
    return result


def load_empty_unsigned_legacy_ledger_sha256(ledger_path: Path) -> str:
    """校验一个旧台账确为空且未签名，并返回其原始字节的 SHA-256。

    输入是本机台账路径，输出是该文件当前原始字节的 SHA-256；此函数只读文件，绝不写
    入任何字段。仅允许 schema_version 为 1、``leads`` 与 ``evidence`` 都为空列表，且
    尚无 evidence/ledger 完整性元数据的旧台账。这样可以保证本次迁移不会接触已存在的
    来源 URL、原始摘录、采集时间、评分依据等证据链字段；发现任何正式数据或已迁移
    痕迹即拒绝，改由人工复核处理。
    """

    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise UserFacingError("默认台账不存在、不是普通文件或是符号链接，已停止迁移。")
    try:
        raw_ledger = ledger_path.read_bytes()
        ledger = json.loads(raw_ledger.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserFacingError("默认台账无法作为有效 JSON 旧台账读取，已停止迁移。") from exc

    if not isinstance(ledger, dict):
        raise UserFacingError("默认台账根节点不是 JSON 对象，已停止迁移。")
    if ledger.get("schema_version") != 1:
        raise UserFacingError("默认台账不是预期的 schema_version=1 旧版，已停止迁移。")

    leads = ledger.get("leads")
    evidence = ledger.get("evidence")
    if not isinstance(leads, list) or not isinstance(evidence, list):
        raise UserFacingError("旧台账的 leads/evidence 字段格式异常，已停止迁移。")
    if leads or evidence:
        raise UserFacingError(
            "旧台账已有正式线索或证据。为避免任何证据链被意外改写，本助手只允许迁移空台账。"
        )
    if "evidence_integrity" in ledger or "ledger_integrity" in ledger:
        raise UserFacingError("台账已含完整性元数据，不能重复迁移。")

    return hashlib.sha256(raw_ledger).hexdigest()


def load_empty_resealable_ledger_sha256(ledger_path: Path) -> str:
    """只读核验当前签名格式的空台账，并返回原始字节 SHA-256 供服务器二次比对。

    输入是固定默认台账路径，输出是当前原始文件字节的 SHA-256。此函数只允许
    ``schema_version=1``、空 ``leads/evidence``、当前空证据链，以及具有当前格式但
    未在本机脚本中验证的整本 HMAC 元数据；它不判断 HMAC 是否匹配，因为该判断必须由
    持有实际平台密钥的服务端执行。函数不写文件、不接收用户 ID、密钥、证据、URL 或
    摘录，发现已有正式事实、格式异常或符号链接即停止，避免“恢复”触及证据链。

    付费升级方案：多机部署时，应将恢复前 SHA-256 和管理员审批同步到 KMS/HSM 保护的
    审计服务或带对象锁的不可变存储（通常按 KMS 实例、对象容量、请求与保留期计费），
    并采用双人复核；不要把主密钥、旧签名或重签能力暴露给本脚本、前端或模型。
    """

    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise UserFacingError("默认台账不存在、不是普通文件或是符号链接，已停止恢复。")
    try:
        raw_ledger = ledger_path.read_bytes()
        ledger = json.loads(raw_ledger.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserFacingError("默认台账无法作为有效 JSON 当前台账读取，已停止恢复。") from exc
    if not isinstance(ledger, dict):
        raise UserFacingError("默认台账根节点不是 JSON 对象，已停止恢复。")
    if ledger.get("schema_version") != 1:
        raise UserFacingError("默认台账不是预期的 schema_version=1，已停止恢复。")
    leads = ledger.get("leads")
    evidence = ledger.get("evidence")
    if not isinstance(leads, list) or not isinstance(evidence, list):
        raise UserFacingError("台账的 leads/evidence 字段格式异常，已停止恢复。")
    if leads or evidence:
        raise UserFacingError(
            "台账已有正式线索或证据。为避免任何证据链被意外改写，本助手只允许恢复空台账。"
        )
    evidence_integrity = ledger.get("evidence_integrity")
    if (
        not isinstance(evidence_integrity, dict)
        or evidence_integrity.get("scheme") != EVIDENCE_INTEGRITY_SCHEME
        or evidence_integrity.get("record_count") != 0
        or evidence_integrity.get("chain_head_signature") is not None
    ):
        raise UserFacingError("台账不是当前格式的空证据链，已停止恢复。")
    ledger_integrity = ledger.get("ledger_integrity")
    signature = ledger_integrity.get("signature") if isinstance(ledger_integrity, dict) else None
    if (
        not isinstance(ledger_integrity, dict)
        or ledger_integrity.get("scheme") != LEDGER_INTEGRITY_SCHEME
        or not isinstance(signature, str)
        or len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise UserFacingError("台账不是当前格式的整本完整性签名，已停止恢复。")
    return hashlib.sha256(raw_ledger).hexdigest()


def authenticate_existing_admin() -> str:
    """交互式登录并仅在内存中返回已验证管理员的短期访问令牌。

    输入为操作者在本机终端输入的现有用户名和隐藏密码，输出为登录接口签发的令牌。脚本
    不接受前端、模型或命令行声称的用户 ID；它还会检查服务器返回的角色必须是 admin。
    密码和令牌均不写文件、不出现在成功输出中，并会在迁移调用完成后由调用方丢弃。
    """

    username = input("现有管理员用户名：").strip()
    if not username:
        raise UserFacingError("管理员用户名不能为空。")
    password = getpass.getpass("现有管理员密码（输入时不会显示）：")
    if not password:
        raise UserFacingError("管理员密码不能为空。")

    login_result = _request_json(
        "/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )
    # QwenPaw 的 LoginResponse 明确使用 ``token``；不兼容地猜测为
    # ``access_token`` 会在密码已验证后误判失败，从而阻断受保护迁移。
    token = login_result.get("token")
    user = login_result.get("user")
    if not isinstance(token, str) or not token or not isinstance(user, dict):
        raise UserFacingError("登录响应缺少必要字段，已停止迁移。")
    if user.get("role") != "admin":
        raise UserFacingError("当前登录用户不是管理员，不能执行旧台账完整性迁移。")
    return token


def migrate_default_empty_ledger() -> dict[str, Any]:
    """在服务器确认管理员身份后迁移固定的空旧台账，并返回经过脱敏的结果。

    该函数没有任何可由用户替换的台账路径、用户 ID、证据字段或 SHA 参数：先读取固定
    台账并计算当前 SHA-256，再将此值交给服务器作并发一致性校验。服务器只会追加迁移
    审计和完整性元数据；本脚本不传递、更不修改 evidence、来源 URL、原始摘录、采集
    时间或评分依据。输出只保留迁移编号、迁移数量和操作范围，避免泄露访问令牌。
    """

    auth_status = _request_json("/api/auth/status")
    if auth_status.get("enabled") is not True:
        raise UserFacingError("QwenPaw 登录验证未启用，已停止迁移。")
    if auth_status.get("has_users") is not True:
        raise UserFacingError("尚未创建登录用户，无法确认管理员身份，已停止迁移。")

    legacy_sha256 = load_empty_unsigned_legacy_ledger_sha256(DEFAULT_LEDGER_PATH)
    token = authenticate_existing_admin()
    try:
        migration_result = _request_json(
            "/api/xuzhou-investment-ledger/evidence-integrity/migrate-legacy",
            method="POST",
            bearer_token=token,
            payload={
                "expected_legacy_ledger_sha256": legacy_sha256,
                "verification_note": MIGRATION_NOTE,
            },
        )
    finally:
        # 令牌只应存活到这次受保护请求结束；不缓存、不记录，也不复用。
        token = ""

    # 迁移接口将不可变迁移审计放在 migration 对象内；数量和台账路径仍在顶层。
    # 只读取服务端已生成的审计，不接受或重组任何客户端提供的操作者、证据字段。
    migration_audit = migration_result.get("migration")
    if not isinstance(migration_audit, dict):
        raise UserFacingError(
            "迁移已由服务器接受，但响应缺少迁移审计对象；请勿重复执行迁移，请联系工程师核验。"
        )

    return {
        "status": "迁移请求已完成",
        "migration_id": migration_audit.get("migration_id"),
        "migrated_evidence_count": migration_result.get("migrated_evidence_count"),
        "operation_scope": migration_audit.get("operation_scope"),
        "ledger_path": str(DEFAULT_LEDGER_PATH),
    }


def reseal_default_empty_ledger() -> dict[str, Any]:
    """请求服务端以当前认证管理员身份恢复固定的空台账整本 HMAC。

    输入不接受命令行参数；输出仅包含服务端生成的恢复编号、操作范围和固定路径。函数先
    只读核验本地空台账，再以服务器登录令牌调用管理员接口；请求体仅含 SHA-256 与固定
    核验说明，不含操作者 ID、旧/新签名、密钥、线索、证据、来源 URL、摘录、采集时间或
    评分依据。服务端会在锁内复算 SHA-256、确认其实际密钥确实不能验证旧签名，并拒绝
    含有任何正式线索或证据的台账；因此本脚本不能绕过权限或修改原始证据链。
    """

    auth_status = _request_json("/api/auth/status")
    if auth_status.get("enabled") is not True:
        raise UserFacingError("QwenPaw 登录验证未启用，已停止恢复。")
    if auth_status.get("has_users") is not True:
        raise UserFacingError("尚未创建登录用户，无法确认管理员身份，已停止恢复。")

    ledger_sha256 = load_empty_resealable_ledger_sha256(DEFAULT_LEDGER_PATH)
    token = authenticate_existing_admin()
    try:
        reseal_result = _request_json(
            "/api/xuzhou-investment-ledger/evidence-integrity/reseal-empty-ledger",
            method="POST",
            bearer_token=token,
            payload={
                "expected_ledger_sha256": ledger_sha256,
                "verification_note": RESEAL_NOTE,
            },
        )
    finally:
        # 短期令牌仅在本次管理员请求中存在；不记录、不缓存，也不用于其他接口。
        token = ""

    reseal_audit = reseal_result.get("reseal")
    if not isinstance(reseal_audit, dict):
        raise UserFacingError(
            "恢复已由服务器接受，但响应缺少恢复审计对象；请勿重复执行，请联系工程师核验。"
        )
    return {
        "status": "空台账整本签名恢复请求已完成",
        "reseal_id": reseal_audit.get("reseal_id"),
        "operation_scope": reseal_audit.get("operation_scope"),
        "ledger_path": str(DEFAULT_LEDGER_PATH),
    }


def main() -> int:
    """运行本机空台账迁移或整本签名恢复，并打印可安全转发的脱敏结果。

    无参数时仅运行旧版未签名迁移；精确的 ``--reseal-empty-ledger`` 参数才会请求当前
    空台账的整本签名恢复。返回值 0 表示服务器已接受相应操作，1 表示安全、身份或网络
    校验未通过。未知参数一律失败，函数不会降级、跳过认证或直接写台账文件。
    """

    try:
        arguments = sys.argv[1:]
        if not arguments:
            safe_result = migrate_default_empty_ledger()
        elif arguments == ["--reseal-empty-ledger"]:
            safe_result = reseal_default_empty_ledger()
        else:
            raise UserFacingError(
                "用法：不带参数执行旧版迁移；或使用 --reseal-empty-ledger 恢复当前空台账签名。"
            )
    except KeyboardInterrupt:
        print("\n已取消；未执行迁移。", file=sys.stderr)
        return 1
    except UserFacingError as exc:
        print(f"未迁移：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(safe_result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
