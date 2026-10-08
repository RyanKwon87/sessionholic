"use strict";

// Exercise state and transport boundaries without starting a browser or a real agent.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");

function node() {
  return {
    children: [],
    value: "",
    textContent: "",
    hidden: false,
    disabled: false,
    dataset: {},
    style: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    removeChild(child) { this.children = this.children.filter((item) => item !== child); return child; },
    insertBefore(child, before) {
      this.children = this.children.filter((item) => item !== child);
      const index = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(index, 0, child); return child;
    },
    querySelectorAll: () => [],
    querySelector: () => null,
    focus() { this.focusCount = (this.focusCount || 0) + 1; },
    blur() {},
    showModal() { this.open = true; },
    close() { this.open = false; },
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    events: {},
    addEventListener(type, callback) { this.events[type] = callback; },
    classList: { add() {}, remove() {} },
  };
}

function client(
  fetchImpl = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true }),
  }),
  timers = { setTimeout, clearTimeout },
) {
  const nodes = new Map();
  const frames = [];
  const windowEvents = {};
  const documentEvents = {};
  const document = {
    addEventListener(type, callback) { documentEvents[type] = callback; },
    documentElement: { style: { setProperty() {} } },
    body: {
      classList: {
        remove() {},
        add() {},
        contains() {
          return false;
        },
      },
    },
    hidden: false,
    createElement(tag) { const created = node(); created.tagName = tag.toUpperCase(); return created; },
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
  };
  const context = vm.createContext({
    console,
    document,
    window: {
      addEventListener(type, callback) { windowEvents[type] = callback; },
      matchMedia: () => ({ matches: false }),
      history: {
        state: null,
        backCount: 0,
        pushState(value) {
          this.state = value;
        },
        replaceState(value) {
          this.state = value;
        },
        back() {
          this.backCount++;
        },
      },
    },
    localStorage: { getItem: () => null },
    fetch: fetchImpl,
    URLSearchParams,
    Uint8Array,
    TextEncoder,
    AbortController,
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    queueMicrotask,
    requestAnimationFrame: (callback) => { frames.push(callback); },
    Date,
    Math,
    crypto: { randomUUID: require("node:crypto").randomUUID },
    Option: function Option(label, value) {
      this.textContent = label;
      this.value = value;
      this.disabled = false;
    },
  });
  vm.runInContext(source, context);
  const api = vm.runInContext(
    "({state,api,sourceOf,accountOf,sendInput,pasteDraft,prepareDraftPaste,startDraftComposition,endDraftComposition,inputDraft,stopTerminalReader,startTerminalReader,closePlan,navigateView,backToList,backFromTerminal,closeTerminal,cancelTerminalClose,confirmTerminalClose,populateAccount,populateRoute,updateContinue,renderPlan,launchPlan,openTerminal,saveDraft,draftKey,refreshTerminals,renderTerminals,renderTerminalReturn,renderFilters,terminalCloseCopy,warnDraftUnload,preparePlan,renderGroups,renderMessages,loadDetail,chatKey,chatEntry,currentChat,chatSupported,chatAttachmentsSupported,buildChatComposer,sendChat,loadChatState,applyChatReceipt,uploadChatFiles,pasteChat,scheduleChatPoll,stopChatPolling,openNativeChat,allSessions,buildContinuePanel,chatReceiptCopy,returnToBoard,readChatClipboard,accountDisplay,sessionAccountLabel,closeChatToolsOutside,closeChatToolsEscape,drawDetail,refreshDetail,loadCapabilities,matches,recoverConnection,networkOffline,poll,select,focusDetailBack,restoreSelectedCardFocus,showLogin,lock,beginAuthEpoch})",
    context,
  );
  api.state.csrf = "csrf-test";
  return {
    ...api,
    context,
    nodes,
    windowEvents,
    documentEvents,
    get: document.getElementById.bind(document),
    flushFrames: () => { for (const callback of frames.splice(0)) callback(); },
  };
}

function findId(root, id) {
  if (root.id === id) return root;
  for (const child of root.children || []) { const found = findId(child, id); if (found) return found; }
  return null;
}

function textOf(node) {
  return [node.textContent || "", ...(node.children || []).map(textOf)].join(" ");
}

test("routing uses runtime identity; Codex accounts preserve home isolation", () => {
  const c = client();
  assert.equal(
    c.sourceOf({
      host: "workstation",
      agent: "claude",
      id: "worker-id",
      sessionId: "conversation-id",
    }).id,
    "worker-id",
  );
  assert.equal(c.accountOf({ agent: "claude" }), "claude:default");
  assert.equal(
    c.accountOf({ agent: "codex", home: ".codex-example" }),
    "codex:.codex-example",
  );
});

test("cross-host transfer keeps the source Codex home or requires an explicit target account", () => {
  const c = client();
  const source = {
    host: "workstation",
    hostLabel: "Workstation",
    agent: "codex",
    home: ".codex-example",
    account: "Sample profile",
  };
  c.state.csrf = "csrf-test";
  c.state.capabilities = {
    terminal: { available: true },
    hosts: [{
      name: "homeserver",
      label: "Home server",
      agents: [{
        id: "codex",
        accounts: [
          { id: "codex:.codex-secondary", label: "업무" },
          { id: "codex:.codex-example", label: "Sample profile" },
        ],
      }],
    }],
  };
  c.state.route = { host: "homeserver", agent: "codex", account: "" };
  c.populateAccount(source);
  assert.equal(c.get("route-account").value, "codex:.codex-example");

  c.state.capabilities.hosts[0].agents[0].accounts = [
    { id: "codex:.codex-secondary", label: "업무" },
  ];
  c.state.route.account = "";
  c.populateAccount(source);
  assert.equal(c.get("route-account").value, "");
  assert.equal(c.get("route-account").children[0].textContent, "대상 계정을 선택하세요");
  assert.equal(c.get("continue-open").disabled, true);
  assert.match(c.get("continue-open").textContent, /이 기기로 넘기기/);
  assert.match(c.get("continue-note").textContent, /계정을 직접 선택/);
});

test("transfer plan states interruption, copied workspace, exclusions, and fresh destination", () => {
  const c = client();
  c.state.capabilities = {
    hosts: [{ name: "homeserver", label: "Home server", agents: [{
      id: "codex",
      label: "Codex",
      accounts: [{ id: "codex:.codex-example", label: "Sample profile" }],
    }] }],
  };
  const source = {
    hostLabel: "Workstation",
    agent: "codex",
    account: "Sample profile",
  };
  const target = { host: "homeserver", agent: "codex", account: "codex:.codex-example" };
  const plan = {
    allowed: true,
    id: "transfer-plan",
    mode: "transfer",
    transfer: {
      sourceHostLabel: "Workstation",
      targetHostLabel: "Home server",
      requiresInterrupt: true,
      destinationNote: "Worktrees/sample-app-20300101T0900",
      workspace: {
        root: "/home/demo/projects/sample-app",
        cwdRelative: "sample-app",
        git: true,
        head: "1234567890abcdef",
        branch: "feature/mobile",
        fileCount: 14,
        bytes: 1048576,
        excluded: [".git", "node_modules"],
        warnings: ["일부 생성 파일은 제외됩니다."],
      },
    },
  };
  c.renderPlan(source, plan, target);
  const body = textOf(c.get("plan-body"));
  assert.match(body, /Workstation.*sample-app.*Home server/);
  assert.match(body, /진행 중인 응답을 중단하고/);
  assert.match(body, /14개 파일.*1MB/);
  assert.match(body, /브랜치 feature\/mobile.*기준 12345678/);
  assert.match(body, /제외 항목: \.git · node_modules/);
  assert.match(body, /왕복 인계도 매번 새 폴더/);
  assert.match(body, /Worktrees\/sample-app/);
  assert.equal(c.get("plan-launch").textContent, "중단하고 Home server로 넘기기");

  plan.transfer.requiresInterrupt = false;
  c.renderPlan(source, plan, target);
  assert.match(textOf(c.get("plan-body")), /진행 중인 응답은 없습니다/);
  assert.equal(c.get("plan-launch").textContent, "Home server로 넘기기");
});

test("transfer launch shows interruption progress and reports the new execution host", async () => {
  const calls = [];
  let release;
  const c = client(async (url, options) => {
    calls.push({ url, options });
    if (url === "/api/launch")
      return await new Promise((resolve) => {
        release = () => resolve({
          ok: true,
          status: 200,
          json: async () => ({ terminal: { id: "transfer-terminal", title: "project" }, mode: "transfer" }),
        });
      });
    return { ok: true, status: 200, json: async () => ({ terminals: [] }) };
  });
  c.state.plan = {
    id: "transfer-plan",
    allowed: true,
    mode: "transfer",
    transfer: { requiresInterrupt: true, targetHostLabel: "Home server" },
  };
  const pending = c.launchPlan();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.state.launchBusy, true);
  assert.match(textOf(c.get("plan-body")), /응답 중단·파일 복사·대상 실행 준비 중/);
  assert.equal(c.get("plan-launch").disabled, true);
  assert.equal(c.get("plan-close").disabled, true);
  release();
  await pending;
  assert.equal(calls.filter((call) => call.url === "/api/launch").length, 1);
  assert.match(c.get("toast").textContent, /실제 실행 위치가 Home server로 바뀌었습니다/);
});

test("mutating requests carry CSRF and never place credentials in a URL", async () => {
  const calls = [];
  const c = client(async (...args) => {
    calls.push(args);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  await c.api("/api/plan", { method: "POST", body: { target: "fixture" } });
  assert.equal(calls[0][0], "/api/plan");
  assert.equal(calls[0][1].headers["X-CSRF-Token"], "csrf-test");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.equal(calls[0][1].cache, "no-store");
});

test("uncertain input is never retried and queued input is discarded", async () => {
  let calls = 0;
  const c = client(async () => {
    calls++;
    throw new Error("lost response");
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  const first = c.sendInput("first");
  const queued = c.sendInput("second");
  await Promise.allSettled([first, queued]);
  assert.equal(calls, 1);
  assert.equal(c.state.connected, false);
  assert.equal(c.state.inputFailed, true);
  await assert.rejects(c.sendInput("third"));
  assert.equal(calls, 1);
});

test("input captures terminal ownership and cannot leak into a switched terminal", async () => {
  const calls = [];
  let release;
  const c = client(async (url) => {
    calls.push(url);
    if (calls.length === 1)
      await new Promise((resolve) => {
        release = resolve;
      });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  const first = c.sendInput("a");
  const queued = c.sendInput("b");
  await new Promise((resolve) => setImmediate(resolve));
  c.state.terminal = { id: "term-2" };
  c.state.inputEpoch++;
  release();
  await Promise.allSettled([first, queued]);
  assert.deepEqual(calls, ["/api/terminal/term-1/input"]);
});

test("multiline draft requires bracketed paste and preserves its text when unavailable", async () => {
  let calls = 0;
  const c = client(async () => {
    calls++;
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: false } };
  c.get("terminal-draft").value = "첫 줄\n둘째 줄";
  await c.pasteDraft();
  assert.equal(calls, 0);
  assert.equal(c.get("terminal-draft").value, "첫 줄\n둘째 줄");
  assert.match(c.get("draft-note").textContent, /한 줄씩/);
});

test("Korean draft is bracketed, acknowledged, and never followed by Enter", async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body));
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: true }, scrollToBottom() {} };
  c.get("terminal-draft").value = "첫 줄\n둘째 줄";
  await c.pasteDraft();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].text, "\x1b[200~첫 줄\n둘째 줄\x1b[201~");
  assert.equal(c.get("terminal-draft").value, "");
});

test("unconfirmed composition is retained, reported, and never blindly pasted", async () => {
  let calls = 0;
  const c = client(async () => {
    calls++;
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: true } };
  c.state.composing = true;
  c.get("terminal-draft").value = "조합 중";
  const pending = c.pasteDraft();
  c.flushFrames();
  assert.equal(calls, 0);
  assert.match(c.get("draft-note").textContent, /조합/);
  c.stopTerminalReader();
  await pending;
  c.state.connected = true;
  c.state.composing = false;
  c.get("terminal-draft").value = "x\x1b[201~\rcommand";
  await c.pasteDraft();
  assert.equal(calls, 0);
});

test("a lost draft acknowledgement preserves draft and stops further input", async () => {
  const c = client(async () => {
    throw new Error("lost ack");
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: true } };
  c.get("terminal-draft").value = "보존할 초안";
  await c.pasteDraft();
  assert.equal(c.get("terminal-draft").value, "보존할 초안");
  assert.equal(c.state.connected, false);
});

test("paste blurs Android IME and sends only the final committed Korean once", async () => {
  const sent = [];
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: false }, scrollToBottom() {} };
  const draft = c.get("terminal-draft");
  c.context.document.activeElement = draft;
  draft.value = "한글 테스";
  c.startDraftComposition();
  let blurs = 0;
  draft.blur = () => {
    blurs++;
    c.context.document.activeElement = null;
    c.endDraftComposition();
    draft.value = "한글 테스트 123";
    c.inputDraft({ isComposing: false });
  };
  const pending = c.pasteDraft();
  await c.pasteDraft();
  assert.equal(sent.length, 0);
  c.flushFrames();
  await pending;
  assert.equal(blurs, 1);
  assert.deepEqual(sent, ["한글 테스트 123"]);
  assert.equal(draft.value, "");
});

test("final non-composing input after blur can commit Android's missing compositionend", async () => {
  const sent = [];
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  c.get("terminal-draft").value = "한글";
  c.startDraftComposition();
  const pending = c.pasteDraft();
  c.inputDraft({ isComposing: true });
  c.flushFrames();
  assert.equal(sent.length, 0);
  c.get("terminal-draft").value = "한글 완성";
  c.inputDraft({ isComposing: false });
  c.flushFrames();
  await pending;
  assert.deepEqual(sent, ["한글 완성"]);
});

