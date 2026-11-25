"""普通登录用户确认证据草稿的受控重抓与追加式审计。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .candidate_review import candidate_recapture_scope_blocker
from .evidence_draft import (
    _evidence_draft_confirmation_audits,
    _evidence_drafts,
    business_review_state_for_draft,
    missing_draft_confirmation_fields,
)
from .source_discovery import capture_discovery_candidate
from .storage import ledger_path, load_ledger, save_ledger


CONFIRMATION_OPERATION_SCOPE = "authenticated_user_confirm_draft_then_server_recapture_only"
_VALID_DRAFT_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")
# 只允许与核心台账完全相同的三类主体进入服务器重抓；此白名单不接受客户端扩展枚举，
# 因而普通用户确认草稿时不能借实体类型字段写入任意业务数据或影响既有证据链。
_VALID_ENTITY_KINDS = frozenset({"enterprise", "fund", "startup_team"})
_VALID_CLAIM_TYPES = frozenset(
    {
        "financing",
        "fund_raising",
        "xuzhou_connection",
        "cooperation",
        "expansion",
        "industry_focus",
        "fund_investment",
        "risk",
    }
)


def _utc_now() -> str:
    """返回确认尝试的服务器 UTC 时间，不接受客户端或草稿指定的操作时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """根据服务器生成的确认审计字段创建稳定 ID，不允许客户端指定审计身份。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验确认服务的有限文本输入，拒绝空白、超长与非字符串而不修改任何台账。"""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    return normalized


def _required_draft_id(value: object) -> str:
    """校验只能使用服务器生成的草稿 ID，阻止路径、URL 或任意对象进入确认流程。"""
    draft_id = _required_text(value, "draft_id", maximum=120)
    if any(character not in _VALID_DRAFT_ID_CHARACTERS for character in draft_id):
        raise ValueError("draft_id必须是有效的服务器草稿 ID")
    return draft_id


def _confirmation_audits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得只追加确认审计列表，格式异常即停止而不重置或删改历史记录。"""
    return _evidence_draft_confirmation_audits(ledger)


def _find_draft(ledger: dict[str, Any], draft_id: str) -> dict[str, Any]:
    """从已签名台账定位草稿；找不到即失败，不能由请求体临时构造证据字段。"""
    draft = next(
        (
            item
            for item in _evidence_drafts(ledger)
            if isinstance(item, dict) and item.get("draft_id") == draft_id
        ),
        None,
    )
    if draft is None:
        raise ValueError("未找到待确认的证据草稿")
    return draft


def _stable_json(value: object) -> str:
    """稳定序列化已签名记录，用于确认流程前后检测是否出现越权改写。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _draft_capture_fields(draft: dict[str, Any]) -> dict[str, object]:
    """从服务器已保存草稿提取既有抓取器所需字段，拒绝不完整草稿。

    输入仅为台账中已签名的草稿，输出是传给受控重抓器的实体、类别、日期与摘录。函数不
    接收客户端的同名字段，也不补全缺失内容；这样普通用户只能确认“用这份草稿重抓核验”，
    不能借确认接口伪造来源事实或覆盖原始观察快照。
    """
    required_text_fields = (
        "candidate_id",
        "entity_name",
        "source_published_at",
        "claim_type",
        "excerpt",
    )
    fields: dict[str, object] = {}
    for field_name in required_text_fields:
        value = draft.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"证据草稿缺少可确认字段：{field_name}")
        fields[field_name] = value
    entity_kind = draft.get("entity_kind")
    if entity_kind not in _VALID_ENTITY_KINDS:
        raise ValueError("证据草稿的entity_kind不可确认")
    if fields["claim_type"] not in _VALID_CLAIM_TYPES:
        raise ValueError("证据草稿的claim_type不可确认")
    fields["entity_kind"] = entity_kind
    return fields


def _historical_evidence_snapshot(ledger: dict[str, Any]) -> list[str]:
    """保存确认前每条正式证据的完整序列化，允许后续只追加而拒绝改写、删除或重排。"""
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    return [_stable_json(record) for record in evidence]


def _assert_historical_evidence_unchanged(before: list[str], ledger: dict[str, Any]) -> None:
    """确认受控抓取只可在证据链末尾追加，不能改写既有来源 URL、摘录或采集时间。"""
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < len(before):
        raise RuntimeError("草稿确认不得删除既有正式证据")
    if [_stable_json(record) for record in evidence[: len(before)]] != before:
        raise RuntimeError("草稿确认不得改写既有正式证据链")


def _protected_draft_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """记录确认操作禁止修改的草稿、原观察、评分与反馈，供保存前逐字校验。"""
    protected_fields = (
        "evidence_drafts",
        "candidate_observations",
        "score_snapshots",
        "outcome_feedback",
    )
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = _stable_json(value)
    return snapshot


def _assert_protected_drafts_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """防止草稿确认借受控抓取流程改写草稿原字段、候选快照、评分或反馈。"""
    if _protected_draft_snapshot(ledger) != before:
        raise RuntimeError("草稿确认不得修改草稿原字段、候选观察、评分或结果反馈")


