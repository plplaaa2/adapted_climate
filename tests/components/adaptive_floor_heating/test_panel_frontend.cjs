// Verify pushed HA states and registry identity in a real browser; related: frontend/panel.js.
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
      const state = (name, temperature) => ({state:"auto",attributes:{friendly_name:name,current_temperature:temperature,temperature:23,preset_mode:"home",learning_model:"curve",heater_confirmed_on:true,heater_command_pending:false,control_state:"HEATING",faults:[]}});
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
    await page.evaluate(() => {const s=window.hass.states["climate.renamed"];s.state="unavailable";s.attributes.faults=["sensor_fault"];window.panel.hass={...window.hass};});
    assert.equal(await value("current"), "—");
    assert.equal(await value("heater"), "확인 불가");
    assert.equal(await value("faults"), "sensor_fault");
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
    assert.equal(await page.evaluate(() => window.commands.every(command => command === "config/entity_registry/list")), true);
    assert.deepEqual(errors, []);
    console.log("PASS: room filtering/selection, pushed state, renamed ID, OFF pending/null, unavailable/faults, invalid temperature, disconnect/reconnect, removal, API failure, subscription cleanup, tabs, responsive layout, read-only commands.");
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
