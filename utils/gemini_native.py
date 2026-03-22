"""Native Gemini REST client for Google Search grounding.

Used only when MODEL_PROVIDER=gemini_aistudio and GEMINI_SEARCH_GROUNDING=True.
The OpenAI-compatible Gemini endpoint does not support search grounding tools;
this client calls the native generateContent endpoint directly.

Returns a duck-typed response object with a .choices[0].message.content interface
so callers don't need to change their response-parsing logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT = 120.0


@dataclass
class _Message:
    content: str
    role: str = "assistant"


@dataclass
class _Choice:
    message: _Message
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class GeminiNativeResponse:
    """Duck-typed response that matches the openai ChatCompletion interface."""

    choices: list[_Choice]
    usage: _Usage = field(default_factory=_Usage)
    model: str = ""

    @classmethod
    def from_raw(cls, raw: dict, model: str = "") -> "GeminiNativeResponse":
        candidates = raw.get("candidates", [])
        if not candidates:
            raise ValueError(f"Gemini native response has no candidates. Raw: {json.dumps(raw)[:500]}")

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if "text" in part).strip()
        finish_reason = candidate.get("finishReason", "stop").lower()

        usage_meta = raw.get("usageMetadata", {})
        usage = _Usage(
            prompt_tokens=usage_meta.get("promptTokenCount", 0),
            completion_tokens=usage_meta.get("candidatesTokenCount", 0),
            total_tokens=usage_meta.get("totalTokenCount", 0),
        )

        return cls(
            choices=[_Choice(message=_Message(content=text), finish_reason=finish_reason)],
            usage=usage,
            model=model,
        )


def _build_payload(
    *,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    search_grounding: bool,
    merge_system_into_user: bool = False,
) -> dict[str, Any]:
    if merge_system_into_user:
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_message}",
                        }
                    ],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
    else:
        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_message}],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }

    if search_grounding:
        payload["tools"] = [{"google_search": {}}]

    return payload


def _post_and_parse(*, api_key: str, model: str, payload: dict[str, Any]) -> GeminiNativeResponse:
    url = f"{_NATIVE_BASE}/{model}:generateContent"
    with httpx.Client(timeout=_TIMEOUT) as http:
        response = http.post(url, params={"key": api_key}, json=payload)

    if response.status_code != 200:
        raise RuntimeError(f"Gemini native API error {response.status_code}: {response.text}")

    raw = response.json()
    result = GeminiNativeResponse.from_raw(raw, model=model)
    log.info(
        "Gemini native response: tokens=%d finish=%s",
        result.usage.total_tokens,
        result.choices[0].finish_reason,
    )
    return result


async def _post_and_parse_async(*, api_key: str, model: str, payload: dict[str, Any]) -> GeminiNativeResponse:
    url = f"{_NATIVE_BASE}/{model}:generateContent"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        response = await http.post(url, params={"key": api_key}, json=payload)

    if response.status_code != 200:
        raise RuntimeError(f"Gemini native API error {response.status_code}: {response.text}")

    raw = response.json()
    result = GeminiNativeResponse.from_raw(raw, model=model)
    log.info(
        "Gemini native async response: tokens=%d finish=%s",
        result.usage.total_tokens,
        result.choices[0].finish_reason,
    )
    return result


def call_gemini_native(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 8000,
    temperature: float = 1.0,
    search_grounding: bool = True,
) -> GeminiNativeResponse:
    """Call the native Gemini generateContent endpoint synchronously."""
    log.debug("Gemini native call: model=%s search=%s", model, search_grounding)

    payload = _build_payload(
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
        search_grounding=search_grounding,
    )
    try:
        return _post_and_parse(api_key=api_key, model=model, payload=payload)
    except RuntimeError as error:
        error_text = str(error)
        if "400" not in error_text and "INVALID_ARGUMENT" not in error_text:
            raise

        log.warning("Gemini native rejected system_instruction, retrying with merged user turn")
        fallback_payload = _build_payload(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            search_grounding=search_grounding,
            merge_system_into_user=True,
        )
        return _post_and_parse(api_key=api_key, model=model, payload=fallback_payload)


async def call_gemini_native_async(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 8000,
    temperature: float = 1.0,
    search_grounding: bool = True,
) -> GeminiNativeResponse:
    """Async version of call_gemini_native. Same interface."""
    log.debug("Gemini native async call: model=%s search=%s", model, search_grounding)

    payload = _build_payload(
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
        search_grounding=search_grounding,
    )
    try:
        return await _post_and_parse_async(api_key=api_key, model=model, payload=payload)
    except RuntimeError as error:
        error_text = str(error)
        if "400" not in error_text and "INVALID_ARGUMENT" not in error_text:
            raise

        log.warning("Gemini native async rejected system_instruction, retrying with merged user turn")
        fallback_payload = _build_payload(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            search_grounding=search_grounding,
            merge_system_into_user=True,
        )
        return await _post_and_parse_async(api_key=api_key, model=model, payload=fallback_payload)