def _successful_confirmation(audits: list[dict[str, Any]], draft_id: str) -> dict[str, Any] | None:
    """查询已成功的确认审计，阻止同一草稿由重复请求再次发起网页抓取。"""
    return next(
        (
            audit
            for audit in reversed(audits)
            if isinstance(audit, dict)
            and audit.get("draft_id") == draft_id
            and audit.get("new_state") in {"confirmed_and_captured", "confirmed_with_existing_evidence"}
        ),
        None,
    )


def _append_confirmation_audit(
    workspace_dir: Path,
    *,
    draft_id: str,
    candidate_id: str,
    authenticated_user_id: str,
    review_note: str,
    new_state: str,
    capture_attempted: bool,
    capture_result: str,
    evidence_id: str | None,
    protected_before: dict[str, str],
    verification_failure_reason: str | None = None,
    verification_failure_stage: str | None = None,
) -> dict[str, object]:
    """在受控抓取结束后只追加确认审计，并再次校验草稿和原观察未变。

    输入均为服务器已验证的草稿标识、认证身份与抓取结果；输出为新审计和台账路径。函数
    不接收也不更新实体、来源 URL、日期、摘录、哈希、评分或线索状态。它只追加“谁在何时
    以什么备注确认、服务器是否实际入证”的审计，令人工责任可追溯而原始证据链不可覆盖。
    """
    ledger = load_ledger(workspace_dir)
    _find_draft(ledger, draft_id)
    _assert_protected_drafts_unchanged(protected_before, ledger)
    attempted_at = _utc_now()
    audit = {
        "confirmation_id": _stable_id(
            "draft-confirmation",
            {
                "draft_id": draft_id,
                "attempted_at": attempted_at,
                "authenticated_user_id": authenticated_user_id,
                "new_state": new_state,
                "capture_result": capture_result,
            },
        ),
        "draft_id": draft_id,
        "candidate_id": candidate_id,
        "attempted_at": attempted_at,
        "confirmed_by_user_id": authenticated_user_id,
        "review_note": review_note,
        "old_state": "pending_human_confirmation",
        "new_state": new_state,
        "capture_attempted": capture_attempted,
        "capture_result": capture_result,
        "evidence_id": evidence_id,
        "operation_scope": CONFIRMATION_OPERATION_SCOPE,
    }
    # These optional fields explain a failed server verification to the same
    # authenticated team that can see the candidate.  They copy only the
    # bounded server reason, never the page body, local path or request data.
    if verification_failure_reason is not None:
        audit["verification_failure_reason"] = verification_failure_reason[:500]
    if verification_failure_stage is not None:
        audit["verification_failure_stage"] = verification_failure_stage[:100]
    _confirmation_audits(ledger).append(audit)
    _assert_protected_drafts_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"audit": audit, "ledger_path": str(saved_path)}


