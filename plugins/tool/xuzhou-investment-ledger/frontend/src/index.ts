/**
 * 徐州招商工作台前端入口。
 *
 * React、Ant Design、认证令牌和当前智能体 ID 全部由 QwenPaw Console 宿主提供。本文件
 * 不保存用户身份、不直连任何外部网站、不把浏览器输入当作证据；所有数据均来自插件的
 * 已认证接口，所有会改变业务状态的调用也只发送接口允许的最小字段。
 */

type DataRecord = Record<string, any>;

const host = window.QwenPaw.host;
const React = host.React;
const antd = host.antd;
const { Alert, Badge, Button, Card, Checkbox, Col, Descriptions, Divider, Drawer, Empty, Input, List, Modal, Row, Select, Space, Spin, Statistic, Table, Tabs, Tag, Typography, message } = antd;
const { Title, Text: AntText, Paragraph } = Typography;

const API_ROOT = "/xuzhou-investment-ledger";

const REVIEW_LABELS: Record<string, string> = {
  pending_user_review: "等待用户复核",
  advance: "已纳入招商跟进池",
  need_evidence: "待补证",
  observe: "观察",
  waive_xuzhou: "无需徐州字段（待服务器重新核验）",
  invalid: "无效",
};

const DRAFT_CONFIRMATION_LABELS: Record<string, string> = {
  pending_human_confirmation: "等待人工确认",
  pending_user_review: "等待人工确认",
  pending_confirmation: "等待人工确认",
  pending: "等待人工确认",
  confirmed: "已确认入证",
  rejected: "未确认",
  invalid: "无效",
};

const PUBLIC_SOURCE_CRAWL_LABELS: Record<string, string> = {
  ready_for_public_crawl: "服务器可直接抓取",
  robots_disallowed: "网站不允许机器抓取",
  login_or_paid_access: "需要登录/会员",
  unsupported_api_or_credentials: "疑似 API 或带凭据入口",
  temporarily_unreachable: "暂时无法访问",
  unsafe_target: "未通过安全检查",
};

/** 候选业务状态的产品文案；值仍严格受服务器枚举约束。 */
const CANDIDATE_DECISION_OPTIONS: DataRecord[] = [
  {
    value: "need_evidence",
    label: "待补证：有价值，但目前依据不够",
    help: "选择它表示：请保留这条候选，待补足中国业务、徐州关联、投资落地或其他关键公开依据。你或团队成员负责判断缺什么；系统的固定低频监控仍会观察公开页面，但不会承诺为这一条自动找到新材料。",
  },
  {
    value: "observe",
    label: "观察：暂不下结论，保留低频关注",
    help: "选择它表示：目前不值得立即投入核验时间，但也不应删除。该候选仍可进入系统固定的低频自动观察池；系统不会因此自动确认事实、联系企业或创建线索。",
  },
  {
    value: "invalid",
    label: "无效：与招商范围无关或明显不适用",
    help: "选择它表示：停止系统后续自动访问这一候选，避免继续占用公开抓取预算。历史页面和你的判断理由仍会保留，必要时可审计复盘。",
  },
];

const CONFIRM_EVIDENCE_OPTION: DataRecord = {
  value: "confirm",
  label: "请求服务器重新核验并入证",
  help: "适用于你已阅读材料，认为草稿所述事实与页面一致且关键字段完整的情形。提交后不是直接“放行”：服务器会重新访问原页面并校验；通过才追加正式证据，失败则明确显示未生效原因。",
};

const SECTOR_LABELS: Record<string, string> = {
  semiconductor: "半导体",
  embodied_intelligence: "具身智能",
  biomedicine: "医药",
  other: "其他",
};

const CLAIM_LABELS: Record<string, string> = {
  financing: "融资",
  fund_raising: "基金募资",
  xuzhou_connection: "徐州关联",
  cooperation: "合作",
  expansion: "扩张",
  industry_focus: "产业方向",
  fund_investment: "投资案例",
  risk: "企业风险",
  unclassified: "待人工分类",
};

const CARD_STYLE = {
  borderRadius: 14,
  border: "1px solid rgba(16, 54, 44, .12)",
  boxShadow: "0 8px 24px rgba(28, 64, 52, .06)",
};

/**
 * 读取 Console 当前选中的智能体 ID，确保浏览器请求与用户正在查看的团队台账一致。
 *
 * 输入为空，输出为当前标签页的受限智能体 ID 或 ``null``。该值只是 API 网关用于定位
 * 工作区的请求头，不能代表用户身份；用户身份仍由服务器验证 Bearer 令牌，前端不会伪造
 * ``reviewer_user_id`` 或跨智能体读取数据。
 */
function getSelectedAgentId(): string | null {
  try {
    const raw = window.sessionStorage.getItem("qwenpaw-agent-storage") ?? window.localStorage.getItem("qwenpaw-agent-storage");
    if (!raw) return null;
    const selected = JSON.parse(raw)?.state?.selectedAgent;
    return typeof selected === "string" && selected ? selected : null;
  } catch {
    return null;
  }
}

/**
 * 构造认证与工作区范围请求头，不从表单、模型或 URL 接收用户身份。
 *
 * 输入为空，输出为当前 Console 会话令牌和可选智能体 ID。服务器会重新验证令牌、名册和
 * 工作区权限；本函数只帮助页面沿用宿主登录状态，不保存密码、密钥或业务证据。
 */
function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const token = host.getApiToken?.();
  if (token) headers.Authorization = `Bearer ${token}`;
  const agentId = getSelectedAgentId();
  if (agentId) headers["X-Agent-Id"] = agentId;
  return headers;
}

/**
 * 调用插件 API 并将服务器的安全错误转换为可读提示。
 *
 * 输入是固定插件路径和可选受限请求体，输出为服务器 JSON。该函数不会替页面追加用户 ID、
 * 来源 URL、原始摘录、风险等级或事实分类；写入接口只能收到各自 Pydantic 模型允许的
 * 决定和备注，证据链是否可访问或可修改始终由后端决定。
 */
async function request(path: string, method = "GET", body?: DataRecord): Promise<DataRecord> {
  const response = await fetch(host.getApiUrl(`${API_ROOT}${path}`), {
    method,
    headers: { ...authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const raw = await response.text();
  let payload: DataRecord = {};
  try {
    payload = raw ? JSON.parse(raw) : {};
  } catch {
    payload = { detail: raw };
  }
  if (!response.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

/**
 * 并发读取一个看板资源；单个统计接口失败不会遮蔽其他已可用的业务数据。
 *
 * 输入是只读 API 路径，输出为数据或附带 ``_loadError`` 的空对象。它只处理展示层错误，
 * 不触发重试、抓取、监控、复核、评分或任何证据写入；用户可用页面顶部刷新按钮主动重读。
 */
async function safeGet(path: string): Promise<DataRecord> {
  try {
    return await request(path);
  } catch (error) {
    return { _loadError: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * 把服务器枚举转换为中文展示标签，不把未知新值猜成业务结论。
 *
 * 输入是任意记录值和固定标签表，输出为已知中文标签或原值/“—”。该函数只影响文字显示，
 * 不修改服务器状态、证据或评分；未知枚举保留原样，方便后续开发者发现后端新增状态。
 */
function label(value: unknown, labels: Record<string, string>): string {
  if (typeof value !== "string" || !value) return "—";
  return labels[value] ?? value;
}

/**
 * 将 ISO 时间或服务器日期格式化为本地易读文本，不把缺失日期编造成当前时间。
 *
 * 输入为来自服务器的日期值，输出为格式化字符串或“—”。函数不参与证据时效判断，避免
 * 浏览器时区、解析失败或空值影响后端风险队列和人工复核逻辑。
 */
function formatDate(value: unknown): string {
  if (typeof value !== "string" || !value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 依据服务器状态选择展示色，不依据模型标签或前端推断标红。
 *
 * 输入为线索或证据的已有状态，输出为 Ant Design 颜色名。红色仅在结构化企业风险预警仍
 * 打开时展示；普通待补证、来源词命中或“高优先级候选”不能被浏览器擅自改写为企业风险。
 */
function stateColor(value: unknown): string {
  if (value === "advance") return "green";
  if (value === "invalid") return "default";
  if (value === "need_evidence") return "gold";
  if (value === "observe") return "blue";
  return "processing";
}

/**
 * 呈现一张统计卡片，统一突出数字与其业务解释。
 *
 * 输入是标题、数值、说明和可选告警色，输出仅为 React 展示节点。该组件不汇总不同统计
 * 口径、不从列表推测事实，也不含任何点击后写入台账的行为。
 */
function SummaryCard({ title, value, note, danger = false }: DataRecord): any {
  return React.createElement(
    Card,
    { style: { ...CARD_STYLE, height: "100%", borderTop: `3px solid ${danger ? "#cf1322" : "#0f766e"}` }, bodyStyle: { padding: 18 } },
    React.createElement(Statistic, { title, value: typeof value === "number" ? value : 0, valueStyle: { color: danger ? "#b42318" : "#123f35", fontWeight: 700 } }),
    React.createElement(AntText, { type: "secondary", style: { display: "block", marginTop: 8, fontSize: 12 } }, note),
  );
}

/**
 * 将很长的服务器只读文本以明确的“展开/收起”控制呈现。
 *
 * 输入是已认证接口返回的展示文本及折叠长度，输出为截断或全文及可逆按钮。此组件不写入
 * 快照、不改变原文，也不根据折叠后的文本产生任何业务结论；它只解决长文本挤坏表格和
 * Ant Design 内置省略号只能展开、无法收起的问题。
 */
function CollapsibleText({ value, collapsedLength = 360, emptyText = "服务器未保存可展示文本。", inline = false }: DataRecord): any {
  const [expanded, setExpanded] = React.useState(false);
  const text = typeof value === "string" && value.trim() ? value.trim() : emptyText;
  const needsToggle = text.length > collapsedLength;
  const displayed = expanded || !needsToggle ? text : `${text.slice(0, collapsedLength).trimEnd()}…`;
  return React.createElement(
    Space,
    { direction: inline ? "horizontal" : "vertical", size: inline ? 4 : 6, align: inline ? "start" : undefined, style: { width: "100%" } },
    React.createElement(AntText, { style: { display: "block", whiteSpace: inline ? "normal" : "pre-wrap", overflowWrap: "anywhere", wordBreak: "break-word", lineHeight: 1.65 } }, displayed),
    needsToggle ? React.createElement(Button, { type: "link", size: "small", style: { padding: 0, height: "auto", alignSelf: inline ? "flex-start" : undefined }, onClick: () => setExpanded((current: boolean) => !current) }, expanded ? "收起" : "展开全文") : null,
  );
}

/**
 * 用业务语言解释候选的完整处理闭环，而非要求用户理解内部状态码。
 *
 * 输入是最近一次确认结果及跳转回调，输出为纯说明和既有页面跳转按钮。它不替用户选择
 * 业务状态、不触发抓取或学习；所有“谁做、何时生效”的事实均与后端固定流程保持一致。
 */
function CandidateWorkflowGuide({ latestOutcome, onOpenLeads }: DataRecord): any {
  const guideCards = [
    {
      title: "1. 先分清“链接、材料、证据”",
      color: "#0f766e",
      text: "发现到原网页链接，只表示系统保存了链接、标题和搜索摘要。服务器还须保存可复查的页面副本、采集时间和内容哈希，才叫“已保存页面材料”；通过重新核验后，才会成为“正式证据”。",
    },
    {
      title: "2. 谁做什么",
      color: "#1d4ed8",
      text: "任一已登录的团队业务成员阅读已保存材料并作判断；服务器只按固定低频计划访问允许的公开网页并保存快照。系统不会联系企业，也不能保证为某一候选找到新材料。",
    },
    {
      title: "3. 什么时候可以提交",
      color: "#a16207",
      text: "只有草稿字段完整且不缺中国/徐州依据时，才会显示“请求重新核验并入证”。服务器会重新抓取原页面；只有校验通过才追加正式证据，不能跳过这一步。",
    },
    {
      title: "4. 怎么知道是否生效",
      color: "#7c3aed",
      text: "提交后页面会显示“已生效”或“未生效”的原因，并刷新台账。已生效的正式证据可在“线索与证据”页查看；业务判断则保存在候选的审计记录中。",
    },
  ];
  const outcomeAlert = latestOutcome
    ? React.createElement(Alert, {
        type: latestOutcome.type === "warning" ? "warning" : "success",
        showIcon: true,
        message: latestOutcome.message,
        description: latestOutcome.description,
        action: latestOutcome.confirmed ? React.createElement(Button, { size: "small", onClick: onOpenLeads }, "查看线索与证据") : undefined,
      })
    : null;
  return React.createElement(
    Card,
    { title: "先看这里：一条候选怎么处理", style: CARD_STYLE },
    React.createElement(
      Space,
      { direction: "vertical", size: 14, style: { width: "100%" } },
      React.createElement(Alert, { type: "info", showIcon: true, message: "候选链接 ≠ 已保存材料 ≠ 正式证据", description: "“已找到原网页”不代表系统拥有可审计材料：浏览器打开网页不会保存到台账。只有服务器按规则保存原页面副本、可见文本、采集时间和内容哈希后，团队才能共同核对；之后还必须重新抓取并逐项匹配，才可能新增正式证据。" }),
      outcomeAlert,
      React.createElement(
        Row,
        { gutter: [12, 12] },
        ...guideCards.map((item) => React.createElement(Col, { key: item.title, xs: 24, sm: 12, xl: 6 }, React.createElement(Card, { size: "small", style: { height: "100%", borderTop: `3px solid ${item.color}` }, bodyStyle: { padding: 14 } }, React.createElement(AntText, { strong: true, style: { display: "block", marginBottom: 6 } }, item.title), React.createElement(AntText, { type: "secondary", style: { fontSize: 13, lineHeight: 1.65 } }, item.text)))),
      ),
      React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, "机器学习的边界：每日学习只会基于已确认正式证据生成“来源优先顺序建议”；满足样本门槛后仍必须由人工批准。它不会自行改写 Skill、搜索词、证据、评分或既有业务判断。"),
    ),
  );
}

/**
 * 展示证据卡片并显式说明服务器分类的原文支持状态。
 *
 * 输入为一条只读证据，输出为来源链接、摘录、分类和待人工核验提示。页面只显示服务器保存
 * 的原始字段，不能编辑 URL、摘录、采集时间、哈希或分类；“待人工分类”不会被渲染成事实。
 */
function EvidenceCard({ evidence }: DataRecord): any {
  const claimType = label(evidence?.claim_type, CLAIM_LABELS);
  const support = evidence?.claim_type_text_support;
  const supportText = support === "matched_fixed_terms" || support === "server_reclassified_risk" ? "服务器原文固定词支持" : "待人工分类，不作为自动事实";
  const sourceTitle = typeof evidence?.source_title === "string" ? evidence.source_title : "未命名来源";
  return React.createElement(
    Card,
    { size: "small", style: { marginBottom: 10, borderRadius: 10 } },
    React.createElement(
      Space,
      { direction: "vertical", size: 6, style: { width: "100%" } },
      React.createElement(
        Space,
        { wrap: true },
        React.createElement(Tag, { color: evidence?.claim_type === "risk" ? "red" : evidence?.claim_type === "unclassified" ? "default" : "cyan" }, claimType),
        React.createElement(Tag, null, label(evidence?.source_level, { primary: "一手", secondary: "二手", lead_only: "线索" })),
        React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, supportText),
      ),
      React.createElement("a", { href: evidence?.source_url, target: "_blank", rel: "noreferrer", style: { fontWeight: 600, color: "#0f766e" } }, sourceTitle),
      React.createElement(Paragraph, { style: { marginBottom: 0, color: "#334155" }, ellipsis: { rows: 3, expandable: true, symbol: "展开" } }, evidence?.excerpt || "未保存可展示摘录。"),
      React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, `来源日期：${formatDate(evidence?.source_published_at)}　采集时间：${formatDate(evidence?.collected_at ?? evidence?.created_at)}　证据 ID：${evidence?.evidence_id ?? "—"}`),
    ),
  );
}

/**
 * 显示正式证据后清晰的人机分工卡。它把“入证”与“纳入招商跟进池”分开：前者由服务器
 * 重抓校验，后者必须由团队成员填写完整业务判断。此组件只读显示卡片和打开受限表单，
 * 不会自动联系企业、发送钉钉或把计划写成企业事实。
 */
function FollowupApprovalCard({ lead, followup, loading, onLoad, onApprove }: DataRecord): any {
  const card = followup?.card ?? followup;
  const machineChecks = Array.isArray(card?.machine_checks) ? card.machine_checks : [];
  const humanItems = Array.isArray(card?.human_required_items) ? card.human_required_items : [];
  const blockers = Array.isArray(card?.blockers) ? card.blockers : [];
  const approval = card?.current_approval as DataRecord | null | undefined;
  const canApprove = card?.can_approve === true;
  const stateColorName = card?.state === "approved_for_followup" ? "green" : canApprove ? "blue" : "gold";
  return React.createElement(
    React.Fragment,
    null,
    React.createElement(Divider, { orientation: "left" }, "第 3 步：人工招商跟进决定"),
    React.createElement(
      Card,
      {
        size: "small",
        style: { borderRadius: 12, border: "1px solid rgba(14, 116, 144, .2)", background: "#f0f9ff" },
        title: "招商跟进审批卡：机器核对，业务人员拍板",
        extra: React.createElement(Button, { size: "small", loading, onClick: () => onLoad(lead.lead_id) }, "刷新审批卡"),
      },
      !card
        ? React.createElement(Empty, { description: "尚未读取审批卡；点击“刷新审批卡”查看。" })
        : React.createElement(
            Space,
            { direction: "vertical", size: 14, style: { width: "100%" } },
            React.createElement(Alert, { type: card.state === "approved_for_followup" ? "success" : canApprove ? "info" : "warning", showIcon: true, message: card.state_label || "等待人工招商判断", description: card.boundary || "审批通过只表示可纳入内部招商跟进池，不表示企业已同意来徐州、已签约或系统会自动对外联系。" }),
            React.createElement(Descriptions, { bordered: true, size: "small", column: 2, items: [
              { key: "evidence", label: "机器已确认", children: `已关联 ${card.formal_evidence_count ?? 0} 条正式证据` },
              { key: "status", label: "当前招商状态", children: React.createElement(Tag, { color: stateColorName }, card.state_label || "—") },
              { key: "machine", label: "机器负责", children: card.machine_role || "保存和校验正式证据、提示风险、记录审计。" },
              { key: "human", label: "人负责", children: card.human_role || "判断招商价值、安排负责人和执行下一步。" },
            ] }),
            blockers.length ? React.createElement(Alert, { type: "warning", showIcon: true, message: "提交前请先处理以下事项", description: blockers.join("；") }) : null,
            React.createElement(Row, { gutter: [12, 12] },
              React.createElement(Col, { xs: 24, md: 12 }, React.createElement(Card, { size: "small", title: "机器已经做完什么" }, React.createElement(List, { size: "small", dataSource: machineChecks, renderItem: (item: DataRecord) => React.createElement(List.Item, null, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(Tag, { color: item.passed ? "green" : "gold" }, item.passed ? "已完成/已提示" : "需要人工处理"), React.createElement(AntText, { strong: true }, item.title || "机器检查"), React.createElement(AntText, { type: "secondary" }, item.detail || "—"))) }))),
              React.createElement(Col, { xs: 24, md: 12 }, React.createElement(Card, { size: "small", title: "业务人员必须确认什么" }, React.createElement(List, { size: "small", dataSource: humanItems, renderItem: (item: DataRecord, index: number) => React.createElement(List.Item, null, `${index + 1}. ${item.label || "人工检查项"}`) }))),
            ),
            approval ? React.createElement(Card, { size: "small", title: "当前已记录的招商跟进决定" }, React.createElement(Descriptions, { size: "small", column: 1, items: [
              { key: "approver", label: "确认人", children: approval.approved_by_user_id || "—" },
              { key: "when", label: "确认时间", children: formatDate(approval.approved_at) },
              { key: "owner", label: "负责人", children: approval.owner_name || "—" },
              { key: "date", label: "下一步完成日期", children: approval.target_date || "—" },
              { key: "action", label: "下一步动作", children: `${label(approval.next_action, { official_information_check: "核实企业公开官方信息", internal_industry_matching: "完成内部产业与载体匹配研判", prepare_human_outreach: "准备人工初步沟通", assign_followup_research: "分派进一步调研", other_human_action: "其他人工动作" })}：${approval.next_action_detail || "—"}` },
              { key: "value", label: "招商价值判断", children: approval.business_value_reason || "—" },
            ] })) : React.createElement(AntText, { type: "secondary" }, "尚未有人确认纳入招商跟进池。请先阅读证据链，再完成右侧六项人工检查。"),
            React.createElement(Button, { type: "primary", disabled: !canApprove, onClick: () => onApprove(lead), style: { alignSelf: "flex-start", background: "#0f766e" } }, approval ? "重新确认或更新跟进计划" : "填写招商跟进审批卡"),
          ),
    ),
  );
}

/**
 * 显示单线索侧栏及其只读证据链、研判报告、招商准备卡和人工审批卡。
 *
 * 输入为服务器加载的详情及操作回调，输出为可关闭抽屉。页面把“正式证据”与“招商跟进
 * 决定”放在相邻但独立的步骤，避免业务人员误以为入证等于系统已经替其作出招商结论。
 */
function LeadDrawer({ open, onClose, details, report, outreach, followup, reportLoading, outreachLoading, followupLoading, onLoadReport, onLoadOutreach, onLoadFollowup, onReview, onApproveFollowup, onSnapshot, onOutcome }: DataRecord): any {
  const lead = details?.lead as DataRecord | undefined;
  const evidence = Array.isArray(details?.evidence) ? details.evidence : [];
  const sections = Array.isArray(report?.standardized_sections) ? report.standardized_sections : [];
  const scoreSnapshots = Array.isArray(report?.score_snapshots) ? report.score_snapshots : [];
  const actionCards = Array.isArray(outreach?.action_cards) ? outreach.action_cards : [];
  const drawerTitle = lead ? `${lead.entity_name}｜招商研判` : "招商线索详情";
  return React.createElement(
    Drawer,
    { open, onClose, width: "min(760px, 94vw)", title: drawerTitle, destroyOnClose: false },
    !lead
      ? React.createElement(Spin, { style: { display: "block", margin: "60px auto" } })
      : React.createElement(
          Space,
          { direction: "vertical", size: 16, style: { width: "100%" } },
          React.createElement(
            Alert,
            { type: lead.red_alert ? "error" : "info", showIcon: true, message: lead.red_alert ? `风险等级：${lead.risk_alert_level ?? "待人工核验"}` : "这是一条已有正式证据的线索，尚未自动形成招商结论", description: lead.red_alert ? lead.alert_reason || "请在招商跟进审批卡中明确风险处理判断后再决定是否跟进。" : "机器完成入证后，只能提供材料与提示；是否纳入招商跟进池、由谁做和何时完成，必须由业务人员填写审批卡。" },
          ),
          React.createElement(Descriptions, { bordered: true, size: "small", column: 2, items: [
            { key: "sector", label: "产业", children: label(lead.sector, SECTOR_LABELS) },
            { key: "state", label: "人工状态", children: React.createElement(Tag, { color: stateColor(lead.review_state) }, label(lead.review_state, REVIEW_LABELS)) },
            { key: "kind", label: "主体类型", children: label(lead.entity_kind, { enterprise: "企业", fund: "基金", startup_team: "创业团队" }) },
            { key: "updated", label: "最近更新", children: formatDate(lead.updated_at) },
          ] }),
          React.createElement(
            Space,
            { wrap: true },
            React.createElement(Button, { onClick: () => onReview(lead) }, "处理证据问题或暂不推进"),
            React.createElement(Button, { onClick: () => onSnapshot(lead.lead_id) }, "生成本次评分"),
            React.createElement(Button, { loading: reportLoading, onClick: () => onLoadReport(lead.lead_id) }, "读取标准研判报告"),
            React.createElement(Button, { loading: outreachLoading, onClick: () => onLoadOutreach(lead.lead_id) }, "读取招商准备卡"),
          ),
          React.createElement(FollowupApprovalCard, { lead, followup, loading: followupLoading, onLoad: onLoadFollowup, onApprove: onApproveFollowup }),
          React.createElement(Divider, { orientation: "left" }, `可追溯证据链（${evidence.length}）`),
          evidence.length ? evidence.map((item: DataRecord) => React.createElement(EvidenceCard, { key: item.evidence_id, evidence: item })) : React.createElement(Empty, { description: "暂无可展示证据" }),
          report
            ? React.createElement(
                React.Fragment,
                null,
                React.createElement(Divider, { orientation: "left" }, "标准研判报告（只读）"),
                sections.map((section: DataRecord) => React.createElement(Card, { key: section.section_key, size: "small", style: { marginBottom: 8 } }, React.createElement(Space, { direction: "vertical", size: 4 }, React.createElement(AntText, { strong: true }, section.title), React.createElement(Tag, { color: section.status === "evidence_available" ? "green" : "gold" }, section.status === "evidence_available" ? "已有可追溯证据" : "待补证"), React.createElement(AntText, { type: "secondary" }, section.caveat || section.instruction || "仍需人工核验原文。"))))
                , scoreSnapshots.length ? React.createElement(Card, { size: "small", title: "已有评分快照与结果反馈", style: { marginTop: 8 } }, React.createElement(List, { size: "small", dataSource: scoreSnapshots, renderItem: (snapshot: DataRecord) => React.createElement(List.Item, { actions: [React.createElement(Button, { key: "outcome", size: "small", onClick: () => onOutcome({ lead_id: lead.lead_id, score_snapshot_id: snapshot.score_snapshot_id }) }, "录入实际结果")] }, React.createElement(AntText, null, `快照：${snapshot.score_snapshot_id ?? "—"}　总分：${snapshot.total_score ?? "—"}　生成：${formatDate(snapshot.created_at)}`)) })) : React.createElement(AntText, { type: "secondary", style: { display: "block", marginTop: 8 } }, "暂无评分历史；生成评分后可在此记录实际招商结果。")
              )
            : null,
          outreach
            ? React.createElement(
                React.Fragment,
                null,
                React.createElement(Divider, { orientation: "left" }, "招商准备卡（仅供人工核验）"),
                actionCards.length ? React.createElement(List, { size: "small", bordered: true, dataSource: actionCards, renderItem: (item: DataRecord) => React.createElement(List.Item, null, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, item.title || item.action_key || "待办"), React.createElement(AntText, null, item.reason || item.instruction || "请基于原始证据人工核验。"))) }) : React.createElement(Empty, { description: "暂无准备卡待办" }),
              )
            : null,
        ),
  );
}

/**
 * 提供普通登录用户可用的线索业务复核表单。
 *
 * 输入是待复核线索和提交/关闭回调，输出为受控模态框。浏览器仅提交 ``decision`` 与
 * ``review_note``；不提供 reviewer_user_id、证据、URL、摘录、评分或风险字段，服务器
 * 会把当前认证身份写入审计，并在保存前后验证不可变证据链没有变化。
 */
function ReviewModal({ lead, onClose, onSubmit, submitting }: DataRecord): any {
  const [decision, setDecision] = React.useState("need_evidence");
  const [note, setNote] = React.useState("");
  React.useEffect(() => {
    setDecision("need_evidence");
    setNote("");
  }, [lead?.lead_id]);
  return React.createElement(
    Modal,
    { open: !!lead, title: lead ? `处理线索：${lead.entity_name}` : "处理线索", okText: "保存处理结果", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !note.trim() }, onCancel: onClose, onOk: () => onSubmit(lead.lead_id, decision, note.trim()) },
    React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 16 }, message: "这里不能直接“推进”招商", description: "此处只适用于待补证、观察或不纳入。若确认值得由徐州跟进，请关闭本窗口，在“招商跟进审批卡”逐项核对后提交。来源 URL、原始摘录、采集时间、原始分类依据与操作人身份均不能在此修改。" }),
    React.createElement(AntText, { strong: true }, "业务状态"),
    React.createElement(Select, { value: decision, style: { width: "100%", marginTop: 8, marginBottom: 16 }, onChange: setDecision, options: [
      { value: "need_evidence", label: "待补证：现有正式依据不足" },
      { value: "observe", label: "观察：暂不进入招商跟进" },
      { value: "invalid", label: "不纳入：与当前招商范围不适用" },
    ] }),
    React.createElement(AntText, { strong: true }, "复核备注"),
    React.createElement(Input.TextArea, { value: note, onChange: (event: any) => setNote(event.target.value), autoSize: { minRows: 4, maxRows: 8 }, maxLength: 2000, showCount: true, style: { marginTop: 8 }, placeholder: "说明为什么待补证、观察或不纳入；此说明不会改写原网页材料。" }),
  );
}