test("final non-composing input between native blur and click clears stale IME state", async () => {
  const sent = [];
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({}) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  const draft = c.get("terminal-draft");
  c.context.document.activeElement = draft;
  c.startDraftComposition();
  c.context.document.activeElement = null;
  draft.value = "한글 테스트 123";
  c.inputDraft({ isComposing: false });
  assert.equal(c.state.composing, false);
  await c.pasteDraft();
  assert.deepEqual(sent, ["한글 테스트 123"]);
  assert.equal(c.state.draftPaste, null);
});

test("paste pointerdown retains IME focus until click explicitly commits it", async () => {
  const sent = [];
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({}) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  const draft = c.get("terminal-draft");
  draft.value = "한글";
  c.context.document.activeElement = draft;
  c.startDraftComposition();
  let prevented = 0, blurs = 0;
  c.prepareDraftPaste({ preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
  assert.equal(c.context.document.activeElement, draft);
  assert.equal(c.state.composing, true);
  draft.blur = () => {
    blurs++;
    c.context.document.activeElement = null;
    draft.value = "한글 완성";
    c.inputDraft({ isComposing: false });
  };
  const pending = c.pasteDraft();
  c.flushFrames();
  await pending;
  assert.equal(blurs, 1);
  assert.deepEqual(sent, ["한글 완성"]);
});

test("compositionend alone commits updated DOM; a later input notification never duplicates it", async () => {
  const sent = [];
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({}) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  const draft = c.get("terminal-draft");
  draft.value = "한글 조합";
  c.startDraftComposition();
  const pending = c.pasteDraft();
  draft.value = "한글 조합 완성";
  c.endDraftComposition();
  c.flushFrames();
  await pending;
  c.inputDraft({ isComposing: false });
  c.flushFrames();
  assert.deepEqual(sent, ["한글 조합 완성"]);
  assert.equal(c.state.draftPaste, null);
});

test("missing IME completion times out without sending and allows an explicit retry", async () => {
  const sent = [], callbacks = new Map();
  let timerId = 0;
  const c = client(async (url, opts) => {
    sent.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({}) };
  }, {
    setTimeout(callback) { const id = ++timerId; callbacks.set(id, callback); return id; },
    clearTimeout(id) { callbacks.delete(id); },
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  c.get("terminal-draft").value = "보관할 한글";
  c.startDraftComposition();
  const pending = c.pasteDraft();
  assert.equal(c.get("draft-paste").disabled, true);
  [...callbacks.values()][0]();
  await pending;
  assert.deepEqual(sent, []);
  assert.equal(c.get("terminal-draft").value, "보관할 한글");
  assert.equal(c.state.draftPaste, null);
  assert.equal(c.get("draft-paste").disabled, false);
  assert.match(c.get("draft-note").textContent, /보내지 않았습니다/);
  c.endDraftComposition();
  c.inputDraft({ isComposing: false });
  c.flushFrames();
  assert.deepEqual(sent, []);
  await c.pasteDraft();
  assert.deepEqual(sent, ["보관할 한글"]);
});

test("pending IME paste is cancelled by a terminal switch, epoch change, or new composition", async () => {
  for (const change of ["terminal", "epoch", "composition"]) {
    let calls = 0;
    const c = client(async () => { calls++; return { ok: true, status: 200, json: async () => ({}) }; });
    c.state.connected = true;
    c.state.terminal = { id: "term-1" };
    c.get("terminal-draft").value = "보관할 초안";
    c.startDraftComposition();
    const pending = c.pasteDraft();
    if (change === "terminal") {
      c.state.drafts.set("term-2", "새 터미널 초안");
      c.state.terminal = { id: "term-2" };
    }
    if (change === "epoch") c.state.inputEpoch++;
    c.endDraftComposition();
    if (change === "composition") c.startDraftComposition();
    c.flushFrames();
    await pending;
    assert.equal(calls, 0, change);
    assert.equal(c.get("terminal-draft").value, change === "terminal" ? "새 터미널 초안" : "보관할 초안", change);
  }
});

test("double paste sends once and an acknowledgement preserves a newly typed identical draft", async () => {
  let calls = 0, release;
  const c = client(async () => {
    calls++;
    await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({}) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { scrollToBottom() {} };
  c.get("terminal-draft").value = "같은 초안";
  const pending = c.pasteDraft();
  await c.pasteDraft();
  await new Promise((resolve) => setImmediate(resolve));
  c.inputDraft({ isComposing: false });
  release();
  await pending;
  assert.equal(calls, 1);
  assert.equal(c.get("terminal-draft").value, "같은 초안");
});

test("terminal close captures the confirmed ID, blocks double submit, and preserves a switched terminal", async () => {
  const urls = [];
  let release;
  const c = client(async (url) => {
    urls.push(url);
    if (url.endsWith("/close")) await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ terminals: [] }) };
  });
  c.state.terminal = { id: "term-1", title: "첫 터미널" };
  c.state.terminals = [c.state.terminal];
  c.state.drafts.set("term-1", "이전 초안");
  c.get("terminal-draft").value = "이전 초안";
  c.closeTerminal("term-1");
  const pending = c.confirmTerminalClose();
  await c.confirmTerminalClose();
  c.cancelTerminalClose();
  assert.equal(c.get("close-terminal-dialog").open, true);
  c.state.terminal = { id: "term-2" };
  c.get("terminal-draft").value = "새 터미널 초안";
  release();
  await pending;
  assert.equal(urls.filter((url) => url.endsWith("/close")).length, 1);
  assert.equal(urls[0], "/api/terminal/term-1/close");
  assert.equal(c.state.terminal.id, "term-2");
  assert.equal(c.get("terminal-draft").value, "새 터미널 초안");
  assert.equal(c.state.drafts.get("term-1"), "이전 초안");
});

test("cancelling terminal close sends nothing; an uncertain close is never retried", async () => {
  let closes = 0;
  const c = client(async (url) => {
    if (url.endsWith("/close")) { closes++; throw new Error("lost close acknowledgement"); }
    return { ok: true, status: 200, json: async () => ({ terminals: [] }) };
  });
  c.state.terminal = { id: "term-1" };
  c.closeTerminal("term-1");
  c.cancelTerminalClose();
  await c.confirmTerminalClose();
  assert.equal(closes, 0);
  c.get("terminal-draft").value = "보관 초안";
  c.closeTerminal("term-1");
  await c.confirmTerminalClose();
  await c.confirmTerminalClose();
  assert.equal(closes, 1);
  assert.equal(c.state.terminal.id, "term-1");
  assert.equal(c.get("terminal-draft").value, "보관 초안");
  assert.match(c.get("close-terminal-error").textContent, /종료 여부/);
});

test("confirmed current-terminal close cancels pending IME paste and disposes only its screen", async () => {
  const sent = [];
  let disposed = 0;
  const c = client(async (url) => {
    sent.push(url);
    return { ok: true, status: 200, json: async () => ({ terminals: [] }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.terminalVisible = true;
  c.state.term = { dispose() { disposed++; } };
  c.get("terminal-draft").value = "조합 중";
  c.startDraftComposition();
  const paste = c.pasteDraft();
  c.closeTerminal("term-1");
  await c.confirmTerminalClose();
  await paste;
  assert.equal(disposed, 1);
  assert.equal(c.state.terminal, null);
  assert.equal(c.state.terminalVisible, false);
  assert.equal(c.get("terminal-draft").value, "");
  assert.equal(sent.some((url) => url.endsWith("/input")), false);
  assert.equal(c.state.drafts.get("term-1"), "조합 중");
});

test("close wording and tone distinguish attachment, fresh execution, and unknown modes", async () => {
  const c = client();
  for (const [terminal, label, danger, description] of [
    [{ id: "attach", mode: "attach" }, "연결 닫기", false, /중단 명령을 보내지/],
    [{ id: "handoff", mode: "handoff" }, "실행 종료", true, /새로 시작한 작업/],
    [{ id: "transfer", metadata: { mode: "transfer" } }, "실행 종료", true, /실행 기기의 최근 작업/],
    [{ id: "unknown" }, "터미널 닫기", true, /종류를 확인하지 못/],
  ]) {
    await c.openTerminal(terminal);
    assert.equal(c.get("terminal-close").textContent, label);
    assert.match(c.get("terminal-context").textContent, /돌아가거나 브라우저를 닫아도/);
    c.closeTerminal(terminal.id);
    assert.equal(c.get("close-terminal-confirm").textContent, label);
    assert.equal(c.get("close-terminal-confirm").className.includes("danger"), danger);
    assert.match(c.get("close-terminal-description").textContent, description);
    c.cancelTerminalClose();
  }
});

test("closed and reopened work keeps unsent drafts in memory by route and source identity", async () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }));
  let persisted = 0;
  c.context.localStorage.setItem = () => { persisted++; };
  const terminal = { id: "old-id", mode: "attach", sourceKey: "source", host: "homeserver", agent: "codex", account: "personal" };
  await c.openTerminal(terminal);
  c.get("terminal-draft").value = "보존할 한글 초안";
  c.inputDraft({ isComposing: false });
  c.closeTerminal(terminal.id);
  assert.equal(c.get("close-terminal-draft-note").hidden, false);
  await c.confirmTerminalClose();
  await c.openTerminal({ ...terminal, id: "new-id" });
  assert.equal(c.get("terminal-draft").value, "보존할 한글 초안");
  await c.openTerminal({ ...terminal, id: "different-route", account: "other" });
  assert.equal(c.get("terminal-draft").value, "");
  await c.openTerminal({ ...terminal, id: "same-route-again" });
  assert.equal(c.get("terminal-draft").value, "보존할 한글 초안");
  assert.equal(persisted, 0);
});

test("lost close acknowledgement recovers only from a fresh closed readback, without retry", async () => {
  let closes = 0;
  const c = client(async (url) => {
    if (url.endsWith("/close")) { closes++; throw new Error("lost acknowledgement"); }
    return { ok: true, status: 200, json: async () => ({ terminals: [{ id: "term-1", mode: "attach", alive: false, closed: true }] }) };
  });
  c.state.terminal = { id: "term-1", mode: "attach" };
  c.state.terminalVisible = true;
  c.get("terminal-draft").value = "보관";
  c.closeTerminal("term-1");
  await c.confirmTerminalClose();
  await c.confirmTerminalClose();
  assert.equal(closes, 1);
  assert.equal(c.state.terminal, null);
  assert.equal(c.get("close-terminal-dialog").open, false);
  assert.equal(c.state.drafts.get("term-1"), "보관");
  assert.match(c.get("toast").textContent, /연결을 닫았습니다/);
});

test("closing the visible terminal replaces its history view before another terminal opens", async () => {
  const c = client();
  c.navigateView("list", true);
  c.navigateView("detail");
  c.navigateView("terminal");
  c.state.terminal = { id: "first", mode: "attach" };
  c.state.terminalVisible = true;
  c.closeTerminal("first");
  await c.confirmTerminalClose();
  assert.equal(c.state.terminalVisible, false);
  assert.equal(c.context.window.history.state.view, "list");
  assert.equal(c.context.window.history.backCount, 0);
  const views = [];
  c.context.window.history.pushState = function (value) {
    views.push(value.view);
    this.state = value;
  };
  await c.openTerminal({ id: "second", mode: "attach" });
  assert.deepEqual(views, ["terminal"]);
  c.backFromTerminal();
  assert.equal(c.context.window.history.backCount, 1);
});

test("closing a terminal from the board preserves its current detail history", async () => {
  for (const closingCurrent of [true, false]) {
    const c = client();
    c.navigateView("detail", true);
    c.state.terminal = { id: "current", mode: "attach" };
    c.state.terminalVisible = false;
    c.state.terminals = [c.state.terminal, { id: "other", mode: "handoff" }];
    c.closeTerminal(closingCurrent ? "current" : "other");
    await c.confirmTerminalClose();
    assert.equal(c.context.window.history.state.view, "detail");
    assert.equal(c.context.window.history.backCount, 0);
    assert.equal(c.state.terminal?.id, closingCurrent ? undefined : "current");
  }
});

test("failed terminal listing keeps previous rows and exposes retry; a closed row hides return", async () => {
  let fail = true;
  const c = client(async () => {
    if (fail) throw new Error("offline");
    return { ok: true, status: 200, json: async () => ({ terminals: [{ id: "term", mode: "handoff", alive: false }] }) };
  });
  c.state.terminal = { id: "term", mode: "handoff", alive: true, host: "homeserver", agent: "codex", account: "Sample profile", title: "열린 작업" };
  c.state.terminals = [c.state.terminal];
  await c.refreshTerminals();
  assert.equal(c.get("running-terminals").hidden, false);
  assert.equal(c.get("running-list").children.length, 1);
  assert.match(textOf(c.get("running-list")), /homeserver · Codex · Sample profile/);
  assert.equal(c.get("running-error").hidden, false);
  assert.equal(c.get("running-refresh").hidden, false);
  fail = false;
  await c.refreshTerminals();
  assert.equal(c.get("running-error").hidden, true);
  assert.equal(c.get("running-refresh").hidden, true);
  assert.equal(c.get("terminal-return").hidden, true);
  assert.equal(c.get("terminal-reconnect").hidden, true);
  assert.match(c.get("terminal-status").textContent, /실행 기기의 최근 작업/);
});

test("capability and filter refresh retain selected choices and block missing accounts or offline hosts", () => {
  const c = client();
  const source = { key: "source", host: "workstation", agent: "codex", home: ".codex-example" };
  c.state.selected = source.key;
  c.state.route = { host: "homeserver", agent: "codex", account: "chosen-account" };
  c.state.capabilities = { terminal: { available: true }, hosts: [
    { name: "workstation", online: true },
    { name: "homeserver", online: true, agents: [{ id: "codex", accounts: [{ id: "another-account", available: true }] }] },
  ] };
  c.populateRoute(source);
  assert.equal(c.state.route.account, "chosen-account");
  assert.equal(c.get("route-account").value, "chosen-account");
  assert.equal(c.get("continue-open").disabled, true);
  c.state.capabilities.hosts[1].agents[0].accounts.push({ id: "chosen-account", available: true });
  c.populateRoute(source);
  assert.equal(c.get("continue-open").disabled, false);
  c.state.capabilities.hosts[0].online = false;
  c.updateContinue(source);
  assert.equal(c.get("continue-open").disabled, true);
  assert.match(c.get("continue-note").textContent, /열린 목록/);
  c.state.filters.project = "temporarily-missing";
  c.renderFilters([]);
  const field = c.get("filter-fields").children.find((item) => item.children[0].id === "filter-project");
  assert.equal(field.children[0].value, "temporarily-missing");
  assert.equal(field.children[0].children.some((item) => item.value === "temporarily-missing"), true);
});

test("a late compositionend restores the new terminal draft and unload warns only for unsent text", async () => {
  const c = client();
  await c.openTerminal({ id: "old" });
  c.get("terminal-draft").value = "이전 조합";
  c.startDraftComposition();
  c.state.drafts.set("new", "새 터미널의 초안");
  await c.openTerminal({ id: "new" });
  c.get("terminal-draft").value = "이전 조합의 늦은 완료";
  c.endDraftComposition();
  assert.equal(c.get("terminal-draft").value, "새 터미널의 초안");
  assert.equal(c.state.drafts.get("new"), "새 터미널의 초안");
  let warnings = 0;
  c.warnDraftUnload({ preventDefault() { warnings++; } });
  assert.equal(warnings, 1);
  c.state.drafts.clear();
  c.get("terminal-draft").value = "";
  c.warnDraftUnload({ preventDefault() { warnings++; } });
  assert.equal(warnings, 1);
});

test("switching during IME replaces its control so late final input cannot edit the new draft", async () => {
  const c = client();
  await c.openTerminal({ id: "old" });
  const previous = c.get("terminal-draft");
  previous.value = "이전 조합";
  previous.cloneNode = () => node();
  previous.replaceWith = (replacement) => c.nodes.set("terminal-draft", replacement);
  c.startDraftComposition({ target: previous });
  c.state.drafts.set("new", "새 초안");
  await c.openTerminal({ id: "new" });
  assert.notEqual(c.get("terminal-draft"), previous);
  previous.value = "늦은 IME 완성";
  c.endDraftComposition({ target: previous });
  c.inputDraft({ target: previous, isComposing: false, inputType: "insertText" });
  assert.equal(c.get("terminal-draft").value, "새 초안");
  assert.equal(c.state.drafts.get("new"), "새 초안");
});

test("duplicate plan preparation focuses existing confirmation and never starts another request", async () => {
  let requests = 0;
  const c = client(async () => { requests++; throw new Error("must not call"); });
  c.state.snapshot = { hosts: [{ name: "workstation", data: { codex: [{ id: "source", agent: "codex" }] } }] };
  c.state.selected = "workstation|codex||source";
  c.state.route = { host: "workstation", agent: "codex", account: "default" };
  c.get("plan-dialog").open = true;
  await c.preparePlan();
  assert.equal(requests, 0);
  assert.equal(c.get("plan-cancel").focusCount, 1);
});

test("terminal reconnect preserves epoch and handles reset output in one response", async () => {
  const calls = [];
  const output = [];
  let reset = 0;
  const c = client(async (url) => {
    calls.push(url);
    return {
      ok: true,
      status: 200,
      json: async () =>
        url.includes("/events")
          ? {
              after: 3,
              data: Buffer.from("가나다").toString("base64"),
              reset: true,
              alive: false,
              epoch: "new-epoch",
            }
          : { terminals: [] },
    };
  });
  c.state.terminal = { id: "term-1" };
  c.state.terminalVisible = true;
  c.state.terminalAfter = 42;
  c.state.terminalEpoch = "old-epoch";
  c.state.term = {
    reset() {
      reset++;
    },
    write(bytes, done) {
      output.push(Buffer.from(bytes).toString());
      done();
    },
  };
  await c.startTerminalReader();
  assert.match(calls[0], /after=42&wait=20&epoch=old-epoch/);
  assert.equal(reset, 1);
  assert.deepEqual(output, ["가나다"]);
  assert.equal(c.state.terminalAfter, 3);
  assert.equal(c.state.terminalEpoch, "new-epoch");
  assert.equal(c.state.connected, false);
});

test("stop and resume during an xterm write callback continue after queued output once", async () => {
  const calls = [], output = [];
  let release, events = 0;
  const c = client(async (url) => {
    calls.push(url);
    const result = url.includes("/events")
      ? (++events === 1
        ? { after: 3, data: Buffer.from("가").toString("base64"), alive: true, epoch: "epoch-1" }
        : { after: 6, data: Buffer.from("나").toString("base64"), alive: false, epoch: "epoch-1" })
      : { terminals: [] };
    return { ok: true, status: 200, json: async () => result };
  });
  c.state.terminal = { id: "term-1" };
  c.state.terminalVisible = true;
  c.state.term = {
    write(bytes, done) {
      output.push(Buffer.from(bytes).toString());
      if (output.length === 1) release = done;
      else done();
    },
  };
  const first = c.startTerminalReader();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.state.terminalAfter, 3);
  assert.equal(c.state.terminalEpoch, "epoch-1");
  c.stopTerminalReader();
  await c.startTerminalReader();
  release();
  await first;
  const requests = calls.filter((url) => url.includes("/events"));
  assert.equal(requests.length, 2);
  assert.match(requests[1], /after=3&wait=20&epoch=epoch-1/);
  assert.deepEqual(output, ["가", "나"]);
  assert.equal(c.state.terminalAfter, 6);
});

test("launching plan cannot be dismissed into an uncertain execution state", () => {
  const c = client();
  c.state.launchBusy = true;
  c.get("plan-dialog").open = true;
  c.closePlan();
  assert.equal(c.get("plan-dialog").open, true);
});

test("service worker refuses session, login, terminal, and query-bearing cache requests", () => {
  const listeners = {};
  let intercepted = 0;
  const self = {
    location: { origin: "https://board.example" },
    addEventListener: (name, handler) => {
      listeners[name] = handler;
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../web/sw.js"), "utf8"),
    {
      self,
      URL,
      Set,
      fetch: async () => ({ ok: false }),
      caches: {},
      Response,
    },
  );
  for (const route of [
    "/api/snapshot",
    "/api/read",
    "/api/terminal/a/events",
    "/terminal/a",
    "/login",
    "/login?once=secret",
    "/logout",
    "/?token=secret",
    "/unknown.html",
    "/app.js?v=private",
  ]) {
    listeners.fetch({
      request: { method: "GET", url: `https://board.example${route}` },
      respondWith() {
        intercepted++;
      },
    });
  }
  assert.equal(intercepted, 0);
  listeners.fetch({
    request: { method: "GET", url: "https://board.example/app.js" },
    respondWith() {
      intercepted++;
    },
  });
  assert.equal(intercepted, 1);
});

test("browser history contains only the view, and Android back returns one screen", () => {
  const c = client();
  c.navigateView("list", true);
  c.navigateView("detail");
  assert.equal(c.context.window.history.state.view, "detail");
  assert.deepEqual(Object.keys(c.context.window.history.state).sort(), [
    "sessionholic",
    "view",
  ]);
  c.backToList();
  assert.equal(c.context.window.history.backCount, 1);
  c.navigateView("terminal");
  c.backFromTerminal();
  assert.equal(c.context.window.history.backCount, 2);
});

test("oversized Korean draft is retained and not sent over the input limit", async () => {
  let calls = 0;
  const c = client(async () => {
    calls++;
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: "term-1" };
  c.state.term = { modes: { bracketedPasteMode: true } };
  const draft = "가".repeat(12000);
  c.get("terminal-draft").value = draft;
  await c.pasteDraft();
  assert.equal(calls, 0);
  assert.equal(c.get("terminal-draft").value, draft);
});


test("empty lists distinguish filtered results, offline hosts, and pending collection", () => {
  const c = client();
  c.state.snapshot = { sessions: [], hosts: [{ ok: false }] };
  c.renderGroups([], Date.now());
  assert.match(textOf(c.get("groups")), /기기에 연결되지/);
  assert.match(textOf(c.get("groups")), /다시 확인/);
  c.state.search = "없는 작업";
  c.state.filters.host = "homeserver";
  c.renderGroups([], Date.now());
  assert.match(textOf(c.get("groups")), /조건에 맞는 작업/);
  c.get("groups").children[0].children[1].events.click();
  assert.equal(c.state.search, "");
  assert.equal(c.state.filters.host, "");
  assert.equal(c.get("search").value, "");
  c.state.snapshot.hosts[0].refreshing = true;
  c.renderGroups([], Date.now());
  assert.match(textOf(c.get("groups")), /확인하고 있습니다/);
});

test("conversation retry reloads its selected source and ignores a switched selection", async () => {
  const calls = [];
  const c = client(async (url) => {
    calls.push(url);
    return { ok: true, status: 200, json: async () => ({ messages: [] }) };
  });
  const session = { key: "homeserver|codex||session-id", host: "homeserver", agent: "codex", id: "session-id" };
  c.state.snapshot = { hosts: [{ name: "homeserver", label: "Home server", ok: true, data: { codex: [session] } }] };
  c.state.selected = session.key;
  c.state.authenticated = true;
  c.renderMessages(session, [], "연결 실패");
  const retry = c.get("messages").children[0].children[1];
  retry.events.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.match(calls[0], /\/api\/read\?/);
  assert.match(textOf(c.get("messages")), /저장된 대화를 아직/);
  c.state.selected = "another-source";
  retry.events.click();
  assert.equal(calls.length, 1);
});

function selectChat(c, { home = '.codex-example', id = 'native-session', agent = 'codex' } = {}) {
  const s = { host: 'workstation', agent, home, id, key: ['workstation', agent, home, id].join('|') };
  c.state.snapshot = { hosts: [{ name: 'workstation', label: 'Workstation', ok: true, data: { [agent]: [s] } }] };
  c.state.selected = s.key;
  c.state.authenticated = true;
  const entry = c.chatEntry(s);
  entry.data = { capability: { supported: true }, phase: 'idle', route: c.sourceOf(s) };
  c.get('chat-composer').dataset.source = c.chatKey(entry.source);
  return { s, entry };
}

const noChatTimers = { setTimeout: () => 1, clearTimeout() {} };

test('native chat drafts are isolated by exact host, agent, home and session', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.text = '첫 프로필 초안';
  assert.equal(c.chatEntry({ ...s, home: '.codex-secondary' }).text, '');
  assert.equal(c.chatEntry({ ...s, host: 'homeserver' }).text, '');
  assert.equal(c.chatEntry({ ...s, id: 'another-session' }).text, '');
  assert.equal(c.chatEntry(s).text, '첫 프로필 초안');
  entry.data.route.home = '.codex-secondary';
  assert.equal(c.chatSupported(entry), false);
});

test('native send captures the actual source and retains a switched session draft', async () => {
  let resolve;
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return await new Promise((done) => { resolve = done; });
  }, noChatTimers);
  const { entry } = selectChat(c);
  entry.text = '첫 프로필 세션에 보내기';
  entry.attachments = [{ id: 'same-source-attachment' }];
  c.state.route = { host: 'homeserver', agent: 'claude', account: 'claude:default' };
  const pending = c.sendChat(entry);
  const { entry: other } = selectChat(c, { home: '.codex-secondary' });
  other.text = '다른 계정의 초안';
  resolve({ ok: true, status: 200, json: async () => ({ status: 'accepted' }) });
  await pending;
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.source.home, '.codex-example');
  assert.equal(requests[0].body.source.host, 'workstation');
  assert.deepEqual(requests[0].body.attachments, ['same-source-attachment']);
  assert.equal(entry.text, '');
  assert.equal(other.text, '다른 계정의 초안');
  assert.equal(entry.receipt.status, 'accepted');
});

