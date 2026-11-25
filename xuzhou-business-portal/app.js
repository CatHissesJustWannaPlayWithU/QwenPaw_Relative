const PLUGIN = "/api/xuzhou-investment-ledger";
const TOKEN_KEY = "xuzhou-business-token";
const USER_KEY = "xuzhou-business-user";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: storedUser(),
  authMode: "login",
  route: "overview",
  queueTab: "discovery",
  adminTab: "team",
  dashboard: null,
  dashboardLoading: false,
  empowerment: null,
  empowermentLoading: false,
  humanDraft: null,
  admin: null,
  adminLoading: false,
  modal: null,
  notice: null,
  chat: defaultChatEntries(),
  chatWorking: false,
  chatSessions: null,
  chatSessionsLoading: false,
  chatHistoryLoading: false,
  chatSessionId: "",
  activeChatId: "",
  chatError: "",
};

function storedUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

function defaultChatEntries() {
  return [
    {
      role: "assistant",
      text: "你好，我是徐州招商助手。你可以让我解释材料、梳理今天的人工待办、起草内部研判思路，或帮你准备和企业沟通前的问题清单。\n\n我不会替你作招商决定、确认企业意愿或自动联系任何企业；需要生效的业务判断，仍请在材料队列和招商跟进中由业务人员保存。",
    },
  ];
}

function currentUserId() {
  return String(state.user?.id || state.user?.username || "").trim();
}

function businessSessionPrefix() {
  return `xuzhou-business:${currentUserId() || "user"}:`;
}

function createBusinessSessionId() {
  const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  return `${businessSessionPrefix()}${nonce}`;
}

function isBusinessChat(value) {
  const sessionId = String(value?.session_id || "");
  const userId = currentUserId();
  return (
    String(value?.channel || "") === "console" &&
    (sessionId.startsWith(businessSessionPrefix()) || sessionId === `xuzhou-business-${userId}`)
  );
}

function apiUrl(path) {
  return `${window.QWENPAW_API_BASE || ""}${path}`;
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

function pick(value, names, fallback = "") {
  if (!value || typeof value !== "object") return fallback;
  for (const name of names) {
    if (value[name] !== undefined && value[name] !== null && value[name] !== "") {
      return value[name];
    }
  }
  return fallback;
}

function records(value, names) {
  if (Array.isArray(value)) return value;
  for (const name of names) {
    if (Array.isArray(value?.[name])) return value[name];
  }
  return [];
}

function itemId(value, names) {
  return String(pick(value, names, ""));
}

function candidateId(value) {
  return itemId(value, ["candidate_id", "id"]);
}

function observationId(value) {
  return itemId(value, ["observation_id", "id"]);
}

function leadId(value) {
  return itemId(value, ["lead_id", "id"]);
}

function draftId(value) {
  return itemId(value, ["draft_id", "evidence_draft_id", "id"]);
}

function label(value, fallback = "未命名材料") {
  return String(
    pick(value, ["entity_name", "company_name", "title", "source_title", "name"], fallback),
  ).trim() || fallback;
}

function escape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function sample(value, length = 160) {
  const text = String(
    pick(value, ["summary", "source_summary", "excerpt", "description", "visible_text"], ""),
  )
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "服务器只保存了基础索引信息。";
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function publicUrl(value) {
  try {
    const result = new URL(String(value));
    return ["http:", "https:"].includes(result.protocol) ? result.toString() : "";
  } catch {
    return "";
  }
}

function sourceDomain(value) {
  try {
    return new URL(String(value)).hostname || "公开网页";
  } catch {
    return "公开网页";
  }
}

function when(value) {
  if (!value) return "未提供";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  }).format(date);
}

function errorText(payload, fallback) {
  const detail = payload?.detail ?? payload?.message ?? payload?.error;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg || item?.message || String(item)).join("；");
  }
  return fallback;
}

