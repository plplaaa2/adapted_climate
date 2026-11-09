// Verify actual five-minute learned deltas and separate layers; related: curve_api.py, frontend/panel.js.
// Usage: node test_learning_frontend.cjs <path-to-playwright-module> [screenshot-path]
const {chromium}=require(process.argv[2]||"playwright");
const assert=require("node:assert/strict");const path=require("node:path");
(async()=>{
  const browser=await chromium.launch({channel:"msedge",headless:true});
  try{
    const page=await browser.newPage(),errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.setViewportSize({width:1024,height:1400});
    await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
    await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
    await page.evaluate(()=>{
      window.panel=document.querySelector("adaptive-floor-heating-panel");window.requests=[];window.pending=[];
      const point=(index,delta,samples=8,promotions=0)=>({index,minutes:(index+1)*5,delta,samples,promotions,confidence:.8,updated_at:1791000000});
      window.data={status:"ready",unit:"°C",bucket_minutes:5,curves:{
        WARM_HEATING:{current:[point(0,0),point(1,.1),point(2,.16),point(3,.13)],long_term:[point(0,.02,8,4),point(1,.08,8,4),point(2,.12,8,4)],current_confidence:.75,long_term_confidence:.8,accepted:18,rejected:2},
        PREDICTIVE_WARM_HEATING:{current:[],long_term:[],current_confidence:0,long_term_confidence:0,accepted:0,rejected:0},
        COOLING:{current:[],long_term:[point(0,-.05,8,3),point(1,-.1,8,3)],current_confidence:0,long_term_confidence:.9,accepted:8,rejected:0},
      }};
      window.serviceCalls=[];
      window.hass={config:{unit_system:{temperature:"°C"},time_zone:"Asia/Seoul"},connection:{connected:true,subscribeEvents:async()=>()=>{}},states:{
        "climate.living":{state:"auto",attributes:{friendly_name:"거실",current_temperature:22.6,temperature:23,heater_confirmed_on:false,heater_command_pending:null}},
        "climate.bedroom":{state:"off",attributes:{friendly_name:"침실",heater_confirmed_on:false,heater_command_pending:null}},
        "button.living_reset":{state:"unknown"},"button.renamed_bedroom_reset":{state:"unknown"},
      },callService:async (...args)=>{
        window.serviceCalls.push(args);
        if(window.rejectReset)throw Error("unauthorized");
        await new Promise(resolve=>window.completeReset=resolve);
      },callWS:async request=>{
        if(request.type==="config/entity_registry/list")return [
          {platform:"adaptive_floor_heating",entity_id:"climate.living",unique_id:"living",config_entry_id:"living-entry"},
          {platform:"adaptive_floor_heating",entity_id:"climate.bedroom",unique_id:"bedroom",config_entry_id:"bedroom-entry"},
          {platform:"adaptive_floor_heating",entity_id:"button.living_reset",unique_id:"living-entry_learning_reset",config_entry_id:"living-entry"},
          {platform:"adaptive_floor_heating",entity_id:"button.renamed_bedroom_reset",unique_id:"bedroom-entry_learning_reset",config_entry_id:"bedroom-entry"},
        ];
        if(request.type==="history/history_during_period")return {};
        if(request.type==="adaptive_floor_heating/curve_cycles")return {status:"ready",cycles:[],total:0};
        window.requests.push(request);
        if(window.failApi)throw Error("unauthorized");
        if(window.holdApi)return await new Promise(resolve=>window.pending.push(resolve));
        return structuredClone(window.data);
      }};window.panel.hass=window.hass;
    });
    const panel=page.locator("adaptive-floor-heating-panel");
    await page.waitForFunction(()=>window.panel.learningStatus==="ready");
    await panel.getByRole("tab",{name:"학습 분석",exact:true}).click();
    const card=panel.locator("#learning .analysis-curves");
    assert.deepEqual(await page.evaluate(()=>window.requests[0]),{type:"adaptive_floor_heating/curve_memory",entity_id:"climate.living"});
    assert.equal(await card.locator("[data-learning=counts]").count(),0);
    assert.match(await panel.locator(".analysis-summary").textContent(),/18 \/ 2/);
    assert.equal(await card.locator("[data-learning=current-confidence]").textContent(),"75%");
    assert.equal(await card.locator("[data-learning=long-confidence]").textContent(),"80%");
    assert.equal(await card.locator("[data-learning-series=current]").count(),1);
    assert.equal(await card.locator("[data-learning-point='current:0']").count(),1);
    if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
    assert.deepEqual(await card.locator(".learning-kind option").evaluateAll(options=>options.map(option=>option.value)),["WARM_HEATING","PREDICTIVE_WARM_HEATING","COOLING"]);
    await card.locator(".learning-kind").selectOption("COOLING");
    assert.equal(await card.locator("[data-learning-point^='current:']").count(),0);
    assert.equal(await card.locator("[data-learning-point^='long_term:']").count(),2);
    assert.match(await card.locator("svg").textContent(),/최고점부터/);
    await panel.getByRole("tab",{name:"학습 분석",exact:true}).click();
    const full=panel.locator("#learning .analysis-curves");
    assert.equal(await full.locator(".learning-kind").inputValue(),"COOLING");
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const rect=await full.locator("svg").boundingBox();
    await page.mouse.move(rect.x+rect.width*.8,rect.y+100);
    assert.match(await full.locator(".learning-detail").textContent(),/장기 학습 -0.100 °C/);
    assert.match(await full.locator(".learning-detail").textContent(),/장기 반영 3/);
    await page.evaluate(()=>{window.hass.config.unit_system.temperature="°F";window.panel.hass={...window.hass};});
    await page.waitForFunction(()=>window.panel.learningStatus==="ready");
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const fahrenheit=await full.locator("svg").boundingBox();
    await page.mouse.move(fahrenheit.x+fahrenheit.width*.9,fahrenheit.y+110);
    assert.match(await full.locator(".learning-detail").textContent(),/-0.180 °F/);
    await full.locator(".learning-kind").selectOption("PREDICTIVE_WARM_HEATING");
    assert.match(await full.locator(".learning-notice").textContent(),/5분 구간이 없습니다/);
    assert.equal(await full.locator("svg").count(),0);
    await full.locator(".learning-kind").selectOption("WARM_HEATING");
    for(const width of [1024,700,360,320]){
      await page.setViewportSize({width,height:1400});
      await page.waitForFunction(()=>{const chart=window.panel.shadowRoot.querySelector("#learning .learning-chart");return chart.querySelector("svg")?.viewBox.baseVal.width===chart.clientWidth;});
      assert.equal(await full.evaluate(element=>element.getBoundingClientRect().right<=innerWidth+1),true);
    }
    // Preserve learned SVG nodes during refresh; related: panel.js drawLearning.
    await page.evaluate(()=>{window.savedLearningPlot=window.panel.shadowRoot.querySelector("#learning .learning-chart svg");window.holdApi=true;});
    await panel.locator(".analysis-refresh").click();
    assert.equal(await page.evaluate(()=>window.savedLearningPlot!==null && window.savedLearningPlot===window.panel.shadowRoot.querySelector("#learning .learning-chart svg")),true);
    await panel.locator("#room").selectOption("bedroom");
    await page.waitForFunction(()=>window.pending.length===2);
    await page.evaluate(()=>{window.pending[1]({status:"unavailable",curves:{}});});
    await page.waitForFunction(()=>window.panel.learningStatus==="unavailable");
    await page.evaluate(()=>{window.pending[0](window.data);window.holdApi=false;});
    assert.equal(await full.locator("svg").count(),0);
    assert.match(await full.locator(".learning-notice").textContent(),/저장소를 사용할 수 없습니다/);
    await page.evaluate(()=>{window.failApi=true;});
    await panel.locator(".analysis-refresh").click();
    await page.waitForFunction(()=>window.panel.learningStatus==="error");
    assert.match(await full.locator(".learning-notice").textContent(),/조회할 수 없습니다/);
    await page.evaluate(()=>{window.failApi=false;});
    await panel.locator(".analysis-refresh").click();
    await page.waitForFunction(()=>window.panel.learningStatus==="ready");
    // The reset must target the selected entry and require an explicit confirmation.
    const reset=panel.getByRole("button",{name:"학습 리셋",exact:true});
    const dialog=panel.getByRole("dialog",{name:"학습을 리셋할까요?"});
    await reset.click();
    assert.match(await dialog.textContent(),/침실의 학습 모델/);
    assert.equal(await dialog.evaluate(element=>element.getBoundingClientRect().width<=innerWidth-16),true);
    await dialog.getByRole("button",{name:"취소",exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>window.serviceCalls),[]);
    await reset.click();
    await page.evaluate(()=>{window.hass.states["climate.bedroom"].attributes.heater_confirmed_on=true;window.panel.hass={...window.hass};});
    await dialog.waitFor({state:"hidden"});
    assert.equal(await reset.isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>window.serviceCalls),[]);
    await page.evaluate(()=>{window.hass.states["climate.bedroom"].attributes.heater_confirmed_on=false;window.rejectReset=true;window.panel.hass={...window.hass};});
    await reset.click();
    await dialog.getByRole("button",{name:"학습 리셋 실행"}).click();
    await page.waitForFunction(()=>window.panel.shadowRoot.querySelector(".learning-reset-status").textContent.includes("못했습니다"));
    await page.evaluate(()=>{window.rejectReset=false;});
    await reset.click();
    await dialog.getByRole("button",{name:"학습 리셋 실행"}).click();
    await page.waitForFunction(()=>typeof window.completeReset==="function");
    assert.equal(await reset.isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>window.serviceCalls),[
      ["button","press",{entity_id:"button.renamed_bedroom_reset"}],
      ["button","press",{entity_id:"button.renamed_bedroom_reset"}],
    ]);
    await page.evaluate(()=>{window.data={status:"ready",curves:{}};window.completeReset();});
    await page.waitForFunction(()=>window.panel.learningStatus==="ready" && window.panel.shadowRoot.querySelector(".learning-reset-status").textContent.includes("초기화했습니다"));
    assert.equal(await full.locator("svg").count(),0);
    await page.evaluate(()=>{window.hass.connection.connected=false;window.panel.hass={...window.hass};});
    assert.equal(await full.locator("svg").count(),0);
    await page.evaluate(()=>window.panel.remove());
    assert.deepEqual(errors,[]);
    console.log("PASS: learning charts, responsive layout, stale-room isolation, and confirmed per-entry reset with retry and refreshed data.");
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