test('a lost chat acknowledgement keeps the request id and never resends an unknown request', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    if (url === '/api/chat/send') throw new Error('network timeout');
    return { ok: true, status: 200, json: async () => ({
      capability: { supported: true }, phase: 'working', route: requests[0].body.source, receipts: [],
    }) };
  }, noChatTimers);
  const { entry } = selectChat(c);
  entry.text = '중복되면 안 되는 요청';
  await c.sendChat(entry);
  const requestId = entry.pending.requestId;
  await c.sendChat(entry);
  assert.equal(requests.filter((r) => r.url === '/api/chat/send').length, 1);
  assert.equal(entry.text, '중복되면 안 되는 요청');
  assert.equal(entry.pending.requestId, requestId);
  assert.equal(requests.find((r) => r.url === '/api/chat/state').body.requestId, requestId);
  c.applyChatReceipt(entry, { requestId: 'wrong-request', status: 'accepted' });
  assert.equal(entry.pending.requestId, requestId);
  c.applyChatReceipt(entry, { requestId, status: 'queued' });
  assert.equal(entry.pending, null);
  assert.equal(entry.text, '');
  assert.equal(entry.receipt.status, 'queued');
});

test('IME and mismatched native routes block chat submission', async () => {
  let sends = 0;
  const c = client(async () => { sends++; throw new Error('should not send'); }, noChatTimers);
  const { entry } = selectChat(c);
  entry.text = '조합 중';
  entry.composing = true;
  await c.sendChat(entry);
  entry.composing = false;
  entry.data.route = { ...entry.source, id: 'wrong-native-id' };
  await c.sendChat(entry);
  assert.equal(sends, 0);
});

