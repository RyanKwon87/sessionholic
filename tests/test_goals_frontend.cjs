'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  goalMatches, sameSession, sessionRef, originOf, linkedSessions,
  completionError, editable, parentGroups,
} = require('../web/goals.js');
const session = (id, extra = {}) => ({ id, key: `workstation|codex|.codex|${id}`, host: 'workstation', hostLabel: '작업 기기', agent: 'codex', home: '.codex', title: '구현 대화', project: '제품 개선', initiator: 'human', ...extra });
const goal = (extra = {}) => ({ id: 'goal-1', title: '고객 안내문 완성', status: 'todo', completionCriteria: '최종 문구를 확인한다', current: '', next: '', project: '', evidence: '', sessions: [], ...extra });

test('목표 검색이 연결 세션명·프로젝트·기기·시작 주체를 같은 흐름에서 찾는다', () => {
  const s = session('one', { title: '안내문 검토', initiator: 'agent', initiatorName: '검토 담당' });
  const g = goal({ sessions: [sessionRef(s)] });
  for (const query of ['고객', '안내문 검토', '제품 개선', '작업 기기', '에이전트 시작', '검토 담당']) assert.equal(goalMatches(g, query, {}, [s]), true, query);
  assert.equal(goalMatches(g, '다른 목표', {}, [s]), false);
  assert.equal(goalMatches(g, '고객 기기', {}, [s]), true, '공백으로 나눈 검색어를 함께 찾는다');
});

test('여러 세션·여러 목표 연결에서 기기와 시작 주체 필터는 각각의 명시 메타데이터를 사용한다', () => {
  const human = session('human');
  const agent = session('agent', { host: 'laptop', initiator: 'agent' });
  const g = goal({ sessions: [sessionRef(human), sessionRef(agent)] });
  assert.equal(goalMatches(g, '', { origin: 'human' }, [human, agent]), true);
  assert.equal(goalMatches(g, '', { origin: 'agent' }, [human, agent]), true);
  assert.equal(goalMatches(g, '', { host: 'laptop' }, [human, agent]), true);
  assert.equal(goalMatches(g, '', { host: 'other' }, [human, agent]), false);
  assert.equal(linkedSessions(g, [human, agent]).length, 2);
  assert.equal(linkedSessions(goal({ id: 'another', sessions: [sessionRef(human)] }), [human])[0].session.id, human.id);
});

test('출처를 모르는 터미널·CLI·background 세션을 사람 또는 에이전트라고 추측하지 않는다', () => {
  for (const source of ['cli', 'terminal', 'interactive', 'background', 'exec']) {
    assert.equal(originOf({ source }), 'unknown');
    assert.equal(originOf({ source, initiator: 'unknown' }), 'unknown');
  }
  assert.equal(originOf({ initiator: 'human', source: 'exec' }), 'human');
  assert.equal(originOf({ initiator: 'agent', source: 'terminal' }), 'agent');
});

test('세션 identity는 기기·도구·계정 홈·정확한 id 모두를 확인한다', () => {
  const base = sessionRef(session('one'));
  assert.equal(sameSession(base, base), true);
  for (const [key, value] of [['host', 'other'], ['agent', 'claude'], ['id', 'another'], ['home', '.codex-private']]) assert.equal(sameSession(base, { ...base, [key]: value }), false, key);
  assert.equal(sameSession({ ...base, id: '' }, { ...base, id: '' }), false);
  assert.equal(sameSession({ ...base, home: null }, base), true, '서버의 기본 홈 정규화와 일치한다');
  assert.equal(sessionRef({ host: 'device', agent: 'claude', id: 'two' }).home, '.claude');
});

test('기기 오프라인 또는 응답 종료가 목표 상태를 바꾸지 않는다', () => {
  const s = session('one', { stale: true, phase: 'closed' });
  const g = goal({ status: 'in_progress', sessions: [sessionRef(s)] });
  linkedSessions(g, [s]); goalMatches(g, '', {}, [s]);
  assert.equal(g.status, 'in_progress');
  assert.equal(originOf(s), 'human');
});

test('수집 목록에서 사라진 세션도 연결 참조와 근거를 잃지 않는다', () => {
  const ref = { ...sessionRef(session('missing')), point: '첫 번째 검토 답변' };
  const linked = linkedSessions(goal({ sessions: [ref] }), []);
  assert.equal(linked.length, 1); assert.equal(linked[0].session, null); assert.equal(linked[0].ref.point, '첫 번째 검토 답변');
});

