"use strict";

const appState = {
  channels: null,
  bookmarks: null,
  selectedChannel: null,
  channelSelectionInitialized: false,
  renderToken: 0,
  liveHls: null,
  liveVisibilityHandler: null,
  liveResumeOnVisible: false,
  liveConnectTimer: null,
  bufferProfileCache: null,
};

const routeMeta = {
  epg: ["节目单", "频道与节目单"],
  channels: ["频道管理", "管理频道"],
  sources: ["直播源", "管理直播线路"],
  subscriptions: ["我的订阅", "订阅规则"],
  recordings: ["录制与处理", "录制任务"],
  summaries: ["节目摘要", "智能摘要"],
  bookmarks: ["我的收藏", "收藏内容"],
  system: ["通用设置", "系统设置"],
};

const statusMeta = {
  queued: ["等待录制", "info"],
  recording: ["录制中", "danger"],
  completed: ["录制完成", "success"],
  asr_processing: ["转写中", "warning"],
  asr_done: ["转写完成", "success"],
  summarizing: ["摘要中", "warning"],
  summarized: ["处理完成", "success"],
  failed: ["录制失败", "danger"],
  asr_failed: ["转写失败", "danger"],
  summary_failed: ["摘要失败", "danger"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function localDateValue(date = new Date()) {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 10);
}

function formatDateTime(value, options = {}) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false, ...options,
  }).format(date);
}

function formatTime(value) {
  return formatDateTime(value, { month: undefined, day: undefined });
}

