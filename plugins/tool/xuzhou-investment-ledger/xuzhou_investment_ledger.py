"""徐州招商线索与证据台账插件注册入口。"""

from __future__ import annotations

from qwenpaw.plugins.api import PluginApi

from .adaptive_learning_api import router as adaptive_learning_router
from .automation_schedule_api import router as automation_schedule_router
from .co_investment_signal_api import router as co_investment_signal_router
from .dashboard_api import router as dashboard_router
from .dingtalk_daily_digest import (
    DAILY_DIGEST_SCOPE,
    deliver_xuzhou_dingtalk_daily_digest,
    record_xuzhou_dingtalk_daily_digest_delivery,
)
from .dingtalk_daily_digest_api import router as dingtalk_daily_digest_router
from .evidence_draft_api import router as evidence_draft_router
from .human_evidence_api import router as human_evidence_router
from .industry_metrics_api import router as industry_metrics_router
from .integrity_migration_api import router as integrity_migration_router
from .outcome_learning_schedule_api import router as outcome_learning_schedule_router
from .review_api import router as review_router
from .relationship_graph_api import router as relationship_graph_router
from .relationship_draft_api import router as relationship_draft_router
from .scoring_rules_api import router as scoring_rules_router
from .scoring_rules import OUTCOME_LEARNING_CYCLE_SCOPE
from .source_catalog_api import router as source_catalog_router
from .public_source_supplement_api import router as public_source_supplement_router
from .source_recipe_learning_api import router as source_recipe_learning_router
from .source_monitoring import SCHEDULED_PUBLIC_DISCOVERY_SCOPE
from .team_api import router as team_router
from .tools import (
    capture_xuzhou_discovery_candidate,
    discover_xuzhou_public_sources,
    get_xuzhou_investment_action_center,
    get_xuzhou_investment_assessment_report,
    get_xuzhou_industry_subdirection_coverage_report,
    get_xuzhou_industry_business_metrics_report,
    get_xuzhou_investment_work_summary,
    get_xuzhou_relationship_graph,
    get_xuzhou_outreach_preparation_brief,
    list_xuzhou_relationship_review_queue,
    list_xuzhou_relationship_drafts,
    list_xuzhou_co_investment_signal_drafts,
    get_xuzhou_investment_lead,
    get_xuzhou_candidate_observation,
    get_xuzhou_source_coverage_report,
    inspect_xuzhou_discovery_candidate,
    list_xuzhou_investment_leads,
    list_xuzhou_candidate_observations,
    run_xuzhou_outcome_learning_cycle,
    run_xuzhou_source_monitor,
)


def _register_governance_tools() -> None:
    """登记工具与治理策略名称，使平台能统一控制其调用权限。"""
    from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY

    tool_metadata = {
        "list_xuzhou_investment_leads": "ListXuzhouInvestmentLeads",
        "get_xuzhou_investment_lead": "GetXuzhouInvestmentLead",
        "get_xuzhou_investment_action_center": "GetXuzhouInvestmentActionCenter",
        "get_xuzhou_outreach_preparation_brief": "GetXuzhouOutreachPreparationBrief",
        "get_xuzhou_investment_assessment_report": "GetXuzhouInvestmentAssessmentReport",
        "get_xuzhou_industry_subdirection_coverage_report": "GetXuzhouIndustrySubdirectionCoverageReport",
        "get_xuzhou_industry_business_metrics_report": "GetXuzhouIndustryBusinessMetricsReport",
        "get_xuzhou_investment_work_summary": "GetXuzhouInvestmentWorkSummary",
        "get_xuzhou_relationship_graph": "GetXuzhouRelationshipGraph",
        "list_xuzhou_relationship_review_queue": "ListXuzhouRelationshipReviewQueue",
        "list_xuzhou_relationship_drafts": "ListXuzhouRelationshipDrafts",
        "list_xuzhou_co_investment_signal_drafts": "ListXuzhouCoInvestmentSignalDrafts",
        "list_xuzhou_candidate_observations": "ListXuzhouCandidateObservations",
        "get_xuzhou_candidate_observation": "GetXuzhouCandidateObservation",
        "discover_xuzhou_public_sources": "DiscoverXuzhouPublicSources",
        "capture_xuzhou_discovery_candidate": "CaptureXuzhouDiscoveryCandidate",
        "inspect_xuzhou_discovery_candidate": "InspectXuzhouDiscoveryCandidate",
        "run_xuzhou_source_monitor": "RunXuzhouSourceMonitor",
        "run_xuzhou_outcome_learning_cycle": "RunXuzhouOutcomeLearningCycle",
        "deliver_xuzhou_dingtalk_daily_digest": "DeliverXuzhouDingTalkDailyDigest",
        "get_xuzhou_source_coverage_report": "GetXuzhouSourceCoverageReport",
    }
    for python_name, policy_name in tool_metadata.items():
        DEFAULT_REGISTRY.register(policy_name, "local", "")
        DEFAULT_REGISTRY.register_python_name(python_name, policy_name)


