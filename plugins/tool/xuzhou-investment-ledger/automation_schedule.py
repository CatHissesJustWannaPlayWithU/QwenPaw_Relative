"""徐州招商公开来源监控的受控后台定时计划服务。

本模块只登记、读取和暂停一条固定的 ``tool`` 类型 Cron 计划；实际抓取仍由
``source_monitoring`` 的来源白名单、robots、访问退避和证据保护规则完成。它不接受任意
Cron、URL、搜索词、工具参数、用户 ID、证据或线索内容，避免把“后台定时”变成绕过招商
证据治理和平台工具安全策略的入口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qwenpaw.app.crons.models import (
    CronJobSpec,
    CronJobState,
    CronToolRequest,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)

from .scoring_rules import OUTCOME_LEARNING_CYCLE_SCOPE
from .source_monitoring import SCHEDULED_PUBLIC_DISCOVERY_SCOPE
from .storage import load_ledger, save_ledger


# 计划 ID、频率、时区、工具名与参数均是服务器常量：认证用户只能启用或暂停，不能让
# 后台任务访问任意网站、提高频率、改为模型任务，或带入另一个用户/工作区的参数。
SOURCE_MONITOR_SCHEDULE_JOB_ID = "xuzhou-investment-public-source-monitor-v1"
SOURCE_MONITOR_SCHEDULE_NAME = "徐州招商公开来源监控（每6小时，免费来源）"
SOURCE_MONITOR_CRON = "17 */6 * * *"
SOURCE_MONITOR_TIMEZONE = "Asia/Shanghai"
SOURCE_MONITOR_TOOL_NAME = "run_xuzhou_source_monitor"
SOURCE_MONITOR_OPERATION_SCOPE = "authenticated_user_fixed_source_monitor_schedule_only"
OUTCOME_LEARNING_SCHEDULE_JOB_ID = "xuzhou-investment-outcome-learning-v1"
OUTCOME_LEARNING_SCHEDULE_NAME = "徐州招商结果反馈规则学习（每日一次）"
OUTCOME_LEARNING_CRON = "35 3 * * *"
OUTCOME_LEARNING_TIMEZONE = "Asia/Shanghai"
OUTCOME_LEARNING_TOOL_NAME = "run_xuzhou_outcome_learning_cycle"
OUTCOME_LEARNING_OPERATION_SCOPE = "authenticated_user_fixed_outcome_learning_schedule_only"
_AUTOMATION_AUDIT_FIELD = "automation_schedule_audit"
# Cron 管理器各次写入虽有内部锁，但“更新 Cron 后追加台账审计、失败则回滚 Cron”是一个
# 跨两个持久化对象的复合操作；本锁将本插件进程内的启停请求串行化，避免两个认证用户的
# 并发操作互相覆盖或错误回滚对方刚写入的固定计划。
_SOURCE_MONITOR_SCHEDULE_MUTATION_LOCK = asyncio.Lock()


def _utc_now() -> str:
    """生成服务器 UTC 审计时间；计划接口不接受客户端时间，避免伪造操作先后。"""
    return datetime.now(timezone.utc).isoformat()


def _required_server_identity(value: object) -> str:
    """校验认证层传入的当前用户 ID，拒绝请求体、模型或前端自称的身份。"""
    if not isinstance(value, str) or not (user_id := value.strip()):
        raise ValueError("服务器认证用户ID不能为空")
    if len(user_id) > 100:
        raise ValueError("服务器认证用户ID不能超过100个字符")
    return user_id


def _synthetic_schedule_session_id(agent_id: object) -> str:
    """生成计划专用会话标识，避免后台工具冒用用户交互会话或客户端 session ID。"""
    if not isinstance(agent_id, str) or not (normalized_agent_id := agent_id.strip()):
        raise ValueError("当前智能体ID不能为空")
    digest = hashlib.sha256(normalized_agent_id.encode("utf-8")).hexdigest()[:16]
    return f"xuzhou-source-monitor-{digest}"


def _schedule_audit_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """取得只追加的计划操作审计列表；它独立于证据链且仍受整本 HMAC 保护。"""
    records = ledger.get(_AUTOMATION_AUDIT_FIELD)
    if not isinstance(records, list):
        raise ValueError("招商线索台账中的 automation_schedule_audit 必须是列表")
    return records


def _protected_evidence_snapshot(ledger: dict[str, Any]) -> str:
    """快照所有原始证据字段，防止计划审计操作意外改写来源链。"""
    evidence = ledger.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("招商线索台账中的 evidence 必须是列表")
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_schedule_audit(
    ledger: dict[str, Any],
    *,
    action: str,
    authenticated_user_id: str,
    agent_id: str,
    job_id: str,
    cron: str,
    timezone_name: str,
    operation_scope: str,
) -> dict[str, object]:
    """追加固定计划的认证操作审计，且逐字确认原始证据未变。

    输入仅为受控动作、服务器认证用户 ID 和当前智能体 ID，输出为服务器生成的审计记录。
    函数不接受计划参数、证据、URL、原始摘录、采集时间、评分或用户提交的审计字段；记录
    写入前后会比较 ``evidence``，保证普通用户启停后台任务不能成为改写证据链的旁路。
    ``agent_id`` 标识计划实际托管在哪个智能体的 Cron 管理器上，是跨成员防重复启用的
    唯一依据，客户端不能指定或伪造。
    """
    if action not in {"enabled", "paused"}:
        raise ValueError("计划审计动作不受支持")
    if not isinstance(agent_id, str):
        raise ValueError("计划审计的智能体ID不能为空")
    normalized_agent_id = agent_id.strip()
    if not normalized_agent_id:
        raise ValueError("计划审计的智能体ID不能为空")
    if len(normalized_agent_id) > 100:
        raise ValueError("计划审计的智能体ID不能超过100个字符")
    evidence_before = _protected_evidence_snapshot(ledger)
    operated_at = _utc_now()
    digest_source = (
        f"{job_id}\x00{action}\x00{authenticated_user_id}"
        f"\x00{normalized_agent_id}\x00{operated_at}"
    )
    audit = {
        "audit_id": "automation-schedule-"
        + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16],
        "action": action,
        "job_id": job_id,
        "cron": cron,
        "timezone": timezone_name,
        "operated_at": operated_at,
        "operated_by_user_id": authenticated_user_id,
        "agent_id": normalized_agent_id,
        "operation_scope": operation_scope,
    }
    _schedule_audit_records(ledger).append(audit)
    if _protected_evidence_snapshot(ledger) != evidence_before:
        raise RuntimeError("后台计划操作不得修改原始招商证据链")
    return audit


def _latest_schedule_audit_for_job(
    ledger: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any] | None:
    """返回共享台账中某个固定计划的最近一次启停审计，没有任何记录时返回 ``None``。

    输入是已加载的共享台账和服务器固定 job_id，输出为最后追加的那条审计。审计列表本身
    只追加且受整本 HMAC 保护，因此它是判断“计划当前托管在哪个成员/智能体”的可信依据；
    函数不读取 Cron 管理器，也不修改台账。
    """
    latest: dict[str, Any] | None = None
    for record in _schedule_audit_records(ledger):
        if isinstance(record, dict) and record.get("job_id") == job_id:
            latest = record
    return latest


def _assert_schedule_host_consistency(
    ledger: dict[str, Any],
    *,
    job_id: str,
    schedule_display_name: str,
    agent_id: object,
) -> None:
    """拒绝把已由其他成员、其他智能体或未知托管者托管的固定计划重复启用。

    输入是已加载的共享台账、服务器固定 job_id 与当前认证身份；无输出，冲突时抛出
    ``RuntimeError``。共享台账只有一份，但 Cron 任务挂在各智能体自己的管理器上，同 ID
    去重只在单个管理器内有效；因此启用前必须查共享台账的追加审计：

    - 最近一次“enabled”明确记录托管智能体且就是本智能体时，允许（可重建本机丢失的任务）；
    - 审计没有 ``agent_id`` 的历史记录一律视为未知托管者，即使同一登录用户也拒绝跨
      智能体再次启用，必须由原启用者在原托管智能体上暂停再启用，写入真实托管信息；
    - 其余情况说明计划由其他成员或其他智能体托管，拒绝重复启用。

    函数不读取客户端传入的用户/智能体声明，也不修改台账或 Cron。
    """
    latest = _latest_schedule_audit_for_job(ledger, job_id=job_id)
    if latest is None or latest.get("action") != "enabled":
        return
    host_agent_id = latest.get("agent_id")
    if isinstance(host_agent_id, str) and host_agent_id == agent_id:
        return
    if not isinstance(host_agent_id, str):
        raise RuntimeError(
            f"{schedule_display_name}的历史审计未记录托管智能体，为避免重复运行，"
            "请原启用者在原托管智能体上暂停一次再重新启用，写入托管信息后再操作"
        )
    raise RuntimeError(
        f"{schedule_display_name}已由其他招商成员或智能体启用；"
        "共享台账只允许一个后台计划在运行，请先由原启用者暂停"
    )


def _assert_local_job_matches_shared_host(
    ledger: dict[str, Any],
    *,
    job_id: str,
    schedule_display_name: str,
    agent_id: object,
) -> None:
    """本机已有启用中的同名任务时，核对共享台账确认本智能体确实是唯一托管者。

    输入是已加载的共享台账、服务器固定 job_id 与当前智能体；无输出，冲突时抛出
    ``RuntimeError``。幂等提前返回不能跳过托管核对：若历史原因已在多个智能体上存在重复
    任务，或最近审计没有 ``agent_id``，本函数要求先暂停再启用以写入托管信息；本机任务
    若是不该有的重复副本，运维人员也可据此在本机暂停移除。函数不修改台账或 Cron。
    """
    latest = _latest_schedule_audit_for_job(ledger, job_id=job_id)
    if latest is None or latest.get("action") != "enabled":
        return
    host_agent_id = latest.get("agent_id")
    if isinstance(host_agent_id, str) and host_agent_id == agent_id:
        return
    raise RuntimeError(
        f"本智能体已存在启用中的{schedule_display_name}任务，但共享台账审计未确认本机为"
        "唯一托管者；若本机确为唯一托管者，请先暂停再重新启用以写入托管信息，"
        "若本机任务是历史遗留的重复副本，请直接暂停移除"
    )


def _assert_pause_targets_local_host(
    ledger: dict[str, Any],
    *,
    job_id: str,
    schedule_display_name: str,
) -> None:
    """当本智能体的 Cron 管理器上没有对应任务时，拒绝“暂停”一个托管在别处的运行中计划。

    输入是已加载的共享台账与服务器固定 job_id；无输出，冲突时抛出 ``RuntimeError``。
    没有本检查的暂停会静默无操作，成员会误以为团队监控已停止；只有共享台账最近审计显示
    计划仍在运行时本函数才拒绝，未配置或已暂停时保持原有“无变化”行为。函数不修改台账。
    """
    latest = _latest_schedule_audit_for_job(ledger, job_id=job_id)
    if latest is not None and latest.get("action") == "enabled":
        raise RuntimeError(
            f"{schedule_display_name}正由其他招商成员或智能体托管，"
            "本智能体上没有可暂停的任务；请联系原启用者或管理员在其智能体上暂停"
        )


def _build_source_monitor_job(
    *,
    authenticated_user_id: str,
    agent_id: object,
    enabled: bool,
) -> CronJobSpec:
    """构造唯一固定的后台来源监控任务，不暴露任何调用方可写的调度或工具参数。

    输入是认证主体和当前智能体 ID，仅用于计划结果投递上下文；输出为 QwenPaw Cron 规格。
    Cron 直接调用已登记的 ``cron_safe`` 工具，固定传递预设范围标记，绝不经过模型，也不
    接受客户端指定的 URL、行业、搜索词、线索或证据。监控工具仍会限制每轮访问和保存范围，
    不会自动确认、推进或改写正式证据。
    """
    return CronJobSpec(
        id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
        name=SOURCE_MONITOR_SCHEDULE_NAME,
        enabled=enabled,
        schedule=ScheduleSpec(
            type="cron",
            cron=SOURCE_MONITOR_CRON,
            timezone=SOURCE_MONITOR_TIMEZONE,
        ),
        task_type="tool",
        tool=CronToolRequest(
            name=SOURCE_MONITOR_TOOL_NAME,
            arguments={"run_scope": SCHEDULED_PUBLIC_DISCOVERY_SCOPE},
        ),
        dispatch=DispatchSpec(
            type="channel",
            channel="console",
            target=DispatchTarget(
                user_id=authenticated_user_id,
                session_id=_synthetic_schedule_session_id(agent_id),
            ),
            mode="final",
            silent=True,
            meta={"purpose": "xuzhou_public_source_monitor"},
        ),
        save_result_to_inbox=True,
        runtime=JobRuntimeSpec(
            max_concurrency=1,
            timeout_seconds=240,
            misfire_grace_seconds=1800,
            share_session=False,
            tool_safety=True,
        ),
        meta={
            "purpose": "xuzhou_public_source_monitor",
            "source_policy": "free_public_sources_robots_rss_sitemap_search_only",
            "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
        },
    )


def _assert_managed_source_monitor_job(job: CronJobSpec) -> None:
    """验证同 ID 的既有任务仍是本模块创建的固定安全计划，拒绝覆盖陌生任务。"""
    if (
        job.id != SOURCE_MONITOR_SCHEDULE_JOB_ID
        or job.task_type != "tool"
        or job.tool is None
        or job.tool.name != SOURCE_MONITOR_TOOL_NAME
        or job.tool.arguments != {"run_scope": SCHEDULED_PUBLIC_DISCOVERY_SCOPE}
        or job.schedule.type != "cron"
        or job.schedule.cron != SOURCE_MONITOR_CRON
        or job.schedule.timezone != SOURCE_MONITOR_TIMEZONE
        or job.meta.get("operation_scope") != SOURCE_MONITOR_OPERATION_SCOPE
    ):
        raise RuntimeError("发现同名但不受控的后台计划，拒绝覆盖，请由系统管理员人工核验")


def _cron_manager_or_raise(workspace: object) -> Any:
    """取得当前工作区已启动的 Cron 管理器，缺失时失败关闭而不写台账审计。"""
    manager = getattr(workspace, "cron_manager", None)
    if manager is None:
        raise RuntimeError("当前智能体的后台定时服务未初始化，无法登记来源监控")
    return manager


def _required_shared_ledger_dir(ledger_dir: object) -> Path:
    """校验路由层解析的共享台账目录，拒绝把工作区或客户端路径写入计划审计。

    输入只能是 ``shared_ledger_dir_for_request`` 在服务器端按认证主体和团队名册解析出的
    ``Path``；输出为规范绝对路径。本函数不从 HTTP 请求、模型参数或 Cron 参数读取路径，
    因此计划启停不能跨团队读写数据。符号链接会被拒绝，避免计划审计成为绕过共享目录
    私有权限与 HMAC 保护的写入通道；它不读取或修改台账、证据或 Cron。
    """
    if not isinstance(ledger_dir, Path):
        raise ValueError("共享招商台账目录无效")
    if ledger_dir.is_symlink():
        raise ValueError("共享招商台账目录不能是符号链接")
    resolved = ledger_dir.resolve()
    if not resolved.is_absolute():
        raise ValueError("共享招商台账目录必须是绝对路径")
    return resolved


def _public_schedule_state(job: CronJobSpec | None, state: CronJobState | None) -> dict[str, object]:
    """返回脱敏计划状态；不回显调度目标用户 ID、会话 ID、令牌或任意工作区路径。"""
    if job is None:
        return {
            "configured": False,
            "job_id": SOURCE_MONITOR_SCHEDULE_JOB_ID,
            "fixed_cron": SOURCE_MONITOR_CRON,
            "timezone": SOURCE_MONITOR_TIMEZONE,
            "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
        }
    state_payload = state.model_dump(mode="json") if state is not None else {}
    return {
        "configured": True,
        "job_id": SOURCE_MONITOR_SCHEDULE_JOB_ID,
        "enabled": job.enabled,
        "fixed_cron": SOURCE_MONITOR_CRON,
        "timezone": SOURCE_MONITOR_TIMEZONE,
        "tool_name": SOURCE_MONITOR_TOOL_NAME,
        "tool_arguments": {"run_scope": SCHEDULED_PUBLIC_DISCOVERY_SCOPE},
        "runtime_state": state_payload,
        "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
    }


def _team_schedule_host_hint(
    ledger_dir: Path | None,
    *,
    job_id: str,
) -> dict[str, object]:
    """当本智能体上没有对应计划时，从共享台账审计给出“计划托管在别处”的脱敏提示。

    输入是路由层按认证主体解析的共享台账目录（状态查询为只读时允许为 ``None``）与服务器
    固定 job_id；输出仅含最近一次动作与服务器时间，不回显操作者、投递目标或会话标识。
    它解决“监控已在同事的智能体上运行，但本智能体状态显示未配置”的误导；台账读取失败
    或没有审计时只返回空提示，不影响状态查询本身。函数不修改台账或 Cron。
    """
    if ledger_dir is None:
        return {}
    try:
        ledger = load_ledger(_required_shared_ledger_dir(ledger_dir))
        latest = _latest_schedule_audit_for_job(ledger, job_id=job_id)
    except (OSError, RuntimeError, ValueError):
        return {}
    if latest is None:
        return {}
    return {
        "team_last_action": latest.get("action"),
        "team_last_operated_at": latest.get("operated_at"),
        "hosted_by_current_agent": False,
    }


async def get_source_monitor_schedule_status(
    workspace: object,
    *,
    ledger_dir: Path | None = None,
) -> dict[str, object]:
    """只读查询当前工作区的固定来源监控计划状态，不联网、不执行任务、不写台账。

    本智能体上没有该计划时，附带共享台账审计中的最近一次团队启停动作，避免成员把
    “由同事托管的运行中计划”误读为“未配置”。
    """
    manager = _cron_manager_or_raise(workspace)
    job = await manager.get_job(SOURCE_MONITOR_SCHEDULE_JOB_ID)
    if job is None:
        return {
            **_public_schedule_state(None, None),
            "team_host_hint": _team_schedule_host_hint(
                ledger_dir,
                job_id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
            ),
        }
    _assert_managed_source_monitor_job(job)
    return _public_schedule_state(job, manager.get_state(SOURCE_MONITOR_SCHEDULE_JOB_ID))


async def _set_source_monitor_schedule_enabled_locked(
    workspace: object,
    *,
    ledger_dir: Path,
    authenticated_user_id: str,
    enabled: bool,
) -> dict[str, object]:
    """以认证用户身份启用或暂停固定来源监控，并追加不可覆盖的业务审计。

    输入仅为服务器认证的当前用户 ID 与服务器路由固定动作；输出为脱敏计划状态和审计。
    调用方不能提供 Cron、时区、工具参数、投递目标、用户 ID、URL、证据或线索。启用时若
    同 ID 已被未知任务占用则拒绝覆盖；暂停时只把既有受控任务的 ``enabled`` 设为 false。
    先更新当前运行实例的 QwenPaw Cron，再在团队共享台账追加审计；若审计保存失败则恢复先前计划，避免出现
    “已启用却没有可追溯记录”的状态。全过程不触及原始证据链、人工复核或招商推进。
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled必须是布尔值")
    user_id = _required_server_identity(authenticated_user_id)
    shared_ledger_dir = _required_shared_ledger_dir(ledger_dir)
    manager = _cron_manager_or_raise(workspace)
    agent_id = getattr(workspace, "agent_id", None)
    existing_job = await manager.get_job(SOURCE_MONITOR_SCHEDULE_JOB_ID)
    if existing_job is not None:
        _assert_managed_source_monitor_job(existing_job)
        if existing_job.enabled is enabled:
            if enabled:
                # 幂等提前返回也要核对共享台账托管者：历史重复任务或缺失 agent_id 的
                # 旧审计不能就此隐身，必须先暂停再启用完成托管登记。
                _assert_local_job_matches_shared_host(
                    load_ledger(shared_ledger_dir),
                    job_id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
                    schedule_display_name="来源监控计划",
                    agent_id=agent_id,
                )
            return {
                "changed": False,
                "schedule": _public_schedule_state(
                    existing_job,
                    manager.get_state(SOURCE_MONITOR_SCHEDULE_JOB_ID),
                ),
                "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
            }

    # 台账审计是跨成员判断计划托管位置的唯一可信依据，必须先于 Cron 变更完成校验。
    ledger = load_ledger(shared_ledger_dir)
    if enabled:
        _assert_schedule_host_consistency(
            ledger,
            job_id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
            schedule_display_name="来源监控计划",
            agent_id=agent_id,
        )
    elif existing_job is None:
        _assert_pause_targets_local_host(
            ledger,
            job_id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
            schedule_display_name="来源监控计划",
        )

    new_job = (
        _build_source_monitor_job(
            authenticated_user_id=user_id,
            agent_id=agent_id,
            enabled=True,
        )
        if enabled
        else existing_job.model_copy(update={"enabled": False})
        if existing_job is not None
        else None
    )
    if new_job is None:
        # 未曾登记过计划时不能凭“暂停”制造配置；调用方需先启用，状态接口会返回未配置。
        return {
            "changed": False,
            "schedule": _public_schedule_state(None, None),
            "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
        }

    await manager.create_or_replace_job(new_job)
    try:
        audit = _append_schedule_audit(
            ledger,
            action="enabled" if enabled else "paused",
            authenticated_user_id=user_id,
            agent_id=agent_id if isinstance(agent_id, str) else "",
            job_id=SOURCE_MONITOR_SCHEDULE_JOB_ID,
            cron=SOURCE_MONITOR_CRON,
            timezone_name=SOURCE_MONITOR_TIMEZONE,
            operation_scope=SOURCE_MONITOR_OPERATION_SCOPE,
        )
        save_ledger(shared_ledger_dir, ledger)
    except Exception:
        # 保存 HMAC 审计失败时恢复原计划（或删除新计划）；不能让无人值守访问脱离审计。
        if existing_job is None:
            await manager.delete_job(SOURCE_MONITOR_SCHEDULE_JOB_ID)
        else:
            await manager.create_or_replace_job(existing_job)
        raise
    return {
        "changed": True,
        "schedule": _public_schedule_state(
            new_job,
            manager.get_state(SOURCE_MONITOR_SCHEDULE_JOB_ID),
        ),
        "audit": audit,
        "operation_scope": SOURCE_MONITOR_OPERATION_SCOPE,
    }