function requestHeaders(json = false) {
  const result = {};
  if (state.token) result.Authorization = `Bearer ${state.token}`;
  if (json) result["Content-Type"] = "application/json";
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(apiUrl(path), {
    method: options.method || "GET",
    headers: requestHeaders(options.body !== undefined),
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(errorText(data, `请求未完成（HTTP ${response.status}）`));
    error.status = response.status;
    throw error;
  }
  return data;
}

function isAdmin() {
  return state.user?.role === "admin";
}

function writeSession(token, user) {
  state.token = token;
  state.user = user;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

function clearSession() {
  state.token = "";
  state.user = null;
  state.dashboard = null;
  state.empowerment = null;
  state.empowermentLoading = false;
  state.humanDraft = null;
  state.admin = null;
  state.modal = null;
  state.chat = defaultChatEntries();
  state.chatWorking = false;
  state.chatSessions = null;
  state.chatSessionsLoading = false;
  state.chatHistoryLoading = false;
  state.chatSessionId = "";
  state.activeChatId = "";
  state.chatError = "";
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

function notify(text, tone = "success") {
  state.notice = { text, tone };
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => {
    state.notice = null;
    render();
  }, 6500);
  render();
}

function candidateState(value) {
  const raw = pick(value, ["review_state", "business_review_state", "state"], "pending_user_review");
  return {
    pending_user_review: "等待人工判断",
    need_evidence: "需要补充依据",
    observe: "继续观察",
    invalid: "不纳入范围",
    waive_xuzhou: "无需徐州字段，待服务器核验",
  }[raw] || String(raw);
}

function leadState(value) {
  const raw = pick(value, ["review_state", "state", "status"], "pending_user_review");
  return {
    pending_user_review: "等待人工研判",
    advance: "已纳入人工跟进池",
    observe: "继续观察",
    invalid: "不纳入",
  }[raw] || String(raw);
}

function materialPriority(value) {
  return pick(value, ["review_priority", "priority"], "") === "high_priority_review"
    ? "材料高优先级"
    : "常规优先级";
}

function learnedReadingPriority(value) {
  const priority = value?.learning_priority;
  if (!priority || typeof priority !== "object") {
    return {
      label: "暂无学习样本",
      reason: "机器尚未获得足够的同来源历史记录；这不表示该页面价值较低。",
      tone: "neutral",
    };
  }
  const raw = String(priority.priority || "");
  return {
    label: String(priority.label || "暂无学习样本"),
    reason: String(priority.reason || "当前没有可解释的来源排序原因。"),
    tone: raw.includes("high") ? "green" : raw.includes("low") ? "warm" : "neutral",
  };
}

function geography(value) {
  const scope =
    pick(value, ["geographic_scope", "candidate_geographic_scope", "scope_assessment"], null) ||
    value?.eligibility_assessment ||
    {};
  const status = scope?.xuzhou_association_status || "";
  const priority = scope?.geographic_priority || "";
  let result = "地域待人工决定";
  if (status.includes("explicit") || priority === "high_with_xuzhou") {
    result = "有徐州字段（地域高优先）";
  }
  if (priority === "low_without_xuzhou") {
    result = "无徐州字段（地域低优先）";
  }
  if (status === "not_required_by_human") {
    result = "已人工决定无需徐州字段";
  }
  return { label: result, origin: scope?.enterprise_origin?.origin || value?.enterprise_origin?.origin || "" };
}

function selected(value, expected) {
  return String(value ?? "") === expected ? "selected" : "";
}

function humanAccessLabel(value) {
  return {
    public_page_accessible_to_human: "公开网页（我能正常查看）",
    login_or_member_access: "登录/成员权限内页面",
    dynamic_or_script_rendered_page: "动态页面（服务器未必能渲染）",
    robot_or_server_blocked: "服务器受 robots 或访问限制",
    authorized_file_or_export: "我有权使用的文件或导出材料",
    other_authorized_access: "其他已获授权的访问方式",
  }[String(value || "")] || "未说明访问方式";
}

function portalSector(value) {
  const text = String(value || "").toLowerCase();
  if (text.includes("半导体") || text.includes("semiconductor")) return "semiconductor";
  if (text.includes("具身") || text.includes("智能") || text.includes("embodied")) return "embodied_intelligence";
  if (text.includes("医药") || text.includes("生物") || text.includes("biomedicine")) return "biomedicine";
  return "other";
}

function sourceProfile(profile) {
  const profiles = {
    enterprise_official: {
      label: "企业官方网站",
      source_type: "enterprise_official",
      source_level: "primary",
      evidence_quality: 4,
      allowed_claim_types: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"],
    },
    fund_official: {
      label: "基金/投资机构官网",
      source_type: "fund_official",
      source_level: "primary",
      evidence_quality: 4,
      allowed_claim_types: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"],
    },
    government_investment_portal: {
      label: "政府招商公开入口",
      source_type: "government_investment_portal",
      source_level: "primary",
      evidence_quality: 4,
      allowed_claim_types: ["xuzhou_connection", "cooperation", "expansion", "industry_focus"],
    },
    official_statistics: {
      label: "官方统计公开入口",
      source_type: "official_statistics",
      source_level: "primary",
      evidence_quality: 5,
      allowed_claim_types: ["expansion", "industry_focus"],
    },
    university_alumni: {
      label: "高校校友/官方组织页面",
      source_type: "university_alumni",
      source_level: "primary",
      evidence_quality: 4,
      allowed_claim_types: ["xuzhou_connection", "cooperation", "industry_focus"],
    },
    industry_association: {
      label: "行业协会公开页面",
      source_type: "industry_association",
      source_level: "secondary",
      evidence_quality: 3,
      allowed_claim_types: ["cooperation", "expansion", "industry_focus", "fund_investment", "risk"],
    },
    verified_media: {
      label: "已核验媒体（仅作发现入口）",
      source_type: "verified_media",
      source_level: "secondary",
      evidence_quality: 3,
      allowed_claim_types: ["financing", "fund_raising", "xuzhou_connection", "cooperation", "expansion", "industry_focus", "fund_investment", "risk"],
    },
    human_added_public_source: {
      label: "人工补充公开网页（仅作发现入口）",
      source_type: "human_added_public_source",
      source_level: "lead_only",
      evidence_quality: 1,
      allowed_claim_types: ["industry_focus"],
    },
  };
  return profiles[profile] || profiles.enterprise_official;
}

function appTitle() {
  return {
    overview: "今日总览",
    queue: "材料队列",
    leads: "招商跟进",
    human: "人工材料入证",
    learning: "来源与学习",
    assistant: "智能对话",
    admin: "管理员",
  }[state.route] || "徐州招商";
}

function toast() {
  if (!state.notice) return "";
  return `<div class="toast ${state.notice.tone === "error" ? "error" : ""}" role="status">${escape(state.notice.text)}</div>`;
}

function loading(text) {
  return `<div class="loading"><span></span>${escape(text)}</div>`;
}

function blank(titleText, detail) {
  return `<section class="empty-state"><h3>${escape(titleText)}</h3><p>${escape(detail)}</p></section>`;
}

function authPage() {
  const registration = state.authMode === "register";
  return `
    <main class="auth-page">
      <section class="auth-intro">
        <span class="eyebrow">徐州招商 · 业务协同</span>
        <h1>让网页材料走向<br />可由人拍板的招商行动。</h1>
        <p>机器负责发现、留存、重新核验和记录审计；业务人员负责判断价值、指定负责人和完成实际业务工作。</p>
        <ol class="auth-steps">
          <li><strong>机器发现</strong><span>记录公开候选，标题不是企业事实。</span></li>
          <li><strong>服务器核验</strong><span>重新抓取并核对原页面，才可能形成正式证据。</span></li>
          <li><strong>人工招商决策</strong><span>决定是否跟进、由谁做、何时完成。</span></li>
        </ol>
      </section>
      <section class="auth-panel">
        <div class="auth-heading"><span class="brand-mark">徐</span><div><h2>${registration ? "创建业务账号" : "登录业务台"}</h2><p>${registration ? "注册后请管理员将你加入徐州招商团队。" : "使用现有 QwenPaw 账号登录。"}</p></div></div>
        <form id="${registration ? "register-form" : "login-form"}" class="form-stack">
          ${registration ? `<label>姓名<input name="name" required maxlength="100" autocomplete="name" placeholder="例如：王小明" /></label>` : ""}
          <label>用户名<input name="username" required minlength="3" maxlength="100" autocomplete="username" placeholder="输入平台用户名" /></label>
          <label>密码<input name="password" required minlength="8" type="password" autocomplete="${registration ? "new-password" : "current-password"}" placeholder="${registration ? "至少 8 位" : "输入密码"}" /></label>
          <button class="primary-button full" type="submit">${registration ? "创建并进入业务台" : "登录"}</button>
        </form>
        <p class="auth-switch">${registration ? "已经有账号？" : "还没有业务账号？"} <button class="link-button" data-action="${registration ? "show-login" : "show-register"}">${registration ? "返回登录" : "创建账号"}</button></p>
        <p class="tiny-note">页面不保存密码。账号权限、团队访问和全部业务写入均由 QwenPaw 服务端核验。</p>
      </section>
    </main>
    ${toast()}`;
}

function shell(content) {
  const nav = [
    ["overview", "总览", "看清今天做什么"],
    ["queue", "材料队列", "从发现到正式证据"],
    ["human", "人工材料入证", "我能合法提交的材料"],
    ["leads", "招商跟进", "人工审批与行动"],
    ["learning", "来源与学习", "让机器少做无效工作"],
    ["assistant", "智能对话", "台账工作与业务研判"],
    ...(isAdmin() ? [["admin", "管理员", "账号、团队与钉钉"]] : []),
  ];
  const name = state.user?.name || state.user?.username || "当前用户";
  return `
    <div class="portal-shell">
      <aside class="sidebar">
        <button class="brand" data-route="overview"><span class="brand-mark">徐</span><span><strong>徐州招商业务台</strong><small>业务人员工作入口</small></span></button>
        <nav class="main-nav">${nav.map(([route, text, hint]) => `<button class="nav-item ${state.route === route ? "active" : ""}" data-route="${route}"><strong>${text}</strong><small>${hint}</small></button>`).join("")}</nav>
        <div class="sidebar-foot"><div class="user-card"><span class="avatar">${escape(name.slice(0, 1))}</span><span><strong>${escape(name)}</strong><small>${isAdmin() ? "管理员" : "业务成员"}</small></span></div><button class="quiet-button" data-action="logout">退出登录</button></div>
      </aside>
      <main class="workspace">
        <header class="topbar"><button class="mobile-menu" data-action="toggle-menu" aria-label="菜单">☰</button><div><span>徐州招商</span><strong>${appTitle()}</strong></div><div class="topbar-actions"><button class="guide-button" data-action="open-guide">使用手册</button><button class="outline-button" data-action="refresh-all">刷新数据</button></div></header>
        <section class="page-content">${content}</section>
      </main>
    </div>
    ${toast()}
    ${renderModal()}`;
}

function accessHint() {
  const error = state.dashboard?.accessError;
  if (!error) return "";
  return `<section class="access-hint"><div><strong>还不能读取徐州共享台账</strong><p>${escape(error)}</p></div>${isAdmin() ? `<button class="outline-button" data-route="admin">打开团队名册</button>` : `<span>请联系管理员，将你的平台用户名加入“徐州招商团队名册”。</span>`}</section>`;
}

function empowermentAccessHint() {
  const error = state.empowerment?.accessError;
  if (!error) return "";
  return `<section class="access-hint"><div><strong>还不能读取徐州共享台账</strong><p>${escape(error)}</p></div>${isAdmin() ? `<button class="outline-button" data-route="admin">打开团队名册</button>` : `<span>请联系管理员，将你的平台用户名加入“徐州招商团队名册”。</span>`}</section>`;
}

function stats() {
  const summary = state.dashboard?.summary?.summary || state.dashboard?.summary || {};
  return {
    discoveries: state.dashboard?.discoveries?.length ?? pick(summary, ["discovery_candidate_count", "discovery_candidates"], 0),
    materials: state.dashboard?.observations?.length ?? pick(summary, ["candidate_observation_count", "candidate_observations"], 0),
    leads: state.dashboard?.leads?.length ?? pick(summary, ["lead_count", "leads"], 0),
    actions: state.dashboard?.actions?.length ?? pick(summary, ["human_action_count", "action_count", "pending_human_review_count"], 0),
  };
}

function metric(number, text, detail) {
  return `<article class="metric-card"><strong>${Number.isFinite(Number(number)) ? number : 0}</strong><span>${escape(text)}</span><small>${escape(detail)}</small></article>`;
}

function process(number, heading, owner, detail) {
  return `<article class="process-step"><span>${number}</span><h3>${escape(heading)}</h3><strong>${escape(owner)}负责</strong><p>${escape(detail)}</p></article>`;
}

function observationRow(value) {
  return `<button class="compact-row" data-action="open-observation" data-id="${escape(observationId(value))}"><span class="row-badge">${materialPriority(value).includes("高") ? "优先" : "材料"}</span><span><strong>${escape(label(value))}</strong><small>${escape(sample(value, 88))}</small></span><span class="row-arrow">›</span></button>`;
}

function leadRow(value) {
  return `<button class="compact-row" data-action="open-lead" data-id="${escape(leadId(value))}"><span class="row-badge green">线索</span><span><strong>${escape(label(value, "正式招商线索"))}</strong><small>${escape(leadState(value))} · ${escape(pick(value, ["sector", "industry"], "待归类"))}</small></span><span class="row-arrow">›</span></button>`;
}

function overview() {
  const count = stats();
  const observations = list(state.dashboard?.observations).slice(0, 4);
  const leads = list(state.dashboard?.leads).slice(0, 4);
  return `
    <section class="hero">
      <div><span class="eyebrow">今日工作入口</span><h1>先看材料，再由人决定下一步。</h1><p>“已发现”不等于企业事实，“正式证据”不等于企业同意落地。真正的招商跟进由业务人员完成判断、安排与沟通。</p></div>
      <div class="hero-actions"><button class="primary-button" data-route="queue">处理材料队列</button><button class="outline-button" data-route="assistant">询问徐州 Agent</button></div>
    </section>
    ${state.dashboardLoading ? loading("正在从共享台账读取数据…") : ""}
    ${accessHint()}
    <section class="metric-grid">
      ${metric(count.discoveries, "机器发现候选", "只有标题、链接和发现摘要，尚未代表企业事实。")}
      ${metric(count.materials, "已保存页面材料", "服务器已保存页面文本和哈希，等待人工阅读。")}
      ${metric(count.leads, "正式招商线索", "已通过服务器重新核验形成正式证据。")}
      ${metric(count.actions, "人工待办", "业务人员需要亲自判断、安排和后续行动。")}
    </section>
    <section class="process-panel">
      <div class="section-head"><div><span class="eyebrow">人机分工</span><h2>一条材料怎样进入招商跟进</h2></div><span class="small-tag">机器不代替人拍板</span></div>
      <div class="process-steps">
        ${process("01", "发现候选", "机器", "记录公开页面标题、链接、摘要和发现时间。")}
        ${process("02", "保存材料", "机器", "按来源规则保存页面文本、哈希和采集时间。")}
        ${process("03", "形成正式证据", "服务器 + 人", "业务人员提出确认，服务器重新抓取原页面再核验。")}
        ${process("04", "招商跟进审批", "业务人员", "逐项核对，指定负责人和下一步；实际联系由人完成。")}
      </div>
    </section>
    <section class="two-column">
      <article class="panel"><div class="section-head"><div><span class="eyebrow">优先阅读</span><h2>已保存页面材料</h2></div><button class="link-button" data-route="queue">查看全部</button></div>${observations.length ? `<div class="compact-list">${observations.map(observationRow).join("")}</div>` : blank("目前没有保存的页面材料", "机器发现后会在允许范围内保存材料；这不会自动产生正式证据。")}</article>
      <article class="panel"><div class="section-head"><div><span class="eyebrow">人工决定</span><h2>正式线索与跟进</h2></div><button class="link-button" data-route="leads">查看全部</button></div>${leads.length ? `<div class="compact-list">${leads.map(leadRow).join("")}</div>` : blank("目前还没有正式招商线索", "候选材料必须经服务器重新核验形成正式证据后，才会出现在这里。")}</article>
    </section>
  `;
}

function discoveryTable(values) {
  if (!values.length) return blank("暂时没有新的发现候选", "这不表示监控已停止，也不表示招商工作已经完成。");
  return `<div class="table-wrap"><table><thead><tr><th>候选页面</th><th>来源</th><th>机器阅读顺序</th><th>地域优先级</th><th>人工状态</th><th></th></tr></thead><tbody>
    ${values.map((value) => {
      const geo = geography(value);
      const learned = learnedReadingPriority(value);
      return `<tr><td><strong>${escape(label(value))}</strong><small>${escape(sample(value, 110))}</small></td><td>${escape(sourceDomain(pick(value, ["source_url", "url", "canonical_url"], "")))}</td><td><span class="status-pill ${learned.tone}" title="${escape(learned.reason)}">${escape(learned.label)}</span><small>${escape(learned.reason)}</small></td><td><span class="status-pill neutral">${escape(geo.label)}</span>${geo.origin ? `<small>来源地：${escape(geo.origin)}</small>` : ""}</td><td>${escape(candidateState(value))}</td><td><button class="primary-button small" data-action="open-discovery" data-id="${escape(candidateId(value))}">查看候选</button></td></tr>`;
    }).join("")}
  </tbody></table></div>`;
}

function observationTable(values) {
  if (!values.length) return blank("暂时没有已保存页面材料", "候选只有在服务器按来源规则成功保存后才会进入这里；没有材料时，不能请求确认入证。");
  return `<div class="table-wrap"><table><thead><tr><th>页面材料</th><th>阅读优先级</th><th>采集时间</th><th>人工状态</th><th></th></tr></thead><tbody>
    ${values.map((value) => `<tr><td><strong>${escape(label(value))}</strong><small>${escape(sample(value, 110))}</small></td><td><span class="status-pill ${materialPriority(value).includes("高") ? "warm" : "neutral"}">${escape(materialPriority(value))}</span></td><td>${escape(when(pick(value, ["captured_at", "collected_at", "observed_at", "created_at"], "")))}</td><td>${escape(candidateState(value))}</td><td><button class="primary-button small" data-action="open-observation" data-id="${escape(observationId(value))}">阅读材料</button></td></tr>`).join("")}
  </tbody></table></div>`;
}

function queue() {
  const discoveries = list(state.dashboard?.discoveries);
  const observations = list(state.dashboard?.observations);
  const active = state.queueTab;
  return `
    <section class="page-heading"><span class="eyebrow">从网页到证据</span><h1>材料队列</h1><p>候选和已保存材料是两个阶段。打开候选只会让你阅读标题和链接；打开材料才能阅读服务器保存的页面文本。任何“通过”都只会请求服务器重新核验，不会直接把网页写成事实。</p></section>
    ${accessHint()}
    <section class="queue-guide"><article><span>未保存页面材料</span><strong>已保存：标题、链接、发现摘要、发现时间</strong><p>你可以记录“补充依据、观察、无效”，或因高价值决定无需徐州字段；目前还没有页面文本、哈希或证据草稿。若开启学习，机器只会依据同来源历史结果调整阅读顺序，绝不替你作业务判断。</p></article><article><span>已保存页面材料</span><strong>新增保存：页面文本、内容哈希、采集时间</strong><p>阅读后才可以请求服务器重新抓取并核验；只有成功时才会新增服务器核验材料。你本人能合法打开但服务器不能访问的页面，可转到“人工材料入账”。</p></article></section>
    <div class="tab-bar"><button class="${active === "discovery" ? "active" : ""}" data-action="set-queue" data-tab="discovery">机器发现候选 <span>${discoveries.length}</span></button><button class="${active === "material" ? "active" : ""}" data-action="set-queue" data-tab="material">已保存页面材料 <span>${observations.length}</span></button></div>
    <section class="panel queue-panel"><div class="section-head"><div><span class="eyebrow">${active === "discovery" ? "第 1 步 · 发现" : "第 2 步 · 保存"}</span><h2>${active === "discovery" ? "尚未保存页面材料的候选" : "等待人工阅读的页面材料"}</h2></div><button class="outline-button" data-action="refresh-dashboard">刷新队列</button></div>${state.dashboardLoading ? loading("正在读取材料队列…") : active === "discovery" ? discoveryTable(discoveries) : observationTable(observations)}</section>
  `;
}

function leadCard(value) {
  const evidenceCount = pick(value, ["evidence_count", "formal_evidence_count"], 0);
  return `<article class="lead-card"><span class="status-pill green">${escape(leadState(value))}</span><h3>${escape(label(value, "正式招商线索"))}</h3><p>${escape(sample(value, 160))}</p><dl><div><dt>产业</dt><dd>${escape(pick(value, ["sector", "industry"], "待归类"))}</dd></div><div><dt>可追溯入账材料</dt><dd>${Number.isFinite(Number(evidenceCount)) ? evidenceCount : 0} 条</dd></div></dl><button class="primary-button full" data-action="open-lead" data-id="${escape(leadId(value))}">查看审批与跟进</button></article>`;
}

function leads() {
  const values = list(state.dashboard?.leads);
  return `
    <section class="page-heading"><span class="eyebrow">人工招商决策</span><h1>招商跟进</h1><p>有可追溯入账材料的线索会出现在这里：材料可能是服务器抓取，也可能是业务人员合法提交。页面会明确区分两者；业务人员仍需逐项核对招商价值、负责人和下一步，系统不会自动联系企业，也不会把审批写成企业承诺。</p></section>
    ${accessHint()}
    <section class="approval-explainer"><div><strong>机器做什么</strong><p>保留正式证据、检查证据是否存在、提示风险并记录审计。</p></div><div><strong>业务人员做什么</strong><p>核对主体和事实，判断是否值得跟进，指定负责人、目标日期和实际行动。</p></div><div><strong>审批通过表示什么</strong><p>仅表示纳入内部人工跟进池，不表示企业已同意来徐州、签约或投资。</p></div></section>
    <section class="panel"><div class="section-head"><div><span class="eyebrow">可追溯材料后的队列</span><h2>可由业务人员研判的招商线索</h2></div><button class="outline-button" data-action="refresh-dashboard">刷新线索</button></div>${state.dashboardLoading ? loading("正在读取招商线索…") : values.length ? `<div class="lead-grid">${values.map(leadCard).join("")}</div>` : blank("尚未形成可研判招商线索", "候选材料不是正式证据。你可以在材料队列请求服务器核验，或在“人工材料入账”登记自己有权使用的材料。")}</section>
  `;
}

function humanEvidenceRow(value) {
  const nestedEvidence = value.evidence || {};
  const evidence = Object.keys(nestedEvidence).length ? nestedEvidence : value;
  const source = publicUrl(pick(evidence, ["source_url", "url"], pick(value, ["source_url", "url"], "")));
  const attachment = value.attachment || {};
  return `<article class="human-material-row"><div><span class="status-pill sky">人工授权材料</span><h3>${escape(label(evidence, label(value, "未命名材料")))}</h3><p>${escape(sample(evidence, 220))}</p><small>提交人：${escape(pick(value, ["submitted_by_user_id", "created_by_user_id"], "服务器记录"))} · 提交时间：${escape(when(pick(value, ["submitted_at", "created_at"], "")))}</small><small>访问方式：${escape(humanAccessLabel(pick(value, ["access_mode"], evidence?.human_attestation?.access_mode || "")))}${attachment.present ? " · 已附关键材料" : ""}</small></div><div class="human-material-actions">${source ? `<a class="outline-button small" href="${escape(source)}" target="_blank" rel="noopener noreferrer">打开原网页</a>` : ""}<span>服务器未代替你登录或抓取</span></div></article>`;
}

function learningScheduleCard(value) {
  const schedule = value && typeof value === "object" ? value : {};
  const configured = schedule.configured === true;
  const enabled = configured && schedule.enabled === true;
  const hostedElsewhere = !configured && schedule.team_host_hint?.team_last_action === "enabled";
  const statusLabel = enabled ? "每日自动学习已启用" : hostedElsewhere ? "团队计划由其他智能体托管" : "每日自动学习尚未启用";
  const detail = enabled
    ? "每天 03:35（北京时间）读取已记录的结果与来源表现，自动更新阅读顺序；不会访问网页、创建证据或联系企业。"
    : hostedElsewhere
      ? `团队最近一次启用记录在 ${when(schedule.team_host_hint?.team_last_operated_at || "")}。为避免重复运行，请在原托管智能体或由管理员统一管理。`
      : "启用后每天 03:35（北京时间）运行一次受限学习。不会立刻运行，也不会替你改变任何企业结论。";
  return `<section class="panel learning-schedule"><div class="section-head"><div><span class="eyebrow">减少重复整理</span><h2>每日学习节奏</h2></div><span class="status-pill ${enabled ? "green" : hostedElsewhere ? "warm" : "neutral"}">${escape(statusLabel)}</span></div><p class="plain-copy">${escape(detail)}</p><div class="button-row">${enabled ? `<button class="danger-button small" data-action="pause-learning-schedule">暂停每日学习</button>` : hostedElsewhere ? "" : `<button class="primary-button small" data-action="enable-learning-schedule">启用每日学习</button>`}</div></section>`;
}

function publicSourceStatusLabel(value) {
  return {
    ready_for_public_crawl: "服务器可直接抓取",
    robots_disallowed: "网站不允许机器抓取",
    login_or_paid_access: "需要登录/会员",
    unsupported_api_or_credentials: "疑似 API 或带凭据入口",
    temporarily_unreachable: "暂时无法访问",
    unsafe_target: "未通过安全检查",
  }[String(value || "")] || String(value || "待检测");
}

function publicSourceStatusTone(value) {
  const status = String(value || "");
  if (status === "ready_for_public_crawl") return "green";
  if (["robots_disallowed", "login_or_paid_access", "unsupported_api_or_credentials", "unsafe_target"].includes(status)) return "warm";
  return "neutral";
}

function publicSourceSupplementSection(value, loadError = "") {
  const sources = list(value);
  const rows = sources.map((source) => {
    const sourceUrl = publicUrl(source.seed_url || "");
    const crawlStatus = String(source.crawl_status || "");
    const nextStatus = source.status === "active" ? "disabled" : "active";
    return `<tr><td>${sourceUrl ? `<a class="link-button" href="${escape(sourceUrl)}" target="_blank" rel="noopener noreferrer">${escape(source.domain || sourceUrl)}</a>` : escape(source.domain || "未提供")}<small>${escape(source.seed_url || "未提供入口")}</small></td><td><span class="status-pill ${publicSourceStatusTone(crawlStatus)}">${escape(publicSourceStatusLabel(crawlStatus))}</span><small>${escape(when(source.last_preflight_at || source.created_at || ""))}</small></td><td><span class="status-pill ${source.status === "active" ? "green" : "neutral"}">${source.status === "active" ? "自动发现已启用" : "已暂停"}</span></td><td><small>${escape(source.crawl_detail || "尚未记录检测说明")}</small></td><td><div class="button-row compact"><button class="outline-button small" data-action="retest-public-source" data-id="${escape(String(source.source_id || ""))}">重新检测</button><button class="outline-button small" data-action="toggle-public-source" data-id="${escape(String(source.source_id || ""))}" data-status="${escape(nextStatus)}">${nextStatus === "active" ? "重新检测并启用" : "暂停自动发现"}</button></div></td></tr>`;
  }).join("");
  return `<section class="panel public-source-supplement"><div class="section-head"><div><span class="eyebrow">可信来源目录 · 人工补充</span><h2>添加机器可直接读取的公开网页</h2></div><span class="small-tag">${sources.length} 条，最多启用 8 条</span></div><p class="plain-copy">这里适合填写希望机器定期查看的公开网页入口，例如政府公告栏目、企业新闻页或行业协会资讯页。点击“检测并加入”后，服务器会先检查网址格式、robots 规则、是否需要登录/付费、是否能安全读取以及是否跳到别的域名；检测失败不会写入目录。</p><div class="public-source-boundary"><strong>通过检测代表什么：</strong><span>服务器能读取这个公开入口。后续最多发现同域少量 RSS、Sitemap 或首页链接，生成的仍是待人工核验候选，不会自动形成正式证据、招商结论或联系企业。</span><strong>不能填写什么：</strong><span>不要填 API 地址、带 token/签名参数的链接、Cookie、账号或付费接口。付费 API 必须由开发者在后端源码和密钥配置中按供应商许可接入。</span></div><form id="public-source-form" class="form-stack decision-form"><label>公开网页完整网址<input name="source_url" type="url" required maxlength="2000" placeholder="例如：https://example.gov.cn/news" /></label><label>为什么要加入（可选）<textarea name="note" rows="2" maxlength="1000" placeholder="例如：徐州经开区招商公告栏目，适合发现项目动态。"></textarea></label><button class="primary-button" type="submit">检测并加入公开来源</button></form>${loadError ? `<div class="source-inline-error">${escape(loadError)}</div>` : ""}${sources.length ? `<div class="table-wrap public-source-table"><table><thead><tr><th>入口网址</th><th>服务器检测</th><th>当前状态</th><th>最近说明</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : blank("还没有人工补充公开网页", "先填写一个无需登录、无需密钥的公开网页。服务器检测通过后，入口才会出现在这里并参与低频候选发现。")}</section>`;
}

function humanMaterials() {
  const empowerment = state.empowerment || {};
  const human = empowerment.human || {};
  const items = list(human.items);
  const draft = state.humanDraft || {};
  const sourceDate = String(draft.source_published_at || "").slice(0, 10);
  return `
    <section class="page-heading"><span class="eyebrow">业务人员的直接授权</span><h1>人工材料入账</h1><p>如果你能合法打开网页、登录组织账号查看页面，或持有有权使用的导出材料，而服务器无法抓取，你可以直接登记关键材料。系统会记录提交人、时间、原文哈希和访问方式，但会明确标记为“人工提交”，不会假装服务器已经访问过该网页。</p></section>
    ${state.empowermentLoading ? loading("正在读取人工材料与授权边界…") : ""}
    ${empowermentAccessHint()}
    <section class="empowerment-hero">
      <div><span class="eyebrow">你的权限</span><h2>把你有权看到的材料，带进团队台账。</h2><p>可提交你已获授权使用的网页摘录、页面日期、关键截图、PDF 或文本导出。提交后可供人工招商研判，不会直接改变机器评分、风险告警或成为“服务器抓取事实”。</p></div>
      <div class="empowerment-guard"><strong>绝不提交</strong><span>密码、Cookie、访问令牌、他人账号信息、受保护个人信息或你无权保存的材料。</span></div>
    </section>
    <section class="permission-grid">
      <article><span>01</span><h3>你做什么</h3><p>阅读原网页或授权材料，摘出对招商判断真正有用的内容，写清楚来源、访问方式和你为什么有权使用。</p></article>
      <article><span>02</span><h3>机器做什么</h3><p>保存不可编辑的材料包、时间和哈希，标注为人工提交；不会拿你的登录权限去访问网页，也不会替你判断企业事实。</p></article>
      <article><span>03</span><h3>如何进入业务动作</h3><p>由业务人员再核对主体、关键事项和交叉来源，填写负责人和下一步审批。人工材料可以支持研判，但不能被自动升级成服务器证据。</p></article>
    </section>
    <section class="two-column materials-layout">
      <article class="panel material-submission"><div class="section-head"><div><span class="eyebrow">提交前请核对</span><h2>登记一份人工授权材料</h2></div><span class="small-tag">团队可审计</span></div>
        <form id="human-evidence-form" class="form-stack">
          <div class="form-grid"><label>企业/机构名称<input name="entity_name" required maxlength="500" value="${escape(draft.entity_name || "")}" placeholder="例如：某某科技" /></label><label>主体类型<select name="entity_kind"><option value="enterprise" ${selected(draft.entity_kind, "enterprise")}>企业</option><option value="fund" ${selected(draft.entity_kind, "fund")}>基金/投资机构</option><option value="startup_team" ${selected(draft.entity_kind, "startup_team")}>创业团队</option></select></label></div>
          <div class="form-grid"><label>所在产业<select name="sector"><option value="semiconductor" ${selected(draft.sector, "semiconductor")}>半导体</option><option value="embodied_intelligence" ${selected(draft.sector, "embodied_intelligence")}>具身智能</option><option value="biomedicine" ${selected(draft.sector, "biomedicine")}>生物医药</option><option value="other" ${selected(draft.sector, "other")}>其他（请在摘要说明）</option></select></label><label>材料主要支持什么<select name="claim_type"><option value="financing">融资动态</option><option value="fund_raising">基金募资</option><option value="xuzhou_connection">徐州关联</option><option value="cooperation">合作线索</option><option value="expansion">扩张/项目动态</option><option value="industry_focus">产业方向</option><option value="fund_investment">基金投资</option><option value="risk">需留意风险</option></select></label></div>
          <label>原网页或材料来源链接<input name="source_url" type="url" required maxlength="2000" value="${escape(draft.source_url || "")}" placeholder="https://…（只登记你有权使用的来源）" /></label>
          <div class="form-grid"><label>页面/文件标题<input name="source_title" required maxlength="500" value="${escape(draft.source_title || "")}" placeholder="按页面或文件原题填写" /></label><label>页面显示日期（没有可留空）<input name="source_published_at" type="date" value="${escape(sourceDate)}" /></label></div>
          <div class="form-grid"><label>你是如何访问到的<select name="access_mode"><option value="public_page_accessible_to_human" ${selected(draft.access_mode, "public_page_accessible_to_human")}>公开网页，我能正常查看</option><option value="login_or_member_access" ${selected(draft.access_mode, "login_or_member_access")}>登录/成员权限内页面</option><option value="dynamic_or_script_rendered_page" ${selected(draft.access_mode, "dynamic_or_script_rendered_page")}>动态网页，服务器未必能渲染</option><option value="robot_or_server_blocked" ${selected(draft.access_mode, "robot_or_server_blocked")}>服务器受 robots 或访问限制</option><option value="authorized_file_or_export" ${selected(draft.access_mode, "authorized_file_or_export")}>我有权使用的文件/导出材料</option><option value="other_authorized_access" ${selected(draft.access_mode, "other_authorized_access")}>其他已获授权的方式</option></select></label><label>来源属性<select name="source_level"><option value="primary">一手/主要来源</option><option value="secondary">二手来源（需交叉核对）</option><option value="lead_only">仅作发现线索</option></select></label></div>
          <label>你对材料完整性的判断<select name="evidence_quality"><option value="5">5 · 原始页面/正式文件，关键段落完整</option><option value="4" selected>4 · 来源清晰，关键段落完整</option><option value="3">3 · 有价值但需交叉核对</option><option value="2">2 · 仅供发现线索</option><option value="1">1 · 仅作待查提示</option></select></label>
          <label>关键摘录（必填，最多 2,000 字）<textarea name="excerpt" rows="5" required maxlength="2000" placeholder="只摘录支持本次招商判断的原文或准确摘要；不要粘贴密码、Cookie、个人敏感信息。">${escape(draft.excerpt || "")}</textarea></label>
          <label>补充说明（可选，最多 12,000 字）<textarea name="material_text" rows="4" maxlength="12000" placeholder="说明页面上下文、你看到的限制、需要交叉核对的点；这不是对企业事实的自动确认。">${escape(draft.material_text || "")}</textarea></label>
          <label>关键附件（可选，PDF/JPG/PNG/WEBP/TXT，最大 512KB）<input name="attachment" type="file" accept="application/pdf,image/jpeg,image/png,image/webp,text/plain" /><small>附件只保存关键截图、PDF 或文本导出；不上传浏览器配置、账号信息或大文件。</small></label>
          <fieldset class="tag-fieldset"><legend>如适用，勾选徐州关联线索（可多选）</legend><label><input type="checkbox" name="xuzhou_association_tags" value="ancestry_or_birthplace" /> 籍贯/出生地公开关联</label><label><input type="checkbox" name="xuzhou_association_tags" value="founder_or_team" /> 创始人或团队公开关联</label><label><input type="checkbox" name="xuzhou_association_tags" value="university_alumni" /> 高校校友公开关联</label><label><input type="checkbox" name="xuzhou_association_tags" value="organization_cooperation" /> 组织/项目公开合作</label><label><input type="checkbox" name="xuzhou_association_tags" value="industry_chain" /> 产业链/业务关联</label></fieldset>
          <label class="checkline human-attestation"><input type="checkbox" name="attestation_confirmed" required /><span><strong>我确认：我有权保存和用于内部招商研判这份材料。</strong><small>我没有提交密码、Cookie、访问令牌、受保护个人信息或无权使用的内容；我理解它会被标记为人工提交，而不是服务器抓取。</small></span></label>
          <div class="button-row"><button class="primary-button" type="submit">登记为人工授权材料</button><button class="outline-button" type="button" data-route="queue">返回材料队列</button></div>
        </form>
      </article>
      <article class="panel material-history"><div class="section-head"><div><span class="eyebrow">团队已登记</span><h2>最近人工材料</h2></div><span class="small-tag">${Number(human.count || items.length)} 份</span></div>${items.length ? `<div class="human-material-list">${items.slice(0, 12).map(humanEvidenceRow).join("")}</div>` : blank("尚未登记人工授权材料", "当服务器无法访问、但你能合法查看时，可以在左侧把关键材料纳入可追溯台账。")}</article>
    </section>
  `;
}

function learning() {
  const empowerment = state.empowerment || {};
  const learningData = empowerment.learning || {};
  const policy = learningData.policy || {};
  const sources = list(empowerment.sources);
  const catalogSources = sources.filter((source) => source?.source_type !== "human_added_public_source");
  const publicSources = list(empowerment.publicSources);
  const signals = list(learningData.source_signals);
  const learningSchedule = empowerment.learningSchedule || {};
  const diversity = learningData.source_diversity || {};
  const automatic = learningData.automatic_actions || {};
  const recommended = String(policy.mode || "adaptive_ranking");
  return `
    <section class="page-heading"><span class="eyebrow">少做重复劳动，保留人的决定权</span><h1>来源与学习</h1><p>机器从已保存材料、人工审批和实际结果中学习“下一批先看什么来源”；你仍能登记、停用来源并选择自动化范围。学习永远不会自动新增网站、关键词、产业范围、正式证据或对外联系。</p></section>
    ${state.empowermentLoading ? loading("正在读取来源目录与学习状态…") : ""}
    ${empowermentAccessHint()}
    <section class="learning-boundary"><div><strong>机器现在可做</strong><p>${automatic.queue_sorting ? "按已记录结果调整队列阅读优先级" : "只展示建议，不改变队列顺序"}${automatic.fixed_recipe_ordering ? "；按已授权策略重排现有固定来源配方" : "；不改变固定来源配方"}。</p></div><div><strong>机器永远不能做</strong><p>机器不能擅自新增网站、登录你的网页、绕过 robots/访问限制、把材料自动入证、替你决定招商结论或自动联系企业。只有人可以在下方提交公开网址，且必须先通过服务器检测。</p></div></section>
    ${diversity.status === "concentrated_needs_more_human_selected_sources" ? `<section class="source-alert"><strong>来源集中提醒</strong><p>${escape(diversity.message || "近期候选集中于单一来源。")} 当前主要来源：${escape(diversity.top_source_domain || "未提供")}，占 ${Math.round(Number(diversity.top_source_share || 0) * 100)}%。你可以在下方登记或启用其他已授权公开域名。</p></section>` : ""}
    <section class="two-column learning-layout">
      <article class="panel"><div class="section-head"><div><span class="eyebrow">人定义自动化边界</span><h2>学习策略</h2></div><span class="small-tag">${escape(policy.mode_label || "自动排序")}</span></div><p class="plain-copy">${escape(learningData.policy_boundary || "默认依据历史结果调整队列排序，不自动扩大任何来源范围。")}</p><form id="learning-policy-form" class="form-stack decision-form"><label>让机器自动做到哪一步<select name="mode"><option value="manual_advice" ${selected(recommended, "manual_advice")}>只给建议，不自动排序</option><option value="adaptive_ranking" ${selected(recommended, "adaptive_ranking")}>自动调整业务队列优先级（推荐）</option><option value="adaptive_ranking_and_recipe_order" ${selected(recommended, "adaptive_ranking_and_recipe_order")}>自动排序，并重排现有固定来源配方</option></select></label><label>为什么调整（必填，会写入审计）<textarea name="change_reason" rows="3" required maxlength="2000" placeholder="例如：近期候选来源过于集中，希望先自动调整阅读顺序，但不扩大来源范围。"></textarea></label><button class="primary-button" type="submit">保存学习策略</button></form></article>
      <article class="panel"><div class="section-head"><div><span class="eyebrow">人工补齐来源多样性</span><h2>登记可信公开域名</h2></div><span class="small-tag">未来分级用</span></div><p class="plain-copy">登记域名只影响未来材料的来源分级；不会立刻抓取，也不会改写历史证据。服务器仍按公开访问规则、robots 和安全限制决定能否访问。</p><form id="trusted-source-form" class="form-stack decision-form"><label>公开域名<input name="domain" required maxlength="253" placeholder="例如：example.com（不能填写页面路径）" /></label><label>来源类型<select name="profile"><option value="enterprise_official">企业官方网站</option><option value="fund_official">基金/投资机构官网</option><option value="government_investment_portal">政府招商公开入口</option><option value="official_statistics">官方统计公开入口</option><option value="university_alumni">高校校友/官方组织页面</option><option value="industry_association">行业协会公开页面</option><option value="verified_media">已核验媒体（只作发现入口）</option></select></label><label>登记理由（必填）<textarea name="change_reason" rows="3" required maxlength="2000" placeholder="说明为什么该公开域名适合纳入未来来源目录。"></textarea></label><button class="outline-button" type="submit">登记可信来源</button></form></article>
    </section>
    ${publicSourceSupplementSection(publicSources, empowerment.publicSourcesError)}
    ${learningScheduleCard(learningSchedule)}
    <section class="panel signal-panel"><div class="section-head"><div><span class="eyebrow">机器学到了什么</span><h2>来源表现与排序解释</h2></div><span class="small-tag">最少 ${Number(learningData.minimum_sample_count || 3)} 个样本才会影响优先级</span></div>${signals.length ? `<div class="table-wrap"><table><thead><tr><th>域名</th><th>当前学习结果</th><th>样本</th><th>服务器材料</th><th>人工材料</th><th>已记录结果</th><th>为什么</th></tr></thead><tbody>${signals.map((signal) => `<tr><td><strong>${escape(signal.domain || "未提供")}</strong></td><td><span class="status-pill ${String(signal.priority || "").includes("high") ? "green" : String(signal.priority || "").includes("low") ? "warm" : "neutral"}">${escape(signal.label || "待学习")}</span></td><td>${Number(signal.sample_count || 0)}</td><td>${Number(signal.server_captured_evidence_count || 0)}</td><td>${Number(signal.human_attested_material_count || 0)}</td><td>推进 ${Number(signal.positive_progress_count || 0)} · 落地 ${Number(signal.landed_count || 0)} · 未转化/无效 ${Number(signal.not_converted_count || 0) + Number(signal.invalidated_count || 0)}</td><td><small>${escape(signal.reason || "当前没有可解释原因")}</small></td></tr>`).join("")}</tbody></table></div>` : blank("尚无足够来源学习样本", "当团队积累已保存材料、审批或实际结果后，这里会解释机器如何调整阅读优先级。")}</section>
    <section class="panel source-catalog"><div class="section-head"><div><span class="eyebrow">由人维护，机器不擅自扩展</span><h2>可信来源目录</h2></div><span class="small-tag">${catalogSources.length} 条分级配置</span></div>${catalogSources.length ? `<div class="table-wrap"><table><thead><tr><th>域名</th><th>类型</th><th>等级</th><th>状态</th><th>允许支持的事项</th><th></th></tr></thead><tbody>${catalogSources.map((source) => `<tr><td><strong>${escape(source.domain || "未提供")}</strong><small>由 ${escape(source.created_by_user_id || "系统") } 登记</small></td><td>${escape(sourceProfile(source.source_type).label)}</td><td>${escape(source.source_level === "primary" ? "一手/主要来源" : "二手/发现来源")}</td><td><span class="status-pill ${source.status === "active" ? "green" : "neutral"}">${source.status === "active" ? "已启用" : escape(source.status || "未知")}</span></td><td><small>${escape(list(source.allowed_claim_types).join("、") || "按类型限制")}</small></td><td>${["active", "disabled"].includes(String(source.status || "")) ? `<button class="outline-button small" data-action="toggle-source" data-id="${escape(String(source.source_id || ""))}" data-status="${escape(String(source.status || ""))}" data-domain="${escape(String(source.domain || ""))}">${source.status === "active" ? "停用" : "重新启用"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : blank("尚未登记可信来源", "请只登记团队认为适合未来公开发现与分级的域名；登记不会立即抓取网页。")}</section>
  `;
}

function chatSessionTitle(value) {
  const name = String(pick(value, ["name", "title"], "")).trim();
  if (name && !/^new (chat|conversation)$/i.test(name)) return name;
  return `业务对话 · ${when(pick(value, ["updated_at", "created_at"], ""))}`;
}

function chatMessage(entry) {
  const user = entry.role === "user";
  const waiting = Boolean(entry.streaming && !entry.text);
  return `<article class="conversation-message ${user ? "from-user" : "from-agent"}"><span class="conversation-avatar ${user ? "user" : "agent"}">${user ? "我" : "徐"}</span><div class="conversation-bubble"><div class="conversation-message-meta"><strong>${user ? "我" : "徐州 Agent"}</strong>${entry.streaming ? `<span>正在回答</span>` : ""}</div><p>${waiting ? `<span class="answering"><i></i><i></i><i></i>正在整理回答</span>` : escape(entry.text || "")}</p></div></article>`;
}

function assistant() {
  const sessions = list(state.chatSessions);
  const active = sessions.find((entry) => String(entry.session_id || "") === state.chatSessionId);
  const currentTitle = active ? chatSessionTitle(active) : "新建业务对话";
  const sessionItems = state.chatSessionsLoading
    ? `<div class="conversation-rail-loading">正在读取你的历史会话…</div>`
    : sessions.length
      ? sessions.map((entry) => {
          const selected = String(entry.session_id || "") === state.chatSessionId;
          return `<article class="conversation-session ${selected ? "selected" : ""}"><button class="conversation-session-main" data-action="open-chat" data-id="${escape(String(entry.id || ""))}"><strong>${escape(chatSessionTitle(entry))}</strong><small>${escape(when(pick(entry, ["updated_at", "created_at"], "")))}</small></button><button class="archive-session" data-action="archive-chat" data-id="${escape(String(entry.id || ""))}" aria-label="归档此会话" title="归档此会话">×</button></article>`;
        }).join("")
      : `<div class="conversation-rail-empty"><strong>还没有已保存的对话</strong><span>发送第一条消息后，系统会自动保存到你自己的会话历史。</span></div>`;
  return `
    <section class="conversation-intro">
      <div><span class="eyebrow">业务研判对话中心</span><h1>和徐州 Agent 讨论下一步</h1><p>用自然语言理解台账、拆解待办、准备内部研判。对话记录仅属于当前登录账号，不能替代证据核验或人工招商审批。</p></div>
      <div class="conversation-protection"><span>账号专属</span><strong>服务端鉴权与会话隔离</strong><small>不会显示其他用户的业务对话</small></div>
    </section>
    <section class="conversation-workbench" aria-label="徐州 Agent 对话工作台">
      <aside class="conversation-rail">
        <button class="new-conversation" data-action="new-chat" ${state.chatWorking ? "disabled" : ""}><span>＋</span>新建业务对话</button>
        <div class="conversation-rail-title"><span>我的会话</span><div><small>${sessions.length} 个已保存</small><button class="refresh-conversations" data-action="refresh-chats" title="刷新会话列表" aria-label="刷新会话列表">↻</button></div></div>
        <div class="conversation-session-list">${sessionItems}</div>
        <article class="conversation-boundary"><span>使用边界</span><p>Agent 可以解释和起草；是否入证、是否跟进、由谁联系企业，必须由业务人员在工作台中另行确认。</p></article>
      </aside>
      <section class="conversation-canvas">
        <header class="conversation-header"><div><span class="eyebrow">当前会话</span><h2>${escape(currentTitle)}</h2><p>${state.activeChatId ? "已由服务端保存，可在左侧继续查看。" : "发送第一条消息后会自动生成并保存会话。"}</p></div><div class="conversation-header-actions">${state.chatWorking ? `<button class="outline-button stop-answer" data-action="stop-chat" ${state.activeChatId ? "" : "disabled"}>停止回答</button>` : ""}${state.activeChatId ? `<button class="quiet-button archive-current" data-action="archive-chat" data-id="${escape(state.activeChatId)}">归档会话</button>` : ""}</div></header>
        ${state.chatError ? `<div class="conversation-error">${escape(state.chatError)}</div>` : ""}
        <div id="chat-messages" class="conversation-messages" aria-live="polite">${state.chatHistoryLoading ? loading("正在恢复这段会话…") : state.chat.map(chatMessage).join("")}</div>
        <div class="conversation-prompts"><span>快速提问</span><button class="suggestion" data-action="suggestion">解释“候选、材料、正式证据、招商线索”的区别</button><button class="suggestion" data-action="suggestion">结合当前台账，列出今天应优先由人工核对的事项</button><button class="suggestion" data-action="suggestion">请生成一次内部研判前需要向企业确认的问题清单</button></div>
        <form id="chat-form" class="conversation-compose"><label class="sr-only" for="business-message">输入给徐州 Agent 的问题</label><textarea id="business-message" name="message" rows="3" required maxlength="4000" ${state.chatWorking || state.chatHistoryLoading ? "disabled" : ""} placeholder="例如：请用业务人员能看懂的话说明，这条页面材料下一步该由谁做什么。"></textarea><div class="conversation-compose-foot"><span>只用于内部研判；不等于企业事实、对外承诺或自动联系指令。</span><button class="send-conversation" type="submit" ${state.chatWorking || state.chatHistoryLoading ? "disabled" : ""}>${state.chatWorking ? "正在回答" : "发送问题"}<b>↗</b></button></div></form>
      </section>
    </section>
  `;
}

function teamAdmin() {
  const members = list(state.admin?.members);
  return `
    <div class="section-head"><div><span class="eyebrow">共享台账访问授权</span><h2>徐州招商团队名册</h2></div><button class="outline-button" data-action="refresh-admin">刷新</button></div>
    <section class="info-strip"><strong>管理员做什么：</strong><span>输入已有的平台用户名并加入名册。移出名册只撤销访问权，不删除历史材料或审计记录。</span></section>
    <form id="add-member-form" class="inline-form"><label>平台用户名<input name="username" required maxlength="100" placeholder="例如：wangxiaoming" /></label><button class="primary-button" type="submit">加入徐州团队</button></form>
    ${members.length ? `<div class="member-list">${members.map((entry) => { const username = pick(entry, ["username", "member_username"], ""); return `<article class="member-row"><div><strong>${escape(username || "未知用户名")}</strong><small>加入时间：${escape(when(pick(entry, ["added_at", "created_at"], "")))}</small></div><button class="danger-button small" data-action="remove-member" data-username="${escape(username)}">移出团队</button></article>`; }).join("")}</div>` : blank("名册中暂时没有成员", "请先在平台用户中创建或确认账号，再把对应用户名加入徐州团队。")}
  `;
}

function usersAdmin() {
  const users = list(state.admin?.users);
  const members = list(state.admin?.members);
  return `
    <div class="section-head"><div><span class="eyebrow">平台登录账号</span><h2>业务用户管理</h2></div><button class="primary-button" data-action="open-create-user">创建业务账号</button></div>
    <section class="info-strip"><strong>注意：</strong><span>账号创建完成后，还需要在徐州团队名册中添加同一用户名，才能读取共享招商台账。</span></section>
    ${users.length ? `<div class="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>账号状态</th><th>团队访问</th><th></th></tr></thead><tbody>${users.map((entry) => {
      const username = pick(entry, ["username"], "");
      const joined = members.some((member) => pick(member, ["username", "member_username"], "") === username);
      const status = pick(entry, ["status"], "active");
      return `<tr><td><strong>${escape(pick(entry, ["name", "username"], "未命名用户"))}</strong><small>${escape(username)}</small></td><td>${entry.role === "admin" ? "管理员" : "业务成员"}</td><td><span class="status-pill ${status === "active" ? "green" : "danger"}">${status === "active" ? "可登录" : "已停用"}</span></td><td><span class="status-pill ${joined ? "green" : "neutral"}">${joined ? "已加入徐州团队" : "尚未加入"}</span></td><td><button class="outline-button small" data-action="toggle-user" data-id="${escape(pick(entry, ["id", "user_id"], ""))}" data-status="${escape(status)}">${status === "active" ? "停用" : "恢复"}</button></td></tr>`;
    }).join("")}</tbody></table></div>` : blank("没有可展示的平台用户", "可以先创建一个业务账号。")}
  `;
}

