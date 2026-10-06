// Verify room-owned sensor values and numeric Recorder history; related: frontend/panel.js, sensor.py.
const {chromium}=require(process.argv[2]||"playwright");
const assert=require("node:assert/strict"),path=require("node:path");
(async()=>{const browser=await chromium.launch({channel:"msedge",headless:true});try{
  const page=await browser.newPage();const errors=[];page.on("pageerror",e=>errors.push(e.message));
  await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
  await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
  await page.evaluate(()=>{
    window.panel=document.querySelector("adaptive-floor-heating-panel");window.calls=[];
    const sensor=(id,key,extra={})=>({platform:"adaptive_floor_heating",entity_id:`sensor.${id}`,unique_id:`room_${key}`,config_entry_id:"room",...extra});
    window.registry=[{platform:"adaptive_floor_heating",entity_id:"climate.room",unique_id:"room_climate",config_entry_id:"room"},sensor("slope","temperature_slope"),sensor("delay","learned_heating_response_delay"),sensor("empty","learned_residual_rise"),sensor("enum","observation_phase"),sensor("hidden","hidden",{hidden_by:"user"}),sensor("other","temperature_slope",{config_entry_id:"other",unique_id:"other_temperature_slope"})];
    const state=(value,unit,extra={})=>({state:value,attributes:{unit_of_measurement:unit,...extra}});
    window.hass={connection:{connected:true,subscribeEvents:async()=>()=>{}},config:{time_zone:"Asia/Seoul",unit_system:{temperature:"°F"}},states:{"climate.room":{state:"auto",attributes:{temperature:73.4,current_temperature:72,min_temp:64,max_temp:86,target_temp_step:.1,supported_features:17,hvac_modes:["off","heat","auto"]}},"sensor.slope":state("0","°C/h"),"sensor.delay":state("5","min"),"sensor.empty":state("unknown","°F"),"sensor.enum":state("idle",null,{device_class:"enum"}),"sensor.hidden":state("1","min"),"sensor.other":state("9","°C/h")},callWS:async cmd=>{
      if(cmd.type==="config/entity_registry/list")return window.registry;
      if(cmd.type!=="history/history_during_period")return {status:"unavailable",curves:{}};
      if(!cmd.entity_ids.some(id=>id.startsWith("sensor.")))return {};
      window.calls.push(cmd);if(window.rejectHistory)throw Error("denied");
      const start=Date.parse(cmd.start_time)/1000,end=Date.parse(cmd.end_time)/1000;
      const result={"sensor.slope":[{lu:start-1,s:"0",a:{unit_of_measurement:"°C/h"}},{lu:start+1800,s:"unknown",a:{unit_of_measurement:"°C/h"}},{lu:start+3600,s:"-.2",a:{unit_of_measurement:"°C/h"}},{lu:end+1,s:"99",a:{unit_of_measurement:"°C/h"}}],"sensor.delay":[{last_updated:new Date(start*1000).toISOString(),state:"5",attributes:{unit_of_measurement:"min"}}],"sensor.empty":[]};
      if(window.holdHistory)await new Promise(resolve=>window.finishHistory=resolve);return result;
    }};window.panel.hass=window.hass;
  });
  const panel=page.locator("adaptive-floor-heating-panel"),choice=panel.getByRole("combobox",{name:"진단 센서 선택",exact:true});
  await page.waitForFunction(()=>window.panel.sensorStatus==="ready");
  assert.deepEqual(await choice.locator("option").allTextContents(),["실내 온도 변화율","학습 반응 지연"]);
  assert.match(await panel.locator(".sensor-values").textContent(),/0 °C\/h/);
  assert.match(await panel.locator(".sensor-values").textContent(),/5 min/);
  assert.equal(await page.evaluate(()=>window.calls[0].entity_ids.includes("sensor.other")),false);
  assert.equal(await page.evaluate(()=>window.calls[0].entity_ids.includes("sensor.enum")),false);
  assert.equal(await page.evaluate(()=>window.calls[0].no_attributes),false);
  assert.equal(await page.evaluate(()=>window.panel.sensorRows[0].value),0);
  assert.equal((await panel.locator("[data-sensor-series]").getAttribute("d")).match(/M/g).length,2);
  await choice.selectOption("room_learned_heating_response_delay");await page.waitForFunction(()=>window.panel.sensorStatus==="ready");
  assert.match(await panel.locator(".sensor-chart svg").getAttribute("aria-label"),/min/);
  await panel.getByRole("combobox",{name:"센서 그래프 기간",exact:true}).selectOption("24");await page.waitForFunction(()=>window.panel.sensorStatus==="ready");
  assert.equal(await page.evaluate(()=>{const c=window.calls.at(-1);return Date.parse(c.end_time)-Date.parse(c.start_time);}),24*3600000);
  await page.evaluate(()=>{window.rejectHistory=true;});await panel.locator(".sensor-refresh").click();await page.waitForFunction(()=>window.panel.sensorStatus==="error");
  assert.match(await panel.locator(".sensor-notice").textContent(),/권한/);
  await page.evaluate(()=>{window.rejectHistory=false;});await panel.locator(".sensor-refresh").click();await page.waitForFunction(()=>window.panel.sensorStatus==="ready");
  // Preserve sensor SVG nodes during refresh; related: panel.js drawSensorHistory.
  await page.evaluate(()=>{window.savedSensorPlot=window.panel.shadowRoot.querySelector(".sensor-chart svg");window.holdHistory=true;});await panel.locator(".sensor-refresh").click();await page.waitForFunction(()=>Boolean(window.finishHistory));
  assert.equal(await page.evaluate(()=>window.savedSensorPlot!==null && window.savedSensorPlot===window.panel.shadowRoot.querySelector(".sensor-chart svg")),true);
  await page.evaluate(()=>{window.hass.connection.connected=false;window.panel.hass={...window.hass};window.holdHistory=false;window.finishHistory();});
  await page.waitForFunction(()=>window.panel.sensorKey===null);assert.equal(await panel.locator(".sensor-chart svg").count(),0);
  await page.evaluate(()=>{window.hass.connection.connected=true;window.panel.hass={...window.hass};});await page.waitForFunction(()=>window.panel.sensorStatus==="ready");
  for(const width of [1024,700,360,320]){await page.setViewportSize({width,height:1200});assert.equal(await panel.evaluate(el=>[...el.shadowRoot.querySelectorAll(".box")].filter(e=>e.getClientRects().length).every(e=>e.getBoundingClientRect().right<=innerWidth+1)),true);}
  if(process.argv[3]){await page.setViewportSize({width:1024,height:1200});await page.screenshot({path:process.argv[3],fullPage:true});}
  assert.deepEqual(errors,[]);console.log("PASS sensor identity/values/units, numeric history-only options, zero/gaps/seed, periods, errors/retry, stale disconnect, reconnect and responsive layout.");
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
