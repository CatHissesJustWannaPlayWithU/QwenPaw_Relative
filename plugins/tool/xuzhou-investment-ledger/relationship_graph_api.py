"""徐州招商证据化关系图谱的已认证普通用户 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .relationship_graph import (
    append_relationship_edge_evidence_supplement,
    get_relationship_graph,
    register_relationship_edge,
    register_relationship_node,
    review_relationship_edge,
)
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class RegisterRelationshipNodeRequest(BaseModel):
    """普通用户登记图谱节点时唯一允许提交的业务字段。

    请求模型禁止节点 ID、创建时间、创建人、操作范围、审计和任何原始证据字段；用户
    只能引用服务器已保存的 ``evidence_ids``。关键人员节点仅限公开姓名和宽泛徐州
    关联分类；本模型的固定字段配合 ``extra=forbid`` 不接受电话、住址、资产、募资
    能力或客户端用户 ID。这样前端或模型不能伪造操作者，也不能通过图谱接口替换来源
    URL、原始摘录、采集时间、内容哈希或评分依据。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(..., min_length=1, max_length=300)
    node_kind: str = Field(..., min_length=1, max_length=100)
    locality: str = Field(..., min_length=1, max_length=100)
    linked_lead_id: str | None = Field(default=None, min_length=1, max_length=100)
    evidence_ids: list[str] = Field(..., min_length=1, max_length=20)
    registration_reason: str = Field(..., min_length=1, max_length=2_000)


class RegisterRelationshipEdgeRequest(BaseModel):
    """普通用户登记关系边时唯一允许提交的业务字段。

    关键人员角色由核心层限制为“公开人员 → 已关联招商线索的组织”，本请求不能提交
    个人联系方式、资产、资金来源、招商意愿或任何用户身份字段。这样普通登录用户可
    记录已证据化的法人/股东/高管关系，却不能把图谱变成个人募资或个人信息库。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    from_node_id: str = Field(..., min_length=1, max_length=100)
    to_node_id: str = Field(..., min_length=1, max_length=100)
    relationship_type: str = Field(..., min_length=1, max_length=100)
    evidence_ids: list[str] = Field(..., min_length=1, max_length=20)
    registration_reason: str = Field(..., min_length=1, max_length=2_000)


class ReviewRelationshipEdgeRequest(BaseModel):
    """普通用户复核关系边时允许提交的唯一业务字段。

    请求体不包含操作者、节点、关系类型、证据引用或原始证据字段。操作者只能由服务端
    认证主体提供；复核结论与备注只能追加到关系边历史，不能借接口改写关系事实或证据
    链，因此模型和前端无法伪造责任人或覆盖来源 URL、原始摘录和采集时间。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


