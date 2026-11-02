"use strict";

const STATUS_LABELS = {
  ok: "распознана",
  check: "сверить",
  doubtful: "сомнительная",
  manual: "внесена вручную",
  edited: "исправлена",
  confirmed: "сверена",
};
const VALUE_PATTERN = /^-?\d{1,4}([.,]\d{1,3})?$/;
const MIN_MARKER_WIDTH = 9;
const MIN_MARKER_HEIGHT = 7;

const byId = (id) => document.getElementById(id);
const state = {
  analysis: null,
  marks: [],
  metadata: {},
  pageNumber: 1,
  selectedId: null,
  filter: "all",
  adding: false,
  view: { scale: 1, x: 0, y: 0 },
  summaryRequest: null,
  summaryTimer: null,
};

function formatValue(value, digits = 2) {
  return Number(value).toFixed(digits).replace(".", ",");
}

function parseValue(text) {
  const cleaned = text.trim();
  return VALUE_PATTERN.test(cleaned) ? Number(cleaned.replace(",", ".")) : null;
}

function showToast(message, bad = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("bad", bad);
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, bad ? 7000 : 3500);
}

function showScreen(name) {
  byId("start").hidden = name !== "start";
  byId("progress").hidden = name !== "progress";
  byId("workspace").hidden = name !== "workspace";
}

async function errorMessage(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch { /* ответ не JSON */ }
  return `Сервер вернул ошибку ${response.status}. Попробуйте еще раз.`;
}

async function startAnalysis(request, title) {
  byId("start-error").hidden = true;
  byId("progress-title").textContent = title;
  showScreen("progress");
  try {
    const response = await request();
    if (!response.ok) throw new Error(await errorMessage(response));
    openResult(await response.json());
  } catch (error) {
    const known = error instanceof TypeError ? "Нет связи с сервером. Проверьте, что приложение запущено." : error.message;
    byId("start-error").textContent = known;
    byId("start-error").hidden = false;
    showScreen("start");
  }
}

function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  startAnalysis(() => fetch("/api/analyses", { method: "POST", body: form }), `Читаю ${file.name}`);
}

function openResult(result) {
  state.analysis = result;
  state.marks = result.marks.map((mark) => ({ ...mark, text: formatValue(mark.value, Math.max(2, decimals(mark.value))) }));
  state.metadata = { ...result.metadata };
  state.pageNumber = bestPage();
  state.selectedId = null;
  state.filter = "all";
  setAdding(false);

  byId("file-name").textContent = result.filename;
  byId("file-method").textContent = `Источник данных: ${result.method}`;
  document.querySelectorAll("[data-meta]").forEach((input) => { input.value = state.metadata[input.dataset.meta] || ""; });
  const page = currentPage();
  const dpiInput = byId("dpi-input");
  dpiInput.value = page.dpi ? Math.round(page.dpi) : "";
  dpiInput.disabled = page.rectified;
  dpiInput.placeholder = page.rectified ? "фото" : "для уклона";

  const warnings = byId("warnings");
  warnings.replaceChildren(...result.warnings.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  }));
  warnings.hidden = result.warnings.length === 0;

  renderPageTabs();
  showScreen("workspace");
  showTab("sheet");
  loadPage();
  renderLedger();
  requestSummary(0);
}

function decimals(value) {
  const part = String(value).split(".")[1];
  return part ? part.length : 0;
}

function bestPage() {
  const counts = new Map();
  state.marks.forEach((mark) => counts.set(mark.page, (counts.get(mark.page) || 0) + 1));
  let best = state.analysis.pages[0].number;
  let bestCount = -1;
  counts.forEach((count, page) => { if (count > bestCount) { best = page; bestCount = count; } });
  return best;
}

function currentPage() {
  return state.analysis.pages.find((page) => page.number === state.pageNumber);
}

function renderPageTabs() {
  const tabs = byId("page-tabs");
  const pages = state.analysis.pages;
  tabs.hidden = pages.length < 2;
  tabs.replaceChildren(...pages.map((page) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `Стр. ${page.number}`;
    button.setAttribute("aria-pressed", String(page.number === state.pageNumber));
    button.addEventListener("click", () => {
      state.pageNumber = page.number;
      renderPageTabs();
      loadPage();
    });
    return button;
  }));
}

