const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  view: "tasks",
  mode: "album",
  filter: "all",
  search: "",
  album: "",
  albumTitle: "",
  // 选中集是「那一刻的 id 快照」而不是筛选条件：删除期间后台新下完的任务
  // 不会被误伤，确认框上的数字也就永远算数。
  picked: new Set(),
  settings: null,
  login: null,
  tasks: [],
  counts: {},
  page: {
    offset: 0,
    limit: 100,
    total: 0,
    has_previous: false,
    has_next: false,
  },
  operation: null,
  taskError: null,
  taskRenderKey: "",
  operationRenderKey: "",
  riskLoaded: false,
  settingsPopulated: false,
  handledTerminals: new Set(),
};

const polling = {
  timer: null,
  running: false,
  searchTimer: null,
  taskRequest: 0,
  lastTaskRefreshAt: 0,
};

const ACTIVE_OPERATION_POLL_MS = 850;
const IDLE_OPERATION_POLL_MS = 4000;
const ACTIVE_TASK_POLL_MS = 1500;
const IDLE_TASK_POLL_MS = 10000;
let riskReportPromise = null;
let apkCaptcha = null;
let apkLoginConfig = null;
let geetestLoader = null;
let apkSmsCooldownTimer = null;
let apkSmsCooldownUntil = 0;
let apkLoginMode = "sms";

const terminalStatuses = new Set(["succeeded", "failed", "stopped"]);
const operationLabels = {
  login: "登录",
  download_track: "单曲下载",
  download_album: "专辑下载",
  resume: "恢复任务",
  formats: "音质探测",
  inspect_storage: "浏览器存储检查",
  gen_sign: "签名冒烟",
  extract_device: "设备信息采集",
  refresh_cookies: "登录凭据刷新",
};

