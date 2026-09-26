"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = {
  summary: null,
  settings: null,
  jobs: {},
  linksPage: 1,
  storiesPage: 1,
  selectedLinks: new Set(),
  selectedStories: new Set(),
  selectedJob: "auto",
  readerStory: null,
  readerTab: "formatted",
};

const fmt = value => Number(value || 0).toLocaleString();

function statusPill(status) {
  const cls = status === "verified" || status === "scraped" ? "ok" : status === "review" || status === "pending" ? "warn" : status === "failed" ? "bad" : "";
  const label = { verified: "Verified", review: "Review", failed: "Failed", scraped: "Scraped", pending: "Pending" }[status] || status || "—";
  return `<span class="pill ${cls}">${label}</span>`;
}

function toast(message, type = "") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), 3600);
}

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok || data.ok === false) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function debounce(fn, ms = 300) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function routePage() {
  const path = location.pathname.replace(/^\/+|\/+$/g, "") || "dashboard";
  if (path === "activity") return "dashboard";
  if (path === "review") return "stories";
  return ["dashboard", "sources", "stories", "settings", "story"].includes(path) ? path : "dashboard";
}

function navigate(href) {
  history.pushState({}, "", href);
  renderRoute();
}

async function renderRoute() {
  const page = routePage();
  $$(".page").forEach(el => el.classList.toggle("active", el.dataset.page === page));
  $$('[data-nav]').forEach(el => el.classList.toggle("active", el.dataset.nav === page));
  $("#topbar-title").textContent = { dashboard: "Dashboard", sources: "Sources", stories: "Library", settings: "Settings", story: "Story" }[page];
  $("#sidebar").classList.remove("open");
  try {
    if (page === "dashboard") await loadDashboard();
    if (page === "sources") await loadSources();
    if (page === "stories") {
      const requestedStatus = new URLSearchParams(location.search).get("status");
      if (["all", "verified", "review", "failed"].includes(requestedStatus)) $("#story-status").value = requestedStatus;
      await loadStories(1);
      await loadSeries();
    }
    if (page === "settings") { await loadSettings(); await loadMaintenance(); }
    if (page === "story") await loadReader();
  } catch (error) { toast(error.message, "error"); }
}

document.addEventListener("click", event => {
  const anchor = event.target.closest("a[href]");
  if (!anchor || anchor.target || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const url = new URL(anchor.href, location.href);
  if (url.origin !== location.origin) return;
  event.preventDefault();
  navigate(url.pathname + url.search);
});
window.addEventListener("popstate", renderRoute);

async function loadSummary() {
  state.summary = await api("/api/summary");
  state.settings = state.summary.settings;
  return state.summary;
}

function folderLabel(item) {
  if (item.part_number != null) return `📁 ${item.series_title || item.title} / Part ${item.part_number}`;
  return item.category && item.category !== "Uncategorized" ? `📁 ${item.category}` : "—";
}

const CATEGORY_SOURCE_LABEL = { title: "matched title", content: "matched story content", manual: "set manually", none: "no match" };

function renderRows(tbody, items, compact = false, selectable = false) {
  tbody.replaceChildren();
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">No stories found.</div></td></tr>`;
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const selectCell = selectable
      ? `<td class="select-cell mobile-meta" data-label="Select"><input class="story-check" type="checkbox" aria-label="Select story" ${state.selectedStories.has(item.url) ? "checked" : ""}></td>`
      : "";
    if (compact) {
      row.innerHTML = `${selectCell}<td class="story-cell"><strong></strong><small></small></td><td class="folder-cell mobile-meta" data-label="Folder"></td><td class="status-cell mobile-meta" data-label="Status">${statusPill(item.status)}</td><td class="row-actions"><button class="btn small open-story" type="button">Read</button></td>`;
      $("strong", row).textContent = item.title || "Untitled";
      $("small", row).textContent = item.source_host || item.url;
      $(".folder-cell", row).textContent = folderLabel(item);
    } else {
      row.innerHTML = `${selectCell}<td class="story-cell"><strong></strong><small></small></td><td class="folder-cell mobile-meta" data-label="Folder"></td><td class="category-cell mobile-meta" data-label="Category"></td><td class="words-cell mobile-meta" data-label="Words">${fmt(item.words)}</td><td class="status-cell mobile-meta" data-label="Status">${statusPill(item.status)}</td><td class="row-actions"><button class="btn small open-story" type="button">Read</button></td>`;
      $("strong", row).textContent = item.title || "Untitled";
      $("small", row).textContent = item.status === "review" && item.review_reason ? item.review_reason : item.url;
      $(".folder-cell", row).textContent = item.part_number != null ? `📁 ${item.series_title} / Part ${item.part_number}` : "Single story";
      const categoryCell = $(".category-cell", row);
      categoryCell.textContent = item.category || "Uncategorized";
      const reason = CATEGORY_SOURCE_LABEL[item.category_source] || "";
      if (reason) categoryCell.title = `Category ${reason}`;
    }
    if (selectable) {
      $(".story-check", row).addEventListener("change", event => {
        if (event.target.checked) state.selectedStories.add(item.url); else state.selectedStories.delete(item.url);
        updateStorySelection();
      });
    }
    $(".open-story", row).addEventListener("click", () => navigate(`/story?url=${encodeURIComponent(item.url)}`));
    tbody.append(row);
  }
}

