/* Phone notifications: this browser subscribes once; the board's service worker shows
   what the board queued for it. The board only wakes the browser and never sends text
   through a push service. */
(function (root) {
  'use strict';
  let options = {}, busy = false, run = 0, mode = 'off';
  const $ = (id) => document.getElementById(id);
  const COPY = {
    unsupported: '이 브라우저는 알림을 지원하지 않아요.',
    ios: '아이폰·아이패드는 Safari에서 공유 → 홈 화면에 추가한 뒤, 홈 화면의 앱으로 열면 알림을 켤 수 있어요.',
    denied: '이 기기에서 보드 알림이 차단되어 있어요. 기기 설정에서 알림을 허용한 뒤 다시 켜 주세요.',
    off: '답이 필요하거나 오래 걸린 작업이 끝나면 이 기기로 알려 드려요. 대화 내용은 알림 서비스로 보내지 않아요.',
    on: '이 기기로 알림을 받고 있어요. 답이 필요하거나 2분 넘게 걸린 작업이 끝나면 알려 드려요.',
    stale: '이 기기의 알림 등록이 보드에서 사라졌어요. 다시 켜면 이어서 받을 수 있어요.',
    checking: '알림 상태를 확인하고 있어요…',
  };
  function supported() {
    return 'serviceWorker' in navigator && 'PushManager' in root && 'Notification' in root;
  }
  function appleTouch() {
    const agent = navigator.userAgent || '';
    return /iP(hone|ad|od)/.test(agent) || (/Macintosh/.test(agent) && (navigator.maxTouchPoints || 0) > 1);
  }
  function standalone() {
    return root.matchMedia?.('(display-mode: standalone)')?.matches === true || navigator.standalone === true;
  }
  function deviceLabel() {
    const agent = navigator.userAgent || '';
    const kind = /iPhone/.test(agent) ? 'iPhone' : /iPad/.test(agent) || appleTouch() ? 'iPad'
      : /Android/.test(agent) ? 'Android' : /Mac/.test(agent) ? 'Mac' : /Windows/.test(agent) ? 'Windows' : '브라우저';
    return kind + (standalone() ? ' 앱' : '');
  }
  function serverKey(value) {
    const text = String(value || '').replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(text + '='.repeat((4 - (text.length % 4)) % 4));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
  }
  function sameKey(subscription, key) {
    const raw = subscription?.options?.applicationServerKey;
    if (!raw) return true;
    const bytes = new Uint8Array(raw);
    return bytes.length === key.length && bytes.every((byte, index) => byte === key[index]);
  }
  // The board names a device by the same digest of its push address.
  async function deviceId(endpoint) {
    const digest = await root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('').slice(0, 32);
  }
  async function registration() {
    const timeout = new Promise((_, fail) => setTimeout(() => fail(new Error('알림 준비가 늦어지고 있어요. 화면을 새로 연 뒤 다시 시도해 주세요.')), 8000));
    return Promise.race([navigator.serviceWorker.ready, timeout]);
  }
  async function current() {
    return (await registration()).pushManager.getSubscription();
  }
  function show(text, next, { toggle = true, test = false } = {}) {
    mode = next;
    if (!$('push-status')) return;
    $('push-status').textContent = text;
    $('push-toggle').hidden = !toggle;
    $('push-toggle').textContent = next === 'on' ? '알림 끄기' : next === 'stale' ? '알림 다시 켜기' : '알림 켜기';
    $('push-toggle').disabled = busy;
    $('push-test').hidden = !test;
    $('push-test').disabled = busy;
  }
  async function render() {
    const mine = ++run;
    if (!supported()) { show(appleTouch() && !standalone() ? COPY.ios : COPY.unsupported, 'none', { toggle: false }); return; }
    if (Notification.permission === 'denied') { show(COPY.denied, 'none', { toggle: false }); return; }
    show(COPY.checking, mode, { toggle: false });
    try {
      const subscription = await current();
      if (!subscription) { if (mine === run) show(COPY.off, 'off'); return; }
      const status = await options.api('/api/push?id=' + await deviceId(subscription.endpoint));
      if (mine !== run) return;
      const live = status?.registered === true && (!status.publicKey || sameKey(subscription, serverKey(status.publicKey)));
      show(live ? COPY.on : COPY.stale, live ? 'on' : 'stale', { test: live });
    } catch (error) {
      if (mine === run && !error?.unauthorized && !error?.staleAuth) show(error?.message || COPY.unsupported, 'off');
    }
  }
  async function enable() {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error(permission === 'denied' ? COPY.denied : '알림 허용을 선택해야 켤 수 있어요.');
    const status = await options.api('/api/push');
    if (!status?.available || !status.publicKey) throw new Error(status?.reason || '이 보드에서는 알림을 보낼 수 없어요.');
    const pushManager = (await registration()).pushManager;
    const key = serverKey(status.publicKey);
    let subscription = await pushManager.getSubscription();
    // A subscription made for another board key cannot be woken with this one.
    if (subscription && !sameKey(subscription, key)) { await subscription.unsubscribe(); subscription = null; }
    subscription ||= await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    await options.api('/api/push/subscribe', { method: 'POST', body: { subscription: subscription.toJSON(), label: deviceLabel() } });
    options.toast?.('이 기기로 알림을 받도록 켰어요.');
  }
  async function disable() {
    const subscription = await current();
    if (!subscription) return;
    try {
      await options.api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: subscription.endpoint } });
    } finally {
      await subscription.unsubscribe();
    }
    options.toast?.('이 기기의 알림을 껐어요.');
  }
  async function act(action) {
    if (busy) return;
    busy = true;
    if ($('push-toggle')) $('push-toggle').disabled = true;
    if ($('push-test')) $('push-test').disabled = true;
    try {
      await action();
    } catch (error) {
      if (!error?.unauthorized && !error?.staleAuth)
        options.toast?.(error?.message || '알림 설정을 바꾸지 못했어요. 다시 시도해 주세요.');
    } finally {
      busy = false;
      await render();
    }
  }
  function toggle() {
    return act(() => (mode === 'on' ? disable() : enable()));
  }
  function test() {
    return act(async () => {
      const subscription = await current();
      if (!subscription) throw new Error('먼저 알림을 켜 주세요.');
      await options.api('/api/push/test', { method: 'POST', body: { endpoint: subscription.endpoint } });
      options.toast?.('테스트 알림을 보냈어요. 잠시 뒤 이 기기에 표시돼요.');
    });
  }
  function init(value) {
    options = value || {};
    $('push-toggle')?.addEventListener('click', toggle);
    $('push-test')?.addEventListener('click', test);
  }
  root.SessionholicPush = { init, render, toggle, test };
})(window);