async function api(path, options = {}) {
  const init = {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  };
  const response = await fetch(path, init);
  const text = await response.text();
  let payload = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    const error = new Error(payload.detail || `请求失败（${response.status}）`);
    error.status = response.status;
    error.category = payload.category;
    throw error;
  }
  return payload;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast${type === "error" ? " is-error" : ""}`;
  node.textContent = message;
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 4200);
}

function openLogin() {
  if ((state.settings?.source_backend || "http") !== "apk") {
    startOperation("/api/operations/login");
    return;
  }
  renderApkAccounts();
  setApkLoginMode(apkLoginMode);
  $("#apk-login-dialog").showModal();
}

function renderApkAccounts() {
  const list = $("#apk-account-list");
  if (!list) return;
  const accounts = Array.isArray(state.login?.accounts) ? state.login.accounts : [];
  $("#apk-account-count").textContent = `${accounts.length} 个`;
  if (!accounts.length) {
    list.innerHTML = '<p class="apk-account-empty">暂无账号，完成下方任一登录后会自动保存。</p>';
    return;
  }
  list.innerHTML = accounts.map((account) => `
    <div class="apk-account-row ${account.active ? "is-active" : ""}">
      <div class="apk-account-identity">
        <span class="apk-account-uid">UID ${escapeHtml(account.uid)}</span>
        <span class="apk-account-meta">${account.active ? "当前使用" : "登录态已保存"}</span>
      </div>
      <div class="apk-account-actions">
        ${account.active ? "" : `<button class="button secondary" type="button"
          data-apk-account-action="switch" data-apk-account-uid="${escapeHtml(account.uid)}">切换</button>`}
        <button class="button secondary" type="button"
          data-apk-account-action="delete" data-apk-account-uid="${escapeHtml(account.uid)}">删除</button>
      </div>
    </div>`).join("");
}

async function mutateApkAccount(action, uid) {
  const path = action === "switch"
    ? "/api/apk-auth/switch" : "/api/apk-auth/accounts/delete";
  try {
    const result = await api(path, {
      method: "POST", body: JSON.stringify({ uid }),
    });
    state.login = { ...state.login, ...result };
    renderApkAccounts();
    renderHeader();
    toast(action === "switch"
      ? `已切换到 APK 账号 UID ${uid}，可点击“恢复全部”继续`
      : `已删除 APK 账号 UID ${uid}`);
    await loadBootstrap(false);
  } catch (error) {
    toast(error.message, "error");
  }
}

function confirmDeleteApkAccount(uid) {
  openDialog({
    title: `删除 APK 账号 UID ${uid}？`,
    sub: "只会删除本机保存的该账号登录态。",
    lines: [
      '<div class="line warn"><i></i><span>删除后如需再次使用该账号，必须重新登录。</span></div>',
    ],
    confirmText: "删除账号",
    safeNote: "其他 APK 账号、浏览器登录态和下载记录不会受到影响。",
    onConfirm: () => mutateApkAccount("delete", uid),
  });
}

function setApkLoginMode(mode) {
  apkLoginMode = ["sms", "mobile", "email"].includes(mode) ? mode : "sms";
  $$('[data-apk-login-mode]').forEach((button) => {
    const active = button.dataset.apkLoginMode === apkLoginMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $('[data-apk-login-panel="sms"]').classList.toggle("is-hidden", apkLoginMode !== "sms");
  $('[data-apk-login-panel="password"]').classList.toggle("is-hidden", apkLoginMode === "sms");
  $("#apk-submit-sms").classList.toggle("is-hidden", apkLoginMode !== "sms");
  if (apkLoginMode === "sms") {
    setApkLoginMessage("输入手机号后获取短信验证码。", "info");
    return;
  }
  const email = apkLoginMode === "email";
  $("#apk-account-label").textContent = email ? "邮箱" : "手机号";
  $("#apk-account").placeholder = email ? "请输入邮箱" : "请输入手机号";
  $("#apk-account").inputMode = email ? "email" : "tel";
  setApkLoginMessage(`输入${email ? "邮箱" : "手机号"}和密码后完成安全验证。`, "info");
}

function requireApkLogin() {
  if ((state.settings?.source_backend || "http") !== "apk"
      || state.login?.authenticated) return true;
  openLogin();
  setApkLoginMessage("请先登录 APK 协议账号，再开始下载或恢复任务。", "warning");
  return false;
}

function setApkLoginMessage(message, type = "info") {
  const node = $("#apk-login-message");
  node.textContent = message;
  node.dataset.type = type;
}

function setApkSmsBusy(busy) {
  const button = $("#apk-send-sms");
  if (!busy && apkSmsCooldownUntil > Date.now()) return;
  button.disabled = busy;
  button.textContent = busy ? "正在打开安全验证…" : "获取短信验证码";
}

function setApkCaptchaVerified(verified) {
  const help = $("#apk-sms-help");
  help.classList.toggle("is-verified", verified);
  help.textContent = verified ? "✓ 安全验证成功" : "点击后完成 GeeTest 安全验证";
}

function startApkSmsCooldown(seconds = 180) {
  window.clearInterval(apkSmsCooldownTimer);
  apkSmsCooldownUntil = Date.now() + Math.max(1, Number(seconds) || 180) * 1000;
  const update = () => {
    const remaining = Math.max(0, Math.ceil((apkSmsCooldownUntil - Date.now()) / 1000));
    const button = $("#apk-send-sms");
    if (remaining > 0) {
      button.disabled = true;
      button.textContent = `${remaining} 秒后可重试`;
      return;
    }
    window.clearInterval(apkSmsCooldownTimer);
    apkSmsCooldownTimer = null;
    apkSmsCooldownUntil = 0;
    button.disabled = false;
    button.textContent = "获取短信验证码";
    setApkCaptchaVerified(false);
  };
  update();
  apkSmsCooldownTimer = window.setInterval(update, 1000);
}

function hideApkLoginForCaptcha() {
  const dialog = $("#apk-login-dialog");
  // showModal() 会让其余文档进入 inert 状态；GeeTest 挂在 body 下，保持 modal
  // 会导致验证层即使可见也无法交互。同步切换为非模态 open 状态后再隐藏，
  // 表单节点和输入值均保留，页面交互同时恢复。
  if (dialog.open) dialog.close();
  dialog.show();
  dialog.classList.add("is-captcha-hidden");
  dialog.setAttribute("aria-hidden", "true");
}

function restoreApkLoginAfterCaptcha() {
  const dialog = $("#apk-login-dialog");
  dialog.classList.remove("is-captcha-hidden");
  dialog.removeAttribute("aria-hidden");
  if (dialog.open) dialog.close();
  dialog.showModal();
}

function loadGeetestSdk() {
  if (typeof window.initGeetest4 === "function") return Promise.resolve();
  if (geetestLoader) return geetestLoader;
  geetestLoader = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-xdl-geetest]');
    const script = existing || document.createElement("script");
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      if (!error && typeof window.initGeetest4 === "function") resolve();
      else {
        script.remove();
        geetestLoader = null;
        reject(error || new Error("GeeTest SDK 初始化失败，请检查网络后重试。"));
      }
    };
    const timer = window.setTimeout(
      () => finish(new Error("安全验证加载超时，请检查网络后重试。")), 12000,
    );
    script.addEventListener("load", () => finish(), { once: true });
    script.addEventListener("error", () => finish(
      new Error("无法加载 GeeTest 安全验证，请检查网络后重试。"),
    ), { once: true });
    if (!existing) {
      script.src = "https://static.geetest.com/v4/gt4.js";
      script.async = true;
      script.dataset.xdlGeetest = "true";
      document.head.append(script);
    }
  });
  return geetestLoader;
}

async function apkSendSms() {
  if (apkSmsCooldownUntil > Date.now()) return;
  setApkCaptchaVerified(false);
  setApkSmsBusy(true);
  setApkLoginMessage("正在加载安全验证…", "loading");
  try {
    const mobile = $("#apk-mobile").value.trim();
    if (!/^\+?\d{6,18}$/.test(mobile)) throw new Error("手机号格式无效");
    await loadGeetestSdk();
    apkLoginConfig ||= await api("/api/apk-auth/config");
    apkCaptcha ||= await new Promise((resolve, reject) => {
      try {
        window.initGeetest4({
          captchaId: apkLoginConfig.captcha_id,
          product: "bind",
          language: "zho",
        }, resolve);
      } catch (error) { reject(error); }
    });
    const captcha = apkCaptcha;
    captcha.onSuccess(async () => {
      if (apkCaptcha !== captcha) return;
      try {
        const fdsOtp = captcha.getValidate();
        apkCaptcha = null;
        captcha.destroy?.();
        restoreApkLoginAfterCaptcha();
        setApkCaptchaVerified(true);
        setApkLoginMessage("安全验证通过，正在发送短信验证码…", "loading");
        const sms = await api("/api/apk-auth/sms", {
          method: "POST", body: JSON.stringify({ mobile, fds_otp: fdsOtp }),
        });
        setApkLoginMessage("验证码已发送，请填写短信验证码。", "success");
        startApkSmsCooldown(sms.retry_after_seconds || 180);
        $("#apk-code").focus();
      } catch (error) {
        setApkLoginMessage(error.message, "error");
      } finally {
        setApkSmsBusy(false);
        restoreApkLoginAfterCaptcha();
      }
    });
    captcha.onError((error) => {
      if (apkCaptcha !== captcha) return;
      setApkLoginMessage(error?.msg || "安全验证失败，请重试。", "error");
      setApkCaptchaVerified(false);
      apkCaptcha = null;
      captcha.destroy?.();
      setApkSmsBusy(false);
      restoreApkLoginAfterCaptcha();
    });
    captcha.onClose?.(() => {
      if (apkCaptcha !== captcha) return;
      setApkLoginMessage("安全验证已取消，可以重新获取验证码。", "info");
      setApkCaptchaVerified(false);
      apkCaptcha = null;
      captcha.destroy?.();
      setApkSmsBusy(false);
      restoreApkLoginAfterCaptcha();
    });
    setApkLoginMessage("请完成弹出的安全验证。", "loading");
    // 原生 <dialog> 位于浏览器 top layer；GeeTest 将浮层挂到普通 body 下。
    // 保持 dialog.open 不变，仅在验证期间 display:none，避免改变弹窗状态；
    // 完成、取消或失败后移除隐藏类即可原位恢复。
    hideApkLoginForCaptcha();
    captcha.showCaptcha();
  } catch (error) {
    setApkLoginMessage(error.message, "error");
    setApkSmsBusy(false);
    restoreApkLoginAfterCaptcha();
  }
}

async function apkVerifyLogin(event) {
  event.preventDefault();
  if (apkLoginMode !== "sms") return;
  try {
    const result = await api("/api/apk-auth/verify", {
      method: "POST", body: JSON.stringify({ code: $("#apk-code").value.trim() }),
    });
    completeApkLogin(result);
  } catch (error) {
    setApkLoginMessage(error.message, "error");
  }
}

function completeApkLogin(result) {
  setApkLoginMessage(result.authenticated ? "登录成功。" : "登录未完成。",
    result.authenticated ? "success" : "error");
  if (!result.authenticated) return;
  $("#apk-login-dialog").close();
  const requeued = Number(result.requeued_auth_tasks || 0);
  toast(requeued
    ? `APK 协议登录成功，已恢复 ${requeued} 条鉴权失败任务`
    : "APK 协议登录成功");
  loadBootstrap(false);
}

async function apkPasswordLogin() {
  const account = $("#apk-account").value.trim();
  const password = $("#apk-password").value;
  if (apkLoginMode === "mobile" && !/^\+?\d{6,18}$/.test(account)) {
    setApkLoginMessage("手机号格式无效。", "error");
    return;
  }
  if (apkLoginMode === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(account)) {
    setApkLoginMessage("邮箱格式无效。", "error");
    return;
  }
  if (!password) {
    setApkLoginMessage("请输入密码。", "error");
    return;
  }
  const button = $("#apk-password-captcha");
  button.disabled = true;
  button.textContent = "正在打开安全验证…";
  setApkLoginMessage("正在加载安全验证…", "loading");
  try {
    await loadGeetestSdk();
    apkLoginConfig ||= await api("/api/apk-auth/config");
    const captcha = await new Promise((resolve, reject) => {
      try {
        window.initGeetest4({
          captchaId: apkLoginConfig.captcha_id,
          product: "bind",
          language: "zho",
        }, resolve);
      } catch (error) { reject(error); }
    });
    apkCaptcha = captcha;
    captcha.onSuccess(async () => {
      if (apkCaptcha !== captcha) return;
      let loginCompleted = false;
      try {
        const fdsOtp = captcha.getValidate();
        apkCaptcha = null;
        captcha.destroy?.();
        restoreApkLoginAfterCaptcha();
        $("#apk-password-help").textContent = "✓ 安全验证成功";
        $("#apk-password-help").classList.add("is-verified");
        setApkLoginMessage("安全验证通过，正在登录…", "loading");
        const result = await api("/api/apk-auth/password", {
          method: "POST",
          body: JSON.stringify({ account, password, mode: apkLoginMode, fds_otp: fdsOtp }),
        });
        $("#apk-password").value = "";
        loginCompleted = Boolean(result.authenticated);
        completeApkLogin(result);
      } catch (error) {
        setApkLoginMessage(error.message, "error");
      } finally {
        button.disabled = false;
        button.textContent = "安全验证并登录";
        // 登录成功时 completeApkLogin 已关闭弹窗；失败时恢复原表单供重试。
        if (!loginCompleted) restoreApkLoginAfterCaptcha();
      }
    });
    const failed = (message) => {
      if (apkCaptcha !== captcha) return;
      apkCaptcha = null;
      captcha.destroy?.();
      button.disabled = false;
      button.textContent = "安全验证并登录";
      setApkLoginMessage(message, "error");
      restoreApkLoginAfterCaptcha();
    };
    captcha.onError((error) => failed(error?.msg || "安全验证失败，请重试。"));
    captcha.onClose?.(() => failed("安全验证已取消。"));
    setApkLoginMessage("请完成弹出的安全验证。", "loading");
    hideApkLoginForCaptcha();
    captcha.showCaptcha();
  } catch (error) {
    button.disabled = false;
    button.textContent = "安全验证并登录";
    setApkLoginMessage(error.message, "error");
    restoreApkLoginAfterCaptcha();
  }
}

function switchView(view) {
  state.view = view;
  $$('[data-view-panel]').forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === view);
  });
  $$('.nav-item[data-view]').forEach((item) => {
    item.classList.toggle("is-active", item.dataset.view === view);
  });
  if (view === "diagnostics" && !state.riskLoaded) loadRiskReport();
  if (view === "tasks" && Date.now() - polling.lastTaskRefreshAt > IDLE_TASK_POLL_MS) {
    loadTaskPage();
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function focusComposer() {
  switchView("tasks");
  window.setTimeout(() => {
    $("#download-target").focus();
    $("#download-form").scrollIntoView({ block: "center", behavior: "smooth" });
  }, 80);
}

function setDownloadMode(mode) {
  state.mode = mode;
  $$('[data-mode]').forEach((button) => {
    button.classList.toggle("is-active", button.dataset.mode === mode);
  });
  const range = $("#download-range");
  const hint = $("#composer-hint");
  const rangeField = $(".range-field");
  if (mode === "track") {
    range.value = "";
    range.disabled = true;
    rangeField.classList.add("is-hidden");
    hint.textContent = "单曲下载会按所选音质自动回退到可用格式。";
  } else {
    range.disabled = false;
    rangeField.classList.remove("is-hidden");
    hint.textContent = "区间支持 1-20、5-、-10 或单集序号。";
  }
}

function renderHeader() {
  const loginButton = $("#login-status");
  const loginText = $("#login-status-text");
  const backend = state.settings?.source_backend || "http";
  const apkLoginButton = $("#apk-login-button");
  apkLoginButton?.classList.toggle("is-hidden", backend !== "apk");
  if (backend === "apk" && apkLoginButton) {
    apkLoginButton.textContent = state.login?.authenticated
      ? `APK · ${state.login.uid || "已登录"}` : "APK 登录";
    apkLoginButton.classList.toggle("is-warning", !state.login?.authenticated);
    apkLoginButton.title = state.login?.authenticated
      ? "点击可重新登录 APK 协议账号"
      : "下载前请先完成 APK 协议登录";
  }
  // 登录态按浏览器分家，所以状态里必须带上"是哪个浏览器"，否则用户切换浏览器后
  // 只会看到莫名其妙的"尚未登录"。
  const browserName = state.login?.browser_name || "";
  const prefix = browserName ? `${browserName} · ` : "";
  const other = state.login?.other_browser_authenticated;
  const otherName = other === "edge" ? "Edge" : other === "chrome" ? "Chrome" : "";
  if (state.login?.authenticated) {
    loginText.textContent = `${prefix}已保存登录态`;
    loginButton.classList.remove("is-warning");
    loginButton.title = "点击重新登录";
  } else if (otherName) {
    loginText.textContent = `${prefix}尚未登录`;
    loginButton.classList.add("is-warning");
    loginButton.title = `${otherName} 中已有登录态并已完整保留，可在设置里切回；`
      + `或点击在${browserName || "当前浏览器"}中登录。`;
  } else if (state.login?.profile_exists) {
    loginText.textContent = `${prefix}凭据未缓存`;
    loginButton.classList.add("is-warning");
    loginButton.title = "点击登录或刷新凭据";
  } else {
    loginText.textContent = `${prefix}尚未登录`;
    loginButton.classList.add("is-warning");
    loginButton.title = "点击打开浏览器登录";
  }
  $("#backend-status").textContent =
    backend === "pc" ? "PC 桌面端接口"
      : backend === "http" ? "HTTP 后端"
        : backend === "apk" ? "Android APK 协议" : "浏览器后端";
  $("#concurrency-status").textContent = `并发 ${state.settings?.max_concurrency ?? 1}`;
  if (state.settings?.default_quality) {
    $("#download-quality").value = state.settings.default_quality;
  }
}

function renderCounts() {
  for (const key of ["all", "pending", "downloading", "done", "failed"]) {
    $(`#count-${key}`).textContent = state.counts?.[key] ?? 0;
    const button = $(`[data-filter="${key}"]`);
    button?.setAttribute("aria-pressed", String(state.filter === key));
  }
}