function dingtalkAdmin() {
  const subscriptions = list(state.admin?.subscriptions);
  return `
    <div class="section-head"><div><span class="eyebrow">已绑定的单人钉钉会话</span><h2>每日新增动态简报</h2></div><button class="outline-button" data-action="refresh-admin">刷新</button></div>
    <section class="info-strip"><strong>固定规则：</strong><span>每日 09:00 向管理员已绑定的个人钉钉会话发送摘要；失败后 09:05 重试一次。简报只显示新增数量和最多 5 条标题，不含网页链接或联系方式。</span></section>
    ${subscriptions.length ? `<div class="digest-list">${subscriptions.map((entry) => {
      const enabled = pick(entry, ["enabled", "active"], false) === true || pick(entry, ["status"], "") === "active";
      const id = itemId(entry, ["subscription_id", "id"]);
      return `<article class="digest-card"><div><span class="status-pill ${enabled ? "green" : "neutral"}">${enabled ? "已启用" : "已暂停"}</span><h3>个人钉钉每日新增动态</h3><p>${enabled ? "将在每日 09:00 推送；若失败，系统在 09:05 自动重试一次。" : "暂停时不会发送简报，也不会进行 09:05 重试。"}</p></div><div class="button-row">${enabled ? `<button class="outline-button small" data-action="run-digest" data-id="${escape(id)}">立即发送测试简报</button><button class="danger-button small" data-action="pause-digest" data-id="${escape(id)}">暂停</button>` : `<button class="primary-button small" data-action="enable-digest">启用</button>`}</div></article>`;
    }).join("")}</div>` : `<section class="empty-state"><h3>尚未读取到日报订阅</h3><p>如已在 QwenPaw 绑定个人钉钉会话，可在这里启用每日简报；页面不会展示会话 ID 或其他敏感配置。</p><button class="primary-button" data-action="enable-digest">启用每日 09:00 简报</button></section>`}
  `;
}

