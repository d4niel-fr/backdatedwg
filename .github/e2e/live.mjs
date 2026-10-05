// End-to-end check of a deployed site: converts a real DWG and a DXF in a
// real browser and verifies the downloaded file's AutoCAD version.
//   node live.mjs <site url> <dwg file> <dxf file>
import { chromium } from "playwright";

const [url, dwg, dxf] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
const problems = [];
page.on("pageerror", (e) => problems.push(`page error: ${e}`));
page.on("console", (m) => m.type() === "error" && console.log("console:", m.text()));

const res = await page.goto(url, { waitUntil: "networkidle" });
console.log("GET", url, "->", res.status());
if (res.status() !== 200) throw new Error(`site returned ${res.status()}`);
await page.waitForSelector("#choose-btn");
console.log("header tag:", await page.textContent("#free-tag"));

async function convert(file, target, expectCode) {
  await page.setInputFiles("#file-input", file);
  await page.waitForSelector("#convert-btn:not([hidden])", { timeout: 15000 });
  console.log("picked:", await page.textContent("#pick-name"), "|", await page.textContent("#pick-meta"));
  await page.click(`#target-chips label:has(input[value="${target}"])`);
  const t0 = Date.now();
  await page.click("#convert-btn");
  await page.waitForFunction(
    () => !document.querySelector("#screen-done").hidden || !document.querySelector("#drop-error").hidden,
    null,
    { timeout: 240000 },
  );
  if (await page.isVisible("#drop-error")) {
    throw new Error(`conversion failed: ${await page.textContent("#drop-error")}`);
  }
  const secs = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`done in ${secs}s:`, await page.textContent("#done-title"), "|", await page.textContent("#done-meta"));
  console.log("report:", await page.textContent("#report-tag"));
  for (const li of await page.$$eval("#report-list li", (ls) => ls.map((l) => l.innerText.replace(/\n/g, " — "))))
    console.log("   ", li);
  const href = await page.getAttribute("#download-btn", "href");
  const head = await page.evaluate(async (h) => {
    const buf = await (await fetch(h)).arrayBuffer();
    return { size: buf.byteLength, text: new TextDecoder().decode(buf.slice(0, 4000)) };
  }, href);
  const ver = /\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})/.exec(head.text);
  console.log("download:", head.size, "bytes, $ACADVER =", ver && ver[1]);
  if (!ver || ver[1] !== expectCode) throw new Error(`expected ${expectCode}, got ${ver && ver[1]}`);
  await page.screenshot({ path: `done-${target}.png`, fullPage: true });
  await page.click("#again-btn");
}

await convert(dwg, "2010", "AC1024");
await convert(dxf, "2004", "AC1018");
await browser.close();
if (problems.length) throw new Error(problems.join("\n"));
console.log("LIVE SITE OK");
