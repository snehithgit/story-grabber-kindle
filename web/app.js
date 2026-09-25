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
    }
    if (page === "settings") await loadSettings();
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

function renderRows(tbody, items, compact = false) {
  tbody.replaceChildren();
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state">No stories found.</div></td></tr>`;
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    if (compact) {
      row.innerHTML = `<td class="story-cell"><strong></strong><small></small></td><td class="folder-cell mobile-meta" data-label="Folder"></td><td class="status-cell mobile-meta" data-label="Status">${statusPill(item.status)}</td><td class="row-actions"><button class="btn small open-story" type="button">Read</button></td>`;
      $("strong", row).textContent = item.title || "Untitled";
      $("small", row).textContent = item.source_host || item.url;
      $(".folder-cell", row).textContent = folderLabel(item);
    } else {
      row.innerHTML = `<td class="story-cell"><strong></strong><small></small></td><td class="folder-cell mobile-meta" data-label="Folder"></td><td class="category-cell mobile-meta" data-label="Category"></td><td class="words-cell mobile-meta" data-label="Words">${fmt(item.words)}</td><td class="status-cell mobile-meta" data-label="Status">${statusPill(item.status)}</td><td class="row-actions"><button class="btn small open-story" type="button">Read</button></td>`;
      $("strong", row).textContent = item.title || "Untitled";
      $("small", row).textContent = item.status === "review" && item.review_reason ? item.review_reason : item.url;
      $(".folder-cell", row).textContent = item.part_number != null ? `📁 ${item.series_title} / Part ${item.part_number}` : "Single story";
      $(".category-cell", row).textContent = item.category || "Uncategorized";
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
  const select = $("#story-category");
  const current = select.value || "all";
  select.innerHTML = '<option value="all">All categories</option>';
  for (const name of state.settings?.categories || []) {
    const option = document.createElement("option"); option.value = name; option.textContent = name; select.append(option);
  }
  const uncategorized = document.createElement("option"); uncategorized.value = "Uncategorized"; uncategorized.textContent = "Uncategorized"; select.append(uncategorized);
  if ([...select.options].some(o => o.value === current)) select.value = current;
}

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
  renderRows($("#stories-table"), data.items, false);
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

async function pollJobs() {
  try {
    state.jobs = await api("/api/status/all");
    const active = Object.values(state.jobs).find(isActive);
    $("#job-chip").classList.toggle("running", Boolean(active));
    $("#job-chip-text").textContent = active ? `${jobLabel(active.name)} · ${active.state}` : "Idle";
    $("#engine-summary").textContent = active ? `${jobLabel(active.name)} running` : "Idle";
    if (routePage() === "dashboard") renderJobs();
  } catch {}
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

async function loadSettings() {
  const settings = await api("/api/settings");
  state.settings = settings;
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (key === "categories") input.value = (value || []).join("\n");
    else if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
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
  if (!confirm("Re-categorize all existing stories using title first, then story content? This only rebuilds the organized library; raw/formatted stories are not changed.")) return;
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
$("#run-formatter").addEventListener("click", async () => {
  try { const result = await api("/api/start/format", { method: "POST", body: JSON.stringify({ force: true }) }); toast(result.message, "success"); navigate("/dashboard"); $("#engine-panel").open = true; }
  catch (error) { toast(error.message, "error"); }
});

// Settings
$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget, payload = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.name === "categories") payload.categories = input.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    else payload[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try {
    const result = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    state.settings = result.settings;
    $("#settings-message").textContent = result.organization ? `Saved. Reorganized ${fmt(result.organization.stories)} stories.` : "Saved.";
    toast("Settings saved.", "success");
  } catch (error) { $("#settings-message").textContent = error.message; toast(error.message, "error"); }
});

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

(async function init() {
  await pollJobs();
  try { await loadSummary(); } catch {}
  await renderRoute();
  setInterval(pollJobs, 1500);
  setInterval(async () => {
    if (["dashboard", "sources", "stories"].includes(routePage())) {
      try { await loadSummary(); } catch {}
    }
  }, 8000);
})();
