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
};

const routeMeta = {
  epg: ["节目单", "CHANNELS & EPG"],
  channels: ["频道管理", "CHANNEL ADMIN"],
  sources: ["直播源", "STREAM SOURCES"],
  subscriptions: ["我的订阅", "SUBSCRIPTIONS"],
  recordings: ["录制与处理", "RECORDINGS"],
  summaries: ["节目摘要", "AI SUMMARIES"],
  bookmarks: ["我的收藏", "BOOKMARKS"],
  system: ["系统状态", "SYSTEM"],
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
  const modal = $("#modal");
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $(".modal-panel", modal).classList.toggle("wide", wide);
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  requestAnimationFrame(() => $("button, input, select, textarea", $("#modal-body"))?.focus());
}

function closeModal() {
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
  const value = { asr_enabled: true, asr_model: "small", llm_enabled: true, push_enabled: true, auto_bookmark: false, post_roll_seconds: 180, enabled: true, ...preset, ...(existing || {}) };
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
      <div class="field"><span>自动处理</span><div class="checks"><label class="check"><input type="checkbox" name="asr_enabled" ${value.asr_enabled ? "checked" : ""}>转写</label><label class="check"><input type="checkbox" name="llm_enabled" ${value.llm_enabled ? "checked" : ""}>摘要</label><label class="check"><input type="checkbox" name="push_enabled" ${value.push_enabled ? "checked" : ""}>邮件</label><label class="check"><input type="checkbox" name="auto_bookmark" ${value.auto_bookmark ? "checked" : ""}>自动收藏摘要</label></div></div>
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
      asr_enabled: data.has("asr_enabled"), asr_model: data.get("asr_model"),
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
  $("#view").innerHTML = `
    ${pageIntro("追踪从排队、录制、转写、摘要到邮件推送的完整处理状态。", '<button class="button" id="manual-recording" type="button">● 手工录制</button>')}
    <div class="toolbar"><label class="field"><span>状态筛选</span><select class="select" id="recording-filter"><option value="">全部状态</option>${Object.entries(statusMeta).map(([key, meta]) => `<option value="${key}">${meta[0]}</option>`).join("")}</select></label><div class="grow"></div><span class="badge" id="recording-count">${recordings.length} 条</span></div>
    <section class="panel"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>节目</th><th>时间</th><th>状态</th><th>文件</th><th></th></tr></thead><tbody id="recording-body"></tbody></table></div></section>`;
  const renderRows = () => {
    const filter = $("#recording-filter").value;
    const rows = recordings.filter(row => !filter || row.status === filter);
    $("#recording-count").textContent = `${rows.length} 条`;
    $("#recording-body").innerHTML = rows.length ? rows.map(row => {
      const saved = Boolean(bookmarkFor("recording", row._id));
      const playable = Boolean(row.file_path) && !["queued","recording","failed"].includes(row.status);
      const incomplete = row.partial ? `<span class="badge danger" title="本次录制不完整，缺失开头约 ${esc(row.missed_seconds || 0)} 秒">不完整</span> ` : "";
      return `<tr><td><strong>${esc(row.program_title)}</strong><small>${esc(row.channel_id)}${row.manual ? " · 手工" : ""}${row.epg_time_suspect ? " · EPG时间存疑" : ""}</small></td><td>${esc(formatDateTime(row.start_time))}</td><td>${incomplete}${badge(row.status)}</td><td>${esc(formatBytes(row.size_bytes))}</td><td><div class="table-actions"><button class="button small ghost" data-watch-channel="${esc(row.channel_id)}">直播</button><button class="button small secondary" data-recording-detail="${esc(row._id)}">详情</button>${playable ? `<button class="button small secondary" data-play="${esc(row._id)}" data-title="${esc(row.program_title)}">播放</button>` : ""}<button class="button small ghost" data-bookmark-recording="${esc(row._id)}">${saved ? "♥" : "♡"}</button></div></td></tr>`;
    }).join("") : `<tr><td colspan="5">${emptyState("暂无记录", "当前筛选条件下没有录制。", true)}</td></tr>`;
  };
  renderRows();
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
    <div class="field"><span>后续处理</span><div class="checks"><label class="check"><input type="checkbox" name="asr_enabled" checked>转写</label><label class="check"><input type="checkbox" name="llm_enabled" checked>摘要</label><label class="check"><input type="checkbox" name="push_enabled">邮件</label></div></div>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">开始录制</button></div>
  </form>`);
  $("#manual-form").addEventListener("submit", async event => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      await api("/api/recordings/manual", { method: "POST", body: {
        channel_id: data.get("channel_id"), duration_sec: Number(data.get("duration_sec")),
        program_title: data.get("program_title").trim(), asr_enabled: data.has("asr_enabled"),
        asr_model: data.get("asr_model"), llm_enabled: data.has("llm_enabled"), push_enabled: data.has("push_enabled"),
      }});
      toast("录制任务已创建"); closeModal(); renderRoute();
    } catch (error) { toast(error.message, "error"); }
  });
}

function summarySections(summary, transcript) {
  if (!summary && !transcript) return '<div class="notice">当前还没有转写或摘要。</div>';
  return `<div class="detail-stack">
    ${summary ? `<section class="detail-section"><h3>内容摘要</h3><p>${esc(summary.summary_text)}</p></section>
      <section class="detail-section"><h3>核心要点</h3><ol class="key-points">${(summary.key_points || []).map(point => `<li>${esc(point)}</li>`).join("") || "<li>暂无</li>"}</ol></section>
      <section class="detail-section"><h3>实体与跟进</h3><div class="tag-list">${(summary.entities || []).map(entity => `<span class="tag">${esc(typeof entity === "string" ? entity : JSON.stringify(entity))}</span>`).join("") || '<span class="tag">暂无实体</span>'}</div>${summary.follow_up ? `<p style="margin-top:12px">${esc(summary.follow_up)}</p>` : ""}</section>` : ""}
    ${transcript ? `<section class="detail-section"><h3>转写全文</h3><p>${esc(transcript.full_text || "暂无转写文本")}</p></section>` : ""}
  </div>`;
}

async function openRecordingDetail(recordingId) {
  openModal("录制详情", '<div class="page-loading"><span class="spinner"></span></div>', true);
  try {
    const detail = await api(`/api/recordings/${recordingId}`);
    const row = detail.recording;
    $("#modal-title").textContent = row.program_title || "录制详情";
    $("#modal-body").innerHTML = `<div class="detail-stack">
      ${row.partial ? `<div class="notice warning">本次录制不完整，缺失开头约 ${esc(row.missed_seconds || 0)} 秒。</div>` : ""}
      ${row.epg_time_suspect ? `<div class="notice warning">EPG 时间可能不准，建议加大录制缓冲。</div>` : ""}
      <dl class="definition-grid"><div><dt>频道</dt><dd>${esc(row.channel_id)}</dd></div><div><dt>状态</dt><dd>${badge(row.status)}</dd></div><div><dt>开始时间</dt><dd>${esc(formatDateTime(row.start_time))}</dd></div><div><dt>时长 / 大小</dt><dd>${esc(row.duration_sec || "—")} 秒 · ${esc(formatBytes(row.size_bytes))}</dd></div></dl>
      <div class="button-row">${row.file_path ? `<button class="button" data-detail-play type="button">▶ 播放</button><a class="button secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/download">下载原文件</a>` : ""}</div>
      ${summarySections(detail.summary, detail.transcript)}
      <section class="detail-section"><h3>推送事件</h3>${detail.push_events?.length ? detail.push_events.map(item => `<p>${esc(item.provider)} · ${esc(item.status)} · ${esc(formatDateTime(item.updated_at))}</p>`).join("") : "<p>暂无推送事件</p>"}</section>
    </div>`;
    $("[data-detail-play]")?.addEventListener("click", () => openPlayer(recordingId, row.program_title));
  } catch (error) { $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

function openPlayer(recordingId, title) {
  openModal(`播放 · ${title || "录像"}`, `<div class="video-shell">
    <video id="recording-player" controls preload="metadata" playsinline></video>
    <div class="video-preparing" id="video-preparing"><span class="spinner"></span><strong>正在准备浏览器视频</strong><small>首次播放需要无损转封装，通常等待数秒。</small></div>
  </div><div class="playback-toolbar"><button class="button small secondary" id="seek-back" type="button">−10 秒</button><label class="speed-control"><span>播放速度</span><select class="select" id="playback-rate"><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label><button class="button small secondary" id="seek-forward" type="button">+10 秒</button><span class="playback-time" id="playback-time">00:00 / --:--</span></div><div class="modal-actions"><a class="button secondary" href="/api/recordings/${encodeURIComponent(recordingId)}/download">下载原始录像</a><button class="button secondary" data-close-modal type="button">关闭</button></div>`, true);
  const video = $("#recording-player");
  const preparing = $("#video-preparing");
  video.addEventListener("loadedmetadata", () => { preparing.hidden = true; });
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
  video.src = `/api/recordings/${encodeURIComponent(recordingId)}/play`;
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
  const connect = (automatic = false) => {
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
    if (window.Hls?.isSupported()) {
      const hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        maxBufferLength: 60,
        maxMaxBufferLength: 120,
        backBufferLength: 60,
        liveSyncDurationCount: 10,
        liveMaxLatencyDurationCount: 20,
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
      const candidates = data.candidates || [];
      if (!candidates.length) {
        badge.textContent = "单线路";
        select.innerHTML = '<option value="auto" selected>自动（系统优选）</option>';
        picker.hidden = false;
        return;
      }
      const current = data.current_index;
      select.innerHTML = '<option value="auto">自动（系统优选）</option>' + candidates.map(item => {
        const label = `线路 ${item.index + 1} · ${esc(item.host)}${streamQualityLabel(item.quality)}`;
        const selected = !data.manual && item.index === current;
        return `<option value="${item.index}" ${selected ? "selected" : ""}>${label}</option>`;
      }).join("");
      select.disabled = false;
      badge.textContent = data.manual ? "手动" : "自动";
      picker.hidden = false;
      select.addEventListener("change", async () => {
        const value = select.value;
        const index = value === "auto" ? null : Number(value);
        select.disabled = true;
        try {
          await api(`/api/channels/${encodeURIComponent(channelId)}/stream`, { method: "POST", body: { index } });
          badge.textContent = index === null ? "自动" : "手动";
          toast(index === null ? "已切回自动优选，正在重连" : "已切换线路，正在重连");
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
    ${pageIntro("管理频道名称、分类、排序与启用状态。改名/改分类写入 override 字段，不会被次日 EPG 同步覆盖。", '<button class="button" id="admin-batch-enable" type="button">批量启用</button> <button class="button secondary" id="admin-batch-disable" type="button">批量禁用</button>')}
    <div class="toolbar"><label class="field"><span>分类筛选</span><select class="select" id="admin-category-filter"><option value="">全部分类</option>${categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></label><div class="grow"></div><span class="badge" id="admin-count">${channels.length} 个频道</span></div>
    <section class="panel"><div class="data-table-wrap"><table class="data-table"><thead><tr><th><input type="checkbox" id="admin-check-all" aria-label="全选"></th><th>名称</th><th>分类</th><th>线路</th><th>排序权重</th><th>状态</th><th></th></tr></thead><tbody id="admin-body"></tbody></table></div></section>`;
  const renderRows = () => {
    const filter = $("#admin-category-filter").value;
    const rows = channels.filter(row => !filter || row.category === filter);
    $("#admin-count").textContent = `${rows.length} 个频道`;
    $("#admin-body").innerHTML = rows.length ? rows.map(row => {
      const disabled = row.disabled === true;
      return `<tr>
        <td><input type="checkbox" class="admin-check" data-channel-id="${esc(row.channel_id)}"></td>
        <td><strong>${esc(row.name)}</strong><small>${esc(row.channel_id)}${row.name_override ? " · 已改名" : ""}${row.category_override ? " · 已改分类" : ""}</small></td>
        <td>${esc(row.category)}</td>
        <td>${row.stream_count || 0}</td>
        <td>${row.sort_weight ?? "—"}</td>
        <td>${disabled ? '<span class="badge">已禁用</span>' : '<span class="badge success">启用</span>'}</td>
        <td><div class="table-actions"><button class="button small secondary" data-admin-edit="${esc(row.channel_id)}">编辑</button><button class="button small ghost" data-admin-detail="${esc(row.channel_id)}">详情</button><button class="button small ${disabled ? "secondary" : "danger"}" data-admin-toggle="${esc(row.channel_id)}">${disabled ? "启用" : "禁用"}</button></div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7">${emptyState("没有频道", "当前筛选条件下没有频道。", true)}</td></tr>`;
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
  $("#admin-body").addEventListener("click", async event => {
    const edit = event.target.closest("[data-admin-edit]");
    const detail = event.target.closest("[data-admin-detail]");
    const toggle = event.target.closest("[data-admin-toggle]");
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
    const streams = detail.streams?.candidates || [];
    $("#modal-body").innerHTML = `<div class="detail-stack">
      <dl class="definition-grid">
        <div><dt>频道</dt><dd>${esc(row.name)}</dd></div>
        <div><dt>分类</dt><dd>${esc(row.category)}</dd></div>
        <div><dt>EPG 匹配</dt><dd>${detail.epg?.matched ? `<span class="badge success">已匹配 · ${detail.epg.program_count} 条节目</span>` : '<span class="badge">未匹配</span>'}</dd></div>
        <div><dt>线路数</dt><dd>${streams.length} 条</dd></div>
      </dl>
      <section class="detail-section"><h3>直播线路</h3>${streams.length ? `<table class="data-table"><thead><tr><th>#</th><th>线路域名</th><th>状态</th></tr></thead><tbody>${streams.map((s, i) => `<tr><td>${i}</td><td>${esc(s.host)}</td><td>${s.current ? '<span class="badge success">当前使用</span>' : '<span class="badge">候选</span>'}${streamQualityLabel(s.quality)}</td></tr>`).join("")}</tbody></table>` : "<p>暂无线路</p>"}</section>
      <section class="detail-section"><h3>近 10 条录制</h3>${detail.recent_recordings?.length ? detail.recent_recordings.map(r => `<p class="meta-row"><span class="badge ${r.status === "completed" || r.status === "summarized" ? "success" : ""}">${esc(r.status)}</span><span>${esc(r.program_title || "—")}</span><span>${esc(formatDateTime(r.start_time))}</span></p>`).join("") : "<p>暂无录制</p>"}</section>
    </div>`;
  } catch (error) { $("#modal-body").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }
}

async function renderSources() {
  const [data, channels] = await Promise.all([api("/api/sources"), loadChannels()]);
  const summary = data.m3u_summary || {};
  $("#view").innerHTML = `
    ${pageIntro("管理直播线路。内置源来自 iptv-api 每日同步，自定义源独立存储不会被同步覆盖。", '<button class="button" id="source-new" type="button">＋ 新增自定义源</button>')}
    <div class="toolbar">
      <label class="field"><span>频道筛选</span><select class="select" id="source-channel-filter"><option value="">全部频道</option>${channels.map(c => `<option value="${esc(c.channel_id)}">${esc(c.name || c.channel_id)}</option>`).join("")}</select></label>
      <label class="field"><span>来源类型</span><select class="select" id="source-type-filter"><option value="">全部</option><option value="seed">内置</option><option value="custom">自定义</option></select></label>
      <div class="grow"></div><span class="badge" id="source-count">${data.sources.length} 条线路</span>
    </div>
    <div class="meta-row" style="margin:0 0 12px"><span>M3U 总源：${esc(summary.source || "—")} · 最后同步 ${esc(formatDateTime(summary.last_sync_at))} · 内置线路 ${summary.seed_stream_count ?? 0} 条</span></div>
    <section class="panel"><div class="data-table-wrap"><table class="data-table"><thead><tr><th>频道</th><th>线路地址</th><th>来源</th><th>备注</th><th>状态</th><th></th></tr></thead><tbody id="source-body"></tbody></table></div></section>`;
  const renderRows = () => {
    const channelFilter = $("#source-channel-filter").value;
    const typeFilter = $("#source-type-filter").value;
    const rows = data.sources.filter(row => (!channelFilter || row.channel_id === channelFilter) && (!typeFilter || row.source_type === typeFilter));
    $("#source-count").textContent = `${rows.length} 条线路`;
    $("#source-body").innerHTML = rows.length ? rows.map(row => {
      const custom = row.source_type === "custom";
      const host = (() => { try { return new URL(row.url).host; } catch { return row.url; } })();
      return `<tr>
        <td><strong>${esc(row.channel_name)}</strong><small>${esc(row.channel_id)}</small></td>
        <td><span title="${esc(row.url)}">${esc(host)}</span>${row.current ? ' <span class="badge success">当前</span>' : ""}</td>
        <td>${custom ? '<span class="badge warning">自定义</span>' : '<span class="badge info">内置</span>'}</td>
        <td>${esc(row.note || "—")}</td>
        <td>${row.enabled === false ? '<span class="badge">已禁用</span>' : '<span class="badge success">启用</span>'}</td>
        <td><div class="table-actions"><button class="button small ghost" data-source-test="${esc(row.url)}" data-source-name="${esc(row.channel_id)}">测试</button>${custom ? `<button class="button small secondary" data-source-edit="${esc(row._id)}">编辑</button><button class="button small danger" data-source-delete="${esc(row._id)}">删除</button>` : ""}</div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="6">${emptyState("暂无线路", "当前筛选条件下没有线路。", true)}</td></tr>`;
  };
  renderRows();
  $("#source-channel-filter").addEventListener("change", renderRows);
  $("#source-type-filter").addEventListener("change", renderRows);
  $("#source-new").addEventListener("click", () => openSourceForm(channels));
  $("#source-body").addEventListener("click", async event => {
    const test = event.target.closest("[data-source-test]");
    const edit = event.target.closest("[data-source-edit]");
    const del = event.target.closest("[data-source-delete]");
    if (test) return testSourceUrl(test.dataset.sourceTest, test.dataset.sourceName);
    if (edit) return openSourceForm(channels, data.sources.find(row => row._id === edit.dataset.sourceEdit));
    if (del) {
      const row = data.sources.find(item => item._id === del.dataset.sourceDelete);
      if (!confirm(`确定删除自定义源「${row?.channel_id} · ${row?.url}」？`)) return;
      try {
        await api(`/api/sources/${encodeURIComponent(row._id)}`, { method: "DELETE" });
        toast("自定义源已删除"); renderRoute();
      } catch (error) { toast(error.message, "error"); }
    }
  });
}

function openSourceForm(channels, existing = null) {
  openModal(existing ? "编辑自定义源" : "新增自定义源", `<form id="source-form" class="form-grid">
    <label class="field wide"><span>频道</span><select class="select" name="channel_id" ${existing ? "disabled" : "required"}>${channelOptions(channels, existing?.channel_id || "", false)}</select>${existing ? '<small>自定义源归属频道不可修改</small>' : ""}</label>
    <label class="field wide"><span>线路 URL</span><input class="input" name="url" required value="${esc(existing?.url || "")}" placeholder="http://... 或 https://..."></label>
    <label class="field wide"><span>备注</span><input class="input" name="note" maxlength="200" value="${esc(existing?.note || "")}" placeholder="可选，例如：备用线路、1080p"></label>
    <label class="field"><span>状态</span><select class="select" name="enabled"><option value="true" ${existing?.enabled !== false ? "selected" : ""}>启用</option><option value="false" ${existing?.enabled === false ? "selected" : ""}>禁用</option></select></label>
    <div class="modal-actions wide"><button class="button secondary" data-close-modal type="button">取消</button><button class="button" type="submit">${existing ? "保存" : "添加"}</button></div>
  </form>`);
  $("#source-form").addEventListener("submit", async event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const body = { url: data.get("url").trim(), note: data.get("note").trim(), enabled: data.get("enabled") === "true" };
    if (!existing) body.channel_id = data.get("channel_id");
    try {
      await api(existing ? `/api/sources/${encodeURIComponent(existing._id)}` : "/api/sources", { method: existing ? "PATCH" : "POST", body });
      toast(existing ? "自定义源已更新" : "自定义源已添加"); closeModal(); renderRoute();
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
  const [status, events, health] = await Promise.all([api("/api/system/status"), api("/api/push/events?limit=100"), api("/health")]);
  const recordingTotal = Object.values(status.recordings_by_status || {}).reduce((sum, value) => sum + value, 0);
  $("#view").innerHTML = `
    ${pageIntro("查看业务统计和邮件推送审计。容器与磁盘明细未通过管理 API 暴露，避免把宿主权限交给网页。")}
    <div class="stats-grid"><article class="stat-card"><small>可播频道</small><strong>${esc(status.channels)}</strong></article><article class="stat-card"><small>EPG 节目</small><strong>${esc(status.epg_programs)}</strong></article><article class="stat-card"><small>启用订阅</small><strong>${esc(status.subscriptions)}</strong></article><article class="stat-card"><small>录制记录</small><strong>${esc(recordingTotal)}</strong></article></div>
    <div class="split-layout system-layout">
      <section class="panel"><div class="panel-header"><h2>推送事件</h2><span class="badge">${events.length} 条</span></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>通道</th><th>状态</th><th>更新时间</th></tr></thead><tbody>${events.map(row => `<tr><td>${esc(row.provider)}</td><td><span class="badge ${row.status === "delivered" ? "success" : row.status === "failed" ? "danger" : ""}">${esc(row.status)}</span></td><td>${esc(formatDateTime(row.updated_at))}</td></tr>`).join("") || '<tr><td colspan="3">暂无事件</td></tr>'}</tbody></table></div></section>
      <div class="detail-stack"><section class="panel"><div class="panel-header"><h2>服务状态</h2></div><div class="panel-body"><p><span class="badge success">API 正常</span></p><p class="meta-row">检查时间：${esc(formatDateTime(health.time))}</p><p class="meta-row">最新摘要：${esc(formatDateTime(status.latest_summary_at))}</p></div></section>
      <section class="panel"><div class="panel-header"><h2>API 访问令牌</h2></div><div class="panel-body"><p class="meta-row">仅在 NAS 开启 WEB_API_TOKEN 后填写，保存在本次浏览器会话中。</p><label class="field"><span>Bearer Token</span><input class="input" id="api-token" type="password" autocomplete="off" value="${esc(sessionStorage.getItem("iptv_api_token") || "")}"></label><div class="button-row" style="margin-top:12px"><button class="button small" id="save-token">保存</button><button class="button small secondary" id="clear-token">清除</button></div></div></section></div>
    </div>`;
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
window.addEventListener("hashchange", renderRoute);
if (!location.hash) location.hash = "#/epg";
renderRoute();
checkHealth();
setInterval(checkHealth, 60000);
