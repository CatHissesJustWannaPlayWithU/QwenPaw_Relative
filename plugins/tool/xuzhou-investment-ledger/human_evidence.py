"""业务人员可提交的授权材料与可追溯人工证据通道。

服务器抓取公开网页仍是最理想的证据来源，但它无法读取需要登录、动态渲染、机器人
拦截或仅向业务人员开放的页面。本模块允许已经获徐州团队授权的业务人员把其*有权
保留和使用*的页面摘录、导出文本或小型截图/PDF 直接登记为一条明确标识的
``human_attested_material``。它不会把人工材料伪装成服务器抓取：材料来源方式、实名
提交人、提交时间、内容哈希、附件哈希和服务器是否曾重新抓取均会连同证据链签名保存。

该通道让人可以在服务器访问失败时继续完成内部招商研判，但不会登录网站、保存密码或
Cookie、执行网页脚本、自动联系外部主体，亦不会把人工材料用于机器自动事实分类、风险
预警、自动评分或自动对外动作。
"""

from __future__ import annotations

import base64
import copy
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .core import (
    CLAIM_TYPE_ALIASES,
    ENTITY_KIND_ALIASES,
    ENTITY_KINDS,
    REQUESTABLE_CLAIM_TYPES,
    SECTOR_ALIASES,
    SECTORS,
    SOURCE_LEVEL_ALIASES,
    SOURCE_LEVELS,
    _append_status_history,
    _canonical,
    _fingerprint,
    _normalize_xuzhou_tags,
    _required_text,
    _validate_http_url,
)
from .evidence_integrity import sign_next_evidence
from .storage import (
    evidence_snapshot_path,
    ledger_path,
    load_ledger,
    save_evidence_snapshot,
    save_ledger,
)


HUMAN_EVIDENCE_OPERATION_SCOPE = "authenticated_team_member_human_attested_material_only"
HUMAN_EVIDENCE_ORIGIN = "human_attested_material"
HUMAN_EVIDENCE_COLLECTION_METHOD = "human_authorized_submission"
HUMAN_EVIDENCE_CLASSIFICATION_METHOD = "human_attested_explicit_business_classification_v1"
MAX_HUMAN_MATERIAL_TEXT_CHARS = 12_000
MAX_HUMAN_ATTACHMENT_BYTES = 512 * 1024
MAX_HUMAN_EVIDENCE_SUBMISSIONS = 2_000
MAX_HUMAN_EVIDENCE_LIST_LIMIT = 200
UNKNOWN_PUBLICATION_DATE = "未显示页面日期"

ACCESS_MODES = frozenset(
    {
        "public_page_accessible_to_human",
        "login_or_member_access",
        "dynamic_or_script_rendered_page",
        "robot_or_server_blocked",
        "authorized_file_or_export",
        "other_authorized_access",
    }
)
ACCESS_MODE_LABELS = {
    "public_page_accessible_to_human": "业务人员可打开的公开页面",
    "login_or_member_access": "需要合法登录或成员权限的页面",
    "dynamic_or_script_rendered_page": "需要浏览器动态渲染的页面",
    "robot_or_server_blocked": "服务器被 robots、访问限制或站点拦截",
    "authorized_file_or_export": "业务人员有权取得的文件或网页导出材料",
    "other_authorized_access": "其他已获授权的访问方式",
}
SENSITIVE_URL_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "id_token",
        "password",
        "session",
        "signature",
        "sig",
        "token",
    }
)
ALLOWED_ATTACHMENT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    }
)


def _utc_now() -> str:
    """返回服务器生成的提交时间，不接受浏览器时钟。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """由受控业务字段生成稳定 ID，避免浏览器传入主键或重复提交。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _required_human_text(value: object, field_name: str, *, maximum: int) -> str:
    """校验人工说明，不允许控制字符、空白或无界内容进入共享台账。"""
    text = _required_text(value, field_name, max_length=maximum)
    if any(ord(character) < 0x20 and character not in {"\n", "\t"} for character in text):
        raise ValueError(f"{field_name}不能包含控制字符")
    return text


