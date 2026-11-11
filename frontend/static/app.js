const STATUSES = ["todo", "doing", "done"];
const LABEL = { todo: "Todo", doing: "Doing", done: "Done" };
const THEME_KEY = "cdb-theme";
const $ = (sel) => document.querySelector(sel);

const state = { user: null, tasks: [], loading: true, query: "", busy: new Set() };
let pendingDelete = null;
let slowTimer = null;

const byOrder = (a, b) => a.position - b.position || a.id - b.id;

function sortTasks() {
  state.tasks.sort(byOrder);
}

function columnTasks(status) {
  return state.tasks.filter((t) => t.status === status).sort(byOrder);
}

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "network_error", "Không kết nối được máy chủ");
  }
  const body = await res.json().catch(() => null);
  if (res.status === 401 && location.pathname !== "/login") {
    location.replace("/login");
    throw new ApiError(401, "unauthenticated", "Phiên đăng nhập đã hết");
  }
  if (!res.ok) {
    throw new ApiError(res.status, body && body.code, (body && body.error) || "Lỗi " + res.status);
  }
  return body;
}

function toast(message, kind = "info") {
  const box = $("#toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  box.append(el);
  setTimeout(() => {
    el.classList.add("hide");
    setTimeout(() => el.remove(), 200);
  }, 3600);
}

function beginLoad() {
  clearTimeout(slowTimer);
  slowTimer = setTimeout(() => {
    const banner = $("#slow");
    if (banner) banner.hidden = false;
  }, 2000);
}

function endLoad() {
  clearTimeout(slowTimer);
  const banner = $("#slow");
  if (banner) banner.hidden = true;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

let dragState = null;
let dropSlot = null;

function clearDropSlot() {
  if (dropSlot) {
    dropSlot.remove();
    dropSlot = null;
  }
  document.querySelectorAll(".col.over").forEach((c) => c.classList.remove("over"));
}

function showDropSlot(list, cards, index, height) {
  if (!dropSlot) {
    dropSlot = el("div", "drop-slot");
    dropSlot.setAttribute("aria-hidden", "true");
    dropSlot.style.height = "0px";
  }
  const ref = cards[index] || null;
  if (dropSlot.parentElement !== list || dropSlot.nextElementSibling !== ref) {
    list.insertBefore(dropSlot, ref);
  }
  dropSlot.dataset.index = String(index);
  if (!dropSlot.classList.contains("on")) {
    void dropSlot.offsetHeight;
    dropSlot.classList.add("on");
    dropSlot.style.height = height + "px";
  }
}

function dragIndex(list, event) {
  const cards = [...list.querySelectorAll(".task")]
    .filter((c) => !dragState || c !== dragState.node);
  let index = cards.length;
  for (let i = 0; i < cards.length; i += 1) {
    const rect = cards[i].getBoundingClientRect();
    if (event.clientY < rect.top + rect.height / 2) {
      index = i;
      break;
    }
  }
  return { cards, index };
}

function emptyState(status) {
  const box = el("div", "empty");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "46");
  svg.setAttribute("height", "34");
  svg.setAttribute("viewBox", "0 0 46 34");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML =
    '<rect x="1" y="1" width="44" height="32" rx="6" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<path d="M9 12h20M9 18h13" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>';
  box.append(svg, el("span", null, state.query ? "Không có việc khớp từ khoá" : "Chưa có việc ở cột " + LABEL[status]));
  return box;
}

function card(task, position, total) {
  const busy = state.busy.has(task.id);
  const node = el("article", "task" + (busy ? " busy" : ""));
  node.dataset.id = String(task.id);
  node.setAttribute("draggable", "true");

  const title = el("button", "task-title", task.title);
  title.type = "button";
  title.setAttribute("aria-label", "Sửa tiêu đề: " + task.title);
  title.addEventListener("dblclick", () => startEdit(task, title));

  const meta = el("div", "task-meta");
  const statusChip = el("span", "task-status", LABEL[task.status]);
  statusChip.dataset.status = task.status;
  meta.append(statusChip, el("span", "task-id", "#" + task.id));

  const actions = el("div", "task-actions");
  const idx = STATUSES.indexOf(task.status);
  const makeBtn = (label, glyph) => {
    const b = el("button", "btn icon", glyph);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.disabled = busy;
    return b;
  };

  const up = makeBtn("Đưa việc #" + task.id + " lên", "↑");
  up.disabled = busy || Boolean(state.query) || position === 0;
  up.addEventListener("click", () => moveTask(task.id, task.status, position - 1));

  const down = makeBtn("Đưa việc #" + task.id + " xuống", "↓");
  down.disabled = busy || Boolean(state.query) || position === total - 1;
  down.addEventListener("click", () => moveTask(task.id, task.status, position + 1));

  actions.append(up, down);
  if (idx > 0) {
    const left = makeBtn("Chuyển sang " + LABEL[STATUSES[idx - 1]], "←");
    left.addEventListener("click", () => moveTask(task.id, STATUSES[idx - 1]));
    actions.append(left);
  }
  if (idx < STATUSES.length - 1) {
    const right = makeBtn("Chuyển sang " + LABEL[STATUSES[idx + 1]], "→");
    right.addEventListener("click", () => moveTask(task.id, STATUSES[idx + 1]));
    actions.append(right);
  }
  const edit = makeBtn("Sửa tiêu đề #" + task.id, "✎");
  edit.addEventListener("click", () => startEdit(task, title));
  const del = makeBtn("Xoá việc #" + task.id, "✕");
  del.addEventListener("click", () => askDelete(task));
  actions.append(edit, del);

  meta.append(actions);
  node.append(title, meta);

  node.addEventListener("dragstart", (e) => {
    if (busy || state.query) { e.preventDefault(); return; }
    e.dataTransfer.setData("text/plain", String(task.id));
    e.dataTransfer.effectAllowed = "move";
    dragState = { id: task.id, node, height: Math.round(node.getBoundingClientRect().height) };
    node.classList.add("dragging");
  });
  node.addEventListener("dragend", () => {
    node.classList.remove("dragging");
    dragState = null;
    clearDropSlot();
  });
  return node;
}

function render() {
  const q = state.query;
  const visible = state.tasks.filter((t) => !q || t.title.toLowerCase().includes(q));
  for (const status of STATUSES) {
    const list = document.getElementById("list-" + status);
    const items = visible.filter((t) => t.status === status);
    list.textContent = "";
    document.getElementById("count-" + status).textContent = String(items.length);
    if (state.loading) {
      for (let i = 0; i < 2; i += 1) list.append(el("div", "skel"));
      continue;
    }
    if (!items.length) {
      list.append(emptyState(status));
      continue;
    }
    items.forEach((task, i) => list.append(card(task, i, items.length)));
  }
}

async function patchTask(id, patch) {
  const task = state.tasks.find((t) => t.id === id);
  if (!task || state.busy.has(id)) return;
  const before = { ...task };
  Object.assign(task, patch);
  state.busy.add(id);
  render();
  try {
    const row = await api("/api/tasks/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: state.user.id, ...patch }),
    });
    Object.assign(task, row);
  } catch (err) {
    Object.assign(task, before);
    toast(err.message, "error");
  } finally {
    state.busy.delete(id);
    render();
  }
}