function admin() {
  if (!isAdmin()) {
    return `<section class="page-heading"><h1>无权限</h1><p>管理员专属的账号、团队和钉钉配置不会对业务成员显示。</p></section>`;
  }
  const tabs = [["team", "徐州团队名册"], ["users", "平台用户"], ["dingtalk", "钉钉日报"]];
  return `
    <section class="page-heading"><span class="eyebrow">管理员专属</span><h1>账号、团队与日报</h1><p>创建平台账号不等于可以读取共享台账。必须在徐州团队名册中明确添加，用户才获得招商材料访问权。</p></section>
    <div class="tab-bar">${tabs.map(([id, text]) => `<button class="${state.adminTab === id ? "active" : ""}" data-action="set-admin" data-tab="${id}">${text}</button>`).join("")}</div>
    <section class="panel admin-panel">${state.adminLoading ? loading("正在读取管理员数据…") : state.adminTab === "team" ? teamAdmin() : state.adminTab === "users" ? usersAdmin() : dingtalkAdmin()}</section>
  `;
}

function modalShell(titleText, body, wide = true) {
  return `<div class="modal-backdrop"><section class="modal ${wide ? "wide" : "compact"}" role="dialog" aria-modal="true"><header class="modal-head"><div><span class="eyebrow">徐州招商业务台</span><h2>${escape(titleText)}</h2></div><button class="modal-close" data-action="close-modal" aria-label="关闭">×</button></header><div class="modal-body">${body}</div></section></div>`;
}

function reviewForm(value) {
  const geo = geography(value);
  return `
    <form id="candidate-review-form" class="form-stack decision-form">
      <div><span class="eyebrow">人工判断（会留下审计记录）</span><h3>你认为这条候选下一步应怎么处理？</h3><p>这个决定不会修改原网页、补写事实或直接形成正式证据。</p></div>
      <label>业务判断<select name="decision" id="candidate-decision" required><option value="need_evidence">需要补充依据</option><option value="observe">继续观察</option><option value="invalid">不纳入招商范围</option><option value="waive_xuzhou">无需徐州字段，作为高价值材料继续核验</option></select></label>
      <label>判断理由（必填）<textarea name="review_note" rows="4" required maxlength="2000" placeholder="说明你看到什么、还缺什么，或为什么它具有高价值。"></textarea></label>
      <fieldset id="waiver-fields" class="waiver-fields"><legend>选择“无需徐州字段”时必须补充</legend><p>这是人工范围决定：请确认企业有中国业务，并说明企业来源地。它不是正式证据，也不会跳过服务器重新抓取。</p><label>企业来源地<input name="origin" maxlength="100" placeholder="例如：北京" /></label><label>来源地类型<select name="origin_kind"><option value="registered_location">注册地</option><option value="headquarters">总部所在地</option><option value="operation_site">主要运营地</option><option value="unknown">暂不明确</option></select></label><label>核对依据<textarea name="evidence_basis" rows="3" maxlength="2000" placeholder="例如：企业官网“关于我们”页面或工商公示信息。"></textarea></label><p class="tiny-note">当前地域提示：${escape(geo.label)}。请勿把新闻网站所在地当作企业来源地。</p></fieldset>
      <div class="button-row"><button class="outline-button" type="button" data-action="cancel-review">取消</button><button class="primary-button" type="submit">保存人工判断</button></div>
    </form>
  `;
}

