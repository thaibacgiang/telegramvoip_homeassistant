"use strict";

const TOKEN_KEY = "telegram_voip_token";
const THEME_KEY = "telegram_voip_theme";
const BASE = window.location.pathname.endsWith("/")
  ? window.location.pathname
  : window.location.pathname + "/";
const BOOTSTRAP_TOKEN = typeof window.__TGVOIP_TOKEN__ === "string"
  ? window.__TGVOIP_TOKEN__.trim()
  : "";

const VOICE_LABELS = {
  "vi-VN-HoaiMyNeural": "Hoài My – Tiếng Việt chuẩn (nữ)",
  "vi-VN-NamMinhNeural": "Nam Minh – Tiếng Việt (nam, thay thế)",
  "en-US-AriaNeural": "Aria – English US (nữ)",
  "en-US-GuyNeural": "Guy – English US (nam)",
  "en-GB-SoniaNeural": "Sonia – English UK (nữ)",
};

const state = {
  token: BOOTSTRAP_TOKEN || localStorage.getItem(TOKEN_KEY) || "",
  autoAuth: Boolean(BOOTSTRAP_TOKEN),
  config: null,
  status: null,
  view: "overview",
  loginId: null,
  statusTimer: null,
  logsTimer: null,
  callTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function icon(name) {
  return `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function apiUrl(path) {
  return BASE + String(path).replace(/^\//, "");
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const response = await fetch(apiUrl(path), {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: AbortSignal.timeout(options.timeout || 30000),
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.status === 401) {
    if (state.token) {
      lock(false);
      toast("Phiên bảng điều khiển đã hết hiệu lực. Nhập lại API token.", "error");
    }
    throw new ApiError("API token không hợp lệ", 401);
  }
  if (!response.ok) {
    throw new ApiError(payload?.detail || `Lỗi HTTP ${response.status}`, response.status);
  }
  return payload;
}

function toast(message, type = "info") {
  const region = $("#toastRegion");
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  const symbol = type === "success" ? "check" : type === "error" ? "alert" : "phone";
  item.innerHTML = `${icon(symbol)}<p>${escapeHtml(message)}</p>`;
  region.appendChild(item);
  window.setTimeout(() => {
    item.style.opacity = "0";
    item.style.transform = "translateY(4px)";
    window.setTimeout(() => item.remove(), 220);
  }, type === "error" ? 6500 : 3800);
}

function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    day: "2-digit",
    month: "2-digit",
  }).format(date);
}

function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

const STATUS_LABELS = {
  online: "Online",
  offline: "Offline",
  needs_login: "Cần đăng nhập",
  error: "Lỗi",
  queued: "Đang chờ",
  ringing: "Đang đổ chuông",
  connected: "Đã kết nối",
  completed: "Hoàn tất",
  no_answer: "Không nghe máy",
  declined: "Từ chối",
  busy: "Bận",
  failed: "Thất bại",
  cancelled: "Đã hủy",
  running: "Đang chạy",
  answered: "Đã trả lời",
};

function statusLabel(value) {
  return STATUS_LABELS[value] || value || "Không rõ";
}

function statusClass(value) {
  return String(value || "").replace(/[^a-z_]/g, "");
}

function badge(value) {
  return `<span class="badge ${statusClass(value)}">${escapeHtml(statusLabel(value))}</span>`;
}

function accountName(account) {
  const me = account?.me;
  if (!me) return account?.name || "Tài khoản";
  const full = [me.first_name, me.last_name].filter(Boolean).join(" ").trim();
  return full || (me.username ? `@${me.username}` : account.name);
}

function accountInitial(account) {
  return (accountName(account).replace(/^@/, "").trim()[0] || "T").toUpperCase();
}

function selectedAccountId(select) {
  if (!select) return null;
  if (select.value) return select.value;
  const online = (state.status?.accounts || []).find((item) => item.state === "online");
  return online?.id || null;
}

function showLogin() {
  $("#appView").hidden = true;
  $("#loginView").hidden = false;
  $("#loginError").hidden = true;
  window.setTimeout(() => $("#tokenInput").focus(), 30);
}

function showApp() {
  $("#loginView").hidden = true;
  $("#appView").hidden = false;
  $("#lockButton").hidden = state.autoAuth;
  $("#clearSavedToken").hidden = state.autoAuth;
}

function stopTimers() {
  window.clearInterval(state.statusTimer);
  window.clearInterval(state.logsTimer);
  window.clearInterval(state.callTimer);
  state.statusTimer = null;
  state.logsTimer = null;
  state.callTimer = null;
}

function lock(showMessage = true) {
  stopTimers();
  state.token = "";
  state.config = null;
  state.status = null;
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
  if (showMessage) {
    toast("Đã khóa bảng điều khiển trên thiết bị này.", "success");
  }
}

async function login(token) {
  state.token = token.trim();
  const config = await api("api/config");
  const status = await api("api/status");
  state.config = config;
  state.status = status;
  if (state.autoAuth) {
    localStorage.removeItem(TOKEN_KEY);
  } else {
    localStorage.setItem(TOKEN_KEY, state.token);
  }
  showApp();
  populateVoices();
  renderAll();
  startTimers();
}

function startTimers() {
  stopTimers();
  state.statusTimer = window.setInterval(() => refreshStatus(true), 5000);
  state.logsTimer = window.setInterval(() => {
    if (state.view === "logs") refreshLogs(true);
  }, 8000);
  state.callTimer = window.setInterval(() => {
    if (state.view === "calls") refreshStatus(true);
  }, 2000);
}

async function refreshStatus(silent = false) {
  try {
    const status = await api("api/status");
    state.status = status;
    renderHeader();
    renderOverview();
    renderAccounts();
    renderAccountSelects();
    renderCalls();
    renderSettings();
    if (!silent) toast("Đã làm mới dữ liệu.", "success");
  } catch (error) {
    if (error.status !== 401) {
      setConnection(false, error.message || "Mất kết nối");
      if (!silent) toast(error.message, "error");
    }
  }
}

function setConnection(ok, text) {
  const dot = $("#connectionDot");
  dot.classList.toggle("ok", Boolean(ok));
  dot.classList.toggle("bad", !ok);
  $("#connectionText").textContent = text;
}

function renderHeader() {
  const accounts = state.status?.accounts || [];
  const online = accounts.filter((item) => item.state === "online").length;
  if (online > 0) {
    setConnection(true, `${online} tài khoản online`);
  } else {
    setConnection(false, "Chưa có tài khoản online");
  }
}

function renderOverview() {
  const status = state.status;
  if (!status) return;
  const accounts = status.accounts || [];
  const online = accounts.filter((item) => item.state === "online").length;
  $("#statAccounts").textContent = `${online}/${accounts.length}`;
  $("#statAccountsHint").textContent = accounts.length
    ? online
      ? "Sẵn sàng gửi và gọi"
      : "Kiểm tra phiên đăng nhập"
    : "Chưa có tài khoản";
  $("#statCalls").textContent = String(status.active_calls || 0);
  $("#statCallsHint").textContent = status.active_calls ? "Đang đổ chuông hoặc đàm thoại" : "Không có cuộc gọi";
  $("#statMessages").textContent = String(status.stats?.messages_sent || 0);
  $("#statVoices").textContent = String(status.stats?.voices_sent || 0);

  const accountBox = $("#overviewAccounts");
  if (!accounts.length) {
    accountBox.innerHTML = `<div class="empty-inline">Chưa có tài khoản. <button class="btn btn-ghost btn-sm" type="button" data-goto="accounts">Thêm ngay</button></div>`;
  } else {
    accountBox.innerHTML = accounts.slice(0, 4).map((account) => `
      <div class="account-row">
        <span class="avatar">${escapeHtml(accountInitial(account))}</span>
        <span class="account-row-main"><strong>${escapeHtml(account.name)}</strong><small>${escapeHtml(account.phone)} · ${escapeHtml(accountName(account))}</small></span>
        ${badge(account.state)}
      </div>
    `).join("");
  }
  renderEvents($("#overviewEvents"), state.lastEvents || [], 4);
}

function renderEvents(container, events, limit = 20) {
  if (!container) return;
  if (!events.length) {
    container.innerHTML = `<p class="empty-inline">Chưa có hoạt động.</p>`;
    return;
  }
  const icons = {
    account: "users",
    login: "check",
    message: "send",
    voice: "phone",
    call: "phone",
    ring: "bell",
  };
  container.innerHTML = events.slice(0, limit).map((event) => `
    <div class="event-item">
      <span class="event-icon ${event.level === "error" ? "error" : ""}">${icon(event.level === "error" ? "alert" : (icons[event.kind] || "list"))}</span>
      <div class="event-body">
        <span class="event-kind">${escapeHtml(event.kind || "event")}</span>
        <strong>${escapeHtml(event.message)}</strong>
        <time datetime="${escapeHtml(event.ts)}">${escapeHtml(fmtTime(event.ts))}</time>
      </div>
    </div>
  `).join("");
}

function renderAccounts() {
  const accounts = state.status?.accounts || [];
  const grid = $("#accountGrid");
  const empty = $("#accountsEmpty");
  if (!accounts.length) {
    grid.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  grid.innerHTML = accounts.map((account) => {
    const me = account.me || {};
    const username = me.username ? `@${me.username}` : "";
    const userId = me.id ? `ID ${me.id}` : "";
    const details = [account.phone, username, userId].filter(Boolean).join(" · ");
    return `
      <article class="account-card">
        <div class="account-top">
          <span class="avatar">${escapeHtml(accountInitial(account))}</span>
          <span class="account-title"><strong>${escapeHtml(account.name)}</strong><small>${escapeHtml(details)}</small></span>
          ${badge(account.state)}
        </div>
        <div class="account-meta">
          <span>Bắt đầu: ${escapeHtml(account.started_at ? fmtTime(account.started_at) : "—")}</span>
          ${account.last_error ? `<span>Lỗi: ${escapeHtml(account.last_error)}</span>` : ""}
        </div>
        <div class="account-actions">
          <button class="btn btn-ghost btn-sm" type="button" data-reconnect="${escapeHtml(account.id)}">${icon("refresh")}Kết nối lại</button>
          <button class="btn btn-ghost btn-sm" type="button" data-logout="${escapeHtml(account.id)}">${icon("logout")}Đăng xuất</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderAccountSelects() {
  const accounts = state.status?.accounts || [];
  const online = accounts.filter((account) => account.state === "online");
  ["#sendAccount", "#callAccount"].forEach((selector) => {
    const select = $(selector);
    if (!select) return;
    const previous = select.value;
    const options = online.map((account) => {
      const label = `${account.name} · ${account.phone}`;
      return `<option value="${escapeHtml(account.id)}">${escapeHtml(label)}</option>`;
    }).join("");
    select.innerHTML = `<option value="">Tự động (online đầu tiên)</option>${options}`;
    if (previous && online.some((account) => account.id === previous)) {
      select.value = previous;
    }
    select.disabled = online.length === 0;
  });
}

function populateVoices() {
  const voices = state.config?.voices || [];
  ["#sendVoice", "#callVoice"].forEach((selector) => {
    const select = $(selector);
    if (!select) return;
    const previous = select.value;
    select.innerHTML = voices.map((voice) => {
      const label = VOICE_LABELS[voice] || voice;
      return `<option value="${escapeHtml(voice)}" title="${escapeHtml(voice)}">${escapeHtml(label)}</option>`;
    }).join("");
    const preferred = previous || state.config?.defaults?.voice;
    if (preferred && voices.includes(preferred)) select.value = preferred;
  });
  const rate = state.config?.defaults?.rate || "+0%";
  if ($("#sendRate") && !$("#sendRate").value) $("#sendRate").value = rate;
  if ($("#callRate") && !$("#callRate").value) $("#callRate").value = rate;
  if ($("#callAttempts")) $("#callAttempts").value = state.config?.defaults?.ring_attempts ?? 2;
  if ($("#callInterval")) $("#callInterval").value = state.config?.defaults?.ring_interval ?? 15;
  if ($("#callTimeout")) $("#callTimeout").value = state.config?.defaults?.ring_timeout ?? 45;
}

function callItem(call, active) {
  const status = call.status;
  const description = call.kind === "ring" ? "Chuông báo" : "Cuộc gọi";
  const started = call.started_at || call.created_at;
  const action = active
    ? `<button class="btn btn-ghost btn-sm" type="button" data-hangup="${escapeHtml(call.id)}">${icon("x")}Dừng</button>`
    : "";
  return `
    <div class="call-item">
      <span class="event-icon ${status === "failed" || status === "declined" ? "error" : ""}">${icon(call.kind === "ring" ? "bell" : "phone")}</span>
      <div class="call-main">
        <strong>${escapeHtml(call.peer_name || call.peer_id)}</strong>
        <small>${description} · ${escapeHtml(fmtTime(started))}${call.error ? ` · ${escapeHtml(call.error)}` : ""}</small>
      </div>
      ${badge(status)}
      <div class="call-actions">${action}</div>
    </div>
  `;
}

function renderCalls() {
  const status = state.status;
  if (!status) return;
  const calls = status.calls || [];
  const active = calls.filter((call) => ["queued", "ringing", "connected"].includes(call.status));
  const history = calls.filter((call) => !["queued", "ringing", "connected"].includes(call.status));
  const activeBox = $("#activeCalls");
  const historyBox = $("#callHistory");
  if (activeBox) {
    activeBox.innerHTML = active.length
      ? active.map((call) => callItem(call, true)).join("")
      : `<p class="empty-inline">Không có cuộc gọi đang hoạt động.</p>`;
  }
  if (historyBox) {
    historyBox.innerHTML = history.length
      ? history.slice(0, 10).map((call) => callItem(call, false)).join("")
      : `<p class="empty-inline">Chưa có lịch sử cuộc gọi.</p>`;
  }
}

function renderSettings() {
  if (!state.config) return;
  const defaults = state.config.defaults || {};
  const tts = state.status?.tts || {};
  $("#settingsVersion").textContent = state.config.version || "—";
  $("#settingsVoice").textContent = defaults.voice || "—";
  $("#settingsRate").textContent = defaults.rate || "—";
  $("#settingsTimeout").textContent = `${defaults.ring_timeout ?? "—"} giây`;
  $("#settingsAttempts").textContent = String(defaults.ring_attempts ?? "—");
  $("#settingsInterval").textContent = `${defaults.ring_interval ?? "—"} giây`;
  $("#settingsCache").textContent = `${tts.files || 0} tệp · ${fmtBytes(tts.bytes)}`;
  $("#settingsApi").textContent = state.config.api_configured ? "Đã cấu hình" : "Chưa cấu hình";
  $("#settingsApiId").value = state.config.api_id || "";
  $("#settingsApiHashState").textContent = state.config.api_hash_configured ? "có" : "chưa";
  $("#settingsToken").value = state.token;
}

function showView(name) {
  state.view = name;
  $$(".view").forEach((view) => {
    view.hidden = view.id !== `view-${name}`;
  });
  $$(".navbtn").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  if (name === "logs") refreshLogs();
  if (name === "calls") refreshStatus(true);
  if (name === "settings") renderSettings();
  if (name === "accounts") renderAccounts();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function refreshLogs(silent = false) {
  try {
    const kind = $("#logKind")?.value || "";
    const query = `api/logs?limit=150${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`;
    const data = await api(query);
    const events = data.events || [];
    state.lastEvents = events;
    const rows = $("#logRows");
    const empty = $("#logsEmpty");
    if (!events.length) {
      rows.innerHTML = "";
      empty.hidden = false;
    } else {
      empty.hidden = true;
      rows.innerHTML = events.map((event) => `
        <tr>
          <td><time datetime="${escapeHtml(event.ts)}">${escapeHtml(fmtTime(event.ts))}</time></td>
          <td>${escapeHtml(event.kind || "event")}</td>
          <td>${escapeHtml(event.message)}</td>
        </tr>
      `).join("");
    }
    if (!silent) renderOverview();
  } catch (error) {
    if (error.status !== 401 && !silent) toast(error.message, "error");
  }
}

async function refreshEvents() {
  try {
    const data = await api("api/logs?limit=8");
    state.lastEvents = data.events || [];
    renderOverview();
  } catch {
    // Keep the last known events when a background poll fails.
  }
}

async function loadDialogs(button) {
  const account = selectedAccountId($("#sendAccount"));
  if (!account) {
    toast("Chọn hoặc thêm một tài khoản online trước.", "error");
    return;
  }
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Đang tải...";
  try {
    const data = await api(`api/accounts/${encodeURIComponent(account)}/dialogs?limit=100`);
    const dialogs = data.dialogs || [];
    $("#recipientOptions").innerHTML = dialogs.map((item) => {
      const hint = item.username ? `@${item.username}` : item.name;
      return `<option value="${escapeHtml(item.username ? `@${item.username}` : String(item.id))}">${escapeHtml(hint)}</option>`;
    }).join("");
    toast(`Đã tải ${dialogs.length} người liên hệ.`, "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function openAccountDialog() {
  $("#accountForm").reset();
  $("#accountStepOne").hidden = false;
  $("#accountStepTwo").hidden = true;
  $("#passwordField").hidden = true;
  $("#accountError").hidden = true;
  $("#accountErrorTwo").hidden = true;
  state.loginId = null;
  const configured = Boolean(state.config?.api_configured);
  $("#apiConfiguredHint").hidden = !configured;
  $("#apiDetails").open = false;
  $("#accountDialog").showModal();
  window.setTimeout(() => $("#accountName").focus(), 40);
}

async function closeAccountDialog() {
  const pending = state.loginId;
  state.loginId = null;
  const dialog = $("#accountDialog");
  if (dialog.open) dialog.close();
  if (pending) {
    try {
      await api(`api/accounts/login/${encodeURIComponent(pending)}`, { method: "DELETE" });
    } catch {
      // The pending login expires automatically on the add-on side.
    }
  }
}

function setButtonBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    if (label) button.textContent = label;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
    }
  }
}

async function startAccountLogin(event) {
  event.preventDefault();
  const errorBox = $("#accountError");
  errorBox.hidden = true;
  const name = $("#accountName").value.trim();
  const phone = $("#accountPhone").value.trim();
  if (!name || !phone) {
    errorBox.textContent = "Nhập tên gợi nhớ và số điện thoại.";
    errorBox.hidden = false;
    return;
  }
  const apiId = $("#accountApiId").value.trim();
  const apiHash = $("#accountApiHash").value.trim();
  const button = $("#sendCodeButton");
  setButtonBusy(button, true, "Đang gửi mã...");
  try {
    const data = await api("api/accounts/login", {
      method: "POST",
      body: {
        name,
        phone,
        api_id: apiId ? Number(apiId) : null,
        api_hash: apiHash || null,
      },
    });
    state.loginId = data.login_id;
    $("#codePhone").textContent = data.phone || phone;
    $("#accountStepOne").hidden = true;
    $("#accountStepTwo").hidden = false;
    $("#accountCode").value = "";
    $("#accountPassword").value = "";
    $("#passwordField").hidden = !data.needs_password;
    window.setTimeout(() => $("#accountCode").focus(), 40);
    toast("Đã gửi mã Telegram. Kiểm tra ứng dụng hoặc SMS.", "success");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    setButtonBusy(button, false);
  }
}

async function verifyAccountLogin(event) {
  event.preventDefault();
  if (!state.loginId) {
    toast("Phiên đăng nhập đã hết hạn. Gửi lại mã.", "error");
    return;
  }
  const errorBox = $("#accountErrorTwo");
  errorBox.hidden = true;
  const code = $("#accountCode").value.trim();
  const password = $("#accountPassword").value;
  const button = $("#verifyButton");
  setButtonBusy(button, true, "Đang xác nhận...");
  try {
    const data = await api(`api/accounts/login/${encodeURIComponent(state.loginId)}/verify`, {
      method: "POST",
      body: { code: code || null, password: password || null },
    });
    if (data.needs_password) {
      $("#passwordField").hidden = false;
      $("#accountPassword").focus();
      toast("Tài khoản có 2FA. Nhập mật khẩu xác thực hai lớp.", "info");
      return;
    }
    state.loginId = null;
    $("#accountDialog").close();
    toast(`Đã thêm tài khoản ${data.account?.name || ""}.`, "success");
    await refreshStatus(true);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    setButtonBusy(button, false);
  }
}

async function reconnectAccount(id) {
  try {
    await api(`api/accounts/${encodeURIComponent(id)}/reconnect`, { method: "POST" });
    toast("Đã kết nối lại tài khoản.", "success");
    await refreshStatus(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function logoutAccount(id) {
  if (!window.confirm("Đăng xuất và xóa phiên Telegram này khỏi add-on?")) return;
  try {
    await api(`api/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Đã xóa tài khoản.", "success");
    await refreshStatus(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function submitCredentials(event) {
  event.preventDefault();
  const errorBox = $("#credentialsError");
  errorBox.hidden = true;
  const body = {};
  const apiId = $("#settingsApiId").value.trim();
  const apiHash = $("#settingsApiHash").value.trim();
  const token = $("#settingsNewToken").value.trim();
  if (apiId) {
    if (!/^\d+$/.test(apiId)) {
      errorBox.textContent = "api_id phải là số.";
      errorBox.hidden = false;
      return;
    }
    body.api_id = Number(apiId);
  }
  if (apiHash) {
    if (apiHash.length < 16) {
      errorBox.textContent = "api_hash quá ngắn.";
      errorBox.hidden = false;
      return;
    }
    body.api_hash = apiHash;
  }
  if (token) {
    if (token.length < 16) {
      errorBox.textContent = "API token phải có ít nhất 16 ký tự.";
      errorBox.hidden = false;
      return;
    }
    body.api_token = token;
  }
  if (!Object.keys(body).length) {
    toast("Không có thông tin mới để lưu.", "info");
    return;
  }
  const button = $("#saveCredentialsButton");
  setButtonBusy(button, true, "Đang lưu...");
  try {
    const result = await api("api/settings/credentials", { method: "POST", body });
    state.token = result.api_token;
    state.config.api_id = result.api_id;
    state.config.api_hash_configured = result.api_hash_configured;
    state.config.api_configured = Boolean(result.api_id && result.api_hash_configured);
    if (!state.autoAuth) localStorage.setItem(TOKEN_KEY, state.token);
    $("#settingsApiHash").value = "";
    $("#settingsNewToken").value = "";
    renderSettings();
    toast("Đã lưu thông tin Telegram.", "success");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    setButtonBusy(button, false);
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(new Error("Không đọc được file ảnh.")));
    reader.readAsDataURL(file);
  });
}

async function submitSend(event) {
  event.preventDefault();
  const mode = $('input[name="mode"]:checked')?.value || "message";
  const account = $("#sendAccount").value || null;
  const to = $("#sendTo").value.trim();
  const button = $("#sendButton");
  if (!to) {
    toast("Nhập người nhận.", "error");
    return;
  }

  if (mode === "photo") {
    const file = $("#sendPhotoFile").files[0];
    let image = $("#sendPhotoUrl").value.trim();
    if (file && file.size > 8 * 1024 * 1024) {
      toast("Ảnh phải nhỏ hơn 8 MB.", "error");
      return;
    }
    if (file) {
      try {
        image = await readFileAsDataUrl(file);
      } catch (error) {
        toast(error.message, "error");
        return;
      }
    }
    if (!image) {
      toast("Nhập URL ảnh hoặc chọn file ảnh.", "error");
      return;
    }
    setButtonBusy(button, true, "Đang gửi ảnh...");
    try {
      await api("api/photos", {
        method: "POST",
        body: {
          account,
          to,
          image,
          caption: $("#sendPhotoCaption").value.trim() || null,
        },
        timeout: 120000,
      });
      $("#sendPhotoUrl").value = "";
      $("#sendPhotoFile").value = "";
      $("#sendPhotoCaption").value = "";
      toast("Đã gửi ảnh.", "success");
      await refreshStatus(true);
      refreshEvents();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setButtonBusy(button, false);
    }
    return;
  }

  const body = {
    account,
    to,
    message: $("#sendText").value.trim(),
  };
  if (!body.message) {
    toast("Nhập nội dung.", "error");
    return;
  }
  const label = mode === "tts" ? "Đang tạo voice..." : "Đang gửi...";
  setButtonBusy(button, true, label);
  try {
    if (mode === "tts") {
      body.voice = $("#sendVoice").value || null;
      body.rate = $("#sendRate").value || null;
      body.caption = $("#sendCaption").value.trim() || null;
      const result = await api("api/tts", { method: "POST", body });
      toast(`Đã gửi voice TTS (${result.voice || body.voice || "giọng mặc định"}).`, "success");
    } else {
      await api("api/messages", { method: "POST", body });
      toast("Đã gửi tin nhắn.", "success");
    }
    $("#sendText").value = "";
    updateCharCount();
    await refreshStatus(true);
    refreshEvents();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function ringPayload() {
  return {
    account: $("#callAccount").value || null,
    to: $("#callTo").value.trim(),
    message: $("#callText").value.trim(),
    voice: $("#callVoice").value || null,
    rate: $("#callRate").value || null,
    attempts: Number($("#callAttempts").value) || null,
    interval: Number($("#callInterval").value) || 0,
    ring_timeout: Number($("#callTimeout").value) || null,
    send_message: $("#callSendMessage").checked,
  };
}

async function submitRing(event) {
  event.preventDefault();
  const body = ringPayload();
  if (!body.to || !body.message) {
    toast("Nhập người nhận và lời thoại.", "error");
    return;
  }
  const button = $("#ringButton");
  setButtonBusy(button, true, "Đang chuẩn bị...");
  try {
    const ring = await api("api/rings", { method: "POST", body });
    toast(`Đã bắt đầu chuông báo (${ring.attempts} lần).`, "success");
    await refreshStatus(true);
    showView("calls");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function submitOneCall() {
  const body = ringPayload();
  body.wait = false;
  body.attempts = undefined;
  body.interval = undefined;
  body.send_message = undefined;
  if (!body.to || !body.message) {
    toast("Nhập người nhận và lời thoại.", "error");
    return;
  }
  const button = $("#oneCallButton");
  setButtonBusy(button, true, "Đang gọi...");
  try {
    await api("api/calls", { method: "POST", body });
    toast("Đang gọi. Khi người nhận nghe máy, voice TTS sẽ phát.", "success");
    await refreshStatus(true);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function hangupCall(id) {
  try {
    await api(`api/calls/${encodeURIComponent(id)}/hangup`, { method: "POST" });
    toast("Đã dừng cuộc gọi.", "success");
    await refreshStatus(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateCharCount() {
  const count = $("#sendCount");
  if (count) count.textContent = String($("#sendText").value.length);
}

function toggleVoiceFields() {
  const mode = $('input[name="mode"]:checked')?.value || "message";
  $("#voiceFields").hidden = mode !== "tts";
  $("#photoFields").hidden = mode !== "photo";
  $("#sendTextField").hidden = mode === "photo";
  $("#sendText").required = mode !== "photo";
  $("#sendButton span").textContent = mode === "tts" ? "Gửi voice" : (
    mode === "photo" ? "Gửi ảnh" : "Gửi ngay"
  );
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
  localStorage.setItem(THEME_KEY, theme);
  const button = $("#themeButton");
  if (button) {
    button.setAttribute("aria-label", theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối");
  }
}

function cycleTheme() {
  const current = localStorage.getItem(THEME_KEY) || "auto";
  const next = current === "auto" ? "light" : current === "light" ? "dark" : "auto";
  applyTheme(next);
  toast(next === "auto" ? "Giao diện theo hệ thống." : `Giao diện ${next === "dark" ? "tối" : "sáng"}.`, "info");
}

async function copyTarget(id) {
  const element = document.getElementById(id);
  const value = element instanceof HTMLInputElement ? element.value : element.textContent;
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast("Đã sao chép.", "success");
  } catch {
    const temp = document.createElement("textarea");
    temp.value = value;
    document.body.appendChild(temp);
    temp.select();
    document.execCommand("copy");
    temp.remove();
    toast("Đã sao chép.", "success");
  }
}

function renderAll() {
  renderHeader();
  renderOverview();
  renderAccounts();
  renderAccountSelects();
  renderCalls();
  renderSettings();
  refreshEvents();
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = $("#loginError");
    errorBox.hidden = true;
    const token = $("#tokenInput").value.trim();
    if (!token) return;
    const button = $("#loginForm button[type=submit]");
    setButtonBusy(button, true, "Đang kiểm tra...");
    try {
      await login(token);
      toast("Đã kết nối add-on.", "success");
    } catch (error) {
      errorBox.textContent = error.status === 401
        ? "API token không đúng. Đọc /data/api_token hoặc cấu hình api_token của add-on."
        : `Không kết nối được: ${error.message}`;
      errorBox.hidden = false;
    } finally {
      setButtonBusy(button, false);
    }
  });

  $("#themeButton").addEventListener("click", cycleTheme);
  $("#lockButton").addEventListener("click", () => lock(true));
  $("#clearSavedToken").addEventListener("click", () => lock(true));
  $("#addAccountButton").addEventListener("click", openAccountDialog);
  $$("[data-add-account]").forEach((button) => button.addEventListener("click", openAccountDialog));
  $$("[data-close-dialog]").forEach((button) => button.addEventListener("click", closeAccountDialog));
  $("#accountDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAccountDialog();
  });
  $("#accountForm").addEventListener("submit", (event) => {
    if (state.loginId) verifyAccountLogin(event);
    else startAccountLogin(event);
  });
  $("#credentialsForm").addEventListener("submit", submitCredentials);

  $$(".navbtn").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$("[data-goto]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.goto)));
  $$("[data-refresh]").forEach((button) => button.addEventListener("click", () => refreshStatus(false)));
  $$("[data-refresh-logs]").forEach((button) => button.addEventListener("click", () => refreshLogs(false)));
  $("#logKind").addEventListener("change", () => refreshLogs());

  $("#accountGrid").addEventListener("click", (event) => {
    const reconnect = event.target.closest("[data-reconnect]");
    if (reconnect) {
      reconnectAccount(reconnect.dataset.reconnect);
      return;
    }
    const logout = event.target.closest("[data-logout]");
    if (logout) logoutAccount(logout.dataset.logout);
  });
  $("#overviewAccounts").addEventListener("click", (event) => {
    const goto = event.target.closest("[data-goto]");
    if (goto) showView(goto.dataset.goto);
  });

  $("#sendText").addEventListener("input", updateCharCount);
  $$('input[name="mode"]').forEach((input) => input.addEventListener("change", toggleVoiceFields));
  $("#sendForm").addEventListener("submit", submitSend);
  $("#loadDialogsButton").addEventListener("click", (event) => loadDialogs(event.currentTarget));
  $("#callForm").addEventListener("submit", submitRing);
  $("#oneCallButton").addEventListener("click", submitOneCall);

  $("#activeCalls").addEventListener("click", (event) => {
    const button = event.target.closest("[data-hangup]");
    if (button) hangupCall(button.dataset.hangup);
  });

  $$("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", () => copyTarget(button.dataset.copyTarget));
  });
  $$("[data-toggle-token]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $("#settingsToken");
      input.type = input.type === "password" ? "text" : "password";
    });
  });
}

async function init() {
  applyTheme(localStorage.getItem(THEME_KEY) || "auto");
  bindEvents();
  toggleVoiceFields();
  updateCharCount();
  const saved = state.token;
  if (!saved) {
    showLogin();
    return;
  }
  try {
    await login(saved);
  } catch (error) {
    state.token = "";
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    $("#loginError").textContent = error.status === 401
      ? "API token đã lưu không còn đúng. Nhập lại token."
      : `Không kết nối được add-on: ${error.message}`;
    $("#loginError").hidden = false;
  }
}

document.addEventListener("DOMContentLoaded", init);
