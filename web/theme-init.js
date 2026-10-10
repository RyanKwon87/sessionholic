"use strict";

// Runs before the first paint (loaded in <head> without defer; the CSP blocks
// inline scripts). It applies only the stored look of the board, the same
// "sessionholic:ui:*" preferences app.js keeps, so a forced light or dark
// board and a chosen text size never flash the device defaults first.
(function () {
  var root = document.documentElement;
  if (!root) return;
  function read(key) {
    try {
      return JSON.parse(window.localStorage.getItem("sessionholic:ui:" + key));
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
