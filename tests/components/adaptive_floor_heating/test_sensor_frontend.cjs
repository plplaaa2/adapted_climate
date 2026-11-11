// Verify phase-only state sensor presentation; related: panel.js and sensor.py.
const {chromium}=require(process.argv[2]||"playwright");
const assert=require("node:assert/strict"),path=require("node:path");
(async()=>{const browser=await chromium.launch({channel:"msedge",headless:true});try{
  const page=await browser.newPage(),errors=[];page.on("pageerror",e=>errors.push(e.message));
  await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
  await page.addScriptTag({path:path.resolve(__dirname,"../../../custom_components/adaptive_floor_heating/frontend/panel.js")});
  await page.evaluate(()=>{
    window.panel=document.querySelector("adaptive-floor-heating-panel");
    const sensor=(key,id)=>({platform:"adaptive_floor_heating",entity_id:`sensor.${id}`,unique_id:`room_${key}`,config_entry_id:"room"});
    window.registry=[
      {platform:"adaptive_floor_heating",entity_id:"climate.room",unique_id:"room_climate",config_entry_id:"room"},
      sensor("initial_learning_status","initial"),sensor("learning_status","learning"),
      sensor("observation_status","observation"),sensor("prediction_status","prediction"),
      {platform:"adaptive_floor_heating",entity_id:"select.policy",unique_id:"room_prediction_mode",config_entry_id:"room"},
    ];
    const enumState=value=>({state:value,attributes:{device_class:"enum"}});
    window.hass={connection:{connected:true,subscribeEvents:async()=>()=>{}},config:{unit_system:{temperature:"°C"}},
      states:{"climate.room":{state:"auto",attributes:{temperature:23,current_temperature:22.4,min_temp:18,max_temp:30,target_temp_step:.1,supported_features:17,hvac_modes:["off","heat","auto"],preset_modes:["home","away"],preset_mode:"home",heater_confirmed_on:true,heater_command_pending:false,control_state:"HEATING",faults:[],off_prediction:{model:"curve",predicted_peak:24.1,confidence:.72,generated_at:1791000000}}},
        "sensor.initial":enumState("not_configured"),"sensor.learning":enumState("heating"),"sensor.observation":enumState("heating_response"),"sensor.prediction":enumState("ready"),
        "select.policy":{state:"balanced",attributes:{options:["eco","balanced","comfort"]}}},
      callWS:async command=>command.type==="config/entity_registry/list"?window.registry:{status:"unavailable",curves:{}},
      callService:()=>{throw Error("read-only panel issued a command");}};
    window.panel.hass=window.hass;
    window.hass.states["climate.room"].attributes.learning_summary={normal_heating_cycles:6,cooling_segments:6,excluded_heating_records:12,base_samples:6,base_minimum:3,outdoor_samples:2,outdoor_minimum:4,outdoor_range:[-5,10],reaction_samples:6,reaction_minimum:4,comparison_count:2,mean_absolute_error:.3,last_normal_learning_at:1791000000,last_quality_reason:"INCOMPLETE_PEAK"};
    window.panel.hass={...window.hass};
  });
  const panel=page.locator("adaptive-floor-heating-panel");
  await page.waitForFunction(()=>window.panel.registryStatus==="ready");
  await panel.getByRole("tab",{name:"예측 분석",exact:true}).click();
  assert.equal(await panel.locator("#dashboard [data-value=initialLearning]").count(),0);
  assert.equal(await panel.locator("#dashboard [data-value=learning]").textContent(),"난방 곡선 관측 중");
  assert.equal(await panel.locator("#dashboard [data-value=observation]").count(),0);
  assert.equal(await panel.locator("#dashboard [data-value=predictionStatus]").count(),0);
  assert.equal(await panel.locator("#prediction [data-prediction=status]").textContent(),"예측 가능");
  assert.equal(await panel.locator("#prediction [data-prediction=initial-learning]").count(),0);
  assert.equal(await panel.locator("#prediction [data-prediction=learning]").count(),0);
  assert.equal(await panel.locator("#prediction [data-prediction=observation]").textContent(),"가동 중 반응 관측");
  assert.equal(await panel.getByRole("combobox",{name:"AUTO 학습 모델",exact:true}).count(),0);
  assert.equal(await panel.locator(".sensor-choice").count(),0);
  assert.equal(await panel.locator(".sensor-period").count(),1);
  assert.equal(await panel.locator("[data-sensor-phase=observation]").count(),0);
  assert.equal(await panel.locator("[data-sensor-phase=initial-learning]").count(),0);
  assert.equal(await panel.locator(".sensor-roles>span").count(),0);
  assert.equal(await panel.locator("[data-sensor-phase=learning]").count(),0);
  assert.equal(await panel.locator("[data-sensor-phase=prediction]").count(),0);
  assert.equal(await panel.locator('input[role="switch"][aria-label="예측 ON"]').count(),1);
  assert.equal(await panel.locator('[data-learning-summary="accepted"]').textContent(),"난방 6회 · 냉각 6구간");
  assert.equal(await panel.locator('[data-learning-summary="outdoor-range"]').textContent(),"-5.0 ~ 10.0 °C");
  assert.equal(await panel.locator('[data-learning-summary="validation"]').textContent(),"2회 · 평균 절대오차 0.30 °C");
  assert.match(await panel.locator('dt:has(+ [data-learning-summary="validation"])').textContent(),/최근 보정 비교/);
  assert.equal(await panel.locator(".sensor-roles>span.is-active").count(),0);
  assert.equal(await panel.locator('.sensor-roles>span[aria-current="step"]').count(),0);
  await page.evaluate(()=>{window.hass.states["sensor.observation"].state="idle";window.hass.states["sensor.learning"].state="applied";window.hass.states["sensor.prediction"].state="completed";window.panel.hass={...window.hass};});
  assert.equal(await panel.locator(".sensor-roles>span.is-active").count(),0);
  assert.equal(await panel.locator("#prediction [data-prediction=status]").textContent(),"관측 완료");
  assert.equal(await panel.locator("#dashboard [data-value=learning]").textContent(),"학습 반영 완료");
  await page.evaluate(()=>{window.hass.states["sensor.observation"].state="fault";window.hass.states["sensor.learning"].state="storage_error";window.panel.hass={...window.hass};});
  assert.equal(await panel.locator("#prediction [data-prediction=observation]").textContent(),"오류");
  assert.equal(await panel.locator("#dashboard [data-value=learning]").textContent(),"학습 저장 오류");
  await page.evaluate(()=>{window.hass.connection.connected=false;window.panel.hass={...window.hass};});
  assert.equal(await panel.locator(".sensor-roles>span.is-error").count(),0);
  assert.equal(await panel.locator('.sensor-roles>span[aria-current="step"]').count(),0);
  await page.evaluate(()=>{window.hass.connection.connected=true;window.hass.states["sensor.observation"].state="heating_response";window.hass.states["sensor.learning"].state="heating";window.hass.states["sensor.prediction"].state="ready";window.panel.hass={...window.hass};});
  // Check held values, exact five-minute deltas and invalid intervals; related: panel.js.
  await panel.getByRole("tab",{name:"대시보드",exact:true}).click();
  const samples=await page.evaluate(()=>{
    const start=Date.UTC(2026,10,5,0),minute=60000;
    window.panel.sensorStart=start;window.panel.sensorEnd=start+30*minute;
    window.panel.sensorStatus="ready";window.panel.sensorUnit="°C";
    window.panel.sensorRows=[{time:start,value:23},{time:start+7*minute,value:23.1},
      {time:start+12*minute,value:null},{time:start+13*minute,value:23.1}];
    window.panel.drawSensorHistory();
    const path=window.panel.shadowRoot.querySelector('[data-sensor-series="true"]').getAttribute("d");
    return path.match(/[ML][^ML]+/g).map(part=>({command:part[0],y:Number(part.slice(1).trim().split(/\s+/)[1])}));
  });
  assert.equal(samples.length,5,"one interval crossing unavailable is omitted");
  assert.equal(samples[0].command,"M");assert.equal(samples[1].command,"L");
  assert.equal(samples[2].command,"M","invalid interval breaks the line after recovery");
  assert.ok(samples[1].y<samples[0].y,"0.1 C rise produces a positive rate");
  assert.equal(samples[0].y,samples[2].y,"unchanged held temperatures produce zero");
  assert.equal(samples[2].y,samples[4].y,"zero continues beyond 15 minutes without new reports");
  // Keep the selected, current, and saved models distinct and show only their applicable outputs.
  const field=key=>panel.locator(`[data-prediction="${key}"]`);
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    Object.assign(attrs,{off_model:"hybrid",active_off_model:"runtime",prediction_on:false,
      heating_prediction_info:{model:"runtime",planned_minutes:45,planned_off_at:1791002700}});
    Object.assign(attrs.off_prediction,{off_model:"runtime",required_heating_minutes:40,runtime_extrapolated:true});
    window.panel.hass={...window.hass};
  });
  await panel.getByRole("tab",{name:"예측 분석",exact:true}).click();
  assert.equal(await field("active-off-model").textContent(),"가동시간 모델");
  assert.equal(await field("mode").textContent(),"꺼짐");
  assert.equal(await field("live-runtime").textContent(),"45분");
  assert.notEqual(await field("planned-end").textContent(),"해당 없음");
  assert.equal(await panel.locator(".runtime-details").isVisible(),true);
  assert.equal(await panel.locator(".peak-details").isVisible(),false);
  assert.match(await panel.locator(".prediction-explanation").textContent(),/종료 예약은 유지/);
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    attrs.active_off_model="hybrid";
    const forecast={kind:"hybrid_v1",temperature:23,base_residual:1,outdoor_correction:-.2,reaction_correction:.1,error_correction:0,limit_correction:0,predicted_peak:23.9,sample_count:6,nearby_samples:3,outdoor_samples:4,outdoor_status:"extrapolated"};
    attrs.off_prediction={model:"curve",off_model:"hybrid",predicted_peak:23.9,peak_forecast:forecast};
    window.panel.hass={...window.hass};
  });
  const sum=panel.locator(".prediction-additive");
  assert.equal(await field("live-peak").textContent(),"23.9 °C");
  assert.match(await field("live-runtime").textContent(),/총 가동시간을 제공하지 않음/);
  assert.equal(await field("planned-end").textContent(),"해당 없음");
  assert.equal(await panel.locator(".runtime-details").isVisible(),false);
  assert.match(await sum.textContent(),/가동시간별 잔열/);
  assert.equal(await panel.locator("#prediction > article").first().getAttribute("class"),"box prediction-summary");
  assert.match(await sum.textContent(),/외기 보정/);
  assert.match(await sum.textContent(),/최종 예상 Peak23\.9 °C/);
  assert.match(await sum.textContent(),/해당 기온의 정확도는 미검증/);
  await page.evaluate(()=>{window.hass.config.unit_system.temperature="°F";window.panel.hass={...window.hass};});
  assert.match(await sum.textContent(),/75\.0 °F/);
  assert.match(await sum.textContent(),/-0\.4 °F/);
  await page.evaluate(()=>{window.hass.config.unit_system.temperature="°C";window.panel.hass={...window.hass};});
  assert.match(await sum.textContent(),/최종 예상 Peak23\.9 °C/);
  for(const width of [320,1280]){
    await page.setViewportSize({width,height:1100});
    assert.equal(await sum.evaluate(element=>element.scrollWidth<=element.clientWidth+1),true);
  }
  // Preserve actual OFF evidence and distinguish a missing runtime from a pending forecast; related: runtime.py.
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    window.hass.states["sensor.prediction"].state="minimum_on";
    window.panel.hass={...window.hass};
  });
  assert.match(await panel.locator('[data-prediction="live-runtime"]').textContent(),/총 가동시간을 제공하지 않음/);
  assert.match(await panel.locator('.prediction-explanation').textContent(),/최소 가동시간/);
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    attrs.last_off_prediction={off_at:1791000000,off_model:"hybrid",predictions:{curve:23.9},peak_forecast:attrs.off_prediction.peak_forecast,confidences:{curve:.8},comparison_counts:{curve:4}};
    attrs.off_model="additive";
    attrs.active_off_model="additive";
    attrs.heater_confirmed_on=false;
    attrs.off_prediction=null;
    attrs.heating_prediction_info={model:"hybrid"};
    window.hass.states["sensor.prediction"].state="waiting_peak";
    window.panel.hass={...window.hass};
  });
  assert.match(await panel.locator('[data-prediction="live-peak"]').textContent(),/23.9 °C · 마지막 정지 예측/);
  assert.match(await sum.textContent(),/최종 예상 Peak23\.9 °C/);
  assert.equal(await field("active-off-model").textContent(),"해당 없음");
  assert.equal(await field("last-off-model").textContent(),"가동합산 모델");
  assert.equal(await field("comparison-count").textContent(),"4회");
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    attrs.last_off_prediction.off_learning_source="long_term";
    attrs.start_prediction={status:"waiting",predicted_minimum:22.6,threshold:22.5,response_delay_minutes:15,
      cooling_delta:-.25,cooling_source:"outdoor",outdoor_group:"COLD",cooling_layers:["blended"]};
    window.panel.hass={...window.hass};
  });
  assert.match(await field("comparison-count").textContent(),/장기 학습 적용 · 최근 보정 미사용/);
  const start = field("start-evidence");
  assert.match(await start.textContent(),/예상 최저 22\.60 °C · 시작 기준 22\.50 °C · 반응 지연 15분 · 외기별 냉각 \(추움\) · 장기\+최근 학습/);
  assert.equal(await panel.locator("#dashboard .start-prediction-info").textContent(),await start.textContent());
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    Object.assign(attrs.start_prediction,{status:"no_cooling",cooling_delta:.1,cooling_source:"global",cooling_layers:["current","long_term"]});
    window.hass.config.unit_system.temperature="°F";
    window.panel.hass={...window.hass};
  });
  assert.match(await start.textContent(),/추가 하락 예측 없음.*시작 기준 72\.50 °F.*공통 냉각 \(외기 추움 구간 근거 부족\).*최근 학습 \/ 장기 학습 \(5분 구간별\)/);
  assert.doesNotMatch(await start.textContent(),/예상 하락/);
  await page.evaluate(()=>{
    window.hass.states["climate.room"].attributes.start_prediction={status:"insufficient_data",reason:"peak_unconfirmed",threshold:22.5};
    window.hass.config.unit_system.temperature="°C";
    window.panel.hass={...window.hass};
  });
  assert.equal(await start.textContent(),"이번 정지 구간의 최고점 확인 대기 · 기본 시작 기준 사용 · 시작 기준 22.50 °C");
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    attrs.heater_confirmed_on=true;
    attrs.curve_fallback_reason="INSUFFICIENT_OFF_MODEL_DATA";
    window.hass.states["sensor.prediction"].state="insufficient_evidence";
    window.panel.hass={...window.hass};
  });
  assert.equal(await field("active-off-model").textContent(),"합산 잔열 모델");
  assert.equal(await field("last-off-model").textContent(),"가동합산 모델");
  assert.equal(await field("comparison-count").textContent(),"기록 없음");
  assert.equal(await field("live-peak").textContent(),"예측 대기");
  assert.match(await panel.locator(".prediction-explanation").textContent(),/기본 온도 제어/);
  const compact=await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    return window.panel.compactPredictionHistory({"climate.room":[{s:"auto",lu:1,a:attrs}]},"climate.room")["climate.room"][0].a;
  });
  assert.equal(compact.active_off_model,"additive");
  assert.equal(compact.last_off_prediction.off_model,"hybrid");
  await page.evaluate(()=>{
    const attrs=window.hass.states["climate.room"].attributes;
    attrs.curve_fallback_reason=null;
    attrs.off_prediction={model:"curve",off_model:"additive",predicted_peak:23.9,
      peak_forecast:{...attrs.last_off_prediction.peak_forecast,kind:"additive_v1"}};
    window.hass.states["sensor.prediction"].state="ready";
    window.panel.hass={...window.hass};
  });
  assert.match(await sum.textContent(),/기본 잔열/);
  assert.doesNotMatch(await sum.textContent(),/가동시간별 잔열/);
  assert.equal(await field("planned-end").textContent(),"해당 없음");
  assert.match(await panel.locator(".prediction-explanation").textContent(),/가중평균/);
  await panel.getByRole("tab",{name:"대시보드",exact:true}).click();
  for(const width of [320,1280]){await page.setViewportSize({width,height:1100});assert.equal(await panel.locator("#dashboard").evaluate(el=>el.scrollWidth<=el.clientWidth+1),true);}
  if(process.argv[3]){
    await page.screenshot({path:process.argv[3],fullPage:true});
    await page.evaluate(()=>{window.panel.style.setProperty("--card-background-color","#202020");window.panel.style.setProperty("--secondary-background-color","#292929");window.panel.style.setProperty("--primary-text-color","#eee");window.panel.style.setProperty("--secondary-text-color","#bbb");});
    await page.screenshot({path:process.argv[3].replace(/\.png$/,"-dark.png"),fullPage:true});
  }
  assert.deepEqual(errors,[]);
  console.log("PASS phase-only sensor labels, separate measurement UI, and no learning-model selector.");
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
