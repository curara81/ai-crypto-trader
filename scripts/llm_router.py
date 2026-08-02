#!/usr/bin/env python3
"""LLM 라우터 — Gemini → Claude → Ollama 3단계 폴백 + 비용 추적.

v7.0: Ollama 로컬 모델 폴백 + CostTracker 통합.
Gemini 5회 연속 실패 → Claude → Claude도 실패 시 Ollama(qwen3:8b) 로컬.

사용:
    from llm_router import LLMRouter

    router = LLMRouter()
    response_json = router.call(prompt)
    # 내부적으로 Gemini → Claude → Ollama 폴백 처리
    print(router.cost_report())  # 비용 리포트
"""
import json
import logging
import os
import time
from typing import Optional

import requests

try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(key: str) -> Optional[str]:
        return os.environ.get(key)

# v3.8: Vertex AI Gemini support (GCP credits)
try:
    from google import genai as _vertex_genai
    _VERTEX_AVAILABLE = True
except ImportError:
    _VERTEX_AVAILABLE = False

# v7.0: Cost tracker
try:
    from cost_tracker import CostTracker
    _COST_TRACKER = CostTracker()
except ImportError:
    _COST_TRACKER = None

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

FAIL_THRESHOLD = 5     # Gemini 연속 실패 N회 후 Claude로 전환
RECOVERY_INTERVAL = 600  # Claude 모드에서 N초 후 Gemini 재시도


