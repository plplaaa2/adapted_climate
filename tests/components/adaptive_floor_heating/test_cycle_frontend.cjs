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
  // Query five self-stored rounds and separate all-time accuracy without Recorder dependency.
  await page.evaluate(()=>{
    window.historyQueries=[];
    const original=window.hass.callWS;
    window.hass.callWS=async request=>{
      if(request.type!=="adaptive_floor_heating/curve_cycles")return original(request);
      window.historyQueries.push(request);
      if(window.rejectPredictions)throw Error("storage offline");
      if(window.holdPredictions)return await new Promise(resolve=>window.releasePredictions=resolve);
      return {status:"ready",accuracy:{count:123,mean_absolute_error:.25},accuracy_by_model:{hybrid:{count:2,mean_absolute_error:.3}},
        hybrid_summary_accuracy:{hybrid_paired:{count:1,mean_absolute_error:.3},hybrid_summary:{count:1,mean_absolute_error:.1}},cycles:Array.from({length:5},(_,i)=>({
        id:String(i),curve_type:"WARM_HEATING",accepted:true,started_at:"2026-11-08T00:00:00+00:00",
        off_at:"2026-11-08T01:00:00+00:00",ended_at:"2026-11-08T02:00:00+00:00",peak_at:"2026-11-08T01:30:00+00:00",
        heating_duration_minutes:60,peak_temperature:23.2,
        analysis:i?{}:{prediction_comparison:{off_model:"hybrid",actual_peak:23.2,hybrid_summary_peak:23.3,predictions:{curve:23.5}}}
      }))};
    };
    window.panel.selectTab("prediction");
  });
  await page.waitForFunction(()=>window.panel.predictionHistoryStatus==="ready");
  assert.equal(await panel.locator(".prediction-history-rows details").count(),5);
  assert.deepEqual(await page.evaluate(()=>window.historyQueries[0]),{type:"adaptive_floor_heating/curve_cycles",entity_id:"climate.room",limit:5,heating_only:true});
  assert.match(await panel.locator('[data-accuracy="count"]').innerText(),/123회/);
  assert.match(await panel.locator('[data-accuracy="mae"]').innerText(),/0.25 °C/);
  assert.equal(await panel.locator('dd[data-model-accuracy="hybrid"]').textContent(),"0.30 °C · 2회");
  assert.match(await panel.locator('dd[data-model-accuracy="hybrid_summary"]').textContent(),/기존 0.30 · 요약 학습 0.10 °C · 1회/);
  assert.match(await panel.locator(".prediction-history-rows summary").first().innerText(),/예상 23.5 °C → 실제 23.2 °C · 오차 \+0.30 °C/);
  assert.match(await panel.locator(".prediction-history-rows summary").nth(1).innerText(),/예상 기록 없음/);
  assert.equal(await panel.locator("#prediction > article:last-child").getAttribute("class"),"box prediction-comparison");
  assert.equal(await panel.locator('[data-prediction="curve-peak"]').count(),0);
  await panel.locator(".prediction-history-rows summary").first().click();
  assert.equal(await panel.locator(".prediction-history-rows details").first().getAttribute("open"),"");
  assert.match(await panel.locator(".prediction-history-rows details").first().innerText(),/가동합산 모델/);
  assert.match(await panel.locator(".prediction-history-rows details").first().innerText(),/가동합산 요약 학습 예상 최고점\s+23.3 °C/);
  await page.evaluate(()=>{window.hass.config.unit_system.temperature="°F";window.panel.hass={...window.hass};});
  assert.match(await panel.locator('[data-accuracy="mae"]').innerText(),/0.45 °F/);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.querySelector("adaptive-floor-heating-panel").shadowRoot.querySelector("main").scrollWidth<=390),true);
  if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
  await page.evaluate(async()=>{
    window.holdPredictions=true;
    window.panel.loadPredictionHistory();
  });
  await page.waitForFunction(()=>!!window.releasePredictions);
  await page.evaluate(()=>{
    const releaseOldRoom=window.releasePredictions;
    window.holdPredictions=false;
    window.panel.registry.push({entity_id:"climate.other",unique_id:"other"});
    window.hass.states["climate.other"]={state:"auto",attributes:{}};
    window.panel.selectedEntry="other";
    window.panel.syncPredictionHistory();
    releaseOldRoom({status:"ready",cycles:[{id:"wrong-room"}]});
  });
  await page.waitForFunction(()=>window.panel.predictionHistoryStatus==="ready");
  assert.equal(await page.evaluate(()=>window.panel.predictionHistory.cycles.some(cycle=>cycle.id==="wrong-room")),false);
  assert.equal(await page.evaluate(()=>window.historyQueries.at(-1).entity_id),"climate.other");
  await page.evaluate(async()=>{window.rejectPredictions=true;await window.panel.loadPredictionHistory();});
  assert.match(await panel.locator(".prediction-comparison-status").innerText(),/조회하지 못했습니다/);
  assert.equal(await panel.locator(".prediction-history-rows details").count(),0);
  await page.evaluate(()=>{window.rejectPredictions=false;window.holdPredictions=true;window.releasePredictions=null;window.panel.loadPredictionHistory();});
  await page.waitForFunction(()=>!!window.releasePredictions);
  await page.evaluate(()=>{window.panel.remove();window.releasePredictions({status:"ready",cycles:[{id:"late"}]});});
  await page.waitForTimeout(50);
  assert.equal(await page.evaluate(()=>window.panel.predictionHistory),null);
  assert.deepEqual(errors,[]);
  console.log("PASS: five self-stored prediction rounds, independent all-time MAE, missing forecasts, units, mobile layout, read failures and disconnected response isolation.");
}finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1;});