function discoveryModal() {
  const value = state.modal?.item || {};
  const source = publicUrl(pick(value, ["source_url", "url", "canonical_url"], ""));
  const geo = geography(value);
  const learned = learnedReadingPriority(value);
  const body = `
    <section class="stage-banner discovery"><span>第 1 步 · 机器发现候选</span><strong>尚未保存页面材料，不能确认入证</strong><p>目前服务器保存的是标题、链接、发现摘要和发现时间；还没有保存可核验的页面正文、哈希或证据草稿。</p></section>
    <div class="detail-grid"><div><span>来源网站</span><strong>${escape(sourceDomain(source))}</strong></div><div><span>发现时间</span><strong>${escape(when(pick(value, ["discovered_at", "created_at", "updated_at"], "")))}</strong></div><div><span>机器阅读顺序</span><strong>${escape(learned.label)}</strong></div><div><span>地域优先级</span><strong>${escape(geo.label)}</strong></div><div><span>人工状态</span><strong>${escape(candidateState(value))}</strong></div></div>
    <section class="learning-note"><strong>为什么排在这里</strong><p>${escape(learned.reason)} 这只影响队列阅读顺序；你仍可以按实际招商价值优先处理任何一条。</p></section>
    <section class="content-card"><h3>机器当时看到的摘要</h3><p>${escape(sample(value, 1400))}</p>${source ? `<a class="external-link" href="${escape(source)}" target="_blank" rel="noopener noreferrer">在新窗口查看原网页 ↗</a>` : ""}</section>
    <section class="role-grid"><div><strong>机器下一步</strong><p>按照来源规则和访问限制，后续可能保存页面材料；不会因为你打开了网页而自动创建正式证据。</p></div><div><strong>你现在可以做什么</strong><p>记录补充依据、继续观察、无效，或对高价值材料决定无需徐州字段；全部判断都会留下操作者审计。</p></div></section>
    ${state.modal?.review ? reviewForm(value) : `<div class="button-row"><button class="primary-button" data-action="open-review">记录业务判断</button><button class="outline-button" data-action="use-human-material">我能合法打开，提交人工材料</button>${source ? `<a class="outline-button" href="${escape(source)}" target="_blank" rel="noopener noreferrer">查看原网页</a>` : ""}</div>`}
  `;
  return modalShell(label(value), body);
}

function drafts(draftList) {
  if (!draftList.length) {
    return blank(
      "当前没有待确认的证据草稿",
      "这份页面材料仍只能供人工阅读。请先记录判断，或等待服务端生成可核验的证据草稿。",
    );
  }
  return `<div class="draft-list">${draftList.map((draft) => {
    const missing = records(draft, ["missing_fields", "missing_confirmation_fields"]);
    const ready = missing.length === 0 && !draft.requires_china_business_verification;
    const reason = missing.length ? "仍缺：" + missing.join("、") : "仍需核对中国、徐州或投资落地依据";
    return `<article class="draft-card"><div><span class="status-pill ${ready ? "green" : "warm"}">${ready ? "可请求重新核验" : "尚缺关键字段"}</span><h4>${escape(label(draft, "待确认草稿"))}</h4><p>${ready ? "请确认已阅读材料。点击后服务器会重新抓取原页面；只有通过核验，才会新增正式证据。" : "目前不能直接入证：" + reason}</p></div><button class="${ready ? "primary-button" : "outline-button"} small" data-action="${ready ? "open-draft-confirm" : "open-draft-review"}" data-id="${escape(draftId(draft))}">${ready ? "请求服务器核验" : "记录草稿判断"}</button></article>`;
  }).join("")}</div>`;
}

function observationModal() {
  const data = state.modal?.data || {};
  const observation = data.observation || data;
  const candidate = data.candidate || {};
  const snapshot = data.snapshot || {};
  const source = publicUrl(
    pick(
      snapshot,
      ["source_url", "url"],
      pick(observation, ["source_url", "url"], pick(candidate, ["source_url", "url"], "")),
    ),
  );
  const pageText = pick(snapshot, ["visible_text", "page_text"], pick(data, ["saved_page_text", "visible_text"], ""));
  const draftList = records(data, ["evidence_drafts", "drafts"]);
  const geo = geography({ ...candidate, ...observation, ...data });
  const body = `
    <section class="stage-banner material"><span>第 2 步 · 已保存页面材料</span><strong>现在可阅读材料，但仍未自动形成正式证据</strong><p>服务器已经保存页面文本、来源链接、采集时间和内容哈希。你不能编辑这些字段；只能提出业务判断或请求服务器重新核验。</p></section>
    <div class="detail-grid"><div><span>来源网站</span><strong>${escape(sourceDomain(source))}</strong></div><div><span>采集时间</span><strong>${escape(when(pick(observation, ["captured_at", "collected_at", "observed_at"], "")))}</strong></div><div><span>材料优先级</span><strong>${escape(materialPriority(observation))}</strong></div><div><span>地域提示</span><strong>${escape(geo.label)}</strong></div></div>
    <section class="content-card"><h3>服务器保存的页面文本（只读）</h3><p class="readonly-note">由服务器保存，并带有内容哈希；正式入证时服务器会重新抓取原页面比较，不能手工编辑。</p><div class="saved-text">${escape(pageText || "当前响应未返回可展示的页面文本。")}</div>${source ? `<a class="external-link" href="${escape(source)}" target="_blank" rel="noopener noreferrer">在新窗口查看原网页 ↗</a>` : ""}</section>
    <section class="role-grid"><div><strong>机器已完成</strong><p>保存页面材料并保留完整性信息；它没有确认企业事实、招商意愿或招商结论。</p></div><div><strong>你要完成</strong><p>核对材料是否有招商价值、还缺哪些公开依据；请求确认时服务器会重新抓取原网页。</p></div></section>
    <section class="draft-section"><div class="section-head"><div><span class="eyebrow">第 3 步 · 证据草稿</span><h3>请求服务器重新核验</h3></div></div>${drafts(draftList)}</section>
    ${state.modal?.review ? reviewForm({ ...candidate, ...observation }) : `<div class="button-row"><button class="outline-button" data-action="open-review">记录业务判断</button><button class="outline-button" data-action="use-human-material">我有授权补充材料</button></div>`}
  `;
  return modalShell(label({ ...candidate, ...observation, ...snapshot }), body);
}

function evidenceRow(value) {
  const source = publicUrl(pick(value, ["source_url", "url"], ""));
  const human = value.evidence_origin === "human_attested_material";
  const materialType = human ? "业务人员授权材料" : "服务器抓取并核验的材料";
  const materialNote = human
    ? `提交人：${pick(value?.human_attestation, ["attested_by_user_id"], "服务器记录")} · ${humanAccessLabel(value?.human_attestation?.access_mode)}`
    : "服务器采集时间和完整性记录已保存";
  return `<article><span class="status-pill ${human ? "sky" : "green"}">${escape(materialType)}</span><strong>${escape(label(value, human ? "人工授权材料" : "服务器材料"))}</strong><p>${escape(sample(value, 480))}</p><small>${escape(when(pick(value, ["captured_at", "published_at", "created_at", "collected_at"], "")))} · ${escape(materialNote)}</small>${source ? `<a href="${escape(source)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a>` : ""}</article>`;
}

function followupCard(card) {
  if (!Object.keys(card).length) {
    return `<section class="content-card"><h3>招商跟进审批卡暂未读取</h3><p>请刷新后再试；没有审批卡时不会开放人工推进操作。</p></section>`;
  }
  const checks = records(card, ["machine_checks"]);
  const blockers = records(card, ["blockers"]);
  const approval = card.current_approval;
  return `
    <section class="followup-card">
      <div class="section-head"><div><span class="eyebrow">由人最终决定</span><h3>招商跟进审批卡</h3></div><div class="button-row">${Number(card.human_attested_material_count || 0) > 0 ? `<button class="outline-button small" data-action="open-human-outcome">记录实际结果，帮助来源学习</button>` : ""}${card.can_approve ? `<button class="primary-button" data-action="open-followup">开始人工审批</button>` : `<span class="status-pill warm">请先处理阻塞项</span>`}</div></div>
      <p>${escape(card.human_role || "业务人员须决定招商价值、负责人和下一步。")}</p>
      ${checks.length ? `<div class="machine-checks">${checks.map((check) => `<div><strong>${escape(check.label || check.key || "机器检查")}</strong><span class="${check.passed === true ? "check-ok" : "check-warn"}">${check.passed === true ? "已满足" : "需人工留意"}</span><small>${escape(check.detail || check.reason || "")}</small></div>`).join("")}</div>` : ""}
      ${blockers.length ? `<div class="blocker-list"><strong>当前不能通过的原因</strong>${blockers.map((blocker) => `<p>${escape(typeof blocker === "string" ? blocker : blocker.message || blocker.reason || "请查看审批卡")}</p>`).join("")}</div>` : ""}
      ${approval ? `<div class="approval-record"><strong>最近一次人工审批</strong><p>负责人：${escape(approval.owner_name || "未提供")} · 目标日期：${escape(approval.target_date || "未安排")}</p><p>下一步：${escape(approval.next_action_label || approval.next_action_detail || approval.next_action || "未提供")}</p></div>` : ""}
    </section>
  `;
}

function leadModal() {
  const lead = state.modal?.lead || {};
  const card = state.modal?.card?.card || state.modal?.card || {};
  const evidence = records(lead, ["evidence", "evidence_chain", "items"]);
  if (state.modal?.loading) return modalShell("招商线索", loading("正在读取线索、材料和审批卡…"));
  const serverCount = Number(card.server_captured_evidence_count || evidence.filter((entry) => entry.evidence_origin !== "human_attested_material").length);
  const humanCount = Number(card.human_attested_material_count || evidence.filter((entry) => entry.evidence_origin === "human_attested_material").length);
  const body = `
    <section class="stage-banner lead"><span>第 4 步 · 人工招商跟进审批</span><strong>${escape(card.boundary || "审批通过仅代表纳入内部人工跟进池。")}</strong><p>${escape(card.machine_role || "机器保留已核验的正式证据和审计记录。")}</p></section>
    <div class="detail-grid"><div><span>产业</span><strong>${escape(pick(lead, ["sector", "industry"], "待归类"))}</strong></div><div><span>当前状态</span><strong>${escape(leadState(lead))}</strong></div><div><span>服务器材料</span><strong>${serverCount} 条</strong></div><div><span>人工授权材料</span><strong>${humanCount} 条</strong></div></div>
    <section class="content-card"><h3>线索摘要</h3><p>${escape(sample(lead, 1600))}</p></section>
    <section class="evidence-section"><h3>可追溯入账材料（明确区分来源）</h3><p class="readonly-note">服务器材料可支持机器核验；人工授权材料可支持人工研判，但不能被自动升级成服务器抓取事实。最终招商判断仍由业务人员负责。</p>${evidence.length ? `<div class="evidence-list">${evidence.map(evidenceRow).join("")}</div>` : `<p class="empty-inline">暂无可展示的入账材料；因此不能纳入招商跟进池。</p>`}</section>
    ${followupCard(card)}
  `;
  return modalShell(label(lead, "招商线索"), body);
}

function followupModal() {
  const lead = state.modal?.lead || {};
  const card = state.modal?.card?.card || state.modal?.card || {};
  const checks = records(card, ["human_required_items"]);
  const actions = records(card, ["next_action_options"]);
  const body = card.can_approve
    ? `<form id="followup-form" class="form-stack"><section class="stage-banner lead"><span>不会自动对外联系</span><strong>请由实际业务负责人逐项核对后再提交</strong><p>审批只把线索纳入内部人工跟进池，不会向企业发送消息、承诺政策或确认招商成功。</p></section><fieldset class="checklist"><legend>六项人工核对（全部必选）</legend>${checks.map((entry) => `<label class="checkline"><input type="checkbox" name="${escape(entry.key)}" required /><span><strong>${escape(entry.label)}</strong><small>请在阅读正式证据和内部信息后勾选。</small></span></label>`).join("")}</fieldset><label>为什么值得跟进（必填）<textarea name="business_value_reason" rows="4" required maxlength="2000" placeholder="说明与徐州产业、资源或招商方向的匹配理由；不要把推测写成企业承诺。"></textarea></label><label>下一步由人完成什么<select name="next_action" required>${actions.map((entry) => `<option value="${escape(entry.value)}">${escape(entry.label || entry.value)}</option>`).join("")}</select></label><label>下一步具体说明（必填）<textarea name="next_action_detail" rows="3" required maxlength="2000" placeholder="例如：由招商主管核对企业官网与公开工商信息，再准备内部研判材料。"></textarea></label><div class="form-grid"><label>负责人姓名<input name="owner_name" required maxlength="100" placeholder="实际负责此事项的人" /></label><label>目标完成日期<input name="target_date" type="date" required /></label></div><label>审批说明（必填）<textarea name="review_note" rows="3" required maxlength="2000" placeholder="写明本次人工核对的范围、风险处理和限制。"></textarea></label><div class="button-row"><button class="outline-button" type="button" data-action="back-lead">返回线索</button><button class="primary-button" type="submit">确认纳入人工跟进池</button></div></form>`
    : `<section class="empty-state"><h3>目前不能发起人工审批</h3><p>${escape(records(card, ["blockers"]).join("；") || "请先确保服务器已有正式证据，并处理审批卡中的阻塞项。")}</p><button class="outline-button" data-action="back-lead">返回线索</button></section>`;
  return modalShell("人工审批：" + label(lead, "正式招商线索"), body);
}

function draftConfirmModal() {
  const body = `
    <section class="stage-banner material"><span>服务器会再次访问原页面</span><strong>只有重新抓取并校验通过，才会新增正式证据</strong><p>你不能填写或修改实体、链接、日期和页面内容；这里只记录本次人工确认依据。</p></section>
    <form id="draft-confirm-form" class="form-stack"><label>人工确认依据（必填）<textarea name="review_note" rows="5" required maxlength="2000" placeholder="说明你已阅读保存材料，以及为什么应请求服务器重新核验。"></textarea></label><div class="button-row"><button type="button" class="outline-button" data-action="back-observation">返回材料</button><button type="submit" class="primary-button">请求服务器重新核验</button></div></form>
  `;
  return modalShell("请求服务器重新核验", body, false);
}

function draftReviewModal() {
  const missing = records(state.modal?.draft, ["missing_fields", "missing_confirmation_fields"]);
  const reason = missing.length ? "仍缺：" + missing.join("、") : "仍需补充关键公开依据";
  const body = `
    <section class="stage-banner discovery"><span>草稿尚未具备入证条件</span><strong>${escape(reason)}</strong><p>这个判断不会补造事实，也不会改变保存页面材料；它只告诉团队后续应补证、观察或停止处理。</p></section>
    <form id="draft-review-form" class="form-stack"><label>业务判断<select name="decision"><option value="need_evidence">需要补充依据</option><option value="observe">继续观察</option><option value="invalid">不纳入招商范围</option></select></label><label>判断理由（必填）<textarea name="review_note" rows="5" required maxlength="2000" placeholder="写清楚还缺少什么公开依据，或为什么继续观察/停止处理。"></textarea></label><div class="button-row"><button type="button" class="outline-button" data-action="back-observation">返回材料</button><button type="submit" class="primary-button">保存草稿判断</button></div></form>
  `;
  return modalShell("记录草稿业务判断", body, false);
}

function humanOutcomeModal() {
  const lead = state.modal?.lead || {};
  const body = `
    <section class="stage-banner lead"><span>来源学习只读取结果，不改写事实</span><strong>记录这条人工材料线索的实际业务结果</strong><p>此操作帮助机器判断今后同类来源的阅读优先级；不会改变机器评分、线索状态、材料内容或对外动作。</p></section>
    <form id="human-material-outcome-form" class="form-stack"><label>实际结果<select name="outcome"><option value="positive_progress">有实质推进</option><option value="landed">已落地/已取得明确结果</option><option value="deferred">暂缓，保留观察</option><option value="not_converted">未转化</option><option value="invalidated">确认无效</option></select></label><label>结果说明（必填）<textarea name="note" rows="5" required maxlength="2000" placeholder="说明本次实际业务结果及判断依据；不要填写个人敏感信息或对外承诺。"></textarea></label><div class="button-row"><button type="button" class="outline-button" data-action="back-lead">返回线索</button><button type="submit" class="primary-button">记录为来源学习样本</button></div></form>
  `;
  return modalShell("记录实际业务结果：" + label(lead, "招商线索"), body, false);
}