/**
 * 让业务人员逐项确认招商价值、责任和下一步。浏览器只提交受控的业务计划字段；企业事实
 * 仍来自服务器已有证据，审批人身份由服务端会话取得。提交成功也不会自动联系企业。
 */
function FollowupApprovalModal({ lead, followup, onClose, onSubmit, submitting }: DataRecord): any {
  const [checks, setChecks] = React.useState({
    identity_and_subject_checked: false,
    formal_evidence_checked: false,
    industry_fit_checked: false,
    geographic_basis_checked: false,
    key_claim_corroborated: false,
    risk_and_conflict_checked: false,
  } as DataRecord);
  const [businessValueReason, setBusinessValueReason] = React.useState("");
  const [nextAction, setNextAction] = React.useState("official_information_check");
  const [nextActionDetail, setNextActionDetail] = React.useState("");
  const [ownerName, setOwnerName] = React.useState("");
  const [targetDate, setTargetDate] = React.useState("");
  const [reviewNote, setReviewNote] = React.useState("");
  React.useEffect(() => {
    setChecks({ identity_and_subject_checked: false, formal_evidence_checked: false, industry_fit_checked: false, geographic_basis_checked: false, key_claim_corroborated: false, risk_and_conflict_checked: false });
    setBusinessValueReason("");
    setNextAction("official_information_check");
    setNextActionDetail("");
    setOwnerName("");
    setTargetDate("");
    setReviewNote("");
  }, [lead?.lead_id]);
  const items = Array.isArray(followup?.card?.human_required_items) ? followup.card.human_required_items : [
    { key: "identity_and_subject_checked", label: "已核对企业主体与材料所述对象一致" },
    { key: "formal_evidence_checked", label: "已逐项阅读并核对正式证据" },
    { key: "industry_fit_checked", label: "已判断其与徐州目标产业或载体的匹配性" },
    { key: "geographic_basis_checked", label: "已核对徐州/中国业务依据，或写明无徐州字段仍纳入研判的理由" },
    { key: "key_claim_corroborated", label: "关键事项已由一手公开来源或独立公开来源交叉核对" },
    { key: "risk_and_conflict_checked", label: "已核对风险、冲突与时效，并明确当前没有未处理的阻断项" },
  ];
  const actionOptions = Array.isArray(followup?.card?.next_action_options) ? followup.card.next_action_options : [];
  const allChecked = items.every((item: DataRecord) => checks[item.key] === true);
  const complete = allChecked && businessValueReason.trim() && nextActionDetail.trim() && ownerName.trim() && targetDate && reviewNote.trim();
  return React.createElement(
    Modal,
    { open: !!lead, title: lead ? `招商跟进审批：${lead.entity_name}` : "招商跟进审批", width: "min(760px, 94vw)", okText: "确认纳入招商跟进池", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !complete }, onCancel: onClose, onOk: () => onSubmit(lead.lead_id, { ...checks, business_value_reason: businessValueReason.trim(), next_action: nextAction, next_action_detail: nextActionDetail.trim(), owner_name: ownerName.trim(), target_date: targetDate, review_note: reviewNote.trim() }) },
    React.createElement(Space, { direction: "vertical", size: 14, style: { width: "100%" } },
      React.createElement(Alert, { type: "warning", showIcon: true, message: "这是人工业务决策，不是机器自动招商", description: "机器已保存和核验证据；请由实际负责业务判断的团队成员逐项勾选并写清后续计划。提交后只会记为“可跟进”，不会自动联系企业、发送外部消息或承诺政策。" }),
      React.createElement(Card, { size: "small", title: "第 1 部分：逐项人工确认（全部必选）" }, React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } }, items.map((item: DataRecord) => React.createElement(Checkbox, { key: item.key, checked: checks[item.key] === true, onChange: (event: any) => setChecks((current: DataRecord) => ({ ...current, [item.key]: event.target.checked })) }, item.label)))),
      React.createElement(Card, { size: "small", title: "第 2 部分：写清为什么值得跟进" }, React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } },
        React.createElement(AntText, { strong: true }, "招商价值判断（必填）"),
        React.createElement(Input.TextArea, { value: businessValueReason, onChange: (event: any) => setBusinessValueReason(event.target.value), maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, placeholder: "例如：该企业的产品方向与徐州某重点产业链匹配，且公开融资/扩产信号仍具时效，因此建议进入人工跟进。" }),
        React.createElement(AntText, { strong: true }, "下一步动作（必填）"),
        React.createElement(Select, { value: nextAction, style: { width: "100%" }, onChange: setNextAction, options: actionOptions.length ? actionOptions : [
          { value: "official_information_check", label: "核实企业公开官方信息" },
          { value: "internal_industry_matching", label: "完成内部产业与载体匹配研判" },
          { value: "prepare_human_outreach", label: "准备由业务人员通过公开渠道开展初步沟通" },
          { value: "assign_followup_research", label: "分派招商主管完成进一步调研" },
          { value: "other_human_action", label: "其他人工动作（请说明）" },
        ] }),
        React.createElement(Input.TextArea, { value: nextActionDetail, onChange: (event: any) => setNextActionDetail(event.target.value), maxLength: 2000, showCount: true, autoSize: { minRows: 2, maxRows: 5 }, placeholder: "具体说明下一步由人做什么；不要填写个人联系方式，也不要把计划写成企业已经承诺。" }),
      )),
      React.createElement(Card, { size: "small", title: "第 3 部分：明确谁负责、何时完成" }, React.createElement(Row, { gutter: [12, 12] },
        React.createElement(Col, { xs: 24, md: 12 }, React.createElement(Space, { direction: "vertical", size: 6, style: { width: "100%" } }, React.createElement(AntText, { strong: true }, "负责人（必填）"), React.createElement(Input, { value: ownerName, onChange: (event: any) => setOwnerName(event.target.value), maxLength: 100, placeholder: "填写内部负责人姓名或岗位" }))),
        React.createElement(Col, { xs: 24, md: 12 }, React.createElement(Space, { direction: "vertical", size: 6, style: { width: "100%" } }, React.createElement(AntText, { strong: true }, "下一步完成日期（必填）"), React.createElement("input", { type: "date", value: targetDate, onChange: (event: any) => setTargetDate(event.target.value), style: { width: "100%", minHeight: 32, border: "1px solid #d9d9d9", borderRadius: 6, padding: "4px 10px" } }))),
      )),
      React.createElement(AntText, { strong: true }, "审批说明（必填）"),
      React.createElement(Input.TextArea, { value: reviewNote, onChange: (event: any) => setReviewNote(event.target.value), maxLength: 2000, showCount: true, autoSize: { minRows: 2, maxRows: 5 }, placeholder: "说明本次由谁基于哪些证据和判断确认进入跟进池。" }),
    ),
  );
}

/**
 * 总览页集中展示真实待办与台账规模，不把统计数字解释为招商结果。
 *
 * 输入是服务器只读汇总、行动中心和选中回调，输出为统计卡与人工待办表。红色行仅使用后端
 * 结构化风险队列，不依据前端关键词着色；点击会跳转到对应线索的证据链，不能自动推进。
 */
function OverviewTab({ leadsData, actionCenter, workSummary, candidates, discoveryCandidates, onOpenLead }: DataRecord): any {
  const leadCount = leadsData?.count ?? 0;
  const actionSummary = actionCenter?.summary ?? {};
  const candidateCount = candidates?.count ?? candidates?.observation_count ?? 0;
  const discoveryCandidateCount = discoveryCandidates?.count ?? 0;
  const actions = Array.isArray(actionCenter?.items) ? actionCenter.items : [];
  const columns = [
    { title: "优先级", dataIndex: "action_priority_rank", width: 78, render: (value: unknown) => React.createElement(Tag, { color: value === 1 ? "red" : value === 2 ? "gold" : "blue" }, `P${value ?? "—"}`) },
    { title: "待办", dataIndex: "item_kind", render: (value: unknown, row: DataRecord) => React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, String(value ?? "人工待办")), React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, row.reason || "请核验来源与当前影响。")) },
    { title: "对象", render: (_: unknown, row: DataRecord) => row.lead?.entity_name ? React.createElement(Button, { type: "link", onClick: () => onOpenLead(row.lead.lead_id) }, row.lead.entity_name) : "关系/配置待办" },
  ];
  return React.createElement(
    Space,
    { direction: "vertical", size: 18, style: { width: "100%" } },
    React.createElement(Alert, { type: "warning", showIcon: true, message: "业务流程：网页材料 → 正式证据 → 人工招商审批 → 人工跟进 → 人工确认结果", description: "机器负责发现、留存、重新核验、提示风险和记录审计；业务人员负责判断是否值得徐州跟进、指定负责人并完成实际调研或沟通。任何统计、评分或“已纳入跟进池”都不等于企业已同意落地或已经招商成功。" }),
    workSummary?.candidate_source_diversity?.warning ? React.createElement(Alert, { type: "warning", showIcon: true, message: "来源集中度提醒", description: workSummary.candidate_source_diversity.warning }) : null,
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, sm: 12, lg: 6 }, React.createElement(SummaryCard, { title: "已登记线索", value: leadCount, note: "当前团队共享台账" })),
      React.createElement(Col, { xs: 24, sm: 12, lg: 6 }, React.createElement(SummaryCard, { title: "企业风险待复核", value: actionSummary.red_alert_lead_count ?? 0, note: "仅结构化风险预警", danger: true })),
      React.createElement(Col, { xs: 24, sm: 12, lg: 6 }, React.createElement(SummaryCard, { title: "人工待办", value: actionSummary.action_item_count ?? 0, note: "不自动推进或外部推送" })),
      React.createElement(Col, { xs: 24, sm: 12, lg: 6 }, React.createElement(SummaryCard, { title: "已发现候选链接", value: discoveryCandidateCount, note: "尚未必有页面快照或正式证据" })),
      React.createElement(Col, { xs: 24, sm: 12, lg: 6 }, React.createElement(SummaryCard, { title: "待补证候选快照", value: candidateCount, note: "自动监控保存，尚非事实" })),
    ),
    React.createElement(Card, { title: "人工行动中心", style: CARD_STYLE, extra: React.createElement(AntText, { type: "secondary" }, `生成时间：${formatDate(actionCenter?.generated_at)}`) }, React.createElement(Table, { size: "small", rowKey: (row: DataRecord) => `${row.item_kind}-${row.lead?.lead_id ?? row.relationship_item?.edge_id ?? Math.random()}`, columns, dataSource: actions, pagination: { pageSize: 8, hideOnSinglePage: true }, locale: { emptyText: "当前没有需要人工处理的待办" } })),
    React.createElement(Card, { title: "日常工作统计说明", style: CARD_STYLE }, React.createElement(Paragraph, { style: { marginBottom: 0 } }, workSummary?.caveat || "统计只反映台账已有线索、证据、观察与人工操作数量，不等同招商成果、企业事实或人员绩效。")),
  );
}

/**
 * 线索列表页按服务器状态过滤、搜索并打开详情；搜索只在当前已读列表中执行。
 *
 * 输入是线索集合、筛选状态与打开回调，输出为表格。前端筛选不能改变台账状态或产业归类；
 * 所有列均为服务器已有字段，风险展示仍依赖结构化 ``red_alert`` 而非主观事实标签。
 */
