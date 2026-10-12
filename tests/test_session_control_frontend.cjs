'use strict';
// Execute the production transport and session-control functions with inert DOM/fetch.
// No browser, server, native process, SSH or shared frontend harness mutation.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function cut(start, end) {
  const a = app.indexOf(start), b = app.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `production boundary: ${start}`);
  return app.slice(a, b);
}
const production = cut('function assertAuthEpoch(', '\nfunction allSessions(')
  + cut('function sourceOf(', '\nfunction accountOf(')
  + cut('// Original execution controls are separate', '\nfunction focusDetailBack(');
const source = (extra = {}) => ({ host: 'workstation', hostLabel: '합성 기기', agent: 'codex', home: '.codex-isolated', id: 'synthetic-one', title: '합성 작업', ...extra });
const preview = (extra = {}) => ({ targetProof: 'synthetic-proof', capabilities: { interrupt: { supported: true }, terminate: { supported: true } }, ...extra });
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function client(fetchImpl) {
  const nodes = new Map(), calls = [], counts = { refresh: 0, poll: 0, capabilities: 0, login: 0 };
  let request = 0, methods;
  function el(tag, className = '', textContent = '') {
    const value = { tagName: tag.toUpperCase(), className, textContent, children: [], events: {}, disabled: false, hidden: false, open: false,
      append(...items) { for (const item of items) { item.parentNode = this; this.children.push(item); } },
      replaceChildren(...items) { this.children = []; this.append(...items); },
      replaceWith(next) { const parent = this.parentNode; parent.children.splice(parent.children.indexOf(this), 1, next); next.parentNode = parent; },
      querySelectorAll(selector) { return this.children.flatMap((child) => [child, ...child.querySelectorAll(selector)]).filter((item) => item.tagName.toLowerCase() === selector); },
      setAttribute() {}, addEventListener(type, callback) { this.events[type] = callback; },
      close() { this.open = false; }, focus() { this.focusCount = (this.focusCount || 0) + 1; },
    };
    Object.defineProperty(value, 'id', { get() { return this._id; }, set(id) { this._id = id; nodes.set(id, this); } });
    return value;
  }
  const state = { authenticated: true, authEpoch: 1, csrf: 'synthetic-csrf', pollBusy: false, terminalsRefreshing: false };
  const context = vm.createContext({ console, state, el, $: (id) => nodes.get(id),
    document: { body: el('body') }, window: { SessionholicGoals: { async refresh() { counts.refresh++; } } },
    button(text, className, callback) { const value = el('button', className, text); value.events.click = callback; return value; },
    cardTitle: (s) => s.title, sourceOf: undefined, botRequestForNative: () => null,
    sheet: (id) => ({ open() { nodes.get(id).open = true; } }), requestId: () => `synthetic-request-${++request}`,
    poll() { counts.poll++; }, loadCapabilities: async () => { counts.capabilities++; },
    showLogin() { counts.login++; state.authenticated = false; state.authEpoch++; methods.closeSessionControl(true); },
    fetch: async (url, options) => { const call = { url, body: options.body && JSON.parse(options.body), options }; calls.push(call); return fetchImpl(call, calls); },
  });
  vm.runInContext(production, context);
  methods = vm.runInContext('({manageSession,closeSessionControl,beginAuthEpoch,confirmSessionAction,executeSessionAction,archiveSessionWork,current:()=>sessionControl})', context);
  return { ...methods, state, calls, counts, get: (id) => nodes.get(id) };
}

test('관리 미리보기는 정확한 기기·계정·세션을 보내고 지원하지 않는 제어를 비활성화한다', async () => {
  const c = client(async () => response(preview({ capabilities: { interrupt: { supported: false, reason: '지원하지 않는 실행' }, terminate: { supported: true } } })));
  await c.manageSession(source());
  assert.deepEqual(c.calls[0].body.source, { host: 'workstation', agent: 'codex', home: '.codex-isolated', id: 'synthetic-one' });
  assert.equal(c.calls[0].url, '/api/session/preview');
  assert.equal(c.get('session-control-interrupt').disabled, true);
  assert.equal(c.get('session-control-interrupt-reason').textContent, '지원하지 않는 실행');
  c.get('session-control-interrupt').events.click();
  assert.equal(c.get('session-control-confirmation').hidden, true);
  assert.equal(c.calls.length, 1);
});

test('실행 확인 전에는 제어하지 않고 중복 클릭·완료 후 재클릭도 원본에 한 번만 보낸다', async () => {
  const pending = deferred();
  const c = client(async ({ url }) => url.endsWith('/preview') ? response(preview()) : pending.promise);
  const s = source(); await c.manageSession(s);
  c.get('session-control-terminate').events.click();
  assert.equal(c.calls.length, 1, '제어 확인을 눌러야 POST action을 보낸다');
  const confirm = c.get('session-control-confirm');
  s.home = '.codex-other'; s.id = 'other';
  const first = confirm.events.click(); await confirm.events.click();
  assert.equal(c.calls.filter((call) => call.url.endsWith('/action')).length, 1);
  assert.equal(c.calls[1].body.source.home, '.codex-isolated'); assert.equal(c.calls[1].body.source.id, 'synthetic-one');
  assert.equal(c.calls[1].body.targetProof, 'synthetic-proof');
  assert.equal(c.get('session-control-close').disabled, true);
  pending.resolve(response({ confirmed: true })); await first;
  await confirm.events.click();
  assert.equal(c.calls.length, 2); assert.equal(c.get('session-control-terminate').disabled, true);
  assert.match(c.get('session-control-status').textContent, /실행 종료를 확인/);
  assert.equal(c.counts.refresh, 1); assert.equal(c.counts.poll, 1);
});

