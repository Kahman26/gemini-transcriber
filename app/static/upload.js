// Загрузка записей: список выбранных файлов и прогресс отправки.
"use strict";

const form = document.getElementById("upload-form");
const input = document.getElementById("files");
const list = document.getElementById("file-list");
const zone = document.getElementById("dropzone");
const progressBox = document.getElementById("upload-progress");
const bar = progressBox.querySelector("progress");
const progressText = document.getElementById("upload-progress-text");
const errorBox = document.getElementById("upload-error");
const submit = document.getElementById("upload-submit");

const mb = (bytes) => (bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + " МБ";

function showError(text) {
  errorBox.textContent = text;
  errorBox.hidden = !text;
}

function renderList() {
  list.replaceChildren(...[...input.files].map((f) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    const size = document.createElement("span");
    name.textContent = f.name;
    size.textContent = mb(f.size);
    li.append(name, size);
    return li;
  }));
  list.hidden = !input.files.length;
  showError("");
}

input.addEventListener("change", renderList);
["dragenter", "dragover"].forEach((t) => zone.addEventListener(t, () => zone.classList.add("drag")));
["dragleave", "drop"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("drag")));

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!input.files.length) return showError("Выберите хотя бы один файл");
  const maxFiles = 10;
  if (input.files.length > maxFiles) return showError(`Не больше ${maxFiles} файлов за раз`);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  xhr.setRequestHeader("X-CSRF-Token", window.TR.csrf());
  xhr.upload.addEventListener("progress", (ev) => {
    if (!ev.lengthComputable) return;
    const pct = Math.round((ev.loaded / ev.total) * 100);
    bar.value = pct;
    progressText.textContent = pct < 100 ? `Загрузка… ${pct}%` : "Проверяю файл…";
  });
  xhr.addEventListener("load", () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* не JSON — ниже общий текст */ }
    if (xhr.status === 200 && data.ok) {
      if (data.errors?.length) alert("Не все файлы приняты:\n" + data.errors.join("\n"));
      location.href = data.redirect || "/";
      return;
    }
    if (xhr.status === 413) data.errors = ["Файл слишком большой"];
    finish((data.errors || ["Не получилось загрузить, попробуйте ещё раз"]).join("\n"));
  });
  xhr.addEventListener("error", () => finish("Нет связи с сервером"));

  function finish(err) {
    submit.disabled = false;
    progressBox.hidden = true;
    showError(err);
  }

  submit.disabled = true;
  progressBox.hidden = false;
  bar.value = 0;
  progressText.textContent = "Загрузка…";
  showError("");
  xhr.send(new FormData(form));
});
