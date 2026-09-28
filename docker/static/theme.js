/* WorkBuddy 每日助手 —— 主题切换（控制台 + 报告单文件共用一份）
   ------------------------------------------------------------------
   设计要点
     · 主题存在 localStorage['wb-theme']，值 'light' | 'dark'。
     · 首次访问没有记录时，跟随系统 prefers-color-scheme。
     · 页头内联了同样逻辑的一小段脚本（防闪白），这里负责按钮交互。
     · 报告 HTML 可单独打开，所以这段必须能内联进去（不要外链）。
   ------------------------------------------------------------------ */
(function () {
  "use strict";
  var KEY = "wb-theme";
  var root = document.documentElement;

  function sysTheme() {
    return (window.matchMedia &&
            window.matchMedia("(prefers-color-scheme: dark)").matches)
      ? "dark" : "light";
  }

  function saved() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function apply(t) {
    root.setAttribute("data-theme", t);
    root.style.colorScheme = (t === "dark") ? "dark" : "light";
    var ic = document.getElementById("wb-theme-ico");
    var lb = document.getElementById("wb-theme-label");
    if (ic) { ic.textContent = (t === "dark") ? "☀" : "☾"; }
    if (lb) { lb.textContent = (t === "dark") ? "白天" : "夜晚"; }
    var b = document.getElementById("wb-theme-btn");
    if (b) {
      b.setAttribute("aria-label",
        (t === "dark") ? "切换到白天模式" : "切换到夜晚模式");
      b.title = (t === "dark") ? "切换到白天模式" : "切换到夜晚模式";
    }
  }

  // 首次访问跟随系统；用户手动切过就永远听用户的
  apply(saved() || sysTheme());

  window.wbToggleTheme = function () {
    var next = (root.getAttribute("data-theme") === "dark") ? "light" : "dark";
    try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式忽略 */ }
    apply(next);
  };

  // 没手动设过时，跟随系统变化（开着页面时切系统主题也能跟上）
  if (window.matchMedia) {
    try {
      window.matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", function (e) {
          if (!saved()) { apply(e.matches ? "dark" : "light"); }
        });
    } catch (err) { /* 老浏览器忽略 */ }
  }
})();