function applyTaskPayload(payload, { force = false } = {}) {
  const renderKey = JSON.stringify([
    payload.tasks || [], payload.counts || {}, payload.page || {}, payload.error || null,
  ]);
  state.tasks = payload.tasks || [];
  state.counts = payload.counts || {};
  state.page = { ...state.page, ...(payload.page || {}) };
  state.taskError = payload.error || null;
  if (force || renderKey !== state.taskRenderKey) {
    state.taskRenderKey = renderKey;
    renderTasks();
  }
}

function renderSelection() {
  const page = state.tasks.filter((task) => task.state !== "downloading");
  const on = page.filter((task) => state.picked.has(task.id)).length;
  const head = $("#head-check");
  head.checked = page.length > 0 && on === page.length;
  head.indeterminate = on > 0 && on < page.length;
  head.disabled = page.length === 0;

  const chip = $("#album-chip");
  chip.classList.toggle("is-hidden", !state.album);
  if (state.album) {
    $("#album-chip-text").textContent =
      `专辑：${state.albumTitle || state.album}`;
  }

  const total = Number(state.page.total || 0);
  const selectAll = $("#select-all");
  selectAll.textContent = `选中全部 ${total} 条${scopeWord()}`;
  selectAll.disabled = total === 0;

  const count = state.picked.size;
  $("#bulk-bar").classList.toggle("is-hidden", count === 0);
  if (count > 0) {
    $("#bulk-count").textContent = count;
    const locked = state.tasks.filter((task) => task.state === "downloading").length;
    const note = $("#bulk-note");
    note.classList.toggle("is-hidden", locked === 0);
    note.textContent = locked ? `· ${locked} 条运行中无法选择` : "";
  }
}

