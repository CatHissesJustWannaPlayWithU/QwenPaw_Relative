"""自动候选页面快照与待补证词表信号。

本模块位于“自动发现候选”和“人工证据核验”之间：它只对 robots 允许的少量候选保存
原始页面快照，并以固定词表提示人工优先复核。它也提供仅从当前工作区读取的查看服务，
用于核验快照哈希和可见正文。任何命中都不是事实证据，不能创建或推进线索、修改评分、
触发 LeadRecord 的红色预警，也不能覆盖已有 evidence。
"""

from __future__ import annotations

import base64
import copy
import difflib
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .candidate_review import candidate_geographic_scope, invalid_discovery_candidate_ids, _latest_reviews_by_candidate
from .source_capture import MAX_RESPONSE_BYTES, fetch_public_source_for_observation
from .source_feed_discovery import check_robots_access
from .source_monitoring import (
    apply_source_health_outcome,
    is_source_eligible,
    policy_blocked_page_domains,
    source_health_key_for_url,
)
from .storage import (
    candidate_observation_snapshot_path,
    ledger_path,
    load_ledger,
    save_candidate_observation_snapshot,
    save_ledger,
)


# 每轮后台任务至多读取三个候选，并要求来自不同域名；这是低频公开观察而非批量爬取。
MAX_AUTOMATIC_SNAPSHOTS_PER_MONITOR = 3
MAX_CANDIDATE_OBSERVATION_ERROR_LENGTH = 500
MAX_CANDIDATE_OBSERVATION_LIST_LIMIT = 100
MAX_RETURN_VISIBLE_TEXT_CHARS = 12_000
# 原始快照包含 Base64 原文和可见文本，实际 JSON 可能明显大于 1 MB 原始响应；先按五倍
# 响应上限预留容量，确保不会为了计算精确大小而先下载一个无处可保存的页面。
MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RESERVATION_BYTES = MAX_RESPONSE_BYTES * 5
# 这两项硬限制防止后台低频任务长期运行时无限累积本地原始页面。达到限制后保留历史
# 快照且不联网抓取新页面，具体数值集中为常量，后续可经业务评审统一调整。
MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RECORDS = 1_000
MAX_CANDIDATE_OBSERVATION_SNAPSHOT_STORAGE_BYTES = 256 * 1024 * 1024
# 详情中的页面差异只比较有限正文，避免超大公开页面使纯只读人工查看变成高 CPU/内存任务。
# 原始快照仍完整保留并可逐字校验；这里的截断只影响“复核提示摘要”，绝不影响证据链。
MAX_CHANGE_SUMMARY_INPUT_CHARS = 24_000
MAX_CHANGE_SUMMARY_SEGMENTS = 8
MAX_CHANGE_SUMMARY_SEGMENT_CHARS = 400
# 已观察候选至少间隔 24 小时才允许再次访问。新发现候选不受此延迟影响；定时任务仍受
# 每轮三站点、robots、来源健康度和快照配额共同约束，避免“持续监控”退化为高频抓取。
MIN_CANDIDATE_REOBSERVATION_INTERVAL = timedelta(hours=24)
# 新门槛上线前的候选没有“中国/徐州业务机会”评估信息。为避免历史积压项继续被后台抓取，
# 它们会被追加式地标为范围外；原始候选和快照均不删除、不覆盖。
CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION = "china_xuzhou_business_opportunity_gate_v1"

# 词表的输出是“某页面出现了这些词”，不是对企业现实状态的断言。``high_priority_review``
# 仅表示人工应优先查看原始快照，不是红色企业风险预警；真正的红色预警只能由正式证据
# 在 core.py 中按严格风险规则创建，避免把融资、扩产等招商机会误标为风险。
PENDING_EVIDENCE_SIGNAL_RULES: tuple[dict[str, object], ...] = (
    {
        "signal_key": "fundraising_need_terms",
        "label": "融资需求词命中",
        "terms": ("正在融资", "融资需求", "寻求融资", "紧急融资", "启动募资", "融资中"),
        "review_priority": "high_priority_review",
    },
    {
        "signal_key": "financing_event_terms",
        "label": "融资或募资词命中",
        "terms": ("完成融资", "获得融资", "新一轮融资", "完成募资", "获得投资"),
        "review_priority": "high_priority_review",
    },
    {
        "signal_key": "capacity_expansion_terms",
        "label": "扩产或建设词命中",
        "terms": ("扩产", "扩建", "新建生产线", "新增产线", "投产", "开工"),
        "review_priority": "high_priority_review",
    },
    {
        "signal_key": "landing_window_terms",
        "label": "招商或落地窗口词命中",
        "terms": ("招商", "落地", "选址", "产业园", "项目签约", "寻找合作伙伴"),
        "review_priority": "high_priority_review",
    },
    {
        "signal_key": "enterprise_risk_terms",
        "label": "明确企业风险词命中",
        "terms": (
            "资金链紧张",
            "流动性紧张",
            "融资失败",
            "项目暂停",
            "终止合作",
            "停产",
            "破产重整",
            "行政处罚",
            "核心团队离职",
        ),
        "review_priority": "high_priority_review",
    },
    {
        "signal_key": "cooperation_change_terms",
        "label": "合作变化词命中",
        "terms": ("战略合作", "签署合作", "终止合作", "合作调整"),
        "review_priority": "normal_review",
    },
)


def _utc_now() -> str:
    """返回由服务器产生的 UTC 采集时间，不接受候选、模型或前端指定的时间。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """依据服务器已有候选、抓取哈希和时间生成观察 ID，避免调用方指定快照身份。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:16]}"


def _pending_evidence_signals(source_title: str, visible_text: str) -> list[dict[str, object]]:
    """以固定词表标记页面中的待补证线索，不从文字推导企业事实或评分。

    输入是本次受控抓取的标题和可见正文，输出是匹配规则、实际词语和人工复核优先级。
    只有明确出现的词才会返回；没有命中时输出空列表。函数不联网、不写台账、不创建
    evidence、不变更线索，也不使用模型推理，因此“命中”始终只是待人工核验的提示。
    """
    searchable_text = f"{source_title}\n{visible_text}".casefold()
    signals: list[dict[str, object]] = []
    for rule in PENDING_EVIDENCE_SIGNAL_RULES:
        terms = rule["terms"]
        if not isinstance(terms, tuple):
            raise RuntimeError("待补证词表规则格式异常")
        matched_terms = [term for term in terms if isinstance(term, str) and term.casefold() in searchable_text]
        if matched_terms:
            signals.append(
                {
                    "signal_key": rule["signal_key"],
                    "label": rule["label"],
                    "matched_terms": matched_terms,
                    "state": "pending_evidence_verification",
                    "review_priority": rule["review_priority"],
                    "reason": "页面原始快照命中固定待补证词表，须人工核验实体、时间和原始摘录。",
                }
            )
    return signals


def _review_priority_for_signals(signals: list[dict[str, object]]) -> str:
    """把词表命中汇总为人工队列优先级，不生成企业红色风险预警或业务状态。"""
    return (
        "high_priority_review"
        if any(signal.get("review_priority") == "high_priority_review" for signal in signals)
        else "normal_review"
    )