function optimisticMove(task, status, position) {
  const oldStatus = task.status;
  const oldColumn = columnTasks(oldStatus).filter((t) => t.id !== task.id);
  const target = status === oldStatus
    ? oldColumn
    : columnTasks(status).filter((t) => t.id !== task.id);
  const index = position === undefined
    ? target.length
    : Math.max(0, Math.min(position, target.length));
  target.splice(index, 0, task);
  task.status = status;
  if (status !== oldStatus) oldColumn.forEach((t, i) => { t.position = i; });
  target.forEach((t, i) => { t.position = i; });
}

async function moveTask(id, status, position) {
  const task = state.tasks.find((t) => t.id === id);
  if (!task || state.busy.has(id)) return;
  const before = state.tasks.map((t) => ({ ...t }));
  optimisticMove(task, status, position);
  state.busy.add(id);
  sortTasks();
  render();
  try {
    const body = { user_id: state.user.id, status };
    if (position !== undefined) body.position = position;
    const row = await api("/api/tasks/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    Object.assign(task, row);
  } catch (err) {
    state.tasks = before;
    toast(err.message, "error");
  } finally {
    state.busy.delete(id);
    sortTasks();
    render();
  }
}

async function addTask(title) {
  const row = await api("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: state.user.id, title }),
  });
  state.tasks.push(row);
  sortTasks();
  render();
  toast("Đã thêm việc", "success");
}