test('native attachment upload binds raw bytes to its exact source and rejects oversized files locally', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ attachment: {
      id: 'uploaded-native-file', name: '한글.png', size: 5, type: 'image/png', path: '/actual/cwd/.attachments/한글.png',
    } }) };
  }, noChatTimers);
  const { entry } = selectChat(c);
  const file = { name: '한글.png', size: 5, type: 'image/png' };
  await c.uploadChatFiles(entry, [file, { name: 'large.bin', size: 21 * 1024 * 1024 }]);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/chat/upload');
  assert.equal(requests[0].options.body, file);
  assert.equal(requests[0].options.headers['X-CSRF-Token'], 'csrf-test');
  const source = JSON.parse(decodeURIComponent(requests[0].options.headers['X-Chat-Source']));
  assert.equal(source.home, '.codex-example');
  assert.equal(source.id, 'native-session');
  assert.equal(decodeURIComponent(requests[0].options.headers['X-File-Name']), '한글.png');
  assert.equal(entry.attachments[0].id, 'uploaded-native-file');
  assert.match(entry.error, /최대/);
  assert.equal(entry.uploads, 0);
});

test('Claude terminal fallback permits same-session attachment paths but blocks unsupported direct send', async () => {
  const c = client(undefined, noChatTimers);
  const { entry } = selectChat(c, { agent: 'claude', home: '' });
  entry.data.capability.supported = false;
  assert.equal(c.chatSupported(entry), false);
  assert.equal(c.chatAttachmentsSupported(entry), true);
  entry.data.phase = 'unavailable';
  assert.equal(c.chatAttachmentsSupported(entry), false);
});

test('selected native polling has a fixed request budget and stops while hidden or in terminal', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {} });
  const { s, entry } = selectChat(c);
  entry.data.phase = 'working';
  c.scheduleChatPoll(s, entry);
  assert.equal(timers.at(-1).delay, 3000);
  const count = timers.length;
  c.state.chatPollCount = 40;
  c.scheduleChatPoll(s, entry);
  assert.equal(timers.length, count);
  c.state.chatPollCount = 0;
  c.state.terminalVisible = true;
  c.scheduleChatPoll(s, entry);
  assert.equal(timers.length, count);
});

test('accepted native receipts are read back until completion and interrupted pending work unlocks', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body); requests.push(body);
    return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'idle', route: body.source,
      receipts: [{ requestId: body.requestId, status: 'completed' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.receipt = { requestId: 'accepted-request-id', status: 'accepted' };
  await c.loadChatState(s, true);
  assert.equal(requests[0].requestId, 'accepted-request-id');
  assert.equal(entry.receipt.status, 'completed');
  entry.text = '중단된 요청';
  entry.pending = { requestId: 'interrupted-request-id', revision: entry.revision };
  c.applyChatReceipt(entry, { requestId: 'interrupted-request-id', status: 'interrupted' });
  assert.equal(entry.pending, null);
  assert.equal(entry.text, '');
  assert.equal(entry.receipt.status, 'interrupted');
});

test('native chat input limits are enforced before any uncertain transport is started', async () => {
  let requests = 0;
  const c = client(async () => { requests++; throw new Error('must remain local'); }, noChatTimers);
  const { entry } = selectChat(c);
  entry.text = '한'.repeat(11000);
  await c.sendChat(entry);
  assert.equal(entry.pending, null);
  assert.equal(entry.text.length, 11000);
  assert.match(entry.error, /32KiB/);
  entry.text = '';
  await c.uploadChatFiles(entry, Array.from({ length: 9 }, (_, i) => ({ name: i + '.txt', size: 1 })));
  assert.match(entry.error, /8개/);
  assert.equal(requests, 0);
});

test('an explicit native destination opens a synthetic exact-source row without changing the original draft', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    const body = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'idle', route: body.source, messages: [] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.text = '원본에만 남을 초안';
  const nativeSource = { host: 'homeserver', agent: 'codex', home: '.codex-example', id: 'new-native-id', cwd: '/actual/new/cwd' };
  c.state.terminal = { id: 'board-terminal', title: '옮긴 작업', metadata: { nativeSource, mode: 'attach', launchMode: 'transfer' } };
  assert.equal(c.state.selected, s.key);
  c.openNativeChat(c.state.terminal);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.state.selected, 'homeserver|codex|.codex-example|new-native-id');
  const selected = c.allSessions().find((row) => row.key === c.state.selected);
  assert.equal(selected.nativeSource, true);
  assert.equal(selected.cwd, '/actual/new/cwd');
  assert.equal(c.chatEntry(selected).text, '');
  assert.equal(entry.text, '원본에만 남을 초안');
  assert.ok(requests.includes('/api/chat/state'));
  assert.ok(!requests.some((url) => url.startsWith('/api/read')));
});

test('environment changes live in a native disclosure that starts closed and retains the existing controls', () => {
  const c = client(undefined, noChatTimers);
  const details = c.buildContinuePanel({ host: 'workstation', agent: 'codex', home: '.codex-example', id: 'session', key: 'source-key' });
  assert.equal(details.tagName, 'DETAILS');
  assert.equal(details.className, 'continue-disclosure');
  assert.ok(!details.open);
  assert.equal(details.hidden, false);
  assert.equal(details.children[0].tagName, 'SUMMARY');
  assert.equal(details.children[0].textContent, '다른 기기·계정에서 이어가기');
  const panel = details.children[1];
  assert.equal(panel.className, 'continue-panel');
  const controls = panel.children[1].children.map((field) => field.children[1].id);
  assert.deepEqual(controls, ['route-host', 'route-agent', 'route-account']);
  assert.equal(panel.children[2].children[1].id, 'continue-open');
  assert.equal(typeof panel.children[2].children[1].events.click, 'function');
});

test('a confirmed native turn failure clears its submitted draft while an unaccepted failure preserves it', () => {
  const c = client(undefined, noChatTimers);
  const { entry } = selectChat(c);
  entry.text = '이미 접수된 요청';
  entry.attachments = [{ id: 'attached-file' }];
  entry.pending = { requestId: 'confirmed-failed-id', revision: entry.revision };
  c.applyChatReceipt(entry, { requestId: 'confirmed-failed-id', status: 'failed', confirmed: true });
  assert.equal(entry.text, '');
  assert.equal(entry.attachments.length, 0);
  assert.equal(entry.pending, null);
  assert.match(c.chatReceiptCopy(entry.receipt), /처리 중 오류/);
  assert.doesNotMatch(c.chatReceiptCopy(entry.receipt), /접수하지 못/);
  entry.text = '미접수 요청';
  entry.pending = { requestId: 'unaccepted-failed-id', revision: entry.revision };
  c.applyChatReceipt(entry, { requestId: 'unaccepted-failed-id', status: 'failed', confirmed: false });
  assert.equal(entry.text, '미접수 요청');
  assert.equal(entry.pending, null);
  assert.match(c.chatReceiptCopy(entry.receipt), /초안은 유지/);
});

test('switching away and back resets chat IME ownership and ignores the detached editor final input', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body); requests.push({ url, body });
    return { ok: true, status: 200, json: async () => url === '/api/chat/send' ? { status: 'accepted' } :
      { capability: { supported: true }, phase: 'idle', route: body.source } };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  const oldComposer = c.buildChatComposer(s);
  const oldInput = findId(oldComposer, 'chat-input');
  oldInput.events.compositionstart();
  oldInput.value = '조합 중인 원본 초안';
  oldInput.events.input();
  assert.equal(entry.composing, true);
  const oldEpoch = entry.editorEpoch;
  selectChat(c, { home: '.codex-secondary' });
  oldInput.value = '다른 세션에 있을 때 온 이벤트';
  oldInput.events.input();
  assert.equal(entry.text, '조합 중인 원본 초안');
  selectChat(c);
  const newComposer = c.buildChatComposer(s);
  const newInput = findId(newComposer, 'chat-input');
  assert.equal(entry.editorEpoch, oldEpoch + 1);
  assert.equal(entry.composing, false);
  assert.equal(newInput.value, '조합 중인 원본 초안');
  newInput.value = '다시 돌아와 입력한 한글';
  newInput.events.compositionstart();
  newInput.events.input();
  const revision = entry.revision;
  oldInput.value = '늦게 도착한 옛 한글';
  oldInput.events.compositionend();
  oldInput.events.compositionstart();
  oldInput.events.input();
  oldInput.events.paste({ clipboardData: { files: [], getData: () => '옛 붙여넣기' }, preventDefault() {} });
  oldInput.events.keydown({ key: 'Enter', ctrlKey: true, preventDefault() {} });
  const oldSend = findId(oldComposer, 'chat-send');
  oldSend.events.click();
  assert.equal(entry.revision, revision);
  assert.equal(entry.text, '다시 돌아와 입력한 한글');
  assert.equal(entry.composing, true);
  newInput.events.keydown({ key: 'Enter', ctrlKey: true, isComposing: false, preventDefault() {} });
  assert.equal(requests.filter((r) => r.url === '/api/chat/send').length, 0);
  newInput.events.compositionend();
  newInput.events.keydown({ key: 'Enter', metaKey: true, isComposing: false, preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));
  const sends = requests.filter((r) => r.url === '/api/chat/send');
  assert.equal(sends.length, 1);
  assert.equal(sends[0].body.text, '다시 돌아와 입력한 한글');
  assert.equal(sends[0].body.source.home, '.codex-example');
});

test('a delayed clipboard read cannot replace a remounted chat editor draft', async () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  let resolveClipboard;
  c.context.navigator = { clipboard: { read: () => new Promise((resolve) => { resolveClipboard = resolve; }) } };
  const oldComposer = c.buildChatComposer(s);
  findId(oldComposer, 'chat-clipboard').events.click();
  const newComposer = c.buildChatComposer(s);
  const newInput = findId(newComposer, 'chat-input');
  newInput.value = '새 입력창의 초안';
  newInput.events.input();
  resolveClipboard([{ types: ['text/plain'], getType: async () => ({ text: async () => '이전 입력창의 클립보드' }) }]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.text, '새 입력창의 초안');
  assert.equal(newInput.value, '새 입력창의 초안');
});

test('returning from an explicitly launched transfer selects the actual destination and keeps source drafts separate', async () => {
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'idle', route: body.source }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.text = '원본 기기에 보관할 미전송 초안';
  const nativeSource = { host: 'homeserver', agent: 'codex', home: '.codex-example', id: 'launched-destination', cwd: '/actual/destination' };
  c.state.terminal = { id: 'transferred-terminal', host: 'homeserver', title: '새 기기에서 이어가는 작업',
    metadata: { nativeSource, mode: 'attach', launchMode: 'transfer' } };
  c.state.terminalVisible = true;
  c.returnToBoard();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.state.terminalVisible, false);
  assert.equal(c.state.selected, 'homeserver|codex|.codex-example|launched-destination');
  const destination = c.allSessions().find((row) => row.key === c.state.selected);
  assert.equal(c.chatEntry(destination).source.host, 'homeserver');
  assert.equal(c.chatEntry(destination).text, '');
  assert.equal(entry.text, '원본 기기에 보관할 미전송 초안');
  assert.equal(c.chatEntry(s), entry);
  // A plain attachment's return never changes an independently selected source.
  c.state.selected = s.key;
  c.state.terminal.metadata = { nativeSource, mode: 'attach' };
  c.state.terminalVisible = true;
  c.returnToBoard();
  assert.equal(c.state.selected, s.key);
});

test('compact chat expands and minimizes the same focused editor without resetting its IME or draft', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.text = '보관할 초안';
  const composer = c.buildChatComposer(s);
  const input = findId(composer, 'chat-input');
  const toggle = findId(composer, 'chat-expand');
  const heading = composer.children.find((child) => child.className === 'chat-heading');
  const hint = findId(composer, 'chat-hint');
  const epoch = entry.editorEpoch;
  assert.equal(input.rows, 1);
  assert.equal(heading.hidden, true);
  assert.equal(hint.hidden, true);
  assert.equal(toggle.attributes['aria-expanded'], 'false');
  input.events.compositionstart();
  input.value = '조합 중인 한글 초안'; input.events.input();
  let prevented = 0;
  toggle.events.pointerdown({ preventDefault() { prevented++; } });
  toggle.events.click();
  assert.equal(prevented, 1);
  assert.equal(findId(composer, 'chat-input'), input);
  assert.equal(input.rows, 3);
  assert.equal(entry.composing, true);
  assert.equal(entry.editorEpoch, epoch);
  assert.equal(heading.hidden, false);
  toggle.events.click();
  assert.equal(input.rows, 1);
  assert.equal(entry.composing, true);
  assert.equal(entry.text, '조합 중인 한글 초안');
  input.events.compositionend();
  assert.equal(entry.composing, false);
  assert.equal(entry.text, '조합 중인 한글 초안');
  const tools = composer.children.find((child) => child.className === 'chat-toolbar').children.find((child) => child.className === 'chat-tools');
  assert.equal(tools.tagName, 'DETAILS');
  assert.ok(!tools.open);
  assert.ok(findId(tools, 'chat-clipboard'));
  assert.ok(findId(tools, 'chat-check'));
  assert.ok(findId(composer, 'chat-attach'));
  assert.ok(findId(composer, 'chat-send'));
});

