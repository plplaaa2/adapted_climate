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
        {platform:"adaptive_floor_heating", entity_id:"climate.custom_living", unique_id:"living_climate"},
        {platform:"adaptive_floor_heating", entity_id:"climate.custom_bedroom", unique_id:"bedroom_climate"},
        {platform:"other", entity_id:"climate.other", unique_id:"other"},
        {platform:"adaptive_floor_heating", entity_id:"climate.hidden", unique_id:"hidden",hidden_by:"user"},
      ];
      const state = (name, temperature) => ({state:"auto",attributes:{friendly_name:name,current_temperature:temperature,temperature:23,min_temp:18,max_temp:30,target_temp_step:0.1,supported_features:17,hvac_modes:["off","heat","auto"],preset_modes:["home","away"],preset_mode:"home",learning_model:"curve",heater_confirmed_on:true,heater_command_pending:false,control_state:"HEATING",faults:[]}});
      window.unsubscribed = 0;
      window.commands = [];
      window.hass = {connection:{connected:true,subscribeEvents:async callback => {window.registryUpdated=callback;return () => window.unsubscribed++;}},
        callWS:async command => {window.commands.push(command.type);if(window.failRegistry)throw Error("registry unavailable");return window.registry;},
        states:{"climate.custom_living":state("거실",22.6),"climate.custom_bedroom":state("침실",21.5),"climate.hidden":state("숨김",22)},
        callService:() => {throw Error("Read-only panel issued a command");}};
      window.panel.hass=window.hass;
    });
    const panel = page.locator("adaptive-floor-heating-panel");
    await page.waitForFunction(() => window.panel.registryStatus === "ready");
    const value = key => panel.locator(`[data-value="${key}"]`).textContent();
    assert.equal(await panel.locator("#room option").count(), 2);
    assert.equal(await value("current"), "22.6 °C");
    assert.equal(await value("pending"), "OFF 확인 대기");
    await panel.locator("#room").selectOption("bedroom_climate");
    assert.equal(await value("current"), "21.5 °C");
    await page.evaluate(() => {window.hass.states["climate.custom_bedroom"].attributes.current_temperature=22;window.hass.states["climate.custom_bedroom"].attributes.heater_command_pending=null;window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "22.0 °C");
    assert.equal(await value("pending"), "없음");
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
    await controls.getByRole("spinbutton").fill("24.2");
    await page.evaluate(() => {window.panel.hass={...window.hass};});
    assert.equal(await controls.getByRole("spinbutton").inputValue(), "24.2");
    await controls.getByRole("button",{name:"적용",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_temperature",data:{temperature:24.2,entity_id:"climate.renamed"}});
    assert.equal(await value("target"), "23.0 °C");
    await controls.getByRole("button",{name:"HEAT",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_hvac_mode",data:{hvac_mode:"heat",entity_id:"climate.renamed"}});
    assert.equal(await value("mode"), "AUTO");
    await controls.getByRole("button",{name:"외출",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.deepEqual(await page.evaluate(() => window.serviceCalls.at(-1)), {domain:"climate",service:"set_preset_mode",data:{preset_mode:"away",entity_id:"climate.renamed"}});
    await controls.getByRole("spinbutton").fill("31");
    const beforeInvalid = await page.evaluate(() => window.serviceCalls.length);
    await controls.getByRole("button",{name:"적용",exact:true}).click();
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid);
    assert.match(await controls.locator(".command-message").textContent(), /허용 범위/);
    await controls.getByRole("spinbutton").fill("");
    await controls.getByRole("button",{name:"적용",exact:true}).click();
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid);
    await page.evaluate(() => {window.holdService=true;});
    await controls.getByRole("button",{name:"OFF",exact:true}).click();
    assert.equal(await controls.getByRole("button",{name:"HEAT",exact:true}).isDisabled(), true);
    assert.equal(await panel.locator("#room").isDisabled(), true);
    await page.evaluate(async () => {await window.panel.sendCommand({command:"mode",mode:"heat"});window.holdService=false;window.finishService();});
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.length), beforeInvalid+1);
    await page.evaluate(() => {window.rejectService=true;});
    await controls.getByRole("button",{name:"HEAT",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.match(await controls.locator(".command-message").textContent(), /실패/);
    assert.equal(await controls.getByRole("button",{name:"HEAT",exact:true}).isDisabled(), false);
    await page.evaluate(() => {window.rejectService=false;});
    await page.evaluate(() => {window.hass.config.unit_system.temperature="°F";Object.assign(window.hass.states["climate.renamed"].attributes,{temperature:73.4,min_temp:64.4,max_temp:86,target_temp_step:0.1});window.panel.hass={...window.hass};});
    await controls.getByRole("spinbutton").fill("75.2");
    await controls.getByRole("button",{name:"적용",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).data.temperature), 75.2);
    await page.evaluate(() => {window.hass.config.unit_system.temperature="°C";Object.assign(window.hass.states["climate.renamed"].attributes,{temperature:23,min_temp:18,max_temp:30});window.panel.hass={...window.hass};});
    await panel.getByRole("tab",{name:"운전 제어",exact:true}).click();
    await panel.locator("#control .controls").getByRole("button",{name:"재실",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.equal(await page.evaluate(() => window.serviceCalls.at(-1).service), "set_preset_mode");
    await panel.getByRole("tab",{name:"대시보드",exact:true}).click();
    if (process.argv[3]) {
      await page.setViewportSize({width:1024,height:1200});
      await page.screenshot({path:process.argv[3],fullPage:true});
    }
    await page.evaluate(() => {
      window.holdService=true;
      window.originalTimeout=window.setTimeout;
      window.setTimeout=(callback,delay,...args)=>window.originalTimeout(callback,delay===15000?1:delay,...args);
    });
    await controls.getByRole("button",{name:"HEAT",exact:true}).click();
    await page.waitForFunction(() => !window.panel.commandBusy);
    assert.match(await controls.locator(".command-message").textContent(), /응답 확인 시간/);
    await page.evaluate(() => {window.setTimeout=window.originalTimeout;window.holdService=false;window.finishService();});
    await page.evaluate(() => {const s=window.hass.states["climate.renamed"];s.state="unavailable";s.attributes.faults=["sensor_fault"];window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "—");
    assert.equal(await value("heater"), "확인 불가");
    assert.equal(await value("faults"), "sensor_fault");
    assert.equal(await controls.getByRole("button",{name:"OFF",exact:true}).isDisabled(), true);
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
    assert.equal(await panel.getByRole("tabpanel").getAttribute("id"), "history");
    await page.keyboard.press("Home");
    for (const width of [1024,700,360,320]) {
      await page.setViewportSize({width,height:1100});
      assert.equal(await panel.evaluate(element => [...element.shadowRoot.querySelectorAll("header,main,.box")].filter(item => item.getClientRects().length).every(item => item.getBoundingClientRect().right <= innerWidth+1)), true);
    }
    await page.evaluate(async () => {window.registry=[];await window.registryUpdated();});
    assert.equal(await value("current"), "—");
    assert.match(await panel.locator(".notice").textContent(), /항목이 없습니다/);
    await page.evaluate(async () => {window.failRegistry=true;await window.registryUpdated();});
    assert.match(await panel.locator(".notice").textContent(), /불러올 수 없습니다/);
    await page.evaluate(() => window.panel.remove());
    assert.equal(await page.evaluate(() => window.unsubscribed), 2);
    assert.equal(await page.evaluate(() => window.commands.every(command => ["config/entity_registry/list","history/history_during_period"].includes(command))), true);
    assert.deepEqual(errors, []);
    console.log("PASS: room selection/state/rename, pending semantics, unit display, Climate service targets, draft preservation, range/empty validation, busy/double-click guard, rejection recovery, no optimistic state, both control surfaces, unavailable guard, reconnect, cleanup, tabs and responsive layout.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
