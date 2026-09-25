import { parentPort } from "node:worker_threads";
import { parseContent } from "./parser_engine.mjs";

parentPort.on("message", async ({ id, html, url, options }) => {
  try {
    const result = await parseContent(html, url, options);
    parentPort.postMessage({ id, result });
  } catch (error) {
    parentPort.postMessage({
      id,
      error: error?.stack || error?.message || String(error),
    });
  }
});
