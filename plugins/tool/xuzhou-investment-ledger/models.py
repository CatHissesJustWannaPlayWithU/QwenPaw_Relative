"""徐州招商线索与证据台账的数据结构定义。

本模块定义公开线索、证据、受限自动发现、可信来源目录和后续评分所需的数据边界。
自动发现候选、来源目录和原始证据分别保存：候选不是事实，目录不是网页证据，评分
或人工业务判断也不能覆盖来源 URL、摘录、采集时间和内容哈希。所有记录均以当前
智能体工作区为边界，避免不同智能体混用线索。
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class EvidenceRecord(TypedDict):
    """一条可回溯的公开证据。

    source_url、source_title、source_published_at 和 excerpt 共同构成最小证据链。
    source_level 用于标识来源等级，evidence_quality 用于记录人工录入时的证据质量判断。
    """

    evidence_id: str
    lead_id: str
    source_url: str
    source_title: str
    source_published_at: str
    source_level: str
    # ``claim_type`` 是服务器从标题和原始摘录得到的归档分类，不是对企业现实的自动结论。
    # 原始调用方请求、字面支持状态和命中词同时保留，方便人工识别分类是否有固定文本依据。
    claim_type: str
    claim_type_requested: NotRequired[str]
    claim_type_matched_terms: NotRequired[list[str]]
    claim_type_text_support: NotRequired[str]
    claim_type_classification_method: NotRequired[str]
    evidence_quality: int
    excerpt: str
    xuzhou_association_tags: list[str]
    created_at: str
    source_type: NotRequired[str]
    collection_method: NotRequired[str]
    collected_at: NotRequired[str]
    raw_sha256: NotRequired[str]
    discovery_run_id: NotRequired[str]
    discovery_candidate_id: NotRequired[str]
    trusted_source_id: NotRequired[str]
    trusted_source_type: NotRequired[str]
    # 由服务器私有密钥生成的链式 HMAC；调用方不能提交、复核或覆盖它。
    integrity_signature: NotRequired[str]


class DiscoveryRunRecord(TypedDict):
    """一次低频公开来源发现任务的审计记录。

    每次任务保存服务器生成的查询语句、使用的免费公开搜索目录、时间及成功或失败
    数量，不把搜索摘要直接当作事实证据。该记录允许后续定位某条候选链接来自哪次
    自动发现，同时与不可变的 ``evidence`` 列表分开，避免重跑任务覆盖原始证据。
    """

    run_id: str
    sector: str
    started_at: str
    completed_at: str
    query_count: int
    candidate_count: int
    failed_query_count: int
    # 搜索源虽然返回内容、但标题与摘要完全未命中本产业总称或细分词的次数。它用于来源
    # 健康治理，避免把固定泛搜索结果误报为正常发现；不是任何企业的风险或事实标签。
    non_topical_response_count: NotRequired[int]
    skipped_due_to_cooldown_count: int
    # 搜索摘要未满足中国/徐州、组织主体与机会信号门槛时，仅在批次审计中保留排除原因；
    # 它不是企业事实、风险结论或负面名单。
    rejected_result_count: NotRequired[int]
    rejected_results: NotRequired[list[dict[str, object]]]
    operation_scope: str


class DiscoveryCandidateRecord(TypedDict):
    """由公开搜索发现、但尚未被当作事实证据的候选页面。

    ``source_url`` 来自服务器执行的受控搜索，而不是调用方传入的任意 URL；候选仅是
    待抓取线索，默认 ``lead_only``。抓取成功后只保存对证据的引用，不能反向覆盖该
    证据中的链接、摘录、来源日期、抓取时间或内容哈希。
    """

    candidate_id: str
    run_id: str
    source_url: str
    source_title: str
    search_snippet: str
    sector: str
    source_catalog_id: str
    discovery_query: str
    discovered_at: str
    source_type: str
    source_level: str
    state: str
    capture_attempts: int
    captured_evidence_id: str | None
    last_capture_error: str | None
    last_captured_at: str | None
    # 自动观察只补充候选页的原始快照索引和访问结果，不能改变候选的 capture 状态，
    # 更不能把关键词命中升级为 evidence、评分或人工招商结论。
    observation_attempts: NotRequired[int]
    last_observation_id: NotRequired[str | None]
    last_observation_error: NotRequired[str | None]
    last_observed_at: NotRequired[str | None]
    # 服务器固定范围门槛的可复查结果，仅表示是否值得自动观察；不确认中国业务、徐州
    # 关联或投资意愿，页面原文和人工复核仍不可省略。
    eligibility_assessment: NotRequired[dict[str, object]]
    # 仅固定中国财经媒体的标题级候选可能为 True。它提示页面快照后仍必须补足中国/徐州
    # 业务依据，且禁止由候选直接生成正式证据；不代表企业属于中国或有招商意愿。
    requires_china_business_verification: NotRequired[bool]


class CandidateObservationRecord(TypedDict):
    """一条待人工补证的候选页面原始观察记录。

    观察记录保存服务器采集时间、原始内容哈希和只追加快照的位置；``signals`` 只表示
    固定词表在该次页面文本中的命中，不是企业融资、扩产、合作或落地的事实。人工后续
    必须另行核验实体、日期和原始摘录，才能创建不可变 ``EvidenceRecord``。
    """

    observation_id: str
    candidate_id: str
    source_url: str
    source_title: str
    content_type: str
    collected_at: str
    raw_sha256: str
    byte_size: int
    # 实际 JSON 快照文件占用（含 Base64 原文），仅用于容量审计；不影响原始页面字节数。
    snapshot_storage_bytes: NotRequired[int]
    snapshot_relative_path: str
    signals: list[dict[str, object]]
    review_priority: str
    state: str
    operation_scope: str
    # 首次观察没有前序记录；页面哈希变化后新增的独立观察会引用上一次快照，绝不覆盖它。
    observation_kind: NotRequired[str]
    previous_observation_id: NotRequired[str | None]
    previous_raw_sha256: NotRequired[str | None]


class CandidateCorroborationAttemptRecord(TypedDict):
    """从候选原始页面的明确官网链接得到的第二来源补证尝试审计。

    该记录只说明服务器在已验证候选快照中发现了一个标为官网的跨域公开链接，并记录
    robots/抓取结果；``official_site_link_candidate`` 不是对网址归属、企业中国业务、徐州
    关联、融资真实性或招商意愿的确认。原候选、原始快照和正式 evidence 全部保持独立，
    仍必须由人工使用多个可回溯来源进行核验。
    """

    corroboration_id: str
    candidate_id: str
    parent_observation_id: str
    parent_raw_sha256: str
    attempted_at: str
    outcome: str
    selector_reason: str
    source_url: NotRequired[str | None]
    source_hostname: NotRequired[str | None]
    source_title: NotRequired[str | None]
    raw_sha256: NotRequired[str | None]
    byte_size: NotRequired[int | None]
    snapshot_storage_bytes: NotRequired[int | None]
    snapshot_relative_path: NotRequired[str | None]
    error: NotRequired[str | None]
    operation_scope: str


class CandidateScopeExclusionRecord(TypedDict):
    """自动候选范围排除的只追加审计记录。

    记录只使不满足中国/徐州业务机会门槛的候选退出自动观察和默认工作队列，不删除候选、
    原始快照或正式 evidence。它不是对主体所在地、投资意愿或风险的事实判断，后续若有
    可追溯的一手材料，仍可通过受控证据流程重新建立新的合格候选或线索。
    """

    exclusion_id: str
    target_kind: str
    candidate_id: str
    observation_id: str | None
    excluded_at: str
    rule_version: str
    exclusion_reason: str
    operation_scope: str


class CandidateBusinessReviewRecord(TypedDict):
    """由认证用户对发现候选追加的业务判断审计。

    该记录不改写 ``DiscoveryCandidateRecord``，更不改写候选快照或正式证据；它只让
    后续工作流知道人工决定暂缓补证、持续观察或停止自动处理。操作者身份只能由服务端
    登录态传入，客户端和模型不能自行声明用户 ID。
    """

    review_id: str
    candidate_id: str
    old_review_state: str
    new_review_state: str
    review_note: str
    reviewed_at: str
    reviewed_by_user_id: str
    operation_scope: str
    # Only present for ``new_review_state=waive_xuzhou``.  This is a human
    # scope decision with an enterprise-origin annotation, not page evidence;
    # server recapture must still validate the original URL and evidence fields.
    geographic_scope_review: NotRequired[dict[str, object]]


class EvidenceDraftRecord(TypedDict):
    """由候选原始快照确定性抽取、等待人工确认的证据草稿。

    草稿保留它来自哪次观察、哪份原始字节哈希及固定词表命中，但不是 ``EvidenceRecord``：
    ``entity_name``、日期、事实类别和摘录都可能为空或有误，不能据此创建线索、评分、预警
    或修改业务状态。后续只能由受控正式抓取再次核验后另行追加证据，不能用草稿覆盖原始
    快照、候选观察或既有证据字段。
    """

    draft_id: str
    observation_id: str
    candidate_id: str
    source_url: str
    source_title: str
    collected_at: str
    raw_sha256: str
    entity_name: str | None
    entity_kind: str | None
    source_published_at: str | None
    source_published_at_raw: str | None
    claim_type: str | None
    excerpt: str | None
    matched_terms: list[str]
    needs_human_confirmation: list[str]
    extractor_version: str
    created_at: str
    state: str
    operation_scope: str


class EvidenceDraftConfirmationRecord(TypedDict):
    """普通登录用户确认草稿的一次追加式审计。

    该记录只保留认证操作者、备注和服务器受控抓取的结果，不接受也不复制客户端给出的
    实体、URL、日期或摘录。草稿与原始观察快照保持原样；成功时正式证据仍由既有抓取器
    新建并签名，失败时也不能把草稿或推测写成事实。
    """

    confirmation_id: str
    draft_id: str
    candidate_id: str
    attempted_at: str
    confirmed_by_user_id: str
    review_note: str
    old_state: str
    new_state: str
    capture_attempted: bool
    capture_result: str
    evidence_id: str | None
    operation_scope: str
    verification_failure_reason: NotRequired[str]
    verification_failure_stage: NotRequired[str]


class EvidenceDraftBusinessReviewRecord(TypedDict):
    """普通登录用户对信息不完整草稿追加的业务判断审计。

    判断只允许标注待补证、观察或无效，并保留认证操作者、备注和缺失字段列表。它不是
    对页面事实的补录，不能携带实体、URL、日期、摘录或评分，也不能覆盖草稿、候选快照
    或正式证据；后续若有可验证页面，仍须重新抓取并走独立确认流程。
    """

    review_id: str
    draft_id: str
    candidate_id: str
    reviewed_at: str
    reviewed_by_user_id: str
    old_state: str
    new_state: str
    review_note: str
    missing_evidence_fields: list[str]
    operation_scope: str


class IndustryMetricDraftRecord(TypedDict):
    """从已签名正式证据摘录提取、等待普通用户复核的一条经营指标草稿。

    草稿保留原始证据 ID、来源 URL、来源日期和命中的完整原句，便于追溯，但并不是新的
    ``EvidenceRecord``，也不改变原证据。``value_yuan`` 仅为固定单位换算的候选值，必须
    经认证用户确认后才能进入经营指标统计；三类 ``metric_kind`` 只能并列，不能相加。
    """

    metric_draft_id: str
    evidence_id: str
    lead_id: str
    entity_name: str
    sector: str
    metric_kind: str
    metric_label: str
    source_metric_label: str
    statistic_scope: str
    period_year: int
    value_yuan: int
    original_value_text: str
    matched_excerpt: str
    source_url: str
    source_published_at: str
    extractor_version: str
    state: str
    created_at: str
    operation_scope: str


class IndustryMetricDraftReviewRecord(TypedDict):
    """普通登录用户对指标草稿一次只追加的确认、待补证或无效审计。

    审计只记录服务器认证身份、旧新状态和备注；它不能携带或覆盖金额、年份、实体、证据
    ID、来源 URL、原始摘录或采集时间。指标草稿原始字段与 ``EvidenceRecord`` 因此保持
    可追溯，复核只能形成业务判断。
    """

    metric_draft_review_id: str
    metric_draft_id: str
    reviewed_at: str
    reviewed_by_user_id: str
    old_state: str
    new_state: str
    review_note: str
    operation_scope: str


class IndustryBusinessMetricRecord(TypedDict):
    """由一条已确认指标草稿派生的只读统计记录。

    记录引用草稿和正式证据，而非复制或修改来源事实；确认人和确认时间用于业务审计。
    ``metric_kind``、``statistic_scope`` 与 ``value_yuan`` 共同限定比较口径，报表不得将
    营业收入、产值和项目投资额跨口径相加，也不能据此自动推进招商线索或调整评分。
    """

    metric_id: str
    metric_draft_id: str
    evidence_id: str
    lead_id: str
    entity_name: str
    sector: str
    metric_kind: str
    metric_label: str
    source_metric_label: str
    statistic_scope: str
    period_year: int
    value_yuan: int
    original_value_text: str
    confirmed_at: str
    confirmed_by_user_id: str
    operation_scope: str


class TrustedSourceRecord(TypedDict):
    """一条可由普通业务用户维护的可信公开来源目录记录。

    目录只声明某个公开域名及其可支持的事实类别，不保存页面正文，也不会追溯改写已
    入库证据。未来自动抓取时，只有域名匹配且 ``allowed_claim_types`` 允许当前事实
    类别，才可使用该记录的一手或二手等级；登记人、时间和变更原因用于业务审计。
    """

    source_id: str
    domain: str
    source_type: str
    source_level: str
    evidence_quality: int
    allowed_claim_types: list[str]
    status: str
    created_at: str
    created_by_user_id: str
    change_reason: str
    # 仅系统固定的免费中国财经媒体种子可带此标记；它只允许“标题缺中国语境”的项目进入
    # 原页快照补证队列，不能证明企业在华业务，也不能绕过正式 evidence 抓取限制。
    system_curated_chinese_business_media: NotRequired[bool]


class TrustedSourceAuditRecord(TypedDict):
    """可信来源目录的追加式业务审计，不承载也不修改原始证据。"""

    audit_id: str
    source_id: str
    action: str
    old_status: str | None
    new_status: str
    reason: str
    operated_at: str
    operated_by_user_id: str
    operation_scope: str


class SourceHealthRecord(TypedDict):
    """一个公开搜索源或候选域名的可再生访问健康记录。

    该记录仅用于决定何时暂停自动重试，不包含网页正文、原始摘录或线索判断。来源键
    由服务器固定搜索源或候选 URL 域名生成；即使后续状态恢复，也不能反向提升既有
    证据的来源等级或改写证据链。
    """

    source_key: str
    status: str
    consecutive_failures: int
    last_success_at: str | None
    last_failure_at: str | None
    last_error_kind: str | None
    last_error: str | None
    next_eligible_at: str | None
    updated_at: str


class MonitoringRunRecord(TypedDict):
    """一次后台固定范围公开候选发现的追加式审计记录。

    任务只枚举已批准的产业与查询目录，``sector_results`` 仅引用发现批次与数量；不
    保存网页全文、不创建事实证据，也不能修改线索状态、评分、红色预警或人工复核。
    """

    monitor_run_id: str
    started_at: str
    completed_at: str
    sectors: list[str]
    status: str
    sector_results: list[dict[str, object]]
    # 固定免费媒体来源种子仅说明可作为二手候选发现入口；该审计字段不表示页面或企业
    # 已被核验，也不会允许任何人通过监控结果改写 evidence 或招商业务状态。
    free_verified_media_seed_result: NotRequired[dict[str, object]]
    source_directory_result: NotRequired[dict[str, object]]
    candidate_observation_result: NotRequired[dict[str, object]]
    # 草稿生成只读取已验证快照；该审计不意味着草稿已被人工确认或已成为正式证据。
    evidence_draft_result: NotRequired[dict[str, object]]
    # 周期、失败计数和运行治理告警只约束后台任务本身，不是企业异常、更不影响招商线索预警。
    consecutive_failure_count: NotRequired[int]
    failure_alert: NotRequired[dict[str, object] | None]
    schedule: NotRequired[dict[str, object]]
    operation_scope: str


class SourceFeedRunRecord(TypedDict):
    """一次可信目录 robots、RSS、Sitemap 与固定产业栏目候选发现的追加式审计。

    记录只保存来源目录 ID、访问策略结果和候选数量，用于观察免费公开来源的覆盖度。
    RSS 标题、摘要、Sitemap URL 与固定投融资栏目卡片都只是待核验候选，不能作为证据或
    覆盖既有来源 URL、原始摘录、采集时间、评分依据和人工业务状态。
    """

    source_feed_run_id: str
    started_at: str
    completed_at: str
    source_count: int
    # 每轮实际被轮换选中的可信来源目录 ID。它只解释调度覆盖面，不能成为页面事实、证据
    # 或招商判断依据，且不会允许调用方借此插入任意访问 URL。
    selected_source_ids: NotRequired[list[str]]
    # 手动可信来源发现可附带一个服务器规范的产业标识，供审计其只返回该产业链接；它不是
    # 调用方自定义搜索词，也不能改变目录、页面正文或任一来源的事实等级。
    requested_sector: NotRequired[str | None]
    # 仅工具源码固定的手动发现会设为 True，表示预检媒体在五个来源上限内优先被尝试；
    # 它不扩大来源域名、不能绕过 robots，且不是企业或媒体内容的可信度判断。
    curated_media_first: NotRequired[bool]
    # 后台产业主发现可由服务器固定为 True，仅访问源码映射的产业栏目；它不接受客户端
    # URL、关键词或域名，不能说明某个候选企业已被核验或具有招商意愿。
    fixed_sector_index_only: NotRequired[bool]
    candidate_count: int
    status: str
    source_results: list[dict[str, object]]
    operation_scope: str


class StatusHistoryRecord(TypedDict, total=False):
    """线索状态变化的不可覆盖审计记录。

    该记录只描述一次业务状态操作，不承载来源 URL、原始摘录或其他证据字段；因此
    后续人工复核和推进只能追加判断，不能借审计记录改写原始证据链。
    """

    state: str
    reason: str
    at: str
    reviewer_user_id: str
    old_state: str
    new_state: str
    review_note: str
    operation_scope: str


class LeadRecord(TypedDict):
    """一个企业或基金的招商线索汇总记录。

    ``red_alert`` 只表示尚未关闭的企业风险预警，绝不表示融资、扩产或合作等招商机会。
    风险等级、类型、处理状态和触发证据 ID 与人工状态历史分开保存：普通用户可以改变
    业务去留，但不能删除或覆盖触发风险的原始证据链。
    """

    lead_id: str
    entity_name: str
    entity_kind: str
    sector: str
    review_state: str
    red_alert: bool
    alert_reason: str | None
    risk_alert_level: str | None
    risk_alert_type: str | None
    risk_alert_status: str | None
    risk_alert_evidence_ids: list[str]
    created_at: str
    updated_at: str
    evidence_ids: list[str]
    status_history: list[StatusHistoryRecord]


class LeadFollowupApprovalRecord(TypedDict):
    """一条“纳入招商跟进池”的只追加人工审批记录。

    它保存的是团队内部的业务判断、负责人和后续动作，不复制或修改任何网页事实。正式
    证据仍只由 ``evidence_ids`` 引用；审批通过也不表示企业已表达落地意愿、已签约或已
    产生招商成果。
    """

    approval_id: str
    lead_id: str
    decision: str
    approved_at: str
    approved_by_user_id: str
    old_review_state: str
    new_review_state: str
    evidence_ids: list[str]
    checklist: dict[str, bool]
    business_value_reason: str
    next_action: str
    next_action_detail: str
    owner_name: str
    target_date: str
    review_note: str
    operation_scope: str


class ScoringDimension(TypedDict):
    """一项可配置的招商研判维度。

    ``key`` 是程序稳定使用的英文标识，``label`` 和 ``description`` 用于向业务人员
    解释规则。``weight`` 仅保存已审批的业务权重；实际范围校验和评分计算将在后续
    模块实现，避免数据定义阶段擅自改变线索优先级。
    """

    key: str
    label: str
    weight: int
    description: str
    enabled: bool


class ScoringPenaltyRule(TypedDict):
    """一项可追溯的降权规则。

    降权规则与正向评分维度分开保存，使直接竞品、过期信号或证据矛盾等风险不会被
    正向总分掩盖；``score_delta`` 应由后续校验逻辑限制为负数。
    """

    key: str
    label: str
    score_delta: int
    description: str
    enabled: bool


class ScoringRuleSet(TypedDict):
    """一版经人工确认后才能生效的招商评分规则。

    规则版本记录创建人、创建时间和变更原因，供后续普通用户调整业务权重以及回滚
    使用。模型或采集到的网页内容不能直接改写此结构；任何学习建议都必须先创建新
    版本并由人工确认。
    """

    rule_set_id: str
    version: int
    status: str
    dimensions: list[ScoringDimension]
    penalty_rules: list[ScoringPenaltyRule]
    created_at: str
    created_by_user_id: str
    change_reason: str


class ScoreComponent(TypedDict):
    """一次评分中单个维度或降权项的可解释结果。

    ``evidence_ids`` 只引用既有不可变证据，``reason`` 只说明该证据如何影响本项分数；
    它们不能保存或覆盖来源 URL、原始摘录和采集时间。
    """

    rule_key: str
    raw_score: int
    weighted_score: int
    reason: str
    evidence_ids: list[str]


class ScoreSnapshot(TypedDict):
    """某条线索在指定规则版本下生成的不可重解释评分快照。

    快照绑定规则集、时间和引用证据，后续权重变化必须生成新快照而非覆盖旧结果，
    以便核验历史招商判断当时使用的依据。
    """

    score_snapshot_id: str
    lead_id: str
    rule_set_id: str
    rule_set_version: int
    total_score: int
    components: list[ScoreComponent]
    scored_at: str
    scored_by_user_id: str
    evidence_ids: list[str]
    operation_scope: str


class OutcomeFeedbackRecord(TypedDict):
    """人工确认的招商结果反馈，供未来规则优化使用。

    该记录保存真实业务结果和录入人，不直接修改权重或 Skill；后续学习模块只能基于
    已确认反馈提出新的规则版本建议，从而保留人工决策权和可回滚能力。
    """

    feedback_id: str
    lead_id: str
    outcome: str
    note: str
    recorded_at: str
    recorded_by_user_id: str
    score_snapshot_id: str | None
    operation_scope: str


class RelationshipGraphNodeRecord(TypedDict):
    """证据化关系图谱中的一个组织或公开关键人员节点。

    节点可代表目标企业、基金、徐州本地企业、高校、园区或公开披露的法人/创始人/股东/
    高管等关键人员，但不能因为人工输入名称就被当作事实：``evidence_ids`` 必须引用台账
    中已经存在的公开证据。``linked_lead_id`` 只用于把组织节点连接到既有招商线索；人员
    节点严禁绑定个人招商线索，只能以宽泛“徐州关联”或外地/未知分类呈现，不能保存电话、
    地址、资产、募资能力或其他私密信息。没有关联线索的节点仍必须给出支撑证据，且不
    允许借节点字段覆盖原始证据内容。
    """

    node_id: str
    display_name: str
    node_kind: str
    locality: str
    linked_lead_id: str | None
    evidence_ids: list[str]
    registration_reason: str
    created_at: str
    created_by_user_id: str
    operation_scope: str


class RelationshipGraphEdgeRecord(TypedDict):
    """两个图谱节点之间一条必须有既有证据支撑的关系边。

    ``from_node_id`` 与 ``to_node_id`` 保留人工登记的业务方向，``evidence_ids`` 只引用
    既有台账证据。公开关键人员关系仅允许“人员 → 已关联招商线索的组织”的受控角色
    类型，不能用于记录个人投资能力、资金来源、联系方式或招商意愿。关系边不能携带、
    复制或改写 URL、原始摘录、采集时间、内容哈希或评分依据；需要查看事实时应沿引用
    读取原始 ``EvidenceRecord``。
    """

    edge_id: str
    from_node_id: str
    to_node_id: str
    relationship_type: str
    evidence_ids: list[str]
    registration_reason: str
    created_at: str
    created_by_user_id: str
    operation_scope: str
    review_state: str
    review_history: list[RelationshipGraphEdgeReviewRecord]


class RelationshipGraphEdgeReviewRecord(TypedDict):
    """关系边一次只追加的人工业务复核记录。

    复核记录只保存旧状态、新状态、备注、服务器时间和认证操作者。它不能包含、更不能
    修改关系端点、关系类型、证据 ID 或 ``EvidenceRecord`` 中的来源 URL、原始摘录、
    采集时间、哈希及评分依据；需要补充事实时必须新建既有台账证据后另行登记关系。
    """

    old_review_state: str
    new_review_state: str
    review_note: str
    reviewed_at: str
    reviewer_user_id: str
    operation_scope: str


class RelationshipGraphEdgeEvidenceSupplementRecord(TypedDict):
    """关系边的一次只追加补证引用记录。

    补证记录与关系边的初始 ``evidence_ids`` 分开保存，确保补证不会覆盖原边的证据
    依据。它只引用既有 ``EvidenceRecord`` 的 ID，保存补证理由、服务器时间与认证
    操作者；不复制也不允许改写来源 URL、原始摘录、采集时间、内容哈希和评分依据。
    """

    supplement_id: str
    edge_id: str
    evidence_ids: list[str]
    supplement_note: str
    supplemented_at: str
    supplemented_by_user_id: str
    operation_scope: str


class RelationshipGraphAuditRecord(TypedDict):
    """图谱节点或关系边的只追加业务审计记录。"""

    audit_id: str
    action: str
    target_kind: str
    target_id: str
    operated_at: str
    operated_by_user_id: str
    operation_scope: str


class LedgerIntegrityResealRecord(TypedDict):
    """管理员对“无正式事实的签名不匹配台账”执行的一次只追加恢复审计。

    该记录仅适用于 ``leads`` 与 ``evidence`` 都为空、证据链仍为当前空链格式、且旧整本
    HMAC 已不能用当前服务端密钥验证的特殊恢复场景。它保存管理员认证身份、迁移前原始
    文件 SHA-256、服务器时间与核验说明，不复制或接受任何线索、URL、原始摘录、采集时间
    或签名值。恢复只会重新签整本空台账并追加本审计；一旦已有正式线索或证据，必须拒绝，
    不能以“恢复”名义覆盖或重写证据链。
    """

    reseal_id: str
    resealed_at: str
    resealed_by_admin_user_id: str
    pre_reseal_ledger_sha256: str
    previous_ledger_integrity_scheme: str
    verification_note: str
    operation_scope: str


class AutomationScheduleAuditRecord(TypedDict):
    """后台公开来源监控计划的一次只追加认证操作审计。

    记录只保存固定计划的启用/暂停动作、固定 Cron 口径、服务器时间、认证操作者及托管
    智能体 ID，便于招商人员追溯何时开启或暂停无人值守的免费公开来源监控，并作为共享
    台账下防止多名成员在不同智能体上重复启用同一计划的判断依据。它不保存调度令牌、
    会话 ID、
    URL、搜索词、线索、证据、来源摘录、采集时间或评分依据；计划管理不能成为重写原始
    证据链或扩大后台抓取范围的入口，完整记录仍由整本台账 HMAC 保护。
    """

    audit_id: str
    action: str
    job_id: str
    cron: str
    timezone: str
    operated_at: str
    operated_by_user_id: str
    agent_id: str
    operation_scope: str


class RelationshipDraftRecord(TypedDict):
    """从已签名正式证据确定性抽取的公开关键人员关系待复核草稿。

    草稿只引用 ``evidence_id`` 并保留原始摘录中可定位的一小段原文，用于协助人工发现
    法人、创始人、实控人、股东、董事和高管与目标组织的明确公开关系。它不是图谱节点、
    关系边或招商结论：不能保存联系方式、住址、资产、个人募资能力，也不能以草稿为由
    改写 EvidenceRecord 的来源 URL、原始摘录、采集时间、内容哈希或评分依据。
    """

    relationship_draft_id: str
    lead_id: str
    evidence_id: str
    organization_name: str
    person_display_name: str
    relationship_type: str
    person_locality_candidate: str
    matched_role_term: str
    matched_xuzhou_term: str | None
    matched_excerpt: str
    extractor_version: str
    created_at: str
    state: str
    operation_scope: str


class RelationshipDraftReviewRecord(TypedDict):
    """普通登录用户对关键人员关系草稿追加的业务判断记录。

    复核只记录由服务器认证身份得出的受控结论与备注，供后续人工登记既有图谱节点和边时
    参考；不能修改草稿抽取内容、证据 ID、组织/人员名称、原始证据或线索状态。即使标为
    ``ready_for_graph_registration``，也仍须通过图谱接口再次校验原始摘录，绝不自动建边。
    """

    review_id: str
    relationship_draft_id: str
    reviewed_at: str
    reviewed_by_user_id: str
    old_state: str
    new_state: str
    review_note: str
    operation_scope: str


class CoInvestmentSignalDraftRecord(TypedDict):
    """从已签名公开证据提取的协同投资/联合出资信号草稿。

    草稿仅表示目标主体在一手公开材料中出现了联合投资、共同设立基金或寻找出资方等
    可定位文本，供招商人员核验是否存在可继续沟通的产业资本协同窗口。它不是任何个人
    的资产、投资能力、风险偏好或参与承诺：不保存自然人的金额、联系方式、财务状况或
    适当性判断，也不能把中间人背书、政府背景或多方参与推断为确定资金。原始事实始终
    只由 ``evidence_id`` 回链，草稿不能覆盖其 URL、摘录、采集时间、哈希或评分依据。
    """

    co_investment_draft_id: str
    lead_id: str
    evidence_id: str
    entity_name: str
    sector: str
    signal_type: str
    matched_terms: list[str]
    matched_excerpt: str
    source_url: str
    source_published_at: str
    source_level: str
    xuzhou_association_tags: list[str]
    extractor_version: str
    created_at: str
    state: str
    operation_scope: str


class CoInvestmentSignalDraftReviewRecord(TypedDict):
    """普通登录用户对协同投资信号草稿追加的业务复核审计。

    复核只保存服务器认证操作者、旧新状态和备注。即使结论为可核验对接，也只意味着可由
    招商人员通过合规公开渠道进一步确认联系人和主体，不代表可募资、可销售金融产品或
    可向任何个人发起招揽；审计不能修改草稿、证据、线索、评分或原始来源字段。
    """

    review_id: str
    co_investment_draft_id: str
    reviewed_at: str
    reviewed_by_user_id: str
    old_state: str
    new_state: str
    review_note: str
    operation_scope: str


class LedgerDocument(TypedDict):
    """工作区内整本招商线索台账的文件结构。

    线索与证据是原始事实记录；评分规则、评分快照和人工结果反馈是可版本化的业务
    判断数据，三者分别保存，避免后续调整权重或学习结果覆盖来源 URL、原始摘录、
    采集时间及其他不可变证据链字段。
    """

    schema_version: int
    ledger_revision: int
    updated_at: str
    leads: list[LeadRecord]
    evidence: list[EvidenceRecord]
    evidence_integrity: dict[str, object]
    ledger_integrity: dict[str, object]
    evidence_integrity_migrations: list[dict[str, object]]
    ledger_integrity_reseals: list[LedgerIntegrityResealRecord]
    automation_schedule_audit: list[AutomationScheduleAuditRecord]
    # 钉钉日报投递只服务管理员显式登记的内部会话；它不承载外部联系方式、网页原文或
    # 任何可变证据字段，整本 HMAC 仍可审计订阅及发送/失败状态。
    dingtalk_daily_digest_subscriptions: list[dict[str, object]]
    dingtalk_daily_digest_delivery_audit: list[dict[str, object]]
    scoring_rule_sets: list[ScoringRuleSet]
    score_snapshots: list[ScoreSnapshot]
    outcome_feedback: list[OutcomeFeedbackRecord]
    discovery_runs: list[DiscoveryRunRecord]
    discovery_candidates: list[DiscoveryCandidateRecord]
    candidate_observations: list[CandidateObservationRecord]
    candidate_corroboration_attempts: list[CandidateCorroborationAttemptRecord]
    candidate_scope_exclusions: list[CandidateScopeExclusionRecord]
    candidate_business_reviews: list[CandidateBusinessReviewRecord]
    # 自动抽取的待确认草稿独立于正式 evidence，不能据此替代受控抓取和人工核验。
    evidence_drafts: list[EvidenceDraftRecord]
    evidence_draft_confirmation_audit: list[EvidenceDraftConfirmationRecord]
    evidence_draft_business_review_audit: list[EvidenceDraftBusinessReviewRecord]
    # “正式证据后是否纳入招商跟进池”的人工审批与证据链分表保存。它只追加业务判断、
    # 负责人和下一步，不允许替换原始证据、快照、来源 URL 或采集时间。
    lead_followup_approval_audit: list[LeadFollowupApprovalRecord]
    industry_metric_drafts: list[IndustryMetricDraftRecord]
    industry_metric_draft_reviews: list[IndustryMetricDraftReviewRecord]
    industry_business_metrics: list[IndustryBusinessMetricRecord]
    source_health: list[SourceHealthRecord]
    monitoring_runs: list[MonitoringRunRecord]
    source_feed_runs: list[SourceFeedRunRecord]
    trusted_sources: list[TrustedSourceRecord]
    trusted_source_audit: list[TrustedSourceAuditRecord]
    relationship_graph_nodes: list[RelationshipGraphNodeRecord]
    relationship_graph_edges: list[RelationshipGraphEdgeRecord]
    relationship_graph_edge_evidence_supplements: list[RelationshipGraphEdgeEvidenceSupplementRecord]
    relationship_graph_audit: list[RelationshipGraphAuditRecord]
    relationship_drafts: list[RelationshipDraftRecord]
    relationship_draft_reviews: list[RelationshipDraftReviewRecord]
    co_investment_signal_drafts: list[CoInvestmentSignalDraftRecord]
    co_investment_signal_draft_reviews: list[CoInvestmentSignalDraftReviewRecord]
