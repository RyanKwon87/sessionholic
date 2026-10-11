"use strict";

// Session content is rendered as text. No transcript, draft, auth or terminal data is persisted here.
// The server names this installation in <meta name="sessionholic-instance">: titles, the
// preference prefix, the history marker and the attachment folder. A missing or broken
// value keeps the public name.
const INSTANCE = (() => {
  const names = {
    title: "세션홀릭 - 에이전트 세션 매니저",
    shortName: "세션홀릭",
    storagePrefix: "sessionholic:",
    historyKey: "sessionholic",
    attachmentsDir: ".sessionholic-attachments",
  };
  const valid = {
    title: (value) => value.length > 0 && value.length <= 256,
    shortName: (value) => value.length > 0 && value.length <= 256,
    storagePrefix: (value) => /^[A-Za-z0-9_.:-]{1,32}$/.test(value),
    historyKey: (value) => /^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(value),
    attachmentsDir: (value) => /^[A-Za-z0-9._-]{1,64}$/.test(value) && value !== "." && value !== "..",
  };
  try {
    const meta = document.querySelector?.('meta[name="sessionholic-instance"]');
    const given = JSON.parse(meta?.getAttribute?.("content") || "{}");
    if (given && typeof given === "object" && !Array.isArray(given)) {
      for (const key of Object.keys(names)) {
        if (typeof given[key] === "string" && valid[key](given[key])) names[key] = given[key];
      }
    }
  } catch {
    /* a broken name list keeps the public names */
  }
  return Object.freeze(names);
})();
const GROUPS = [
  { key: "needs_input", label: "내 답이 필요한 작업", tone: "red" },
  { key: "working", label: "진행 중", tone: "amber" },
  { key: "rest", label: "최근 작업", tone: "gray" },
];
const BADGES = {
  needs_input: ["답 필요", "red"],
  working: ["작업 중", "amber"],
  idle: ["열림", "gray"],
  done: ["완료", "gray"],
  closed: ["닫힘", "gray"],
};
const AGENTS = { claude: "Claude Code", codex: "Codex" };
const POLL_MS = 60000;
const RECENT_SECONDS = 3 * 24 * 3600;
// Every touch decision reads its thresholds here, so tuning on a real phone
// changes only this object (distances in CSS px, times in ms, speeds in px/ms).
const GESTURE = Object.freeze({
  longPressMs: 450, // a press held this long opens the menu (cards, messages)
  pressSlopPx: 10, // drift allowed before a press becomes a drag (long press, swipe, pull)
  tapSlopPx: 10, // a tap moves at most this far between finger down and up
  tapMaxMs: 300, // and lifts within this time
  scrollSettleMs: 120, // a touch this soon after the list moved only stops the scroll
  swipeRatio: 1.7, // |dx| ≥ |dy| × this (within about 30° of horizontal) swipes a card
  swipeRevealPx: 96, // width of the action a card swipe reveals
  swipeActionRatio: 0.6, // a swipe past this share of the card runs its action
  swipeFlickSpeed: 0.4, // a flick this fast opens (left) or closes (right) the swipe
  pullTriggerPx: 64, // pull to refresh fires past this (elastic) distance
  pullMaxPx: 110, // the indicator never travels further
  pullResistPx: 160, // larger: the indicator follows the finger longer
  readingHideTravelPx: 32, // scrolling this far one way hides the conversation's top and bottom bars
  readingBottomPx: 24, // mouse layouts: this close to the end shows them again
  jumpShowPx: 160, // the "맨 아래" button appears this far above the end
  largeTitleCollapsePx: 36, // the list's large title folds into the bar past this
  pinchMinFont: 10, // terminal text size range (pinch and A−/A+)
  pinchMaxFont: 24,
  momentumMinSpeed: 0.35, // a terminal flick at least this fast keeps scrolling
  momentumDecay: 0.94, // share of the speed kept per 16 ms frame
  momentumStopSpeed: 0.03, // below this the scroll stops
  momentumMaxMs: 1600, // and it never runs longer
  sheetDismissRatio: 0.25, // a sheet dragged down past this share of its height closes
  sheetDismissSpeed: 0.5, // or flicked down this fast
  sheetFarRatio: 0.5, // past this (or this fast) a large sheet closes instead of shrinking
  sheetFarSpeed: 1.4,
  sheetExpandPx: 48, // a medium sheet dragged up this far (or flicked) grows
  sheetExpandSpeed: 0.5,
});
const INPUT_CLEANUP_NOTICE = "터미널은 닫혔지만 입력 기록 정리가 남았습니다. 목록을 새로고침해 다시 확인해 주세요.";
const $ = (id) => document.getElementById(id);
const prefs = {
  get(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(INSTANCE.storagePrefix + "ui:" + key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(INSTANCE.storagePrefix + "ui:" + key, JSON.stringify(value));
    } catch {
      /* optional preferences */
    }
  },
};
const THEMES = ["auto", "light", "dark"];
const TEXT_SIZES = ["small", "default", "large"];
const state = {
  snapshot: null,
  capabilities: null,
  capabilitiesRevision: null,
  // The server process (bootId) the capabilities and their CSRF token were read from.
  capabilitiesBoot: null,
  refreshDeadline: 0,
  csrf: "",
  selected: null,
  filters: { host: "", agent: "", project: "", account: "" },
  // The status chips above the list ("needs_input" or "working"); tapping the
  // active chip again clears it.
  phaseFilter: "",
  accountFilterRoutes: null,
  search: "",
  showOld: false,
  detailFor: null,
  detailUpdatedAt: null,
  detailRevision: null,
  detailRun: 0,
  route: null,
  plan: null,
  planRun: 0,
  launchBusy: false,
  pollTimer: null,
  pollBusy: false,
  authenticated: false,
  authEpoch: 0,
  loginBusy: false,
  offline: false,
  terminals: [],
  terminalsRefreshing: false,
  terminalsError: "",
  terminal: null,
  terminalVisible: false,
  term: null,
  fit: null,
  terminalAfter: 0,
  terminalEpoch: null,
  terminalRun: 0,
  terminalAbort: null,
  connected: false,
  terminalAlive: true,
  inputQueue: Promise.resolve(),
  inputEpoch: 0,
  inputFailed: false,
  drafts: new Map(),
  composing: false,
  compositionOwner: null,
  draftRevision: 0,
  draftPaste: null,
  closingTerminal: null,
  terminalCloseBusy: false,
  resizeTimer: null,
  lastSize: "",
  fontSize: prefs.get("fontSize", 14),
  screenReaderMode: prefs.get("screenReaderMode", false) === true,
  directTerminalInput: prefs.get("directTerminalInput", null),
  terminalScrollLines: 0,
  terminalScrollPending: 0,
  terminalScrollTimer: null,
  terminalScrollMode: null,
  terminalScrollInFlight: false,
  terminalMomentum: null,
  toastTimer: null,
  chats: new Map(),
  nativeSessions: new Map(),
  // Session key -> { phase, base }: the phase an open conversation's own read
  // found, shown while the list still has the phase `base` it had then.
  livePhases: new Map(),
  chatPollTimer: null,
  chatPollRun: 0,
  chatPollCount: 0,
  // Board look (UI preference only) and phone navigation state.
  theme: normalizeTheme(prefs.get("theme", "auto")),
  // Board text size (UI preference only); the terminal has its own size.
  textSize: normalizeTextSize(prefs.get("textSize", "default")),
  detailShown: false,
  // The conversation's top and bottom bars (phones: a tap shows or hides them).
  chromeVisible: true,
  tapHintShown: false,
  tapHintTimer: null,
  terminalOpenRun: 0,
  assetVersions: null,
  // Live updates (/api/events) and the snapshot version they announce.
  events: null,
  eventsRetryAt: 0,
  snapshotVersion: null,
  eventsSnapshotVersion: null,
  // The server process those versions count in: a restart counts from 0 again.
  serverBoot: null,
  snapshotPending: false,
  snapshotEventTimer: null,
  // The board terminal list's version: the one /api/terminals last returned, the
  // newest a live update announced, and a read asked for while one was in flight.
  terminalsVersion: null,
  eventsTerminalsVersion: null,
  terminalsPending: false,
  terminalsEventTimer: null,
  terminalsReadAt: 0,
};

// --- UI foundation: theme, haptics, screen transitions, bottom sheet -------
// Theme: "auto" follows the device; "light"/"dark" force the board. Only this
// choice is stored, as a UI preference (THEMES is declared above `state`). The
// terminal is always dark, so the browser bar turns dark while it is open.
const THEME_COLORS = { light: "#f2f2f7", dark: "#000000", terminal: "#1c1c1e" };
function normalizeTheme(value) {
  return THEMES.includes(value) ? value : "auto";
}
function applyTheme(theme = state.theme) {
  state.theme = normalizeTheme(theme);
  const root = document.documentElement;
  if (state.theme === "auto") root?.removeAttribute?.("data-theme");
  else root?.setAttribute?.("data-theme", state.theme);
  for (const value of THEMES) {
    const input = $(`theme-${value}`);
    if (input) input.checked = value === state.theme;
  }
  updateThemeColor();
}
function setTheme(theme) {
  const next = normalizeTheme(theme);
  if (next === state.theme) return;
  applyTheme(next);
  prefs.set("theme", next);
  haptic("select");
}
// Board text size: "small"/"large" scale the root font size (style.css section
// 12); "default" removes the attribute. theme-init.js applies the stored
// choice before the first paint.
function normalizeTextSize(value) {
  return TEXT_SIZES.includes(value) ? value : "default";
}
function applyTextSize(size = state.textSize) {
  state.textSize = normalizeTextSize(size);
  const root = document.documentElement;
  if (state.textSize === "default") root?.removeAttribute?.("data-text-size");
  else root?.setAttribute?.("data-text-size", state.textSize);
  for (const value of TEXT_SIZES) {
    const input = $(`text-size-${value}`);
    if (input) input.checked = value === state.textSize;
  }
}
function setTextSize(size) {
  const next = normalizeTextSize(size);
  if (next === state.textSize) return;
  applyTextSize(next);
  prefs.set("textSize", next);
  haptic("select");
  // The composer and terminal measure text; let them follow the new size.
  updateViewport();
}
function updateThemeColor() {
  // Both media-specific metas take the forced color; "auto" restores them.
  const forced = state.terminalVisible ? THEME_COLORS.terminal
    : state.theme === "auto" ? null : THEME_COLORS[state.theme];
  for (const meta of document.querySelectorAll?.('meta[name="theme-color"]') || []) {
    if (meta.dataset && meta.dataset.original == null) meta.dataset.original = meta.getAttribute?.("content") || "";
    const value = forced || meta.dataset?.original;
    if (value && meta.getAttribute?.("content") !== value) meta.setAttribute?.("content", value);
  }
}

// Haptic ticks where the platform has them (Android); silent elsewhere.
const HAPTICS = { select: 8, success: [12, 60, 18], error: [30, 50, 30, 50, 30] };
function haptic(kind = "select") {
  const pattern = HAPTICS[kind];
  if (pattern == null) return false;
  try {
    return globalThis.navigator?.vibrate?.(pattern) === true;
  } catch {
    return false;
  }
}

function prefersReducedMotion() {
  return !!window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}
function phoneLayout() {
  return !!window.matchMedia?.("(max-width: 700px)")?.matches;
}
// A phone held sideways: as short as the board's other short-screen rules.
function shortLayout() {
  return !!window.matchMedia?.("(max-height: 520px)")?.matches;
}
// Height an on-screen keyboard takes from the page. Safari shrinks only the
// visual viewport; Chromium (interactive-widget=resizes-content) shrinks the
// window too, so both are compared with the window's height before a text
// field took the focus (a keyboard only opens for one; a window resized
// without one, as in Stage Manager, is the new full height). Pinch zoom
// (scale) is not a keyboard. A hardware keyboard's shortcut bar stays below
// KEYBOARD_MIN_PX.
const KEYBOARD_MIN_PX = 150;
function editingText() {
  const node = document.activeElement;
  return !!node && (node.isContentEditable === true || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName || ""));
}
function keyboardInset() {
  const width = window.innerWidth || 0;
  const layout = window.innerHeight || 0;
  const viewport = window.visualViewport;
  const visible = viewport?.height ? viewport.height * (viewport.scale || 1) : layout;
  if (state.viewportWidth !== width || !editingText()) {
    state.viewportWidth = width;
    state.viewportTall = layout;
  } else state.viewportTall = Math.max(state.viewportTall || 0, layout);
  return Math.max(0, state.viewportTall - visible);
}
function keyboardOpen() {
  return keyboardInset() > KEYBOARD_MIN_PX;
}
// Phone rules for a conversation's bars (scrolling hides them, a tap toggles
// them, typing folds them, the one-time tap hint) need a touch screen that is
// small: phone-sized, or with an on-screen keyboard that really took the
// space. A wide touch screen (a tablet, with a hardware keyboard or none)
// keeps its bars in place.
function compactChrome() {
  return touchDevice() && (phoneLayout() || shortLayout() || keyboardOpen());
}
// Runs `update` inside a view transition sliding in `direction` ("push",
// "pop", "present" or "dismiss"; style.css section 11). Without support,
// with reduced motion or in a hidden tab it runs at once. Returns the
// transition, or null when `update` already ran.
let navToken = 0;
function navTransition(direction, update) {
  const root = document.documentElement;
  if (typeof document.startViewTransition !== "function" || prefersReducedMotion() || document.hidden || !root?.dataset) {
    update();
    return null;
  }
  const token = ++navToken;
  root.dataset.nav = direction;
  const clear = () => { if (token === navToken) delete root.dataset.nav; };
  let transition;
  try {
    transition = document.startViewTransition(update);
  } catch {
    clear();
    update();
    return null;
  }
  transition?.finished?.then?.(clear, clear);
  return transition || null;
}
// Phone screens: the list or one conversation. State changes at once (chat
// reads depend on it); only the visible switch may wait for the transition.
function setDetailShown(shown, { direction = null, focus = false } = {}) {
  const changed = state.detailShown !== shown;
  state.detailShown = shown;
  // Entering a conversation (again) always starts with its bars shown.
  if (shown && changed) setChromeVisible(true);
  const apply = () => {
    if (shown) document.body.classList.add("show-detail");
    else document.body.classList.remove("show-detail");
    if (focus) {
      if (shown) focusDetailBack();
      else restoreSelectedCardFocus();
    }
  };
  const transition = direction && changed && phoneLayout() ? navTransition(direction, apply) : (apply(), null);
  syncEvents();
  if (chatVisible()) tickWorking();
  else stopWorkingClock();
  return transition;
}

// Bottom sheet for phone dialogs; wider screens keep the centered dialog.
// Reuse: const sheet = BottomSheet.attach(dialog, { detents: ["medium", "large"], onDismiss });
// sheet.open({ detent: "large" }); sheet.setDetent("medium"); sheet.close().
// The <dialog> takes class "plan-dialog sheet" (a .sheet-handle is added when
// missing); its handle, .dialog-top and title are the drag zone. A drag down,
// a tap outside and the caller's own "cancel" handler should all dismiss via
// onDismiss, which may refuse (e.g. while busy): the sheet then springs back.
function sheetRelease({ offset = 0, velocity = 0, height = 0, detent = "medium", detents = ["medium", "large"] } = {}) {
  // offset: px dragged (down is positive); velocity: px/ms at release.
  const size = Math.max(1, height);
  if (offset > 0) {
    const dismiss = offset > size * GESTURE.sheetDismissRatio || velocity > GESTURE.sheetDismissSpeed;
    const far = offset > size * GESTURE.sheetFarRatio || velocity > GESTURE.sheetFarSpeed;
    if (dismiss && !far && detent === "large" && detents.includes("medium")) return "medium";
    return dismiss ? "close" : detent;
  }
  if (offset < 0 && detent !== "large" && detents.includes("large") &&
      (offset < -GESTURE.sheetExpandPx || velocity < -GESTURE.sheetExpandSpeed)) return "large";
  return detent;
}
class BottomSheet {
  static attach(dialog, options = {}) {
    if (!dialog) return null;
    if (!dialog.bottomSheet) dialog.bottomSheet = new BottomSheet(dialog, options);
    return dialog.bottomSheet;
  }
  constructor(dialog, { detents = ["medium", "large"], detent = detents[0], onDismiss = null } = {}) {
    this.dialog = dialog;
    this.detents = detents;
    this.initial = detent;
    this.onDismiss = onDismiss;
    this.drag = null;
    if (!dialog.querySelector?.(".sheet-handle") && typeof dialog.prepend === "function") {
      const handle = el("div", "sheet-handle");
      handle.setAttribute("aria-hidden", "true");
      dialog.prepend(handle);
    }
    dialog.classList?.add("sheet");
    this.setDetent(detent);
    dialog.addEventListener("pointerdown", (event) => this.start(event));
    dialog.addEventListener("pointermove", (event) => this.move(event));
    dialog.addEventListener("pointerup", (event) => this.end(event));
    dialog.addEventListener("pointercancel", (event) => this.end(event, true));
    // A gesture whose pointerup never arrives must not block the next drag.
    dialog.addEventListener("lostpointercapture", (event) => this.end(event, true));
    dialog.addEventListener("click", (event) => this.outsideClick(event));
    // Closed by code mid-drag: forget the gesture.
    dialog.addEventListener("close", () => {
      this.drag = null;
      dialog.classList?.remove("is-dragging");
    });
  }
  open({ detent = this.initial } = {}) {
    this.setDetent(detent);
    this.offset(0);
    // A menu opened by a long press must not close on that finger's release:
    // only a press that starts on the open sheet's backdrop dismisses it.
    this.pressedSinceOpen = false;
    this.pressTarget = null;
    if (!this.dialog.open) this.dialog.showModal();
  }
  close() {
    if (this.dialog.open) this.dialog.close();
  }
  dismiss() {
    if (!this.dialog.open) return;
    if (typeof this.onDismiss === "function") this.onDismiss();
    else this.close();
  }
  setDetent(detent) {
    if (!this.detents.includes(detent)) return;
    this.detent = detent;
    if (this.dialog.dataset) this.dialog.dataset.detent = detent;
  }
  offset(px) {
    this.dialog.style?.setProperty?.("--sheet-drag", `${Math.round(px)}px`);
  }
  start(event) {
    if (this.dialog.open) {
      this.pressedSinceOpen = true;
      this.pressTarget = event.target || null;
    }
    if (this.drag) this.end({ pointerId: this.drag.id }, true);
    if (!this.dialog.open || !phoneLayout() || (event.button || 0) > 0) return;
    const zone = event.target?.closest?.(".sheet-handle, .dialog-top, h2");
    if (!zone || zone.parentElement !== this.dialog || event.target.closest("button, a, input, select, textarea")) return;
    const now = Date.now();
    const rect = this.dialog.getBoundingClientRect?.();
    this.drag = { id: event.pointerId, y: event.clientY, offset: 0, height: rect?.height || this.dialog.offsetHeight || 0,
      samples: [[now, event.clientY]] };
    this.dialog.setPointerCapture?.(event.pointerId);
    this.dialog.classList?.add("is-dragging");
  }
  move(event) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.id) return;
    drag.offset = event.clientY - drag.y;
    drag.samples.push([Date.now(), event.clientY]);
    if (drag.samples.length > 6) drag.samples.shift();
    // Down follows the finger; up resists like a rubber band.
    this.offset(drag.offset >= 0 ? drag.offset : -Math.min(40, Math.sqrt(-drag.offset) * 4));
    if (event.cancelable) event.preventDefault();
  }
  end(event, cancelled = false) {
    const drag = this.drag;
    if (!drag || event?.pointerId !== drag.id) return;
    this.drag = null;
    this.dialog.classList?.remove("is-dragging");
    const now = Date.now();
    const recent = drag.samples.filter(([time]) => now - time < 120);
    const velocity = recent.length >= 2
      ? (recent[recent.length - 1][1] - recent[0][1]) / Math.max(1, recent[recent.length - 1][0] - recent[0][0]) : 0;
    const outcome = cancelled ? this.detent
      : sheetRelease({ offset: drag.offset, velocity, height: drag.height, detent: this.detent, detents: this.detents });
    if (outcome === "close") this.dismiss();
    else if (outcome !== this.detent) {
      this.setDetent(outcome);
      haptic("select");
    }
    // Closed: the exit transition ignores the offset. Still open: spring back.
    this.offset(0);
  }
  outsideClick(event) {
    if (event.target !== this.dialog || !this.dialog.open || !this.pressedSinceOpen) return;
    const rect = this.dialog.getBoundingClientRect?.();
    if (!rect || (event.clientX >= rect.left && event.clientX <= rect.right &&
        event.clientY >= rect.top && event.clientY <= rect.bottom)) return;
    this.dismiss();
  }
}
const SHEETS = {
  "plan-dialog": { onDismiss: () => closePlan() },
  "close-terminal-dialog": { onDismiss: () => cancelTerminalClose() },
  "settings-dialog": { onDismiss: () => closeSettings() },
  "usage-dialog": { onDismiss: () => closeSheet("usage-dialog", "usage") },
  "filter-dialog": { onDismiss: () => closeSheet("filter-dialog", "filter-open") },
  "terminals-dialog": { onDismiss: () => closeSheet("terminals-dialog", "running-terminals") },
  "action-sheet": { detents: ["medium"], onDismiss: () => closeActionSheet() },
};
function sheet(id) {
  return BottomSheet.attach($(id), SHEETS[id]);
}
// Closes a simple sheet and returns focus to the control that opened it.
function closeSheet(id, returnFocus = null) {
  const dialog = $(id);
  if (!dialog?.open) return;
  dialog.close();
  if (returnFocus) $(returnFocus)?.focus?.({ preventScroll: true });
}
function openSettings() {
  applyTheme();
  applyTextSize();
  sheet("settings-dialog").open();
}
function closeSettings() {
  if (!$("settings-dialog").open) return;
  $("settings-dialog").close();
  $("settings")?.focus?.({ preventScroll: true });
}

applyTheme();
applyTextSize();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
// Adds or removes one class through className, so the change is the same for
// real elements and for the test DOM (whose classList is a stub).
function setClass(node, name, on) {
  if (!node) return;
  const names = String(node.className || "").split(/\s+/).filter(Boolean);
  if (names.includes(name) === !!on) return;
  node.className = (on ? [...names, name] : names.filter((item) => item !== name)).join(" ");
}
function button(text, className, action) {
  const node = el("button", className, text);
  node.type = "button";
  if (action) node.addEventListener("click", action);
  return node;
}
function ago(ts, now = Math.floor(Date.now() / 1000)) {
  if (!ts) return "확인 전";
  const d = Math.max(0, now - ts);
  if (d < 60) return "방금";
  if (d < 3600) return `${Math.floor(d / 60)}분 전`;
  if (d < 86400) return `${Math.floor(d / 3600)}시간 전`;
  return `${Math.floor(d / 86400)}일 전`;
}
function clock(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
function requestId() {
  return (
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}
function toast(text) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 3500);
}
function updateInputCleanupNotice(pending) {
  if (pending) notice(INPUT_CLEANUP_NOTICE);
  else if ($("connection-notice").textContent === INPUT_CLEANUP_NOTICE) notice();
}
function notice(text = "") {
  const box = $("connection-notice");
  box.textContent = text;
  box.hidden = !text;
}

function assertAuthEpoch(epoch) {
  if (epoch === state.authEpoch) return;
  const err = new Error("이전 접속에서 시작한 요청입니다.");
  err.staleAuth = true;
  throw err;
}
function beginAuthEpoch() {
  state.authEpoch++;
  state.pollBusy = false;
  state.terminalsRefreshing = false;
  return state.authEpoch;
}
// After a server restart the CSRF token held here is stale (403 csrf_expired),
// and the server refused before doing anything. Reads, and requests it
// deduplicates by requestId, are sent once more with a fresh token. A chat send
// never is: it waits for a new press (sendChat).
const CSRF_RETRY = new Set(["/api/chat/state", "/api/chat/prompt", "/api/plan", "/api/launch",
  "/api/refresh", "/api/usage/refresh"]);
async function api(path, { method = "GET", body, signal, authEpoch = state.authEpoch, csrfRetry = true } = {}) {
  assertAuthEpoch(authEpoch);
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  let response;
  try {
    response = await fetch(path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", cache: "no-store", signal,
    });
  } catch (err) { assertAuthEpoch(authEpoch); throw err; }
  assertAuthEpoch(authEpoch);
  if (response.status === 401) {
    showLogin();
    const err = new Error("로그인이 필요합니다.");
    err.unauthorized = true;
    throw err;
  }
  const data = await response.json().catch(() => ({}));
  assertAuthEpoch(authEpoch);
  if (!response.ok && response.status === 403 && data.code === "csrf_expired" && csrfRetry &&
      method !== "GET" && CSRF_RETRY.has(path)) {
    let refreshed = false;
    try {
      await loadCapabilities(authEpoch, { refreshView: false });
      refreshed = true;
    } catch (err) {
      if (err.unauthorized || err.staleAuth) throw err;
    }
    assertAuthEpoch(authEpoch);
    if (refreshed) return api(path, { method, body, signal, authEpoch, csrfRetry: false });
  }
  if (!response.ok) {
    const err = new Error(
      typeof data.error === "string"
        ? data.error
        : `요청을 완료하지 못했습니다 (${response.status}).`,
    );
    err.status = response.status;
    err.code = data.code;
    err.dispatchState = data.dispatchState;
    if (data.recovery && typeof data.recovery === "object" && !Array.isArray(data.recovery)) err.recovery = data.recovery;
    throw err;
  }
  return data;
}

function allSessions() {
  const out = [];
  for (const host of state.snapshot?.hosts || []) {
    for (const s of [
      ...(host.data?.claude || []),
      ...(host.data?.codex || []),
    ]) {
      // A Claude row listed without an id is told apart by its process (or folder and start).
      const key = [host.name, s.agent, s.home || "", s.id || (s.agent === "claude"
        ? `~${s.pid ?? ""}:${s.sessionId || ""}:${s.cwd || ""}:${s.startedAt ?? ""}` : "")].join("|");
      // The list's own phase stands again once it changed since the conversation was read.
      const live = state.livePhases.get(key);
      if (live && live.base !== s.phase) state.livePhases.delete(key);
      out.push({
        ...s,
        phase: live && live.base === s.phase ? live.phase : s.phase,
        listedPhase: s.phase,
        host: host.name,
        account: sessionAccountLabel({ ...s, host: host.name }),
        hostLabel: host.label,
        stale: !host.ok,
        fetchedAt: host.fetchedAt,
        key,
      });
    }
  }
  for (const session of state.nativeSessions.values()) {
    if (!out.some((s) => s.key === session.key)) out.push({ ...session, account: sessionAccountLabel(session) });
  }
  return out;
}
// The open conversation's own read says what its session does now; the list
// may say so only a minute later (a host without a resident collector). Its
// card, header and counts follow the read until the list's phase changes.
function noteLivePhase(s, data) {
  const listed = allSessions().find((value) => value.key === s?.key);
  if (!listed || listed.listedPhase === undefined && listed.nativeSource || !BADGES[data?.phase] ||
      chatKey(data.route || {}) !== chatKey(sourceOf(listed))) return;
  if (data.phase === listed.listedPhase) state.livePhases.delete(listed.key);
  else state.livePhases.set(listed.key, { phase: data.phase, base: listed.listedPhase });
  // Redrawn only where it shows (a detail not drawn yet takes it when drawn).
  // This read is the news: the header shows it without reading the
  // conversation again, and the list follows.
  if (listed.phase !== data.phase && state.detailFor === listed.key) {
    const shown = allSessions().find((value) => value.key === listed.key);
    state.detailRevision = detailRevision(shown);
    patchDetailMetadata(shown);
    render();
  }
}
function sourceOf(s) {
  const source = {
    host: s.host,
    agent: s.agent,
    id: s.id,
  };
  if (s.home) source.home = s.home;
  return source;
}
function accountOf(s) {
  return s.agent === "claude"
    ? "claude:default"
    : `codex:${s.home || ".codex"}`;
}
function selectedSession() {
  return allSessions().find((s) => s.key === state.selected);
}
function groupOf(s) {
  return s.phase === "needs_input"
    ? "needs_input"
    : s.phase === "working"
      ? "working"
      : "rest";
}
function matches(s, now, { phase = true } = {}) {
  if (phase && state.phaseFilter && groupOf(s) !== state.phaseFilter) return false;
  for (const key of Object.keys(state.filters)) {
    if (key === "account" && state.filters.account && state.accountFilterRoutes) {
      if (!state.accountFilterRoutes.has(accountRouteKey(s))) return false;
      continue;
    }
    if (state.filters[key] && s[key] !== state.filters[key]) return false;
  }
  if (
    !state.showOld &&
    groupOf(s) === "rest" &&
    now - (s.updatedAt || 0) > RECENT_SECONDS
  )
    return false;
  if (!state.search) return true;
  const q = state.search.toLocaleLowerCase();
  return [
    s.title,
    s.project,
    s.cwd,
    s.account,
    s.snippet,
    s.hostLabel,
    AGENTS[s.agent],
  ].some((v) =>
    String(v || "")
      .toLocaleLowerCase()
      .includes(q),
  );
}

function render() {
  if (!state.snapshot) return;
  const now = state.snapshot.serverTime || Date.now() / 1000;
  const sessions = allSessions();
  // The status chips count what the other filters leave, so the inactive
  // chip keeps its number while the other one filters the list.
  const base = sessions.filter((s) => matches(s, now, { phase: false }));
  const shown = state.phaseFilter ? base.filter((s) => groupOf(s) === state.phaseFilter) : base;
  renderHosts(now);
  renderFreshness(now);
  renderCounts(base);
  renderAttention(sessions);
  renderFilters(sessions);
  renderGroups(shown, now);
  $("session-count").textContent = `${shown.length}개`;
  const selected = sessions.find((s) => s.key === state.selected);
  if (!state.selected && !$("detail").children.length) {
    const welcome = el("div", "welcome");
    welcome.append(
      el("span", "welcome-mark", "↗"),
      el("h2", null, "맥락부터 확인하세요."),
      el(
        "p",
        null,
        "작업을 고르면 최근 대화를 볼 수 있습니다.\n이어서 사용할 에이전트와 계정도 여기서 선택하세요.",
      ),
    );
    $("detail").append(welcome);
  }
  if (selected) refreshDetail(selected);
  else if (state.selected) {
    state.selected = null;
    state.detailFor = null;
    // Nothing follows the conversation any more (polling, its live stream).
    stopChatPolling();
    $("detail").replaceChildren(
      el(
        "div",
        "welcome",
        "이 작업을 목록에서 더 이상 찾을 수 없습니다. 새로고침 후 확인해 주세요.",
      ),
    );
    // A phone showed nothing but that conversation, and its back button went
    // with it: back to the list, saying why.
    if (phoneLayout() && state.detailShown && !state.terminalVisible) {
      toast("열어 둔 작업을 목록에서 더 이상 찾을 수 없어 목록으로 돌아왔습니다.");
      backToList();
    } else syncEvents();
  }
}
function renderHosts(now) {
  const box = $("hosts");
  box.replaceChildren();
  for (const host of state.snapshot.hosts) {
    const chip = el(
      "span",
      `host ${host.refreshing ? "checking" : host.ok ? "ok" : "bad"}`,
    );
    chip.append(el("span", "dot"), el("span", null, host.label));
    chip.title = host.refreshing
      ? `${host.label} · 작업 확인 중`
      : host.ok
        ? `${host.label} · ${ago(host.fetchedAt, now)} 확인`
        : `${host.label} · ${host.error || "연결 확인 필요"}${host.fetchedAt ? ` · 마지막 확인 ${ago(host.fetchedAt, now)}` : ""}`;
    chip.setAttribute("aria-label", chip.title);
    box.append(chip);
  }
}
// While hosts are still collecting, say how old the list on screen is; the
// note disappears once the collection finishes.
function renderFreshness(now) {
  const box = $("list-freshness");
  if (!box) return;
  const times = (state.snapshot?.hosts || []).filter((host) => host.refreshing)
    .map((host) => host.fetchedAt).filter((value) => Number.isFinite(value) && value > 0);
  const oldest = times.length ? Math.min(...times) : null;
  const text = oldest && now - oldest >= 60 ? `${ago(oldest, now)} 기준 · 갱신 중` : "";
  if (box.textContent !== text) box.textContent = text;
  box.hidden = !text;
}
// Agent usage (remaining limits) when the board host provides it; a board
// without the endpoint simply never shows the strip.
function usageTone(remaining) {
  return !Number.isFinite(remaining) ? "muted" : remaining < 15 ? "red" : remaining < 40 ? "orange" : "green";
}
function kstShort(iso) {
  const time = Date.parse(iso || "");
  if (!Number.isFinite(time)) return "";
  const date = new Date(time + 9 * 3600 * 1000);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getUTCMonth() + 1}/${date.getUTCDate()} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}