def _optional_publication_date(value: object) -> str:
    """保留“页面未显示日期”，绝不用采集或提交时间伪造发布时间。"""
    if value is None or value == "":
        return UNKNOWN_PUBLICATION_DATE
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError("source_published_at必须使用YYYY-MM-DD格式，或留空表示页面未显示日期")
    try:
        return date.fromisoformat(normalized[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("source_published_at必须使用YYYY-MM-DD格式，或留空表示页面未显示日期") from exc


def _normalize_access_mode(value: object) -> str:
    """将人工访问情形限制为固定枚举，避免把凭据或自由文本写进审计。"""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError("access_mode不能为空")
    if normalized not in ACCESS_MODES:
        raise ValueError("access_mode不是允许的人工访问情形")
    return normalized


def _validate_human_source_url(value: object) -> str:
    """保存人工材料链接前拒绝本机、内网和带凭据地址。

    人工材料允许“服务器无法抓到、但业务人员有权打开”的页面，不代表台账应保存可指向
    本机控制面、私网服务或嵌入账号密码的链接。组织内部页面应使用其正常的受控域名；
    服务器不会尝试访问该链接或取得业务人员的登录状态。
    """

    source_url = _validate_http_url(value)
    parsed = urlparse(source_url)
    hostname = parsed.hostname
    if parsed.username or parsed.password:
        raise ValueError("source_url不能包含用户名、密码或访问凭据")
    if not hostname:
        raise ValueError("source_url必须包含公开或受控组织域名")
    normalized_host = hostname.casefold().rstrip(".")
    if normalized_host in {"localhost", "localhost.localdomain"} or normalized_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("source_url不能指向本机或内部网络地址")
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise ValueError("source_url必须使用域名，不能使用IP地址")
    # 带签名下载地址、会话令牌等经常被放在查询参数中。即使页面提交者有权使用，团队共享
    # 台账也不应把它们扩散成可复制的访问凭据；可保留不含凭据的规范页面地址并在材料文本
    # 中说明访问情形。普通分页、筛选或文章 ID 查询参数仍可保留。
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & SENSITIVE_URL_QUERY_KEYS:
        raise ValueError("source_url不能包含访问令牌、会话、签名、密码或其他凭据参数")
    return source_url


def _normalize_attachment(
    value: object,
) -> tuple[dict[str, object] | None, str | None]:
    """验证可选的小型人工附件，只返回受控元数据和 Base64 原文。

    附件只作为人工提交材料的不可变快照组成部分，不会被服务器执行、解析为网页脚本或
    自动当作企业事实。限制 MIME、大小、文件名和 Base64 格式以减少任意文件写入风险。
    """
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError("attachment必须是对象")
    allowed_fields = {"file_name", "mime_type", "content_base64"}
    unexpected_fields = set(value) - allowed_fields
    if unexpected_fields:
        raise ValueError("attachment包含不允许的字段")
    file_name = _required_human_text(value.get("file_name"), "attachment.file_name", maximum=180)
    # 文件名仅用于界面与审计，不参与任何路径构造；仍拒绝路径分隔符以免后续维护者误用。
    if any(character in file_name for character in {"/", "\\", "\x00"}):
        raise ValueError("attachment.file_name不能包含路径字符")
    mime_type = value.get("mime_type")
    if not isinstance(mime_type, str) or mime_type not in ALLOWED_ATTACHMENT_MIME_TYPES:
        raise ValueError("attachment.mime_type不是允许的 PDF、图片或纯文本类型")
    encoded = value.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("attachment.content_base64不能为空")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("attachment.content_base64不是有效Base64") from exc
    if not raw:
        raise ValueError("attachment不能是空文件")
    if len(raw) > MAX_HUMAN_ATTACHMENT_BYTES:
        raise ValueError("附件不能超过512KB；请上传关键截图、节选或导出的单页材料")
    return (
        {
            "file_name": file_name,
            "mime_type": mime_type,
            "byte_size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        encoded,
    )


def _submission_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得人类提交审计列表；历史台账缺省时只在本次受控保存中补齐。"""
    records = ledger.setdefault("human_evidence_submissions", [])
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("招商线索台账中的 human_evidence_submissions 必须是对象列表")
    return records


def _human_attested_evidence_count(ledger: dict[str, Any]) -> int:
    """返回已记录的人工材料数，供上限检查而非业务评分。"""
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    return sum(
        1
        for item in evidence
        if isinstance(item, dict) and item.get("evidence_origin") == HUMAN_EVIDENCE_ORIGIN
    )


def _lead_for_human_evidence(
    ledger: dict[str, Any],
    *,
    lead_id: str,
    entity_name: str,
    entity_kind: str,
    sector: str,
    now: str,
) -> dict[str, Any]:
    """查找或创建线索；首次人工材料同样只进入待人工研判状态。"""
    leads = ledger.get("leads")
    if not isinstance(leads, list):
        raise ValueError("招商线索台账中的 leads 必须是列表")
    lead = next(
        (item for item in leads if isinstance(item, dict) and item.get("lead_id") == lead_id),
        None,
    )
    if lead is not None:
        lead.setdefault("risk_alert_level", None)
        lead.setdefault("risk_alert_type", None)
        lead.setdefault("risk_alert_status", None)
        lead.setdefault("risk_alert_evidence_ids", [])
        return lead
    lead = {
        "lead_id": lead_id,
        "entity_name": entity_name,
        "entity_kind": entity_kind,
        "sector": sector,
        "review_state": "pending_user_review",
        "red_alert": False,
        "alert_reason": None,
        "risk_alert_level": None,
        "risk_alert_type": None,
        "risk_alert_status": None,
        "risk_alert_evidence_ids": [],
        "evidence_ids": [],
        "status_history": [],
        "created_at": now,
        "updated_at": now,
    }
    _append_status_history(
        lead,
        state="pending_user_review",
        reason="首次登记业务人员授权材料，等待人工招商研判；不是服务器抓取结论。",
    )
    leads.append(lead)
    return lead


def _machine_assistance(
    ledger: dict[str, Any],
    *,
    lead_id: str,
    source_url: str,
    access_mode: str,
) -> dict[str, object]:
    """生成透明的机械辅助提示，不从人工材料自动产生企业事实。"""
    matching_source_evidence_ids = [
        str(item.get("evidence_id"))
        for item in ledger.get("evidence", [])
        if isinstance(item, dict)
        and item.get("source_url") == source_url
        and isinstance(item.get("evidence_id"), str)
    ]
    same_lead_evidence_count = sum(
        1
        for item in ledger.get("evidence", [])
        if isinstance(item, dict) and item.get("lead_id") == lead_id
    )
    can_later_retry = access_mode in {
        "public_page_accessible_to_human",
        "dynamic_or_script_rendered_page",
        "robot_or_server_blocked",
    }
    return {
        "same_source_evidence_ids": matching_source_evidence_ids[:20],
        "same_lead_evidence_count_before_submission": same_lead_evidence_count,
        "server_recapture_recommended": can_later_retry,
        "server_recapture_note": (
            "服务器可在后续有公开访问条件时再次尝试读取原链接；这不会覆盖本次人工材料。"
            if can_later_retry
            else "该材料可能依赖授权访问或文件导出；请保留提交时的摘录和附件，服务器不会尝试取得你的登录信息。"
        ),
        "automatic_business_conclusion": "not_generated",
    }


def _public_submission(
    submission: dict[str, Any], evidence: dict[str, Any] | None = None
) -> dict[str, object]:
    """输出可供团队查看的人工材料摘要，刻意排除附件原文与快照路径。"""
    fields = (
        "submission_id",
        "evidence_id",
        "lead_id",
        "submitted_at",
        "submitted_by_user_id",
        "access_mode",
        "access_mode_label",
        "publication_date_status",
        "attachment",
        "machine_assistance",
        "operation_scope",
    )
    result: dict[str, object] = {
        field_name: copy.deepcopy(submission.get(field_name)) for field_name in fields
    }
    if isinstance(evidence, dict):
        for field_name in (
            "source_url",
            "source_title",
            "source_published_at",
            "source_level",
            "claim_type",
            "evidence_quality",
            "excerpt",
            "xuzhou_association_tags",
            "evidence_origin",
            "server_recapture_status",
        ):
            result[field_name] = copy.deepcopy(evidence.get(field_name))
    return result


def record_human_attested_evidence(
    workspace_dir: Path,
    *,
    entity_name: str,
    entity_kind: str,
    sector: str,
    source_url: str,
    source_title: str,
    source_published_at: str | None,
    source_level: str,
    claim_type: str,
    evidence_quality: int,
    excerpt: str,
    material_text: str | None,
    access_mode: str,
    attestation_confirmed: bool,
    xuzhou_association_tags: list[str] | None,
    attachment: dict[str, object] | None,
    authenticated_user_id: str,
) -> dict[str, object]:
    """登记一条带实名证明的人工材料，并追加签名证据链记录。

    人工可选择在服务器无法访问原页面时直接登记；该记录是内部台账中可追溯的材料依据，
    但明确标识为人工提交，且不会触发服务器风险预警、自动事实分类、自动评分或自动外联。
    后续招商跟进仍需要业务人员逐项完成既有审批卡，特别是主体、关键事项和交叉来源核对。
    """
    if attestation_confirmed is not True:
        raise ValueError("必须确认你有权保存和用于内部招商研判该材料，且未提交密码、Cookie或受限个人信息")
    normalized_entity_name = _required_human_text(entity_name, "entity_name", maximum=500)
    normalized_entity_kind = _canonical(entity_kind, "entity_kind", ENTITY_KINDS, ENTITY_KIND_ALIASES)
    normalized_sector = _canonical(sector, "sector", SECTORS, SECTOR_ALIASES)
    normalized_source_url = _validate_human_source_url(source_url)
    normalized_source_title = _required_human_text(source_title, "source_title", maximum=500)
    normalized_source_date = _optional_publication_date(source_published_at)
    normalized_source_level = _canonical(source_level, "source_level", SOURCE_LEVELS, SOURCE_LEVEL_ALIASES)
    normalized_claim_type = _canonical(
        claim_type,
        "claim_type",
        REQUESTABLE_CLAIM_TYPES,
        CLAIM_TYPE_ALIASES,
    )
    if not isinstance(evidence_quality, int) or isinstance(evidence_quality, bool) or not 1 <= evidence_quality <= 5:
        raise ValueError("evidence_quality必须是1到5之间的整数")
    normalized_excerpt = _required_human_text(excerpt, "excerpt", maximum=2_000)
    normalized_material_text = ""
    if material_text is not None and material_text != "":
        normalized_material_text = _required_human_text(
            material_text,
            "material_text",
            maximum=MAX_HUMAN_MATERIAL_TEXT_CHARS,
        )
    normalized_access_mode = _normalize_access_mode(access_mode)
    normalized_tags = _normalize_xuzhou_tags(xuzhou_association_tags)
    attachment_metadata, attachment_base64 = _normalize_attachment(attachment)
    normalized_user_id = _required_human_text(
        authenticated_user_id,
        "authenticated_user_id",
        maximum=100,
    )

    now = _utc_now()
    material_envelope = {
        "schema": "xuzhou-human-attested-material-v1",
        "submitted_at": now,
        "submitted_by_user_id": normalized_user_id,
        "source": {
            "source_url": normalized_source_url,
            "source_title": normalized_source_title,
            "source_published_at": normalized_source_date,
        },
        "attestation": {
            "confirmed": True,
            "access_mode": normalized_access_mode,
            "access_mode_label": ACCESS_MODE_LABELS[normalized_access_mode],
            "boundary": "人工材料不等于服务器重新抓取；不得提交密码、Cookie、会话令牌或无权留存的信息。",
        },
        "material": {
            "exact_excerpt": normalized_excerpt,
            "additional_material_text": normalized_material_text,
            "attachment": (
                {**attachment_metadata, "content_base64": attachment_base64}
                if attachment_metadata is not None and attachment_base64 is not None
                else None
            ),
        },
    }
    raw_content = json.dumps(
        material_envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    # ``save_evidence_snapshot`` 的总上限为 256 MiB，但单一正式证据使用 1 MiB 固定
    # 边界；人工附件的更小上限确保 JSON/Base64 包装后仍不会冲破这一边界。
    if len(raw_content) > 1_048_576:
        raise ValueError("人工材料连同附件超过1MB；请保留关键摘录并上传更小的截图或导出页")
    raw_sha256 = hashlib.sha256(raw_content).hexdigest()
    lead_id = _fingerprint(
        "lead",
        {"entity_kind": normalized_entity_kind, "entity_name": normalized_entity_name.casefold()},
    )
    evidence_id = _fingerprint(
        "human-evidence",
        {
            "lead_id": lead_id,
            "source_url": normalized_source_url,
            "source_title": normalized_source_title,
            "source_published_at": normalized_source_date,
            "excerpt": normalized_excerpt,
            "raw_sha256": raw_sha256,
        },
    )

    ledger = load_ledger(workspace_dir)
    evidence_records = ledger.get("evidence")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    existing_evidence = next(
        (
            item
            for item in evidence_records
            if isinstance(item, dict) and item.get("evidence_id") == evidence_id
        ),
        None,
    )
    if existing_evidence is not None:
        existing_submission = next(
            (
                item
                for item in _submission_records(ledger)
                if item.get("evidence_id") == evidence_id
            ),
            None,
        )
        return {
            "created": False,
            "message": "相同人工材料已经登记，未重复写入共享台账",
            "lead": next(
                (item for item in ledger.get("leads", []) if isinstance(item, dict) and item.get("lead_id") == lead_id),
                None,
            ),
            "evidence": copy.deepcopy(existing_evidence),
            "submission": _public_submission(existing_submission, existing_evidence)
            if isinstance(existing_submission, dict)
            else None,
            "ledger_path": str(ledger_path(workspace_dir)),
        }
    if _human_attested_evidence_count(ledger) >= MAX_HUMAN_EVIDENCE_SUBMISSIONS:
        raise RuntimeError("人工材料记录已达到安全上限，请由管理员导出归档后再继续")

    lead = _lead_for_human_evidence(
        ledger,
        lead_id=lead_id,
        entity_name=normalized_entity_name,
        entity_kind=normalized_entity_kind,
        sector=normalized_sector,
        now=now,
    )
    assistance = _machine_assistance(
        ledger,
        lead_id=lead_id,
        source_url=normalized_source_url,
        access_mode=normalized_access_mode,
    )
    evidence = {
        "evidence_id": evidence_id,
        "lead_id": lead_id,
        "source_url": normalized_source_url,
        "source_title": normalized_source_title,
        "source_published_at": normalized_source_date,
        "source_level": normalized_source_level,
        "claim_type": normalized_claim_type,
        "claim_type_requested": normalized_claim_type,
        "claim_type_matched_terms": [],
        "claim_type_text_support": "human_attested_not_server_text_classified",
        "claim_type_classification_method": HUMAN_EVIDENCE_CLASSIFICATION_METHOD,
        "evidence_quality": evidence_quality,
        "excerpt": normalized_excerpt,
        "xuzhou_association_tags": normalized_tags,
        "created_at": now,
        "source_type": HUMAN_EVIDENCE_ORIGIN,
        "collection_method": HUMAN_EVIDENCE_COLLECTION_METHOD,
        "collected_at": now,
        "raw_sha256": raw_sha256,
        "evidence_origin": HUMAN_EVIDENCE_ORIGIN,
        "human_attestation": {
            "attested_by_user_id": normalized_user_id,
            "attested_at": now,
            "access_mode": normalized_access_mode,
            "access_mode_label": ACCESS_MODE_LABELS[normalized_access_mode],
            "publication_date_status": (
                "page_date_not_visible"
                if normalized_source_date == UNKNOWN_PUBLICATION_DATE
                else "human_entered_page_date"
            ),
            "source_level_assessed_by": "human",
            "evidence_quality_assessed_by": "human",
            "server_retrieval": "not_performed_at_human_submission",
        },
        "server_recapture_status": "not_requested",
        "operation_scope": HUMAN_EVIDENCE_OPERATION_SCOPE,
    }
    snapshot_path = evidence_snapshot_path(workspace_dir, evidence_id)
    evidence["raw_snapshot_relative_path"] = str(snapshot_path.relative_to(workspace_dir))
    evidence["raw_snapshot_byte_size"] = len(raw_content)
    sign_next_evidence(workspace_dir, ledger, evidence)
    submission = {
        "submission_id": _stable_id(
            "human-evidence-submission",
            {
                "evidence_id": evidence_id,
                "submitted_at": now,
                "submitted_by_user_id": normalized_user_id,
            },
        ),
        "evidence_id": evidence_id,
        "lead_id": lead_id,
        "submitted_at": now,
        "submitted_by_user_id": normalized_user_id,
        "access_mode": normalized_access_mode,
        "access_mode_label": ACCESS_MODE_LABELS[normalized_access_mode],
        "publication_date_status": (
            "page_date_not_visible"
            if normalized_source_date == UNKNOWN_PUBLICATION_DATE
            else "human_entered_page_date"
        ),
        "attachment": {"present": attachment_metadata is not None, **(attachment_metadata or {})},
        "machine_assistance": assistance,
        "operation_scope": HUMAN_EVIDENCE_OPERATION_SCOPE,
    }
    submissions = _submission_records(ledger)
    snapshot_created = False
    try:
        save_evidence_snapshot(
            workspace_dir,
            evidence_id=evidence_id,
            payload={
                "evidence_id": evidence_id,
                "source_url": normalized_source_url,
                "collected_at": now,
                "raw_sha256": raw_sha256,
                "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
            },
        )
        snapshot_created = True
        evidence_records.append(evidence)
        evidence_ids = lead.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            raise RuntimeError("招商线索证据引用格式异常")
        evidence_ids.append(evidence_id)
        lead["updated_at"] = now
        submissions.append(submission)
        saved_path = save_ledger(workspace_dir, ledger)  # type: ignore[arg-type]
    except Exception:
        if snapshot_created and snapshot_path.exists() and snapshot_path.is_file() and not snapshot_path.is_symlink():
            snapshot_path.unlink()
        raise
    return {
        "created": True,
        "lead": copy.deepcopy(lead),
        "evidence": copy.deepcopy(evidence),
        "submission": _public_submission(submission, evidence),
        "ledger_path": str(saved_path),
        "next_step": "材料已作为“人工提交、未由服务器重新抓取”的可追溯依据入账；如要纳入招商跟进，仍请由业务人员完成主体、关键事项、来源交叉核对、负责人和下一步审批。",
    }


def list_human_evidence_submissions(
    workspace_dir: Path,
    *,
    limit: int = 100,
) -> dict[str, object]:
    """列出已登记的人工材料摘要，不返回附件原文、私有快照路径或完整性签名。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_HUMAN_EVIDENCE_LIST_LIMIT:
        raise ValueError(f"limit必须是1到{MAX_HUMAN_EVIDENCE_LIST_LIMIT}之间的整数")
    ledger = load_ledger(workspace_dir)
    evidence_by_id = {
        item.get("evidence_id"): item
        for item in ledger.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    submissions = _submission_records(ledger)
    public_items = [
        _public_submission(submission, evidence_by_id.get(submission.get("evidence_id")))
        for submission in submissions
    ]
    public_items.sort(key=lambda item: str(item.get("submitted_at") or ""), reverse=True)
    return {
        "items": public_items[:limit],
        "count": len(public_items),
        "operation_scope": "authenticated_team_member_human_attested_material_readonly",
        "ledger_path": str(ledger_path(workspace_dir)),
    }