def _candidate_observation_lists(
    ledger: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """取得候选与观察审计列表，结构异常时停止而不猜测或修复台账内容。"""
    candidates = ledger.get("discovery_candidates")
    observations = ledger.get("candidate_observations")
    if not isinstance(candidates, list) or not isinstance(observations, list):
        raise ValueError("招商线索台账中的候选观察字段必须是列表")
    return candidates, observations


def _candidate_scope_exclusion_list(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得候选范围排除审计列表，异常结构时停止而不删除任何历史候选或快照。

    输入是已通过整本 HMAC 校验的内存台账，输出是只追加的排除审计列表。记录的作用是
    说明某候选为何不再自动抓取或展示，不修改 ``discovery_candidates``、候选快照、正式
    evidence、线索、评分或人工判断；因此旧 RS 等页面仍可按原始哈希留存供审计。
    """
    exclusions = ledger.get("candidate_scope_exclusions")
    if not isinstance(exclusions, list):
        raise ValueError("招商线索台账中的 candidate_scope_exclusions 必须是列表")
    return exclusions


def _automatic_observation_exclusion_reason(candidate: dict[str, Any]) -> str | None:
    """判断候选是否缺少自动观察所需的严格范围评估，不把结果改写为企业事实。

    输入是一条服务器保存的发现候选，输出为空表示可参与自动队列，或返回固定排除原因。
    旧候选无评估字段时采用失败关闭：它可能仍可由人工按正式证据流程重新核验，但不能
    继续消耗后台访问预算。函数只读内存、不联网、不保存，也不改变候选自身状态。
    """
    assessment = candidate.get("eligibility_assessment")
    if not isinstance(assessment, dict):
        return "legacy_candidate_missing_china_xuzhou_scope_assessment"
    if assessment.get("gate_version") != CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION:
        return "candidate_scope_assessment_version_not_accepted"
    if assessment.get("eligible") is not True:
        return "candidate_failed_china_xuzhou_business_opportunity_gate"
    return None


def _scope_exclusion_id(
    *,
    target_kind: str,
    candidate_id: str,
    observation_id: str | None,
) -> str:
    """为同一候选或快照生成稳定排除审计 ID，防止每轮监控重复写入相同结论。"""
    return _stable_id(
        "candidate-scope-exclusion",
        {
            "rule_version": CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION,
            "target_kind": target_kind,
            "candidate_id": candidate_id,
            "observation_id": observation_id,
        },
    )


def _reconcile_candidate_scope_exclusions(
    ledger: dict[str, Any],
    *,
    excluded_at: str,
) -> dict[str, object]:
    """为不符合新范围门槛的历史候选和快照追加不可覆盖的排除审计。

    输入是服务器生成的排除时间，输出为已排除候选/快照 ID 和本轮新增审计数。函数绝不
    修改候选记录、观察记录或快照文件：它只在独立列表追加“自动队列不再使用”的原因。
    因此可立即清除无关页面对招商工作台的干扰，同时保留 URL、采集时间、原始字节和哈希
    以便后来审计；该自动排除也不等于对企业中国业务、投资意愿或风险做出事实结论。
    """
    candidates, observations = _candidate_observation_lists(ledger)
    exclusions = _candidate_scope_exclusion_list(ledger)
    existing_ids = {
        item.get("exclusion_id")
        for item in exclusions
        if isinstance(item, dict) and isinstance(item.get("exclusion_id"), str)
    }
    excluded_candidate_ids: set[str] = set()
    candidate_reasons: dict[str, str] = {}
    new_exclusion_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        reason = _automatic_observation_exclusion_reason(candidate)
        if reason is None:
            continue
        excluded_candidate_ids.add(candidate_id)
        candidate_reasons[candidate_id] = reason
        exclusion_id = _scope_exclusion_id(
            target_kind="discovery_candidate",
            candidate_id=candidate_id,
            observation_id=None,
        )
        if exclusion_id in existing_ids:
            continue
        exclusions.append(
            {
                "exclusion_id": exclusion_id,
                "target_kind": "discovery_candidate",
                "candidate_id": candidate_id,
                "observation_id": None,
                "excluded_at": excluded_at,
                "rule_version": CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION,
                "exclusion_reason": reason,
                "operation_scope": "server_side_candidate_scope_exclusion_audit_only",
            }
        )
        existing_ids.add(exclusion_id)
        new_exclusion_count += 1
    excluded_observation_ids: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        observation_id = observation.get("observation_id")
        candidate_id = observation.get("candidate_id")
        if not isinstance(observation_id, str) or not isinstance(candidate_id, str):
            continue
        reason = candidate_reasons.get(candidate_id)
        if reason is None:
            continue
        excluded_observation_ids.add(observation_id)
        exclusion_id = _scope_exclusion_id(
            target_kind="candidate_observation",
            candidate_id=candidate_id,
            observation_id=observation_id,
        )
        if exclusion_id in existing_ids:
            continue
        exclusions.append(
            {
                "exclusion_id": exclusion_id,
                "target_kind": "candidate_observation",
                "candidate_id": candidate_id,
                "observation_id": observation_id,
                "excluded_at": excluded_at,
                "rule_version": CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION,
                "exclusion_reason": reason,
                "operation_scope": "server_side_candidate_scope_exclusion_audit_only",
            }
        )
        existing_ids.add(exclusion_id)
        new_exclusion_count += 1
    return {
        "excluded_candidate_ids": excluded_candidate_ids,
        "excluded_observation_ids": excluded_observation_ids,
        "new_exclusion_count": new_exclusion_count,
    }


def _excluded_observation_ids(ledger: dict[str, Any]) -> set[str]:
    """从已追加审计中读取默认隐藏的快照 ID，不写台账也不暴露本地原始文件路径。"""
    exclusions = _candidate_scope_exclusion_list(ledger)
    return {
        item["observation_id"]
        for item in exclusions
        if isinstance(item, dict)
        and item.get("target_kind") == "candidate_observation"
        and isinstance(item.get("observation_id"), str)
    }


def _scope_exclusion_for_observation(
    ledger: dict[str, Any],
    *,
    observation: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, object] | None:
    """返回快照被自动隐藏的审计原因；没有排除时返回空且绝不写入台账。

    输入是已定位的观察、对应候选列表和可信台账，输出仅为范围治理元数据。优先读取已
    追加的审计记录；在下一次后台任务尚未写入审计前，旧候选也会按同一失败关闭规则被
    临时隐藏，避免新代码部署到下次定时任务之间继续展示无关页面。该函数不读取快照正文、
    不删除任何记录，也不将“被排除”描述成企业事实。
    """
    observation_id = observation.get("observation_id")
    candidate_id = observation.get("candidate_id")
    if not isinstance(observation_id, str) or not isinstance(candidate_id, str):
        return None
    exclusions = _candidate_scope_exclusion_list(ledger)
    for item in reversed(exclusions):
        if (
            isinstance(item, dict)
            and item.get("target_kind") == "candidate_observation"
            and item.get("observation_id") == observation_id
        ):
            return {
                "excluded": True,
                "exclusion_reason": item.get("exclusion_reason"),
                "excluded_at": item.get("excluded_at"),
                "rule_version": item.get("rule_version"),
                "audit_recorded": True,
            }
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if isinstance(candidate, dict) and (
        reason := _automatic_observation_exclusion_reason(candidate)
    ) is not None:
        return {
            "excluded": True,
            "exclusion_reason": reason,
            "excluded_at": None,
            "rule_version": CANDIDATE_SCOPE_EXCLUSION_RULE_VERSION,
            "audit_recorded": False,
        }
    return None


def _existing_snapshot_storage_bytes(
    workspace_dir: Path,
    observations: list[dict[str, Any]],
) -> int:
    """统计已入账快照的真实文件大小，并拒绝缺失或越界的历史快照索引。

    输入是当前受平台隔离的工作区和台账中的观察记录，输出为固定快照目录中所有已登记
    文件的实际字节总量。路径只由服务器生成的 observation_id 推导，并逐项比对台账保存
    的相对路径，不能相信可编辑 JSON 中的任意路径；缺失、符号链接或不一致时直接失败，
    避免误把损坏台账当作零占用继续抓取。函数只读取元数据，不修改快照、证据或业务状态。
    """
    total_bytes = 0
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValueError("候选观察记录必须是对象")
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str):
            raise ValueError("候选观察记录缺少有效 observation_id")
        expected_path = candidate_observation_snapshot_path(workspace_dir, observation_id)
        expected_relative_path = str(expected_path.relative_to(workspace_dir))
        if observation.get("snapshot_relative_path") != expected_relative_path:
            raise ValueError("候选观察快照路径与服务器规则不一致")
        if (
            expected_path.parent.is_symlink()
            or expected_path.is_symlink()
            or not expected_path.is_file()
        ):
            raise ValueError("候选观察快照文件缺失或不是普通文件")
        snapshot_size = expected_path.stat().st_size
        if snapshot_size < 0:
            raise ValueError("候选观察快照文件大小异常")
        total_bytes += snapshot_size
    return total_bytes


def _serialized_snapshot_storage_bytes(payload: dict[str, object]) -> int:
    """计算将要写入的快照 JSON 精确大小，用于在落盘前执行容量配额判断。

    输入是即将交给固定快照写入器的服务器构造载荷，输出为与写入器相同 JSON 格式的 UTF-8
    字节数。此函数不落盘、不读取网络、不修改候选或证据；其唯一目的是真实评估 Base64
    原文和可见正文占用，保证达到容量限制时不会新增无法审查的部分写入。
    """
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return len((serialized + "\n").encode("utf-8"))


def _remove_uncommitted_snapshot_files(snapshot_paths: list[Path]) -> None:
    """仅清理由本轮刚创建但未能写入台账索引的快照，避免并发冲突留下孤儿文件。

    输入是当前函数在内存中记录的新快照固定路径，输出为空。只有最终 ``save_ledger``
    失败时调用；路径不来自台账、URL 或调用方，且符号链接一律不删除，因此不会清理
    任何既有历史快照或原始证据。清理失败不掩盖原始保存异常，后续管理员可按固定目录
    审计残留文件。
    """
    for path in snapshot_paths:
        try:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            # 原始台账保存冲突才是需要反馈给调用方的根因，清理仅为尽力而为的资源回收。
            continue


def _protected_business_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """序列化自动观察绝不能触碰的业务记录，供结束前逐字防篡改校验。"""
    protected_fields = ("leads", "evidence", "score_snapshots", "outcome_feedback")
    snapshot: dict[str, str] = {}
    for field_name in protected_fields:
        value = ledger.get(field_name)
        if not isinstance(value, list):
            raise ValueError(f"招商线索台账中的 {field_name} 必须是列表")
        snapshot[field_name] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return snapshot


def _assert_protected_business_records_unchanged(
    before: dict[str, str], ledger: dict[str, Any]
) -> None:
    """确保观察任务只补候选快照元数据，绝不写入或覆盖证据和业务判断。"""
    after = _protected_business_snapshot(ledger)
    if after != before:
        raise RuntimeError("自动候选观察不得修改线索、原始证据、评分或结果反馈")


def _parse_server_utc_timestamp(value: object) -> datetime | None:
    """解析候选先前由服务器生成的 UTC 观察时间，格式异常时返回空而不猜测时间。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _select_observation_candidates(
    candidates: list[dict[str, Any]],
    *,
    selected_at: str,
    blocked_hostnames: set[str] | None = None,
    excluded_candidate_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """选择首次观察或间隔已满的候选，限制同域名与每轮访问量。

    输入是服务器发现候选、本轮服务器时间和当前被政策禁止的域名集合，输出为至多三个
    不同域名的 ``discovered`` 候选。首次观察优先按发现时间倒序，确保新发现的合格页面
    不会被旧积压项堵住；已有快照的候选仅在上次观察满 24 小时后才可再抓，时间异常则
    保守跳过。``policy_blocked`` 域名及范围排除候选不参与选择：robots、证书、访问政策
    或中国/徐州业务信息不足都不能反复挤占每轮三个域名的预算。已 ``captured`` 的候选不
    在此处重抓，因为它的多次正式入证需独立规则；函数不联网、不写台账、不创建证据、
    不改变线索状态或人工判断。
    """
    selected_datetime = _parse_server_utc_timestamp(selected_at)
    if selected_datetime is None:
        raise RuntimeError("候选观察选择时间格式异常")
    selected: list[dict[str, Any]] = []
    observed_domains: set[str] = set()
    initial_candidates: list[dict[str, Any]] = []
    reobservation_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("state") != "discovered":
            continue
        candidate_id = candidate.get("candidate_id")
        if isinstance(candidate_id, str) and excluded_candidate_ids and candidate_id in excluded_candidate_ids:
            continue
        if not candidate.get("last_observation_id"):
            initial_candidates.append(candidate)
            continue
        last_observed_at = _parse_server_utc_timestamp(candidate.get("last_observed_at"))
        if (
            last_observed_at is not None
            and selected_datetime - last_observed_at >= MIN_CANDIDATE_REOBSERVATION_INTERVAL
        ):
            reobservation_candidates.append(candidate)
    eligible_candidates = sorted(
        initial_candidates,
        key=lambda candidate: str(candidate.get("discovered_at", "")),
        reverse=True,
    ) + sorted(
        reobservation_candidates,
        key=lambda candidate: str(candidate.get("last_observed_at", "")),
        reverse=True,
    )
    for candidate in eligible_candidates:
        source_url = candidate.get("source_url")
        if not isinstance(source_url, str):
            continue
        hostname = (urlparse(source_url).hostname or "").casefold()
        if not hostname or hostname in observed_domains:
            continue
        if blocked_hostnames and hostname in blocked_hostnames:
            continue
        selected.append(candidate)
        observed_domains.add(hostname)
        if len(selected) >= MAX_AUTOMATIC_SNAPSHOTS_PER_MONITOR:
            break
    return selected


def _latest_observation_by_candidate(
    observations: list[dict[str, Any]], candidate_id: str
) -> dict[str, Any] | None:
    """定位候选最近一次已存观察，仅供判断当前页面哈希是否变化而不修改历史快照。"""
    matches = [
        observation
        for observation in observations
        if isinstance(observation, dict) and observation.get("candidate_id") == candidate_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda observation: str(observation.get("collected_at", "")), reverse=True)
    return matches[0]


def _record_observation_attempt(
    candidate: dict[str, Any],
    *,
    observed_at: str,
    error: str | None,
    observation_id: str | None,
) -> None:
    """更新候选的可再生观察结果，不改变其抓取状态、证据引用或人工复核状态。"""
    attempts = candidate.get("observation_attempts", 0)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError("候选观察次数格式异常")
    candidate["observation_attempts"] = attempts + 1
    candidate["last_observed_at"] = observed_at
    candidate["last_observation_error"] = error
    if observation_id is not None:
        candidate["last_observation_id"] = observation_id


def observe_discovery_candidates(workspace_dir: Path) -> dict[str, Any]:
    """对少量 robots 允许的候选保存原始快照并生成待补证提示。

    输入是当前智能体工作区；输出包括新观察记录数量、被跳过的来源和追加式审计摘要。
    仅服务器先前发现的候选 URL 可以进入本函数，且每轮上限三个不同域名。每个页面先
    检查 robots，再由受控抓取器读取，原始字节以 Base64 与哈希写入不可覆盖快照；若
    访问受限则记录健康度并停止该来源。快照数或文件总量达到硬上限时，在联网前跳过
    候选，保留历史快照而不删除或覆盖。命中词表只得到 ``pending_evidence_verification``
    和人工优先级，绝不写入 evidence、创建/推进线索、改变评分或 Lead 红色预警；函数
    保存前会逐字核验这些受保护业务记录保持不变。
    """
    started_at = _utc_now()
    ledger = load_ledger(workspace_dir)
    # 仅用于判断本轮是否真的改变了候选观察或来源健康度。若没有合格候选、尚未到重访间隔
    # 或全部仅被跳过，则不重存相同台账，避免无业务变化时递增版本并制造并发保存冲突。
    # 该副本不参与任何业务判断，也不会被写回，因此不能成为覆盖原始证据链的通道。
    ledger_before_run = copy.deepcopy(ledger)
    protected_before = _protected_business_snapshot(ledger)
    candidates, observations = _candidate_observation_lists(ledger)
    selection_at = _utc_now()
    # 先用服务器固定规则为历史积压项追加范围排除审计，再挑选本轮页面。该步骤不改写
    # 原候选或快照，既能让 RS 等无关页面立即退出工作队列，也能阻止旧队列继续挤占预算。
    scope_reconciliation = _reconcile_candidate_scope_exclusions(
        ledger,
        excluded_at=selection_at,
    )
    excluded_candidate_ids = scope_reconciliation["excluded_candidate_ids"]
    if not isinstance(excluded_candidate_ids, set):
        raise RuntimeError("候选范围排除结果格式异常")
    # “无效”是认证用户的业务判断而非自动范围结论：它不删除候选、快照或证据，只停止
    # 后续对同一候选的自动页面访问，避免已明确排除的材料继续占用免费抓取预算。
    business_invalid_candidate_ids = invalid_discovery_candidate_ids(ledger)
    selected_candidates = _select_observation_candidates(
        candidates,
        selected_at=selection_at,
        blocked_hostnames=policy_blocked_page_domains(ledger),
        excluded_candidate_ids=excluded_candidate_ids | business_invalid_candidate_ids,
    )
    observed_records: list[dict[str, Any]] = []
    unchanged_content_candidates: list[str] = []
    skipped_candidates: list[dict[str, str]] = []
    failed_candidates: list[dict[str, str]] = []
    uncommitted_snapshot_paths: list[Path] = []
    existing_snapshot_storage_bytes = _existing_snapshot_storage_bytes(workspace_dir, observations)
    snapshot_storage_bytes = existing_snapshot_storage_bytes
    if (
        len(observations) >= MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RECORDS
        or snapshot_storage_bytes + MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RESERVATION_BYTES
        > MAX_CANDIDATE_OBSERVATION_SNAPSHOT_STORAGE_BYTES
    ):
        # 容量限制属于工作区资源治理，不把它伪装成候选抓取失败，也不增加访问次数。
        skipped_candidates.extend(
            {
                "candidate_id": str(candidate["candidate_id"]),
                "reason": "observation_snapshot_storage_quota_reached",
            }
            for candidate in selected_candidates
            if isinstance(candidate.get("candidate_id"), str)
        )
        selected_candidates = []

    for candidate in selected_candidates:
        candidate_id = candidate.get("candidate_id")
        source_url = candidate.get("source_url")
        if not isinstance(candidate_id, str) or not isinstance(source_url, str):
            continue
        observed_at = _utc_now()
        source_key = source_health_key_for_url(source_url)
        if not is_source_eligible(ledger, source_key=source_key, observed_at=observed_at):
            skipped_candidates.append(
                {"candidate_id": candidate_id, "reason": "source_health_cooldown_or_policy_blocked"}
            )
            continue

        try:
            robots_result = check_robots_access(source_url)
            if not robots_result.get("allowed"):
                error = f"robots policy: {robots_result.get('reason', 'denied')}"
                _record_observation_attempt(
                    candidate,
                    observed_at=observed_at,
                    error=error[:MAX_CANDIDATE_OBSERVATION_ERROR_LENGTH],
                    observation_id=None,
                )
                apply_source_health_outcome(
                    ledger,
                    source_key=source_key,
                    success=False,
                    error=error,
                    observed_at=observed_at,
                )
                skipped_candidates.append({"candidate_id": candidate_id, "reason": "robots_disallow"})
                continue

            snapshot = fetch_public_source_for_observation(source_url)
            raw_content = snapshot.get("raw_content")
            source_title = snapshot.get("source_title")
            visible_text = snapshot.get("visible_text")
            final_url = snapshot.get("source_url")
            raw_sha256 = snapshot.get("raw_sha256")
            content_type = snapshot.get("content_type")
            byte_size = snapshot.get("byte_size")
            if not (
                isinstance(raw_content, bytes)
                and isinstance(source_title, str)
                and isinstance(visible_text, str)
                and isinstance(final_url, str)
                and isinstance(raw_sha256, str)
                and isinstance(content_type, str)
                and isinstance(byte_size, int)
                and not isinstance(byte_size, bool)
                and byte_size >= 0
            ):
                raise RuntimeError("候选观察页面响应格式异常")
            if hashlib.sha256(raw_content).hexdigest() != raw_sha256:
                raise RuntimeError("候选观察页面哈希与原始字节不一致")
            if byte_size != len(raw_content):
                raise RuntimeError("候选观察页面字节数与原始字节不一致")

            previous_observation = _latest_observation_by_candidate(observations, candidate_id)
            previous_observation_id = (
                previous_observation.get("observation_id")
                if isinstance(previous_observation, dict)
                else None
            )
            previous_raw_sha256 = (
                previous_observation.get("raw_sha256")
                if isinstance(previous_observation, dict)
                else None
            )
            if previous_observation is not None and previous_raw_sha256 == raw_sha256:
                # 相同原始字节不重复落盘、不生成新草稿。只更新候选的可再生检查时间与健康度，
                # 让下个 24 小时间隔从本次成功检查起算，历史观察及其任何人工审计保持不变。
                _record_observation_attempt(
                    candidate,
                    observed_at=_utc_now(),
                    error=None,
                    observation_id=(
                        previous_observation_id if isinstance(previous_observation_id, str) else None
                    ),
                )
                apply_source_health_outcome(
                    ledger,
                    source_key=source_key,
                    success=True,
                    observed_at=_utc_now(),
                )
                unchanged_content_candidates.append(candidate_id)
                continue

            collected_at = _utc_now()
            observation_id = _stable_id(
                "candidate-observation",
                {
                    "candidate_id": candidate_id,
                    "collected_at": collected_at,
                    "raw_sha256": raw_sha256,
                },
            )
            signals = _pending_evidence_signals(source_title, visible_text)
            snapshot_payload: dict[str, object] = {
                "observation_id": observation_id,
                "candidate_id": candidate_id,
                "source_url": final_url,
                "source_title": source_title,
                "content_type": content_type,
                "collected_at": collected_at,
                "raw_sha256": raw_sha256,
                "byte_size": byte_size,
                "visible_text": visible_text,
                "raw_content_base64": base64.b64encode(raw_content).decode("ascii"),
                "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
            }
            serialized_snapshot_bytes = _serialized_snapshot_storage_bytes(snapshot_payload)
            if (
                len(observations) >= MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RECORDS
                or snapshot_storage_bytes + serialized_snapshot_bytes
                > MAX_CANDIDATE_OBSERVATION_SNAPSHOT_STORAGE_BYTES
            ):
                # 即使页面在本轮已读取，也不允许为它写入超限的半份历史记录或伪造失败。
                skipped_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "reason": "observation_snapshot_storage_quota_reached",
                    }
                )
                continue
            snapshot_path = save_candidate_observation_snapshot(
                workspace_dir,
                observation_id=observation_id,
                payload=snapshot_payload,
            )
            uncommitted_snapshot_paths.append(snapshot_path)
            observation = {
                "observation_id": observation_id,
                "candidate_id": candidate_id,
                "source_url": final_url,
                "source_title": source_title,
                "content_type": content_type,
                "collected_at": collected_at,
                "raw_sha256": raw_sha256,
                "byte_size": byte_size,
                "snapshot_storage_bytes": serialized_snapshot_bytes,
                "snapshot_relative_path": str(snapshot_path.relative_to(workspace_dir)),
                "signals": signals,
                "review_priority": _review_priority_for_signals(signals),
                "state": "pending_evidence_verification",
                "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
                "observation_kind": (
                    "initial" if previous_observation is None else "content_changed"
                ),
                "previous_observation_id": (
                    previous_observation_id if isinstance(previous_observation_id, str) else None
                ),
                "previous_raw_sha256": (
                    previous_raw_sha256 if isinstance(previous_raw_sha256, str) else None
                ),
            }
            observations.append(observation)
            snapshot_storage_bytes += serialized_snapshot_bytes
            _record_observation_attempt(
                candidate,
                observed_at=collected_at,
                error=None,
                observation_id=observation_id,
            )
            apply_source_health_outcome(
                ledger,
                source_key=source_key,
                success=True,
                observed_at=collected_at,
            )
            observed_records.append(observation)
        except (OSError, RuntimeError, ValueError) as exc:
            error = str(exc)[:MAX_CANDIDATE_OBSERVATION_ERROR_LENGTH]
            _record_observation_attempt(
                candidate,
                observed_at=observed_at,
                error=error,
                observation_id=None,
            )
            apply_source_health_outcome(
                ledger,
                source_key=source_key,
                success=False,
                error=error,
                observed_at=observed_at,
            )
            failed_candidates.append({"candidate_id": candidate_id, "error": error})

    _assert_protected_business_records_unchanged(protected_before, ledger)
    completed_at = _utc_now()
    run = {
        "started_at": started_at,
        "completed_at": completed_at,
        "selected_candidate_count": len(selected_candidates),
        "scope_excluded_candidate_count": len(excluded_candidate_ids),
        "business_invalid_candidate_count": len(business_invalid_candidate_ids),
        "scope_excluded_observation_count": len(scope_reconciliation["excluded_observation_ids"]),
        "new_scope_exclusion_audit_count": scope_reconciliation["new_exclusion_count"],
        "observation_count": len(observed_records),
        "initial_observation_count": sum(
            1 for record in observed_records if record.get("observation_kind") == "initial"
        ),
        "content_changed_observation_count": sum(
            1 for record in observed_records if record.get("observation_kind") == "content_changed"
        ),
        "unchanged_content_candidate_count": len(unchanged_content_candidates),
        "unchanged_content_candidate_ids": unchanged_content_candidates,
        "high_priority_observation_count": sum(
            1 for record in observed_records if record["review_priority"] == "high_priority_review"
        ),
        "skipped_candidates": skipped_candidates,
        "failed_candidates": failed_candidates,
        "snapshot_storage_bytes_before": existing_snapshot_storage_bytes,
        "snapshot_storage_bytes_after": snapshot_storage_bytes,
        "snapshot_storage_quota_bytes": MAX_CANDIDATE_OBSERVATION_SNAPSHOT_STORAGE_BYTES,
        "snapshot_record_quota": MAX_CANDIDATE_OBSERVATION_SNAPSHOT_RECORDS,
        "status": "failed" if selected_candidates and len(failed_candidates) == len(selected_candidates) else "partial_failure" if failed_candidates else "completed",
        "operation_scope": "automatic_candidate_raw_snapshot_pending_evidence_only",
    }
    if ledger == ledger_before_run:
        # 只读/空轮次不改变 revision、updated_at 或 HMAC；返回既有路径保持调用方结果结构稳定。
        saved_path = ledger_path(workspace_dir)
    else:
        try:
            saved_path = save_ledger(workspace_dir, ledger)
        except (OSError, RuntimeError, ValueError):
            _remove_uncommitted_snapshot_files(uncommitted_snapshot_paths)
            raise
    return {"run": run, "observations": observed_records, "ledger_path": str(saved_path)}


def _required_server_identifier(value: object, field_name: str) -> str:
    """校验只读查询的服务器记录 ID，阻止查询参数被解释为文件路径或 URL。

    输入是候选 ID 或观察 ID，输出为只含小写字母、数字和连字符的稳定记录标识。调用方
    不能传入斜杠、空格、URL 或任意文件名；因此后续快照读取始终由已保存观察记录和
    服务器路径规则共同决定。本函数不联网、不读写台账，也不返回或改变任何证据。
    """
    if not isinstance(value, str) or not value or len(value) > 120:
        raise ValueError(f"{field_name}必须是有效的服务器记录 ID")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in value):
        raise ValueError(f"{field_name}必须是有效的服务器记录 ID")
    return value