function loadPage() {
  const page = currentPage();
  const layer = byId("sheet-layer");
  layer.style.width = `${page.width}px`;
  layer.style.height = `${page.height}px`;
  byId("sheet-image").src = page.preview;
  fitSheet();
}

function fitScale() {
  const page = currentPage();
  const box = byId("sheet-view").getBoundingClientRect();
  if (!box.width || !box.height) return 1;
  return Math.min(box.width / page.width, box.height / page.height);
}

function fitSheet() {
  const page = currentPage();
  const box = byId("sheet-view").getBoundingClientRect();
  const scale = fitScale();
  state.view = { scale, x: (box.width - page.width * scale) / 2, y: (box.height - page.height * scale) / 2 };
  applyView();
}

function zoomAt(factor, clientX, clientY) {
  const box = byId("sheet-view").getBoundingClientRect();
  const pointX = (clientX ?? box.left + box.width / 2) - box.left;
  const pointY = (clientY ?? box.top + box.height / 2) - box.top;
  const fit = fitScale();
  const scale = Math.min(Math.max(state.view.scale * factor, fit * 0.6), Math.max(fit * 14, 1.5));
  const ratio = scale / state.view.scale;
  state.view = { scale, x: pointX - (pointX - state.view.x) * ratio, y: pointY - (pointY - state.view.y) * ratio };
  applyView();
}

function applyView() {
  const { scale, x, y } = state.view;
  byId("sheet-layer").style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  renderMarkers();
}

function markerClass(mark) {
  const classes = ["marker"];
  if (mark.status === "check" || mark.status === "doubtful") classes.push(mark.status);
  if (mark.status === "manual" || mark.status === "edited" || mark.status === "confirmed") classes.push("manual");
  if (!mark.included) classes.push("excluded");
  if (mark.id === state.selectedId) classes.push("selected");
  return classes.join(" ");
}

function renderMarkers() {
  const { scale, x: offsetX, y: offsetY } = state.view;
  const layer = byId("markers");
  const nodes = [];
  state.marks.filter((mark) => mark.page === state.pageNumber).forEach((mark) => {
    const width = Math.max((mark.width || 0) * scale, MIN_MARKER_WIDTH);
    const height = Math.max((mark.height || 0) * scale, MIN_MARKER_HEIGHT);
    const button = document.createElement("button");
    button.type = "button";
    button.className = markerClass(mark);
    button.style.left = `${offsetX + mark.x * scale - width / 2}px`;
    button.style.top = `${offsetY + mark.y * scale - height / 2}px`;
    button.style.width = `${width}px`;
    button.style.height = `${height}px`;
    button.setAttribute("aria-label", `Отметка ${mark.text || "без значения"}, ${STATUS_LABELS[mark.status]}`);
    button.addEventListener("pointerdown", (event) => event.stopPropagation());
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectMark(mark.id, true);
    });
    if (mark.id === state.selectedId) {
      const flag = document.createElement("span");
      flag.className = "flag";
      flag.textContent = mark.text || "значение?";
      button.append(flag);
    }
    nodes.push(button);
  });
  layer.replaceChildren(...nodes);
}

function selectMark(id, fromSheet = false) {
  state.selectedId = id;
  const mark = state.marks.find((item) => item.id === id);
  if (mark && mark.page !== state.pageNumber) {
    state.pageNumber = mark.page;
    renderPageTabs();
    loadPage();
  }
  if (mark && !fromSheet) revealMark(mark);
  renderMarkers();
  document.querySelectorAll("#ledger-rows tr").forEach((row) => {
    const selected = Number(row.dataset.id) === id;
    row.classList.toggle("selected", selected);
    if (selected && fromSheet) row.scrollIntoView({ block: "nearest" });
  });
  if (fromSheet && mark && !visibleInFilter(mark)) {
    setFilter("all");
    selectMark(id, true);
  }
}

function revealMark(mark) {
  const box = byId("sheet-view").getBoundingClientRect();
  if (!box.width) return;
  const minimum = Math.max(fitScale() * 3, 0.5);
  if (state.view.scale < minimum) state.view.scale = minimum;
  const x = mark.x * state.view.scale + state.view.x;
  const y = mark.y * state.view.scale + state.view.y;
  const margin = 70;
  if (x < margin || y < margin || x > box.width - margin || y > box.height - margin || state.view.scale === minimum) {
    state.view.x = box.width / 2 - mark.x * state.view.scale;
    state.view.y = box.height / 2 - mark.y * state.view.scale;
  }
  applyView();
}

