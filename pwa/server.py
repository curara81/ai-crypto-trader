#!/usr/bin/env python3
"""AI 트레이더 PWA Backend — FastAPI.

4개 탭의 데이터를 통합 노출 + 명령어 실행.

엔드포인트:
  GET  /                          → SPA 정적 페이지
  GET  /api/health                → 모든 서비스 상태
  GET  /api/crypto/status         → 오픈 포지션 + 누적 손익
  GET  /api/crypto/decisions      → 최근 Gemini 결정
  GET  /api/crypto/filters        → Fear&Greed + 필터 상태
  POST /api/crypto/killswitch     → KILLSWITCH 토글
  POST /api/crypto/forceexit/{id} → 강제 청산
  POST /api/crypto/control        → start/stop/reload_config
  GET  /api/stocks/us/status      → US 주식봇 상태 + 결정
  GET  /api/stocks/kr/positions   → 국내주식 (tossctl)
  GET  /api/summary               → 모의투자 통합 뷰
"""
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ─── 경로 ─────────────────────────────────────
TRADING_ROOT = Path(os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading")))
USERDATA = TRADING_ROOT / "freqtrade_userdata"
LOG_DIR = USERDATA / "logs"
SCRIPTS_DIR = TRADING_ROOT / "scripts"
KILLSWITCH = TRADING_ROOT / "KILLSWITCH"

sys.path.insert(0, str(SCRIPTS_DIR))

# Freqtrade API
FT_API = "http://127.0.0.1:8080/api/v1"
FT_AUTH = ("freqtrade", "freqtrade")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
logger = logging.getLogger("pwa")

app = FastAPI(title="AI Trader Dashboard")

# ─── 정적 파일 ───────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# v5.0.3: 모든 응답에 no-cache 헤더 강제 (iOS 사파리 캐시 무력화)
@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(STATIC_DIR / "manifest.json"))


@app.get("/sw.js")
def service_worker():
    return FileResponse(str(STATIC_DIR / "sw.js"), media_type="application/javascript")


@app.get("/apple-touch-icon.png")
def apple_icon():
    p = STATIC_DIR / "apple-touch-icon.png"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({"error": "not found"}, status_code=404)


# ─── 헬퍼 ────────────────────────────────────
async def ft_get(endpoint: str, params: Optional[dict] = None):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{FT_API}/{endpoint}", auth=FT_AUTH, params=params or {})
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.warning(f"FT API {endpoint} 실패: {e}")
        return None


async def ft_post(endpoint: str, payload: Optional[dict] = None):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{FT_API}/{endpoint}", auth=FT_AUTH, json=payload or {})
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def tail_jsonl(path: Path, n: int = 20) -> list[dict]:
    """JSONL 파일의 마지막 N개 항목."""
    if not path.exists():
        return []
    try:
        with open(path) as f:
            lines = f.readlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
        return out
    except Exception:
        return []


# ─── 헬스 ────────────────────────────────────
@app.get("/api/health")
async def health():
    """모든 서비스 상태."""
    services = {}
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        )
        for name in [
            "freqtrade-dryrun", "freqtrade-aibot", "freqtrade-notifier",
            "freqtrade-analyzer", "freqtrade-health", "freqtrade-dailyreport",
            "kis-stockbot",
        ]:
            services[name] = f"com.curara.{name}" in result.stdout
    except Exception:
        pass

    killswitch_active = KILLSWITCH.exists()
    return {
        "services": services,
        "killswitch": killswitch_active,
        "killswitch_reason": KILLSWITCH.read_text().strip()[:200] if killswitch_active else None,
        "ts": time.time(),
    }