def confirm_evidence_draft(
    workspace_dir: Path,
    *,
    draft_id: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由已认证普通用户确认草稿后重新抓取页面，核验成功才追加正式证据。

    输入是服务器生成草稿 ID、复核备注和认证中间件提供的用户 ID；输出为受控抓取结果与
    追加确认审计。函数绝不接收客户端提供的实体、类别、来源 URL、发布日期、摘录、哈希
    或操作者身份：这些字段只能从已签名草稿读取，页面则必须由既有抓取器根据服务器候选
    URL 重新访问并核验。成功时仅允许在正式证据链尾部新增一条已签名证据；失败时不得新增
    证据或线索。无论结果如何，草稿原字段与候选原始观察快照都逐字保持不变，确认责任只
    写入独立追加审计，普通用户不需要管理员角色。
    """
    normalized_draft_id = _required_draft_id(draft_id)
    normalized_note = _required_text(review_note, "review_note", maximum=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id, "authenticated_user_id", maximum=100
    )
    before_ledger = load_ledger(workspace_dir)
    draft = _find_draft(before_ledger, normalized_draft_id)
    if draft.get("state") != "pending_human_confirmation":
        raise ValueError("该草稿不是等待人工确认状态")
    candidate_id = draft.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("证据草稿缺少有效候选 ID")
    already_confirmed = _successful_confirmation(
        _confirmation_audits(before_ledger), normalized_draft_id
    )
    if already_confirmed is not None:
        return {
            "confirmed": False,
            "message": "该草稿已经成功确认，未重复抓取或追加证据",
            "audit": already_confirmed,
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    # “无效”是普通用户已经给出的业务结论；保留其审计并停止网络访问，避免同一草稿被
    # 后续请求反复抓取。待补证和观察仍可在字段完善后由用户主动发起服务器重新核验。
    if business_review_state_for_draft(before_ledger, draft_id=normalized_draft_id) == "invalid":
        raise ValueError("该草稿已被业务判断为无效，不能再次触发服务器重新抓取")
    protected_before = _protected_draft_snapshot(before_ledger)
    evidence_before = _historical_evidence_snapshot(before_ledger)
    leads_before = _stable_json(before_ledger.get("leads"))
    missing_fields = missing_draft_confirmation_fields(draft)
    preflight_blockers: list[str] = []
    try:
        capture_fields = _draft_capture_fields(draft)
    except ValueError as exc:
        capture_fields = None
        preflight_blockers.append(str(exc))
    scope_blocker = candidate_recapture_scope_blocker(
        before_ledger, candidate_id=candidate_id
    )
    if scope_blocker is not None:
        preflight_blockers.append(scope_blocker)
    if preflight_blockers:
        audit_result = _append_confirmation_audit(
            workspace_dir,
            draft_id=normalized_draft_id,
            candidate_id=candidate_id,
            authenticated_user_id=normalized_user_id,
            review_note=normalized_note,
            new_state="confirmation_rejected_incomplete_draft",
            capture_attempted=False,
            capture_result="draft_fields_incomplete",
            evidence_id=None,
            protected_before=protected_before,
            verification_failure_reason="；".join(preflight_blockers),
            verification_failure_stage="preflight_before_server_recapture",
        )
        return {
            "confirmed": False,
            "message": "服务器未访问原页面：" + "；".join(preflight_blockers),
            "preflight": {
                "passed": False,
                "capture_started": False,
                "missing_confirmation_fields": missing_fields,
                "requires_china_business_verification": scope_blocker is not None,
                "blockers": preflight_blockers,
            },
            **audit_result,
        }

    if capture_fields is None:
        raise RuntimeError("证据草稿预检查异常")

    capture = capture_discovery_candidate(
        workspace_dir,
        candidate_id=str(capture_fields["candidate_id"]),
        entity_name=str(capture_fields["entity_name"]),
        entity_kind=str(capture_fields["entity_kind"]),
        claim_type=str(capture_fields["claim_type"]),
        source_published_at=str(capture_fields["source_published_at"]),
        excerpt=str(capture_fields["excerpt"]),
        xuzhou_association_tags=None,
        # The confirmation preflight just re-read the append-only review audit.
        # Capture re-validates it again before network access, so this flag is
        # not a client-controlled bypass.
        allow_reviewed_xuzhou_waiver=True,
    )
    after_capture = load_ledger(workspace_dir)
    _assert_protected_drafts_unchanged(protected_before, after_capture)
    _assert_historical_evidence_unchanged(evidence_before, after_capture)
    captured = capture.get("captured") is True
    if not captured:
        # 抓取器失败可以记录候选失败与来源退避，但不得在失败路径制造线索或正式证据。
        if (
            len(after_capture.get("evidence", [])) != len(evidence_before)
            or _stable_json(after_capture.get("leads")) != leads_before
        ):
            raise RuntimeError("草稿确认失败时不得新增或修改招商线索和正式证据")
        capture_candidate = capture.get("candidate")
        capture_candidate = capture_candidate if isinstance(capture_candidate, dict) else {}
        failure_reason = capture.get("message")
        if not isinstance(failure_reason, str) or not failure_reason.strip():
            fallback_error = capture_candidate.get("last_capture_error")
            failure_reason = fallback_error if isinstance(fallback_error, str) else "服务器重新抓取未能完成证据核验"
        failure_stage = capture.get("failure_stage")
        failure_stage = failure_stage if isinstance(failure_stage, str) else "page_or_content_verification"
        audit_result = _append_confirmation_audit(
            workspace_dir,
            draft_id=normalized_draft_id,
            candidate_id=candidate_id,
            authenticated_user_id=normalized_user_id,
            review_note=normalized_note,
            new_state="confirmation_not_captured",
            capture_attempted=True,
            capture_result="server_recapture_not_verified",
            evidence_id=None,
            protected_before=protected_before,
            verification_failure_reason=failure_reason,
            verification_failure_stage=failure_stage,
        )
        return {
            "confirmed": False,
            "message": failure_reason,
            "verification": {
                "stage": failure_stage,
                "reason": failure_reason,
                "next_step": "不要在浏览器改写草稿日期、实体或摘录；请根据已保存材料选择待补证、观察或无效，或在当前页面确有新公开内容后再次请求服务器核验。",
            },
            "capture": capture,
            **audit_result,
        }

    result = capture.get("result")
    evidence = result.get("evidence") if isinstance(result, dict) else None
    evidence_id = evidence.get("evidence_id") if isinstance(evidence, dict) else None
    if not isinstance(evidence_id, str):
        raise RuntimeError("受控抓取成功但未返回可追溯的正式证据 ID")
    new_state = (
        "confirmed_and_captured"
        if len(after_capture.get("evidence", [])) > len(evidence_before)
        else "confirmed_with_existing_evidence"
    )
    audit_result = _append_confirmation_audit(
        workspace_dir,
        draft_id=normalized_draft_id,
        candidate_id=candidate_id,
        authenticated_user_id=normalized_user_id,
        review_note=normalized_note,
        new_state=new_state,
        capture_attempted=True,
        capture_result="server_recapture_verified",
        evidence_id=evidence_id,
        protected_before=protected_before,
    )
    return {"confirmed": True, "capture": capture, **audit_result}