function needsCheck(mark) {
  return mark.status === "check" || (mark.status === "doubtful" && mark.included);
}

function visibleInFilter(mark) {
  if (state.filter === "check") return mark.status === "check" || mark.status === "doubtful";
  if (state.filter === "excluded") return !mark.included;
  return true;
}

function setFilter(filter) {
  state.filter = filter;
  renderLedger();
}

function renderLedger() {
  const rows = state.marks.filter(visibleInFilter).map(ledgerRow);
  byId("ledger-rows").replaceChildren(...rows);
  const empty = byId("ledger-empty");
  empty.hidden = rows.length > 0;
  if (state.marks.length === 0) {
    empty.textContent = "Отметки не найдены. Нажмите «Добавить отметку» и укажите их на листе.";
  } else {
    empty.textContent = state.filter === "check" ? "Все отметки сверены." : "Исключенных отметок нет.";
  }
  document.querySelectorAll("#filters button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter));
  });
  updateCounts();
}

function updateCounts() {
  const toCheck = state.marks.filter((mark) => mark.status === "check" || mark.status === "doubtful").length;
  const inReport = state.marks.filter(needsCheck).length;
  byId("count-all").textContent = state.marks.length;
  byId("count-check").textContent = toCheck;
  byId("count-excluded").textContent = state.marks.filter((mark) => !mark.included).length;
  const note = byId("pending-note");
  note.hidden = inReport === 0;
  note.textContent = `Не сверено: ${inReport}`;
}

function ledgerRow(mark) {
  const row = document.createElement("tr");
  row.dataset.id = mark.id;
  row.classList.toggle("selected", mark.id === state.selectedId);
  row.classList.toggle("excluded", !mark.included);
  row.addEventListener("click", () => selectMark(mark.id));

  const includeCell = document.createElement("td");
  const include = document.createElement("input");
  include.type = "checkbox";
  include.checked = mark.included;
  include.setAttribute("aria-label", `Включить отметку ${mark.text} в отчет`);
  include.addEventListener("click", (event) => event.stopPropagation());
  include.addEventListener("change", () => {
    if (include.checked && parseValue(mark.text) === null) {
      include.checked = false;
      showToast("Сначала введите значение отметки.", true);
      return;
    }
    mark.included = include.checked;
    afterEdit();
  });
  includeCell.append(include);

  const valueCell = document.createElement("td");
  const input = document.createElement("input");
  input.className = "value-input";
  input.value = mark.text;
  input.inputMode = "decimal";
  input.setAttribute("aria-label", "Значение отметки в метрах");
  input.addEventListener("focus", () => selectMark(mark.id));
  input.addEventListener("input", () => {
    input.setAttribute("aria-invalid", String(parseValue(input.value) === null));
  });
  input.addEventListener("change", () => commitValue(mark, input));
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    commitValue(mark, input);
    if (parseValue(input.value) !== null) confirmAndAdvance(mark);
  });
  valueCell.append(input);

  const statusCell = document.createElement("td");
  const status = document.createElement("span");
  status.className = `status ${mark.status}`;
  status.textContent = STATUS_LABELS[mark.status];
  statusCell.append(status);
  if (mark.note) {
    const note = document.createElement("small");
    note.className = "note";
    note.textContent = mark.note;
    statusCell.append(note);
  }

  const actionCell = document.createElement("td");
  if (mark.status === "check" || mark.status === "doubtful") {
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "row-action";
    confirm.textContent = "Сверено";
    confirm.setAttribute("aria-label", `Подтвердить отметку ${mark.text}`);
    confirm.addEventListener("click", (event) => {
      event.stopPropagation();
      if (parseValue(input.value) !== null) confirmAndAdvance(mark);
    });
    actionCell.append(confirm);
  }
  row.append(includeCell, valueCell, statusCell, actionCell);
  return row;
}

