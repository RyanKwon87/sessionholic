"use strict";

// Only the standalone label module is loaded. All sessions and responses are synthetic.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const moduleSource = fs.readFileSync(path.join(__dirname, "../web/session_labels.js"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise(setImmediate);
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function descendants(root) {
  return (root?.children || []).flatMap((child) => [child, ...descendants(child)]);
}
function textOf(root) {
  return [root?.textContent || "", ...(root?.children || []).map(textOf)].join(" ");
}
function client(transport = async () => { throw new Error("Unexpected fixture request"); }, options = {}) {
  const nodes = new Map(), calls = [], changed = [], toasts = [], htmlWrites = [];
  let authEpoch = 7;
  const element = (tag = "div") => {
    const item = {
      tagName: tag.toUpperCase(), children: [], dataset: {}, attributes: {}, events: {}, style: {},
      value: "", textContent: "", className: "", disabled: false, hidden: false, open: false,
      append(...children) {
        for (const child of children) { child.parentNode = this; this.children.push(child); }
      },
      appendChild(child) { this.append(child); return child; },
      replaceChildren(...children) { this.children = []; this.append(...children); },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); },
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; },
      removeAttribute(name) { delete this.attributes[name]; },
      addEventListener(type, callback) { (this.events[type] ||= []).push(callback); },
      async dispatch(type, fields = {}) {
        const event = { target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...fields };
        const callbacks = [...(this.events[type] || []), ...(typeof this[`on${type}`] === "function" ? [this[`on${type}`]] : [])];
        await Promise.all(callbacks.map((callback) => callback(event)));
      },
      querySelectorAll(selector) {
        return descendants(this).filter((child) => selector.startsWith("#") ? child.id === selector.slice(1)
          : selector.startsWith(".") ? String(child.className || "").split(/\s+/).includes(selector.slice(1))
            : child.tagName === selector.toUpperCase());
      },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      contains(child) { return child === this || descendants(this).includes(child); },
      focus() { this.focusCount = (this.focusCount || 0) + 1; }, select() {},
      showModal() { this.open = true; }, close() { this.open = false; },
    };
    Object.defineProperty(item, "id", { get() { return this.elementId; }, set(value) { this.elementId = value; nodes.set(value, this); } });
    Object.defineProperty(item, "innerHTML", { get() { return this.html || ""; }, set(value) { this.html = value; htmlWrites.push(value); } });
    item.classList = {
      add(...names) { item.className = [...new Set([...item.className.split(/\s+/).filter(Boolean), ...names])].join(" "); },
      remove(...names) { item.className = item.className.split(/\s+/).filter((name) => !names.includes(name)).join(" "); },
      contains(name) { return item.className.split(/\s+/).includes(name); },
      toggle(name, on) { if (on ?? !this.contains(name)) this.add(name); else this.remove(name); },
    };
    return item;
  };
  const body = element("body");
  const document = { body, createElement: element, createTextNode: (text) => ({ textContent: text, children: [] }),
    getElementById: (id) => nodes.get(id) || null, addEventListener() {},
    querySelector: (selector) => body.querySelector(selector), querySelectorAll: (selector) => body.querySelectorAll(selector) };
  const context = vm.createContext({ console, document, window: {}, setTimeout, clearTimeout, queueMicrotask });
  vm.runInContext(moduleSource, context);
  const labels = context.window.SessionholicLabels;
  labels.init({
    api: async (url, request) => { calls.push({ url, ...plain(request) }); return transport(url, request); },
    canEdit: options.canEdit || (() => true),
    sourceOf: (s) => ({ host: s.host, agent: s.agent, id: s.id, ...(s.home ? { home: s.home } : {}) }),
    getAuthEpoch: () => authEpoch,
    onChanged: (record) => changed.push(plain(record)),
    openDialog: (dialog) => dialog.showModal(), toast: (message) => toasts.push(message),
  });
  return { labels, calls, changed, toasts, htmlWrites, nodes, body, context,
    get: (id) => nodes.get(id), newEpoch() { authEpoch++; },
    async input(value) { const input = nodes.get("session-label-input"); input.value = value; await input.dispatch("input"); },
    click: (id) => nodes.get(id).dispatch("click"),
  };
}
function session(fields = {}) {
  return { host: "fixture-host", agent: "codex", id: "session-label-1", home: ".codex-fixture",
    title: "원본 제목", originalTitle: "원본 제목", displayTitle: "원본 제목", titleAlias: null,
    name: "runtime-name", cwd: "/fixture/project", ...fields };
}
function source(s) {
  const home = s.home || (s.agent === "claude" ? ".claude" : null);
  return { host: s.host, agent: s.agent, id: s.id || s.sessionId, ...(home ? { home } : {}),
    ...(s.agent === "claude" && s.sessionId ? { sessionId: s.sessionId } : {}) };
}
function record(s, alias) {
  return { source: source(s), titleAlias: alias, originalTitle: s.originalTitle,
    displayTitle: alias ?? s.originalTitle, revision: 2 };
}

