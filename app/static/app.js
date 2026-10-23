// Общее для всех страниц: подтверждения, нижние листы, поиск, статусы обработки.
"use strict";

window.TR = {
  csrf: () => document.querySelector('meta[name="csrf"]')?.content || "",
};

// Подтверждение опасных действий: <form data-confirm="…">
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset?.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// ── нижние листы ─────────────────────────────────────────────────────────
function openSheet(id) {
  const sheet = document.getElementById(id);
  if (!sheet) return;
  sheet.hidden = false;
  document.body.style.overflow = "hidden";
  sheet.dispatchEvent(new CustomEvent("sheet:open"));
}
function closeSheets() {
  document.querySelectorAll(".sheet").forEach((s) => (s.hidden = true));
  document.body.style.overflow = "";
}
document.addEventListener("click", (e) => {
  const opener = e.target.closest("[data-open-sheet]");
  if (opener) return openSheet(opener.dataset.openSheet);
  if (e.target.closest("[data-close-sheet]")) closeSheets();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheets(); });

// ── копирование токена ───────────────────────────────────────────────────
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const input = document.querySelector(btn.dataset.copy);
  if (!input) return;
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select();
    document.execCommand("copy");
  }
  btn.textContent = "Скопировано";
});

// ── поиск по списку ──────────────────────────────────────────────────────
const filter = document.querySelector("[data-filter]");
if (filter) {
  const items = [...document.querySelectorAll("[data-item]")];
  const empty = document.querySelector("[data-filter-empty]");
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    items.forEach((li) => {
      const ok = !q || li.dataset.search.includes(q);
      li.hidden = !ok;
      shown += ok;
    });
    if (empty) empty.hidden = shown > 0;
  });
}

// ── статусы обработки: опрос, пока что-то в очереди ──────────────────────
const watched = () =>
  [...document.querySelectorAll("[data-status-id]")].filter((el) =>
    ["queued", "processing"].includes(el.dataset.status));

async function pollStatuses() {
  const els = watched();
  if (!els.length) return;
  const ids = [...new Set(els.map((el) => el.dataset.statusId))];
  try {
    const resp = await fetch("/calls/status?ids=" + ids.join(","), { credentials: "same-origin" });
    if (resp.status === 401) return location.reload();
    const data = await resp.json();
    let finished = false;
    els.forEach((el) => {
      const s = data[el.dataset.statusId];
      if (!s) return;
      const label = el.querySelector(".status-label");
      const bar = el.querySelector("progress");
      if (label) label.textContent = s.status === "processing" ? (s.message || s.label) : s.label;
      if (bar) bar.value = s.progress;
      if (s.status !== el.dataset.status && !["queued", "processing"].includes(s.status)) finished = true;
      el.dataset.status = s.status;
    });
    if (finished) return location.reload();
  } catch { /* сеть пропала — попробуем позже */ }
  setTimeout(pollStatuses, document.hidden ? 10000 : 3000);
}
if (watched().length) setTimeout(pollStatuses, 2000);
