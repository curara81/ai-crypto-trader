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
// 탭별 새로고침
function refreshCurrentTab() {
  if (currentTab === "crypto") loadCrypto();
  else if (currentTab === "us") loadUS();
  else if (currentTab === "kr") loadKR();
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

document.addEventListener("DOMContentLoaded", init);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshCurrentTab();
});

// Service Worker (PWA)
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