function LeadsTab({ leadsData, onOpenLead }: DataRecord): any {
  const [sector, setSector] = React.useState();
  const [reviewState, setReviewState] = React.useState();
  const [keyword, setKeyword] = React.useState("");
  const records = Array.isArray(leadsData?.leads) ? leadsData.leads : [];
  const displayed = records.filter((lead: DataRecord) => (!sector || lead.sector === sector) && (!reviewState || lead.review_state === reviewState) && (!keyword.trim() || String(lead.entity_name || "").toLocaleLowerCase().includes(keyword.trim().toLocaleLowerCase())));
  const columns = [
    { title: "企业/机构", dataIndex: "entity_name", render: (value: unknown, row: DataRecord) => React.createElement(Button, { type: "link", style: { padding: 0, fontWeight: 600 }, onClick: () => onOpenLead(row.lead_id) }, String(value ?? "未命名主体")) },
    { title: "产业", dataIndex: "sector", width: 120, render: (value: unknown) => label(value, SECTOR_LABELS) },
    { title: "人工状态", dataIndex: "review_state", width: 140, render: (value: unknown) => React.createElement(Tag, { color: stateColor(value) }, label(value, REVIEW_LABELS)) },
    { title: "风险", width: 120, render: (_: unknown, row: DataRecord) => row.red_alert ? React.createElement(Badge, { status: "error", text: `待复核 ${row.risk_alert_level ?? "风险"}` }) : React.createElement(AntText, { type: "secondary" }, "无打开预警") },
    { title: "最近更新", dataIndex: "updated_at", width: 170, render: formatDate },
  ];
  return React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Alert, { type: "info", showIcon: true, message: "分类与标签的边界", description: "“待人工分类”不会进入自动评分、行动卡或报告事实章节；徐州关联标签本身也不会单独自动加分。" }),
    React.createElement(Space, { wrap: true },
      React.createElement(Input.Search, { allowClear: true, placeholder: "搜索当前已加载的主体名称", style: { width: 260 }, onChange: (event: any) => setKeyword(event.target.value) }),
      React.createElement(Select, { allowClear: true, placeholder: "全部产业", style: { width: 150 }, value: sector, onChange: setSector, options: Object.entries(SECTOR_LABELS).map(([value, text]) => ({ value, label: text })) }),
      React.createElement(Select, { allowClear: true, placeholder: "全部人工状态", style: { width: 160 }, value: reviewState, onChange: setReviewState, options: Object.entries(REVIEW_LABELS).map(([value, text]) => ({ value, label: text })) }),
      React.createElement(AntText, { type: "secondary" }, `显示 ${displayed.length} / ${records.length} 条`),
    ),
    React.createElement(Card, { style: CARD_STYLE, bodyStyle: { padding: 0 } }, React.createElement(Table, { rowKey: "lead_id", columns, dataSource: displayed, pagination: { pageSize: 10, hideOnSinglePage: true }, scroll: { x: 760 }, locale: { emptyText: "当前筛选条件下没有线索" } })),
  );
}

/**
 * 产业与来源页并列展示不同统计口径、监控状态和细分方向覆盖，不做数值混加。
 *
 * 输入是多个服务器只读报告，输出为表格与原始可审查摘要。营业收入、产值、项目投资额
 * 永远按各自 ``metric_kind`` 和统计范围并列；词命中、覆盖缺口和退避也只是工作线索，
 * 不会被页面转换为企业事实或自动采集命令。
 */
function AnalysisTab({ metrics, sourceCoverage, subdirectionCoverage, monitorSchedule, outcomeSchedule, onScheduleAction, scheduling }: DataRecord): any {
  const series = Array.isArray(metrics?.series) ? metrics.series : [];
  const metricRows = series.flatMap((group: DataRecord) => (Array.isArray(group.records) ? group.records.map((record: DataRecord) => ({ ...record, group_key: `${group.entity_name}-${group.metric_kind}-${record.metric_id}` })) : []));
  const metricColumns = [
    { title: "主体", dataIndex: "entity_name", render: (_: unknown, row: DataRecord) => row.entity_name || "—" },
    { title: "产业", dataIndex: "sector", render: (_: unknown, row: DataRecord) => label(row.sector, SECTOR_LABELS) },
    { title: "口径", dataIndex: "metric_label", render: (_: unknown, row: DataRecord) => row.metric_label || row.metric_kind || "—" },
    { title: "年度", dataIndex: "period_year" },
    { title: "金额（元）", dataIndex: "value_yuan", render: (value: unknown) => typeof value === "number" ? value.toLocaleString("zh-CN") : "—" },
    { title: "同比/同口径变化", dataIndex: "same_metric_change_percent", render: (value: unknown) => value === null || value === undefined ? "—" : `${value}%` },
  ];
  const scheduleCard = (title: string, schedule: DataRecord, kind: string) => React.createElement(Card, { size: "small", title, style: { borderRadius: 10, height: "100%" } }, React.createElement(Space, { direction: "vertical", size: 6 }, React.createElement(Badge, { status: schedule?.enabled ? "success" : "default", text: schedule?.enabled ? "已启用" : "已暂停" }), React.createElement(AntText, { type: "secondary" }, `固定计划：${schedule?.fixed_cron ?? "未配置"}`), React.createElement(AntText, { type: "secondary" }, `下次运行：${formatDate(schedule?.runtime_state?.next_run_at)}`), React.createElement(Button, { size: "small", loading: scheduling === kind, onClick: () => onScheduleAction(kind, schedule?.enabled ? "pause" : "enable") }, schedule?.enabled ? "暂停计划" : "启用计划")));
  return React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Alert, { type: "warning", showIcon: true, message: "三类经营指标只能并列查看", description: metrics?.mixed_metric_rule?.reason || "营业收入、产值与项目投资额统计口径不同，不能相加。" }),
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, lg: 12 }, scheduleCard("公开来源监控", monitorSchedule?.schedule ?? monitorSchedule, "source")),
      React.createElement(Col, { xs: 24, lg: 12 }, scheduleCard("结果学习计划", outcomeSchedule?.schedule ?? outcomeSchedule, "outcome")),
    ),
    React.createElement(Card, { title: `产业经营指标（${metrics?.confirmed_metric_count ?? 0} 条已确认记录）`, style: CARD_STYLE }, React.createElement(Table, { size: "small", rowKey: "group_key", columns: metricColumns, dataSource: metricRows, pagination: { pageSize: 8, hideOnSinglePage: true }, scroll: { x: 900 }, locale: { emptyText: "当前窗口暂无人工确认的经营指标" } })),
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, lg: 12 }, React.createElement(Card, { title: "来源覆盖与访问退避", style: CARD_STYLE }, React.createElement(Paragraph, { style: { marginBottom: 10 } }, "以下为已保存的来源覆盖、失败和退避统计；查看不会触发网页重试或绕过反爬限制。"), React.createElement("pre", { style: { maxHeight: 260, overflow: "auto", margin: 0, whiteSpace: "pre-wrap", fontSize: 12, color: "#334155" } }, JSON.stringify(sourceCoverage?._loadError ? { 提示: sourceCoverage._loadError } : sourceCoverage?.summary ?? sourceCoverage ?? {}, null, 2)))),
      React.createElement(Col, { xs: 24, lg: 12 }, React.createElement(Card, { title: "细分产业方向覆盖", style: CARD_STYLE }, React.createElement(Paragraph, { style: { marginBottom: 10 } }, "词命中只表示台账文本中出现相关术语，仍需人工判断是否真正属于该细分方向。"), React.createElement("pre", { style: { maxHeight: 260, overflow: "auto", margin: 0, whiteSpace: "pre-wrap", fontSize: 12, color: "#334155" } }, JSON.stringify(subdirectionCoverage?._loadError ? { 提示: subdirectionCoverage._loadError } : subdirectionCoverage?.summary ?? subdirectionCoverage ?? {}, null, 2)))),
    ),
  );
}

/**
 * 来源、候选和关系页呈现自动发现后的人工待办，不把候选快照等同正式证据。
 *
 * 输入为服务器返回的候选、可信来源、图谱和关系队列，输出为只读表格。页面不会直接访问
 * 候选 URL、不会抓取网页或新建图谱关系；所有写操作都保留在已有认证接口，证据链不受
 * 该展示组件影响。
 */
function SourcesAndRelationsTab({ candidates, discoveryCandidates, trustedSources, graph, relationshipQueue, onOpenObservation, onOpenCorroboration, onReviewCandidate, onWaiveXuzhou, latestOutcome, onOpenLeads }: DataRecord): any {
  const observations = Array.isArray(candidates?.observations) ? candidates.observations : Array.isArray(candidates?.items) ? candidates.items : [];
  const discovered = Array.isArray(discoveryCandidates?.items) ? discoveryCandidates.items : [];
  // 已经保存过观察快照的候选只在“已保存材料”表出现。旧版同时在两张表渲染它，导致
  // “查看材料”打开同一快照两次，业务人员误以为是两种不同待办。
  const unobservedDiscovered = discovered.filter((row: DataRecord) => !row.latest_observation_id && row.state !== "captured");
  const sources = Array.isArray(trustedSources?.sources) ? trustedSources.sources : [];
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph?.edges) ? graph.edges : [];
  const queue = Array.isArray(relationshipQueue?.items) ? relationshipQueue.items : [];
  const sourceLink = (value: unknown, row: DataRecord, width: number) => React.createElement(
    "a",
    { href: row.source_url, target: "_blank", rel: "noreferrer", title: String(value || row.source_url || "候选页面"), style: { display: "block", maxWidth: width, overflowWrap: "anywhere", wordBreak: "break-word", lineHeight: 1.5 } },
    String(value || row.source_url || "候选页面"),
  );
  const observationColumns = [
    { title: "候选页面", dataIndex: "source_title", width: 320, render: (value: unknown, row: DataRecord) => sourceLink(value, row, 288) },
    { title: "核验优先级", width: 198, render: (_: unknown, row: DataRecord) => {
      const geographicPriority = row.geographic_priority;
      const materialPriority = row.review_priority === "high_priority_review" || row.review_priority === "red_review" ? "材料高优先" : "待补证";
      const geographicLabel = geographicPriority === "high_with_xuzhou"
        ? "已见徐州字段"
        : geographicPriority === "low_without_xuzhou"
          ? "无徐州字段（地域低优先）"
          : geographicPriority === "human_waived_xuzhou"
            ? "人工确认无需徐州字段"
            : "地域待人工决定";
      return React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(Tag, { color: row.review_priority === "high_priority_review" || row.review_priority === "red_review" ? "gold" : "default" }, materialPriority), React.createElement(Tag, { color: geographicPriority === "high_with_xuzhou" ? "green" : geographicPriority === "human_waived_xuzhou" ? "blue" : "default" }, geographicLabel));
    } },
    { title: "采集时间", dataIndex: "collected_at", width: 174, render: formatDate },
    { title: "当前状态", dataIndex: "state", width: 158, render: (value: unknown) => React.createElement(Tag, { color: stateColor(value) }, label(value, { ...REVIEW_LABELS, pending_evidence_verification: "待核验证据" })) },
    { title: "下一步", width: 130, fixed: "right", render: (_: unknown, row: DataRecord) => React.createElement(Button, { type: "primary", size: "small", onClick: () => onOpenObservation(row.observation_id ?? row.candidate_id) }, "查看材料") },
  ];
  const discoveryColumns = [
    { title: "候选企业/页面", dataIndex: "source_title", width: 286, render: (value: unknown, row: DataRecord) => sourceLink(value, row, 254) },
    { title: "产业", dataIndex: "sector", width: 96, render: (value: unknown) => label(value, SECTOR_LABELS) },
    { title: "系统发现摘要", dataIndex: "search_snippet", width: 250, render: (value: unknown) => React.createElement(CollapsibleText, { value, collapsedLength: 150, emptyText: "服务器未保存可展示摘要" }) },
    { title: "服务器保存进度", width: 230, render: (_: unknown, row: DataRecord) => row.state === "capture_failed" ? React.createElement(Space, { direction: "vertical", size: 3 }, React.createElement(Tag, { color: "volcano" }, "上次重新核验未通过"), React.createElement(AntText, { type: "secondary", style: { fontSize: 12, overflowWrap: "anywhere" } }, row.last_capture_error || "服务器未能形成可入证材料；请查看失败原因。")) : React.createElement(Space, { direction: "vertical", size: 3 }, React.createElement(Tag, { color: "blue" }, "仅保存链接、标题和摘要"), React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, "尚无服务器保存的页面副本、采集时间与哈希。")) },
    { title: "还缺什么", width: 140, render: (_: unknown, row: DataRecord) => row.requires_china_business_verification ? React.createElement(Tag, { color: "gold" }, "需人工判断中国/徐州依据") : React.createElement(Tag, null, "等待服务器保存页面材料") },
    { title: "第二来源", width: 172, render: (_: unknown, row: DataRecord) => row.latest_corroboration_outcome === "official_site_link_candidate" && row.latest_corroboration_url ? React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(Tag, { color: "cyan" }, "官网链接候选"), React.createElement(Button, { size: "small", onClick: () => onOpenCorroboration(row.candidate_id) }, "查看补证材料")) : row.latest_corroboration_outcome === "no_independent_official_site_link" ? React.createElement(AntText, { type: "secondary", style: { overflowWrap: "anywhere" } }, "原页未找到明确跨域官网链接") : row.latest_corroboration_outcome === "robots_disallow" ? React.createElement(AntText, { type: "secondary" }, "该来源不允许采集") : row.latest_corroboration_outcome === "corroboration_failed" ? React.createElement(AntText, { type: "secondary", style: { overflowWrap: "anywhere" } }, String(row.latest_corroboration_error || "第二来源补证未完成")) : React.createElement(AntText, { type: "secondary" }, "等待低频补证") },
    { title: "当前判断", dataIndex: "business_review_state", width: 124, render: (value: unknown) => React.createElement(Tag, { color: stateColor(value) }, label(value, REVIEW_LABELS)) },
    { title: "下一步", width: 190, fixed: "right", render: (_: unknown, row: DataRecord) => React.createElement(Space, { direction: "vertical", size: 4 }, React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, "尚无保存材料，不能入证"), React.createElement(Button, { size: "small", onClick: () => onReviewCandidate(row.candidate_id) }, "做业务判断"), row.requires_china_business_verification ? React.createElement(Button, { size: "small", type: "primary", onClick: () => onWaiveXuzhou(row) }, "无需徐州字段") : null) },
  ];
  const sourceColumns = [
    { title: "域名", dataIndex: "domain" },
    { title: "来源类型", dataIndex: "source_type" },
    { title: "来源等级", dataIndex: "source_level" },
    { title: "状态", dataIndex: "status", render: (value: unknown) => React.createElement(Tag, { color: value === "active" ? "green" : "default" }, String(value ?? "—")) },
  ];
  const edgeColumns = [
    { title: "关系边", dataIndex: "relationship_type", render: (value: unknown) => String(value ?? "关系") },
    { title: "状态", dataIndex: "review_state", render: (value: unknown) => React.createElement(Tag, { color: stateColor(value) }, label(value, REVIEW_LABELS)) },
    { title: "证据引用", dataIndex: "evidence_ids", render: (value: unknown) => Array.isArray(value) ? `${value.length} 条` : "—" },
  ];
  return React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(CandidateWorkflowGuide, { latestOutcome, onOpenLeads }),
    React.createElement(Card, { title: `已发现公开网页链接，尚未形成服务器留存材料（${unobservedDiscovered.length}）`, extra: React.createElement(AntText, { type: "secondary" }, "这里不要求你另找文件；服务器尚未保存可共同核对的页面快照"), style: CARD_STYLE }, React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } }, React.createElement(Alert, { type: "info", showIcon: true, message: "“尚未保存”具体缺少什么？", description: "当前台账只有公开网页链接、标题和搜索摘要；尚未保存该网页的服务器页面副本、可见文本、采集时间和内容哈希。原网页可能会变化或消失，所以直接在浏览器看到它，不能替代服务器留存材料。通常无需业务人员操作，等待固定计划访问；若已显示失败原因，请先阅读原因，再决定待补证、观察或无效。" }), React.createElement(Table, { size: "small", tableLayout: "fixed", rowKey: "candidate_id", columns: discoveryColumns, dataSource: unobservedDiscovered, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 1420 }, locale: { emptyText: "暂无仅有链接、尚未保存页面材料的候选" } }))),
    React.createElement(Card, { title: `已保存材料，等待人工判断（${observations.length}）`, extra: React.createElement(AntText, { type: "secondary" }, "先看快照；材料优先级与地域优先级分别显示，二者都不等于入证"), style: CARD_STYLE }, React.createElement(Table, { size: "small", tableLayout: "fixed", rowKey: (row: DataRecord) => row.observation_id ?? row.candidate_id, columns: observationColumns, dataSource: observations, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 980 }, locale: { emptyText: "暂无已保存、待判断的材料" } })),
    React.createElement(Card, { title: `可信来源目录（${sources.length}）`, style: CARD_STYLE }, React.createElement(Table, { size: "small", rowKey: "source_id", columns: sourceColumns, dataSource: sources, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 700 }, locale: { emptyText: "暂无可信来源配置" } })),
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, lg: 12 }, React.createElement(Card, { title: `关系图谱摘要：${nodes.length} 个节点 / ${edges.length} 条边`, style: CARD_STYLE }, React.createElement(Table, { size: "small", rowKey: "edge_id", columns: edgeColumns, dataSource: edges, pagination: { pageSize: 5, hideOnSinglePage: true }, locale: { emptyText: "暂无已登记的证据化关系边" } }))),
      React.createElement(Col, { xs: 24, lg: 12 }, React.createElement(Card, { title: `关系待复核（${queue.length}）`, style: CARD_STYLE }, React.createElement(List, { size: "small", dataSource: queue, locale: { emptyText: "暂无关系待办" }, renderItem: (item: DataRecord) => React.createElement(List.Item, null, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, item.priority || "关系复核"), React.createElement(AntText, { type: "secondary" }, item.reason || item.instruction || "请依据已引用证据人工复核。"))) }))),
    ),
  );
}

/**
 * 从不同后端只读响应中提取列表，不把缺失响应伪造成空的业务结论。
 *
 * 输入是响应对象和按接口约定的候选字段名，输出为其中第一个数组字段或空数组。该函数仅
 * 适配展示层版本差异，不写入台账、不会将加载失败解释为“没有待办”，调用方仍会看到顶部
 * 的读取错误提示。
 */
function listField(payload: DataRecord | null | undefined, keys: string[]): DataRecord[] {
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload?.[key] as DataRecord[];
  }
  return [];
}

/**
 * 将逗号或换行分隔的既有证据 ID 转成去重列表，禁止由页面构造来源事实。
 *
 * 输入是人工填写的服务器证据 ID 文本，输出为去重后的有限 ID 数组。它不验证、创建或修改
 * 证据；真正的归属和完整性验证只在后端完成，空项仍会交给后端返回明确校验错误。
 */
function parseEvidenceIds(value: string): string[] {
  return Array.from(new Set(value.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean)));
}

/**
 * 展示自动监控候选的可读快照及服务器生成的证据草稿。
 *
 * 输入是脱敏后的候选详情和受限确认/业务复核回调，输出为只读抽屉。页面只显示服务器保存
 * 的可展示文本、哈希和草稿状态，绝不接收或编辑原始 URL、摘录、采集时间、类别或操作人；
 * “确认入证”只会打开备注表单，后端会重新抓取并校验快照后才可能新增正式 evidence。
 */
