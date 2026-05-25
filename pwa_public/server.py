#!/usr/bin/env python3
"""v4.4: 공유용 분석 PWA — 지인 공유 목적.

원본 PWA(8088)에서 봇 제어/포지션/통합뷰는 모두 제거.
오직 코인/주식 분석 3종만:
  - TopMovers
  - AI 추천 (Gemini Pro)
  - 특정 종목 심층 분석

특징:
  - Tailscale Funnel로 공개 HTTPS URL 노출
  - 간단한 rate limiting (IP당 시간당 10건 LLM 호출)
  - 봇 데이터 노출 X (보안)
  - GCP 크레딧만 사용
"""
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# scripts 경로 (분석 모듈 import용)
TRADING_ROOT = Path(os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading")))
sys.path.insert(0, str(TRADING_ROOT / "scripts"))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("pwa_public")

app = FastAPI(title="AI 트레이더 분석 — 공유용")

# CORS (안전: 모두 허용, 분석 결과만이라 OK)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# v5.0.3: 모든 응답에 no-cache 헤더 강제 (iOS 사파리 캐시 무력화)
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# ─── Rate Limiting ────────────────────────────
# IP당 시간당 10건의 LLM 호출 제한 (TopMovers는 무제한, 분석/추천만 제한)
_request_log: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 10  # 시간당
RATE_WINDOW = 3600  # 1시간


def check_rate_limit(request: Request) -> bool:
    """IP 기반 rate limit. True면 차단."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    log = _request_log[ip]
    # 윈도우 밖 항목 제거
    _request_log[ip] = [t for t in log if now - t < RATE_WINDOW]
    if len(_request_log[ip]) >= RATE_LIMIT:
        return True
    _request_log[ip].append(now)
    return False


# ─── 라우팅 ────────────────────────────────────
@app.get("/")
def index():
    # v5.2.4: 동적 cache-bust — 사파리가 ?v= 정적 쿼리를 캐시하는 경우 대응
    from fastapi.responses import HTMLResponse
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    ts = int(time.time())
    html = html.replace("app.js?v=5.2.0", f"app.js?v={ts}")
    html = html.replace("style.css?v=5.2.0", f"style.css?v={ts}")
    return HTMLResponse(html)


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"))


# v5.3: Service Worker (자동 갱신)
@app.get("/sw.js")
def sw_js():
    return FileResponse(
        str(STATIC_DIR / "sw.js"),
        media_type="application/javascript",
    )


@app.get("/apple-touch-icon.png")
def apple_icon():
    p = STATIC_DIR / "apple-touch-icon.png"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "AI Trader Analysis (public)", "ts": time.time()}


# ─── 코인 분석 (rate limit 없음, 가벼움) ────
@app.get("/api/analysis/movers")
def crypto_movers(n: int = 10):
    try:
        from coin_analyzer import fetch_top_movers
        return fetch_top_movers(n=n)
    except Exception as e:
        logger.exception("crypto_movers failed")
        return {
            "timestamp": time.time(),
            "total_pairs": 0,
            "top_gainers": [],
            "top_losers": [],
            "top_volume": [],
            "error": str(e)[:200],
        }


# ─── v5.2: 국내 주식 분석 ───────────────────────
@app.get("/api/analysis/kr/movers")
def kr_movers(n: int = 10):
    try:
        from stock_analyzer import fetch_top_movers_kr
        return fetch_top_movers_kr(n=n)
    except Exception as e:
        logger.exception("kr_movers failed")
        return {
            "timestamp": time.time(),
            "total_stocks": 0,
            "top_gainers": [],
            "top_losers": [],
            "top_volume": [],
            "error": str(e)[:200],
        }


@app.get("/api/analysis/kr/recommend")
def kr_recommend(request: Request, n: int = 5):
    if check_rate_limit(request):
        return JSONResponse({"error": f"시간당 {RATE_LIMIT}건 제한 도달."}, status_code=429)
    try:
        from stock_analyzer import recommend_kr_stocks
        return recommend_kr_stocks(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/kr/{symbol}")
def kr_analyze(request: Request, symbol: str):
    if check_rate_limit(request):
        return JSONResponse({"error": f"시간당 {RATE_LIMIT}건 제한 도달."}, status_code=429)
    try:
        from stock_analyzer import analyze_kr_stock
        return analyze_kr_stock(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── v4.5: 종목명 검색 (KR) ──
@app.get("/api/search/kr_stocks")
def search_kr_stocks_ep(q: str, limit: int = 8):
    try:
        from symbol_search import search_kr_stocks
        return {"query": q, "results": search_kr_stocks(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/stocks/movers")
def stocks_movers(n: int = 10):
    try:
        from stock_analyzer import fetch_top_movers_stocks
        return fetch_top_movers_stocks(n=n)
    except Exception as e:
        # v5.0.4: 500 대신 빈 결과 (프론트가 깨지지 않도록)
        logger.exception("stocks_movers failed")
        return {
            "timestamp": time.time(),
            "total_stocks": 0,
            "top_gainers": [],
            "top_losers": [],
            "top_volume": [],
            "error": str(e)[:200],
        }


# ─── AI 추천 (rate limit 적용) ─────────────────
@app.get("/api/analysis/recommend")
def crypto_recommend(request: Request, n: int = 5):
    if check_rate_limit(request):
        return JSONResponse(
            {"error": f"시간당 {RATE_LIMIT}건 제한 도달. 잠시 후 다시 시도하세요."},
            status_code=429,
        )
    try:
        from coin_analyzer import recommend_coins
        return recommend_coins(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/stocks/recommend")
def stocks_recommend(request: Request, n: int = 5):
    if check_rate_limit(request):
        return JSONResponse(
            {"error": f"시간당 {RATE_LIMIT}건 제한 도달."},
            status_code=429,
        )
    try:
        from stock_analyzer import recommend_stocks
        return recommend_stocks(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── 심층 분석 (rate limit 적용) ───────────────
@app.get("/api/analysis/coin/{symbol}")
def analyze_coin_ep(request: Request, symbol: str):
    if check_rate_limit(request):
        return JSONResponse(
            {"error": f"시간당 {RATE_LIMIT}건 제한 도달."},
            status_code=429,
        )
    try:
        from coin_analyzer import analyze_coin
        return analyze_coin(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/stocks/{symbol}")
def analyze_stock_ep(request: Request, symbol: str):
    if check_rate_limit(request):
        return JSONResponse(
            {"error": f"시간당 {RATE_LIMIT}건 제한 도달."},
            status_code=429,
        )
    try:
        from stock_analyzer import analyze_stock
        return analyze_stock(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── v4.8: 비동기 작업 (PWA 백그라운드 문제 해결) ──
import uuid as _uuid
import threading as _threading

_jobs: dict = {}
_result_cache: dict = {}
_JOB_TTL = 3600
_CACHE_TTL = 300


def _cleanup_old_jobs():
    now = time.time()
    for jid in list(_jobs.keys()):
        if now - _jobs[jid].get("started", now) > _JOB_TTL:
            del _jobs[jid]
    for k in list(_result_cache.keys()):
        if now - _result_cache[k]["ts"] > _CACHE_TTL:
            del _result_cache[k]


def _run_job(job_id: str, market: str, symbol: str):
    try:
        cache_key = (market, symbol)
        cached = _result_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
            _jobs[job_id].update({
                "status": "completed", "result": cached["result"],
                "completed_at": time.time(), "from_cache": True,
            })
            return
        if market == "crypto":
            from coin_analyzer import analyze_coin
            result = analyze_coin(symbol)
        elif market == "kr":
            from stock_analyzer import analyze_kr_stock
            result = analyze_kr_stock(symbol)
        else:
            from stock_analyzer import analyze_stock
            result = analyze_stock(symbol)
        _result_cache[cache_key] = {"result": result, "ts": time.time()}
        _jobs[job_id].update({
            "status": "completed", "result": result,
            "completed_at": time.time(), "from_cache": False,
        })
    except Exception as e:
        _jobs[job_id].update({
            "status": "failed", "error": str(e)[:300],
            "completed_at": time.time(),
        })


@app.post("/api/analysis/job/start")
def start_job(request: Request, payload: dict):
    # rate limit는 분석 시작 시점에 한 번 체크
    if check_rate_limit(request):
        return JSONResponse({"error": f"시간당 {RATE_LIMIT}건 제한 도달."}, status_code=429)
    market = payload.get("market", "crypto")
    symbol = payload.get("symbol", "").upper().strip()
    if not symbol or market not in ("crypto", "stocks", "kr"):  # v5.2: kr 추가
        return JSONResponse({"error": "invalid params"}, status_code=400)
    _cleanup_old_jobs()
    job_id = str(_uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending", "market": market, "symbol": symbol, "started": time.time(),
    }
    _threading.Thread(target=_run_job, args=(job_id, market, symbol), daemon=True).start()
    return {"job_id": job_id, "status": "pending", "symbol": symbol, "market": market}


@app.get("/api/analysis/job/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    elapsed = time.time() - job.get("started", time.time())
    return {**job, "elapsed_seconds": elapsed}


# ─── v5.0: 시장 지수/ETF/외환/원자재 위젯 (가벼움, 캐시) ──
_indices_cache: dict = {}
_INDICES_CACHE_TTL = 60  # 1분


@app.get("/api/analysis/indices")
def indices_ep(cat: str = "us_index"):
    """v5.0.6: importlib.reload + 빈 결과 디버그 정보 노출."""
    now = time.time()
    cached = _indices_cache.get(cat)
    if cached and (now - cached["ts"]) < _INDICES_CACHE_TTL:
        return cached["result"]
    try:
        # v5.2.3: importlib.reload 제거 — 파일 핸들 leak으로 OSError [Errno 24] 발생
        from market_indices import fetch_indices
        result = fetch_indices(cat)
        if result.get("count", 0) > 0:
            _indices_cache[cat] = {"result": result, "ts": now}
        else:
            result["_debug"] = {
                "items_len": len(result.get("items", [])),
            }
        return result
    except Exception as e:
        import traceback
        logger.exception(f"indices_ep({cat}) failed")
        return {
            "timestamp": time.time(),
            "category": cat,
            "items": [],
            "count": 0,
            "error": str(e)[:200],
            "_traceback": traceback.format_exc()[-400:],
        }


# ─── v4.5: 종목명 검색 (rate limit 없음, 가벼움) ──
@app.get("/api/search/coins")
def search_coins_ep(q: str, limit: int = 8):
    try:
        from symbol_search import search_coins
        return {"query": q, "results": search_coins(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/search/stocks")
def search_stocks_ep(q: str, limit: int = 8):
    try:
        from symbol_search import search_stocks
        return {"query": q, "results": search_stocks(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("PWA_HOST", "0.0.0.0")
    port = int(os.environ.get("PWA_PORT", "8089"))
    logger.info(f"공유용 PWA 시작 — http://{host}:{port}")
    logger.info(f"Rate limit: IP당 시간당 {RATE_LIMIT}건 (LLM 호출)")
    uvicorn.run(app, host=host, port=port, log_level="info")
