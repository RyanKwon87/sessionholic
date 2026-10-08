"use strict";

// Session content is rendered as text. No transcript, draft, auth or terminal data is persisted here.
const GROUPS = [
  { key: "needs_input", label: "내 답이 필요한 작업", tone: "red" },
  { key: "working", label: "진행 중", tone: "amber" },
  { key: "rest", label: "최근 작업", tone: "gray" },
];
const BADGES = {
  needs_input: ["답 필요", "red"],
  working: ["작업 중", "amber"],
  idle: ["열림", "gray"],
  done: ["완료", "gray"],
  closed: ["닫힘", "gray"],
};
const AGENTS = { claude: "Claude Code", codex: "Codex" };
const POLL_MS = 60000;
const RECENT_SECONDS = 3 * 24 * 3600;
const INPUT_CLEANUP_NOTICE = "터미널은 닫혔지만 입력 기록 정리가 남았습니다. 목록을 새로고침해 다시 확인해 주세요.";
const $ = (id) => document.getElementById(id);
const prefs = {
  get(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem("sessionholic:ui:" + key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("sessionholic:ui:" + key, JSON.stringify(value));
    } catch {
      /* optional preferences */
    }
  },
};
const state = {
  snapshot: null,
  capabilities: null,
  capabilitiesRevision: null,
  refreshDeadline: 0,
  csrf: "",
  selected: null,
  filters: { host: "", agent: "", project: "", account: "" },
  accountFilterRoutes: null,
  search: "",
  showOld: false,
  detailFor: null,
  detailUpdatedAt: null,
  detailRevision: null,
  detailRun: 0,
  route: null,
  plan: null,
  planRun: 0,
  launchBusy: false,
  pollTimer: null,
  pollBusy: false,
  authenticated: false,
  authEpoch: 0,
  loginBusy: false,
  offline: false,
  terminals: [],
  terminalsRefreshing: false,
  terminalsError: "",
  terminal: null,
  terminalVisible: false,
  term: null,
  fit: null,
  terminalAfter: 0,
  terminalEpoch: null,
  terminalRun: 0,
  terminalAbort: null,
  connected: false,
  terminalAlive: true,
  inputQueue: Promise.resolve(),
  inputEpoch: 0,
  inputFailed: false,
  drafts: new Map(),
  composing: false,
  compositionOwner: null,
  draftRevision: 0,
  draftPaste: null,
  closingTerminal: null,
  terminalCloseBusy: false,
  resizeTimer: null,
  lastSize: "",
  fontSize: prefs.get("fontSize", 14),
  screenReaderMode: prefs.get("screenReaderMode", false) === true,
  toastTimer: null,
  chats: new Map(),
  nativeSessions: new Map(),
  chatPollTimer: null,
  chatPollRun: 0,
  chatPollCount: 0,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
function button(text, className, action) {
  const node = el("button", className, text);
  node.type = "button";
  if (action) node.addEventListener("click", action);
  return node;
}
function ago(ts, now = Math.floor(Date.now() / 1000)) {
  if (!ts) return "확인 전";
  const d = Math.max(0, now - ts);
  if (d < 60) return "방금";
  if (d < 3600) return `${Math.floor(d / 60)}분 전`;
  if (d < 86400) return `${Math.floor(d / 3600)}시간 전`;
  return `${Math.floor(d / 86400)}일 전`;
}
function clock(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function requestId() {
  return (
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}
function toast(text) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3500);
}
function updateInputCleanupNotice(pending) {
  if (pending) notice(INPUT_CLEANUP_NOTICE);
  else if ($("connection-notice").textContent === INPUT_CLEANUP_NOTICE) notice();
}
function notice(text = "") {
  const box = $("connection-notice");
  box.textContent = text;
  box.hidden = !text;
}

function assertAuthEpoch(epoch) {
  if (epoch === state.authEpoch) return;
  const err = new Error("이전 접속에서 시작한 요청입니다.");
  err.staleAuth = true;
  throw err;
}
function beginAuthEpoch() {
  closeChatQueue(false);
  state.authEpoch++;
  state.pollBusy = false;
  state.terminalsRefreshing = false;
  return state.authEpoch;
}
async function api(path, { method = "GET", body, signal, authEpoch = state.authEpoch } = {}) {
  assertAuthEpoch(authEpoch);
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  let response;
  try {
    response = await fetch(path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", cache: "no-store", signal,
    });
  } catch (err) { assertAuthEpoch(authEpoch); throw err; }
  assertAuthEpoch(authEpoch);
  if (response.status === 401) {
    showLogin();
    const err = new Error("로그인이 필요합니다.");
    err.unauthorized = true;
    throw err;
  }
  const data = await response.json().catch(() => ({}));
  assertAuthEpoch(authEpoch);
  if (!response.ok) {
    const err = new Error(
      typeof data.error === "string"
        ? data.error
        : `요청을 완료하지 못했습니다 (${response.status}).`,
    );
    err.status = response.status;
    err.code = data.code;
    err.dispatchState = data.dispatchState;
    if (data.recovery && typeof data.recovery === "object" && !Array.isArray(data.recovery)) err.recovery = data.recovery;
    throw err;
  }
  return data;
}

function allSessions() {
  const out = [];
  for (const host of state.snapshot?.hosts || []) {
    for (const s of [
      ...(host.data?.claude || []),
      ...(host.data?.codex || []),
    ]) {
      out.push({
        ...s,
        host: host.name,
        account: sessionAccountLabel({ ...s, host: host.name }),
        hostLabel: host.label,
        stale: !host.ok,
        fetchedAt: host.fetchedAt,
        key: [host.name, s.agent, s.home || "", s.id].join("|"),
      });
    }
  }
  for (const session of state.nativeSessions.values()) {
    if (!out.some((s) => s.key === session.key)) out.push({ ...session, account: sessionAccountLabel(session) });
  }
  return out;
}
function sourceOf(s) {
  const source = {
    host: s.host,
    agent: s.agent,
    id: s.id,
  };
  if (s.home) source.home = s.home;
  return source;
}
function accountOf(s) {
  return s.agent === "claude"
    ? "claude:default"
    : `codex:${s.home || ".codex"}`;
}
function selectedSession() {
  return allSessions().find((s) => s.key === state.selected);
}
function groupOf(s) {
  return s.phase === "needs_input"
    ? "needs_input"
    : s.phase === "working"
      ? "working"
      : "rest";
}
function matches(s, now) {
  for (const key of Object.keys(state.filters)) {
    if (key === "account" && state.filters.account && state.accountFilterRoutes) {
      if (!state.accountFilterRoutes.has(accountRouteKey(s))) return false;
      continue;
    }
    if (state.filters[key] && s[key] !== state.filters[key]) return false;
  }
  if (
    !state.showOld &&
    groupOf(s) === "rest" &&
    now - (s.updatedAt || 0) > RECENT_SECONDS
  )
    return false;
  if (!state.search) return true;
  const q = state.search.toLocaleLowerCase();
  return [
    s.title,
    s.project,
    s.cwd,
    s.account,
    s.snippet,
    s.hostLabel,
    AGENTS[s.agent],
  ].some((v) =>
    String(v || "")
      .toLocaleLowerCase()
      .includes(q),
  );
}

