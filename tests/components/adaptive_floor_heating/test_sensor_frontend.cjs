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
  });
  const panel=page.locator("adaptive-floor-heating-panel");
  await page.waitForFunction(()=>window.panel.registryStatus==="ready");
  await panel.getByRole("tab",{name:"예측 분석",exact:true}).click();
  assert.equal(await panel.locator("#dashboard [data-value=initialLearning]").textContent(),"초기 학습 미설정");
  assert.equal(await panel.locator("#dashboard [data-value=learning]").textContent(),"난방 곡선 관측 중");
  assert.equal(await panel.locator("#dashboard [data-value=observation]").textContent(),"가동 중 온도 상승 관측");
  assert.equal(await panel.locator("#dashboard [data-value=predictionStatus]").textContent(),"예측 가능");
  assert.equal(await panel.locator("#prediction [data-prediction=status]").textContent(),"예측 가능");
  assert.equal(await panel.locator("#prediction [data-prediction=initial-learning]").textContent(),"초기 학습 미설정");
  assert.equal(await panel.locator("#prediction [data-prediction=learning]").textContent(),"난방 사이클 관측 중");
  assert.equal(await panel.locator("#prediction [data-prediction=observation]").textContent(),"가동 중 반응 관측");
  assert.equal(await panel.getByRole("combobox",{name:"AUTO 학습 모델",exact:true}).count(),0);
  assert.equal(await panel.locator(".sensor-choice").count(),0);
  assert.equal(await panel.locator(".sensor-period").count(),1);
  assert.equal(await panel.locator("[data-sensor-phase=observation]").textContent(),"가동 중 온도 상승 관측");
  assert.equal(await panel.locator("[data-sensor-phase=initial-learning]").textContent(),"초기 학습 미설정");
  assert.equal(await panel.locator("[data-sensor-phase=learning]").textContent(),"난방 곡선 관측 중");
  assert.equal(await panel.locator("[data-sensor-phase=prediction]").textContent(),"예측 가능");
  assert.equal(await panel.locator('select[aria-label="예측 운전"]').count(),1);
  assert.deepEqual(errors,[]);
  console.log("PASS phase-only sensor labels, separate measurement UI, and no learning-model selector.");
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