def _validate_observation_list_limit(limit: object) -> int:
    """限制只读观察列表的返回量，避免一次查询无边界返回历史页面元数据。"""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit必须是1到{MAX_CANDIDATE_OBSERVATION_LIST_LIMIT}之间的整数")
    if not 1 <= limit <= MAX_CANDIDATE_OBSERVATION_LIST_LIMIT:
        raise ValueError(f"limit必须是1到{MAX_CANDIDATE_OBSERVATION_LIST_LIMIT}之间的整数")
    return limit


def _public_observation_metadata(observation: dict[str, Any]) -> dict[str, object]:
    """返回供人工排队核验的观察元数据，刻意排除本地快照路径和 Base64 原始字节。

    输入是一条台账观察记录，输出是来源链接、采集时间、哈希、词表命中和优先级等可读
    字段。快照路径不返回，防止调用方把工作区存储细节当作可访问文件名；原始字节也不
    返回，避免模型工具一次输出大页面内容。函数纯内存转换，不联网、不写台账、不改变
    候选、证据、评分或人工业务状态。
    """
    required_fields = (
        "observation_id",
        "candidate_id",
        "source_url",
        "source_title",
        "content_type",
        "collected_at",
        "raw_sha256",
        "byte_size",
        "signals",
        "review_priority",
        "state",
        "operation_scope",
    )
    result: dict[str, object] = {}
    for field_name in required_fields:
        if field_name not in observation:
            raise ValueError(f"候选观察记录缺少字段：{field_name}")
        result[field_name] = observation[field_name]
    # 新版观察可能是同一候选网页内容变化后的独立快照。三个字段都是服务器历史引用而非
    # 文件路径或原始字节，可让人工比对变化链；旧观察没有这些字段时保持向后兼容。
    for field_name in ("observation_kind", "previous_observation_id", "previous_raw_sha256"):
        if field_name in observation:
            result[field_name] = observation[field_name]
    return result