function formatBytes(value) {
  if (!Number.isFinite(Number(value))) return "—";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function shorten(value, length = 120) {
  const text = String(value || "").trim();
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function idOf(value) {
  return typeof value === "string" ? value : value?.$oid || "";
}

function badge(status) {
  const [label, tone] = statusMeta[status] || [status || "未知", ""];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
}

function channelCategory(channel) {
  if (channel.category) return channel.category;
  const name = `${channel.name || ""} ${channel.channel_id || ""}`.toLowerCase();
  if (/^(cctv|cgtn)/i.test(channel.channel_id || "")) return "央视频道";
  if (/(nhk|bbc|cna|al jazeera|france 24|arirang|cgtn)/i.test(name)) return "国际频道";
  if (name.includes("卫视")) return "卫视频道";
  return "地方与其他";
}

function channelMonogram(channel) {
  const value = channel.name || channel.channel_id || "TV";
  if (/^CCTV-/i.test(value)) return value.replace(/^CCTV-/i, "C");
  return value.slice(0, 2).toUpperCase();
}

function streamQualityLabel(quality) {
  if (!quality) return "";
  if (quality.valid === false) return " · 不可用";
  const parts = [];
  const ratio = Number(quality.download_ratio);
  const mbps = Number(quality.required_mbps);
  if (Number.isFinite(ratio)) parts.push(`占用 ${Math.round(ratio)}%`);
  if (Number.isFinite(mbps)) parts.push(`${mbps.toFixed(1)}Mbps`);
  if (quality.sustainable === false) parts.push("偏慢");
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

async function loadBufferProfile(force = false) {
  if (!appState.bufferProfileCache || force) {
    const data = await api("/api/settings/buffer");
    appState.bufferProfileCache = data.profiles[data.profile];
  }
  return appState.bufferProfileCache;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  const token = sessionStorage.getItem("iptv_api_token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = options.body;
  if (body && typeof body !== "string") {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  const response = await fetch(path, { ...options, headers, body });
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "object" ? payload.detail : payload;
    const error = new Error(message || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function toast(message, tone = "") {
  const node = document.createElement("div");
  node.className = `toast ${tone}`;
  node.textContent = message;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), 3600);
}

function openModal(title, html, wide = false) {
  appState.liveDetailToken = null;
  const modal = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $(".modal-panel", modal).classList.toggle("wide", wide);
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => $("button, input, select, textarea", $("#modal-body"))?.focus());
}

function closeModal() {
  appState.liveDetailToken = null;
  if (appState.liveConnectTimer) {
    clearTimeout(appState.liveConnectTimer);
    appState.liveConnectTimer = null;
  }
  if (appState.liveVisibilityHandler) {
    document.removeEventListener("visibilitychange", appState.liveVisibilityHandler);
    appState.liveVisibilityHandler = null;
  }
  if (appState.liveHls) {
    appState.liveHls.destroy();
    appState.liveHls = null;
  }
  appState.liveResumeOnVisible = false;
  const video = $("#modal video");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.load();
  }
  $("#modal").hidden = true;
  $("#modal-body").innerHTML = "";
  document.body.style.overflow = "";
}

function emptyState(title, detail, compact = false) {
  return `<div class="empty-state ${compact ? "compact" : ""}"><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>`;
}

function loading() {
  $("#view").innerHTML = '<div class="page-loading"><span class="spinner"></span><p>正在读取 NAS 数据…</p></div>';
}

function renderError(error) {
  const authHint = error.status === 401
    ? '<p>API 已启用访问令牌。可在“系统”页保存本次浏览器会话令牌。</p>'
    : "";
  $("#view").innerHTML = `<div class="error-panel"><h2>页面加载失败</h2><p>${esc(error.message)}</p>${authHint}<button class="button secondary" id="retry-page">重试</button></div>`;
  $("#retry-page")?.addEventListener("click", () => renderRoute());
}

async function loadChannels(force = false) {
  if (!appState.channels || force) appState.channels = await api("/api/channels");
  return appState.channels;
}

async function loadBookmarks(force = false) {
  if (!appState.bookmarks || force) appState.bookmarks = await api("/api/bookmarks");
  return appState.bookmarks;
}

function bookmarkFor(type, itemId) {
  return (appState.bookmarks || []).find(item => item.item_type === type && idOf(item.item_id) === idOf(itemId));
}

async function toggleBookmark(type, itemId) {
  await loadBookmarks();
  const existing = bookmarkFor(type, itemId);
  if (existing) {
    await api(`/api/bookmarks/${encodeURIComponent(existing._id)}`, { method: "DELETE" });
    toast("已取消收藏");
  } else {
    await api("/api/bookmarks", { method: "POST", body: { user: "default", item_type: type, item_id: itemId, note: "" } });
    toast("已加入收藏");
  }
  await loadBookmarks(true);
}

function channelOptions(channels, selected = "", allowEmpty = true) {
  const first = allowEmpty ? '<option value="">全部频道</option>' : "";
  return first + channels.map(channel => `<option value="${esc(channel.channel_id)}" ${channel.channel_id === selected ? "selected" : ""}>${esc(channel.name || channel.channel_id)}</option>`).join("");
}

function channelName(channelId) {
  const channel = (appState.channels || []).find(item => item.channel_id === channelId);
  return channel?.name || channelId;
}

function pageIntro(description, actions = "") {
  return `<div class="page-intro"><p>${esc(description)}</p><div class="button-row">${actions}</div></div>`;
}

async function renderEpg() {
  const channels = await loadChannels();
  const preferredChannel = channels.find(item => item.channel_id === "CCTV-1")?.channel_id || channels[0]?.channel_id;
  if (!appState.channelSelectionInitialized) {
    appState.selectedChannel = preferredChannel;
    appState.channelSelectionInitialized = true;
  } else if (appState.selectedChannel && !channels.some(item => item.channel_id === appState.selectedChannel)) {
    appState.selectedChannel = null;
  }
  const selected = appState.selectedChannel;
  appState.selectedChannel = selected;
  const groups = Object.groupBy ? Object.groupBy(channels, channelCategory) : channels.reduce((all, item) => {
    const key = channelCategory(item); (all[key] ||= []).push(item); return all;
  }, {});
  const orderedGroups = ["央视频道", "卫视频道", "国际频道", "地方与其他"]
    .concat(Object.keys(groups).filter(group => !["央视频道", "卫视频道", "国际频道", "地方与其他"].includes(group)))
    .filter(group => groups[group])
    .map(group => [group, groups[group]]);
  $("#view").innerHTML = `
    ${pageIntro("浏览频道节目单，或搜索未来节目。节目上的“持续订阅”会创建长期关键词规则，并非只录当前一期。")}
    <div class="toolbar">
      <label class="field"><span>日期</span><input class="input" type="date" id="epg-date" value="${localDateValue()}"></label>
      <label class="field grow"><span>搜索节目</span><input class="input" id="epg-search" placeholder="输入节目名称，例如：新闻联播"></label>
      <button class="button secondary" id="epg-search-button" type="button">搜索</button>
    </div>
    <div class="split-layout">
      <section class="panel"><div class="panel-header"><h2>频道</h2><span class="badge">${channels.length} 个</span></div><div class="channel-list" id="channel-list">
        <button class="channel-item ${selected === null ? "active" : ""}" data-all-channels type="button">
          <span class="channel-logo">全</span><span class="channel-copy"><strong>全部频道</strong><small>搜索时不限制频道</small></span>
        </button>
        ${orderedGroups.map(([group, rows]) => `<div class="channel-group-label">${esc(group)}</div>${rows.map(channel => `
          <button class="channel-item ${channel.channel_id === selected ? "active" : ""}" data-channel="${esc(channel.channel_id)}" type="button">
            <span class="channel-logo">${esc(channelMonogram(channel))}</span><span class="channel-copy"><strong>${esc(channel.name || channel.channel_id)}</strong><small>当前优选 · ${esc(channel.source || "聚合线路")}</small></span>
          </button>`).join("")}`).join("")}
      </div></section>
      <section class="panel"><div class="panel-header"><h2 id="epg-panel-title">${esc(selected ? channelName(selected) : "全部频道")}</h2><div class="button-row"><button class="button small" id="watch-selected-channel" type="button" ${selected ? "" : "hidden"}>▶ 观看直播</button><span class="badge info" id="epg-count">加载中</span></div></div><div class="panel-body timeline" id="epg-results"></div></section>
    </div>`;

  const loadProgramme = async () => {
    const channel = appState.selectedChannel;
    const date = $("#epg-date").value;
    const watchButton = $("#watch-selected-channel");
    watchButton.hidden = !channel;
    if (!channel) {
      $("#epg-panel-title").textContent = "全部频道";
      $("#epg-count").textContent = "搜索模式";
      $("#epg-results").innerHTML = emptyState("已选择全部频道", "输入关键词可搜索全部频道；选择左侧频道则只查看或搜索该频道。", true);
      return;
    }
    $("#epg-results").innerHTML = '<div class="page-loading"><span class="spinner"></span></div>';
    const rows = await api(`/api/epg?channel=${encodeURIComponent(channel)}&date=${encodeURIComponent(date)}`);
    renderProgramRows(rows, $("#epg-results"), false);
    $("#epg-count").textContent = `${rows.length} 条`;
    $("#epg-panel-title").textContent = channels.find(item => item.channel_id === channel)?.name || channel;
  };
  const refreshProgramme = () => $("#epg-search").value.trim() ? searchEpg(loadProgramme) : loadProgramme();
  $("#channel-list").addEventListener("click", event => {
    const button = event.target.closest("[data-channel], [data-all-channels]");
    if (!button) return;
    appState.selectedChannel = button.hasAttribute("data-all-channels") ? null : button.dataset.channel;
    $$(".channel-item").forEach(item => item.classList.toggle("active", item === button));
    refreshProgramme().catch(renderError);
  });
  $("#epg-date").addEventListener("change", () => refreshProgramme().catch(renderError));
  $("#watch-selected-channel").addEventListener("click", () => openLivePlayer(appState.selectedChannel, channelName(appState.selectedChannel)));
  $("#epg-search-button").addEventListener("click", () => searchEpg(loadProgramme));
  $("#epg-search").addEventListener("keydown", event => { if (event.key === "Enter") searchEpg(loadProgramme); });
  $("#epg-search").addEventListener("input", event => {
    if (!event.currentTarget.value.trim()) loadProgramme().catch(renderError);
  });
  await loadProgramme();
}

function renderProgramRows(rows, target, searchMode) {
  if (!rows.length) {
    target.innerHTML = emptyState("暂无节目", searchMode ? "没有找到匹配的节目。" : "该频道当天暂无 EPG 数据。", true);
    return;
  }
  const now = Date.now();
  target.innerHTML = rows.map((row, index) => {
    const current = new Date(row.start_time).getTime() <= now && new Date(row.end_time).getTime() > now;
    return `<article class="program-row ${current ? "now" : ""}">
      <div class="program-time">${esc(formatTime(row.start_time))}</div>
      <div class="program-main"><strong>${esc(row.title)}</strong><p>${esc(searchMode ? `${row.channel_id} · ${formatDateTime(row.start_time)}` : shorten(row.description, 110))}</p></div>
      <div class="program-actions"><button class="button small secondary" data-subscribe-program="${index}" type="button">持续订阅</button></div>
    </article>`;
  }).join("");
  target.onclick = event => {
    const button = event.target.closest("[data-subscribe-program]");
    if (!button) return;
    const row = rows[Number(button.dataset.subscribeProgram)];
    openSubscriptionForm(null, { name: row.title, keyword: row.title, channel_id: row.channel_id || appState.selectedChannel });
  };
}

async function searchEpg(restore) {
  const query = $("#epg-search").value.trim();
  if (!query) return restore();
  $("#epg-results").innerHTML = '<div class="page-loading"><span class="spinner"></span></div>';
  const channel = appState.selectedChannel;
  const scope = channel ? `&channel=${encodeURIComponent(channel)}` : "";
  const rows = await api(`/api/epg/search?q=${encodeURIComponent(query)}${scope}&limit=100`);
  $("#watch-selected-channel").hidden = !channel;
  $("#epg-panel-title").textContent = `${channel ? channelName(channel) : "全部频道"} · 搜索：${query}`;
  $("#epg-count").textContent = `${rows.length} 条`;
  renderProgramRows(rows, $("#epg-results"), true);
}

function subscriptionSummary(subscription) {
  if (subscription.match_type === "keyword") {
    return `${subscription.keyword}${subscription.channel_id ? ` · ${subscription.channel_id}` : " · 全部频道"}`;
  }
  const rule = subscription.time_rule || {};
  const configuredWeekdays = rule.weekdays || (rule.weekday === undefined || rule.weekday === null ? [] : [rule.weekday]);
  const weekdays = configuredWeekdays.map(day => "一二三四五六日"[day]).join("、") || "每天";
  return `${subscription.channel_id} · 周${weekdays} ${rule.time || "任意时间"}`;
}

async function renderSubscriptions() {
  const [subscriptions, channels] = await Promise.all([api("/api/subscriptions"), loadChannels()]);
  $("#view").innerHTML = `
    ${pageIntro("管理长期订阅规则。停用不会删除历史录制，可随时重新启用。", '<button class="button" id="new-subscription" type="button">＋ 新建订阅</button>')}
    <section class="panel"><div class="panel-header"><h2>订阅规则</h2><span class="badge">${subscriptions.length} 条</span></div>
      <div class="data-table-wrap">${subscriptions.length ? `<table class="data-table"><thead><tr><th>名称</th><th>匹配规则</th><th>处理能力</th><th>状态</th><th></th></tr></thead><tbody>
        ${subscriptions.map(row => `<tr>
          <td><strong>${esc(row.name)}</strong><small>${esc(row.match_type === "keyword" ? "关键词" : "频道时间")}</small></td>
          <td>${esc(subscriptionSummary(row))}</td>
          <td><span class="badge ${row.asr_enabled ? "success" : ""}">ASR</span> <span class="badge ${row.llm_enabled ? "success" : ""}">摘要</span> <span class="badge ${row.push_enabled ? "success" : ""}">邮件</span></td>
          <td>${row.enabled ? '<span class="badge success">启用</span>' : '<span class="badge">已停用</span>'}</td>
          <td><div class="table-actions">${row.channel_id ? `<button class="button small ghost" data-watch-channel="${esc(row.channel_id)}">直播</button>` : ""}<button class="button small secondary" data-edit-sub="${esc(row._id)}">编辑</button><button class="button small ${row.enabled ? "danger" : "secondary"}" data-toggle-sub="${esc(row._id)}">${row.enabled ? "停用" : "启用"}</button></div></td>
        </tr>`).join("")}</tbody></table>` : emptyState("还没有订阅", "从节目单一键创建，或手动新建关键词/频道时间规则。")}</div>
    </section>`;
  $("#new-subscription").addEventListener("click", () => openSubscriptionForm());
  $("#view").addEventListener("click", async event => {
    const watch = event.target.closest("[data-watch-channel]");
    const edit = event.target.closest("[data-edit-sub]");
    const toggle = event.target.closest("[data-toggle-sub]");
    if (watch) return openLivePlayer(watch.dataset.watchChannel, channelName(watch.dataset.watchChannel));
    if (edit) return openSubscriptionForm(subscriptions.find(item => item._id === edit.dataset.editSub));
    if (!toggle) return;
    const row = subscriptions.find(item => item._id === toggle.dataset.toggleSub);
    try {
      if (row.enabled) await api(`/api/subscriptions/${row._id}`, { method: "DELETE" });
      else await api(`/api/subscriptions/${row._id}`, { method: "PATCH", body: { enabled: true } });
      toast(row.enabled ? "订阅已停用" : "订阅已启用");
      renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
  void channels;
}

async function openSubscriptionForm(existing = null, preset = {}) {
  const channels = await loadChannels();
  const value = { asr_enabled: true, live_asr_enabled: true, asr_model: "small", llm_enabled: true, push_enabled: true, auto_bookmark: false, post_roll_seconds: 180, enabled: true, ...preset, ...(existing || {}) };
  const type = value.match_type || "keyword";
  const weekdays = value.time_rule?.weekdays
    || (value.time_rule?.weekday === undefined || value.time_rule?.weekday === null ? [] : [value.time_rule.weekday]);
  openModal(existing ? "编辑订阅" : "新建订阅", `
    <form id="subscription-form" class="form-grid">
      <label class="field wide"><span>订阅名称</span><input class="input" name="name" required maxlength="100" value="${esc(value.name || "")}" placeholder="例如：新闻联播"></label>
      <label class="field"><span>匹配方式</span><select class="select" name="match_type" id="subscription-type"><option value="keyword" ${type === "keyword" ? "selected" : ""}>关键词</option><option value="channel_time" ${type === "channel_time" ? "selected" : ""}>频道时间</option></select></label>
      <label class="field"><span>频道</span><select class="select" name="channel_id" id="subscription-channel">${channelOptions(channels, value.channel_id || "", type === "keyword")}</select></label>
      <label class="field wide keyword-field"><span>节目关键词</span><input class="input" name="keyword" value="${esc(value.keyword || "")}" placeholder="节目标题或简介中的关键词"></label>
      <div class="wide time-fields">
        <div class="form-grid">
          <label class="field"><span>播出时间</span><input class="input" type="time" name="rule_time" value="${esc(value.time_rule?.time || "20:00")}"></label>
          <label class="field"><span>容差（分钟）</span><input class="input" type="number" name="tolerance_min" min="0" max="180" value="${esc(value.time_rule?.tolerance_min ?? 5)}"></label>
          <div class="field wide"><span>星期</span><div class="checks">${["一","二","三","四","五","六","日"].map((label, day) => `<label class="check"><input type="checkbox" name="weekday" value="${day}" ${weekdays.includes(day) ? "checked" : ""}>周${label}</label>`).join("")}</div></div>
        </div>
      </div>
      <label class="field"><span>ASR 模型</span><select class="select" name="asr_model">${["tiny","base","small","medium"].map(model => `<option ${value.asr_model === model ? "selected" : ""}>${model}</option>`).join("")}</select></label>
      <label class="field"><span>录制缓冲（片尾）</span><select class="select" name="post_roll_seconds">${[["60","1 分钟"],["180","3 分钟（默认）"],["300","5 分钟"],["600","10 分钟"]].map(([sec, label]) => `<option value="${sec}" ${Number(value.post_roll_seconds || 180) === Number(sec) ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      <div class="field"><span>自动处理</span><div class="checks"><label class="check"><input type="checkbox" name="asr_enabled" ${value.asr_enabled ? "checked" : ""}>转写</label><label class="check"><input type="checkbox" name="live_asr_enabled" ${value.live_asr_enabled ? "checked" : ""}>录制时转写</label><label class="check"><input type="checkbox" name="llm_enabled" ${value.llm_enabled ? "checked" : ""}>摘要</label><label class="check"><input type="checkbox" name="push_enabled" ${value.push_enabled ? "checked" : ""}>邮件</label><label class="check"><input type="checkbox" name="auto_bookmark" ${value.auto_bookmark ? "checked" : ""}>自动收藏摘要</label></div></div>
      <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">${existing ? "保存修改" : "创建订阅"}</button></div>
    </form>`);
  const form = $("#subscription-form");
  const syncFields = () => {
    const keyword = $("#subscription-type").value === "keyword";
    $$(".keyword-field", form).forEach(node => node.hidden = !keyword);
    $$(".time-fields", form).forEach(node => node.hidden = keyword);
    $("#subscription-channel").required = !keyword;
  };
  $("#subscription-type").addEventListener("change", syncFields);
  syncFields();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(form);
    const matchType = data.get("match_type");
    const body = {
      name: data.get("name").trim(), match_type: matchType,
      keyword: matchType === "keyword" ? data.get("keyword").trim() : null,
      channel_id: data.get("channel_id") || null,
      time_rule: matchType === "channel_time" ? {
        weekdays: data.getAll("weekday").map(Number), time: data.get("rule_time"),
        tolerance_min: Number(data.get("tolerance_min")), timezone: "Asia/Shanghai",
      } : null,
      asr_enabled: data.has("asr_enabled"), live_asr_enabled: data.has("live_asr_enabled"), asr_model: data.get("asr_model"),
      llm_enabled: data.has("llm_enabled"), push_enabled: data.has("push_enabled"),
      auto_bookmark: data.has("auto_bookmark"), post_roll_seconds: Number(data.get("post_roll_seconds")),
      enabled: existing ? Boolean(existing.enabled) : true,
    };
    if (matchType === "channel_time" && !body.time_rule.weekdays.length) return toast("频道时间订阅至少选择一个星期", "error");
    try {
      await api(existing ? `/api/subscriptions/${existing._id}` : "/api/subscriptions", { method: existing ? "PATCH" : "POST", body });
      toast(existing ? "订阅已更新" : "订阅已创建"); closeModal(); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

async function renderRecordings() {
  const [recordings, channels, bookmarks] = await Promise.all([api("/api/recordings?limit=200"), loadChannels(), loadBookmarks()]);
  const renderToken = appState.renderToken;
  $("#view").innerHTML = `
    ${pageIntro("追踪从排队、录制、转写、摘要到邮件推送的完整处理状态。", '<button class="button" id="manual-recording" type="button">● 手工录制</button>')}
    <div class="toolbar"><label class="field"><span>状态筛选</span><select class="select" id="recording-filter"><option value="">全部状态</option>${Object.entries(statusMeta).map(([key, meta]) => `<option value="${key}">${meta[0]}</option>`).join("")}</select></label><div class="grow"></div><span class="badge danger" id="recording-live"></span><span class="badge" id="recording-count">${recordings.length} 条</span></div>
    <section class="panel"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>节目</th><th>时间</th><th>状态</th><th>文件</th><th></th></tr></thead><tbody id="recording-body"></tbody></table></div></section>`;
  const renderRows = () => {
    const filter = $("#recording-filter").value;
    const rows = recordings.filter(row => !filter || row.status === filter);
    const running = recordings.filter(row => row.status === "recording").length;
    $("#recording-count").textContent = `${rows.length} 条`;
    const liveBadge = $("#recording-live");
    liveBadge.textContent = running ? `录制中 ${running}` : "";
    liveBadge.style.display = running ? "" : "none";
    $("#recording-body").innerHTML = rows.length ? rows.map(row => {
      const saved = Boolean(bookmarkFor("recording", row._id));
      const playable = Boolean(row.file_path) && !["queued","recording","failed"].includes(row.status);
      const incomplete = row.partial ? `<span class="badge danger" title="本次录制不完整，缺失开头约 ${esc(row.missed_seconds || 0)} 秒">不完整</span> ` : "";
      const encodeMark = row.encode_status === "done"
        ? `<span class="badge success" title="${esc(formatBytes(row.original_size_bytes))} → ${esc(formatBytes(row.encoded_size_bytes))}">已压缩</span> `
        : row.encode_status === "failed" ? '<span class="badge danger" title="转码失败，仍保留原始 TS">压缩失败</span> '
        : row.encode_status === "encoding" ? '<span class="badge">转码中</span> ' : "";
      return `<tr><td><strong>${esc(row.program_title)}</strong><small>${esc(row.channel_id)}${row.manual ? " · 手工" : ""}${row.epg_time_suspect ? " · EPG时间存疑" : ""}</small></td><td>${esc(formatDateTime(row.start_time))}</td><td>${incomplete}${encodeMark}${badge(row.status)}</td><td>${esc(formatBytes(row.size_bytes))}</td><td><div class="table-actions"><button class="button small ghost" data-watch-channel="${esc(row.channel_id)}">直播</button><button class="button small secondary" data-recording-detail="${esc(row._id)}">详情</button>${playable ? `<button class="button small secondary" data-play="${esc(row._id)}" data-title="${esc(row.program_title)}">播放</button>` : ""}<button class="button small ghost" data-bookmark-recording="${esc(row._id)}">${saved ? "♥" : "♡"}</button></div></td></tr>`;
    }).join("") : `<tr><td colspan="5">${emptyState("暂无记录", "当前筛选条件下没有录制。", true)}</td></tr>`;
  };
  const scheduleRefresh = () => {
    clearTimeout(appState.recordingRefreshTimer);
    const active = recordings.some(row => ["recording", "queued", "completed", "asr_processing", "asr_done", "summarizing"].includes(row.status));
    if (!active) return;
    appState.recordingRefreshTimer = setTimeout(async () => {
      if (appState.renderToken !== renderToken) return;
      try {
        const latest = await api("/api/recordings?limit=200");
        recordings.length = 0;
        recordings.push(...latest);
        renderRows();
        scheduleRefresh();
      } catch (_error) {
        scheduleRefresh();
      }
    }, 10000);
  };
  renderRows();
  scheduleRefresh();
  $("#recording-filter").addEventListener("change", renderRows);
  $("#manual-recording").addEventListener("click", () => openManualRecording(channels));
  $("#recording-body").addEventListener("click", async event => {
    const watch = event.target.closest("[data-watch-channel]");
    const detail = event.target.closest("[data-recording-detail]");
    const play = event.target.closest("[data-play]");
    const bookmark = event.target.closest("[data-bookmark-recording]");
    if (watch) return openLivePlayer(watch.dataset.watchChannel, channelName(watch.dataset.watchChannel));
    if (detail) return openRecordingDetail(detail.dataset.recordingDetail);
    if (play) return openPlayer(play.dataset.play, play.dataset.title);
    if (bookmark) {
      try { await toggleBookmark("recording", bookmark.dataset.bookmarkRecording); renderRoute(); }
      catch (error) { toast(error.message, "error"); }
    }
  });
}

function openManualRecording(channels) {
  openModal("手工录制", `<form id="manual-form" class="form-grid">
    <label class="field wide"><span>节目名称</span><input class="input" name="program_title" required value="手工录制"></label>
    <label class="field"><span>频道</span><select class="select" name="channel_id" required>${channelOptions(channels, "", false)}</select></label>
    <label class="field"><span>时长（秒）</span><input class="input" name="duration_sec" type="number" min="10" max="600" value="30" required></label>
    <label class="field"><span>ASR 模型</span><select class="select" name="asr_model"><option>tiny</option><option selected>base</option><option>small</option><option>medium</option></select></label>
    <div class="field"><span>后续处理</span><div class="checks"><label class="check"><input type="checkbox" name="asr_enabled" checked>转写</label><label class="check"><input type="checkbox" name="live_asr_enabled" checked>录制时转写</label><label class="check"><input type="checkbox" name="llm_enabled" checked>摘要</label><label class="check"><input type="checkbox" name="push_enabled">邮件</label></div></div>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">开始录制</button></div>
  </form>`);
  $("#manual-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      await api("/api/recordings/manual", { method: "POST", body: {
        channel_id: data.get("channel_id"), duration_sec: Number(data.get("duration_sec")),
        program_title: data.get("program_title").trim(), asr_enabled: data.has("asr_enabled"), live_asr_enabled: data.has("live_asr_enabled"),
        asr_model: data.get("asr_model"), llm_enabled: data.has("llm_enabled"), push_enabled: data.has("push_enabled"),
      }});
      toast("录制任务已创建"); closeModal(); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

function analysisClock(value) {
  const n = Math.max(0, Math.floor(Number(value) || 0));
  return [Math.floor(n / 3600), Math.floor(n % 3600 / 60), n % 60].map(v => String(v).padStart(2, '0')).join(':');
}

async function openTranscriptEditor(recordingId, focus = null, offset = 0, search = "") {
  openModal('转写时间轴与人工校正', '<div class="page-loading">加载原文…</div>', true);
  const token = Symbol('transcript-editor');
  appState.liveDetailToken = token;
  try {
    const query = new URLSearchParams({ offset, limit: 50, q: search });
    if (focus) query.set('focus', focus);
    const page = await api(`/api/recordings/${encodeURIComponent(recordingId)}/transcript?${query}`);
    if (appState.liveDetailToken !== token) return;
    $('#modal-body').innerHTML = `<div class="detail-stack"><form data-transcript-search class="button-row"><input class="input" name="query" maxlength="200" placeholder="检索转写文字" value="${esc(search)}"><button class="button secondary" type="submit">检索</button></form><p>文本版本 ${page.revision} · 共 ${page.total} 段。校正后只重算受影响的分段笔记，再汇总正式摘要；录像保持原样。</p>${!page.editable ? '<div class="notice">录制或分析尚未完成，当前可查看原文，完成后可校正。</div>' : ''}${page.segments.map(s => `<section class="detail-section"><h4>${analysisClock(s.start)}–${analysisClock(s.end)} ${s.edited_at ? '· 已人工校正' : ''}</h4><div class="button-row"><button class="button small secondary" data-evidence-play="${esc(recordingId)}" data-part-id="${esc(s.part_id === 'main' ? '' : s.part_id || '')}" data-start="${Number(s.media_start ?? s.start) || 0}">▶ 回看此段</button></div><form data-segment-form="${esc(s.id)}"><label class="field"><span>转写文字</span><textarea class="input" name="text" rows="3" maxlength="4000" required ${page.editable ? '' : 'readonly'}>${esc(s.text)}</textarea></label>${s.original_text ? `<details><summary>识别原文</summary><p>${esc(s.original_text)}</p></details>` : ''}${page.editable ? '<label class="check"><input type="checkbox" name="notify">完成后重新推送（默认关闭）</label><button class="button small" type="submit">保存并重新生成受影响摘要</button>' : ''}</form></section>`).join('')}<div class="button-row"><button class="button secondary" data-transcript-prev ${page.offset === 0 ? 'disabled' : ''}>上一页</button><button class="button secondary" data-transcript-next ${page.offset + page.segments.length >= page.total ? 'disabled' : ''}>下一页</button><button class="button secondary" data-transcript-history>修订记录</button></div><div data-edit-history></div></div>`;
    $('[data-transcript-search]').onsubmit = event => {
      event.preventDefault();
      openTranscriptEditor(recordingId, null, 0, new FormData(event.currentTarget).get('query'));
    };
    $('[data-transcript-prev]').onclick = () => openTranscriptEditor(recordingId, null, Math.max(0, page.offset - 50), search);
    $('[data-transcript-next]').onclick = () => openTranscriptEditor(recordingId, null, page.offset + 50, search);
    $('[data-transcript-history]').onclick = async () => {
      try {
        const rows = await api(`/api/recordings/${encodeURIComponent(recordingId)}/transcript/edits`);
        if (appState.liveDetailToken !== token) return;
        $('[data-edit-history]').innerHTML = rows.map(e => `<p>${esc(formatDateTime(e.created_at))} · ${e.kind === 'regenerate' ? '重新生成摘要' : `文字版本 ${e.from_revision} → ${e.to_revision}`} · ${esc(e.state)}${e.after ? `<br>${esc(e.before)} → ${esc(e.after)}` : ''}</p>`).join('') || '<p>暂无修订记录。</p>';
      } catch (error) { toast(error.message, 'error'); }
    };
    document.querySelectorAll('[data-segment-form]').forEach(form => form.addEventListener('submit', async event => {
      event.preventDefault();
      const data = new FormData(form), button = form.querySelector('button[type=submit]');
      button.disabled = true;
      try {
        const result = await api(`/api/recordings/${encodeURIComponent(recordingId)}/transcript/segments/${encodeURIComponent(form.dataset.segmentForm)}`, { method: 'PATCH', body: { text: data.get('text'), expected_revision: page.revision, notify: data.has('notify') } });
        toast(result.changed ? '校正已保存，正在重新生成摘要。' : '文字没有变化。');
        if (appState.liveDetailToken === token) {
          if (result.changed) openRecordingDetail(recordingId);
          else button.disabled = false;
        }
      } catch (error) { if (appState.liveDetailToken === token) button.disabled = false; toast(error.message, 'error'); }
    }));
  } catch (error) { if (appState.liveDetailToken === token) $('#modal-body').innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

document.addEventListener('click', async event => {
  const play = event.target.closest('[data-evidence-play]');
  if (play) return openPlayer(play.dataset.evidencePlay, '原文对应录像', Number(play.dataset.start), play.dataset.partId || null);
  const archived = event.target.closest('[data-partial-transcripts]');
  if (archived) {
    try {
      const rows = await api(`/api/recordings/${encodeURIComponent(archived.dataset.partialTranscripts)}/partial-transcripts`);
      openModal('中断前保留的转写草稿', rows.map(r => `<section class="detail-section"><h3>${analysisClock(r.offset)} 起</h3><p style="white-space:pre-wrap">${esc(r.text)}</p></section>`).join('') || '<p>中断前尚无已完成的实时转写；保留片段会在采集结束后统一补转写。</p>', true);
    } catch (error) { toast(error.message, 'error'); }
    return;
  }
  const edit = event.target.closest('[data-evidence-edit]');
  if (edit) return openTranscriptEditor(edit.dataset.evidenceEdit, edit.dataset.segmentId || null);
  const regenerate = event.target.closest('[data-analysis-regenerate]');
  if (regenerate) {
    regenerate.disabled = true;
    try {
      await api(`/api/recordings/${encodeURIComponent(regenerate.dataset.analysisRegenerate)}/analysis/regenerate`, { method: 'POST' });
      toast('正在生成可追溯摘要，不会自动重新推送。');
      openRecordingDetail(regenerate.dataset.analysisRegenerate);
    } catch (error) { regenerate.disabled = false; toast(error.message, 'error'); }
  }
});

function summarySections(summary, transcript) {
  if (!summary && !transcript) return '<div class="notice">当前还没有转写或摘要。</div>';
  return `<div class="detail-stack">
    ${summary ? `<section class="detail-section"><h3>内容摘要</h3><p>${esc(summary.summary_text)}</p></section>
      <section class="detail-section"><h3>新闻 / 话题章节</h3><p>按原文依据定位话题；起点供回看参考，不代表节目编辑边界。</p><div class="button-row">${(summary.chapters || []).map(c => `<button class="button small secondary" data-evidence-play="${esc(summary.recording_id)}" data-part-id="${esc(c.part_id === 'main' ? '' : c.part_id || '')}" data-start="${Number(c.media_start ?? c.start) || 0}">${analysisClock(c.start)} · ${esc(c.title)}</button>`).join('') || '<span>尚无章节，请重新生成可追溯摘要。</span>'}</div></section><section class="detail-section"><h3>核心要点与原文依据</h3><ol class="key-points">${(summary.key_points || []).map((point, index) => `<li><p>${esc(point)}</p>${(summary.evidence || []).filter(e => e.point_index === index).map(e => `<blockquote><p>${esc(e.quote)}</p><button class="button small secondary" data-evidence-play="${esc(summary.recording_id)}" data-part-id="${esc(e.part_id === 'main' ? '' : e.part_id || '')}" data-start="${Number(e.media_start ?? e.start) || 0}">▶ ${analysisClock(e.start)}–${analysisClock(e.end)}</button> <button class="button small secondary" data-evidence-edit="${esc(summary.recording_id)}" data-segment-id="${esc(e.segment_id)}">查看 / 校正原文</button></blockquote>`).join("") || '<small>未定位到可核对的原文，不提供推测时间点。</small>'}</li>`).join("") || "<li>暂无</li>"}</ol></section>
      <section class="detail-section"><h3>实体与跟进</h3><div class="tag-list">${(summary.entities || []).map(entity => `<span class="tag">${esc(typeof entity === "string" ? entity : JSON.stringify(entity))}</span>`).join("") || '<span class="tag">暂无实体</span>'}</div>${summary.follow_up ? `<p style="margin-top:12px">${esc(summary.follow_up)}</p>` : ""}</section>` : ""}
    ${transcript ? `<section class="detail-section"><h3>转写全文</h3>${!transcript.segments?.some(s => s.part_id && s.part_id !== 'main') ? `<div class="button-row"><a class="button small secondary" href="/api/recordings/${encodeURIComponent(transcript.recording_id)}/transcript/subtitles?format=srt">导出 SRT 字幕</a><a class="button small secondary" href="/api/recordings/${encodeURIComponent(transcript.recording_id)}/transcript/subtitles?format=vtt">导出 WebVTT 字幕</a></div>` : '<p>断流录像请在片段列表导出字幕。</p>'}<p>${esc(transcript.full_text || "暂无转写文本")}</p></section>` : ""}
  </div>`;
}

async function openRecordingDetail(recordingId) {
  openModal("录制详情", '<div class="page-loading"><span class="spinner"></span></div>', true);
  const token = Symbol("recording-detail");
  appState.liveDetailToken = token;
  const refresh = async () => {
  if (appState.liveDetailToken !== token) return;
  try {
    const detail = await api(`/api/recordings/${recordingId}`);
    if (appState.liveDetailToken !== token) return;
    const selection = window.getSelection();
    if (selection?.toString() && $("#modal-body").contains(selection.anchorNode)) {
      setTimeout(refresh, 5000);
      return;
    }
    const row = detail.recording;
    const live = detail.live || {};
    const labels = { recording: "录制中", completed: "录制完成", streaming: "持续转写中", done: "已完成", waiting: "等待处理", preview: "预览已更新", finalizing: "生成终版中", fallback: "转为录后处理", failed: "处理失败", disabled: "未启用" };
    const scroll = $("#modal-body").scrollTop;
    $("#modal-title").textContent = row.program_title || "录制详情";
    $("#modal-body").innerHTML = `<div class="detail-stack">
      ${row.partial ? `<div class="notice warning">${row.recording_parts?.length ? '本次录制出现中断，已保留可用片段，摘要仅覆盖保留内容。' : `本次录制不完整，缺失开头约 ${esc(row.missed_seconds || 0)} 秒。`}</div>` : ""}
      ${row.epg_time_suspect ? `<div class="notice warning">EPG 时间可能不准，建议加大录制缓冲。</div>` : ""}
      <dl class="definition-grid"><div><dt>频道</dt><dd>${esc(row.channel_id)}</dd></div><div><dt>状态</dt><dd>${badge(row.status)}</dd></div><div><dt>开始时间</dt><dd>${esc(formatDateTime(row.start_time))}</dd></div><div><dt>时长 / 大小</dt><dd>${esc(row.duration_sec || "—")} 秒 · ${esc(formatBytes(row.size_bytes))}</dd></div>${row.encode_status === "done" ? `<div><dt>压缩</dt><dd><span class="badge success">已压缩</span> ${esc(formatBytes(row.original_size_bytes))} → ${esc(formatBytes(row.encoded_size_bytes))}${row.encode_settings?.height ? ` · ${esc(row.encode_settings.height)}p` : ""}</dd></div>` : row.encode_status === "pending" || row.encode_status === "encoding" ? `<div><dt>压缩</dt><dd><span class="badge">${row.encode_status === "encoding" ? "转码中" : "等待转码"}</span></dd></div>` : row.encode_status === "failed" ? `<div><dt>压缩</dt><dd><span class="badge danger">转码失败</span>${row.encode_error ? `<small class="danger">${esc(shorten(row.encode_error, 80))}</small>` : ""}</dd></div>` : ""}</dl>
      <div class="button-row">${row.file_path ? `<button class="button" data-detail-play type="button">▶ 播放</button><a class="button secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/download">下载原文件</a>` : ""}</div>
      ${live.epoch ? `<section class="detail-section"><h3>实时分析</h3><p>录制：${esc(labels[live.capture] || live.capture)} · 转写：${esc(labels[live.asr] || live.asr)} · 摘要：${esc(labels[live.summary] || live.summary)}</p><p>文字已处理至 ${Math.floor((live.through_seconds || 0) / 60)} 分 ${Math.floor((live.through_seconds || 0) % 60)} 秒 · 落后采集约 ${Math.round(live.lag_seconds || 0)} 秒 · 待处理 ${live.pending_chunks || 0} 段${live.estimated_catchup_seconds == null ? "" : ` · 预计处理当前积压约 ${Math.round(live.estimated_catchup_seconds)} 秒`}</p><p>正式文字已处理至 ${Math.floor((live.verified_through_seconds || 0) / 60)} 分 ${Math.floor((live.verified_through_seconds || 0) % 60)} 秒 · 正式通道待处理 ${live.verified_pending_chunks || 0} 段</p><p>运行设备：${esc(live.runtime?.device || "等待识别")}${live.runtime?.fallback_reason ? "（已降级）" : ""}</p>${live.state === "fallback" ? '<div class="notice">实时分析暂不可用，录像继续保存，录后自动补转写。</div>' : ""}${live.preview && !detail.summary ? `<h4>预览摘要 · 非终版</h4><p style="white-space:pre-wrap">${esc(live.preview.summary_text)}</p><small>更新于 ${esc(formatDateTime(live.updated_at))}；预览不推送。</small>` : ""}${live.text && !detail.transcript ? `<h4>实时文字 · 草稿</h4><small>短窗识别可能有错字，正式文字由长窗结果生成。</small><p style="white-space:pre-wrap">${esc(live.text)}</p>` : ""}</section>` : ""}
      ${detail.summary || detail.transcript || !live.epoch ? summarySections(detail.summary, detail.transcript) : '<div class="notice">节目分析完成后将显示终版摘要。</div>'}
      ${detail.transcript ? `<div class="button-row"><button class="button secondary" data-evidence-edit="${esc(recordingId)}">转写时间轴 / 人工校正</button><button class="button secondary" data-analysis-regenerate="${esc(recordingId)}">重新生成可追溯摘要</button></div>` : ""}
      ${detail.coverage ? `<section class="detail-section"><h3>已保留录制片段</h3><button class="button small secondary" data-partial-transcripts="${esc(recordingId)}">查看中断前已完成的转写</button><p>片段独立保存。换源可能有延迟或重复，回看时按片段选择。</p>${detail.coverage.parts.map((p, i) => `<p>片段 ${i+1} · ${analysisClock(p.offset)} 起 · ${Math.round(p.duration)} 秒 <button class="button small secondary" data-evidence-play="${esc(recordingId)}" data-part-id="${esc(p.id)}" data-start="0">播放</button> <a class="button small secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/parts/${encodeURIComponent(p.id)}/download">下载</a> <a class="button small secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/transcript/subtitles?format=srt&part_id=${encodeURIComponent(p.id)}">SRT 字幕</a> <a class="button small secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/transcript/subtitles?format=vtt&part_id=${encodeURIComponent(p.id)}">WebVTT 字幕</a></p>`).join('')}<p>估算缺失时段：${detail.coverage.missing_intervals.map(g => `${analysisClock(g.start)}–${analysisClock(g.end)}`).join('、') || '未检测到明显缺口'}</p></section>` : ""}
      <section class="detail-section"><h3>推送事件</h3>${detail.push_events?.length ? detail.push_events.map(item => `<p>${esc(item.provider)} · ${esc(item.status)} · ${esc(formatDateTime(item.updated_at))}</p>`).join("") : "<p>暂无推送事件</p>"}</section>
    </div>`;
    $("[data-detail-play]")?.addEventListener("click", () => openPlayer(recordingId, row.program_title));
    $("#modal-body").scrollTop = scroll;
  } catch (error) { if (appState.liveDetailToken === token) $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
  if (appState.liveDetailToken === token) setTimeout(refresh, 5000);
  };
  await refresh();
}

async function openPlayer(recordingId, title, startSeconds = 0, partId = null) {
  try {
    const detail = await api(`/api/recordings/${encodeURIComponent(recordingId)}`);
    if (!partId && ["queued", "recording"].includes(detail.recording.status)) {
      return toast("录像仍在录制或收尾，完成后即可播放。");
    }
  } catch (error) { return toast(error.message, "error"); }
  const mediaBase = `/api/recordings/${encodeURIComponent(recordingId)}${partId ? `/parts/${encodeURIComponent(partId)}` : ""}`;
  openModal(`播放 · ${title || "录像"}`, `<div class="video-shell">
    <video id="recording-player" controls preload="metadata" playsinline></video>
    <div class="video-preparing" id="video-preparing"><span class="spinner"></span><strong>正在准备浏览器视频</strong><small>首次播放需要无损转封装，通常等待数秒。</small></div>
  </div><div class="playback-toolbar"><button class="button small secondary" id="seek-back" type="button">−10 秒</button><label class="speed-control"><span>播放速度</span><select class="select" id="playback-rate"><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label><button class="button small secondary" id="seek-forward" type="button">+10 秒</button><span class="playback-time" id="playback-time">00:00 / --:--</span></div><div class="modal-actions"><a class="button secondary" href="${mediaBase}/download">${partId ? "下载此片段" : "下载原始录像"}</a><button class="button secondary" data-close-modal type="button">关闭</button></div>`, true);
  const video = $("#recording-player");
  const preparing = $("#video-preparing");
  video.addEventListener("loadedmetadata", () => {
    preparing.hidden = true;
    const target = Math.max(0, Number(startSeconds) || 0);
    video.currentTime = Number.isFinite(video.duration) ? Math.min(target, Math.max(0, video.duration - .1)) : target;
  });
  video.addEventListener("canplay", () => { preparing.hidden = true; });
  video.addEventListener("error", () => {
    preparing.innerHTML = '<strong>浏览器无法播放该录像</strong><small>可使用下方“下载原始录像”在播放器中打开。</small>';
  });
  const timeLabel = $("#playback-time");
  const clock = value => {
    if (!Number.isFinite(value)) return "--:--";
    const seconds = Math.max(0, Math.floor(value));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  const updateTime = () => { timeLabel.textContent = `${clock(video.currentTime)} / ${clock(video.duration)}`; };
  video.addEventListener("timeupdate", updateTime);
  video.addEventListener("durationchange", updateTime);
  $("#seek-back").addEventListener("click", () => { video.currentTime = Math.max(0, video.currentTime - 10); });
  $("#seek-forward").addEventListener("click", () => {
    video.currentTime = Math.min(Number.isFinite(video.duration) ? video.duration : video.currentTime + 10, video.currentTime + 10);
  });
  $("#playback-rate").addEventListener("change", event => { video.playbackRate = Number(event.currentTarget.value); });
  video.src = `${mediaBase}/play`;
  video.load();
}

function openLivePlayer(channelId, title) {
  if (!channelId) return toast("该订阅没有指定频道，无法直接观看直播", "error");
  openModal(`直播 · ${title || channelId}`, `<div class="video-shell">
    <video id="live-player" controls autoplay playsinline></video>
    <div class="video-preparing" id="live-preparing"><span class="spinner"></span><strong>正在连接直播</strong><small>首次连接通常需要数秒。</small></div>
  </div><div class="stream-picker" id="stream-picker" hidden style="display:flex;align-items:flex-end;gap:12px;padding:0 4px 10px;">
    <label class="field grow"><span>播放线路</span><select class="select" id="stream-select" disabled><option>加载中…</option></select></label>
    <span class="badge info" id="stream-badge">自动</span>
  </div><div class="live-note">稳定播放模式使用 NAS 内部预缓冲和浏览器缓冲，整体延迟通常约 30–50 秒；切换线路会立即重新连接。</div><div class="modal-actions"><button class="button secondary" id="reload-live" type="button">重新连接</button><button class="button secondary" data-close-modal type="button">关闭</button></div>`, true);
  const video = $("#live-player");
  const preparing = $("#live-preparing");
  const source = `/api/channels/${encodeURIComponent(channelId)}/live`;
  let reconnectAttempts = 0;
  const connect = async (automatic = false) => {
    if (!automatic) reconnectAttempts = 0;
    if (appState.liveConnectTimer) {
      clearTimeout(appState.liveConnectTimer);
      appState.liveConnectTimer = null;
    }
    if (appState.liveHls) appState.liveHls.destroy();
    video.removeAttribute("src");
    video.load();
    preparing.hidden = false;
    preparing.innerHTML = '<span class="spinner"></span><strong>正在连接直播</strong><small>首次连接通常需要数秒。</small>';
    const buffer = await loadBufferProfile();
    const maxBufferLength = buffer.buffer_duration;
    const liveSyncDurationCount = buffer.live_sync;
    if (window.Hls?.isSupported()) {
      const hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        maxBufferLength,
        maxMaxBufferLength: Math.max(maxBufferLength * 2, 120),
        backBufferLength: maxBufferLength,
        liveSyncDurationCount,
        liveMaxLatencyDurationCount: Math.max(liveSyncDurationCount * 2, 20),
        maxLiveSyncPlaybackRate: 1.0,
        maxBufferHole: 1,
        highBufferWatchdogPeriod: 2,
        nudgeOffset: 0.3,
        nudgeMaxRetry: 5,
        startLevel: -1,
        capLevelToPlayerSize: true,
        fragLoadPolicy: {
          default: {
            maxTimeToFirstByteMs: 10000,
            maxLoadTimeMs: 20000,
            timeoutRetry: { maxNumRetry: 6, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
            errorRetry: { maxNumRetry: 6, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
          },
        },
        manifestLoadPolicy: {
          default: {
            maxTimeToFirstByteMs: 10000,
            maxLoadTimeMs: 20000,
            timeoutRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
            errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
          },
        },
        playlistLoadPolicy: {
          default: {
            maxTimeToFirstByteMs: 10000,
            maxLoadTimeMs: 20000,
            timeoutRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
            errorRetry: { maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 8000 },
          },
        },
        xhrSetup(xhr) {
          const token = sessionStorage.getItem("iptv_api_token");
          if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
        },
      });
      appState.liveHls = hls;
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(window.Hls.Events.FRAG_BUFFERED, () => {
        if (appState.liveConnectTimer) clearTimeout(appState.liveConnectTimer);
        appState.liveConnectTimer = null;
        preparing.hidden = true;
      });
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          hls.startLoad();
          return;
        }
        if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError();
          return;
        }
        if (video.videoWidth && !video.paused) return;
        preparing.hidden = false;
        preparing.innerHTML = `<strong>直播连接失败</strong><small>${esc(data.reason || data.details || "请稍后重试")}</small>`;
      });
      hls.loadSource(source);
      hls.attachMedia(video);
      appState.liveConnectTimer = setTimeout(() => {
        if (appState.liveHls !== hls || video.buffered.length) return;
        if (reconnectAttempts < 1) {
          reconnectAttempts += 1;
          preparing.hidden = false;
          preparing.innerHTML = '<span class="spinner"></span><strong>直播缓冲异常，正在自动重连</strong><small>正在避开不可播放的旧分片。</small>';
          connect(true);
          return;
        }
        preparing.hidden = false;
        preparing.innerHTML = '<strong>直播暂时无法形成可播放缓冲</strong><small>线路时间轴异常，请稍后点击“重新连接”。</small>';
      }, 15000);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      video.play().catch(() => {});
    } else {
      preparing.innerHTML = '<strong>当前浏览器不支持 HLS 直播</strong><small>请使用最新版 Chrome、Edge 或 Safari。</small>';
    }
  };
  appState.liveVisibilityHandler = () => {
    const hls = appState.liveHls;
    if (!hls) return;
    if (document.hidden) {
      appState.liveResumeOnVisible = !video.paused;
      video.pause();
      hls.stopLoad();
      return;
    }
    hls.startLoad(-1);
    if (appState.liveResumeOnVisible) video.play().catch(() => {});
    appState.liveResumeOnVisible = false;
  };
  document.addEventListener("visibilitychange", appState.liveVisibilityHandler);
  const markPlayable = () => {
    if (appState.liveConnectTimer) clearTimeout(appState.liveConnectTimer);
    appState.liveConnectTimer = null;
    preparing.hidden = true;
  };
  video.addEventListener("playing", markPlayable);
  video.addEventListener("canplay", markPlayable);
  video.addEventListener("timeupdate", () => {
    if (video.videoWidth && video.currentTime > 0) preparing.hidden = true;
  });
  video.addEventListener("error", () => {
    preparing.hidden = false;
    preparing.innerHTML = '<strong>直播播放失败</strong><small>可能是线路临时不可用，请重新连接。</small>';
  });
  $("#reload-live").addEventListener("click", () => connect(false));
  const loadStreams = async () => {
    const picker = $("#stream-picker");
    const select = $("#stream-select");
    const badge = $("#stream-badge");
    try {
      const data = await api(`/api/channels/${encodeURIComponent(channelId)}/streams`);
      const sources = data.sources || [];
      if (!sources.length) {
        badge.textContent = "单线路";
        select.innerHTML = '<option value="" selected>仅有一条线路</option>';
        picker.hidden = false;
        return;
      }
      const activeId = data.active_source_id;
      const kindLabel = item => SOURCE_KIND_LABELS[item.kind] || "线路";
      select.innerHTML = sources.map(item => {
        const marks = [];
        if (item.preferred) marks.push("首选");
        if (item.locked) marks.push("已锁定");
        const suffix = marks.length ? ` · ${marks.join(" / ")}` : "";
        return `<option value="${esc(item.source_id)}" ${item.source_id === activeId ? "selected" : ""}>${esc(kindLabel(item))} ${esc(item.label || item.host || "线路")}${suffix}${streamQualityLabel(item.quality)}</option>`;
      }).join("");
      select.disabled = false;
      const fallback = data.active_state === "fallback";
      badge.textContent = data.policy === "manual" ? "手动" : fallback ? "已回退" : "首选";
      badge.className = fallback ? "badge warning" : "badge info";
      picker.hidden = false;
      select.addEventListener("change", async () => {
        const sourceId = select.value;
        if (!sourceId || sourceId === activeId) return;
        select.disabled = true;
        try {
          await api(`/api/channels/${encodeURIComponent(channelId)}/source`, {
            method: "POST",
            body: { source_id: sourceId, mode: "temporary" },
          });
          toast("已临时切换线路，正在重连；首选源不会被替换");
          connect(false);
        } catch (error) {
          toast(error.message, "error");
        } finally {
          select.disabled = false;
        }
      });
    } catch (error) {
      badge.textContent = "线路信息不可用";
      picker.hidden = false;
    }
  };
  loadStreams();
  connect();
}

async function renderSummaries() {
  await loadBookmarks();
  $("#view").innerHTML = `
    ${pageIntro("查看 AI 结构化摘要、核心要点、实体和关联转写。")}
    <div class="toolbar"><label class="field"><span>日期筛选</span><input class="input" id="summary-date" type="date"></label><button class="button secondary" id="summary-date-button">按日期查看</button><button class="button ghost" id="summary-latest-button">恢复最新</button><div class="grow"></div><span class="badge" id="summary-count">加载中</span></div>
    <div class="card-grid" id="summary-grid"></div>`;
  const load = async date => {
    $("#summary-grid").innerHTML = '<div class="page-loading"><span class="spinner"></span></div>';
    const rows = await api(date ? `/api/summaries?date=${encodeURIComponent(date)}` : "/api/summaries/latest?limit=100");
    $("#summary-count").textContent = `${rows.length} 条`;
    $("#summary-grid").innerHTML = rows.length ? rows.map(row => {
      const saved = Boolean(bookmarkFor("summary", row._id));
      return `<article class="content-card"><div class="meta-row"><span class="badge info">${esc(row.channel_id)}</span><span>${esc(formatDateTime(row.start_time || row.created_at))}</span></div><h3>${esc(row.program_title)}</h3><p>${esc(shorten(row.summary_text, 150))}</p><footer><button class="button small secondary" data-summary-detail="${esc(row._id)}">查看详情</button>${row.recording_id ? `<button class="button small secondary" data-play="${esc(idOf(row.recording_id))}" data-title="${esc(row.program_title)}">播放</button>` : ""}<button class="button small ghost" data-bookmark-summary="${esc(row._id)}">${saved ? "♥ 已收藏" : "♡ 收藏"}</button></footer></article>`;
    }).join("") : emptyState("暂无摘要", date ? "该日期没有节目摘要。" : "完成节目处理后会显示在这里。");
  };
  $("#summary-date-button").addEventListener("click", () => load($("#summary-date").value).catch(renderError));
  $("#summary-latest-button").addEventListener("click", () => { $("#summary-date").value = ""; load().catch(renderError); });
  $("#summary-grid").addEventListener("click", async event => {
    const detail = event.target.closest("[data-summary-detail]");
    const play = event.target.closest("[data-play]");
    const bookmark = event.target.closest("[data-bookmark-summary]");
    if (detail) return openSummaryDetail(detail.dataset.summaryDetail);
    if (play) return openPlayer(play.dataset.play, play.dataset.title);
    if (bookmark) {
      try { await toggleBookmark("summary", bookmark.dataset.bookmarkSummary); load($("#summary-date").value || null); }
      catch (error) { toast(error.message, "error"); }
    }
  });
  await load();
}

async function openSummaryDetail(summaryId) {
  openModal("摘要详情", '<div class="page-loading"><span class="spinner"></span></div>', true);
  try {
    const row = await api(`/api/summaries/${summaryId}`);
    $("#modal-title").textContent = row.program_title || "摘要详情";
    $("#modal-body").innerHTML = `<div class="detail-stack"><div class="meta-row"><span class="badge info">${esc(row.channel_id)}</span><span>${esc(formatDateTime(row.start_time || row.created_at))}</span></div>${summarySections(row, row.transcript)}<div class="button-row">${row.recording_id ? `<button class="button" data-summary-play>▶ 播放关联录像</button>` : ""}</div></div>`;
    $("[data-summary-play]")?.addEventListener("click", () => openPlayer(idOf(row.recording_id), row.program_title));
  } catch (error) { $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

async function renderBookmarks() {
  const bookmarks = await loadBookmarks(true);
  $("#view").innerHTML = `${pageIntro("收藏节目摘要或录制。录像收藏可直接播放，原始文件也可下载。")}<div class="card-grid" id="bookmark-grid"><div class="page-loading"><span class="spinner"></span></div></div>`;
  const hydrated = await Promise.all(bookmarks.map(async mark => {
    try {
      const item = mark.item_type === "recording"
        ? await api(`/api/recordings/${idOf(mark.item_id)}`)
        : await api(`/api/summaries/${idOf(mark.item_id)}`);
      return { mark, item };
    } catch (error) { return { mark, error }; }
  }));
  $("#bookmark-grid").innerHTML = hydrated.length ? hydrated.map(({ mark, item, error }) => {
    if (error) return `<article class="content-card"><span class="badge danger">项目不可用</span><h3>失效收藏</h3><p>${esc(error.message)}</p><footer><button class="button small danger" data-remove-bookmark="${esc(mark._id)}">移除</button></footer></article>`;
    const recording = mark.item_type === "recording" ? item.recording : null;
    const summary = mark.item_type === "summary" ? item : item.summary;
    const title = recording?.program_title || summary?.program_title || "收藏项目";
    const recordingId = recording?._id || idOf(summary?.recording_id);
    return `<article class="content-card"><div class="meta-row"><span class="badge info">${mark.item_type === "recording" ? "录像" : "摘要"}</span><span>${esc(formatDateTime(mark.created_at))}</span></div><h3>${esc(title)}</h3><p>${esc(shorten(summary?.summary_text || `${recording?.channel_id || ""} · ${formatDateTime(recording?.start_time)}`, 140))}</p><footer>${recordingId ? `<button class="button small" data-play="${esc(recordingId)}" data-title="${esc(title)}">▶ 播放</button>` : ""}<button class="button small danger" data-remove-bookmark="${esc(mark._id)}">取消收藏</button></footer></article>`;
  }).join("") : emptyState("收藏夹还是空的", "可在录制或摘要页面点击收藏按钮。") ;
  $("#bookmark-grid").addEventListener("click", async event => {
    const play = event.target.closest("[data-play]");
    const remove = event.target.closest("[data-remove-bookmark]");
    if (play) return openPlayer(play.dataset.play, play.dataset.title);
    if (remove) {
      try { await api(`/api/bookmarks/${remove.dataset.removeBookmark}`, { method: "DELETE" }); appState.bookmarks = null; toast("已取消收藏"); renderRoute(); }
      catch (error) { toast(error.message, "error"); }
    }
  });
}

async function renderChannelsAdmin() {
  const channels = await api("/api/channels/admin");
  const categories = ["央视频道", "卫视频道", "国际频道", "地方与其他"];
  $("#view").innerHTML = `
    ${pageIntro("管理频道名称、分类、排序与启用状态。改名/改分类写入 override 字段，不会被次日 EPG 同步覆盖。", '<button class="button" id="admin-new-channel" type="button">＋ 添加频道</button> <button class="button secondary" id="admin-batch-enable" type="button">批量启用</button> <button class="button secondary" id="admin-batch-disable" type="button">批量禁用</button>')}
    <div class="toolbar"><label class="field"><span>分类筛选</span><select class="select" id="admin-category-filter"><option value="">全部分类</option>${categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></label><div class="grow"></div><span class="badge" id="admin-count">${channels.length} 个频道</span></div>
    <section class="panel"><div class="data-table-wrap"><table class="data-table"><thead><tr><th><input type="checkbox" id="admin-check-all" aria-label="全选"></th><th>名称</th><th>分类</th><th>线路</th><th>排序权重</th><th>状态</th><th></th></tr></thead><tbody id="admin-body"></tbody></table></div></section>`;
  const renderRows = () => {
    const filter = $("#admin-category-filter").value;
    const rows = channels.filter(row => !filter || row.category === filter);
    $("#admin-count").textContent = `${rows.length} 个频道`;
    $("#admin-body").innerHTML = rows.length ? rows.map(row => {
      const disabled = row.disabled === true;
      const deletable = row.source !== "iptv-api";
      return `<tr>
        <td><input type="checkbox" class="admin-check" data-channel-id="${esc(row.channel_id)}"></td>
        <td><strong>${esc(row.name)}</strong><small>${esc(row.channel_id)}${row.name_override ? " · 已改名" : ""}${row.category_override ? " · 已改分类" : ""}</small></td>
        <td>${esc(row.category)}</td>
        <td>${row.stream_count || 0}${row.active_state === "fallback" ? ' <span class="badge warning">回退中</span>' : row.source_locked ? ' <span class="badge info">首选锁定</span>' : ""}</td>
        <td>${row.sort_weight ?? "—"}</td>
        <td>${disabled ? '<span class="badge">已禁用</span>' : '<span class="badge success">启用</span>'}</td>
        <td><div class="table-actions"><button class="button small secondary" data-admin-edit="${esc(row.channel_id)}">编辑</button><button class="button small ghost" data-admin-detail="${esc(row.channel_id)}">详情</button><button class="button small ${disabled ? "secondary" : "danger"}" data-admin-toggle="${esc(row.channel_id)}">${disabled ? "启用" : "禁用"}</button>${deletable ? `<button class="button small danger" data-admin-delete="${esc(row.channel_id)}">删除</button>` : ""}</div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7">${emptyState(
      "没有频道",
      channels.length ? "当前筛选条件下没有频道。" : "首次使用可点击上方“添加频道”；批量导入请查看部署文档。",
      true,
    )}</td></tr>`;
  };
  renderRows();
  $("#admin-category-filter").addEventListener("change", renderRows);
  $("#admin-check-all").addEventListener("change", event => {
    $$(".admin-check").forEach(check => { check.checked = event.target.checked; });
  });
  const batch = async disabled => {
    const ids = $$(".admin-check:checked").map(check => check.dataset.channelId);
    if (!ids.length) return toast("请先勾选频道", "error");
    try {
      await api("/api/channels/batch", { method: "POST", body: { channel_ids: ids, disabled } });
      toast(disabled ? "已批量禁用" : "已批量启用");
      appState.channels = null;
      renderRoute();
    } catch (error) { toast(error.message, "error"); }
  };
  $("#admin-batch-enable").addEventListener("click", () => batch(false));
  $("#admin-batch-disable").addEventListener("click", () => batch(true));
  $("#admin-new-channel").addEventListener("click", () => openChannelCreateForm());
  $("#admin-body").addEventListener("click", async event => {
    const edit = event.target.closest("[data-admin-edit]");
    const detail = event.target.closest("[data-admin-detail]");
    const toggle = event.target.closest("[data-admin-toggle]");
    const del = event.target.closest("[data-admin-delete]");
    if (edit) return openChannelAdminForm(channels.find(row => row.channel_id === edit.dataset.adminEdit));
    if (detail) return openChannelDetail(detail.dataset.adminDetail);
    if (toggle) {
      const row = channels.find(item => item.channel_id === toggle.dataset.adminToggle);
      try {
        await api(`/api/channels/${encodeURIComponent(row.channel_id)}`, { method: "PATCH", body: { disabled: row.disabled !== true } });
        toast(row.disabled ? "频道已启用" : "频道已禁用");
        appState.channels = null;
        renderRoute();
      } catch (error) { toast(error.message, "error"); }
    }
    if (del) {
      const row = channels.find(item => item.channel_id === del.dataset.adminDelete);
      if (!confirm(`确定删除频道「${row?.name}」（${row?.channel_id}）？此操作不可撤销。`)) return;
      try {
        await api(`/api/channels/${encodeURIComponent(row.channel_id)}`, { method: "DELETE" });
        toast("频道已删除"); appState.channels = null; renderRoute();
      } catch (error) { toast(error.message, "error"); }
    }
  });
}

function openChannelCreateForm() {
  const rowHtml = (index, value = {}) => `
    <div class="source-input-row" data-source-row>
      <input class="input" name="source_url" required placeholder="http://... 或 https://..." value="${esc(value.url || "")}">
      <input class="input" name="source_name" maxlength="100" placeholder="线路名称（可选，如：官方组播）" value="${esc(value.name || "")}">
      <label class="inline-check"><input type="radio" name="primary_index" value="${index}" ${index === 0 ? "checked" : ""}> 首选</label>
      <button class="button small ghost" type="button" data-remove-source ${index === 0 ? "hidden" : ""}>移除</button>
    </div>`;
  openModal("添加频道", `<form id="channel-create-form" class="form-grid">
    <label class="field wide"><span>频道 ID</span><input class="input" name="channel_id" required maxlength="100" placeholder="自定义 ID，避免与内置频道冲突（如 MY-TV-1）"></label>
    <label class="field wide"><span>频道名称</span><input class="input" name="name" required maxlength="100" placeholder="例如：我的频道"></label>
    <div class="field wide"><span>直播线路</span>
      <div id="source-inputs" class="source-input-list">${rowHtml(0)}</div>
      <button class="button small secondary" type="button" id="add-source-row">＋ 添加备用线路</button>
      <p class="form-hint">第一条选中的线路会锁定为首选源；其他线路作为备用，首选不可用时自动回退。内置频道同步不会修改这里的地址。</p>
    </div>
    <label class="field"><span>分类</span><select class="select" name="category">${["央视频道","卫视频道","国际频道","地方与其他"].map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></label>
    <label class="field"><span>状态</span><select class="select" name="enabled"><option value="true" selected>启用</option><option value="false">禁用</option></select></label>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">添加</button></div>
  </form>`);
  const container = $("#source-inputs");
  const renumber = () => {
    $$("[data-source-row]", container).forEach((row, index) => {
      row.querySelector("[name=primary_index]").value = String(index);
      row.querySelector("[data-remove-source]").hidden = index === 0;
    });
  };
  $("#add-source-row").addEventListener("click", () => {
    if ($$("[data-source-row]", container).length >= 20) return toast("最多添加 20 条线路", "error");
    container.insertAdjacentHTML("beforeend", rowHtml($$("[data-source-row]", container).length));
    renumber();
  });
  container.addEventListener("click", event => {
    const remove = event.target.closest("[data-remove-source]");
    if (!remove) return;
    remove.closest("[data-source-row]").remove();
    renumber();
  });
  $("#channel-create-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const sources = [];
    $$("[data-source-row]", container).forEach(row => {
      const url = row.querySelector("[name=source_url]").value.trim();
      if (!url) return;
      sources.push({ url, name: row.querySelector("[name=source_name]").value.trim() });
    });
    if (!sources.length) return toast("请至少填写一条直播源 URL", "error");
    const primaryIndex = Number(data.get("primary_index") || 0);
    if (primaryIndex > 0) {
      const [primary] = sources.splice(primaryIndex, 1);
      sources.unshift(primary);
    }
    try {
      await api("/api/channels", { method: "POST", body: {
        channel_id: data.get("channel_id").trim(),
        name: data.get("name").trim(),
        sources,
        category: data.get("category"),
        enabled: data.get("enabled") === "true",
      }});
      toast("频道已添加"); closeModal(); appState.channels = null; renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

function openChannelAdminForm(channel) {
  openModal(`编辑频道 · ${esc(channel.channel_id)}`, `<form id="channel-admin-form" class="form-grid">
    <label class="field wide"><span>显示名称</span><input class="input" name="name_override" maxlength="100" value="${esc(channel.name_override || "")}" placeholder="${esc(channel.name)}（留空则用原始名称）"></label>
    <label class="field"><span>分类</span><select class="select" name="category_override"><option value="">（默认：${esc(channel.category)}）</option>${["央视频道","卫视频道","国际频道","地方与其他"].map(c => `<option value="${esc(c)}" ${channel.category_override === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
    <label class="field"><span>排序权重</span><input class="input" name="sort_weight" type="number" value="${channel.sort_weight ?? ""}" placeholder="留空=默认按名称，数字大排前"></label>
    <div class="field"><span>当前状态</span><p class="meta-row">${channel.disabled === true ? '<span class="badge">已禁用</span>' : '<span class="badge success">启用</span>'}</p></div>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">保存</button></div>
  </form>`);
  $("#channel-admin-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const body = {};
    const nameOverride = data.get("name_override").trim();
    const categoryOverride = data.get("category_override");
    const sortWeight = data.get("sort_weight").trim();
    body.name_override = nameOverride || null;
    body.category_override = categoryOverride || null;
    body.sort_weight = sortWeight === "" ? null : Number(sortWeight);
    try {
      await api(`/api/channels/${encodeURIComponent(channel.channel_id)}`, { method: "PATCH", body });
      toast("频道已更新"); closeModal(); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

async function openChannelDetail(channelId) {
  openModal("频道详情", '<div class="page-loading"><span class="spinner"></span></div>', true);
  try {
    const detail = await api(`/api/channels/${encodeURIComponent(channelId)}/detail`);
    const row = detail.channel;
    $("#modal-title").textContent = `${row.name} · ${channelId}`;
    const streams = detail.streams?.sources || [];
    const renderSources = () => streams.map((s, i) => `<tr>
        <td>${s.preferred ? '<span class="badge success">首选</span>' : ""}${s.locked && !s.preferred ? '<span class="badge info">已锁定</span>' : ""}${s.current && !s.preferred ? '<span class="badge warning">当前</span>' : ""}${!s.enabled ? '<span class="badge">已禁用</span>' : ""}<br><small>${esc(s.label || "")}</small></td>
        <td><span title="${esc(s.url)}">${esc((() => { try { return new URL(s.url).host; } catch { return s.url; } })())}</span>${s.quality?.valid === false ? `<small class="danger">${esc(shorten(s.quality?.error || "不可用", 40))}</small>` : ""}</td>
        <td><span class="badge">${esc(SOURCE_KIND_LABELS[s.kind] || "线路")}</span></td>
        <td><div class="table-actions">${!s.preferred && s.origin !== "seed" ? `<button class="button small secondary" data-detail-preferred="${esc(s.source_id)}">设为首选</button>` : ""}${s.origin !== "seed" ? `<button class="button small danger" data-detail-delete="${esc(s.source_id)}">删除</button>` : ""}</div></td>
      </tr>`).join("");
    $("#modal-body").innerHTML = `<div class="detail-stack">
      <dl class="definition-grid">
        <div><dt>频道</dt><dd>${esc(row.name)}</dd></div>
        <div><dt>分类</dt><dd>${esc(row.category)}</dd></div>
        <div><dt>EPG 匹配</dt><dd>${detail.epg?.matched ? `<span class="badge success">已匹配 · ${detail.epg.program_count} 条节目</span>` : '<span class="badge">未匹配</span>'}</dd></div>
        <div><dt>线路数</dt><dd>${streams.length} 条</dd></div>
      </dl>
      <section class="detail-section"><h3>直播线路</h3>
        <div class="button-row" style="margin-bottom:8px"><button class="button small secondary" type="button" data-detail-add-source>＋ 新增线路</button></div>
        ${streams.length ? `<table class="data-table"><thead><tr><th>线路</th><th>域名</th><th>类型</th><th></th></tr></thead><tbody>${renderSources()}</tbody></table>` : "<p>暂无线路</p>"}
      </section>
      <section class="detail-section"><h3>近 10 条录制</h3>${detail.recent_recordings?.length ? detail.recent_recordings.map(r => `<p class="meta-row"><span class="badge ${r.status === "completed" || r.status === "summarized" ? "success" : ""}">${esc(r.status)}</span><span>${esc(r.program_title || "—")}</span><span>${esc(formatDateTime(r.start_time))}</span></p>`).join("") : "<p>暂无录制</p>"}</section>
    </div>`;
    $("#modal-body").addEventListener("click", async event => {
      const preferred = event.target.closest("[data-detail-preferred]");
      const del = event.target.closest("[data-detail-delete]");
      const add = event.target.closest("[data-detail-add-source]");
      if (preferred) {
        try {
          await api(`/api/sources/${encodeURIComponent(preferred.dataset.detailPreferred)}/preferred`, { method: "POST" });
          toast("已设为首选并锁定"); await openChannelDetail(channelId);
        } catch (error) { toast(error.message, "error"); }
        return;
      }
      if (del) {
        const source = streams.find(item => item.source_id === del.dataset.detailDelete);
        if (!confirm(`确定删除线路「${source?.label || source?.url}」？`)) return;
        try {
          await api(`/api/sources/${encodeURIComponent(source.source_id)}`, { method: "DELETE" });
          toast("线路已删除"); await openChannelDetail(channelId);
        } catch (error) { toast(error.message, "error"); }
        return;
      }
      if (add) {
        const url = prompt("新的直播源 URL（http/https）：");
        if (!url) return;
        try {
          await api("/api/sources", { method: "POST", body: { name: `${row.name} 线路`, url: url.trim(), channel_id: channelId } });
          toast("线路已添加"); await openChannelDetail(channelId);
        } catch (error) { toast(error.message, "error"); }
      }
    });
  } catch (error) { $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

const SOURCE_KIND_LABELS = {
  udpxy: "组播",
  m3u: "HLS",
  custom: "自定义",
  backup_m3u: "备用M3U",
  legacy: "旧线路",
};

async function renderSources() {
  const [data, channels] = await Promise.all([api("/api/sources"), loadChannels()]);
  const summary = data.m3u_summary || {};
  const groups = data.groups || [];
  const grouped = new Map();
  for (const row of data.sources || []) {
    if (!row.channel_id) continue;
    if (!grouped.has(row.channel_id)) grouped.set(row.channel_id, []);
    grouped.get(row.channel_id).push(row);
  }
  for (const rows of grouped.values()) {
    rows.sort((a, b) => (a.priority || 0) - (b.priority || 0));
  }
  const unbound = data.unbound || [];
  $("#view").innerHTML = `
    ${pageIntro("管理直播线路。首选源已锁定，不会被 M3U 同步、质量刷新或规范化服务替换；首选不可用时会自动回退到备用源，恢复后自动切回。", '<button class="button" id="source-new" type="button">＋ 新增备用源</button><button class="button secondary" id="source-import" type="button">导入备用 M3U</button>')}
    <div class="toolbar">
      <label class="field grow"><span>筛选频道</span><input class="input" id="source-filter" placeholder="按频道名称或 ID 搜索"></label>
      <span class="badge" id="source-count">${(data.sources || []).length} 条线路</span>
    </div>
    <div class="meta-row" style="margin:0 0 12px"><span>${esc(summary.source || "M3U")} · 最后同步 ${esc(formatDateTime(summary.last_sync_at))} · 内置线路 ${summary.seed_stream_count ?? 0} 条</span></div>
    <div id="source-groups"></div>
    ${unbound.length ? `<section class="panel"><h3>待绑定线路</h3><div class="data-table-wrap"><table class="data-table"><thead><tr><th>名称</th><th>地址</th><th>类型</th><th>绑定频道</th><th></th></tr></thead><tbody>${unbound.map(row => {
      const host = (() => { try { return new URL(row.url).host; } catch { return row.url; } })();
      return `<tr>
        <td><strong>${esc(row.label || host)}</strong>${row.note ? `<small>${esc(row.note)}</small>` : ""}</td>
        <td><span title="${esc(row.url)}">${esc(host)}</span></td>
        <td><span class="badge info">${esc(SOURCE_KIND_LABELS[row.kind] || "线路")}</span></td>
        <td><select class="select" data-bind-source="${esc(row.source_id)}"><option value="">选择频道…</option>${channels.map(c => `<option value="${esc(c.channel_id)}">${esc(c.name || c.channel_id)}</option>`).join("")}</select></td>
        <td><div class="table-actions"><button class="button small ghost" data-source-test="${esc(row.url)}" data-source-name="${esc(row.label || row.channel_id || host)}">测试</button><button class="button small danger" data-source-delete="${esc(row.source_id)}">删除</button></div></td>
      </tr>`;
    }).join("")}</tbody></table></div></section>` : ""}`;
  const renderGroups = () => {
    const query = ($("#source-filter")?.value || "").trim().toLowerCase();
    const matches = (channelId, rows) => {
      if (!query) return true;
      const name = (rows[0]?.channel_name || channelId || "").toLowerCase();
      const label = (rows[0]?.group?.label || "").toLowerCase();
      return name.includes(query) || String(channelId).toLowerCase().includes(query) || label.includes(query);
    };
    const byGroup = new Map();
    for (const [channelId, rows] of grouped.entries()) {
      if (!matches(channelId, rows)) continue;
      const key = rows[0]?.group?.key || "custom";
      if (!byGroup.has(key)) byGroup.set(key, { label: rows[0]?.group?.label || "其他来源", channels: [] });
      byGroup.get(key).channels.push([channelId, rows]);
    }
    const channelCount = [...byGroup.values()].reduce((total, group) => total + group.channels.length, 0);
    const lineCount = [...byGroup.values()].reduce(
      (total, group) => total + group.channels.reduce((sum, [, rows]) => sum + rows.length, 0), 0);
    $("#source-count").textContent = `${lineCount} 条线路 · ${channelCount} 个频道 · ${byGroup.size} 个来源`;
    $("#source-groups").innerHTML = byGroup.size ? [...byGroup.entries()].map(([key, group]) => {
      const known = groups.find(item => item.key === key);
      return `<section class="panel" style="margin-bottom:16px">
        <div class="source-group-head">
          <h3>${esc(group.label)}</h3>
          <span class="badge">${esc(key)}</span>
          <span class="badge">${group.channels.length} 个频道</span>
          <span class="badge">${group.channels.reduce((sum, [, rows]) => sum + rows.length, 0)} 条线路</span>
          ${known?.origin ? `<span class="badge">${esc(SOURCE_ORIGIN_LABELS[known.origin] || known.origin)}</span>` : ""}
        </div>
        <div class="data-table-wrap"><table class="data-table"><thead><tr><th>频道</th><th>线路</th><th>地址</th><th>类型</th><th>状态</th><th>质量</th><th></th></tr></thead><tbody>
        ${group.channels.map(([channelId, rows]) => rows.map((row, index) => {
          const host = (() => { try { return new URL(row.url).host; } catch { return row.url; } })();
          const marks = [];
          if (row.preferred) marks.push('<span class="badge success">首选</span>');
          if (row.locked) marks.push('<span class="badge info">已锁定</span>');
          if (row.current && !row.preferred) marks.push('<span class="badge warning">当前</span>');
          if (!row.enabled) marks.push('<span class="badge">已禁用</span>');
          if (row.origin === "seed") marks.push('<span class="badge">内置</span>');
          if (row.origin === "import") marks.push('<span class="badge warning">导入</span>');
          return `<tr>
            <td>${index === 0 ? `<strong>${esc(row.channel_name || channelId)}</strong><small>${esc(channelId)}</small>` : ""}</td>
            <td>${marks.join(" ")}<br><small>${esc(row.label || "")}</small></td>
            <td><span title="${esc(row.url)}">${esc(host)}</span>${row.last_error ? `<small class="danger">${esc(shorten(row.last_error, 60))}</small>` : ""}${row.recording_health ? `<small>录制稳定性 ${row.recording_health.score}% · 最近 ${row.recording_health.attempts} 次</small>` : ''}</td>
            <td><span class="badge">${esc(SOURCE_KIND_LABELS[row.kind] || "线路")}</span></td>
            <td>${row.enabled ? "启用" : "禁用"}</td>
            <td>${esc((streamQualityLabel(row.quality) || "").replace(/^ · /, "") || "—")}</td>
            <td><div class="table-actions">
              <button class="button small ghost" data-source-test="${esc(row.url)}" data-source-name="${esc(row.label || row.channel_id)}">测试</button>
              ${!row.preferred ? `<button class="button small secondary" data-source-preferred="${esc(row.source_id)}">设为首选</button><button class="button small ghost" data-source-use="${esc(row.source_id)}">临时使用</button>` : ""}
              <button class="button small ghost" data-source-toggle="${esc(row.source_id)}" data-source-enabled="${row.enabled ? "false" : "true"}">${row.enabled ? "禁用" : "启用"}</button>
              ${row.origin !== "seed" ? `<button class="button small secondary" data-source-edit="${esc(row.source_id)}">编辑</button><button class="button small danger" data-source-delete="${esc(row.source_id)}">删除</button>` : ""}
            </div></td>
          </tr>`;
        }).join("")).join("")}
        </tbody></table></div></section>`;
    }).join("") : emptyState("没有匹配的线路", "调整筛选或新增备用源。", true);
  };
  renderGroups();
  $("#source-filter").addEventListener("input", renderGroups);
  $("#source-new").addEventListener("click", () => openSourceForm(channels));
  $("#source-import").addEventListener("click", () => openImportForm());
  bindSourceDelegation(data.sources || [], channels);
}

const SOURCE_ORIGIN_LABELS = {
  seed: "IPTV-API 同步",
  custom: "用户自定义",
  import: "M3U 导入",
  migration: "历史迁移",
};

function bindSourceDelegation(rows, channels) {
  const view = $("#view");
  if (view._sourceClick) view.removeEventListener("click", view._sourceClick);
  if (view._sourceChange) view.removeEventListener("change", view._sourceChange);
  view._sourceClick = event => handleSourceActions(event, rows, channels);
  view._sourceChange = event => {
    const bind = event.target.closest("[data-bind-source]");
    if (!bind || !bind.value) return;
    api(`/api/sources/${encodeURIComponent(bind.dataset.bindSource)}`, { method: "PATCH", body: { channel_id: bind.value } })
      .then(() => { toast("线路已绑定到频道"); renderRoute(); })
      .catch(error => toast(error.message, "error"));
  };
  view.addEventListener("click", view._sourceClick);
  view.addEventListener("change", view._sourceChange);
}

async function handleSourceActions(event, rows, channels) {
  const test = event.target.closest("[data-source-test]");
  const preferred = event.target.closest("[data-source-preferred]");
  const use = event.target.closest("[data-source-use]");
  const toggle = event.target.closest("[data-source-toggle]");
  const edit = event.target.closest("[data-source-edit]");
  const del = event.target.closest("[data-source-delete]");
  if (test) return testSourceUrl(test.dataset.sourceTest, test.dataset.sourceName);
  if (preferred) {
    if (!confirm("设为该频道的首选源后，它会锁定并排在第一位。确定继续？")) return;
    try {
      await api(`/api/sources/${encodeURIComponent(preferred.dataset.sourcePreferred)}/preferred`, { method: "POST" });
      toast("已设为首选并锁定"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
    return;
  }
  if (use) {
    try {
      await api(`/api/sources/${encodeURIComponent(use.dataset.sourceUse)}/activate`, { method: "POST", body: { mode: "temporary" } });
      toast("已临时切换；首选源保持不变"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
    return;
  }
  if (toggle) {
    try {
      await api(`/api/sources/${encodeURIComponent(toggle.dataset.sourceToggle)}`, { method: "PATCH", body: { enabled: toggle.dataset.sourceEnabled === "true" } });
      toast("线路状态已更新"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
    return;
  }
  if (edit) {
    const row = rows.find(item => item.source_id === edit.dataset.sourceEdit);
    return openSourceForm(channels, row);
  }
  if (del) {
    const row = rows.find(item => item.source_id === del.dataset.sourceDelete);
    if (!confirm(`确定删除线路「${row?.label || row?.url}」？`)) return;
    try {
      await api(`/api/sources/${encodeURIComponent(row.source_id)}`, { method: "DELETE" });
      toast("线路已删除"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  }
}

function openSourceForm(channels, existing = null) {
  openModal(existing ? "编辑直播源" : "新增备用源", `<form id="source-form" class="form-grid">
    <label class="field wide"><span>名称</span><input class="input" name="name" required maxlength="100" value="${esc(existing?.label || "")}" placeholder="例如：央视备用线路"></label>
    <label class="field wide"><span>线路 URL</span><input class="input" name="url" required value="${esc(existing?.url || "")}" placeholder="http://... 或 https://..."></label>
    <label class="field wide"><span>绑定频道（可选）</span><select class="select" name="channel_id"><option value="">不绑定</option>${channels.map(c => `<option value="${esc(c.channel_id)}" ${c.channel_id === (existing?.channel_id || "") ? "selected" : ""}>${esc(c.name || c.channel_id)}</option>`).join("")}</select></label>
    <label class="field wide"><span>备注</span><input class="input" name="note" maxlength="200" value="${esc(existing?.note || "")}" placeholder="可选，例如：备用线路、1080p"></label>
    <label class="field"><span>状态</span><select class="select" name="enabled"><option value="true" ${existing?.enabled !== false ? "selected" : ""}>启用</option><option value="false" ${existing?.enabled === false ? "selected" : ""}>禁用</option></select></label>
    <p class="form-hint wide">新增的线路默认作为备用源，不会替换频道已锁定的首选源。需要替换时在列表中点“设为首选”。</p>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">${existing ? "保存" : "添加"}</button></div>
  </form>`);
  $("#source-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const body = {
      name: data.get("name").trim(),
      url: data.get("url").trim(),
      note: data.get("note").trim(),
      enabled: data.get("enabled") === "true",
      channel_id: data.get("channel_id") || null,
    };
    try {
      await api(existing ? `/api/sources/${encodeURIComponent(existing.source_id)}` : "/api/sources", { method: existing ? "PATCH" : "POST", body });
      toast(existing ? "线路已更新" : "备用源已添加"); closeModal(); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

function openImportForm() {
  openModal("导入备用 M3U", `<form id="import-form" class="form-grid">
    <label class="field wide"><span>M3U 地址或容器内文件路径</span><input class="input" name="source" required placeholder="https://... 或 /data/m3u/backup.m3u"></label>
    <label class="field wide"><span>匹配方式</span>
      <div class="inline-checks">
        <label><input type="checkbox" name="match" value="tvg_id" checked> tvg-id</label>
        <label><input type="checkbox" name="match" value="name" checked> 名称</label>
        <label><input type="checkbox" name="match" value="normalized_name" checked> 归一化名称</label>
      </div>
    </label>
    <p class="form-hint wide">导入只会新增备用源，不会替换任何首选源。先预览，确认后再写入。</p>
    <div id="import-preview" class="wide"></div>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="button" id="import-preview-btn">预览</button><button class="button secondary" type="button" id="import-apply-btn" disabled>确认导入</button></div>
  </form>`);
  let lastSource = "";
  let lastMatch = [];
  const collect = () => ({
    source: $("#import-form [name=source]").value.trim(),
    match: [...document.querySelectorAll("#import-form [name=match]:checked")].map(item => item.value),
  });
  $("#import-preview-btn").addEventListener("click", async () => {
    const body = collect();
    if (!body.source) return toast("请填写 M3U 地址或路径", "error");
    $("#import-preview").innerHTML = '<div class="page-loading"><span class="spinner"></span><p>正在匹配频道…</p></div>';
    try {
      const result = await api("/api/sources/import", { method: "POST", body: { ...body, dry_run: true } });
      lastSource = body.source;
      lastMatch = body.match;
      const counts = result.counts || {};
      $("#import-preview").innerHTML = `<div class="meta-row"><span class="badge success">可新增 ${counts.create || 0}</span><span class="badge">已存在 ${counts.exists || 0}</span><span class="badge warning">未匹配 ${counts.unbound || 0}</span></div>
        <div class="data-table-wrap"><table class="data-table"><thead><tr><th>频道</th><th>匹配方式</th><th>线路名称</th><th>结果</th></tr></thead><tbody>${(result.rows || []).slice(0, 50).map(row => `<tr><td>${esc(row.channel_name || row.channel_id || "—")}</td><td>${esc(row.matched_by || "未匹配")}</td><td>${esc(shorten(row.name, 40))}</td><td>${row.action === "create" ? '<span class="badge success">新增备用</span>' : row.action === "exists" ? '<span class="badge">已存在</span>' : '<span class="badge warning">待绑定</span>'}</td></tr>`).join("")}</tbody></table></div>`;
      $("#import-apply-btn").disabled = !(counts.create > 0);
    } catch (error) {
      $("#import-preview").innerHTML = `<p class="danger">${esc(error.message)}</p>`;
      $("#import-apply-btn").disabled = true;
    }
  });
  $("#import-apply-btn").addEventListener("click", async () => {
    try {
      const result = await api("/api/sources/import", { method: "POST", body: { source: lastSource, match: lastMatch, dry_run: false } });
      toast(`已导入 ${result.created || 0} 条备用线路`);
      closeModal();
      renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

async function testSourceUrl(url, channelId) {
  openModal("线路测试", '<div class="page-loading"><span class="spinner"></span><p>正在探测线路…</p></div>', true);
  $("#modal-title").textContent = `测试 · ${channelId}`;
  try {
    const result = await api("/api/sources/test", { method: "POST", body: { url } });
    const seg = result.first_segment;
    $("#modal-body").innerHTML = `<div class="detail-stack">
      <dl class="definition-grid">
        <div><dt>结果</dt><dd>${result.ok ? '<span class="badge success">可用</span>' : '<span class="badge danger">不可用</span>'}</dd></div>
        <div><dt>HTTP 状态</dt><dd>${esc(result.status ?? "—")}</dd></div>
        <div><dt>耗时</dt><dd>${esc(result.elapsed_ms)} ms</dd></div>
        <div><dt>首分片</dt><dd>${seg ? `${esc(seg.status)} · ${esc(seg.size_bytes)} B · ${esc(seg.elapsed_ms)} ms` : (seg?.error ? `失败：${esc(seg.error)}` : "无")}</dd></div>
      </dl>
      ${result.error ? `<div class="notice warning">${esc(result.error)}</div>` : ""}
      <div class="button-row"><button class="button secondary" data-close-modal type="button">关闭</button></div>
    </div>`;
    $("[data-close-modal]")?.addEventListener("click", closeModal);
  } catch (error) { $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

async function renderSystem() {
  const [status, events, health, buffer, push, encoding] = await Promise.all([
    api("/api/system/status"),
    api("/api/push/events?limit=100"),
    api("/health"),
    api("/api/settings/buffer"),
    api("/api/settings/push").catch(() => null),
    api("/api/settings/recording").catch(() => null),
  ]);
  const recordingTotal = Object.values(status.recordings_by_status || {}).reduce((sum, value) => sum + value, 0);
    const pipelineLabels = { asr: "录后转写", live_asr: "实时转写", encoder: "录像压缩", llm: "节目摘要" };
  const pipelineRows = Object.entries(status.pipeline || {}).map(([name, row]) => `<tr><td>${esc(pipelineLabels[name] || name)}</td><td>${row.pending}${row.retrying ? `（退避 ${row.retrying}）` : ""}</td><td>${row.active}</td><td>${row.blocked || 0}</td><td>${row.failed}</td><td><span class="badge ${row.stalled ? "danger" : "success"}">${row.draining ? "等待维护" : row.stalled ? "进度异常" : "正常"}</span></td><td>${esc(formatDateTime(row.last_success_at))}</td></tr>`).join("");
  const profileOptions = Object.values(buffer.profiles || {}).map(row => `<option value="${esc(row.key)}" ${row.key === buffer.profile ? "selected" : ""}>${esc(row.label)}（缓冲 ${row.buffer_duration}s · 同步 ${row.live_sync} 段）</option>`).join("");
  const pushEmail = push?.email || {};
  const pushProviderOptions = [["none", "关闭"], ["email", "邮箱"]]
    .map(([value, label]) => `<option value="${value}" ${push?.provider === value ? "selected" : ""}>${label}</option>`).join("");
  const encodingOptions = [[0, "保持原分辨率"], [1080, "1080p"], [720, "720p（推荐）"], [540, "540p"], [480, "480p"]]
    .map(([value, label]) => `<option value="${value}" ${Number(encoding?.height ?? 720) === value ? "selected" : ""}>${label}</option>`).join("");
  $("#view").innerHTML = `
    ${pageIntro("配置直播缓冲、邮件推送和录制压缩，并查看推送审计。容器与磁盘明细未通过管理 API 暴露，避免把宿主权限交给网页。")}
    <div class="stats-grid"><article class="stat-card"><small>可播频道</small><strong>${esc(status.channels)}</strong></article><article class="stat-card"><small>EPG 节目</small><strong>${esc(status.epg_programs)}</strong></article><article class="stat-card"><small>启用订阅</small><strong>${esc(status.subscriptions)}</strong></article><article class="stat-card"><small>录制记录</small><strong>${esc(recordingTotal)}</strong></article></div>
    <section class="panel"><div class="panel-header"><h2>处理队列</h2><span class="badge">刷新页面更新</span></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>阶段</th><th>待处理</th><th>处理中</th><th>等待上游</th><th>失败</th><th>状态</th><th>最近完成</th></tr></thead><tbody>${pipelineRows}</tbody></table></div></section>
    <div class="settings-layout">
      <div class="detail-stack">
        <section class="panel"><div class="panel-header"><h2>直播缓冲档位</h2><span class="badge">${esc(buffer.profile)}</span></div><div class="panel-body compact"><label class="field compact"><span>播放缓冲模式</span><select class="select" id="buffer-profile-select">${profileOptions}</select></label><p class="meta-row" style="margin-top:6px">低延迟≈低缓冲更实时、流畅≈大缓冲更稳定；切换后新打开的直播立即生效。</p></div></section>
        <section class="panel"><div class="panel-header"><h2>推送配置</h2><span class="badge">${push?.provider === "email" ? "邮件" : "已关闭"}</span></div><div class="panel-body">
          <form id="push-form" class="form-grid">
            <label class="field"><span>推送渠道</span><select class="select" name="provider">${pushProviderOptions}</select></label>
            <label class="field"><span>收件邮箱</span><input class="input" name="recipient" type="email" value="${esc(pushEmail.recipient || "")}" placeholder="name@example.com"></label>
            <label class="field"><span>SMTP 主机</span><input class="input" name="host" value="${esc(pushEmail.host || "")}" placeholder="smtp.example.com"></label>
            <label class="field"><span>端口</span><input class="input" name="port" type="number" min="1" max="65535" value="${esc(pushEmail.port ?? 465)}"></label>
            <label class="field"><span>账号</span><input class="input" name="username" value="${esc(pushEmail.username || "")}" autocomplete="off"></label>
            <label class="field"><span>密码 / 授权码</span><input class="input" name="password" type="password" autocomplete="new-password" placeholder="${pushEmail.has_password ? "已保存，留空则不修改" : "未设置"}"></label>
            <label class="field"><span>发件人</span><input class="input" name="sender" value="${esc(pushEmail.sender || "")}" placeholder="留空则使用账号"></label>
            <label class="field"><span>加密</span><select class="select" name="use_ssl"><option value="true" ${pushEmail.use_ssl !== false ? "selected" : ""}>SSL / 465</option><option value="false" ${pushEmail.use_ssl === false ? "selected" : ""}>STARTTLS</option></select></label>
            <div class="button-row wide"><button class="button" type="submit">保存推送配置</button><button class="button secondary" type="button" id="push-test">发送测试邮件</button></div>
            <p class="form-hint wide">保存后推送服务即时生效，无需重启容器。修改的配置优先于 .env。</p>
          </form>
        </div></section>
        <section class="panel"><div class="panel-header"><h2>录制压缩</h2><span class="badge">${encoding?.enabled ? "已开启" : "已关闭"}</span></div><div class="panel-body">
          <form id="encoding-form" class="form-grid">
            <label class="field"><span>转码</span><select class="select" name="enabled"><option value="true" ${encoding?.enabled !== false ? "selected" : ""}>开启（录制后转 MP4）</option><option value="false" ${encoding?.enabled === false ? "selected" : ""}>关闭（保留原始 TS）</option></select></label>
            <label class="field"><span>分辨率</span><select class="select" name="height">${encodingOptions}</select></label>
            <label class="field"><span>画质 CRF</span><input class="input" name="crf" type="number" min="18" max="34" value="${esc(encoding?.crf ?? 26)}"><small class="form-hint">数值越小画质越好、体积越大；推荐 26。</small></label>
            <label class="field"><span>编码速度</span><select class="select" name="preset">${["ultrafast","veryfast","faster","fast","medium","slow"].map(p => `<option value="${p}" ${encoding?.preset === p ? "selected" : ""}>${p}</option>`).join("")}</select></label>
            <label class="field"><span>原文件</span><select class="select" name="keep_original"><option value="false" ${encoding?.keep_original !== true ? "selected" : ""}>转码后删除 TS</option><option value="true" ${encoding?.keep_original === true ? "selected" : ""}>保留 TS</option></select></label>
            <div class="button-row wide"><button class="button" type="submit">保存压缩配置</button></div>
            <p class="form-hint wide">录制仍以原画质保存为 TS，避免源不稳定导致丢流；转码在后台进行，完成后替换为 MP4。压缩在有 ASR 的录像完成摘要后进行。</p>
          </form>
        </div></section>
      </div>
      <div class="detail-stack">
        <section class="panel"><div class="panel-header"><h2>推送事件</h2><span class="badge">${events.length} 条</span></div><div class="data-table-wrap push-event-table"><table class="data-table"><thead><tr><th>通道</th><th>状态</th><th>更新时间</th></tr></thead><tbody>${events.map(row => `<tr><td>${esc(row.provider)}</td><td><span class="badge ${row.status === "delivered" ? "success" : row.status === "failed" ? "danger" : ""}">${esc(row.status)}</span></td><td>${esc(formatDateTime(row.updated_at))}</td></tr>`).join("") || '<tr><td colspan="3">暂无事件</td></tr>'}</tbody></table></div></section>
        <section class="panel"><div class="panel-header"><h2>直播规范化池</h2><span class="badge">${(status.normalizer_pool?.channels || []).length}/${status.normalizer_pool?.max_concurrency ?? 2} 路</span></div><div class="panel-body">${(status.normalizer_pool?.channels || []).length ? status.normalizer_pool.channels.map(row => `<p class="meta-row"><span class="badge info">手动</span><span>${esc(row.name)}</span><span>${row.healthy === true ? '<span class="badge success">健康</span>' : row.healthy === false ? '<span class="badge danger">异常</span>' : '<span class="badge">未知</span>'}</span><span>${esc(formatDateTime(row.measured_at))}</span></p>`).join("") : '<p class="meta-row">当前无手动指定的规范化频道。</p>'}</div></section>
        <section class="panel"><div class="panel-header"><h2>服务状态</h2></div><div class="panel-body"><p><span class="badge success">API 正常</span></p><p class="meta-row">检查时间：${esc(formatDateTime(health.time))}</p><p class="meta-row">最新摘要：${esc(formatDateTime(status.latest_summary_at))}</p></div></section>
        <section class="panel"><div class="panel-header"><h2>API 访问令牌</h2></div><div class="panel-body"><p class="meta-row">仅在 NAS 开启 WEB_API_TOKEN 后填写，保存在本次浏览器会话中。</p><label class="field"><span>Bearer Token</span><input class="input" id="api-token" type="password" autocomplete="off" value="${esc(sessionStorage.getItem("iptv_api_token") || "")}"></label><div class="button-row" style="margin-top:12px"><button class="button small" id="save-token">保存</button><button class="button small secondary" id="clear-token">清除</button></div></div></section>
      </div>
    </div>`;
  $("#buffer-profile-select").addEventListener("change", async event => {
    const profile = event.target.value;
    try {
      await api("/api/settings/buffer", { method: "PATCH", body: { profile } });
      appState.bufferProfileCache = null;
      toast("缓冲档位已切换，新打开的直播立即生效");
      renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#push-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await api("/api/settings/push", { method: "PATCH", body: {
        provider: data.get("provider"),
        email: {
          recipient: data.get("recipient").trim(),
          host: data.get("host").trim(),
          port: Number(data.get("port") || 465),
          username: data.get("username").trim(),
          password: data.get("password"),
          sender: data.get("sender").trim(),
          use_ssl: data.get("use_ssl") === "true",
        },
      }});
      toast("推送配置已保存，即时生效"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#push-test").addEventListener("click", async () => {
    try {
      const result = await api("/api/settings/push/test", { method: "POST" });
      toast(`测试邮件已发送至 ${result.recipient}`);
    } catch (error) { toast(error.message, "error"); }
  });
  $("#encoding-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      await api("/api/settings/recording", { method: "PATCH", body: {
        enabled: data.get("enabled") === "true",
        height: Number(data.get("height")),
        crf: Number(data.get("crf")),
        preset: data.get("preset"),
        keep_original: data.get("keep_original") === "true",
      }});
      toast("压缩配置已保存"); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
  $("#save-token").addEventListener("click", () => { sessionStorage.setItem("iptv_api_token", $("#api-token").value.trim()); toast("令牌已保存到当前会话"); });
  $("#clear-token").addEventListener("click", () => { sessionStorage.removeItem("iptv_api_token"); $("#api-token").value = ""; toast("会话令牌已清除"); });
}

async function renderRoute() {
  const token = ++appState.renderToken;
  const route = (location.hash.replace(/^#\//, "") || "epg").split("?")[0];
  const current = routeMeta[route] ? route : "epg";
  const [title, eyebrow] = routeMeta[current];
  $("#page-title").textContent = title;
  $("#page-eyebrow").textContent = eyebrow;
  $$(`[data-route]`).forEach(link => link.classList.toggle("active", link.dataset.route === current));
  loading();
  const renderers = { epg: renderEpg, channels: renderChannelsAdmin, sources: renderSources, subscriptions: renderSubscriptions, recordings: renderRecordings, summaries: renderSummaries, bookmarks: renderBookmarks, system: renderSystem };
  try {
    await renderers[current]();
    if (token === appState.renderToken) $("#view").focus({ preventScroll: true });
  } catch (error) {
    if (token === appState.renderToken) renderError(error);
  }
}

async function checkHealth() {
  const pill = $("#health-pill");
  try {
    await api("/health");
    pill.className = "health-pill ok";
    $("span", pill).textContent = "服务正常";
  } catch {
    pill.className = "health-pill bad";
    $("span", pill).textContent = "连接异常";
  }
}

$("#modal").addEventListener("click", event => {
  if (event.target === $("#modal") || event.target.closest("[data-close-modal]")) closeModal();
});
document.addEventListener("keydown", event => { if (event.key === "Escape" && !$("#modal").hidden) closeModal(); });
$("#refresh-button").addEventListener("click", () => { appState.channels = null; appState.bookmarks = null; renderRoute(); checkHealth(); });
document.querySelectorAll(".nav-group-toggle").forEach(toggle => {
  toggle.addEventListener("click", () => {
    const group = toggle.closest(".nav-group");
    const collapsed = group.classList.toggle("collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
  });
});
window.addEventListener("hashchange", renderRoute);
if (!location.hash) location.hash = "#/epg";
renderRoute();
checkHealth();
setInterval(checkHealth, 60000);