test('mobile text clipboard fallback is bounded and unavailable reads retain guidance without opening a delayed picker', async () => {
  const c = client(undefined, noChatTimers);
  const { entry } = selectChat(c);
  const input = c.get('chat-input');
  const files = c.get('chat-files');
  input.value = '초안: '; let clicks = 0; files.click = () => { clicks++; };
  let reads = 0;
  c.context.navigator = { clipboard: { readText: async () => { reads++; return '한글 클립보드'; } } };
  await c.readChatClipboard(entry, input, files);
  assert.equal(reads, 1);
  assert.equal(entry.text, '초안: 한글 클립보드');
  assert.match(entry.notice, /텍스트를 붙여넣/);
  assert.equal(clicks, 0);
  const denied = Object.assign(new Error('private browser detail'), { name: 'NotAllowedError' });
  let richReads = 0; reads = 0;
  c.context.navigator = { clipboard: { read: async () => { richReads++; throw denied; }, readText: async () => { reads++; throw denied; } } };
  await c.readChatClipboard(entry, input, files);
  assert.equal(richReads, 1);
  assert.equal(reads, 1);
  assert.equal(clicks, 0);
  assert.match(entry.notice, /허용하지 않았/);
  assert.match(entry.notice, /길게 눌러/);
  assert.match(c.get('chat-status').textContent, /첨부/);
  assert.equal(entry.error, '');
  assert.doesNotMatch(entry.notice, /private browser detail/);
});

test('clipboard images and paste-event file items upload to their exact session without dropping accompanying text', async () => {
  const uploads = [];
  const c = client(async (url, options) => {
    if (url === '/api/chat/upload') uploads.push(options);
    return { ok: true, status: 200, json: async () => ({ attachment: { id: 'image-' + uploads.length, name: 'photo.png', size: 12, type: 'image/png' } }) };
  }, noChatTimers);
  const { entry } = selectChat(c);
  const input = c.get('chat-input');
  const files = c.get('chat-files');
  c.context.File = class ClipboardFile {
    constructor(parts, name, options) { this.parts = parts; this.name = name; this.type = options.type; this.size = parts[0].size; }
  };
  c.context.navigator = { clipboard: { read: async () => [{ types: ['text/plain', 'image/png'], getType: async (type) =>
    type === 'text/plain' ? { text: async () => '사진 설명' } : { size: 12, type: 'image/png' } }] } };
  await c.readChatClipboard(entry, input, files);
  assert.equal(entry.text, '사진 설명');
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].body.type, 'image/png');
  assert.match(decodeURIComponent(uploads[0].headers['X-File-Name']), /\.png$/);
  const image = { name: '휴대폰 사진.jpg', size: 15, type: 'image/jpeg' };
  let prevented = 0;
  c.pasteChat(entry, { clipboardData: { files: [], items: [{ kind: 'file', getAsFile: () => image }], getData: () => '\n붙여넣은 사진' }, preventDefault() { prevented++; } }, input);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(prevented, 1);
  assert.equal(uploads.length, 2);
  assert.equal(uploads[1].body, image);
  assert.equal(JSON.parse(decodeURIComponent(uploads[1].headers['X-Chat-Source'])).id, entry.source.id);
  assert.match(entry.text, /붙여넣은 사진/);
  assert.equal(entry.attachments.length, 2);
});

test('account settings group named environments without merging identities or implying verified login', () => {
  const c = client(undefined, noChatTimers);
  const personal = { id: 'codex:.codex-lab-sample', label: '실험 환경 · sample', scope: 'lab',
    scopeLabel: '실험 환경', accountKey: 'sample', accountLabel: 'sample', home: '.codex-lab-sample', identityVerified: false, available: true };
  const standard = { ...personal, id: 'codex:.codex-sample', label: '공용 환경 · sample', scope: 'shared', scopeLabel: '공용 환경', home: '.codex-sample' };
  c.state.capabilities = { terminal: { available: true }, hosts: [{ name: 'workstation', label: 'Workstation', online: true,
    agents: [{ id: 'codex', accounts: [personal, standard] }] }] };
  const { s } = selectChat(c, { home: '.codex-lab-sample' });
  c.state.route = { host: 'workstation', agent: 'codex', account: personal.id };
  c.populateAccount(s);
  const select = c.get('route-account');
  assert.equal(select.value, personal.id);
  assert.deepEqual(select.children.map((child) => child.tagName), ['OPTGROUP', 'OPTGROUP']);
  assert.deepEqual(select.children.map((child) => child.label), ['실험 환경', '공용 환경']);
  const options = select.children.flatMap((group) => group.children);
  assert.deepEqual(options.map((option) => option.value), [personal.id, standard.id]);
  assert.match(options[0].textContent, /실험 환경 · sample · 현재 사용 중/);
  assert.equal(options[1].textContent, '공용 환경 · sample');
  assert.doesNotMatch(options.map((option) => option.textContent).join(' '), /인증 완료|로그인 확인/);
  assert.equal(c.sourceOf(s).home, personal.home);
  select.value = standard.id; select.onchange();
  assert.equal(c.state.route.account, standard.id);
  assert.equal(c.sourceOf(s).home, personal.home);
  assert.equal(c.allSessions()[0].account, personal.label);
  const disclosure = c.buildContinuePanel(s);
  assert.match(textOf(disclosure), /계정 설정/);
  assert.match(textOf(disclosure), /같은 이름도 로그인은 별도로 관리/);
});

test('native destination account display uses the exact profile label and never exposes its home as an account name', async () => {
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'idle', route: body.source }) };
  }, noChatTimers);
  selectChat(c);
  const profile = { id: 'codex:.codex-lab-sample', label: '실험 환경 · sample', scope: 'lab', scopeLabel: '실험 환경', identityVerified: false };
  c.state.capabilities = { hosts: [{ name: 'homeserver', label: 'Home server', agents: [{ id: 'codex', accounts: [profile] }] }] };
  const nativeSource = { host: 'homeserver', agent: 'codex', home: '.codex-lab-sample', id: 'display-native-id', cwd: '/actual/path' };
  c.openNativeChat({ id: 'native-terminal', metadata: { nativeSource, account: 'sample', title: '실제 작업' } });
  await new Promise((resolve) => setImmediate(resolve));
  const destination = c.allSessions().find((row) => row.key === c.state.selected);
  assert.equal(destination.account, profile.label);
  assert.equal(c.sessionAccountLabel(destination), profile.label);
  assert.equal(destination.home, '.codex-lab-sample');
  assert.equal(c.sourceOf(destination).home, '.codex-lab-sample');
  assert.equal(c.accountDisplay(null, '.codex-lab-sample'), '현재 계정 설정');
});

test('unsupported direct chat attachments explain persistent storage and path-copy terminal fallback', () => {
  const c = client(undefined, noChatTimers);
  const { entry } = selectChat(c, { agent: 'claude', home: '.claude' });
  entry.data.capability.supported = false;
  entry.attachments = [{ id: 'claude-file', name: '메모.txt', size: 12, path: '/actual/workspace/메모.txt' }];
  // Receipt reconciliation repaints the composer through its current-source guard.
  c.applyChatReceipt(entry, { requestId: 'known', status: 'accepted' });
  c.context.document.getElementById('chat-composer').dataset.source = c.chatKey(entry.source);
  vm.runInContext('updateChatComposer([...state.chats.values()][0])', c.context);
  assert.match(c.get('chat-status').textContent, /작업 폴더에 저장/);
  assert.match(c.get('chat-status').textContent, /경로.*터미널/);
  assert.equal(c.get('chat-send').disabled, true);
  assert.equal(c.get('chat-attach').disabled, false);
});

test('chat tools close on selection before clipboard access and dismiss outside or with non-composing Escape', async () => {
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'idle', route: body.source }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  const composer = c.buildChatComposer(s);
  const tools = findId(composer, 'chat-tools');
  const clipboard = findId(composer, 'chat-clipboard');
  c.nodes.set('chat-tools', tools);
  tools.contains = (target) => target === tools || target === clipboard;
  tools.open = true;
  c.closeChatToolsOutside({ target: clipboard });
  assert.equal(tools.open, true);
  c.closeChatToolsOutside({ target: {} });
  assert.equal(tools.open, false);
  tools.open = true;
  let prevented = 0;
  c.closeChatToolsEscape({ key: 'Escape', isComposing: true, preventDefault() { prevented++; } });
  assert.equal(tools.open, true);
  c.closeChatToolsEscape({ key: 'Escape', isComposing: false, preventDefault() { prevented++; } });
  assert.equal(tools.open, false);
  assert.equal(prevented, 1);
  const summary = tools.children[0];
  tools.querySelector = () => summary;
  c.context.document.activeElement = clipboard;
  tools.open = true;
  c.closeChatToolsEscape({ key: 'Escape', preventDefault() {} });
  assert.equal(summary.focusCount, 1);
  c.context.document.activeElement = findId(composer, 'chat-input');
  tools.open = true;
  c.closeChatToolsEscape({ key: 'Escape', preventDefault() {} });
  assert.equal(summary.focusCount, 1);
  const reads = [];
  c.context.navigator = { clipboard: { read: async () => {
    reads.push({ menuOpen: tools.open, text: entry.text });
    return [{ types: ['text/plain'], getType: async () => ({ text: async () => '메뉴에서 붙여넣기' }) }];
  } } };
  tools.open = true;
  clipboard.events.pointerdown({ preventDefault() { prevented++; } });
  clipboard.events.click();
  assert.equal(tools.open, false);
  assert.deepEqual(reads.map((read) => read.menuOpen), [false]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.text, '메뉴에서 붙여넣기');
  tools.open = true;
  findId(composer, 'chat-check').events.click();
  assert.equal(tools.open, false);
  tools.open = true;
  findId(composer, 'chat-native-open').events.click();
  assert.equal(tools.open, false);
});

test('clipboard tool preserves active Korean composition and never inserts text into its marked range', async () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  const composer = c.buildChatComposer(s);
  const input = findId(composer, 'chat-input');
  const tools = findId(composer, 'chat-tools');
  input.value = '조합 중인 한글';
  input.events.compositionstart(); input.events.input();
  let reads = 0;
  c.context.navigator = { clipboard: { read: async () => { reads++; return []; } } };
  tools.open = true;
  findId(composer, 'chat-clipboard').events.click();
  assert.equal(tools.open, false);
  assert.equal(reads, 0);
  assert.equal(entry.composing, true);
  assert.equal(entry.text, '조합 중인 한글');
  assert.match(entry.notice, /한글 입력을 마친 뒤/);
  input.events.compositionend();
  assert.equal(entry.composing, false);
});

function wireTree(c, root, parent = null) {
  if (root.id) c.nodes.set(root.id, root);
  root.parentNode = parent;
  root.querySelector = (selector) => {
    const className = selector.split(' ').at(-1).slice(1);
    const visit = (current) => {
      for (const child of current.children || []) {
        if ((child.className || '').split(' ').includes(className)) return child;
        const found = visit(child); if (found) return found;
      }
      return null;
    };
    return visit(root);
  };
  root.after = (child) => {
    const siblings = root.parentNode.children;
    siblings.splice(siblings.indexOf(root) + 1, 0, child);
    wireTree(c, child, root.parentNode);
  };
  root.remove = () => {
    if (root.parentNode) root.parentNode.children = root.parentNode.children.filter((child) => child !== root);
  };
  for (const child of root.children || []) wireTree(c, child, root);
  return root;
}

test('same-session metadata changes preserve the active chat DOM, Korean composition, draft and transcript position', async () => {
  let message = '기존 대화';
  const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/chat/state'
      ? { capability: { supported: true }, phase: 'idle', route: JSON.parse(options.body).source }
      : { messages: [{ role: 'assistant', text: message }] } };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  s.title = '원래 제목'; s.project = '원래 프로젝트'; s.updatedAt = Date.now() / 1000;
  c.state.snapshot.hosts[0].data.codex[0] = s;
  c.refreshDetail(s);
  wireTree(c, c.get('detail'));
  await new Promise((resolve) => setImmediate(resolve));
  const composer = c.get('chat-composer'); const input = c.get('chat-input');
  const epoch = entry.editorEpoch;
  input.value = '한글 입력 중인 초안'; input.events.compositionstart(); input.events.input();
  c.context.document.activeElement = input;
  const messages = c.get('messages');
  messages.scrollHeight = 500; messages.clientHeight = 120; messages.scrollTop = 35;
  message = '갱신된 대화';
  const updated = { ...s, title: '갱신된 제목', project: '새 프로젝트', phase: 'working',
    account: '실험 환경 · 새 표시', stale: true, hostLabel: 'Workstation', updatedAt: s.updatedAt + 1 };
  c.state.snapshot.hosts[0].data.codex[0] = updated;
  c.refreshDetail(updated);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.get('chat-composer'), composer);
  assert.equal(c.get('chat-input'), input);
  assert.equal(c.context.document.activeElement, input);
  assert.equal(entry.editorEpoch, epoch);
  assert.equal(entry.composing, true);
  assert.equal(entry.text, '한글 입력 중인 초안');
  assert.equal(c.get('detail').querySelector('.detail-title').textContent, '갱신된 제목');
  assert.equal(c.get('detail').querySelector('.eyebrow').textContent, '새 프로젝트');
  assert.match(textOf(c.get('detail').querySelector('.detail-meta')), /작업 중.*실험 환경/);
  assert.match(c.get('detail').querySelector('.stale-notice').textContent, /Workstation/);
  assert.equal(messages.scrollTop, 35);
  assert.match(textOf(messages), /갱신된 대화/);
  assert.equal(requests.filter((url) => url.startsWith('/api/read')).length, 2);
  input.events.compositionend();
  assert.equal(entry.composing, false);
});

