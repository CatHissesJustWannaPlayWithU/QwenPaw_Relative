"""供徐州招商智能体调用的受控工具入口。

工具负责登记、读取、低频自动发现及受限抓取公开证据。人工复核与评分规则修改
不注册为模型工具，而是经由已认证 HTTP 接口完成，以避免模型或前端传入伪造身份。
"""

from __future__ import annotations

import logging
from typing import Any

from qwenpaw.app.agent_context import get_current_agent_id
from qwenpaw.app.inbox_store import append_event, list_events as list_inbox_events

from .action_center import get_investment_action_center, get_outreach_preparation_brief
from .adaptive_learning import run_adaptive_learning_cycle
from .core import get_investment_lead, list_investment_leads
from .candidate_observation import get_candidate_observation, list_candidate_observations
from .reports import (
    get_industry_subdirection_coverage_report,
    get_investment_assessment_report,
    get_investment_work_summary,
)
from .relationship_graph import get_relationship_graph, get_relationship_review_queue
from .relationship_draft import list_relationship_drafts
from .co_investment_signal import list_co_investment_signal_drafts
from .industry_metrics import get_industry_business_metrics_report
from .scoring_rules import (
    OUTCOME_LEARNING_CYCLE_SCOPE,
    run_outcome_learning_cycle,
)
from .source_discovery import (
    build_discovery_queries,
    capture_discovery_candidate,
    inspect_discovery_candidate,
)
from .source_catalog import (
    ensure_free_verified_media_source_seeds,
    ensure_official_statistics_source_seeds,
)
from .source_feed_discovery import discover_trusted_source_feeds
from .source_monitoring import (
    SCHEDULED_PUBLIC_DISCOVERY_SCOPE,
    get_source_coverage_report,
    run_scheduled_public_discovery,
)
from .shared_workspace import shared_ledger_dir_for_current_execution


logger = logging.getLogger(__name__)
LOCAL_INBOX_SOURCE_TYPE = "xuzhou_source_monitor"
LOCAL_INBOX_EVENT_TYPE = "xuzhou_source_monitor_manual_review_alert"
LOCAL_INBOX_OPERATION_SCOPE = "free_local_inbox_manual_review_notification_only"


def _workspace_dir():
    """兼容既有工具调用名，返回已授权执行用户对应的团队共享台账目录。

    输入为空，输出是 ``shared_ledger_dir_for_current_execution`` 以服务器执行上下文中的
    ``current_user_id``、平台用户目录和团队名册三重核验后给出的固定目录。历史函数名保留
    是为了不破坏已经注册的工具与定时任务，但它不再读取当前智能体工作区：所有业务数据
    （线索、证据、快照、评分、复核、学习和审计）现在只写入团队共享台账。模型、前端和
    Cron 参数均不能传入 user_id 或路径；上下文缺失、账号停用或成员撤销都会失败关闭。
    本函数不写任何业务内容，也不会影响仅由下方收件箱函数使用的当前智能体本机提醒。
    """
    return shared_ledger_dir_for_current_execution()