async def set_source_monitor_schedule_enabled(
    workspace: object,
    *,
    ledger_dir: Path,
    authenticated_user_id: str,
    enabled: bool,
) -> dict[str, object]:
    """串行启用或暂停固定监控计划，防止并发认证请求交叉覆盖 Cron 与审计。

    输入、输出、权限判断和证据保护规则与内部实现一致：身份只能来自 HTTP 认证主体，共享
    台账目录只能由服务器路由解析；调用方不能提交 Cron、工具、URL、搜索词、会话、线索或证据。输出仅给出脱敏状态与服务器
    追加审计；互斥锁不扩大权限，也不会等待或执行实际抓取。锁内的复合操作若审计保存失败
    会恢复前一 Cron 配置，因此后台访问与台账审计保持一致，原始证据链始终不被修改。
    """
    async with _SOURCE_MONITOR_SCHEDULE_MUTATION_LOCK:
        return await _set_source_monitor_schedule_enabled_locked(
            workspace,
            ledger_dir=ledger_dir,
            authenticated_user_id=authenticated_user_id,
            enabled=enabled,
        )


def _build_outcome_learning_job(
    *,
    authenticated_user_id: str,
    agent_id: object,
    enabled: bool,
) -> CronJobSpec:
    """构造唯一固定的每日结果反馈学习任务，不接受调用方指定规则、权重或模型输入。

    输入仅为服务端认证用户和当前智能体 ID；输出是固定 Cron 规格。计划直接调用已经
    ``cron_safe`` 的结果学习工具，工具参数只能是服务器常量范围标记。学习核心只有在
    充分的人工最终结果反馈下才会创建一版受限的新评分规则；它不联网、不读取网页、不改
    原始证据、线索状态、人工复核或已有结果反馈，更不会更新 Skill 或自行推进招商项目。
    """
    if not isinstance(agent_id, str) or not (normalized_agent_id := agent_id.strip()):
        raise ValueError("当前智能体ID不能为空")
    session_digest = hashlib.sha256(normalized_agent_id.encode("utf-8")).hexdigest()[:16]
    return CronJobSpec(
        id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
        name=OUTCOME_LEARNING_SCHEDULE_NAME,
        enabled=enabled,
        schedule=ScheduleSpec(
            type="cron",
            cron=OUTCOME_LEARNING_CRON,
            timezone=OUTCOME_LEARNING_TIMEZONE,
        ),
        task_type="tool",
        tool=CronToolRequest(
            name=OUTCOME_LEARNING_TOOL_NAME,
            arguments={"run_scope": OUTCOME_LEARNING_CYCLE_SCOPE},
        ),
        dispatch=DispatchSpec(
            type="channel",
            channel="console",
            target=DispatchTarget(
                user_id=authenticated_user_id,
                session_id=f"xuzhou-outcome-learning-{session_digest}",
            ),
            mode="final",
            silent=True,
            meta={"purpose": "xuzhou_outcome_learning"},
        ),
        save_result_to_inbox=True,
        runtime=JobRuntimeSpec(
            max_concurrency=1,
            timeout_seconds=120,
            misfire_grace_seconds=7200,
            share_session=False,
            tool_safety=True,
        ),
        meta={
            "purpose": "xuzhou_outcome_learning",
            "learning_policy": "bounded_final_feedback_rule_versioning_only",
            "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
        },
    )


