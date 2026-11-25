"""从已验证候选快照生成待人工确认的证据草稿。

本模块不访问网络、不会使用模型推理，也不把页面命中当成企业事实。它只读取已经由
``candidate_observation`` 校验 SHA-256 的本地原始快照，以固定正则和词表提取“可能的”
实体、日期、事实类别与原句，作为人工进一步核验的草稿。草稿和正式 ``evidence`` 分离，
后续若要正式入证仍必须经过受控重抓、原文摘录校验和既有证据完整性签名流程。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .candidate_observation import (
    _candidate_observation_lists,
    _read_verified_observation_snapshot,
    _required_server_identifier,
)
from .storage import ledger_path, load_ledger, save_ledger


# 单次后台任务只处理与候选原始快照相同的小批量，防止本地抽取积压时占用过多资源。
MAX_EVIDENCE_DRAFTS_PER_RUN = 3
MAX_EVIDENCE_DRAFT_RECORDS = 1_000
MAX_EVIDENCE_DRAFT_EXCERPT_CHARS = 1_200
EXTRACTOR_VERSION = "deterministic-candidate-observation-v1"
DRAFT_STATE = "pending_human_confirmation"
DRAFT_OPERATION_SCOPE = "automatic_deterministic_candidate_observation_draft_only"
DRAFT_REQUIRED_CONFIRMATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity_name", "实体名称"),
    ("entity_kind", "实体类型（企业、基金或创业团队）"),
    ("source_published_at", "来源发布日期"),
    ("claim_type", "事实类别"),
    ("excerpt", "原始摘录"),
)

# 这些词只用于给出“事实类别候选”，映射严格限制在 core.py 已允许的正式证据类别内。
# 风险词放在机会词之前，确保页面同时提及融资和资金链异常时优先生成 ``risk`` 草稿；
# 命中并不证明事情实际发生，更不能跳过人工复核或正式抓取。
CLAIM_TYPE_TERM_RULES: tuple[tuple[str, str], ...] = (
    ("资金链断裂", "risk"),
    ("资金链紧张", "risk"),
    ("流动性紧张", "risk"),
    ("融资失败", "risk"),
    ("项目暂停", "risk"),
    ("终止合作", "risk"),
    ("停产", "risk"),
    ("停工", "risk"),
    ("破产重整", "risk"),
    ("行政处罚", "risk"),
    ("核心团队离职", "risk"),
    ("完成募资", "fund_raising"),
    ("启动募资", "fund_raising"),
    ("基金募资", "fund_raising"),
    ("融资", "financing"),
    ("获得投资", "financing"),
    ("获投", "financing"),
    ("战略合作", "cooperation"),
    ("签署合作", "cooperation"),
    ("寻找合作伙伴", "cooperation"),
    ("扩产", "expansion"),
    ("扩建", "expansion"),
    ("新建生产线", "expansion"),
    ("投产", "expansion"),
    ("开工", "expansion"),
)

ENTITY_EVENT_PATTERN = re.compile(
    r"(?P<entity>[\u4e00-\u9fffA-Za-z0-9·（）()\-]{2,80}?)\s*"
    r"(?:宣布|发布|披露|完成|获得|启动|签署|正在|计划|拟|寻求|落地|出现|陷入|被列为|"
    r"停产|停工|终止|拖欠|破产|清算|重整)",
)
# Investment-news titles often put the company display name in Chinese quotes.
# Prefer that explicit title text before scanning long body sentences, where a
# broad event pattern can otherwise stop at an unrelated phrase such as
# “公司成立不足一年即…”.  This still yields only a *draft* entity candidate.
TITLE_QUOTED_ENTITY_PATTERN = re.compile(
    r"^[「『“\"]\s*(?P<entity>[\u4e00-\u9fffA-Za-z0-9·（）()\-]{2,80}?)\s*[」』”\"]"
)
# The negative lookahead prevents a partial match such as ``2026-05-1`` inside
# ``2026-05-18``.  A partial day used to make an otherwise valid re-check fail.
ISO_DATE_PATTERN = re.compile(r"(?P<date>20\d{2})[-/.](?P<month>0?[1-9]|1[0-2])[-/.](?P<day>0?[1-9]|[12]\d|3[01])(?!\d)")
CHINESE_DATE_PATTERN = re.compile(r"(?P<date>20\d{2})年(?P<month>0?[1-9]|1[0-2])月(?P<day>0?[1-9]|[12]\d|3[01])日(?!\d)")
SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?", re.MULTILINE)
GENERIC_ENTITY_NAMES = frozenset({"企业", "公司", "机构", "基金", "团队", "本公司", "该公司"})


def _utc_now() -> str:
    """返回由服务器产生的草稿审计时间，不接受页面、模型或前端传入的时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """用观察 ID、原始哈希和抽取版本生成稳定草稿 ID，阻止重复任务反复追加同一草稿。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _evidence_drafts(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得独立草稿列表；结构异常即停止，绝不猜测、修复或改写历史台账。"""
    drafts = ledger.get("evidence_drafts")
    if not isinstance(drafts, list):
        raise ValueError("招商线索台账中的 evidence_drafts 必须是列表")
    return drafts