test('capability refresh immediately updates account labels while preserving exact previously filtered profile routes', async () => {
  const now = Date.now() / 1000;
  const personal = { id: 'codex:.codex-lab-sample', label: '실험 환경 · sample', scope: 'lab', scopeLabel: '실험 환경', available: true };
  const standard = { id: 'codex:.codex-sample', label: '공용 환경 · sample', scope: 'shared', scopeLabel: '공용 환경', available: true };
  const other = { id: 'codex:.codex-other', label: personal.label, scope: 'lab', available: true };
  let profiles = [personal, standard, other];
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ csrfToken: 'test', terminal: { available: true },
    hosts: [{ name: 'workstation', label: 'Workstation', online: true, agents: [{ id: 'codex', accounts: profiles }] }] }) }), noChatTimers);
  const rows = [
    { id: 'personal', home: '.codex-lab-sample', title: '실험 설정 작업', account: 'sample' },
    { id: 'standard', home: '.codex-sample', title: '공용 설정 작업', account: 'sample' },
    { id: 'foreign', home: '.codex-other', title: '원래 필터와 무관한 작업', account: '다른 설정' },
  ].map((row) => ({ ...row, host: 'workstation', agent: 'codex', phase: 'idle', updatedAt: now }));
  c.state.snapshot = { hosts: [{ name: 'workstation', label: 'Workstation', ok: true, data: { codex: rows } }] };
  c.state.authenticated = true; c.state.filters.account = 'sample';
  await c.loadCapabilities();
  assert.deepEqual(Array.from(c.allSessions(), (row) => row.account), [personal.label, standard.label, personal.label]);
  assert.equal(c.state.filters.account, 'sample');
  assert.equal(c.state.accountFilterRoutes.size, 2);
  assert.match(textOf(c.get('groups')), /실험 환경 · sample/);
  assert.match(textOf(c.get('groups')), /공용 환경 · sample/);
  assert.doesNotMatch(textOf(c.get('groups')), /원래 필터와 무관한 작업/);
  assert.equal(c.get('session-count').textContent, '2개');
  profiles = [personal, { ...standard, label: personal.label }, other];
  await c.loadCapabilities();
  assert.equal(c.state.filters.account, personal.label);
  assert.equal(c.get('session-count').textContent, '2개');
  assert.doesNotMatch(textOf(c.get('groups')), /원래 필터와 무관한 작업/);
  const accountFilter = findId(c.get('filter-fields'), 'filter-account');
  accountFilter.value = personal.label;
  accountFilter.events.change();
  assert.equal(c.state.accountFilterRoutes, null);
  assert.equal(c.state.filters.account, personal.label);
});

test('failed conversation refresh keeps readable rows and retries without changing the reading position', async () => {
  let fail = true;
  const c = client(async () => {
    if (fail) throw new Error('읽기 연결 실패');
    return { ok: true, status: 200, json: async () => ({ messages: [{ role: 'assistant', text: '기존 대화' }] }) };
  }, noChatTimers);
  const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'assistant', text: '기존 대화' }]);
  const list = c.get('messages'); const row = list.children[0];
  list.scrollHeight = 900; list.clientHeight = 200; list.scrollTop = 45;
  await c.loadDetail(s);
  assert.equal(list.children[1], row);
  assert.match(textOf(list), /기존 대화/);
  assert.match(textOf(list), /읽기 연결 실패/);
  assert.equal(list.scrollTop, 45);
  fail = false;
  list.conversationError.children[1].events.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(list.children[0], row);
  assert.equal(list.children.length, 1);
  assert.equal(list.scrollTop, 45);
});

test('unchanged and appended conversations retain selected nodes and expanded tools; copy uses only the body', async () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const messages = [{ role: 'tool', text: '도구 본문' }, { role: 'assistant', text: '답변 본문', ts: 123 }];
  c.renderMessages(s, messages);
  const list = c.get('messages'); const tool = list.children[0]; const answer = list.children[1];
  tool.children[0].open = true;
  list.contains = (node) => node === answer;
  c.context.window.getSelection = () => ({ isCollapsed: false, anchorNode: answer, focusNode: answer });
  list.scrollHeight = 500; list.clientHeight = 100; list.scrollTop = 395;
  c.renderMessages(s, messages);
  c.renderMessages(s, [...messages, { role: 'user', text: '새 답변' }]);
  assert.equal(list.children[0], tool); assert.equal(list.children[1], answer);
  assert.equal(tool.children[0].open, true); assert.equal(list.scrollTop, 395);
  let copied;
  c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  await answer.children.at(-1).events.click();
  assert.equal(copied, '답변 본문');
  assert.equal(answer.children.at(-1).attributes['aria-label'], '메시지 본문 복사');
});

test('a delayed previous-session state success or failure cannot cancel the current polling timer', async () => {
  for (const failed of [false, true]) {
    let release; let timer = 0; const cancelled = [];
    const c = client(async (url, options) => {
      const route = JSON.parse(options.body).source;
      if (route.id === 'old') return await new Promise((resolve, reject) => {
        release = () => failed ? reject(new Error('이전 요청 실패')) : resolve({ ok: true, status: 200,
          json: async () => ({ capability: { supported: true }, phase: 'working', route }) });
      });
      return { ok: true, status: 200, json: async () => ({ capability: { supported: true }, phase: 'working', route }) };
    }, { setTimeout: () => ++timer, clearTimeout: (id) => cancelled.push(id) });
    const old = selectChat(c, { id: 'old' }); const pending = c.loadChatState(old.s);
    c.stopChatPolling();
    const current = selectChat(c, { id: 'current' }); await c.loadChatState(current.s);
    const currentTimer = c.state.chatPollTimer;
    release(); await pending;
    assert.equal(cancelled.includes(currentTimer), false);
    assert.equal(c.state.chatPollTimer, currentTimer);
  }
});

test('clipboard results arriving after IME, draft, or selection changes leave the current draft untouched', async () => {
  for (const change of ['composition', 'draft', 'selection']) {
    const c = client(undefined, noChatTimers); const { entry } = selectChat(c);
    const input = c.get('chat-input'); input.value = '한'; input.selectionStart = 0; input.selectionEnd = 1;
    let release;
    c.context.navigator = { clipboard: { readText: () => new Promise((resolve) => { release = resolve; }) } };
    const pending = c.readChatClipboard(entry, input, c.get('chat-files'));
    if (change === 'composition') entry.composing = true;
    if (change === 'draft') { input.value = entry.text = '새 초안'; entry.revision++; }
    if (change === 'selection') input.selectionEnd = 0;
    const retained = input.value;
    release('늦게 온 클립보드'); await pending;
    assert.equal(input.value, retained);
    assert.match(entry.notice, /바뀌어 클립보드를 붙이지 않았습니다/);
  }
});

test('offline drafts remain editable but send, uploads, clipboard reads and terminal input send no transport', async () => {
  let requests = 0; let reads = 0;
  const c = client(async () => { requests++; return { ok: true, status: 200, json: async () => ({}) }; }, noChatTimers);
  const { entry } = selectChat(c); entry.text = '오프라인 초안';
  c.context.navigator = { onLine: false, clipboard: { readText: async () => { reads++; return 'text'; } } };
  await c.sendChat(entry); await c.uploadChatFiles(entry, [{ name: 'offline.png', size: 1 }]);
  await c.readChatClipboard(entry, c.get('chat-input'), c.get('chat-files'));
  c.state.connected = true; c.state.terminal = { id: 'terminal-id' };
  await assert.rejects(c.sendInput('input'));
  assert.equal(requests, 0); assert.equal(reads, 0); assert.equal(entry.pending, null);
  assert.equal(entry.text, '오프라인 초안'); assert.equal(c.get('chat-input').disabled, false);
  assert.equal(c.get('chat-send').disabled, true); assert.match(c.get('chat-status').textContent, /오프라인.*초안/);
});

test('online recovery reads one selected state and snapshot without resending unknown work or reconnecting terminal input', async () => {
  const requests = [];
  let snapshot;
  const c = client(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => url === '/api/snapshot' ? snapshot :
      { route: JSON.parse(options.body).source, capability: { supported: true }, phase: 'idle' } };
  }, noChatTimers);
  const { s, entry } = selectChat(c); snapshot = c.state.snapshot;
  c.state.detailFor = s.key; c.state.detailUpdatedAt = s.updatedAt;
  c.state.detailRevision = JSON.stringify([false, undefined, undefined, undefined, '현재 계정 설정', 'Workstation', undefined]);
  c.state.capabilities = {}; c.state.capabilitiesRevision = JSON.stringify([['workstation', true, undefined]]);
  entry.pending = { requestId: 'unknown-request', revision: entry.revision }; entry.text = '전송 결과 모르는 초안';
  c.state.offline = true; c.context.navigator = { onLine: true };
  c.recoverConnection(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.filter((r) => r.url === '/api/snapshot').length, 1);
  assert.equal(requests.filter((r) => r.url === '/api/chat/state').length, 1);
  assert.equal(requests.some((r) => /chat\/send|chat\/upload|terminal\//.test(r.url)), false);
  assert.equal(JSON.parse(requests.find((r) => r.url === '/api/chat/state').options.body).requestId, 'unknown-request');
  assert.equal(entry.pending.requestId, 'unknown-request');
  c.state.terminalVisible = true; c.state.terminal = { id: 'visible-terminal' }; c.state.offline = true;
  const before = requests.length; c.recoverConnection();
  assert.equal(requests.length, before); assert.equal(c.state.connected, false);
  assert.match(c.get('terminal-status').textContent, /다시 연결/);
});

test('a state read begun before sending cannot roll back new chat state and schedules one reconciliation read', async () => {
  let releaseRead; let stateReads = 0;
  const c = client(async (url, options) => {
    const route = JSON.parse(options.body).source;
    if (url === '/api/chat/send') return { ok: true, status: 200, json: async () => ({ status: 'accepted' }) };
    stateReads++;
    if (stateReads === 1) return await new Promise((resolve) => { releaseRead = () => resolve({ ok: true, status: 200,
      json: async () => ({ route, capability: { supported: true }, phase: 'idle', messages: [{ role: 'assistant', text: '과거 읽기' }] }) }); });
    return { ok: true, status: 200, json: async () => ({ route, capability: { supported: true }, phase: 'working', messages: [{ role: 'assistant', text: '현재 읽기' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c); entry.text = '새 메시지';
  const pending = c.loadChatState(s); await c.sendChat(entry);
  assert.equal(entry.pending, null); assert.equal(entry.recheckQueued, true);
  releaseRead(); await pending; await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stateReads, 2); assert.equal(entry.data.phase, 'working');
  assert.match(textOf(c.get('messages')), /현재 읽기/); assert.doesNotMatch(textOf(c.get('messages')), /과거 읽기/);
});

test('an offline interruption stops remaining uploads and reports files that require explicit reattachment', async () => {
  let requests = 0;
  const c = client(async () => {
    requests++; c.context.navigator.onLine = false;
    return { ok: true, status: 200, json: async () => ({ attachment: { id: 'uploaded', name: 'first.png', size: 1 } }) };
  }, noChatTimers);
  const { entry } = selectChat(c); c.context.navigator = { onLine: true };
  await c.uploadChatFiles(entry, [{ name: 'first.png', size: 1 }, { name: 'second.png', size: 1 }, { name: 'third.png', size: 1 }]);
  assert.equal(requests, 1); assert.equal(entry.attachments.length, 1); assert.equal(entry.uploads, 0);
  assert.match(entry.notice, /파일 2개.*다시 첨부/);
});

test('terminal settings dismiss outside and with Escape while controls inside retain the menu', () => {
  const c = client(undefined, noChatTimers); const menu = c.get('terminal-settings'); const inside = node(); const summary = node();
  menu.open = true; menu.contains = (target) => target === inside; menu.querySelector = () => summary;
  c.closeChatToolsOutside({ target: inside }); assert.equal(menu.open, true);
  c.closeChatToolsOutside({ target: node() }); assert.equal(menu.open, false);
  menu.open = true; c.context.document.activeElement = inside;
  let prevented = 0;
  c.closeChatToolsEscape({ key: 'Escape', isComposing: true, preventDefault() { prevented++; } });
  assert.equal(menu.open, true);
  c.closeChatToolsEscape({ key: 'Escape', preventDefault() { prevented++; } });
  assert.equal(menu.open, false); assert.equal(summary.focusCount, 1); assert.equal(prevented, 1);
});

test('explicit mobile navigation focuses the back control and restores its source card without opening the keyboard', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c); const back = node(); const card = node();
  card.dataset.sessionKey = s.key;
  c.get('detail').querySelector = () => back; c.get('groups').querySelectorAll = () => [card];
  c.context.window.matchMedia = () => ({ matches: true });
  c.select(s.key);
  assert.equal(back.focusCount, 1); assert.equal(c.get('chat-input').focusCount, undefined);
  c.context.window.history.state = null; c.backToList();
  assert.equal(card.focusCount, 1);
  c.renderMessages(s, [{ role: 'assistant', text: '주기적 갱신' }]);
  assert.equal(back.focusCount, 1); assert.equal(card.focusCount, 1);
});

test('screen reader preference changes the existing xterm option and stores only the boolean UI preference', () => {
  const c = client(undefined, noChatTimers); const saved = [];
  c.context.navigator = { onLine: false };
  c.context.localStorage = { getItem: () => null, removeItem() {}, setItem: (key, value) => saved.push([key, value]) };
  c.windowEvents.DOMContentLoaded();
  const settings = c.get('terminal-settings'); settings.open = true;
  const terminal = { options: { screenReaderMode: false } }; c.state.term = terminal;
  const checkbox = c.get('terminal-accessibility');
  checkbox.events.change({ target: { checked: true } });
  assert.equal(c.state.term, terminal); assert.equal(terminal.options.screenReaderMode, true);
  assert.equal(c.state.screenReaderMode, true); assert.equal(settings.open, true);
  assert.deepEqual(saved, [['sessionholic:ui:screenReaderMode', 'true']]);
});

test('offline visibility and return-online events restart only bounded reads and never restore an uncertain write', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/snapshot' ? c.state.snapshot :
      { route: JSON.parse(options.body).source, capability: { supported: true }, phase: 'idle' } };
  }, noChatTimers);
  c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded();
  await new Promise((resolve) => setImmediate(resolve));
  const { s, entry } = selectChat(c);
  c.state.detailFor = s.key; c.state.detailUpdatedAt = s.updatedAt;
  c.state.detailRevision = JSON.stringify([false, undefined, undefined, undefined, '현재 계정 설정', 'Workstation', undefined]);
  c.state.capabilities = {}; c.state.capabilitiesRevision = JSON.stringify([['workstation', true, undefined]]);
  entry.text = '미전송 초안';
  c.windowEvents.offline(); assert.equal(c.state.offline, true);
  c.context.document.hidden = true; c.documentEvents.visibilitychange();
  c.context.document.hidden = false; c.documentEvents.visibilitychange();
  // A visibility restoration while still offline cannot start any transport.
  assert.equal(requests.length, 0);
  c.context.navigator.onLine = true; c.windowEvents.online();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/snapshot', '/api/chat/state']);
  assert.equal(entry.text, '미전송 초안'); assert.equal(entry.pending, null);
});

