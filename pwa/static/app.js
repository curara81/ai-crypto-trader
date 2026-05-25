// AI 트레이더 PWA — frontend
const API = "";  // 같은 origin
const REFRESH_INTERVAL = 30000;  // 30초

let currentTab = "crypto";
let refreshTimer = null;

// ────────────────────────────────────────────
// 유틸
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }
function fmt(n, digits = 2) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return Number(n).toLocaleString("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtKrw(n) {
  if (!n) return "0";
  return Math.round(n).toLocaleString("ko-KR");
}
function fmtPct(n) {
  if (n === null || n === undefined) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${Number(n).toFixed(2)}%`;
}
function toast(msg, type = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show " + type;
  setTimeout(() => t.className = "toast", 2500);
}
async function api(path, opts = {}) {
  try {
    const r = await fetch(API + path, opts);
    return await r.json();
  } catch (e) {
    console.error(path, e);
    return null;
  }
}

// ────────────────────────────────────────────
// 탭 전환
function switchTab(tab) {
  currentTab = tab;
  $$(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-panel").forEach(p => p.classList.toggle("active", p.dataset.tab === tab));
  refreshCurrentTab();
}
$$(".tab-btn").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

// ────────────────────────────────────────────
// 상태바
async function updateStatus() {
  const h = await api("/api/health");
  if (!h) {
    $("#botDot").className = "status-dot offline";
    $("#botStatus").textContent = "연결 끊김";
    return;
  }
  const services = h.services || {};
  const allOk = Object.values(services).every(v => v);
  const someOk = Object.values(services).some(v => v);
  $("#botDot").className = "status-dot " + (allOk ? "online" : someOk ? "warn" : "offline");
  const count = Object.values(services).filter(v => v).length;
  const total = Object.keys(services).length;
  $("#botStatus").textContent = `서비스 ${count}/${total}`;

  if (h.killswitch) {
    $("#killswitchStatus").textContent = "🛑 비상정지";
    $("#killswitchBtn").classList.add("active");
  } else {
    $("#killswitchStatus").textContent = "정상 가동";
    $("#killswitchBtn").classList.remove("active");
  }
}

// ────────────────────────────────────────────
// 코인 탭
async function loadCrypto() {
  // status
  const s = await api("/api/crypto/status");
  if (s) {
    const profit = s.profit_total_krw || 0;
    const profitPct = s.profit_total_pct || 0;
    const profitEl = $("#cryptoProfit");
    profitEl.textContent = `${profit >= 0 ? "+" : ""}${fmtKrw(profit)} KRW`;
    profitEl.className = "hero-value " + (profit > 0 ? "positive" : profit < 0 ? "negative" : "");
    $("#cryptoProfitSub").textContent =
      `${s.closed_trades || 0}건 청산 · 승률 ${fmt(s.winrate, 1)}% · 평균 ${fmt(profitPct, 2)}%`;

    $("#openCount").textContent = s.open_trades.length;
    const posEl = $("#openPositions");
    if (s.open_trades.length === 0) {
      posEl.innerHTML = '<div class="empty">오픈 포지션 없음</div>';
    } else {
      posEl.innerHTML = s.open_trades.map(p => `
        <div class="position-item">
          <div>
            <div class="position-pair">${p.pair}</div>
            <div class="position-meta">${fmtKrw(p.stake_amount)} KRW · 손절 ${fmt(p.stoploss_pct, 2)}%</div>
          </div>
          <div class="position-pnl ${p.profit_pct > 0 ? "positive" : p.profit_pct < 0 ? "negative" : ""}">
            ${fmtPct(p.profit_pct)}<br>
            <span style="font-size:11px;opacity:0.7;">${fmtKrw(p.profit_abs)} KRW</span>
          </div>
        </div>
      `).join("");
    }
  }

  // decisions
  const d = await api("/api/crypto/decisions");
  if (d && d.decisions) {
    const el = $("#decisions");
    if (d.decisions.length === 0) {
      el.innerHTML = '<div class="empty">최근 결정 없음</div>';
    } else {
      el.innerHTML = d.decisions.map(x => `
        <div class="decision-item ${x.action}">
          <div class="decision-header">
            <span class="decision-action ${x.action}">${x.action} · ${x.pair}</span>
            <span class="decision-conf">conf ${fmt(x.confidence, 2)}</span>
          </div>
          <div class="decision-reason">${escapeHtml(x.reason || "")}</div>
          <div class="decision-ts">${x.ts}</div>
        </div>
      `).join("");
    }
  }

  // filters
  const f = await api("/api/crypto/filters");
  if (f) {
    const fg = f.fear_greed || {};
    const tw = f.time_window || {};
    $("#filterStatus").innerHTML = `
      <div class="filter-item ${fg.blocks_buy ? "blocked" : "active"}">
        <div class="filter-name">Fear & Greed</div>
        <div class="filter-value">${fg.score ?? "?"}</div>
        <div class="filter-name" style="margin-top:4px;">${fg.classification ?? ""}</div>
      </div>
      <div class="filter-item ${tw.blocked ? "blocked" : "active"}">
        <div class="filter-name">시간대</div>
        <div class="filter-value" style="font-size:14px;">${tw.blocked ? "차단" : "허용"}</div>
        <div class="filter-name" style="margin-top:4px;">${tw.reason || "정상 거래 시간"}</div>
      </div>
    `;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// v4.0.1: 영문 enum → 한국어 표시 매핑
const KO_MAP = {
  // risk_level
  low: "낮음", medium: "중간", high: "높음",
  // time_horizon
  short: "단기", long: "장기",
  // trend
  bullish: "🟢 상승", bearish: "🔴 하락", neutral: "⚪ 중립",
  // recommendation
  STRONG_BUY: "강력 매수", BUY: "매수", HOLD: "관망",
  SELL: "매도", STRONG_SELL: "강력 매도", AVOID: "회피",
  // current_setup
  accumulation: "매집", distribution: "분산",
  breakout: "돌파", breakdown: "붕괴",
  consolidation: "통합", range_bound: "박스권",
  uptrend: "상승추세", downtrend: "하락추세",
  sideways: "횡보",
};
function ko(v) {
  if (v === null || v === undefined) return "?";
  // medium은 risk/horizon 둘 다 사용 → 컨텍스트 구분 어려워 "중간"으로 통일
  return KO_MAP[v] || v;
}
// time_horizon 전용 (medium 의미 다름)
function koHorizon(v) {
  return ({ short: "단기", medium: "중기", long: "장기" })[v] || v;
}

// v4.6: 밸류에이션 판정
function ko_verdict(v) {
  return ({ undervalued: "저평가 💎", fair: "적정 ⚖️", overvalued: "고평가 ⚠️" })[v] || v;
}
function trend_icon(t) {
  return ({ growing: "📈", stable: "➡️", declining: "📉" })[t] || "";
}

// ────────────────────────────────────────────
// 봇 제어
async function ctrl(command) {
  if (command === "stop" && !confirm("봇을 정지하시겠어요?")) return;
  const r = await api("/api/crypto/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command }),
  });
  if (r && !r.error) {
    toast(`✓ ${command} 실행됨`, "success");
  } else {
    toast(`✗ 실패: ${r?.error || "unknown"}`, "error");
  }
  setTimeout(loadCrypto, 1500);
}

// KILLSWITCH 토글
$("#killswitchBtn").addEventListener("click", async () => {
  const isActive = $("#killswitchBtn").classList.contains("active");
  if (!isActive && !confirm("🛑 비상정지를 활성화하시겠어요?\n모든 신규 매수가 차단됩니다.")) return;
  const r = await api("/api/crypto/killswitch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "toggle" }),
  });
  if (r) toast(r.msg, r.killswitch ? "error" : "success");
  updateStatus();
});

// ────────────────────────────────────────────
// 해외주식 탭
async function loadUS() {
  const s = await api("/api/stocks/us/status");
  if (!s) return;
  $("#usMarketStatus").textContent = s.market_open ? "🟢 장 열림" : "🔴 장 마감";
  $("#usSub").textContent =
    `${s.alive ? "봇 가동 중" : "봇 정지"} · 누적 결정 ${s.decision_count}건`;
  const el = $("#usDecisions");
  if (!s.decisions || s.decisions.length === 0) {
    el.innerHTML = '<div class="empty">아직 결정 없음 (장 시작 후 5분 단위 분석)</div>';
  } else {
    el.innerHTML = s.decisions.map(x => `
      <div class="decision-item ${x.action}">
        <div class="decision-header">
          <span class="decision-action ${x.action}">${x.action} · ${x.symbol}</span>
          <span class="decision-conf">conf ${fmt(x.confidence, 2)}</span>
        </div>
        <div class="decision-reason">${escapeHtml(x.reason || "")}</div>
        <div class="decision-ts">${x.ts}</div>
      </div>
    `).join("");
  }
}

// ────────────────────────────────────────────
// 국내주식 탭
async function loadKR() {
  const k = await api("/api/stocks/kr/positions");
  if (!k) return;
  const el = $("#krPositions");
  if (k.error) {
    el.innerHTML = `<div class="empty">조회 실패: ${escapeHtml(k.error)}</div>`;
  } else if (!k.positions || k.positions.length === 0) {
    el.innerHTML = '<div class="empty">보유 종목 없음</div>';
  } else {
    el.innerHTML = k.positions.map(p => `
      <div class="position-item">
        <div class="kr-position-raw">${escapeHtml(p.raw)}</div>
      </div>
    `).join("");
  }
}

// ────────────────────────────────────────────
// 통합 탭
async function loadSummary() {
  const s = await api("/api/summary");
  if (!s) return;
  const totalKrw = (s.crypto?.profit_krw || 0);
  const profitEl = $("#summaryProfit");
  profitEl.textContent = `${totalKrw >= 0 ? "+" : ""}${fmtKrw(totalKrw)} KRW`;
  profitEl.className = "hero-value " + (totalKrw > 0 ? "positive" : totalKrw < 0 ? "negative" : "");
  $("#summarySub").textContent =
    `코인 ${s.crypto?.open_count || 0}개 보유 · 미국주식 오늘 ${s.us_stocks?.decisions_today || 0}건`;

  // Fear & Greed
  const fg = s.market?.fear_greed || {};
  const fgClass = fg.score <= 25 ? "extreme-fear" :
                  fg.score <= 45 ? "fear" :
                  fg.score <= 55 ? "neutral" :
                  fg.score <= 75 ? "greed" : "extreme-greed";
  $("#summaryFG").innerHTML = `
    <div class="fg-score ${fgClass}">${fg.score ?? "?"}</div>
    <div class="fg-label">${fg.classification || ""}</div>
  `;

  // 서비스 상태
  const h = await api("/api/health");
  if (h && h.services) {
    const labels = {
      "freqtrade-dryrun": "코인봇",
      "freqtrade-aibot": "텔레그램",
      "freqtrade-notifier": "알림",
      "freqtrade-analyzer": "성과분석",
      "freqtrade-health": "헬스체크",
      "freqtrade-dailyreport": "일일리포트",
      "kis-stockbot": "미주봇",
    };
    $("#serviceStatus").innerHTML = Object.entries(h.services).map(([k, v]) => `
      <div class="service-item ${v ? "on" : "off"}">${labels[k] || k}</div>
    `).join("");
  }
}

// ────────────────────────────────────────────
// v5.0: 분석 탭 — 코인/주식 + 영/한 + 지수 위젯
let currentMarket = "crypto";  // "crypto" | "stocks"
let currentLang = (typeof localStorage !== 'undefined' && localStorage.getItem('analysis_lang')) || 'ko';
let currentIndicesCat = null;
let _lastAnalysisResult = null;

function L(obj, base) {
  if (!obj) return '';
  return obj[base + '_' + currentLang] || obj[base + '_ko'] || obj[base] || '';
}
function Larr(obj, base) {
  if (!obj) return [];
  return obj[base + '_' + currentLang] || obj[base + '_ko'] || obj[base] || [];
}
function switchLang(lang) {
  if (lang !== 'ko' && lang !== 'en') return;
  currentLang = lang;
  try { localStorage.setItem('analysis_lang', lang); } catch(e) {}
  $$('.lang-btn').forEach(b => b.classList.toggle('active', b.dataset.lang === lang));
  if (_lastAnalysisResult) displayAnalysisResult(_lastAnalysisResult);
}

// v5.0: loadMovers는 loadAnalysis의 별칭 (새로고침 버튼용)
function loadMovers() { loadAnalysis(); }

// v5.0: 지수 위젯 — 마켓에 따라 서브탭 구성
function renderIndicesTabs() {
  const tabs = currentMarket === 'stocks'
    ? [
        {cat: 'us_index',  label: '📊 지수'},
        {cat: 'commodity', label: '🛢 원자재'},
        {cat: 'fx',        label: '💱 외환'},
        {cat: 'etf',       label: '📈 ETF'},
      ]
    : currentMarket === 'kr'
    ? [
        {cat: 'kr_index',  label: '📊 국내 지수'},
        {cat: 'kr_fx',     label: '💱 환율'},
        {cat: 'commodity', label: '🛢 원자재'},
      ]
    : [
        {cat: 'crypto_idx', label: '🪙 주요 코인'},
        {cat: 'commodity',  label: '🛢 원자재'},
        {cat: 'macro',      label: '🌐 매크로'},
      ];
  if (!currentIndicesCat || !tabs.find(t => t.cat === currentIndicesCat)) {
    currentIndicesCat = tabs[0].cat;
  }
  const tabsEl = $("#indicesTabs");
  if (tabsEl) {
    tabsEl.innerHTML = tabs.map(t => `
      <button class="indices-tab ${currentIndicesCat === t.cat ? 'active' : ''}"
              onclick="selectIndicesCat('${t.cat}')">${t.label}</button>
    `).join('');
  }
  const titleEl = $("#indicesTitle");
  if (titleEl) {
    titleEl.textContent = currentMarket === 'stocks'
      ? '📊 미국 시장 지수'
      : currentMarket === 'kr'
      ? '📊 국내 시장 지수'
      : '📊 코인·매크로 지수';
  }
}

function selectIndicesCat(cat) {
  currentIndicesCat = cat;
  renderIndicesTabs();
  loadIndices();
}

async function loadIndices() {
  if (!currentIndicesCat) renderIndicesTabs();
  const target = $("#indicesList");
  if (!target) return;
  target.innerHTML = '<div class="loading-spinner">📡 시세 로딩...</div>';
  const r = await api(`/api/analysis/indices?cat=${currentIndicesCat}`);
  if (!r || r.error) {
    target.innerHTML = `<div class="empty">로딩 실패: ${r?.error || ''}</div>`;
    return;
  }
  const items = (r.items || []).filter(x => x.price != null);
  if (items.length === 0) {
    target.innerHTML = '<div class="empty">데이터 없음</div>';
    return;
  }
  target.innerHTML = items.map(x => {
    const ch = x.change_pct;
    const cls = ch > 0 ? 'positive' : ch < 0 ? 'negative' : '';
    const arrow = ch > 0 ? '▲' : ch < 0 ? '▼' : '·';
    const priceStr = x.ticker.startsWith('KRW-')
      ? fmtKrw(x.price)
      : (x.ticker.endsWith('.KS') || x.ticker.endsWith('.KQ'))
      ? fmtKrw(x.price) + ' 원'
      : fmt(x.price, x.price > 100 ? 2 : 4);
    return `
      <div class="indices-item">
        <div class="indices-name">
          <div class="indices-label">${escapeHtml(x.name)}</div>
          <div class="indices-cat">${escapeHtml(x.category || '')}</div>
        </div>
        <div class="indices-price">${priceStr}</div>
        <div class="indices-change ${cls}">${arrow} ${fmtPct(ch)}</div>
      </div>`;
  }).join('');
  const upd = $("#indicesUpdated");
  if (upd) upd.textContent = `갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
}

