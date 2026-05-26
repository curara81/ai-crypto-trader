// 공유용 AI 분석 PWA (v5.0)
const API = "";
let currentMarket = "crypto";
let currentLang = (typeof localStorage !== 'undefined' && localStorage.getItem('analysis_lang')) || 'ko';
let currentIndicesCat = null;  // 마켓에 따라 결정
let _lastAnalysisResult = null;  // 언어 토글 시 재렌더용

function $(s) { return document.querySelector(s); }
function $$(s) { return document.querySelectorAll(s); }

// v5.0: 다국어 헬퍼 — obj.<base>_<currentLang> 우선, 없으면 _ko fallback
function L(obj, base) {
  if (!obj) return '';
  const k = base + '_' + currentLang;
  const ko = base + '_ko';
  return obj[k] || obj[ko] || obj[base] || '';
}
function Larr(obj, base) {
  if (!obj) return [];
  const k = base + '_' + currentLang;
  const ko = base + '_ko';
  return obj[k] || obj[ko] || obj[base] || [];
}

// v5.4: 분석 결과를 PNG 이미지로 캡처 → 시스템 공유 시트 (카카오톡/메시지/메일 등)
async function shareAnalysis() {
  const target = $("#analysisResult");
  if (!target || !target.innerHTML.trim()) {
    toast("분석 결과가 없어요", "error");
    return;
  }
  if (typeof html2canvas === "undefined") {
    toast("이미지 라이브러리 로딩 안됨", "error");
    return;
  }

  const r = _lastAnalysisResult;
  const sym = r?.symbol || "분석";
  const company = r?.company_name ? ` (${r.company_name})` : "";
  toast("📸 이미지 생성 중...");

  try {
    // 캡처 시 공유 버튼 자체는 숨김 (이미지에 안 들어가게)
    const btnsToHide = target.querySelectorAll(".share-btn, .lang-toggle");
    btnsToHide.forEach(b => b.style.visibility = "hidden");

    // 다크 배경 그대로 캡처 (PWA 배경색 추정)
    const bg = getComputedStyle(document.body).backgroundColor || "#0f1419";
    const canvas = await html2canvas(target, {
      backgroundColor: bg,
      scale: 2,  // 고해상도
      useCORS: true,
      logging: false,
      windowWidth: target.scrollWidth,
      windowHeight: target.scrollHeight,
    });

    btnsToHide.forEach(b => b.style.visibility = "");

    // Blob → File
    const blob = await new Promise(res => canvas.toBlob(res, "image/png", 0.95));
    if (!blob) {
      toast("이미지 생성 실패", "error");
      return;
    }
    const filename = `AI분석_${sym}_${new Date().toISOString().slice(0,10)}.png`;
    const file = new File([blob], filename, {type: "image/png"});

    // Web Share API (iOS 15+, Android Chrome 등)
    // v6.2.1: 플랫폼 분기 — 모바일은 Web Share, PC는 클립보드+다운로드
    const isMobile = /iPhone|iPad|iPod|Android|Mobile/i.test(navigator.userAgent);
    const canShareFile = navigator.canShare && navigator.canShare({files: [file]});

    if (isMobile && canShareFile) {
      // 모바일: Web Share API (iOS Share Sheet / Android Share)
      try {
        await navigator.share({
          files: [file],
          title: `AI 분석: ${sym}${company}`,
          text: `${sym}${company} AI 분석 결과 — Gemini 2.5 Pro`,
        });
        toast("✓ 공유 완료", "success");
      } catch (e) {
        if (e.name !== "AbortError") {
          console.error("share failed:", e);
          toast("공유 취소", "error");
        }
      }
    } else {
      // PC 또는 미지원: 클립보드 복사 + 다운로드 동시 (카톡 PC는 Ctrl+V로 첨부 가능)
      let clipboardOk = false;
      try {
        if (navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([
            new ClipboardItem({"image/png": blob})
          ]);
          clipboardOk = true;
        }
      } catch (e) {
        console.warn("clipboard write failed:", e);
      }
      // 다운로드도 항상 (보험)
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);

      if (clipboardOk) {
        toast("✓ 📋 클립보드 복사 + 💾 다운로드 완료. 카톡/메모에 Ctrl+V 가능", "success");
      } else {
        toast("💾 사진 다운로드 완료. 카톡 등에 첨부", "success");
      }
    }
  } catch (e) {
    console.error("share error:", e);
    toast("이미지 생성 오류: " + (e.message || e), "error");
  }
}

