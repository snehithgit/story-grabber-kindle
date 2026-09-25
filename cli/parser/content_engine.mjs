#!/usr/bin/env node
/**
 * Batch link-content engine for crawler output.
 *
 * Reads TXT, JSON, or JSONL URLs, fetches HTML with bounded concurrency, and
 * delegates content extraction to parser_engine.mjs. Successful pages are
 * stored as JSON + Markdown; manifest.json makes runs safely resumable.
 */

import crypto from "node:crypto";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";
import { Worker } from "node:worker_threads";
import { parseHTML } from "linkedom";
import { chromium } from "playwright";

const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (compatible; StoryGrabber/2.0)";

class FetchError extends Error {
  constructor(message, { retryable = false, status = null, fatal = false } = {}) {
    super(message);
    this.name = "FetchError";
    this.retryable = retryable;
    this.status = status;
    this.fatal = fatal;
  }
}

function usage() {
  console.log(`
Link Content Engine

Usage:
  node content_engine.mjs [INPUT] [options]

INPUT may be crawler JSON, JSONL, or a text file with one URL per line.
Default input: ../sublinks.json

Options:
  -o, --output DIR       Output directory (default: content_output)
  -c, --concurrency N    Parallel fetch workers (default: 3)
  --delay MS             Minimum delay per host (default: 500)
  --timeout MS           Per-request timeout (default: 30000)
  --retries N            Retries after transient failures (default: 2)
  --max-page-mb N        Maximum HTML response size (default: 8)
  --fetch-mode MODE      auto, http, cloudscraper, or browser (default: auto)
  --browser-mode MODE    background, headless, or visible (default: background)
  --browser-profile DIR  Persistent browser profile directory
  --browser-wait MS      Wait after DOM load for rendering (default: 1000)
  --html-only            Store only clean HTML in the pages directory
  --no-html-only         Store HTML, JSON, and Markdown (launcher override)
  --limit N              Process at most N input URLs
  --user-agent VALUE     HTTP User-Agent header
  --retry-failed         Retry URLs marked failed in an earlier run
  --force                Reprocess all URLs, including successful ones
  --debug                Include parser diagnostics
  --dry-run              Validate input and display work without fetching
  -h, --help             Show this help

Output:
  manifest.json          Compact run state and page index
  results.jsonl          One compact result record per input URL
  pages/*.json           Complete structured extraction per successful URL
  pages/*.md             Clean Markdown per successful URL
  pages/*.html           Minimal title + article-content HTML
`);
}

function integer(value, name, { min = 0 } = {}) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min) {
    throw new Error(`${name} must be an integer >= ${min}`);
  }
  return parsed;
}

function parseArgs(argv) {
  const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const args = {
    input: path.join(projectRoot, "sublinks.json"),
    output: path.join(projectRoot, "content_output"),
    concurrency: 3,
    delay: 500,
    timeout: 30_000,
    retries: 2,
    maxPageMb: 8,
    fetchMode: "auto",
    browserMode: process.platform === "win32" ? "background" : "headless",
    browserProfile: path.join(projectRoot, "content_browser_profile"),
    browserWait: 1000,
    limit: null,
    userAgent: DEFAULT_USER_AGENT,
    retryFailed: false,
    force: false,
    debug: false,
    dryRun: false,
    htmlOnly: false,
  };

  let positionalSeen = false;
  const takeValue = (index, option) => {
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("-")) {
      throw new Error(`${option} requires a value`);
    }
    return value;
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "-h" || arg === "--help") {
      usage();
      process.exit(0);
    } else if (arg === "-o" || arg === "--output") {
      args.output = path.resolve(takeValue(i++, arg));
    } else if (arg === "-c" || arg === "--concurrency") {
      args.concurrency = integer(takeValue(i++, arg), "concurrency", { min: 1 });
    } else if (arg === "--delay") {
      args.delay = integer(takeValue(i++, arg), "delay");
    } else if (arg === "--timeout") {
      args.timeout = integer(takeValue(i++, arg), "timeout", { min: 1 });
    } else if (arg === "--retries") {
      args.retries = integer(takeValue(i++, arg), "retries");
    } else if (arg === "--max-page-mb") {
      args.maxPageMb = integer(takeValue(i++, arg), "max-page-mb", { min: 1 });
    } else if (arg === "--fetch-mode") {
      args.fetchMode = takeValue(i++, arg);
      if (!["auto", "http", "cloudscraper", "browser"].includes(args.fetchMode)) {
        throw new Error("fetch-mode must be auto, http, cloudscraper, or browser");
      }
    } else if (arg === "--browser-mode") {
      args.browserMode = takeValue(i++, arg);
      if (!["background", "headless", "visible"].includes(args.browserMode)) {
        throw new Error("browser-mode must be background, headless, or visible");
      }
    } else if (arg === "--browser-profile") {
      args.browserProfile = path.resolve(takeValue(i++, arg));
    } else if (arg === "--browser-wait") {
      args.browserWait = integer(takeValue(i++, arg), "browser-wait");
    } else if (arg === "--limit") {
      args.limit = integer(takeValue(i++, arg), "limit", { min: 1 });
    } else if (arg === "--user-agent") {
      args.userAgent = takeValue(i++, arg);
      if (!args.userAgent) throw new Error("user-agent cannot be empty");
    } else if (arg === "--retry-failed") {
      args.retryFailed = true;
    } else if (arg === "--force") {
      args.force = true;
    } else if (arg === "--debug") {
      args.debug = true;
    } else if (arg === "--dry-run") {
      args.dryRun = true;
    } else if (arg === "--html-only") {
      args.htmlOnly = true;
    } else if (arg === "--no-html-only") {
      args.htmlOnly = false;
    } else if (arg.startsWith("-")) {
      throw new Error(`Unknown option: ${arg}`);
    } else if (!positionalSeen) {
      args.input = path.resolve(arg);
      positionalSeen = true;
    } else {
      throw new Error(`Unexpected argument: ${arg}`);
    }
  }
  return args;
}

