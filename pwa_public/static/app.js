// 공유용 AI 분석 PWA
const API = "";
let currentMarket = "crypto";

function $(s) { return document.querySelector(s); }
function $$(s) { return document.querySelectorAll(s); }
function fmt(n, d=2) {
  if (n == null || isNaN(n)) return "—";
  return Number(n).toLocaleString("ko-KR", {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtKrw(n) {
  if (!n) return "0";
  return Math.round(n).toLocaleString("ko-KR");
}
function fmtPct(n) {
  if (n == null) return "—";
  return (n > 0 ? "+" : "") + Number(n).toFixed(2) + "%";
}
function toast(m, t="") {
  const e = $("#toast");
  e.textContent = m;
  e.className = "toast show " + t;
  setTimeout(() => e.className = "toast", 2500);
}
async function api(p, o={}) {
  try {
    const r = await fetch(API + p, o);
    return await r.json();
  } catch (e) {
    return null;
  }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

const KO_MAP = {
  low: "낮음", medium: "중간", high: "높음",
  short: "단기", long: "장기",
  bullish: "🟢 상승", bearish: "🔴 하락", neutral: "⚪ 중립",
  STRONG_BUY: "강력 매수", BUY: "매수", HOLD: "관망",
  SELL: "매도", STRONG_SELL: "강력 매도", AVOID: "회피",
  accumulation: "매집", distribution: "분산",
  breakout: "돌파", breakdown: "붕괴",
  consolidation: "통합", range_bound: "박스권",
  uptrend: "상승추세", downtrend: "하락추세",
  sideways: "횡보",
};
function ko(v) { return v == null ? "?" : (KO_MAP[v] || v); }
function koHorizon(v) {
  return ({short: "단기", medium: "중기", long: "장기"})[v] || v;
}
function ko_verdict(v) {
  return ({ undervalued: "저평가 💎", fair: "적정 ⚖️", overvalued: "고평가 ⚠️" })[v] || v;
}
function trend_icon(t) {
  return ({ growing: "📈", stable: "➡️", declining: "📉" })[t] || "";
}

function switchMarket(m) {
  currentMarket = m;
  $$(".toggle-btn").forEach(b => b.classList.toggle("active", b.dataset.market === m));
  if (m === "crypto") {
    $("#marketLabel").textContent = "코인 (Upbit KRW)";
    $("#analyzeInput").placeholder = "BTC, ETH, XRP...";
    $("#analyzeTitle").textContent = "🔍 특정 코인 심층 분석";
  } else {
    $("#marketLabel").textContent = "미국 주식 (Yahoo Finance)";
    $("#analyzeInput").placeholder = "NVDA, TSLA, AAPL...";
    $("#analyzeTitle").textContent = "🔍 특정 주식 심층 분석";
  }
  $("#topGainers").innerHTML = "로딩 중...";
  $("#topLosers").innerHTML = "";
  $("#topVolume").innerHTML = "";
  $("#recommendations").innerHTML = "";
  $("#analysisResult").innerHTML = "";
  $("#analyzeInput").value = "";
  loadMovers();
}

async function loadMovers() {
  const endpoint = currentMarket === "crypto"
    ? "/api/analysis/movers?n=10"
    : "/api/analysis/stocks/movers?n=10";
  const m = await api(endpoint);
  if (!m || m.error) {
    $("#topGainers").innerHTML = `<div class="empty">${m?.error || "로딩 실패"}</div>`;
    return;
  }

  if (currentMarket === "crypto") {
    $("#contextInfo").textContent = `Upbit KRW 마켓 ${m.total_pairs}개 분석`;
  } else {
    $("#contextInfo").textContent = `미국 주식 ${m.total_stocks}개 분석 (S&P 500 + 인기주)`;
  }

  const renderItem = (x) => {
    const price = currentMarket === "crypto"
      ? `${fmtKrw(x.price)} KRW · 거래 ${fmtKrw(x.volume_24h_krw/1e8)}억`
      : `$${fmt(x.price, 2)} · 거래 $${fmt(x.volume_24h_usd/1e9, 2)}B`;
    return `
      <div class="mover-item" onclick="quickAnalyze('${x.symbol}')">
        <div>
          <div class="mover-symbol">${x.symbol}</div>
          <div class="mover-meta">${price}</div>
        </div>
        <div class="mover-change ${x.change_24h > 0 ? "positive" : "negative"}">${fmtPct(x.change_24h)}</div>
      </div>
    `;
  };

  $("#topGainers").innerHTML = m.top_gainers.slice(0, 10).map(renderItem).join("");
  $("#topLosers").innerHTML = m.top_losers.slice(0, 5).map(renderItem).join("");
  $("#topVolume").innerHTML = m.top_volume.slice(0, 5).map(renderItem).join("");
}

async function loadRecommend() {
  const btn = $("#recommendBtn");
  const target = $("#recommendations");
  btn.disabled = true;
  btn.textContent = "분석 중... (Pro 모델)";
  target.innerHTML = '<div class="loading-spinner">🧠 시장 종합 분석 중... (30-60초)</div>';
  const endpoint = currentMarket === "crypto"
    ? "/api/analysis/recommend?n=5"
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
        <div style="margin-top:6px">
          <button class="btn" style="font-size:11px;padding:4px 8px" onclick="quickAnalyze('${x.symbol}')">심층분석 →</button>
        </div>
      </div>
    `;
  }).join("");
  toast(`✓ AI 추천 ${recs.length}건`, "success");
}

async function analyzeAsset() {
  const input = $("#analyzeInput").value.trim();
  if (!input) {
    toast(currentMarket === "crypto" ? "코인명 (예: BTC, 비트코인)" : "주식명 (예: NVDA, 엔비디아)", "error");
    return;
  }
  const isPureTicker = /^[A-Z0-9.\-]{1,7}$/.test(input.toUpperCase());
  if (isPureTicker) {
    await runAnalysis(input.toUpperCase());
  } else {
    const endpoint = currentMarket === "crypto"
      ? `/api/search/coins?q=${encodeURIComponent(input)}&limit=3`
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

// v4.5: 자동완성
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

function quickAnalyze(sym) {
  $("#analyzeInput").value = sym;
  setTimeout(() => runAnalysis(sym), 50);
  window.scrollTo({top: document.body.scrollHeight, behavior: "smooth"});
}

async function runAnalysis(sym) {
  // v4.8: 비동기 작업 + polling
  const startResp = await api("/api/analysis/job/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({market: currentMarket, symbol: sym}),
  });
  if (!startResp || startResp.error) {
    showAnalysisError(startResp?.error || "작업 시작 실패");
    return;
  }
  localStorage.setItem("pending_analysis", JSON.stringify({
    jobId: startResp.job_id, symbol: sym, market: currentMarket, started: Date.now(),
  }));
  await pollAnalysisJob(startResp.job_id, sym);
}

function showAnalysisError(msg) {
  const target = $("#analysisResult");
  if (target) target.innerHTML = `<div class="empty">실패: ${escapeHtml(msg)}</div>`;
  const btn = $("#analyzeBtn");
  if (btn) { btn.disabled = false; btn.textContent = "분석"; }
}

async function pollAnalysisJob(jobId, sym) {
  const btn = $("#analyzeBtn");
  const target = $("#analysisResult");
  if (btn) { btn.disabled = true; btn.textContent = "분석 중"; }
  let attempts = 0;
  const poll = async () => {
    if (target) {
      const elapsed = attempts * 2;
      target.innerHTML = `<div class="loading-spinner">
        🔍 ${escapeHtml(sym)} 분석 진행 중... (${elapsed}초)<br>
        <span style="font-size:11px;color:var(--fg-faint)">💡 화면 나가도 OK. 다시 열면 자동 표시.</span>
      </div>`;
    }
    const r = await api(`/api/analysis/job/${jobId}`);
    if (!r || r.status === "not_found") {
      showAnalysisError("작업 없음 (시간 경과 또는 재시작)");
      localStorage.removeItem("pending_analysis");
      return;
    }
    if (r.status === "completed") {
      localStorage.removeItem("pending_analysis");
      displayAnalysisResult(r.result);
      if (btn) { btn.disabled = false; btn.textContent = "분석"; }
      if (r.from_cache) toast("✓ 캐시된 결과 (5분 내)", "success");
      return;
    }
    if (r.status === "failed") {
      localStorage.removeItem("pending_analysis");
      showAnalysisError(r.error || "분석 실패");
      return;
    }
    attempts++;
    if (attempts >= 90) {
      showAnalysisError("타임아웃 (3분)");
      localStorage.removeItem("pending_analysis");
      return;
    }
    setTimeout(poll, 2000);
  };
  poll();
}

function displayAnalysisResult(r) {
  const target = $("#analysisResult");
  if (!r || r.error) {
    showAnalysisError(r?.error || "결과 없음");
    return;
  }
  const a = r.analysis || {};
  const raw = r.raw_data || {};
  const isStock = currentMarket === "stocks";
  const summary = a.summary_ko || a.summary || "";
  const setup = a.current_setup_ko || ko(a.current_setup) || "?";
  const risks = a.key_risks_ko || a.key_risks || [];
  const catalysts = a.key_catalysts_ko || a.key_catalysts || [];
  const entry = a.entry_zone_krw || a.entry_zone_usd || [];
  const stop = a.stop_loss_krw ?? a.stop_loss_usd;
  const t1 = a.target_1_krw ?? a.target_1_usd;
  const t2 = a.target_2_krw ?? a.target_2_usd;
  const resistance = a.resistance_levels_krw || a.resistance_levels_usd || [];
  const support = a.support_levels_krw || a.support_levels_usd || [];
  const fmtPrice = v => v == null ? "—" : (isStock ? "$" + fmt(v, 2) : fmtKrw(v));
  const header = isStock && r.company_name
    ? `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · ${escapeHtml(r.company_name)} · 현재가 ${fmtPrice(r.current_price)}</div>`
    : `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · 현재가 ${fmtPrice(r.current_price)}</div>`;
  const rsiLine = isStock
    ? `RSI(daily) ${fmt(raw.rsi_daily, 0)} · RSI(hourly) ${fmt(raw.rsi_hourly, 0)}${raw.sector ? ' · ' + raw.sector : ''}${raw.forward_pe ? ' · P/E ' + fmt(raw.forward_pe, 1) : ''}`
    : `RSI(1h) ${fmt(raw.rsi_1h, 0)} · RSI(daily) ${fmt(raw.rsi_daily, 0)} · 30일 범위 ${fmt(raw.position_30d_pct, 0)}%`;
  target.innerHTML = `
    ${header}
    <div class="summary">${escapeHtml(summary)}</div>
    <div><span class="recommendation ${a.recommendation || ""}">${ko(a.recommendation)}</span>
         <span style="font-size:11px;color:var(--fg-dim)">신뢰도 ${fmt(a.confidence, 2)} · ${koHorizon(a.time_horizon)}</span></div>
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
      <b>💬 상세 분석</b><br>${escapeHtml(a.korean_advice || "")}<br><br>
      <b>⚠️ 주요 리스크:</b><br>${risks.map(x => `· ${escapeHtml(x)}`).join('<br>')}<br><br>
      <b>🚀 상승 모멘텀:</b><br>${catalysts.map(x => `· ${escapeHtml(x)}`).join('<br>')}
    </div>
    ${a.valuation_ko ? `
      <div class="extra-section">
        <div class="extra-title">💰 밸류에이션 평가
          ${a.valuation_verdict ? `<span class="verdict-badge ${a.valuation_verdict}">${ko_verdict(a.valuation_verdict)}</span>` : ''}
        </div>
        <div>${escapeHtml(a.valuation_ko)}</div>
        ${a.valuation_peer_comparison ? `<div class="extra-meta">📊 ${escapeHtml(a.valuation_peer_comparison)}</div>` : ''}
      </div>` : ''}
    ${(a.core_business_ko || a.use_case_ko) ? `
      <div class="extra-section">
        <div class="extra-title">${isStock ? '🏢 본업 분석' : '🔧 유즈케이스/네트워크'}</div>
        <div>${escapeHtml(a.core_business_ko || a.use_case_ko)}</div>
        ${a.core_business_segments ? `
          <div class="extra-meta">
            ${(a.core_business_segments || []).map(s => `
              <span class="segment-chip ${s.trend}">${escapeHtml(s.name)} ${s.revenue_share_pct ?? '?'}% ${trend_icon(s.trend)}</span>
            `).join('')}
          </div>` : ''}
      </div>` : ''}
    ${(a.growth_drivers_ko || a.tokenomics_ko) ? `
      <div class="extra-section">
        <div class="extra-title">${isStock ? '🚀 신규 성장 동력' : '🪙 토크노믹스'}</div>
        <div>${escapeHtml(a.growth_drivers_ko || a.tokenomics_ko)}</div>
      </div>` : ''}
    ${a.shareholder_returns_ko ? `
      <div class="extra-section">
        <div class="extra-title">💸 주주 환원</div>
        <div>${escapeHtml(a.shareholder_returns_ko)}</div>
      </div>` : ''}
    ${(a.geopolitical_risk_ko || a.regulatory_risk_ko) ? `
      <div class="extra-section danger">
        <div class="extra-title">🌍 ${isStock ? '지정학/규제 리스크' : '규제 리스크'}</div>
        <div>${escapeHtml(a.geopolitical_risk_ko || a.regulatory_risk_ko)}</div>
      </div>` : ''}
    ${a.company_guidance_ko ? `
      <div class="extra-section">
        <div class="extra-title">📢 회사 가이던스 vs 컨센서스</div>
        <div>${escapeHtml(a.company_guidance_ko)}</div>
      </div>` : ''}
    ${a.horizon_analysis ? `
      <div class="extra-section">
        <div class="extra-title">⏱ 시간 프레임별 전망</div>
        ${['short_term_1w', 'medium_term_3m', 'long_term_1y'].map(h => {
          const ha = a.horizon_analysis[h]; if (!ha) return '';
          const label = h === 'short_term_1w' ? '단기 (1주)' : h === 'medium_term_3m' ? '중기 (3개월)' : '장기 (1년)';
          return `
            <div class="horizon-row">
              <div class="horizon-header">
                <span class="horizon-label">${label}</span>
                <span class="horizon-outlook ${ha.outlook}">${ko(ha.outlook)}</span>
                <span class="horizon-conf">${fmt(ha.confidence, 2)}</span>
              </div>
              <div class="horizon-summary">${escapeHtml(ha.summary_ko || '')}</div>
            </div>`;
        }).join('')}
      </div>` : ''}
    ${a.scenarios ? `
      <div class="extra-section">
        <div class="extra-title">🎯 시나리오 분석 (Bull / Base / Bear)</div>
        ${['bullish', 'base', 'bearish'].map(k => {
          const s = a.scenarios[k]; if (!s) return '';
          const label = k === 'bullish' ? '🟢 낙관' : k === 'base' ? '⚪ 중립' : '🔴 비관';
          const target = isStock
            ? (s.price_target_usd ? '$' + fmt(s.price_target_usd, 2)
              : s.price_range_usd ? '$' + s.price_range_usd.map(v => fmt(v, 2)).join(' ~ $')
              : s.downside_target_usd ? '$' + fmt(s.downside_target_usd, 2) : '—')
            : (s.price_target_krw ? fmtKrw(s.price_target_krw) + ' KRW'
              : s.price_range_krw ? s.price_range_krw.map(fmtKrw).join(' ~ ') + ' KRW'
              : s.downside_target_krw ? fmtKrw(s.downside_target_krw) + ' KRW' : '—');
          return `
            <div class="scenario-row ${k}">
              <div class="scenario-header">
                <span class="scenario-label">${label}</span>
                <span class="scenario-prob">확률 ${(s.probability * 100).toFixed(0)}%</span>
                <span class="scenario-target">${target}</span>
              </div>
              <div class="scenario-narrative">${escapeHtml(s.narrative_ko || '')}</div>
              ${(s.triggers_ko && s.triggers_ko.length > 0) ? `<div class="scenario-triggers">트리거: ${s.triggers_ko.map(escapeHtml).join(' · ')}</div>` : ''}
            </div>`;
        }).join('')}
      </div>` : ''}
    ${a.data_freshness_note_ko ? `
      <div class="extra-section" style="border-left-color:var(--warning)">
        <div class="extra-title">⚠️ 데이터 신선도 노트</div>
        <div style="font-size:11px">${escapeHtml(a.data_freshness_note_ko)}</div>
      </div>` : ''}
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
  `;
  toast(`✓ ${r.symbol || ''} 분석 완료`, "success");
}

// v4.8: PWA 다시 열 때 진행 중 분석 복원
function restorePendingAnalysis() {
  try {
    const stored = localStorage.getItem("pending_analysis");
    if (!stored) return;
    const {jobId, symbol, market, started} = JSON.parse(stored);
    if (Date.now() - started > 30 * 60 * 1000) {
      localStorage.removeItem("pending_analysis");
      return;
    }
    if (market && market !== currentMarket) switchMarket(market);
    const input = $("#analyzeInput");
    if (input) input.value = symbol;
    toast(`🔄 진행 중인 ${symbol} 분석 결과 가져오는 중...`);
    pollAnalysisJob(jobId, symbol);
  } catch (e) {
    localStorage.removeItem("pending_analysis");
  }
}

$("#refreshBtn").addEventListener("click", () => {
  loadMovers();
  toast("새로고침 완료");
});

document.addEventListener("DOMContentLoaded", () => {
  loadMovers();
  setupAutocomplete();
  restorePendingAnalysis();  // v4.8: 진행 중 작업 자동 복원
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    restorePendingAnalysis();
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadMovers();
});