// v5.0: 언어 전환
function switchLang(lang) {
  if (lang !== 'ko' && lang !== 'en') return;
  currentLang = lang;
  try { localStorage.setItem('analysis_lang', lang); } catch(e) {}
  $$('.lang-btn').forEach(b => b.classList.toggle('active', b.dataset.lang === lang));
  // 현재 분석 결과가 있으면 즉시 재렌더
  if (_lastAnalysisResult) displayAnalysisResult(_lastAnalysisResult);
}
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
  } else if (m === "kr") {
    $("#marketLabel").textContent = "국내 주식 (KOSPI/KOSDAQ)";
    $("#analyzeInput").placeholder = "삼성전자, 005930, SK하이닉스...";
    $("#analyzeTitle").textContent = "🔍 특정 국내 주식 심층 분석";
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
  _lastAnalysisResult = null;
  renderIndicesTabs();
  loadMovers();
  loadIndices();
}

// v5.0: 지수 위젯 — 마켓에 따라 서브탭 구성
// 지수 위젯은 항상 한국어 고정 (KO/EN 토글 영향 안 받음)
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
  const target = $("#indicesList");
  if (!target) return;  // v5.0.1: 캐시된 구 HTML에서는 위젯 없음 — silent skip
  if (!currentIndicesCat) renderIndicesTabs();
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