function ObservationDrawer({ open, onClose, observation, onConfirmDraft, onReviewDraft, onReviewCandidate, onWaiveXuzhou }: DataRecord): any {
  const snapshot = observation?.snapshot ?? {};
  const drafts = listField(observation, ["evidence_drafts", "drafts"]);
  const candidateId = observation?.candidate?.candidate_id ?? observation?.observation?.candidate_id;
  const candidateReviewState = observation?.candidate?.business_review_state ?? observation?.observation?.business_review_state;
  const requiresChinaBusinessVerification = observation?.candidate?.requires_china_business_verification === true;
  const geographicScope = observation?.candidate?.geographic_scope ?? {};
  const canWaiveXuzhou = geographicScope?.scope_status === "legacy_scope_unresolved";
  const lastCaptureError = typeof observation?.candidate?.last_capture_error === "string" ? observation.candidate.last_capture_error : "";
  const lastCaptureAt = observation?.candidate?.last_captured_at;
  return React.createElement(
    Drawer,
    { open, onClose, width: "min(820px, 94vw)", title: observation?.candidate?.source_title || observation?.observation?.source_title || "候选快照详情", destroyOnClose: false },
    !observation
      ? React.createElement(Spin, { style: { display: "block", margin: "60px auto" } })
      : React.createElement(
          Space,
          { direction: "vertical", size: 16, style: { width: "100%" } },
          React.createElement(Alert, { type: "warning", showIcon: true, message: "你现在看到的是“已保存页面材料”，还不是正式证据", description: "服务器已经保存了当时网页的可见文本、原始内容校验哈希和采集时间，因此团队可以共同核对。但网页快照只说明系统当时看到了什么；它不自动证明企业、融资、合作、徐州关联或招商意愿。完整保存文本在本页底部。" }),
          candidateId ? React.createElement(Alert, { type: "info", showIcon: true, message: `候选处理状态：${label(candidateReviewState, REVIEW_LABELS)}`, description: "“待补证”表示有价值但依据不够；“观察”表示暂不下结论、保留低频关注；“无效”表示停止自动访问。你作出的选择会记录你的登录身份和理由。", action: React.createElement(Space, { size: 6 }, React.createElement(Button, { size: "small", onClick: () => onReviewCandidate(candidateId) }, "选择处理方式"), canWaiveXuzhou ? React.createElement(Button, { size: "small", type: "primary", onClick: () => onWaiveXuzhou(observation?.candidate) }, "无需徐州字段") : null) }) : null,
          candidateId ? React.createElement(Alert, { type: geographicScope?.geographic_priority === "high_with_xuzhou" ? "success" : "warning", showIcon: true, message: geographicScope?.geographic_priority === "high_with_xuzhou" ? "地域优先级：已见徐州字段（高优先）" : geographicScope?.scope_status === "xuzhou_waived_by_human_pending_server_recapture" ? "地域判断：已记录无需徐州字段，待服务器重新核验" : "地域判断：尚待人工决定", description: geographicScope?.scope_note || "系统不会从媒体域名推测企业所在地。请依据保存材料人工判断中国业务和徐州关联。" }) : null,
          React.createElement(Descriptions, { bordered: true, size: "small", column: 2, items: [
            { key: "source", label: "候选来源", children: React.createElement("a", { href: snapshot.source_url || observation?.candidate?.source_url, target: "_blank", rel: "noreferrer" }, snapshot.title || observation?.candidate?.source_title || "打开来源") },
            { key: "collected", label: "采集时间", children: formatDate(snapshot.collected_at || observation?.observation?.collected_at) },
            { key: "hash", label: "内容哈希", children: snapshot.raw_sha256 || "—" },
            { key: "bytes", label: "页面大小", children: snapshot.byte_size ?? "—" },
            { key: "origin", label: "企业来源地（人工登记）", children: geographicScope?.enterprise_origin?.origin ? `${geographicScope.enterprise_origin.origin}（${geographicScope.enterprise_origin.origin_kind === "headquarters" ? "总部" : geographicScope.enterprise_origin.origin_kind === "operation_site" ? "经营地" : geographicScope.enterprise_origin.origin_kind === "registered_location" ? "注册地" : "类型待明确"}；尚非正式证据）` : "尚未登记" },
          ] }),
          React.createElement(Divider, { orientation: "left" }, `关联证据草稿（${drafts.length} 条）`),
          React.createElement(Alert, { type: "info", showIcon: true, message: "草稿只是系统从保存页面中预填的“核对清单”", description: "先核对下方的拟定主体、类型、页面日期、事实类别和原始摘录；它们都不是已确认事实。确认提交后，服务器会重新访问同一原页面，并要求这些字段仍能在当前页面中找到。任何一项不一致，系统不会新增正式证据。" }),
          drafts.length
            ? drafts.map((draft: DataRecord) => {
                const confirmationState = typeof draft.confirmation_state === "string" && draft.confirmation_state ? draft.confirmation_state : (draft.state || "pending_human_confirmation");
                const businessReviewState = draft.business_review_state;
                const awaitingConfirmation = ["pending_human_confirmation", "pending_user_review", "pending_confirmation", "pending"].includes(confirmationState);
                const rawRequirements = Array.isArray(draft.needs_human_confirmation) ? draft.needs_human_confirmation : [];
                const missingFields = rawRequirements.filter((item: unknown) => typeof item === "string" && item !== "重新受控抓取并校验来源当前正文、来源等级和正式证据字段") as string[];
                const hasMissingFields = missingFields.length > 0;
                const canConfirm = awaitingConfirmation && businessReviewState !== "invalid" && !hasMissingFields && !requiresChinaBusinessVerification;
                const confirmationHistory = Array.isArray(draft.confirmation_history) ? draft.confirmation_history : [];
                const latestFailedConfirmation = confirmationHistory.find((item: DataRecord) => item?.new_state === "confirmation_not_captured" || item?.new_state === "confirmation_rejected_incomplete_draft");
                const failureReason = typeof latestFailedConfirmation?.verification_failure_reason === "string" ? latestFailedConfirmation.verification_failure_reason : lastCaptureError;
                const failureStage = typeof latestFailedConfirmation?.verification_failure_stage === "string" ? latestFailedConfirmation.verification_failure_stage : "";
                const hasFailureReason = Boolean(failureReason);
                const dateMismatch = failureStage === "source_date_not_found_in_current_page" || failureReason.startsWith("source_published_at");
                const draftTitle = hasMissingFields ? "字段不完整的系统草稿（尚不能提交）" : "系统提取的待核验草稿（尚未入证）";
                const chinaScopeBlockerText = requiresChinaBusinessVerification ? "此外，这条候选仍缺中国业务、徐州关联或投资落地的公开依据。" : "";
                return React.createElement(
                  Card,
                  { key: draft.draft_id ?? draft.evidence_draft_id, size: "small", style: { marginBottom: 8 } },
                  React.createElement(
                    Space,
                    { direction: "vertical", size: 8, style: { width: "100%" } },
                    React.createElement(AntText, { strong: true }, draftTitle),
                    React.createElement(Space, { wrap: true, size: 6 }, React.createElement(Tag, { color: awaitingConfirmation ? "blue" : "default" }, `人工确认：${label(confirmationState, DRAFT_CONFIRMATION_LABELS)}`), businessReviewState ? React.createElement(Tag, { color: stateColor(businessReviewState) }, `业务判断：${label(businessReviewState, REVIEW_LABELS)}`) : null),
                    React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, `草稿 ID：${draft.draft_id ?? draft.evidence_draft_id ?? "—"}`),
                    React.createElement(Descriptions, { bordered: true, size: "small", column: 2, items: [
                      { key: "entity", label: "拟定主体", children: draft.entity_name || "系统未能可靠提取，不能入证" },
                      { key: "kind", label: "拟定主体类型", children: draft.entity_kind === "enterprise" ? "企业" : draft.entity_kind === "fund" ? "基金" : draft.entity_kind === "startup_team" ? "创业团队" : "系统未能可靠提取，不能入证" },
                      { key: "date", label: "拟定页面日期", children: draft.source_published_at || "系统未找到可核对日期，不能入证" },
                      { key: "claim", label: "拟定事实类别", children: label(draft.claim_type, CLAIM_LABELS) },
                    ] }),
                    hasFailureReason ? React.createElement(Alert, { type: "warning", showIcon: true, message: dateMismatch ? "上次核验：当前网页没有找到草稿中的页面日期" : "上次核验未通过：请查看具体原因", description: dateMismatch ? `服务器已在 ${formatDate(lastCaptureAt || latestFailedConfirmation?.attempted_at)} 重新访问原网页，但在当前可见文本中找不到草稿日期“${draft.source_published_at || "—"}”。系统不会把采集日期代替页面日期，因此没有新增正式证据。原网页可能已变化，或自动草稿把页面中的其他日期误当成页面日期。不要在浏览器改写日期；请选择“暂不入证，作业务判断”，或仅在原网页已有新的明确公开内容后再次请求核验。` : `服务器已执行重新抓取，但未通过：${failureReason}。未新增正式证据。请阅读保存材料后选择待补证、观察或无效；只有当前原网页确有新公开内容时，再次请求核验才有意义。` }) : null,
                    React.createElement(CollapsibleText, { value: draft.excerpt || draft.matched_excerpt, collapsedLength: 420, emptyText: "草稿事实由服务器保存，请回看候选快照。" }),
                    canConfirm
                      ? React.createElement(React.Fragment, null, React.createElement(Alert, { type: "info", showIcon: true, message: "下一步：由服务器重新访问同一原网页", description: "你提交的是“请服务器核对”的请求，不是直接放行。服务器将依次检查：能否访问页面、拟定主体是否仍存在、拟定页面日期是否仍存在、原始摘录是否仍一致。全部通过后才追加正式证据。" }), React.createElement(Space, { wrap: true }, React.createElement(Button, { type: "primary", size: "small", onClick: () => onConfirmDraft(draft) }, hasFailureReason ? "原网页有更新后，再次请求核验" : "请求重新核验并入证"), React.createElement(Button, { size: "small", onClick: () => onReviewDraft(draft) }, "暂不入证，作业务判断")))
                      : businessReviewState === "invalid"
                        ? React.createElement(Alert, { type: "warning", showIcon: true, message: "该草稿已标记无效，不能确认入证", description: "系统不会重新访问原页面；历史材料和判断理由仍保留在审计记录中。" })
                        : hasMissingFields
                          ? React.createElement(React.Fragment, null, React.createElement(Alert, { type: "warning", showIcon: true, message: `暂不能重新核验：草稿缺少${missingFields.join("、")}`, description: `服务器尚未访问原页面。不能由用户在页面上补写这些事实字段；请依据当前材料选择“待补证、观察或无效”，等待后续合规公开材料。${chinaScopeBlockerText}` }), React.createElement(Button, { size: "small", onClick: () => onReviewDraft(draft) }, "暂不入证，作业务判断"))
                          : requiresChinaBusinessVerification
                            ? React.createElement(React.Fragment, null, React.createElement(Alert, { type: "warning", showIcon: true, message: "暂不能重新核验：地域依据仍待人工决定", description: "服务器尚未访问原页面。若你已能确认中国业务且这条高价值材料无需徐州字段，请点击上方“无需徐州字段”，登记企业来源地和核验依据；保存后仍必须由服务器重新抓取原页面。" }), canWaiveXuzhou ? React.createElement(Button, { size: "small", type: "primary", onClick: () => onWaiveXuzhou(observation?.candidate) }, "无需徐州字段，登记后重新核验") : null)
                            : React.createElement(Alert, { type: "info", showIcon: true, message: "该草稿已完成确认流程", description: "历史业务判断和原始材料仍会保留在审计记录中。" }),
                  ),
                );
              })
            : React.createElement(Empty, { description: "该候选当前没有待人工处理的证据草稿" }),
          React.createElement(Divider, { orientation: "left" }, "完整保存页面文本（只读）"),
          React.createElement(Card, { title: "需要核对全文时再展开", size: "small" }, React.createElement(CollapsibleText, { value: snapshot.visible_text, collapsedLength: 650 })),
        ),
  );
}

/**
 * 展示已保存的第二来源补证快照，明确其只是官网链接候选而非事实确认。
 *
 * 输入是服务器验证后的补证详情与关闭回调，输出为只读抽屉。页面只显示链接、采集时间、
 * 哈希和有限正文，不能输入 URL、实体、业务状态、证据字段或操作人；因此用户可审查系统
 * 当时抓到的第二页面，又无法从浏览器修改原候选、原始快照、正式 evidence 或招商状态。
 */
function CorroborationDrawer({ open, onClose, details }: DataRecord): any {
  const corroboration = details?.corroboration ?? {};
  const snapshot = details?.snapshot ?? {};
  return React.createElement(
    Drawer,
    { open, onClose, width: "min(820px, 94vw)", title: corroboration.source_title || "第二来源补证快照", destroyOnClose: false },
    !details
      ? React.createElement(Spin, { style: { display: "block", margin: "60px auto" } })
      : React.createElement(
          Space,
          { direction: "vertical", size: 16, style: { width: "100%" } },
          React.createElement(Alert, { type: "warning", showIcon: true, message: "第二来源仍待人工核验", description: details.caveat || "该页面只因候选原页明确标为官网的跨域链接被抓取，不自动确认官网归属、中国业务、徐州关联、融资事实或招商意愿。" }),
          React.createElement(Descriptions, { bordered: true, size: "small", column: 2, items: [
            { key: "source", label: "第二来源", children: React.createElement("a", { href: snapshot.source_url || corroboration.source_url, target: "_blank", rel: "noreferrer" }, snapshot.source_title || corroboration.source_title || "打开来源") },
            { key: "collected", label: "采集时间", children: formatDate(snapshot.collected_at || corroboration.attempted_at) },
            { key: "hash", label: "内容哈希", children: snapshot.raw_sha256 || corroboration.raw_sha256 || "—" },
            { key: "bytes", label: "页面大小", children: snapshot.byte_size ?? corroboration.byte_size ?? "—" },
          ] }),
          React.createElement(Card, { title: "保存的页面文本（只读）", size: "small" }, React.createElement(CollapsibleText, { value: snapshot.visible_text, collapsedLength: 1500 })),
        ),
  );
}

/**
 * 提供所有“决定 + 备注”类业务操作的统一受限表单。
 *
 * 输入是服务器既有对象、固定状态枚举和提交回调，输出为业务复核模态框。浏览器只提交
 * ``decision`` 与 ``review_note``，没有用户 ID、证据正文、URL、摘录、金额或评分输入；
 * 操作者、对象有效性和追加审计均由后端决定，失败不会把前端选择当作已经保存。
 */
function TaskReviewModal({ task, onClose, submitting }: DataRecord): any {
  const [decision, setDecision] = React.useState("");
  const [note, setNote] = React.useState("");
  const selectedOption = (task?.options ?? []).find((option: DataRecord) => option.value === decision);
  React.useEffect(() => {
    setDecision(task?.options?.[0]?.value ?? "");
    setNote("");
  }, [task?.key]);
  return React.createElement(
    Modal,
    { open: !!task, zIndex: 1200, title: task?.title || "业务复核", okText: task?.okText || "保存", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !decision || !note.trim() }, onCancel: onClose, onOk: () => task?.submit(decision, note.trim()) },
    React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 16 }, message: task?.notice || "本操作只追加业务决定和审计", description: task?.description || "操作人由服务器当前登录身份确定；原始证据链不在此表单中出现，也不能修改。" }),
    React.createElement(AntText, { strong: true }, "业务决定"),
    React.createElement(Select, { value: decision, style: { width: "100%", marginTop: 8, marginBottom: 16 }, options: task?.options ?? [], onChange: setDecision }),
    selectedOption?.help ? React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 16 }, message: `你选择的是：${selectedOption.label}`, description: selectedOption.help }) : null,
    React.createElement(AntText, { strong: true }, task?.noteLabel || "判断理由（必填）"),
    React.createElement(AntText, { type: "secondary", style: { display: "block", marginTop: 4, fontSize: 12 } }, "保存后会记录到审计中。请写明你在材料中看到了什么，以及为什么选择该决定；不需要、也不能粘贴或修改原始网页。"),
    React.createElement(Input.TextArea, { value: note, maxLength: 2000, showCount: true, autoSize: { minRows: 4, maxRows: 8 }, style: { marginTop: 8 }, placeholder: task?.notePlaceholder || (decision === "confirm" ? "示例：已核对页面中的主体、事实表述和日期，认为草稿摘录与页面一致，请服务器重新核验。" : "示例：页面仅介绍产品和融资，未见中国业务或徐州落地依据，因此先标记为待补证。"), onChange: (event: any) => setNote(event.target.value) }),
    !note.trim() ? React.createElement(AntText, { type: "warning", style: { display: "block", marginTop: 8, fontSize: 12 } }, "填写判断理由后，“保存”按钮才会可用。") : null,
  );
}

/**
 * 单独承载“无需徐州字段”的高风险业务判断，避免把企业来源地塞进通用备注框。
 * 此表单只产生一条人工地域审计；提交后仍需由用户请求服务器重新抓取，不能直接入证。
 */
function XuzhouScopeWaiverModal({ candidate, onClose, onSubmit, submitting }: DataRecord): any {
  const [origin, setOrigin] = React.useState("");
  const [originKind, setOriginKind] = React.useState("registered_location");
  const [basis, setBasis] = React.useState("");
  const [note, setNote] = React.useState("");
  React.useEffect(() => {
    setOrigin("");
    setOriginKind("registered_location");
    setBasis("");
    setNote("");
  }, [candidate?.candidate_id]);
  const valid = !!origin.trim() && !!basis.trim() && !!note.trim();
  return React.createElement(
    Modal,
    {
      open: !!candidate,
      zIndex: 1250,
      title: candidate ? "无需徐州字段：人工地域判断" : "人工地域判断",
      okText: "保存判断，等待重新核验",
      cancelText: "取消",
      confirmLoading: submitting,
      okButtonProps: { disabled: !valid },
      onCancel: onClose,
      onOk: () => onSubmit(candidate.candidate_id, {
        decision: "waive_xuzhou",
        review_note: note.trim(),
        enterprise_origin: {
          origin: origin.trim(),
          origin_kind: originKind,
          evidence_basis: basis.trim(),
          china_business_confirmed: true,
        },
      }),
    },
    React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 16 }, message: "这不是直接入证，也不是跳过中国业务", description: "你正在记录：该材料经人工确认具有中国业务依据，但本条不以徐州字段作为前提。保存后，仍须回到材料页点击“请求重新核验并入证”；服务器会重新访问原页面，任一核验失败都不会新增正式证据。" }),
    React.createElement(AntText, { strong: true }, "企业来源地（例：北京）"),
    React.createElement(Input, { value: origin, maxLength: 100, style: { marginTop: 8, marginBottom: 12 }, placeholder: "请填写材料中核对到的所在地，不要填写媒体网站所在地", onChange: (event: any) => setOrigin(event.target.value) }),
    React.createElement(AntText, { strong: true }, "来源地性质"),
    React.createElement(Select, { value: originKind, style: { width: "100%", marginTop: 8, marginBottom: 12 }, onChange: setOriginKind, options: [
      { value: "registered_location", label: "注册地" },
      { value: "headquarters", label: "总部所在地" },
      { value: "operation_site", label: "主要经营地" },
      { value: "unknown", label: "性质暂不明确（仍须说明依据）" },
    ] }),
    React.createElement(AntText, { strong: true }, "中国业务人工核验依据（必填）"),
    React.createElement(Input.TextArea, { value: basis, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, style: { marginTop: 8, marginBottom: 12 }, placeholder: "例如：已阅读保存页面中的工商/官网信息，明确写明企业注册于北京市……", onChange: (event: any) => setBasis(event.target.value) }),
    React.createElement(AntText, { strong: true }, "本次业务判断理由（必填）"),
    React.createElement(Input.TextArea, { value: note, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, style: { marginTop: 8 }, placeholder: "说明为何虽无徐州字段，仍认为该材料值得进入服务器重新核验。", onChange: (event: any) => setNote(event.target.value) }),
  );
}

/**
 * 让普通成员登记新的可信公开来源，字段受固定目录模型限制。
 *
 * 输入是关闭/提交回调，输出为来源配置表单。该表单没有来源 ID、状态审计、用户 ID、证据
 * 内容或抓取参数；服务端会校验域名、类型、等级和允许事实类别，并将配置变更追加审计，
 * 不会追溯修改历史 evidence 的 URL、摘录、日期或评级。
 */