function switchMarket(market) {
  currentMarket = market;
  $$(".toggle-btn").forEach(b => b.classList.toggle("active", b.dataset.market === market));
  // v5.3.2: UI 라벨 KR 분기
  if (market === "crypto") {
    $("#marketLabel").textContent = "AI 코인 분석 (Gemini 2.5 Pro)";
    $("#analyzeInput").placeholder = "BTC, ETH, XRP...";
    $("#analyzeTitle").textContent = "🔍 특정 코인 심층 분석";
  } else if (market === "kr") {
    $("#marketLabel").textContent = "AI 국내 주식 분석 (Gemini 2.5 Pro)";
    $("#analyzeInput").placeholder = "삼성전자, 신흥, 005930...";
    $("#analyzeTitle").textContent = "🔍 특정 국내 주식 심층 분석";
  } else {
    $("#marketLabel").textContent = "AI 미국주식 분석 (Gemini 2.5 Pro)";
    $("#analyzeInput").placeholder = "NVDA, TSLA, AAPL...";
    $("#analyzeTitle").textContent = "🔍 특정 주식 심층 분석";
  }
  $("#topGainers").innerHTML = "로딩 중...";
  $("#topLosers").innerHTML = "";
  $("#topVolume").innerHTML = "";
  $("#recommendations").innerHTML = "";
  $("#coinAnalysis").innerHTML = "";
  $("#analyzeInput").value = "";
  _lastAnalysisResult = null;
  currentIndicesCat = null;  // v5.3.2: 마켓 전환 시 카테고리 리셋
  renderIndicesTabs();
  loadIndices();
  loadAnalysis();
}

