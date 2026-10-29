// Verify dashboard cycle details moved to the learning analysis surface; related: panel.js.
const {chromium}=require(process.argv[2]||"playwright");
const assert=require("node:assert/strict"),path=require("node:path");
(async()=>{const browser=await chromium.launch({channel:"msedge",headless:true});try{
  const page=await browser.newPage(),errors=[];page.on("pageerror",error=>errors.push(error.message));
  await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
  await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
  await page.evaluate(()=>{window.panel=document.querySelector("adaptive-floor-heating-panel");window.hass={connection:{connected:true,subscribeEvents:async()=>()=>{}},config:{unit_system:{temperature:"°C"}},states:{"climate.room":{state:"auto",attributes:{current_temperature:22.5,temperature:23,last_peak_comparison:{actual_peak:23.4}}}},callWS:async request=>request.type==="config/entity_registry/list"?[{platform:"adaptive_floor_heating",entity_id:"climate.room",unique_id:"room"}]:{},callService:()=>{throw Error("read-only dashboard issued a command");}};window.panel.hass=window.hass;});
  const panel=page.locator("adaptive-floor-heating-panel");
  await page.waitForFunction(()=>window.panel.registryStatus==="ready");
  assert.equal(await panel.locator(".completed-cycle").count(),0);
  assert.equal(await panel.locator("#dashboard .details").count(),0);
  assert.equal(await panel.getByRole("tab",{name:"학습 분석",exact:true}).count(),1);
  await page.evaluate(()=>window.panel.remove());assert.deepEqual(errors,[]);
  console.log("PASS: completed-cycle and dashboard learning-curve cards are removed; analysis tab remains.");
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1;});