def _find_observation(
    observations: list[dict[str, Any]],
    *,
    observation_id: str | None,
    candidate_id: str | None,
) -> tuple[dict[str, Any], int]:
    """按观察 ID 精确定位，或按候选 ID 返回最新观察，并显式报告候选的观察次数。

    输入必须二选一：观察 ID 用于精确读取，候选 ID 用于读取该候选最近一次原始快照。
    输出是目标观察和同候选观察数；候选未来可能重新观察时，旧快照仍保留，只是不作为
    默认展示项。函数不读取页面或文件、不写台账，也不推断快照中任何业务事实。
    """
    if (observation_id is None) == (candidate_id is None):
        raise ValueError("observation_id 与 candidate_id 必须且只能提供一个")
    if observation_id is not None:
        normalized_observation_id = _required_server_identifier(observation_id, "observation_id")
        for observation in observations:
            if isinstance(observation, dict) and observation.get("observation_id") == normalized_observation_id:
                return observation, 1
        raise ValueError("未找到候选观察记录")

    normalized_candidate_id = _required_server_identifier(candidate_id, "candidate_id")
    matches = [
        observation
        for observation in observations
        if isinstance(observation, dict) and observation.get("candidate_id") == normalized_candidate_id
    ]
    if not matches:
        raise ValueError("该候选尚无原始观察快照")
    matches.sort(key=lambda observation: str(observation.get("collected_at", "")), reverse=True)
    return matches[0], len(matches)