// Rendering a changed title must never change routing fields or the native title.
test("별칭은 공백을 정리해 정확한 source로 저장하고 HTML로 해석하지 않으며 목록 확인 전까지 보존한다", async () => {
  const s = session(), before = plain(s), alias = '<img src=x onerror="fixture"> 별칭';
  const c = client(async () => record(s, alias));
  c.labels.open(s);
  await c.input(`  ${alias}  `); await c.click("session-label-save"); await settle();
  assert.deepEqual(c.calls, [{ url: "/api/session/label", method: "POST", body: { source: source(s), title: alias }, authEpoch: 7, csrfRetry: false }]);
  assert.equal(c.changed.length, 1);
  const decorated = c.labels.decorate(s, { ok: true, revision: 1 });
  assert.equal(decorated.titleAlias, alias); assert.equal(decorated.displayTitle, alias);
  assert.equal(decorated.title, s.title); assert.equal(decorated.name, s.name); assert.equal(decorated.cwd, s.cwd);
  assert.equal(decorated.id, s.id); assert.equal(decorated.home, s.home); assert.deepEqual(s, before);
  c.labels.open(decorated);
  assert.equal(c.get("session-label-input").value, alias);
  assert.ok(c.htmlWrites.every((html) => !html.includes(alias)), "별칭은 innerHTML에 넣지 않는다");
  assert.equal(descendants(c.body).filter((node) => node.tagName === "IMG").length, 0);
  assert.equal(c.labels.decorate({ ...s }, { ok: true, revision: 1 }).titleAlias, alias, "이전 revision 목록은 저장된 별칭을 덮지 않는다");
  assert.equal(c.labels.decorate({ ...s, titleAlias: "현재 revision 별칭", displayTitle: "현재 revision 별칭" }, { ok: true, revision: 2 }).titleAlias,
    "현재 revision 별칭", "같은 revision의 서버값도 수용한다");
  assert.equal(c.labels.decorate({ ...s, titleAlias: "다른 기기에서 변경", displayTitle: "다른 기기에서 변경" }, { ok: true, revision: 3 }).titleAlias,
    "다른 기기에서 변경", "같은 revision을 확인한 뒤에는 다음 서버 변경을 따른다");
  assert.equal(c.labels.decorate({ ...s }, { ok: true, revision: 1 }).titleAlias, "다른 기기에서 변경",
    "뒤늦게 도착한 낮은 revision은 마지막으로 확인한 서버값을 덮지 않는다");
  c.labels.locked();
  assert.equal(c.labels.decorate({ ...s }, { ok: true, revision: 1 }).titleAlias, null, "잠금은 확인한 별칭 캐시도 비운다");
});

