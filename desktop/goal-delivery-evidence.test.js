"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const destination = path.join(os.tmpdir(), "A portable project with a long folder name");
const test = require("node:test");
const vm = require("node:vm");
const {chromium} = require("playwright-core");
const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
const helper = source.slice(source.indexOf("function chatDeliveryNotice"), source.indexOf("function normalizedLongHorizonCorrelation"));

test("only engine-owned working-copy evidence adds a delivery notice", () => {
  const context = vm.createContext({});
  vm.runInContext(helper, context);
  assert.equal(context.chatDeliveryNotice({text:"The game is delivered"}), "");
  assert.equal(context.chatDeliveryNotice({correlation:{schema_version:1, kind:"user",delivery_contract:"selected-project-delivery/v1",delivery_state:"working_copy_report"}}), "");
  const result = context.chatDeliveryNotice({correlation:{schema_version:1,kind:"long_horizon_agent_event",delivery_contract:"selected-project-delivery/v1",delivery_state:"working_copy_report",delivery_project:destination}});
  assert.match(result,/Not delivered at this point/);
  assert.ok(result.includes(destination));
});

test("delivery notice and exact destination are visible at desktop and narrow widths", async () => {
  const runtime = path.join(__dirname,"build-output/win-unpacked/resources/runtime");
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime,"NEXUS_RUNTIME.json"),"utf8"));
  const browser = await chromium.launch({headless:true,executablePath:path.join(runtime,"playwright",manifest.playwright.chromium_executable)});
  try {
    const page = await browser.newPage();
    await page.setContent('<main class="the-big-chat-what"><div id="notice"></div><p>The game is playable by opening the file.</p></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(__dirname,"../src/our_harness/ui/styles.css"),"utf8")});
    await page.addScriptTag({content:helper + `document.querySelector('#notice').className='chat-delivery-notice';document.querySelector('#notice').textContent=chatDeliveryNotice({correlation:{schema_version:1,kind:'long_horizon_agent_event',delivery_contract:'selected-project-delivery/v1',delivery_state:'working_copy_report',delivery_project:${JSON.stringify(destination)}}});`});
    for (const width of [1264,390]) {
      await page.setViewportSize({width,height:775});
      assert.ok(await page.locator('#notice').isVisible());
      assert.match(await page.locator('#notice').innerText(),/Not delivered at this point/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    }
  } finally {await browser.close();}
});
