/* Goals are independent of conversation, delivery and connection state. */
(function (root) {
  'use strict';
  const STATUSES = [
    ['todo', '할 일'], ['in_progress', '진행 중'], ['waiting', '대기'],
    ['review', '확인 필요'], ['done', '완료'],
  ];
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
  const pure = { STATUSES, ORIGINS, sessionRef, sameSession, originOf, linkedSessions, goalMatches, completionError, editable, parentGroups };
  if (typeof module !== 'undefined' && module.exports) module.exports = pure;
  if (!root.document) return;
  const document = root.document;
  const $ = (id) => document.getElementById(id);
  const state = {
    options: {}, goals: [], sessions: [], selected: null, tab: 'goals', query: '',
    filters: { project: '', host: '', origin: '', pinned: false, archived: false },
    recent: [], detailPositions: new Map(), ready: false, loading: false, busy: false, conversation: false, mutations: 0,
    view: null, sessionKey: null, boardPosition: { top: 0, left: 0 }, undo: null, epoch: 0, init: false,
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
    const epoch = state.epoch; const mutation = state.mutations; state.loading = true;
    $('goal-board').setAttribute('aria-busy', 'true');
    if (!state.ready) announce('작업을 불러오고 있습니다…');
    try {
      const result = await state.options.api('/api/goals');
      if (epoch !== state.epoch || mutation !== state.mutations) return;
      const position = { top: $('goal-board-scroll').scrollTop, left: $('goal-board').scrollLeft };
      state.goals = Array.isArray(result.goals) ? result.goals : [];
      state.ready = true; announce(''); render(); state.options.onChanged?.();
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
      const result = await state.options.api(goal ? `/api/goals/${goal.id}` : '/api/goals', { method: 'POST', body });
      if (epoch !== state.epoch) return null;
      const saved = result.goal;
      if (!saved?.id) throw new Error('저장 결과를 확인하지 못했습니다. 새로고침으로 작업을 확인하세요.');
      state.mutations += 1;
      const index = state.goals.findIndex((value) => value.id === saved.id);
      if (index >= 0) state.goals[index] = saved; else state.goals.push(saved);
      if (undo && goal) showUndo(saved, editable(goal), reason);
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
    const before = JSON.stringify(state.sessions.map((s) => [s.key, s.title, s.project, s.hostLabel, s.initiator, s.initiatorName, s.parentSessionId]));
    state.sessions = Array.isArray(sessions) ? sessions : [];
    const after = JSON.stringify(state.sessions.map((s) => [s.key, s.title, s.project, s.hostLabel, s.initiator, s.initiatorName, s.parentSessionId]));
    if (before !== after) { renderFilters(); renderBoard(); if (!state.conversation) renderDetail(); }
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
    const recent = state.recent.map((id) => state.goals.find((goal) => goal.id === id && !goal.archived)).filter(Boolean);
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
    board.hidden = !!clean(state.query) && !goals.length;
    board.dataset.view = state.view || (root.matchMedia(LIST_LAYOUT).matches ? 'list' : 'kanban');
    $('goal-view-list').setAttribute('aria-pressed', String(board.dataset.view === 'list'));
    $('goal-view-kanban').setAttribute('aria-pressed', String(board.dataset.view === 'kanban'));
    for (const [status, label] of STATUSES) {
      const column = node('section', `goal-column status-${status}`); column.dataset.goalStatus = status;
      column.setAttribute('aria-label', label);
      const items = goals.filter((goal) => goal.status === status);
      const heading = node('div', 'goal-column-heading'); heading.append(node('h2', '', label), node('span', 'goal-column-count', `${items.length}`));
      column.append(heading);
      for (const goal of [...items.filter((g) => g.pinned), ...items.filter((g) => !g.pinned)]) column.append(renderCard(goal));
      if (!items.length) column.append(node('p', 'goal-column-empty', '아직 작업이 없어요'));
      const add = button(`${label} 작업 추가`, 'goal-column-add', (event) => openEditor(null, { status }, event.currentTarget));
      add.prepend(icon('plus')); column.append(add); board.append(column);
    }
    const empty = $('goal-empty'); empty.hidden = goals.length > 0;
    const active = [state.query && `검색 “${state.query}”`, state.filters.project, state.filters.host,
      state.filters.origin && ORIGINS[state.filters.origin], state.filters.pinned && '고정한 작업'].filter(Boolean);
    $('goal-empty-title').textContent = active.length ? '조건에 맞는 작업이 없어요' : '다음에 이어 할 일을 남겨 두세요';
    $('goal-empty-copy').textContent = active.length
      ? `${active.join(' · ')} 조건을 적용했어요. 목표는 오래된 기록도 검색합니다. 연결 세션은 현재 수집된 기록에서 찾습니다.`
      : '목표와 완료 조건을 적고, 관련 세션을 연결하면 기기를 바꿔도 같은 작업을 이어 갈 수 있어요.';
    $('goal-reset').hidden = !active.length;
    renderSearchSessions(); markSelected(); $('goal-board-scroll').scrollTop = position.top; board.scrollLeft = position.left;
    if (focused) document.querySelector(`[data-goal-open="${focused}"]`)?.focus({ preventScroll: true });
  }
  function renderCard(goal) {
    const card = node('article', 'goal-card'); card.dataset.goalId = goal.id;
    const open = button(goal.title, 'goal-card-open', () => openGoal(goal.id)); open.dataset.goalOpen = goal.id;
    if (goal.pinned) { const pin = node('span', 'goal-pinned', '고정'); card.append(pin); }
    card.append(open);
    for (const [label, text] of [['현재', goal.current], ['다음', goal.next]]) {
      const row = node('p', 'goal-card-line'); row.append(node('span', '', label), node('span', '', text || (label === '현재' ? '현재 상황을 남겨 주세요' : '다음 행동을 남겨 주세요'))); card.append(row);
    }
    const linked = linkedSessions(goal, state.sessions);
    const devices = [...new Set(linked.map(({ ref, session }) => `${session?.hostLabel || ref.host} · ${session?.agent || ref.agent}`))];
    const origins = [...new Set(linked.map(({ session }) => ORIGINS[originOf(session)]))];
    const meta = node('p', 'goal-card-meta', [goal.project, ...devices.slice(0, 2), ...origins, `세션 ${linked.length}개`].filter(Boolean).join(' · ')); card.append(meta);
    const footer = node('div', 'goal-card-footer');
    const status = select(STATUSES, goal.status, `${goal.title} 상태 변경`, (event) => changeStatus(goal, event.target.value));
    footer.append(status);
    const grip = button('', 'goal-drag-handle', () => {}); grip.setAttribute('aria-label', `${goal.title} 끌어 이동. 키보드는 옆 상태 선택을 사용하세요.`); grip.append(icon('grip'));
    bindDrag(grip, card, goal); footer.append(grip); card.append(footer); return card;
  }
  function changeStatus(goal, status) {
    if (status === goal.status) return;
    if (completionError({ ...goal, status })) { openEditor(goal, { status }); return; }
    void save(goal, { status }, `${STATUSES.find(([key]) => key === status)[1]}로 이동`).catch(() => renderBoard());
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
    if (container.dataset.goalId && previousContent) state.detailPositions.set(container.dataset.goalId, previousContent.scrollTop);
    const goal = current(); container.replaceChildren(); container.dataset.goalId = goal?.id || ''; if (!goal) return;
    const header = node('header', 'goal-detail-head');
    const back = button('작업으로', 'goal-back', () => closeGoal()); back.setAttribute('aria-label', '작업 목록으로 돌아가기');
    const edit = button('수정', 'goal-text-button', (event) => openEditor(goal, {}, event.currentTarget));
    header.append(back, edit); container.append(header);
    const content = node('div', 'goal-detail-content');
    const title = node('h1', '', goal.title); title.id = 'goal-detail-title'; title.tabIndex = -1; content.append(title);
    const statusRow = node('div', 'goal-detail-status');
    statusRow.append(select(STATUSES, goal.status, '작업 상태', (event) => changeStatus(goal, event.target.value)));
    const pin = button(goal.pinned ? '고정 해제' : '자주 쓰는 작업 고정', 'goal-text-button', () => { void save(goal, { pinned: !goal.pinned }, goal.pinned ? '고정 해제' : '작업 고정').catch(() => {}); });
    pin.setAttribute('aria-pressed', String(goal.pinned)); statusRow.append(pin); content.append(statusRow);
    for (const [label, text] of [['완료 조건', goal.completionCriteria], ['현재 상황', goal.current], ['다음 행동', goal.next]]) {
      const section = node('section', 'goal-detail-section'); section.append(node('h2', '', label), node('p', text ? '' : 'goal-help', text || '아직 적지 않았어요. 수정에서 남길 수 있습니다.')); content.append(section);
    }
    if (goal.status === 'waiting') { const waiting = node('section', 'goal-detail-section'); waiting.append(node('h2', '', '기다리는 이유'), node('p', '', goal.waitingReason)); content.append(waiting); }
    const linked = node('section', 'goal-detail-section goal-linked'); const heading = node('div', 'goal-section-heading');
    heading.append(node('h2', '', `연결된 세션 ${goal.sessions?.length || 0}개`), button('세션 연결', 'goal-text-button', (event) => openLinker(goal, null, event.currentTarget))); linked.append(heading);
    const entries = linkedSessions(goal, state.sessions);
    if (!entries.length) linked.append(node('p', 'goal-help', '조사·구현·검토 세션을 같은 작업에 연결해 보세요.'));
    for (const entry of parentGroups(entries)) {
      linked.append(renderSessionLink(entry, goal));
      if (entry.children.length) {
        const children = node('details', 'goal-children'); children.append(node('summary', '', `하위 세션 ${entry.children.length}개`));
        for (const child of entry.children) children.append(renderSessionLink(child, goal)); linked.append(children);
      }
    }
    content.append(linked);
    const evidence = node('section', 'goal-detail-section'); evidence.append(node('h2', '', '완료 근거'), node('p', goal.evidence ? '' : 'goal-help', goal.evidence || '확인한 결과나 관련 대화 지점을 남겨 주세요.')); content.append(evidence);
    const history = node('details', 'goal-history'); history.append(node('summary', '', '변경 이력과 세부 정보'));
    if (goal.project) history.append(node('p', 'goal-help', `프로젝트 · ${goal.project}`));
    const list = node('ol');
    for (const event of [...(goal.history || [])].reverse().slice(0, 30)) {
      const item = node('li');
      const time = new Date(event.at).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      const origin = event.actor?.type === 'inferred' ? '추정 제안' : event.actor?.type === 'agent' ? '에이전트 보고' : '사용자 변경';
      item.append(node('span', '', event.reason || '작업 수정'), node('small', '', `${event.actor?.label || origin} · ${origin} · ${time} KST`)); list.append(item);
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
    row.append(button('연결 해제', 'goal-text-button', () => { void save(goal, { sessions: goal.sessions.filter((value) => !sameSession(value, ref)) }, '세션 연결 해제').catch(() => {}); }));
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
  function openEditor(goal = null, preset = {}, trigger = null) {
    const value = { title: '', completionCriteria: '', current: '', next: '', waitingReason: '', evidence: '', project: '', status: 'todo', ...goal, ...preset };
    const dialog = modal('goal-editor', goal ? '작업 수정' : '새 작업 시작');
    const form = node('form', 'goal-form'); const body = node('div', 'dialog-body goal-form-body'); const inputs = {};
    for (const [key, label, placeholder, multiline] of [
      ['title', '목표', '무엇을 이루고 싶나요?', false],
      ['completionCriteria', '완료 조건', '어떤 결과가 있으면 완료인가요?', true],
      ['current', '현재 상황', '어디까지 진행했나요?', true],
      ['next', '다음 행동', '다음에 무엇을 하면 되나요?', true],
      ['project', '프로젝트', '프로젝트 이름 (선택)', false],
    ]) {
      const input = node(multiline ? 'textarea' : 'input'); input.value = value[key] || ''; input.name = key;
      input.placeholder = placeholder; input.maxLength = ({ title: 240, completionCriteria: 8000, current: 2000, next: 2000, project: 240 })[key]; if (multiline) input.rows = 2;
      if (key === 'title') input.required = true;
      inputs[key] = input; body.append(field(label, input));
    }
    inputs.status = select(STATUSES, value.status, '목표 상태', () => updateRequirements()); body.append(field('목표 상태', inputs.status));
    for (const [key, label, placeholder] of [['waitingReason', '기다리는 이유', '필요한 응답이나 막힌 이유'], ['evidence', '완료 근거', '확인한 결과 · 링크 · 관련 대화 지점']]) {
      const input = node('textarea'); input.value = value[key] || ''; input.rows = 3; input.maxLength = key === 'evidence' ? 8000 : 2000; input.placeholder = placeholder;
      inputs[key] = input; body.append(field(label, input));
    }
    function updateRequirements() {
      inputs.waitingReason.required = inputs.status.value === 'waiting';
      inputs.evidence.required = inputs.status.value === 'done'; inputs.completionCriteria.required = inputs.status.value === 'done';
    }
    updateRequirements();
    const error = node('p', 'error'); error.setAttribute('role', 'alert'); body.append(error);
    const actions = node('div', 'dialog-actions');
    actions.append(button('취소', 'secondary', () => closeModal(dialog)));
    const submit = node('button', 'primary', goal ? '변경 저장' : '작업 만들기'); submit.type = 'submit'; actions.append(submit);
    form.append(body, actions); dialog.append(form);
    form.addEventListener('submit', async (event) => {
      event.preventDefault(); if (state.busy) return;
      const patch = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
      if (!goal && value.sessions) patch.sessions = value.sessions;
      const invalid = completionError(patch); if (invalid) { error.textContent = invalid; return; }
      submit.disabled = true; submit.textContent = '저장 중…'; error.textContent = '';
      try {
        const saved = await save(goal, patch, goal ? '목표 내용 수정' : '새 작업 만들기');
        if (saved) { closeModal(dialog); openGoal(saved.id); }
      } catch (failure) { error.textContent = failure.message; }
      finally { submit.disabled = false; submit.textContent = goal ? '변경 저장' : '작업 만들기'; }
    });
    showModal(dialog, trigger); inputs.title.focus();
  }
  function openLinker(goal, preselected = null, trigger = null) {
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
  function goalsForSession(session) { return state.goals.filter((goal) => !goal.archived && goal.sessions?.some((ref) => sameSession(ref, sessionRef(session)))); }
  function goalNamesForSession(session) { return goalsForSession(session).map((goal) => goal.title).join(' '); }
  function sessionMatches(session, query) { return lower(goalNamesForSession(session)).includes(lower(query)); }
  function attachSessionDetail(session, head) {
    if (!head || !state.init) return;
    const links = node('div', 'session-goal-links'); links.setAttribute('aria-label', '연결된 작업');
    for (const goal of goalsForSession(session)) links.append(button(goal.title, 'session-goal-link', () => openGoal(goal.id)));
    links.append(button('작업에 연결', 'session-goal-link', () => {
      const dialog = modal('goal-session-picker', '연결할 작업'); const body = node('div', 'dialog-body');
      const goals = state.goals.filter((goal) => !goal.archived);
      for (const goal of goals) body.append(button(goal.title, 'goal-session-result', () => { closeModal(dialog); openLinker(goal, session); }));
      if (!goals.length) body.append(node('p', 'goal-help', '새 작업을 만든 뒤 이 세션을 연결해 주세요.'));
      body.append(button('새 작업 시작', 'goal-text-button', () => { closeModal(dialog); openEditor(null, { sessions: [sessionRef(session)] }); }));
      dialog.append(body); showModal(dialog);
    })); head.append(links);
  }
  function bindDrag(handle, card, goal) {
    let gesture = null;
    function finish(event, cancel = false) {
      if (!gesture) return;
      clearTimeout(gesture.timer);
      const status = gesture.target?.dataset.goalStatus;
      gesture.ghost?.remove(); gesture.target?.classList.remove('goal-drop-target'); card.classList.remove('goal-dragging');
      try { handle.releasePointerCapture(gesture.id); } catch (_) {}
      const moved = gesture.active; gesture = null; if (moved) $('goal-drag-status').textContent = '';
      if (moved && !cancel && status) changeStatus(goal, status);
    }
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || state.busy) return;
      gesture = { id: event.pointerId, x: event.clientX, y: event.clientY, active: false };
      handle.setPointerCapture(event.pointerId);
      gesture.timer = setTimeout(() => {
        if (!gesture) return;
        gesture.active = true; const rect = card.getBoundingClientRect();
        gesture.offsetX = gesture.x - rect.left; gesture.offsetY = gesture.y - rect.top;
        gesture.ghost = card.cloneNode(true); gesture.ghost.classList.add('goal-drag-ghost');
        const ghostStatus = gesture.ghost.querySelector('select'); if (ghostStatus) ghostStatus.value = goal.status;
        gesture.ghost.style.width = `${rect.width}px`; gesture.ghost.style.left = `${rect.left}px`; gesture.ghost.style.top = `${rect.top}px`;
        gesture.ghost.setAttribute('aria-hidden', 'true'); document.body.append(gesture.ghost); card.classList.add('goal-dragging');
        $('goal-drag-status').textContent = `${goal.title} 이동 중. 놓을 상태로 끌어 주세요.`;
      }, event.pointerType === 'mouse' ? 0 : 240);
    });
    handle.addEventListener('pointermove', (event) => {
      if (!gesture) return;
      if (!gesture.active) { if (Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > 12) finish(event, true); return; }
      event.preventDefault();
      gesture.ghost.style.left = `${event.clientX - gesture.offsetX}px`; gesture.ghost.style.top = `${event.clientY - gesture.offsetY}px`;
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-goal-status]');
      if (target !== gesture.target) { gesture.target?.classList.remove('goal-drop-target'); gesture.target = target; target?.classList.add('goal-drop-target'); }
      const board = $('goal-board'); const rect = board.getBoundingClientRect();
      if (event.clientX > rect.right - 48) board.scrollLeft += 18;
      else if (event.clientX < rect.left + 48) board.scrollLeft -= 18;
      if (event.clientY > rect.bottom - 48) board.scrollTop += 18;
      else if (event.clientY < rect.top + 48) board.scrollTop -= 18;
    });
    handle.addEventListener('pointerup', (event) => finish(event));
    handle.addEventListener('pointercancel', (event) => finish(event, true));
    handle.addEventListener('lostpointercapture', (event) => finish(event, true));
    handle.addEventListener('keydown', (event) => { if (event.key === 'Escape') finish(event, true); });
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
    state.detailPositions.clear(); state.ready = false; state.loading = false; state.busy = false; state.undo = null; state.query = '';
    state.filters = { project: '', host: '', origin: '', pinned: false, archived: false };
    restoreConversationNode(); state.tab = 'goals';
    for (const id of ['goal-editor', 'goal-linker', 'goal-session-picker']) { const dialog = $(id); if (dialog?.open) dialog.close(); dialog?.replaceChildren(); }
    if (state.init) { $('goal-search').value = ''; $('goal-undo').hidden = true; announce(''); render(); }
  }
  function init(options) {
    if (state.init) return; state.options = options; state.init = true;
    const dragStatus = node('div', 'sr-only'); dragStatus.id = 'goal-drag-status'; dragStatus.setAttribute('role', 'status'); document.body.append(dragStatus);
    for (const tab of ['goals', 'sessions']) $(`nav-${tab}`).addEventListener('click', () => openTab(tab));
    $('primary-nav').addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault(); const tab = state.tab === 'goals' ? 'sessions' : 'goals'; openTab(tab); $(`nav-${tab}`).focus();
    });
    $('goal-new').addEventListener('click', (event) => openEditor(null, {}, event.currentTarget)); $('goal-empty-new').addEventListener('click', (event) => openEditor(null, {}, event.currentTarget));
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