function TrustedSourceModal({ open, onClose, onSubmit, submitting }: DataRecord): any {
  const [domain, setDomain] = React.useState("");
  const [sourceType, setSourceType] = React.useState("enterprise_official");
  const [sourceLevel, setSourceLevel] = React.useState("primary");
  const [quality, setQuality] = React.useState("5");
  const [claims, setClaims] = React.useState(["financing"] as string[]);
  const [reason, setReason] = React.useState("");
  const reset = () => { setDomain(""); setSourceType("enterprise_official"); setSourceLevel("primary"); setQuality("5"); setClaims(["financing"]); setReason(""); };
  const sourceRules: Record<string, DataRecord> = {
    enterprise_official: { level: "primary", qualities: [4, 5], claims: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"] },
    fund_official: { level: "primary", qualities: [4, 5], claims: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"] },
    government_investment_portal: { level: "primary", qualities: [4, 5], claims: ["xuzhou_connection", "cooperation", "expansion", "industry_focus"] },
    official_statistics: { level: "primary", qualities: [4, 5], claims: ["expansion", "industry_focus"] },
    university_alumni: { level: "primary", qualities: [4], claims: ["xuzhou_connection", "cooperation", "industry_focus"] },
    industry_association: { level: "secondary", qualities: [2, 3], claims: ["cooperation", "expansion", "industry_focus", "fund_investment", "risk"] },
    verified_media: { level: "secondary", qualities: [2, 3], claims: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"] },
  };
  const currentRule = sourceRules[sourceType];
  const sourceTypes = Object.keys(sourceRules).map((value) => ({ value, label: value }));
  const claimOptions = currentRule.claims.map((value: string) => ({ value, label: CLAIM_LABELS[value] ?? value }));
  const changeSourceType = (nextType: string) => { const nextRule = sourceRules[nextType]; setSourceType(nextType); setSourceLevel(nextRule.level); setQuality(String(nextRule.qualities[0])); setClaims([nextRule.claims[0]]); };
  return React.createElement(
    Modal,
    { open, onCancel: () => { reset(); onClose(); }, title: "登记可信公开来源", okText: "登记并留存审计", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !domain.trim() || !reason.trim() || !claims.length || !Number(quality) }, onOk: () => onSubmit({ domain: domain.trim(), source_type: sourceType, source_level: sourceLevel, evidence_quality: Number(quality), allowed_claim_types: claims, change_reason: reason.trim() }, reset) },
    React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 14 }, message: "仅登记可公开访问的域名", description: "来源目录影响未来材料的可信评级，不回写任何既有证据；后端会拒绝不匹配的类型、等级和事实类别。" }),
    React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } },
      React.createElement(Input, { value: domain, maxLength: 253, placeholder: "例如：example.gov.cn（不填路径）", onChange: (event: any) => setDomain(event.target.value) }),
      React.createElement(Select, { value: sourceType, options: sourceTypes, onChange: changeSourceType, style: { width: "100%" }, placeholder: "来源类型" }),
      React.createElement(Select, { value: sourceLevel, options: [{ value: currentRule.level, label: currentRule.level === "primary" ? "一手" : "二手" }], onChange: setSourceLevel, style: { width: "100%" }, placeholder: "来源等级" }),
      React.createElement(Select, { value: quality, options: currentRule.qualities.map((value: number) => ({ value: String(value), label: `${value} 级` })), onChange: setQuality, style: { width: "100%" }, placeholder: "证据质量" }),
      React.createElement(Select, { value: claims, mode: "multiple", options: claimOptions, onChange: setClaims, style: { width: "100%" }, placeholder: "允许的事实类别" }),
      React.createElement(Input.TextArea, { value: reason, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, onChange: (event: any) => setReason(event.target.value), placeholder: "登记理由与人工核验依据" }),
    ),
  );
}

/**
 * 让业务成员补充一个服务器可直接读取的公开网页入口。
 *
 * 这个入口不接收 Cookie、账号、密钥、抓取深度或请求头。点击提交时，服务器会重新检查
 * URL 公网边界、robots、同域重定向及页面可读性；只有通过后才登记为低频发现入口，且
 * 后续仍只产生待人工核验候选，不会自动入证或联系企业。
 */
function PublicSourceSupplementModal({ open, onClose, onSubmit, submitting }: DataRecord): any {
  const [sourceUrl, setSourceUrl] = React.useState("");
  const [note, setNote] = React.useState("");
  const reset = () => { setSourceUrl(""); setNote(""); };
  React.useEffect(() => { if (open) reset(); }, [open]);
  return React.createElement(
    Modal,
    {
      open,
      onCancel: () => { reset(); onClose(); },
      title: "检测并加入公开网页来源",
      okText: "检测并加入",
      cancelText: "取消",
      confirmLoading: submitting,
      okButtonProps: { disabled: !sourceUrl.trim() },
      onOk: () => onSubmit({ source_url: sourceUrl.trim(), note: note.trim() || null }, reset),
    },
    React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 14 }, message: "仅填写无需账号或密钥的公开网页完整网址", description: "服务器会先检查网址是否安全、robots 是否允许、页面是否需要登录及是否能直接读取。通过只表示可作为低频发现入口；机器仍只能生成待人工核验候选，不能自动新增正式证据、招商结论或对外联系。" }),
    React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 14 }, message: "付费 API、账号、Cookie、Token 或带参数的临时链接不能填在这里", description: "这类数据源必须由开发者在后端源码和密钥配置中，按供应商许可接入。页面不会保存或传递你的账号、Cookie 或 API Key。" }),
    React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } },
      React.createElement(Input, { value: sourceUrl, maxLength: 2000, placeholder: "例如：https://example.gov.cn/news", onChange: (event: any) => setSourceUrl(event.target.value) }),
      React.createElement(Input.TextArea, { value: note, maxLength: 1000, showCount: true, autoSize: { minRows: 3, maxRows: 5 }, placeholder: "为什么补充这个公开来源（可选）", onChange: (event: any) => setNote(event.target.value) }),
    ),
  );
}

/**
 * 以当前版本为底稿让用户创建新的评分规则版本，不允许覆盖历史版本或证据。
 *
 * 输入是服务器读取的生效规则和提交回调，输出为受限 JSON 表单。页面只发送维度、降权规则
 * 与变更原因；版本号、创建人和全部历史快照由服务器生成/保留。解析失败不会发送请求，
 * 后端还会逐字段验证权重结构，自动学习建议不会因此直接改权重。
 */
function ScoringRulesModal({ ruleSet, open, onClose, onSubmit, submitting }: DataRecord): any {
  const [ruleJson, setRuleJson] = React.useState("");
  const [reason, setReason] = React.useState("");
  React.useEffect(() => {
    if (open) {
      setRuleJson(JSON.stringify({ dimensions: ruleSet?.dimensions ?? [], penalty_rules: ruleSet?.penalty_rules ?? [] }, null, 2));
      setReason("");
    }
  }, [open, ruleSet]);
  const submit = () => {
    try {
      const parsed = JSON.parse(ruleJson);
      onSubmit({ dimensions: parsed?.dimensions, penalty_rules: parsed?.penalty_rules, change_reason: reason.trim() });
    } catch {
      message.error("规则 JSON 格式无效，请检查逗号、引号和括号。");
    }
  };
  return React.createElement(Modal, { open, onCancel: onClose, width: "min(880px, 94vw)", title: "创建新的评分规则版本", okText: "创建新版本", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !ruleJson.trim() || !reason.trim() }, onOk: submit }, React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 14 }, message: "规则修改不是自动学习，也不重算历史", description: "请人工结合结果建议调整。提交后后端只新增版本；既有评分快照、结果反馈、线索状态与原始证据均不可覆盖。" }), React.createElement(Input.TextArea, { value: ruleJson, onChange: (event: any) => setRuleJson(event.target.value), autoSize: { minRows: 14, maxRows: 26 }, style: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12 }, placeholder: "{\n  \"dimensions\": [],\n  \"penalty_rules\": []\n}" }), React.createElement(Input.TextArea, { value: reason, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 5 }, style: { marginTop: 12 }, onChange: (event: any) => setReason(event.target.value), placeholder: "说明本次人工调整原因" }));
}

/**
 * 管理员维护共享台账名册的唯一前端入口，普通用户只能看到服务器拒绝提示。
 *
 * 输入是当前名册响应和提交回调，输出为用户名表单。前端永不接收 user_id 或角色；管理员
 * 限制、用户名到用户 ID 的解析和授权审计全部在服务器完成，移除成员也不会删除其业务审计。
 */
function TeamMemberModal({ open, onClose, onSubmit, submitting }: DataRecord): any {
  const [username, setUsername] = React.useState("");
  const [action, setAction] = React.useState("add");
  React.useEffect(() => { if (open) { setUsername(""); setAction("add"); } }, [open]);
  return React.createElement(Modal, { open, onCancel: onClose, title: "管理员维护团队名册", okText: action === "add" ? "加入团队" : "移出团队", cancelText: "取消", confirmLoading: submitting, okButtonProps: { danger: action === "remove", disabled: !username.trim() }, onOk: () => onSubmit(action, username.trim()) }, React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 14 }, message: "仅管理员可执行", description: "仅传入平台登录用户名；服务器会解析真实用户 ID、校验管理员角色并追加授权审计。" }), React.createElement(Select, { value: action, options: [{ value: "add", label: "加入共享台账团队" }, { value: "remove", label: "移出团队名册" }], onChange: setAction, style: { width: "100%", marginBottom: 12 } }), React.createElement(Input, { value: username, maxLength: 100, placeholder: "平台登录用户名", onChange: (event: any) => setUsername(event.target.value) }));
}

/**
 * 为管理员提供历史台账完整性迁移和空台账重签的显式危险操作入口。
 *
 * 输入是管理员人工核验的 SHA-256、核验说明和提交回调，输出为两种严格分离的安全维护表单。
 * 前端不会读取或计算本机台账文件、不会传入用户 ID、密钥、签名、证据、URL 或摘录；服务端
 * 必须再次验证管理员身份、工作区、哈希和严格的空台账/历史迁移前提。普通用户将被服务器
 * 拒绝，任何失败都不会改变证据链，因此日常业务人员不应使用该面板。
 */
function IntegrityMaintenanceModal({ open, onClose, onSubmit, submitting }: DataRecord): any {
  const [action, setAction] = React.useState("migrate");
  const [hash, setHash] = React.useState("");
  const [note, setNote] = React.useState("");
  React.useEffect(() => { if (open) { setAction("migrate"); setHash(""); setNote(""); } }, [open]);
  const hashOk = /^[a-fA-F0-9]{64}$/.test(hash.trim());
  return React.createElement(Modal, { open, onCancel: onClose, title: "管理员：台账完整性维护", okText: action === "migrate" ? "执行历史迁移" : "恢复空台账签名", cancelText: "取消", confirmLoading: submitting, okButtonProps: { danger: true, disabled: !hashOk || !note.trim() }, onOk: () => onSubmit(action, hash.trim().toLowerCase(), note.trim()) }, React.createElement(Alert, { type: "error", showIcon: true, style: { marginBottom: 14 }, message: "高风险管理员操作：仅在脚本明确提示需要时使用", description: "请在独立的管理员核验流程中取得当前 SHA-256 后手动粘贴。页面不会为你计算哈希或自动选择操作；普通招商复核、来源管理和证据确认都不需要此功能。" }), React.createElement(Select, { value: action, onChange: setAction, style: { width: "100%", marginBottom: 12 }, options: [{ value: "migrate", label: "迁移已核验的历史证据链" }, { value: "reseal", label: "仅恢复确认为空的台账签名" }] }), React.createElement(Input, { value: hash, maxLength: 64, placeholder: "已人工核验的 64 位 SHA-256", onChange: (event: any) => setHash(event.target.value) }), React.createElement(Input.TextArea, { value: note, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, style: { marginTop: 12 }, placeholder: "说明核验来源、时间和本次必要性", onChange: (event: any) => setNote(event.target.value) }));
}

/**
 * 为已有关系边追加“引用哪些既有证据”的补证记录，不能覆盖原关系或伪造来源。
 *
 * 输入是服务器关系边和提交回调，输出为证据 ID 与理由表单。用户只能引用已登记 evidence ID；
 * 后端会检查其是否属于该关系所关联的线索并追加独立审计，页面没有 URL、摘录、采集时间、
 * 关系端点、操作人或替换原证据列表的输入。
 */
function EvidenceSupplementModal({ edge, onClose, onSubmit, submitting }: DataRecord): any {
  const [evidenceText, setEvidenceText] = React.useState("");
  const [note, setNote] = React.useState("");
  React.useEffect(() => { setEvidenceText(""); setNote(""); }, [edge?.edge_id]);
  const evidenceIds = parseEvidenceIds(evidenceText);
  return React.createElement(Modal, { open: !!edge, title: "为关系边追加既有证据", okText: "追加补证审计", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !evidenceIds.length || !note.trim() }, onCancel: onClose, onOk: () => onSubmit(edge.edge_id, evidenceIds, note.trim()) }, React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 14 }, message: "只允许追加，不允许替换", description: "填写已在共享台账中的证据 ID（逗号或换行分隔）。服务器会校验引用关系，原边、原证据和来源内容不会被改写。" }), React.createElement(Input.TextArea, { value: evidenceText, autoSize: { minRows: 3, maxRows: 6 }, maxLength: 2400, showCount: true, placeholder: "evidence-...，evidence-...", onChange: (event: any) => setEvidenceText(event.target.value) }), React.createElement(Input.TextArea, { value: note, autoSize: { minRows: 3, maxRows: 6 }, maxLength: 2000, showCount: true, style: { marginTop: 12 }, placeholder: "补证理由", onChange: (event: any) => setNote(event.target.value) }));
}

/**
 * 在确有公开证据时登记图谱节点或关系边，提供草稿登记之外的受控人工入口。
 *
 * 输入是提交回调，输出为节点/边所需的最少字段。页面不收集电话、住址、资产、资金承诺或
 * 用户 ID；证据 ID 只能指向既有台账记录，后端会二次校验角色、线索关联和不可变证据链，
 * 因此本入口不能把主观关系或个人财务信息写入图谱。
 */
function GraphRegistrationModal({ open, onClose, onSubmit, submitting }: DataRecord): any {
  const [mode, setMode] = React.useState("node");
  const [values, setValues] = React.useState({ display_name: "", node_kind: "organization", locality: "徐州关联待核验", linked_lead_id: "", from_node_id: "", to_node_id: "", relationship_type: "", evidence_ids: "", registration_reason: "" });
  React.useEffect(() => { if (open) { setMode("node"); setValues({ display_name: "", node_kind: "organization", locality: "徐州关联待核验", linked_lead_id: "", from_node_id: "", to_node_id: "", relationship_type: "", evidence_ids: "", registration_reason: "" }); } }, [open]);
  const set = (key: string) => (event: any) => setValues({ ...values, [key]: event.target.value });
  const ids = parseEvidenceIds(values.evidence_ids);
  const valid = mode === "node" ? !!values.display_name.trim() && !!values.node_kind.trim() && !!values.locality.trim() && ids.length > 0 && !!values.registration_reason.trim() : !!values.from_node_id.trim() && !!values.to_node_id.trim() && !!values.relationship_type.trim() && ids.length > 0 && !!values.registration_reason.trim();
  return React.createElement(Modal, { open, width: "min(700px, 94vw)", onCancel: onClose, title: "人工登记证据化关系图谱", okText: mode === "node" ? "登记节点" : "登记关系边", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !valid }, onOk: () => onSubmit(mode, values, ids) }, React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 14 }, message: "只登记公开、可回链的业务关系", description: "不得录入联系方式、住址、个人资产或资金承诺。节点/边均须引用既有正式证据，登记后关系边仍须单独人工复核。" }), React.createElement(Select, { value: mode, options: [{ value: "node", label: "登记节点" }, { value: "edge", label: "登记关系边" }], onChange: setMode, style: { width: "100%", marginBottom: 12 } }), mode === "node" ? React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } }, React.createElement(Input, { value: values.display_name, maxLength: 300, placeholder: "公开显示名称", onChange: set("display_name") }), React.createElement(Input, { value: values.node_kind, maxLength: 100, placeholder: "节点类型（由后端校验）", onChange: set("node_kind") }), React.createElement(Input, { value: values.locality, maxLength: 100, placeholder: "宽泛地域关联分类", onChange: set("locality") }), React.createElement(Input, { value: values.linked_lead_id, maxLength: 100, placeholder: "关联线索 ID（可选）", onChange: set("linked_lead_id") })) : React.createElement(Space, { direction: "vertical", size: 10, style: { width: "100%" } }, React.createElement(Input, { value: values.from_node_id, maxLength: 100, placeholder: "起点节点 ID", onChange: set("from_node_id") }), React.createElement(Input, { value: values.to_node_id, maxLength: 100, placeholder: "终点节点 ID", onChange: set("to_node_id") }), React.createElement(Input, { value: values.relationship_type, maxLength: 100, placeholder: "关系类型（由后端校验）", onChange: set("relationship_type") })), React.createElement(Input.TextArea, { value: values.evidence_ids, maxLength: 2400, showCount: true, autoSize: { minRows: 3, maxRows: 5 }, style: { marginTop: 12 }, placeholder: "已有证据 ID，逗号或换行分隔", onChange: set("evidence_ids") }), React.createElement(Input.TextArea, { value: values.registration_reason, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 5 }, style: { marginTop: 12 }, placeholder: "登记理由", onChange: set("registration_reason") }));
}

/**
 * 为一个服务器已有评分快照追加真实招商结果，供后续人工审查规则建议。
 *
 * 输入是线索 ID、快照 ID 与提交回调，输出为结果枚举和备注表单。页面不能提交总分、规则、
 * 证据、线索状态或用户 ID；反馈不会自动调整权重、修改 Skill 或推进线索，后端只追加审计。
 */
function OutcomeFeedbackModal({ target, onClose, onSubmit, submitting }: DataRecord): any {
  const [outcome, setOutcome] = React.useState("positive_progress");
  const [note, setNote] = React.useState("");
  React.useEffect(() => { setOutcome("positive_progress"); setNote(""); }, [target?.score_snapshot_id]);
  const options = [{ value: "landed", label: "已落地" }, { value: "positive_progress", label: "推进" }, { value: "deferred", label: "观察" }, { value: "not_converted", label: "未转化" }, { value: "invalidated", label: "无效" }];
  return React.createElement(Modal, { open: !!target, title: "录入招商结果反馈", okText: "追加结果反馈", cancelText: "取消", confirmLoading: submitting, okButtonProps: { disabled: !note.trim() }, onCancel: onClose, onOk: () => onSubmit(target.lead_id, target.score_snapshot_id, outcome, note.trim()) }, React.createElement(Alert, { type: "info", showIcon: true, style: { marginBottom: 14 }, message: "结果反馈不等于自动改权重", description: "它只为后续人工查看的规则调整建议积累样本，不改动线索状态、评分结果和证据链。" }), React.createElement(Select, { value: outcome, options, onChange: setOutcome, style: { width: "100%", marginBottom: 12 } }), React.createElement(Input.TextArea, { value: note, maxLength: 2000, showCount: true, autoSize: { minRows: 3, maxRows: 6 }, placeholder: "记录实际进展和判断依据", onChange: (event: any) => setNote(event.target.value) }));
}

/**
 * 为业务人员和维护人员提供同一份页面内手册。
 *
 * 输入是打开状态和关闭回调，输出为只读抽屉。手册不请求 API、不读取台账，也不会触发
 * 抓取、复核、学习或外部消息；它只解释当前版本已有功能、人机边界和扩展约定。
 */