function normalizeUrl(raw) {
  try {
    const url = new URL(String(raw).trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    url.hash = "";
    return url.href;
  } catch {
    return null;
  }
}

function itemFrom(value, inheritedTitle = "") {
  if (typeof value === "string") {
    const url = normalizeUrl(value);
    return url ? { url, discovered_title: inheritedTitle || "" } : null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const rawUrl = value.link || value.url || value.href || value.source_url;
  const url = normalizeUrl(rawUrl);
  if (!url) return null;
  return {
    url,
    discovered_title: String(value.title || value.name || inheritedTitle || "").trim(),
  };
}

function itemsFromJson(data) {
  const found = [];
  const visit = (value) => {
    if (Array.isArray(value)) {
      for (const entry of value) visit(entry);
      return;
    }
    if (!value || typeof value !== "object") {
      const item = itemFrom(value);
      if (item) found.push(item);
      return;
    }
    if (Array.isArray(value.sublinks)) {
      for (const entry of value.sublinks) {
        const item = itemFrom(entry);
        if (item) found.push(item);
      }
      return;
    }
    const item = itemFrom(value);
    if (item) found.push(item);
  };
  visit(data);
  return found;
}

export async function loadInput(inputPath) {
  const raw = await fs.readFile(inputPath, "utf8");
  const extension = path.extname(inputPath).toLowerCase();
  let items = [];

  if (extension === ".json") {
    items = itemsFromJson(JSON.parse(raw));
  } else if (extension === ".jsonl" || extension === ".ndjson") {
    for (const [index, line] of raw.split(/\r?\n/).entries()) {
      if (!line.trim()) continue;
      try {
        items.push(...itemsFromJson(JSON.parse(line)));
      } catch (error) {
        throw new Error(`Invalid JSON on line ${index + 1}: ${error.message}`);
      }
    }
  } else {
    items = raw
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#"))
      .map((line) => itemFrom(line))
      .filter(Boolean);
  }

  const unique = new Map();
  for (const item of items) {
    if (!unique.has(item.url)) unique.set(item.url, item);
  }
  return [...unique.values()];
}

function slug(value) {
  return String(value || "page")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 70) || "page";
}

function pageStem(item, title = "") {
  const url = new URL(item.url);
  const rawSegment = url.pathname.split("/").filter(Boolean).pop() || url.hostname;
  let lastSegment = rawSegment;
  try { lastSegment = decodeURIComponent(rawSegment); } catch {}
  const label = title || item.discovered_title || lastSegment;
  const hash = crypto.createHash("sha256").update(item.url).digest("hex").slice(0, 12);
  return `${slug(label)}-${hash}`;
}

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function cleanHtmlDocument(parsed) {
  const title = parsed.metadata.title || "Untitled";
  const normalizedTitle = title.trim().toLocaleLowerCase();
  let skippedDuplicateTitle = false;
  const allowed = new Set([
    "article", "section", "div", "p", "blockquote", "ul", "ol", "li",
    "pre", "code", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "u",
    "s", "sub", "sup", "mark", "small", "figure", "figcaption", "details",
    "summary", "dl", "dt", "dd", "br", "hr",
  ]);
  const discard = new Set([
    "script", "style", "template", "noscript", "iframe", "object", "embed",
    "form", "input", "button", "select", "textarea", "canvas", "svg", "img",
    "video", "audio", "source", "picture",
  ]);
  const { document } = parseHTML(`<body>${parsed.content.html || ""}</body>`);
  const render = (node) => {
    if (node.nodeType === 3) return escapeHtml(node.nodeValue || "");
    if (node.nodeType !== 1) return "";
    const tag = node.localName.toLowerCase();
    if (discard.has(tag)) return "";
    if (/^h[1-6]$/.test(tag)) {
      const text = String(node.textContent || "").trim().toLocaleLowerCase();
      if (!skippedDuplicateTitle && text === normalizedTitle) {
        skippedDuplicateTitle = true;
        return "";
      }
    }
    const children = [...node.childNodes].map(render).join("");
    if (!allowed.has(tag)) return children;
    if (tag === "br" || tag === "hr") return `<${tag}>`;
    return `<${tag}>${children}</${tag}>`;
  };
  let cleanBody = [...document.body.childNodes].map(render).join("").trim();
  if (!cleanBody && parsed.content.text) cleanBody = `<p>${escapeHtml(parsed.content.text)}</p>`;

  const language = /^[a-z]{2,8}(?:-[a-z0-9]+)*$/i.test(parsed.metadata.language || "")
    ? parsed.metadata.language
    : "";
  return [
    "<!doctype html>",
    `<html${language ? ` lang="${escapeHtml(language)}"` : ""}>`,
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${escapeHtml(title)}</title>`,
    "</head>",
    "<body>",
    "<main>",
    "<article>",
    `<h1>${escapeHtml(title)}</h1>`,
    cleanBody,
    "</article>",
    "</main>",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class ParsePool {
  constructor(size) {
    this.queue = [];
    this.slots = [];
    this.nextId = 1;
    this.closed = false;
    this.fatalError = null;
    for (let index = 0; index < size; index++) this.addWorker();
  }

  addWorker() {
    const worker = new Worker(new URL("./parser_worker.mjs", import.meta.url));
    const slot = { worker, task: null };
    this.slots.push(slot);
    worker.on("message", ({ id, result, error }) => {
      if (!slot.task || slot.task.id !== id) return;
      const task = slot.task;
      slot.task = null;
      if (error) task.reject(new Error(error));
      else task.resolve(result);
      this.pump();
    });
    worker.on("error", (error) => {
      if (slot.task) slot.task.reject(error);
      slot.task = null;
    });
    worker.on("exit", (code) => {
      if (slot.task) slot.task.reject(new Error(`Parser worker exited with code ${code}`));
      const position = this.slots.indexOf(slot);
      if (position >= 0) this.slots.splice(position, 1);
      if (!this.closed) {
        this.addWorker();
        this.pump();
      }
    });
  }

  parse(html, url, options) {
    if (this.closed) return Promise.reject(new Error("Parser pool is closed"));
    return new Promise((resolve, reject) => {
      this.queue.push({ id: this.nextId++, html, url, options, resolve, reject });
      this.pump();
    });
  }

  pump() {
    for (const slot of this.slots) {
      if (slot.task || !this.queue.length) continue;
      slot.task = this.queue.shift();
      slot.worker.postMessage({
        id: slot.task.id,
        html: slot.task.html,
        url: slot.task.url,
        options: slot.task.options,
      });
    }
  }

  async close() {
    this.closed = true;
    for (const task of this.queue.splice(0)) task.reject(new Error("Parser pool closed"));
    await Promise.all(this.slots.map(({ worker }) => worker.terminate()));
    this.slots = [];
  }
}

class HostThrottle {
  constructor(delayMs) {
    this.delayMs = delayMs;
    this.hosts = new Map();
  }

  async wait(url) {
    const host = new URL(url).host;
    const previous = this.hosts.get(host) || Promise.resolve();
    let release;
    const turn = new Promise((resolve) => { release = resolve; });
    this.hosts.set(host, previous.then(() => turn));
    await previous;
    try {
      if (this.delayMs) await sleep(this.delayMs);
    } finally {
      release();
    }
  }
}

class CloudscraperFetcher {
  constructor(args, throttle) {
    this.args = args;
    this.throttle = throttle;
    this.child = null;
    this.pending = new Map();
    this.nextId = 1;
    this.closed = false;
  }

  start() {
    if (this.fatalError) throw this.fatalError;
    if (this.closed) {
      throw new FetchError("cloudscraper fetcher is closing", { retryable: true });
    }
    if (this.child) return;
    const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const bridge = path.join(projectRoot, "cloudscraper_fetch.py");
    const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
    const child = spawn(python, ["-u", bridge], {
      cwd: projectRoot,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
        CLOUDSCRAPER_WORKERS: String(this.args.concurrency),
      },
    });
    this.child = child;
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (!message.ok) {
        pending.reject(new FetchError(message.error || "cloudscraper failed", {
          retryable: !message.status || [408, 425, 429].includes(message.status) || message.status >= 500,
          status: message.status || null,
        }));
        return;
      }
      pending.resolve({
        html: Buffer.from(message.html_base64, "base64").toString("utf8"),
        finalUrl: message.final_url,
        status: message.status,
        contentType: message.content_type,
        transport: "cloudscraper",
      });
    });
    child.stderr.on("data", (chunk) => {
      const message = String(chunk).trim();
      if (message) console.error(`cloudscraper: ${message}`);
    });
    child.on("error", (error) => {
      const failure = new FetchError(`Cannot start cloudscraper bridge: ${error.message}`, {
        retryable: false,
        fatal: error?.code === "ENOENT",
      });
      if (failure.fatal) this.fatalError = failure;
      this.rejectAll(failure);
      if (this.child === child) this.child = null;
    });
    child.on("exit", (code) => {
      this.rejectAll(new Error(`cloudscraper bridge exited with code ${code}`));
      if (this.child === child) this.child = null;
    });
    console.log("Fetcher    : cloudscraper 3.x (persistent sessions)");
  }

  rejectAll(error) {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }

  async fetchOnce(url) {
    this.start();
    await this.throttle.wait(url);
    const id = this.nextId++;
    const request = {
      id,
      url,
      timeout: this.args.timeout / 1000,
      max_bytes: this.args.maxPageMb * 1024 * 1024,
    };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new FetchError(`cloudscraper timed out after ${this.args.timeout}ms`, { retryable: true }));
      }, this.args.timeout + 10_000);
      this.pending.set(id, { resolve, reject, timer });
      const child = this.child;
      if (!child?.stdin?.writable) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new FetchError("cloudscraper bridge is unavailable", { retryable: true }));
        return;
      }
      child.stdin.write(`${JSON.stringify(request)}\n`, "utf8", (error) => {
        if (!error) return;
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error);
      });
    });
  }

  async fetch(url) {
    let lastError;
    for (let attempt = 0; attempt <= this.args.retries; attempt++) {
      try {
        return { ...(await this.fetchOnce(url)), attempts: attempt + 1 };
      } catch (error) {
        lastError = error;
        if (!error.retryable || attempt === this.args.retries) break;
        await sleep(Math.min(1000 * 2 ** attempt, 8000));
      }
    }
    throw lastError;
  }

  async close() {
    this.closed = true;
    if (!this.child) return;
    const child = this.child;
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        child.kill();
        resolve();
      }, 3000);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
      child.stdin.end();
    });
    this.child = null;
  }
}

function looksLikeChallenge(title, html) {
  const sample = `${title}\n${html.slice(0, 25_000)}`.toLowerCase();
  return [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "challenges.cloudflare.com",
    "cf-chl-",
  ].some((marker) => sample.includes(marker));
}

class BrowserFetcher {
  constructor(args, throttle) {
    this.args = args;
    this.throttle = throttle;
    this.context = null;
    this.startPromise = null;
    this.browserName = null;
  }

  async start() {
    if (this.context) return;
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.launch();
    try {
      await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  async launch() {
    await fs.mkdir(this.args.browserProfile, { recursive: true });
    const launchArgs = [
      "--no-first-run",
      "--no-default-browser-check",
      "--window-size=1400,900",
    ];
    if (this.args.browserMode === "background") {
      launchArgs.push("--window-position=-3000,-3000");
    }
    const baseOptions = {
      headless: this.args.browserMode === "headless",
      viewport: { width: 1400, height: 900 },
      locale: "en-US",
      args: launchArgs,
    };
    const candidates = process.platform === "win32"
      ? ["msedge", "chrome", null]
      : [null];
    let lastError;
    for (const channel of candidates) {
      try {
        const options = channel ? { ...baseOptions, channel } : baseOptions;
        this.context = await chromium.launchPersistentContext(
          this.args.browserProfile,
          options
        );
        this.browserName = channel || "playwright-chromium";
        console.log(`Browser    : ${this.browserName} (${this.args.browserMode})`);
        return;
      } catch (error) {
        lastError = error;
      }
    }
    throw new Error(`Could not launch a browser: ${lastError?.message || lastError}`);
  }

  async fetchOnce(url) {
    await this.start();
    await this.throttle.wait(url);
    const page = await this.context.newPage();
    page.setDefaultTimeout(this.args.timeout);
    let response = null;
    try {
      try {
        response = await page.goto(url, {
          waitUntil: "domcontentloaded",
          timeout: this.args.timeout,
        });
      } catch (error) {
        if (error?.name !== "TimeoutError") throw error;
        try { await page.evaluate(() => window.stop()); } catch {}
      }
      if (this.args.browserWait) await page.waitForTimeout(this.args.browserWait);
      let html = await page.content();
      let title = await page.title();
      let status = response?.status() ?? null;
      if (looksLikeChallenge(title, html)) {
        const waitMs = this.args.browserMode === "visible" ? 120_000 : 10_000;
        if (this.args.browserMode === "visible") {
          console.log("  Verification detected. Complete it in the browser window (waiting up to 2 minutes)...");
        }
        const deadline = Date.now() + waitMs;
        while (Date.now() < deadline && looksLikeChallenge(title, html)) {
          await page.waitForTimeout(2000);
          html = await page.content();
          title = await page.title();
        }
        if (!looksLikeChallenge(title, html)) status = null;
      }
      if (looksLikeChallenge(title, html)) {
        throw new FetchError(
          "Browser received a verification page; use --browser-mode visible to complete it",
          { retryable: true, status }
        );
      }
      if (status !== null && status >= 400) {
        throw new FetchError(`Browser HTTP ${status}`, {
          retryable: [408, 425, 429].includes(status) || status >= 500,
          status,
        });
      }
      const size = Buffer.byteLength(html, "utf8");
      const maxBytes = this.args.maxPageMb * 1024 * 1024;
      if (size > maxBytes) {
        throw new FetchError(`Rendered page exceeded ${maxBytes} byte limit`);
      }
      const headers = response ? await response.allHeaders() : {};
      return {
        html,
        finalUrl: page.url() || url,
        status,
        contentType: headers["content-type"] || "text/html (rendered)",
        browser: this.browserName,
      };
    } catch (error) {
      if (error instanceof FetchError) throw error;
      if (error?.name === "TimeoutError") {
        throw new FetchError(`Browser timed out after ${this.args.timeout}ms`, { retryable: true });
      }
      throw new FetchError(error?.message || String(error), { retryable: true });
    } finally {
      await page.close().catch(() => {});
    }
  }

  async fetch(url) {
    let lastError;
    for (let attempt = 0; attempt <= this.args.retries; attempt++) {
      try {
        return { ...(await this.fetchOnce(url)), attempts: attempt + 1 };
      } catch (error) {
        lastError = error;
        if (!error.retryable || attempt === this.args.retries) break;
        await sleep(Math.min(1000 * 2 ** attempt, 8000));
      }
    }
    throw lastError;
  }

  async close() {
    if (this.context) {
      await this.context.close();
      this.context = null;
    }
  }
}

async function responseBody(response, maxBytes) {
  const advertised = Number(response.headers.get("content-length") || 0);
  if (advertised > maxBytes) {
    throw new FetchError(`Response too large: ${advertised} bytes (limit ${maxBytes})`);
  }
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel();
      throw new FetchError(`Response exceeded ${maxBytes} byte limit`);
    }
    chunks.push(value);
  }
  const combined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return combined;
}

function decodeHtml(bytes, contentType = "") {
  const header = /charset\s*=\s*["']?\s*([\w.+:-]+)/i.exec(contentType)?.[1];
  const ascii = Buffer.from(bytes.buffer, bytes.byteOffset, Math.min(bytes.byteLength, 8192))
    .toString("latin1");
  const meta = /charset\s*=\s*["']?\s*([\w.+:-]+)/i.exec(ascii)?.[1];
  for (const encoding of [header, meta, "utf-8"]) {
    if (!encoding) continue;
    try { return new TextDecoder(encoding).decode(bytes); } catch {}
  }
  return Buffer.from(bytes).toString("utf8");
}

async function fetchOnce(url, args, throttle) {
  await throttle.wait(url);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), args.timeout);
  let response;
  try {
    response = await fetch(url, {
      redirect: "follow",
      signal: controller.signal,
      headers: {
        "User-Agent": args.userAgent,
        Accept: "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "Accept-Language": "en-US,en;q=0.8",
      },
    });
    if (!response.ok) {
      const retryable = [408, 425, 429].includes(response.status) || response.status >= 500;
      throw new FetchError(`HTTP ${response.status} ${response.statusText}`, {
        retryable,
        status: response.status,
      });
    }
    const contentType = response.headers.get("content-type") || "";
    if (!/text\/html|application\/xhtml\+xml/i.test(contentType)) {
      throw new FetchError(`Not HTML: ${contentType || "unknown content type"}`);
    }
    const bytes = await responseBody(response, args.maxPageMb * 1024 * 1024);
    const html = decodeHtml(bytes, contentType);
    return {
      html,
      finalUrl: response.url || url,
      status: response.status,
      contentType,
    };
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new FetchError(`Timed out after ${args.timeout}ms`, { retryable: true });
    }
    if (error instanceof FetchError) throw error;
    throw new FetchError(error?.message || String(error), { retryable: true });
  } finally {
    clearTimeout(timer);
  }
}

async function fetchWithRetries(url, args, throttle, fallbackStatuses = new Set()) {
  let lastError;
  for (let attempt = 0; attempt <= args.retries; attempt++) {
    try {
      return { ...(await fetchOnce(url, args, throttle)), attempts: attempt + 1 };
    } catch (error) {
      lastError = error;
      if (fallbackStatuses.has(error?.status)) break;
      if (!error.retryable || attempt === args.retries) break;
      await sleep(Math.min(1000 * 2 ** attempt, 8000));
    }
  }
  throw lastError;
}

/**
 * Write a file and force it onto the disk before returning, so a power cut
 * cannot leave it empty or half-written after we have recorded it as saved.
 */
async function writeDurable(file, data) {
  const handle = await fs.open(file, "w");
  try {
    await handle.writeFile(data, "utf8");
    await handle.sync();
  } finally {
    await handle.close();
  }
}

/** Persist a rename/create in the directory entry itself (no-op on Windows). */
async function syncDir(dir) {
  let handle;
  try {
    handle = await fs.open(dir, "r");
    await handle.sync();
  } catch {
    // Windows cannot open directories; NTFS journals metadata itself.
  } finally {
    await handle?.close().catch(() => {});
  }
}

async function atomicJson(file, value, { backup = false } = {}) {
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await writeDurable(temporary, JSON.stringify(value, null, 2) + "\n");
  if (backup) {
    // Keep the previous good copy; readManifest falls back to it if needed.
    await fs.copyFile(file, `${file}.bak`).catch(() => {});
  }
  let lastError;
  try {
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        await fs.rename(temporary, file);
        await syncDir(path.dirname(file));
        return;
      } catch (error) {
        lastError = error;
        if (!["EPERM", "EACCES", "EBUSY"].includes(error?.code)) throw error;
        await sleep(Math.min(25 * 2 ** attempt, 800));
      }
    }

    // Antivirus and concurrent readers can briefly prevent rename on Windows.
    // After bounded retries, copy the complete temp file over the destination.
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        await fs.copyFile(temporary, file);
        await fs.unlink(temporary).catch(() => {});
        return;
      } catch (error) {
        lastError = error;
        if (!["EPERM", "EACCES", "EBUSY"].includes(error?.code)) throw error;
        await sleep(Math.min(100 * 2 ** attempt, 1000));
      }
    }
    throw lastError;
  } finally {
    await fs.unlink(temporary).catch(() => {});
  }
}

async function readManifest(file) {
  // After a power cut the newest manifest may be damaged; fall back to the
  // previous good copy (manifest.json.bak) instead of refusing to start.
  for (const candidate of [file, `${file}.bak`]) {
    try {
      const value = JSON.parse(await fs.readFile(candidate, "utf8"));
      if (value?.version === 1 && value.pages && typeof value.pages === "object") {
        if (candidate !== file) console.log(`Recovered  : ${path.basename(file)} was damaged; using backup copy`);
        return value;
      }
    } catch (error) {
      if (error.code === "ENOENT") continue;
      if (candidate === file) {
        const damaged = `${file}.damaged-${Date.now()}`;
        await fs.rename(file, damaged).catch(() => {});
        console.log(`Warning    : ${path.basename(file)} is unreadable (${error.message}); kept as ${path.basename(damaged)}`);
      }
    }
  }
  return { version: 1, created_at: new Date().toISOString(), pages: {} };
}

async function replayProgress(file, manifest) {
  let raw;
  try {
    raw = await fs.readFile(file, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line);
      if (entry?.url && entry?.state) manifest.pages[entry.url] = entry.state;
    } catch {
      // A crash may leave only the last JSONL line incomplete; earlier lines
      // remain valid and are still replayed.
    }
  }
}

function compactRecord(item, state) {
  return {
    url: item.url,
    discovered_title: item.discovered_title,
    status: state.status,
    final_url: state.final_url || null,
    title: state.title || "",
    words: state.words || 0,
    json_file: state.json_file || null,
    markdown_file: state.markdown_file || null,
    html_file: state.html_file || null,
    error: state.error || null,
  };
}

async function run(args) {
  let items = await loadInput(args.input);
  if (args.limit !== null) items = items.slice(0, args.limit);
  if (!items.length) throw new Error(`No valid HTTP(S) URLs found in ${args.input}`);

  console.log(`Input      : ${args.input}`);
  console.log(`URLs       : ${items.length}`);
  console.log(`Output     : ${args.output}`);
  console.log(`Concurrency: ${args.concurrency}`);
  console.log(`Fetch mode : ${args.fetchMode}`);
  if (args.dryRun) {
    for (const item of items.slice(0, 10)) console.log(`  ${item.url}`);
    if (items.length > 10) console.log(`  ... and ${items.length - 10} more`);
    console.log("Dry run complete; nothing fetched or written.");
    return;
  }

  const pagesDir = path.join(args.output, "pages");
  const manifestPath = path.join(args.output, "manifest.json");
  const progressPath = path.join(args.output, "progress.jsonl");
  await fs.mkdir(pagesDir, { recursive: true });
  const manifest = await readManifest(manifestPath);
  await replayProgress(progressPath, manifest);
  manifest.input_file = args.input;
  manifest.updated_at = new Date().toISOString();
  manifest.settings = {
    concurrency: args.concurrency,
    delay_ms: args.delay,
    timeout_ms: args.timeout,
    retries: args.retries,
    max_page_mb: args.maxPageMb,
    fetch_mode: args.fetchMode,
    browser_mode: args.browserMode,
    browser_profile: args.browserProfile,
    html_only: args.htmlOnly,
  };

  const pending = items.filter((item) => {
    const old = manifest.pages[item.url];
    if (args.force || !old) return true;
    if (old.status === "success") return false;
    return args.retryFailed;
  });
  const skipped = items.length - pending.length;
  console.log(`Pending    : ${pending.length}${skipped ? ` (${skipped} resumed/skipped)` : ""}`);

  const throttle = new HostThrottle(args.delay);
  const browserFetcher = new BrowserFetcher(args, throttle);
  const cloudscraperFetcher = new CloudscraperFetcher(args, throttle);
  const parserPool = new ParsePool(Math.max(1, Math.min(args.concurrency, pending.length || 1)));
  const browserHosts = new Set();
  const cloudscraperHosts = new Set();
  let cursor = 0;
  let completed = 0;
  let succeeded = 0;
  let failed = 0;
  let writeChain = Promise.resolve();
  let updatesSinceCheckpoint = 0;
  const queueWrite = (action) => {
    const operation = writeChain.then(action);
    // Keep the queue usable after one failed write while returning the failure
    // to the worker that requested this specific save.
    writeChain = operation.catch(() => {});
    return operation;
  };
  let journal = null;
  const closeJournal = async () => {
    const handle = journal;
    journal = null;
    await handle?.close().catch(() => {});
  };
  const checkpoint = () => queueWrite(async () => {
    // The manifest is fully on disk before the journal is emptied.
    await atomicJson(manifestPath, manifest, { backup: true });
    await closeJournal();
    await writeDurable(progressPath, "");
    updatesSinceCheckpoint = 0;
  });
  const appendJournal = async (line) => {
    journal ??= await fs.open(progressPath, "a");
    await journal.write(line);
    await journal.datasync();       // survive a power cut right after this page
  };
  const savePageState = (url) => {
    const line = JSON.stringify({ url, state: manifest.pages[url] }) + "\n";
    updatesSinceCheckpoint++;
    const appended = queueWrite(() => appendJournal(line));
    if (updatesSinceCheckpoint >= 100) {
      return appended.then(() => checkpoint());
    }
    return appended;
  };
  // Compact any journal recovered from a previous interrupted run before new work.
  await checkpoint();

  async function processItem(item) {
    const startedAt = Date.now();
    let fatalError = null;
    try {
      const host = new URL(item.url).host;
      let fetched;
      if (args.fetchMode === "browser" || browserHosts.has(host)) {
        fetched = await browserFetcher.fetch(item.url);
      } else if (args.fetchMode === "cloudscraper" || cloudscraperHosts.has(host)) {
        try {
          fetched = await cloudscraperFetcher.fetch(item.url);
        } catch (cloudscraperError) {
          if (args.fetchMode !== "auto") throw cloudscraperError;
          if ([401, 403, 429].includes(cloudscraperError?.status)) browserHosts.add(host);
          console.log(`  cloudscraper failed; using browser for this request (${host})`);
          fetched = await browserFetcher.fetch(item.url);
        }
      } else {
        try {
          fetched = await fetchWithRetries(
            item.url,
            args,
            throttle,
            args.fetchMode === "auto" ? new Set([401, 403, 429]) : new Set()
          );
        } catch (error) {
          const shouldUseBrowser =
            args.fetchMode === "auto" && [401, 403, 429].includes(error?.status);
          if (!shouldUseBrowser) throw error;
          cloudscraperHosts.add(host);
          try {
            console.log(`  Switching ${host} to cloudscraper after HTTP ${error.status}`);
            fetched = await cloudscraperFetcher.fetch(item.url);
          } catch (cloudscraperError) {
            if ([401, 403, 429].includes(cloudscraperError?.status)) browserHosts.add(host);
            console.log(`  cloudscraper failed; using browser for this request (${host})`);
            fetched = await browserFetcher.fetch(item.url);
          }
        }
      }
      const parsed = await parserPool.parse(fetched.html, fetched.finalUrl, {
        debug: args.debug,
        lean: args.htmlOnly,
      });
      const stem = pageStem(item, parsed.metadata.title);
      const jsonName = `${stem}.json`;
      const markdownName = `${stem}.md`;
      const htmlName = `${stem}.html`;
      const jsonPath = path.join(pagesDir, jsonName);
      const markdownPath = path.join(pagesDir, markdownName);
      const htmlPath = path.join(pagesDir, htmlName);
      parsed.fetch = {
        requested_url: item.url,
        final_url: fetched.finalUrl,
        http_status: fetched.status,
        content_type: fetched.contentType,
        attempts: fetched.attempts,
        fetched_at: new Date().toISOString(),
        browser: fetched.browser || null,
        transport: fetched.transport || (fetched.browser ? "browser" : "http"),
      };
      parsed.input = { discovered_title: item.discovered_title };
      if (!args.htmlOnly) {
        await atomicJson(jsonPath, parsed);
        await writeDurable(markdownPath, `${parsed.content.markdown.trim()}\n`);
      }
      await writeDurable(htmlPath, cleanHtmlDocument(parsed));
      manifest.pages[item.url] = {
        status: "success",
        final_url: fetched.finalUrl,
        title: parsed.metadata.title,
        words: parsed.stats.words,
        parser: parsed.parser.selected,
        fetcher: fetched.transport || (fetched.browser ? "browser" : "http"),
        json_file: args.htmlOnly ? null : path.posix.join("pages", jsonName),
        markdown_file: args.htmlOnly ? null : path.posix.join("pages", markdownName),
        html_file: path.posix.join("pages", htmlName),
        attempts: fetched.attempts,
        duration_ms: Date.now() - startedAt,
        updated_at: new Date().toISOString(),
      };
      succeeded++;
      console.log(`[${++completed}/${pending.length}] OK   ${parsed.stats.words} words  ${item.url}`);
    } catch (error) {
      manifest.pages[item.url] = {
        status: "failed",
        error: error?.message || String(error),
        http_status: error?.status || null,
        duration_ms: Date.now() - startedAt,
        updated_at: new Date().toISOString(),
      };
      failed++;
      console.error(`[${++completed}/${pending.length}] FAIL ${item.url} — ${error?.message || error}`);
      if (error?.fatal) fatalError = error;
    }
    manifest.updated_at = new Date().toISOString();
    await savePageState(item.url);
    if (fatalError) throw fatalError;
  }

  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= pending.length) return;
      await processItem(pending[index]);
    }
  }

  try {
    const outcomes = await Promise.allSettled(
      Array.from({ length: Math.min(args.concurrency, pending.length) }, worker)
    );
    const rejected = outcomes.find((outcome) => outcome.status === "rejected");
    if (rejected) throw rejected.reason;
    await writeChain;
  } finally {
    await cloudscraperFetcher.close();
    await browserFetcher.close();
    await parserPool.close();
  }

  const records = items.map((item) => compactRecord(item, manifest.pages[item.url] || { status: "not_processed" }));
  await writeDurable(
    path.join(args.output, "results.jsonl"),
    records.map((record) => JSON.stringify(record)).join("\n") + "\n"
  );
  manifest.summary = {
    input_urls: items.length,
    success: records.filter((record) => record.status === "success").length,
    failed: records.filter((record) => record.status === "failed").length,
    not_processed: records.filter((record) => record.status === "not_processed").length,
  };
  manifest.updated_at = new Date().toISOString();
  await checkpoint();

  console.log(`Finished   : ${succeeded} succeeded, ${failed} failed, ${skipped} skipped`);
  console.log(`Manifest   : ${manifestPath}`);
  console.log(`Index      : ${path.join(args.output, "results.jsonl")}`);
  if (failed) process.exitCode = 2;
}

const isMain = process.argv[1] &&
  path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1]);

if (isMain) {
  try {
    await run(parseArgs(process.argv.slice(2)));
  } catch (error) {
    console.error(`ERROR: ${error?.message || error}`);
    process.exitCode = 1;
  }
}