function render() {
  if (!state.snapshot) return;
  const now = state.snapshot.serverTime || Date.now() / 1000;
  const sessions = allSessions();
  const shown = sessions.filter((s) => matches(s, now));
  renderHosts(now);
  renderCounts(shown);
  renderFilters(sessions);
  renderGroups(shown, now);
  $("session-count").textContent = `${shown.length}개`;
  const selected = sessions.find((s) => s.key === state.selected);
  if (!state.selected && !$("detail").children.length) {
    const welcome = el("div", "welcome");
    welcome.append(
      el("span", "welcome-mark", "↗"),
      el("h2", null, "맥락부터 확인하세요."),
      el(
        "p",
        null,
        "작업을 고르면 최근 대화를 볼 수 있습니다.\n이어서 사용할 에이전트와 계정도 여기서 선택하세요.",
      ),
    );
    $("detail").append(welcome);
  }
  if (selected) refreshDetail(selected);
  else if (state.selected) {
    closeChatQueue(false);
    state.selected = null;
    state.detailFor = null;
    $("detail").replaceChildren(
      el(
        "div",
        "welcome",
        "이 작업을 목록에서 더 이상 찾을 수 없습니다. 새로고침 후 확인해 주세요.",
      ),
    );
  }
}
function renderHosts(now) {
  const box = $("hosts");
  box.replaceChildren();
  for (const host of state.snapshot.hosts) {
    const chip = el(
      "span",
      `host ${host.refreshing ? "checking" : host.ok ? "ok" : "bad"}`,
    );
    chip.append(el("span", "dot"), el("span", null, host.label));
    chip.title = host.refreshing
      ? `${host.label} · 작업 확인 중`
      : host.ok
        ? `${host.label} · ${ago(host.fetchedAt, now)} 확인`
        : `${host.label} · ${host.error || "연결 확인 필요"}${host.fetchedAt ? ` · 마지막 확인 ${ago(host.fetchedAt, now)}` : ""}`;
    chip.setAttribute("aria-label", chip.title);
    box.append(chip);
  }
}
function renderCounts(shown) {
  const box = $("counts");
  box.replaceChildren();
  for (const group of GROUPS.slice(0, 2)) {
    const n = shown.filter((s) => groupOf(s) === group.key).length;
    const pill = el("span", `count ${group.tone}${n ? "" : " zero"}`);
    pill.append(
      el("span", "dot"),
      el(
        "span",
        null,
        `${group.key === "needs_input" ? "답 필요" : "작업 중"} ${n}`,
      ),
    );
    box.append(pill);
  }
}
function renderFilters(sessions) {
  const active = Object.values(state.filters).filter(Boolean).length;
  $("filter-count").textContent = active ? `${active}개 적용` : "";
  const box = $("filter-fields");
  const focused = document.activeElement?.id;
  if (focused?.startsWith("filter-")) return;
  box.replaceChildren();
  for (const [key, label, pick] of [
    ["host", "기기", (s) => [s.host, s.hostLabel]],
    ["agent", "에이전트", (s) => [s.agent, AGENTS[s.agent]]],
    ["project", "프로젝트", (s) => [s.project, s.project]],
    ["account", "계정 설정", (s) => [s.account, s.account]],
  ]) {
    const options = new Map(sessions.map(pick).filter(([v]) => v));
    const field = el("label", null, label);
    const select = el("select");
    select.id = `filter-${key}`;
    select.append(new Option("전체", ""));
    for (const [value, text] of [...options].sort((a, b) =>
      String(a[1]).localeCompare(String(b[1]), "ko"),
    ))
      select.append(new Option(text, value));
    if (state.filters[key] && !options.has(state.filters[key]))
      select.append(new Option(`${state.filters[key]} · 마지막 선택`, state.filters[key]));
    select.value = state.filters[key];
    select.addEventListener("change", () => {
      state.filters[key] = select.value;
      if (key === "account") state.accountFilterRoutes = null;
      render();
    });
    field.append(select);
    box.append(field);
  }
  const n = Object.values(state.filters).filter(Boolean).length;
  $("filter-count").textContent = n ? `${n}개 적용` : "";
}
function cardLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}
function cardProject(s) {
  const project = cardLine(s.project);
  return project === "~" ? "홈 폴더" : project || "프로젝트 확인 필요";
}
function cardFolderParts(s) {
  const cwd = cardLine(s.cwd).replace(/\\/g, "/").replace(/\/+$/, "");
  if (!cwd) return cardLine(s.cwd).startsWith("/") ? ["최상위 폴더"] : [];
  if (cwd === "~" || cwd === "/root") return ["홈 폴더"];
  if (cwd === ".") return ["현재 폴더"];
  let parts = cwd.split("/").filter(Boolean);
  if (parts[0] === "~") parts = parts.slice(1);
  else if (["users", "home"].includes(parts[0]?.toLowerCase()) && parts.length >= 2) parts = parts.slice(2);
  else if (/^[A-Za-z]:$/.test(parts[0] || "") && parts[1]?.toLowerCase() === "users" && parts.length >= 3) parts = parts.slice(3);
  return parts.length ? parts : ["홈 폴더"];
}
function cardFolderHints(shown) {
  const folders = shown.map((s) => ({ s, project: cardProject(s), parts: cardFolderParts(s), cwd: cardLine(s.cwd) }));
  const hints = new Map();
  const suffix = (parts, depth) => parts.slice(-depth).join(" / ");
  for (const folder of folders) {
    const { s, project, parts, cwd } = folder;
    if (!parts.length) { hints.set(s.key, ""); continue; }
    const peers = folders.filter((other) => other.project === project && other.cwd !== cwd && other.parts.length);
    if (!peers.length && parts.at(-1).toLocaleLowerCase() === project.toLocaleLowerCase()) {
      hints.set(s.key, ""); continue;
    }
    let depth = Math.min(2, parts.length);
    while (depth < parts.length && peers.some((other) => suffix(other.parts, depth) === suffix(parts, depth))) depth++;
    hints.set(s.key, suffix(parts, depth));
  }
  return hints;
}
function placeChildren(parent, desired) {
  for (const child of [...parent.children]) if (!desired.includes(child)) parent.removeChild(child);
  for (let index = 0; index < desired.length; index++)
    if (parent.children[index] !== desired[index]) parent.insertBefore(desired[index], parent.children[index] || null);
}
function renderGroups(shown, now) {
  const box = $("groups");
  const pane = $("task-list-pane");
  const scroll = pane?.scrollTop;
  const active = document.activeElement;
  const sections = new Map();
  const cards = new Map();
  // Reuse only what is mounted now; lock clears this DOM and its references.
  for (const section of box.children) {
    if (section.dataset.groupKey) sections.set(section.dataset.groupKey, section);
    for (const node of section.children)
      if (node.dataset.sessionKey) cards.set(node.dataset.sessionKey, node);
  }
  const focused = active?.dataset?.sessionKey && cards.get(active.dataset.sessionKey) === active ? active : null;
  const folders = cardFolderHints(shown);
  const desired = [];
  const visible = new Set();
  for (const group of GROUPS) {
    const items = shown
      .filter((s) => groupOf(s) === group.key)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    if (!items.length) continue;
    let section = sections.get(group.key);
    if (!section) {
      section = el("section", "group");
      section.dataset.groupKey = group.key;
      const head = el("h2", `group-head ${group.tone}`, group.label);
      const count = el("span", "group-count");
      head.append(count);
      section.cardHeading = head;
      section.cardCount = count;
    }
    section.cardCount.textContent = items.length;
    const children = [section.cardHeading];
    for (const session of items) {
      const source = chatKey(sourceOf(session));
      const previous = cards.get(session.key);
      const node = previous?.dataset.sourceKey === source ? previous : card(session);
      updateCard(node, session, now, folders.get(session.key));
      children.push(node); visible.add(node);
    }
    placeChildren(section, children);
    desired.push(section);
  }
  if (!shown.length) {
    const filtered = state.search || Object.values(state.filters).some(Boolean);
    const unconfigured = !state.snapshot?.hosts?.length;
    const offline = state.snapshot?.hosts?.length && state.snapshot.hosts.every((host) => !host.ok && !host.refreshing);
    const empty = el("div", "empty-list");
    empty.append(el("p", null, filtered ? "조건에 맞는 작업이 없습니다." : unconfigured
      ? "연결할 기기가 아직 설정되지 않았습니다. 설치 안내에 따라 기기를 설정한 뒤 새로고침해 주세요." : offline
      ? "기기에 연결되지 않아 작업을 확인하지 못했습니다. 기기 연결과 에이전트 설치 상태를 확인한 뒤 다시 확인해 주세요."
      : state.snapshot?.hosts?.some((host) => host.refreshing)
        ? "기기에서 작업을 확인하고 있습니다…"
        : "표시할 최근 작업이 없습니다. 설정한 기기에서 Claude Code나 Codex로 대화를 시작한 뒤 새로고침해 주세요. 이전 대화는 필터에서 확인할 수 있습니다."));
    if (filtered) empty.append(button("검색·필터 초기화", "secondary", () => {
      state.search = "";
      for (const key of Object.keys(state.filters)) state.filters[key] = "";
      state.accountFilterRoutes = null;
      $("search").value = "";
      render();
    }));
    else if (offline) empty.append(button("다시 확인", "secondary", manualRefresh));
    desired.push(empty);
  }
  placeChildren(box, desired);
  if (focused && visible.has(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
  if (typeof scroll === "number") pane.scrollTop = scroll;
}
function card(s) {
  const node = button("", "card", () => {
    const mounted = [...$("groups").children].some((section) => [...section.children].includes(node));
    const current = allSessions().find((session) => session.key === node.dataset.sessionKey);
    if (state.authenticated && mounted && current && chatKey(sourceOf(current)) === node.dataset.sourceKey) select(current.key);
  });
  node.dataset.sessionKey = s.key;
  node.dataset.sourceKey = chatKey(sourceOf(s));
  const project = el("span", "card-project");
  const title = el("span", "card-title");
  const snippet = el("span", "card-snippet");
  const folder = el("span", "card-folder");
  const meta = el("span", "card-meta");
  const badge = el("span", "badge");
  const agent = el("span", "card-agent");
  const host = el("span", "card-host");
  const time = el("span", "card-time");
  meta.append(badge, agent, host, time);
  const accountLine = el("span", "card-account-line");
  const account = el("span", "card-account");
  const stale = el("span", "stale-tag", "마지막 확인 기준");
  accountLine.append(account, stale);
  node.append(project, title, snippet, folder, meta, accountLine);
  node.cardParts = { project, title, snippet, folder, badge, agent, host, time, accountLine, account, stale };
  return node;
}
function updateCard(node, s, now, folderHint = "") {
  const p = node.cardParts;
  const write = (target, value) => { if (target.textContent !== value) target.textContent = value; };
  const project = cardProject(s);
  const rawTitle = cardLine(s.title);
  const title = !rawTitle || ["(제목 없음)", "(이름 없음)"].includes(rawTitle) ? "제목 없는 작업" : rawTitle;
  const snippet = cardLine(s.snippet);
  const preview = snippet && snippet !== rawTitle && snippet !== title ? snippet : "";
  const agent = AGENTS[s.agent] || s.agent || "에이전트";
  const host = s.hostLabel || s.host || "기기 확인 필요";
  const account = cardLine(s.account);
  const [label, tone] = BADGES[s.phase] || [s.phase || "상태 확인 필요", "gray"];
  const time = ago(s.updatedAt, now);
  node.className = `card${state.selected === s.key ? " selected" : ""}`;
  node.setAttribute("aria-pressed", String(state.selected === s.key));
  node.setAttribute("aria-label", ["프로젝트: " + project, "작업: " + title,
    preview ? "대화 미리보기: " + preview : "", folderHint ? "작업 폴더: " + folderHint : "",
    label, agent, "기기: " + host, account ? "계정 설정: " + account : "", time,
    s.stale ? "마지막 확인 기준" : ""].filter(Boolean).join(". "));
  node.title = `${project}\n${title}`;
  write(p.project, project); p.project.title = project;
  write(p.title, title); p.title.title = title;
  write(p.snippet, preview ? `대화 미리보기 · ${preview}` : ""); p.snippet.hidden = !preview; p.snippet.title = preview;
  write(p.folder, folderHint ? `작업 폴더 · ${folderHint}` : ""); p.folder.hidden = !folderHint; p.folder.title = folderHint;
  write(p.badge, label); p.badge.className = `badge ${tone}`;
  write(p.agent, agent); p.agent.className = `card-agent ${s.agent}`;
  write(p.host, host); p.host.title = host;
  write(p.time, time);
  write(p.account, account ? `계정 설정 · ${account}` : ""); p.account.title = account; p.account.hidden = !account;
  p.stale.hidden = !s.stale;
  p.accountLine.hidden = !account && !s.stale;
}

function focusDetailBack() {
  $("detail").querySelector(".back")?.focus({ preventScroll: true });
}
function restoreSelectedCardFocus() {
  const card = [...$("groups").querySelectorAll(".card")].find((node) => node.dataset.sessionKey === state.selected);
  (card || $("home")).focus({ preventScroll: true });
}
function select(key) {
  if (state.launchBusy) return;
  const mobile = window.matchMedia("(max-width: 700px)").matches;
  if (
    window.matchMedia("(max-width: 700px)").matches &&
    !document.body.classList.contains("show-detail")
  )
    navigateView("detail");
  if (state.selected === key) {
    document.body.classList.add("show-detail");
    if (mobile) focusDetailBack();
    return;
  }
  closeChatQueue(false);
  state.selected = key;
  stopChatPolling();
  state.detailFor = null;
  state.route = null;
  document.body.classList.add("show-detail");
  render();
  if (mobile) focusDetailBack();
}
function refreshDetail(s) {
  const revision = JSON.stringify([s.stale, s.phase, s.title, s.project, s.account, s.hostLabel, s.fetchedAt,
    s.cwd || null, s.sessionId || null]);
  if (
    state.detailFor === s.key &&
    state.detailUpdatedAt === s.updatedAt &&
    state.detailRevision === revision
  )
    return;
  const fresh = state.detailFor !== s.key;
  state.detailFor = s.key;
  state.detailUpdatedAt = s.updatedAt;
  state.detailRevision = revision;
  if (fresh) drawDetail(s);
  else patchDetailMetadata(s);
  loadDetail(s, fresh);
}
function detailMetaChildren(s) {
  const [label, tone] = BADGES[s.phase] || [s.phase || "상태 확인 필요", "gray"];
  return [el("span", `badge ${tone}`, label),
    el("span", null, [AGENTS[s.agent], s.account, s.hostLabel].filter(Boolean).join(" · ")),
    el("span", null, clock(s.updatedAt))];
}
function patchDetailMetadata(s) {
  const pane = $("detail");
  if (pane.dataset.key !== s.key) return;
  const title = pane.querySelector(".detail-title");
  const eyebrow = pane.querySelector(".detail-titles .eyebrow");
  if (title) title.textContent = s.title || "제목 없는 작업";
  if (eyebrow) eyebrow.textContent = s.project || "RECENT CONVERSATION";
  pane.querySelector(".detail-meta")?.replaceChildren(...detailMetaChildren(s));
  let note = pane.querySelector(".stale-notice");
  if (s.stale) {
    if (!note) {
      note = el("div", "stale-notice");
      pane.querySelector(".detail-head")?.after(note);
    }
    note.textContent = `${s.hostLabel}에 연결되지 않았습니다. ${s.fetchedAt ? `${ago(s.fetchedAt)} 확인한` : "저장된"} 정보입니다.`;
  } else note?.remove();
  const route = pane.querySelector(".chat-route");
  if (route) route.textContent = `${s.hostLabel || s.host} · ${AGENTS[s.agent] || s.agent} · ${sessionAccountLabel(s)}`;
}
async function loadDetail(s, fresh = false) {
  const run = ++state.detailRun;
  const authEpoch = state.authEpoch;
  const identity = conversationIdentity(s);
  const entry = chatEntry(s);
  const stateEpoch = entry.stateEpoch;
  const viewCurrent = () => run === state.detailRun && authEpoch === state.authEpoch &&
    state.authenticated && state.selected === s.key && conversationIdentity(selectedSession()) === identity &&
    stateEpoch === entry.stateEpoch;
  const cached = entry.conversation;
  const usableCache = cached && cached.authEpoch === authEpoch && cached.identity === identity;
  if (usableCache) {
    renderMessages(s, cached.messages, "", fresh);
    conversationSyncNotice(s, "checking");
  }
  if (networkOffline() || !chatVisible()) {
    conversationSyncNotice(s, "stale");
    if (!usableCache && networkOffline()) renderMessages(s, null, "오프라인입니다. 연결 후 대화를 다시 확인해 주세요.");
    return;
  }
  const live = await loadChatState(s, true, { join: true });
  if (!viewCurrent()) return;
  if (live?.status === "success" && chatReadSupported(entry, live.data)) {
    conversationSyncNotice(s, live.data.stale ? "stale" : "");
    return;
  }
  const params = new URLSearchParams({
    ...sourceOf(s),
    id: s.agent === "claude" ? s.sessionId || s.id : s.id,
  });
  const fallbackSnapshot = entry.conversation;
  try {
    const data = await api(`/api/read?${params}`, { authEpoch });
    if (!viewCurrent()) return;
    if (entry.conversation !== fallbackSnapshot && chatReadSupported(entry)) return;
    const messages = data.messages || [];
    rememberConversation(s, messages, data);
    renderMessages(s, messages, data.error || "", fresh && !usableCache);
    updateReadNotice(data);
    conversationSyncNotice(s, data.error ? "failed" : data.stale ? "stale" : "");
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth && viewCurrent()) {
      if (entry.conversation !== fallbackSnapshot && chatReadSupported(entry)) return;
      renderMessages(s, null, err.message);
      conversationSyncNotice(s, "failed");
    }
  }
}

function drawDetail(s) {
  const pane = $("detail");
  pane.replaceChildren();
  pane.dataset.key = s.key;
  const head = el("div", "detail-head");
  head.append(button("←", "back", backToList));
  const titles = el("div", "detail-titles");
  titles.append(
    el("p", "eyebrow", s.project || "RECENT CONVERSATION"),
    el("h1", "detail-title", s.title || "제목 없는 작업"),
  );
  const meta = el("div", "detail-meta");
  meta.append(...detailMetaChildren(s));
  titles.append(meta);
  head.append(titles);
  pane.append(head);
  if (s.stale)
    pane.append(
      el(
        "div",
        "stale-notice",
        `${s.hostLabel}에 연결되지 않았습니다. ${s.fetchedAt ? `${ago(s.fetchedAt)} 확인한` : "저장된"} 정보입니다.`,
      ),
    );
  const bar = el("div", "conversation-bar");
  bar.append(
    el("strong", null, "최근 대화"),
    button("맨 아래 ↓", "", () => {
      const list = $("messages");
      list.scrollTop = list.scrollHeight;
    }),
  );
  const list = el("div", "messages");
  list.id = "messages";
  list.setAttribute("aria-label", "최근 대화 읽기");
  list.append(el("p", "group-empty", "대화를 불러오고 있습니다…"));
  const sync = el("p", "conversation-sync-notice");
  sync.id = "conversation-sync-notice";
  sync.hidden = true;
  sync.setAttribute("role", "status");
  pane.append(bar, sync, list, buildChatComposer(s), buildContinuePanel(s));
}

// Drafts and attachment handles stay in memory and are keyed by the actual
// runtime identity, independently of the continuation/transfer route selectors.
function chatKey(source) {
  return JSON.stringify([source.host, source.agent, source.home || "", source.id]);
}
function conversationIdentity(s) {
  if (!s) return null;
  return JSON.stringify([chatKey(sourceOf(s)), s.cwd || null, s.agent === "claude" ? s.sessionId || s.id : s.id]);
}
function chatReadSupported(entry, data = entry.data) {
  return !!data && !data.error && chatKey(data.route || {}) === chatKey(entry.source) && Array.isArray(data.messages);
}
function rememberConversation(s, messages, data = {}) {
  if (!Array.isArray(messages) || data.error) return;
  chatEntry(s).conversation = { identity: conversationIdentity(s), authEpoch: state.authEpoch,
    messages, fetchedAt: data.fetchedAt || Date.now() / 1000, stale: !!data.stale };
}
function conversationSyncNotice(s, status) {
  if (state.selected !== s.key || $("detail").dataset.key !== s.key) return;
  const cached = chatEntry(s).conversation;
  const sameCache = cached && cached.authEpoch === state.authEpoch && cached.identity === conversationIdentity(s);
  const text = status === "checking" ? "저장된 대화 · 최신 내용 확인 중…"
    : status === "failed" && sameCache ? "저장된 대화 · 최신 내용을 확인하지 못했습니다."
    : status === "stale" && sameCache ? "저장된 대화 · 연결 후 최신 내용을 확인해 주세요." : "";
  const notice = $("conversation-sync-notice");
  notice.textContent = text;
  notice.hidden = !text;
}
function attachmentMetadata(files) {
  if (!Array.isArray(files)) return [];
  return files.filter((file) => file && typeof file === "object").map((file) => {
    const type = typeof file.type === "string" ? file.type : "";
    const kind = file.kind === "image" || file.kind !== "file" && type.startsWith("image/") ? "image" : "file";
    return { id: typeof file.id === "string" ? file.id : null,
      name: typeof file.name === "string" && file.name ? file.name : kind === "image" ? "이미지 첨부" : "첨부파일",
      type, size: Number.isInteger(file.size) && file.size >= 0 ? file.size : null, kind,
      source: file.source === "native" ? "native" : "sessionholic" };
  });
}
function attachmentGroup(files, caption = `첨부 ${files.length}개`) {
  const group = el("div", "msg-attachments");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", caption);
  group.append(el("span", "attachment-caption", caption));
  const chips = el("div", "attachment-chips");
  chips.setAttribute("role", "list");
  for (const file of files) {
    const kind = file.kind === "image" ? "이미지" : "파일";
    const size = file.size == null ? "" : formatBytes(file.size);
    const chip = el("span", "msg-attachment");
    chip.setAttribute("role", "listitem");
    chip.setAttribute("aria-label", `${kind}: ${file.name}${size ? `, ${size}` : ""}`);
    chip.title = file.name;
    chip.append(el("span", "attachment-kind", kind), el("span", "attachment-name", file.name));
    if (size) chip.append(el("span", "attachment-size", size));
    chips.append(chip);
  }
  group.append(chips);
  return group;
}
function updateChatDeliveryEvidence(entry) {
  const box = $("chat-delivery-evidence");
  const receipt = entry.receipt;
  const pending = entry.pending;
  const matchingReceipt = !pending || receipt?.requestId === pending.requestId;
  const files = attachmentMetadata(matchingReceipt && Array.isArray(receipt?.attachments)
    ? receipt.attachments : pending?.attachmentDetails);
  const retained = pending || ["accepted", "queued", "completed", "interrupted", "unknown"].includes(receipt?.status) ||
    receipt?.status === "failed" && receipt.confirmed === true;
  const visible = retained && files.length > 0;
  box.hidden = !visible;
  if (!visible) { box.replaceChildren(); box.deliveryRevision = null; return false; }
  const labels = { accepted: "전송 접수", queued: "대기열 접수", completed: "요청 완료",
    interrupted: "접수 후 중단", unknown: "전송 결과 미확인", failed: "접수 후 오류" };
  const label = pending ? entry.sending ? "전송 중" : "전송 결과 미확인" : labels[receipt?.status];
  const record = receipt?.readbackConfirmed === true && !pending ? " · 세션 기록 확인" : "";
  const caption = `${label}${record} · 전송에 포함한 첨부 ${files.length}개`;
  const revision = JSON.stringify([pending?.requestId || receipt?.requestId, caption, files]);
  if (box.deliveryRevision === revision) return true;
  box.deliveryRevision = revision;
  box.replaceChildren(attachmentGroup(files, caption));
  return true;
}
const CHAT_SUBMISSIONS_MAX = 20;
const CHAT_SUBMISSIONS_BYTES = 256 * 1024;
function queueContext(entry, s = selectedSession()) {
  if (!s || chatKey(sourceOf(s)) !== chatKey(entry.source)) return false;
  const identity = conversationIdentity(s);
  if (entry.submissionIdentity !== identity || entry.submissionAuthEpoch !== state.authEpoch) {
    entry.submissions = [];
    entry.submissionIdentity = identity;
    entry.submissionAuthEpoch = state.authEpoch;
    entry.queueState = null;
    entry.queueKeys = new Set();
    entry.submissionsLimited = false;
  }
  return true;
}
function queueRecordKey(item) {
  return typeof item.requestId === "string" && item.requestId ? `request:${item.requestId}`
    : typeof item.queueId === "string" && item.queueId ? `queue:${item.queueId}` : null;
}
function limitSubmissions(entry) {
  let bytes = entry.submissions.reduce((total, item) => total + item.textBytes, 0);
  while (entry.submissions.length > CHAT_SUBMISSIONS_MAX || bytes > CHAT_SUBMISSIONS_BYTES) {
    let index = entry.submissions.findIndex((item) => !["queued", "sending", "unknown"].includes(item.status));
    if (index < 0) index = 0;
    bytes -= entry.submissions[index].textBytes;
    entry.submissions.splice(index, 1);
    entry.submissionsLimited = true;
  }
}
function queueRecords(entry) {
  return (entry.submissions || []).filter((item) => item.wasQueued || ["unknown", "sending"].includes(item.status));
}
function rememberSubmission(entry, pending) {
  if (!queueContext(entry)) return;
  entry.submissions.push({ key: `request:${pending.requestId}`, requestId: pending.requestId, queueId: null,
    text: pending.text, textTruncated: false, attachments: pending.attachmentDetails,
    contentAvailable: true,
    textBytes: new TextEncoder().encode(pending.text).length,
    submittedAt: Date.now() / 1000, status: "sending", confirmed: false, wasQueued: false, local: true,
    localQueuedAfterRead: false });
  limitSubmissions(entry);
}
function updateSubmissionReceipt(entry, receipt, fromSnapshot = false) {
  const item = (entry.submissions || []).find((record) => record.requestId === receipt?.requestId);
  if (!item) return;
  const confirmed = receipt.confirmed === true;
  const inCurrentQueue = entry.queueState?.available === true && entry.queueKeys?.has(item.key);
  const historicalActive = receipt.currentSnapshotConfirmed === false && ["queued", "accepted"].includes(receipt.status);
  item.confirmed = confirmed;
  item.status = historicalActive || receipt.status === "unknown" || !confirmed && receipt.status !== "failed" ? "unknown" : receipt.status;
  if (inCurrentQueue && ["queued", "unknown"].includes(receipt.status)) { item.status = "queued"; item.confirmed = true; }
  if (fromSnapshot && item.status === "queued" && !inCurrentQueue && receipt.currentSnapshotConfirmed !== true) item.status = "unknown";
  if (confirmed && receipt.status === "queued") item.wasQueued = true;
  if (receipt.dispatchState === "not_started") item.status = "failed";
  if (Array.isArray(receipt.attachments)) item.attachments = attachmentMetadata(receipt.attachments);
  if (receipt.queueId) item.queueId = receipt.queueId;
  if (item.status === "queued") {
    item.wasQueued = true;
    item.localQueuedAfterRead = !fromSnapshot;
  }
}
function syncQueuedMessages(entry, s, data) {
  if (!queueContext(entry, s) || chatKey(data.route || {}) !== chatKey(entry.source)) return;
  const supplied = Array.isArray(data.queuedMessages);
  const queue = supplied ? data.queuedMessages : [];
  const seen = new Set();
  if (supplied) {
    for (const item of entry.submissions) item.localQueuedAfterRead = false;
    entry.queueState = data.queueState && typeof data.queueState === "object" ? { ...data.queueState } : null;
    for (const raw of queue) {
      if (!raw || typeof raw !== "object") continue;
      const key = queueRecordKey(raw);
      if (!key) continue;
      let item = entry.submissions.find((record) => record.key === key || raw.requestId && record.requestId === raw.requestId ||
        raw.queueId && record.queueId === raw.queueId);
      if (!item) {
        item = { key, requestId: typeof raw.requestId === "string" ? raw.requestId : null,
          submittedAt: null, local: false };
        entry.submissions.push(item);
      }
      seen.add(item.key);
      Object.assign(item, { queueId: raw.queueId || null,
        text: item.local ? item.text : typeof raw.text === "string" ? raw.text : null,
        textTruncated: !item.local && raw.textTruncated === true,
        contentAvailable: item.local || raw.contentAvailable !== false && typeof raw.text === "string",
        attachments: attachmentMetadata(raw.attachments), status: "queued", confirmed: true,
        wasQueued: true, localQueuedAfterRead: false });
      item.textBytes = new TextEncoder().encode(item.text || "").length;
    }
    entry.queueKeys = seen;
    if (entry.queueState?.available === false || entry.queueState?.complete === true && !entry.queueState.truncated) {
      for (const item of entry.submissions) {
        if (item.status === "queued" && !seen.has(item.key)) item.status = "unknown";
        item.localQueuedAfterRead = false;
      }
    }
  }
  // Receipts carry exact request evidence; queue absence alone proves no turn state.
  for (const receipt of data.receipts || []) updateSubmissionReceipt(entry, receipt, true);
  limitSubmissions(entry);
}
function chatQueueCount(entry) {
  const queue = entry.queueState;
  const known = (entry.submissions || []).filter((item) => item.status === "queued" && item.confirmed);
  if (queue?.available === true && Number.isInteger(queue.total) && queue.total >= 0) {
    const added = known.filter((item) => item.localQueuedAfterRead && !entry.queueKeys?.has(item.key));
    return queue.total + added.length;
  }
  return known.length;
}
function submissionRequestIds(entry, lastRequestId) {
  const active = (entry.submissions || []).filter((item) => ["queued", "accepted", "unknown", "sending"].includes(item.status));
  return [...new Set([lastRequestId, ...active.map((item) => item.requestId).reverse()])]
    .filter((id) => typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/.test(id)).slice(0, 20);
}
function submissionStatus(item) {
  const labels = { queued: "대기 중", accepted: "실행 시작", completed: "완료",
    interrupted: "실행 중단", failed: item.confirmed ? "처리 오류" : "미접수",
    sending: "전송 중", unknown: item.wasQueued ? "접수 후 상태 미확인" : "전송 결과 미확인" };
  return labels[item.status] || "상태 미확인";
}
function closeChatQueue(restoreFocus = true) {
  const view = state.chatQueueView;
  state.chatQueueView = null;
  const dialog = $("chat-queue-dialog");
  if (dialog.open) dialog.close();
  $("chat-queue-list").replaceChildren();
  $("chat-queue-note").textContent = "";
  if (restoreFocus && view?.authEpoch === state.authEpoch && currentChat(view.entry) &&
      view.identity === conversationIdentity(selectedSession())) $("chat-queue-open").focus({ preventScroll: true });
}
function renderChatQueue(entry) {
  const view = state.chatQueueView;
  if (!view) return;
  if (view.entry !== entry) {
    if (!currentChat(view.entry) || view.identity !== conversationIdentity(selectedSession())) closeChatQueue(false);
    return;
  }
  if (view.authEpoch !== state.authEpoch || !currentChat(entry) || view.identity !== conversationIdentity(selectedSession())) {
    closeChatQueue(false); return;
  }
  const records = queueRecords(entry);
  const list = $("chat-queue-list");
  const desired = [];
  for (const item of records) {
    let shown = view.rows.get(item.key);
    if (!shown) {
      const row = el("article", "queued-message");
      const meta = el("div", "queued-message-meta");
      const status = el("strong", "queued-message-status");
      const timestamp = el("span", "queued-message-time");
      meta.append(status, timestamp);
      const body = el("div", "queued-message-body");
      const origin = el("p", "queued-message-origin");
      const attachments = el("div", "queued-message-files");
      const note = el("p", "queued-message-note");
      const copy = button("본문 복사", "secondary queued-message-copy", async () => {
        if (state.chatQueueView !== view || !currentChat(entry) || view.identity !== conversationIdentity(selectedSession())) return;
        const latest = entry.submissions.find((record) => record.key === item.key);
        if (latest?.contentAvailable === false || typeof latest?.text !== "string" || !latest.text) return;
        try { await navigator.clipboard.writeText(latest.text); toast("보낸 메시지 본문을 복사했습니다."); }
        catch { toast("클립보드에 접근할 수 없습니다. 본문을 길게 눌러 선택해 주세요."); }
      });
      row.append(meta, origin, body, attachments, note, copy);
      shown = { row, status, timestamp, origin, body, attachments, note, copy, attachmentRevision: null };
      view.rows.set(item.key, shown);
    }
    shown.status.textContent = submissionStatus(item);
    shown.timestamp.textContent = item.submittedAt ? `이 탭에서 보낸 시각 · ${clock(item.submittedAt)}` : "";
    shown.origin.textContent = item.local ? "이 탭에서 보낸 원문" : "원본 세션 대기열에서 확인한 내용";
    const hasText = item.contentAvailable !== false && typeof item.text === "string";
    const bodyText = hasText ? item.text || (item.attachments.length ? "첨부만 보낸 메시지" : "텍스트 본문이 없습니다.") : "메시지 내용을 확인하지 못했습니다.";
    if (shown.body.textContent !== bodyText) shown.body.textContent = bodyText;
    const attachmentRevision = JSON.stringify(item.attachments);
    if (shown.attachmentRevision !== attachmentRevision) {
      shown.attachments.replaceChildren(...(item.attachments.length ? [attachmentGroup(item.attachments, `전송에 포함한 첨부 ${item.attachments.length}개`)] : []));
      shown.attachmentRevision = attachmentRevision;
    }
    shown.note.textContent = item.textTruncated ? "본문이 길어 일부만 표시합니다. 전체 내용은 원본 세션에서 확인해 주세요." : "";
    shown.note.hidden = !item.textTruncated;
    shown.copy.disabled = !hasText || !item.text;
    shown.copy.hidden = shown.copy.disabled;
    shown.copy.setAttribute("aria-label", "표시된 메시지 본문 복사");
    desired.push(shown.row);
  }
  if (!desired.length) desired.push(el("p", "group-empty", "현재 표시할 메시지 내용을 확인하지 못했습니다."));
  for (const child of [...list.children]) if (!desired.includes(child)) list.removeChild(child);
  for (let index = 0; index < desired.length; index++)
    if (list.children[index] !== desired[index]) list.insertBefore(desired[index], list.children[index] || null);
  for (const [key, shown] of view.rows) if (!desired.includes(shown.row)) view.rows.delete(key);
  const partial = entry.submissionsLimited || entry.queueState?.truncated || entry.queueState?.complete === false;
  $("chat-queue-note").textContent = `${partial ? "표시 범위를 넘어 일부 내역은 보이지 않을 수 있습니다. " : ""}상태는 마지막 확인 기준입니다. 대기열 정보만으로 완료 여부를 알 수 없습니다.${entry.queueState?.available === false ? " 현재 대기열을 확인하지 못했습니다." : ""}${networkOffline() ? " 오프라인입니다." : ""}`;
}
function openChatQueue(entry) {
  if (!currentChat(entry) || !queueContext(entry)) return;
  if (entry.composing) { toast("한글 입력을 마친 뒤 예약 내용을 확인해 주세요."); return; }
  state.chatQueueView = { entry, identity: conversationIdentity(selectedSession()), authEpoch: state.authEpoch, rows: new Map() };
  renderChatQueue(entry);
  $("chat-queue-open").focus({ preventScroll: true });
  if (!$("chat-queue-dialog").open) $("chat-queue-dialog").showModal();
}
function updateChatQueue(entry) {
  if (!queueContext(entry)) return;
  const records = queueRecords(entry);
  const count = chatQueueCount(entry);
  const control = $("chat-queue-open");
  control.hidden = !records.length && !count;
  control.textContent = count ? `예약 ${count}개` : records.some((item) => item.status === "unknown") ? "미확인 내역"
    : records.some((item) => item.status === "sending") ? "전송 중" : "예약 내역";
  control.setAttribute("aria-label", `${count ? `예약 ${count}개` : "보낸 메시지 내역"} 내용 보기`);
  control.title = "보낸 메시지와 예약 내용 보기";
  renderChatQueue(entry);
}
function chatEntry(s) {
  const source = sourceOf(s);
  const key = chatKey(source);
  if (!state.chats.has(key)) state.chats.set(key, {
    source: Object.freeze({ ...source }), text: "", attachments: [], data: null, conversation: null,
    pending: null, receipt: null, sending: false, uploads: 0,
    composing: false, editorEpoch: 0, stateEpoch: 0, recheckQueued: false, expanded: false, inputCollapsed: false, checking: false, error: "", notice: "", revision: 0,
  });
  return state.chats.get(key);
}
function currentChat(entry) {
  const selected = selectedSession();
  return !!selected && chatEntry(selected) === entry && state.authenticated;
}
function networkOffline() {
  return state.offline || globalThis.navigator?.onLine === false;
}
function chatVisible() {
  return !document.hidden && !state.terminalVisible &&
    (!window.matchMedia?.("(max-width: 700px)").matches || document.body.classList.contains("show-detail"));
}
function chatSupported(entry) {
  return entry.data?.capability?.supported === true &&
    chatKey(entry.data.route || {}) === chatKey(entry.source);
}
function chatAttachmentsSupported(entry) {
  return !!entry.data && chatKey(entry.data.route || {}) === chatKey(entry.source) &&
    entry.data.phase !== "unavailable" && entry.data.capability?.attachments !== false;
}
function buildChatComposer(s) {
  const entry = chatEntry(s);
  // Detached editors can still emit Android IME events after a session switch.
  // Their final input belongs to that editor, never to a newly mounted draft.
  entry.composing = false;
  const editorEpoch = ++entry.editorEpoch;
  const editorCurrent = () => entry.editorEpoch === editorEpoch && currentChat(entry);
  const box = el("section", `chat-composer${entry.expanded ? "" : " is-compact"}`);
  box.id = "chat-composer";
  box.setAttribute("aria-label", "실제 세션에 메시지 보내기");
  box.dataset.source = chatKey(entry.source);
  const heading = el("div", "chat-heading");
  heading.hidden = !entry.expanded;
  heading.append(el("strong", null, "이 작업에 메시지 보내기"), el("span", "chat-route", `${s.hostLabel || s.host} · ${AGENTS[s.agent] || s.agent} · ${sessionAccountLabel(s)}`));
  const status = el("div", "chat-status");
  status.id = "chat-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const evidence = el("div", "chat-delivery-evidence");
  evidence.id = "chat-delivery-evidence";
  evidence.hidden = true;
  const attachmentsLabel = el("p", "chat-attachments-label");
  attachmentsLabel.id = "chat-attachments-label";
  attachmentsLabel.hidden = true;
  const attachments = el("div", "chat-attachments");
  attachments.id = "chat-attachments";
  const input = el("textarea", "chat-input");
  input.id = "chat-input";
  input.rows = entry.expanded ? 3 : 1;
  input.placeholder = "이 세션에 보낼 메시지…";
  input.setAttribute("aria-label", "메시지");
  input.value = entry.text;
  input.addEventListener("input", () => {
    if (!editorCurrent()) return;
    entry.inputCollapsed = false;
    entry.text = input.value;
    entry.notice = "";
    entry.revision++;
    updateChatComposer(entry);
  });
  input.addEventListener("compositionstart", () => {
    if (!editorCurrent()) return;
    entry.composing = true;
    updateChatComposer(entry);
  });
  input.addEventListener("compositionend", () => {
    if (!editorCurrent()) return;
    entry.composing = false;
    entry.inputCollapsed = false;
    entry.text = input.value;
    entry.revision++;
    updateChatComposer(entry);
  });
  input.addEventListener("keydown", (event) => {
    if (!editorCurrent()) return;
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    if (event.isComposing || event.keyCode === 229 || entry.composing) return;
    sendChat(entry);
  });
  input.addEventListener("paste", (event) => {
    if (editorCurrent()) pasteChat(entry, event, input);
  });
  const files = el("input");
  files.id = "chat-files";
  files.type = "file";
  files.multiple = true;
  files.hidden = true;
  files.addEventListener("change", () => {
    if (!editorCurrent()) return;
    uploadChatFiles(entry, [...(files.files || [])]);
    files.value = "";
  });
  const actions = el("div", "chat-toolbar");
  const tools = el("details", "chat-tools");
  tools.id = "chat-tools";
  const attach = button("첨부", "secondary", () => { if (editorCurrent()) files.click(); });
  attach.id = "chat-attach";
  attach.setAttribute("aria-label", "사진 또는 파일 첨부");
  const clipboard = button("클립보드 붙여넣기", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    readChatClipboard(entry, input, files, editorEpoch);
  });
  clipboard.id = "chat-clipboard";
  const check = button("상태 다시 확인", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    state.chatPollCount = 0;
    loadChatState(s, true);
  });
  check.id = "chat-check";
  const terminal = button("터미널·승인", "secondary", () => { if (editorCurrent()) openChatTerminal(s); });
  terminal.id = "chat-terminal";
  const native = button("이전한 작업 열기", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    openNativeChat(state.terminal);
  });
  native.id = "chat-native-open";
  native.hidden = true;
  const send = button("보내기", "primary", () => {
    if (!editorCurrent()) return;
    if (entry.composing) {
      toast("한글 입력을 마친 뒤 보내기를 눌러 주세요.");
      return;
    }
    entry.text = input.value;
    sendChat(entry);
  });
  send.id = "chat-send";
  const hint = el("p", "chat-hint", "Ctrl/⌘ + Enter로 전송 · 초안과 첨부 선택은 이 탭에서만 보관됩니다.");
  hint.id = "chat-hint";
  hint.hidden = !entry.expanded;
  const expand = button(entry.expanded ? "접기" : "펼치기", "chat-toggle", () => {
    if (!editorCurrent()) return;
    const collapse = entry.expanded || input.dataset.grown === "true";
    entry.expanded = !collapse;
    entry.inputCollapsed = collapse;
    box.classList.toggle?.("is-compact", !entry.expanded);
    input.rows = entry.expanded ? 3 : 1;
    heading.hidden = hint.hidden = !entry.expanded;
    expand.textContent = entry.expanded ? "접기" : "펼치기";
    expand.setAttribute("aria-expanded", String(entry.expanded));
    updateChatComposer(entry);
  });
  expand.id = "chat-expand";
  expand.setAttribute("aria-expanded", String(!!entry.expanded));
  expand.setAttribute("aria-controls", "chat-input");
  // Keep the focused textarea and its in-progress IME composition intact.
  expand.addEventListener("pointerdown", (event) => event.preventDefault());
  tools.append(el("summary", null, "도구"));
  const toolsActions = el("div", "chat-tools-actions");
  toolsActions.append(clipboard, check, native);
  for (const action of [clipboard, check, native])
    action.addEventListener("pointerdown", (event) => event.preventDefault());
  tools.append(toolsActions);
  const queue = button("예약 내용", "chat-queue-open", () => { if (editorCurrent()) openChatQueue(entry); });
  queue.id = "chat-queue-open";
  queue.hidden = true;
  queue.setAttribute("aria-haspopup", "dialog");
  queue.setAttribute("aria-controls", "chat-queue-dialog");
  queue.addEventListener("pointerdown", (event) => event.preventDefault());
  actions.append(attach, expand, terminal, queue, tools);
  const main = el("div", "chat-main");
  main.append(input, send);
  box.append(heading, status, evidence, attachmentsLabel, attachments, main, files, actions, hint);
  queueMicrotask(() => {
    if (!editorCurrent()) return;
    updateChatComposer(entry);
  });
  return box;
}
function resizeChatInput(entry) {
  if (!currentChat(entry) || $("chat-composer")?.dataset.source !== chatKey(entry.source)) return;
  const input = $("chat-input");
  const style = window.getComputedStyle?.(input);
  const line = parseFloat(style?.lineHeight) || 22;
  const padding = (parseFloat(style?.paddingTop) || 10) + (parseFloat(style?.paddingBottom) || 10);
  const border = (parseFloat(style?.borderTopWidth) || 1) + (parseFloat(style?.borderBottomWidth) || 1);
  const base = Math.max(44, line + padding + border);
  const viewport = window.visualViewport?.height || window.innerHeight || 800;
  const cap = entry.inputCollapsed ? base : Math.max(base,
    Math.min(line * (entry.expanded ? 11 : 6) + padding + border, viewport * 0.34, viewport - 200));
  const minimum = Math.min(cap, entry.expanded ? 3 * line + padding + border : base);
  const list = $("messages");
  const atBottom = list && list.scrollHeight - list.scrollTop - list.clientHeight < 50;
  const inputScroll = input.scrollTop || 0;
  input.style.minHeight = `${minimum}px`;
  input.style.maxHeight = `${cap}px`;
  input.style.height = "0px";
  const content = input.value ? input.scrollHeight + border || minimum : minimum;
  const height = Math.max(minimum, Math.min(cap, content));
  input.style.height = `${Math.ceil(height)}px`;
  input.style.overflowY = content > cap ? "auto" : "hidden";
  input.scrollTop = input.value ? inputScroll : 0;
  if (document.activeElement === input && input.selectionEnd === input.value.length && content > cap)
    input.scrollTop = input.scrollHeight;
  input.dataset.grown = String(height > base + 1);
  const toggle = $("chat-expand");
  toggle.textContent = height > base + 1 || entry.expanded ? "접기" : "펼치기";
  toggle.setAttribute("aria-expanded", String(height > base + 1 || entry.expanded));
  if (atBottom) list.scrollTop = list.scrollHeight;
}
function closeChatToolsOutside(event) {
  for (const id of ["chat-tools", "terminal-settings"]) {
    const tools = $(id);
    if (tools?.open && !tools.contains(event.target)) tools.open = false;
  }
}
function closeChatToolsEscape(event) {
  if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
  for (const id of ["chat-tools", "terminal-settings"]) {
    const tools = $(id);
    if (!tools?.open) continue;
    const restoreSummaryFocus = tools.contains(document.activeElement);
    tools.open = false;
    if (restoreSummaryFocus) tools.querySelector("summary")?.focus();
    event.preventDefault();
  }
}
function chatReceiptCopy(receipt) {
  if (receipt?.dispatchState === "not_started")
    return `${receipt.reason || "메시지 전송 조건을 확인해 주세요."} 메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.`;
  if (receipt?.status === "failed" && receipt.confirmed === true)
    return "요청 처리 중 오류가 발생했습니다. 대화를 확인해 주세요.";
  const labels = {
    accepted: "메시지를 접수했습니다. 작업 완료는 대화에서 확인해 주세요.",
    queued: "현재 작업 뒤에 보낼 메시지로 접수했습니다.",
    completed: "요청한 작업이 완료됐습니다.",
    interrupted: "접수했던 작업이 중단되었습니다. 대화를 확인한 뒤 새 메시지를 보내 주세요.",
    unknown: "전송 결과를 아직 확인하지 못했습니다. 중복 전송하지 않고 상태를 확인합니다.",
    failed: "메시지를 접수하지 못했습니다. 초안은 유지됩니다.",
  };
  return labels[receipt?.status] || "";
}
function updateChatComposer(entry) {
  if (!currentChat(entry) || $("chat-composer")?.dataset.source !== chatKey(entry.source)) return;
  const supported = chatSupported(entry);
  $("chat-composer").classList.toggle?.("is-busy", entry.data?.phase === "working" || entry.sending || !!entry.uploads);
  $("chat-composer").classList.toggle?.("is-error", !!entry.error || entry.receipt?.status === "failed");
  $("chat-composer").classList.toggle?.("is-unknown", !!entry.pending && !entry.sending);
  const offline = networkOffline();
  const blocked = offline || !supported || entry.sending || !!entry.pending || !!entry.uploads;
  $("chat-input").disabled = !supported || entry.sending || !!entry.pending;
  $("chat-attach").disabled = offline || !chatAttachmentsSupported(entry) || entry.sending || !!entry.pending || !!entry.uploads;
  $("chat-clipboard").disabled = $("chat-attach").disabled;
  $("chat-send").disabled = blocked || entry.composing || (!entry.text.trim() && !entry.attachments.length);
  $("chat-send").textContent = entry.sending ? "전송 중" : "보내기";
  $("chat-send").setAttribute("aria-label", entry.data?.phase === "working" ? "작업 대기열에 메시지 보내기" : "메시지 보내기");
  $("chat-check").disabled = offline || entry.checking;
  $("chat-terminal").disabled = state.launchBusy;
  const native = terminalInfo(state.terminal).nativeSource;
  $("chat-native-open").hidden = !native || chatKey(native) === chatKey(entry.source) || !state.terminalAlive;
  const phase = entry.data?.phase;
  const phaseCopy = phase === "working" ? "작업 중 · 보낸 메시지는 대기열에 접수됩니다." : phase === "needs_input" ? "답변·승인 필요 · 승인은 터미널에서 처리해 주세요." : entry.expanded ? "위의 실제 세션에 메시지를 전달합니다." : "";
  const budgetNote = state.chatPollCount >= 40 ? " 자동 상태 확인을 마쳤습니다. 상태 다시 확인을 눌러 주세요." : "";
  const unsupportedCopy = !supported && entry.attachments.length
    ? "첨부는 이 작업 폴더에 저장됐습니다. 첨부의 ‘경로’를 복사해 터미널에 붙여넣으세요."
    : entry.data?.capability?.reason || "직접 입력을 지원하지 않습니다. 터미널을 사용해 주세요.";
  $("chat-status").textContent = (offline ? "오프라인입니다. 초안은 유지됩니다. 연결 후 상태를 확인하고 보내 주세요." : entry.error || entry.notice || (entry.uploads ? `첨부 ${entry.uploads}개를 올리고 있습니다…` : !entry.data ? "메시지 전송 환경을 확인하고 있습니다…" : !supported ? unsupportedCopy : chatReceiptCopy(entry.receipt) || phaseCopy)) + budgetNote;
  updateChatQueue(entry);
  const deliveryVisible = updateChatDeliveryEvidence(entry);
  const chips = $("chat-attachments");
  chips.hidden = !!entry.pending && deliveryVisible;
  const attachmentsLabel = $("chat-attachments-label");
  attachmentsLabel.hidden = chips.hidden || !entry.attachments.length;
  const selectedCaption = entry.pending
    ? `전송 요청 첨부 ${entry.attachments.length}개 · 결과 미확인`
    : `전송 전 첨부 ${entry.attachments.length}개 · 업로드 완료`;
  attachmentsLabel.textContent = selectedCaption;
  chips.setAttribute("aria-label", selectedCaption);
  chips.replaceChildren();
  for (const file of entry.attachments) {
    const chip = el("div", "chat-attachment");
    chip.append(el("span", null, `${file.name || "첨부파일"} · ${formatBytes(file.size)}`));
    const remove = button("×", "chat-attachment-remove", () => {
      if (entry.sending || entry.pending) return;
      entry.attachments = entry.attachments.filter((item) => item.id !== file.id);
      entry.revision++;
      updateChatComposer(entry);
    });
    remove.setAttribute("aria-label", `${file.name || "첨부파일"} 제거`);
    remove.disabled = entry.sending || !!entry.pending;
    chip.append(remove);
    if (file.path) {
      const copy = button("경로", "chat-attachment-copy", async () => {
      try { await navigator.clipboard.writeText(file.path); toast("실제 세션의 첨부 경로를 복사했습니다. 같은 작업의 터미널에 붙여넣을 수 있습니다."); }
      catch { toast("클립보드에 접근할 수 없습니다."); }
      });
      copy.setAttribute("aria-label", `${file.name || "첨부파일"} 실제 경로 복사`);
      copy.title = "실제 첨부 경로 복사";
      chip.append(copy);
    }
    chips.append(chip);
  }
  resizeChatInput(entry);
}
function stopChatPolling() {
  clearTimeout(state.chatPollTimer);
  state.chatPollTimer = null;
  state.chatPollRun++;
  state.chatPollCount = 0;
}
function scheduleChatPoll(s, entry) {
  if (!currentChat(entry)) return;
  clearTimeout(state.chatPollTimer);
  const queuedWork = chatQueueCount(entry) > 0 || (entry.submissions || []).some((item) => item.wasQueued && item.status === "accepted" && item.confirmed);
  if (networkOffline() || !currentChat(entry) || !chatVisible() || state.chatPollCount >= 40 ||
      (!entry.pending && !queuedWork && !["working", "needs_input"].includes(entry.data?.phase))) return;
  const run = state.chatPollRun;
  state.chatPollTimer = setTimeout(() => {
    if (run !== state.chatPollRun || !currentChat(entry) || !chatVisible()) return;
    state.chatPollCount++;
    loadChatState(s, false);
  }, 3000);
}
function applyChatReceipt(entry, receipt, fromSnapshot = false) {
  updateSubmissionReceipt(entry, receipt, fromSnapshot);
  if (!receipt || (entry.pending && receipt.requestId !== entry.pending.requestId)) return;
  const fallback = entry.pending?.attachmentDetails ||
    (entry.receipt?.requestId === receipt.requestId ? entry.receipt.attachments : []);
  entry.receipt = { ...receipt, attachments: attachmentMetadata(Array.isArray(receipt.attachments) ? receipt.attachments : fallback) };
  if (!entry.pending || !["accepted", "queued", "completed", "interrupted", "failed"].includes(receipt.status)) return;
  if ((receipt.status !== "failed" || receipt.confirmed === true) && entry.revision === entry.pending.revision) {
    entry.text = "";
    entry.attachments = [];
    if (currentChat(entry)) {
      $("chat-input").value = "";
      resizeChatInput(entry);
    }
  }
  entry.pending = null;
}
async function loadChatState(s, manual = false, { join = false } = {}) {
  const entry = chatEntry(s);
  if (!currentChat(entry) || !chatVisible() || networkOffline()) return;
  const identity = conversationIdentity(s);
  if (entry.checking) {
    if (join) {
      if (entry.stateReadIdentity === identity && entry.stateReadAuthEpoch === state.authEpoch)
        return entry.stateReadPromise;
      // A different cwd/transcript is a new read, not a retry of the old view.
      await entry.stateReadPromise;
      return loadChatState(s, manual, { join: true });
    }
    if (manual) entry.recheckQueued = true;
    return;
  }
  entry.checking = true;
  let complete;
  entry.stateReadPromise = new Promise((resolve) => { complete = resolve; });
  entry.stateReadIdentity = identity;
  entry.stateReadAuthEpoch = state.authEpoch;
  let outcome = { status: "skipped" };
  const epoch = entry.stateEpoch;
  const pollRun = state.chatPollRun;
  let readFailed = false;
  if (manual) { state.chatPollCount = 0; entry.error = ""; }
  updateChatComposer(entry);
  try {
    const lastRequestId = entry.pending?.requestId ||
      (entry.receipt?.dispatchState === "not_started" || ["completed", "interrupted", "failed"].includes(entry.receipt?.status) ? null : entry.receipt?.requestId);
    const requestIds = submissionRequestIds(entry, lastRequestId);
    const data = await api("/api/chat/state", { method: "POST", body: {
      source: entry.source, ...(lastRequestId ? { requestId: lastRequestId } : {}),
      ...(requestIds.length ? { requestIds } : {}),
    } });
    if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
    if (epoch !== entry.stateEpoch || currentChat(entry) && conversationIdentity(selectedSession()) !== identity) return;
    entry.data = data;
    outcome = { status: "success", data };
    entry.error = "";
    syncQueuedMessages(entry, s, data);
    for (const receipt of data.receipts || []) {
      if (receipt.requestId === entry.pending?.requestId ||
          (entry.receipt?.dispatchState !== "not_started" && receipt.requestId === entry.receipt?.requestId))
        applyChatReceipt(entry, receipt, true);
    }
    if (chatReadSupported(entry, data)) {
      rememberConversation(s, data.messages, data);
      if (currentChat(entry)) {
        renderMessages(s, data.messages);
        conversationSyncNotice(s, data.stale ? "stale" : "");
      }
    }
    scheduleChatPoll(s, entry);
  } catch (err) {
    readFailed = true;
    outcome = { status: "failed", error: err };
    if (!err.unauthorized && !err.staleAuth && epoch === entry.stateEpoch &&
        currentChat(entry) && conversationIdentity(selectedSession()) === identity)
      conversationSyncNotice(s, "failed");
    if (!err.unauthorized && !err.staleAuth && epoch === entry.stateEpoch) entry.error = `${err.message} 상태 다시 확인을 눌러 주세요.`;
    if (currentChat(entry) && pollRun === state.chatPollRun) clearTimeout(state.chatPollTimer);
  } finally {
    entry.checking = false;
    complete(outcome);
    entry.stateReadPromise = null;
    updateChatComposer(entry);
    const recheck = entry.recheckQueued || epoch !== entry.stateEpoch;
    entry.recheckQueued = false;
    if (recheck && !readFailed && currentChat(entry) && chatVisible() && !networkOffline())
      queueMicrotask(() => loadChatState(s, false));
  }
  return outcome;
}
async function sendChat(entry) {
  if (networkOffline()) { updateChatComposer(entry); return; }
  if (!currentChat(entry) || !chatSupported(entry) || entry.composing || entry.sending || entry.pending || entry.uploads ||
      (!entry.text.trim() && !entry.attachments.length)) return;
  if (new TextEncoder().encode(entry.text).length > 32768 || entry.attachments.length > 8) {
    entry.error = "메시지는 32KiB, 첨부는 8개까지 보낼 수 있습니다. 초안은 유지됩니다.";
    updateChatComposer(entry);
    return;
  }
  // Keep the exact source and idempotency key even if selection changes during transport.
  const authEpoch = state.authEpoch;
  const pending = { requestId: requestId(), revision: entry.revision, source: entry.source,
    text: entry.text, attachments: entry.attachments.map((item) => item.id),
    attachmentDetails: attachmentMetadata(entry.attachments) };
  rememberSubmission(entry, pending);
  entry.pending = pending;
  entry.stateEpoch++;
  entry.sending = true;
  entry.error = "";
  entry.notice = "";
  updateChatComposer(entry);
  try {
    const result = await api("/api/chat/send", { method: "POST", body: {
      source: pending.source, requestId: pending.requestId, text: pending.text, attachments: pending.attachments,
    }, authEpoch });
    if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
    if (result.receipt?.requestId && result.receipt.requestId !== pending.requestId)
      throw new Error("요청 확인 번호가 일치하지 않습니다.");
    applyChatReceipt(entry, { ...result.receipt, requestId: pending.requestId, status: result.status || "unknown" });
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      if (err.dispatchState === "not_started") {
        applyChatReceipt(entry, { requestId: pending.requestId, status: "failed", confirmed: false,
          dispatchState: "not_started", reason: err.message });
        entry.error = `${err.message} 메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.`;
        if (err.code === "csrf_expired") {
          try {
            // Refresh credentials once; keep the current editor mounted and
            // require a new explicit send rather than replaying this request.
            await loadCapabilities(authEpoch, { refreshView: false });
            assertAuthEpoch(authEpoch);
            entry.receipt.reason = "접속 설정을 갱신했습니다. 내용을 확인한 뒤 보내기를 다시 눌러 주세요.";
            entry.error = "";
          } catch (refreshError) {
            if (authEpoch === state.authEpoch && !refreshError.unauthorized && !refreshError.staleAuth) {
              entry.receipt.reason = "접속 설정을 갱신하지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.";
              entry.error = chatReceiptCopy(entry.receipt);
            }
          }
        }
      } else {
        // HTTP status alone cannot establish whether the native runtime started.
        entry.receipt = { requestId: pending.requestId, status: "unknown", attachments: pending.attachmentDetails };
        updateSubmissionReceipt(entry, entry.receipt);
        entry.error = "전송 결과를 확인하지 못했습니다. 상태 다시 확인을 눌러 주세요. 중복 전송은 잠겼습니다.";
      }
    }
  } finally {
    entry.stateEpoch++;
    entry.sending = false;
    updateChatComposer(entry);
    const s = selectedSession();
    if (currentChat(entry) && s) { state.chatPollCount = 0; loadChatState(s, true); }
  }
}
async function uploadChatFiles(entry, files) {
  if (networkOffline()) { updateChatComposer(entry); return; }
  if (!currentChat(entry) || !chatAttachmentsSupported(entry) || entry.sending || entry.pending || entry.uploads || !files.length) return;
  if (entry.attachments.length + files.length > 8) {
    entry.error = "한 메시지에는 첨부 파일을 8개까지 추가할 수 있습니다.";
    updateChatComposer(entry);
    return;
  }
  const authEpoch = state.authEpoch;
  entry.uploads = files.length;
  entry.error = "";
  entry.notice = "";
  updateChatComposer(entry);
  for (const file of files) {
    if (authEpoch !== state.authEpoch) { entry.uploads = 0; return; }
    if (networkOffline()) {
      entry.notice = `아직 올리지 못한 파일 ${entry.uploads}개가 있습니다. 다시 첨부해 주세요.`;
      entry.uploads = 0; updateChatComposer(entry); break;
    }
    try {
      const limit = entry.data.maxAttachmentBytes || 20 * 1024 * 1024;
      if (file.size > limit) throw new Error(`${file.name || "파일"}: 최대 ${formatBytes(limit)}까지 첨부할 수 있습니다.`);
      const response = await fetch("/api/chat/upload", {
        method: "POST", credentials: "same-origin", cache: "no-store", body: file,
        headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": state.csrf,
          "X-Chat-Source": encodeURIComponent(JSON.stringify(entry.source)),
          "X-File-Name": encodeURIComponent(file.name || `clipboard-${Date.now()}.${file.type?.split("/")[1]?.replace("jpeg", "jpg") || "bin"}`) },
      });
      assertAuthEpoch(authEpoch);
      if (response.status === 401) { showLogin(); return; }
      const data = await response.json().catch(() => ({}));
      assertAuthEpoch(authEpoch);
      if (!response.ok || !data.attachment?.id) throw new Error(data.error || "첨부파일을 올리지 못했습니다.");
      if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
      entry.attachments.push(data.attachment);
      entry.revision++;
    } catch (err) {
      if (authEpoch !== state.authEpoch || err.staleAuth) { entry.uploads = 0; return; }
      entry.error = err.message;
      if (networkOffline()) {
        entry.notice = `아직 올리지 못한 파일 ${entry.uploads}개가 있습니다. 다시 첨부해 주세요.`;
        entry.error = `${err.message} ${entry.notice}`;
        break;
      }
    } finally { entry.uploads = Math.max(0, entry.uploads - 1); updateChatComposer(entry); }
  }
  entry.uploads = 0;
  updateChatComposer(entry);
}
function insertChatText(entry, input, text) {
  if (!text || !currentChat(entry) || input.disabled) return;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  input.setSelectionRange?.(start + text.length, start + text.length);
  entry.text = input.value;
  entry.inputCollapsed = false;
  entry.revision++;
  updateChatComposer(entry);
}
function pasteChat(entry, event, input) {
  const clipboard = event.clipboardData;
  const files = [...(clipboard?.files || [])];
  if (!files.length) {
    for (const item of clipboard?.items || []) {
      if (item.kind === "file") {
        const file = item.getAsFile?.();
        if (file) files.push(file);
      }
    }
  }
  if (!files.length) return; // Native text paste retains browser selection and IME behavior.
  event.preventDefault();
  if (!currentChat(entry)) return;
  if (entry.composing) {
    entry.notice = "한글 입력을 마친 뒤 사진·파일을 다시 붙여넣어 주세요.";
    updateChatComposer(entry);
    return;
  }
  // Text-only paste stays native. For mixed file/text pastes, preserve the text
  // even when the file cannot be uploaded yet, and explain the skipped file.
  insertChatText(entry, input, clipboard.getData("text/plain"));
  const blocked = networkOffline() ? "오프라인이라 파일을 추가하지 못했습니다. 연결 후 다시 붙여넣어 주세요."
    : entry.uploads ? "첨부를 올리고 있습니다. 완료된 뒤 파일을 다시 붙여넣어 주세요."
    : entry.sending || entry.pending ? "전송 결과를 확인한 뒤 파일을 다시 붙여넣어 주세요."
    : !chatAttachmentsSupported(entry) ? "이 세션은 파일 첨부를 지원하지 않습니다. 터미널에서 파일을 확인해 주세요."
    : "";
  if (blocked) {
    entry.notice = blocked;
    updateChatComposer(entry);
    return;
  }
  uploadChatFiles(entry, files);
}
async function readChatClipboard(entry, input, files, editorEpoch = entry.editorEpoch) {
  const editorCurrent = () => currentChat(entry) && entry.editorEpoch === editorEpoch;
  if (networkOffline()) { updateChatComposer(entry); return; }
  if (!editorCurrent() || !chatAttachmentsSupported(entry) || entry.sending || entry.pending || entry.uploads) return;
  if (entry.composing) {
    entry.notice = "한글 입력을 마친 뒤 붙여넣기를 눌러 주세요.";
    updateChatComposer(entry);
    return;
  }
  const revision = entry.revision;
  const stateEpoch = entry.stateEpoch;
  const inputValue = input.value;
  const selection = [input.selectionStart, input.selectionEnd];
  const canApply = () => {
    if (!editorCurrent()) return false;
    if (networkOffline() || entry.composing || entry.revision !== revision ||
        entry.stateEpoch !== stateEpoch || input.value !== inputValue ||
        input.selectionStart !== selection[0] || input.selectionEnd !== selection[1]) {
      entry.notice = "입력 중 내용이나 선택 위치가 바뀌어 클립보드를 붙이지 않았습니다. 입력을 마친 뒤 다시 눌러 주세요.";
      updateChatComposer(entry);
      return false;
    }
    return true;
  };
  let clipboardError;
  const richReadSupported = typeof navigator.clipboard?.read === "function";
  try {
    if (!richReadSupported) throw new Error("clipboard unavailable");
    const items = await navigator.clipboard.read();
    const attachments = [];
    const texts = [];
    for (const item of items) {
      if (item.types.includes("text/plain")) texts.push(await (await item.getType("text/plain")).text());
      const image = item.types.find((type) => type.startsWith("image/"));
      if (image) {
        const blob = await item.getType(image);
        attachments.push(new File([blob], `clipboard-${Date.now()}-${attachments.length}.${image.split("/")[1].replace("jpeg", "jpg")}`, { type: image }));
      }
    }
    if (!canApply()) return;
    if (!texts.length && !attachments.length) throw new Error("clipboard empty");
    entry.notice = "";
    if (texts.length && input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
    } else insertChatText(entry, input, texts.join("\n"));
    await uploadChatFiles(entry, attachments);
    if (texts.length && input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
    }
    return;
  } catch (err) { clipboardError = err; }
  if (!canApply()) return;
  // Some mobile browsers expose text reads while rich image reads are unavailable.
  // A single bounded fallback preserves the same editor/source and never opens a
  // delayed file picker after transient browser user activation has expired.
  try {
    if (typeof navigator.clipboard?.readText !== "function") throw new Error("clipboard unavailable");
    const text = await navigator.clipboard.readText();
    if (!canApply()) return;
    if (!text) throw new Error("clipboard empty");
    if (input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
      return;
    }
    insertChatText(entry, input, text);
    entry.notice = "클립보드 텍스트를 붙여넣었습니다. 사진은 첨부에서 선택할 수 있습니다.";
    updateChatComposer(entry);
    return;
  } catch (err) {
    if (["NotAllowedError", "SecurityError"].includes(err.name)) clipboardError = err;
  }
  {
    if (!editorCurrent()) return;
    const permissionDenied = ["NotAllowedError", "SecurityError"].includes(clipboardError?.name);
    entry.notice = `${permissionDenied ? "브라우저가 클립보드 읽기를 허용하지 않았습니다." : !richReadSupported ? "이 브라우저는 클립보드 버튼 붙여넣기를 지원하지 않습니다." : "이 클립보드 내용을 버튼으로 읽을 수 없습니다."} 입력칸을 길게 눌러 붙여넣거나 첨부에서 사진·파일을 선택해 주세요.`;
    updateChatComposer(entry);
  }
}
function openChatTerminal(s) {
  if (state.selected !== s.key || state.launchBusy) return;
  if (state.terminalAlive && chatKey(terminalInfo(state.terminal).nativeSource || {}) === chatKey(sourceOf(s))) {
    openTerminal(state.terminal);
    return;
  }
  if (s.nativeSource) { toast("이 실행의 터미널이 종료됐습니다. 새로고침 후 실제 작업 목록에서 선택해 주세요."); return; }
  state.route = { host: s.host, agent: s.agent, account: accountOf(s) };
  populateRoute(s);
  preparePlan();
}
function openNativeChat(terminal) {
  const info = terminalInfo(terminal);
  const source = info.nativeSource;
  if (!source?.host || !source.agent || !source.id || !state.authenticated) return;
  const key = [source.host, source.agent, source.home || "", source.id].join("|");
  // This identity comes only from the server-owned launched terminal metadata.
  // The user explicitly selects it; the original source never changes underneath a draft.
  if (!allSessions().some((s) => s.key === key)) {
    state.nativeSessions.set(key, { ...source, key, nativeSource: true,
      title: info.title || "이어가는 작업", hostLabel: hostLabel(source.host),
      account: sessionAccountLabel({ ...source, account: info.account }),
      phase: "idle", project: "이어가는 실제 세션", updatedAt: Date.now() / 1000 });
  }
  if (state.terminalVisible) returnToBoard({ selectLaunchedSource: false });
  select(key);
}
function renderMessages(s, messages, error = "", fresh = false) {
  const list = $("messages");
  if (!list) return;
  const source = chatKey(sourceOf(s));
  const previous = list.conversationSource === source ? list.conversationRows || [] : [];
  // A failed refresh never removes a conversation that was already readable.
  if (messages == null || (error && !messages.length && previous.length))
    messages = previous.map((item) => item.message);
  const selectedText = window.getSelection?.();
  const readingSelection = selectedText && !selectedText.isCollapsed &&
    (list.contains?.(selectedText.anchorNode) || list.contains?.(selectedText.focusNode));
  const stick = !readingSelection &&
    (fresh || list.scrollHeight - list.scrollTop - list.clientHeight < 50);
  const scroll = list.scrollTop;
  const oldWarningHeight = list.conversationError && [...list.children].includes(list.conversationError)
    ? list.conversationError.offsetHeight || 0 : 0;
  const available = new Map();
  for (const item of previous) {
    if (!available.has(item.key)) available.set(item.key, []);
    available.get(item.key).push(item);
  }
  const rows = messages.map((message) => {
    const attachments = attachmentMetadata(message.attachments);
    const key = JSON.stringify([message.role, message.text, message.ts || null, attachments]);
    const retained = available.get(key)?.shift();
    if (retained) return retained;
    const role = ["user", "assistant", "tool", "system"].includes(message.role) ? message.role : "system";
    const row = el("article", `msg ${role}`);
    if (role === "tool") {
      const more = el("details");
      more.append(el("summary", null, "도구 실행 기록"), el("div", "bubble", message.text));
      row.append(more);
    } else {
      if (role !== "system") {
        const label = el("div", "msg-label");
        label.append(el("strong", null, role === "user" ? "나" : AGENTS[s.agent] || "에이전트"));
        if (message.ts) label.append(el("span", null, clock(message.ts)));
        row.append(label);
      }
      const bubble = el("div", "bubble");
      if (role === "assistant" && typeof MessageFormat !== "undefined") {
        MessageFormat.render(bubble, message.text || "", {
          onCopy: async (text, kind) => {
            try {
              await navigator.clipboard.writeText(text);
              toast(kind === "code" ? "코드를 복사했습니다." : "인용문을 복사했습니다.");
            } catch { toast("클립보드에 접근할 수 없습니다. 본문을 길게 눌러 선택해 주세요."); }
          },
        });
      } else {
        const text = message.text || "";
        if (role === "user" && attachments.length) {
          if (text) bubble.append(el("span", "msg-body", text));
          bubble.append(attachmentGroup(attachments));
        } else bubble.textContent = text;
      }
      row.append(bubble);
    }
    const copy = button("복사", "msg-copy", async () => {
      const text = role === "assistant" && typeof MessageFormat !== "undefined"
        ? MessageFormat.plainText(message.text || "") : message.text || "";
      try { await navigator.clipboard.writeText(text); toast("메시지 본문을 복사했습니다."); }
      catch { toast("클립보드에 접근할 수 없습니다. 본문을 길게 눌러 선택해 주세요."); }
    });
    copy.setAttribute("aria-label", "메시지 본문 복사");
    if (role === "user" && attachments.length && !message.text) {
      copy.disabled = true;
      copy.hidden = true;
      copy.setAttribute("aria-label", "복사할 텍스트 본문이 없습니다");
    }
    row.append(copy);
    return { key, message, row };
  });
  const desired = rows.map((item) => item.row);
  if (error) {
    if (!list.conversationError || list.conversationErrorText !== error || list.conversationSource !== source) {
      const warning = el("div", "conversation-read-error");
      warning.setAttribute("role", "status");
      warning.append(el("p", "detail-error", error), button("대화 다시 불러오기", "secondary", () => {
        const current = selectedSession();
        if (current?.key === s.key) loadDetail(current, !list.conversationRows?.length);
      }));
      list.conversationError = warning;
      list.conversationErrorText = error;
    }
    desired.unshift(list.conversationError);
  } else {
    list.conversationError = null;
    list.conversationErrorText = "";
    if (!rows.length) {
      list.conversationEmpty ||= el("p", "group-empty", "저장된 대화를 아직 찾지 못했습니다.");
      desired.push(list.conversationEmpty);
    }
  }
  // Keep unchanged text nodes mounted: mobile selections and expanded tool
  // records survive reads, including new messages appended during a turn.
  for (const child of [...list.children]) {
    if (!desired.includes(child)) list.removeChild(child);
  }
  for (let index = 0; index < desired.length; index++) {
    if (list.children[index] !== desired[index])
      list.insertBefore(desired[index], list.children[index] || null);
  }
  list.conversationSource = source;
  list.conversationRows = rows;
  const warningHeight = error ? list.conversationError.offsetHeight || 0 : 0;
  list.scrollTop = stick ? list.scrollHeight : scroll + warningHeight - oldWarningHeight;
}

