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
    cachePrefix: "sessionholic-",
    attachmentsDir: ".sessionholic-attachments",
  };
  const valid = {
    title: (value) => value.length > 0 && value.length <= 256,
    shortName: (value) => value.length > 0 && value.length <= 256,
    storagePrefix: (value) => /^[A-Za-z0-9_.:-]{1,32}$/.test(value),
    historyKey: (value) => /^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(value),
    cachePrefix: (value) => /^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$/.test(value),
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
// The list reads top to bottom as what needs you, what is running, what just
// stopped (FINISHED_SECONDS) and everything else; each group by title (titleOrder).
const GROUPS = [
  { key: "needs_input", label: "답 필요", tone: "red" },
  { key: "working", label: "작업 중", tone: "amber" },
  { key: "finished", label: "최근 끝남", tone: "green" },
  { key: "rest", label: "이전", tone: "gray" },
];
const FINISHED_SECONDS = 3 * 3600;
// A session put away from the list (ended from the board, or 목록에서 정리) stays away until it
// changes again; changes within this long after are the shutdown's own writes.
const TIDY_GRACE_SECONDS = 120;
function sessionTidied(s) {
  return Number.isInteger(s?.tidiedAt) && (Number(s.updatedAt) || 0) <= s.tidiedAt + TIDY_GRACE_SECONDS;
}
// 목록에서 지우기: unlike a tidy, it stays away after it changes again. This page's own choice
// ({removed, after}) counts at once, and until a snapshot newer than the one shown when the
// board took it (`after`) replaces it; one still in flight (no `after`) counts until it ends.
function sessionRemoved(s) {
  if (!s) return false;
  const local = state.removedLocal?.get(s.key);
  if (local) {
    // The board answers with its version: a snapshot from before it (a read already on its
    // way) does not undo this page's choice; one at or after it says the same or later.
    const read = local.version != null ? (Number(state.snapshot?.version) || 0) >= local.version
      : local.after && state.snapshot !== local.after;
    if (read) state.removedLocal.delete(s.key);
    else return local.removed;
  }
  return Number.isInteger(s.removedAt);
}
function sessionPinned(s) {
  const local = state.pinnedLocal?.get(s?.key);
  if (local && !(local.version != null && Number(state.snapshot?.version) >= local.version)) return local.pinned;
  if (local) state.pinnedLocal.delete(s.key);
  return Number.isInteger(s?.pinnedAt);
}
async function pinSession(s) {
  if (!state.authenticated || state.pinnedLocal?.get(s.key)?.busy) return;
  const mark = { pinned: !sessionPinned(s), busy: true }, epoch = state.authEpoch;
  (state.pinnedLocal ||= new Map()).set(s.key, mark);
  render();
  try {
    const result = await api("/api/session/pin", { method: "POST", body: { source: seenSource(s), pinned: mark.pinned } });
    if (epoch !== state.authEpoch) return;
    mark.version = result.version; mark.busy = false;
    render(); poll(false);
    toast(mark.pinned ? "즐겨찾기에 고정했습니다." : "상단 고정을 해제했습니다.");
  } catch (error) {
    if (epoch !== state.authEpoch) return;
    if (state.pinnedLocal.get(s.key) === mark) state.pinnedLocal.delete(s.key);
    render();
    if (!error.unauthorized && !error.staleAuth) toast(error.message);
  }
}
const BADGES = {
  needs_input: ["답 필요", "red"],
  working: ["작업 중", "amber"],
  idle: ["열림", "gray"],
  done: ["완료", "gray"],
  closed: ["닫힘", "gray"],
  unknown: ["실행 확인 전", "gray"],
};
const AGENTS = { claude: "Claude Code", codex: "Codex" };
const POLL_MS = 60000;
const RECENT_SECONDS = 3 * 24 * 3600;
// Every touch decision reads its thresholds here, so tuning on a real phone
// changes only this object (distances in CSS px, times in ms, speeds in px/ms).
// Conversations open with their bars folded (집중 모드); 집중 모드 해제 or a tap brings them back.
const CONVERSATION_OPENS_FOCUSED = true;
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
  filters: { host: "", agent: "", project: "", account: "", initiator: "" },
  // The status chips above the list ("needs_input" or "working"); tapping the
  // active chip again clears it.
  phaseFilter: "",
  sessionCategory: "general",
  selectedBotRequest: null,
  botRequests: emptyBotRequests(),
  // A bot request's conversation, watch-only: one read per open, kept briefly by request.
  botConversations: new Map(),
  accountFilterRoutes: null,
  search: "",
  showOld: false,
  showRemoved: false,
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
  pollRun: null, // the list read in flight (poll), shared by callers that wait for it
  manualRun: null, // the manual refresh in flight (manualRefresh), shared by a second press or pull
  seenLocal: new Map(), // session key -> update time this page marked read (markSeen)
  seenFailed: new Map(), // session key -> when its last mark failed (markSeen waits SEEN_RETRY_MS)
  removedLocal: new Map(), // session key -> {removed, after} this page chose until a newer snapshot (sessionRemoved)
  pinnedLocal: new Map(),
  openedLocal: new Map(), // session key -> when this page opened its conversation (markOpened)
  openedHeld: null, // {key, at}: the open conversation's place in 이전 while it is shown beside the list
  updateAvailable: false, // the board serves a newer screen than this page runs (checkWebVersion)
  shownAt: Date.now(), // when the page was last shown (visibilitychange)
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
  inputRefused: false, // an input a restarted board refused; cleared by the next one sent
  inputRefusedAt: 0, // the refusal, or the latest key dropped after it (INPUT_REFUSED_PAUSE_MS)
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
  // A wide screen shows the terminal beside the conversation ("나란히"); "크게" covers the board.
  terminalDock: prefs.get("terminalDock", true) !== false,
  docked: false,
  dockSolo: false,
  dockWidth: 0,
  dockSide: "terminal",
  screenReaderMode: prefs.get("screenReaderMode", false) === true,
  directTerminalInput: prefs.get("directTerminalInput", null),
  terminalScrollLines: 0,
  terminalScrollPending: 0,
  terminalScrollTimer: null,
  terminalScrollMode: null,
  terminalScrollInFlight: false,
  terminalScrollGeneration: 0,
  terminalMods: { ctrl: false, alt: false, meta: false },
  // 음성 입력 in progress: { field, recognition, … } (startDictation), at most one.
  dictation: null,
  terminalKeyHandled: false,
  terminalMomentum: null,
  toastTimer: null,
  chats: new Map(),
  draftSavedShapes: new Map(),
  quickRepliesShared: null,
  quickRepliesLocal: null,
  quickRepliesError: "",
  quickRepliesSaving: null,
  quickRepliesEditor: null,
  quickRepliesUnsaved: null,
  historySearch: { mode: false, query: "", run: 0, busy: false, hits: new Map(), errors: new Map(), cursor: null, status: "", controller: null },
  // Reading state stays in this tab. Recoverable drafts are stored separately on this device.
  conversationViews: new Map(),
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
  reopenOnReturn: null, // a session key to open once back from an ended terminal
  // Hidden (or left for the back-forward cache) since last shown. True at first: a page
  // can load hidden (a background tab, a restored session), and nothing reads until shown.
  pageHidden: true,
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
  const forced = boardCovered() ? THEME_COLORS.terminal
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
  if (!shown) rememberConversationView();
  const changed = state.detailShown !== shown;
  state.detailShown = shown;
  const apply = () => {
    if (shown) document.body.classList.add("show-detail");
    else document.body.classList.remove("show-detail");
    // A phone lays the conversation out only now: a scroll set while it was
    // hidden did nothing, so a pinned one is put at its end once it has a size.
    if (shown) {
      const list = $("messages");
      keepAtEnd(list);
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => keepAtEnd(list));
    }
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
// See BottomSheet.unseenPressTaken.
const SHEET_UNSEEN_PRESS_MS = 700;
// A click this soon after the long press's finger lifted is that finger's.
const SHEET_LIFT_CLICK_MS = 300;
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
    this.openedAt = Date.now();
    if (!this.dialog.open) this.dialog.showModal();
  }
  // A tap whose press this sheet never saw: iPadOS keeps a pointerdown for its own text
  // selection (one it made under the finger that opened the menu, or one left on the page).
  // Taken once the finger that opened it is long gone (a click after a hold that long is
  // not that finger's: a long press makes no click).
  unseenPressTaken() {
    return !this.pressedSinceOpen && Date.now() - (this.openedAt || 0) >= SHEET_UNSEEN_PRESS_MS &&
      Date.now() - (state.pressLiftAt || 0) >= SHEET_LIFT_CLICK_MS;
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
    if (event.target !== this.dialog || !this.dialog.open || !(this.pressedSinceOpen || this.unseenPressTaken())) return;
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
  "hardware-dialog": { onDismiss: () => closeSheet("hardware-dialog", "hardware") },
  "filter-dialog": { onDismiss: () => closeSheet("filter-dialog", "filter-open") },
  "terminals-dialog": { onDismiss: () => closeSheet("terminals-dialog", "running-terminals") },
  "new-session-dialog": { onDismiss: () => closeNewSession() },
  "action-sheet": { detents: ["medium"], onDismiss: () => closeActionSheet() },
  "session-control-dialog": { onDismiss: () => closeSessionControl() },
  "session-label-dialog": { onDismiss: () => window.SessionholicLabels?.close?.() },
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
  if ($("board-status")) $("board-status").open = false;
  applyTheme();
  applyTextSize();
  if ($("draft-recovery")) $("draft-recovery").checked = draftRecoveryEnabled();
  if ($("quick-replies")) $("quick-replies").value = state.quickRepliesUnsaved?.text ?? quickReplies().join("\n");
  state.quickRepliesEditor = state.quickRepliesUnsaved ? { value: state.quickRepliesUnsaved.baseValue, revision: state.quickRepliesUnsaved.revision }
    : { value: $("quick-replies")?.value || "", revision: state.quickRepliesShared?.revision ?? null };
  renderQuickReplySettings();
  loadQuickReplyPreferences();
  window.SessionholicPush?.render?.();
  sheet("settings-dialog").open();
}
// A notification tap opens its session: from the page address on a cold start, or
// from the service worker while the board is already open. The session may arrive
// with a later snapshot, so the request waits briefly for it.
function requestedSessionFrom(url) {
  try {
    const parsed = new URL(url, globalThis.location?.origin || "http://board.invalid");
    const key = parsed.pathname === "/" ? parsed.searchParams.get("session") : null;
    return key && key.length <= 600 ? key : null;
  } catch {
    return null;
  }
}
function requestSessionOpen(url) {
  const key = requestedSessionFrom(url);
  if (!key) return false;
  state.openRequest = { key, until: Date.now() + 120000 };
  openRequestedSession();
  return true;
}
function openRequestedSession() {
  const request = state.openRequest;
  if (!request) return;
  if (Date.now() > request.until) { state.openRequest = null; return; }
  if (!state.authenticated || state.launchBusy || !allSessions().some((session) => session.key === request.key)) return;
  state.openRequest = null;
  // Over an open terminal too (or beside one of another session): the conversation
  // shows, and back returns to the terminal.
  if (state.terminalVisible && (boardCovered() || sessionForTerminal(state.terminal)?.key !== request.key)) {
    returnToBoard({ selectLaunchedSource: false, animate: false });
    navigateView("detail", false, { notice: request.key });
  }
  const target = allSessions().find((session) => session.key === request.key);
  if (target && groupOf(target) === "needs_input") answerOnOpen(target);
  select(request.key);
}
// Opened from the 답 필요 group or its notification: the question is loaded once,
// as "질문 불러오기" would, so the choices are there without another tap.
function answerOnOpen(s) {
  state.answerOnOpen = { key: s.key, until: Date.now() + 30000 };
}
// 상단 / 설정 › 캐시 지우고 새로고침: this board's shell caches go, and a worker that receives no
// notifications is unregistered (one that does keeps its subscription and only updates).
// Other applications' caches and every stored preference stay.
async function hardReload({ discard = false, preserveDrafts = false } = {}) {
  if (state.hardReloadBusy) return;
  if ($("board-status")) $("board-status").open = false;
  if (networkOffline()) {
    // A cleared shell could not load again until the board is reachable.
    toast("오프라인이라 캐시를 지우지 않았습니다. 연결된 뒤 다시 눌러 주세요.");
    return;
  }
  if (!discard && unsavedWork()) {
    const recoverable = persistAllDrafts();
    openActionSheet({ title: "새로고침할까요?", preview: recoverable
      ? "초안과 업로드가 끝난 첨부를 이 기기에 보관하고 최신 화면을 불러옵니다. 전송 확인 중인 메시지는 다시 보내지 않습니다."
      : "보내지 않은 초안·첨부와 아직 확인 중인 입력이 있습니다. 초안을 보관하지 못했거나 업로드가 진행 중입니다. 새로고침하면 복원하지 못한 내용은 사라집니다.",
      actions: [{ label: "돌아가기", run: () => {} }, { label: recoverable ? "초안 보관 후 새로고침" : "초안을 버리고 새로고침",
        danger: !recoverable, run: () => hardReload({ discard: true, preserveDrafts: recoverable }) }],
      trigger: document.activeElement });
    return;
  }
  state.hardReloadBusy = true;
  const buttonIds = ["hard-reload", "quick-reload", "detail-reload"];
  const buttons = buttonIds.map($).filter(Boolean);
  for (const button of buttons) button.disabled = true;
  try {
    const keys = (await globalThis.caches?.keys?.()) || [];
    await Promise.all(keys.filter((key) => typeof key === "string" && key.startsWith(INSTANCE.cachePrefix))
      .map((key) => globalThis.caches.delete(key)));
    const registrations = (await globalThis.navigator?.serviceWorker?.getRegistrations?.()) || [];
    for (const registration of registrations) {
      const subscribed = await Promise.resolve(registration.pushManager?.getSubscription?.()).catch(() => null);
      if (subscribed) await Promise.resolve(registration.update?.()).catch(() => {});
      else await Promise.resolve(registration.unregister?.()).catch(() => {});
    }
  } catch {
    // Reload anyway: the fresh page reads everything from the board again.
  }
  state.discardOnReload = discard;
  if (discard && !preserveDrafts) clearSavedDrafts();
  // Still here (a draft's leave prompt was declined): the button works again.
  setTimeout(() => {
    state.hardReloadBusy = false;
    state.discardOnReload = false;
    for (const id of buttonIds) { const button = $(id); if (button) button.disabled = false; }
  }, 3000);
  window.location.reload();
}
async function closeSettings() {
  if (!$("settings-dialog").open) return;
  // A phrase list edited without leaving the field is kept too.
  const quick = $("quick-replies");
  if (quick && quick.value !== quickReplies().join("\n") && !await saveSharedQuickReplies()) {
    rememberQuickReplyEdits();
    toast("저장하지 못한 문구 편집을 이 화면에 보관했습니다. 설정에서 다시 저장할 수 있습니다.");
  }
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
  state.pollRun = null;
  state.manualRun = null;
  state.terminalsRefreshing = false;
  return state.authEpoch;
}
// After a server restart the CSRF token held here is stale (403 csrf_expired),
// and the server refused before doing anything. Reads, and requests it
// deduplicates by requestId, are sent once more with a fresh token. A chat send
// never is: it waits for a new press (sendChat).
const CSRF_RETRY = new Set(["/api/chat/state", "/api/chat/prompt", "/api/plan", "/api/launch",
  "/api/new-session", "/api/app-server", "/api/refresh", "/api/usage/refresh", "/api/hardware/refresh",
  "/api/push/subscribe", "/api/push/unsubscribe", "/api/session/seen", "/api/session/tidy", "/api/session/remove",
  "/api/session/opened", "/api/chat/warm", "/api/diagnostics/dictation"]);