async function removeTask(id) {
  const index = state.tasks.findIndex((t) => t.id === id);
  if (index < 0) return;
  const task = state.tasks[index];
  state.tasks.splice(index, 1);
  render();
  try {
    await api("/api/tasks/" + id + "?user_id=" + encodeURIComponent(state.user.id), { method: "DELETE" });
    toast("Đã xoá việc", "success");
  } catch (err) {
    state.tasks.splice(index, 0, task);
    render();
    toast(err.message, "error");
  }
}

function startEdit(task, titleBtn) {
  if (state.busy.has(task.id)) return;
  const input = el("input", "title-edit");
  input.type = "text";
  input.maxLength = 200;
  input.value = task.title;
  input.setAttribute("aria-label", "Sửa tiêu đề việc #" + task.id);
  titleBtn.replaceWith(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = (save) => {
    if (finished) return;
    finished = true;
    const next = input.value.trim();
    if (!save || !next || next === task.title) {
      render();
      return;
    }
    patchTask(task.id, { title: next });
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

function askDelete(task) {
  pendingDelete = task.id;
  $("#confirm-text").textContent = "#" + task.id + " — " + task.title + " (" + LABEL[task.status] + ")";
  $("#confirm").showModal();
}

async function loadTasks() {
  state.tasks = await api("/api/tasks?user_id=" + encodeURIComponent(state.user.id));
  sortTasks();
}

function initAccountMenu() {
  const toggle = $("#account-toggle");
  const menu = $("#account-menu");
  if (!toggle || !menu) return;

  const close = () => {
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };
  const open = () => {
    menu.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
  };

  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) open();
    else close();
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !toggle.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) {
      close();
      toggle.focus();
    }
  });
}

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "auto") root.removeAttribute("data-theme");
  else root.dataset.theme = mode;
  const btn = $("#theme");
  if (btn) {
    btn.title = "Giao diện: " + (mode === "auto" ? "tự động" : mode === "light" ? "sáng" : "tối");
    btn.setAttribute("aria-label", "Đổi giao diện (hiện tại: " + (mode === "auto" ? "tự động" : mode === "light" ? "sáng" : "tối") + ")");
  }
}

function storedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "auto";
  } catch {
    return "auto";
  }
}

function initTheme() {
  const btn = $("#theme");
  if (!btn) return;
  applyTheme(storedTheme());
  btn.addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(storedTheme()) + 1) % order.length];
    try {
      if (next === "auto") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, next);
    } catch { /* storage bị chặn thì chỉ đổi trong phiên */ }
    applyTheme(next);
  });
}