class XuzhouInvestmentLedgerPlugin:
    """注册招商证据工具及已认证的人工复核接口。"""

    def register(self, api: PluginApi) -> None:
        """注册台账、自动发现工具及仅限登录用户的业务 HTTP 路由。

        复核仍不注册为模型工具，避免模型或前端伪造审核人；HTTP 路由会从认证中间件
        取得当前 ``Principal``，允许普通登录用户进行业务复核，同时保留平台的工作区
        归属校验。自动发现工具只执行小批量免费公开搜索和候选抓取，不能接收任意
        URL 或直接生成事实证据。定时监控仅允许平台 Cron 用固定范围参数触发，执行
        后也只写入候选和任务审计。该注册只暴露业务接口，不改变认证、密钥或跨用户
        数据权限。
        """
        _register_governance_tools()
        api.register_http_router(
            review_router,
            prefix="/xuzhou-investment-ledger",
            tags=["xuzhou-investment-ledger"],
        )
        # 工作台读取接口独立挂载，只返回服务器现有的脱敏业务副本。所有写入仍走各自的
        # 认证接口，避免浏览器界面变成可以传入状态、证据、评分或操作者字段的通用入口。
        api.register_http_router(
            dashboard_router,
            prefix="/xuzhou-investment-ledger/dashboard",
            tags=["xuzhou-investment-ledger"],
        )
        # 草稿确认仅经认证 HTTP 路由进入：请求不接收实体、日期、摘录、URL 或操作者 ID，
        # 服务器必须重新抓取核验后才可新增正式证据，故不能作为模型写入工具注册。
        api.register_http_router(
            evidence_draft_router,
            prefix="/xuzhou-investment-ledger/evidence-drafts",
            tags=["xuzhou-investment-ledger"],
        )
        # 人工授权材料与服务器抓取证据分开：团队成员可提交自己有权使用的网页摘录、
        # 导出文本或小型附件，服务器会签名留痕但明确标记为人工材料，绝不伪装成抓取。
        api.register_http_router(
            human_evidence_router,
            prefix="/xuzhou-investment-ledger/human-evidence",
            tags=["xuzhou-investment-ledger"],
        )
        # 经营指标复核同样只允许认证 HTTP 路由：请求不接受金额、年份、实体、URL、证据
        # 或操作者 ID。普通登录用户只能对服务器从已签名证据生成的草稿追加业务判断，
        # 不能借指标确认覆盖原始证据链、线索状态或评分。
        api.register_http_router(
            industry_metrics_router,
            prefix="/xuzhou-investment-ledger/industry-metrics",
            tags=["xuzhou-investment-ledger"],
        )
        # 评分规则版本由普通登录用户维护；路由与线索复核分开挂载，避免路径冲突。
        # 规则接口只保存可追溯的业务配置，不暴露任何改写原始证据链的能力。
        api.register_http_router(
            scoring_rules_router,
            prefix="/xuzhou-investment-ledger/scoring-rules",
            tags=["xuzhou-investment-ledger"],
        )
        # 可信来源目录是普通用户可审计的业务配置。它独立于模型工具与原始证据，
        # 路由只允许认证主体在其拥有的工作区维护未来来源分级规则。
        api.register_http_router(
            source_catalog_router,
            prefix="/xuzhou-investment-ledger/trusted-sources",
            tags=["xuzhou-investment-ledger"],
        )
        # 业务人员可补充一个无需账号或密钥的公开网页入口，但专用接口会在登记与重新
        # 启用时依次验证 URL 公网边界、robots 与受限页面读取。它不是通用爬虫、不能
        # 接收 Cookie/API Key/请求头/抓取深度，且只会在既有低频发现任务中产生待核验候选。
        api.register_http_router(
            public_source_supplement_router,
            prefix="/xuzhou-investment-ledger/public-sources",
            tags=["xuzhou-investment-ledger"],
        )
        # 来源学习建议与来源目录分开挂载：定时任务只能追加建议，认证普通用户只能对既有
        # proposal 批准或拒绝。请求没有关键词、URL、规则版本或身份字段，批准也只能重排
        # 完整固定配方，不能借“自学习”缩小产业范围、改写 Skill 或触碰证据链。
        api.register_http_router(
            source_recipe_learning_router,
            prefix="/xuzhou-investment-ledger/source-recipe-learning",
            tags=["xuzhou-investment-ledger"],
        )
        # 自适应学习策略由团队成员明确选择。它最多自动排序工作队列与固定五来源配方，
        # 不能新增域名、关键词、产业范围、Skill、证据或外部联系。
        api.register_http_router(
            adaptive_learning_router,
            prefix="/xuzhou-investment-ledger/adaptive-learning",
            tags=["xuzhou-investment-ledger"],
        )
        # 后台监控计划只经认证 HTTP 路由管理：用户无法提交 Cron、URL、搜索词、工具参数、
        # 会话或身份，服务端只允许启停固定的免费公开来源监控，并追加受 HMAC 保护的审计。
        # 计划运行仍只产出候选/待补证信息，不能自动确认事实或推进招商线索。
        api.register_http_router(
            automation_schedule_router,
            prefix="/xuzhou-investment-ledger/source-monitor-schedule",
            tags=["xuzhou-investment-ledger"],
        )
        # 钉钉日报不接受收件人、会话或正文参数：收件人只能由部署管理员预先绑定为唯一
        # 会话，HTTP 仅允许平台管理员启停或验收固定的 09:00/09:05 任务。
        api.register_http_router(
            dingtalk_daily_digest_router,
            prefix="/xuzhou-investment-ledger/dingtalk-daily-digest",
            tags=["xuzhou-investment-ledger"],
        )
        # 结果学习计划也只经认证路由启停。它的每日频率、固定工具范围和系统学习边界均由
        # 服务器常量限定；用户不能提交权重、反馈、规则、模型提示词或操作者 ID，且学习
        # 只在充分人工最终反馈后创建可回溯的新规则版本，绝不改写原始证据或推进线索。
        api.register_http_router(
            outcome_learning_schedule_router,
            prefix="/xuzhou-investment-ledger/outcome-learning-schedule",
            tags=["xuzhou-investment-ledger"],
        )
        # 图谱节点和边由普通登录用户经认证接口登记；边必须引用既有证据。它不注册为
        # 写入模型工具，避免模型伪造操作者、自动推断关系或改写证据链。
        api.register_http_router(
            relationship_graph_router,
            prefix="/xuzhou-investment-ledger/relationship-graph",
            tags=["xuzhou-investment-ledger"],
        )
        # 公开关键人员关系草稿由固定规则从已签名 evidence 生成，只能在认证普通用户复核后
        # 作为人工登记图谱的参考。请求不允许提交人员资料、关系事实、证据或操作者 ID，
        # 且不会自动创建节点/边，因此不能借草稿接口改写证据链或扩大个人信息处理范围。
        api.register_http_router(
            relationship_draft_router,
            prefix="/xuzhou-investment-ledger/relationship-drafts",
            tags=["xuzhou-investment-ledger"],
        )
        # 协同投资信号只经认证 HTTP 路由复核：请求不接受个人资产、金额、联系方式、
        # 资金承诺、证据字段或操作者 ID。普通用户仅能对服务器从已签名一手证据提取的
        # 组织级信号追加判断，不能建立资金池、自动联系或改写任何原始证据链。
        api.register_http_router(
            co_investment_signal_router,
            prefix="/xuzhou-investment-ledger/co-investment-signals",
            tags=["xuzhou-investment-ledger"],
        )
        # 历史证据重封属于数据安全操作，仅管理员 API 可调用；它不注册为模型工具，
        # 不允许普通用户以业务复核名义触发，也不接收任何可改写原始证据的请求字段。
        api.register_http_router(
            integrity_migration_router,
            # 与普通线索复核根路由分开挂载，避免插件热加载时同一 FastAPI 前缀被重复
            # 注册。最终接口仍为 /xuzhou-investment-ledger/evidence-integrity/migrate-legacy；
            # 迁移仍只接受管理员认证身份、服务器工作区和历史台账 SHA-256。
            prefix="/xuzhou-investment-ledger/evidence-integrity",
            tags=["xuzhou-investment-ledger"],
        )
        # 团队名册管理仅管理员可用：请求体只有平台用户名，user_id 只能由服务端平台
        # 用户目录解析；名册决定普通成员能否访问团队共享台账，本身不包含也不改写任何
        # 线索、证据或评分类业务数据。
        api.register_http_router(
            team_router,
            prefix="/xuzhou-investment-ledger/team",
            tags=["xuzhou-investment-ledger"],
        )
        api.register_tool(
            tool_name="list_xuzhou_investment_leads",
            tool_func=list_xuzhou_investment_leads,
            description="查看团队共享台账中的徐州招商线索，可按产业和人工复核状态筛选。",
            icon="list",
        )
        api.register_tool(
            tool_name="get_xuzhou_investment_lead",
            tool_func=get_xuzhou_investment_lead,
            description="查看单条招商线索、状态历史及其完整公开证据链，用于人工核验。",
            icon="search",
        )
        api.register_tool(
            tool_name="get_xuzhou_investment_action_center",
            tool_func=get_xuzhou_investment_action_center,
            description=(
                "只读汇总红色预警、关系待办、来源对齐和时效复核；仅供人工轮询，不发送"
                "外部推送、不自动复核或推进线索。"
            ),
            icon="bell-ring",
        )
        api.register_tool(
            tool_name="get_xuzhou_outreach_preparation_brief",
            tool_func=get_xuzhou_outreach_preparation_brief,
            description=(
                "只读生成单线索的证据化招商准备卡；不联系企业、不承诺政策、不改变业务"
                "状态，后续由人工核验与授权。"
            ),
            icon="clipboard-check",
        )
        api.register_tool(
            tool_name="get_xuzhou_investment_assessment_report",
            tool_func=get_xuzhou_investment_assessment_report,
            description=(
                "只读生成单条招商线索的标准研判报告：完整证据链、待补证维度、跨来源覆盖"
                "提示与历史评分；不作自动事实、意愿或推进结论。"
            ),
            icon="file-text",
        )
        api.register_tool(
            tool_name="get_xuzhou_industry_subdirection_coverage_report",
            tool_func=get_xuzhou_industry_subdirection_coverage_report,
            description=(
                "只读汇总三条重点产业全部配置细分方向的证据文本与来源目录覆盖缺口；"
                "不自动分类、搜索、评分或推进线索。"
            ),
            icon="chart-bar",
        )
        api.register_tool(
            tool_name="get_xuzhou_investment_work_summary",
            tool_func=get_xuzhou_investment_work_summary,
            description=(
                "只读汇总招商线索、证据、预警、待补证观察和每日工作活动，供人工汇报；"
                "不评价人员绩效、不改写任何业务数据。"
            ),
            icon="chart-bar",
        )
        api.register_tool(
            tool_name="get_xuzhou_industry_business_metrics_report",
            tool_func=get_xuzhou_industry_business_metrics_report,
            description=(
                "只读查看人工确认的营业收入、产值和项目投资额时序；三种口径仅并列展示，"
                "不相加、不自动确认指标或推进线索。"
            ),
            icon="chart-bar",
        )
        api.register_tool(
            tool_name="get_xuzhou_relationship_graph",
            tool_func=get_xuzhou_relationship_graph,
            description=(
                "只读查看已由公开证据支撑的企业、基金、高校、园区等关系图谱；可按招商"
                "线索查看一跳子图，不自动推断关系或推进线索。"
            ),
            icon="share-2",
        )
        api.register_tool(
            tool_name="list_xuzhou_relationship_review_queue",
            tool_func=list_xuzhou_relationship_review_queue,
            description=(
                "只读列出待人工复核或待补证的关系图谱边，并说明固定排序规则与证据引用；"
                "不自动复核、标红或推进任何线索。"
            ),
            icon="list-check",
        )
        api.register_tool(
            tool_name="list_xuzhou_relationship_drafts",
            tool_func=list_xuzhou_relationship_drafts,
            description=(
                "只读列出已签名公开证据抽取的法人、创始人、实控人、股东、董事和高管关系"
                "草稿及人工判断；不自动建图、推进线索或对个人作出招商结论。"
            ),
            icon="users-round",
        )
        api.register_tool(
            tool_name="list_xuzhou_co_investment_signal_drafts",
            tool_func=list_xuzhou_co_investment_signal_drafts,
            description=(
                "只读列出一手公开证据中的组织级联合投资、基金设立或寻找出资方信号及人工"
                "判断；不收集个人资产、不募资、不联系主体或自动推进招商线索。"
            ),
            icon="landmark",
        )
        api.register_tool(
            tool_name="list_xuzhou_candidate_observations",
            tool_func=list_xuzhou_candidate_observations,
            description=(
                "列出待补证候选原始快照的来源、哈希、词表命中和人工优先级；只读，不返回"
                "本地快照路径或原始字节，不触发网页访问或改变线索。"
            ),
            icon="list-search",
        )
        api.register_tool(
            tool_name="get_xuzhou_candidate_observation",
            tool_func=get_xuzhou_candidate_observation,
            description=(
                "按服务器生成的候选或观察 ID 校验并查看候选原始快照的可见正文与待补证"
                "信号；只读，不重抓页面、不写证据或推进线索。"
            ),
            icon="file-search",
        )
        api.register_tool(
            tool_name="discover_xuzhou_public_sources",
            tool_func=discover_xuzhou_public_sources,
            description=(
                "按产业执行小批量免费公开搜索，发现融资、合作、扩张、徐州关联和校友等"
                "待核验来源链接；搜索结果不会直接成为招商事实或评分。"
            ),
            icon="compass",
        )
        api.register_tool(
            tool_name="capture_xuzhou_discovery_candidate",
            tool_func=capture_xuzhou_discovery_candidate,
            description=(
                "抓取已自动发现的候选页面；仅在实体、原始摘录和发布日期可被页面正文"
                "核验时追加证据，并保留来源快照哈希；只有可信目录匹配才能采用其来源等级。"
            ),
            icon="download",
        )
        api.register_tool(
            tool_name="inspect_xuzhou_discovery_candidate",
            tool_func=inspect_xuzhou_discovery_candidate,
            description=(
                "读取自动发现候选的受限页面快照，供智能体从原文提取实体、发布日期和"
                "原始摘录；查看本身不会写入证据或改变线索状态。"
            ),
            icon="file-search",
        )
        # Cron 只能传入这一个非空固定标记；工具自身还会再次校验，不能被借作任意搜索
        # 或采集入口。默认不启用，须由用户在目标智能体中明确启用后才可创建定时任务。
        api.register_tool(
            tool_name="run_xuzhou_source_monitor",
            tool_func=run_xuzhou_source_monitor,
            description=(
                "按固定产业和查询目录运行一次后台公开候选发现，并记录来源访问退避与"
                "任务审计；robots 允许时最多保存三个不同域名的候选原始快照并标记待补证，"
                "但不写证据、不创建或自动推进线索。"
            ),
            icon="timer",
            cron_safe=True,
            cron_fixed_arguments={"run_scope": SCHEDULED_PUBLIC_DISCOVERY_SCOPE},
        )
        # 结果学习与来源监控分开调度：它没有网络输入，只能用服务器固定范围触发，避免
        # 模型借自动学习接口传入权重、身份或任意台账对象。默认同样不启用 Cron。
        api.register_tool(
            tool_name="run_xuzhou_outcome_learning_cycle",
            tool_func=run_xuzhou_outcome_learning_cycle,
            description=(
                "仅供固定 Cron 参数调用：基于当前规则版本的充分最终反馈，至多创建一版"
                "单维度等额平衡的新评分规则；不改原始证据、线索状态、反馈或人工复核。"
            ),
            icon="brain-circuit",
            cron_safe=True,
            cron_fixed_arguments={"run_scope": OUTCOME_LEARNING_CYCLE_SCOPE},
        )
        # 这是 Cron-only 工具，不出现在普通对话工具配置中。它只返回经用户批准的数量和
        # 最多五个来源标题；Cron 执行器只会把这个明确字段原样投递给部署时锁定的钉钉目标。
        api.register_tool(
            tool_name="deliver_xuzhou_dingtalk_daily_digest",
            tool_func=deliver_xuzhou_dingtalk_daily_digest,
            description="仅供固定钉钉日报Cron调用，不接受普通对话或自定义消息参数。",
            icon="send",
            cron_safe=True,
            cron_fixed_arguments={"run_scope": DAILY_DIGEST_SCOPE},
            cron_only=True,
            cron_deliver_result=True,
            cron_delivery_result_handler=record_xuzhou_dingtalk_daily_digest_delivery,
        )
        api.register_tool(
            tool_name="get_xuzhou_source_coverage_report",
            tool_func=get_xuzhou_source_coverage_report,
            description=(
                "查看按产业、信号目录汇总的来源覆盖缺口及访问退避状态；该报告只读，"
                "不触发网页访问或修改证据链。"
            ),
            icon="chart-bar",
        )


plugin = XuzhouInvestmentLedgerPlugin()
