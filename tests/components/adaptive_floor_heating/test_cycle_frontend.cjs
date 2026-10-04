// Verify completed-cycle identity and diagnostic units without commands; related: frontend/panel.js, runtime.py.
// Usage: node test_cycle_frontend.cjs <path-to-playwright-module> [screenshot-path]
const {chromium} = require(process.argv[2] || "playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
(async () => {
  const browser=await chromium.launch({channel:"msedge",headless:true});
  try {
    const page=await browser.newPage();const errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.setViewportSize({width:1024,height:1300});
    await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
    await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
    await page.evaluate(()=>{
      window.panel=document.querySelector("adaptive-floor-heating-panel");
      const off=1791068400;
      window.comparison={off_at:off,peak_at:off+3000,completed_at:off+3600,actual_peak:23.3,predictions:{existing:23.5,curve:23.2},errors:{existing:.2,curve:-.1}};
      window.forecast={off_at:off,predictions:{existing:23.5,curve:23.2},confidences:{existing:.8,curve:.76},selected_model:"curve"};
      window.hass={config:{unit_system:{temperature:"°C"},time_zone:"Asia/Seoul"},connection:{connected:true,subscribeEvents:async()=>()=>{}},
        states:{"climate.living":{state:"auto",attributes:{friendly_name:"거실",current_temperature:22.6,temperature:23,last_peak_comparison:window.comparison,last_off_prediction:window.forecast}},"climate.bedroom":{state:"off",attributes:{friendly_name:"침실"}}},
        callWS:async msg=>msg.type==="config/entity_registry/list"?[{platform:"adaptive_floor_heating",entity_id:"climate.living",unique_id:"living"},{platform:"adaptive_floor_heating",entity_id:"climate.bedroom",unique_id:"bedroom"}]:{},
        callService:()=>{throw Error("Cycle display dispatched a command");}};
      window.panel.hass=window.hass;
    });
    const panel=page.locator("adaptive-floor-heating-panel"),card=panel.locator(".completed-cycle");
    const cell=key=>card.locator(`[data-cycle="${key}"]`).textContent();
    await page.waitForFunction(()=>window.panel.registryStatus==="ready");
    assert.equal(await card.locator(".cycle-actual").textContent(),"23.3 °C");
    assert.equal(await cell("existing-prediction"),"23.5 °C");
    assert.equal(await cell("existing-error"),"+0.2 °C");
    assert.equal(await cell("curve-error"),"-0.1 °C");
    assert.equal(await cell("existing-confidence"),"80%");
    assert.equal(await cell("curve-confidence"),"76%");
    const expected=await page.evaluate(()=>new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(window.comparison.off_at*1000)));
    assert.equal(await card.locator("[data-cycle-time=off_at]").textContent(),expected);
    if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
    await page.evaluate(()=>{window.forecast.off_at+=86400;window.forecast.predictions={existing:29,curve:30};window.panel.hass={...window.hass};});
    assert.equal(await cell("existing-prediction"),"23.5 °C");
    assert.equal(await cell("existing-error"),"+0.2 °C");
    assert.equal(await cell("existing-confidence"),"—");
    await page.evaluate(()=>{window.hass.config.unit_system.temperature="°F";window.panel.hass={...window.hass};});
    assert.equal(await card.locator(".cycle-actual").textContent(),"73.9 °F");
    assert.equal(await cell("existing-error"),"+0.4 °F");
    assert.equal(await cell("curve-error"),"-0.2 °F");
    await page.evaluate(()=>{window.hass.config.unit_system.temperature="°C";window.hass.states["climate.living"].attributes.last_peak_comparison={actual_peak:23.3,predictions:{},errors:{}};window.panel.hass={...window.hass};});
    assert.equal(await card.locator(".cycle-actual").textContent(),"23.3 °C");
    assert.equal(await cell("curve-prediction"),"—");
    assert.equal(await cell("curve-error"),"—");
    assert.equal(await card.locator("[data-cycle-time=off_at]").textContent(),"기록 없음");
    await page.evaluate(()=>{window.hass.states["climate.living"].attributes.last_peak_comparison={actual_peak:0,predictions:{existing:0,curve:NaN},errors:{existing:0,curve:0},off_at:Infinity,peak_at:true};window.panel.hass={...window.hass};});
    assert.equal(await card.locator(".cycle-actual").textContent(),"0.0 °C");
    assert.equal(await cell("existing-error"),"0.0 °C");
    assert.equal(await cell("curve-error"),"—");
    await page.evaluate(()=>{window.hass.states["climate.living"].state="unavailable";window.panel.hass={...window.hass};});
    assert.match(await card.locator(".cycle-status").textContent(),/과거 완료/);
    await panel.locator("#room").selectOption("bedroom");
    assert.equal(await card.locator(".cycle-result").isVisible(),false);
    assert.match(await card.locator(".cycle-status").textContent(),/기록이 없습니다/);
    await panel.locator("#room").selectOption("living");
    await page.evaluate(()=>{window.hass.states["climate.living"].state="auto";window.hass.states["climate.living"].attributes.last_peak_comparison=window.comparison;window.panel.hass={...window.hass};});
    for(const width of [1024,700,360,320]){
      await page.setViewportSize({width,height:1300});
      assert.equal(await card.evaluate(element=>[...element.querySelectorAll("td,th,dd")].every(cell=>cell.getBoundingClientRect().right<=innerWidth+1)),true);
    }
    for(const invalid of [null,true,[],{actual_peak:null},{actual_peak:Infinity}]){
      await page.evaluate(value=>{window.hass.states["climate.living"].attributes.last_peak_comparison=value;window.panel.hass={...window.hass};},invalid);
      assert.equal(await card.locator(".cycle-result").isVisible(),false);
    }
    await page.evaluate(()=>window.panel.remove());
    assert.deepEqual(errors,[]);
    console.log("PASS: completed snapshot predictions/errors, matching-cycle confidence, newer OFF isolation, Celsius/Fahrenheit absolute/delta conversion, missing forecasts, legacy metadata, zero/invalid values, historical unavailable display, room switch and responsive table.");
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