async function loadDashboard() {
  const data = await loadSummary();
  $("#metric-links").textContent = fmt(data.links);
  $("#metric-pending").textContent = `${fmt(data.pending_links)} pending`;
  $("#metric-stories").textContent = fmt(data.stories.total);
  $("#metric-verified").textContent = `${fmt(data.stories.verified)} verified`;
  $("#metric-review").textContent = fmt(data.stories.review);
  $("#metric-failed").textContent = `${fmt(data.stories.failed)} failed`;

  const sources = $("#source-summary");
  sources.replaceChildren();
  if (!data.sources.length) sources.innerHTML = '<div class="empty-state">No sources yet. Add a website to begin.</div>';
  for (const source of data.sources.slice(0, 15)) {
    const percent = source.links ? Math.round((source.scraped / source.links) * 100) : 0;
    const row = document.createElement("div");
    row.className = "source-row";
    row.innerHTML = `<div class="source-row-top"><strong></strong><small>${percent}%</small></div><div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div><div class="source-row-meta"><span>${fmt(source.scraped)} scraped</span><span>${fmt(source.pending)} pending</span><span>${fmt(source.failed)} failed</span></div>`;
    $("strong", row).textContent = source.source;
    sources.append(row);
  }
  renderRows($("#recent-stories"), data.recent, true);
  renderJobs();
}

function selectedScrapeMode() {
  return $('input[name="scrape_mode_run"]:checked')?.value || state.settings?.scrape_mode || "manual";
}

function applyScrapeMode() {
  const auto = selectedScrapeMode() === "auto";
  document.body.classList.toggle("auto-mode", auto);
  $("#manual-actions").hidden = auto;
  $("#start-crawl").textContent = auto ? "Start auto crawl" : "Find links";
}

async function loadSources() {
  const data = await loadSummary();
  $("#sites-input").value = data.sites.join("\n");
  $("#crawl-depth").value = data.settings.crawler_depth;
  $("#crawl-pages").value = data.settings.crawler_max_pages;
  $("#crawl-delay").value = data.settings.crawler_delay;
  const radio = $(`input[name="scrape_mode_run"][value="${data.settings.scrape_mode || "manual"}"]`);
  if (radio) radio.checked = true;
  applyScrapeMode();
  await loadLinks(1);
}

function linkQuery(page) {
  const q = encodeURIComponent($("#links-search").value || "");
  const status = encodeURIComponent($("#links-status").value || "all");
  return `/api/links?page=${page}&per_page=50&q=${q}&status=${status}`;
}

async function loadLinks(page = 1) {
  state.linksPage = page;
  const data = await api(linkQuery(page));
  $("#links-total").textContent = fmt(data.total);
  const tbody = $("#links-table");
  tbody.replaceChildren();
  if (!data.items.length) tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state">No links match this view.</div></td></tr>';
  for (const item of data.items) {
    const row = document.createElement("tr");
    const checked = state.selectedLinks.has(item.url) ? "checked" : "";
    row.innerHTML = `<td class="manual-cell mobile-meta" data-label="Select"><input class="link-check" type="checkbox" aria-label="Select story link" ${checked}></td><td class="story-cell"><strong></strong><small></small></td><td class="status-cell mobile-meta" data-label="Status">${statusPill(item.status)}</td><td class="row-actions"><a class="btn small original-link" target="_blank" rel="noreferrer">Open</a></td>`;
    $("strong", row).textContent = item.title || item.url;
    $("small", row).textContent = item.url;
    $(".original-link", row).href = item.url;
    $(".link-check", row).addEventListener("change", event => {
      if (event.target.checked) state.selectedLinks.add(item.url); else state.selectedLinks.delete(item.url);
      updateSelection();
    });
    tbody.append(row);
  }
  renderPagination($("#links-pagination"), data, loadLinks);
  $("#links-select-all").checked = false;
  updateSelection();
}

