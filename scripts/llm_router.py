#!/usr/bin/env python3
"""LLM 라우터 — Gemini 우선, 실패 시 Claude로 자동 폴백.

Gemini 2.5 Flash가 5회 연속 실패하면 자동으로 Claude Sonnet 4.6으로 전환.
정상화되면 다시 Gemini로 복귀.

사용:
    from llm_router import LLMRouter

    router = LLMRouter()
    response_json = router.call(prompt)
    # 내부적으로 Gemini → Claude 폴백 처리
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

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"

FAIL_THRESHOLD = 5     # Gemini 연속 실패 N회 후 Claude로 전환
RECOVERY_INTERVAL = 600  # Claude 모드에서 N초 후 Gemini 재시도


class LLMRouter:
    """다중 LLM 폴백 라우터."""

    def __init__(self):
        self._gemini_key = _get_secret("GEMINI_API_KEY") or ""
        self._claude_key = _get_secret("ANTHROPIC_API_KEY") or ""
        self._gemini_fail_count = 0
        self._claude_mode_since: Optional[float] = None  # Claude 모드 진입 시각

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

    def _call_gemini(self, prompt: str, timeout: int = 60) -> dict:
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
            "json": json.loads(content),
            "usage": data.get("usageMetadata", {}),
            "provider": "gemini",
        }

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
            "json": json.loads(text),
            "usage": data.get("usage", {}),
            "provider": "claude",
        }

    def call(self, prompt: str, timeout: int = 60) -> dict:
        """LLM 호출 — Gemini 우선, 자동 폴백.

        Returns: {"json": dict, "usage": dict, "provider": "gemini"|"claude"}
        Raises: RuntimeError if both providers fail
        """
        if self._should_use_claude():
            try:
                return self._call_claude(prompt, timeout)
            except Exception as e:
                logger.warning(f"Claude도 실패, Gemini 재시도: {e}")
                self._claude_mode_since = None  # Claude도 안되면 Gemini로 강제 회귀

        # Gemini 시도
        try:
            result = self._call_gemini(prompt, timeout)
            self._gemini_fail_count = 0
            return result
        except Exception as e:
            self._gemini_fail_count += 1
            logger.warning(f"Gemini 실패 ({self._gemini_fail_count}/{FAIL_THRESHOLD}): {e}")

            # 임계 도달 시 Claude로 전환
            if self._gemini_fail_count >= FAIL_THRESHOLD and self._claude_key:
                logger.error(f"Gemini {FAIL_THRESHOLD}회 연속 실패 → Claude 폴백")
                self._claude_mode_since = time.time()
                try:
                    return self._call_claude(prompt, timeout)
                except Exception as ce:
                    raise RuntimeError(f"Both LLMs failed — Gemini: {e}, Claude: {ce}") from ce

            raise