def _assert_managed_outcome_learning_job(job: CronJobSpec) -> None:
    """验证同 ID 既有任务是受控学习计划，拒绝把陌生 Cron 覆盖为后台学习任务。"""
    if (
        job.id != OUTCOME_LEARNING_SCHEDULE_JOB_ID
        or job.task_type != "tool"
        or job.tool is None
        or job.tool.name != OUTCOME_LEARNING_TOOL_NAME
        or job.tool.arguments != {"run_scope": OUTCOME_LEARNING_CYCLE_SCOPE}
        or job.schedule.type != "cron"
        or job.schedule.cron != OUTCOME_LEARNING_CRON
        or job.schedule.timezone != OUTCOME_LEARNING_TIMEZONE
        or job.meta.get("operation_scope") != OUTCOME_LEARNING_OPERATION_SCOPE
    ):
        raise RuntimeError("发现同名但不受控的规则学习计划，拒绝覆盖，请由系统管理员人工核验")


def _public_outcome_learning_schedule_state(
    job: CronJobSpec | None,
    state: CronJobState | None,
) -> dict[str, object]:
    """返回脱敏结果学习计划状态，不回显投递用户、会话、令牌或任何规则内容。"""
    if job is None:
        return {
            "configured": False,
            "job_id": OUTCOME_LEARNING_SCHEDULE_JOB_ID,
            "fixed_cron": OUTCOME_LEARNING_CRON,
            "timezone": OUTCOME_LEARNING_TIMEZONE,
            "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
        }
    state_payload = state.model_dump(mode="json") if state is not None else {}
    return {
        "configured": True,
        "job_id": OUTCOME_LEARNING_SCHEDULE_JOB_ID,
        "enabled": job.enabled,
        "fixed_cron": OUTCOME_LEARNING_CRON,
        "timezone": OUTCOME_LEARNING_TIMEZONE,
        "tool_name": OUTCOME_LEARNING_TOOL_NAME,
        "tool_arguments": {"run_scope": OUTCOME_LEARNING_CYCLE_SCOPE},
        "runtime_state": state_payload,
        "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
    }


