#!/usr/bin/env python3
"""멀티에이전트 트레이딩 시스템 — CrewAI 기반.

3명의 전문 에이전트가 독립적으로 분석 후 투표로 최종 판단:
  1. 기술분석 에이전트 (Technical Analyst)
  2. 뉴스감성 에이전트 (Sentiment Analyst)
  3. 리스크관리 에이전트 (Risk Manager)

LLMRouter를 백엔드로 사용하여 Gemini/Claude/Ollama 자동 폴백.

사용:
    crew = TradingCrew()
    decision = crew.analyze("NVDA", market_data, news, technicals)
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from crewai import Agent, Task, Crew, Process, LLM
    _CREWAI_OK = True
except ImportError:
    _CREWAI_OK = False
    logger.warning("crewai 미설치 — 멀티에이전트 비활성")

try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(key: str) -> Optional[str]:
        return os.environ.get(key)


def _build_crew_llm() -> Optional[object]:
    """CrewAI용 LLM 객체 생성.

    v8.0: 기본은 무료 전용. 멀티에이전트 패널은 심볼 하나당 LLM을 여러 번
    호출하므로, 유료 경로가 열려 있으면 여기가 가장 비싼 지점이 된다.
    llm_router와 동일하게 LLM_ALLOW_PAID=1 이 있어야만 유료 모델을 쓴다.
    """
    if not _CREWAI_OK:
        return None

    free_only = os.environ.get("LLM_ALLOW_PAID", "0") != "1"
    ollama_model = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

    if free_only:
        return LLM(model=f"ollama/{ollama_model}",
                   base_url="http://localhost:11434", temperature=0.2)

    gemini_key = _get_secret("GEMINI_API_KEY")
    if gemini_key:
        return LLM(
            model="gemini/gemini-2.5-flash",
            api_key=gemini_key,
            temperature=0.2,
        )
    anthropic_key = _get_secret("ANTHROPIC_API_KEY")
    if anthropic_key:
        return LLM(
            model="anthropic/claude-sonnet-4-6",
            api_key=anthropic_key,
            temperature=0.2,
        )
    return LLM(model=f"ollama/{ollama_model}", base_url="http://localhost:11434")


class TradingCrew:

    def __init__(self):
        self._ready = False
        if not _CREWAI_OK:
            return

        self._llm = _build_crew_llm()
        if not self._llm:
            logger.warning("TradingCrew: LLM 설정 실패")
            return

        self._tech_agent = Agent(
            role="Technical Analyst",
            goal="차트 패턴, 기술지표, 시장 구조를 분석하여 매매 신호를 도출한다.",
            backstory=(
                "20년 경력의 기술적 분석 전문가. EMA, RSI, MACD, 볼린저밴드, "
                "래리윌리엄스 변동성돌파, 터틀트레이딩, BNF 평균회귀 등 "
                "다양한 전략에 정통하다. 시장 레짐(상승/횡보/하락)에 따라 "
                "적합한 전략을 선택하는 능력이 뛰어나다."
            ),
            llm=self._llm,
            verbose=False,
        )

        self._sentiment_agent = Agent(
            role="Sentiment Analyst",
            goal="뉴스, 시장 심리, 펀더멘털 요소를 분석하여 매매에 영향을 주는 외부 요인을 평가한다.",
            backstory=(
                "금융 뉴스 분석과 시장 심리 판독 전문가. "
                "뉴스 헤드라인에서 실제 주가 영향력을 정확히 판단하고, "
                "시장의 공포/탐욕 지수, 섹터 로테이션, 매크로 환경을 종합 분석한다. "
                "노이즈와 신호를 구별하는 데 탁월하다."
            ),
            llm=self._llm,
            verbose=False,
        )

        self._risk_agent = Agent(
            role="Risk Manager",
            goal="포트폴리오 리스크를 평가하고, 포지션 사이징과 손절/익절 수준을 결정한다.",
            backstory=(
                "헤지펀드 출신 리스크 관리자. 최소 1.5:1 보상비율을 엄격히 준수하고, "
                "일일 손실 한도, 연속 손실 방지, 포지션 집중도를 관리한다. "
                "과거 판단 이력에서 패턴을 분석하여 반복 실수를 방지한다. "
                "모의투자라도 실전 마인드로 리스크를 관리한다."
            ),
            llm=self._llm,
            verbose=False,
        )
        self._ready = True
        logger.info("TradingCrew 초기화 완료 (3 agents)")

    @property
    def is_ready(self) -> bool:
        return self._ready

    def analyze(self, symbol: str, market_data: str, news: str,
                technicals: str, past_decisions: str = "",
                portfolio: str = "", rag_context: str = "") -> Optional[dict]:
        if not self._ready:
            return None

        combined_context = (
            f"종목: {symbol}\n\n"
            f"## 시장 데이터\n{market_data}\n\n"
            f"## 기술지표\n{technicals}\n\n"
            f"## 뉴스\n{news}\n\n"
        )
        if rag_context:
            combined_context += f"## RAG 컨텍스트 (벡터 검색)\n{rag_context}\n\n"
        if past_decisions:
            combined_context += f"## 과거 판단 이력\n{past_decisions}\n\n"
        if portfolio:
            combined_context += f"## 포트폴리오\n{portfolio}\n\n"

        json_format = '{"action":"buy/sell/hold","confidence":0.0-1.0,"reason":"분석 근거"}'

        tech_task = Task(
            description=(
                f"다음 시장 데이터를 기술적으로 분석하고 매매 판단을 내려라.\n\n"
                f"{combined_context}\n\n"
                f"반드시 JSON으로만 응답: {json_format}"
            ),
            expected_output=f"JSON: {json_format}",
            agent=self._tech_agent,
        )

        sentiment_task = Task(
            description=(
                f"다음 뉴스와 시장 심리를 분석하고 매매 판단을 내려라.\n\n"
                f"{combined_context}\n\n"
                f"반드시 JSON으로만 응답: {json_format}"
            ),
            expected_output=f"JSON: {json_format}",
            agent=self._sentiment_agent,
        )

        risk_task = Task(
            description=(
                f"기술분석가와 감성분석가의 판단을 종합하고, "
                f"리스크 관점에서 최종 매매 판단을 내려라.\n\n"
                f"{combined_context}\n\n"
                f"포지션 사이징(stake_multiplier 0.5~2.0), 손절가, 목표가를 포함하라.\n"
                f'JSON: {{"action":"buy/sell/hold","confidence":0.0-1.0,'
                f'"reason":"종합 분석","risk_level":"low/medium/high",'
                f'"price_target":"$X","stop_loss":"$X","stake_multiplier":1.0}}'
            ),
            expected_output="Final trading decision as JSON",
            agent=self._risk_agent,
            context=[tech_task, sentiment_task],
        )

        try:
            crew = Crew(
                agents=[self._tech_agent, self._sentiment_agent, self._risk_agent],
                tasks=[tech_task, sentiment_task, risk_task],
                process=Process.sequential,
                verbose=False,
            )
            result = crew.kickoff()
            raw = str(result)

            first_brace = raw.find("{")
            last_brace = raw.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                decision = json.loads(raw[first_brace:last_brace + 1])
            else:
                decision = json.loads(raw)

            decision.setdefault("action", "hold")
            decision.setdefault("confidence", 0.5)
            decision.setdefault("reason", "multi-agent consensus")
            decision.setdefault("risk_level", "medium")
            decision.setdefault("price_target", "N/A")
            decision.setdefault("stop_loss", "N/A")
            decision.setdefault("stake_multiplier", 1.0)
            decision["provider"] = "crewai-multi-agent"

            logger.info(
                f"CrewAI {symbol}: {decision['action']} "
                f"(conf={decision['confidence']:.2f}) {decision['reason'][:60]}"
            )
            return decision

        except Exception as e:
            logger.error(f"CrewAI 분석 실패 ({symbol}): {e}")
            return None


class QuickCrew:
    """경량 멀티에이전트 — API 호출 1회로 3관점 분석.

    CrewAI 풀 버전 대비 속도 3배, 비용 1/3.
    단일 LLM 호출에서 3명의 전문가 역할극을 수행.
    """

    def __init__(self):
        self._ready = False
        try:
            from llm_router import LLMRouter
            self._router = LLMRouter()
            self._ready = True
        except Exception as e:
            logger.warning(f"QuickCrew LLMRouter 실패: {e}")

    @property
    def is_ready(self) -> bool:
        return self._ready

    def analyze(self, symbol: str, market_data: str, news: str,
                technicals: str, past_decisions: str = "",
                portfolio: str = "", rag_context: str = "") -> Optional[dict]:
        if not self._ready:
            return None

        prompt = f"""You are three expert traders analyzing {symbol}. Each gives an independent opinion, then you synthesize a final decision.

