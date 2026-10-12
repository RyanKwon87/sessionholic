/* Work is projected from sessions; user edits are small, explicit corrections. */
(function (root) {
  'use strict';
  const STATUSES = [
    ['todo', '할 일'], ['in_progress', '진행 중'], ['waiting', '대기'],
    ['review', '확인 필요'], ['done', '완료'],
  ];
  const ACTIVITIES = [['attention', '내 답변 필요'], ['active', '진행 중'], ['result', '최근 결과'], ['waiting', '기다리는 일'], ['recent', '이전 작업']];
  const ORIGINS = { human: '직접 시작', agent: '에이전트 시작', unknown: '출처 미확인' };
  const LIST_LAYOUT = '(max-width: 700px), (max-width: 1000px) and (orientation: portrait)';
  const EDIT_FIELDS = ['title', 'completionCriteria', 'current', 'next', 'status', 'waitingReason', 'evidence', 'project', 'pinned', 'archived', 'sessions'];
  const clean = (value) => String(value || '').trim();
  const lower = (value) => clean(value).toLocaleLowerCase();
  function sessionRef(session) {
    return { host: session.host || '', agent: session.agent || '', id: session.id || session.sessionId || '', home: session.home || (session.agent === 'claude' ? '.claude' : '.codex') };
  }
  function sameSession(a, b) {
    return !!a && !!b && !!a.id && a.host === b.host && a.agent === b.agent && a.id === b.id && (a.home || (a.agent === 'claude' ? '.claude' : '.codex')) === (b.home || (b.agent === 'claude' ? '.claude' : '.codex'));
  }
  function originOf(session) {
    const value = typeof session?.initiator === 'string' ? session.initiator : 'unknown';
    return Object.hasOwn(ORIGINS, value) ? value : 'unknown';
  }
  function sessionText(session) {
    return [session.title, session.project, session.host, session.hostLabel, session.agent,
      ORIGINS[originOf(session)], session.initiatorName].join(' ');
  }
  function linkedSessions(goal, sessions) {
    return (goal.sessions || []).map((ref) => ({ ref, session: sessions.find((s) => sameSession(ref, sessionRef(s))) || null }));
  }
  function goalMatches(goal, query, filters = {}, sessions = []) {
    if (goal.automation?.mergedInto) return false;
    const linked = linkedSessions(goal, sessions);
    if (goal.archived && !filters.archived) return false;
    if (filters.pinned && !goal.pinned) return false;
    if (filters.project && goal.project !== filters.project && !linked.some(({ session }) => session?.project === filters.project)) return false;
    if (filters.host && !linked.some(({ ref }) => ref.host === filters.host)) return false;
    if (filters.origin && !linked.some(({ session }) => originOf(session) === filters.origin)) return false;
    const haystack = lower([goal.title, goal.project, goal.current, goal.next, ...linked.map(({ session, ref }) => session ? sessionText(session) : `${ref.host} ${ref.agent}`)].join(' '));
    return lower(query).split(/\s+/).filter(Boolean).every((word) => haystack.includes(word));
  }
  function completionError(goal) {
    if (!clean(goal.title)) return '작업 이름을 입력하세요.';
    if (goal.status === 'done' && (!clean(goal.completionCriteria) || !clean(goal.evidence))) return '완료 조건과 확인한 근거를 적어 주세요. 아직 판단하기 어렵다면 확인 필요로 옮길 수 있어요.';
    if (goal.status === 'waiting' && !clean(goal.waitingReason)) return '무엇을 기다리는지 적어 주세요.';
    return '';
  }
  function editable(goal) {
    return Object.fromEntries(EDIT_FIELDS.filter((key) => goal[key] !== undefined).map((key) => [key, structuredClone(goal[key])]));
  }
  function parentGroups(entries) {
    // Only an explicit, matching parent reference can nest another session.
    const roots = [], children = new Map();
    for (const entry of entries) {
      const parent = entry.session?.parentSessionId ? { ...entry.ref, id: entry.session.parentSessionId } : null;
      const candidate = parent && entries.find((other) => other !== entry && sameSession(parent, other.ref));
      if (candidate && !candidate.session?.parentSessionId) {
        if (!children.has(candidate)) children.set(candidate, []);
        children.get(candidate).push(entry);
      } else roots.push(entry);
    }
    return roots.map((entry) => ({ ...entry, children: children.get(entry) || [] }));
  }
  function activityOf(goal) {
    if (ACTIVITIES.some(([key]) => key === goal.activity)) return goal.activity;
    return ({ in_progress: 'active', waiting: 'waiting', review: 'attention', done: 'result' })[goal.status] || 'recent';
  }
  function activityLabel(goal) {
    if (goal.status === 'done' && clean(goal.evidence)) return '완료 근거 확인';
    return ({ attention: '답변 필요', active: '진행 중', result: '답변 도착', waiting: '외부 응답·작업 대기', recent: '최근 대화' })[activityOf(goal)];
  }
  function primarySession(goal, sessions) {
    const entries = linkedSessions(goal, sessions).filter(({ session }) => session);
    const score = ({ session }) => (session.phase === 'needs_input' ? 100 : session.phase === 'working' ? 50 : 0) + (!session.parentSessionId ? 10 : 0);
    return entries.map((entry, index) => ({ ...entry, index })).sort((a, b) => score(b) - score(a) || a.index - b.index)[0]?.session || null;
  }
  function correctionUndo(goal, patch) {
    const previous = {}, reset = [];
    for (const key of Object.keys(patch).filter((key) => EDIT_FIELDS.includes(key))) {
      if (goal.automatic && key === 'title' && !goal.automation?.overrides?.includes(key)) reset.push(key);
      else previous[key] = structuredClone(goal[key] ?? (['pinned', 'archived'].includes(key) ? false : key === 'sessions' ? [] : ''));
    }
    if (reset.length) previous.resetOverrides = reset;
    return previous;
  }
  const pure = { STATUSES, ACTIVITIES, ORIGINS, sessionRef, sameSession, originOf, linkedSessions, goalMatches, completionError, editable, parentGroups, activityOf, activityLabel, primarySession, correctionUndo };
  if (typeof module !== 'undefined' && module.exports) module.exports = pure;
  if (!root.document) return;
  const document = root.document;
  const $ = (id) => document.getElementById(id);
  const state = {
    options: {}, goals: [], sessions: [], selected: null, tab: 'sessions', query: '',
    filters: { project: '', host: '', origin: '', pinned: false, archived: false },
    recent: [], detailPositions: new Map(), detailOpen: new Map(), ready: false, loading: false, lastRefreshAt: 0, busy: false, conversation: false, mutations: 0,
    view: 'list', groupOpen: new Map(), cardOrder: [], sessionKey: null, boardPosition: { top: 0, left: 0 }, undo: null, epoch: 0, init: false,
  };
  function node(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function button(text, className, onClick) {
    const value = node('button', className, text); value.type = 'button';
    value.addEventListener('click', onClick); return value;
  }
  function field(label, input) { const wrap = node('label', 'goal-field'); wrap.append(node('span', '', label), input); return wrap; }
  function select(options, value, label, onChange) {
    const result = node('select'); result.setAttribute('aria-label', label);
    for (const [key, text] of options) { const item = node('option', '', text); item.value = key; result.append(item); }
    result.value = value;
    result.addEventListener('change', onChange); return result;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 20 20'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', name === 'grip' ? 'M7 5h.01M13 5h.01M7 10h.01M13 10h.01M7 15h.01M13 15h.01' : 'M10 4v12M4 10h12');
    path.setAttribute('fill', 'none'); path.setAttribute('stroke', 'currentColor'); path.setAttribute('stroke-width', '2'); path.setAttribute('stroke-linecap', 'round');
    svg.append(path); return svg;
  }
  function announce(text, error = false) {
    const target = $('goal-notice'); target.textContent = text; target.hidden = !text;
    target.classList.toggle('is-error', error);
  }
  function current() { return state.goals.find((goal) => goal.id === state.selected); }
  function rememberPosition() {
    const board = $('goal-board');
    state.boardPosition = { top: $('goal-board-scroll').scrollTop, left: board.scrollLeft };
  }
  function restorePosition(focus = false) {
    const focusOwner = document.activeElement;
    root.requestAnimationFrame(() => {
      const board = $('goal-board'); $('goal-board-scroll').scrollTop = state.boardPosition.top; board.scrollLeft = state.boardPosition.left;
      if (focus && state.selected && document.activeElement === focusOwner) (document.querySelector(`[data-goal-open="${state.selected}"]`) || $('goal-search'))?.focus({ preventScroll: true });
    });
  }
  function setWorkspace() {
    $('goals-workspace').hidden = state.tab !== 'goals';
    if ($('nav-title')) { $('nav-title').textContent = state.tab === 'goals' ? '작업' : '세션'; $('nav-title').setAttribute('aria-label', `${state.tab === 'goals' ? '작업' : '세션'} 목록 맨 위로`); }
    $('session-layout').hidden = state.tab === 'goals';
    document.body.classList.toggle('goals-active', state.tab === 'goals');
    document.body.classList.toggle('goal-has-detail', state.tab === 'goals' && !!state.selected);
    document.body.classList.toggle('goal-has-conversation', state.tab === 'goals' && state.conversation);
    for (const tab of ['goals', 'sessions']) {
      $(`nav-${tab}`).setAttribute('aria-selected', String(state.tab === tab));
      $(`nav-${tab}`).tabIndex = state.tab === tab ? 0 : -1;
    }
    $('goal-detail').hidden = !state.selected || state.conversation;
    $('goal-conversation-slot').hidden = !state.conversation;
    $('goal-resizer').hidden = !state.selected;
  }
  function restoreConversationNode() {
    const detail = $('detail');
    if (detail && detail.parentNode !== $('session-layout')) $('session-layout').append(detail);
    state.conversation = false; state.sessionKey = null;
  }
  function openTab(tab, { history = true, clearSelection = false } = {}) {
    if (!state.init || !['goals', 'sessions'].includes(tab)) return;
    rememberPosition(); restoreConversationNode();
    state.options.hideSession?.(); state.tab = tab; if (clearSelection) state.selected = null;
    setWorkspace();
    if (tab === 'goals') { render(); restorePosition(); }
    if (history) recordHistory(true);
  }
  function recordHistory(replace = false) {
    const next = { ...(root.history.state || {}), view: state.conversation ? 'detail' : 'list',
      sessionholicGoals: { tab: state.tab, id: state.selected, conversation: state.conversation, sessionKey: state.sessionKey } };
    root.history[replace ? 'replaceState' : 'pushState'](next, '');
  }
  function openGoal(id, { history = true, focus = true } = {}) {
    if (!state.goals.some((goal) => goal.id === id)) return;
    rememberPosition(); restoreConversationNode(); state.options.hideSession?.();
    state.tab = 'goals'; state.selected = id;
    state.recent = [id, ...state.recent.filter((value) => value !== id)].slice(0, 5);
    setWorkspace(); renderDetail(); renderRecent(); markSelected();
    if (history) recordHistory();
    if (focus) $('goal-detail-title')?.focus({ preventScroll: true });
  }
  function closeGoal({ history = true } = {}) {
    const id = state.selected;
    restoreConversationNode(); state.options.hideSession?.(); state.selected = null; setWorkspace();
    if (history) recordHistory(true);
    restorePosition();
    const focusOwner = document.activeElement;
    root.requestAnimationFrame(() => {
      if (document.activeElement === focusOwner) (document.querySelector(`[data-goal-open="${id}"]`) || $('goal-search'))?.focus({ preventScroll: true });
    });
  }
  function openSession(session, goal) {
    if (!session) return;
    if (goal) {
      if (state.selected !== goal.id) openGoal(goal.id, { history: false, focus: false });
      state.tab = 'goals'; state.conversation = true; state.sessionKey = session.key;
      $('goal-conversation-slot').append($('detail')); setWorkspace(); recordHistory();
    } else openTab('sessions');
    state.options.openSession?.(session.key);
  }
  function backFromSession() {
    if (state.tab !== 'goals' || !state.conversation) return false;
    if (root.history.state?.sessionholicGoals?.conversation) { root.history.back(); return true; }
    restoreConversationNode(); state.options.hideSession?.(); setWorkspace(); renderDetail(); recordHistory(true);
    $('goal-detail-title')?.focus({ preventScroll: true }); return true;
  }
  function handleHistory(event) {
    const value = event.state?.sessionholicGoals;
    if (!value || !state.ready || event.state?.view === 'terminal') return false;
    rememberPosition(); restoreConversationNode(); state.tab = value.tab;
    state.selected = state.goals.some((goal) => goal.id === value.id) ? value.id : null;
    if (state.tab !== 'goals') { setWorkspace(); return false; }
    const session = value.conversation && state.selected && state.sessions.find((item) => item.key === value.sessionKey);
    if (session) {
      state.conversation = true; state.sessionKey = session.key;
      $('goal-conversation-slot').append($('detail')); setWorkspace();
      state.options.openSession?.(session.key);
    } else {
      state.options.hideSession?.(); setWorkspace(); renderDetail(); restorePosition(true);
      if (state.selected) $('goal-detail-title')?.focus({ preventScroll: true });
    }
    return true;
  }
  async function refresh() {
    if (!state.init || state.loading) return;
    const epoch = state.epoch; const mutation = state.mutations; state.loading = true; state.lastRefreshAt = Date.now();
    $('goal-board').setAttribute('aria-busy', 'true');
    if (!state.ready) announce('작업을 불러오고 있습니다…');
    try {
      const result = await state.options.api('/api/work');
      if (epoch !== state.epoch || mutation !== state.mutations) return;
      const position = { top: $('goal-board-scroll').scrollTop, left: $('goal-board').scrollLeft };
      state.goals = Array.isArray(result.goals) ? result.goals : [];
      state.cardOrder = [...state.cardOrder.filter((id) => state.goals.some((goal) => goal.id === id)), ...state.goals.map((goal) => goal.id).filter((id) => !state.cardOrder.includes(id))];
      if (current()?.automation?.mergedInto) state.selected = state.goals.find((goal) => goal.id === current().automation.mergedInto)?.id || null;
      state.ready = true; announce(result.automation?.state === 'limited' ? result.automation.message || '일부 작업은 마지막 확인 내용을 표시합니다.' : ''); render(); state.options.onChanged?.();
      $('goal-board-scroll').scrollTop = position.top; $('goal-board').scrollLeft = position.left;
    } catch (error) {
      if (epoch === state.epoch) announce(`작업을 불러오지 못했습니다. ${error.message || ''} 새로고침으로 다시 확인하세요.`, true);
    } finally {
      if (epoch === state.epoch) { state.loading = false; $('goal-board').setAttribute('aria-busy', 'false'); }
    }
  }
  async function save(goal, patch, reason, { undo = true } = {}) {
    if (state.busy) return null;
    const epoch = state.epoch; state.busy = true;
    const body = { ...patch, actor: { type: 'user', label: '사용자' }, reason };
    if (goal) body.revision = goal.revision;
    try {
      if (!goal?.id) throw new Error('세션에서 작업이 나타난 뒤 다시 확인하세요.');
      const result = await state.options.api(`/api/work/${goal.id}`, { method: 'POST', body });
      if (epoch !== state.epoch) return null;
      const saved = result.goal;
      if (!saved?.id) throw new Error('저장 결과를 확인하지 못했습니다. 새로고침으로 작업을 확인하세요.');
      state.mutations += 1;
      const index = state.goals.findIndex((value) => value.id === saved.id);
      if (index >= 0) state.goals[index] = saved; else state.goals.push(saved);
      if (undo && goal) showUndo(saved, correctionUndo(goal, patch), reason);
      announce(''); render(); state.options.onChanged?.(); return saved;
    } catch (error) {
      if (epoch === state.epoch) {
        const text = /409|conflict|다른|revision/i.test(`${error.status} ${error.code} ${error.message}`)
          ? '다른 곳에서 작업이 바뀌었습니다. 입력은 그대로 두었어요. 새로고침 후 최신 내용을 확인하세요.'
          : `작업을 저장하지 못했습니다. ${error.message || '연결을 확인하고 다시 저장하세요.'}`;
        announce(text, true); throw new Error(text);
      }
      return null;
    } finally { if (epoch === state.epoch) state.busy = false; }
  }
  function showUndo(goal, previous, reason) {
    state.undo = { id: goal.id, revision: goal.revision, previous };
    $('goal-undo-label').textContent = reason; $('goal-undo').hidden = false;
  }
  async function undo() {
    const value = state.undo; if (!value || state.busy) return;
    try {
      const saved = await save({ id: value.id, revision: value.revision }, value.previous, '직전 변경 실행 취소', { undo: false });
      if (saved) { state.undo = null; $('goal-undo').hidden = true; }
    } catch (_) { /* The explicit conflict notice retains the current work. */ }
  }
  function update(sessions) {
    if (!state.init) return;
    const before = JSON.stringify(state.sessions.map((s) => [s.key, s.title, s.project, s.hostLabel, s.initiator, s.initiatorName, s.parentSessionId, s.phase, s.stale]));
    state.sessions = Array.isArray(sessions) ? sessions : [];
    const after = JSON.stringify(state.sessions.map((s) => [s.key, s.title, s.project, s.hostLabel, s.initiator, s.initiatorName, s.parentSessionId, s.phase, s.stale]));
    if (before !== after) { renderFilters(); renderBoard(); if (!state.conversation) renderDetail(); }
    // A snapshot event may keep the session list unchanged while work summaries
    // advance. Refresh at most once per 10 seconds of visible board updates.
    if (state.ready && !state.loading && !document.hidden && Date.now() - state.lastRefreshAt >= 10000) void refresh();
  }
  function renderFilters() {
    const container = $('goal-filters');
    const focused = document.activeElement?.getAttribute('data-goal-filter');
    container.replaceChildren();
    const projects = [...new Set([...state.goals.map((g) => g.project), ...state.sessions.map((s) => s.project)].filter(Boolean))].sort();
    const hosts = [...new Map(state.sessions.map((s) => [s.host, s.hostLabel || s.host])).entries()];
    for (const [key, label, options] of [
      ['project', '프로젝트', [['', '모든 프로젝트'], ...projects.map((name) => [name, name])]],
      ['host', '기기', [['', '모든 기기'], ...hosts]],
      ['origin', '시작 주체', [['', '모든 시작 주체'], ...Object.entries(ORIGINS)]],
    ]) {
      const input = select(options, state.filters[key], label, () => { state.filters[key] = input.value; renderBoard(); });
      input.dataset.goalFilter = key; container.append(field(label, input));
    }
    for (const [key, label] of [['pinned', '고정한 작업'], ['archived', '보관한 작업 포함']]) {
      const input = node('input'); input.type = 'checkbox'; input.checked = state.filters[key]; input.dataset.goalFilter = key;
      input.addEventListener('change', () => { state.filters[key] = input.checked; renderBoard(); });
      const wrap = node('label', 'goal-check'); wrap.append(input, node('span', '', label)); container.append(wrap);
    }
    if (focused) container.querySelector(`[data-goal-filter="${focused}"]`)?.focus();
  }
  function renderRecent() {
    const box = $('goal-recent'); box.replaceChildren();
    const recent = state.recent.map((id) => state.goals.find((goal) => goal.id === id && !goal.archived && !goal.automation?.mergedInto)).filter(Boolean);
    box.hidden = !recent.length;
    if (recent.length) box.append(node('span', 'goal-recent-label', '최근 작업'));
    for (const goal of recent) box.append(button(goal.title, 'goal-recent-item', () => openGoal(goal.id)));
  }
  function render() {
    if (!state.init) return;
    renderFilters(); renderRecent(); renderBoard();
    if (!state.conversation) renderDetail(); setWorkspace();
  }
  function markSelected() {
    for (const card of document.querySelectorAll('[data-goal-open]')) card.setAttribute('aria-current', String(card.dataset.goalOpen === state.selected));
  }
  function renderBoard() {
    if (!state.init) return;
    const board = $('goal-board'); const position = { top: $('goal-board-scroll').scrollTop, left: board.scrollLeft };
    const focused = document.activeElement?.dataset?.goalOpen;
    board.replaceChildren();
    const goals = state.goals.filter((goal) => goalMatches(goal, state.query, state.filters, state.sessions));
    $('goal-count').textContent = `${goals.length}개 작업`;
    board.hidden = !goals.length;
    board.dataset.view = state.view;
    $('goal-view-list').setAttribute('aria-pressed', String(state.view === 'list'));
    $('goal-view-kanban').setAttribute('aria-pressed', String(state.view === 'kanban'));
    const groups = state.view === 'kanban' ? STATUSES : ACTIVITIES;
    for (const [key, label] of groups) {
      const items = goals.filter((goal) => (state.view === 'kanban' ? goal.status : activityOf(goal)) === key);
      if (state.view === 'list' && !items.length) continue;
      const folded = state.view === 'list' && ['waiting', 'recent'].includes(key);
      const column = node(folded ? 'details' : 'section', `goal-column ${state.view === 'kanban' ? 'status' : 'activity'}-${key}`);
      column.dataset.goalGroup = key; column.setAttribute('aria-label', label);
      if (folded) {
        column.open = clean(state.query) ? true : !!state.groupOpen.get(key);
        column.addEventListener('toggle', () => state.groupOpen.set(key, column.open));
      }
      const heading = node(folded ? 'summary' : 'div', 'goal-column-heading');
      heading.append(node('h2', '', label), node('span', 'goal-column-count', `${items.length}`)); column.append(heading);
      const cards = node('div', 'goal-column-cards');
      const order = (goal) => { const index = state.cardOrder.indexOf(goal.id); return index < 0 ? state.cardOrder.length : index; };
      for (const goal of [...items].sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || order(a) - order(b))) cards.append(renderCard(goal));
      if (!items.length) cards.append(node('p', 'goal-column-empty', '해당 작업이 없어요'));
      column.append(cards); board.append(column);
    }
    const empty = $('goal-empty'); empty.hidden = goals.length > 0 || !state.ready;
    const active = [state.query && `검색 “${state.query}”`, state.filters.project, state.filters.host,
      state.filters.origin && ORIGINS[state.filters.origin], state.filters.pinned && '고정한 작업'].filter(Boolean);
    $('goal-empty-title').textContent = active.length ? '조건에 맞는 작업이 없어요' : '세션을 시작하면 여기에 나타나요';
    $('goal-empty-copy').textContent = active.length
      ? `${active.join(' · ')} 조건을 적용했어요. 수집된 작업과 세션을 함께 찾으며, 보관한 작업은 필터에서 포함할 수 있어요.`
      : '진행 중인 세션에서 하는 일과 최근 결과를 자동으로 모읍니다. 이미 연 세션이 있다면 새로고침해 주세요.';
    $('goal-reset').hidden = !active.length;
    renderSearchSessions(); markSelected(); $('goal-board-scroll').scrollTop = position.top; board.scrollLeft = position.left;
    if (focused) document.querySelector(`[data-goal-open="${focused}"]`)?.focus({ preventScroll: true });
  }
  function openWork(goal) {
    const session = primarySession(goal, state.sessions);
    if (session) openSession(session, goal); else openGoal(goal.id);
  }
  function manageSession(session, trigger) {
    if (session) state.options.manageSession?.(session, trigger);
  }
  function resultText(goal) {
    return typeof goal.latestResult === 'string' ? goal.latestResult : goal.latestResult?.text || goal.latestResult?.excerpt || '';
  }
  function observedTime(value) {
    if (!value) return '';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  }
  function automationNote(goal) {
    if (!goal.automatic) return '직접 정리한 작업';
    const inferred = Object.values(goal.automation?.fieldEvidence || {}).some((item) => item?.confidence === 'inferred' || String(item?.kind || '').startsWith('inferred'));
    const time = observedTime(goal.lastActivityAt);
    return [inferred ? '대화에서 추정' : '세션에서 자동 정리', time && `${time} 확인`].filter(Boolean).join(' · ');
  }
  function inferredField(goal, field) {
    const evidence = goal.automation?.fieldEvidence?.[field];
    return evidence?.confidence === 'inferred' || String(evidence?.kind || '').startsWith('inferred');
  }
  function connectionNote(goal) {
    if (!['offline', 'mixed'].includes(goal.connection?.state)) return '';
    return goal.connection.state === 'offline' ? '기기 연결 끊김 · 마지막 확인 내용을 표시해요' : '일부 기기 연결 끊김 · 마지막 확인 내용을 포함해요';
  }
  function renderCard(goal) {
    const card = node('article', `goal-card activity-${activityOf(goal)}`); card.dataset.goalId = goal.id;
    const open = button(goal.title, 'goal-card-open', () => openWork(goal)); open.dataset.goalOpen = goal.id;
    open.setAttribute('aria-label', `${goal.title} · ${primarySession(goal, state.sessions) ? '관련 대화 열기' : '작업 요약 보기'}`);
    const activity = node('div', 'goal-card-status');
    activity.append(node('span', `goal-activity activity-${activityOf(goal)}`, activityLabel(goal)));
    if (goal.pinned) activity.append(node('span', 'goal-pinned', '고정'));
    card.append(open, activity);
    const result = activityOf(goal) === 'result' ? resultText(goal) : '';
    const request = activityOf(goal) === 'attention' ? clean(goal.waitingReason) : '';
    for (const [label, text] of [[request ? '요청' : '현재', request || result || goal.current || '세션의 최근 활동을 확인하고 있어요.'], ['다음', goal.next]]) {
      if (!clean(text)) continue;
      const row = node('p', 'goal-card-line'); const copy = node('span', '', text);
      if (label === '요청' && inferredField(goal, 'waitingReason')) copy.append(node('span', 'goal-inferred-note', '추정'));
      row.append(node('span', '', label === '현재' && result ? '결과' : label), copy); card.append(row);
    }
    if (activityOf(goal) === 'waiting' && clean(goal.waitingReason)) {
      const waiting = node('p', 'goal-card-waiting', goal.waitingReason);
      if (inferredField(goal, 'waitingReason')) waiting.append(node('span', 'goal-inferred-note', '추정'));
      card.append(waiting);
    }
    const linked = linkedSessions(goal, state.sessions);
    const groups = parentGroups(linked); const workers = linked.length - groups.length;
    const devices = [...new Set(linked.map(({ ref, session }) => `${session?.hostLabel || ref.host} · ${session?.agent || ref.agent}`))];
    card.append(node('p', 'goal-card-meta', [goal.project, ...devices.slice(0, 2), `세션 ${linked.length}개`, workers && `하위 작업 ${workers}개 포함`].filter(Boolean).join(' · ')));
    if (connectionNote(goal)) card.append(node('p', 'goal-connection-note', connectionNote(goal)));
    const footer = node('div', 'goal-card-footer');
    footer.append(button('요약·연결', 'goal-text-button', () => openGoal(goal.id)));
    const primary = primarySession(goal, state.sessions);
    if (primary && state.options.manageSession) footer.append(button('세션 관리', 'goal-text-button', (event) => manageSession(primary, event.currentTarget)));
    footer.append(node('span', 'goal-auto-note', automationNote(goal)));
    card.append(footer); return card;
  }
  function renderSearchSessions() {
    const box = $('goal-search-sessions'); box.replaceChildren(); box.hidden = !clean(state.query);
    if (box.hidden) return;
    const query = lower(state.query).split(/\s+/).filter(Boolean);
    const sessions = state.sessions.filter((session) => {
      if (state.filters.host && session.host !== state.filters.host) return false;
      if (state.filters.project && session.project !== state.filters.project) return false;
      if (state.filters.origin && originOf(session) !== state.filters.origin) return false;
      return query.every((word) => lower(`${sessionText(session)} ${goalNamesForSession(session)}`).includes(word));
    });
    box.append(node('h2', '', `연결해서 찾은 세션 ${sessions.length}개`));
    if (!sessions.length) box.append(node('p', 'goal-help', '현재 검색·필터에 맞는 수집된 세션이 없습니다. 목표에는 오래된 기록 제외를 적용하지 않습니다.'));
    for (const session of sessions.slice(0, 30)) {
      const goals = goalsForSession(session);
      const row = button('', 'goal-session-result', () => openSession(session, goals[0]));
      row.append(node('strong', '', session.title || '이름 없는 세션'), node('span', '', [goals.map((goal) => goal.title).join(', ') || '연결된 작업 없음', session.hostLabel || session.host, session.project, ORIGINS[originOf(session)]].filter(Boolean).join(' · ')));
      box.append(row);
    }
    if (sessions.length > 30) box.append(node('p', 'goal-help', '앞의 30개를 표시합니다. 검색어나 필터를 더해 범위를 줄여 주세요.'));
  }
  function renderDetail() {
    const container = $('goal-detail'); if (!container || state.conversation) return;
    const previousContent = container.querySelector('.goal-detail-content');
    if (container.dataset.goalId && previousContent) {
      state.detailPositions.set(container.dataset.goalId, previousContent.scrollTop);
      state.detailOpen.set(container.dataset.goalId, new Set([...previousContent.querySelectorAll('details')].filter((item) => item.open).map((item) => item.dataset.disclosure)));
    }
    const goal = current(); container.replaceChildren(); container.dataset.goalId = goal?.id || ''; if (!goal) return;
    const header = node('header', 'goal-detail-head');
    const back = button('작업으로', 'goal-back', () => closeGoal()); back.setAttribute('aria-label', '작업 목록으로 돌아가기');
    const edit = button('제목 수정', 'goal-text-button', (event) => openEditor(goal, {}, event.currentTarget));
    header.append(back, edit); container.append(header);
    const content = node('div', 'goal-detail-content');
    const title = node('h1', '', goal.title); title.id = 'goal-detail-title'; title.tabIndex = -1; content.append(title);
    const statusRow = node('div', 'goal-detail-status');
    statusRow.append(node('span', `goal-activity activity-${activityOf(goal)}`, activityLabel(goal)));
    const pin = button(goal.pinned ? '고정 해제' : '작업 고정', 'goal-text-button', () => { void save(goal, { pinned: !goal.pinned }, goal.pinned ? '고정 해제' : '작업 고정').catch(() => {}); });
    pin.setAttribute('aria-pressed', String(goal.pinned)); statusRow.append(pin); content.append(statusRow);
    content.append(node('p', 'goal-detail-auto goal-help', automationNote(goal)));
    if (connectionNote(goal)) content.append(node('p', 'goal-connection-note', connectionNote(goal)));
    const session = primarySession(goal, state.sessions);
    if (session) {
      const actions = node('div', 'goal-detail-actions');
      actions.append(button(activityOf(goal) === 'attention' ? '대화에서 답하기' : '대화 이어가기', 'primary', () => openSession(session, goal)));
      if (state.options.manageSession) actions.append(button('세션 관리', 'goal-text-button', (event) => manageSession(session, event.currentTarget)));
      content.append(actions);
    }
    const sections = [['최근 결과', resultText(goal)], ['현재 상황', goal.current], ['다음 행동', goal.next]];
    if (['waiting', 'attention'].includes(activityOf(goal))) sections.push([activityOf(goal) === 'attention' ? '확인이 필요한 이유' : '기다리는 이유', goal.waitingReason]);
    if (goal.status === 'done' && clean(goal.evidence)) sections.push(['완료 근거', goal.evidence]);
    for (const [label, text] of sections) {
      if (!clean(text)) continue;
      const section = node('section', 'goal-detail-section'); const heading = node('h2', '', label);
      if (['기다리는 이유', '확인이 필요한 이유'].includes(label) && inferredField(goal, 'waitingReason')) heading.append(node('span', 'goal-inferred-note', '추정'));
      section.append(heading, node('p', '', text)); content.append(section);
    }
    if (!sections.some(([, text]) => clean(text))) content.append(node('p', 'goal-detail-auto goal-help', '연결된 대화에서 최근 활동을 확인할 수 있어요.'));
    if (activityOf(goal) === 'result' && goal.status !== 'done') content.append(node('p', 'goal-result-note', '답변이 도착했어요. 작업 완료 여부는 아직 확인되지 않았습니다.'));
    const linked = node('section', 'goal-detail-section goal-linked'); const heading = node('div', 'goal-section-heading');
    heading.append(node('h2', '', `연결된 세션 ${goal.sessions?.length || 0}개`), button('묶음 교정', 'goal-text-button', (event) => openLinker(goal, null, event.currentTarget))); linked.append(heading);
    const entries = linkedSessions(goal, state.sessions);
    if (!entries.length) linked.append(node('p', 'goal-help', '현재 수집된 세션과 연결된 대화가 없습니다. 새로고침으로 다시 확인할 수 있어요.'));
    for (const entry of parentGroups(entries)) {
      linked.append(renderSessionLink(entry, goal));
      if (entry.children.length) {
        const children = node('details', 'goal-children'); children.dataset.disclosure = `children:${JSON.stringify(entry.ref)}`;
        children.open = state.detailOpen.get(goal.id)?.has(children.dataset.disclosure) || false;
        children.append(node('summary', '', `하위 작업 ${entry.children.length}개`));
        for (const child of entry.children) children.append(renderSessionLink(child, goal)); linked.append(children);
      }
    }
    content.append(linked);
    const history = node('details', 'goal-history'); history.dataset.disclosure = 'history'; history.open = state.detailOpen.get(goal.id)?.has('history') || false;
    history.append(node('summary', '', '정리 근거와 변경 이력'));
    if (goal.project) history.append(node('p', 'goal-help', `프로젝트 · ${goal.project}`));
    if (goal.completionCriteria) history.append(node('p', 'goal-help', `확인할 결과 · ${goal.completionCriteria}`));
    const fieldLabels = { title: '제목', current: '현재 상황', next: '다음 행동', status: '상태', latestResult: '최근 결과' };
    for (const [key, evidence] of Object.entries(goal.automation?.fieldEvidence || {})) {
      if (evidence?.excerpt) history.append(node('p', 'goal-evidence', `${fieldLabels[key] || '대화'} · ${inferredField(goal, key) ? '추정' : '확인 근거'}: ${evidence.excerpt}`));
    }
    const list = node('ol');
    for (const event of [...(goal.history || [])].reverse().slice(0, 30)) {
      const item = node('li'); const time = observedTime(event.at);
      const origin = event.actor?.type === 'inferred' ? '자동 정리' : event.actor?.type === 'agent' ? '에이전트 보고' : '사용자 교정';
      item.append(node('span', '', event.reason || '작업 갱신'), node('small', '', `${event.actor?.label || origin} · ${time} KST`)); list.append(item);
    }
    history.append(list); content.append(history);
    content.append(button(goal.archived ? '보관 해제' : '작업 보관', 'goal-text-button goal-archive', () => { void save(goal, { archived: !goal.archived }, goal.archived ? '보관 해제' : '작업 보관').catch(() => {}); }));
    container.append(content); content.scrollTop = state.detailPositions.get(goal.id) || 0;
  }
  function renderSessionLink({ ref, session }, goal) {
    const row = node('div', 'goal-session-link');
    const open = button('', 'goal-session-open', () => openSession(session, goal)); open.disabled = !session;
    open.append(node('strong', '', session?.title || '현재 목록에 없는 연결 세션'), node('span', '', [session?.hostLabel || ref.host, ref.agent, session ? ORIGINS[originOf(session)] : '새로고침 후 다시 확인'].join(' · ')));
    row.append(open);
    if (ref.point) row.append(node('p', 'goal-session-point', ref.point));
    if (session && state.options.manageSession) row.append(button('세션 관리', 'goal-text-button', (event) => manageSession(session, event.currentTarget)));
    if (goal.automatic && goal.sessions.length > 1) row.append(button('별도 작업으로 분리', 'goal-text-button', async () => {
      try { if (await save(goal, { action: 'split', session: sessionRef(ref) }, '세션을 별도 작업으로 분리', { undo: false })) await refresh(); } catch (_) {}
    }));
    return row;
  }
  function modal(id, title) {
    let dialog = $(id);
    if (!dialog) { dialog = node('dialog', 'plan-dialog sheet goal-dialog'); dialog.id = id; document.body.append(dialog); }
    dialog.replaceChildren();
    const handle = node('div', 'sheet-handle'); handle.setAttribute('aria-hidden', 'true');
    const header = node('div', 'goal-dialog-head dialog-top'); const heading = node('h2', '', title); heading.id = `${id}-title`;
    const close = button('닫기', 'goal-text-button', () => closeModal(dialog)); header.append(heading, close);
    dialog.setAttribute('aria-labelledby', heading.id); dialog.append(handle, header);
    dialog.oncancel = (event) => { event.preventDefault(); closeModal(dialog); };
    return dialog;
  }
  function showModal(dialog, trigger = null) {
    dialog.returnFocus = trigger || document.activeElement;
    if (state.options.getSheet) {
      const sheet = state.options.getSheet(dialog.id); sheet.onDismiss = () => closeModal(dialog); sheet.open({ detent: 'large' });
    } else dialog.showModal();
  }
  function closeModal(dialog) {
    if (state.busy) return;
    if (state.options.getSheet) state.options.getSheet(dialog.id).close(); else dialog.close();
    dialog.returnFocus?.focus({ preventScroll: true });
    if (dialog.id === 'goal-editor') renderBoard();
  }
  function startWork(trigger) { state.options.startWork?.(trigger); }
  function openEditor(goal, preset = {}, trigger = null) {
    if (!goal) { startWork(trigger); return; }
    const dialog = modal('goal-editor', '제목 수정');
    const form = node('form', 'goal-form'); const body = node('div', 'dialog-body goal-form-body');
    const input = node('input'); input.name = 'title'; input.value = goal.title || ''; input.maxLength = 240; input.required = true;
    body.append(field('작업 제목', input));
    if (goal.automatic) body.append(node('p', 'goal-help', '수정한 제목은 이후 자동 갱신에서도 유지됩니다.'));
    const error = node('p', 'error'); error.setAttribute('role', 'alert'); body.append(error);
    const actions = node('div', 'dialog-actions'); actions.append(button('취소', 'secondary', () => closeModal(dialog)));
    const submit = node('button', 'primary', '저장'); submit.type = 'submit'; actions.append(submit);
    form.append(body, actions); dialog.append(form);
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); if (state.busy) return;
      if (!clean(input.value)) { error.textContent = '작업 제목을 입력하세요.'; return; }
      submit.disabled = true; error.textContent = '';
      try { if (await save(goal, { title: clean(input.value) }, '작업 제목 수정')) closeModal(dialog); }
      catch (failure) { error.textContent = failure.message; }
      finally { submit.disabled = false; }
    });
    if (goal.automatic && goal.automation?.overrides?.includes('title')) body.append(button('자동 제목으로 되돌리기', 'goal-text-button', async () => {
      try { if (await save(goal, { resetOverrides: ['title'] }, '자동 제목으로 복원', { undo: false })) closeModal(dialog); }
      catch (failure) { error.textContent = failure.message; }
    }));
    showModal(dialog, trigger); input.focus();
  }
  function openLinker(goal, preselected = null, trigger = null) {
    if (!goal.automatic) { openLegacyLinker(goal, preselected, trigger); return; }
    const dialog = modal('goal-linker', '묶음 교정'); const body = node('div', 'dialog-body');
    body.append(node('p', 'goal-help', '같은 일을 하는 작업을 선택하면 연결된 세션을 함께 모읍니다. 세션 분리는 작업 요약에서 할 수 있어요.'));
    const search = node('input', 'goal-link-search'); search.type = 'search'; search.placeholder = '함께 묶을 작업 검색'; search.setAttribute('aria-label', '함께 묶을 작업 검색');
    const list = node('div', 'goal-link-options'); let selected = null;
    const error = node('p', 'error'); error.setAttribute('role', 'alert');
    const submit = button('함께 묶기', 'primary', async () => {
      if (!selected || state.busy) return;
      submit.disabled = true; error.textContent = '';
      try {
        const saved = await save(goal, { action: 'merge', targetId: selected.id, targetRevision: selected.revision }, '같은 작업으로 묶기', { undo: false });
        if (saved) { closeModal(dialog); await refresh(); if (state.goals.some((item) => item.id === saved.id)) openGoal(saved.id); }
      } catch (failure) { error.textContent = failure.message; }
      finally { submit.disabled = !selected; }
    }); submit.disabled = true;
    function renderOptions() {
      list.replaceChildren();
      const goals = state.goals.filter((item) => item.id !== goal.id && item.automatic && !item.archived && goalMatches(item, search.value, {}, state.sessions));
      for (const item of goals) {
        const radio = node('input'); radio.type = 'radio'; radio.name = 'merge-target'; radio.value = item.id; radio.checked = selected?.id === item.id;
        radio.addEventListener('change', () => { selected = item; submit.disabled = false; });
        const label = node('label', 'goal-link-option'); const copy = node('span');
        copy.append(node('strong', '', item.title), node('small', '', [item.project, `세션 ${item.sessions?.length || 0}개`].filter(Boolean).join(' · ')));
        label.append(radio, copy); list.append(label);
      }
      if (!goals.length) list.append(node('p', 'goal-help', '함께 묶을 작업이 없습니다. 검색어를 바꿔 확인할 수 있어요.'));
    }
    search.addEventListener('input', renderOptions); renderOptions(); body.append(search, list, error);
    const actions = node('div', 'dialog-actions'); actions.append(button('취소', 'secondary', () => closeModal(dialog)), submit);
    dialog.append(body, actions); showModal(dialog, trigger); search.focus();
  }
  function openLegacyLinker(goal, preselected = null, trigger = null) {
    const dialog = modal('goal-linker', '세션 연결');
    const body = node('div', 'dialog-body');
    body.append(node('p', 'goal-help', '여러 세션을 같은 목표에 연결할 수 있어요. 세션은 다른 목표에도 연결할 수 있습니다.'));
    const search = node('input', 'goal-link-search'); search.type = 'search'; search.placeholder = '세션·프로젝트·기기 검색'; search.setAttribute('aria-label', '연결할 세션 검색');
    const list = node('div', 'goal-link-options'); const selected = new Map((goal.sessions || []).map((ref) => [JSON.stringify(sessionRef(ref)), ref]));
    if (preselected) selected.set(JSON.stringify(sessionRef(preselected)), sessionRef(preselected));
    const point = node('input'); point.placeholder = '관련 대화 지점이나 연결 이유 (선택)'; point.maxLength = 1024;
    const pointLabel = field('새 연결의 근거', point);
    function renderOptions() {
      list.replaceChildren();
      const sessions = state.sessions.filter((s) => lower(sessionText(s)).includes(lower(search.value)));
      for (const session of sessions) {
        const ref = sessionRef(session); if (!ref.id) continue;
        const key = JSON.stringify(ref); const check = node('input'); check.type = 'checkbox'; check.checked = selected.has(key);
        check.addEventListener('change', () => { if (check.checked) selected.set(key, ref); else selected.delete(key); });
        const label = node('label', 'goal-link-option'); const copy = node('span');
        copy.append(node('strong', '', session.title || '이름 없는 세션'), node('small', '', [session.hostLabel || session.host, session.project, session.agent, ORIGINS[originOf(session)]].filter(Boolean).join(' · ')));
        label.append(check, copy); list.append(label);
      }
      if (!sessions.length) list.append(node('p', 'goal-help', '조건에 맞는 수집된 세션이 없습니다. 검색어를 바꿔 주세요.'));
    }
    search.addEventListener('input', renderOptions); renderOptions(); body.append(search, list, pointLabel);
    const error = node('p', 'error'); error.setAttribute('role', 'alert'); body.append(error);
    const actions = node('div', 'dialog-actions'); actions.append(button('취소', 'secondary', () => closeModal(dialog)));
    const submit = button('연결 저장', 'primary', async () => {
      if (state.busy) return;
      submit.disabled = true;
      const refs = [...selected.values()].map((ref) => ({ ...ref, ...(!goal.sessions.some((previous) => sameSession(previous, ref)) && clean(point.value) ? { point: clean(point.value) } : {}) }));
      try { if (await save(goal, { sessions: refs }, '연결된 세션 수정')) closeModal(dialog); }
      catch (failure) { error.textContent = failure.message; }
      finally { submit.disabled = false; }
    });
    actions.append(submit); dialog.append(body, actions); showModal(dialog, trigger); search.focus();
  }
  function goalsForSession(session) { return state.goals.filter((goal) => !goal.archived && !goal.automation?.mergedInto && goal.sessions?.some((ref) => sameSession(ref, sessionRef(session)))); }
  function goalNamesForSession(session) { return goalsForSession(session).map((goal) => goal.title).join(' '); }
  function sessionMatches(session, query) { return lower(goalNamesForSession(session)).includes(lower(query)); }
  function attachSessionDetail(session, head) {
    if (!head || !state.init) return;
    const links = node('div', 'session-goal-links'); links.setAttribute('aria-label', '연결된 작업');
    const goals = goalsForSession(session);
    for (const goal of goals) links.append(button(goal.title, 'session-goal-link', () => openGoal(goal.id)));
    if (!goals.length) links.append(button('작업 다시 확인', 'session-goal-link', refresh));
    head.append(links);
  }
  function bindResizer() {
    const handle = $('goal-resizer'); let active = false;
    function resize(clientX) {
      const rect = $('goals-workspace').getBoundingClientRect();
      const width = Math.max(290, Math.min(rect.width - 340, rect.right - clientX));
      $('goals-workspace').style.setProperty('--goal-detail-width', `${width}px`);
      handle.setAttribute('aria-valuenow', String(Math.round(width)));
      handle.setAttribute('aria-valuemax', String(Math.round(rect.width - 340)));
    }
    handle.addEventListener('pointerdown', (event) => { active = true; handle.setPointerCapture(event.pointerId); event.preventDefault(); });
    handle.addEventListener('pointermove', (event) => { if (active) resize(event.clientX); });
    handle.addEventListener('pointerup', () => { active = false; }); handle.addEventListener('pointercancel', () => { active = false; });
    handle.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
      event.preventDefault();
      if (event.key === 'Home') $('goals-workspace').style.removeProperty('--goal-detail-width');
      else resize(handle.getBoundingClientRect().left + (event.key === 'ArrowLeft' ? -24 : 24));
    });
  }
  function locked() {
    state.epoch += 1; state.goals = []; state.sessions = []; state.selected = null; state.recent = [];
    state.detailPositions.clear(); state.detailOpen.clear(); state.groupOpen.clear(); state.cardOrder = []; state.ready = false; state.loading = false; state.lastRefreshAt = 0; state.busy = false; state.undo = null; state.query = '';
    state.filters = { project: '', host: '', origin: '', pinned: false, archived: false };
    restoreConversationNode(); state.tab = 'sessions';
    for (const id of ['goal-editor', 'goal-linker', 'goal-session-picker']) { const dialog = $(id); if (dialog?.open) dialog.close(); dialog?.replaceChildren(); }
    if (state.init) { $('goal-search').value = ''; $('goal-undo').hidden = true; announce(''); render(); recordHistory(true); }
  }
  function init(options) {
    if (state.init) return; state.options = options; state.init = true;
    const dragStatus = node('div', 'sr-only'); dragStatus.id = 'goal-drag-status'; dragStatus.setAttribute('role', 'status'); document.body.append(dragStatus);
    for (const tab of ['goals', 'sessions']) $(`nav-${tab}`).addEventListener('click', () => openTab(tab));
    $('primary-nav').addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault(); const tab = state.tab === 'goals' ? 'sessions' : 'goals'; openTab(tab); $(`nav-${tab}`).focus();
    });
    $('goal-new').addEventListener('click', (event) => startWork(event.currentTarget)); $('goal-empty-new').addEventListener('click', (event) => startWork(event.currentTarget));
    $('goal-refresh').addEventListener('click', refresh);
    $('goal-search').addEventListener('input', (event) => { state.query = event.target.value; state.options.onSearch?.(state.query); renderBoard(); });
    $('goal-filter-toggle').addEventListener('click', () => {
      const hidden = !$('goal-filters').hidden; $('goal-filters').hidden = hidden; $('goal-filter-toggle').setAttribute('aria-expanded', String(!hidden));
    });
    $('goal-reset').addEventListener('click', () => { state.query = ''; $('goal-search').value = ''; state.filters = { project: '', host: '', origin: '', pinned: false, archived: false }; state.options.onSearch?.(''); render(); });
    for (const view of ['list', 'kanban']) $(`goal-view-${view}`).addEventListener('click', () => { state.view = view; renderBoard(); });
    $('goal-undo-action').addEventListener('click', undo); $('goal-undo-close').addEventListener('click', () => { state.undo = null; $('goal-undo').hidden = true; });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !document.querySelector('dialog[open]') && state.tab === 'goals' && state.selected && !state.conversation) closeGoal();
    });
    root.matchMedia(LIST_LAYOUT).addEventListener?.('change', () => { if (!state.view) renderBoard(); });
    bindResizer(); render(); recordHistory(true);
  }
  root.SessionholicGoals = { init, refresh, update, locked, openTab, openGoal, backFromSession, handleHistory, attachSessionDetail,
    sessionMatches, goalNamesForSession, linkedTitles: goalNamesForSession, searchElement: () => state.tab === 'goals' ? $('goal-search') : $('search'), goalsForSession, setQuery(query) { state.query = query; if (state.init) { $('goal-search').value = query; renderBoard(); } }, ...pure };
})(typeof window !== 'undefined' ? window : globalThis);
