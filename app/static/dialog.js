// Modal dialogs: [data-dialog="id"] opens <dialog id="id">, [data-close] closes it,
// and so do Esc (native) and a click on the backdrop.
document.addEventListener("click", (ev) => {
  const opener = ev.target.closest("[data-dialog]");
  if (opener) {
    ev.preventDefault();
    document.getElementById(opener.dataset.dialog)?.showModal();
    return;
  }
  const closer = ev.target.closest("[data-close]");
  if (closer) {
    closer.closest("dialog")?.close();
    return;
  }
  if (ev.target instanceof HTMLDialogElement) {
    const r = ev.target.getBoundingClientRect();
    const inside = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
    if (!inside) ev.target.close();
  }
});
