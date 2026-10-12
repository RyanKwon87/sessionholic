'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  goalMatches, sameSession, sessionRef, originOf, linkedSessions,
  completionError, editable, parentGroups, activityOf, activityLabel, primarySession, correctionUndo,
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
function browser(api, { deferFrames = false, hooks = {} } = {}) {
  const fs = require('node:fs');
  const vm = require('node:vm');
  const ids = new Map(); const frames = []; let clock = 100000;
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
  const context = vm.createContext({ window, console, structuredClone, setTimeout, clearTimeout, Date: class extends Date { static now() { return clock; } } });
  vm.runInContext(fs.readFileSync(require.resolve('../web/goals.js'), 'utf8'), context);
  const module = window.SessionholicGoals;
  module.init({ api, hideSession() {}, openSession() {}, ...hooks });
  return { module, document, history, get: document.getElementById, all, advance(ms) { clock += ms; }, flushFrames() { for (const frame of frames.splice(0)) frame(); } };
}
function editTitle(client, id, title) {
  client.module.openGoal(id);
  const edit = client.get('goal-detail').querySelector('.goal-detail-head').children.find((item) => item.textContent === '제목 수정');
  edit.events.click({ currentTarget: edit });
  const dialog = client.get('goal-editor');
  client.all(dialog).find((item) => item.name === 'title').value = title;
  return client.all(dialog).find((item) => item.tagName === 'FORM').events.submit({ preventDefault() {} });
}

