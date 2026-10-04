// Render the first-stage panel shell without data or commands; related: panel.py.
class AdaptiveFloorHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.activeTab = "dashboard";
  }

  connectedCallback() {
    if (this.shadowRoot.childElementCount) return;
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
        <section id="dashboard" role="tabpanel" aria-labelledby="tab-dashboard">
          <div class="overview">
            <article class="box"><h2>현재 온도 · 운전 제어</h2><div class="empty"><p>온도와 운전 제어가 연결되면 표시됩니다.</p></div></article>
            <article class="box"><h2>예측과 운전</h2><div class="empty"><p>학습 모델과 운전 상태가 연결되면 표시됩니다.</p></div></article>
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