function updateReadNotice(data) {
  const pane = $("detail");
  let note = pane.querySelector(".stale-notice");
  if (!data.stale) return;
  if (!note) {
    note = el("div", "stale-notice");
    pane.querySelector(".detail-head").after(note);
  }
  note.textContent = `기기에 연결되지 않아 ${data.fetchedAt ? `${ago(data.fetchedAt)} 저장한` : "저장된"} 대화를 표시합니다. 실행 상태는 연결 후 확인해 주세요.`;
}

function navigateView(view, replace = false) {
  const current = window.history.state;
  if (!replace && current?.sessionholic && current.view === view) return;
  window.history[replace ? "replaceState" : "pushState"](
    { sessionholic: true, view },
    "",
  );
}
function backToList() {
  closeChatQueue(false);
  stopChatPolling();
  if (
    window.history.state?.sessionholic &&
    window.history.state.view === "detail"
  )
    window.history.back();
  else { document.body.classList.remove("show-detail"); restoreSelectedCardFocus(); }
}
function backFromTerminal() {
  if (
    window.history.state?.sessionholic &&
    window.history.state.view === "terminal"
  )
    window.history.back();
  else returnToBoard();
}

function capsHost(name) {
  return state.capabilities?.hosts?.find((h) => h.name === name);
}
function capsAgent(host, agent) {
  return capsHost(host)?.agents?.find((a) => a.id === agent);
}
function accountDisplay(profile, fallback) {
  if (profile?.label) return profile.label;
  if (profile?.scopeLabel && profile?.accountLabel) return `${profile.scopeLabel} · ${profile.accountLabel}`;
  if (profile?.accountLabel) return profile.accountLabel;
  if (typeof fallback === "string" && fallback && !/^(?:\.codex|codex:|\.claude)/.test(fallback)) return fallback;
  return "현재 계정 설정";
}
function sessionAccountLabel(s) {
  const profile = capsAgent(s.host, s.agent)?.accounts?.find((account) => account.id === accountOf(s));
  return accountDisplay(profile, s.account);
}
function accountRouteKey(s) {
  return JSON.stringify([s.host, s.agent, accountOf(s)]);
}
function targetLabel(route) {
  if (!route) return "";
  const host = capsHost(route.host);
  const agent = capsAgent(route.host, route.agent);
  const account = agent?.accounts?.find((a) => a.id === route.account);
  return [
    host?.label || route.host,
    agent?.label || AGENTS[route.agent] || route.agent,
    accountDisplay(account, route.account),
  ]
    .filter(Boolean)
    .join(" · ");
}
function hostLabel(name) {
  return capsHost(name)?.label || name || "대상 기기";
}
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes}B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    value /= 1024;
    unit = next;
    if (value < 1024 || next === "TB") break;
  }
  return `${value.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}${unit}`;
}
function buildContinuePanel(s) {
  const disclosure = el("details", "continue-disclosure");
  disclosure.append(el("summary", null, "다른 기기·계정에서 이어가기"));
  const panel = el("section", "continue-panel");
  panel.setAttribute("aria-label", "이어서 사용할 환경 선택");
  const heading = el("div", "continue-heading");
  heading.append(
    el("h2", null, "어디서 이어갈까요?"),
    el("span", null, "기기와 실행 환경을 변경합니다"),
  );
  const form = el("div", "route-form");
  for (const [id, label] of [
    ["route-host", "기기"],
    ["route-agent", "에이전트"],
    ["route-account", "계정 설정"],
  ]) {
    const field = el("label", "route-field");
    field.append(el("span", null, label));
    const select = el("select");
    select.id = id;
    field.append(select);
    form.append(field);
  }
  const actions = el("div", "continue-actions");
  const note = el("p", "continue-note");
  note.id = "continue-note";
  const action = button("터미널 열기 ↗", "primary", () => preparePlan());
  action.id = "continue-open";
  actions.append(note, action);
  panel.append(heading, form, actions);
  const accountContext = el("p", "account-context", "실행 환경과 프로필은 설치한 서버의 설정을 따릅니다. 같은 이름도 로그인은 별도로 관리됩니다.");
  accountContext.id = "account-context";
  panel.append(accountContext);
  if (s.resume) {
    const fallback = el("details", "resume-fallback");
    fallback.append(el("summary", null, "기존 터미널에서 직접 열기"));
    const row = el("div");
    row.append(
      el("code", null, s.resume),
      button("명령 복사", "copy", async () => {
        try {
          await navigator.clipboard.writeText(s.resume);
          toast("재개 명령을 복사했습니다.");
        } catch {
          toast("클립보드에 접근할 수 없습니다.");
        }
      }),
    );
    fallback.append(row);
    panel.append(fallback);
  }
  queueMicrotask(() => populateRoute(s));
  disclosure.append(panel);
  return disclosure;
}
function populateRoute(s) {
  if (!$("route-host") || state.selected !== s.key) return;
  const hosts = state.capabilities?.hosts || [];
  const preserve = !!state.route;
  state.route = state.route || {
    host: s.host,
    agent: s.agent,
    account: accountOf(s),
  };
  const hostSelect = $("route-host");
  hostSelect.replaceChildren();
  for (const host of hosts) {
    const option = new Option(
      `${host.label}${host.online ? "" : " · 연결 확인 필요"}`,
      host.name,
    );
    hostSelect.append(option);
  }
  if (!hosts.some((h) => h.name === state.route.host)) {
    hostSelect.append(
      new Option(s.hostLabel || state.route.host, state.route.host),
    );
  }
  hostSelect.value = state.route.host;
  hostSelect.onchange = () => {
    state.route = {
      host: hostSelect.value,
      agent: state.route.agent,
      account: state.route.account,
    };
    populateAgent(s, false);
  };
  populateAgent(s, preserve);
}
function populateAgent(s, preserve = false) {
  const host = capsHost(state.route.host);
  const agents = host?.agents || [];
  const select = $("route-agent");
  select.replaceChildren();
  for (const agent of agents)
    select.append(
      new Option(agent.label || AGENTS[agent.id] || agent.id, agent.id),
    );
  if (!agents.some((a) => a.id === state.route.agent) && !preserve)
    state.route.agent = agents[0]?.id || s.agent;
  if (!agents.some((a) => a.id === state.route.agent))
    select.append(
      new Option(
        `${AGENTS[state.route.agent] || state.route.agent}${preserve ? " · 확인 필요" : ""}`,
        state.route.agent,
      ),
    );
  select.value = state.route.agent;
  select.onchange = () => {
    state.route.agent = select.value;
    state.route.account = "";
    populateAccount(s);
  };
  populateAccount(s, preserve);
}
function populateAccount(s, preserve = false) {
  const accounts =
    capsAgent(state.route.host, state.route.agent)?.accounts || [];
  const select = $("route-account");
  select.replaceChildren();
  const crossHost = state.route.host !== s.host;
  const current = accounts.find(
    (a) => a.id === state.route.account && (preserve || a.available !== false),
  );
  const sourceAccount = accounts.find(
    (a) => a.id === accountOf(s) && a.available !== false,
  );
  const retainedMissing = preserve && !!state.route.account && !current;
  const match = current || (!retainedMissing ? sourceAccount : null);
  const needsChoice = crossHost && !match && !retainedMissing && accounts.length > 0;
  if (match) state.route.account = match.id;
  if (needsChoice)
    select.append(new Option("대상 계정을 선택하세요", ""));
  const groups = new Map();
  const grouped = accounts.some((account) => account.scope || account.scopeLabel);
  for (const account of accounts) {
    const actualSource = state.route.host === s.host && state.route.agent === s.agent && account.id === accountOf(s);
    const option = new Option(
      `${accountDisplay(account)}${actualSource ? " · 현재 사용 중" : ""}${account.available === false ? " · 사용 불가" : ""}`,
      account.id,
    );
    option.disabled = account.available === false;
    if (!grouped) select.append(option);
    else {
      const scope = account.scope || account.scopeLabel || "other";
      if (!groups.has(scope)) {
        const group = el("optgroup");
        group.label = account.scopeLabel || "다른 실행 환경";
        groups.set(scope, group);
        select.append(group);
      }
      groups.get(scope).append(option);
    }
  }
  if (retainedMissing)
    select.append(new Option(`${state.route.account} · 확인 필요`, state.route.account));
  if (!match && !retainedMissing)
    state.route.account = needsChoice
      ? ""
      : accounts.find((a) => a.available !== false)?.id || "";
  if (!accounts.length && !retainedMissing) select.append(new Option("사용할 계정 없음", ""));
  select.value = state.route.account;
  select.onchange = () => {
    state.route.account = select.value;
    updateContinue(s);
  };
  updateContinue(s);
}
function updateContinue(s) {
  const crossHost = state.route.host !== s.host;
  const same =
    state.route.host === s.host &&
    state.route.agent === s.agent &&
    state.route.account === accountOf(s);
  const account = capsAgent(
    state.route.host,
    state.route.agent,
  )?.accounts?.find((a) => a.id === state.route.account);
  const accountContext = $("account-context");
  if (accountContext) accountContext.textContent = `${account?.scopeDescription || "실행 환경과 프로필은 설치한 서버의 설정을 따릅니다."} 같은 이름도 로그인은 별도로 관리됩니다.`;
  const hasAvailableAccounts = !!capsAgent(
    state.route.host,
    state.route.agent,
  )?.accounts?.some((a) => a.available !== false);
  const terminal = state.capabilities?.terminal;
  const offline = capsHost(s.host)?.online === false || capsHost(state.route.host)?.online === false;
  const ready = !!(
    state.csrf &&
    state.route.account &&
    account && account.available !== false &&
    terminal?.available && !offline
  );
  $("continue-open").disabled = !ready || state.launchBusy;
  $("continue-open").textContent = crossHost
    ? "이 기기로 넘기기 ↗"
    : same
      ? "터미널 열기 ↗"
      : "이 설정으로 이어가기 ↗";
  $("continue-note").textContent = !state.capabilities
    ? "연결 환경을 확인하고 있습니다."
    : offline
      ? "출발·대상 기기 연결을 확인해 주세요. 열어 둔 터미널은 열린 목록에서 확인할 수 있습니다."
    : !terminal?.available
      ? terminal?.reason || "터미널 실행 환경이 아직 준비되지 않았습니다."
      : !state.route.account
        ? crossHost && hasAvailableAccounts
          ? "대상 기기의 계정을 직접 선택해 주세요."
          : "사용 가능한 계정이 없습니다."
        : !account
          ? "선택한 계정을 확인하지 못했습니다. 선택은 유지됩니다. 연결 후 다시 확인해 주세요."
        : account.available === false
          ? account.reason || "이 계정을 사용할 수 없습니다."
          : crossHost
            ? `실제 실행 위치가 ${hostLabel(state.route.host)}로 바뀝니다. 코드·미커밋 변경과 최근 대화가 새 작업 폴더로 옮겨집니다.`
            : same
              ? "현재 환경으로 연결합니다."
              : "작업 맥락을 전달할 수 있는지 먼저 확인합니다.";
}

