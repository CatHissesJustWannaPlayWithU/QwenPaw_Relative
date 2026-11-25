"""徐州招商证据草稿确认的已认证普通用户 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .evidence_draft_business_review import review_incomplete_evidence_draft
from .evidence_draft_confirmation import confirm_evidence_draft
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ConfirmEvidenceDraftRequest(BaseModel):
    """确认草稿时客户端唯一可提交的业务字段。

    请求只接收人工复核备注。``extra=forbid`` 会拒绝 ``reviewer_user_id``、实体、组织类型、
    来源 URL、日期、摘录、哈希、评分和状态等字段；操作者仅来自服务器认证身份，草稿事实
    字段仅从已签名台账读取，不能由模型或前端在确认时替换。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    review_note: str = Field(..., min_length=1, max_length=2_000)


class ReviewIncompleteEvidenceDraftRequest(BaseModel):
    """信息不完整草稿的业务判断接口允许的唯一客户端字段。

    请求只接收待补证、观察或无效决定及其备注。请求模型禁止用户 ID、草稿内容、来源 URL、
    日期、摘录、哈希、评分、线索状态等字段，确保用户只能追加业务判断，不能补造事实或
    覆盖自动抽取草稿与原始证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.post("/{draft_id}/confirm", status_code=status.HTTP_200_OK)
async def confirm_xuzhou_evidence_draft(
    draft_id: str,
    body: ConfirmEvidenceDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """由任一已登录用户确认其工作区草稿，服务器重抓核验后才可能新增正式证据。

    输入为服务器草稿 ID、复核备注及认证请求；输出是重抓结果和确认审计。函数先从认证
    中间件取得 ``principal.user_id``，再由平台校验工作区归属，普通用户不要求管理员角色。
    请求体不能携带用户 ID、实体、URL、日期、摘录或证据字段；核心服务只从已签名草稿读取
    这些值，并按服务器候选 URL 重新抓取、核验与签名。确认失败不会新增证据或线索，确认
    审计也不能修改草稿原字段、候选快照或既有证据链。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = confirm_evidence_draft(
            ledger_dir,
            draft_id=draft_id,
            review_note=body.review_note,
            authenticated_user_id=principal.user_id,
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
            detail="无法完成证据草稿确认或保存确认审计",
        ) from exc


@router.post("/{draft_id}/business-review", status_code=status.HTTP_200_OK)
async def review_xuzhou_incomplete_evidence_draft(
    draft_id: str,
    body: ReviewIncompleteEvidenceDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户为不完整草稿追加业务判断，身份只来自服务器认证主体。

    输入是服务器草稿 ID、三种受控业务结论、备注与认证请求；输出为新增判断审计和派生状态。
    函数通过平台校验工作区归属并从 ``principal.user_id`` 取得操作者，普通用户无需管理员。
    请求体不接收实体、URL、日期、摘录、哈希、评分、线索状态或用户 ID；核心只允许关键字段
    缺失的草稿追加待补证/观察/无效审计，既不联网、也不创建线索或证据，更不能修改原始草稿
    与候选快照。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_incomplete_evidence_draft(
            ledger_dir,
            draft_id=draft_id,
            decision=body.decision,
            review_note=body.review_note,
            authenticated_user_id=principal.user_id,
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
            detail="无法保存证据草稿业务判断审计",
        ) from exc