function scopeWord() {
  const bits = [];
  const labels = { downloading: "进行中", pending: "待恢复", done: "已完成", failed: "失败" };
  if (state.filter !== "all") bits.push(labels[state.filter] || state.filter);
  if (state.album) bits.push(`《${state.albumTitle || state.album}》`);
  if (state.search.trim()) bits.push(`含“${state.search.trim()}”`);
  return bits.length ? ` ${bits.join(" · ")}` : "";
}

/* 范围变了选中必须失效，否则会出现「要删的东西一条都不在屏幕上」。
   翻页不算范围变化——用户得能翻页核对再删。 */
function resetScope(changes) {
  Object.assign(state, changes);
  state.page.offset = 0;
  state.picked.clear();
  loadTaskPage();
}

function renderTasks() {
  renderCounts();
  const list = $("#task-list");
  const empty = $("#task-empty");
  const tasks = state.tasks;
  list.innerHTML = tasks.map(taskRow).join("");
  empty.classList.toggle("is-hidden", tasks.length > 0);
  if (tasks.length === 0) {
    const heading = $("#task-empty h2");
    const copy = $("#task-empty p");
    const hasAny = Number(state.counts?.all || 0) > 0;
    heading.textContent = hasAny ? "没有符合条件的任务" : "还没有下载任务";
    copy.textContent = hasAny
      ? "切换筛选条件，或尝试搜索其他曲目和 ID。"
      : "粘贴专辑或曲目链接，第一条任务会出现在这里。";
  }
  renderTaskError();
  renderPagination();
  renderSelection();
}

function renderTaskError() {
  const taskError = $("#task-error");
  taskError.classList.toggle("is-hidden", !state.taskError);
  taskError.textContent = state.taskError ? `任务库暂时不可用：${state.taskError}` : "";
}

function renderPagination() {
  const page = state.page;
  const total = Number(page.total || 0);
  const limit = Math.max(1, Number(page.limit || 100));
  const offset = Math.max(0, Number(page.offset || 0));
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const pageNumber = total ? Math.floor(offset / limit) + 1 : 1;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + state.tasks.length, total);
  const globalTotal = Number(state.counts?.all || 0);
  const scope = globalTotal !== total ? `，任务库共 ${globalTotal} 条` : "";
  $("#task-page-summary").textContent = total
    ? `显示 ${start}–${end}，当前筛选共 ${total} 条${scope}`
    : `当前筛选没有任务${scope}`;
  $("#task-page-number").textContent = `第 ${pageNumber} / ${pageCount} 页`;
  $("#task-page-previous").disabled = !page.has_previous;
  $("#task-page-next").disabled = !page.has_next;
}

function taskRow(task) {
  const labels = {
    downloading: "进行中",
    pending: "待恢复",
    done: "已完成",
    failed: "失败",
  };
  const episode = task.album_index > 0 ? `第 ${String(task.album_index).padStart(2, "0")} 集` : "单曲";
  const error = task.last_error_msg
    ? `<span class="task-error-copy" title="${escapeHtml(task.last_error_msg)}">${escapeHtml(task.last_error_msg)}</span>`
    : "";
  // 运行中的任务不能删：下载线程正握着它的 .part，删了记录它也会继续写完
  const locked = task.state === "downloading";
  const lockHint = "任务运行中，请先优雅停止";
  const picked = state.picked.has(task.id);
  const parent = task.album_id
    ? `<button class="album-link" type="button" data-album-id="${escapeHtml(task.album_id)}"
        title="只看这个专辑">专辑 ${escapeHtml(task.album_id)}</button>`
    : "独立曲目";
  return `
    <tr data-task-id="${task.id ?? ""}" class="${picked ? "is-picked" : ""}${locked ? " is-locked" : ""}">
      <td class="select-cell">
        <input class="cbx" type="checkbox" data-pick="${task.id}"
               ${picked ? "checked" : ""} ${locked ? "disabled" : ""}
               title="${locked ? lockHint : "选择这条任务"}"
               aria-label="选择 ${escapeHtml(task.title)}">
      </td>
      <td>
        <div class="task-title">
          <strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong>
          <span>${parent} · ${episode} · ID ${escapeHtml(task.track_id)}</span>
          ${error}
        </div>
      </td>
      <td><span class="state-badge state-${task.state}">${labels[task.state] || task.state}</span></td>
      <td><span class="format-data">${qualityLabel(task.quality)}</span></td>
      <td><span class="size-data">${formatBytes(task.total_bytes)}</span></td>
      <td>
        <div class="progress-cell">
          <span class="progress-value">${task.progress}%</span>
          ${chapterTicks(task.progress, task.state)}
        </div>
      </td>
      <td>
        <div class="row-actions">
          <button class="row-action" type="button" data-open-task="${task.id}">打开目录</button>
          <button class="icon-action" type="button" data-delete-task="${task.id}"
                  ${locked ? "disabled" : ""}
                  title="${locked ? lockHint : "删除这条任务"}" aria-label="删除">
            <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>
          </button>
        </div>
      </td>
    </tr>`;
}

