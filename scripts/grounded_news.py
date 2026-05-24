#!/usr/bin/env python3
"""v4.3: Vertex AI Grounding with Google Search.

Gemini에 Google Search 도구를 붙여 실시간 정보 검색.
JSON 출력과 함께 쓰기 어려우므로, 분석 시작 전 별도 호출로
"최신 사건 요약 + 출처 URL"을 받아와서 분석 프롬프트에 주입.

장점:
  - GCP 크레딧 사용 (추가 비용 ~$0.035/grounded request)
  - 출처 URL 자동 첨부 (citation)
  - 학습 cutoff 이후 사건도 답변 가능
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai.types import Tool, GoogleSearch, GenerateContentConfig
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

_CLIENT: Optional["genai.Client"] = None


def _get_client():
    global _CLIENT
    if _CLIENT is None and _AVAILABLE:
        project = os.environ.get("GCP_PROJECT", "timesfm-personal-lab")
        location = os.environ.get("GCP_LOCATION", "us-central1")
        _CLIENT = genai.Client(vertexai=True, project=project, location=location)
    return _CLIENT


def fetch_grounded_news(query: str, max_chars: int = 1500) -> dict:
    """Google Search grounding으로 최신 정보 + 출처 가져오기.

    Returns:
        {
            "text": "Gemini의 검색 기반 요약 (max_chars 제한)",
            "sources": [{"title": "...", "uri": "...", "domain": "..."}],
            "query": query,
            "error": Optional[str],
        }
    """
    if not _AVAILABLE:
        return {"text": "", "sources": [], "query": query, "error": "google-genai 미설치"}

    client = _get_client()
    if not client:
        return {"text": "", "sources": [], "query": query, "error": "Vertex AI client 미초기화"}

    try:
        config = GenerateContentConfig(
            tools=[Tool(google_search=GoogleSearch())],
            temperature=0.1,
            max_output_tokens=1500,
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",  # 속도 우선
            contents=query,
            config=config,
        )

        text = (response.text or "")[:max_chars]

        sources = []
        if response.candidates and response.candidates[0].grounding_metadata:
            chunks = response.candidates[0].grounding_metadata.grounding_chunks or []
            seen_uris = set()
            for c in chunks:
                if c.web and c.web.uri and c.web.uri not in seen_uris:
                    seen_uris.add(c.web.uri)
                    # vertexaisearch URL → 도메인 추출 어려움, title 사용
                    title = c.web.title or "?"
                    sources.append({
                        "title": title[:80],
                        "uri": c.web.uri,
                        "domain": title.split("/")[0] if "/" in title else title[:30],
                    })

        logger.info(f"Grounded news '{query[:40]}...': {len(text)}자, {len(sources)}개 출처")
        return {
            "text": text,
            "sources": sources[:10],
            "query": query,
            "error": None,
        }
    except Exception as e:
        logger.warning(f"Grounded news 실패: {e}")
        return {"text": "", "sources": [], "query": query, "error": str(e)[:200]}