def _scheduled_monitor_manual_review_alert(
    monitor_result: dict[str, Any],
) -> dict[str, object] | None:
    """从服务器生成的监控审计提取“仅供人工复核”的本机提醒摘要。

    输入是 ``run_scheduled_public_discovery`` 已完成后返回的服务器审计，输出为可写入当前
    智能体本机收件箱的最小提醒，或在没有高优先级候选/连续失败告警时返回 ``None``。
    该函数不接收模型、前端或 Cron 传入的企业名称、用户 ID、收件人、URL、证据或评分；
    只重述已记录的候选观察数量和运行治理告警。候选高优先级与运行失败都不是企业红色
    风险预警，更不能被理解为企业已经融资、寻求落地或存在任何已确认招商事实。
    """

    monitor_run = monitor_result.get("monitor_run")
    if not isinstance(monitor_run, dict):
        return None
    monitor_run_id = monitor_run.get("monitor_run_id")
    if not isinstance(monitor_run_id, str) or not monitor_run_id:
        return None

    observation_result = monitor_run.get("candidate_observation_result")
    high_priority_count = (
        observation_result.get("high_priority_observation_count", 0)
        if isinstance(observation_result, dict)
        else 0
    )
    # 运行审计来自服务端，但仍作类型边界校验，防止异常数据被拼成误导性的企业风险提示。
    if not isinstance(high_priority_count, int) or isinstance(high_priority_count, bool):
        high_priority_count = 0
    high_priority_count = max(high_priority_count, 0)

    failure_alert = monitor_run.get("failure_alert")
    has_failure_alert = isinstance(failure_alert, dict)
    if high_priority_count == 0 and not has_failure_alert:
        return None

    alert_kinds: list[str] = []
    body_lines: list[str] = []
    if high_priority_count:
        alert_kinds.append("pending_evidence_high_priority_candidate")
        body_lines.append(
            f"本轮公开来源监控发现 {high_priority_count} 条高优先级待补证候选页面观察。"
            "这只是页面快照的受控词表命中或变化提示，不是企业融资、合作、投资或落地事实；"
            "请在候选观察队列中人工核验原始页面。"
        )
    if has_failure_alert:
        alert_kinds.append("monitoring_governance_failure")
        consecutive_failure_count = failure_alert.get("consecutive_failure_count")
        failure_count_text = (
            str(consecutive_failure_count)
            if isinstance(consecutive_failure_count, int)
            and not isinstance(consecutive_failure_count, bool)
            else "达到阈值"
        )
        body_lines.append(
            f"后台监控连续未完整成功次数为 {failure_count_text}，请检查来源访问、退避和任务配置。"
            "这属于运行治理告警，不是企业异常。"
        )

    return {
        "monitor_run_id": monitor_run_id,
        "title": "徐州招商高优先级待补证提醒",
        "body": "\n".join(body_lines),
        "payload": {
            "monitor_run_id": monitor_run_id,
            "alert_kinds": alert_kinds,
            "high_priority_observation_count": high_priority_count,
            "requires_manual_review": True,
            "automatic_business_conclusion": "not_generated",
            "evidence_mutation": "not_performed",
            "operation_scope": LOCAL_INBOX_OPERATION_SCOPE,
        },
    }


async def _append_scheduled_monitor_manual_review_alert(
    monitor_result: dict[str, Any],
) -> dict[str, object]:
    """向当前智能体的免费本机收件箱追加一条去重的高优先级人工复核提醒。

    输入是服务器监控结果，输出为 ``not_required``、``already_exists`` 或 ``created`` 的
    脱敏投递状态。收件箱所属智能体只能由 QwenPaw 的当前执行上下文取得，绝不接受调用
    方声明的用户/智能体 ID；同一 ``monitor_run_id`` 只保留一条提醒。该函数只写平台
    收件箱事件，不向短信、邮件、企业微信或任意 webhook 发送内容，不保存候选正文，
    不写招商台账，因而不能更改证据 URL、原始摘录、采集时间、评分或业务状态。
    """

    alert = _scheduled_monitor_manual_review_alert(monitor_result)
    if alert is None:
        return {"status": "not_required", "operation_scope": LOCAL_INBOX_OPERATION_SCOPE}

    agent_id = get_current_agent_id()
    monitor_run_id = str(alert["monitor_run_id"])
    existing_events = await list_inbox_events(
        limit=500,
        source_type=LOCAL_INBOX_SOURCE_TYPE,
        agent_id=agent_id,
    )
    if any(
        event.get("source_id") == monitor_run_id
        and event.get("event_type") == LOCAL_INBOX_EVENT_TYPE
        for event in existing_events
    ):
        return {
            "status": "already_exists",
            "monitor_run_id": monitor_run_id,
            "operation_scope": LOCAL_INBOX_OPERATION_SCOPE,
        }

    event = await append_event(
        agent_id=agent_id,
        source_type=LOCAL_INBOX_SOURCE_TYPE,
        source_id=monitor_run_id,
        event_type=LOCAL_INBOX_EVENT_TYPE,
        status="manual_review_required",
        severity="error",
        title=str(alert["title"]),
        body=str(alert["body"]),
        payload=alert["payload"] if isinstance(alert["payload"], dict) else {},
    )
    return {
        "status": "created",
        "monitor_run_id": monitor_run_id,
        "inbox_event_id": event.get("id"),
        "operation_scope": LOCAL_INBOX_OPERATION_SCOPE,
    }