class AppendRelationshipEdgeEvidenceSupplementRequest(BaseModel):
    """普通用户为关系边追加补证时允许提交的唯一业务字段。

    请求体只能引用既有证据 ID 并说明补证理由，不允许提交补证 ID、时间、操作者、节点、
    关系类型、原边证据列表或任何来源正文。操作者严格取服务器认证主体，核心服务会把
    新引用保存为独立追加记录，防止前端或模型覆盖原关系及其原始证据链。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_ids: list[str] = Field(..., min_length=1, max_length=20)
    supplement_note: str = Field(..., min_length=1, max_length=2_000)


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
async def register_xuzhou_relationship_node(
    body: RegisterRelationshipNodeRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户登记证据化图谱节点，身份只来自服务器认证主体。

    输入是受限节点业务字段和已认证请求，输出为新增或去重节点。平台先校验请求归属
    的工作区，再从 ``principal.user_id`` 获取操作者；不需要管理员角色，且绝不接受
    客户端自称的用户 ID。核心服务要求引用既有证据并校验写入前后原始证据链及其他
    业务记录不变，因此节点登记不可能改写来源事实或评分依据。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = register_relationship_node(
            ledger_dir,
            display_name=body.display_name,
            node_kind=body.node_kind,
            locality=body.locality,
            linked_lead_id=body.linked_lead_id,
            evidence_ids=body.evidence_ids,
            registration_reason=body.registration_reason,
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
            detail="无法保存关系图谱节点",
        ) from exc


@router.post("/edges", status_code=status.HTTP_201_CREATED)
async def register_xuzhou_relationship_edge(
    body: RegisterRelationshipEdgeRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户登记证据化关系边，原始证据与操作者身份均不可由请求伪造。

    输入是两个既有节点、关系类型、已有证据引用、登记理由和认证请求；输出为新增或
    去重关系边。认证主体而非请求体决定操作者，普通用户即可操作。核心服务要求关系
    至少一端对应招商线索且引用该线索证据，并在保存前后比对所有非图谱记录，故本接口
    不会自动推断关系、推进招商或改写任何 URL、摘录、采集时间、哈希和评分依据。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = register_relationship_edge(
            ledger_dir,
            from_node_id=body.from_node_id,
            to_node_id=body.to_node_id,
            relationship_type=body.relationship_type,
            evidence_ids=body.evidence_ids,
            registration_reason=body.registration_reason,
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
            detail="无法保存关系图谱边",
        ) from exc


@router.post("/edges/{edge_id}/review", status_code=status.HTTP_200_OK)
async def review_xuzhou_relationship_edge(
    edge_id: str,
    body: ReviewRelationshipEdgeRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户复核关系边，身份只取服务器认证结果且不改证据引用。

    输入是关系边 ID、有效/待补证/无效结论、复核备注和已认证请求；输出为新增复核历史
    后的关系边。无需管理员角色，平台会校验当前用户对工作区的归属；``reviewer_user_id``
    不在请求模型中，实际操作者只使用 ``principal.user_id``。核心服务只变更关系边的
    业务状态并追加审计，严格拒绝改写节点、端点、关系类型、证据 ID、URL、摘录、采集
    时间、内容哈希、评分、线索状态或自动招商推进。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_relationship_edge(
            ledger_dir,
            edge_id=edge_id,
            decision=body.decision,
            review_note=body.review_note,
            reviewer_user_id=principal.user_id,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法保存关系图谱复核",
        ) from exc


@router.post("/edges/{edge_id}/evidence-supplements", status_code=status.HTTP_201_CREATED)
async def append_xuzhou_relationship_edge_evidence_supplement(
    edge_id: str,
    body: AppendRelationshipEdgeEvidenceSupplementRequest,
    request: Request,
) -> dict[str, Any]:
    """普通登录用户为关系边追加既有证据引用，永不覆盖原边或原始证据。

    输入是服务器生成的关系边 ID、既有证据 ID 列表、补证理由和认证请求；输出独立的
    补证记录。普通用户无需管理员角色，工作区归属由平台校验；操作者仅使用
    ``principal.user_id``，请求模型不允许用户 ID。核心服务会拒绝重复引用与无关线索
    证据，并只追加补证及审计，绝不修改关系端点、类型、原边证据、URL、摘录、采集
    时间、哈希、评分、线索状态或招商推进结论。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = append_relationship_edge_evidence_supplement(
            ledger_dir,
            edge_id=edge_id,
            evidence_ids=body.evidence_ids,
            supplement_note=body.supplement_note,
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
            detail="无法保存关系边补证记录",
        ) from exc


@router.get("", status_code=status.HTTP_200_OK)
async def get_xuzhou_relationship_graph(
    request: Request,
    lead_id: str | None = None,
    review_state: str | None = None,
) -> dict[str, Any]:
    """读取当前工作区图谱，可按线索和关系复核状态筛选并返回汇总，不修改业务数据。

    输入为已认证请求、可选既有线索 ID 与可选关系边复核状态；输出节点、边、审计、
    证据引用 ID 及当前查询范围的状态汇总。所有登录用户均可读取其已获平台工作区授权
    的数据；函数不接收用户 ID、证据正文或写入字段，不联网、不重算、不自动建立关系，
    也不会改变线索、原始证据、评分或复核状态。
    """
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = get_relationship_graph(
            ledger_dir,
            lead_id=lead_id,
            review_state=review_state,
        )
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="无法读取关系图谱",
        ) from exc