def _evidence_draft_confirmation_audits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得草稿确认追加审计，字段损坏时失败关闭而不伪造任何复核历史。"""
    audits = ledger.get("evidence_draft_confirmation_audit")
    if not isinstance(audits, list):
        raise ValueError("招商线索台账中的 evidence_draft_confirmation_audit 必须是列表")
    return audits


def _evidence_draft_business_review_audits(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得信息不完整草稿的业务判断审计，异常时失败关闭而不伪造状态历史。"""
    audits = ledger.get("evidence_draft_business_review_audit")
    if not isinstance(audits, list):
        raise ValueError("招商线索台账中的 evidence_draft_business_review_audit 必须是列表")
    return audits


def _protected_records_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """序列化自动草稿绝不能触碰的业务、证据和候选快照索引。

    输入是本次保存前的内存台账，输出为受保护字段的稳定序列化快照。草稿生成可以只追加
    ``evidence_drafts``；即使是已保存候选的状态、原始观察索引、评分和人工反馈也不能被
    这个自动流程调整，避免“辅助抽取”越权变成业务自动化。
    """
    protected_fields = (
        "leads",
        "evidence",
        "score_snapshots",
        "outcome_feedback",
        "discovery_candidates",
        "candidate_observations",
    )
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return snapshot


def _assert_protected_records_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """在保存前确认草稿任务未改写任何既有证据链或业务记录。"""
    if _protected_records_snapshot(ledger) != before:
        raise RuntimeError("自动证据草稿不得修改线索、原始证据、评分、反馈、候选或观察快照")


def _source_date_candidate(visible_text: str) -> tuple[str | None, str | None]:
    """从原始可见正文识别明确书写的发布日期，无法解析时保持待确认。

    输入为已验证快照的原始可见正文，输出为 ``(ISO 日期, 原页面日期文字)``；仅接受完整
    年月日且可由 Python 日期库校验的值，绝不拿采集时间代替发布日期。函数不写台账、不
    推断页面发布时间，也不会修改任何原始证据字段。
    """
    for pattern in (ISO_DATE_PATTERN, CHINESE_DATE_PATTERN):
        match = pattern.search(visible_text)
        if match is None:
            continue
        try:
            normalized = date(
                int(match.group("date")), int(match.group("month")), int(match.group("day"))
            ).isoformat()
        except ValueError:
            continue
        return normalized, match.group(0)
    return None, None


def _clean_entity_candidate(value: str) -> str | None:
    """去掉日期和常见报道引导词后保留原句中的实体候选，不把泛称当成企业或基金。"""
    cleaned = re.sub(r"^(?:20\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?[，,、\s]*)", "", value)
    cleaned = re.sub(r"^(?:近日|日前|今日|消息称|据报道|记者获悉)[，,:：\s]*", "", cleaned)
    cleaned = cleaned.strip("，,:：;；()（） ")
    if len(cleaned) < 2 or cleaned in GENERIC_ENTITY_NAMES:
        return None
    return cleaned[:80]