async function loadAnalysis() {
  if (currentMarket === "crypto") {
    await loadCryptoAnalysis();
  } else if (currentMarket === "kr") {
    await loadKRAnalysis();
  } else {
    await loadStocksAnalysis();
  }
}

// v5.3.2: 국내 주식 분석탭 로드
async function loadKRAnalysis() {
  const m = await api("/api/analysis/kr/movers?n=10");
  if (!m || m.error) {
    $("#topGainers").innerHTML = `<div class="empty">${m?.error || "로딩 실패"}</div>`;
    return;
  }
  const fg = $("#analysisFG");
  if (fg) fg.textContent = `국내 ${m.total_stocks}개 종목 분석 (KOSPI/KOSDAQ, 거래대금 10억+)`;

  const renderMover = (x) => {
    const noise = x.noise_flag ? '<span class="noise-flag" title="±25% 이상 변동">⚠️</span> ' : '';
    const code = x.symbol.replace('.KS', '').replace('.KQ', '');
    const label = x.name_ko ? `${x.name_ko} <span style="font-size:10px;color:var(--fg-dim)">${code}</span>` : x.symbol;
    return `
    <div class="mover-item" onclick="quickAnalyze('${x.symbol}')">
      <div>
        <div class="mover-symbol">${noise}${label}</div>
        <div class="mover-meta">${fmtKrw(x.price)} KRW · 거래 ${fmtKrw(x.volume_24h_krw/1e8)}억</div>
      </div>
      <div class="mover-change ${x.change_24h > 0 ? "positive" : "negative"}">${fmtPct(x.change_24h)}</div>
    </div>`;
  };

  $("#topGainers").innerHTML = m.top_gainers.slice(0, 10).map(renderMover).join("");
  $("#topLosers").innerHTML = m.top_losers.slice(0, 5).map(renderMover).join("");
  $("#topVolume").innerHTML = m.top_volume.slice(0, 5).map(renderMover).join("");
  const upd = $("#moversUpdated");
  if (upd) upd.textContent = `갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
}

async function loadCryptoAnalysis() {
  const m = await api("/api/analysis/movers?n=10");
  if (!m || m.error) {
    $("#topGainers").innerHTML = `<div class="empty">${m?.error || "로딩 실패"}</div>`;
    return;
  }

  const f = await api("/api/crypto/filters");
  if (f?.fear_greed) {
    $("#analysisFG").textContent =
      `F&G ${f.fear_greed.score} (${f.fear_greed.classification}) · 총 ${m.total_pairs}개 KRW 마켓`;
  }

  const renderMover = (x) => {
    const noise = x.noise_flag ? '<span class="noise-flag" title="±50% 이상 변동 - 노이즈 가능">⚠️</span> ' : '';
    return `
    <div class="mover-item" onclick="quickAnalyze('${x.symbol}')">
      <div>
        <div class="mover-symbol">${noise}${x.symbol}</div>
        <div class="mover-meta">${fmtKrw(x.price)} KRW · 거래 ${fmtKrw(x.volume_24h_krw/1e8)}억</div>
      </div>
      <div class="mover-change ${x.change_24h > 0 ? "positive" : "negative"}">${fmtPct(x.change_24h)}</div>
    </div>
  `;
  };

  $("#topGainers").innerHTML = m.top_gainers.slice(0, 10).map(renderMover).join("");
  $("#topLosers").innerHTML = m.top_losers.slice(0, 5).map(renderMover).join("");
  $("#topVolume").innerHTML = m.top_volume.slice(0, 5).map(renderMover).join("");
  const upd = $("#moversUpdated");
  if (upd) upd.textContent = `갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
}

