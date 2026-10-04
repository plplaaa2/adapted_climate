// Display HA Climate states and dispatch validated Climate services; related: panel.py, climate.py.
class AdaptiveFloorHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.activeTab = "dashboard";
    this.registry = [];
    this.selectedEntry = null;
    this.registryStatus = "loading";
    this.generation = 0;
    this.commandBusy = false;
    this.commandMessage = "";
    this.targetDirty = false;
    this.historyHours = 6;
    this.historyRows = [];
    this.historyStatus = "idle";
    this.historyRequest = 0;
    this.learningKind = "WARM_HEATING";
    this.learningStatus = "idle";
    this.learningData = null;
    this.learningRequest = 0;
  }

  connectedCallback() {
    if (this.shadowRoot.childElementCount) {
      this.startGraphLifecycle();
      this.startConnection();
      return;
    }
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: var(--primary-text-color, #253549); background: var(--primary-background-color, #f5f6f8); min-height: 100%; font-family: var(--paper-font-body1_-_font-family, sans-serif); }
        * { box-sizing: border-box; }
        header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; padding: 16px 24px; background: var(--card-background-color, #fff); border-bottom: 1px solid var(--divider-color, #e1e6ec); }
        .brand { font-weight: 500; margin-right: 12px; }
        nav { display: flex; flex-wrap: wrap; gap: 6px; }
        button { font: inherit; color: var(--secondary-text-color, #657588); border: 0; border-radius: 8px; padding: 10px 14px; background: transparent; cursor: pointer; min-height: 44px; }
        button[aria-selected="true"] { color: var(--primary-text-color, #253549); background: var(--secondary-background-color, #edf3f8); }
        button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
        main { max-width: 1280px; padding: 24px; margin: auto; }
        h1 { font-size: 24px; font-weight: 500; margin: 0 0 6px; }
        h2 { font-size: 16px; font-weight: 500; margin: 0 0 12px; }
        p { margin: 0; color: var(--secondary-text-color, #657588); font-size: 14px; line-height: 1.6; }
        .subtitle { margin-bottom: 24px; }
        .room-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
        select { max-width: 100%; min-height: 44px; padding: 8px 12px; font: inherit; color: var(--primary-text-color, #253549); background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e1e6ec); border-radius: 8px; }
        .temperature { font-size: 42px; font-variant-numeric: tabular-nums; margin: 16px 0; }
        dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px; margin: 0; }
        dt { color: var(--secondary-text-color, #657588); }
        dd { margin: 0; text-align: right; overflow-wrap: anywhere; }
        .notice { margin-bottom: 16px; }
        .controls { margin-top: 18px; }
        .control-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 10px 0; }
        .controls input { width: 100px; min-height: 44px; font: inherit; padding: 8px; color: var(--primary-text-color, #253549); background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e1e6ec); border-radius: 8px; }
        .controls button { border: 1px solid var(--divider-color, #e1e6ec); }
        .controls button[aria-pressed="true"] { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        button:disabled, input:disabled { opacity: .5; cursor: default; }
        .command-message { margin-top: 12px; }
        .overview, .details { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .box { min-width: 0; padding: 20px; background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e1e6ec); border-radius: 12px; }
        .empty { display: grid; place-items: center; min-height: 150px; text-align: center; }
        .graph { margin: 16px 0; }
        .graph .empty { min-height: 230px; }
        .graph-heading, .graph-legend { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
        .graph-legend { justify-content: flex-start; margin-top: 10px; font-size: 12px; color: var(--secondary-text-color, #657588); }
        .graph-legend span { display: inline-flex; align-items: center; gap: 6px; }
        .swatch { width: 20px; height: 3px; background: var(--primary-color, #03a9f4); }
        .swatch.target { background: transparent; border-top: 2px dashed var(--primary-text-color, #253549); }
        .swatch.heater { height: 9px; background: var(--warning-color, #ed8a3b); opacity: .5; }
        .history-chart { position: relative; min-width: 0; }
        .history-chart svg { display: block; width: 100%; height: 285px; touch-action: pan-y; }
        .history-chart svg text { font-size: 12px; fill: var(--secondary-text-color, #657588); }
        .history-chart .grid { stroke: var(--divider-color, #e1e6ec); stroke-width: 1; }
        .history-chart .actual { stroke: var(--primary-color, #03a9f4); stroke-width: 2.5; fill: none; }
        .history-chart .target { stroke: var(--primary-text-color, #253549); stroke-width: 1.5; stroke-dasharray: 5 4; fill: none; }
        .graph-detail { min-height: 42px; margin-top: 8px; font-variant-numeric: tabular-nums; }
        .cycle-actual { font-size: 28px; font-variant-numeric: tabular-nums; margin: 8px 0 16px; }
        .cycle-times { font-size: 12px; gap: 6px; margin-bottom: 16px; }
        .cycle-table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; }
        .cycle-table th, .cycle-table td { padding: 8px 3px; text-align: right; border-bottom: 1px solid var(--divider-color, #e1e6ec); overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
        .cycle-table th:first-child, .cycle-table td:first-child { text-align: left; }
        .cycle-note { font-size: 12px; margin-top: 12px; }
        .learning-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
        .learning-heading select { min-width: 0; }
        .thermostat { max-width: 440px; margin: 0 auto; }
        .temperature-dial { position: relative; width: min(100%, 320px); margin: 12px auto 0; }
        .temperature-arc { display:block; width:100%; height:auto; touch-action:none; cursor:pointer; }
        .temperature-arc[aria-disabled="true"] { cursor:default; }
        .arc-track,.arc-value { fill:none; stroke-width:18; stroke-linecap:round; }
        .arc-track { stroke:var(--divider-color, #ddd); }
        .arc-value { stroke:var(--primary-color, #03a9f4); }
        .heating .arc-value,.heating .arc-handle { stroke:var(--state-climate-heat-color, #ff9800); }
        .arc-handle { fill:var(--card-background-color, #fff); stroke:var(--primary-color, #03a9f4); stroke-width:6; }
        .arc-hit { fill:transparent; }
        .dial-center { position:absolute; top:25%; left:18%; right:18%; text-align:center; pointer-events:none; }
        .dial-center label { font-size:13px; color:var(--secondary-text-color); }
        .dial-center input { display:block; width:100%; box-sizing:border-box; min-width:0; font:inherit; font-size:44px; text-align:center; border:0; background:transparent; color:var(--primary-text-color); padding:0; pointer-events:auto; appearance:textfield; }
        .dial-center input::-webkit-inner-spin-button { appearance:none; }
        .dial-current { font-size:14px; color:var(--secondary-text-color); }
        .dial-adjust { justify-content:center; gap:28px; margin-top:12px; pointer-events:auto; }
        .dial-adjust button { border-radius:50%; width:44px; padding:0; background:var(--secondary-background-color); font-size:24px; }
        .dial-apply,.dial-modes,.dial-presets { justify-content:center; }
        .dial-status { text-align:center; margin:8px 0 14px; }
        .dial-modes button { display:flex; flex-direction:column; align-items:center; min-width:64px; gap:4px; border-radius:16px; }
        .dial-modes button span { font-size:24px; }
        .thermostat .command-message { margin-top:16px; }
        .dial-settings { border-top:1px solid var(--divider-color); margin-top:16px; padding-top:16px; }
        .dial-settings summary { cursor:pointer; min-height:32px; }
        .learning-chart { min-width: 0; }
        .learning-chart svg { display: block; width: 100%; height: 260px; touch-action: pan-y; }
        .learning-chart svg text { font-size: 12px; fill: var(--secondary-text-color, #657588); }
        .learning-chart .grid { stroke: var(--divider-color, #e1e6ec); stroke-width: 1; }
        .learning-chart .current { stroke: var(--primary-color, #03a9f4); stroke-width: 2; fill: none; }
        .learning-chart .long_term { stroke: var(--primary-text-color, #253549); stroke-width: 2; stroke-dasharray: 5 4; fill: none; }
        .learning-metrics { margin: 12px 0; font-size: 12px; }
        .learning-detail { font-size: 12px; margin-top: 12px; min-height: 40px; }
        [hidden] { display: none !important; }
        .menu { display: none; }
        :host([narrow]) .menu { display: inline-flex; }
        @media (max-width: 700px) { header { padding: 12px 16px; gap: 8px; } .brand { flex: 1; } nav { width: 100%; } main { padding: 16px; } .overview, .details { grid-template-columns: 1fr; } }
      </style>
      <header>
        <button class="menu" aria-label="Home Assistant 사이드바 열기">☰</button>
        <span class="brand">Adaptive Floor Heating</span>
        <nav role="tablist" aria-label="바닥난방 메뉴">
          <button id="tab-dashboard" role="tab" data-tab="dashboard" aria-controls="dashboard" aria-selected="true">대시보드</button>
          <button id="tab-control" role="tab" data-tab="control" aria-controls="control" aria-selected="false" tabindex="-1">운전 제어</button>
          <button id="tab-learning" role="tab" data-tab="learning" aria-controls="learning" aria-selected="false" tabindex="-1">학습 분석</button>
          <button id="tab-history" role="tab" data-tab="history" aria-controls="history" aria-selected="false" tabindex="-1">운전 기록</button>
        </nav>
      </header>
      <main>
        <h1>바닥난방</h1><p class="subtitle">상태와 제어 · 운전 그래프 · 학습 분석</p>
        <div class="room-row"><label for="room">방 / 통합</label><select id="room" aria-label="방 / 통합 선택" disabled></select></div>
        <p class="notice" role="status">항목을 불러오는 중입니다.</p>
        <section id="dashboard" role="tabpanel" aria-labelledby="tab-dashboard">
          <div class="overview">
            <article class="box"><h2>현재 온도 · 운전 상태</h2><div class="temperature" data-value="current">—</div><dl><dt>목표온도</dt><dd data-value="target">—</dd><dt>운전 모드</dt><dd data-value="mode">—</dd><dt>재실 / 외출</dt><dd data-value="preset">—</dd></dl></article>
            <article class="box"><h2>예측과 운전</h2><dl><dt>학습 모델</dt><dd data-value="model">—</dd><dt>히터 확인 상태</dt><dd data-value="heater">—</dd><dt>명령 대기</dt><dd data-value="pending">—</dd><dt>제어 상태</dt><dd data-value="control">—</dd><dt>오류</dt><dd data-value="faults">—</dd></dl></article>
          </div>
          <article class="box graph"><h2>온도와 난방 운전</h2><div class="empty"><p>온도 이력이 연결되면 그래프가 표시됩니다.</p></div></article>
          <div class="details">
            <article class="box"><h2>학습 커브</h2><div class="empty"><p>Current · Long-term 학습 데이터 연결 예정</p></div></article>
            <article class="box completed-cycle"><h2>최근 완료 사이클</h2><p class="cycle-status" role="status"></p><div class="cycle-result" hidden><p>실제 최고온도</p><div class="cycle-actual">—</div><dl class="cycle-times"><dt>난방 OFF</dt><dd data-cycle-time="off_at">—</dd><dt>실제 최고점</dt><dd data-cycle-time="peak_at">—</dd><dt>관측 완료</dt><dd data-cycle-time="completed_at">—</dd></dl><table class="cycle-table"><thead><tr><th scope="col">모델</th><th scope="col">예측 최고</th><th scope="col">오차</th><th scope="col">신뢰도</th></tr></thead><tbody><tr><th scope="row">기본 학습</th><td data-cycle="existing-prediction">—</td><td data-cycle="existing-error">—</td><td data-cycle="existing-confidence">—</td></tr><tr><th scope="row">5분 커브</th><td data-cycle="curve-prediction">—</td><td data-cycle="curve-error">—</td><td data-cycle="curve-confidence">—</td></tr></tbody></table><p class="cycle-note"></p></div></article>
          </div>
        </section>
        <section id="control" role="tabpanel" aria-labelledby="tab-control" hidden><article class="box"><h2>운전 제어</h2><div class="empty"><p>온도 · 모드 · 프리셋 제어 연결 예정</p></div></article></section>
        <section id="learning" role="tabpanel" aria-labelledby="tab-learning" hidden><article class="box"><h2>학습 분석</h2><div class="empty"><p>학습 커브와 표본·신뢰도 연결 예정</p></div></article></section>
        <section id="history" role="tabpanel" aria-labelledby="tab-history" hidden><article class="box"><h2>운전 기록</h2><div class="empty"><p>온도와 난방 운전 이력 연결 예정</p></div></article></section>
      </main>`;
    this.shadowRoot.querySelector(".menu").addEventListener("click", () => {
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    });
    const tabs = [...this.shadowRoot.querySelectorAll("[role=tab]")];
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => this.selectTab(tab.dataset.tab));
      tab.addEventListener("keydown", (event) => {
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
        if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        this.selectTab(tabs[next].dataset.tab);
        tabs[next].focus();
      });
    });
    this.selectTab(this.activeTab);
    // Share controls across dashboard and control tab; related: climate.py service handlers.
    const makeControls = (suffix) => `<div class="controls thermostat"><div class="temperature-dial"><svg viewBox="0 0 320 280" class="temperature-arc" role="img" aria-label="목표온도 아크: 누르거나 드래그해 조절"><path class="arc-track" d="M62 238 A124 124 0 1 1 258 238"/><path class="arc-value" d="M62 238 A124 124 0 1 1 258 238" pathLength="100"/><circle class="arc-handle" r="14"/><circle class="arc-hit" r="24"/></svg><div class="dial-center"><label for="target-${suffix}">목표온도 <span class="target-unit"></span></label><input id="target-${suffix}" type="number" aria-label="목표온도 입력"><div class="dial-current">현재 <span>—</span></div><div class="control-row dial-adjust"><button data-adjust="-1" aria-label="목표온도 낮추기">−</button><button data-adjust="1" aria-label="목표온도 높이기">+</button></div></div></div><div class="control-row dial-apply"><button data-command="temperature">적용</button></div><p class="dial-status"></p><div class="control-row dial-modes" aria-label="운전 모드"><button data-command="mode" data-mode="off"><span aria-hidden="true">⏻</span>OFF</button><button data-command="mode" data-mode="heat"><span aria-hidden="true">♨</span>HEAT</button><button data-command="mode" data-mode="auto"><span aria-hidden="true">↻</span>AUTO</button></div><div class="control-row dial-presets" aria-label="재실 프리셋"><button data-command="preset" data-preset="home">재실</button><button data-command="preset" data-preset="away">외출</button></div><p class="command-message" role="status"></p></div>`;
    this.shadowRoot.querySelector(".overview .box").insertAdjacentHTML("beforeend", makeControls("dashboard"));
    this.shadowRoot.querySelector("#control .box").innerHTML = `<h2>운전 제어</h2>${makeControls("control")}`;
    this.setupTemperatureArcs();
    this.shadowRoot.querySelectorAll(".controls input").forEach(input => input.addEventListener("input", () => {
      this.targetDraft = input.value;
      this.targetDirty = true;
      this.renderState();
    }));
    this.shadowRoot.querySelectorAll("[data-adjust]").forEach(button => button.addEventListener("click", () => {
      const attrs = this.selectedState()?.attributes || {};
      const current = Number(this.targetDraft);
      if (!Number.isFinite(current) || this.targetDraft === "") return;
      this.targetDraft = String(Math.round(Math.max(attrs.min_temp, Math.min(attrs.max_temp, current + Number(button.dataset.adjust) * this.targetStep(attrs))) * 1000) / 1000);
      this.targetDirty = true;
      this.renderState();
    }));
    this.shadowRoot.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", () => this.sendCommand(button.dataset)));
    // Resolve sibling Select entities through registry ownership; related: select.py unique IDs.
    this.shadowRoot.querySelectorAll(".controls").forEach((controls, index) => {
      controls.querySelector(".command-message").insertAdjacentHTML("beforebegin", `<div class="control-row"><label for="model-${index}">AUTO 학습 모델</label><select id="model-${index}" data-selector="learning_model" aria-label="AUTO 학습 모델"></select></div><div class="control-row"><label for="prediction-${index}">예측 운전</label><select id="prediction-${index}" data-selector="prediction_mode" aria-label="예측 운전"></select></div><p>AUTO에 적용됩니다. eco는 예측 ON 미사용, balanced는 반응 지연의 절반, comfort는 전체를 반영합니다. 세 모드 모두 예측 OFF를 사용합니다.</p>`);
    });
    this.shadowRoot.querySelectorAll("[data-selector]").forEach(select => select.addEventListener("change", () => {
      const option = select.value;
      this.renderState();
      this.sendCommand({command:"select", kind:select.dataset.selector, option});
    }));
    this.shadowRoot.querySelectorAll(".controls").forEach(controls => {
      const settings = document.createElement("details");
      settings.className = "dial-settings"; settings.open = true;
      const summary = document.createElement("summary"); summary.textContent = "학습·예측 설정";
      settings.append(summary);
      const first = controls.querySelector("[data-selector]").closest(".control-row");
      const second = first.nextElementSibling, note = second.nextElementSibling;
      first.before(settings); settings.append(first,second,note);
    });
    // History stays read-only and uses the same selected Climate; related: climate.py.
    const graphMarkup = `<div class="graph-heading"><h2>온도와 난방 운전</h2><div class="control-row"><select class="history-period" aria-label="그래프 기간"><option value="6">최근 6시간</option><option value="24">최근 24시간</option></select><button class="history-refresh">새로고침</button></div></div><p class="history-notice" role="status"></p><div class="history-chart"></div><div class="graph-legend"><span><i class="swatch"></i>실내 온도</span><span><i class="swatch target"></i>목표온도</span><span><i class="swatch heater"></i>히터 ON 확인</span></div><p class="graph-detail">그래프를 가리키거나 터치하면 해당 시각의 기록을 확인할 수 있습니다.</p>`;
    this.shadowRoot.querySelector("#dashboard .graph").innerHTML = graphMarkup;
    this.shadowRoot.querySelector("#history .box").innerHTML = graphMarkup;
    this.shadowRoot.querySelectorAll(".history-period").forEach(select => select.addEventListener("change", () => {
      this.historyHours = Number(select.value);
      this.syncHistory();
    }));
    this.shadowRoot.querySelectorAll(".history-refresh").forEach(button => button.addEventListener("click", () => this.loadHistory()));
    const learningMarkup = `<div class="learning-heading"><h2>학습 커브</h2><select class="learning-kind" aria-label="학습 커브 종류"><option value="COLD_HEATING">Cold</option><option value="WARM_HEATING" selected>Warm</option><option value="PREDICTIVE_WARM_HEATING">Predictive Warm</option><option value="COOLING">Cooling</option></select><button class="learning-refresh">새로고침</button></div><p class="learning-notice" role="status"></p><dl class="learning-metrics"><dt>승인 / 제외 사이클</dt><dd data-learning="counts">—</dd><dt>Current 신뢰도</dt><dd data-learning="current-confidence">—</dd><dt>Long-term 신뢰도</dt><dd data-learning="long-confidence">—</dd></dl><div class="learning-chart"></div><div class="graph-legend"><span><i class="swatch"></i>Current</span><span><i class="swatch target"></i>Long-term</span></div><p class="learning-detail">5분 구간별 온도 변화량 · 누락 구간은 연결하지 않습니다.</p>`;
    this.shadowRoot.querySelector(".details .box").innerHTML = learningMarkup;
    this.shadowRoot.querySelector("#learning .box").innerHTML = learningMarkup;
    this.shadowRoot.querySelectorAll(".learning-kind").forEach(select => select.addEventListener("change", () => {
      this.learningKind = select.value;
      this.drawLearning();
    }));
    this.shadowRoot.querySelectorAll(".learning-refresh").forEach(button => button.addEventListener("click", () => this.loadLearning()));
    this.startGraphLifecycle();
    this.shadowRoot.getElementById("room").addEventListener("change", (event) => {
      this.selectedEntry = event.target.value;
      this.commandMessage = "";
      this.renderState();
    });
    this.renderState();
    this.startConnection();
  }

  // Use HA's authenticated registry and pushed states, including renamed IDs.
  // Related: climate.py unique IDs and panel.py local custom-panel registration.
  set hass(value) {
    this._hass = value;
    if (value?.connection?.connected === false) {
      this.disconnectedCallback();
      this.registry = [];
      this.registryStatus = "disconnected";
    }
    if (this.isConnected) this.startConnection();
    this.renderState();
  }

  get hass() { return this._hass; }

  disconnectedCallback() {
    this.generation += 1;
    this.unsubscribeRegistry?.();
    this.unsubscribeRegistry = null;
    this.connection = null;
    this.commandBusy = false;
    this.commandMessage = "";
    clearInterval(this.historyTimer);
    this.historyObserver?.disconnect();
    this.historyRequest += 1;
    this.historyKey = null;
    this.historyRows = [];
    this.historyStatus = "idle";
    this.drawHistory();
    this.learningRequest += 1;
    this.learningKey = null;
    this.learningData = null;
    this.learningStatus = "idle";
    this.drawLearning();
  }

  async startConnection() {
    const connection = this._hass?.connection;
    if (!connection || connection.connected === false || connection === this.connection) return;
    this.unsubscribeRegistry?.();
    this.unsubscribeRegistry = null;
    this.connection = connection;
    const generation = ++this.generation;
    this.startGraphLifecycle();
    this.commandBusy = false;
    this.commandMessage = "";
    this.registry = [];
    this.registryStatus = "loading";
    this.renderState();
    try {
      const unsubscribe = await connection.subscribeEvents(() => this.loadRegistry(generation), "entity_registry_updated");
      if (generation !== this.generation || !this.isConnected) { unsubscribe(); return; }
      this.unsubscribeRegistry = unsubscribe;
    } catch {
      if (generation === this.generation) this.registryStatus = "error";
    }
    if (generation === this.generation && this.isConnected) await this.loadRegistry(generation);
  }

  async loadRegistry(generation) {
    if (generation !== this.generation || !this.isConnected) return;
    const request = this.registryRequest = (this.registryRequest || 0) + 1;
    try {
      const entries = await this._hass.callWS({ type: "config/entity_registry/list" });
      if (generation !== this.generation || request !== this.registryRequest || !this.isConnected) return;
      this.registry = entries.filter((entry) => entry.platform === "adaptive_floor_heating"
        && entry.entity_id.startsWith("climate.") && !entry.disabled_by && !entry.hidden_by);
      this.selectRegistry = entries.filter(entry => entry.platform === "adaptive_floor_heating"
        && entry.entity_id.startsWith("select.") && !entry.disabled_by && !entry.hidden_by);
      this.registryStatus = "ready";
    } catch {
      if (generation !== this.generation || request !== this.registryRequest || !this.isConnected) return;
      this.registry = [];
      this.registryStatus = "error";
    }
    this.renderState();
  }

  renderState() {
    const room = this.shadowRoot.getElementById("room");
    if (!room) return;
    const states = this._hass?.states || {};
    const entries = this.registry.filter((entry) => Object.hasOwn(states, entry.entity_id));
    if (!this.registry.some((entry) => entry.unique_id === this.selectedEntry)) {
      this.selectedEntry = entries[0]?.unique_id || null;
    }
    const options = entries.map((entry) => ({ value: entry.unique_id,
      label: states[entry.entity_id].attributes?.friendly_name || entry.name || entry.entity_id }));
    const signature = JSON.stringify(options);
    if (signature !== this.optionsSignature) {
      this.optionsSignature = signature;
      room.replaceChildren(...options.map((item) => new Option(item.label, item.value)));
    }
    room.value = this.selectedEntry || "";
    room.disabled = !entries.length || this.commandBusy;
    const entry = entries.find((item) => item.unique_id === this.selectedEntry);
    const state = entry && states[entry.entity_id];
    const unavailable = !state || ["unavailable", "unknown"].includes(state.state);
    const attrs = state?.attributes || {};
    const notice = this.shadowRoot.querySelector(".notice");
    notice.textContent = this.registryStatus === "error" ? "항목을 불러올 수 없습니다. 페이지를 다시 열어주세요." : this.registryStatus === "disconnected" ? "Home Assistant 연결이 끊어졌습니다." : this.registryStatus === "loading" ? "항목을 불러오는 중입니다." : !state ? "표시할 바닥난방 Climate 항목이 없습니다." : unavailable ? "이 항목은 현재 사용할 수 없습니다." : "";
    notice.hidden = !notice.textContent;
    const number = (value) => typeof value === "number" && Number.isFinite(value);
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    const temperature = (value) => !unavailable && number(value) ? `${value.toFixed(1)} ${unit}` : "—";
    const bool = (value, yes, no) => value === true ? yes : value === false ? no : "확인 불가";
    const values = {
      current: temperature(attrs.current_temperature), target: temperature(attrs.temperature),
      mode: unavailable ? "확인 불가" : ({off:"OFF",heat:"HEAT",auto:"AUTO"}[state.state] || state.state),
      preset: unavailable ? "확인 불가" : ({home:"재실",away:"외출"}[attrs.preset_mode] || "—"),
      model: unavailable ? "확인 불가" : ({existing:"기본 학습",curve:"5분 커브 학습"}[attrs.learning_model] || "—"),
      heater: unavailable ? "확인 불가" : bool(attrs.heater_confirmed_on, "ON 확인", "OFF 확인"),
      pending: unavailable ? "확인 불가" : attrs.heater_command_pending === true ? "ON 확인 대기" : attrs.heater_command_pending === false ? "OFF 확인 대기" : attrs.heater_command_pending === null ? "없음" : "확인 불가",
      control: unavailable ? "확인 불가" : ({OFF:"정지",STARTUP:"시작 대기",HEATING:"난방 중",IDLE:"대기",FAULT:"오류 잠금",WAIT_MIN_ON:"최소 ON 대기",WAIT_MIN_OFF:"최소 OFF 대기",PREDICTIVE_ON:"예측 난방 시작",PREDICTIVE_OFF:"예측 난방 정지",PREDICTIVE_WAIT:"잔열 관측 대기"}[attrs.control_state] || attrs.control_state || "—"),
      faults: Array.isArray(attrs.faults) ? (attrs.faults.length ? attrs.faults.join(", ") : "없음") : "확인 불가",
    };
    for (const [key, value] of Object.entries(values)) {
      this.shadowRoot.querySelector(`[data-value="${key}"]`).textContent = value;
    }
    this.renderControls(state, unavailable, unit);
    this.renderCompletedCycle(state, unit);
    this.syncHistory();
    this.syncLearning();
  }

  // Completed comparisons own their predictions/errors; confidence needs matching OFF metadata.
  // Related: runtime.py last_peak_comparison, last_off_prediction and storage.py snapshots.
  renderCompletedCycle(state, unit) {
    const root = this.shadowRoot.querySelector(".completed-cycle");
    if (!root) return;
    const record = state?.attributes?.last_peak_comparison;
    const numeric = value => typeof value === "number" && Number.isFinite(value);
    const timestamp = value => numeric(value) && value > 0 && value * 1000 <= 8640000000000000;
    const valid = record && typeof record === "object" && !Array.isArray(record) && numeric(record.actual_peak);
    root.querySelector(".cycle-result").hidden = !valid;
    const status = root.querySelector(".cycle-status");
    status.textContent = !valid ? "완료된 사이클 기록이 없습니다." : ["unavailable","unknown"].includes(state.state) ? "현재 항목은 사용할 수 없습니다. 아래는 저장된 과거 완료 결과입니다." : "";
    status.hidden = !status.textContent;
    if (!valid) return;
    // Nested runtime diagnostics remain Celsius even when HA displays Climate values in Fahrenheit.
    const fahrenheit = unit === "°F";
    const temperature = value => numeric(value) ? `${(fahrenheit ? value * 9 / 5 + 32 : value).toFixed(1)} ${unit}` : "—";
    const errorText = value => {
      if (!numeric(value)) return "—";
      const converted = fahrenheit ? value * 9 / 5 : value;
      const rounded = Number(converted.toFixed(1));
      return `${rounded > 0 ? "+" : ""}${rounded.toFixed(1)} ${unit}`;
    };
    root.querySelector(".cycle-actual").textContent = temperature(record.actual_peak);
    const formatTime = value => timestamp(value) ? new Intl.DateTimeFormat("ko-KR",{timeZone:this._hass?.config?.time_zone || "Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value*1000)) : "기록 없음";
    for (const field of ["off_at","peak_at","completed_at"]) root.querySelector(`[data-cycle-time="${field}"]`).textContent = formatTime(record[field]);
    const off = state.attributes.last_off_prediction;
    const matched = timestamp(record.off_at) && timestamp(off?.off_at) && record.off_at === off.off_at;
    for (const model of ["existing","curve"]) {
      const prediction = record.predictions?.[model];
      const confidence = matched && numeric(prediction) ? off.confidences?.[model] : null;
      root.querySelector(`[data-cycle="${model}-prediction"]`).textContent = temperature(prediction);
      root.querySelector(`[data-cycle="${model}-error"]`).textContent = numeric(prediction) ? errorText(record.errors?.[model]) : "—";
      root.querySelector(`[data-cycle="${model}-confidence"]`).textContent = numeric(confidence) && confidence >= 0 && confidence <= 1 ? `${Math.round(confidence*100)}%` : "—";
    }
    root.querySelector(".cycle-note").textContent = `오차 = 예측 − 실제 · —는 기록 없음${matched ? "" : " · 같은 회차의 OFF 기록을 확인할 수 없어 신뢰도는 표시하지 않습니다."}`;
  }

  // Read full Climate attribute history: compressed lu/a/s or ordinary HA states.
  // Related: climate.py heater_confirmed_on and HA history/history_during_period.
  startGraphLifecycle() {
    clearInterval(this.historyTimer);
    this.historyTimer = setInterval(() => {
      if (document.visibilityState !== "hidden" && ["dashboard", "history"].includes(this.activeTab) && this.historyKey) this.loadHistory();
      if (document.visibilityState !== "hidden" && ["dashboard", "learning"].includes(this.activeTab) && this.learningKey) this.loadLearning();
    }, 60000);
    this.historyObserver?.disconnect();
    this.historyObserver = new ResizeObserver(() => {this.drawHistory();this.drawLearning();});
    this.shadowRoot.querySelectorAll(".history-chart,.learning-chart").forEach(chart => this.historyObserver.observe(chart));
  }

  // Query persisted in-memory buckets instead of adding high-volume sensor attributes.
  // Related: curve_api.py read permission checks and curve_memory.py five-minute deltas.
  syncLearning() {
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    const state = this.selectedState();
    const valid = this.isConnected && this.registryStatus === "ready" && entry && state && this._hass?.connection?.connected !== false;
    const key = valid ? JSON.stringify([entry.unique_id,entry.entity_id,this.generation,this._hass?.config?.unit_system?.temperature,state.attributes?.curve_learning_counts,state.attributes?.curve_rejected_counts]) : null;
    if (key === this.learningKey) return;
    this.learningKey = key;
    this.learningRequest += 1;
    this.learningData = null;
    this.learningStatus = "idle";
    this.drawLearning();
    if (key) this.loadLearning();
  }

  async loadLearning() {
    if (!this.learningKey || !this.isConnected) return;
    const key = this.learningKey, request = ++this.learningRequest;
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    this.learningStatus = "loading";
    this.drawLearning();
    let timeout;
    try {
      const result = await Promise.race([
        this._hass.callWS({type:"adaptive_floor_heating/curve_memory",entity_id:entry.entity_id}),
        new Promise((resolve,reject) => {timeout=setTimeout(()=>reject(new Error("timeout")),15000);}),
      ]);
      if (key!==this.learningKey || request!==this.learningRequest || !this.isConnected) return;
      this.learningData = result?.status === "ready" ? result : null;
      this.learningStatus = result?.status === "ready" ? "ready" : "unavailable";
    } catch {
      if (key!==this.learningKey || request!==this.learningRequest || !this.isConnected) return;
      this.learningData = null;
      this.learningStatus = "error";
    } finally {
      clearTimeout(timeout);
      if (key===this.learningKey && request===this.learningRequest && this.isConnected) this.drawLearning();
    }
  }

  drawLearning() {
    const curve = this.learningData?.curves?.[this.learningKind];
    const numeric = value => typeof value === "number" && Number.isFinite(value);
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    const factor = unit === "°F" ? 1.8 : 1;
    const points = layer => Array.isArray(curve?.[layer]) ? curve[layer].filter(point=>Number.isInteger(point.index)&&point.index>=0&&numeric(point.delta)).map(point=>({...point,minutes:(point.index+1)*5,delta:point.delta*factor})).sort((a,b)=>a.index-b.index) : [];
    const layers = {current:points("current"),long_term:points("long_term")};
    const values = [...layers.current,...layers.long_term].map(point=>point.delta);
    const notices = {idle:"표시할 항목을 선택해 주세요.",loading:"학습 커브를 불러오는 중입니다.",unavailable:"학습 저장소를 사용할 수 없습니다.",error:"학습 커브를 조회할 수 없습니다. 통합 버전과 조회 권한을 확인해 주세요."};
    const confidence = value=>numeric(value)&&value>=0&&value<=1?`${Math.round(value*100)}%`:"—";
    this.shadowRoot.querySelectorAll(".learning-chart").forEach(chart=>{
      const box = chart.parentElement;
      box.querySelector(".learning-kind").value=this.learningKind;
      box.querySelector(".learning-refresh").disabled=!this.learningKey||this.learningStatus==="loading";
      const notice=box.querySelector(".learning-notice");
      notice.textContent=notices[this.learningStatus] || (!values.length?"이 종류의 학습된 버킷이 없습니다.":"");notice.hidden=!notice.textContent;
      box.querySelector("[data-learning=counts]").textContent=Number.isInteger(curve?.accepted)&&Number.isInteger(curve?.rejected)?`${curve.accepted} / ${curve.rejected}`:"—";
      box.querySelector("[data-learning=current-confidence]").textContent=confidence(curve?.current_confidence);
      box.querySelector("[data-learning=long-confidence]").textContent=confidence(curve?.long_term_confidence);
      const detail=box.querySelector(".learning-detail");detail.textContent="5분 구간별 온도 변화량 · 누락 구간은 연결하지 않습니다.";
      chart.replaceChildren();
      if(this.learningStatus!=="ready"||!values.length||chart.clientWidth<100)return;
      const width=chart.clientWidth,height=260,left=57,right=width-12,top=30,bottom=205;
      const low=Math.min(0,values.reduce((a,b)=>Math.min(a,b),Infinity));
      const high=Math.max(0,values.reduce((a,b)=>Math.max(a,b),-Infinity));
      const padding=Math.max((high-low)*.15,.03*factor),min=low-padding,max=high+padding;
      const end=Math.max(10,...[...layers.current,...layers.long_term].map(point=>point.minutes));
      const x=value=>left+value/end*(right-left),y=value=>bottom-(value-min)/(max-min)*(bottom-top);
      const ns="http://www.w3.org/2000/svg",svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`${this.learningKind} Current와 Long-term 5분 구간 온도 변화 (${unit})`);
      const add=(tag,attrs,text)=>{const node=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([name,value])=>node.setAttribute(name,value));if(text!==undefined)node.textContent=text;svg.append(node);return node;};
      add("text",{x:left,y:17},`5분 온도 변화 (${unit})`);
      for(let i=0;i<4;i++){const value=min+(max-min)*i/3;add("path",{class:"grid",d:`M${left} ${y(value)}H${right}`});add("text",{x:left-6,y:y(value)+4,"text-anchor":"end"},value.toFixed(2));}
      const ticks=width<400?3:5;
      for(let i=0;i<ticks;i++){const minute=end*i/(ticks-1);add("text",{x:x(minute),y:227,"text-anchor":i===0?"start":i===ticks-1?"end":"middle"},Math.round(minute));}
      add("text",{x:(left+right)/2,y:251,"text-anchor":"middle"},this.learningKind==="COOLING"?"최고점부터 경과시간 (분)":"난방 ON부터 경과시간 (분)");
      for(const [layer,rows] of Object.entries(layers)){
        let d="",previous=-2;
        rows.forEach(point=>{d+=`${point.index===previous+1?"L":"M"}${x(point.minutes)} ${y(point.delta)}`;previous=point.index;});
        add("path",{class:layer,d,"data-learning-series":layer});
        rows.forEach(point=>{
          add("circle",{cx:x(point.minutes),cy:y(point.delta),r:3,fill:layer==="current"?"var(--primary-color, #03a9f4)":"var(--primary-text-color, #253549)","data-learning-point":`${layer}:${point.index}`});
        });
      }
      const show=event=>{
        const rect=svg.getBoundingClientRect(),minute=Math.max(0,Math.min(end,((event.clientX-rect.left)*width/rect.width-left)/(right-left)*end));
        const indices=[...new Set([...layers.current,...layers.long_term].map(point=>point.index))];
        const index=indices.reduce((nearest,item)=>Math.abs((item+1)*5-minute)<Math.abs((nearest+1)*5-minute)?item:nearest,indices[0]);
        const info=Object.entries(layers).map(([layer,rows])=>{const point=rows.find(item=>item.index===index);return `${layer==="current"?"Current":"Long-term"} ${point?`${point.delta.toFixed(3)} ${unit} · 증거 ${point.samples} · 승격 ${point.promotions}`:"기록 없음"}`;});
        detail.textContent=`${index*5}–${(index+1)*5}분 · ${info.join(" / ")}`;
      };
      svg.addEventListener("pointermove",show);svg.addEventListener("pointerdown",show);chart.append(svg);
    });
  }

  syncHistory() {
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    const valid = this.isConnected && this.registryStatus === "ready" && entry && this._hass?.states?.[entry.entity_id] && this._hass?.connection?.connected !== false;
    const key = valid ? `${entry.unique_id}:${entry.entity_id}:${this.historyHours}:${unit}:${this.generation}` : null;
    this.shadowRoot.querySelectorAll(".history-period").forEach(select => {select.value = String(this.historyHours);});
    if (key === this.historyKey) return;
    this.historyKey = key;
    this.historyRequest += 1;
    this.historyRows = [];
    this.historyStatus = "idle";
    this.drawHistory();
    if (key) this.loadHistory();
  }

  async loadHistory() {
    if (!this.historyKey || !this.isConnected) return;
    const key = this.historyKey;
    const request = ++this.historyRequest;
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    const end = Date.now();
    const start = end - this.historyHours * 3600000;
    this.historyStatus = "loading";
    this.drawHistory();
    let timeout;
    try {
      const response = await Promise.race([
        this._hass.callWS({type:"history/history_during_period", start_time:new Date(start).toISOString(), end_time:new Date(end).toISOString(), entity_ids:[entry.entity_id], include_start_time_state:true, significant_changes_only:false, minimal_response:false, no_attributes:false}),
        new Promise((resolve, reject) => {timeout = setTimeout(() => reject(new Error("timeout")), 15000);}),
      ]);
      if (key !== this.historyKey || request !== this.historyRequest || !this.isConnected) return;
      this.historyStart = start;
      this.historyEnd = end;
      this.historyRows = this.decodeHistory(response?.[entry.entity_id] || [], start, end);
      this.historyStatus = this.historyRows.length ? "ready" : "empty";
    } catch {
      if (key !== this.historyKey || request !== this.historyRequest || !this.isConnected) return;
      this.historyRows = [];
      this.historyStatus = "error";
    } finally {
      clearTimeout(timeout);
      if (key === this.historyKey && request === this.historyRequest && this.isConnected) this.drawHistory();
    }
  }

  decodeHistory(records, start, end) {
    if (!Array.isArray(records)) return [];
    const rows = records.map(record => {
      const timestamp = record.lu ?? record.last_updated ?? record.lc ?? record.last_changed;
      const time = typeof timestamp === "number" ? timestamp * 1000 : Date.parse(timestamp);
      const attrs = record.a ?? record.attributes ?? {};
      const state = record.s ?? record.state;
      const valid = !["unavailable", "unknown"].includes(state);
      const numeric = value => typeof value === "number" && Number.isFinite(value) ? value : null;
      return {time, current:valid ? numeric(attrs.current_temperature) : null, target:valid ? numeric(attrs.temperature) : null,
        heater:valid && typeof attrs.heater_confirmed_on === "boolean" ? attrs.heater_confirmed_on : null};
    }).filter(row => Number.isFinite(row.time) && row.time <= end).sort((a,b) => a.time-b.time);
    const before = rows.filter(row => row.time <= start).at(-1);
    const visible = rows.filter(row => row.time > start);
    if (before) visible.unshift({...before,time:start});
    return visible.filter((row,index) => index===0 || ["current","target","heater"].some(key => row[key]!==visible[index-1][key]));
  }

  drawHistory() {
    const root = this.shadowRoot;
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    const messages = {idle:this._hass?.connection?.connected===false?"HA 연결이 끊어져 이력을 표시할 수 없습니다.":"표시할 항목을 선택해 주세요.",loading:"이력을 불러오는 중입니다.",empty:"이 기간에 저장된 Climate 이력이 없습니다.",error:"이력을 조회할 수 없습니다. HA History·Recorder 설정과 조회 권한을 확인해 주세요."};
    root.querySelectorAll(".history-notice").forEach(notice => {notice.textContent = messages[this.historyStatus] || "";notice.hidden = !notice.textContent;});
    root.querySelectorAll(".history-refresh").forEach(button => {button.disabled = !this.historyKey || this.historyStatus === "loading";});
    root.querySelectorAll(".history-chart").forEach(chart => {
      chart.replaceChildren();
      chart.parentElement.querySelector(".graph-detail").textContent = "기록된 값은 다음 보고까지 유지해 표시합니다. 히터 확인 상태는 실제 열공급 측정값이 아닙니다.";
      if (this.historyStatus !== "ready" || !this.historyRows.length || chart.clientWidth < 100) return;
      const width = chart.clientWidth, height = 285;
      const left = 55, right = width-12, top = 25, bottom = 195;
      const rows = this.historyRows;
      const numbers = rows.flatMap(row => [row.current,row.target]).filter(value => value !== null);
      let low = numbers.length ? numbers.reduce((a,b)=>Math.min(a,b),Infinity) : 0;
      let high = numbers.length ? numbers.reduce((a,b)=>Math.max(a,b),-Infinity) : 1;
      const padding = Math.max((high-low)*.15,.3); low-=padding; high+=padding;
      const x = time => left+(time-this.historyStart)/(this.historyEnd-this.historyStart)*(right-left);
      const y = value => bottom-(value-low)/(high-low)*(bottom-top);
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns,"svg");
      svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`기록된 실내 온도, 목표온도와 히터 ON 확인 구간 (${unit})`);
      const add = (tag, attrs, text) => {const node = document.createElementNS(ns,tag);Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,value));if(text!==undefined)node.textContent=text;svg.append(node);return node;};
      add("text",{x:left,y:15},numbers.length?`온도 (${unit})`:"유효한 온도 기록 없음");
      if(numbers.length)for (let i=0;i<4;i++) {const value=low+(high-low)*i/3;add("path",{class:"grid",d:`M${left} ${y(value)}H${right}`});add("text",{x:left-7,y:y(value)+4,"text-anchor":"end"},value.toFixed(1));}
      const timeLabel = time => new Intl.DateTimeFormat("ko-KR",{timeZone:this._hass?.config?.time_zone || "Asia/Seoul",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(time));
      const ticks = width<420 ? 3 : 5;
      for(let i=0;i<ticks;i++){const time=this.historyStart+(this.historyEnd-this.historyStart)*i/(ticks-1);add("text",{x:x(time),y:272,"text-anchor":i===0?"start":i===ticks-1?"end":"middle"},timeLabel(time));}
      add("text",{x:left,y:218},"히터 확인");
      for (let i=0;i<rows.length;i++) {
        const row=rows[i], next=rows[i+1]?.time ?? this.historyEnd;
        add("rect",{x:x(row.time),y:226,width:Math.max(0,x(next)-x(row.time)),height:12,fill:row.heater===true?"var(--warning-color, #ed8a3b)":"var(--divider-color, #e1e6ec)",opacity:row.heater===null ? .2 : row.heater ? .6 : .35,"data-heater":row.heater===null?"unknown":String(row.heater)});
      }
      for(const key of ["current","target"]){
        let d="", previous=null;
        rows.forEach(row=>{const value=row[key];if(value===null){if(previous!==null)d+=`H${x(row.time)}`;previous=null;return;}d+=previous===null?`M${x(row.time)} ${y(value)}`:`H${x(row.time)}V${y(value)}`;previous=value;});
        if(previous!==null)d+=`H${right}`;
        add("path",{class:key==="current"?"actual":"target",d,"data-series":key});
      }
      const guide=add("line",{x1:left,x2:left,y1:top,y2:240,stroke:"var(--secondary-text-color, #657588)",visibility:"hidden"});
      const show = event => {
        const rect=svg.getBoundingClientRect();const pointer=Math.max(left,Math.min(right,(event.clientX-rect.left)*width/rect.width));
        const time=this.historyStart+(pointer-left)/(right-left)*(this.historyEnd-this.historyStart);
        let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>1;if(rows[mid].time<=time)lo=mid+1;else hi=mid;}const row=rows[lo-1];
        guide.setAttribute("x1",pointer);guide.setAttribute("x2",pointer);guide.setAttribute("visibility","visible");
        const temperature = value => value===null||value===undefined?"확인 불가":`${value.toFixed(1)} ${unit}`;
        chart.parentElement.querySelector(".graph-detail").textContent = `${timeLabel(time)} · 실내 ${temperature(row?.current)} · 목표 ${temperature(row?.target)} · 히터 ${row?.heater===true?"ON 확인":row?.heater===false?"OFF 확인":"확인 불가"}`;
      };
      svg.addEventListener("pointermove",show);svg.addEventListener("pointerdown",show);
      chart.append(svg);
    });
  }

  // Resolve the current registry ID at click time, never target raw heater switches.
  // Related: climate.py, controller.py validation and HA Climate service metadata.
  selectedState() {
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    return entry && this._hass?.states?.[entry.entity_id];
  }

  // Pointer gestures modify a draft and commit once on release through Climate services.
  // Related: climate.py temperature bounds/steps and sendCommand's state/busy validation.
  setupTemperatureArcs() {
    this.shadowRoot.querySelectorAll(".temperature-arc").forEach(svg => {
      const controls = svg.closest(".controls");
      let gesture;
      const context = () => `${this.generation}:${this.selectedEntry}:${this._hass?.config?.unit_system?.temperature || "°C"}`;
      const valid = () => gesture && gesture.context === context() && !controls.querySelector("input").disabled;
      const update = event => {
        const point = new DOMPoint(event.clientX,event.clientY).matrixTransform(svg.getScreenCTM().inverse());
        let angle = Math.atan2(point.y-162,point.x-160)*180/Math.PI;
        if (angle < 0) angle += 360;
        if (angle < 142.2) angle += 360;
        if (angle > 397.8) angle = angle < 450 ? 397.8 : 142.2;
        const attrs = this.selectedState().attributes;
        const raw = attrs.min_temp+(angle-142.2)/255.6*(attrs.max_temp-attrs.min_temp);
        const rounded = attrs.min_temp+Math.round((raw-attrs.min_temp)/this.targetStep(attrs))*this.targetStep(attrs);
        this.targetDraft = String(Math.round(Math.max(attrs.min_temp,Math.min(attrs.max_temp,rounded))*1000)/1000);
        this.targetDirty = true;
        this.renderState();
      };
      svg.addEventListener("pointerdown", event => {
        if (event.button !== 0 || controls.querySelector("input").disabled) return;
        const p = new DOMPoint(event.clientX,event.clientY).matrixTransform(svg.getScreenCTM().inverse());
        if (Math.abs(Math.hypot(p.x-160,p.y-162)-124)>30) return;
        gesture = {context:context(),draft:this.targetDraft,dirty:this.targetDirty,pointer:event.pointerId};
        svg.setPointerCapture(event.pointerId);
        update(event);
      });
      svg.addEventListener("pointermove", event => {if (valid() && gesture.pointer === event.pointerId) update(event);});
      svg.addEventListener("pointerup", event => {
        if (!gesture || gesture.pointer !== event.pointerId) return;
        const commit = valid(); gesture = null;
        if (commit) this.sendCommand({command:"temperature"});
      });
      const cancel = () => {
        if (valid()) {this.targetDraft=gesture.draft;this.targetDirty=gesture.dirty;}
        gesture=null;this.renderState();
      };
      svg.addEventListener("pointercancel",cancel);
      svg.addEventListener("lostpointercapture",cancel);
    });
  }

  targetStep(attrs) {
    return typeof attrs.target_temp_step === "number" && Number.isFinite(attrs.target_temp_step) && attrs.target_temp_step > 0 ? attrs.target_temp_step : 0.1;
  }

  // Match exact integration unique IDs and config entries, never names or entity ID prefixes.
  // Related: climate.py and select.py entity ownership; HA Select service dispatch.
  selector(kind) {
    if (!["learning_model", "prediction_mode"].includes(kind)) return null;
    const climate = this.registry.find(entry => entry.unique_id === this.selectedEntry);
    if (!climate?.config_entry_id) return null;
    const matches = (this.selectRegistry || []).filter(entry => entry.config_entry_id === climate.config_entry_id
      && entry.unique_id === `${climate.config_entry_id}_${kind}`);
    if (matches.length !== 1) return null;
    const entry = matches[0], state = this._hass?.states?.[entry.entity_id];
    if (!state || ["unknown", "unavailable"].includes(state.state)) return null;
    const allowed = kind === "learning_model" ? ["existing", "curve"] : ["eco", "balanced", "comfort"];
    const options = Array.isArray(state.attributes?.options) ? state.attributes.options.filter(option => allowed.includes(option)) : [];
    return {entry, state, options};
  }

  renderControls(state, unavailable, unit) {
    const attrs = state?.attributes || {};
    const key = `${this.selectedEntry}:${unit}`;
    if (this.draftKey !== key) {
      this.draftKey = key;
      this.targetDirty = false;
      this.commandMessage = "";
    }
    if (!this.targetDirty) this.targetDraft = typeof attrs.temperature === "number" && Number.isFinite(attrs.temperature) ? String(attrs.temperature) : "";
    const blocked = unavailable || this.commandBusy || this.registryStatus !== "ready" || this._hass?.connection?.connected === false;
    const validRange = Number.isFinite(attrs.min_temp) && Number.isFinite(attrs.max_temp) && attrs.min_temp <= attrs.max_temp;
    this.shadowRoot.querySelectorAll(".controls").forEach(controls => {
      controls.querySelector(".target-unit").textContent = unit;
      const input = controls.querySelector("input");
      if (input.value !== this.targetDraft) input.value = this.targetDraft;
      input.min = validRange ? attrs.min_temp : "";
      input.max = validRange ? attrs.max_temp : "";
      input.step = this.targetStep(attrs);
      input.disabled = blocked || !validRange || !(attrs.supported_features & 1);
      const draft = Number(this.targetDraft);
      const drawable = !input.disabled && this.targetDraft !== "" && Number.isFinite(draft) && attrs.max_temp > attrs.min_temp;
      const fraction = drawable ? Math.max(0,Math.min(1,(draft-attrs.min_temp)/(attrs.max_temp-attrs.min_temp))) : 0;
      const angle = (142.2+fraction*255.6)*Math.PI/180;
      controls.querySelector(".temperature-arc").setAttribute("aria-disabled",String(input.disabled));
      controls.querySelector(".arc-value").setAttribute("stroke-dasharray",`${fraction*100} 100`);
      controls.querySelectorAll(".arc-handle,.arc-hit").forEach(dot => {
        dot.setAttribute("cx",160+124*Math.cos(angle));dot.setAttribute("cy",162+124*Math.sin(angle));
        dot.style.display = drawable ? "" : "none";
      });
      controls.querySelector(".dial-current span").textContent = !unavailable && typeof attrs.current_temperature === "number" && Number.isFinite(attrs.current_temperature) ? `${attrs.current_temperature.toFixed(1)} ${unit}` : "—";
      controls.querySelector(".dial-status").textContent = unavailable ? "사용할 수 없음" : `${state.state.toUpperCase()} · ${attrs.heater_confirmed_on === true ? "난방 ON 확인" : attrs.heater_confirmed_on === false ? "난방 OFF 확인" : "히터 상태 확인 불가"}`;
      controls.classList.toggle("heating",!unavailable && attrs.heater_confirmed_on === true);
      controls.querySelectorAll("button").forEach(button => {
        const mode = button.dataset.mode;
        const preset = button.dataset.preset;
        button.disabled = blocked || (mode ? !attrs.hvac_modes?.includes(mode) : preset ? !attrs.preset_modes?.includes(preset) : input.disabled);
        if (mode || preset) button.setAttribute("aria-pressed", String(mode ? !unavailable && state.state === mode : !unavailable && attrs.preset_mode === preset));
      });
      controls.querySelectorAll("[data-selector]").forEach(select => {
        const sibling = this.selector(select.dataset.selector);
        const labels = {existing:"기본 학습",curve:"5분 커브 학습",eco:"eco",balanced:"balanced",comfort:"comfort"};
        // Show a placeholder only for unreported selections; related: select.py states.
        const known = sibling?.options.includes(sibling.state.state);
        select.replaceChildren(...(known ? [] : [new Option(sibling ? "선택 확인 불가" : "사용할 수 없음", "")]),
          ...(sibling?.options || []).map(option => new Option(labels[option], option)));
        select.value = known ? sibling.state.state : "";
        if (!known) select.options[0].disabled = true;
        select.disabled = blocked || !sibling?.options.length;
      });
      controls.querySelector(".command-message").textContent = this.commandBusy ? "요청 처리 중…" : this.commandMessage;
    });
  }

  async sendCommand(data) {
    const entry = this.registry.find(item => item.unique_id === this.selectedEntry);
    const state = this.selectedState();
    if (this.commandBusy || !this.isConnected || this.registryStatus !== "ready" || !entry || !state || ["unknown", "unavailable"].includes(state.state) || this._hass?.connection?.connected === false) return;
    const attrs = state.attributes || {};
    let service, payload, domain = "climate", target = entry.entity_id;
    if (data.command === "temperature") {
      const temperature = Number(this.targetDraft);
      if (!(attrs.supported_features & 1) || this.targetDraft === "" || !Number.isFinite(temperature) || !Number.isFinite(attrs.min_temp) || !Number.isFinite(attrs.max_temp) || temperature < attrs.min_temp || temperature > attrs.max_temp) {
        this.commandMessage = "허용 범위 안의 목표온도를 입력해 주세요.";
        this.renderState();
        return;
      }
      service = "set_temperature"; payload = {temperature};
    } else if (data.command === "mode" && attrs.hvac_modes?.includes(data.mode)) {
      service = "set_hvac_mode"; payload = {hvac_mode:data.mode};
    } else if (data.command === "preset" && attrs.preset_modes?.includes(data.preset)) {
      service = "set_preset_mode"; payload = {preset_mode:data.preset};
    } else if (data.command === "select") {
      const sibling = this.selector(data.kind);
      if (!sibling?.options.includes(data.option)) return;
      domain = "select"; target = sibling.entry.entity_id;
      service = "select_option"; payload = {option:data.option};
    } else return;
    const generation = this.generation;
    this.commandBusy = true;
    this.commandMessage = "";
    this.renderState();
    let timeout;
    try {
      await Promise.race([
        this._hass.callService(domain, service, {...payload, entity_id:target}),
        new Promise((resolve, reject) => {timeout = setTimeout(() => reject(new Error("timeout")), 15000);}),
      ]);
      if (generation !== this.generation || entry.unique_id !== this.selectedEntry) return;
      if (data.command === "temperature") this.targetDirty = false;
      this.commandMessage = "요청을 처리했습니다. 실제 상태는 HA 보고값으로 표시됩니다.";
    } catch (error) {
      if (generation !== this.generation || entry.unique_id !== this.selectedEntry) return;
      this.commandMessage = error?.message === "timeout" ? "응답 확인 시간이 지났습니다. 상태를 확인해 주세요." : "요청에 실패했습니다. 권한과 기기 상태를 확인해 주세요.";
    } finally {
      clearTimeout(timeout);
      if (generation === this.generation) {this.commandBusy = false; this.renderState();}
    }
  }

  // HA supplies responsive layout state; related: panel.py custom-panel registration.
  set narrow(value) {
    this.toggleAttribute("narrow", Boolean(value));
  }

  selectTab(name) {
    this.activeTab = name;
    this.shadowRoot.querySelectorAll("[role=tab]").forEach((tab) => {
      const selected = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    this.shadowRoot.querySelectorAll("[role=tabpanel]").forEach((panel) => {
      panel.hidden = panel.id !== name;
    });
    this.drawHistory();
    this.drawLearning();
  }
}

if (!customElements.get("adaptive-floor-heating-panel")) {
  customElements.define("adaptive-floor-heating-panel", AdaptiveFloorHeatingPanel);
}