# ─── 코인 탭 ────────────────────────────────
@app.get("/api/crypto/status")
async def crypto_status():
    """현재 포지션 + 누적 손익."""
    profit = await ft_get("profit") or {}
    trades = await ft_get("status") or []
    open_trades = trades if isinstance(trades, list) else []

    return {
        "open_trades": [
            {
                "pair": t["pair"],
                "open_rate": t["open_rate"],
                "current_rate": t.get("current_rate", 0),
                "profit_pct": t.get("profit_pct", 0),
                "profit_abs": t.get("profit_abs", 0),
                "stake_amount": t.get("stake_amount", 0),
                "open_date": t.get("open_date", ""),
                "stoploss_pct": t.get("stop_loss_pct", 0),
            }
            for t in open_trades
        ],
        "total_trades": profit.get("trade_count", 0),
        "closed_trades": profit.get("closed_trade_count", 0),
        "profit_total_krw": profit.get("profit_all_fiat", 0),
        "profit_total_pct": profit.get("profit_all_percent", 0),
        "winrate": profit.get("winrate", 0) * 100,
        "best_pair": profit.get("best_pair", ""),
        "best_pair_pct": profit.get("best_pair_profit_ratio", 0) * 100,
        "avg_duration_min": int(profit.get("avg_duration", "0:0:0").split(":")[0]) * 60
                            + int(profit.get("avg_duration", "0:0:0").split(":")[1])
                            if profit.get("avg_duration") and ":" in str(profit.get("avg_duration"))
                            else 0,
    }


@app.get("/api/crypto/decisions")
async def crypto_decisions():
    """최근 Gemini 결정 20개."""
    entries = tail_jsonl(LOG_DIR / "gemini_decisions.jsonl", 20)
    return {
        "decisions": [
            {
                "ts": e.get("timestamp", "")[:19],
                "pair": e.get("pair", "?"),
                "action": e.get("action", "?"),
                "confidence": e.get("confidence", 0),
                "reason": e.get("reason", "")[:200],
                "price": e.get("price", 0),
            }
            for e in entries[::-1]
        ]
    }


@app.get("/api/crypto/filters")
async def crypto_filters():
    """v3.4 4개 필터의 실시간 상태."""
    out = {"timestamp": time.time()}
    try:
        from market_filters import FearGreedFilter, TimeWindowFilter
        fg = FearGreedFilter()
        score = fg.get_score()
        out["fear_greed"] = {
            "score": score,
            "classification": (
                "Extreme Fear" if score and score <= 25 else
                "Fear" if score and score <= 45 else
                "Neutral" if score and score <= 55 else
                "Greed" if score and score <= 75 else
                "Extreme Greed" if score else "?"
            ),
            "blocks_buy": score is not None and score >= 75,
        }
        tw = TimeWindowFilter()
        blocked, reason = tw.should_block()
        out["time_window"] = {"blocked": blocked, "reason": reason}
    except Exception as e:
        out["error"] = str(e)
    return out


@app.post("/api/crypto/killswitch")
async def toggle_killswitch(payload: dict):
    """KILLSWITCH 토글."""
    action = payload.get("action", "toggle")
    if action == "on" or (action == "toggle" and not KILLSWITCH.exists()):
        KILLSWITCH.write_text(f"manual via PWA at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        return {"killswitch": True, "msg": "비상정지 활성 (모든 신규 진입 차단)"}
    else:
        if KILLSWITCH.exists():
            KILLSWITCH.unlink()
        return {"killswitch": False, "msg": "비상정지 해제"}


@app.post("/api/crypto/forceexit/{trade_id}")
async def force_exit(trade_id: int):
    """특정 trade 강제 청산."""
    return await ft_post("forceexit", {"tradeid": str(trade_id)})


@app.post("/api/crypto/control")
async def crypto_control(payload: dict):
    """봇 제어: start / stop / reload_config / stopbuy"""
    cmd = payload.get("command")
    if cmd not in ("start", "stop", "reload_config", "stopbuy"):
        raise HTTPException(400, "unsupported command")
    return await ft_post(cmd)


# ─── 해외주식 탭 ─────────────────────────────
@app.get("/api/stocks/us/status")
async def us_status():
    """KIS 미국주식 봇 상태 + 최근 결정."""
    decisions = tail_jsonl(LOG_DIR / "stock_decisions.jsonl", 20)

    # 봇 가동 여부
    alive = False
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3)
        alive = "com.curara.kis-stockbot" in result.stdout
    except Exception:
        pass

    return {
        "alive": alive,
        "decisions": [
            {
                "ts": e.get("timestamp", "")[:19],
                "symbol": e.get("symbol", "?"),
                "action": e.get("action", "?"),
                "confidence": e.get("confidence", 0),
                "reason": e.get("reason", "")[:200],
                "price": e.get("price", 0),
            }
            for e in decisions[::-1]
        ],
        "decision_count": len(decisions),
        "market_open": _us_market_open(),
    }