function sourceStatusModal() {
  const source = state.modal?.source || {};
  const next = state.modal?.nextStatus === "active" ? "重新启用" : "停用";
  const body = `
    <section class="stage-banner discovery"><span>只影响未来来源分级</span><strong>${escape(next)}：${escape(source.domain || "该来源")}</strong><p>此操作不会删除历史材料、不会改写已入账证据，也不会让机器立即抓取任何页面。服务器仍会独立遵守公开访问、robots 和安全限制。</p></section>
    <form id="source-status-form" class="form-stack"><label>变更原因（必填，会写入审计）<textarea name="change_reason" rows="4" required maxlength="2000" placeholder="例如：近期页面质量不稳定，先暂停作为未来发现来源。"></textarea></label><div class="button-row"><button type="button" class="outline-button" data-action="close-modal">取消</button><button type="submit" class="${state.modal?.nextStatus === "active" ? "primary-button" : "danger-button"}">${escape(next)}来源</button></div></form>
  `;
  return modalShell(next + "可信来源", body, false);
}

function createUserModal() {
  const body = `
    <form id="create-user-form" class="form-stack">
      <label>姓名<input name="name" required maxlength="100" placeholder="例如：王小明" /></label>
      <label>用户名<input name="username" required minlength="3" maxlength="100" placeholder="至少 3 个字符" /></label>
      <label>初始密码<input name="password" type="password" required minlength="8" autocomplete="new-password" placeholder="至少 8 位" /></label>
      <label>平台角色<select name="role"><option value="user">业务成员</option><option value="admin">管理员</option></select></label>
      <p class="tiny-note">账号创建完成后，仍需在徐州团队名册中添加该用户名，才能访问共享台账。</p>
      <div class="button-row"><button type="button" class="outline-button" data-action="close-modal">取消</button><button type="submit" class="primary-button">创建账号</button></div>
    </form>
  `;
  return modalShell("创建业务账号", body, false);
}

function guideModal() {
  const featureRows = [
    ["总览", "看数量、人工待办和今天最值得处理的事项。", "只读，不会触发抓取或改变状态。"],
    ["材料队列", "处理仅有链接的候选、已保存页面材料和证据草稿。", "人作判断；服务器按请求重新核验。"],
    ["人工材料入账", "提交你有权使用、但服务器无法读取的摘录或小附件。", "不提交密码、Cookie、令牌或无权使用的内容。"],
    ["招商跟进", "查看正式证据，决定是否纳入人工跟进池，指定负责人和日期。", "审批不等于企业同意、签约或落地。"],
    ["来源与学习", "补充公开网址、管理来源、查看来源效果并控制学习方式。", "机器只调整阅读顺序；不能自创新来源或结论。"],
    ["智能对话", "解释材料、整理待办、准备内部研判和沟通问题。", "回答不直接写入正式证据或替人审批。"],
    ["管理员", "管理平台账号、徐州团队名册和个人钉钉日报。", "创建账号后仍须加入团队名册。"],
  ];
  const body = `
    <section class="guide-hero">
      <span class="eyebrow">三分钟看懂</span>
      <h3>机器负责找、存、核验和提醒；人负责判断、授权和实际招商。</h3>
      <p>系统里的“候选、材料、草稿、证据、跟进”是五个不同阶段。任何自动统计、评分、对话回答或钉钉简报，都不代表企业有来徐投资意愿。</p>
    </section>

    <section class="guide-section">
      <div class="guide-section-head"><span>01</span><div><h3>从网页到招商行动的完整流程</h3><p>按顺序处理，不要把前一步当成后一步。</p></div></div>
      <ol class="guide-flow">
        <li><strong>发现候选</strong><span>机器保存标题、网址和发现摘要；此时还没有可审计页面材料。</span></li>
        <li><strong>保存材料</strong><span>服务器在 robots 和安全规则允许时保存可见文本、时间和哈希。</span></li>
        <li><strong>人工判断</strong><span>业务人员选择补充依据、观察、无效，或说明无需徐州字段仍值得核验。</span></li>
        <li><strong>重新核验入证</strong><span>人发起请求；服务器重访原网页并核对主体、日期、摘录等，通过才新增正式证据。</span></li>
        <li><strong>人工招商审批</strong><span>人核对产业、地域、交叉来源和风险，指定负责人、下一步与完成日期。</span></li>
        <li><strong>结果反馈与学习</strong><span>人记录实际进展；机器只据此优化以后先看哪些来源，不改写事实。</span></li>
      </ol>
    </section>

    <section class="guide-section">
      <div class="guide-section-head"><span>02</span><div><h3>人和机器分别能做什么</h3><p>人的业务决定权最大，但原始材料和审计字段不能被任何前端改写。</p></div></div>
      <div class="guide-role-grid">
        <article><span class="role-chip machine">机器</span><h4>自动执行</h4><ul><li>低频发现公开候选</li><li>按安全规则保存页面材料</li><li>按人工请求重新核验</li><li>提示缺项、风险和来源集中度</li><li>生成只读报告、排序和钉钉简报</li></ul><p><strong>机器不能：</strong>替人认定招商价值、联系企业、承诺政策、绕过登录或 robots、自动把候选变成招商成果。</p></article>
        <article><span class="role-chip human">业务人员</span><h4>最终拍板</h4><ul><li>打开原网页并判断材料价值</li><li>提交有权使用的人工材料</li><li>补充公开网址并启停来源</li><li>选择补证、观察、无效或地域豁免</li><li>发起重新核验、审批跟进、指定负责人</li><li>记录真实业务结果并控制学习策略</li></ul><p><strong>人不能改：</strong>服务器快照、历史证据、内容哈希、采集时间、登录身份和既有审计。</p></article>
        <article><span class="role-chip admin">管理员</span><h4>管理准入</h4><ul><li>创建、停用和恢复平台账号</li><li>维护徐州团队名册</li><li>管理绑定个人会话的钉钉日报</li><li>在严格条件下执行完整性维护</li></ul><p>管理员权限不等于可以伪造事实或改写历史证据。</p></article>
      </div>
    </section>

    <section class="guide-section">
      <div class="guide-section-head"><span>03</span><div><h3>每个功能在哪里、用来做什么</h3><p>找不到操作时，先按左侧栏目定位。</p></div></div>
      <div class="table-wrap"><table class="guide-table"><thead><tr><th>栏目</th><th>主要用途</th><th>使用边界</th></tr></thead><tbody>${featureRows.map((row) => `<tr><td><strong>${escape(row[0])}</strong></td><td>${escape(row[1])}</td><td>${escape(row[2])}</td></tr>`).join("")}</tbody></table></div>
    </section>

    <section class="guide-section">
      <div class="guide-section-head"><span>04</span><div><h3>常见状态怎么理解</h3><p>状态是在告诉你下一步，不是在宣布招商结果。</p></div></div>
      <div class="guide-status-grid">
        <article><strong>仅发现链接</strong><p>只有标题、网址和摘要。可作业务判断，但不能确认入证。</p></article>
        <article><strong>已保存页面材料</strong><p>服务器已有只读文本、时间和哈希；仍须人工阅读。</p></article>
        <article><strong>待补充依据</strong><p>人认为值得保留，但缺中国业务、徐州关联、投资落地或其他关键材料。</p></article>
        <article><strong>观察</strong><p>暂不投入大量人工时间，保留低频关注；不会自动确认事实。</p></article>
        <article><strong>正式证据</strong><p>服务器重新访问并通过一致性校验后追加的可追溯记录。</p></article>
        <article><strong>已纳入人工跟进池</strong><p>团队决定由人继续招商工作；不代表企业已同意或系统已联系。</p></article>
      </div>
    </section>

    <section class="guide-section">
      <div class="guide-section-head"><span>05</span><div><h3>技术维护与扩展点</h3><p>两个前端共用同一套 QwenPaw 账号、权限、台账和插件接口。</p></div></div>
      <div class="guide-maintenance">
        <div><strong>新增普通公开网页</strong><p>优先使用“来源与学习 → 人工补充公开来源”。服务器会检查网址、robots、公开访问和跨域跳转。</p></div>
        <div><strong>新增付费 API 或登录数据源</strong><p>不能把密钥填在页面。由开发者在后端按供应商许可增加最小权限密钥、限速、审计和故障隔离。</p></div>
        <div><strong>扩展业务字段或状态</strong><p>同时更新后端数据模型、写接口白名单、审计、两个前端的中文解释和回归测试；未知状态应原样显示，不能猜成成功。</p></div>
        <div><strong>排查问题</strong><p>先看登录和团队名册，再看页面显示的失败阶段。抓取失败不等于网页不存在；入证失败也不等于服务器没有尝试。</p></div>
        <div><strong>自动学习边界</strong><p>只允许调整阅读队列和经人批准的固定来源配方；不得自动新增来源、关键词、产业、证据或对外动作。</p></div>
        <div><strong>上线原则</strong><p>新版本须通过后端测试、两个前端检查、发布包校验、备份、健康检查和可恢复发布；不能直接改生产台账。</p></div>
      </div>
    </section>

    <section class="guide-footer-note"><strong>遇到不确定时：</strong>先保留材料并写清判断理由。不要把推测写成事实，不要提交密码、Cookie、令牌、个人敏感信息或无权使用的材料。</section>
  `;
  return modalShell("简明使用手册", body);
}

function renderModal() {
  if (!state.modal) return "";
  if (state.modal.type === "discovery") return discoveryModal();
  if (state.modal.type === "observation") return observationModal();
  if (state.modal.type === "lead") return leadModal();
  if (state.modal.type === "followup") return followupModal();
  if (state.modal.type === "draft-confirm") return draftConfirmModal();
  if (state.modal.type === "draft-review") return draftReviewModal();
  if (state.modal.type === "human-outcome") return humanOutcomeModal();
  if (state.modal.type === "source-status") return sourceStatusModal();
  if (state.modal.type === "create-user") return createUserModal();
  if (state.modal.type === "guide") return guideModal();
  return "";
}

async function loadDashboard(force = false) {
  if (state.dashboardLoading || (state.dashboard && !force)) return;
  state.dashboardLoading = true;
  render();
  const endpoints = {
    summary: PLUGIN + "/dashboard/work-summary?window_days=30",
    discoveries: PLUGIN + "/dashboard/discovery-candidates?limit=100",
    observations: PLUGIN + "/dashboard/candidate-observations?limit=100",
    leads: PLUGIN + "/dashboard/leads?limit=100",
    actions: PLUGIN + "/dashboard/action-center?limit=100",
  };
  const names = Object.keys(endpoints);
  const responses = await Promise.allSettled(names.map((name) => api(endpoints[name])));
  const next = { ...(state.dashboard || {}), accessError: "" };
  let accessError = null;
  responses.forEach((response, index) => {
    const name = names[index];
    if (response.status === "fulfilled") {
      if (name === "discoveries") next.discoveries = records(response.value, ["candidates", "discovery_candidates", "items", "data"]);
      else if (name === "observations") next.observations = records(response.value, ["observations", "candidate_observations", "items", "data"]);
      else if (name === "leads") next.leads = records(response.value, ["leads", "items", "data"]);
      else if (name === "actions") next.actions = records(response.value, ["actions", "items", "data"]);
      else next.summary = response.value;
    } else if ([401, 403].includes(response.reason?.status)) {
      accessError = response.reason;
    }
  });
  if (accessError) {
    next.accessError = accessError.status === 401
      ? "登录状态已失效，请重新登录。"
      : "你已登录，但还未被授权访问徐州共享台账。管理员需要在徐州团队名册中添加你的平台用户名。";
    next.discoveries = [];
    next.observations = [];
    next.leads = [];
    next.actions = [];
  }
  state.dashboard = next;
  state.dashboardLoading = false;
  render();
}

async function loadEmpowerment(force = false) {
  if (state.empowermentLoading || (state.empowerment && !force)) return;
  state.empowermentLoading = true;
  render();
  const [human, learningData, sources, learningSchedule, publicSources] = await Promise.allSettled([
    api(PLUGIN + "/human-evidence?limit=100"),
    api(PLUGIN + "/adaptive-learning"),
    api(PLUGIN + "/trusted-sources?active_only=false"),
    api(PLUGIN + "/outcome-learning-schedule/"),
    api(PLUGIN + "/public-sources"),
  ]);
  const responses = [human, learningData, sources, learningSchedule, publicSources];
  const accessFailure = responses.find(
    (entry) => entry.status === "rejected" && [401, 403].includes(entry.reason?.status),
  );
  state.empowerment = {
    human: human.status === "fulfilled" ? human.value : { items: [], count: 0 },
    learning: learningData.status === "fulfilled" ? learningData.value : {},
    sources: sources.status === "fulfilled" ? records(sources.value, ["sources", "items", "data"]) : [],
    learningSchedule: learningSchedule.status === "fulfilled" ? learningSchedule.value : {},
    publicSources: publicSources.status === "fulfilled" ? records(publicSources.value, ["sources", "items", "data"]) : [],
    publicSourcesError: publicSources.status === "rejected" ? (publicSources.reason?.message || "补充公开来源暂时无法读取。") : "",
    accessError: accessFailure
      ? accessFailure.reason.status === 401
        ? "登录状态已失效，请重新登录。"
        : "你已登录，但还未被授权访问徐州共享台账。管理员需要在徐州团队名册中添加你的平台用户名。"
      : "",
  };
  state.empowermentLoading = false;
  render();
}

async function loadAdmin(force = false) {
  if (!isAdmin() || state.adminLoading || (state.admin && !force)) return;
  state.adminLoading = true;
  render();
  const [users, members, subscriptions] = await Promise.allSettled([
    api("/api/users"),
    api(PLUGIN + "/team/members"),
    api(PLUGIN + "/dingtalk-daily-digest/subscriptions"),
  ]);
  state.admin = {
    users: users.status === "fulfilled" ? records(users.value, ["users", "items", "data"]) : [],
    members: members.status === "fulfilled" ? records(members.value, ["members", "team_members", "items", "data"]) : [],
    subscriptions: subscriptions.status === "fulfilled" ? records(subscriptions.value, ["subscriptions", "items", "data"]) : [],
  };
  state.adminLoading = false;
  render();
}

async function openDiscovery(id) {
  const item = list(state.dashboard?.discoveries).find((entry) => candidateId(entry) === id);
  if (!item) return notify("找不到这条候选，请先刷新队列。", "error");
  state.modal = { type: "discovery", item };
  render();
}

async function openObservation(id) {
  state.modal = { type: "observation", loading: true };
  render();
  try {
    const response = await api(PLUGIN + "/dashboard/candidate-observations/" + encodeURIComponent(id));
    state.modal = {
      type: "observation",
      data: {
        ...response,
        candidate: response.candidate || {},
        observation: response.observation || {},
        snapshot: response.snapshot || {},
        evidence_drafts: records(response, ["evidence_drafts", "drafts"]),
      },
    };
  } catch (error) {
    state.modal = null;
    notify(error.message || "无法读取保存页面材料。", "error");
  }
  render();
}

async function openLead(id) {
  state.modal = { type: "lead", loading: true, lead: {} };
  render();
  const [leadResponse, cardResponse] = await Promise.allSettled([
    api(PLUGIN + "/dashboard/leads/" + encodeURIComponent(id)),
    api(PLUGIN + "/dashboard/leads/" + encodeURIComponent(id) + "/followup-card"),
  ]);
  if (leadResponse.status === "rejected") {
    state.modal = null;
    return notify(leadResponse.reason?.message || "无法读取正式招商线索。", "error");
  }
  state.modal = {
    type: "lead",
    loading: false,
    lead: leadResponse.value?.lead || leadResponse.value?.item || leadResponse.value,
    card: cardResponse.status === "fulfilled" ? cardResponse.value : {},
  };
  render();
}

