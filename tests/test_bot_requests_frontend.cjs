"use strict";

// Synthetic VM fixtures only: no browser, server, SSH, or agent process is started.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const appSource = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const messageSource = fs.readFileSync(path.join(__dirname, "../web/message-format.js"), "utf8");

function descendants(root) {
  return (root.children || []).flatMap((child) => [child, ...descendants(child)]);
}
function byClass(root, name) {
  return descendants(root).filter((child) => String(child.className || "").split(/\s+/).includes(name));
}
function textOf(root) {
  return [root.textContent || "", ...(root.children || []).map(textOf)].join(" ");
}
function node(tagName = "DIV") {
  const element = {
    tagName, children: [], value: "", textContent: "", hidden: false, disabled: false,
    dataset: {}, style: { setProperty() {}, removeProperty() {} }, attributes: {}, events: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    insertBefore(child, before) {
      this.children = this.children.filter((item) => item !== child);
      this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, child);
    },
    removeChild(child) { this.children = this.children.filter((item) => item !== child); return child; },
    querySelectorAll(selector) {
      if (selector.startsWith(".")) return byClass(this, selector.slice(1));
      if (/^[a-z]+$/.test(selector)) return descendants(this).filter((child) => child.tagName === selector.toUpperCase());
      return [];
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    focus() { this.focusCount = (this.focusCount || 0) + 1; },
    blur() {}, showModal() { this.open = true; }, close() { this.open = false; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(type, callback) { this.events[type] = callback; },
  };
  const classes = new Set();
  element.classList = {
    add(...names) { names.forEach((name) => classes.add(name)); },
    remove(...names) { names.forEach((name) => classes.delete(name)); },
    contains(name) { return classes.has(name); },
  };
  return element;
}
function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function client(fetchImpl = () => { throw new Error("Unexpected request"); }, { phone = false } = {}) {
  const nodes = new Map(), windowEvents = {}, calls = [], timers = new Map();
  let now = 2000000000000, nextTimer = 0;
  const get = (id) => {
    if (!nodes.has(id)) { const item = node(); item.id = id; nodes.set(id, item); }
    return nodes.get(id);
  };
  const document = {
    hidden: false, body: node("BODY"), documentElement: node("HTML"),
    addEventListener() {}, getElementById: get,
    createElement(tag) {
      const created = node(tag.toUpperCase());
      Object.defineProperty(created, "id", { get() { return this.elementId; },
        set(value) { this.elementId = value; nodes.set(value, this); } });
      return created;
    },
  };
  const history = {
    state: null, backCount: 0,
    pushState(value) { this.state = value; }, replaceState(value) { this.state = value; },
    back() { this.backCount++; },
  };
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({
    console, document, navigator: { onLine: true },
    window: { history, innerHeight: 800, addEventListener(type, callback) { windowEvents[type] = callback; },
      matchMedia: (query) => ({ matches: phone && query === "(max-width: 700px)" }) },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: (url, options) => { calls.push({ url, ...options }); return fetchImpl(url, options); },
    URL, URLSearchParams, Uint8Array, TextEncoder, AbortController, Date: Clock, Math, queueMicrotask,
    crypto: { randomUUID: require("node:crypto").randomUUID },
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearTimeout: (id) => timers.delete(id), requestAnimationFrame: () => 0,
    Option: function Option(label, value) { Object.assign(this, node("OPTION"), { textContent: label, value }); },
  });
  vm.runInContext(messageSource, context);
  vm.runInContext(appSource, context);
  const exposed = vm.runInContext(`({ state, emptyBotRequests, botRequestKey, botSnapshot, loadBotRequests,
    botRequestForNative, renderBotRequests, selectBotRequest, drawBotRequest, setSessionCategory,
    sessionCategoryOf, sessionCategoryKeydown, renderSessionCategories, render, poll, capabilitiesRevision,
    sourceOf, allSessions, matches, manageSession, select, chatEntry, chatSupported, currentChat, sendChat,
    chatRecovery, chatAttachmentsSupported, openChatTerminal, preparePlan,
    beginAuthEpoch, showLogin, lock, manualRefresh, backToList })`, context);
  Object.assign(exposed.state, { authenticated: true, csrf: "fixture-csrf", capabilities: { hosts: [] },
    snapshot: { serverTime: now / 1000, hosts: [] } });
  return { ...exposed, context, get, calls, windowEvents, history, timers,
    advance(ms) { now += ms; }, now: () => now };
}
function request(overrides = {}) {
  return { sourceId: "source-alpha", id: "request-1", botName: "자동홀릭", host: "fixture-host",
    title: "요청 검토", requesterLabel: "테스트 요청자", channelLabel: "테스트 채널", status: "running",
    authorityLabel: "unknown", scopeLabel: "unconfirmed", updatedAt: 2000000000, ...overrides };
}
function snapshot(requests = [request()], sources = [{ id: "source-alpha", label: "테스트 소스", ok: true }]) {
  return { requests, sources, fetchedAt: 2000000000 };
}
function cache(c, requests) {
  Object.assign(c.state.botRequests, c.botSnapshot(snapshot(requests)), { loaded: true, readAt: c.now() });
}
function session(overrides = {}) {
  const row = { host: "fixture-host", agent: "codex", home: ".codex-fixture", id: "native-1",
    initiator: "agent", title: "봇과 무관한 원본 제목", phase: "working", updatedAt: 2000000000, ...overrides };
  row.key = [row.host, row.agent, row.home || "", row.id].join("|");
  return row;
}
function addSessions(c, rows) {
  for (const row of rows) c.state.nativeSessions.set(row.key, row);
}
function botCalls(c) { return c.calls.filter((call) => call.url.startsWith("/api/bot-requests")); }

// After the first authenticated read, only entering the category checks an expired cache.
test("봇 진입은 GET 한 번이며 120초 캐시와 진행 중 요청을 공유하고 render·poll은 재조회하지 않는다", async () => {
  const pending = deferred();
  const c = client((url) => {
    if (url === "/api/bot-requests") return botCalls(c).length === 1 ? pending.promise : response(snapshot());
    if (url === "/api/snapshot") return response(c.state.snapshot);
    throw new Error(`Unexpected fixture route: ${url}`);
  });
  c.setSessionCategory("bot");
  const first = c.state.botRequests.promise;
  c.setSessionCategory("bot");
  const concurrent = c.loadBotRequests({ refresh: true });
  assert.equal(botCalls(c).length, 1);
  assert.equal(c.get("bot-requests-refresh").disabled, true);
  pending.resolve(response(snapshot())); await Promise.all([first, concurrent]);
  assert.equal(c.state.botRequests.busy, false);
  assert.equal(c.state.botRequests.promise, null);
  c.advance(119999); await c.loadBotRequests();
  assert.equal(botCalls(c).length, 1);
  c.advance(1);
  c.state.usageAt = c.now();
  c.state.capabilitiesRevision = c.capabilitiesRevision(c.state.snapshot);
  c.render(); c.renderBotRequests(); await c.poll(false, { capabilities: false });
  assert.equal(c.calls.filter((call) => call.url === "/api/snapshot").length, 1);
  assert.equal(c.state.pollFailures, 0, "일반 poll 자체도 성공해야 한다");
  assert.equal(botCalls(c).length, 1, "만료된 캐시도 일반 화면 갱신으로 읽지 않는다");
  c.setSessionCategory("general"); c.setSessionCategory("bot");
  await c.state.botRequests.promise;
  assert.deepEqual(botCalls(c).map((call) => call.method), ["GET", "GET"]);
});

test("첫 인증은 봇 GET을 한 번 시작하되 기다리지 않고 일반 목록의 중복을 제거하며 성공·실패 모두 주기 조회로 재시도하지 않는다", async () => {
  for (const succeeds of [true, false]) {
    const pending = deferred();
    const native = session({ initiator: "human" }), human = session({ id: "human-only", initiator: "human" });
    const board = { serverTime: 2000000000, hosts: [{ name: native.host, label: "테스트 기기", ok: true,
      data: { codex: [native, human] } }] };
    const c = client((url) => {
      if (url === "/api/snapshot") return response(board);
      if (url === "/api/bot-requests") return pending.promise;
      throw new Error(`Unexpected fixture route: ${url}`);
    });
    c.state.authenticated = false; c.state.snapshot = null;
    c.state.usageAt = c.now(); c.state.capabilitiesRevision = c.capabilitiesRevision(board);
    let pollFinished = false;
    const polling = c.poll().then(() => { pollFinished = true; });
    await new Promise(setImmediate);
    const initialRead = c.state.botRequests.promise;
    assert.equal(pollFinished, true, "봇 응답을 기다리는 동안에도 첫 snapshot 처리가 끝난다");
    assert.equal(c.state.pollBusy, false);
    assert.equal(c.state.pollFailures, 0);
    assert.equal(c.state.botRequests.busy, true);
    assert.equal(botCalls(c).length, 1);
    assert.equal(botCalls(c)[0].method, "GET");
    assert.equal(c.state.sessionCategory, "general");
    assert.equal(byClass(c.get("groups"), "card").length, 2);
    pending.resolve(succeeds
      ? response(snapshot([request({ nativeRef: c.sourceOf(native) })]))
      : response({ error: "메타데이터 확인 실패" }, 503));
    await Promise.all([initialRead, polling]);
    assert.equal(c.state.authenticated, true);
    assert.equal(c.state.botRequests.loaded, succeeds);
    assert.equal(c.state.sessionCategory, "general", "메타데이터 수신은 사용자의 분류를 바꾸지 않는다");
    const cards = byClass(c.get("groups"), "card");
    assert.equal(cards.length, succeeds ? 1 : 2);
    if (succeeds) {
      assert.equal(cards[0].dataset.sessionKey, human.key);
      assert.equal(c.get("session-category-bot-count").textContent, "1");
    } else assert.match(c.state.botRequests.error, /메타데이터 확인 실패/);
    c.advance(120001); c.state.usageAt = c.now();
    await c.poll(); c.render(); await c.poll();
    assert.equal(c.state.pollFailures, 0);
    assert.equal(c.calls.filter((call) => call.url === "/api/snapshot").length, 3);
    assert.equal(botCalls(c).length, 1, "성공한 캐시 만료나 초기 실패가 주기 봇 조회를 만들지 않는다");
  }
});

test("명시적 새로고침은 POST 한 번의 응답을 반영하고 실패·CSRF 만료를 자동 재시도하지 않는다", async () => {
  const pending = deferred();
  let refreshCount = 0;
  const c = client((url) => {
    if (url === "/api/bot-requests") return response(snapshot());
    if (url === "/api/bot-requests/refresh") {
      return ++refreshCount === 1 ? pending.promise : response({ code: "csrf_expired", error: "확인 실패" }, 403);
    }
    throw new Error(`Unexpected fixture route: ${url}`);
  });
  c.setSessionCategory("bot"); await c.state.botRequests.promise;
  const refresh = c.get("bot-requests-refresh").events.click();
  const sameRefresh = c.manualRefresh();
  assert.equal(refreshCount, 1);
  pending.resolve(response(snapshot([request({ title: "새 응답", status: "completed" })])));
  await Promise.all([refresh, sameRefresh]);
  assert.match(textOf(c.get("groups")), /새 응답.*완료|완료.*새 응답/);
  assert.equal(botCalls(c)[1].method, "POST");
  assert.equal(botCalls(c)[1].body, "{}");
  assert.equal(botCalls(c)[1].headers["X-CSRF-Token"], "fixture-csrf");
  await c.manualRefresh(); c.render();
  assert.equal(refreshCount, 2);
  assert.equal(c.calls.length, 3, "CSRF 재발급이나 GET 후속 조회도 없다");
  assert.match(textOf(c.get("groups")), /마지막으로 확인한 요청/);
  assert.equal(c.state.botRequests.requests[0].title, "새 응답");
});

test("잠금과 인증 세대 변경 뒤 도착한 봇 응답은 현재 목록과 선택을 되살리지 않는다", async () => {
  for (const boundary of ["lock", "auth-epoch"]) {
    const pending = deferred();
    const c = client((url) => url === "/api/bot-requests" ? pending.promise : response({ ok: true }));
    const loading = c.loadBotRequests();
    const oldCache = c.state.botRequests;
    if (boundary === "lock") await c.lock();
    else c.beginAuthEpoch();
    pending.resolve(response(snapshot([request({ title: "이전 인증의 요청" })]))); await loading;
    assert.equal(c.state.botRequests.requests.length, 0, boundary);
    assert.equal(c.state.botRequests.loaded, false, boundary);
    assert.equal(c.state.selectedBotRequest, null, boundary);
    assert.doesNotMatch(textOf(c.get("groups")), /이전 인증/);
    if (boundary === "lock") {
      assert.notEqual(c.state.botRequests, oldCache);
      assert.equal(c.state.authenticated, false);
      assert.equal(c.state.botRequests.busy, false);
      assert.equal(c.history.state.view, "list");
    }
  }
});

test("요청 ID는 소스별로 분리하며 같은 소스의 중복만 합치고 잘못된 응답을 거부한다", () => {
  const c = client();
  const alpha = request(), beta = request({ sourceId: "source-beta" });
  const result = c.botSnapshot(snapshot([alpha, beta, { ...alpha, title: "수정된 제목" }]));
  assert.notEqual(c.botRequestKey(alpha), c.botRequestKey(beta));
  assert.equal(result.requests.length, 2);
  assert.equal(result.requests[0].title, "수정된 제목");
  assert.equal(result.requests[1].sourceId, "source-beta");
  assert.notEqual(c.botRequestKey(request({ sourceId: "a|b", id: "c" })),
    c.botRequestKey(request({ sourceId: "a", id: "b|c" })), "구분자가 포함되어도 소스와 ID가 충돌하지 않는다");
  for (const invalid of [null, { requests: [], sources: null }, snapshot([request({ id: "" })]),
    snapshot([request({ sourceId: null })]), snapshot([], [null])]) {
    assert.throws(() => c.botSnapshot(invalid), /목록 형식/);
  }
});

test("nativeRef의 기기·에이전트·홈·ID가 모두 같은 세션만 제외하며 봇 같은 제목으로 추정하지 않는다", () => {
  const c = client();
  const exact = session(), human = session({ id: "human", initiator: "human", title: "자동홀릭 봇 요청" });
  const others = [session({ host: "other-host" }), session({ home: ".codex-other" }),
    session({ id: "other-id" }), session({ agent: "claude" }), session({ home: undefined }), human];
  cache(c, [request({ nativeRef: c.sourceOf(exact) })]);
  addSessions(c, [exact, ...others]);
  assert.ok(c.botRequestForNative(c.sourceOf(exact)));
  assert.equal(c.sessionCategoryOf(exact), "bot");
  assert.equal(c.matches(exact, c.now() / 1000, { category: false }), false);
  for (const row of others) {
    assert.equal(c.botRequestForNative(c.sourceOf(row)), undefined, row.key);
    assert.equal(c.matches(row, c.now() / 1000, { category: false }), true, row.key);
  }
  assert.equal(c.sessionCategoryOf(human), "general");
  c.renderSessionCategories(c.allSessions(), c.now() / 1000);
  assert.equal(c.get("session-category-agent-count").textContent, "5");
  assert.equal(c.get("session-category-general-count").textContent, "1");
  assert.equal(c.get("session-category-bot-count").textContent, "1");
  exact.phase = human.phase = "needs_input"; c.render();
  assert.match(c.context.document.title, /^\(1\) 답 필요/, "일반 알림 개수에도 봇 원본을 중복 포함하지 않는다");
});

test("nativeRef가 없는 세 봇 요청도 표시하고 미확인 권한·범위를 그대로 알리며 상세는 읽기 전용이다", () => {
  const c = client();
  const rows = ["자동홀릭", "안내봇", "작업봇"].map((botName, index) => request({ botName, id: `request-${index}`,
    sourceId: index === 2 ? "source-beta" : "source-alpha",
    title: index ? `요청 ${index}` : "<img src=x onerror=unsafe()>",
    updatedAt: index === 1 ? "2033-05-18T03:35:00Z" : 2000000000 + index }));
  cache(c, rows);
  c.state.botRequests.sources.push({ id: "source-beta", label: "다른 정상 소스", ok: true });
  c.state.botRequests.sources.push({ id: "unavailable-source", label: "확인 불가 소스", ok: false, error: { message: "연결 확인 필요" } });
  c.setSessionCategory("bot");
  const cards = byClass(c.get("groups"), "bot-request-card");
  assert.equal(cards.length, 3);
  assert.equal(cards[0].dataset.botRequestKey, c.botRequestKey(rows[1]), "ISO와 초 단위 시각을 최신순으로 정렬한다");
  assert.match(textOf(c.get("groups")), /확인 불가 소스.*연결 확인 필요/);
  for (const card of cards) {
    assert.match(textOf(card), /권한 미확인/);
    assert.match(textOf(card), /범위 미확인/);
  }
  cards.find((card) => card.dataset.botRequestKey === c.botRequestKey(rows[0])).events.click();
  const detail = c.get("detail");
  assert.match(textOf(detail), /<img src=x onerror=unsafe\(\)>/);
  assert.equal(descendants(detail).filter((item) => ["INPUT", "TEXTAREA", "SELECT", "IMG"].includes(item.tagName)).length, 0);
  assert.equal(detail.querySelectorAll("button").length, 1, "상세에는 목록으로 돌아가는 버튼만 있다");
  assert.doesNotMatch(textOf(detail), /세션 관리|터미널 열기|메시지 보내기/);
  assert.match(textOf(detail), /원래 봇에서 이어가세요/);
  const source = c.state.botRequests.sources[0];
  for (const [kind, count] of [["source", 2], ["request", 1], ["global", 3]]) {
    if (kind === "source") Object.assign(source, { ok: false, stale: true, error: "소스 읽기 실패" });
    if (kind === "request") rows[0].stale = true;
    if (kind === "global") c.state.botRequests.error = "전체 목록 읽기 실패";
    c.renderBotRequests();
    const staleTags = byClass(c.get("groups"), "bot-stale-tag");
    assert.equal(staleTags.length, count, `${kind}: 해당 요청에만 이전 정보 표식을 표시한다`);
    assert.ok(staleTags.every((tag) => tag.textContent === "이전 정보"));
    assert.match(textOf(detail), /이전 정보.*마지막으로 확인한 요청.*새로고침/);
    if (kind === "source") assert.match(textOf(c.get("groups")), /소스 읽기 실패.*마지막으로 확인한 요청을 표시합니다/);
    Object.assign(source, { ok: true, stale: false, error: null });
    rows[0].stale = false; c.state.botRequests.error = "";
    c.renderBotRequests();
    assert.equal(byClass(c.get("groups"), "bot-stale-tag").length, 0, `${kind}: 복구하면 카드 표식이 사라진다`);
    assert.equal(byClass(detail, "bot-source-notice").length, 0, `${kind}: 요청 내용이 같아도 상세 표식을 갱신한다`);
    assert.equal(c.state.selectedBotRequest, c.botRequestKey(rows[0]));
  }
  assert.equal(c.calls.length, 0);
});

test("봇 요청에 묶인 세션은 대화를 보기 전용으로 보이고, 보내기·관리·터미널·이어가기는 하지 않는다", async () => {
  const c = client();
  const native = session(); addSessions(c, [native]);
  const row = request({ nativeRef: c.sourceOf(native) }); cache(c, [row]);
  // Its conversation, to follow: watched, with the request's details in place of the composer.
  c.select(native.key);
  assert.equal(c.state.selected, native.key);
  assert.equal(c.state.selectedBotRequest, c.botRequestKey(row));
  assert.equal(c.state.sessionCategory, "bot");
  assert.match(textOf(c.get("detail")), /보기 전용/);
  assert.match(textOf(c.get("detail")), /입력과 실행 관리는 원래 봇/);
  assert.doesNotMatch(textOf(c.get("detail")), /이 작업에 메시지 보내기/);
  // Picking the request shows the same conversation.
  c.state.selectedBotRequest = null;
  c.selectBotRequest(c.botRequestKey(row));
  assert.equal(c.state.selected, native.key);
  assert.equal(c.state.selectedBotRequest, c.botRequestKey(row));
  await c.manageSession(native);
  assert.equal(c.get("session-control-dialog").open, undefined);
  const entry = c.chatEntry(native);
  entry.text = "전송하면 안 되는 초안";
  entry.data = { route: c.sourceOf(native), capability: { supported: true },
    recovery: { supported: true, action: "resume_session" } };
  assert.equal(c.currentChat(entry), true);
  assert.equal(c.chatSupported(entry), false);
  assert.equal(c.chatRecovery(entry), null);
  assert.equal(c.chatAttachmentsSupported(entry), false);
  await c.sendChat(entry);
  assert.equal(entry.text, "전송하면 안 되는 초안");
  assert.equal(entry.sending, false);
  c.openChatTerminal(native);
  c.state.route = { host: native.host, agent: native.agent, account: `codex:${native.home}` };
  await c.preparePlan();
  assert.equal(c.get("plan-dialog").open, undefined);
  // Reading its conversation only: no send, session control, plan, launch or terminal call.
  const writes = c.calls.filter((call) => /^\/api\/(chat\/(send|upload|warm|prompt)|session\/(preview|action|archive)|plan|launch|terminal|new-session)/.test(call.url));
  assert.deepEqual(writes, [], "채팅 보내기·관리·실행·터미널 API를 호출하지 않는다");
});

test("세 분류의 방향키·Home·End와 휴대폰 뒤로·앞으로가 봇 상세 선택을 보존한다", () => {
  const c = client(undefined, { phone: true });
  c.context.navigator.onLine = false; c.windowEvents.DOMContentLoaded();
  c.context.navigator.onLine = true; c.state.authenticated = true;
  const row = request(); cache(c, [row]);
  let prevented = 0;
  for (const [key, category] of [["ArrowRight", "agent"], ["ArrowRight", "bot"], ["ArrowRight", "general"],
    ["ArrowLeft", "bot"], ["Home", "general"], ["End", "bot"]]) {
    c.sessionCategoryKeydown({ key, preventDefault() { prevented++; } });
    assert.equal(c.state.sessionCategory, category);
    assert.equal(c.get(`session-category-${category}`).tabIndex, 0);
    assert.equal(c.get(`session-category-${category}`).attributes["aria-selected"], "true");
    assert.ok(c.get(`session-category-${category}`).focusCount > 0);
  }
  assert.equal(prevented, 6);
  c.selectBotRequest(c.botRequestKey(row));
  assert.equal(c.history.state.view, "detail");
  assert.equal(c.state.detailShown, true);
  const detail = c.get("detail").children[1];
  c.backToList(); assert.equal(c.history.backCount, 1);
  c.history.state = { sessionholic: true, view: "list" };
  c.windowEvents.popstate({ state: c.history.state });
  assert.equal(c.state.detailShown, false);
  assert.equal(c.state.selectedBotRequest, c.botRequestKey(row));
  assert.ok(byClass(c.get("groups"), "bot-request-card")[0].focusCount > 0);
  c.history.state = { sessionholic: true, view: "detail" };
  c.windowEvents.popstate({ state: c.history.state, hasUAVisualTransition: true });
  assert.equal(c.state.detailShown, true);
  assert.equal(c.get("detail").children[1], detail, "앞으로 이동은 저장된 메타 상세를 재사용한다");
  assert.equal(c.calls.length, 0, "히스토리 이동은 봇 목록이나 대화를 다시 읽지 않는다");
  c.state.botRequests.requests = []; c.renderBotRequests();
  assert.equal(c.state.selectedBotRequest, null);
  assert.equal(c.state.detailShown, false);
  assert.equal(c.history.state.view, "list");
});