function updateSelection() { $("#selection-count").textContent = `${fmt(state.selectedLinks.size)} selected`; }

async function startContent(payload = {}) {
  const result = await api("/api/start/content", { method: "POST", body: JSON.stringify(payload) });
  state.selectedLinks.clear();
  updateSelection();
  toast(result.message, "success");
  navigate("/dashboard");
  $("#engine-panel").open = true;
}

function populateCategoryFilter() {
  for (const select of [$("#story-category"), $("#bulk-category-select")]) {
    const isFilter = select.id === "story-category";
    const current = select.value || (isFilter ? "all" : "");
    select.innerHTML = isFilter ? '<option value="all">All categories</option>' : '<option value="">Move to category…</option>';
    for (const name of state.settings?.categories || []) {
      const option = document.createElement("option"); option.value = name; option.textContent = name; select.append(option);
    }
    if (isFilter) {
      const uncategorized = document.createElement("option"); uncategorized.value = "Uncategorized"; uncategorized.textContent = "Uncategorized"; select.append(uncategorized);
    }
    if ([...select.options].some(o => o.value === current)) select.value = current;
  }
}

function updateStorySelection() { $("#stories-selection-count").textContent = `${fmt(state.selectedStories.size)} selected`; }

async function loadStories(page = 1) {
  if (!state.settings) await loadSummary();
  populateCategoryFilter();
  state.storiesPage = page;
  const q = encodeURIComponent($("#story-search").value || "");
  const category = encodeURIComponent($("#story-category").value || "all");
  const status = $("#story-status").value || "all";
  const sort = $("#story-sort").value || "newest";
  const per = state.settings?.stories_per_page || 50;
  const data = await api(`/api/stories?page=${page}&per_page=${per}&q=${q}&category=${category}&status=${status}&sort=${sort}`);
  renderRows($("#stories-table"), data.items, false, true);
  $("#stories-select-all").checked = false;
  updateStorySelection();
  renderPagination($("#stories-pagination"), data, loadStories);
}

function renderPagination(container, data, callback) {
  container.replaceChildren();
  if (data.pages <= 1) return;
  const add = (label, target, disabled = false, active = false) => {
    const button = document.createElement("button"); button.textContent = label; button.disabled = disabled; button.classList.toggle("active", active); button.addEventListener("click", () => callback(target)); container.append(button);
  };
  add("←", data.page - 1, data.page <= 1);
  const start = Math.max(1, data.page - 2), end = Math.min(data.pages, data.page + 2);
  for (let page = start; page <= end; page++) add(String(page), page, false, page === data.page);
  add("→", data.page + 1, data.page >= data.pages);
}

function jobLabel(name) { return { auto: "Auto crawl + scrape", links: "Crawler", content: "Scraper", format: "Formatter" }[name] || name; }
function isActive(job) { return job && ["running", "stopping"].includes(job.state); }

// v3.4 P1 "adaptive polling": don't hammer /api/status and /api/summary on a
// fixed timer regardless of what's happening. Job polling runs fast only
// while an engine is actually active, backs off when idle, and stops
// completely while the tab is hidden. The summary refresh is driven by the
// same loop: it fires immediately the moment a job finishes (so the
// Dashboard updates right away instead of waiting for the next tick) and
// otherwise refreshes on a slow, visibility-aware cadence.
let wasJobActive = false;

async function pollJobs() {
  try {
    state.jobs = await api("/api/status/all");
    const active = Object.values(state.jobs).find(isActive);
    $("#job-chip").classList.toggle("running", Boolean(active));
    $("#job-chip-text").textContent = active ? `${jobLabel(active.name)} · ${active.state}` : "Idle";
    $("#engine-summary").textContent = active ? `${jobLabel(active.name)} running` : "Idle";
    if (routePage() === "dashboard") renderJobs();
    const isActiveNow = Boolean(active);
    if (wasJobActive && !isActiveNow && ["dashboard", "sources", "stories"].includes(routePage())) {
      // A job just finished: refresh the summary right away rather than
      // waiting up to 30s for the next slow-cadence tick.
      try { await loadSummary(); } catch {}
    }
    wasJobActive = isActiveNow;
    return isActiveNow;
  } catch {
    return wasJobActive;
  }
}

