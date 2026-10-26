// Copy-to-clipboard for any [data-copy] button. navigator.clipboard only exists on
// HTTPS/localhost, so fall back to a hidden textarea + execCommand on plain HTTP.
(() => {
  function fallbackCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    // Inside an open modal <dialog>, only the dialog's subtree can take focus/selection.
    (document.querySelector("dialog[open]") || document.body).append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }

  async function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
    }
    return fallbackCopy(text);
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const text = btn.dataset.copy;
    if (await copy(text)) {
      btn.classList.add("copied");
      btn.textContent = "✓";
      setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "⧉"; }, 1200);
      window.toast?.(`Copied ${text.slice(0, 6)}…${text.slice(-4)}`, "success");
    } else {
      window.toast?.("Couldn't copy automatically — select the address and press Ctrl+C.", "error");
    }
  });
})();
