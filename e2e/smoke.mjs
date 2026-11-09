// Playwright smoke test: upload a 300-page PDF, ask a question, click a citation,
// and check the viewer opens at the cited page. Fails on any console error.
//   BASE_URL=http://localhost:8000 PDF=/path/sample300.pdf node e2e/smoke.mjs
import { createRequire } from "node:module";

// Resolve playwright via require() so a globally installed copy (NODE_PATH) works too.
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE_URL || "http://localhost:8000";
const PDF = process.env.PDF;
const SHOTS = process.env.SHOTS || "";
const QUESTION = process.env.QUESTION || "What is the checkpoint code for section 137?";
const EXPECT_PAGE = Number(process.env.EXPECT_PAGE || 137);
if (!PDF) throw new Error("Set PDF=/path/to/file.pdf");

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: SHOTS ? 2 : 1 });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));

const t0 = Date.now();
await page.goto(BASE);
await page.waitForSelector("#empty-state");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/01-empty.png` });

await page.setInputFiles("#file-input", PDF);
await page.waitForSelector(".doc .chip-busy", { timeout: 15000 });
if (SHOTS) await page.screenshot({ path: `${SHOTS}/02-uploading.png` });
await page.waitForSelector(".doc .chip-ready", { timeout: 180000 });
console.log(`indexed in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
await page.waitForSelector(".suggestion:not(.skeleton)", { timeout: 20000 });
if (SHOTS) await page.screenshot({ path: `${SHOTS}/03-ready.png` });

await page.fill("#composer-input", QUESTION);
await page.keyboard.press("Enter");
await page.waitForSelector(".msg-bot .route-badge:not(.route-working)", { timeout: 60000 });
const route = await page.textContent(".msg-bot .route-badge");
console.log("route:", route.trim());
if (SHOTS) await page.screenshot({ path: `${SHOTS}/04-answer.png` });

// Prefer the citation whose source is the expected page; otherwise take the first PDF citation.
const cited = await page.$$eval(".msg-bot .source[data-tag^='P']", (els) =>
  els.map((e) => ({ tag: e.dataset.tag, page: Number((e.textContent.match(/p\.(\d+)/) || [])[1]) })));
console.log("cited pages:", cited.map((c) => `${c.tag}=p.${c.page}`).join(" "));
const target = cited.find((c) => c.page === EXPECT_PAGE) || cited[0];
if (!target) throw new Error("answer has no PDF citations");
const cite = page.locator(`.msg-bot .answer .cite-pdf[data-tag="${target.tag}"]`).first();
await cite.hover();
await page.waitForSelector("#tooltip:not([hidden])");
await cite.click();
await page.waitForSelector("#viewer:not([hidden])");
await page.waitForFunction(() => document.querySelectorAll("#text-layer span").length > 0, null, { timeout: 20000 });
const shown = Number(await page.inputValue("#vw-page"));
const hl = await page.locator("#text-layer span.hl").count();
console.log(`viewer page=${shown} highlighted spans=${hl}`);
if (SHOTS) await page.screenshot({ path: `${SHOTS}/05-viewer.png` });

await browser.close();
const failures = [];
if (shown !== target.page) failures.push(`expected viewer on cited page ${target.page}, got ${shown}`);
if (!cited.some((c) => c.page === EXPECT_PAGE)) console.warn(`note: page ${EXPECT_PAGE} was not among the cited pages`);
if (!hl) failures.push("no highlighted text on the cited page");
if (errors.length) failures.push(`console errors:\n  ${errors.join("\n  ")}`);
if (failures.length) { console.error("FAIL\n" + failures.join("\n")); process.exit(1); }
console.log("PASS");
