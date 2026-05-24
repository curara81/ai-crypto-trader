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
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"))


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
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/stocks/movers")
def stocks_movers(n: int = 10):
    try:
        from stock_analyzer import fetch_top_movers_stocks
        return fetch_top_movers_stocks(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


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


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("PWA_HOST", "0.0.0.0")
    port = int(os.environ.get("PWA_PORT", "8089"))
    logger.info(f"공유용 PWA 시작 — http://{host}:{port}")
    logger.info(f"Rate limit: IP당 시간당 {RATE_LIMIT}건 (LLM 호출)")
    uvicorn.run(app, host=host, port=port, log_level="info")
