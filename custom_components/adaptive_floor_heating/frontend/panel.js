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
    this.cyclesRequest = 0;
    this.cyclesStatus = "idle";
    this.cyclesData = null;
    this.cyclesKind = "";
    this.cyclesAccepted = "";
    this.cyclesLimit = 30;
    this.selectedCycle = null;
    this.sensorRequest = 0;
    this.sensorRows = [];
    this.sensorHours = 6;
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
        .target-label { font-size:13px; color:var(--secondary-text-color); }
        .dial-target { display:block; font-size:44px; color:var(--primary-text-color); font-variant-numeric:tabular-nums; }
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
        .sensor-heading { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:20px; }
        .sensor-heading h3 { flex-basis:100%; margin:0; font-size:16px; }
        .sensor-choice { min-width:0; width:100%; }
        .sensor-chart svg { display:block; width:100%; }
        .sensor-chart text { font-size:12px; fill:var(--primary-text-color); }
        .sensor-chart .grid { fill:none; stroke:var(--divider-color); }
        .sensor-chart .actual { fill:none; stroke:var(--primary-color, #03a9f4); stroke-width:2; }
        .sensor-detail,.sensor-notice { font-size:12px; }
        .learning-chart { min-width: 0; }
        .learning-chart svg { display: block; width: 100%; height: 260px; touch-action: pan-y; }
        .learning-chart svg text { font-size: 12px; fill: var(--secondary-text-color, #657588); }
        .learning-chart .grid { stroke: var(--divider-color, #e1e6ec); stroke-width: 1; }
        .learning-chart .current { stroke: var(--primary-color, #03a9f4); stroke-width: 2; fill: none; }
        .learning-chart .long_term { stroke: var(--primary-text-color, #253549); stroke-width: 2; stroke-dasharray: 5 4; fill: none; }
        .learning-metrics { margin: 12px 0; font-size: 12px; }
        .learning-detail { font-size: 12px; margin-top: 12px; min-height: 40px; }
        /* Cycle-first analysis with responsive evidence tables; related: curve_api.py, curve_storage.py. */
        .analysis-intro { margin-bottom:20px; }
        .analysis-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0; }
        .analysis-stat { padding:16px; border:1px solid var(--divider-color,#e1e6ec); border-radius:12px; background:var(--card-background-color,#fff); }
        .analysis-stat h3 { margin:0 0 12px; font-size:14px; }
        .analysis-stat strong { display:block; font-size:24px; margin:6px 0; }
        .analysis-stat p { font-size:12px; }
        .analysis-latest { margin:0 0 20px; padding:12px 16px; border-left:3px solid var(--warning-color,#ed8a3b); background:var(--card-background-color,#fff); overflow-wrap:anywhere; }
        .analysis-workspace { display:grid; grid-template-columns:minmax(240px, .8fr) minmax(0,1.7fr); gap:16px; align-items:start; }
        .analysis-filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
        .analysis-filters label { flex:1 1 110px; font-size:12px; color:var(--secondary-text-color); }
        .analysis-filters select { display:block; width:100%; margin-top:4px; }
        .analysis-list { max-height:680px; overflow:auto; margin-top:12px; display:grid; gap:8px; }
        .analysis-record { width:100%; text-align:left; border:1px solid var(--divider-color,#e1e6ec); padding:12px; color:var(--primary-text-color); }
        .analysis-record[aria-pressed=true] { border-color:var(--primary-color,#03a9f4); background:var(--secondary-background-color,#edf3f8); }
        .analysis-record span { display:block; font-size:12px; margin-top:5px; overflow-wrap:anywhere; }
        .analysis-badge { border-radius:6px; padding:3px 8px; font-size:12px; background:var(--secondary-background-color,#edf3f8); }
        .analysis-verdict { margin:14px 0; padding:12px; border:1px solid var(--divider-color,#e1e6ec); border-radius:8px; overflow-wrap:anywhere; }
        .analysis-timeline { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin:16px 0; }
        .analysis-timeline div { border-top:3px solid var(--primary-color,#03a9f4); padding-top:8px; }
        .analysis-timeline dt { font-size:12px; }
        .analysis-timeline dd { text-align:left; font-size:12px; margin-top:6px; }
        .analysis-evidence { font-size:13px; margin:16px 0; }
        .analysis-subheading { font-size:14px; margin:24px 0 10px; }
        .analysis-table-wrap { max-width:100%; overflow:auto; }
        .analysis-table { width:100%; border-collapse:collapse; font-size:12px; color:var(--primary-text-color,#253549); }
        .analysis-table th,.analysis-table td { padding:10px 6px; text-align:left; border-bottom:1px solid var(--divider-color,#e1e6ec); overflow-wrap:anywhere; }
        .analysis-table th { color:var(--secondary-text-color); font-weight:500; }
        .analysis-detail { min-width:0; }
        .analysis-bucket-chart svg { display:block; width:100%; height:200px; }
        .analysis-bucket-chart text { font-size:11px; fill:var(--secondary-text-color,#657588); }
        .analysis-bucket-chart .grid { stroke:var(--divider-color,#e1e6ec); }
        .analysis-bucket-chart .actual { stroke:var(--primary-color,#03a9f4); stroke-width:2; fill:none; }
        .analysis-curves { margin-top:20px; }
        @media (max-width:900px) { .analysis-summary { grid-template-columns:repeat(2,minmax(0,1fr)); } .analysis-workspace { grid-template-columns:1fr; } .analysis-list { max-height:320px; } }
        @media (max-width:400px) { .analysis-timeline { grid-template-columns:repeat(2,minmax(0,1fr)); } .analysis-workspace>.box { padding:14px; } .analysis-summary { gap:8px; } .analysis-stat { padding:12px; } }
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
            <article class="box"><h2>운전 제어</h2></article>
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
    const makeControls = (suffix) => `<div class="controls thermostat"><div class="temperature-dial"><svg viewBox="0 0 320 280" class="temperature-arc" role="img" aria-label="목표온도 아크: 누르거나 드래그해 조절"><path class="arc-track" d="M62 238 A124 124 0 1 1 258 238"/><path class="arc-value" d="M62 238 A124 124 0 1 1 258 238" pathLength="100"/><circle class="arc-handle" r="14"/><circle class="arc-hit" r="24"/></svg><div class="dial-center"><div class="target-label">목표온도 <span class="target-unit"></span></div><output class="dial-target" aria-label="목표온도" aria-live="polite">—</output><div class="dial-current">현재 <span>—</span></div><div class="control-row dial-adjust"><button data-adjust="-1" aria-label="목표온도 낮추기">−</button><button data-adjust="1" aria-label="목표온도 높이기">+</button></div></div></div><p class="dial-status"></p><div class="control-row dial-modes" aria-label="운전 모드"><button data-command="mode" data-mode="off"><span aria-hidden="true">⏻</span>OFF</button><button data-command="mode" data-mode="heat"><span aria-hidden="true">♨</span>HEAT</button><button data-command="mode" data-mode="auto"><span aria-hidden="true">↻</span>AUTO</button></div><div class="control-row dial-presets" aria-label="재실 프리셋"><button data-command="preset" data-preset="home">재실</button><button data-command="preset" data-preset="away">외출</button></div><p class="command-message" role="status"></p></div>`;
    this.shadowRoot.querySelector(".overview .box").insertAdjacentHTML("beforeend", makeControls("dashboard"));
    this.shadowRoot.querySelector("#control .box").innerHTML = `<h2>운전 제어</h2>${makeControls("control")}`;
    this.setupTemperatureArcs();
    // Show actual diagnostic entities and Recorder history; related: sensor.py, diagnostics.py.
    this.shadowRoot.querySelector(".overview .box:nth-child(2)").insertAdjacentHTML("beforeend", `<dl class="sensor-values"></dl><div class="sensor-heading"><h3>진단 센서 이력</h3><select class="sensor-choice" aria-label="진단 센서 선택"></select><select class="sensor-period" aria-label="센서 그래프 기간"><option value="6">최근 6시간</option><option value="24">최근 24시간</option></select><button class="sensor-refresh">새로고침</button></div><p class="sensor-notice" role="status"></p><div class="sensor-chart"></div><p class="sensor-detail"></p>`);
    this.shadowRoot.querySelector(".sensor-choice").addEventListener("change",event=>{this.selectedSensor=event.target.value;this.syncSensors();});
    this.shadowRoot.querySelector(".sensor-period").addEventListener("change",event=>{this.sensorHours=Number(event.target.value);this.syncSensors();});
    this.shadowRoot.querySelector(".sensor-refresh").addEventListener("click",()=>this.loadSensorHistory());
    this.shadowRoot.querySelectorAll("[data-adjust]").forEach(button => button.addEventListener("click", () => {
      const attrs = this.selectedState()?.attributes || {};
      const current = Number(this.targetDraft);
      if (!Number.isFinite(current) || this.targetDraft === "") return;
      this.targetDraft = String(Math.round(Math.max(attrs.min_temp, Math.min(attrs.max_temp, current + Number(button.dataset.adjust) * this.targetStep(attrs))) * 1000) / 1000);
      this.targetDirty = true;
      this.renderState();
      this.sendCommand({command:"temperature"});
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
    // Build a complete investigation surface; all stored text is rendered via textContent below.
    // Related: curve_api.py authenticated cycle reads and curve_storage.py schema-five evidence.
    this.shadowRoot.querySelector("#learning").innerHTML = `<div class="analysis-intro"><h2>학습 분석</h2><p>관측된 사이클이 왜 학습되거나 제외됐는지 확인하고, 저장된 커브와 비교합니다.</p></div><div class="analysis-summary"></div><p class="analysis-latest"></p><div class="analysis-workspace"><article class="box analysis-records"><div class="learning-heading"><h2>사이클 기록</h2><button class="analysis-refresh">새로고침</button></div><div class="analysis-filters"><label>종류<select class="analysis-kind"><option value="">전체</option><option value="COLD_HEATING">Cold</option><option value="WARM_HEATING">Warm</option><option value="PREDICTIVE_WARM_HEATING">Predictive Warm</option><option value="COOLING">Cooling</option></select></label><label>학습 결과<select class="analysis-accepted"><option value="">전체</option><option value="false">제외</option><option value="true">승인</option></select></label><label>조회 개수<select class="analysis-limit"><option value="30">최근 30건</option><option value="100">최근 100건</option></select></label></div><p class="analysis-notice" role="status"></p><p class="analysis-count"></p><div class="analysis-list"></div><p class="cycle-note">Heating과 Cooling은 같은 운전에서도 별도 기록입니다. 진행 중인 사이클은 종료 후 표시됩니다.</p></article><article class="box analysis-detail"><h2>사이클 상세</h2><div class="analysis-detail-body"><p>기록을 선택해 주세요.</p></div></article></div><article class="box analysis-curves">${learningMarkup}</article>`;
    this.shadowRoot.querySelector(".analysis-refresh").addEventListener("click",()=>{this.loadCycles();this.loadLearning();});
    for(const [selector,field] of [[".analysis-kind","cyclesKind"],[".analysis-accepted","cyclesAccepted"],[".analysis-limit","cyclesLimit"]]){
      this.shadowRoot.querySelector(selector).addEventListener("change",event=>{this[field]=field==="cyclesLimit"?Number(event.target.value):event.target.value;this.selectedCycle=null;this.loadCycles();});
    }
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
    this.sensorRequest += 1; this.sensorKey=null; this.sensorRows=[]; this.sensorStatus="idle";
    this.drawSensorHistory();
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
    this.cyclesRequest += 1; this.cyclesData=null; this.cyclesStatus="idle";this.selectedCycle=null;
    this.drawLearning();
    this.drawCycleAnalysis();
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
      this.sensorRegistry = entries.filter(entry=>entry.platform==="adaptive_floor_heating"
        && entry.entity_id.startsWith("sensor.") && !entry.disabled_by && !entry.hidden_by);
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
      // Controls now own temperature/mode/preset presentation; related: renderControls.
      const display = this.shadowRoot.querySelector(`[data-value="${key}"]`);
      if (display) display.textContent = value;
    }
    this.renderControls(state, unavailable, unit);
    this.renderCompletedCycle(state, unit);
    this.syncHistory();
    this.syncLearning();
    this.syncSensors();
    this.drawHistory();
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
      if (document.visibilityState !== "hidden" && this.activeTab === "learning" && this.learningKey) this.loadCycles();
      if (document.visibilityState !== "hidden" && this.activeTab === "dashboard" && this.sensorKey) this.loadSensorHistory();
    }, 60000);
    this.historyObserver?.disconnect();
    this.historyObserver = new ResizeObserver(() => {this.drawHistory();this.drawLearning();this.drawSensorHistory();this.drawCycleBuckets();});
    this.shadowRoot.querySelectorAll(".history-chart,.learning-chart,.sensor-chart").forEach(chart => this.historyObserver.observe(chart));
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
    this.cyclesRequest += 1;this.cyclesData=null;this.cyclesStatus="idle";this.selectedCycle=null;
    this.drawLearning();
    this.drawCycleAnalysis();
    if (key) {this.loadLearning();if(this.activeTab==="learning")this.loadCycles();}
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
    this.drawCycleAnalysis(true);
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
      // Keep existing plots during refresh and skip unchanged geometry/data.
      // Related: loadLearning, startGraphLifecycle and frontend browser tests.
      if(this.learningStatus==="loading" && chart.firstChild)return;
      const signature=JSON.stringify([this.learningKey,this.learningKind,this.learningStatus,curve,unit,chart.clientWidth]);
      if(chart.plotSignature===signature)return;
      chart.plotSignature=signature;
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

  // Inspect stored evidence without dispatching heater services; related: curve_api.py, curve_storage.py.
  async loadCycles() {
    if(!this.learningKey || !this.isConnected || this.activeTab!=="learning")return;
    const key=this.learningKey,request=++this.cyclesRequest;
    const entry=this.registry.find(item=>item.unique_id===this.selectedEntry);
    const query={type:"adaptive_floor_heating/curve_cycles",entity_id:entry.entity_id,limit:this.cyclesLimit};
    if(this.cyclesKind)query.curve_type=this.cyclesKind;
    if(this.cyclesAccepted!=="")query.accepted=this.cyclesAccepted==="true";
    this.cyclesStatus="loading";this.cyclesData=null;this.drawCycleAnalysis();let timeout;
    try{
      const result=await Promise.race([this._hass.callWS(query),new Promise((resolve,reject)=>{timeout=setTimeout(()=>reject(Error("timeout")),15000);})]);
      if(key!==this.learningKey || request!==this.cyclesRequest || !this.isConnected)return;
      this.cyclesStatus=result?.status==="ready"&&Array.isArray(result.cycles)?"ready":"unavailable";
      this.cyclesData=this.cyclesStatus==="ready"?result:null;
    }catch{
      if(key!==this.learningKey || request!==this.cyclesRequest || !this.isConnected)return;
      this.cyclesStatus="error";this.cyclesData=null;
    }finally{
      clearTimeout(timeout);
      if(key===this.learningKey && request===this.cyclesRequest && this.isConnected)this.drawCycleAnalysis();
    }
  }

  cycleReason(code) {
    return ({ACCEPTED:"품질 조건을 통과해 학습에 반영했습니다.",INCOMPLETE_PEAK:"실제 최고점을 확정하지 못했습니다.",
      SENSOR_UNAVAILABLE:"관측 중 실내 온도 센서를 사용할 수 없었습니다.",HEATER_UNAVAILABLE:"관측 중 히터 상태를 확인할 수 없었습니다.",
      MANUAL_TARGET_CHANGE:"관측 중 사용자가 목표온도를 변경했습니다.",MANUAL_MODE_CHANGE:"관측 중 사용자가 운전 모드를 변경했습니다.",
      MANUAL_PRESET_CHANGE:"관측 중 재실·외출 프리셋이 변경되었습니다.",EXTERNAL_OVERRIDE:"외부 히터 조작으로 관측이 무효화되었습니다.",
      RELOAD_OR_SHUTDOWN:"통합 재로드 또는 종료로 관측이 중단되었습니다.",HA_SHUTDOWN:"Home Assistant 종료로 관측이 중단되었습니다.",
      OBSERVATION_OVERFLOW:"온도 관측 기록의 최대 개수를 초과했습니다.",AWAY_CYCLE:"외출 프리셋의 사이클은 학습에서 제외합니다.",
      INSUFFICIENT_BUCKETS:"유효한 5분 버킷이 2개 미만입니다.",INVALID_RESIDUAL_RISE:"잔열 상승량이 허용 범위를 벗어나거나 측정되지 않았습니다.",
      INVALID_PEAK_DELAY:"OFF부터 최고점까지 시간이 허용 범위를 벗어나거나 측정되지 않았습니다.",
      IMPLAUSIBLE_FIVE_MINUTE_DELTA:"5분 온도 변화량에 비정상 값이 있습니다.",CURVE_DEVIATION:"충분한 증거가 있는 기존 커브와 편차가 큽니다.",
      PEAK_CONFIRMED:"실제 온도 보고로 최고점 확정 조건을 충족했습니다.",PEAK_TIMEOUT:"OFF 후 관측 제한 시간 내 최고점 관측이 완료되지 않았습니다.",
      NEXT_ON_BEFORE_PEAK:"최고점 확정 전에 다음 난방이 시작되었습니다.",NEXT_ON:"다음 난방이 시작되어 냉각 관측을 종료했습니다.",
      THREE_HOUR_TIMEOUT:"최고점 이후 180분 냉각 관측을 종료했습니다.",SUSTAINED_WARMING:"지속적인 재상승으로 냉각 관측을 종료했습니다.",
      OBSERVATION_VALID:"관측 연속성",THRESHOLD_START:"시작 온도 기준 도달",COLD_START:"외출 복귀 후 난방 시작",
      PREDICTIVE_START:"예측 난방 시작",TARGET_REACHED:"목표온도 정지 기준 도달",UNKNOWN:"원인 기록 없음"})[code]||"이 사유의 설명은 아직 제공되지 않습니다.";
  }

  cycleKind(kind) {return ({COLD_HEATING:"Cold Heating",WARM_HEATING:"Warm Heating",PREDICTIVE_WARM_HEATING:"Predictive Warm Heating",COOLING:"Cooling"})[kind]||kind;}
  cycleKey(cycle) {return JSON.stringify([cycle.id,cycle.curve_type]);}
  cycleTime(value) {
    const time=typeof value==="string"?Date.parse(value):NaN;
    return Number.isFinite(time)?new Intl.DateTimeFormat("ko-KR",{timeZone:this._hass?.config?.time_zone||"Asia/Seoul",month:"2-digit",day:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(time)):"기록 없음";
  }

  drawCycleAnalysis(summaryOnly=false) {
    const root=this.shadowRoot.querySelector("#learning .analysis-summary");if(!root)return;
    const numeric=value=>typeof value==="number"&&Number.isFinite(value);
    const percent=value=>numeric(value)?`${Math.round(value*100)}%`:"—";
    root.replaceChildren();
    for(const kind of ["COLD_HEATING","WARM_HEATING","PREDICTIVE_WARM_HEATING","COOLING"]){
      const curve=this.learningData?.curves?.[kind],card=document.createElement("div");card.className="analysis-stat";
      const title=document.createElement("h3"),count=document.createElement("strong"),caption=document.createElement("p"),confidence=document.createElement("p");
      title.textContent=this.cycleKind(kind);count.textContent=curve?`${curve.accepted} / ${curve.rejected}`:"—";
      caption.textContent="승인 / 제외 · 누적 기록";confidence.textContent=`Current ${percent(curve?.current_confidence)} · Long-term ${percent(curve?.long_term_confidence)}`;
      card.append(title,count,caption,confidence);root.append(card);
    }
    if(summaryOnly)return;
    const cycles=this.cyclesData?.cycles||[];
    const latest=cycles.find(cycle=>cycle.accepted===false);
    this.shadowRoot.querySelector(".analysis-latest").textContent=latest?`조회 결과의 최근 제외 · ${this.cycleTime(latest.started_at)} · ${this.cycleKind(latest.curve_type)} · ${this.cycleReason(latest.quality_reason)} (${latest.quality_reason})`:"사이클별 승인·제외 판단은 아래 기록에서 확인할 수 있습니다. 신뢰도와 학습 품질 판정은 별개입니다.";
    this.shadowRoot.querySelector(".analysis-refresh").disabled=!this.learningKey||this.cyclesStatus==="loading";
    const notices={idle:"표시할 항목을 선택해 주세요.",loading:"사이클 기록을 불러오는 중입니다.",error:"조회에 실패했습니다. 통합 업데이트와 읽기 권한을 확인한 뒤 새로고침해 주세요.",unavailable:"학습 저장소를 사용할 수 없습니다."};
    this.shadowRoot.querySelector(".analysis-notice").textContent=notices[this.cyclesStatus]||(cycles.length?"":"조건에 맞는 종료된 사이클이 없습니다.");
    this.shadowRoot.querySelector(".analysis-count").textContent=this.cyclesStatus==="ready"?`최근 ${cycles.length}건 / 조건에 맞는 전체 ${this.cyclesData.total}건 · 시작 시각 표시, 관측 종료 최신순`:"";
    const list=this.shadowRoot.querySelector(".analysis-list");list.replaceChildren();
    if(!cycles.some(cycle=>this.cycleKey(cycle)===this.selectedCycle))this.selectedCycle=cycles.length?this.cycleKey(cycles[0]):null;
    for(const cycle of cycles){
      const button=document.createElement("button");button.className="analysis-record";button.dataset.cycleKey=this.cycleKey(cycle);
      button.setAttribute("aria-pressed",String(button.dataset.cycleKey===this.selectedCycle));
      const heading=document.createElement("b"),time=document.createElement("span"),reason=document.createElement("span");
      heading.textContent=`${cycle.accepted?"승인":"제외"} · ${this.cycleKind(cycle.curve_type)}`;time.textContent=this.cycleTime(cycle.started_at);
      reason.textContent=`${cycle.quality_reason} · 버킷 ${Number.isInteger(cycle.bucket_count)?cycle.bucket_count:"기록 없음"}`;
      button.append(heading,time,reason);button.addEventListener("click",()=>{this.selectedCycle=button.dataset.cycleKey;this.drawCycleAnalysis();});list.append(button);
    }
    this.drawCycleDetail(cycles.find(cycle=>this.cycleKey(cycle)===this.selectedCycle));
  }

  // Use absolute conversions for temperatures and scale-only conversions for deltas/slopes.
  // Related: curve_storage.py stored Celsius evidence and HA display temperature units.
  drawCycleDetail(cycle) {
    const root=this.shadowRoot.querySelector(".analysis-detail-body");if(!root)return;
    const key=cycle?this.cycleKey(cycle):"",opened=root.dataset.cycleKey===key?[...root.querySelectorAll("details")].map(element=>element.open):[];let detailIndex=0;
    root.dataset.cycleKey=key;root.replaceChildren();
    const node=(tag,text,className)=>{const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(className)element.className=className;if(tag==="details")element.open=Boolean(opened[detailIndex++]);return element;};
    if(!cycle){root.append(node("p",this.cyclesStatus==="loading"?"조회 중입니다.":"기록을 선택해 주세요."));return;}
    const unit=this._hass?.config?.unit_system?.temperature||"°C",factor=unit==="°F"?1.8:1;
    const numeric=value=>typeof value==="number"&&Number.isFinite(value);
    const value=(number,suffix="",absolute=false)=>numeric(number)?`${(absolute?number*factor+(unit==="°F"?32:0):number).toFixed(2)}${suffix?` ${suffix}`:""}`:"기록 없음";
    const delta=number=>value(numeric(number)?number*factor:null,unit);
    const reason=(label,code)=>`${label}: ${code||"기록 없음"} · ${code?this.cycleReason(code):""}`;
    root.append(node("span",`${cycle.accepted?"승인":"제외"} · ${this.cycleKind(cycle.curve_type)}`,"analysis-badge"));
    root.append(node("p",reason("최종 품질 판정",cycle.quality_reason),"analysis-verdict"));
    const timeline=node("dl",undefined,"analysis-timeline");
    for(const [label,field] of [["난방 ON","started_at"],["난방 OFF","off_at"],["실제 최고점","peak_at"],["관측 종료","ended_at"]]){const step=node("div");step.append(node("dt",label),node("dd",this.cycleTime(cycle[field])));timeline.append(step);}root.append(timeline);
    const metrics=node("dl",undefined,"analysis-evidence");
    for(const [label,text] of [["난방 지속시간",value(cycle.heating_duration_minutes,"분")],["OFF → 최고점",value(cycle.peak_delay_minutes,"분")],
      ["시작 온도",value(cycle.start_temperature,unit,true)],["OFF 온도",value(cycle.off_temperature,unit,true)],["최고 온도",value(cycle.peak_temperature,unit,true)],
      ["잔열 상승량",delta(cycle.residual_rise)],["OFF 시 온도 변화율",value(numeric(cycle.slope_at_off)?cycle.slope_at_off*factor:null,`${unit}/h`)],
      ["프리셋 / 모드",`${({home:"재실",away:"외출"})[cycle.preset]||cycle.preset} / ${cycle.mode}`],["원래 버킷 수",Number.isInteger(cycle.bucket_count)?String(cycle.bucket_count):"기록 없음"]]){metrics.append(node("dt",label),node("dd",text));}root.append(metrics);
    for(const [label,field] of [["시작 사유","start_reason"],["OFF 사유","off_reason"],["종료 사유","end_reason"]])root.append(node("p",reason(label,cycle[field]),"cycle-note"));
    const table=(headers,rows)=>{
      const wrap=node("div",undefined,"analysis-table-wrap"),element=node("table",undefined,"analysis-table"),head=node("thead"),tr=node("tr"),body=node("tbody");
      headers.forEach(text=>{const th=node("th",text);th.scope="col";tr.append(th);});head.append(tr);
      rows.forEach(cells=>{const row=node("tr");cells.forEach(text=>row.append(node("td",String(text))));body.append(row);});element.append(head,body);wrap.append(element);return wrap;
    };
    // Show the saved policy, never rewrite older cycles using today's confirmation thresholds.
    // Related: curve_storage.py peak_confirmation evidence and history.py PeakTracker.
    const confirmation=cycle.analysis?.peak_confirmation;
    if(confirmation){
      root.append(node("h3","최고점 확정 조건","analysis-subheading"));
      root.append(node("p","첫 하락 보고 후 다음 실제 보고가 더 내려갔을 때 확정합니다. 같은 온도 반복은 확정하지 않습니다.","cycle-note"));
      root.append(table(["항목","저장 당시 기준","확인값"],[
        ["최고온도 대비 첫 하락",delta(confirmation.minimum_drop_c),delta(confirmation.observed_first_drop_c)],
        ["다음 보고의 추가 하락",delta(confirmation.minimum_extra_drop_c),delta(confirmation.observed_extra_drop_c)],
        ["확정 시 최고온도 대비 하락","—",delta(confirmation.observed_drop_c)],
        ["실제 보고 수",String(confirmation.minimum_reports),Number.isInteger(confirmation.observed_reports)?String(confirmation.observed_reports):"기록 없음"],
        ["OFF 이후 관측 상한",value(confirmation.maximum_wait_minutes,"분"),"—"],
      ]));
    }
    root.append(node("h3","품질 검사 근거","analysis-subheading"));
    const checks=cycle.analysis?.checks;
    const checkLabels={OBSERVATION_VALID:"관측 연속성",AWAY_CYCLE:"프리셋",INSUFFICIENT_BUCKETS:"버킷 개수",INVALID_RESIDUAL_RISE:"잔열 상승량",INVALID_PEAK_DELAY:"최고점 지연",IMPLAUSIBLE_FIVE_MINUTE_DELTA:"5분 변화량 최대 절댓값",CURVE_DEVIATION:"기존 커브 편차"};
    if(Array.isArray(checks)){
      root.append(node("p","저장 당시 기준과 관측값입니다. 최종 판정은 첫 제외 사유를 따르며, 아래는 각 조건의 검사 근거입니다.","cycle-note"));
      const formatCheck=(number,check)=>check.unit==="°C"?delta(number):value(number,check.unit==="minutes"?"분":check.unit==="buckets"?"개":"");
      root.append(table(["검사 항목","실제 관측","허용 기준","결과"],checks.map(check=>{
        let actual=formatCheck(check.actual,check),allowed=numeric(check.min)&&numeric(check.max)?`${formatCheck(check.min,check)} ~ ${formatCheck(check.max,check)}`:numeric(check.min)?`${formatCheck(check.min,check)} 이상`:numeric(check.max)?`${formatCheck(check.max,check)} 이하`:"—";
        if(check.code==="OBSERVATION_VALID"){actual=check.actual||"무효화 사유 없음";allowed="무효화 사유 없음";}
        if(check.code==="AWAY_CYCLE"){actual=({home:"재실",away:"외출"})[check.actual]||check.actual;allowed="외출 이외";}
        if(check.nonfinite)actual+=" · 비정상 수치 포함";
        if(check.code==="CURVE_DEVIATION"){
          actual=`누적 편차 ${formatCheck(check.actual,check)} · 비교 ${check.comparable_count}개 · 큰 편차 ${check.large_count}개`;
          allowed=`누적 ≤ ${formatCheck(check.max,check)} · ${delta(check.bucket_difference_limit)} 초과 버킷 ${check.large_count_limit}개 미만`;
        }
        return [checkLabels[check.code]||check.code,actual,allowed,check.applicable===false?"비교 증거 부족":check.passed?"통과":"미충족"];
      })));
      const deviation=checks.find(check=>check.code==="CURVE_DEVIATION");
      if(deviation?.compared?.length){const details=node("details");details.append(node("summary","기존 커브와 비교한 버킷"),table(["구간","실측 변화","기준 변화"],deviation.compared.map(row=>[`${row.index*5}–${(row.index+1)*5}분`,delta(row.actual),delta(row.reference)])));root.append(details);}
    }else root.append(node("p","구형 기록에는 당시 측정값과 허용 기준이 저장되지 않았습니다. 사유 코드와 시각만 확인할 수 있습니다."));
    root.append(node("h3","학습 반영 결과","analysis-subheading"));
    const learning=cycle.analysis?.learning;
    if(learning){
      const percent=number=>numeric(number)?`${(number*100).toFixed(1)}%`:"기록 없음";
      root.append(node("p",learning.applied?`Current에 반영됨 · Long-term ${learning.promoted?"승격됨":"승격 없음"}`:"제외되어 Current·Long-term에 반영하지 않았습니다."));
      const before=learning.before?.diagnostics||{},after=learning.after?.diagnostics||{};
      root.append(table(["신뢰도 (저장 시점)","반영 전","반영 후"],[
        ["Current 커브",percent(before.current_confidence),percent(after.current_confidence)],
        ["Long-term 커브",percent(before.long_term_confidence),percent(after.long_term_confidence)],
        ["Current OFF 응답",percent(before.off_current_confidence),percent(after.off_current_confidence)],
        ["Long-term OFF 응답",percent(before.off_long_term_confidence),percent(after.off_long_term_confidence)],
      ]));
      const rows=learning.after?.current||[],old=learning.before?.current||[];
      if(rows.length){const details=node("details");details.append(node("summary","Current 버킷 반영 전후"),table(["구간","반영 전","반영 후","증거 수"],rows.map(row=>[`${row.index*5}–${(row.index+1)*5}분`,delta(old.find(item=>item.index===row.index)?.delta),delta(row.delta),row.samples])));root.append(details);}
    }else root.append(node("p","구형 기록에는 커브 반영 전후·신뢰도 변화·승격 여부가 저장되지 않았습니다."));
    root.append(node("h3","5분 관측 버킷","analysis-subheading"));
    root.append(node("p",cycle.curve_type==="COOLING"?"최고점부터 경과시간 · 각 구간 온도 변화량":"난방 ON부터 경과시간 · OFF 이후 최고점까지의 관측도 포함합니다.","cycle-note"));
    if(cycle.buckets?.length){
      root.append(node("div",undefined,"analysis-bucket-chart"));
      const details=node("details");details.append(node("summary",`전체 ${cycle.buckets.length}개 버킷 보기`),table(["구간","온도 변화량"],cycle.buckets.map(bucket=>[`${bucket.index*5}–${(bucket.index+1)*5}분`,delta(bucket.delta)])));root.append(details);
    }else root.append(node("p",({expired:"보존기간 7일이 지나 원본 버킷이 삭제되었습니다. 사이클 사유와 저장된 분석 정보는 유지됩니다.",empty:"이 사이클에는 유효한 버킷이 없습니다.",unavailable:"원본 버킷이 없습니다. 구형 기록은 보존기간 경과 여부와 원래 개수를 확인할 수 없습니다."})[cycle.raw_status]||"원본 버킷을 확인할 수 없습니다."));
    root.append(node("p",`사이클 ID: ${cycle.id}`,"cycle-note"));
    this.drawCycleBuckets();
  }

  drawCycleBuckets() {
    const chart=this.shadowRoot.querySelector(".analysis-bucket-chart");if(!chart)return;
    const cycle=this.cyclesData?.cycles?.find(item=>this.cycleKey(item)===this.selectedCycle);
    const unit=this._hass?.config?.unit_system?.temperature||"°C",factor=unit==="°F"?1.8:1;
    const rows=(cycle?.buckets||[]).filter(row=>Number.isInteger(row.index)&&typeof row.delta==="number"&&Number.isFinite(row.delta)).map(row=>({...row,delta:row.delta*factor}));
    chart.replaceChildren();const width=chart.clientWidth;if(width<100||!rows.length)return;
    const left=52,right=width-12,top=25,bottom=158,low=Math.min(0,...rows.map(row=>row.delta)),high=Math.max(0,...rows.map(row=>row.delta)),pad=Math.max((high-low)*.15,.03*factor),min=low-pad,max=high+pad,end=Math.max(10,...rows.map(row=>(row.index+1)*5));
    const x=minute=>left+minute/end*(right-left),y=delta=>bottom-(delta-min)/(max-min)*(bottom-top),ns="http://www.w3.org/2000/svg";
    const svg=document.createElementNS(ns,"svg");svg.setAttribute("viewBox",`0 0 ${width} 200`);svg.setAttribute("role","img");
    svg.setAttribute("aria-label",`사이클 5분 온도 변화 (${unit}), ${rows.length}개 버킷`);
    const add=(tag,attrs,text)=>{const element=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([key,value])=>element.setAttribute(key,value));if(text!==undefined)element.textContent=text;svg.append(element);return element;};
    for(let index=0;index<3;index++){const value=min+(max-min)*index/2;add("path",{class:"grid",d:`M${left} ${y(value)}H${right}`});add("text",{x:left-5,y:y(value)+4,"text-anchor":"end"},value.toFixed(2));}
    for(let index=0;index<3;index++)add("text",{x:x(end*index/2),y:178,"text-anchor":index===0?"start":index===2?"end":"middle"},`${Math.round(end*index/2)}분`);
    add("text",{x:left,y:15},`5분 온도 변화 (${unit})`);let path="",previous=-2;
    for(const row of rows){path+=`${row.index===previous+1?"L":"M"}${x((row.index+1)*5)} ${y(row.delta)}`;previous=row.index;}
    add("path",{class:"actual",d:path});
    for(const row of rows){const circle=add("circle",{cx:x((row.index+1)*5),cy:y(row.delta),r:3,fill:"var(--primary-color,#03a9f4)"});const title=document.createElementNS(ns,"title");title.textContent=`${row.index*5}–${(row.index+1)*5}분: ${row.delta.toFixed(3)} ${unit}`;circle.append(title);}
    chart.append(svg);
  }

  // Resolve room-owned numeric sensors by registry identity; use entity units without conversion.
  // Related: sensor.py unique IDs and Recorder history/history_during_period.
  syncSensors() {
    const select=this.shadowRoot.querySelector(".sensor-choice");if(!select)return;
    const climate=this.registry.find(entry=>entry.unique_id===this.selectedEntry);
    const connected=this.isConnected && this.registryStatus==="ready" && this._hass?.connection?.connected!==false;
    const sensors=connected && climate?.config_entry_id ? (this.sensorRegistry||[]).filter(entry=>{
      const state=this._hass.states[entry.entity_id], attrs=state?.attributes||{};
      return entry.config_entry_id===climate.config_entry_id && state && !["timestamp","enum"].includes(attrs.device_class)
        && (attrs.unit_of_measurement || (state.state.trim()!=="" && Number.isFinite(Number(state.state))));
    }) : [];
    const primary={temperature_slope:"실내 온도 변화율",learned_heating_response_delay:"학습 반응 지연",learned_residual_rise:"학습 잔열 상승량",learned_peak_delay:"학습 최고점 지연",off_predicted_peak_existing:"기본 학습 예상 최고온도",off_predicted_peak_curve:"커브 예상 최고온도",off_prediction_confidence:"예측 신뢰도"};
    const label=entry=>primary[entry.unique_id.slice((climate?.config_entry_id?.length||0)+1)] || this._hass.states[entry.entity_id]?.attributes?.friendly_name || entry.name || entry.entity_id;
    const format=state=>state && state.state.trim()!=="" && Number.isFinite(Number(state.state)) ? `${Number(state.state).toLocaleString("ko-KR",{maximumFractionDigits:4})} ${state.attributes?.unit_of_measurement||""}`.trim() : "—";
    const metrics=this.shadowRoot.querySelector(".sensor-values");metrics.replaceChildren();
    for(const [key,name] of Object.entries(primary)){
      const entry=sensors.find(item=>item.unique_id===`${climate?.config_entry_id}_${key}`);
      const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=name;dd.textContent=format(entry&&this._hass.states[entry.entity_id]);metrics.append(dt,dd);
    }
    this.sensorCandidates=sensors;this.sensorLabels=new Map(sensors.map(entry=>[entry.unique_id,label(entry)]));
    const discovery=connected && climate ? `${this.generation}:${climate.unique_id}:${this.sensorHours}:${sensors.map(entry=>`${entry.entity_id}:${this._hass.states[entry.entity_id]?.attributes?.unit_of_measurement||""}`).join("|")}`:null;
    if(discovery!==this.sensorDiscovery){this.sensorDiscovery=discovery;this.sensorAvailable=null;}
    if(!sensors.length)this.sensorAvailable=[];
    const available=this.sensorAvailable?sensors.filter(entry=>this.sensorAvailable.includes(entry.unique_id)):sensors;
    if(!available.some(entry=>entry.unique_id===this.selectedSensor))this.selectedSensor=available[0]?.unique_id||"";
    select.replaceChildren(...(this.sensorAvailable && available.length?available.map(entry=>new Option(label(entry),entry.unique_id)):[new Option(this.sensorAvailable?"그래프 이력이 있는 센서 없음":"센서 이력 확인 중","")]));
    select.value=this.sensorAvailable?this.selectedSensor:"";select.disabled=!this.sensorAvailable || !available.length;
    this.sensorEntry=available.find(entry=>entry.unique_id===this.selectedSensor);
    this.sensorUnit=this._hass?.states?.[this.sensorEntry?.entity_id]?.attributes?.unit_of_measurement||"";
    const key=sensors.length?`${discovery}:${this.selectedSensor}:${this.sensorUnit}`:null;
    if(key===this.sensorKey)return;
    this.sensorKey=key;this.sensorRequest++;this.sensorRows=[];this.sensorStatus="idle";this.drawSensorHistory();
    if(key)this.loadSensorHistory();
  }

  async loadSensorHistory() {
    if(!this.sensorKey || !this.isConnected)return;
    const key=this.sensorKey,request=++this.sensorRequest,discovery=this.sensorDiscovery;
    const candidates=this.sensorCandidates.map(entry=>({...entry,unit:this._hass.states[entry.entity_id]?.attributes?.unit_of_measurement||""}));
    const end=Date.now(),start=end-this.sensorHours*3600000;
    this.sensorStatus="loading";this.drawSensorHistory();let timeout;
    try{
      const response=await Promise.race([this._hass.callWS({type:"history/history_during_period",entity_ids:candidates.map(entry=>entry.entity_id),start_time:new Date(start).toISOString(),end_time:new Date(end).toISOString(),include_start_time_state:true,significant_changes_only:false,minimal_response:false,no_attributes:false}),new Promise((resolve,reject)=>{timeout=setTimeout(()=>reject(Error("timeout")),15000);})]);
      if(key!==this.sensorKey || request!==this.sensorRequest || !this.isConnected)return;
      const histories=new Map();
      for(const candidate of candidates){
      let attrs={};
      const rows=(Array.isArray(response?.[candidate.entity_id])?response[candidate.entity_id]:[]).map(record=>{
        const timestamp=record.lu??record.last_updated??record.lc??record.last_changed;
        const time=typeof timestamp==="number"?timestamp*1000:Date.parse(timestamp);
        attrs=record.a??record.attributes??attrs;
        const raw=record.s??record.state;
        const numeric=typeof raw==="number" || typeof raw==="string" && raw.trim()!=="";
        const value=numeric && Number.isFinite(Number(raw)) && (attrs.unit_of_measurement||"")===candidate.unit ? Number(raw):null;
        return {time,value};
      }).filter(row=>Number.isFinite(row.time)&&row.time<=end).sort((a,b)=>a.time-b.time);
      const seed=rows.filter(row=>row.time<=start).at(-1),visible=rows.filter(row=>row.time>start);
      if(seed)visible.unshift({...seed,time:start});
      if(visible.some(row=>row.value!==null))histories.set(candidate.unique_id,visible);
      }
      if(discovery!==this.sensorDiscovery)return;
      this.sensorAvailable=[...histories.keys()];
      const available=candidates.filter(entry=>histories.has(entry.unique_id));
      if(!histories.has(this.selectedSensor))this.selectedSensor=available[0]?.unique_id||"";
      this.sensorEntry=available.find(entry=>entry.unique_id===this.selectedSensor);
      this.sensorUnit=this.sensorEntry?.unit||"";
      const select=this.shadowRoot.querySelector(".sensor-choice");
      select.replaceChildren(...(available.length?available.map(entry=>new Option(this.sensorLabels.get(entry.unique_id),entry.unique_id)):[new Option("그래프 이력이 있는 센서 없음","")]));select.value=this.selectedSensor;select.disabled=!available.length;
      this.sensorRows=histories.get(this.selectedSensor)||[];this.sensorStart=start;this.sensorEnd=end;
      this.sensorStatus=this.sensorRows.length?"ready":"empty";
    }catch{
      if(key!==this.sensorKey || request!==this.sensorRequest || !this.isConnected)return;
      this.sensorRows=[];this.sensorStatus="error";
    }finally{clearTimeout(timeout);if(key===this.sensorKey && request===this.sensorRequest)this.drawSensorHistory();}
  }

  drawSensorHistory() {
    const chart=this.shadowRoot.querySelector(".sensor-chart");if(!chart)return;
    this.shadowRoot.querySelector(".sensor-notice").textContent=({idle:"센서를 선택해 주세요. 비활성 센서는 HA 엔티티 설정에서 활성화해야 합니다.",loading:"센서 이력을 불러오는 중입니다.",empty:"이 기간에 유효한 숫자 이력이 없습니다.",error:"센서 이력을 조회할 수 없습니다. Recorder 설정과 권한을 확인해 주세요."})[this.sensorStatus]||"";
    this.shadowRoot.querySelector(".sensor-refresh").disabled=!this.sensorKey || this.sensorStatus==="loading";
    // Preserve the current sensor plot while its replacement is being fetched.
    // Related: loadSensorHistory and test_sensor_frontend.cjs.
    if(this.sensorStatus==="loading" && chart.firstChild)return;
    const signature=JSON.stringify([this.sensorKey,this.sensorStatus,this.sensorRows,this.sensorStart,this.sensorEnd,this.sensorUnit,this._hass?.config?.time_zone,chart.clientWidth]);
    if(chart.plotSignature===signature)return;
    chart.plotSignature=signature;
    chart.replaceChildren();
    const detail=this.shadowRoot.querySelector(".sensor-detail");detail.textContent="기록값은 다음 보고까지 유지하며, 사용 불가·단위 변경 구간은 연결하지 않습니다.";
    if(this.sensorStatus!=="ready" || chart.clientWidth<100)return;
    const width=chart.clientWidth,left=64,right=width-12,top=30,bottom=180,unit=this.sensorUnit;
    const values=this.sensorRows.filter(row=>row.value!==null).map(row=>row.value);
    let low=values.reduce((a,b)=>Math.min(a,b),Infinity),high=values.reduce((a,b)=>Math.max(a,b),-Infinity);const padding=Math.max((high-low)*.15,Math.abs(high)*.02,.01);low-=padding;high+=padding;
    const x=time=>left+(time-this.sensorStart)/(this.sensorEnd-this.sensorStart)*(right-left),y=value=>bottom-(value-low)/(high-low)*(bottom-top);
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox",`0 0 ${width} 225`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`선택한 진단 센서 이력 (${unit})`);
    const add=(tag,attrs,text)=>{const node=document.createElementNS(svg.namespaceURI,tag);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,value));if(text!==undefined)node.textContent=text;svg.append(node);return node;};
    add("text",{x:left,y:16},`센서값 (${unit||"단위 없음"})`);
    for(let i=0;i<4;i++){const value=low+(high-low)*i/3;add("path",{class:"grid",d:`M${left} ${y(value)}H${right}`});add("text",{x:left-6,y:y(value)+4,"text-anchor":"end"},Number(value.toPrecision(3)).toString());}
    const timeLabel=time=>new Intl.DateTimeFormat("ko-KR",{timeZone:this._hass?.config?.time_zone||"Asia/Seoul",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(time));
    for(let i=0;i<3;i++){const time=this.sensorStart+(this.sensorEnd-this.sensorStart)*i/2;add("text",{x:x(time),y:216,"text-anchor":i===0?"start":i===2?"end":"middle"},timeLabel(time));}
    let d="",previous=null;this.sensorRows.forEach(row=>{if(row.value===null){if(previous!==null)d+=`H${x(row.time)}`;previous=null;return;}d+=previous===null?`M${x(row.time)} ${y(row.value)}`:`H${x(row.time)}V${y(row.value)}`;previous=row.value;});if(previous!==null)d+=`H${right}`;
    add("path",{class:"actual",d,"data-sensor-series":"true"});
    const show=event=>{const rect=svg.getBoundingClientRect(),pointer=Math.max(left,Math.min(right,(event.clientX-rect.left)*width/rect.width)),time=this.sensorStart+(pointer-left)/(right-left)*(this.sensorEnd-this.sensorStart);const row=this.sensorRows.filter(row=>row.time<=time).at(-1);detail.textContent=`${timeLabel(time)} · ${row?.value===null||row?.value===undefined?"확인 불가":`${row.value} ${unit}`}`;};
    svg.addEventListener("pointermove",show);svg.addEventListener("pointerdown",show);chart.append(svg);
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
    this.historyStart = null; this.historyEnd = null;
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
    // HA compressed records omit unchanged state/attributes; related: history frontend regression tests.
    let attrs = {}, state = null;
    const rows = records.map(record => {
      const timestamp = record.lu ?? record.last_updated ?? record.lc ?? record.last_changed;
      const time = typeof timestamp === "number" ? timestamp * 1000 : Date.parse(timestamp);
      attrs = record.a ?? record.attributes ?? attrs;
      state = record.s ?? record.state ?? state;
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

  // Display only the live, sampled OFF response supplied by the active predictor.
  // Related: runtime.py off_prediction, off_response.py relative minute/Celsius trajectories.
  displayedForecast() {
    const state=this.selectedState(),attrs=state?.attributes||{},forecast=attrs.off_prediction;
    const numeric=value=>typeof value==="number" && Number.isFinite(value);
    if(!this.isConnected || this._hass?.connection?.connected===false || this.registryStatus!=="ready"
      || !state || ["unknown","unavailable"].includes(state.state) || attrs.heater_confirmed_on!==true
      || !forecast || !["existing","curve"].includes(forecast.model) || !numeric(forecast.accepted_cycles) || forecast.accepted_cycles<1
      || !numeric(forecast.generated_at) || !numeric(forecast.temperature) || !numeric(forecast.predicted_peak)
      || !Array.isArray(forecast.trajectory) || forecast.trajectory.length<2 || forecast.trajectory.length>145)return null;
    const now=Date.now(),anchor=forecast.generated_at*1000;
    if(anchor>now+60000 || now-anchor>15*60000)return null;
    const unit=this._hass?.config?.unit_system?.temperature||"°C";
    const absolute=value=>unit==="°F"?value*1.8+32:value;
    let previous=-1;const points=[];
    for(const point of forecast.trajectory){
      if(!Array.isArray(point)||point.length!==2||!numeric(point[0])||!numeric(point[1])||point[0]<0||point[0]>1440||point[0]<=previous)return null;
      previous=point[0];points.push({time:anchor+point[0]*60000,value:absolute(forecast.temperature+point[1])});
    }
    if(points[0].time!==anchor)return null;
    return {points,peak:absolute(forecast.predicted_peak),model:forecast.model,confidence:numeric(forecast.confidence)?`${Math.round(forecast.confidence*100)}%`:"—"};
  }

  drawHistory() {
    const root = this.shadowRoot;
    const unit = this._hass?.config?.unit_system?.temperature || "°C";
    const forecast=this.displayedForecast();
    const messages = {idle:this._hass?.connection?.connected===false?"HA 연결이 끊어져 이력을 표시할 수 없습니다.":"표시할 항목을 선택해 주세요.",loading:"이력을 불러오는 중입니다.",empty:"이 기간에 저장된 Climate 이력이 없습니다.",error:"이력을 조회할 수 없습니다. HA History·Recorder 설정과 조회 권한을 확인해 주세요."};
    root.querySelectorAll(".history-notice").forEach(notice => {notice.textContent = messages[this.historyStatus] || "";notice.hidden = !notice.textContent;});
    root.querySelectorAll(".history-refresh").forEach(button => {button.disabled = !this.historyKey || this.historyStatus === "loading";});
    root.querySelectorAll(".history-chart").forEach(chart => {
      // HA pushes unrelated states too; retain SVG nodes until plot inputs change.
      // Related: renderState, loadHistory and test_history_frontend.cjs.
      if(this.historyStatus==="loading" && chart.firstChild)return;
      const signature=JSON.stringify([this.historyKey,this.historyStatus,this.historyRows,this.historyStart,this.historyEnd,forecast,unit,this._hass?.config?.time_zone,chart.clientWidth]);
      if(chart.plotSignature===signature)return;
      chart.plotSignature=signature;
      chart.replaceChildren();
      chart.parentElement.querySelector(".graph-detail").textContent = "기록된 값은 다음 보고까지 유지해 표시합니다. 히터 확인 상태는 실제 열공급 측정값이 아닙니다.";
      if ((!forecast && (this.historyStatus !== "ready" || !this.historyRows.length)) || !this.historyStart || chart.clientWidth < 100) return;
      const width = chart.clientWidth, height = 285;
      const left = 55, right = width-12, top = 25, bottom = 195;
      const rows = this.historyRows;
      const numbers = [...rows.flatMap(row => [row.current,row.target]).filter(value => value !== null),...(forecast?forecast.points.map(point=>point.value):[])];
      let low = numbers.length ? numbers.reduce((a,b)=>Math.min(a,b),Infinity) : 0;
      let high = numbers.length ? numbers.reduce((a,b)=>Math.max(a,b),-Infinity) : 1;
      const padding = Math.max((high-low)*.15,.3); low-=padding; high+=padding;
      const plotEnd=Math.max(this.historyEnd,forecast?.points.at(-1).time||0);
      const x = time => left+(time-this.historyStart)/(plotEnd-this.historyStart)*(right-left);
      const y = value => bottom-(value-low)/(high-low)*(bottom-top);
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns,"svg");
      svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`기록된 실내 온도, 목표온도와 히터 ON 확인 구간 (${unit})`);
      const add = (tag, attrs, text) => {const node = document.createElementNS(ns,tag);Object.entries(attrs).forEach(([key,value]) => node.setAttribute(key,value));if(text!==undefined)node.textContent=text;svg.append(node);return node;};
      add("text",{x:left,y:15},numbers.length?`온도 (${unit})`:"유효한 온도 기록 없음");
      if(numbers.length)for (let i=0;i<4;i++) {const value=low+(high-low)*i/3;add("path",{class:"grid",d:`M${left} ${y(value)}H${right}`});add("text",{x:left-7,y:y(value)+4,"text-anchor":"end"},value.toFixed(1));}
      const timeLabel = time => new Intl.DateTimeFormat("ko-KR",{timeZone:this._hass?.config?.time_zone || "Asia/Seoul",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(time));
      const ticks = width<420 ? 3 : 5;
      for(let i=0;i<ticks;i++){const time=this.historyStart+(plotEnd-this.historyStart)*i/(ticks-1);add("text",{x:x(time),y:272,"text-anchor":i===0?"start":i===ticks-1?"end":"middle"},timeLabel(time));}
      add("text",{x:left,y:218},"히터 확인");
      for (let i=0;i<rows.length;i++) {
        const row=rows[i], next=rows[i+1]?.time ?? this.historyEnd;
        add("rect",{x:x(row.time),y:226,width:Math.max(0,x(next)-x(row.time)),height:12,fill:row.heater===true?"var(--warning-color, #ed8a3b)":"var(--divider-color, #e1e6ec)",opacity:row.heater===null ? .2 : row.heater ? .6 : .35,"data-heater":row.heater===null?"unknown":String(row.heater)});
      }
      for(const key of ["current","target"]){
        let d="", previous=null;
        rows.forEach(row=>{const value=row[key];if(value===null){if(previous!==null)d+=`H${x(row.time)}`;previous=null;return;}d+=previous===null?`M${x(row.time)} ${y(value)}`:`H${x(row.time)}V${y(value)}`;previous=value;});
        if(previous!==null)d+=`H${x(this.historyEnd)}`;
        add("path",{class:key==="current"?"actual":"target",d,"data-series":key});
      }
      if(forecast){
        const d=forecast.points.map((point,index)=>`${index?"L":"M"}${x(point.time)} ${y(point.value)}`).join("");
        add("path",{d,fill:"none",stroke:"var(--warning-color, #ed8a3b)","stroke-width":2,"stroke-dasharray":"6 4","data-series":"forecast"});
        chart.parentElement.querySelector(".graph-detail").textContent=`점선: 지금 OFF 시 잔열 예측 · ${forecast.model==="curve"?"커브":"기본"} · Peak ${forecast.peak.toFixed(1)} ${unit} · 신뢰도 ${forecast.confidence}`;
      }
      const guide=add("line",{x1:left,x2:left,y1:top,y2:240,stroke:"var(--secondary-text-color, #657588)",visibility:"hidden"});
      const show = event => {
        const rect=svg.getBoundingClientRect();const pointer=Math.max(left,Math.min(right,(event.clientX-rect.left)*width/rect.width));
        const time=this.historyStart+(pointer-left)/(right-left)*(plotEnd-this.historyStart);
        let lo=0,hi=rows.length;while(lo<hi){const mid=(lo+hi)>>1;if(rows[mid].time<=time)lo=mid+1;else hi=mid;}const row=rows[lo-1];
        guide.setAttribute("x1",pointer);guide.setAttribute("x2",pointer);guide.setAttribute("visibility","visible");
        const temperature = value => value===null||value===undefined?"확인 불가":`${value.toFixed(1)} ${unit}`;
        let predicted=null;
        if(forecast && time>=forecast.points[0].time && time<=forecast.points.at(-1).time){
          const index=forecast.points.findIndex(point=>point.time>=time),end=forecast.points[index],start=forecast.points[Math.max(0,index-1)];
          predicted=end.time===start.time?end.value:start.value+(end.value-start.value)*(time-start.time)/(end.time-start.time);
        }
        chart.parentElement.querySelector(".graph-detail").textContent = `${timeLabel(time)} · 실내 ${temperature(time<=this.historyEnd?row?.current:null)} · 목표 ${temperature(time<=this.historyEnd?row?.target:null)} · 히터 ${time>this.historyEnd?"예측 구간":row?.heater===true?"ON 확인":row?.heater===false?"OFF 확인":"확인 불가"}${predicted===null?"":` · 지금 OFF 시 잔열 예측 ${temperature(predicted)}`}`;
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
      const valid = () => gesture && gesture.context === context() && !controls.temperatureDisabled;
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
        if (event.button !== 0 || controls.temperatureDisabled) return;
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
      controls.temperatureDisabled = blocked || !validRange || !(attrs.supported_features & 1) || this.targetDraft === "";
      const output = controls.querySelector(".dial-target");
      output.value = unavailable || this.targetDraft === "" ? "—" : Number(this.targetDraft).toFixed(1);
      const draft = Number(this.targetDraft);
      const drawable = !controls.temperatureDisabled && Number.isFinite(draft) && attrs.max_temp > attrs.min_temp;
      const fraction = drawable ? Math.max(0,Math.min(1,(draft-attrs.min_temp)/(attrs.max_temp-attrs.min_temp))) : 0;
      const angle = (142.2+fraction*255.6)*Math.PI/180;
      controls.querySelector(".temperature-arc").setAttribute("aria-disabled",String(controls.temperatureDisabled));
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
        button.disabled = blocked || (mode ? !attrs.hvac_modes?.includes(mode) : preset ? !attrs.preset_modes?.includes(preset) : controls.temperatureDisabled || (button.dataset.adjust === "-1" ? draft <= attrs.min_temp : draft >= attrs.max_temp));
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
      if (generation === this.generation) {if(data.command === "temperature")this.targetDirty=false;this.commandBusy = false; this.renderState();}
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
    if(name==="learning" && this.cyclesStatus==="idle")this.loadCycles();
  }
}

if (!customElements.get("adaptive-floor-heating-panel")) {
  customElements.define("adaptive-floor-heating-panel", AdaptiveFloorHeatingPanel);
}