async function preparePlan() {
  const s = selectedSession();
  if (!s || !state.route) return;
  if (state.launchBusy || $("plan-dialog").open) {
    $(state.launchBusy ? "plan-dialog" : "plan-cancel").focus();
    return;
  }
  const target = { ...state.route };
  const run = ++state.planRun;
  state.plan = null;
  $("plan-title").textContent = "이어서 할 준비를 확인합니다";
  $("plan-error").textContent = "";
  $("plan-body").replaceChildren(
    progress("대화와 실행 환경을 확인하고 있습니다…"),
  );
  $("plan-launch").disabled = true;
  $("plan-launch").textContent = "터미널 열기 ↗";
  $("plan-close").disabled = false;
  $("plan-cancel").disabled = false;
  $("plan-dialog").showModal();
  try {
    const plan = await api("/api/plan", {
      method: "POST",
      body: { source: sourceOf(s), target },
    });
    if (run !== state.planRun || !$("plan-dialog").open) return;
    state.plan = plan;
    renderPlan(s, plan, target);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth && run === state.planRun && $("plan-dialog").open) {
      $("plan-title").textContent = "준비를 완료하지 못했습니다";
      $("plan-body").replaceChildren();
      $("plan-error").textContent = err.message;
    }
  }
}
function progress(text) {
  const node = el("div", "plan-progress");
  node.append(el("span", "spinner"), el("span", null, text));
  return node;
}
function renderPlan(s, plan, target) {
  $("plan-title").textContent = plan.allowed
    ? plan.mode === "transfer"
      ? "새 작업 폴더로 옮길까요?"
      : plan.mode === "reconnect"
      ? "열어 둔 터미널로 돌아갑니다"
      : plan.mode === "attach"
        ? "이 작업으로 돌아갑니다"
        : "작업 맥락을 이어갑니다"
    : "지금은 전환할 수 없습니다";
  const body = $("plan-body");
  body.replaceChildren();
  const route = el("div", "plan-route");
  const transfer = plan.mode === "transfer" ? plan.transfer || {} : null;
  if (transfer) {
    const workspace = transfer.workspace || {};
    route.append(
      el("span", null, "출발 · 실제 작업 위치"),
      el(
        "strong",
        null,
        [transfer.sourceHostLabel || s.hostLabel,
          workspace.cwdRelative && workspace.cwdRelative !== "."
            ? workspace.cwdRelative
            : workspace.projectName || workspace.root]
          .filter(Boolean)
          .join(" · "),
      ),
      el("span", "plan-route-arrow", "↓"),
      el("span", null, "도착 · 새 작업 폴더"),
      el(
        "strong",
        null,
        targetLabel(target),
      ),
    );
    body.append(route);
    body.append(
      el(
        "p",
        "plan-summary",
        transfer.requiresInterrupt
          ? "진행 중인 응답을 중단하고, 코드·미커밋 변경·최근 대화를 새 작업 폴더로 옮깁니다. 원본 파일·기록은 남습니다."
          : "진행 중인 응답은 없습니다. 코드·미커밋 변경·최근 대화를 새 작업 폴더로 옮깁니다. 원본 파일·기록은 남습니다.",
      ),
    );
    const metrics = [];
    if (Number.isFinite(workspace.fileCount))
      metrics.push(`${workspace.fileCount.toLocaleString("ko-KR")}개 파일`);
    const size = formatBytes(workspace.bytes);
    if (size) metrics.push(size);
    const sourceDetails = [
      workspace.git === true ? "Git 작업 폴더" : "",
      typeof workspace.git === "string" && workspace.git
        ? `Git ${workspace.git}`
        : "",
      workspace.branch ? `브랜치 ${workspace.branch}` : "",
      workspace.head ? `기준 ${String(workspace.head).slice(0, 8)}` : "",
    ].filter(Boolean);
    if (metrics.length || sourceDetails.length)
      body.append(
        el("p", "plan-preserve", [...metrics, ...sourceDetails].join(" · ")),
      );
    const excluded = Array.isArray(workspace.excluded)
      ? workspace.excluded.map((item) => typeof item === "string" ? item : item?.path || item?.name).filter(Boolean)
      : [];
    if (excluded.length)
      body.append(
        el("p", "plan-preserve", `제외 항목: ${excluded.join(" · ")}`),
      );
    body.append(
      el(
        "p",
        "plan-preserve",
        `대상 기기에 새 폴더를 만듭니다. 원본 폴더를 덮어쓰지 않으며, 왕복 인계도 매번 새 폴더를 사용합니다.${transfer.destinationNote ? ` ${transfer.destinationNote}` : ""}`,
      ),
    );
  } else {
    route.append(
      el("span", null, "현재 작업"),
      el(
        "strong",
        null,
        [s.hostLabel, AGENTS[s.agent], s.account].filter(Boolean).join(" · "),
      ),
      el("span", "plan-route-arrow", "↓"),
      el("span", null, "이어서 사용할 환경"),
      el("strong", null, targetLabel(target)),
    );
    body.append(route);
  }
  if (plan.summary && !transfer) body.append(el("p", "plan-summary", plan.summary));
  if (!plan.allowed && plan.reason) body.append(el("p", "error", plan.reason));
  const warningItems = [
    ...(plan.warnings || []),
    ...(transfer?.workspace?.warnings || []),
  ];
  if (warningItems.length) {
    const warningList = el("ul", "plan-warnings");
    for (const warning of [...new Set([
      ...(plan.warnings || []),
      ...(transfer?.workspace?.warnings || []),
    ])])
      warningList.append(el("li", null, String(warning)));
    body.append(warningList);
  }
  if (!transfer)
    body.append(
      el(
        "p",
        "plan-preserve",
        plan.mode !== "handoff"
          ? "새 화면을 열어도 기존 작업 기록은 유지됩니다."
          : "원본 대화는 그대로 두고, 대상 환경에 전달할 맥락을 준비합니다.",
      ),
    );
  $("plan-launch").disabled = !plan.allowed;
  $("plan-launch").textContent =
    plan.mode === "transfer"
      ? `${transfer.requiresInterrupt ? "중단하고 " : ""}${transfer.targetHostLabel || hostLabel(target.host)}로 넘기기`
      : plan.mode === "reconnect"
      ? "열어 둔 터미널로 ↗"
      : plan.mode === "attach"
        ? "터미널 열기 ↗"
        : "맥락 전달 후 열기 ↗";
}
function closePlan() {
  if (state.launchBusy) return;
  state.planRun++;
  state.plan = null;
  $("plan-dialog").close();
}
async function launchPlan() {
  if (!state.plan?.allowed || state.launchBusy) return;
  const plan = state.plan;
  const authEpoch = state.authEpoch;
  state.launchBusy = true;
  $("plan-launch").disabled = true;
  $("plan-close").disabled = true;
  $("plan-cancel").disabled = true;
  $("plan-error").textContent = "";
  $("plan-body").append(
    progress(
      plan.mode === "transfer"
        ? plan.transfer?.requiresInterrupt
          ? "응답 중단·파일 복사·대상 실행 준비 중…"
          : "파일 복사·대상 실행 준비 중…"
        : plan.mode !== "handoff"
        ? "터미널에 연결하고 있습니다…"
        : "작업 맥락을 전달하고 터미널을 준비하고 있습니다…",
    ),
  );
  try {
    const result = await api("/api/launch", {
      method: "POST",
      body: { planId: plan.id, requestId: requestId() }, authEpoch,
    });
    assertAuthEpoch(authEpoch);
    if (!result.terminal?.id)
      throw new Error("터미널 연결 정보를 받지 못했습니다.");
    $("plan-dialog").close();
    state.plan = null;
    await openTerminal(result.terminal);
    assertAuthEpoch(authEpoch);
    if (plan.mode === "transfer" || result.mode === "transfer")
      toast(
        `실제 실행 위치가 ${plan.transfer?.targetHostLabel || hostLabel(plan.target?.host || "")}로 바뀌었습니다. 새 작업 폴더에서 이어가세요.`,
      );
    else if (result.reused)
      toast(
        "열어 둔 터미널에 다시 연결했습니다. 새 요청은 터미널에서 이어가세요.",
      );
    refreshTerminals(authEpoch);
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      if (err.recovery) {
        const recovery = err.recovery;
        const location = hostLabel(recovery.host || plan.target?.host);
        const started = recovery.targetStarted === true ? "대상 작업이 이미 시작됐습니다." : "대상 작업이 이미 시작됐을 수 있습니다.";
        $("plan-error").textContent = `${err.message} ${location}: ${started} 다시 실행하지 말고 보드 목록을 새로고침해 대상 작업을 확인해 주세요.`;
        state.plan = null;
        if (recovery.terminal?.id) {
          const terminal = { ...recovery.terminal };
          terminal.host ||= recovery.host;
          if (!terminalInfo(terminal).nativeSource && recovery.nativeSource) terminal.nativeSource = { ...recovery.nativeSource };
          const existing = state.terminals.findIndex((item) => item.id === terminal.id);
          if (existing < 0) state.terminals.push(terminal);
          else state.terminals[existing] = { ...state.terminals[existing], ...terminal };
          renderTerminals();
        }
      } else $("plan-error").textContent = `${err.message} 원본 작업은 목록에서 다시 확인할 수 있습니다.`;
      $("plan-body").querySelector(".plan-progress")?.remove();
      $("plan-launch").disabled = true;
      $("plan-launch").textContent = "다시 검토해 주세요";
    }
  } finally {
    if (authEpoch === state.authEpoch) {
      state.launchBusy = false;
      $("plan-close").disabled = false;
      $("plan-cancel").disabled = false;
      const s = selectedSession();
      if (s && $("continue-open")) updateContinue(s);
      if (s) updateChatComposer(chatEntry(s));
    }
  }
}