function renderJobs() {
  const cards = $("#job-cards");
  if (!cards) return;
  cards.replaceChildren();
  for (const name of ["auto", "links", "content", "format"]) {
    const job = state.jobs[name] || { name, state: "idle", logs: [] };
    const card = document.createElement("div");
    card.className = `job-card ${state.selectedJob === name ? "active" : ""}`;
    card.innerHTML = `<strong>${jobLabel(name)}</strong><small>${job.state || "idle"}</small><button class="btn small view-job" type="button">Log</button>${isActive(job) ? '<button class="btn small danger stop-job" type="button">Stop</button>' : ""}`;
    $(".view-job", card).addEventListener("click", () => { state.selectedJob = name; renderJobs(); });
    $(".stop-job", card)?.addEventListener("click", () => stopJob(name));
    cards.append(card);
  }
  const selected = state.jobs[state.selectedJob] || { state: "idle", logs: [] };
  $("#log-subtitle").textContent = `${jobLabel(state.selectedJob)} · ${selected.state}`;
  $("#job-log").textContent = selected.logs?.length ? selected.logs.join("\n") : "No job has run yet.";
}

async function stopJob(name) {
  try {
    const result = await api(`/api/stop/${name}`, { method: "POST", body: "{}" });
    toast(result.message);
  } catch (error) { toast(error.message, "error"); }
}

function aliasesToText(value) {
  if (!value || typeof value !== "object") return "";
  return Object.entries(value).map(([name, aliases]) => `${name} = ${(aliases || []).join(", ")}`).join("\n");
}

