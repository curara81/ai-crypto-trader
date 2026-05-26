#!/usr/bin/env python3
"""뉴스 RAG 파이프라인 — 벡터DB 기반 뉴스/판단 이력 검색.

Tavily 뉴스를 ChromaDB에 임베딩하여 저장하고,
매매 판단 시 관련 뉴스를 시맨틱 검색으로 조회한다.

과거 판단 이력도 벡터화하여 유사 상황 패턴 매칭에 활용.

사용:
    rag = NewsRAG()
    rag.ingest_news("NVDA", [{"title": "...", "content": "..."}])
    context = rag.query("NVDA recent earnings and GPU demand outlook")
"""
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
CHROMA_DIR = os.path.join(TRADING_ROOT, "data/chromadb")

try:
    import chromadb
    from chromadb.config import Settings
    _CHROMA_OK = True
except ImportError:
    _CHROMA_OK = False
    logger.warning("chromadb 미설치 — RAG 비활성")

try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except ImportError:
    _ST_OK = False
    logger.warning("sentence-transformers 미설치 — 임베딩 불가")

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(key: str) -> Optional[str]:
        return os.environ.get(key)


class NewsRAG:
    EMBED_MODEL = "all-MiniLM-L6-v2"
    NEWS_COLLECTION = "trading_news"
    DECISIONS_COLLECTION = "trading_decisions"
    MAX_NEWS_AGE_DAYS = 7

    def __init__(self, persist_dir: str = CHROMA_DIR):
        self._ready = False
        if not (_CHROMA_OK and _ST_OK):
            logger.warning("RAG 의존성 불완전 — 비활성 모드")
            return

        os.makedirs(persist_dir, exist_ok=True)
        try:
            self._embedder = SentenceTransformer(self.EMBED_MODEL)
            self._client = chromadb.PersistentClient(path=persist_dir)
            self._news_col = self._client.get_or_create_collection(
                name=self.NEWS_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            self._decisions_col = self._client.get_or_create_collection(
                name=self.DECISIONS_COLLECTION,
                metadata={"hnsw:space": "cosine"},
            )
            self._ready = True
            logger.info(f"NewsRAG 초기화 완료 (news={self._news_col.count()}, decisions={self._decisions_col.count()})")
        except Exception as e:
            logger.error(f"NewsRAG 초기화 실패: {e}")

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._embedder.encode(texts).tolist()

    def ingest_news(self, symbol: str, articles: list[dict]) -> int:
        if not self._ready or not articles:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        ids, docs, metas = [], [], []
        for i, article in enumerate(articles):
            title = article.get("title", "")
            content = article.get("content", article.get("snippet", ""))
            text = f"{title}. {content}".strip()
            if not text or len(text) < 10:
                continue
            doc_id = f"{symbol}_{int(time.time())}_{i}"
            ids.append(doc_id)
            docs.append(text)
            metas.append({
                "symbol": symbol,
                "title": title[:200],
                "source": article.get("url", ""),
                "ingested_at": now,
            })

        if not ids:
            return 0

        embeddings = self._embed(docs)
        self._news_col.add(
            ids=ids,
            embeddings=embeddings,
            documents=docs,
            metadatas=metas,
        )
        logger.info(f"RAG: {symbol} 뉴스 {len(ids)}건 저장 (총 {self._news_col.count()})")
        return len(ids)

    def ingest_decision(self, symbol: str, decision: dict, price: float,
                        technicals_summary: str = "") -> None:
        if not self._ready:
            return

        action = decision.get("action", "hold")
        confidence = decision.get("confidence", 0)
        reason = decision.get("reason", "")
        text = (
            f"{symbol} {action} (confidence={confidence:.2f}) at ${price:.2f}. "
            f"{reason}. {technicals_summary}"
        )
        doc_id = f"{symbol}_{int(time.time())}_dec"
        embedding = self._embed([text])

        self._decisions_col.add(
            ids=[doc_id],
            embeddings=embedding,
            documents=[text],
            metadatas=[{
                "symbol": symbol,
                "action": action,
                "confidence": confidence,
                "price": price,
                "ts": datetime.now(timezone.utc).isoformat(),
            }],
        )

    def query_news(self, symbol: str, query: str, n_results: int = 5) -> str:
        if not self._ready:
            return ""

        embedding = self._embed([query])
        results = self._news_col.query(
            query_embeddings=embedding,
            n_results=n_results,
            where={"symbol": symbol},
        )

        if not results["documents"] or not results["documents"][0]:
            return ""

        lines = ["## RAG: 관련 뉴스 컨텍스트 (벡터 검색)"]
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            title = meta.get("title", "")
            lines.append(f"- {title}: {doc[:150]}")
        return "\n".join(lines)

    def query_similar_decisions(self, symbol: str, current_situation: str,
                                 n_results: int = 3) -> str:
        if not self._ready or self._decisions_col.count() == 0:
            return ""

        embedding = self._embed([current_situation])
        try:
            results = self._decisions_col.query(
                query_embeddings=embedding,
                n_results=n_results,
                where={"symbol": symbol},
            )
        except Exception:
            results = self._decisions_col.query(
                query_embeddings=embedding,
                n_results=n_results,
            )

        if not results["documents"] or not results["documents"][0]:
            return ""

        lines = ["## RAG: 유사 과거 판단 (패턴 매칭)"]
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            action = meta.get("action", "?")
            conf = meta.get("confidence", 0)
            ts = meta.get("ts", "?")[:10]
            lines.append(f"- [{ts}] {action}(conf={conf:.2f}): {doc[:120]}")
        return "\n".join(lines)

    def fetch_and_ingest(self, symbol: str, stock_name: str,
                         max_results: int = 5) -> tuple[str, str]:
        """Tavily 뉴스 fetch + RAG 저장 + 검색 결과 반환.
        Returns: (plain_news_text, rag_context)
        """
        tavily_key = _get_secret("TAVILY_API_KEY") if _get_secret else None
        plain_news = "No news available."
        articles = []

        if tavily_key and _requests:
            try:
                resp = _requests.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_key,
                        "query": f"{stock_name} {symbol} stock price analysis today",
                        "max_results": max_results,
                        "search_depth": "basic",
                        "include_raw_content": False,
                    },
                    timeout=15,
                )
                raw_articles = resp.json().get("results", [])
                if raw_articles:
                    articles = [
                        {"title": a.get("title", ""), "content": a.get("content", ""),
                         "url": a.get("url", "")}
                        for a in raw_articles
                    ]
                    plain_news = "\n".join(f"- {a['title']}" for a in articles[:max_results])
                    self.ingest_news(symbol, articles)
            except Exception as e:
                logger.warning(f"뉴스 수집 실패 ({symbol}): {e}")

        rag_context = ""
        if self._ready:
            rag_context = self.query_news(
                symbol, f"{stock_name} {symbol} latest market outlook momentum"
            )
            similar = self.query_similar_decisions(
                symbol, f"{stock_name} current price action and technical setup"
            )
            if similar:
                rag_context = f"{rag_context}\n\n{similar}" if rag_context else similar

        return plain_news, rag_context

    def cleanup_old(self, max_age_days: int = 7) -> int:
        if not self._ready:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        try:
            old = self._news_col.get(where={"ingested_at": {"$lt": cutoff}})
            if old["ids"]:
                self._news_col.delete(ids=old["ids"])
                logger.info(f"RAG: {len(old['ids'])}건 오래된 뉴스 삭제")
                return len(old["ids"])
        except Exception as e:
            logger.warning(f"RAG cleanup 실패: {e}")
        return 0