function terminalStatus(text, connected = false) {
  state.connected = connected;
  $("terminal-status").textContent = text;
  $("terminal-status").dataset.connected = String(connected);
  for (const node of $("terminal-keys").querySelectorAll("button"))
    node.disabled = !connected || !!state.draftPaste;
  $("draft-paste").disabled = !connected || !!state.draftPaste;
  $("terminal-reconnect").hidden = connected || !state.terminalAlive;
}
function terminalInfo(terminal) {
  return { ...terminal?.metadata, ...terminal };
}
function terminalCloseCopy(terminal) {
  const mode = terminal?.mode || terminal?.metadata?.mode;
  if (mode === "attach") return {
    action: "연결 닫기", title: "이 보드의 연결을 닫을까요?", danger: false,
    description: "이 보드의 연결만 닫습니다. 기존 작업에 중단 명령을 보내지 않습니다. 작업 상태는 실행 기기에서 확인하세요.",
    context: "기존 작업에 연결했습니다. 보드로 돌아가거나 브라우저를 닫아도 작업은 유지됩니다.",
    ended: "이 보드의 연결이 종료되었습니다. 기존 작업 상태는 실행 기기에서 확인하세요.",
    done: "연결을 닫았습니다. 기존 작업에 중단 명령을 보내지 않았습니다.",
  };
  if (mode === "handoff" || mode === "transfer") return {
    action: "실행 종료", title: "이 터미널의 실행을 종료할까요?", danger: true,
    description: "이 보드에서 새로 시작한 작업을 종료합니다. 진행 중인 응답이 중단될 수 있고, 대화 기록은 남습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
    context: `${mode === "transfer" ? "기기를 옮겨 새로 실행한" : "맥락을 전달해 새로 실행한"} 작업입니다. 보드로 돌아가거나 브라우저를 닫아도 실행은 유지됩니다.`,
    ended: "이 터미널의 새 실행이 종료되었습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
    done: "이 터미널의 실행을 종료했습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
  };
  return {
    action: "터미널 닫기", title: "이 터미널을 닫을까요?", danger: true,
    description: "실행 종류를 확인하지 못했습니다. 이 터미널을 닫으면 실행 중인 작업에 영향을 줄 수 있습니다. 대화 기록은 남으며, 작업 상태는 실행 기기에서 확인하세요.",
    context: "실행 종류를 확인하지 못했습니다. 보드로 돌아가거나 브라우저를 닫아도 터미널은 유지됩니다.",
    ended: "이 터미널의 연결이 종료되었습니다. 작업 상태는 실행 기기에서 확인하세요.",
    done: "터미널을 닫았습니다. 작업 상태는 실행 기기에서 확인하세요.",
  };
}
function draftKey(terminal) {
  const info = terminalInfo(terminal);
  return info.sourceKey
    ? JSON.stringify([info.sourceKey, info.host || "", info.agent || "", info.account || ""])
    : info.id;
}
function terminalError(text = "") {
  $("terminal-error").textContent = text;
  $("terminal-error").hidden = !text;
}
function stopTerminalReader() {
  cancelDraftPaste();
  state.terminalRun++;
  state.terminalAbort?.abort();
  state.terminalAbort = null;
  state.connected = false;
}
function saveDraft() {
  if (state.terminal)
    state.drafts.set(draftKey(state.terminal), $("terminal-draft").value);
}
function cancelDraftPaste() {
  const pending = state.draftPaste;
  if (!pending) return;
  clearTimeout(pending.compositionTimer);
  pending.cancelled = true;
  pending.resolve?.(false);
  state.draftPaste = null;
}
function settleDraftComposition() {
  const pending = state.draftPaste;
  if (!pending?.resolve || pending.frameQueued) return;
  pending.frameQueued = true;
  // compositionend follows the control update; the frame also coalesces late
  // input notifications. A separate final input event is not required.
  requestAnimationFrame(() => {
    if (state.draftPaste !== pending || pending.cancelled) return;
    pending.frameQueued = false;
    if (!state.composing) pending.resolve(true);
  });
}
function startDraftComposition(event) {
  if (event?.target && event.target !== $("terminal-draft")) return;
  if (state.draftPaste) {
    cancelDraftPaste();
    $("draft-note").textContent = "새 입력을 보관했습니다. 조합이 끝나면 붙여넣기를 눌러 주세요.";
    terminalStatus($("terminal-status").textContent, state.connected);
  }
  state.composing = true;
  state.compositionOwner = state.terminal?.id || null;
  saveDraft();
}
function endDraftComposition(event) {
  if (event?.target && event.target !== $("terminal-draft")) return;
  state.composing = false;
  if (state.compositionOwner && state.compositionOwner !== state.terminal?.id) {
    cancelDraftPaste();
    $("terminal-draft").value = state.drafts.get(draftKey(state.terminal)) || "";
    return;
  }
  saveDraft();
  settleDraftComposition();
}
function inputDraft(event) {
  if (event.target && event.target !== $("terminal-draft")) return;
  if (state.compositionOwner && state.compositionOwner !== state.terminal?.id &&
      (event.isComposing || /Composition/.test(event.inputType || ""))) {
    $("terminal-draft").value = state.drafts.get(draftKey(state.terminal)) || "";
    return;
  }
  if (!event.isComposing) state.compositionOwner = state.terminal?.id || null;
  state.draftRevision++;
  saveDraft();
  // Final input may arrive before the paste click, without compositionend.
  if (event.isComposing === false) {
    state.composing = false;
    settleDraftComposition();
  }
}
function prepareDraftPaste(event) {
  // Retain textarea focus until the explicit click can request an IME commit.
  event.preventDefault();
}
function bindDraftInput(node) {
  node.addEventListener("input", inputDraft);
  node.addEventListener("compositionstart", startDraftComposition);
  node.addEventListener("compositionend", endDraftComposition);
}
function fitTerminal() {
  if (!state.term || !state.terminalVisible || !state.fit) return;
  try {
    state.fit.fit();
  } catch {
    return;
  }
  clearTimeout(state.resizeTimer);
  state.resizeTimer = setTimeout(async () => {
    if (!state.connected || !state.terminal) return;
    const size = `${state.term.cols}x${state.term.rows}`;
    if (size === state.lastSize) return;
    try {
      await api(
        `/api/terminal/${encodeURIComponent(state.terminal.id)}/resize`,
        {
          method: "POST",
          body: { cols: state.term.cols, rows: state.term.rows },
        },
      );
      state.lastSize = size;
    } catch (err) {
      if (!err.unauthorized && !err.staleAuth)
        terminalError("터미널 크기를 맞추지 못했습니다. 다시 연결해 주세요.");
    }
  }, 180);
}
async function openTerminal(terminal, { recordHistory = true } = {}) {
  closeChatQueue(false);
  stopChatPolling();
  if (recordHistory) navigateView("terminal");
  const changing = state.terminal?.id !== terminal.id;
  saveDraft();
  if (state.composing) {
    $("terminal-draft").blur();
    saveDraft();
  }
  if (changing && $("terminal-draft").cloneNode) {
    // Closing a terminal may clear the composition flag before its final input.
    // Every new terminal identity gets a new control, so old events stay detached.
    const previous = $("terminal-draft");
    const next = previous.cloneNode(true);
    previous.replaceWith(next);
    bindDraftInput(next);
  }
  stopTerminalReader();
  state.inputEpoch++;
  state.inputQueue = Promise.resolve();
  state.inputFailed = false;
  state.terminal = terminal;
  state.terminalAlive = terminal.alive !== false && !terminal.closed;
  state.terminalVisible = true;
  $("app").hidden = true;
  $("terminal-screen").hidden = false;
  $("terminal-title").textContent = terminal.title || "진행 중인 작업";
  $("terminal-route").textContent = targetLabel(terminalInfo(terminal));
  const nativeHost = terminalInfo(terminal).nativeSource?.host || terminalInfo(terminal).host;
  $("terminal-back").setAttribute("aria-label", `${hostLabel(nativeHost)}의 작업을 보드에서 보기`);
  const copy = terminalCloseCopy(terminal);
  $("terminal-close").textContent = copy.action;
  $("terminal-close").setAttribute("aria-label", copy.action);
  $("terminal-close").className = `terminal-button terminal-close${copy.danger ? " danger-quiet" : ""}`;
  $("terminal-close").disabled = !state.terminalAlive;
  $("terminal-context").textContent = copy.context;
  $("terminal-draft").value = state.drafts.get(draftKey(terminal)) || "";
  state.composing = false;
  state.draftRevision++;
  $("draft-note").textContent = "미전송 초안은 잠시 보관됩니다. 새로고침하거나 브라우저를 닫으면 사라집니다.";
  terminalError();
  renderTerminalReturn();
  if (!state.terminalAlive) {
    terminalStatus(copy.ended);
    return;
  }
  if (!window.Terminal || !window.FitAddon) {
    terminalStatus("터미널 화면을 불러오지 못했습니다.");
    terminalError(
      "터미널 파일이 준비되지 않았습니다. 보드로 돌아가 새로고침해 주세요.",
    );
    return;
  }
  if (changing || !state.term) {
    state.term?.dispose();
    state.terminalAfter = 0;
    state.terminalEpoch = null;
    state.lastSize = "";
    $("terminal-viewport").replaceChildren();
    state.term = new Terminal({
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: state.fontSize,
      lineHeight: 1.2,
      cursorBlink: true,
      scrollback: 5000,
      screenReaderMode: state.screenReaderMode,
      convertEol: false,
      allowProposedApi: false,
      theme: {
        background: "#161618",
        foreground: "#ececf1",
        cursor: "#0a84ff",
        selectionBackground: "rgba(142,142,147,0.35)",
      },
    });
    state.fit = new FitAddon.FitAddon();
    state.term.loadAddon(state.fit);
    state.term.open($("terminal-viewport"));
    state.term.onData((text) => {
      if (state.connected) sendInput(text).catch(() => {});
    });
    state.term.onBinary((data) => {
      if (state.connected)
        terminalError(
          "이 입력 형식은 지원하지 않습니다. 일반 키보드 입력을 사용해 주세요.",
        );
    });
  }
  requestAnimationFrame(fitTerminal);
  renderTerminalReturn();
  terminalStatus("터미널에 연결하고 있습니다…");
  startTerminalReader();
}
async function startTerminalReader() {
  if (networkOffline()) return;
  if (!state.terminal || !state.terminalVisible || document.hidden) return;
  if (!state.terminalAlive || state.terminal.closed) {
    terminalStatus(terminalCloseCopy(state.terminal).ended);
    renderTerminalReturn();
    return;
  }
  stopTerminalReader();
  const run = state.terminalRun;
  const id = state.terminal.id;
  const controller = new AbortController();
  state.terminalAbort = controller;
  state.inputFailed = false;
  state.inputEpoch++;
  terminalError();
  terminalStatus("터미널에 연결하고 있습니다…");
  while (
    run === state.terminalRun &&
    state.terminalVisible &&
    !document.hidden
  ) {
    try {
      const result = await api(
        `/api/terminal/${encodeURIComponent(id)}/events?after=${state.terminalAfter}&wait=20${state.terminalEpoch ? `&epoch=${encodeURIComponent(state.terminalEpoch)}` : ""}`,
        { signal: controller.signal },
      );
      if (run !== state.terminalRun) return;
      const screen = state.term;
      const commitCursor = () => {
        if (state.terminal?.id !== id || state.term !== screen) return;
        state.terminalAfter = Number.isFinite(Number(result.after))
          ? Number(result.after)
          : state.terminalAfter;
        state.terminalEpoch = result.epoch || state.terminalEpoch;
      };
      if (result.reset) {
        screen.reset();
        terminalError(
          "최근 출력부터 다시 연결했습니다. 이전 대화는 보드에서 확인할 수 있습니다.",
        );
      }
      if (result.data) {
        const bytes = Uint8Array.from(atob(result.data), (char) =>
          char.charCodeAt(0),
        );
        const written = new Promise((resolve) => screen.write(bytes, resolve));
        // xterm owns the queued bytes now. Reconnecting during its callback
        // must start after them, while a replaced screen keeps its own cursor.
        commitCursor();
        await written;
      } else commitCursor();
      if (run !== state.terminalRun) return;
      state.terminalAlive = result.alive !== false;
      if (!state.terminalAlive) {
        state.terminal.alive = false;
        $("terminal-close").disabled = true;
        terminalStatus(terminalCloseCopy(state.terminal).ended);
        renderTerminalReturn();
        refreshTerminals();
        return;
      }
      if (!state.inputFailed) {
        terminalStatus("연결됨 · 입력 가능", true);
        fitTerminal();
      }
    } catch (err) {
      if (
        err.name === "AbortError" ||
        run !== state.terminalRun ||
        err.unauthorized || err.staleAuth
      )
        return;
      state.inputEpoch++;
      terminalStatus("연결을 확인할 수 없습니다.");
      terminalError(
        "새 입력은 보내지 않습니다. 실행 상태를 확인하려면 다시 연결해 주세요.",
      );
      return;
    }
  }
}
function sendInput(text) {
  if (networkOffline() || !state.connected || state.inputFailed || !state.terminal)
    return Promise.reject(new Error("터미널에 연결되어 있지 않습니다."));
  const epoch = state.inputEpoch;
  const id = state.terminal.id;
  const req = requestId();
  const send = state.inputQueue.then(async () => {
    if (
      epoch !== state.inputEpoch ||
      networkOffline() || !state.connected ||
      state.terminal?.id !== id
    )
      throw new Error("연결이 바뀌어 입력을 보내지 않았습니다.");
    try {
      await api(`/api/terminal/${encodeURIComponent(id)}/input`, {
        method: "POST",
        body: { text, requestId: req },
      });
    } catch (err) {
      if (!err.unauthorized && !err.staleAuth && epoch === state.inputEpoch) {
        state.inputFailed = true;
        state.inputEpoch++;
        stopTerminalReader();
        terminalStatus("입력 전달을 확인하지 못했습니다.");
        terminalError(
          "입력이 반영됐을 수도 있어 자동으로 다시 보내지 않습니다. 다시 연결한 뒤 터미널 내용을 확인해 주세요.",
        );
      }
      throw err;
    }
  });
  state.inputQueue = send.catch(() => {});
  return send;
}
function returnToBoard({ selectLaunchedSource = true } = {}) {
  saveDraft();
  stopTerminalReader();
  state.inputEpoch++;
  state.terminalVisible = false;
  $("terminal-screen").hidden = true;
  $("app").hidden = false;
  renderTerminalReturn();
  schedule();
  const info = terminalInfo(state.terminal);
  if (selectLaunchedSource && state.authenticated && info.nativeSource &&
      ["handoff", "transfer"].includes(info.launchMode || info.mode)) {
    // Returning from the new execution follows the destination the user launched.
    // Its draft is separate; choosing any source in the list still never redirects.
    openNativeChat(state.terminal);
  }
  const selected = selectedSession();
  if (selected) loadChatState(selected, true);
}
function renderTerminalReturn() {
  const exists = !!state.terminal && state.terminalAlive && state.terminal.alive !== false && !state.terminal.closed;
  $("terminal-return").hidden = !exists;
  const info = terminalInfo(state.terminal);
  const host = capsHost(info.host)?.label || info.host;
  $("terminal-return-label").textContent = exists
    ? [host, state.terminal.title || "열어 둔 터미널"].filter(Boolean).join(" · ")
    : "";
}
async function pasteDraft() {
  if (state.draftPaste) return;
  if (!state.connected || !state.terminal) {
    $("draft-note").textContent = "터미널에 다시 연결한 뒤 붙여넣어 주세요.";
    return;
  }
  const pending = { id: state.terminal.id, key: draftKey(state.terminal), epoch: state.inputEpoch, cancelled: false };
  state.draftPaste = pending;
  terminalStatus($("terminal-status").textContent, true);
  try {
    if (state.composing) {
      $("draft-note").textContent = "한글 조합을 확정하고 있습니다. 초안은 보관됩니다.";
      const committed = new Promise((resolve) => { pending.resolve = resolve; });
      pending.compositionTimer = setTimeout(() => {
        if (state.draftPaste !== pending) return;
        cancelDraftPaste();
        $("draft-note").textContent = "한글 조합을 확인하지 못해 보내지 않았습니다. 조합을 마친 뒤 다시 붙여넣어 주세요.";
        terminalStatus($("terminal-status").textContent, state.connected);
      }, 1500);
      $("terminal-draft").blur();
      if (!(await committed)) return;
      clearTimeout(pending.compositionTimer);
      pending.resolve = null;
    }
    if (pending.cancelled || state.terminal?.id !== pending.id ||
        state.inputEpoch !== pending.epoch || !state.connected) return;
    const text = $("terminal-draft").value;
    const revision = state.draftRevision;
    if (!text) return;
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) {
      $("draft-note").textContent =
        "초안에 제어 문자가 포함되어 있습니다. 일반 텍스트만 붙여넣을 수 있습니다.";
      return;
    }
    const bracketed = !!state.term?.modes?.bracketedPasteMode;
    if (/[\r\n]/.test(text) && !bracketed) {
      $("draft-note").textContent =
        "현재 터미널 입력칸은 안전한 여러 줄 붙여넣기를 지원하지 않습니다. 한 줄씩 전달해 주세요.";
      return;
    }
    if (new TextEncoder().encode(text).length > 32750) {
      $("draft-note").textContent =
        "한 번에 붙여넣을 수 있는 길이를 넘었습니다. 초안을 나누어 전달해 주세요.";
      return;
    }
    const normalized = text.replace(/\r\n?/g, "\n");
    await sendInput(bracketed ? `\x1b[200~${normalized}\x1b[201~` : normalized);
    if (pending.cancelled || state.terminal?.id !== pending.id || state.inputEpoch !== pending.epoch) return;
    if (state.draftRevision === revision && $("terminal-draft").value === text) {
      $("terminal-draft").value = "";
      state.drafts.delete(pending.key);
    }
    $("draft-note").textContent =
      "붙여넣었습니다. 내용을 확인한 뒤 Enter를 눌러 실행하세요.";
    state.term.scrollToBottom();
  } catch {
    if (state.terminal?.id === pending.id)
      $("draft-note").textContent =
        "전달 여부를 확인하지 못해 초안을 남겼습니다. 터미널을 확인한 뒤 다시 시도하세요.";
  } finally {
    clearTimeout(pending.compositionTimer);
    if (state.draftPaste === pending) {
      state.draftPaste = null;
      terminalStatus($("terminal-status").textContent, state.connected);
    }
  }
}
async function refreshTerminals(authEpoch = state.authEpoch) {
  if (authEpoch !== state.authEpoch) return null;
  if (state.terminalsRefreshing) return null;
  state.terminalsRefreshing = true;
  $("running-refresh").disabled = true;
  try {
    const result = await api("/api/terminals", { authEpoch });
    assertAuthEpoch(authEpoch);
    state.terminals = result.terminals || [];
    state.terminalsError = "";
    updateInputCleanupNotice(result.inputCleanupPending === true);
    const current = state.terminals.find((t) => t.id === state.terminal?.id);
    if (current) {
      state.terminal = { ...state.terminal, ...current };
      if (current.closed || current.alive === false) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        state.terminalAlive = false;
        terminalStatus(terminalCloseCopy(state.terminal).ended);
        $("terminal-close").disabled = true;
      }
      renderTerminalReturn();
    }
    const closing = state.closingTerminal;
    if (closing?.uncertain && state.terminals.some((t) => t.id === closing.id && (t.closed || t.alive === false)))
      finishTerminalClose(closing);
    renderTerminals();
    return state.terminals;
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      state.terminalsError = "열린 터미널 목록을 확인하지 못했습니다. 마지막 목록을 표시합니다.";
      renderTerminals();
    }
    return null;
  } finally {
    if (authEpoch === state.authEpoch) {
      state.terminalsRefreshing = false;
      $("running-refresh").disabled = false;
    }
  }
}
function renderTerminals() {
  const list = $("running-list");
  const focused = document.activeElement?.dataset;
  const restoreFocus = focused?.terminalId ? { id: focused.terminalId, action: focused.action } : null;
  list.replaceChildren();
  const alive = state.terminals.filter((t) => t.alive !== false && !t.closed);
  $("running-terminals").hidden = !alive.length && !state.terminalsError;
  $("running-error").textContent = state.terminalsError;
  $("running-error").hidden = !state.terminalsError;
  $("running-refresh").hidden = !state.terminalsError;
  $("running-count").textContent = alive.length;
  for (const terminal of alive) {
    const node = button("", "running-terminal", () => openTerminal(terminal));
    node.dataset.terminalId = terminal.id;
    node.dataset.action = "open";
    const labels = el("span", "running-terminal-copy");
    labels.append(
      el("strong", "running-terminal-title", terminal.title || "열어 둔 터미널"),
      el("span", "running-terminal-meta", targetLabel(terminalInfo(terminal))),
    );
    const arrow = el("span", null, "↗");
    arrow.setAttribute("aria-hidden", "true");
    node.append(
      el("span", "dot"),
      labels,
      arrow,
    );
    const row = el("div", "running-terminal-row");
    const copy = terminalCloseCopy(terminal);
    const close = button(copy.action, `running-terminal-close${copy.danger ? " danger-quiet" : ""}`, () => closeTerminal(terminal.id));
    close.dataset.terminalId = terminal.id;
    close.dataset.action = "close";
    close.setAttribute("aria-label", `${terminal.title || "터미널"} ${copy.action}`);
    row.append(node, close);
    list.append(row);
    if (restoreFocus?.id === terminal.id)
      (restoreFocus.action === "close" ? close : node).focus();
  }
}