function aliasesFromText(value) {
  const out = {};
  for (const raw of String(value || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    if (at < 1) continue;
    const name = line.slice(0, at).trim();
    const aliases = line.slice(at + 1).split(/[,|]/).map(v => v.trim()).filter(Boolean);
    if (name && aliases.length) out[name] = aliases;
  }
  return out;
}

// v3.7: per-site crawl-delay overrides, edited the same "key = value" way as category aliases.
function siteProfilesToText(value) {
  if (!value || typeof value !== "object") return "";
  return Object.entries(value).map(([host, seconds]) => `${host} = ${seconds}`).join("\n");
}

function siteProfilesFromText(value) {
  const out = {};
  for (const raw of String(value || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    if (at < 1) continue;
    const host = line.slice(0, at).trim().toLowerCase();
    const seconds = Number(line.slice(at + 1).trim());
    if (host && Number.isFinite(seconds) && seconds >= 0) out[host] = seconds;
  }
  return out;
}

async function loadSettings() {
  const settings = await api("/api/settings");
  state.settings = settings;
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (key === "categories") input.value = (value || []).join("\n");
    else if (key === "category_aliases") input.value = aliasesToText(value);
    else if (key === "site_profiles") input.value = siteProfilesToText(value);
    else if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
}

// v3.6 maintenance panel
function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = n, index = -1;
  do { value /= 1024; index++; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(1)} ${units[index]}`;
}

async function loadMaintenance() {
  await Promise.all([loadMaintenanceStorage(), loadBackups(), loadJobsHistory()]);
}

async function loadMaintenanceStorage() {
  const health = await api("/api/maintenance/storage");
  $("#maintenance-storage").textContent =
    `${fmt(health.story_count)} stories · ${fmt(health.link_count)} links · database ${formatBytes(health.database_bytes)} · ` +
    `raw ${formatBytes(health.raw_pages_bytes)} · formatted ${formatBytes(health.formatted_pages_bytes)} · library ${formatBytes(health.library_bytes)}`;
}

function renderIntegrityIssues(result) {
  const container = $("#maintenance-issues");
  container.replaceChildren();
  if (!result.issue_count) { container.innerHTML = '<div class="empty-state">No integrity issues found.</div>'; return; }
  const summary = document.createElement("div");
  summary.className = "muted";
  summary.textContent = `${fmt(result.issue_count)} issue(s) found (checked ${new Date(result.checked_at).toLocaleString()})`;
  container.append(summary);
  for (const issue of result.issues.slice(0, 200)) {
    const row = document.createElement("div");
    row.className = "series-row";
    row.innerHTML = `<div class="series-row-top"><strong></strong><span class="muted"></span></div>`;
    $("strong", row).textContent = issue.type.replace(/_/g, " ");
    $(".muted", row).textContent = issue.url ? `${issue.url} — ${issue.detail}` : issue.detail;
    container.append(row);
  }
}

async function loadBackups() {
  const data = await api("/api/maintenance/backups");
  const container = $("#backups-list");
  container.replaceChildren();
  $("#backups-count").textContent = data.backups.length ? `(${fmt(data.backups.length)})` : "";
  if (!data.backups.length) { container.innerHTML = '<div class="empty-state">No backups yet.</div>'; return; }
  for (const backup of data.backups) {
    const row = document.createElement("div");
    row.className = "series-row";
    row.innerHTML = `<div class="series-row-top"><strong></strong><span class="muted"></span></div>`;
    $("strong", row).textContent = `${backup.filename} — ${formatBytes(backup.size_bytes)}`;
    $(".muted", row).textContent = new Date(backup.created_at).toLocaleString() + (backup.note ? ` · ${backup.note}` : "");
    const restoreButton = document.createElement("button");
    restoreButton.className = "btn small";
    restoreButton.type = "button";
    restoreButton.textContent = "Restore";
    restoreButton.addEventListener("click", () => restoreBackup(backup.filename));
    $(".series-row-top", row).append(restoreButton);
    container.append(row);
  }
}

async function restoreBackup(filename) {
  if (!confirm(`Restore "${filename}"? The current database will be moved aside as a .before-restore file first. Stop all running engines before continuing.`)) return;
  try {
    const result = await api("/api/maintenance/restore", { method: "POST", body: JSON.stringify({ filename }) });
    toast(result.message, "success");
    await loadMaintenance();
  } catch (error) { toast(error.message, "error"); }
}

async function loadJobsHistory() {
  const data = await api("/api/jobs/history");
  const container = $("#jobs-history-list");
  container.replaceChildren();
  $("#jobs-history-count").textContent = data.runs.length ? `(${fmt(data.runs.length)})` : "";
  if (!data.runs.length) { container.innerHTML = '<div class="empty-state">No job runs recorded yet.</div>'; return; }
  for (const run of data.runs.slice(0, 100)) {
    const row = document.createElement("div");
    row.className = "series-row";
    const pillClass = run.state === "complete" ? "ok" : run.state === "failed" ? "bad" : run.state === "running" ? "warn" : "";
    row.innerHTML = `<div class="series-row-top"><strong></strong><span class="pill ${pillClass}"></span></div><div class="muted"></div>`;
    $("strong", row).textContent = `${run.name} — started ${new Date(run.started_at).toLocaleString()}`;
    $(".pill", row).textContent = run.state;
    $(".muted", row).textContent = run.error || (run.finished_at ? `finished ${new Date(run.finished_at).toLocaleString()}` : "");
    container.append(row);
  }
}

async function loadReader() {
  const url = new URLSearchParams(location.search).get("url");
  if (!url) throw new Error("No story selected.");
  const data = await api(`/api/story?url=${encodeURIComponent(url)}`);
  const story = data.story;
  state.readerStory = story;
  $("#reader-title").textContent = story.title || "Untitled";
  const folder = story.part_number != null ? `${story.category || "Uncategorized"} / ${story.series_title} / Part ${story.part_number}` : `${story.category || "Uncategorized"} / ${story.title}`;
  $("#reader-folder").textContent = folder;
  const qualityBits = [
    `${fmt(story.words)} words`,
    `${fmt(story.paragraphs)} paragraphs`,
    story.dialogue_turns ? `${fmt(story.dialogue_turns)} dialogue turns` : null,
    story.integrity_exact ? "✓ source text exact" : "text check failed",
    story.quality_pass ? "✓ readability checked" : story.manual_accept ? "manual accept" : "review needed",
    story.romanized ? "romanized" : "original script",
    CATEGORY_SOURCE_LABEL[story.category_source] ? `category ${CATEGORY_SOURCE_LABEL[story.category_source]}` : null,
  ].filter(Boolean);
  $("#reader-meta").replaceChildren(...qualityBits.map(value => { const span = document.createElement("span"); span.textContent = value; return span; }));
  $("#open-original").href = story.url;
  state.readerTab = story.romanized_text ? "romanized" : story.formatted_text ? "formatted" : "original";
  renderReader();

  const review = $("#review-tools");
  const needsReview = story.status === "review";
  review.hidden = !needsReview;
  if (needsReview) {
    $("#review-reason").textContent = story.review_reason || "Check readability";
    $("#review-original").value = story.original_text || "";
    $("#review-formatted").value = story.formatted_text || story.original_text || "";
  }
}

function renderReader() {
  const story = state.readerStory;
  if (!story) return;
  $$('[data-reader-tab]').forEach(button => button.classList.toggle("active", button.dataset.readerTab === state.readerTab));
  const text = state.readerTab === "romanized" ? (story.romanized_text || story.formatted_text || story.original_text) : state.readerTab === "formatted" ? (story.formatted_text || story.original_text) : story.original_text;
  const reader = $("#story-reader"); reader.replaceChildren();
  const paragraphs = String(text || "").split(/\n\s*\n|\n/).map(v => v.trim()).filter(Boolean);
  if (!paragraphs.length) { reader.innerHTML = '<div class="empty-state">No readable text saved.</div>'; return; }
  for (const value of paragraphs) {
    const p = document.createElement("p");
    // Formatting is decided by the verified formatter engine. Do not infer a
    // speaker again in the browser: arbitrary prose such as `... 11:30 ...`
    // contains colons and must never be restyled as dialogue.
    p.textContent = value;
    reader.append(p);
  }
}

async function reviewAction(action) {
  const story = state.readerStory;
  if (!story) return;
  try {
    let result = { message: "Story updated." };
    if (action === "save") result = await api("/api/story/save-format", { method: "POST", body: JSON.stringify({ url: story.url, text: $("#review-formatted").value }) });
    if (action === "accept") result = await api("/api/story/accept", { method: "POST", body: JSON.stringify({ url: story.url }) });
    if (action === "original") result = await api("/api/story/use-original", { method: "POST", body: JSON.stringify({ url: story.url }) });
    if (action === "reformat") result = await api("/api/story/reformat", { method: "POST", body: JSON.stringify({ url: story.url }) });
    toast(result.message || "Story updated.", "success");
    await loadReader();
  } catch (error) { toast(error.message, "error"); }
}

// Sources
$("#crawl-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const result = await api("/api/start/links", {
      method: "POST",
      body: JSON.stringify({
        sites: $("#sites-input").value,
        depth: $("#crawl-depth").value,
        max_pages: $("#crawl-pages").value,
        delay: $("#crawl-delay").value,
        fresh: $("#crawl-fresh").checked,
        scrape_mode: selectedScrapeMode(),
      }),
    });
    $("#crawl-message").textContent = result.message;
    toast(result.message, "success");
    navigate("/dashboard");
    $("#engine-panel").open = true;
  } catch (error) { $("#crawl-message").textContent = error.message; toast(error.message, "error"); }
});
$$('input[name="scrape_mode_run"]').forEach(input => input.addEventListener("change", applyScrapeMode));
$("#stop-acquisition").addEventListener("click", () => {
  const active = ["auto", "links", "content"].find(name => isActive(state.jobs[name]));
  if (active) stopJob(active); else toast("No crawl or scrape job is running.");
});
$("#links-search").addEventListener("input", debounce(() => loadLinks(1)));
$("#links-status").addEventListener("change", () => loadLinks(1));
$("#links-select-all").addEventListener("change", event => $$("#links-table .link-check").forEach(box => { if (box.checked !== event.target.checked) box.click(); }));
$("#scrape-selected").addEventListener("click", () => state.selectedLinks.size ? startContent({ urls: [...state.selectedLinks] }) : toast("Select at least one link.", "error"));
$("#scrape-pending").addEventListener("click", () => startContent({ pending_only: true }).catch(error => toast(error.message, "error")));

// Library
$("#story-search").addEventListener("input", debounce(() => loadStories(1)));
for (const id of ["story-category", "story-status", "story-sort"]) $("#" + id).addEventListener("change", () => loadStories(1));
$("#recategorize-library").addEventListener("click", async () => {
  const button = $("#recategorize-library");
  if (!confirm("Re-categorize all existing stories using title first, then story content, including category aliases? This only rebuilds the organized library; raw/formatted stories are not changed.")) return;
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = "Re-categorizing…";
  try {
    const result = await api("/api/library/recategorize", { method: "POST", body: "{}" });
    toast(result.message, "success");
    await loadSummary();
    await loadStories(1);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
});
$("#reromanize-library").addEventListener("click", async () => {
  if (!confirm("Rebuild Romanized text for all existing Telugu stories using the improved Tenglish engine? Raw and formatted stories are not changed.")) return;
  try {
    const result = await api("/api/start/romanize", { method: "POST", body: "{}" });
    toast(result.message, "success");
    navigate("/dashboard");
    $("#engine-panel").open = true;
  } catch (error) { toast(error.message, "error"); }
});

$("#run-formatter").addEventListener("click", async () => {
  try { const result = await api("/api/start/format", { method: "POST", body: JSON.stringify({ force: true }) }); toast(result.message, "success"); navigate("/dashboard"); $("#engine-panel").open = true; }
  catch (error) { toast(error.message, "error"); }
});

// v3.5 bulk category editor
$("#stories-select-all").addEventListener("change", event => $$("#stories-table .story-check").forEach(box => { if (box.checked !== event.target.checked) box.click(); }));
$("#bulk-move").addEventListener("click", async () => {
  const category = $("#bulk-category-select").value;
  if (!state.selectedStories.size) { toast("Select at least one story.", "error"); return; }
  if (!category) { toast("Choose a category first.", "error"); return; }
  try {
    const result = await api("/api/library/bulk", { method: "POST", body: JSON.stringify({ urls: [...state.selectedStories], action: "set_category", category }) });
    toast(result.message, "success");
    state.selectedStories.clear();
    await loadStories(state.storiesPage);
  } catch (error) { toast(error.message, "error"); }
});
$("#bulk-unlock").addEventListener("click", async () => {
  if (!state.selectedStories.size) { toast("Select at least one story.", "error"); return; }
  try {
    const result = await api("/api/library/bulk", { method: "POST", body: JSON.stringify({ urls: [...state.selectedStories], action: "unlock" }) });
    toast(result.message, "success");
    state.selectedStories.clear();
    await loadStories(state.storiesPage);
  } catch (error) { toast(error.message, "error"); }
});

// v3.5 series manager
async function loadSeries() {
  const data = await api("/api/library/series");
  const container = $("#series-list");
  container.replaceChildren();
  $("#series-count").textContent = data.series.length ? `(${fmt(data.series.length)})` : "";
  if (!data.series.length) { container.innerHTML = '<div class="empty-state">No multipart series yet.</div>'; return; }
  for (const series of data.series) {
    const row = document.createElement("div");
    row.className = "series-row";
    const missing = series.missing_parts.length ? `<span class="pill warn">Missing part ${series.missing_parts.join(", ")}</span>` : "";
    row.innerHTML = `<div class="series-row-top"><strong></strong><span class="muted"></span>${missing}</div>`;
    $("strong", row).textContent = series.series_title;
    $(".muted", row).textContent = `${series.category} · ${series.part_count} part(s)`;
    container.append(row);
  }
}

// v3.5 duplicate detection
async function loadDuplicates() {
  const button = $("#scan-duplicates");
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = "Scanning…";
  try {
    const data = await api("/api/library/duplicates");
    const container = $("#duplicates-list");
    container.replaceChildren();
    $("#duplicates-count").textContent = data.group_count ? `(${fmt(data.group_count)})` : "";
    if (data.fuzzy_skipped_buckets) {
      const warning = document.createElement("div");
      warning.className = "notice warning";
      warning.textContent = `Fuzzy title scan skipped ${fmt(data.fuzzy_skipped_stories)} stories across ${fmt(data.fuzzy_skipped_buckets)} oversized word-count bucket(s). Exact/hash checks still ran.`;
      container.append(warning);
    }
    if (!data.group_count) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = data.fuzzy_skipped_buckets ? "No duplicates found in the portions that were checked." : "No likely duplicates found.";
      container.append(empty);
      return;
    }
    for (const group of data.groups) {
      const row = document.createElement("div");
      row.className = "duplicate-group";
      row.innerHTML = `<div class="duplicate-reason"></div>`;
      $(".duplicate-reason", row).textContent = group.detail;
      for (const story of group.stories) {
        const link = document.createElement("a");
        link.href = `/story?url=${encodeURIComponent(story.url)}`;
        link.textContent = `${story.title || story.url} (${story.category || "Uncategorized"})`;
        row.append(link);
      }
      container.append(row);
    }
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}
$("#scan-duplicates").addEventListener("click", () => loadDuplicates().catch(error => toast(error.message, "error")));

// Settings
$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget, payload = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.name === "categories") payload.categories = input.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    else if (input.name === "category_aliases") payload.category_aliases = aliasesFromText(input.value);
    else if (input.name === "site_profiles") payload.site_profiles = siteProfilesFromText(input.value);
    else payload[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try {
    const result = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    state.settings = result.settings;
    $("#settings-message").textContent = result.organization ? `Saved. Reorganized ${fmt(result.organization.stories)} stories.` : "Saved.";
    toast("Settings saved.", "success");
  } catch (error) { $("#settings-message").textContent = error.message; toast(error.message, "error"); }
});

// v3.6 maintenance actions
async function runMaintenanceAction(button, path, { confirmMessage, successMessage, method = "POST", refresh = true } = {}) {
  if (confirmMessage && !confirm(confirmMessage)) return;
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try {
    const result = await api(path, method === "POST" ? { method: "POST", body: JSON.stringify({}) } : {});
    if (path === "/api/maintenance/integrity") renderIntegrityIssues(result);
    toast(successMessage || result.message || "Done.", "success");
    if (refresh) await loadMaintenance();
    return result;
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}
$("#run-integrity-check").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/maintenance/integrity", { method: "GET", refresh: false, successMessage: "Integrity check complete." }));
$("#run-repair").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/maintenance/repair", { confirmMessage: "Repair rebuilds the search index and organized library from the database. Continue?" }));
$("#run-analyze").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/maintenance/analyze", { refresh: false }));
$("#run-vacuum").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/maintenance/vacuum", { confirmMessage: "Vacuuming compacts the database file; it can take a while on a large library. Continue?", refresh: false }));
$("#run-backup").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/maintenance/backup"));
$("#retry-failed-links").addEventListener("click", event => runMaintenanceAction(event.currentTarget, "/api/links/retry-failed", { refresh: false }));

// Reader / review
$$('[data-reader-tab]').forEach(button => button.addEventListener("click", () => { state.readerTab = button.dataset.readerTab; renderReader(); }));
$$('[data-review-action]').forEach(button => button.addEventListener("click", () => reviewAction(button.dataset.reviewAction)));
$("#copy-story").addEventListener("click", async () => { await navigator.clipboard.writeText($("#story-reader").innerText); toast("Story copied."); });

// General
$("#copy-log").addEventListener("click", async () => { await navigator.clipboard.writeText($("#job-log").textContent); toast("Log copied."); });
$("#menu-button").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
const themeButton = $("#theme-button");
function applyTheme(value) { document.documentElement.dataset.theme = value === "system" ? "" : value; themeButton.textContent = value === "dark" ? "☾" : value === "light" ? "☀" : "◐"; }
let theme = localStorage.getItem("story-grabber-theme") || "system"; applyTheme(theme);
themeButton.addEventListener("click", () => { theme = theme === "system" ? "light" : theme === "light" ? "dark" : "system"; localStorage.setItem("story-grabber-theme", theme); applyTheme(theme); });

const POLL_ACTIVE_MS = 1500;      // a job is running: check often
const POLL_IDLE_MS = 6000;        // nothing running: check occasionally
const SUMMARY_IDLE_MS = 25000;    // slow background refresh while idle

function schedulePolling() {
  let summaryTimer = 0;

  const tick = async () => {
    if (document.hidden) {
      // Tab is in the background: stop polling entirely rather than
      // burning battery/requests on a page nobody is looking at. A single
      // visibilitychange listener (below) resumes immediately on return.
      return;
    }
    const active = await pollJobs();
    setTimeout(tick, active ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  };

  const summaryTick = async () => {
    if (!document.hidden && ["dashboard", "sources", "stories"].includes(routePage())) {
      try { await loadSummary(); } catch {}
    }
    summaryTimer = setTimeout(summaryTick, SUMMARY_IDLE_MS);
  };

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      tick();
      clearTimeout(summaryTimer);
      summaryTick();
    }
  });

  tick();
  summaryTick();
}

(async function init() {
  await pollJobs();
  try { await loadSummary(); } catch {}
  await renderRoute();
  schedulePolling();
})();