def _entity_name_candidate(source_title: str, visible_text: str) -> str | None:
    """从标题和正文的事件句抽取实体候选，缺少明确句式时显式返回空。

    输入是已验证快照中的标题和可见正文，输出为一个原文中的实体候选或 ``None``。规则只
    接受“实体 + 明确事件动词”的短句，且拒绝“企业”“公司”等泛称；它不使用模型补全，
    不宣称该名称真实、有效或与徐州有关，也不创建正式线索。
    """
    quoted_title_match = TITLE_QUOTED_ENTITY_PATTERN.search(source_title.strip())
    if quoted_title_match is not None:
        title_entity = _clean_entity_candidate(quoted_title_match.group("entity"))
        if title_entity is not None:
            return title_entity
    for text in (source_title, visible_text):
        for match in ENTITY_EVENT_PATTERN.finditer(text):
            candidate = _clean_entity_candidate(match.group("entity"))
            if candidate is not None:
                return candidate
    return None


def _entity_kind_candidate(entity_name: str | None) -> str | None:
    """按实体候选中明示的组织词给出企业、基金或创业团队候选，信息不足时不猜测。

    输入是已校验观察快照中抽出的实体名称，输出仅是待确认的受控枚举或 ``None``。团队
    词优先于一般企业词处理，避免将明确写作“创业团队”的早期主体误归为企业；该结果仍
    不是主体登记事实，不能绕过草稿确认、重新抓取、原始证据签名或人工业务复核。
    """
    if entity_name is None:
        return None
    if any(term in entity_name for term in ("基金", "资本", "创投", "投资管理")):
        return "fund"
    if any(term in entity_name for term in ("创业团队", "团队")):
        return "startup_team"
    if any(term in entity_name for term in ("公司", "科技", "集团", "研究院", "工作室")):
        return "enterprise"
    return None


def _claim_type_candidate(source_title: str, visible_text: str) -> tuple[str | None, list[str]]:
    """按固定优先级映射页面明示词为允许的事实类别候选，未命中则待人工确认。

    输入是已验证的标题和正文，输出为 ``(类别候选, 命中词)``。输出只使用正式证据已有
    枚举，且“命中”不等于事实成立；函数不改变来源等级、评分或红色预警，也不写台账。
    """
    searchable = f"{source_title}\n{visible_text}".casefold()
    matched_terms: list[str] = []
    for term, claim_type in CLAIM_TYPE_TERM_RULES:
        if term.casefold() in searchable:
            matched_terms.append(term)
            return claim_type, matched_terms
    return None, matched_terms


def _excerpt_candidate(visible_text: str, matched_terms: list[str]) -> str | None:
    """选择包含固定命中词的一整句原文作为摘录候选，不拼接、改写或截断页面内容。

    输入是已校验快照正文和确定性词表命中，输出为一条不超过 1,200 字的原文句或 ``None``。
    过长句、无命中或无法明确分句时宁可留待人工补证，避免为了自动化拼造看似完整的摘录；
    该函数只在内存中读取文本，不写入正式证据或任何业务状态。
    """
    if not matched_terms:
        return None
    for match in SENTENCE_PATTERN.finditer(visible_text):
        sentence = match.group(0).strip()
        if (
            sentence
            and len(sentence) <= MAX_EVIDENCE_DRAFT_EXCERPT_CHARS
            and any(term.casefold() in sentence.casefold() for term in matched_terms)
        ):
            return sentence
    return None


def _needs_human_confirmation(draft: dict[str, object]) -> list[str]:
    """列出每条草稿仍必须由人工和正式抓取共同确认的项目，避免被当作自动结论。"""
    pending = missing_draft_confirmation_fields(draft)
    pending.append("重新受控抓取并校验来源当前正文、来源等级和正式证据字段")
    return pending