async def list_xuzhou_investment_leads(
    sector: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """列出团队共享台账中已登记的招商线索，不会重新采集任何网页数据。"""
    try:
        result = list_investment_leads(
            _workspace_dir(), sector=sector, review_state=review_state, limit=limit
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_investment_lead(lead_id: str) -> dict[str, Any]:
    """查看单条招商线索的状态历史和可追溯公开证据链。"""
    try:
        result = get_investment_lead(_workspace_dir(), lead_id=lead_id)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_investment_action_center(limit: int = 50) -> dict[str, Any]:
    """只读查看团队共享台账的红色预警、关系待办、来源对齐和时效复核行动中心。

    输入只有 1–200 的展示上限，输出固定排序的人工待办和完整分类计数。工具不发送外部
    消息或企业联系，不接收用户 ID、收件人、URL、证据正文或写入字段；红色预警和多来源
    记录只提示人工核验，绝不自动认定事实冲突、招商意愿或推进结论，也不改证据链。
    """
    try:
        result = get_investment_action_center(_workspace_dir(), limit=limit)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_outreach_preparation_brief(lead_id: str) -> dict[str, Any]:
    """只读生成单线索招商准备卡，供人工核验后决定是否与企业或基金联系。

    输入只能是服务器已有 lead_id，输出固定规则生成的融资、徐州关联、合作扩张、风险和
    关系待办准备动作及证据 ID。工具不会发消息、承诺政策、改变线索状态、关系或评分，
    不接收联系人、用户 ID、URL 或写入内容；普通登录用户仍须通过认证业务接口完成复核。
    """
    try:
        result = get_outreach_preparation_brief(_workspace_dir(), lead_id=lead_id)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def discover_xuzhou_public_sources(
    sector: str,
    max_queries: int = 4,
    max_results_per_query: int = 5,
) -> dict[str, Any]:
    """从已核验可信目录优先发现指定产业的免费公开候选链接。

    输入是受控产业标识和为兼容旧调用保留的小批量参数；输出是服务器生成的可信目录发现
    审计及候选页。函数先在源码固定的免费统计/媒体种子中补齐缺失目录，再只读取 robots
    允许的已启用来源首页、RSS 或 Sitemap；它不再把低质量 Bing RSS 当作手动发现入口。
    ``max_queries`` 与 ``max_results_per_query`` 仅校验旧工具调用的合法范围，不能改变访问
    来源、关键词、URL、行业细分或最多五个来源的硬上限。候选默认只是 ``lead_only``，不
    是招商事实，不写入线索、评分或人工状态；尤其是媒体标题缺中国业务依据的候选只能进入
    原页待补证队列，不能直接成为正式 evidence。
    """
    try:
        # 重用公开发现器的固定产业/参数校验，但不执行其旧 Bing 网络访问；返回的 canonical
        # sector 仅供可信目录筛选，调用方不能把英文拆词、URL 或自定义检索词传入来源发现器。
        query_plan = build_discovery_queries(
            sector,
            max_queries=max_queries,
        )
        if not query_plan:
            raise RuntimeError("可信来源手动发现未生成有效产业计划")
        canonical_sector = query_plan[0].get("sector")
        if not isinstance(canonical_sector, str):
            raise RuntimeError("可信来源手动发现缺少规范产业标识")
        if not isinstance(max_results_per_query, int) or isinstance(max_results_per_query, bool):
            raise ValueError("max_results_per_query必须是整数")
        if not 1 <= max_results_per_query <= 10:
            raise ValueError("max_results_per_query必须是1到10之间的整数")

        workspace_dir = _workspace_dir()
        official_seed_result = ensure_official_statistics_source_seeds(workspace_dir)
        media_seed_result = ensure_free_verified_media_source_seeds(workspace_dir)
        result = discover_trusted_source_feeds(
            workspace_dir,
            requested_sector=canonical_sector,
            # 此处是唯一的手动优先入口，布尔值来自源码而非模型参数；它确保用户测试产业
            # 时先读取经过预检的中国财经媒体，而不是低质量公共搜索结果。进一步限定为
            # 固定产业栏目，避免首页导航和泛新闻链接占用人工测试额度。
            curated_media_first=True,
            fixed_sector_index_only=True,
        )
        return {
            "ok": True,
            "discovery_mode": "trusted_sources_first_no_bing_manual_search",
            "requested_sector": canonical_sector,
            "official_statistics_seed_result": official_seed_result,
            "free_verified_media_seed_result": media_seed_result,
            **result,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def capture_xuzhou_discovery_candidate(
    candidate_id: str,
    entity_name: str,
    entity_kind: str,
    claim_type: str,
    source_published_at: str,
    excerpt: str,
    xuzhou_association_tags: list[str] | None = None,
) -> dict[str, Any]:
    """抓取已自动发现的候选页，在原文可核验时追加低等级公开证据。

    输入只含候选 ID 和从该页面识别的实体、**请求分类**、发布日期及原文摘录；输出为抓取
    状态和新增证据。来源 URL、页面标题、采集时间和内容哈希全部由服务器抓取结果决定，
    调用方无法伪造；实体名、发布日期及摘录必须存在于可见正文，否则只记录抓取失败而不
    写证据。服务器会用标题和摘录固定词表重新分类：无支持的请求会保存为
    ``unclassified``，明确主体风险会覆盖为 ``risk``，因此模型/前端不能用主观标签触发
    融资、合作、来源升格或风险结论。未匹配可信来源目录时成功记录为 ``lead_only``；只有
    最终域名与服务器分类均匹配已启用目录时才可采用其一手或二手等级。无论来源等级如何，
    工具都不会自动评分、推进或替代人工复核。
    """
    try:
        result = capture_discovery_candidate(
            _workspace_dir(),
            candidate_id=candidate_id,
            entity_name=entity_name,
            entity_kind=entity_kind,
            claim_type=claim_type,
            source_published_at=source_published_at,
            excerpt=excerpt,
            xuzhou_association_tags=xuzhou_association_tags,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def inspect_xuzhou_discovery_candidate(candidate_id: str) -> dict[str, Any]:
    """读取自动发现候选的受限正文快照，供智能体形成可核验的抓取字段。

    输入只有候选 ID，输出为最多 12,000 个字符的正文、标题、最终链接、哈希和读取
    时间；工具不接收 URL，不写入线索或证据，也不改变候选状态。智能体只能依据该
    快照提出实体名、发布日期和原始摘录；提交抓取时服务器仍会重新读取页面并校验，
    因此本次查看不会绕过证据链的真实性和不可改写边界。
    """
    try:
        result = inspect_discovery_candidate(_workspace_dir(), candidate_id=candidate_id)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def run_xuzhou_source_monitor(run_scope: str) -> dict[str, Any]:
    """执行一次仅供 Cron 固定参数调用的后台公开候选发现。

    输入必须是服务器注册时写死的 ``scheduled_public_discovery`` 标记；输出是三条预设
    产业线的任务审计，或“周期未到/已有任务运行”的安全跳过结果。函数不接受行业、URL、
    搜索词、抓取字段或操作者 ID，因而 Cron 与模型都不能借此扩大访问范围；同一共享台账
    两次实际运行至少间隔六小时，且全程由非阻塞执行锁防止重叠。连续三次未完整成功时
    运行审计会出现仅供人工检查部署与来源覆盖的红色治理告警，不是企业异常。它会在
    robots 允许、每轮最多三个不同域名的前提下保存候选页面原始快照，并用固定词表标为
    “待补证”人工优先级；这些命中不是事实。出现此类高优先级待办时，只向当前智能体
    的免费本机收件箱追加去重提醒，不发送外部消息。工具不写 evidence、不创建或推进
    线索、不改变评分、Lead 红色预警或人工复核，原始证据链始终保持不变。
    """
    try:
        if run_scope != SCHEDULED_PUBLIC_DISCOVERY_SCOPE:
            raise ValueError("run_scope必须是平台登记的定时公开发现范围")
        result = run_scheduled_public_discovery(_workspace_dir())
        try:
            notification = await _append_scheduled_monitor_manual_review_alert(result)
        except (OSError, RuntimeError, ValueError) as exc:
            # 提醒写入失败不能掩盖已完成的监控结果；任务审计仍可供人工检查。
            logger.warning("徐州招商监控本机提醒写入失败：%s", exc)
            notification = {
                "status": "failed",
                "operation_scope": LOCAL_INBOX_OPERATION_SCOPE,
            }
        return {"ok": True, **result, "notification": notification}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def run_xuzhou_outcome_learning_cycle(run_scope: str) -> dict[str, Any]:
    """执行一次仅供 Cron 固定参数调用的结果反馈自动学习周期。

    输入必须是服务器注册的 ``scheduled_outcome_learning_cycle`` 标记；输出为样本不足、无
    调整建议或新规则版本结果。函数不接受权重、维度、规则版本、用户 ID、线索或证据字段，
    因而模型和前端不能借此篡改业务配置；核心周期仅在充分反馈下创建一版等额平衡的规则。
    它不联网、不改原始证据、快照、反馈、线索状态或人工复核，且不会更新 Skill；在
    评分周期成功后，最多额外执行一次受限来源学习：默认只让工作台按已有结果排序；只有
    已登录业务人员明确选择“自动排序加固定配方重排”时，才会重排现有固定配方。它绝不
    新增来源、关键词、Skill 或正式证据。具体 Cron 是否启用仍由部署层明确配置。
    """
    try:
        if run_scope != OUTCOME_LEARNING_CYCLE_SCOPE:
            raise ValueError("run_scope必须是平台登记的定时结果学习范围")
        workspace_dir = _workspace_dir()
        scoring_learning_result = run_outcome_learning_cycle(workspace_dir)
        try:
            source_recipe_learning_result = run_adaptive_learning_cycle(workspace_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            # 自适应排序失败不能篡改或掩盖评分学习的确定性结果；它只影响下一次来源
            # 研究的优先级解释，人工仍可经来源覆盖与策略界面复核或暂停自动化。
            logger.warning("徐州招商自适应来源学习周期失败：%s", exc)
            source_recipe_learning_result = {
                "status": "failed",
                "operation_scope": "scheduled_bounded_adaptive_learning_only",
            }
        return {
            "ok": True,
            **scoring_learning_result,
            # 保留旧字段，避免既有部署脚本或审计看板失效；新页面应读取
            # ``adaptive_learning``，它准确描述这里不是无条件自动改配方。
            "source_recipe_learning": source_recipe_learning_result,
            "adaptive_learning": source_recipe_learning_result,
        }
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_source_coverage_report(window_days: int = 30) -> dict[str, Any]:
    """读取来源覆盖缺口与退避状态，供业务人员调整目录或后续人工补证。

    输入是 1–90 天统计窗口，输出为按产业和固定信号目录汇总的候选、抓取失败、覆盖
    缺口及受限来源。函数是只读操作：不联网、不触发重试、不写入候选或审计，更不会
    修改 evidence 中的来源 URL、摘录、采集时间、评分依据或人工复核状态。
    """
    try:
        result = get_source_coverage_report(_workspace_dir(), window_days=window_days)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def list_xuzhou_candidate_observations(
    review_priority: str | None = None,
    content_changed_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """列出团队共享台账的待补证候选快照队列，不读取网页或改写台账。

    输入可用 ``high_priority_review``、``normal_review`` 及 ``content_changed_only`` 筛选，并受
    1–100 条上限；后者只返回服务器已确认页面字节变化的独立观察。输出只含来源、采集
    时间、哈希、词表命中和优先级，不含本地快照路径或 Base64 原始字节。工具不接收 URL、
    用户 ID 或写入字段，因此普通登录用户和智能体都不能借列表操作重抓网页、篡改快照、
    写入 evidence、推进线索或变更评分；单条正文需使用下方的精确读取工具。
    """
    try:
        result = list_candidate_observations(
            _workspace_dir(),
            review_priority=review_priority,
            content_changed_only=content_changed_only,
            limit=limit,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_candidate_observation(
    observation_id: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """查看一条已校验的候选原始快照正文与待补证信号，始终不修改任何业务记录。

    输入必须且只能给出服务器生成的 observation_id 或 candidate_id；输出包含候选上下文、
    观察词表命中、来源链接、采集时间、SHA-256、字节数和最多 12,000 字可见正文；若页面
    已变化，另含经前序哈希链校验的有限正文差异摘要。摘要不是企业事实，必须人工核验。
    函数从工作区固定快照路径读取并校验原始字节哈希，拒绝被篡改的路径或内容；不接受 URL、
    文件路径、操作者 ID、修改字段或 Base64 输出，且不联网、不重抓、不写 evidence、不
    改变线索、评分、红色预警或人工复核状态。
    """
    try:
        result = get_candidate_observation(
            _workspace_dir(),
            observation_id=observation_id,
            candidate_id=candidate_id,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_investment_assessment_report(lead_id: str) -> dict[str, Any]:
    """只读生成一条线索的标准化研判报告，不生成自动招商结论。

    输入仅为服务器已存在的 lead_id；输出完整证据链、按来源公开日期排列的有限时间线、
    融资/团队/技术/徐州关联/合作扩张/风险章节来源卡片、章节级交叉核验提示、待补证
    完整性矩阵、关联待补证观察及历史评分快照。没有证据的章节会明确写为待补证，多域名
    仅提示需人工核对同一事件，绝不当作事实或意愿判定。工具不接收 URL、用户 ID 或写入
    字段，不联网、不保存报告、不改 evidence、候选、评分、预警或人工复核状态。
    """
    try:
        result = get_investment_assessment_report(_workspace_dir(), lead_id=lead_id)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_investment_work_summary(window_days: int = 30) -> dict[str, Any]:
    """只读汇总招商日常工作记录，供人工汇报而不评价人员或企业事实。

    输入是 1–90 天统计窗口，输出线索、证据、预警、待补证观察、产业、人工状态、来源
    等级和每日活动计数。统计仅反映台账已有记录，不等同于招商成果、企业意愿或人员
    绩效；工具不接收操作者 ID、URL 或修改字段，不联网、不写文件、不改变证据链、评分
    或人工复核状态。
    """
    try:
        result = get_investment_work_summary(_workspace_dir(), window_days=window_days)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_industry_business_metrics_report(window_years: int = 5) -> dict[str, Any]:
    """只读汇总人工确认的经营指标，并强制按三种不同口径并列展示。

    输入仅为 1–10 年的服务器校验展示窗口，输出为已确认营业收入、产值、项目投资额的
    企业级时序与同口径变动，以及待人工复核草稿计数。工具不接收金额、实体、来源 URL、
    证据 ID、用户 ID 或写入字段，不联网、不触发自动提取、不确认草稿；特别不会把三类
    不同统计口径相加，也不会改变 evidence、线索、评分、预警或人工复核状态。
    """
    try:
        result = get_industry_business_metrics_report(_workspace_dir(), window_years=window_years)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_industry_subdirection_coverage_report(
    window_days: int = 30,
) -> dict[str, Any]:
    """只读汇总三条重点产业全部配置细分方向的证据文本与来源覆盖缺口。

    输入仅为 1–90 天统计窗口，输出每个固定细分词的已见证据卡片、待补证状态、关联
    线索和大产业宽查询来源目录覆盖。词匹配只是当前台账文本信号，不是自动产业分类、
    企业研判或招商优先级；工具不接收细分词、URL、用户 ID 或写入字段，不联网、不搜索、
    不触发采集、不自动打标签、不改变证据链、评分、预警、关系或人工复核状态。
    """
    try:
        result = get_industry_subdirection_coverage_report(
            _workspace_dir(),
            window_days=window_days,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def get_xuzhou_relationship_graph(
    lead_id: str | None = None,
    review_state: str | None = None,
) -> dict[str, Any]:
    """只读查看招商证据化关系图谱及复核状态汇总，不登记、推断或改变任何业务记录。

    输入可选为已存在的 lead_id 和关系边复核状态；无输入返回团队共享图谱，有输入
    返回该线索的一跳子图，状态筛选同时返回该范围内的状态计数。输出还会按已经人工
    登记的受控关系类型标示上下游展示方向、证据引用覆盖度及待补证原因；这些提示不
    等同于真实交易强度、合作深度或招商意愿。函数不接收 URL、证据正文、操作者 ID 或
    写入字段，不联网、不创建关系、不自动推进线索，也不改写原始证据、评分、预警和
    人工复核状态。
    """
    try:
        result = get_relationship_graph(
            _workspace_dir(),
            lead_id=lead_id,
            review_state=review_state,
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def list_xuzhou_relationship_review_queue(
    limit: int = 50,
) -> dict[str, Any]:
    """只读列出团队共享关系图谱的人工待办，并公开固定排序规则。

    输入仅为 1–200 的展示上限，输出等待用户复核或待补证边的固定优先级、关联线索
    摘要、关系方向、证据 ID 和人工原因。优先级只是建议查看顺序，不等于企业意愿、
    交易强度或自动招商结论；工具不接收用户 ID、URL、证据正文或写入字段，不联网、
    不自动复核、标红、推进、调整评分或修改原始证据链。读取仅限当前服务器工作区，
    普通登录用户的实际业务复核仍须经已认证 HTTP 接口完成。
    """
    try:
        result = get_relationship_review_queue(_workspace_dir(), limit=limit)
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def list_xuzhou_relationship_drafts(
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """只读查看已签名证据抽取的关键人员关系草稿，不登记节点、边或个人招商结论。

    输入是可选既有招商线索 ID、派生复核状态和 1–200 的展示上限；输出为法人、创始人、
    实控人、股东、董事和高管的明确公开角色草稿，以及其仅追加的人工判断。工具只读取
    团队共享台账，不接收人员联系方式、资产、URL、证据正文或用户 ID，不联网、不自动建图、
    不改变原始 URL、摘录、采集时间、评分、线索状态或红色预警。实际业务复核仍须经已认证
    HTTP 接口，且“可登记图谱”只表示可由人工下一步调用图谱接口再次核验。
    """

    try:
        result = list_relationship_drafts(
            _workspace_dir(), lead_id=lead_id, review_state=review_state, limit=limit
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


async def list_xuzhou_co_investment_signal_drafts(
    lead_id: str | None = None,
    review_state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """只读查看一手公开证据抽取的组织级协同投资信号草稿，不处理个人资金或发起联络。

    输入是可选既有线索 ID、派生复核状态和 1–200 的展示上限；输出为联合投资、共同设立
    基金或寻找出资方等组织级公开文本信号及追加式人工判断。工具只读取团队共享台账，不接收
    自然人资产、金额、联系方式、URL、证据正文或用户 ID；不联网、不自动联系、募资、撮合
    或推进线索，也不改写来源 URL、摘录、采集时间、评分、线索状态或原始证据链。实际复核
    必须经认证 HTTP 接口，且“可核验对接”只表示人工可继续核验公开主体和授权联系人。
    """

    try:
        result = list_co_investment_signal_drafts(
            _workspace_dir(), lead_id=lead_id, review_state=review_state, limit=limit
        )
        return {"ok": True, **result}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
