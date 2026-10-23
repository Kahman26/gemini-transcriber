// Страница звонка: вкладки, плеер ↔ расшифровка, «Поделиться» через меню iOS.
"use strict";

const audio = document.getElementById("audio");

// ── вкладки ──────────────────────────────────────────────────────────────
const tabs = [...document.querySelectorAll("[data-tab]")];
function showTab(name) {
  tabs.forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  document.querySelectorAll("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== name));
}
tabs.forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

// ── скорость ─────────────────────────────────────────────────────────────
document.querySelectorAll("[data-speed]").forEach((btn) =>
  btn.addEventListener("click", () => {
    if (!audio) return;
    audio.playbackRate = Number(btn.dataset.speed);
    document.querySelectorAll("[data-speed]").forEach((b) => b.classList.toggle("active", b === btn));
  }));

// ── таймкоды: нажатие перематывает запись ────────────────────────────────
document.addEventListener("click", (e) => {
  const ts = e.target.closest(".ts");
  if (!ts || !audio) return;
  audio.currentTime = Number(ts.dataset.t);
  audio.play().catch(() => {});
});

// ── подсветка текущей реплики ────────────────────────────────────────────
const lines = [...document.querySelectorAll(".line[data-t]")];
const times = lines.map((l) => Number(l.dataset.t));
const follow = document.getElementById("follow");
let current = -1;

function lineAt(t) {
  // последняя реплика, начавшаяся не позже t (таймкоды идут по возрастанию)
  let lo = 0, hi = times.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t + 0.3) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

if (audio && lines.length) {
  audio.addEventListener("timeupdate", () => {
    const i = lineAt(audio.currentTime);
    if (i === current) return;
    lines[current]?.classList.remove("current");
    current = i;
    const line = lines[i];
    if (!line) return;
    line.classList.add("current");
    const panel = line.closest("[data-panel]");
    if (follow?.checked && panel && !panel.hidden && !audio.paused) {
      line.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  });
}

// ── «Поделиться» ─────────────────────────────────────────────────────────
// Safari открывает системное меню только сразу после нажатия, поэтому файлы
// скачиваются заранее — когда открывается лист, а не по кнопке.
const shareSheet = document.getElementById("share-sheet");
const prepared = new Map();

function filenameFrom(resp, fallback) {
  const cd = resp.headers.get("content-disposition") || "";
  const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) return decodeURIComponent(star[1]);
  const plain = cd.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : fallback;
}

function saveFile(file) {
  const url = URL.createObjectURL(file);
  const a = document.createElement("a");
  a.href = url;
  a.download = file.name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

async function prepare(item) {
  const key = item.dataset.share;
  const btn = item.querySelector("[data-share-btn]");
  if (prepared.has(key)) return;
  prepared.set(key, null);
  btn.textContent = "…";
  try {
    const resp = await fetch(item.dataset.url, { credentials: "same-origin" });
    if (!resp.ok) throw new Error(resp.status);
    const blob = await resp.blob();
    const file = new File([blob], filenameFrom(resp, key), { type: blob.type });
    prepared.set(key, file);
    const canShare = navigator.canShare?.({ files: [file] });
    btn.textContent = canShare ? "Поделиться" : "Скачать";
    if (!canShare) shareSheet.querySelector("[data-share-note]").hidden = false;
    btn.disabled = false;
  } catch {
    prepared.delete(key);
    btn.textContent = "Ошибка";
  }
}

if (shareSheet) {
  shareSheet.addEventListener("sheet:open", () =>
    shareSheet.querySelectorAll("[data-share]").forEach(prepare));
  shareSheet.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-share-btn]");
    if (!btn) return;
    const file = prepared.get(btn.closest("[data-share]").dataset.share);
    if (!file) return;
    if (navigator.canShare?.({ files: [file] })) {
      try {
        await navigator.share({ files: [file] });
      } catch (err) {
        if (err.name !== "AbortError") saveFile(file);
      }
    } else {
      saveFile(file);
    }
  });
}
