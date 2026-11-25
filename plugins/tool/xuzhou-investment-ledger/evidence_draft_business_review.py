"""信息不完整证据草稿的普通用户业务判断与追加审计。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .evidence_draft import (
    DRAFT_STATE,
    _evidence_draft_business_review_audits,
    _evidence_draft_confirmation_audits,
    _evidence_drafts,
    business_review_state_for_draft,
    missing_draft_confirmation_fields,
)
from .storage import ledger_path, load_ledger, save_ledger


BUSINESS_REVIEW_OPERATION_SCOPE = "authenticated_user_incomplete_draft_business_judgment_only"
BUSINESS_REVIEW_STATES = frozenset({"need_evidence", "observe", "invalid"})
BUSINESS_REVIEW_STATE_ALIASES = {
    "待补证": "need_evidence",
    "观察": "observe",
    "无效": "invalid",
}
_VALID_DRAFT_ID_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _utc_now() -> str:
    """返回业务判断的服务器 UTC 审计时间，不接受客户端伪造的操作时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """用服务器审计字段生成稳定判断 ID，不允许客户端覆盖历史记录标识。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验判断备注、认证用户与草稿 ID 的有限文本，不写入任何来源或证据字段。"""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name}不能超过{maximum}个字符")
    return normalized


def _required_draft_id(value: object) -> str:
    """仅接受服务器草稿 ID，拒绝 URL、路径与外部对象进入业务判断流程。"""
    draft_id = _required_text(value, "draft_id", maximum=120)
    if any(character not in _VALID_DRAFT_ID_CHARACTERS for character in draft_id):
        raise ValueError("draft_id必须是有效的服务器草稿 ID")
    return draft_id


def _normalize_decision(value: object) -> str:
    """把中文或英文业务结论收敛为三种受控状态，禁止把未证实草稿直接推进。"""
    raw_decision = _required_text(value, "decision", maximum=100).casefold()
    normalized = BUSINESS_REVIEW_STATE_ALIASES.get(raw_decision, raw_decision)
    if normalized not in BUSINESS_REVIEW_STATES:
        raise ValueError("decision只能是待补证、观察或无效")
    return normalized


def _find_draft(ledger: dict[str, Any], draft_id: str) -> dict[str, Any]:
    """在已签名草稿列表中精确定位记录；不存在时不允许请求临时提交草稿内容。"""
    draft = next(
        (
            item
            for item in _evidence_drafts(ledger)
            if isinstance(item, dict) and item.get("draft_id") == draft_id
        ),
        None,
    )
    if draft is None:
        raise ValueError("未找到待补证的证据草稿")
    return draft


def _has_successful_confirmation(ledger: dict[str, Any], draft_id: str) -> bool:
    """判断草稿是否已经入证成功，成功后应改在线索层复核而不是再写草稿业务判断。"""
    return any(
        isinstance(audit, dict)
        and audit.get("draft_id") == draft_id
        and audit.get("new_state") in {"confirmed_and_captured", "confirmed_with_existing_evidence"}
        for audit in _evidence_draft_confirmation_audits(ledger)
    )


def _stable_json(value: object) -> str:
    """稳定序列化受保护业务字段，供保存前逐字发现任何越权改写。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _protected_records_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """保存业务判断绝不能改写的所有草稿、来源、证据和业务记录快照。

    输入是当前已验签台账，输出为稳定序列化的保护字段。业务判断只能追加
    ``evidence_draft_business_review_audit``；它不能创建线索、增加证据、更新候选、改写
    自动草稿、候选原始快照、评分、反馈或既有确认历史。
    """
    protected_fields = (
        "leads",
        "evidence",
        "discovery_candidates",
        "candidate_observations",
        "evidence_drafts",
        "evidence_draft_confirmation_audit",
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


def _assert_protected_records_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """确认业务判断保存前后，除独立审计外没有任何原始或业务数据被改写。"""
    if _protected_records_snapshot(ledger) != before:
        raise RuntimeError("草稿业务判断不得修改线索、证据、候选、快照、评分或确认审计")


def review_incomplete_evidence_draft(
    workspace_dir: Path,
    *,
    draft_id: str,
    decision: str,
    review_note: str,
    authenticated_user_id: str,
) -> dict[str, object]:
    """由普通登录用户为关键字段缺失的草稿追加待补证、观察或无效业务判断。

    输入是服务器草稿 ID、三种受控业务结论、备注与认证中间件提供的用户 ID；输出为追加审计
    和当前派生状态。客户端不能提交实体、组织类型、URL、日期、摘录、哈希、来源等级、评分
    或用户身份，因此无法利用“补证”伪造来源事实。只有缺少关键可核验字段的草稿可处理；
    字段完整的草稿必须走服务器重抓确认，已成功入证的草稿必须在线索层继续复核。保存前后
    逐字校验所有受保护记录，唯一变化是本函数新加的一条业务判断审计，普通用户不需管理员。
    """
    normalized_draft_id = _required_draft_id(draft_id)
    normalized_decision = _normalize_decision(decision)
    normalized_note = _required_text(review_note, "review_note", maximum=2_000)
    normalized_user_id = _required_text(
        authenticated_user_id, "authenticated_user_id", maximum=100
    )
    ledger = load_ledger(workspace_dir)
    draft = _find_draft(ledger, normalized_draft_id)
    if draft.get("state") != DRAFT_STATE:
        raise ValueError("该草稿不是等待人工确认状态")
    if _has_successful_confirmation(ledger, normalized_draft_id):
        raise ValueError("该草稿已经成功入证，请在线索层进行后续业务复核")
    missing_fields = missing_draft_confirmation_fields(draft)
    if not missing_fields:
        raise ValueError("草稿关键字段完整，应通过服务器重新抓取确认，不能走待补证业务判断")
    candidate_id = draft.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("证据草稿缺少有效候选 ID")
    protected_before = _protected_records_snapshot(ledger)
    old_state = business_review_state_for_draft(ledger, draft_id=normalized_draft_id)
    reviewed_at = _utc_now()
    audit = {
        "review_id": _stable_id(
            "draft-business-review",
            {
                "draft_id": normalized_draft_id,
                "reviewed_at": reviewed_at,
                "authenticated_user_id": normalized_user_id,
                "new_state": normalized_decision,
            },
        ),
        "draft_id": normalized_draft_id,
        "candidate_id": candidate_id,
        "reviewed_at": reviewed_at,
        "reviewed_by_user_id": normalized_user_id,
        "old_state": old_state,
        "new_state": normalized_decision,
        "review_note": normalized_note,
        "missing_evidence_fields": missing_fields,
        "operation_scope": BUSINESS_REVIEW_OPERATION_SCOPE,
    }
    audits = _evidence_draft_business_review_audits(ledger)
    audits.append(audit)
    _assert_protected_records_unchanged(protected_before, ledger)
    saved_path = save_ledger(workspace_dir, ledger)
    return {
        "business_review": audit,
        "business_review_state": normalized_decision,
        "ledger_path": str(saved_path),
    }