def _candidate_context(
    candidates: list[dict[str, Any]],
    candidate_id: str,
    *,
    latest_reviews: dict[str, dict[str, Any]] | None = None,
) -> dict[str, object]:
    """返回与观察关联的候选元数据，不暴露可写引用或将候选变成招商线索。

    输入是已保存候选列表和观察记录中的候选 ID，输出为发现来源、产业、状态和观察索引。
    该结果仅帮助人工理解页面来源；它不是事实证据，不包含任何编辑能力，也不修改候选
    的抓取状态、证据引用、人工复核状态或评分。
    """
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise ValueError("候选观察记录引用的候选不存在")
    fields = (
        "candidate_id",
        "source_url",
        "source_title",
        "sector",
        "source_catalog_id",
        "discovered_at",
        "source_type",
        "source_level",
        "state",
        "capture_attempts",
        "captured_evidence_id",
        "last_capture_error",
        "last_captured_at",
        "observation_attempts",
        "last_observation_id",
        "last_observation_error",
        "last_observed_at",
    )
    # Front end users need an explicit two-dimensional scope explanation.  The
    # derived view neither changes the stored candidate nor upgrades an index
    # match or a human decision into formal evidence.
    review = (latest_reviews or {}).get(candidate_id)
    geographic_scope = candidate_geographic_scope(candidate, review)
    context = {field_name: candidate.get(field_name) for field_name in fields}
    context["business_review_state"] = (
        review.get("new_review_state") if isinstance(review, dict) else "pending_user_review"
    )
    context["business_reviewed_at"] = review.get("reviewed_at") if isinstance(review, dict) else None
    context["requires_china_business_verification"] = geographic_scope.get("can_request_server_recapture") is not True
    context["geographic_scope"] = geographic_scope
    context["recapture_scope_status"] = geographic_scope.get("scope_status")
    return context