test("홈 없는 Codex와 편집 불가 봇은 차단하고 Claude 기본 홈과 interactive ID를 정규화한다", async () => {
  for (const s of [session({ agent: "claude", home: undefined }),
    session({ agent: "claude", home: undefined, id: "", kind: "interactive", sessionId: "claude-transcript-1234" })]) {
    const c = client(async () => record(s, "별칭"));
    c.labels.open(s); await c.input("별칭"); await c.click("session-label-save"); await settle();
    assert.deepEqual(c.calls[0].body.source, source(s));
    assert.equal(c.changed.length, 1);
  }
  for (const home of [undefined, null, ""]) {
    const c = client();
    assert.equal(c.labels.open(session({ home })), false, "Codex 계정 홈을 추정하지 않는다");
    assert.equal(c.get("session-label-dialog")?.open || false, false);
    assert.equal(c.calls.length, 0);
  }
  const denied = client(undefined, { canEdit: () => false });
  denied.labels.open(session());
  assert.equal(denied.get("session-label-dialog")?.open || false, false);
  assert.equal(denied.calls.length, 0);
});

test("원래 제목 복원은 null을 한 번 보내며 늦은 별칭 목록에도 복원값을 유지한다", async () => {
  const s = session({ titleAlias: "사용자 별칭", displayTitle: "사용자 별칭" });
  const c = client(async () => record(s, null));
  c.labels.open(s); await c.click("session-label-reset"); await settle();
  assert.equal(c.calls.length, 1); assert.equal(c.calls[0].body.title, null);
  assert.deepEqual(c.calls[0].body.source, source(s));
  const restored = c.labels.decorate(s, { ok: true, revision: 1 });
  assert.equal(restored.titleAlias, null); assert.equal(restored.displayTitle, s.originalTitle);
  assert.equal(restored.title, s.title); assert.equal(s.titleAlias, "사용자 별칭");
  assert.equal(c.labels.decorate({ ...s, titleAlias: "다음 별칭", displayTitle: "다음 별칭" }, { ok: true, revision: 3 }).titleAlias,
    "다음 별칭", "더 높은 revision은 복원 직후의 override도 해제한다");
  assert.equal(c.labels.decorate(s, { ok: true, revision: 1 }).titleAlias, "다음 별칭");
});

test("저장 중 중복 클릭은 한 요청만 보내며 실패한 초안은 유지하고 명시적 재시도로 저장한다", async () => {
  const pending = deferred(), s = session(); let attempts = 0;
  const c = client(async () => ++attempts === 1 ? pending.promise : record(s, "남겨 둔 별칭"));
  c.labels.open(s); await c.input("  남겨 둔 별칭  ");
  const saving = c.click("session-label-save");
  const duplicate = c.click("session-label-save");
  assert.equal(c.calls.length, 1); assert.equal(c.get("session-label-save").disabled, true);
  pending.reject(new Error("합성 연결 실패")); await Promise.all([saving, duplicate]); await settle();
  assert.equal(c.get("session-label-input").value, "  남겨 둔 별칭  ");
  assert.equal(c.get("session-label-dialog").open, true);
  assert.match(c.get("session-label-status").textContent, /합성 연결 실패/);
  assert.equal(c.get("session-label-save").disabled, false);
  assert.equal(c.changed.length, 0); assert.equal(c.calls.length, 1);
  await c.click("session-label-save"); await settle();
  assert.equal(c.calls.length, 2); assert.equal(c.changed.length, 1);
  assert.equal(c.labels.decorate(s).displayTitle, "남겨 둔 별칭");
});

test("잠금 이후의 늦은 성공·실패는 다시 연 다른 세션의 초안·대화상자·별칭을 바꾸지 않는다", async () => {
  for (const succeeds of [true, false]) {
    const pending = deferred(), previous = session(), next = session({ id: "next-session" });
    const c = client(async () => pending.promise);
    c.labels.open(previous); await c.input("이전 인증 별칭");
    const saving = c.click("session-label-save");
    c.labels.locked(); c.newEpoch();
    c.labels.open(next); await c.input("새 인증 초안");
    if (succeeds) pending.resolve(record(previous, "이전 인증 별칭"));
    else pending.reject(new Error("이전 인증 실패"));
    await saving; await settle();
    assert.equal(c.changed.length, 0);
    assert.equal(c.labels.decorate(previous).titleAlias, null);
    assert.equal(c.get("session-label-dialog").open, true);
    assert.equal(c.get("session-label-input").value, "새 인증 초안");
    assert.doesNotMatch(c.get("session-label-status").textContent, /이전 인증/);
  }
});

