"""来源配方学习建议的已认证普通用户接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .source_recipe_governance import (
    get_active_source_recipe_rule_set,
    list_source_recipe_learning_proposals,
    review_source_recipe_learning_proposal,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ReviewSourceRecipeLearningProposalRequest(BaseModel):
    """普通用户审批来源学习建议时唯一允许提交的业务字段。

    请求体只允许批准/拒绝决定和业务备注；建议顺序、规则版本、证据统计、审批时间及
    操作者均由服务器读取或生成。``extra="forbid"`` 会拒绝 ``reviewed_by_user_id``、
    URL、关键词、行业、权重、证据或 Skill 字段，防止客户端借审批接口伪造身份、扩大
    监控范围或改写原始证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=40)
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.get("/active", status_code=status.HTTP_200_OK)
async def get_active_xuzhou_source_recipe_rule_set(request: Request) -> dict[str, Any]:
    """读取当前工作区的生效来源配方顺序，不触发学习或更改搜索配置。

    输入是认证请求，输出为当前规则版本和完整固定配方顺序。调用方身份与工作区归属
    均由平台验证，普通登录用户无需管理员角色；接口不接收排序、关键词、URL、证据或
    用户 ID，因此查询不能越权影响后续监控，也不读写线索、证据、评分或复核。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, **get_active_source_recipe_rule_set(ledger_dir)}
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
            detail="无法读取来源配方规则",
        ) from exc


@router.get("/learning-proposals", status_code=status.HTTP_200_OK)
async def list_xuzhou_source_recipe_learning_proposals(request: Request) -> dict[str, Any]:
    """列出当前工作区自动生成的待审批来源学习建议，保持完全只读。

    输入是认证请求，输出为建议原文和由只追加审计推导的审批状态；服务器不允许客户端
    提交建议、来源 URL、关键词或身份。任一登录用户可查看本工作区业务建议，但读取
    不会运行搜索、更新 Skill、写入规则、线索、证据、评分或人工状态。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {
            "ok": True,
            **list_source_recipe_learning_proposals(ledger_dir),
        }
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
            detail="无法读取来源学习建议",
        ) from exc


@router.post(
    "/learning-proposals/{proposal_id}/review",
    status_code=status.HTTP_200_OK,
)
async def review_xuzhou_source_recipe_learning_proposal(
    proposal_id: str,
    body: ReviewSourceRecipeLearningProposalRequest,
    request: Request,
) -> dict[str, Any]:
    """由普通登录用户批准或拒绝一条服务器已有的来源学习建议。

    输入是路径中的服务器 proposal ID、受控决定和备注；输出为追加式审批审计，并在批准
    时返回新的规则版本。操作者只从认证中间件的 ``principal.user_id`` 取得，客户端无法
    自称审批人或提交配方顺序；平台还验证工作区归属。核心服务只重排完整固定配方，绝不
    改写网页关键词、产业范围、Skill、原始证据、线索、评分、预警或既有人工复核。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_source_recipe_learning_proposal(
            ledger_dir,
            proposal_id=proposal_id,
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
            detail="无法保存来源学习审批结果",
        ) from exc