function currentCandidate() {
  if (state.modal?.type === "discovery") return state.modal.item || {};
  if (state.modal?.type === "observation") {
    return { ...(state.modal.data?.candidate || {}), ...(state.modal.data?.observation || {}) };
  }
  return {};
}

async function saveCandidateReview(form) {
  const value = currentCandidate();
  const id = candidateId(value);
  const input = Object.fromEntries(new FormData(form).entries());
  if (!id) return notify("候选缺少服务器标识，不能保存业务判断。", "error");
  const body = {
    decision: input.decision,
    review_note: String(input.review_note || "").trim(),
  };
  if (!body.review_note) return notify("请填写判断理由，便于团队复核。", "error");
  if (body.decision === "waive_xuzhou") {
    if (!String(input.origin || "").trim() || !String(input.evidence_basis || "").trim()) {
      return notify("选择无需徐州字段时，请填写企业来源地和核对依据。", "error");
    }
    body.enterprise_origin = {
      origin: String(input.origin).trim(),
      origin_kind: input.origin_kind,
      evidence_basis: String(input.evidence_basis).trim(),
      china_business_confirmed: true,
    };
  }
  try {
    await api(PLUGIN + "/discovery-candidates/" + encodeURIComponent(id) + "/business-review", {
      method: "POST",
      body,
    });
    state.modal = null;
    state.dashboard = null;
    notify("人工判断已保存。它没有修改原网页，也没有直接形成正式证据。");
    await loadDashboard(true);
  } catch (error) {
    notify(error.message || "保存人工判断未完成。", "error");
  }
}

function draftInCurrentMaterial(id) {
  return records(state.modal?.data, ["evidence_drafts", "drafts"]).find((entry) => draftId(entry) === id);
}

function restoreMaterial(parent) {
  state.modal = { type: "observation", data: parent };
  render();
}

async function confirmDraft(form) {
  const draft = state.modal?.draft;
  const parent = state.modal?.parent;
  const note = String(new FormData(form).get("review_note") || "").trim();
  if (!note) return notify("请说明本次人工确认依据。", "error");
  try {
    const result = await api(PLUGIN + "/evidence-drafts/" + encodeURIComponent(draftId(draft)) + "/confirm", {
      method: "POST",
      body: { review_note: note },
    });
    const audit = result.audit || {};
    const successful =
      result.confirmed === true ||
      ["confirmed_and_captured", "confirmed_with_existing_evidence"].includes(
        audit.new_state || result.confirmation_state,
      );
    state.modal = null;
    state.dashboard = null;
    const detail = result.message || audit.capture_result || result.reason || "服务器重新核验未通过，请查看材料或补充判断。";
    notify(
      successful ? "服务器重新核验通过，正式证据已追加到共享台账。" : "暂未形成正式证据：" + detail,
      successful ? "success" : "error",
    );
    await loadDashboard(true);
  } catch (error) {
    notify(error.message || "服务器重新核验未完成。", "error");
    if (parent) restoreMaterial(parent);
  }
}

async function saveDraftReview(form) {
  const draft = state.modal?.draft;
  const input = Object.fromEntries(new FormData(form).entries());
  const note = String(input.review_note || "").trim();
  if (!note) return notify("请填写判断理由。", "error");
  try {
    await api(PLUGIN + "/evidence-drafts/" + encodeURIComponent(draftId(draft)) + "/business-review", {
      method: "POST",
      body: { decision: input.decision, review_note: note },
    });
    state.modal = null;
    state.dashboard = null;
    notify("草稿业务判断已保存。它不会修改原页面材料或补造事实。");
    await loadDashboard(true);
  } catch (error) {
    notify(error.message || "保存草稿判断未完成。", "error");
  }
}

async function saveFollowup(form) {
  const formData = new FormData(form);
  const values = Object.fromEntries(formData.entries());
  const card = state.modal?.card?.card || state.modal?.card || {};
  const body = {
    identity_and_subject_checked: formData.get("identity_and_subject_checked") === "on",
    formal_evidence_checked: formData.get("formal_evidence_checked") === "on",
    industry_fit_checked: formData.get("industry_fit_checked") === "on",
    geographic_basis_checked: formData.get("geographic_basis_checked") === "on",
    key_claim_corroborated: formData.get("key_claim_corroborated") === "on",
    risk_and_conflict_checked: formData.get("risk_and_conflict_checked") === "on",
    business_value_reason: String(values.business_value_reason || "").trim(),
    next_action: values.next_action,
    next_action_detail: String(values.next_action_detail || "").trim(),
    owner_name: String(values.owner_name || "").trim(),
    target_date: values.target_date,
    review_note: String(values.review_note || "").trim(),
  };
  const absent = records(card, ["human_required_items"])
    .filter((entry) => body[entry.key] !== true)
    .map((entry) => entry.label);
  if (absent.length) return notify("请逐项确认：" + absent.join("、") + "。", "error");
  if (Object.values(body).some((value) => value === "" || value === undefined)) {
    return notify("请完整填写招商价值、下一步、负责人、日期和审批说明。", "error");
  }
  try {
    await api(PLUGIN + "/leads/" + encodeURIComponent(leadId(state.modal?.lead)) + "/followup-approval", {
      method: "POST",
      body,
    });
    state.modal = null;
    state.dashboard = null;
    notify("已完成内部招商跟进审批，并记录负责人和下一步。系统没有向企业自动发送消息。");
    await loadDashboard(true);
  } catch (error) {
    notify(error.message || "招商跟进审批未完成。", "error");
  }
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取所选附件。"));
    reader.onload = () => {
      const value = String(reader.result || "");
      const marker = value.indexOf(",");
      resolve(marker >= 0 ? value.slice(marker + 1) : value);
    };
    reader.readAsDataURL(file);
  });
}

function prepareHumanMaterial() {
  const candidate = currentCandidate();
  const material = state.modal?.data || {};
  const snapshot = material.snapshot || {};
  const sourceUrl = pick(snapshot, ["source_url", "url"], pick(candidate, ["source_url", "url", "canonical_url"], ""));
  const sourceTitle = pick(snapshot, ["source_title", "title"], pick(candidate, ["source_title", "title"], ""));
  state.humanDraft = {
    sector: portalSector(pick(candidate, ["sector", "industry"], "other")),
    source_url: sourceUrl,
    source_title: sourceTitle,
    source_published_at: pick(snapshot, ["source_published_at", "published_at"], ""),
    excerpt: pick(snapshot, ["visible_text", "page_text"], sample(candidate, 1500)),
    access_mode: "robot_or_server_blocked",
  };
  state.modal = null;
  state.route = "human";
  render();
  void loadEmpowerment();
}

async function saveHumanEvidence(form) {
  const formData = new FormData(form);
  const file = formData.get("attachment");
  let attachment;
  if (file instanceof File && file.size > 0) {
    const supported = ["application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain"];
    if (file.size > 512 * 1024) return notify("附件超过 512KB，请只保留关键截图、PDF 或文本导出。", "error");
    if (!supported.includes(file.type)) return notify("附件只支持 PDF、JPG、PNG、WEBP 或 TXT。", "error");
    attachment = { file_name: file.name, mime_type: file.type, content_base64: await fileAsBase64(file) };
  }
  const body = {
    entity_name: String(formData.get("entity_name") || "").trim(),
    entity_kind: String(formData.get("entity_kind") || "").trim(),
    sector: String(formData.get("sector") || "").trim(),
    source_url: String(formData.get("source_url") || "").trim(),
    source_title: String(formData.get("source_title") || "").trim(),
    source_published_at: String(formData.get("source_published_at") || "").trim() || null,
    source_level: String(formData.get("source_level") || "").trim(),
    claim_type: String(formData.get("claim_type") || "").trim(),
    evidence_quality: Number(formData.get("evidence_quality") || 0),
    excerpt: String(formData.get("excerpt") || "").trim(),
    material_text: String(formData.get("material_text") || "").trim() || null,
    access_mode: String(formData.get("access_mode") || "").trim(),
    attestation_confirmed: formData.get("attestation_confirmed") === "on",
    xuzhou_association_tags: formData.getAll("xuzhou_association_tags").map((value) => String(value)),
    ...(attachment ? { attachment } : {}),
  };
  if (!body.entity_name || !body.source_url || !body.source_title || !body.excerpt || !body.attestation_confirmed) {
    return notify("请补全主体、来源、关键摘录，并确认你有权使用该材料。", "error");
  }
  try {
    const result = await api(PLUGIN + "/human-evidence", { method: "POST", body });
    state.humanDraft = null;
    state.empowerment = null;
    state.dashboard = null;
    notify(result.next_step || "人工授权材料已入账，并已明确标记为人工提交。");
    await Promise.all([loadEmpowerment(true), loadDashboard(true)]);
  } catch (error) {
    notify(error.message || "登记人工授权材料未完成。", "error");
  }
}

async function saveLearningPolicy(form) {
  const input = Object.fromEntries(new FormData(form).entries());
  const changeReason = String(input.change_reason || "").trim();
  if (!changeReason) return notify("请说明调整学习策略的原因，便于团队审计。", "error");
  try {
    const result = await api(PLUGIN + "/adaptive-learning/policy", {
      method: "POST",
      body: { mode: input.mode, change_reason: changeReason },
    });
    state.empowerment = null;
    notify(result.policy_boundary || "学习策略已保存；机器只会在你选择的范围内自动工作。");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "保存学习策略未完成。", "error");
  }
}

async function saveLearningSchedule(enabled) {
  try {
    const result = await api(
      PLUGIN + "/outcome-learning-schedule/" + (enabled ? "enable" : "pause"),
      { method: "POST", body: {} },
    );
    state.empowerment = null;
    notify(
      result.schedule?.enabled === true
        ? "每日学习已启用；机器会在固定时间整理已记录结果，不会立即改变企业结论。"
        : "每日学习已暂停；历史材料和学习记录仍会保留。",
    );
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "更新每日学习计划未完成。", "error");
  }
}

async function registerTrustedSource(form) {
  const input = Object.fromEntries(new FormData(form).entries());
  const profile = sourceProfile(String(input.profile || ""));
  const changeReason = String(input.change_reason || "").trim();
  if (!String(input.domain || "").trim() || !changeReason) return notify("请填写公开域名和登记理由。", "error");
  try {
    await api(PLUGIN + "/trusted-sources", {
      method: "POST",
      body: {
        domain: String(input.domain).trim(),
        source_type: profile.source_type,
        source_level: profile.source_level,
        evidence_quality: profile.evidence_quality,
        allowed_claim_types: profile.allowed_claim_types,
        change_reason: changeReason,
      },
    });
    state.empowerment = null;
    notify("可信来源已登记。它只影响未来材料分级，不会立即抓取或改写历史材料。");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "登记可信来源未完成。", "error");
  }
}

async function addPublicSource(form) {
  const input = Object.fromEntries(new FormData(form).entries());
  const sourceUrl = String(input.source_url || "").trim();
  const note = String(input.note || "").trim();
  if (!sourceUrl) return notify("请填写完整的公开网页网址。", "error");
  try {
    const result = await api(PLUGIN + "/public-sources", {
      method: "POST",
      body: { source_url: sourceUrl, note: note || null },
    });
    state.empowerment = null;
    notify(result.preflight?.detail || "服务器检测通过，公开来源已加入低频发现目录。");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "服务器未能将该网址加入公开来源目录。", "error");
  }
}

async function retestPublicSource(sourceId) {
  if (!sourceId) return;
  try {
    const result = await api(PLUGIN + "/public-sources/" + encodeURIComponent(sourceId) + "/retest", {
      method: "POST",
      body: {},
    });
    state.empowerment = null;
    notify(result.preflight?.detail || "已完成服务器重新检测，请查看最新状态。");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "重新检测未完成。", "error");
  }
}

async function togglePublicSource(sourceId, nextStatus) {
  if (!sourceId || !["active", "disabled"].includes(nextStatus)) return;
  try {
    const result = await api(PLUGIN + "/public-sources/" + encodeURIComponent(sourceId) + "/status", {
      method: "POST",
      body: { new_status: nextStatus },
    });
    state.empowerment = null;
    notify(
      nextStatus === "active"
        ? result.preflight?.detail || "服务器检测通过，公开来源已重新启用。"
        : "已暂停此公开来源的后续自动发现；历史材料和审计仍会保留。",
    );
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "更新公开来源状态未完成。", "error");
  }
}

async function saveSourceStatus(form) {
  const source = state.modal?.source || {};
  const nextStatus = state.modal?.nextStatus;
  const changeReason = String(new FormData(form).get("change_reason") || "").trim();
  if (!source.source_id || !changeReason) return notify("请填写来源变更原因。", "error");
  try {
    await api(PLUGIN + "/trusted-sources/" + encodeURIComponent(source.source_id) + "/status", {
      method: "POST",
      body: { new_status: nextStatus, change_reason: changeReason },
    });
    state.modal = null;
    state.empowerment = null;
    notify(nextStatus === "active" ? "来源已重新启用，将参与未来来源分级。" : "来源已停用；历史材料和审计不会被删除。");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "更新来源状态未完成。", "error");
  }
}

async function saveHumanMaterialOutcome(form) {
  const lead = state.modal?.lead || {};
  const input = Object.fromEntries(new FormData(form).entries());
  const note = String(input.note || "").trim();
  if (!leadId(lead) || !note) return notify("请填写实际结果说明。", "error");
  try {
    const result = await api(
      PLUGIN + "/adaptive-learning/leads/" + encodeURIComponent(leadId(lead)) + "/human-material-outcome",
      { method: "POST", body: { outcome: input.outcome, note } },
    );
    state.modal = null;
    state.empowerment = null;
    notify(result.message || "实际结果已记录为来源学习样本。它不会影响机器评分。 ");
    await loadEmpowerment(true);
  } catch (error) {
    notify(error.message || "记录实际结果未完成。", "error");
  }
}

async function login(form, registration = false) {
  const input = Object.fromEntries(new FormData(form).entries());
  try {
    const result = await api(registration ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      body: registration ? { ...input, avatar: "" } : input,
    });
    const user = result.user || (await api("/api/auth/me"));
    writeSession(result.token, user);
    state.route = "overview";
    state.dashboard = null;
    state.chat = defaultChatEntries();
    state.chatSessions = null;
    state.chatSessionsLoading = false;
    state.chatHistoryLoading = false;
    state.chatSessionId = "";
    state.activeChatId = "";
    state.chatError = "";
    notify(
      registration
        ? "账号已创建。若看不到共享台账，请让管理员把你的用户名加入徐州团队名册。"
        : "登录成功，正在读取徐州招商台账。",
    );
    render();
    await loadDashboard(true);
  } catch (error) {
    notify(error.message || "登录未完成。", "error");
  }
}

async function createUser(form) {
  const input = Object.fromEntries(new FormData(form).entries());
  try {
    await api("/api/users", { method: "POST", body: { ...input, status: "active" } });
    state.modal = null;
    state.admin = null;
    notify("业务账号已创建。下一步请在徐州团队名册中添加该用户名。");
    await loadAdmin(true);
  } catch (error) {
    notify(error.message || "创建业务账号未完成。", "error");
  }
}

async function addMember(form) {
  const username = String(new FormData(form).get("username") || "").trim();
  if (!username) return notify("请输入要授权的平台用户名。", "error");
  try {
    await api(PLUGIN + "/team/members", { method: "POST", body: { username } });
    form.reset();
    state.admin = null;
    notify("已将 " + username + " 加入徐州招商团队。");
    await loadAdmin(true);
  } catch (error) {
    notify(error.message || "添加团队成员未完成。", "error");
  }
}

async function removeMember(username) {
  try {
    await api(PLUGIN + "/team/members/remove", { method: "POST", body: { username } });
    state.admin = null;
    notify("已移出 " + username + "；历史业务审计不会被删除。");
    await loadAdmin(true);
  } catch (error) {
    notify(error.message || "移出团队未完成。", "error");
  }
}

