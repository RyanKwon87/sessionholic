const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/session_history.js'), 'utf8');
const settle = () => new Promise(setImmediate);

function setup(api) {
  class Node {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.events = {}; this.textContent = ''; this.open = false; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    setAttribute() {}
    addEventListener(type, fn) { this.events[type] = fn; }
    showModal() { this.open = true; }
    close() { this.open = false; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); }
    get isConnected() { return !!this.parent; }
    fire(type) { return this.events[type]?.({ currentTarget: this }); }
  }
  const ids = ['history-enabled', 'history-idle-days', 'history-grace-days', 'history-save', 'history-reload',
    'history-status', 'history-records'];
  const body = new Node('body');
  const nodes = Object.fromEntries(ids.map((id) => [id, new Node()]));
  const all = (node) => [node, ...node.children.flatMap(all)];
  const get = (id) => nodes[id] || all(body).find((n) => n.id === id);
  const document = { body, getElementById: get, createElement: (tag) => new Node(tag) };
  const window = {}, copies = [], toasts = [];
  vm.runInNewContext(source, { document, window, Date, Number, encodeURIComponent,
    navigator: { clipboard: { writeText: async (text) => copies.push(text) } } });
  window.SessionholicHistory.init({ api, toast: (text) => toasts.push(text) });
  return { feature: window.SessionholicHistory, get, all, body, copies, toasts };
}

const record = { key: 'a'.repeat(64), title: '<img onerror=attack()>', state: 'pending', messageCount: 101,
  source: { host: 'fixture', agent: 'codex' }, summary: { purpose: '첫 요청', lastRequest: '최근 요청', lastAnswer: '최근 결과' } };
const status = { policy: { enabled: true, idleDays: 14, graceDays: 7 }, records: [record] };

test('history reading preserves text, paginates every message and can copy a continuation', async () => {
  const calls = [];
  const c = setup(async (url) => {
    calls.push(url);
    if (url === '/api/session-history') return status;
    const offset = Number(new URL(url, 'https://fixture').searchParams.get('offset'));
    return { messages: [{ role: 'user', text: '<script>plain text</script>' + offset, ts: 1 }],
      nextOffset: offset === 0 ? 100 : null };
  });
  await c.feature.load();
  assert.equal(c.get('history-idle-days').value, 14);
  const row = c.get('history-records').children[0];
  assert.equal(row.children[0].textContent, record.title);
  row.children.find((n) => n.tag === 'button' && n.textContent.includes('읽기')).fire('click');
  await settle();
  const dialog = c.get('session-history-dialog');
  const next = dialog.children.find((n) => n.textContent === '다음 대화 더 보기');
  assert.equal(dialog.open, true);
  assert.equal(c.all(dialog).find((n) => n.tag === 'pre' && n.textContent.includes('<script>')).textContent, '<script>plain text</script>0');
  await next.fire('click');
  assert.equal(next.hidden, true);
  assert.ok(calls.some((url) => url.includes('offset=100')));
  await dialog.children.find((n) => n.textContent === '이어가기 내용 복사').fire('click');
  assert.match(c.copies[0], /첫 요청[\s\S]*최근 결과/);
});

test('locking clears private history and ignores an old in-flight response', async () => {
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const c = setup(async (url) => url === '/api/session-history' ? status : waiting);
  await c.feature.load();
  c.get('history-records').children[0].children.find((n) => n.textContent.includes('읽기')).fire('click');
  c.feature.close();
  release({ messages: [{ role: 'assistant', text: 'must stay private' }], nextOffset: null });
  await settle();
  assert.equal(c.get('session-history-dialog'), undefined);
  assert.equal(c.get('history-records').children.length, 0);
  assert.ok(!c.all(c.body).some((n) => n.textContent.includes('must stay private')));
});

test('preservation waits for the server and cannot claim success on a failure', async () => {
  const c = setup(async (url) => { if (url === '/api/session-history') return status; throw new Error('저장 실패'); });
  await c.feature.load();
  const button = c.get('history-records').children[0].children.find((n) => n.textContent === '원본 계속 보존');
  await button.fire('click');
  assert.equal(button.textContent, '원본 계속 보존');
  assert.equal(button.disabled, false);
  assert.deepEqual(c.toasts, ['저장 실패']);
});
