"""协同投资信号草稿的已认证普通用户 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .co_investment_signal import (
    list_co_investment_signal_drafts,
    review_co_investment_signal_draft,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ReviewCoInvestmentSignalDraftRequest(BaseModel):
    """协同投资信号复核唯一允许客户端提交的业务字段。

    请求仅含受控结论和备注。``extra=forbid`` 会拒绝 ``reviewer_user_id``、个人姓名、资产、
    联系方式、出资金额、资金承诺、证据 ID、URL、摘录、评分和线索状态；操作人只来自服务
    器认证主体。普通用户可以作业务判断，但不能借接口创建资金池、金融招揽或改写证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.get("", status_code=status.HTTP_200_OK)
async def list_xuzhou_co_investment_signal_drafts(
    request: Request,
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """由任一登录用户只读查看当前工作区的组织级协同投资信号草稿。

    输入为认证请求和可选线索/状态筛选；输出是仅能回链既有正式证据的草稿与审计。认证只
    校验当前工作区归属，不要求管理员；接口不生成草稿、不联网、不联系企业或个人、不处理
    资金与个人财务，也不写 URL、摘录、采集时间、评分、线索或原始 evidence。
    """

    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = list_co_investment_signal_drafts(
            ledger_dir,
            lead_id=lead_id,
            review_state=review_state,
            limit=limit,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取协同投资信号草稿",
        ) from exc


@router.post("/{co_investment_draft_id}/review", status_code=status.HTTP_200_OK)
async def review_xuzhou_co_investment_signal_draft(
    co_investment_draft_id: str,
    body: ReviewCoInvestmentSignalDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户追加协同投资信号判断，身份只来自服务器认证主体。

    输入为服务器草稿 ID、有限业务结论、备注和认证请求；输出为追加审计与派生状态。函数先
    获取 ``principal.user_id``，再校验工作区归属，普通用户无需管理员权限；请求体不接受
    操作者 ID 或任何资金/证据事实。核心层只追加审计并保护草稿和 evidence，因此该接口不
    会发起联系、撮合、募资、自动推进线索，或改写来源 URL、原始摘录、采集时间和评分。
    """

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_co_investment_signal_draft(
            ledger_dir,
            co_investment_draft_id=co_investment_draft_id,
            decision=body.decision,
            review_note=body.review_note,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存协同投资信号草稿复核审计",
        ) from exc
