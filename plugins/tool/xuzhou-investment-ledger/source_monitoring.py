"""徐州招商公开来源的健康度、覆盖缺口与定时发现编排。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import (
    ScheduledMonitorAlreadyRunningError,
    exclusive_scheduled_monitor_lock,
    ledger_path,
    load_ledger,
    save_ledger,
)


# 定时任务只巡检既有的三条产业主线；不接受 Cron 或模型临时指定的行业、URL、来源站点。
MONITORED_SECTORS = ("embodied_intelligence", "semiconductor", "biomedicine")
SCHEDULED_PUBLIC_DISCOVERY_SCOPE = "scheduled_public_discovery"
SEARCH_FEED_SOURCE_KEY = "public_search_feed:bing_rss"
MAX_HEALTH_ERROR_LENGTH = 500
MAX_COVERAGE_WINDOW_DAYS = 90
# 平台 Cron 的具体表达式由部署层显式配置；插件自身仍强制同工作区两次实际巡检至少相隔
# 六小时，防止 Cron 误配、重复投递或手工误触发导致免费公开来源被高频访问。
MIN_SCHEDULED_MONITOR_INTERVAL = timedelta(hours=6)
CONSECUTIVE_MONITOR_FAILURE_ALERT_THRESHOLD = 3


def _utc_now() -> str:
    """返回来源健康度与定时审计共用的 UTC 时间，避免服务器时区影响退避判断。"""
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, payload: dict[str, object]) -> str:
    """按审计输入生成稳定短 ID，便于把定时任务与其分产业结果关联。"""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _source_health_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得台账中的来源健康记录；异常结构显式失败，绝不据此重置历史证据。"""
    records = ledger.get("source_health")
    if not isinstance(records, list):
        raise ValueError("招商线索台账中的 source_health 必须是列表")
    return records