function UsageGuideDrawer({ open, onClose }: DataRecord): any {
  const flow = [
    ["1", "发现候选", "机器保存标题、网址和发现摘要；此时还没有可审计页面材料。"],
    ["2", "保存材料", "服务器按 robots、安全和访问规则保存可见文本、时间与内容哈希。"],
    ["3", "人工判断", "业务人员选择补证、观察、无效，或说明无需徐州字段仍值得核验。"],
    ["4", "重新核验入证", "人发起请求；服务器重访原网页并逐项核对，通过才新增正式证据。"],
    ["5", "人工招商审批", "人核对价值和风险，指定负责人、下一步和完成日期。"],
    ["6", "结果反馈与学习", "人记录真实结果；机器只优化阅读顺序，不改写事实或替人决策。"],
  ];
  const featureRows = [
    { area: "招商总览", purpose: "查看线索、风险、人工待办、候选和工作统计。", boundary: "只读汇总，不等于成果或企业事实。" },
    { area: "证据与招商跟进", purpose: "查看正式证据、研判报告、准备卡和人工审批。", boundary: "审批只进入内部跟进池，不会自动外联。" },
    { area: "产业与来源", purpose: "看产业指标、来源覆盖、计划状态和访问退避。", boundary: "不同统计口径只并列展示，不能相加。" },
    { area: "候选与关系", purpose: "处理候选、保存材料、证据草稿、第二来源和关系图谱。", boundary: "候选、材料、草稿都不是正式证据。" },
    { area: "复核与治理", purpose: "补充公开网址、管理来源、规则、学习建议、关系与名册。", boundary: "所有写入均由服务器认证、校验并审计。" },
    { area: "独立业务页", purpose: "提供人工材料入账、智能对话、账号管理和钉钉日报。", boundary: "与本工作台共用后端、账号、台账和权限。" },
  ];
  const statusRows = [
    { state: "仅发现链接", meaning: "只有标题、网址和摘要", next: "可作业务判断；等待机器保存材料，或合法提交人工材料" },
    { state: "已保存页面材料", meaning: "已有只读文本、采集时间和哈希", next: "人工阅读；具备条件时请求服务器重新核验" },
    { state: "待补证", meaning: "有价值但关键依据不足", next: "说明缺什么，继续找独立来源或一手材料" },
    { state: "观察", meaning: "暂不投入大量人工时间", next: "保留低频关注，不自动确认事实" },
    { state: "正式证据", meaning: "服务器重新访问并通过一致性校验", next: "进入人工招商价值判断与跟进审批" },
    { state: "已纳入人工跟进池", meaning: "团队决定由人继续工作", next: "负责人按计划执行；不代表企业已经同意" },
  ];
  const guideList = (items: string[]) => React.createElement("ul", { style: { paddingLeft: 20, margin: "8px 0 0", color: "#475569", lineHeight: 1.8 } }, items.map((item) => React.createElement("li", { key: item }, item)));
  const roleCard = (tag: string, color: string, title: string, items: string[], warning: string) => React.createElement(
    Card,
    { size: "small", style: { ...CARD_STYLE, height: "100%", borderTop: `4px solid ${color}` } },
    React.createElement(Tag, { color }, tag),
    React.createElement(AntText, { strong: true, style: { display: "block", marginTop: 10, fontSize: 16 } }, title),
    guideList(items),
    React.createElement(Paragraph, { style: { margin: "12px 0 0", color: "#64748b", fontSize: 13 } }, warning),
  );
  const quickStart = React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Alert, { type: "info", showIcon: true, message: "先记住：机器找证据，人决定要不要招商跟进", description: "自动统计、评分、对话回答和钉钉简报都不能证明企业有来徐投资意愿。系统里的候选、材料、草稿、证据和跟进是五个不同阶段。" }),
    React.createElement(Row, { gutter: [12, 12] }, ...flow.map((item) => React.createElement(Col, { key: item[0], xs: 24, md: 12, xl: 8 }, React.createElement(Card, { size: "small", style: { height: "100%", background: "#f7fbf9", border: "1px solid #dcebe4" } }, React.createElement(Space, { align: "start" }, React.createElement(Tag, { color: "cyan", style: { marginTop: 2 } }, item[0]), React.createElement(Space, { direction: "vertical", size: 4 }, React.createElement(AntText, { strong: true }, item[1]), React.createElement(AntText, { type: "secondary", style: { lineHeight: 1.65 } }, item[2]))))))),
    React.createElement(Card, { size: "small", title: "业务人员每天最短使用路径", style: CARD_STYLE }, guideList(["先看“招商总览”的人工待办和风险提醒。", "到“候选与关系”阅读材料，记录补证、观察、无效或地域判断。", "材料字段完整时请求服务器重新核验；以页面回执判断是否真正入证。", "到“证据与招商跟进”完成人工审批，写清负责人、下一步和日期。", "有真实进展后记录结果，让来源学习改善未来阅读顺序。"])),
  );
  const permissions = React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Row, { gutter: [12, 12] },
      React.createElement(Col, { xs: 24, lg: 8 }, roleCard("机器", "blue", "自动执行但不拍板", ["低频发现公开候选", "安全保存页面材料", "按人工请求重新核验", "提示缺项、风险与来源集中度", "生成只读报告、排序和钉钉简报"], "不能联系企业、承诺政策、绕过登录或 robots，也不能自动宣布招商成功。")),
      React.createElement(Col, { xs: 24, lg: 8 }, roleCard("业务人员", "green", "拥有最终业务决定权", ["打开原网页判断价值", "提交有权使用的人工材料", "补充公开网址并启停来源", "选择补证、观察、无效或地域豁免", "发起核验、审批跟进并指定负责人", "记录真实结果并控制学习策略"], "不能改写服务器快照、历史证据、内容哈希、采集时间、登录身份或既有审计。")),
      React.createElement(Col, { xs: 24, lg: 8 }, roleCard("管理员", "orange", "管理账号和准入", ["创建、停用和恢复平台账号", "维护徐州团队名册", "管理个人钉钉日报", "在严格条件下执行完整性维护"], "管理员也不能伪造企业事实、覆盖历史证据或替业务人员完成真实招商。")),
    ),
    React.createElement(Alert, { type: "warning", showIcon: true, message: "人的权限大，证据边界仍然不可绕过", description: "人可以否定、保留、补充、发起核验和决定跟进；但任何人都不能把浏览器手填内容冒充成服务器抓取，或把人工判断写成企业承诺。" }),
  );
  const featureMap = React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Table, { size: "small", rowKey: "area", pagination: false, scroll: { x: 760 }, columns: [{ title: "区域", dataIndex: "area", width: 150, render: (value: string) => React.createElement(AntText, { strong: true }, value) }, { title: "主要用途", dataIndex: "purpose" }, { title: "使用边界", dataIndex: "boundary" }], dataSource: featureRows }),
    React.createElement(Divider, { orientation: "left" }, "关键状态"),
    React.createElement(Table, { size: "small", rowKey: "state", pagination: false, scroll: { x: 760 }, columns: [{ title: "状态", dataIndex: "state", width: 150, render: (value: string) => React.createElement(Tag, null, value) }, { title: "真实含义", dataIndex: "meaning" }, { title: "下一步", dataIndex: "next" }], dataSource: statusRows }),
  );
  const maintenance = React.createElement(
    Space,
    { direction: "vertical", size: 14, style: { width: "100%" } },
    React.createElement(Alert, { type: "info", showIcon: true, message: "双端共用一个后端事实源", description: "QwenPaw 工作台和独立业务页都调用 /api/xuzhou-investment-ledger；账号、团队名册、共享台账、证据链、审计和钉钉配置均在服务器统一校验，前端不应复制业务事实。" }),
    React.createElement(Card, { size: "small", title: "来源扩展", style: CARD_STYLE }, guideList(["普通公开网页：优先由“复核与治理 → 人工补充公开来源”提交，服务器检查 URL、robots、公开访问和跨域跳转。", "付费 API、登录数据源：必须在后端按供应商许可接入最小权限密钥、限速、审计和故障隔离；禁止让用户把密钥贴到页面。", "固定来源配方：只能在现有来源中重排，并须遵守人工选择的学习策略。"])),
    React.createElement(Card, { size: "small", title: "功能与字段扩展", style: CARD_STYLE }, guideList(["先修改后端模型和受限接口，再同步两个前端的中文解释；浏览器不能成为身份、时间、哈希或证据字段的权威来源。", "新增状态时必须补齐状态含义、用户下一步、审计和回归测试；前端遇到未知状态应原样显示，不能猜成成功。", "新自动化必须明确触发时间、失败重试、收件人和数据范围，且不能借定时任务扩大对外权限。"])),
    React.createElement(Card, { size: "small", title: "维修顺序", style: CARD_STYLE }, guideList(["页面空白或无数据：先查登录状态，再查徐州团队名册，然后看单个接口错误；不要用假数据填充。", "抓取失败：看 robots、DNS、登录/付费、重定向和页面变化；业务人员能打开不代表服务器可访问。", "核验未生效：区分“预检查未开始抓取”和“已抓取但字段不一致”，必须把真实失败阶段显示给用户。", "上线前：运行后端测试、两个前端检查、生成校验包；生产发布必须先备份、健康检查并保留回滚路径。"])),
    React.createElement(Alert, { type: "warning", showIcon: true, message: "学习模块的硬边界", description: "自动学习只允许调整阅读队列，以及在用户明确授权后重排既有固定来源配方；不得自行新增来源、关键词、产业、Skill、证据、招商结论或对外动作。" }),
  );
  return React.createElement(
    Drawer,
    { open, onClose, width: "min(980px, 96vw)", title: React.createElement(Space, { direction: "vertical", size: 1 }, React.createElement(AntText, { strong: true, style: { fontSize: 18 } }, "徐州招商智能体简明使用手册"), React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, "业务操作、权限边界与开发维护速查")), destroyOnClose: false },
    React.createElement(Tabs, { defaultActiveKey: "start", items: [{ key: "start", label: "完整流程", children: quickStart }, { key: "permissions", label: "人机权限", children: permissions }, { key: "features", label: "功能与状态", children: featureMap }, { key: "maintenance", label: "维护与扩展", children: maintenance }] }),
  );
}

/**
 * 复核与治理页汇集此前仅有后端接口的人工入口，所有写操作仍指向各自最小接口。
 *
 * 输入是已认证读取的数据和由根组件提供的受限动作回调，输出为来源、规则、学习建议、指标、
 * 关系及名册操作页。组件不直接拼接用户 ID、不接受证据正文或来源 URL 的修改；每个按钮
 * 仅携带服务器已有 ID、固定决定和备注，真实权限/完整性检查在后端执行并由刷新后的数据体现。
 */
function GovernanceTab({ trustedSources, publicSources, sourceAudit, scoringRules, scoringSuggestions, recipeRules, recipeProposals, actionCenter, relationshipDrafts, coInvestmentDrafts, graph, team, onOpenSource, onOpenPublicSource, onOpenRules, onOpenTeam, onOpenIntegrityMaintenance, onOpenGraphRegistration, onOpenSupplement, onOpenTask, onWrite }: DataRecord): any {
  const sources = listField(trustedSources, ["sources", "trusted_sources"]).filter((item: DataRecord) => item?.source_type !== "human_added_public_source");
  const publicSourceRecords = listField(publicSources, ["sources", "items", "data"]);
  const sourceAuditEvents = listField(sourceAudit, ["audit_events"]);
  const proposals = listField(recipeProposals, ["proposals", "learning_proposals"]);
  const relationshipDraftList = listField(relationshipDrafts, ["relationship_drafts", "drafts"]);
  const coInvestmentList = listField(coInvestmentDrafts, ["co_investment_signal_drafts", "drafts"]);
  const edges = listField(graph, ["edges"]);
  const members = listField(team, ["members", "team_members"]);
  const actionItems = listField(actionCenter, ["items"]);
  const metricDraftItems = actionItems.filter((item) => item?.item_kind === "industry_metric_draft_manual_review" && item?.metric_draft);
  const makeTask = (key: string, title: string, description: string, options: DataRecord[], submit: (decision: string, note: string) => Promise<void>) => onOpenTask({ key, title, description, options, submit });
  const sourceColumns = [
    { title: "域名", dataIndex: "domain" },
    { title: "类型", dataIndex: "source_type" },
    { title: "等级", dataIndex: "source_level" },
    { title: "状态", dataIndex: "status", render: (value: unknown) => React.createElement(Tag, { color: value === "active" ? "green" : "default" }, String(value ?? "—")) },
    { title: "操作", render: (_: unknown, row: DataRecord) => React.createElement(Button, { size: "small", onClick: () => makeTask(`source-${row.source_id}`, "变更可信来源状态", "只影响未来材料的目录资格，历史证据与其来源字段不会重写。", [{ value: "active", label: "启用" }, { value: "disabled", label: "停用" }], (decision, reviewNote) => onWrite(`/trusted-sources/${encodeURIComponent(row.source_id)}/status`, "POST", { new_status: decision, change_reason: reviewNote }, "来源状态已追加审计。")) }, "启用/停用") },
  ];
  const publicSourceColumns = [
    { title: "公开入口", render: (_: unknown, row: DataRecord) => row.seed_url ? React.createElement("a", { href: row.seed_url, target: "_blank", rel: "noreferrer" }, row.domain || row.seed_url) : row.domain || "—" },
    { title: "服务器检测", render: (_: unknown, row: DataRecord) => React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(Tag, { color: row.crawl_status === "ready_for_public_crawl" ? "green" : "gold" }, label(row.crawl_status, PUBLIC_SOURCE_CRAWL_LABELS)), React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, formatDate(row.last_preflight_at || row.created_at))) },
    { title: "当前状态", render: (_: unknown, row: DataRecord) => React.createElement(Tag, { color: row.status === "active" ? "green" : "default" }, row.status === "active" ? "自动发现已启用" : "已暂停") },
    { title: "最近说明", dataIndex: "crawl_detail", render: (value: unknown) => React.createElement(AntText, { type: "secondary", style: { fontSize: 12 } }, String(value || "尚未记录检测说明")) },
    { title: "操作", render: (_: unknown, row: DataRecord) => React.createElement(Space, { wrap: true, size: 4 }, React.createElement(Button, { size: "small", onClick: () => onWrite(`/public-sources/${encodeURIComponent(row.source_id)}/retest`, "POST", undefined, "服务器重新检测已完成，请查看最新状态。") }, "重新检测"), React.createElement(Button, { size: "small", onClick: () => onWrite(`/public-sources/${encodeURIComponent(row.source_id)}/status`, "POST", { new_status: row.status === "active" ? "disabled" : "active" }, row.status === "active" ? "已暂停该公开来源的自动发现。" : "服务器检测通过后，公开来源已重新启用。") }, row.status === "active" ? "暂停" : "重新检测并启用")) },
  ];
  const edgeColumns = [
    { title: "关系", dataIndex: "relationship_type" },
    { title: "当前状态", dataIndex: "review_state", render: (value: unknown) => React.createElement(Tag, null, String(value ?? "—")) },
    { title: "引用证据", dataIndex: "evidence_ids", render: (value: unknown) => Array.isArray(value) ? `${value.length} 条` : "—" },
    { title: "操作", render: (_: unknown, row: DataRecord) => React.createElement(Space, { wrap: true, size: 4 }, React.createElement(Button, { size: "small", onClick: () => makeTask(`edge-${row.edge_id}`, "复核关系图谱边", "复核只追加关系边业务状态；不能改关系端点、类型或证据引用。", [{ value: "valid", label: "有效" }, { value: "need_evidence", label: "待补证" }, { value: "invalid", label: "无效" }], (decision, reviewNote) => onWrite(`/relationship-graph/edges/${encodeURIComponent(row.edge_id)}/review`, "POST", { decision, review_note: reviewNote }, "关系边复核已保存。")) }, "复核"), React.createElement(Button, { size: "small", onClick: () => onOpenSupplement(row) }, "追加补证")) },
  ];
  const sourceAuditContent = sourceAudit?._loadError
    ? React.createElement(Alert, { type: "info", showIcon: true, message: "来源审计暂不可读取", description: sourceAudit._loadError })
    : React.createElement(List, {
        size: "small",
        dataSource: sourceAuditEvents,
        locale: { emptyText: "暂无来源配置审计" },
        renderItem: (item: DataRecord) => React.createElement(
          List.Item,
          null,
          React.createElement(
            Space,
            { direction: "vertical", size: 2 },
            React.createElement(AntText, { strong: true }, `${item.action || "目录变更"}｜${item.source_id || "—"}`),
            React.createElement(AntText, { type: "secondary" }, `${formatDate(item.operated_at)}　${item.old_status || "—"} → ${item.new_status || "—"}`),
            React.createElement(AntText, { type: "secondary" }, item.reason || "未记录变更理由。"),
          ),
        ),
      });
  const simpleCard = (title: string, extra: any, content: any) => React.createElement(Card, { title, extra, style: CARD_STYLE }, content);
  return React.createElement(
    Space,
    { direction: "vertical", size: 16, style: { width: "100%" } },
    React.createElement(Alert, { type: "warning", showIcon: true, message: "所有治理操作均保留人工和证据边界", description: "普通成员可做业务复核、来源配置和规则版本决策；只有团队名册保持管理员限制。所有操作人由服务器登录态确定，页面不发送 reviewer_user_id。" }),
    simpleCard(`人工补充公开来源（${publicSourceRecords.length}）`, React.createElement(Button, { type: "primary", onClick: onOpenPublicSource }, "检测并加入公开网址"), React.createElement(Space, { direction: "vertical", size: 12, style: { width: "100%" } }, React.createElement(Alert, { type: "info", showIcon: true, message: "业务人员可补充机器能直接读取的公开网页", description: "服务器会在登记和重新启用时检查公开访问、robots、同域跳转与页面可读性。这里只能填写无需账号或密钥的网页；付费 API 请由开发者在后端按供应商许可接入。通过检测只会生成待人工核验候选，不会自动入证或联系企业。" }), publicSources?._loadError ? React.createElement(Alert, { type: "warning", showIcon: true, message: "补充来源暂时无法读取", description: publicSources._loadError }) : React.createElement(Table, { size: "small", rowKey: "source_id", columns: publicSourceColumns, dataSource: publicSourceRecords, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 980 }, locale: { emptyText: "还没有人工补充公开来源" } }))),
    simpleCard(`可信来源目录（${sources.length}）`, React.createElement(Button, { type: "primary", onClick: onOpenSource }, "登记来源"), React.createElement(Space, { direction: "vertical", size: 12, style: { width: "100%" } }, React.createElement(Table, { size: "small", rowKey: "source_id", columns: sourceColumns, dataSource: sources, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 760 }, locale: { emptyText: "暂无来源或无读取权限" } }), React.createElement(Divider, { orientation: "left", style: { margin: "4px 0" } }, `来源配置审计（${sourceAuditEvents.length}）`), sourceAuditContent)),
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, lg: 12 }, simpleCard("当前评分规则与结果建议", React.createElement(Button, { onClick: onOpenRules }, "创建新规则版本"), React.createElement(Space, { direction: "vertical", size: 8, style: { width: "100%" } }, React.createElement(AntText, { strong: true }, `规则版本：${scoringRules?.rule_set?.version ?? "未读取"}`), React.createElement(AntText, { type: "secondary" }, "规则建议只供人工审查；确认修改会创建新版本，不覆盖历史快照。"), React.createElement("pre", { style: { maxHeight: 150, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 12, margin: 0 } }, JSON.stringify(scoringSuggestions?._loadError ? { 提示: scoringSuggestions._loadError } : scoringSuggestions?.summary ?? scoringSuggestions ?? {}, null, 2))))),
      React.createElement(Col, { xs: 24, lg: 12 }, simpleCard("来源配方与学习建议", null, React.createElement(Space, { direction: "vertical", size: 8, style: { width: "100%" } }, React.createElement(AntText, { type: "secondary" }, `当前配方版本：${recipeRules?.version ?? recipeRules?.rule_set?.version ?? "未读取"}`), proposals.length ? React.createElement(List, { size: "small", dataSource: proposals, renderItem: (item: DataRecord) => React.createElement(List.Item, { actions: [React.createElement(Button, { key: "review", size: "small", onClick: () => makeTask(`recipe-${item.proposal_id}`, "审批来源配方学习建议", "批准仅创建下一版固定来源配方顺序，不新增搜索词、URL、产业范围或自动改写 Skill。", [{ value: "approved", label: "批准" }, { value: "rejected", label: "拒绝" }], (decision, reviewNote) => onWrite(`/source-recipe-learning/learning-proposals/${encodeURIComponent(item.proposal_id)}/review`, "POST", { decision, review_note: reviewNote }, "来源配方学习建议已处理。")) }, "审批")] }, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, item.title || item.proposal_id || "来源配方建议"), React.createElement(AntText, { type: "secondary" }, item.reason || item.summary || "请审查样本量与建议依据。"))) }) : React.createElement(Empty, { description: "当前没有待审批的来源学习建议" })))),
    ),
    simpleCard(`经营指标草稿待复核（${metricDraftItems.length}）`, null, metricDraftItems.length ? React.createElement(List, { size: "small", dataSource: metricDraftItems, renderItem: (item: DataRecord) => { const draft = item.metric_draft; return React.createElement(List.Item, { actions: [React.createElement(Button, { key: "metric", size: "small", onClick: () => makeTask(`metric-${draft.metric_draft_id}`, "复核产业经营指标草稿", "确认仅让该指标进入并列统计；金额、年份、主体、来源与证据全部由服务器原草稿读取。", [{ value: "confirm", label: "确认" }, { value: "need_evidence", label: "待补证" }, { value: "invalid", label: "无效" }], (decision, reviewNote) => onWrite(`/industry-metrics/drafts/${encodeURIComponent(draft.metric_draft_id)}/review`, "POST", { decision, review_note: reviewNote }, "经营指标草稿已复核。")) }, "复核")] }, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, `${draft.entity_name || "未命名主体"}｜${draft.metric_label || draft.metric_kind || "指标"}`), React.createElement(AntText, { type: "secondary" }, `${draft.period_year ?? "—"}：${draft.original_value_text || draft.value_yuan || "—"}；${draft.statistic_scope || "统计范围待核验"}`))) } }) : React.createElement(Empty, { description: "当前没有行动中心中的指标草稿待办" })),
    React.createElement(Row, { gutter: [14, 14] },
      React.createElement(Col, { xs: 24, lg: 12 }, simpleCard(`关键人员关系草稿（${relationshipDraftList.length}）`, React.createElement(Button, { onClick: onOpenGraphRegistration }, "人工登记图谱"), relationshipDraftList.length ? React.createElement(List, { size: "small", dataSource: relationshipDraftList, renderItem: (item: DataRecord) => React.createElement(List.Item, { actions: [React.createElement(Button, { key: "review", size: "small", onClick: () => makeTask(`relationship-draft-${item.relationship_draft_id}`, "复核关键人员关系草稿", "草稿只可按已有证据人工判断；标为可登记后仍需明确点击登记，并会再次证据校验。", [{ value: "ready_for_graph_registration", label: "可登记图谱" }, { value: "need_evidence", label: "待补证" }, { value: "observe", label: "观察" }, { value: "invalid", label: "无效" }], (decision, reviewNote) => onWrite(`/relationship-drafts/${encodeURIComponent(item.relationship_draft_id)}/review`, "POST", { decision, review_note: reviewNote }, "关系草稿复核已保存。")) }, "复核"), React.createElement(Button, { key: "register", size: "small", disabled: item.review_state !== "ready_for_graph_registration", onClick: () => onWrite(`/relationship-drafts/${encodeURIComponent(item.relationship_draft_id)}/register-to-graph`, "POST", undefined, "已将草稿登记为仍待复核的图谱边。") }, "登记图谱")] }, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, item.person_name || item.display_name || item.relationship_draft_id || "关系草稿"), React.createElement(AntText, { type: "secondary" }, `状态：${item.review_state || item.state || "等待用户复核"}；${item.reason || item.matched_excerpt || "请回看引用证据。"}`))) }) : React.createElement(Empty, { description: "暂无关键人员关系草稿" }))),
      React.createElement(Col, { xs: 24, lg: 12 }, simpleCard(`组织级协同投资信号（${coInvestmentList.length}）`, null, coInvestmentList.length ? React.createElement(List, { size: "small", dataSource: coInvestmentList, renderItem: (item: DataRecord) => React.createElement(List.Item, { actions: [React.createElement(Button, { key: "review", size: "small", onClick: () => makeTask(`co-investment-${item.co_investment_draft_id}`, "复核组织级协同投资信号", "它只表示可能需要人工核验公开主体和授权联系人，不代表募资、资金承诺、撮合或自动推进。", [{ value: "ready_for_contact_validation", label: "可核验对接" }, { value: "need_evidence", label: "待补证" }, { value: "observe", label: "观察" }, { value: "invalid", label: "无效" }], (decision, reviewNote) => onWrite(`/co-investment-signals/${encodeURIComponent(item.co_investment_draft_id)}/review`, "POST", { decision, review_note: reviewNote }, "协同投资信号已复核。")) }, "复核")] }, React.createElement(Space, { direction: "vertical", size: 2 }, React.createElement(AntText, { strong: true }, item.entity_name || item.co_investment_draft_id || "协同投资信号"), React.createElement(AntText, { type: "secondary" }, `状态：${item.review_state || item.state || "等待用户复核"}；${item.matched_excerpt || item.reason || "请回看已有公开证据。"}`))) }) : React.createElement(Empty, { description: "暂无协同投资信号草稿" }))),
    ),
    simpleCard(`图谱关系边（${edges.length}）`, React.createElement(Button, { onClick: onOpenGraphRegistration }, "人工登记图谱"), React.createElement(Table, { size: "small", rowKey: "edge_id", columns: edgeColumns, dataSource: edges, pagination: { pageSize: 6, hideOnSinglePage: true }, scroll: { x: 760 }, locale: { emptyText: "暂无图谱边" } })),
    simpleCard(`团队名册（${members.length}）`, React.createElement(Button, { onClick: onOpenTeam }, "管理员维护名册"), team?._loadError ? React.createElement(Alert, { type: "info", showIcon: true, message: "名册仅限管理员", description: team._loadError }) : React.createElement(Table, { size: "small", rowKey: (row: DataRecord) => row.user_id ?? row.username, columns: [{ title: "用户名", dataIndex: "username" }, { title: "状态", dataIndex: "status" }, { title: "加入时间", dataIndex: "added_at", render: formatDate }], dataSource: members, pagination: { pageSize: 6, hideOnSinglePage: true }, locale: { emptyText: "管理员可在此读取团队名册" } })),
    simpleCard("系统完整性维护（仅管理员）", React.createElement(Button, { danger: true, onClick: onOpenIntegrityMaintenance }, "打开管理员维护"), React.createElement(Alert, { type: "error", showIcon: true, message: "历史迁移和空台账重签不属于日常招商操作", description: "仅在管理员已通过独立核验取得 SHA-256，且维护脚本明确要求执行时使用。服务器会再次强制管理员权限与台账安全条件；普通用户不会因此获得证据链修改能力。" })),
  );
}

