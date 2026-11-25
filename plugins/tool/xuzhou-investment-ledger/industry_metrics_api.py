"""徐州招商经营指标草稿的已认证普通用户复核 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .industry_metrics import review_industry_metric_draft
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ReviewIndustryMetricDraftRequest(BaseModel):
    """指标草稿复核唯一允许客户端提交的业务字段。

    输入只包含确认、待补证或无效决定和备注。``extra=forbid`` 会拒绝 ``reviewer_user_id``、
    ``authenticated_user_id``、金额、年份、实体、来源 URL、摘录、证据 ID 和评分等字段；
    因而浏览器或模型无法伪造操作者，或借复核接口改写原始证据链和自动抽取的指标内容。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.post("/drafts/{metric_draft_id}/review", status_code=status.HTTP_200_OK)
async def review_xuzhou_industry_metric_draft(
    metric_draft_id: str,
    body: ReviewIndustryMetricDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """由任一已登录用户复核当前工作区的一条经营指标草稿。

    输入是服务器草稿 ID、受限业务决定、备注和认证请求；输出为追加审计及确认后的派生指标。
    函数先从认证中间件获取 ``principal.user_id``，再由平台校验工作区归属，普通用户无需
    管理员角色。请求体不接收操作者 ID、金额、年份、来源或证据字段，核心函数只从已签名
    草稿读取这些事实，并在保存前验证 evidence、线索及草稿本体不变；因此接口不能改写
    URL、原始摘录、采集时间、证据签名、评分依据或招商状态。
    """

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_industry_metric_draft(
            ledger_dir,
            metric_draft_id=metric_draft_id,
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
            detail="无法保存经营指标草稿复核审计",
        ) from exc
