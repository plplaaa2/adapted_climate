// Verify HA history decoding, gaps, selection races and responsive plots; related: frontend/panel.js.
// Usage: node test_history_frontend.cjs <path-to-playwright-module> [screenshot-path]
const {chromium} = require(process.argv[2] || "playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
(async () => {
  const browser = await chromium.launch({channel:"msedge",headless:true});
  try {
    const page = await browser.newPage();
    const errors = [];page.on("pageerror",error => errors.push(error.message));
    await page.setViewportSize({width:1024,height:1300});
    await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
    await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
    await page.evaluate(() => {
      window.panel=document.querySelector("adaptive-floor-heating-panel");
      window.calls=[];window.pending=[];
      window.registry=[{platform:"adaptive_floor_heating",entity_id:"climate.living",unique_id:"living"},{platform:"adaptive_floor_heating",entity_id:"climate.bedroom",unique_id:"bedroom"}];
      const state={state:"auto",attributes:{current_temperature:22.6,temperature:23,heater_confirmed_on:false,heater_command_pending:null,faults:[]}};
      window.hass={config:{unit_system:{temperature:"°C"},time_zone:"Asia/Seoul"},connection:{connected:true,subscribeEvents:async callback => {window.registryEvent=callback;return ()=>{};}},states:{"climate.living":state,"climate.bedroom":state},callService:()=>{throw Error("history must not control heating");},callWS:async command => {
        if(command.type==="config/entity_registry/list")return window.registry;
        window.calls.push(command);
        if(window.failHistory)throw Error("unavailable API");
        if(window.holdHistory)return await new Promise(resolve=>window.pending.push({command,resolve}));
        if(window.emptyHistory)return {};
        const t=Date.parse(command.start_time)/1000;
        return {[command.entity_ids[0]]:[
          {s:"auto",lu:t,a:{current_temperature:21.8,temperature:23,heater_confirmed_on:false}},
          {s:"heat",lu:t+3600,a:{current_temperature:22,temperature:23,heater_confirmed_on:true}},
          {s:"auto",lu:t+7200,a:{current_temperature:22.5,temperature:23,heater_confirmed_on:false}},
          {s:"unavailable",lu:t+10800,a:{current_temperature:99,temperature:99,heater_confirmed_on:true}},
          {s:"auto",lu:t+14400,a:{current_temperature:22.8,temperature:23,heater_confirmed_on:false}},
          {s:"auto",lu:t+18000,a:{current_temperature:22.6,temperature:23,heater_confirmed_on:false}},
        ]};
      }};
      window.panel.hass=window.hass;
    });
    const panel=page.locator("adaptive-floor-heating-panel");
    const graph=panel.locator("#dashboard .graph");
    await page.waitForFunction(()=>window.panel.historyStatus==="ready");
    const command=await page.evaluate(()=>window.calls[0]);
    assert.equal(command.type,"history/history_during_period");
    assert.deepEqual(command.entity_ids,["climate.living"]);
    assert.equal(command.significant_changes_only,false);assert.equal(command.no_attributes,false);assert.equal(command.minimal_response,false);
    assert.equal(Date.parse(command.end_time)-Date.parse(command.start_time),6*3600000);
    assert.equal(await graph.locator("svg [data-heater=true]").count(),1);
    assert.equal(await graph.locator("svg [data-heater=unknown]").count(),1);
    const trace=await graph.locator("[data-series=current]").getAttribute("d");
    assert.equal((trace.match(/M/g)||[]).length,2);
    assert.equal(await page.evaluate(()=>window.panel.historyRows.some(row=>row.current===99)),false);
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const bounds=await graph.locator("svg").boundingBox();
    await page.mouse.move(bounds.x+bounds.width*.5,bounds.y+80);
    assert.match(await graph.locator(".graph-detail").textContent(),/실내/);
    if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
    await graph.getByRole("combobox",{name:"그래프 기간"}).selectOption("24");
    await page.waitForFunction(()=>window.panel.historyStatus==="ready");
    assert.equal(await page.evaluate(()=>{const c=window.calls.at(-1);return Date.parse(c.end_time)-Date.parse(c.start_time);}),24*3600000);
    await panel.getByRole("tab",{name:"운전 기록",exact:true}).click();
    assert.equal(await panel.locator("#history svg").count(),1);
    assert.equal(await panel.locator("#history .history-period").inputValue(),"24");
    await page.evaluate(()=>{window.holdHistory=true;});
    await panel.locator("#history .history-refresh").click();
    await panel.locator("#room").selectOption("bedroom");
    await page.waitForFunction(()=>window.pending.length===2);
    await page.evaluate(()=>{
      const request=window.pending[1];const t=Date.parse(request.command.start_time)/1000;
      request.resolve({"climate.bedroom":[{s:"auto",lu:t,a:{current_temperature:19,temperature:20,heater_confirmed_on:false}}]});
    });
    await page.waitForFunction(()=>window.panel.historyStatus==="ready");
    await page.evaluate(()=>{const request=window.pending[0];const t=Date.parse(request.command.start_time)/1000;request.resolve({"climate.living":[{s:"auto",lu:t,a:{current_temperature:28,temperature:29,heater_confirmed_on:true}}]});window.holdHistory=false;});
    assert.equal(await page.evaluate(()=>window.panel.historyRows[0].current),19);
    for(const width of [1024,700,360,320]){
      await page.setViewportSize({width,height:1300});
      await page.waitForFunction(()=>{const c=window.panel.shadowRoot.querySelector("#history .history-chart");return c.querySelector("svg")?.viewBox.baseVal.width===c.clientWidth;});
      assert.equal(await panel.evaluate(element=>[...element.shadowRoot.querySelectorAll("#history .box,#history svg")].every(item=>item.getBoundingClientRect().right<=innerWidth+1)),true);
    }
    await page.evaluate(()=>{window.emptyHistory=true;});
    await panel.locator("#history .history-refresh").click();
    await page.waitForFunction(()=>window.panel.historyStatus==="empty");
    assert.equal(await panel.locator("#history svg").count(),0);
    await page.evaluate(()=>{window.failHistory=true;});
    await panel.locator("#history .history-refresh").click();
    await page.waitForFunction(()=>window.panel.historyStatus==="error");
    assert.match(await panel.locator("#history .history-notice").textContent(),/조회할 수 없습니다/);
    await page.evaluate(()=>{window.emptyHistory=false;window.failHistory=false;});
    await panel.locator("#history .history-refresh").click();
    await page.waitForFunction(()=>window.panel.historyStatus==="ready");
    assert.equal(await page.evaluate(()=>{
      const rows=window.panel.decodeHistory([
        {state:"auto",last_updated:new Date(500).toISOString(),attributes:{current_temperature:21,temperature:22,heater_confirmed_on:false}},
        {state:"auto",last_updated:new Date(1500).toISOString(),attributes:{current_temperature:21,temperature:22,heater_confirmed_on:false}},
        {state:"auto",last_updated:new Date(2000).toISOString(),attributes:{current_temperature:null,temperature:NaN,heater_confirmed_on:null}},
      ],1000,3000);return rows.length===2&&rows[0].time===1000&&rows[1].current===null;
    }),true);
    await page.evaluate(()=>{window.hass.connection.connected=false;window.panel.hass={...window.hass};});
    assert.equal(await panel.locator("#history svg").count(),0);
    await page.evaluate(()=>window.panel.remove());
    assert.deepEqual(errors,[]);
    console.log("PASS: full-attribute query, compressed/ordinary history, seed and dedup, unavailable gaps, ON/unknown bands, pointer details, 6/24-hour periods, shared history tab, stale-response race, responsive SVG, empty/error/retry and disconnect cleanup.");
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