async def get_outcome_learning_schedule_status(
    workspace: object,
    *,
    ledger_dir: Path | None = None,
) -> dict[str, object]:
    """只读查询每日规则学习计划状态；不运行学习、不写规则、不读取或改变原始证据。

    本智能体上没有该计划时，附带共享台账审计中的最近一次团队启停动作，含义与来源
    监控状态中的 ``team_host_hint`` 相同。
    """
    manager = _cron_manager_or_raise(workspace)
    job = await manager.get_job(OUTCOME_LEARNING_SCHEDULE_JOB_ID)
    if job is None:
        return {
            **_public_outcome_learning_schedule_state(None, None),
            "team_host_hint": _team_schedule_host_hint(
                ledger_dir,
                job_id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
            ),
        }
    _assert_managed_outcome_learning_job(job)
    return _public_outcome_learning_schedule_state(
        job,
        manager.get_state(OUTCOME_LEARNING_SCHEDULE_JOB_ID),
    )


async def _set_outcome_learning_schedule_enabled_locked(
    workspace: object,
    *,
    ledger_dir: Path,
    authenticated_user_id: str,
    enabled: bool,
) -> dict[str, object]:
    """在共享互斥锁内启用或暂停固定学习计划，并把认证操作追加进台账审计。

    输入只能是服务器认证用户 ID 与固定布尔动作；输出是脱敏状态和审计。客户端、模型和
    Cron 均不能传入权重、规则、反馈、用户 ID、工具参数、URL、证据或线索。计划更新后若
    台账审计保存失败，将回滚到原 Cron 规格（新任务则删除），避免无人值守学习脱离追溯；
    正常启停操作也不会立即运行学习或改变任何评分规则。
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled必须是布尔值")
    user_id = _required_server_identity(authenticated_user_id)
    shared_ledger_dir = _required_shared_ledger_dir(ledger_dir)
    manager = _cron_manager_or_raise(workspace)
    agent_id = getattr(workspace, "agent_id", None)
    existing_job = await manager.get_job(OUTCOME_LEARNING_SCHEDULE_JOB_ID)
    if existing_job is not None:
        _assert_managed_outcome_learning_job(existing_job)
        if existing_job.enabled is enabled:
            if enabled:
                # 与来源监控相同：幂等提前返回也要核对共享台账托管者，历史重复任务或
                # 缺失 agent_id 的旧审计不能就此隐身，必须先暂停再启用完成托管登记。
                _assert_local_job_matches_shared_host(
                    load_ledger(shared_ledger_dir),
                    job_id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
                    schedule_display_name="结果学习计划",
                    agent_id=agent_id,
                )
            return {
                "changed": False,
                "schedule": _public_outcome_learning_schedule_state(
                    existing_job,
                    manager.get_state(OUTCOME_LEARNING_SCHEDULE_JOB_ID),
                ),
                "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
            }
    # 与来源监控相同：先查共享台账审计判断计划托管位置，再决定是否允许变更 Cron。
    ledger = load_ledger(shared_ledger_dir)
    if enabled:
        _assert_schedule_host_consistency(
            ledger,
            job_id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
            schedule_display_name="结果学习计划",
            agent_id=agent_id,
        )
    elif existing_job is None:
        _assert_pause_targets_local_host(
            ledger,
            job_id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
            schedule_display_name="结果学习计划",
        )
    new_job = (
        _build_outcome_learning_job(
            authenticated_user_id=user_id,
            agent_id=agent_id,
            enabled=True,
        )
        if enabled
        else existing_job.model_copy(update={"enabled": False})
        if existing_job is not None
        else None
    )
    if new_job is None:
        return {
            "changed": False,
            "schedule": _public_outcome_learning_schedule_state(None, None),
            "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
        }
    await manager.create_or_replace_job(new_job)
    try:
        audit = _append_schedule_audit(
            ledger,
            action="enabled" if enabled else "paused",
            authenticated_user_id=user_id,
            agent_id=agent_id if isinstance(agent_id, str) else "",
            job_id=OUTCOME_LEARNING_SCHEDULE_JOB_ID,
            cron=OUTCOME_LEARNING_CRON,
            timezone_name=OUTCOME_LEARNING_TIMEZONE,
            operation_scope=OUTCOME_LEARNING_OPERATION_SCOPE,
        )
        save_ledger(shared_ledger_dir, ledger)
    except Exception:
        if existing_job is None:
            await manager.delete_job(OUTCOME_LEARNING_SCHEDULE_JOB_ID)
        else:
            await manager.create_or_replace_job(existing_job)
        raise
    return {
        "changed": True,
        "schedule": _public_outcome_learning_schedule_state(
            new_job,
            manager.get_state(OUTCOME_LEARNING_SCHEDULE_JOB_ID),
        ),
        "audit": audit,
        "operation_scope": OUTCOME_LEARNING_OPERATION_SCOPE,
    }


async def set_outcome_learning_schedule_enabled(
    workspace: object,
    *,
    ledger_dir: Path,
    authenticated_user_id: str,
    enabled: bool,
) -> dict[str, object]:
    """串行启用或暂停每日规则学习计划；权限、审计回滚和证据保护均由服务器执行。"""
    async with _SOURCE_MONITOR_SCHEDULE_MUTATION_LOCK:
        return await _set_outcome_learning_schedule_enabled_locked(
            workspace,
            ledger_dir=ledger_dir,
            authenticated_user_id=authenticated_user_id,
            enabled=enabled,
        )