test('완료는 미리 정한 조건과 근거를 모두 요구하며 확인 필요는 허용한다', () => {
  assert.match(completionError(goal({ status: 'done' })), /완료 조건과 확인한 근거/);
  assert.match(completionError(goal({ status: 'done', completionCriteria: ' ', evidence: '통과' })), /완료 조건/);
  assert.equal(completionError(goal({ status: 'done', evidence: '담당자가 최종 문구를 확인함' })), '');
  assert.equal(completionError(goal({ status: 'review', completionCriteria: '', evidence: '' })), '');
});

test('대기에는 이유가 필요하지만 모든 목표에 배포·사용자 승인 조건을 강제하지 않는다', () => {
  assert.match(completionError(goal({ status: 'waiting', waitingReason: ' ' })), /기다리는지/);
  assert.equal(completionError(goal({ status: 'waiting', waitingReason: '자료 회신 대기' })), '');
  assert.equal(completionError(goal({ status: 'done', completionCriteria: '추천 문구 세 가지', evidence: '세 가지 초안 작성' })), '');
});

test('보관·고정 필터를 해제하기 전까지 조건에 맞는 목표만 표시한다', () => {
  assert.equal(goalMatches(goal({ archived: true }), '', {}, []), false);
  assert.equal(goalMatches(goal({ archived: true }), '', { archived: true }, []), true);
  assert.equal(goalMatches(goal(), '', { pinned: true }, []), false);
  assert.equal(goalMatches(goal({ pinned: true }), '', { pinned: true }, []), true);
});

test('정확히 확인된 부모만 하위 세션을 접으며 다른 기기·계정은 분리한다', () => {
  const parent = session('parent');
  const child = session('child', { parentSessionId: 'parent', initiator: 'agent' });
  const foreign = session('foreign', { parentSessionId: 'parent', host: 'other' });
  const isolated = session('isolated', { parentSessionId: 'parent', home: '.codex-private' });
  const entries = [parent, child, foreign, isolated].map((s) => ({ ref: sessionRef(s), session: s }));
  const groups = parentGroups(entries);
  assert.equal(groups.length, 3);
  assert.equal(groups[0].children[0].session.id, 'child');
  assert.equal(groups[1].session.id, 'foreign');
  assert.equal(groups[2].session.id, 'isolated');
});

test('부모 세션이 없는 하위 실행과 순환 참조는 숨기지 않는다', () => {
  const sessions = [session('a', { parentSessionId: 'b' }), session('b', { parentSessionId: 'a' }), session('c', { parentSessionId: 'missing' })];
  const groups = parentGroups(sessions.map((s) => ({ ref: sessionRef(s), session: s })));
  assert.equal(groups.length, 3);
  assert.equal(groups.flatMap((g) => g.children).length, 0);
});

test('실행 취소용 변경 스냅샷은 목표 데이터만 보존하고 서버 이력·revision을 덮어쓰지 않는다', () => {
  const original = goal({ sessions: [sessionRef(session('one'))], revision: 4, history: [{ reason: 'old' }], createdAt: '2026-10-01T00:00:00Z' });
  const snapshot = editable(original);
  assert.equal(snapshot.revision, undefined); assert.equal(snapshot.history, undefined); assert.equal(snapshot.createdAt, undefined);
  snapshot.sessions[0].id = 'new';
  assert.equal(original.sessions[0].id, 'one', '뒤의 변경이 이전 undo 스냅샷에 섞이지 않는다');
  assert.equal(snapshot.title, original.title);
});