test('public application metadata uses Sessionholic consistently while retaining the established icons', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../web/manifest.webmanifest'), 'utf8'));
  assert.match(html, /<title>세션홀릭 - 에이전트 세션 매니저<\/title>/);
  assert.match(html, /name="application-name" content="Sessionholic"/);
  assert.match(html, /name="apple-mobile-web-app-title" content="세션홀릭"/);
  assert.equal(manifest.name, '세션홀릭 - 에이전트 세션 매니저');
  assert.equal(manifest.short_name, '세션홀릭'); assert.equal(manifest.id, '/sessionholic');
  assert.equal(manifest.icons.some((icon) => icon.src === '/icon-maskable-512.png' && icon.purpose === 'maskable'), true);
  assert.match(html, /서버 관리자에게 접속 토큰을 확인/);
  assert.doesNotMatch(html, /~\/\.config\//);
});

test('UI preferences and startup cleanup never read or remove another application namespace', () => {
  const reads = []; const removed = []; const writes = [];
  const c = client(undefined, noChatTimers);
  c.context.navigator = { onLine: false };
  c.context.localStorage = {
    getItem: (key) => { reads.push(key); return null; },
    removeItem: (key) => removed.push(key),
    setItem: (key, value) => writes.push([key, value]),
  };
  // Reading defaults is isolated as well as the already tested option write.
  vm.runInContext('prefs.get("fontSize", 14)', c.context);
  c.windowEvents.DOMContentLoaded();
  c.get('terminal-accessibility').events.change({ target: { checked: true } });
  assert.deepEqual(reads, ['sessionholic:ui:fontSize']);
  assert.deepEqual(removed, ['sessionholic:selected', 'sessionholic:filters', 'sessionholic:showOld']);
  assert.deepEqual(writes, [['sessionholic:ui:screenReaderMode', 'true']]);
});

test('arbitrary environment metadata determines account groups and descriptions without changing source identity', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c, { home: '.codex-example' });
  const profiles = [
    { id: 'codex:.codex-example', scope: 'research-team', scopeLabel: '연구 환경', accountLabel: 'Sample profile',
      scopeDescription: '이 환경은 연구 프로젝트용 실행 설정입니다.', available: true },
    { id: 'codex:.codex-secondary', scope: 'personal', scopeLabel: '자율 설정', label: 'Secondary profile', available: true },
  ];
  c.state.capabilities = { terminal: { available: true }, hosts: [{ name: 'workstation', online: true,
    agents: [{ id: 'codex', accounts: profiles }] }] };
  c.state.route = { host: 'workstation', agent: 'codex', account: profiles[0].id };
  c.populateAccount(s);
  assert.deepEqual(c.get('route-account').children.map((group) => group.label), ['연구 환경', '자율 설정']);
  assert.equal(c.get('route-account').value, profiles[0].id);
  assert.match(c.get('account-context').textContent, /연구 프로젝트용 실행 설정/);
  assert.equal(c.sourceOf(s).home, '.codex-example');
  assert.doesNotMatch(c.get('account-context').textContent, /인증 완료|로그인 확인/);
  profiles.forEach((profile) => { delete profile.scope; delete profile.scopeLabel; });
  c.populateAccount(s);
  assert.equal(c.get('route-account').children.some((child) => child.tagName === 'OPTGROUP'), false);
});

test('first-run, empty-session and unavailable-host views give the installer a concrete next action', () => {
  const c = client(undefined, noChatTimers);
  c.state.snapshot = { hosts: [] }; c.renderGroups([], Date.now() / 1000);
  assert.match(textOf(c.get('groups')), /기기가 아직 설정되지.*설치 안내.*새로고침/);
  c.state.snapshot = { hosts: [{ name: 'workstation', label: 'Workstation', ok: true }] };
  c.renderGroups([], Date.now() / 1000);
  assert.match(textOf(c.get('groups')), /Claude Code나 Codex로 대화를 시작.*새로고침/);
  c.state.snapshot.hosts[0].ok = false; c.renderGroups([], Date.now() / 1000);
  assert.match(textOf(c.get('groups')), /기기 연결과 에이전트 설치 상태.*다시 확인/);
  assert.equal(c.get('groups').children[0].children[1].textContent, '다시 확인');
});

test('service worker evicts only its own old shells and offline reads use the current Sessionholic cache', async () => {
  const listeners = {}; const deleted = []; const opened = []; let response;
  const self = { location: { origin: 'https://app.example' }, addEventListener: (type, handler) => { listeners[type] = handler; },
    clients: { claim: async () => {} } };
  const currentShell = new Response('Sessionholic shell');
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8'), {
    self, URL, Set, Response,
    fetch: async () => { throw new Error('offline'); },
    caches: {
      keys: async () => ['another-app-shell', 'sessionholic-shell-v0', 'sessionholic-shell-v1', 'sessionholic-shell-v2', 'sessionholic-shell-v3'],
      delete: async (key) => { deleted.push(key); },
      open: async (key) => { opened.push(key); return { match: async () => currentShell }; },
      match: async () => { throw new Error('Global cross-application cache lookup is forbidden'); },
    },
  });
  let activated;
  listeners.activate({ waitUntil: (promise) => { activated = promise; } }); await activated;
  assert.deepEqual(deleted, ['sessionholic-shell-v0', 'sessionholic-shell-v1', 'sessionholic-shell-v2']);
  listeners.fetch({ request: { method: 'GET', url: 'https://app.example/' },
    respondWith: (promise) => { response = promise; }, waitUntil() {} });
  assert.equal(await response, currentShell);
  assert.deepEqual(opened, ['sessionholic-shell-v3']);
});

test('an offline first launch restores authentication and already-open terminal rows without reopening their sessions', async () => {
  const requests = [];
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/snapshot'
      ? { hosts: [], serverTime: Date.now() / 1000 }
      : url === '/api/terminals' ? { terminals: [{ id: 'existing-terminal', alive: true,
        host: 'workstation', agent: 'codex', title: '이미 열린 작업' }] }
      : { hosts: [], csrfToken: 'test-csrf', authMode: 'token' } };
  }, noChatTimers);
  c.context.navigator = { onLine: false };
  c.get('app').hidden = true; c.get('login').hidden = true;
  c.windowEvents.DOMContentLoaded(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.get('login').hidden, false);
  assert.match(c.get('login-error').textContent, /오프라인.*연결/);
  assert.equal(c.get('login-submit').disabled, true);
  assert.equal(requests.length, 0); assert.equal(c.state.authenticated, false);
  c.get('login-form').events.submit({ preventDefault() {} });
  assert.equal(requests.length, 0);
  c.context.navigator.onLine = true; c.windowEvents.online();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/snapshot', '/api/capabilities', '/api/terminals']);
  assert.match(textOf(c.get('running-list')), /이미 열린 작업/);
  assert.equal(c.state.terminal, null); assert.equal(c.state.terminalVisible, false);
  assert.equal(c.get('app').hidden, false); assert.equal(c.get('login').hidden, true);
  assert.equal(c.get('login-submit').disabled, false);
  c.windowEvents.online(); await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, ['/api/snapshot', '/api/capabilities', '/api/terminals', '/api/snapshot']);
});

test('returning to a first-launch tab after online recovery while hidden reads login state without transmitting a token', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, method: options.method });
    return { ok: false, status: 401, json: async () => ({}) };
  }, noChatTimers);
  c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded();
  await new Promise((resolve) => setImmediate(resolve));
  c.context.document.hidden = true; c.documentEvents.visibilitychange();
  c.context.navigator.onLine = true; c.windowEvents.online();
  assert.equal(requests.length, 0);
  c.context.document.hidden = false; c.documentEvents.visibilitychange();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests, [{ url: '/api/snapshot', method: 'GET' }]);
  assert.equal(c.get('login').hidden, false); assert.equal(c.get('login-submit').disabled, false);
  assert.equal(c.get('app').hidden, true);
});

test('native file paste cannot replace an active Korean composition and leaves text-only paste to the browser', async () => {
  let uploads = 0;
  const c = client(async () => { uploads++; return { ok: true, status: 200, json: async () => ({ attachment: { id: 'new-image' } }) }; }, noChatTimers);
  const { entry } = selectChat(c); const input = c.get('chat-input');
  input.value = entry.text = '조합 중인 한글'; input.selectionStart = 4; input.selectionEnd = 7;
  entry.composing = true; const revision = entry.revision;
  let prevented = 0;
  c.pasteChat(entry, { clipboardData: { files: [{ name: 'image.png', size: 1 }], getData: () => '붙여넣은 텍스트' },
    preventDefault() { prevented++; } }, input);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(uploads, 0); assert.equal(prevented, 1);
  assert.equal(input.value, '조합 중인 한글'); assert.equal(entry.revision, revision);
  assert.equal(entry.composing, true); assert.match(entry.notice, /한글 입력.*마친 뒤/);
  c.pasteChat(entry, { clipboardData: { files: [], getData: () => '브라우저 텍스트' }, preventDefault() { prevented++; } }, input);
  assert.equal(prevented, 1); assert.equal(input.value, '조합 중인 한글');
});

test('native file paste during an upload preserves accompanying text and explains why its new files need to be pasted again', () => {
  const c = client(undefined, noChatTimers); const { entry } = selectChat(c);
  const input = c.get('chat-input'); input.value = entry.text = '기존 초안';
  input.selectionStart = input.selectionEnd = input.value.length;
  entry.uploads = 1;
  c.pasteChat(entry, { clipboardData: { files: [{ name: 'next.png', size: 1 }], getData: () => '와 붙여넣은 텍스트' },
    preventDefault() {} }, input);
  assert.equal(input.value, '기존 초안와 붙여넣은 텍스트');
  assert.equal(entry.text, input.value); assert.equal(entry.uploads, 1); assert.equal(entry.attachments.length, 0);
  assert.match(entry.notice, /첨부.*완료.*다시 붙여넣/);
});

test('locking invalidates delayed snapshot, capability and terminal reads without restoring private UI', async () => {
  for (const kind of ['snapshot', 'capabilities', 'terminals']) {
    let release;
    const c = client(async (url) => {
      if (url === '/logout') return { ok: true, status: 200, json: async () => ({ ok: true }) };
      return await new Promise((resolve) => { release = resolve; });
    }, noChatTimers);
    selectChat(c);
    c.get('app').hidden = false;
    const old = kind === 'snapshot' ? c.poll(false) : kind === 'capabilities' ? c.loadCapabilities() : c.refreshTerminals();
    // Capabilities intentionally rejects stale results; attach before settling.
    const settled = old.catch((err) => { assert.equal(err.staleAuth, true); });
    await c.lock();
    assert.equal(c.state.authenticated, false); assert.equal(c.get('app').hidden, true);
    release({ ok: true, status: 200, json: async () => ({
      hosts: [{ name: 'workstation', ok: true, data: { codex: [{ id: 'old', agent: 'codex', title: '과거 비공개 제목' }] } }],
      csrfToken: 'old-csrf', terminals: [{ id: 'old-terminal', title: '과거 터미널' }],
    }) });
    await settled;
    assert.equal(c.state.authenticated, false); assert.equal(c.get('app').hidden, true);
    assert.equal(c.state.snapshot, null); assert.equal(c.state.capabilities, null);
    assert.equal(c.state.csrf, ''); assert.equal(c.state.terminals.length, 0);
    assert.doesNotMatch(textOf(c.get('groups')), /과거 비공개 제목/);
  }
});

test('a delayed response body is also discarded when locking advances the authentication epoch', async () => {
  let releaseBody;
  const c = client(async () => ({ ok: true, status: 200, json: () => new Promise((resolve) => { releaseBody = resolve; }) }), noChatTimers);
  const pending = c.api('/api/capabilities').catch((err) => { assert.equal(err.staleAuth, true); });
  await new Promise((resolve) => setImmediate(resolve));
  c.showLogin();
  releaseBody({ csrfToken: 'old-csrf' }); await pending;
  assert.equal(c.state.authenticated, false); assert.equal(c.state.csrf, '');
});

test('late API and binary-upload 401 responses cannot lock a newly logged-in session or erase its new draft', async () => {
  for (const kind of ['api', 'upload']) {
    let releaseOld;
    const c = client(async (url) => {
      if (url === '/api/old-read' || url === '/api/chat/upload') return await new Promise((resolve) => { releaseOld = resolve; });
      return { ok: true, status: 200, json: async () => url.startsWith('/api/snapshot') ? { hosts: [] }
        : url === '/api/capabilities' ? { hosts: [], csrfToken: 'new-csrf' } : { terminals: [] } };
    }, noChatTimers);
    c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded();
    c.context.navigator.onLine = true;
    const original = selectChat(c);
    const old = kind === 'api' ? c.api('/api/old-read').catch((err) => { assert.equal(err.staleAuth, true); })
      : c.uploadChatFiles(original.entry, [{ name: 'old.png', size: 1 }]);
    c.showLogin();
    c.get('login-token').value = 'test-only-token';
    await c.get('login-form').events.submit({ preventDefault() {} });
    await new Promise((resolve) => setImmediate(resolve));
    const current = selectChat(c, { id: 'new-session' }); current.entry.text = '새 로그인에서 쓴 초안';
    const epoch = c.state.authEpoch;
    releaseOld({ ok: false, status: 401, json: async () => ({ error: 'expired old request' }) });
    await old;
    assert.equal(c.state.authEpoch, epoch); assert.equal(c.state.authenticated, true);
    assert.equal(c.get('app').hidden, false); assert.equal(c.state.csrf, 'new-csrf');
    assert.equal(current.entry.text, '새 로그인에서 쓴 초안');
    assert.equal(c.state.chats.get(c.chatKey(current.entry.source)), current.entry);
  }
});

