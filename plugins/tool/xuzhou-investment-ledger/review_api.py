"""徐州招商线索人工复核的已认证 HTTP 接口。"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw.app.principal import get_current_principal

from .candidate_review import review_discovery_candidate
from .core import review_investment_lead
from .lead_followup import approve_lead_for_followup
from .shared_workspace import shared_ledger_dir_for_request


router = APIRouter()


class ReviewInvestmentLeadRequest(BaseModel):
    """人工复核接口允许客户端提交的业务字段。

    输入只有复核结论和备注，输出由路由返回更新后的线索。``extra="forbid"`` 明确
    拒绝 ``reviewer_user_id``、证据和评分等额外字段：操作者只能来自服务端认证
    的当前登录身份，原始证据链也不能通过本请求被覆盖。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)


class EnterpriseOriginRequest(BaseModel):
    """人工核验的企业来源地，不把新闻域名当作企业所在地。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    origin: str = Field(..., min_length=1, max_length=100)
    origin_kind: Literal["registered_location", "headquarters", "operation_site", "unknown"]
    evidence_basis: str = Field(..., min_length=1, max_length=2_000)
    china_business_confirmed: Literal[True]


class ReviewDiscoveryCandidateRequest(BaseModel):
    """发现候选业务判断接口允许的最小客户端字段。

    常规决定只接收受控结论和备注。选择“无需徐州字段”时必须额外提交一个经人工核对的
    企业来源地及其依据；它是独立审计，不是客户端补写正式证据。``extra=\"forbid\"`` 拒绝
    操作者、URL、页面摘要、快照、证据和评分字段。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(..., min_length=1, max_length=100)
    review_note: str = Field(..., min_length=1, max_length=2_000)
    enterprise_origin: EnterpriseOriginRequest | None = None


class ApproveLeadForFollowupRequest(BaseModel):
    """招商跟进审批卡唯一允许浏览器提交的人工业务字段。

    这不是证据录入接口：它拒绝 URL、正文、摘录、企业事实、评分、外部联系方式和用户 ID。
    主体、证据和操作者分别从服务器已有台账和当前认证会话取得。六个 ``Literal[True]``
    检查项确保业务人员逐项确认，而不是只写“同意推进”就绕过招商价值判断。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    identity_and_subject_checked: Literal[True]
    formal_evidence_checked: Literal[True]
    industry_fit_checked: Literal[True]
    geographic_basis_checked: Literal[True]
    key_claim_corroborated: Literal[True]
    risk_and_conflict_checked: Literal[True]
    business_value_reason: str = Field(..., min_length=1, max_length=2_000)
    next_action: Literal[
        "official_information_check",
        "internal_industry_matching",
        "prepare_human_outreach",
        "assign_followup_research",
        "other_human_action",
    ]
    next_action_detail: str = Field(..., min_length=1, max_length=2_000)
    owner_name: str = Field(..., min_length=1, max_length=100)
    target_date: date
    review_note: str = Field(..., min_length=1, max_length=2_000)


@router.post("/leads/{lead_id}/review", status_code=status.HTTP_200_OK)
async def review_xuzhou_investment_lead(
    lead_id: str,
    body: ReviewInvestmentLeadRequest,
    request: Request,
) -> dict[str, Any]:
    """由任一已获名册授权的登录用户复核团队共享台账中的一条招商线索。

    输入是路径中的线索 ID、受限的业务结论和复核备注；输出包含更新后的线索与台账
    路径。函数首先从认证中间件读取 ``Principal``，再经共享目录守卫确认其为管理员或
    名册成员；全团队操作同一份共享台账，普通成员不需要管理员角色，非成员一律 403。
    客户端没有操作者 ID 字段，函数只把认证后的 ``principal.user_id`` 传给核心审计逻辑；
    核心函数只更新业务状态和审计记录，并校验证据链不变，因此本接口不能改写来源、
    原始摘录、采集时间或评分依据。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)

    try:
        result = review_investment_lead(
            ledger_dir,
            lead_id=lead_id,
            decision=body.decision,
            review_note=body.review_note,
            reviewer_user_id=principal.user_id,
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
            detail="无法保存招商线索复核结果",
        ) from exc


@router.post("/leads/{lead_id}/followup-approval", status_code=status.HTTP_200_OK)
async def approve_xuzhou_lead_for_followup(
    lead_id: str,
    body: ApproveLeadForFollowupRequest,
    request: Request,
) -> dict[str, Any]:
    """由团队已授权成员确认一条正式证据线索可进入人工招商跟进池。

    机器只确认台账中已有正式证据并记录审计；用户必须在审批卡中确认主体、事实、产业、
    地域、交叉核验和风险处理，同时写明招商价值、负责人、下一步和日期。成功不发送
    钉钉、不联系企业、不承诺政策，也不代表企业已同意来徐州；它只把团队内部线索标为
    可跟进。操作者身份完全来自当前 ``Principal``，请求体无法伪造。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = approve_lead_for_followup(
            ledger_dir,
            lead_id=lead_id,
            checklist={
                "identity_and_subject_checked": body.identity_and_subject_checked,
                "formal_evidence_checked": body.formal_evidence_checked,
                "industry_fit_checked": body.industry_fit_checked,
                "geographic_basis_checked": body.geographic_basis_checked,
                "key_claim_corroborated": body.key_claim_corroborated,
                "risk_and_conflict_checked": body.risk_and_conflict_checked,
            },
            business_value_reason=body.business_value_reason,
            next_action=body.next_action,
            next_action_detail=body.next_action_detail,
            owner_name=body.owner_name,
            target_date=body.target_date,
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
            detail="无法保存招商跟进审批卡",
        ) from exc


@router.post("/discovery-candidates/{candidate_id}/business-review", status_code=status.HTTP_200_OK)
async def review_xuzhou_discovery_candidate(
    candidate_id: str,
    body: ReviewDiscoveryCandidateRequest,
    request: Request,
) -> dict[str, Any]:
    """由普通登录用户处置刚发现候选的待补证、观察或无效状态。

    输入是服务器候选 ID、受限业务决定和备注；输出包含新增审计和候选摘要。用户身份仅来自
    认证中间件的 Principal，客户端不能提交 reviewer_user_id；核心逻辑只追加审计并验证
    候选 URL、摘要、发现时间、快照、正式 evidence 和线索均不变，因此该接口不能把候选
    页面伪造成招商事实或覆盖原始证据链。
    """
    principal = get_current_principal(request)
    ledger_dir = shared_ledger_dir_for_request(request)
    try:
        result = review_discovery_candidate(
            ledger_dir,
            candidate_id=candidate_id,
            decision=body.decision,
            review_note=body.review_note,
            authenticated_user_id=principal.user_id,
            enterprise_origin=(body.enterprise_origin.model_dump() if body.enterprise_origin is not None else None),
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
            detail="无法保存发现候选业务判断",
        ) from exc