function commitValue(mark, input) {
  const value = parseValue(input.value);
  if (value === null) {
    showToast("Введите отметку числом, например 145,27.", true);
    return;
  }
  input.setAttribute("aria-invalid", "false");
  const changed = value !== mark.value;
  mark.text = input.value.trim().replace(".", ",");
  if (!changed) return;
  const wasEmpty = mark.value === null;
  mark.value = value;
  if (wasEmpty) mark.included = true;
  if (mark.status !== "manual") mark.status = "edited";
  mark.note = "";
  afterEdit();
}

function confirmAndAdvance(mark) {
  if (mark.status === "check" || mark.status === "doubtful") {
    mark.status = "confirmed";
    mark.note = "";
    mark.included = true;
  }
  afterEdit();
  const next = state.marks.find((item) => item.id !== mark.id && item.status === "check");
  if (next) {
    selectMark(next.id);
    focusValue(next.id);
  }
}

function focusValue(id) {
  const row = document.querySelector(`#ledger-rows tr[data-id="${id}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "nearest" });
  row.querySelector(".value-input").focus();
  row.querySelector(".value-input").select();
}

function afterEdit() {
  renderLedger();
  renderMarkers();
  requestSummary();
}

function setAdding(active) {
  state.adding = active;
  byId("add-mark").setAttribute("aria-pressed", String(active));
  byId("add-hint").hidden = !active;
  byId("sheet-view").classList.toggle("adding", active);
}

function addMarkAt(clientX, clientY) {
  const box = byId("sheet-view").getBoundingClientRect();
  const page = currentPage();
  const x = (clientX - box.left - state.view.x) / state.view.scale;
  const y = (clientY - box.top - state.view.y) / state.view.scale;
  if (x < 0 || y < 0 || x > page.width || y > page.height) {
    showToast("Отметка должна быть внутри листа.", true);
    return;
  }
  const id = state.marks.reduce((largest, mark) => Math.max(largest, mark.id), 0) + 1;
  state.marks.push({
    id, value: null, text: "", page: page.number, x, y, width: 0, height: 0,
    confidence: 1, source: "manual", status: "manual", note: "", included: false, raw: "",
  });
  setAdding(false);
  setFilter("all");
  state.selectedId = id;
  renderMarkers();
  // Браузер переводит фокус на область листа после отпускания кнопки, поэтому ждем конца клика.
  setTimeout(() => focusValue(id), 0);
}

function reviewBody() {
  const marks = state.marks
    .filter((mark) => mark.value !== null)
    .map(({ id, value, page, x, y, height, confidence, status, included }) => ({ id, value, page, x, y, height, confidence, status, included }));
  const dpi = Number(byId("dpi-input").value.replace(",", "."));
  return { marks, metadata: state.metadata, dpi: dpi > 0 && dpi <= 4800 ? dpi : null };
}

function requestSummary(delay = 350) {
  clearTimeout(state.summaryTimer);
  state.summaryTimer = setTimeout(loadSummary, delay);
}

async function loadSummary() {
  if (state.summaryRequest) state.summaryRequest.abort();
  const controller = new AbortController();
  state.summaryRequest = controller;
  try {
    const response = await fetch(`/api/analyses/${state.analysis.id}/summary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reviewBody()),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    renderSummary(await response.json());
  } catch (error) {
    if (error.name !== "AbortError") showToast(error.message, true);
  }
}

function renderSummary({ stats, slope, map }) {
  const text = (value) => (value === undefined ? "—" : formatValue(value));
  byId("stat-min").textContent = text(stats?.minimum);
  byId("stat-max").textContent = text(stats?.maximum);
  byId("stat-spread").textContent = stats ? `${formatValue(stats.spread)} м` : "—";
  byId("stat-mean").textContent = text(stats?.mean);
  const slopeCell = byId("stat-slope");
  if (slope) {
    slopeCell.textContent = slope.direction
      ? `${formatValue(slope.mean_percent)} %, понижение на ${slope.direction}`
      : "рельеф ровный";
    slopeCell.title = `Наибольший между соседними точками: ${formatValue(slope.max_percent)} %. Север считается верхом листа.`;
  } else {
    const rectified = currentPage().rectified;
    slopeCell.textContent = rectified ? "не считается для фото" : "укажите масштаб и dpi";
    slopeCell.title = rectified
      ? "После выравнивания фото масштаб неточен, поэтому уклон не рассчитывается."
      : "Для расчета нужны масштаб плана и разрешение листа.";
  }
  const image = byId("map-image");
  image.hidden = !map;
  byId("map-empty").hidden = Boolean(map);
  if (map) image.src = `data:image/png;base64,${map}`;
}