function chapterTicks(progress, taskState) {
  const total = 20;
  const filled = taskState === "done" ? total : Math.floor((progress / 100) * total);
  const ticks = [];
  for (let index = 0; index < total; index += 1) {
    let klass = "chapter-tick";
    if (index < filled) klass += " is-filled";
    if (taskState === "downloading" && index === filled && filled < total) klass += " is-current";
    ticks.push(`<span class="${klass}"></span>`);
  }
  return `<div class="chapter-progress" role="progressbar" aria-label="下载进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}">${ticks.join("")}</div>`;
}

function qualityLabel(value) {
  return { high: "高", standard: "标准", low: "低" }[value] || value || "—";
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function applyOperation(operation, { force = false } = {}) {
  const renderKey = JSON.stringify(operation || null);
  state.operation = operation;
  if (force || renderKey !== state.operationRenderKey) {
    state.operationRenderKey = renderKey;
    renderOperation();
  }
}

function renderOperation() {
  const panel = $("#operation-strip");
  const operation = state.operation;
  if (!operation) {
    panel.classList.add("is-hidden");
    document.title = "XDL · 下载任务";
    return;
  }
  panel.classList.remove("is-hidden");
  panel.dataset.status = operation.status;
  const statusLabels = {
    running: "进行中",
    succeeded: "已完成",
    failed: "失败",
    stopped: "已停止",
  };
  $("#operation-state").textContent = statusLabels[operation.status] || operation.status;
  $("#operation-title").textContent = operation.current_title || operation.label || operationLabels[operation.kind] || "后台操作";
  $("#operation-message").textContent = operation.message || operationLabels[operation.kind] || "正在准备";
  const total = Number(operation.progress_total || 0);
  const done = Number(operation.progress_done || 0);
  const percent = total > 0 ? Math.min(100, Math.floor((done / total) * 100)) : 0;
  const progress = $("#operation-progress");
  progress.classList.toggle("is-indeterminate", operation.status === "running" && total <= 0);
  $("#operation-progress span").style.width = total > 0 ? `${percent}%` : "";
  progress.setAttribute("aria-valuenow", String(percent));
  const stop = $("#stop-button");
  stop.classList.toggle("is-hidden", operation.status !== "running" || !operation.cancellable);
  stop.disabled = Boolean(operation.stop_requested);
  stop.textContent = operation.stop_requested ? "正在停止" : "优雅停止";
  $("#operation-notes").innerHTML = (operation.notes || [])
    .slice().reverse().map((note) => `<li>${escapeHtml(note.message)}</li>`).join("");
  document.title = operation.status === "running"
    ? `● ${operation.label} · XDL`
    : "XDL · 下载任务";
  handleOperationTerminal(operation);
}

function handleOperationTerminal(operation) {
  if (!terminalStatuses.has(operation.status)) return;
  const key = `${operation.id}:${operation.status}`;
  if (state.handledTerminals.has(key)) return;
  state.handledTerminals.add(key);
  if (operation.status === "failed") {
    toast(operation.message || `${operation.label}失败`, "error");
  } else if (operation.status === "stopped") {
    toast("任务已停止，进度已保留");
  } else {
    toast(`${operation.label}已完成`);
  }
  if (shouldShowResult(operation)) {
    showOperationResult(operation);
  } else if (operation.has_result) {
    loadOperationResult(operation.id);
  }
  if (["login", "refresh_cookies"].includes(operation.kind) && operation.status === "succeeded") {
    window.setTimeout(() => loadBootstrap(false), 100);
  }
  if (["download_track", "download_album", "resume"].includes(operation.kind)) {
    window.setTimeout(() => loadTaskPage(), 100);
  }
}

async function loadOperationResult(operationId) {
  try {
    const payload = await api("/api/operation?include_result=true");
    const operation = payload.operation;
    if (!operation || operation.id !== operationId) return;
    if (shouldShowResult(operation)) showOperationResult(operation);
  } catch (error) {
    console.warn("载入操作结果失败", error);
  }
}

function shouldShowResult(operation) {
  if (!operation.result) return false;
  if (["formats", "inspect_storage", "gen_sign", "extract_device", "refresh_cookies"].includes(operation.kind)) return true;
  if (operation.kind === "download_album") {
    const result = operation.result.album;
    return Boolean(result?.failed?.length || result?.risk_control || result?.incomplete);
  }
  if (operation.kind === "resume") {
    return operation.result.albums?.some((item) => item.failed?.length || item.risk_control || item.incomplete);
  }
  return false;
}

function showOperationResult(operation) {
  $("#dialog-tag").textContent = operationLabels[operation.kind] || "操作结果";
  $("#dialog-title").textContent = operation.status === "failed" ? "操作失败" : "操作结果";
  const content = $("#dialog-content");
  content.innerHTML = resultMarkup(operation);
  const dialog = $("#result-dialog");
  if (!dialog.open) dialog.showModal();
}

function resultMarkup(operation) {
  const result = operation.result || {};
  if (operation.kind === "formats") {
    const rows = (result.formats || []).map((format) => `
      <tr><td>${escapeHtml(format.type)}</td><td>${escapeHtml(format.codec)}</td><td>${format.bitrate || "—"}k</td><td>${formatBytes(format.file_size)}</td></tr>`).join("");
    return `<p class="result-summary"><strong>${escapeHtml(result.title || "曲目")}</strong><br>ID ${escapeHtml(result.track_id || "—")} · 共 ${(result.formats || []).length} 种可用格式</p>
      <table class="result-table"><thead><tr><th>格式</th><th>编码</th><th>码率</th><th>大小</th></tr></thead><tbody>${rows || '<tr><td colspan="4">没有可用格式</td></tr>'}</tbody></table>`;
  }
  if (operation.kind === "gen_sign") {
    return `<p class="result-summary">已生成 ${result.repeat || 0} 个签名值。签名仅用于检查本地链路。</p><code class="result-code">${(result.values || []).map(escapeHtml).join("\n\n")}</code>`;
  }
  if (operation.kind === "extract_device") {
    return `<p class="result-summary">${escapeHtml(result.summary || "采集完成")}</p><dl class="definition-list"><div><dt>字段</dt><dd>${result.field_count ?? "—"}</dd></div><div><dt>短指纹</dt><dd>${escapeHtml(result.identity || "—")}</dd></div></dl><p><code>${escapeHtml(result.output_path || "")}</code></p>`;
  }
  if (operation.kind === "refresh_cookies") {
    return `<p class="result-summary">已验证登录 token，并保存 ${result.cookie_count || 0} 个 Cookie。匿名结果不会覆盖缓存。</p><p><code>${escapeHtml(result.output_path || "")}</code></p>`;
  }
  if (operation.kind === "download_album") return albumMarkup(result.album);
  if (operation.kind === "resume") return (result.albums || []).map(albumMarkup).join("");
  return `<pre class="json-output">${escapeHtml(JSON.stringify(result, null, 2))}</pre>`;
}

function albumMarkup(album = {}) {
  const failures = (album.failed || []).map((item) => `<li>第 ${item.index} 集 ${escapeHtml(item.title)}：${escapeHtml(item.error)}</li>`).join("");
  return `<p class="result-summary">${escapeHtml(album.summary || "任务已完成")}</p>${failures ? `<h3>失败明细</h3><ul>${failures}</ul>` : ""}`;
}

async function startOperation(path, body = null) {
  try {
    const options = { method: "POST" };
    if (body !== null) options.body = JSON.stringify(body);
    applyOperation(await api(path, options), { force: true });
    toast(`${state.operation.label || "操作"}已开始`);
    scheduleRuntimeRefresh(100);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function stopOperation() {
  try {
    applyOperation(await api("/api/operations/stop", { method: "POST" }), { force: true });
    scheduleRuntimeRefresh(100);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function openDownloads(taskId = null) {
  try {
    const result = await api("/api/open-downloads", {
      method: "POST",
      body: JSON.stringify({ task_id: taskId }),
    });
    toast(`已打开 ${result.path}`);
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ---------- 选择、删除、恢复队列 ---------- */

function togglePick(id, on) {
  if (on) state.picked.add(id);
  else state.picked.delete(id);
  renderTasks();
}

function togglePagePick() {
  const page = state.tasks.filter((task) => task.state !== "downloading");
  const on = page.length > 0 && page.every((task) => state.picked.has(task.id));
  page.forEach((task) => (on ? state.picked.delete(task.id) : state.picked.add(task.id)));
  renderTasks();
}

async function selectAllInScope() {
  try {
    const payload = await api(`/api/tasks/ids?${scopeParams()}`);
    state.picked = new Set(payload.ids || []);
    renderTasks();
    if (payload.truncated) {
      toast(`任务过多，只选中了最近的 ${payload.count} 条`, "error");
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function clearPick() {
  state.picked.clear();
  renderTasks();
}

/* 不管几条都弹窗：删除的行为必须可预测，用户不该先在脑子里判断
   「这条是不是已完成」才能预期会发生什么。分档只决定弹窗里说什么。

   明细一律取自后端按真实 id 集算出的 summary——选中集可以跨页，前端手里只有
   当前页那 100 条，自己统计会漏掉绝大部分，让确认框上的数字撒谎。 */
async function deletePrompt(ids) {
  if (!ids.length) return;
  let summary;
  try {
    const payload = await api("/api/tasks/preview", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    summary = payload.summary || {};
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  const labels = { pending: "待恢复", done: "已完成", failed: "失败" };
  const states = summary.states || {};
  const live = Number(summary.running || 0);
  const count = Object.values(states).reduce((sum, n) => sum + Number(n), 0);
  if (!count) {
    toast(live ? "选中的任务都在下载中，请先优雅停止。" : "选中的任务已不存在。",
          "error");
    return;
  }
  const single = count === 1 && ids.length === 1;
  const known = single ? state.tasks.find((task) => task.id === ids[0]) : null;

  const lines = [];
  if (single && known) {
    const where = known.album_id ? `专辑 ${escapeHtml(known.album_id)}` : "独立曲目";
    // 曲目名放正文而不是标题：名字里常自带《》，套进标题会变成双层书名号，
    // 长标题还会把标题行撑开
    lines.push(`<div class="line"><i></i><strong>${escapeHtml(known.title)}</strong></div>`);
    lines.push(`<div class="line"><i></i><span>${labels[known.state] || known.state} · ${where}</span></div>`);
  } else {
    const parts = Object.entries(states)
      .filter(([, n]) => n > 0)
      .map(([key, n]) => `${labels[key] || key} ${n}`);
    lines.push(`<div class="line"><i></i><span>${parts.join(" · ")}</span></div>`);
  }
  if (summary.with_part) {
    const scale = single ? "" : `${summary.with_part} 条的`;
    lines.push(`<div class="line warn"><i></i><span>${scale}未完成进度（约 ${formatBytes(summary.part_bytes)}）会被清除，重新下载需从头开始。</span></div>`);
  }
  if (live) {
    lines.push(`<div class="line skip"><i></i><span>${live} 条正在下载，已跳过（需先优雅停止）。</span></div>`);
  }
  if (summary.missing) {
    lines.push(`<div class="line skip"><i></i><span>${summary.missing} 条已不存在，忽略。</span></div>`);
  }

  openDialog({
    title: single ? "删除这条任务？" : `删除选中的 ${count} 条任务？`,
    sub: "此操作不可撤销。",
    lines,
    confirmText: single ? "删除" : `删除 ${count} 条`,
    onConfirm: () => deleteTasks(ids),
  });
}

async function deleteTasks(ids) {
  try {
    const payload = await api("/api/tasks/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    const result = payload.result || {};
    state.picked.clear();
    applyTaskPayload(payload, { force: true });
    polling.lastTaskRefreshAt = Date.now();
    const bits = [`已删除 ${result.deleted || 0} 条`];
    if (result.files_removed) bits.push(`清理 ${result.files_removed} 个未完成文件`);
    if (result.skipped_running) bits.push(`跳过 ${result.skipped_running} 条运行中`);
    if (result.files_failed) bits.push(`${result.files_failed} 个文件删不掉，可稍后手动清理`);
    toast(bits.join("，"), result.files_failed ? "error" : "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function requeuePicked() {
  const ids = [...state.picked];
  if (!ids.length) return;
  try {
    const payload = await api("/api/tasks/requeue", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    state.picked.clear();
    applyTaskPayload(payload, { force: true });
    polling.lastTaskRefreshAt = Date.now();
    toast(payload.requeued
      ? `已把 ${payload.requeued} 条加入恢复队列，点“恢复全部”开始下载`
      : "选中的任务里没有可重新排队的失败任务");
  } catch (error) {
    toast(error.message, "error");
  }
}

function openDialog({ title, sub, lines, confirmText, onConfirm, safeNote = "" }) {
  const root = $("#dialog-root");
  const previous = document.activeElement;
  root.innerHTML = `
    <div class="scrim" role="dialog" aria-modal="true" aria-label="${title}">
      <div class="dialog">
        <h2>${title}</h2>
        ${sub ? `<p class="dialog-sub">${sub}</p>` : ""}
        <div class="consequences">${lines.join("")}</div>
        <div class="safe-note">
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4.4-3 7.7-7 9-4-1.3-7-4.6-7-9V6z"/><path d="m9 12 2 2 4-4"/></svg>
          <span>${safeNote || "已下载完成的音频文件<b>不会</b>被删除，只清理任务记录与未完成的临时文件。"}</span>
        </div>
        <div class="dialog-actions">
          <button class="button secondary" type="button" data-dialog="cancel">取消</button>
          <button class="button danger" type="button" data-dialog="confirm">${confirmText}</button>
        </div>
      </div>
    </div>`;

  const close = () => {
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey, true);
    if (previous?.isConnected) previous.focus();
  };
  const onKey = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  };
  document.addEventListener("keydown", onKey, true);
  // 默认焦点落在取消：回车不该等于确认删除
  root.querySelector('[data-dialog="cancel"]').focus();
  root.addEventListener("click", (event) => {
    if (event.target.classList.contains("scrim")) return close();
    const button = event.target.closest("[data-dialog]");
    if (!button) return;
    close();
    if (button.dataset.dialog === "confirm") onConfirm();
  });
}

async function loadBootstrap(populateSettings = true) {
  try {
    const payload = await api("/api/bootstrap");
    state.settings = payload.settings;
    state.login = payload.login;
    renderHeader();
    renderApkAccounts();
    applyOperation(payload.operation, { force: true });
    if (populateSettings || !state.settingsPopulated) {
      applyTaskPayload({
        tasks: payload.tasks,
        counts: payload.counts,
        page: payload.page,
        error: payload.task_error,
      }, { force: true });
      polling.lastTaskRefreshAt = Date.now();
    }
    if (populateSettings || !state.settingsPopulated) populateSettingsForm();
  } catch (error) {
    toast(`无法载入 WebUI：${error.message}`, "error");
  }
}

function taskQueryPath() {
  const params = new URLSearchParams({
    limit: String(state.page.limit || 100),
    offset: String(state.page.offset || 0),
  });
  if (state.filter !== "all") params.set("state", state.filter);
  if (state.search.trim()) params.set("search", state.search.trim().slice(0, 200));
  if (state.album) params.set("album_id", state.album);
  return `/api/tasks?${params}`;
}

function scopeParams() {
  const params = new URLSearchParams();
  if (state.filter !== "all") params.set("state", state.filter);
  if (state.search.trim()) params.set("search", state.search.trim().slice(0, 200));
  if (state.album) params.set("album_id", state.album);
  return params;
}

async function loadTaskPage() {
  const request = ++polling.taskRequest;
  $("#task-table-wrap").setAttribute("aria-busy", "true");
  try {
    const payload = await api(taskQueryPath());
    if (request !== polling.taskRequest) return;
    applyTaskPayload(payload);
    polling.lastTaskRefreshAt = Date.now();
  } catch (error) {
    if (request !== polling.taskRequest) return;
    state.taskError = error.message;
    renderTaskError();
    $("#task-page-summary").textContent = "刷新失败，已保留当前任务列表";
  } finally {
    if (request === polling.taskRequest) {
      $("#task-table-wrap").setAttribute("aria-busy", "false");
    }
  }
}

async function loadOperationSnapshot() {
  try {
    const payload = await api("/api/operation");
    applyOperation(payload.operation);
    return true;
  } catch (error) {
    if (!document.hidden) console.warn("刷新运行状态失败", error);
    return false;
  }
}

function scheduleRuntimeRefresh(delay = null) {
  window.clearTimeout(polling.timer);
  polling.timer = null;
  if (document.hidden) return;
  const running = state.operation?.status === "running";
  polling.timer = window.setTimeout(
    refreshRuntime,
    delay ?? (running ? ACTIVE_OPERATION_POLL_MS : IDLE_OPERATION_POLL_MS),
  );
}

async function refreshRuntime({ forceTasks = false } = {}) {
  if (document.hidden || polling.running) return;
  polling.running = true;
  const wasRunning = state.operation?.status === "running";
  try {
    await loadOperationSnapshot();
    const isRunning = state.operation?.status === "running";
    const taskInterval = isRunning ? ACTIVE_TASK_POLL_MS : IDLE_TASK_POLL_MS;
    const taskDue = Date.now() - polling.lastTaskRefreshAt >= taskInterval;
    if (forceTasks || wasRunning !== isRunning || taskDue) {
      await loadTaskPage();
    }
  } finally {
    polling.running = false;
    scheduleRuntimeRefresh();
  }
}

async function loadRiskReport(force = false) {
  if (state.riskLoaded && !force) return;
  if (riskReportPromise) return riskReportPromise;
  riskReportPromise = (async () => {
    try {
      const payload = await api("/api/risk-report");
      renderRiskReport(payload);
      state.riskLoaded = true;
    } catch (error) {
      toast(error.message, "error");
    } finally {
      riskReportPromise = null;
    }
  })();
  return riskReportPromise;
}

function renderRiskReport(payload) {
  const summary = payload.summary || {};
  $("#risk-path").textContent = payload.path || "未配置风控日志";
  $("#risk-total").textContent = summary.total ?? 0;
  $("#risk-first").textContent = summary.first_risk_request_index ?? "—";
  $("#risk-inflight").textContent = summary.max_in_flight ?? 0;
  $("#risk-rate").textContent = summary.requests_per_minute ?? 0;
  const outcomes = Object.entries(summary.outcomes || {});
  $("#risk-outcomes").innerHTML = outcomes.length
    ? outcomes.map(([name, count]) => `<span class="distribution-item">${escapeHtml(outcomeLabel(name))}<strong>${count}</strong></span>`).join("")
    : '<span class="distribution-item">暂无观测数据</span>';
  const latency = summary.latency_ms || {};
  $("#risk-latency").innerHTML = ["min", "p50", "p95", "max"].map((key) => `
    <div><dt>${key}</dt><dd>${latency[key] ?? "—"}${latency[key] == null ? "" : " ms"}</dd></div>`).join("");
}

function outcomeLabel(value) {
  return { success: "成功", risk_control: "风控", auth: "鉴权", network: "网络", unknown: "未知" }[value] || value;
}

const BROWSER_NOTE_DEFAULT =
  "登录与设备采集所用的浏览器。每个浏览器的 Profile、登录凭据与设备指纹各自独立保存，"
  + "互不覆盖；自定义填写的路径保持不变。";

function renderBrowserNote() {
  const note = $("#browser-note");
  if (!note) return;
  const selected = $('#settings-form [name="browser"]')?.value;
  const saved = state.settings?.browser || "auto";
  if (!selected || selected === saved) {
    note.textContent = BROWSER_NOTE_DEFAULT;
    note.classList.remove("is-warning");
    return;
  }
  // 切换是换一整套身份，需要重新登录一次——但旧浏览器的登录态完整保留，
  // 切回即可恢复。把"可逆"讲清楚，用户才不会以为自己把登录弄丢了。
  const current = state.login?.browser_name || "当前浏览器";
  const target = { auto: "自动选择的浏览器", chrome: "Chrome", edge: "Edge" }[selected]
    || selected;
  note.textContent = `保存后将切换到 ${target}，需要在其中重新登录一次；`
    + `${current} 的登录态与设备指纹会完整保留，切回即可恢复。`;
  note.classList.add("is-warning");
}

function populateSettingsForm() {
  if (!state.settings) return;
  const form = $("#settings-form");
  $$('[name]', form).forEach((input) => {
    const value = state.settings[input.name];
    if (value === undefined || value === null) return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = String(value);
  });
  renderBrowserNote();
  state.settingsPopulated = true;
}

function settingsPayload() {
  const payload = {};
  $$('[name]', $("#settings-form")).forEach((input) => {
    if (input.type === "checkbox") {
      payload[input.name] = input.checked;
    } else if (input.type === "number") {
      payload[input.name] = input.value === "" ? null : Number(input.value);
    } else {
      payload[input.name] = input.value;
    }
  });
  return payload;
}

async function saveSettings() {
  try {
    const result = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settingsPayload()),
    });
    state.settings = result.settings;
    renderHeader();
    populateSettingsForm();
    toast("设置已保存，运行器已重新加载");
    window.setTimeout(() => refreshRuntime({ forceTasks: true }), 100);
  } catch (error) {
    toast(error.message, "error");
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function changeTaskPage(direction) {
  const step = Number(state.page.limit || 100);
  if (direction < 0 && !state.page.has_previous) return;
  if (direction > 0 && !state.page.has_next) return;
  state.page.offset = Math.max(0, Number(state.page.offset || 0) + direction * step);
  loadTaskPage();
  $("#task-table-wrap").scrollIntoView({ block: "start", behavior: "smooth" });
}

document.addEventListener("click", (event) => {
  const view = event.target.closest("[data-view]");
  if (view) switchView(view.dataset.view);

  const mode = event.target.closest("[data-mode]");
  if (mode) setDownloadMode(mode.dataset.mode);

  const filter = event.target.closest("[data-filter]");
  if (filter) {
    $$('[data-filter]').forEach((button) => button.classList.toggle("is-active", button === filter));
    renderCounts();
    resetScope({ filter: filter.dataset.filter });
  }

  const albumLink = event.target.closest("[data-album-id]");
  if (albumLink) {
    resetScope({ album: albumLink.dataset.albumId, albumTitle: "" });
  }

  const taskButton = event.target.closest("[data-open-task]");
  if (taskButton) openDownloads(Number(taskButton.dataset.openTask));

  const deleteButton = event.target.closest("[data-delete-task]");
  if (deleteButton) deletePrompt([Number(deleteButton.dataset.deleteTask)]);

  const action = event.target.closest("[data-action]")?.dataset.action;
  if (!action) return;
  const actions = {
    "focus-composer": focusComposer,
    login: openLogin,
    resume: () => {
      if (requireApkLogin()) startOperation("/api/operations/resume");
    },
    stop: stopOperation,
    "open-downloads": () => openDownloads(),
    "refresh-risk": () => loadRiskReport(true),
    "tasks-previous": () => changeTaskPage(-1),
    "tasks-next": () => changeTaskPage(1),
    "select-all": selectAllInScope,
    "clear-pick": clearPick,
    "clear-album": () => resetScope({ album: "", albumTitle: "" }),
    "delete-picked": () => deletePrompt([...state.picked]),
    "requeue-picked": requeuePicked,
    "refresh-cookies": () => startOperation("/api/operations/refresh-cookies", {
      headless: !$("#cookies-visible").checked,
    }),
    "inspect-storage": () => startOperation("/api/operations/inspect-storage"),
  };
  actions[action]?.();
});

$("#apk-send-sms").addEventListener("click", apkSendSms);
$("#apk-login-form").addEventListener("submit", apkVerifyLogin);
$("#apk-password-captcha").addEventListener("click", apkPasswordLogin);
$$('[data-apk-login-mode]').forEach((button) => {
  button.addEventListener("click", () => setApkLoginMode(button.dataset.apkLoginMode));
});
$$('[data-apk-login-close]').forEach((button) => button.addEventListener("click", () => {
  $("#apk-password").value = "";
  $("#apk-login-dialog").close();
}));
$("#apk-account-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-apk-account-action]");
  if (!button) return;
  if (button.dataset.apkAccountAction === "delete") {
    confirmDeleteApkAccount(button.dataset.apkAccountUid);
  } else {
    mutateApkAccount(button.dataset.apkAccountAction, button.dataset.apkAccountUid);
  }
});

$("#download-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!requireApkLogin()) return;
  startOperation("/api/operations/download", {
    mode: state.mode,
    target: $("#download-target").value.trim(),
    quality: $("#download-quality").value,
    range: state.mode === "album" ? $("#download-range").value.trim() || null : null,
  });
});

$("#formats-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startOperation("/api/operations/formats", {
    target: $("#formats-target").value.trim(),
  });
});

$("#sign-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startOperation("/api/operations/gen-sign", {
    repeat: Number($("#sign-repeat").value || 1),
  });
});

$("#extract-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startOperation("/api/operations/extract-device", {
    output: $("#extract-output").value.trim() || null,
    profile: $("#extract-profile").value.trim() || null,
    headless: !$("#extract-visible").checked,
    refresh: $("#extract-refresh").checked,
    fresh_profile: $("#extract-fresh").checked,
  });
});

$("#settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveSettings();
});

$("#settings-form").addEventListener("change", (event) => {
  if (event.target?.name === "browser") renderBrowserNote();
});

$("#task-list").addEventListener("change", (event) => {
  const box = event.target.closest("[data-pick]");
  if (box) togglePick(Number(box.dataset.pick), box.checked);
});

$("#head-check").addEventListener("change", togglePagePick);

$("#task-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  state.page.offset = 0;
  // 搜索改变了范围，选中集随之失效；防抖期间不清，等请求真的发出去时才清
  window.clearTimeout(polling.searchTimer);
  polling.searchTimer = window.setTimeout(() => {
    state.picked.clear();
    loadTaskPage();
  }, 300);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    window.clearTimeout(polling.timer);
    polling.timer = null;
  } else {
    refreshRuntime({ forceTasks: true });
  }
});

setDownloadMode("album");
await loadBootstrap();
scheduleRuntimeRefresh(500);
