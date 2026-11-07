/* autoDownloads · local web workbench */
(() => {
  "use strict";

  const bootNode = document.getElementById("boot-data");
  let BOOT = {};
  try {
    BOOT = JSON.parse(bootNode?.textContent || "{}") || {};
  } catch (_) {
    BOOT = {};
  }

  const $ = (selector, scope = document) => scope.querySelector(selector);
  const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
  const SETTINGS_KEY = "autodownloads:web:v1";
  const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
  const RUNNING = new Set(["queued", "running"]);

  const FALLBACK_PLATFORMS = {
    bilibili: {
      label: "B 站",
      defaultQuality: "1080",
      qualities: [
        { value: "2160", label: "4K", note: "大会员" },
        { value: "1440", label: "2K", note: "大会员" },
        { value: "1080", label: "1080P", note: "推荐" },
        { value: "720", label: "720P", note: "" },
        { value: "480", label: "480P", note: "" },
        { value: "best", label: "最佳", note: "跟随账号" },
      ],
    },
    douyin: {
      label: "抖音",
      defaultQuality: "best",
      qualities: [
        { value: "best", label: "最佳", note: "推荐" },
        { value: "1080", label: "1080P", note: "" },
        { value: "720", label: "720P", note: "" },
        { value: "480", label: "480P", note: "" },
      ],
    },
  };

  const PLATFORM_COPY = {
    bilibili: {
      placeholder: "粘贴 B 站视频 / 合集 / UP 主空间链接或数字 mid，一行一个；也可以直接粘贴分享文案。",
      qualityHint: "4K / 2K 需要大会员，1080P 需要登录",
      allLabel: "下载该 UP 主全部视频",
      loginName: "B 站",
    },
    douyin: {
      placeholder: "粘贴抖音视频 / 主页链接，一行一个；也可以直接粘贴分享文案。",
      qualityHint: "实际清晰度以平台返回为准",
      allLabel: "下载该主页全部作品",
      loginName: "抖音",
    },
  };

  const state = {
    online: Boolean(BOOT.token),
    platform: "bilibili",
    quality: { bilibili: "1080", douyin: "best" },
    settings: loadSettings(),
    status: null,
    levels: null,
    job: null,
    cursor: 0,
    logs: [],
    pollTimer: 0,
    toastTimer: 0,
    jobAnnounced: false,
  };

  const el = {};

  /* ------------------------------------------------------------------ */
  /* Tiny helpers                                                        */
  /* ------------------------------------------------------------------ */

  function loadSettings() {
    try {
      const parsed = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function persistSettings() {
    if (!el.form) return;
    state.settings = {
      platform: state.platform,
      quality: { ...state.quality },
      output: el.output?.value || "",
      limit: el.limit?.value || "0",
      all: Boolean(el.all?.checked),
      folder_by_author: Boolean(el.folderByAuthor?.checked),
      force: Boolean(el.force?.checked),
      proxy: el.proxy?.value || "",
      limit_rate: el.rate?.value || "",
      engine: el.engine?.value || "auto",
      prefer_h264: Boolean(el.preferH264?.checked),
      info: Boolean(el.info?.checked),
      thumbnail: Boolean(el.thumbnail?.checked),
    };
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
    } catch (_) {
      /* storage can be unavailable in private mode */
    }
  }

  function escapeHTML(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function truncate(value, length = 64) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  }

  function formatClock(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function isRunning(job = state.job) {
    return Boolean(job && RUNNING.has(job.status));
  }

  function platformData() {
    return state.status?.platforms?.[state.platform] || FALLBACK_PLATFORMS[state.platform];
  }

  function qualities() {
    return platformData().qualities || FALLBACK_PLATFORMS[state.platform].qualities;
  }

  function qualityLabel(value) {
    const items = qualityItems();
    return (items.find((item) => item.value === value) || items[0])?.label || value;
  }

  function toast(message, type = "") {
    if (!el.toast) return;
    window.clearTimeout(state.toastTimer);
    el.toast.textContent = message;
    el.toast.classList.toggle("error", type === "error");
    el.toast.classList.add("show");
    state.toastTimer = window.setTimeout(() => el.toast.classList.remove("show"), 2600);
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {
      /* fall through */
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    area.remove();
    return ok;
  }

  async function api(path, options = {}) {
    if (!state.online || !BOOT.token) {
      throw new Error("本地服务未连接");
    }
    const headers = { "X-App-Token": BOOT.token };
    if (options.body) headers["Content-Type"] = "application/json";
    let response;
    try {
      response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body,
        cache: "no-store",
      });
    } catch (_) {
      setOffline("无法连接本地服务");
      throw new Error("无法连接本地服务");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      if (response.status === 403) setOffline("访问令牌已失效，请重新打开页面");
      throw new Error(data?.error || `请求失败（${response.status}）`);
    }
    return data;
  }

  /* ------------------------------------------------------------------ */
  /* Boot / settings                                                     */
  /* ------------------------------------------------------------------ */

  function cacheElements() {
    el.form = $("#downloadForm");
    el.links = $("#links");
    el.linkCount = $("#linkCount");
    el.qualityGrid = $("#qualityGrid");
    el.qualityHint = $("#qualityHint");
    el.output = $("#output");
    el.limit = $("#limit");
    el.all = $("#all");
    el.allLabel = $("#allLabel");
    el.folderByAuthor = $("#folderByAuthor");
    el.force = $("#force");
    el.proxy = $("#proxy");
    el.rate = $("#rate");
    el.engine = $("#engine");
    el.preferH264 = $("#preferH264");
    el.info = $("#info");
    el.thumbnail = $("#thumbnail");
    el.engineField = $("#engineField");
    el.loginStrip = $("#loginStrip");
    el.loginTitle = $("#loginTitle");
    el.loginText = $("#loginText");
    el.loginBtn = $("#loginBtn");
    el.startBtn = $("#startBtn");
    el.startHint = $("#startHint");
    el.stopBtn = $("#stopBtn");
    el.jobState = $("#jobState");
    el.jobPercent = $("#jobPercent");
    el.jobStage = $("#jobStage");
    el.jobProgress = $("#jobProgress");
    el.jobBar = $("#jobBar");
    el.jobPlatform = $("#jobPlatform");
    el.jobStarted = $("#jobStarted");
    el.jobOutput = $("#jobOutput");
    el.log = $("#log");
    el.logEmpty = $("#logEmpty");
    el.toast = $("#toast");
    el.serviceBanner = $("#serviceBanner");
    el.envGrid = $("#envGrid");
    el.railStatus = $("#railStatus");
    el.railVersion = $("#railVersion");
    el.footerVersion = $("#footerVersion");
    el.exitLayer = $("#exitLayer");
    el.folderLayer = $("#folderLayer");
    el.folderList = $("#folderList");
    el.folderPath = $("#folderPath");
    el.menuBtn = $("#menuBtn");
    el.railScrim = $("#railScrim");
    el.sidebar = $("#sidebar");
  }

  function applySettings() {
    const settings = state.settings || {};
    if (settings.platform === "bilibili" || settings.platform === "douyin") {
      state.platform = settings.platform;
    }
    if (settings.quality && typeof settings.quality === "object") {
      for (const platform of ["bilibili", "douyin"]) {
        if (typeof settings.quality[platform] === "string") {
          state.quality[platform] = settings.quality[platform];
        }
      }
    }
    if (el.output) el.output.value = settings.output || BOOT.defaultOutput || "";
    if (el.limit) el.limit.value = settings.limit || "0";
    if (el.all) el.all.checked = Boolean(settings.all);
    if (el.folderByAuthor) el.folderByAuthor.checked = Boolean(settings.folder_by_author);
    if (el.force) el.force.checked = Boolean(settings.force);
    if (el.proxy) el.proxy.value = settings.proxy || "";
    if (el.rate) el.rate.value = settings.limit_rate || "";
    if (el.engine) el.engine.value = settings.engine || "auto";
    if (el.preferH264) el.preferH264.checked = Boolean(settings.prefer_h264);
    if (el.info) el.info.checked = Boolean(settings.info);
    if (el.thumbnail) el.thumbnail.checked = Boolean(settings.thumbnail);
    if (BOOT.version) {
      if (el.railVersion) el.railVersion.textContent = `v${BOOT.version}`;
      if (el.footerVersion) el.footerVersion.textContent = `v${BOOT.version}`;
    }
  }

  function normalizeQuality() {
    const items = qualityItems();
    const valid = new Set(items.map((item) => item.value));
    const current = state.quality[state.platform];
    if (!valid.has(current)) {
      state.quality[state.platform] = items[0]?.value || "best";
    }
  }

  /* ------------------------------------------------------------------ */
  /* Platform / quality / login                                          */
  /* ------------------------------------------------------------------ */

  function setPlatform(platform, scroll = false) {
    if (!["bilibili", "douyin"].includes(platform)) return;
    if (isRunning() && platform !== state.platform) {
      toast("任务运行中，完成后可以切换平台");
      return;
    }
    state.platform = platform;
    state.levels = null; // 换平台先回到默认档位，等新链接的探测结果
    persistSettings();
    renderPlatform();
    scheduleQualityProbe();
    if (scroll) $("#workbench")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderPlatform() {
    const copy = PLATFORM_COPY[state.platform];
    document.body.dataset.platform = state.platform;
    $$(".platform-tab").forEach((tab) => {
      const active = tab.dataset.platform === state.platform;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.disabled = isRunning();
    });
    if (el.links) el.links.placeholder = copy.placeholder;
    if (el.qualityHint) el.qualityHint.textContent = copy.qualityHint;
    if (el.allLabel) el.allLabel.textContent = copy.allLabel;
    $$(".douyin-only").forEach((node) => {
      node.hidden = state.platform !== "douyin";
    });
    normalizeQuality();
    renderQuality();
    renderLogin();
    updateLinkCount();
    updateButtonState();
  }

  function renderQuality() {
    if (!el.qualityGrid) return;
    const items = qualityItems();
    let selected = state.quality[state.platform];
    if (!items.some((item) => item.value === selected)) {
      selected = items[0]?.value || "best";
      state.quality[state.platform] = selected;
    }

    el.qualityGrid.innerHTML = items
      .map(
        (item) => `
        <button class="quality-chip ${item.value === selected ? "active" : ""}" type="button"
          role="radio" aria-checked="${item.value === selected}" data-quality="${item.value}"
          ${item.disabled ? "disabled" : ""}>
          <strong>${escapeHTML(item.label)}</strong>
          <small>${escapeHTML(item.note || " ")}</small>
        </button>`
      )
      .join("");
    $$(".quality-chip", el.qualityGrid).forEach((button) => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        state.quality[state.platform] = button.dataset.quality;
        persistSettings();
        renderQuality();
        updateLinkCount();
      });
    });
    renderQualityHint();
  }

  /** 探测到的真实档位（按清晰度从高到低）；没有探测结果时返回 null。 */
  function probeLevels() {
    const data = state.levels;
    if (!data || !Array.isArray(data.levels) || !data.levels.length) return null;
    return [...data.levels].sort(
      (a, b) => Number(b.height || 0) - Number(a.height || 0) || Number(b.fps || 0) - Number(a.fps || 0),
    );
  }

  /** 清晰度按钮：有探测结果就直接用视频给的档位，否则用平台默认档位。 */
  function qualityItems() {
    const levels = probeLevels();
    if (!levels) {
      return qualities().map((item) => ({
        value: item.value,
        label: item.label,
        note: item.note || "",
        disabled: false,
      }));
    }
    const items = [{ value: "best", label: "最佳", note: "跟随账号", disabled: false }];
    for (const level of levels) {
      items.push({
        value: String(level.value ?? level.height),
        label: level.label || `${level.height}P`,
        note: levelNote(level),
        disabled: !level.available,
      });
    }
    return items;
  }

  function levelNote(level) {
    if (!level.available) return level.needVip ? "需大会员" : "不可用";
    if (Number(level.fps) >= 50) return "60 帧";
    return level.needVip ? "大会员" : "";
  }

  function renderQualityHint() {
    if (!el.qualityHint) return;
    const levels = probeLevels();
    if (!levels) {
      el.qualityHint.textContent = PLATFORM_COPY[state.platform].qualityHint;
      return;
    }
    const title = state.levels?.title ? `《${truncate(state.levels.title, 26)}》` : "该视频";
    const top = levels.find((level) => level.available);
    el.qualityHint.textContent = top
      ? `${title} 最高可用 ${top.label}`
      : `${title} 读不到可用清晰度`;
  }

  function renderLogin() {
    if (!el.loginStrip || !el.loginBtn) return;
    const platform = state.platform;
    const status = state.status?.[platform];
    const name = PLATFORM_COPY[platform].loginName;
    const logged = Boolean(status?.loggedIn);
    el.loginStrip.classList.toggle("warn", !logged);
    const dot = $(".status-dot", el.loginStrip);
    if (dot) dot.className = `status-dot ${logged ? "ok" : "warn"}`;
    if (!status) {
      el.loginTitle.textContent = `正在读取${name}登录态`;
      el.loginText.textContent = "请稍候…";
    } else {
      el.loginTitle.textContent = logged ? `${name}登录有效` : `${name}尚未登录`;
      el.loginText.textContent = status.text || "点击右侧按钮完成登录";
    }
    el.loginBtn.querySelector("span").textContent = logged ? "重新登录" : `登录${name}`;
    el.loginBtn.disabled = !state.online || isRunning();
  }

  /* ------------------------------------------------------------------ */
  /* Links / button state                                                */
  /* ------------------------------------------------------------------ */

  function getLinks() {
    const seen = new Set();
    const links = [];
    for (const raw of String(el.links?.value || "").split("\n")) {
      const line = raw.trim();
      if (!line || seen.has(line)) continue;
      seen.add(line);
      links.push(line);
    }
    return links;
  }

  function updateLinkCount() {
    const links = getLinks();
    if (el.linkCount) el.linkCount.textContent = `${links.length} 条链接`;
    updateButtonState();
  }

  /* 读取当前链接实际提供的清晰度                                 */
  let qualityTimer = 0;

  function scheduleQualityProbe() {
    window.clearTimeout(qualityTimer);
    const links = getLinks();
    if (links.length !== 1) {
      state.levels = null;
      renderQuality();
      return;
    }
    const platform = state.platform;
    qualityTimer = window.setTimeout(() => probeQuality(platform, links[0]), 600);
  }

  async function probeQuality(platform, link) {
    if (!state.online) return;
    try {
      const data = await api("/api/qualities", {
        method: "POST",
        body: JSON.stringify({ platform, link }),
      });
      const current = getLinks();
      if (state.platform !== platform || current.length !== 1 || current[0] !== link) return;
      state.levels = data;
    } catch (_) {
      /* 读不到档位就退回默认档位，不打扰用户 */
      state.levels = null;
    }
    renderQuality();
  }

  function updateButtonState() {
    if (!el.startBtn) return;
    const links = getLinks();
    const running = isRunning();
    const needsFfmpeg =
      state.platform === "bilibili" && Boolean(state.status?.ffmpeg) && !state.status.ffmpeg.ok;
    const canStart =
      state.online && links.length > 0 && !running && Boolean(el.output?.value.trim()) && !needsFfmpeg;
    el.startBtn.disabled = !canStart;
    const label = el.startBtn.querySelector("span");
    if (label) {
      label.textContent = running
        ? state.job?.kind === "login"
          ? "登录进行中"
          : "任务运行中"
        : "开始下载";
    }
    el.startBtn.classList.toggle("busy", running);
    if (el.loginBtn) el.loginBtn.disabled = !state.online || running;
    if (el.stopBtn) el.stopBtn.disabled = !running;
    if (!state.online) {
      if (el.startHint) el.startHint.textContent = "本地服务未连接，请运行 start.bat。";
    } else if (running) {
      if (el.startHint) el.startHint.textContent = "任务正在运行；可以停止，日志会继续保留。";
    } else if (!links.length) {
      if (el.startHint) el.startHint.textContent = "粘贴链接后即可开始。";
    } else if (needsFfmpeg) {
      if (el.startHint) el.startHint.textContent = "未找到 ffmpeg，B 站下载需要先安装。";
    } else if (!el.output?.value.trim()) {
      if (el.startHint) el.startHint.textContent = "请填写保存目录。";
    } else {
      if (el.startHint) {
        el.startHint.textContent = `${links.length} 条链接 · ${qualityLabel(state.quality[state.platform])} · 写入本机`;
      }
    }
    $$(".platform-tab").forEach((tab) => {
      tab.disabled = running;
    });
  }

  /* ------------------------------------------------------------------ */
  /* Download + jobs                                                     */
  /* ------------------------------------------------------------------ */

  function collectOptions() {
    return {
      all: Boolean(el.all?.checked),
      limit: Number(el.limit?.value || 0),
      folder_by_author: Boolean(el.folderByAuthor?.checked),
      force: Boolean(el.force?.checked),
      proxy: el.proxy?.value.trim() || "",
      limit_rate: el.rate?.value.trim() || "",
      engine: el.engine?.value || "auto",
      prefer_h264: Boolean(el.preferH264?.checked),
      info: Boolean(el.info?.checked),
      thumbnail: Boolean(el.thumbnail?.checked),
    };
  }

  async function startDownload() {
    if (!state.online) {
      toast("请先运行 start.bat 启动本地服务", "error");
      showServiceBanner();
      return;
    }
    if (isRunning()) {
      toast("已有任务正在运行");
      return;
    }
    if (state.platform === "bilibili" && state.status?.ffmpeg && !state.status.ffmpeg.ok) {
      await requestFfmpegInstall();
      return;
    }
    const links = getLinks();
    if (!links.length) {
      toast("请先粘贴至少一个链接", "error");
      el.links?.focus();
      return;
    }
    const output = el.output?.value.trim() || "";
    if (!output) {
      toast("请填写保存目录", "error");
      el.output?.focus();
      return;
    }
    if (!state.quality[state.platform]) normalizeQuality();
    const button = el.startBtn;
    button.disabled = true;
    const label = button.querySelector("span");
    if (label) label.textContent = "正在创建…";
    try {
      const data = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({
          platform: state.platform,
          links: el.links.value,
          quality: state.quality[state.platform],
          output,
          options: collectOptions(),
        }),
      });
      beginJob(data.job, true);
      toast("任务已开始");
    } catch (error) {
      toast(error.message || "任务创建失败", "error");
      updateButtonState();
    }
  }

  function beginJob(job, clear = true) {
    state.job = job;
    state.cursor = 0;
    state.jobAnnounced = false;
    if (clear) clearLog();
    renderJob();
    updateButtonState();
    if (isRunning(job)) {
      schedulePoll(300);
    } else {
      loadTerminalLogs(job.id);
    }
  }

  async function loadTerminalLogs(jobId) {
    try {
      const data = await api(`/api/jobs/${jobId}?cursor=0`);
      appendLogs(data.logs);
      state.cursor = data.nextCursor;
      state.job = data.job;
      renderJob();
      updateButtonState();
    } catch (_) {
      /* terminal job logs are a nicety; don't block the page */
    }
  }

  function schedulePoll(delay = 550) {
    window.clearTimeout(state.pollTimer);
    if (!isRunning()) return;
    state.pollTimer = window.setTimeout(pollJob, delay);
  }

  async function pollJob() {
    if (!state.job || !isRunning()) return;
    const jobId = state.job.id;
    try {
      const data = await api(`/api/jobs/${jobId}?cursor=${state.cursor}`);
      state.cursor = data.nextCursor;
      appendLogs(data.logs);
      state.job = data.job;
      renderJob();
      updateButtonState();
      if (isRunning()) {
        schedulePoll();
      } else {
        finishJob();
      }
    } catch (error) {
      if (state.online) {
        schedulePoll(1400);
      }
    }
  }

  async function finishJob() {
    const job = state.job;
    if (!job || state.jobAnnounced) return;
    state.jobAnnounced = true;
    updateButtonState();
    renderJob();
    if (job.status === "succeeded") {
      toast(
        job.kind === "login"
          ? "登录成功，登录态已保存"
          : job.kind === "install"
            ? "ffmpeg 安装完成"
            : "下载完成",
      );
    } else if (job.status === "failed") {
      toast(truncate(job.error || "任务失败", 80), "error");
    } else if (job.status === "cancelled") {
      toast("任务已停止");
    }
    if (job.kind === "login" && job.status === "succeeded") {
      await refreshStatus();
    } else if (job.kind === "install") {
      await refreshStatus();
    } else if (job.kind === "download" && job.status === "succeeded") {
      refreshStatus({ quiet: true });
    }
  }

  async function stopJob() {
    if (!state.job || !isRunning()) return;
    el.stopBtn.disabled = true;
    try {
      const data = await api(`/api/jobs/${state.job.id}/stop`, { method: "POST" });
      state.job = data.job;
      renderJob();
      updateButtonState();
      toast("已请求停止，等待当前进程收尾");
    } catch (error) {
      toast(error.message || "停止失败", "error");
      el.stopBtn.disabled = false;
    }
  }

  async function startLogin(platform = state.platform) {
    if (!state.online) {
      toast("请先运行 start.bat 启动本地服务", "error");
      return;
    }
    if (isRunning()) {
      toast("请先等待当前任务结束");
      return;
    }
    try {
      const data = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({ platform }),
      });
      beginJob(data.job, true);
      toast(`正在打开浏览器，请完成${PLATFORM_COPY[platform].loginName}登录`);
    } catch (error) {
      toast(error.message || "登录启动失败", "error");
    }
  }

  async function requestFfmpegInstall(quiet = false) {
    if (!state.online) {
      toast("请先运行 start.bat 启动本地服务", "error");
      return false;
    }
    if (isRunning()) {
      toast("请先等待当前任务结束");
      return false;
    }
    try {
      const data = await api("/api/install-ffmpeg", { method: "POST" });
      beginJob(data.job, true);
      if (!quiet) toast("正在自动下载 ffmpeg，装好后会提示你");
      return true;
    } catch (error) {
      toast(error.message || "ffmpeg 安装启动失败", "error");
      return false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Job display                                                         */
  /* ------------------------------------------------------------------ */

  function renderJob() {
    if (!el.jobState) return;
    const job = state.job;
    el.jobState.className = "run-state";
    el.jobProgress.classList.remove("indeterminate");

    if (!job) {
      el.jobState.textContent = "空闲";
      el.jobPercent.parentElement.classList.remove("login-mode");
      el.jobPercent.textContent = "00";
      el.jobStage.textContent = "等待任务";
      el.jobBar.style.width = "0%";
      el.jobProgress.setAttribute("aria-valuenow", "0");
      el.jobPlatform.textContent = "—";
      el.jobStarted.textContent = "—";
      el.jobOutput.textContent = "—";
      el.jobOutput.title = "";
      document.title = "autoDownloads · 本地视频下载台";
      return;
    }

    const running = isRunning(job);
    const loginMode = job.kind === "login";
    const stateName = {
      queued: "排队中",
      running: job.kind === "login" ? "登录中" : "运行中",
      succeeded: "已完成",
      failed: "失败",
      cancelled: "已停止",
    }[job.status] || job.status;
    el.jobState.textContent = stateName;
    if (job.status === "running" || job.status === "queued") el.jobState.classList.add("running");
    else if (job.status === "succeeded") el.jobState.classList.add("success");
    else if (job.status === "failed") el.jobState.classList.add("error");
    else if (job.status === "cancelled") el.jobState.classList.add("cancelled");

    el.jobPercent.parentElement.classList.toggle("login-mode", loginMode);
    const zeroTerminal =
      !loginMode && !running && job.status !== "succeeded" && Number(job.progress || 0) <= 0;
    if (zeroTerminal) el.jobPercent.parentElement.classList.add("login-mode");
    const indeterminate = Boolean(job.indeterminate) && running;
    if (loginMode) {
      el.jobPercent.textContent = job.status === "succeeded" ? "OK" : "—";
    } else if (zeroTerminal) {
      el.jobPercent.textContent = "—";
    } else {
      el.jobPercent.textContent = indeterminate ? "—" : String(Math.round(job.progress || 0)).padStart(2, "0");
    }
    if (indeterminate) el.jobProgress.classList.add("indeterminate");
    el.jobBar.style.width = `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`;
    el.jobProgress.setAttribute("aria-valuenow", String(Math.round(job.progress || 0)));

    if (job.kind === "login") {
      if (job.status === "succeeded") el.jobStage.textContent = "登录成功，登录态已保存";
      else if (job.status === "failed") el.jobStage.textContent = truncate(job.error || "登录失败", 56);
      else if (job.status === "cancelled") el.jobStage.textContent = "登录已取消";
      else el.jobStage.textContent = "请在弹出的浏览器中完成登录…";
    } else if (job.status === "succeeded") {
      el.jobStage.textContent = job.summary ? `下载完成 · ${job.summary}` : "下载完成";
    } else if (job.status === "failed") {
      el.jobStage.textContent = truncate(job.error || "任务失败", 64);
    } else if (job.status === "cancelled") {
      el.jobStage.textContent = "任务已停止，已写入的文件保留";
    } else if (job.itemTotal > 0) {
      el.jobStage.textContent = `第 ${job.itemIndex || 1} / ${job.itemTotal} 个 · 正在处理`;
    } else {
      el.jobStage.textContent = "正在解析链接…";
    }

    const platformName =
      job.platform === "bilibili" ? "B 站" : job.platform === "douyin" ? "抖音" : "环境";
    el.jobPlatform.textContent =
      job.kind === "login"
        ? `${platformName}登录`
        : job.kind === "install"
          ? "ffmpeg 安装"
          : platformName;
    el.jobStarted.textContent = formatClock(job.startedAt);
    el.jobOutput.textContent = job.outputDir || el.output?.value || "—";
    el.jobOutput.title = job.outputDir || el.output?.value || "";
    el.stopBtn.disabled = !running;
    if (running) {
      document.title = loginMode
        ? `登录中 · autoDownloads`
        : `${el.jobPercent.textContent}% · ${platformName}任务 · autoDownloads`;
    } else {
      document.title = `${stateName} · autoDownloads`;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Logs                                                                */
  /* ------------------------------------------------------------------ */

  function clearLog(showPlaceholder = false) {
    if (!el.log) return;
    state.logs = [];
    el.log.innerHTML = "";
    if (showPlaceholder) {
      el.log.innerHTML =
        '<div class="log-empty"><span class="prompt-mark">&gt;_</span><p>等待任务。下载日志会实时出现在这里。</p></div>';
    }
  }

  function appendLogs(entries) {
    if (!Array.isArray(entries) || !entries.length) return;
    const nearBottom =
      el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 48;
    const fragment = document.createDocumentFragment();
    for (const entry of entries) {
      state.logs.push(entry);
      if (state.logs.length > 2000) state.logs.shift();
      const line = document.createElement("div");
      line.className = `log-line ${entry.level || "info"}`;
      const time = document.createElement("span");
      time.className = "log-time";
      time.textContent = entry.time || "";
      const text = document.createElement("span");
      text.className = "log-text";
      text.textContent = entry.text || "";
      line.append(time, text);
      fragment.append(line);
    }
    const empty = $(".log-empty", el.log);
    if (empty) empty.remove();
    el.log.append(fragment);
    if (nearBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  async function copyLog() {
    if (!state.logs.length) {
      toast("还没有日志可复制");
      return;
    }
    const text = state.logs
      .map((entry) => `[${entry.time || "--:--:--"}] ${entry.text || ""}`)
      .join("\n");
    const ok = await copyText(text);
    toast(ok ? "日志已复制" : "复制失败，请手动选择日志");
  }

  /* ------------------------------------------------------------------ */
  /* Status                                                              */
  /* ------------------------------------------------------------------ */

  async function refreshStatus(options = {}) {
    try {
      const data = await api("/api/status");
      state.status = data;
      state.online = true;
      const globalDot = $(".local-pill .status-dot");
      if (globalDot) globalDot.className = "status-dot ok";
      hideServiceBanner();
      if (el.output && !el.output.value.trim()) {
        el.output.value = data.outputDefault || BOOT.defaultOutput || "";
      }
      normalizeQuality();
      renderPlatform();
      renderStatus();
      return data;
    } catch (error) {
      if (!options.quiet) {
        setOffline(error.message || "无法连接本地服务");
      }
      return null;
    }
  }

  function renderStatus() {
    const status = state.status;
    if (!status) return;
    const bili = status.bilibili || {};
    const douyin = status.douyin || {};
    const ffmpeg = status.ffmpeg || {};
    const browser = status.browser || {};

    if (el.railStatus) {
      el.railStatus.innerHTML = `
        <div class="rail-status-row"><span class="status-dot ${ffmpeg.ok ? "ok" : "error"}"></span><span>FFmpeg</span><em>${ffmpeg.ok ? "可用" : "缺失"}</em></div>
        <div class="rail-status-row"><span class="status-dot ${bili.loggedIn ? "ok" : "warn"}"></span><span>B 站登录</span><em>${bili.loggedIn ? "已登录" : "未登录"}</em></div>
        <div class="rail-status-row"><span class="status-dot ${douyin.loggedIn ? "ok" : "warn"}"></span><span>抖音配置</span><em>${douyin.loggedIn ? "已登录" : "待登录"}</em></div>
      `;
    }

    if (el.envGrid) {
      el.envGrid.innerHTML = `
        <article class="env-card">
          <span class="env-index">01 / FFMPEG</span>
          <h3><span class="status-dot ${ffmpeg.ok ? "ok" : "error"}"></span>${ffmpeg.ok ? "可用" : "未找到"}</h3>
          <p title="${escapeHTML(ffmpeg.path || "")}">${ffmpeg.ok ? escapeHTML(truncate(ffmpeg.path, 64)) : "B 站下载需要 ffmpeg 合并音视频。"}${status.freeGb != null ? `<br>磁盘剩余：${escapeHTML(status.freeGb)} GB` : ""}</p>
          <div class="mini-actions">
            ${ffmpeg.ok ? "" : '<button class="mini-action" type="button" data-install-ffmpeg>一键下载 ffmpeg</button>'}
            ${ffmpeg.ok ? "" : '<button class="mini-action" type="button" data-copy-command="winget install Gyan.FFmpeg">复制安装命令</button>'}
            <button class="mini-action" type="button" data-refresh-status>重新检查</button>
          </div>
        </article>
        <article class="env-card">
          <span class="env-index">02 / B 站</span>
          <h3><span class="status-dot ${bili.loggedIn ? "ok" : "warn"}"></span>${bili.loggedIn ? "登录有效" : "未登录"}</h3>
          <p>${escapeHTML(bili.text || "未检测到登录态")}</p>
          <button class="mini-action" type="button" data-login-platform="bilibili">${bili.loggedIn ? "重新登录" : "登录 B 站"}</button>
        </article>
        <article class="env-card">
          <span class="env-index">03 / 抖音</span>
          <h3><span class="status-dot ${douyin.loggedIn ? "ok" : "warn"}"></span>${douyin.loggedIn ? "配置可用" : "待登录"}</h3>
          <p>${escapeHTML(douyin.text || "首次使用需要在浏览器中登录")}<br>${escapeHTML(browser.ok ? `浏览器：${truncate(browser.path, 46)}` : "未找到 Chromium 浏览器")}</p>
          <button class="mini-action" type="button" data-login-platform="douyin">${douyin.loggedIn ? "重新登录" : "登录抖音"}</button>
        </article>
      `;
    }

    renderLogin();
    updateButtonState();
  }

  function showServiceBanner() {
    if (!el.serviceBanner) return;
    el.serviceBanner.hidden = false;
  }

  function hideServiceBanner() {
    if (el.serviceBanner) el.serviceBanner.hidden = true;
  }

  function setOffline(message) {
    state.online = false;
    const globalDot = $(".local-pill .status-dot");
    if (globalDot) globalDot.className = "status-dot error";
    if (el.serviceBanner) {
      el.serviceBanner.hidden = false;
      const strong = $("strong", el.serviceBanner);
      const span = $("span", el.serviceBanner);
      if (strong) strong.textContent = "本地服务未连接";
      if (span) {
        span.innerHTML = `${escapeHTML(message || "请运行 start.bat 启动下载服务")}。`;
      }
    }
    if (el.railStatus) {
      el.railStatus.innerHTML =
        '<div class="rail-status-row"><span class="status-dot error"></span><span>服务</span><em>未连接</em></div>';
    }
    updateButtonState();
    renderLogin();
  }

  /* ------------------------------------------------------------------ */
  /* Open folder / exit                                                  */
  /* ------------------------------------------------------------------ */

  function formatSize(bytes) {
    const value = Number(bytes || 0);
    if (!value) return "";
    if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
    if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    return `${Math.max(1, Math.round(value / 1024))} KB`;
  }

  async function openFolder() {
    if (!state.online) {
      toast("本地服务未连接", "error");
      return;
    }
    const path = state.job?.outputDir || el.output?.value.trim() || "";
    try {
      const data = await api("/api/open-folder", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      toast(`已打开：${data.path}`);
    } catch (_error) {
      // 系统不允许调起资源管理器时，直接在页面里列目录。
      const shown = await showFolderLayer(path);
      if (!shown) toast("打开目录失败", "error");
    }
  }

  async function showFolderLayer(path) {
    if (!el.folderLayer) return false;
    let data;
    try {
      data = await api("/api/list", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
    } catch (error) {
      toast(error.message || "读取目录失败", "error");
      return false;
    }
    if (el.folderPath) el.folderPath.textContent = data.path || path || "";
    if (el.folderList) {
      const rows = (data.entries || []).slice(0, 200).map((entry) => {
        const meta = entry.dir ? "文件夹" : `${formatSize(entry.size)} · ${entry.mtime}`;
        return `
          <div class="folder-row">
            <span class="folder-name" title="${escapeHTML(entry.name)}">${entry.dir ? "▸ " : ""}${escapeHTML(entry.name)}</span>
            <span class="folder-meta">${escapeHTML(meta)}</span>
          </div>`;
      });
      el.folderList.innerHTML = rows.length
        ? rows.join("")
        : '<div class="folder-empty">目录里还没有文件。</div>';
    }
    el.folderLayer.hidden = false;
    document.body.classList.add("no-scroll");
    return true;
  }

  function closeFolderLayer() {
    if (el.folderLayer) el.folderLayer.hidden = true;
    if (el.exitLayer?.hidden !== false) document.body.classList.remove("no-scroll");
  }

  function openExit() {
    if (!el.exitLayer) return;
    el.exitLayer.hidden = false;
    document.body.classList.add("no-scroll");
    $("#confirmExit")?.focus();
  }

  function closeExit() {
    if (!el.exitLayer) return;
    el.exitLayer.hidden = true;
    if (!document.body.classList.contains("nav-open")) {
      document.body.classList.remove("no-scroll");
    }
  }

  async function confirmExit() {
    closeExit();
    try {
      await api("/api/shutdown", { method: "POST" });
      state.online = false;
      if (el.serviceBanner) {
        el.serviceBanner.hidden = false;
        const strong = $("strong", el.serviceBanner);
        const span = $("span", el.serviceBanner);
        if (strong) strong.textContent = "本地服务已退出";
        if (span) span.textContent = "可以关闭这个页面。下次使用请重新运行 start.bat。";
      }
      updateButtonState();
      toast("本地服务已退出");
    } catch (error) {
      toast(error.message || "退出失败", "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Mobile rail                                                         */
  /* ------------------------------------------------------------------ */

  function openRail() {
    document.body.classList.add("nav-open", "no-scroll");
    if (el.railScrim) el.railScrim.hidden = false;
    el.menuBtn?.setAttribute("aria-expanded", "true");
    el.sidebar?.removeAttribute("inert");
  }

  function closeRail(restoreFocus = false) {
    document.body.classList.remove("nav-open");
    if (el.exitLayer?.hidden !== false) document.body.classList.remove("no-scroll");
    if (el.railScrim) el.railScrim.hidden = true;
    el.menuBtn?.setAttribute("aria-expanded", "false");
    if (window.innerWidth <= 900) el.sidebar?.setAttribute("inert", "");
    if (restoreFocus) el.menuBtn?.focus();
  }

  function syncRailInert() {
    if (!el.sidebar) return;
    if (window.innerWidth <= 900 && !document.body.classList.contains("nav-open")) {
      el.sidebar.setAttribute("inert", "");
    } else {
      el.sidebar.removeAttribute("inert");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Bind                                                                */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    $$(".platform-tab").forEach((tab) => {
      tab.addEventListener("click", () => setPlatform(tab.dataset.platform));
    });
    $$("[data-jump-platform]").forEach((button) => {
      button.addEventListener("click", () => {
        setPlatform(button.dataset.jumpPlatform, true);
        closeRail(false);
      });
    });
    $$("[data-login-platform]").forEach((button) => {
      button.addEventListener("click", () => startLogin(button.dataset.loginPlatform));
    });

    el.links?.addEventListener("input", () => {
      updateLinkCount();
      persistSettings();
      scheduleQualityProbe();
    });
    $("#clearLinks")?.addEventListener("click", () => {
      el.links.value = "";
      updateLinkCount();
      persistSettings();
      scheduleQualityProbe();
      el.links.focus();
    });

    el.form?.addEventListener("submit", (event) => {
      event.preventDefault();
      startDownload();
    });
    el.form?.addEventListener("input", () => {
      persistSettings();
      updateLinkCount();
    });
    el.form?.addEventListener("change", () => {
      persistSettings();
      updateLinkCount();
    });

    el.form?.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        startDownload();
      }
    });

    el.loginBtn?.addEventListener("click", () => startLogin(state.platform));
    el.stopBtn?.addEventListener("click", stopJob);
    $("#topOpenFolder")?.addEventListener("click", openFolder);
    $("#openFolderBtn")?.addEventListener("click", openFolder);
    $("#runOpenFolder")?.addEventListener("click", openFolder);
    $("#copyLogBtn")?.addEventListener("click", copyLog);
    $("#clearLogBtn")?.addEventListener("click", () => clearLog(false));

    $("#shutdownBtn")?.addEventListener("click", openExit);
    $$("[data-exit]").forEach((node) => {
      node.addEventListener("click", closeExit);
    });
    $("#confirmExit")?.addEventListener("click", confirmExit);

    el.menuBtn?.addEventListener("click", () => {
      if (document.body.classList.contains("nav-open")) closeRail(true);
      else openRail();
    });
    el.railScrim?.addEventListener("click", () => closeRail(true));

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (el.folderLayer?.hidden === false) {
          closeFolderLayer();
        } else if (el.exitLayer?.hidden === false) {
          closeExit();
        } else if (document.body.classList.contains("nav-open")) {
          closeRail(true);
        }
      }
    });

    el.folderLayer?.addEventListener("click", (event) => {
      if (event.target.closest("[data-folder='close']")) {
        closeFolderLayer();
        return;
      }
      if (event.target.closest("[data-folder='copy']")) {
        copyText(el.folderPath?.textContent || "").then((ok) => {
          toast(ok ? "路径已复制" : "复制失败，请手动选择路径");
        });
      }
    });

    window.addEventListener("resize", () => {
      if (window.innerWidth > 900) closeRail(false);
      syncRailInert();
    });

    // Environment login buttons are re-rendered by refreshStatus.
    el.envGrid?.addEventListener("click", (event) => {
      const loginButton = event.target.closest("[data-login-platform]");
      if (loginButton) {
        startLogin(loginButton.dataset.loginPlatform);
        return;
      }
      const copyButton = event.target.closest("[data-copy-command]");
      if (copyButton) {
        copyText(copyButton.dataset.copyCommand).then((ok) => {
          toast(ok ? "安装命令已复制，请在终端中执行" : "复制失败，请手动输入安装命令");
        });
        return;
      }
      if (event.target.closest("[data-install-ffmpeg]")) {
        requestFfmpegInstall();
        return;
      }
      if (event.target.closest("[data-refresh-status]")) {
        refreshStatus();
        toast("正在重新检查本机环境");
      }
    });
  }

  function init() {
    cacheElements();
    applySettings();
    bindEvents();
    syncRailInert();
    renderPlatform();
    updateLinkCount();
    updateButtonState();
    renderJob();
    if (!state.online) {
      setOffline("请双击 start.bat 启动本地下载服务");
      return;
    }
    hideServiceBanner();
    scheduleQualityProbe();
    refreshStatus().then((status) => {
      if (status?.job) {
        beginJob(status.job, true);
      }
      if (status?.runtime?.elevated) {
        toast(
          "当前以管理员身份运行，Windows 可能拒绝打开资源管理器；建议普通双击 start.bat",
          "error",
        );
      }
    });
  }

  init();
})();
