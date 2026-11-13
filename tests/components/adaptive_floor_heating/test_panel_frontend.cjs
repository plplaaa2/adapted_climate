// Verify pushed states, registry identity and Climate service dispatch; related: frontend/panel.js.
// Usage: node test_panel_frontend.cjs <path-to-playwright-module>
const { chromium } = require(process.argv[2] || "playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent("<adaptive-floor-heating-panel></adaptive-floor-heating-panel>");
    await page.addScriptTag({ path: path.resolve(__dirname, "../../../custom_components/adaptive_floor_heating/frontend/panel.js") });
    await page.evaluate(() => {
      window.panel = document.querySelector("adaptive-floor-heating-panel");
      window.registry = [
        {platform:"adaptive_floor_heating", entity_id:"climate.custom_living", unique_id:"living_climate",config_entry_id:"living"},
        {platform:"adaptive_floor_heating", entity_id:"climate.custom_bedroom", unique_id:"bedroom_climate",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"switch.bedroom_policy",unique_id:"bedroom_prediction_on",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"select.renamed_off_model",unique_id:"bedroom_off_model",config_entry_id:"bedroom"},
        {platform:"other", entity_id:"climate.other", unique_id:"other"},
        {platform:"adaptive_floor_heating", entity_id:"climate.hidden", unique_id:"hidden",hidden_by:"user"},
        {platform:"adaptive_floor_heating",entity_id:"switch.bedroom_saving",unique_id:"bedroom_sunrise_saving",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"time.bedroom_start",unique_id:"bedroom_sunrise_start",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"time.bedroom_end",unique_id:"bedroom_sunrise_end",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"number.bedroom_limit",unique_id:"bedroom_peak_observation_hours",config_entry_id:"bedroom"},
        {platform:"adaptive_floor_heating",entity_id:"time.living_start",unique_id:"living_sunrise_start",config_entry_id:"living"},
      ];
      const state = (name, temperature) => ({state:"auto",attributes:{friendly_name:name,current_temperature:temperature,temperature:23,min_temp:18,max_temp:30,target_temp_step:0.1,supported_features:17,hvac_modes:["off","heat","auto"],preset_modes:["home","away"],preset_mode:"home",heater_confirmed_on:true,heater_command_pending:false,control_state:"HEATING",faults:[]}});
      window.unsubscribed = 0;
      window.commands = [];
      window.hass = {connection:{connected:true,subscribeEvents:async callback => {window.registryUpdated=callback;return () => window.unsubscribed++;}},
        callWS:async command => {window.commands.push(command.type);if(window.failRegistry)throw Error("registry unavailable");return window.registry;},
        states:{"climate.custom_living":state("거실",22.6),"climate.custom_bedroom":state("침실",21.5),"climate.hidden":state("숨김",22)},
        callService:() => {throw Error("Read-only panel issued a command");}};
      window.panel.hass=window.hass;
      window.hass.states["switch.bedroom_policy"]={state:"on",attributes:{}};
      window.hass.states["switch.bedroom_saving"]={state:"off",attributes:{}};
      window.hass.states["time.bedroom_start"]={state:"23:30:00",attributes:{}};
      window.hass.states["time.bedroom_end"]={state:"06:10:00",attributes:{}};
      window.hass.states["time.living_start"]={state:"04:15:00",attributes:{}};
      Object.assign(window.hass.states["climate.custom_bedroom"].attributes,
        {sunrise_saving_enabled:false,sunrise_saving_active:false});
      window.hass.states["select.renamed_off_model"]={state:"runtime",attributes:{options:["runtime","additive","hybrid"]}};
    });
    const panel = page.locator("adaptive-floor-heating-panel");
    await page.waitForFunction(() => window.panel.registryStatus === "ready");
    // HA updates must preserve native picker options until their available values change.
    assert.deepEqual(await page.evaluate(() => {
      const root=window.panel.shadowRoot, mode=root.querySelector("[data-mode-selector]"), preset=root.querySelector("[data-preset-selector]");
      const originalMode=mode.firstChild, originalPreset=preset.firstChild;
      window.panel.hass={...window.hass};
      const stable=mode.firstChild===originalMode && preset.firstChild===originalPreset;
      const attrs=window.hass.states["climate.custom_living"].attributes;
      attrs.hvac_modes=["off","heat"];attrs.preset_modes=["home"];
      window.panel.hass={...window.hass};
      const updated=mode.firstChild!==originalMode && preset.firstChild!==originalPreset
        && [...mode.options].map(option=>option.value).join(",")==="off,heat"
        && [...preset.options].map(option=>option.value).join(",")==="home";
      attrs.hvac_modes=["off","heat","auto"];attrs.preset_modes=["home","away"];
      window.panel.hass={...window.hass};
      return {stable,updated};
    }), {stable:true,updated:true});
    const value = async key => {
      if (key === "current") return panel.locator("#dashboard .dial-current span").textContent();
      if (key === "target") return `${Number(await panel.locator("#dashboard .dial-target").textContent()).toFixed(1)} ${await panel.locator("#dashboard .target-unit").textContent()}`;
      if (key === "mode") return (await panel.locator('#dashboard [data-mode-selector]').inputValue()).toUpperCase();
      return panel.locator(`[data-value="${key}"]`).textContent();
    };
    assert.equal(await panel.locator('[data-value="current"],[data-value="target"],[data-value="mode"],[data-value="preset"]').count(),0);
    assert.equal(await panel.locator("#room option").count(), 2);
    assert.equal(await value("current"), "22.6 °C");
    assert.equal(await panel.locator('[data-value="pending"]').count(), 0);
    await panel.locator("#room").selectOption("bedroom_climate");
    assert.equal(await value("current"), "21.5 °C");
    await page.evaluate(() => {window.hass.states["climate.custom_bedroom"].attributes.current_temperature=22;window.hass.states["climate.custom_bedroom"].attributes.heater_command_pending=null;window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "22.0 °C");
    await page.evaluate(() => {window.hass.config={unit_system:{temperature:"°F"}};window.hass.states["climate.custom_bedroom"].attributes.current_temperature=71.6;window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "71.6 °F");
    await page.evaluate(() => {window.hass.config.unit_system.temperature="°C";window.hass.states["climate.custom_bedroom"].attributes.current_temperature=22;window.panel.hass={...window.hass};});
    await page.evaluate(async () => {window.registry[1].entity_id="climate.renamed";window.hass.states["climate.renamed"]=window.hass.states["climate.custom_bedroom"];delete window.hass.states["climate.custom_bedroom"];window.panel.hass={...window.hass};await window.registryUpdated();});
    assert.equal(await panel.locator("#room").inputValue(), "bedroom_climate");
    assert.equal(await value("current"), "22.0 °C");
    await page.evaluate(() => {
      window.serviceCalls=[];
      window.hass.callService=async (domain,service,data) => {window.serviceCalls.push({domain,service,data});if(window.rejectService)throw Error("private backend error");if(window.holdService)await new Promise(resolve=>window.finishService=resolve);};
      window.panel.hass={...window.hass};
    });
    const controls = panel.locator("#dashboard .controls");
    // Arc gestures commit once on release and roll back cancellation without service calls.
    const arc = controls.locator(".temperature-arc");
    const arcBox = await arc.boundingBox();
    const topPoint = {x:arcBox.x+arcBox.width/2,y:arcBox.y+38/320*arcBox.width};
    const beforeArc = await page.evaluate(() => window.serviceCalls.length);
    await page.mouse.move(topPoint.x,topPoint.y); await page.mouse.down();
    assert.equal(await controls.locator(".dial-target").textContent(), "24.0");
    assert.equal(await page.evaluate(() => window.serviceCalls.length),beforeArc);
    await page.mouse.move(arcBox.x+284/320*arcBox.width,arcBox.y+162/320*arcBox.width,{steps:6});
    await page.mouse.up();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.length),beforeArc+1);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).data.entity_id),"climate.renamed");
    assert.equal(await controls.locator(".dial-target").textContent(),"23.0");
    assert.equal(await arc.locator(".arc-handle").isVisible(),true);
    await page.mouse.move(topPoint.x,topPoint.y);await page.mouse.down();
    await arc.dispatchEvent("pointercancel",{pointerId:1});await page.mouse.up();
    assert.equal(await page.evaluate(() => window.serviceCalls.length),beforeArc+1);
    assert.equal(await controls.locator(".dial-target").textContent(),"23.0");
    // Verify the replacement switch dispatches only to the owned entry.
    const policy = controls.getByRole("switch",{name:"예측 ON",exact:true});
    const saving = controls.getByRole("switch",{name:"일출 절약",exact:true});
    const start = controls.getByLabel("시작 시각",{exact:true});
    const end = controls.getByLabel("종료 시각",{exact:true});
    assert.equal(await controls.locator(".sunrise-status").count(),0);
    assert.equal(await saving.isChecked(),false);
    assert.match(await start.inputValue(),/^23:30/);
    assert.match(await end.inputValue(),/^06:10/);
    await saving.click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)),
      {domain:"switch",service:"turn_on",data:{entity_id:"switch.bedroom_saving"}});
    await page.evaluate(() => {
      window.hass.states["switch.bedroom_saving"].state="on";
      window.panel.hass={...window.hass};
    });
    assert.equal(await saving.isChecked(),true);
    await start.fill("22:45:10");
    await page.evaluate(() => {
      window.hass.states["number.bedroom_limit"] = {state:"6",attributes:{min:1,max:12,step:1}};
      window.panel.hass={...window.hass};
    });
    assert.equal(await start.inputValue(),"22:45:10");
    await controls.getByRole("button",{name:"시작 시각 저장"}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)),
      {domain:"time",service:"set_value",data:{entity_id:"time.bedroom_start",time:"22:45:10"}});
    await end.fill("07:15:00");
    await controls.getByRole("button",{name:"종료 시각 저장"}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)),
      {domain:"time",service:"set_value",data:{entity_id:"time.bedroom_end",time:"07:15:00"}});
    const beforeInvalidTime = await page.evaluate(() => window.serviceCalls.length);
    await page.evaluate(() => window.panel.sendCommand({command:"sunrise_time",kind:"sunrise_start",value:"25:00"}));
    assert.equal(await page.evaluate(() => window.serviceCalls.length),beforeInvalidTime);
    const peakLimit = controls.getByRole("spinbutton",{name:"최고점 관측 시간 한도 (시간)"});
    assert.equal(await peakLimit.inputValue(),"6");
    await peakLimit.fill("8");
    await peakLimit.press("Tab");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)),
      {domain:"number",service:"set_value",data:{entity_id:"number.bedroom_limit",value:8}});
    assert.equal(await controls.getByRole("combobox",{name:"OFF 모델 선택",exact:true}).count(),0);
    assert.equal(await controls.getByRole("combobox",{name:"AUTO 학습 모델",exact:true}).count(),0);
    assert.equal(await policy.isChecked(), true);
    for (const option of ["off","on"]) {
      await policy.click();
      await page.waitForFunction(() => !window.panel.commandBusy);
      assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"switch",service:option==="on"?"turn_on":"turn_off",data:{entity_id:"switch.bedroom_policy"}});
      await page.evaluate(option=>{window.hass.states["switch.bedroom_policy"].state=option;window.panel.hass={...window.hass};},option);
    }
    await page.evaluate(() => {window.holdService=true;});
    await policy.click();
    assert.equal(await policy.isDisabled(), true);
    assert.equal(await panel.locator("#room").isDisabled(), true);
    await page.evaluate(() => {window.holdService=false;window.finishService();});
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).data.entity_id), "switch.bedroom_policy");
    await page.evaluate(() => {window.rejectService=true;});
    await policy.click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.match(await controls.locator(".command-message").textContent(), /실패/);
    await page.evaluate(() => {window.rejectService=false;window.hass.states["switch.bedroom_policy"].state="unavailable";window.panel.hass={...window.hass};});
    assert.equal(await policy.isDisabled(), true);
    const beforeSelectInvalid = await page.evaluate(() => window.serviceCalls.length);
    await page.evaluate(async () => {await window.panel.sendCommand({command:"select",kind:"learning_model",option:"curve"});await window.panel.sendCommand({command:"select",kind:"prediction_mode",option:"invalid"});});
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeSelectInvalid);
    await page.evaluate(() => {window.hass.states["switch.bedroom_policy"].state="on";window.panel.hass={...window.hass};});
    await panel.locator("#room").selectOption("living_climate");
    assert.equal(await panel.locator("#dashboard .controls").getByRole("switch",{name:"예측 ON",exact:true}).isDisabled(), true);
    assert.equal(await controls.locator(".sunrise-settings").isVisible(),false);
    await page.evaluate(() => {
      window.hass.states["climate.custom_living"].attributes.sunrise_saving_enabled=false;
      window.panel.hass={...window.hass};
    });
    assert.match(await start.inputValue(),/^04:15/);
    assert.equal(await end.isDisabled(),true);
    await start.fill("04:30:00");
    await controls.getByRole("button",{name:"시작 시각 저장"}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)),
      {domain:"time",service:"set_value",data:{entity_id:"time.living_start",time:"04:30:00"}});
    await panel.locator("#room").selectOption("bedroom_climate");
    await policy.click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).data.entity_id), "switch.bedroom_policy");
    assert.equal(await controls.locator("input[type='time']").count(),2);
    assert.equal(await controls.getByRole("button",{name:"적용",exact:true}).count(),0);
    await controls.getByRole("button",{name:"목표온도 높이기",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_temperature",data:{temperature:23.1,entity_id:"climate.renamed"}});
    assert.equal(await value("target"), "23.0 °C");
    await page.evaluate(()=>{window.rejectService=true;});
    await controls.getByRole("button",{name:"목표온도 낮추기",exact:true}).click();
    await page.waitForFunction(()=>!window.panel.commandBusy);
    assert.equal(await controls.locator(".dial-target").textContent(),"23.0");
    assert.match(await controls.locator(".command-message").textContent(),/실패/);
    await page.evaluate(()=>{window.rejectService=false;window.hass.states["climate.renamed"].attributes.temperature=30;window.panel.hass={...window.hass};});
    assert.equal(await controls.getByRole("button",{name:"목표온도 높이기",exact:true}).isDisabled(),true);
    await page.evaluate(()=>{window.hass.states["climate.renamed"].attributes.temperature=18;window.panel.hass={...window.hass};});
    assert.equal(await controls.getByRole("button",{name:"목표온도 낮추기",exact:true}).isDisabled(),true);
    await page.evaluate(()=>{window.hass.states["climate.renamed"].attributes.temperature=23;window.panel.hass={...window.hass};});
    await controls.getByRole("combobox",{name:"운전 모드",exact:true}).selectOption("heat");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_hvac_mode",data:{hvac_mode:"heat",entity_id:"climate.renamed"}});
    assert.equal(await value("mode"), "AUTO");
    await controls.getByRole("combobox",{name:"재실 프리셋",exact:true}).selectOption("away");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_preset_mode",data:{preset_mode:"away",entity_id:"climate.renamed"}});
    const beforeInvalid = await page.evaluate(() => window.serviceCalls.length);
    await page.evaluate(async () => {window.panel.targetDraft="31";await window.panel.sendCommand({command:"temperature"});});
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid);
    assert.match(await controls.locator(".command-message").textContent(), /허용 범위/);
    await page.evaluate(async () => {window.panel.targetDraft="";await window.panel.sendCommand({command:"temperature"});window.panel.renderState();});
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid);
    await page.evaluate(() => {window.holdService=true;});
    await controls.getByRole("combobox",{name:"운전 모드",exact:true}).selectOption("off");
    assert.equal(await controls.getByRole("combobox",{name:"운전 모드",exact:true}).isDisabled(), true);
    assert.equal(await panel.locator("#room").isDisabled(), true);
    await page.evaluate(async () => {await window.panel.sendCommand({command:"mode",mode:"heat"});window.holdService=false;window.finishService();});
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid+1);
    await page.evaluate(() => {window.rejectService=true;});
    await controls.getByRole("combobox",{name:"운전 모드",exact:true}).selectOption("heat");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.match(await controls.locator(".command-message").textContent(), /실패/);
    assert.equal(await controls.getByRole("combobox",{name:"운전 모드",exact:true}).isDisabled(), false);
    await page.evaluate(() => {window.rejectService=false;});
    await page.evaluate(() => {window.hass.config.unit_system.temperature="°F";Object.assign(window.hass.states["climate.renamed"].attributes,{temperature:73.4,min_temp:64.4,max_temp:86,target_temp_step:0.1});window.panel.hass={...window.hass};});
    await controls.getByRole("button",{name:"목표온도 낮추기",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).data.temperature), 73.3);
    await page.evaluate(() => {window.hass.config.unit_system.temperature="°C";Object.assign(window.hass.states["climate.renamed"].attributes,{temperature:23,min_temp:18,max_temp:30});window.panel.hass={...window.hass};});
    await controls.getByRole("combobox",{name:"재실 프리셋",exact:true}).selectOption("home");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).service), "set_preset_mode");
    if (process.argv[3]) {
      await page.setViewportSize({width:1024,height:1200});
      await page.screenshot({path:process.argv[3],fullPage:true});
    }
    await page.evaluate(() => {
      window.holdService=true;
      window.originalTimeout=window.setTimeout;
      window.setTimeout=(callback,delay,...args)=>window.originalTimeout(callback,delay===15000?1:delay,...args);
    });
    await controls.getByRole("combobox",{name:"운전 모드",exact:true}).selectOption("heat");
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.match(await controls.locator(".command-message").textContent(), /응답 확인 시간/);
    await page.evaluate(() => {window.setTimeout=window.originalTimeout;window.holdService=false;window.finishService();});
    await page.evaluate(() => {const s=window.hass.states["climate.renamed"];s.state="unavailable";s.attributes.faults=["sensor_fault"];window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "—");
    assert.equal(await panel.locator("#dashboard .dial-status").textContent(), "사용할 수 없음");
    assert.equal(await controls.getByRole("combobox",{name:"운전 모드",exact:true}).isDisabled(), true);
    const beforeUnavailable = await page.evaluate(() => window.serviceCalls.length);
    await page.evaluate(() => window.panel.sendCommand({command:"mode",mode:"heat"}));
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeUnavailable);
    await page.evaluate(() => {window.hass.states["climate.renamed"].state="auto";window.hass.states["climate.renamed"].attributes.current_temperature=NaN;window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "—");
    await page.evaluate(() => {window.hass.connection.connected=false;window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "—");
    assert.match(await panel.locator(".notice").textContent(), /연결이 끊어/);
    await page.evaluate(() => {window.hass.connection.connected=true;window.hass.states["climate.renamed"].attributes.current_temperature=22;window.panel.hass={...window.hass};});
    await page.waitForFunction(() => window.panel.registryStatus === "ready");
    await panel.getByRole("tab",{name:"학습 분석",exact:true}).click();
    await page.keyboard.press("ArrowRight");
    assert.equal(await panel.getByRole("tabpanel").getAttribute("id"), "dashboard");
    await page.keyboard.press("Home");
    for (const width of [1024,700,360,320]) {
      await page.setViewportSize({width,height:1100});
      assert.equal(await panel.evaluate(element => [...element.shadowRoot.querySelectorAll("header,main,.box")].filter(item => item.getClientRects().length).every(item => item.getBoundingClientRect().right <= innerWidth+1)), true);
      assert.equal(await controls.locator(".sunrise-settings").evaluate(element => element.scrollWidth <= element.clientWidth + 1),true);
    }
    await page.evaluate(async () => {window.registry=[];await window.registryUpdated();});
    assert.equal(await value("current"), "—");
    assert.match(await panel.locator(".notice").textContent(), /항목이 없습니다/);
    await page.evaluate(async () => {window.failRegistry=true;await window.registryUpdated();});
    assert.match(await panel.locator(".notice").textContent(), /불러올 수 없습니다/);
    await page.evaluate(() => window.panel.remove());
    assert.equal(await page.evaluate(() => window.unsubscribed), 2);
    assert.equal(await page.evaluate(() => window.commands.every(command => ["config/entity_registry/list","history/history_during_period","adaptive_floor_heating/curve_memory","adaptive_floor_heating/curve_cycles"].includes(command))), true);
    assert.deepEqual(errors, []);
    console.log("PASS: room selection/state/rename, pending semantics, unit display, Climate service targets, draft preservation, range/empty validation, busy/double-click guard, rejection recovery, dashboard controls, unavailable guard, reconnect, cleanup, tabs and responsive layout.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