/**
 * 工作台根组件：统一加载只读数据、控制受限复核操作并注册各业务视图。
 *
 * 输入为空，输出为招商工作台页面。首次加载和刷新只调用已认证读取接口；状态变更仅经独立
 * 复核、评分或计划启停接口执行，且请求从不携带用户 ID、来源 URL、原始摘录、事实标签或
 * 风险等级。任何后端拒绝都会保留原页面数据并提示用户，而非前端猜测成功结果。
 */
function XuzhouInvestmentDashboard(): any {
  const [loading, setLoading] = React.useState(true);
  const [refreshing, setRefreshing] = React.useState(false);
  const [activeTab, setActiveTab] = React.useState("overview");
  const [latestCandidateOutcome, setLatestCandidateOutcome] = React.useState(null as DataRecord | null);
  const [data, setData] = React.useState({} as DataRecord);
  const [selectedDetails, setSelectedDetails] = React.useState(null as DataRecord | null);
  const [selectedReport, setSelectedReport] = React.useState(null as DataRecord | null);
  const [selectedOutreach, setSelectedOutreach] = React.useState(null as DataRecord | null);
  const [selectedFollowup, setSelectedFollowup] = React.useState(null as DataRecord | null);
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [reviewLead, setReviewLead] = React.useState(null as DataRecord | null);
  const [reviewSubmitting, setReviewSubmitting] = React.useState(false);
  const [reportLoading, setReportLoading] = React.useState(false);
  const [outreachLoading, setOutreachLoading] = React.useState(false);
  const [followupLoading, setFollowupLoading] = React.useState(false);
  const [followupApprovalTarget, setFollowupApprovalTarget] = React.useState(null as DataRecord | null);
  const [followupSubmitting, setFollowupSubmitting] = React.useState(false);
  const [scheduling, setScheduling] = React.useState(null as string | null);
  const [selectedObservation, setSelectedObservation] = React.useState(null as DataRecord | null);
  const [observationDrawerOpen, setObservationDrawerOpen] = React.useState(false);
  const [selectedCorroboration, setSelectedCorroboration] = React.useState(null as DataRecord | null);
  const [corroborationDrawerOpen, setCorroborationDrawerOpen] = React.useState(false);
  const [task, setTask] = React.useState(null as DataRecord | null);
  const [xuzhouScopeCandidate, setXuzhouScopeCandidate] = React.useState(null as DataRecord | null);
  const [mutationSubmitting, setMutationSubmitting] = React.useState(false);
  const [sourceModalOpen, setSourceModalOpen] = React.useState(false);
  const [publicSourceModalOpen, setPublicSourceModalOpen] = React.useState(false);
  const [rulesModalOpen, setRulesModalOpen] = React.useState(false);
  const [teamModalOpen, setTeamModalOpen] = React.useState(false);
  const [integrityModalOpen, setIntegrityModalOpen] = React.useState(false);
  const [graphModalOpen, setGraphModalOpen] = React.useState(false);
  const [supplementEdge, setSupplementEdge] = React.useState(null as DataRecord | null);
  const [outcomeTarget, setOutcomeTarget] = React.useState(null as DataRecord | null);
  const [guideOpen, setGuideOpen] = React.useState(false);

  /**
   * 刷新各个彼此独立的只读视图，允许一个非核心报告失败而其余区域继续显示。
   *
   * 输入为空，输出为异步状态更新；函数不触发搜索、候选抓取、自动学习或人工复核，只读取
   * 已有共享台账。每一项均通过 ``safeGet`` 保留错误文本，避免因部分来源统计问题隐藏风险
   * 或证据链数据。
   */
  const refresh = React.useCallback(async () => {
    setRefreshing(true);
    const [leads, actionCenter, workSummary, sourceCoverage, candidates, discoveryCandidates, metrics, subdirectionCoverage, monitorSchedule, outcomeSchedule, trustedSources, publicSources, sourceAudit, graph, relationshipQueue, scoringRules, scoringSuggestions, recipeRules, recipeProposals, relationshipDrafts, coInvestmentDrafts, team] = await Promise.all([
      safeGet("/dashboard/leads?limit=100"),
      safeGet("/dashboard/action-center?limit=100"),
      safeGet("/dashboard/work-summary?window_days=30"),
      safeGet("/dashboard/source-coverage?window_days=30"),
      safeGet("/dashboard/candidate-observations?limit=100"),
      safeGet("/dashboard/discovery-candidates?limit=100"),
      safeGet("/dashboard/industry-metrics?window_years=5"),
      safeGet("/dashboard/subdirection-coverage?window_days=30"),
      safeGet("/source-monitor-schedule/"),
      safeGet("/outcome-learning-schedule/"),
      safeGet("/trusted-sources?active_only=false"),
      safeGet("/public-sources"),
      safeGet("/trusted-sources/audit?limit=100"),
      safeGet("/relationship-graph"),
      safeGet("/dashboard/relationship-review-queue?limit=100"),
      safeGet("/scoring-rules/active"),
      safeGet("/scoring-rules/adjustment-suggestions"),
      safeGet("/source-recipe-learning/active"),
      safeGet("/source-recipe-learning/learning-proposals"),
      safeGet("/relationship-drafts?limit=100"),
      safeGet("/co-investment-signals?limit=100"),
      safeGet("/team/members"),
    ]);
    setData({ leads, actionCenter, workSummary, sourceCoverage, candidates, discoveryCandidates, metrics, subdirectionCoverage, monitorSchedule, outcomeSchedule, trustedSources, publicSources, sourceAudit, graph, relationshipQueue, scoringRules, scoringSuggestions, recipeRules, recipeProposals, relationshipDrafts, coInvestmentDrafts, team });
    setLoading(false);
    setRefreshing(false);
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  /**
   * 读取一条线索的招商跟进审批卡。它只显示已有正式证据、人机分工和审批历史，
   * 不联网、不产生招商动作；写入仍须经过独立审批接口和完整人工检查项。
   */
  const loadFollowup = React.useCallback(async (leadId: string) => {
    setFollowupLoading(true);
    try {
      setSelectedFollowup(await request(`/dashboard/leads/${encodeURIComponent(leadId)}/followup-card`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setFollowupLoading(false);
    }
  }, []);

  /**
   * 打开指定线索的详情抽屉，并从服务器读取当前证据链和招商跟进审批卡。
   *
   * 输入是服务器已有的 lead ID，输出为抽屉状态更新。读取失败时不会根据列表残留数据伪造
   * 详情，更不会编辑证据；用户可关闭抽屉后刷新，或检查登录/共享台账权限。
   */
  const openLead = React.useCallback(async (leadId: string) => {
    setDrawerOpen(true);
    setSelectedDetails(null);
    setSelectedReport(null);
    setSelectedOutreach(null);
    setSelectedFollowup(null);
    try {
      setSelectedDetails(await request(`/dashboard/leads/${encodeURIComponent(leadId)}`));
      await loadFollowup(leadId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
      setDrawerOpen(false);
    }
  }, [loadFollowup]);

  /**
   * 按需读取标准研判报告，避免列表浏览时重复生成大量只读汇总。
   *
   * 输入是当前抽屉的服务器线索 ID，输出为报告状态更新。报告仅汇总证据覆盖与待补证提示，
   * 不产生招商结论、不修改评分、风险或状态；失败时保留此前已加载的证据详情。
   */
  const loadReport = React.useCallback(async (leadId: string) => {
    setReportLoading(true);
    try {
      setSelectedReport(await request(`/dashboard/leads/${encodeURIComponent(leadId)}/assessment`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setReportLoading(false);
    }
  }, []);

  /**
   * 按需读取招商准备卡，明确其是人工后续核验清单而不是自动联系指令。
   *
   * 输入是当前线索 ID，输出为只读准备卡状态。函数不发送消息、联系企业、承诺政策或改变
   * 线索状态；任何准备卡中的标签仍需业务人员查看其引用的原始证据后人工处理。
   */
  const loadOutreach = React.useCallback(async (leadId: string) => {
    setOutreachLoading(true);
    try {
      setSelectedOutreach(await request(`/dashboard/leads/${encodeURIComponent(leadId)}/outreach`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setOutreachLoading(false);
    }
  }, []);

  /**
   * 提交普通登录用户的线索复核决定；可信操作者由服务器会话生成。
   *
   * 输入是 lead ID、允许的决定与备注，输出为刷新后的页面状态。浏览器绝不提交 reviewer
   * user ID、证据、URL、原始摘录或风险字段；后端会校验证据链不变并追加审计，失败时不会
   * 关闭模态框或将本地选择伪装成已保存。
   */
  const submitReview = React.useCallback(async (leadId: string, decision: string, reviewNote: string) => {
    setReviewSubmitting(true);
    try {
      await request(`/leads/${encodeURIComponent(leadId)}/review`, "POST", { decision, review_note: reviewNote });
      message.success("复核已保存；原始证据链保持不变。");
      setReviewLead(null);
      await refresh();
      await openLead(leadId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setReviewSubmitting(false);
    }
  }, [openLead, refresh]);

  /**
   * 提交完整招商跟进审批卡。请求体没有企业事实、URL、摘录、评分或操作者 ID；服务器
   * 只会在已有正式证据基础上追加业务审批、负责人和下一步，绝不会自动联系企业。
   */
  const submitFollowupApproval = React.useCallback(async (leadId: string, body: DataRecord) => {
    setFollowupSubmitting(true);
    try {
      await request(`/leads/${encodeURIComponent(leadId)}/followup-approval`, "POST", body);
      message.success("已确认纳入招商跟进池；负责人和下一步已写入团队审计。系统不会自动对外联系。");
      setFollowupApprovalTarget(null);
      await refresh();
      await openLead(leadId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setFollowupSubmitting(false);
    }
  }, [openLead, refresh]);

  /**
   * 请求后端为既有证据生成一份新的、可回溯评分快照。
   *
   * 输入是线索 ID，输出为提示并刷新详情；函数没有总分、权重、证据或操作者输入，服务端
   * 使用当前生效规则和认证身份。评分只是人工研判辅助，不会自动推进线索或覆盖历史快照。
   */
  const createSnapshot = React.useCallback(async (leadId: string) => {
    try {
      const result = await request(`/scoring-rules/leads/${encodeURIComponent(leadId)}/snapshots`, "POST");
      message.success(`已生成可追溯评分：${result?.snapshot?.total_score ?? result?.score_snapshot?.total_score ?? "请在报告中查看"}`);
      await openLead(leadId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    }
  }, [openLead]);

  /**
   * 启停服务器固定的来源监控或结果学习计划，浏览器不能提交 Cron 或执行参数。
   *
   * 输入为预定义计划类别和 ``enable/pause`` 操作，输出为刷新后的计划状态。接口只允许已
   * 认证用户调用固定端点，后端还会校验共享托管者；前端不接受 URL、搜索词、权重或用户 ID，
   * 因此不能借按钮扩大采集范围或改写证据链。
   */
  const scheduleAction = React.useCallback(async (kind: string, action: string) => {
    const route = kind === "source" ? "/source-monitor-schedule" : "/outcome-learning-schedule";
    setScheduling(kind);
    try {
      await request(`${route}/${action}`, "POST");
      message.success(action === "enable" ? "固定计划已启用。" : "固定计划已暂停。");
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setScheduling(null);
    }
  }, [refresh]);

  /**
   * 执行一个已有业务接口的最小写入请求，并以服务器返回结果刷新整个共享工作台。
   *
   * 输入是固定 API 路径、HTTP 方法、该接口允许的请求体和成功提示，输出为异步页面状态。
   * 调用者不能借此函数附加用户 ID、来源内容或证据字段；每个具体按钮在创建时固定路径和
   * body。服务器拒绝时保留当前界面并显示错误，成功后关闭受限弹窗、重读台账，避免本地
   * 假设写入成功或覆盖不可变证据链。
   */
  const executeWrite = React.useCallback(async (path: string, method: string, body: DataRecord | undefined, successText: string | ((result: DataRecord) => DataRecord), afterSuccess?: (result: DataRecord) => void) => {
    setMutationSubmitting(true);
    try {
      const result = await request(path, method, body);
      const feedback = typeof successText === "function" ? successText(result) : { type: "success", text: successText };
      if (feedback.type === "warning") message.warning(feedback.text);
      else message.success(feedback.text);
      afterSuccess?.(result);
      setTask(null);
      await refresh();
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setMutationSubmitting(false);
    }
  }, [refresh]);

  /** Open the separate form that records a human China-business confirmation and XuZhou waiver. */
  const openXuzhouScopeWaiver = React.useCallback((candidate: DataRecord) => {
    const candidateId = candidate?.candidate_id;
    if (!candidateId) return;
    setObservationDrawerOpen(false);
    setTask(null);
    setXuzhouScopeCandidate({ candidate_id: candidateId, source_title: candidate?.source_title });
  }, []);

  const submitXuzhouScopeWaiver = React.useCallback(async (candidateId: string, body: DataRecord) => {
    await executeWrite(`/discovery-candidates/${encodeURIComponent(candidateId)}/business-review`, "POST", body, (_result: DataRecord) => {
      setLatestCandidateOutcome({ type: "success", confirmed: false, message: "地域判断已生效：等待服务器重新核验", description: "已记录企业来源地、中国业务人工核验依据和“无需徐州字段”决定。它不是正式证据；请回到材料页点击“请求重新核验并入证”。" });
      return { type: "success", text: "地域判断已保存；下一步仍须服务器重新核验。" };
    }, () => setXuzhouScopeCandidate(null));
  }, [executeWrite]);

  /**
   * 打开候选详情并只读取服务器脱敏后的快照和草稿，供后续人工确认。
   *
   * 输入为候选观察 ID，输出为抽屉状态更新；接口不触发网页抓取或证据确认。详情读取失败
   * 会关闭抽屉而不以列表摘要拼凑证据，前端不能由此修改原始快照、哈希、URL 或采集时间。
   */
  const openObservation = React.useCallback(async (observationId: string) => {
    if (!observationId) return;
    setObservationDrawerOpen(true);
    setSelectedObservation(null);
    try {
      setSelectedObservation(await request(`/dashboard/candidate-observations/${encodeURIComponent(observationId)}`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
      setObservationDrawerOpen(false);
    }
  }, []);

  /**
   * 打开候选的第二来源补证快照，只读取服务器已验证的历史材料。
   *
   * 输入是服务器候选 ID，输出为只读抽屉状态。该调用不会访问当前外网、不抓取新页面、不创建
   * 证据或线索，也不确认官网/中国业务/徐州关联；若不存在已保存快照则显示服务器错误，而不
   * 用当前网页或前端推测替代历史材料。
   */
  const openCorroboration = React.useCallback(async (candidateId: string) => {
    if (!candidateId) return;
    setCorroborationDrawerOpen(true);
    setSelectedCorroboration(null);
    try {
      setSelectedCorroboration(await request(`/dashboard/discovery-candidates/${encodeURIComponent(candidateId)}/corroboration`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
      setCorroborationDrawerOpen(false);
    }
  }, []);

  /**
   * 打开刚发现候选的受限业务判断表单，不把页面摘要或快照交给浏览器编辑。
   *
   * 输入是服务器返回的 candidate ID，输出为待补证、观察或无效三选一表单。提交时只发送
   * decision 与 review_note；认证身份由服务器取得，后端会确保候选 URL、摘要、快照和
   * 正式证据均不变，故该入口不能把“人工判断”伪装成企业事实或招商推进。
   */
  const openDiscoveryCandidateReview = React.useCallback((candidateId: string) => {
    // 先关闭材料抽屉，再打开模态框；否则 Drawer 会遮住 Modal，造成截图中的“图层错误”。
    setObservationDrawerOpen(false);
    setTask({ key: `review-discovery-candidate-${candidateId}`, title: "选择候选处理方式", description: "这只是对候选的业务处置，不会改写来源 URL、页面摘要、快照、采集时间、哈希、正式证据或线索。请根据刚才阅读的材料，选择最符合当前事实的一项。", options: CANDIDATE_DECISION_OPTIONS, submit: (decision: string, reviewNote: string) => executeWrite(`/discovery-candidates/${encodeURIComponent(candidateId)}/business-review`, "POST", { decision, review_note: reviewNote }, (result: DataRecord) => {
      const savedDecision = result?.review?.new_review_state || decision;
      const decisionText = label(savedDecision, REVIEW_LABELS);
      setLatestCandidateOutcome({ type: "success", confirmed: false, message: `已生效：候选已标记为“${decisionText}”`, description: savedDecision === "invalid" ? "系统将停止后续自动访问这一候选；历史材料和你的理由仍可审计。" : savedDecision === "observe" ? "该候选会保留在固定低频观察范围内，但不会自动确认事实或主动联系企业。" : "该候选会保留为待办；请在后续公开材料出现时再次阅读并决定是否请求重新核验。" });
      return { type: "success", text: `已生效：候选处理状态为“${decisionText}”。` };
    }) });
  }, [executeWrite]);

  /**
   * 只提交证据草稿的人工备注，要求后端重新抓取和核验后才确认入正式证据。
   *
   * 输入是服务器草稿记录，输出为确认任务弹窗。浏览器不发送草稿内的实体、URL、摘录、
   * 日期、类别、哈希或用户 ID；确认失败不会新建 evidence，成功后刷新候选与线索视图。
   */
  const openConfirmEvidenceDraft = React.useCallback((draft: DataRecord) => {
    const draftId = draft.draft_id ?? draft.evidence_draft_id;
    setObservationDrawerOpen(false);
    setTask({ key: `confirm-evidence-${draftId}`, title: "请求服务器重新核验", okText: "提交核验请求", noteLabel: "你已核对什么（必填）", description: "第 1 步：服务器使用草稿原有的网页链接重新访问页面。第 2 步：核对拟定主体、主体类型、页面日期、事实类别和原始摘录是否仍可见。第 3 步：仅当全部通过才新增正式证据。你只能说明已核对的内容，不能在此修改日期、实体、摘录或原始快照。", options: [CONFIRM_EVIDENCE_OPTION], submit: (_decision: string, reviewNote: string) => executeWrite(`/evidence-drafts/${encodeURIComponent(draftId)}/confirm`, "POST", { review_note: reviewNote }, (result: DataRecord) => {
      const audit = result?.audit ?? {};
      const isConfirmed = result?.confirmed === true || ["confirmed_and_captured", "confirmed_with_existing_evidence"].includes(audit.new_state);
      const evidenceId = typeof audit.evidence_id === "string" && audit.evidence_id ? `正式证据 ID：${audit.evidence_id}` : "请在“线索与证据”中查看服务器返回的正式记录。";
      if (isConfirmed) {
        setLatestCandidateOutcome({ type: "success", confirmed: true, message: "已生效：服务器重新核验通过，正式证据已追加", description: `${evidenceId}。台账已刷新；这不等于自动推进招商或对外联系。` });
        return { type: "success", text: "已生效：服务器重新核验通过，正式证据已追加。" };
      }
      const preflight = result?.preflight ?? {};
      const reason = typeof result?.message === "string" && result.message ? result.message : (typeof audit.capture_result === "string" ? audit.capture_result : "服务器未能完成重新核验");
      const captureNotStarted = preflight?.capture_started === false || audit.capture_attempted === false;
      const outcomeMessage = captureNotStarted ? "暂未生效：服务器未访问原页面" : "暂未生效：服务器未新增正式证据";
      const nextStep = typeof result?.verification?.next_step === "string" ? result.verification.next_step : "请查看材料页的失败原因，选择待补证、观察或无效；不要在浏览器补写证据字段。";
      setLatestCandidateOutcome({ type: "warning", confirmed: false, message: outcomeMessage, description: `原因：${reason}。下一步：${nextStep}` });
      return { type: "warning", text: `${outcomeMessage}：${reason}` };
    }) });
  }, [executeWrite]);

  /**
   * 为不完整证据草稿追加待补证、观察或无效的业务判断，不修改草稿或候选快照。
   *
   * 输入是服务器草稿记录，输出为限定枚举的任务弹窗。前端只传决定与备注，服务器认证身份
   * 决定审计操作人，且后端会拒绝把业务复核当作补造来源、摘录或事实字段的渠道。
   */
  const openEvidenceDraftBusinessReview = React.useCallback((draft: DataRecord) => {
    const draftId = draft.draft_id ?? draft.evidence_draft_id;
    setObservationDrawerOpen(false);
    setTask({ key: `review-evidence-${draftId}`, title: "暂不入证，处理草稿", description: "该草稿关键字段还不完整，不能补写实体、URL、摘录或采集时间。请选择保留补证、低频观察，或停止自动访问；需要新的事实只能来自后续合规公开材料。", options: CANDIDATE_DECISION_OPTIONS, submit: (decision: string, reviewNote: string) => executeWrite(`/evidence-drafts/${encodeURIComponent(draftId)}/business-review`, "POST", { decision, review_note: reviewNote }, (result: DataRecord) => {
      const savedDecision = result?.review?.new_state || decision;
      const decisionText = label(savedDecision, REVIEW_LABELS);
      setLatestCandidateOutcome({ type: "success", confirmed: false, message: `已生效：草稿业务判断为“${decisionText}”`, description: "该决定已记录到审计中，但没有新增正式证据；未来是否重新核验仍需由你或团队成员基于公开材料发起。" });
      return { type: "success", text: `已生效：草稿已标记为“${decisionText}”。` };
    }) });
  }, [executeWrite]);

  /**
   * 为既有评分快照录入真实业务结果，显式保持“学习建议须人工决定”的边界。
   *
   * 输入为服务器线索 ID、快照 ID、结果和备注，输出为追加审计后的刷新。请求没有权重、
   * 总分、证据或用户 ID，后端不会自动改规则、Skill 或推进线索；结果仅成为后续建议样本。
   */
  const submitOutcomeFeedback = React.useCallback(async (leadId: string, scoreSnapshotId: string, outcome: string, note: string) => {
    await executeWrite(`/scoring-rules/leads/${encodeURIComponent(leadId)}/outcome-feedback`, "POST", { score_snapshot_id: scoreSnapshotId, outcome, note }, "招商结果反馈已追加。", () => setOutcomeTarget(null));
  }, [executeWrite]);

  /**
   * 调用后端登记节点或关系边，所有人员和组织关系均须回链既有正式证据。
   *
   * 输入为表单模式、有限字段和已拆分的证据 ID，输出为刷新后的图谱。组件不提交人员联系
   * 方式、资产、资金承诺或用户 ID；后端会二次校验节点类型、关系、证据和线索关联，失败
   * 不会写入任何图谱记录或证据变更。
   */
  const submitGraphRegistration = React.useCallback(async (mode: string, values: DataRecord, evidenceIds: string[]) => {
    const body = mode === "node" ? { display_name: values.display_name.trim(), node_kind: values.node_kind.trim(), locality: values.locality.trim(), linked_lead_id: values.linked_lead_id.trim() || null, evidence_ids: evidenceIds, registration_reason: values.registration_reason.trim() } : { from_node_id: values.from_node_id.trim(), to_node_id: values.to_node_id.trim(), relationship_type: values.relationship_type.trim(), evidence_ids: evidenceIds, registration_reason: values.registration_reason.trim() };
    await executeWrite(mode === "node" ? "/relationship-graph/nodes" : "/relationship-graph/edges", "POST", body, mode === "node" ? "图谱节点已登记。" : "关系边已登记，仍需人工复核。", () => setGraphModalOpen(false));
  }, [executeWrite]);

  if (loading) {
    return React.createElement(Spin, { tip: "正在读取徐州招商共享台账…", style: { display: "block", margin: "18vh auto" } });
  }

  const loadErrors = Object.values(data).filter((item: any) => item?._loadError).map((item: any) => item._loadError);
  const tabItems = [
    { key: "overview", label: "招商总览", children: React.createElement(OverviewTab, { leadsData: data.leads, actionCenter: data.actionCenter, workSummary: data.workSummary, candidates: data.candidates, discoveryCandidates: data.discoveryCandidates, onOpenLead: openLead }) },
    { key: "leads", label: "证据与招商跟进", children: React.createElement(LeadsTab, { leadsData: data.leads, onOpenLead: openLead }) },
    { key: "analysis", label: "产业与来源", children: React.createElement(AnalysisTab, { metrics: data.metrics, sourceCoverage: data.sourceCoverage, subdirectionCoverage: data.subdirectionCoverage, monitorSchedule: data.monitorSchedule, outcomeSchedule: data.outcomeSchedule, onScheduleAction: scheduleAction, scheduling }) },
    { key: "relations", label: "候选与关系", children: React.createElement(SourcesAndRelationsTab, { candidates: data.candidates, discoveryCandidates: data.discoveryCandidates, trustedSources: data.trustedSources, graph: data.graph, relationshipQueue: data.relationshipQueue, onOpenObservation: openObservation, onOpenCorroboration: openCorroboration, onReviewCandidate: openDiscoveryCandidateReview, onWaiveXuzhou: openXuzhouScopeWaiver, latestOutcome: latestCandidateOutcome, onOpenLeads: () => setActiveTab("leads") }) },
    { key: "governance", label: "复核与治理", children: React.createElement(GovernanceTab, { trustedSources: data.trustedSources, publicSources: data.publicSources, sourceAudit: data.sourceAudit, scoringRules: data.scoringRules, scoringSuggestions: data.scoringSuggestions, recipeRules: data.recipeRules, recipeProposals: data.recipeProposals, actionCenter: data.actionCenter, relationshipDrafts: data.relationshipDrafts, coInvestmentDrafts: data.coInvestmentDrafts, graph: data.graph, team: data.team, onOpenSource: () => setSourceModalOpen(true), onOpenPublicSource: () => setPublicSourceModalOpen(true), onOpenRules: () => setRulesModalOpen(true), onOpenTeam: () => setTeamModalOpen(true), onOpenIntegrityMaintenance: () => setIntegrityModalOpen(true), onOpenGraphRegistration: () => setGraphModalOpen(true), onOpenSupplement: setSupplementEdge, onOpenTask: setTask, onWrite: executeWrite }) },
  ];
  return React.createElement(
    "main",
    { style: { maxWidth: 1440, margin: "0 auto", padding: "24px clamp(16px, 3vw, 40px) 48px", background: "linear-gradient(180deg, #f2f8f5 0, #ffffff 360px)", minHeight: "100%" } },
    React.createElement(
      "section",
      { style: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, marginBottom: 20, flexWrap: "wrap" } },
      React.createElement(Space, { direction: "vertical", size: 4 }, React.createElement(Title, { level: 2, style: { margin: 0, color: "#123f35", letterSpacing: ".02em" } }, "徐州市招商智能体工作台"), React.createElement(AntText, { type: "secondary" }, "机器找证据并留痕；业务人员决定是否跟进、由谁跟进和下一步怎么做。")),
      React.createElement(Space, { wrap: true }, React.createElement(Button, { size: "large", onClick: () => setGuideOpen(true) }, "使用手册"), React.createElement(Button, { type: "primary", size: "large", loading: refreshing, onClick: () => void refresh(), style: { background: "#0f766e" } }, "刷新共享台账")),
    ),
    loadErrors.length ? React.createElement(Alert, { type: "warning", showIcon: true, style: { marginBottom: 16 }, message: "部分非核心统计暂时无法读取", description: Array.from(new Set(loadErrors)).join("；") }) : null,
    React.createElement(Tabs, { activeKey: activeTab, onChange: setActiveTab, items: tabItems, size: "large", destroyInactiveTabPane: false }),
    React.createElement(LeadDrawer, { open: drawerOpen, onClose: () => setDrawerOpen(false), details: selectedDetails, report: selectedReport, outreach: selectedOutreach, followup: selectedFollowup, reportLoading, outreachLoading, followupLoading, onLoadReport: loadReport, onLoadOutreach: loadOutreach, onLoadFollowup: loadFollowup, onReview: setReviewLead, onApproveFollowup: setFollowupApprovalTarget, onSnapshot: createSnapshot, onOutcome: setOutcomeTarget }),
    React.createElement(ReviewModal, { lead: reviewLead, onClose: () => setReviewLead(null), onSubmit: submitReview, submitting: reviewSubmitting }),
    React.createElement(FollowupApprovalModal, { lead: followupApprovalTarget, followup: selectedFollowup, onClose: () => setFollowupApprovalTarget(null), onSubmit: submitFollowupApproval, submitting: followupSubmitting }),
    React.createElement(ObservationDrawer, { open: observationDrawerOpen, onClose: () => setObservationDrawerOpen(false), observation: selectedObservation, onConfirmDraft: openConfirmEvidenceDraft, onReviewDraft: openEvidenceDraftBusinessReview, onReviewCandidate: openDiscoveryCandidateReview, onWaiveXuzhou: openXuzhouScopeWaiver }),
    React.createElement(CorroborationDrawer, { open: corroborationDrawerOpen, onClose: () => setCorroborationDrawerOpen(false), details: selectedCorroboration }),
    React.createElement(TaskReviewModal, { task, onClose: () => setTask(null), submitting: mutationSubmitting }),
    React.createElement(XuzhouScopeWaiverModal, { candidate: xuzhouScopeCandidate, onClose: () => setXuzhouScopeCandidate(null), onSubmit: submitXuzhouScopeWaiver, submitting: mutationSubmitting }),
    React.createElement(TrustedSourceModal, { open: sourceModalOpen, onClose: () => setSourceModalOpen(false), submitting: mutationSubmitting, onSubmit: (body: DataRecord, reset: () => void) => executeWrite("/trusted-sources", "POST", body, "可信来源已登记并追加审计。", () => { reset(); setSourceModalOpen(false); }) }),
    React.createElement(PublicSourceSupplementModal, { open: publicSourceModalOpen, onClose: () => setPublicSourceModalOpen(false), submitting: mutationSubmitting, onSubmit: (body: DataRecord, reset: () => void) => executeWrite("/public-sources", "POST", body, (result: DataRecord) => ({ type: "success", text: result?.preflight?.detail || "服务器检测通过，公开来源已加入低频发现目录。" }), () => { reset(); setPublicSourceModalOpen(false); }) }),
    React.createElement(ScoringRulesModal, { open: rulesModalOpen, onClose: () => setRulesModalOpen(false), submitting: mutationSubmitting, ruleSet: data.scoringRules?.rule_set, onSubmit: (body: DataRecord) => executeWrite("/scoring-rules", "POST", body, "新的评分规则版本已创建。", () => setRulesModalOpen(false)) }),
    React.createElement(TeamMemberModal, { open: teamModalOpen, onClose: () => setTeamModalOpen(false), submitting: mutationSubmitting, onSubmit: (action: string, username: string) => executeWrite(action === "add" ? "/team/members" : "/team/members/remove", "POST", { username }, action === "add" ? "成员已加入共享台账团队。" : "成员已移出团队名册。", () => setTeamModalOpen(false)) }),
    React.createElement(IntegrityMaintenanceModal, { open: integrityModalOpen, onClose: () => setIntegrityModalOpen(false), submitting: mutationSubmitting, onSubmit: (action: string, expectedHash: string, verificationNote: string) => executeWrite(action === "migrate" ? "/evidence-integrity/migrate-legacy" : "/evidence-integrity/reseal-empty-ledger", "POST", action === "migrate" ? { expected_legacy_ledger_sha256: expectedHash, verification_note: verificationNote } : { expected_ledger_sha256: expectedHash, verification_note: verificationNote }, action === "migrate" ? "历史证据完整性迁移已完成。" : "空台账完整性签名恢复已完成。", () => setIntegrityModalOpen(false)) }),
    React.createElement(GraphRegistrationModal, { open: graphModalOpen, onClose: () => setGraphModalOpen(false), submitting: mutationSubmitting, onSubmit: submitGraphRegistration }),
    React.createElement(EvidenceSupplementModal, { edge: supplementEdge, onClose: () => setSupplementEdge(null), submitting: mutationSubmitting, onSubmit: (edgeId: string, evidenceIds: string[], supplementNote: string) => executeWrite(`/relationship-graph/edges/${encodeURIComponent(edgeId)}/evidence-supplements`, "POST", { evidence_ids: evidenceIds, supplement_note: supplementNote }, "关系边补证已追加。", () => setSupplementEdge(null)) }),
    React.createElement(OutcomeFeedbackModal, { target: outcomeTarget, onClose: () => setOutcomeTarget(null), onSubmit: submitOutcomeFeedback, submitting: mutationSubmitting }),
    React.createElement(UsageGuideDrawer, { open: guideOpen, onClose: () => setGuideOpen(false) }),
  );
}

/**
 * 将工作台路由注册到 Console 的插件菜单和路由表。
 *
 * 输入为空，输出为空。注册只声明前端页面位置，不创建后端身份、不会加载或修改台账数据；
 * 页面自身仍须通过认证 API 读取当前选中智能体的共享台账，未登录或未入团队名册者由服务器
 * 拒绝访问而不是由前端隐藏数据。
 */
function registerDashboardRoute(): void {
  window.QwenPaw.registerRoutes?.("xuzhou-investment-ledger", [
    {
      path: "/xuzhou-investment",
      component: XuzhouInvestmentDashboard,
      label: "徐州招商工作台",
      icon: "🏙️",
      priority: 35,
    },
  ]);
}

registerDashboardRoute();
