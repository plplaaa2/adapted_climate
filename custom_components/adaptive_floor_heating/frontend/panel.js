// Read registered Climate states without commands; related: panel.py, climate.py.
class AdaptiveFloorHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.activeTab = "dashboard";
    this.registry = [];
    this.selectedEntry = null;
    this.registryStatus = "loading";
    this.generation = 0;
  }

  connectedCallback() {
    if (this.shadowRoot.childElementCount) {
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
        .overview, .details { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .box { min-width: 0; padding: 20px; background: var(--card-background-color, #fff); border: 1px solid var(--divider-color, #e1e6ec); border-radius: 12px; }
        .empty { display: grid; place-items: center; min-height: 150px; text-align: center; }
        .graph { margin: 16px 0; }
        .graph .empty { min-height: 230px; }
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
            <article class="box"><h2>최근 완료 사이클</h2><div class="empty"><p>예측과 실제 최고온도 비교 연결 예정</p></div></article>
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
    this.shadowRoot.getElementById("room").addEventListener("change", (event) => {
      this.selectedEntry = event.target.value;
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
  }

  async startConnection() {
    const connection = this._hass?.connection;
    if (!connection || connection.connected === false || connection === this.connection) return;
    this.unsubscribeRegistry?.();
    this.unsubscribeRegistry = null;
    this.connection = connection;
    const generation = ++this.generation;
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
    room.disabled = !entries.length;
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
      model: unavailable ? "확인 불가" : ({existing:"기존 학습",curve:"5분 커브 학습"}[attrs.learning_model] || "—"),
      heater: unavailable ? "확인 불가" : bool(attrs.heater_confirmed_on, "ON 확인", "OFF 확인"),
      pending: unavailable ? "확인 불가" : attrs.heater_command_pending === true ? "ON 확인 대기" : attrs.heater_command_pending === false ? "OFF 확인 대기" : attrs.heater_command_pending === null ? "없음" : "확인 불가",
      control: unavailable ? "확인 불가" : ({OFF:"정지",STARTUP:"시작 대기",HEATING:"난방 중",IDLE:"대기",FAULT:"오류 잠금",WAIT_MIN_ON:"최소 ON 대기",WAIT_MIN_OFF:"최소 OFF 대기",PREDICTIVE_ON:"예측 난방 시작",PREDICTIVE_OFF:"예측 난방 정지",PREDICTIVE_WAIT:"잔열 관측 대기"}[attrs.control_state] || attrs.control_state || "—"),
      faults: Array.isArray(attrs.faults) ? (attrs.faults.length ? attrs.faults.join(", ") : "없음") : "확인 불가",
    };
    for (const [key, value] of Object.entries(values)) {
      this.shadowRoot.querySelector(`[data-value="${key}"]`).textContent = value;
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
  }
}

if (!customElements.get("adaptive-floor-heating-panel")) {
  customElements.define("adaptive-floor-heating-panel", AdaptiveFloorHeatingPanel);
}