test('응답 유실은 미확인으로 남기며 같은 관리 화면에서 자동·수동 재전송하지 않는다', async () => {
  const c = client(async ({ url }) => { if (url.endsWith('/preview')) return response(preview()); throw new Error('network lost'); });
  await c.manageSession(source()); c.get('session-control-interrupt').events.click();
  const confirm = c.get('session-control-confirm'); await confirm.events.click(); await confirm.events.click();
  assert.equal(c.calls.filter((call) => call.url.endsWith('/action')).length, 1);
  assert.match(c.get('session-control-status').textContent, /확인하지 못했습니다/);
  assert.match(c.get('session-control-status').textContent, /자동으로 다시 보내지 않습니다/);
  assert.equal(c.counts.poll, 0); assert.equal(c.counts.refresh, 0);
});

test('제어 POST의 CSRF 만료도 capability 재조회나 자동 재요청을 하지 않는다', async () => {
  const c = client(async ({ url }) => url.endsWith('/preview') ? response(preview()) : response({ code: 'csrf_expired', error: 'expired' }, 403));
  await c.manageSession(source()); c.get('session-control-interrupt').events.click(); await c.get('session-control-confirm').events.click();
  assert.equal(c.calls.length, 2); assert.equal(c.counts.capabilities, 0);
  assert.equal(c.get('session-control-interrupt').disabled, true);
});

test('새 관리 화면이 열린 뒤 늦은 이전 미리보기는 대상 증명과 버튼 상태를 덮지 않는다', async () => {
  const late = deferred();
  const c = client(async ({ body }) => body.source.id === 'synthetic-one' ? late.promise : response(preview({ targetProof: 'new-proof' })));
  const first = c.manageSession(source());
  await c.manageSession(source({ id: 'synthetic-two' }));
  late.resolve(response(preview({ targetProof: 'old-proof', capabilities: {} }))); await first;
  assert.equal(c.current().source.id, 'synthetic-two'); assert.equal(c.current().preview.targetProof, 'new-proof');
  assert.equal(c.get('session-control-terminate').disabled, false);
});

test('인증 세대 변경 뒤 늦은 제어 성공은 새 화면에 성공 표시나 후속 새로고침을 하지 않는다', async () => {
  const late = deferred();
  const c = client(async ({ url }) => url.endsWith('/preview') ? response(preview()) : late.promise);
  await c.manageSession(source()); c.get('session-control-terminate').events.click();
  const action = c.get('session-control-confirm').events.click();
  c.beginAuthEpoch(); c.closeSessionControl(true);
  await c.manageSession(source({ id: 'synthetic-two' }));
  const message = c.get('session-control-status').textContent;
  late.resolve(response({ confirmed: true })); await action;
  assert.equal(c.current().source.id, 'synthetic-two'); assert.equal(c.get('session-control-status').textContent, message);
  assert.equal(c.counts.poll, 0); assert.equal(c.counts.refresh, 0);
});

test('401 인증 만료는 관리 화면을 닫고 오래된 제어 결과를 지운다', async () => {
  const c = client(async ({ url }) => url.endsWith('/preview') ? response(preview()) : response({ error: 'expired' }, 401));
  await c.manageSession(source()); c.get('session-control-interrupt').events.click(); await c.get('session-control-confirm').events.click();
  assert.equal(c.state.authenticated, false); assert.equal(c.current(), null); assert.equal(c.get('session-control-dialog').open, false);
  assert.equal(c.calls.length, 2); assert.equal(c.counts.login, 1);
});

test('보관과 되돌리기는 작업 보관 API만 호출하며 실행 중단·종료를 전송하지 않는다', async () => {
  const c = client(async ({ url }) => response(url.endsWith('/preview') ? preview({ capabilities: {} }) : { archived: true, executionChanged: false }));
  await c.manageSession(source()); await c.get('session-control-archive').events.click();
  assert.match(c.get('session-control-status').textContent, /실행은 계속/);
  assert.equal(c.get('session-control-archive').textContent, '보관 되돌리기');
  await c.get('session-control-archive').events.click();
  assert.deepEqual(c.calls.map((call) => call.url), ['/api/session/preview', '/api/session/archive', '/api/session/archive']);
  assert.deepEqual(c.calls.slice(1).map((call) => call.body.archived), [true, false]);
  assert.equal(c.counts.refresh, 2); assert.equal(c.counts.poll, 0);
});


test('세션과 하위 에이전트 종료 범위를 설명과 최종 확인 양쪽에 표시한다', async () => {
  const effect = '선택한 세션과 하위 에이전트를 종료하고 대화 기록을 보관합니다.';
  const c = client(async () => response(preview({ capabilities: { terminate: { supported: true, effect, method: 'codex-thread-archive' } } })));
  await c.manageSession(source({ source: 'vscode' }));
  assert.equal(c.get('session-control-terminate-reason').textContent, effect);
  c.get('session-control-terminate').events.click();
  assert.ok(c.get('session-control-confirmation').children[0].textContent.includes(effect));
  assert.equal(c.calls.length, 1, '범위를 확인하는 동안 실행 요청은 보내지 않는다');
});
