"""徐州招商历史证据完整性迁移的管理员专用 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .storage import (
    migrate_verified_legacy_evidence_integrity,
    reseal_verified_empty_ledger_integrity,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class MigrateLegacyEvidenceIntegrityRequest(BaseModel):
    """管理员可信迁移仅允许提交预先核验的文件哈希和核验说明。

    请求模型明确禁止提交管理员 ID、evidence、URL、摘录、签名、迁移时间或审计对象；
    身份只能取服务器认证主体，证据内容只能从固定台账读取。这样客户端不能借安全迁移
    伪造操作者、替换原始来源链或自行指定需要被重新签名的记录。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_legacy_ledger_sha256: str = Field(..., min_length=64, max_length=64)
    verification_note: str = Field(..., min_length=1, max_length=2_000)


class ResealEmptyLedgerIntegrityRequest(BaseModel):
    """管理员恢复空台账整本签名时唯一允许提交的核验字段。

    请求只能提供管理员已核验的当前文件 SHA-256 和恢复说明；不能提交管理员 ID、旧/新
    密钥、签名、线索、证据、URL、摘录、时间或恢复审计。管理员身份只能从服务器认证
    主体取得，核心层还会在固定工作区中确认台账没有任何正式线索或证据，避免前端或模型
    借“恢复”接口覆盖真实证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_ledger_sha256: str = Field(..., min_length=64, max_length=64)
    verification_note: str = Field(..., min_length=1, max_length=2_000)


@router.post("/migrate-legacy", status_code=status.HTTP_200_OK)
async def migrate_xuzhou_legacy_evidence_integrity(
    body: MigrateLegacyEvidenceIntegrityRequest,
    request: Request,
) -> dict[str, Any]:
    """仅管理员可将已人工核验的旧台账封存为当前签名链，普通用户一律拒绝。

    输入是限定的历史文件 SHA-256、管理员核验备注和认证请求；输出为追加式迁移审计、
    已重封数量及台账路径。函数先取得服务器 ``Principal`` 并强制 ``is_admin``，随后由
    平台校验其拥有工作区；请求体没有用户 ID 或证据字段。核心迁移会在保存锁内再次比较
    哈希，只新增签名元数据和审计，原始 URL、摘录、采集时间、内容哈希及评分依据保持
    不变。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    迁移完成后可将迁移前 SHA-256 与追加审计同步到 WORM、KMS/HSM 签名或第三方时间戳
    服务，以提供本机安全边界外的留存证明。以阿里云公开价估算，软件 KMS 约 2,499 元/月、
    硬件 KMS 约 7,699 元/月；对象原文存储约 0.12 元/GB/月起，另计请求、流量和保留策略。
    第三方时间戳与合规存证通常按项目/年询价，可先按 1 万–5 万元/年预留评估额度；这些
    仅为采购预算占位，必须由安全管理员、法务和采购根据数据地域、保留年限及服务合同复核。
    """
    principal = get_current_principal(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员可以迁移历史招商证据完整性",
        )
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = migrate_verified_legacy_evidence_integrity(
            ledger_dir,
            expected_legacy_ledger_sha256=body.expected_legacy_ledger_sha256,
            verification_note=body.verification_note,
            authenticated_admin_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存历史招商证据完整性迁移结果",
        ) from exc


@router.post("/reseal-empty-ledger", status_code=status.HTTP_200_OK)
async def reseal_xuzhou_empty_ledger_integrity(
    body: ResealEmptyLedgerIntegrityRequest,
    request: Request,
) -> dict[str, Any]:
    """仅管理员可恢复签名不匹配的空台账，不能触及已有招商事实或证据链。

    输入是当前文件 SHA-256、核验说明和已认证请求；输出为服务器追加的恢复审计。函数先
    强制 ``principal.is_admin``，再由平台校验工作区归属，操作者只使用 ``principal.user_id``；
    请求体没有用户 ID、密钥或证据字段。核心服务只接受没有 leads/evidence 的当前空链，
    在保存锁内再次核对 SHA-256 后重新签整本台账；它拒绝签名已正常的台账、缺失/异常的
    证据链及一切含正式事实的数据，因此不能被普通业务操作、模型或前端用作篡改入口。
    """
    principal = get_current_principal(request)
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员可以恢复空招商台账的完整性签名",
        )
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = reseal_verified_empty_ledger_integrity(
            ledger_dir,
            expected_ledger_sha256=body.expected_ledger_sha256,
            verification_note=body.verification_note,
            authenticated_admin_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法恢复空招商台账的完整性签名",
        ) from exc