def _monitoring_runs(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得定时监控审计列表；它只描述任务结果，不保存或改写网页证据。"""
    runs = ledger.get("monitoring_runs")
    if not isinstance(runs, list):
        raise ValueError("招商线索台账中的 monitoring_runs 必须是列表")
    return runs


def _parse_utc_timestamp(value: object) -> datetime | None:
    """解析本插件生成的 UTC 时间；损坏或未知时间返回空，报告会保守地不计入窗口。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _scheduled_monitor_configuration() -> dict[str, object]:
    """返回固定后台监控配置，避免模型、前端或 Cron 参数改变访问频率与告警门槛。

    输入为空，输出仅包含代码常量定义的最小周期、连续失败阈值和锁策略；这些值是运行
    治理元数据，不是企业、来源或证据事实。函数不读写台账、不联网、不创建定时任务，
    部署层仍须单独明确设置 Cron 表达式，且不得把该配置改造成调用方可写参数。
    """
    return {
        "scope": SCHEDULED_PUBLIC_DISCOVERY_SCOPE,
        "minimum_interval_seconds": int(MIN_SCHEDULED_MONITOR_INTERVAL.total_seconds()),
        "minimum_interval_hours": int(MIN_SCHEDULED_MONITOR_INTERVAL.total_seconds() // 3600),
        "consecutive_failure_alert_threshold": CONSECUTIVE_MONITOR_FAILURE_ALERT_THRESHOLD,
        "lock_strategy": "nonblocking_flock_per_workspace",
    }


def _latest_monitor_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """返回追加式监控审计中的最新有效对象，不修复或重排历史记录。"""
    for run in reversed(runs):
        if isinstance(run, dict):
            return run
    return None


def _consecutive_monitor_failure_count(runs: list[dict[str, Any]]) -> int:
    """计算末尾连续未完整成功的实际监控次数，跳过记录不会被写入因此不计入。

    输入是已保存的监控审计列表，输出从最新一条向前连续的 ``failed`` 或
    ``partial_failure`` 数量；遇到 ``completed`` 或未知状态立即停止，避免损坏历史被
    猜测为失败。该计算只读，不发告警、不写台账，也不影响来源退避或任何原始证据。
    """
    count = 0
    for run in reversed(runs):
        if not isinstance(run, dict):
            break
        status = run.get("status")
        if status in {"failed", "partial_failure"}:
            count += 1
            continue
        break
    return count


def _scheduled_monitor_status(
    ledger: dict[str, Any],
    *,
    evaluated_at: str,
) -> dict[str, object]:
    """根据服务器时间和追加审计计算下一次监控是否到期，仅供调度治理展示。

    输入为已验证台账与服务器生成的 UTC 时间，输出为上次实际运行、下次允许时间、是否
    到期和连续失败数。时间格式异常时保守地允许一次受锁保护的执行，而不编造下次时间；
    函数不写入运行审计、不修改来源健康度，更不会改写 evidence、候选、线索或人工状态。
    """
    evaluated_datetime = _parse_utc_timestamp(evaluated_at)
    if evaluated_datetime is None:
        raise RuntimeError("后台监控服务器时间格式异常")
    runs = _monitoring_runs(ledger)
    latest_run = _latest_monitor_run(runs)
    latest_started_at = latest_run.get("started_at") if latest_run is not None else None
    latest_started_datetime = _parse_utc_timestamp(latest_started_at)
    next_eligible_at = (
        (latest_started_datetime + MIN_SCHEDULED_MONITOR_INTERVAL).isoformat()
        if latest_started_datetime is not None
        else None
    )
    is_due = latest_started_datetime is None or evaluated_datetime >= (
        latest_started_datetime + MIN_SCHEDULED_MONITOR_INTERVAL
    )
    return {
        **_scheduled_monitor_configuration(),
        "last_monitor_run_id": latest_run.get("monitor_run_id") if latest_run is not None else None,
        "last_started_at": latest_started_at if isinstance(latest_started_at, str) else None,
        "last_status": latest_run.get("status") if latest_run is not None else None,
        "next_eligible_at": next_eligible_at,
        "is_due": is_due,
        "consecutive_failure_count": _consecutive_monitor_failure_count(runs),
    }


def _monitor_failure_alert(
    monitor_run: dict[str, Any],
    *,
    consecutive_failure_count: int,
) -> dict[str, object] | None:
    """在连续失败达到固定阈值时生成一条运行治理高等级人工复核告警。

    输入为本轮追加审计和已计算的连续失败数，输出为告警对象或空值。告警只表示后台
    监控连续未完整成功，不表示任何企业发生异常，也不会设置 LeadRecord.red_alert；它
    随本轮审计一同保存，不能被模型、前端或用户输入伪造、确认或覆盖。
    """
    if consecutive_failure_count < CONSECUTIVE_MONITOR_FAILURE_ALERT_THRESHOLD:
        return None
    monitor_run_id = monitor_run.get("monitor_run_id")
    if not isinstance(monitor_run_id, str):
        raise RuntimeError("监控运行审计缺少有效 ID")
    return {
        "alert_id": _stable_id(
            "monitor-alert",
            {
                "monitor_run_id": monitor_run_id,
                "consecutive_failure_count": consecutive_failure_count,
                "alert_type": "scheduled_monitor_consecutive_failure",
            },
        ),
        "alert_type": "scheduled_monitor_consecutive_failure",
        # 这是抓取任务健康度，不是企业风险红色预警；使用一般运行严重度名称避免前端或
        # 人工把“来源连续失败”误读为某家企业的负面异常。
        "severity": "high",
        "generated_at": monitor_run.get("completed_at"),
        "consecutive_failure_count": consecutive_failure_count,
        "message": "后台公开来源监控连续未完整成功，请人工检查来源覆盖、网络和部署调度配置。",
        "operation_scope": "scheduled_monitor_runtime_governance_only",
    }


def _find_source_health_record(
    records: list[dict[str, Any]],
    source_key: str,
) -> dict[str, Any] | None:
    """按服务器生成的来源键定位健康记录，不从页面正文、模型输入推断来源身份。"""
    return next(
        (
            record
            for record in records
            if isinstance(record, dict) and record.get("source_key") == source_key
        ),
        None,
    )


def source_health_key_for_url(source_url: str) -> str:
    """由候选 URL 的主机名生成健康度键，避免把路径、查询参数或证据内容写入健康记录。

    输入必须是服务器保存的公开 HTTP(S) 候选链接；输出仅为小写域名键。该函数不联网、
    不写台账，也不把域名当作来源可信度或企业事实。
    """
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("自动发现候选缺少有效公开来源域名")
    return f"public_page_domain:{parsed.hostname.casefold()}"


def _failure_kind(error: str) -> str:
    """把访问错误归为有限类别，以确定退避时长；不解析网页正文，也不尝试绕过限制。"""
    normalized = error.casefold()
    if "robots" in normalized or "policy" in normalized or "451" in normalized:
        return "policy_blocked"
    if "429" in normalized or "rate limit" in normalized or "too many requests" in normalized:
        return "rate_limited"
    if "403" in normalized or "401" in normalized or "captcha" in normalized:
        return "access_limited"
    return "transient_error"


def _cooldown_duration(failure_kind: str, consecutive_failures: int) -> timedelta | None:
    """返回合规退避时间，受政策禁止时不安排自动重试，避免重复访问受限网站。"""
    exponent = min(max(consecutive_failures - 1, 0), 4)
    if failure_kind == "policy_blocked":
        return None
    if failure_kind == "rate_limited":
        return timedelta(hours=min(72, 6 * (2**exponent)))
    if failure_kind == "access_limited":
        return timedelta(hours=min(168, 24 * (2**exponent)))
    return timedelta(hours=min(24, 1 * (2**exponent)))


def is_source_eligible(
    ledger: dict[str, Any],
    *,
    source_key: str,
    observed_at: str | None = None,
) -> bool:
    """判断来源是否可被自动任务访问。

    输入是内存台账、服务器生成的来源键和可选的服务器审计时间；输出为是否允许本次
    访问。政策禁止或尚在退避期时返回 ``False``，从而减少反复请求，并且不改写任何
    ``evidence``、来源 URL、摘录或采集时间。
    """
    records = _source_health_records(ledger)
    record = _find_source_health_record(records, source_key)
    if record is None:
        return True
    if record.get("status") == "policy_blocked":
        return False
    next_eligible_at = _parse_utc_timestamp(record.get("next_eligible_at"))
    now = _parse_utc_timestamp(observed_at) if observed_at else datetime.now(timezone.utc)
    return next_eligible_at is None or next_eligible_at <= now


def policy_blocked_page_domains(ledger: dict[str, Any]) -> set[str]:
    """列出因 robots、证书或访问政策被禁止自动访问的页面域名。

    输入是内存台账；输出为 ``public_page_domain:`` 来源键中状态为 ``policy_blocked``
    的小写域名集合。政策禁止不会因低频重试而改变，这些域名不应再占用每轮观察选择
    预算，否则最老的被封候选会反复挤占名额，使新候选永远轮不到首次观察。函数只读
    台账健康记录，不联网、不修改证据链、线索或来源健康度。
    """
    blocked: set[str] = set()
    for record in _source_health_records(ledger):
        if not isinstance(record, dict) or record.get("status") != "policy_blocked":
            continue
        source_key = record.get("source_key")
        if isinstance(source_key, str) and source_key.startswith("public_page_domain:"):
            blocked.add(source_key.removeprefix("public_page_domain:"))
    return blocked


def apply_source_health_outcome(
    ledger: dict[str, Any],
    *,
    source_key: str,
    success: bool,
    error: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """在内存台账中追加或更新一次来源访问结果。

    输入由服务器抓取代码提供：来源键、成功标记和有限错误文本；输出是对应健康记录。
    成功会清除连续失败和退避，失败会按 429、访问限制、政策限制或临时错误进行指数
    退避。该函数只更新可再生的运行治理元数据，不能写入、删除或改写原始证据链。
    调用方负责在同一业务操作中原子保存台账。
    """
    if not isinstance(source_key, str) or not source_key.strip():
        raise ValueError("source_key不能为空")
    observed = observed_at or _utc_now()
    records = _source_health_records(ledger)
    record = _find_source_health_record(records, source_key)
    if record is None:
        record = {
            "source_key": source_key,
            "status": "healthy",
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error_kind": None,
            "last_error": None,
            "next_eligible_at": None,
            "updated_at": observed,
        }
        records.append(record)

    record["updated_at"] = observed
    if success:
        record.update(
            {
                "status": "healthy",
                "consecutive_failures": 0,
                "last_success_at": observed,
                "last_error_kind": None,
                "last_error": None,
                "next_eligible_at": None,
            }
        )
        return record

    normalized_error = (error or "未提供访问失败原因").strip()[:MAX_HEALTH_ERROR_LENGTH]
    failures = int(record.get("consecutive_failures", 0)) + 1
    failure_kind = _failure_kind(normalized_error)
    cooldown = _cooldown_duration(failure_kind, failures)
    observed_datetime = _parse_utc_timestamp(observed)
    if observed_datetime is None:
        raise RuntimeError("来源健康度审计时间格式异常")
    record.update(
        {
            "status": "policy_blocked" if cooldown is None else "cooldown",
            "consecutive_failures": failures,
            "last_failure_at": observed,
            "last_error_kind": failure_kind,
            "last_error": normalized_error,
            "next_eligible_at": (
                None if cooldown is None else (observed_datetime + cooldown).isoformat()
            ),
        }
    )
    return record


def _existing_evidence_snapshot(ledger: dict[str, Any]) -> dict[str, str]:
    """序列化整份证据列表用于防篡改检查，不对证据内容做任何规范化或修复。

    输入是当前内存台账，输出是整份 ``evidence`` 列表的稳定序列化结果。比较整个列表
    而不是只比较已有 ID，可同时阻止定时任务覆盖旧证据、删除证据或偷偷追加新证据；
    自动发现和候选观察都只能写候选及运行审计。
    """
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    return {"evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True)}


def _assert_existing_evidence_unchanged(before: dict[str, str], ledger: dict[str, Any]) -> None:
    """确认定时发现仅新增候选与审计，绝不增删或改写任何原始证据对象。"""
    after = _existing_evidence_snapshot(ledger)
    if after != before:
        raise RuntimeError("定时来源发现不得修改既有原始证据链")


def _skipped_monitor_result(
    workspace_dir: Path,
    *,
    status: str,
    reason: str,
    started_at: str,
    schedule: dict[str, object],
) -> dict[str, Any]:
    """构造未实际执行的定时调用结果，明确跳过而不制造无边界持久化审计。

    输入均为服务器代码已确定的跳过状态、原因、时间和调度元数据，输出保持与实际监控
    相同的结果外形但标明 ``not_persisted``。周期未到或已有任务运行时不写入
    ``monitoring_runs``，避免错误配置的 Cron 在台账中制造大量无业务价值记录；此函数
    不联网、不读写候选、证据、快照、线索、评分或人工复核。
    """
    monitor_run = {
        "monitor_run_id": _stable_id(
            "monitor-skip",
            {"started_at": started_at, "status": status, "reason": reason},
        ),
        "started_at": started_at,
        "completed_at": started_at,
        "sectors": list(MONITORED_SECTORS),
        "status": status,
        "skipped_reason": reason,
        "not_persisted": True,
        "schedule": schedule,
        "operation_scope": "scheduled_public_discovery_skip_only",
    }
    return {"monitor_run": monitor_run, "ledger_path": str(ledger_path(workspace_dir))}


def run_scheduled_public_discovery(workspace_dir: Path) -> dict[str, Any]:
    """在固定周期与整次执行锁约束下运行一次后台公开来源发现。

    输入是当前智能体的服务器工作区，输出是一次实际运行审计，或“周期未到/已有任务”的
    安全跳过结果。调用方不能传入 Cron、行业、URL、来源站点或锁名称；同工作区只允许一
    个进程持锁，且两次实际运行至少相隔六小时。连续三次 ``failed`` 或
    ``partial_failure`` 会在第三次运行审计中附加运行治理红色人工复核告警，不代表企业
    异常、更不会改变 Lead 红色预警。函数不创建真实 Cron；部署层仍须明确启用平台定时
    任务，本函数只提供可安全调用的后端治理边界。
    """
    try:
        with exclusive_scheduled_monitor_lock(workspace_dir):
            started_at = _utc_now()
            ledger = load_ledger(workspace_dir)
            schedule = _scheduled_monitor_status(ledger, evaluated_at=started_at)
            schedule["lock_state"] = "acquired"
            if schedule["is_due"] is False:
                return _skipped_monitor_result(
                    workspace_dir,
                    status="skipped_not_due",
                    reason="minimum_interval_not_elapsed",
                    started_at=started_at,
                    schedule=schedule,
                )
            return _run_scheduled_public_discovery_locked(
                workspace_dir,
                started_at=started_at,
            )
    except ScheduledMonitorAlreadyRunningError:
        started_at = _utc_now()
        schedule = {
            **_scheduled_monitor_configuration(),
            "lock_state": "already_running",
            "is_due": None,
            "last_monitor_run_id": None,
            "last_started_at": None,
            "last_status": None,
            "next_eligible_at": None,
            "consecutive_failure_count": None,
        }
        return _skipped_monitor_result(
            workspace_dir,
            status="skipped_already_running",
            reason="scheduled_monitor_execution_lock_held",
            started_at=started_at,
            schedule=schedule,
        )


def _run_scheduled_public_discovery_locked(
    workspace_dir: Path,
    *,
    started_at: str,
) -> dict[str, Any]:
    """在已取得运行锁且已通过周期判断后执行固定范围的后台公开来源发现。

    输入是当前智能体的服务器工作区，输出是三条预设产业线的汇总和追加式任务审计。
    函数先按固定源码种子补齐缺失的免费统计与二手媒体发现入口（尊重同域人工停用/替换），
    再对三条产业分别读取其源码固定、robots 允许的投融资栏目。它不再调用低质量 Bing
    RSS 搜索，避免英文行业词被错误拆分成无关百科、字典或认证页。其它可信目录仍以独立
    轮换作为补充，不与产业主发现混写。函数绝不抓取任意客户端 URL、自动提取事实、写入
    evidence、改变招商状态、评分或红色预警。每个产业与目录引导的异常独立记录，不会因
    单个来源失败而中止其余产业；结束前会逐字比对既有证据，防止定时任务越界。付费升级
    方案：若需“发现后主动推送”，应由部署层使用经审批的消息队列与企业微信/短信/邮件
    服务，并保存投递结果、退订和失败重试审计；插件不得接受模型或前端传入的 webhook
    地址、收件人或服务密钥，也不能把待补证词表命中直接当作对外招商结论。

    ===== 付费升级（暂不启用；仅供后续工程师预算）=====
    企业级外部告警可采购受审计消息队列和企业微信/邮件服务；若使用国内短信，按腾讯云
    2026 年公开价，1 万条通知短信约 470 元/两年（约 0.047 元/条），短信签名、模板、
    收件人授权和退订合规需另行实施。商业告警编排/值班平台可先按约 3 千–3 万元/年估算，
    以实际供应商、消息量和合规要求为准；在取得审批前不得在插件中硬编码 webhook、号码或密钥。
    """
    from .source_feed_discovery import discover_trusted_source_feeds

    # 对已有工作区只补齐尚未出现的固定免费来源；任何同域人工记录（包括 disabled）
    # 都会被保留。引导不联网，只写目录与审计，失败也不会阻断三条产业线的独立发现任务，
    # 但会在本轮监控审计中显式标为部分失败，供人工处理。
    try:
        from .source_catalog import ensure_official_statistics_source_seeds

        official_statistics_seed_result = ensure_official_statistics_source_seeds(workspace_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        official_statistics_seed_result: dict[str, Any] = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
            "operation_scope": "system_official_statistics_source_seed_configuration_only",
        }
    try:
        from .source_catalog import ensure_government_investment_source_seeds

        government_investment_seed_result = ensure_government_investment_source_seeds(workspace_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        government_investment_seed_result: dict[str, Any] = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
            "operation_scope": "system_government_investment_source_seed_configuration_only",
        }
    try:
        from .source_catalog import ensure_free_verified_media_source_seeds

        free_verified_media_seed_result = ensure_free_verified_media_source_seeds(workspace_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        free_verified_media_seed_result: dict[str, Any] = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
            "operation_scope": "system_free_verified_media_source_seed_configuration_only",
        }

    evidence_before = _existing_evidence_snapshot(load_ledger(workspace_dir))
    sector_results: list[dict[str, Any]] = []
    for sector in MONITORED_SECTORS:
        try:
            result = discover_trusted_source_feeds(
                workspace_dir,
                requested_sector=sector,
                # 该参数由定时任务源码固定，不能由模型、前端、Cron 参数或目录 API 传入。
                # 它只选择投资界已审查的固定产业栏目，杜绝回退到 Bing、自由搜索词或 URL。
                curated_media_first=True,
                fixed_sector_index_only=True,
            )
            run = result.get("run", {})
            run_status = run.get("status")
            if run_status not in {"completed", "partial_failure"}:
                raise RuntimeError("固定产业栏目发现批次缺少有效完成状态")
            sector_results.append(
                {
                    "sector": sector,
                    # 任一固定栏目页失败都会使本产业变为 partial_failure；这只触发来源
                    # 治理人工检查，不会给企业标红、改变证据链或自动推进任何线索。
                    "status": run_status,
                    "source_feed_run_id": run.get("source_feed_run_id"),
                    "candidate_count": run.get("candidate_count", 0),
                    "selected_source_ids": run.get("selected_source_ids", []),
                    "fixed_sector_index_only": True,
                }
            )
        except (OSError, RuntimeError, ValueError) as exc:
            sector_results.append(
                {
                    "sector": sector,
                    "status": "failed",
                    "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
                }
            )

    # 普通可信目录补充与固定产业主发现分开执行：它只能读取已登记域名的 robots、RSS 和
    # Sitemap，且仍只写候选。它不是 Bing 搜索的回退，不能因固定栏目暂时无数据而扩大到
    # 自由关键词、任意 URL 或规避访问限制。动态导入避免基础来源健康函数与发现编排循环。
    try:
        # The fixed-column source already receives one bounded, robots-checked
        # pass per sector above.  This separate unscoped directory pass uses
        # the stable never-tried/least-recently-tried source rotation, so newly
        # seeded official and secondary domains are not starved by that column.
        directory_discovery = discover_trusted_source_feeds(workspace_dir)
        source_directory_result = {
            "status": directory_discovery["run"].get("status", "completed"),
            "source_feed_run_id": directory_discovery["run"].get("source_feed_run_id"),
            "candidate_count": directory_discovery["run"].get("candidate_count", 0),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        source_directory_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 原始候选页面观察在所有候选发现后才执行，且由独立服务再次检查 robots、域名退避
    # 和每轮上限。观察快照仅供人工补证，任何词表命中都不能直接生成事实 evidence。
    try:
        from .candidate_observation import observe_discovery_candidates

        candidate_observation = observe_discovery_candidates(workspace_dir)
        candidate_observation_result = {
            "status": candidate_observation["run"].get("status", "completed"),
            "observation_count": candidate_observation["run"].get("observation_count", 0),
            "content_changed_observation_count": candidate_observation["run"].get(
                "content_changed_observation_count", 0
            ),
            "unchanged_content_candidate_count": candidate_observation["run"].get(
                "unchanged_content_candidate_count", 0
            ),
            "high_priority_observation_count": candidate_observation["run"].get(
                "high_priority_observation_count", 0
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        candidate_observation_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 免费第二来源补证只从本轮/历史已校验候选快照内明确写为“官网”的跨域链接开始，不会把
    # 公司名提交给任意搜索引擎，也不会绕过 robots 或验证码。它产生的仍是待人工核验快照，
    # 绝不确认官网归属、中国/徐州业务、融资事实或招商意愿；失败/无链接只记录审计原因。
    try:
        from .candidate_corroboration import run_candidate_official_link_corroboration

        corroboration = run_candidate_official_link_corroboration(workspace_dir)
        candidate_corroboration_result = {
            "status": corroboration["run"].get("status", "completed"),
            "official_site_link_candidate_count": corroboration["run"].get(
                "official_site_link_candidate_count", 0
            ),
            "no_independent_official_site_link_count": corroboration["run"].get(
                "no_independent_official_site_link_count", 0
            ),
            "robots_denied_count": corroboration["run"].get("robots_denied_count", 0),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        candidate_corroboration_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 草稿抽取只读取上一步已经保存、已校验 SHA-256 的本地候选快照，不访问网页，也不将
    # 关键词命中升级为正式证据。它始终输出待人工确认草稿，不能创建或推进招商线索。
    try:
        from .evidence_draft import generate_evidence_drafts_from_observations

        evidence_drafts = generate_evidence_drafts_from_observations(workspace_dir)
        evidence_draft_result = {
            "status": evidence_drafts["run"].get("status", "completed"),
            "generated_draft_count": evidence_drafts["run"].get("generated_draft_count", 0),
            "skipped_invalid_snapshot_count": evidence_drafts["run"].get(
                "skipped_invalid_snapshot_count", 0
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        evidence_draft_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 经营指标草稿只扫描已经完成签名的正式 evidence 摘录，不读取本轮搜索摘要或候选快照。
    # 它只追加待人工复核草稿，不能确认金额、改变线索状态或把营业收入、产值和项目投资额
    # 相加；提取失败仅记录治理状态，不应掩盖此前已保存的候选观察与证据草稿结果。
    try:
        from .industry_metrics import generate_industry_metric_drafts

        industry_metric_drafts = generate_industry_metric_drafts(workspace_dir)
        industry_metric_draft_result = {
            "status": industry_metric_drafts["run"].get("status", "completed"),
            "generated_draft_count": industry_metric_drafts["run"].get("generated_draft_count", 0),
            "skipped_invalid_evidence_count": industry_metric_drafts["run"].get(
                "skipped_invalid_evidence_count", 0
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        industry_metric_draft_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 关键人员关系草稿只读取已签名的正式 evidence，而不是本轮搜索摘要、候选页面或指标
    # 草稿。它只能提出“姓名—组织—受控角色”的待人工复核候选，绝不自动建立图谱边、
    # 个人线索或招商结论；任一异常也只记录本轮治理状态，不影响已保存的原始证据链。
    try:
        from .relationship_draft import generate_relationship_drafts_from_signed_evidence

        relationship_drafts = generate_relationship_drafts_from_signed_evidence(workspace_dir)
        relationship_draft_result = {
            "status": relationship_drafts["run"].get("status", "completed"),
            "generated_draft_count": relationship_drafts["run"].get("generated_draft_count", 0),
            "skipped_invalid_evidence_count": relationship_drafts["run"].get(
                "skipped_invalid_evidence_count", 0
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        relationship_draft_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    # 协同投资信号草稿同样只扫描已完成完整性校验的一手正式 evidence；它只提示组织级
    # 联合投资/基金设立/寻找出资方文本，绝不收集个人资产、建立资金池、联系主体或自动
    # 推进招商。提取失败只记录治理状态，不能影响此前已经保存的证据、线索和人工审计。
    try:
        from .co_investment_signal import generate_co_investment_signal_drafts_from_signed_evidence

        co_investment_signal_drafts = generate_co_investment_signal_drafts_from_signed_evidence(
            workspace_dir
        )
        co_investment_signal_draft_result = {
            "status": co_investment_signal_drafts["run"].get("status", "completed"),
            "generated_draft_count": co_investment_signal_drafts["run"].get(
                "generated_draft_count", 0
            ),
            "skipped_invalid_evidence_count": co_investment_signal_drafts["run"].get(
                "skipped_invalid_evidence_count", 0
            ),
        }
    except (OSError, RuntimeError, ValueError) as exc:
        co_investment_signal_draft_result = {
            "status": "failed",
            "error": str(exc)[:MAX_HEALTH_ERROR_LENGTH],
        }

    ledger = load_ledger(workspace_dir)
    _assert_existing_evidence_unchanged(evidence_before, ledger)
    completed_at = _utc_now()
    failed_sectors = [
        result for result in sector_results if result["status"] in {"failed", "partial_failure"}
    ]
    source_directory_failed = source_directory_result["status"] in {"failed", "partial_failure"}
    candidate_observation_failed = candidate_observation_result["status"] in {
        "failed",
        "partial_failure",
    }
    candidate_corroboration_failed = candidate_corroboration_result["status"] in {
        "failed",
        "partial_failure",
    }
    evidence_draft_failed = evidence_draft_result["status"] in {"failed", "partial_failure"}
    industry_metric_draft_failed = industry_metric_draft_result["status"] in {
        "failed",
        "partial_failure",
    }
    relationship_draft_failed = relationship_draft_result["status"] in {
        "failed",
        "partial_failure",
    }
    co_investment_signal_draft_failed = co_investment_signal_draft_result["status"] in {
        "failed",
        "partial_failure",
    }
    official_statistics_seed_failed = official_statistics_seed_result["status"] == "failed"
    government_investment_seed_failed = government_investment_seed_result["status"] == "failed"
    free_verified_media_seed_failed = free_verified_media_seed_result["status"] == "failed"
    monitor_status = (
        "completed"
        if not failed_sectors
        and not source_directory_failed
        and not candidate_observation_failed
        and not candidate_corroboration_failed
        and not evidence_draft_failed
        and not industry_metric_draft_failed
        and not relationship_draft_failed
        and not co_investment_signal_draft_failed
        and not official_statistics_seed_failed
        and not government_investment_seed_failed
        and not free_verified_media_seed_failed
        else "failed"
        if (
            len(failed_sectors) == len(MONITORED_SECTORS)
            and source_directory_failed
            and candidate_observation_failed
            and candidate_corroboration_failed
            and evidence_draft_failed
            and industry_metric_draft_failed
            and relationship_draft_failed
            and co_investment_signal_draft_failed
            and official_statistics_seed_failed
            and government_investment_seed_failed
            and free_verified_media_seed_failed
        )
        else "partial_failure"
    )
    monitor_run: dict[str, Any] = {
        "monitor_run_id": _stable_id(
            "monitor-run",
            {
                "started_at": started_at,
                "sector_results": sector_results,
                "candidate_observation_result": candidate_observation_result,
                "candidate_corroboration_result": candidate_corroboration_result,
                "evidence_draft_result": evidence_draft_result,
                "industry_metric_draft_result": industry_metric_draft_result,
                "relationship_draft_result": relationship_draft_result,
                "co_investment_signal_draft_result": co_investment_signal_draft_result,
            },
        ),
        "started_at": started_at,
        "completed_at": completed_at,
        "sectors": list(MONITORED_SECTORS),
        "status": monitor_status,
        "sector_results": sector_results,
        "official_statistics_seed_result": official_statistics_seed_result,
        "government_investment_seed_result": government_investment_seed_result,
        "free_verified_media_seed_result": free_verified_media_seed_result,
        "source_directory_result": source_directory_result,
        "candidate_observation_result": candidate_observation_result,
        "candidate_corroboration_result": candidate_corroboration_result,
        "evidence_draft_result": evidence_draft_result,
        "industry_metric_draft_result": industry_metric_draft_result,
        "relationship_draft_result": relationship_draft_result,
        "co_investment_signal_draft_result": co_investment_signal_draft_result,
        "operation_scope": (
            "scheduled_fixed_sector_index_and_trusted_directory_source_seed_pending_"
            "evidence_metric_relationship_and_co_investment_signal_draft_only"
        ),
    }
    monitoring_runs = _monitoring_runs(ledger)
    monitoring_runs.append(monitor_run)
    consecutive_failure_count = _consecutive_monitor_failure_count(monitoring_runs)
    monitor_run["consecutive_failure_count"] = consecutive_failure_count
    monitor_run["failure_alert"] = _monitor_failure_alert(
        monitor_run,
        consecutive_failure_count=consecutive_failure_count,
    )
    # 运行审计已先追加到内存列表，再计算下次允许时间；因此返回结果与覆盖报告可看到
    # 相同的当前失败计数和周期。该元数据只治理下一轮自动任务，不触碰正式证据链。
    monitor_run["schedule"] = _scheduled_monitor_status(ledger, evaluated_at=completed_at)
    saved_path = save_ledger(workspace_dir, ledger)
    return {"monitor_run": monitor_run, "ledger_path": str(saved_path)}


def _source_recipe_effectiveness_feedback(
    ledger: dict[str, Any],
    *,
    candidates: list[object],
    sectors: dict[str, dict[str, Any]],
    window_started_at: datetime,
) -> dict[str, object]:
    """从既有候选、观察和正式证据生成来源配方的只读有效性反馈。

    输入是已经完成完整性校验的台账、当前统计窗口内外的候选记录、覆盖报告初始化的
    产业/配方结构和服务器计算出的窗口起点；输出为每条固定配方的候选、域名广度、已
    抓取状态、待补证观察及窗口内已签名证据数量，并给出固定的人工复核建议。证据只
    通过服务器保存的 ``discovery_candidate_id`` 与候选关联，观察也只按已保存的
    ``candidate_id`` 对齐，因而调用方不能用名称、URL、摘要或自称结果伪造“有效来源”。

    这是为了让后续人工基于真实产出调整免费来源目录或查询配方的反馈材料，而不是让
    网页文本、单次成功或模型自行改写搜索词、目录、频率、权重或 Skill。函数不联网、
    不保存报告、不创建候选或证据、不改变线索/评分/预警/人工状态；不同数量也不表示
    企业事实、来源真实性或招商成功率。
    """
    evidence_records = ledger.get("evidence")
    observations = ledger.get("candidate_observations")
    if not isinstance(evidence_records, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    if not isinstance(observations, list):
        raise ValueError("招商线索台账中的 candidate_observations 必须是列表")

    # 按既有覆盖报告的固定产业与配方顺序初始化；不根据候选页面内容创建新的产业、
    # 配方或来源类型，避免自动发现结果反向扩大系统网络访问范围。
    feedback_by_key: dict[tuple[str, str], dict[str, object]] = {}
    ordered_keys: list[tuple[str, str]] = []
    for sector in MONITORED_SECTORS:
        sector_summary = sectors.get(sector)
        recipes = sector_summary.get("recipes") if isinstance(sector_summary, dict) else None
        if not isinstance(recipes, dict):
            raise ValueError("来源覆盖报告缺少固定产业配方结构")
        for recipe_id in recipes:
            if not isinstance(recipe_id, str):
                raise ValueError("来源覆盖报告中的配方标识必须是字符串")
            key = (sector, recipe_id)
            ordered_keys.append(key)
            feedback_by_key[key] = {
                "sector": sector,
                "source_catalog_id": recipe_id,
                "candidate_count": 0,
                "candidate_source_domains": set(),
                "captured_candidate_count": 0,
                "capture_failed_candidate_count": 0,
                "formal_evidence_ids": set(),
                "candidate_observation_count": 0,
                "high_priority_observation_count": 0,
            }

    # 仅把“本统计窗口内、且属于固定产业/配方”的候选加入关联索引。后续证据与观察
    # 必须引用该受控索引中的 candidate_id，不能靠同名实体或相似 URL 推断归属。
    candidate_context_by_id: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        sector = candidate.get("sector")
        recipe_id = candidate.get("source_catalog_id")
        candidate_id = candidate.get("candidate_id")
        discovered_at = _parse_utc_timestamp(candidate.get("discovered_at"))
        key = (sector, recipe_id)
        if (
            not isinstance(sector, str)
            or not isinstance(recipe_id, str)
            or not isinstance(candidate_id, str)
            or not candidate_id
            or key not in feedback_by_key
            or discovered_at is None
            or discovered_at < window_started_at
        ):
            continue
        metrics = feedback_by_key[key]
        metrics["candidate_count"] = int(metrics["candidate_count"]) + 1
        source_url = candidate.get("source_url")
        source_domain = urlparse(source_url).hostname if isinstance(source_url, str) else None
        if isinstance(source_domain, str) and source_domain:
            domains = metrics["candidate_source_domains"]
            if not isinstance(domains, set):
                raise RuntimeError("来源有效性反馈域名集合结构异常")
            domains.add(source_domain.casefold().rstrip("."))
        if candidate.get("state") == "captured":
            metrics["captured_candidate_count"] = int(metrics["captured_candidate_count"]) + 1
        elif candidate.get("state") == "capture_failed":
            metrics["capture_failed_candidate_count"] = (
                int(metrics["capture_failed_candidate_count"]) + 1
            )
        # 同一 candidate_id 的重复历史记录不能被用于放大正式证据/观察收益；保留最早
        # 归入的固定配方，异常重复记录仍会在原台账中保留供人工追溯。
        candidate_context_by_id.setdefault(candidate_id, key)

    for evidence in evidence_records:
        if not isinstance(evidence, dict):
            continue
        candidate_id = evidence.get("discovery_candidate_id")
        evidence_id = evidence.get("evidence_id")
        created_at = _parse_utc_timestamp(evidence.get("created_at"))
        key = candidate_context_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        if (
            key is None
            or not isinstance(evidence_id, str)
            or not evidence_id
            or created_at is None
            or created_at < window_started_at
        ):
            continue
        evidence_ids = feedback_by_key[key]["formal_evidence_ids"]
        if not isinstance(evidence_ids, set):
            raise RuntimeError("来源有效性反馈证据集合结构异常")
        evidence_ids.add(evidence_id)

    for observation in observations:
        if not isinstance(observation, dict):
            continue
        candidate_id = observation.get("candidate_id")
        collected_at = _parse_utc_timestamp(observation.get("collected_at"))
        key = candidate_context_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        if key is None or collected_at is None or collected_at < window_started_at:
            continue
        metrics = feedback_by_key[key]
        metrics["candidate_observation_count"] = int(metrics["candidate_observation_count"]) + 1
        if observation.get("review_priority") in {"high_priority_review", "red_review"}:
            # ``red_review`` 是旧版候选观察的兼容值，只代表“高优先级待补证”，从不表示
            # 企业风险；统计时将其并入新版名称，但不回写历史原始观察记录。
            metrics["high_priority_observation_count"] = (
                int(metrics["high_priority_observation_count"]) + 1
            )

    assessments: list[dict[str, object]] = []
    for key in ordered_keys:
        metrics = feedback_by_key[key]
        candidate_count = int(metrics["candidate_count"])
        captured_candidate_count = int(metrics["captured_candidate_count"])
        capture_failed_candidate_count = int(metrics["capture_failed_candidate_count"])
        formal_evidence_ids = metrics["formal_evidence_ids"]
        candidate_source_domains = metrics["candidate_source_domains"]
        if not isinstance(formal_evidence_ids, set) or not isinstance(candidate_source_domains, set):
            raise RuntimeError("来源有效性反馈统计集合结构异常")
        formal_evidence_count = len(formal_evidence_ids)
        if candidate_count == 0:
            assessment_status = "no_candidate_coverage_in_window"
            recommended_manual_action = (
                "人工检查该免费来源配方的检索表达、可信目录和来源可访问性；不得因无候选自动扩大"
                "访问范围或引入付费/绕过式采集。"
            )
        elif formal_evidence_count > 0:
            assessment_status = "formal_evidence_yield_observed"
            recommended_manual_action = (
                "保留当前免费来源配方并继续人工核对已入证来源；不得因单个成功样本自动扩大、"
                "收缩或调权。"
            )
        elif capture_failed_candidate_count > 0:
            assessment_status = "capture_barrier_needs_manual_review"
            recommended_manual_action = (
                "人工检查 robots、公开访问限制、页面变化和来源替代方案；不得使用伪造指纹、"
                "隐身插件或其他绕过方式。"
            )
        elif captured_candidate_count > 0:
            assessment_status = "captured_state_without_formal_evidence_needs_audit"
            recommended_manual_action = (
                "人工核对候选抓取状态与正式证据引用是否一致；不一致时应审查既有审计，不能由"
                "报告补写证据或修改候选状态。"
            )
        else:
            assessment_status = "pending_candidate_or_observation_review"
            recommended_manual_action = (
                "先人工查看已有候选/待补证快照并完成受控核验；在出现正式证据或明确访问失败前，"
                "不调整来源目录或搜索配方。"
            )
        assessments.append(
            {
                "sector": metrics["sector"],
                "source_catalog_id": metrics["source_catalog_id"],
                "assessment_status": assessment_status,
                "metrics": {
                    "candidate_count": candidate_count,
                    "distinct_candidate_source_domain_count": len(candidate_source_domains),
                    "captured_candidate_count": captured_candidate_count,
                    "capture_failed_candidate_count": capture_failed_candidate_count,
                    "formal_evidence_count_in_window": formal_evidence_count,
                    "candidate_observation_count": int(metrics["candidate_observation_count"]),
                    "high_priority_observation_count": int(
                        metrics["high_priority_observation_count"]
                    ),
                },
                "recommended_manual_action": recommended_manual_action,
            }
        )

    return {
        "feedback_type": "evidence_backed_source_recipe_effectiveness_read_only",
        "assessment_count": len(assessments),
        "recipe_assessments": assessments,
        "automatic_source_catalog_change_performed": False,
        "automatic_query_or_weight_change_performed": False,
        "evidence_chain_mutation_performed": False,
        "caveat": (
            "本反馈只统计本窗口内已有候选、观察和正式证据的可追溯关联；数量高低不等于来源"
            "真实性、企业招商意愿或转化率，所有目录/配方调整仍须由普通登录用户人工决定。"
        ),
    }


def get_source_coverage_report(
    workspace_dir: Path,
    *,
    window_days: int = 30,
) -> dict[str, Any]:
    """生成只读的来源覆盖与访问健康报告。

    输入是当前工作区及 1–90 天的统计窗口，输出按产业和固定信号目录统计的候选、已
    抓取和失败数量、官方统计来源目录状态、只读来源有效性反馈，并标出无候选或仅失败
    的覆盖缺口。报告读取已有候选、观察、正式证据、监控审计、可信目录和健康度，不
    联网、不触发重试、不写台账，也不会将候选、域名或宏观统计升级为单个企业的招商
    事实或自动调整搜索范围。
    """
    if not isinstance(window_days, int) or isinstance(window_days, bool):
        raise ValueError("window_days必须是1到90之间的整数")
    if not 1 <= window_days <= MAX_COVERAGE_WINDOW_DAYS:
        raise ValueError("window_days必须是1到90之间的整数")
    from .source_discovery import QUERY_RECIPES, SECTOR_DISCOVERY_CONFIG

    ledger = load_ledger(workspace_dir)
    candidates = ledger.get("discovery_candidates")
    if not isinstance(candidates, list):
        raise ValueError("招商线索台账中的 discovery_candidates 必须是列表")
    now = datetime.now(timezone.utc)
    window_started_at = now - timedelta(days=window_days)
    # RSS/Sitemap 与搜索查询目录并列展示，避免只看搜索命中而忽略可信站点供给是否失效。
    recipe_ids = [recipe["catalog_id"] for recipe in QUERY_RECIPES] + [
        "trusted_rss_feed",
        "trusted_sitemap",
    ]
    sectors: dict[str, dict[str, Any]] = {}
    for sector in MONITORED_SECTORS:
        config = SECTOR_DISCOVERY_CONFIG[sector]
        sectors[sector] = {
            "label": config["label"],
            "recipes": {
                recipe_id: {
                    "discovered_candidates": 0,
                    "captured_candidates": 0,
                    "capture_failed_candidates": 0,
                }
                for recipe_id in recipe_ids
            },
        }

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        sector = candidate.get("sector")
        recipe_id = candidate.get("source_catalog_id")
        discovered_at = _parse_utc_timestamp(candidate.get("discovered_at"))
        if (
            sector not in sectors
            or recipe_id not in sectors[sector]["recipes"]
            or discovered_at is None
            or discovered_at < window_started_at
        ):
            continue
        summary = sectors[sector]["recipes"][recipe_id]
        summary["discovered_candidates"] += 1
        if candidate.get("state") == "captured":
            summary["captured_candidates"] += 1
        elif candidate.get("state") == "capture_failed":
            summary["capture_failed_candidates"] += 1

    gaps: list[dict[str, str]] = []
    for sector, sector_summary in sectors.items():
        for recipe_id, recipe_summary in sector_summary["recipes"].items():
            if recipe_summary["discovered_candidates"] == 0:
                gaps.append(
                    {
                        "sector": sector,
                        "source_catalog_id": recipe_id,
                        "gap_type": "no_discovery_candidates",
                    }
                )

            elif (
                recipe_summary["captured_candidates"] == 0
                and recipe_summary["capture_failed_candidates"] > 0
            ):
                gaps.append(
                    {
                        "sector": sector,
                        "source_catalog_id": recipe_id,
                        "gap_type": "captured_evidence_unavailable",
                    }
                )

    source_effectiveness_feedback = _source_recipe_effectiveness_feedback(
        ledger,
        candidates=candidates,
        sectors=sectors,
        window_started_at=window_started_at,
    )

    source_health = _source_health_records(ledger)
    health_summary = [
        record
        for record in source_health
        if isinstance(record, dict)
        and not is_source_eligible(ledger, source_key=str(record.get("source_key", "")))
    ]
    trusted_sources = ledger.get("trusted_sources")
    if not isinstance(trusted_sources, list):
        raise ValueError("招商线索台账中的 trusted_sources 必须是列表")
    # 仅返回来源目录最小字段，方便人工确认免费官方统计源是否已配置/停用；不返回
    # 页面正文、不触发连接，也不把“active”解释成某项统计、企业项目或招商信号有效。
    official_statistics_sources = sorted(
        [
            {
                "source_id": source.get("source_id"),
                "domain": source.get("domain"),
                "status": source.get("status"),
                "source_level": source.get("source_level"),
                "allowed_claim_types": copy.deepcopy(source.get("allowed_claim_types")),
            }
            for source in trusted_sources
            if isinstance(source, dict) and source.get("source_type") == "official_statistics"
        ],
        key=lambda source: str(source["domain"]),
    )
    generated_at = _utc_now()
    return {
        "window_days": window_days,
        "window_started_at": window_started_at.isoformat(),
        "generated_at": generated_at,
        "sectors": sectors,
        "coverage_gaps": gaps,
        "source_effectiveness_feedback": source_effectiveness_feedback,
        "restricted_sources": health_summary,
        "official_statistics_sources": official_statistics_sources,
        "recent_monitor_runs": _monitoring_runs(ledger)[-20:],
        # 周期状态由既有追加审计推导，报告读取不会尝试取得运行锁、触发任务或写回配置。
        "monitor_schedule": _scheduled_monitor_status(ledger, evaluated_at=generated_at),
        "recent_source_feed_runs": ledger.get("source_feed_runs", [])[-20:],
        "recent_candidate_observations": ledger.get("candidate_observations", [])[-20:],
        "ledger_path": str(ledger_path(workspace_dir)),
    }