## Market Data
{market_data}

## Technical Indicators
{technicals}

## News
{news}

{f"## RAG Context{chr(10)}{rag_context}" if rag_context else ""}
{f"## Past Decisions{chr(10)}{past_decisions}" if past_decisions else ""}
{f"## Portfolio{chr(10)}{portfolio}" if portfolio else ""}

## Expert Panel:
1. **Technical Analyst**: Focus on chart patterns, EMA/RSI/MACD signals, market regime.
2. **Sentiment Analyst**: Focus on news impact, market psychology, sector trends.
3. **Risk Manager**: Evaluate risk/reward, position sizing, stop-loss levels.

Respond with ONLY this JSON (no extra text):
{{
  "tech_opinion": {{"action": "buy/sell/hold", "confidence": 0.0-1.0, "reasoning": "..."}},
  "sentiment_opinion": {{"action": "buy/sell/hold", "confidence": 0.0-1.0, "reasoning": "..."}},
  "risk_opinion": {{"action": "buy/sell/hold", "confidence": 0.0-1.0, "reasoning": "..."}},
  "final": {{
    "action": "buy/sell/hold",
    "confidence": 0.0-1.0,
    "reason": "30-word synthesis of all three opinions",
    "risk_level": "low/medium/high",
    "price_target": "$X or N/A",
    "stop_loss": "$X or N/A",
    "stake_multiplier": 0.5-2.0
  }}
}}"""

        try:
            result = self._router.call(prompt, timeout=90)
            data = result["json"]
            final = data.get("final", data)
            final.setdefault("action", "hold")
            final.setdefault("confidence", 0.5)
            final["provider"] = f"quick-crew-{result.get('provider', 'unknown')}"

            opinions = []
            for role in ["tech_opinion", "sentiment_opinion", "risk_opinion"]:
                op = data.get(role, {})
                if op:
                    opinions.append(f"{role.split('_')[0]}={op.get('action','?')}"
                                    f"({op.get('confidence',0):.0%})")
            if opinions:
                final["panel"] = " | ".join(opinions)

            logger.info(
                f"QuickCrew {symbol}: {final['action']} "
                f"(conf={final['confidence']:.2f}) [{final.get('panel','')}]"
            )
            return final
        except Exception as e:
            logger.error(f"QuickCrew 분석 실패 ({symbol}): {e}")
            return None