function renderUsage(data) {
  const box = $("usage");
  if (!box) return;
  const providers = data?.available && Array.isArray(data.providers) ? data.providers.filter((item) => item && typeof item === "object") : [];
  if (!providers.length) {
    box.hidden = true;
    box.dataset.revision = "";
    if ($("usage-dialog")?.open) closeSheet("usage-dialog");
    return;
  }
  box.hidden = false;
  const revision = JSON.stringify([providers, data.refreshing, data.fetchedAt, data.error]);
  if (box.dataset.revision === revision) return;
  box.dataset.revision = revision;
  const summary = $("usage-summary");
  const pills = providers.map((provider) => {
    const remaining = Number.isFinite(provider.remaining) ? provider.remaining : null;
    const pill = el("span", `usage-pill ${provider.status === "ready" ? usageTone(remaining) : "muted"}`);
    pill.append(el("span", "usage-short", provider.short || provider.label || ""), el("strong", null, remaining === null ? "—" : `${remaining}%`));
    return pill;
  });
  if (data.refreshing) pills.push(el("span", "usage-state", "조회 중"));
  summary.replaceChildren(...pills);
  box.setAttribute("aria-label", `남은 사용량 ${providers.map((provider) =>
    `${provider.label || provider.short} ${Number.isFinite(provider.remaining) ? provider.remaining + "%" : "확인 불가"}`).join(", ")}. 계정별로 보기`);
  const body = $("usage-body");
  const sections = providers.map((provider) => {
    const section = el("section", "usage-provider");
    section.append(el("h3", null, provider.label || provider.short || "사용량"));
    if (provider.note) section.append(el("p", "usage-note", provider.note));
    for (const row of Array.isArray(provider.rows) ? provider.rows : []) {
      const item = el("div", "usage-row");
      const head = el("div", "usage-row-head");
      head.append(el("strong", null, row.name || ""));
      if (row.credits) head.append(el("span", "usage-credits", `크레딧 ${row.credits}`));
      item.append(head);
      if (row.note) item.append(el("p", "usage-note", row.note));
      for (const window of Array.isArray(row.windows) ? row.windows : []) {
        const remaining = Number.isFinite(window.remaining) ? Math.max(0, Math.min(100, window.remaining)) : null;
        const line = el("div", "usage-window");
        const bar = el("div", "usage-bar");
        const fill = el("span", `usage-fill ${usageTone(remaining)}`);
        if (fill.style) fill.style.width = `${remaining ?? 0}%`;
        bar.append(fill);
        const reset = kstShort(window.resetsAt);
        line.append(el("span", "usage-window-label", window.label || ""), bar,
          el("span", "usage-window-value", remaining === null ? "—" : `${remaining}%`),
          el("span", "usage-window-reset", reset ? `${reset} 초기화` : ""));
        item.append(line);
      }
      section.append(item);
    }
    return section;
  });
  const foot = el("div", "usage-foot");
  const fetched = Number.isFinite(data.fetchedAt) ? clock(data.fetchedAt) : "";
  foot.append(el("span", null, data.error ? data.error : fetched ? `마지막 조회 ${fetched} · 남은 비율` : "남은 비율"));
  const refresh = button(data.refreshing ? "조회 중…" : "새로고침", "usage-refresh", () => loadUsage(true));
  refresh.disabled = !!data.refreshing;
  foot.append(refresh);
  body.replaceChildren(...sections, foot);
}
function openUsage() {
  if ($("usage")?.hidden) return;
  sheet("usage-dialog").open();
  // A sheet opened long after the last read asks for fresh numbers.
  if (Date.now() - (state.usageAt || 0) > 55000) loadUsage();
}
async function loadUsage(force = false) {
  if (state.usageUnsupported || !state.authenticated || networkOffline() || document.hidden) return;
  if (state.usageBusy) return;
  state.usageBusy = true;
  state.usageAt = Date.now();
  try {
    const data = force ? await api("/api/usage/refresh", { method: "POST", body: {} }) : await api("/api/usage");
    state.usage = data;
    renderUsage(data);
    // Follow a refresh the server is running, without polling forever.
    if (data?.refreshing && (state.usageFollow = (state.usageFollow || 0) + 1) <= 15)
      setTimeout(() => loadUsage(false), 4000);
    else if (!data?.refreshing) state.usageFollow = 0;
  } catch (err) {
    if (err.status === 404) { state.usageUnsupported = true; renderUsage(null); }
  } finally {
    state.usageBusy = false;
  }
}
const BASE_TITLE = INSTANCE.title;
function renderAttention(sessions) {
  // The tab title and app icon say when work waits for an answer while this view is elsewhere.
  const waiting = sessions.filter((s) => groupOf(s) === "needs_input").length;
  const title = waiting ? `(${waiting}) 답 필요 · ${INSTANCE.shortName}` : BASE_TITLE;
  if (document.title !== title) document.title = title;
  try {
    if (waiting) navigator.setAppBadge?.(waiting)?.catch?.(() => {});
    else navigator.clearAppBadge?.()?.catch?.(() => {});
  } catch {
    /* badges are optional */
  }
}
// Status chips: "답 필요 N" and "작업 중 N" filter the list to that state;
// tapping the active chip again shows everything. The buttons are kept and
// only relabelled, so keyboard focus stays on a chip while counts change.
const PHASE_LABELS = { needs_input: "답 필요", working: "작업 중" };
function togglePhaseFilter(key) {
  state.phaseFilter = state.phaseFilter === key ? "" : key;
  haptic("select");
  render();
}
function renderCounts(sessions) {
  const box = $("counts");
  const counts = GROUPS.slice(0, 2).map((group) => sessions.filter((s) => groupOf(s) === group.key).length);
  const revision = `${counts.join(",")}|${state.phaseFilter}`;
  if (box.dataset.revision === revision) return;
  box.dataset.revision = revision;
  box.chips ||= new Map();
  const chips = GROUPS.slice(0, 2).map((group, index) => {
    let chip = box.chips.get(group.key);
    if (!chip) {
      chip = button("", "count", () => togglePhaseFilter(group.key));
      chip.dataset.phase = group.key;
      chip.chipLabel = el("span", "count-label");
      chip.append(el("span", "dot"), chip.chipLabel);
      box.chips.set(group.key, chip);
    }
    const n = counts[index];
    const active = state.phaseFilter === group.key;
    chip.className = `count ${group.tone}${n ? "" : " zero"}${active ? " is-active" : ""}`;
    chip.chipLabel.textContent = `${PHASE_LABELS[group.key]} ${n}`;
    chip.setAttribute("aria-pressed", String(active));
    chip.setAttribute("aria-label", `${PHASE_LABELS[group.key]} ${n}개${active ? ", 이것만 보는 중" : ""}`);
    return chip;
  });
  placeChildren(box, chips);
}
// The filter sheet's selects; the button that opens it shows how many apply.
const FILTER_FIELDS = ["filter-host", "filter-agent", "filter-project", "filter-account"];
function renderFilters(sessions) {
  const active = Object.values(state.filters).filter(Boolean).length + (state.showOld ? 1 : 0);
  $("filter-count").textContent = active ? String(active) : "";
  const open = $("filter-open");
  if (open) {
    setClass(open, "is-active", active > 0);
    open.setAttribute?.("aria-label", active ? `필터 ${active}개 적용됨` : "필터");
  }
  const box = $("filter-fields");
  // A select being used keeps its element (and the open picker).
  if (FILTER_FIELDS.includes(document.activeElement?.id)) return;
  box.replaceChildren();
  for (const [key, label, pick] of [
    ["host", "기기", (s) => [s.host, s.hostLabel]],
    ["agent", "에이전트", (s) => [s.agent, AGENTS[s.agent]]],
    ["project", "프로젝트", (s) => [s.project, s.project]],
    ["account", "계정 설정", (s) => [s.account, s.account]],
  ]) {
    const options = new Map(sessions.map(pick).filter(([v]) => v));
    const field = el("label", null, label);
    const select = el("select");
    select.id = `filter-${key}`;
    select.append(new Option("전체", ""));
    for (const [value, text] of [...options].sort((a, b) =>
      String(a[1]).localeCompare(String(b[1]), "ko"),
    ))
      select.append(new Option(text, value));
    if (state.filters[key] && !options.has(state.filters[key]))
      select.append(new Option(`${state.filters[key]} · 마지막 선택`, state.filters[key]));
    select.value = state.filters[key];
    select.addEventListener("change", () => {
      state.filters[key] = select.value;
      if (key === "account") state.accountFilterRoutes = null;
      render();
    });
    field.append(select);
    box.append(field);
  }
}
function resetFilters() {
  for (const key of Object.keys(state.filters)) state.filters[key] = "";
  state.accountFilterRoutes = null;
  state.showOld = false;
  if ($("show-old")) $("show-old").checked = false;
  renderFilters(allSessions());
  render();
}
function cardLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}
function cardProject(s) {
  const project = cardLine(s.project);
  return project === "~" ? "홈 폴더" : project || "프로젝트 확인 필요";
}
function cardFolderParts(s) {
  const cwd = cardLine(s.cwd).replace(/\\/g, "/").replace(/\/+$/, "");
  if (!cwd) return cardLine(s.cwd).startsWith("/") ? ["최상위 폴더"] : [];
  if (cwd === "~" || cwd === "/root") return ["홈 폴더"];
  if (cwd === ".") return ["현재 폴더"];
  let parts = cwd.split("/").filter(Boolean);
  if (parts[0] === "~") parts = parts.slice(1);
  else if (["users", "home"].includes(parts[0]?.toLowerCase()) && parts.length >= 2) parts = parts.slice(2);
  else if (/^[A-Za-z]:$/.test(parts[0] || "") && parts[1]?.toLowerCase() === "users" && parts.length >= 3) parts = parts.slice(3);
  return parts.length ? parts : ["홈 폴더"];
}
function cardFolderHints(shown) {
  const folders = shown.map((s) => ({ s, project: cardProject(s), parts: cardFolderParts(s), cwd: cardLine(s.cwd) }));
  const hints = new Map();
  const suffix = (parts, depth) => parts.slice(-depth).join(" / ");
  for (const folder of folders) {
    const { s, project, parts, cwd } = folder;
    if (!parts.length) { hints.set(s.key, ""); continue; }
    const peers = folders.filter((other) => other.project === project && other.cwd !== cwd && other.parts.length);
    if (!peers.length && parts.at(-1).toLocaleLowerCase() === project.toLocaleLowerCase()) {
      hints.set(s.key, ""); continue;
    }
    let depth = Math.min(2, parts.length);
    while (depth < parts.length && peers.some((other) => suffix(other.parts, depth) === suffix(parts, depth))) depth++;
    hints.set(s.key, suffix(parts, depth));
  }
  return hints;
}
function placeChildren(parent, desired) {
  for (const child of [...parent.children]) if (!desired.includes(child)) parent.removeChild(child);
  for (let index = 0; index < desired.length; index++)
    if (parent.children[index] !== desired[index]) parent.insertBefore(desired[index], parent.children[index] || null);
}
function renderGroups(shown, now) {
  const box = $("groups");
  const pane = $("task-list-pane");
  const scroll = pane?.scrollTop;
  const active = document.activeElement;
  const sections = new Map();
  const cards = new Map();
  // Reuse only what is mounted now; lock clears this DOM and its references.
  for (const section of box.children) {
    if (section.dataset?.groupKey) sections.set(section.dataset.groupKey, section);
    for (const node of section.children || [])
      if (node.dataset?.sessionKey) cards.set(node.dataset.sessionKey, node);
  }
  const focused = active?.dataset?.sessionKey && cards.get(active.dataset.sessionKey)?.card === active ? active : null;
  const folders = cardFolderHints(shown);
  const desired = [];
  const visible = new Set();
  for (const group of GROUPS) {
    const items = shown
      .filter((s) => groupOf(s) === group.key)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    if (!items.length) continue;
    let section = sections.get(group.key);
    if (!section) {
      section = el("section", "group");
      section.dataset.groupKey = group.key;
      const head = el("h2", `group-head ${group.tone}`, group.label);
      const count = el("span", "group-count");
      head.append(count);
      section.cardHeading = head;
      section.cardCount = count;
    }
    section.cardCount.textContent = items.length;
    const children = [section.cardHeading];
    for (const session of items) {
      const source = chatKey(sourceOf(session));
      const previous = cards.get(session.key);
      const row = previous?.dataset.sourceKey === source ? previous : card(session);
      updateCard(row.card, session, now, folders.get(session.key));
      children.push(row); visible.add(row.card);
    }
    placeChildren(section, children);
    desired.push(section);
  }
  let loading = false;
  if (!shown.length) {
    const searching = !!state.search;
    const filtering = Object.values(state.filters).some(Boolean);
    const unconfigured = !state.snapshot?.hosts?.length;
    const offline = state.snapshot?.hosts?.length && state.snapshot.hosts.every((host) => !host.ok && !host.refreshing);
    const clearAll = () => {
      state.search = "";
      state.phaseFilter = "";
      for (const key of Object.keys(state.filters)) state.filters[key] = "";
      state.accountFilterRoutes = null;
      $("search").value = "";
      render();
    };
    let empty;
    if (state.phaseFilter && !searching && !filtering) {
      empty = emptyState("filter", state.phaseFilter === "needs_input" ? "답이 필요한 작업이 없습니다." : "진행 중인 작업이 없습니다.",
        "다른 작업은 필터를 해제하면 보입니다.", button("필터 해제", "secondary", () => togglePhaseFilter(state.phaseFilter)));
    } else if (searching || filtering || state.phaseFilter) {
      empty = emptyState("search", "조건에 맞는 작업이 없습니다.",
        searching ? `‘${state.search}’로 찾은 작업이 없습니다. 검색어나 필터를 바꿔 보세요.` : "필터를 바꾸거나 초기화해 보세요.",
        button("검색·필터 초기화", "secondary", clearAll));
    } else if (unconfigured) {
      empty = emptyState("setup", "연결할 기기가 아직 설정되지 않았습니다.",
        "설치 안내에 따라 기기를 설정한 뒤 새로고침해 주세요.");
    } else if (offline) {
      empty = emptyState("offline", "기기에 연결되지 않아 작업을 확인하지 못했습니다.",
        "기기 연결과 에이전트 설치 상태를 확인한 뒤 다시 확인해 주세요.", button("다시 확인", "secondary", manualRefresh));
    } else if (state.snapshot?.hosts?.some((host) => host.refreshing)) {
      loading = true;
      empty = skeletonList("기기에서 작업을 확인하고 있습니다…");
    } else {
      empty = emptyState("none", "표시할 최근 작업이 없습니다.",
        "설정한 기기에서 Claude Code나 Codex로 대화를 시작한 뒤 새로고침해 주세요. 3일 지난 대화는 필터에서 볼 수 있습니다.");
    }
    desired.push(empty);
  }
  box.setAttribute?.("aria-busy", String(loading));
  placeChildren(box, desired);
  if (focused && visible.has(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
  if (typeof scroll === "number") pane.scrollTop = scroll;
}
// Empty and loading states of the list: an icon, a short title, what to do,
// and at most one action.
function emptyState(kind, title, body = "", action = null) {
  const box = el("div", `empty-list empty-${kind}`);
  const icon = el("span", "empty-icon");
  icon.setAttribute("aria-hidden", "true");
  box.append(icon, el("p", "empty-title", title));
  if (body) box.append(el("p", "empty-body", body));
  if (action) box.append(action);
  return box;
}
function skeletonList(label = "작업을 확인하고 있습니다…") {
  const wrap = el("div", "skeleton-wrap");
  const list = el("div", "skeleton-list");
  list.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 4; index++) {
    const item = el("div", "skeleton-card");
    item.append(el("span"), el("span"), el("span"));
    list.append(item);
  }
  const note = el("p", "sr-only", label);
  note.setAttribute("role", "status");
  wrap.append(list, note);
  return wrap;
}
function cardTitle(s) {
  const rawTitle = cardLine(s.title);
  return !rawTitle || ["(제목 없음)", "(이름 없음)"].includes(rawTitle) ? "제목 없는 작업" : rawTitle;
}
// A card is three short lines (title, a one-line preview, status · agent ·
// project · time) with a disclosure chevron; device and account stay in its
// accessible name and in the conversation header. The row holds the card and
// the action a left swipe reveals.
function card(s) {
  const row = el("div", "card-row");
  row.dataset.sessionKey = s.key;
  row.dataset.sourceKey = chatKey(sourceOf(s));
  // Touch shortcut only; the same actions open from a long press, a right
  // click or the conversation itself, so it stays out of the focus order.
  const action = button("", "card-swipe-action", () => runSwipeAction(row));
  action.tabIndex = -1;
  action.setAttribute("aria-hidden", "true");
  const node = button("", "card", () => {
    if (state.swipeOpen === row) { closeSwipe(row); return; }
    const mounted = [...$("groups").children].some((section) => [...(section.children || [])].includes(row));
    const current = allSessions().find((session) => session.key === row.dataset.sessionKey);
    if (state.authenticated && mounted && current && chatKey(sourceOf(current)) === row.dataset.sourceKey) select(current.key);
  });
  node.dataset.sessionKey = s.key;
  node.dataset.sourceKey = row.dataset.sourceKey;
  const title = el("span", "card-title");
  const snippet = el("span", "card-snippet");
  const meta = el("span", "card-meta");
  const badge = el("span", "badge");
  const agent = el("span", "card-agent");
  const project = el("span", "card-project");
  const stale = el("span", "stale-tag", "이전 정보");
  const time = el("span", "card-time");
  // A live board terminal of this session (renderTerminalMarks keeps it current).
  const terminal = terminalMark("card-terminal");
  meta.append(badge, terminal, agent, project, stale, time);
  const chevron = el("span", "card-chevron");
  chevron.setAttribute("aria-hidden", "true");
  node.append(title, snippet, meta, chevron);
  node.cardParts = { title, snippet, badge, terminal, agent, project, stale, time };
  node.cardRow = row;
  row.append(action, node);
  row.card = node;
  row.swipeAction = action;
  return row;
}
function updateCard(node, s, now, folderHint = "") {
  const p = node.cardParts;
  const write = (target, value) => { if (target.textContent !== value) target.textContent = value; };
  const project = cardProject(s);
  const rawTitle = cardLine(s.title);
  const title = cardTitle(s);
  const snippet = cardLine(s.snippet);
  const preview = snippet && snippet !== rawTitle && snippet !== title ? snippet : "";
  const agent = AGENTS[s.agent] || s.agent || "에이전트";
  const host = s.hostLabel || s.host || "기기 확인 필요";
  const account = cardLine(s.account);
  const [label, tone] = BADGES[s.phase] || [s.phase || "상태 확인 필요", "gray"];
  const time = ago(s.updatedAt, now);
  node.className = `card${state.selected === s.key ? " selected" : ""}`;
  node.setAttribute("aria-pressed", String(state.selected === s.key));
  node.cardLabel = ["프로젝트: " + project, "작업: " + title,
    preview ? "대화 미리보기: " + preview : "", folderHint ? "작업 폴더: " + folderHint : "",
    label, agent, "기기: " + host, account ? "계정 설정: " + account : "", time,
    s.stale ? "마지막 확인 기준" : ""].filter(Boolean).join(". ");
  markCardTerminal(node, s);
  node.title = `${project}\n${title}`;
  const place = folderHint ? `${project} · ${folderHint}` : project;
  write(p.title, title); p.title.title = title;
  write(p.snippet, preview); p.snippet.hidden = !preview; p.snippet.title = preview;
  write(p.badge, label); p.badge.className = `badge ${tone}`;
  write(p.agent, agent); p.agent.className = `card-agent ${s.agent}`;
  write(p.project, place); p.project.title = place;
  write(p.time, time);
  p.stale.hidden = !s.stale;
  p.stale.title = s.stale ? "기기에 연결되지 않아 마지막으로 확인한 정보입니다." : "";
}

// --- Gestures: long press menus and card swipes --------------------------------
// A long press (or a right click) opens the same action sheet as the visible
// controls; a left swipe on a card reveals its terminal action. The decisions
// are pure functions so their timing and angles are testable (thresholds: GESTURE).
// What a drag is once it passes the slop: "swipe" (sideways: leftward, or
// either way on an open row), "scroll" (anything steeper, or rightward on a
// closed row), or null while still undecided.
function swipeIntent(dx, dy, open = false) {
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  if (Math.max(ax, ay) < GESTURE.pressSlopPx) return null;
  if (ax < ay * GESTURE.swipeRatio) return "scroll";
  return dx < 0 || open ? "swipe" : "scroll";
}
// Where a released swipe settles. offset: px from rest (left is negative);
// velocity: px/ms (left is negative). "action" runs it (a pull past 60% of
// the card), "open" keeps the button revealed, "close" springs back.
function swipeRelease({ offset = 0, velocity = 0, width = 0, reveal = GESTURE.swipeRevealPx } = {}) {
  const size = Math.max(1, width);
  if (offset <= -size * GESTURE.swipeActionRatio) return "action";
  if (velocity > GESTURE.swipeFlickSpeed) return "close";
  if (offset <= -reveal * 0.5 || (velocity < -GESTURE.swipeFlickSpeed && offset < -GESTURE.pressSlopPx)) return "open";
  return "close";
}
// One press at a time: fires onPress after GESTURE.longPressMs unless the
// pointer moved past GESTURE.pressSlopPx, lifted or was cancelled (scrolling)
// first. Mouse buttons use the context menu instead.
function createLongPress(onPress) {
  let press = null;
  const tracker = {
    start(event, target) {
      tracker.cancel();
      if (!target || event?.pointerType === "mouse" || event?.isPrimary === false) return;
      press = { id: event.pointerId, x: event.clientX, y: event.clientY, target, fired: false };
      const current = press;
      press.timer = setTimeout(() => {
        if (press !== current || current.fired) return;
        current.fired = true;
        onPress(current.target, current);
      }, GESTURE.longPressMs);
    },
    move(event) {
      if (!press || press.fired || event?.pointerId !== press.id) return;
      if (Math.hypot((event.clientX || 0) - press.x, (event.clientY || 0) - press.y) > GESTURE.pressSlopPx) tracker.cancel();
    },
    // True when this press already opened its menu: its click is ignored.
    end(event) {
      if (!press || (event && event.pointerId !== press.id)) return false;
      const fired = press.fired;
      clearTimeout(press.timer);
      press = null;
      return fired;
    },
    // Android sends contextmenu for a long press; it opens the menu at once.
    fire() {
      if (!press || press.fired) return false;
      clearTimeout(press.timer);
      press.fired = true;
      onPress(press.target, press);
      return true;
    },
    cancel() {
      if (press) clearTimeout(press.timer);
      press = null;
    },
    get active() {
      return press;
    },
  };
  return tracker;
}
const PRESS_MENU_SKIP = "button:not(.card):not(.tool-group-toggle):not(.tool-item), input, textarea, select, summary";
// Long press, right click and the context-menu key open `open(row, point)`;
// the click a long press leaves behind is swallowed.
function bindPressMenu(root, rowOf, open, { onDown = null, onMove = null, onUp = null } = {}) {
  const press = createLongPress((row) => {
    root.pressedAt = Date.now();
    haptic("select");
    open(row, null);
  });
  root.addEventListener("pointerdown", (event) => {
    onDown?.(event);
    // Cards and tool records are buttons that also take a long press (their menu).
    if (event.target?.closest?.(PRESS_MENU_SKIP)) return;
    press.start(event, rowOf(event.target));
  });
  root.addEventListener("pointermove", (event) => {
    press.move(event);
    if (onMove?.(event)) press.cancel();
  });
  root.addEventListener("pointerup", (event) => {
    // `fired`: this press already opened its menu, so it was not a tap.
    const fired = press.end(event);
    if (fired) root.pressedAt = Date.now();
    onUp?.(event, false, fired);
  });
  root.addEventListener("pointercancel", (event) => {
    press.cancel();
    onUp?.(event, true);
  });
  root.addEventListener("contextmenu", (event) => {
    const row = rowOf(event.target);
    if (!row) return;
    if (press.active) {
      event.preventDefault();
      if (press.fire()) root.pressedAt = Date.now();
      return;
    }
    if (Date.now() - (root.pressedAt || 0) < 1000) {
      event.preventDefault();
      return;
    }
    // A text selection keeps the browser's own menu (its copy works on it).
    const selection = window.getSelection?.();
    if (selection && !selection.isCollapsed && row.contains?.(selection.anchorNode)) return;
    event.preventDefault();
    open(row, { x: event.clientX, y: event.clientY });
  });
  root.addEventListener("click", (event) => {
    // The button a swipe just revealed takes its first tap.
    if (event.target?.closest?.(".card-swipe-action")) return;
    if (Date.now() - (root.pressedAt || 0) > 700) return;
    root.pressedAt = 0;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  return press;
}

// Cards: swipe left for the terminal action, long press for all actions.
function sessionForRow(row) {
  return row ? allSessions().find((session) => session.key === row.dataset?.sessionKey) || null : null;
}
// The alive board terminal that belongs to a session: its attached runtime
// (nativeSource) first, then the session it was launched from (sourceKey).
function terminalForSession(s) {
  if (!s) return null;
  const key = chatKey(sourceOf(s));
  const alive = state.terminals.filter((terminal) => terminal && terminal.alive !== false && !terminal.closed);
  const launchedFrom = (info) => {
    try {
      const source = JSON.parse(info.sourceKey || "null");
      return source && typeof source === "object" ? chatKey(source) : "";
    } catch {
      return "";
    }
  };
  return alive.find((terminal) => {
    const native = terminalInfo(terminal).nativeSource;
    return native?.host && chatKey(native) === key;
  }) || alive.find((terminal) => launchedFrom(terminalInfo(terminal)) === key) || null;
}
// "터미널 열림": the list chip's terminal glyph with a green dot. Hidden until the
// session has a live board terminal (terminalForSession).
function terminalMark(className) {
  const mark = el("span", `terminal-mark ${className}`);
  const icon = el("span", "terminal-mark-icon", "›_");
  icon.setAttribute("aria-hidden", "true");
  mark.append(icon, el("span", "terminal-mark-label", "터미널 열림"));
  mark.hidden = true;
  return mark;
}
function markCardTerminal(node, s) {
  const mark = node?.cardParts?.terminal;
  if (!mark) return;
  const open = !!terminalForSession(s);
  if (mark.hidden !== !open) mark.hidden = !open;
  if (typeof node.cardLabel === "string")
    node.setAttribute("aria-label", open ? `${node.cardLabel}. 터미널 열림` : node.cardLabel);
}
// The terminal list changed (a read, an open or a close here, or a live update):
// mounted cards and the open conversation's header follow without a full render.
function renderTerminalMarks() {
  const sessions = new Map(allSessions().map((s) => [s.key, s]));
  for (const section of $("groups")?.children || [])
    for (const row of section.children || [])
      if (row.card?.cardParts) markCardTerminal(row.card, sessions.get(row.dataset?.sessionKey));
  const selected = sessions.get(state.selected);
  if (selected) renderDetailTerminal(selected);
}
function renderDetailTerminal(s) {
  if (!s || $("detail").dataset?.key !== s.key) return;
  const box = $("detail-terminal");
  if (!box) return;
  const terminal = terminalForSession(s);
  box.hidden = !terminal;
  $("detail-terminal-open")?.setAttribute("aria-label",
    terminal ? `열린 터미널로 가기: ${terminal.title || s.title || "열어 둔 터미널"}` : "열린 터미널로 가기");
}
// "터미널로" in the conversation header: the terminal is looked up again on the
// tap, so one closed meanwhile is never opened (the header just hides it).
function goToSessionTerminal(key) {
  const s = selectedSession();
  if (!s || s.key !== key || state.launchBusy || !state.authenticated) return;
  const terminal = terminalForSession(s);
  if (!terminal) {
    renderDetailTerminal(s);
    return;
  }
  openTerminal(terminal);
}
function openSessionTerminal(s) {
  if (!s || state.launchBusy || !state.authenticated) return;
  const terminal = terminalForSession(s);
  if (terminal) {
    openTerminal(terminal);
    return;
  }
  // Opening a new terminal is reviewed from the conversation (plan sheet).
  if (state.selected !== s.key) select(s.key);
  const current = selectedSession();
  if (current?.key === s.key) openChatTerminal(current);
}
function swipeActionFor(s) {
  const terminal = terminalForSession(s);
  if (!terminal) return { kind: "open", label: "터미널 열기", s };
  const copy = terminalCloseCopy(terminal);
  return { kind: "close", label: copy.action, danger: copy.danger, terminal, s };
}
function setSwipeOffset(row, px) {
  row?.style?.setProperty?.("--swipe-x", `${Math.round(px)}px`);
  if (row) row.swipeOffset = px;
}
function prepareSwipe(row) {
  const action = swipeActionFor(sessionForRow(row));
  row.pendingSwipe = action;
  row.swipeAction.textContent = action.label;
  row.swipeAction.className = `card-swipe-action ${action.kind === "close" ? `is-close${action.danger ? " is-danger" : ""}` : "is-open"}`;
  setClass(row, "is-swiping", true);
}
function closeSwipe(row = state.swipeOpen) {
  if (!row) return;
  setSwipeOffset(row, 0);
  setClass(row, "is-swiping", false);
  setClass(row, "is-open", false);
  if (state.swipeOpen === row) state.swipeOpen = null;
}
function settleSwipe(row, outcome) {
  setClass(row, "is-swiping", false);
  if (outcome === "open") {
    if (state.swipeOpen && state.swipeOpen !== row) closeSwipe();
    setSwipeOffset(row, -GESTURE.swipeRevealPx);
    setClass(row, "is-open", true);
    state.swipeOpen = row;
    haptic("select");
    return;
  }
  closeSwipe(row);
  if (outcome === "action") runSwipeAction(row);
}
function runSwipeAction(row) {
  const action = row.pendingSwipe || swipeActionFor(sessionForRow(row));
  row.pendingSwipe = null;
  closeSwipe(row);
  if (!action?.s || !state.authenticated) return;
  haptic("select");
  if (action.kind === "close") closeTerminal(action.terminal.id);
  else openSessionTerminal(action.s);
}
function cardActions(s) {
  const terminal = terminalForSession(s);
  const actions = [
    { label: "대화 열기", run: () => select(s.key) },
    { label: terminal ? "터미널로 돌아가기" : "터미널 열기", run: () => openSessionTerminal(s), disabled: state.launchBusy },
  ];
  if (terminal) {
    const copy = terminalCloseCopy(terminal);
    actions.push({ label: copy.action, danger: copy.danger, run: () => closeTerminal(terminal.id) });
  }
  return actions;
}
function openCardMenu(row, at = null) {
  const s = sessionForRow(row);
  if (!s || !state.authenticated) return;
  closeSwipe();
  openActionSheet({ eyebrow: cardProject(s), title: cardTitle(s), actions: cardActions(s), at, trigger: row.card });
}
function bindCardGestures(root) {
  let drag = null;
  const rowOf = (target) => {
    const row = target?.closest?.(".card-row");
    return row?.card ? row : null;
  };
  bindPressMenu(root, rowOf, openCardMenu, {
    onDown(event) {
      const row = rowOf(event.target);
      if (state.swipeOpen && state.swipeOpen !== row) closeSwipe();
      drag = null;
      if (!row || event.pointerType === "mouse" || event.target?.closest?.(".card-swipe-action")) return;
      drag = { row, id: event.pointerId, x: event.clientX, y: event.clientY, intent: null, offset: 0,
        base: state.swipeOpen === row ? -GESTURE.swipeRevealPx : 0, samples: [[Date.now(), event.clientX]] };
    },
    // Returns true once the drag is a swipe, which ends the long press.
    onMove(event) {
      if (!drag || event.pointerId !== drag.id) return false;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (!drag.intent) {
        drag.intent = swipeIntent(dx, dy, drag.base !== 0);
        if (drag.intent === "scroll") {
          drag = null;
          return false;
        }
        if (drag.intent !== "swipe") return false;
        prepareSwipe(drag.row);
        root.swiping = true;
      }
      // Never right of rest; past the card's width the pull resists.
      const raw = Math.min(0, drag.base + dx);
      const width = drag.row.offsetWidth || 360;
      drag.offset = raw < -width * 0.75 ? -width * 0.75 - Math.sqrt(-raw - width * 0.75) * 4 : raw;
      drag.samples.push([Date.now(), event.clientX]);
      if (drag.samples.length > 6) drag.samples.shift();
      setSwipeOffset(drag.row, drag.offset);
      if (event.cancelable) event.preventDefault();
      return true;
    },
    onUp(event, cancelled) {
      const current = drag;
      if (!current || event.pointerId !== current.id) return;
      drag = null;
      root.swiping = false;
      if (current.intent !== "swipe") return;
      root.pressedAt = Date.now(); // the click after a swipe never opens the card
      root.swiping = false;
      const now = Date.now();
      const recent = current.samples.filter(([time]) => now - time < 120);
      const velocity = recent.length >= 2
        ? (recent[recent.length - 1][1] - recent[0][1]) / Math.max(1, recent[recent.length - 1][0] - recent[0][0]) : 0;
      settleSwipe(current.row, cancelled ? "close"
        : swipeRelease({ offset: current.offset, velocity, width: current.row.offsetWidth || 360 }));
    },
  });
  // Once a drag is a swipe, the list does not also scroll under the finger.
  root.addEventListener("touchmove", (event) => {
    if (root.swiping && event.cancelable) event.preventDefault();
  }, { passive: false });
}

// --- Action sheet (menus) -------------------------------------------------------
// One sheet serves every menu: a bottom sheet on phones, a small popover at
// the pointer for a right click on wider screens.
function openActionSheet({ eyebrow = "", title = "", preview = "", actions = [], at = null, trigger = null } = {}) {
  const dialog = $("action-sheet");
  if (!dialog || !actions.length) return;
  if (dialog.open) dialog.close();
  $("action-eyebrow").textContent = eyebrow;
  $("action-title").textContent = title;
  const previewNode = $("action-preview");
  previewNode.textContent = preview;
  previewNode.hidden = !preview;
  const items = actions.map((action) => {
    const item = button(action.label, `action-item${action.danger ? " danger" : ""}`, (event) => {
      if (item.disabled || !menuItemPressed(dialog, item, event)) return;
      closeActionSheet({ restoreFocus: false });
      action.run();
    });
    item.disabled = !!action.disabled;
    return item;
  });
  $("action-list").replaceChildren(...items);
  state.actionTrigger = trigger;
  const popover = !!at && !phoneLayout();
  setClass(dialog, "is-popover", popover);
  if (dialog.style) {
    const width = window.innerWidth || 0;
    const height = window.innerHeight || 0;
    dialog.style.left = popover ? `${Math.round(Math.max(8, Math.min(at.x, width - 268)))}px` : "";
    dialog.style.top = popover ? `${Math.round(Math.max(8, Math.min(at.y, height - 96 - 46 * items.length)))}px` : "";
  }
  sheet("action-sheet").open();
}
// A pointer click runs a menu item only when its press started on that item
// after the menu opened. The finger that opened the menu by a long press
// lifts over it (a centered menu on wide screens sits right under it) and
// must run nothing. A keyboard's click (Enter, Space) has no pointer: detail 0.
function menuItemPressed(dialog, item, event) {
  if (!event?.detail) return true;
  const target = dialog?.bottomSheet?.pressedSinceOpen ? dialog.bottomSheet.pressTarget : null;
  return !!target && (target === item || !!item.contains?.(target));
}
function closeActionSheet({ restoreFocus = true } = {}) {
  const dialog = $("action-sheet");
  if (dialog?.open) dialog.close();
  const trigger = state.actionTrigger;
  state.actionTrigger = null;
  if (restoreFocus && trigger && trigger.isConnected !== false) trigger.focus?.({ preventScroll: true });
}

function focusDetailBack() {
  $("detail").querySelector(".back")?.focus({ preventScroll: true });
}
function restoreSelectedCardFocus() {
  const card = [...$("groups").querySelectorAll(".card")].find((node) => node.dataset.sessionKey === state.selected);
  (card || $("home")).focus({ preventScroll: true });
}
function select(key) {
  if (state.launchBusy) return;
  const mobile = phoneLayout();
  const entering = mobile && !state.detailShown;
  if (entering) navigateView("detail");
  if (state.selected === key) {
    setDetailShown(true, { direction: entering ? "push" : null, focus: mobile });
    return;
  }
  haptic("select");
  state.selected = key;
  stopChatPolling();
  state.detailFor = null;
  state.route = null;
  // The conversation read that render() starts checks this, so it is set
  // first; setDetailShown then switches the screen (and syncs live updates).
  const wasShown = state.detailShown;
  state.detailShown = true;
  render();
  state.detailShown = wasShown;
  setDetailShown(true, { direction: entering ? "push" : null, focus: mobile });
}
function detailRevision(s) {
  return JSON.stringify([s.stale, s.phase, s.title, s.project, s.account, s.hostLabel, s.fetchedAt,
    s.cwd || null, s.sessionId || null]);
}
function refreshDetail(s) {
  const revision = detailRevision(s);
  if (
    state.detailFor === s.key &&
    state.detailUpdatedAt === s.updatedAt &&
    state.detailRevision === revision
  )
    return;
  const fresh = state.detailFor !== s.key;
  state.detailFor = s.key;
  state.detailUpdatedAt = s.updatedAt;
  state.detailRevision = revision;
  if (fresh) drawDetail(s);
  else patchDetailMetadata(s);
  loadDetail(s, fresh);
}
function detailMetaChildren(s) {
  const [label, tone] = BADGES[s.phase] || [s.phase || "상태 확인 필요", "gray"];
  return [el("span", `badge ${tone}`, label),
    el("span", null, [AGENTS[s.agent], s.account, s.hostLabel].filter(Boolean).join(" · ")),
    el("span", null, clock(s.updatedAt))];
}
function patchDetailMetadata(s) {
  const pane = $("detail");
  if (pane.dataset.key !== s.key) return;
  const title = pane.querySelector(".detail-title");
  const eyebrow = pane.querySelector(".detail-titles .eyebrow");
  if (title) title.textContent = s.title || "제목 없는 작업";
  if (eyebrow) eyebrow.textContent = s.project || "RECENT CONVERSATION";
  pane.querySelector(".detail-meta")?.replaceChildren(...detailMetaChildren(s));
  let note = pane.querySelector(".stale-notice");
  if (s.stale) {
    if (!note) {
      note = el("div", "stale-notice");
      pane.querySelector(".detail-head")?.after(note);
    }
    note.textContent = `${s.hostLabel}에 연결되지 않았습니다. ${s.fetchedAt ? `${ago(s.fetchedAt)} 확인한` : "저장된"} 정보입니다.`;
  } else note?.remove();
  const route = pane.querySelector(".chat-route");
  if (route) route.textContent = `${s.hostLabel || s.host} · ${AGENTS[s.agent] || s.agent} · ${sessionAccountLabel(s)}`;
  renderDetailTerminal(s);
}
async function loadDetail(s, fresh = false) {
  const run = ++state.detailRun;
  const authEpoch = state.authEpoch;
  const identity = conversationIdentity(s);
  const entry = chatEntry(s);
  const stateEpoch = entry.stateEpoch;
  const viewCurrent = () => run === state.detailRun && authEpoch === state.authEpoch &&
    state.authenticated && state.selected === s.key && conversationIdentity(selectedSession()) === identity &&
    stateEpoch === entry.stateEpoch;
  if (claudeWithoutId(s) && !s.sessionId) {
    // Nothing to read yet: one notice, no request that can only fail.
    renderMessages(s, [], "", fresh);
    conversationSyncNotice(s, "");
    updateChatComposer(entry);
    return;
  }
  const cached = entry.conversation;
  const usableCache = cached && cached.authEpoch === authEpoch && cached.identity === identity;
  if (usableCache) {
    renderMessages(s, cached.messages, "", fresh);
    conversationSyncNotice(s, "checking");
  }
  if (networkOffline() || !chatVisible()) {
    conversationSyncNotice(s, "stale");
    if (!usableCache && networkOffline()) renderMessages(s, null, "오프라인입니다. 연결 후 대화를 다시 확인해 주세요.");
    return;
  }
  const live = await loadChatState(s, true, { join: true });
  if (!viewCurrent()) return;
  if (live?.status === "success" && chatReadSupported(entry, live.data)) {
    conversationSyncNotice(s, live.data.stale ? "stale" : "");
    return;
  }
  const params = new URLSearchParams({
    ...sourceOf(s),
    id: s.agent === "claude" ? s.sessionId || s.id : s.id,
  });
  const fallbackSnapshot = entry.conversation;
  try {
    const data = await api(`/api/read?${params}`, { authEpoch });
    if (!viewCurrent()) return;
    if (entry.conversation !== fallbackSnapshot && chatReadSupported(entry)) return;
    const messages = data.messages || [];
    rememberConversation(s, messages, data);
    renderMessages(s, messages, data.error || "", fresh && !usableCache);
    updateReadNotice(data);
    conversationSyncNotice(s, data.error ? "failed" : data.stale ? "stale" : "");
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth && viewCurrent()) {
      if (entry.conversation !== fallbackSnapshot && chatReadSupported(entry)) return;
      renderMessages(s, null, err.message);
      conversationSyncNotice(s, "failed");
    }
  }
}

function drawDetail(s) {
  const pane = $("detail");
  pane.replaceChildren();
  pane.dataset.key = s.key;
  const head = el("div", "detail-head");
  head.append(button("←", "back", backToList));
  const titles = el("div", "detail-titles");
  titles.append(
    el("p", "eyebrow", s.project || "RECENT CONVERSATION"),
    el("h1", "detail-title", s.title || "제목 없는 작업"),
  );
  const meta = el("div", "detail-meta");
  meta.append(...detailMetaChildren(s));
  // Shown while this session has a live board terminal (renderDetailTerminal);
  // without one, the composer's own "터미널 열기" stays the way to open it.
  const terminal = el("div", "detail-terminal");
  terminal.id = "detail-terminal";
  terminal.hidden = true;
  const goTerminal = button("터미널로", "detail-terminal-open", () => goToSessionTerminal(s.key));
  goTerminal.id = "detail-terminal-open";
  const mark = terminalMark("detail-terminal-mark");
  mark.hidden = false;
  terminal.append(mark, goTerminal);
  titles.append(meta, terminal);
  head.append(titles);
  pane.append(head);
  renderDetailTerminal(s);
  if (s.stale)
    pane.append(
      el(
        "div",
        "stale-notice",
        `${s.hostLabel}에 연결되지 않았습니다. ${s.fetchedAt ? `${ago(s.fetchedAt)} 확인한` : "저장된"} 정보입니다.`,
      ),
    );
  const bar = el("div", "conversation-bar");
  bar.append(
    el("strong", null, "최근 대화"),
    button("맨 아래 ↓", "", () => scrollMessagesToEnd()),
  );
  const list = el("div", "messages");
  list.id = "messages";
  list.setAttribute("aria-label", "최근 대화 읽기");
  list.append(el("p", "group-empty", "대화를 불러오고 있습니다…"));
  const sync = el("p", "conversation-sync-notice");
  sync.id = "conversation-sync-notice";
  sync.hidden = true;
  sync.setAttribute("role", "status");
  const jump = button("↓", "jump-bottom", () => scrollMessagesToEnd());
  jump.id = "jump-bottom";
  jump.hidden = true;
  jump.setAttribute("aria-label", "대화 맨 아래로");
  list.addEventListener("scroll", () => readingScroll(list), { passive: true });
  bindMessageGestures(list);
  // The folded one-line header unfolds when tapped (the back button keeps its job).
  head.addEventListener("click", (event) => {
    if (!state.chromeVisible && !event.target?.closest?.(".back, .detail-terminal-open"))
      setChromeVisible(true, { stickBottom: messagesAtEnd(list) });
  });
  state.chromeVisible = true;
  pane.classList?.remove("is-reading");
  pane.classList?.remove("is-typing");
  // Shown once, the first time scrolling hides the bars on a phone.
  const hint = el("p", "tap-hint", "화면을 탭하면 메뉴가 나타나요");
  hint.id = "tap-hint";
  hint.hidden = true;
  hint.setAttribute("role", "status");
  const prompt = el("section", "chat-prompt");
  prompt.id = "chat-prompt";
  prompt.hidden = true;
  prompt.setAttribute("aria-live", "polite");
  pane.append(bar, sync, list, jump, hint, prompt, buildChatComposer(s), buildContinuePanel(s));
}
// Questions and approvals drawn by the CLI in the board's terminal, answered
// here with the same keys a person would press. Nothing is sent until the
// second, explicit tap, and the server rechecks the exact menu first.
const AGENT_NAMES = { claude: "Claude", codex: "Codex" };
const PROMPT_INLINE_OPTIONS = 4;
function renderPrompt(s, data) {
  const card = $("chat-prompt");
  if (!card || !currentChat(chatEntry(s))) return;
  const entry = chatEntry(s);
  const prompt = data?.prompt && Array.isArray(data.prompt.options) && data.prompt.options.length ? data.prompt : null;
  const openable = !prompt && data?.promptOpenable === true && data?.phase === "needs_input";
  if (!prompt && !openable) {
    card.hidden = true;
    card.chooseNumber = null;
    card.dataset.fingerprint = "";
    card.dataset.question = "";
    entry.promptChoice = null;
    // A note belongs to the question it was written for.
    entry.promptNote = "";
    return;
  }
  const question = prompt ? prompt.fingerprint : "open";
  if (card.dataset.question && card.dataset.question !== question) entry.promptNote = "";
  const revision = prompt ? `${prompt.fingerprint}|${prompt.current}|${entry.promptChoice?.number || ""}|${entry.promptBusy ? 1 : 0}|${entry.promptNote || ""}`
    : `open|${entry.promptBusy ? 1 : 0}|${entry.promptNote || ""}`;
  // A question that just appeared (or a different one) must not be missed:
  // the bars come back. Re-renders of the same question leave them alone.
  const appeared = card.hidden || card.dataset.question !== question;
  card.hidden = false;
  card.dataset.question = question;
  if (appeared) revealChrome();
  if (card.dataset.fingerprint === revision) return;
  card.dataset.fingerprint = revision;
  if (entry.promptChoice && entry.promptChoice.fingerprint !== prompt?.fingerprint) entry.promptChoice = null;
  const head = el("div", "chat-prompt-head");
  head.append(el("strong", null, prompt ? `${AGENT_NAMES[prompt.agent] || "에이전트"}가 답을 기다려요` : "답이 필요한 상태입니다"));
  const terminal = button("터미널", "chat-prompt-terminal", () => openChatTerminal(s));
  head.append(terminal);
  const nodes = [head];
  if (!prompt) {
    card.chooseNumber = null;
    nodes.push(el("p", "chat-prompt-note", "질문 화면을 불러오면 여기서 바로 고를 수 있습니다."));
    const open = button(entry.promptBusy ? "불러오는 중…" : "질문 불러오기", "primary", () => promptAction(s, { action: "open" }));
    open.disabled = !!entry.promptBusy || networkOffline();
    nodes.push(open);
    // Why the last "질문 불러오기" did not work.
    if (entry.promptNote) nodes.push(el("p", "chat-prompt-note", entry.promptNote));
    card.replaceChildren(...nodes);
    return;
  }
  if (prompt.question) {
    const question = el("pre", "chat-prompt-question", prompt.question);
    nodes.push(question);
  }
  const selectable = !prompt.multi && Number.isInteger(prompt.current);
  // A hardware keyboard's 1–9 (promptKeydown): the same choice as a tap,
  // among every option (the sheet's too); false when there is no such one.
  card.chooseNumber = selectable ? (number) => {
    const option = prompt.options.find((item) => item.number === number);
    if (!option || entry.promptBusy || !currentChat(entry) || card.dataset.question !== prompt.fingerprint) return false;
    if (entry.promptChoice?.number !== option.number) {
      entry.promptChoice = { fingerprint: prompt.fingerprint, number: option.number };
      haptic("select");
      renderPrompt(s, entry.data);
    }
    return true;
  } : null;
  const options = el("div", "chat-prompt-options");
  options.setAttribute("role", "radiogroup");
  // A long menu keeps the card short: the first few choices (and the chosen
  // one) inline, every choice in a sheet.
  const many = selectable && prompt.options.length > PROMPT_INLINE_OPTIONS + 1;
  const inline = many ? prompt.options.filter((option, index) => index < PROMPT_INLINE_OPTIONS ||
    option.number === entry.promptChoice?.number) : prompt.options;
  for (const option of inline) {
    const chosen = entry.promptChoice?.number === option.number;
    const item = button("", `chat-prompt-option${chosen ? " is-chosen" : ""}${option.number === prompt.current ? " is-current" : ""}`, () => {
      if (!selectable || entry.promptBusy) return;
      entry.promptChoice = { fingerprint: prompt.fingerprint, number: option.number };
      haptic("select");
      renderPrompt(s, entry.data);
    });
    item.setAttribute("role", "radio");
    item.setAttribute("aria-checked", String(chosen));
    item.disabled = !selectable || !!entry.promptBusy;
    item.append(el("span", "chat-prompt-number", String(option.number)), el("span", "chat-prompt-label", option.label));
    if (option.detail) item.append(el("span", "chat-prompt-detail", option.detail));
    options.append(item);
  }
  nodes.push(options);
  if (many) {
    const all = button(`선택지 ${prompt.options.length}개 모두 보기`, "chat-prompt-more", () => {
      if (entry.promptBusy) return;
      openActionSheet({ eyebrow: "선택지", title: prompt.question ? prompt.question.split("\n")[0] : "선택지를 고르세요",
        trigger: all, actions: prompt.options.map((option) => ({
          label: `${option.number}. ${option.label}${entry.promptChoice?.number === option.number ? " · 선택됨" : ""}`,
          run: () => {
            entry.promptChoice = { fingerprint: prompt.fingerprint, number: option.number };
            haptic("select");
            renderPrompt(s, entry.data);
          },
        })) });
    });
    all.setAttribute("aria-haspopup", "dialog");
    all.disabled = !!entry.promptBusy;
    nodes.push(all);
  }
  if (!selectable) {
    nodes.push(el("p", "chat-prompt-note", prompt.multi ? "여러 개를 고르는 메뉴는 터미널에서 선택해 주세요."
      : "현재 선택 위치를 확인하지 못했습니다. 터미널에서 선택해 주세요."));
  } else {
    const choice = prompt.options.find((option) => option.number === entry.promptChoice?.number);
    const send = button(entry.promptBusy ? "보내는 중…" : choice ? `${choice.number}번 보내기` : "선택지를 고르세요", "primary", () => {
      if (!choice) return;
      promptAction(s, { action: "answer", fingerprint: prompt.fingerprint, number: choice.number });
    });
    send.disabled = !choice || !!entry.promptBusy || networkOffline();
    nodes.push(send);
  }
  if (entry.promptNote) nodes.push(el("p", "chat-prompt-note", entry.promptNote));
  card.replaceChildren(...nodes);
}
async function promptAction(s, body) {
  const entry = chatEntry(s);
  if (entry.promptBusy || !currentChat(entry)) return;
  entry.promptBusy = true;
  entry.promptNote = "";
  // A state read begun before the answer still shows the question: it is not
  // drawn again (the read that follows the answer is).
  entry.stateEpoch++;
  renderPrompt(s, entry.data);
  try {
    const result = await api("/api/chat/prompt", { method: "POST", body: {
      source: entry.source, requestId: requestId(), ...body } });
    if (!currentChat(entry)) return;
    entry.promptChoice = null;
    if (body.action === "answer")
      entry.promptNote = result.status === "answered" ? "" : "선택이 반영됐는지 확인하지 못했습니다. 터미널에서 확인해 주세요.";
    if (body.action === "answer" && result.status === "answered") { toast("선택을 보냈습니다."); haptic("success"); }
    entry.data = { ...entry.data, prompt: result.prompt || null, promptOpenable: false };
    // "질문 불러오기" may have attached a board terminal: its marks show it now.
    if (body.action === "open") refreshTerminals();
  } catch (err) {
    if (!err.unauthorized && currentChat(entry)) { entry.promptNote = err.message; haptic("error"); }
  } finally {
    entry.promptBusy = false;
    entry.stateEpoch++;
    if (currentChat(entry)) {
      renderPrompt(s, entry.data);
      state.chatPollCount = 0;
      loadChatState(s, true);
    }
  }
}
// --- Conversation bars (header and composer tools, "chrome") ------------------
// Reading and typing are different intents, so they are separate states:
// state.chromeVisible is the reader's choice, "is-typing" the input's own
// folded layout. Phones (coarse pointer): scrolling either way hides the bars,
// reaching the end does not bring them back, and a tap on the conversation
// shows or hides them; a new question, a send result or a connection problem
// shows them. Mouse layouts keep the older rule: an upward read hides them and
// the newest message restores them.
const CHROME_TAP_IGNORE = "a, button, input, textarea, select, label, summary, [role=button], " +
  ".msg-more, .message-block-copy, .message-code-toggle, .chat-prompt, .msg-attachment, .chat-attachment, img, video";
const TAP_HINT_MS = 2500;
function chatTyping() {
  return document.activeElement?.id === "chat-input";
}
function messagesAtEnd(list, slack = 50) {
  return !!list && list.scrollHeight - list.scrollTop - list.clientHeight < slack;
}
// Applies the reader's choice; while a phone is typing it waits for the blur.
function setChromeVisible(visible, { stickBottom = false } = {}) {
  state.chromeVisible = !!visible;
  if (chatTyping() && touchDevice()) return;
  setReading(!visible, { stickBottom });
}
function toggleChrome() {
  const hint = $("tap-hint");
  if (hint && !hint.hidden) hint.hidden = true;
  setChromeVisible(!state.chromeVisible, { stickBottom: messagesAtEnd($("messages")) });
}
// Something the reader must not miss shows the bars on phones; mouse layouts
// are left as they were.
function revealChrome() {
  if (state.chromeVisible || !touchDevice()) return;
  setChromeVisible(true, { stickBottom: messagesAtEnd($("messages")) });
}
// Whether a finger's down → up toggles the bars: short and still, not on a
// control (which keeps its own tap), no text selected, no long-press menu.
function isChromeTap(down, up, { now = Date.now(), fired = false } = {}) {
  if (!down || !up || fired || up.pointerId !== down.id) return false;
  const dx = (Number.isFinite(up.clientX) ? up.clientX : down.x) - down.x;
  const dy = (Number.isFinite(up.clientY) ? up.clientY : down.y) - down.y;
  if (Math.hypot(dx, dy) > GESTURE.tapSlopPx || now - down.at > GESTURE.tapMaxMs) return false;
  if (down.target?.closest?.(CHROME_TAP_IGNORE)) return false;
  const selection = window.getSelection?.();
  return !(selection && selection.isCollapsed === false);
}
function showTapHint() {
  if (state.tapHintShown || prefs.get("tapHint", false) === true) return;
  state.tapHintShown = true;
  prefs.set("tapHint", true);
  const hint = $("tap-hint");
  if (!hint) return;
  const list = $("messages");
  if (hint.style && Number.isFinite(list?.offsetTop)) hint.style.top = `${list.offsetTop + 12}px`;
  hint.hidden = false;
  clearTimeout(state.tapHintTimer);
  state.tapHintTimer = setTimeout(() => { hint.hidden = true; }, TAP_HINT_MS);
}
// The board's own scroll of the conversation (new messages, layout changes):
// the next scroll event sees no travel, so it never hides the bars.
function setMessagesScroll(list, top) {
  list.scrollTop = top;
  list.lastScrollTop = list.scrollTop;
}
// The class mirror of state.chromeVisible (and of the input's focus):
// "is-reading" folds the header and the composer tools.
function setReading(reading, { stickBottom = false } = {}) {
  const pane = $("detail");
  if (!pane?.classList || pane.classList.contains?.("is-reading") === reading) return;
  const list = $("messages");
  const before = list?.getBoundingClientRect?.().top;
  pane.classList.toggle?.("is-reading", reading);
  if (list && typeof before === "number") {
    if (stickBottom) list.scrollTop = list.scrollHeight;
    else {
      // The header folding moves the list; shift its scroll by the same amount
      // so the line being read stays exactly where it was.
      const shift = list.getBoundingClientRect().top - before;
      if (shift) list.scrollTop += shift;
    }
    list.lastScrollTop = list.scrollTop;
  }
  renderJump();
}
// Typing on a phone folds the same chrome as reading mode (header to one
// line, conversation bar, continue row and tool row), so the keyboard leaves
// the transcript most of the screen. Leaving the input restores it.
function setTyping(typing) {
  const pane = $("detail");
  if (!pane?.classList || !!pane.classList.contains?.("is-typing") === typing) return;
  const list = $("messages");
  const atBottom = messagesAtEnd(list);
  if (typing) pane.classList.add("is-typing");
  else pane.classList.remove("is-typing");
  if (list && atBottom) setMessagesScroll(list, list.scrollHeight);
  renderJump();
}
// The viewport changed (an on-screen keyboard opened or closed, a rotation, a
// split view): typing folds the bars only while the screen is compact, and a
// wide touch screen never keeps them hidden or shows the tap hint.
function syncChromeMode() {
  if (!touchDevice()) return;
  const compact = compactChrome();
  if (chatTyping()) setTyping(compact);
  if (compact) return;
  const hint = $("tap-hint");
  if (hint && !hint.hidden) hint.hidden = true;
  if (!state.chromeVisible) setChromeVisible(true, { stickBottom: messagesAtEnd($("messages")) });
}
function readingScroll(list) {
  const top = list.scrollTop;
  const previous = list.lastScrollTop ?? top;
  list.lastScrollTop = top;
  // While typing, the chrome is already folded (setTyping) and stays put. A
  // wide touch screen keeps its bars: its scroll is tracked but hides nothing.
  const typing = chatTyping();
  if (touchDevice()) phoneReadingScroll(list, top - previous, typing || !compactChrome());
  else if (list.scrollHeight - top - list.clientHeight < GESTURE.readingBottomPx) {
    list.upTravel = 0;
    list.unread = 0;
    if (!typing) setChromeVisible(true, { stickBottom: true });
  } else if (!typing) {
    // A deliberate upward read folds the chrome; a small wobble does not.
    list.upTravel = top < previous ? (list.upTravel || 0) + previous - top : 0;
    if (list.upTravel > GESTURE.readingHideTravelPx) setChromeVisible(false);
  }
  renderJump();
}
// Phones: a deliberate scroll either way hides the bars; only a tap (or
// something that needs attention) shows them again, even at the end. `keep`
// (typing, or a wide touch screen) records the scroll without hiding.
function phoneReadingScroll(list, delta, keep) {
  if (messagesAtEnd(list, GESTURE.readingBottomPx)) list.unread = 0;
  const height = list.clientHeight;
  const resized = Number.isFinite(list.lastClientHeight) && list.lastClientHeight !== height;
  list.lastClientHeight = height;
  if (!delta) return;
  list.lastScrollAt = Date.now();
  // A keyboard, the bars themselves, a growing composer or the board's smooth
  // "맨 아래" also move the list: that is not reading.
  if (keep || resized || Date.now() < (list.autoScrollUntil || 0)) {
    list.travel = 0;
    return;
  }
  const direction = Math.sign(delta);
  list.travel = direction === list.travelDirection ? (list.travel || 0) + Math.abs(delta) : Math.abs(delta);
  list.travelDirection = direction;
  if (list.travel <= GESTURE.readingHideTravelPx || !state.chromeVisible) return;
  list.travel = 0;
  setChromeVisible(false);
  showTapHint();
}
function scrollMessagesToEnd() {
  const list = $("messages");
  if (!list) return;
  list.unread = 0;
  if (typeof list.scrollTo === "function") {
    // The smooth scroll's own events do not hide the bars; a touch ends this.
    list.autoScrollUntil = Date.now() + 1000;
    list.scrollTo({ top: list.scrollHeight, behavior: "smooth" });
  } else setMessagesScroll(list, list.scrollHeight);
  renderJump();
}
function renderJump() {
  const list = $("messages");
  const jump = $("jump-bottom");
  if (!list || !jump) return;
  const far = list.scrollHeight - list.scrollTop - list.clientHeight > GESTURE.jumpShowPx;
  const unread = list.unread || 0;
  jump.hidden = !far && !unread;
  const label = unread ? `↓ 새 메시지 ${unread}` : "↓";
  if (jump.textContent !== label) jump.textContent = label;
  jump.classList?.toggle?.("has-unread", unread > 0);
  jump.setAttribute("aria-label", unread ? `새 메시지 ${unread}개, 맨 아래로` : "대화 맨 아래로");
  // Float just above the conversation's bottom edge, whatever the composer height.
  const pane = $("detail");
  if (jump.style && pane?.clientHeight && Number.isFinite(list.offsetTop)) {
    const bottom = pane.clientHeight - list.offsetTop - list.clientHeight + 12;
    if (bottom > 0) jump.style.bottom = `${bottom}px`;
  }
}

// Drafts and attachment handles stay in memory and are keyed by the actual
// runtime identity, independently of the continuation/transfer route selectors.
function chatKey(source) {
  return JSON.stringify([source.host, source.agent, source.home || "", source.id]);
}
function conversationIdentity(s) {
  if (!s) return null;
  return JSON.stringify([chatKey(sourceOf(s)), s.cwd || null, s.agent === "claude" ? s.sessionId || s.id : s.id]);
}
function chatReadSupported(entry, data = entry.data) {
  return !!data && !data.error && chatKey(data.route || {}) === chatKey(entry.source) && Array.isArray(data.messages);
}
function rememberConversation(s, messages, data = {}) {
  if (!Array.isArray(messages) || data.error) return;
  chatEntry(s).conversation = { identity: conversationIdentity(s), authEpoch: state.authEpoch,
    messages, fetchedAt: data.fetchedAt || Date.now() / 1000, stale: !!data.stale };
}
function conversationSyncNotice(s, status) {
  if (state.selected !== s.key || $("detail").dataset.key !== s.key) return;
  const cached = chatEntry(s).conversation;
  const sameCache = cached && cached.authEpoch === state.authEpoch && cached.identity === conversationIdentity(s);
  const text = status === "checking" ? "저장된 대화 · 최신 내용 확인 중…"
    : status === "failed" && sameCache ? "저장된 대화 · 최신 내용을 확인하지 못했습니다."
    : status === "stale" && sameCache ? "저장된 대화 · 연결 후 최신 내용을 확인해 주세요." : "";
  const notice = $("conversation-sync-notice");
  notice.textContent = text;
  notice.hidden = !text;
}
function attachmentMetadata(files) {
  if (!Array.isArray(files)) return [];
  return files.filter((file) => file && typeof file === "object").map((file) => {
    const type = typeof file.type === "string" ? file.type : "";
    const kind = file.kind === "image" || file.kind !== "file" && type.startsWith("image/") ? "image" : "file";
    return { id: typeof file.id === "string" ? file.id : null,
      name: typeof file.name === "string" && file.name ? file.name : kind === "image" ? "이미지 첨부" : "첨부파일",
      type, size: Number.isInteger(file.size) && file.size >= 0 ? file.size : null, kind,
      source: file.source === "native" ? "native" : "sessionholic" };
  });
}
// Names a transcript gives files it cannot name; such a chip shows its kind once.
const GENERIC_ATTACHMENT_NAMES = new Set(["이미지 첨부", "이미지", "사진", "첨부파일", "파일", "image", "file", "attachment"]);
function attachmentGroup(files, caption = `첨부 ${files.length}개`, { showCaption = true } = {}) {
  const group = el("div", "msg-attachments");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", caption);
  if (showCaption) group.append(el("span", "attachment-caption", caption));
  const chips = el("div", "attachment-chips");
  chips.setAttribute("role", "list");
  for (const file of files) {
    const image = file.kind === "image";
    const kind = image ? "이미지" : "파일";
    const generic = GENERIC_ATTACHMENT_NAMES.has(String(file.name || "").trim().toLowerCase());
    const name = generic ? kind : file.name;
    const size = file.size == null ? "" : formatBytes(file.size);
    const chip = el("span", `msg-attachment ${image ? "is-image" : "is-file"}`);
    chip.setAttribute("role", "listitem");
    chip.setAttribute("aria-label", `${kind}${generic ? "" : `: ${file.name}`}${size ? `, ${size}` : ""}`);
    chip.title = generic ? kind : file.name;
    const icon = el("span", "attachment-icon");
    icon.setAttribute("aria-hidden", "true");
    chip.append(icon, el("span", "attachment-name", name));
    if (size) chip.append(el("span", "attachment-size", size));
    chips.append(chip);
  }
  group.append(chips);
  return group;
}
function updateChatDeliveryEvidence(entry) {
  const box = $("chat-delivery-evidence");
  const receipt = entry.receipt;
  const pending = entry.pending;
  const matchingReceipt = !pending || receipt?.requestId === pending.requestId;
  const files = attachmentMetadata(matchingReceipt && Array.isArray(receipt?.attachments)
    ? receipt.attachments : pending?.attachmentDetails);
  // Once the message is in the conversation, its own chips show the files.
  const retained = pending || !receiptSettled(entry) &&
    (["accepted", "queued", "completed", "interrupted", "unknown"].includes(receipt?.status) ||
    receipt?.status === "failed" && receipt.confirmed === true);
  const visible = retained && files.length > 0;
  box.hidden = !visible;
  if (!visible) { box.replaceChildren(); box.deliveryRevision = null; return false; }
  const labels = { accepted: "전송 접수", queued: "전송 접수", completed: "요청 완료",
    interrupted: "접수 후 중단", unknown: "전송 결과 미확인", failed: "접수 후 오류" };
  const label = pending ? entry.sending ? "전송 중" : "전송 결과 미확인" : labels[receipt?.status];
  const record = receipt?.readbackConfirmed === true && !pending ? " · 세션 기록 확인" : "";
  const caption = `${label}${record} · 전송에 포함한 첨부 ${files.length}개`;
  const revision = JSON.stringify([pending?.requestId || receipt?.requestId, caption, files]);
  if (box.deliveryRevision === revision) return true;
  box.deliveryRevision = revision;
  box.replaceChildren(attachmentGroup(files, caption));
  return true;
}
function submissionRequestIds(entry, lastRequestId) {
  return [lastRequestId].filter((id) => typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/.test(id));
}
function chatEntry(s) {
  const source = sourceOf(s);
  const key = chatKey(source);
  if (!state.chats.has(key)) state.chats.set(key, {
    source: Object.freeze({ ...source }), text: "", attachments: [], data: null, conversation: null,
    pending: null, receipt: null, sending: false, uploads: 0, outbox: [], outboxClaims: [], uploadQueue: [], thumbs: new Map(),
    composing: false, editorEpoch: 0, stateEpoch: 0, recheckQueued: false, expanded: false, inputCollapsed: false, checking: false, error: "", notice: "", revision: 0,
  });
  return state.chats.get(key);
}
function currentChat(entry) {
  const selected = selectedSession();
  return !!selected && chatEntry(selected) === entry && state.authenticated;
}
function networkOffline() {
  return state.offline || globalThis.navigator?.onLine === false;
}
function chatVisible() {
  return !document.hidden && !state.terminalVisible && (!phoneLayout() || state.detailShown);
}
function chatSupported(entry) {
  return entry.data?.capability?.supported === true &&
    chatKey(entry.data.route || {}) === chatKey(entry.source);
}
// The server's capability says sending needs a board terminal the user opens.
function terminalRequired(entry) {
  return entry.data?.capability?.action === "open_terminal" && chatKey(entry.data.route || {}) === chatKey(entry.source);
}
function chatAttachmentsSupported(entry) {
  return !!entry.data && chatKey(entry.data.route || {}) === chatKey(entry.source) &&
    entry.data.phase !== "unavailable" && entry.data.capability?.attachments !== false;
}
function buildChatComposer(s) {
  const entry = chatEntry(s);
  // Detached editors can still emit Android IME events after a session switch.
  // Their final input belongs to that editor, never to a newly mounted draft.
  entry.composing = false;
  const editorEpoch = ++entry.editorEpoch;
  const editorCurrent = () => entry.editorEpoch === editorEpoch && currentChat(entry);
  const box = el("section", `chat-composer${entry.expanded ? "" : " is-compact"}`);
  box.id = "chat-composer";
  box.setAttribute("aria-label", "실제 세션에 메시지 보내기");
  box.dataset.source = chatKey(entry.source);
  const heading = el("div", "chat-heading");
  heading.hidden = !entry.expanded;
  heading.append(el("strong", null, "이 작업에 메시지 보내기"), el("span", "chat-route", `${s.hostLabel || s.host} · ${AGENTS[s.agent] || s.agent} · ${sessionAccountLabel(s)}`));
  const status = el("div", "chat-status");
  status.id = "chat-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  // A Claude background session takes input only through a board terminal; the
  // board never opens one by itself, so the user opens it here (the same plan
  // flow as "터미널·승인") and sends after coming back.
  const gate = el("div", "chat-terminal-gate");
  gate.id = "chat-terminal-gate";
  gate.hidden = true;
  const gateText = el("span", "chat-terminal-gate-text");
  gateText.id = "chat-terminal-gate-text";
  const openTerminal = button("터미널 열기", "primary", () => { if (editorCurrent()) openChatTerminal(s); });
  openTerminal.id = "chat-open-terminal";
  gate.append(gateText, openTerminal);
  const evidence = el("div", "chat-delivery-evidence");
  evidence.id = "chat-delivery-evidence";
  evidence.hidden = true;
  const attachmentsLabel = el("p", "chat-attachments-label");
  attachmentsLabel.id = "chat-attachments-label";
  attachmentsLabel.hidden = true;
  const attachments = el("div", "chat-attachments");
  attachments.id = "chat-attachments";
  const input = el("textarea", "chat-input");
  input.id = "chat-input";
  input.rows = entry.expanded ? 3 : 1;
  input.placeholder = "이 세션에 보낼 메시지…";
  input.setAttribute("aria-label", "메시지");
  input.value = entry.text;
  input.addEventListener("input", () => {
    if (!editorCurrent()) return;
    entry.inputCollapsed = false;
    entry.text = input.value;
    entry.notice = "";
    // Editing the draft answers an input error about it.
    if (entry.inputError && entry.error === entry.inputError) entry.error = "";
    entry.inputError = "";
    entry.revision++;
    updateChatComposer(entry);
  });
  // Phones fold the bars for typing and return to the reader's choice on blur
  // (hidden stays hidden); mouse layouts show them for the input, as before.
  // A tablet folds them only once its on-screen keyboard takes the space
  // (syncChromeMode on the viewport's resize); a hardware keyboard never does.
  input.addEventListener("focus", () => {
    if (!touchDevice()) state.chromeVisible = true;
    setReading(false);
    setTyping(compactChrome());
  });
  input.addEventListener("blur", () => {
    setTyping(false);
    if (!state.chromeVisible) setReading(true, { stickBottom: messagesAtEnd($("messages")) });
  });
  input.addEventListener("compositionstart", () => {
    if (!editorCurrent()) return;
    entry.composing = true;
    updateChatComposer(entry);
  });
  input.addEventListener("compositionend", () => {
    if (!editorCurrent()) return;
    entry.composing = false;
    entry.inputCollapsed = false;
    entry.text = input.value;
    entry.revision++;
    updateChatComposer(entry);
  });
  input.addEventListener("keydown", (event) => {
    if (!editorCurrent()) return;
    // Esc leaves the input (a hardware keyboard's way out); an open tools menu
    // closes first (closeChatToolsEscape), and an IME keeps its own Esc.
    if (event.key === "Escape") {
      if (event.isComposing || event.keyCode === 229 || entry.composing || tools.open) return;
      event.preventDefault();
      input.blur();
      return;
    }
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    if (event.isComposing || event.keyCode === 229 || entry.composing) return;
    sendChat(entry);
  });
  input.addEventListener("paste", (event) => {
    if (editorCurrent()) pasteChat(entry, event, input);
  });
  const files = el("input");
  files.id = "chat-files";
  files.type = "file";
  files.multiple = true;
  files.hidden = true;
  files.addEventListener("change", () => {
    if (!editorCurrent()) return;
    uploadChatFiles(entry, [...(files.files || [])]);
    files.value = "";
  });
  const actions = el("div", "chat-toolbar");
  const tools = el("details", "chat-tools");
  tools.id = "chat-tools";
  const attach = button("첨부", "secondary", () => { if (editorCurrent()) files.click(); });
  attach.id = "chat-attach";
  attach.setAttribute("aria-label", "사진 또는 파일 첨부");
  const clipboard = button("클립보드 붙여넣기", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    readChatClipboard(entry, input, files, editorEpoch);
  });
  clipboard.id = "chat-clipboard";
  const check = button("상태 다시 확인", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    state.chatPollCount = 0;
    loadChatState(s, true);
  });
  check.id = "chat-check";
  const terminal = button("터미널·승인", "secondary", () => { if (editorCurrent()) openChatTerminal(s); });
  terminal.id = "chat-terminal";
  const native = button("이전한 작업 열기", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    openNativeChat(state.terminal);
  });
  native.id = "chat-native-open";
  native.hidden = true;
  // The same "연결 닫기" a card offers on a swipe or long press.
  const closeTerm = button("연결 닫기", "secondary", () => {
    if (!editorCurrent()) return;
    tools.open = false;
    const terminal = terminalForSession(s);
    if (terminal) closeTerminal(terminal.id);
  });
  closeTerm.id = "chat-terminal-close";
  closeTerm.hidden = true;
  const send = button("보내기", "primary", () => {
    if (!editorCurrent()) return;
    if (entry.composing) {
      toast("한글 입력을 마친 뒤 보내기를 눌러 주세요.");
      return;
    }
    entry.text = input.value;
    sendChat(entry);
  });
  send.id = "chat-send";
  send.setAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
  // Sending keeps the keyboard and the folded typing layout; a blur first
  // would move the button away under the finger before the click lands.
  send.addEventListener("pointerdown", (event) => {
    if (document.activeElement === input) event.preventDefault();
  });
  const hint = el("p", "chat-hint", "Ctrl/⌘ + Enter로 전송 · 초안과 첨부 선택은 이 탭에서만 보관됩니다.");
  hint.id = "chat-hint";
  hint.hidden = !entry.expanded;
  // The folded composer's reminder of the send key, once a keyboard is known
  // to be there (keyboardPresent); the expanded one says it in its hint.
  const keyHint = el("span", "chat-key-hint", `${sendShortcutLabel()} 보내기`);
  keyHint.id = "chat-key-hint";
  keyHint.hidden = true;
  keyHint.setAttribute("aria-hidden", "true");
  const expand = button(entry.expanded ? "접기" : "펼치기", "chat-toggle", () => {
    if (!editorCurrent()) return;
    const collapse = entry.expanded || input.dataset.grown === "true";
    entry.expanded = !collapse;
    entry.inputCollapsed = collapse;
    box.classList.toggle?.("is-compact", !entry.expanded);
    input.rows = entry.expanded ? 3 : 1;
    heading.hidden = hint.hidden = !entry.expanded;
    expand.textContent = entry.expanded ? "접기" : "펼치기";
    expand.setAttribute("aria-expanded", String(entry.expanded));
    updateChatComposer(entry);
  });
  expand.id = "chat-expand";
  expand.setAttribute("aria-expanded", String(!!entry.expanded));
  expand.setAttribute("aria-controls", "chat-input");
  // Keep the focused textarea and its in-progress IME composition intact.
  expand.addEventListener("pointerdown", (event) => event.preventDefault());
  tools.append(el("summary", null, "도구"));
  const toolsActions = el("div", "chat-tools-actions");
  toolsActions.append(clipboard, check, native, closeTerm);
  for (const action of [clipboard, check, native, closeTerm])
    action.addEventListener("pointerdown", (event) => event.preventDefault());
  tools.append(toolsActions);
  actions.append(attach, expand, terminal, keyHint, tools);
  const main = el("div", "chat-main");
  main.append(input, send);
  box.append(heading, status, gate, evidence, attachmentsLabel, attachments, main, files, actions, hint);
  queueMicrotask(() => {
    if (!editorCurrent()) return;
    updateChatComposer(entry);
  });
  return box;
}
function resizeChatInput(entry) {
  if (!currentChat(entry) || $("chat-composer")?.dataset.source !== chatKey(entry.source)) return;
  const input = $("chat-input");
  const style = window.getComputedStyle?.(input);
  const line = parseFloat(style?.lineHeight) || 22;
  const padding = (parseFloat(style?.paddingTop) || 10) + (parseFloat(style?.paddingBottom) || 10);
  const border = (parseFloat(style?.borderTopWidth) || 1) + (parseFloat(style?.borderBottomWidth) || 1);
  const base = Math.max(44, line + padding + border);
  const viewport = window.visualViewport?.height || window.innerHeight || 800;
  const cap = entry.inputCollapsed || sentDraftHidden(entry) ? base : Math.max(base,
    Math.min(line * (entry.expanded ? 11 : 6) + padding + border, viewport * 0.34, viewport - 200));
  const minimum = Math.min(cap, entry.expanded ? 3 * line + padding + border : base);
  const list = $("messages");
  const atBottom = list && list.scrollHeight - list.scrollTop - list.clientHeight < 50;
  const inputScroll = input.scrollTop || 0;
  input.style.minHeight = `${minimum}px`;
  input.style.maxHeight = `${cap}px`;
  input.style.height = "0px";
  const content = input.value ? input.scrollHeight + border || minimum : minimum;
  const height = Math.max(minimum, Math.min(cap, content));
  input.style.height = `${Math.ceil(height)}px`;
  input.style.overflowY = content > cap ? "auto" : "hidden";
  input.scrollTop = input.value ? inputScroll : 0;
  if (document.activeElement === input && input.selectionEnd === input.value.length && content > cap)
    input.scrollTop = input.scrollHeight;
  input.dataset.grown = String(height > base + 1);
  const toggle = $("chat-expand");
  toggle.textContent = height > base + 1 || entry.expanded ? "접기" : "펼치기";
  toggle.setAttribute("aria-expanded", String(height > base + 1 || entry.expanded));
  if (atBottom) setMessagesScroll(list, list.scrollHeight);
}
// While a send is in flight or unconfirmed, its text already shows as the
// bubble; the editor keeps it (a rejected send leaves it there) but hides it.
function sentDraftHidden(entry) {
  return !entry.composing && draftIsPending(entry);
}
function closeChatToolsOutside(event) {
  for (const id of ["chat-tools", "terminal-settings"]) {
    const tools = $(id);
    if (tools?.open && !tools.contains(event.target)) tools.open = false;
  }
  // An open card swipe closes on any touch outside its row.
  const open = state.swipeOpen;
  if (open && !open.contains?.(event.target)) closeSwipe(open);
}
function closeChatToolsEscape(event) {
  if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
  for (const id of ["chat-tools", "terminal-settings"]) {
    const tools = $(id);
    if (!tools?.open) continue;
    const restoreSummaryFocus = tools.contains(document.activeElement);
    tools.open = false;
    if (restoreSummaryFocus) tools.querySelector("summary")?.focus();
    event.preventDefault();
  }
}
// --- Hardware keyboard ----------------------------------------------------------
// A keyboard is assumed with a mouse or trackpad as the main pointer. On a
// touch screen it is known from a key an on-screen keyboard does not send
// (a modifier, Esc, arrows) or from typing into the composer while no
// on-screen keyboard takes the screen (an iPad's Magic Keyboard). Memory only.
const HARDWARE_KEYS = new Set(["Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  "Home", "End", "PageUp", "PageDown", "Meta", "Control", "Alt"]);
function keyboardPresent() {
  return !touchDevice() || state.hardwareKeyboard === true;
}
function sendShortcutLabel() {
  const nav = globalThis.navigator;
  const platform = nav?.userAgentData?.platform || nav?.platform || "";
  return /Mac|iPhone|iPad|iPod/i.test(platform) ? "⌘↩" : "Ctrl+Enter";
}
function noteHardwareKey(event) {
  if (state.hardwareKeyboard || !event || event.isComposing || event.keyCode === 229) return;
  const key = String(event.key || "");
  const physical = event.metaKey || event.ctrlKey || event.altKey || HARDWARE_KEYS.has(key) ||
    (key.length === 1 && chatTyping() && !phoneLayout() && !shortLayout() && !keyboardOpen());
  if (!physical) return;
  state.hardwareKeyboard = true;
  const s = selectedSession();
  if (s) updateChatComposer(chatEntry(s));
}
// 1–9 on a question card choose that option (only choose: sending stays the
// card's second, explicit step). Typing in a field, a modifier, an IME, an
// open sheet or the terminal leave the key alone.
function promptKeydown(event) {
  const key = String(event?.key || "");
  if (!/^[1-9]$/.test(key) || event.metaKey || event.ctrlKey || event.altKey || event.isComposing ||
      event.keyCode === 229 || event.defaultPrevented) return false;
  const target = event.target;
  if (target?.isContentEditable || target?.closest?.("input, textarea, select, [contenteditable]")) return false;
  if (state.terminalVisible || !chatVisible() || Object.keys(SHEETS).some((id) => $(id)?.open)) return false;
  const card = $("chat-prompt");
  if (!card || card.hidden || typeof card.chooseNumber !== "function" || !card.chooseNumber(Number(key))) return false;
  event.preventDefault();
  return true;
}
function documentKeydown(event) {
  noteHardwareKey(event);
  closeChatToolsEscape(event);
  promptKeydown(event);
}
function chatReceiptCopy(receipt) {
  if (receipt?.dispatchState === "not_started")
    return `${receipt.reason || "메시지 전송 조건을 확인해 주세요."} 메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.`;
  if (receipt?.status === "failed" && receipt.confirmed === true)
    return "요청 처리 중 오류가 발생했습니다. 대화를 확인해 주세요.";
  const labels = {
    accepted: "메시지를 접수했습니다. 작업 완료는 대화에서 확인해 주세요.",
    queued: "메시지를 접수했습니다. 답변은 대화에서 확인해 주세요.",
    completed: "요청한 작업이 완료됐습니다.",
    interrupted: "접수했던 작업이 중단되었습니다. 대화를 확인한 뒤 새 메시지를 보내 주세요.",
    unknown: "전송 결과를 아직 확인하지 못했습니다. 중복 전송하지 않고 상태를 확인합니다.",
    failed: "메시지를 접수하지 못했습니다. 초안은 유지됩니다.",
  };
  return labels[receipt?.status] || "";
}
// The last receipt's message reached the conversation (its bubble went): what
// the composer said about it being taken is no longer news.
function receiptSettled(entry) {
  return !entry.pending && !!entry.receipt?.requestId && entry.settledRequestId === entry.receipt.requestId;
}
function receiptNotice(entry) {
  const status = entry.receipt?.status;
  const phase = entry.data?.phase;
  // A refusal keeps saying the draft was not sent; anything else waits while
  // the agent asks something (the phase says so).
  if (phase === "needs_input" && entry.receipt?.dispatchState !== "not_started" && status !== "failed") return "";
  if (receiptSettled(entry) && ["accepted", "queued"].includes(status)) return "";
  // Its request ended and the agent now works on a newer one (typed in the
  // terminal, say): the phase says what is going on.
  if (["completed", "interrupted"].includes(status) && phase === "working") return "";
  return chatReceiptCopy(entry.receipt);
}
function updateChatComposer(entry) {
  if (!currentChat(entry) || $("chat-composer")?.dataset.source !== chatKey(entry.source)) return;
  const supported = chatSupported(entry);
  $("chat-composer").classList.toggle?.("is-busy", entry.data?.phase === "working" || entry.sending || !!entry.uploads);
  $("chat-composer").classList.toggle?.("is-error", !!entry.error || entry.receipt?.status === "failed");
  $("chat-composer").classList.toggle?.("is-unknown", !!entry.pending && !entry.sending);
  setClass($("chat-composer"), "is-sent-draft", sentDraftHidden(entry));
  const offline = networkOffline();
  const blocked = offline || !supported || entry.sending || !!entry.pending || !!entry.uploads;
  // No board terminal yet (Claude background): the draft can be written now and
  // sent once the user opened the terminal from the gate.
  const gateOn = !supported && terminalRequired(entry);
  const gateReason = entry.data?.capability?.reason || "보내려면 먼저 터미널을 여세요.";
  $("chat-terminal-gate").hidden = !gateOn;
  $("chat-terminal-gate-text").textContent = gateOn ? gateReason : "";
  $("chat-open-terminal").disabled = offline || state.launchBusy;
  // A send in flight (or unconfirmed) locks the text but not the field: a
  // disabled field would drop the focus, and with it the keyboard and the
  // next ⌘↩. sendChat itself refuses a second send meanwhile.
  const input = $("chat-input");
  input.disabled = !supported && !gateOn;
  input.readOnly = !input.disabled && (entry.sending || !!entry.pending);
  input.setAttribute("aria-disabled", String(input.disabled || input.readOnly));
  $("chat-attach").disabled = offline || !chatAttachmentsSupported(entry) || entry.sending || !!entry.pending || !!entry.uploads;
  $("chat-clipboard").disabled = $("chat-attach").disabled;
  $("chat-send").disabled = blocked || entry.composing || (!entry.text.trim() && !entry.attachments.length);
  $("chat-send").textContent = entry.sending ? "전송 중" : "보내기";
  $("chat-send").setAttribute("aria-label", "메시지 보내기");
  $("chat-key-hint").hidden = !!entry.expanded || !keyboardPresent();
  $("chat-check").disabled = offline || entry.checking;
  $("chat-terminal").disabled = state.launchBusy;
  const native = terminalInfo(state.terminal).nativeSource;
  $("chat-native-open").hidden = !native || chatKey(native) === chatKey(entry.source) || !state.terminalAlive;
  const openTerminalRow = terminalForSession(selectedSession());
  $("chat-terminal-close").hidden = !openTerminalRow;
  if (openTerminalRow) $("chat-terminal-close").textContent = terminalCloseCopy(openTerminalRow).action;
  const phase = entry.data?.phase;
  const phaseCopy = phase === "working" ? "작업 중 · 지금 보내도 CLI가 이어서 반영합니다." : phase === "needs_input" ? (entry.data?.prompt ? "답변·승인 필요 · 위 카드에서 답해 주세요." : "답변·승인 필요 · 터미널에서 처리해 주세요.") : entry.expanded ? "위의 실제 세션에 메시지를 전달합니다." : "";
  const budgetNote = state.chatPollCount >= CHAT_POLL_MAX ? " 자동 상태 확인을 잠시 멈췄습니다. 상태 다시 확인을 눌러 주세요." : "";
  // The gate itself says what to do; the status line only adds that a refused
  // message was not sent.
  const unsupportedCopy = gateOn ? (entry.receipt?.dispatchState === "not_started"
    ? "메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다." : "") : !supported && entry.attachments.length
    ? "첨부는 이 작업 폴더에 저장됐습니다. 첨부의 ‘경로’를 복사해 터미널에 붙여넣으세요."
    : entry.data?.capability?.reason || "직접 입력을 지원하지 않습니다. 터미널을 사용해 주세요.";
  $("chat-status").textContent = (offline ? "오프라인입니다. 초안은 유지됩니다. 연결 후 상태를 확인하고 보내 주세요." : entry.error || entry.notice || (entry.uploads ? `첨부 ${entry.uploads}개를 올리고 있습니다…` : !entry.data ? (claudeWithoutId(selectedSession()) ? CLAUDE_NO_SEND : "메시지 전송 환경을 확인하고 있습니다…") : !supported ? unsupportedCopy : receiptNotice(entry) || phaseCopy)) + budgetNote;
  const deliveryVisible = updateChatDeliveryEvidence(entry);
  const chips = $("chat-attachments");
  // While a bubble resends its own message, another draft's files stay as they are.
  const draftSent = draftIsPending(entry);
  chips.hidden = draftSent && deliveryVisible;
  const attachmentsLabel = $("chat-attachments-label");
  attachmentsLabel.hidden = chips.hidden || !entry.attachments.length;
  const selectedCaption = draftSent
    ? `전송 요청 첨부 ${entry.attachments.length}개 · 결과 미확인`
    : `전송 전 첨부 ${entry.attachments.length}개 · 업로드 완료`;
  attachmentsLabel.textContent = selectedCaption;
  chips.setAttribute("aria-label", selectedCaption);
  renderAttachmentTiles(entry, chips);
  resizeChatInput(entry);
}
// Attachment tiles: a row that scrolls sideways. Images show a local preview
// (an object URL of the picked file, memory only, revoked once the tile is
// gone); uploads show a progress ring.
function attachmentThumb(entry, key, file) {
  entry.thumbs ||= new Map();
  if (!String(file?.type || "").startsWith("image/") || entry.thumbs.has(key)) return;
  try {
    const url = URL.createObjectURL?.(file);
    if (url) entry.thumbs.set(key, url);
  } catch {
    /* no preview for this file */
  }
}
function revokeThumb(entry, key) {
  const url = entry.thumbs?.get(key);
  if (!url) return;
  entry.thumbs.delete(key);
  try {
    URL.revokeObjectURL?.(url);
  } catch {
    /* already released */
  }
}
function pruneThumbs(entry) {
  if (!entry.thumbs?.size) return;
  const live = new Set([...entry.attachments.map((item) => item.id), ...(entry.uploadQueue || []).map((item) => item.key)]);
  for (const key of [...entry.thumbs.keys()]) if (!live.has(key)) revokeThumb(entry, key);
}
function attachmentTile(entry, file, { key, uploading = null } = {}) {
  const image = String(file.type || "").startsWith("image/") || /\.(png|jpe?g|gif|webp|heic|bmp)$/i.test(file.name || "");
  const tile = el("div", `chat-attachment${image ? " is-image" : " is-file"}${uploading ? " is-uploading" : ""}`);
  tile.dataset.key = key;
  const thumb = entry.thumbs.get(key);
  const preview = el("span", "chat-attachment-preview");
  preview.setAttribute("aria-hidden", "true");
  if (thumb) {
    const img = el("img");
    img.src = thumb;
    img.alt = "";
    img.decoding = "async";
    preview.append(img);
  } else preview.append(el("span", "chat-attachment-ext", (String(file.name || "").split(".").pop() || "").slice(0, 4).toUpperCase()));
  tile.append(preview);
  tile.append(el("span", "chat-attachment-name", `${file.name || "첨부파일"} · ${formatBytes(file.size)}`));
  if (uploading) {
    const ring = el("span", "upload-ring");
    ring.setAttribute("role", "progressbar");
    ring.setAttribute("aria-label", `${file.name || "첨부파일"} 올리는 중`);
    tile.uploadRing = ring;
    tile.append(ring);
    setUploadProgress(tile, uploading);
    return tile;
  }
  const remove = button("×", "chat-attachment-remove", () => {
    if (entry.sending || entry.pending) return;
    entry.attachments = entry.attachments.filter((item) => item.id !== file.id);
    revokeThumb(entry, file.id);
    entry.revision++;
    updateChatComposer(entry);
  });
  remove.setAttribute("aria-label", `${file.name || "첨부파일"} 제거`);
  remove.disabled = entry.sending || !!entry.pending;
  tile.append(remove);
  if (file.path) {
    const copy = button("경로", "chat-attachment-copy", async () => {
      try { await navigator.clipboard.writeText(file.path); toast("실제 세션의 첨부 경로를 복사했습니다. 같은 작업의 터미널에 붙여넣을 수 있습니다."); }
      catch { toast("클립보드에 접근할 수 없습니다."); }
    });
    copy.setAttribute("aria-label", `${file.name || "첨부파일"} 실제 경로 복사`);
    copy.title = "실제 첨부 경로 복사";
    tile.append(copy);
  }
  return tile;
}
function setUploadProgress(tile, item) {
  const ring = tile?.uploadRing;
  if (!ring) return;
  const percent = item.state === "uploading" && Number.isFinite(item.progress) ? Math.round(item.progress * 100) : null;
  ring.style?.setProperty?.("--progress", `${percent ?? 0}%`);
  setClass(ring, "is-waiting", percent === null);
  if (percent === null) ring.removeAttribute?.("aria-valuenow");
  else ring.setAttribute("aria-valuenow", String(percent));
}
function renderAttachmentTiles(entry, box = $("chat-attachments")) {
  entry.thumbs ||= new Map();
  entry.uploadQueue ||= [];
  pruneThumbs(entry);
  const tiles = [
    ...entry.attachments.map((file) => attachmentTile(entry, file, { key: file.id })),
    ...entry.uploadQueue.map((item) => attachmentTile(entry, item.file, { key: item.key, uploading: item })),
  ];
  for (const item of entry.uploadQueue) item.tile = tiles.find((tile) => tile.dataset.key === item.key) || null;
  box.replaceChildren(...tiles);
}
// Uploads report progress through XMLHttpRequest where it exists; fetch is
// the fallback (same request, no progress).
function sendUpload(file, headers, onProgress) {
  if (typeof XMLHttpRequest !== "function") {
    return fetch("/api/chat/upload", { method: "POST", credentials: "same-origin", cache: "no-store", body: file, headers })
      .then(async (response) => ({ status: response.status, ok: response.ok, data: await response.json().catch(() => ({})) }));
  }
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/chat/upload");
    for (const [name, value] of Object.entries(headers)) request.setRequestHeader(name, value);
    request.upload?.addEventListener?.("progress", (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    });
    request.addEventListener("load", () => {
      let data = {};
      try {
        data = JSON.parse(request.responseText || "{}") || {};
      } catch {
        data = {};
      }
      resolve({ status: request.status, ok: request.status >= 200 && request.status < 300, data });
    });
    request.addEventListener("error", () => reject(new Error("첨부파일을 올리지 못했습니다. 연결을 확인해 주세요.")));
    request.addEventListener("abort", () => reject(new Error("첨부파일 올리기를 멈췄습니다.")));
    request.send(file);
  });
}
function stopChatPolling() {
  clearTimeout(state.chatPollTimer);
  state.chatPollTimer = null;
  state.chatPollRun++;
  state.chatPollCount = 0;
}
const CHAT_POLL_MAX = 240;
function chatPollDelay(count) {
  // Live updates announce changes; polling is then only a safety net.
  if (eventsHealthy()) return EVENTS_SAFETY_MS.chat;
  // Fast while a reply is likely, then slower; long turns keep being followed.
  return count < 10 ? 3000 : count < 30 ? 5000 : count < 90 ? 10000 : 20000;
}
function scheduleChatPoll(s, entry) {
  if (!currentChat(entry)) return;
  clearTimeout(state.chatPollTimer);
  // A bubble still waiting for its message keeps the conversation followed, so it
  // goes when the message (or an answer after it) shows, or when it times out.
  if (networkOffline() || !currentChat(entry) || !chatVisible() || state.chatPollCount >= CHAT_POLL_MAX ||
      (!entry.pending && !(entry.error && entry.error !== entry.inputError) && !entry.data?.prompt &&
       !["working", "needs_input"].includes(entry.data?.phase) &&
       !(entry.outbox || []).some(outboxAwaitsAgent))) return;
  const run = state.chatPollRun;
  state.chatPollTimer = setTimeout(() => {
    if (run !== state.chatPollRun || !currentChat(entry) || !chatVisible()) return;
    state.chatPollCount++;
    loadChatState(s, false);
  }, chatPollDelay(state.chatPollCount));
}
function applyChatReceipt(entry, receipt) {
  if (!receipt || (entry.pending && receipt.requestId !== entry.pending.requestId)) return;
  const fallback = entry.pending?.attachmentDetails ||
    (entry.receipt?.requestId === receipt.requestId ? entry.receipt.attachments : []);
  entry.receipt = { ...receipt, attachments: attachmentMetadata(Array.isArray(receipt.attachments) ? receipt.attachments : fallback) };
  markOutbox(entry, receipt.requestId, entry.receipt);
  if (!entry.pending || !["accepted", "queued", "completed", "interrupted", "failed"].includes(receipt.status)) return;
  // Only the draft that was sent is cleared; a resend from a bubble leaves
  // any other draft in the editor.
  if ((receipt.status !== "failed" || receipt.confirmed === true) && entry.revision === entry.pending.revision &&
      draftIsPending(entry)) {
    entry.text = "";
    entry.attachments = [];
    if (currentChat(entry)) {
      $("chat-input").value = "";
      resizeChatInput(entry);
    }
  }
  entry.pending = null;
}
async function loadChatState(s, manual = false, { join = false } = {}) {
  const entry = chatEntry(s);
  if (!currentChat(entry) || !chatVisible() || networkOffline()) return;
  // Without an id the session has no chat state; its transcript (if named) is read directly.
  if (claudeWithoutId(s)) return { status: "skipped" };
  const identity = conversationIdentity(s);
  if (entry.checking) {
    if (join) {
      if (entry.stateReadIdentity === identity && entry.stateReadAuthEpoch === state.authEpoch)
        return entry.stateReadPromise;
      // A different cwd/transcript is a new read, not a retry of the old view.
      await entry.stateReadPromise;
      return loadChatState(s, manual, { join: true });
    }
    if (manual) entry.recheckQueued = true;
    return;
  }
  entry.checking = true;
  let complete;
  entry.stateReadPromise = new Promise((resolve) => { complete = resolve; });
  entry.stateReadIdentity = identity;
  entry.stateReadAuthEpoch = state.authEpoch;
  let outcome = { status: "skipped" };
  const epoch = entry.stateEpoch;
  const pollRun = state.chatPollRun;
  let readFailed = false;
  // A read clears its own (and a send's) errors; an input error (a file too
  // large, too many files, too long a message) stays until the user acts again.
  const clearError = () => { if (entry.error !== entry.inputError) entry.error = ""; };
  if (manual) { state.chatPollCount = 0; clearError(); }
  updateChatComposer(entry);
  try {
    const lastRequestId = entry.pending?.requestId ||
      (entry.receipt?.dispatchState === "not_started" || ["completed", "interrupted", "failed"].includes(entry.receipt?.status) ? null : entry.receipt?.requestId);
    const requestIds = submissionRequestIds(entry, lastRequestId);
    const data = await api("/api/chat/state", { method: "POST", body: {
      source: entry.source, ...(lastRequestId ? { requestId: lastRequestId } : {}),
      ...(requestIds.length ? { requestIds } : {}),
    } });
    if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
    if (epoch !== entry.stateEpoch || currentChat(entry) && conversationIdentity(selectedSession()) !== identity) return;
    entry.data = data;
    outcome = { status: "success", data };
    clearError();
    for (const receipt of data.receipts || []) {
      if (receipt.requestId === entry.pending?.requestId ||
          (entry.receipt?.dispatchState !== "not_started" && receipt.requestId === entry.receipt?.requestId))
        applyChatReceipt(entry, receipt);
    }
    renderPrompt(s, data);
    if (chatReadSupported(entry, data)) {
      rememberConversation(s, data.messages, data);
      if (currentChat(entry)) {
        renderMessages(s, data.messages);
        conversationSyncNotice(s, data.stale ? "stale" : "");
      }
    }
    noteLivePhase(s, data);
    scheduleChatPoll(s, entry);
  } catch (err) {
    readFailed = true;
    outcome = { status: "failed", error: err };
    if (!err.unauthorized && !err.staleAuth && epoch === entry.stateEpoch &&
        currentChat(entry) && conversationIdentity(selectedSession()) === identity)
      conversationSyncNotice(s, "failed");
    if (!err.unauthorized && !err.staleAuth && epoch === entry.stateEpoch) {
      // A connection problem that just started shows the bars (its notice is in them).
      if (!entry.error && currentChat(entry)) revealChrome();
      entry.error = `${err.message} 상태 다시 확인을 눌러 주세요.`;
    }
    // A transient failure keeps following the session with the same backoff.
    if (currentChat(entry) && pollRun === state.chatPollRun && !err.unauthorized && !err.staleAuth)
      scheduleChatPoll(s, entry);
  } finally {
    entry.checking = false;
    complete(outcome);
    entry.stateReadPromise = null;
    updateChatComposer(entry);
    const recheck = entry.recheckQueued || epoch !== entry.stateEpoch;
    entry.recheckQueued = false;
    if (recheck && !readFailed && currentChat(entry) && chatVisible() && !networkOffline())
      queueMicrotask(() => loadChatState(s, false));
  }
  return outcome;
}
// Whether the editor still holds exactly the message in flight.
function draftIsPending(entry) {
  const pending = entry.pending;
  return !!pending && entry.text === pending.text && entry.attachments.length === pending.attachments.length &&
    entry.attachments.every((item, index) => item.id === pending.attachments[index]);
}
// Sends the editor's draft, or `message` ({ text, attachments }) when a
// bubble resends what it showed; the editor is then left as it is.
async function sendChat(entry, message = null) {
  if (networkOffline()) { updateChatComposer(entry); return; }
  const text = message ? String(message.text || "") : entry.text;
  const attachments = (message ? message.attachments || [] : entry.attachments).slice();
  if (!currentChat(entry) || !chatSupported(entry) || entry.composing || entry.sending || entry.pending || entry.uploads ||
      (!text.trim() && !attachments.length)) return;
  if (new TextEncoder().encode(text).length > 32768 || attachments.length > 8) {
    entry.error = entry.inputError = "메시지는 32KiB, 첨부는 8개까지 보낼 수 있습니다. 초안은 유지됩니다.";
    updateChatComposer(entry);
    return;
  }
  // Keep the exact source and idempotency key even if selection changes during transport.
  const authEpoch = state.authEpoch;
  const pending = { requestId: requestId(), revision: entry.revision, source: entry.source,
    text, attachments: attachments.map((item) => item.id),
    attachmentDetails: attachmentMetadata(attachments) };
  entry.pending = pending;
  entry.stateEpoch++;
  entry.sending = true;
  entry.error = "";
  entry.notice = "";
  // The bubble shows at once, translucent, and becomes the real message
  // when the transcript has it. A failed earlier attempt is superseded.
  entry.outbox = (entry.outbox || []).filter((item) => item.state !== "failed");
  entry.outbox.push({ requestId: pending.requestId, text: pending.text, attachments: pending.attachmentDetails,
    attachmentObjects: attachments, state: "sending", status: "", reason: "", at: Date.now(),
    ...outboxOrigin(entry.conversation?.messages) });
  updateChatComposer(entry);
  refreshConversationView(entry, true);
  try {
    const result = await api("/api/chat/send", { method: "POST", body: {
      source: pending.source, requestId: pending.requestId, text: pending.text, attachments: pending.attachments,
    }, authEpoch });
    if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
    if (result.receipt?.requestId && result.receipt.requestId !== pending.requestId)
      throw new Error("요청 확인 번호가 일치하지 않습니다.");
    applyChatReceipt(entry, { ...result.receipt, requestId: pending.requestId, status: result.status || "unknown" });
    if (["accepted", "queued", "completed"].includes(result.status)) haptic("success");
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      haptic("error");
      if (err.dispatchState === "not_started") {
        applyChatReceipt(entry, { requestId: pending.requestId, status: "failed", confirmed: false,
          dispatchState: "not_started", reason: err.message });
        entry.error = `${err.message} 메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.`;
        // The terminal closed since the last read: offer the button at once
        // (the read that follows says the same).
        if (err.code === "terminal_required" && entry.data)
          entry.data = { ...entry.data, capability: { ...(entry.data.capability || {}), supported: false, action: "open_terminal" } };
        if (err.code === "csrf_expired") {
          try {
            // Refresh credentials once; keep the current editor mounted and
            // require a new explicit send rather than replaying this request.
            await loadCapabilities(authEpoch, { refreshView: false });
            assertAuthEpoch(authEpoch);
            entry.receipt.reason = "접속 설정을 갱신했습니다. 내용을 확인한 뒤 보내기를 다시 눌러 주세요.";
            entry.error = "";
          } catch (refreshError) {
            if (authEpoch === state.authEpoch && !refreshError.unauthorized && !refreshError.staleAuth) {
              entry.receipt.reason = "접속 설정을 갱신하지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.";
              entry.error = chatReceiptCopy(entry.receipt);
            }
          }
        }
      } else {
        // HTTP status alone cannot establish whether the native runtime started.
        entry.receipt = { requestId: pending.requestId, status: "unknown", attachments: pending.attachmentDetails };
        entry.error = "전송 결과를 확인하지 못했습니다. 상태 다시 확인을 눌러 주세요. 중복 전송은 잠겼습니다.";
      }
    }
  } finally {
    entry.stateEpoch++;
    entry.sending = false;
    if (entry.receipt?.requestId === pending.requestId) markOutbox(entry, pending.requestId, entry.receipt);
    else if (entry.pending?.requestId === pending.requestId) markOutbox(entry, pending.requestId, { status: "unknown" });
    updateChatComposer(entry);
    refreshConversationView(entry);
    // The result (sent, refused or unknown) is read in the composer's status.
    if (currentChat(entry)) revealChrome();
    const s = selectedSession();
    if (currentChat(entry) && s) { state.chatPollCount = 0; loadChatState(s, true); }
  }
}
// --- Sent-from-here bubbles (outbox) ------------------------------------------
// Memory only, per conversation. "sending" until the server answers; then
// "sent" (accepted or queued), "uncertain" (no answer: the send stays locked
// as before) or "failed" (never started: the draft is still in the editor).
const OUTBOX_LABELS = { sending: "전송 중…", uncertain: "전송 확인 중", sent: "전송됨",
  queued: "대기 중 · 지금 작업이 끝나면 전달됩니다", failed: "보내지 못함" };
