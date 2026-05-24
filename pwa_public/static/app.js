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
  const sym = $("#analyzeInput").value.trim().toUpperCase();
  if (!sym) {
    toast(currentMarket === "crypto" ? "코인 심볼 (예: BTC)" : "주식 티커 (예: NVDA)", "error");
    return;
  }
  await runAnalysis(sym);
}

function quickAnalyze(sym) {
  $("#analyzeInput").value = sym;
  setTimeout(() => runAnalysis(sym), 50);
  window.scrollTo({top: document.body.scrollHeight, behavior: "smooth"});
}

async function runAnalysis(sym) {
  const btn = $("#analyzeBtn");
  const target = $("#analysisResult");
  btn.disabled = true;
  btn.textContent = "분석 중";
  target.innerHTML = `<div class="loading-spinner">🔍 ${sym} 심층 분석 중... (Pro + Google Search, 30-60초)</div>`;
  const endpoint = currentMarket === "crypto"
    ? `/api/analysis/coin/${sym}`
    : `/api/analysis/stocks/${sym}`;
  const r = await api(endpoint);
  btn.disabled = false;
  btn.textContent = "분석";
  if (!r || r.error) {
    target.innerHTML = `<div class="empty">실패: ${r?.error || "unknown"}</div>`;
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
  toast(`✓ ${sym} 분석 완료`, "success");
}

$("#refreshBtn").addEventListener("click", () => {
  loadMovers();
  toast("새로고침 완료");
});

document.addEventListener("DOMContentLoaded", loadMovers);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadMovers();
});
