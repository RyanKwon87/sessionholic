/* Private histories stay online-only. Render conversation text with textContent, never HTML. */
(() => {
  let api, toast, generation = 0, view = null;
  const $ = (id) => document.getElementById(id);
  const node = (tag, words, className) => {
    const item = document.createElement(tag);
    if (words != null) item.textContent = words;
    if (className) item.className = className;
    return item;
  };
  const action = (words, run) => {
    const item = node('button', words, 'secondary');
    item.type = 'button'; item.addEventListener('click', run); return item;
  };
  const labels = { pending: '원본 정리 예정', deleted: '원본 기록 정리됨', archived: '대화 보존 · 원본 유지',
    preserved: '원본 보존 지정', waiting: '최근 사용 · 정리 취소', blocked: '정리 보류', uncertain: '정리 결과 확인 필요' };
  function close() {
    generation++; view = null;
    const dialog = $('session-history-dialog');
    if (dialog?.open) dialog.close();
    dialog?.remove();
    $('history-records')?.replaceChildren();
  }
  async function more() {
    const current = view, epoch = generation;
    if (!current || current.loading) return;
    current.loading = true;
    current.more.disabled = true;
    try {
      const data = await api(`/api/session-history/conversation?key=${encodeURIComponent(current.key)}&offset=${current.offset}`);
      if (epoch !== generation || view !== current) return;
      for (const message of data.messages) {
        const row = node('article', null, 'history-message');
        const primary = data.source?.agent === 'claude' ? data.source.sessionId || data.source.id : data.source?.id;
        const child = message.threadId && primary && message.threadId !== primary;
        const speaker = child ? `하위 대화 · ${message.role === 'user' ? '요청' : '답변'}` : message.role === 'user' ? '나' : 'AI';
        row.append(node('strong', `${speaker}${message.ts ? ' · ' + new Date(message.ts * 1000).toLocaleString() : ''}`),
          node('pre', message.text));
        current.body.append(row);
      }
      current.offset = data.nextOffset;
      current.more.hidden = data.nextOffset == null;
    } catch (error) {
      if (epoch === generation) current.status.textContent = error.message || '보존 대화를 읽지 못했습니다.';
    } finally {
      current.loading = false; current.more.disabled = false;
    }
  }
  function read(record) {
    const dialog = $('session-history-dialog') || node('dialog', null, 'plan-dialog history-dialog');
    dialog.id = 'session-history-dialog'; dialog.setAttribute('aria-labelledby', 'history-dialog-title');
    dialog.replaceChildren();
    const heading = node('h2', record.title || '보존한 대화'); heading.id = 'history-dialog-title';
    const body = node('div', null, 'history-conversation');
    const status = node('p', '', 'plan-preserve'); status.setAttribute('role', 'status');
    const next = action('다음 대화 더 보기', more);
    const summary = node('details');
    summary.append(node('summary', '대화 요약'), node('pre', `${record.summary?.purpose || ''}\n\n최근 요청\n${record.summary?.lastRequest || ''}\n\n최근 답변\n${record.summary?.lastAnswer || ''}`));
    const copy = action('이어가기 내용 복사', async () => {
      try {
        await navigator.clipboard.writeText(`이전 대화의 발췌 요약입니다. 파일은 별도로 확인하세요.\n\n첫 요청\n${record.summary?.purpose || ''}\n\n최근 요청\n${record.summary?.lastRequest || ''}\n\n최근 답변\n${record.summary?.lastAnswer || ''}`);
        status.textContent = '새 대화에 붙여넣을 내용을 복사했습니다.';
      } catch (_) { status.textContent = '클립보드에 복사하지 못했습니다. 요약의 글을 선택해 복사해 주세요.'; }
    });
    dialog.append(heading, action('닫기', () => { dialog.close(); generation++; view = null; }), summary, copy, body, status, next);
    if (!dialog.isConnected) document.body.append(dialog);
    dialog.addEventListener('cancel', () => { generation++; view = null; }, { once: true });
    view = { key: record.key, body, status, more: next, offset: 0, loading: false };
    dialog.showModal(); more();
  }
  function render(data) {
    $('history-enabled').checked = data.policy.enabled;
    $('history-idle-days').value = data.policy.idleDays;
    $('history-grace-days').value = data.policy.graceDays;
    const list = $('history-records'); list.replaceChildren();
    if (!data.records.length) list.append(node('p', '아직 보존한 대화가 없습니다.', 'plan-preserve'));
    for (const record of data.records) {
      const row = node('article', null, 'history-record');
      row.append(node('strong', record.title || '제목 없는 대화'));
      let status = labels[record.state] || '보존 상태 확인 중';
      if (record.state === 'pending' && record.deleteAfter) status += ` · ${new Date(record.deleteAfter * 1000).toLocaleDateString()}`;
      row.append(node('p', `${status} · ${record.source.host} · ${record.source.agent}`, 'plan-preserve'));
      if (record.reason) row.append(node('p', record.reason, 'plan-preserve'));
      if (record.archiveBytes != null) row.append(node('p', `보존 용량 ${(record.archiveBytes / 1024).toFixed(1)} KiB`, 'plan-preserve'));
      if (record.messageCount) row.append(action(`대화 ${record.messageCount}개 읽기`, () => read(record)));
      if (!['deleted', 'uncertain'].includes(record.state)) row.append(action(record.preserved ? '원본 보존 해제' : '원본 계속 보존', async (event) => {
        const button = event.currentTarget; button.disabled = true;
        const epoch = generation;
        try {
          const result = await api('/api/session-history/preserve', { method: 'POST', body: { key: record.key, preserved: !record.preserved } });
          if (epoch === generation) render(result);
        } catch (error) { if (epoch === generation) toast(error.message); }
        finally { button.disabled = false; }
      }));
      list.append(row);
    }
  }
  async function load() {
    if (!api || !$('history-records')) return;
    const epoch = generation;
    $('history-status').textContent = '대화 보존 상태를 읽고 있습니다…';
    try {
      const data = await api('/api/session-history');
      if (epoch !== generation) return;
      render(data); $('history-status').textContent = data.workerEnabled === false ? '이 실행에서는 자동 정리가 꺼져 있습니다.'
        : data.running ? '대화를 보존하고 있습니다.' : '자동 정리는 매시간 확인합니다.';
    } catch (error) { if (epoch === generation) $('history-status').textContent = error.message || '대화 보존 상태를 읽지 못했습니다.'; }
  }
  function init(options) {
    api = options.api; toast = options.toast;
    $('history-reload')?.addEventListener('click', load);
    $('history-save')?.addEventListener('click', async (event) => {
      const button = event.currentTarget, epoch = generation;
      button.disabled = true;
      try {
        const result = await api('/api/session-history/policy', { method: 'POST', body: {
          enabled: $('history-enabled').checked, idleDays: Number($('history-idle-days').value), graceDays: Number($('history-grace-days').value) } });
        if (epoch !== generation) return;
        render(result); $('history-status').textContent = '대화 보존 설정을 저장했습니다.';
      } catch (error) { if (epoch === generation) $('history-status').textContent = error.message; }
      finally { button.disabled = false; }
    });
  }
  window.SessionholicHistory = { init, load, close };
})();