async function toggleUser(id, current) {
  const next = current === "active" ? "disabled" : "active";
  try {
    await api("/api/users/" + encodeURIComponent(id), {
      method: "PATCH",
      body: { status: next },
    });
    state.admin = null;
    notify(next === "active" ? "账号已恢复登录。" : "账号已停用。");
    await loadAdmin(true);
  } catch (error) {
    notify(error.message || "更新账号状态未完成。", "error");
  }
}

async function digest(action, id = "") {
  const suffix = action === "enable" ? "/enable" : "/subscriptions/" + encodeURIComponent(id) + "/" + action;
  try {
    await api(PLUGIN + "/dingtalk-daily-digest" + suffix, { method: "POST", body: {} });
    state.admin = null;
    notify(
      action === "enable"
        ? "每日 09:00 钉钉简报已启用，失败时会在 09:05 重试一次。"
        : action === "pause"
          ? "钉钉日报及重试已暂停。"
          : "已请求发送一次测试简报，请在钉钉中查看。",
    );
    await loadAdmin(true);
  } catch (error) {
    notify(error.message || "钉钉日报操作未完成。", "error");
  }
}

function chatEventText(event, previous) {
  if (event?.error) {
    throw new Error(typeof event.error === "string" ? event.error : "智能体响应失败");
  }
  const direct = [
    event?.text,
    event?.delta?.text,
    event?.content?.text,
    event?.message?.content,
    event?.output_text,
  ].find((value) => typeof value === "string" && value.trim());
  if (direct) {
    if (!previous || direct.startsWith(previous)) return direct;
    if (previous.endsWith(direct)) return previous;
    return previous + direct;
  }
  const nested = records(event, ["output"])
    .flatMap((entry) => records(entry, ["content"]))
    .map((entry) => entry?.text)
    .filter((value) => typeof value === "string")
    .join("\n");
  if (!nested) return previous;
  if (!previous || nested.startsWith(previous)) return nested;
  if (previous.endsWith(nested)) return previous;
  return previous + nested;
}

function chatHistoryText(value) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((part) => {
        if (typeof part === "string") return part;
        return String(pick(part, ["text", "content", "value"], ""));
      })
      .filter(Boolean)
      .join("\n");
  }
  return String(pick(value, ["text", "content", "message"], ""));
}

function historyEntries(value) {
  return records(value, ["messages", "items"])
    .map((entry) => ({
      role: String(entry?.role || entry?.message?.role || ""),
      text: chatHistoryText(entry?.content ?? entry?.message?.content ?? entry?.text),
    }))
    .filter((entry) => ["user", "assistant"].includes(entry.role) && entry.text.trim());
}

function sessionUpdatedAt(value) {
  const time = Date.parse(String(pick(value, ["updated_at", "created_at"], "")));
  return Number.isFinite(time) ? time : 0;
}

function scrollChatToLatest(smooth = false) {
  requestAnimationFrame(() => {
    document.querySelector("#chat-messages")?.scrollTo({ top: 999999, behavior: smooth ? "smooth" : "auto" });
  });
}

function startNewBusinessChat() {
  if (state.chatWorking) return;
  state.chatSessionId = createBusinessSessionId();
  state.activeChatId = "";
  state.chat = defaultChatEntries();
  state.chatError = "";
  state.chatHistoryLoading = false;
  render();
  scrollChatToLatest();
}

async function openBusinessChat(value) {
  const id = String(value?.id || "");
  const sessionId = String(value?.session_id || "");
  if (!id || !sessionId || state.chatWorking) return;
  state.chatSessionId = sessionId;
  state.activeChatId = id;
  state.chat = [];
  state.chatError = "";
  state.chatHistoryLoading = true;
  render();
  try {
    const history = await api("/api/chats/" + encodeURIComponent(id));
    if (state.chatSessionId !== sessionId) return;
    const entries = historyEntries(history);
    state.chat = entries.length ? entries : defaultChatEntries();
  } catch (error) {
    if (state.chatSessionId !== sessionId) return;
    state.chat = defaultChatEntries();
    state.chatError = "无法恢复这段历史会话：" + (error.message || "未知错误");
  } finally {
    if (state.chatSessionId === sessionId) {
      state.chatHistoryLoading = false;
      render();
      scrollChatToLatest();
    }
  }
}

async function loadBusinessChats(force = false) {
  if (state.chatSessionsLoading || (!force && state.chatSessions !== null)) return;
  const userId = currentUserId();
  if (!userId) return;
  state.chatSessionsLoading = true;
  if (state.route === "assistant") render();
  try {
    const query = new URLSearchParams({ user_id: userId, channel: "console", archived: "false" });
    const response = await api("/api/chats?" + query.toString());
    state.chatSessions = list(response).filter(isBusinessChat).sort((a, b) => sessionUpdatedAt(b) - sessionUpdatedAt(a));
    state.chatError = "";
    if (!state.chatSessionId && state.chatSessions.length) {
      state.chatSessionsLoading = false;
      await openBusinessChat(state.chatSessions[0]);
      return;
    }
    const active = state.chatSessions.find((entry) => String(entry.session_id || "") === state.chatSessionId);
    if (active) state.activeChatId = String(active.id || "");
  } catch (error) {
    state.chatSessions = [];
    state.chatError = "无法读取历史会话：" + (error.message || "未知错误");
  } finally {
    state.chatSessionsLoading = false;
    if (state.route === "assistant") render();
  }
}

async function archiveBusinessChat(id) {
  const chatId = String(id || "");
  if (!chatId || state.chatWorking) return;
  try {
    await api("/api/chats/" + encodeURIComponent(chatId) + "/archive", { method: "POST", body: {} });
    state.chatSessions = list(state.chatSessions).filter((entry) => String(entry.id || "") !== chatId);
    if (state.activeChatId === chatId) startNewBusinessChat();
    notify("这段业务对话已归档，不会再出现在当前会话列表中。");
    await loadBusinessChats(true);
  } catch (error) {
    notify(error.message || "归档会话未完成。", "error");
  }
}

async function stopBusinessChat() {
  if (!state.chatWorking || !state.activeChatId) return;
  try {
    await api("/api/console/chat/stop?chat_id=" + encodeURIComponent(state.activeChatId), {
      method: "POST",
      body: {},
    });
    notify("已请求停止本次回答；已生成的内容会保留在当前会话中。");
  } catch (error) {
    notify(error.message || "停止回答未完成。", "error");
  }
}

async function readChat(response, onUpdate) {
  const reader = response.body?.getReader();
  if (!reader) return "";
  const decoder = new TextDecoder();
  let buffer = "";
  let answer = "";
  const consume = (raw) => {
    for (const line of raw.replaceAll("\r", "").split("\n")) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        const next = chatEventText(JSON.parse(data), answer);
        if (next !== answer) {
          answer = next;
          onUpdate?.(answer);
        }
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
      }
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";
    events.forEach(consume);
  }
  if (buffer.trim()) consume(buffer);
  return answer;
}

async function chat(input) {
  const text = String(input || "").trim();
  if (!text || state.chatWorking) return;
  const sessionId = state.chatSessionId || createBusinessSessionId();
  state.chatSessionId = sessionId;
  state.chatError = "";
  state.chat.push({ role: "user", text });
  const answerIndex = state.chat.push({ role: "assistant", text: "", streaming: true }) - 1;
  state.chatWorking = true;
  render();
  scrollChatToLatest();
  try {
    const response = await fetch(apiUrl("/api/console/chat"), {
      method: "POST",
      headers: requestHeaders(true),
      body: JSON.stringify({
        // Keep the documented QwenPaw console envelope.  The server replaces
        // this user_id with the authenticated principal when login is enabled,
        // so the browser never chooses another user's identity.
        input: [{ role: "user", type: "message", content: [{ type: "text", text }] }],
        user_id: state.user?.id || state.user?.username || "business-user",
        session_id: sessionId,
        channel: "console",
        stream: true,
      }),
    });
    if (!response.ok) {
      throw new Error(
        errorText(await response.json().catch(() => ({})), "对话未完成（HTTP " + response.status + "）"),
      );
    }
    // The server creates the ChatSpec before it starts streaming. Refreshing it
    // in the background lets the first-ever conversation use “停止回答” too.
    void loadBusinessChats(true);
    const answer = await readChat(response, (next) => {
      const entry = state.chat[answerIndex];
      if (!entry) return;
      entry.text = next;
      render();
      scrollChatToLatest();
    });
    const entry = state.chat[answerIndex];
    if (entry) {
      entry.text = answer || "本次对话已完成，但服务器没有返回可展示的文字。";
      entry.streaming = false;
    }
  } catch (error) {
    const entry = state.chat[answerIndex];
    if (entry) {
      entry.text = entry.text || "本次对话未完成：" + (error.message || "未知错误");
      entry.streaming = false;
    }
  }
  state.chatWorking = false;
  await loadBusinessChats(true);
  render();
  scrollChatToLatest(true);
}

function render() {
  const root = document.querySelector("#app");
  if (!root) return;
  if (!state.token || !state.user) {
    root.innerHTML = authPage();
    return;
  }
  const pages = { overview, queue, human: humanMaterials, leads, learning, assistant, admin };
  root.innerHTML = shell((pages[state.route] || overview)());
  if (["overview", "queue", "leads"].includes(state.route)) void loadDashboard();
  if (["human", "learning"].includes(state.route)) void loadEmpowerment();
  if (state.route === "admin") void loadAdmin();
  if (state.route === "assistant" && state.chatSessions === null && !state.chatSessionsLoading) {
    void loadBusinessChats();
  }
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action], [data-route]");
  if (!target) return;
  const { action, route, id, tab, username, status } = target.dataset;
  if (route) {
    state.route = route;
    state.modal = null;
    render();
    return;
  }
  if (action === "show-login") {
    state.authMode = "login";
    render();
  } else if (action === "show-register") {
    state.authMode = "register";
    render();
  } else if (action === "logout") {
    clearSession();
    notify("已退出登录。");
  } else if (action === "toggle-menu") {
    document.querySelector(".sidebar")?.classList.toggle("open");
  } else if (action === "refresh-dashboard") {
    state.dashboard = null;
    void loadDashboard(true);
  } else if (action === "refresh-all") {
    if (["human", "learning"].includes(state.route)) {
      state.empowerment = null;
      void loadEmpowerment(true);
    } else {
      state.dashboard = null;
      void loadDashboard(true);
    }
  } else if (action === "refresh-admin") {
    state.admin = null;
    void loadAdmin(true);
  } else if (action === "open-guide") {
    state.modal = { type: "guide" };
    render();
  } else if (action === "set-queue") {
    state.queueTab = tab;
    render();
  } else if (action === "set-admin") {
    state.adminTab = tab;
    render();
  } else if (action === "open-discovery") {
    void openDiscovery(id);
  } else if (action === "open-observation") {
    void openObservation(id);
  } else if (action === "open-lead") {
    void openLead(id);
  } else if (action === "close-modal") {
    state.modal = null;
    render();
  } else if (action === "open-review") {
    state.modal.review = true;
    render();
  } else if (action === "cancel-review") {
    state.modal.review = false;
    render();
  } else if (action === "use-human-material") {
    prepareHumanMaterial();
  } else if (action === "open-draft-confirm") {
    const draft = draftInCurrentMaterial(id);
    const parent = state.modal?.data;
    if (draft) {
      state.modal = { type: "draft-confirm", draft, parent };
      render();
    }
  } else if (action === "open-draft-review") {
    const draft = draftInCurrentMaterial(id);
    const parent = state.modal?.data;
    if (draft) {
      state.modal = { type: "draft-review", draft, parent };
      render();
    }
  } else if (action === "back-observation") {
    restoreMaterial(state.modal?.parent);
  } else if (action === "open-followup") {
    state.modal = { type: "followup", lead: state.modal.lead, card: state.modal.card };
    render();
  } else if (action === "back-lead") {
    state.modal = { type: "lead", lead: state.modal.lead, card: state.modal.card };
    render();
  } else if (action === "open-human-outcome") {
    state.modal = { type: "human-outcome", lead: state.modal.lead, card: state.modal.card };
    render();
  } else if (action === "toggle-source") {
    const source = list(state.empowerment?.sources).find((entry) => String(entry.source_id || "") === String(id || ""));
    if (source) {
      state.modal = { type: "source-status", source, nextStatus: status === "active" ? "disabled" : "active" };
      render();
    }
  } else if (action === "retest-public-source") {
    void retestPublicSource(id);
  } else if (action === "toggle-public-source") {
    void togglePublicSource(id, status);
  } else if (action === "enable-learning-schedule") {
    void saveLearningSchedule(true);
  } else if (action === "pause-learning-schedule") {
    void saveLearningSchedule(false);
  } else if (action === "open-create-user") {
    state.modal = { type: "create-user" };
    render();
  } else if (action === "remove-member") {
    void removeMember(username);
  } else if (action === "toggle-user") {
    void toggleUser(id, status);
  } else if (action === "enable-digest") {
    void digest("enable");
  } else if (action === "pause-digest") {
    void digest("pause", id);
  } else if (action === "run-digest") {
    void digest("run", id);
  } else if (action === "new-chat") {
    startNewBusinessChat();
  } else if (action === "refresh-chats") {
    void loadBusinessChats(true);
  } else if (action === "open-chat") {
    const chatRecord = list(state.chatSessions).find((entry) => String(entry.id || "") === String(id || ""));
    if (chatRecord) void openBusinessChat(chatRecord);
  } else if (action === "archive-chat") {
    void archiveBusinessChat(id);
  } else if (action === "stop-chat") {
    void stopBusinessChat();
  } else if (action === "suggestion") {
    const textarea = document.querySelector("#chat-form textarea");
    if (textarea) {
      textarea.value = target.textContent.trim();
      textarea.focus();
    }
  }
});

document.addEventListener("change", (event) => {
  if (event.target?.id === "candidate-decision") {
    document
      .querySelector("#waiver-fields")
      ?.classList.toggle("visible", event.target.value === "waive_xuzhou");
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.id === "login-form") {
    event.preventDefault();
    await login(form);
  } else if (form.id === "register-form") {
    event.preventDefault();
    await login(form, true);
  } else if (form.id === "candidate-review-form") {
    event.preventDefault();
    await saveCandidateReview(form);
  } else if (form.id === "draft-confirm-form") {
    event.preventDefault();
    await confirmDraft(form);
  } else if (form.id === "draft-review-form") {
    event.preventDefault();
    await saveDraftReview(form);
  } else if (form.id === "followup-form") {
    event.preventDefault();
    await saveFollowup(form);
  } else if (form.id === "human-evidence-form") {
    event.preventDefault();
    await saveHumanEvidence(form);
  } else if (form.id === "learning-policy-form") {
    event.preventDefault();
    await saveLearningPolicy(form);
  } else if (form.id === "trusted-source-form") {
    event.preventDefault();
    await registerTrustedSource(form);
  } else if (form.id === "public-source-form") {
    event.preventDefault();
    await addPublicSource(form);
  } else if (form.id === "source-status-form") {
    event.preventDefault();
    await saveSourceStatus(form);
  } else if (form.id === "human-material-outcome-form") {
    event.preventDefault();
    await saveHumanMaterialOutcome(form);
  } else if (form.id === "create-user-form") {
    event.preventDefault();
    await createUser(form);
  } else if (form.id === "add-member-form") {
    event.preventDefault();
    await addMember(form);
  } else if (form.id === "chat-form") {
    event.preventDefault();
    const value = String(new FormData(form).get("message") || "");
    form.reset();
    await chat(value);
  }
});

async function initialise() {
  if (state.token) {
    try {
      writeSession(state.token, await api("/api/auth/me"));
    } catch {
      clearSession();
    }
  }
  render();
}

void initialise();
