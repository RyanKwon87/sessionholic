"use strict";

// Exercise state and transport boundaries without starting a browser or a real agent.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const messageSource = fs.readFileSync(path.join(__dirname, "../web/message-format.js"), "utf8");

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
    dispatchEvent(event) { this.events[event.type]?.(event); return true; },
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
  { instanceMeta, currentScript } = {},
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
    createElement(tag) {
      const created = node(); created.tagName = tag.toUpperCase();
      Object.defineProperty(created, 'id', { get() { return this.elementId; },
        set(value) { this.elementId = value; nodes.set(value, this); } });
      return created;
    },
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
  };
  // The server fills <meta name="sessionholic-instance">; app.js reads it once at load.
  if (currentScript !== undefined) document.currentScript = currentScript;
  if (instanceMeta !== undefined) {
    document.querySelector = (selector) => (selector === 'meta[name="sessionholic-instance"]'
      ? { getAttribute: (name) => (name === 'content' ? instanceMeta : null) } : null);
  }
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
    URL,
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
  vm.runInContext(messageSource, context);
  vm.runInContext(source, context);
  const api = vm.runInContext(
    "({state,api,sourceOf,accountOf,sendInput,pasteDraft,prepareDraftPaste,startDraftComposition,endDraftComposition,inputDraft,stopTerminalReader,startTerminalReader,closePlan,navigateView,backToList,backFromTerminal,closeTerminal,cancelTerminalClose,confirmTerminalClose,populateAccount,populateRoute,updateContinue,renderPlan,launchPlan,openTerminal,saveDraft,draftKey,refreshTerminals,renderTerminals,renderTerminalReturn,renderFilters,terminalCloseCopy,warnDraftUnload,preparePlan,renderGroups,renderMessages,loadDetail,chatKey,chatEntry,currentChat,chatSupported,chatRecovery,recoverChatSession,openChatTerminal,chatAttachmentsSupported,buildChatComposer,sendChat,loadChatState,applyChatReceipt,uploadChatFiles,pasteChat,scheduleChatPoll,stopChatPolling,openNativeChat,allSessions,buildContinuePanel,chatReceiptCopy,returnToBoard,readChatClipboard,accountDisplay,sessionAccountLabel,closeChatToolsOutside,closeChatToolsEscape,drawDetail,refreshDetail,loadCapabilities,matches,recoverConnection,networkOffline,poll,select,focusDetailBack,restoreSelectedCardFocus,showLogin,lock,beginAuthEpoch,resizeChatInput,updateViewport,draftKeydown,configureTerminalKeyboard,queueTerminalScroll,flushTerminalScroll,terminalScrollEnd,resetTerminalScroll,renderAttention,chatPollDelay,pullRefresh,readingScroll,renderJump,pullDistance,listScroll,setReading,renderPrompt,promptAction,renderUsage,loadUsage,applyTheme,setTheme,updateThemeColor,haptic,navTransition,setDetailShown,chatVisible,sheetRelease,BottomSheet,sheet,openSettings,closeSettings,setTyping,pinchFontSize,setTerminalFontSize,loadXterm,assetUrl,syncEvents,closeEvents,eventsHealthy,renderFreshness,schedule,touchDevice,togglePhaseFilter,renderCounts,createLongPress,swipeIntent,swipeRelease,groupMessageRows,setTextSize,applyTextSize,renderConnectivity,terminalForSession,swipeActionFor,openMessageMenu,openCardMenu,messageActions,outboxShape,outboxMatches,outboxOrigin,settleOutbox,retryOutbox,renderAttachmentTiles,attachmentThumb,sendUpload,openActionSheet,closeActionSheet,setListCollapsed,closeSwipe,settleSwipe,prepareSwipe,runSwipeAction,GESTURE,setChromeVisible,toggleChrome,revealChrome,isChromeTap,snapshotAnnounced,updateChatComposer,capabilitiesRevision,toolKindOf,userMessageParts,formatElapsed,tickWorking,renderTerminalMarks,renderDetailTerminal,terminalsAnnounced,goToSessionTerminal,rememberTerminal,terminalMenu,updateTerminalMenu,chooseTerminalMenu,clearTerminalMenu,renderNewSession,openNewSession,startNewSession,startAppServer,rememberConversationView,restoreConversationView,conversationIdentity,initiatorOf,initiatorLabel,documentKeydown,sessionCategoryOf,setSessionCategory,renderSessionCategories,prepareSessionListFor,sessionCategoryKeydown,render,resetFilters,terminalTitle,renameTerminals,requestedSessionFrom,requestSessionOpen,openRequestedSession,answerOnOpen,hardReload,terminalKeyBytes,terminalKeyFromEvent,pressTerminalKey,toggleTerminalMod,clearTerminalMods,renderTerminalMods,draftBeforeInput,terminalDocumentKey,xtermKeyEvent,renderHardware,loadHardware,startDictation,stopDictation,dictationText,toggleDraftDictation,renderDictation,DICTATION_LISTENING,DICTATION_ERRORS,DICTATION_PREPARING,DICTATION_STALLED,DICTATION_ENDED_AT_ONCE,DICTATION_INTERRUPTED,DICTATION_NOT_STARTED,DICTATION_SILENCE_END,bindDraftInput,insertChatText,reloadForUpdate,checkWebVersion,unsavedWork,detailMetaChildren,updateBannerPressed,manualRefresh,listNavigationKeydown,sessionUnread,markSeen,quickReplies,saveQuickReplies,openQuickReplies})",
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
  assert.match(textOf(c.get("groups")), /조건에 맞는 세션/);
  assert.match(textOf(c.get("groups")), /‘없는 작업’ 검색.*필터.*3일 지난 대화/s);
  byClass(c.get("groups"), "secondary")[0].events.click();
  assert.equal(c.state.search, "");
  assert.equal(c.state.filters.host, "");
  assert.equal(c.get("search").value, "");
  c.state.snapshot.hosts[0].refreshing = true;
  c.renderGroups([], Date.now());
  assert.match(textOf(c.get("groups")), /확인하고 있습니다/);
});

test("conversation retry reloads its selected source and ignores a switched selection", async () => {
  const calls = [];
  const c = client(async (url, options) => {
    calls.push(url);
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source, messages: [] }) };
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
  assert.equal(calls[0], '/api/chat/state');
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

test('selected native polling backs off during long turns, stays bounded and stops while hidden or in terminal', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {} });
  const { s, entry } = selectChat(c);
  entry.data.phase = 'working';
  c.scheduleChatPoll(s, entry);
  assert.equal(timers.at(-1).delay, 3000);
  // A turn longer than two minutes is still followed, just less often.
  for (const [count, delay] of [[10, 5000], [30, 10000], [90, 20000], [239, 20000]]) {
    c.state.chatPollCount = count;
    c.scheduleChatPoll(s, entry);
    assert.equal(timers.at(-1).delay, delay);
  }
  const count = timers.length;
  c.state.chatPollCount = 240;
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
  entry.pending = { requestId: 'interrupted-request-id', revision: entry.revision, text: entry.text, attachments: [] };
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
  entry.pending = { requestId: 'confirmed-failed-id', revision: entry.revision, text: entry.text, attachments: ['attached-file'] };
  c.applyChatReceipt(entry, { requestId: 'confirmed-failed-id', status: 'failed', confirmed: true });
  assert.equal(entry.text, '');
  assert.equal(entry.attachments.length, 0);
  assert.equal(entry.pending, null);
  assert.match(c.chatReceiptCopy(entry.receipt), /처리 중 오류/);
  assert.doesNotMatch(c.chatReceiptCopy(entry.receipt), /접수하지 못/);
  entry.text = '미접수 요청';
  entry.pending = { requestId: 'unaccepted-failed-id', revision: entry.revision, text: entry.text, attachments: [] };
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
  assert.equal(requests.filter((r) => r.url === '/api/chat/send').length, 0, 'the IME commit Enter never sends');
  entry.compositionEndedAt -= 100;
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
  let phase = 'idle';
  const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/chat/state'
      ? { capability: { supported: true }, phase, route: JSON.parse(options.body).source }
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
  message = '갱신된 대화'; phase = 'working';
  const updated ={ ...s, title: '갱신된 제목', project: '새 프로젝트', phase: 'working',
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
  assert.equal(c.get('detail').querySelector('.detail-project').textContent, '새 프로젝트');
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
  // Cards show three short lines; the account stays in each card's accessible name.
  const cardNames = () => byClass(c.get('groups'), 'card').map((card) => card.attributes['aria-label']).join(' ');
  assert.match(cardNames(), /실험 환경 · sample/);
  assert.match(cardNames(), /공용 환경 · sample/);
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
  tool.toolToggle.events.click();
  list.contains = (node) => node === answer;
  c.context.window.getSelection = () => ({ isCollapsed: false, anchorNode: answer, focusNode: answer });
  list.scrollHeight = 500; list.clientHeight = 100; list.scrollTop = 395;
  c.renderMessages(s, messages);
  c.renderMessages(s, [...messages, { role: 'user', text: '새 답변' }]);
  assert.equal(list.children[0], tool); assert.equal(list.children[1], answer);
  assert.equal(tool.open, true); assert.equal(tool.toolList.hidden, false); assert.equal(list.scrollTop, 395);
  let copied;
  c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  assert.equal(answer.moreButton.attributes['aria-label'], '메시지 메뉴');
  assert.equal(byClass(answer, 'msg-copy').length, 0, 'no per-message copy button');
  await menuAction(c, answer, '복사');
  assert.equal(copied, '답변 본문');
});

test('assistant replies render Markdown and copy readable body or exact code while user input stays literal', async () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const raw = '**요청**\n\n> 첫 줄\n>\n> **둘째 줄**\n\n```python\nif a > b:\n    print("확인")\n```';
  c.renderMessages(s, [{ role: 'assistant', text: raw }, { role: 'user', text: raw }]);
  const [answer, user] = c.get('messages').children;
  const flatten = (root) => [root, ...(root.children || []).flatMap(flatten)];
  const rendered = flatten(answer);
  assert.equal(rendered.filter((n) => n.tagName === 'BLOCKQUOTE').length, 1);
  assert.deepEqual(rendered.filter((n) => n.tagName === 'STRONG' && textOf(n).trim() !== 'Codex').map((n) => textOf(n).trim()), ['요청', '둘째 줄']);
  assert.equal(rendered.filter((n) => n.tagName === 'PRE').length, 1);
  assert.equal(flatten(user).some((n) => n.tagName === 'BLOCKQUOTE'), false);
  let copied;
  c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  await menuAction(c, answer, '복사');
  assert.equal(copied, '요청\n\n첫 줄\n\n둘째 줄\n\nif a > b:\n    print("확인")\n');
  // Quotes copy with the message; only code keeps its own small icon.
  assert.equal(rendered.some((n) => n.attributes['aria-label'] === '인용문 본문 복사'), false);
  rendered.find((n) => n.attributes['aria-label'] === '코드 블록 복사').events.click();
  await settle();
  assert.equal(copied, 'if a > b:\n    print("확인")\n');
  copied = '';
  await menuAction(c, answer, '코드 복사');
  assert.equal(copied, 'if a > b:\n    print("확인")\n');
  await menuAction(c, user, '복사');
  assert.equal(copied, raw);
  assert.equal(menuLabels(c, user).includes('코드 복사'), false, 'user text is never parsed for code');
});

// Opens a message's menu with its button and runs one action by label.
function menuLabels(c, row) {
  row.moreButton.events.click();
  return c.get('action-list').children.map((item) => item.textContent);
}
async function menuAction(c, row, label) {
  row.moreButton.events.click();
  const item = c.get('action-list').children.find((child) => child.textContent === label);
  assert.ok(item, `menu item ${label}`);
  item.events.click();
  await settle();
}

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

test('online recovery reads one selected state and snapshot without resending unknown work or terminal input', async () => {
  const requests = [];
  let snapshot;
  const c = client(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, status: 200, json: async () => url === '/api/snapshot' ? snapshot :
      { route: JSON.parse(options.body).source, capability: { supported: true }, phase: 'idle' } };
  }, noChatTimers);
  const { s, entry } = selectChat(c); snapshot = c.state.snapshot;
  c.state.detailFor = s.key; c.state.detailUpdatedAt = s.updatedAt;
  c.state.detailRevision = vm.runInContext('detailRevision', c.context)(c.allSessions().find((item) => item.key === s.key));
  c.state.capabilities = {}; c.state.capabilitiesRevision = c.capabilitiesRevision(c.state.snapshot);
  entry.pending = { requestId: 'unknown-request', revision: entry.revision }; entry.text = '전송 결과 모르는 초안';
  c.state.offline = true; c.context.navigator = { onLine: true };
  c.recoverConnection(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.filter((r) => r.url === '/api/snapshot').length, 1);
  assert.equal(requests.filter((r) => r.url === '/api/chat/state').length, 1);
  assert.equal(requests.some((r) => /chat\/send|chat\/upload|terminal\//.test(r.url)), false);
  assert.equal(JSON.parse(requests.find((r) => r.url === '/api/chat/state').options.body).requestId, 'unknown-request');
  assert.equal(entry.pending.requestId, 'unknown-request');
  c.state.terminalVisible = true; c.state.terminal = { id: 'visible-terminal' }; c.state.offline = true;
  const before = requests.length; c.recoverConnection(); await new Promise((resolve) => setImmediate(resolve));
  // Output is re-read from its cursor; input is never replayed and stays locked until a valid read.
  const after = requests.slice(before);
  assert.ok(after.length >= 1);
  assert.ok(after.every((r) => /\/api\/terminal\/visible-terminal\/events\?/.test(r.url) && (r.options?.method || 'GET') === 'GET'));
  assert.equal(c.state.connected, false);
  assert.match(c.get('terminal-status').textContent, /다시 연결/);
});

test('the open conversation\'s own phase shows on its header, card and counts until the list\'s phase changes', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source, capability: { supported: true },
      phase: 'idle', receipts: [], messages: [] }) };
  }, noChatTimers);
  const { s } = selectChat(c);
  Object.assign(s, { phase: 'working', title: '목록이 늦은 작업', updatedAt: Date.now() / 1000 });
  c.state.snapshot.hosts[0].data.codex[0] = s;
  c.refreshDetail(c.allSessions()[0]);
  wireTree(c, c.get('detail'));
  assert.match(textOf(c.get('detail').querySelector('.detail-meta')), /작업 중/, 'the list says working');
  await settle();
  // The conversation's read (newer than the list) says the turn ended.
  assert.equal(c.allSessions()[0].phase, 'idle');
  assert.match(textOf(c.get('detail').querySelector('.detail-meta')), /열림/);
  assert.equal(requests.filter((url) => url === '/api/chat/state').length, 1, 'shown without reading the conversation again');
  // The list catches up, then moves on: its own phase stands again.
  c.state.snapshot.hosts[0].data.codex[0] = { ...s, phase: 'idle' };
  assert.equal(c.allSessions()[0].phase, 'idle');
  assert.equal(c.state.livePhases.size, 0);
  c.state.snapshot.hosts[0].data.codex[0] = { ...s, phase: 'needs_input' };
  assert.equal(c.allSessions()[0].phase, 'needs_input');
  // A quick turn may end where it began (a new question). While the conversation is open its
  // next read decides, even if a list read finishes first (it may have begun before that read).
  const host = c.state.snapshot.hosts[0];
  host.fetchedAt = 1000;
  c.state.livePhases.set(c.allSessions()[0].key, { phase: 'working', base: 'needs_input', fetchedAt: 1000 });
  assert.equal(c.allSessions()[0].phase, 'working', 'the read stands while the list is no newer');
  host.fetchedAt = 1060;
  assert.equal(c.allSessions()[0].phase, 'working', 'the open conversation is read again soon');
  // Left: no read follows, so the next list read stands though its phase looks unchanged.
  c.state.selected = null;
  assert.equal(c.allSessions()[0].phase, 'needs_input');
  assert.equal(c.state.livePhases.size, 0);
});

test('a terminal list whose first read failed is read again by the next regular read', async () => {
  const requests = []; let fail = true; let snapshot;
  const c = client(async (url) => {
    requests.push(url);
    if (url === '/api/terminals') {
      if (fail) return { ok: false, status: 502, json: async () => ({ error: '일시적인 오류' }) };
      return { ok: true, status: 200, json: async () => ({ terminals: [], version: 3 }) };
    }
    return { ok: true, status: 200, json: async () => url === '/api/snapshot' ? snapshot : {} };
  }, noChatTimers);
  selectChat(c);
  snapshot = c.state.snapshot;
  c.state.capabilities = {}; c.state.capabilitiesRevision = c.capabilitiesRevision(snapshot);
  await c.refreshTerminals();
  assert.ok(c.state.terminalsError);
  assert.equal(c.state.terminalsReadAt, 0);
  fail = false;
  await c.poll(); await settle();
  assert.equal(requests.filter((url) => url === '/api/terminals').length, 2);
  assert.equal(c.state.terminalsError, '');
  assert.ok(c.state.terminalsReadAt > 0);
  await c.poll(); await settle();
  assert.equal(requests.filter((url) => url === '/api/terminals').length, 2, 'then once a minute as before');
});

test('a conversation that left the list stops being followed, and a phone goes back to the list instead of a screen without a way back', async () => {
  const requests = [];
  let snapshot;
  const c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/snapshot' ? snapshot : {} };
  }, noChatTimers);
  c.context.window.matchMedia = (query) => ({ matches: query === '(max-width: 700px)' });
  const { s } = selectChat(c);
  c.state.detailShown = true;
  c.state.capabilities = {};
  snapshot = { hosts: [{ name: 'workstation', label: 'Workstation', ok: true, data: { codex: [] } }] };
  c.state.capabilitiesRevision = c.capabilitiesRevision(snapshot);
  const run = c.state.chatPollRun;
  await c.poll();
  assert.equal(c.state.selected, null);
  assert.ok(c.state.chatPollRun > run, 'its polling stopped');
  assert.equal(c.state.detailShown, false, 'the phone shows the list again');
  assert.match(c.get('toast').textContent, /더 이상 찾을 수 없어 목록으로/);
  // A wide screen keeps the list beside it and says so in the detail pane.
  const d = client(async (url) => ({ ok: true, status: 200, json: async () => url === '/api/snapshot' ? snapshot : {} }), noChatTimers);
  selectChat(d);
  d.state.detailShown = true;
  d.state.capabilities = {};
  d.state.capabilitiesRevision = d.capabilitiesRevision(snapshot);
  await d.poll();
  assert.equal(d.state.selected, null);
  assert.match(textOf(d.get('detail')), /더 이상 찾을 수 없습니다/);
  assert.ok(s);
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
  c.state.detailRevision = vm.runInContext('detailRevision', c.context)(c.allSessions().find((item) => item.key === s.key));
  c.state.capabilities = {}; c.state.capabilitiesRevision = c.capabilitiesRevision(c.state.snapshot);
  entry.text = '미전송 초안';
  c.windowEvents.offline(); assert.equal(c.state.offline, true);
  c.context.document.hidden = true; c.documentEvents.visibilitychange();
  c.context.document.hidden = false; c.documentEvents.visibilitychange();
  // A visibility restoration while still offline cannot start any transport.
  assert.equal(requests.length, 0);
  c.context.navigator.onLine = true; c.windowEvents.online();
  await new Promise((resolve) => setImmediate(resolve));
  // The usage strip is another bounded read alongside the list.
  assert.deepEqual(requests.filter((url) => url !== '/api/usage' && url !== '/api/hardware'), ['/api/snapshot', '/api/chat/state']);
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
  assert.equal(byClass(c.get('groups'), 'secondary')[0].textContent, '다시 확인');
  assert.equal(byClass(c.get('groups'), 'empty-title').length, 1, 'one short title per empty state');
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
      keys: async () => ['another-app-shell', 'sessionholic-shell-v0', 'sessionholic-shell-v1', 'sessionholic-shell-v2', 'sessionholic-shell-v4', 'sessionholic-shell-v5', 'sessionholic-shell-v6', 'sessionholic-shell-v7', 'sessionholic-shell-v8', 'sessionholic-shell-v9', 'sessionholic-shell-v10', 'sessionholic-shell-v11', 'sessionholic-shell-v12'],
      delete: async (key) => { deleted.push(key); },
      open: async (key) => { opened.push(key); return { match: async () => currentShell }; },
      match: async () => { throw new Error('Global cross-application cache lookup is forbidden'); },
    },
  });
  let activated;
  listeners.activate({ waitUntil: (promise) => { activated = promise; } }); await activated;
  assert.deepEqual(deleted, ['sessionholic-shell-v0', 'sessionholic-shell-v1', 'sessionholic-shell-v2', 'sessionholic-shell-v4', 'sessionholic-shell-v5', 'sessionholic-shell-v6', 'sessionholic-shell-v7', 'sessionholic-shell-v8', 'sessionholic-shell-v9', 'sessionholic-shell-v10', 'sessionholic-shell-v11', 'sessionholic-shell-v12']);
  listeners.fetch({ request: { method: 'GET', url: 'https://app.example/' },
    respondWith: (promise) => { response = promise; }, waitUntil() {} });
  assert.equal(await response, currentShell);
  assert.deepEqual(opened, ['sessionholic-shell-v14']);
});

test('an offline first launch restores authentication and already-open terminal rows without reopening their sessions', async () => {
  const requests = [];
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => url === '/api/snapshot'
      ? { hosts: [], serverTime: Date.now() / 1000 }
      : url === '/api/terminals' ? { terminals: [{ id: 'existing-terminal', alive: true,
        host: 'workstation', agent: 'codex', title: '이미 열린 작업' }] }
      : url === '/api/bot-requests' ? { requests: [], sources: [] } : { hosts: [], csrfToken: 'test-csrf', authMode: 'token' } };
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
  assert.deepEqual(requests.filter((url) => url !== '/api/usage' && url !== '/api/hardware'), ['/api/snapshot', '/api/bot-requests', '/api/capabilities', '/api/terminals']);
  assert.match(textOf(c.get('running-list')), /이미 열린 작업/);
  assert.equal(c.state.terminal, null); assert.equal(c.state.terminalVisible, false);
  assert.equal(c.get('app').hidden, false); assert.equal(c.get('login').hidden, true);
  assert.equal(c.get('login-submit').disabled, false);
  c.windowEvents.online(); await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests.filter((url) => url !== '/api/usage' && url !== '/api/hardware'), ['/api/snapshot', '/api/bot-requests', '/api/capabilities', '/api/terminals', '/api/snapshot']);
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

test('an expired login closes every open sheet, the new-session one too, so the login form is reachable', () => {
  const c = client(undefined, noChatTimers);
  const sheets = ['plan-dialog', 'settings-dialog', 'usage-dialog', 'filter-dialog', 'terminals-dialog', 'new-session-dialog', 'action-sheet'];
  for (const id of sheets) c.get(id).open = true;
  c.showLogin();
  assert.deepEqual(sheets.filter((id) => c.get(id).open), []);
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
    byClass(c.get('chat-attachments'), 'chat-attachment-remove')[0].events.click();
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
    assert.equal(c.get('chat-input').readOnly, true); assert.equal(c.get('chat-input').disabled, false);
    assert.equal(c.get('chat-input').attributes['aria-disabled'], 'true'); assert.equal(entry.text, '전송 결과를 모르는 초안');
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

test('chat grows while typing or pasting, shrinks after deletion and bounds long drafts', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  const composer = wireTree(c, c.buildChatComposer(s));
  const input = c.get('chat-input');
  c.context.window.innerHeight = 800;
  Object.defineProperty(input, 'scrollHeight', { get: () => 20 + 22 * (input.value.split('\n').length) });
  input.value = '첫 줄\n둘째 줄\n셋째 줄'; input.events.input();
  assert.equal(input.style.height, '88px');
  assert.equal(c.get('chat-expand').textContent, '접기');
  input.value = Array(20).fill('긴 글').join('\n'); input.events.input();
  assert.equal(input.style.height, '154px');
  assert.equal(input.style.overflowY, 'auto');
  c.get('chat-expand').events.click();
  assert.equal(input.style.height, '44px');
  assert.equal(entry.text, Array(20).fill('긴 글').join('\n'));
  input.value += '\n계속 작성'; input.events.input();
  assert.equal(input.style.height, '154px');
  input.value = '짧게'; input.events.input();
  assert.equal(input.style.height, '44px');
  input.value = ''; input.events.input();
  assert.equal(input.style.height, '44px');
  assert.equal(input.style.overflowY, 'hidden');
  c.get('chat-expand').events.click();
  c.get('chat-expand').events.click();
  input.events.compositionstart();
  input.value = '조합 첫 줄\n둘째 줄\n셋째 줄';
  input.events.compositionend(); // Some IMEs commit without a following input event.
  assert.equal(input.style.height, '88px');
  assert.equal(entry.inputCollapsed, false);
  assert.equal(findId(composer, 'chat-input'), input);
});

test('chat resize preserves active Korean composition and respects keyboard viewport and wrapping changes', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s));
  const input = c.get('chat-input');
  c.context.document.activeElement = input;
  c.context.window.innerHeight = 800;
  let measured = 64;
  Object.defineProperty(input, 'scrollHeight', { get: () => measured });
  input.value = '아직 입력 중인 한글'; input.events.compositionstart(); input.events.input();
  const epoch = entry.editorEpoch;
  assert.equal(input.style.height, '66px');
  measured = 152;
  c.updateViewport(); // A narrower width wraps the same text onto more lines.
  assert.equal(input.style.height, '154px');
  c.context.window.visualViewport = { height: 300 };
  c.updateViewport();
  assert.equal(input.style.height, '100px');
  assert.equal(input.style.overflowY, 'auto');
  assert.equal(entry.composing, true);
  assert.equal(entry.editorEpoch, epoch);
  assert.equal(c.context.document.activeElement, input);
  c.context.window.visualViewport.height = 240;
  c.updateViewport();
  assert.equal(input.style.height, '44px');
  assert.equal(input.value, '아직 입력 중인 한글');
});

test('programmatic clipboard insertion grows the editor and accepted delivery shrinks it without clearing newer drafts', async () => {
  const text = '복사 첫 줄\n둘째 줄\n셋째 줄';
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s));
  const input = c.get('chat-input');
  c.context.navigator = { clipboard: { readText: async () => text } };
  Object.defineProperty(input, 'scrollHeight', { get: () => 20 + 22 * input.value.split('\n').length });
  await c.readChatClipboard(entry, input, c.get('chat-files'), entry.editorEpoch);
  assert.equal(input.style.height, '88px');
  entry.pending = { requestId: 'grow-draft-request', revision: entry.revision, text: entry.text, attachments: [] };
  c.applyChatReceipt(entry, { requestId: 'grow-draft-request', status: 'accepted' });
  assert.equal(input.value, '');
  assert.equal(input.style.height, '44px');
  input.value = text; input.events.input();
  entry.pending = { requestId: 'older-draft-request', revision: entry.revision - 1 };
  c.applyChatReceipt(entry, { requestId: 'older-draft-request', status: 'accepted' });
  assert.equal(input.value, text);
  assert.equal(input.style.height, '88px');
});

function byClass(root, className) {
  const found = [];
  const visit = (current) => {
    if ((current.className || '').split(' ').includes(className)) found.push(current);
    for (const child of current.children || []) visit(child);
  };
  visit(root);
  return found;
}

function attachmentState(entry, receipts = [], messages) {
  return { route: entry.source, capability: { supported: true }, phase: 'idle', receipts: receipts.filter(Boolean),
    ...(messages ? { messages } : {}) };
}

test('user history displays durable attachment names, image kinds and attachment-only rows without altering body copy', async () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  let copied;
  c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  const messages = [
    { role: 'user', text: '사진 **원문**을 확인해 주세요', attachments: [
      { id: 'photo', name: '화면.png', type: 'image/png', size: 2048, kind: 'image', source: 'sessionholic' },
      { id: 'notes', name: '<img onerror=alert(1)>.txt', type: 'text/plain', size: 12, kind: 'file', source: 'sessionholic' },
    ] },
    { role: 'user', text: '', attachments: [{ id: null, name: '이미지 첨부', type: '', size: null, kind: 'image', source: 'native' }] },
    { role: 'user', text: '파일을 보냈다고만 말하는 텍스트' },
  ];
  c.renderMessages(s, messages);
  const rows = c.get('messages').children;
  const group = byClass(rows[0], 'msg-attachments')[0];
  assert.equal(group.attributes['aria-label'], '첨부 2개');
  const chips = byClass(group, 'msg-attachment');
  assert.equal(chips.length, 2);
  assert.deepEqual(chips.map((chip) => chip.attributes['aria-label']), ['이미지: 화면.png, 2KB', '파일: <img onerror=alert(1)>.txt, 12B']);
  assert.match(textOf(group), /화면.png.*<img onerror=alert\(1\)>.txt/);
  assert.equal(byClass(group, 'attachment-name')[1].textContent, '<img onerror=alert(1)>.txt');
  await menuAction(c, rows[0], '복사');
  assert.equal(copied, messages[0].text);
  // A transcript that cannot name the file shows its kind once, not "이미지 이미지 첨부".
  assert.equal(byClass(rows[1], 'attachment-name')[0].textContent, '이미지');
  assert.doesNotMatch(textOf(rows[1]), /이미지\s+이미지/);
  assert.equal(byClass(rows[1], 'msg-attachment')[0].attributes['aria-label'], '이미지');
  assert.equal(byClass(rows[1], 'attachment-size').length, 0);
  assert.equal(menuLabels(c, rows[1]).includes('복사'), false, 'nothing to copy from an attachment-only message');
  assert.equal(byClass(rows[2], 'msg-attachments').length, 0);
});

test('attachment metadata refresh replaces only changed message rows and preserves unrelated tool state and scroll', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  const messages = [{ role: 'tool', text: '기존 도구 기록' },
    { role: 'user', text: '검토 부탁', attachments: [{ id: 'a', name: 'old.txt', kind: 'file', source: 'sessionholic' }] }];
  const list = c.get('messages');
  list.scrollHeight = 500; list.clientHeight = 100; list.scrollTop = 70;
  c.renderMessages(s, messages);
  const [tool, user] = list.children;
  tool.toolToggle.events.click();
  c.renderMessages(s, JSON.parse(JSON.stringify(messages)));
  assert.equal(list.children[0], tool); assert.equal(list.children[1], user);
  const renamed = structuredClone(messages);
  renamed[1].attachments[0].name = '서버에서 복원된 이름.txt';
  c.renderMessages(s, renamed);
  assert.equal(list.children[0], tool); assert.equal(tool.open, true);
  assert.notEqual(list.children[1], user);
  assert.match(textOf(list.children[1]), /서버에서 복원된 이름.txt/);
  assert.equal(list.scrollTop, 70);
});

test('upload completion labels files as before-send and creates neither a history message nor delivery evidence', async () => {
  const requests = [];
  let entry;
  const c = client(async (url) => {
    requests.push(url);
    if (url === '/api/chat/state') return { ok: true, status: 200, json: async () => attachmentState(entry) };
    return { ok: true, status: 200, json: async () => ({ attachment: {
      id: 'uploaded', name: 'server-normalized.png', type: 'image/png', size: 5,
    } }) };
  }, noChatTimers);
  const selected = selectChat(c); entry = selected.entry;
  const s = selected.s;
  wireTree(c, c.buildChatComposer(s));
  await new Promise((resolve) => setImmediate(resolve));
  await c.uploadChatFiles(entry, [{ name: 'local.png', type: 'image/png', size: 5 }]);
  assert.deepEqual(requests.filter((url) => url !== '/api/chat/state'), ['/api/chat/upload']);
  assert.equal(c.get('chat-attachments-label').hidden, false);
  assert.match(c.get('chat-attachments-label').textContent, /전송 전 첨부 1개.*업로드 완료/);
  assert.match(textOf(c.get('chat-attachments')), /server-normalized.png/);
  assert.equal(c.get('chat-delivery-evidence').hidden, true);
  assert.equal(c.get('messages').children.length, 0);
  assert.equal(entry.receipt, null);
});

test('queued attachment delivery keeps server metadata evidence after clearing the sent draft and avoids content-read claims', async () => {
  const requests = [];
  let entry;
  let receipt;
  const c = client(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    if (url === '/api/chat/send') {
      receipt = { requestId: requests.at(-1).body.requestId, status: 'queued', confirmed: true,
        acknowledged: true, readbackConfirmed: true, attachments: [
          { id: 'selected', name: 'authoritative.png', type: 'image/png', size: 7, kind: 'image', source: 'sessionholic' },
        ] };
      return { ok: true, status: 200, json: async () => ({ status: 'queued', receipt }) };
    }
    return { ok: true, status: 200, json: async () => attachmentState(entry, [receipt]) };
  }, noChatTimers);
  const selected = selectChat(c); entry = selected.entry;
  wireTree(c, c.buildChatComposer(selected.s));
  await new Promise((resolve) => setImmediate(resolve));
  entry.text = '첨부를 확인해 주세요';
  entry.attachments = [{ id: 'selected', name: 'local.png', type: 'image/png', size: 7, path: '/not-for-evidence/local.png' }];
  await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.pending, null); assert.equal(entry.text, ''); assert.equal(entry.attachments.length, 0);
  const outbound = requests.find((request) => request.url === '/api/chat/send');
  assert.deepEqual(outbound.body.attachments, ['selected']);
  assert.equal(Object.hasOwn(outbound.body, 'attachmentDetails'), false);
  const evidence = c.get('chat-delivery-evidence');
  assert.equal(evidence.hidden, false);
  assert.match(textOf(evidence), /전송 접수.*세션 기록 확인.*전송에 포함한 첨부 1개.*authoritative.png/);
  assert.doesNotMatch(textOf(evidence), /local.png|not-for-evidence|모델.*읽|동기화 완료/);
  assert.equal(c.get('chat-attachments-label').hidden, true);
  const retained = evidence.children[0];
  await c.loadChatState(selected.s, true);
  assert.equal(evidence.children[0], retained);
  receipt = { ...receipt, status: 'completed' };
  await c.loadChatState(selected.s, true);
  assert.match(textOf(evidence), /응답 종료.*authoritative.png/);
  assert.doesNotMatch(textOf(evidence), /모델.*읽|동기화 완료/);
});

test('uncertain attachment sends retain file evidence, stay locked and remain isolated across session changes', async () => {
  const sends = [];
  let entry;
  let emptyMetadata = false;
  const c = client(async (url, options) => {
    if (url === '/api/chat/send') { sends.push(JSON.parse(options.body)); throw new Error('response lost'); }
    const receipt = entry.pending && { requestId: entry.pending.requestId, status: 'unknown',
      ...(emptyMetadata ? { attachments: [] } : {}) };
    return { ok: true, status: 200, json: async () => attachmentState(entry, [receipt]) };
  }, noChatTimers);
  const original = selectChat(c); entry = original.entry;
  wireTree(c, c.buildChatComposer(original.s));
  await new Promise((resolve) => setImmediate(resolve));
  entry.attachments = [{ id: 'lost-image', name: '보낸 사진.png', type: 'image/png', size: 6 }];
  await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve));
  assert.ok(entry.pending);
  assert.equal(entry.pending.attachmentDetails[0].name, '보낸 사진.png');
  assert.match(textOf(c.get('chat-delivery-evidence')), /전송 결과 미확인.*보낸 사진.png/);
  assert.equal(c.get('chat-attachments').hidden, true);
  assert.equal(c.get('chat-send').disabled, true);
  await c.sendChat(entry);
  assert.equal(sends.length, 1);
  emptyMetadata = true;
  await c.loadChatState(original.s, true);
  assert.equal(c.get('chat-delivery-evidence').hidden, true);
  assert.equal(c.get('chat-attachments').hidden, false);
  assert.match(c.get('chat-attachments-label').textContent, /전송 요청 첨부 1개.*결과 미확인/);
  assert.match(textOf(c.get('chat-attachments')), /보낸 사진.png/);
  const other = selectChat(c, { id: 'other-session' });
  wireTree(c, c.buildChatComposer(other.s));
  // A user edit runs the same composer update without starting a network read.
  c.get('chat-input').value = '다른 세션 초안'; c.get('chat-input').events.input();
  assert.equal(c.get('chat-delivery-evidence').hidden, true);
  assert.doesNotMatch(textOf(c.get('chat-attachments')), /보낸 사진/);
  assert.equal(entry.pending.requestId, sends[0].requestId);
  assert.equal(other.entry.text, '다른 세션 초안');
});

test('server receipt metadata takes precedence while a pending request stays uncertain and newer Korean drafts remain intact', async () => {
  let entry;
  let receipt;
  const c = client(async () => ({ ok: true, status: 200, json: async () => attachmentState(entry, [receipt]) }), noChatTimers);
  const selected = selectChat(c); entry = selected.entry;
  wireTree(c, c.buildChatComposer(selected.s));
  await new Promise((resolve) => setImmediate(resolve));
  const input = c.get('chat-input');
  entry.pending = { requestId: 'old', revision: entry.revision,
    attachmentDetails: [{ id: 'old-file', name: 'local-name.png', type: 'image/png' }] };
  receipt = { requestId: 'old', status: 'unknown', attachments: [
    { id: 'old-file', name: 'server-name.png', type: 'image/png', kind: 'image', source: 'sessionholic' },
  ] };
  await c.loadChatState(selected.s, true);
  assert.match(textOf(c.get('chat-delivery-evidence')), /server-name.png/);
  assert.doesNotMatch(textOf(c.get('chat-delivery-evidence')), /local-name/);
  // A composition already in progress can commit after a receipt arrives.
  input.value = '나중에 작성한 한글'; input.events.compositionstart(); input.events.input();
  entry.attachments = [{ id: 'new-file', name: '다음 초안.txt', type: 'text/plain', size: 4 }];
  receipt = { ...receipt, status: 'accepted', confirmed: true };
  await c.loadChatState(selected.s, true);
  assert.equal(entry.pending, null);
  assert.equal(entry.text, '나중에 작성한 한글'); assert.equal(input.value, entry.text);
  assert.equal(entry.composing, true);
  assert.equal(entry.attachments[0].id, 'new-file');
  assert.match(textOf(c.get('chat-delivery-evidence')), /전송 접수.*server-name.png/);
  assert.match(textOf(c.get('chat-attachments')), /다음 초안.txt/);
  assert.match(c.get('chat-attachments-label').textContent, /전송 전 첨부/);
});

test('a pre-dispatch rejection keeps attachments editable and never labels them as sent', async () => {
  let entry;
  const c = client(async (url) => url === '/api/chat/send'
    ? { ok: false, status: 409, json: async () => ({ error: '세션을 먼저 확인해 주세요.', dispatchState: 'not_started' }) }
    : { ok: true, status: 200, json: async () => attachmentState(entry) }, noChatTimers);
  const selected = selectChat(c); entry = selected.entry;
  wireTree(c, c.buildChatComposer(selected.s));
  entry.attachments = [{ id: 'draft-file', name: '미전송.txt', type: 'text/plain', size: 3 }];
  await c.sendChat(entry); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(entry.pending, null);
  assert.equal(entry.attachments.length, 1);
  assert.equal(c.get('chat-delivery-evidence').hidden, true);
  assert.match(c.get('chat-attachments-label').textContent, /전송 전 첨부/);
  assert.equal(byClass(c.get('chat-attachments'), 'chat-attachment-remove')[0].disabled, false);
  assert.equal(c.get('messages').children.length, 0);
});

test('selecting readable Codex, closed Codex and native Claude uses one state read independently of send capability', async () => {
  for (const mode of ['codex', 'closed-codex', 'native-claude', 'empty-codex']) {
    const calls = [];
    const c = client(async (url, options) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
        capability: { supported: mode === 'codex' || mode === 'empty-codex' }, phase: 'idle',
        messages: mode === 'empty-codex' ? [] : [{ role: 'assistant', text: mode + ' readable transcript' }] }) };
    }, noChatTimers);
    const { s } = selectChat(c, { agent: mode === 'native-claude' ? 'claude' : 'codex' });
    if (mode === 'native-claude') s.nativeSource = true;
    c.refreshDetail(s);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(calls, ['/api/chat/state'], mode);
    assert.doesNotMatch(textOf(c.get('messages')), /불러오고 있습니다/);
    assert.match(textOf(c.get('messages')), mode === 'empty-codex' ? /저장된 대화를 아직/ : /readable transcript/);
    if (mode === 'closed-codex' || mode === 'native-claude') assert.equal(c.get('chat-input').disabled, true);
  }
});

test('reselecting a conversation renders current memory before transport and preserves reading position and Korean draft during refresh', async () => {
  let defer = false; let release; let calls = 0;
  const c = client(async (url, options) => {
    calls++; assert.equal(url, '/api/chat/state');
    if (defer) await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
      capability: { supported: true }, phase: 'idle', messages: [
        { role: 'assistant', text: 'previously read reply' }, ...(defer ? [{ role: 'assistant', text: 'new appended reply' }] : []),
      ] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c); s.cwd = '/example/work';
  c.refreshDetail(s); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 1);
  entry.text = '작성해 둔 초안';
  const other = selectChat(c, { id: 'other-session' });
  c.drawDetail(other.s);
  selectChat(c); s.cwd = '/example/work';
  c.state.snapshot.hosts[0].data.codex[0] = s;
  c.state.detailFor = null; defer = true;
  c.refreshDetail(s);
  const list = c.get('messages'); const input = c.get('chat-input'); const cachedRow = list.children[0];
  assert.match(textOf(list), /previously read reply/);
  assert.equal(c.get('conversation-sync-notice').hidden, false);
  assert.match(c.get('conversation-sync-notice').textContent, /저장된 대화.*최신 내용 확인 중/);
  assert.equal(input.value, '작성해 둔 초안');
  list.scrollHeight = 900; list.clientHeight = 200; list.scrollTop = 40;
  input.value = '갱신 중 한글 조합'; input.events.compositionstart(); input.events.input();
  release(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(list.children[0], cachedRow); assert.equal(list.scrollTop, 40);
  assert.match(textOf(list), /new appended reply/);
  assert.equal(c.get('conversation-sync-notice').hidden, true);
  assert.equal(c.get('chat-input'), input); assert.equal(entry.composing, true); assert.equal(entry.text, input.value);
});

test('failed refresh keeps the cached transcript with an explicit stale notice and successful retry clears it', async () => {
  let fail = false; const requests = [];
  const c = client(async (url, options) => {
    requests.push(url);
    if (fail) throw new Error('fixture read failed');
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
      capability: { supported: true }, phase: 'idle', messages: [{ role: 'assistant', text: 'cached reply to retain' }] }) };
  }, noChatTimers);
  const { s } = selectChat(c);
  c.refreshDetail(s); await new Promise((resolve) => setImmediate(resolve));
  fail = true; c.state.detailFor = null; c.refreshDetail(s);
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(textOf(c.get('messages')), /cached reply to retain/);
  assert.match(c.get('conversation-sync-notice').textContent, /저장된 대화.*최신 내용을 확인하지 못했습니다/);
  assert.equal(requests.filter((url) => url.startsWith('/api/read?')).length, 1);
  fail = false;
  c.get('messages').conversationError.children[1].events.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.get('conversation-sync-notice').hidden, true);
  assert.equal(c.get('messages').conversationError, null);
  assert.match(textOf(c.get('messages')), /cached reply to retain/);
});

test('memory transcripts do not cross auth epochs, cwd changes, account homes or Claude transcript IDs, including offline views', async () => {
  for (const boundary of ['auth', 'cwd', 'home', 'claude-session']) {
    const c = client(async (url, options) => ({ ok: true, status: 200, json: async () => ({
      route: JSON.parse(options.body).source, capability: { supported: false }, phase: 'idle',
      messages: [{ role: 'assistant', text: 'private cached transcript' }],
    }) }), noChatTimers);
    const { s } = selectChat(c, { agent: boundary === 'claude-session' ? 'claude' : 'codex' });
    s.cwd = '/example/original'; if (s.agent === 'claude') s.sessionId = 'original-transcript';
    c.refreshDetail(s); await new Promise((resolve) => setImmediate(resolve));
    if (boundary === 'auth') c.beginAuthEpoch();
    if (boundary === 'cwd') s.cwd = '/example/changed';
    if (boundary === 'home') {
      s.home = '.codex-different'; s.key = 'different-account'; c.state.selected = s.key;
    }
    if (boundary === 'claude-session') s.sessionId = 'different-transcript';
    c.state.offline = true; c.state.detailFor = null; c.refreshDetail(s);
    assert.doesNotMatch(textOf(c.get('messages')), /private cached transcript|불러오고 있습니다/, boundary);
    assert.match(textOf(c.get('messages')), /오프라인/, boundary);
    assert.equal(c.get('conversation-sync-notice').hidden, true, boundary);
  }
});

test('canonical detail loads join a current state read without queuing duplicate readback', async () => {
  let release; const calls = [];
  const c = client(async (url, options) => {
    calls.push(url); await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
      capability: { supported: true }, phase: 'idle', messages: [{ role: 'assistant', text: 'joined reply' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  const first = c.loadChatState(s, false);
  c.refreshDetail(s); await Promise.resolve();
  assert.equal(entry.recheckQueued, false);
  release(); await first; await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ['/api/chat/state']);
  assert.match(textOf(c.get('messages')), /joined reply/);
});

test('legacy fallback cannot overwrite a newer send or a newer independent native state read', async () => {
  for (const boundary of ['send', 'state', 'state-failure']) {
    let releaseLegacy; let stateReads = 0; let sends = 0;
    const c = client(async (url, options) => {
      if (url.startsWith('/api/read?')) {
        await new Promise((resolve) => { releaseLegacy = resolve; });
        if (boundary === 'state-failure') throw new Error('obsolete legacy failure');
        return { ok: true, status: 200, json: async () => ({ messages: [{ role: 'assistant', text: 'obsolete legacy reply' }] }) };
      }
      const body = JSON.parse(options.body);
      if (url === '/api/chat/send') {
        sends++;
        return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: { requestId: body.requestId, confirmed: true } }) };
      }
      stateReads++;
      return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'idle',
        ...(stateReads > 1 ? { messages: [{ role: 'assistant', text: 'new authoritative reply' }] } : {}),
      }) };
    }, noChatTimers);
    const { s, entry } = selectChat(c); c.drawDetail(s);
    const loading = c.loadDetail(s, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(typeof releaseLegacy, 'function');
    if (boundary === 'send') { entry.text = 'new send'; await c.sendChat(entry); }
    else await c.loadChatState(s, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.match(textOf(c.get('messages')), /new authoritative reply/);
    releaseLegacy(); await loading;
    assert.doesNotMatch(textOf(c.get('messages')), /obsolete legacy reply/);
    assert.doesNotMatch(textOf(c.get('messages')), /obsolete legacy failure/);
    assert.equal(c.get('conversation-sync-notice').hidden, true);
    assert.equal(stateReads, 2); assert.equal(sends, boundary === 'send' ? 1 : 0);
  }
});

test('legacy fallback responses and native memory reads from a locked epoch cannot restore the new login view', async () => {
  let release; let requests = 0;
  const c = client(async (url, options) => {
    requests++;
    if (url === '/api/chat/state') return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source, capability: { supported: false } }) };
    await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ messages: [{ role: 'assistant', text: 'old private transcript' }] }) };
  }, noChatTimers);
  const { s } = selectChat(c); c.drawDetail(s);
  const load = c.loadDetail(s, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests, 2); c.showLogin();
  const next = selectChat(c, { home: '.codex-new-login' }); c.drawDetail(next.s);
  c.get('chat-input').value = '새 로그인 초안'; c.get('chat-input').events.input();
  release(); await load;
  assert.doesNotMatch(textOf(c.get('messages')), /old private transcript/);
  assert.equal(c.state.chats.size, 1);
  assert.equal(next.entry.text, '새 로그인 초안');
  assert.equal(next.entry.conversation, null);
});

test('a cwd change during an in-flight read fetches the new view once and keeps its mounted Korean editor', async () => {
  let release; const calls = [];
  const c = client(async (url, options) => {
    calls.push(url); const ordinal = calls.length;
    if (ordinal === 1) await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source,
      capability: { supported: true }, phase: 'idle', messages: [{ role: 'assistant', text: ordinal === 1 ? 'obsolete cwd reply' : 'new cwd reply' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c); s.cwd = '/example/original';
  c.refreshDetail(s);
  const input = c.get('chat-input'); input.value = '계속 조합하는 한글'; input.events.compositionstart(); input.events.input();
  const updated = { ...s, cwd: '/example/changed' };
  c.state.snapshot.hosts[0].data.codex[0] = updated;
  c.refreshDetail(updated);
  assert.equal(c.get('chat-input'), input); assert.equal(calls.length, 1);
  release(); await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ['/api/chat/state', '/api/chat/state']);
  assert.match(textOf(c.get('messages')), /new cwd reply/);
  assert.doesNotMatch(textOf(c.get('messages')), /obsolete cwd reply/);
  assert.equal(entry.composing, true); assert.equal(entry.text, '계속 조합하는 한글');
  assert.equal(c.get('chat-input'), input);
});

function queueStateReply(source, queuedMessages, receipts = [], queueState = {}) {
  return { route: source, capability: { supported: true }, phase: 'working', messages: [], queuedMessages,
    queueState: { available: true, complete: true, total: queuedMessages.length, truncated: false, ...queueState }, receipts };
}
function queueItem(requestId, text, attachments = [], extra = {}) {
  return { requestId, queueId: 'queue-' + requestId, text, textTruncated: false, contentAvailable: true,
    attachments, status: 'queued', confirmed: true, ...extra };
}

function recentSession(id, fields = {}) {
  const s = { host: 'workstation', hostLabel: 'Workstation', agent: 'codex', home: '.codex-example', id,
    title: '검토할 작업', project: 'Sample project', account: 'Sample profile', phase: 'idle', updatedAt: 1000, ...fields };
  s.key = [s.host, s.agent, s.home || '', s.id].join('|');
  return s;
}
function setRecentSnapshot(c, sessions) {
  c.state.authenticated = true;
  c.state.snapshot = { serverTime: 1100, hosts: [{ name: 'workstation', label: 'Workstation', ok: true,
    data: { codex: sessions.filter((s) => s.agent === 'codex'), claude: sessions.filter((s) => s.agent === 'claude') } }] };
}

test('recent cards give project and full task separate priority while retaining device, status and account without transport or generated titles', () => {
  let calls = 0; const c = client(async () => { calls++; throw new Error('render must not fetch'); }, noChatTimers);
  const project = '매우 긴 프로젝트 라벨 '.repeat(8); const title = '긴 실제 작업 제목 '.repeat(12);
  const s = recentSession('layout', { project, title, snippet: '  ' + title + '  ', cwd: '/Users/example/work/service', phase: 'working', stale: true });
  setRecentSnapshot(c, [s]); c.state.selected = s.key;
  c.renderGroups([s], 1100);
  const card = byClass(c.get('groups'), 'card')[0];
  assert.equal(card.tagName, 'BUTTON'); assert.equal(card.type, 'button');
  // Three short lines: title, preview, then status · agent · project · time.
  assert.deepEqual(card.children.map((child) => child.className), ['card-title', 'card-snippet', 'card-provenance', 'card-goals', 'card-meta', 'card-chevron']);
  assert.equal(byClass(card, 'card-title')[0].textContent, title.trim());
  assert.equal(byClass(card, 'card-snippet')[0].hidden, true);
  const meta = byClass(card, 'card-meta')[0];
  assert.deepEqual(meta.children.map((child) => child.className.split(' ')[0]), ['card-unread', 'badge', 'terminal-mark', 'card-agent', 'card-execution', 'card-project', 'stale-tag', 'card-time']);
  assert.equal(byClass(card, 'card-unread')[0].hidden, true, 'no read marks kept yet: nothing is unread');
  assert.equal(byClass(card, 'card-execution')[0].hidden, true, '일반 대화에는 일회성 실행 표식을 표시하지 않는다');
  assert.equal(byClass(card, 'card-terminal')[0].hidden, true, '"터미널 열림" only with a live board terminal');
  assert.match(byClass(card, 'card-project')[0].textContent, new RegExp('^' + project.trim()));
  assert.equal(byClass(card, 'stale-tag')[0].hidden, false);
  assert.match(card.attributes['aria-label'], /프로젝트:.*작업:.*작업 중.*Codex.*기기: Workstation.*계정 설정: Sample profile.*마지막 확인 기준/s);
  assert.equal(card.attributes['aria-pressed'], 'true');
  assert.equal(byClass(card, 'agent').length, 0);
  assert.equal(byClass(card, 'card-agent')[0].textContent, 'Codex');
  // Device gets an independent readable badge; the initiator stays separate.
  assert.equal(byClass(card, 'card-device-label')[0].textContent, 'Workstation');
  assert.equal(byClass(card, 'card-device-icon')[0].attributes['aria-hidden'], 'true');
  assert.equal(byClass(card, 'card-initiator')[0].textContent, '출처 미확인');
  assert.match(textOf(card), /Workstation.*출처 미확인/);
  assert.doesNotMatch(textOf(card), /Sample profile/);
  assert.equal(calls, 0); assert.equal(c.state.chats.size, 0);
  assert.equal(s.title, title); assert.equal(s.cwd, '/Users/example/work/service');
});

test('recent cards expose only disambiguating folder suffixes, handle home and missing project labels honestly, and search actual cwd', () => {
  const c = client(undefined, noChatTimers);
  const rows = [
    recentSession('a', { project: 'Shared project', cwd: '/Users/source/work/team-a/repo/src' }),
    recentSession('b', { project: 'Shared project', cwd: '/home/other/work/team-b/repo/src' }),
    recentSession('simple', { project: 'Sessionholic', cwd: '/Users/source/projects/sessionholic' }),
    recentSession('home', { project: '~', cwd: '/Users/source', title: '(제목 없음)' }),
    recentSession('unknown', { project: '', cwd: '', title: '(이름 없음)' }),
  ];
  c.renderGroups(rows, 1100);
  const cards = byClass(c.get('groups'), 'card');
  // The folder suffix follows the project in the meta line only when it disambiguates.
  assert.equal(byClass(cards[0], 'card-project')[0].textContent, 'Shared project · team-a / repo / src');
  assert.equal(byClass(cards[1], 'card-project')[0].textContent, 'Shared project · team-b / repo / src');
  assert.match(cards[0].attributes['aria-label'], /작업 폴더: team-a \/ repo \/ src/);
  for (const card of cards) {
    assert.doesNotMatch(textOf(card) + card.attributes['aria-label'], /source|other|\/Users|\/home/);
  }
  assert.equal(byClass(cards[2], 'card-project')[0].textContent, 'Sessionholic');
  assert.equal(byClass(cards[3], 'card-project')[0].textContent, '홈 폴더');
  assert.equal(byClass(cards[4], 'card-project')[0].textContent, '프로젝트 확인 필요');
  assert.equal(byClass(cards[4], 'card-title')[0].textContent, '제목 없는 작업');
  c.state.search = 'work/team-b/repo/src';
  assert.equal(c.matches(rows[1], 1100), true); assert.equal(c.matches(rows[0], 1100), false);
  assert.equal(rows[0].project, 'Shared project'); // Folder hints never rename the project.
});

test('recent card updates retain mounted buttons, selection, keyboard focus and list scroll through group changes without stealing search focus', () => {
  const c = client(undefined, noChatTimers);
  const first = recentSession('first', { updatedAt: 1001 }); const second = recentSession('second', { updatedAt: 1002 });
  c.state.selected = first.key;
  c.renderGroups([first, second], 1100);
  const cards = byClass(c.get('groups'), 'card'); const selected = cards.find((card) => card.dataset.sessionKey === first.key);
  assert.equal(cards[0].dataset.sessionKey, first.key); // The same title: the session key keeps the order.
  selected.focus = function () { this.focusCount = (this.focusCount || 0) + 1; c.context.document.activeElement = this; };
  c.context.document.activeElement = selected;
  const pane = c.get('task-list-pane'); pane.scrollTop = 55;
  c.renderGroups([{ ...first, title: '수정된 작업 제목' }, second], 1100);
  assert.equal(byClass(c.get('groups'), 'card').find((card) => card.dataset.sessionKey === first.key), selected);
  assert.equal(c.context.document.activeElement, selected); assert.equal(pane.scrollTop, 55);
  const create = c.context.document.createElement;
  c.context.document.createElement = (tag) => {
    const made = create(tag); const insert = made.insertBefore;
    made.insertBefore = function (child, before) {
      if (child === selected.cardRow) { c.context.document.activeElement = c.context.document.body; pane.scrollTop = 0; }
      return insert.call(this, child, before);
    };
    return made;
  };
  c.renderGroups([{ ...first, phase: 'needs_input' }, second], 1100);
  assert.equal(byClass(c.get('groups'), 'card')[0], selected);
  assert.equal(selected.attributes['aria-pressed'], 'true');
  assert.equal(c.context.document.activeElement, selected); assert.equal(selected.focusCount, 1); assert.equal(pane.scrollTop, 55);
  const search = c.get('search'); c.context.document.activeElement = search;
  c.renderGroups([{ ...first, phase: 'needs_input', account: 'Updated profile' }, second], 1100);
  assert.equal(c.context.document.activeElement, search); assert.equal(selected.focusCount, 1);
});

test('card activation keeps exact home identity and one initial read, while removed and prior-authentication buttons cannot activate later sessions', async () => {
  const requests = [];
  const c = client(async (url, options) => { requests.push({ url, body: JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => queueStateReply(JSON.parse(options.body).source, [], [], { total: 0 }) };
  }, noChatTimers);
  const first = recentSession('same-session', { home: '.codex-first', project: 'Same project', title: 'Same title' });
  const second = recentSession('same-session', { home: '.codex-second', project: 'Same project', title: 'Same title' });
  setRecentSnapshot(c, [first, second]); c.renderGroups(c.allSessions(), 1100);
  const cards = byClass(c.get('groups'), 'card'); const oldFirst = cards[0]; const oldSecond = cards[1];
  assert.notEqual(oldFirst, oldSecond); assert.notEqual(oldFirst.dataset.sourceKey, oldSecond.dataset.sourceKey);
  oldSecond.events.click(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.state.selected, second.key); assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/chat/state'); assert.equal(requests[0].body.source.home, '.codex-second');
  c.state.snapshot.hosts[0].data.codex = [first]; vm.runInContext('render()', c.context);
  oldSecond.events.click(); assert.equal(c.state.selected, null); assert.equal(requests.length, 1);
  c.showLogin(); setRecentSnapshot(c, [first]); c.renderGroups(c.allSessions(), 1100);
  const newFirst = byClass(c.get('groups'), 'card')[0];
  assert.notEqual(newFirst, oldFirst); oldFirst.events.click(); assert.equal(requests.length, 1);
  newFirst.events.click(); await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2); assert.equal(requests[1].body.source.home, '.codex-first');
});

test('changing list filters removes hidden cards without changing the selected source and different preview text stays explicitly a conversation preview', () => {
  const c = client(undefined, noChatTimers);
  const first = recentSession('preview-a', { title: '설정 점검', snippet: '이전에 확인한 대화의 본문', project: 'First project' });
  const second = recentSession('preview-b', { project: 'Second project' });
  c.state.selected = first.key; c.renderGroups([first, second], 1100);
  const original = byClass(c.get('groups'), 'card').find((card) => card.dataset.sessionKey === first.key);
  assert.equal(byClass(original, 'card-snippet')[0].textContent, '이전에 확인한 대화의 본문');
  assert.match(original.attributes['aria-label'], /대화 미리보기: 이전에 확인한 대화의 본문/);
  c.state.filters.project = 'Second project'; c.renderGroups([second], 1100);
  assert.equal(byClass(c.get('groups'), 'card').length, 1);
  assert.equal(c.state.selected, first.key);
  assert.equal(byClass(c.get('groups'), 'card')[0].dataset.sessionKey, second.key);
});

test('terminal keys sit in two rows with held modifiers, an inverted-T arrow cluster and the requested control keys', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');
  for (const key of ['escape', 'tab', 'backtab', 'interrupt', 'ctrl-j', 'ctrl-u', 'ctrl-x', 'ctrl-l', 'up', 'left', 'down', 'right', 'enter'])
    assert.match(html, new RegExp(`data-key="${key}"`));
  for (const mod of ['ctrl', 'alt', 'meta'])
    assert.match(html, new RegExp(`data-mod="${mod}" aria-pressed="false"`));
  assert.match(css, /"ctrl k1 k2 k3 k4 meta up\s+\.\s+enter"/);
  assert.match(css, /"alt\s+k5 k6 k7 k8 left down right enter"/);
  // Modifier buttons are not keys: a key loop over every button would send undefined.
  assert.ok(source.includes('querySelectorAll("button[data-key]")'));
  for (const [key, bytes] of [['backtab', '\\x1b[Z'], ['"ctrl-j"', '\\n'], ['"ctrl-u"', '\\x15'], ['"ctrl-x"', '\\x18'], ['"ctrl-l"', '\\x0c']])
    assert.ok(source.includes(`${key}: "${bytes}"`), key);
});

test('terminal keys encode modifiers, cursor and function keys as a terminal does, and ⌘ edits the line by host', () => {
  const c = client(undefined, noChatTimers);
  const k = (key, mods = {}, windows = false) => c.terminalKeyBytes(key, mods, { windows });
  const cases = [
    ['c', { ctrl: true }, '\x03'], [' ', { ctrl: true }, '\x00'], ['[', { ctrl: true }, '\x1b'], ['2', { ctrl: true }, '\x00'],
    ['?', { ctrl: true }, '\x7f'], ['x', { alt: true }, '\x1bx'], ['c', { ctrl: true, alt: true }, '\x1b\x03'],
    ['ArrowUp', {}, '\x1b[A'], ['ArrowUp', { shift: true }, '\x1b[1;2A'], ['ArrowLeft', { ctrl: true }, '\x1b[1;5D'],
    ['Home', {}, '\x1b[H'], ['End', { ctrl: true }, '\x1b[1;5F'], ['PageUp', {}, '\x1b[5~'], ['Delete', { ctrl: true }, '\x1b[3;5~'],
    ['F1', {}, '\x1bOP'], ['F4', { shift: true }, '\x1b[1;2S'], ['F5', {}, '\x1b[15~'], ['F12', { ctrl: true }, '\x1b[24;5~'],
    ['Tab', {}, '\t'], ['Tab', { shift: true }, '\x1b[Z'], ['Enter', { alt: true }, '\x1b\r'], ['Escape', {}, '\x1b'],
    ['Backspace', {}, '\x7f'], ['Backspace', { ctrl: true }, '\x08'], ['UIKeyInputUpArrow', {}, '\x1b[A'],
    // macOS editing on a Mac host.
    ['ArrowLeft', { meta: true }, '\x01'], ['ArrowRight', { meta: true }, '\x05'], ['Backspace', { meta: true }, '\x15'],
    ['ArrowLeft', { alt: true }, '\x1bb'], ['ArrowRight', { alt: true }, '\x1bf'], ['Backspace', { alt: true }, '\x1b\x7f'],
    ['c', { meta: true }, null], ['1', { ctrl: true }, null], ['ArrowUp', { meta: true }, null],
  ];
  for (const [key, mods, bytes] of cases) assert.equal(k(key, mods), bytes, `${key} ${JSON.stringify(mods)}`);
  // The Windows console's own keys on a Windows host.
  for (const [key, mods, bytes] of [['ArrowLeft', { meta: true }, '\x1b[H'], ['ArrowRight', { meta: true }, '\x1b[F'],
    ['Backspace', { meta: true }, '\x1b[1;5H'], ['ArrowLeft', { alt: true }, '\x1b[1;5D'], ['ArrowRight', { alt: true }, '\x1b[1;5C'],
    ['Backspace', { alt: true }, '\x08'], ['c', { ctrl: true }, '\x03']])
    assert.equal(k(key, mods, true), bytes, `windows ${key} ${JSON.stringify(mods)}`);
});

test('a hardware keyboard reaches the CLI: by key place, without taking a typed draft\'s editing or the browser\'s ⌘ keys', () => {
  const c = client(undefined, noChatTimers);
  const key = (fields, options) => c.terminalKeyFromEvent({ key: '', code: '', ...fields }, options);
  c.context.navigator = { platform: 'MacIntel' };
  assert.equal(key({ key: 'ㅊ', code: 'KeyC', ctrlKey: true }, { draftEmpty: false }), '\x03', 'a Korean layout still means Ctrl+C');
  assert.equal(key({ key: 'å', code: 'KeyA', altKey: true }, { draftEmpty: false }), null, '⌥ prints into the input bar');
  assert.equal(key({ key: '\\', code: 'Backslash', altKey: true }, { draftEmpty: true }), null, '⌥₩ is \\ on a Korean Mac');
  assert.equal(key({ key: 'å', code: 'KeyA', altKey: true }, { focusless: true }), '\x1ba', 'with nothing focused ⌥ is Meta');
  c.state.terminalMods = { ctrl: false, alt: true, meta: false };
  assert.equal(key({ key: 'b', code: 'KeyB' }, { draftEmpty: false }), '\x1bb', "the bar's ⌥ is meant for the CLI");
  c.state.terminalMods = { ctrl: false, alt: false, meta: false };
  assert.equal(key({ key: 'ArrowLeft' }, { draftEmpty: false }), null, 'a typed draft keeps its cursor');
  assert.equal(key({ key: 'ArrowLeft' }, { draftEmpty: true }), '\x1b[D');
  assert.equal(key({ key: 'Tab' }, { draftEmpty: false }), null, 'Tab leaves a typed draft (no keyboard trap)');
  assert.equal(key({ key: 'Escape' }, { draftEmpty: false }), null);
  assert.equal(key({ key: 'Escape' }, { draftEmpty: true }), '\x1b');
  assert.equal(key({ key: 'F2' }, { draftEmpty: false }), '\x1bOQ', 'function keys always');
  assert.equal(key({ key: 'PageDown' }, { draftEmpty: false }), '\x1b[6~');
  assert.equal(key({ key: 'v', code: 'KeyV', metaKey: true }, { draftEmpty: true }), null, '⌘V stays the browser paste');
  assert.equal(key({ key: 'ArrowLeft', metaKey: true }, { draftEmpty: true }), '\x01');
  for (const fields of [{ key: 'ArrowLeft', metaKey: true }, { key: 'Backspace', metaKey: true },
    { key: 'Backspace', altKey: true }, { key: 'ArrowRight', altKey: true }, { key: 'ArrowLeft', ctrlKey: true }])
    assert.equal(key(fields, { draftEmpty: false }), null, 'a typed draft keeps ⌘/⌥/Ctrl editing: ' + JSON.stringify(fields));
  assert.equal(key({ key: 'Backspace', altKey: true }, { draftEmpty: true }), '\x1b\x7f');
  assert.equal(key({ key: 'a' }, { draftEmpty: true }), null, 'typing goes to the input bar');
  assert.equal(key({ key: 'a' }, { focusless: true }), 'a', 'with nothing focused it goes to the CLI');
  assert.equal(key({ key: 'Enter' }, { draftEmpty: true }), null, "the input bar's own Enter");
  assert.equal(key({ key: 'Enter' }, { focusless: true }), '\r');
  assert.equal(key({ key: 'ㅎ', isComposing: true }, { focusless: true }), null);
  assert.equal(key({ key: 'Process', keyCode: 229 }, { focusless: true }), null);
  assert.equal(key({ key: 'Shift', shiftKey: true }, { focusless: true }), null);
  // A PC browser keeps Ctrl+C/V for a typed draft; an empty one interrupts.
  c.context.navigator = { platform: 'Win32' };
  assert.equal(key({ key: 'v', code: 'KeyV', ctrlKey: true }, { draftEmpty: false }), null);
  assert.equal(key({ key: 'v', code: 'KeyV', ctrlKey: true }, { draftEmpty: true }), null, 'Ctrl+V pastes into an empty bar too');
  assert.equal(key({ key: 'c', code: 'KeyC', ctrlKey: true }, { draftEmpty: true }), '\x03');
  // AltGr (Ctrl+Alt on a PC) prints its character.
  assert.equal(key({ key: '@', code: 'KeyQ', ctrlKey: true, altKey: true }, { draftEmpty: false }), null);
  assert.equal(key({ key: '@', code: 'KeyQ', ctrlKey: true, altKey: true }, { focusless: true }), '@');
  // A PC's Alt types nothing: it stays Meta for the CLI (Claude Code's Alt+P, Alt+T), and Alt+←
  // in a typed draft never becomes the browser's Back.
  assert.equal(key({ key: 'p', code: 'KeyP', altKey: true }, { draftEmpty: true }), '\x1bp');
  assert.equal(key({ key: 'p', code: 'KeyP', altKey: true }, { draftEmpty: false }), '\x1bp');
  assert.equal(key({ key: 'ArrowLeft', altKey: true }, { draftEmpty: false }), '\x1bb');
  assert.equal(key({ key: 'ArrowLeft', ctrlKey: true }, { draftEmpty: false }), null, 'Ctrl+← moves by word in the draft');
  // A Mac: Ctrl+V reaches the CLI (it pastes images there).
  c.context.navigator = { platform: 'MacIntel' };
  assert.equal(key({ key: 'v', code: 'KeyV', ctrlKey: true }, { draftEmpty: true }), '\x16');
  assert.equal(key({ key: 'π', code: 'KeyP', altKey: true }, { draftEmpty: true }), null, "a Mac's ⌥ prints π");
});

test('held bar modifiers combine with the next key from the bar, the input bar or xterm, then release', async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, noChatTimers);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.pressTerminalKey('up');
  await settle();
  assert.deepEqual(calls, ['\x1b[A'], 'a bar key alone sends its own bytes');
  c.toggleTerminalMod('ctrl');
  assert.equal(c.state.terminalMods.ctrl, true);
  c.pressTerminalKey('up');
  await settle();
  assert.equal(calls.at(-1), '\x1b[1;5A');
  assert.equal(c.state.terminalMods.ctrl, false, 'released after one key');
  c.toggleTerminalMod('alt');
  c.pressTerminalKey('interrupt');
  await settle();
  assert.equal(calls.at(-1), '\x1b\x03');
  // A character typed into the input bar with Ctrl held.
  c.toggleTerminalMod('ctrl');
  let prevented = false;
  c.draftBeforeInput({ inputType: 'insertText', data: 'r', preventDefault() { prevented = true; } });
  await settle();
  assert.equal(prevented, true);
  assert.equal(calls.at(-1), '\x12');
  assert.equal(c.state.terminalMods.ctrl, false);
  const before = calls.length;
  c.draftBeforeInput({ inputType: 'insertText', data: 'r', preventDefault() { throw new Error('no modifier'); } });
  await settle();
  assert.equal(calls.length, before, 'plain typing stays in the draft');
  // xterm: its own keys pass through; ⌘← and a held modifier are ours, and its keypress is swallowed.
  assert.equal(c.xtermKeyEvent({ type: 'keydown', key: 'a', code: 'KeyA' }), true);
  assert.equal(c.xtermKeyEvent({ type: 'keydown', key: 'ArrowLeft', metaKey: true, preventDefault() {} }), false);
  assert.equal(c.xtermKeyEvent({ type: 'keypress', key: 'ArrowLeft', metaKey: true }), false);
  await settle();
  assert.equal(calls.at(-1), '\x01');
  c.toggleTerminalMod('ctrl');
  assert.equal(c.xtermKeyEvent({ type: 'keydown', key: 'd', code: 'KeyD', preventDefault() {} }), false);
  await settle();
  assert.equal(calls.at(-1), '\x04');
  assert.equal(c.xtermKeyEvent({ type: 'keydown', key: 'c', code: 'KeyC', metaKey: true }), true, '⌘C copies');
});

test('with nothing focused a hardware keyboard types into the shown terminal, Ctrl+K included, but not past an open dialog', async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, noChatTimers);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.state.terminalVisible = true;
  c.context.document.activeElement = null;
  const event = (fields) => ({ key: '', code: '', target: { closest: () => null }, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, ...fields });
  assert.equal(c.terminalDocumentKey(event({ key: 'x', code: 'KeyX' })), true);
  assert.equal(c.terminalDocumentKey(event({ key: 'k', code: 'KeyK', ctrlKey: true })), true);
  await settle();
  assert.deepEqual(calls, ['x', '\x0b']);
  c.context.document.activeElement = { tagName: 'INPUT' };
  assert.equal(c.terminalDocumentKey(event({ key: 'y', code: 'KeyY' })), false, 'a focused field types for itself');
  c.context.document.activeElement = null;
  assert.equal(c.terminalDocumentKey(event({ key: 'y', code: 'KeyY', target: { closest: (q) => (q.includes('.xterm') ? {} : null) } })), false,
    'xterm sends its own keys');
  c.state.terminalVisible = false;
  assert.equal(c.terminalDocumentKey(event({ key: 'y', code: 'KeyY' })), false);
});

test('a focused control and Tab keep their keys, and a held bar modifier never waits for a later key', async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, noChatTimers);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.state.terminalVisible = true;
  const event = (fields) => ({ key: '', code: '', target: { closest: () => null }, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, ...fields });
  // A button (보드, 닫기…) has the focus: its Enter, Space and Tab are its own.
  c.context.document.activeElement = { tagName: 'BUTTON' };
  for (const key of ['Enter', ' ', 'Tab', 'ArrowDown'])
    assert.equal(c.terminalDocumentKey(event({ key })), false, key);
  c.toggleTerminalMod('ctrl');
  assert.equal(c.terminalDocumentKey(event({ key: ' ' })), false);
  assert.equal(c.state.terminalMods.ctrl, false, 'a key left to a control releases a held modifier');
  // Nothing focused: Tab still moves the focus to the page's controls.
  c.context.document.activeElement = null;
  c.toggleTerminalMod('ctrl');
  assert.equal(c.terminalDocumentKey(event({ key: 'Tab' })), false);
  assert.equal(c.state.terminalMods.ctrl, false, 'Ctrl does not wait past Tab to interrupt a later c');
  assert.equal(c.terminalDocumentKey(event({ key: 'Tab', shiftKey: true })), false);
  // ⌘ held on the bar and a letter it does not combine with: the letter types, ⌘ is released.
  c.toggleTerminalMod('meta');
  assert.equal(c.terminalDocumentKey(event({ key: 'a', code: 'KeyA' })), false);
  assert.equal(c.state.terminalMods.meta, false);
  c.toggleTerminalMod('ctrl');
  c.draftKeydown({ key: '1', code: 'Digit1', target: { value: '' }, preventDefault() {} });
  assert.equal(c.state.terminalMods.ctrl, false, 'Ctrl+1 is nothing: released before the next key');
  c.toggleTerminalMod('alt');
  c.draftKeydown({ key: 'Shift', code: 'ShiftLeft', shiftKey: true, target: { value: '' }, preventDefault() {} });
  c.draftKeydown({ key: 'Process', keyCode: 229, target: { value: '' }, preventDefault() {} });
  assert.equal(c.state.terminalMods.alt, true, 'a modifier or a composing key is not the next key');
  c.clearTerminalMods();
  c.toggleTerminalMod('ctrl');
  c.draftBeforeInput({ inputType: 'insertText', data: '1', preventDefault() { throw new Error('typed as it is'); } });
  assert.equal(c.state.terminalMods.ctrl, false, 'a soft keyboard key Ctrl does not combine with: typed, Ctrl released');
  c.toggleTerminalMod('ctrl');
  c.startDraftComposition({ target: c.get('terminal-draft') });
  assert.equal(c.state.terminalMods.ctrl, false);
  assert.match(c.get('draft-note').textContent, /보조키가 풀립니다/, 'released with a word, not silently');
  c.endDraftComposition?.({ target: c.get('terminal-draft'), data: '' });
  c.state.composing = false;
  // ⌫ held from a typed draft stops at its start; held from an empty one it goes on.
  const press = (value, repeat) => {
    const keyEvent = { key: 'Backspace', code: 'Backspace', repeat, target: { value }, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } };
    c.draftKeydown(keyEvent);
    return keyEvent.defaultPrevented;
  };
  assert.equal(press('hi', false), false);
  assert.equal(press('', true), false, 'the draft emptied under a held ⌫: the CLI line is not touched');
  assert.equal(press('', false), true);
  assert.equal(press('', true), true);
  await settle();
  assert.deepEqual(calls, ['\x7f', '\x7f']);
});

test('a Windows host terminal hides ⌘ and names ⌥ Alt, and its word keys are the console\'s', () => {
  const c = client(undefined, noChatTimers);
  const meta = { dataset: { mod: 'meta' }, setAttribute() {}, hidden: false, textContent: '⌘' };
  const alt = { dataset: { mod: 'alt' }, setAttribute() {}, hidden: false, textContent: '⌥' };
  c.get('terminal-keys').querySelectorAll = (selector) => (selector === 'button[data-mod]' ? [meta, alt] : []);
  c.state.capabilities = { hosts: [{ name: 'pc', platform: 'windows' }, { name: 'mac' }] };
  c.state.terminal = { id: 'term-1', host: 'pc' };
  c.renderTerminalMods();
  assert.equal(meta.hidden, true);
  assert.equal(alt.textContent, 'Alt');
  assert.equal(c.terminalKeyFromEvent({ key: 'ArrowLeft', code: 'ArrowLeft', altKey: true }, { draftEmpty: true }), '\x1b[1;5D');
  c.state.terminal = { id: 'term-2', host: 'mac' };
  c.renderTerminalMods();
  assert.equal(meta.hidden, false);
  assert.equal(alt.textContent, '⌥');
});

test('the input bar sends a paste then a separate Enter, while input-only never adds Enter', async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.state.term = { modes: { bracketedPasteMode: true }, scrollToBottom() {} };
  c.get('terminal-draft').value = '테스트 돌려줘';
  await c.pasteDraft(true);
  assert.deepEqual(calls, ['\x1b[200~테스트 돌려줘\x1b[201~', '\r']);
  assert.equal(c.get('terminal-draft').value, '');
  calls.length = 0;
  c.get('terminal-draft').value = '1';
  await c.pasteDraft(false);
  assert.deepEqual(calls, ['\x1b[200~1\x1b[201~']);
  assert.match(c.get('draft-note').textContent, /Enter/);
});

test('Enter in the input bar sends; Shift+Enter and IME composition keep editing', async () => {
  const calls = [];
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).text);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  });
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.state.term = { modes: { bracketedPasteMode: true }, scrollToBottom() {} };
  c.get('terminal-draft').value = '계속해';
  let prevented = 0;
  const key = (extra) => ({ key: 'Enter', preventDefault() { prevented++; }, ...extra });
  c.draftKeydown(key({ shiftKey: true }));
  c.draftKeydown(key({ isComposing: true }));
  c.draftKeydown(key({ keyCode: 229 }));
  assert.equal(prevented, 0);
  c.draftKeydown(key({}));
  assert.equal(prevented, 1);
  await new Promise((resolve) => setTimeout(resolve, 250));
  assert.deepEqual(calls, ['\x1b[200~계속해\x1b[201~', '\r']);
});

test('touch devices keep the xterm keyboard closed unless direct terminal input is chosen', () => {
  const c = client();
  c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' });
  const field = node(); field.removeAttribute = function (name) { delete this.attributes[name]; };
  c.state.term = { textarea: field };
  c.configureTerminalKeyboard();
  assert.equal(field.attributes.inputmode, 'none');
  c.state.directTerminalInput = true;
  c.configureTerminalKeyboard();
  assert.equal(field.attributes.inputmode, undefined);
  c.context.window.matchMedia = () => ({ matches: false });
  c.state.directTerminalInput = null;
  c.configureTerminalKeyboard();
  assert.equal(field.attributes.inputmode, undefined);
});

test('a swipe made on one terminal never scrolls or marks the next one', async () => {
  const calls = [];
  const timers = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const c = client(async (url, opts) => {
    calls.push([url, JSON.parse(opts.body)]);
    await gate;
    return { ok: true, status: 200, json: async () => ({ mode: 'history', position: 7 }) };
  }, { setTimeout: (callback) => { timers.push(callback); return timers.length; }, clearTimeout() {} });
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.state.terminalScrollPending = -3;
  const flight = c.flushTerminalScroll();
  c.queueTerminalScroll(-2);
  // Another terminal opens while the reply for the first is on its way.
  c.state.terminal = { id: 'term-2' };
  c.resetTerminalScroll();
  release();
  await flight;
  assert.deepEqual(calls, [['/api/terminal/term-1/scroll', { lines: -3 }]]);
  assert.equal(c.state.terminalScrollLines, 0, "the first terminal's position is not the second's");
  assert.equal(c.state.terminalScrollPending, 0, 'a swipe left over is not sent to the second');
  assert.equal(timers.length, 0);
  // The first terminal's request does not hold up the second's swipes, even while it is on its way.
  let releaseFirst;
  const slow = new Promise((resolve) => { releaseFirst = resolve; });
  const d = client(async (url) => {
    calls.push([url]);
    if (url.includes('term-1')) await slow;
    return { ok: true, status: 200, json: async () => ({ mode: 'history', position: 4 }) };
  }, { setTimeout: (callback) => { timers.push(callback); return timers.length; }, clearTimeout() {} });
  d.state.connected = true;
  d.state.terminal = { id: 'term-1' };
  d.state.terminalScrollPending = -3;
  const first = d.flushTerminalScroll();
  d.state.terminal = { id: 'term-2' };
  d.resetTerminalScroll();
  d.queueTerminalScroll(-4);
  assert.equal(timers.length, 1, 'the second terminal schedules its own swipe');
  await timers.pop()();
  assert.deepEqual(calls.at(-1), ['/api/terminal/term-2/scroll']);
  assert.equal(d.state.terminalScrollLines, 4);
  releaseFirst();
  await first;
  assert.equal(d.state.terminalScrollLines, 4, "the first terminal's late reply changes nothing");
  assert.equal(d.state.terminalScrollInFlight, false);
});

test('terminal swipes coalesce into tmux history scrolls that never pass below the live screen', async () => {
  const calls = [];
  const timers = [];
  const c = client(async (url, opts) => {
    calls.push([url, JSON.parse(opts.body)]);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, { setTimeout: (callback) => { timers.push(callback); return timers.length; }, clearTimeout() {} });
  c.state.connected = true;
  c.state.terminal = { id: 'term-1' };
  c.queueTerminalScroll(-2.5); c.queueTerminalScroll(-1);
  assert.equal(timers.length, 1);
  await c.flushTerminalScroll();
  assert.deepEqual(calls.at(-1), ['/api/terminal/term-1/scroll', { lines: -3 }]);
  assert.equal(c.state.terminalScrollLines, 3);
  c.state.terminalScrollPending = 0;
  c.queueTerminalScroll(10);
  await c.flushTerminalScroll();
  assert.deepEqual(calls.at(-1), ['/api/terminal/term-1/scroll', { lines: 3 }]);
  assert.equal(c.state.terminalScrollLines, 0);
  const count = calls.length;
  c.state.terminalScrollPending = 0;
  c.queueTerminalScroll(5);
  await c.flushTerminalScroll();
  assert.equal(calls.length, count);
  c.state.terminalScrollLines = 4;
  await c.terminalScrollEnd();
  assert.deepEqual(calls.at(-1), ['/api/terminal/term-1/scroll-end', {}]);
  assert.equal(c.state.terminalScrollLines, 0);
});

test('waiting answers are counted in the tab title for every visible session', () => {
  const c = client();
  c.renderAttention([{ phase: 'needs_input' }, { phase: 'needs_input' }, { phase: 'working' }]);
  assert.equal(c.context.document.title, '(2) 답 필요 · 세션홀릭');
  c.renderAttention([{ phase: 'working' }]);
  assert.equal(c.context.document.title, '세션홀릭 - 에이전트 세션 매니저');
});

test('a message cut at the server limit says so and copies as a visible part only', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'assistant', text: '앞부분 …', ts: 1, truncated: true, fullLength: 12000 },
    { role: 'assistant', text: '짧은 답변', ts: 2 }]);
  const text = textOf(c.get('messages'));
  assert.match(text, /긴 메시지라 앞부분만 표시합니다 · 전체 12,000자/);
  assert.equal((text.match(/앞부분만 표시/g) || []).length, 1);
  const [cut, whole] = c.get('messages').children;
  assert.equal(menuLabels(c, cut)[0], '표시된 부분 복사');
  assert.equal(menuLabels(c, whole)[0], '복사');
});

test('pull to refresh reloads the board once and never overlaps a running refresh', async () => {
  const requests = [];
  let release;
  const c = client(async (url) => {
    requests.push(url);
    if (url === '/api/refresh') await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ ok: true, hosts: [], terminals: [] }) };
  }, { setTimeout: (callback, delay) => (delay === 700 ? setImmediate(callback) : 1), clearTimeout() {} });
  c.state.authenticated = true;
  const first = c.pullRefresh();
  const second = c.pullRefresh();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.filter((url) => url === '/api/refresh').length, 1);
  assert.equal(c.state.pullRefreshing, true);
  release();
  await new Promise((resolve) => setImmediate(resolve));
  // A short confirmation is shown before the indicator slides away.
  assert.match(c.get('pull-refresh-label').textContent, /업데이트됨|오프라인/);
  await Promise.all([first, second]);
  assert.equal(c.state.pullRefreshing, false);
  assert.equal(requests[0], '/api/refresh');
});

test('a request with no answer within its limit fails as a timeout, so a held read never blocks the next; sends and launches wait longer', async () => {
  const limits = [];
  const c = client((path, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' })));
  }), noChatTimers);
  c.context.AbortSignal = { timeout(ms) { const controller = new AbortController(); limits.push({ ms, controller }); return controller.signal; } };
  const read = c.api('/api/snapshot');
  assert.equal(limits[0].ms, 30000);
  limits[0].controller.abort();
  await assert.rejects(read, (err) => err.timeout === true && /응답이 늦어 기다리기를 멈췄습니다/.test(err.message));
  const send = c.api('/api/chat/send', { method: 'POST', body: {} }).catch((err) => err);
  const launch = c.api('/api/launch', { method: 'POST', body: {} }).catch((err) => err);
  const remoteRead = c.api('/api/read?host=pc&agent=codex&id=fixture').catch((err) => err);
  assert.deepEqual(limits.slice(1).map((item) => item.ms), [90000, 660000, 45000], 'a query string does not hide the route');
  // The caller's own abort still ends the request, and is not called a timeout.
  const caller = new AbortController();
  const own = c.api('/api/terminals', { signal: caller.signal });
  caller.abort();
  await assert.rejects(own, (err) => err.name === 'AbortError' && !err.timeout);
  for (const item of limits) item.controller.abort();
  assert.equal((await send).timeout, true); assert.equal((await remoteRead).timeout, true);
  const late = await launch;
  assert.equal(late.timeout, true);
  assert.match(late.message, /이미 시작됐거나 전달됐을 수 있으니 목록과 터미널을 확인/, 'a launch may have started: no blind retry');
});

test('pull to refresh waits for a list read in flight, then says it could not refresh when the read fails', async () => {
  const requests = [];
  let release = null;
  const c = client(async (url) => {
    requests.push(url);
    if (url.startsWith('/api/snapshot')) {
      if (!release) await new Promise((resolve) => { release = resolve; });
      throw new TypeError('Load failed');
    }
    return { ok: true, status: 200, json: async () => ({ ok: true, hosts: [], terminals: [] }) };
  }, { setTimeout: (callback, delay) => (delay === 700 ? setImmediate(callback) : 1), clearTimeout() {} });
  c.state.authenticated = true;
  const label = c.get('pull-refresh-label'); const seen = []; let text = '';
  Object.defineProperty(label, 'textContent', { get: () => text, set: (value) => { text = value; seen.push(value); } });
  const inFlight = c.poll(false);
  await new Promise((resolve) => setImmediate(resolve));
  const pulled = c.pullRefresh();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.includes('/api/refresh'), false); // The read in flight is waited for first.
  release();
  assert.equal(await inFlight, false);
  await pulled;
  assert.ok(requests.indexOf('/api/refresh') > requests.indexOf('/api/snapshot'));
  assert.equal(requests.filter((url) => url.startsWith('/api/snapshot')).length, 2);
  assert.ok(seen.includes('새로고침하지 못했습니다'));
  assert.ok(!seen.includes('업데이트됨'));
  assert.ok(c.state.pollFailures >= 2);
});

test('a page older than the screen the board serves offers a reload, and reloads on coming back when nothing would be lost', async () => {
  const capabilities = { csrfToken: 'csrf-test', webVersion: 'bbbb2222' };
  const c = client(async () => ({ ok: true, status: 200, json: async () => capabilities }), noChatTimers,
    { currentScript: { src: 'https://board.test/app.js?v=aaaa1111' } });
  let reloaded = 0;
  c.context.window.location = { reload: () => { reloaded += 1; } };
  c.state.authenticated = true; c.state.snapshot = { hosts: [] };
  c.state.shownAt = 0; // Not just back on the page: the banner offers it.
  await c.loadCapabilities(c.state.authEpoch, { refreshView: false });
  assert.equal(c.state.updateAvailable, true);
  assert.equal(c.get('update-banner').hidden, false);
  assert.equal(reloaded, 0);
  // Something unsent in a composer, or a terminal on screen: coming back still only offers it.
  c.state.chats.set('draft', { text: '보내지 않은 말', attachments: [] });
  assert.equal(c.unsavedWork(), true);
  assert.equal(c.reloadForUpdate(), false);
  c.state.chats.clear();
  c.state.terminalVisible = true;
  assert.equal(c.reloadForUpdate(), false);
  c.state.terminalVisible = false;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reloaded, 0);
  // Offline, or with a sheet open, it waits too.
  c.context.navigator = { onLine: false };
  assert.equal(c.networkOffline(), true);
  assert.equal(c.reloadForUpdate(), false);
  c.context.navigator = { onLine: true };
  c.context.document.querySelector = (selector) => (selector === 'dialog[open]' ? {} : null);
  assert.equal(c.reloadForUpdate(), false);
  c.context.document.querySelector = () => null;
  // The banner's tap keeps unsent words too.
  c.state.chats.set('draft', { text: '보내지 않은 말', attachments: [] });
  c.updateBannerPressed();
  assert.match(c.get('toast').textContent, /보내지 않은 글이나 첨부가 있어 새로고침하지 않았습니다/);
  c.state.chats.clear();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reloaded, 0);
  // Nothing would be lost: coming back loads the newer screen.
  assert.equal(c.reloadForUpdate(), true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(reloaded, 1);
  // Found right after coming back, it loads at once.
  const back = client(async () => ({ ok: true, status: 200, json: async () => capabilities }), noChatTimers,
    { currentScript: { src: 'https://board.test/app.js?v=aaaa1111' } });
  let again = 0;
  back.context.window.location = { reload: () => { again += 1; } };
  back.state.shownAt = Date.now();
  await back.loadCapabilities(back.state.authEpoch, { refreshView: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(again, 1);
  // The same screen, or a page that cannot tell its own, offers nothing.
  const same = client(async () => ({ ok: true, status: 200, json: async () => ({ csrfToken: 'x', webVersion: 'aaaa1111' }) }),
    noChatTimers, { currentScript: { src: 'https://board.test/app.js?v=aaaa1111' } });
  await same.loadCapabilities(same.state.authEpoch, { refreshView: false });
  assert.equal(same.state.updateAvailable, false);
  const unknown = client(async () => ({ ok: true, status: 200, json: async () => capabilities }), noChatTimers);
  await unknown.loadCapabilities(unknown.state.authEpoch, { refreshView: false });
  assert.equal(unknown.state.updateAvailable, false);
});

test('a device that lost its connection comes first among the header chips, and a tap says each one\'s state', () => {
  const c = client(undefined, noChatTimers);
  c.state.snapshot = { serverTime: 1000, hosts: [
    { name: 'studio', label: '작업 컴퓨터', ok: true, fetchedAt: 990 },
    { name: 'laptop', label: '노트북', ok: true, refreshing: true, fetchedAt: 900 },
    { name: 'pc', label: 'PC', ok: false, error: 'SSH 연결 실패', fetchedAt: 600 },
  ] };
  c.render();
  const chips = c.get('hosts').children;
  // A collection in progress only pulses its dot: it does not move ahead.
  assert.deepEqual(chips.map((chip) => chip.children[1].textContent), ['PC', '작업 컴퓨터', '노트북']);
  assert.deepEqual(chips.map((chip) => chip.className), ['host bad', 'host ok', 'host checking']);
  assert.equal(chips[0].tagName, 'BUTTON');
  chips[0].events.click();
  assert.match(c.get('toast').textContent, /^PC · SSH 연결 실패 · 마지막 확인/);
  // A lost device being checked again keeps its place in front.
  c.state.snapshot.hosts[2].refreshing = true; c.render();
  assert.equal(c.get('hosts').children[0].children[1].textContent, 'PC');
  c.state.snapshot.hosts[2].refreshing = false;
  // A warning (a PC project server off) puts a reachable device right after a lost one; a
  // collector's informational errors do not, though a tap still says them.
  c.state.snapshot.hosts[0].data = { warnings: ['PC 프로젝트 Codex 서버가 꺼져 있어 그 프로젝트의 실행 상태를 확인하지 못했습니다: fixture.'] };
  c.state.snapshot.hosts[1].data = { errors: ['일부 작업을 아직 읽지 못했습니다.'] };
  c.render();
  const again = c.get('hosts').children;
  assert.deepEqual(again.map((chip) => chip.className), ['host bad', 'host warn', 'host checking']);
  again[1].events.click();
  assert.match(c.get('toast').textContent, /^작업 컴퓨터 · .* 확인 · PC 프로젝트 Codex 서버가 꺼져/);
});

test('a PC record says why its state is what it is under the conversation title', () => {
  const c = client(undefined, noChatTimers);
  const base = { agent: 'codex', hostLabel: 'PC', account: '기기 Codex', updatedAt: 100, phase: 'unknown', runtimeKnown: false };
  const reason = '이 프로젝트의 Codex 서버가 PC에서 꺼져 있어 실행 상태를 확인하지 못했습니다.';
  const parts = c.detailMetaChildren({ ...base, runtimeReason: reason });
  assert.equal(parts[0].textContent, '실행 확인 전');
  assert.equal(parts.at(-1).className, 'detail-runtime'); assert.equal(parts.at(-1).textContent, reason);
  assert.ok(!c.detailMetaChildren(base).some((part) => part.className === 'detail-runtime'));
});

test('pulling while a refresh runs shares it instead of calling it failed', async () => {
  const requests = [];
  let release;
  const c = client(async (url) => {
    requests.push(url);
    if (url === '/api/refresh') await new Promise((resolve) => { release = resolve; });
    return { ok: true, status: 200, json: async () => ({ ok: true, hosts: [], terminals: [] }) };
  }, { setTimeout: (callback, delay) => (delay === 700 ? setImmediate(callback) : 1), clearTimeout() {} });
  c.state.authenticated = true;
  const label = c.get('pull-refresh-label'); const seen = []; let text = '';
  Object.defineProperty(label, 'textContent', { get: () => text, set: (value) => { text = value; seen.push(value); } });
  const pressed = c.manualRefresh();
  await new Promise((resolve) => setImmediate(resolve));
  const pulled = c.pullRefresh();
  await new Promise((resolve) => setImmediate(resolve));
  release();
  assert.equal(await pressed, true);
  await pulled;
  assert.equal(requests.filter((url) => url === '/api/refresh').length, 1);
  assert.ok(seen.includes('업데이트됨')); assert.ok(!seen.includes('새로고침하지 못했습니다'));
  // While a launch is prepared nothing is tried, and the pull says so.
  c.state.launchBusy = true;
  seen.length = 0;
  assert.equal(await c.manualRefresh(), null);
  await c.pullRefresh();
  assert.ok(seen.some((value) => /잠시 뒤 다시 당겨/.test(value)));
});

test('a hardware keyboard goes from the search field into the list, between cards, and back from a conversation', () => {
  const c = client(undefined, noChatTimers);
  const focused = [], clicked = [];
  const cards = [0, 1, 2].map((index) => ({ index, dataset: { sessionKey: 'card-' + index }, classList: { contains: (name) => name === 'card' }, closest: () => null,
    focus() { focused.push(index); }, click() { clicked.push(index); } }));
  c.get('groups').querySelectorAll = (selector) => (selector === '.card' ? cards : []);
  const search = { id: 'search', closest: () => null };
  const press = (target, key, extra = {}) => {
    const event = { key, target, preventDefault() { this.prevented = true; }, ...extra };
    return [c.listNavigationKeydown(event), !!event.prevented];
  };
  assert.deepEqual(press(search, 'ArrowDown'), [true, true]); assert.deepEqual(focused, [0]);
  assert.deepEqual(press(search, 'Enter'), [true, true]); assert.deepEqual(clicked, [0]);
  assert.deepEqual(press(cards[0], 'ArrowDown'), [true, true]); assert.deepEqual(focused, [0, 1]);
  assert.deepEqual(press(cards[1], 'ArrowUp'), [true, true]); assert.deepEqual(focused, [0, 1, 0]);
  // Composition, a modifier or a terminal keep their keys.
  assert.deepEqual(press(search, 'Enter', { isComposing: true }), [false, false]);
  assert.deepEqual(press(cards[0], 'ArrowDown', { shiftKey: true }), [false, false]);
  c.state.terminalVisible = true;
  assert.deepEqual(press(search, 'ArrowDown'), [false, false]);
  c.state.terminalVisible = false;
  // Back from a conversation: Esc outside a field, or ⌘[; never Esc while typing.
  c.state.selected = 'workstation|codex|.codex|fixture';
  const typing = { closest: (selector) => (selector.includes('textarea') ? {} : null) };
  assert.deepEqual(press(typing, 'Escape'), [false, false]);
  assert.deepEqual(press({ closest: () => null }, 'Escape'), [true, true]);
  assert.deepEqual(press(typing, '[', { metaKey: true }), [true, true]);
  assert.deepEqual(press({ closest: (selector) => (selector === 'dialog' ? {} : null) }, 'Escape'), [false, false]);
  // A key already taken (Esc closing the tools menu) or one an input method is composing is left alone.
  assert.deepEqual(press({ closest: () => null }, 'Escape', { defaultPrevented: true }), [false, false]);
  assert.deepEqual(press(search, 'ArrowDown', { keyCode: 229 }), [false, false]);
  // An iPad's key names are understood.
  focused.length = 0;
  assert.deepEqual(press(search, 'UIKeyInputDownArrow'), [true, true]); assert.deepEqual(focused, [0]);
  // On a touch screen without a hardware keyboard, Return from the search field only closes the keyboard.
  c.context.window.matchMedia = (query) => ({ matches: /coarse/.test(query) });
  c.state.hardwareKeyboard = false;
  assert.equal(vm.runInContext('keyboardPresent()', c.context), false);
  clicked.length = 0;
  assert.deepEqual(press(search, 'Enter'), [false, false]); assert.deepEqual(clicked, []);
  // Once a hardware key was seen, Enter opens it again.
  c.state.hardwareKeyboard = true;
  assert.deepEqual(press(search, 'Enter'), [true, true]); assert.deepEqual(clicked, [0]);
});

test('a conversation at the read window says the items before it are not shown', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  const items = (count) => Array.from({ length: count }, (unused, index) => ({ role: index % 2 ? 'assistant' : 'user', text: `항목 ${index}`, ts: index + 1 }));
  const notice = () => c.get('messages').children.find((node) => node.className === 'conversation-window-notice');
  c.renderMessages(s, items(79));
  assert.equal(notice(), undefined);
  c.renderMessages(s, items(80));
  assert.match(notice().textContent, /최근 80개 항목까지만 보입니다. 그보다 앞의 대화는 여기에 나오지 않습니다/);
  assert.equal(c.get('messages').children[0], notice(), 'above the conversation');
});

test('a session that answered or finished since it was last opened shows unread until it is opened, on every device', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return { ok: true, status: 200, json: async () => ({ seenAt: 1050 }) };
  }, noChatTimers);
  const now = 2000;
  const done = recentSession('done', { phase: 'done', updatedAt: 1050 });
  c.state.snapshot = { serverTime: now, sessionSeen: { ok: true, since: 1000 }, hosts: [] };
  assert.equal(c.sessionUnread(done, now), true);
  assert.equal(c.sessionUnread({ ...done, seenAt: 1050 }, now), false, 'opened since (on any device)');
  assert.equal(c.sessionUnread({ ...done, updatedAt: 900 }, now), false, 'before the board kept marks');
  assert.equal(c.sessionUnread({ ...done, phase: 'working' }, now), false, 'still working');
  assert.equal(c.sessionUnread({ ...done, phase: 'needs_input' }, now), true, 'waiting for an answer');
  c.state.snapshot.sessionSeen = { ok: true, since: null };
  assert.equal(c.sessionUnread(done, now), false, 'nothing is unread before the first mark');
  c.state.snapshot.sessionSeen = { ok: true, since: 1000 };
  c.renderGroups([done], now);
  const card = byClass(c.get('groups'), 'card')[0];
  assert.equal(byClass(card, 'card-unread')[0].hidden, false);
  assert.match(card.cardLabel, /^읽지 않음\./);
  c.markSeen(done);
  c.markSeen(done);
  await new Promise((resolve) => setImmediate(resolve));
  const marks = requests.filter((request) => request.url === '/api/session/seen');
  assert.equal(marks.length, 1);
  assert.deepEqual(marks[0].body, JSON.parse(JSON.stringify({ source: c.sourceOf(done), updatedAt: 1050 })));
  assert.equal(c.sessionUnread(done, now), false, 'counted at once on this page');
  c.renderGroups([done], now);
  assert.equal(byClass(byClass(c.get('groups'), 'card')[0], 'card-unread')[0].hidden, true);
});

test('a mark the board refuses brings the dot back and waits a minute; a Claude conversation without a worker id is named by its conversation', async () => {
  const requests = [];
  let refuse = true;
  const c = client(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url === '/api/session/seen' && refuse) return { ok: false, status: 400, json: async () => ({ error: 'fixture refusal' }) };
    return { ok: true, status: 200, json: async () => ({}) };
  }, noChatTimers);
  const now = 2000;
  c.state.snapshot = { serverTime: now, sessionSeen: { ok: true, since: 1000 }, hosts: [] };
  const done = recentSession('done', { phase: 'done', updatedAt: 1050 });
  c.markSeen(done);
  assert.equal(c.sessionUnread(done, now), false, 'counted at once');
  for (let turn = 0; turn < 3; turn++) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(c.sessionUnread(done, now), true, 'refused: the dot comes back');
  c.markSeen(done);
  assert.equal(requests.filter((request) => request.url === '/api/session/seen').length, 1, 'not tried again within a minute');
  refuse = false;
  const conversation = '019c0000-0000-7000-8000-000000000009';
  c.markSeen({ host: 'workstation', agent: 'claude', id: '', sessionId: conversation, kind: 'interactive',
    phase: 'done', updatedAt: 1060, key: 'workstation|claude||' + conversation });
  const body = requests.at(-1).body;
  assert.deepEqual([body.source.id, body.source.sessionId, body.source.home], [conversation, conversation, '.claude']);
});

test('quick replies come from this browser\'s list (the defaults until edited) and are offered for the input, never sent', () => {
  const store = new Map();
  const c = client(undefined, noChatTimers);
  c.context.localStorage = { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) };
  assert.equal(c.quickReplies()[0], '계속 진행해 줘.');
  const saved = c.saveQuickReplies(' 첫째 문구 \n\n둘째 문구\n' + Array.from({ length: 20 }, (unused, index) => `문구 ${index}`).join('\n'));
  assert.equal(saved.length, 12);
  assert.deepEqual([...saved.slice(0, 2)], ['첫째 문구', '둘째 문구']);
  assert.deepEqual([...c.quickReplies()], [...saved]);
  const { entry } = selectChat(c);
  const input = c.get('chat-input');
  c.openQuickReplies(entry, input, null);
  const items = c.get('action-list').children;
  assert.deepEqual(items.slice(0, 2).map((item) => item.textContent), ['첫째 문구', '둘째 문구']);
  assert.match(c.get('action-eyebrow').textContent, /보내지 않음/);
  c.saveQuickReplies('');
  assert.equal(c.quickReplies().length, 0);
  c.openQuickReplies(entry, input, null);
  assert.equal(c.get('action-list').children[0].disabled, true);
});

function classes() {
  const set = new Set();
  return { set, add: (name) => set.add(name), remove: (name) => set.delete(name), contains: (name) => set.has(name),
    toggle: (name, force) => { const on = force ?? !set.has(name); on ? set.add(name) : set.delete(name); return on; } };
}

test('reading mode folds after deliberate scrolling and stays folded at the newest message', () => {
  const c = client(undefined, noChatTimers);
  const pane = c.get('detail'); pane.classList = classes();
  const list = c.get('messages'); Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 2400 });
  c.get('jump-bottom');
  c.readingScroll(list);
  list.scrollTop = 2390; c.readingScroll(list);
  assert.equal(pane.classList.contains('is-reading'), false, 'a 10px wobble keeps the chrome');
  list.scrollTop = 2350; c.readingScroll(list);
  assert.equal(pane.classList.contains('is-reading'), true, 'a 50px upward read folds it');
  assert.equal(c.get('jump-bottom').hidden, true, 'the jump appears only once the end is out of reach');
  list.scrollTop = 1500; c.readingScroll(list);
  assert.equal(c.get('jump-bottom').hidden, false);
  list.scrollTop = 2300; c.readingScroll(list);
  assert.equal(pane.classList.contains('is-reading'), true, 'reading downward keeps the space');
  list.scrollTop = 2395; c.readingScroll(list);
  assert.equal(pane.classList.contains('is-reading'), true, 'the newest message preserves the focus choice');
  c.toggleChrome();
  assert.equal(pane.classList.contains('is-reading'), false, 'explicit restore shows the tools');
  assert.equal(c.get('jump-bottom').hidden, true);
});

test('messages arriving while reading back are counted on the jump button and cleared at the bottom', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  const list = c.get('messages'); const jump = c.get('jump-bottom'); jump.classList = classes();
  c.renderMessages(s, [{ role: 'assistant', text: '첫 답', ts: 1 }], '', true);
  Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 100 });
  c.renderMessages(s, [{ role: 'assistant', text: '첫 답', ts: 1 }, { role: 'user', text: '질문', ts: 2 },
    { role: 'tool', text: '$ ls', ts: 3 }, { role: 'assistant', text: '둘째 답', ts: 4 }]);
  assert.equal(list.unread, 2, 'tool rows are not counted');
  assert.match(jump.textContent, /새 메시지 2/);
  assert.equal(jump.classList.contains('has-unread'), true);
  list.scrollTop = 2400; c.readingScroll(list);
  assert.equal(list.unread, 0);
  assert.equal(jump.textContent, '↓');
});

test('the new-message count skips a row that only got its time and this board\'s own message that replaced its bubble', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  const list = c.get('messages');
  const now = Date.now(); const ts = Math.floor(now / 1000);
  const shown = [{ role: 'user', text: '요청', ts: ts - 60 }, { role: 'assistant', text: '중간 보고' }];
  c.renderMessages(s, shown, '', true);
  Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 100 });
  const untimed = { role: 'assistant', text: '최종 답' };
  c.renderMessages(s, [...shown, untimed]);
  assert.equal(list.unread, 1);
  // The turn ended: the same answer now carries the turn's end time.
  c.renderMessages(s, [...shown, { ...untimed, ts: ts - 1 }]);
  assert.equal(list.unread, 1, 'the same answer, counted once');
  // A message sent from here reaches the transcript while the reader is above.
  const seen = [...shown, { ...untimed, ts: ts - 1 }];
  entry.outbox = [{ requestId: 'req-own-0001', text: '내 질문', attachments: [], state: 'sent', status: 'accepted', reason: '',
    at: now, ...c.outboxOrigin(seen) }];
  c.renderMessages(s, [...seen, { role: 'user', text: '내 질문', ts: ts + 1 }]);
  assert.equal(entry.outbox.length, 0);
  assert.equal(list.unread, 1, 'its own message is no news');
  c.renderMessages(s, [...seen, { role: 'user', text: '내 질문', ts: ts + 1 }, { role: 'assistant', text: '답', ts: ts + 5 }]);
  assert.equal(list.unread, 2, 'the answer to it is');
});

test('two turns that start with the same command keep their own tool groups when the window drops the first', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  // Codex tool records carry no time: both turns' first records have the same key.
  const turn = (n) => [{ role: 'user', text: `요청 ${n}`, ts: 1000 + n * 100 },
    { role: 'tool', text: '$ npm test', toolKind: 'command' }, { role: 'tool', text: `파일 수정 · ${n}.py`, toolKind: 'edit' },
    { role: 'assistant', text: `답 ${n}`, ts: 1050 + n * 100 }];
  const groups = () => list.children.filter((row) => /tool-group/.test(row.className));
  c.renderMessages(s, [...turn(1), ...turn(2), ...turn(3)]);
  const [first, second, third] = groups();
  first.toolToggle.events.click();
  third.toolToggle.events.click();
  assert.deepEqual(groups().map((group) => group.open), [true, false, true]);
  c.renderMessages(s, [...turn(2), ...turn(3)]);
  assert.deepEqual(groups(), [second, third], 'each turn keeps its own group');
  assert.deepEqual(groups().map((group) => group.open), [false, true]);
  assert.deepEqual(second.toolList.children.map((item) => textOf(item).trim()), ['$ npm test', '파일 수정 · 2.py']);
});

test('pull distance is elastic and the large title collapses into the bar on phones only', () => {
  const c = client(undefined, noChatTimers);
  assert.equal(c.pullDistance(0), 0);
  assert.ok(c.pullDistance(80) < 80 && c.pullDistance(300) < 110 && c.pullDistance(300) > c.pullDistance(150));
  c.context.document.body.classList = classes();
  c.context.window.matchMedia = (query) => ({ matches: query === '(max-width: 700px)' });
  const pane = { scrollTop: 0 };
  const collapsed = () => c.context.document.body.classList.contains('list-collapsed');
  for (const top of [10, 30]) { pane.scrollTop = top; c.listScroll(pane); }
  assert.equal(collapsed(), false, 'the large title is still visible');
  pane.scrollTop = 80; c.listScroll(pane);
  assert.equal(collapsed(), true, 'past the title the bar shows the small title');
  pane.scrollTop = 500; c.listScroll(pane);
  assert.equal(collapsed(), true, 'the bar never slides away while reading down');
  pane.scrollTop = 0; c.listScroll(pane);
  assert.equal(collapsed(), false);
  c.context.window.matchMedia = () => ({ matches: false });
  pane.scrollTop = 800; c.listScroll(pane);
  assert.equal(collapsed(), false, 'wide screens keep their plain bar');
  const html = indexPage();
  assert.match(html, /id="nav-title"[^>]*aria-label="세션 목록 맨 위로"[^>]*>세션</);
  assert.match(styleSheet(), /body\.list-collapsed \.top \{[^}]*backdrop-filter: saturate\(180%\) blur\(20px\)/);
});

test('an app that owns its screen scrolls in both directions, while tmux history stops at the live screen', async () => {
  const calls = [];
  let mode = 'app';
  const c = client(async (url, opts) => {
    calls.push(JSON.parse(opts.body).lines);
    return { ok: true, status: 200, json: async () => (mode === 'app' ? { ok: true, mode: 'app', position: null }
      : { ok: true, mode: 'history', position: 7 }) };
  }, { setTimeout: () => 1, clearTimeout() {} });
  c.state.connected = true; c.state.terminal = { id: 'term-1' };
  c.state.terminalScrollPending = 6; await c.flushTerminalScroll();
  assert.deepEqual(calls, [6], 'app mode can scroll down from the live screen');
  assert.equal(c.state.terminalScrollLines, 0);
  mode = 'history'; c.state.terminalScrollMode = null;
  c.state.terminalScrollPending = -9; await c.flushTerminalScroll();
  assert.equal(c.state.terminalScrollLines, 7, 'the server position is shown');
  assert.equal(c.get('terminal-live').hidden, false);
  assert.match(c.get('terminal-live').textContent, /7줄 위/);
  c.state.terminalScrollPending = 20; await c.flushTerminalScroll();
  assert.equal(calls.at(-1), 7, 'history never scrolls below the live screen');
});

function findClass(root, name, out = []) {
  if (typeof root?.className === 'string' && root.className.split(' ').includes(name)) out.push(root);
  for (const child of root?.children || []) findClass(child, name, out);
  return out;
}

test('a question card keeps the keyboard on the chosen option, and arrows move the choice as in a radio group', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  const prompt = { agent: 'claude', fingerprint: 'e'.repeat(64), current: 1, multi: false, terminalId: 'a'.repeat(32),
    question: 'Do you want to proceed?', options: [{ number: 1, label: 'Yes', detail: '' },
      { number: 2, label: 'Yes, always', detail: '' }, { number: 3, label: 'No', detail: '' }] };
  entry.data = { ...entry.data, prompt, phase: 'needs_input' };
  c.renderPrompt(s, entry.data);
  const options = () => findClass(c.get('chat-prompt'), 'chat-prompt-option');
  assert.deepEqual(options().map((item) => item.tabIndex), [0, -1, -1], 'one stop in the tab order');
  c.context.document.activeElement = options()[0];
  options()[0].events.click();
  assert.equal(options()[0].focusCount, 1, 'the redrawn choice takes the focus back');
  c.context.document.activeElement = options()[0];
  options()[0].events.keydown({ key: 'ArrowUp', preventDefault() {} });
  assert.equal(entry.promptChoice.number, 3, 'up from the first wraps to the last');
  assert.equal(options()[2].focusCount, 1);
  assert.deepEqual(options().map((item) => item.tabIndex), [-1, -1, 0]);
  c.context.document.activeElement = null;
});

test('a question card selects first and sends only on the second tap, with the exact menu fingerprint', async () => {
  const requests = [];
  const c = client(async (url, opts) => {
    requests.push([url, JSON.parse(opts.body)]);
    return { ok: true, status: 200, json: async () => (url === '/api/chat/prompt' ? { status: 'answered', prompt: null } : { messages: [] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  const prompt = { agent: 'claude', fingerprint: 'f'.repeat(64), current: 1, multi: false, terminalId: 'a'.repeat(32),
    question: 'Bash command\n  rm -rf build\nDo you want to proceed?',
    options: [{ number: 1, label: 'Yes', detail: '' }, { number: 2, label: "Yes, and don't ask again", detail: '' },
      { number: 3, label: 'No, and tell Claude what to do differently (esc)', detail: '' }] };
  entry.data = { ...entry.data, prompt, phase: 'needs_input' };
  c.renderPrompt(s, entry.data);
  const card = c.get('chat-prompt');
  assert.equal(card.hidden, false);
  assert.match(textOf(card), /Claude가 답을 기다려요/);
  assert.match(textOf(card), /rm -rf build/);
  const options = findClass(card, 'chat-prompt-option');
  assert.equal(options.length, 3);
  options[2].events.click();
  assert.equal(requests.length, 0, 'choosing alone sends nothing');
  assert.equal(entry.promptChoice.number, 3);
  const again = c.get('chat-prompt');
  const send = findClass(again, 'primary')[0];
  assert.match(send.textContent, /3번 보내기/);
  // A poll with the same menu keeps the selection instead of resetting it.
  c.renderPrompt(s, entry.data);
  assert.equal(entry.promptChoice.number, 3);
  await send.events.click();
  await new Promise((resolve) => setImmediate(resolve));
  const [url, body] = requests[0];
  assert.equal(url, '/api/chat/prompt');
  assert.deepEqual({ action: body.action, fingerprint: body.fingerprint, number: body.number },
    { action: 'answer', fingerprint: prompt.fingerprint, number: 3 });
  assert.equal(entry.promptChoice, null);
});

test('menus the web cannot drive stay readable with disabled options, and a waiting session can load its question', async () => {
  const requests = [];
  const c = client(async (url, opts) => {
    requests.push([url, JSON.parse(opts.body)]);
    return { ok: true, status: 200, json: async () => ({ prompt: null, messages: [] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  entry.data = { ...entry.data, phase: 'needs_input', prompt: { agent: 'codex', fingerprint: 'e'.repeat(64), current: 1, multi: true,
    question: '고를 항목', options: [{ number: 1, label: '[ ] A', detail: '' }, { number: 2, label: '[ ] B', detail: '' }] } };
  c.renderPrompt(s, entry.data);
  assert.ok(findClass(c.get('chat-prompt'), 'chat-prompt-option').every((option) => option.disabled));
  assert.match(textOf(c.get('chat-prompt')), /터미널에서 선택/);
  entry.data = { ...entry.data, prompt: null, promptOpenable: true };
  c.renderPrompt(s, entry.data);
  const open = findClass(c.get('chat-prompt'), 'primary')[0];
  assert.match(open.textContent, /질문 불러오기/);
  await open.events.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests[0][1].action, 'open');
  entry.data = { ...entry.data, promptOpenable: false, phase: 'working' };
  c.renderPrompt(s, entry.data);
  assert.equal(c.get('chat-prompt').hidden, true);
});

test('a 답 필요 session opened from its card or notification loads its question once, without another tap', async () => {
  const requests = [];
  const c = client(async (url, opts) => {
    requests.push([url, JSON.parse(opts.body || '{}')]);
    return { ok: true, status: 200, json: async () => ({ prompt: null, messages: [] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  const opens = () => requests.filter(([url, body]) => url === '/api/chat/prompt' && body.action === 'open').length;
  // Opened another way: the card waits for "질문 불러오기".
  entry.data = { ...entry.data, phase: 'needs_input', prompt: null, promptOpenable: true };
  c.renderPrompt(s, entry.data);
  await settle();
  assert.equal(opens(), 0);
  c.answerOnOpen(s);
  c.renderPrompt(s, { ...entry.data });
  await settle();
  assert.equal(opens(), 1);
  assert.equal(c.state.answerOnOpen, null);
  c.renderPrompt(s, { ...entry.data });
  await settle();
  assert.equal(opens(), 1, 'later renders never load it again by themselves');
  // A request that waited too long, or one for a question already shown, does nothing.
  c.answerOnOpen(s);
  c.state.answerOnOpen.until = Date.now() - 1;
  c.renderPrompt(s, { ...entry.data });
  await settle();
  assert.equal(opens(), 1);
  assert.equal(c.state.answerOnOpen, null);
});

test('a failed "질문 불러오기" says why on its card, and the note never follows to the next question', async () => {
  const c = client(async (url, opts) => {
    if (url === '/api/chat/prompt') return { ok: false, status: 409, json: async () => ({ error: '질문 화면을 불러오지 못했습니다.' }) };
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(opts.body).source, capability: { supported: true },
      phase: 'needs_input', prompt: null, promptOpenable: true, messages: [] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  entry.data = { ...entry.data, phase: 'needs_input', prompt: null, promptOpenable: true };
  c.renderPrompt(s, entry.data);
  await findClass(c.get('chat-prompt'), 'primary')[0].events.click();
  await settle();
  assert.match(textOf(c.get('chat-prompt')), /질문 화면을 불러오지 못했습니다/);
  // The question shows up later (the terminal was opened by hand): a fresh card.
  entry.data = { ...entry.data, prompt: { agent: 'codex', fingerprint: 'd'.repeat(64), current: 1, multi: false, question: '계속할까요?',
    options: [{ number: 1, label: 'Yes', detail: '' }, { number: 2, label: 'No', detail: '' }] } };
  c.renderPrompt(s, entry.data);
  assert.match(textOf(c.get('chat-prompt')), /계속할까요/);
  assert.doesNotMatch(textOf(c.get('chat-prompt')), /불러오지 못했습니다/);
});

test('a state read begun before an answer does not bring the answered question back', async () => {
  const releases = [];
  const prompt = { agent: 'codex', fingerprint: 'c'.repeat(64), current: 1, multi: false, question: '계속할까요?',
    options: [{ number: 1, label: 'Yes', detail: '' }, { number: 2, label: 'No', detail: '' }] };
  const c = client(async (url, opts) => {
    if (url === '/api/chat/prompt') return { ok: true, status: 200, json: async () => ({ status: 'answered', prompt: null }) };
    const route = JSON.parse(opts.body).source;
    // The first read saw the question; any later one does not (both wait to be released).
    const answer = releases.length ? { route, capability: { supported: true }, phase: 'working', prompt: null, messages: [] }
      : { route, capability: { supported: true }, phase: 'needs_input', prompt, messages: [] };
    return await new Promise((resolve) => releases.push(() => resolve({ ok: true, status: 200, json: async () => answer })));
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  entry.data = { ...entry.data, phase: 'needs_input', prompt };
  c.renderPrompt(s, entry.data);
  const stale = c.loadChatState(s, false);
  await settle();
  findClass(c.get('chat-prompt'), 'chat-prompt-option')[0].events.click();
  await findClass(c.get('chat-prompt'), 'primary')[0].events.click();
  await settle();
  assert.equal(c.get('chat-prompt').hidden, true, 'answered');
  releases[0]();
  await stale; await settle();
  assert.equal(c.get('chat-prompt').hidden, true, 'the read from before the answer is not drawn');
  assert.equal(releases.length, 2, 'a read after the answer follows');
  releases[1](); await settle();
  assert.equal(c.get('chat-prompt').hidden, true);
});

test('an input error (a file too large) stays until the user acts again; a state read does not wipe it', async () => {
  const c = client(async (url, opts) => ({ ok: true, status: 200, json: async () => ({ route: JSON.parse(opts.body).source,
    capability: { supported: true }, phase: 'working', receipts: [], messages: [] }) }), noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  entry.data = { ...entry.data, phase: 'working', maxAttachmentBytes: 10 };
  await c.uploadChatFiles(entry, [{ name: 'big.png', size: 100, type: 'image/png' }]);
  const status = () => c.get('chat-status').textContent;
  assert.match(status(), /big\.png.*최대/);
  await c.loadChatState(s, false);
  assert.match(status(), /big\.png.*최대/, 'an automatic read keeps it');
  await c.loadChatState(s, true);
  assert.match(status(), /big\.png.*최대/, 'so does a detail refresh');
  const input = c.get('chat-input'); input.value = '다른 파일로 보낼게요'; input.events.input();
  assert.doesNotMatch(status(), /최대/, 'editing the draft answers it');
  // A read's own error still goes with the next successful read.
  entry.error = '상태를 확인하지 못했습니다. 상태 다시 확인을 눌러 주세요.';
  await c.loadChatState(s, false);
  assert.doesNotMatch(status(), /확인하지 못했습니다/);
});

test('the machines chip says how many answer and the sheet shows each one\'s CPU, memory, disk, battery and uptime', async () => {
  const c = client(undefined, noChatTimers);
  const box = c.get('hardware');
  const ready = { name: 'board', label: '보드 기기', status: 'ready', refreshing: false, fetchedAt: 1791600000, error: null, system: 'macos',
    cpu: { model: 'Fixture M1', cores: 8, load: [1.5, 1.2, 1.0], percent: 37 }, memory: { total: 16 * 1024 ** 3, used: 4 * 1024 ** 3, percent: 25 },
    disk: { total: 500 * 1024 ** 3, free: 20 * 1024 ** 3, percent: 96 }, battery: { percent: 80, charging: true, power: 'ac' }, uptimeSeconds: 90000 };
  const offline = { name: 'office', label: '사무실 PC', status: 'offline', refreshing: false, fetchedAt: null,
    error: '연결하지 못했습니다. 잠자기·오프라인이면 깨어난 뒤 다시 읽습니다.' };
  c.renderHardware({ available: true, refreshing: false, hosts: [ready, offline] });
  assert.equal(box.hidden, false);
  assert.match(textOf(c.get("hardware-summary")), /기기\s*1\/2/);
  assert.match(c.get('hardware-summary').children[0].className, /red/, 'an unanswered machine or a full disk shows red');
  const text = textOf(c.get('hardware-body'));
  for (const part of ['보드 기기', '37%', 'Fixture M1 · 8코어 · 부하 1.5', '4.0GB / 16GB', '여유 20GB', '배터리 80% · 충전 중',
    '가동 1일 1시간', '사무실 PC', '연결 안 됨', '연결하지 못했습니다'])
    assert.ok(text.includes(part), part);
  const fills = [];
  const walk = (node) => { if (String(node.className || '').includes('usage-fill')) fills.push(node.className); (node.children || []).forEach(walk); };
  walk(c.get('hardware-body'));
  assert.deepEqual(fills, ['usage-fill green', 'usage-fill green', 'usage-fill red'], 'busier is warmer, the opposite of remaining usage');
  // A Mac's first reading has no CPU share yet: its load says how busy it is.
  c.renderHardware({ available: true, refreshing: true, hosts: [{ ...ready, cpu: { ...ready.cpu, percent: null } }] });
  assert.match(textOf(c.get('hardware-body')), /CPU.*부하 1\.5/);
  assert.match(textOf(c.get('hardware-body')), /갱신 중|읽는 중…/);
  // A board without the endpoint never shows the chip.
  const d = client(async () => ({ ok: false, status: 404, json: async () => ({}) }), noChatTimers);
  d.state.authenticated = true;
  await d.loadHardware();
  assert.equal(d.get('hardware').hidden, true);
  assert.equal(d.state.hardwareUnsupported, true);
});

test('the usage strip shows remaining limits per provider and hides on boards without the endpoint', async () => {
  const c = client(undefined, noChatTimers);
  const box = c.get('usage');
  c.renderUsage({ available: true, refreshing: false, fetchedAt: 1791600000, providers: [
    { id: 'codex', short: 'CX', label: 'Codex', remaining: 45, status: 'ready', rows: [
      { name: '업무', credits: '1,250', windows: [{ label: '5시간', remaining: null, resetsAt: null },
        { label: '주간', remaining: 68, resetsAt: '2026-10-14T13:00:00Z' }] }] },
    { id: 'claude', short: 'CC', label: 'Claude', remaining: 10, status: 'ready', rows: [
      { name: 'Claude Code', windows: [{ label: '5시간', remaining: 86, resetsAt: '2026-10-10T06:29:59Z' }] }] }] });
  assert.equal(box.hidden, false);
  const summary = textOf(c.get('usage-summary'));
  assert.match(summary, /CX\s*45%/); assert.match(summary, /CC\s*10%/);
  const body = textOf(c.get('usage-body'));
  assert.match(body, /크레딧 1,250/); assert.match(body, /68%/); assert.match(body, /10\/14 22:00 초기화/);
  assert.match(body, /—/);
  c.renderUsage({ available: false });
  assert.equal(box.hidden, true);
  const missing = client(async () => ({ ok: false, status: 404, json: async () => ({ error: '없는 경로입니다.' }) }), noChatTimers);
  missing.state.authenticated = true;
  await missing.loadUsage();
  assert.equal(missing.state.usageUnsupported, true);
  assert.equal(missing.get('usage').hidden, true);
});

// --- UI foundation: theme, motion, sheets, typing, terminal, assets, live updates ---
const settle = async (rounds = 3) => { for (let i = 0; i < rounds; i++) await new Promise((resolve) => setImmediate(resolve)); };
const styleSheet = () => fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');
const indexPage = () => fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
function cssBlock(css, selector) {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, selector);
  const end = css.indexOf('}', start);
  const tokens = {};
  for (const line of css.slice(css.indexOf('{', start) + 1, end).split(';')) {
    const match = /^\s*(--[\w-]+|color-scheme)\s*:\s*([\s\S]+?)\s*$/.exec(line);
    if (match) tokens[match[1]] = match[2].replace(/\s+/g, ' ');
  }
  return tokens;
}

test('every board color is a token, and dark values exist for the device setting and for a forced dark board', () => {
  const css = styleSheet().replace(/\/\*[\s\S]*?\*\//g, '');
  const tokenBlock = /^(:root(\[data-theme="(light|dark)"\]|:not\(\[data-theme="light"\]\))?)$/;
  const literals = [];
  const stack = [];
  let buffer = '';
  for (const char of css) {
    if (char === '{') { stack.push(buffer.trim()); buffer = ''; continue; }
    if (char === '}') {
      const selector = stack.pop();
      if (selector && !tokenBlock.test(selector))
        for (const declaration of buffer.split(';')) {
          const value = declaration.split(':').slice(1).join(':');
          if (/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\bwhite\b|\bblack\b/.test(value)) literals.push(`${selector} -> ${declaration.trim()}`);
        }
      buffer = '';
      continue;
    }
    buffer += char;
  }
  assert.deepEqual(literals, [], 'colors outside the token blocks');
  assert.match(css, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{/);
  const light = cssBlock(css, ':root');
  const system = cssBlock(css, ':root:not([data-theme="light"])');
  const forced = cssBlock(css, ':root[data-theme="dark"]');
  assert.deepEqual(system, forced, 'both dark blocks carry the same values');
  assert.equal(forced['color-scheme'], 'dark');
  for (const name of ['--bg', '--surface', '--label', '--label-3', '--accent', '--accent-fill', '--bubble', '--codex', '--toast-bg', '--backdrop', '--chevron-select'])
    assert.ok(forced[name] && forced[name] !== light[name], `${name} has its own dark value`);
  for (const name of Object.keys(forced)) if (name !== 'color-scheme') assert.ok(name in light, `${name} has a light value`);
  assert.equal(light['color-scheme'], 'light dark');
  assert.match(css, /:root\[data-theme="light"\] \{\s*color-scheme: light;/);
  const html = indexPage();
  assert.match(html, /name="color-scheme" content="light dark"/);
  assert.match(html, /name="theme-color" content="#f2f2f7" media="\(prefers-color-scheme: light\)"/);
  assert.match(html, /name="theme-color" content="#000000" media="\(prefers-color-scheme: dark\)"/);
});

test('the theme follows the device by default, forced themes set data-theme, only the choice is stored, and the terminal darkens the browser bar', () => {
  const c = client(undefined, noChatTimers);
  const root = c.context.document.documentElement;
  root.attributes = {};
  root.setAttribute = (name, value) => { root.attributes[name] = String(value); };
  root.removeAttribute = (name) => { delete root.attributes[name]; };
  const meta = (content) => ({ attributes: { content }, dataset: {}, getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; } });
  const metas = [meta('#f2f2f7'), meta('#000000')];
  c.context.document.querySelectorAll = (selector) => (selector === 'meta[name="theme-color"]' ? metas : []);
  const writes = [];
  c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  c.applyTheme('auto');
  assert.equal(root.attributes['data-theme'], undefined);
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#f2f2f7', '#000000']);
  c.setTheme('dark');
  assert.equal(root.attributes['data-theme'], 'dark');
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#000000', '#000000']);
  assert.equal(c.get('theme-dark').checked, true); assert.equal(c.get('theme-auto').checked, false);
  c.setTheme('dark');
  c.setTheme('sepia');
  assert.equal(c.state.theme, 'auto'); assert.equal(root.attributes['data-theme'], undefined);
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#f2f2f7', '#000000'], 'auto restores the media colors');
  assert.deepEqual(writes, [['sessionholic:ui:theme', '"dark"'], ['sessionholic:ui:theme', '"auto"']]);
  c.setTheme('light');
  c.state.terminalVisible = true; c.updateThemeColor();
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#1c1c1e', '#1c1c1e']);
  c.state.terminalVisible = false; c.updateThemeColor();
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#f2f2f7', '#f2f2f7']);
});

test('the settings sheet opens from the top bar with the current theme and returns focus when closed', () => {
  const c = client(undefined, noChatTimers);
  c.context.navigator = { onLine: false };
  c.windowEvents.DOMContentLoaded();
  c.state.theme = 'dark';
  c.get('settings').events.click();
  assert.equal(c.get('settings-dialog').open, true);
  assert.equal(c.get('theme-dark').checked, true);
  c.get('theme-light').events.change({ target: { checked: true } });
  assert.equal(c.state.theme, 'light');
  c.get('settings-done').events.click();
  assert.equal(c.get('settings-dialog').open, false);
  assert.equal(c.get('settings').focusCount, 1);
  let prevented = 0;
  c.get('settings').events.click();
  c.get('settings-dialog').events.cancel({ preventDefault() { prevented++; } });
  assert.equal(prevented, 1); assert.equal(c.get('settings-dialog').open, false);
});

test('haptics use vibration only where available and map selection, success and errors', async () => {
  const c = client(undefined, noChatTimers);
  assert.equal(c.haptic('select'), false, 'no navigator: silent');
  const calls = [];
  c.context.navigator = { vibrate: (pattern) => { calls.push(pattern); return true; } };
  c.haptic('select'); c.haptic('success'); c.haptic('error'); c.haptic('unknown');
  assert.equal(JSON.stringify(calls), JSON.stringify([8, [12, 60, 18], [30, 50, 30, 50, 30]]));
  c.context.navigator = { vibrate: () => { throw new Error('blocked'); } };
  assert.equal(c.haptic('select'), false);
  let fail = false;
  const d = client(async (url, options) => {
    if (url === '/api/chat/send' && fail) throw new Error('lost');
    return { ok: true, status: 200, json: async () => (url === '/api/chat/send' ? { status: 'accepted', receipt: {} }
      : { route: JSON.parse(options.body).source, capability: { supported: true }, phase: 'idle' }) };
  }, noChatTimers);
  const { entry } = selectChat(d);
  const pulses = [];
  d.context.navigator = { vibrate: (pattern) => { pulses.push(pattern); return true; } };
  entry.text = '보낼 메시지';
  await d.sendChat(entry);
  assert.equal(JSON.stringify(pulses), '[[12,60,18]]', 'an accepted send ticks success');
  fail = true; entry.pending = null; entry.text = '다시 보낼 메시지';
  await d.sendChat(entry);
  assert.equal(JSON.stringify(pulses.at(-1)), '[30,50,30,50,30]', 'an uncertain send buzzes an error');
});

test('screen changes run at once without view transitions or with reduced motion, and slide in the given direction otherwise', async () => {
  const c = client(undefined, noChatTimers);
  const root = c.context.document.documentElement;
  root.dataset = {};
  let ran = 0;
  assert.equal(c.navTransition('push', () => ran++), null);
  assert.equal(ran, 1, 'no support: the update runs immediately');
  const seen = [];
  let finish;
  c.context.document.startViewTransition = (update) => {
    seen.push(root.dataset.nav);
    Promise.resolve().then(update);
    return { finished: new Promise((resolve) => { finish = resolve; }), updateCallbackDone: Promise.resolve() };
  };
  assert.ok(c.navTransition('pop', () => ran++));
  assert.deepEqual(seen, ['pop']); assert.equal(root.dataset.nav, 'pop');
  await settle();
  assert.equal(ran, 2);
  finish(); await settle();
  assert.equal(root.dataset.nav, undefined, 'the direction is cleared when the slide ends');
  c.context.window.matchMedia = (query) => ({ matches: query === '(prefers-reduced-motion: reduce)' });
  c.navTransition('push', () => ran++);
  assert.equal(ran, 3); assert.deepEqual(seen, ['pop'], 'reduced motion never starts a transition');
  c.context.window.matchMedia = () => ({ matches: false });
  c.context.document.startViewTransition = () => { throw new Error('busy'); };
  c.navTransition('push', () => ran++);
  assert.equal(ran, 4); assert.equal(root.dataset.nav, undefined, 'a refused transition still updates');
});

test('Android back keeps one history step per phone screen while list and conversation slide', async () => {
  const c = client(undefined, noChatTimers);
  c.context.navigator = { onLine: false };
  c.windowEvents.DOMContentLoaded();
  const { s } = selectChat(c);
  const root = c.context.document.documentElement;
  root.dataset = {};
  const body = classes();
  c.context.document.body.classList = body;
  c.context.window.matchMedia = (query) => ({ matches: query === '(max-width: 700px)' });
  const slides = [];
  c.context.document.startViewTransition = (update) => { slides.push(root.dataset.nav); update(); return { finished: Promise.resolve() }; };
  c.select(s.key);
  assert.equal(c.context.window.history.state.view, 'detail');
  assert.deepEqual(slides, ['push']); assert.equal(body.contains('show-detail'), true); assert.equal(c.chatVisible(), true);
  c.backToList();
  assert.equal(c.context.window.history.backCount, 1, 'back uses the browser history');
  c.context.window.history.state = { sessionholic: true, view: 'list' };
  c.windowEvents.popstate({ state: { sessionholic: true, view: 'list' } });
  assert.deepEqual(slides, ['push', 'pop']);
  assert.equal(body.contains('show-detail'), false); assert.equal(c.chatVisible(), false);
  c.windowEvents.popstate({ state: { sessionholic: true, view: 'detail' }, hasUAVisualTransition: true });
  assert.deepEqual(slides, ['push', 'pop'], 'a browser-animated swipe gets no second slide');
  assert.equal(body.contains('show-detail'), true);
  await settle();
});

test('bottom sheets follow the finger, dismiss on a long or fast pull, step between heights and spring back when dismissal is refused', () => {
  const c = client(undefined, noChatTimers);
  const release = (fields) => c.sheetRelease({ height: 600, detent: 'medium', ...fields });
  assert.equal(release({ offset: 30, velocity: 0.1 }), 'medium');
  assert.equal(release({ offset: 200, velocity: 0.1 }), 'close');
  assert.equal(release({ offset: 60, velocity: 0.9 }), 'close');
  assert.equal(release({ offset: 260, velocity: 0.2, height: 800, detent: 'large' }), 'medium');
  assert.equal(release({ offset: 500, velocity: 0.2, height: 800, detent: 'large' }), 'close');
  assert.equal(release({ offset: -60, velocity: -0.1 }), 'large');
  assert.equal(release({ offset: -20, velocity: -0.1 }), 'medium');
  assert.equal(release({ offset: -60, detents: ['medium'] }), 'medium');
  let now = 1000;
  c.context.Date = { now: () => now };
  c.context.window.matchMedia = (query) => ({ matches: query === '(max-width: 700px)' });
  const dialog = node();
  dialog.open = false;
  dialog.classList = classes();
  dialog.style = { props: {}, setProperty(name, value) { this.props[name] = value; } };
  dialog.getBoundingClientRect = () => ({ top: 300, bottom: 900, left: 0, right: 412, height: 600 });
  const handle = node();
  handle.parentElement = dialog;
  handle.closest = (selector) => (selector.includes('button') ? null : handle);
  let dismissed = 0, allow = true;
  const sheet = c.BottomSheet.attach(dialog, { onDismiss: () => { dismissed++; if (allow) dialog.close(); } });
  assert.equal(c.BottomSheet.attach(dialog), sheet, 'one controller per dialog');
  sheet.open();
  assert.equal(dialog.open, true); assert.equal(dialog.dataset.detent, 'medium');
  const pointer = (type, y, extra = {}) => dialog.events[type]({ pointerId: 1, clientY: y, clientX: 200, button: 0, target: handle,
    cancelable: true, preventDefault() {}, ...extra });
  pointer('pointerdown', 320); now = 1100; pointer('pointermove', 360);
  assert.equal(dialog.style.props['--sheet-drag'], '40px'); assert.equal(dialog.classList.contains('is-dragging'), true);
  now = 1300; pointer('pointerup', 360);
  assert.equal(dismissed, 0, 'a short slow pull stays'); assert.equal(dialog.style.props['--sheet-drag'], '0px');
  assert.equal(dialog.classList.contains('is-dragging'), false);
  pointer('pointerdown', 320); now = 1400; pointer('pointermove', 600); now = 1700; pointer('pointerup', 600);
  assert.equal(dismissed, 1); assert.equal(dialog.open, false);
  sheet.open(); allow = false;
  pointer('pointerdown', 320); now = 1800; pointer('pointermove', 620); now = 2100; pointer('pointerup', 620);
  assert.equal(dismissed, 2); assert.equal(dialog.open, true, 'a refused dismissal keeps the sheet');
  assert.equal(dialog.style.props['--sheet-drag'], '0px', 'and springs back');
  pointer('pointerdown', 320); now = 2200; pointer('pointermove', 250);
  assert.equal(dialog.style.props['--sheet-drag'], '-33px', 'pulling up resists');
  now = 2400; pointer('pointerup', 250);
  assert.equal(dialog.dataset.detent, 'large');
  const button = node(); button.closest = () => button;
  pointer('pointerdown', 320, { target: button }); now = 2500; pointer('pointermove', 700);
  assert.equal(dialog.style.props['--sheet-drag'], '0px', 'header buttons keep their taps');
  c.context.window.matchMedia = () => ({ matches: false });
  pointer('pointerdown', 320); pointer('pointermove', 700);
  assert.equal(dialog.style.props['--sheet-drag'], '0px', 'wide screens keep the centered dialog');
  dialog.events.click({ target: dialog, clientX: 200, clientY: 100 });
  assert.equal(dismissed, 3, 'a tap outside dismisses');
  dialog.events.click({ target: dialog, clientX: 200, clientY: 500 });
  assert.equal(dismissed, 3, 'a tap inside does not');
});

test('typing on a phone folds the conversation chrome, sending keeps the keyboard, and blur restores the layout', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  c.drawDetail(s);
  const pane = c.get('detail');
  pane.classList = classes();
  const list = c.get('messages');
  Object.assign(list, { scrollHeight: 2000, clientHeight: 300, scrollTop: 1700 });
  const input = c.get('chat-input');
  c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' || query === '(max-width: 700px)' });
  input.events.focus();
  assert.equal(pane.classList.contains('is-typing'), true);
  assert.equal(list.scrollTop, 2000, 'the newest message stays in view');
  input.events.blur();
  assert.equal(pane.classList.contains('is-typing'), false);
  c.context.window.matchMedia = () => ({ matches: false });
  input.events.focus();
  assert.equal(pane.classList.contains('is-typing'), false, 'a mouse and keyboard keep the full layout');
  const send = c.get('chat-send');
  let prevented = 0;
  c.context.document.activeElement = input;
  send.events.pointerdown({ preventDefault() { prevented++; } });
  c.context.document.activeElement = null;
  send.events.pointerdown({ preventDefault() { prevented++; } });
  assert.equal(prevented, 1, 'focus is kept only while typing');
  const css = styleSheet();
  for (const part of ['.conversation-bar', '.continue-disclosure', '.chat-toolbar', '.detail-meta'])
    assert.match(css, new RegExp(`\\.detail-pane\\.is-typing ${part.replace('.', '\\.')}`));
});

test('terminal text size pinches within the A−/A+ range, stores once on release, and keys are taller', () => {
  const c = client(undefined, noChatTimers);
  assert.equal(c.pinchFontSize(14, 100, 200), 24);
  assert.equal(c.pinchFontSize(14, 100, 50), 10);
  assert.equal(c.pinchFontSize(14, 100, 120), 17);
  assert.equal(c.pinchFontSize(14, 0, 100), 14);
  const writes = [];
  c.context.navigator = { onLine: false };
  c.context.localStorage = { getItem: () => null, removeItem() {}, setItem: (key, value) => writes.push([key, value]) };
  c.windowEvents.DOMContentLoaded();
  const term = { options: { fontSize: 14 } };
  c.state.term = term; c.state.fontSize = 14;
  c.state.connected = true; c.state.terminal = { id: 'term-1' };
  const viewport = c.get('terminal-viewport');
  const fingers = (distance) => [{ clientX: 0, clientY: 100 }, { clientX: distance, clientY: 100 }];
  viewport.events.touchstart({ touches: fingers(100) });
  viewport.events.touchmove({ touches: fingers(130), cancelable: true, preventDefault() {} });
  assert.equal(c.state.fontSize, 18); assert.equal(term.options.fontSize, 18);
  viewport.events.touchmove({ touches: fingers(400), cancelable: true, preventDefault() {} });
  assert.equal(c.state.fontSize, 24);
  assert.deepEqual(writes, [], 'nothing is stored mid-gesture');
  viewport.events.touchend({ touches: [{ clientX: 0, clientY: 100 }] });
  assert.deepEqual(writes, [['sessionholic:ui:fontSize', '24']]);
  assert.match(c.get('toast').textContent, /글자 크기 24/);
  viewport.events.touchmove({ touches: [{ clientX: 0, clientY: 300 }], cancelable: true, preventDefault() {} });
  assert.equal(c.state.terminalScrollPending, 0, 'the finger left behind does not scroll');
  c.get('terminal-font-up').events.click();
  assert.equal(c.state.fontSize, 24);
  c.get('terminal-font-down').events.click();
  assert.equal(c.state.fontSize, 23); assert.equal(term.options.fontSize, 23);
  const css = styleSheet();
  assert.match(css, /\.terminal-keys \{[^}]*grid-template-rows: repeat\(2, 42px\)/);
  assert.match(css, /\.terminal-screen:not\(\[hidden\]\) ~ \.toast \{\s*bottom: calc\(var\(--terminal-tools-h/);
  assert.match(source, /D2Coding/); assert.match(source, /"Noto Sans Mono CJK KR"/);
});

test('the terminal library loads once on first open with versioned URLs, and a failed load is reported and can be retried', async () => {
  const html = indexPage();
  assert.doesNotMatch(html, /<(script|link)\b[^>]*(xterm|addon-fit)/, 'no terminal files on the board page');
  assert.match(html, /<meta name="sessionholic-assets" content="\{\}"/);
  const c = client(undefined, noChatTimers);
  const appended = [];
  c.context.document.head = { appendChild(item) { appended.push(item); } };
  c.context.document.querySelector = (selector) => (selector === 'meta[name="sessionholic-assets"]'
    ? { getAttribute: () => JSON.stringify({ '/vendor/xterm.js': 'abcdef12', '/vendor/addon-fit.js': '1234abcd', '/vendor/xterm.css': 'bad' }) } : null);
  const first = c.loadXterm();
  assert.equal(c.loadXterm(), first, 'one promise while loading');
  assert.deepEqual(appended.map((item) => item.href || item.src), ['/vendor/xterm.css', '/vendor/xterm.js?v=abcdef12', '/vendor/addon-fit.js?v=1234abcd']);
  assert.equal(appended[1].async, false); assert.equal(appended[2].async, false);
  c.context.window.Terminal = function Terminal() {};
  c.context.window.FitAddon = { FitAddon: function FitAddon() {} };
  for (const item of appended) item.onload();
  await first;
  await c.loadXterm();
  assert.equal(appended.length, 3, 'loaded once');
  const d = client(undefined, noChatTimers);
  const added = [];
  d.context.document.head = { appendChild(item) { added.push(item); } };
  const failed = d.loadXterm();
  added[1].onerror();
  await assert.rejects(failed);
  d.loadXterm();
  assert.equal(added.length, 6, 'opening again retries');
  const e = client(undefined, noChatTimers);
  e.context.document.head = { appendChild(item) { queueMicrotask(() => item.onerror()); } };
  const viewport = e.get('terminal-viewport');
  const loading = [];
  viewport.classList = { add: (name) => loading.push(`+${name}`), remove: (name) => loading.push(`-${name}`) };
  await e.openTerminal({ id: 'term-1', mode: 'attach' });
  assert.deepEqual(loading, ['+is-loading', '-is-loading']);
  assert.match(e.get('terminal-status').textContent, /불러오지 못했습니다/);
  assert.match(e.get('terminal-error').textContent, /다시 열어/);
});

test('service worker serves versioned assets cache-first by full URL, drops older versions, keeps the page network-first and never caches APIs', async () => {
  const listeners = {};
  const store = new Map();
  const cache = {
    match: async (request) => store.get(typeof request === 'string' ? request : request.url),
    put: async (request, response) => { store.set(typeof request === 'string' ? request : request.url, response); },
    keys: async () => [...store.keys()].map((url) => ({ url })),
    delete: async (request) => store.delete(request.url),
  };
  const fetched = [];
  let online = true;
  // Like the server: immutable only when the requested version is the current file.
  const current = { '/app.js': 'aaaaaaaa' };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8'), {
    self: { location: { origin: 'https://app.example' }, addEventListener: (type, handler) => { listeners[type] = handler; } },
    URL, Set, Response,
    fetch: async (request) => {
      const url = typeof request === 'string' ? request : request.url;
      fetched.push(url);
      if (!online) throw new Error('offline');
      const parsed = new URL(url, 'https://app.example');
      const fresh = parsed.searchParams.get('v') === current[parsed.pathname];
      return new Response(`body ${url}`, { headers: { 'Cache-Control': fresh ? 'public, max-age=31536000, immutable' : 'no-cache' } });
    },
    caches: { open: async () => cache, keys: async () => [] },
  });
  const run = async (route) => {
    let response;
    const waits = [];
    listeners.fetch({ request: { method: 'GET', url: `https://app.example${route}` }, respondWith: (promise) => { response = promise; },
      waitUntil: (promise) => waits.push(promise) });
    if (!response) return null;
    const result = await response;
    await Promise.all(waits);
    return result;
  };
  assert.match(await (await run('/app.js?v=aaaaaaaa')).text(), /aaaaaaaa/);
  await run('/app.js?v=aaaaaaaa');
  assert.equal(fetched.length, 1, 'a cached version is served without the network');
  current['/app.js'] = 'bbbbbbbb';
  await run('/app.js?v=bbbbbbbb');
  assert.equal(fetched.length, 2);
  assert.deepEqual([...store.keys()], ['https://app.example/app.js?v=bbbbbbbb'], 'older versions of the path are dropped');
  online = false;
  assert.match(await (await run('/app.js?v=bbbbbbbb')).text(), /bbbbbbbb/, 'versioned assets work offline');
  online = true;
  for (const route of ['/api/snapshot', '/api/events?host=a', '/api/chat/state', '/app.js?v=private', '/app.js?v=aaaaaaaa&x=1', '/?token=x', '/login'])
    assert.equal(await run(route), null, route);
  await run('/');
  assert.ok(store.has('https://app.example/'));
  online = false;
  assert.match(await (await run('/')).text(), /body https:\/\/app\.example\//, 'the page falls back to its cached copy offline');
  assert.equal((await run('/style.css')).status, 503, 'an uncached unversioned asset reports offline');
  assert.ok(![...store.keys()].some((url) => url.includes('/api/')));
});

function serviceWorkerHarness(page = '') {
  const listeners = {};
  const store = new Map();
  const key = (request) => new URL(typeof request === 'string' ? request : request.url, 'https://app.example').href;
  // A Cache without add(): storing goes through fetch, a header check, then put.
  const cache = {
    match: async (request) => store.get(key(request)),
    put: async (request, response) => { store.set(key(request), response); },
    keys: async () => [...store.keys()].map((url) => ({ url })),
    delete: async (request) => store.delete(key(request)),
  };
  const fetched = [];
  const current = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8'), {
    self: { location: { origin: 'https://app.example' }, addEventListener: (type, handler) => { listeners[type] = handler; },
      skipWaiting() {} },
    URL, Set, Response,
    fetch: async (request) => {
      const url = typeof request === 'string' ? request : request.url;
      fetched.push(url);
      const parsed = new URL(url, 'https://app.example');
      if (parsed.pathname === '/') return new Response(page, { headers: { 'Cache-Control': 'no-cache' } });
      const version = parsed.searchParams.get('v');
      const fresh = version === current[parsed.pathname];
      return new Response(`${parsed.pathname}@${current[parsed.pathname]}`,
        { headers: { 'Cache-Control': fresh ? 'public, max-age=31536000, immutable' : 'no-cache' } });
    },
    caches: { open: async () => cache, keys: async () => [] },
  });
  const run = async (route) => {
    let response;
    const waits = [];
    listeners.fetch({ request: { method: 'GET', url: `https://app.example${route}` }, respondWith: (promise) => { response = promise; },
      waitUntil: (promise) => waits.push(promise) });
    if (!response) return null;
    const result = await response;
    await Promise.all(waits);
    return result;
  };
  return { listeners, store, fetched, current, run };
}

test('service worker keeps only responses the server marked immutable, so a mismatched version is never pinned to that URL', async () => {
  const sw = serviceWorkerHarness();
  sw.current['/app.js'] = 'aaaaaaaa';
  await sw.run('/app.js?v=aaaaaaaa');
  assert.deepEqual([...sw.store.keys()], ['https://app.example/app.js?v=aaaaaaaa']);
  // A page that asks for v=bbbbbbbb before the server has it (or after a
  // rollback) gets the current file with no-cache: shown, but not stored.
  assert.equal(await (await sw.run('/app.js?v=bbbbbbbb')).text(), '/app.js@aaaaaaaa');
  assert.deepEqual([...sw.store.keys()], ['https://app.example/app.js?v=aaaaaaaa'], 'not kept, and the valid version stays');
  const before = sw.fetched.length;
  await sw.run('/app.js?v=bbbbbbbb');
  assert.equal(sw.fetched.length, before + 1, 'asked again rather than served from the cache');
  // Once the server has that version it is immutable and cached for good.
  sw.current['/app.js'] = 'bbbbbbbb';
  assert.equal(await (await sw.run('/app.js?v=bbbbbbbb')).text(), '/app.js@bbbbbbbb');
  assert.deepEqual([...sw.store.keys()], ['https://app.example/app.js?v=bbbbbbbb']);
  const cached = sw.fetched.length;
  assert.equal(await (await sw.run('/app.js?v=bbbbbbbb')).text(), '/app.js@bbbbbbbb');
  assert.equal(sw.fetched.length, cached, 'served from the cache');
  assert.match(fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8'), /const CACHE = "sessionholic-shell-v14";/,
    'a new cache name drops entries stored by the older rule');
});

test('service worker precache stores only immutable versioned assets and skips the terminal library', async () => {
  const versions = { '/app.js': 'aaaaaaaa', '/style.css': 'cccccccc', '/theme-init.js': 'eeeeeeee', '/vendor/xterm.js': 'dddddddd',
    '/session_labels.js': 'abababab', '/api/snapshot': 'ffffffff' };
  const page = `<meta name="sessionholic-assets" content="${JSON.stringify(versions).replace(/"/g, '&quot;')}">`;
  const sw = serviceWorkerHarness(page);
  // The server already serves a different app.js than the page names.
  Object.assign(sw.current, { '/app.js': '99999999', '/style.css': 'cccccccc', '/theme-init.js': 'eeeeeeee', '/vendor/xterm.js': 'dddddddd', '/session_labels.js': 'abababab' });
  let installed;
  sw.listeners.install({ waitUntil: (promise) => { installed = promise; } });
  await installed;
  assert.deepEqual([...sw.store.keys()].sort(), ['https://app.example/', 'https://app.example/session_labels.js?v=abababab', 'https://app.example/style.css?v=cccccccc',
    'https://app.example/theme-init.js?v=eeeeeeee']);
  assert.ok(sw.fetched.includes('/app.js?v=aaaaaaaa'), 'fetched, then refused by its header');
  assert.ok(!sw.fetched.some((url) => url.includes('xterm') || url.includes('/api/')));
});

test('a list still being collected says how old it is, outside the cards, and the note clears when collection ends', () => {
  const c = client(undefined, noChatTimers);
  const now = 1800000000;
  c.state.snapshot = { serverTime: now, hosts: [{ name: 'a', refreshing: true, fetchedAt: now - 300 },
    { name: 'b', refreshing: false, fetchedAt: now - 3000 }] };
  c.renderFreshness(now);
  assert.equal(c.get('list-freshness').textContent, '5분 전 기준 · 갱신 중');
  assert.equal(c.get('list-freshness').hidden, false);
  c.state.snapshot.hosts[0].fetchedAt = now - 20;
  c.renderFreshness(now);
  assert.equal(c.get('list-freshness').hidden, true, 'a fresh list needs no note');
  c.state.snapshot.hosts[0] = { name: 'a', refreshing: false, fetchedAt: now - 300 };
  c.renderFreshness(now);
  assert.equal(c.get('list-freshness').hidden, true); assert.equal(c.get('list-freshness').textContent, '');
  assert.match(indexPage(), /id="list-freshness"[\s\S]*id="groups"/);
});

function fakeEventSource() {
  const sources = [];
  class FakeEventSource {
    constructor(url) { this.url = url; this.readyState = 0; this.listeners = {}; this.closed = false; sources.push(this); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    emit(type, data) { this.listeners[type]?.({ data: data === undefined ? undefined : JSON.stringify(data) }); }
    close() { this.closed = true; this.readyState = 2; }
  }
  return { FakeEventSource, sources };
}

test('live updates follow the open conversation, reread only on newer events, slow polling to a safety net and fall back after errors', async () => {
  const requests = [];
  const timers = [];
  let c;
  c = client(async (url, options) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot') ? { hosts: c.state.snapshot.hosts, version: 7 }
      : { route: JSON.parse(options.body).source, capability: { supported: true }, phase: 'working', messages: [] }) };
  }, { setTimeout: (callback, ms) => {
    // Announced snapshots are read after a 300ms batch; run that one at once.
    if (ms === 300) { setImmediate(callback); return 'batch'; }
    timers.push(ms); return timers.length;
  }, clearTimeout() {} });
  const { FakeEventSource, sources } = fakeEventSource();
  c.context.EventSource = FakeEventSource;
  const { s } = selectChat(c);
  c.state.detailShown = true;
  c.state.capabilities = {};
  c.state.capabilitiesRevision = c.capabilitiesRevision(c.state.snapshot);
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  c.state.detailFor = s.key; c.state.detailUpdatedAt = s.updatedAt;
  c.state.detailRevision = vm.runInContext('detailRevision', c.context)(c.allSessions().find((item) => item.key === s.key));
  c.syncEvents();
  assert.equal(sources.length, 1);
  assert.equal(sources[0].url, '/api/events?host=workstation&agent=codex&home=.codex-example&id=native-session');
  c.syncEvents();
  assert.equal(sources.length, 1, 'the same conversation keeps its stream');
  assert.equal(c.chatPollDelay(0), 3000);
  c.state.snapshot.hosts[0].refreshing = true;
  c.state.refreshDeadline = Date.now() + 60000;
  sources[0].emit('hello', { snapshotVersion: 3 });
  await settle();
  assert.equal(c.eventsHealthy(), true);
  assert.equal(requests.filter((url) => url === '/api/snapshot').length, 1, 'the first announced version is read once');
  assert.equal(c.state.snapshotVersion, 7);
  assert.equal(c.chatPollDelay(0), 30000, 'conversation polling becomes a 30s safety net');
  timers.length = 0; c.schedule();
  assert.deepEqual(timers, [60000], 'snapshot polling becomes a 60s safety net even while collecting');
  requests.length = 0;
  sources[0].emit('snapshot', { version: 7 });
  await settle();
  assert.deepEqual(requests, [], 'a version already shown reads nothing');
  sources[0].emit('snapshot', { version: 8 });
  await settle();
  assert.deepEqual(requests, ['/api/snapshot']);
  requests.length = 0;
  sources[0].emit('chat', { digest: 'd1' });
  await settle();
  assert.deepEqual(requests, ['/api/chat/state']);
  sources[0].emit('chat', { digest: 'd1' });
  await settle();
  assert.deepEqual(requests, ['/api/chat/state'], 'an unchanged digest reads nothing');
  c.state.detailShown = false;
  c.context.window.matchMedia = (query) => ({ matches: query === '(max-width: 700px)' });
  c.syncEvents();
  assert.equal(sources[0].closed, true);
  assert.equal(sources[1].url, '/api/events', 'the list alone subscribes to snapshots only');
  c.context.window.matchMedia = () => ({ matches: false });
  c.context.document.hidden = true; c.syncEvents();
  assert.equal(sources[1].closed, true); assert.equal(c.state.events, null, 'a hidden page closes the stream');
  c.context.document.hidden = false; c.syncEvents();
  assert.equal(sources.length, 3);
  sources[2].emit('open');
  assert.equal(c.eventsHealthy(), true);
  for (let index = 0; index < 3; index++) sources[2].emit('error');
  assert.equal(sources[2].closed, true, 'three errors within 30 seconds fall back');
  assert.equal(c.eventsHealthy(), false);
  assert.equal(c.chatPollDelay(0), 3000, 'polling returns to its normal backoff');
  timers.length = 0; c.schedule();
  assert.deepEqual(timers, [2000], 'and to the 2s collection loop');
  c.syncEvents();
  assert.equal(sources.length, 3, 'no reconnect right after a fallback');
  c.state.eventsRetryAt = 0;
  c.syncEvents();
  sources[3].emit('open');
  timers.length = 0; c.schedule();
  assert.deepEqual(timers, [60000]);
  timers.length = 0;
  sources[3].readyState = 2;
  sources[3].emit('error');
  assert.equal(c.state.events, null, 'a healthy stream closed for good falls back');
  assert.deepEqual(timers, [2000, 3000], 'and both safety timers (60s list, 30s conversation) return to the normal pace at once');
  const d = client(undefined, noChatTimers);
  const second = fakeEventSource();
  d.context.EventSource = second.FakeEventSource;
  selectChat(d);
  d.syncEvents();
  second.sources[0].readyState = 2;
  second.sources[0].emit('error');
  assert.equal(d.state.events, null, 'a server without /api/events (closed before opening) falls back at once');
  const e = client(undefined, noChatTimers);
  selectChat(e);
  e.syncEvents();
  assert.equal(e.state.events, null, 'no EventSource: the existing polling stays');
});

// --- UI round 2: list header, sending, gestures, attachments, states, text size ---

test('status chips filter the list to answers or running work, keep both counts, and clear on a second tap', () => {
  const c = client(undefined, noChatTimers);
  const rows = [recentSession('wait-a', { phase: 'needs_input', updatedAt: 1090 }), recentSession('wait-b', { phase: 'needs_input', updatedAt: 1080 }),
    recentSession('run', { phase: 'working', updatedAt: 1070 }), recentSession('idle', { updatedAt: 1060 })];
  setRecentSnapshot(c, rows);
  vm.runInContext('render()', c.context);
  const chips = c.get('counts').children;
  assert.deepEqual(chips.map((chip) => chip.tagName), ['BUTTON', 'BUTTON']);
  assert.deepEqual(chips.map((chip) => chip.chipLabel.textContent), ['답 필요 2', '작업 중 1']);
  assert.deepEqual(chips.map((chip) => chip.attributes['aria-pressed']), ['false', 'false']);
  assert.equal(byClass(c.get('groups'), 'card').length, 4);
  chips[0].events.click();
  assert.equal(c.state.phaseFilter, 'needs_input');
  assert.deepEqual(byClass(c.get('groups'), 'card').map((card) => card.dataset.sessionKey), [rows[0].key, rows[1].key]);
  const after = c.get('counts').children;
  assert.equal(after[0], chips[0], 'the chip keeps its element (and keyboard focus)');
  assert.equal(after[0].attributes['aria-pressed'], 'true');
  assert.match(after[0].className, /is-active/);
  assert.equal(after[1].chipLabel.textContent, '작업 중 1', 'the other chip keeps its count');
  assert.equal(c.get('session-count').textContent, '2개');
  assert.equal(c.context.document.title, '(2) 답 필요 · 세션홀릭', 'the tab title still counts every waiting session');
  after[1].events.click();
  assert.equal(c.state.phaseFilter, 'working');
  assert.deepEqual(byClass(c.get('groups'), 'card').map((card) => card.dataset.sessionKey), [rows[2].key]);
  after[1].events.click();
  assert.equal(c.state.phaseFilter, '');
  assert.equal(byClass(c.get('groups'), 'card').length, 4);
  assert.equal(c.matches(rows[3], 1100), true);
  // A filter that leaves nothing explains itself and clears with one tap.
  c.state.snapshot.hosts[0].data.codex = rows.filter((s) => s.phase !== 'working');
  after[1].events.click();
  assert.match(textOf(c.get('groups')), /작업 중인 세션이 없습니다/);
  byClass(c.get('groups'), 'secondary')[0].events.click();
  assert.equal(c.state.phaseFilter, '');
  assert.equal(byClass(c.get('groups'), 'card').length, 3);
});

test('the usage line is one chip that opens a sheet of per-account bars, and open terminals live in their own sheet', () => {
  const c = client(undefined, noChatTimers);
  c.context.navigator = { onLine: false };
  c.windowEvents.DOMContentLoaded();
  c.renderUsage({ available: true, providers: [{ short: 'CX', label: 'Codex', remaining: 45, status: 'ready', rows: [
    { name: '개인', windows: [{ label: '주간', remaining: 45 }] }] }] });
  assert.equal(c.get('usage').hidden, false);
  assert.match(c.get('usage').attributes['aria-label'], /남은 사용량 Codex 45%.*계정별로 보기/);
  c.get('usage').events.click();
  assert.equal(c.get('usage-dialog').open, true);
  assert.match(textOf(c.get('usage-body')), /개인.*주간.*45%/);
  c.get('usage-close').events.click();
  assert.equal(c.get('usage-dialog').open, false);
  assert.equal(c.get('usage').focusCount, 1, 'focus returns to the chip');
  c.state.terminals = [{ id: 'one', alive: true, title: '열어 둔 작업', host: 'workstation', agent: 'claude', mode: 'attach' }];
  c.renderTerminals();
  assert.equal(c.get('running-terminals').hidden, false);
  assert.equal(c.get('running-count').textContent, 1);
  c.get('running-terminals').events.click();
  assert.equal(c.get('terminals-dialog').open, true);
  // A connection to an existing session is folded under one row until asked for.
  assert.doesNotMatch(textOf(c.get('running-list')), /열어 둔 작업/);
  byClass(c.get('running-list'), 'running-connections')[0].events.click();
  assert.match(textOf(c.get('running-list')), /열어 둔 작업/);
  c.state.terminals = [];
  c.renderTerminals();
  assert.equal(c.get('running-terminals').hidden, true);
  assert.equal(c.get('terminals-dialog').open, false, 'an emptied list closes its sheet');
  // The filter sheet holds the selects and the old-conversations switch; its button counts what applies.
  c.state.filters.agent = 'codex'; c.state.showOld = true;
  c.renderFilters([]);
  assert.equal(c.get('filter-count').textContent, '2');
  assert.equal(c.get('filter-open').attributes['aria-label'], '필터 2개 적용됨');
  c.get('filter-reset').events.click();
  assert.equal(c.state.filters.agent, ''); assert.equal(c.state.showOld, false);
  assert.equal(c.get('filter-count').textContent, '');
  const html = indexPage();
  for (const id of ['usage-dialog', 'filter-dialog', 'terminals-dialog', 'action-sheet'])
    assert.match(html, new RegExp(`<dialog id="${id}" class="plan-dialog sheet`));
  assert.match(html, /id="terminal-return"[\s\S]*<\/aside>/, 'the terminal bar floats inside the list');
});

test('after an answer a still-working session shows no "작업 중" until a message is sent, which brings it back right after the bubble', async () => {
  let release; let stage = 'answered'; const sends = [];
  const now = Math.floor(Date.now() / 1000);
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body);
      return await new Promise((resolve) => { release = () => resolve({ ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) }); });
    }
    const messages = [{ role: 'user', text: '정리해 주세요', ts: now - 120, itemId: 'item-user-0', clientId: null },
      { role: 'assistant', text: '정리했습니다', ts: now - 60, itemId: 'item-answer-0', clientId: null }];
    if (stage !== 'answered') messages.push({ role: 'user', text: '이것도 해 주세요', ts: now, itemId: 'item-user-1', clientId: sends[0].requestId });
    if (stage === 'done') messages.push({ role: 'assistant', text: '그것도 했습니다', ts: now + 5, itemId: 'item-answer-1', clientId: null });
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      workingSince: now - 30, receipts: body.requestId ? [{ requestId: body.requestId, status: 'accepted' }] : [], messages }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  await c.loadChatState(s, true);
  const list = c.get('messages');
  const working = () => list.children.filter((row) => /msg-working/.test(row.className));
  assert.equal(entry.data.phase, 'working');
  assert.equal(working().length, 0, 'the answer is the newest row');
  c.get('chat-input').value = entry.text = '이것도 해 주세요';
  const pending = c.sendChat(entry);
  const bubble = list.children.find((row) => /is-outbox/.test(row.className));
  assert.match(bubble.className, /is-sending/);
  assert.equal(list.children.at(-2), bubble);
  assert.match(list.children.at(-1).className, /msg-working/, 'right after the bubble sent from here');
  assert.match(list.children.at(-1).workingTime.textContent, /^ · 3[0-2]초$/, 'Codex counts its exact turn start');
  await settle();
  stage = 'delivered';
  release(); await pending; await settle();
  assert.equal(list.children.some((row) => /is-outbox/.test(row.className)), false);
  assert.equal(list.children.at(-2).messageItem.message.text, '이것도 해 주세요');
  assert.match(list.children.at(-1).className, /msg-working/, 'the transcript\'s own user row still waits');
  stage = 'done';
  await c.loadChatState(s, true);
  assert.equal(working().length, 0);
});

test('sending shows the bubble at once, keeps but hides the draft, and gives way to the transcript row', async () => {
  let release; let delivered = false; const sends = [];
  const now = Math.floor(Date.now() / 1000);
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body);
      return await new Promise((resolve) => { release = () => resolve({ ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) }); });
    }
    // A Codex state read names each item and the request a board message came from.
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      receipts: body.requestId ? [{ requestId: body.requestId, status: 'accepted' }] : [],
      messages: [{ role: 'assistant', text: '이전 답변', ts: now - 60, itemId: 'item-answer-0', clientId: null },
        ...(delivered ? [{ role: 'user', text: '테스트를 다시 돌려 주세요', ts: now, itemId: 'item-user-1', clientId: sends[0].requestId }] : [])] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  await c.loadChatState(s, true);
  const list = c.get('messages');
  const input = c.get('chat-input'); input.value = entry.text = '테스트를 다시 돌려 주세요';
  const pending = c.sendChat(entry);
  // Before any answer: a translucent "전송 중" bubble after the transcript, before the working row.
  const outbox = () => list.children.filter((row) => /is-outbox/.test(row.className));
  assert.equal(outbox().length, 1);
  assert.match(outbox()[0].className, /is-outbox is-sending/);
  assert.match(textOf(outbox()[0]), /테스트를 다시 돌려 주세요.*전송 중/);
  assert.match(list.children.at(-1).className, /msg-working/);
  assert.match(c.get('chat-composer').className, /is-sent-draft/, 'the kept draft is drawn empty');
  assert.equal(entry.text, '테스트를 다시 돌려 주세요', 'and is not cleared before the server accepts it');
  await settle();
  assert.equal(sends.length, 1);
  delivered = true;
  release(); await pending; await settle();
  assert.equal(entry.text, ''); assert.equal(input.value, '');
  assert.equal(entry.outbox.length, 0, 'the transcript has the message now');
  assert.equal(outbox().length, 0);
  assert.equal(list.children.filter((row) => row.messageItem?.message.text === '테스트를 다시 돌려 주세요').length, 1);
  assert.doesNotMatch(c.get('chat-composer').className, /is-sent-draft/);
  // Accepted but not yet in the transcript (a queued turn): the bubble stays, labelled.
  const d = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') return { ok: true, status: 200, json: async () => ({ status: 'queued', receipt: {} }) };
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working', receipts: [], messages: [] }) };
  }, noChatTimers);
  const queued = selectChat(d);
  wireTree(d, d.buildChatComposer(queued.s)); await settle();
  d.renderMessages(queued.s, []);
  queued.entry.text = '끝나면 이어서 해 주세요';
  await d.sendChat(queued.entry); await settle();
  const bubble = d.get('messages').children.find((row) => /is-outbox/.test(row.className));
  assert.match(bubble.className, /is-sent/);
  assert.match(textOf(bubble), /대기 중/);
});

test('a refused send becomes "보내지 못함 · 다시 보내기", shows the kept draft again and resends only when asked', async () => {
  const sends = [];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body.requestId);
      if (sends.length === 1) return { ok: false, status: 409, json: async () => ({
        error: '현재 실행 중인 작업은 새 입력을 받을 수 없습니다.', dispatchState: 'not_started' }) };
      return { ok: true, status: 200, json: async () => ({ status: 'queued', receipt: {} }) };
    }
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      receipts: [], messages: [{ role: 'assistant', text: '진행 중' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  c.renderMessages(s, [{ role: 'assistant', text: '진행 중' }]);
  const input = c.get('chat-input'); input.value = entry.text = '끝나면 요약해 주세요';
  await c.sendChat(entry); await settle();
  const failed = c.get('messages').children.find((row) => /is-outbox/.test(row.className));
  assert.match(failed.className, /is-failed/);
  assert.match(textOf(failed), /끝나면 요약해 주세요.*보내지 못함/);
  assert.equal(failed.deliveryStatus.title, '현재 실행 중인 작업은 새 입력을 받을 수 없습니다.');
  const retry = byClass(failed, 'msg-retry')[0];
  assert.equal(retry.textContent, '다시 보내기');
  assert.equal(entry.text, '끝나면 요약해 주세요'); assert.equal(input.value, '끝나면 요약해 주세요');
  assert.doesNotMatch(c.get('chat-composer').className, /is-sent-draft/, 'the kept draft shows again');
  assert.match(c.get('chat-status').textContent, /새 입력을 받을 수 없습니다.*전송되지 않았습니다/);
  assert.equal(sends.length, 1, 'nothing is resent by itself');
  retry.events.click(); await settle();
  assert.equal(sends.length, 2);
  assert.notEqual(sends[1], sends[0], 'an explicit resend is a new request');
  assert.deepEqual(Array.from(entry.outbox, (item) => [item.state, item.status]), [['sent', 'queued']]);
  assert.equal(entry.text, '');
  // A runtime refusal reported in a 200 receipt (NOT_STEERABLE) is a failed bubble too.
  const d = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') return { ok: true, status: 200, json: async () => ({ status: 'failed',
      receipt: { confirmed: false, reason: '현재 실행 중인 작업(/review·/compact 등)은 새 입력을 받을 수 없습니다. 끝난 뒤 다시 보내 주세요.' } }) };
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working', receipts: [], messages: [] }) };
  }, noChatTimers);
  const second = selectChat(d);
  wireTree(d, d.buildChatComposer(second.s)); await settle();
  d.renderMessages(second.s, []);
  second.entry.text = '리뷰가 끝나면 알려 주세요';
  await d.sendChat(second.entry); await settle();
  assert.match(d.get('messages').children.find((row) => /is-outbox/.test(row.className)).className, /is-failed/);
  assert.equal(second.entry.text, '리뷰가 끝나면 알려 주세요');
  assert.equal(second.entry.pending, null);
});

test('resending a refused bubble sends that message and its files only, and leaves a different draft in the editor', async () => {
  const sends = [];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body);
      if (sends.length === 1) return { ok: false, status: 409, json: async () => ({
        error: '현재 실행 중인 작업은 새 입력을 받을 수 없습니다.', dispatchState: 'not_started' }) };
      return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) };
    }
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      receipts: [], messages: [{ role: 'assistant', text: '진행 중' }] }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  c.renderMessages(s, [{ role: 'assistant', text: '진행 중' }]);
  const input = c.get('chat-input');
  input.value = entry.text = '메시지 A';
  entry.attachments = [{ id: 'file-a', name: 'a.png', type: 'image/png', size: 10 }];
  await c.sendChat(entry); await settle();
  const failed = c.get('messages').children.find((row) => /is-outbox/.test(row.className));
  assert.match(failed.className, /is-failed/);
  // The user drops A's file, writes B, then taps A's "다시 보내기".
  entry.attachments = []; entry.revision++;
  input.value = '초안 B'; input.events.input();
  byClass(failed, 'msg-retry')[0].events.click(); await settle();
  assert.equal(sends.length, 2);
  assert.equal(sends[1].text, '메시지 A', 'the bubble\'s own text');
  assert.deepEqual(sends[1].attachments, ['file-a'], 'and its own files');
  assert.notEqual(sends[1].requestId, sends[0].requestId, 'a refused request gets a new id');
  assert.deepEqual(Array.from(entry.outbox, (item) => [item.text, item.state]), [['메시지 A', 'sent']]);
  assert.equal(entry.text, '초안 B', 'the other draft is kept'); assert.equal(input.value, '초안 B');
  assert.equal(entry.attachments.length, 0);
  assert.equal(entry.pending, null);
  assert.ok(!sends.some((body) => body.text === '초안 B'), 'and is never sent by itself');
  // With the editor emptied instead, the message comes back to it and leaves once accepted.
  const emptied = [];
  const d = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      emptied.push(body);
      if (emptied.length === 1) return { ok: false, status: 409, json: async () => ({ error: '거절', dispatchState: 'not_started' }) };
      return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) };
    }
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'idle', receipts: [], messages: [] }) };
  }, noChatTimers);
  const second = selectChat(d);
  wireTree(d, d.buildChatComposer(second.s)); await settle();
  d.renderMessages(second.s, []);
  const editor = d.get('chat-input');
  editor.value = second.entry.text = '메시지 C';
  await d.sendChat(second.entry); await settle();
  editor.value = ''; editor.events.input();
  byClass(d.get('messages').children.find((row) => /is-failed/.test(row.className)), 'msg-retry')[0].events.click();
  assert.equal(second.entry.text, '메시지 C', 'back in the editor while it is sent');
  await settle();
  assert.equal(emptied[1].text, '메시지 C');
  assert.equal(second.entry.text, ''); assert.equal(editor.value, '');
});

test('one transcript message settles one bubble, matched by its whole text and attachment count, never by an earlier message', () => {
  const c = client(undefined, noChatTimers);
  // Claude rows carry no identifiers, so a Claude bubble is matched by its text.
  const { s, entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  wireTree(c, c.buildChatComposer(s));
  const now = Date.now(); const ts = Math.floor(now / 1000);
  const bubble = (requestId, text, extra = {}) => ({ requestId, text, attachments: [], state: 'sent', status: 'accepted', reason: '',
    at: now, base: 0, ...extra });
  const pending = () => entry.outbox.map((item) => item.requestId);
  // The same text sent twice, both accepted; the transcript shows one so far.
  entry.outbox = [bubble('req-first-0001', '다시 확인해 주세요', { at: now - 2000 }),
    bubble('req-second-0002', '다시 확인해 주세요', { at: now - 1000 })];
  const earlier = { role: 'user', text: '다시 확인해 주세요', ts: ts - 600 };
  const first = { role: 'user', text: '다시 확인해 주세요\n', ts };
  c.renderMessages(s, [earlier, first]);
  assert.deepEqual(pending(), ['req-second-0002']);
  assert.equal(c.get('messages').children.filter((row) => /is-outbox/.test(row.className)).length, 1, 'one bubble left on screen');
  c.renderMessages(s, [earlier, first]);
  assert.deepEqual(pending(), ['req-second-0002'], 'a message that settled a bubble never settles another on a later render');
  c.renderMessages(s, [earlier, first, { role: 'user', text: '  다시   확인해 주세요 ', ts: ts + 1 }]);
  assert.deepEqual(pending(), []);
  // Neither a longer message containing the text, another attachment count,
  // nor the same message from before the send settles a bubble by its text.
  // (An unconfirmed bubble: a delivered Claude bubble would take the next new
  // message through the safety net, tested on its own below.)
  entry.outbox = [bubble('req-third-0003', '확인', { state: 'uncertain', status: 'unknown',
    attachments: [{ id: 'f', name: 'a.png', type: 'image/png', kind: 'image' }] })];
  const others = [{ role: 'user', text: '확인', ts: ts - 120, attachments: [{ kind: 'image' }] },
    { role: 'user', text: '다시 확인할까요', ts: ts + 2, attachments: [{ kind: 'image' }] },
    { role: 'user', text: '확인', ts: ts + 2 }];
  c.settleOutbox(entry, others);
  assert.deepEqual(pending(), ['req-third-0003']);
  // A Claude message carries its file as a "첨부 파일:" line the server appended.
  c.settleOutbox(entry, [...others, { role: 'user', text: '확인\n첨부 파일: /work/a.png', ts: ts + 3 }]);
  assert.deepEqual(pending(), []);
  // A Codex message with only an image reads "[이미지 첨부]".
  entry.outbox = [bubble('req-image-0004', '', { attachments: [{ id: 'g', name: 'b.png', type: 'image/png', kind: 'image' }] })];
  c.settleOutbox(entry, [{ role: 'user', text: '[이미지 첨부]', ts: ts + 4, attachments: [{ kind: 'image' }] }]);
  assert.deepEqual(pending(), []);
  // Without times, only messages after those on screen at the send count.
  entry.outbox = [bubble('req-untimed-0005', '계속', { base: 2 })];
  const untimed = [{ role: 'user', text: '계속' }, { role: 'assistant', text: '네' }];
  c.settleOutbox(entry, untimed);
  assert.deepEqual(pending(), ['req-untimed-0005']);
  c.settleOutbox(entry, [...untimed, { role: 'user', text: '계속' }]);
  assert.deepEqual(pending(), []);
  // A Codex row names its request: another request's row never settles it, its
  // own does even when it joined a turn that started before the send.
  const d = client(undefined, noChatTimers);
  const codex = selectChat(d).entry;
  codex.outbox = [bubble('req-codex-0006', '계속 진행')];
  d.settleOutbox(codex, [{ role: 'user', text: '계속 진행', ts: ts + 5, itemId: 'item-9', clientId: 'req-other-0099' }]);
  assert.deepEqual(codex.outbox.map((item) => item.requestId), ['req-codex-0006']);
  d.settleOutbox(codex, [{ role: 'user', text: '계속 진행', ts: ts - 300, itemId: 'item-6', clientId: 'req-codex-0006' }]);
  assert.deepEqual(codex.outbox.map((item) => item.requestId), []);
  // A cut long message is compared by its beginning.
  const long = '가'.repeat(5000);
  entry.outbox = [bubble('req-long-0007', long)];
  c.settleOutbox(entry, [{ role: 'user', text: long.slice(0, 4000) + ' …', truncated: true, fullLength: 5000, ts: ts + 6 }]);
  assert.deepEqual(pending(), []);
});

test('the same text sent 10 seconds earlier never settles the new bubble, also after switching away and back', async () => {
  const now = Math.floor(Date.now() / 1000);
  const sends = []; let delivered = false;
  const earlier = [{ role: 'user', text: '계속 진행해 주세요', ts: now - 10 }, { role: 'assistant', text: '진행했습니다', ts: now - 5 }];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') { sends.push(body); return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) }; }
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working', receipts: [],
      messages: [...earlier, ...(delivered ? [{ role: 'user', text: '계속 진행해 주세요', ts: now }] : [])] }) };
  }, noChatTimers);
  const claude = { agent: 'claude', home: '.claude', id: 'claude-session' };
  const { s, entry } = selectChat(c, claude);
  wireTree(c, c.buildChatComposer(s)); await settle();
  await c.loadChatState(s, true);
  entry.text = '계속 진행해 주세요';
  await c.sendChat(entry); await settle();
  assert.equal(sends.length, 1);
  const bubbles = () => c.get('messages').children.filter((row) => /is-outbox/.test(row.className)).length;
  assert.equal(entry.outbox.length, 1, 'the message already there is not the one just sent');
  assert.equal(bubbles(), 1);
  await c.loadChatState(s, true); await settle();
  assert.equal(entry.outbox.length, 1, 'nor on the next read');
  // Another conversation and back: the bubble still knows what it was sent after.
  const other = selectChat(c, { ...claude, id: 'claude-other' });
  c.renderMessages(other.s, []);
  assert.equal(bubbles(), 0);
  assert.equal(selectChat(c, claude).entry, entry);
  await c.loadChatState(s, true); await settle();
  assert.equal(entry.outbox.length, 1);
  assert.equal(bubbles(), 1);
  delivered = true;
  await c.loadChatState(s, true); await settle();
  assert.equal(entry.outbox.length, 0, 'its own message settles it');
  assert.equal(bubbles(), 0);
  assert.equal(c.get('messages').children.filter((row) => row.messageItem?.message.text === '계속 진행해 주세요').length, 2);
});

test('a Codex bubble settles only by its request id, and /api/read rows without ids never settle a second bubble of the same text', async () => {
  const now = Math.floor(Date.now() / 1000);
  const sends = []; const reads = [];
  let stateFails = false;
  const answer = { role: 'assistant', text: '확인했습니다', ts: now - 30 };
  let stateRows = [{ ...answer, itemId: 'item-answer', clientId: null }];
  let readRows = [answer];
  const c = client(async (url, options) => {
    if (url.startsWith('/api/read')) { reads.push(url); return { ok: true, status: 200, json: async () => ({ messages: readRows }) }; }
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') { sends.push(body); return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: {} }) }; }
    if (stateFails) return { ok: false, status: 502, json: async () => ({ error: '상태를 확인하지 못했습니다.' }) };
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      receipts: [], messages: stateRows }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.state.detailShown = true;
  wireTree(c, c.buildChatComposer(s)); await settle();
  await c.loadChatState(s, true);
  for (let i = 0; i < 2; i++) {
    entry.text = '다시 확인해 주세요';
    await c.sendChat(entry); await settle();
  }
  const [first, second] = sends.map((body) => body.requestId);
  assert.equal(sends.length, 2); assert.notEqual(first, second);
  const pending = () => Array.from(entry.outbox, (item) => item.requestId);
  const bubbles = () => c.get('messages').children.filter((row) => /is-outbox/.test(row.className)).length;
  assert.deepEqual(pending(), [first, second]);
  // The state read shows the first send, named by its request id.
  const own = (requestId, itemId) => ({ role: 'user', text: '다시 확인해 주세요', ts: now, itemId, clientId: requestId });
  stateRows = [...stateRows, own(first, 'item-1')];
  await c.loadChatState(s, true); await settle();
  assert.deepEqual(pending(), [second]);
  // The state read fails; /api/read shows the same message without any id.
  stateFails = true;
  readRows = [answer, { role: 'user', text: '다시 확인해 주세요', ts: now }];
  await c.loadDetail(s); await settle();
  assert.equal(reads.length, 1, 'the conversation came from /api/read');
  assert.deepEqual(pending(), [second], 'a message settled by its id is not taken again by its text');
  assert.equal(bubbles(), 1);
  // Even the second message, as /api/read shows it, cannot say which request it was.
  readRows = [...readRows, { role: 'user', text: '다시 확인해 주세요', ts: now }];
  await c.loadDetail(s); await settle();
  assert.equal(reads.length, 2);
  assert.deepEqual(pending(), [second]);
  // Back on the state read, its own row settles it.
  stateFails = false;
  stateRows = [...stateRows, own(second, 'item-2')];
  await c.loadChatState(s, true); await settle();
  assert.deepEqual(pending(), []);
  assert.equal(bubbles(), 0);
});

test('one new message settles one bubble with or without row ids, after the last message the bubble saw', () => {
  const now = Date.now(); const ts = Math.floor(now / 1000);
  const c = client(undefined, noChatTimers);
  const bubble = (requestId, text, seen, extra = {}) => ({ requestId, text, attachments: [], state: 'sent', status: 'accepted', reason: '',
    at: now, ...c.outboxOrigin(seen), ...extra });
  // Rows named by item id (a Codex state read).
  const codex = selectChat(c).entry;
  const named = [{ role: 'user', text: '계속', ts: ts - 10, itemId: 'item-old', clientId: null },
    { role: 'assistant', text: '네', ts: ts - 5, itemId: 'item-answer', clientId: null }];
  codex.outbox = [bubble('req-codex-a001', '계속', named), bubble('req-codex-b002', '계속', named)];
  assert.deepEqual(codex.outbox[0].known, ['item-old']);
  const codexPending = () => codex.outbox.map((item) => item.requestId);
  // Neither the message already there nor one typed in the terminal (no request id) settles a Codex bubble.
  const typed = { role: 'user', text: '계속', ts, itemId: 'item-typed', clientId: null };
  c.settleOutbox(codex, [...named, typed]);
  assert.deepEqual(codexPending(), ['req-codex-a001', 'req-codex-b002']);
  const a = { role: 'user', text: '계속', ts, itemId: 'item-a', clientId: 'req-codex-a001' };
  c.settleOutbox(codex, [...named, typed, a]);
  assert.deepEqual(codexPending(), ['req-codex-b002'], 'one new row, one bubble');
  c.settleOutbox(codex, [...named, typed, a]);
  assert.deepEqual(codexPending(), ['req-codex-b002'], 'and not another on the next read');
  // A row named at the send never counts as new, whatever it claims.
  c.settleOutbox(codex, [{ ...named[0], clientId: 'req-codex-b002' }, named[1]]);
  assert.deepEqual(codexPending(), ['req-codex-b002']);
  // Rows without ids (a Claude read): only what follows the last message the bubble saw.
  const claude = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' }).entry;
  const shown = [{ role: 'user', text: '계속', ts: ts - 10 }, { role: 'assistant', text: '네', ts: ts - 5 }];
  claude.outbox = [bubble('req-claude-a001', '계속', shown), bubble('req-claude-b002', '계속', shown)];
  assert.equal(claude.outbox[0].known, null);
  const claudePending = () => claude.outbox.map((item) => item.requestId);
  c.settleOutbox(claude, shown);
  assert.deepEqual(claudePending(), ['req-claude-a001', 'req-claude-b002']);
  const next = { role: 'user', text: '계속', ts };
  c.settleOutbox(claude, [...shown, next]);
  assert.deepEqual(claudePending(), ['req-claude-b002'], 'one new row, one bubble');
  c.settleOutbox(claude, [...shown, next]);
  assert.deepEqual(claudePending(), ['req-claude-b002']);
  // The server's window moved on (the oldest message dropped): the bubbles' last message is found again.
  c.settleOutbox(claude, [shown[1], next]);
  assert.deepEqual(claudePending(), ['req-claude-b002']);
  c.settleOutbox(claude, [shown[1], next, { role: 'user', text: '계속', ts: ts + 1 }]);
  assert.deepEqual(claudePending(), []);
  // A Claude message names its file in a "첨부 파일:" line; the same one from before the send does not count.
  const withFile = [{ role: 'user', text: '확인\n첨부 파일: /work/a.png', ts: ts - 10 }];
  claude.outbox = [bubble('req-claude-c003', '확인', withFile, { attachments: [{ id: 'f', name: 'a.png', type: 'image/png', kind: 'image' }] })];
  c.settleOutbox(claude, withFile);
  assert.deepEqual(claudePending(), ['req-claude-c003']);
  c.settleOutbox(claude, [...withFile, { role: 'user', text: '확인\n첨부 파일: /work/a.png', ts: ts + 2 }]);
  assert.deepEqual(claudePending(), []);
  // What settled stays remembered per conversation only up to a limit.
  claude.outboxClaims = Array.from({ length: 200 }, (_, index) => ({ id: null, key: `old-${index}`, base: 0, at: now }));
  claude.outbox = [bubble('req-claude-d004', '끝', [])];
  c.settleOutbox(claude, [{ role: 'user', text: '끝', ts: ts + 3 }]);
  assert.deepEqual(claudePending(), []);
  assert.equal(claude.outboxClaims.length, 80);
  assert.equal(claude.outboxClaims.at(-1).key, JSON.stringify([ts + 3, '끝']));
});

test('a Claude image message recorded as "[Image #1]본문" settles its bubble, so neither "작업 중" nor the receipt note stays', async () => {
  const now = Math.floor(Date.now() / 1000);
  const sends = []; let stage = 'before';
  const native = { id: null, name: '이미지', type: 'image/*', size: null, kind: 'image', source: 'native' };
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body);
      return { ok: true, status: 200, json: async () => ({ status: 'accepted', receipt: { confirmed: true, acknowledged: true,
        attachments: [{ id: 'file-a', name: 'shot.png', type: 'image/png', size: 10, kind: 'image', source: 'sessionholic' }] } }) };
    }
    const messages = [{ role: 'assistant', text: '이전 답변', ts: now - 60 }];
    // Claude Code turned the pasted "첨부 파일: <경로>" line into an image and "[Image #1]".
    if (stage !== 'before') messages.push({ role: 'user', text: '[Image #1]코덱스로 이어서 하려는데 여기서 막힘', ts: now + 1, attachments: [native] });
    if (stage === 'answered') messages.push({ role: 'assistant', text: '확인했습니다', ts: now + 5 }, { role: 'assistant', text: '이어서 정리했습니다', ts: now + 9 });
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true }, phase: 'working',
      receipts: body.requestId ? [{ requestId: body.requestId, status: 'accepted', confirmed: true, acknowledged: true }] : [], messages }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  wireTree(c, c.buildChatComposer(s)); await settle();
  await c.loadChatState(s, true);
  const list = c.get('messages');
  const bubbles = () => list.children.filter((row) => /is-outbox/.test(row.className));
  const working = () => list.children.some((row) => /msg-working/.test(row.className));
  entry.text = '코덱스로 이어서 하려는데 여기서 막힘';
  entry.attachments = [{ id: 'file-a', name: 'shot.png', type: 'image/png', size: 10 }];
  await c.sendChat(entry); await settle();
  assert.equal(sends.length, 1);
  assert.equal(bubbles().length, 1, 'not in the transcript yet');
  assert.match(c.get('chat-status').textContent, /메시지를 접수했습니다/);
  assert.equal(c.get('chat-delivery-evidence').hidden, false);
  assert.equal(working(), true, 'the conversation waits on the agent');
  stage = 'answered';
  await c.loadChatState(s, true); await settle();
  assert.equal(bubbles().length, 0, 'its own message, as Claude Code recorded it, settled the bubble');
  assert.equal(entry.outbox.length, 0);
  assert.equal(working(), false, 'the agent answered: the newest row is its answer');
  assert.doesNotMatch(c.get('chat-status').textContent, /접수했습니다/, 'the receipt note went with the bubble');
  assert.equal(c.get('chat-delivery-evidence').hidden, true, 'the message\'s own chips show its file now');
  const own = list.children.find((row) => row.messageItem?.message.text.startsWith('[Image #1]'));
  assert.ok(own, 'the transcript row is drawn once');
  assert.doesNotMatch(textOf(own), /\[Image #1\]/, 'the image token is not drawn next to the image chip');
  assert.match(textOf(own), /코덱스로 이어서 하려는데 여기서 막힘/);
  assert.equal(c.messageActions(s, own.messageItem)[0].label, '복사');
  // The token sits before, after or instead of the words; spaces around it vary.
  for (const text of ['[Image #1] 확인', '확인 [Image #1]', '확인\n[Image #1]', '[Image #1]확인\n첨부 파일: [Image #2]']) {
    const shape = c.outboxShape({ role: 'user', text, attachments: [native] });
    assert.equal(shape.body, '확인', text);
    assert.ok(shape.counts.has(1), text);
  }
  assert.equal(c.outboxShape({ role: 'user', text: '[Image #1]', attachments: [native] }).body, '');
  assert.equal(c.outboxShape({ role: 'user', text: '[Pasted text #1 +12 lines]' }).body, '');
});

test('a delivered Claude bubble the transcript words differently takes the next new user message, or goes after an answer to it', () => {
  const c = client(undefined, noChatTimers);
  const now = Date.now(); const ts = Math.floor(now / 1000);
  const { entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  const seen = [{ role: 'user', text: '처음 요청', ts: ts - 100 }, { role: 'assistant', text: '네', ts: ts - 90 }];
  const bubble = (requestId, text, extra = {}) => ({ requestId, text, attachments: [], state: 'sent', status: 'accepted', reason: '',
    at: now, ...c.outboxOrigin(seen), ...extra });
  const pending = () => entry.outbox.map((item) => item.requestId);
  // Every bubble is matched by its own text first: a reworded message never takes a later bubble's row.
  entry.outbox = [bubble('req-a-0001', '긴 붙여넣기'), bubble('req-b-0002', '짧은 확인')];
  const shortRow = { role: 'user', text: '짧은 확인', ts: ts + 1 };
  c.settleOutbox(entry, [...seen, shortRow]);
  assert.deepEqual(pending(), ['req-a-0001'], 'B by its own text; nothing new is left for A');
  c.settleOutbox(entry, [...seen, shortRow, { role: 'user', text: '[Pasted text #1 +12 lines]', ts: ts + 2 }]);
  assert.deepEqual(pending(), [], 'A takes the next new message, one for one');
  // Not a message from before the send, and never for a bubble the server has not taken.
  entry.outbox = [bubble('req-c-0003', '확인', { state: 'uncertain', status: 'unknown' }), bubble('req-d-0004', '확인', { status: 'queued' }),
    bubble('req-e-0005', '확인', { state: 'failed', status: 'failed' })];
  c.settleOutbox(entry, [...seen, { role: 'user', text: '다른 말', ts: ts + 3 }]);
  assert.deepEqual(pending(), ['req-c-0003', 'req-d-0004', 'req-e-0005']);
  entry.outbox = [bubble('req-f-0006', '확인')];
  c.settleOutbox(entry, [...seen, { role: 'user', text: '예전 말', ts: ts - 120 }]);
  assert.deepEqual(pending(), ['req-f-0006']);
  // A Codex bubble still settles only by its request id.
  const codex = selectChat(c).entry;
  codex.outbox = [bubble('req-codex-0007', '계속')];
  c.settleOutbox(codex, [...seen, { role: 'user', text: '터미널에서 입력', ts: ts + 4, itemId: 'item-typed', clientId: null },
    { role: 'assistant', text: '답', ts: ts + 5, itemId: 'item-answer', clientId: null }]);
  assert.deepEqual(codex.outbox.map((item) => item.requestId), ['req-codex-0007']);
  // No message of its own (yet), but the agent answered after it.
  entry.outbox = [bubble('req-g-0008', '이미지 확인')];
  const answered = [...seen, { role: 'assistant', text: '확인했습니다', ts: ts + 6 }];
  c.settleOutbox(entry, answered, { working: true });
  assert.deepEqual(pending(), ['req-g-0008'], 'while the agent works, its own message gets a minute to show');
  entry.outbox[0].at = now - 61 * 1000;
  c.settleOutbox(entry, answered, { working: true });
  assert.deepEqual(pending(), []);
  entry.outbox = [bubble('req-h-0009', '이미지 확인')];
  c.settleOutbox(entry, [...seen, { role: 'assistant', text: '보내기 직전 답', ts: ts - 1 }]);
  assert.deepEqual(pending(), ['req-h-0009'], 'an answer recorded before the send is no reply to it');
  c.settleOutbox(entry, [...seen, { role: 'assistant', text: '답', ts: ts + 7 }]);
  assert.deepEqual(pending(), [], 'an agent that answered after it and stopped');
  // The server read the conversation and found the message: that settles it too,
  // for Claude and for Codex, and the composer's receipt note goes with it.
  for (const target of [entry, codex]) {
    target.outbox = [bubble('req-i-0010', '어떤 말')];
    target.pending = null;
    c.applyChatReceipt(target, { requestId: 'req-i-0010', status: 'accepted', readbackConfirmed: false });
    c.settleOutbox(target, seen, { working: true });
    assert.equal(target.outbox.length, 1, 'taken, not yet found');
    c.applyChatReceipt(target, { requestId: 'req-i-0010', status: 'accepted', readbackConfirmed: true });
    c.settleOutbox(target, seen, { working: true });
    assert.equal(target.outbox.length, 0);
    assert.equal(target.settledRequestId, 'req-i-0010');
  }
});

test('the composer note: a question waiting beats the receipt note, and an ended request yields to a newer running turn', () => {
  const c = client(undefined, noChatTimers);
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s));
  const status = () => c.get('chat-status').textContent;
  entry.receipt = { requestId: 'req-done-0001', status: 'completed' };
  c.updateChatComposer(entry);
  assert.match(status(), /응답이 끝났습니다.*목표 완료/);
  // A new turn (typed in the terminal) runs now: the old "completed" is no news.
  entry.data = { ...entry.data, phase: 'working' };
  c.updateChatComposer(entry);
  assert.match(status(), /^작업 중/);
  entry.receipt = { requestId: 'req-stop-0002', status: 'interrupted' };
  c.updateChatComposer(entry);
  assert.match(status(), /^작업 중/);
  // The agent asks something: that comes before "접수했습니다".
  entry.receipt = { requestId: 'req-wait-0003', status: 'accepted' };
  entry.data = { ...entry.data, phase: 'needs_input' };
  c.updateChatComposer(entry);
  assert.match(status(), /답변·승인 필요/);
  entry.data = { ...entry.data, phase: 'working' };
  c.updateChatComposer(entry);
  assert.match(status(), /메시지를 접수했습니다/, 'until its bubble settles');
  entry.settledRequestId = 'req-wait-0003';
  c.updateChatComposer(entry);
  assert.match(status(), /^작업 중/);
  // A refused send keeps saying its draft was not sent, question or not.
  entry.receipt = { requestId: 'req-refused-0004', status: 'failed', confirmed: false, dispatchState: 'not_started',
    reason: '메뉴가 떠 있어 보내지 않았습니다.' };
  entry.data = { ...entry.data, phase: 'needs_input' };
  c.updateChatComposer(entry);
  assert.match(status(), /메뉴가 떠 있어 보내지 않았습니다.*초안과 첨부는 유지/);
});

test('"작업 중" waits only on bubbles still on their way, and a waiting bubble keeps the conversation followed', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {} });
  const { s, entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  wireTree(c, c.buildChatComposer(s));
  const now = Date.now(); const ts = Math.floor(now / 1000);
  const shown = [{ role: 'user', text: '요청', ts: ts - 60 }, { role: 'assistant', text: '답변', ts: ts - 30 }];
  const item = (extra) => ({ requestId: 'req-wait-0001', text: '보낸 말', attachments: [], reason: '', at: now, ...c.outboxOrigin(shown), ...extra });
  const list = c.get('messages');
  const working = () => list.children.some((row) => /msg-working/.test(row.className));
  const bubbles = () => list.children.filter((row) => /is-outbox/.test(row.className)).length;
  entry.data.phase = 'working';
  entry.outbox = [item({ state: 'failed', status: 'failed', reason: '거절', settledAt: now })];
  c.renderMessages(s, shown);
  assert.equal(bubbles(), 1, 'a refused bubble stays to be resent');
  assert.equal(working(), false, 'but nothing waits on it');
  entry.outbox = [item({ state: 'uncertain', status: 'unknown' })];
  c.renderMessages(s, shown);
  assert.equal(working(), true, 'a message on its way');
  // A Codex request that already ended (the transcript never showed it) waits on nothing.
  const codex = selectChat(c);
  wireTree(c, c.buildChatComposer(codex.s));
  codex.entry.data.phase = 'working';
  codex.entry.outbox = [item({ state: 'sent', status: 'completed', settledAt: now })];
  c.renderMessages(codex.s, shown);
  assert.equal(working(), false);
  // An idle conversation is still read while a bubble waits for its message.
  const claude = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  claude.entry.data.phase = 'idle';
  claude.entry.outbox = [item({ state: 'sent', status: 'accepted' })];
  const count = timers.length;
  c.scheduleChatPoll(claude.s, claude.entry);
  assert.equal(timers.length, count + 1);
  claude.entry.outbox = [item({ state: 'failed', status: 'failed', settledAt: now })];
  c.scheduleChatPoll(claude.s, claude.entry);
  assert.equal(timers.length, count + 1, 'not for a refused one');
});

test('a failed xterm stylesheet fails the terminal load, and opening again retries only that file', async () => {
  const c = client(undefined, noChatTimers);
  const appended = []; const removed = [];
  c.context.document.head = { appendChild(item) { item.remove = () => removed.push(item); appended.push(item); } };
  c.context.document.querySelector = (selector) => (selector === 'meta[name="sessionholic-assets"]'
    ? { getAttribute: () => JSON.stringify({ '/vendor/xterm.js': 'abcdef12', '/vendor/addon-fit.js': '1234abcd', '/vendor/xterm.css': '0badc0de' }) } : null);
  const first = c.loadXterm();
  c.context.window.Terminal = function Terminal() {};
  c.context.window.FitAddon = { FitAddon: function FitAddon() {} };
  appended[1].onload(); appended[2].onload();
  appended[0].onerror();
  await assert.rejects(first, /받지 못했습니다/);
  assert.deepEqual(removed, [appended[0]], 'the failed stylesheet is not left in the page');
  const second = c.loadXterm();
  assert.deepEqual(appended.slice(3).map((item) => item.href || item.src), ['/vendor/xterm.css?v=0badc0de'], 'only the stylesheet again');
  appended[3].onload();
  await second;
  await c.loadXterm();
  assert.equal(appended.length, 4, 'nothing more once all three loaded');
  // Scripts that ran without defining the terminal are asked again too.
  const e = client(undefined, noChatTimers);
  const tried = [];
  e.context.document.head = { appendChild(item) { tried.push(item); } };
  const broken = e.loadXterm();
  for (const item of tried) item.onload();
  await assert.rejects(broken, /올바르지 않습니다/);
  e.loadXterm();
  assert.deepEqual(tried.slice(3).map((item) => item.src), ['/vendor/xterm.js', '/vendor/addon-fit.js']);
  // Opening a terminal whose scripts are present but whose stylesheet fails
  // reports it instead of drawing an unstyled terminal.
  const d = client(undefined, noChatTimers);
  d.context.window.Terminal = function Terminal() {};
  d.context.window.FitAddon = { FitAddon: function FitAddon() {} };
  d.context.document.head = { appendChild(item) { queueMicrotask(() => (item.tagName === 'LINK' ? item.onerror() : item.onload())); } };
  await d.openTerminal({ id: 'term-1', mode: 'attach' });
  assert.match(d.get('terminal-status').textContent, /불러오지 못했습니다/);
  assert.match(d.get('terminal-error').textContent, /다시 열어/);
});

test('a long press opens its menu after 450ms, while a drag, an early release or a mouse never does', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; } });
  assert.equal(c.GESTURE.longPressMs, 450); assert.equal(c.GESTURE.pressSlopPx, 10);
  const fired = [];
  const press = c.createLongPress((target) => fired.push(target));
  const down = (extra = {}) => ({ pointerId: 1, pointerType: 'touch', clientX: 100, clientY: 200, ...extra });
  press.start(down(), 'row-a');
  assert.equal(timers.at(-1).delay, 450);
  press.move({ pointerId: 1, clientX: 106, clientY: 205 });
  timers.at(-1).callback();
  assert.deepEqual(fired, ['row-a'], 'a small wobble still counts as a press');
  assert.equal(press.end({ pointerId: 1 }), true, 'the click after a fired press is swallowed');
  press.start(down(), 'row-b');
  press.move({ pointerId: 1, clientX: 100, clientY: 212 });
  assert.equal(timers.at(-1).cleared, true, 'moving past the slop (a scroll) cancels');
  timers.at(-1).callback();
  press.start(down(), 'row-c');
  assert.equal(press.end({ pointerId: 1 }), false, 'a release before 450ms is an ordinary tap');
  timers.at(-1).callback();
  assert.deepEqual(fired, ['row-a']);
  press.start(down({ pointerType: 'mouse' }), 'row-d');
  assert.equal(press.active, null, 'a mouse uses the right-click menu instead');
  press.start(down(), 'row-e');
  assert.equal(press.fire(), true, "Android's contextmenu opens it at once");
  assert.equal(press.fire(), false);
  assert.deepEqual(fired, ['row-a', 'row-e']);
  // Wired to a conversation: the long press opens the message menu and its click is ignored.
  const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'assistant', text: '길게 눌러 볼 답변' }]);
  const list = c.get('messages');
  const row = list.children[0];
  vm.runInContext('bindMessageGestures', c.context)(list);
  const target = { closest: (selector) => (selector === '.msg' ? row : null) };
  list.events.pointerdown({ ...down(), target });
  timers.at(-1).callback();
  assert.equal(c.get('action-sheet').open, true);
  assert.deepEqual(c.get('action-list').children.map((item) => item.textContent), ['복사', '텍스트 선택', '터미널에서 보기']);
  list.events.pointerup({ pointerId: 1 });
  let stopped = 0;
  list.events.click({ preventDefault() { stopped++; }, stopPropagation() { stopped++; } });
  assert.equal(stopped, 2);
  let prevented = 0;
  list.events.contextmenu({ target, clientX: 10, clientY: 10, preventDefault() { prevented++; } });
  assert.equal(prevented, 1, 'the native long-press menu stays closed');
  // A right click (no press in progress) opens the same menu at the pointer.
  c.closeActionSheet();
  c.context.Date = { now: () => 1e13 };
  list.events.contextmenu({ target, clientX: 10, clientY: 10, preventDefault() { prevented++; } });
  assert.equal(prevented, 2); assert.equal(c.get('action-sheet').open, true);
});

test('a card swipe needs a sideways drag within about 30 degrees and settles open, back or into its action', () => {
  const c = client(undefined, noChatTimers);
  assert.equal(c.swipeIntent(-6, 2), null, 'inside the slop nothing is decided');
  assert.equal(c.swipeIntent(-30, 5), 'swipe');
  assert.equal(c.swipeIntent(-17, 10), 'swipe', 'up to about 30 degrees');
  assert.equal(c.swipeIntent(-16, 10), 'scroll', 'steeper drags scroll the list');
  assert.equal(c.swipeIntent(2, 40), 'scroll');
  assert.equal(c.swipeIntent(30, 2), 'scroll', 'a rightward drag on a closed card is left to the browser');
  assert.equal(c.swipeIntent(30, 2, true), 'swipe', 'but closes an open card');
  const release = (fields) => c.swipeRelease({ width: 380, ...fields });
  assert.equal(release({ offset: -20, velocity: 0 }), 'close');
  assert.equal(release({ offset: -60, velocity: 0 }), 'open');
  assert.equal(release({ offset: -30, velocity: -0.8 }), 'open', 'a quick flick opens');
  assert.equal(release({ offset: -230, velocity: 0 }), 'action', 'past 60% of the card runs the action');
  assert.equal(release({ offset: -80, velocity: 0.6 }), 'close', 'flicking back closes');
});

test('a swipe offers 터미널 열기, or 연결 닫기 when the session has a board terminal, and the same actions open from a long press', () => {
  const c = client(undefined, noChatTimers);
  const s = recentSession('swipe-me');
  setRecentSnapshot(c, [s]); c.renderGroups([s], 1100);
  const row = byClass(c.get('groups'), 'card-row')[0];
  assert.equal(row.card, byClass(c.get('groups'), 'card')[0]);
  assert.equal(row.swipeAction.attributes['aria-hidden'], 'true', 'the swipe button stays out of the focus order');
  assert.equal(c.swipeActionFor(s).label, '터미널 열기');
  c.state.terminals = [{ id: 'attached', alive: true, mode: 'attach',
    metadata: { nativeSource: { host: s.host, agent: s.agent, home: s.home, id: s.id }, mode: 'attach' } }];
  assert.equal(c.terminalForSession(s).id, 'attached');
  assert.equal(c.swipeActionFor(s).label, '연결 닫기');
  c.state.terminals = [{ id: 'gone', alive: false, sourceKey: JSON.stringify({ agent: s.agent, home: s.home, host: s.host, id: s.id }) },
    { id: 'launched', alive: true, mode: 'handoff', sourceKey: JSON.stringify({ agent: s.agent, home: s.home, host: s.host, id: s.id }) }];
  assert.equal(c.terminalForSession(s).id, 'launched', 'a terminal launched from the session counts; a closed one does not');
  assert.equal(c.swipeActionFor(s).label, '실행 종료');
  // Touch drag on the list: the card follows the finger and stays open past half the button.
  const groups = c.get('groups');
  vm.runInContext('bindCardGestures', c.context)(groups);
  const target = { closest: (selector) => (selector === '.card-row' ? row : null) };
  let prevented = 0;
  groups.events.pointerdown({ target, pointerId: 3, pointerType: 'touch', clientX: 300, clientY: 100 });
  groups.events.pointermove({ target, pointerId: 3, clientX: 280, clientY: 103, cancelable: true, preventDefault() {} });
  groups.events.touchmove({ cancelable: true, preventDefault() { prevented++; } });
  groups.events.pointermove({ target, pointerId: 3, clientX: 200, clientY: 106, cancelable: true, preventDefault() {} });
  assert.equal(row.swipeOffset, -100);
  assert.equal(prevented, 1, 'a swiping finger does not also scroll the list');
  assert.equal(row.swipeAction.textContent, '실행 종료');
  groups.events.pointerup({ pointerId: 3 });
  assert.equal(c.state.swipeOpen, row); assert.equal(row.swipeOffset, -c.GESTURE.swipeRevealPx);
  assert.match(row.className, /is-open/);
  // The revealed button takes its first tap right after the swipe.
  let swallowed = 0;
  groups.events.click({ target: { closest: (selector) => (selector === '.card-swipe-action' ? row.swipeAction : null) },
    preventDefault() { swallowed++; }, stopPropagation() { swallowed++; } });
  assert.equal(swallowed, 0);
  row.swipeAction.events.click();
  assert.equal(c.get('close-terminal-dialog').open, true, 'its action runs (closing asks first)');
  assert.equal(c.state.swipeOpen, null);
  c.cancelTerminalClose();
  c.prepareSwipe(row); c.settleSwipe(row, 'open');
  // Tapping the open card closes it instead of opening the conversation.
  c.context.Date = { now: () => 1e13 };
  row.card.events.click();
  assert.equal(c.state.swipeOpen, null); assert.equal(c.state.selected, null);
  // The action asks for confirmation (the existing close sheet); nothing closes directly.
  c.prepareSwipe(row); c.settleSwipe(row, 'action');
  assert.equal(c.get('close-terminal-dialog').open, true);
  assert.equal(c.get('close-terminal-confirm').textContent, '실행 종료');
  c.cancelTerminalClose();
  c.openCardMenu(row);
  assert.deepEqual(c.get('action-list').children.map((item) => item.textContent), ['대화 열기', '세션 관리', '터미널로 돌아가기', '실행 종료']);
  // A vertical drag is a scroll: no swipe, no menu.
  groups.events.pointerdown({ target, pointerId: 4, pointerType: 'touch', clientX: 300, clientY: 100 });
  groups.events.pointermove({ target, pointerId: 4, clientX: 296, clientY: 130, cancelable: true, preventDefault() {} });
  groups.events.pointerup({ pointerId: 4 });
  assert.equal(c.state.swipeOpen, null);
});

test('consecutive messages from one speaker group under one name and time, with tighter spacing, and regroup on new rows', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const messages = [{ role: 'assistant', text: '첫 답변', ts: 1000 }, { role: 'assistant', text: '이어지는 답변', ts: 1060 },
    { role: 'user', text: '질문', ts: 1100 }, { role: 'user', text: '추가 질문', ts: 1110 },
    { role: 'tool', text: '도구 기록' }, { role: 'assistant', text: '도구 뒤 답변', ts: 1200 },
    { role: 'assistant', text: '한참 뒤 답변', ts: 1900 }];
  c.renderMessages(s, messages);
  const flags = () => c.get('messages').children.map((row) =>
    [/is-cont/.test(row.className), /has-next/.test(row.className), row.msgLabel ? row.msgLabel.hidden : null]);
  assert.deepEqual(flags(), [[false, true, false], [true, false, true], [false, true, false], [true, false, true],
    [false, false, null], [false, false, false], [false, false, false]]);
  const last = c.get('messages').children[6];
  c.renderMessages(s, [...messages, { role: 'assistant', text: '바로 이어진 답변', ts: 1930 }]);
  assert.equal(c.get('messages').children[6], last, 'the row is reused');
  assert.deepEqual(flags().slice(6), [[false, true, false], [true, false, true]]);
  assert.match(styleSheet(), /\.msg\.is-cont \{\s*margin-top: -10px;/);
});

test('a working agent shows "··· 작업 중" after the newest message, still under reduced motion, and it leaves when the turn ends', () => {
  const c = client(undefined, noChatTimers); const { s, entry } = selectChat(c);
  entry.data.phase = 'working';
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  const list = c.get('messages');
  assert.match(list.children.at(-1).className, /msg-working/);
  assert.match(textOf(list.children.at(-1)), /작업 중/);
  assert.equal(list.children.at(-1).attributes.role, 'status');
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }, { role: 'tool', text: '$ npm test' }]);
  assert.match(list.children.at(-1).className, /msg-working/, 'it stays last as tool records arrive');
  entry.data.phase = 'idle';
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }, { role: 'assistant', text: '끝났습니다' }]);
  assert.equal(list.children.some((row) => /msg-working/.test(row.className)), false);
  assert.match(styleSheet(), /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.working-dots i \{\s*animation: none;/);
});

test('"작업 중" shows only while the conversation waits on the agent: after a user message or tool records, never after an answer or a system line', () => {
  const armed = [];
  const c = client(undefined, { setTimeout: (callback, ms) => { armed.push({ callback, ms }); return armed.length; },
    clearTimeout: (id) => { if (armed[id - 1]) armed[id - 1].cleared = true; } });
  const ticking = () => armed.filter((timer) => timer.callback === c.context.tickWorking && !timer.cleared);
  const { s, entry } = selectChat(c);
  entry.data.phase = 'working';
  entry.data.workingSince = Math.floor(Date.now() / 1000) - 90;
  const list = c.get('messages');
  const working = () => list.children.filter((row) => /msg-working/.test(row.className));
  const ask = { role: 'user', text: '테스트를 고쳐 주세요' };
  const tool = (n) => ({ role: 'tool', text: `$ npm test -- --shard=${n}` });
  const answer = { role: 'assistant', text: '고쳤습니다. 테스트가 모두 통과합니다.' };
  c.renderMessages(s, [ask]);
  assert.equal(working().length, 1); assert.equal(list.children.at(-1), working()[0], 'a user message waits for its first answer');
  assert.equal(ticking().length, 1);
  c.renderMessages(s, [ask, tool(1), tool(2)]);
  const group = list.children.find((row) => /tool-group/.test(row.className));
  assert.ok(group); assert.equal(list.children.at(-1), working()[0], 'tool records mean something runs now');
  group.toolToggle.events.click();
  // The agent's answer is on screen: background tools may still run, but the row leaves and its clock stops.
  c.renderMessages(s, [ask, tool(1), tool(2), answer]);
  assert.equal(working().length, 0);
  assert.equal(ticking().length, 0, 'no clock for a row that is not shown');
  assert.equal(list.children.at(-1).messageItem.message.text, answer.text);
  c.renderMessages(s, [ask, tool(1), tool(2), answer, { role: 'system', text: '세션을 다시 시작했습니다.' }]);
  assert.equal(working().length, 0, 'a system line ends nothing on screen either');
  // More tool records after the answer: the same row returns and counts again.
  const row = list.workingRow;
  c.renderMessages(s, [ask, tool(1), tool(2), answer, tool(3)]);
  assert.equal(list.children.at(-1), row); assert.equal(ticking().length, 1);
  assert.match(row.workingTime.textContent, /^ · 1분 3[01]초$/);
  assert.equal(group.open, true, 'an opened tool group stays open while the row comes and goes');
  // Phase "working" alone, with nothing read yet (a Claude row without an id): the row stays.
  c.renderMessages(s, []);
  assert.equal(list.children.at(-1), row);
  assert.doesNotMatch(textOf(list), /저장된 대화를 아직/);
});

test('Claude\'s approximate start shows "··· 작업 중" without a time or a ticking clock; the status line carries no time', () => {
  const armed = [];
  const c = client(undefined, { setTimeout: (callback, ms) => { armed.push({ callback, ms }); return armed.length; },
    clearTimeout: (id) => { if (armed[id - 1]) armed[id - 1].cleared = true; } });
  const ticking = () => armed.filter((timer) => timer.callback === c.context.tickWorking && !timer.cleared);
  const { s, entry } = selectChat(c, { agent: 'claude', home: '.claude', id: 'claude-session' });
  entry.data.phase = 'working';
  // The server's newest-prompt time for Claude: a guess, never shown.
  entry.data.workingSince = Math.floor(Date.now() / 1000) - 754;
  entry.data.workingSinceApprox = true;
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  const list = c.get('messages'); const row = list.children.at(-1);
  assert.match(row.className, /msg-working/);
  assert.equal(textOf(row).trim(), '작업 중');
  assert.equal(row.workingTime.textContent, '');
  assert.ok(!row.title, 'no note about an approximate time either');
  assert.equal(ticking().length, 0, 'nothing to count');
  c.tickWorking();
  assert.equal(ticking().length, 0);
  // An answer on screen ends it here as well, though the session is still working.
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }, { role: 'assistant', text: '끝났습니다' }]);
  assert.equal(list.children.includes(row), false);
  vm.runInContext('updateChatComposer([...state.chats.values()][0])', c.context);
  assert.equal(c.get('chat-status').textContent, '작업 중 · 지금 보내도 CLI가 이어서 반영합니다.', 'the session state stays in the status line');
});

test('picked images preview from memory with an upload ring, move to the uploaded file, and are released when removed', async () => {
  const created = []; const revoked = [];
  const c = client(undefined, noChatTimers);
  c.context.URL = class extends URL {};
  c.context.URL.createObjectURL = (file) => { created.push(file.name); return `blob:preview-${created.length}`; };
  c.context.URL.revokeObjectURL = (url) => revoked.push(url);
  const sent = [];
  c.context.XMLHttpRequest = class {
    constructor() { this.headers = {}; this.listeners = {}; this.upload = { addEventListener: (type, callback) => { this.onProgress = callback; } }; sent.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    send(body) { this.body = body; }
  };
  const { s, entry } = selectChat(c);
  wireTree(c, c.buildChatComposer(s)); await settle();
  const photo = { name: '화면.png', size: 2048, type: 'image/png' };
  const pending = c.uploadChatFiles(entry, [photo]);
  await settle();
  const request = sent[0];
  assert.equal(request.method, 'POST'); assert.equal(request.url, '/api/chat/upload'); assert.equal(request.body, photo);
  assert.equal(request.headers['X-CSRF-Token'], 'csrf-test');
  assert.equal(JSON.parse(decodeURIComponent(request.headers['X-Chat-Source'])).home, '.codex-example');
  assert.equal(decodeURIComponent(request.headers['X-File-Name']), '화면.png');
  const tile = byClass(c.get('chat-attachments'), 'chat-attachment')[0];
  assert.match(tile.className, /is-image is-uploading/);
  assert.equal(byClass(tile, 'chat-attachment-preview')[0].children[0].src, 'blob:preview-1');
  const ring = byClass(tile, 'upload-ring')[0];
  assert.equal(ring.attributes.role, 'progressbar');
  request.onProgress({ lengthComputable: true, loaded: 512, total: 2048 });
  assert.equal(ring.attributes['aria-valuenow'], '25');
  request.status = 200;
  request.responseText = JSON.stringify({ attachment: { id: 'up-1', name: '화면.png', size: 2048, type: 'image/png' } });
  request.listeners.load();
  await pending; await settle();
  assert.deepEqual(Array.from(entry.attachments, (item) => item.id), ['up-1']);
  const done = byClass(c.get('chat-attachments'), 'chat-attachment')[0];
  assert.doesNotMatch(done.className, /is-uploading/);
  assert.equal(byClass(done, 'chat-attachment-preview')[0].children[0].src, 'blob:preview-1', 'the preview follows the upload');
  assert.deepEqual(revoked, []);
  byClass(done, 'chat-attachment-remove')[0].events.click();
  assert.deepEqual(revoked, ['blob:preview-1'], 'released with its tile');
  // A failed upload releases its preview too, and locking releases whatever is left.
  const again = c.uploadChatFiles(entry, [{ name: '두번째.png', size: 10, type: 'image/png' }]);
  await settle();
  sent[1].listeners.error();
  await again; await settle();
  assert.deepEqual(revoked, ['blob:preview-1', 'blob:preview-2']);
  assert.match(entry.error, /첨부파일을 올리지 못했습니다/);
  c.attachmentThumb(entry, 'left-over', { name: 'x.png', type: 'image/png' });
  c.showLogin();
  assert.deepEqual(revoked, ['blob:preview-1', 'blob:preview-2', 'blob:preview-3']);
});

test('the board text size is its own stored choice scaling the rem type scale, apart from the terminal size', () => {
  const c = client(undefined, noChatTimers);
  const root = c.context.document.documentElement;
  root.attributes = {};
  root.setAttribute = (name, value) => { root.attributes[name] = String(value); };
  root.removeAttribute = (name) => { delete root.attributes[name]; };
  const writes = [];
  c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  c.setTextSize('large');
  assert.equal(root.attributes['data-text-size'], 'large');
  assert.equal(c.get('text-size-large').checked, true); assert.equal(c.get('text-size-default').checked, false);
  c.setTextSize('large');
  c.setTextSize('huge');
  assert.equal(c.state.textSize, 'default'); assert.equal(root.attributes['data-text-size'], undefined);
  assert.deepEqual(writes, [['sessionholic:ui:textSize', '"large"'], ['sessionholic:ui:textSize', '"default"']]);
  assert.equal(c.state.fontSize, 14, 'the terminal text size is untouched');
  c.context.navigator = { onLine: false };
  c.windowEvents.DOMContentLoaded();
  c.get('text-size-small').events.change({ target: { checked: true } });
  assert.equal(root.attributes['data-text-size'], 'small');
  const css = styleSheet();
  assert.match(css, /:root\[data-text-size="large"\] \{\s*font-size: 112\.5%;/);
  assert.match(css, /:root\[data-text-size="small"\] \{\s*font-size: 93\.75%;/);
  const tokens = cssBlock(css.replace(/\/\*[\s\S]*?\*\//g, ''), ':root');
  for (const name of ['--fs-head', '--fs-body', '--fs-sub', '--fs-cap']) assert.match(tokens[name], /^[\d.]+rem$/, name);
  for (const name of ['--fs-large', '--fs-title']) assert.match(tokens[name], /^clamp\([\d.]+rem, .*\)$/, name);
  assert.doesNotMatch(css, /content:\s*['"][⌄⌃]['"]/, 'chevrons are drawn, not glyphs');
  assert.match(indexPage(), /id="refresh"[\s\S]{0,200}<svg/, 'the refresh icon is drawn too');
  assert.match(css, /input\.switch \{[^}]*appearance: none;/);
  for (const id of ['show-old', 'terminal-direct-input', 'terminal-accessibility'])
    assert.match(indexPage(), new RegExp(`id="${id}" class="switch" type="checkbox" role="switch"`), id);
});

test('theme-init applies a forced theme and text size before paint, keeps meta originals for auto, and survives blocked storage', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/theme-init.js'), 'utf8');
  const run = (stored, { throws = false, extra = {} } = {}) => {
    const attributes = {};
    const meta = (content) => ({ attributes: { content }, getAttribute(name) { return this.attributes[name] ?? null; },
      setAttribute(name, value) { this.attributes[name] = String(value); } });
    const metas = [meta('#f2f2f7'), meta('#000000')];
    const document = { documentElement: { setAttribute(name, value) { attributes[name] = String(value); } },
      querySelectorAll: (selector) => (selector === 'meta[name="theme-color"]' ? metas : []) };
    const localStorage = { getItem(key) { if (throws) throw new Error('blocked'); return stored[key] ?? null; } };
    vm.runInNewContext(source, { window: { localStorage, ...extra }, document, JSON });
    return { attributes, metas };
  };
  const dark = run({ 'sessionholic:ui:theme': '"dark"', 'sessionholic:ui:textSize': '"large"' });
  assert.deepEqual(dark.attributes, { 'data-theme': 'dark', 'data-text-size': 'large' });
  assert.deepEqual(dark.metas.map((item) => [item.attributes.content, item.attributes['data-original']]),
    [['#000000', '#f2f2f7'], ['#000000', '#000000']]);
  assert.deepEqual(run({ 'sessionholic:ui:theme': '"light"' }).metas.map((item) => item.attributes.content), ['#f2f2f7', '#f2f2f7']);
  const auto = run({ 'sessionholic:ui:theme': '"auto"', 'sessionholic:ui:textSize': '"default"' });
  assert.deepEqual(auto.attributes, {});
  assert.equal(auto.metas[0].attributes['data-original'], undefined);
  assert.deepEqual(run({ 'sessionholic:ui:theme': '{not json' }).attributes, {});
  assert.deepEqual(run({}, { throws: true }).attributes, {}, 'blocked storage keeps the device defaults');
  // An iPad home-screen app gets room for iPadOS's window controls; nothing else does.
  const device = (userAgent, maxTouchPoints, standalone) => ({ navigator: { userAgent, maxTouchPoints },
    matchMedia: (query) => ({ matches: standalone && query === '(display-mode: standalone)' }) });
  const ipadOS = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15';
  assert.deepEqual(run({}, { extra: device(ipadOS, 5, true) }).attributes, { 'data-ipad-app': '' });
  assert.deepEqual(run({}, { extra: device('Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)', 5, true) }).attributes, { 'data-ipad-app': '' });
  assert.deepEqual(run({}, { extra: device(ipadOS, 5, false) }).attributes, {}, 'the same iPad in a Safari tab');
  assert.deepEqual(run({}, { extra: device('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)', 5, true) }).attributes, {});
  assert.deepEqual(run({}, { extra: device(ipadOS, 0, true) }).attributes, {}, 'a Mac browser has no touch points');
  const css = styleSheet();
  assert.match(css, /:root\[data-ipad-app\] \{\s*--top-inset: max\(env\(safe-area-inset-top\), 36px\);/);
  assert.equal((css.match(/env\(safe-area-inset-top\)/g) || []).length, 2, 'every other top inset reads --top-inset');
  const html = indexPage();
  const init = html.indexOf('<script src="/theme-init.js"></script>');
  assert.ok(init > 0, 'loaded without defer');
  assert.ok(init < html.indexOf('<link rel="stylesheet" href="/style.css"'), 'before the stylesheet');
  assert.ok(html.lastIndexOf('name="theme-color"', init) > 0, 'after the theme-color metas it updates');
  assert.match(fs.readFileSync(path.join(__dirname, '../web/sw.js'), 'utf8'), /"\/theme-init\.js",/);
  assert.ok(JSON.parse(fs.readFileSync(path.join(__dirname, '../scripts/public-files.json'), 'utf8')).includes('web/theme-init.js'));
  // app.js keeps the original colors theme-init recorded, so "auto" restores them.
  const c = client(undefined, noChatTimers);
  const metas = dark.metas.map((item) => ({ ...item, dataset: { original: item.attributes['data-original'] },
    getAttribute(name) { return this.attributes[name]; }, setAttribute(name, value) { this.attributes[name] = value; } }));
  c.context.document.querySelectorAll = (selector) => (selector === 'meta[name="theme-color"]' ? metas : []);
  c.applyTheme('auto');
  assert.deepEqual(metas.map((item) => item.attributes.content), ['#f2f2f7', '#000000']);
});

test('the top shows "오프라인 · 다시 연결 중" while offline or after two failed reads in a row, and clears after a good read', async () => {
  let fail = false;
  const c = client(async (url) => {
    if (fail) throw new Error('down');
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot') ? { hosts: [], serverTime: 1 } : { hosts: [], terminals: [] }) };
  }, noChatTimers);
  c.context.navigator = { onLine: true };
  c.windowEvents.DOMContentLoaded(); await settle(10);
  assert.equal(c.state.authenticated, true);
  assert.equal(c.get('offline-banner').hidden, true);
  c.windowEvents.offline();
  assert.equal(c.get('offline-banner').hidden, false);
  assert.equal(c.get('offline-text').textContent, '오프라인 · 다시 연결 중');
  assert.equal(c.get('offline-banner').dataset.kind, 'offline');
  assert.equal(c.get('connection-notice').hidden, true, 'one indicator for one state');
  c.context.navigator.onLine = true; c.windowEvents.online(); await settle(10);
  assert.equal(c.get('offline-banner').hidden, true);
  fail = true;
  await c.poll(false);
  assert.equal(c.get('offline-banner').hidden, true, 'a single failed read retries quietly');
  await c.poll(false);
  assert.equal(c.get('offline-banner').hidden, false);
  assert.equal(c.get('offline-text').textContent, '연결 끊김 · 다시 연결 중');
  c.state.terminalVisible = true; c.renderConnectivity();
  assert.equal(c.get('offline-banner').hidden, true, 'the terminal shows its own connection state');
  c.state.terminalVisible = false;
  fail = false; await c.poll(false);
  assert.equal(c.get('offline-banner').hidden, true);
  assert.match(indexPage(), /<div id="offline-banner" class="offline-banner" role="status" hidden>/);
});

test('the first paint shows skeleton cards, a collection in progress keeps them, and the list replaces them', () => {
  const html = indexPage();
  assert.match(html, /<div id="groups" class="groups"[^>]*aria-busy="true">\s*<div class="skeleton-list" aria-hidden="true">/);
  assert.match(html, /<p class="sr-only" role="status">세션을 확인하고 있습니다…<\/p>/);
  const c = client(undefined, noChatTimers);
  c.state.snapshot = { hosts: [{ name: 'workstation', ok: true, refreshing: true }] };
  c.renderGroups([], 1100);
  assert.equal(byClass(c.get('groups'), 'skeleton-card').length, 4);
  assert.equal(c.get('groups').attributes['aria-busy'], 'true');
  const s = recentSession('ready'); setRecentSnapshot(c, [s]);
  c.renderGroups([s], 1100);
  assert.equal(byClass(c.get('groups'), 'skeleton-card').length, 0);
  assert.equal(c.get('groups').attributes['aria-busy'], 'false');
  assert.match(styleSheet(), /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.skeleton-card span \{\s*animation: none;/);
});

test('a long question menu keeps its first choices inline and lists every choice in a sheet that only selects', async () => {
  const requests = [];
  const c = client(async (url, opts) => { requests.push(url); return { ok: true, status: 200, json: async () => ({ messages: [] }) }; }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  const prompt = { agent: 'codex', fingerprint: 'e'.repeat(64), current: 1, multi: false, question: '어느 브랜치에 반영할까요?\n자세한 설명',
    options: Array.from({ length: 7 }, (_, index) => ({ number: index + 1, label: `브랜치 ${index + 1}`, detail: '' })) };
  entry.data = { ...entry.data, prompt, phase: 'needs_input' };
  c.renderPrompt(s, entry.data);
  const card = c.get('chat-prompt');
  assert.equal(findClass(card, 'chat-prompt-option').length, 4);
  const more = findClass(card, 'chat-prompt-more')[0];
  assert.equal(more.textContent, '선택지 7개 모두 보기');
  more.events.click();
  assert.equal(c.get('action-sheet').open, true);
  assert.equal(c.get('action-title').textContent, '어느 브랜치에 반영할까요?');
  const items = c.get('action-list').children;
  assert.equal(items.length, 7);
  items[5].events.click();
  assert.equal(c.get('action-sheet').open, false);
  assert.equal(entry.promptChoice.number, 6);
  assert.equal(requests.length, 0, 'choosing in the sheet sends nothing');
  const inline = findClass(c.get('chat-prompt'), 'chat-prompt-option');
  assert.deepEqual(inline.map((item) => item.attributes['aria-checked']), ['false', 'false', 'false', 'false', 'true'],
    'the chosen choice joins the inline list');
  assert.match(findClass(c.get('chat-prompt'), 'primary')[0].textContent, /6번 보내기/);
  // Short menus are unchanged: every choice inline, no sheet.
  entry.data = { ...entry.data, prompt: { ...prompt, fingerprint: 'd'.repeat(64), options: prompt.options.slice(0, 5) } };
  c.renderPrompt(s, entry.data);
  assert.equal(findClass(c.get('chat-prompt'), 'chat-prompt-option').length, 5);
  assert.equal(findClass(c.get('chat-prompt'), 'chat-prompt-more').length, 0);
});

// --- Phone conversation bars, explicit terminals, gesture thresholds, batched snapshot events ---

// A phone conversation (coarse pointer, phone width) with a scrolled transcript and a
// controllable clock. `wide`: a tablet instead (coarse pointer, wide screen).
// `folded`: as a conversation opens (집중 모드); otherwise the bars were brought back first.
function phoneConversation({ coarse = true, wide = false, timers = noChatTimers, fetchImpl, folded = false } = {}) {
  const c = client(fetchImpl, timers);
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  const pane = c.get('detail'); pane.classList = classes();
  if (!folded) c.setChromeVisible(true);
  const list = c.get('messages');
  // As after the first render (renderMessages keeps the position it set).
  Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 2400, offsetTop: 80, lastScrollTop: 2400 });
  c.context.window.matchMedia = (query) => ({ matches: coarse &&
    (query === '(pointer: coarse)' || (!wide && query === '(max-width: 700px)')) });
  c.state.detailShown = true; // a phone shows the conversation screen
  const clock = { now: 1e13 };
  c.context.Date = class extends Date { static now() { return clock.now; } };
  const plain = { closest: () => null };
  const pointer = (type, { id = 1, x = 200, y = 400, target = plain, pointerType = 'touch' } = {}) =>
    list.events[type]({ pointerId: id, pointerType, isPrimary: true, clientX: x, clientY: y, target, cancelable: true, preventDefault() {} });
  // `after` ms since the last scroll: down, `ms` later up `dx` px away.
  const tap = (options = {}, { ms = 80, dx = 0, after = 200 } = {}) => {
    clock.now += after;
    pointer('pointerdown', options);
    clock.now += ms;
    if (dx) pointer('pointermove', { ...options, x: (options.x ?? 200) + dx });
    pointer('pointerup', { ...options, x: (options.x ?? 200) + dx });
    clock.now += 400;
  };
  const scrollTo = (top) => { list.scrollTop = top; list.events.scroll(); clock.now += 16; };
  return { c, s, entry, pane, list, clock, pointer, tap, scrollTo, reading: () => pane.classList.contains('is-reading') };
}

test('gesture thresholds live in one frozen object with the values the board used before', () => {
  const c = client(undefined, noChatTimers);
  assert.equal(Object.isFrozen(c.GESTURE), true);
  const expected = { longPressMs: 450, pressSlopPx: 10, tapSlopPx: 10, tapMaxMs: 300, swipeRatio: 1.7, swipeRevealPx: 96,
    swipeActionRatio: 0.6, swipeFlickSpeed: 0.4, pullTriggerPx: 64, pullMaxPx: 110, pullResistPx: 160, readingHideTravelPx: 32,
    readingBottomPx: 24, jumpShowPx: 160, largeTitleCollapsePx: 36, pinchMinFont: 10, pinchMaxFont: 24, momentumMinSpeed: 0.35,
    momentumDecay: 0.94, momentumStopSpeed: 0.03, momentumMaxMs: 1600, sheetDismissRatio: 0.25, sheetDismissSpeed: 0.5,
    sheetFarRatio: 0.5, sheetFarSpeed: 1.4, sheetExpandPx: 48, sheetExpandSpeed: 0.5 };
  for (const [key, value] of Object.entries(expected)) assert.equal(c.GESTURE[key], value, key);
  // The decisions read them: the pull is elastic up to pullMaxPx, the swipe angle and slop are unchanged.
  assert.ok(c.pullDistance(10000) <= c.GESTURE.pullMaxPx);
  assert.equal(c.swipeIntent(-9, 0), null);
  assert.equal(c.swipeIntent(-17, 10), 'swipe');
  assert.equal(c.swipeIntent(-16, 10), 'scroll');
  assert.equal(c.pinchFontSize(14, 100, 1000), c.GESTURE.pinchMaxFont);
  // No stray copies of the old constants remain in the app.
  assert.doesNotMatch(source, /\b(LONG_PRESS_MS|PRESS_SLOP|SWIPE_RATIO|SWIPE_REVEAL|PULL_TRIGGER|FONT_MIN|FONT_MAX|LARGE_TITLE_COLLAPSE)\b/);
});

test('on a phone a tap on the conversation shows or hides its bars; scrolling hides them and the end does not bring them back', () => {
  const { c, pane, list, tap, scrollTo, reading } = phoneConversation();
  assert.equal(c.state.chromeVisible, true, 'a conversation opens with its bars');
  scrollTo(2390);
  assert.equal(reading(), false, 'a 10px wobble keeps them');
  scrollTo(2420); scrollTo(2440);
  assert.equal(reading(), true, 'a 40px downward scroll hides them (the same 32px as upward)');
  assert.equal(c.state.chromeVisible, false);
  scrollTo(2400);
  assert.equal(reading(), true, 'reaching the end keeps them hidden');
  assert.equal(list.unread || 0, 0, 'the end still clears the new-message count');
  tap();
  assert.equal(reading(), false, 'a tap shows them');
  assert.equal(c.state.chromeVisible, true);
  tap();
  assert.equal(reading(), true, 'another tap hides them');
  tap();
  scrollTo(2350);
  assert.equal(reading(), true, 'scrolling up hides them again');
  assert.equal(pane.classList.contains('is-typing'), false);
});

test('a drag, a slow press, a long press, a control, a selection or a touch that stops the list never toggles the bars', () => {
  const callbacks = [];
  const timers = { setTimeout: (callback, ms) => { callbacks.push({ callback, ms }); return callbacks.length; },
    clearTimeout(id) { if (callbacks[id - 1]) callbacks[id - 1].cleared = true; } };
  const { c, list, clock, pointer, tap, reading } = phoneConversation({ timers });
  tap({}, { dx: 11 });
  assert.equal(reading(), false, 'moving past tapSlopPx is a drag');
  tap({}, { dx: 9 });
  assert.equal(reading(), true, 'within tapSlopPx it is a tap');
  tap({}, { ms: 301 });
  assert.equal(reading(), true, 'held past tapMaxMs it is not a tap');
  pointer('pointerdown'); clock.now += 60; pointer('pointercancel'); clock.now += 400;
  assert.equal(reading(), true, 'a scroll ends in pointercancel, not a tap');
  // Long press wins: its menu opens and the release is not a tap.
  const row = { className: 'msg', messageItem: { role: 'assistant', message: { text: '답' } } };
  const onRow = { closest: (selector) => (selector === '.msg' ? row : null) };
  pointer('pointerdown', { target: onRow });
  const press = callbacks.find((item) => item.ms === c.GESTURE.longPressMs && !item.cleared);
  assert.ok(press, 'a long press is armed');
  clock.now += 100; press.callback();
  pointer('pointerup', { target: onRow }); clock.now += 400;
  assert.equal(reading(), true, 'the long press opened the menu instead');
  // Controls keep their own tap.
  for (const selector of ['button', 'a', '.msg-more', '.message-block-copy', '.chat-prompt', '.msg-attachment', 'summary']) {
    const control = { closest: (query) => (query.split(/,\s*/).includes(selector) ? {} : null) };
    tap({ target: control });
    assert.equal(reading(), true, selector);
  }
  c.context.window.getSelection = () => ({ isCollapsed: false });
  tap();
  assert.equal(reading(), true, 'a text selection is kept');
  c.context.window.getSelection = () => ({ isCollapsed: true });
  // A touch right after the list moved only stops its momentum.
  list.scrollTop = 2200; list.events.scroll();
  tap({}, { after: 50 });
  assert.equal(reading(), true);
  tap({}, { after: 200 });
  assert.equal(reading(), false, 'once the list settled a tap works again');
  // Mouse layouts keep their own rules: no tap toggle there.
  c.context.window.matchMedia = () => ({ matches: false });
  tap({ pointerType: 'mouse' });
  assert.equal(reading(), false);
  assert.equal(c.isChromeTap(null, {}), false);
});

test('the board\'s own scrolling, layout changes and typing never hide the bars on a phone', () => {
  const { c, s, list, clock, scrollTo, reading } = phoneConversation();
  // New messages at the end keep the reader at the end without hiding anything.
  c.renderMessages(s, [{ role: 'assistant', text: '첫 답', ts: 1 }], '', true);
  list.scrollHeight = 3400;
  c.renderMessages(s, [{ role: 'assistant', text: '첫 답', ts: 1 }, { role: 'assistant', text: '새 답', ts: 2 }]);
  assert.equal(list.scrollTop, 3400);
  list.events.scroll();
  assert.equal(reading(), false, 'a stick-to-end jump is not reading');
  // A keyboard or the bars resizing the list moves it too.
  list.clientHeight = 300; list.scrollTop = 3000; list.events.scroll();
  assert.equal(reading(), false, 'a resize is not reading');
  list.clientHeight = 600;
  // The smooth "맨 아래" scroll is the board's, until a finger touches the list.
  list.scrollTo = () => {};
  list.scrollTop = 1000; list.lastScrollTop = 1000; list.events.scroll(); list.lastClientHeight = 600;
  c.get('jump-bottom').events.click();
  for (const top of [1400, 1900, 2400]) scrollTo(top);
  assert.equal(reading(), false, 'the jump to the end does not hide the bars');
  clock.now += 2000;
  scrollTo(2350); scrollTo(2300);
  assert.equal(reading(), true, 'the reader\'s own scroll still does');
  // Typing: the input's folded layout; leaving it restores the reader's choice.
  const input = c.get('chat-input');
  const pane = c.get('detail');
  c.context.document.activeElement = input;
  input.events.focus();
  assert.equal(pane.classList.contains('is-typing'), true);
  assert.equal(reading(), true, 'typing preserves the focus choice');
  assert.equal(c.state.chromeVisible, false, 'the reader\'s choice is kept for later');
  scrollTo(1500);
  c.revealChrome();
  assert.equal(c.state.chromeVisible, false);
  assert.equal(reading(), true, 'attention never unfolds the tools under the keyboard');
  c.setChromeVisible(false);
  c.context.document.activeElement = null;
  input.events.blur();
  assert.equal(pane.classList.contains('is-typing'), false);
  assert.equal(reading(), true, 'hidden before typing: hidden again after');
  c.setChromeVisible(true);
  c.context.document.activeElement = input; input.events.focus();
  c.context.document.activeElement = null; input.events.blur();
  assert.equal(reading(), false, 'shown before typing: shown again after');
});

test('questions and failures remain visible without overriding focus mode', async () => {
  const { c, s, entry, tap, reading } = phoneConversation({ fetchImpl: async (url) => {
    if (url === '/api/chat/send') return { ok: false, status: 409, json: async () => ({ error: '지금은 보낼 수 없습니다.', dispatchState: 'not_started' }) };
    if (url === '/api/chat/state') return { ok: false, status: 503, json: async () => ({ error: '연결 실패' }) };
    return { ok: true, status: 200, json: async () => ({}) };
  } });
  c.setChromeVisible(false);
  assert.equal(reading(), true);
  const prompt = { agent: 'codex', fingerprint: 'a'.repeat(64), current: 1, multi: false, question: '진행할까요?',
    options: [{ number: 1, label: 'Yes', detail: '' }, { number: 2, label: 'No', detail: '' }] };
  entry.data = { ...entry.data, prompt, phase: 'needs_input' };
  c.renderPrompt(s, entry.data);
  assert.equal(reading(), true, 'a question preserves focus mode');
  assert.equal(c.get('chat-prompt').hidden, false, 'the approval card stays visible');
  c.setChromeVisible(false);
  c.renderPrompt(s, entry.data);
  c.renderPrompt(s, { ...entry.data, prompt: { ...prompt, current: 2 } });
  assert.equal(reading(), true, 'the same question re-rendered by polling leaves the bars hidden');
  c.renderPrompt(s, { ...entry.data, prompt: { ...prompt, fingerprint: 'b'.repeat(64) } });
  assert.equal(reading(), true, 'a different question preserves the choice');
  c.setChromeVisible(false);
  c.renderPrompt(s, { ...entry.data, prompt: null, promptOpenable: false });
  c.renderPrompt(s, { ...entry.data, prompt: { ...prompt, fingerprint: 'b'.repeat(64) } });
  assert.equal(reading(), true, 'a returning question keeps focus mode');
  // A send that was refused: the result is in the composer, so the bars come back.
  entry.data = { capability: { supported: true }, phase: 'idle', route: c.sourceOf(s) };
  c.setChromeVisible(false);
  assert.equal(reading(), true);
  entry.text = '안녕하세요';
  await c.sendChat(entry);
  assert.equal(reading(), true, 'a send result stays near the composer');
  assert.equal(entry.text, '안녕하세요', 'the refused draft is kept');
  await settle();
  // A failing conversation read (connection problem) shows them once.
  c.setChromeVisible(false);
  entry.error = '';
  await c.loadChatState(s, false);
  assert.equal(reading(), true);
  c.setChromeVisible(false);
  await c.loadChatState(s, false);
  assert.equal(reading(), true, 'the same ongoing problem does not keep unfolding them');
  // The board's offline notice.
  c.context.navigator = { onLine: false };
  c.get('offline-text').textContent = '';
  c.renderConnectivity();
  assert.equal(c.get('offline-text').textContent, '오프라인 · 다시 연결 중');
  assert.equal(reading(), true, 'going offline keeps the tools folded');
});

test('a conversation opens in focus mode; on a touch screen it says once how to bring the bars back', () => {
  const writes = [];
  const c = client(undefined, noChatTimers);
  c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' });
  c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  const { s } = selectChat(c);
  c.drawDetail(s);
  assert.equal(c.state.chromeVisible, false, 'the bars are folded');
  assert.equal(c.get('focus-toggle').textContent, '집중 모드 해제');
  assert.equal(c.get('focus-toggle').attributes['aria-pressed'], 'true');
  assert.equal(c.get('tap-hint').hidden, false, 'a touch screen is told how to bring them back');
  assert.deepEqual(writes, [['sessionholic:ui:tapHint', 'true']]);
  c.toggleChrome();
  assert.equal(c.state.chromeVisible, true, 'one tap brings them back');
  // A mouse screen opens folded too; its button says how to leave, so no hint.
  const d = client(undefined, noChatTimers);
  d.context.localStorage = { getItem: () => null, setItem() { throw new Error('no write'); } };
  const { s: other } = selectChat(d);
  d.drawDetail(other);
  assert.equal(d.state.chromeVisible, false);
  assert.equal(d.get('tap-hint').hidden, true);
});

test('bars brought back fold on a deliberate scroll, and revisiting preserves focus mode and the one-time hint', () => {
  const writes = [];
  const { c, list, scrollTo, reading } = phoneConversation();
  c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  const hint = c.get('tap-hint');
  assert.equal(hint.hidden, true);
  scrollTo(2350);
  assert.equal(reading(), true);
  assert.equal(hint.hidden, false, 'the first hide shows the hint');
  assert.equal(hint.textContent, '화면을 탭하면 메뉴가 나타나요');
  assert.equal(hint.style.top, '92px', 'just below the folded header');
  assert.deepEqual(writes, [['sessionholic:ui:tapHint', 'true']], 'remembered as a UI preference only');
  c.toggleChrome();
  assert.equal(hint.hidden, true, 'a tap clears it');
  scrollTo(2300); scrollTo(2250);
  assert.equal(reading(), true);
  assert.equal(hint.hidden, true, 'shown once');
  assert.equal(writes.length, 1);
  // A board that saw it before (another page load) never shows it.
  const other = phoneConversation();
  other.c.context.localStorage = { getItem: (key) => (key === 'sessionholic:ui:tapHint' ? 'true' : null), setItem() { throw new Error('no write'); } };
  other.scrollTo(2350);
  assert.equal(other.reading(), true);
  assert.equal(other.c.get('tap-hint').hidden, true);
  // Leaving and entering the same conversation preserves the choice.
  c.setDetailShown(false);
  c.setDetailShown(true);
  assert.equal(c.state.chromeVisible, false);
  assert.equal(reading(), true);
  // Reduced motion: the bars switch without transitions; the gate folds with them.
  const css = styleSheet();
  assert.match(css, /\.jump-bottom, \.tap-hint, \.detail-head, \.conversation-bar,\s*\.detail-pane\.is-reading \.detail-head \{ transition: none; animation: none; \}/);
  assert.match(css, /\.detail-pane\.is-reading \.chat-terminal-gate/);
  // The jump button and the new-message badge stay usable while hidden.
  assert.doesNotMatch(css, /\.detail-pane\.is-reading \.jump-bottom/);
  assert.equal(list.unread || 0, 0);
});

test('a Claude conversation without a board terminal offers "터미널 열기" and never opens one by itself', async () => {
  const requests = [];
  const c = client(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    return { ok: true, status: 200, json: async () => ({ allowed: true, id: 'plan-1', mode: 'attach' }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c, { agent: 'claude', home: '' });
  c.drawDetail(s);
  await settle();
  requests.length = 0;
  entry.data = { route: c.sourceOf(s), phase: 'idle', capability: { supported: false, attachments: true,
    reason: '보내려면 먼저 터미널을 여세요.', action: 'open_terminal' } };
  entry.text = '먼저 써 둔 초안';
  c.updateChatComposer(entry);
  assert.equal(c.get('chat-terminal-gate').hidden, false);
  assert.equal(c.get('chat-terminal-gate-text').textContent, '보내려면 먼저 터미널을 여세요.');
  assert.equal(c.get('chat-open-terminal').textContent, '터미널 열기');
  assert.equal(c.get('chat-status').textContent, '', 'the gate says it; the status line stays for errors');
  assert.equal(c.get('chat-input').disabled, false, 'a draft can be written before opening the terminal');
  assert.equal(c.get('chat-send').disabled, true, 'but not sent');
  await c.sendChat(entry);
  assert.deepEqual(requests, [], 'nothing is sent and no terminal is opened in the background');
  c.get('chat-open-terminal').events.click();
  await settle();
  assert.equal(requests[0].url, '/api/plan', 'the button reuses the reviewed plan sheet');
  assert.deepEqual(requests[0].body.target, { host: 'workstation', agent: 'claude', account: 'claude:default' });
  // Returning to the same session's own terminal (attach) opens it right after the plan,
  // as "터미널·승인" always did: only because the user pressed the button.
  assert.deepEqual(requests.map((request) => request.url).slice(0, 2), ['/api/plan', '/api/launch']);
  // With the terminal open the composer sends as before.
  entry.data = { ...entry.data, capability: { supported: true, attachments: true, reason: null } };
  c.updateChatComposer(entry);
  assert.equal(c.get('chat-terminal-gate').hidden, true);
  assert.equal(c.get('chat-send').disabled, false);
  // An interactive session (no attach possible) gets no button.
  entry.data = { ...entry.data, capability: { supported: false, attachments: true, reason: '보드 밖 터미널' } };
  c.updateChatComposer(entry);
  assert.equal(c.get('chat-terminal-gate').hidden, true);
  assert.equal(c.get('chat-input').disabled, true);
  assert.equal(c.get('chat-status').textContent, '보드 밖 터미널');
});

test('a send refused because the terminal closed keeps the draft and offers the button', async () => {
  let reads = 0;
  let c;
  c = client(async (url, options) => {
    if (url === '/api/chat/send')
      return { ok: false, status: 409, json: async () => ({ error: '보내려면 먼저 터미널을 여세요.', dispatchState: 'not_started', code: 'terminal_required' }) };
    if (url === '/api/chat/state' && ++reads === 1) return { ok: false, status: 503, json: async () => ({ error: '연결 실패' }) };
    return { ok: true, status: 200, json: async () => ({ route: JSON.parse(options.body).source, phase: 'idle', messages: [],
      capability: { supported: false, attachments: true, reason: '보내려면 먼저 터미널을 여세요.', action: 'open_terminal' } }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c, { agent: 'claude', home: '' });
  c.drawDetail(s);
  await settle();
  reads = 0;
  entry.data = { route: c.sourceOf(s), phase: 'idle', capability: { supported: true, attachments: true } };
  entry.text = '보낼 내용';
  await c.sendChat(entry);
  await settle();
  assert.equal(entry.text, '보낼 내용', 'nothing was sent: the draft stays');
  assert.equal(entry.receipt.dispatchState, 'not_started');
  // Even when the next read fails, the refusal's code already offers the button.
  assert.equal(entry.data.capability.action, 'open_terminal');
  c.updateChatComposer(entry);
  assert.equal(c.get('chat-terminal-gate').hidden, false);
  // The next good read says the same, and the status line adds that nothing was sent.
  entry.error = '';
  await c.loadChatState(s, false);
  assert.equal(c.get('chat-terminal-gate').hidden, false);
  assert.equal(c.get('chat-status').textContent, '메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.');
});

test('snapshot events are read together after 300ms and never reload capabilities', async () => {
  const requests = [];
  const pending = [];
  let version = 7;
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot')
      ? { hosts: [{ name: 'workstation', ok: true, fetchedAt: 1000 + requests.length, data: {} }], version }
      : url === '/api/capabilities' ? { hosts: [], csrfToken: 'csrf-test' } : {}) };
  }, { setTimeout: (callback, ms) => { if (ms === 300) pending.push(callback); return pending.length; }, clearTimeout() {} });
  c.state.authenticated = true;
  c.state.snapshot = { hosts: [{ name: 'workstation', ok: true, fetchedAt: 1, data: {} }] };
  c.state.snapshotVersion = 6;
  c.state.capabilities = { hosts: [] };
  c.state.capabilitiesRevision = 'old';
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  for (const announced of [7, 8, 9]) c.snapshotAnnounced(announced);
  assert.equal(pending.length, 1, 'a burst arms one read');
  assert.deepEqual(requests, [], 'nothing is read before the batch window ends');
  version = 9;
  pending.shift()();
  await settle();
  assert.deepEqual(requests, ['/api/snapshot'], 'one snapshot read, no /api/capabilities even though the hosts changed');
  assert.equal(c.state.snapshotVersion, 9);
  c.snapshotAnnounced(9);
  assert.equal(pending.length, 0, 'a version already shown reads nothing');
  // An announcement during a read is batched again once that read ends.
  c.state.pollBusy = true;
  c.snapshotAnnounced(10);
  pending.shift()();
  assert.equal(c.state.snapshotPending, true);
  c.state.pollBusy = false;
  requests.length = 0;
  await c.poll(false, { capabilities: false });
  assert.deepEqual(requests, ['/api/snapshot']);
  assert.equal(pending.length, 1, 'still behind (version 9 < 10): one more batched read');
  version = 10;
  pending.shift()();
  await settle();
  assert.equal(c.state.snapshotVersion, 10);
  assert.equal(requests.filter((url) => url === '/api/capabilities').length, 0);
  // A regular read still catches capabilities up.
  requests.length = 0;
  await c.poll(false);
  assert.deepEqual(requests.slice(0, 2), ['/api/snapshot', '/api/capabilities']);
});

test('the 60s safety poll reads /api/capabilities only when a host recovers or its accounts change, never for a new fetchedAt', async () => {
  const requests = [];
  const hosts = [{ name: 'workstation', ok: true, fetchedAt: 1, data: { codex: [{ id: 'a', home: '.codex' }] } }];
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot')
      ? { hosts: JSON.parse(JSON.stringify(hosts)) } : url === '/api/capabilities' ? { hosts: [], csrfToken: 'csrf-test' } : {}) };
  }, noChatTimers);
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  const reads = async () => { requests.length = 0; await c.poll(false); return requests.filter((url) => url === '/api/capabilities').length; };
  assert.equal(await reads(), 1, 'the first read loads capabilities');
  hosts[0].fetchedAt = 2;
  assert.equal(await reads(), 0, 'a watch snapshot (new fetchedAt) is not an account change');
  hosts[0].ok = false;
  assert.equal(await reads(), 1);
  hosts[0].ok = true; hosts[0].fetchedAt = 3;
  assert.equal(await reads(), 1, 'a host recovering from a failure is read again');
  hosts[0].data.codex.push({ id: 'b', home: '.codex-work' });
  assert.equal(await reads(), 1, 'a new Codex account home is read again');
  hosts[0].data.codex.push({ id: 'c', home: '.codex-work' });
  assert.equal(await reads(), 0);
});

test('consecutive tool records fold into one "도구 실행 N개" line that opens to one line per record and once more to the full text', async () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  const turn = [{ role: 'user', text: '고쳐 주세요', ts: 1000 },
    { role: 'tool', text: '$ npm test', toolKind: 'command' }, { role: 'tool', text: '파일 수정 · a.py', toolKind: 'edit' },
    { role: 'tool', text: '$ git status\n두 번째 줄', toolKind: 'command' }];
  c.renderMessages(s, turn);
  assert.equal(list.children.length, 2, 'three records are one line');
  const group = list.children[1];
  assert.match(group.className, /msg tool tool-group/);
  assert.equal(group.toolTitle.textContent, '도구 실행 3개');
  assert.equal(group.toolKinds.textContent, '· 명령 2 · 파일 수정 1');
  assert.equal(group.toolToggle.tagName, 'BUTTON', 'a control, so a tap on it never toggles the bars');
  assert.equal(group.toolToggle.attributes['aria-expanded'], 'false'); assert.equal(group.toolList.hidden, true);
  group.toolToggle.events.click();
  assert.equal(group.toolToggle.attributes['aria-expanded'], 'true'); assert.equal(group.toolList.hidden, false);
  assert.deepEqual(group.toolList.children.map((item) => textOf(item).trim()), ['$ npm test', '파일 수정 · a.py', '$ git status\n두 번째 줄']);
  const record = group.toolList.children[2];
  assert.equal(record.attributes['aria-expanded'], 'false');
  record.events.click();
  assert.match(record.className, /is-full/); assert.equal(record.attributes['aria-expanded'], 'true');
  // The running turn adds a record: the same group counts it, still open, the record still full.
  list.scrollHeight = 900; list.clientHeight = 200; list.scrollTop = 100;
  const more = [...turn, { role: 'tool', text: 'Grep · TODO' }];
  c.renderMessages(s, more);
  assert.equal(list.children[1], group); assert.equal(group.toolTitle.textContent, '도구 실행 4개');
  assert.equal(group.toolKinds.textContent, '· 명령 2 · 파일 수정 1 · 검색 1');
  assert.equal(group.open, true); assert.equal(group.toolList.children[2], record); assert.match(record.className, /is-full/);
  assert.equal(list.unread || 0, 0, 'a new tool record raises no "새 메시지" badge');
  // An answer ends the group; tools after it start another one.
  c.renderMessages(s, [...more, { role: 'assistant', text: '테스트를 고쳤습니다.', ts: 1500 }, { role: 'tool', text: '$ ls' }]);
  assert.equal(list.children.length, 4); assert.equal(list.children[1], group);
  assert.notEqual(list.children[3], group); assert.equal(list.children[3].toolTitle.textContent, '도구 실행 1개');
  assert.equal(list.children[3].open, false); assert.equal(list.unread, 1, 'only the answer counts');
  // The long-press menu copies the whole group.
  let copied; c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  const labels = menuLabels(c, group);
  assert.equal(labels.includes('텍스트 선택'), false);
  await menuAction(c, group, '도구 기록 4개 모두 복사');
  assert.equal(copied, '$ npm test\n파일 수정 · a.py\n$ git status\n두 번째 줄\nGrep · TODO');
  assert.match(styleSheet(), /\.tool-item\.is-full \.tool-item-text \{[^}]*white-space: pre-wrap;/);
});

test('a tool record without toolKind is classified from the collector text', () => {
  const c = client(undefined, noChatTimers);
  const kinds = ['$ npm test', '파일 수정 · a.py', '웹 검색 · 날씨', 'Bash · 목록', 'Edit · a.py', 'Write · b', 'Read · c',
    'Glob · *.js', 'WebFetch · https://e.com', 'mcp__slack__send', 'Task · 조사', 'slack send', 'TodoWrite']
    .map((text) => c.toolKindOf({ role: 'tool', text }));
  assert.deepEqual(kinds, ['command', 'edit', 'search', 'command', 'edit', 'edit', 'read', 'search', 'search', 'mcp',
    'agent', 'other', 'other']);
  assert.equal(c.toolKindOf({ text: '$ ls', toolKind: 'mcp' }), 'mcp', 'the collector kind wins');
  assert.equal(c.toolKindOf({ text: '$ ls', toolKind: 'nonsense' }), 'command');
});

test('B2: reading back, the row on screen keeps its place when rows above it leave the 80-row window', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  const rows = (from, to) => Array.from({ length: to - from }, (_, i) =>
    ({ role: 'assistant', text: `답변 ${from + i}`, ts: 1000 + (from + i) * 600 }));
  c.renderMessages(s, rows(0, 10));
  const layout = (node) => Object.defineProperties(node, {
    offsetTop: { configurable: true, get: () => list.children.indexOf(node) * 100 },
    offsetHeight: { configurable: true, get: () => 100 } });
  list.children.forEach(layout);
  Object.assign(list, { scrollHeight: 1000, clientHeight: 300, scrollTop: 450 });
  const reading = list.children[4];
  // Two rows leave at the top and two arrive below while the reader is at "답변 4".
  c.renderMessages(s, rows(2, 12));
  assert.equal(list.children[2], reading);
  assert.equal(list.scrollTop, 250, 'the row the reader was on stays 50px above the top edge');
  assert.equal(list.unread, 2);
  // Without layout (or once the row itself left), the old position is kept.
  const plain = client(undefined, noChatTimers); const other = selectChat(plain);
  plain.renderMessages(other.s, rows(0, 3));
  Object.assign(plain.get('messages'), { scrollHeight: 900, clientHeight: 200, scrollTop: 45 });
  plain.renderMessages(other.s, rows(1, 4));
  assert.equal(plain.get('messages').scrollTop, 45);
});

test('B4: the board\'s own attachment path lines become file chips and "[이미지 첨부]" alone is not shown', async () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const codexLine = '첨부 파일: "/w/project/.sessionholic-attachments/0a1b/보고서.pdf"';
  const claudeLine = '첨부 파일: /w/project/.sessionholic-attachments/0c2d/화면.png';
  c.renderMessages(s, [
    { role: 'user', text: '검토해 주세요\n' + codexLine + '\n' + claudeLine, ts: 1 },
    { role: 'user', text: '[이미지 첨부]', ts: 2, attachments: [{ id: null, name: '이미지', kind: 'image', source: 'native' }] },
    { role: 'user', text: '첨부 파일: 메모로 남긴 줄', ts: 3 },
    { role: 'user', text: codexLine, ts: 4, attachments: [{ id: 'f', name: '보고서.pdf', kind: 'file', source: 'sessionholic' }] }]);
  const rows = c.get('messages').children;
  assert.doesNotMatch(textOf(rows[0]), /sessionholic-attachments|첨부 파일:/);
  assert.match(textOf(rows[0]), /검토해 주세요/);
  assert.deepEqual(byClass(rows[0], 'attachment-name').map((node) => node.textContent), ['보고서.pdf', '화면.png']);
  assert.match(byClass(rows[0], 'msg-attachment')[1].className, /is-image/);
  assert.doesNotMatch(textOf(rows[1]), /\[이미지 첨부\]/);
  assert.equal(byClass(rows[1], 'msg-attachment').length, 1);
  assert.match(textOf(rows[2]), /첨부 파일: 메모로 남긴 줄/, 'a typed line is not an attachment');
  assert.deepEqual(byClass(rows[3], 'attachment-name').map((node) => node.textContent), ['보고서.pdf'], 'one chip per file');
  assert.equal(byClass(rows[3], 'msg-body').length, 0);
  let copied; c.context.navigator = { clipboard: { writeText: async (text) => { copied = text; } } };
  await menuAction(c, rows[0], '복사'); assert.equal(copied, '검토해 주세요');
  assert.equal(menuLabels(c, rows[3]).includes('복사'), false);
  // A transcript image chip without a name takes the name from its path line.
  assert.deepEqual(c.userMessageParts({ text: claudeLine, attachments: [{ name: '이미지', kind: 'image', source: 'native' }] })
    .attachments.map((file) => file.name), ['화면.png']);
});

test('B5: a timed final answer after untimed progress notes shows its own name and time', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'user', text: '질문', ts: 1000 }, { role: 'assistant', text: '살펴볼게요', ts: null },
    { role: 'assistant', text: '조금 더', ts: null }, { role: 'assistant', text: '최종 답', ts: 2500 }]);
  const rows = c.get('messages').children;
  assert.deepEqual(rows.map((row) => row.msgLabel?.hidden ?? null), [false, false, true, false]);
  assert.equal(rows[1].msgLabel.children.length, 1, 'no time on a progress note');
  assert.equal(rows[3].msgLabel.children.length, 2, 'the final answer shows when the turn completed');
});

test('B8: Claude summary and interruption notes render as system lines, not "나" bubbles', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c, { agent: 'claude', home: '', id: 'claude-row' });
  c.renderMessages(s, [{ role: 'system', text: '이전 대화 요약으로 이어짐', ts: 1 }, { role: 'system', text: '사용자가 중단함', ts: 2 }]);
  const rows = c.get('messages').children;
  assert.deepEqual(rows.map((row) => row.className), ['msg system', 'msg system']);
  assert.equal(rows.some((row) => row.msgLabel), false);
});

test('the working row counts "작업 중 · 12분 34초" from workingSince once a second, and stops while the page is hidden', () => {
  const armed = [];
  const c = client(undefined, { setTimeout: (callback, ms) => { armed.push({ callback, ms }); return armed.length; },
    clearTimeout: (id) => { if (armed[id - 1]) armed[id - 1].cleared = true; } });
  const clock = armed.filter.bind(armed);
  const ticking = () => clock((timer) => timer.callback === c.context.tickWorking && !timer.cleared);
  // The page's own visibility handler (registered at start; offline, so nothing is fetched).
  c.context.navigator = { onLine: false }; c.windowEvents.DOMContentLoaded();
  const { s, entry } = selectChat(c);
  const now = Date.now();
  entry.data.phase = 'working';
  entry.data.workingSince = Math.floor(now / 1000) - 754;
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  const list = c.get('messages'); const row = list.children.at(-1);
  assert.match(row.className, /msg-working/);
  assert.match(row.workingTime.textContent, /^ · 12분 3[45]초$/);
  assert.match(textOf(row), /작업 중/);
  assert.equal(row.workingTime.attributes['aria-live'], 'off', 'the second count is not announced');
  assert.equal(ticking().length, 1); assert.ok(ticking()[0].ms > 0 && ticking()[0].ms <= 1020);
  // Repeated renders keep one timer.
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  assert.equal(ticking().length, 1);
  c.context.document.hidden = true; c.documentEvents.visibilitychange();
  assert.equal(ticking().length, 0, 'no ticking while hidden');
  c.context.document.hidden = false; c.tickWorking();
  assert.equal(ticking().length, 1);
  assert.deepEqual([0, 59, 60, 754, 3599, 3600, 3900, 7200].map(c.formatElapsed),
    ['0초', '59초', '1분 0초', '12분 34초', '59분 59초', '1시간', '1시간 5분', '2시간']);
  // An approximate start (Claude) and no workingSince both leave the label alone.
  entry.data.workingSinceApprox = true; c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  assert.equal(row.workingTime.textContent, ''); assert.equal(ticking().length, 0);
  assert.match(textOf(row), /작업 중/);
  entry.data.workingSinceApprox = false; c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  assert.match(row.workingTime.textContent, /^ · 12분 3[4-6]초$/); assert.equal(ticking().length, 1);
  entry.data.workingSince = null; c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  assert.equal(row.workingTime.textContent, ''); assert.equal(ticking().length, 0);
  entry.data.phase = 'idle'; entry.data.workingSince = Math.floor(now / 1000) - 10;
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }, { role: 'assistant', text: '끝' }]);
  assert.equal(list.children.includes(row), false); assert.equal(ticking().length, 0);
  assert.match(styleSheet(), /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.working-dots i \{\s*animation: none;/);
});

test('B10: a Claude row listed without an id gets its own key, one notice and no request that can only fail', async () => {
  const requests = [];
  const c = client(async (url) => { requests.push(url); return { ok: true, status: 200, json: async () => ({ messages: [] }) }; }, noChatTimers);
  const { FakeEventSource, sources } = fakeEventSource();
  c.context.EventSource = FakeEventSource;
  const rows = [{ agent: 'claude', id: '', sessionId: '', pid: 11, cwd: '/w', phase: 'working', title: 'A', kind: 'interactive' },
    { agent: 'claude', id: '', sessionId: '', pid: 12, cwd: '/w', phase: 'idle', title: 'B', kind: 'interactive' }];
  c.state.snapshot = { hosts: [{ name: 'workstation', label: 'Workstation', ok: true, data: { claude: rows } }] };
  c.state.authenticated = true;
  const sessions = c.allSessions();
  assert.equal(new Set(sessions.map((session) => session.key)).size, 2, 'two rows without ids never share a key');
  const s = sessions[0];
  c.state.selected = s.key;
  c.get('chat-composer').dataset.source = c.chatKey(c.sourceOf(s));
  await c.loadDetail(s, true);
  await c.loadChatState(s, true);
  c.syncEvents();
  assert.deepEqual(requests, [], 'neither /api/chat/state nor /api/read');
  assert.deepEqual(sources.map((source) => source.url), ['/api/events'], 'the list stream only, no conversation subscription');
  const list = c.get('messages');
  assert.match(textOf(list), /대화 기록 ID를 알 수 없어 여기서 읽을 수 없습니다/);
  assert.doesNotMatch(textOf(list), /저장된 대화를 아직|올바르지 않습니다/);
  assert.match(list.children.at(-1).className, /msg-working/, 'the listed phase still shows it works');
  assert.match(c.get('chat-status').textContent, /세션 ID가 없어 여기서 보낼 수 없습니다/);
  // With a session id the transcript is read directly, still without chat state.
  const named = { ...s, sessionId: 'session-1234', key: 'workstation|claude||~named' };
  rows.push({ ...rows[0], sessionId: 'session-1234', pid: 13 });
  const withSession = c.allSessions().find((session) => session.sessionId === 'session-1234');
  c.state.selected = withSession.key;
  await c.loadDetail(withSession, true);
  assert.deepEqual(requests.map((url) => url.split('?')[0]), ['/api/read']);
  assert.match(requests[0], /id=session-1234/);
  assert.ok(named);
});

test('a long press on a tool group line (a button) opens the group menu and its tap then does not toggle the group', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; } });
  const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'tool', text: '$ ls', toolKind: 'command' }, { role: 'tool', text: 'Read · a.py' }]);
  const list = c.get('messages');
  const group = list.children[0];
  vm.runInContext('bindMessageGestures', c.context)(list);
  // The toggle matches a bare "button" but not the long-press skip list.
  const skip = vm.runInContext('PRESS_MENU_SKIP', c.context);
  assert.match(skip, /button:not\(\.card\):not\(\.tool-group-toggle\):not\(\.tool-item\)/);
  const onToggle = { closest: (selector) => (selector === '.msg' ? group : selector === skip ? null : null) };
  list.events.pointerdown({ pointerId: 1, pointerType: 'touch', clientX: 50, clientY: 50, target: onToggle });
  timers.filter((timer) => timer.delay === c.GESTURE.longPressMs && !timer.cleared).at(-1).callback();
  assert.equal(c.get('action-sheet').open, true);
  assert.equal(c.get('action-list').children[0].textContent, '도구 기록 2개 모두 복사');
  list.events.pointerup({ pointerId: 1 });
  let stopped = 0;
  list.events.click({ preventDefault() { stopped++; }, stopPropagation() { stopped++; } });
  assert.equal(stopped, 2, 'the click the long press leaves behind is swallowed');
  // Any other button (code copy, message menu) still keeps its own tap only.
  const onCopy = { closest: (selector) => (selector === '.msg' ? group : selector === skip ? {} : null) };
  c.closeActionSheet();
  const before = timers.length;
  list.events.pointerdown({ pointerId: 2, pointerType: 'touch', clientX: 50, clientY: 50, target: onCopy });
  assert.equal(timers.slice(before).some((timer) => timer.delay === c.GESTURE.longPressMs), false);
});

test('clearing the cache removes only this board\'s shells and keeps the worker of a device that gets notifications', async () => {
  const c = client(undefined, noChatTimers);
  const deleted = [], actions = [];
  let reloaded = 0;
  c.context.caches = { keys: async () => ['sessionholic-shell-1a2b3c4d', 'another-app-shell', 'xsessionholic-shell', 'sessionholic-shell-v13'],
    delete: async (key) => { deleted.push(key); return true; } };
  const worker = (subscription, name) => ({ pushManager: { getSubscription: async () => subscription },
    update: async () => actions.push('update ' + name), unregister: async () => actions.push('unregister ' + name) });
  c.context.navigator = { serviceWorker: { getRegistrations: async () => [worker({ endpoint: 'x' }, 'notified'), worker(null, 'quiet')] } };
  c.context.window.location = { reload: () => { reloaded += 1; } };
  c.context.localStorage = { getItem: () => null, setItem() { throw new Error('no preference changes'); }, removeItem() { throw new Error('no preference changes'); } };
  await c.hardReload();
  assert.deepEqual(deleted, ['sessionholic-shell-1a2b3c4d', 'sessionholic-shell-v13'], "only this board's caches");
  assert.deepEqual(actions, ['update notified', 'unregister quiet'], 'a notified device keeps its subscription');
  assert.equal(reloaded, 1);
  // Without caches or a worker (an old browser) it still reloads.
  const d = client(undefined, noChatTimers);
  let again = 0;
  d.context.window.location = { reload: () => { again += 1; } };
  await d.hardReload();
  assert.equal(again, 1);
  // Offline, nothing is cleared: a shell without its cache could not come back.
  const e = client(undefined, noChatTimers);
  let cleared = 0;
  e.context.caches = { keys: async () => { cleared += 1; return []; }, delete: async () => true };
  e.context.window.location = { reload: () => { cleared += 1; } };
  e.state.offline = true;
  await e.hardReload();
  assert.equal(cleared, 0);
  const html = indexPage();
  assert.match(html, /<button id="hard-reload" class="push-button" type="button" aria-describedby="reload-help">캐시 지우고 새로고침<\/button>/);
});

test('the installation meta names preferences, titles, history and attachment folders', () => {
  const names = JSON.stringify({ title: '데모 보드', shortName: '데모', storagePrefix: 'db:', historyKey: 'demoBoard',
    attachmentsDir: '.demo-board-attachments' });
  const reads = []; const removed = []; const writes = [];
  const c = client(undefined, noChatTimers, { instanceMeta: names });
  c.context.navigator = { onLine: false };
  c.context.localStorage = {
    getItem: (key) => { reads.push(key); return null; },
    removeItem: (key) => removed.push(key),
    setItem: (key, value) => writes.push([key, value]),
  };
  vm.runInContext('prefs.get("fontSize", 14)', c.context);
  c.windowEvents.DOMContentLoaded();
  c.get('terminal-accessibility').events.change({ target: { checked: true } });
  assert.deepEqual(reads, ['db:ui:fontSize']);
  assert.deepEqual(removed, ['db:selected', 'db:filters', 'db:showOld'], 'legacy cleanup follows the prefix');
  assert.deepEqual(writes, [['db:ui:screenReaderMode', 'true']]);
  c.renderAttention([{ phase: 'needs_input' }, { phase: 'working' }]);
  assert.equal(c.context.document.title, '(1) 답 필요 · 데모');
  c.renderAttention([]);
  assert.equal(c.context.document.title, '데모 보드');
  c.navigateView('detail');
  assert.deepEqual(Object.keys(c.context.window.history.state).sort(), ['demoBoard', 'view']);
  c.backToList();
  assert.equal(c.context.window.history.backCount, 1);
  c.context.window.history.state = { sessionholic: true, view: 'detail' };
  c.windowEvents.popstate({ state: { sessionholic: true, view: 'detail' } });
  c.backToList();
  assert.equal(c.context.window.history.backCount, 1, "another installation's history entry is not ours");
  const own = c.userMessageParts({ text: '첨부 파일: /w/p/.demo-board-attachments/0a/화면.png', attachments: [] });
  assert.deepEqual(own.attachments.map((file) => [file.name, file.source]), [['화면.png', 'sessionholic']]);
  const other = c.userMessageParts({ text: '첨부 파일: /w/p/.sessionholic-attachments/0a/화면.png', attachments: [] });
  assert.deepEqual(other.attachments, [], 'another folder name stays text');
});

test('a missing, broken or invalid installation meta keeps the public names', () => {
  const cases = [undefined, '{not json', '[]', 'null',
    JSON.stringify({ storagePrefix: 'has space', historyKey: '1bad', title: '', attachmentsDir: '../x', shortName: 7 })];
  for (const instanceMeta of cases) {
    const writes = [];
    const c = client(undefined, noChatTimers, { instanceMeta });
    c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
    vm.runInContext('prefs.set("fontSize", 15)', c.context);
    assert.deepEqual(writes.at(-1), ['sessionholic:ui:fontSize', '15'], String(instanceMeta));
    c.renderAttention([{ phase: 'needs_input' }]);
    assert.equal(c.context.document.title, '(1) 답 필요 · 세션홀릭');
    c.renderAttention([]);
    assert.equal(c.context.document.title, '세션홀릭 - 에이전트 세션 매니저');
    c.navigateView('detail');
    assert.deepEqual(Object.keys(c.context.window.history.state).sort(), ['sessionholic', 'view']);
  }
  // One valid key is used even when another is not.
  const c = client(undefined, noChatTimers, { instanceMeta: JSON.stringify({ storagePrefix: 'db:', historyKey: '1bad' }) });
  const writes = [];
  c.context.localStorage = { getItem: () => null, setItem: (key, value) => writes.push([key, value]) };
  vm.runInContext('prefs.set("fontSize", 15)', c.context);
  assert.deepEqual(writes, [['db:ui:fontSize', '15']]);
  c.navigateView('detail');
  assert.deepEqual(Object.keys(c.context.window.history.state).sort(), ['sessionholic', 'view']);
});

test('theme-init reads the preference prefix from the installation meta above it', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/theme-init.js'), 'utf8');
  const run = (content, stored) => {
    const attributes = {};
    const document = { documentElement: { setAttribute(name, value) { attributes[name] = String(value); } },
      querySelectorAll: () => [],
      querySelector: (selector) => (selector === 'meta[name="sessionholic-instance"]' && content !== undefined
        ? { getAttribute: (name) => (name === 'content' ? content : null) } : null) };
    const localStorage = { getItem: (key) => stored[key] ?? null };
    vm.runInNewContext(source, { window: { localStorage }, document, JSON });
    return attributes;
  };
  const stored = { 'db:ui:theme': '"dark"', 'db:ui:textSize': '"large"', 'sessionholic:ui:theme': '"light"' };
  assert.deepEqual(run(JSON.stringify({ storagePrefix: 'db:' }), stored), { 'data-theme': 'dark', 'data-text-size': 'large' });
  for (const content of [undefined, '{broken', JSON.stringify({ storagePrefix: 'bad prefix' }), '{}']) {
    assert.deepEqual(run(content, stored), { 'data-theme': 'light' }, String(content));
  }
  const html = indexPage();
  assert.ok(html.indexOf('name="sessionholic-instance"') > 0);
  assert.ok(html.indexOf('name="sessionholic-instance"') < html.indexOf('<script src="/theme-init.js"></script>'),
    'the meta is parsed before theme-init runs');
});

test('H3: a restarted server (another bootId) has its CSRF token read again, even on a read an event announced', async () => {
  const requests = [];
  let boot = 'boot-a'; let version = 5; let csrf = 'csrf-a';
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot')
      ? { hosts: [{ name: 'workstation', ok: true, fetchedAt: 1, data: {} }], version, bootId: boot }
      : url === '/api/capabilities' ? { hosts: [], csrfToken: csrf } : {}) };
  }, noChatTimers);
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  await c.poll(false);
  assert.equal(c.state.csrf, 'csrf-a');
  assert.equal(requests.filter((url) => url === '/api/capabilities').length, 1);
  // The same process: an announced read and a regular read read only the snapshot.
  requests.length = 0; version = 6;
  await c.poll(false, { capabilities: false });
  await c.poll(false);
  assert.deepEqual(requests, ['/api/snapshot', '/api/snapshot']);
  // The server restarts: its versions count from 0 again and its token is new.
  boot = 'boot-b'; version = 0; csrf = 'csrf-b';
  requests.length = 0;
  await c.poll(false, { capabilities: false });
  assert.deepEqual(requests, ['/api/snapshot', '/api/capabilities']);
  assert.equal(c.state.csrf, 'csrf-b');
  assert.equal(c.state.snapshotVersion, 0, 'the new process\'s first version is taken');
  requests.length = 0; version = 1;
  await c.poll(false);
  assert.deepEqual(requests, ['/api/snapshot'], 'read once per restart');
});

test('H3: after a restart the reconnected stream\'s hello (another bootId) reads the new versions and token at once', async () => {
  const requests = [];
  let boot = 'boot-a'; let version = 40; let csrf = 'csrf-a';
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url.startsWith('/api/snapshot')
      ? { hosts: [{ name: 'workstation', ok: true, fetchedAt: 1, data: {} }], version, bootId: boot }
      : url === '/api/capabilities' ? { hosts: [], csrfToken: csrf } : { requests: [], sources: [] }) };
  }, { setTimeout: (callback, ms) => { if (ms === 300) setImmediate(callback); return 1; }, clearTimeout() {} });
  const { FakeEventSource, sources } = fakeEventSource();
  c.context.EventSource = FakeEventSource;
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  await c.poll(false);
  assert.equal(sources.length, 1);
  sources[0].emit('hello', { snapshotVersion: 40, bootId: 'boot-a' });
  await settle();
  assert.deepEqual(requests, ['/api/snapshot', '/api/bot-requests', '/api/capabilities'], 'initial bot metadata is read once; the same process adds no reads');
  // The server restarts with version 2; the browser reconnects the same stream.
  boot = 'boot-b'; version = 2; csrf = 'csrf-b';
  requests.length = 0;
  sources[0].emit('hello', { snapshotVersion: 2, bootId: 'boot-b' });
  await settle();
  assert.deepEqual(requests, ['/api/snapshot', '/api/capabilities']);
  assert.equal(c.state.csrf, 'csrf-b');
  assert.equal(c.state.snapshotVersion, 2);
  // Later versions of the new process are read (below the old process's 40).
  requests.length = 0; version = 3;
  sources[0].emit('snapshot', { version: 3 });
  await settle();
  assert.deepEqual(requests, ['/api/snapshot']);
  assert.equal(c.state.snapshotVersion, 3);
});

test('H3: a stale CSRF token is refreshed once and a read or a deduplicated request sent again; never a chat send', async () => {
  const refused = { ok: false, status: 403, json: async () => ({ error: '보안 연결을 갱신한 뒤 다시 시도해 주세요.',
    code: 'csrf_expired' }) };
  const setup = ({ fresh = true, capabilities = 'ok' } = {}) => {
    const requests = [];
    const c = client(async (url, options) => {
      requests.push([url, options?.headers?.['X-CSRF-Token']]);
      if (url === '/api/capabilities') {
        if (capabilities === 'network') throw new Error('network unavailable');
        return { ok: true, status: 200, json: async () => ({ hosts: [], csrfToken: 'fresh-csrf' }) };
      }
      if (!fresh || options.headers['X-CSRF-Token'] !== 'fresh-csrf') return refused;
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }, noChatTimers);
    c.state.authenticated = true;
    return { c, requests };
  };
  const retried = [['/api/chat/state', { source: {} }], ['/api/chat/prompt', { requestId: 'request-0001' }],
    ['/api/plan', { source: {} }], ['/api/launch', { planId: 'plan', requestId: 'request-0002' }], ['/api/refresh', {}],
    ['/api/usage/refresh', {}]];
  for (const [path, body] of retried) {
    const { c, requests } = setup();
    assert.deepEqual(await c.api(path, { method: 'POST', body }), { ok: true }, path);
    assert.deepEqual(requests, [[path, 'csrf-test'], ['/api/capabilities', undefined], [path, 'fresh-csrf']], path);
    assert.equal(c.state.csrf, 'fresh-csrf');
  }
  // Only once: a second refusal is the caller's error.
  {
    const { c, requests } = setup({ fresh: false });
    await assert.rejects(c.api('/api/chat/state', { method: 'POST', body: {} }), (err) => err.code === 'csrf_expired');
    assert.deepEqual(requests.map(([url]) => url), ['/api/chat/state', '/api/capabilities', '/api/chat/state']);
  }
  // A failed refresh reports the refusal, without another attempt.
  {
    const { c, requests } = setup({ capabilities: 'network' });
    await assert.rejects(c.api('/api/refresh', { method: 'POST', body: {} }), (err) => err.code === 'csrf_expired');
    assert.deepEqual(requests.map(([url]) => url), ['/api/refresh', '/api/capabilities']);
  }
  // A chat send and a terminal input are never sent again, nor is the token read here.
  for (const path of ['/api/chat/send', `/api/terminal/${'a'.repeat(32)}/input`]) {
    const { c, requests } = setup();
    await assert.rejects(c.api(path, { method: 'POST', body: { requestId: 'request-0003' } }),
      (err) => err.code === 'csrf_expired', path);
    assert.deepEqual(requests.map(([url]) => url), [path], path);
  }
});

test('M5: an opened tool group stays open when the 80-row window moves past its first record', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  const tool = (n) => ({ role: 'tool', text: `$ step ${n}`, toolKind: 'command' });
  c.renderMessages(s, [{ role: 'user', text: '시작', ts: 1000 }, tool(1), tool(2), tool(3)]);
  const group = list.children[1];
  group.toolToggle.events.click();
  assert.equal(group.open, true);
  // The question and the group's first two records leave the window; a record arrives.
  c.renderMessages(s, [tool(3), tool(4)]);
  assert.deepEqual(list.children, [group], 'the same group carries on');
  assert.equal(group.open, true); assert.equal(group.toolList.hidden, false);
  assert.equal(group.toolTitle.textContent, '도구 실행 2개');
  c.renderMessages(s, [tool(4), { role: 'assistant', text: '끝', ts: 2000 }]);
  assert.equal(list.children[0], group); assert.equal(group.open, true);
  // A group never opened stays closed when its first record leaves.
  const d = client(undefined, noChatTimers); const other = selectChat(d);
  d.renderMessages(other.s, [tool(1), tool(2)]);
  const closed = d.get('messages').children[0];
  d.renderMessages(other.s, [tool(2)]);
  assert.equal(d.get('messages').children[0], closed); assert.equal(closed.open, false);
  // A group split in two by a new row is drawn once; the second part opens like it.
  const e = client(undefined, noChatTimers); const third = selectChat(e);
  e.renderMessages(third.s, [tool(1), tool(2), tool(3)]);
  const whole = e.get('messages').children[0];
  whole.toolToggle.events.click();
  e.renderMessages(third.s, [tool(1), { role: 'assistant', text: '중간 답', ts: 3000 }, tool(3)]);
  const rows = e.get('messages').children;
  assert.equal(rows.length, 3); assert.equal(rows[0], whole); assert.notEqual(rows[2], whole);
  assert.equal(rows[2].open, true); assert.equal(rows[2].toolTitle.textContent, '도구 실행 1개');
});

test('reading back, the row kept in place is measured in the list\'s own coordinates, not by an ancestor\'s offsetTop', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  const rows = (from, to) => Array.from({ length: to - from }, (_, i) =>
    ({ role: 'assistant', text: `답변 ${from + i}`, ts: 1000 + (from + i) * 600 }));
  c.renderMessages(s, rows(0, 10));
  // Like a browser: .messages is 200px below the page top and not positioned, so a
  // row's offsetTop counts from the page and moves with the list's scroll; removing
  // rows above the one on screen lowers scrollTop by itself (scroll anchoring).
  const LIST_TOP = 200;
  const top = (node) => list.children.indexOf(node) * 100;
  const screen = (node) => LIST_TOP + top(node) - list.scrollTop;
  const layout = (node) => Object.defineProperties(node, {
    offsetTop: { configurable: true, get: () => screen(node) },
    offsetHeight: { configurable: true, get: () => 100 },
    getBoundingClientRect: { configurable: true, value: () => ({ top: screen(node), height: 100 }) } });
  list.children.forEach(layout);
  list.getBoundingClientRect = () => ({ top: LIST_TOP, height: 300 });
  const remove = list.removeChild.bind(list);
  list.removeChild = (child) => {
    const above = top(child) + 100 <= list.scrollTop;
    remove(child);
    if (above) list.scrollTop -= 100;
    return child;
  };
  Object.assign(list, { scrollHeight: 1000, clientHeight: 300, scrollTop: 450 });
  const reading = list.children[4];
  // Two rows leave at the top and two arrive below while the reader is at "답변 4".
  c.renderMessages(s, rows(2, 12));
  assert.equal(list.children[2], reading);
  assert.equal(list.scrollTop, 250, 'the row the reader was on stays 50px above the top edge');
});

test('the working time does not tick on a phone\'s list or behind a terminal, and counts again when the conversation shows', () => {
  const armed = [];
  const c = client(undefined, { setTimeout: (callback, ms) => { armed.push({ callback, ms }); return armed.length; },
    clearTimeout: (id) => { if (armed[id - 1]) armed[id - 1].cleared = true; } });
  const ticking = () => armed.filter((timer) => timer.callback === c.context.tickWorking && !timer.cleared);
  c.context.window.matchMedia = () => ({ matches: true });
  const { s, entry } = selectChat(c);
  c.state.detailShown = true;
  entry.data.phase = 'working';
  entry.data.workingSince = Math.floor(Date.now() / 1000) - 30;
  c.renderMessages(s, [{ role: 'user', text: '시작해 주세요' }]);
  assert.equal(ticking().length, 1);
  c.setDetailShown(false);
  assert.equal(ticking().length, 0, 'the list is on screen');
  c.tickWorking();
  assert.equal(ticking().length, 0);
  c.setDetailShown(true);
  assert.equal(ticking().length, 1, 'the conversation shows again');
  c.state.terminalVisible = true;
  ticking()[0].callback();
  assert.equal(ticking().length, 0, 'a terminal covers the conversation');
});

// --- Tablets (wide touch screens) and hardware keyboards ---

test('a menu item runs only for a press that started on it after the menu opened, so a long press lifting over it runs nothing', () => {
  const timers = [];
  const c = client(undefined, { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cleared = true; } });
  c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' }); // a tablet: the centered menu
  const { s } = selectChat(c);
  c.renderMessages(s, [{ role: 'assistant', text: '길게 눌러 볼 답변' }]);
  const list = c.get('messages');
  const row = list.children[0];
  vm.runInContext('bindMessageGestures', c.context)(list);
  const dialog = c.get('action-sheet');
  const target = { closest: (selector) => (selector === '.msg' ? row : null) };
  const longPress = (id) => {
    list.events.pointerdown({ pointerId: id, pointerType: 'touch', isPrimary: true, clientX: 300, clientY: 600, target });
    timers.filter((timer) => timer.delay === c.GESTURE.longPressMs && !timer.cleared).at(-1).callback();
    assert.equal(dialog.open, true, 'the long press opened the menu');
    return c.get('action-list').children.find((item) => item.textContent === '텍스트 선택');
  };
  let item = longPress(1);
  // The finger lifts where the menu now is: its click lands on an item.
  list.events.pointerup({ pointerId: 1, clientX: 300, clientY: 600 });
  item.events.click({ detail: 1, target: item });
  assert.equal(dialog.open, true, 'the opening finger runs nothing and the menu stays');
  // A press on the backdrop (not on the item) does not arm the item either.
  dialog.events.pointerdown({ pointerId: 2, pointerType: 'touch', target: dialog, clientX: 10, clientY: 10 });
  item.events.click({ detail: 1, target: item });
  assert.equal(dialog.open, true);
  // A tap that starts on the item runs it.
  dialog.events.pointerdown({ pointerId: 3, pointerType: 'touch', target: item, clientX: 300, clientY: 640 });
  item.events.click({ detail: 1, target: item });
  assert.equal(dialog.open, false, 'a deliberate tap on the item runs it');
  // The next menu forgets that press.
  item = longPress(4);
  item.events.click({ detail: 1, target: item });
  assert.equal(dialog.open, true);
  // Keyboard (Enter/Space) and assistive clicks have no pointer: detail 0.
  item.events.click({ detail: 0, target: item });
  assert.equal(dialog.open, false, 'the keyboard still runs items');
});

test('sending locks the draft read-only instead of disabling the field, so the focus and the next ⌘↩ stay', async () => {
  let release = null;
  const sends = [];
  const c = client(async (url, options) => {
    const body = JSON.parse(options.body);
    if (url === '/api/chat/send') {
      sends.push(body);
      await new Promise((resolve) => { release = resolve; });
      return { ok: true, status: 200, json: async () => ({ status: 'accepted' }) };
    }
    return { ok: true, status: 200, json: async () => ({ route: body.source, capability: { supported: true, attachments: true }, phase: 'idle' }) };
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  entry.data.capability.attachments = true;
  wireTree(c, c.buildChatComposer(s));
  await settle();
  const input = c.get('chat-input');
  c.context.document.activeElement = input;
  const key = { key: 'Enter', metaKey: true, isComposing: false, preventDefault() {} };
  input.value = '첫 메시지'; input.events.input();
  input.events.keydown(key);
  assert.equal(entry.sending, true);
  assert.equal(input.disabled, false, 'a disabled field would drop the focus (and the keyboard)');
  assert.equal(input.readOnly, true, 'the text being sent cannot change');
  assert.equal(input.attributes['aria-disabled'], 'true');
  input.events.keydown(key);
  input.events.paste({ clipboardData: { files: [{ name: 'a.png', size: 1, type: 'image/png' }], items: [], getData: () => '끼어든 글' },
    preventDefault() {} });
  assert.equal(entry.text, '첫 메시지', 'a paste cannot slip into the locked draft');
  await settle();
  assert.equal(sends.length, 1, 'a second ⌘↩ meanwhile sends nothing');
  release(); await settle();
  assert.equal(entry.pending, null);
  assert.equal(input.readOnly, false);
  assert.equal(input.attributes['aria-disabled'], 'false');
  assert.equal(input.value, '', 'the sent draft is cleared');
  // The next message goes out with the same key, without touching the field again.
  input.value = '두 번째 메시지'; input.events.input();
  input.events.keydown(key);
  await settle();
  assert.equal(sends.length, 2);
  assert.equal(sends[1].text, '두 번째 메시지');
  release(); await settle();
  // A session that cannot take input at all is still disabled.
  entry.data = { ...entry.data, capability: { supported: false, reason: '보드 밖 터미널' } };
  c.updateChatComposer(entry);
  assert.equal(input.disabled, true);
  assert.equal(input.readOnly, false);
  assert.match(styleSheet(), /\.chat-input:disabled,\n\.chat-input\[readonly\] \{/);
});

test('a tablet supports focus scrolling and taps, preserving the choice across keyboards and rotation', () => {
  const { c, pane, tap, scrollTo, reading } = phoneConversation({ wide: true });
  const typing = () => pane.classList.contains('is-typing');
  scrollTo(2350); scrollTo(2300); scrollTo(2200);
  assert.equal(reading(), true, 'tablet scrolling folds the tools');
  scrollTo(2300); scrollTo(2400);
  assert.equal(reading(), true, 'the bottom keeps focus mode');
  assert.equal(c.get('tap-hint').hidden, false, 'tablet receives the tap hint');
  tap();
  assert.equal(reading(), false, 'a safe tablet tap restores the tools');
  assert.equal(c.state.chromeVisible, true);
  // A hardware keyboard: nothing takes the screen (a shortcut bar stays below 150px).
  const input = c.get('chat-input');
  Object.assign(c.context.window, { innerWidth: 834, innerHeight: 1194, visualViewport: { height: 1194, scale: 1 } });
  c.context.document.activeElement = input;
  input.events.focus();
  assert.equal(typing(), false, 'the tools stay while typing on a hardware keyboard');
  c.context.window.visualViewport.height = 1194 - 55;
  c.updateViewport();
  assert.equal(typing(), false, 'a shortcut bar is not an on-screen keyboard');
  // Safari: the on-screen keyboard shrinks the visual viewport after the focus.
  c.context.window.visualViewport.height = 1194 - 360;
  c.updateViewport();
  assert.equal(typing(), true, 'an on-screen keyboard that took the space folds them as on a phone');
  c.context.window.visualViewport = { height: 1194 / 2, scale: 2 };
  c.updateViewport();
  assert.equal(typing(), false, 'pinch zoom is not a keyboard');
  // Chromium (resizes-content): the window shrinks too; the tallest one at this width counts.
  Object.assign(c.context.window, { innerHeight: 834, visualViewport: { height: 834, scale: 1 } });
  c.updateViewport();
  assert.equal(typing(), true);
  Object.assign(c.context.window, { innerHeight: 1194, visualViewport: { height: 1194, scale: 1 } });
  c.updateViewport();
  assert.equal(typing(), false, 'the keyboard went down while the input kept the focus');
  c.context.document.activeElement = null; input.events.blur();
  assert.equal(reading(), false);
  // Stage Manager: the window itself gets shorter while no text field has the focus.
  Object.assign(c.context.window, { innerHeight: 700, visualViewport: { height: 700, scale: 1 } });
  c.updateViewport();
  c.context.document.activeElement = input; input.events.focus();
  assert.equal(typing(), false, 'a shorter window is not a keyboard');
  c.context.document.activeElement = null; input.events.blur();
  // A phone that hid them becomes a wide screen (rotation, split view): they come back.
  const phone = phoneConversation();
  phone.scrollTo(2350);
  assert.equal(phone.reading(), true);
  assert.equal(phone.c.get('tap-hint').hidden, false);
  phone.c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' });
  phone.c.updateViewport();
  assert.equal(phone.reading(), true);
  assert.equal(phone.c.state.chromeVisible, false);
  // A phone held sideways (short) keeps the phone rules.
  const sideways = phoneConversation({ wide: true });
  sideways.c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' || query === '(max-height: 520px)' });
  sideways.scrollTo(2350);
  assert.equal(sideways.reading(), true);
  sideways.tap();
  assert.equal(sideways.reading(), false);
  const sidewaysInput = sideways.c.get('chat-input');
  sideways.c.context.document.activeElement = sidewaysInput; sidewaysInput.events.focus();
  assert.equal(sideways.pane.classList.contains('is-typing'), true);
});

test('wide touch screens read like a phone: 16px text, one reading column for bubbles, question card and composer, and a narrower list', () => {
  const css = styleSheet();
  const wide = css.match(/@media \(pointer: coarse\) and \(min-width: 701px\) and \(min-height: 521px\) \{([\s\S]*?)\n\}/);
  assert.ok(wide, 'a block for wide touch screens (phones held sideways excluded)');
  assert.match(wide[1], /--fs-body: 1rem;/);
  assert.match(wide[1], /--fs-sub: 0\.875rem;/);
  // 42.5rem = 680px at 16px; a bubble's text is 680 - 28px padding = 652px, about 40 Hangul characters.
  assert.match(wide[1], /--detail-pad: max\(16px, calc\(\(100% - 42\.5rem\) \/ 2\)\);/);
  assert.match(wide[1], /\.chat-prompt \{\s*margin-inline: var\(--detail-pad\);/, 'the question card sits in the same column');
  const list = css.match(/@media \(pointer: coarse\) and \(min-width: 701px\) and \(max-width: 1023px\) and \(min-height: 521px\) \{([\s\S]*?)\n\}/);
  assert.ok(list);
  assert.match(list[1], /\.layout \{\s*grid-template-columns: clamp\(256px, 33vw, 300px\) minmax\(0, 1fr\);/);
  // Same specificity as the rules they refine, so they must come later.
  for (const earlier of ['@media (max-width: 900px)', '.chat-prompt {', '.layout {'])
    assert.ok(css.indexOf(wide[0]) > css.indexOf(earlier), earlier);
  assert.ok(css.indexOf(list[0]) > css.indexOf('@media (max-width: 900px)'));
  // Phones keep their own values.
  const phone = cssBlock(css, '@media (max-width: 700px)');
  assert.equal(phone['--fs-body'], '1rem'); assert.equal(phone['--detail-pad'], '16px');
});

test('a hardware keyboard: the folded composer shows the send key once one is known, Esc leaves the input, and 1-9 choose on a question card', async () => {
  const requests = [];
  const c = client(async (url) => { requests.push(url); return { ok: true, status: 200, json: async () => ({}) }; }, noChatTimers);
  c.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' }); // a tablet
  c.context.navigator = { onLine: true, platform: 'MacIntel' }; // iPadOS Safari says Mac
  Object.assign(c.context.window, { innerWidth: 834, innerHeight: 1194, visualViewport: { height: 1194 - 360, scale: 1 } });
  const { s, entry } = selectChat(c);
  c.drawDetail(s);
  await settle();
  const hint = c.get('chat-key-hint');
  assert.equal(hint.textContent, '⌘↩ 보내기');
  assert.equal(c.get('chat-send').attributes['aria-keyshortcuts'], 'Meta+Enter Control+Enter');
  c.updateChatComposer(entry);
  assert.equal(hint.hidden, true, 'a touch screen alone may have no keyboard');
  const plain = { closest: () => null };
  const field = { closest: (query) => (/textarea/.test(query) ? {} : null) };
  const press = (extra) => c.context.documentKeydown?.({ target: plain, preventDefault() {}, ...extra });
  const input = c.get('chat-input');
  c.context.document.activeElement = input;
  press({ key: 'a', target: field });
  assert.equal(hint.hidden, true, 'typing on an on-screen keyboard that took the screen says nothing');
  press({ key: 'Unidentified', keyCode: 229, isComposing: true, target: field });
  assert.equal(hint.hidden, true);
  c.context.window.visualViewport.height = 1194 - 55;
  press({ key: 'a', target: field });
  assert.equal(hint.hidden, false, 'typing while nothing took the screen: a hardware keyboard');
  entry.expanded = true; c.updateChatComposer(entry);
  assert.equal(hint.hidden, true, 'the expanded composer says it in its own hint');
  entry.expanded = false; c.updateChatComposer(entry);
  assert.equal(hint.hidden, false);
  // Esc leaves the input; an IME or an open tools menu keep it.
  let blurred = 0; input.blur = () => { blurred++; };
  let prevented = 0;
  const escape = (extra = {}) => input.events.keydown({ key: 'Escape', preventDefault() { prevented++; }, ...extra });
  escape({ isComposing: true }); escape({ keyCode: 229 });
  c.get('chat-tools').open = true; escape(); c.get('chat-tools').open = false;
  assert.equal(blurred, 0);
  escape();
  assert.equal(blurred, 1); assert.equal(prevented, 1);
  // A question card: 1-9 choose (any option, the sheet's too); sending stays the second step.
  c.context.document.activeElement = null;
  const prompt = { agent: 'claude', fingerprint: 'f'.repeat(64), current: 1, multi: false, question: '계속할까요?',
    options: Array.from({ length: 7 }, (_, index) => ({ number: index + 1, label: `선택 ${index + 1}`, detail: '' })) };
  entry.data = { ...entry.data, prompt, phase: 'needs_input' };
  c.renderPrompt(s, entry.data);
  press({ key: '2', target: field });
  assert.equal(entry.promptChoice ?? null, null, 'a digit typed into a field is text');
  press({ key: '2', metaKey: true }); press({ key: '2', isComposing: true }); press({ key: '0' });
  assert.equal(entry.promptChoice ?? null, null);
  press({ key: '2' });
  assert.equal(entry.promptChoice.number, 2);
  assert.match(findClass(c.get('chat-prompt'), 'primary')[0].textContent, /2번 보내기/);
  press({ key: '6' });
  assert.equal(entry.promptChoice.number, 6, 'beyond the inline four');
  press({ key: '9' });
  assert.equal(entry.promptChoice.number, 6, 'no such option');
  c.get('action-sheet').open = true; press({ key: '1' }); c.get('action-sheet').open = false;
  c.state.terminalVisible = true; press({ key: '1' }); c.state.terminalVisible = false;
  entry.promptBusy = true; press({ key: '1' }); entry.promptBusy = false;
  assert.equal(entry.promptChoice.number, 6, 'an open sheet, the terminal or a busy card leave the key alone');
  assert.equal(requests.filter((url) => url === '/api/chat/prompt').length, 0, 'a key never sends');
  c.renderPrompt(s, { ...entry.data, prompt: { ...prompt, fingerprint: 'g'.repeat(64), multi: true } });
  press({ key: '3' });
  assert.notEqual(entry.promptChoice?.number, 3, 'a multi-select menu is answered in the terminal');
  // A phone: a modifier is evidence, typing is not; a mouse layout always has a keyboard.
  const phone = client(undefined, noChatTimers);
  phone.context.window.matchMedia = (query) => ({ matches: query === '(pointer: coarse)' || query === '(max-width: 700px)' });
  const onPhone = selectChat(phone);
  phone.state.detailShown = true;
  phone.drawDetail(onPhone.s); await settle();
  phone.context.document.activeElement = phone.get('chat-input');
  phone.context.documentKeydown?.({ key: 'a', target: field, preventDefault() {} });
  phone.updateChatComposer(onPhone.entry);
  assert.equal(phone.get('chat-key-hint').hidden, true);
  phone.context.documentKeydown?.({ key: 'Meta', metaKey: true, target: field, preventDefault() {} });
  assert.equal(phone.get('chat-key-hint').hidden, false);
  const desk = client(undefined, noChatTimers);
  desk.context.navigator = { onLine: true, platform: 'Win32' };
  const onDesk = selectChat(desk);
  desk.drawDetail(onDesk.s); await settle();
  desk.updateChatComposer(onDesk.entry);
  assert.equal(desk.get('chat-key-hint').hidden, false, 'a mouse layout has a keyboard');
  assert.equal(desk.get('chat-key-hint').textContent, 'Ctrl+Enter 보내기');
});

// --- Open board terminals shown on cards and in the conversation header ------

function attachedTo(s, fields = {}) {
  return { id: 'attached', alive: true, title: '보드 터미널', mode: 'attach',
    metadata: { nativeSource: { host: s.host, agent: s.agent, home: s.home, id: s.id }, mode: 'attach' }, ...fields };
}
function cardOf(c, s) {
  return byClass(c.get('groups'), 'card').find((card) => card.dataset.sessionKey === s.key);
}

test('the session list groups what needs an answer, what runs, what stopped in the last three hours and the rest', () => {
  const c = client(undefined, noChatTimers);
  const now = 2_000_000;
  const sessions = [
    recentSession('old-question', { phase: 'needs_input', updatedAt: now - 10 * 86400 }),
    recentSession('running', { phase: 'working', updatedAt: now - 60 }),
    recentSession('just-done', { phase: 'done', updatedAt: now - 3600 }),
    recentSession('just-idle', { phase: 'idle', updatedAt: now - 3 * 3600 }),
    recentSession('earlier', { phase: 'idle', updatedAt: now - 3 * 3600 - 1 }),
    recentSession('unknown', { phase: 'unknown', updatedAt: now - 60 }),
    recentSession('ancient', { phase: 'closed', updatedAt: now - 5 * 86400 }),
  ];
  c.state.snapshot = { serverTime: now, hosts: [{ name: 'workstation', label: 'Workstation', ok: true, data: { codex: sessions, claude: [] } }] };
  c.state.authenticated = true;
  const shown = c.allSessions().filter((s) => c.matches(s, now));
  c.renderGroups(shown, now);
  const sections = c.get('groups').children.filter((node) => node.dataset?.groupKey);
  assert.deepEqual(sections.map((section) => section.cardHeading.textContent), ['답 필요', '작업 중', '최근 끝남', '이전']);
  const ids = (section) => section.children.filter((node) => node.dataset?.sessionKey).map((node) => node.dataset.sessionKey.split('|').pop());
  // One title throughout: within a group the session key decides, never the update time.
  assert.deepEqual(sections.map(ids), [['old-question'], ['running'], ['just-done', 'just-idle'], ['earlier', 'unknown']]);
  assert.equal(sections[2].cardHeading.className, 'group-head green');
  assert.ok(!shown.some((s) => s.id === 'ancient'), 'only the 이전 group drops sessions older than three days');
});

test('each list group keeps its sessions in title order (가나다, numbers by value) however often they update; a rename moves one', () => {
  const c = client(undefined, noChatTimers);
  const now = 2_000_000;
  const at = (id, title, updatedAt) => recentSession(id, { phase: 'working', title, updatedAt });
  const titles = () => byClass(c.get('groups'), 'card').map((card) => byClass(card, 'card-title')[0].textContent);
  c.renderGroups([at('a', '작업 10', now - 1), at('b', '나 정리', now - 50), at('c', '작업 2', now - 9), at('d', '가 검토', now - 30)], now);
  assert.deepEqual(titles(), ['가 검토', '나 정리', '작업 2', '작업 10']);
  // Every session updates (in another order): the list does not move.
  c.renderGroups([at('a', '작업 10', now - 40), at('b', '나 정리', now), at('c', '작업 2', now - 70), at('d', '가 검토', now - 2)], now);
  assert.deepEqual(titles(), ['가 검토', '나 정리', '작업 2', '작업 10']);
  // A 제목 수정 is how one is moved.
  c.renderGroups([at('a', '0 먼저 볼 작업', now), at('b', '나 정리', now), at('c', '작업 2', now), at('d', '가 검토', now)], now);
  assert.deepEqual(titles(), ['0 먼저 볼 작업', '가 검토', '나 정리', '작업 2']);
});

// The board's own clock format; ICU data differs between Node versions (오후 / PM),
// so names are checked against the same formatter rather than a fixed spelling.
const seoulClock = (ts) => new Date(ts * 1000).toLocaleString('ko-KR', {
  timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

test('a 새 세션 terminal is named by what it runs, and a second view of the same Claude agents folder is marked as a repeat', () => {
  const c = client(undefined, noChatTimers);
  const launcher = (id, createdAt, program, programCwd, host = 'laptop') => ({ id, alive: true, title: 'zz', mode: 'new-session',
    host, createdAt, metadata: { mode: 'new-session', host, createdAt, program, programCwd } });
  c.state.terminals = [
    launcher('later', 1791792862, 'claude-agents', '/Users/example/project-a'),
    launcher('first', 1791785812, 'claude-agents', '/Users/example/project-a'),
    launcher('other-folder', 1791790000, 'claude-agents', '/Users/example/project-b'),
    launcher('codex', 1791791000, 'codex', null),
    launcher('mini', 1791791500, 'claude-agents', '/Users/example/project-a', 'mini'),
    { id: 'link', alive: true, title: '세션 연결', mode: 'attach', agent: 'claude', metadata: { mode: 'attach', agent: 'claude' } },
  ];
  c.renderTerminals();
  const rows = byClass(c.get('running-list'), 'running-terminal-row');
  assert.deepEqual(rows.map((row) => byClass(row, 'running-terminal-title')[0].textContent),
    ['Claude 에이전트 목록 · project-a', 'Claude 에이전트 목록 · project-a', 'Claude 에이전트 목록 · project-b',
      `Codex · ${seoulClock(1791791000)} 시작`, 'Claude 에이전트 목록 · project-a']);
  const repeated = rows.filter((row) => row.className.includes('duplicate')).map((row) => row.children[0].dataset.terminalId);
  assert.deepEqual(repeated, ['later'], 'only the later view of one host and folder repeats the first');
  assert.match(textOf(rows[0]), new RegExp(escapeRegExp(`같은 화면이 이미 열려 있어요(${seoulClock(1791785812)} 시작)`)));
  assert.equal(c.get('running-count').textContent, 5, 'the chip counts terminals the user opened');
  assert.match(c.get('running-terminals').attributes['aria-label'], /열린 터미널 5개, 세션 연결 1개/);
  assert.equal(byClass(c.get('running-list'), 'running-connections')[0].attributes['aria-expanded'], 'false');
});

test('the folded session connections row keeps the keyboard focus as it opens', () => {
  const c = client(undefined, noChatTimers);
  c.state.terminals = [{ id: 'link', alive: true, title: '세션 연결', mode: 'attach', agent: 'claude', metadata: { mode: 'attach', agent: 'claude' } }];
  c.renderTerminals();
  const first = byClass(c.get('running-list'), 'running-connections')[0];
  c.context.document.activeElement = first;
  first.events.click();
  const reopened = byClass(c.get('running-list'), 'running-connections')[0];
  assert.equal(reopened.attributes['aria-expanded'], 'true');
  assert.equal(reopened.focusCount, 1, 'the redrawn row takes the focus back');
  assert.equal(byClass(c.get('running-list'), 'running-terminal-row').length, 1, 'opening it shows the connection');
  c.context.document.activeElement = null;
});

test('a notification link opens its session once it is listed, and an old request expires', async () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }), noChatTimers);
  assert.equal(c.requestedSessionFrom('/?session=workstation%7Ccodex%7C.codex-example%7Cnote'), 'workstation|codex|.codex-example|note');
  assert.equal(c.requestedSessionFrom('/other?session=x'), null);
  assert.equal(c.requestedSessionFrom('/?view=list'), null);
  const s = recentSession('notified');
  c.state.authenticated = true;
  setRecentSnapshot(c, []);
  assert.equal(c.requestSessionOpen('/?session=' + encodeURIComponent(s.key)), true);
  assert.equal(c.state.selected, null, 'nothing opens before the session is listed');
  setRecentSnapshot(c, [s]);
  c.openRequestedSession();
  assert.equal(c.state.selected, s.key);
  assert.equal(c.state.openRequest, null);
  c.requestSessionOpen('/?session=' + encodeURIComponent('workstation|codex|.codex-example|missing'));
  c.state.openRequest.until = Date.now() - 1;
  c.openRequestedSession();
  assert.equal(c.state.openRequest, null, 'a request older than its wait is dropped');
  assert.equal(c.state.selected, s.key);
  // Opened while a terminal fills the screen: the terminal steps aside for the conversation.
  const other = recentSession('notified-over-terminal');
  setRecentSnapshot(c, [s, other]);
  c.state.terminal = attachedTo(s);
  c.state.terminalVisible = true;
  c.context.window.history.state = { [c.context.INSTANCE?.historyKey || 'sessionholic'] : true, view: 'terminal' };
  const pushed = [];
  const push = c.context.window.history.pushState;
  c.context.window.history.pushState = function (value) { pushed.push(value.view); return push.call(this, value); };
  c.requestSessionOpen('/?session=' + encodeURIComponent(other.key));
  c.openRequestedSession();
  assert.equal(c.state.terminalVisible, false);
  assert.equal(c.state.selected, other.key);
  // A conversation entry over the terminal's: back returns to the terminal.
  assert.deepEqual(pushed, ['detail']);
  assert.equal(c.context.window.history.state.view, 'detail');
  assert.equal(c.context.window.history.state.notice, other.key);
  // Any later entry leaves the notice behind.
  c.navigateView('terminal');
  assert.equal(c.context.window.history.state.notice, undefined);
});

test('forward again to a conversation a notification opened shows it, not the handoff the terminal launched', () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }), noChatTimers);
  c.context.navigator = { onLine: false };
  c.windowEvents.DOMContentLoaded();
  const s = recentSession('notified-forward');
  c.state.authenticated = true;
  setRecentSnapshot(c, [s]);
  c.select(s.key);
  const destination = { host: s.host, agent: 'codex', id: 'handoff-destination', home: '.codex-example', cwd: '/fixture' };
  c.state.terminal = { id: 'h'.repeat(32), alive: true, mode: 'handoff', launchMode: 'handoff', nativeSource: destination,
    metadata: { mode: 'handoff', launchMode: 'handoff', nativeSource: destination } };
  c.state.terminalVisible = true;
  c.windowEvents.popstate({ state: { sessionholic: true, view: 'detail', notice: s.key } });
  assert.equal(c.state.terminalVisible, false);
  assert.equal(c.state.selected, s.key, 'the notified conversation stays chosen');
});

test('an open terminal is named after the session it runs and follows a renamed session; a new-session terminal without one shows when it opened', async () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }), noChatTimers);
  const s = recentSession('named-terminal');
  setRecentSnapshot(c, [s]);
  const attached = attachedTo(s);
  const launcher = (id, createdAt) => ({ id, alive: true, title: 'zz', mode: 'new-session', createdAt,
    metadata: { mode: 'new-session', createdAt } });
  c.state.terminals = [attached, launcher('first', 1791792862), launcher('second', 1791785812)];
  c.state.showConnections = true;
  c.renderTerminals();
  const titles = () => byClass(c.get('running-list'), 'running-terminal-title').map((node) => node.textContent);
  // A Codex connection the user opened is listed with the other terminals (only Claude ones fold).
  assert.deepEqual(titles(), ['검토할 작업', `zz · ${seoulClock(1791792862)} 시작`, `zz · ${seoulClock(1791785812)} 시작`]);
  // A newer snapshot with the session renamed renames its terminal.
  // An open sheet is not rebuilt under the finger; it renames once it is not open.
  setRecentSnapshot(c, [{ ...s, title: '바뀐 작업 이름' }]);
  c.get('terminals-dialog').open = true;
  c.renameTerminals();
  assert.equal(titles()[0], '검토할 작업');
  c.get('terminals-dialog').open = false;
  c.renameTerminals();
  assert.equal(titles()[0], '바뀐 작업 이름');
  // A link estimated from start times says so.
  assert.equal(c.terminalTitle({ ...attached, metadata: { ...attached.metadata, linkConfidence: 'estimated' } }),
    '바뀐 작업 이름 · 연결 추정');
  // A session no longer listed leaves the title the terminal was opened with.
  setRecentSnapshot(c, []);
  assert.equal(c.terminalTitle(attached), '보드 터미널');
});

test('a card shows 터미널 열림 while its session has a live board terminal, by the same rule as the swipe, and follows each list read', async () => {
  let rows = [];
  const requests = [];
  const c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => ({ terminals: JSON.parse(JSON.stringify(rows)), version: 4 }) };
  }, noChatTimers);
  const s = recentSession('with-terminal');
  const other = recentSession('launched-here');
  setRecentSnapshot(c, [s, other]);
  c.renderGroups([s, other], 1100);
  const mark = (session) => byClass(cardOf(c, session), 'card-terminal')[0];
  assert.equal(mark(s).hidden, true);
  assert.doesNotMatch(cardOf(c, s).attributes['aria-label'], /터미널 열림/);
  rows = [attachedTo(s)];
  await c.refreshTerminals();
  assert.deepEqual(requests, ['/api/terminals']);
  assert.equal(c.state.terminalsVersion, 4);
  assert.equal(mark(s).hidden, false, 'the mounted card shows it without a full render');
  assert.match(textOf(mark(s)), /터미널 열림/);
  assert.match(cardOf(c, s).attributes['aria-label'], /\. 터미널 열림$/);
  assert.equal(mark(other).hidden, true);
  assert.equal(c.swipeActionFor(s).kind, 'close', 'the swipe offers 연결 닫기 for the same terminal');
  // A closed terminal no longer counts; one launched from a session (sourceKey) does.
  rows = [attachedTo(s, { alive: false, closed: true }),
    { id: 'launched', alive: true, mode: 'handoff', sourceKey: JSON.stringify({ agent: other.agent, home: other.home, host: other.host, id: other.id }) }];
  await c.refreshTerminals();
  assert.equal(mark(s).hidden, true);
  assert.equal(mark(other).hidden, false);
  assert.equal(c.swipeActionFor(other).kind, 'close');
  // A full render keeps the marks.
  c.renderGroups([s, other], 1100);
  assert.equal(mark(s).hidden, true);
  assert.equal(mark(other).hidden, false);
});

test('the conversation header shows 터미널 열림 and 터미널로 only with a live board terminal, and the button goes to that terminal', async () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ terminals: [] }) }), noChatTimers);
  const s = recentSession('detail-terminal');
  setRecentSnapshot(c, [s]);
  c.state.selected = s.key;
  c.drawDetail(s);
  const box = c.get('detail-terminal');
  assert.equal(box.hidden, true, 'no board terminal: nothing in the header (the composer keeps its own 터미널 열기)');
  c.state.terminals = [attachedTo(s)];
  c.renderTerminals();
  assert.equal(box.hidden, false);
  assert.match(textOf(box), /터미널 열림/);
  const go = c.get('detail-terminal-open');
  assert.equal(go.textContent, '터미널로');
  // The terminal is named after the session it runs, not the title it was opened with.
  assert.match(go.attributes['aria-label'], /열린 터미널로 가기: 검토할 작업/);
  // A new header drawn while the terminal is live shows it at once.
  c.drawDetail(s);
  assert.equal(c.get('detail-terminal').hidden, false);
  const opened = [];
  c.context.openTerminal = (terminal) => { opened.push(terminal.id); };
  c.get('detail-terminal-open').events.click();
  assert.deepEqual(opened, ['attached']);
  // Closed meanwhile (another device): the tap opens nothing and the header hides it.
  c.state.terminals = [attachedTo(s, { alive: false, closed: true })];
  c.get('detail-terminal-open').events.click();
  assert.deepEqual(opened, ['attached']);
  assert.equal(c.get('detail-terminal').hidden, true);
  // The header of another session is never changed by this session's terminal.
  const other = recentSession('other-detail');
  setRecentSnapshot(c, [s, other]);
  c.state.selected = other.key;
  c.drawDetail(other);
  c.state.terminals = [attachedTo(s)];
  c.renderTerminals();
  assert.equal(c.get('detail-terminal').hidden, true);
});

test('terminal events reread the list once per 300ms burst, only when newer, not on a first hello, and again after a read in flight', async () => {
  const requests = [];
  const batches = [];
  let version = 5;
  let hold = null;
  const c = client(async (url) => {
    requests.push(url);
    if (hold) await hold;
    const answered = version;
    return { ok: true, status: 200, json: async () => ({ terminals: [], version: answered }) };
  }, { setTimeout: (callback, ms) => { if (ms === 300) batches.push(callback); return batches.length; }, clearTimeout() {} });
  c.state.authenticated = true;
  // The first connection: login and startup read the list themselves.
  c.terminalsAnnounced(3, { hello: true });
  assert.equal(batches.length, 0);
  await c.refreshTerminals();
  assert.equal(c.state.terminalsVersion, 5);
  requests.length = 0;
  c.terminalsAnnounced(5);
  assert.equal(batches.length, 0, 'a version already read reads nothing');
  for (const announced of [6, 7, 8]) c.terminalsAnnounced(announced);
  assert.equal(batches.length, 1, 'a burst arms one read');
  assert.deepEqual(requests, [], 'nothing is read before the batch window ends');
  version = 8;
  batches.shift()();
  await settle();
  assert.deepEqual(requests, ['/api/terminals']);
  assert.equal(c.state.terminalsVersion, 8);
  // Announced while a read is in flight: that read may predate it, so one more follows.
  requests.length = 0;
  let release;
  hold = new Promise((resolve) => { release = resolve; });
  version = 9;
  const reading = c.refreshTerminals();
  c.terminalsAnnounced(10);
  batches.shift()();
  assert.equal(c.state.terminalsPending, true);
  assert.deepEqual(requests, ['/api/terminals'], 'never two reads at once');
  hold = null;
  release();
  await reading;
  assert.equal(batches.length, 1, 'still behind (9 < 10): one more batched read');
  version = 10;
  batches.shift()();
  await settle();
  assert.deepEqual(requests, ['/api/terminals', '/api/terminals']);
  assert.equal(c.state.terminalsVersion, 10);
  // A reconnect's hello reads only a newer list.
  c.terminalsAnnounced(10, { hello: true });
  assert.equal(batches.length, 0);
  c.terminalsAnnounced(12, { hello: true });
  assert.equal(batches.length, 1);
  // A batch from before a login change reads nothing.
  c.state.authEpoch++;
  requests.length = 0;
  batches.shift()();
  await settle();
  assert.deepEqual(requests, []);
});

test('the live stream announces terminals, a restarted server is read on its hello, and the regular poll is the safety net', async () => {
  const requests = [];
  const batches = [];
  let c;
  c = client(async (url) => {
    requests.push(url);
    return { ok: true, status: 200, json: async () => (url === '/api/terminals' ? { terminals: [], version: 2 }
      : url.startsWith('/api/snapshot') ? { hosts: c.state.snapshot.hosts, bootId: 'boot-a' } : {}) };
  }, { setTimeout: (callback, ms) => { if (ms === 300) batches.push(callback); return 0; }, clearTimeout() {} });
  const { FakeEventSource, sources } = fakeEventSource();
  c.context.EventSource = FakeEventSource;
  selectChat(c);
  c.state.capabilities = {};
  c.state.capabilitiesRevision = c.capabilitiesRevision(c.state.snapshot);
  c.state.capabilitiesBoot = 'boot-a';
  c.state.usageAt = Date.now(); c.state.hardwareAt = Date.now();
  c.state.serverBoot = 'boot-a';
  await c.refreshTerminals();
  c.syncEvents();
  sources[0].emit('hello', { snapshotVersion: 1, bootId: 'boot-a', terminalsVersion: 2 });
  assert.equal(batches.length, 0, 'the list read already shows version 2');
  sources[0].emit('terminals', { version: 3 });
  assert.equal(batches.length, 1);
  await settle();
  requests.length = 0;
  batches.shift()();
  await settle();
  assert.deepEqual(requests.filter((url) => url === '/api/terminals'), ['/api/terminals']);
  // Another server process counts from 0 again: its hello is read even at a lower number.
  sources[0].emit('hello', { snapshotVersion: 0, bootId: 'boot-b', terminalsVersion: 0 });
  assert.equal(c.state.terminalsVersion, null);
  assert.equal(batches.length, 1);
  await settle(6);
  // Without live updates, a regular read rereads a list older than a minute.
  c.closeEvents();
  c.state.serverBoot = 'boot-a';
  batches.length = 0;
  requests.length = 0;
  c.state.terminalsReadAt = Date.now();
  await c.poll(false);
  assert.deepEqual(requests.filter((url) => url === '/api/terminals'), [], 'a fresh list is not read again');
  c.state.terminalsReadAt = Date.now() - 61000;
  requests.length = 0;
  await c.poll(false);
  await settle();
  assert.deepEqual(requests.filter((url) => url === '/api/terminals'), ['/api/terminals']);
  requests.length = 0;
  c.state.terminalsReadAt = Date.now() - 61000;
  await c.poll(false, { capabilities: false });
  assert.deepEqual(requests.filter((url) => url === '/api/terminals'), [], 'an announced snapshot read stays a snapshot read');
});

test('a terminal opened or closed here shows on its card and header at once, before the list is read again', async () => {
  const s = recentSession('opened-here');
  const opened = attachedTo(s, { id: 'b'.repeat(32) });
  const requests = [];
  const c = client(async (url) => {
    requests.push(url);
    if (url === '/api/terminals') return await new Promise(() => {});
    if (url === '/api/launch') return { ok: true, status: 200, json: async () => ({ terminal: opened, mode: 'attach' }) };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, noChatTimers);
  setRecentSnapshot(c, [s]);
  c.state.selected = s.key;
  c.renderGroups([s], 1100);
  c.drawDetail(s);
  c.context.openTerminal = async () => {};
  c.state.plan = { id: 'plan-1', allowed: true, mode: 'attach' };
  await c.launchPlan();
  assert.ok(requests.includes('/api/terminals'), 'the list is read again (still in flight here)');
  assert.equal(byClass(cardOf(c, s), 'card-terminal')[0].hidden, false);
  assert.equal(c.get('detail-terminal').hidden, false);
  c.closeTerminal(opened.id);
  c.confirmTerminalClose();
  await settle();
  assert.equal(byClass(cardOf(c, s), 'card-terminal')[0].hidden, true, 'the close answer alone hides it');
  assert.equal(c.get('detail-terminal').hidden, true);
  assert.equal(c.state.terminals.find((item) => item.id === opened.id).closed, true);
});

// "새 세션": a host's own launcher in a terminal; its numbered menu becomes buttons.
const LAUNCHER_MENU = ['', '프로젝트 선택', '  1. Alpha project', '  2. Personal', '  q. 취소', '선택:'];

test('a launcher menu is read from the rows above the prompt on the cursor line', () => {
  const c = client();
  const menu = c.terminalMenu(LAUNCHER_MENU);
  assert.equal(menu.title, '프로젝트 선택');
  assert.equal(menu.prompt, '선택');
  assert.deepEqual(JSON.parse(JSON.stringify(menu.options)),
    [{ key: '1', label: 'Alpha project' }, { key: '2', label: 'Personal' }, { key: 'q', label: '취소' }]);
  // A refused answer and its hint between the rows and the prompt asked again.
  const retried = c.terminalMenu([...LAUNCHER_MENU.slice(0, -1), '선택: x', '번호를 직접 선택하세요.', '선택:']);
  assert.equal(retried.options.length, 3);
  // Not a menu: an answer already typed, no numbered row, a drawn list, a distant prompt.
  assert.equal(c.terminalMenu([...LAUNCHER_MENU.slice(0, -1), '선택: 1']), null);
  assert.equal(c.terminalMenu(['계정', '  a. 하나', '  b. 둘', '선택:']), null);
  assert.equal(c.terminalMenu(['│ 1. Yes      │', '│ 2. No       │', '╰─────────────╯', '› ']), null);
  assert.equal(c.terminalMenu([...LAUNCHER_MENU.slice(0, -1), ...Array(7).fill('출력'), '선택:']), null);
  assert.equal(c.terminalMenu(['  1. 하나', '  1. 둘', '선택:']), null);
});

function menuScreen(c, lines, mode = 'new-session') {
  c.state.terminal = { id: 'c'.repeat(32), mode };
  c.state.terminalAlive = true;
  c.state.term = { buffer: { active: { baseY: 0, cursorY: lines.length - 1,
    getLine: (y) => ({ translateToString: () => lines[y] }) } } };
}

test('only a new-session terminal turns a launcher menu into buttons that type the key and Enter', () => {
  const c = client();
  const sent = [];
  c.context.sendInput = (text) => { sent.push(text); return Promise.resolve(); };
  menuScreen(c, LAUNCHER_MENU, 'attach');
  c.updateTerminalMenu();
  assert.equal(c.get('terminal-menu').hidden, true, 'an attached agent CLI never gets menu buttons');
  menuScreen(c, LAUNCHER_MENU);
  c.updateTerminalMenu();
  assert.equal(c.get('terminal-menu').hidden, false);
  assert.equal(c.get('terminal-menu-title').textContent, '프로젝트 선택');
  const options = c.get('terminal-menu-options').children;
  assert.deepEqual(options.map((node) => node.textContent), ['1. Alpha project', '2. Personal', '취소']);
  assert.match(options[2].className, /cancel/);
  options[1].events.click();
  assert.deepEqual(sent, ['2\r']);
  assert.equal(c.get('terminal-menu').hidden, true, 'hidden until the launcher answers');
  // The echoed answer is not a menu; the next menu appears with the next output.
  menuScreen(c, [...LAUNCHER_MENU.slice(0, -1), '선택: 2']);
  c.updateTerminalMenu();
  assert.equal(c.get('terminal-menu').hidden, true);
  menuScreen(c, [...LAUNCHER_MENU.slice(0, -1), '선택: 2', '', '에이전트 선택', '  1. Codex', '  2. Claude Code', '  q. 취소', '선택:']);
  c.updateTerminalMenu();
  assert.equal(c.get('terminal-menu-title').textContent, '에이전트 선택');
  c.state.terminalAlive = false;
  c.updateTerminalMenu();
  assert.equal(c.get('terminal-menu').hidden, true, 'an ended terminal shows no menu');
});

const NEW_SESSION_HOSTS = [
  { name: 'mini', label: 'Mini', online: true, newSession: { available: true, label: 'launcher' } },
  { name: 'air', label: 'Air', online: true, newSession: { available: false, label: null, reason: '이 기기에는 새 세션 실행 명령이 설정되지 않았습니다.' } },
  { name: 'old', label: 'Old', online: false, newSession: { available: true, label: 'launcher' } },
];

test('the new session chip shows only when a host can open one; the sheet says why others cannot', () => {
  const c = client();
  c.state.capabilities = { hosts: NEW_SESSION_HOSTS.slice(1, 2) };
  c.renderNewSession();
  assert.equal(c.get('new-session').hidden, true);
  c.state.capabilities = { hosts: NEW_SESSION_HOSTS };
  c.renderNewSession();
  assert.equal(c.get('new-session').hidden, false);
  c.openNewSession();
  const rows = c.get('new-session-hosts').children;
  assert.deepEqual(rows.map((row) => row.disabled), [false, true, true]);
  assert.match(textOf(rows[0]), /launcher 실행/);
  assert.match(textOf(rows[1]), /설정되지 않았습니다/);
  assert.match(textOf(rows[2]), /연결할 수 없습니다/);
  assert.equal(c.get('new-session-dialog').open, true);
});

test('opening a new session posts once per host and request, then opens its terminal', async () => {
  const calls = [];
  let fail = true;
  const terminal = { id: 'd'.repeat(32), title: 'launcher', host: 'mini', mode: 'new-session', alive: true };
  const c = client(async (url, options) => {
    calls.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url === '/api/new-session') {
      if (fail) { fail = false; return { ok: false, status: 409, json: async () => ({ error: '기기 응답 시간이 초과됐습니다.' }) }; }
      return { ok: true, status: 200, json: async () => ({ terminal, mode: 'new-session' }) };
    }
    return { ok: true, status: 200, json: async () => ({ terminals: [terminal] }) };
  });
  const opened = [];
  c.context.openTerminal = async (value) => { opened.push(value.id); };
  c.state.capabilities = { hosts: NEW_SESSION_HOSTS };
  c.openNewSession();
  await c.startNewSession('mini');
  assert.equal(c.get('new-session-error').hidden, false);
  assert.match(c.get('new-session-error').textContent, /시간이 초과/);
  assert.equal(c.get('new-session-dialog').open, true, 'the sheet stays for a retry');
  await c.startNewSession('mini');
  const posts = calls.filter((call) => call.url === '/api/new-session');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].body.host, 'mini');
  assert.equal(posts[0].body.requestId, posts[1].body.requestId, 'a retry reuses the request id');
  assert.deepEqual(opened, [terminal.id]);
  assert.equal(c.get('new-session-dialog').open, false);
  assert.ok(c.state.terminals.some((item) => item.id === terminal.id));
  assert.equal(c.terminalCloseCopy(terminal).action, '실행 종료');
  assert.equal(c.terminalCloseCopy(terminal).danger, true);
  // A PC conversation resumed in a board terminal runs there: closing it ends the run.
  const resumed = c.terminalCloseCopy({ metadata: { mode: 'resume', launchMode: 'attach' } });
  assert.equal(resumed.action, '실행 종료');
  assert.equal(resumed.danger, true);
  assert.match(resumed.description, /다시 연 대화/);
});

// The conversation keeps its newest message in view (pinnedToEnd, keepAtEnd).
function scrollingList(c, { scrollHeight, clientHeight }) {
  const list = c.get('messages');
  let top = 0;
  Object.defineProperty(list, 'scrollTop', { configurable: true, get: () => top,
    // A browser ignores a scroll on a list without a box and clamps one past the end.
    set: (value) => { if (list.clientHeight) top = Math.max(0, Math.min(value, list.scrollHeight - list.clientHeight)); } });
  Object.assign(list, { scrollHeight, clientHeight });
  return list;
}
function resizeObservers(c) {
  const observers = [];
  c.context.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.targets = []; observers.push(this); }
    observe(target) { this.targets.push(target); }
    disconnect() { this.targets = []; }
  };
  return () => { for (const observer of observers) observer.callback(observer.targets.map((target) => ({ target }))); };
}
const LONG_CONVERSATION = Array.from({ length: 30 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', text: `메시지 ${i}`, ts: i + 1 }));
function openLong(c, id) {
  const s = recentSession(id);
  setRecentSnapshot(c, [s]);
  c.state.selected = s.key;
  c.drawDetail(s);
  return s;
}

test('a conversation drawn while its pane is still hidden opens at its newest message once laid out', () => {
  const c = client(undefined, noChatTimers);
  const resize = resizeObservers(c);
  const s = openLong(c, 'hidden-open');
  const list = scrollingList(c, { scrollHeight: 3000, clientHeight: 0 });
  // A phone draws the cached copy before it shows the conversation pane.
  c.renderMessages(s, LONG_CONVERSATION, '', true);
  assert.equal(list.scrollTop, 0, 'a hidden list cannot scroll');
  list.clientHeight = 600;
  resize();
  assert.equal(list.scrollTop, 2400);
  list.scrollHeight = 3200;
  c.renderMessages(s, [...LONG_CONVERSATION, { role: 'assistant', text: '새 답', ts: 99 }]);
  assert.equal(list.scrollTop, 2600, 'the read that follows stays at the end');
  assert.equal(c.get('jump-bottom').hidden, true);
  // Without a resize callback, showing the pane puts it at the end as well.
  const other = client(undefined, noChatTimers);
  const t = openLong(other, 'hidden-open-2');
  const plain = scrollingList(other, { scrollHeight: 3000, clientHeight: 0 });
  other.renderMessages(t, LONG_CONVERSATION, '', true);
  plain.clientHeight = 600;
  other.setDetailShown(true);
  assert.equal(plain.scrollTop, 2400);
});

test('a question card, the keyboard or the bars shrinking the list keep a pinned conversation at its end', () => {
  const c = client(undefined, noChatTimers);
  const resize = resizeObservers(c);
  const s = openLong(c, 'pinned-shrink');
  const list = scrollingList(c, { scrollHeight: 3000, clientHeight: 600 });
  c.renderMessages(s, LONG_CONVERSATION, '', true);
  assert.equal(list.scrollTop, 2400);
  list.clientHeight = 350;
  resize();
  assert.equal(list.scrollTop, 2650);
  // A read that comes before the resize callback still sticks, though 300px from the end.
  list.clientHeight = 300;
  c.renderMessages(s, [...LONG_CONVERSATION, { role: 'assistant', text: '질문입니다', ts: 99 }]);
  assert.equal(list.scrollTop, 2700);
  assert.equal(list.unread, 0);
});

test('a reader who scrolled up keeps the place through resizes and new messages until 맨 아래', () => {
  const c = client(undefined, noChatTimers);
  const resize = resizeObservers(c);
  const s = openLong(c, 'reading-back');
  const list = scrollingList(c, { scrollHeight: 3000, clientHeight: 600 });
  c.renderMessages(s, LONG_CONVERSATION, '', true);
  list.scrollTop = 1200;
  c.readingScroll(list);
  assert.equal(list.pinned, false);
  list.clientHeight = 500;
  resize();
  assert.equal(list.scrollTop, 1200);
  c.renderMessages(s, [...LONG_CONVERSATION, { role: 'assistant', text: '새 답', ts: 99 }]);
  assert.equal(list.scrollTop, 1200);
  assert.equal(list.unread, 1);
  c.get('jump-bottom').events.click();
  assert.equal(list.scrollTop, 2500);
  assert.equal(list.pinned, true);
  list.clientHeight = 450;
  resize();
  assert.equal(list.scrollTop, 2550);
});

test('a new-session terminal belongs to the session the server linked it to, and only then', () => {
  const c = client(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) }), noChatTimers);
  const s = recentSession('launched-here', { home: '.codex', id: '019a0000-0000-7000-8000-000000000004' });
  const terminal = { id: 'e'.repeat(32), alive: true, title: 'zz', mode: 'new-session', host: 'workstation',
    metadata: { mode: 'new-session', host: 'workstation', title: 'zz', createdAt: 1000 } };
  c.state.terminals = [terminal];
  assert.equal(c.terminalForSession(s), null, 'no session is guessed in the browser');
  c.state.terminals = [{ ...terminal, nativeSource: { host: s.host, agent: 'codex', id: s.id, home: '.codex', cwd: '/work' },
    metadata: { ...terminal.metadata, nativeSource: { host: s.host, agent: 'codex', id: s.id, home: '.codex', cwd: '/work' } } }];
  assert.equal(c.terminalForSession(s)?.id, terminal.id);
});

// A Codex account without its app-server: listed from its state DB; "앱 서버 켜기" starts it.
test('a conversation of an account whose app-server is off offers to start it, then reads again', async () => {
  const calls = [];
  const c = client(async (url, options) => {
    calls.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url === '/api/app-server') return { ok: true, status: 200, json: async () => ({ ok: true, started: true }) };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, noChatTimers);
  const s = recentSession('off-thread', { home: '.codex', appServer: 'off', phase: 'closed' });
  setRecentSnapshot(c, [s]);
  c.state.selected = s.key;
  c.renderGroups([s], 1100);
  c.drawDetail(s);
  const reloaded = [];
  c.context.poll = async (refresh) => { reloaded.push(['poll', refresh]); };
  c.context.loadDetail = async (current, fresh) => { reloaded.push(['detail', current.key, fresh]); };
  c.renderMessages(s, [], '이 계정의 Codex 앱 서버가 꺼져 있어 대화를 읽지 못했습니다.');
  const box = byClass(c.get('messages'), 'conversation-read-error')[0];
  assert.match(box.className, /app-server-off/);
  const start = byClass(box, 'app-server-start')[0];
  assert.equal(start.textContent, '앱 서버 켜기');
  assert.equal(byClass(box, 'secondary').length, 0, 'no plain retry: reading cannot work before the start');
  await start.events.click({ currentTarget: start });
  const posts = calls.filter((call) => call.url === '/api/app-server');
  assert.deepEqual(posts.map((call) => call.body), [{ host: 'workstation', home: '.codex' }]);
  assert.deepEqual(reloaded, [['poll', true], ['detail', s.key, true]]);
  assert.equal(start.disabled, false);
  // An ordinary read failure keeps its retry button.
  const other = recentSession('other-thread');
  c.renderMessages(other, [], '대화를 읽지 못했습니다.');
  const plain = byClass(c.get('messages'), 'conversation-read-error')[0];
  assert.equal(byClass(plain, 'app-server-start').length, 0);
  assert.equal(byClass(plain, 'secondary')[0].textContent, '대화 다시 불러오기');
});


test('focus view restores the message anchor per conversation without storing conversation text', () => {
  const c = client(undefined, noChatTimers);
  const { s } = selectChat(c);
  const list = c.get('messages');
  Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 400, lastScrollTop: 400, pinned: false });
  c.renderMessages(s, [{ role: 'assistant', text: '검증용 대화', ts: 1 }]);
  const item = list.conversationRows[0]; Object.assign(item.row, { offsetTop: 350, offsetHeight: 150 });
  c.state.chromeVisible = false;
  c.rememberConversationView();
  const saved = c.state.conversationViews.get(c.conversationIdentity(s));
  assert.equal(saved.top, 400); assert.equal(saved.chromeVisible, false); assert.equal(saved.pinned, false);
  assert.equal(saved.anchor, item.key);
  assert.equal(c.state.conversationViews.has(c.conversationIdentity({ ...s, home: '.codex-other' })), false);
  Object.assign(item.row, { offsetTop: 550 });
  list.pendingView = saved; list.scrollTop = 0;
  c.restoreConversationView(list);
  assert.equal(list.scrollTop, 600, 'the same message keeps its visual offset after layout changes');
  assert.equal(list.pinned, false);
  c.showLogin();
  assert.equal(c.state.conversationViews.size, 0, 'locking clears private view metadata');
});

test('focus restoration waits for a hidden pane to acquire its real size', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  c.renderMessages(s, [{ role: 'assistant', text: '기존 답', ts: 1 }]);
  list.pendingView = { top: 225, pinned: false, anchor: null, offset: 0, chromeVisible: false };
  list.clientHeight = 0; c.restoreConversationView(list);
  assert.ok(list.pendingView);
  list.clientHeight = 600; c.restoreConversationView(list);
  assert.equal(list.scrollTop, 225); assert.equal(list.pendingView, null);
});

test('reopening the same retained conversation restores its scroll after a hidden parent reset', () => {
  const c = client(undefined, noChatTimers); const { s } = selectChat(c);
  const list = c.get('messages');
  c.renderMessages(s, [{ role: 'assistant', text: '다시 읽을 대화', ts: 1 }]);
  Object.assign(list, { scrollHeight: 3000, clientHeight: 600, scrollTop: 720, lastScrollTop: 720, pinned: false });
  c.state.chromeVisible = false;
  c.rememberConversationView();
  c.state.detailShown = false;
  list.scrollTop = 0; // Browsers reset this when the retained node moves to a hidden pane.
  c.select(s.key);
  assert.equal(list.scrollTop, 720);
  assert.equal(c.state.chromeVisible, false);
  list.scrollTop = 840;
  c.select(s.key);
  assert.equal(list.scrollTop, 840, 'selecting an already visible conversation does not replay an older view');
});

test('session origin filters never equate execution surface with its initiator', () => {
  const c = client(undefined, noChatTimers); c.state.showOld = true;
  for (const row of [{ source: 'cli' }, { kind: 'background' }, { source: 'exec' }]) {
    assert.equal(c.initiatorOf(row), 'unknown');
    c.state.filters.initiator = 'human'; assert.equal(c.matches(row, 100), false);
    c.state.filters.initiator = 'unknown'; assert.equal(c.matches(row, 100), true);
  }
  c.state.filters.initiator = 'agent'; c.state.sessionCategory = 'agent';
  assert.equal(c.matches({ initiator: 'agent', initiatorName: 'Planner' }, 100), true);
  assert.equal(c.initiatorLabel({ initiator: 'agent', initiatorName: 'Planner' }), '에이전트 시작 · Planner');
});

test('Ctrl or Command K focuses the visible unified search without submitting a draft', () => {
  const c = client(undefined, noChatTimers); let prevented = 0;
  c.documentKeydown({ key: 'k', metaKey: true, preventDefault() { prevented++; } });
  assert.equal(c.get('search').focusCount, 1); assert.equal(prevented, 1);
  c.documentKeydown({ key: 'k', metaKey: true, isComposing: true, preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
});


test('Windows 저장 기록의 실행 미확인은 기기 연결·과거 live phase와 별개로 표시한다', () => {
  const c = client(undefined, noChatTimers);
  const row = recentSession('windows-record', { host: 'windows-pc', phase: 'unknown', runtimeKnown: false, readOnly: true });
  c.state.authenticated = true;
  c.state.snapshot = { serverTime: 1100, hosts: [{ name: 'windows-pc', label: '작업실 PC', ok: true, fetchedAt: 1099, data: { codex: [row] } }] };
  c.state.livePhases.set(row.key, { base: 'unknown', phase: 'working' });
  const record = c.allSessions()[0];
  assert.equal(record.phase, 'unknown'); assert.equal(record.stale, false); assert.equal(record.hostLabel, '작업실 PC');
  c.renderGroups([record], 1100);
  let card = byClass(c.get('groups'), 'card')[0];
  assert.equal(byClass(card, 'badge')[0].textContent, '실행 확인 전');
  assert.equal(byClass(card, 'card-device-label')[0].textContent, '작업실 PC');
  assert.equal(byClass(card, 'stale-tag')[0].hidden, true, '실행 미확인을 오프라인으로 표시하지 않는다');
  const detailMeta = vm.runInContext('detailMetaChildren', c.context)(record);
  assert.equal(detailMeta[0].textContent, '실행 확인 전');
  vm.runInContext('noteLivePhase', c.context)(record, { route: c.sourceOf(record), phase: 'idle' });
  assert.equal(c.allSessions()[0].phase, 'unknown', '저장된 대화의 phase는 실행 상태의 증거가 아니다');
  c.state.snapshot.hosts[0].ok = false;
  c.renderGroups(c.allSessions(), 1100); card = byClass(c.get('groups'), 'card')[0];
  assert.equal(byClass(card, 'badge')[0].textContent, '실행 확인 전');
  assert.equal(byClass(card, 'stale-tag')[0].hidden, false, '기기 연결이 끊긴 경우에는 별도 이전 정보 표시를 붙인다');
});


test('세션 분류는 명시 agent만 분리하고 일반 세션에 human과 미확인 실행을 중복 없이 남긴다', () => {
  const c = client(undefined, noChatTimers);
  const rows = [recentSession('human', { initiator: 'human' }), recentSession('agent', { initiator: 'agent' }),
    ...['cli', 'background', 'exec'].map((kind) => recentSession(kind, { source: kind, kind, parentSessionId: 'agent', agentName: 'worker' }))];
  setRecentSnapshot(c, rows);
  const general = rows.filter(s => c.matches(s, 1100));
  assert.deepEqual(general.map(s => s.id), ['human', 'cli', 'background', 'exec']);
  c.state.sessionCategory = 'agent';
  const agents = rows.filter(s => c.matches(s, 1100)); assert.deepEqual(agents.map(s => s.id), ['agent']);
  assert.equal(new Set([...general, ...agents].map(s => s.key)).size, rows.length);
  c.renderSessionCategories(rows, 1100);
  assert.equal(c.get('session-category-general-count').textContent, '4');
  assert.equal(c.get('session-category-agent-count').textContent, '1');
  assert.equal(c.get('session-category-agent').attributes['aria-selected'], 'true');
  assert.equal(c.get('groups').attributes['aria-labelledby'], 'session-category-agent');
});

test('검색은 선택 분류에서 동작하고 다른 분류의 일치 결과로 조건을 유지해 이동한다', () => {
  const c = client(undefined, noChatTimers);
  const rows = [recentSession('human', { initiator: 'human', title: '일반 문서 검토' }),
    recentSession('agent', { initiator: 'agent', title: '검색 회귀 확인', phase: 'working' }),
    recentSession('other-device', { initiator: 'agent', host: 'other', title: '검색 회귀 확인', phase: 'working' })];
  setRecentSnapshot(c, rows.slice(0, 2));
  c.state.snapshot.hosts.push({ name: 'other', label: '다른 기기', ok: true, data: { codex: [rows[2]] } });
  c.state.search = '검색 회귀'; c.state.filters.host = 'workstation'; c.state.phaseFilter = 'working';
  c.render();
  assert.equal(byClass(c.get('groups'), 'card').length, 0);
  assert.equal(c.get('session-category-search').hidden, false);
  assert.match(c.get('session-category-search-action').textContent, /에이전트 세션.*1개/);
  c.setSessionCategory(c.get('session-category-search-action').dataset.category);
  assert.equal(c.state.search, '검색 회귀'); assert.equal(c.state.filters.host, 'workstation'); assert.equal(c.state.phaseFilter, 'working');
  assert.equal(byClass(c.get('groups'), 'card').length, 1); assert.equal(c.get('session-category-search').hidden, true);
});

test('시작 주체 필터와 분류를 동기화하고 초기화는 현재 분류를 유지한다', () => {
  const c = client(undefined, noChatTimers);
  const rows = [recentSession('human', { initiator: 'human' }), recentSession('agent', { initiator: 'agent' }), recentSession('unknown')];
  setRecentSnapshot(c, rows); c.renderFilters(rows);
  let control = c.get('filter-initiator'); control.value = 'agent'; control.events.change();
  assert.equal(c.state.sessionCategory, 'agent'); assert.equal(c.state.filters.initiator, 'agent');
  assert.equal(byClass(c.get('groups'), 'card').length, 1);
  c.resetFilters(); assert.equal(c.state.sessionCategory, 'agent'); assert.equal(c.state.filters.initiator, '');
  control = c.get('filter-initiator'); control.value = 'unknown'; control.events.change();
  assert.equal(c.state.sessionCategory, 'general'); assert.equal(byClass(c.get('groups'), 'card').length, 1);
  control = c.get('filter-initiator'); control.value = 'human'; control.events.change();
  assert.equal(c.get('session-category-note').hidden, true, '직접 시작만 보는 중에는 미확인 포함 안내를 숨긴다');
  c.setSessionCategory('agent'); assert.equal(c.state.filters.initiator, '', '분류와 충돌하는 시작 주체 조건만 해제한다');
  c.sessionCategoryKeydown({ key: 'Home', preventDefault() {} });
  assert.equal(c.state.sessionCategory, 'general'); assert.equal(c.get('session-category-general').tabIndex, 0);
  assert.ok(c.get('session-category-general').focusCount > 0);
  c.sessionCategoryKeydown({ key: 'ArrowRight', preventDefault() {} });
  assert.equal(c.state.sessionCategory, 'agent'); assert.equal(c.get('session-category-agent').attributes['aria-selected'], 'true');
});

test('작업에서 대화를 직접 열면 대상 분류와 목록 조건을 맞추고 분류 전환은 초안과 보기 기록을 보존한다', () => {
  const c = client(undefined, noChatTimers); c.context.navigator = { onLine: false };
  const human = recentSession('human', { initiator: 'human' }), agent = recentSession('agent', { initiator: 'agent', phase: 'working' });
  setRecentSnapshot(c, [human, agent]);
  c.select(human.key); const draft = c.chatEntry(human); draft.text = '보내지 않은 초안';
  const view = { top: 125, anchor: 'saved-row', pinned: false, chromeVisible: false };
  c.state.conversationViews.set(c.conversationIdentity(human), view);
  c.state.filters.host = 'other'; c.state.filters.initiator = 'human'; c.state.search = '다른 대화'; c.state.phaseFilter = 'needs_input';
  c.select(agent.key);
  assert.equal(c.state.sessionCategory, 'agent'); assert.equal(c.state.selected, agent.key);
  assert.equal(c.state.filters.host, ''); assert.equal(c.state.filters.initiator, ''); assert.equal(c.state.search, ''); assert.equal(c.state.phaseFilter, '');
  assert.equal(c.matches(c.allSessions().find(s => s.key === agent.key), 1100), true);
  c.chatEntry(agent).text = '워커 초안'; c.setSessionCategory('general');
  assert.equal(c.state.selected, null); assert.equal(c.state.detailShown, false);
  assert.equal(byClass(c.get('detail'), 'detail-head').length, 0, '다른 분류의 열린 대화를 남기지 않는다');
  assert.equal(c.chatEntry(agent).text, '워커 초안'); assert.equal(draft.text, '보내지 않은 초안');
  assert.equal(c.state.conversationViews.get(c.conversationIdentity(human)), view);
  c.select(human.key); assert.equal(c.state.sessionCategory, 'general'); assert.equal(c.chatEntry(human).text, '보내지 않은 초안');
});

test('잠금은 분류 선택과 시작 주체 조건을 일반 세션으로 초기화한다', () => {
  const c = client(undefined, noChatTimers);
  c.state.sessionCategory = 'agent'; c.state.filters.initiator = 'agent'; c.showLogin();
  assert.equal(c.state.sessionCategory, 'general'); assert.equal(c.state.filters.initiator, '');
  assert.equal(c.get('session-category-general').attributes['aria-selected'], 'true');
  assert.equal(c.get('session-category-agent-count').textContent, '0');
});


test('closed-session recovery stays explicit, keeps its draft, and plans the same source exactly once', async () => {
  const calls = []; let resolvePlan;
  const c = client(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return new Promise((resolve) => { resolvePlan = resolve; });
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  s.nativeSource = true; s.phase = 'closed'; entry.text = '이어갈 초안';
  entry.data = { route: c.sourceOf(s), phase: 'closed', capability: { supported: false, action: 'resume_session' },
    recovery: { supported: true, action: 'resume_session', closedByBoard: false } };
  c.buildChatComposer(s); c.updateChatComposer(entry);
  assert.equal(c.get('chat-recovery').hidden, false);
  assert.equal(c.get('chat-recover').textContent, '세션 다시 열기');
  assert.equal(c.get('chat-input').disabled, false); assert.equal(c.get('chat-send').disabled, true);
  assert.equal(calls.length, 0, 'reading a closed session never starts a terminal or model turn');
  c.state.route = { host: 'other-device', agent: 'claude', account: 'other-account' };
  const pending = c.get('chat-recover').events.click();
  await c.get('chat-recover').events.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/plan');
  assert.deepEqual(calls[0].body, { source: { host: s.host, agent: s.agent, home: s.home, id: s.id },
    target: { host: s.host, agent: s.agent, account: 'codex:' + s.home }, resume: true });
  resolvePlan({ ok: true, status: 200, json: async () => ({ allowed: true, mode: 'resume', id: 'resume-plan', warnings: [] }) });
  await pending;
  assert.equal(calls.length, 1, 'launch still requires the user confirmation');
  assert.equal(c.get('plan-launch').textContent, '세션 다시 열기');
  assert.equal(entry.text, '이어갈 초안');
});

test('board-terminated or mismatched-route recovery cannot open a session', async () => {
  const calls = []; const c = client(async (url) => { calls.push(url); throw new Error('unexpected request'); }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.buildChatComposer(s);
  entry.data = { route: c.sourceOf(s), phase: 'closed', capability: { supported: false, reason: '웹보드에서 종료한 세션입니다.' },
    recovery: { supported: true, action: 'resume_session', closedByBoard: true } };
  c.updateChatComposer(entry); await c.recoverChatSession(s);
  assert.equal(c.get('chat-recovery').hidden, true); assert.equal(c.get('chat-input').disabled, true);
  assert.match(c.get('chat-status').textContent, /웹보드에서 종료/);
  entry.data.recovery.closedByBoard = false; entry.data.route = { ...entry.data.route, home: '.codex-other' };
  c.updateChatComposer(entry); await c.recoverChatSession(s);
  assert.equal(c.get('chat-recovery').hidden, true); assert.equal(calls.length, 0);
});

test('offline recovery rechecks only its host once and never resumes from a cached capability', async () => {
  const calls = []; let resolveRefresh;
  const c = client(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return new Promise((resolve) => { resolveRefresh = resolve; });
  }, noChatTimers);
  const { s, entry } = selectChat(c);
  c.state.snapshot.hosts[0].ok = false;
  entry.data.recovery = { supported: true, action: 'resume_session' };
  entry.text = '연결 후 보낼 초안';
  vm.runInContext('poll = async () => {}', c.context);
  c.buildChatComposer(s); c.updateChatComposer(entry);
  assert.equal(c.get('chat-recover').textContent, '기기 연결 다시 확인');
  assert.equal(c.chatSupported(entry), false); assert.equal(c.get('chat-send').disabled, true);
  const pending = c.get('chat-recover').events.click(); await c.get('chat-recover').events.click();
  assert.deepEqual(calls, [{ url: '/api/refresh', body: { host: s.host } }]);
  resolveRefresh({ ok: true, status: 200, json: async () => ({ ok: true }) }); await pending;
  assert.equal(entry.text, '연결 후 보낼 초안'); assert.equal(calls.length, 1);
  c.state.offline = true; await c.recoverChatSession(s); assert.equal(calls.length, 1);
});

test('external sessions expose management beside the conversation and Windows input follows server capability', () => {
  const c = client(undefined, noChatTimers); const { s, entry } = selectChat(c);
  s.source = 'vscode'; s.readOnly = true; s.runtimeKnown = false;
  c.drawDetail(s); c.updateChatComposer(entry);
  assert.equal(c.get('detail-manage-session').textContent, '세션 관리');
  vm.runInContext('manageSession = (s) => { globalThis.managedSource = sourceOf(s); }', c.context);
  c.get('detail-manage-session').events.click({ currentTarget: c.get('detail-manage-session') });
  assert.deepEqual({ ...c.context.managedSource }, { host: s.host, agent: s.agent, home: s.home, id: s.id });
  assert.equal(c.get('chat-input').disabled, false, 'snapshot read-only does not override live canSend');
  entry.data.capability.supported = false; c.updateChatComposer(entry);
  assert.equal(c.get('chat-input').disabled, true);
});

const ONE_SHOT_HISTORY = '대화 기록을 저장하지 않는 일회성 실행입니다. 실행 상태를 확인하고 결과는 요청한 에이전트에서 확인해 주세요.';
const ONE_SHOT_INPUT = '일회성 실행에는 후속 메시지를 보낼 수 없습니다. 결과는 요청한 에이전트에서 확인해 주세요.';
function selectWindowsOneShot(c) {
  const s = recentSession('claude-oneshot-4102', { host: 'windows-pc', hostLabel: 'Windows PC', agent: 'claude', home: '',
    executionMode: 'one-shot', transcriptAvailability: 'disabled', initiator: 'agent', initiatorName: 'Codex', phase: 'working' });
  c.state.snapshot = { serverTime: 1100, hosts: [{ name: s.host, label: s.hostLabel, ok: true, data: { claude: [s] } }] };
  c.state.selected = s.key; c.state.authenticated = true;
  const entry = c.chatEntry(s);
  entry.data = { route: c.sourceOf(s), executionMode: s.executionMode, transcriptAvailability: s.transcriptAvailability,
    capability: { supported: false, reason: ONE_SHOT_INPUT }, phase: 'working', messages: [],
    historyNotice: { code: 'not_persisted', message: ONE_SHOT_HISTORY } };
  c.get('chat-composer').dataset.source = c.chatKey(entry.source);
  return { s, entry };
}
function assertOneShotHistory(c) {
  const messages = c.get('messages'), notices = byClass(messages, 'conversation-history-notice');
  assert.equal(notices.length, 1);
  assert.match(textOf(notices[0]), /일회성 실행 · 기록 저장 안 함/);
  assert.ok(textOf(notices[0]).includes(ONE_SHOT_HISTORY));
  for (const className of ['conversation-read-error', 'group-empty', 'conversation-loading', 'msg-working'])
    assert.equal(byClass(messages, className).length, 0, className);
}

test('Windows 일회성 Claude는 에이전트 분류·기기·호출자를 유지하고 정상 chat/state에서 기록 미저장 안내를 표시한다', async () => {
  const calls = []; let stateData;
  const c = client(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    assert.equal(url, '/api/chat/state');
    return { ok: true, status: 200, json: async () => stateData };
  }, noChatTimers);
  const { s, entry } = selectWindowsOneShot(c);
  stateData = entry.data; entry.data = null;
  assert.equal(c.sessionCategoryOf(s), 'agent');
  c.renderSessionCategories([s], 1100); c.renderGroups([s], 1100);
  assert.equal(c.get('session-category-agent-count').textContent, '1');
  assert.equal(c.get('session-category-general-count').textContent, '0');
  const card = byClass(c.get('groups'), 'card')[0];
  assert.match(textOf(card), /Windows PC/); assert.match(textOf(card), /Codex/);
  assert.equal(byClass(card, 'card-execution')[0].hidden, false);
  c.drawDetail(s); await c.loadDetail(s, true);
  assertOneShotHistory(c);
  assert.match(textOf(c.get('detail')), /일회성 실행/);
  assert.match(textOf(c.get('detail')), /에이전트 시작 · Codex/);
  assert.equal(c.get('chat-status').textContent, ONE_SHOT_INPUT);
  assert.equal(c.get('chat-input').disabled, true);
  assert.deepEqual(calls, [{ url: '/api/chat/state', body: { source: { host: 'windows-pc', agent: 'claude', id: s.id } } }]);
  assert.equal(entry.source.id, 'claude-oneshot-4102');
  assert.equal(entry.data.route.id, entry.source.id);
});

test('일회성 Claude의 /api/read fallback도 정상 미저장 안내를 표시하고 일반 세션으로 바꾸면 안내를 제거한다', async () => {
  const calls = []; let readData;
  const c = client(async (url) => {
    calls.push(url);
    return url === '/api/chat/state'
      ? { ok: false, status: 404, json: async () => ({ error: '채팅 상태 경로 없음' }) }
      : { ok: true, status: 200, json: async () => readData };
  }, noChatTimers);
  const selected = selectWindowsOneShot(c);
  readData = { ...selected.entry.data, historyNotice: { code: 'not_persisted', message: ONE_SHOT_HISTORY + ' 수집기 확인 안내입니다.' } };
  selected.entry.data = null;
  c.drawDetail(selected.s); await c.loadDetail(selected.s, true);
  assert.deepEqual(calls.map((url) => url.split('?')[0]), ['/api/chat/state', '/api/read']);
  const query = new URL(calls[1], 'https://fixture.invalid').searchParams;
  assert.equal(query.get('host'), 'windows-pc'); assert.equal(query.get('agent'), 'claude');
  assert.equal(query.get('id'), 'claude-oneshot-4102');
  assertOneShotHistory(c);
  assert.match(textOf(c.get('messages')), /수집기 확인 안내입니다/);
  assert.equal(selected.entry.error, '', '읽기 성공 뒤 chat/state fallback 오류를 입력 안내에 남기지 않는다');
  const messages = c.get('messages');
  const ordinary = selectChat(c, { agent: 'claude', home: '', id: 'ordinary-claude-session' });
  c.renderMessages(ordinary.s, [{ role: 'assistant', text: '일반 세션의 저장된 대화' }]);
  assert.equal(c.get('messages'), messages, '같은 목록 DOM에서 이전 안내가 제거된다');
  assert.equal(byClass(messages, 'conversation-history-notice').length, 0);
  assert.match(textOf(messages), /일반 세션의 저장된 대화/);
  assert.doesNotMatch(textOf(messages), /기록 저장 안 함|결과는 요청한 에이전트/);
  assert.equal(calls.length, 2);
});

test('일회성 Claude는 남은 전송·터미널 capability도 거부하고 세션 관리의 원본 식별자는 유지한다', async () => {
  const calls = [];
  const c = client(async (url) => { calls.push(url); throw new Error('일회성 실행은 입력·터미널 요청을 보내지 않는다'); }, noChatTimers);
  const { s, entry } = selectWindowsOneShot(c);
  entry.data.capability = { supported: true, attachments: true, action: 'open_terminal' };
  entry.text = '보내지 않을 초안';
  c.drawDetail(s); c.updateChatComposer(entry);
  const { terminalRequired } = vm.runInContext('({ terminalRequired })', c.context);
  assert.equal(c.chatSupported(entry), false); assert.equal(c.chatAttachmentsSupported(entry), false);
  assert.equal(terminalRequired(entry), false);
  assert.equal(c.get('chat-input').disabled, true); assert.equal(c.get('chat-send').disabled, true);
  assert.equal(c.get('chat-attach').disabled, true); assert.equal(c.get('chat-terminal-gate').hidden, true);
  assert.equal(c.get('chat-terminal').disabled, true);
  assert.equal(c.get('chat-status').textContent, ONE_SHOT_INPUT);
  await c.sendChat(entry); c.openChatTerminal(s);
  c.state.route = { host: s.host, agent: s.agent, account: 'claude:default' };
  await c.preparePlan();
  assert.equal(calls.length, 0); assert.equal(entry.text, '보내지 않을 초안');
  assert.equal(c.get('plan-dialog').open, undefined);
  const manage = c.get('detail-manage-session');
  assert.equal(manage.textContent, '세션 관리'); assert.equal(manage.disabled, false);
  vm.runInContext('manageSession = (s) => { globalThis.managedSource = sourceOf(s); }', c.context);
  manage.events.click({ currentTarget: manage });
  assert.deepEqual({ ...c.context.managedSource }, { host: 'windows-pc', agent: 'claude', id: s.id });
});

test('저장된 일회성 대화는 이전 미저장 안내보다 우선하며 disabled 메타데이터만으로 안내를 만들지 않는다', async () => {
  for (const fallback of [false, true]) {
    let saved;
    const c = client(async (url) => {
      if (fallback && url === '/api/chat/state') return { ok: false, status: 404, json: async () => ({ error: '상태 경로 없음' }) };
      return { ok: true, status: 200, json: async () => saved };
    }, noChatTimers);
    const { s, entry } = selectWindowsOneShot(c);
    s.historyNotice = { code: 'not_persisted', message: ONE_SHOT_HISTORY };
    c.drawDetail(s);
    entry.conversation = { identity: c.conversationIdentity(s), authEpoch: c.state.authEpoch,
      messages: [], historyNotice: s.historyNotice, executionMode: 'one-shot', transcriptAvailability: 'disabled' };
    c.renderMessages(s, []); assertOneShotHistory(c);
    saved = { route: c.sourceOf(s), phase: 'idle', executionMode: 'one-shot', transcriptAvailability: 'disabled',
      messages: [{ role: 'assistant', text: '이전에 저장된 실제 대화입니다.' }], capability: { supported: false, reason: ONE_SHOT_INPUT } };
    await c.loadDetail(s, true);
    assert.match(textOf(c.get('messages')), /이전에 저장된 실제 대화/);
    assert.equal(byClass(c.get('messages'), 'conversation-history-notice').length, 0, `저장 본문 우선, fallback=${fallback}`);
    assert.equal(entry.conversation.historyNotice, null);
    assert.equal(entry.error, '', '일회성 기록 읽기가 성공하면 실패한 상태 경로의 오류를 지운다');
    c.renderMessages(s, []);
    assert.equal(byClass(c.get('messages'), 'conversation-history-notice').length, 0, '새 읽기의 명시 안내 부재가 이전 snapshot·cache 안내를 무효화한다');
    assert.equal(c.chatSupported(entry), false, '본문 유무와 일회성 입력 제한은 별개다');
  }
});

test('보드 표시명은 목록·검색에 반영되고 원본 제목·세션 경로는 보존한다', () => {
  const c = client(undefined, noChatTimers), now = Math.floor(Date.now() / 1000);
  const original = { id: 'label-native-id', agent: 'codex', home: '.codex-fixture', name: 'native-runtime-name',
    cwd: '/fixture/original', title: '원본 찾기', originalTitle: '원본 찾기', displayTitle: '보드 별칭 찾기',
    titleAlias: '보드 별칭 찾기', phase: 'working', updatedAt: now, initiator: 'agent' };
  c.state.snapshot = { sessionLabels: { ok: true, revision: 4 }, hosts: [{ name: 'local', label: '합성 PC', ok: true, data: { codex: [original] } }] };
  c.state.sessionCategory = 'agent';
  const row = c.allSessions()[0];
  assert.equal(row.title, '보드 별칭 찾기');
  assert.equal(row.originalTitle, '원본 찾기');
  for (const field of ['id', 'name', 'cwd', 'home']) assert.equal(row[field], original[field]);
  assert.equal(original.title, '원본 찾기', 'snapshot native title remains untouched');
  for (const query of ['보드 별칭', '원본 찾기']) { c.state.search = query; assert.equal(c.matches(row, now), true); }
  let decorated;
  c.context.window.SessionholicLabels = { decorate: (value, metadata) => {
    decorated = { value, metadata }; return { ...value, displayTitle: '저장 직후 제목' };
  } };
  assert.equal(c.allSessions()[0].title, '저장 직후 제목');
  assert.equal(decorated.value.host, 'local', 'identity is complete before applying the saved alias');
  assert.equal(decorated.metadata.revision, 4);
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.ok(html.indexOf('src="/session_labels.js"') < html.indexOf('src="/app.js"'), 'module loads before app initialization');
});

// 음성 입력: the browser's own recognition, faked here. Like a browser, stop() and abort()
// end it a moment later (abort with an "aborted" error first).
function dictationClient() {
  const calls = [];
  const timeouts = [];
  const c = client(async (url, opts) => {
    calls.push(url);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, { setTimeout: (callback, ms) => { timeouts.push({ callback, ms }); return timeouts.length; }, clearTimeout() {} });
  const intervals = new Map();
  let nextInterval = 0;
  c.context.setInterval = (callback) => { intervals.set(++nextInterval, callback); return nextInterval; };
  c.context.clearInterval = (id) => { intervals.delete(id); };
  c.context.Event = class { constructor(type) { this.type = type; } };
  const recognitions = [];
  class FakeRecognition {
    constructor() { recognitions.push(this); this.started = 0; this.stopped = 0; this.aborted = 0; }
    start() { this.started++; }
    stop() { this.stopped++; queueMicrotask(() => this.onend?.()); }
    abort() { this.aborted++; queueMicrotask(() => { this.onerror?.({ error: 'aborted' }); this.onend?.(); }); }
  }
  const result = (...texts) => ({ results: texts.map((text) => [{ transcript: text }]) });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  const tick = (times = 1) => { for (let i = 0; i < times; i++) for (const callback of [...intervals.values()]) callback(); };
  const stopTimer = () => timeouts.filter((timer) => timer.ms === 3000).at(-1);
  return { c, calls, intervals, tick, recognitions, FakeRecognition, result, settle, stopTimer };
}

test('a browser without speech recognition shows no mic', () => {
  const { c } = dictationClient();
  const { s } = selectChat(c);
  const composer = c.buildChatComposer(s);
  assert.equal(findId(composer, 'chat-mic').hidden, true);
  c.renderDictation();
  assert.equal(c.get('draft-mic').hidden, true);
  c.state.terminal = { id: 'term-1' };
  c.toggleDraftDictation();
  assert.equal(c.state.dictation, null);
});

test('the chat mic writes what the device hears at the cursor in Korean, live, and never sends it', async () => {
  const { c, calls, intervals, recognitions, FakeRecognition, result, settle } = dictationClient();
  c.context.webkitSpeechRecognition = FakeRecognition;
  const { s, entry } = selectChat(c);
  entry.text = '앞 뒤';
  const composer = c.buildChatComposer(s);
  const input = findId(composer, 'chat-input');
  const mic = findId(composer, 'chat-mic');
  assert.equal(mic.hidden, false);
  assert.equal(mic.attributes['aria-label'], '음성 입력');
  input.selectionStart = input.selectionEnd = 2;
  let kept = 0;
  c.context.document.activeElement = input;
  mic.events.pointerdown({ preventDefault() { kept++; } });
  mic.events.mousedown({ preventDefault() { kept++; } });
  assert.equal(kept, 2, 'a focused input keeps its keyboard, for a touch\'s mousedown too');
  mic.events.click();
  const [rec] = recognitions;
  assert.equal(rec.started, 1);
  assert.deepEqual([rec.lang, rec.continuous, rec.interimResults], ['ko-KR', true, true]);
  assert.equal(mic.attributes['aria-pressed'], 'true');
  assert.equal(mic.attributes['aria-label'], '음성 입력', 'the label stays; aria-pressed says it listens');
  assert.equal(c.get('chat-status').textContent, c.DICTATION_PREPARING, 'until the recognition says it started');
  rec.onstart();
  assert.equal(c.get('chat-status').textContent, c.DICTATION_LISTENING);
  rec.onresult(result('안녕하세요'));
  assert.equal(input.value, '앞 안녕하세요 뒤', 'at the cursor, spaced from the words around it');
  assert.equal(entry.text, input.value, 'the draft follows as typed text would');
  assert.equal(c.get('chat-status').textContent, c.DICTATION_LISTENING, 'words landing keep the listening line');
  rec.onresult(result('안녕하세요', ' 오늘   날씨'));
  assert.equal(input.value, '앞 안녕하세요 오늘 날씨 뒤');
  // Said twice on purpose: both stay (only Android's cumulative results are merged).
  rec.onresult(result('네', '네 알겠습니다'));
  assert.equal(input.value, '앞 네 네 알겠습니다 뒤');
  // An input method re-marking the landed words (no change to the text) stops nothing.
  input.events.compositionstart();
  input.events.input();
  input.events.compositionend();
  assert.equal(c.state.dictation?.field, input);
  // Pressed again: the last words still land, then it ends quietly.
  mic.events.click();
  assert.equal(rec.stopped, 1);
  rec.onresult(result('네', '네 알겠습니다 고마워요'));
  await settle();
  assert.equal(input.value, '앞 네 네 알겠습니다 고마워요 뒤');
  assert.equal(c.state.dictation, null);
  assert.equal(entry.dictationNotice, '');
  assert.equal(mic.attributes['aria-pressed'], 'false');
  assert.equal(intervals.size, 0, 'no check left running');
  assert.equal(calls.filter((url) => url === '/api/chat/send').length, 0, 'dictation never sends');
});

test('typing, a paste, sending, an error or the input leaving the screen ends the chat dictation', async () => {
  const { c, intervals, tick, recognitions, FakeRecognition, result, settle, stopTimer } = dictationClient();
  c.context.webkitSpeechRecognition = FakeRecognition;
  const { s, entry } = selectChat(c);
  const composer = c.buildChatComposer(s);
  const input = findId(composer, 'chat-input');
  const mic = findId(composer, 'chat-mic');
  const begin = () => { mic.events.click(); const rec = recognitions.at(-1); rec.onstart(); return rec; };
  // The user types: their words win and nothing more lands.
  let rec = begin();
  rec.onresult(result('하나'));
  input.value = '하나 직접';
  input.events.input();
  assert.equal(rec.aborted, 1);
  assert.equal(c.state.dictation, null);
  rec.onresult(result('하나 둘'));
  assert.equal(input.value, '하나 직접');
  // Text put in by other means (a clipboard paste, a restored message) is kept: the next words stop.
  rec = begin();
  rec.onresult(result('둘'));
  input.value = `${input.value} 붙인 글`;
  rec.onresult(result('둘 셋'));
  assert.equal(input.value, '하나 직접 둘 붙인 글');
  assert.equal(rec.aborted, 1);
  // Sending: what is sent is the draft as it is; later words never land after it.
  rec = begin();
  rec.onresult(result('보낼 말'));
  entry.text = input.value;
  await c.sendChat(entry);
  assert.equal(rec.aborted, 1);
  assert.equal(c.state.dictation, null);
  await settle();
  // The fake send leaves no confirmed route: the session takes input again for the rest.
  entry.pending = null; entry.sending = false; entry.outbox = []; entry.receipt = null;
  entry.data = { capability: { supported: true }, phase: 'idle', route: c.sourceOf(s) };
  c.updateChatComposer(entry);
  assert.equal(mic.disabled, false);
  // The mic, like editing the draft, answers an input error about it.
  entry.error = entry.inputError = '메시지는 32KiB까지';
  rec = begin();
  assert.equal(entry.error, '');
  // Another error: the dictation's line leads while it listens; once it ends, the error does
  // (a send's failure is never hidden by an earlier dictation note).
  entry.error = '상태 오류';
  c.updateChatComposer(entry);
  assert.equal(c.get('chat-status').textContent, c.DICTATION_LISTENING);
  rec.onerror({ error: 'network' });
  rec.onend();
  assert.equal(c.get('chat-status').textContent, '상태 오류');
  assert.match(entry.dictationNotice, /\(network\)/);
  entry.error = '';
  c.updateChatComposer(entry);
  assert.match(c.get('chat-status').textContent, /\(network\)/, 'then said, once nothing else is');
  // Errors say why, with their code.
  for (const [code, words] of [['not-allowed', /마이크 사용이 허용되지 않았습니다.*\(not-allowed\)/],
    ['service-not-allowed', /이 화면에서는 음성 인식을 쓸 수 없습니다.*\(service-not-allowed\)/],
    ['network', /음성 인식 서비스에 연결/], ['strange', /음성 입력을 마치지 못했습니다.*\(strange\)/]]) {
    rec = begin();
    rec.onerror({ error: code });
    rec.onend();
    assert.match(c.get('chat-status').textContent, words, code);
  }
  // A send clears what a finished dictation said.
  entry.text = input.value = '보낼 초안';
  await c.sendChat(entry);
  assert.equal(entry.dictationNotice, '');
  await settle();
  entry.pending = null; entry.sending = false; entry.outbox = []; entry.receipt = null;
  entry.data = { capability: { supported: true }, phase: 'idle', route: c.sourceOf(s) };
  c.updateChatComposer(entry);
  // An error the engine reports without ending (seen in WebKit): said with its code on the next check.
  mic.events.click();
  rec = recognitions.at(-1);
  rec.onerror({ error: 'not-allowed' });
  tick();
  assert.equal(rec.aborted, 1);
  assert.match(entry.dictationNotice, /\(not-allowed\)/);
  // A Korean composition never locks a listening mic.
  rec = begin();
  input.events.compositionstart();
  c.updateChatComposer(entry);
  assert.equal(mic.disabled, false);
  mic.events.click();
  assert.equal(rec.stopped, 1);
  input.events.compositionend();
  await settle();
  // Words, then 10 seconds of silence: it ends and says so.
  rec = begin();
  rec.onresult(result('잠깐'));
  tick(10);
  await settle();
  assert.equal(c.state.dictation, null);
  assert.equal(entry.dictationNotice, c.DICTATION_SILENCE_END);
  // The engine answering our stop with "aborted" is a plain end.
  rec = begin();
  rec.onresult(result('그만'));
  mic.events.click();
  rec.onerror({ error: 'aborted' });
  rec.onend();
  assert.equal(entry.dictationNotice, '');
  // Nothing heard after it listened a while.
  rec = begin();
  tick();
  rec.onend();
  assert.match(entry.dictationNotice, /말소리를 듣지 못했습니다/);
  // Ended at once, before any check, nothing heard (as a home-screen app may): said so.
  mic.events.click();
  recognitions.at(-1).onend();
  assert.equal(entry.dictationNotice, `${c.DICTATION_ENDED_AT_ONCE} (end)`);
  // The platform aborting: at once, or after words (kept).
  rec = begin();
  rec.onerror({ error: 'aborted' });
  rec.onend();
  assert.equal(entry.dictationNotice, `${c.DICTATION_ENDED_AT_ONCE} (aborted)`);
  rec = begin();
  rec.onresult(result('남는 말'));
  rec.onerror({ error: 'aborted' });
  rec.onend();
  assert.equal(entry.dictationNotice, `${c.DICTATION_INTERRUPTED} (aborted)`);
  assert.match(input.value, /남는 말/);
  // Never started within 15 seconds (no start event): aborted, and said so.
  mic.events.click();
  rec = recognitions.at(-1);
  tick(14);
  assert.equal(c.state.dictation?.recognition, rec);
  tick();
  assert.equal(rec.aborted, 1);
  assert.equal(entry.dictationNotice, c.DICTATION_NOT_STARTED, 'nothing heard: it never started');
  // Silent for 10 seconds after it started: it stops; a recognition that no longer answers is aborted.
  rec = begin();
  rec.stop = function () { this.stopped++; };
  rec.onresult(result('말하다'));
  tick(9);
  assert.equal(rec.stopped, 0);
  tick();
  assert.equal(rec.stopped, 1);
  stopTimer().callback();
  assert.equal(rec.aborted, 1);
  assert.equal(entry.dictationNotice, c.DICTATION_STALLED);
  assert.match(input.value, /말하다/, 'what it heard stays');
  // Pressed again while it ends: it ends now.
  rec = begin();
  rec.stop = function () { this.stopped++; };
  mic.events.click();
  mic.events.click();
  assert.deepEqual([rec.stopped, rec.aborted], [1, 1]);
  assert.equal(c.state.dictation, null);
  // The conversation leaves the screen (a terminal opens over it): the mic closes, quietly.
  rec = begin();
  c.state.terminalVisible = true;
  tick();
  assert.equal(rec.aborted, 1);
  assert.equal(c.state.dictation, null);
  rec.onresult(result('늦은 말'));
  await settle();
  assert.doesNotMatch(input.value, /늦은 말/);
  assert.equal(entry.dictationNotice, '');
  c.state.terminalVisible = false;
  // A start the browser refuses ends at once.
  c.context.webkitSpeechRecognition = class extends FakeRecognition { start() { throw new Error('busy'); } };
  mic.events.click();
  assert.equal(c.state.dictation, null);
  assert.match(entry.dictationNotice, /음성 입력을 마치지 못했습니다.*\(start\)/);
  await settle();
  assert.equal(intervals.size, 0, 'no check left running');
});

test('the terminal input mic dictates into that terminal draft and stops for a paste or another terminal', async () => {
  const { c, intervals, tick, recognitions, FakeRecognition, result, settle } = dictationClient();
  c.context.webkitSpeechRecognition = FakeRecognition;
  c.renderDictation();
  assert.equal(c.get('draft-mic').hidden, false);
  c.state.terminal = { id: 'term-1' };
  c.state.terminalVisible = true;
  const draft = c.get('terminal-draft');
  c.bindDraftInput(draft);
  draft.value = 'git';
  c.toggleDraftDictation();
  assert.equal(recognitions[0].started, 1);
  assert.equal(c.get('draft-note').textContent, c.DICTATION_PREPARING);
  recognitions[0].onaudiostart();
  assert.equal(c.get('draft-note').textContent, c.DICTATION_LISTENING);
  assert.equal(c.get('draft-mic').attributes['aria-pressed'], 'true');
  recognitions[0].onresult(result('상태 보여 줘'));
  assert.equal(draft.value, 'git 상태 보여 줘');
  assert.equal(c.state.drafts.get(c.draftKey(c.state.terminal)), 'git 상태 보여 줘', 'saved as the terminal draft');
  assert.equal(c.state.dictation?.field, draft, 'its own words do not stop it');
  c.toggleDraftDictation();
  assert.equal(recognitions[0].stopped, 1);
  await settle();
  assert.equal(c.get('draft-note').textContent, '');
  // A paste takes the draft as it is.
  c.toggleDraftDictation();
  c.state.connected = false;
  c.pasteDraft(true);
  assert.equal(recognitions[1].aborted, 1);
  // Another terminal on screen: this draft no longer listens.
  c.toggleDraftDictation();
  c.state.terminal = { id: 'term-2' };
  recognitions[2].onresult(result('다른 터미널'));
  assert.equal(recognitions[2].aborted, 1);
  assert.doesNotMatch(draft.value, /다른 터미널/);
  // Back on the first terminal within a second, its input replaced: the old one never listens on.
  c.state.terminal = { id: 'term-1' };
  c.toggleDraftDictation();
  c.state.terminal = { id: 'term-2' };
  c.state.terminal = { id: 'term-1' };
  c.nodes.set('terminal-draft', { ...draft, value: '' });
  tick();
  assert.equal(recognitions[3].aborted, 1);
  // While a Korean composition or a paste is in progress it does not start, and says why.
  c.nodes.set('terminal-draft', draft);
  c.state.composing = true;
  c.toggleDraftDictation();
  assert.equal(recognitions.length, 4);
  assert.match(c.get('draft-note').textContent, /한글 조합을 마친 뒤/);
  c.state.composing = false;
  c.state.draftPaste = { id: 'term-1' };
  c.toggleDraftDictation();
  assert.equal(recognitions.length, 4);
  assert.match(c.get('draft-note').textContent, /붙여넣는 중/);
  c.state.draftPaste = null;
  c.toggleDraftDictation();
  c.state.terminalVisible = false;
  tick();
  assert.equal(recognitions[4].aborted, 1, 'leaving the terminal closes the mic');
  await settle();
  assert.equal(intervals.size, 0);
});

test('what was heard is joined in order; only Android\'s cumulative results replace the earlier words', () => {
  const { c } = dictationClient();
  const results = (...texts) => texts.map((text) => [{ transcript: text }]);
  const android = { cumulative: true };
  assert.equal(c.dictationText(results('안녕', ' 하세요 '), { cumulative: false }), '안녕 하세요');
  assert.equal(c.dictationText(results('네', '네 알겠습니다'), { cumulative: false }), '네 네 알겠습니다');
  assert.equal(c.dictationText(results('1', '10번 줄'), android), '1 10번 줄', 'a word boundary, not a prefix');
  assert.equal(c.dictationText(results('아', '아니요'), android), '아 아니요');
  assert.equal(c.dictationText(results('오늘', '오늘 날씨', '오늘 날씨 어때'), android), '오늘 날씨 어때');
  assert.equal(c.dictationText(results('안녕하세요', '오늘', '안녕하세요 오늘 날씨'), android), '안녕하세요 오늘 날씨');
  assert.equal(c.dictationText(results('오늘 날씨', '오늘날씨 어때'), android), '오늘날씨 어때', 'spacing corrected');
  assert.equal(c.dictationText(results('안녕하세요.', '안녕하세요 오늘'), android), '안녕하세요 오늘', 'punctuation aside');
  assert.equal(c.dictationText(results('Hello', 'hello world'), android), 'hello world');
  assert.equal(c.dictationText(results('', '  ', '끝')), '끝');
  assert.equal(c.dictationText(undefined), '');
  c.context.navigator = { userAgent: 'Mozilla/5.0 (Linux; Android 14; SM-F741N) Chrome/141' };
  assert.equal(c.dictationText(results('오늘', '오늘 날씨')), '오늘 날씨');
  c.context.navigator = { userAgent: 'Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) Safari/605.1.15' };
  assert.equal(c.dictationText(results('오늘', '오늘 날씨')), '오늘 오늘 날씨');
});