const OUTBOX_SETTLED_MS = 3 * 60 * 1000;
const OUTBOX_MAX_MS = 15 * 60 * 1000;
// A transcript may record a message a little before the browser's clock says
// it was sent (clocks of the phone and the agent host differ slightly).
const OUTBOX_CLOCK_SLACK_MS = 30 * 1000;
// Settled messages remembered per conversation (the server shows 80 at most).
const OUTBOX_CLAIM_LIMIT = 80;
const OUTBOX_ATTACHMENT_LINE = /^첨부 파일:/;
const OUTBOX_ATTACHMENT_ONLY = new Set(["[이미지 첨부]", "[파일 첨부]"]);
// Claude Code records a pasted image path as "[Image #1]" in the prompt (before,
// after or instead of the words) and may show a long paste as
// "[Pasted text #1 +12 lines]"; the board's own words never contain them.
const CLAUDE_INPUT_TOKEN = /\[(?:Image|Pasted text|Pasted image) #\d+(?: \+\d+ lines?)?\]/g;
const CLAUDE_IMAGE_TOKEN = /[ \t]*\[(?:Image|Pasted image) #\d+\][ \t]*/g;
// A sent bubble waits this long for its own message before an answer recorded
// after it settles it while the agent still works (a message sent mid-turn is
// recorded when the agent takes it, after the rest of the answer in progress).
const OUTBOX_REPLY_GRACE_MS = 60 * 1000;
function outboxText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}
// A transcript user message as a bubble compares it: the body without Claude
// Code's input tokens and the "첨부 파일: …" lines the server appends for
// attachments, and the possible attachment counts (native attachments and
// those lines, which a transcript may record once or twice for the same file).
function outboxShape(message) {
  const lines = String(message?.text || "").replace(CLAUDE_INPUT_TOKEN, " ").split("\n");
  let marked = 0;
  while (lines.length) {
    const last = lines[lines.length - 1].trim();
    if (last && !OUTBOX_ATTACHMENT_LINE.test(last)) break;
    lines.pop();
    if (last) marked++;
  }
  const native = attachmentMetadata(message?.attachments).length;
  const truncated = message?.truncated === true;
  let body = outboxText(lines.join("\n"));
  if (truncated) body = body.replace(/\s*…$/, "");
  if ((native || marked) && OUTBOX_ATTACHMENT_ONLY.has(body)) body = "";
  return { body, counts: new Set([native + marked, Math.max(native, marked)]), truncated };
}
// A transcript row's stable identifier: Codex rows from /api/chat/state name
// their item; /api/read and Claude rows carry none.
function outboxRowId(message) {
  return typeof message?.itemId === "string" && message.itemId ? message.itemId : null;
}
// A message as every read path shows it (role, text and time), whatever
// attachment details or identifiers that path adds.
function outboxPrint(message) {
  return JSON.stringify([message?.role || "", message?.text || "", message?.ts ?? null]);
}
// What a bubble keeps of the conversation it was sent into, so a message that
// was already there never counts as the one it sent: the user rows' item ids
// when that read named them, and the message count with the last message.
function outboxOrigin(messages) {
  const list = Array.isArray(messages) ? messages : [];
  const named = list.some((message) => outboxRowId(message));
  return { base: list.length, anchor: list.length ? outboxPrint(list[list.length - 1]) : null,
    known: named ? list.filter((message) => message?.role === "user").map(outboxRowId).filter(Boolean) : null };
}
// Where the messages sent after this bubble start: just after the last message
// it saw (found again even when the server's window moved), else its count.
function outboxCut(item, messages) {
  const base = Math.max(0, Number(item.base) || 0);
  if (!base || !item.anchor) return base;
  if (base <= messages.length && outboxPrint(messages[base - 1]) === item.anchor) return base;
  for (let index = messages.length - 1; index >= 0; index--)
    if (outboxPrint(messages[index]) === item.anchor) return index + 1;
  return base;
}
// Whether a transcript user message (row: { message, index, shape, id, taken })
// is the one this bubble sent (view: { codex, cut, known, loose } of this
// read). A message already there at the send never is. Codex names it by
// request id, and nothing else settles a Codex bubble; otherwise it is a
// message after those the bubble saw, with the same body and attachment
// count, and not recorded before the send (the clock slack only narrows it
// further). A loose view (the Claude safety net) skips the body comparison.
function outboxMatches(item, row, view = {}) {
  const known = view.known || null;
  if (row.id && known?.has(row.id)) return false;
  const clientId = row.message?.clientId;
  if (typeof clientId === "string" && clientId) return clientId === item.requestId;
  if (view.codex || row.taken) return false;
  if (!(row.id && known) && row.index < (view.cut ?? (Number(item.base) || 0))) return false;
  if (!outboxAfterSend(item, row.message)) return false;
  if (view.loose) return true;
  const body = outboxText(item.text);
  // A cut long message keeps only its beginning (and loses the attachment lines).
  if (row.shape.truncated) return !!row.shape.body && body.startsWith(row.shape.body);
  return row.shape.body === body && row.shape.counts.has(item.attachments?.length || 0);
}
// Not recorded before the send (a message without a time may be after it).
function outboxAfterSend(item, message) {
  const ts = Number(message?.ts);
  return !(Number.isFinite(ts) && ts > 0 && ts * 1000 < item.at - OUTBOX_CLOCK_SLACK_MS);
}
// A bubble the server took (accepted, or a later turn status): not one still
// sending, unconfirmed, refused, or queued behind the running turn.
function outboxDelivered(item) {
  return item.state === "sent" && item.status !== "queued";
}
// A bubble whose message the agent has yet to show: it keeps "작업 중" after
// it. A refused one, or one whose request already ended, does not.
function outboxAwaitsAgent(item) {
  return item.state !== "failed" && !item.settledAt;
}
// The same on /api/chat/state and /api/read: attachment details differ between
// them (state names the board's files), the time and text do not.
function outboxClaimKey(row) {
  return JSON.stringify([row.message?.ts ?? null, row.message?.text || ""]);
}
function outboxState(receipt) {
  if (!receipt) return "uncertain";
  if (receipt.dispatchState === "not_started") return "failed";
  if (receipt.status === "failed") return receipt.confirmed === true ? "sent" : "failed";
  if (["accepted", "queued", "completed", "interrupted"].includes(receipt.status)) return "sent";
  return "uncertain";
}
function markOutbox(entry, requestId, receipt) {
  const item = entry.outbox?.find((value) => value.requestId === requestId);
  if (!item) return;
  item.state = outboxState(receipt);
  item.status = receipt?.status || "unknown";
  item.reason = item.state === "failed" ? String(receipt?.reason || "") : "";
  if (item.state === "failed" || ["completed", "interrupted"].includes(receipt?.status)) item.settledAt ||= Date.now();
  // The server found the message in the conversation it read.
  if (item.state !== "failed" && receipt?.readbackConfirmed === true) item.recorded = true;
}
// Drops bubbles whose message reached the transcript, settled ones the
// transcript never showed, and anything older than OUTBOX_MAX_MS. Each
// transcript message settles at most one bubble, in the order they were
// sent, and stays claimed on later renders (from either read path) so it
// never settles another. Every bubble is first matched by its own text; a
// delivered Claude bubble left over then takes the next new user message
// (Claude Code may record the words differently), or goes once the agent
// answered after it (`working`: the conversation's agent still works).
// Returns whether a bubble went.
function settleOutbox(entry, messages, { working = false, claimed = null } = {}) {
  if (!entry) return false;
  const now = Date.now();
  entry.outboxClaims = (entry.outboxClaims || []).filter((claim) => now - claim.at < OUTBOX_MAX_MS);
  if (!entry.outbox?.length) return false;
  const list = Array.isArray(messages) ? messages : [];
  const rows = [];
  list.forEach((message, index) => {
    if (message?.role === "user") rows.push({ message, index, shape: outboxShape(message), id: outboxRowId(message), taken: false });
  });
  for (const claim of entry.outboxClaims) {
    const row = rows.find((value) => !value.taken && (claim.id && value.id ? claim.id === value.id
      : outboxClaimKey(value) === claim.key && (value.message.ts != null || value.index >= claim.base)));
    if (row) row.taken = true;
  }
  const codex = entry.source?.agent === "codex";
  const settled = new Set();
  const views = new Map();
  const claim = (item, row, view) => {
    row.taken = true;
    settled.add(item);
    claimed?.add(row.message);
    entry.outboxClaims.push({ id: row.id, key: outboxClaimKey(row), base: view.cut, at: now });
  };
  for (const item of entry.outbox) {
    if (item.state === "failed") continue;
    const view = { codex, cut: outboxCut(item, list), known: Array.isArray(item.known) ? new Set(item.known) : null };
    views.set(item, view);
    const row = rows.find((value) => outboxMatches(item, value, view));
    if (row) claim(item, row, view);
  }
  for (const item of entry.outbox) {
    if (settled.has(item) || item.state === "failed") continue;
    const view = { ...views.get(item), loose: true };
    if (!codex && outboxDelivered(item)) {
      const row = rows.find((value) => outboxMatches(item, value, view));
      if (row) { claim(item, row, view); continue; }
    }
    // The server read the conversation and found the message there.
    if (item.recorded) { settled.add(item); continue; }
    if (!codex && outboxDelivered(item)) {
      // An answer recorded after the send itself (no clock slack: an answer the
      // bubble's view of the conversation had not caught yet is no reply).
      const answered = list.slice(view.cut).some((message) => {
        const ts = Number(message?.ts);
        return message?.role === "assistant" && !(Number.isFinite(ts) && ts > 0 && (ts + 1) * 1000 <= item.at);
      });
      if (answered && (!working || now - item.at > OUTBOX_REPLY_GRACE_MS)) settled.add(item);
    }
  }
  if (entry.outboxClaims.length > OUTBOX_CLAIM_LIMIT)
    entry.outboxClaims.splice(0, entry.outboxClaims.length - OUTBOX_CLAIM_LIMIT);
  const before = entry.outbox.length;
  entry.outbox = entry.outbox.filter((item) => {
    const keep = !settled.has(item) &&
      !(item.state !== "failed" && item.settledAt && now - item.settledAt > OUTBOX_SETTLED_MS) &&
      now - item.at < OUTBOX_MAX_MS;
    // The composer's "접수했습니다" note was about this message; the conversation
    // now says the rest.
    if (!keep && item.state !== "failed" && entry.receipt?.requestId === item.requestId)
      entry.settledRequestId = item.requestId;
    return keep;
  });
  return entry.outbox.length !== before;
}
// Resends exactly what the failed bubble showed. Another draft in the editor
// stays there, unsent; an empty editor gets the message back so a second
// refusal still leaves it to edit.
function retryOutbox(entry, item) {
  if (!currentChat(entry) || entry.sending || entry.pending || entry.uploads || entry.composing) return;
  const message = { text: String(item.text || ""), attachments: (item.attachmentObjects || []).slice() };
  if (!message.text.trim() && !message.attachments.length) {
    entry.outbox = (entry.outbox || []).filter((value) => value !== item);
    refreshConversationView(entry);
    return;
  }
  if (!entry.text.trim() && !entry.attachments.length) {
    entry.text = message.text;
    entry.attachments = message.attachments.slice();
    entry.revision++;
    const input = $("chat-input");
    if (input && $("chat-composer")?.dataset.source === chatKey(entry.source)) input.value = entry.text;
  }
  // sendChat replaces the failed bubble once the resend starts.
  sendChat(entry, message);
}
function outboxRows(list, entry) {
  list.outboxNodes ||= new Map();
  const rows = [];
  const keep = new Set();
  for (const item of entry.outbox || []) {
    keep.add(item.requestId);
    let row = list.outboxNodes.get(item.requestId);
    if (!row) {
      row = el("article", "msg user is-outbox");
      const bubble = el("div", "bubble");
      if (item.text) bubble.append(el("span", "msg-body", item.text));
      if (item.attachments?.length)
        bubble.append(attachmentGroup(item.attachments, `첨부 ${item.attachments.length}개`, { showCaption: false }));
      const status = el("p", "msg-delivery");
      status.setAttribute("role", "status");
      row.append(bubble, status);
      row.deliveryStatus = status;
      list.outboxNodes.set(item.requestId, row);
    }
    const name = item.state === "sent" && item.status === "queued" ? "queued" : item.state;
    const className = `msg user is-outbox is-${item.state}`;
    if (row.className !== className) row.className = className;
    const revision = `${name}|${item.reason}`;
    if (row.deliveryRevision !== revision) {
      row.deliveryRevision = revision;
      // The reason stays in the composer's status line above the kept draft.
      row.deliveryStatus.title = item.state === "failed" ? item.reason : "";
      if (item.state === "failed") {
        const retry = button("다시 보내기", "msg-retry", () => retryOutbox(entry, item));
        retry.setAttribute("aria-label", "보내지 못한 메시지 다시 보내기");
        row.deliveryStatus.replaceChildren(el("span", "msg-delivery-text", OUTBOX_LABELS.failed), retry);
      } else row.deliveryStatus.replaceChildren(el("span", "msg-delivery-text", OUTBOX_LABELS[name] || OUTBOX_LABELS.uncertain));
    }
    rows.push(row);
  }
  for (const key of [...list.outboxNodes.keys()]) if (!keep.has(key)) list.outboxNodes.delete(key);
  return rows;
}
// "··· 작업 중 · 12분 34초" at the end of a conversation that waits on its
// working agent (renderMessages decides), counted from an exact workingSince
// (epoch seconds); without one (Claude) it says "··· 작업 중" alone.
function workingRow(list) {
  if (list.workingRow) return list.workingRow;
  const row = el("article", "msg assistant msg-working");
  row.setAttribute("role", "status");
  const bubble = el("div", "bubble working-bubble");
  const dots = el("span", "working-dots");
  dots.setAttribute("aria-hidden", "true");
  dots.append(el("i"), el("i"), el("i"));
  // The ticking time is not announced every second; the status is.
  row.workingTime = el("span", "working-time");
  row.workingTime.setAttribute("aria-live", "off");
  const label = el("span", "working-label", "작업 중");
  label.append(row.workingTime);
  bubble.append(dots, label);
  row.append(bubble);
  list.workingRow = row;
  return row;
}
// Under an hour minutes and seconds ("12분 34초"), then hours and minutes.
function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total}초`;
  if (total < 3600) return `${Math.floor(total / 60)}분 ${total % 60}초`;
  const minutes = Math.floor((total % 3600) / 60);
  return `${Math.floor(total / 3600)}시간${minutes ? ` ${minutes}분` : ""}`;
}
function workingElapsedText(since, now = Date.now()) {
  return Number.isFinite(since) && since > 0 ? ` · ${formatElapsed(now / 1000 - since)}` : "";
}
function stopWorkingClock() {
  clearTimeout(state.workingTimer);
  state.workingTimer = null;
}
// Updates the shown working time, then once a second while the conversation
// is on screen: not on a phone's list, behind a terminal or on a hidden page
// (visibilitychange and setDetailShown stop and restart it). Reduced motion
// stops only the dots; the time keeps counting.
function tickWorking() {
  stopWorkingClock();
  const list = $("messages");
  const row = list?.workingRow;
  if (!row || ![...(list.children || [])].includes(row)) return;
  const text = workingElapsedText(list.workingSince);
  if (row.workingTime.textContent !== text) row.workingTime.textContent = text;
  if (!text || !chatVisible()) return;
  state.workingTimer = setTimeout(tickWorking, 1000 - (Date.now() % 1000) + 20);
}
// `claude agents` can list a working interactive session without an id (and
// then often without its transcript's session id too): the board can neither
// send to it nor, without a session id, read it.
const CLAUDE_UNREADABLE = "이 Claude 세션은 아직 대화 기록 ID를 알 수 없어 여기서 읽을 수 없습니다. " +
  "잠시 뒤 다시 열거나 터미널에서 확인해 주세요.";
const CLAUDE_NO_SEND = "이 Claude 세션은 세션 ID가 없어 여기서 보낼 수 없습니다. 터미널에서 이어가 주세요.";
function claudeWithoutId(s) {
  return s?.agent === "claude" && !s.nativeSource && !s.id;
}
// Re-renders the open conversation from what is already on screen (new
// bubble states, the working indicator), optionally scrolling to the end.
function refreshConversationView(entry, toEnd = false) {
  const list = $("messages");
  const s = selectedSession();
  if (!s || !list || !currentChat(entry) || list.conversationSource !== chatKey(entry.source)) return;
  renderMessages(s, null, list.conversationErrorText || "", toEnd);
}
async function uploadChatFiles(entry, files) {
  if (networkOffline()) { updateChatComposer(entry); return; }
  if (!currentChat(entry) || !chatAttachmentsSupported(entry) || entry.sending || entry.pending || entry.uploads || !files.length) return;
  if (entry.attachments.length + files.length > 8) {
    entry.error = entry.inputError = "한 메시지에는 첨부 파일을 8개까지 추가할 수 있습니다.";
    updateChatComposer(entry);
    return;
  }
  const authEpoch = state.authEpoch;
  entry.uploads = files.length;
  entry.error = "";
  entry.notice = "";
  const queue = files.map((file, index) => ({ key: `upload-${Date.now()}-${index}`, file, state: "waiting", progress: null }));
  for (const item of queue) attachmentThumb(entry, item.key, item.file);
  entry.uploadQueue = queue;
  const unqueue = (item) => {
    entry.uploadQueue = entry.uploadQueue.filter((value) => value !== item);
  };
  updateChatComposer(entry);
  try {
    for (const item of queue) {
      const file = item.file;
      if (authEpoch !== state.authEpoch) { entry.uploads = 0; return; }
      if (networkOffline()) {
        entry.notice = `아직 올리지 못한 파일 ${entry.uploads}개가 있습니다. 다시 첨부해 주세요.`;
        entry.uploads = 0; updateChatComposer(entry); break;
      }
      try {
        const limit = entry.data.maxAttachmentBytes || 20 * 1024 * 1024;
        if (file.size > limit) throw new Error(`${file.name || "파일"}: 최대 ${formatBytes(limit)}까지 첨부할 수 있습니다.`);
        item.state = "uploading";
        item.progress = 0;
        setUploadProgress(item.tile, item);
        const response = await sendUpload(file, { "Content-Type": "application/octet-stream", "X-CSRF-Token": state.csrf,
          "X-Chat-Source": encodeURIComponent(JSON.stringify(entry.source)),
          "X-File-Name": encodeURIComponent(file.name || `clipboard-${Date.now()}.${file.type?.split("/")[1]?.replace("jpeg", "jpg") || "bin"}`) },
        (ratio) => {
          item.progress = ratio;
          setUploadProgress(item.tile, item);
        });
        assertAuthEpoch(authEpoch);
        if (response.status === 401) { showLogin(); return; }
        const data = response.data || {};
        if (!response.ok || !data.attachment?.id) throw new Error(data.error || "첨부파일을 올리지 못했습니다.");
        if (!state.authenticated || state.chats.get(chatKey(entry.source)) !== entry) return;
        // The local preview now belongs to the uploaded attachment.
        const thumb = entry.thumbs.get(item.key);
        if (thumb) {
          entry.thumbs.delete(item.key);
          entry.thumbs.set(data.attachment.id, thumb);
        }
        unqueue(item);
        entry.attachments.push(data.attachment);
        entry.revision++;
      } catch (err) {
        if (authEpoch !== state.authEpoch || err.staleAuth) { entry.uploads = 0; return; }
        unqueue(item);
        entry.error = entry.inputError = err.message;
        if (networkOffline()) {
          entry.notice = `아직 올리지 못한 파일 ${entry.uploads}개가 있습니다. 다시 첨부해 주세요.`;
          entry.error = entry.inputError = `${err.message} ${entry.notice}`;
          break;
        }
      } finally { entry.uploads = Math.max(0, entry.uploads - 1); updateChatComposer(entry); }
    }
  } finally {
    entry.uploadQueue = entry.uploadQueue.filter((item) => !queue.includes(item));
  }
  entry.uploads = 0;
  updateChatComposer(entry);
}
function insertChatText(entry, input, text) {
  if (!text || !currentChat(entry) || input.disabled || input.readOnly) return;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  input.setSelectionRange?.(start + text.length, start + text.length);
  entry.text = input.value;
  entry.inputCollapsed = false;
  entry.revision++;
  updateChatComposer(entry);
}
function pasteChat(entry, event, input) {
  const clipboard = event.clipboardData;
  const files = [...(clipboard?.files || [])];
  if (!files.length) {
    for (const item of clipboard?.items || []) {
      if (item.kind === "file") {
        const file = item.getAsFile?.();
        if (file) files.push(file);
      }
    }
  }
  if (!files.length) return; // Native text paste retains browser selection and IME behavior.
  event.preventDefault();
  if (!currentChat(entry)) return;
  if (entry.composing) {
    entry.notice = "한글 입력을 마친 뒤 사진·파일을 다시 붙여넣어 주세요.";
    updateChatComposer(entry);
    return;
  }
  // Text-only paste stays native. For mixed file/text pastes, preserve the text
  // even when the file cannot be uploaded yet, and explain the skipped file.
  insertChatText(entry, input, clipboard.getData("text/plain"));
  const blocked = networkOffline() ? "오프라인이라 파일을 추가하지 못했습니다. 연결 후 다시 붙여넣어 주세요."
    : entry.uploads ? "첨부를 올리고 있습니다. 완료된 뒤 파일을 다시 붙여넣어 주세요."
    : entry.sending || entry.pending ? "전송 결과를 확인한 뒤 파일을 다시 붙여넣어 주세요."
    : !chatAttachmentsSupported(entry) ? "이 세션은 파일 첨부를 지원하지 않습니다. 터미널에서 파일을 확인해 주세요."
    : "";
  if (blocked) {
    entry.notice = blocked;
    updateChatComposer(entry);
    return;
  }
  uploadChatFiles(entry, files);
}
async function readChatClipboard(entry, input, files, editorEpoch = entry.editorEpoch) {
  const editorCurrent = () => currentChat(entry) && entry.editorEpoch === editorEpoch;
  if (networkOffline()) { updateChatComposer(entry); return; }
  if (!editorCurrent() || !chatAttachmentsSupported(entry) || entry.sending || entry.pending || entry.uploads) return;
  if (entry.composing) {
    entry.notice = "한글 입력을 마친 뒤 붙여넣기를 눌러 주세요.";
    updateChatComposer(entry);
    return;
  }
  const revision = entry.revision;
  const stateEpoch = entry.stateEpoch;
  const inputValue = input.value;
  const selection = [input.selectionStart, input.selectionEnd];
  const canApply = () => {
    if (!editorCurrent()) return false;
    if (networkOffline() || entry.composing || entry.revision !== revision ||
        entry.stateEpoch !== stateEpoch || input.value !== inputValue ||
        input.selectionStart !== selection[0] || input.selectionEnd !== selection[1]) {
      entry.notice = "입력 중 내용이나 선택 위치가 바뀌어 클립보드를 붙이지 않았습니다. 입력을 마친 뒤 다시 눌러 주세요.";
      updateChatComposer(entry);
      return false;
    }
    return true;
  };
  let clipboardError;
  const richReadSupported = typeof navigator.clipboard?.read === "function";
  try {
    if (!richReadSupported) throw new Error("clipboard unavailable");
    const items = await navigator.clipboard.read();
    const attachments = [];
    const texts = [];
    for (const item of items) {
      if (item.types.includes("text/plain")) texts.push(await (await item.getType("text/plain")).text());
      const image = item.types.find((type) => type.startsWith("image/"));
      if (image) {
        const blob = await item.getType(image);
        attachments.push(new File([blob], `clipboard-${Date.now()}-${attachments.length}.${image.split("/")[1].replace("jpeg", "jpg")}`, { type: image }));
      }
    }
    if (!canApply()) return;
    if (!texts.length && !attachments.length) throw new Error("clipboard empty");
    entry.notice = "";
    if (texts.length && input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
    } else insertChatText(entry, input, texts.join("\n"));
    await uploadChatFiles(entry, attachments);
    if (texts.length && input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
    }
    return;
  } catch (err) { clipboardError = err; }
  if (!canApply()) return;
  // Some mobile browsers expose text reads while rich image reads are unavailable.
  // A single bounded fallback preserves the same editor/source and never opens a
  // delayed file picker after transient browser user activation has expired.
  try {
    if (typeof navigator.clipboard?.readText !== "function") throw new Error("clipboard unavailable");
    const text = await navigator.clipboard.readText();
    if (!canApply()) return;
    if (!text) throw new Error("clipboard empty");
    if (input.disabled) {
      entry.notice = "이 세션의 텍스트 입력은 터미널에서 처리해 주세요. 사진·파일은 첨부한 뒤 경로를 복사할 수 있습니다.";
      updateChatComposer(entry);
      return;
    }
    insertChatText(entry, input, text);
    entry.notice = "클립보드 텍스트를 붙여넣었습니다. 사진은 첨부에서 선택할 수 있습니다.";
    updateChatComposer(entry);
    return;
  } catch (err) {
    if (["NotAllowedError", "SecurityError"].includes(err.name)) clipboardError = err;
  }
  {
    if (!editorCurrent()) return;
    const permissionDenied = ["NotAllowedError", "SecurityError"].includes(clipboardError?.name);
    entry.notice = `${permissionDenied ? "브라우저가 클립보드 읽기를 허용하지 않았습니다." : !richReadSupported ? "이 브라우저는 클립보드 버튼 붙여넣기를 지원하지 않습니다." : "이 클립보드 내용을 버튼으로 읽을 수 없습니다."} 입력칸을 길게 눌러 붙여넣거나 첨부에서 사진·파일을 선택해 주세요.`;
    updateChatComposer(entry);
  }
}
function openChatTerminal(s) {
  if (state.selected !== s.key || state.launchBusy) return;
  if (state.terminalAlive && chatKey(terminalInfo(state.terminal).nativeSource || {}) === chatKey(sourceOf(s))) {
    openTerminal(state.terminal);
    return;
  }
  if (s.nativeSource) { toast("이 실행의 터미널이 종료됐습니다. 새로고침 후 실제 작업 목록에서 선택해 주세요."); return; }
  state.route = { host: s.host, agent: s.agent, account: accountOf(s) };
  populateRoute(s);
  preparePlan();
}
function openNativeChat(terminal) {
  const info = terminalInfo(terminal);
  const source = info.nativeSource;
  if (!source?.host || !source.agent || !source.id || !state.authenticated) return;
  const key = [source.host, source.agent, source.home || "", source.id].join("|");
  // This identity comes only from the server-owned launched terminal metadata.
  // The user explicitly selects it; the original source never changes underneath a draft.
  if (!allSessions().some((s) => s.key === key)) {
    state.nativeSessions.set(key, { ...source, key, nativeSource: true,
      title: info.title || "이어가는 작업", hostLabel: hostLabel(source.host),
      account: sessionAccountLabel({ ...source, account: info.account }),
      phase: "idle", project: "이어가는 실제 세션", updatedAt: Date.now() / 1000 });
  }
  if (state.terminalVisible) returnToBoard({ selectLaunchedSource: false });
  select(key);
}
// --- Tool records: consecutive tool rows fold into one line -----------------
// "도구 실행 7개 · 명령 4 · 파일 수정 2" opens to one line per record, and a
// record opens once more to its full text. Groups and records are reused
// across reads, so what is open stays open while a turn adds records.
const TOOL_KIND_LABELS = { command: "명령", edit: "파일 수정", read: "파일 읽기", search: "검색",
  mcp: "MCP", agent: "에이전트", other: "기타" };
const CLAUDE_TOOL_KINDS = { Bash: "command", BashOutput: "command", KillShell: "command", KillBash: "command",
  Edit: "edit", MultiEdit: "edit", Write: "edit", NotebookEdit: "edit", Read: "read", NotebookRead: "read",
  Grep: "search", Glob: "search", LS: "search", WebSearch: "search", WebFetch: "search", ToolSearch: "search",
  Task: "agent", Agent: "agent" };
// The collector names each record's kind (toolKind); a read without it is
// told apart by the text the collector writes ("$ …", "파일 수정 · …", "Bash · …").
function toolKindOf(message) {
  if (Object.prototype.hasOwnProperty.call(TOOL_KIND_LABELS, message?.toolKind)) return message.toolKind;
  const text = String(message?.text || "");
  if (text.startsWith("$ ")) return "command";
  if (text.startsWith("파일 수정")) return "edit";
  if (text.startsWith("웹 검색")) return "search";
  const name = text.split(" · ")[0].trim();
  if (name.startsWith("mcp__")) return "mcp";
  return CLAUDE_TOOL_KINDS[name] || "other";
}
function toolItemNode(message) {
  const node = button("", "tool-item", () => {
    node.full = !node.full;
    setClass(node, "is-full", node.full);
    node.setAttribute("aria-expanded", String(node.full));
  });
  node.setAttribute("aria-expanded", "false");
  node.append(el("span", "tool-item-text", String(message.text || "")));
  return node;
}
function toolGroupTitle(items) {
  return `도구 실행 ${items.length}개`;
}
function toolGroupKinds(items) {
  const counts = new Map();
  for (const item of items) counts.set(item.kind, (counts.get(item.kind) || 0) + 1);
  const order = Object.keys(TOOL_KIND_LABELS);
  return [...counts].sort((a, b) => b[1] - a[1] || order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([kind, count]) => `${TOOL_KIND_LABELS[kind] || TOOL_KIND_LABELS.other} ${count}`).join(" · ");
}
function setToolGroupOpen(row, open) {
  row.open = !!open;
  setClass(row, "is-open", row.open);
  row.toolToggle.setAttribute("aria-expanded", String(row.open));
  row.toolList.hidden = !row.open;
}
function toolGroupNode() {
  const row = el("article", "msg tool tool-group");
  const toggle = button("", "tool-group-toggle", () => setToolGroupOpen(row, !row.open));
  const icon = el("span", "tool-group-icon");
  icon.setAttribute("aria-hidden", "true");
  const chevron = el("span", "tool-group-chevron");
  chevron.setAttribute("aria-hidden", "true");
  row.toolTitle = el("span", "tool-group-title");
  row.toolKinds = el("span", "tool-group-kinds");
  toggle.append(icon, row.toolTitle, row.toolKinds, chevron);
  row.toolToggle = toggle;
  row.toolList = el("div", "tool-group-list");
  row.append(toggle, row.toolList);
  setToolGroupOpen(row, false);
  // The long-press menu copies every record of the group.
  row.messageItem = { key: "", message: { role: "tool", text: "", ts: null }, row, role: "tool", truncated: false,
    group: true, items: [] };
  const more = button("", "msg-more", () => openMessageMenu(row));
  more.setAttribute("aria-label", "도구 실행 기록 메뉴");
  more.setAttribute("aria-haspopup", "dialog");
  more.title = "도구 실행 기록 메뉴";
  row.moreButton = more;
  row.append(more);
  return row;
}
function updateToolGroup(row, items) {
  const title = toolGroupTitle(items);
  const kinds = toolGroupKinds(items);
  if (row.toolTitle.textContent !== title) row.toolTitle.textContent = title;
  if (row.toolKinds.textContent !== `· ${kinds}`) row.toolKinds.textContent = `· ${kinds}`;
  row.toolToggle.setAttribute("aria-label", `${title}, ${kinds}`);
  // Records already shown stay mounted, so their own open state survives.
  const nodes = items.map((item) => item.row);
  const box = row.toolList;
  for (const child of [...box.children]) if (!nodes.includes(child)) box.removeChild(child);
  nodes.forEach((node, index) => {
    if (box.children[index] !== node) box.insertBefore(node, box.children[index] || null);
  });
  Object.assign(row.messageItem, { key: items[0].key, items, message: { role: "tool",
    text: items.map((item) => item.message.text || "").join("\n"), ts: items[0].message.ts ?? null } });
  // Every record remembers its group, for when the group's first record leaves.
  for (const item of items) item.inGroup = row;
}
// The conversation's rows with each run of tool records as one group. A group
// belongs to its first record (the same object while later records join it).
// When the server's 80-row window moves past that record, the group of a record
// still shown carries on, opened or not; one group is never drawn twice.
function toolGroupRows(list, rows) {
  const desired = [];
  const used = new Set();
  for (let index = 0; index < rows.length;) {
    if (rows[index].role !== "tool") { desired.push(rows[index++].row); continue; }
    const items = [];
    while (index < rows.length && rows[index].role === "tool") items.push(rows[index++]);
    if (!items[0].toolGroup || used.has(items[0].toolGroup)) {
      const earlier = items.map((item) => item.inGroup).filter(Boolean);
      let node = earlier.find((group) => !used.has(group));
      if (!node) {
        node = toolGroupNode();
        if (earlier.some((group) => group.open)) setToolGroupOpen(node, true);
      }
      items[0].toolGroup = node;
    }
    used.add(items[0].toolGroup);
    updateToolGroup(items[0].toolGroup, items);
    desired.push(items[0].toolGroup);
  }
  return desired;
}
// What a user bubble shows: the board's own "첨부 파일: <경로>" lines (added
// when it sends files) become chips named by the file, and "[이미지 첨부]"
// alone is left out when chips say it. No attachment path is drawn.
const ATTACHMENT_PATH_LINE = /^첨부 파일: (.+)$/;
const IMAGE_NAME = /\.(png|jpe?g|gif|webp|heic|heif|bmp|svg)$/i;
function userMessageParts(message) {
  const text = String(message?.text || "");
  const chips = attachmentMetadata(message?.attachments);
  const lines = text.split("\n");
  const names = [];
  while (lines.length) {
    const match = lines[lines.length - 1].trim().match(ATTACHMENT_PATH_LINE);
    if (!match) break;
    let path = match[1].trim();
    if (path.startsWith('"')) {
      try { path = String(JSON.parse(path)); } catch { path = path.replace(/^"|"$/g, ""); }
    }
    // Only the board's attachment store; a typed "첨부 파일: …" line stays text.
    if (!path.includes(`/${INSTANCE.attachmentsDir}/`)) break;
    names.unshift(path.slice(path.lastIndexOf("/") + 1) || "첨부파일");
    lines.pop();
  }
  for (const name of names) {
    if (chips.some((file) => file.name === name)) continue;
    const kind = IMAGE_NAME.test(name) ? "image" : "file";
    const chip = { id: null, name, type: "", size: null, kind, source: "sessionholic" };
    // A transcript chip that could not name its file takes the name from the path.
    const unnamed = chips.findIndex((file) => file.source === "native" && file.kind === kind &&
      GENERIC_ATTACHMENT_NAMES.has(String(file.name).trim().toLowerCase()));
    if (unnamed >= 0) chips[unnamed] = chip;
    else chips.push(chip);
  }
  let body = names.length ? lines.join("\n").replace(/\s+$/, "") : text;
  // Claude Code's "[Image #1]" stands for an image the chips already show.
  if (chips.some((file) => file.kind === "image")) {
    const words = body.replace(CLAUDE_IMAGE_TOKEN, " ");
    if (words !== body) body = words.trim();
  }
  if (chips.length && OUTBOX_ATTACHMENT_ONLY.has(body.trim())) body = "";
  return { body, attachments: chips };
}
function renderMessages(s, messages, error = "", fresh = false) {
  const list = $("messages");
  if (!list) return;
  const source = chatKey(sourceOf(s));
  const entry = state.chats.get(source) || null;
  const previous = list.conversationSource === source ? list.conversationRows || [] : [];
  // A failed refresh never removes a conversation that was already readable.
  if (messages == null || (error && !messages.length && previous.length))
    messages = previous.map((item) => item.message);
  const selectedText = window.getSelection?.();
  const readingSelection = selectedText && !selectedText.isCollapsed &&
    (list.contains?.(selectedText.anchorNode) || list.contains?.(selectedText.focusNode));
  const stick = !readingSelection &&
    (fresh || list.scrollHeight - list.scrollTop - list.clientHeight < 50);
  const scroll = list.scrollTop;
  const previousKeys = new Set(previous.map((item) => item.key));
  const oldWarningHeight = list.conversationError && [...list.children].includes(list.conversationError)
    ? list.conversationError.offsetHeight || 0 : 0;
  const keys = messages.map((message) => JSON.stringify([message.role, message.text, message.ts || null,
    attachmentMetadata(message.attachments), message.truncated === true]));
  // The server's 80-row window drops whole rows at its start: the rows on
  // screen are first lined up with the new ones where they overlap most, so
  // two rows with the same key (the same first command of two Codex turns,
  // which have no time) each keep their own row and tool group.
  let overlap = 0;
  let shift = 0;
  for (let start = 0; start < previous.length && previous.length - start > overlap; start++) {
    let length = 0;
    while (start + length < previous.length && length < keys.length && previous[start + length].key === keys[length]) length++;
    if (length > overlap) { overlap = length; shift = start; }
  }
  const lined = new Set(previous.slice(shift, shift + overlap));
  const available = new Map();
  for (const item of previous) {
    if (lined.has(item)) continue;
    if (!available.has(item.key)) available.set(item.key, []);
    available.get(item.key).push(item);
  }
  const rows = messages.map((message, index) => {
    const key = keys[index];
    const retained = index < overlap ? previous[shift + index] : available.get(key)?.shift();
    if (retained) return retained;
    const role = ["user", "assistant", "tool", "system"].includes(message.role) ? message.role : "system";
    // A tool record is one line of its group (toolGroupNode), not a row of its own.
    if (role === "tool") return { key, message, row: toolItemNode(message), role, truncated: false, kind: toolKindOf(message) };
    const row = el("article", `msg ${role}`);
    if (role !== "system") {
      const label = el("div", "msg-label");
      label.append(el("strong", null, role === "user" ? "나" : AGENTS[s.agent] || "에이전트"));
      if (message.ts) label.append(el("span", null, clock(message.ts)));
      row.append(label);
      row.msgLabel = label;
    }
    const bubble = el("div", "bubble");
    if (role === "assistant" && typeof MessageFormat !== "undefined") {
      MessageFormat.render(bubble, message.text || "", {
        onCopy: (text) => copyText(text, "코드를 복사했습니다."),
      });
    } else if (role === "user") {
      // The board's own "첨부 파일: <경로>" lines become chips, never paths.
      const shown = userMessageParts(message);
      if (shown.attachments.length) {
        if (shown.body) bubble.append(el("span", "msg-body", shown.body));
        bubble.append(attachmentGroup(shown.attachments, `첨부 ${shown.attachments.length}개`, { showCaption: false }));
      } else bubble.textContent = shown.body;
    } else bubble.textContent = message.text || "";
    row.append(bubble);
    // The server keeps at most 4,000 characters per message; say so instead of
    // letting a partial copy pass for the whole answer.
    const truncated = message.truncated === true;
    if (truncated) {
      const total = Number.isInteger(message.fullLength) ? ` · 전체 ${message.fullLength.toLocaleString()}자` : "";
      row.append(el("p", "msg-truncated", `긴 메시지라 앞부분만 표시합니다${total}. 전체 내용은 터미널에서 확인해 주세요.`));
    }
    // Copy, code copy, selection and the terminal live in a menu: long press
    // on touch, right click or this button (shown on hover) with a mouse.
    const item = { key, message, row, role, truncated };
    row.messageItem = item;
    if (role !== "system") {
      const more = button("", "msg-more", () => openMessageMenu(row));
      more.setAttribute("aria-label", "메시지 메뉴");
      more.setAttribute("aria-haspopup", "dialog");
      more.title = "메시지 메뉴";
      row.moreButton = more;
      row.append(more);
    }
    return item;
  });
  groupMessageRows(rows);
  const desired = toolGroupRows(list, rows);
  // This board's own sends that the transcript does not show yet, then the
  // working indicator, always after the transcript.
  let tail = [];
  let working = false;
  // Transcript messages that settled one of this board's bubbles in this render.
  const claimed = new Set();
  if (entry) {
    const live = entry.data && chatKey(entry.data.route || {}) === source ? entry.data : null;
    // A Claude row without an id is never read; its listed phase still says it works.
    const phaseWorking = live ? live.phase === "working" : claudeWithoutId(s) && s.phase === "working";
    // The composer's receipt note follows a bubble that went (renderMessages
    // also runs for /api/read, which no composer update follows).
    if (settleOutbox(entry, messages, { working: phaseWorking, claimed })) updateChatComposer(entry);
    tail = outboxRows(list, entry);
    // The phase stays "working" while a background agent still runs tools after
    // its answer, so the row shows only while the conversation waits on the
    // agent: it ends with a message of the user's (one sent from here and not
    // refused or ended included), with tool records, or nothing has been read yet.
    const lastRole = (entry.outbox || []).some(outboxAwaitsAgent) ? "user" : rows.at(-1)?.role;
    working = phaseWorking && (!lastRole || lastRole === "user" || lastRole === "tool");
    // Only an exact start (Codex's running turn) is counted; Claude's newest
    // prompt time is an approximation and shows no time.
    list.workingSince = live && live.workingSinceApprox !== true ? live.workingSince ?? null : null;
    if (working) tail.push(workingRow(list));
  }
  if (error) {
    if (!list.conversationError || list.conversationErrorText !== error || list.conversationSource !== source) {
      const warning = el("div", "conversation-read-error");
      warning.setAttribute("role", "status");
      warning.append(el("p", "detail-error", error), button("대화 다시 불러오기", "secondary", () => {
        const current = selectedSession();
        if (current?.key === s.key) loadDetail(current, !list.conversationRows?.length);
      }));
      list.conversationError = warning;
      list.conversationErrorText = error;
    }
    desired.unshift(list.conversationError);
  } else {
    list.conversationError = null;
    list.conversationErrorText = "";
    if (claudeWithoutId(s) && !s.sessionId) {
      // One explanation instead of a read error here and another in the composer.
      list.conversationUnreadable ||= el("p", "group-empty conversation-unreadable", CLAUDE_UNREADABLE);
      desired.unshift(list.conversationUnreadable);
    } else if (!rows.length && !tail.length) {
      list.conversationEmpty ||= el("p", "group-empty", "저장된 대화를 아직 찾지 못했습니다.");
      desired.push(list.conversationEmpty);
    }
  }
  desired.push(...tail);
  // While reading back, the first row on screen that stays keeps its place:
  // rows dropped above it (the server's 80-row window moving during a turn) or
  // a grown tool group no longer push the text being read up.
  const keep = new Set(desired);
  let anchor = null;
  let anchorOffset = 0;
  if (!stick) {
    for (const child of [...list.children]) {
      if (!keep.has(child)) continue;
      const box = rowBox(list, child);
      if (!box) break;
      if (box.top + box.height > list.scrollTop) {
        anchor = child;
        anchorOffset = box.top - list.scrollTop;
        break;
      }
    }
  }
  // Keep unchanged text nodes mounted: mobile selections and expanded tool
  // records survive reads, including new messages appended during a turn.
  for (const child of [...list.children]) {
    if (!keep.has(child)) list.removeChild(child);
  }
  for (let index = 0; index < desired.length; index++) {
    if (list.children[index] !== desired[index])
      list.insertBefore(desired[index], list.children[index] || null);
  }
  list.conversationSource = source;
  list.conversationRows = rows;
  const warningHeight = error ? list.conversationError.offsetHeight || 0 : 0;
  const anchorBox = anchor && !stick ? rowBox(list, anchor) : null;
  if (stick) setMessagesScroll(list, list.scrollHeight);
  else if (anchorBox) setMessagesScroll(list, anchorBox.top - anchorOffset);
  else setMessagesScroll(list, scroll + warningHeight - oldWarningHeight);
  if (working) tickWorking();
  else stopWorkingClock();
  // While reading back, count what arrived below instead of jumping to it. A
  // row that only got its time (Codex's final answer takes its turn's end) is
  // not new, nor this board's own message that took the place of its bubble.
  if (!stick && previous.length) {
    const before = new Set(previous);
    const loose = (item) => JSON.stringify([item.message.role, item.message.text, attachmentMetadata(item.message.attachments)]);
    const gone = new Map();
    for (const item of previous) {
      if (rows.includes(item)) continue;
      gone.set(loose(item), (gone.get(loose(item)) || 0) + 1);
    }
    let added = 0;
    for (const item of rows) {
      if (before.has(item) || previousKeys.has(item.key)) continue;
      const replaced = gone.get(loose(item)) || 0;
      if (replaced) { gone.set(loose(item), replaced - 1); continue; }
      if (item.message.role !== "tool" && !claimed.has(item.message)) added++;
    }
    if (added) list.unread = (list.unread || 0) + added;
  } else if (stick) list.unread = 0;
  renderJump();
}

// A row's top and height in the conversation's own scrolled content, or null
// without layout. .messages is not positioned, so a row's offsetTop counts from
// an ancestor above it; the distance between the two boxes on screen (plus the
// list's scroll) does not. Without boxes (tests), offsetTop.
function rowBox(list, row) {
  const outer = list.getBoundingClientRect?.();
  const inner = row.getBoundingClientRect?.();
  if (Number.isFinite(outer?.top) && Number.isFinite(inner?.top) && Number.isFinite(inner?.height))
    return { top: inner.top - outer.top - (list.clientTop || 0) + list.scrollTop, height: inner.height };
  if (!Number.isFinite(row.offsetTop) || !Number.isFinite(row.offsetHeight)) return null;
  return { top: row.offsetTop, height: row.offsetHeight };
}

// Consecutive messages of one speaker form a group: only the first keeps the
// name and time, and the gap between them narrows (style.css: .is-cont).
// Rows are reused across reads, so this runs on every render.
const GROUP_GAP_SECONDS = 300;
function sameSpeaker(a, b) {
  if (!a || !b || a.role !== b.role || !["user", "assistant"].includes(a.role)) return false;
  const first = a.message?.ts;
  const next = b.message?.ts;
  // A timed row after untimed ones (Codex's final answer after its progress
  // notes, which have no time of their own) shows its name and time again.
  if (!Number.isFinite(first) && Number.isFinite(next)) return false;
  return !(Number.isFinite(first) && Number.isFinite(next) && next - first > GROUP_GAP_SECONDS);
}
function groupMessageRows(rows) {
  rows.forEach((item, index) => {
    const continued = sameSpeaker(rows[index - 1], item);
    setClass(item.row, "is-cont", continued);
    setClass(item.row, "has-next", sameSpeaker(item, rows[index + 1]));
    if (item.row.msgLabel) item.row.msgLabel.hidden = continued;
  });
}
async function copyText(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast("클립보드에 접근할 수 없습니다. 메뉴의 ‘텍스트 선택’으로 골라 복사해 주세요.");
  }
}
function messagePlainText(item) {
  const text = item?.message?.text || "";
  if (item?.role === "user") return userMessageParts(item.message).body;
  return item?.role === "assistant" && typeof MessageFormat !== "undefined" ? MessageFormat.plainText(text) : text;
}
function messageActions(s, item) {
  const actions = [];
  const text = messagePlainText(item);
  if (item.group && text.trim()) {
    actions.push({ label: `도구 기록 ${item.items.length}개 모두 복사`, run: () => copyText(text, "도구 실행 기록을 복사했습니다.") });
  } else if (text.trim()) {
    actions.push({ label: item.truncated ? "표시된 부분 복사" : "복사", run: () => copyText(text, item.truncated
      ? "표시된 부분만 복사했습니다. 전체 내용은 터미널에서 확인해 주세요." : "메시지 본문을 복사했습니다.") });
  }
  const code = item.role === "assistant" && typeof MessageFormat?.codeBlocks === "function"
    ? MessageFormat.codeBlocks(item.message.text || "") : [];
  if (code.length) {
    actions.push({ label: code.length > 1 ? `코드 ${code.length}개 모두 복사` : "코드 복사",
      run: () => copyText(code.join("\n"), "코드를 복사했습니다.") });
  }
  if (text.trim() && !item.group) actions.push({ label: "텍스트 선택", run: () => selectMessageText(item.row) });
  if (chatEntry(s).data && !state.launchBusy && (!s.nativeSource || terminalForSession(s)))
    actions.push({ label: "터미널에서 보기", run: () => openChatTerminal(s) });
  return actions;
}
function openMessageMenu(row, at = null) {
  const item = row?.messageItem;
  const s = selectedSession();
  if (!item || !s || chatKey(sourceOf(s)) !== $("messages")?.conversationSource) return;
  const speaker = item.role === "user" ? "내 메시지" : item.group ? toolGroupTitle(item.items)
    : item.role === "tool" ? "도구 실행 기록" : `${AGENTS[s.agent] || "에이전트"}의 답변`;
  const preview = messagePlainText(item).replace(/\s+/g, " ").trim().slice(0, 140);
  openActionSheet({ eyebrow: item.message.ts ? clock(item.message.ts) : "", title: speaker, preview,
    actions: messageActions(s, item), at, trigger: row.moreButton || null });
}
// "텍스트 선택": the bubble becomes selectable (touch turns selection off so a
// long press opens the menu) and its text is selected for the handles.
function selectMessageText(row) {
  const bubble = row?.querySelector?.(".bubble");
  if (!bubble) return;
  setClass(row, "is-selecting", true);
  try {
    const range = document.createRange();
    range.selectNodeContents(bubble);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  } catch {
    /* the bubble stays selectable by hand */
  }
}
// Long press opens a message's menu; on phones a tap on the conversation shows
// or hides its bars. Both share the list's one set of pointer listeners.
function bindMessageGestures(list) {
  const rowOf = (target) => {
    const row = target?.closest?.(".msg");
    return row?.messageItem ? row : null;
  };
  let tap = null;
  bindPressMenu(list, rowOf, openMessageMenu, {
    onDown(event) {
      // A selection made from the menu ends when another message is touched.
      const row = rowOf(event.target);
      for (const item of list.conversationRows || [])
        if (item.row !== row && String(item.row.className || "").includes("is-selecting")) setClass(item.row, "is-selecting", false);
      // A finger on the list ends the board's own smooth scroll.
      list.autoScrollUntil = 0;
      // A touch that stops a moving list only stops it.
      const settled = Date.now() - (list.lastScrollAt || 0) >= GESTURE.scrollSettleMs;
      tap = compactChrome() && event.pointerType !== "mouse" && event.isPrimary !== false && settled
        ? { id: event.pointerId, x: event.clientX, y: event.clientY, at: Date.now(), target: event.target } : null;
    },
    onMove(event) {
      if (tap && event.pointerId === tap.id &&
          Math.hypot((event.clientX || 0) - tap.x, (event.clientY || 0) - tap.y) > GESTURE.tapSlopPx) tap = null;
      return false;
    },
    onUp(event, cancelled, fired) {
      const down = tap;
      tap = null;
      // While typing, the input's folded layout stays; a scroll ends in pointercancel.
      if (cancelled || chatTyping() || !isChromeTap(down, event, { fired })) return;
      toggleChrome();
    },
  });
}
function updateReadNotice(data) {
  const pane = $("detail");
  let note = pane.querySelector(".stale-notice");
  if (!data.stale) return;
  if (!note) {
    note = el("div", "stale-notice");
    pane.querySelector(".detail-head").after(note);
  }
  note.textContent = `기기에 연결되지 않아 ${data.fetchedAt ? `${ago(data.fetchedAt)} 저장한` : "저장된"} 대화를 표시합니다. 실행 상태는 연결 후 확인해 주세요.`;
}

function navigateView(view, replace = false) {
  const current = window.history.state;
  if (!replace && current?.[INSTANCE.historyKey] && current.view === view) return;
  window.history[replace ? "replaceState" : "pushState"](
    { [INSTANCE.historyKey]: true, view },
    "",
  );
}
function backToList() {
  stopChatPolling();
  if (
    window.history.state?.[INSTANCE.historyKey] &&
    window.history.state.view === "detail"
  )
    window.history.back();
  else setDetailShown(false, { direction: "pop", focus: true });
}
function backFromTerminal() {
  if (
    window.history.state?.[INSTANCE.historyKey] &&
    window.history.state.view === "terminal"
  )
    window.history.back();
  else returnToBoard();
}

function capsHost(name) {
  return state.capabilities?.hosts?.find((h) => h.name === name);
}
function capsAgent(host, agent) {
  return capsHost(host)?.agents?.find((a) => a.id === agent);
}
function accountDisplay(profile, fallback) {
  if (profile?.label) return profile.label;
  if (profile?.scopeLabel && profile?.accountLabel) return `${profile.scopeLabel} · ${profile.accountLabel}`;
  if (profile?.accountLabel) return profile.accountLabel;
  if (typeof fallback === "string" && fallback && !/^(?:\.codex|codex:|\.claude)/.test(fallback)) return fallback;
  return "현재 계정 설정";
}
function sessionAccountLabel(s) {
  const profile = capsAgent(s.host, s.agent)?.accounts?.find((account) => account.id === accountOf(s));
  return accountDisplay(profile, s.account);
}
function accountRouteKey(s) {
  return JSON.stringify([s.host, s.agent, accountOf(s)]);
}
function targetLabel(route) {
  if (!route) return "";
  const host = capsHost(route.host);
  const agent = capsAgent(route.host, route.agent);
  const account = agent?.accounts?.find((a) => a.id === route.account);
  return [
    host?.label || route.host,
    agent?.label || AGENTS[route.agent] || route.agent,
    accountDisplay(account, route.account),
  ]
    .filter(Boolean)
    .join(" · ");
}
function hostLabel(name) {
  return capsHost(name)?.label || name || "대상 기기";
}
function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes}B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const next of units) {
    value /= 1024;
    unit = next;
    if (value < 1024 || next === "TB") break;
  }
  return `${value.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}${unit}`;
}
function buildContinuePanel(s) {
  const disclosure = el("details", "continue-disclosure");
  disclosure.append(el("summary", null, "다른 기기·계정에서 이어가기"));
  const panel = el("section", "continue-panel");
  panel.setAttribute("aria-label", "이어서 사용할 환경 선택");
  const heading = el("div", "continue-heading");
  heading.append(
    el("h2", null, "어디서 이어갈까요?"),
    el("span", null, "기기와 실행 환경을 변경합니다"),
  );
  const form = el("div", "route-form");
  for (const [id, label] of [
    ["route-host", "기기"],
    ["route-agent", "에이전트"],
    ["route-account", "계정 설정"],
  ]) {
    const field = el("label", "route-field");
    field.append(el("span", null, label));
    const select = el("select");
    select.id = id;
    field.append(select);
    form.append(field);
  }
  const actions = el("div", "continue-actions");
  const note = el("p", "continue-note");
  note.id = "continue-note";
  const action = button("터미널 열기 ↗", "primary", () => preparePlan());
  action.id = "continue-open";
  actions.append(note, action);
  panel.append(heading, form, actions);
  const accountContext = el("p", "account-context", "실행 환경과 프로필은 설치한 서버의 설정을 따릅니다. 같은 이름도 로그인은 별도로 관리됩니다.");
  accountContext.id = "account-context";
  panel.append(accountContext);
  if (s.resume) {
    const fallback = el("details", "resume-fallback");
    fallback.append(el("summary", null, "기존 터미널에서 직접 열기"));
    const row = el("div");
    row.append(
      el("code", null, s.resume),
      button("명령 복사", "copy", async () => {
        try {
          await navigator.clipboard.writeText(s.resume);
          toast("재개 명령을 복사했습니다.");
        } catch {
          toast("클립보드에 접근할 수 없습니다.");
        }
      }),
    );
    fallback.append(row);
    panel.append(fallback);
  }
  queueMicrotask(() => populateRoute(s));
  disclosure.append(panel);
  return disclosure;
}
function populateRoute(s) {
  if (!$("route-host") || state.selected !== s.key) return;
  const hosts = state.capabilities?.hosts || [];
  const preserve = !!state.route;
  state.route = state.route || {
    host: s.host,
    agent: s.agent,
    account: accountOf(s),
  };
  const hostSelect = $("route-host");
  hostSelect.replaceChildren();
  for (const host of hosts) {
    const option = new Option(
      `${host.label}${host.online ? "" : " · 연결 확인 필요"}`,
      host.name,
    );
    hostSelect.append(option);
  }
  if (!hosts.some((h) => h.name === state.route.host)) {
    hostSelect.append(
      new Option(s.hostLabel || state.route.host, state.route.host),
    );
  }
  hostSelect.value = state.route.host;
  hostSelect.onchange = () => {
    state.route = {
      host: hostSelect.value,
      agent: state.route.agent,
      account: state.route.account,
    };
    populateAgent(s, false);
  };
  populateAgent(s, preserve);
}
function populateAgent(s, preserve = false) {
  const host = capsHost(state.route.host);
  const agents = host?.agents || [];
  const select = $("route-agent");
  select.replaceChildren();
  for (const agent of agents)
    select.append(
      new Option(agent.label || AGENTS[agent.id] || agent.id, agent.id),
    );
  if (!agents.some((a) => a.id === state.route.agent) && !preserve)
    state.route.agent = agents[0]?.id || s.agent;
  if (!agents.some((a) => a.id === state.route.agent))
    select.append(
      new Option(
        `${AGENTS[state.route.agent] || state.route.agent}${preserve ? " · 확인 필요" : ""}`,
        state.route.agent,
      ),
    );
  select.value = state.route.agent;
  select.onchange = () => {
    state.route.agent = select.value;
    state.route.account = "";
    populateAccount(s);
  };
  populateAccount(s, preserve);
}
function populateAccount(s, preserve = false) {
  const accounts =
    capsAgent(state.route.host, state.route.agent)?.accounts || [];
  const select = $("route-account");
  select.replaceChildren();
  const crossHost = state.route.host !== s.host;
  const current = accounts.find(
    (a) => a.id === state.route.account && (preserve || a.available !== false),
  );
  const sourceAccount = accounts.find(
    (a) => a.id === accountOf(s) && a.available !== false,
  );
  const retainedMissing = preserve && !!state.route.account && !current;
  const match = current || (!retainedMissing ? sourceAccount : null);
  const needsChoice = crossHost && !match && !retainedMissing && accounts.length > 0;
  if (match) state.route.account = match.id;
  if (needsChoice)
    select.append(new Option("대상 계정을 선택하세요", ""));
  const groups = new Map();
  const grouped = accounts.some((account) => account.scope || account.scopeLabel);
  for (const account of accounts) {
    const actualSource = state.route.host === s.host && state.route.agent === s.agent && account.id === accountOf(s);
    const option = new Option(
      `${accountDisplay(account)}${actualSource ? " · 현재 사용 중" : ""}${account.available === false ? " · 사용 불가" : ""}`,
      account.id,
    );
    option.disabled = account.available === false;
    if (!grouped) select.append(option);
    else {
      const scope = account.scope || account.scopeLabel || "other";
      if (!groups.has(scope)) {
        const group = el("optgroup");
        group.label = account.scopeLabel || "다른 실행 환경";
        groups.set(scope, group);
        select.append(group);
      }
      groups.get(scope).append(option);
    }
  }
  if (retainedMissing)
    select.append(new Option(`${state.route.account} · 확인 필요`, state.route.account));
  if (!match && !retainedMissing)
    state.route.account = needsChoice
      ? ""
      : accounts.find((a) => a.available !== false)?.id || "";
  if (!accounts.length && !retainedMissing) select.append(new Option("사용할 계정 없음", ""));
  select.value = state.route.account;
  select.onchange = () => {
    state.route.account = select.value;
    updateContinue(s);
  };
  updateContinue(s);
}
function updateContinue(s) {
  const crossHost = state.route.host !== s.host;
  const same =
    state.route.host === s.host &&
    state.route.agent === s.agent &&
    state.route.account === accountOf(s);
  const account = capsAgent(
    state.route.host,
    state.route.agent,
  )?.accounts?.find((a) => a.id === state.route.account);
  const accountContext = $("account-context");
  if (accountContext) accountContext.textContent = `${account?.scopeDescription || "실행 환경과 프로필은 설치한 서버의 설정을 따릅니다."} 같은 이름도 로그인은 별도로 관리됩니다.`;
  const hasAvailableAccounts = !!capsAgent(
    state.route.host,
    state.route.agent,
  )?.accounts?.some((a) => a.available !== false);
  const terminal = state.capabilities?.terminal;
  const offline = capsHost(s.host)?.online === false || capsHost(state.route.host)?.online === false;
  const ready = !!(
    state.csrf &&
    state.route.account &&
    account && account.available !== false &&
    terminal?.available && !offline
  );
  $("continue-open").disabled = !ready || state.launchBusy;
  $("continue-open").textContent = crossHost
    ? "이 기기로 넘기기 ↗"
    : same
      ? "터미널 열기 ↗"
      : "이 설정으로 이어가기 ↗";
  $("continue-note").textContent = !state.capabilities
    ? "연결 환경을 확인하고 있습니다."
    : offline
      ? "출발·대상 기기 연결을 확인해 주세요. 열어 둔 터미널은 열린 목록에서 확인할 수 있습니다."
    : !terminal?.available
      ? terminal?.reason || "터미널 실행 환경이 아직 준비되지 않았습니다."
      : !state.route.account
        ? crossHost && hasAvailableAccounts
          ? "대상 기기의 계정을 직접 선택해 주세요."
          : "사용 가능한 계정이 없습니다."
        : !account
          ? "선택한 계정을 확인하지 못했습니다. 선택은 유지됩니다. 연결 후 다시 확인해 주세요."
        : account.available === false
          ? account.reason || "이 계정을 사용할 수 없습니다."
          : crossHost
            ? `실제 실행 위치가 ${hostLabel(state.route.host)}로 바뀝니다. 코드·미커밋 변경과 최근 대화가 새 작업 폴더로 옮겨집니다.`
            : same
              ? "현재 환경으로 연결합니다."
              : "작업 맥락을 전달할 수 있는지 먼저 확인합니다.";
}

async function preparePlan() {
  const s = selectedSession();
  if (!s || !state.route) return;
  if (state.launchBusy || $("plan-dialog").open) {
    $(state.launchBusy ? "plan-dialog" : "plan-cancel").focus();
    return;
  }
  const target = { ...state.route };
  const run = ++state.planRun;
  state.plan = null;
  $("plan-title").textContent = "이어서 할 준비를 확인합니다";
  $("plan-error").textContent = "";
  $("plan-body").replaceChildren(
    progress("대화와 실행 환경을 확인하고 있습니다…"),
  );
  $("plan-launch").disabled = true;
  $("plan-launch").textContent = "터미널 열기 ↗";
  $("plan-close").disabled = false;
  $("plan-cancel").disabled = false;
  sheet("plan-dialog").open();
  try {
    const plan = await api("/api/plan", {
      method: "POST",
      body: { source: sourceOf(s), target },
    });
    if (run !== state.planRun || !$("plan-dialog").open) return;
    state.plan = plan;
    renderPlan(s, plan, target);
    // Returning to the same session's own terminal needs no second confirmation.
    if (plan.allowed && ["attach", "reconnect"].includes(plan.mode) && !(plan.warnings || []).length)
      await launchPlan();
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth && run === state.planRun && $("plan-dialog").open) {
      $("plan-title").textContent = "준비를 완료하지 못했습니다";
      $("plan-body").replaceChildren();
      $("plan-error").textContent = err.message;
    }
  }
}
function progress(text) {
  const node = el("div", "plan-progress");
  node.append(el("span", "spinner"), el("span", null, text));
  return node;
}
function renderPlan(s, plan, target) {
  $("plan-title").textContent = plan.allowed
    ? plan.mode === "transfer"
      ? "새 작업 폴더로 옮길까요?"
      : plan.mode === "reconnect"
      ? "열어 둔 터미널로 돌아갑니다"
      : plan.mode === "attach"
        ? "이 작업으로 돌아갑니다"
        : "작업 맥락을 이어갑니다"
    : "지금은 전환할 수 없습니다";
  const body = $("plan-body");
  body.replaceChildren();
  const route = el("div", "plan-route");
  const transfer = plan.mode === "transfer" ? plan.transfer || {} : null;
  if (transfer) {
    const workspace = transfer.workspace || {};
    route.append(
      el("span", null, "출발 · 실제 작업 위치"),
      el(
        "strong",
        null,
        [transfer.sourceHostLabel || s.hostLabel,
          workspace.cwdRelative && workspace.cwdRelative !== "."
            ? workspace.cwdRelative
            : workspace.projectName || workspace.root]
          .filter(Boolean)
          .join(" · "),
      ),
      el("span", "plan-route-arrow", "↓"),
      el("span", null, "도착 · 새 작업 폴더"),
      el(
        "strong",
        null,
        targetLabel(target),
      ),
    );
    body.append(route);
    body.append(
      el(
        "p",
        "plan-summary",
        transfer.requiresInterrupt
          ? "진행 중인 응답을 중단하고, 코드·미커밋 변경·최근 대화를 새 작업 폴더로 옮깁니다. 원본 파일·기록은 남습니다."
          : "진행 중인 응답은 없습니다. 코드·미커밋 변경·최근 대화를 새 작업 폴더로 옮깁니다. 원본 파일·기록은 남습니다.",
      ),
    );
    const metrics = [];
    if (Number.isFinite(workspace.fileCount))
      metrics.push(`${workspace.fileCount.toLocaleString("ko-KR")}개 파일`);
    const size = formatBytes(workspace.bytes);
    if (size) metrics.push(size);
    const sourceDetails = [
      workspace.git === true ? "Git 작업 폴더" : "",
      typeof workspace.git === "string" && workspace.git
        ? `Git ${workspace.git}`
        : "",
      workspace.branch ? `브랜치 ${workspace.branch}` : "",
      workspace.head ? `기준 ${String(workspace.head).slice(0, 8)}` : "",
    ].filter(Boolean);
    if (metrics.length || sourceDetails.length)
      body.append(
        el("p", "plan-preserve", [...metrics, ...sourceDetails].join(" · ")),
      );
    const excluded = Array.isArray(workspace.excluded)
      ? workspace.excluded.map((item) => typeof item === "string" ? item : item?.path || item?.name).filter(Boolean)
      : [];
    if (excluded.length)
      body.append(
        el("p", "plan-preserve", `제외 항목: ${excluded.join(" · ")}`),
      );
    body.append(
      el(
        "p",
        "plan-preserve",
        `대상 기기에 새 폴더를 만듭니다. 원본 폴더를 덮어쓰지 않으며, 왕복 인계도 매번 새 폴더를 사용합니다.${transfer.destinationNote ? ` ${transfer.destinationNote}` : ""}`,
      ),
    );
  } else {
    route.append(
      el("span", null, "현재 작업"),
      el(
        "strong",
        null,
        [s.hostLabel, AGENTS[s.agent], s.account].filter(Boolean).join(" · "),
      ),
      el("span", "plan-route-arrow", "↓"),
      el("span", null, "이어서 사용할 환경"),
      el("strong", null, targetLabel(target)),
    );
    body.append(route);
  }
  if (plan.summary && !transfer) body.append(el("p", "plan-summary", plan.summary));
  if (!plan.allowed && plan.reason) body.append(el("p", "error", plan.reason));
  const warningItems = [
    ...(plan.warnings || []),
    ...(transfer?.workspace?.warnings || []),
  ];
  if (warningItems.length) {
    const warningList = el("ul", "plan-warnings");
    for (const warning of [...new Set([
      ...(plan.warnings || []),
      ...(transfer?.workspace?.warnings || []),
    ])])
      warningList.append(el("li", null, String(warning)));
    body.append(warningList);
  }
  if (!transfer)
    body.append(
      el(
        "p",
        "plan-preserve",
        plan.mode !== "handoff"
          ? "새 화면을 열어도 기존 작업 기록은 유지됩니다."
          : "원본 대화는 그대로 두고, 대상 환경에 전달할 맥락을 준비합니다.",
      ),
    );
  $("plan-launch").disabled = !plan.allowed;
  $("plan-launch").textContent =
    plan.mode === "transfer"
      ? `${transfer.requiresInterrupt ? "중단하고 " : ""}${transfer.targetHostLabel || hostLabel(target.host)}로 넘기기`
      : plan.mode === "reconnect"
      ? "열어 둔 터미널로 ↗"
      : plan.mode === "attach"
        ? "터미널 열기 ↗"
        : "맥락 전달 후 열기 ↗";
}
function closePlan() {
  if (state.launchBusy) return;
  state.planRun++;
  state.plan = null;
  $("plan-dialog").close();
}
async function launchPlan() {
  if (!state.plan?.allowed || state.launchBusy) return;
  const plan = state.plan;
  const authEpoch = state.authEpoch;
  state.launchBusy = true;
  $("plan-launch").disabled = true;
  $("plan-close").disabled = true;
  $("plan-cancel").disabled = true;
  $("plan-error").textContent = "";
  $("plan-body").append(
    progress(
      plan.mode === "transfer"
        ? plan.transfer?.requiresInterrupt
          ? "응답 중단·파일 복사·대상 실행 준비 중…"
          : "파일 복사·대상 실행 준비 중…"
        : plan.mode !== "handoff"
        ? "터미널에 연결하고 있습니다…"
        : "작업 맥락을 전달하고 터미널을 준비하고 있습니다…",
    ),
  );
  try {
    const result = await api("/api/launch", {
      method: "POST",
      body: { planId: plan.id, requestId: requestId() }, authEpoch,
    });
    assertAuthEpoch(authEpoch);
    if (!result.terminal?.id)
      throw new Error("터미널 연결 정보를 받지 못했습니다.");
    $("plan-dialog").close();
    state.plan = null;
    // Its card and conversation header show "터미널 열림" at once.
    rememberTerminal(result.terminal);
    await openTerminal(result.terminal);
    assertAuthEpoch(authEpoch);
    if (plan.mode === "transfer" || result.mode === "transfer")
      toast(
        `실제 실행 위치가 ${plan.transfer?.targetHostLabel || hostLabel(plan.target?.host || "")}로 바뀌었습니다. 새 작업 폴더에서 이어가세요.`,
      );
    else if (result.reused)
      toast(
        "열어 둔 터미널에 다시 연결했습니다. 새 요청은 터미널에서 이어가세요.",
      );
    refreshTerminals(authEpoch);
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      if (err.recovery) {
        const recovery = err.recovery;
        const location = hostLabel(recovery.host || plan.target?.host);
        const started = recovery.targetStarted === true ? "대상 작업이 이미 시작됐습니다." : "대상 작업이 이미 시작됐을 수 있습니다.";
        $("plan-error").textContent = `${err.message} ${location}: ${started} 다시 실행하지 말고 보드 목록을 새로고침해 대상 작업을 확인해 주세요.`;
        state.plan = null;
        if (recovery.terminal?.id) {
          const terminal = { ...recovery.terminal };
          terminal.host ||= recovery.host;
          if (!terminalInfo(terminal).nativeSource && recovery.nativeSource) terminal.nativeSource = { ...recovery.nativeSource };
          const existing = state.terminals.findIndex((item) => item.id === terminal.id);
          if (existing < 0) state.terminals.push(terminal);
          else state.terminals[existing] = { ...state.terminals[existing], ...terminal };
          renderTerminals();
        }
      } else $("plan-error").textContent = `${err.message} 원본 작업은 목록에서 다시 확인할 수 있습니다.`;
      $("plan-body").querySelector(".plan-progress")?.remove();
      $("plan-launch").disabled = true;
      $("plan-launch").textContent = "다시 검토해 주세요";
    }
  } finally {
    if (authEpoch === state.authEpoch) {
      state.launchBusy = false;
      $("plan-close").disabled = false;
      $("plan-cancel").disabled = false;
      const s = selectedSession();
      if (s && $("continue-open")) updateContinue(s);
      if (s) updateChatComposer(chatEntry(s));
    }
  }
}

function terminalStatus(text, connected = false) {
  state.connected = connected;
  $("terminal-status").textContent = text;
  $("terminal-status").dataset.connected = String(connected);
  for (const node of $("terminal-keys").querySelectorAll("button"))
    node.disabled = !connected || !!state.draftPaste;
  $("draft-paste").disabled = !connected || !!state.draftPaste;
  $("draft-type").disabled = !connected || !!state.draftPaste;
  $("terminal-reconnect").hidden = connected || !state.terminalAlive;
}
function terminalInfo(terminal) {
  return { ...terminal?.metadata, ...terminal };
}
function terminalCloseCopy(terminal) {
  const mode = terminal?.mode || terminal?.metadata?.mode;
  if (mode === "attach") return {
    action: "연결 닫기", title: "이 보드의 연결을 닫을까요?", danger: false,
    description: "이 보드의 연결만 닫습니다. 기존 작업에 중단 명령을 보내지 않습니다. 작업 상태는 실행 기기에서 확인하세요.",
    context: "기존 작업에 연결했습니다. 보드로 돌아가거나 브라우저를 닫아도 작업은 유지됩니다.",
    ended: "이 보드의 연결이 종료되었습니다. 기존 작업 상태는 실행 기기에서 확인하세요.",
    done: "연결을 닫았습니다. 기존 작업에 중단 명령을 보내지 않았습니다.",
  };
  if (mode === "handoff" || mode === "transfer") return {
    action: "실행 종료", title: "이 터미널의 실행을 종료할까요?", danger: true,
    description: "이 보드에서 새로 시작한 작업을 종료합니다. 진행 중인 응답이 중단될 수 있고, 대화 기록은 남습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
    context: `${mode === "transfer" ? "기기를 옮겨 새로 실행한" : "맥락을 전달해 새로 실행한"} 작업입니다. 보드로 돌아가거나 브라우저를 닫아도 실행은 유지됩니다.`,
    ended: "이 터미널의 새 실행이 종료되었습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
    done: "이 터미널의 실행을 종료했습니다. 이어서 하려면 실행 기기의 최근 작업을 선택하세요.",
  };
  return {
    action: "터미널 닫기", title: "이 터미널을 닫을까요?", danger: true,
    description: "실행 종류를 확인하지 못했습니다. 이 터미널을 닫으면 실행 중인 작업에 영향을 줄 수 있습니다. 대화 기록은 남으며, 작업 상태는 실행 기기에서 확인하세요.",
    context: "실행 종류를 확인하지 못했습니다. 보드로 돌아가거나 브라우저를 닫아도 터미널은 유지됩니다.",
    ended: "이 터미널의 연결이 종료되었습니다. 작업 상태는 실행 기기에서 확인하세요.",
    done: "터미널을 닫았습니다. 작업 상태는 실행 기기에서 확인하세요.",
  };
}
function draftKey(terminal) {
  const info = terminalInfo(terminal);
  return info.sourceKey
    ? JSON.stringify([info.sourceKey, info.host || "", info.agent || "", info.account || ""])
    : info.id;
}
function terminalError(text = "") {
  $("terminal-error").textContent = text;
  $("terminal-error").hidden = !text;
}
function stopTerminalReader() {
  cancelDraftPaste();
  state.terminalRun++;
  state.terminalAbort?.abort();
  state.terminalAbort = null;
  state.connected = false;
}
function saveDraft() {
  if (state.terminal)
    state.drafts.set(draftKey(state.terminal), $("terminal-draft").value);
}
function cancelDraftPaste() {
  const pending = state.draftPaste;
  if (!pending) return;
  clearTimeout(pending.compositionTimer);
  pending.cancelled = true;
  pending.resolve?.(false);
  state.draftPaste = null;
}
function settleDraftComposition() {
  const pending = state.draftPaste;
  if (!pending?.resolve || pending.frameQueued) return;
  pending.frameQueued = true;
  // compositionend follows the control update; the frame also coalesces late
  // input notifications. A separate final input event is not required.
  requestAnimationFrame(() => {
    if (state.draftPaste !== pending || pending.cancelled) return;
    pending.frameQueued = false;
    if (!state.composing) pending.resolve(true);
  });
}
function startDraftComposition(event) {
  if (event?.target && event.target !== $("terminal-draft")) return;
  if (state.draftPaste) {
    cancelDraftPaste();
    $("draft-note").textContent = "새 입력을 보관했습니다. 조합이 끝나면 붙여넣기를 눌러 주세요.";
    terminalStatus($("terminal-status").textContent, state.connected);
  }
  state.composing = true;
  state.compositionOwner = state.terminal?.id || null;
  saveDraft();
}
function endDraftComposition(event) {
  if (event?.target && event.target !== $("terminal-draft")) return;
  state.composing = false;
  if (state.compositionOwner && state.compositionOwner !== state.terminal?.id) {
    cancelDraftPaste();
    $("terminal-draft").value = state.drafts.get(draftKey(state.terminal)) || "";
    return;
  }
  saveDraft();
  settleDraftComposition();
}
function inputDraft(event) {
  if (event.target && event.target !== $("terminal-draft")) return;
  if (state.compositionOwner && state.compositionOwner !== state.terminal?.id &&
      (event.isComposing || /Composition/.test(event.inputType || ""))) {
    $("terminal-draft").value = state.drafts.get(draftKey(state.terminal)) || "";
    return;
  }
  if (!event.isComposing) state.compositionOwner = state.terminal?.id || null;
  state.draftRevision++;
  saveDraft();
  // Final input may arrive before the paste click, without compositionend.
  if (event.isComposing === false) {
    state.composing = false;
    settleDraftComposition();
  }
}
function prepareDraftPaste(event) {
  // Retain textarea focus until the explicit click can request an IME commit.
  event.preventDefault();
}
function bindDraftInput(node) {
  node.addEventListener("input", (event) => {
    inputDraft(event);
    resizeDraft();
  });
  node.addEventListener("keydown", draftKeydown);
  node.addEventListener("compositionstart", startDraftComposition);
  node.addEventListener("compositionend", endDraftComposition);
}
// Korean needs monospace fallbacks that keep two-cell glyphs aligned. Only
// fonts already on the device are named; no web font is bundled.
const TERMINAL_FONT = 'ui-monospace, SFMono-Regular, Menlo, Consolas, D2Coding, "D2Coding ligature", ' +
  '"Nanum Gothic Coding", NanumGothicCoding, "Noto Sans Mono CJK KR", "Source Han Mono K", "Sarasa Mono K", monospace';
function clampFontSize(size) {
  return Math.max(GESTURE.pinchMinFont, Math.min(GESTURE.pinchMaxFont, Math.round(Number(size) || 14)));
}
// A−/A+ and the pinch share one range and one stored preference.
function setTerminalFontSize(size, { persist = true } = {}) {
  const next = clampFontSize(size);
  const changed = next !== state.fontSize;
  state.fontSize = next;
  if (persist) prefs.set("fontSize", next);
  if (changed && state.term) {
    state.term.options.fontSize = next;
    fitTerminal();
  }
  return changed;
}
function pinchFontSize(startSize, startDistance, distance) {
  if (!(startDistance > 0) || !(distance > 0)) return clampFontSize(startSize);
  return clampFontSize(startSize * (distance / startDistance));
}
function touchDistance(touches) {
  const [a, b] = [touches[0], touches[1]];
  return a && b ? Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) : 0;
}
function fitTerminal() {
  measureTerminalTools();
  if (!state.term || !state.terminalVisible || !state.fit) return;
  try {
    state.fit.fit();
  } catch {
    return;
  }
  clearTimeout(state.resizeTimer);
  state.resizeTimer = setTimeout(async () => {
    if (!state.connected || !state.terminal) return;
    const size = `${state.term.cols}x${state.term.rows}`;
    if (size === state.lastSize) return;
    try {
      await api(
        `/api/terminal/${encodeURIComponent(state.terminal.id)}/resize`,
        {
          method: "POST",
          body: { cols: state.term.cols, rows: state.term.rows },
        },
      );
      state.lastSize = size;
    } catch (err) {
      if (!err.unauthorized && !err.staleAuth)
        terminalError("터미널 크기를 맞추지 못했습니다. 다시 연결해 주세요.");
    }
  }, 180);
}
function showTerminalScreen(visible) {
  $("app").hidden = visible;
  $("terminal-screen").hidden = !visible;
}
// Keeps the toast above the key bar (style.css, section 8).
function measureTerminalTools() {
  const tools = $("terminal-tools");
  if (tools?.offsetHeight) document.documentElement.style?.setProperty?.("--terminal-tools-h", `${tools.offsetHeight}px`);
}
// The terminal library (xterm + fit addon, ~490KB) loads on the first
// terminal open, once; a failed load can be retried by opening again.
function assetVersions() {
  if (state.assetVersions) return state.assetVersions;
  let versions = {};
  try {
    const meta = document.querySelector?.('meta[name="sessionholic-assets"]');
    const parsed = JSON.parse(meta?.getAttribute?.("content") || "{}");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) versions = parsed;
  } catch {
    versions = {};
  }
  state.assetVersions = versions;
  return versions;
}
function assetUrl(path) {
  const version = assetVersions()[path];
  return typeof version === "string" && /^[0-9a-f]{8}$/.test(version) ? `${path}?v=${version}` : path;
}
let xtermLoading = null;
// The stylesheet and both scripts are all required. Each one that loaded is
// remembered, so opening again retries only the ones that failed.
const xtermLoaded = new Set();
function xtermReady() {
  return !!(window.Terminal && window.FitAddon) && xtermLoaded.has("/vendor/xterm.css");
}
function loadXterm() {
  if (xtermReady()) return Promise.resolve();
  if (xtermLoading) return xtermLoading;
  const head = document.head;
  if (typeof head?.appendChild !== "function")
    return Promise.reject(new Error("터미널 화면 파일을 불러올 수 없습니다."));
  const add = (path, tag, attributes, loaded = () => true) => new Promise((resolve, reject) => {
    const node = document.createElement(tag);
    Object.assign(node, attributes);
    node.onload = () => {
      if (loaded()) xtermLoaded.add(path);
      resolve();
    };
    node.onerror = () => {
      node.remove?.();
      reject(new Error("터미널 화면 파일을 받지 못했습니다."));
    };
    head.appendChild(node);
  });
  const assets = [
    ["/vendor/xterm.css", "link", { rel: "stylesheet", href: assetUrl("/vendor/xterm.css") }],
    // Inserted scripts with async=false still run in insertion order.
    ["/vendor/xterm.js", "script", { src: assetUrl("/vendor/xterm.js"), async: false }, () => !!window.Terminal],
    ["/vendor/addon-fit.js", "script", { src: assetUrl("/vendor/addon-fit.js"), async: false }, () => !!window.FitAddon],
  ];
  xtermLoading = Promise.all(assets.filter(([path]) => !xtermLoaded.has(path)).map((asset) => add(...asset))).then(() => {
    if (!window.Terminal || !window.FitAddon) {
      xtermLoaded.delete("/vendor/xterm.js");
      xtermLoaded.delete("/vendor/addon-fit.js");
      throw new Error("터미널 화면 파일이 올바르지 않습니다.");
    }
    if (!xtermReady()) throw new Error("터미널 화면 파일을 받지 못했습니다.");
  }).catch((err) => {
    xtermLoading = null;
    throw err;
  });
  return xtermLoading;
}
async function openTerminal(terminal, { recordHistory = true } = {}) {
  stopChatPolling();
  if (recordHistory) navigateView("terminal");
  const presenting = !state.terminalVisible;
  const openRun = ++state.terminalOpenRun;
  const current = () => openRun === state.terminalOpenRun && state.terminal?.id === terminal.id && state.terminalVisible;
  const changing = state.terminal?.id !== terminal.id;
  saveDraft();
  if (state.composing) {
    $("terminal-draft").blur();
    saveDraft();
  }
  if (changing && $("terminal-draft").cloneNode) {
    // Closing a terminal may clear the composition flag before its final input.
    // Every new terminal identity gets a new control, so old events stay detached.
    const previous = $("terminal-draft");
    const next = previous.cloneNode(true);
    previous.replaceWith(next);
    bindDraftInput(next);
  }
  stopTerminalReader();
  state.inputEpoch++;
  state.inputQueue = Promise.resolve();
  state.inputFailed = false;
  state.terminal = terminal;
  state.terminalAlive = terminal.alive !== false && !terminal.closed;
  state.terminalVisible = true;
  renderConnectivity();
  const shown = presenting ? navTransition("present", () => showTerminalScreen(true)) : (showTerminalScreen(true), null);
  updateThemeColor();
  closeEvents();
  $("terminal-title").textContent = terminal.title || "진행 중인 작업";
  $("terminal-route").textContent = targetLabel(terminalInfo(terminal));
  const nativeHost = terminalInfo(terminal).nativeSource?.host || terminalInfo(terminal).host;
  $("terminal-back").setAttribute("aria-label", `${hostLabel(nativeHost)}의 작업을 보드에서 보기`);
  const copy = terminalCloseCopy(terminal);
  $("terminal-close").textContent = copy.action;
  $("terminal-close").setAttribute("aria-label", copy.action);
  $("terminal-close").className = `terminal-button terminal-close${copy.danger ? " danger-quiet" : ""}`;
  $("terminal-close").disabled = !state.terminalAlive;
  $("terminal-context").textContent = copy.context;
  $("terminal-draft").value = state.drafts.get(draftKey(terminal)) || "";
  state.composing = false;
  state.draftRevision++;
  $("draft-note").textContent = "";
  terminalError();
  renderTerminalReturn();
  if (!state.terminalAlive) {
    terminalStatus(copy.ended);
    return;
  }
  if (!xtermReady()) {
    const viewport = $("terminal-viewport");
    viewport.classList?.add("is-loading");
    terminalStatus("터미널 화면을 불러오고 있습니다…");
    try {
      await loadXterm();
    } catch {
      if (current()) {
        terminalStatus("터미널 화면을 불러오지 못했습니다.");
        terminalError("터미널 화면 파일을 받지 못했습니다. 연결을 확인한 뒤 보드로 돌아가 다시 열어 주세요.");
      }
      return;
    } finally {
      viewport.classList?.remove("is-loading");
    }
    if (!current()) return;
  }
  // xterm measures its cells on open, so the screen must be visible first.
  if (shown?.updateCallbackDone) {
    await shown.updateCallbackDone.catch(() => {});
    if (!current()) return;
  }
  if (changing || !state.term) {
    state.term?.dispose();
    state.terminalAfter = 0;
    state.terminalEpoch = null;
    state.lastSize = "";
    $("terminal-viewport").replaceChildren();
    state.term = new Terminal({
      fontFamily: TERMINAL_FONT,
      fontSize: state.fontSize,
      lineHeight: 1.2,
      cursorBlink: true,
      scrollback: 5000,
      screenReaderMode: state.screenReaderMode,
      convertEol: false,
      allowProposedApi: false,
      theme: {
        background: "#161618",
        foreground: "#ececf1",
        cursor: "#0a84ff",
        selectionBackground: "rgba(142,142,147,0.35)",
      },
    });
    state.fit = new FitAddon.FitAddon();
    state.term.loadAddon(state.fit);
    state.term.open($("terminal-viewport"));
    configureTerminalKeyboard();
    // An app that asked for the mouse gets the wheel from xterm itself (tmux
    // forwards it); otherwise the wheel scrolls tmux history instead of
    // reaching the CLI as arrow keys.
    state.term.attachCustomWheelEventHandler?.((event) => {
      if ((state.term?.modes?.mouseTrackingMode || "none") !== "none") return true;
      queueTerminalScroll(event.deltaY / terminalLineHeight());
      return false;
    });
    state.term.onData((text) => {
      if (!state.connected) return;
      state.terminalScrollLines = 0;
      renderTerminalScroll();
      sendInput(text).catch(() => {});
    });
    state.term.onBinary((data) => {
      if (state.connected)
        terminalError(
          "이 입력 형식은 지원하지 않습니다. 일반 키보드 입력을 사용해 주세요.",
        );
    });
  }
  requestAnimationFrame(fitTerminal);
  renderTerminalReturn();
  terminalStatus("터미널에 연결하고 있습니다…");
  startTerminalReader();
}
const TERMINAL_RETRIES = 6;
async function startTerminalReader() {
  if (networkOffline()) return;
  if (!state.terminal || !state.terminalVisible || document.hidden) return;
  if (!state.terminalAlive || state.terminal.closed) {
    terminalStatus(terminalCloseCopy(state.terminal).ended);
    renderTerminalReturn();
    return;
  }
  stopTerminalReader();
  const run = state.terminalRun;
  const id = state.terminal.id;
  const controller = new AbortController();
  state.terminalAbort = controller;
  state.inputFailed = false;
  state.inputEpoch++;
  terminalError();
  terminalStatus("터미널에 연결하고 있습니다…");
  while (
    run === state.terminalRun &&
    state.terminalVisible &&
    !document.hidden
  ) {
    try {
      // A silently dropped connection (Wi-Fi to LTE) must not hold the long poll forever.
      const signal = globalThis.AbortSignal?.any && AbortSignal.timeout
        ? AbortSignal.any([controller.signal, AbortSignal.timeout(35000)]) : controller.signal;
      const result = await api(
        `/api/terminal/${encodeURIComponent(id)}/events?after=${state.terminalAfter}&wait=20${state.terminalEpoch ? `&epoch=${encodeURIComponent(state.terminalEpoch)}` : ""}`,
        { signal },
      );
      if (run !== state.terminalRun) return;
      // A malformed reply counts as a failed read, so it can never spin this loop.
      if (!result || !Number.isFinite(Number(result.after)))
        throw new Error("터미널 응답 형식이 올바르지 않습니다.");
      state.terminalRetry = 0;
      const screen = state.term;
      const commitCursor = () => {
        if (state.terminal?.id !== id || state.term !== screen) return;
        state.terminalAfter = Number.isFinite(Number(result.after))
          ? Number(result.after)
          : state.terminalAfter;
        state.terminalEpoch = result.epoch || state.terminalEpoch;
      };
      if (result.reset) {
        screen.reset();
        terminalError(
          "최근 출력부터 다시 연결했습니다. 이전 대화는 보드에서 확인할 수 있습니다.",
        );
      }
      if (result.data) {
        const bytes = Uint8Array.from(atob(result.data), (char) =>
          char.charCodeAt(0),
        );
        const written = new Promise((resolve) => screen.write(bytes, resolve));
        // xterm owns the queued bytes now. Reconnecting during its callback
        // must start after them, while a replaced screen keeps its own cursor.
        commitCursor();
        await written;
      } else commitCursor();
      if (run !== state.terminalRun) return;
      state.terminalAlive = result.alive !== false;
      if (!state.terminalAlive) {
        state.terminal.alive = false;
        $("terminal-close").disabled = true;
        terminalStatus(terminalCloseCopy(state.terminal).ended);
        renderTerminalReturn();
        refreshTerminals();
        return;
      }
      if (!state.inputFailed) {
        terminalStatus("연결됨 · 입력 가능", true);
        fitTerminal();
      }
    } catch (err) {
      if (
        err.name === "AbortError" ||
        run !== state.terminalRun ||
        err.unauthorized || err.staleAuth
      )
        return;
      // Reading output is cursor based and safe to repeat; queued input is
      // still invalidated so an uncertain keystroke is never sent twice.
      state.inputEpoch++;
      const attempt = (state.terminalRetry || 0) + 1;
      state.terminalRetry = attempt;
      if (attempt > TERMINAL_RETRIES || networkOffline()) {
        state.terminalRetry = 0;
        terminalStatus("연결을 확인할 수 없습니다.");
        terminalError(
          "새 입력은 보내지 않습니다. 실행 상태를 확인하려면 다시 연결해 주세요.",
        );
        return;
      }
      terminalStatus(`연결이 끊겨 다시 연결하는 중… (${attempt}/${TERMINAL_RETRIES})`);
      await new Promise((resolve) => setTimeout(resolve, Math.min(15000, 1000 * 2 ** (attempt - 1))));
      if (run !== state.terminalRun || networkOffline()) return;
    }
  }
}
function sendInput(text) {
  if (networkOffline() || !state.connected || state.inputFailed || !state.terminal)
    return Promise.reject(new Error("터미널에 연결되어 있지 않습니다."));
  const epoch = state.inputEpoch;
  const id = state.terminal.id;
  const req = requestId();
  const send = state.inputQueue.then(async () => {
    if (
      epoch !== state.inputEpoch ||
      networkOffline() || !state.connected ||
      state.terminal?.id !== id
    )
      throw new Error("연결이 바뀌어 입력을 보내지 않았습니다.");
    try {
      await api(`/api/terminal/${encodeURIComponent(id)}/input`, {
        method: "POST",
        body: { text, requestId: req },
      });
    } catch (err) {
      if (!err.unauthorized && !err.staleAuth && epoch === state.inputEpoch) {
        state.inputFailed = true;
        state.inputEpoch++;
        stopTerminalReader();
        terminalStatus("입력 전달을 확인하지 못했습니다.");
        terminalError(
          "입력이 반영됐을 수도 있어 자동으로 다시 보내지 않습니다. 다시 연결한 뒤 터미널 내용을 확인해 주세요.",
        );
      }
      throw err;
    }
  });
  state.inputQueue = send.catch(() => {});
  return send;
}
function returnToBoard({ selectLaunchedSource = true, animate = true } = {}) {
  saveDraft();
  stopTerminalReader();
  state.inputEpoch++;
  state.terminalOpenRun++;
  state.terminalVisible = false;
  renderConnectivity();
  if (animate) navTransition("dismiss", () => showTerminalScreen(false));
  else showTerminalScreen(false);
  updateThemeColor();
  renderTerminalReturn();
  schedule();
  syncEvents();
  const info = terminalInfo(state.terminal);
  if (selectLaunchedSource && state.authenticated && info.nativeSource &&
      ["handoff", "transfer"].includes(info.launchMode || info.mode)) {
    // Returning from the new execution follows the destination the user launched.
    // Its draft is separate; choosing any source in the list still never redirects.
    openNativeChat(state.terminal);
  }
  const selected = selectedSession();
  if (selected) loadChatState(selected, true);
}
function renderTerminalReturn() {
  const exists = !!state.terminal && state.terminalAlive && state.terminal.alive !== false && !state.terminal.closed;
  $("terminal-return").hidden = !exists;
  const info = terminalInfo(state.terminal);
  const host = capsHost(info.host)?.label || info.host;
  $("terminal-return-label").textContent = exists
    ? [state.terminal.title || "열어 둔 터미널", host].filter(Boolean).join(" · ")
    : "";
  $("return-terminal").setAttribute?.("aria-label", exists ? `열린 터미널로 돌아가기: ${$("terminal-return-label").textContent}` : "열린 터미널로 돌아가기");
}
function touchDevice() {
  return !!window.matchMedia?.("(pointer: coarse)").matches;
}
function directTerminalInput() {
  return typeof state.directTerminalInput === "boolean" ? state.directTerminalInput : !touchDevice();
}
function configureTerminalKeyboard() {
  // Mobile IMEs and dictation rewrite whole words inside xterm's hidden field,
  // which reaches the CLI as repeated text. Touch devices type in the input bar.
  const field = state.term?.textarea;
  if (!field?.setAttribute) return;
  if (directTerminalInput()) field.removeAttribute("inputmode");
  else field.setAttribute("inputmode", "none");
}
function terminalLineHeight() {
  return Math.max(8, (state.fontSize || 14) * 1.2);
}
function renderTerminalScroll() {
  const scrolled = state.terminalScrollLines > 0;
  $("terminal-bottom")?.classList?.toggle?.("is-active", scrolled);
  $("terminal-screen")?.classList?.toggle?.("is-scrolled", scrolled);
  const live = $("terminal-live");
  if (!live) return;
  live.hidden = !scrolled;
  if (scrolled) {
    measureTerminalTools();
    live.textContent = `실시간으로 ↓ · ${state.terminalScrollLines}줄 위`;
    const tools = $("terminal-tools") || document.querySelector?.(".terminal-tools");
    if (live.style && tools?.offsetHeight) live.style.bottom = `${tools.offsetHeight + 12}px`;
  }
}
function queueTerminalScroll(lines) {
  if (!state.connected || !state.terminal || !Number.isFinite(lines) || !lines) return;
  state.terminalScrollPending += lines;
  if (state.terminalScrollTimer || state.terminalScrollInFlight) return;
  state.terminalScrollTimer = setTimeout(flushTerminalScroll, 60);
}
async function flushTerminalScroll() {
  state.terminalScrollTimer = null;
  const whole = Math.trunc(state.terminalScrollPending);
  if (!whole || !state.terminal) return;
  state.terminalScrollPending -= whole;
  // Up reveals older output. Below the live screen there is nothing in tmux
  // history, but an app that owns its screen (alternate screen) scrolls itself.
  let lines = Math.max(-200, Math.min(200, whole));
  if (lines > 0 && state.terminalScrollMode === "history") lines = Math.min(lines, state.terminalScrollLines);
  if (!lines) return;
  const estimate = Math.max(0, state.terminalScrollLines - lines);
  state.terminalScrollInFlight = true;
  try {
    const result = await api(`/api/terminal/${encodeURIComponent(state.terminal.id)}/scroll`,
      { method: "POST", body: { lines } });
    if (result?.mode === "app") {
      state.terminalScrollMode = "app";
      state.terminalScrollLines = 0;
    } else {
      state.terminalScrollMode = "history";
      state.terminalScrollLines = Number.isInteger(result?.position) ? result.position : estimate;
    }
  } catch {
    state.terminalScrollLines = 0;
    stopTerminalMomentum();
  } finally {
    state.terminalScrollInFlight = false;
    renderTerminalScroll();
    // Swipes that arrived during the request are sent next, never dropped or doubled.
    if (Math.trunc(state.terminalScrollPending) && !state.terminalScrollTimer)
      state.terminalScrollTimer = setTimeout(flushTerminalScroll, 30);
  }
}
async function terminalScrollEnd() {
  stopTerminalMomentum();
  state.terminalScrollPending = 0;
  state.terminalScrollLines = 0;
  renderTerminalScroll();
  state.term?.scrollToBottom();
  if (!state.connected || !state.terminal) return;
  try {
    await api(`/api/terminal/${encodeURIComponent(state.terminal.id)}/scroll-end`, { method: "POST", body: {} });
  } catch {
    /* typing also returns to the live screen */
  }
}
function stopTerminalMomentum() {
  if (state.terminalMomentum) globalThis.cancelAnimationFrame?.(state.terminalMomentum);
  state.terminalMomentum = null;
}
// A flick keeps scrolling and slows down, like any native list.
function startTerminalMomentum(velocity) {
  stopTerminalMomentum();
  let speed = velocity;
  let last = Date.now();
  const started = last;
  const step = () => {
    const now = Date.now();
    const elapsed = Math.max(1, now - last);
    last = now;
    queueTerminalScroll((-speed * elapsed) / terminalLineHeight());
    speed *= Math.pow(GESTURE.momentumDecay, elapsed / 16);
    if (Math.abs(speed) < GESTURE.momentumStopSpeed || now - started > GESTURE.momentumMaxMs || !state.connected) {
      state.terminalMomentum = null;
      return;
    }
    state.terminalMomentum = requestAnimationFrame(step);
  };
  state.terminalMomentum = requestAnimationFrame(step);
}
function bindTerminalTouchScroll(node) {
  let lastY = null;
  let samples = [];
  // Two fingers change the text size instead of scrolling; saved on release.
  let pinch = null;
  const endPinch = () => {
    if (pinch?.changed) {
      prefs.set("fontSize", state.fontSize);
      toast(`터미널 글자 크기 ${state.fontSize}`);
    }
    pinch = null;
  };
  // Capture phase: xterm's own layers must not swallow the gesture first.
  node.addEventListener("touchstart", (event) => {
    stopTerminalMomentum();
    if (event.touches.length === 2) {
      pinch = { size: state.fontSize, distance: touchDistance(event.touches), changed: false };
      lastY = null;
      samples = [];
      return;
    }
    // A new single touch starts a new gesture (also after a cancelled pinch).
    if (event.touches.length === 1) endPinch();
    if (pinch) return;
    lastY = event.touches.length === 1 ? event.touches[0].clientY : null;
    samples = lastY === null ? [] : [[Date.now(), lastY]];
  }, { passive: true, capture: true });
  node.addEventListener("touchmove", (event) => {
    if (pinch) {
      if (event.touches.length === 2 &&
          setTerminalFontSize(pinchFontSize(pinch.size, pinch.distance, touchDistance(event.touches)), { persist: false }))
        pinch.changed = true;
      if (event.cancelable) event.preventDefault();
      return;
    }
    if (lastY === null || event.touches.length !== 1) return;
    const y = event.touches[0].clientY;
    // Dragging down reveals older lines, as in any scrolling list.
    queueTerminalScroll((lastY - y) / terminalLineHeight());
    lastY = y;
    samples.push([Date.now(), y]);
    if (samples.length > 6) samples.shift();
    if (event.cancelable) event.preventDefault();
  }, { passive: false, capture: true });
  node.addEventListener("touchend", (event) => {
    if (pinch) {
      // The finger left behind does not start a scroll.
      if ((event?.touches?.length || 0) < 2) endPinch();
      lastY = null;
      samples = [];
      return;
    }
    const recent = samples.filter(([time]) => Date.now() - time < 100);
    if (recent.length >= 2) {
      const [t0, y0] = recent[0];
      const [t1, y1] = recent[recent.length - 1];
      const velocity = (y1 - y0) / Math.max(1, t1 - t0);
      if (Math.abs(velocity) > GESTURE.momentumMinSpeed) startTerminalMomentum(velocity);
    }
    lastY = null;
    samples = [];
  }, { passive: true, capture: true });
}
function resizeDraft() {
  const input = $("terminal-draft");
  if (!input?.style) return;
  const before = input.style.height;
  input.style.height = "auto";
  const line = parseFloat(window.getComputedStyle?.(input)?.lineHeight) || 22;
  input.style.height = `${Math.min(input.scrollHeight, line * 4 + 20)}px`;
  // A taller or shorter input bar changes the terminal's rows; refit so no line is hidden.
  if (input.style.height !== before) requestAnimationFrame(fitTerminal);
}
function draftKeydown(event) {
  // Enter sends like a chat box; Shift+Enter keeps a newline in the draft.
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  pasteDraft(true);
}
async function pasteDraft(submit = false) {
  if (state.draftPaste) return;
  if (!state.connected || !state.terminal) {
    $("draft-note").textContent = "터미널에 다시 연결한 뒤 붙여넣어 주세요.";
    return;
  }
  const pending = { id: state.terminal.id, key: draftKey(state.terminal), epoch: state.inputEpoch, cancelled: false };
  state.draftPaste = pending;
  terminalStatus($("terminal-status").textContent, true);
  try {
    if (state.composing) {
      $("draft-note").textContent = "한글 조합을 확정하고 있습니다. 초안은 보관됩니다.";
      const committed = new Promise((resolve) => { pending.resolve = resolve; });
      pending.compositionTimer = setTimeout(() => {
        if (state.draftPaste !== pending) return;
        cancelDraftPaste();
        $("draft-note").textContent = "한글 조합을 확인하지 못해 보내지 않았습니다. 조합을 마친 뒤 다시 붙여넣어 주세요.";
        terminalStatus($("terminal-status").textContent, state.connected);
      }, 1500);
      $("terminal-draft").blur();
      if (!(await committed)) return;
      clearTimeout(pending.compositionTimer);
      pending.resolve = null;
    }
    if (pending.cancelled || state.terminal?.id !== pending.id ||
        state.inputEpoch !== pending.epoch || !state.connected) return;
    const text = $("terminal-draft").value;
    const revision = state.draftRevision;
    if (!text) return;
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text)) {
      $("draft-note").textContent =
        "초안에 제어 문자가 포함되어 있습니다. 일반 텍스트만 붙여넣을 수 있습니다.";
      return;
    }
    const bracketed = !!state.term?.modes?.bracketedPasteMode;
    if (/[\r\n]/.test(text) && !bracketed) {
      $("draft-note").textContent =
        "현재 터미널 입력칸은 안전한 여러 줄 붙여넣기를 지원하지 않습니다. 한 줄씩 전달해 주세요.";
      return;
    }
    if (new TextEncoder().encode(text).length > 32750) {
      $("draft-note").textContent =
        "한 번에 붙여넣을 수 있는 길이를 넘었습니다. 초안을 나누어 전달해 주세요.";
      return;
    }
    const normalized = text.replace(/\r\n?/g, "\n");
    await sendInput(bracketed ? `\x1b[200~${normalized}\x1b[201~` : normalized);
    if (pending.cancelled || state.terminal?.id !== pending.id || state.inputEpoch !== pending.epoch) return;
    if (submit) {
      // A separate Enter after the TUI has handled the paste, as a person would type it.
      await new Promise((resolve) => setTimeout(resolve, 120));
      if (pending.cancelled || state.terminal?.id !== pending.id || state.inputEpoch !== pending.epoch) return;
      await sendInput("\r");
    }
    if (state.draftRevision === revision && $("terminal-draft").value === text) {
      $("terminal-draft").value = "";
      state.drafts.delete(pending.key);
      resizeDraft();
    }
    $("draft-note").textContent = submit ? "" : "입력했습니다. 확인한 뒤 Enter를 눌러 실행하세요.";
    if (submit) haptic("success");
    state.terminalScrollLines = 0;
    renderTerminalScroll();
    state.term.scrollToBottom();
  } catch {
    if (state.terminal?.id === pending.id) {
      $("draft-note").textContent =
        "전달 여부를 확인하지 못해 초안을 남겼습니다. 터미널을 확인한 뒤 다시 시도하세요.";
      haptic("error");
    }
  } finally {
    clearTimeout(pending.compositionTimer);
    if (state.draftPaste === pending) {
      state.draftPaste = null;
      terminalStatus($("terminal-status").textContent, state.connected);
    }
  }
}
async function refreshTerminals(authEpoch = state.authEpoch) {
  if (authEpoch !== state.authEpoch) return null;
  if (state.terminalsRefreshing) return null;
  state.terminalsRefreshing = true;
  $("running-refresh").disabled = true;
  try {
    const result = await api("/api/terminals", { authEpoch });
    assertAuthEpoch(authEpoch);
    state.terminals = result.terminals || [];
    state.terminalsError = "";
    // The version the server read before this list; a newer announcement reads again.
    state.terminalsVersion = Number.isInteger(result.version) ? result.version : null;
    state.terminalsReadAt = Date.now();
    updateInputCleanupNotice(result.inputCleanupPending === true);
    const current = state.terminals.find((t) => t.id === state.terminal?.id);
    if (current) {
      state.terminal = { ...state.terminal, ...current };
      if (current.closed || current.alive === false) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        state.terminalAlive = false;
        terminalStatus(terminalCloseCopy(state.terminal).ended);
        $("terminal-close").disabled = true;
      }
      renderTerminalReturn();
    }
    const closing = state.closingTerminal;
    if (closing?.uncertain && state.terminals.some((t) => t.id === closing.id && (t.closed || t.alive === false)))
      finishTerminalClose(closing);
    renderTerminals();
    return state.terminals;
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      state.terminalsError = "열린 터미널 목록을 확인하지 못했습니다. 마지막 목록을 표시합니다.";
      renderTerminals();
    }
    return null;
  } finally {
    if (authEpoch === state.authEpoch) {
      state.terminalsRefreshing = false;
      $("running-refresh").disabled = false;
      // A change announced during this read: read once more if still behind.
      if (state.terminalsPending) {
        state.terminalsPending = false;
        if (terminalsBehind()) terminalsAnnounced(state.eventsTerminalsVersion);
      }
    }
  }
}
// Live updates announce the terminal list's version ("terminals"): creates,
// closes, attaches and detaches on any device, and terminals that ended on
// their own. A burst is read together after TERMINALS_EVENT_BATCH_MS.
const TERMINALS_EVENT_BATCH_MS = 300;
function terminalsBehind() {
  return Number.isInteger(state.eventsTerminalsVersion) &&
    !(Number.isInteger(state.terminalsVersion) && state.eventsTerminalsVersion <= state.terminalsVersion);
}
// hello: a stream's first message. On the first connection nothing was read
// yet and the read after login shows the list, so only a reconnect reads.
function terminalsAnnounced(version, { hello = false } = {}) {
  if (!Number.isInteger(version)) return;
  state.eventsTerminalsVersion = Math.max(version, state.eventsTerminalsVersion ?? version);
  if (Number.isInteger(state.terminalsVersion) && version <= state.terminalsVersion) return;
  if (hello && !Number.isInteger(state.terminalsVersion) && !state.terminalsReadAt && !state.terminalsError) return;
  if (state.terminalsEventTimer) return;
  const authEpoch = state.authEpoch;
  state.terminalsEventTimer = setTimeout(() => {
    state.terminalsEventTimer = null;
    if (authEpoch === state.authEpoch) readAnnouncedTerminals();
  }, TERMINALS_EVENT_BATCH_MS);
}
function readAnnouncedTerminals() {
  if (!state.authenticated || !terminalsBehind()) return;
  if (state.terminalsRefreshing) state.terminalsPending = true;
  else refreshTerminals();
}
// An open or a close made here shows at once; the list read that follows confirms it.
function rememberTerminal(terminal, changes = {}) {
  if (!terminal?.id) return;
  const index = state.terminals.findIndex((item) => item.id === terminal.id);
  const row = { ...(index < 0 ? {} : state.terminals[index]), ...terminal, ...changes };
  state.terminals = index < 0 ? [...state.terminals, row] : state.terminals.map((item, i) => (i === index ? row : item));
  renderTerminals();
}
function renderTerminals() {
  const list = $("running-list");
  const focused = document.activeElement?.dataset;
  const restoreFocus = focused?.terminalId ? { id: focused.terminalId, action: focused.action } : null;
  list.replaceChildren();
  const alive = state.terminals.filter((t) => t.alive !== false && !t.closed);
  // A chip in the list's top row; the rows themselves live in a sheet.
  const chip = $("running-terminals");
  chip.hidden = !alive.length && !state.terminalsError;
  setClass(chip, "has-error", !!state.terminalsError);
  chip.setAttribute?.("aria-label", `열린 터미널 ${alive.length}개${state.terminalsError ? ", 목록 확인 필요" : ""}`);
  if (chip.hidden && $("terminals-dialog")?.open) closeSheet("terminals-dialog");
  $("running-error").textContent = state.terminalsError;
  $("running-error").hidden = !state.terminalsError;
  $("running-refresh").hidden = !state.terminalsError;
  $("running-count").textContent = alive.length;
  for (const terminal of alive) {
    const node = button("", "running-terminal", () => {
      closeSheet("terminals-dialog");
      openTerminal(terminal);
    });
    node.dataset.terminalId = terminal.id;
    node.dataset.action = "open";
    const labels = el("span", "running-terminal-copy");
    labels.append(
      el("strong", "running-terminal-title", terminal.title || "열어 둔 터미널"),
      el("span", "running-terminal-meta", targetLabel(terminalInfo(terminal))),
    );
    const arrow = el("span", null, "↗");
    arrow.setAttribute("aria-hidden", "true");
    node.append(
      el("span", "dot"),
      labels,
      arrow,
    );
    const row = el("div", "running-terminal-row");
    const copy = terminalCloseCopy(terminal);
    const close = button(copy.action, `running-terminal-close${copy.danger ? " danger-quiet" : ""}`, () => closeTerminal(terminal.id));
    close.dataset.terminalId = terminal.id;
    close.dataset.action = "close";
    close.setAttribute("aria-label", `${terminal.title || "터미널"} ${copy.action}`);
    row.append(node, close);
    list.append(row);
    if (restoreFocus?.id === terminal.id)
      (restoreFocus.action === "close" ? close : node).focus();
  }
  // Cards and the open conversation offer "연결 닫기" for these terminals, and
  // show "터미널 열림" while one is live.
  renderTerminalMarks();
  const selected = selectedSession();
  if (selected) updateChatComposer(chatEntry(selected));
}

function closeTerminal(id) {
  if (state.terminalCloseBusy) return;
  if ($("close-terminal-dialog").open) {
    $("close-terminal-cancel").focus();
    return;
  }
  const terminal = state.terminals.find((item) => item.id === id) ||
    (state.terminal?.id === id ? state.terminal : null);
  if (!terminal) return;
  const copy = terminalCloseCopy(terminal);
  state.closingTerminal = { ...terminal, id, title: terminal.title || "열어 둔 터미널", copy, trigger: document.activeElement };
  $("close-terminal-title").textContent = copy.title;
  $("close-terminal-description").textContent = copy.description;
  $("close-terminal-name").textContent = state.closingTerminal.title;
  const draft = state.terminal?.id === id ? $("terminal-draft").value : state.drafts.get(draftKey(terminal));
  $("close-terminal-draft-note").hidden = !draft;
  $("close-terminal-draft-note").textContent = draft
    ? "같은 작업을 같은 기기·에이전트·계정으로 열면 초안을 다시 볼 수 있습니다. 새로고침하거나 브라우저를 닫으면 사라집니다."
    : "";
  $("close-terminal-error").textContent = "";
  $("close-terminal-confirm").textContent = copy.action;
  $("close-terminal-confirm").className = `primary${copy.danger ? " danger" : ""}`;
  $("close-terminal-confirm").disabled = false;
  sheet("close-terminal-dialog").open();
  $("close-terminal-cancel").focus();
}
function cancelTerminalClose() {
  if (state.terminalCloseBusy) return;
  const trigger = state.closingTerminal?.trigger;
  state.closingTerminal = null;
  $("close-terminal-dialog").close();
  if (trigger?.isConnected !== false) trigger?.focus();
}
function finishTerminalClose(closing) {
  // Cards and the conversation header drop "터미널 열림" at once.
  if (state.terminals.some((item) => item.id === closing.id && item.alive !== false && !item.closed))
    rememberTerminal({ id: closing.id }, { alive: false, closed: true });
  if (state.terminal?.id === closing.id) {
    const wasVisible = state.terminalVisible;
    saveDraft();
    stopTerminalReader();
    state.inputEpoch++;
    state.terminal = null;
    state.terminalAlive = false;
    state.terminalVisible = false;
    state.composing = false;
    $("terminal-draft").value = "";
    state.term?.dispose();
    state.term = null;
    state.fit = null;
    $("terminal-viewport").replaceChildren();
    showTerminalScreen(false);
    updateThemeColor();
    renderTerminalReturn();
    if (wasVisible) {
      navigateView("list", true);
      stopChatPolling();
      setDetailShown(false, { focus: true });
    }
    schedule();
    syncEvents();
  }
  state.closingTerminal = null;
  $("close-terminal-dialog").close();
  toast(closing.copy.done);
}
async function confirmTerminalClose() {
  if (!state.closingTerminal || state.closingTerminal.uncertain || state.terminalCloseBusy) return;
  const closing = state.closingTerminal;
  const authEpoch = state.authEpoch;
  if (state.terminal?.id === closing.id) {
    saveDraft();
    stopTerminalReader();
    state.inputEpoch++;
    terminalStatus(`${closing.copy.action} 요청을 확인하고 있습니다…`);
  }
  state.terminalCloseBusy = true;
  for (const id of ["close-terminal-confirm", "close-terminal-cancel", "close-terminal-close"])
    $(id).disabled = true;
  try {
    const result = await api(`/api/terminal/${encodeURIComponent(closing.id)}/close`, { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    finishTerminalClose(closing);
    updateInputCleanupNotice(result.inputCleanupPending === true);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      closing.uncertain = true;
      $("close-terminal-error").textContent = `${closing.copy.action} 결과를 확인하지 못했습니다. 종료 여부는 목록을 새로고침해 확인하세요. 요청을 자동으로 다시 보내지 않습니다.`;
      // An uncertain close is never resent by this confirmation dialog.
      $("close-terminal-confirm").disabled = true;
    }
  } finally {
    if (authEpoch === state.authEpoch) {
      state.terminalCloseBusy = false;
      $("close-terminal-cancel").disabled = false;
      $("close-terminal-close").disabled = false;
      await refreshTerminals(authEpoch);
    }
  }
}

// Live updates: one EventSource for the board, subscribed to the open
// conversation (its source in the query) and to snapshot changes. While it
// is connected the polling below only runs as a slow safety net; a server
// without /api/events, or 3 errors within 30s, falls back to normal polling
// for a while. Events only say what changed; data is still read online.
const EVENTS_SAFETY_MS = { snapshot: 60000, chat: 30000 };
const EVENTS_RETRY_MS = 5 * 60 * 1000;
function eventsHealthy() {
  return !!state.events?.healthy;
}
function closeEvents() {
  const events = state.events;
  if (!events) return;
  state.events = null;
  try {
    events.source.close();
  } catch {
    /* already closed */
  }
  // Polling returns to its normal pace until a new stream is healthy.
  if (events.healthy) rescheduleAfterEvents();
}
function rescheduleAfterEvents() {
  if (state.pollTimer) schedule();
  const s = selectedSession();
  if (s && state.chatPollTimer) scheduleChatPoll(s, chatEntry(s));
}
function fallbackEvents() {
  state.eventsRetryAt = Date.now() + EVENTS_RETRY_MS;
  closeEvents();
}
function syncEvents() {
  const Source = globalThis.EventSource;
  if (typeof Source !== "function" || !state.authenticated || document.hidden || networkOffline() ||
      state.terminalVisible || Date.now() < state.eventsRetryAt) {
    closeEvents();
    return;
  }
  const s = selectedSession();
  const chat = s && chatVisible() && !claudeWithoutId(s) ? sourceOf(s) : null;
  const key = chat ? chatKey(chat) : "";
  if (state.events?.key === key) return;
  closeEvents();
  const params = new URLSearchParams();
  for (const name of ["host", "agent", "home", "id"]) if (chat?.[name]) params.set(name, chat[name]);
  const query = params.toString();
  let source;
  try {
    source = new Source(`/api/events${query ? `?${query}` : ""}`);
  } catch {
    fallbackEvents();
    return;
  }
  const events = { source, key, healthy: false, everOpen: false, errors: [], digest: null, authEpoch: state.authEpoch };
  state.events = events;
  const current = () => state.events === events && events.authEpoch === state.authEpoch;
  const parse = (event) => {
    try {
      return JSON.parse(event?.data || "{}") || {};
    } catch {
      return {};
    }
  };
  const healthy = () => {
    if (events.healthy) return;
    events.healthy = events.everOpen = true;
    rescheduleAfterEvents();
  };
  source.addEventListener("open", () => { if (current()) healthy(); });
  source.addEventListener("hello", (event) => {
    if (!current()) return;
    healthy();
    const hello = parse(event);
    // A restarted server counts versions from 0 again: forget the old numbers first.
    serverBooted(hello.bootId);
    snapshotAnnounced(hello.snapshotVersion, { immediate: true });
    terminalsAnnounced(hello.terminalsVersion, { hello: true });
  });
  source.addEventListener("snapshot", (event) => {
    if (current()) snapshotAnnounced(parse(event).version);
  });
  source.addEventListener("terminals", (event) => {
    if (current()) terminalsAnnounced(parse(event).version);
  });
  source.addEventListener("chat", (event) => {
    if (!current()) return;
    const digest = parse(event).digest;
    if (typeof digest === "string" && digest && digest === events.digest) return;
    events.digest = typeof digest === "string" ? digest : null;
    const selected = selectedSession();
    if (!selected || !events.key || chatKey(sourceOf(selected)) !== events.key || !chatVisible()) return;
    const entry = chatEntry(selected);
    if (entry.checking) entry.recheckQueued = true;
    else loadChatState(selected, false);
  });
  source.addEventListener("error", () => {
    if (!current()) return;
    const now = Date.now();
    events.errors = events.errors.filter((time) => now - time < 30000).concat(now);
    const wasHealthy = events.healthy;
    events.healthy = false;
    // CLOSED: the browser will not retry (404, 503, a closed server).
    if (source.readyState === 2 || events.errors.length >= 3) fallbackEvents();
    // CONNECTING: the browser reconnects by itself. Either way, the polling
    // timers set for a healthy stream go back to their normal pace now.
    if (wasHealthy) rescheduleAfterEvents();
  });
}
// A resident collector can announce many versions in a burst: they are read
// together, at most once per SNAPSHOT_EVENT_BATCH_MS, and only the snapshot
// (host and account settings come from the regular reads).
const SNAPSHOT_EVENT_BATCH_MS = 300;
function snapshotBehind() {
  return Number.isInteger(state.eventsSnapshotVersion) &&
    !(Number.isInteger(state.snapshotVersion) && state.eventsSnapshotVersion <= state.snapshotVersion);
}
function readAnnouncedSnapshot() {
  if (!state.authenticated || !snapshotBehind()) return;
  if (state.pollBusy) state.snapshotPending = true;
  else poll(false, { capabilities: false });
}
// immediate: the stream's one "hello" after connecting is read at once.
function snapshotAnnounced(version, { immediate = false } = {}) {
  if (!Number.isInteger(version)) return;
  state.eventsSnapshotVersion = Math.max(version, state.eventsSnapshotVersion ?? version);
  if (Number.isInteger(state.snapshotVersion) && version <= state.snapshotVersion) return;
  if (state.snapshotEventTimer) return;
  if (immediate) {
    readAnnouncedSnapshot();
    return;
  }
  const authEpoch = state.authEpoch;
  state.snapshotEventTimer = setTimeout(() => {
    state.snapshotEventTimer = null;
    if (authEpoch === state.authEpoch) readAnnouncedSnapshot();
  }, SNAPSHOT_EVENT_BATCH_MS);
}
function schedule() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (!state.authenticated || networkOffline() || state.terminalVisible || document.hidden) return;
  // Reading a collection already in flight does not start another host request.
  const collecting =
    state.snapshot?.hosts?.some((host) => host.refreshing) &&
    Date.now() < state.refreshDeadline;
  // After a failed read, retry sooner with backoff instead of waiting a full minute.
  const retry = state.pollFailures ? Math.min(POLL_MS, 5000 * 2 ** (state.pollFailures - 1)) : POLL_MS;
  // Live snapshot events replace the 2s collection loop while connected.
  const delay = eventsHealthy() && !state.pollFailures ? EVENTS_SAFETY_MS.snapshot : collecting ? 2000 : retry;
  state.pollTimer = setTimeout(() => poll(!collecting), delay);
}
async function loadCapabilities(authEpoch = state.authEpoch, { refreshView = true } = {}) {
  const accountFilter = state.filters.account;
  const previousRoutes = state.accountFilterRoutes || (accountFilter ? new Set(
    allSessions().filter((s) => s.account === accountFilter).map(accountRouteKey)) : null);
  const capabilities = await api("/api/capabilities", { authEpoch });
  assertAuthEpoch(authEpoch);
  state.capabilities = capabilities;
  state.csrf = state.capabilities.csrfToken || "";
  $("logout").hidden = state.capabilities.authMode === "tailscale";
  if ($("settings-lock")) $("settings-lock").hidden = $("logout").hidden;
  if (accountFilter && state.filters.account === accountFilter && previousRoutes?.size) {
    const labels = new Set(allSessions().filter((s) => previousRoutes.has(accountRouteKey(s))).map((s) => s.account));
    if (labels.size === 1) state.filters.account = [...labels][0];
    state.accountFilterRoutes = previousRoutes;
  }
  if (!refreshView) return;
  render();
  const s = selectedSession();
  if (s && $("route-host")) populateRoute(s);
}
// What /api/capabilities depends on: the server process (bootId; a restarted
// server has a new CSRF token), each host's reachability (a host that recovers
// is read again) and the Codex accounts it lists. A resident collector's new
// snapshot (another fetchedAt) alone never changes it.
function capabilitiesRevision(snapshot) {
  return JSON.stringify([snapshot?.bootId ?? null, (snapshot?.hosts || []).map((host) => [host.name, !!host.ok,
    [...new Set((host.data?.codex || []).map((s) => s.home || ".codex"))].sort()])]);
}
// Snapshot versions count within one server process. Another bootId (a
// restart) forgets the numbers of the previous one, so its first versions are read.
function serverBooted(bootId) {
  if (typeof bootId !== "string" || !bootId || bootId === state.serverBoot) return;
  if (state.serverBoot !== null) {
    state.snapshotVersion = null;
    state.eventsSnapshotVersion = null;
    // The terminal list is read again on the new process's first announcement.
    state.terminalsVersion = null;
    state.eventsTerminalsVersion = null;
  }
  state.serverBoot = bootId;
}
// capabilities: false (a read announced by a snapshot event) reads the
// snapshot only; the next regular read catches the capabilities up.
async function poll(refresh = false, { capabilities = true } = {}) {
  if (networkOffline() || state.loginBusy || state.pollBusy || document.hidden || state.terminalVisible) return;
  const authEpoch = state.authEpoch;
  state.pollBusy = true;
  if (refresh) state.refreshDeadline = Date.now() + 60000;
  clearTimeout(state.pollTimer);
  try {
    const snapshot = await api(`/api/snapshot${refresh ? "?refresh=1" : ""}`, { authEpoch });
    assertAuthEpoch(authEpoch);
    state.snapshot = snapshot;
    serverBooted(snapshot?.bootId);
    if (Number.isInteger(snapshot?.version)) state.snapshotVersion = snapshot.version;
    state.authenticated = true;
    $("login").hidden = true;
    $("app").hidden = false;
    notice();
    render();
    const revision = capabilitiesRevision(state.snapshot);
    const boot = typeof snapshot?.bootId === "string" ? snapshot.bootId : null;
    // Another server process: its CSRF token is read at once, even on a read an
    // event announced (capabilities: false), before any request needs it.
    const restarted = boot !== null && boot !== state.capabilitiesBoot;
    if (!state.capabilities || restarted || (capabilities && state.capabilitiesRevision !== revision)) {
      await loadCapabilities(authEpoch);
      assertAuthEpoch(authEpoch);
      state.capabilitiesRevision = revision;
      state.capabilitiesBoot = boot;
    }
    state.pollFailures = 0;
    renderConnectivity();
    if (Date.now() - (state.usageAt || 0) > 55000) loadUsage();
    // Safety net for missed (or unavailable) terminal announcements: a regular
    // read also reads a terminal list older than a minute. The first list is
    // read by login and startup themselves; when that read failed, this tries again.
    if (capabilities && (state.terminalsReadAt ? Date.now() - state.terminalsReadAt >= POLL_MS : !!state.terminalsError))
      refreshTerminals(authEpoch);
    schedule();
    syncEvents();
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) {
      // A transient failure (server restart, network blip) must not stop the
      // list for good; from the second failure in a row the top shows it.
      state.pollFailures = (state.pollFailures || 0) + 1;
      renderConnectivity();
      if (authEpoch === state.authEpoch) {
        state.pollBusy = false;
        schedule();
      }
      if (!state.snapshot) {
        $("login").hidden = false;
        $("login-error").textContent =
          "서버에 연결할 수 없습니다. 연결을 확인한 뒤 다시 열어 주세요.";
      }
    }
  } finally {
    if (authEpoch === state.authEpoch) {
      state.pollBusy = false;
      // A version announced during this read: read once more if still behind
      // (batched like any other announcement).
      if (state.snapshotPending) {
        state.snapshotPending = false;
        if (snapshotBehind()) snapshotAnnounced(state.eventsSnapshotVersion);
      }
    }
  }
}
async function manualRefresh() {
  if (state.pollBusy || state.launchBusy) return;
  const authEpoch = state.authEpoch;
  $("refresh").disabled = true;
  try {
    if (!state.csrf) await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    state.refreshDeadline = Date.now() + 60000;
    await api("/api/refresh", { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    state.detailUpdatedAt = null;
    await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    await poll(false);
    if (authEpoch === state.authEpoch) refreshTerminals(authEpoch);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) notice(err.message);
  } finally {
    if (authEpoch === state.authEpoch) $("refresh").disabled = false;
  }
}
// Phones (iOS large title): "작업" scrolls with the list. Once it has passed
// under the bar, the bar turns translucent (blurred) and shows the small
// centered title; scrolling back to the top restores the large title.
function setListCollapsed(collapsed) {
  const body = document.body;
  if (state.listCollapsed === collapsed && body?.classList?.contains?.("list-collapsed") === collapsed) return;
  state.listCollapsed = collapsed;
  if (!body?.classList) return;
  if (collapsed) body.classList.add("list-collapsed");
  else body.classList.remove("list-collapsed");
}
function listScroll(pane) {
  setListCollapsed(phoneLayout() && (pane?.scrollTop || 0) > GESTURE.largeTitleCollapsePx);
  if (state.swipeOpen) closeSwipe();
}
function scrollListToTop() {
  const pane = $("task-list-pane");
  if (typeof pane?.scrollTo === "function") pane.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
  else if (pane) pane.scrollTop = 0;
}
// Pull to refresh: the installed app has no browser gesture (overscroll is off),
// so a downward pull from the top of the list or conversation reloads both.
function scrollParent(node) {
  for (let item = node; item && item !== document.body; item = item.parentElement) {
    const overflow = window.getComputedStyle?.(item)?.overflowY || "";
    if (/(auto|scroll)/.test(overflow) && item.scrollHeight > item.clientHeight) return item;
  }
  return null;
}
function pullDistance(pulled) {
  // Elastic: the indicator follows the finger less and less, like a rubber band.
  return GESTURE.pullMaxPx * (1 - Math.exp(-Math.max(0, pulled) / GESTURE.pullResistPx));
}
function showPull(distance, label = "당겨서 새로고침", ready = false) {
  const indicator = $("pull-refresh");
  if (!indicator?.style) return;
  indicator.style.transform = `translate(-50%, ${Math.round(distance - 48)}px)`;
  indicator.style.opacity = distance > 4 ? "1" : "0";
  indicator.classList?.toggle?.("is-ready", ready);
  const icon = indicator.querySelector?.(".pull-refresh-icon");
  if (icon?.style && !indicator.classList?.contains?.("is-loading"))
    icon.style.transform = `rotate(${Math.round(Math.min(1, distance / GESTURE.pullTriggerPx) * 180)}deg)`;
  $("pull-refresh-label").textContent = label;
}
async function pullRefresh() {
  if (state.pullRefreshing) return;
  state.pullRefreshing = true;
  showPull(GESTURE.pullTriggerPx, "새로고침 중…", true);
  $("pull-refresh")?.classList?.add("is-loading");
  let label = "업데이트됨";
  try {
    await manualRefresh();
    const selected = selectedSession();
    if (selected && chatVisible()) await loadChatState(selected, true);
    if (networkOffline()) label = "오프라인입니다";
  } catch {
    label = "새로고침하지 못했습니다";
  } finally {
    $("pull-refresh")?.classList?.remove("is-loading");
    showPull(GESTURE.pullTriggerPx, label, false);
    $("pull-refresh")?.classList?.add("is-done");
    // A short confirmation, then the indicator slides away.
    await new Promise((resolve) => setTimeout(resolve, 700));
    $("pull-refresh")?.classList?.remove("is-done");
    state.pullRefreshing = false;
    showPull(0);
  }
}
function bindPullToRefresh(root) {
  let start = null, startX = 0, distance = 0;
  root.addEventListener("touchstart", (event) => {
    start = null;
    distance = 0;
    if (event.touches.length !== 1 || state.terminalVisible || state.pullRefreshing) return;
    if (event.target?.closest?.("textarea, input, select, dialog[open]")) return;
    // In a conversation the message list decides, wherever the finger starts.
    const scroller = event.target?.closest?.("#detail") ? $("messages") : scrollParent(event.target);
    if (scroller && scroller.scrollTop > 0) return;
    start = event.touches[0].clientY;
    startX = event.touches[0].clientX;
  }, { passive: true });
  root.addEventListener("touchmove", (event) => {
    if (start === null || event.touches.length !== 1) return;
    const pulled = event.touches[0].clientY - start;
    // The pull starts only on a clearly downward drag: a sideways one (a
    // card swipe) is never a pull, and a small wobble waits for direction.
    if (distance === 0) {
      const sideways = Math.abs((event.touches[0].clientX || 0) - startX);
      if (sideways > GESTURE.pressSlopPx && sideways >= Math.abs(pulled)) {
        start = null;
        showPull(0);
        return;
      }
      if (pulled > 0 && pulled < GESTURE.pressSlopPx && sideways > pulled) return;
    }
    if (pulled <= 0) { distance = 0; showPull(0); return; }
    const wasReady = distance >= GESTURE.pullTriggerPx;
    distance = pullDistance(pulled);
    const ready = distance >= GESTURE.pullTriggerPx;
    // A light tick when release would refresh (devices without vibration ignore it).
    if (ready && !wasReady) haptic("select");
    showPull(distance, ready ? "놓으면 새로고침" : "당겨서 새로고침", ready);
    if (event.cancelable) event.preventDefault();
  }, { passive: false });
  const finish = () => {
    if (start !== null && distance >= GESTURE.pullTriggerPx) pullRefresh();
    else if (!state.pullRefreshing) showPull(0);
    start = null;
    distance = 0;
  };
  root.addEventListener("touchend", finish, { passive: true });
  root.addEventListener("touchcancel", () => { start = null; distance = 0; if (!state.pullRefreshing) showPull(0); }, { passive: true });
}
function showLogin() {
  beginAuthEpoch();
  state.loginBusy = false;
  state.launchBusy = false;
  state.terminalCloseBusy = false;
  $("refresh").disabled = false;
  $("running-refresh").disabled = false;
  $("login-submit").disabled = false;
  clearTimeout(state.pollTimer);
  clearTimeout(state.snapshotEventTimer);
  state.snapshotEventTimer = null;
  clearTimeout(state.terminalsEventTimer);
  state.terminalsEventTimer = null;
  state.terminalsVersion = null;
  state.eventsTerminalsVersion = null;
  state.terminalsPending = false;
  state.terminalsReadAt = 0;
  stopTerminalReader();
  stopChatPolling();
  // Object URLs of picked images are released with their drafts.
  for (const entry of state.chats.values()) for (const key of [...(entry.thumbs?.keys() || [])]) revokeThumb(entry, key);
  state.chats.clear();
  state.phaseFilter = "";
  state.swipeOpen = null;
  state.pollFailures = 0;
  state.nativeSessions.clear();
  state.livePhases.clear();
  state.accountFilterRoutes = null;
  state.inputEpoch++;
  state.authenticated = false;
  state.csrf = "";
  state.capabilities = null;
  state.capabilitiesRevision = null;
  state.capabilitiesBoot = null;
  state.refreshDeadline = 0;
  state.snapshot = null;
  state.snapshotVersion = null;
  state.eventsSnapshotVersion = null;
  state.serverBoot = null;
  state.snapshotPending = false;
  state.selected = null;
  navigateView("list", true);
  setDetailShown(false);
  state.detailFor = null;
  state.detailRun++;
  state.planRun++;
  state.plan = null;
  state.drafts.clear();
  state.terminal = null;
  state.terminalVisible = false;
  state.term?.dispose();
  state.term = null;
  state.fit = null;
  state.terminals = [];
  state.terminalsError = "";
  state.compositionOwner = null;
  state.closingTerminal = null;
  if ($("close-terminal-dialog").open) $("close-terminal-dialog").close();
  $("terminal-draft").value = "";
  $("terminal-viewport").replaceChildren();
  $("detail").replaceChildren();
  $("groups").replaceChildren();
  $("terminal-screen").hidden = true;
  $("app").hidden = true;
  $("login").hidden = false;
  if ($("plan-dialog").open) $("plan-dialog").close();
  if ($("settings-dialog").open) $("settings-dialog").close();
  for (const id of ["usage-dialog", "filter-dialog", "terminals-dialog", "action-sheet"]) if ($(id)?.open) $(id).close();
  renderConnectivity();
  updateThemeColor();
  $("login-token").focus();
}
async function lock() {
  if (state.launchBusy) {
    toast("터미널 준비가 끝난 뒤 잠글 수 있습니다.");
    return;
  }
  const authEpoch = state.authEpoch;
  try {
    await api("/logout", { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    showLogin();
    $("login-error").textContent = "";
    toast("잠갔습니다. 실행 중인 작업은 종료하지 않습니다.");
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth)
      toast("잠금 요청을 완료하지 못했습니다. 연결을 확인해 주세요.");
  }
}
function updateViewport() {
  const height = Math.round(
    window.visualViewport?.height || window.innerHeight,
  );
  document.documentElement.style.setProperty("--app-height", `${height}px`);
  syncChromeMode();
  const selected = selectedSession();
  if (selected) resizeChatInput(chatEntry(selected));
  fitTerminal();
}
// "오프라인 · 다시 연결 중" at the top while the browser is offline or the
// board failed to answer twice in a row; it goes away by itself once a read
// succeeds. The terminal shows its own connection state instead.
function renderConnectivity() {
  const box = $("offline-banner");
  if (!box) return;
  const offline = networkOffline();
  const failing = !offline && (state.pollFailures || 0) >= 2;
  const text = !state.authenticated && !state.snapshot ? ""
    : offline ? "오프라인 · 다시 연결 중" : failing ? "연결 끊김 · 다시 연결 중" : "";
  // A connection problem that just started shows a conversation's bars.
  if (text && !$("offline-text").textContent) revealChrome();
  if ($("offline-text").textContent !== text) $("offline-text").textContent = text;
  box.dataset.kind = offline ? "offline" : failing ? "retrying" : "";
  box.hidden = !text || state.terminalVisible;
}
function showOfflineStart() {
  if (state.snapshot || state.terminalVisible) return;
  $("login").hidden = false;
  $("app").hidden = true;
  $("login-error").textContent = "오프라인입니다. 연결이 돌아오면 접속 상태를 다시 확인합니다.";
  $("login-submit").disabled = true;
}
function recoverConnection() {
  state.offline = false;
  renderConnectivity();
  if (networkOffline()) return;
  if (!state.loginBusy) $("login-submit").disabled = false;
  if (document.hidden || state.loginBusy) return;
  if (!state.snapshot) $("login-error").textContent = "";
  if (state.terminalVisible) {
    terminalStatus("네트워크 연결이 돌아왔습니다. 다시 연결하고 있습니다…");
    state.terminalRetry = 0;
    startTerminalReader();
    return;
  }
  const initialAuthentication = !state.authenticated;
  const authEpoch = state.authEpoch;
  poll(false).then(() => {
    if (authEpoch === state.authEpoch && initialAuthentication && state.authenticated && !document.hidden &&
        !state.terminalVisible && !networkOffline()) refreshTerminals(authEpoch);
  });
  const selected = selectedSession();
  if (selected) { updateChatComposer(chatEntry(selected)); loadChatState(selected, true); }
}
function warnDraftUnload(event) {
  if (![...state.drafts.values(), state.terminal ? $("terminal-draft").value : ""].some(Boolean) &&
      ![...state.chats.values()].some((entry) => entry.text || entry.attachments.length || entry.pending || entry.uploads)) return;
  event.preventDefault();
  event.returnValue = "";
}

window.addEventListener("DOMContentLoaded", () => {
  navigateView("list", true);
  document.addEventListener("pointerdown", closeChatToolsOutside);
  document.addEventListener("keydown", documentKeydown);
  window.addEventListener("popstate", (event) => {
    const view = event.state?.[INSTANCE.historyKey] ? event.state.view : "list";
    if (!state.authenticated) {
      navigateView("list", true);
      return;
    }
    if (view === "terminal" && state.terminal) {
      if (!state.terminalVisible)
        openTerminal(state.terminal, { recordHistory: false });
      return;
    }
    // A browser that already animated its own back swipe gets no second slide.
    const animate = !event.hasUAVisualTransition;
    const fromTerminal = state.terminalVisible;
    if (fromTerminal) returnToBoard({ animate });
    const detail = view === "detail" && !!state.selected;
    stopChatPolling();
    setDetailShown(detail, { direction: animate && !fromTerminal ? (detail ? "push" : "pop") : null, focus: true });
    const selected = selectedSession();
    if (selected && chatVisible()) loadChatState(selected, true);
  });
  $("settings").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-done").addEventListener("click", closeSettings);
  $("settings-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeSettings();
  });
  for (const value of THEMES)
    $(`theme-${value}`).addEventListener("change", (event) => {
      if (event?.target?.checked !== false) setTheme(value);
    });
  for (const value of TEXT_SIZES)
    $(`text-size-${value}`).addEventListener("change", (event) => {
      if (event?.target?.checked !== false) setTextSize(value);
    });
  $("settings-lock").addEventListener("click", () => {
    closeSettings();
    lock();
  });
  applyTheme();
  applyTextSize();
  // List chips and their sheets.
  $("usage").addEventListener("click", openUsage);
  $("usage-close").addEventListener("click", () => closeSheet("usage-dialog", "usage"));
  $("filter-open").addEventListener("click", () => sheet("filter-dialog").open());
  $("filter-close").addEventListener("click", () => closeSheet("filter-dialog", "filter-open"));
  $("filter-done").addEventListener("click", () => closeSheet("filter-dialog", "filter-open"));
  $("filter-reset").addEventListener("click", resetFilters);
  $("running-terminals").addEventListener("click", () => sheet("terminals-dialog").open());
  $("terminals-close").addEventListener("click", () => closeSheet("terminals-dialog", "running-terminals"));
  $("action-close").addEventListener("click", () => closeActionSheet());
  for (const [id, close] of [["usage-dialog", () => closeSheet("usage-dialog", "usage")],
    ["filter-dialog", () => closeSheet("filter-dialog", "filter-open")],
    ["terminals-dialog", () => closeSheet("terminals-dialog", "running-terminals")],
    ["action-sheet", () => closeActionSheet()]])
    $(id).addEventListener("cancel", (event) => {
      event.preventDefault();
      close();
    });
  $("nav-title").addEventListener("click", scrollListToTop);
  $("connection-notice").addEventListener("click", () => {
    // Floating on phones; a tap acknowledges it.
    if (phoneLayout()) notice();
  });
  bindCardGestures($("groups"));
  try {
    for (const key of ["selected", "filters", "showOld"])
      localStorage.removeItem(INSTANCE.storagePrefix + key);
  } catch {
    /* remove legacy session metadata where available */
  }
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (networkOffline()) { showOfflineStart(); return; }
    if (state.loginBusy) return;
    let authEpoch = beginAuthEpoch();
    state.loginBusy = true;
    $("login-submit").disabled = true;
    $("login-error").textContent = "";
    try {
      const response = await fetch("/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: $("login-token").value.trim() }),
        cache: "no-store",
      });
      assertAuthEpoch(authEpoch);
      if (!response.ok)
        throw new Error(
          response.status === 403
            ? "접속 토큰이 맞지 않습니다."
            : "접속할 수 없습니다. 서버 연결을 확인해 주세요.",
        );
      authEpoch = beginAuthEpoch();
      state.loginBusy = false;
      $("login-token").value = "";
      await poll(true);
      if (authEpoch === state.authEpoch && state.authenticated) refreshTerminals(authEpoch);
    } catch (err) {
      if (authEpoch === state.authEpoch && !err.staleAuth) $("login-error").textContent = err.message;
    } finally {
      if (authEpoch === state.authEpoch) {
        state.loginBusy = false;
        $("login-submit").disabled = false;
      }
    }
  });
  $("search").addEventListener("input", (event) => {
    state.search = event.target.value.trim();
    render();
  });
  $("show-old").addEventListener("change", (event) => {
    state.showOld = event.target.checked;
    render();
  });
  $("home").addEventListener("click", backToList);
  $("refresh").addEventListener("click", manualRefresh);
  $("running-refresh").addEventListener("click", () => refreshTerminals());
  $("logout").addEventListener("click", lock);
  $("plan-close").addEventListener("click", closePlan);
  $("plan-cancel").addEventListener("click", closePlan);
  $("plan-launch").addEventListener("click", launchPlan);
  $("plan-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closePlan();
  });
  $("terminal-back").addEventListener("click", backFromTerminal);
  $("terminal-close").addEventListener("click", () => {
    if (state.terminal) closeTerminal(state.terminal.id);
  });
  $("close-terminal-confirm").addEventListener("click", confirmTerminalClose);
  $("close-terminal-cancel").addEventListener("click", cancelTerminalClose);
  $("close-terminal-close").addEventListener("click", cancelTerminalClose);
  $("close-terminal-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelTerminalClose();
  });
  $("return-terminal").addEventListener("click", () => {
    if (state.terminal) openTerminal(state.terminal);
  });
  $("terminal-reconnect").addEventListener("click", () => {
    state.lastSize = "";
    startTerminalReader();
  });
  $("terminal-bottom").addEventListener("click", terminalScrollEnd);
  $("terminal-live").addEventListener("click", terminalScrollEnd);
  bindPullToRefresh($("app"));
  $("task-list-pane").addEventListener("scroll", (event) => listScroll(event.currentTarget || $("task-list-pane")), { passive: true });
  bindTerminalTouchScroll($("terminal-viewport"));
  $("terminal-copy").addEventListener("click", async () => {
    const text = state.term?.getSelection();
    if (!text) {
      toast("터미널에서 복사할 텍스트를 먼저 선택해 주세요.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("선택한 텍스트를 복사했습니다.");
    } catch {
      toast("클립보드에 접근할 수 없습니다.");
    }
  });
  const keys = {
    escape: "\x1b",
    tab: "\t",
    backtab: "\x1b[Z",
    up: "\x1b[A",
    down: "\x1b[B",
    left: "\x1b[D",
    right: "\x1b[C",
    interrupt: "\x03",
    "ctrl-j": "\n",
    "ctrl-u": "\x15",
    "ctrl-x": "\x18",
    "ctrl-l": "\x0c",
    enter: "\r",
  };
  for (const node of $("terminal-keys").querySelectorAll("button")) {
    node.addEventListener("pointerdown", (event) => event.preventDefault());
    node.addEventListener("click", () => {
      if (state.composing || state.draftPaste) return;
      state.terminalScrollLines = 0;
      renderTerminalScroll();
      sendInput(keys[node.dataset.key]).catch(() => {});
    });
  }
  $("draft-paste").addEventListener("pointerdown", prepareDraftPaste);
  $("draft-paste").addEventListener("click", () => pasteDraft(true));
  $("draft-type").addEventListener("pointerdown", prepareDraftPaste);
  $("draft-type").addEventListener("click", () => pasteDraft(false));
  bindDraftInput($("terminal-draft"));
  for (const [id, delta] of [
    ["terminal-font-down", -1],
    ["terminal-font-up", 1],
  ])
    $(id).addEventListener("click", () => setTerminalFontSize(state.fontSize + delta));
  document.addEventListener("visibilitychange", () => {
    clearTimeout(state.pollTimer);
    stopChatPolling();
    // The working time stops counting while the page is hidden and catches up when shown.
    if (document.hidden) stopWorkingClock();
    else tickWorking();
    if (document.hidden) {
      // Live updates stop with the page; a visible page reconnects after its read.
      closeEvents();
      if (state.terminalVisible) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        terminalStatus("화면으로 돌아오면 다시 연결합니다.");
      }
    } else if (state.terminalVisible) {
      startTerminalReader();
    } else if (state.authenticated) {
      poll(true);
      const selected = selectedSession();
      if (selected) loadChatState(selected, true);
    } else recoverConnection();
  });
  window.addEventListener("offline", () => {
    state.offline = true;
    closeEvents();
    showOfflineStart();
    stopChatPolling();
    const selected = selectedSession();
    if (selected) updateChatComposer(chatEntry(selected));
    if (state.terminalVisible) {
      stopTerminalReader();
      state.inputEpoch++;
      terminalStatus("네트워크 연결이 끊겼습니다.");
      terminalError(
        "입력은 전송되지 않습니다. 연결이 돌아오면 다시 연결을 눌러 주세요.",
      );
    } else {
      clearTimeout(state.pollTimer);
    }
    renderConnectivity();
  });
  window.addEventListener("online", recoverConnection);
  $("terminal-direct-input").checked = directTerminalInput();
  $("terminal-direct-input").addEventListener("change", (event) => {
    state.directTerminalInput = event.target.checked;
    prefs.set("directTerminalInput", state.directTerminalInput);
    configureTerminalKeyboard();
  });
  $("terminal-accessibility").checked = state.screenReaderMode;
  $("terminal-accessibility").addEventListener("change", (event) => {
    state.screenReaderMode = event.target.checked;
    prefs.set("screenReaderMode", state.screenReaderMode);
    if (state.term) state.term.options.screenReaderMode = state.screenReaderMode;
  });
  window.addEventListener("resize", updateViewport);
  window.addEventListener("beforeunload", warnDraftUnload);
  // Dialog sheets: drag handle, detents and outside taps (BottomSheet).
  for (const id of Object.keys(SHEETS)) sheet(id);
  window.visualViewport?.addEventListener("resize", updateViewport);
  updateViewport();
  if ("serviceWorker" in navigator)
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  if (networkOffline()) {
    showOfflineStart();
  } else {
    const authEpoch = state.authEpoch;
    poll(true).then(() => {
      if (authEpoch === state.authEpoch && state.authenticated) refreshTerminals(authEpoch);
    });
  }
});