def missing_draft_confirmation_fields(draft: dict[str, object]) -> list[str]:
    """列出草稿缺失的关键可核验字段，供待补证业务判断使用而不补造任何事实。

    输入是已保存草稿，输出仅为缺失字段的中文说明。字符串为空白也视为缺失；函数不从
    搜索摘要、采集时间或模型推测补全实体、日期、类别或摘录，不读写台账、不改变草稿。
    """
    return [
        label
        for field_name, label in DRAFT_REQUIRED_CONFIRMATION_FIELDS
        if not isinstance(draft.get(field_name), str) or not str(draft.get(field_name)).strip()
    ]


def _public_evidence_draft(draft: dict[str, Any]) -> dict[str, object]:
    """返回人工可见的草稿元数据，不暴露本地路径或快照 Base64 原始字节。"""
    fields = (
        "draft_id",
        "observation_id",
        "candidate_id",
        "source_url",
        "source_title",
        "collected_at",
        "raw_sha256",
        "entity_name",
        "entity_kind",
        "source_published_at",
        "source_published_at_raw",
        "claim_type",
        "excerpt",
        "matched_terms",
        "needs_human_confirmation",
        "extractor_version",
        "created_at",
        "state",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in draft:
            raise ValueError(f"证据草稿缺少字段：{field_name}")
    return {field_name: draft[field_name] for field_name in fields}


def _public_draft_confirmation(audit: dict[str, Any]) -> dict[str, object]:
    """返回可审计的确认结果，不返回本地路径、页面原文或任何可改写草稿的字段。"""
    fields = (
        "confirmation_id",
        "draft_id",
        "candidate_id",
        "attempted_at",
        "confirmed_by_user_id",
        "review_note",
        "old_state",
        "new_state",
        "capture_attempted",
        "capture_result",
        "evidence_id",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in audit:
            raise ValueError(f"证据草稿确认审计缺少字段：{field_name}")
    result = {field_name: audit[field_name] for field_name in fields}
    # 0.41.1 adds optional, bounded failure context.  Older append-only audit
    # records deliberately remain valid and simply have no such fields.
    for field_name in ("verification_failure_reason", "verification_failure_stage"):
        if isinstance(audit.get(field_name), str) and audit[field_name]:
            result[field_name] = audit[field_name]
    return result


def _public_draft_business_review(audit: dict[str, Any]) -> dict[str, object]:
    """返回草稿业务判断审计，不包含、也不允许修改任何来源事实或快照内容。"""
    fields = (
        "review_id",
        "draft_id",
        "candidate_id",
        "reviewed_at",
        "reviewed_by_user_id",
        "old_state",
        "new_state",
        "review_note",
        "missing_evidence_fields",
        "operation_scope",
    )
    for field_name in fields:
        if field_name not in audit:
            raise ValueError(f"证据草稿业务判断审计缺少字段：{field_name}")
    return {field_name: audit[field_name] for field_name in fields}


def _confirmation_history_for_draft(
    ledger: dict[str, Any], *, draft_id: str
) -> list[dict[str, object]]:
    """按草稿 ID 读取确认历史，供只读视图显示每次尝试而不修改草稿或证据。"""
    selected = [
        audit
        for audit in _evidence_draft_confirmation_audits(ledger)
        if isinstance(audit, dict) and audit.get("draft_id") == draft_id
    ]
    selected.sort(key=lambda audit: str(audit.get("attempted_at", "")), reverse=True)
    return [_public_draft_confirmation(audit) for audit in selected[:50]]


def _confirmation_state(
    draft: dict[str, Any], confirmation_history: list[dict[str, object]]
) -> str:
    """根据追加审计计算只读确认状态，不回写或覆盖草稿原始抽取字段。"""
    for audit in confirmation_history:
        if audit.get("new_state") in {"confirmed_and_captured", "confirmed_with_existing_evidence"}:
            return str(audit["new_state"])
    return str(draft.get("state", DRAFT_STATE))


def _business_review_history_for_draft(
    ledger: dict[str, Any], *, draft_id: str
) -> list[dict[str, object]]:
    """读取同一草稿的追加业务判断历史，按最新在前展示而不覆盖任一旧判断。"""
    selected = [
        audit
        for audit in _evidence_draft_business_review_audits(ledger)
        if isinstance(audit, dict) and audit.get("draft_id") == draft_id
    ]
    selected.sort(key=lambda audit: str(audit.get("reviewed_at", "")), reverse=True)
    return [_public_draft_business_review(audit) for audit in selected[:50]]


def business_review_state_for_draft(ledger: dict[str, Any], *, draft_id: str) -> str:
    """计算草稿当前业务判断状态，状态来自最新追加审计而非改写自动抽取原记录。"""
    history = _business_review_history_for_draft(ledger, draft_id=draft_id)
    return str(history[0]["new_state"]) if history else DRAFT_STATE


def evidence_drafts_for_observation(
    ledger: dict[str, Any], *, observation_id: str
) -> list[dict[str, object]]:
    """从当前已读台账筛选某观察关联的草稿，供现有只读快照详情展示。

    输入为当前内存台账和服务器生成的观察 ID，输出为按生成时间倒序的公开草稿视图。函数
    不接收 URL、路径、用户 ID 或修改字段，不读取网络、不写台账、不确认草稿，更不会改写
    原始快照或 evidence；普通用户只能据此发起后续受控人工核验流程。
    """
    normalized_observation_id = _required_server_identifier(observation_id, "observation_id")
    selected = [
        draft
        for draft in _evidence_drafts(ledger)
        if isinstance(draft, dict) and draft.get("observation_id") == normalized_observation_id
    ]
    selected.sort(key=lambda draft: str(draft.get("created_at", "")), reverse=True)
    result: list[dict[str, object]] = []
    for draft in selected:
        public_draft = _public_evidence_draft(draft)
        history = _confirmation_history_for_draft(ledger, draft_id=str(draft["draft_id"]))
        business_history = _business_review_history_for_draft(
            ledger, draft_id=str(draft["draft_id"])
        )
        public_draft["confirmation_state"] = _confirmation_state(draft, history)
        public_draft["confirmation_history"] = history
        public_draft["business_review_state"] = (
            str(business_history[0]["new_state"]) if business_history else DRAFT_STATE
        )
        public_draft["business_review_history"] = business_history
        result.append(public_draft)
    return result


def generate_evidence_drafts_from_observations(workspace_dir: Path) -> dict[str, object]:
    """从已验证候选快照追加确定性待确认草稿，不联网且不改写任何业务或证据记录。

    输入是当前智能体受隔离工作区；输出为本轮追加、重复跳过、无资格跳过与快照校验失败
    的数量及审计摘要。函数只处理 ``pending_evidence_verification`` 的既有观察，逐条调用
    快照 SHA-256 校验后以固定规则抽取候选字段；任何校验异常只跳过该观察，绝不尝试修复
    或覆盖它。保存前逐字比对线索、正式证据、评分、反馈、候选和观察索引，确保唯一可变
    数据是独立的 ``evidence_drafts`` 追加列表。草稿状态始终为待人工确认，不能自动入证、
    创建/推进线索、改变红色预警或业务评分。
    """
    started_at = _utc_now()
    ledger = load_ledger(workspace_dir)
    protected_before = _protected_records_snapshot(ledger)
    _, observations = _candidate_observation_lists(ledger)
    drafts = _evidence_drafts(ledger)
    existing_draft_ids = {
        draft.get("draft_id") for draft in drafts if isinstance(draft, dict) and isinstance(draft.get("draft_id"), str)
    }
    eligible_observations = sorted(
        (
            observation
            for observation in observations
            if isinstance(observation, dict)
            and observation.get("state") == "pending_evidence_verification"
        ),
        key=lambda observation: str(observation.get("collected_at", "")),
    )
    generated_drafts: list[dict[str, object]] = []
    skipped_duplicates = 0
    skipped_invalid_snapshots = 0
    skipped_quota = 0

    for observation in eligible_observations:
        observation_id = observation.get("observation_id")
        raw_sha256 = observation.get("raw_sha256")
        if not isinstance(observation_id, str) or not isinstance(raw_sha256, str):
            skipped_invalid_snapshots += 1
            continue
        draft_id = _stable_id(
            "evidence-draft",
            {
                "observation_id": observation_id,
                "raw_sha256": raw_sha256,
                "extractor_version": EXTRACTOR_VERSION,
            },
        )
        if draft_id in existing_draft_ids:
            skipped_duplicates += 1
            continue
        if len(drafts) + len(generated_drafts) >= MAX_EVIDENCE_DRAFT_RECORDS:
            skipped_quota += 1
            continue
        if len(generated_drafts) >= MAX_EVIDENCE_DRAFTS_PER_RUN:
            skipped_quota += 1
            continue
        try:
            snapshot = _read_verified_observation_snapshot(workspace_dir, observation)
        except (OSError, RuntimeError, ValueError):
            # 快照被篡改或损坏时保持原样，并留下计数供监控审计；绝不依据不可信正文生成草稿。
            skipped_invalid_snapshots += 1
            continue
        visible_text = snapshot.get("visible_text")
        source_title = snapshot.get("source_title")
        candidate_id = observation.get("candidate_id")
        source_url = observation.get("source_url")
        collected_at = observation.get("collected_at")
        if not all(
            isinstance(value, str)
            for value in (visible_text, source_title, candidate_id, source_url, collected_at)
        ):
            skipped_invalid_snapshots += 1
            continue
        claim_type, matched_terms = _claim_type_candidate(source_title, visible_text)
        entity_name = _entity_name_candidate(source_title, visible_text)
        source_published_at, source_published_at_raw = _source_date_candidate(visible_text)
        draft: dict[str, object] = {
            "draft_id": draft_id,
            "observation_id": observation_id,
            "candidate_id": candidate_id,
            "source_url": source_url,
            "source_title": source_title,
            "collected_at": collected_at,
            "raw_sha256": raw_sha256,
            "entity_name": entity_name,
            "entity_kind": _entity_kind_candidate(entity_name),
            "source_published_at": source_published_at,
            "source_published_at_raw": source_published_at_raw,
            "claim_type": claim_type,
            "excerpt": _excerpt_candidate(visible_text, matched_terms),
            "matched_terms": matched_terms,
            "extractor_version": EXTRACTOR_VERSION,
            "created_at": _utc_now(),
            "state": DRAFT_STATE,
            "operation_scope": DRAFT_OPERATION_SCOPE,
        }
        draft["needs_human_confirmation"] = _needs_human_confirmation(draft)
        generated_drafts.append(draft)
        existing_draft_ids.add(draft_id)

    _assert_protected_records_unchanged(protected_before, ledger)
    if generated_drafts:
        drafts.extend(generated_drafts)
        _assert_protected_records_unchanged(protected_before, ledger)
        saved_path = save_ledger(workspace_dir, ledger)
    else:
        saved_path = ledger_path(workspace_dir)
    run = {
        "started_at": started_at,
        "completed_at": _utc_now(),
        "generated_draft_count": len(generated_drafts),
        "skipped_duplicate_count": skipped_duplicates,
        "skipped_invalid_snapshot_count": skipped_invalid_snapshots,
        "skipped_quota_count": skipped_quota,
        "draft_record_quota": MAX_EVIDENCE_DRAFT_RECORDS,
        "status": "completed",
        "operation_scope": DRAFT_OPERATION_SCOPE,
    }
    return {
        "run": run,
        "drafts": [_public_evidence_draft(draft) for draft in generated_drafts],
        "ledger_path": str(saved_path),
    }