async function loadStocksAnalysis() {
  const m = await api("/api/analysis/stocks/movers?n=10");
  if (!m || m.error) {
    $("#topGainers").innerHTML = `<div class="empty">${m?.error || "로딩 실패"}</div>`;
    return;
  }

  $("#analysisFG").textContent = `총 ${m.total_stocks}개 미국 주식 분석 · 거래대금 $10M+ 필터`;

  const renderStock = (x) => {
    const noise = x.noise_flag ? '<span class="noise-flag" title="±25% 이상 변동 - 사전·사후거래 노이즈 가능">⚠️</span> ' : '';
    return `
    <div class="mover-item" onclick="quickAnalyze('${x.symbol}')">
      <div>
        <div class="mover-symbol">${noise}${x.symbol}</div>
        <div class="mover-meta">$${fmt(x.price, 2)} · 거래 $${fmt(x.volume_24h_usd/1e9, 2)}B</div>
      </div>
      <div class="mover-change ${x.change_24h > 0 ? "positive" : "negative"}">${fmtPct(x.change_24h)}</div>
    </div>
  `;
  };

  $("#topGainers").innerHTML = m.top_gainers.slice(0, 10).map(renderStock).join("");
  $("#topLosers").innerHTML = m.top_losers.slice(0, 5).map(renderStock).join("");
  $("#topVolume").innerHTML = m.top_volume.slice(0, 5).map(renderStock).join("");
  const upd = $("#moversUpdated");
  if (upd) upd.textContent = `갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
}

async function loadRecommend() {
  const btn = $("#recommendBtn");
  const target = $("#recommendations");
  btn.disabled = true;
  btn.textContent = "분석 중... (Gemini Pro)";
  target.innerHTML = '<div class="loading-spinner">🧠 시장 종합 분석 중... (30-60초)</div>';

  const endpoint = currentMarket === "crypto"
    ? "/api/analysis/recommend?n=5"
    : currentMarket === "kr"
    ? "/api/analysis/kr/recommend?n=5"
    : "/api/analysis/stocks/recommend?n=5";
  const r = await api(endpoint);
  btn.disabled = false;
  btn.textContent = "다시 추천받기";

  if (!r || r.error) {
    target.innerHTML = `<div class="empty">실패: ${r?.error || "unknown"}</div>`;
    return;
  }

  const recs = r.recommendations || [];
  target.innerHTML = recs.map(x => {
    const thesis = x.thesis_ko || x.thesis || "";
    return `
    <div class="recommend-item">
      <div class="recommend-header">
        <div>
          <span class="recommend-symbol">${x.symbol}</span>
          <span class="recommend-badge ${x.risk_level}">위험 ${ko(x.risk_level)}</span>
          <span class="recommend-badge">${koHorizon(x.time_horizon)}</span>
        </div>
        <span class="recommend-badge">신뢰 ${fmt(x.confidence, 2)}</span>
      </div>
      <div class="recommend-thesis">${escapeHtml(thesis)}</div>
      <div style="margin-top:6px"><button class="btn" style="font-size:11px;padding:4px 8px" onclick="quickAnalyze('${x.symbol}')">심층분석 →</button></div>
    </div>
    `;
  }).join("");
  toast(`✓ AI 추천 ${recs.length}건 (Pro 모델)`, "success");
}

async function analyzeAsset() {
  const input = $("#analyzeInput").value.trim();
  if (!input) {
    toast(currentMarket === "crypto" ? "코인명 입력 (예: BTC, 비트코인)" : "주식명 입력 (예: NVDA, 엔비디아)", "error");
    return;
  }
  // 한글 등 ASCII 아닌 경우 또는 영문이어도 모호 → search → 첫 결과 사용
  const isPureTicker = /^[A-Z0-9.\-]{1,7}$/.test(input.toUpperCase());
  if (isPureTicker) {
    await runAnalysis(input.toUpperCase());
  } else {
    // 자동 검색 후 첫 결과로 분석
    const endpoint = currentMarket === "crypto"
      ? `/api/search/coins?q=${encodeURIComponent(input)}&limit=3`
      : currentMarket === "kr"
      ? `/api/search/kr_stocks?q=${encodeURIComponent(input)}&limit=3`
      : `/api/search/stocks?q=${encodeURIComponent(input)}&limit=3`;
    toast(`'${input}' 검색 중...`);
    const sr = await api(endpoint);
    const first = sr?.results?.[0];
    if (!first) {
      toast(`'${input}' 검색 결과 없음`, "error");
      return;
    }
    $("#analyzeInput").value = first.symbol;
    toast(`→ ${first.symbol}${first.name_ko ? ' (' + first.name_ko + ')' : ''} 분석 중`);
    await runAnalysis(first.symbol);
  }
}

// v4.5: 자동완성 검색
let _searchTimer = null;
function setupAutocomplete() {
  const input = $("#analyzeInput");
  if (!input) return;
  input.addEventListener("input", () => {
    clearTimeout(_searchTimer);
    const q = input.value.trim();
    const box = $("#searchSuggestions");
    if (!q || q.length < 1) {
      if (box) box.style.display = "none";
      return;
    }
    _searchTimer = setTimeout(() => doAutoSearch(q), 300);
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { const b = $("#searchSuggestions"); if (b) b.style.display = "none"; }, 200);
  });
}

async function doAutoSearch(q) {
  const box = $("#searchSuggestions");
  if (!box) return;
  const endpoint = currentMarket === "crypto"
    ? `/api/search/coins?q=${encodeURIComponent(q)}&limit=6`
    : currentMarket === "kr"
    ? `/api/search/kr_stocks?q=${encodeURIComponent(q)}&limit=6`
    : `/api/search/stocks?q=${encodeURIComponent(q)}&limit=6`;
  const sr = await api(endpoint);
  const results = sr?.results || [];
  if (results.length === 0) {
    box.style.display = "none";
    return;
  }
  box.innerHTML = results.map(r => {
    const koLabel = r.name_ko ? ` · ${escapeHtml(r.name_ko)}` : "";
    const nameDisplay = r.name && r.name !== r.symbol ? escapeHtml(r.name) : "";
    const badge = (currentMarket === "crypto")
      ? (r.available_on_upbit ? '<span class="suggestion-badge available">Upbit ✓</span>' : '<span class="suggestion-badge">Upbit ✗</span>')
      : `<span class="suggestion-badge">${r.exchange || 'US'}</span>`;
    return `
      <div class="suggestion-item" onmousedown="pickSuggestion('${escapeHtml(r.symbol)}')">
        <div class="suggestion-info">
          <span class="suggestion-symbol">${escapeHtml(r.symbol)}${koLabel}</span>
          ${nameDisplay ? `<span class="suggestion-name">${nameDisplay}</span>` : ''}
        </div>
        ${badge}
      </div>
    `;
  }).join("");
  box.style.display = "block";
}

function pickSuggestion(symbol) {
  $("#analyzeInput").value = symbol;
  const box = $("#searchSuggestions");
  if (box) box.style.display = "none";
  runAnalysis(symbol);
}

async function quickAnalyze(symbol) {
  $("#analyzeInput").value = symbol;
  switchTab("analysis");
  setTimeout(() => runAnalysis(symbol), 100);
}

// v5.1: 분석 폴링 중단 플래그 + 친절한 에러 매핑
let _pollAbort = false;

function cancelAnalysis() {
  _pollAbort = true;
  localStorage.removeItem("pending_analysis");
  const target = $("#coinAnalysis");
  if (target) target.innerHTML = '<div class="empty">⏹ 분석 중지됨</div>';
  _setAnalyzeBtnState("idle");
  toast("분석을 중지했습니다");
}

// v5.1.1: analyzeBtn / cancelBtn mutually exclusive 토글
function _setAnalyzeBtnState(state) {
  const btn = $("#analyzeBtn");
  const cancelBtn = $("#cancelBtn");
  if (state === "running") {
    if (btn) btn.style.display = "none";
    if (cancelBtn) cancelBtn.style.display = "";
  } else {
    if (btn) { btn.disabled = false; btn.textContent = "분석"; btn.style.display = ""; }
    if (cancelBtn) cancelBtn.style.display = "none";
  }
}

function _friendlyError(msg) {
  if (!msg) return "분석 실패 — 다시 시도해주세요";
  const m = String(msg);
  if (/Expecting value|JSONDecode|JSON|응답 형식/.test(m)) {
    return "AI 응답이 너무 길어 잘렸어요. 다시 시도하면 보통 성공합니다.";
  }
  if (/timeout|Timeout|타임아웃/.test(m)) return "응답 시간 초과 — 잠시 후 다시 시도해주세요";
  if (/429|rate limit|시간당/.test(m)) return m;
  if (/500|Internal Server/.test(m)) return "서버 오류 — 잠시 후 다시 시도해주세요";
  if (/404|not found/.test(m)) return "종목을 찾을 수 없어요. 티커를 확인해주세요";
  return m.length > 80 ? m.substring(0, 80) + "…" : m;
}

async function runAnalysis(symbol) {
  // v5.1.1: analyze hide → cancel show
  _pollAbort = false;
  _setAnalyzeBtnState("running");
  const startResp = await api("/api/analysis/job/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({market: currentMarket, symbol}),
  });
  if (!startResp || startResp.error) {
    showAnalysisError(startResp?.error || "작업 시작 실패");
    return;
  }
  const jobId = startResp.job_id;
  localStorage.setItem("pending_analysis", JSON.stringify({
    jobId, symbol, market: currentMarket, started: Date.now(),
  }));
  await pollAnalysisJob(jobId, symbol);
}

function showAnalysisError(msg) {
  const target = $("#coinAnalysis");
  const friendly = _friendlyError(msg);
  if (target) target.innerHTML = `<div class="empty">⚠️ ${escapeHtml(friendly)}</div>`;
  _setAnalyzeBtnState("idle");
}

async function pollAnalysisJob(jobId, symbol) {
  const target = $("#coinAnalysis");
  _pollAbort = false;
  _setAnalyzeBtnState("running");

  let attempts = 0;
  const maxAttempts = 90;
  const poll = async () => {
    if (_pollAbort) return;
    if (target) {
      const elapsed = attempts * 2;
      target.innerHTML = `<div class="loading-spinner">
        🔍 ${escapeHtml(symbol)} 분석 진행 중... (${elapsed}초 경과)<br>
        <span style="font-size:11px;color:var(--fg-faint)">
          💡 화면 나가도 OK! 다시 열면 자동으로 결과 표시됩니다
        </span>
      </div>`;
    }
    const r = await api(`/api/analysis/job/${jobId}`);
    if (_pollAbort) return;
    if (!r || r.status === "not_found") {
      showAnalysisError("작업을 찾을 수 없음 (1시간 경과 또는 서버 재시작)");
      localStorage.removeItem("pending_analysis");
      return;
    }
    if (r.status === "completed") {
      localStorage.removeItem("pending_analysis");
      displayAnalysisResult(r.result);
      _setAnalyzeBtnState("idle");
      if (r.from_cache) toast("✓ 캐시된 결과 (5분 내 재호출)", "success");
      return;
    }
    if (r.status === "failed") {
      localStorage.removeItem("pending_analysis");
      showAnalysisError(r.error || "분석 실패");
      return;
    }
    attempts++;
    if (attempts >= maxAttempts) {
      showAnalysisError("3분 타임아웃 — 다시 시도해주세요");
      localStorage.removeItem("pending_analysis");
      return;
    }
    setTimeout(poll, 2000);
  };
  poll();
}

function displayAnalysisResult(r) {
  const target = $("#coinAnalysis");
  if (!r || r.error) {
    showAnalysisError(r?.error || "결과 없음");
    return;
  }
  _lastAnalysisResult = r;

  const a = r.analysis || {};
  const raw = r.raw_data || {};

  const summary = L(a, 'summary');
  const setup = currentLang === 'en'
    ? (a.current_setup_en || a.current_setup || ko(a.current_setup))
    : (a.current_setup_ko || ko(a.current_setup) || "?");
  const risks = Larr(a, 'key_risks');
  const catalysts = Larr(a, 'key_catalysts');
  // v5.0.1: KO/EN 토글을 분석 결과 카드 안으로 — 적용 범위 명확화
  const langToggleHTML = `
    <div class="result-lang-toggle">
      <span class="lang-label">${currentLang === 'en' ? 'Analysis language:' : '분석 언어:'}</span>
      <div class="lang-toggle">
        <button class="lang-btn ${currentLang === 'ko' ? 'active' : ''}" data-lang="ko" onclick="switchLang('ko')">KO</button>
        <button class="lang-btn ${currentLang === 'en' ? 'active' : ''}" data-lang="en" onclick="switchLang('en')">EN</button>
      </div>
    </div>`;

  // v5.3.2: 통화/타입 분기
  const isStock = currentMarket === "stocks" || currentMarket === "kr";
  const isUSD = currentMarket === "stocks";
  const isKR = currentMarket === "kr";
  const entry = a.entry_zone_krw || a.entry_zone_usd || [];
  const stop = a.stop_loss_krw ?? a.stop_loss_usd;
  const t1 = a.target_1_krw ?? a.target_1_usd;
  const t2 = a.target_2_krw ?? a.target_2_usd;
  const resistance = a.resistance_levels_krw || a.resistance_levels_usd || [];
  const support = a.support_levels_krw || a.support_levels_usd || [];

  const fmtPrice = (v) => {
    if (v === null || v === undefined) return "—";
    if (isUSD) return "$" + fmt(v, 2);
    return fmtKrw(v);
  };

  // 회사명/심볼 표시 (KR은 KRW 단위 추가)
  const krwSuffix = isKR ? ' KRW' : '';
  const header = isStock && r.company_name
    ? `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · ${escapeHtml(r.company_name)} · 현재가 ${fmtPrice(r.current_price)}${krwSuffix}</div>`
    : `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · 현재가 ${fmtPrice(r.current_price)}${krwSuffix}</div>`;

  // 미주는 hourly RSI도 함께 표시
  const rsiLine = isStock
    ? `RSI(daily) ${fmt(raw.rsi_daily, 0)} · RSI(hourly) ${fmt(raw.rsi_hourly, 0)} · 30d 범위 위치 ${fmt(raw.position_30d_pct, 0)}%${raw.sector ? ' · ' + raw.sector : ''}${raw.forward_pe ? ' · P/E ' + fmt(raw.forward_pe, 1) : ''}`
    : `RSI(1h) ${fmt(raw.rsi_1h, 0)} · RSI(daily) ${fmt(raw.rsi_daily, 0)} · 30일 범위 위치 ${fmt(raw.position_30d_pct, 0)}%`;

  target.innerHTML = `
    ${langToggleHTML}
    ${header}
    <div class="summary">${escapeHtml(summary)}</div>
    <div><span class="recommendation ${a.recommendation || ""}">${ko(a.recommendation)}</span>
         <span style="font-size:11px;color:var(--fg-dim)">${currentLang === 'en' ? 'Conf' : '신뢰도'} ${fmt(a.confidence, 2)} · ${koHorizon(a.time_horizon)}</span></div>

    <div class="grid-2">
      <div class="grid-item"><div class="label">1일</div><div class="value" style="color:${a.trend_1d === 'bullish' ? 'var(--accent)' : a.trend_1d === 'bearish' ? 'var(--danger)' : 'var(--fg)'}">${ko(a.trend_1d)}</div></div>
      <div class="grid-item"><div class="label">1주</div><div class="value" style="color:${a.trend_1w === 'bullish' ? 'var(--accent)' : a.trend_1w === 'bearish' ? 'var(--danger)' : 'var(--fg)'}">${ko(a.trend_1w)}</div></div>
      <div class="grid-item"><div class="label">1달</div><div class="value" style="color:${a.trend_1m === 'bullish' ? 'var(--accent)' : a.trend_1m === 'bearish' ? 'var(--danger)' : 'var(--fg)'}">${ko(a.trend_1m)}</div></div>
      <div class="grid-item"><div class="label">시장 상태</div><div class="value" style="font-size:12px">${escapeHtml(setup)}</div></div>
    </div>

    <div class="grid-2">
      <div class="grid-item"><div class="label">진입 구간</div><div class="value">${entry.map(fmtPrice).join(' ~ ')}</div></div>
      <div class="grid-item"><div class="label">손절</div><div class="value" style="color:var(--danger)">${fmtPrice(stop)}</div></div>
      <div class="grid-item"><div class="label">목표 1 (보수)</div><div class="value" style="color:var(--accent)">${fmtPrice(t1)}</div></div>
      <div class="grid-item"><div class="label">목표 2 (공격)</div><div class="value" style="color:var(--accent)">${fmtPrice(t2)}</div></div>
    </div>

    <div class="levels">
      <div><b>저항선:</b> ${resistance.map(fmtPrice).join(', ')}</div>
      <div><b>지지선:</b> ${support.map(fmtPrice).join(', ')}</div>
      <div><b>손익비:</b> ${a.risk_reward_ratio || '?'}</div>
    </div>

    <div class="korean-advice">
      <b>💬 ${currentLang === 'en' ? 'Detailed Analysis' : '상세 분석'}</b><br>
      ${escapeHtml(currentLang === 'en' ? (a.english_advice || a.korean_advice || '') : (a.korean_advice || ''))}
      <br><br>
      <b>⚠️ ${currentLang === 'en' ? 'Key Risks' : '주요 리스크'}:</b><br>
      ${risks.map(r => `· ${escapeHtml(r)}`).join('<br>')}
      <br><br>
      <b>🚀 ${currentLang === 'en' ? 'Bullish Catalysts' : '상승 모멘텀'}:</b><br>
      ${catalysts.map(c => `· ${escapeHtml(c)}`).join('<br>')}
    </div>

    ${L(a, 'valuation') ? `
    <div class="extra-section">
      <div class="extra-title">💰 ${currentLang === 'en' ? 'Valuation' : '밸류에이션 평가'}
        ${a.valuation_verdict ? `<span class="verdict-badge ${a.valuation_verdict}">${currentLang === 'en' ? a.valuation_verdict : ko_verdict(a.valuation_verdict)}</span>` : ''}
      </div>
      <div>${escapeHtml(L(a, 'valuation'))}</div>
      ${a.valuation_peer_comparison ? `<div class="extra-meta">📊 ${escapeHtml(a.valuation_peer_comparison)}</div>` : ''}
    </div>
    ` : ''}

    ${(L(a, 'core_business') || L(a, 'use_case')) ? `
    <div class="extra-section">
      <div class="extra-title">${isStock ? (currentLang === 'en' ? '🏢 Core Business' : '🏢 본업 분석') : (currentLang === 'en' ? '🔧 Use Case / Network' : '🔧 유즈케이스/네트워크')}</div>
      <div>${escapeHtml(L(a, 'core_business') || L(a, 'use_case'))}</div>
      ${a.core_business_segments ? `
        <div class="extra-meta">
          ${(a.core_business_segments || []).map(s => `
            <span class="segment-chip ${s.trend}">${escapeHtml(s.name)} ${s.revenue_share_pct ?? '?'}% ${trend_icon(s.trend)}</span>
          `).join('')}
        </div>
      ` : ''}
    </div>
    ` : ''}

    ${(L(a, 'growth_drivers') || L(a, 'tokenomics')) ? `
    <div class="extra-section">
      <div class="extra-title">${isStock ? (currentLang === 'en' ? '🚀 Growth Drivers' : '🚀 신규 성장 동력') : (currentLang === 'en' ? '🪙 Tokenomics' : '🪙 토크노믹스')}</div>
      <div>${escapeHtml(L(a, 'growth_drivers') || L(a, 'tokenomics'))}</div>
    </div>
    ` : ''}

    ${L(a, 'shareholder_returns') ? `
    <div class="extra-section">
      <div class="extra-title">💸 ${currentLang === 'en' ? 'Shareholder Returns' : '주주 환원'}</div>
      <div>${escapeHtml(L(a, 'shareholder_returns'))}</div>
    </div>
    ` : ''}

    ${(L(a, 'geopolitical_risk') || L(a, 'regulatory_risk')) ? `
    <div class="extra-section danger">
      <div class="extra-title">🌍 ${isStock ? (currentLang === 'en' ? 'Geopolitical / Regulatory Risk' : '지정학/규제 리스크') : (currentLang === 'en' ? 'Regulatory Risk' : '규제 리스크')}</div>
      <div>${escapeHtml(L(a, 'geopolitical_risk') || L(a, 'regulatory_risk'))}</div>
    </div>
    ` : ''}

    ${L(a, 'company_guidance') ? `
    <div class="extra-section">
      <div class="extra-title">📢 ${currentLang === 'en' ? 'Company Guidance vs Consensus' : '회사 가이던스 vs 컨센서스'}</div>
      <div>${escapeHtml(L(a, 'company_guidance'))}</div>
    </div>
    ` : ''}

    ${a.horizon_analysis ? `
    <div class="extra-section">
      <div class="extra-title">⏱ ${currentLang === 'en' ? 'Multi-Horizon Outlook' : '시간 프레임별 전망'}</div>
      ${['short_term_1w', 'medium_term_3m', 'long_term_1y'].map(h => {
        const ha = a.horizon_analysis[h];
        if (!ha) return '';
        const labelKo = h === 'short_term_1w' ? '단기 (1주)' : h === 'medium_term_3m' ? '중기 (3개월)' : '장기 (1년)';
        const labelEn = h === 'short_term_1w' ? 'Short (1W)' : h === 'medium_term_3m' ? 'Medium (3M)' : 'Long (1Y)';
        const label = currentLang === 'en' ? labelEn : labelKo;
        const outlookLabel = currentLang === 'en' ? (ha.outlook || '?') : ko(ha.outlook);
        return `
          <div class="horizon-row">
            <div class="horizon-header">
              <span class="horizon-label">${label}</span>
              <span class="horizon-outlook ${ha.outlook}">${outlookLabel}</span>
              <span class="horizon-conf">${fmt(ha.confidence, 2)}</span>
            </div>
            <div class="horizon-summary">${escapeHtml(L(ha, 'summary'))}</div>
          </div>
        `;
      }).join('')}
    </div>
    ` : ''}

    ${a.scenarios ? `
    <div class="extra-section">
      <div class="extra-title">🎯 ${currentLang === 'en' ? 'Scenario Analysis (Bull / Base / Bear)' : '시나리오 분석 (Bull / Base / Bear)'}</div>
      ${['bullish', 'base', 'bearish'].map(k => {
        const s = a.scenarios[k];
        if (!s) return '';
        const labelKo = k === 'bullish' ? '🟢 낙관' : k === 'base' ? '⚪ 중립' : '🔴 비관';
        const labelEn = k === 'bullish' ? '🟢 Bull' : k === 'base' ? '⚪ Base' : '🔴 Bear';
        const label = currentLang === 'en' ? labelEn : labelKo;
        const target = isUSD
          ? (s.price_target_usd ? '$' + fmt(s.price_target_usd, 2)
            : s.price_range_usd ? '$' + s.price_range_usd.map(v => fmt(v, 2)).join(' ~ $')
            : s.downside_target_usd ? '$' + fmt(s.downside_target_usd, 2) : '—')
          : (s.price_target_krw ? fmtKrw(s.price_target_krw) + ' KRW'
            : s.price_range_krw ? s.price_range_krw.map(fmtKrw).join(' ~ ') + ' KRW'
            : s.downside_target_krw ? fmtKrw(s.downside_target_krw) + ' KRW' : '—');
        const probLabel = currentLang === 'en' ? 'Probability' : '확률';
        const triggersLabel = currentLang === 'en' ? 'Triggers' : '트리거';
        const triggers = Larr(s, 'triggers');
        return `
          <div class="scenario-row ${k}">
            <div class="scenario-header">
              <span class="scenario-label">${label}</span>
              <span class="scenario-prob">${probLabel} ${(s.probability * 100).toFixed(0)}%</span>
              <span class="scenario-target">${target}</span>
            </div>
            <div class="scenario-narrative">${escapeHtml(L(s, 'narrative'))}</div>
            ${(triggers && triggers.length > 0) ? `
              <div class="scenario-triggers">${triggersLabel}: ${triggers.map(escapeHtml).join(' · ')}</div>
            ` : ''}
          </div>
        `;
      }).join('')}
    </div>
    ` : ''}

    ${L(a, 'data_freshness_note') ? `
    <div class="extra-section" style="border-left-color:var(--warning)">
      <div class="extra-title">⚠️ ${currentLang === 'en' ? 'Data Freshness Note' : '데이터 신선도 노트'}</div>
      <div style="font-size:11px">${escapeHtml(L(a, 'data_freshness_note'))}</div>
    </div>
    ` : ''}

    ${a.quantitative_metrics ? (() => {
      const q = a.quantitative_metrics;
      const en = currentLang === 'en';
      const items = isKR ? [
        [en ? '🌐 Foreign Ownership' : '🌐 외국인 보유 비율', L(q, 'foreign_ownership_pct')],
        [en ? '🏛 Institutional Flow (30d)' : '🏛 외국인·기관 순매수 (30일)', L(q, 'institutional_flow')],
        [en ? '📉 Short Interest / Loan' : '📉 공매도/대차잔고', L(q, 'short_interest')],
        [en ? '💥 Last Earnings Surprise' : '💥 최근 영업이익 서프라이즈', q.earnings_surprise_last_q_pct != null ? `${q.earnings_surprise_last_q_pct > 0 ? '+' : ''}${q.earnings_surprise_last_q_pct}%` : null],
        [en ? '📊 KOSPI 200 Inclusion' : '📊 KOSPI 200 편입', L(q, 'kospi_200_inclusion')],
        [en ? '💰 Dividend Yield' : '💰 배당수익률', q.dividend_yield_pct != null ? `${q.dividend_yield_pct}%` : null],
      ] : isStock ? [
        [en ? '📦 Backlog / Pipeline' : '📦 수주/파이프라인', L(q, 'backlog_or_pipeline_usd')],
        [en ? '🏭 Inventory (DOI)' : '🏭 재고 사이클 (DOI)', L(q, 'inventory_days')],
        [en ? '💥 Last EPS Surprise' : '💥 최근 EPS 서프라이즈', q.earnings_surprise_last_q_pct != null ? `${q.earnings_surprise_last_q_pct > 0 ? '+' : ''}${q.earnings_surprise_last_q_pct}%` : null],
        [en ? '👔 Insider Activity (90d)' : '👔 내부자 매수/매도 (90일)', L(q, 'insider_activity_90d')],
        [en ? '📉 Short Interest' : '📉 공매도 비율', q.short_interest_pct != null ? `${q.short_interest_pct}%` : null],
        [en ? '🏛 Institutional Ownership' : '🏛 기관 보유 비율', q.institutional_ownership_pct != null ? `${q.institutional_ownership_pct}%` : null],
      ] : [
        ['📊 NVT Ratio', L(q, 'onchain_nvt')],
        ['🔄 MVRV', L(q, 'onchain_mvrv')],
        ['📈 SOPR', L(q, 'sopr')],
        ['💹 Funding Rate', L(q, 'funding_rate')],
        ['🔗 Open Interest', L(q, 'open_interest')],
        [en ? '💸 Exchange Flow' : '💸 거래소 입출금', L(q, 'exchange_flow')],
        [en ? '👥 Active Addresses' : '👥 활성 주소수', L(q, 'active_addresses')],
      ];
      const valid = items.filter(([, v]) => v && v !== 'N/A' && v !== 'null%');
      if (valid.length === 0) return '';
      return `
        <div class="extra-section">
          <div class="extra-title">📊 ${en ? 'Key Quantitative Metrics' : '정량 핵심 지표'}</div>
          ${valid.map(([label, val]) => `
            <div class="metric-row"><span class="metric-label">${label}</span><span class="metric-val">${escapeHtml(String(val))}</span></div>
          `).join('')}
        </div>`;
    })() : ''}

    ${a.macro_assumptions ? (() => {
      const m = a.macro_assumptions;
      const en = currentLang === 'en';
      const title = en ? '🌐 Macro Scenario Assumptions (Fed / USD / Liquidity)' : '🌐 매크로 시나리오 가정 (Fed/달러/유동성)';
      const now = L(m, 'current_macro_phase');
      const bull = L(m, 'bullish_macro');
      const base = L(m, 'base_macro');
      const bear = L(m, 'bearish_macro');
      if (!now && !bull && !base && !bear) return '';
      return `
        <div class="extra-section">
          <div class="extra-title">${title}</div>
          ${now ? `<div class="macro-now"><b>${en ? 'Current' : '현재'}:</b> ${escapeHtml(now)}</div>` : ''}
          ${bull ? `<div class="macro-row bull">🟢 <b>${en ? 'Bull' : 'Bull 가정'}:</b> ${escapeHtml(bull)}</div>` : ''}
          ${base ? `<div class="macro-row base">⚪ <b>${en ? 'Base' : 'Base 가정'}:</b> ${escapeHtml(base)}</div>` : ''}
          ${bear ? `<div class="macro-row bear">🔴 <b>${en ? 'Bear' : 'Bear 가정'}:</b> ${escapeHtml(bear)}</div>` : ''}
        </div>`;
    })() : ''}

    ${a.methodology_scores ? (() => {
      const m = a.methodology_scores;
      const en = currentLang === 'en';
      const labels = isKR ? {
        sepa_minervini: "SEPA (Minervini)",
        stage_weinstein: "Stage (Weinstein)",
        wyckoff: "Wyckoff",
        quality_value: "Quality + Value",
        momentum_rs_vs_kospi: "Momentum vs KOSPI",
        foreign_institutional_flow: "외국인·기관 수급",
      } : isStock ? {
        canslim: "CANSLIM (O'Neil)",
        sepa_minervini: "SEPA (Minervini)",
        stage_weinstein: "Stage (Weinstein)",
        wyckoff: "Wyckoff",
        quality_value: "Quality + Value",
        momentum_rs: "Momentum + RS",
      } : {
        stage_weinstein: "Stage (Weinstein)",
        wyckoff: "Wyckoff",
        onchain_cycle: "On-chain Cycle (NVT/MVRV)",
        momentum_rs_vs_btc: "Momentum vs BTC",
        sentiment_funding: "Sentiment + Funding",
        macro_liquidity: "Macro Liquidity",
      };
      const rows = Object.keys(labels).filter(k => m[k]).map(k => {
        const s = m[k];
        const score = Number(s.score) || 0;
        const color = score >= 7 ? 'good' : score >= 4 ? 'mid' : 'bad';
        return `
          <div class="method-row">
            <div class="method-header">
              <span class="method-name">${labels[k]}</span>
              <span class="method-score ${color}">${score}/10</span>
            </div>
            <div class="method-notes">${escapeHtml(L(s, 'notes'))}</div>
          </div>`;
      }).join('');
      if (!rows) return '';
      return `
        <div class="extra-section">
          <div class="extra-title">🎯 ${en ? 'Validated Methodology Scores' : '검증된 투자 방법론 점수'}</div>
          ${rows}
        </div>`;
    })() : ''}

    ${a.position_sizing ? (() => {
      const p = a.position_sizing;
      const en = currentLang === 'en';
      return `
        <div class="extra-section" style="border-left-color:var(--accent)">
          <div class="extra-title">📐 ${en ? 'Position Sizing Guide' : '포지션 사이징 가이드'}</div>
          ${p.risk_reward_ratio_explicit ? `<div class="ps-row"><b>${en ? 'R/R Ratio' : 'R/R 비율'}:</b> ${Number(p.risk_reward_ratio_explicit).toFixed(2)} ${en ? '(target/stop)' : '(목표/손절)'}</div>` : ''}
          ${p.max_position_pct_of_capital ? `<div class="ps-row"><b>${en ? 'Max Position' : '권장 최대 비중'}:</b> ${escapeHtml(String(p.max_position_pct_of_capital))} ${en ? '(of capital)' : '(자본 대비)'}</div>` : ''}
          ${(L(p, 'scaling_in_plan')) ? `<div class="ps-row"><b>${en ? 'Scaling-in Plan' : '분할 매수'}:</b> ${escapeHtml(L(p, 'scaling_in_plan'))}</div>` : ''}
          ${(L(p, 'stop_loss_rationale')) ? `<div class="ps-row"><b>${en ? 'Stop-loss Rationale' : '손절 근거'}:</b> ${escapeHtml(L(p, 'stop_loss_rationale'))}</div>` : ''}
          ${p.kelly_fraction_estimate != null ? `<div class="ps-row"><b>Kelly:</b> ${Number(p.kelly_fraction_estimate).toFixed(3)} ${en ? '(apply conservatively)' : '(보수적 적용 권장)'}</div>` : ''}
        </div>`;
    })() : ''}

    <div style="font-size:10px;color:var(--fg-faint);margin-top:6px">${rsiLine}</div>

    ${(r.sources && r.sources.length > 0) ? `
    <div class="sources-card">
      <div class="sources-title">🌐 실시간 검색 출처 (Vertex AI Grounding)</div>
      ${r.sources.slice(0, 6).map(s => `
        <a href="${s.uri}" target="_blank" rel="noopener" class="source-link">
          <span class="source-domain">${escapeHtml(s.domain || s.title.split(' ')[0])}</span>
          <span class="source-title">${escapeHtml(s.title)}</span>
        </a>
      `).join('')}
    </div>
    ` : ''}

    ${(r.news && r.news.length > 0) ? `
    <div class="sources-card">
      <div class="sources-title">📰 최근 뉴스 (Tavily, 3일)</div>
      ${r.news.slice(0, 5).map(n => `
        <a href="${n.url}" target="_blank" rel="noopener" class="source-link">
          <span class="source-title">${escapeHtml(n.title)}</span>
          ${n.published_date ? `<span class="source-date">${escapeHtml(n.published_date.split('T')[0])}</span>` : ''}
        </a>
      `).join('')}
    </div>
    ` : ''}
  `;
  toast(`✓ ${r.symbol || ''} 분석 완료`, "success");
}

// v4.8: PWA 다시 열 때 진행 중인 분석 작업 복원
function restorePendingAnalysis() {
  try {
    const stored = localStorage.getItem("pending_analysis");
    if (!stored) return;
    const {jobId, symbol, market, started} = JSON.parse(stored);
    if (Date.now() - started > 30 * 60 * 1000) {
      localStorage.removeItem("pending_analysis");
      return;
    }
    // 분석 탭으로 이동 + 시장 전환 + 입력 복원
    switchTab("analysis");
    if (market && market !== currentMarket) {
      switchMarket(market);
    }
    const input = $("#analyzeInput");
    if (input) input.value = symbol;
    toast(`🔄 진행 중인 ${symbol} 분석 결과 가져오는 중...`);
    pollAnalysisJob(jobId, symbol);
  } catch (e) {
    localStorage.removeItem("pending_analysis");
  }
}

// ────────────────────────────────────────────
// 탭별 새로고침
function refreshCurrentTab() {
  if (currentTab === "crypto") loadCrypto();
  else if (currentTab === "us") loadUS();
  else if (currentTab === "kr") loadKR();
  else if (currentTab === "analysis") {
    loadAnalysis();
    loadIndices();  // v5.0
  }
  else if (currentTab === "summary") loadSummary();
  updateStatus();
}

// ────────────────────────────────────────────
// 새로고침 버튼
$("#refreshBtn").addEventListener("click", () => {
  refreshCurrentTab();
  toast("새로고침 완료");
});

// ────────────────────────────────────────────
// 초기화
function init() {
  refreshCurrentTab();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshCurrentTab, REFRESH_INTERVAL);
}

document.addEventListener("DOMContentLoaded", () => {
  // v5.0: 저장된 언어 + 지수 위젯 초기화
  $$('.lang-btn').forEach(b => b.classList.toggle('active', b.dataset.lang === currentLang));
  renderIndicesTabs();
  init();
  setupAutocomplete();
  // v4.8: PWA 열 때 진행 중인 분석 자동 복원
  restorePendingAnalysis();
});

// v4.8: 백그라운드 → foreground 복귀 시 처리 통합
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    restorePendingAnalysis();
    refreshCurrentTab();
  }
});

// Service Worker (PWA)
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