def _us_market_open() -> bool:
    """미국 장 시간 (KST 22:30-05:00 DST 기준 간이)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    h = now_kst.hour
    # 22:30~05:00
    return h >= 23 or h < 5 or (h == 22 and now_kst.minute >= 30)


# ─── 국내주식 탭 ─────────────────────────────
@app.get("/api/stocks/kr/positions")
async def kr_positions():
    """tossctl로 한국주식 보유 종목 조회 (캐시 1분)."""
    cache_file = Path("/tmp/tossctl_cache.json")
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 60:
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    out = {"positions": [], "summary": {}, "error": None}
    try:
        tossctl = str(TRADING_ROOT / "tossctl")
        result = subprocess.run(
            [tossctl, "portfolio", "positions"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            # 헤더 + 구분선 제외하고 파싱
            for line in lines[2:]:
                # ──── 구분선 스킵
                if "─" in line or not line.strip():
                    continue
                # 종목명 (코드) | 수량 | 매입가 | 현재가 | 평가금 | 손익 | 수익률 | 일간손익 | 일간률
                parts = [p for p in line.split() if p.strip()]
                if len(parts) >= 8:
                    out["positions"].append({"raw": line.strip()})

        # 계좌 요약
        summary_r = subprocess.run(
            [tossctl, "account", "summary"],
            capture_output=True, text=True, timeout=10,
        )
        if summary_r.returncode == 0:
            out["summary"] = {"raw": summary_r.stdout.strip()}
    except Exception as e:
        out["error"] = str(e)

    cache_file.write_text(json.dumps(out))
    return out


# ─── 모의투자 통합 탭 ───────────────────────
@app.get("/api/summary")
async def summary():
    """전체 봇 + 보유자산 통합 요약."""
    crypto = await crypto_status()
    us = await us_status()
    kr = await kr_positions()
    filters = await crypto_filters()

    return {
        "crypto": {
            "open_count": len(crypto["open_trades"]),
            "total_trades": crypto["total_trades"],
            "profit_krw": crypto["profit_total_krw"],
            "profit_pct": crypto["profit_total_pct"],
            "winrate": crypto["winrate"],
        },
        "us_stocks": {
            "alive": us["alive"],
            "decisions_today": len([d for d in us["decisions"] if d["ts"][:10] == time.strftime("%Y-%m-%d")]),
            "market_open": us["market_open"],
        },
        "kr_stocks": {
            "positions": len(kr.get("positions", [])),
        },
        "market": filters,
        "ts": time.time(),
    }


# ─── v4.0: 코인 분석 (Gemini Pro) ────────
@app.get("/api/analysis/movers")
async def analysis_movers(n: int = 10):
    """24h 상승률/하락률/거래량 TOP N."""
    try:
        from coin_analyzer import fetch_top_movers
        return fetch_top_movers(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/recommend")
async def analysis_recommend(n: int = 5):
    """AI 추천 코인 N개 (Gemini Pro, 시간 걸림)."""
    try:
        from coin_analyzer import recommend_coins
        return recommend_coins(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/coin/{symbol}")
async def analysis_coin(symbol: str):
    """특정 코인 심층 분석 (Gemini Pro, 30초+ 소요)."""
    try:
        from coin_analyzer import analyze_coin
        return analyze_coin(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── v5.2: 국내 주식 분석 ──────────────────
@app.get("/api/analysis/kr/movers")
async def analysis_kr_movers(n: int = 10):
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
async def analysis_kr_recommend(n: int = 5):
    try:
        from stock_analyzer import recommend_kr_stocks
        return recommend_kr_stocks(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/kr/{symbol}")
async def analysis_kr_stock(symbol: str):
    try:
        from stock_analyzer import analyze_kr_stock
        return analyze_kr_stock(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/search/kr_stocks")
async def search_kr_stocks_ep(q: str, limit: int = 8):
    """국내 주식 회사명/코드 검색."""
    try:
        from symbol_search import search_kr_stocks
        return {"query": q, "results": search_kr_stocks(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── v4.2: 미국 주식 분석 ────────────────
@app.get("/api/analysis/stocks/movers")
async def analysis_stocks_movers(n: int = 10):
    """미국 주식 24h 상승/하락/거래량 TOP N."""
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


@app.get("/api/analysis/stocks/recommend")
async def analysis_stocks_recommend(n: int = 5):
    """미국 주식 AI 추천 N개 (Gemini Pro)."""
    try:
        from stock_analyzer import recommend_stocks
        return recommend_stocks(n=n)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/analysis/stocks/{symbol}")
async def analysis_stock(symbol: str):
    """특정 미국 주식 심층 분석 (Gemini Pro)."""
    try:
        from stock_analyzer import analyze_stock
        return analyze_stock(symbol.upper())
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


# ─── v4.8: 비동기 분석 작업 (PWA 백그라운드 해결) ──
import uuid as _uuid
import threading as _threading
from fastapi import Request as _Request

_jobs: dict = {}  # {job_id: {status, market, symbol, started, completed, result, error}}
_result_cache: dict = {}  # {(market, symbol): {result, ts}}  — 5분 결과 캐시
_JOB_TTL = 3600  # 작업 결과 1시간 보관
_CACHE_TTL = 300  # 같은 종목 5분 캐시


def _cleanup_old_jobs():
    """오래된 작업/캐시 정리."""
    now = _time.time() if hasattr(__builtins__, '_time') else time.time()
    for jid in list(_jobs.keys()):
        if now - _jobs[jid].get("started", now) > _JOB_TTL:
            del _jobs[jid]
    for k in list(_result_cache.keys()):
        if now - _result_cache[k]["ts"] > _CACHE_TTL:
            del _result_cache[k]


def _run_job(job_id: str, market: str, symbol: str):
    """백그라운드 worker thread."""
    try:
        # 캐시 확인
        cache_key = (market, symbol)
        cached = _result_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < _CACHE_TTL:
            _jobs[job_id].update({
                "status": "completed",
                "result": cached["result"],
                "completed_at": time.time(),
                "from_cache": True,
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
            "status": "completed",
            "result": result,
            "completed_at": time.time(),
            "from_cache": False,
        })
    except Exception as e:
        _jobs[job_id].update({
            "status": "failed",
            "error": str(e)[:300],
            "completed_at": time.time(),
        })


@app.post("/api/analysis/job/start")
async def start_analysis_job(payload: dict):
    """비동기 분석 시작 — 즉시 job_id 반환, 백그라운드 실행."""
    market = payload.get("market", "crypto")
    symbol = payload.get("symbol", "").upper().strip()
    if not symbol or market not in ("crypto", "stocks", "kr"):  # v5.2: kr 추가
        return JSONResponse({"error": "invalid params"}, status_code=400)

    _cleanup_old_jobs()
    job_id = str(_uuid.uuid4())
    _jobs[job_id] = {
        "status": "pending",
        "market": market,
        "symbol": symbol,
        "started": time.time(),
    }
    _threading.Thread(target=_run_job, args=(job_id, market, symbol), daemon=True).start()
    return {"job_id": job_id, "status": "pending", "symbol": symbol, "market": market}


@app.get("/api/analysis/job/{job_id}")
async def get_analysis_job(job_id: str):
    """작업 상태/결과 polling."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    # 진행 중이면 elapsed 시간 추가
    elapsed = time.time() - job.get("started", time.time())
    return {**job, "elapsed_seconds": elapsed}


# ─── v5.0: 시장 지수/ETF/외환/원자재 위젯 (캐시 1분) ──
_indices_cache: dict = {}
_INDICES_CACHE_TTL = 60


@app.get("/api/analysis/indices")
async def indices_ep(cat: str = "us_index"):
    """v5.0.6: importlib.reload + 디버그 정보 노출."""
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


# ─── v4.5: 종목명 검색 (회사명 → 티커 자동 매칭) ──
@app.get("/api/search/coins")
async def search_coins_ep(q: str, limit: int = 8):
    """코인명/한국어/티커로 검색. 'BTC'/'비트코인'/'Bitcoin' 모두 가능."""
    try:
        from symbol_search import search_coins
        return {"query": q, "results": search_coins(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


@app.get("/api/search/stocks")
async def search_stocks_ep(q: str, limit: int = 8):
    """미국주식 회사명/한국어/티커로 검색. 'NVDA'/'엔비디아'/'Nvidia' 모두 가능."""
    try:
        from symbol_search import search_stocks
        return {"query": q, "results": search_stocks(q, limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("PWA_HOST", "0.0.0.0")
    port = int(os.environ.get("PWA_PORT", "8088"))
    uvicorn.run(app, host=host, port=port, log_level="info")
