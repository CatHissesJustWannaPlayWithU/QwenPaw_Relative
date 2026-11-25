"""可解释自适应学习的已认证业务接口。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .adaptive_learning import (
    get_adaptive_learning_status,
    record_human_material_source_outcome,
    set_adaptive_learning_policy,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class SetAdaptiveLearningPolicyRequest(BaseModel):
    """浏览器仅能选择受控自动化边界和说明，不能提交权重、来源、URL、身份或证据。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal[
        "manual_advice",
        "adaptive_ranking",
        "adaptive_ranking_and_recipe_order",
    ]
    change_reason: str = Field(..., min_length=1, max_length=2_000)


class RecordHumanMaterialOutcomeRequest(BaseModel):
    """人工材料线索的真实结果；仅供来源学习，不接收评分、证据或身份字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outcome: Literal[
        "landed",
        "positive_progress",
        "deferred",
        "not_converted",
        "invalidated",
    ]
    note: str = Field(..., min_length=1, max_length=2_000)


@router.get("", status_code=status.HTTP_200_OK)
async def get_xuzhou_adaptive_learning_status(request: Request) -> dict[str, Any]:
    """读取机器学到了什么、如何应用和哪些边界仍由人控制；查询不触发学习。"""
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        return {"ok": True, **get_adaptive_learning_status(ledger_dir)}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法读取自适应学习状态") from exc


@router.post("/policy", status_code=status.HTTP_200_OK)
async def set_xuzhou_adaptive_learning_policy(
    body: SetAdaptiveLearningPolicyRequest,
    request: Request,
) -> dict[str, Any]:
    """让已授权团队成员选择机器可自动做的有限事项，随时可改回人工模式。"""
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = set_adaptive_learning_policy(
            ledger_dir,
            mode=body.mode,
            change_reason=body.change_reason,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法保存自适应学习策略") from exc


@router.post("/leads/{lead_id}/human-material-outcome", status_code=status.HTTP_201_CREATED)
async def record_xuzhou_human_material_source_outcome(
    lead_id: str,
    body: RecordHumanMaterialOutcomeRequest,
    request: Request,
) -> dict[str, Any]:
    """追加人工材料线索的实际业务结果，供来源排序学习而非机器评分。

    认证成员可记录真实推进、落地、暂缓、未转化或无效结果。服务器会验证该线索确实
    含人工授权材料，并只追加审计反馈；不会要求或生成评分快照，不会改变线索状态、
    证据内容、来源目录或自动化策略。
    """

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = record_human_material_source_outcome(
            ledger_dir,
            lead_id=lead_id,
            outcome=body.outcome,
            note=body.note,
            authenticated_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="无法保存人工材料结果反馈") from exc