async function loadMovers() {
  const endpoint = currentMarket === "crypto"
    ? "/api/analysis/movers?n=10"
    : currentMarket === "kr"
    ? "/api/analysis/kr/movers?n=10"
    : "/api/analysis/stocks/movers?n=10";
  ["#topGainers", "#topLosers", "#topVolume"].forEach(s => {
    $(s).innerHTML = '<div class="loading-spinner">로딩...</div>';
  });
  const m = await api(endpoint);
  if (!m || m.error) {
    $("#topGainers").innerHTML = `<div class="empty">${m?.error || "로딩 실패"}</div>`;
    $("#topLosers").innerHTML = "";
    $("#topVolume").innerHTML = "";
    return;
  }

  if (currentMarket === "crypto") {
    $("#contextInfo").textContent = `Upbit KRW 마켓 ${m.total_pairs}개 분석 (5억 KRW 이상)`;
  } else if (currentMarket === "kr") {
    $("#contextInfo").textContent = `국내 주식 ${m.total_stocks}개 분석 (KOSPI/KOSDAQ, 10억+ 거래)`;
  } else {
    $("#contextInfo").textContent = `미국 주식 ${m.total_stocks}개 분석 (거래대금 $10M+)`;
  }

  const renderItem = (x) => {
    let price, symLabel;
    if (currentMarket === "crypto") {
      price = `${fmtKrw(x.price)} KRW · 거래 ${fmtKrw(x.volume_24h_krw/1e8)}억`;
      symLabel = x.symbol;
    } else if (currentMarket === "kr") {
      price = `${fmtKrw(x.price)} KRW · 거래 ${fmtKrw(x.volume_24h_krw/1e8)}억`;
      symLabel = x.name_ko ? `${x.name_ko} <span style="font-size:10px;color:var(--fg-dim)">${x.symbol.replace('.KS','').replace('.KQ','')}</span>` : x.symbol;
    } else {
      price = `$${fmt(x.price, 2)} · 거래 $${fmt(x.volume_24h_usd/1e9, 2)}B`;
      symLabel = x.symbol;
    }
    const noiseBadge = x.noise_flag ? '<span class="noise-flag" title="±25% 이상 변동 - 노이즈 가능">⚠️</span> ' : '';
    // v5.4.1: KR mover에 회사명도 전달
    const displayName = currentMarket === "kr" && x.name_ko
      ? `${x.name_ko} (${x.symbol.replace('.KS','').replace('.KQ','')})`
      : "";
    const safeDisplay = displayName.replace(/'/g, "\\'");
    return `
      <div class="mover-item" onclick="quickAnalyze('${x.symbol}', '${safeDisplay}')">
        <div>
          <div class="mover-symbol">${noiseBadge}${symLabel}</div>
          <div class="mover-meta">${price}</div>
        </div>
        <div class="mover-change ${x.change_24h > 0 ? "positive" : "negative"}">${fmtPct(x.change_24h)}</div>
      </div>
    `;
  };

  $("#topGainers").innerHTML = m.top_gainers.slice(0, 10).map(renderItem).join("");
  $("#topLosers").innerHTML = m.top_losers.slice(0, 5).map(renderItem).join("");
  $("#topVolume").innerHTML = m.top_volume.slice(0, 5).map(renderItem).join("");
  if ($("#moversUpdated")) {
    $("#moversUpdated").textContent = `갱신: ${new Date().toLocaleTimeString('ko-KR')}`;
  }
}

async function loadRecommend() {
  const btn = $("#recommendBtn");
  const target = $("#recommendations");
  btn.disabled = true;
  btn.textContent = "분석 중... (Pro 모델)";
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
    // v6.1.1: KR/주식이면 회사명 + 코드 같이 표시
    const isKR = currentMarket === "kr";
    const isStock = currentMarket === "stocks";
    let symLabel;
    if (isKR && x.name_ko) {
      const code = x.symbol.replace('.KS', '').replace('.KQ', '');
      symLabel = `${escapeHtml(x.name_ko)} <span style="font-size:11px;color:var(--fg-dim);font-weight:400">${code}</span>`;
    } else if (isStock && x.name_ko) {
      symLabel = `${x.symbol} <span style="font-size:11px;color:var(--fg-dim);font-weight:400">${escapeHtml(x.name_ko)}</span>`;
    } else {
      symLabel = x.symbol;
    }
    // 회사명도 quickAnalyze에 전달 (분석 진행 메시지용)
    const displayName = isKR && x.name_ko
      ? `${x.name_ko} (${x.symbol.replace('.KS', '').replace('.KQ', '')})`
      : isStock && x.name_ko
      ? `${x.name_ko} (${x.symbol})`
      : "";
    const safeDisplay = displayName.replace(/'/g, "\\'");
    return `
      <div class="recommend-item">
        <div class="recommend-header">
          <div>
            <span class="recommend-symbol">${symLabel}</span>
            <span class="recommend-badge ${x.risk_level}">위험 ${ko(x.risk_level)}</span>
            <span class="recommend-badge">${koHorizon(x.time_horizon)}</span>
          </div>
          <span class="recommend-badge">신뢰 ${fmt(x.confidence, 2)}</span>
        </div>
        <div class="recommend-thesis">${escapeHtml(thesis)}</div>
        <div style="margin-top:6px">
          <button class="btn" style="font-size:11px;padding:4px 8px" onclick="quickAnalyze('${x.symbol}', '${safeDisplay}')">심층분석 →</button>
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
    // v5.4.1: 검색 회사명도 같이 전달
    const code = first.symbol.replace(".KS", "").replace(".KQ", "");
    const displayName = first.name_ko && currentMarket === "kr"
      ? `${first.name_ko} (${code})`
      : first.name_ko && currentMarket === "stocks"
      ? `${first.name_ko} (${first.symbol})`
      : "";
    await runAnalysis(first.symbol, displayName);
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

function pickSuggestion(symbol, displayName) {
  $("#analyzeInput").value = symbol;
  const box = $("#searchSuggestions");
  if (box) box.style.display = "none";
  runAnalysis(symbol, displayName || "");
}

function quickAnalyze(sym, displayName) {
  // v5.4.1: KR 모드면 코드만 보여주고 회사명 별도 전달
  if (currentMarket === "kr") {
    const code = sym.replace(".KS", "").replace(".KQ", "");
    $("#analyzeInput").value = code;
  } else {
    $("#analyzeInput").value = sym;
  }
  setTimeout(() => runAnalysis(sym, displayName || ""), 50);
  window.scrollTo({top: document.body.scrollHeight, behavior: "smooth"});
}

// v5.4.1: KR 진행 메시지용 회사명 holder
let _pendingDisplayName = "";

async function runAnalysis(sym, displayName = "") {
  _pollAbort = false;
  _setAnalyzeBtnState("running");
  // v5.4.1: 진행 메시지에 쓸 회사명 결정
  _pendingDisplayName = displayName || sym;
  if (currentMarket === "kr" && !displayName) {
    // KR 모드 + 회사명 정보 없으면 search API로 자동 조회
    try {
      const code = sym.replace(".KS", "").replace(".KQ", "");
      const r = await api(`/api/search/kr_stocks?q=${encodeURIComponent(code)}&limit=1`);
      const name = r?.results?.[0]?.name_ko;
      if (name) _pendingDisplayName = `${name} (${code})`;
    } catch (e) {}
  }
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

// v5.1: 분석 폴링 중단 플래그
let _pollAbort = false;

function cancelAnalysis() {
  _pollAbort = true;
  localStorage.removeItem("pending_analysis");
  const target = $("#analysisResult");
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

// v5.1: 친절한 에러 메시지 매핑
function _friendlyError(msg) {
  if (!msg) return "분석 실패 — 다시 시도해주세요";
  const m = String(msg);
  if (/Expecting value|JSONDecode|JSON|응답 형식/.test(m)) {
    return "AI 응답이 너무 길어 잘렸어요. 다시 시도하면 보통 성공합니다.";
  }
  if (/timeout|Timeout|타임아웃/.test(m)) {
    return "응답 시간 초과 — 잠시 후 다시 시도해주세요";
  }
  if (/429|rate limit|시간당/.test(m)) {
    return m;  // rate limit은 원본 (시간당 10건 안내)
  }
  if (/500|Internal Server/.test(m)) {
    return "서버 오류 — 잠시 후 다시 시도해주세요";
  }
  if (/404|not found/.test(m)) {
    return "종목을 찾을 수 없어요. 티커를 확인해주세요";
  }
  return m.length > 80 ? m.substring(0, 80) + "…" : m;
}

function showAnalysisError(msg) {
  const target = $("#analysisResult");
  const friendly = _friendlyError(msg);
  if (target) target.innerHTML = `<div class="empty">⚠️ ${escapeHtml(friendly)}</div>`;
  _setAnalyzeBtnState("idle");
}

async function pollAnalysisJob(jobId, sym) {
  const target = $("#analysisResult");
  _pollAbort = false;
  _setAnalyzeBtnState("running");
  let attempts = 0;
  const poll = async () => {
    if (_pollAbort) return;
    if (target) {
      const elapsed = attempts * 2;
      const displayName = _pendingDisplayName || sym;
      target.innerHTML = `<div class="loading-spinner">
        🔍 ${escapeHtml(displayName)} 분석 진행 중... (${elapsed}초)<br>
        <span style="font-size:11px;color:var(--fg-faint)">💡 화면 나가도 OK. 다시 열면 자동 표시.</span>
      </div>`;
    }
    const r = await api(`/api/analysis/job/${jobId}`);
    if (_pollAbort) return;
    if (!r || r.status === "not_found") {
      showAnalysisError("작업 없음 (시간 경과 또는 재시작)");
      localStorage.removeItem("pending_analysis");
      return;
    }
    if (r.status === "completed") {
      localStorage.removeItem("pending_analysis");
      displayAnalysisResult(r.result);
      _setAnalyzeBtnState("idle");
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
  _lastAnalysisResult = r;
  const a = r.analysis || {};
  const raw = r.raw_data || {};
  // v5.2: isStock = US 또는 KR 주식, isUSD = US 주식만 (가격 통화 분기용)
  const isStock = currentMarket === "stocks" || currentMarket === "kr";
  const isUSD = currentMarket === "stocks";
  const isKR = currentMarket === "kr";
  const summary = L(a, 'summary');
  const setup = currentLang === 'en'
    ? (a.current_setup_en || a.current_setup || ko(a.current_setup))
    : (a.current_setup_ko || ko(a.current_setup) || "?");
  const risks = Larr(a, 'key_risks');
  const catalysts = Larr(a, 'key_catalysts');
  // v6.2: 신뢰도 배지 + sanity warnings
  const reliability = a._reliability || "HIGH";
  const warnings = a._sanity_warnings || [];
  const reliabilityBadge = reliability === "LOW"
    ? '<span class="reliability-badge low">⚠️ 신뢰도 낮음</span>'
    : reliability === "MEDIUM"
    ? '<span class="reliability-badge med">⚡ 신뢰도 중간</span>'
    : '';
  const warningsHTML = warnings.length > 0
    ? `<div class="sanity-warnings">
         <div class="warnings-title">⚠️ 자동 검증 경고 (${warnings.length}건)</div>
         ${warnings.map(w => `<div class="warning-item">• ${escapeHtml(w)}</div>`).join('')}
         <div class="warnings-note">→ KRX/네이버증권에서 원본 데이터 직접 확인 권고</div>
       </div>`
    : '';

  // v5.0.1: KO/EN 토글 + v5.4: 공유 버튼
  const langToggleHTML = `
    <div class="result-lang-toggle">
      <span class="lang-label">${currentLang === 'en' ? 'Analysis language:' : '분석 언어:'}</span>
      <div class="lang-toggle">
        <button class="lang-btn ${currentLang === 'ko' ? 'active' : ''}" data-lang="ko" onclick="switchLang('ko')">KO</button>
        <button class="lang-btn ${currentLang === 'en' ? 'active' : ''}" data-lang="en" onclick="switchLang('en')">EN</button>
      </div>
      <button class="share-btn" onclick="shareAnalysis()" title="${currentLang === 'en' ? 'Share as image' : '이미지로 공유'}">📤 ${currentLang === 'en' ? 'Share' : '공유'}</button>
    </div>`;
  const entry = a.entry_zone_krw || a.entry_zone_usd || [];
  const stop = a.stop_loss_krw ?? a.stop_loss_usd;
  const t1 = a.target_1_krw ?? a.target_1_usd;
  const t2 = a.target_2_krw ?? a.target_2_usd;
  const resistance = a.resistance_levels_krw || a.resistance_levels_usd || [];
  const support = a.support_levels_krw || a.support_levels_usd || [];
  const fmtPrice = v => v == null ? "—" : (isUSD ? "$" + fmt(v, 2) : fmtKrw(v));
  const header = isStock && r.company_name
    ? `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · ${escapeHtml(r.company_name)} · 현재가 ${fmtPrice(r.current_price)}${isKR ? ' KRW' : ''}</div>`
    : `<div style="font-size:11px;color:var(--fg-dim);margin-bottom:8px">${escapeHtml(r.symbol)} · 현재가 ${fmtPrice(r.current_price)}${isKR ? ' KRW' : ''}</div>`;
  const rsiLine = isStock
    ? `RSI(daily) ${fmt(raw.rsi_daily, 0)} · RSI(hourly) ${fmt(raw.rsi_hourly, 0)}${raw.sector ? ' · ' + raw.sector : ''}${raw.forward_pe ? ' · P/E ' + fmt(raw.forward_pe, 1) : ''}${isKR && raw.kospi_change != null ? ' · KOSPI ' + fmtPct(raw.kospi_change) : ''}`
    : `RSI(1h) ${fmt(raw.rsi_1h, 0)} · RSI(daily) ${fmt(raw.rsi_daily, 0)} · 30일 범위 ${fmt(raw.position_30d_pct, 0)}%`;
  target.innerHTML = `
    ${langToggleHTML}
    ${header}
    ${reliabilityBadge}
    ${warningsHTML}
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
      <b>💬 ${currentLang === 'en' ? 'Detailed Analysis' : '상세 분석'}</b><br>${escapeHtml(currentLang === 'en' ? (a.english_advice || a.korean_advice || '') : (a.korean_advice || ''))}<br><br>
      <b>⚠️ ${currentLang === 'en' ? 'Key Risks' : '주요 리스크'}:</b><br>${risks.map(x => `· ${escapeHtml(x)}`).join('<br>')}<br><br>
      <b>🚀 ${currentLang === 'en' ? 'Bullish Catalysts' : '상승 모멘텀'}:</b><br>${catalysts.map(x => `· ${escapeHtml(x)}`).join('<br>')}
    </div>
    ${(L(a, 'valuation') || a.valuation_ko) ? `
      <div class="extra-section">
        <div class="extra-title">💰 ${currentLang === 'en' ? 'Valuation' : '밸류에이션 평가'}
          ${a.valuation_verdict ? `<span class="verdict-badge ${a.valuation_verdict}">${currentLang === 'en' ? a.valuation_verdict : ko_verdict(a.valuation_verdict)}</span>` : ''}
        </div>
        <div>${escapeHtml(L(a, 'valuation'))}</div>
        ${a.valuation_peer_comparison ? `<div class="extra-meta">📊 ${escapeHtml(a.valuation_peer_comparison)}</div>` : ''}
      </div>` : ''}
    ${(L(a, 'core_business') || L(a, 'use_case')) ? `
      <div class="extra-section">
        <div class="extra-title">${isStock ? (currentLang === 'en' ? '🏢 Core Business' : '🏢 본업 분석') : (currentLang === 'en' ? '🔧 Use Case / Network' : '🔧 유즈케이스/네트워크')}</div>
        <div>${escapeHtml(L(a, 'core_business') || L(a, 'use_case'))}</div>
        ${a.core_business_segments ? `
          <div class="extra-meta">
            ${(a.core_business_segments || []).map(s => `
              <span class="segment-chip ${s.trend}">${escapeHtml(s.name)} ${s.revenue_share_pct ?? '?'}% ${trend_icon(s.trend)}</span>
            `).join('')}
          </div>` : ''}
      </div>` : ''}
    ${(L(a, 'growth_drivers') || L(a, 'tokenomics')) ? `
      <div class="extra-section">
        <div class="extra-title">${isStock ? (currentLang === 'en' ? '🚀 Growth Drivers' : '🚀 신규 성장 동력') : (currentLang === 'en' ? '🪙 Tokenomics' : '🪙 토크노믹스')}</div>
        <div>${escapeHtml(L(a, 'growth_drivers') || L(a, 'tokenomics'))}</div>
      </div>` : ''}
    ${L(a, 'shareholder_returns') ? `
      <div class="extra-section">
        <div class="extra-title">💸 ${currentLang === 'en' ? 'Shareholder Returns' : '주주 환원'}</div>
        <div>${escapeHtml(L(a, 'shareholder_returns'))}</div>
      </div>` : ''}
    ${(L(a, 'geopolitical_risk') || L(a, 'regulatory_risk')) ? `
      <div class="extra-section danger">
        <div class="extra-title">🌍 ${isStock ? (currentLang === 'en' ? 'Geopolitical / Regulatory Risk' : '지정학/규제 리스크') : (currentLang === 'en' ? 'Regulatory Risk' : '규제 리스크')}</div>
        <div>${escapeHtml(L(a, 'geopolitical_risk') || L(a, 'regulatory_risk'))}</div>
      </div>` : ''}
    ${L(a, 'company_guidance') ? `
      <div class="extra-section">
        <div class="extra-title">📢 ${currentLang === 'en' ? 'Company Guidance vs Consensus' : '회사 가이던스 vs 컨센서스'}</div>
        <div>${escapeHtml(L(a, 'company_guidance'))}</div>
      </div>` : ''}
    ${a.horizon_analysis ? `
      <div class="extra-section">
        <div class="extra-title">⏱ ${currentLang === 'en' ? 'Multi-Horizon Outlook' : '시간 프레임별 전망'}</div>
        ${['short_term_1w', 'medium_term_3m', 'long_term_1y'].map(h => {
          const ha = a.horizon_analysis[h]; if (!ha) return '';
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
            </div>`;
        }).join('')}
      </div>` : ''}
    ${a.scenarios ? `
      <div class="extra-section">
        <div class="extra-title">🎯 ${currentLang === 'en' ? 'Scenario Analysis (Bull / Base / Bear)' : '시나리오 분석 (Bull / Base / Bear)'}</div>
        ${['bullish', 'base', 'bearish'].map(k => {
          const s = a.scenarios[k]; if (!s) return '';
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
              ${(triggers && triggers.length > 0) ? `<div class="scenario-triggers">${triggersLabel}: ${triggers.map(escapeHtml).join(' · ')}</div>` : ''}
            </div>`;
        }).join('')}
      </div>` : ''}
    ${L(a, 'data_freshness_note') ? `
      <div class="extra-section" style="border-left-color:var(--warning)">
        <div class="extra-title">⚠️ ${currentLang === 'en' ? 'Data Freshness Note' : '데이터 신선도 노트'}</div>
        <div style="font-size:11px">${escapeHtml(L(a, 'data_freshness_note'))}</div>
      </div>` : ''}

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
  `;
  toast(`✓ ${r.symbol || ''} 분석 완료`, "success");
}

// v7.1: 분석에 사용된 도구 & 기법 섹션
function renderToolsSection() {
  const toolsData = [
    { icon: '🤖', title: 'AI/ML 엔진', items: [
      'Gemini 2.5 Flash/Pro — 메인 AI 판단',
      'XGBoost (40%) — 지표 기반 분류',
      'LSTM (30%) — 시계열 예측',
      'DQN (30%) — 강화학습 트레이더',
      'Ollama 로컬 폴백 — API 장애 시'
    ]},
    { icon: '📊', title: '기술적 분석 지표 (20+)', items: [
      'EMA (9/21/50/200), RSI (14), Stochastic RSI',
      'MACD (12/26/9), Bollinger Bands (20, 2σ)',
      'ATR (14), ADX (14), Disparity Index (20)',
      'Donchian Channel (20/55일)'
    ]},
    { icon: '🎯', title: '검증된 매매 전략', items: [
      'Larry Williams 변동성 돌파 (K=0.7, 베어마켓 58%)',
      'Turtle Trading (돈치안 돌파, 역사적 12,636%)',
      'BB+RSI+ADX 평균회귀 (백테스트 179%)',
      'BNF 역발상 매매 (이격도 + RSI + MACD)',
      'Triple Confirmation (Stochastic + RSI + MACD)',
      'MACD Zero-Line Cross (승률 86%)',
      '200 EMA Trend Filter'
    ]},
    { icon: '📐', title: '투자 방법론 점수', items: [
      "CANSLIM (O'Neil), SEPA (Minervini), Stage (Weinstein)",
      'Wyckoff, Quality+Value, Momentum+RS'
    ]},
    { icon: '🌡', title: '시장 상태 분류 (6단계)', items: [
      'STRONG_UPTREND → MILD_UPTREND → SIDEWAYS',
      'MILD_DOWNTREND → STRONG_DOWNTREND → TRANSITION'
    ]},
    { icon: '🔌', title: '데이터 소스', items: [
      'Vertex AI Grounding — 실시간 검색',
      'Tavily API — 뉴스 3일',
      'OpenDART — 재무제표 (한국 종목)',
      'KIS API — 한국투자증권 시세/주문',
      'Upbit API — 암호화폐 시세'
    ]},
    { icon: '🛡', title: '리스크 관리', items: [
      'Guardrails — 환각 방지',
      'Sanity Check — 가격 일관성 검증',
      'Market Regime 기반 전략 자동 선택',
      '포지션 사이징 가이드 (Kelly Criterion)'
    ]},
    { icon: '⚙️', title: '인프라', items: [
      'Freqtrade — 암호화폐 자동매매',
      'LaunchAgent — macOS 스케줄링',
      'iCloud 자동 백업 (매일 4am)',
      'ML 자동 재학습 (매주 일요일)',
      'Telegram / KakaoTalk 알림'
    ]}
  ];
  const sid = 'tools-' + Math.random().toString(36).slice(2,9);
  const cats = toolsData.map(c => `
    <div style="margin-bottom:12px;">
      <div style="font-size:0.85rem;font-weight:600;color:var(--fg);margin-bottom:4px;">${c.icon} ${c.title}</div>
      <ul style="margin:0;padding-left:20px;list-style:disc;font-size:0.78rem;line-height:1.5;">
        ${c.items.map(i => `<li style="padding:2px 0;color:var(--fg-dim);">${i}</li>`).join('')}
      </ul>
    </div>`).join('');
  return `
    <div style="margin-top:24px;border:1px solid var(--border,rgba(255,255,255,0.08));border-radius:10px;background:var(--bg-card);overflow:hidden;">
      <button onclick="(function(){var c=document.getElementById('${sid}'),a=document.getElementById('${sid}-a');if(c.style.display==='none'){c.style.display='block';a.textContent='▾'}else{c.style.display='none';a.textContent='▸'}})()"
        style="width:100%;display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:transparent;border:none;cursor:pointer;color:var(--fg);font-size:0.9rem;font-weight:600;text-align:left;">
        <span>🛠 분석에 사용된 도구 & 기법</span>
        <span id="${sid}-a" style="font-size:0.85rem;color:var(--fg-dim);margin-left:8px;">▸</span>
      </button>
      <div id="${sid}" style="display:none;padding:4px 16px 16px 16px;">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px 24px;">
          ${cats}
        </div>
        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border,rgba(255,255,255,0.06));font-size:0.72rem;color:var(--fg-dim);text-align:center;">
          총 20+ 기술 지표 · 7개 매매 전략 · 6개 방법론 점수 · 5개 AI/ML 모델 · 5개 데이터 소스
        </div>
      </div>
    </div>`;
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
  loadIndices();
  toast("새로고침 완료");
});

document.addEventListener("DOMContentLoaded", () => {
  // v5.0: 저장된 언어 설정 복원
  $$('.lang-btn').forEach(b => b.classList.toggle('active', b.dataset.lang === currentLang));
  renderIndicesTabs();
  // v7.1: 도구 & 기법 섹션 — 최상단 초기 렌더링
  const toolsTop = document.getElementById("toolsSectionTop");
  if (toolsTop) toolsTop.innerHTML = renderToolsSection();
  loadIndices();
  loadMovers();
  setupAutocomplete();
  restorePendingAnalysis();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    restorePendingAnalysis();
    loadMovers();
    loadIndices();
  }
});
