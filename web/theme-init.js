"use strict";

// Runs before the first paint (loaded in <head> without defer; the CSP blocks
// inline scripts). It applies only the stored look of the board, the same
// "<prefix>ui:*" preferences app.js keeps, so a forced light or dark
// board and a chosen text size never flash the device defaults first. The
// prefix comes from the server's <meta name="sessionholic-instance"> above it.
(function () {
  var root = document.documentElement;
  if (!root) return;
  var prefix = "sessionholic:";
  try {
    var meta = document.querySelector ? document.querySelector('meta[name="sessionholic-instance"]') : null;
    var names = meta ? JSON.parse(meta.getAttribute("content") || "{}") : null;
    if (names && typeof names.storagePrefix === "string" && /^[A-Za-z0-9_.:-]{1,32}$/.test(names.storagePrefix))
      prefix = names.storagePrefix;
  } catch (error) {
    // a broken name list keeps the public prefix
  }
  function read(key) {
    try {
      return JSON.parse(window.localStorage.getItem(prefix + "ui:" + key));
    } catch (error) {
      return null; // storage blocked or a malformed value: use the defaults
    }
  }
  var theme = read("theme");
  if (theme === "light" || theme === "dark") {
    root.setAttribute("data-theme", theme);
    // The browser bar follows the forced board. Each meta keeps its original
    // color in data-original so app.js can restore it for "auto".
    var metas = document.querySelectorAll ? document.querySelectorAll('meta[name="theme-color"]') : [];
    for (var i = 0; i < metas.length; i++) {
      if (metas[i].getAttribute("data-original") == null)
        metas[i].setAttribute("data-original", metas[i].getAttribute("content") || "");
      metas[i].setAttribute("content", theme === "dark" ? "#000000" : "#f2f2f7");
    }
  }
  var size = read("textSize");
  if (size === "small" || size === "large") root.setAttribute("data-text-size", size);
})();
