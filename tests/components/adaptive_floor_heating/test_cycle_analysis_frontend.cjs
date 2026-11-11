// Verify cycle investigation, immutable evidence, compressed history and stale reads; related: panel.js, curve_api.py.
// Usage: node test_cycle_analysis_frontend.cjs <playwright-module-path> [screenshot-path]
const {chromium}=require(process.argv[2]||"playwright");
const assert=require("node:assert/strict"),path=require("node:path");
(async()=>{
  const browser=await chromium.launch({channel:"msedge",headless:true});
  try{
    const page=await browser.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.setViewportSize({width:1280,height:1100});
    await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
    await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
    await page.evaluate(()=>{
      window.panel=document.querySelector("adaptive-floor-heating-panel");window.calls=[];window.pending=[];
      const before={diagnostics:{current_confidence:0,long_term_confidence:0,off_current_confidence:0,off_long_term_confidence:0},current:[]};
      const after={diagnostics:{current_confidence:.375,long_term_confidence:0,off_current_confidence:.3,off_long_term_confidence:0},current:[{index:0,delta:0,samples:3},{index:2,delta:.2,samples:3}]};
      // Preserve cycle-specific gates and render live blockers; related: panel.js, curve_memory.py.
      after.promotion={threshold:.7,effective_minimum_samples:6,minimum_prefix_buckets:2,stable_prefix_buckets:0,eligible_prefix:false,first_blocker:{index:0,reasons:["VARIABILITY"]},aggregate:{covered_buckets:3,weakest_index:2,reasons:["INSUFFICIENT_SAMPLES"]},buckets:[{index:0,samples:6,standard_deviation:.0428,score:.55,fresh_score:.58,reasons:["VARIABILITY"]}],response_groups:[{key:"2:0",samples:3,eligible:false,fields:{peak_minutes:{samples:3,score:.2,stable:false,reasons:["INSUFFICIENT_SAMPLES"]}}}]};
      const base={id:"field",curve_type:"WARM_HEATING",accepted:false,quality_reason:"INVALID_RESIDUAL_RISE",end_reason:"PEAK_CONFIRMED",start_reason:"THRESHOLD_START",off_reason:"TARGET_REACHED",preset:"home",mode:"HEAT",
        started_at:"2026-10-05T11:20:00+00:00",off_at:"2026-10-05T13:40:00+00:00",peak_at:"2026-10-05T15:00:00+00:00",ended_at:"2026-10-05T15:15:00+00:00",
        start_temperature:23.2,off_temperature:24,peak_temperature:29.4,slope_at_off:2.363,residual_rise:5.4,peak_delay_minutes:80,heating_duration_minutes:140,bucket_count:3,raw_status:"available",
        buckets:[{index:0,delta:0},{index:1,delta:.1},{index:3,delta:.2}],analysis:{checks:[
          {code:"INVALID_RESIDUAL_RISE",actual:5.4,min:0,max:5,unit:"°C",passed:false},
          {code:"INVALID_PEAK_DELAY",actual:80,min:0,max:360,unit:"minutes",passed:true},
        ],peak_confirmation:{minimum_drop_c:.2,minimum_extra_drop_c:.1,minimum_reports:2,maximum_wait_minutes:360,observed_drop_c:.3,observed_extra_drop_c:.1,observed_reports:2},learning:{applied:false,promoted:false,before,after:before}}};
      window.cycles=[base,{...base,id:"accepted",accepted:true,quality_reason:"ACCEPTED",residual_rise:1.4,peak_temperature:25.4,analysis:{checks:[{code:"INVALID_RESIDUAL_RISE",actual:1.4,min:0,max:5,unit:"°C",passed:true}],learning:{applied:true,promoted:true,before,after}}},
        {...base,id:"legacy",quality_reason:"INCOMPLETE_PEAK",end_reason:"PEAK_TIMEOUT",analysis:null,off_at:null,peak_at:null,peak_delay_minutes:null,start_temperature:null,off_temperature:null,peak_temperature:null,slope_at_off:null,residual_rise:null,bucket_count:null,buckets:[],raw_status:"unavailable"},
        {...base,id:"field",curve_type:"COOLING",accepted:true,quality_reason:"ACCEPTED",bucket_count:3,buckets:[],raw_status:"expired",residual_rise:null,analysis:{checks:[],learning:{applied:true,promoted:false,before,after}}},
        {...base,id:"cooling-rate",curve_type:"COOLING",accepted:false,quality_reason:"SUNRISE_SAVING",end_reason:"SUNRISE_COOLING_TARGET",target_peak_temperature:24,bucket_count:3,buckets:[{index:0,delta:-.05},{index:1,delta:-.1},{index:2,delta:-.15}],raw_status:"available",residual_rise:null,analysis:{sunrise_saving:true,checks:[],learning:{applied:false,promoted:false,before,after:before}}}];
      const curves={};for(const kind of ["WARM_HEATING","PREDICTIVE_WARM_HEATING","COOLING"])curves[kind]={accepted:kind==="WARM_HEATING"?1:0,rejected:kind==="WARM_HEATING"?2:0,current_confidence:0,long_term_confidence:0,current:[],long_term:[]};
      curves.WARM_HEATING.promotion=after.promotion;
      window.hass={config:{unit_system:{temperature:"°C"},time_zone:"Asia/Seoul"},connection:{connected:true,subscribeEvents:async()=>()=>{}},states:{"climate.living":{state:"auto",attributes:{friendly_name:"거실",current_temperature:25,temperature:24}},"climate.bedroom":{state:"off",attributes:{friendly_name:"침실"}}},
        callService:()=>{throw Error("analysis dispatched a control command");},callWS:async query=>{
          if(query.type==="config/entity_registry/list")return [{platform:"adaptive_floor_heating",entity_id:"climate.living",unique_id:"living"},{platform:"adaptive_floor_heating",entity_id:"climate.bedroom",unique_id:"bedroom"}];
          if(query.type==="history/history_during_period")return {};
          if(query.type==="adaptive_floor_heating/curve_memory")return {status:"ready",curves};
          window.calls.push(query);
          if(window.fail)throw Error("unauthorized");if(window.unavailable)return {status:"unavailable",cycles:[]};
          if(window.hold)return new Promise(resolve=>window.pending.push({query,resolve}));
          const rows=(window.exportCycles||window.cycles).filter(cycle=>(!query.curve_type||query.curve_type===cycle.curve_type)&&(query.accepted===undefined||query.accepted===cycle.accepted));
          const offset=query.offset||0;return {status:"ready",cycles:structuredClone(rows.slice(offset,offset+(query.limit||30))),total:rows.length,offset,snapshot:query.snapshot??250,raw_retention_days:7,cycle_retention_days:30};
        }};window.panel.hass=window.hass;
    });
    const panel=page.locator("adaptive-floor-heating-panel"),analysis=panel.locator("#learning");
    await page.waitForFunction(()=>window.panel.learningStatus==="ready");
    assert.equal(await page.evaluate(()=>window.calls.filter(query=>!query.heating_only).length),0,"full cycle reads are lazy outside analysis");
    await panel.getByRole("tab",{name:"학습 분석",exact:true}).click();
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready");
    assert.match(await panel.locator(".cycle-retention").innerText(),/회차·진단 최근 30일 · 원시 5분 관측 최근 7일 보관/);
    await page.evaluate(()=>{window.exportCycles=Array.from({length:205},(_,index)=>({...window.cycles[0],id:`export-${index}`}));window.exports=[];window.panel.downloadJsonFile=(data,name)=>window.exports.push({data,name});});
    await analysis.locator(".learning-download").click();
    await page.waitForFunction(()=>window.exports.length===1);
    assert.equal(await page.evaluate(()=>window.exports[0].data.schema),"adaptive_floor_heating.learning_export.v1");
    assert.equal(await page.evaluate(()=>window.exports[0].data.cycles.included),205);
    assert.equal(await page.evaluate(()=>window.exports[0].data.cycles.rows[204].id),"export-204");
    assert.match(await analysis.locator(".learning-download-status").textContent(),/205건/);
    await page.evaluate(()=>{window.exportCycles=window.cycles;});
    assert.equal(await analysis.locator(".analysis-stat").count(),3);
    assert.deepEqual(await analysis.locator(".analysis-kind option").evaluateAll(options=>options.map(option=>option.value)),["","WARM_HEATING","PREDICTIVE_WARM_HEATING","COOLING"]);
    assert.equal(await analysis.locator(".analysis-record").count(),5);
    assert.match(await analysis.locator(".analysis-stat").nth(0).textContent(),/1 \/ 2/);
    const detail=analysis.locator(".analysis-detail-body");
    assert.match(await detail.textContent(),/INVALID_RESIDUAL_RISE/);
    assert.equal(await detail.locator("details[open]").count(),0);
    assert.equal(await analysis.getByRole("button",{name:"다시 시도",exact:true}).count(),0);
    assert.match(await detail.textContent(),/5\.40 °C/);assert.match(await detail.textContent(),/0\.00 °C ~ 5\.00 °C/);
    assert.match(await detail.textContent(),/22:40/);assert.match(await detail.textContent(),/140\.00 분/);
    await detail.getByText("5분 관측 데이터",{exact:true}).click();
    assert.equal(await detail.locator(".analysis-bucket-chart path.actual").count(),1);
    assert.equal((await detail.locator(".analysis-bucket-chart path.actual").getAttribute("d")).split("M").length-1,2,"missing buckets are not connected");
    assert.match(await detail.textContent(),/반영하지 않았습니다/);
    await analysis.locator(".analysis-record").nth(1).click();
    assert.match(await detail.textContent(),/첫 차단: 0–5분 · 변동 기준 미충족/);
    assert.match(await detail.textContent(),/Peak 도달시간: 3회/);
    assert.match(await analysis.locator(".analysis-model-evidence").textContent(),/예측 정확도를 뜻하지 않습니다/);
    assert.match(await detail.textContent(),/장기 학습 반영됨/);assert.match(await detail.textContent(),/37\.5%/);
    await detail.getByText("학습 반영 결과",{exact:true}).click();
    await detail.getByText("최근 학습 5분 구간 반영 전후",{exact:true}).click();
    assert.match(await detail.textContent(),/증거 수/);
    await page.evaluate(()=>window.panel.drawCycleAnalysis());
    assert.equal(await detail.getByText("학습 반영 결과",{exact:true}).evaluate(element=>element.parentElement.open),true,"refresh preserves expanded evidence");
    await analysis.locator(".analysis-record").nth(2).click();
    assert.match(await detail.textContent(),/INCOMPLETE_PEAK/);assert.match(await detail.textContent(),/PEAK_TIMEOUT/);
    assert.match(await detail.textContent(),/구형 기록/);assert.equal(await detail.locator("svg").count(),0);
    await analysis.locator(".analysis-record").nth(3).click();
    assert.match(await detail.textContent(),/보존기간 7일/);assert.match(await detail.textContent(),/냉각/);
    await analysis.locator(".analysis-record").nth(4).click();
    assert.match(await detail.textContent(),/일출 절약 · 기록 전용/);
    assert.match(await detail.textContent(),/냉각 기록 종료 온도23\.50 °C/);
    assert.match(await detail.textContent(),/SUNRISE_COOLING_TARGET/);
    assert.match(await detail.textContent(),/최고점 이후 평균 변화율 \(저장 버킷 기준\)/);assert.match(await detail.textContent(),/-1\.20 °C\/h/);
    assert.match(await detail.textContent(),/최고점 이후 누적 변화량 \(저장 버킷 합계\)/);assert.match(await detail.textContent(),/-0\.30 °C/);
    await analysis.locator(".analysis-kind").selectOption("WARM_HEATING");
    await analysis.locator(".analysis-accepted").selectOption("false");
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready"&&window.panel.cyclesData.cycles.length===2);
    const query=await page.evaluate(()=>window.calls.at(-1));
    assert.equal(query.curve_type,"WARM_HEATING");assert.equal(query.accepted,false);
    await analysis.locator(".analysis-record").first().click();
    await page.evaluate(()=>{window.hass.config.unit_system.temperature="°F";window.panel.hass={...window.hass};});
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready");
    assert.match(await detail.textContent(),/75\.20 °F/);assert.match(await detail.textContent(),/9\.72 °F/);
    assert.match(await detail.textContent(),/0\.00 °F ~ 9\.00 °F/);
    assert.match(await detail.textContent(),/0\.36 °F/);assert.match(await detail.textContent(),/0\.54 °F/);assert.match(await detail.textContent(),/0\.18 °F/);
    await page.evaluate(()=>{window.hass.config.unit_system.temperature="°C";window.panel.hass={...window.hass};});
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready");
    await analysis.locator(".analysis-model-state").evaluate(element=>{element.open=true;});
    if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
    for(const width of [900,700,360,320]){
      await page.setViewportSize({width,height:1100});await page.waitForTimeout(120);
      assert.equal(await analysis.evaluate(element=>element.scrollWidth<=element.clientWidth+1),true,`overflow at ${width}`);
      assert.equal(await detail.evaluate(element=>element.closest(".analysis-detail").previousElementSibling?.getAttribute("aria-pressed")),"true","mobile detail follows selected record");
      assert.equal(await analysis.locator(".analysis-list").evaluate(element=>element.scrollHeight<=element.clientHeight+1),true,"mobile details are not clipped inside a scrolling list");
    }
    if(process.argv[3]){
      await page.evaluate(()=>{
        const colors={"--primary-text-color":"#e6e6e6","--secondary-text-color":"#a5abb4","--primary-background-color":"#111318","--card-background-color":"#1c2027","--secondary-background-color":"#293241","--divider-color":"#373d49","--primary-color":"#44b9ef","--warning-color":"#e9a743"};
        Object.entries(colors).forEach(([key,value])=>window.panel.style.setProperty(key,value));document.body.style.background="#111318";
      });
      await page.setViewportSize({width:360,height:1100});
      assert.equal(await detail.locator(".analysis-table td").first().evaluate(element=>getComputedStyle(element).color),"rgb(230, 230, 230)","dark-mode evidence remains readable");
      await page.screenshot({path:process.argv[3].replace(/\.png$/,"-mobile.png"),fullPage:true});
      await page.setViewportSize({width:1280,height:1100});
      await page.waitForTimeout(120);
      await page.screenshot({path:process.argv[3].replace(/\.png$/,"-dark.png"),fullPage:true});
    }
    const decoded=await page.evaluate(()=>window.panel.decodeHistory([
      {lu:1,s:"heat",a:{current_temperature:23,temperature:24,heater_confirmed_on:true}},
      {lu:2},{lu:3,s:"unavailable"},{lu:4,a:{current_temperature:24,temperature:24,heater_confirmed_on:true}},
      {lu:5,s:"heat"},{lu:6,a:{}},
    ],1000,6000));
    assert.deepEqual(decoded.map(row=>[row.time,row.heater,row.current]),[[1000,true,23],[3000,null,null],[5000,true,24],[6000,null,null]]);
    await page.evaluate(()=>{window.hold=true;});
    await page.evaluate(()=>window.panel.refreshVisibleLearning());
    await panel.locator("#room").selectOption("bedroom");
    await page.waitForFunction(()=>window.pending.length===2);
    await page.evaluate(()=>{window.pending[1].resolve({status:"ready",cycles:[],total:0});});
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready");
    await page.evaluate(()=>{window.pending[0].resolve({status:"ready",cycles:window.cycles,total:4});window.hold=false;});
    assert.equal(await analysis.locator(".analysis-record").count(),0);assert.match(await analysis.locator(".analysis-notice").textContent(),/없습니다/);
    await page.evaluate(()=>{window.fail=true;window.panel.refreshVisibleLearning();});
    await page.waitForFunction(()=>window.panel.cyclesStatus==="error");assert.match(await analysis.locator(".analysis-notice").textContent(),/실패/);
    await page.evaluate(()=>{window.fail=false;window.unavailable=true;});await analysis.locator(".analysis-refresh").click();
    await page.waitForFunction(()=>window.panel.cyclesStatus==="unavailable");
    await page.evaluate(()=>{window.unavailable=false;});await analysis.locator(".analysis-refresh").click();
    await page.waitForFunction(()=>window.panel.cyclesStatus==="ready");
    await page.evaluate(()=>{window.hass.connection.connected=false;window.panel.hass={...window.hass};});
    assert.equal(await analysis.locator(".analysis-record").count(),0);assert.equal(await detail.locator("table").count(),0);
    assert.deepEqual(errors,[]);
    console.log("PASS: cycle summary/list, quality measurements/limits, timestamp timezone, accepted before/after/promotion, legacy omissions, raw expiry, filters, gap-safe buckets, Fahrenheit, compressed state/attribute carry-forward, mobile layout, stale room response, failure/retry/unavailable/disconnect and no control commands.");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