function closeTerminal(id) {
  if (state.terminalCloseBusy) return;
  if ($("close-terminal-dialog").open) {
    $("close-terminal-cancel").focus();
    return;
  }
  const terminal = state.terminals.find((item) => item.id === id) ||
    (state.terminal?.id === id ? state.terminal : null);
  if (!terminal) return;
  const copy = terminalCloseCopy(terminal);
  state.closingTerminal = { ...terminal, id, title: terminal.title || "열어 둔 터미널", copy, trigger: document.activeElement };
  $("close-terminal-title").textContent = copy.title;
  $("close-terminal-description").textContent = copy.description;
  $("close-terminal-name").textContent = state.closingTerminal.title;
  const draft = state.terminal?.id === id ? $("terminal-draft").value : state.drafts.get(draftKey(terminal));
  $("close-terminal-draft-note").hidden = !draft;
  $("close-terminal-draft-note").textContent = draft
    ? "같은 작업을 같은 기기·에이전트·계정으로 열면 초안을 다시 볼 수 있습니다. 새로고침하거나 브라우저를 닫으면 사라집니다."
    : "";
  $("close-terminal-error").textContent = "";
  $("close-terminal-confirm").textContent = copy.action;
  $("close-terminal-confirm").className = `primary${copy.danger ? " danger" : ""}`;
  $("close-terminal-confirm").disabled = false;
  $("close-terminal-dialog").showModal();
  $("close-terminal-cancel").focus();
}
function cancelTerminalClose() {
  if (state.terminalCloseBusy) return;
  const trigger = state.closingTerminal?.trigger;
  state.closingTerminal = null;
  $("close-terminal-dialog").close();
  if (trigger?.isConnected !== false) trigger?.focus();
}
function finishTerminalClose(closing) {
  if (state.terminal?.id === closing.id) {
    const wasVisible = state.terminalVisible;
    saveDraft();
    stopTerminalReader();
    state.inputEpoch++;
    state.terminal = null;
    state.terminalAlive = false;
    state.terminalVisible = false;
    state.composing = false;
    $("terminal-draft").value = "";
    state.term?.dispose();
    state.term = null;
    state.fit = null;
    $("terminal-viewport").replaceChildren();
    $("terminal-screen").hidden = true;
    $("app").hidden = false;
    renderTerminalReturn();
    if (wasVisible) {
      navigateView("list", true);
      backToList();
    }
    schedule();
  }
  state.closingTerminal = null;
  $("close-terminal-dialog").close();
  toast(closing.copy.done);
}
async function confirmTerminalClose() {
  if (!state.closingTerminal || state.closingTerminal.uncertain || state.terminalCloseBusy) return;
  const closing = state.closingTerminal;
  const authEpoch = state.authEpoch;
  if (state.terminal?.id === closing.id) {
    saveDraft();
    stopTerminalReader();
    state.inputEpoch++;
    terminalStatus(`${closing.copy.action} 요청을 확인하고 있습니다…`);
  }
  state.terminalCloseBusy = true;
  for (const id of ["close-terminal-confirm", "close-terminal-cancel", "close-terminal-close"])
    $(id).disabled = true;
  try {
    const result = await api(`/api/terminal/${encodeURIComponent(closing.id)}/close`, { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    finishTerminalClose(closing);
    updateInputCleanupNotice(result.inputCleanupPending === true);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      closing.uncertain = true;
      $("close-terminal-error").textContent = `${closing.copy.action} 결과를 확인하지 못했습니다. 종료 여부는 목록을 새로고침해 확인하세요. 요청을 자동으로 다시 보내지 않습니다.`;
      // An uncertain close is never resent by this confirmation dialog.
      $("close-terminal-confirm").disabled = true;
    }
  } finally {
    if (authEpoch === state.authEpoch) {
      state.terminalCloseBusy = false;
      $("close-terminal-cancel").disabled = false;
      $("close-terminal-close").disabled = false;
      await refreshTerminals(authEpoch);
    }
  }
}

function schedule() {
  clearTimeout(state.pollTimer);
  if (!state.authenticated || networkOffline() || state.terminalVisible || document.hidden) return;
  // Reading a collection already in flight does not start another host request.
  const collecting =
    state.snapshot?.hosts?.some((host) => host.refreshing) &&
    Date.now() < state.refreshDeadline;
  state.pollTimer = setTimeout(
    () => poll(!collecting),
    collecting ? 2000 : POLL_MS,
  );
}
async function loadCapabilities(authEpoch = state.authEpoch, { refreshView = true } = {}) {
  const accountFilter = state.filters.account;
  const previousRoutes = state.accountFilterRoutes || (accountFilter ? new Set(
    allSessions().filter((s) => s.account === accountFilter).map(accountRouteKey)) : null);
  const capabilities = await api("/api/capabilities", { authEpoch });
  assertAuthEpoch(authEpoch);
  state.capabilities = capabilities;
  state.csrf = state.capabilities.csrfToken || "";
  $("logout").hidden = state.capabilities.authMode === "tailscale";
  if (accountFilter && state.filters.account === accountFilter && previousRoutes?.size) {
    const labels = new Set(allSessions().filter((s) => previousRoutes.has(accountRouteKey(s))).map((s) => s.account));
    if (labels.size === 1) state.filters.account = [...labels][0];
    state.accountFilterRoutes = previousRoutes;
  }
  if (!refreshView) return;
  render();
  const s = selectedSession();
  if (s && $("route-host")) populateRoute(s);
}
async function poll(refresh = false) {
  if (networkOffline() || state.loginBusy || state.pollBusy || document.hidden || state.terminalVisible) return;
  const authEpoch = state.authEpoch;
  state.pollBusy = true;
  if (refresh) state.refreshDeadline = Date.now() + 60000;
  clearTimeout(state.pollTimer);
  try {
    const snapshot = await api(`/api/snapshot${refresh ? "?refresh=1" : ""}`, { authEpoch });
    assertAuthEpoch(authEpoch);
    state.snapshot = snapshot;
    state.authenticated = true;
    $("login").hidden = true;
    $("app").hidden = false;
    notice();
    render();
    const revision = JSON.stringify(
      state.snapshot.hosts.map((host) => [host.name, host.ok, host.fetchedAt]),
    );
    if (!state.capabilities || state.capabilitiesRevision !== revision) {
      await loadCapabilities(authEpoch);
      assertAuthEpoch(authEpoch);
      state.capabilitiesRevision = revision;
    }
    schedule();
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      notice(
        "보드 연결을 확인하지 못했습니다. 마지막으로 읽은 내용을 표시합니다. 새로고침을 눌러 다시 확인하세요.",
      );
      if (!state.snapshot) {
        $("login").hidden = false;
        $("login-error").textContent =
          "서버에 연결할 수 없습니다. 연결을 확인한 뒤 다시 열어 주세요.";
      }
    }
  } finally {
    if (authEpoch === state.authEpoch) state.pollBusy = false;
  }
}
async function manualRefresh() {
  if (state.pollBusy || state.launchBusy) return;
  const authEpoch = state.authEpoch;
  $("refresh").disabled = true;
  try {
    if (!state.csrf) await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    state.refreshDeadline = Date.now() + 60000;
    await api("/api/refresh", { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    state.detailUpdatedAt = null;
    await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    await poll(false);
    if (authEpoch === state.authEpoch) refreshTerminals(authEpoch);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) notice(err.message);
  } finally {
    if (authEpoch === state.authEpoch) $("refresh").disabled = false;
  }
}
function showLogin() {
  closeChatQueue(false);
  beginAuthEpoch();
  state.loginBusy = false;
  state.launchBusy = false;
  state.terminalCloseBusy = false;
  $("refresh").disabled = false;
  $("running-refresh").disabled = false;
  $("login-submit").disabled = false;
  clearTimeout(state.pollTimer);
  stopTerminalReader();
  stopChatPolling();
  state.chats.clear();
  state.nativeSessions.clear();
  state.accountFilterRoutes = null;
  state.inputEpoch++;
  state.authenticated = false;
  state.csrf = "";
  state.capabilities = null;
  state.capabilitiesRevision = null;
  state.refreshDeadline = 0;
  state.snapshot = null;
  state.selected = null;
  navigateView("list", true);
  document.body.classList.remove("show-detail");
  state.detailFor = null;
  state.detailRun++;
  state.planRun++;
  state.plan = null;
  state.drafts.clear();
  state.terminal = null;
  state.terminalVisible = false;
  state.term?.dispose();
  state.term = null;
  state.fit = null;
  state.terminals = [];
  state.terminalsError = "";
  state.compositionOwner = null;
  state.closingTerminal = null;
  if ($("close-terminal-dialog").open) $("close-terminal-dialog").close();
  $("terminal-draft").value = "";
  $("terminal-viewport").replaceChildren();
  $("detail").replaceChildren();
  $("groups").replaceChildren();
  $("terminal-screen").hidden = true;
  $("app").hidden = true;
  $("login").hidden = false;
  if ($("plan-dialog").open) $("plan-dialog").close();
  $("login-token").focus();
}
async function lock() {
  if (state.launchBusy) {
    toast("터미널 준비가 끝난 뒤 잠글 수 있습니다.");
    return;
  }
  const authEpoch = state.authEpoch;
  try {
    await api("/logout", { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    showLogin();
    $("login-error").textContent = "";
    toast("잠갔습니다. 실행 중인 작업은 종료하지 않습니다.");
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth)
      toast("잠금 요청을 완료하지 못했습니다. 연결을 확인해 주세요.");
  }
}
function updateViewport() {
  const height = Math.round(
    window.visualViewport?.height || window.innerHeight,
  );
  document.documentElement.style.setProperty("--app-height", `${height}px`);
  const selected = selectedSession();
  if (selected) resizeChatInput(chatEntry(selected));
  fitTerminal();
}
function showOfflineStart() {
  if (state.snapshot || state.terminalVisible) return;
  $("login").hidden = false;
  $("app").hidden = true;
  $("login-error").textContent = "오프라인입니다. 연결이 돌아오면 접속 상태를 다시 확인합니다.";
  $("login-submit").disabled = true;
}
function recoverConnection() {
  state.offline = false;
  if (networkOffline()) return;
  if (!state.loginBusy) $("login-submit").disabled = false;
  if (document.hidden || state.loginBusy) return;
  if (!state.snapshot) $("login-error").textContent = "";
  if (state.terminalVisible) {
    terminalStatus("네트워크 연결이 돌아왔습니다. 다시 연결을 눌러 주세요.");
    return;
  }
  notice("연결이 돌아왔습니다. 작업 상태를 확인하고 있습니다…");
  const initialAuthentication = !state.authenticated;
  const authEpoch = state.authEpoch;
  poll(false).then(() => {
    if (authEpoch === state.authEpoch && initialAuthentication && state.authenticated && !document.hidden &&
        !state.terminalVisible && !networkOffline()) refreshTerminals(authEpoch);
  });
  const selected = selectedSession();
  if (selected) { updateChatComposer(chatEntry(selected)); loadChatState(selected, true); }
}
function warnDraftUnload(event) {
  if (![...state.drafts.values(), state.terminal ? $("terminal-draft").value : ""].some(Boolean) &&
      ![...state.chats.values()].some((entry) => entry.text || entry.attachments.length || entry.pending || entry.uploads)) return;
  event.preventDefault();
  event.returnValue = "";
}

window.addEventListener("DOMContentLoaded", () => {
  navigateView("list", true);
  document.addEventListener("pointerdown", closeChatToolsOutside);
  document.addEventListener("keydown", closeChatToolsEscape);
  window.addEventListener("popstate", (event) => {
    const view = event.state?.sessionholic ? event.state.view : "list";
    if (!state.authenticated) {
      navigateView("list", true);
      return;
    }
    if (view === "terminal" && state.terminal) {
      if (!state.terminalVisible)
        openTerminal(state.terminal, { recordHistory: false });
      return;
    }
    if (state.terminalVisible) returnToBoard();
    document.body.classList.toggle(
      "show-detail",
      view === "detail" && !!state.selected,
    );
    if (view === "detail") focusDetailBack();
    else restoreSelectedCardFocus();
    stopChatPolling();
    const selected = selectedSession();
    if (selected && chatVisible()) loadChatState(selected, true);
  });
  try {
    for (const key of ["sessionholic:selected", "sessionholic:filters", "sessionholic:showOld"])
      localStorage.removeItem(key);
  } catch {
    /* remove legacy session metadata where available */
  }
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (networkOffline()) { showOfflineStart(); return; }
    if (state.loginBusy) return;
    let authEpoch = beginAuthEpoch();
    state.loginBusy = true;
    $("login-submit").disabled = true;
    $("login-error").textContent = "";
    try {
      const response = await fetch("/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: $("login-token").value.trim() }),
        cache: "no-store",
      });
      assertAuthEpoch(authEpoch);
      if (!response.ok)
        throw new Error(
          response.status === 403
            ? "접속 토큰이 맞지 않습니다."
            : "접속할 수 없습니다. 서버 연결을 확인해 주세요.",
        );
      authEpoch = beginAuthEpoch();
      state.loginBusy = false;
      $("login-token").value = "";
      await poll(true);
      if (authEpoch === state.authEpoch && state.authenticated) refreshTerminals(authEpoch);
    } catch (err) {
      if (authEpoch === state.authEpoch && !err.staleAuth) $("login-error").textContent = err.message;
    } finally {
      if (authEpoch === state.authEpoch) {
        state.loginBusy = false;
        $("login-submit").disabled = false;
      }
    }
  });
  $("search").addEventListener("input", (event) => {
    state.search = event.target.value.trim();
    render();
  });
  $("show-old").addEventListener("change", (event) => {
    state.showOld = event.target.checked;
    render();
  });
  $("home").addEventListener("click", backToList);
  $("refresh").addEventListener("click", manualRefresh);
  $("running-refresh").addEventListener("click", () => refreshTerminals());
  $("logout").addEventListener("click", lock);
  $("chat-queue-close").addEventListener("click", () => closeChatQueue());
  $("chat-queue-dismiss").addEventListener("click", () => closeChatQueue());
  $("chat-queue-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeChatQueue(); });
  $("chat-queue-dialog").addEventListener("click", (event) => {
    if (event.target !== $("chat-queue-dialog")) return;
    const rect = event.target.getBoundingClientRect?.();
    if (rect && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) closeChatQueue();
  });
  $("plan-close").addEventListener("click", closePlan);
  $("plan-cancel").addEventListener("click", closePlan);
  $("plan-launch").addEventListener("click", launchPlan);
  $("plan-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closePlan();
  });
  $("terminal-back").addEventListener("click", backFromTerminal);
  $("terminal-close").addEventListener("click", () => {
    if (state.terminal) closeTerminal(state.terminal.id);
  });
  $("close-terminal-confirm").addEventListener("click", confirmTerminalClose);
  $("close-terminal-cancel").addEventListener("click", cancelTerminalClose);
  $("close-terminal-close").addEventListener("click", cancelTerminalClose);
  $("close-terminal-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelTerminalClose();
  });
  $("return-terminal").addEventListener("click", () => {
    if (state.terminal) openTerminal(state.terminal);
  });
  $("terminal-reconnect").addEventListener("click", () => {
    state.lastSize = "";
    startTerminalReader();
  });
  $("terminal-bottom").addEventListener("click", () =>
    state.term?.scrollToBottom(),
  );
  $("terminal-copy").addEventListener("click", async () => {
    const text = state.term?.getSelection();
    if (!text) {
      toast("터미널에서 복사할 텍스트를 먼저 선택해 주세요.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("선택한 텍스트를 복사했습니다.");
    } catch {
      toast("클립보드에 접근할 수 없습니다.");
    }
  });
  const keys = {
    escape: "\x1b",
    tab: "\t",
    up: "\x1b[A",
    down: "\x1b[B",
    left: "\x1b[D",
    right: "\x1b[C",
    interrupt: "\x03",
    enter: "\r",
  };
  for (const node of $("terminal-keys").querySelectorAll("button")) {
    node.addEventListener("pointerdown", (event) => event.preventDefault());
    node.addEventListener("click", () => {
      if (state.composing || state.draftPaste) return;
      sendInput(keys[node.dataset.key]).catch(() => {});
    });
  }
  $("draft-paste").addEventListener("pointerdown", prepareDraftPaste);
  $("draft-paste").addEventListener("click", pasteDraft);
  bindDraftInput($("terminal-draft"));
  $("draft-panel").addEventListener("toggle", () =>
    requestAnimationFrame(fitTerminal),
  );
  for (const [id, delta] of [
    ["terminal-font-down", -1],
    ["terminal-font-up", 1],
  ])
    $(id).addEventListener("click", () => {
      state.fontSize = Math.max(10, Math.min(24, state.fontSize + delta));
      prefs.set("fontSize", state.fontSize);
      if (state.term) {
        state.term.options.fontSize = state.fontSize;
        fitTerminal();
      }
    });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(state.pollTimer);
    stopChatPolling();
    if (document.hidden) {
      if (state.terminalVisible) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        terminalStatus("화면으로 돌아오면 다시 연결합니다.");
      }
    } else if (state.terminalVisible) {
      startTerminalReader();
    } else if (state.authenticated) {
      poll(true);
      const selected = selectedSession();
      if (selected) loadChatState(selected, true);
    } else recoverConnection();
  });
  window.addEventListener("offline", () => {
    state.offline = true;
    showOfflineStart();
    stopChatPolling();
    const selected = selectedSession();
    if (selected) updateChatComposer(chatEntry(selected));
    if (state.terminalVisible) {
      stopTerminalReader();
      state.inputEpoch++;
      terminalStatus("네트워크 연결이 끊겼습니다.");
      terminalError(
        "입력은 전송되지 않습니다. 연결이 돌아오면 다시 연결을 눌러 주세요.",
      );
    } else {
      clearTimeout(state.pollTimer);
      notice(
        "오프라인입니다. 작업 상태를 확인하려면 연결 후 새로고침해 주세요.",
      );
    }
  });
  window.addEventListener("online", recoverConnection);
  $("terminal-accessibility").checked = state.screenReaderMode;
  $("terminal-accessibility").addEventListener("change", (event) => {
    state.screenReaderMode = event.target.checked;
    prefs.set("screenReaderMode", state.screenReaderMode);
    if (state.term) state.term.options.screenReaderMode = state.screenReaderMode;
  });
  window.addEventListener("resize", updateViewport);
  window.addEventListener("beforeunload", warnDraftUnload);
  window.visualViewport?.addEventListener("resize", updateViewport);
  updateViewport();
  if ("serviceWorker" in navigator)
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (networkOffline()) {
    showOfflineStart();
  } else {
    const authEpoch = state.authEpoch;
    poll(true).then(() => {
      if (authEpoch === state.authEpoch && state.authenticated) refreshTerminals(authEpoch);
    });
  }
});
