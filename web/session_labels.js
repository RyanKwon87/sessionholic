/* Board display names never change a native session's identity or transcript. */
(function (root) {
  'use strict';
  let options = {}, dialog = null, context = null;
  const confirmed = new Map();
  const $ = (id) => document.getElementById(id);
  function node(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function sourceOf(session) {
    const source = { ...(options.sourceOf?.(session) || session) };
    if (source.agent === 'claude') {
      source.home ||= '.claude';
      if (!source.id && session.kind === 'interactive') source.id = session.sessionId;
    }
    return { host: source.host, agent: source.agent, id: source.id, ...(source.home ? { home: source.home } : {}),
      ...(source.agent === 'claude' && session.sessionId ? { sessionId: session.sessionId } : {}) };
  }
  const keyOf = (source) => JSON.stringify([source.host, source.agent, source.id, source.home || '', source.sessionId || '']);
  // A Claude background worker id can outlive one transcript. The API route
  // includes its transcript proof, and cached names use the same exact identity.
  const cacheKeyOf = (session) => keyOf(sourceOf(session));
  const validSource = (source) => typeof source.host === 'string' && !!source.host &&
    ['codex', 'claude'].includes(source.agent) && typeof source.id === 'string' && !!source.id &&
    (source.agent !== 'codex' || typeof source.home === 'string' && !!source.home);
  function eligible(session) {
    return !!session && options.canEdit?.(session) !== false && validSource(sourceOf(session));
  }
  function decorate(session, snapshotMeta) {
    const source = sourceOf(session), cacheKey = cacheKeyOf(session), record = confirmed.get(cacheKey);
    if (!record) return session;
    // A response issued before a save cannot roll the UI back. Newer snapshots
    // can carry edits from another device and become the new high-water mark.
    if (snapshotMeta?.ok === true && Number.isSafeInteger(snapshotMeta.revision) && snapshotMeta.revision >= record.revision) {
      if (typeof session.displayTitle === 'string' && typeof session.originalTitle === 'string' &&
          (session.titleAlias === null || typeof session.titleAlias === 'string')) {
        confirmed.set(cacheKey, { source, revision: snapshotMeta.revision,
          titleAlias: session.titleAlias, originalTitle: session.originalTitle, displayTitle: session.displayTitle });
        return session;
      }
    }
    return { ...session, titleAlias: record.titleAlias, originalTitle: record.originalTitle, displayTitle: record.displayTitle };
  }
  function close(force = false) {
    if (context?.busy && !force) return;
    const trigger = context?.trigger;
    context = null;
    if (dialog?.open) dialog.close();
    if (!force) trigger?.focus?.({ preventScroll: true });
  }
  function build() {
    if (dialog) return;
    dialog = node('dialog', 'plan-dialog session-label-dialog');
    dialog.id = 'session-label-dialog';
    dialog.setAttribute('aria-labelledby', 'session-label-heading');
    const top = node('div', 'dialog-top');
    const heading = node('h2', null, '제목 수정'); heading.id = 'session-label-heading';
    const body = node('div', 'dialog-body session-label-body');
    const label = node('label', null, '보드에서 표시할 제목'); label.htmlFor = 'session-label-input';
    const input = node('input'); input.id = 'session-label-input'; input.type = 'text'; input.maxLength = 160;
    input.autocomplete = 'off'; input.setAttribute('aria-describedby', 'session-label-help session-label-status');
    const help = node('p', 'muted', '이 보드의 표시명만 바꿉니다. 원본 세션과 대화 내용은 그대로 유지됩니다. 최대 160자.');
    help.id = 'session-label-help';
    const original = node('p', 'session-label-original'); original.id = 'session-label-original';
    const reset = node('button', 'quiet', '원래 제목으로 되돌리기'); reset.type = 'button'; reset.id = 'session-label-reset';
    reset.addEventListener('click', () => save(null));
    const status = node('p', 'error'); status.id = 'session-label-status'; status.setAttribute('role', 'status');
    const actions = node('div', 'dialog-actions');
    const cancel = node('button', 'quiet', '취소'); cancel.id = 'session-label-cancel'; cancel.type = 'button';
    cancel.addEventListener('click', () => close());
    const submit = node('button', 'primary', '저장'); submit.id = 'session-label-save'; submit.type = 'button';
    submit.addEventListener('click', () => save(input.value));
    input.addEventListener('input', () => { if (!context?.busy) { status.textContent = ''; input.removeAttribute('aria-invalid'); } });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); save(input.value); }
    });
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
    body.append(label, input, help, original, reset, status);
    actions.append(cancel, submit);
    dialog.append(top, heading, body, actions);
    document.body.append(dialog);
  }
  function busy(value) {
    for (const id of ['session-label-save', 'session-label-cancel', 'session-label-input']) $(id).disabled = value;
    $('session-label-reset').disabled = value || !context?.session.titleAlias;
    $('session-label-save').textContent = value ? '저장 중…' : '저장';
    dialog.setAttribute('aria-busy', String(value));
  }
  function open(session, trigger = null) {
    if (!eligible(session) || context?.busy) return false;
    build();
    session = decorate(session);
    context = { session, source: sourceOf(session), trigger, epoch: options.getAuthEpoch?.(), busy: false };
    const input = $('session-label-input');
    input.value = session.titleAlias || '';
    input.placeholder = session.originalTitle || session.title || '원래 제목';
    input.removeAttribute('aria-invalid');
    $('session-label-original').textContent = `원래 제목: ${session.originalTitle || session.title || '제목 없음'}`;
    $('session-label-status').textContent = '';
    busy(false);
    if (options.openDialog) options.openDialog(dialog); else dialog.showModal();
    input.focus(); input.select?.();
    return true;
  }
  async function save(value) {
    const current = context;
    if (!current || current.busy || !eligible(current.session)) return;
    const title = value === null ? null : String(value).trim() || null;
    if (title && [...title].length > 160) {
      $('session-label-status').textContent = '제목은 160자 이내로 입력해 주세요.';
      $('session-label-input').setAttribute('aria-invalid', 'true');
      $('session-label-input').focus(); return;
    }
    current.busy = true; busy(true); $('session-label-status').textContent = '';
    try {
      const record = await options.api('/api/session/label', { method: 'POST', body: { source: current.source, title },
        authEpoch: current.epoch, csrfRetry: false });
      if (context !== current || current.epoch !== options.getAuthEpoch?.()) return;
      if (!record || !validSource(record.source || {}) || keyOf(sourceOf(record.source)) !== keyOf(current.source) ||
          !Number.isSafeInteger(record.revision) || record.revision < 0 ||
          (record.titleAlias !== null && typeof record.titleAlias !== 'string') ||
          typeof record.originalTitle !== 'string' || typeof record.displayTitle !== 'string') {
        throw new Error('저장 결과의 세션을 확인하지 못했습니다. 입력한 제목을 확인하고 다시 시도해 주세요.');
      }
      confirmed.set(cacheKeyOf(current.session), { ...record, source: current.source });
      current.busy = false;
      options.onChanged?.(record);
      close();
      options.toast?.(title === null ? '원래 제목으로 되돌렸습니다.' : '보드 표시 제목을 저장했습니다.');
    } catch (error) {
      if (context !== current || current.epoch !== options.getAuthEpoch?.()) return;
      current.busy = false; busy(false);
      $('session-label-status').textContent = error?.message || '제목을 저장하지 못했습니다. 다시 시도해 주세요.';
    }
  }
  function locked() {
    close(true); confirmed.clear();
    if (dialog) {
      $('session-label-input').value = ''; $('session-label-input').placeholder = '';
      $('session-label-original').textContent = ''; $('session-label-status').textContent = '';
    }
  }
  root.SessionholicLabels = { init: (value) => { options = value; }, open, close, locked, decorate, eligible };
})(window);