def _safe_json_parse(content: str) -> dict:
    """v5.1: JSON 파싱 + 다단계 repair.

    Gemini가 16k+ 응답에서 quote escape 누락, 응답 truncation 등으로
    invalid JSON 반환할 때 자동 복구 시도.
    """
    if not content:
        raise ValueError("empty response")

    # 1차: 그대로 시도
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        first_err = str(e)
        logger.warning(f"JSON parse 1차 실패 ({first_err}). repair 시도 (len={len(content)})")

    # 2차: 첫 { 부터 마지막 } 까지 substring
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        try:
            return json.loads(content[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    # 3차: 응답이 truncated일 때 — 균형 잡힌 마지막 } 찾기
    if first_brace >= 0:
        depth = 0
        in_string = False
        escape = False
        last_balanced = -1
        for i, ch in enumerate(content[first_brace:], start=first_brace):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    last_balanced = i
                    break
        if last_balanced > first_brace:
            try:
                return json.loads(content[first_brace:last_balanced + 1])
            except json.JSONDecodeError:
                pass

    # 4차: control character 제거 (Gemini가 가끔 \x00 등 삽입)
    import re
    sanitized = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", content)
    try:
        return json.loads(sanitized)
    except json.JSONDecodeError:
        pass

    # 모두 실패 — 마지막 200자 로깅
    logger.error(f"JSON repair 모두 실패. last 200 chars: ...{content[-200:]!r}")
    raise ValueError(f"AI 응답 형식 오류 (응답이 길이 {len(content)}자에서 잘림 또는 깨짐). 다시 시도해주세요.")


class LLMRouter:
    """다중 LLM 폴백 라우터.

    v3.8: Vertex AI Gemini (GCP 크레딧).
    v7.0: Ollama 로컬 폴백 + CostTracker 통합.
    v8.0: FREE_ONLY 모드가 기본. 과금 경로를 코드 레벨에서 차단.

    ── v8.0 배경 ──────────────────────────────────────────────
    GCP 무료 크레딧이 2026-07-30 소진·만료됐고, 그 뒤 Vertex 호출이
    신용카드로 직접 청구되어 ₩101,208이 실제로 빠져나갔다.
    구버전 폴백 체인(Gemini → Claude → Ollama)에는 두 가지 함정이 있었다:
      1. Vertex가 auto 기본값이라, "안 되면 결제 켜면 되지" 하는 순간
         크레딧 완충 없이 바로 카드로 청구된다.
      2. Gemini 무료 등급은 일일 한도(RPD)가 낮아 429가 상시 발생하는데,
         그때마다 유료 Claude API로 폴백된다 — 즉 과금이 예외가 아니라
         정상 동작이 되어버린다.

    그래서 v8.0은 기본 체인을 뒤집었다:
        Ollama(로컬, 무조건 무료) → [선택] Gemini 무료등급
    유료 경로(Vertex, Claude API)는 LLM_ALLOW_PAID=1 을 명시해야만 열린다.
    """

    def __init__(self):
        # v8.0: 무료 전용 모드 (기본 ON). 유료 경로를 열려면 LLM_ALLOW_PAID=1
        self._free_only = os.environ.get("LLM_ALLOW_PAID", "0") != "1"

        self._gemini_key = _get_secret("GEMINI_API_KEY") or ""
        self._claude_key = _get_secret("ANTHROPIC_API_KEY") or ""
        self._gemini_fail_count = 0
        self._claude_mode_since: Optional[float] = None

        # v8.0: 무료 모드에서는 Claude 유료 API 경로를 아예 비활성화
        if self._free_only and self._claude_key:
            logger.info("FREE_ONLY: Claude 유료 API 경로 비활성화 (과금 방지)")
            self._claude_key = ""

        # v3.8: Vertex AI 설정
        self._gcp_project = os.environ.get("GCP_PROJECT", "timesfm-personal-lab")
        self._gcp_location = os.environ.get("GCP_LOCATION", "us-central1")
        provider_env = os.environ.get("GEMINI_PROVIDER", "auto").lower()
        # v8.0: Vertex는 결제 계정이 있어야만 동작 = 무료 모드에서 금지.
        #       auto가 Vertex를 고르던 기존 동작이 카드 청구의 직접 원인이었다.
        if self._free_only:
            self._provider = "direct"
        elif provider_env == "vertex" or (provider_env == "auto" and _VERTEX_AVAILABLE):
            self._provider = "vertex"
        else:
            self._provider = "direct"
        self._vertex_client = None
        if self._provider == "vertex" and _VERTEX_AVAILABLE:
            try:
                self._vertex_client = _vertex_genai.Client(
                    vertexai=True,
                    project=self._gcp_project,
                    location=self._gcp_location,
                )
                logger.info(f"LLMRouter: Vertex AI Gemini ({self._gcp_project}/{self._gcp_location})")
            except Exception as e:
                logger.warning(f"Vertex AI 초기화 실패, Direct API로 폴백: {e}")
                self._provider = "direct"

        # v7.0: Ollama 가용성 체크
        self._ollama_available = self._check_ollama()

        if self._free_only:
            # 무료 모드: Ollama가 1순위. Gemini는 키가 있을 때만 보조로 사용하며,
            # 결제 계정이 연결되지 않은 프로젝트의 키여야 무료 등급이 보장된다.
            if not self._ollama_available:
                logger.error(
                    "FREE_ONLY인데 Ollama가 응답하지 않음. "
                    "`ollama serve` 확인 필요 — LLM 판단 불가."
                )
            else:
                logger.info(f"LLMRouter[FREE_ONLY]: Ollama 우선 ({OLLAMA_MODEL})")
            if self._gemini_key:
                logger.info("LLMRouter[FREE_ONLY]: Gemini 무료등급 보조 활성")
        else:
            logger.warning("LLMRouter: 유료 경로 허용됨 (LLM_ALLOW_PAID=1) — 과금 발생 가능")
            if self._provider == "direct":
                logger.info("LLMRouter: Direct Gemini API")
            if self._ollama_available:
                logger.info(f"LLMRouter: Ollama 로컬 폴백 활성 ({OLLAMA_MODEL})")

    def _should_use_claude(self) -> bool:
        if not self._claude_key:
            return False
        if self._claude_mode_since is None:
            return False
        # 일정 시간 경과 시 Gemini 재시도
        if time.time() - self._claude_mode_since > RECOVERY_INTERVAL:
            logger.info("Claude 모드 만료 — Gemini 재시도")
            self._claude_mode_since = None
            self._gemini_fail_count = 0
            return False
        return True

    def _call_gemini_vertex(self, prompt: str, timeout: int = 60,
                            model: str = "gemini-2.5-flash") -> dict:
        """v3.8: Vertex AI Gemini (GCP 크레딧 사용).

        v4.0: model 인자 지원.
        v5.1: max_output_tokens 32768 (Pro의 bilingual + 4섹션 응답 잘림 방지) + JSON repair.
        """
        if not self._vertex_client:
            raise RuntimeError("Vertex AI client 미초기화")
        # v5.1: Pro 모델은 더 큰 토큰 (16k+ 응답 가능)
        max_tokens = 32768 if "pro" in model.lower() else 16384
        response = self._vertex_client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "max_output_tokens": max_tokens,
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        )
        content = response.text.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        usage = {}
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            u = response.usage_metadata
            usage = {
                "promptTokenCount": getattr(u, "prompt_token_count", 0),
                "candidatesTokenCount": getattr(u, "candidates_token_count", 0),
                "thoughtsTokenCount": getattr(u, "thoughts_token_count", 0) or 0,
            }
        return {
            "json": _safe_json_parse(content),  # v5.1: JSON repair
            "usage": usage,
            "provider": "gemini-vertex",
        }

    def _call_gemini_direct(self, prompt: str, timeout: int = 60) -> dict:
        """기존 paid API 호출."""
        if not self._gemini_key:
            raise RuntimeError("GEMINI_API_KEY missing")
        resp = requests.post(
            f"{GEMINI_URL}?key={self._gemini_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 8192,
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return {
            "json": _safe_json_parse(content),  # v5.1
            "usage": data.get("usageMetadata", {}),
            "provider": "gemini-direct",
        }

    def _call_gemini(self, prompt: str, timeout: int = 60,
                     model: str = "gemini-2.5-flash") -> dict:
        """프로바이더 자동 선택. v4.0: model 인자 추가."""
        if self._provider == "vertex":
            return self._call_gemini_vertex(prompt, timeout, model=model)
        return self._call_gemini_direct(prompt, timeout)

    def _check_ollama(self) -> bool:
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=3)
            models = [m["name"] for m in resp.json().get("models", [])]
            return any(OLLAMA_MODEL.split(":")[0] in m for m in models)
        except Exception:
            return False

    def _call_ollama(self, prompt: str, timeout: int = 120,
                     schema: Optional[dict] = None) -> dict:
        """v7.0: Ollama 로컬 모델 호출 (무료, 오프라인 가능).

        v8.0: schema를 넘기면 Ollama가 JSON Schema에 맞는 출력을 강제한다
        (format 파라미터). 마크다운 펜스 파싱이 필요 없어져 더 안정적.
        """
        # v8.0: keep_alive는 이 모델이 주경로인지에 따라 갈린다.
        #   - 폴백 전용(유료 모드): 0 = 즉시 언로드해 RAM 5GB를 돌려준다.
        #   - 주경로(FREE_ONLY): 언로드하면 호출마다 콜드스타트가 붙는다.
        #     실측 21.9초(콜드) vs 4.6초(웜) — 5분 사이클에서 상주가 이득.
        keep_alive = "30m" if self._free_only else 0
        t0 = time.time()
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": f"/no_think\nRespond with ONLY valid JSON, no markdown.\n\n{prompt}",
                "stream": False,
                "format": schema if schema else "json",
                "keep_alive": keep_alive,
                "options": {"temperature": 0.2, "num_predict": 4096},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("response", "").strip()
        # Strip thinking tags if present
        if "<think>" in content:
            think_end = content.find("</think>")
            if think_end >= 0:
                content = content[think_end + 8:].strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        latency = int((time.time() - t0) * 1000)
        usage = {
            "prompt_eval_count": data.get("prompt_eval_count", 0),
            "eval_count": data.get("eval_count", 0),
        }
        if _COST_TRACKER:
            _COST_TRACKER.record(
                provider="ollama-local", model=OLLAMA_MODEL,
                input_tokens=usage["prompt_eval_count"],
                output_tokens=usage["eval_count"],
                latency_ms=latency,
            )
        return {
            "json": _safe_json_parse(content),
            "usage": usage,
            "provider": "ollama-local",
        }

    def _track_cost(self, provider: str, model: str, usage: dict) -> None:
        """v7.0: 비용 추적."""
        if not _COST_TRACKER:
            return
        _COST_TRACKER.record(
            provider=provider, model=model,
            input_tokens=usage.get("promptTokenCount", usage.get("prompt_token_count",
                          usage.get("input_tokens", 0))),
            output_tokens=usage.get("candidatesTokenCount", usage.get("candidates_token_count",
                           usage.get("output_tokens", 0))),
            thinking_tokens=usage.get("thoughtsTokenCount", usage.get("thoughts_token_count", 0)),
        )

    def cost_report(self) -> str:
        if _COST_TRACKER:
            return _COST_TRACKER.daily_report()
        return "CostTracker 미활성"

    def _call_claude(self, prompt: str, timeout: int = 60) -> dict:
        if not self._claude_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing")

        resp = requests.post(
            CLAUDE_URL,
            headers={
                "x-api-key": self._claude_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
                "system": "Respond with only valid JSON, no markdown.",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return {
            "json": _safe_json_parse(text),  # v5.1
            "usage": data.get("usage", {}),
            "provider": "claude",
        }

    def call(self, prompt: str, timeout: int = 60,
             model: str = "gemini-2.5-flash") -> dict:
        """LLM 호출.

        v4.0: model 인자.
        v7.0: Ollama 최종 폴백 + 비용 추적.
        v8.0: FREE_ONLY 모드에서 체인이 Ollama → Gemini(무료등급)로 뒤집힘.

        Returns: {"json": dict, "usage": dict, "provider": "..."}
        Raises: RuntimeError if all providers fail
        """
        # ── v8.0 무료 전용 경로 ──────────────────────────────
        # Ollama를 1순위로 둔다. 로컬이라 호출량 제한도 과금도 없고,
        # Gemini 무료등급의 일일 한도(RPD)를 아낄 수 있다.
        if self._free_only:
            if self._ollama_available:
                try:
                    return self._call_ollama(prompt, timeout=max(timeout, 120))
                except Exception as e:
                    logger.warning(f"Ollama 실패: {e}")
            # Gemini 무료등급 보조. 한도 초과 시 429가 오고 과금은 되지 않는다.
            if self._gemini_key:
                try:
                    result = self._call_gemini_direct(prompt, timeout)
                    self._track_cost(result["provider"], model, result.get("usage", {}))
                    return result
                except Exception as e:
                    raise RuntimeError(
                        f"무료 경로 모두 실패 — Ollama 불가, Gemini 무료등급도 실패: {e}"
                    ) from e
            raise RuntimeError(
                "무료 경로 없음 — Ollama가 응답하지 않고 GEMINI_API_KEY도 없음"
            )

        if self._should_use_claude():
            try:
                result = self._call_claude(prompt, timeout)
                self._track_cost("claude", CLAUDE_MODEL, result.get("usage", {}))
                return result
            except Exception as e:
                logger.warning(f"Claude도 실패, Gemini 재시도: {e}")
                self._claude_mode_since = None

        # Gemini 시도
        try:
            result = self._call_gemini(prompt, timeout, model=model)
            self._gemini_fail_count = 0
            self._track_cost(result["provider"], model, result.get("usage", {}))
            return result
        except Exception as e:
            self._gemini_fail_count += 1
            logger.warning(f"Gemini 실패 ({self._gemini_fail_count}/{FAIL_THRESHOLD}): {e}")

            # 임계 도달 시 Claude로 전환
            if self._gemini_fail_count >= FAIL_THRESHOLD and self._claude_key:
                logger.error(f"Gemini {FAIL_THRESHOLD}회 연속 실패 → Claude 폴백")
                self._claude_mode_since = time.time()
                try:
                    result = self._call_claude(prompt, timeout)
                    self._track_cost("claude", CLAUDE_MODEL, result.get("usage", {}))
                    return result
                except Exception as ce:
                    logger.error(f"Claude도 실패: {ce}")
                    # v7.0: Ollama 최종 폴백
                    if self._ollama_available:
                        logger.warning("Gemini+Claude 모두 실패 → Ollama 로컬 폴백")
                        try:
                            return self._call_ollama(prompt, timeout=120)
                        except Exception as oe:
                            raise RuntimeError(
                                f"All LLMs failed — Gemini: {e}, Claude: {ce}, Ollama: {oe}"
                            ) from oe
                    raise RuntimeError(f"Both LLMs failed — Gemini: {e}, Claude: {ce}") from ce

            # 단발 실패 시에도 Ollama 폴백 시도
            if self._ollama_available and self._gemini_fail_count >= 2:
                try:
                    logger.info("Gemini 연속 실패 — Ollama 임시 폴백")
                    return self._call_ollama(prompt, timeout=120)
                except Exception:
                    pass

            raise
