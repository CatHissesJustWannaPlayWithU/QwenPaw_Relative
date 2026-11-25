"""关键人员关系草稿的已认证普通用户 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .relationship_draft import list_relationship_drafts, review_relationship_draft
from .relationship_graph import register_approved_relationship_draft_to_graph
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ReviewRelationshipDraftRequest(BaseModel):
    """关系草稿复核时客户端唯一允许提交的业务字段。

    请求只包含受控结论与备注。``extra=forbid`` 明确拒绝 ``reviewer_user_id``、草稿内容、
    人员资料、组织名称、角色、证据 ID、URL、原始摘录、采集时间、评分和线索状态；实际
    操作者只取服务器认证主体，核心层仅追加审计。因此普通用户能复核业务，不可能伪造
    身份、覆盖证据链或让“可登记图谱”自动建边。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.get("", status_code=status.HTTP_200_OK)
async def list_xuzhou_relationship_drafts(
    request: Request,
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """由任一登录用户只读查看当前工作区的关键人员关系待复核草稿。

    输入是认证请求及可选线索/状态筛选；输出为草稿、已有审计和图谱登记边界提示。认证只
    校验当前工作区归属，不要求管理员；函数不接收或写入人员资料、证据正文、URL、用户 ID
    或业务状态，不联网、不生成草稿、不登记节点/边，也不会修改任何原始证据链。
    """

    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = list_relationship_drafts(
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
            detail="无法读取关键人员关系草稿",
        ) from exc


@router.post("/{relationship_draft_id}/review", status_code=status.HTTP_200_OK)
async def review_xuzhou_relationship_draft(
    relationship_draft_id: str,
    body: ReviewRelationshipDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户追加关系草稿判断，身份只来自服务端认证主体。

    输入是服务器草稿 ID、受控决定、备注和已认证请求；输出为只追加的审计及当前派生状态。
    平台先验证工作区归属，再将 ``principal.user_id`` 传给核心；没有管理员判断，也绝不
    接受客户端 ``reviewer_user_id``。核心层逐字保护草稿以外的台账，且“可登记图谱”仍需
    用户后续调用图谱接口再次检查原始证据，故本接口不能改 URL、摘录、采集时间、评分、
    线索状态，不能自动建立关系边或作出个人招商结论。
    """

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_relationship_draft(
            ledger_dir,
            relationship_draft_id=relationship_draft_id,
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
            detail="无法保存关键人员关系草稿复核审计",
        ) from exc


@router.post("/{relationship_draft_id}/register-to-graph", status_code=status.HTTP_201_CREATED)
async def register_xuzhou_approved_relationship_draft_to_graph(
    relationship_draft_id: str,
    request: Request,
) -> dict[str, Any]:
    """由普通登录用户明确将“可登记图谱”的草稿转为一条仍待复核的关系边。

    输入只有服务器草稿 ID 与认证请求，输出为本次创建或复用的人员/组织节点和关系边。请求
    没有 body，故客户端无法注入姓名、关系类型、徐州标签、证据 ID、URL、摘录、备注或用户
    ID；身份仅取 ``principal.user_id``。核心服务要求草稿已由人工标记为可登记图谱，并再次
    以原始已签名 evidence 校验双方名称与角色同现。创建的边仍为等待用户复核，绝不自动
    生效、推进线索或推断个人投资/落地意愿，且保存前后保护全部原始证据链。
    """

    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = register_approved_relationship_draft_to_graph(
            ledger_dir,
            relationship_draft_id=relationship_draft_id,
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
            detail="无法将关键人员关系草稿登记到图谱",
        ) from exc