function showTab(tab) {
  const isSheet = tab === "sheet";
  byId("tab-sheet").setAttribute("aria-selected", String(isSheet));
  byId("tab-map").setAttribute("aria-selected", String(!isSheet));
  byId("sheet-view").hidden = !isSheet;
  byId("sheet-tools").hidden = !isSheet;
  byId("map-view").hidden = isSheet;
  if (isSheet) requestAnimationFrame(applyView);
}

async function download(format) {
  const button = byId(`download-${format}`);
  button.disabled = true;
  try {
    const response = await fetch(`/api/analyses/${state.analysis.id}/report?format=${format}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reviewBody()),
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    const disposition = response.headers.get("content-disposition") || "";
    const encoded = disposition.match(/filename\*=UTF-8''(.+)$/);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = encoded ? decodeURIComponent(encoded[1]) : `Отчет.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    showToast(`Отчет ${format.toUpperCase()} скачан.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function bindSheet() {
  const view = byId("sheet-view");
  let pan = null;
  view.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 1.18 : 1 / 1.18, event.clientX, event.clientY);
  }, { passive: false });
  view.addEventListener("pointerdown", (event) => {
    pan = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, moved: false };
    view.setPointerCapture(event.pointerId);
    view.classList.add("panning");
  });
  view.addEventListener("pointermove", (event) => {
    if (!pan) return;
    state.view.x += event.clientX - pan.x;
    state.view.y += event.clientY - pan.y;
    pan.x = event.clientX;
    pan.y = event.clientY;
    pan.moved = pan.moved || Math.hypot(event.clientX - pan.startX, event.clientY - pan.startY) > 4;
    if (pan.moved) applyView();
  });
  const finish = (event) => {
    if (!pan) return;
    const wasClick = !pan.moved;
    pan = null;
    view.classList.remove("panning");
    if (wasClick && state.adding) addMarkAt(event.clientX, event.clientY);
  };
  view.addEventListener("pointerup", finish);
  view.addEventListener("pointercancel", () => { pan = null; view.classList.remove("panning"); });
  new ResizeObserver(() => { if (state.analysis && !view.hidden) fitSheet(); }).observe(view);
}

function bindStart() {
  const zone = byId("dropzone");
  byId("file-input").addEventListener("change", (event) => {
    const [file] = event.target.files;
    event.target.value = "";
    if (file) uploadFile(file);
  });
  byId("sample-button").addEventListener("click", () => {
    startAnalysis(() => fetch("/api/analyses/sample", { method: "POST" }), "Открываю тестовый лист");
  });
  ["dragenter", "dragover"].forEach((name) => {
    document.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("dragging"); });
  });
  ["dragleave", "drop"].forEach((name) => {
    document.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("dragging"); });
  });
  document.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file && !byId("start").hidden) uploadFile(file);
  });
}

function bindWorkspace() {
  byId("tab-sheet").addEventListener("click", () => showTab("sheet"));
  byId("tab-map").addEventListener("click", () => showTab("map"));
  byId("zoom-in").addEventListener("click", () => zoomAt(1.4));
  byId("zoom-out").addEventListener("click", () => zoomAt(1 / 1.4));
  byId("zoom-fit").addEventListener("click", fitSheet);
  byId("add-mark").addEventListener("click", () => setAdding(!state.adding));
  byId("download-docx").addEventListener("click", () => download("docx"));
  byId("download-pdf").addEventListener("click", () => download("pdf"));
  byId("new-file").addEventListener("click", () => showScreen("start"));
  byId("dpi-input").addEventListener("input", () => requestSummary());
  byId("filters").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button) setFilter(button.dataset.filter);
  });
  document.querySelectorAll("[data-meta]").forEach((input) => {
    input.addEventListener("input", () => {
      state.metadata[input.dataset.meta] = input.value;
      requestSummary();
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.adding) setAdding(false);
  });
}

bindStart();
bindSheet();
bindWorkspace();
