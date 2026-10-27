// Page-level loading feedback: a top progress bar on navigation, plus a spinner
// overlay for form submissions (refresh, import, approve…). Shown only when the
// wait exceeds a short delay so fast pages don't flicker.
(() => {
  const bar = document.getElementById("busy-bar");
  const overlay = document.getElementById("busy-overlay");
  let timer = null;

  function start(withOverlay) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      bar.classList.add("on");
      if (withOverlay) overlay.hidden = false;
    }, 200);
  }

  function stop() {
    clearTimeout(timer);
    bar.classList.remove("on");
    overlay.hidden = true;
  }

  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    // Respect cancelled submits (e.g. a confirm() answered "Cancel") and in-page dialogs.
    if (ev.defaultPrevented || form.method === "dialog" || form.id === "graph-form") return;
    start(form.method.toLowerCase() === "post");
  });

  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a[href]");
    if (!a || ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    if (a.target === "_blank" || a.hasAttribute("download")) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;  // in-page anchor
    start(false);
  });

  // Coming back via the back/forward cache must not leave the spinner up.
  window.addEventListener("pageshow", stop);
})();
