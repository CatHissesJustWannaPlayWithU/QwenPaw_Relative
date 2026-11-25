"""招商评分规则的已认证 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .scoring_rules import (
    create_score_snapshot,
    create_scoring_rule_set,
    get_active_scoring_rule_set,
    get_scoring_adjustment_suggestions,
    record_outcome_feedback,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ScoringDimensionRequest(BaseModel):
    """客户端可提交的一项正向评分维度，不包含可信身份或证据正文。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=100)
    weight: int
    description: str = Field(..., min_length=1, max_length=1_000)
    enabled: bool


class ScoringPenaltyRuleRequest(BaseModel):
    """客户端可提交的一项降权规则，不允许夹带线索或审核人字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=100)
    score_delta: int
    description: str = Field(..., min_length=1, max_length=1_000)
    enabled: bool


class CreateScoringRuleSetRequest(BaseModel):
    """创建新版招商评分规则允许提交的业务字段。

    请求只接收完整维度、降权规则和业务变更原因；``extra="forbid"`` 会拒绝用户
    ID、规则版本、状态、证据及评分结果等字段。版本号和创建人必须由服务器确定，
    防止普通业务请求伪造操作者或覆盖历史规则。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dimensions: list[ScoringDimensionRequest] = Field(..., min_length=1)
    penalty_rules: list[ScoringPenaltyRuleRequest]
    change_reason: str = Field(..., min_length=1, max_length=2_000)


class RecordOutcomeFeedbackRequest(BaseModel):
    """录入招商结果反馈允许提交的业务字段，不包含可信身份、分数或证据。

    客户端只能选择服务器已生成的评分快照、结果枚举和备注。``extra="forbid"`` 会拒绝
    ``recorded_by_user_id``、线索状态、规则权重、证据和快照组件，防止用户借学习反馈
    冒充操作人、修改原始依据或让系统直接更新权重。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    score_snapshot_id: str = Field(..., min_length=1, max_length=100)
    outcome: str = Field(..., min_length=1, max_length=100)
    note: str = Field(..., min_length=1, max_length=2_000)


@router.get("/active", status_code=status.HTTP_200_OK)
async def get_active_xuzhou_scoring_rule_set(request: Request) -> dict[str, Any]:
    """读取当前登录用户所拥有智能体工作区中的生效招商评分规则。

    输入是 HTTP 请求，输出是当前规则版本及台账路径。函数从认证中间件获取当前
    ``Principal`` 并通过工作区归属校验；普通用户可查看业务规则，不需要管理员角色。
    读取不修改线索、证据、规则版本或评分结果。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        rule_set = get_active_scoring_rule_set(ledger_dir)
        return {"ok": True, "rule_set": rule_set}
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取招商评分规则",
        ) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_xuzhou_scoring_rule_set(
    body: CreateScoringRuleSetRequest,
    request: Request,
) -> dict[str, Any]:
    """由普通已登录用户创建其智能体工作区的新版招商评分规则。

    输入是经结构限制的完整规则和变更原因，输出是新规则版本及台账路径。操作者
    只能来自认证中间件提供的 ``principal.user_id``，不读取任何客户端用户 ID；平台
    同时校验工作区归属。核心服务只替代规则版本并保留旧版本，且校验证据、评分历史
    和结果反馈不变，因此业务权重调整不会篡改既有招商判断依据。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = create_scoring_rule_set(
            ledger_dir,
            dimensions=[dimension.model_dump() for dimension in body.dimensions],
            penalty_rules=[rule.model_dump() for rule in body.penalty_rules],
            change_reason=body.change_reason,
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
            detail="无法保存招商评分规则",
        ) from exc


@router.post("/leads/{lead_id}/snapshots", status_code=status.HTTP_201_CREATED)
async def create_xuzhou_score_snapshot(lead_id: str, request: Request) -> dict[str, Any]:
    """由任一已登录用户按当前规则为其工作区线索生成可追溯评分。

    输入只有路径中的 lead ID；输出为评分快照、引用证据 ID 和台账路径。用户身份只来自
    认证中间件，平台同时校验工作区归属；客户端不能提交总分、维度、规则版本或操作者。
    核心函数只追加快照，不改变线索状态、人工复核、原始证据、规则权重或招商推进。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = create_score_snapshot(
            ledger_dir,
            lead_id=lead_id,
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
            detail="无法保存招商评分快照",
        ) from exc


@router.post("/leads/{lead_id}/outcome-feedback", status_code=status.HTTP_201_CREATED)
async def record_xuzhou_outcome_feedback(
    lead_id: str,
    body: RecordOutcomeFeedbackRequest,
    request: Request,
) -> dict[str, Any]:
    """由任一已登录用户为既有评分追加真实招商结果，供后续人工审查权重建议。

    输入是线索 ID、已生成的评分快照、结果和备注；输出为追加式反馈审计。认证主体是唯一
    可信操作人来源，前端或模型没有可传入的用户 ID。接口不接受规则权重、证据、状态或
    总分，也不自动学习、改写 Skill、推进线索或覆盖原始证据；它只把结果交给只读建议服务。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = record_outcome_feedback(
            ledger_dir,
            lead_id=lead_id,
            score_snapshot_id=body.score_snapshot_id,
            outcome=body.outcome,
            note=body.note,
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
            detail="无法保存招商结果反馈",
        ) from exc


@router.get("/adjustment-suggestions", status_code=status.HTTP_200_OK)
async def get_xuzhou_scoring_adjustment_suggestions(request: Request) -> dict[str, Any]:
    """读取基于最终招商结果的权重调整建议，不写规则或触发自动学习。

    输入是当前认证请求，输出为样本量、高低分组的转化统计及建议方向。任何普通登录用户
    均可查看自己工作区的业务建议；路由不接收用户 ID、规则或证据字段，不能借查询越权
    或修改数据。建议仍须人工经既有规则版本接口确认，函数不会自动调整权重或 Skill。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {
            "ok": True,
            **get_scoring_adjustment_suggestions(ledger_dir),
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
            detail="无法读取招商评分调整建议",
        ) from exc
