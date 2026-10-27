// Toast notifications (top-right). Server flashes are rendered into #toasts;
// scripts can add more with toast(message, category).
(() => {
  const box = document.getElementById("toasts");
  const DURATION = { error: 8000, warn: 6000 };

  function dismiss(el) {
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
  }

  function arm(el) {
    const close = document.createElement("button");
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.onclick = () => dismiss(el);
    el.append(close);

    const category = [...el.classList].find((c) => c !== "toast") || "info";
    let timer = setTimeout(() => dismiss(el), DURATION[category] || 4000);
    // Pause while hovered so long messages can be read.
    el.addEventListener("mouseenter", () => clearTimeout(timer));
    el.addEventListener("mouseleave", () => { timer = setTimeout(() => dismiss(el), 2000); });
  }

  window.toast = (message, category = "info") => {
    const el = document.createElement("div");
    el.className = `toast ${category}`;
    el.setAttribute("role", category === "error" ? "alert" : "status");
    el.textContent = message;
    box.append(el);
    arm(el);
  };

  box.querySelectorAll(".toast").forEach(arm);
})();