def _read_verified_observation_snapshot(
    workspace_dir: Path,
    observation: dict[str, Any],
) -> dict[str, object]:
    """读取并校验观察快照的固定路径、字段、原始字节长度与哈希。

    输入是当前工作区和已定位的台账观察记录；输出为已经验证完整性的快照 JSON。函数不
    信任台账中的 ``snapshot_relative_path`` 作为文件路径，而是从观察 ID 重新计算唯一
    路径并逐字比较，随后校验 Base64 原始字节与记录哈希/长度一致。任一不一致均报错，
    不尝试修复、覆盖或删除快照；函数只读文件，不写台账或改写原始证据链。
    """
    observation_id = _required_server_identifier(observation.get("observation_id"), "observation_id")
    expected_path = candidate_observation_snapshot_path(workspace_dir, observation_id)
    expected_relative_path = str(expected_path.relative_to(workspace_dir))
    if observation.get("snapshot_relative_path") != expected_relative_path:
        raise ValueError("候选观察快照路径与服务器规则不一致，拒绝读取")
    if (
        expected_path.parent.is_symlink()
        or expected_path.is_symlink()
        or not expected_path.is_file()
    ):
        raise ValueError("候选观察原始快照不存在")
    try:
        with expected_path.open("r", encoding="utf-8") as file:
            snapshot = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError("候选观察原始快照不是有效 JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("候选观察原始快照根节点必须是对象")
    for field_name in (
        "observation_id",
        "candidate_id",
        "source_url",
        "source_title",
        "content_type",
        "collected_at",
        "raw_sha256",
        "byte_size",
    ):
        if snapshot.get(field_name) != observation.get(field_name):
            raise ValueError(f"候选观察原始快照与台账字段不一致：{field_name}")
    raw_content_base64 = snapshot.get("raw_content_base64")
    if not isinstance(raw_content_base64, str):
        raise ValueError("候选观察原始快照缺少原始字节")
    try:
        raw_content = base64.b64decode(raw_content_base64, validate=True)
    except ValueError as exc:
        raise ValueError("候选观察原始快照的原始字节编码无效") from exc
    if hashlib.sha256(raw_content).hexdigest() != observation.get("raw_sha256"):
        raise ValueError("候选观察原始快照哈希校验失败")
    if len(raw_content) != observation.get("byte_size"):
        raise ValueError("候选观察原始快照字节长度校验失败")
    if not isinstance(snapshot.get("visible_text"), str):
        raise ValueError("候选观察原始快照缺少可见正文")
    return snapshot


def _visible_text_segments_for_change_summary(visible_text: str) -> list[str]:
    """把有限可见正文拆成稳定的人工差异片段，不推断或改写任何来源事实。

    输入是已从服务器快照校验出的可见正文，输出为按中文和英文常见句末符切分的非空片段。
    连续空白会归一化，避免纯排版变化淹没人工队列；原始字节、原始可见正文和证据字段都
    不会被本函数修改。该函数不联网、不读写文件或台账，片段仅作为后续只读差异摘要输入。
    """
    normalized_text = " ".join(visible_text.replace("\u00a0", " ").split())
    if not normalized_text:
        return []
    return [
        segment
        for segment in (
            value.strip() for value in re.split(r"(?<=[。！？!?；;])", normalized_text)
        )
        if segment
    ]


def _bounded_change_segments(segments: list[str]) -> tuple[list[str], bool]:
    """限制差异摘要返回量，避免详情接口回传无边界的历史正文。

    输入是确定性文本比较所得的新增或移除片段，输出为最多八条、每条最多 400 字的安全
    展示列表及是否截断的标记。截断只影响方便人工初筛的视图；完整原始快照仍保留在固定
    路径并由哈希校验，函数不写入、删除或覆盖它们，也不能改变 evidence 或线索状态。
    """
    truncated = len(segments) > MAX_CHANGE_SUMMARY_SEGMENTS
    bounded: list[str] = []
    for segment in segments[:MAX_CHANGE_SUMMARY_SEGMENTS]:
        if len(segment) > MAX_CHANGE_SUMMARY_SEGMENT_CHARS:
            bounded.append(f"{segment[:MAX_CHANGE_SUMMARY_SEGMENT_CHARS]}…")
            truncated = True
        else:
            bounded.append(segment)
    return bounded, truncated


def _change_summary_for_observation(
    workspace_dir: Path,
    *,
    observation: dict[str, Any],
    observations: list[dict[str, Any]],
    current_snapshot: dict[str, object],
) -> dict[str, object]:
    """为内容变化观察生成经哈希链校验的只读正文差异摘要。

    输入为当前已校验快照、台账中的同候选观察索引和工作区；输出包含前序观察 ID/哈希及
    有上限的新增、移除片段。只有 ``content_changed`` 记录才会读取前序快照，并严格验证
    前序 ID、候选 ID、前序哈希和两个快照本身；任一链路异常即拒绝输出，绝不猜测或修复。
    结果只说明已保存页面正文的文本差异，不等同融资、合作、扩产或落地事实；函数只读，
    不联网、不重抓、不写台账、不修改原始证据、草稿、评分、线索或人工复核。
    """
    if observation.get("observation_kind") != "content_changed":
        return {
            "available": False,
            "reason": "initial_or_legacy_observation_has_no_verified_predecessor",
            "comparison_method": "deterministic_visible_text_segment_diff_v1",
        }

    previous_observation_id = observation.get("previous_observation_id")
    previous_raw_sha256 = observation.get("previous_raw_sha256")
    if not isinstance(previous_observation_id, str) or not isinstance(previous_raw_sha256, str):
        raise ValueError("内容变化观察缺少前序快照哈希链路")
    normalized_previous_observation_id = _required_server_identifier(
        previous_observation_id, "previous_observation_id"
    )
    previous_observation, _ = _find_observation(
        observations,
        observation_id=normalized_previous_observation_id,
        candidate_id=None,
    )
    candidate_id = observation.get("candidate_id")
    if not isinstance(candidate_id, str) or previous_observation.get("candidate_id") != candidate_id:
        raise ValueError("内容变化观察的前序快照候选不一致")
    if previous_observation.get("raw_sha256") != previous_raw_sha256:
        raise ValueError("内容变化观察的前序快照哈希不一致")
    current_raw_sha256 = observation.get("raw_sha256")
    if not isinstance(current_raw_sha256, str) or current_raw_sha256 == previous_raw_sha256:
        raise ValueError("内容变化观察的当前哈希与前序快照不一致")

    previous_snapshot = _read_verified_observation_snapshot(workspace_dir, previous_observation)
    previous_visible_text = previous_snapshot.get("visible_text")
    current_visible_text = current_snapshot.get("visible_text")
    if not isinstance(previous_visible_text, str) or not isinstance(current_visible_text, str):
        raise ValueError("候选观察快照缺少可用于差异比较的正文")
    comparison_input_truncated = (
        len(previous_visible_text) > MAX_CHANGE_SUMMARY_INPUT_CHARS
        or len(current_visible_text) > MAX_CHANGE_SUMMARY_INPUT_CHARS
    )
    previous_segments = _visible_text_segments_for_change_summary(
        previous_visible_text[:MAX_CHANGE_SUMMARY_INPUT_CHARS]
    )
    current_segments = _visible_text_segments_for_change_summary(
        current_visible_text[:MAX_CHANGE_SUMMARY_INPUT_CHARS]
    )
    matcher = difflib.SequenceMatcher(
        a=previous_segments,
        b=current_segments,
        autojunk=False,
    )
    added_segments: list[str] = []
    removed_segments: list[str] = []
    for operation, previous_start, previous_end, current_start, current_end in matcher.get_opcodes():
        if operation in {"replace", "insert"}:
            added_segments.extend(current_segments[current_start:current_end])
        if operation in {"replace", "delete"}:
            removed_segments.extend(previous_segments[previous_start:previous_end])
    public_added_segments, added_truncated = _bounded_change_segments(added_segments)
    public_removed_segments, removed_truncated = _bounded_change_segments(removed_segments)
    return {
        "available": True,
        "previous_observation_id": normalized_previous_observation_id,
        "previous_collected_at": previous_observation.get("collected_at"),
        "previous_raw_sha256": previous_raw_sha256,
        "current_raw_sha256": current_raw_sha256,
        "comparison_method": "deterministic_visible_text_segment_diff_v1",
        "added_segment_count": len(added_segments),
        "removed_segment_count": len(removed_segments),
        "added_segments": public_added_segments,
        "removed_segments": public_removed_segments,
        "comparison_input_truncated": comparison_input_truncated,
        "summary_truncated": comparison_input_truncated or added_truncated or removed_truncated,
        "caveat": "仅展示两份已验证页面可见正文的文本差异，不构成企业融资、合作、扩产或落地事实；请结合来源链接、原始快照、采集时间和哈希人工核验。",
    }


def list_candidate_observations(
    workspace_dir: Path,
    *,
    review_priority: str | None = None,
    content_changed_only: bool = False,
    limit: int = 50,
) -> dict[str, object]:
    """列出当前工作区的待补证观察队列，不读取原始网页或修改任何招商数据。

    输入可按 ``high_priority_review`` 或 ``normal_review`` 筛选优先级、以 ``content_changed_only``
    只看服务器哈希已变化的页面，并受 1–100 条限制；输出先列出变化观察、再按采集时间
    倒序的元数据、总匹配数与台账位置。变化筛选仅基于已保存的 ``observation_kind``，不把
    词表命中或推测升级为企业事实。函数不接受 URL、文件路径、用户 ID 或任何写入字段，
    因此不会触发重抓、编辑快照、创建 evidence、推进线索或改变评分和人工复核；要查看
    某项正文，必须再调用精确读取函数。
    """
    normalized_limit = _validate_observation_list_limit(limit)
    # ``red_review`` 仅兼容旧版候选快照；它历史上代表“优先看一眼”，不是企业风险。
    normalized_review_priority = {
        "red_review": "high_priority_review",
    }.get(review_priority, review_priority)
    if normalized_review_priority is not None and normalized_review_priority not in {
        "high_priority_review",
        "normal_review",
    }:
        raise ValueError("review_priority只能是high_priority_review或normal_review")
    if not isinstance(content_changed_only, bool):
        raise ValueError("content_changed_only必须是布尔值")
    ledger = load_ledger(workspace_dir)
    candidates, observations = _candidate_observation_lists(ledger)
    scope_excluded_observation_ids = _excluded_observation_ids(ledger)
    # 即使新规则刚部署、下一次后台轮询尚未来得及追加审计，也按同一严格门槛临时隐藏
    # 历史无评估快照，避免用户继续看到 RS 之类无法证明中国/徐州关联的页面。
    transient_scope_excluded_observation_ids = {
        str(observation["observation_id"])
        for observation in observations
        if isinstance(observation, dict)
        and isinstance(observation.get("observation_id"), str)
        and _scope_exclusion_for_observation(
            ledger,
            observation=observation,
            candidates=candidates,
        )
        is not None
    }
    hidden_observation_ids = scope_excluded_observation_ids | transient_scope_excluded_observation_ids
    selected = [
        observation
        for observation in observations
        if isinstance(observation, dict)
        and observation.get("observation_id") not in hidden_observation_ids
        and (
            normalized_review_priority is None
            or {
                "red_review": "high_priority_review",
            }.get(observation.get("review_priority"), observation.get("review_priority"))
            == normalized_review_priority
        )
        and (
            not content_changed_only
            or observation.get("observation_kind") == "content_changed"
        )
    ]
    # 先按时间稳定排序，再稳定地把内容变化记录提升到队列前方。旧台账没有
    # observation_kind 时视为非变化记录，保持兼容；这里只调整只读输出顺序，绝不回写台账。
    selected.sort(key=lambda observation: str(observation.get("collected_at", "")), reverse=True)
    selected.sort(key=lambda observation: observation.get("observation_kind") != "content_changed")
    latest_reviews = _latest_reviews_by_candidate(ledger)
    candidates_by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("candidate_id"), str)
    }
    public_observations: list[dict[str, object]] = []
    for observation in selected[:normalized_limit]:
        public_observation = _public_observation_metadata(observation)
        candidate_id = observation.get("candidate_id")
        candidate = candidates_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        # 把“词表待补证优先级”和“地域优先级”分开输出。前者只说明页面出现了待核
        # 验词，后者只说明当前人工/字段状态；两者都不等于正式招商事实，也不回写台账。
        if isinstance(candidate, dict):
            geographic_scope = candidate_geographic_scope(candidate, latest_reviews.get(candidate_id))
            public_observation["geographic_scope"] = geographic_scope
            public_observation["geographic_priority"] = geographic_scope.get("geographic_priority")
        public_observations.append(public_observation)
    return {
        "count": len(selected),
        "content_changed_count": sum(
            1 for observation in selected if observation.get("observation_kind") == "content_changed"
        ),
        "content_changed_only": content_changed_only,
        "scope_excluded_count": len(hidden_observation_ids),
        "queue_order": "content_changed_then_collected_at_desc",
        "observations": public_observations,
        "ledger_path": str(ledger_path(workspace_dir)),
    }


