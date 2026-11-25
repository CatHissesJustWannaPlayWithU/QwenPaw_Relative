"""人工授权材料通道的已认证 HTTP 接口。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .human_evidence import (
    ACCESS_MODES,
    ALLOWED_ATTACHMENT_MIME_TYPES,
    list_human_evidence_submissions,
    record_human_attested_evidence,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class HumanEvidenceAttachmentRequest(BaseModel):
    """可选的关键截图、PDF 或纯文本导出；浏览器不能提交路径或哈希。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file_name: str = Field(..., min_length=1, max_length=180)
    mime_type: Literal[
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    ]
    content_base64: str = Field(..., min_length=1, max_length=720_000)


class CreateHumanEvidenceRequest(BaseModel):
    """业务人员能提交的最小材料字段，身份、时间、哈希和签名均由服务器生成。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_name: str = Field(..., min_length=1, max_length=500)
    entity_kind: str = Field(..., min_length=1, max_length=100)
    sector: str = Field(..., min_length=1, max_length=100)
    source_url: str = Field(..., min_length=1, max_length=2_000)
    source_title: str = Field(..., min_length=1, max_length=500)
    source_published_at: str | None = Field(default=None, max_length=30)
    source_level: str = Field(..., min_length=1, max_length=100)
    claim_type: str = Field(..., min_length=1, max_length=100)
    evidence_quality: int
    excerpt: str = Field(..., min_length=1, max_length=2_000)
    material_text: str | None = Field(default=None, max_length=12_000)
    access_mode: str = Field(..., min_length=1, max_length=100)
    attestation_confirmed: Literal[True]
    xuzhou_association_tags: list[str] = Field(default_factory=list, max_length=20)
    attachment: HumanEvidenceAttachmentRequest | None = None


@router.get("", status_code=status.HTTP_200_OK)
async def list_xuzhou_human_evidence(request: Request, limit: int = 100) -> dict[str, Any]:
    """读取团队内人工材料摘要，任何成员均可查看但不能获得附件原文或存储路径。"""
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, **list_human_evidence_submissions(ledger_dir, limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法读取人工材料台账") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_xuzhou_human_evidence(
    body: CreateHumanEvidenceRequest,
    request: Request,
) -> dict[str, Any]:
    """以当前认证业务人员身份登记其有权使用的材料。

    此接口不接受用户 ID、创建时间、内容哈希、证据 ID、线索 ID、快照路径、签名、Cookie
    或密码。人工材料会被明示为人工提交，服务器不会声称已读取登录页或绕过网页限制。
    """
    if body.access_mode not in ACCESS_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="access_mode不是允许的人工访问情形")
    if body.attachment is not None and body.attachment.mime_type not in ALLOWED_ATTACHMENT_MIME_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="attachment.mime_type不受支持")
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = record_human_attested_evidence(
            ledger_dir,
            entity_name=body.entity_name,
            entity_kind=body.entity_kind,
            sector=body.sector,
            source_url=body.source_url,
            source_title=body.source_title,
            source_published_at=body.source_published_at,
            source_level=body.source_level,
            claim_type=body.claim_type,
            evidence_quality=body.evidence_quality,
            excerpt=body.excerpt,
            material_text=body.material_text,
            access_mode=body.access_mode,
            attestation_confirmed=body.attestation_confirmed,
            xuzhou_association_tags=body.xuzhou_association_tags,
            attachment=(body.attachment.model_dump() if body.attachment is not None else None),
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法保存人工材料及其证据快照") from exc
