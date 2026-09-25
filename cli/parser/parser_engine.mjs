#!/usr/bin/env node
/**
 * Lightweight web-content parser engine.
 *
 * Primary extractor:
 *   https://github.com/kepano/defuddle
 *
 * Fallback:
 *   https://github.com/mozilla/readability
 *
 * DOM:
 *   https://github.com/WebReflection/linkedom
 *
 * This is a PARSER ENGINE, not a browser.
 * It accepts HTML and returns clean structured JSON.
 *
 * Examples:
 *
 *   Parse local HTML:
 *     node parser_engine.mjs --html page.html --url-context "https://example.com/page" -o parsed.json
 *
 *   Parse HTML from stdin:
 *     type page.html | node parser_engine.mjs --stdin --url-context "https://example.com/page" -o parsed.json
 *
 *   Lightweight direct URL test (normal fetch only):
 *     node parser_engine.mjs --url "https://example.com/page" -o parsed.json
 *
 * Integration:
 *     import { parseContent } from "./parser_engine.mjs";
 *     const result = await parseContent(html, pageUrl);
 */

import fs from "node:fs/promises";
import process from "node:process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseHTML } from "linkedom";
import { Defuddle } from "defuddle/node";
import { Readability } from "@mozilla/readability";

const MIN_GOOD_WORDS = 80;

function normalizeSpace(value = "") {
  return String(value).replace(/\s+/g, " ").trim();
}

function safeUrl(baseUrl, href) {
  try {
    return new URL(href, baseUrl || undefined).href;
  } catch {
    return href || "";
  }
}

function makeDocument(html, url = "https://example.invalid/") {
  const { document } = parseHTML(html);

  // Some extraction libraries expect a usable location/base URL.
  // linkedom does not provide a full browser Location object, so relative URL
  // resolution is handled explicitly in this engine.
  return document;
}

function textOf(node) {
  return normalizeSpace(node?.textContent || "");
}

function extractTable(table) {
  const rows = [];

  for (const row of table.querySelectorAll("tr")) {
    const cells = [...row.querySelectorAll("th,td")].map((cell) =>
      textOf(cell)
    );

    if (cells.some(Boolean)) {
      rows.push(cells);
    }
  }

  return rows;
}

function extractList(list) {
  return [...list.children]
    .filter((node) => node.tagName === "LI")
    .map((node) => textOf(node))
    .filter(Boolean);
}

function contentDocument(contentHtml) {
  return parseHTML(`<!doctype html><html><body>${contentHtml || ""}</body></html>`).document;
}

function htmlToBlocks(contentHtml, baseUrl, parsedDocument = null) {
  if (!contentHtml || !contentHtml.trim()) return [];

  const document = parsedDocument || contentDocument(contentHtml);
  const root = document.body;

  const blocks = [];

  function walk(node) {
    if (!node || node.nodeType !== 1) return;

    const tag = node.tagName.toLowerCase();

    if (/^h[1-6]$/.test(tag)) {
      const text = textOf(node);
      if (text) {
        blocks.push({
          type: "heading",
          level: Number(tag.slice(1)),
          text,
        });
      }
      return;
    }

    if (tag === "p") {
      const text = textOf(node);
      if (text) {
        blocks.push({
          type: "paragraph",
          text,
        });
      }
      return;
    }

    if (tag === "blockquote") {
      const text = textOf(node);
      if (text) {
        blocks.push({
          type: "quote",
          text,
        });
      }
      return;
    }

    if (tag === "pre") {
      const text = node.textContent?.trim() || "";
      if (text) {
        const code = node.querySelector("code");
        blocks.push({
          type: "code",
          language:
            code?.getAttribute("data-language") ||
            code?.getAttribute("class") ||
            "",
          text,
        });
      }
      return;
    }

    if (tag === "ul" || tag === "ol") {
      const items = extractList(node);

      if (items.length) {
        blocks.push({
          type: "list",
          ordered: tag === "ol",
          items,
        });
      }
      return;
    }

    if (tag === "table") {
      const rows = extractTable(node);

      if (rows.length) {
        const caption = textOf(node.querySelector("caption"));

        blocks.push({
          type: "table",
          caption,
          rows,
        });
      }
      return;
    }

    if (tag === "img") {
      const src =
        node.getAttribute("src") ||
        node.getAttribute("data-src") ||
        node.getAttribute("data-lazy-src") ||
        "";

      if (src) {
        blocks.push({
          type: "image",
          src: safeUrl(baseUrl, src),
          alt: normalizeSpace(node.getAttribute("alt") || ""),
          title: normalizeSpace(node.getAttribute("title") || ""),
        });
      }
      return;
    }

    for (const child of node.children || []) {
      walk(child);
    }
  }

  for (const child of root.children || []) {
    walk(child);
  }

  return blocks;
}