def get_candidate_observation(
    workspace_dir: Path,
    *,
    observation_id: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, object]:
    """读取一条已保存候选观察的校验后正文和待补证信号，不改变任何持久化数据。

    输入必须提供 observation_id 或 candidate_id 之一，输出包含候选上下文、观察元数据、
    至多 12,000 字的原始可见正文及原始字节的 SHA-256/长度校验结果；内容变化观察还会
    返回受限、可追溯的文本差异摘要。Base64 原始字节保留在权限受限的工作区快照中而不经
    工具返回，避免一次只读查询造成大内容外泄；人工仍可用正文、差异片段、URL、采集时间
    和哈希复核。函数不联网、不重抓、不保存、不创建 evidence、不改变线索、评分、红色
    预警或任何人工业务状态。
    """
    ledger = load_ledger(workspace_dir)
    candidates, observations = _candidate_observation_lists(ledger)
    latest_reviews = _latest_reviews_by_candidate(ledger)
    observation, matched_observation_count = _find_observation(
        observations,
        observation_id=observation_id,
        candidate_id=candidate_id,
    )
    snapshot = _read_verified_observation_snapshot(workspace_dir, observation)
    visible_text = snapshot["visible_text"]
    if not isinstance(visible_text, str):
        raise RuntimeError("候选观察原始快照正文格式异常")
    candidate_value = observation.get("candidate_id")
    if not isinstance(candidate_value, str):
        raise ValueError("候选观察记录缺少 candidate_id")
    change_summary = _change_summary_for_observation(
        workspace_dir,
        observation=observation,
        observations=observations,
        current_snapshot=snapshot,
    )
    # 草稿由独立模块只读筛选：详情展示不触发抽取、不写入草稿，更不允许用户以查看操作
    # 确认或修改其中的实体、日期、摘录。正式证据仍必须走受控抓取与完整性校验流程。
    from .evidence_draft import evidence_drafts_for_observation

    evidence_drafts = evidence_drafts_for_observation(ledger, observation_id=observation["observation_id"])
    scope_exclusion = _scope_exclusion_for_observation(
        ledger,
        observation=observation,
        candidates=candidates,
    )
    return {
        "candidate": _candidate_context(candidates, candidate_value, latest_reviews=latest_reviews),
        "observation": _public_observation_metadata(observation),
        "matched_observation_count": matched_observation_count,
        "change_summary": change_summary,
        "snapshot": {
            "source_url": snapshot["source_url"],
            "source_title": snapshot["source_title"],
            "content_type": snapshot["content_type"],
            "collected_at": snapshot["collected_at"],
            "raw_sha256": snapshot["raw_sha256"],
            "byte_size": snapshot["byte_size"],
            "visible_text": visible_text[:MAX_RETURN_VISIBLE_TEXT_CHARS],
            "visible_text_truncated": len(visible_text) > MAX_RETURN_VISIBLE_TEXT_CHARS,
            "raw_content_available": True,
            "raw_content_not_returned_reason": "原始字节保留在受限快照中；请以正文、来源链接、采集时间和哈希进行人工核验。",
        },
        "evidence_draft_count": len(evidence_drafts),
        "evidence_drafts": evidence_drafts,
        # 直接按已知 ID 查看历史快照时保留范围排除说明，方便审计；前端列表不会默认展示
        # 这类快照，且该说明不修改原始快照、候选或证据。
        "scope_exclusion": scope_exclusion,
        "ledger_path": str(ledger_path(workspace_dir)),
    }