test("다른 source나 잘못된 응답은 적용하지 않고 160자까지 허용하며 초과 입력은 전송하지 않는다", async () => {
  const s = session();
  for (const badRecord of [
    { ...record(s, "별칭"), source: { ...source(s), host: "other-host" } },
    { ...record(s, "별칭"), source: { ...source(s), home: ".codex-other" } },
    { ...record(s, "별칭"), source: { ...source(s), id: "other-session" } },
    { ...record(s, "별칭"), titleAlias: { invalid: true } },
    { ...record(s, "별칭"), originalTitle: null },
    { ...record(s, "별칭"), displayTitle: [] },
    { ...record(s, "별칭"), revision: "2" },
  ]) {
    const c = client(async () => badRecord);
    c.labels.open(s); await c.input("별칭"); await c.click("session-label-save"); await settle();
    assert.equal(c.changed.length, 0); assert.equal(c.labels.decorate(s).titleAlias, null);
    assert.equal(c.get("session-label-input").value, "별칭");
    assert.equal(c.get("session-label-dialog").open, true);
    assert.ok(c.get("session-label-status").textContent);
  }
  const c = client(async (_url, options) => record(s, options.body.title));
  c.labels.open(s); await c.input("가".repeat(161)); await c.click("session-label-save");
  assert.equal(c.calls.length, 0); assert.equal(c.get("session-label-input").value.length, 161);
  await c.input("가".repeat(160)); await c.click("session-label-save"); await settle();
  assert.equal(c.calls.length, 1); assert.equal(c.changed.length, 1);
  assert.equal(c.calls[0].body.title.length, 160);
});

test("Claude 작업자 ID가 재사용돼도 다른 transcript에는 이전 대화의 별칭을 적용하지 않는다", async () => {
  const previous = session({ agent: "claude", home: ".claude", kind: "background", sessionId: "transcript-a" });
  const next = { ...previous, sessionId: "transcript-b", title: "새 대화 원본 제목", originalTitle: "새 대화 원본 제목",
    displayTitle: "새 대화 원본 제목", titleAlias: null };
  const c = client(async () => record(previous, "이전 대화 별칭"));
  c.labels.open(previous); await c.input("이전 대화 별칭"); await c.click("session-label-save"); await settle();
  assert.deepEqual(c.calls[0].body.source, source(previous), "API 식별자는 작업자 ID·홈과 정확한 대화 UUID를 함께 보낸다");
  assert.equal(c.calls[0].body.source.sessionId, "transcript-a");
  for (const snapshotMeta of [undefined, { ok: true, revision: 1 }]) {
    const decorated = c.labels.decorate(next, snapshotMeta);
    assert.equal(decorated.titleAlias, null);
    assert.equal(decorated.displayTitle, "새 대화 원본 제목");
    assert.equal(decorated.title, "새 대화 원본 제목");
    assert.equal(c.labels.decorate(previous, snapshotMeta).displayTitle, "이전 대화 별칭");
  }
  for (const sessionId of [undefined, "different-transcript"]) {
    const mismatch = record(previous, "적용하면 안 되는 별칭");
    if (sessionId === undefined) delete mismatch.source.sessionId;
    else mismatch.source.sessionId = sessionId;
    const rejected = client(async () => mismatch);
    rejected.labels.open(previous); await rejected.input("적용하면 안 되는 별칭");
    await rejected.click("session-label-save"); await settle();
    assert.equal(rejected.changed.length, 0, "대화 UUID가 없거나 다른 응답은 거부한다");
    assert.equal(rejected.labels.decorate(previous).titleAlias, null);
    assert.equal(rejected.get("session-label-dialog").open, true);
    assert.equal(rejected.get("session-label-input").value, "적용하면 안 되는 별칭");
    assert.ok(rejected.get("session-label-status").textContent);
  }
});