function extractLinks(contentHtml, baseUrl, parsedDocument = null) {
  if (!contentHtml || !contentHtml.trim()) return [];

  const document = parsedDocument || contentDocument(contentHtml);

  const seen = new Set();
  const links = [];

  for (const anchor of document.querySelectorAll("a[href]")) {
    const href = anchor.getAttribute("href")?.trim();

    if (!href) continue;
    if (/^(#|javascript:|mailto:|tel:|data:)/i.test(href)) continue;

    const url = safeUrl(baseUrl, href);
    const text = textOf(anchor);
    const key = `${url}\n${text}`;

    if (seen.has(key)) continue;
    seen.add(key);

    links.push({ text, url });
  }

  return links;
}

function extractImages(contentHtml, baseUrl, parsedDocument = null) {
  if (!contentHtml || !contentHtml.trim()) return [];

  const document = parsedDocument || contentDocument(contentHtml);

  const seen = new Set();
  const images = [];

  for (const img of document.querySelectorAll("img")) {
    const raw =
      img.getAttribute("src") ||
      img.getAttribute("data-src") ||
      img.getAttribute("data-lazy-src") ||
      "";

    if (!raw) continue;

    const src = safeUrl(baseUrl, raw);
    if (seen.has(src)) continue;
    seen.add(src);

    images.push({
      src,
      alt: normalizeSpace(img.getAttribute("alt") || ""),
      title: normalizeSpace(img.getAttribute("title") || ""),
    });
  }

  return images;
}

function markdownFromSimpleHtml(contentHtml) {
  // Minimal fallback only. Defuddle normally supplies Markdown itself.
  const { document } = parseHTML(
    `<!doctype html><html><body>${contentHtml || ""}</body></html>`
  );

  const lines = [];

  for (const node of document.body.children || []) {
    const tag = node.tagName.toLowerCase();
    const text = textOf(node);

    if (!text) continue;

    if (/^h[1-6]$/.test(tag)) {
      lines.push(`${"#".repeat(Number(tag.slice(1)))} ${text}`);
    } else if (tag === "blockquote") {
      lines.push(`> ${text}`);
    } else {
      lines.push(text);
    }

    lines.push("");
  }

  return lines.join("\n").trim();
}

async function parseWithDefuddle(html, url, debug = false, lean = false) {
  const document = makeDocument(html, url);

  const result = await Defuddle(
    document,
    url || undefined,
    {
      // `markdown` replaces `content` with Markdown. `separateMarkdown` keeps
      // structured HTML in `content` and adds `contentMarkdown` alongside it.
      markdown: false,
      separateMarkdown: !lean,
      debug,
      useAsync: false,
      removeExactSelectors: true,
      removePartialSelectors: true,
      removeHiddenElements: true,
      removeLowScoring: true,
      removeSmallImages: true,
      standardize: true,
    }
  );

  const contentHtml =
    typeof result?.content === "string" ? result.content : "";

  const markdown =
    typeof result?.contentMarkdown === "string"
      ? result.contentMarkdown
      : "";

  return {
    engine: "defuddle",
    contentHtml,
    markdown,
    title: result?.title || "",
    author: result?.author || "",
    description: result?.description || "",
    domain: result?.domain || "",
    favicon: result?.favicon || "",
    image: result?.image || "",
    language: result?.language || "",
    published: result?.published || "",
    site: result?.site || "",
    schemaOrgData: result?.schemaOrgData || null,
    metaTags: result?.metaTags || {},
    wordCount: Number(result?.wordCount || 0),
    parseTimeMs: Number(result?.parseTime || 0),
    debug: debug ? result?.debug || null : undefined,
  };
}

function parseWithReadability(html, url, lean = false) {
  const { document } = parseHTML(html);

  // Mozilla Readability mutates the document, so use a fresh DOM.
  const article = new Readability(document, {
    charThreshold: 200,
    keepClasses: false,
  }).parse();

  if (!article) {
    return null;
  }

  const contentHtml = article.content || "";
  const text = normalizeSpace(article.textContent || "");

  return {
    engine: "mozilla-readability",
    contentHtml,
    markdown: lean ? "" : markdownFromSimpleHtml(contentHtml),
    title: article.title || "",
    author: article.byline || "",
    description: article.excerpt || "",
    language: article.lang || "",
    site: article.siteName || "",
    published: article.publishedTime || "",
    wordCount: text ? text.split(/\s+/).length : 0,
    text,
  };
}

function plainTextFromHtml(contentHtml, parsedDocument = null) {
  if (!contentHtml) return "";

  const document = parsedDocument || contentDocument(contentHtml);

  return normalizeSpace(document.body?.textContent || "");
}

function qualityScore(parsed) {
  const text = parsed.text || plainTextFromHtml(parsed.contentHtml);
  const words = text ? text.split(/\s+/).filter(Boolean).length : 0;
  const chars = text.length;

  // Simple deterministic score used only to choose between two local parsers.
  return words * 4 + Math.min(chars, 20000) / 100;
}

export async function parseContent(html, url = null, options = {}) {
  if (typeof html !== "string" || !html.trim()) {
    throw new Error("HTML input is empty");
  }

  const debug = Boolean(options.debug);
  const lean = Boolean(options.lean);

  let defuddleResult = null;
  let defuddleError = null;

  try {
    defuddleResult = await parseWithDefuddle(html, url, debug, lean);
  } catch (error) {
    defuddleError = String(error?.message || error);
  }

  let chosen = defuddleResult;
  let fallbackUsed = false;
  let readabilityResult = null;

  const defuddleText = defuddleResult
    ? plainTextFromHtml(defuddleResult.contentHtml)
    : "";

  const defuddleWords = defuddleText
    ? defuddleText.split(/\s+/).filter(Boolean).length
    : 0;

  // Only run the fallback when Defuddle failed or extracted suspiciously little.
  if (!defuddleResult || defuddleWords < MIN_GOOD_WORDS) {
    try {
      readabilityResult = parseWithReadability(html, url, lean);

      if (
        readabilityResult &&
        (
          !chosen ||
          qualityScore(readabilityResult) > qualityScore(chosen)
        )
      ) {
        chosen = readabilityResult;
        fallbackUsed = true;
      }
    } catch {
      // Keep Defuddle result if Readability fails.
    }
  }

  if (!chosen) {
    throw new Error(
      `No parser could extract useful content${
        defuddleError ? `; Defuddle: ${defuddleError}` : ""
      }`
    );
  }

  const contentHtml = chosen.contentHtml || "";
  const parsedDocument = contentDocument(contentHtml);
  const text =
    chosen.text ||
    plainTextFromHtml(contentHtml, parsedDocument);

  const markdown = lean ? "" : (
    chosen.markdown ||
    markdownFromSimpleHtml(contentHtml)
  );

  const blocks = lean ? [] : htmlToBlocks(contentHtml, url, parsedDocument);
  const links = lean ? [] : extractLinks(contentHtml, url, parsedDocument);
  const images = lean ? [] : extractImages(contentHtml, url, parsedDocument);

  const blockTypes = {};
  for (const block of blocks) {
    blockTypes[block.type] = (blockTypes[block.type] || 0) + 1;
  }

  return {
    parser: {
      primary: "kepano/defuddle",
      fallback: "mozilla/readability",
      selected: chosen.engine,
      fallback_used: fallbackUsed,
      github: {
        defuddle: "https://github.com/kepano/defuddle",
        readability: "https://github.com/mozilla/readability",
        linkedom: "https://github.com/WebReflection/linkedom",
      },
    },

    source_url: url,

    metadata: {
      title: chosen.title || "",
      author: chosen.author || "",
      description: chosen.description || "",
      domain: chosen.domain || "",
      favicon: chosen.favicon || "",
      image: chosen.image || "",
      language: chosen.language || "",
      published: chosen.published || "",
      site: chosen.site || "",
      schema_org: chosen.schemaOrgData || null,
      meta_tags: chosen.metaTags || {},
    },

    content: {
      text,
      markdown,
      html: contentHtml,
      blocks,
    },

    links,
    images,

    stats: {
      words: text ? text.split(/\s+/).filter(Boolean).length : 0,
      characters: text.length,
      blocks: blocks.length,
      block_types: blockTypes,
      links: links.length,
      images: images.length,
      parse_time_ms: chosen.parseTimeMs || null,
    },

    diagnostics: {
      defuddle_error: defuddleError,
      defuddle_word_count: defuddleWords,
      readability_tested: readabilityResult !== null,
      defuddle_debug:
        debug && defuddleResult ? defuddleResult.debug || null : null,
    },
  };
}

async function fetchHtml(url, userAgent) {
  const response = await fetch(url, {
    redirect: "follow",
    headers: {
      "User-Agent":
        userAgent ||
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) " +
          "Chrome/153.0.0.0 Safari/537.36",
      Accept:
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
    },
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }

  const contentType = response.headers.get("content-type") || "";

  if (
    !contentType.includes("text/html") &&
    !contentType.includes("application/xhtml+xml")
  ) {
    throw new Error(`Not HTML: ${contentType || "unknown content type"}`);
  }

  return {
    html: await response.text(),
    finalUrl: response.url,
    status: response.status,
  };
}

function usage() {
  console.log(`
Usage:
  node parser_engine.mjs --html page.html [--url-context URL] [-o parsed.json]
  node parser_engine.mjs --stdin [--url-context URL] [-o parsed.json]
  node parser_engine.mjs --url URL [-o parsed.json]
  node parser_engine.mjs --url URL --debug

Options:
  --html FILE          Parse an existing HTML file
  --stdin              Parse HTML from stdin
  --url URL            Lightweight direct fetch + parse
  --url-context URL    Original URL for relative links/metadata
  -o, --output FILE    Output JSON (default: parsed_content.json)
  --debug              Include Defuddle debug information
  --user-agent VALUE   User-Agent for --url fetch mode
`);
}

function parseArgs(argv) {
  const args = {
    html: null,
    stdin: false,
    url: null,
    urlContext: null,
    output: "parsed_content.json",
    debug: false,
    userAgent: null,
  };

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];

    if (arg === "--html") args.html = argv[++i];
    else if (arg === "--stdin") args.stdin = true;
    else if (arg === "--url") args.url = argv[++i];
    else if (arg === "--url-context") args.urlContext = argv[++i];
    else if (arg === "-o" || arg === "--output") args.output = argv[++i];
    else if (arg === "--debug") args.debug = true;
    else if (arg === "--user-agent") args.userAgent = argv[++i];
    else if (arg === "-h" || arg === "--help") {
      usage();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  const sourceCount =
    Number(Boolean(args.html)) +
    Number(Boolean(args.stdin)) +
    Number(Boolean(args.url));

  if (sourceCount !== 1) {
    throw new Error(
      "Choose exactly one input: --html, --stdin, or --url"
    );
  }

  return args;
}

async function readStdin() {
  let data = "";
  process.stdin.setEncoding("utf8");

  for await (const chunk of process.stdin) {
    data += chunk;
  }

  return data;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  let html;
  let sourceUrl = args.urlContext;
  let fetchInfo = null;

  if (args.html) {
    html = await fs.readFile(args.html, "utf8");
  } else if (args.stdin) {
    html = await readStdin();
  } else {
    fetchInfo = await fetchHtml(args.url, args.userAgent);
    html = fetchInfo.html;
    sourceUrl = fetchInfo.finalUrl || args.url;
  }

  const result = await parseContent(
    html,
    sourceUrl,
    { debug: args.debug }
  );

  if (fetchInfo) {
    result.fetch = {
      requested_url: args.url,
      final_url: fetchInfo.finalUrl,
      http_status: fetchInfo.status,
    };
  }

  await fs.writeFile(
    args.output,
    JSON.stringify(result, null, 2) + "\n",
    "utf8"
  );

  const mdPath = args.output.replace(/\.json$/i, "") + ".md";
  await fs.writeFile(mdPath, result.content.markdown + "\n", "utf8");

  console.log("Selected parser :", result.parser.selected);
  console.log("Fallback used   :", result.parser.fallback_used);
  console.log("Title           :", result.metadata.title);
  console.log("Words           :", result.stats.words);
  console.log("Blocks          :", result.stats.blocks);
  console.log("Block types     :", JSON.stringify(result.stats.block_types));
  console.log("Links           :", result.stats.links);
  console.log("Images          :", result.stats.images);
  console.log("Saved JSON      :", args.output);
  console.log("Saved Markdown  :", mdPath);
}

const isMain = process.argv[1] &&
  path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1]);

if (isMain) {
  main().catch((error) => {
    console.error("ERROR:", error?.stack || error);
    process.exit(1);
  });
}