function initBoard() {
  initTheme();
  initAccountMenu();

  $("#add-open").addEventListener("click", () => {
    $("#new-title").value = "";
    $("#new-title").removeAttribute("aria-invalid");
    $("#composer").showModal();
    $("#new-title").focus();
  });

  $("#composer-cancel").addEventListener("click", () => $("#composer").close());

  $("#composer-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#new-title");
    const title = input.value.trim();
    if (!title) {
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    const submit = $("#add-submit");
    submit.disabled = true;
    try {
      await addTask(title);
      $("#composer").close();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      submit.disabled = false;
    }
  });

  $("#confirm-no").addEventListener("click", () => {
    pendingDelete = null;
    $("#confirm").close();
  });

  $("#confirm-yes").addEventListener("click", () => {
    const id = pendingDelete;
    pendingDelete = null;
    $("#confirm").close();
    if (id !== null) removeTask(id);
  });

  $("#confirm").addEventListener("close", () => { pendingDelete = null; });

  $("#logout").addEventListener("click", async () => {
    const btn = $("#logout");
    btn.disabled = true;
    try {
      await api("/api/logout", { method: "POST" });
    } catch { /* dù lỗi vẫn đưa về trang đăng nhập */ }
    location.replace("/login");
  });

  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    render();
  });

  for (const col of document.querySelectorAll(".col")) {
    col.addEventListener("dragover", (e) => {
      if (!dragState || state.query || state.busy.has(dragState.id)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      col.classList.add("over");
      const list = col.querySelector(".list");
      const { cards, index } = dragIndex(list, e);
      showDropSlot(list, cards, index, dragState.height);
    });
    col.addEventListener("dragleave", (e) => {
      if (col.contains(e.relatedTarget)) return;
      col.classList.remove("over");
      const board = document.getElementById("board");
      if (!board || !board.contains(e.relatedTarget)) clearDropSlot();
    });
    col.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!dragState || state.query) { clearDropSlot(); return; }
      const dragId = dragState.id;
      const task = state.tasks.find((t) => t.id === dragId);
      const status = col.dataset.status;
      const list = col.querySelector(".list");
      let index = dropSlot && dropSlot.parentElement === list
        ? Number(dropSlot.dataset.index)
        : null;
      if (index === null) index = dragIndex(list, e).index;
      clearDropSlot();
      dragState = null;
      if (!task || state.busy.has(dragId)) return;
      if (task.status === status) {
        const current = columnTasks(status).findIndex((t) => t.id === dragId);
        if (current === index) return;
      }
      moveTask(dragId, status, index);
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea" || e.target.isContentEditable) return;
    if (e.key === "/") {
      e.preventDefault();
      $("#search").focus();
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      $("#add-open").click();
    }
  });

  render();
  beginLoad();

  (async () => {
    try {
      const me = await api("/api/me");
      state.user = me.user;
      $("#account-email").textContent = me.user.email;
      $("#avatar").textContent = me.user.email.slice(0, 1);
      await loadTasks();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.loading = false;
      endLoad();
      render();
    }
  })();
}

function initLogin() {
  initTheme();
  const form = $("#login-form");
  const email = $("#login-email");
  const password = $("#login-password");
  const submit = $("#login-submit");
  const error = $("#login-error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const name = email.value.trim();
    if (!name || !password.value) {
      error.textContent = "Nhập đủ email và mật khẩu.";
      error.hidden = false;
      (name ? password : email).focus();
      return;
    }
    submit.disabled = true;
    submit.textContent = "Đang đăng nhập…";
    try {
      await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: name, password: password.value }),
      });
      location.replace("/");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      password.select();
      submit.disabled = false;
      submit.textContent = "Đăng nhập";
    }
  });
}

function initRegister() {
  initTheme();
  const form = $("#register-form");
  const email = $("#register-email");
  const password = $("#register-password");
  const confirm = $("#register-confirm");
  const submit = $("#register-submit");
  const error = $("#register-error");

  const showError = (message, target) => {
    error.textContent = message;
    error.hidden = false;
    (target || email).focus();
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    const name = email.value.trim();
    if (!name) {
      showError("Nhập email.", email);
      return;
    }
    if (!password.value || password.value.length < 8) {
      showError("Mật khẩu phải có ít nhất 8 ký tự.", password);
      return;
    }
    if (password.value !== confirm.value) {
      showError("Mật khẩu nhập lại không khớp.", confirm);
      return;
    }
    submit.disabled = true;
    submit.textContent = "Đang tạo tài khoản…";
    try {
      await api("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: name, password: password.value }),
      });
      location.replace("/");
    } catch (err) {
      showError(err.message, email);
      submit.disabled = false;
      submit.textContent = "Tạo tài khoản";
    }
  });
}

async function initClusterMeta() {
  const badge = $("#cluster-meta");
  const footer = $("#cluster-footer");
  if (!badge && !footer) return;
  const [fe, be] = await Promise.all([
    fetch("/frontend-info.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null),
    fetch("/api/health", { cache: "no-store" })
      .then((r) => r.json().catch(() => null))
      .catch(() => null),
  ]);
  const ver = (be && be.version) || (fe && fe.version) || (badge && badge.dataset.version) || "v1";
  if (badge) {
    badge.dataset.version = ver;
    badge.textContent = ver;
  }
  if (footer) {
    const parts = ["App " + ver];
    if (fe && fe.pod) parts.push("FE: " + fe.pod);
    if (be && be.pod) parts.push("BE: " + be.pod);
    if (be && be.db_user) parts.push("DB: " + be.db_user);
    footer.textContent = parts.join(" · ");
  }
}

initClusterMeta();
if ($("#login-form")) initLogin();
else if ($("#register-form")) initRegister();
else if ($("#board")) initBoard();