test('finishing an old poll cannot release the busy flag of a new authentication generation', async () => {
  const releases = [];
  const c = client(async (url) => url.startsWith('/api/snapshot')
    ? await new Promise((resolve) => releases.push(resolve))
    : { ok: true, status: 200, json: async () => ({ hosts: [], csrfToken: 'current-csrf' }) }, noChatTimers);
  const old = c.poll(false); c.showLogin();
  const current = c.poll(false); assert.equal(releases.length, 2);
  releases[0]({ ok: true, status: 200, json: async () => ({ hosts: [] }) }); await old;
  assert.equal(c.state.pollBusy, true); assert.equal(c.state.authenticated, false);
  releases[1]({ ok: true, status: 200, json: async () => ({ hosts: [] }) }); await current;
  assert.equal(c.state.pollBusy, false); assert.equal(c.state.authenticated, true);
});

test('only explicit not_started send errors unlock the draft and attachments, preserving the rejection reason across reads', async () => {
  for (const status of [400, 403, 409]) {
    const requests = [];
    const c = client(async (url, options) => {
      const body = JSON.parse(options.body); requests.push({ url, body });
      if (url === '/api/chat/send') return { ok: false, status, json: async () => ({
        error: '첨부를 다시 확인해 주세요.', dispatchState: 'not_started',
      }) };
      return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'idle',
        receipts: body.requestId ? [{ requestId: body.requestId, status: 'unknown' }] : [] }) };
    }, noChatTimers);
    const { s, entry } = selectChat(c); entry.text = '거절된 초안'; entry.attachments = [{ id: 'file-1', name: 'image.png', size: 1 }];
    await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve)); await c.loadChatState(s, true);
    assert.equal(entry.pending, null); assert.equal(entry.receipt.status, 'failed'); assert.equal(entry.receipt.confirmed, false);
    assert.equal(entry.text, '거절된 초안'); assert.equal(entry.attachments.length, 1);
    assert.equal(c.get('chat-input').disabled, false); assert.equal(c.get('chat-send').disabled, false);
    assert.match(c.get('chat-status').textContent, /첨부를 다시 확인.*전송되지 않았습니다.*초안과 첨부는 유지/);
    assert.equal(requests.filter((r) => r.url === '/api/chat/send').length, 1);
    assert.equal(requests.filter((r) => r.url === '/api/chat/state').some((r) => r.body.requestId), false);
    c.get('chat-attachments').children[0].children[1].events.click();
    assert.equal(entry.attachments.length, 0); assert.equal(entry.text, '거절된 초안');
  }
});

test('HTTP status without a not_started guarantee remains unknown and never automatically resends or unlocks', async () => {
  for (const status of [400, 403, 409, 503]) {
    let sends = 0;
    const c = client(async (url, options) => {
      const body = JSON.parse(options.body);
      if (url === '/api/chat/send') { sends++; return { ok: false, status, json: async () => ({ error: '불확실한 전송 오류' }) }; }
      return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'idle',
        receipts: [{ requestId: body.requestId, status: 'unknown' }] }) };
    }, noChatTimers);
    const { entry } = selectChat(c); entry.text = '전송 결과를 모르는 초안';
    await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve)); await c.sendChat(entry);
    assert.equal(sends, 1); assert.equal(!!entry.pending, true); assert.equal(entry.receipt.status, 'unknown');
    assert.equal(c.get('chat-input').disabled, true); assert.equal(entry.text, '전송 결과를 모르는 초안');
  }
});

test('closing a composing terminal then opening another detaches all late final input from the new draft', async () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }), noChatTimers);
  c.state.authenticated = true;
  await c.openTerminal({ id: 'A', mode: 'attach' });
  const old = c.get('terminal-draft'); old.cloneNode = () => node(); old.replaceWith = (next) => c.nodes.set('terminal-draft', next);
  old.value = 'A의 조합 중 초안'; c.startDraftComposition({ target: old });
  c.closeTerminal('A'); await c.confirmTerminalClose();
  c.state.drafts.set('B', 'B의 보관 초안'); await c.openTerminal({ id: 'B', mode: 'attach' });
  const current = c.get('terminal-draft'); assert.notEqual(current, old);
  old.value = 'A의 늦은 입력'; c.inputDraft({ target: old, isComposing: false, inputType: 'insertText' });
  c.endDraftComposition({ target: old });
  assert.equal(current.value, 'B의 보관 초안'); assert.equal(c.state.drafts.get('B'), 'B의 보관 초안');
  assert.equal(c.state.drafts.get('A'), 'A의 조합 중 초안');
  current.value = 'B에서 새로 쓴 초안'; current.events.input({ target: current, isComposing: false, inputType: 'insertText' });
  assert.equal(c.state.drafts.get('B'), 'B에서 새로 쓴 초안');
});

test('launch recovery describes definite or uncertain destination starts and registers only a reviewable terminal row', async () => {
  for (const targetStarted of [true, null]) {
    const requests = [];
    const nativeSource = { host: 'homeserver', agent: 'codex', home: '.codex-secondary', id: 'destination' };
    const c = client(async (url) => {
      requests.push(url);
      return { ok: false, status: 409, json: async () => ({ error: '터미널 연결 실패', recovery: {
        targetStarted, host: 'homeserver', nativeSource, terminal: { id: 'prepared-terminal', title: '준비된 대상 작업', alive: true },
      } }) };
    }, noChatTimers);
    const { s, entry } = selectChat(c); entry.text = '원본 초안';
    c.state.route = { host: s.host, agent: s.agent, account: c.accountOf(s) };
    c.state.plan = { id: 'plan', allowed: true, mode: 'handoff', target: { host: 'homeserver' } };
    await c.launchPlan(); await c.launchPlan();
    assert.deepEqual(requests, ['/api/launch']); assert.equal(c.state.plan, null);
    assert.match(c.get('plan-error').textContent, targetStarted ? /이미 시작됐습니다/ : /이미 시작됐을 수 있습니다/);
    assert.match(c.get('plan-error').textContent, /다시 실행하지 말고.*목록을 새로고침/);
    assert.equal(c.get('plan-launch').disabled, true); assert.equal(c.state.terminal, null); assert.equal(c.state.terminalVisible, false);
    assert.equal(c.state.terminals[0].id, 'prepared-terminal');
    assert.deepEqual({ ...c.state.terminals[0].nativeSource }, nativeSource);
    assert.match(textOf(c.get('running-list')), /준비된 대상 작업/); assert.equal(entry.text, '원본 초안');
  }
});

test('a launch recovery from an old authentication epoch cannot register a target in the new session', async () => {
  let release;
  const c = client(async () => await new Promise((resolve) => { release = resolve; }), noChatTimers);
  c.state.plan = { id: 'old-plan', allowed: true, mode: 'handoff' };
  const old = c.launchPlan(); c.showLogin(); c.beginAuthEpoch(); c.state.authenticated = true;
  c.state.terminals = [{ id: 'current-terminal', title: '현재 접속 작업' }];
  release({ ok: false, status: 409, json: async () => ({ recovery: { targetStarted: null, terminal: { id: 'old-target' } } }) });
  await old;
  assert.deepEqual(c.state.terminals.map((row) => row.id), ['current-terminal']);
  assert.equal(c.state.launchBusy, false); assert.equal(c.state.terminal, null);
});

test('a successful close with delayed input cleanup stays closed, reports pending cleanup and never repeats close or input', async () => {
  const requests = []; let cleanupPending = true;
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url.endsWith('/close')
      ? { ok: true, inputCleanupPending: true }
      : { terminals: [{ id: 'term-1', closed: true, alive: false }], inputCleanupPending: cleanupPending } };
  }, noChatTimers);
  c.state.authenticated = true; c.state.terminal = { id: 'term-1', mode: 'attach' }; c.state.terminalVisible = true;
  c.closeTerminal('term-1'); await c.confirmTerminalClose();
  assert.equal(c.state.terminal, null); assert.equal(c.state.terminalVisible, false);
  assert.equal(c.get('close-terminal-dialog').open, false);
  assert.match(c.get('connection-notice').textContent, /터미널은 닫혔지만.*정리가 남았습니다.*목록을 새로고침/);
  assert.equal(c.get('connection-notice').hidden, false);
  cleanupPending = false; await c.refreshTerminals();
  assert.equal(c.get('connection-notice').hidden, true);
  assert.equal(requests.filter((url) => url.endsWith('/close')).length, 1);
  assert.equal(requests.some((url) => url.endsWith('/input')), false);
});

test('a failed initial snapshot after successful token submission leaves login enabled for a deliberate retry', async () => {
  const c = client(async (url) => ({ ok: url === '/login', status: url === '/login' ? 200 : 401, json: async () => ({}) }), noChatTimers);
  c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded(); c.context.navigator.onLine = true;
  await c.get('login-form').events.submit({ preventDefault() {} });
  assert.equal(c.state.authenticated, false); assert.equal(c.get('login').hidden, false);
  assert.equal(c.get('login-submit').disabled, false); assert.equal(c.state.loginBusy, false);
});

test('the terminal-list retry button refreshes the current epoch when clicked with a browser event', async () => {
  const requests = [];
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => ({ terminals: [{ id: 'term-1', title: '다시 확인한 터미널', alive: true }] }) };
  }, noChatTimers);
  c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded();
  c.context.navigator.onLine = true; c.beginAuthEpoch(); c.state.authenticated = true;
  await c.get('running-refresh').events.click({ type: 'click' });
  assert.deepEqual(requests, ['/api/terminals']);
  assert.match(textOf(c.get('running-list')), /다시 확인한 터미널/);
});

test('a confirmed CSRF rejection refreshes capabilities once, retains the draft and attachment, and requires a new send click', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/chat/send') return { ok: false, status: 403, json: async () => ({
      error: '접속 설정을 확인해 주세요.', code: 'csrf_expired', dispatchState: 'not_started',
    }) };
    if (url === '/api/capabilities') return { ok: true, status: 200, json: async () => ({ hosts: [], csrfToken: 'fresh-csrf' }) };
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
      capability: { supported: true }, phase: 'idle' }) };
  }, noChatTimers);
  const { entry } = selectChat(c); entry.text = '재전송 전 확인할 초안';
  entry.attachments = [{ id: 'selected-file', name: 'image.png', size: 1 }];
  const editor = c.get('chat-input'); const editorEpoch = entry.editorEpoch;
  await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.filter((r) => r.url === '/api/chat/send').length, 1);
  assert.equal(requests.filter((r) => r.url === '/api/capabilities').length, 1);
  assert.equal(requests.find((r) => r.url === '/api/capabilities').options.method, 'GET');
  assert.equal(requests.find((r) => r.url === '/api/chat/state').options.headers['X-CSRF-Token'], 'fresh-csrf');
  assert.equal(c.state.csrf, 'fresh-csrf'); assert.equal(c.get('chat-input'), editor); assert.equal(entry.editorEpoch, editorEpoch);
  assert.equal(entry.pending, null); assert.equal(entry.receipt.dispatchState, 'not_started');
  assert.equal(entry.text, '재전송 전 확인할 초안'); assert.equal(entry.attachments[0].id, 'selected-file');
  assert.equal(c.get('chat-send').disabled, false);
  assert.match(c.get('chat-status').textContent, /접속 설정을 갱신했습니다.*내용을 확인.*보내기를 다시/);
});

test('CSRF recovery failures preserve known non-delivery, while a current 401 follows the normal privacy lock policy', async () => {
  for (const failure of ['network', 503, 401]) {
    const requests = [];
    const c = client(async (url, options) => {
      requests.push(url);
      if (url === '/api/chat/send') return { ok: false, status: 403, json: async () => ({
        error: '접속 설정을 확인해 주세요.', code: 'csrf_expired', dispatchState: 'not_started',
      }) };
      if (url === '/api/capabilities') {
        if (failure === 'network') throw new Error('network unavailable');
        return { ok: false, status: failure, json: async () => ({ error: 'capabilities unavailable' }) };
      }
      return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
        capability: { supported: true }, phase: 'idle' }) };
    }, noChatTimers);
    const { entry } = selectChat(c); entry.text = '갱신 실패에도 보관할 초안';
    entry.attachments = [{ id: 'selected-file', name: 'image.png', size: 1 }];
    await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve));
    assert.equal(requests.filter((url) => url === '/api/chat/send').length, 1);
    assert.equal(requests.filter((url) => url === '/api/capabilities').length, 1);
    if (failure === 401) {
      assert.equal(c.state.authenticated, false); assert.equal(c.state.chats.size, 0); assert.equal(c.get('app').hidden, true);
    } else {
      assert.equal(entry.pending, null); assert.equal(entry.receipt.dispatchState, 'not_started');
      assert.equal(entry.text, '갱신 실패에도 보관할 초안'); assert.equal(entry.attachments[0].id, 'selected-file');
      assert.match(c.get('chat-status').textContent, /갱신하지 못했습니다.*전송되지 않았습니다/);
    }
  }
});