// A small DOM harness exercises the real asynchronous module without a server
// or native agent. The tests drive its buttons instead of exposing private state.
function browser(api, { deferFrames = false } = {}) {
  const fs = require('node:fs');
  const vm = require('node:vm');
  const ids = new Map(); const frames = [];
  let document;
  function element(tag = 'div') {
    const n = { tagName: tag.toUpperCase(), children: [], dataset: {}, attributes: {}, events: {}, style: { setProperty() {}, removeProperty() {} },
      className: '', textContent: '', value: '', scrollTop: 0, scrollLeft: 0, hidden: false,
      classList: { add() {}, remove() {}, toggle() {} },
      append(...items) { for (const item of items) { if (item.parentNode) item.parentNode.children = item.parentNode.children.filter((old) => old !== item); item.parentNode = this; this.children.push(item); } },
      prepend(...items) { this.append(...items); this.children = [...items, ...this.children.filter((item) => !items.includes(item))]; },
      replaceChildren(...items) { for (const item of this.children) item.parentNode = null; this.children = []; this.append(...items); },
      addEventListener(type, fn) { this.events[type] = fn; },
      setAttribute(key, value) { this.attributes[key] = String(value); },
      getAttribute(key) { return this.attributes[key] ?? null; },
      querySelector(selector) { return all(this).find((item) => matches(item, selector)) || null; },
      querySelectorAll(selector) { return all(this).filter((item) => matches(item, selector)); },
      focus() { document.activeElement = this; },
      showModal() { this.open = true; }, close() { this.open = false; },
    };
    Object.defineProperty(n, 'id', { get() { return this._id; }, set(value) { this._id = value; ids.set(value, this); } });
    return n;
  }
  const all = (n) => n.children.flatMap((child) => [child, ...all(child)]);
  function matches(n, selector) {
    if (selector.startsWith('.')) return n.className.split(' ').includes(selector.slice(1));
    const data = selector.match(/^\[data-goal-open(?:="([^"]+)")?\]$/);
    if (data) return n.dataset.goalOpen !== undefined && (!data[1] || n.dataset.goalOpen === data[1]);
    return n.tagName.toLowerCase() === selector;
  }
  document = { body: element('body'), activeElement: null, createElement: element,
    createElementNS: (_namespace, tag) => element(tag), addEventListener() {},
    getElementById(id) { if (!ids.has(id)) { const n = element(); n.id = id; document.body.append(n); } return ids.get(id); },
    querySelector: (selector) => document.body.querySelector(selector), querySelectorAll: (selector) => document.body.querySelectorAll(selector),
  };
  const history = { state: { nativeBoard: true, view: 'list' }, entries: [],
    replaceState(value) { this.state = value; }, pushState(value) { this.entries.push(this.state); this.state = value; },
    back() { this.state = this.entries.pop() || this.state; },
  };
  const window = { document, history, matchMedia: () => ({ matches: false, addEventListener() {} }), requestAnimationFrame: (fn) => deferFrames ? frames.push(fn) : fn(), addEventListener() {} };
  const context = vm.createContext({ window, console, structuredClone, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(require.resolve('../web/goals.js'), 'utf8'), context);
  const module = window.SessionholicGoals;
  module.init({ api, hideSession() {}, openSession() {} });
  return { module, document, history, get: document.getElementById, all, flushFrames() { for (const frame of frames.splice(0)) frame(); } };
}
function createUsingForm(client, title) {
  client.get('goal-new').events.click({ currentTarget: client.get('goal-new') });
  const dialog = client.get('goal-editor');
  const titleInput = client.all(dialog).find((item) => item.name === 'title');
  titleInput.value = title;
  return client.all(dialog).find((item) => item.tagName === 'FORM').events.submit({ preventDefault() {} });
}

test('초기화는 인증 전 API를 호출하지 않으며 늦은 GET이 저장한 새 작업을 덮어쓰지 않는다', async () => {
  let finishRead;
  const calls = [];
  const linked = session('linked');
  const saved = goal({ id: 'a'.repeat(32), title: '새로 저장한 목표', revision: 1, sessions: [sessionRef(linked)] });
  const c = browser(async (path, options) => {
    calls.push({ path, options });
    if (options?.method === 'POST') return { goal: saved };
    return new Promise((resolve) => { finishRead = resolve; });
  });
  assert.equal(calls.length, 0, '초기화는 shell만 만든다');
  const refreshing = c.module.refresh();
  await createUsingForm(c, saved.title);
  assert.equal(c.module.goalNamesForSession(linked), saved.title);
  finishRead({ goals: [], revision: 0 });
  await refreshing;
  assert.equal(c.module.goalNamesForSession(linked), saved.title, '쓰기 전에 시작한 오래된 읽기는 무시한다');
});

test('잠근 뒤 도착한 목표 읽기 응답이 민감한 작업을 다시 그리지 않는다', async () => {
  let finishRead;
  const c = browser(() => new Promise((resolve) => { finishRead = resolve; }));
  const refreshing = c.module.refresh();
  c.module.locked();
  const linked = session('private');
  finishRead({ goals: [goal({ sessions: [sessionRef(linked)] })] });
  await refreshing;
  assert.equal(c.module.goalNamesForSession(linked), '');
  assert.equal(c.get('goal-count').textContent, '0개 작업');
});

test('저장 충돌은 입력을 지우거나 성공으로 표시하지 않고 최신 revision 확인을 안내한다', async () => {
  const c = browser(async () => { const error = new Error('conflict'); error.status = 409; error.code = 'goal_conflict'; throw error; });
  await createUsingForm(c, '아직 저장하지 못한 제목');
  const dialog = c.get('goal-editor');
  assert.equal(dialog.open, true);
  assert.equal(c.all(dialog).find((item) => item.name === 'title').value, '아직 저장하지 못한 제목');
  assert.match(c.all(dialog).find((item) => item.className === 'error').textContent, /다른 곳에서 작업이 바뀌었습니다/);
  assert.equal(c.get('goal-count').textContent, '0개 작업');
});

test('목표→대화→뒤로→앞으로가 같은 세션과 목표 패널을 복원한다', async () => {
  const linked = session('conversation');
  const target = goal({ id: 'c'.repeat(32), revision: 1, sessions: [sessionRef(linked)] });
  const c = browser(async () => ({ goals: [target] }));
  c.module.update([linked]); await c.module.refresh(); c.module.openGoal(target.id);
  c.get('goal-detail').querySelector('.goal-session-open').events.click();
  const conversationEntry = c.history.state;
  assert.equal(conversationEntry.nativeBoard, true, 'native 보드 history 소유 키를 보존한다');
  assert.equal(conversationEntry.view, 'detail');
  assert.equal(conversationEntry.sessionholicGoals.sessionKey, linked.key);
  assert.equal(c.get('detail').parentNode.id, 'goal-conversation-slot');
  assert.equal(c.module.backFromSession(), true);
  assert.equal(c.module.handleHistory({ state: c.history.state }), true);
  assert.equal(c.get('detail').parentNode.id, 'session-layout');
  assert.equal(c.get('goal-detail').hidden, false);
  c.history.state = conversationEntry;
  assert.equal(c.module.handleHistory({ state: conversationEntry }), true);
  assert.equal(c.get('detail').parentNode.id, 'goal-conversation-slot');
  assert.equal(c.get('goal-conversation-slot').hidden, false);
});

test('고정 실행 취소는 저장된 최신 revision으로 역변경하며 외부 실행을 요청하지 않는다', async () => {
  let stored = goal({ id: 'd'.repeat(32), revision: 1, pinned: false });
  const calls = [];
  const c = browser(async (path, options) => {
    if (!options) return { goals: [stored] };
    calls.push({ path, body: options.body });
    assert.equal(options.body.revision, stored.revision);
    stored = { ...stored, ...options.body, revision: stored.revision + 1 };
    return { goal: stored };
  });
  await c.module.refresh(); c.module.openGoal(stored.id);
  const pin = c.get('goal-detail').querySelector('.goal-detail-status').children.find((item) => item.tagName === 'BUTTON');
  pin.events.click(); await new Promise(setImmediate);
  assert.equal(stored.pinned, true);
  assert.equal(c.get('goal-undo').hidden, false);
  await c.get('goal-undo-action').events.click();
  assert.equal(stored.pinned, false);
  assert.deepEqual(calls.map((call) => call.body.revision), [1, 2]);
  assert.match(calls[1].body.reason, /실행 취소/);
  assert.ok(calls.every((call) => call.path.startsWith('/api/goals/')));
  assert.equal(c.get('goal-undo').hidden, true);
});

test('뒤로가기의 지연 카드 초점 복원은 새 검색 입력 초점을 빼앗지 않는다', async () => {
  const target = goal({ id: 'e'.repeat(32), revision: 1 });
  const c = browser(async () => ({ goals: [target] }), { deferFrames: true });
  await c.module.refresh(); c.module.openGoal(target.id);
  c.get('goal-detail').querySelector('.goal-back').events.click();
  c.get('goal-search').focus(); c.flushFrames();
  assert.equal(c.document.activeElement, c.get('goal-search'));
  c.module.openGoal(target.id);
  const listEntry = { ...c.history.state, sessionholicGoals: { tab: 'goals', id: null, conversation: false, sessionKey: null } };
  c.module.handleHistory({ state: listEntry });
  c.get('goal-search').focus(); c.flushFrames();
  assert.equal(c.document.activeElement, c.get('goal-search'));
});

test('Safari처럼 클릭으로 버튼 초점이 바뀌지 않아도 시트 취소는 원래 수정 버튼으로 돌아간다', async () => {
  const target = goal({ id: 'f'.repeat(32), revision: 1 });
  const c = browser(async () => ({ goals: [target] }));
  await c.module.refresh(); c.module.openGoal(target.id);
  const edit = c.get('goal-detail').querySelector('.goal-detail-head').children.find((item) => item.textContent === '수정');
  c.get('goal-detail-title').focus();
  edit.events.click({ currentTarget: edit });
  assert.equal(c.document.activeElement.name, 'title');
  c.all(c.get('goal-editor')).find((item) => item.textContent === '취소').events.click();
  assert.equal(c.document.activeElement, edit);
});