// A terminal's size and scrolling; its keys never (sendInput reads the token instead).
const CSRF_RETRY_TERMINAL = /^\/api\/terminal\/[a-f0-9]{32}\/(resize|scroll|scroll-end)$/;
function csrfRetryable(path) {
  return CSRF_RETRY.has(path) || CSRF_RETRY_TERMINAL.test(path);
}
// The screen this page runs: its app.js ?v=, as the board served it. A tab opened
// before a deploy keeps the old screen until it reloads; the board names the one it
// serves now (capabilities.webVersion) and checkWebVersion compares the two.
const APP_VERSION = (() => {
  try { return new URL(document.currentScript?.src || "").searchParams.get("v") || ""; } catch { return ""; }
})();
// Back on the page this recently, a newer screen loads at once when nothing would be lost.
const UPDATE_RETURN_MS = 15000;
// How long a request waits for its answer. A phone back from sleep or between
// networks can hold one open forever, and a held read blocks every later one
// (poll, refresh, chat state); one that runs out fails like a lost connection.
// Paths the server answers only after remote work (a send, a launch) wait longer.
const API_TIMEOUT_MS = { GET: 30000, POST: 45000 };
// Each is longer than the server's own bound for that route.
const API_PATH_TIMEOUT_MS = {
  "/api/capabilities": 90000, "/api/read": 45000, "/api/bot-requests/conversation": 90000, "/api/chat/send": 90000, "/api/chat/prompt": 60000,
  "/api/plan": 120000, "/api/new-session": 120000, "/api/app-server": 200000, "/api/launch": 660000,
};
const API_TIMEOUT_MESSAGE = "응답이 늦어 기다리기를 멈췄습니다. 연결을 확인한 뒤 다시 시도해 주세요.";
// Where the server may already have acted: say so instead of inviting a blind retry.
const API_TIMEOUT_STARTED = "응답이 늦어 기다리기를 멈췄습니다. 이미 시작됐거나 전달됐을 수 있으니 목록과 터미널을 확인한 뒤 다시 시도해 주세요.";
const API_STARTED_PATHS = new Set(["/api/launch", "/api/new-session", "/api/app-server", "/api/chat/prompt"]);
function apiPathname(path) {
  return String(path).split("?")[0];
}
function apiTimeout(path, method) {
  return API_PATH_TIMEOUT_MS[apiPathname(path)] ?? API_TIMEOUT_MS[method] ?? API_TIMEOUT_MS.POST;
}
// The caller's signal and the time limit, as one. A browser without
// AbortSignal.timeout keeps the old behaviour (no limit).
function apiSignal(signal, timeout) {
  const limit = timeout > 0 && typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
    ? AbortSignal.timeout(timeout) : null;
  if (!limit) return { signal, limit: null };
  if (!signal) return { signal: limit, limit };
  const controller = new AbortController();
  const abort = () => controller.abort();
  for (const source of [signal, limit]) {
    if (source.aborted) abort();
    else source.addEventListener("abort", abort, { once: true });
  }
  return { signal: controller.signal, limit };
}
function apiTimedOut(limit, path = "") {
  if (!limit?.aborted) return null;
  const err = new Error(API_STARTED_PATHS.has(apiPathname(path)) ? API_TIMEOUT_STARTED : API_TIMEOUT_MESSAGE);
  err.timeout = true;
  return err;
}
async function api(path, { method = "GET", body, signal: callerSignal, authEpoch = state.authEpoch, csrfRetry = true,
  timeout = apiTimeout(path, method) } = {}) {
  assertAuthEpoch(authEpoch);
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  const { signal, limit } = apiSignal(callerSignal, timeout);
  let response;
  try {
    response = await fetch(path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin", cache: "no-store", signal,
    });
  } catch (err) { assertAuthEpoch(authEpoch); throw apiTimedOut(limit, path) || err; }
  assertAuthEpoch(authEpoch);
  if (response.status === 401) {
    showLogin();
    const err = new Error("로그인이 필요합니다.");
    err.unauthorized = true;
    throw err;
  }
  const data = await response.json().catch(() => ({}));
  assertAuthEpoch(authEpoch);
  // A body cut off by the limit is not an empty answer.
  const late = apiTimedOut(limit, path);
  if (late) throw late;
  if (!response.ok && response.status === 403 && data.code === "csrf_expired" && csrfRetry &&
      method !== "GET" && csrfRetryable(path)) {
    let refreshed = false;
    try {
      // After a restart every request in flight is refused at once: one refresh serves them all.
      state.csrfRefresh ||= loadCapabilities(authEpoch, { refreshView: false }).finally(() => { state.csrfRefresh = null; });
      await state.csrfRefresh;
      refreshed = true;
    } catch (err) {
      if (err.unauthorized || err.staleAuth) throw err;
    }
    assertAuthEpoch(authEpoch);
    if (refreshed) return api(path, { method, body, signal: callerSignal, authEpoch, csrfRetry: false, timeout });
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

function labeledSession(session) {
  const value = window.SessionholicLabels?.decorate?.(session, state.snapshot?.sessionLabels) || session;
  return { ...value, originalTitle: typeof value.originalTitle === "string" ? value.originalTitle : value.title,
    title: typeof value.displayTitle === "string" && value.displayTitle ? value.displayTitle : value.title };
}
function allSessions() {
  const out = [];
  // The open conversation is read again every few seconds; its own read stands until then.
  const following = chatVisible() ? state.selected : null;
  for (const host of state.snapshot?.hosts || []) {
    for (const s of [
      ...(host.data?.claude || []),
      ...(host.data?.codex || []),
    ]) {
      // A Claude row listed without an id is told apart by its process (or folder and start).
      const key = [host.name, s.agent, s.home || "", s.id || (s.agent === "claude"
        ? `~${s.pid ?? ""}:${s.sessionId || ""}:${s.cwd || ""}:${s.startedAt ?? ""}` : "")].join("|");
      // The list's own phase stands again once it changed since the conversation was read,
      // or, for a conversation no longer open, once the host was read again after it (a quick
      // turn may end where it began).
      let live = state.livePhases.get(key);
      if (live && (live.base !== s.phase || (key !== following && (host.fetchedAt ?? 0) > (live.fetchedAt ?? 0)))) {
        state.livePhases.delete(key);
        live = null;
      }
      out.push(labeledSession({
        ...s,
        phase: s.runtimeKnown === false ? "unknown" : live && live.base === s.phase ? live.phase : s.phase,
        listedPhase: s.phase,
        host: host.name,
        account: sessionAccountLabel({ ...s, host: host.name }),
        hostLabel: host.label,
        stale: !host.ok,
        fetchedAt: host.fetchedAt,
        key,
      }));
    }
  }
  for (const session of state.nativeSessions.values()) {
    if (!out.some((s) => s.key === session.key)) out.push(labeledSession({ ...session, account: sessionAccountLabel(session) }));
  }
  return out;
}
// The open conversation's own read says what its session does now; the list
// may say so only a minute later (a host without a resident collector). Its
// card, header and counts follow the read until the list's phase changes.
function noteLivePhase(s, data) {
  const listed = allSessions().find((value) => value.key === s?.key);
  if (!listed || listed.runtimeKnown === false || listed.listedPhase === undefined && listed.nativeSource || !BADGES[data?.phase] ||
      chatKey(data.route || {}) !== chatKey(sourceOf(listed))) return;
  if (data.phase === listed.listedPhase) state.livePhases.delete(listed.key);
  else state.livePhases.set(listed.key, { phase: data.phase, base: listed.listedPhase, fetchedAt: listed.fetchedAt });
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
function groupOf(s, now = state.snapshot?.serverTime || Date.now() / 1000) {
  if (s.phase === "needs_input") return "needs_input";
  if (s.phase === "working") return "working";
  if (sessionTidied(s) || sessionRemoved(s)) return "rest";
  if (["idle", "done", "closed"].includes(s.phase) && now - (s.updatedAt || 0) <= FINISHED_SECONDS) return "finished";
  return "rest";
}
function matches(s, now, { phase = true, category = true, initiator = true } = {}) {
  // A bot picked in 시작 주체 lists the sessions it opened (its requests' own too) in any tab.
  const bot = botFilter();
  if (!bot && botRequestForNative(sourceOf(s))) return false;
  if (category && !bot && sessionCategoryOf(s) !== state.sessionCategory) return false;
  if (phase && state.phaseFilter && groupOf(s, now) !== state.phaseFilter) return false;
  for (const key of Object.keys(state.filters)) {
    if (key === "initiator") {
      if (initiator && state.filters.initiator && (bot ? botNameOf(s) !== bot : initiatorOf(s) !== state.filters.initiator)) return false;
      continue;
    }
    if (key === "account" && state.filters.account && state.accountFilterRoutes) {
      if (!state.accountFilterRoutes.has(accountRouteKey(s))) return false;
      continue;
    }
    if (state.filters[key] && s[key] !== state.filters[key]) return false;
  }
  // An explicit removal takes precedence over status, search and the age filter.
  const removed = sessionRemoved(s);
  if (removed && !state.showRemoved) return false;
  if (
    !state.showOld && !removed && !sessionPinned(s) &&
    groupOf(s, now) === "rest" &&
    (now - (s.updatedAt || 0) > RECENT_SECONDS || (sessionTidied(s) && !state.search))
  )
    return false;
  if (!state.search) return true;
  const q = state.search.toLocaleLowerCase();
  if (window.SessionholicGoals?.sessionMatches?.(s, q)) return true;
  return [
    s.title,
    s.originalTitle,
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

function renderSessionList(sessions, now) {
  setClass($("task-list-pane"), "is-bot-category", state.sessionCategory === "bot");
  if (state.sessionCategory === "bot") { renderSessionCategories(sessions, now); renderBotRequests(); return; }
  // Updating list conditions must not redraw a retained conversation or reset
  // its scroll anchor when the same source is opened again from a work card.
  const base = sessions.filter((session) => matches(session, now, { phase: false }));
  const shown = state.phaseFilter ? base.filter((session) => groupOf(session, now) === state.phaseFilter) : base;
  renderCounts(base); renderSessionCategories(sessions, now);
  renderFilters(sessions.filter((session) => sessionCategoryOf(session) === state.sessionCategory));
  renderGroups(shown, now);
  $("session-count").textContent = `${shown.length}개`;
}
function render() {
  if (!state.snapshot) return;
  const now = state.snapshot.serverTime || Date.now() / 1000;
  const sessions = allSessions();
  const reading = state.detailShown && sessions.find((session) => session.key === state.selected);
  if (reading && !botFilter() && sessionCategoryOf(reading) !== state.sessionCategory) setSessionCategory(sessionCategoryOf(reading), { renderView: false, clearSelection: false });
  renderHosts(now); renderFreshness(now); renderAttention(sessions);
  // The conversation on screen is read, also when it answers or finishes while shown
  // (before the list is drawn, so its card never flashes a dot).
  const shownSession = chatVisible() && sessions.find((s) => s.key === state.selected);
  if (shownSession && sessionUnread(shownSession, now)) markSeen(shownSession);
  renderSessionList(sessions, now);
  renderHistorySearch();
  if (state.sessionCategory === "bot" && !sessions.some((s) => s.key === state.selected)) { window.SessionholicGoals?.update?.(sessions); return; }
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
  window.SessionholicGoals?.update?.(sessions);
  // Its conversation listed now: it takes its place beside the docked terminal.
  if (!selected && state.docked && state.terminal && !state.dockBeside && dockedSessionFor(state.terminal)) {
    syncBeside();
    return;
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
// A lost connection comes first: on a phone the chips scroll sideways and only the
// first fit, so a device that is down is never the one pushed out of sight. A tap
// says what each one's dot means (a touch has no hover title).
// A device that answers but warns of something to check (a PC project server that is
// off, data.warnings) is "warn". The order follows what was last known (a lost device,
// one with a warning, the rest as configured): a collection in progress only pulses the
// dot, so chips never trade places every minute.
const HOST_ORDER = { bad: 0, warn: 1, ok: 2 };
function hostNotes(host, key) {
  return (Array.isArray(host.data?.[key]) ? host.data[key] : []).filter((text) => typeof text === "string" && text);
}
function hostRank(host) {
  return !host.ok ? "bad" : hostNotes(host, "warnings").length ? "warn" : "ok";
}
function hostTone(host) {
  return host.refreshing ? "checking" : hostRank(host);
}
function renderHosts(now) {
  const box = $("hosts");
  // Rebuilt on every snapshot: a chip that had the keyboard keeps it.
  const focused = box.contains?.(document.activeElement) ? document.activeElement.dataset?.host : null;
  box.replaceChildren();
  const all = [...(state.snapshot?.hosts || [])];
  for (const host of state.hardware?.hosts || []) {
    if (!host?.name || all.some((item) => item.name === host.name)) continue;
    all.push({ name: host.name, label: host.label || host.name, monitorOnly: true,
      ok: host.status === "ready", refreshing: host.status === "pending" && !!host.refreshing,
      fetchedAt: host.fetchedAt, error: host.status === "stale" ? "이전 기기 정보 · 연결 확인 필요"
        : host.status === "pending" ? "연결 확인 전" : host.error || "연결 안 됨" });
  }
  const hosts = all.map((host, index) => ({ host, index }))
    .sort((a, b) => HOST_ORDER[hostRank(a.host)] - HOST_ORDER[hostRank(b.host)] || a.index - b.index);
  const connected = all.filter((host) => host.ok).length;
  $("board-status-summary").textContent = `기기 ${all.length}`;
  $("board-status-counts").textContent = `연결 ${connected}/${all.length}`;
  const summary = $("board-status")?.querySelector?.("summary");
  summary?.setAttribute("aria-label", `기기 ${all.length}대, 연결 ${connected}대 · 기기 연결과 사용량 보기`);
  if (summary) {
    summary.title = `기기 ${all.length}대 · 연결 ${connected}대`;
    summary.dataset.tone = !all.length ? "unknown" : connected === all.length ? "ready" : connected ? "partial" : "offline";
  }
  for (const { host } of hosts) {
    const chip = el("button", `host ${hostTone(host)}`);
    chip.type = "button";
    chip.dataset.host = host.name;
    chip.append(el("span", "dot"), el("span", "host-name", host.label),
      el("span", "host-connection", host.refreshing ? "확인 중" : host.ok ? "연결됨" : "연결 안 됨"));
    const notes = [...hostNotes(host, "warnings"), ...hostNotes(host, "errors")];
    const problems = notes.length ? ` · ${notes[0]}${notes.length > 1 ? ` 외 ${notes.length - 1}건` : ""}` : "";
    chip.title = host.refreshing
      ? `${host.label} · ${host.monitorOnly ? "기기" : "작업"} 확인 중`
      : host.ok
        ? `${host.label} · ${host.monitorOnly ? "기기 연결됨 · " : ""}${ago(host.fetchedAt, now)} 확인${problems}`
        : `${host.label} · ${host.error || "연결 확인 필요"}${host.fetchedAt ? ` · 마지막 확인 ${ago(host.fetchedAt, now)}` : ""}`;
    chip.setAttribute("aria-label", chip.title);
    chip.addEventListener("click", () => toast(chip.title));
    box.append(chip);
    if (focused && host.name === focused) chip.focus?.({ preventScroll: true });
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
// The board machines' hardware (설정 `hardware`): a chip with how many answer and a sheet
// with each one's CPU, memory, disk, battery and uptime. Read every 15 seconds while the
// sheet is open, the chip's summary every 5 minutes otherwise.
const HARDWARE_OPEN_MS = 15000;
const HARDWARE_CLOSED_MS = 300000;
function hardwareTone(percent) {
  return !Number.isFinite(percent) ? "muted" : percent >= 90 ? "red" : percent >= 75 ? "orange" : "green";
}
function hardwareBytes(value) {
  if (!Number.isFinite(value)) return "—";
  const gb = value / 1024 ** 3;
  return gb >= 1000 ? `${(gb / 1024).toFixed(1)}TB` : gb >= 10 ? `${Math.round(gb)}GB` : `${gb.toFixed(1)}GB`;
}
function hardwareUptime(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const days = Math.floor(seconds / 86400), hours = Math.floor((seconds % 86400) / 3600);
  return days ? `${days}일 ${hours}시간` : hours ? `${hours}시간 ${Math.floor((seconds % 3600) / 60)}분` : `${Math.floor(seconds / 60)}분`;
}
function hardwareLine(label, percent, value) {
  const line = el("div", "usage-window");
  const bar = el("div", "usage-bar");
  const fill = el("span", `usage-fill ${hardwareTone(percent)}`);
  if (fill.style) fill.style.width = `${Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0}%`;
  bar.append(fill);
  line.append(el("span", "usage-window-label", label), bar, el("span", "usage-window-value", value), el("span", "usage-window-reset", ""));
  return line;
}
function renderHardware(data) {
  const box = $("hardware");
  if (!box) return;
  const hosts = data?.available && Array.isArray(data.hosts) ? data.hosts.filter((item) => item && typeof item === "object") : [];
  if (!hosts.length) {
    box.hidden = true;
    box.dataset.revision = "";
    if ($("hardware-dialog")?.open) closeSheet("hardware-dialog", "hardware");
    return;
  }
  box.hidden = false;
  const revision = JSON.stringify([hosts, data.refreshing]);
  if (box.dataset.revision === revision) return;
  box.dataset.revision = revision;
  const answering = hosts.filter((host) => host.status === "ready" || host.status === "stale").length;
  const hot = hosts.filter((host) => [host.cpu?.percent, host.memory?.percent, host.disk?.percent]
    .some((value) => Number.isFinite(value) && value >= 90)).length;
  const pill = el("span", `usage-pill ${answering < hosts.length || hot ? "red" : "green"}`);
  pill.append(el("span", "usage-short", "기기"), el("strong", null, `${answering}/${hosts.length}`));
  $("hardware-summary").replaceChildren(pill);
  box.setAttribute("aria-label", `기기 상태: ${hosts.length}대 중 ${answering}대 응답${hot ? `, ${hot}대 사용률 높음` : ""}. 기기별로 보기`);
  const sections = hosts.map((host) => {
    const section = el("section", "usage-provider hardware-host");
    const head = el("div", "usage-row-head");
    head.append(el("strong", null, host.label || host.name));
    const status = host.status === "offline" ? "연결 안 됨" : host.status === "stale" ? "이전 값" :
      host.status === "pending" ? (host.refreshing ? "읽는 중" : "확인 전") : host.refreshing ? "갱신 중" : "";
    if (status) head.append(el("span", "usage-credits", status));
    section.append(head);
    if (host.error) section.append(el("p", "usage-note", host.error));
    const cpu = host.cpu || {};
    if (host.cpu) {
      const cores = Number.isFinite(cpu.cores) ? `${cpu.cores}코어` : "";
      const load = Array.isArray(cpu.load) ? `부하 ${cpu.load[0]}` : "";
      section.append(hardwareLine("CPU", cpu.percent, Number.isFinite(cpu.percent) ? `${cpu.percent}%` : load || "측정 중"));
      const detail = [cpu.model, cores, Number.isFinite(cpu.percent) ? load : ""].filter(Boolean).join(" · ");
      if (detail) section.append(el("p", "usage-note", detail));
    }
    if (host.memory) section.append(hardwareLine("메모리", host.memory.percent,
      `${hardwareBytes(host.memory.used)} / ${hardwareBytes(host.memory.total)}`));
    if (host.disk) section.append(hardwareLine("디스크", host.disk.percent, `여유 ${hardwareBytes(host.disk.free)}`));
    const facts = [];
    if (host.battery) facts.push(`배터리 ${host.battery.percent}%${host.battery.charging ? " · 충전 중" : host.battery.power === "ac" ? " · 전원 연결" : ""}`);
    if (Number.isFinite(host.uptimeSeconds)) facts.push(`가동 ${hardwareUptime(host.uptimeSeconds)}`);
    if (Number.isFinite(host.fetchedAt)) facts.push(`${clock(host.fetchedAt)} 확인`);
    if (facts.length) section.append(el("p", "usage-note", facts.join(" · ")));
    return section;
  });
  const foot = el("div", "usage-foot");
  foot.append(el("span", null, "CPU는 두 번 읽은 사이의 사용률입니다. 맥은 부하(1분 평균)도 함께 보여 줍니다."));
  const refresh = button(data.refreshing ? "읽는 중…" : "새로고침", "usage-refresh", () => loadHardware(true));
  refresh.disabled = !!data.refreshing;
  foot.append(refresh);
  $("hardware-body").replaceChildren(...sections, foot);
}
function openHardware() {
  if ($("hardware")?.hidden) return;
  sheet("hardware-dialog").open();
  loadHardware();
  clearInterval(state.hardwareTimer);
  state.hardwareTimer = setInterval(() => {
    if (!$("hardware-dialog")?.open) { clearInterval(state.hardwareTimer); state.hardwareTimer = null; return; }
    loadHardware();
  }, HARDWARE_OPEN_MS);
}
async function loadHardware(force = false) {
  if (state.hardwareUnsupported || !state.authenticated || networkOffline() || document.hidden || state.hardwareBusy) return;
  state.hardwareBusy = true;
  state.hardwareAt = Date.now();
  try {
    const data = force ? await api("/api/hardware/refresh", { method: "POST", body: {} }) : await api("/api/hardware");
    state.hardware = data;
    renderHardware(data);
    renderHosts(state.snapshot?.serverTime || Date.now() / 1000);
    // Follow the reads the board is making, a few times.
    if (data?.refreshing && (state.hardwareFollow = (state.hardwareFollow || 0) + 1) <= 10)
      setTimeout(() => loadHardware(false), 3000);
    else if (!data?.refreshing) state.hardwareFollow = 0;
  } catch (err) {
    if (err.status === 404) { state.hardwareUnsupported = true; renderHardware(null); }
  } finally {
    state.hardwareBusy = false;
  }
}
const BASE_TITLE = INSTANCE.title;
function renderAttention(sessions) {
  // The tab title and app icon say when work waits for an answer while this view is elsewhere.
  const waiting = sessions.filter((s) => !botRequestForNative(sourceOf(s)) && groupOf(s) === "needs_input").length;
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
const INITIATOR_LABELS = { human: "직접 시작", agent: "에이전트·프로그램 시작", unknown: "출처 미확인" };
// Codex sessions opened by hand: an interactive source (its terminal UI, an app's or IDE's
// connection) or, with none recorded, its terminal UI or a board launch as originator.
const DIRECT_ORIGINATORS = new Set(["codex-tui", "session-board"]);
const DIRECT_CODEX_SOURCES = new Set(["cli", "vscode"]);
// Folders bots work in (a Slack bot's workspace, Hermes' home): their sessions are the bot's.
const BOT_FOLDERS = [[/[\\/]workspaces[\\/]slack(?:[\\/]|$)/i, "Slack"], [/[\\/]\.hermes(?:[\\/]|$)/i, "헤르메스"]];
function botFolderOf(s) {
  const cwd = String(s?.cwd || "");
  return BOT_FOLDERS.find(([folder]) => folder.test(cwd))?.[1] || null;
}
// Who opened a session, from what the board sees of it: "bot" (a bot request's, or a bot's own),
// "agent" (a subagent, or a program's run such as codex exec), "human" (opened by hand in a
// terminal: Codex's terminal UI or a board launch, Claude's interactive or background session)
// or "unknown". 일반 세션 lists only "human"; 에이전트 세션 the agents', programs' and unknown.
function originOf(s) {
  if (!s) return "unknown";
  if (botNameOf(s)) return "bot";
  if (s.initiator === "agent" || s.parentSessionId || s.agentRole) return "agent";
  if (s.initiator === "human") return "human";
  if (s.agent === "codex") {
    // How Codex recorded the run decides: its terminal UI or an app's or IDE's connection is
    // opened by hand, exec is a program's run. The originator names the client that last
    // attached (the board's own reader, say), so it decides only with no source recorded.
    if (DIRECT_CODEX_SOURCES.has(s.source)) return "human";
    if (s.source === "exec") return "agent";
    if (s.originator) return DIRECT_ORIGINATORS.has(s.originator) ? "human" : "agent";
    return "unknown";
  }
  if (s.agent === "claude" && ["interactive", "background"].includes(s.kind)) return "human";
  return "unknown";
}
// 시작 주체's own values (a bot is picked by its name instead).
function initiatorOf(s) {
  const origin = originOf(s);
  return origin === "bot" ? "unknown" : origin;
}
function initiatorLabel(s) {
  const bot = botNameOf(s);
  if (bot) return `${bot} 시작`;
  return INITIATOR_LABELS[initiatorOf(s)] + (s?.initiatorName ? ` · ${s.initiatorName}` : "");
}
// The bot that opened a session: a bot request's source by its name ("Name · Slack" is Name)
// or Codex's originator (헤르메스). 시작 주체 can pick one (value BOT_FILTER_PREFIX + name).
const BOT_FILTER_PREFIX = "bot:";
const ORIGINATOR_BOTS = { hermes: "헤르메스" };
function botNameOf(s) {
  const request = s && state.botRequests?.requests ? botRequestForNative(sourceOf(s)) : null;
  if (request) {
    const source = (state.botRequests.sources || []).find((item) => item.id === request.sourceId);
    return String(source?.label || request.botName || "").split(" · ")[0].trim() || null;
  }
  return ORIGINATOR_BOTS[s?.originator] || botFolderOf(s) || null;
}
function botFilter() {
  const value = state.filters?.initiator || "";
  return value.startsWith(BOT_FILTER_PREFIX) ? value.slice(BOT_FILTER_PREFIX.length) : "";
}
const ONE_SHOT_NO_INPUT = "일회성 실행에는 후속 메시지를 보낼 수 없습니다. 결과는 요청한 에이전트에서 확인해 주세요.";
const ONE_SHOT_HISTORY = "대화 기록을 저장하지 않는 일회성 실행입니다. 실행 상태를 확인하고 결과는 요청한 에이전트에서 확인해 주세요.";
function sessionOneShot(s) { return s?.executionMode === "one-shot"; }
const BOT_CACHE_MS = 120000;
const BOT_STATUS = { pending: "대기", queued: "대기", waiting: "대기", running: "진행 중", working: "진행 중", completed: "완료", done: "완료", failed: "실패", cancelled: "취소", canceled: "취소", stopped: "중단", closed: "종료", active: "진행 중", needs_input: "답변 필요", error: "실패" };
function emptyBotRequests() { return { requests: [], sources: [], loaded: false, busy: false, error: "", readAt: 0, fetchedAt: null, promise: null }; }
function botTimestamp(value) { return typeof value === "number" ? value * 1000 : Date.parse(value) || 0; }
function botRequestKey(request) { return JSON.stringify([request.sourceId, request.id]); }
function botLabel(value) { return typeof value === "string" && value.trim() && !["unknown", "unconfirmed"].includes(value.trim().toLowerCase()) ? value.trim() : "미확인"; }
function botRequestStale(request) { return !!(request.stale || state.botRequests.error || state.botRequests.sources.some((source) => source.id === request.sourceId && source.stale)); }
function botRequestForNative(source) {
  return state.botRequests.requests.find((request) => request.nativeRef && request.host === source.host &&
    request.nativeRef.agent === source.agent && request.nativeRef.id === source.id && request.nativeRef.home === source.home);
}
function botRequestMatches(request) {
  const query = state.search.trim().toLocaleLowerCase();
  return !query || [request.title, request.botName, request.host, request.channelLabel, request.requesterLabel,
    request.authorityLabel, request.scopeLabel, BOT_STATUS[request.status]].some((value) => String(value || "").toLocaleLowerCase().includes(query));
}
function botSnapshot(data) {
  if (!data || !Array.isArray(data.requests) || !Array.isArray(data.sources) ||
      data.requests.some((request) => !request || typeof request.id !== "string" || !request.id || typeof request.sourceId !== "string" || !request.sourceId) ||
      data.sources.some((source) => !source || typeof source.id !== "string"))
    throw new Error("봇 요청 목록 형식을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.");
  const unique = new Map();
  for (const request of data.requests) unique.set(botRequestKey(request), request);
  return { requests: [...unique.values()], sources: data.sources, fetchedAt: data.fetchedAt };
}
async function loadBotRequests({ refresh = false } = {}) {
  if (!state.authenticated) return;
  const cache = state.botRequests;
  if (cache.promise) return cache.promise;
  if (!refresh && cache.loaded && Date.now() - cache.readAt < BOT_CACHE_MS) return;
  if (networkOffline()) { cache.error = "오프라인입니다. 연결 후 봇 요청을 다시 확인해 주세요."; render(); return; }
  const authEpoch = state.authEpoch;
  cache.busy = true; cache.error = "";
  cache.promise = (async () => {
    try {
      // Refresh is explicit and bounded to this bot source service. A normal
      // category visit is one GET; render/poll never starts another request.
      const data = refresh
        ? await api("/api/bot-requests/refresh", { method: "POST", body: {}, authEpoch, csrfRetry: false })
        : await api("/api/bot-requests", { authEpoch });
      assertAuthEpoch(authEpoch);
      if (state.botRequests !== cache) return;
      Object.assign(cache, botSnapshot(data), { loaded: true, readAt: Date.now() });
      const selected = selectedSession();
      const moved = selected && botRequestForNative(sourceOf(selected));
      if (moved) selectBotRequest(botRequestKey(moved));
    } catch (err) {
      if (authEpoch === state.authEpoch && state.botRequests === cache && !err.staleAuth && !err.unauthorized)
        cache.error = err.message || "봇 요청을 확인하지 못했습니다. 다시 확인해 주세요.";
    } finally {
      if (authEpoch === state.authEpoch && state.botRequests === cache) {
        cache.busy = false; cache.promise = null; render();
      }
    }
  })();
  render();
  return cache.promise;
}
// Sessions bots opened with no request of theirs listed (a Slack bot's, Hermes'): 봇 요청 lists
// them under the requests, most recent first.
function botOwnSessions(sessions, now) {
  return sessions.filter((s) => sessionCategoryOf(s) === "bot" && !botRequestForNative(sourceOf(s)) &&
    matches(s, now, { category: false, initiator: false, phase: false })).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}
function renderBotRequests() {
  const cache = state.botRequests, box = $("groups");
  const shown = cache.requests.filter(botRequestMatches).sort((a, b) => botTimestamp(b.updatedAt) - botTimestamp(a.updatedAt));
  const tools = el("div", "bot-list-tools");
  const note = el("p", "muted", cache.busy ? "봇 요청을 확인하고 있습니다…" : cache.loaded ? `확인된 요청 ${cache.requests.length}개` : "봇 요청을 확인해 주세요.");
  const refresh = button(cache.busy ? "확인 중…" : "새로고침", "secondary", () => loadBotRequests({ refresh: true }));
  refresh.id = "bot-requests-refresh"; refresh.disabled = cache.busy || networkOffline();
  tools.append(note, refresh);
  const nodes = [tools];
  if (cache.error) { const error = el("p", "bot-source-notice", cache.error + (cache.loaded ? " 마지막으로 확인한 요청을 표시합니다." : "")); error.setAttribute("role", "status"); nodes.push(error); }
  for (const source of cache.sources.filter((source) => source.ok !== true)) {
    const error = typeof source.error === "string" ? source.error : source.error?.message;
    const notice = el("div", "bot-source-notice");
    notice.append(el("div", "", `${botLabel(source.label)} · ${error || "요청 목록을 확인하지 못했습니다. 연결 상태를 확인해 주세요."}`));
    if (source.stale) notice.append(el("div", "", "마지막으로 확인한 요청을 표시합니다."));
    nodes.push(notice);
  }
  if (cache.busy && !cache.loaded) nodes.push(skeletonList("봇 요청을 확인하고 있습니다…"));
  for (const request of shown) {
    const key = botRequestKey(request);
    const card = button("", "bot-request-card", () => selectBotRequest(key)); card.dataset.botRequestKey = key;
    card.setAttribute("aria-pressed", String(state.selectedBotRequest === key));
    const meta = el("span", "bot-request-top"); meta.append(el("strong", "bot-name", botLabel(request.botName)), el("span", "bot-status", BOT_STATUS[request.status] || "상태 미확인"));
    if (botRequestStale(request)) meta.append(el("span", "bot-stale-tag", "이전 정보"));
    card.append(meta, el("span", "bot-request-title", botLabel(request.title)),
      el("span", "bot-request-context", `요청자 ${botLabel(request.requesterLabel)} · 채널 ${botLabel(request.channelLabel)}`),
      el("span", "bot-request-boundary", `권한 ${botLabel(request.authorityLabel)}`),
      el("span", "bot-request-boundary", `범위 ${botLabel(request.scopeLabel)}`));
    nodes.push(card);
  }
  const now = state.snapshot?.serverTime || Date.now() / 1000;
  const own = botOwnSessions(allSessions(), now);
  if (own.length) {
    const section = el("section", "group bot-own-sessions");
    const head = el("h2", "group-head gray", "봇이 연 세션");
    const count = el("span", "group-count", String(own.length));
    head.append(count);
    const folders = cardFolderHints(own);
    const rows = own.map((session) => {
      const row = card(session);
      updateCard(row.card, session, now, folders.get(session.key));
      return row;
    });
    section.append(head, ...rows);
    nodes.push(section);
  }
  if (!shown.length && !own.length && !cache.busy) nodes.push(emptyState("none", state.search ? "검색에 맞는 봇 요청이 없습니다." : cache.error || cache.sources.some((source) => !source.ok) ? "봇 요청을 확인하지 못했습니다." : "표시할 봇 요청이 없습니다.", "봇에서 접수한 요청과 권한·사용 범위를 이곳에서 확인합니다."));
  box.replaceChildren(...nodes); box.setAttribute("aria-busy", String(cache.busy));
  $("session-count").textContent = cache.loaded ? `${shown.length + own.length}개` : own.length ? `${own.length}개` : "-";
  // A conversation open here (a request's session or a bot's own) draws itself (render).
  if (allSessions().some((session) => session.key === state.selected)) return;
  const selected = cache.requests.find((request) => botRequestKey(request) === state.selectedBotRequest);
  if (state.selectedBotRequest && !selected) {
    state.selectedBotRequest = null; $("detail").replaceChildren(); setDetailShown(false); navigateView("list", true);
  } else if (selected) drawBotRequest(selected);
  else if (!$("detail").children.length) $("detail").append(emptyState("none", "봇 요청을 선택하세요.", "요청자와 채널, 권한과 사용 범위를 확인할 수 있습니다."));
}
function selectBotRequest(key) {
  if (!state.authenticated || state.launchBusy) return;
  if (state.docked) undock();
  const request = state.botRequests.requests.find((row) => botRequestKey(row) === key); if (!request) return;
  // Its session listed: its conversation, to follow (watch only), with the request's details.
  const native = sessionForBotRequest(request);
  if (native) {
    select(native.key);
    return;
  }
  rememberConversationView(); stopChatPolling();
  state.selected = null; state.detailFor = null; state.detailRevision = null; state.detailRun += 1;
  state.selectedBotRequest = key;
  window.SessionholicGoals?.openTab?.("sessions");
  setSessionCategory("bot", { renderView: false, clearSelection: false });
  $("detail").replaceChildren();
  render();
  const entering = phoneLayout() && !state.detailShown;
  if (entering) navigateView("detail");
  setDetailShown(true, { direction: entering ? "push" : null, focus: phoneLayout() });
}
// Under a bot's conversation, in place of the composer: who runs it, and where to answer.
function botWatchNotice(s) {
  const request = botRequestForNative(sourceOf(s));
  const box = el("div", "bot-watch");
  box.id = "bot-watch";
  box.append(el("p", "bot-watch-title", [botNameOf(s), "봇이 진행하는 세션 · 보기 전용"].filter(Boolean).join(" · ")));
  if (request)
    box.append(el("p", "bot-watch-meta", [`요청자 ${botLabel(request.requesterLabel)}`, `채널 ${botLabel(request.channelLabel)}`,
      `권한 ${botLabel(request.authorityLabel)}`, `상태 ${BOT_STATUS[request.status] || "상태 미확인"}`].join(" · ")));
  box.append(el("p", "bot-watch-hint", "대화는 여기서 계속 새로 고쳐집니다. 입력과 실행 관리는 원래 봇에서 해 주세요."));
  return box;
}
function drawBotRequest(request) {
  const pane = $("detail");
  const stale = botRequestStale(request);
  const revision = JSON.stringify([request, stale]);
  if (pane.dataset.botRevision === revision && pane.querySelector?.(".bot-request-detail")) return;
  pane.replaceChildren(); pane.dataset.key = ""; pane.dataset.botRevision = revision;
  pane.classList.remove("is-reading", "is-typing"); state.chromeVisible = true;
  const head = el("div", "detail-head bot-detail-head");
  const back = button("←", "back", backToList); back.setAttribute("aria-label", "봇 요청 목록으로 돌아가기");
  const titles = el("div", "detail-titles"); titles.append(el("h1", "detail-title", botLabel(request.title)), el("p", "detail-project", botLabel(request.botName)));
  head.append(back, titles);
  const detail = el("section", "bot-request-detail"); detail.setAttribute("aria-label", "봇 요청 정보");
  if (stale) detail.append(el("p", "bot-source-notice", "이전 정보 · 마지막으로 확인한 요청입니다. 새로고침으로 최신 상태를 확인해 주세요."));
  detail.append(el("p", "bot-request-guidance", "이 요청은 원래 봇에서 이어가세요. 아래 권한과 사용 범위가 적용됩니다."));
  const metadata = el("dl", "bot-request-metadata");
  for (const [label, value] of [["봇", request.botName], ["요청자", request.requesterLabel], ["채널", request.channelLabel], ["권한", request.authorityLabel], ["사용 범위", request.scopeLabel], ["기기", request.host ? hostLabel(request.host) : null], ["상태", BOT_STATUS[request.status]]]) {
    const row = el("div"); row.append(el("dt", "", label), el("dd", "", botLabel(value))); metadata.append(row);
  }
  detail.append(metadata);
  if (request.nativeRef) detail.append(el("p", "bot-native-note", "연결된 세션 기록이 있습니다. 입력과 실행 관리는 원래 봇에서 진행해 주세요."));
  // Its conversation below the details, watch-only (no composer, no session control).
  const readable = botConversationReadable(request);
  if (readable) {
    const conversation = el("section", "bot-conversation"); conversation.id = "bot-conversation";
    conversation.setAttribute("aria-label", "봇 대화 기록");
    conversation.dataset.conversationKey = botConversationKey(request);
    detail.append(conversation);
  }
  pane.append(head, detail);
  if (readable) loadBotConversation(request);
}
// A request whose bot kept its conversation (a Hermes session, or a Codex one not listed
// here): read on opening, once a minute at most unless the request changed or asked again.
const BOT_CONVERSATION_MS = 60000;
function botConversationReadable(request) { return !!(request?.conversationRef || request?.nativeRef); }
function botConversationKey(request) { return botRequestKey(request); }
async function loadBotConversation(request, { force = false } = {}) {
  if (!state.authenticated || !botConversationReadable(request)) return;
  const key = botConversationKey(request), revision = String(request.updatedAt ?? "");
  let entry = state.botConversations.get(key);
  if (entry?.promise) return entry.promise;
  if (entry && !force && !entry.error && entry.revision === revision && Date.now() - entry.readAt < BOT_CONVERSATION_MS) {
    renderBotConversation(request); return;
  }
  if (!entry) { entry = { data: null, error: "", readAt: 0, revision, promise: null }; state.botConversations.set(key, entry); }
  if (networkOffline()) { entry.error = "오프라인입니다. 연결 후 대화를 다시 불러와 주세요."; renderBotConversation(request); return; }
  const authEpoch = state.authEpoch;
  entry.error = "";
  entry.promise = (async () => {
    try {
      const params = new URLSearchParams({ source: request.sourceId, request: request.id });
      const data = await api(`/api/bot-requests/conversation?${params}`, { authEpoch });
      if (!data || !Array.isArray(data.segments) || data.segments.some((segment) => !segment || !Array.isArray(segment.messages)))
        throw new Error("봇 대화 형식을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요.");
      if (state.botConversations.get(key) === entry) Object.assign(entry, { data, readAt: Date.now(), revision });
    } catch (err) {
      if (authEpoch === state.authEpoch && state.botConversations.get(key) === entry && !err.staleAuth && !err.unauthorized)
        entry.error = err.message || "봇 대화를 읽지 못했습니다. 다시 확인해 주세요.";
    } finally {
      if (state.botConversations.get(key) === entry) { entry.promise = null; renderBotConversation(request); }
    }
  })();
  renderBotConversation(request);
  return entry.promise;
}
function renderBotConversation(request) {
  const key = botConversationKey(request), box = $("bot-conversation");
  if (!box || box.dataset.conversationKey !== key) return;
  const entry = state.botConversations.get(key);
  const segments = entry?.data?.segments || [];
  const nodes = [el("h2", "bot-conversation-title", "대화 기록")];
  if (entry?.promise && !entry.data) nodes.push(el("p", "bot-conversation-status", "대화를 불러오고 있습니다…"));
  if (entry?.error) {
    const notice = el("div", "bot-source-notice"); notice.setAttribute("role", "status");
    notice.append(el("p", "", entry.data ? `${entry.error} 마지막으로 읽은 대화를 표시합니다.` : entry.error),
      button("대화 다시 불러오기", "secondary", () => loadBotConversation(request, { force: true })));
    nodes.push(notice);
  }
  for (const segment of segments) {
    if (segment.error) nodes.push(el("p", "bot-source-notice", segment.error));
    if (segment.more) nodes.push(el("p", "conversation-window-notice", "이 대화의 앞부분 일부는 여기에 나오지 않습니다."));
    for (const message of segment.messages) nodes.push(botMessageRow(message, request));
    if (!segment.error && !segment.messages.length) nodes.push(el("p", "group-empty", "저장된 대화를 찾지 못했습니다."));
  }
  if (entry?.data && !segments.length) nodes.push(el("p", "group-empty", "이 요청에서 읽을 수 있는 대화가 없습니다."));
  box.replaceChildren(...nodes);
  box.setAttribute("aria-busy", String(!!entry?.promise));
}
// One message, in the same bubbles as a session's conversation; nothing here sends or acts.
function botMessageRow(message, request) {
  const role = message?.role === "user" ? "user" : "assistant";
  const text = typeof message?.text === "string" ? message.text : "";
  const row = el("article", `msg ${role}`);
  const label = el("div", "msg-label");
  label.append(el("strong", null, role === "user" ? "요청자" : botLabel(request.botName)));
  if (typeof message.ts === "number" && message.ts > 0) label.append(el("span", null, clock(message.ts)));
  const bubble = el("div", "bubble");
  if (role === "assistant" && typeof MessageFormat !== "undefined")
    MessageFormat.render(bubble, text, { onCopy: (value) => copyText(value, "코드를 복사했습니다.") });
  else bubble.textContent = text;
  row.append(label, bubble);
  if (message.truncated === true) {
    const total = Number.isInteger(message.fullLength) ? ` · 전체 ${message.fullLength.toLocaleString()}자` : "";
    row.append(el("p", "msg-truncated", `긴 메시지라 앞부분만 표시합니다${total}.`));
  }
  return row;
}
const SESSION_CATEGORIES = { general: "일반 세션", agent: "에이전트 세션", bot: "봇 요청" };
function sessionCategoryOf(session) {
  const origin = originOf(session);
  return origin === "bot" ? "bot" : origin === "human" ? "general" : "agent";
}
// A bot request's own session, once listed: its conversation is what the request shows.
function sessionForBotRequest(request) {
  const ref = request?.nativeRef;
  return ref ? allSessions().find((s) => s.host === request.host && s.agent === ref.agent && s.id === ref.id && s.home === ref.home) || null : null;
}
function setSessionCategory(category, { renderView = true, clearSelection = true } = {}) {
  if (!Object.hasOwn(SESSION_CATEGORIES, category) || state.launchBusy) return false;
  const changed = state.sessionCategory !== category;
  const selected = selectedSession();
  // With a bot picked, every tab but 봇 요청 lists its sessions: the open one stays open.
  const kept = botFilter() && category !== "bot";
  if (clearSelection && ((selected && !kept && sessionCategoryOf(selected) !== category) || (state.selectedBotRequest && category !== "bot"))) {
    rememberConversationView(); stopChatPolling();
    state.selected = null; state.selectedBotRequest = null; state.detailFor = null; state.detailRevision = null; state.detailRun += 1;
    $("detail").replaceChildren(); $("detail").dataset.key = ""; setDetailShown(false); navigateView("list", true);
  }
  state.sessionCategory = category;
  if (changed && !state.selected && !state.selectedBotRequest) $("detail").replaceChildren();
  const origin = state.filters.initiator;
  if (category !== "bot" && origin && !botFilter() && (origin === "human" ? "general" : "agent") !== category) state.filters.initiator = "";
  if (renderView) { render(); if (changed) $("task-list-pane").scrollTop = 0; }
  if (category === "bot" && renderView) loadBotRequests();
  return changed;
}
function renderSessionCategories(sessions, now) {
  const botSessions = botOwnSessions(sessions, now).length;
  const counts = { general: 0, agent: 0,
    bot: state.botRequests.loaded ? state.botRequests.requests.filter(botRequestMatches).length + botSessions : botSessions || null };
  for (const session of sessions) {
    const category = sessionCategoryOf(session);
    if (category !== "bot" && matches(session, now, { category: false, initiator: category === state.sessionCategory })) counts[category] += 1;
  }
  // A bot picked: its sessions are listed in either session tab whatever their own tab.
  if (botFilter()) counts.general = counts.agent = sessions.filter((session) => matches(session, now, { category: false })).length;
  for (const [category, label] of Object.entries(SESSION_CATEGORIES)) {
    const control = $(`session-category-${category}`); const selected = state.sessionCategory === category;
    control.setAttribute("aria-selected", String(selected)); control.tabIndex = selected ? 0 : -1;
    control.setAttribute("aria-label", counts[category] === null ? `${label}, 아직 확인하지 않음` : `${label} ${counts[category]}개`);
    $(`session-category-${category}-count`).textContent = counts[category] === null ? "-" : String(counts[category]);
  }
  $("groups").setAttribute("aria-labelledby", `session-category-${state.sessionCategory}`);
  $("session-category-note").hidden = state.sessionCategory !== "agent" || !!state.filters.initiator || !!botFilter();
  const others = Object.keys(SESSION_CATEGORIES).filter((category) => category !== state.sessionCategory && counts[category] > 0);
  const other = others[0];
  $("session-category-search").hidden = !state.search.trim() || !other;
  $("session-category-search-action").textContent = other ? `${SESSION_CATEGORIES[other]}에 검색 결과 ${counts[other]}개 · 보기` : "";
  $("session-category-search-action").dataset.category = other || "general";
}
function sessionCategoryKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const categories = Object.keys(SESSION_CATEGORIES), index = categories.indexOf(state.sessionCategory);
  const category = event.key === "Home" ? categories[0] : event.key === "End" ? categories.at(-1) : categories[(index + (event.key === "ArrowRight" ? 1 : -1) + categories.length) % categories.length];
  setSessionCategory(category); $(`session-category-${category}`).focus();
}
function prepareSessionListFor(session) {
  // A picked bot lists its sessions in any tab: opening one keeps the tab (and the bot).
  let changed = botFilter() ? false : setSessionCategory(sessionCategoryOf(session), { renderView: false, clearSelection: false });
  for (const key of Object.keys(state.filters)) {
    const value = key === "initiator" ? (botFilter() ? BOT_FILTER_PREFIX + botNameOf(session) : initiatorOf(session)) : session[key];
    const excluded = key === "account" && state.filters.account && state.accountFilterRoutes
      ? !state.accountFilterRoutes.has(accountRouteKey(session)) : state.filters[key] && state.filters[key] !== value;
    if (excluded) { state.filters[key] = ""; if (key === "account") state.accountFilterRoutes = null; changed = true; }
  }
  if (state.phaseFilter && groupOf(session) !== state.phaseFilter) { state.phaseFilter = ""; changed = true; }
  const now = state.snapshot?.serverTime || Date.now() / 1000;
  if (!sessionRemoved(session) && !state.showOld && groupOf(session) === "rest" &&
      (now - (session.updatedAt || 0) > RECENT_SECONDS || sessionTidied(session))) {
    state.showOld = true; $("show-old").checked = true; changed = true;
  }
  if (state.search && !matches(session, now)) {
    state.search = ""; $("search").value = ""; window.SessionholicGoals?.setQuery?.(""); changed = true;
  }
  return changed;
}
const FILTER_FIELDS = ["filter-host", "filter-agent", "filter-project", "filter-account", "filter-initiator"];
function renderFilters(sessions) {
  const active = Object.values(state.filters).filter(Boolean).length + (state.showOld ? 1 : 0) + (state.showRemoved ? 1 : 0);
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
    ["initiator", "시작 주체", (s) => [initiatorOf(s), INITIATOR_LABELS[initiatorOf(s)]]],
  ]) {
    const options = new Map(sessions.map(pick).filter(([v]) => v));
    if (key === "initiator") {
      for (const [value, text] of Object.entries(INITIATOR_LABELS)) options.set(value, text);
      // Every bot that opened a listed session, from all tabs (its requests are in 봇 요청).
      for (const session of allSessions()) {
        const bot = botNameOf(session);
        if (bot) options.set(BOT_FILTER_PREFIX + bot, `봇 · ${bot}`);
      }
    }
    const field = el("label", null, label);
    const select = el("select");
    select.id = `filter-${key}`;
    select.append(new Option("전체", ""));
    for (const [value, text] of [...options].sort((a, b) =>
      String(a[1]).localeCompare(String(b[1]), "ko"),
    ))
      select.append(new Option(text, value));
    if (state.filters[key] && !options.has(state.filters[key])) {
      const last = state.filters[key];
      const name = key === "initiator" && last.startsWith(BOT_FILTER_PREFIX) ? `봇 · ${last.slice(BOT_FILTER_PREFIX.length)}` : last;
      select.append(new Option(`${name} · 마지막 선택`, last));
    }
    select.value = state.filters[key];
    select.addEventListener("change", () => {
      state.filters[key] = select.value;
      if (key === "account") state.accountFilterRoutes = null;
      if (key === "initiator" && select.value && !select.value.startsWith(BOT_FILTER_PREFIX))
        setSessionCategory(select.value === "human" ? "general" : "agent", { renderView: false });
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
  state.showRemoved = false;
  if ($("show-removed")) $("show-removed").checked = false;
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
// Within a group the list keeps one order, by the title it shows (가나다, numbers
// by value), so a session that updates every few seconds stays where it is.
// Renaming a session (제목 수정) is how it is moved.
const TITLE_ORDER = new Intl.Collator("ko", { numeric: true, sensitivity: "base" });
function titleOrder(a, b) {
  return TITLE_ORDER.compare(cardTitle(a), cardTitle(b)) || (String(a.key) < String(b.key) ? -1 : String(a.key) > String(b.key) ? 1 : 0);
}
// When a session's conversation was last opened on this board (any device, or this page). The
// conversation shown beside the list keeps the place it had when opened (its card does not
// jump under the keyboard or the pointer); it moves up once another is opened or the list
// alone is shown (a phone going back).
function openedAt(s) {
  if (state.openedHeld?.key === s.key && state.selected === s.key && state.detailShown && !phoneLayout()) return state.openedHeld.at;
  return Math.max(Number(s.openedAt) || 0, state.openedLocal?.get(s.key) || 0);
}
// 이전: the most recently opened first, then the most recently changed (the groups above keep
// their fixed title order).
function recentOrder(a, b) {
  return openedAt(b) - openedAt(a) || (Number(b.updatedAt) || 0) - (Number(a.updatedAt) || 0) || titleOrder(a, b);
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
  for (const group of [{ key: "favorites", label: "★ 즐겨찾기", tone: "gray" }, ...GROUPS]) {
    const items = shown.filter((s) => group.key === "favorites" ? sessionPinned(s) : !sessionPinned(s) && groupOf(s, now) === group.key)
      .sort(group.key === "rest" ? recentOrder : titleOrder);
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
    const filtering = Object.values(state.filters).some(Boolean) || state.showOld || state.showRemoved;
    const unconfigured = !state.snapshot?.hosts?.length;
    const offline = state.snapshot?.hosts?.length && state.snapshot.hosts.every((host) => !host.ok && !host.refreshing);
    const clearAll = () => {
      state.search = "";
      state.phaseFilter = "";
      $("search").value = "";
      resetFilters();
    };
    let empty;
    if (state.phaseFilter && !searching && !filtering) {
      empty = emptyState("filter", state.phaseFilter === "needs_input" ? "답이 필요한 세션이 없습니다." : "작업 중인 세션이 없습니다.",
        "다른 세션은 필터를 해제하면 보입니다.", button("필터 해제", "secondary", () => togglePhaseFilter(state.phaseFilter)));
    } else if (searching || filtering || state.phaseFilter) {
      empty = emptyState("search", "조건에 맞는 세션이 없습니다.",
        `${searching ? `‘${state.search}’ 검색. ` : ""}${filtering ? "선택한 필터가 적용되어 있습니다. " : ""}${state.showRemoved ? "지운 세션도 포함했습니다. " : ""}${state.showOld ? "오래된 기록도 포함했습니다." : "3일 지난 대화는 제외되어 있습니다. 필터에서 오래된 기록을 포함할 수 있습니다."}`,
        button("검색·필터 초기화", "secondary", clearAll));
    } else if (unconfigured) {
      empty = emptyState("setup", "연결할 기기가 아직 설정되지 않았습니다.",
        "설치 안내에 따라 기기를 설정한 뒤 새로고침해 주세요.");
    } else if (offline) {
      empty = emptyState("offline", "기기에 연결되지 않아 세션을 확인하지 못했습니다.",
        "기기 연결과 에이전트 설치 상태를 확인한 뒤 다시 확인해 주세요.", button("다시 확인", "secondary", manualRefresh));
    } else if (state.snapshot?.hosts?.some((host) => host.refreshing)) {
      loading = true;
      empty = skeletonList("기기에서 세션을 확인하고 있습니다…");
    } else {
      empty = emptyState("none", "표시할 최근 세션이 없습니다.",
        "설정한 기기에서 Claude Code나 Codex로 대화를 시작한 뒤 새로고침해 주세요. 지난 대화나 지운 세션은 필터에서 볼 수 있습니다.");
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
// project · time) with a disclosure chevron and an explicit device badge.
// Account details stay in its accessible name and conversation header. The row holds the card and
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
    if (state.authenticated && mounted && current && chatKey(sourceOf(current)) === row.dataset.sourceKey) {
      if (groupOf(current) === "needs_input") answerOnOpen(current);
      select(current.key);
    }
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
  const removed = el("span", "removed-tag", "지움");
  removed.title = "목록에서 지운 세션입니다. 필터의 '지운 세션 포함'을 켜면 보입니다.";
  const execution = el("span", "card-execution", "일회성 실행");
  const time = el("span", "card-time");
  const provenance = el("span", "card-provenance");
  const device = el("span", "card-device");
  const deviceIcon = el("span", "card-device-icon");
  deviceIcon.setAttribute("aria-hidden", "true");
  deviceIcon.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="3" width="15" height="10.5" rx="1.5"/><path d="M10 13.5V17M6.5 17h7"/></svg>';
  const host = el("span", "card-device-label");
  device.append(deviceIcon, host);
  const initiator = el("span", "card-initiator");
  provenance.append(device, initiator);
  const goals = el("span", "card-goals");
  // A live board terminal of this session (renderTerminalMarks keeps it current).
  const terminal = terminalMark("card-terminal");
  // Not looked at since it answered or finished (sessionUnread).
  const unread = el("span", "card-unread");
  unread.setAttribute("aria-hidden", "true");
  unread.hidden = true;
  meta.append(unread, badge, terminal, agent, execution, project, stale, removed, time);
  const chevron = el("span", "card-chevron");
  chevron.setAttribute("aria-hidden", "true");
  node.append(title, snippet, provenance, goals, meta, chevron);
  node.cardParts = { title, snippet, badge, terminal, agent, execution, project, stale, removed, time, initiator, host, goals, unread };
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
  const [label, tone] = BADGES[s.runtimeKnown === false ? "unknown" : s.phase] || ["상태 확인 필요", "gray"];
  const time = ago(s.updatedAt, now);
  const unread = sessionUnread(s, now);
  node.className = `card${state.selected === s.key ? " selected" : ""}`;
  node.setAttribute("aria-pressed", String(state.selected === s.key));
  node.cardLabel = [unread ? "읽지 않음" : "", "프로젝트: " + project, "작업: " + title,
    preview ? "대화 미리보기: " + preview : "", folderHint ? "작업 폴더: " + folderHint : "",
    label, agent, "기기: " + host, account ? "계정 설정: " + account : "", time,
    s.stale ? "마지막 확인 기준" : "", sessionRemoved(s) ? "목록에서 지움" : ""].filter(Boolean).join(". ");
  markCardTerminal(node, s);
  node.title = `${project}\n${title}`;
  const place = folderHint ? `${project} · ${folderHint}` : project;
  write(p.title, (sessionPinned(s) ? "★ " : "") + title); p.title.title = title;
  write(p.snippet, preview); p.snippet.hidden = !preview; p.snippet.title = preview;
  write(p.badge, label); p.badge.className = `badge ${tone}`;
  write(p.agent, agent); p.agent.className = `card-agent ${s.agent}`;
  write(p.project, place); p.project.title = place;
  write(p.time, time);
  write(p.host, host); p.host.title = `기기: ${host}`;
  write(p.initiator, initiatorLabel(s));
  p.execution.hidden = !sessionOneShot(s);
  const linkedGoals = window.SessionholicGoals?.linkedTitles?.(s) || "";
  write(p.goals, linkedGoals);
  p.goals.hidden = !linkedGoals;
  p.stale.hidden = !s.stale;
  p.stale.title = s.stale ? "기기에 연결되지 않아 마지막으로 확인한 정보입니다." : "";
  if (p.removed) p.removed.hidden = !sessionRemoved(s);
  if (p.unread) { p.unread.hidden = !unread; p.unread.title = unread ? "아직 열어 보지 않은 결과" : ""; }
}
// ---------- 읽음 표시 ----------
// A session that answered (답 필요) or just stopped (최근 끝남) after it was last opened on
// this board is shown unread on every device: the board keeps when each was looked at
// (snapshot sessionSeen.since and each row's seenAt). Before the board kept any, nothing
// is unread; a mark from this page counts at once (state.seenLocal) until it is read back.
function sessionUnread(s, now = state.snapshot?.serverTime || Date.now() / 1000) {
  const since = state.snapshot?.sessionSeen?.since;
  if (typeof since !== "number") return false;
  const group = groupOf(s, now);
  if (group !== "needs_input" && group !== "finished") return false;
  const seen = Math.max(Number(s.seenAt) || 0, state.seenLocal.get(s.key) || 0, since);
  return (Number(s.updatedAt) || 0) > seen;
}
// A refused or failed mark brings the dot back and is not tried again for this long.
const SEEN_RETRY_MS = 60000;
// The session as the board's read marks name it: a Claude conversation without a worker id
// by its sessionId (as session_labels.js names it for 제목 수정).
function seenSource(s) {
  const source = { ...sourceOf(s) };
  if (s.agent === "claude") {
    source.home ||= ".claude";
    if (!source.id && s.kind === "interactive") source.id = s.sessionId;
    if (s.sessionId) source.sessionId = s.sessionId;
  }
  return source;
}
// Opening a conversation is kept by the board (the list orders 이전 by it); once a minute at most.
const OPENED_SPACING_SECONDS = 60;
function markOpened(s) {
  const now = Math.floor(Date.now() / 1000);
  const before = Math.max(Number(s.openedAt) || 0, state.openedLocal?.get(s.key) || 0);
  // Opened again after going back (or from another): its place is taken afresh.
  if (state.openedHeld?.key !== s.key || !state.detailShown) state.openedHeld = { key: s.key, at: before };
  if (now - before < OPENED_SPACING_SECONDS) return;
  (state.openedLocal ||= new Map()).set(s.key, now);
  api("/api/session/opened", { method: "POST", body: { source: seenSource(s) } }).catch(() => {
    // Not kept: this page still orders by it; another device will not.
  });
}
function markSeen(s) {
  const at = Number(s?.updatedAt) || 0;
  if (!at || (state.seenLocal.get(s.key) || 0) >= at || (Number(s.seenAt) || 0) >= at) return;
  if (Date.now() - (state.seenFailed.get(s.key) || 0) < SEEN_RETRY_MS) return;
  state.seenLocal.set(s.key, at);
  api("/api/session/seen", { method: "POST", body: { source: seenSource(s), updatedAt: at } }).catch(() => {
    // Not kept by the board: the dot comes back, and this page waits a minute to try again.
    if (state.seenLocal.get(s.key) === at) state.seenLocal.delete(s.key);
    state.seenFailed.set(s.key, Date.now());
    render();
  });
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
    if (fired) root.pressedAt = state.pressLiftAt = Date.now();
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
  const terminal = terminalForSession(s);
  const open = !!terminal;
  const label = terminalEstimated(terminal) ? "터미널 연결 추정" : "터미널 열림";
  const text = mark.querySelector?.(".terminal-mark-label");
  if (text) text.textContent = label;
  if (mark.hidden !== !open) mark.hidden = !open;
  if (typeof node.cardLabel === "string")
    node.setAttribute("aria-label", open ? `${node.cardLabel}. ${label}` : node.cardLabel);
}
function terminalEstimated(terminal) {
  if (!terminal) return false;
  const info = terminalInfo(terminal);
  return info.linkConfidence === "estimated" || (info.mode === "new-session" && info.nativeSource && info.linkConfidence !== "exact");
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
  const mark = box.querySelector?.(".terminal-mark-label");
  if (mark) mark.textContent = terminalEstimated(terminal) ? "터미널 연결 추정" : "터미널 열림";
  box.title = terminalEstimated(terminal) ? "시작 시각으로 추정한 연결입니다. 터미널의 대화가 맞는지 확인해 주세요." : "";
  $("detail-terminal-open")?.setAttribute("aria-label",
    terminal ? `열린 터미널로 가기: ${terminalTitle(terminal, s.title || "열어 둔 터미널")}` : "열린 터미널로 가기");
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
  if (sessionOneShot(s)) { select(s.key); toast(ONE_SHOT_NO_INPUT); return; }
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
  if (sessionOneShot(s)) return { kind: "manage", label: "세션 관리", s };
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
  else if (action.kind === "manage") manageSession(action.s);
  else openSessionTerminal(action.s);
}
function cardActions(s) {
  const terminal = terminalForSession(s);
  const actions = [
    { label: "대화 열기", run: () => select(s.key) },
    { label: sessionPinned(s) ? "★ 상단 고정 해제" : "☆ 상단 고정 (즐겨찾기)", run: () => pinSession(s) },
    { label: "세션 관리", run: () => manageSession(s) },
  ];
  if (window.SessionholicLabels?.eligible?.(s)) actions.push({ label: "제목 수정", run: () => window.SessionholicLabels.open(s, document.activeElement) });
  if (!sessionOneShot(s)) actions.push({ label: terminal ? "터미널로 돌아가기" : "터미널 열기", run: () => openSessionTerminal(s), disabled: state.launchBusy });
  if (terminal) {
    const copy = terminalCloseCopy(terminal);
    actions.push({ label: copy.action, danger: copy.danger, run: () => closeTerminal(terminal.id) });
  }
  // 목록에서 지우기 only changes the list; with a board terminal open, it can close that first.
  const removed = sessionRemoved(s);
  if (terminal && !removed) {
    const copy = terminalCloseCopy(terminal);
    actions.push({ label: `${copy.action} 뒤 목록에서 지우기`, danger: copy.danger, run: () => closeTerminal(terminal.id, { thenRemove: s }) });
  }
  if (!removed) actions.push({ label: "목록에서 지우기", run: () => removeSession(s, true) });
  if (removed || sessionTidied(s)) actions.push({ label: "목록에 다시 표시", run: () => showSessionAgain(s) });
  return actions;
}
const REMOVED_NOTE = "목록에서 지웠습니다. 필터의 '지운 세션 포함'에서 다시 표시할 수 있습니다.";
// 목록에서 지우기 / 다시 표시: the board's own record (session-removed); the run is untouched.
async function removeSession(s, removed, { quiet = false } = {}) {
  const key = s.key;
  const mark = { removed };
  (state.removedLocal ||= new Map()).set(key, mark);
  render();
  try {
    const result = await api("/api/session/remove", { method: "POST", body: { source: seenSource(s), removed } });
    if (Number.isInteger(result?.version)) mark.version = result.version;
    mark.after = state.snapshot;
    if (!removed && state.removedLocal.get(key) === mark) { keepRestoredSession(s); render(); }
    if (!quiet) toast(removed ? REMOVED_NOTE : "목록에 다시 표시합니다.");
    poll(false);
    return true;
  } catch (err) {
    // A later choice for the same session keeps its own mark.
    if (state.removedLocal.get(key) === mark) state.removedLocal.delete(key);
    render();
    if (!err.staleAuth && !err.unauthorized) toast(err.message || "목록을 바꾸지 못했습니다.");
    return false;
  }
}
// Both records that keep a session off the list are cleared (a tidy from an end on the board).
async function showSessionAgain(s) {
  if (sessionTidied(s)) {
    try {
      await api("/api/session/tidy", { method: "POST", body: { source: seenSource(s), tidied: false } });
    } catch (err) {
      if (!err.staleAuth && !err.unauthorized) toast(err.message || "목록을 바꾸지 못했습니다.");
      return;
    }
  }
  if (sessionRemoved(s)) await removeSession(s, false);
  else { keepRestoredSession(s); render(); toast("목록에 다시 표시합니다."); poll(false); }
}
function keepRestoredSession(s) {
  const now = state.snapshot?.serverTime || Date.now() / 1000;
  if (sessionTidied(s) || now - (s.updatedAt || 0) > RECENT_SECONDS) {
    state.showOld = true;
    $("show-old").checked = true;
  }
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
  // A selection left on the page (or one iPadOS starts under the finger) keeps taps for itself.
  try {
    window.getSelection?.()?.removeAllRanges?.();
  } catch {
    /* nothing selected */
  }
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
  if (dialog?.bottomSheet?.unseenPressTaken()) return true;
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

// Original execution controls are separate from closing the board's attached terminal.
let sessionControl = null;
function closeSessionControl(force = false) {
  if (!force && sessionControl?.busy) return;
  const previous = sessionControl;
  sessionControl = null;
  const dialog = $("session-control-dialog");
  if (dialog?.open) dialog.close();
  const handle = dialog?.querySelector?.(".sheet-handle");
  dialog?.replaceChildren(...(handle ? [handle] : []));
  if (!force) previous?.trigger?.focus?.({ preventScroll: true });
}
async function manageSession(s, trigger = null) {
  if (!s || !state.authenticated || sessionControl?.busy) return;
  const bot = botRequestForNative(sourceOf(s));
  if (bot) { selectBotRequest(botRequestKey(bot)); return; }
  closeSessionControl(true);
  let dialog = $("session-control-dialog");
  if (!dialog) {
    dialog = el("dialog", "plan-dialog session-control-dialog"); dialog.id = "session-control-dialog";
    dialog.setAttribute("aria-labelledby", "session-control-title");
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeSessionControl(); });
    document.body.append(dialog);
  }
  const source = sourceOf(s);
  if (!source.id && s.agent === "claude" && s.kind === "interactive" && /^[0-9A-Za-z-]{8,64}$/.test(s.sessionId || "")) source.id = s.sessionId;
  const context = { source, s, trigger, authEpoch: state.authEpoch, busy: false, preview: null, used: false };
  sessionControl = context;
  const top = el("div", "dialog-top");
  const title = el("h2", "", "세션 관리"); title.id = "session-control-title";
  const close = button("닫기", "secondary", () => closeSessionControl()); close.id = "session-control-close";
  top.append(title, close);
  const body = el("div", "dialog-body session-control-body");
  const name = el("h3", "", cardTitle(s));
  body.append(name, el("p", "muted", `${s.hostLabel || s.host} · ${s.agent === "claude" ? "Claude" : "Codex"}`));
  const status = el("p", "session-control-status", "현재 실행 상태를 확인하고 있습니다…");
  status.id = "session-control-status"; status.setAttribute("role", "status");
  body.append(status);
  const actions = el("div", "session-control-actions");
  for (const [action, label, explanation] of [
    ["interrupt", "응답 중단", "현재 생성 중인 응답을 멈춥니다. 대화 기록은 유지됩니다."],
    ["terminate", "실행 종료", "이 세션의 원본 실행을 끝냅니다. 대화 기록은 남습니다."],
  ]) {
    const row = el("div", "session-control-row");
    const actionButton = button(label, action === "terminate" ? "secondary danger" : "secondary", () => confirmSessionAction(context, action));
    actionButton.id = `session-control-${action}`; actionButton.disabled = true;
    const reason = el("p", "muted", explanation); reason.id = `session-control-${action}-reason`;
    row.append(actionButton, reason); actions.append(row);
  }
  const archive = button("작업 보관", "secondary", () => archiveSessionWork(context, true));
  archive.id = "session-control-archive";
  actions.append(archive, el("p", "muted", "이 세션의 작업 카드를 보관합니다. 실행은 계속됩니다."));
  const away = sessionRemoved(s) || sessionTidied(s);
  const tidy = button(away ? "목록에 다시 표시" : "목록에서 지우기", "secondary", () => tidySession(context, !away));
  tidy.id = "session-control-tidy";
  actions.append(tidy, el("p", "muted", "실행과 대화 기록은 그대로 두고 목록에서 지웁니다. 답이 필요하거나 작업하는 동안에만 다시 보이고, 검색이나 '오래된 기록 포함'으로 볼 수 있습니다."));
  body.append(actions);
  const confirmation = el("div", "session-control-confirmation"); confirmation.id = "session-control-confirmation"; confirmation.hidden = true;
  body.append(confirmation); dialog.append(top, body);
  sheet(dialog.id).open();
  try {
    const result = await api("/api/session/preview", { method: "POST", body: { source: context.source }, authEpoch: context.authEpoch });
    if (sessionControl !== context) return;
    context.preview = result;
    status.textContent = "이 세션에서 사용할 동작을 선택하세요.";
    for (const action of ["interrupt", "terminate"]) {
      const capability = result.capabilities?.[action];
      $(`session-control-${action}`).disabled = !capability?.supported;
      if (!capability?.supported) $(`session-control-${action}-reason`).textContent = capability?.reason || "현재 실행에서 지원하지 않습니다.";
      else if (capability.effect) $(`session-control-${action}-reason`).textContent = capability.effect;
    }
  } catch (err) {
    if (sessionControl === context && !err.staleAuth && !err.unauthorized) status.textContent = err.message || "실행 상태를 확인하지 못했습니다.";
  }
}
function confirmSessionAction(context, action) {
  if (sessionControl !== context || context.busy || context.used || !context.preview?.capabilities?.[action]?.supported) return;
  const confirmation = $("session-control-confirmation"); confirmation.hidden = false;
  const label = action === "terminate" ? "실행 종료" : "응답 중단";
  const confirm = button(label, "primary", () => executeSessionAction(context, action));
  confirm.id = "session-control-confirm";
  const effect = context.preview.capabilities[action].effect || (action === "terminate" ? "이 세션의 원본 실행을 끝냅니다. 대화 기록은 남습니다." : "현재 생성 중인 응답을 멈춥니다. 대화 기록은 유지됩니다.");
  confirmation.replaceChildren(el("p", "", `${cardTitle(context.s)} · ${context.s.hostLabel || context.s.host}: ${effect}`),
    confirm, button("취소", "secondary", () => { confirmation.hidden = true; }));
  confirm.focus();
}
function sessionControlBusy(context, busy) {
  context.busy = busy;
  for (const item of $("session-control-dialog").querySelectorAll("button")) item.disabled = busy;
  if (!busy) for (const action of ["interrupt", "terminate"])
    $(`session-control-${action}`).disabled = context.used || !context.preview?.capabilities?.[action]?.supported;
}
async function executeSessionAction(context, action) {
  if (sessionControl !== context || context.busy || context.used) return;
  context.used = true;
  sessionControlBusy(context, true);
  $("session-control-status").textContent = "요청 결과를 확인하고 있습니다…";
  try {
    const result = await api("/api/session/action", { method: "POST", body: { source: context.source, action,
      targetProof: context.preview.targetProof, requestId: requestId() }, authEpoch: context.authEpoch, csrfRetry: false });
    if (sessionControl !== context) return;
    $("session-control-status").textContent = result.confirmed === true
      ? (action === "terminate" ? "이 세션의 실행 종료를 확인했습니다. 기록은 남아 있습니다." : "현재 응답이 중단된 것을 확인했습니다.")
      : result.reason || "처리 결과를 확인하지 못했습니다. 실행 상태를 다시 확인해 주세요. 자동으로 재요청하지 않습니다.";
    if (result.confirmed === true) {
      await window.SessionholicGoals?.refresh?.();
      poll(false);
    }
  } catch (err) {
    if (sessionControl === context && !err.staleAuth && !err.unauthorized)
      $("session-control-status").textContent = "처리 결과를 확인하지 못했습니다. 실행 상태를 다시 확인해 주세요. 요청을 자동으로 다시 보내지 않습니다.";
  } finally {
    if (sessionControl === context) {
      $("session-control-confirmation").hidden = true;
      sessionControlBusy(context, false);
    }
  }
}
async function archiveSessionWork(context, archived) {
  if (sessionControl !== context || context.busy) return;
  sessionControlBusy(context, true);
  try {
    await api("/api/session/archive", { method: "POST", body: { source: context.source, archived }, authEpoch: context.authEpoch });
    if (sessionControl !== context) return;
    $("session-control-status").textContent = archived ? "작업을 보관했습니다. 세션 실행은 계속됩니다." : "작업을 복원했습니다.";
    const archive = $("session-control-archive");
    const replacement = button(archived ? "보관 되돌리기" : "작업 보관", "secondary", () => archiveSessionWork(context, !archived));
    replacement.id = archive.id; archive.replaceWith(replacement);
    await window.SessionholicGoals?.refresh?.();
  } catch (err) {
    if (sessionControl === context && !err.staleAuth && !err.unauthorized) $("session-control-status").textContent = err.message;
  } finally { if (sessionControl === context) sessionControlBusy(context, false); }
}

// 목록에서 지우기 / 목록에 다시 표시 (session-removed; showing again clears a tidy from an end
// on the board too); the run is untouched.
async function tidySession(context, away) {
  if (sessionControl !== context || context.busy) return;
  sessionControlBusy(context, true);
  try {
    // Showing again clears both records whatever this sheet saw when it opened (each clear
    // is a no-op when there is nothing to clear).
    const source = seenSource(context.s);
    if (!away)
      await api("/api/session/tidy", { method: "POST", body: { source, tidied: false }, authEpoch: context.authEpoch });
    const result = await api("/api/session/remove", { method: "POST", body: { source, removed: away }, authEpoch: context.authEpoch });
    (state.removedLocal ||= new Map()).set(context.s.key, { removed: away, after: state.snapshot,
      version: Number.isInteger(result?.version) ? result.version : null });
    if (sessionControl !== context) return;
    if (!away) keepRestoredSession(context.s);
    $("session-control-status").textContent = away ? REMOVED_NOTE : "목록에 다시 표시합니다.";
    const tidy = $("session-control-tidy");
    const replacement = button(away ? "목록에 다시 표시" : "목록에서 지우기", "secondary", () => tidySession(context, !away));
    replacement.id = tidy.id; tidy.replaceWith(replacement);
    render();
    poll(false);
  } catch (err) {
    if (sessionControl === context && !err.staleAuth && !err.unauthorized) $("session-control-status").textContent = err.message;
  } finally { if (sessionControl === context) sessionControlBusy(context, false); }
}

function focusDetailBack() {
  $("detail").querySelector(".back")?.focus({ preventScroll: true });
}
function restoreSelectedCardFocus() {
  const card = state.selectedBotRequest
    ? [...$("groups").querySelectorAll(".bot-request-card")].find((node) => node.dataset.botRequestKey === state.selectedBotRequest)
    : [...$("groups").querySelectorAll(".card")].find((node) => node.dataset.sessionKey === state.selected);
  (card || $("home")).focus({ preventScroll: true });
}
function rememberConversationView() {
  const session = selectedSession();
  const list = $("messages");
  if (!session || !list || !list.conversationSource || !list.clientHeight || list.pendingView) return;
  const pinned = pinnedToEnd(list);
  let anchor = null;
  let occurrence = 0;
  let offset = 0;
  for (const item of list.conversationRows || []) {
    const box = rowBox(list, item.row);
    if (box && box.top + box.height > list.scrollTop) {
      anchor = item.key;
      const rows = list.conversationRows || [];
      occurrence = rows.slice(rows.indexOf(item) + 1).filter((row) => row.key === item.key).length;
      offset = box.top - list.scrollTop;
      break;
    }
  }
  state.conversationViews.set(conversationIdentity(session), {
    chromeVisible: state.chromeVisible, pinned, top: list.scrollTop, anchor, occurrence, offset,
  });
}
function restoreConversationView(list) {
  const view = list.pendingView;
  if (!view || !list.conversationRows?.length || !list.clientHeight) return;
  list.pendingView = null;
  list.pinned = view.pinned;
  const matches = list.conversationRows.filter((item) => item.key === view.anchor);
  const item = matches[Math.max(0, matches.length - 1 - (view.occurrence || 0))];
  const box = item && rowBox(list, item.row);
  setMessagesScroll(list, view.pinned ? list.scrollHeight : box ? box.top - view.offset : view.top);
}
function select(key) {
  if (state.launchBusy) return;
  // A docked terminal stays beside its own conversation; another one shows without it.
  if (state.docked && key !== state.dockBeside) undock();
  if (state.selected !== key) rememberConversationView();
  if (state.selected !== key) closeQuickReplies();
  const target = allSessions().find((session) => session.key === key);
  const bot = target && botRequestForNative(sourceOf(target));
  if (target && sessionUnread(target)) markSeen(target);
  if (target) markOpened(target);
  state.selectedBotRequest = bot ? botRequestKey(bot) : null;
  const listChanged = target && prepareSessionListFor(target);
  if (listChanged && state.selected === key) renderSessionList(allSessions(), state.snapshot?.serverTime || Date.now() / 1000);
  const mobile = phoneLayout();
  const entering = mobile && !state.detailShown;
  if (entering) navigateView("detail");
  if (state.selected === key) {
    // Moving this retained DOM through a hidden goal/session pane can reset its
    // scrollTop. Reapply the saved view only when reopening, not on a live click.
    if (!state.detailShown) {
      const list = $("messages");
      const session = selectedSession();
      const savedView = session && state.conversationViews.get(conversationIdentity(session));
      if (list && savedView) {
        list.pendingView = savedView;
        list.pinned = savedView.pinned;
        setChromeVisible(savedView.chromeVisible);
      }
    }
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
    s.cwd || null, s.sessionId || null, s.runtimeKnown, s.readOnly, s.initiator, s.initiatorName, s.initiatorEvidence, s.parentSessionId,
    s.executionMode, s.transcriptAvailability, s.historyNotice]);
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
  const [label, tone] = BADGES[s.runtimeKnown === false ? "unknown" : s.phase] || ["상태 확인 필요", "gray"];
  return [el("span", `badge ${tone}`, label),
    el("span", null, [AGENTS[s.agent], s.account, s.hostLabel].filter(Boolean).join(" · ")),
    el("span", null, initiatorLabel(s)), ...(sessionOneShot(s) ? [el("span", "detail-execution", "일회성 실행")] : []), el("span", null, clock(s.updatedAt)),
    // Why a PC record's state is what it is (its project server off, a codex exec running).
    ...(typeof s.runtimeReason === "string" && s.runtimeReason ? [el("span", "detail-runtime", s.runtimeReason)] : [])];
}
function patchDetailMetadata(s) {
  const pane = $("detail");
  if (pane.dataset.key !== s.key) return;
  const title = pane.querySelector(".detail-title");
  const eyebrow = pane.querySelector(".detail-project");
  if (title) title.textContent = s.title || "제목 없는 작업";
  const pin = $("detail-pin");
  if (pin) { pin.textContent = sessionPinned(s) ? "★ 고정 해제" : "☆ 상단 고정"; pin.setAttribute("aria-pressed", String(sessionPinned(s))); }
  if (eyebrow) eyebrow.textContent = s.project || "";
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
    if (!data.error && data.executionMode === "one-shot" && entry.error !== entry.inputError) entry.error = "";
    // The host said its daemon is off even if the list read just before did not.
    const shown = data.code === "app_server_off" ? { ...s, appServer: "off" } : s;
    renderMessages(shown, messages, data.error || "", fresh && !usableCache);
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
  if ($("board-status")) $("board-status").open = false;
  const pane = $("detail");
  pane.replaceChildren();
  pane.dataset.key = s.key;
  const head = el("div", "detail-head");
  head.id = "detail-head";
  const back = button("←", "back", backToList);
  back.setAttribute("aria-label", "대화에서 돌아가기");
  head.append(back);
  const titles = el("div", "detail-titles");
  titles.append(
    el("h1", "detail-title", s.title || "제목 없는 작업"),
    el("span", "detail-device", s.hostLabel || s.host || "기기 미확인"),
    el("span", "detail-project", s.project || ""),
  );
  const modelSummary = el("span", "chat-model-summary", "모델·에포트 확인 중…");
  modelSummary.id = "chat-model-summary";
  titles.append(modelSummary);
  const settings = el("div", "chat-model-controls");
  for (const kind of ["model", "effort"]) {
    const control = button("확인 중…", "chat-model-control", () => openChatSettings(chatEntry(s), kind));
    control.id = `chat-${kind}-control`;
    control.setAttribute("aria-label", `${kind === "model" ? "모델" : "에포트"} 확인 중`);
    control.setAttribute("aria-expanded", "false");
    for (const type of ["pointerdown", "mousedown"]) control.addEventListener(type, (event) => {
      if (touchDevice() && editingText()) event.preventDefault();
    });
    settings.append(control);
  }
  const selectionNote = el("span", "chat-model-selection-note");
  selectionNote.id = "chat-model-selection-note";
  selectionNote.setAttribute("role", "status");
  selectionNote.hidden = true;
  settings.append(selectionNote);
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
  head.append(settings);
  const headActions = el("div", "detail-head-actions");
  const reload = button("↻", "detail-reload icon-button", hardReload);
  reload.id = "detail-reload";
  reload.title = "캐시 지우고 앱 새로고침";
  reload.setAttribute("aria-label", "캐시 지우고 앱 새로고침");
  reload.disabled = !!state.hardReloadBusy;
  for (const type of ["pointerdown", "mousedown"]) reload.addEventListener(type, (event) => {
    if (touchDevice() && editingText()) event.preventDefault();
  });
  headActions.append(reload);
  const focusToggle = button("집중", "focus-toggle quiet", toggleChrome);
  focusToggle.id = "focus-toggle";
  // Keep the keyboard and compact header still until the click lands.
  focusToggle.addEventListener("pointerdown", (event) => event.preventDefault());
  focusToggle.setAttribute("aria-pressed", "false");
  headActions.append(focusToggle);
  head.append(headActions);
  window.SessionholicGoals?.attachSessionDetail?.(s, head);
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
  const conversationActions = el("div", "conversation-actions");
  const manage = button("세션 관리", "", (event) => {
    if (state.authenticated && state.selected === s.key) manageSession(s, event.currentTarget);
  });
  manage.id = "detail-manage-session";
  const pin = button(sessionPinned(s) ? "★ 고정 해제" : "☆ 상단 고정", "", () => pinSession(selectedSession() || s));
  pin.id = "detail-pin"; pin.setAttribute("aria-pressed", String(sessionPinned(s)));
  conversationActions.append(pin);
  if (window.SessionholicLabels?.eligible?.(s)) {
    const editTitle = button("제목 수정", "", (event) => {
      if (state.selected === s.key) window.SessionholicLabels.open(selectedSession() || s, event.currentTarget);
    });
    editTitle.id = "detail-edit-title";
    conversationActions.append(editTitle);
  }
  conversationActions.append(manage, button("맨 아래 ↓", "", () => scrollMessagesToEnd()));
  bar.append(el("strong", null, "최근 대화"), conversationActions);
  const list = el("div", "messages");
  list.id = "messages";
  const savedView = state.conversationViews.get(conversationIdentity(s));
  list.pendingView = savedView || null;
  list.pinned = savedView ? savedView.pinned : true;
  list.lastScrollTop = 0;
  observeMessages(list);
  list.setAttribute("aria-label", "최근 대화 읽기");
  const loading = progress("대화를 불러오고 있습니다…");
  loading.classList?.add?.("conversation-loading");
  loading.setAttribute?.("role", "status");
  list.append(loading);
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
  // A conversation opens in focus mode; the same tab reopening it keeps how it was left.
  state.chromeVisible = savedView ? savedView.chromeVisible : !CONVERSATION_OPENS_FOCUSED;
  pane.classList?.remove("is-reading");
  pane.classList?.remove("is-typing");
  // Shown once on a touch screen, the first time the bars are folded away.
  const hint = el("p", "tap-hint", "화면을 탭하면 메뉴가 나타나요");
  hint.id = "tap-hint";
  hint.hidden = true;
  hint.setAttribute("role", "status");
  const prompt = el("section", "chat-prompt");
  prompt.id = "chat-prompt";
  prompt.hidden = true;
  prompt.setAttribute("aria-live", "polite");
  pane.append(bar, sync, list, jump, hint, prompt,
    ...(sessionCategoryOf(s) === "bot" ? [botWatchNotice(s)] : [buildChatComposer(s), buildContinuePanel(s)]));
  setChromeVisible(state.chromeVisible);
  if (!state.chromeVisible && touchDevice()) showTapHint();
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
  const asked = state.answerOnOpen;
  if (asked?.key === s.key && (prompt || Date.now() > asked.until)) state.answerOnOpen = null;
  else if (asked?.key === s.key && openable && !entry.promptBusy && !entry.promptNote && !networkOffline()) {
    state.answerOnOpen = null;
    Promise.resolve().then(() => promptAction(s, { action: "open" }));
  }
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
  // The choice that had the keyboard focus keeps it across this redraw (or the one an arrow moved to).
  const focusedNumber = Number(document.activeElement?.dataset?.promptNumber) || null;
  const focusNumber = entry.promptFocus ?? focusedNumber;
  entry.promptFocus = null;
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
  const items = new Map();
  for (const [index, option] of inline.entries()) {
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
    // One stop in the tab order; arrows move the choice, as in any radio group.
    item.dataset.promptNumber = String(option.number);
    item.tabIndex = (entry.promptChoice ? chosen : index === 0) ? 0 : -1;
    item.addEventListener?.("keydown", (event) => {
      const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
      if (!step || !selectable || entry.promptBusy) return;
      event.preventDefault?.();
      const next = inline[(index + step + inline.length) % inline.length];
      entry.promptChoice = { fingerprint: prompt.fingerprint, number: next.number };
      entry.promptFocus = next.number;
      haptic("select");
      renderPrompt(s, entry.data);
    });
    items.set(option.number, item);
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
  if (focusNumber != null) items.get(focusNumber)?.focus?.({ preventScroll: true });
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
// toggles them on touch devices. Mouse users restore with the focus button.
const CHROME_TAP_IGNORE = "a, button, input, textarea, select, label, summary, [role=button], " +
  "pre, code, .tool-group, .msg-more, .message-block-copy, .message-code-toggle, .chat-prompt, .msg-attachment, .chat-attachment, img, video";
const TAP_HINT_MS = 2500;
function chatTyping() {
  return document.activeElement?.id === "chat-input";
}
function messagesAtEnd(list, slack = 50) {
  return !!list && list.scrollHeight - list.scrollTop - list.clientHeight < slack;
}
// Whether the conversation stays at its newest message. A newly opened one does
// (drawDetail), "맨 아래" sets it again, and only the reader's own scroll away
// from the end clears it — not a hidden list (a phone's detail before it shows),
// a question card, the keyboard or a growing composer, which change the list's
// size but not the reader's place. A scroll the board has not seen yet (no
// scroll event since its own) decides afresh by the distance.
function pinnedToEnd(list) {
  if (!list) return false;
  if (list.scrollTop !== list.lastScrollTop) list.pinned = messagesAtEnd(list);
  return list.pinned === true;
}
// The list's own size changed (shown, a card, the keyboard, the bars): a pinned
// conversation is put back at its newest message.
function keepAtEnd(list) {
  if (list?.pendingView) { restoreConversationView(list); return; }
  if (!list || list.isConnected === false || !list.clientHeight) return;
  if (!pinnedToEnd(list)) {
    renderJump();
    return;
  }
  setMessagesScroll(list, list.scrollHeight);
  list.unread = 0;
  renderJump();
}
function observeMessages(list) {
  if (typeof ResizeObserver === "undefined") return;
  state.messagesObserver ||= new ResizeObserver((entries) => {
    for (const entry of entries) keepAtEnd(entry.target);
  });
  state.messagesObserver.disconnect();
  state.messagesObserver.observe(list);
}
// The explicit choice survives keyboard, network, receipt and viewport changes.
function setChromeVisible(visible, { stickBottom = false } = {}) {
  state.chromeVisible = !!visible;
  setReading(!visible, { stickBottom });
  const toggle = $("focus-toggle");
  if (toggle) {
    toggle.textContent = visible ? "집중" : "전체 보기";
    toggle.title = visible ? "대화 집중 모드" : "집중 모드 해제";
    toggle.setAttribute("aria-pressed", String(!visible));
    toggle.setAttribute("aria-label", visible ? "대화 집중 모드" : "집중 모드 해제");
  }
  rememberConversationView();
}
function toggleChrome() {
  const hint = $("tap-hint");
  if (hint && !hint.hidden) hint.hidden = true;
  setChromeVisible(!state.chromeVisible, { stickBottom: messagesAtEnd($("messages")) });
}
// Attention stays beside the input. It never overrides the reader's choice.
function revealChrome() {
  $("chat-status")?.setAttribute?.("data-attention", "true");
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
  // Below the header once the conversation is laid out (a phone still shows the list when a
  // conversation is drawn); until then the stylesheet's place.
  const place = () => {
    const list = $("messages");
    if (hint.style && list && list.offsetParent !== null && Number.isFinite(list.offsetTop) && list.offsetTop > 0)
      hint.style.top = `${list.offsetTop + 12}px`;
  };
  place();
  globalThis.requestAnimationFrame?.(place);
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
// 집중 모드 adds 빠른 답장 to the input's row: a short hint keeps it on one line on a phone.
function chatPlaceholder(oneShot) {
  if (oneShot) return "후속 메시지 없음";
  return $("detail")?.classList?.contains?.("is-reading") ? "메시지…" : "이 세션에 보낼 메시지…";
}
function setReading(reading, { stickBottom = false } = {}) {
  const pane = $("detail");
  if (!pane?.classList || pane.classList.contains?.("is-reading") === reading) return;
  const list = $("messages");
  const before = list?.getBoundingClientRect?.().top;
  pane.classList.toggle?.("is-reading", reading);
  const chatInput = $("chat-input");
  if (chatInput && chatInput.placeholder !== "후속 메시지 없음") chatInput.placeholder = chatPlaceholder(false);
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
  const before = list?.getBoundingClientRect?.().top;
  const restore = !typing && list && list.typingFrom && list.scrollTop === list.typingFrom.set ? list.typingFrom.top : null;
  const from = list?.scrollTop;
  if (typing) pane.classList.add("is-typing");
  else pane.classList.remove("is-typing");
  if (list && atBottom) setMessagesScroll(list, list.scrollHeight);
  // Back from typing with no scroll since: exactly the place it had (the shift into the
  // typing layout may have been cut short at the top).
  else if (list && restore !== null) setMessagesScroll(list, restore);
  else if (list && typeof before === "number") setMessagesScroll(list, list.scrollTop + list.getBoundingClientRect().top - before);
  if (list) list.typingFrom = typing && !atBottom ? { top: from, set: list.scrollTop } : null;
  renderJump();
}
// The viewport changed (an on-screen keyboard opened or closed, a rotation, a
// split view): only the temporary typing layout follows the available space.
// The reader's focus choice is independent of the keyboard and viewport.
function syncChromeMode() {
  const compact = compactChrome();
  setTyping(chatTyping() && compact);
  const hint = $("tap-hint");
  if (!touchDevice() && hint && !hint.hidden) hint.hidden = true;
}
function readingScroll(list) {
  const top = list.scrollTop;
  const previous = list.lastScrollTop ?? top;
  list.lastScrollTop = top;
  // Pinned to the end unless the reader moved away: a resize or the board's
  // smooth "맨 아래" also scrolls, without unpinning.
  const height = list.clientHeight;
  const resized = Number.isFinite(list.pinHeight) && list.pinHeight !== height;
  list.pinHeight = height;
  if (messagesAtEnd(list)) list.pinned = true;
  else if (top !== previous && !resized && Date.now() >= (list.autoScrollUntil || 0)) list.pinned = false;
  const typing = chatTyping();
  phoneReadingScroll(list, top - previous, typing || resized);
  rememberConversationView();
  renderJump();
}
// Phones: a deliberate scroll either way hides the bars; only a tap (or
// the explicit restore button) shows them again, even at the end. `keep`
// (typing or a resize) records the scroll without hiding.
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
  if (touchDevice()) showTapHint();
}
function scrollMessagesToEnd() {
  const list = $("messages");
  if (!list) return;
  list.unread = 0;
  list.pinned = true;
  if (typeof list.scrollTo === "function") {
    // The smooth scroll's own events do not hide the bars; a touch ends this.
    list.autoScrollUntil = Date.now() + 1000;
    list.scrollTo({ top: list.scrollHeight, behavior: prefersReducedMotion() ? "auto" : "smooth" });
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
    messages, historyNotice: data.historyNotice || null, executionMode: data.executionMode,
    transcriptAvailability: data.transcriptAvailability, fetchedAt: data.fetchedAt || Date.now() / 1000, stale: !!data.stale };
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
  const labels = { accepted: "전송 접수", queued: "전송 접수", completed: "응답 종료",
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
  const draftIdentity = JSON.stringify([source, s.agent === "claude" ? s.sessionId || s.id : s.id]);
  const previous = state.chats.get(key);
  if (previous && previous.draftIdentity !== draftIdentity) {
    if (s.agent === "claude" && s.sessionId && !previous.draftTranscript) {
      // A first transcript UUID enriches the same worker, including its in-flight sends.
      const oldKey = draftStorageKey("chat", previous.draftIdentity), oldShape = state.draftSavedShapes.get(oldKey);
      const oldRow = readSavedDraft(oldKey);
      previous.draftIdentity = draftIdentity; previous.draftTranscript = s.sessionId;
      if (persistChatDraft(previous) && oldRow && oldShape === savedDraftShape(oldRow)) {
        try { localStorage.removeItem(oldKey); } catch { /* keep the older recovery record */ }
      }
      state.draftSavedShapes.delete(oldKey);
    } else {
      persistChatDraft(previous);
      state.chats.delete(key);
    }
  }
  if (!state.chats.has(key)) {
    const entry = {
    source: Object.freeze({ ...source }), text: "", attachments: [], data: null, conversation: null,
    fullMessages: new Map(), historyPreview: null,
    pending: null, receipt: null, sending: false, uploads: 0, outbox: [], outboxClaims: [], uploadQueue: [], thumbs: new Map(),
    composing: false, editorEpoch: 0, stateEpoch: 0, recheckQueued: false, expanded: false, inputCollapsed: false, checking: false, error: "", notice: "", revision: 0,
      draftIdentity, draftAuthEpoch: state.authEpoch, draftTranscript: s.agent === "claude" ? s.sessionId || null : null,
    };
    state.chats.set(key, entry);
    restoreChatDraft(entry);
  }
  return state.chats.get(key);
}
function currentChat(entry) {
  const selected = selectedSession();
  return !!selected && chatEntry(selected) === entry && state.authenticated;
}
function networkOffline() {
  return state.offline || globalThis.navigator?.onLine === false;
}
const DRAFT_PREFIX = INSTANCE.storagePrefix + "draft:v1:";
const DRAFT_TTL = 7 * 24 * 60 * 60 * 1000;
function draftRecoveryEnabled() { return prefs.get("draftRecovery", true) === true; }
function savedDraftKeys() {
  try {
    return Array.from({ length: Math.min(localStorage.length || 0, 10000) }, (_, index) => localStorage.key(index))
      .filter((key) => typeof key === "string" && key.startsWith(DRAFT_PREFIX));
  } catch { return []; }
}
function draftStorageKey(kind, identity) { return DRAFT_PREFIX + encodeURIComponent(JSON.stringify([kind, identity])); }
function clearSavedDrafts() {
  state.draftSavedShapes.clear();
  for (const key of savedDraftKeys()) { try { localStorage.removeItem(key); } catch { /* unavailable storage */ } }
}
function draftStorageNotice(text) {
  if ($("draft-recovery-status")) $("draft-recovery-status").textContent = text;
}
function readSavedDraft(key) {
  if (!state.authenticated || !draftRecoveryEnabled()) return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw || raw.length > 150000) return null;
    const row = JSON.parse(raw);
    if (row?.version !== 1 || typeof row.text !== "string" || new TextEncoder().encode(row.text).length > 32768 ||
        !Number.isFinite(row.at) || row.at > Date.now() + 60000 || Date.now() - row.at > DRAFT_TTL ||
        !Array.isArray(row.attachments) || row.attachments.length > 8 ||
        row.attachments.some((item) => !item || typeof item.id !== "string" || item.id.length > 128) ||
        row.pending && (typeof row.pending.requestId !== "string" || row.pending.requestId.length > 128 ||
          !Number.isInteger(row.pending.revision) || row.pending.revision < 0 ||
          typeof row.pending.text !== "string" || new TextEncoder().encode(row.pending.text).length > 32768 ||
          !Array.isArray(row.pending.attachments) || row.pending.attachments.length > 8 ||
          row.pending.attachments.some((id) => typeof id !== "string" || id.length > 128)) ||
        row.commandRequest && (typeof row.commandRequest.requestId !== "string" || row.commandRequest.requestId.length > 128 ||
          !['model', 'effort', 'compact'].includes(row.commandRequest.command) ||
          !['pending', 'unknown', 'applied'].includes(row.commandRequest.status) ||
          row.commandRequest.value !== null && (typeof row.commandRequest.value !== "string" || row.commandRequest.value.length > 128))) return null;
    return row;
  } catch { return null; }
}
function savedDraftShape(row) {
  return JSON.stringify({ text: row.text, attachments: row.attachments, pending: row.pending ?? null,
    revision: row.revision || 0, commandRequest: row.commandRequest ?? null });
}
function writeSavedDraft(key, row) {
  if (!state.authenticated || !draftRecoveryEnabled() || state.discardOnReload) return false;
  try {
    if (typeof localStorage.setItem !== "function" || typeof localStorage.removeItem !== "function") return false;
    const shape = savedDraftShape(row), previousShape = state.draftSavedShapes.get(key);
    // Status polling in an idle tab must never overwrite another tab's newer edits.
    if (previousShape === shape && localStorage.getItem(key) !== null) return true;
    if (!row.text && !row.attachments.length && !row.pending) {
      const prior = readSavedDraft(key);
      if (!prior || previousShape === savedDraftShape(prior)) localStorage.removeItem(key);
      state.draftSavedShapes.set(key, shape); return true;
    }
    if (new TextEncoder().encode(row.text).length > 32768) throw new Error("draft size");
    const raw = JSON.stringify({ ...row, version: 1, at: Date.now() });
    if (raw.length > 150000) throw new Error("draft size");
    const prior = readSavedDraft(key);
    if (prior && JSON.stringify({ ...prior, at: null }) === JSON.stringify({ ...row, version: 1, at: null })) return true;
    const newKey = localStorage.getItem(key) === null;
    localStorage.setItem(key, raw);
    state.draftSavedShapes.set(key, shape);
    if (newKey || !state.draftPrunedAt || Date.now() - state.draftPrunedAt > 30000) {
      const keys = savedDraftKeys().map((name) => ({ name, at: readSavedDraft(name)?.at || 0 })).sort((a, b) => b.at - a.at);
      for (const item of keys.slice(50)) localStorage.removeItem(item.name);
      state.draftPrunedAt = Date.now();
    }
    draftStorageNotice("이 기기에 초안을 보관합니다. 잠그거나 로그인이 만료되면 삭제합니다.");
    return true;
  } catch {
    // A draft saved before this send must not come back as a new, sendable request.
    state.draftSavedShapes.delete(key);
    if (row.pending) { try { localStorage.removeItem(key); } catch { /* unavailable storage */ } }
    draftStorageNotice("초안을 이 기기에 보관하지 못했습니다. 새로고침 전 작성 내용을 복사해 주세요.");
    return false;
  }
}
function persistChatDraft(entry) {
  if (!entry?.draftIdentity || entry.draftAuthEpoch !== state.authEpoch) return false;
  return writeSavedDraft(draftStorageKey("chat", entry.draftIdentity), chatDraftPayload(entry));
}
function chatDraftPayload(entry) {
  const pending = entry.pending ? { requestId: entry.pending.requestId, revision: entry.pending.revision,
    text: entry.pending.text, attachments: entry.pending.attachments,
    attachmentDetails: attachmentMetadata(entry.pending.attachmentDetails) } : null;
  return { text: entry.text || "", attachments: attachmentMetadata(entry.attachments), pending, revision: entry.revision || 0,
    commandRequest: entry.commandRequest ? { requestId: entry.commandRequest.requestId, command: entry.commandRequest.command,
      value: entry.commandRequest.value ?? null, status: entry.commandRequest.status } : null };
}
function restoreChatDraft(entry) {
  const key = draftStorageKey("chat", entry.draftIdentity), row = readSavedDraft(key);
  if (!row) { state.draftSavedShapes.set(key, savedDraftShape(chatDraftPayload(entry))); return; }
  entry.text = row.text; entry.attachments = attachmentMetadata(row.attachments);
  entry.revision = Number.isInteger(row.revision) && row.revision >= 0 ? row.revision : 0;
  if (row.commandRequest) {
    const command = row.commandRequest;
    const canonical = `/${command.command}${command.value === null ? "" : " " + command.value}`;
    if (command.status === "applied") {
      if (entry.text.trim().replace(/\s+/g, " ") === canonical) { entry.text = ""; entry.revision++; }
    } else {
      entry.commandRequest = { ...command, status: "unknown" };
      entry.notice = "확인 중이던 명령을 복원했습니다. 같은 명령을 다시 확인할 때 기존 요청 번호를 사용합니다.";
    }
  }
  if (row.pending) {
    entry.pending = { ...row.pending, source: entry.source, attachmentDetails: attachmentMetadata(row.pending.attachmentDetails) };
    entry.receipt = { requestId: row.pending.requestId, status: "unknown" };
    entry.outbox = [{ requestId: row.pending.requestId, text: row.pending.text,
      attachments: entry.pending.attachmentDetails, attachmentObjects: entry.attachments,
      state: "uncertain", status: "unknown", reason: "", at: row.at }];
    entry.notice = "전송 확인 중이던 메시지를 복원했습니다. 다시 보내지 않고 전송 상태를 확인합니다.";
  } else if (!entry.notice) entry.notice = "이 기기에 보관한 초안을 복원했습니다.";
  state.draftSavedShapes.set(key, savedDraftShape(chatDraftPayload(entry)));
}
function terminalDraftIdentity(terminal) { return JSON.stringify([terminal?.id, draftKey(terminal)]); }
function persistTerminalDraft(terminal) {
  if (!terminal?.id) return false;
  // Terminal inputs have a separate idempotent queue; retain a locked marker while it drains.
  const key = draftStorageKey("terminal", terminalDraftIdentity(terminal));
  const text = state.drafts.get(draftKey(terminal)) || "";
  const prior = readSavedDraft(key);
  const uncertain = state.draftPaste?.id === terminal.id && state.draftPaste.dispatched || prior?.pending && prior.text === text;
  const pending = uncertain ? { requestId: "terminal-input-pending", revision: 0,
    text: state.drafts.get(draftKey(terminal)) || "", attachments: [] } : null;
  return writeSavedDraft(key, {
    text: state.drafts.get(draftKey(terminal)) || "", attachments: [], pending, revision: 0 });
}
function restoreTerminalDraft(terminal) {
  if (state.drafts.has(draftKey(terminal))) return;
  const row = readSavedDraft(draftStorageKey("terminal", terminalDraftIdentity(terminal)));
  if (row && !row.pending) state.drafts.set(draftKey(terminal), row.text);
  else if (row?.pending) draftStorageNotice("전송 중이던 터미널 입력은 자동 복원하지 않습니다. 터미널에서 반영 여부를 확인해 주세요.");
}
function persistAllDrafts() {
  if (!state.authenticated || !draftRecoveryEnabled()) return false;
  if (state.terminal) saveDraft();
  let saved = true;
  for (const entry of state.chats.values()) saved = persistChatDraft(entry) && saved;
  for (const terminal of state.terminals) if (state.drafts.has(draftKey(terminal))) saved = persistTerminalDraft(terminal) && saved;
  return saved && ![...state.chats.values()].some((entry) => entry.uploads || entry.uploadQueue?.length);
}
function chatVisible() {
  return !document.hidden && !boardCovered() && (!phoneLayout() || state.detailShown);
}
function chatHostOffline(entry) {
  const session = selectedSession();
  return state.snapshot?.hosts?.find((host) => host.name === entry.source.host)?.ok === false ||
    capsHost(entry.source.host)?.online === false ||
    (session && chatKey(sourceOf(session)) === chatKey(entry.source) && session.online === false);
}
function chatOneShot(entry) {
  if (chatKey(entry.data?.route || {}) === chatKey(entry.source) && sessionOneShot(entry.data)) return true;
  return allSessions().some((session) => chatKey(sourceOf(session)) === chatKey(entry.source) &&
    (sessionOneShot(session) || entry.conversation?.identity === conversationIdentity(session) && sessionOneShot(entry.conversation)));
}
function conversationHistoryNotice(s, entry) {
  const data = entry?.data && chatKey(entry.data.route || {}) === chatKey(sourceOf(s)) ? entry.data : null;
  const cached = entry?.conversation?.identity === conversationIdentity(s) ? entry.conversation : null;
  // The newest successful read owns this notice, including its absence. A
  // persisted transcript can predate a run that has persistence disabled.
  const notice = cached ? cached.historyNotice : data ? data.historyNotice : s.historyNotice;
  if (notice?.code === "not_persisted") return typeof notice.message === "string" && notice.message.trim() ? notice.message : ONE_SHOT_HISTORY;
  return "";
}
function chatRecovery(entry) {
  if (botRequestForNative(entry.source)) return null;
  if (chatHostOffline(entry)) return { supported: true, action: "refresh_host",
    reason: chatOneShot(entry) ? "기기에 연결되지 않았습니다. 연결을 확인한 뒤 실행 상태를 확인해 주세요." : "기기에 연결되지 않았습니다. 연결을 확인한 뒤 같은 세션을 이어갈 수 있습니다." };
  if (chatOneShot(entry)) return null;
  if (chatKey(entry.data?.route || {}) !== chatKey(entry.source)) return null;
  const recovery = entry.data?.recovery;
  if (recovery?.supported !== true || recovery.closedByBoard === true) return null;
  return ["resume_session", "refresh_host"].includes(recovery.action) ? recovery : null;
}
function chatSupported(entry) {
  return !botRequestForNative(entry.source) && !chatOneShot(entry) && !chatHostOffline(entry) && entry.data?.capability?.supported === true &&
    chatKey(entry.data.route || {}) === chatKey(entry.source);
}
// The server's capability says sending needs a board terminal the user opens.
function terminalRequired(entry) {
  return !chatOneShot(entry) && entry.data?.capability?.action === "open_terminal" && chatKey(entry.data.route || {}) === chatKey(entry.source);
}
function chatAttachmentsSupported(entry) {
  return !botRequestForNative(entry.source) && !chatOneShot(entry) && !!entry.data && chatKey(entry.data.route || {}) === chatKey(entry.source) &&
    entry.data.phase !== "unavailable" && entry.data.capability?.attachments !== false;
}
// ---------- 음성 입력 ----------
// The browser's own speech recognition (on an iPad or iPhone Apple's, in Chrome on Android
// or a computer Google's; it differs by browser); the board adds no model or service of its
// own. Words land at the cursor as they are heard and are never sent by themselves.
// Changing the text another way, a send, or the field leaving the screen (another session,
// the list, a hidden page) ends the listening. A recognition that never starts, falls silent
// or ends at once (as on some home-screen web apps) is stopped and said so.
const DICTATION_LANG = "ko-KR";
const DICTATION_PREPARING = "마이크 준비 중…";
const DICTATION_LISTENING = "듣는 중 · 끝내려면 마이크를 다시 누르세요";
const DICTATION_ENDING = "받아쓴 글을 마무리하고 있습니다…";
// iPad traces: a recognition whose audio starts within this long of being asked to heard
// nothing at all (every such start; working starts took about 200 ms): the platform hands it
// a capture that gives no sound, and says nothing about it. Such a start ends the listening
// at once instead of after the silence. Neither waiting (12 s) nor a fresh recognition, even
// in a new document, got past it; reloading the page or about 40 s did.
const DICTATION_STALE_START_MS = 100;
const DICTATION_STUCK = "iPad 음성 인식이 소리를 받지 못하는 상태입니다. 잠시 뒤 다시 누르거나 보드를 새로고침해 주세요.";
// It comes right after a listening ended (the second use in a row). So on a WebKit touch
// device ending a listening (the mic pressed again, a send, a command, switching fields) only
// parks it: the engine keeps running, with the platform's microphone indicator on, and
// nothing it hears is written until the mic is pressed again (it goes on at once, in any
// field) or this long passes. The mic pressed again first lets the last words land.
const DICTATION_PARK_MS = 60000;
const DICTATION_PARK_SETTLE_MS = 800;
const DICTATION_PARKED = "마이크 대기 중 · 1분 안에 다시 누르면 바로 이어서 받아씁니다";
function webkitTouchDictation() {
  const agent = String(globalThis.navigator?.userAgent || "");
  return (touchDevice() || (globalThis.navigator?.maxTouchPoints || 0) > 0)
    && /AppleWebKit/.test(agent) && /Macintosh|iPad|iPhone|iPod/.test(agent) && !/Android/i.test(agent);
}
// The status words that last only while a listening runs (the field's own note clears them).
function dictationTransient(text) {
  return [DICTATION_PREPARING, DICTATION_LISTENING, DICTATION_ENDING, DICTATION_PARKED].includes(text);
}
const DICTATION_STALLED = "음성 인식이 응답하지 않아 멈췄습니다. 들은 글은 남겨 두었습니다. 다시 누르거나 키보드의 받아쓰기를 써 주세요.";
const DICTATION_NOT_STARTED = "음성 인식이 시작되지 않아 멈췄습니다. 권한 창이 떴다면 허용한 뒤 마이크를 다시 누르거나, 키보드의 받아쓰기를 써 주세요.";
const DICTATION_SILENCE_END = "말이 없어 듣기를 끝냈습니다. 이어서 말하려면 마이크를 다시 누르세요.";
const DICTATION_ENDED_AT_ONCE = "음성 인식이 바로 끝났습니다. 이 화면(홈 화면 앱 등)에서 막혀 있을 수 있습니다. 브라우저에서 열거나 키보드의 받아쓰기를 써 주세요.";
const DICTATION_INTERRUPTED = "음성 인식이 중간에 끝났습니다. 들은 글은 남겨 두었습니다. 다시 눌러 이어서 말해 주세요.";
const DICTATION_CHANGED = "입력창의 글이 바뀌어 음성 입력을 멈췄습니다. 다시 누르면 이어서 받아씁니다.";
const DICTATION_STOP_WAIT_MS = 8000;
// Checked once a second: no start within this many seconds (a permission prompt included),
// or nothing heard for this many after it, ends the listening.
const DICTATION_START_TICKS = 15;
const DICTATION_SILENCE_TICKS = 10;
// Android's recognizer service may end a session after an utterance though Chrome asked it for
// continuous dictation; the board starts it again (this many times in a row with nothing heard
// between) unless the user ended it, it left the screen, it was refused, or nothing was said
// for the silence above.
const DICTATION_RESTARTS = 30;
const DICTATION_HARD_ERRORS = new Set(["not-allowed", "service-not-allowed", "audio-capture", "network", "language-not-supported"]);
const DICTATION_ERRORS = {
  "not-allowed": "마이크 사용이 허용되지 않았습니다. 브라우저의 사이트 권한이나 기기 설정(마이크·음성 인식)에서 허용해 주세요.",
  "service-not-allowed": "이 화면에서는 음성 인식을 쓸 수 없습니다. 브라우저에서 열거나 키보드의 받아쓰기를 쓰고, 기기 설정에서 받아쓰기가 켜져 있는지 확인해 주세요.",
  "audio-capture": "마이크를 찾지 못했습니다. 다른 앱이 마이크를 쓰고 있는지 확인해 주세요.",
  network: "음성 인식 서비스에 연결하지 못했습니다. 인터넷 연결과, 이 브라우저가 음성 인식을 지원하는지 확인해 주세요.",
  "no-speech": "말소리를 듣지 못했습니다. 마이크를 다시 눌러 말해 주세요.",
  "language-not-supported": "이 기기에서는 한국어 음성 인식을 쓸 수 없습니다.",
};
const MIC_ICON = '<svg aria-hidden="true" viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="2.5" width="6" height="10" rx="3"/>' +
  '<path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg>';
// 빠른 답장: a speech bubble with a quick line.
const QUICK_ICON = '<svg aria-hidden="true" viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4.5h12a1.5 1.5 0 0 1 1.5 1.5v6.5a1.5 1.5 0 0 1-1.5 1.5H9l-3.5 2.5V14H4a1.5 1.5 0 0 1-1.5-1.5V6A1.5 1.5 0 0 1 4 4.5Z"/>' +
  '<path d="M6.5 8.25h7M6.5 10.75h4"/></svg>';
function speechRecognitionClass() {
  return globalThis.SpeechRecognition || globalThis.webkitSpeechRecognition || null;
}
// The error code is kept in the message: it is what tells one platform's refusal from another's.
function dictationErrorText(code) {
  return `${DICTATION_ERRORS[code] || "음성 입력을 마치지 못했습니다. 다시 눌러 주세요."} (${code})`;
}
// What was heard so far: the results in order, final and still changing. Android's Chrome
// may give each result everything heard before it; there (only) a result that repeats all
// the earlier words (spacing and punctuation aside) up to a word boundary replaces them.
function dictationText(results, { cumulative = dictationCumulative() } = {}) {
  let text = "";
  for (const result of Array.from(results || [])) {
    const part = String(result?.[0]?.transcript || "").replace(/\s+/g, " ").trim();
    if (!part) continue;
    if (cumulative && text && dictationRepeats(text, part)) text = part;
    else text = text ? `${text} ${part}` : part;
  }
  return text;
}
function dictationRepeats(text, part) {
  const word = (c) => /[\p{L}\p{N}]/u.test(c);
  const chars = [...part];
  let i = 0;
  for (const c of text) {
    if (!word(c)) continue;
    while (i < chars.length && !word(chars[i])) i++;
    if (i >= chars.length || chars[i].toLowerCase() !== c.toLowerCase()) return false;
    i++;
  }
  return i >= chars.length || !word(chars[i]);
}
function dictationCumulative() {
  return /Android/i.test(String(globalThis.navigator?.userAgent || ""));
}
function dictationListening(field) {
  return !!state.dictation && (!field || state.dictation.field === field);
}
// A pending restart is distinct from listening; another press cancels it.
function renderDictation() {
  const supported = !!speechRecognitionClass();
  for (const [id, fieldId] of [["chat-mic", "chat-input"], ["draft-mic", "terminal-draft"]]) {
    const node = $(id);
    if (!node) continue;
    const listening = !!state.dictation && state.dictation.field === $(fieldId);
    const label = listening ? state.dictation.stopping ? "음성 입력 마무리 · 눌러 즉시 중지" : "음성 입력 중지"
      : state.dictationParked ? "음성 입력 (마이크 대기 중 · 바로 이어서 받아씀)" : "음성 입력";
    node.hidden = !supported;
    node.setAttribute("aria-pressed", String(listening));
    node.setAttribute("aria-label", label);
    node.title = label;
    setClass(node, "is-listening", listening);
  }
}
// The mic's trace: what the device's recognition did and what the board made of it, sent to
// the board a moment after each listening (event names, timings, counts, lengths and codes;
// never what was said). It tells a platform that hears nothing from words the board set aside.
const DICTATION_TRACE_EVENTS = 300;
const DICTATION_TRACE_DELAY_MS = 2500;
function dictationTrace(session, e, fields = {}) {
  const trace = session?.trace;
  if (!trace || trace.events.length >= DICTATION_TRACE_EVENTS) return;
  trace.events.push({ t: Date.now() - trace.t0, e, ...fields });
}
// The status line's words as a code: which of them the user saw.
function dictationReportCode(text) {
  if (!text) return "";
  const known = [[DICTATION_PREPARING, "preparing"], [DICTATION_LISTENING, "listening"], [DICTATION_ENDING, "ending"],
    [DICTATION_STALLED, "stalled"], [DICTATION_NOT_STARTED, "not-started"], [DICTATION_SILENCE_END, "silence"],
    [DICTATION_CHANGED, "changed"], [DICTATION_STUCK, "stuck"], [DICTATION_PARKED, "parked"]];
  for (const [message, code] of known) if (text === message) return code;
  const detail = /\(([a-z-]+)\)$/.exec(text)?.[1] || "";
  if (text.startsWith(DICTATION_ENDED_AT_ONCE)) return `ended-at-once:${detail}`;
  if (text.startsWith(DICTATION_INTERRUPTED)) return `interrupted:${detail}`;
  return detail ? `error:${detail}` : "other";
}
// A platform's error message as a code; its words never leave the device. WebKit reports
// most failures as "aborted" and puts the actual reason only in this message.
function dictationMessageCode(message) {
  const text = String(message || "");
  if (!text) return "";
  let hash = 0x811c9dc5;
  for (const char of text) hash = Math.imul(hash ^ char.codePointAt(0), 0x01000193) >>> 0;
  const kind = [[/audio|session|capture|microphone/i, "audio"], [/permission|denied|not allowed|authori/i, "perm"],
    [/network|connect|server/i, "net"], [/cancel|abort|interrupt/i, "cancel"], [/busy|in use|already/i, "busy"],
    [/speech|recogni/i, "speech"]].find(([pattern]) => pattern.test(text))?.[1] || "msg";
  return `${kind}:${hash.toString(16).padStart(8, "0")}.${Math.min(text.length, 9999)}`;
}
// Where the focus was when the mic was pressed (an element's kind and id, never its text).
function dictationFocusCode() {
  const node = document.activeElement;
  if (!node || node === document.body) return "body";
  return `${String(node.tagName || "").toLowerCase()}:${String(node.id || "")}`.replace(/[^A-Za-z0-9_.:+-]/g, "").slice(0, 40);
}
function sendDictationTrace(session, outcome) {
  const trace = session.trace;
  if (!trace || trace.sent) return;
  trace.sent = true;
  // A recognition that still starts or answers after the end belongs to it too.
  setTimeout(() => {
    api("/api/diagnostics/dictation", { method: "POST",
      body: { env: trace.env, outcome: dictationReportCode(outcome), events: trace.events } }).catch(() => {});
  }, DICTATION_TRACE_DELAY_MS);
}
function dictationInputState(field) {
  const selected = selectedSession();
  const composing = field === $("terminal-draft") ? state.composing
    : selected && field === $("chat-input") ? chatEntry(selected).composing : false;
  return { disabled: !!field?.disabled, readOnly: !!field?.readOnly, composing: !!composing };
}
// The field at a press: its keyboard let go of (see startDictation) and where the words go.
function dictationPress(field, releaseKeyboard) {
  const focused = document.activeElement === field;
  // Touch keyboards get in the listening's way: Android's (Samsung's among them) rewrite or
  // commit again a focused field's text they did not type, which reads as the user's own
  // typing, and iPadOS gives the recognition no sound while its on-screen keyboard is up (it
  // starts, and hears nothing). So the chat input's keyboard lets go while it listens. The
  // terminal's input line does too on WebKit, even if a keyboard animation lost its height
  // baseline; elsewhere it only lets go of an on-screen keyboard. Focusless hardware keys
  // never go to the CLI while that line listens. Let go before reading the field: a keyboard
  // may write the words it held once more.
  const letGo = focused && (webkitTouchDictation() || (touchDevice() && (releaseKeyboard || keyboardOpen())));
  const press = { focused, letGo, pressValue: String(field.value || "").length, act: dictationFocusCode(), kb: keyboardOpen() };
  if (letGo) field.blur?.();
  const value = String(field.value || "");
  const start = Number.isInteger(field.selectionStart) ? Math.min(field.selectionStart, value.length) : value.length;
  const end = Number.isInteger(field.selectionEnd) ? Math.min(Math.max(start, field.selectionEnd), value.length) : start;
  return { ...press, value, start, end };
}
// One listening's trace: what the device's recognition did, from the press (see dictationTrace).
function dictationTraceFor(field, press, extra = {}) {
  return { t0: Date.now(), sent: false, events: [], env: {
    api: globalThis.SpeechRecognition ? "standard" : "webkit", field: String(field.id || ""),
    standalone: !!window.matchMedia?.("(display-mode: standalone)")?.matches, touch: touchDevice(),
    cumulative: dictationCumulative(), visibility: String(document.visibilityState || ""),
    focused: press.focused, letGo: press.letGo, pressValue: press.pressValue, value: press.value.length,
    start: press.start, end: press.end,
    // The keyboard and focus at the press, how long ago (and how) the last listening ended,
    // and how many recognitions this page made before it.
    kb: press.kb, act: press.act,
    sinceEnd: state.dictationEndedAt ? Math.min(Date.now() - state.dictationEndedAt, 1e9) : null,
    lastEnd: state.dictationEndHow || "", made: state.dictationMade || 0,
    ...extra, ...dictationInputState(field) } };
}
// The first way a recognition ended is kept (an abort's late end does not overwrite it).
function noteDictationEnd(session, how) {
  if (!session.invoked || session.endNoted) return;
  session.endNoted = true;
  state.dictationEndedAt = Date.now();
  state.dictationEndHow = String(how || "").replace(/[^A-Za-z0-9_.:+-]/g, "").slice(0, 40);
}
// Checked once a second while it listens: still on screen, an error, or nothing for too long.
function dictationTimer(session) {
  return setInterval(() => {
    if (state.dictation !== session) return;
    if (!session.active()) {
      stopDictation({ abort: true });
      return;
    }
    // An error the engine reported without ending (seen in WebKit) is said, with its code.
    // On Android "no speech" is followed by its end, which listens on (dictationResumable).
    if (session.error && !(dictationCumulative() && session.error === "no-speech" && dictationResumable(session))) {
      stopDictation({ abort: true, message: dictationOutcome(session) });
      return;
    }
    session.ticks++;
    session.idle++;
    if (!session.started && session.ticks >= DICTATION_START_TICKS) stopDictation({ abort: true, message: DICTATION_NOT_STARTED });
    else if (session.started && !session.stopping && session.idle >= DICTATION_SILENCE_TICKS) {
      session.silent = true;
      stopDictation();
    }
  }, 1000);
}
// Whether ending this listening only parks it (DICTATION_PARK_MS): a WebKit touch device,
// a recognition that started and has not failed.
function dictationParkable(session) {
  return webkitTouchDictation() && session.started && session.invoked && !session.error && !!session.recognition;
}
// settle: the last words still land for a moment (the mic pressed again); otherwise at once
// (a send: what was sent is what was heard).
function parkDictation(session, settle) {
  if (settle && !session.stopping) {
    session.stopping = true;
    session.report(DICTATION_ENDING);
    renderDictation();
    clearTimeout(session.stopTimer);
    session.stopTimer = setTimeout(() => {
      if (state.dictation === session) parkDictation(session, false);
    }, DICTATION_PARK_SETTLE_MS);
    return;
  }
  state.dictation = null;
  state.dictationParked = session;
  clearInterval(session.timer);
  clearTimeout(session.stopTimer);
  session.stopping = false;
  session.parkedAt = Date.now();
  session.skip = session.seen;
  dictationTrace(session, "park");
  session.report(DICTATION_PARKED);
  renderDictation();
  sendDictationTrace(session, DICTATION_PARKED);
  clearTimeout(session.parkTimer);
  session.parkTimer = setTimeout(() => {
    if (state.dictationParked === session) dropParkedDictation("expired");
  }, DICTATION_PARK_MS);
}
// The parked engine ends (its time is up, the page is hidden, another kind of start).
function dropParkedDictation(reason) {
  const session = state.dictationParked;
  if (!session) return;
  state.dictationParked = null;
  clearTimeout(session.parkTimer);
  noteDictationEnd(session, "parked:" + reason);
  try {
    session.recognition?.abort();
  } catch {
    // Already ended.
  }
  session.report("");
  renderDictation();
}
// The mic pressed while an engine is parked: it listens on at once, into this field at its caret.
function resumeDictation(session, field, { report, active, releaseKeyboard }) {
  state.dictationParked = null;
  clearTimeout(session.parkTimer);
  const press = dictationPress(field, releaseKeyboard);
  const parkedMs = Math.min(Date.now() - (session.parkedAt || Date.now()), 1e9);
  Object.assign(session, { field, reportTo: report, active, prefix: press.value.slice(0, press.start), suffix: press.value.slice(press.end),
    written: press.value, wrote: false, results: [], finals: [], finalText: "", restarts: 0, heard: false, started: true,
    silent: false, error: "", applying: false, stopping: false, ticks: 0, idle: 0,
    trace: dictationTraceFor(field, press, { resumed: true, parkedMs }) });
  state.dictation = session;
  session.timer = dictationTimer(session);
  dictationTrace(session, "press");
  dictationTrace(session, "resume", { skip: session.skip });
  session.report(DICTATION_LISTENING);
  renderDictation();
  return true;
}
// report(text): the field's own status line ("" clears what this said); active(): whether
// the field is still the one on screen.
function startDictation(field, { report, active, releaseKeyboard = false }) {
  const Recognition = speechRecognitionClass();
  if (!Recognition || !field || field.disabled || field.readOnly) return false;
  // One listening at a time: another field's is parked (or aborted), then this one goes on.
  stopDictation({ abort: true, park: true });
  if (state.dictationParked) return resumeDictation(state.dictationParked, field, { report, active, releaseKeyboard });
  const press = dictationPress(field, releaseKeyboard);
  const trace = dictationTraceFor(field, press);
  const make = (Kind = Recognition) => {
    const made = new Kind();
    state.dictationMade = (state.dictationMade || 0) + 1;
    made.lang = DICTATION_LANG;
    made.interimResults = true;
    made.continuous = true;
    made.maxAlternatives = 1;
    return made;
  };
  let recognition;
  try {
    recognition = make();
  } catch {
    report("이 브라우저에서 음성 입력을 시작하지 못했습니다. 키보드의 받아쓰기를 써 주세요.");
    return false;
  }
  const session = { field, recognition, reportTo: report, active, prefix: press.value.slice(0, press.start), suffix: press.value.slice(press.end),
    written: press.value, wrote: false, results: [], finals: [], finalText: "", restarts: 0, heard: false, started: false, silent: false, error: "", applying: false, stopping: false,
    ticks: 0, idle: 0, timer: null, stopTimer: null, parkTimer: null, beganAt: 0, endNoted: false, seen: 0, skip: 0, trace };
  session.report = (text) => {
    session.reportTo(text);
    dictationTrace(session, "say", { why: dictationReportCode(text), ...dictationInputState(session.field) });
  };
  // A recognition replaced by another is over: what it still reports is traced, never applied.
  const replaced = (rec, name) => {
    if (session.recognition === rec) return false;
    dictationTrace(session, "old:" + name);
    return true;
  };
  // A recognition whose listening already ended (aborted before the platform started it) does
  // not keep the microphone: its first sign of life stops it. A parked one is still wanted.
  const orphaned = (rec) => {
    if (state.dictation === session || state.dictationParked === session) return false;
    dictationTrace(session, "orphan");
    try {
      rec.abort();
    } catch {
      // Already ended.
    }
    return true;
  };
  const listening = (rec) => {
    if (orphaned(rec) || session.started) return;
    session.started = true;
    session.idle = 0;
    session.report(DICTATION_LISTENING);
  };
  // WebKit on a touch device: an audio start this soon after the start was asked for is a
  // capture that gives no sound (DICTATION_STALE_START_MS). It ends the listening at once.
  const staleStart = (lag) => {
    if (!webkitTouchDictation() || state.dictation !== session || session.stopping || session.heard
        || lag > DICTATION_STALE_START_MS) return false;
    dictationTrace(session, "stale", { lag });
    stopDictation({ abort: true, message: DICTATION_STUCK });
    return true;
  };
  for (const name of ["start", "audiostart"]) {
    recognition["on" + name] = () => {
      if (replaced(recognition, name)) return;
      const lag = Date.now() - session.beganAt;
      dictationTrace(session, name, { cur: state.dictation === session, lag });
      if (name === "audiostart" && staleStart(lag)) return;
      listening(recognition);
    };
  }
  for (const name of ["soundstart", "speechstart", "speechend", "soundend", "audioend", "nomatch"])
    recognition["on" + name] = () => {
      if (!replaced(recognition, name)) dictationTrace(session, name, { cur: state.dictation === session });
    };
  recognition.onresult = (event) => {
    if (replaced(recognition, "result")) return;
    const all = Array.from(event?.results || []);
    // A list shorter than what was set aside started over (WebKit after a pause): all of it is new.
    if (all.length < session.skip) session.skip = 0;
    session.seen = all.length;
    if (state.dictationParked === session) {
      // Parked: what it hears is not written; a resume starts after it.
      session.skip = all.length;
      return;
    }
    const list = all.slice(session.skip);
    dictationTrace(session, "result", { cur: state.dictation === session, n: list.length, idx: event?.resultIndex ?? null,
      fin: list.filter((result) => result?.isFinal).length, last: !!list.at(-1)?.isFinal, len: dictationText(list).length });
    if (orphaned(recognition)) return;
    const field = session.field;
    // Off screen, or the text changed by other means (a paste, a restored message): those win.
    if (!session.active()) {
      dictationTrace(session, "inactive", { chat: chatVisible(), hidden: !!document.hidden });
      stopDictation({ abort: true });
      return;
    }
    if (field.value !== session.written && !rebaseDictation(session)) {
      dictationTrace(session, "changed", { value: String(field.value || "").length, written: session.written.length });
      stopDictation({ abort: true, message: DICTATION_CHANGED });
      return;
    }
    listening(recognition);
    session.idle = 0;
    session.restarts = 0;  // Heard: a long dictation restarts once per phrase without limit.
    // Final results never change and keep their places while the list grows (results still
    // changing may merge or drop). A list whose earlier finals are gone or say something
    // else started over (WebKit after a pause): the words those finals wrote stay, and the
    // new list follows them, unless it already holds them (Android repeats what it heard).
    const finals = [];
    for (const result of list) {
      if (!result?.isFinal) break;
      finals.push(String(result?.[0]?.transcript || ""));
    }
    const same = (a, b) => dictationRepeats(a, b) && dictationRepeats(b, a);
    const kept = session.finals.every((text, i) => i < finals.length && same(text, finals[i]));
    const repeats = dictationCumulative() && dictationRepeats(session.finalText, String(list[0]?.[0]?.transcript || ""));
    if (!kept && session.finalText && !repeats) {
      session.prefix += (session.prefix && !/\s$/.test(session.prefix) ? " " : "") + session.finalText;
    }
    session.finals = finals;
    session.finalText = dictationText(list.slice(0, finals.length));
    session.results = list;
    session.heard = true;
    applyDictation(session);
  };
  recognition.onerror = (event) => {
    if (replaced(recognition, "error")) return;
    dictationTrace(session, "error", { code: String(event?.error || "unknown").slice(0, 40), cur: state.dictation === session,
      ...(event?.message ? { msg: dictationMessageCode(event.message) } : {}) });
    if (state.dictation === session) session.error = String(event?.error || "unknown");
  };
  recognition.onend = () => {
    if (replaced(recognition, "end")) return;
    if (state.dictationParked === session) {
      // The parked engine ended by itself: the next press starts a new one.
      dictationTrace(session, "end", { parked: true });
      state.dictationParked = null;
      clearTimeout(session.parkTimer);
      noteDictationEnd(session, "parked:ended");
      session.report("");
      renderDictation();
      return;
    }
    noteDictationEnd(session, session.stopping ? "stop" : session.error ? "error:" + session.error : "end");
    dictationTrace(session, "end", { cur: state.dictation === session, heard: session.heard, idle: session.idle });
    if (state.dictation === session && dictationResumable(session)) {
      // What it wrote stays; the next round's words follow it.
      const text = dictationText(session.results);
      if (text) session.prefix += (session.prefix && !/\s$/.test(session.prefix) ? " " : "") + text;
      Object.assign(session, { results: [], finals: [], finalText: "", error: "", restarts: session.restarts + 1, skip: 0 });
      try {
        recognition.start();
        dictationTrace(session, "restart", { ok: true, round: session.restarts });
        return;
      } catch {
        // Not startable again: it ends as before.
        dictationTrace(session, "restart", { ok: false, round: session.restarts });
      }
    }
    finishDictation(session);
  };
  state.dictation = session;
  session.timer = dictationTimer(session);
  dictationTrace(session, "press");
  session.report(DICTATION_PREPARING);
  renderDictation();
  dictationTrace(session, "begin", { focused: document.activeElement === field, ...dictationInputState(field) });
  try {
    session.invoked = true;
    session.beganAt = Date.now();
    recognition.start();
    return true;
  } catch {
    session.invoked = false;
    dictationTrace(session, "error", { code: "start-threw" });
    session.error = "start";
    finishDictation(session);
    return false;
  }
}
function applyDictation(session) {
  const { field } = session;
  const text = dictationText(session.results);
  const lead = text && session.prefix && !/\s$/.test(session.prefix) ? " " : "";
  const trail = text && session.suffix && !/^\s/.test(session.suffix) ? " " : "";
  const before = session.prefix + lead + text;
  field.value = before + trail + session.suffix;
  session.written = field.value;
  if (text) session.wrote = true;
  dictationTrace(session, "apply", { len: text.length, value: field.value.length, focused: document.activeElement === field });
  if (document.activeElement === field) field.setSelectionRange?.(before.length, before.length);
  // The field's own input handling (draft saving, sizing) runs as for typing.
  session.applying = true;
  try {
    field.dispatchEvent(new Event("input", { bubbles: true }));
  } finally {
    session.applying = false;
  }
}
// What the field's status line says when a listening ends ("" for a plain end after words).
// Our own aborts never reach the end handler, so an "aborted" there is the platform's (or its
// answer to our stop, which is a plain end).
function dictationOutcome(session) {
  if (!session.active()) return ""; // a field that left the screen is not told later, out of context
  const platformAbort = session.error === "aborted" && !session.stopping;
  const error = session.error === "aborted" ? "" : session.error;
  if (platformAbort) return session.heard ? `${DICTATION_INTERRUPTED} (aborted)` : `${DICTATION_ENDED_AT_ONCE} (aborted)`;
  if (error) return dictationErrorText(error);
  if (!session.stopping && !session.heard && session.ticks === 0) return `${DICTATION_ENDED_AT_ONCE} (end)`;
  if (!session.heard) return DICTATION_ERRORS["no-speech"];
  return session.silent ? DICTATION_SILENCE_END : "";
}
// An end the user did not ask for, on Android, while it is on screen and was heard or only
// paused: listen on (see DICTATION_RESTARTS).
function dictationResumable(session) {
  return dictationCumulative() && !session.stopping && session.started && session.active()
    && session.restarts < DICTATION_RESTARTS && session.idle < DICTATION_SILENCE_TICKS
    && !DICTATION_HARD_ERRORS.has(session.error) && session.error !== "aborted";
}
function finishDictation(session) {
  if (state.dictation !== session) return;
  state.dictation = null;
  clearInterval(session.timer);
  clearTimeout(session.stopTimer);
  const outcome = dictationOutcome(session);
  session.report(outcome);
  renderDictation();
  sendDictationTrace(session, outcome);
}
// stop: the last words still arrive (onend finishes); pressed again while it ends, or not
// ended in time (the recognition stopped answering), it is aborted. abort: nothing more lands.
// Where the engine can be parked (dictationParkable) a stop parks it once its last words
// landed, and an abort asked to park (a send, a command, another field) parks it at once.
function stopDictation({ abort = false, field = null, message = "", park = false } = {}) {
  const session = state.dictation;
  if (!session || (field && session.field !== field)) return;
  if (!message && (park || !abort) && dictationParkable(session)) {
    const settle = !park && !session.stopping;
    dictationTrace(session, "stop", { park: true, settle });
    parkDictation(session, settle);
    return;
  }
  if (session.stopping) abort = true;
  dictationTrace(session, "stop", { abort, why: dictationReportCode(message) });
  if (!abort && !session.stopping) {
    session.stopping = true;
    // Its last words may still come; a second press aborts without waiting for the engine.
    session.report(DICTATION_ENDING);
    session.stopTimer = setTimeout(() => {
      if (state.dictation === session) stopDictation({ abort: true, message: !session.active() ? ""
        : session.error ? dictationOutcome(session) : session.heard ? DICTATION_STALLED
        : session.started ? DICTATION_ERRORS["no-speech"] : DICTATION_NOT_STARTED });
    }, DICTATION_STOP_WAIT_MS);
    try {
      session.recognition.stop();
    } catch {
      stopDictation({ abort: true });
    }
    return;
  }
  state.dictation = null;
  clearInterval(session.timer);
  clearTimeout(session.stopTimer);
  // Some engines never send end after abort: the end is noted here too.
  noteDictationEnd(session, "abort");
  try {
    session.recognition?.abort();
  } catch {
    // Already ended.
  }
  session.report(message);
  renderDictation();
  sendDictationTrace(session, message);
}
// The text changed by the user (typed, or an input method's word): their words win. An
// input method only re-marking the words that landed changes nothing and stops nothing.
function dictationTyped(field, event = null) {
  const session = state.dictation;
  if (session?.field !== field || session.applying) return;
  dictationTrace(session, "input", { type: String(event?.inputType || "").slice(0, 40), comp: !!event?.isComposing,
    value: String(field.value || "").length, written: session.written.length, focused: document.activeElement === field,
    wrote: session.wrote });
  if (field.value === session.written) return;
  if (rebaseDictation(session)) dictationTrace(session, "rebase");
  else stopDictation({ abort: true });
}
// The field's own focus and input-method events while it listens, for the trace.
function dictationFieldEvent(field, e) {
  if (state.dictation?.field === field) dictationTrace(state.dictation, e, { value: String(field.value || "").length });
}
// Before any words landed, a change while the field has no focus is the keyboard letting go
// (committing or writing again the words it held, a moment later on Android): the field's
// text stays and the words will go where its caret is. Returns whether it took the change.
function rebaseDictation(session) {
  const { field } = session;
  if (session.wrote || document.activeElement === field) return false;
  const value = String(field.value || "");
  const start = Number.isInteger(field.selectionStart) ? Math.min(field.selectionStart, value.length) : value.length;
  const end = Number.isInteger(field.selectionEnd) ? Math.min(Math.max(start, field.selectionEnd), value.length) : start;
  Object.assign(session, { prefix: value.slice(0, start), suffix: value.slice(end), written: value });
  return true;
}
function draftDictationNote(text) {
  const note = $("draft-note");
  if (text || dictationTransient(note.textContent)) note.textContent = text;
}
function toggleDraftDictation() {
  const field = $("terminal-draft");
  if (dictationListening(field)) {
    stopDictation({ field });
    return;
  }
  if (!state.terminal) return;
  if (state.composing) {
    draftDictationNote("한글 조합을 마친 뒤 마이크를 눌러 주세요.");
    return;
  }
  if (state.draftPaste) {
    draftDictationNote("붙여넣는 중에는 음성 입력을 시작하지 않습니다.");
    return;
  }
  const terminalId = state.terminal.id;
  startDictation(field, {
    report: draftDictationNote,
    active: () => state.terminalVisible && !document.hidden && state.terminal?.id === terminalId && !state.draftPaste
      && $("terminal-draft") === field,
  });
}
const CHAT_COMMANDS = [
  { name: "model", label: "모델 선택" }, { name: "effort", label: "에포트 레벨 선택" },
  { name: "compact", label: "현재 대화 요약" }, { name: "clear", label: "새 대화 시작 · 기존 기록 보존" },
];
function updateSlashMenu(entry, box = $("chat-composer"), input = $("chat-input")) {
  const prefix = /^\/([a-z]*)$/.exec(entry.text.trim());
  const choices = prefix && !entry.composing && entry.slashDismissedText !== entry.text
    ? CHAT_COMMANDS.filter((item) => item.name.startsWith(prefix[1])) : [];
  $("chat-slash-options")?.remove?.();
  input?.removeAttribute?.("aria-activedescendant");
  input?.setAttribute("aria-expanded", String(!!choices.length));
  entry.slashChoices = choices;
  if (!choices.length || !box || !input || !currentChat(entry)) return;
  entry.slashIndex = Math.min(entry.slashIndex || 0, choices.length - 1);
  const menu = el("div", "chat-slash-options");
  menu.id = "chat-slash-options"; menu.setAttribute("role", "listbox"); menu.setAttribute("aria-label", "슬래시 명령");
  choices.forEach((item, index) => {
    const option = button(`/${item.name} · ${item.label}`, "chat-slash-option", () => chooseSlash(entry, index, input));
    option.id = `chat-slash-option-${index}`; option.setAttribute("role", "option");
    option.setAttribute("aria-selected", String(index === entry.slashIndex));
    option.addEventListener("pointerdown", (event) => event.preventDefault());
    menu.append(option);
  });
  input.setAttribute("aria-controls", menu.id); input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-activedescendant", `chat-slash-option-${entry.slashIndex}`);
  box.append(menu);
}
function chooseSlash(entry, index, input) {
  const item = entry.slashChoices?.[index];
  if (!item || !currentChat(entry)) return;
  entry.text = `/${item.name} `; entry.revision++; input.value = entry.text;
  entry.slashDismissedText = entry.text;
  updateSlashMenu(entry); updateChatComposer(entry);
  input.focus({ preventScroll: true });
}
function slashKeydown(entry, event, input) {
  if (event.isComposing || event.keyCode === 229 || entry.composing || !entry.slashChoices?.length) return false;
  if (!["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key) || event.ctrlKey || event.metaKey || event.shiftKey) return false;
  event.preventDefault();
  if (event.key === "Escape") { entry.slashDismissedText = entry.text; updateSlashMenu(entry); }
  else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    entry.slashIndex = ((entry.slashIndex || 0) + (event.key === "ArrowDown" ? 1 : -1) + entry.slashChoices.length) % entry.slashChoices.length;
    updateSlashMenu(entry);
  } else chooseSlash(entry, entry.slashIndex || 0, input);
  return true;
}
function updateModelControls(entry) {
  if (!currentChat(entry)) return;
  const settings = entry.data?.modelSettings || {};
  const next = entry.source.agent === "codex" ? settings.next || {} : {};
  const selected = { model: next.model || settings.model, effort: next.effort || settings.effort };
  const changed = ["model", "effort"].some((kind) => next[kind] && next[kind] !== settings[kind]);
  const observedLabel = settings.observed === "last_response" ? "마지막 응답" : "세션에서 확인";
  const observedValues = [settings.model || "모델 미확인", settings.effort || "에포트 미확인"];
  const observed = settings.observed === "last_response"
    ? `모델(마지막 응답): ${observedValues[0]} · 에포트(세션에서 확인): ${observedValues[1]}`
    : `세션에서 확인: ${observedValues.join(" · ")}`;
  const note = $("chat-model-selection-note");
  const noteText = changed ? `다음 메시지에 사용 · ${observed}` : "";
  if (note) {
    if (note.textContent !== noteText) note.textContent = noteText;
    note.hidden = !changed;
  }
  const summary = $("chat-model-summary");
  if (summary) {
    const values = [selected.model || "모델 미확인", selected.effort || "에포트 미확인"].join(" · ");
    summary.textContent = (changed ? "선택 " : "") + values;
    summary.title = changed ? `다음 메시지 설정: ${values} · ${observed}` : observed;
  }
  for (const kind of ["model", "effort"]) {
    const control = $(`chat-${kind}-control`);
    if (!control) continue;
    const actual = settings[kind], pending = next[kind] && next[kind] !== actual ? next[kind] : null;
    control.textContent = selected[kind] || "미확인";
    const label = kind === "model" ? "모델" : "에포트";
    const explanation = pending ? `다음 메시지에 사용 · ${kind === "model" ? observedLabel : "세션에서 확인"}: ${actual || "미확인"}` : settings.observed === "last_response" && kind === "model" ? "마지막 응답에서 확인한 모델" : `${label} 선택`;
    control.title = `${label}: ${control.textContent} · ${explanation}`;
    control.setAttribute("aria-label", `${label}: ${control.textContent} · 선택하기`);
    control.disabled = !chatSupported(entry) || entry.sending || !!entry.pending || !!entry.commandBusy || networkOffline();
  }
}
function closeChatSettings() {
  $("chat-settings-panel")?.remove?.();
  for (const kind of ["model", "effort"]) $(`chat-${kind}-control`)?.setAttribute("aria-expanded", "false");
}
async function openChatSettings(entry, kind) {
  if (!currentChat(entry) || entry.commandBusy || entry.sending || entry.pending) return;
  const control = $(`chat-${kind}-control`);
  const parent = $("detail-head") || control?.parentElement;
  if (!control || !parent) return;
  closeQuickReplies();
  closeChatSettings();
  // Slash commands must open a visible panel even when focus/typing folds the controls.
  const panel = el("div", "chat-settings-panel"); panel.id = "chat-settings-panel";
  panel.setAttribute("role", "group"); panel.setAttribute("aria-label", kind === "model" ? "모델 선택" : "에포트 레벨 선택");
  panel.append(el("p", "muted", "선택지를 확인하고 있습니다…"));
  parent.append(panel);
  control.setAttribute("aria-expanded", "true");
  const close = () => { if ($("chat-settings-panel") === panel) closeChatSettings(); };
  const epoch = state.authEpoch;
  const draftRevision = entry.revision;
  try {
    const data = await api("/api/chat/command", { method: "POST", body: { source: entry.source, command: "models" } });
    if (epoch !== state.authEpoch || !currentChat(entry) || $("chat-settings-panel") !== panel) return;
    const current = data.next?.model ? data.next : data.current || {};
    const model = (data.models || []).find((row) => row.model === current.model || current.model?.toLowerCase().includes(row.model.toLowerCase()));
    const values = kind === "model" ? (data.models || []).map((row) => ({ value: row.model, label: row.label || row.model }))
      : (model?.efforts || []).map((value) => ({ value, label: value }));
    const label = el("label", "", kind === "model" ? "모델" : "에포트 레벨"); label.htmlFor = "chat-setting-choice";
    const select = el("select"); select.id = "chat-setting-choice";
    for (const item of values) { const option = el("option", "", item.label); option.value = item.value; select.append(option); }
    if (values.some((item) => item.value === current[kind])) select.value = current[kind];
    else if (values.length) select.value = values[0].value;
    const apply = button("적용", "primary", async () => {
      if (!select.value || !currentChat(entry) || entry.commandBusy) return;
      const ok = await executeChatCommand(entry, kind, select.value);
      if (ok) {
        close();
        if (currentChat(entry) && entry.revision === draftRevision && entry.text.trim() === `/${kind}`) {
          entry.text = ""; entry.revision++; $("chat-input").value = ""; updateSlashMenu(entry); updateChatComposer(entry);
        }
      }
    });
    apply.disabled = !values.length;
    panel.replaceChildren(label, select, el("p", "muted", entry.source.agent === "codex" ? "다음 웹 입력부터 적용됩니다. 진행 중인 작업은 현재 설정을 유지합니다." : "Claude의 명령 결과에서 적용 여부를 확인합니다."),
      ...(!values.length ? [el("p", "", "현재 모델의 선택지를 확인하지 못했습니다. 모델을 먼저 선택해 주세요.")] : []), apply, button("닫기", "quiet", close));
  } catch (error) {
    if (epoch === state.authEpoch && currentChat(entry) && $("chat-settings-panel") === panel)
      panel.replaceChildren(el("p", "", error.message), button("닫기", "quiet", close));
  }
}
async function executeChatCommand(entry, command, value = null) {
  if (!currentChat(entry) || !chatSupported(entry) || entry.commandBusy || entry.sending || entry.pending || entry.uploads || entry.composing) return false;
  stopDictation({ abort: true, park: true, field: $("chat-input") });
  closeQuickReplies();
  entry.stateEpoch++;
  if (!entry.commandRequest || entry.commandRequest.command !== command || entry.commandRequest.value !== value || entry.commandRequest.status === "applied")
    entry.commandRequest = { requestId: requestId(), command, value, status: "pending" };
  const commandRequest = entry.commandRequest;
  entry.commandBusy = true; entry.error = entry.inputError = ""; entry.notice = "명령을 적용하고 있습니다…";
  const epoch = state.authEpoch;
  updateChatComposer(entry); updateModelControls(entry);
  try {
    const result = await api("/api/chat/command", { method: "POST", body: { source: entry.source, command, value, requestId: commandRequest.requestId } });
    if (epoch !== state.authEpoch) return false;
    entry.stateEpoch++;
    entry.notice = result.reason || "명령을 전달했습니다.";
    if (entry.data && (result.current || result.next)) entry.data.modelSettings = { ...(entry.data.modelSettings || {}), ...(result.current || {}), ...(result.next ? { next: result.next } : {}) };
    if (result.terminalId) refreshTerminals();
    if (result.status === "unknown" || result.status === "rejected") {
      if (result.status === "rejected") entry.commandRequest = null; else commandRequest.status = "unknown";
      entry.error = entry.inputError = entry.notice; entry.notice = ""; return false;
    }
    commandRequest.status = "applied";
    if (currentChat(entry)) loadChatState(selectedSession(), true);
    return true;
  } catch (error) {
    if (error.dispatchState === "not_started") entry.commandRequest = null; else commandRequest.status = "unknown";
    if (epoch === state.authEpoch && !error.unauthorized && !error.staleAuth) { entry.error = entry.inputError = error.message; entry.notice = ""; }
    return false;
  } finally {
    if (epoch === state.authEpoch) { entry.commandBusy = false; updateChatComposer(entry); updateModelControls(entry); }
  }
}
async function runSlashCommand(entry, text, attachments) {
  if (entry.commandBusy || entry.sending || entry.pending || entry.uploads || entry.composing) return;
  const match = /^\/(model|effort|compact|clear)(?:\s+(\S+))?$/.exec(text);
  if (!match || attachments.length || ["compact", "clear"].includes(match?.[1]) && match?.[2]) {
    entry.error = entry.inputError = "지원하는 명령을 첨부 없이 입력해 주세요: /model, /effort, /compact, /clear"; updateChatComposer(entry); return;
  }
  const [, command, value] = match;
  if (["model", "effort"].includes(command) && !value) { openChatSettings(entry, command); return; }
  const revision = entry.revision;
  let ok;
  if (command === "clear") { openNewSession(); ok = true; }
  else ok = await executeChatCommand(entry, command, value || null);
  if (ok && currentChat(entry) && entry.revision === revision && entry.text.trim() === text) {
    entry.text = ""; entry.revision++; $("chat-input").value = "";
    updateSlashMenu(entry); updateChatComposer(entry);
  }
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
  const recovery = el("div", "chat-recovery");
  recovery.id = "chat-recovery"; recovery.hidden = true;
  const recoveryText = el("span", "chat-terminal-gate-text"); recoveryText.id = "chat-recovery-text";
  const recover = button("세션 다시 열기", "primary", () => { if (editorCurrent()) return recoverChatSession(s); });
  recover.id = "chat-recover";
  recovery.append(recoveryText, recover);
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
  input.placeholder = chatPlaceholder(false);
  input.setAttribute("aria-label", "메시지");
  input.value = entry.text;
  input.addEventListener("input", (event) => {
    dictationTyped(input, event);
    if (!editorCurrent()) return;
    if (input.value) warmChat(entry);
    entry.inputCollapsed = false;
    entry.text = input.value;
    // Words landing from the mic keep its "듣는 중" line; typing clears both.
    if (!state.dictation?.applying) entry.notice = entry.dictationNotice = "";
    // Editing the draft answers an input error about it.
    if (entry.inputError && entry.error === entry.inputError) entry.error = "";
    entry.inputError = "";
    entry.revision++;
    entry.slashDismissedText = null;
    updateSlashMenu(entry, box, input);
    updateChatComposer(entry);
  });
  // Phones fold the bars for typing and return to the reader's choice on blur
  // (hidden stays hidden); mouse layouts show them for the input, as before.
  // A tablet folds them only once its on-screen keyboard takes the space
  // (syncChromeMode on the viewport's resize); a hardware keyboard never does.
  input.addEventListener("focus", () => {
    dictationFieldEvent(input, "focus");
    setTyping(compactChrome());
  });
  input.addEventListener("blur", () => {
    dictationFieldEvent(input, "blur");
    setTyping(false);
    if (!state.chromeVisible) setReading(true, { stickBottom: messagesAtEnd($("messages")) });
  });
  input.addEventListener("compositionstart", () => {
    dictationFieldEvent(input, "compstart");
    if (!editorCurrent()) return;
    entry.composing = true;
    updateSlashMenu(entry, box, input);
    updateChatComposer(entry);
  });
  input.addEventListener("compositionend", () => {
    dictationFieldEvent(input, "compend");
    if (!editorCurrent()) return;
    entry.composing = false;
    entry.compositionEndedAt = Date.now();
    entry.inputCollapsed = false;
    entry.text = input.value;
    entry.revision++;
    updateSlashMenu(entry, box, input);
    updateChatComposer(entry);
  });
  input.addEventListener("keydown", (event) => {
    if (!editorCurrent()) return;
    if (slashKeydown(entry, event, input)) return;
    // Esc leaves the input (a hardware keyboard's way out); an open tools menu
    // closes first (closeChatToolsEscape), and an IME keeps its own Esc.
    if (event.key === "Escape") {
      if (event.isComposing || event.keyCode === 229 || entry.composing || tools.open || state.quickRepliesOpen || $("chat-settings-panel")?.isConnected) return;
      event.preventDefault();
      input.blur();
      return;
    }
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    if (event.isComposing || event.keyCode === 229 || entry.composing || Date.now() - (entry.compositionEndedAt || 0) < 80) return;
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
  const reportDictation = (text) => {
    entry.dictationNotice = text;
    if (editorCurrent()) updateChatComposer(entry);
  };
  const mic = button("", "chat-mic", () => {
    if (!editorCurrent()) return;
    if (dictationListening(input)) {
      stopDictation({ field: input });
      return;
    }
    if (entry.inputError && entry.error === entry.inputError) entry.error = "";
    entry.inputError = "";
    warmChat(entry);
    startDictation(input, { report: reportDictation, releaseKeyboard: true,
      active: () => editorCurrent() && chatVisible() });
  });
  mic.id = "chat-mic";
  mic.innerHTML = MIC_ICON;
  mic.hidden = !speechRecognitionClass();
  mic.setAttribute("aria-label", "음성 입력");
  mic.setAttribute("aria-pressed", "false");
  mic.title = "음성 입력 (브라우저 음성 인식)";
  // Like the send key: a focused field keeps its keyboard and layout (a touch's compatibility
  // mousedown would move the focus too).
  for (const type of ["pointerdown", "mousedown"])
    mic.addEventListener(type, (event) => {
      if (document.activeElement === input) event.preventDefault();
    });
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
  const manage = button("세션 관리", "secondary", (event) => { if (editorCurrent()) { tools.open = false; manageSession(s, event.currentTarget); } });
  manage.id = "chat-manage";
  const quick = button("빠른 답장", "secondary", (event) => {
    if (!editorCurrent()) return;
    tools.open = false;
    openQuickReplies(entry, input, event.currentTarget);
  });
  quick.id = "chat-quick";
  toolsActions.append(quick, clipboard, check, native, closeTerm, manage);
  for (const action of [quick, clipboard, check, native, closeTerm])
    action.addEventListener("pointerdown", (event) => event.preventDefault());
  tools.append(toolsActions);
  actions.append(attach, expand, terminal, keyHint, tools);
  // 빠른 답장 beside 보내기 while 집중 모드 folds 도구 (style.css shows it only then). The
  // keyboard stays on the input: the press never takes focus.
  const quickInline = button("", "chat-quick-inline", (event) => {
    if (!editorCurrent() || input.disabled || input.readOnly || entry.composing) return;
    openQuickReplies(entry, input, event.currentTarget);
  });
  quickInline.id = "chat-quick-inline";
  quickInline.innerHTML = QUICK_ICON;
  quickInline.setAttribute("aria-label", "빠른 답장");
  quickInline.setAttribute("aria-haspopup", "dialog");
  quickInline.title = "빠른 답장";
  quickInline.addEventListener("pointerdown", (event) => event.preventDefault());
  const main = el("div", "chat-main");
  main.append(input, mic, quickInline, send);
  box.append(heading, status, recovery, gate, evidence, attachmentsLabel, attachments, main, files, actions, hint);
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
  const viewport = viewportHeight();
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
  // A taller composer moves the bottom of the list: the 맨 아래 button sits above it again.
  renderJump();
}
// While a send is in flight or unconfirmed, its text already shows as the
// bubble; the editor keeps it (a rejected send leaves it there) but hides it.
function sentDraftHidden(entry) {
  return !entry.composing && draftIsPending(entry);
}
function closeChatToolsOutside(event) {
  const settings = $("chat-settings-panel");
  if (settings?.isConnected && !settings.contains?.(event.target) && !event.target?.closest?.(".chat-model-controls")) closeChatSettings();
  const quick = state.quickRepliesOpen;
  if (quick && !quick.panel.contains?.(event.target) && event.target !== quick.trigger) closeQuickReplies();
  for (const id of ["chat-tools", "terminal-settings", "board-status"]) {
    const tools = $(id);
    if (tools?.open && !tools.contains?.(event.target)) tools.open = false;
  }
  // An open card swipe closes on any touch outside its row.
  const open = state.swipeOpen;
  if (open && !open.contains?.(event.target)) closeSwipe(open);
}
function closeChatToolsEscape(event) {
  if (event.key !== "Escape" || event.isComposing || event.keyCode === 229) return;
  if ($("chat-settings-panel")?.isConnected) { closeChatSettings(); event.preventDefault(); }
  if (state.quickRepliesOpen) {
    const input = state.quickRepliesOpen.input;
    closeQuickReplies();
    if (document.activeElement !== input) input.focus?.({ preventScroll: true });
    event.preventDefault();
  }
  for (const id of ["chat-tools", "terminal-settings", "board-status"]) {
    const tools = $(id);
    if (!tools?.open) continue;
    const restoreSummaryFocus = tools.contains?.(document.activeElement);
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
  if (boardKeysBlocked() || !chatVisible() || Object.keys(SHEETS).some((id) => $(id)?.open)) return false;
  const card = $("chat-prompt");
  if (!card || card.hidden || typeof card.chooseNumber !== "function" || !card.chooseNumber(Number(key))) return false;
  event.preventDefault();
  return true;
}
function documentKeydown(event) {
  if (event.defaultPrevented) {
    noteHardwareKey(event);
    return;
  }
  closeChatToolsEscape(event);
  if (event.defaultPrevented) return;
  // Native details keeps Enter/Space/Tab; a device overview never routes keys to an
  // underlying conversation's approval choices or the terminal beside it.
  if ($("board-status")?.open) { noteHardwareKey(event); return; }
  if (terminalDocumentKey(event)) {
    noteHardwareKey(event);
    return;
  }
  if (!boardKeysBlocked() && (event.metaKey || event.ctrlKey) && String(event.key).toLowerCase() === "k" && !event.isComposing) {
    event.preventDefault();
    // Search is in the list, which a docked terminal may have moved aside.
    if (state.dockSolo) undock();
    const search = window.SessionholicGoals?.searchElement?.() || $("search");
    search?.focus();
    search?.select?.();
    return;
  }
  noteHardwareKey(event);
  if (listNavigationKeydown(event)) return;
  promptKeydown(event);
}
// A hardware keyboard (an iPad's) moves through the list: ↓ (or Enter) from the search
// field goes to (or opens) the first card, ↑↓ move between cards, and in a conversation
// Esc or ⌘[ goes back: to the list on a phone layout, to its card beside the conversation.
// Typing, an input method's composition, a terminal or an open sheet keep their keys.
function listNavigationKeydown(event) {
  // A key something else already took (closing the tools menu with Esc) is not taken again.
  if (event.defaultPrevented || boardKeysBlocked() || event.isComposing || event.keyCode === 229 ||
      event.altKey || event.shiftKey) return false;
  const target = event.target;
  if (target?.id === "search" && state.historySearch.mode) return false;
  if (target?.closest?.("dialog")) return false;
  const key = TERMINAL_KEY_ALIASES[event.key] || event.key;
  const cards = () => [...($("groups")?.querySelectorAll?.(".card") || [])];
  const plain = !event.metaKey && !event.ctrlKey;
  // A touch screen's own keyboard has a Return (search) key that only closes it: Enter
  // opens the first session only from a hardware keyboard.
  const enter = key === "Enter" && keyboardPresent();
  if (target?.id === "search" && plain && (key === "ArrowDown" || enter)) {
    const first = cards()[0];
    if (!first) return false;
    event.preventDefault();
    if (enter) first.click();
    else first.focus();
    return true;
  }
  if (target?.classList?.contains?.("card") && plain && (key === "ArrowDown" || key === "ArrowUp")) {
    const all = cards();
    const next = all[all.indexOf(target) + (key === "ArrowDown" ? 1 : -1)];
    event.preventDefault();
    if (next) next.focus();
    else if (key === "ArrowUp") $("search")?.focus();
    return true;
  }
  const editing = target?.closest?.("input, textarea, select, [contenteditable='true']");
  const back = (key === "Escape" && plain && !editing) || (event.metaKey && key === "[");
  if (!back || !state.selected || !$("session-layout") || $("session-layout").hidden) return false;
  event.preventDefault();
  if (phoneLayout() && state.detailShown) backToList();
  else if (state.dockSolo) backFromTerminal();
  else restoreSelectedCardFocus();
  return true;
}
function chatReceiptCopy(receipt) {
  if (receipt?.dispatchState === "not_started")
    return `${receipt.reason || "메시지 전송 조건을 확인해 주세요."} 메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다.`;
  if (receipt?.status === "failed" && receipt.confirmed === true)
    return "요청 처리 중 오류가 발생했습니다. 대화를 확인해 주세요.";
  const labels = {
    accepted: "메시지를 접수했습니다. 작업 완료는 대화에서 확인해 주세요.",
    queued: "메시지를 접수했습니다. 답변은 대화에서 확인해 주세요.",
    completed: "응답이 끝났습니다. 목표 완료 여부는 결과와 완료 조건을 확인해 주세요.",
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
  persistChatDraft(entry);
  if (!currentChat(entry) || $("chat-composer")?.dataset.source !== chatKey(entry.source)) return;
  updateModelControls(entry);
  const supported = chatSupported(entry);
  const oneShot = chatOneShot(entry);
  $("chat-composer").classList.toggle?.("is-busy", entry.data?.phase === "working" || entry.sending || !!entry.uploads);
  $("chat-composer").classList.toggle?.("is-error", !!entry.error || entry.receipt?.status === "failed");
  $("chat-composer").classList.toggle?.("is-unknown", !!entry.pending && !entry.sending);
  setClass($("chat-composer"), "is-sent-draft", sentDraftHidden(entry));
  const offline = networkOffline();
  const blocked = offline || !supported || entry.sending || !!entry.pending || !!entry.uploads || !!entry.commandBusy;
  // No board terminal yet (Claude background): the draft can be written now and
  // sent once the user opened the terminal from the gate.
  const recovery = chatRecovery(entry);
  const gateOn = !supported && !recovery && terminalRequired(entry);
  $("chat-recovery").hidden = !recovery;
  $("chat-recovery-text").textContent = recovery?.reason || (recovery?.action === "resume_session" ? "대화 기록과 초안을 유지하고 같은 세션을 다시 엽니다." : "");
  $("chat-recover").textContent = entry.recovering ? "연결 확인 중…" : recovery?.action === "refresh_host" ? "기기 연결 다시 확인" : "세션 다시 열기";
  $("chat-recover").disabled = offline || !!entry.recovering || state.launchBusy;
  const gateReason = entry.data?.capability?.reason || "보내려면 먼저 터미널을 여세요.";
  $("chat-terminal-gate").hidden = !gateOn;
  $("chat-terminal-gate-text").textContent = gateOn ? gateReason : "";
  $("chat-open-terminal").disabled = offline || state.launchBusy;
  // A send in flight (or unconfirmed) locks the text but not the field: a
  // disabled field would drop the focus, and with it the keyboard and the
  // next ⌘↩. sendChat itself refuses a second send meanwhile.
  const input = $("chat-input");
  input.placeholder = chatPlaceholder(oneShot);
  input.disabled = !supported && !gateOn && recovery?.action !== "resume_session";
  input.readOnly = !input.disabled && (entry.sending || !!entry.pending || !!entry.commandBusy);
  input.setAttribute("aria-disabled", String(input.disabled || input.readOnly));
  $("chat-attach").disabled = offline || !chatAttachmentsSupported(entry) || entry.sending || !!entry.pending || !!entry.uploads;
  $("chat-clipboard").disabled = $("chat-attach").disabled;
  $("chat-send").disabled = blocked || entry.composing || (!entry.text.trim() && !entry.attachments.length);
  // A listening mic always stays pressable, so a locked input can still end it.
  $("chat-mic").disabled = !dictationListening(input) && (input.disabled || input.readOnly || entry.composing);
  for (const id of ["chat-quick", "chat-quick-inline"])
    if ($(id)) $(id).disabled = blocked || input.disabled || input.readOnly || entry.composing;
  renderDictation();
  $("chat-send").textContent = entry.sending ? "전송 중" : "보내기";
  $("chat-send").setAttribute("aria-label", "메시지 보내기");
  $("chat-key-hint").hidden = oneShot || !!entry.expanded || !keyboardPresent();
  $("chat-check").disabled = offline || entry.checking;
  $("chat-terminal").disabled = state.launchBusy || oneShot;
  $("chat-terminal").hidden = oneShot;
  const continuation = $("detail").querySelector?.(".continue-disclosure");
  if (continuation) continuation.hidden = oneShot;
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
  const unsupportedCopy = oneShot ? ONE_SHOT_NO_INPUT : (gateOn || recovery) ? (entry.receipt?.dispatchState === "not_started"
    ? "메시지는 전송되지 않았습니다. 초안과 첨부는 유지됩니다." : "") : !supported && entry.attachments.length
    ? "첨부는 이 작업 폴더에 저장됐습니다. 첨부의 ‘경로’를 복사해 터미널에 붙여넣으세요."
    : entry.data?.capability?.reason || "직접 입력을 지원하지 않습니다. 터미널을 사용해 주세요.";
  const dictating = state.dictation?.field === input;
  $("chat-status").textContent = (offline ? "오프라인입니다. 초안은 유지됩니다. 연결 후 상태를 확인하고 보내 주세요." : (dictating && entry.dictationNotice) || entry.error || (oneShot ? ONE_SHOT_NO_INPUT : entry.dictationNotice || entry.notice || (entry.uploads ? `첨부 ${entry.uploads}개를 올리고 있습니다…` : !entry.data ? (claudeWithoutId(selectedSession()) ? CLAUDE_NO_SEND : "메시지 전송 환경을 확인하고 있습니다…") : !supported ? unsupportedCopy : receiptNotice(entry) || phaseCopy))) + budgetNote;
  $("chat-status").setAttribute("data-attention", String(!!(offline || chatHostOffline(entry) || entry.error || entry.receipt?.status === "unknown")));
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
  persistChatDraft(entry);
}
const CHAT_RECHECK_RETRY_MS = 3000;
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
    // A transient failure keeps following the session with the same backoff (also when the
    // polling stopped meanwhile: scheduleChatPoll follows only the conversation on screen).
    if (currentChat(entry) && !err.unauthorized && !err.staleAuth) scheduleChatPoll(s, entry);
  } finally {
    entry.checking = false;
    complete(outcome);
    entry.stateReadPromise = null;
    updateChatComposer(entry);
    const recheck = entry.recheckQueued || epoch !== entry.stateEpoch;
    entry.recheckQueued = false;
    // A read asked for while this one ran (a return to the screen, a live update) still
    // happens: at once, or shortly after this one failed, so the old screen never stays.
    if (recheck && currentChat(entry) && chatVisible() && !networkOffline()) {
      if (!readFailed) queueMicrotask(() => loadChatState(s, false));
      else setTimeout(() => { if (currentChat(entry) && chatVisible()) loadChatState(s, false); }, CHAT_RECHECK_RETRY_MS);
    }
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
  // What a finished dictation said gives way to what the send says.
  if (!message && !dictationListening($("chat-input"))) entry.dictationNotice = "";
  const text = message ? String(message.text || "") : entry.text;
  const attachments = (message ? message.attachments || [] : entry.attachments).slice();
  if (!message && currentChat(entry) && /^\/[a-z]+(?:\s|$)/.test(text.trim())) {
    return runSlashCommand(entry, text.trim(), attachments);
  }
  if (!currentChat(entry) || !chatSupported(entry) || entry.composing || entry.sending || entry.pending || entry.uploads ||
      (!text.trim() && !attachments.length)) return;
  if (new TextEncoder().encode(text).length > 32768 || attachments.length > 8) {
    entry.error = entry.inputError = "메시지는 32KiB, 첨부는 8개까지 보낼 수 있습니다. 초안은 유지됩니다.";
    updateChatComposer(entry);
    return;
  }
  // The draft is what is sent: words still being heard never land after it.
  closeQuickReplies();
  if (!message) stopDictation({ abort: true, park: true, field: $("chat-input") });
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
    // A Claude send may have attached the session's board terminal: its marks show it now.
    if (pending.source?.agent === "claude" && result.receipt?.terminalId) refreshTerminals();
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
// How many conversation items (messages and tool records) a read returns at most, newest last.
const CONVERSATION_WINDOW = 80;
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
// ---------- 빠른 답장 ----------
// Selecting a phrase inserts it in the draft and sends through the normal chat flow. This
// browser keeps its own list (설정 › 빠른 답장); until it is edited, these defaults.
const QUICK_REPLIES_DEFAULT = ["계속 진행해 줘.", "좋아, 그렇게 해 줘.", "테스트 돌리고 결과 알려 줘.",
  "커밋하고 푸시해 줘.", "지금까지 한 일을 짧게 정리해 줘."];
const QUICK_REPLY_LIMIT = 12;
const QUICK_REPLY_LENGTH = 200;
function quickRepliesKey() {
  return INSTANCE.storagePrefix + "quickReplies";
}
function quickReplyLines(text) {
  return String(text || "").split("\n").map((line) => Array.from(line.replace(/[\x00-\x1f\x7f-\x9f]/g, " ").trim())
    .map((char) => char.length === 1 && char.charCodeAt(0) >= 0xd800 && char.charCodeAt(0) <= 0xdfff ? "�" : char)
    .slice(0, QUICK_REPLY_LENGTH).join("")).filter(Boolean).slice(0, QUICK_REPLY_LIMIT);
}
function rememberQuickReplyEdits() {
  if (state.quickRepliesEditor && $("quick-replies")) state.quickRepliesUnsaved = {
    text: $("quick-replies").value, baseValue: state.quickRepliesEditor.value, revision: state.quickRepliesEditor.revision };
}
function quickReplies() {
  if (Array.isArray(state.quickRepliesShared?.quickReplies)) return state.quickRepliesShared.quickReplies.slice();
  if (state.quickRepliesLocal) return state.quickRepliesLocal.slice();
  try {
    const saved = JSON.parse(localStorage.getItem(quickRepliesKey()) || "null");
    if (Array.isArray(saved))
      return saved.filter((text) => typeof text === "string" && text.trim())
        .flatMap((text) => quickReplyLines(text)).slice(0, QUICK_REPLY_LIMIT);
  } catch {
    // Unreadable storage: the defaults.
  }
  return QUICK_REPLIES_DEFAULT.slice();
}
function saveQuickReplies(text) {
  const rows = quickReplyLines(text);
  try {
    localStorage.setItem(quickRepliesKey(), JSON.stringify(rows));
    state.quickRepliesError = "";
  } catch {
    state.quickRepliesError = state.quickRepliesShared?.quickReplies != null
      ? "보드에는 저장됐습니다. 이 기기의 임시 복사만 보관하지 못했습니다."
      : "이 기기에 문구를 보관하지 못했습니다. 현재 화면에서는 사용할 수 있습니다.";
  }
  state.quickRepliesLocal = rows;
  renderQuickReplySettings();
  return rows;
}
function renderQuickReplySettings(text = "") {
  const status = $("quick-replies-status");
  if (status) status.textContent = text || (state.quickRepliesSaving ? "다른 기기와 공유할 문구를 저장하고 있습니다…"
    : state.quickRepliesError || (state.quickRepliesShared?.quickReplies != null ? "보드에 저장됨 · 모든 기기에서 공유합니다."
      : "기존 문구입니다. 저장하면 모든 기기에서 공유합니다."));
  for (const id of ["quick-replies-save", "quick-replies"]) if ($(id)) $(id).disabled = !!state.quickRepliesSaving;
}
function adoptQuickReplyPreferences(data) {
  if (data?.ok !== true || !Number.isInteger(data.revision) || data.revision < 0 ||
      data.quickReplies !== null && (!Array.isArray(data.quickReplies) || data.quickReplies.length > QUICK_REPLY_LIMIT ||
        data.quickReplies.some((row) => typeof row !== "string" || !row.trim() || Array.from(row).length > QUICK_REPLY_LENGTH))) {
    if (data?.ok === false) { state.quickRepliesError = data.error || "공유 문구를 읽지 못했습니다."; renderQuickReplySettings(); }
    return false;
  }
  if (state.quickRepliesShared && data.revision < state.quickRepliesShared.revision) return false;
  state.quickRepliesShared = { revision: data.revision, quickReplies: data.quickReplies?.slice() ?? null };
  const editor = state.quickRepliesEditor, input = $("quick-replies");
  if ($("settings-dialog")?.open && editor && input && !state.quickRepliesSaving) {
    if (input.value === editor.value) {
      input.value = quickReplies().join("\n"); editor.value = input.value; editor.revision = data.revision;
    } else if (editor.revision === null) editor.revision = data.revision;
    else if (editor.revision !== data.revision)
      state.quickRepliesError = "다른 기기에서 문구를 바꿨습니다. 편집 내용은 유지됩니다. 최신 문구를 불러온 뒤 다시 편집해 주세요.";
  }
  renderQuickReplySettings();
  return true;
}
async function loadQuickReplyPreferences({ replaceEditor = false } = {}) {
  const authEpoch = state.authEpoch;
  const editor = state.quickRepliesEditor, text = $("quick-replies")?.value;
  if (!state.authenticated || networkOffline()) return false;
  try {
    const data = await api("/api/preferences", { authEpoch });
    if (authEpoch !== state.authEpoch || !state.authenticated) return false;
    const replacing = replaceEditor && state.quickRepliesEditor === editor && $("quick-replies")?.value === text;
    if (replacing && editor) editor.value = text;
    state.quickRepliesError = "";
    const adopted = adoptQuickReplyPreferences(data);
    if (adopted && replacing) state.quickRepliesUnsaved = null;
    return adopted;
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      state.quickRepliesError = err.message; renderQuickReplySettings();
    }
    return false;
  }
}
function saveSharedQuickReplies() {
  if (state.quickRepliesSaving) return state.quickRepliesSaving;
  const input = $("quick-replies"), editor = state.quickRepliesEditor;
  if (!input || !editor || !state.authenticated) return Promise.resolve(false);
  const text = input.value, authEpoch = state.authEpoch;
  const rows = quickReplyLines(text);
  const revision = editor.revision;
  if (networkOffline() || revision === null) {
    state.quickRepliesError = "연결 후 최신 문구를 불러온 뒤 저장해 주세요. 편집 내용은 유지됩니다.";
    renderQuickReplySettings(); return Promise.resolve(false);
  }
  state.quickRepliesError = "";
  const saving = (async () => {
    try {
      const data = await api("/api/preferences/quick-replies", { method: "POST", body: { quickReplies: rows, revision }, authEpoch });
      if (authEpoch !== state.authEpoch || !state.authenticated || state.quickRepliesEditor !== editor) return false;
      if (data?.ok !== true || JSON.stringify(data.quickReplies) !== JSON.stringify(rows) || !adoptQuickReplyPreferences(data))
        throw new Error("저장 결과를 확인하지 못했습니다. 최신 문구를 다시 불러와 확인해 주세요.");
      saveQuickReplies(rows.join("\n"));
      input.value = rows.join("\n"); editor.value = input.value; editor.revision = data.revision;
      state.quickRepliesUnsaved = null;
      return true;
    } catch (err) {
      if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
        state.quickRepliesError = `${err.message} 편집 내용은 유지됩니다.`;
      }
      return false;
    } finally {
      if (state.quickRepliesSaving === saving) { state.quickRepliesSaving = null; renderQuickReplySettings(); }
    }
  })();
  state.quickRepliesSaving = saving;
  renderQuickReplySettings();
  return saving;
}
function openQuickReplies(entry, input, trigger) {
  const ready = () => currentChat(entry) && input === $("chat-input") && !input.disabled && !input.readOnly &&
    !networkOffline() && chatSupported(entry) && !entry.composing && !entry.sending && !entry.pending &&
    !entry.uploads && !entry.commandBusy;
  if (!ready()) return;
  const choose = (text) => {
    closeQuickReplies();
    // The session, editor or send state may have changed while the choices were open.
    if (!ready()) return;
    insertChatText(entry, input, text);
    if (document.activeElement !== input) input.focus?.({ preventScroll: true });
    return sendChat(entry);
  };
  const replies = quickReplies();
  const composer = touchDevice() && document.activeElement === input ? input.closest?.(".chat-composer") : null;
  if (composer) {
    const same = state.quickRepliesOpen?.input === input;
    closeQuickReplies();
    if (same) return;
    const panel = el("div", "chat-quick-options");
    panel.id = "chat-quick-options";
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", "빠른 답장 · 문구를 선택하면 바로 전송");
    const add = (text, run) => {
      const item = button(text, "action-item", run);
      for (const type of ["pointerdown", "mousedown"])
        item.addEventListener(type, (event) => event.preventDefault());
      panel.append(item);
      return item;
    };
    for (const text of replies) add(text, () => choose(text));
    if (!replies.length) add("설정 › 빠른 답장에서 문구를 추가해 주세요", () => {}).disabled = true;
    add("닫기", closeQuickReplies);
    trigger?.setAttribute?.("aria-expanded", "true");
    trigger?.setAttribute?.("aria-haspopup", "false");
    trigger?.setAttribute?.("aria-controls", panel.id);
    state.quickRepliesOpen = { panel, input, trigger };
    composer.append(panel);
    return;
  }
  // Chosen or cancelled, the keyboard goes back to the input, not to the closed tools menu.
  trigger?.setAttribute?.("aria-haspopup", "dialog");
  openActionSheet({ eyebrow: "문구를 선택하면 바로 전송", title: "빠른 답장", trigger: input || trigger,
    actions: replies.length
      ? replies.map((text) => ({ label: text, run: () => choose(text) }))
      : [{ label: "설정 › 빠른 답장에서 문구를 추가해 주세요", disabled: true, run() {} }] });
}
function closeQuickReplies() {
  const opened = state.quickRepliesOpen;
  state.quickRepliesOpen = null;
  opened?.panel.remove?.();
  opened?.trigger?.setAttribute?.("aria-expanded", "false");
}
// A Claude background conversation without a board terminal: its connection opens as its user
// starts a message (typing, the mic or a quick reply; chat.py warm), so the send then pastes at
// once instead of waiting for the attach. Once a minute at most; nothing is typed by it.
const WARM_SPACING_MS = 60000;
// The terminal a send pastes into, as the server picks it: a live one linked to this session
// for sure (not a 새 세션 link guessed from start times, nor one only started from it).
function sendTerminalFor(s) {
  const key = chatKey(sourceOf(s));
  return state.terminals.find((terminal) => {
    const info = terminalInfo(terminal);
    return terminal.alive !== false && !terminal.closed && info.linkConfidence !== "estimated" &&
      info.nativeSource?.host && chatKey(info.nativeSource) === key;
  }) || null;
}
function warmChat(entry) {
  if (Date.now() - (entry.warmedAt || 0) < WARM_SPACING_MS || entry.source?.agent !== "claude") return;
  // Not more than every few seconds while typing (a terminal already open is the usual answer).
  if (Date.now() - (entry.warmCheckedAt || 0) < 5000) return;
  entry.warmCheckedAt = Date.now();
  const s = allSessions().find((session) => chatKey(sourceOf(session)) === chatKey(entry.source));
  // A finished session is not woken by typing; its send opens the connection as before.
  if (s?.kind !== "background" || s.phase === "done" || sendTerminalFor(s)) return;
  entry.warmedAt = Date.now();
  api("/api/chat/warm", { method: "POST", body: { source: entry.source } }).catch(() => {
    // The send opens it then, as before.
  });
}
function insertChatText(entry, input, text) {
  if (!text || !currentChat(entry) || input.disabled || input.readOnly) return;
  warmChat(entry);
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
async function recoverChatSession(s) {
  if (!state.authenticated || state.selected !== s.key || state.launchBusy || networkOffline()) return;
  const entry = chatEntry(s);
  const recovery = chatRecovery(entry);
  if (!recovery || entry.recovering) return;
  if (recovery.action === "resume_session") {
    state.route = { host: s.host, agent: s.agent, account: accountOf(s) };
    return preparePlan({ resume: true });
  }
  const authEpoch = state.authEpoch;
  entry.recovering = true;
  entry.error = "";
  updateChatComposer(entry);
  try {
    await api("/api/refresh", { method: "POST", body: { host: s.host }, authEpoch });
    assertAuthEpoch(authEpoch);
    await poll(false);
    assertAuthEpoch(authEpoch);
    if (currentChat(entry) && !chatHostOffline(entry)) {
      state.chatPollCount = 0;
      await loadChatState(selectedSession(), true, { join: true });
    }
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth)
      entry.error = err.message || "기기 연결을 확인하지 못했습니다. 초안은 유지됩니다.";
  } finally {
    if (authEpoch === state.authEpoch) { entry.recovering = false; updateChatComposer(entry); }
  }
}
function openChatTerminal(s) {
  if (state.selected !== s.key || state.launchBusy) return;
  const bot = botRequestForNative(sourceOf(s));
  if (bot) { selectBotRequest(botRequestKey(bot)); return; }
  if (chatOneShot(chatEntry(s))) { toast(ONE_SHOT_NO_INPUT); return; }
  if (chatRecovery(chatEntry(s))) return recoverChatSession(s);
  if (state.terminalAlive && chatKey(terminalInfo(state.terminal).nativeSource || {}) === chatKey(sourceOf(s))) {
    openTerminal(state.terminal);
    return;
  }
  // The session already has a live board terminal (its "터미널 열림" mark): that one opens,
  // never a second connection to the same session.
  const open = terminalForSession(s);
  if (open) {
    if (terminalEstimated(open)) toast("시작 시각으로 추정한 이 세션의 터미널을 엽니다.");
    openTerminal(open);
    return;
  }
  if (s.nativeSource) { toast("이 실행의 터미널이 종료됐습니다. 새로고침 후 세션 목록에서 선택해 주세요."); return; }
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
  if (boardCovered()) returnToBoard({ selectLaunchedSource: false });
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
function cancelHistorySearch() {
  const search = state.historySearch;
  search.run++; search.controller?.abort(); search.controller = null;
  if (search.busy) search.status = "검색을 멈췄습니다. 찾은 내용은 유지됩니다.";
  search.busy = false;
}
function renderHistorySearch() {
  const search = state.historySearch, panel = $("body-search-panel");
  if (!panel) return;
  panel.hidden = !search.mode;
  $("groups").hidden = search.mode;
  $("search-body")?.setAttribute?.("aria-pressed", String(search.mode));
  if ($("search")) {
    $("search").placeholder = search.mode ? "저장 대화 본문 검색 · 2글자 이상" : "세션·프로젝트 검색";
    $("search").setAttribute("aria-label", search.mode ? "저장 대화 본문 검색" : "세션·프로젝트·기기 검색");
  }
  if (!search.mode) return;
  $("body-search-start").disabled = search.busy || search.hits.size >= 500;
  $("body-search-start").textContent = search.cursor ? "계속 검색" : "본문 검색";
  $("body-search-stop").hidden = !search.busy;
  $("body-search-status").textContent = search.status || "모든 목록의 저장 대화 본문을 검색합니다. 검색어를 2글자 이상 입력해 주세요.";
  const results = $("body-search-results"), desired = [...search.hits.values()].map((hit) => {
    if (!hit.row) {
      hit.row = button("", "body-search-result", () => openHistoryHit(hit));
      hit.row.append(el("strong", null, hit.title || "제목 없는 대화"),
        el("span", "body-search-meta", `${hit.hostLabel || hit.source.host} · ${hit.role === "user" ? "나" : "에이전트"}${hit.ts ? " · " + clock(hit.ts) : ""}`),
        el("span", "body-search-snippet", hit.snippet));
    }
    return hit.row;
  });
  for (const row of [...results.children]) if (!desired.includes(row)) results.removeChild(row);
  desired.forEach((row, index) => { if (results.children[index] !== row) results.insertBefore(row, results.children[index] || null); });
}
async function startHistorySearch({ more = false } = {}) {
  const search = state.historySearch;
  if (search.busy || !search.mode || !state.authenticated) return;
  const query = search.query.trim();
  if (query.length < 2 || query.length > 160 || networkOffline()) {
    search.status = networkOffline() ? "연결 후 본문을 검색해 주세요." : "검색어를 2~160자로 입력해 주세요.";
    renderHistorySearch(); return;
  }
  const authEpoch = state.authEpoch, run = ++search.run;
  search.busy = true; search.controller = new AbortController();
  if (!more) { search.hits.clear(); search.errors.clear(); search.cursor = null; search.skipped = 0; }
  const current = () => authEpoch === state.authEpoch && state.authenticated && search.mode && search.run === run && search.query === query;
  search.status = "저장 대화 본문을 검색하고 있습니다…"; renderHistorySearch();
  try {
    for (let page = 0; page < 60 && current(); page++) {
      const params = new URLSearchParams({ q: query, includeRemoved: state.showRemoved ? "1" : "0", ...(search.cursor ? { cursor: search.cursor } : {}) });
      const data = await api(`/api/history/search?${params}`, { authEpoch, signal: search.controller.signal });
      if (!current()) return;
      if (data.readOnly !== true || !Array.isArray(data.hits) || data.hits.length > 60 || !Array.isArray(data.errors) ||
          !Number.isInteger(data.scanned) || !Number.isInteger(data.total) || data.scanned < 0 || data.scanned > data.total ||
          data.cursor !== null && typeof data.cursor !== "string") throw new Error("본문 검색 응답을 확인하지 못했습니다.");
      for (const hit of data.hits) {
        if (!hit?.source || !/^[a-f0-9]{64}$/.test(hit.bodyHash || "") || typeof hit.snippet !== "string" ||
            !Number.isInteger(hit.recordOffset) || hit.recordOffset < 0) throw new Error("본문 검색 결과가 올바르지 않습니다.");
        const key = JSON.stringify([hit.source, hit.bodyHash, hit.recordOffset]);
        if (!search.hits.has(key)) search.hits.set(key, hit);
        if (search.hits.size >= 500) break;
      }
      for (const error of data.errors) search.errors.set(JSON.stringify(error.source), error);
      search.skipped = (search.skipped || 0) + (data.skipped || 0);
      search.status = `${data.scanned}/${data.total}개 대화 확인 · ${search.hits.size}개 메시지 찾음` +
        (search.errors.size ? ` · ${search.errors.size}개 대화는 연결·저장 기록을 확인해야 합니다.` : "") +
        (search.skipped ? " · 읽지 못한 기록 일부가 있습니다." : "");
      if (data.cursor && data.cursor === search.cursor) throw new Error("검색 위치가 진행되지 않아 중단했습니다.");
      search.cursor = data.cursor; renderHistorySearch();
      if (!search.cursor || search.hits.size >= 500) break;
    }
    if (current() && search.hits.size >= 500) search.status += " 결과가 많아 500개까지만 표시합니다. 검색어를 좁혀 주세요.";
    else if (current() && search.cursor) search.status += " 계속 검색으로 이전 기록을 이어서 확인할 수 있습니다.";
  } catch (err) {
    if (current() && !err.unauthorized && !err.staleAuth && err.name !== "AbortError") {
      if (err.code === "history_catalog_changed") search.cursor = null;
      search.status = `${err.message} 찾은 내용은 유지됩니다.`;
    }
  } finally {
    if (search.run === run && authEpoch === state.authEpoch) { search.busy = false; renderHistorySearch(); }
  }
}
function openHistoryHit(hit) {
  if (!state.authenticated) return;
  const s = allSessions().find((row) => chatKey(sourceOf(row)) === chatKey(hit.source) &&
    (!hit.source.sessionId || row.sessionId === hit.source.sessionId));
  if (!s) { toast("이 대화의 목록 정보가 바뀌었습니다. 다시 검색해 주세요."); return; }
  const entry = chatEntry(s); entry.historyPreview = { ...hit, item: null };
  prepareSessionListFor(s); select(s.key);
  queueMicrotask(() => {
    if (!currentChat(entry) || !entry.historyPreview) return;
    historyPreviewRow(s, entry); refreshConversationView(entry);
    loadLongMessage(s, entry.historyPreview.item);
  });
}
function longMessageControls(s, entry, item) {
  if (!entry || !['user', 'assistant'].includes(item.role) || !item.message.truncated && !item.history) return;
  const hash = item.message.bodyHash;
  const identity = conversationIdentity(s);
  if (item.longEntry && item.longEntry !== entry || item.longIdentity && item.longIdentity !== identity) {
    item.row.removeChild(item.longControls); item.longControls = null; item.longSignature = null;
  }
  item.longEntry = entry;
  item.longIdentity = identity;
  if (!item.longControls) {
    const bar = el("div", "msg-long-controls");
    item.longStatus = el("span", "msg-long-status"); item.longStatus.setAttribute("role", "status");
    item.longLoad = button("전체 보기", "secondary", () => loadLongMessage(s, item));
    item.longFold = button("접기", "secondary", () => {
      const cached = entry.fullMessages.get(hash);
      if (!cached) return;
      cached.controller?.abort(); cached.run++; cached.busy = false; cached.visible = false;
      longMessageControls(s, entry, item);
    });
    bar.append(item.longStatus, item.longLoad, item.longFold); item.row.append(bar); item.longControls = bar;
  }
  const full = entry.fullMessages.get(hash);
  const visible = full?.visible && !!full.text;
  item.displayMessage = visible ? { ...item.message, text: full.text, truncated: !full.complete } : null;
  item.truncated = visible ? !full.complete : item.message.truncated === true;
  item.longLoad.hidden = !!(visible && full.complete);
  item.longLoad.disabled = !!full?.busy;
  item.longLoad.textContent = full?.busy ? "불러오는 중…" : full?.text && !full.complete ? "다음 부분 불러오기" : "전체 보기";
  item.longFold.hidden = !full?.visible && !full?.busy;
  item.longStatus.textContent = full?.error || (full?.busy ? "저장된 본문을 읽고 있습니다." : visible && !full.complete
    ? `${Array.from(full.text).length.toLocaleString()}자를 불러왔습니다.` : visible ? "전체 본문" : "");
  item.longStatus.hidden = !item.longStatus.textContent;
  if (item.truncatedNote) item.truncatedNote.hidden = !!visible;
  const signature = JSON.stringify([!!visible, visible ? full.text : null, visible ? full.complete : null]);
  if (item.longSignature !== signature) {
    item.longSignature = signature;
    const message = item.displayMessage || item.message;
    item.bubble.replaceChildren();
    if (item.role === "assistant" && typeof MessageFormat !== "undefined")
      MessageFormat.render(item.bubble, message.text || "", { onCopy: (text) => copyText(text, "코드를 복사했습니다.") });
    else if (item.role === "user") {
      const shown = userMessageParts(message);
      if (shown.attachments.length) {
        if (shown.body) item.bubble.append(el("span", "msg-body", shown.body));
        item.bubble.append(attachmentGroup(shown.attachments, `첨부 ${shown.attachments.length}개`, { showCaption: false }));
      } else item.bubble.textContent = shown.body;
    }
    else item.bubble.textContent = message.text || "";
  }
}
async function loadLongMessage(s, item) {
  const entry = chatEntry(s), hash = item.message.bodyHash, authEpoch = state.authEpoch;
  if (!currentChat(entry) || networkOffline()) return;
  if (!/^[a-f0-9]{64}$/.test(hash || "")) {
    toast("메시지 정보를 갱신합니다. 다시 전체 보기를 눌러 주세요.");
    loadDetail(s, true); return;
  }
  let full = entry.fullMessages.get(hash);
  if (!full) {
    full = { text: "", complete: false, visible: true, busy: false, run: 0, scanOffset: item.message.recordOffset || 0,
      recent: !Number.isInteger(item.message.recordOffset),
      textOffset: 0, recordOffset: null, error: "", controller: null };
    entry.fullMessages.set(hash, full);
  }
  if (full.busy) return;
  full.visible = true;
  if (full.complete) { longMessageControls(s, entry, item); return; }
  full.busy = true; full.error = ""; const run = ++full.run; full.controller = new AbortController();
  const current = () => authEpoch === state.authEpoch && state.authenticated && currentChat(entry) &&
    state.chats.get(chatKey(entry.source)) === entry && full.run === run;
  longMessageControls(s, entry, item);
  try {
    for (let page = 0; page < 32 && current(); page++) {
      const params = new URLSearchParams({ ...sourceOf(s), ...(s.agent === "claude" && s.sessionId ? { sessionId: s.sessionId } : {}), bodyHash: hash,
        offset: String(full.recordOffset ?? full.scanOffset), textOffset: String(full.textOffset), recent: full.recent ? "1" : "0" });
      const data = await api(`/api/history/message?${params}`, { authEpoch, signal: full.controller.signal });
      if (!current()) return;
      if (data.readOnly !== true) throw new Error("저장 기록 응답을 확인하지 못했습니다.");
      if (data.error) throw new Error(data.error);
      if (data.forwardFallback === true && full.recent) { full.recent = false; full.scanOffset = 0; continue; }
      if (typeof data.text === "string") {
        if (data.bodyHash !== hash || data.textOffset !== full.textOffset || !Number.isInteger(data.recordOffset) ||
            !Number.isInteger(data.fullLength) || data.fullLength < 0 || Array.from(data.text).length > 65536 ||
            data.nextTextOffset !== null && (!Number.isInteger(data.nextTextOffset) || data.nextTextOffset <= full.textOffset))
          throw new Error("본문 응답을 확인하지 못했습니다.");
        if (new TextEncoder().encode(full.text + data.text).length > 2 * 1024 * 1024) throw new Error("이 화면에서 펼칠 수 있는 길이를 넘었습니다. 원본 기록에서 확인해 주세요.");
        full.text += data.text; full.recordOffset = data.recordOffset; full.fullLength = data.fullLength;
        full.textOffset = data.nextTextOffset ?? data.fullLength; full.complete = data.nextTextOffset === null;
        break;
      }
      if (!Number.isInteger(data.nextOffset) || data.nextOffset <= full.scanOffset)
        throw new Error("저장된 본문을 찾지 못했습니다. 대화를 새로고침한 뒤 다시 시도해 주세요.");
      full.scanOffset = data.nextOffset;
      if (page === 31) full.error = "긴 기록을 계속 찾으려면 전체 보기를 다시 눌러 주세요.";
    }
  } catch (err) {
    if (current() && !err.unauthorized && !err.staleAuth && err.name !== "AbortError") full.error = err.message;
  } finally {
    if (authEpoch === state.authEpoch && full.run === run) {
      full.busy = false;
      if (currentChat(entry)) longMessageControls(s, entry, item);
    }
  }
}
function historyPreviewRow(s, entry) {
  const preview = entry?.historyPreview;
  if (!preview) return null;
  if (!preview.item) {
    const row = el("section", "history-message-preview");
    row.setAttribute("aria-label", "검색한 저장 메시지");
    const heading = el("div", "history-preview-heading");
    heading.append(el("strong", null, "검색한 메시지 · 저장 기록"), button("닫기", "secondary", () => {
      if (entry.historyPreview === preview) { entry.historyPreview = null; refreshConversationView(entry); }
    }));
    const bubble = el("div", "bubble"); row.append(heading, bubble);
    preview.item = { row, bubble, role: preview.role, history: true, truncated: true,
      message: { role: preview.role, text: preview.snippet, bodyHash: preview.bodyHash,
        recordOffset: preview.recordOffset, truncated: true, fullLength: preview.fullLength } };
    row.append(button("본문 복사", "secondary", () => copyText(messagePlainText(preview.item),
      preview.item.truncated ? "불러온 부분을 복사했습니다." : "전체 본문을 복사했습니다.")));
  }
  longMessageControls(s, entry, preview.item);
  return preview.item.row;
}
function renderMessages(s, messages, error = "", fresh = false) {
  const list = $("messages");
  if (!list) return;
  const source = chatKey(sourceOf(s));
  const entry = state.chats.get(source) || null;
  const historyNotice = messages?.length ? "" : conversationHistoryNotice(s, entry);
  const previous = list.conversationSource === source ? list.conversationRows || [] : [];
  // A failed refresh never removes a conversation that was already readable.
  if (messages == null || (error && !messages.length && previous.length))
    messages = previous.map((item) => item.message);
  // Bot execution details stay in the original session; this view follows its chat.
  if (sessionCategoryOf(s) === "bot")
    messages = messages.filter((message) => ["user", "assistant"].includes(message.role));
  const selectedText = window.getSelection?.();
  const readingSelection = selectedText && !selectedText.isCollapsed &&
    (list.contains?.(selectedText.anchorNode) || list.contains?.(selectedText.focusNode));
  // Pinned (or within 50px of the end): stays at the newest message, also when the
  // list was laid out at another size (hidden, a question card, the keyboard).
  const stick = !readingSelection &&
    (list.pendingView ? list.pendingView.pinned :
      fresh || pinnedToEnd(list) || list.scrollHeight - list.scrollTop - list.clientHeight < 50);
  if (stick) list.pinned = true;
  const scroll = list.scrollTop;
  const previousKeys = new Set(previous.map((item) => item.key));
  const oldWarningHeight = list.conversationError && [...list.children].includes(list.conversationError)
    ? list.conversationError.offsetHeight || 0 : 0;
  const keys = messages.map((message) => JSON.stringify([message.role, message.text, message.ts || null,
    attachmentMetadata(message.attachments), message.truncated === true, message.bodyHash || null]));
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
    if (retained) { longMessageControls(s, entry, retained); return retained; }
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
      row.truncatedNote = el("p", "msg-truncated", `긴 메시지라 앞부분만 표시합니다${total}. 전체 보기로 나머지를 불러올 수 있습니다.`);
      row.append(row.truncatedNote);
    }
    // Copy, code copy, selection and the terminal live in a menu: long press
    // on touch, right click or this button (shown on hover) with a mouse.
    const item = { key, message, row, bubble, role, truncated, truncatedNote: row.truncatedNote };
    row.messageItem = item;
    if (role !== "system") {
      const more = button("", "msg-more", () => openMessageMenu(row));
      more.setAttribute("aria-label", "메시지 메뉴");
      more.setAttribute("aria-haspopup", "dialog");
      more.title = "메시지 메뉴";
      row.moreButton = more;
      row.append(more);
    }
    longMessageControls(s, entry, item);
    return item;
  });
  groupMessageRows(rows);
  const desired = toolGroupRows(list, rows);
  const preview = historyPreviewRow(s, entry);
  if (preview) desired.unshift(preview);
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
    working = !historyNotice && phaseWorking && (!lastRole || lastRole === "user" || lastRole === "tool");
    // Only an exact start (Codex's running turn) is counted; Claude's newest
    // prompt time is an approximation and shows no time.
    list.workingSince = live && live.workingSinceApprox !== true ? live.workingSince ?? null : null;
    if (working) tail.push(workingRow(list));
  }
  if (error) {
    // A Codex account without its app-server daemon: listed from its state DB, readable
    // once the daemon runs. "앱 서버 켜기" starts it on that host.
    const serverOff = s.agent === "codex" && s.appServer === "off";
    const errorKey = error + (serverOff ? "\0off" : "");
    if (!list.conversationError || list.conversationErrorText !== errorKey || list.conversationSource !== source) {
      const warning = el("div", `conversation-read-error${serverOff ? " app-server-off" : ""}`);
      warning.setAttribute("role", "status");
      warning.append(el("p", "detail-error", error));
      if (serverOff) {
        warning.append(el("p", "detail-note", "켜면 이 계정의 Codex 앱 서버가 이 기기에서 계속 실행되고, 이 계정의 다음 Codex 세션도 같은 앱 서버를 씁니다."),
          button("앱 서버 켜기", "primary app-server-start", (event) => startAppServer(s, event?.currentTarget)));
      } else {
        warning.append(button("대화 다시 불러오기", "secondary", () => {
          const current = selectedSession();
          if (current?.key === s.key) loadDetail(current, !list.conversationRows?.length);
        }));
      }
      list.conversationError = warning;
      list.conversationErrorText = errorKey;
    }
    desired.unshift(list.conversationError);
  } else {
    list.conversationError = null;
    list.conversationErrorText = "";
    if (historyNotice) {
      const note = list.conversationHistoryNotice ||= el("section", "conversation-history-notice");
      note.setAttribute("role", "status");
      if (note.dataset.notice !== historyNotice) {
        note.replaceChildren(el("strong", "", "일회성 실행 · 기록 저장 안 함"), el("p", "", historyNotice));
        note.dataset.notice = historyNotice;
      }
      desired.unshift(note);
    } else if (claudeWithoutId(s) && !s.sessionId) {
      // One explanation instead of a read error here and another in the composer.
      list.conversationUnreadable ||= el("p", "group-empty conversation-unreadable", CLAUDE_UNREADABLE);
      desired.unshift(list.conversationUnreadable);
    } else if (!rows.length && !tail.length) {
      list.conversationEmpty ||= el("p", "group-empty", "저장된 대화를 아직 찾지 못했습니다.");
      desired.push(list.conversationEmpty);
    }
  }
  // The server sends the newest CONVERSATION_WINDOW items: anything before them is not shown.
  if (messages.length >= CONVERSATION_WINDOW) {
    list.conversationWindowNotice ||= el("p", "conversation-window-notice",
      `최근 ${CONVERSATION_WINDOW}개 항목까지만 보입니다. 그보다 앞의 대화는 여기에 나오지 않습니다.`);
    desired.unshift(list.conversationWindowNotice);
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
  restoreConversationView(list);
  if (list.pendingView && typeof requestAnimationFrame === "function") requestAnimationFrame(() => restoreConversationView(list));
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
  const message = item?.displayMessage || item?.message;
  const text = message?.text || "";
  if (item?.role === "user") return userMessageParts(message).body;
  return item?.role === "assistant" && typeof MessageFormat !== "undefined" ? MessageFormat.plainText(text) : text;
}
function messageActions(s, item) {
  const actions = [];
  const text = messagePlainText(item);
  if (item.group && text.trim()) {
    actions.push({ label: `도구 기록 ${item.items.length}개 모두 복사`, run: () => copyText(text, "도구 실행 기록을 복사했습니다.") });
  } else if (text.trim()) {
    actions.push({ label: item.truncated ? "표시된 부분 복사" : "복사", run: () => copyText(text, item.truncated
      ? "표시된 부분만 복사했습니다. 전체 보기로 나머지를 불러올 수 있습니다." : "메시지 본문을 복사했습니다.") });
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
      tap = touchDevice() && event.pointerType !== "mouse" && event.isPrimary !== false && settled
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

function navigateView(view, replace = false, extra = {}) {
  const current = window.history.state;
  if (!replace && current?.[INSTANCE.historyKey] && current.view === view) return;
  // `notice` belongs to the one entry a notification opened.
  const { notice, ...kept } = current || {};
  const depth = view === "list" ? 0 : replace ? (current?.listDepth || 0)
    : current?.[INSTANCE.historyKey] && Number.isInteger(current.listDepth) ? current.listDepth + 1 : 0;
  const navigationId = replace && current?.[INSTANCE.historyKey] && current.navigationId ? current.navigationId : requestId();
  const parentDetailId = view === "terminal" && current?.[INSTANCE.historyKey] && current.view === "detail" ? current.navigationId : null;
  window.history[replace ? "replaceState" : "pushState"](
    { ...kept, ...extra, [INSTANCE.historyKey]: true, view, listDepth: depth, navigationId, parentDetailId },
    "",
  );
}
function backToList() {
  // The list stepped aside for a docked terminal: back brings it, the terminal steps aside.
  if (state.dockSolo) {
    backFromTerminal();
    return;
  }
  rememberConversationView();
  if (!state.selectedBotRequest && window.SessionholicGoals?.backFromSession?.()) return;
  stopChatPolling();
  if (
    window.history.state?.[INSTANCE.historyKey] &&
    window.history.state.view === "detail"
  )
    window.history.back();
  else setDetailShown(false, { direction: "pop", focus: true });
}
function backFromTerminal() {
  // Opening beside the board replaces its history entry; a fresh page has no entry to go
  // back to. Hiding that pane is local and keeps the conversation and run available.
  if (state.docked) {
    undock();
    return;
  }
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
  disclosure.hidden = sessionOneShot(s);
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

async function preparePlan({ resume = false } = {}) {
  const s = selectedSession();
  if (!s || !state.route) return;
  if (chatOneShot(chatEntry(s))) { toast(ONE_SHOT_NO_INPUT); return; }
  const bot = botRequestForNative(sourceOf(s));
  if (bot) { selectBotRequest(botRequestKey(bot)); return; }
  if (state.launchBusy || $("plan-dialog").open) {
    $(state.launchBusy ? "plan-dialog" : "plan-cancel").focus();
    return;
  }
  if (resume && chatRecovery(chatEntry(s))?.action !== "resume_session") return;
  const target = resume ? { host: s.host, agent: s.agent, account: accountOf(s) } : { ...state.route };
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
      body: { source: sourceOf(s), target, ...(resume ? { resume: true } : {}) },
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
    ? plan.mode === "resume"
      ? "같은 세션을 다시 열까요?"
      : plan.mode === "transfer"
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
    plan.mode === "resume"
      ? "세션 다시 열기"
      : plan.mode === "transfer"
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
    if (result.goalLinkWarning) notice(result.goalLinkWarning);
    window.SessionholicGoals?.refresh?.();
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
// A terminal is named after the session it runs once the board knows it (its
// nativeSource): that session's board title, a 제목 수정 alias included. A
// "새 세션" terminal that names no session keeps its launcher label with the
// time it opened, so several of them stay apart.
function terminalTitle(terminal, fallback = "열어 둔 터미널") {
  const info = terminalInfo(terminal);
  if (info.nativeSource?.host) {
    const key = chatKey(info.nativeSource);
    const session = allSessions().find((s) => chatKey(sourceOf(s)) === key);
    if (session?.title) return terminalEstimated(terminal) ? `${session.title} · 연결 추정` : session.title;
  }
  const title = info.title || fallback;
  if (info.mode === "new-session" && !info.nativeSource) {
    // What the launcher ended up running, read from its host (launcher_programs).
    const folder = String(info.programCwd || "").split("/").filter(Boolean).pop();
    if (info.program === "claude-agents") return `Claude 에이전트 목록${folder ? ` · ${folder}` : ""}`;
    const started = Number.isInteger(info.createdAt) ? ` · ${clock(info.createdAt)} 시작` : "";
    if (AGENTS[info.program]) return `${AGENTS[info.program]}${started}`;
    return `${title}${started}`;
  }
  return title;
}
// A Claude connection to an existing session: the board opens these to send or answer
// and closes them after ten unused minutes. A Codex connection the user opened stays listed.
function connectionTerminal(terminal) {
  const info = terminalInfo(terminal);
  return info.mode === "attach" && (!info.launchMode || info.launchMode === "attach") && info.agent === "claude";
}
// Two 새 세션 terminals on one host showing the same Claude agent view (same folder):
// every later one repeats the first. {terminal id: the first terminal}.
function duplicateTerminals(terminals) {
  const first = new Map(), repeats = new Map();
  const ordered = [...terminals].sort((a, b) => (terminalInfo(a).createdAt || 0) - (terminalInfo(b).createdAt || 0));
  for (const terminal of ordered) {
    const info = terminalInfo(terminal);
    if (info.mode !== "new-session" || info.program !== "claude-agents") continue;
    const key = [info.host, info.program, info.programCwd || ""].join("|");
    if (first.has(key)) repeats.set(terminal.id, first.get(key));
    else first.set(key, terminal);
  }
  return repeats;
}
// Session titles arrive with each snapshot: open terminals follow them. An open
// sheet is not rebuilt under a finger reaching for 연결 닫기; it renames on reopen.
function renameTerminals() {
  if (!$("terminals-dialog")?.open) renderTerminals();
  if (state.terminal && state.terminalVisible)
    $("terminal-title").textContent = terminalTitle(state.terminal, "진행 중인 작업");
  renderTerminalReturn();
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
  if (mode === "new-session") return {
    action: "실행 종료", title: "이 터미널의 실행을 종료할까요?", danger: true,
    description: "이 보드에서 연 새 세션을 종료합니다. 진행 중인 응답이 중단될 수 있고, 대화 기록은 남습니다.",
    context: "보드에서 새로 연 세션입니다. 보드로 돌아가거나 브라우저를 닫아도 실행은 유지됩니다.",
    ended: "새 세션의 실행이 종료되었습니다. 대화 기록은 보드의 세션 목록에서 확인하세요.",
    done: "새 세션의 실행을 종료했습니다. 대화 기록은 보드의 세션 목록에서 확인하세요.",
  };
  // A PC conversation resumed in this terminal runs in it: closing it ends that run.
  if (mode === "resume") return {
    action: "실행 종료", title: "이 터미널의 실행을 종료할까요?", danger: true,
    description: "이 보드에서 다시 연 대화의 실행을 종료합니다. 진행 중인 응답이 중단될 수 있고, 대화 기록은 남습니다.",
    context: "저장된 대화를 이 보드 터미널에서 다시 열었습니다. 보드로 돌아가거나 브라우저를 닫아도 실행은 유지됩니다.",
    ended: "다시 연 대화의 실행이 종료되었습니다. 대화 기록은 보드의 세션 목록에서 확인하세요.",
    done: "다시 연 대화의 실행을 종료했습니다. 대화 기록은 보드의 세션 목록에서 확인하세요.",
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
// A run on another host: kept in that host's tmux (holdName), it outlasts a dropped connection;
// otherwise it lives in the connection itself.
function terminalHeld(terminal) {
  return !!terminalInfo(terminal).holdName;
}
function terminalContextCopy(terminal, copy = terminalCloseCopy(terminal)) {
  const mode = terminal?.mode || terminal?.metadata?.mode;
  const host = capsHost(terminalInfo(terminal).host);
  if (terminalHeld(terminal))
    return `${copy.context} 실행 기기가 잠들거나 연결이 끊겨도 실행은 그 기기에서 계속되고, 다시 연결되면 이어서 보입니다.`;
  if (["new-session", "resume", "handoff", "transfer"].includes(mode) && host && !host.local)
    return `${copy.context} 단, 실행 기기가 잠들거나 연결이 2분 넘게 끊기면 실행이 끝납니다.`;
  return copy.context;
}
// What an ended terminal says: why, when the board knows (closedReason), and what remains.
function terminalEndedCopy(terminal) {
  const copy = terminalCloseCopy(terminal);
  // Unread for 10 minutes, or for a minute when the board needed room for another terminal.
  if (terminal?.closedReason === "released")
    return "한동안 보지 않아 이 보드의 보기 연결만 정리했습니다. 작업은 실행 기기에서 계속됩니다.";
  const host = capsHost(terminalInfo(terminal).host);
  // "connection_ended" is any end of the terminal's run: its own exit or a lost host. A held
  // run outlasts a lost host, so its end is its own.
  if (terminal?.closedReason === "connection_ended" && host && !host.local && !terminalHeld(terminal))
    return `${copy.ended} 실행 기기가 잠들었거나 2분 넘게 연결이 끊겼을 수도 있습니다.`;
  return copy.ended;
}
// The listed session an ended terminal showed: by the run it attached to, else the one it was opened from.
function sessionForTerminal(terminal) {
  if (!terminal) return null;
  const info = terminalInfo(terminal);
  const keys = [];
  if (info.nativeSource?.host) keys.push(chatKey(info.nativeSource));
  try {
    const source = JSON.parse(info.sourceKey || "null");
    if (source && typeof source === "object") keys.push(chatKey(source));
  } catch {
    /* no source recorded */
  }
  const sessions = allSessions();
  for (const key of keys) {
    const found = sessions.find((s) => chatKey(sourceOf(s)) === key);
    if (found) return found;
  }
  return null;
}
function showTerminalEnded() {
  terminalStatus(terminalEndedCopy(state.terminal));
  // A session closed by its user is not resumed from the board (the plan refuses it).
  const s = state.terminal?.closedReason === "user" ? null : sessionForTerminal(state.terminal);
  $("terminal-reopen").hidden = !s || sessionOneShot(s) || !!terminalForSession(s);
}
// Opening it again is reviewed like any new terminal (the conversation's plan sheet).
function reopenEndedTerminal() {
  const s = sessionForTerminal(state.terminal);
  if (!s || state.launchBusy || !state.authenticated) return;
  $("terminal-reopen").hidden = true;
  if (window.history.state?.[INSTANCE.historyKey] && window.history.state.view === "terminal") {
    // The way back is asynchronous: the board opens it once back (popstate).
    state.reopenOnReturn = s.key;
    window.history.back();
    return;
  }
  returnToBoard();
  openSessionTerminal(s);
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
  clearTerminalMods();
  state.terminalRun++;
  state.terminalAbort?.abort();
  state.terminalAbort = null;
  state.connected = false;
}
function saveDraft() {
  if (state.terminal) {
    state.drafts.set(draftKey(state.terminal), $("terminal-draft").value);
    persistTerminalDraft(state.terminal);
  }
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
  if (anyTerminalMod()) {
    // An input method composing (한글, say) gives no key to combine with: said, not silent.
    clearTerminalMods();
    $("draft-note").textContent = "한글 등 조합 입력에서는 보조키가 풀립니다. 영문 입력으로 바꿔 다시 눌러 주세요.";
  }
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
    dictationTyped(node, event);
    inputDraft(event);
    resizeDraft();
  });
  node.addEventListener("keydown", draftKeydown);
  node.addEventListener("beforeinput", draftBeforeInput);
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
    // A pinch decides beside-or-covering once, at its end (endPinch).
    if (persist) redock();
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
  const terminal = state.terminal, openRun = state.terminalOpenRun;
  const current = () => state.terminal === terminal && state.terminalOpenRun === openRun && state.terminalVisible;
  state.resizeTimer = setTimeout(async () => {
    if (!current() || !state.connected || !terminal || !state.term) return;
    const size = `${state.term.cols}x${state.term.rows}`;
    if (size === state.lastSize) return;
    try {
      await api(
        `/api/terminal/${encodeURIComponent(terminal.id)}/resize`,
        {
          method: "POST",
          body: { cols: state.term.cols, rows: state.term.rows },
        },
      );
      if (current()) state.lastSize = size;
    } catch (err) {
      // An ended terminal has nothing to fit: its own end notice stays.
      if (current() && !err.unauthorized && !err.staleAuth && state.terminalAlive !== false)
        terminalError("터미널 크기를 맞추지 못했습니다. 다시 연결해 주세요.");
    }
  }, 180);
}
function showTerminalScreen(visible) {
  if ($("board-status")) $("board-status").open = false;
  $("terminal-screen").hidden = !visible;
  applyDock();
}
// Docked, the terminal keeps 80 columns (64 at least, where a CLI's boxes still fit) and
// leaves the conversation 420px; the list stays beside them when 820px are left.
// chrome: the viewport's padding, xterm's scrollbar and the dock's border.
const DOCK = { cols: 80, minCols: 64, share: 0.45, minChat: 420, withList: 820, chrome: 34 };
function terminalCellWidth() {
  const size = state.fontSize || 14;
  try {
    state.cellCanvas ||= document.createElement("canvas");
    const context = state.cellCanvas.getContext?.("2d");
    if (context) {
      context.font = `${size}px ${TERMINAL_FONT}`;
      const width = context.measureText("W".repeat(32)).width / 32;
      if (width > 0) return width;
    }
  } catch {
    /* estimated below */
  }
  return size * 0.62;
}
// The width a terminal takes beside the conversation on this screen; 0 covers the board.
function dockWidth({ preferred = state.terminalDock } = {}) {
  const width = window.innerWidth || 0;
  if (!preferred || width <= 0 || phoneLayout() || shortLayout()) return 0;
  const cell = terminalCellWidth();
  const wanted = Math.max(Math.ceil(cell * DOCK.cols) + DOCK.chrome, Math.round(width * DOCK.share));
  const docked = Math.min(wanted, width - DOCK.minChat);
  return docked >= cell * DOCK.minCols + DOCK.chrome ? Math.floor(docked) : 0;
}
function boardCovered() {
  return state.terminalVisible && !state.docked;
}
// Beside a terminal whose conversation is not listed yet: no other conversation stays there.
function showDockWaiting() {
  if (state.selected) rememberConversationView();
  stopChatPolling();
  state.selected = null;
  state.selectedBotRequest = null;
  state.detailFor = null;
  state.detailRevision = null;
  state.detailRun++;
  const waiting = el("div", "welcome dock-waiting");
  waiting.append(el("span", "welcome-mark", "↗"), el("h2", null, "이 터미널의 대화는 아직 목록에 없습니다."),
    el("p", null, "새 세션이면 첫 대화가 기록되는 대로 여기에 보입니다."));
  // The list stepped aside for the terminal: a way to it (the terminal steps aside then).
  if (state.dockSolo) waiting.append(button("목록 보기", "secondary", backFromTerminal));
  $("detail").replaceChildren(waiting);
  render();
}
// The conversation a docked terminal belongs beside: for a handoff's or transfer's new run
// only that run's (its source is the one it left); for any other, sessionForTerminal's.
function dockedSessionFor(terminal) {
  if (!terminal) return null;
  const info = terminalInfo(terminal);
  if (!["handoff", "transfer"].includes(info.launchMode || info.mode)) return sessionForTerminal(terminal);
  if (!info.nativeSource?.host) return null;
  const key = chatKey(info.nativeSource);
  return allSessions().find((s) => chatKey(sourceOf(s)) === key) || null;
}
// Docked, beside the terminal stands its own conversation (dockBeside, its key) or, while it
// is not listed, a note saying so (dockBeside null): when it opens, when it docks (a toggle,
// a wider window, a turn to landscape) and once its conversation is listed or linked.
function syncBeside() {
  if (!state.docked || !state.terminal) return;
  const beside = dockedSessionFor(state.terminal);
  if (!beside) {
    state.dockBeside = null;
    if (state.selected || state.selectedBotRequest || !$("detail")?.querySelector?.(".dock-waiting")) showDockWaiting();
    return;
  }
  state.dockBeside = beside.key;
  if (beside.key === state.selected) return;
  // A launch opens its terminal while still busy; showing its conversation is part of it.
  const busy = state.launchBusy;
  state.launchBusy = false;
  try {
    select(beside.key);
  } finally {
    state.launchBusy = busy;
  }
}
// Docked, a key typed with nothing focused belongs to the side last touched.
function boardKeysBlocked() {
  return state.terminalVisible && !(state.docked && state.dockSide === "board");
}
function noteDockSide(side) {
  if (state.docked) state.dockSide = side;
}
// Beside the conversation or over the board: decided on open, resize and font change.
function applyDock() {
  const width = state.terminalVisible ? dockWidth() : 0;
  const docked = width > 0;
  const changed = docked !== state.docked;
  // Newly beside the conversation, the terminal has the keys (as when it opens).
  if (changed && docked) state.dockSide = "terminal";
  state.docked = docked;
  state.dockWidth = width;
  document.documentElement.style?.setProperty?.("--dock-w", `${width}px`);
  state.dockSolo = docked && (window.innerWidth || 0) - width < DOCK.withList;
  const classes = document.body?.classList;
  classes?.[docked ? "add" : "remove"]("terminal-docked");
  classes?.[state.dockSolo ? "add" : "remove"]("dock-solo");
  $("app").hidden = state.terminalVisible && !docked;
  // Beside the conversation (the page's main) the terminal is one region of it.
  $("terminal-screen").setAttribute("role", docked ? "region" : "main");
  renderDockToggle();
  renderTerminalReturn();
  if (changed) renderConnectivity();
  if (changed && docked) syncBeside();
  return changed;
}
// Docked, the board stays on screen with its focus (the control that opened the terminal):
// keys typed next belong to the terminal, so that focus is let go.
function releaseBoardFocus() {
  const active = document.activeElement;
  if (state.docked && active?.closest?.("#app")) active.blur?.();
}
// The terminal steps aside for the board, and its history entry gives way to the board's.
// The conversation beside it was already reading: no second read.
function undock() {
  const active = document.activeElement;
  if (active?.closest?.("#terminal-screen")) active.blur?.();
  returnToBoard({ selectLaunchedSource: false, animate: false, loadChat: false });
  if (window.history.state?.[INSTANCE.historyKey] && window.history.state.view === "terminal") navigateView("list", true);
}
// Beside a terminal the board reads on; covered by one it rests (as in openTerminal).
function syncBoardLive() {
  updateThemeColor();
  schedule();
  syncEvents();
  if (boardCovered()) {
    stopChatPolling();
    return;
  }
  const selected = selectedSession();
  if (selected) loadChatState(selected, true);
}
function redock() {
  if (state.terminalVisible && applyDock()) syncBoardLive();
}
function renderDockToggle() {
  const toggle = $("terminal-dock");
  if (toggle) {
    toggle.hidden = !state.terminalVisible || !(dockWidth({ preferred: true }) > 0);
    toggle.textContent = state.docked ? "크게" : "나란히";
    toggle.setAttribute("aria-label", state.docked ? "터미널을 화면 가득 보기" : "터미널을 대화 옆에 보기");
  }
  const back = $("terminal-back");
  back.textContent = state.docked ? "숨기기" : "보드";
  const label = state.docked ? "터미널 칸 숨기기" : back.dataset?.boardLabel;
  if (label) back.setAttribute("aria-label", label);
}
// "크게"/"나란히" is remembered for every terminal on this device.
function toggleDock() {
  state.terminalDock = !state.docked;
  prefs.set("terminalDock", state.terminalDock);
  // Pressed in the terminal's header: its keys stay with it either way.
  state.dockSide = "terminal";
  redock();
  $("terminal-dock").blur?.();
  fitTerminal();
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
  const docking = dockWidth() > 0;
  if (!docking) stopChatPolling();
  if (recordHistory) navigateView("terminal", docking && window.history.state?.[INSTANCE.historyKey] && window.history.state.view === "list");
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
  if (changing) resetTerminalScroll();
  renderTerminalMods();
  state.inputEpoch++;
  state.inputQueue = Promise.resolve();
  state.inputFailed = false;
  clearTerminalMenu();
  state.terminal = terminal;
  state.terminalAlive = terminal.alive !== false && !terminal.closed;
  state.terminalVisible = true;
  state.dockSide = "terminal";
  renderConnectivity();
  const shown = presenting && !docking ? navTransition("present", () => showTerminalScreen(true)) : (showTerminalScreen(true), null);
  releaseBoardFocus();
  updateThemeColor();
  if (boardCovered()) closeEvents();
  $("terminal-title").textContent = terminalTitle(terminal, "진행 중인 작업");
  $("terminal-route").textContent = targetLabel(terminalInfo(terminal));
  const nativeHost = terminalInfo(terminal).nativeSource?.host || terminalInfo(terminal).host;
  $("terminal-back").dataset.boardLabel = `${hostLabel(nativeHost)}의 작업을 보드에서 보기`;
  renderDockToggle();
  const copy = terminalCloseCopy(terminal);
  $("terminal-close").textContent = copy.action;
  $("terminal-close").setAttribute("aria-label", copy.action);
  $("terminal-close").className = `terminal-button terminal-close${copy.danger ? " danger-quiet" : ""}`;
  $("terminal-close").disabled = !state.terminalAlive;
  $("terminal-context").textContent = terminalContextCopy(terminal, copy);
  restoreTerminalDraft(terminal);
  $("terminal-draft").value = state.drafts.get(draftKey(terminal)) || "";
  state.composing = false;
  state.draftRevision++;
  $("draft-note").textContent = "";
  terminalError();
  renderTerminalReturn();
  $("terminal-reopen").hidden = true;
  state.dockBeside = null;
  syncBeside();
  if (!state.terminalAlive) {
    showTerminalEnded();
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
      // ⌥ with a key is Meta (ESC first) for the CLI, not a special character.
      macOptionIsMeta: true,
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
    state.term.attachCustomKeyEventHandler?.(xtermKeyEvent);
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
    showTerminalEnded();
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
      // A silently dropped connection (Wi-Fi to LTE) must not hold the long poll forever:
      // the server answers within 20 seconds, so 35 without one is a lost connection.
      const result = await api(
        `/api/terminal/${encodeURIComponent(id)}/events?after=${state.terminalAfter}&wait=20${state.terminalEpoch ? `&epoch=${encodeURIComponent(state.terminalEpoch)}` : ""}`,
        { signal: controller.signal, timeout: 35000 },
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
      updateTerminalMenu();
      if (!state.terminalAlive) {
        state.terminal.alive = false;
        $("terminal-close").disabled = true;
        showTerminalEnded();
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
// After an input a restarted board refused, keys go nowhere until typing pauses this long:
// the rest of a fast-typed line (and its Enter) must not arrive without its start.
const INPUT_REFUSED_PAUSE_MS = 1000;
function sendInput(text) {
  if (networkOffline() || !state.connected || state.inputFailed || !state.terminal)
    return Promise.reject(new Error("터미널에 연결되어 있지 않습니다."));
  if (state.inputRefused && Date.now() - state.inputRefusedAt < INPUT_REFUSED_PAUSE_MS) {
    state.inputRefusedAt = Date.now();
    return Promise.reject(new Error("보드가 다시 시작돼 입력을 보내지 않았습니다."));
  }
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
      if (state.inputRefused) {
        state.inputRefused = false;
        terminalError("");
      }
    } catch (err) {
      if (err.code === "csrf_expired" && !err.unauthorized && !err.staleAuth && epoch === state.inputEpoch) {
        // A restarted board refused it before anything ran, so it was surely not sent, and
        // the reader stays. Keys queued behind it and typed until a pause are dropped too
        // (never a partial line); the next one waits for the new token.
        state.inputEpoch++;
        state.inputRefused = true;
        state.inputRefusedAt = Date.now();
        terminalError("보드가 다시 시작돼 이 입력은 보내지 않았습니다. 화면을 확인한 뒤 다시 입력해 주세요.");
        try {
          await loadCapabilities(state.authEpoch, { refreshView: false });
        } catch {
          /* the next input is refused the same way and tries again */
        }
        throw err;
      }
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
function returnToBoard({ selectLaunchedSource = true, animate = true, loadChat = true } = {}) {
  saveDraft();
  stopTerminalReader();
  state.inputEpoch++;
  state.terminalOpenRun++;
  const wasDocked = state.docked;
  state.dockBeside = null;
  state.terminalVisible = false;
  renderConnectivity();
  if (animate && !wasDocked) navTransition("dismiss", () => showTerminalScreen(false));
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
  if (selected && loadChat) loadChatState(selected, true);
}
function renderTerminalReturn() {
  const exists = !!state.terminal && state.terminalAlive && state.terminal.alive !== false && !state.terminal.closed;
  $("terminal-return").hidden = !exists || state.terminalVisible;
  const info = terminalInfo(state.terminal);
  const host = capsHost(info.host)?.label || info.host;
  $("terminal-return-label").textContent = exists
    ? [terminalTitle(state.terminal), host].filter(Boolean).join(" · ")
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
  // A reply or a later swipe belongs to the terminal the swipe was made on; a request
  // still on its way for an earlier terminal never holds up the next one's.
  const target = state.terminal.id, generation = state.terminalScrollGeneration;
  const mine = () => generation === state.terminalScrollGeneration && state.terminal?.id === target;
  state.terminalScrollInFlight = true;
  try {
    const result = await api(`/api/terminal/${encodeURIComponent(target)}/scroll`,
      { method: "POST", body: { lines } });
    if (!mine()) return;
    if (result?.mode === "app") {
      state.terminalScrollMode = "app";
      state.terminalScrollLines = 0;
    } else {
      state.terminalScrollMode = "history";
      state.terminalScrollLines = Number.isInteger(result?.position) ? result.position : estimate;
    }
  } catch {
    if (!mine()) return;
    state.terminalScrollLines = 0;
    stopTerminalMomentum();
  } finally {
    if (generation === state.terminalScrollGeneration) state.terminalScrollInFlight = false;
    if (mine()) {
      renderTerminalScroll();
      // Swipes that arrived during the request are sent next, never dropped or doubled.
      if (Math.trunc(state.terminalScrollPending) && !state.terminalScrollTimer)
        state.terminalScrollTimer = setTimeout(flushTerminalScroll, 30);
    }
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
// Another terminal: swipes waiting for it, its reply and its position do not carry over.
function resetTerminalScroll() {
  state.terminalScrollGeneration++;
  state.terminalScrollInFlight = false;
  clearTimeout(state.terminalScrollTimer);
  state.terminalScrollTimer = null;
  stopTerminalMomentum();
  state.terminalScrollPending = 0;
  state.terminalScrollLines = 0;
  state.terminalScrollMode = null;
  renderTerminalScroll();
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
      redock();
      fitTerminal();
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
// --- Terminal keys: modifiers on the key bar and a hardware keyboard -------------
// What a key sends to the CLI, as a terminal emulator would (xterm's sequences).
// Ctrl, ⌥ and ⌘ on the key bar hold for the next key. ⌘ has line-editing meanings only:
// macOS conventions on a Mac host, the Windows console's own keys on a Windows host.
const TERMINAL_KEY_ALIASES = {
  Esc: "Escape", Up: "ArrowUp", Down: "ArrowDown", Left: "ArrowLeft", Right: "ArrowRight",
  UIKeyInputUpArrow: "ArrowUp", UIKeyInputDownArrow: "ArrowDown", UIKeyInputLeftArrow: "ArrowLeft",
  UIKeyInputRightArrow: "ArrowRight", UIKeyInputEscape: "Escape", UIKeyInputPageUp: "PageUp",
  UIKeyInputPageDown: "PageDown", UIKeyInputHome: "Home", UIKeyInputEnd: "End",
};
const TERMINAL_CURSOR_KEYS = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D", Home: "H", End: "F" };
const TERMINAL_TILDE_KEYS = { Insert: 2, Delete: 3, PageUp: 5, PageDown: 6, F5: 15, F6: 17, F7: 18, F8: 19,
  F9: 20, F10: 21, F11: 23, F12: 24 };
const TERMINAL_FUNCTION_KEYS = { F1: "P", F2: "Q", F3: "R", F4: "S" };
const TERMINAL_CTRL_DIGITS = { 2: "\x00", 3: "\x1b", 4: "\x1c", 5: "\x1d", 6: "\x1e", 7: "\x1f", 8: "\x7f" };
const TERMINAL_EDIT_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Tab",
  "Backspace", "Delete", "Escape"]);
const TERMINAL_ALWAYS_KEYS = /^(F([1-9]|1[0-2])|PageUp|PageDown|Insert)$/;
// The bar's own keys as names, for when a held modifier combines with one.
const TERMINAL_BAR_KEYS = {
  escape: ["Escape"], tab: ["Tab"], backtab: ["Tab", { shift: true }], interrupt: ["c", { ctrl: true }],
  "ctrl-j": ["j", { ctrl: true }], "ctrl-u": ["u", { ctrl: true }], "ctrl-x": ["x", { ctrl: true }],
  "ctrl-l": ["l", { ctrl: true }], up: ["ArrowUp"], left: ["ArrowLeft"], down: ["ArrowDown"],
  right: ["ArrowRight"], enter: ["Enter"],
};
const TERMINAL_BAR_BYTES = {
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
function terminalKeyBytes(key, mods = {}, { windows = false } = {}) {
  key = TERMINAL_KEY_ALIASES[key] || key;
  const ctrl = !!mods.ctrl, alt = !!mods.alt, shift = !!mods.shift, meta = !!mods.meta;
  if (meta) {
    // ⌘←/→ to the line's start or end, ⌘⌫ deletes back to its start.
    const edits = windows ? { ArrowLeft: "\x1b[H", ArrowRight: "\x1b[F", Backspace: "\x1b[1;5H" }
      : { ArrowLeft: "\x01", ArrowRight: "\x05", Backspace: "\x15" };
    return ctrl || alt ? null : edits[key] || null;
  }
  if (alt && !ctrl && !shift && ["ArrowLeft", "ArrowRight", "Backspace"].includes(key)) {
    // ⌥←/→ by word and ⌥⌫ deletes a word, as Terminal and the Windows console do it.
    return (windows ? { ArrowLeft: "\x1b[1;5D", ArrowRight: "\x1b[1;5C", Backspace: "\x08" }
      : { ArrowLeft: "\x1bb", ArrowRight: "\x1bf", Backspace: "\x1b\x7f" })[key];
  }
  const level = 1 + (shift ? 1 : 0) + (alt ? 2 : 0) + (ctrl ? 4 : 0);
  if (TERMINAL_CURSOR_KEYS[key]) return level > 1 ? `\x1b[1;${level}${TERMINAL_CURSOR_KEYS[key]}` : `\x1b[${TERMINAL_CURSOR_KEYS[key]}`;
  if (TERMINAL_TILDE_KEYS[key]) return level > 1 ? `\x1b[${TERMINAL_TILDE_KEYS[key]};${level}~` : `\x1b[${TERMINAL_TILDE_KEYS[key]}~`;
  if (TERMINAL_FUNCTION_KEYS[key]) return level > 1 ? `\x1b[1;${level}${TERMINAL_FUNCTION_KEYS[key]}` : `\x1bO${TERMINAL_FUNCTION_KEYS[key]}`;
  const prefix = alt ? "\x1b" : "";
  if (key === "Tab") return shift ? "\x1b[Z" : prefix + "\t";
  if (key === "Enter") return prefix + "\r";
  if (key === "Escape") return "\x1b";
  if (key === "Backspace") return prefix + (ctrl ? "\x08" : "\x7f");
  if (typeof key !== "string" || [...key].length !== 1) return null;
  if (!ctrl) return prefix + key;
  if (key === " ") return prefix + "\x00";
  if (TERMINAL_CTRL_DIGITS[key]) return prefix + TERMINAL_CTRL_DIGITS[key];
  if (key === "?") return prefix + "\x7f";
  const code = key.toUpperCase().charCodeAt(0);
  return code >= 64 && code <= 95 ? prefix + String.fromCharCode(code & 0x1f) : null;
}
// A key pressed with a modifier, by its place on the keyboard: a Korean layout prints ㅊ
// and ⌥ prints å, but Ctrl+C is meant.
function hardwareKeyName(event) {
  const key = TERMINAL_KEY_ALIASES[event.key] || String(event.key || "");
  if (!(event.ctrlKey || event.altKey || event.metaKey || anyTerminalMod()) || typeof event.code !== "string") return key;
  const letter = /^Key([A-Z])$/.exec(event.code);
  if (letter) return event.shiftKey ? letter[1] : letter[1].toLowerCase();
  const digit = /^Digit([0-9])$/.exec(event.code);
  if (digit) return digit[1];
  return { Space: " ", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]", Backslash: "\\",
    Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/", Backquote: "`" }[event.code] || key;
}
function appleBrowser() {
  return /Mac|iPhone|iPad|iPod/.test(String(globalThis.navigator?.platform || globalThis.navigator?.userAgent || ""));
}
const TERMINAL_MODIFIER_NAMES = ["Process", "Unidentified", "Dead", "Shift", "Control", "Alt", "Meta", "CapsLock",
  "HangulMode", "HanjaMode", "KanaMode", "Lang1", "Lang2"];
// The bytes a hardware key sends while a terminal shows, or null to leave it to the page
// (or to the input bar: a typed draft keeps the keyboard's own editing, see below).
function terminalKeyFromEvent(event, { draftEmpty = true, focusless = false } = {}) {
  if (!event || event.isComposing || event.keyCode === 229) return null;
  const sticky = state.terminalMods || {};
  const mods = { ctrl: !!(event.ctrlKey || sticky.ctrl), alt: !!(event.altKey || sticky.alt),
    shift: !!event.shiftKey, meta: !!(event.metaKey || sticky.meta) };
  const key = hardwareKeyName(event);
  if (TERMINAL_MODIFIER_NAMES.includes(key)) return null;
  const options = { windows: terminalOnWindows() };
  // A typed draft keeps the keyboard's own editing: ⌘ and ⌥ (Ctrl on a PC) arrows and ⌫ move
  // and delete in it, and a Mac's ⌥ prints its characters (⌥₩ is \ on a Korean Mac). A PC's
  // Alt types nothing (and Alt+← is the browser's Back), so it stays Meta for the CLI, as do
  // modifiers held on the bar.
  const typed = !focusless && !draftEmpty;
  const apple = appleBrowser();
  // ⌘C, ⌘V and the rest stay the browser's; ⌘ only edits the line.
  if (mods.meta) {
    if (!["ArrowLeft", "ArrowRight", "Backspace"].includes(key) || (typed && !sticky.meta)) return null;
    return terminalKeyBytes(key, mods, options);
  }
  if (TERMINAL_ALWAYS_KEYS.test(key)) return terminalKeyBytes(key, mods, options);
  if ([...key].length === 1) {
    const raw = String(event.key || "");
    if (!apple && event.ctrlKey && !sticky.ctrl) {
      // AltGr (Ctrl+Alt on a PC) prints a character: into the input bar, or as it is.
      if (event.altKey && [...raw].length === 1 && raw.toLowerCase() !== key.toLowerCase())
        return focusless ? raw : null;
      // A PC browser pastes with Ctrl+V and keeps Ctrl+C/X/A/Z for a typed draft;
      // on Apple keyboards ⌘ does those (and Ctrl+V reaches the CLI, which pastes images).
      if (!focusless && (key.toLowerCase() === "v" || (typed && "cxaz".includes(key.toLowerCase())))) return null;
    }
    if (!focusless && apple && event.altKey && !sticky.alt && !mods.ctrl) return null;
    if (mods.ctrl || mods.alt || focusless) return terminalKeyBytes(key, mods, options);
    return null;
  }
  if (key === "Enter") return focusless ? terminalKeyBytes(key, mods, options) : null;
  if (TERMINAL_EDIT_KEYS.has(key) && (!typed || sticky.ctrl || sticky.alt || (event.altKey && !apple)))
    return terminalKeyBytes(key, mods, options);
  return null;
}
// A real key (not a modifier, nor one an input method is still composing): a held bar
// modifier it does not combine with is released, so it never lands on a later key.
function releasesTerminalMods(event) {
  return !!event && !event.isComposing && event.keyCode !== 229
    && !TERMINAL_MODIFIER_NAMES.includes(TERMINAL_KEY_ALIASES[event.key] || String(event.key || ""));
}
function terminalOnWindows() {
  return capsHost(terminalInfo(state.terminal).host)?.platform === "windows";
}
function anyTerminalMod() {
  const mods = state.terminalMods || {};
  return !!(mods.ctrl || mods.alt || mods.meta);
}
function renderTerminalMods() {
  const windows = terminalOnWindows();
  for (const node of $("terminal-keys")?.querySelectorAll?.("button[data-mod]") || []) {
    const name = node.dataset.mod;
    node.setAttribute("aria-pressed", String(!!state.terminalMods[name]));
    if (name === "alt") node.textContent = windows ? "Alt" : "⌥";
    // A Windows host has no ⌘; Ctrl and Alt do its editing there.
    if (name === "meta") node.hidden = windows;
  }
}
function toggleTerminalMod(name) {
  if (!(name in state.terminalMods)) return;
  state.terminalMods = { ...state.terminalMods, [name]: !state.terminalMods[name] };
  renderTerminalMods();
}
function clearTerminalMods() {
  if (!anyTerminalMod()) return;
  state.terminalMods = { ctrl: false, alt: false, meta: false };
  renderTerminalMods();
}
function sendTerminalBytes(bytes) {
  if (!bytes || state.composing || state.draftPaste) return;
  state.terminalScrollLines = 0;
  renderTerminalScroll();
  sendInput(bytes).catch(() => {});
}
// A key on the bar: its own bytes, or combined with the held modifiers (then released).
function pressTerminalKey(name) {
  if (state.composing || state.draftPaste) return;
  let bytes = TERMINAL_BAR_BYTES[name];
  if (anyTerminalMod()) {
    const [key, base = {}] = TERMINAL_BAR_KEYS[name] || [];
    const mods = state.terminalMods;
    const combined = key && terminalKeyBytes(key, { ...base, ctrl: base.ctrl || mods.ctrl, alt: mods.alt, meta: mods.meta },
      { windows: terminalOnWindows() });
    if (combined) bytes = combined;
    clearTerminalMods();
  }
  sendTerminalBytes(bytes);
}
// A held modifier and a character typed into the input bar: sent as that combination.
function draftBeforeInput(event) {
  if (!anyTerminalMod() || event.inputType !== "insertText" || typeof event.data !== "string" || [...event.data].length !== 1) return;
  const bytes = terminalKeyBytes(event.data, { ...state.terminalMods }, { windows: terminalOnWindows() });
  if (!bytes) {
    // Typed as it is; the held modifier does not wait for a later key.
    clearTerminalMods();
    return;
  }
  event.preventDefault();
  clearTerminalMods();
  sendTerminalBytes(bytes);
}
// With nothing focused (the terminal just opened), a hardware keyboard types into the CLI.
// A focused control keeps its keys, and Tab moves the focus as on any page.
function terminalDocumentKey(event) {
  if (!state.terminalVisible || (state.docked && state.dockSide !== "terminal") || event.defaultPrevented ||
      editingText() || dictationListening($("terminal-draft"))) return false;
  const active = document.activeElement;
  if ((active && active !== document.body && active !== document.documentElement) || event.key === "Tab") {
    // Not for the CLI: a held bar modifier must not wait for a later key either.
    if (releasesTerminalMods(event)) clearTerminalMods();
    return false;
  }
  if (event.target?.closest?.(".xterm, dialog[open], details[open]")) return false;
  if (document.querySelector?.("dialog[open]")) return false;
  const bytes = terminalKeyFromEvent(event, { focusless: true });
  if (!bytes) {
    if (releasesTerminalMods(event)) clearTerminalMods();
    return false;
  }
  event.preventDefault();
  clearTerminalMods();
  sendTerminalBytes(bytes);
  return true;
}
// xterm has the focus: it sends most keys itself; ⌘ edits, ⌥ word moves and held bar
// modifiers are ours (the keypress that follows a handled keydown is swallowed too).
function xtermKeyEvent(event) {
  if (event.type !== "keydown") return !state.terminalKeyHandled;
  state.terminalKeyHandled = false;
  const ours = anyTerminalMod() || event.metaKey || (event.altKey && ["ArrowLeft", "ArrowRight", "Backspace"].includes(event.key));
  if (!ours) return true;
  const bytes = terminalKeyFromEvent(event, { focusless: true });
  if (!bytes) {
    if (releasesTerminalMods(event)) clearTerminalMods();
    return true;
  }
  event.preventDefault?.();
  state.terminalKeyHandled = true;
  clearTerminalMods();
  sendTerminalBytes(bytes);
  return false;
}
function draftKeydown(event) {
  // A hardware keyboard's terminal keys go to the CLI (see terminalKeyFromEvent). A key held
  // down from a typed draft (⌫, say) stops at its start instead of going on into the CLI line.
  const empty = !event.target?.value;
  if (!event.repeat) state.draftPressEmpty = empty;
  const draftEmpty = empty && (!event.repeat || state.draftPressEmpty !== false);
  const bytes = state.terminalVisible ? terminalKeyFromEvent(event, { draftEmpty }) : null;
  if (bytes) {
    event.preventDefault();
    clearTerminalMods();
    sendTerminalBytes(bytes);
    return;
  }
  if (state.terminalVisible && releasesTerminalMods(event)) clearTerminalMods();
  // Enter sends like a chat box; Shift+Enter keeps a newline in the draft.
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  pasteDraft(true);
}
async function pasteDraft(submit = false) {
  if (state.draftPaste) return;
  stopDictation({ abort: true, field: $("terminal-draft") });
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
    pending.dispatched = true;
    saveDraft();
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
      if (state.terminal?.id === pending.id) saveDraft();
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
      // Linked to its session now (a new session's): its title, and beside it its conversation.
      renameTerminals();
      if (state.docked && !state.dockBeside) syncBeside();
      if (current.closed || current.alive === false) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        state.terminalAlive = false;
        showTerminalEnded();
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
  const restoreFocus = focused?.terminalId ? { id: focused.terminalId, action: focused.action }
    : focused?.action === "connections" ? { action: "connections" } : null;
  list.replaceChildren();
  const alive = state.terminals.filter((t) => t.alive !== false && !t.closed);
  const direct = alive.filter((terminal) => !connectionTerminal(terminal));
  const connections = alive.filter(connectionTerminal);
  const repeats = duplicateTerminals(direct);
  // A chip in the list's top row; the rows themselves live in a sheet.
  const chip = $("running-terminals");
  chip.hidden = !alive.length && !state.terminalsError;
  setClass(chip, "has-error", !!state.terminalsError);
  chip.setAttribute?.("aria-label", `열린 터미널 ${direct.length}개${connections.length ? `, 세션 연결 ${connections.length}개` : ""}${state.terminalsError ? ", 목록 확인 필요" : ""}`);
  if (chip.hidden && $("terminals-dialog")?.open) closeSheet("terminals-dialog");
  $("running-error").textContent = state.terminalsError;
  $("running-error").hidden = !state.terminalsError;
  $("running-refresh").hidden = !state.terminalsError;
  $("running-count").textContent = direct.length || connections.length;
  const row = (terminal) => {
    const node = button("", "running-terminal", () => {
      closeSheet("terminals-dialog");
      openTerminal(terminal);
    });
    node.dataset.terminalId = terminal.id;
    node.dataset.action = "open";
    const labels = el("span", "running-terminal-copy");
    const repeated = repeats.get(terminal.id);
    labels.append(
      el("strong", "running-terminal-title", terminalTitle(terminal)),
      el("span", "running-terminal-meta", targetLabel(terminalInfo(terminal))),
    );
    if (repeated) labels.append(el("span", "running-terminal-duplicate",
      `같은 화면이 이미 열려 있어요(${clock(terminalInfo(repeated).createdAt)} 시작). 이 터미널은 닫아도 세션은 계속 돌아가요.`));
    const arrow = el("span", null, "↗");
    arrow.setAttribute("aria-hidden", "true");
    node.append(
      el("span", "dot"),
      labels,
      arrow,
    );
    const item = el("div", `running-terminal-row${repeated ? " duplicate" : ""}`);
    const copy = terminalCloseCopy(terminal);
    const close = button(copy.action, `running-terminal-close${copy.danger ? " danger-quiet" : ""}`, () => closeTerminal(terminal.id));
    close.dataset.terminalId = terminal.id;
    close.dataset.action = "close";
    close.setAttribute("aria-label", `${terminalTitle(terminal, "터미널")} ${copy.action}`);
    item.append(node, close);
    list.append(item);
    if (restoreFocus?.id === terminal.id)
      (restoreFocus.action === "close" ? close : node).focus();
  };
  direct.forEach(row);
  if (connections.length) {
    // Connections the board opened to send or answer: folded, since they close by themselves.
    const toggle = button(`Claude 세션 연결 ${connections.length}개 · 10분 쓰지 않으면 보드가 닫아요`, "running-connections",
      () => { state.showConnections = !state.showConnections; renderTerminals(); });
    toggle.setAttribute("aria-expanded", String(!!state.showConnections));
    toggle.dataset.action = "connections";
    list.append(toggle);
    if (restoreFocus?.action === "connections") toggle.focus();
    if (state.showConnections) connections.forEach(row);
  }
  // Cards and the open conversation offer "연결 닫기" for these terminals, and
  // show "터미널 열림" while one is live.
  renderTerminalMarks();
  const selected = selectedSession();
  if (selected) updateChatComposer(chatEntry(selected));
}

// "앱 서버 켜기": start one Codex account's daemon on its host, then read the list and
// this conversation again. Nothing is sent to the agent.
async function startAppServer(s, trigger) {
  if (state.appServerBusy) return;
  state.appServerBusy = true;
  const authEpoch = state.authEpoch;
  if (trigger) {
    trigger.disabled = true;
    trigger.textContent = "앱 서버를 켜는 중…";
  }
  try {
    await api("/api/app-server", { method: "POST", body: { host: s.host, home: s.home || ".codex" }, authEpoch });
    assertAuthEpoch(authEpoch);
    toast("앱 서버를 켰습니다. 대화를 다시 읽습니다.");
    await poll(true);
    const current = selectedSession();
    if (current?.key === s.key) await loadDetail(current, true);
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth && authEpoch === state.authEpoch)
      toast(err.message || "앱 서버를 켜지 못했습니다.");
  } finally {
    state.appServerBusy = false;
    if (trigger && trigger.isConnected !== false) {
      trigger.disabled = false;
      trigger.textContent = "앱 서버 켜기";
    }
  }
}

// "새 세션": a host's own launcher (settings `launcher`), opened in a board
// terminal. The launcher, not the board, chooses the project, agent and account.
// 새 세션 presets (launch.LAUNCH_PRESETS on the board): whatever the launcher then starts runs
// with this model and effort; 기본 keeps the device's own settings. A PC's launcher choices
// are its own commands, so a preset never reaches them. The page's choice (state) is what is
// sent; this browser remembers it (prefs).
const NEW_SESSION_PRESETS = [
  { id: "", label: "기본", note: "기기에 설정된 모델과 수준 그대로 시작합니다." },
  { id: "fast", label: "빠르게", note: "Claude는 Sonnet·보통으로 시작합니다(실행 명령이 지원할 때). Codex는 기기 설정 그대로입니다." },
  { id: "max", label: "최대", note: "Claude는 Opus·최대로 시작합니다(실행 명령이 지원할 때). Codex는 기기 설정 그대로입니다." },
];
function newSessionPreset() {
  const id = state.newSessionPreset ?? prefs.get("newSessionPreset", "");
  return NEW_SESSION_PRESETS.some((preset) => preset.id === id) ? id : "";
}
// The radios are made once and then only updated, so a keyboard choice keeps its focus.
function renderNewSessionPresets() {
  const box = $("new-session-presets");
  if (!box) return;
  // Only a device that is not a PC takes a preset: with none, there is nothing to choose.
  const usable = newSessionHosts().some((host) => host.platform !== "windows" && host.newSession.available);
  for (const id of ["new-session-preset-label", "new-session-presets", "new-session-preset-note"]) $(id).hidden = !usable;
  if (!box.presetInputs) {
    box.presetInputs = NEW_SESSION_PRESETS.map((preset) => {
      const option = el("label", "theme-option");
      const input = el("input");
      input.type = "radio"; input.name = "new-session-preset"; input.value = preset.id;
      input.addEventListener("change", () => {
        if (!input.checked) return;
        state.newSessionPreset = preset.id;
        prefs.set("newSessionPreset", preset.id);
        renderNewSessionPresets(); renderNewSessionHosts();
      });
      option.append(input, el("span", null, preset.label));
      box.append(option);
      return input;
    });
  }
  const current = newSessionPreset();
  for (const input of box.presetInputs) {
    input.checked = input.value === current;
    input.disabled = !!state.newSessionBusy;
  }
  $("new-session-preset-note").textContent = NEW_SESSION_PRESETS.find((preset) => preset.id === current).note
    + (current && newSessionHosts().some((host) => host.platform === "windows") ? " PC는 기기 설정 그대로 엽니다." : "");
}
function newSessionHosts() {
  return (state.capabilities?.hosts || []).filter((host) => host.newSession);
}
function renderNewSession() {
  const chip = $("new-session");
  if (!chip) return;
  chip.hidden = !newSessionHosts().some((host) => host.newSession.available);
  if (chip.hidden && $("new-session-dialog")?.open) closeNewSession();
}
function newSessionHostMeta(host) {
  const status = host.newSession;
  if (host.online === false) return "기기에 연결할 수 없습니다.";
  if (!status.available) return status.reason || "이 기기에서는 열 수 없습니다.";
  const preset = host.platform === "windows" ? null : NEW_SESSION_PRESETS.find((item) => item.id && item.id === newSessionPreset());
  return `${status.label || "새 세션"} 실행` + (preset ? ` · ${preset.label}` : "");
}
function renderNewSessionHosts() {
  const list = $("new-session-hosts");
  list.replaceChildren();
  for (const host of newSessionHosts()) {
    const ready = host.newSession.available && host.online !== false;
    const node = button("", "new-session-host", () => startNewSession(host.name));
    node.dataset.host = host.name;
    node.disabled = !ready || !!state.newSessionBusy;
    const copy = el("span", "new-session-host-copy");
    copy.append(
      el("strong", "new-session-host-title", host.label || host.name),
      el("span", "new-session-host-meta",
        state.newSessionBusy === host.name ? "터미널을 여는 중…" : newSessionHostMeta(host)),
    );
    const arrow = el("span", null, "↗");
    arrow.setAttribute("aria-hidden", "true");
    node.append(copy, arrow);
    list.append(node);
    // Already open here: going back to one is usually what was meant, and avoids a repeat.
    for (const terminal of state.terminals.filter((t) => t.alive !== false && !t.closed && !connectionTerminal(t)
        && terminalInfo(t).mode === "new-session" && terminalInfo(t).host === host.name)) {
      const open = button("", "new-session-open", () => {
        closeSheet("new-session-dialog");
        openTerminal(terminal);
      });
      open.dataset.terminalId = terminal.id;
      open.append(el("span", "new-session-open-label", "열린 화면으로"), el("strong", null, terminalTitle(terminal)));
      list.append(open);
    }
  }
}
function openNewSession() {
  // One request id per host and preset while this sheet is open: a retry after a lost
  // reply returns the same terminal instead of opening a second one.
  state.newSessionRequests = new Map();
  state.newSessionPreset = prefs.get("newSessionPreset", "");
  $("new-session-error").textContent = "";
  $("new-session-error").hidden = true;
  renderNewSessionPresets();
  renderNewSessionHosts();
  sheet("new-session-dialog").open();
}
function closeNewSession() {
  closeSheet("new-session-dialog", "new-session");
}
async function startNewSession(hostName) {
  if (state.newSessionBusy) return;
  const authEpoch = state.authEpoch;
  const host = newSessionHosts().find((item) => item.name === hostName);
  const preset = host?.platform === "windows" ? "" : newSessionPreset();
  const key = hostName + "\n" + preset;
  state.newSessionRequests ||= new Map();
  if (!state.newSessionRequests.has(key)) state.newSessionRequests.set(key, requestId());
  state.newSessionBusy = hostName;
  $("new-session-error").hidden = true;
  renderNewSessionPresets();
  renderNewSessionHosts();
  try {
    const result = await api("/api/new-session", {
      method: "POST",
      body: { host: hostName, requestId: state.newSessionRequests.get(key), ...(preset ? { preset } : {}) }, authEpoch,
    });
    assertAuthEpoch(authEpoch);
    if (!result.terminal?.id) throw new Error("터미널 연결 정보를 받지 못했습니다.");
    state.newSessionBusy = null;
    closeSheet("new-session-dialog");
    rememberTerminal(result.terminal);
    await openTerminal(result.terminal);
    refreshTerminals(authEpoch);
  } catch (err) {
    if (authEpoch === state.authEpoch && !err.unauthorized && !err.staleAuth) {
      $("new-session-error").textContent = err.message || "새 세션을 열지 못했습니다.";
      $("new-session-error").hidden = false;
    }
  } finally {
    state.newSessionBusy = null;
    if ($("new-session-dialog").open) { renderNewSessionPresets(); renderNewSessionHosts(); }
  }
}

// A numbered menu a launcher prints and waits on: rows such as "  1. label"
// (and "  q. 취소"), then a short prompt such as "선택:" on the cursor's line.
// A refused answer and its hint may sit between the rows and the new prompt.
const MENU_ROW = /^\s{0,8}([0-9]{1,2}|[A-Za-z])\.\s+(\S.*)$/;
function terminalMenu(lines, cursorLine = lines.length - 1) {
  const prompt = /^(\S[^:：]{0,30})[:：]$/.exec(String(lines[cursorLine] || "").trim());
  if (!prompt) return null;
  let index = cursorLine - 1;
  for (let skipped = 0; index >= 0 && skipped < 6 && !MENU_ROW.test(lines[index] || ""); skipped++) index--;
  const options = [];
  for (; index >= 0 && MENU_ROW.test(lines[index] || ""); index--) {
    const [, key, label] = MENU_ROW.exec(lines[index]);
    options.unshift({ key, label: label.trim() });
  }
  if (!options.some((option) => /^\d+$/.test(option.key)) ||
      new Set(options.map((option) => option.key.toLowerCase())).size !== options.length)
    return null;
  const title = String(lines[index] || "").trim();
  return { title: title.length <= 80 ? title : "", prompt: prompt[1], options };
}
// Only "새 세션" terminals: the agent CLIs that follow in the same terminal
// draw their own lists, which are never turned into buttons.
function updateTerminalMenu() {
  const box = $("terminal-menu");
  if (!box) return;
  let menu = null;
  const buffer = state.term?.buffer?.active;
  if (buffer && terminalInfo(state.terminal).mode === "new-session" && state.terminalAlive !== false) {
    const cursor = buffer.baseY + buffer.cursorY;
    const lines = [];
    for (let y = Math.max(0, cursor - 40); y <= cursor; y++)
      lines.push(buffer.getLine(y)?.translateToString(true) || "");
    menu = terminalMenu(lines);
  }
  const signature = menu ? JSON.stringify(menu) : "";
  if (signature === state.terminalMenuSignature) return;
  state.terminalMenuSignature = signature;
  box.hidden = !menu;
  $("terminal-menu-options").replaceChildren();
  if (!menu) return;
  $("terminal-menu-title").textContent = menu.title || menu.prompt;
  box.setAttribute("aria-label", menu.title || "선택 메뉴");
  for (const option of menu.options) {
    const cancel = /^q$/i.test(option.key);
    $("terminal-menu-options").append(button(cancel ? option.label : `${option.key}. ${option.label}`,
      `terminal-menu-option${cancel ? " cancel" : ""}`, () => chooseTerminalMenu(option.key)));
  }
}
function clearTerminalMenu() {
  state.terminalMenuSignature = "";
  if ($("terminal-menu")) $("terminal-menu").hidden = true;
  $("terminal-menu-options")?.replaceChildren();
}
function chooseTerminalMenu(key) {
  // Hidden at once; the launcher's echo and next menu (if any) follow as output.
  clearTerminalMenu();
  sendInput(`${key}\r`).catch(() => {});
}

function closeTerminal(id, { thenRemove = null, trigger = null } = {}) {
  if (state.terminalCloseBusy) return;
  if ($("close-terminal-dialog").open) {
    $("close-terminal-cancel").focus();
    return;
  }
  const terminal = state.terminals.find((item) => item.id === id) ||
    (state.terminal?.id === id ? state.terminal : null);
  if (!terminal) return;
  const copy = terminalCloseCopy(terminal);
  state.closingTerminal = { ...terminal, id, title: terminalTitle(terminal), copy, trigger: trigger || document.activeElement, thenRemove };
  stopDictation({ abort: true });
  if (editingText()) document.activeElement.blur?.();
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
    const wasDocked = state.docked;
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
      const history = window.history;
      const closed = state.closedHistory ||= new Set();
      for (const key of [history.state?.navigationId, !wasDocked && history.state?.parentDetailId]) if (key) closed.add(key);
      const depth = history.state?.[INSTANCE.historyKey] && history.state.view === "terminal" ? history.state.listDepth : 0;
      if (!wasDocked && Number.isInteger(depth) && depth > 0 && depth <= 2 && typeof history.go === "function") {
        state.closedTerminalNavigation = true;
        history.go(-depth);
      } else navigateView("list", true);
    }
    // A covering terminal returns to the list; beside the conversation, that stays.
    if (wasVisible && !wasDocked) {
      stopChatPolling();
      setDetailShown(false, { focus: true });
    }
    schedule();
    syncEvents();
  }
  state.closingTerminal = null;
  $("close-terminal-dialog").close();
  toast(closing.copy.done);
  if (closing.thenRemove) removeSession(closing.thenRemove, true, { quiet: true }).then((done) => {
    if (done) toast(`${closing.copy.done} 목록에서도 지웠습니다.`);
  });
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
    // The run its host keeps ends once that host is back (the board's sweep).
    if (result.remoteCleanupPending === true) toast("실행 기기에 연결되면 그 기기의 실행도 종료합니다.");
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
      boardCovered() || Date.now() < state.eventsRetryAt) {
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
  if (!state.authenticated || networkOffline() || boardCovered() || document.hidden) return;
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
  checkWebVersion();
  renderNewSession();
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
  // A new process counts its versions from 0: remove marks waiting for a version are dropped.
  state.removedLocal?.clear?.();
}
// capabilities: false (a read announced by a snapshot event) reads the
// snapshot only; the next regular read catches the capabilities up.
// Resolves true when the list was read, false when it was not (or not tried);
// a read already in flight is shared (state.pollRun), not skipped as if done.
function poll(refresh = false, options = {}) {
  if (state.pollBusy) return state.pollRun || Promise.resolve(false);
  const run = pollOnce(refresh, options);
  if (state.pollBusy) {
    state.pollRun = run;
    run.finally(() => { if (state.pollRun === run) state.pollRun = null; });
  }
  return run;
}
async function pollOnce(refresh = false, { capabilities = true } = {}) {
  if (networkOffline() || state.loginBusy || state.pollBusy || document.hidden || boardCovered()) return false;
  const authEpoch = state.authEpoch;
  state.pollBusy = true;
  if (refresh) state.refreshDeadline = Date.now() + 60000;
  clearTimeout(state.pollTimer);
  try {
    const snapshot = await api(`/api/snapshot${refresh ? "?refresh=1" : ""}`, { authEpoch });
    assertAuthEpoch(authEpoch);
    state.snapshot = snapshot;
    if (snapshot.quickReplyPreferences) adoptQuickReplyPreferences(snapshot.quickReplyPreferences);
    serverBooted(snapshot?.bootId);
    if (Number.isInteger(snapshot?.version)) state.snapshotVersion = snapshot.version;
    const firstAuthentication = !state.authenticated;
    state.authenticated = true;
    // One non-blocking metadata read separates known bot sessions immediately.
    // An ordinary poll never retries this initial read, even after failure.
    if (firstAuthentication) loadBotRequests();
    $("login").hidden = true;
    $("app").hidden = false;
    notice();
    render();
    renameTerminals();
    openRequestedSession();
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
    if (refresh) window.SessionholicGoals?.refresh?.();
    if (Date.now() - (state.usageAt || 0) > 55000) loadUsage();
    if (Date.now() - (state.hardwareAt || 0) > HARDWARE_CLOSED_MS - 5000) loadHardware();
    // Safety net for missed (or unavailable) terminal announcements: a regular
    // read also reads a terminal list older than a minute. The first list is
    // read by login and startup themselves; when that read failed, this tries again.
    if (capabilities && (state.terminalsReadAt ? Date.now() - state.terminalsReadAt >= POLL_MS : !!state.terminalsError))
      refreshTerminals(authEpoch);
    schedule();
    syncEvents();
    return true;
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
    return false;
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
// Resolves true when the list was read again, false when it was not, null when it
// was not tried (a launch is being prepared). A read already in flight is waited for
// (each one is time-limited) and then followed by this one, never reported as a
// refresh that did not happen; a refresh in flight is shared by a second press or pull.
function manualRefresh() {
  if (state.sessionCategory === "bot" && !$("session-layout").hidden) return loadBotRequests({ refresh: true });
  if (state.manualRun) return state.manualRun;
  if (state.launchBusy) return Promise.resolve(null);
  const run = manualRefreshOnce();
  state.manualRun = run;
  run.finally(() => { if (state.manualRun === run) state.manualRun = null; });
  return run;
}
async function manualRefreshOnce() {
  const authEpoch = state.authEpoch;
  $("refresh").disabled = true;
  try {
    if (state.pollRun) await state.pollRun;
    assertAuthEpoch(authEpoch);
    if (!state.csrf) await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    state.refreshDeadline = Date.now() + 60000;
    await api("/api/refresh", { method: "POST", body: {}, authEpoch });
    assertAuthEpoch(authEpoch);
    state.detailUpdatedAt = null;
    await loadCapabilities(authEpoch);
    assertAuthEpoch(authEpoch);
    if (state.pollRun) await state.pollRun;
    const read = await poll(false);
    if (authEpoch === state.authEpoch) refreshTerminals(authEpoch);
    if (!read && state.pollFailures && authEpoch === state.authEpoch) notice("목록을 새로 읽지 못했습니다. 연결을 확인한 뒤 다시 시도해 주세요.");
    return read;
  } catch (err) {
    if (!err.unauthorized && !err.staleAuth) notice(err.message);
    return false;
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
    const read = await manualRefresh();
    const selected = selectedSession();
    if (selected && chatVisible()) await loadChatState(selected, true);
    if (networkOffline()) label = "오프라인입니다";
    else if (read === false) label = "새로고침하지 못했습니다";
    else if (read === null) label = "실행을 준비하는 중이라 잠시 뒤 다시 당겨 주세요";
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
    if (event.touches.length !== 1 || boardCovered() || state.pullRefreshing) return;
    if (event.target?.closest?.("textarea, input, select, dialog[open]")) return;
    // In a conversation the message list decides, wherever the finger starts.
    const scroller = event.target?.closest?.("#detail") ? $("messages") : scrollParent(event.target);
    if (scroller && scroller.scrollTop > 0) return;
    start = event.touches[0].clientY;
    startX = event.touches[0].clientX;
  }, { passive: true });
  root.addEventListener("touchmove", (event) => {
    if (start === null || event.touches.length !== 1) return;
    // A menu opened under the finger (a long press) takes the gesture: no refresh behind it.
    if (document.querySelector?.("dialog[open]")) {
      start = null;
      distance = 0;
      showPull(0);
      return;
    }
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
    if (start !== null && distance >= GESTURE.pullTriggerPx && !document.querySelector?.("dialog[open]")) pullRefresh();
    else if (!state.pullRefreshing) showPull(0);
    start = null;
    distance = 0;
  };
  root.addEventListener("touchend", finish, { passive: true });
  root.addEventListener("touchcancel", () => { start = null; distance = 0; if (!state.pullRefreshing) showPull(0); }, { passive: true });
}
function showLogin() {
  if (state.historySearch.mode && $("search")) $("search").value = state.search;
  cancelHistorySearch();
  state.historySearch.mode = false; state.historySearch.query = ""; state.historySearch.hits.clear();
  state.historySearch.errors.clear(); state.historySearch.cursor = null; state.historySearch.status = "";
  clearSavedDrafts();
  if ($("board-status")) $("board-status").open = false;
  closeQuickReplies();
  beginAuthEpoch();
  state.pinnedLocal.clear();
  window.SessionholicLabels?.locked?.();
  closeSessionControl(true);
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
  state.quickRepliesShared = null; state.quickRepliesEditor = null; state.quickRepliesSaving = null; state.quickRepliesError = "";
  state.quickRepliesUnsaved = null;
  state.conversationViews.clear();
  window.SessionholicGoals?.locked?.();
  state.phaseFilter = "";
  state.sessionCategory = "general"; state.filters.initiator = "";
  state.selectedBotRequest = null; state.botRequests = emptyBotRequests(); state.botConversations = new Map();
  renderSessionCategories([], Date.now() / 1000);
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
  state.closedTerminalNavigation = false;
  state.closedHistory?.clear?.();
  if ($("close-terminal-dialog").open) $("close-terminal-dialog").close();
  $("terminal-draft").value = "";
  $("terminal-viewport").replaceChildren();
  $("detail").replaceChildren();
  $("groups").replaceChildren();
  $("terminal-screen").hidden = true;
  applyDock();
  $("app").hidden = true;
  $("login").hidden = false;
  if ($("plan-dialog").open) $("plan-dialog").close();
  if ($("settings-dialog").open) $("settings-dialog").close();
  for (const id of ["usage-dialog", "hardware-dialog", "filter-dialog", "terminals-dialog", "new-session-dialog", "action-sheet"]) if ($(id)?.open) $(id).close();
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
// The height the app has: the visual viewport (an on-screen keyboard takes its space), except
// that without a focused text field no keyboard takes space, so a visual viewport shorter than
// the window is stale: iPadOS reports one for a moment when the app comes back from the
// background or a minimized window, with no resize after it. Not when pinch-zoomed (the
// visual viewport is then meant to be smaller).
function viewportHeight() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight || 800;
  if (!editingText() && (viewport?.scale || 1) <= 1.01 && window.innerHeight > height) return window.innerHeight;
  return height;
}
function updateViewport() {
  const height = Math.round(viewportHeight());
  document.documentElement.style.setProperty("--app-height", `${height}px`);
  redock();
  renderKeyboardRoom();
  renderAppShift();
  traceKeyboardRoom();
  syncChromeMode();
  const selected = selectedSession();
  if (selected) resizeChatInput(chatEntry(selected));
  fitTerminal();
}
// An iPad's on-screen keyboard takes half the screen: while typing with it the board's top
// bar and the terminal's note fold away (phones fold their own way, "is-typing"). Beside a
// docked terminal, typing on the board leaves the terminal its full height, so the keyboard
// opening and closing does not resize (redraw) the CLI. Returns whether that changed.
function renderKeyboardRoom() {
  // A phone held upright folds its own way (is-typing); one held sideways is as short as a tablet
  // with its keyboard up, so its top bar folds too.
  const open = touchDevice() && editingText() && keyboardOpen();
  const typing = open && (!phoneLayout() || shortLayout());
  // Typing on the board (its sheets included) leaves a docked terminal its height.
  const beside = typing && state.docked && !!document.activeElement?.closest?.("#app, dialog");
  // What the keyboard covers of the window's bottom (iOS keeps the window's height): toasts
  // stay above it.
  const viewport = window.visualViewport;
  const covered = open && viewport ? Math.max(0, Math.round((window.innerHeight || 0) - (viewport.offsetTop || 0) - viewport.height)) : 0;
  document.documentElement.style?.setProperty?.("--kb-inset", `${covered}px`);
  const classes = document.body?.classList;
  const changed = !!classes?.contains?.("keyboard-beside") !== beside;
  classes?.[typing ? "add" : "remove"]("keyboard-typing");
  classes?.[beside ? "add" : "remove"]("keyboard-beside");
  document.documentElement.style?.setProperty?.("--layout-height", `${Math.round(state.viewportTall || window.innerHeight || 0)}px`);
  return changed;
}
// iOS scrolls the page to show a focused field above its keyboard before the app shrinks to
// the space left (--app-height): the app then sits above what is on screen, which is blank
// unless scrolled back. Both panes follow the visible area, including the interval after
// focus leaves a field but Safari has not yet restored the viewport's scroll position.
function renderAppShift() {
  const viewport = window.visualViewport;
  const following = (editingText() && keyboardOpen()) || (state.appShift || 0) > 0;
  const top = viewport && touchDevice() && (viewport.scale || 1) <= 1.01 && following
    ? Math.max(0, Math.round(viewport.pageTop ?? ((window.scrollY || 0) + (viewport.offsetTop || 0)))) : 0;
  if (top === (state.appShift || 0)) return;
  state.appShift = top;
  document.documentElement.style?.setProperty?.("--app-shift", `${top}px`);
  document.body?.classList?.[top ? "add" : "remove"]("app-shifted");
}
// The keyboard's room as this device reports it (sizes and offsets, never content), sent to
// the board like the mic's trace when an on-screen keyboard opens: at once, once it settled
// and a second later. A few per page.
const KEYBOARD_TRACES = 12;
function traceKeyboardRoom() {
  const open = touchDevice() && editingText() && keyboardOpen();
  if (open === !!state.keyboardTraced) return;
  state.keyboardTraced = open;
  if (!open || (state.keyboardTraces = (state.keyboardTraces || 0) + 1) > KEYBOARD_TRACES) return;
  const t0 = Date.now();
  const events = [];
  const env = { kind: "viewport", field: String(document.activeElement?.id || ""), touch: true,
    standalone: !!window.matchMedia?.("(display-mode: standalone)")?.matches, phone: phoneLayout(),
    width: Math.round(window.innerWidth || 0), docked: !!state.docked };
  const box = (node) => node?.getBoundingClientRect?.() || null;
  const round = (value) => (Number.isFinite(value) ? Math.round(value) : null);
  const snap = (e) => {
    const viewport = window.visualViewport;
    const app = box($("app"));
    const field = box(document.activeElement);
    events.push({ t: Date.now() - t0, e, ih: round(window.innerHeight), vh: round(viewport?.height),
      vt: round(viewport?.offsetTop), pt: round(viewport?.pageTop), sy: round(window.scrollY), at: round(app?.top),
      ah: round(app?.height), it: round(field?.top), ib: round(field?.bottom), shift: state.appShift || 0 });
  };
  snap("open");
  setTimeout(() => snap("settled"), 400);
  setTimeout(() => snap("later"), 1500);
  setTimeout(() => {
    api("/api/diagnostics/dictation", { method: "POST", body: { env, outcome: "viewport", events } }).catch(() => {});
  }, 1700);
}
// Back on screen (from the background, a minimized window or the app switcher): the
// sizes WebKit reports settle a moment later and may come with no resize event, so the
// height is read again now and a little after.
function settleViewport() {
  updateViewport();
  window.requestAnimationFrame?.(() => updateViewport());
  for (const delay of [250, 1000]) setTimeout(updateViewport, delay);
}
// "오프라인 · 다시 연결 중" at the top while the browser is offline or the
// board failed to answer twice in a row; it goes away by itself once a read
// succeeds. The terminal shows its own connection state instead.
function checkWebVersion() {
  const served = state.capabilities?.webVersion;
  const behind = !!APP_VERSION && typeof served === "string" && !!served && served !== APP_VERSION;
  const found = behind && !state.updateAvailable;
  state.updateAvailable = behind;
  if (found && Date.now() - (state.shownAt || 0) < UPDATE_RETURN_MS && reloadForUpdate()) return;
  renderConnectivity();
}
// A newer screen loads now unless a reload would lose something (what warnDraftUnload
// guards), interrupt a terminal or close an open sheet; the banner offers it then.
// A plain reload is enough: the page is read from the network and assets carry ?v=.
function reloadForUpdate() {
  if (!state.updateAvailable || networkOffline() || state.terminalVisible || unsavedWork() ||
      document.querySelector?.("dialog[open]")) return false;
  window.location.reload();
  return true;
}
// The banner's tap: never at the cost of unsent words (a home-screen app shows no leave prompt).
function updateBannerPressed() {
  if (unsavedWork()) {
    toast("보내지 않은 글이나 첨부가 있어 새로고침하지 않았습니다. 보내거나 지운 뒤 다시 눌러 주세요.");
    return;
  }
  window.location.reload();
}
function renderConnectivity() {
  const update = $("update-banner");
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
  box.hidden = !text || boardCovered();
  // A connection problem is said first; the newer screen waits for it.
  if (update) update.hidden = !state.updateAvailable || !!text || state.terminalVisible;
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
    if (boardCovered()) return;
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
// The page is on screen again, maybe after hours in the background: once per return,
// though a browser may fire both visibilitychange and pageshow for it.
function pageShown() {
  if (!state.pageHidden) return;
  state.pageHidden = false;
  // Live updates that failed while away (or as the network came back) start afresh.
  state.eventsRetryAt = 0;
  // A home-screen app in the background can miss the browser's online event, and an
  // offline page reads nothing; one shown again believes navigator.onLine.
  if (state.offline && globalThis.navigator?.onLine !== false) {
    state.shownAt = Date.now();
    recoverConnection();
  } else if (boardCovered()) {
    startTerminalReader();
  } else if (state.authenticated) {
    if (state.terminalVisible) startTerminalReader();
    state.shownAt = Date.now();
    if (reloadForUpdate()) return;
    poll(true);
    const selected = selectedSession();
    if (selected) loadChatState(selected, true);
  } else recoverConnection();
}
// Unsent words or files on this page: what leaving or reloading it would lose.
function unsavedWork() {
  return [...state.drafts.values(), state.terminal ? $("terminal-draft").value : ""].some(Boolean) ||
    [...state.chats.values()].some((entry) => entry.text || entry.attachments.length || entry.pending || entry.uploads);
}
function warnDraftUnload(event) {
  if (state.discardOnReload) return;
  persistAllDrafts();
  if (!unsavedWork() || state.discardOnReload) return;
  event.preventDefault();
  event.returnValue = "";
}

window.addEventListener("DOMContentLoaded", () => {
  navigateView("list", true);
  window.SessionholicLabels?.init?.({ api, sourceOf, getAuthEpoch: () => state.authEpoch,
    canEdit: (session) => state.authenticated && !botRequestForNative(sourceOf(session)),
    openDialog: (dialog) => sheet(dialog.id).open(), toast,
    onChanged: () => {
      const selected = selectedSession();
      if (selected && state.detailFor === selected.key) {
        state.detailRevision = detailRevision(selected);
        patchDetailMetadata(selected);
      }
      render();
    } });
  window.SessionholicPush?.init?.({ api, toast });
  window.SessionholicGoals?.init?.({ api, sessions: allSessions, openSession: select,
    startWork: openNewSession, manageSession,
    hideSession: () => setDetailShown(false), getSheet: sheet,
    onChanged: render,
    onSearch: (query) => { state.search = query.trim(); $("search").value = query; render(); } });
  document.addEventListener("pointerdown", closeChatToolsOutside);
  document.addEventListener("keydown", documentKeydown);
  window.addEventListener("popstate", (event) => {
    let view = event.state?.[INSTANCE.historyKey] ? event.state.view : "list";
    if (state.closedTerminalNavigation || state.closedHistory?.has(event.state?.navigationId) || view === "terminal" && !state.terminal) {
      state.closedTerminalNavigation = false;
      view = "list";
      navigateView("list", true);
      event = { ...event, state: window.history.state };
    }
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
    const reopen = fromTerminal ? state.reopenOnReturn : null;
    state.reopenOnReturn = null;
    // An ended terminal's 이 세션 다시 열기 (reopenEndedTerminal), once the board is shown.
    const reopenSession = () => {
      const target = reopen && allSessions().find((item) => item.key === reopen);
      if (target) openSessionTerminal(target);
    };
    // Forward again to a notification's conversation shows it, not the terminal's launch.
    if (fromTerminal) returnToBoard({ animate, selectLaunchedSource: !event.state?.notice });
    if (window.SessionholicGoals?.handleHistory?.(event)) {
      reopenSession();
      return;
    }
    const detail = view === "detail" && !!(state.selected || state.selectedBotRequest);
    stopChatPolling();
    setDetailShown(detail, { direction: animate && !fromTerminal ? (detail ? "push" : "pop") : null, focus: true });
    const selected = selectedSession();
    if (selected && chatVisible()) loadChatState(selected, true);
    reopenSession();
  });
  $("settings").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-done").addEventListener("click", closeSettings);
  $("hard-reload")?.addEventListener("click", hardReload);
  $("quick-reload")?.addEventListener("click", hardReload);
  $("board-status-panel")?.addEventListener("click", (event) => {
    if (event.target?.closest?.("button")) $("board-status").open = false;
  });
  $("update-banner")?.addEventListener("click", updateBannerPressed);
  $("quick-replies-save")?.addEventListener("click", saveSharedQuickReplies);
  $("quick-replies-reload")?.addEventListener("click", () => loadQuickReplyPreferences({ replaceEditor: true }));
  $("quick-replies-close")?.addEventListener("click", () => {
    rememberQuickReplyEdits(); $("settings-dialog").close(); $("settings")?.focus?.({ preventScroll: true });
  });
  $("draft-recovery")?.addEventListener("change", (event) => {
    prefs.set("draftRecovery", event.target.checked);
    if (!event.target.checked) clearSavedDrafts(); else persistAllDrafts();
  });
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
  // A top chip reached by the keyboard scrolls into the header's view (a phone scrolls it).
  for (const id of ["usage", "hardware"])
    $(id).addEventListener("focus", (event) => event.currentTarget.scrollIntoView?.({ block: "nearest", inline: "nearest" }));
  $("usage-close").addEventListener("click", () => closeSheet("usage-dialog", "usage"));
  $("hardware").addEventListener("click", openHardware);
  $("hardware-close").addEventListener("click", () => closeSheet("hardware-dialog", "hardware"));
  $("filter-open").addEventListener("click", () => sheet("filter-dialog").open());
  $("filter-close").addEventListener("click", () => closeSheet("filter-dialog", "filter-open"));
  $("filter-done").addEventListener("click", () => closeSheet("filter-dialog", "filter-open"));
  $("filter-reset").addEventListener("click", resetFilters);
  for (const category of Object.keys(SESSION_CATEGORIES))
    $(`session-category-${category}`).addEventListener("click", () => setSessionCategory(category));
  $("session-categories").addEventListener("keydown", sessionCategoryKeydown);
  $("session-category-search-action").addEventListener("click", (event) => setSessionCategory(event.currentTarget.dataset.category));
  $("running-terminals").addEventListener("click", () => {
    renderTerminals();
    sheet("terminals-dialog").open();
  });
  $("terminals-close").addEventListener("click", () => closeSheet("terminals-dialog", "running-terminals"));
  $("new-session").addEventListener("click", () => openNewSession());
  $("new-session-close").addEventListener("click", () => closeNewSession());
  $("action-close").addEventListener("click", () => closeActionSheet());
  for (const [id, close] of [["usage-dialog", () => closeSheet("usage-dialog", "usage")],
    ["hardware-dialog", () => closeSheet("hardware-dialog", "hardware")],
    ["filter-dialog", () => closeSheet("filter-dialog", "filter-open")],
    ["terminals-dialog", () => closeSheet("terminals-dialog", "running-terminals")],
    ["new-session-dialog", () => closeNewSession()],
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
    if (state.historySearch.mode) {
      cancelHistorySearch(); state.historySearch.query = event.target.value.trim();
      state.historySearch.cursor = null; state.historySearch.hits.clear(); state.historySearch.errors.clear();
      state.historySearch.status = "검색어를 입력하고 본문 검색을 눌러 주세요."; renderHistorySearch(); return;
    }
    state.search = event.target.value.trim();
    window.SessionholicGoals?.setQuery?.(state.search);
    render();
  });
  $("search-body")?.addEventListener("click", () => {
    cancelHistorySearch(); state.historySearch.mode = !state.historySearch.mode;
    if (state.historySearch.mode) state.historySearch.query = $("search").value.trim();
    else $("search").value = state.search;
    renderHistorySearch();
  });
  $("search")?.addEventListener("keydown", (event) => {
    if (state.historySearch.mode && event.key === "Enter" && !event.isComposing && event.keyCode !== 229 && !event.repeat) {
      event.preventDefault(); startHistorySearch({ more: !!state.historySearch.cursor });
    }
  });
  $("body-search-start")?.addEventListener("click", () => startHistorySearch({ more: !!state.historySearch.cursor }));
  $("body-search-stop")?.addEventListener("click", () => { cancelHistorySearch(); renderHistorySearch(); });
  $("show-old").addEventListener("change", (event) => {
    state.showOld = event.target.checked;
    render();
  });
  $("show-removed").addEventListener("change", (event) => {
    state.showRemoved = event.target.checked;
    render();
  });
  $("home").addEventListener("click", () => {
    rememberConversationView();
    if (window.SessionholicGoals) window.SessionholicGoals.openTab("sessions", { clearSelection: true });
    else backToList();
  });
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
  $("terminal-dock").addEventListener("click", toggleDock);
  // iPad can scroll/shrink both panes between press and click when focus leaves an input.
  // Keep the hit target in place until its click decides where the focus should go.
  for (const id of ["terminal-back", "terminal-dock", "terminal-close"])
    for (const type of ["pointerdown", "mousedown"]) $(id).addEventListener(type, (event) => {
      if (touchDevice() && editingText()) event.preventDefault();
    });
  for (const [id, side] of [["app", "board"], ["terminal-screen", "terminal"]])
    for (const type of ["pointerdown", "focusin"]) $(id).addEventListener(type, () => {
      noteDockSide(side);
      // The keyboard may stay up while the focus crosses sides: the terminal's height follows.
      if (type === "focusin" && renderKeyboardRoom()) fitTerminal();
    });
  $("terminal-close").addEventListener("click", () => {
    if (state.terminal) closeTerminal(state.terminal.id, { trigger: $("terminal-close") });
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
  $("terminal-reopen").addEventListener("click", reopenEndedTerminal);
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
  for (const node of $("terminal-keys").querySelectorAll("button[data-key]")) {
    node.addEventListener("pointerdown", (event) => event.preventDefault());
    node.addEventListener("click", () => pressTerminalKey(node.dataset.key));
  }
  for (const node of $("terminal-keys").querySelectorAll("button[data-mod]")) {
    node.addEventListener("pointerdown", (event) => event.preventDefault());
    node.addEventListener("click", () => toggleTerminalMod(node.dataset.mod));
  }
  $("draft-paste").addEventListener("pointerdown", prepareDraftPaste);
  $("draft-paste").addEventListener("click", () => pasteDraft(true));
  $("draft-type").addEventListener("pointerdown", prepareDraftPaste);
  $("draft-type").addEventListener("click", () => pasteDraft(false));
  for (const type of ["pointerdown", "mousedown"])
    $("draft-mic").addEventListener(type, (event) => {
      if (document.activeElement === $("terminal-draft")) event.preventDefault();
    });
  $("draft-mic").addEventListener("click", toggleDraftDictation);
  renderDictation();
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
    // A hidden page does not keep the microphone open, listening or parked.
    if (document.hidden) {
      stopDictation({ abort: true });
      dropParkedDictation("hidden");
    }
    if (document.hidden) {
      state.pageHidden = true;
      // Live updates stop with the page; a visible page reconnects after its read.
      closeEvents();
      if (state.terminalVisible) {
        saveDraft();
        stopTerminalReader();
        state.inputEpoch++;
        terminalStatus("화면으로 돌아오면 다시 연결합니다.");
      }
    } else {
      settleViewport();
      pageShown();
    }
  });
  // A page restored from the back-forward cache may fire no visibilitychange.
  window.addEventListener("pagehide", () => { persistAllDrafts(); state.pageHidden = true; });
  window.addEventListener("pageshow", (event) => {
    settleViewport();
    if (event.persisted && !document.hidden) pageShown();
  });
  window.addEventListener("focus", settleViewport);
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
  // iOS moves the visible area with no resize (its keyboard's own scroll, or a drag).
  window.visualViewport?.addEventListener("scroll", renderAppShift);
  updateViewport();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener?.("message", (event) => {
      if (event.data?.type === "open-session") requestSessionOpen(event.data.url);
    });
  }
  // Opened from a notification: remember the session, then drop it from the address.
  if (globalThis.location?.search && requestSessionOpen(globalThis.location.href))
    window.history.replaceState(window.history.state, "", "/");
  if (networkOffline()) {
    showOfflineStart();
  } else {
    const authEpoch = state.authEpoch;
    poll(true).then(() => {
      if (authEpoch === state.authEpoch && state.authenticated) refreshTerminals(authEpoch);
    });
  }
});