test('초기화는 인증 전 API를 호출하지 않으며 늦은 GET이 저장한 제목 교정을 덮어쓰지 않는다', async () => {
  let finishRead;
  const calls = [];
  const linked = session('linked');
  const initial = goal({ id: 'a'.repeat(32), title: '자동으로 읽은 제목', automatic: true, activity: 'active', revision: 1, sessions: [sessionRef(linked)] });
  const saved = { ...initial, title: '사용자가 교정한 제목', revision: 2 };
  const c = browser(async (path, options) => {
    calls.push({ path, options });
    if (options?.method === 'POST') return { goal: saved };
    if (calls.length === 1) return { goals: [initial], revision: 1 };
    return new Promise((resolve) => { finishRead = resolve; });
  });
  assert.equal(calls.length, 0, '초기화는 shell만 만든다');
  await c.module.refresh();
  const refreshing = c.module.refresh();
  await editTitle(c, initial.id, saved.title);
  assert.equal(c.module.goalNamesForSession(linked), saved.title);
  finishRead({ goals: [initial], revision: 1 });
  await refreshing;
  assert.equal(c.module.goalNamesForSession(linked), saved.title, '쓰기 전에 시작한 오래된 읽기는 무시한다');
  assert.ok(calls.every(({ path }) => path.startsWith('/api/work')));
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

test('제목 교정 충돌은 입력을 지우거나 성공으로 표시하지 않는다', async () => {
  const initial = goal({ revision: 3, automatic: true });
  const c = browser(async (_path, options) => {
    if (!options) return { goals: [initial] };
    const error = new Error('conflict'); error.status = 409; error.code = 'work_conflict'; throw error;
  });
  await c.module.refresh(); await editTitle(c, initial.id, '아직 저장하지 못한 제목');
  const dialog = c.get('goal-editor');
  assert.equal(dialog.open, true);
  assert.equal(c.all(dialog).find((item) => item.name === 'title').value, '아직 저장하지 못한 제목');
  assert.match(c.all(dialog).find((item) => item.className === 'error').textContent, /다른 곳에서 작업이 바뀌었습니다/);
  assert.equal(c.get('goal-count').textContent, '1개 작업');
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
  assert.ok(calls.every((call) => call.path.startsWith('/api/work/')));
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
  const edit = c.get('goal-detail').querySelector('.goal-detail-head').children.find((item) => item.textContent === '제목 수정');
  c.get('goal-detail-title').focus();
  edit.events.click({ currentTarget: edit });
  assert.equal(c.document.activeElement.name, 'title');
  c.all(c.get('goal-editor')).find((item) => item.textContent === '취소').events.click();
  assert.equal(c.document.activeElement, edit);
});


test('일 시작하기는 작업 입력 폼을 만들지 않고 기존 새 세션 흐름을 호출한다', () => {
  const triggers = [];
  const c = browser(async () => { throw new Error('쓰기 요청은 없어야 한다'); }, { hooks: { startWork: (trigger) => triggers.push(trigger) } });
  for (const id of ['goal-new', 'goal-empty-new']) c.get(id).events.click({ currentTarget: c.get(id) });
  assert.deepEqual(triggers, [c.get('goal-new'), c.get('goal-empty-new')]);
  assert.equal(c.all(c.document.body).some((item) => item.tagName === 'FORM'), false);
});

test('자동 작업 기본 화면은 답변 필요·진행 중·최근 결과 순이며 대기는 접힌다', async () => {
  const goals = ['waiting', 'recent', 'result', 'active', 'attention'].map((activity, i) => goal({ id: `work-${i}`, automatic: true, activity }));
  const c = browser(async () => ({ goals })); await c.module.refresh();
  const board = c.get('goal-board');
  assert.equal(board.dataset.view, 'list');
  assert.deepEqual(board.children.map((item) => item.dataset.goalGroup), ['attention', 'active', 'result', 'waiting', 'recent']);
  for (const key of ['waiting', 'recent']) {
    const group = board.children.find((item) => item.dataset.goalGroup === key);
    assert.equal(group.tagName, 'DETAILS'); assert.equal(group.open, false);
  }
  assert.equal(c.all(board).some((item) => item.tagName === 'SELECT'), false, '자동 상태를 수동 상태 선택으로 덮지 않는다');
  c.get('goal-view-kanban').events.click();
  assert.equal(board.dataset.view, 'kanban'); assert.equal(board.children.length, 5);
});

test('응답 도착과 완료 근거 확인은 구분하며 오프라인은 자동 작업 상태를 바꾸지 않는다', () => {
  assert.equal(activityLabel(goal({ automatic: true, activity: 'result', status: 'review' })), '답변 도착');
  assert.equal(activityLabel(goal({ automatic: true, activity: 'result', status: 'done', evidence: '' })), '답변 도착');
  assert.equal(activityLabel(goal({ automatic: true, activity: 'result', status: 'done', evidence: '테스트 통과' })), '완료 근거 확인');
  assert.equal(activityOf(goal({ automatic: true, activity: 'active', connection: { state: 'offline' } })), 'active');
});

test('카드는 답변이 필요한 연결 세션을 바로 열고 관리는 동일한 세션에 전달한다', async () => {
  const parent = session('parent', { phase: 'working' });
  const child = session('child', { parentSessionId: 'parent', phase: 'needs_input' });
  const idle = session('idle', { phase: 'idle' });
  const target = goal({ automatic: true, activity: 'attention', sessions: [parent, child, idle].map(sessionRef) });
  assert.equal(primarySession(target, [parent, child, idle]).id, 'child');
  const opened = [], managed = [];
  const c = browser(async () => ({ goals: [target] }), { hooks: { openSession: (key) => opened.push(key), manageSession: (value, trigger) => managed.push({ value, trigger }) } });
  c.module.update([parent, child, idle]); await c.module.refresh();
  const card = c.get('goal-board').querySelector('.goal-card');
  const manage = c.all(card).find((item) => item.tagName === 'BUTTON' && item.textContent === '세션 관리');
  manage.events.click({ currentTarget: manage });
  assert.equal(managed[0].value.id, child.id); assert.equal(managed[0].trigger, manage);
  card.querySelector('.goal-card-open').events.click();
  assert.deepEqual(opened, [child.key]);
  assert.equal(c.get('detail').parentNode.id, 'goal-conversation-slot');
});

test('제목만 교정하며 완료 조건·현재·다음 입력과 무관한 자동 필드는 전송하지 않는다', async () => {
  const original = goal({ automatic: true, activity: 'active', revision: 4, current: '빌드 중', next: '검수' });
  const writes = [];
  const c = browser(async (_path, options) => {
    if (!options) return { goals: [original] };
    writes.push(options.body); return { goal: { ...original, ...options.body, revision: 5 } };
  });
  await c.module.refresh(); await editTitle(c, original.id, '읽기 쉬운 이름');
  assert.deepEqual(Object.keys(writes[0]).sort(), ['actor', 'reason', 'revision', 'title']);
  const fields = c.all(c.get('goal-editor')).filter((item) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(item.tagName));
  assert.deepEqual(fields.map((item) => item.name), ['title']);
  assert.deepEqual(correctionUndo(original, { title: '교정 제목' }), { resetOverrides: ['title'] });
  assert.deepEqual(correctionUndo({ ...original, automation: { overrides: ['title'] } }, { title: '교정 제목' }), { title: original.title });
  assert.deepEqual(correctionUndo(original, { pinned: true }), { pinned: false });
});

test('자동 카드 새로고침은 같은 그룹 순서와 펼친 대기 영역을 유지한다', async () => {
  const a = goal({ id: 'a', automatic: true, activity: 'active' }), b = goal({ id: 'b', automatic: true, activity: 'active' });
  const waiting = goal({ id: 'wait', automatic: true, activity: 'waiting' });
  let goals = [a, b, waiting];
  const c = browser(async () => ({ goals })); await c.module.refresh();
  const wait = c.get('goal-board').children.find((item) => item.dataset.goalGroup === 'waiting');
  wait.open = true; wait.events.toggle();
  goals = [waiting, b, a]; await c.module.refresh();
  const cards = c.get('goal-board').children.find((item) => item.dataset.goalGroup === 'active').querySelectorAll('.goal-card');
  assert.deepEqual(cards.map((item) => item.dataset.goalId), ['a', 'b']);
  assert.equal(c.get('goal-board').children.find((item) => item.dataset.goalGroup === 'waiting').open, true);
});

test('묶음 교정은 원본과 대상 revision을 모두 전달하고 목표 작성 폼을 요구하지 않는다', async () => {
  const from = goal({ id: 'from', automatic: true, activity: 'active', revision: 2 });
  const to = goal({ id: 'to', automatic: true, activity: 'active', revision: 7 });
  const writes = []; let goals = [from, to];
  const c = browser(async (path, options) => {
    if (!options) return { goals };
    writes.push({ path, body: options.body }); goals = [{ ...to, revision: 8 }]; return { goal: goals[0] };
  });
  await c.module.refresh(); c.module.openGoal(from.id);
  const correction = c.all(c.get('goal-detail')).find((item) => item.textContent === '묶음 교정');
  correction.events.click({ currentTarget: correction });
  const dialog = c.get('goal-linker');
  const target = c.all(dialog).find((item) => item.type === 'radio'); target.checked = true; target.events.change();
  await c.all(dialog).find((item) => item.textContent === '함께 묶기').events.click();
  assert.equal(writes[0].path, '/api/work/from');
  assert.equal(writes[0].body.action, 'merge'); assert.equal(writes[0].body.revision, 2);
  assert.equal(writes[0].body.targetId, 'to'); assert.equal(writes[0].body.targetRevision, 7);
  assert.equal(c.get('goal-count').textContent, '1개 작업');
  assert.equal(c.get('goal-detail').dataset.goalId, 'to');
});

test('하위 세션 분리는 정확한 계정·기기 참조로 요청하고 갱신된 독립 카드를 표시한다', async () => {
  const parent = session('parent'), child = session('child', { parentSessionId: 'parent', initiator: 'agent' });
  const original = goal({ automatic: true, activity: 'active', revision: 2, sessions: [parent, child].map(sessionRef) });
  let goals = [original]; const writes = [];
  const c = browser(async (_path, options) => {
    if (!options) return { goals };
    writes.push(options.body);
    goals = [{ ...original, revision: 3, sessions: [sessionRef(parent)] }, goal({ id: 'split', automatic: true, activity: 'active', sessions: [sessionRef(child)] })];
    return { goal: goals[0] };
  });
  c.module.update([parent, child]); await c.module.refresh(); c.module.openGoal(original.id);
  const children = c.get('goal-detail').querySelector('.goal-children');
  assert.equal(children.open, false);
  await c.all(children).find((item) => item.textContent === '별도 작업으로 분리').events.click();
  assert.equal(writes[0].action, 'split');
  assert.deepEqual(JSON.parse(JSON.stringify(writes[0].session)), sessionRef(child));
  assert.equal(writes[0].revision, 2);
  assert.equal(c.get('goal-count').textContent, '2개 작업');
});


test('다른 카드에 병합된 원본은 보관 포함·검색·칸반에서도 중복 표시하지 않는다', async () => {
  const merged = goal({ id: 'merged', automatic: true, archived: true, automation: { mergedInto: 'target' } });
  const target = goal({ id: 'target', automatic: true, activity: 'active' });
  assert.equal(goalMatches(merged, '', { archived: true }), false);
  const c = browser(async () => ({ goals: [merged, target] })); await c.module.refresh();
  assert.equal(c.get('goal-count').textContent, '1개 작업');
  c.get('goal-view-kanban').events.click();
  assert.equal(c.get('goal-board').querySelectorAll('.goal-card').length, 1);
});

test('대화 질문에서 추정한 대기 이유는 카드와 요약에서 추정으로 표시한다', async () => {
  const target = goal({ automatic: true, activity: 'attention', waitingReason: '자료를 기다리는 것으로 보입니다', automation: { fieldEvidence: { waitingReason: { kind: 'inferred_question', confidence: 'inferred', excerpt: '자료가 도착했나요?' } } } });
  const c = browser(async () => ({ goals: [target] })); await c.module.refresh();
  assert.equal(c.get('goal-board').querySelector('.goal-inferred-note').textContent, '추정');
  c.module.openGoal(target.id);
  assert.equal(c.get('goal-detail').querySelector('.goal-inferred-note').textContent, '추정');
});


test('처음 접속과 잠금 복귀는 세션 탭·ARIA·history를 함께 초기화한다', () => {
  const c = browser(async () => ({ goals: [] }));
  assert.equal(c.get('nav-sessions').getAttribute('aria-selected'), 'true');
  assert.equal(c.get('nav-sessions').tabIndex, 0);
  assert.equal(c.get('nav-goals').getAttribute('aria-selected'), 'false');
  assert.equal(c.get('goals-workspace').hidden, true);
  assert.equal(c.get('session-layout').hidden, false);
  assert.equal(c.history.state.sessionholicGoals.tab, 'sessions');
  c.module.openTab('goals'); c.module.locked();
  assert.equal(c.get('nav-sessions').getAttribute('aria-selected'), 'true');
  assert.equal(c.get('session-layout').hidden, false);
  assert.equal(c.history.state.sessionholicGoals.tab, 'sessions');
});

test('세션 업데이트는 작업을 10초 단위로 한 번만 읽고 변경 없는 세션에서도 새 요약을 반영한다', async () => {
  let gets = 0; let complete;
  const first = goal({ title: '관측 전', automatic: true });
  const c = browser(async () => { gets++; return gets === 1 ? { goals: [first] } : new Promise((resolve) => { complete = resolve; }); });
  await c.module.refresh();
  c.advance(9999); c.module.update([]); assert.equal(gets, 1);
  c.advance(1); c.module.update([]); assert.equal(gets, 2);
  c.module.update([]); c.advance(20000); c.module.update([]); assert.equal(gets, 2, '진행 중 읽기는 하나뿐이다');
  complete({ goals: [{ ...first, title: '새로 관측한 내용' }] }); await new Promise(setImmediate);
  assert.match(c.all(c.get('goal-board')).map((node) => node.textContent).join(' '), /새로 관측한 내용/);
});

test('작업 새로고침의 onChanged 재렌더는 재귀 조회를 만들지 않고 숨긴 페이지는 자동 읽지 않는다', async () => {
  let gets = 0; let c;
  c = browser(async () => { gets++; return { goals: [] }; }, { hooks: { onChanged: () => c.module.update([]) } });
  await c.module.refresh(); assert.equal(gets, 1);
  c.advance(10000); c.document.hidden = true; c.module.update([]); assert.equal(gets, 1);
  c.document.hidden = false; c.module.update([]); await new Promise(setImmediate); assert.equal(gets, 2);
  c.module.update([]); assert.equal(gets, 2);
  c.module.locked(); c.advance(10000); c.module.update([]); assert.equal(gets, 2, '잠금 뒤 자동 읽기를 하지 않는다');
});
