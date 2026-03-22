"""Retry helpers for API and network-bound operations."""

from __future__ import annotations

import asyncio
import random
import re
import time
from functools import wraps
from typing import Any, Callable

from utils.run_context import log_event, log_retry_attempt


NETWORK_ERROR_NAMES = {
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "SSLError",
    "Timeout",
    "TimeoutError",
}


def _is_network_error(error: Exception) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return type(error).__name__ in NETWORK_ERROR_NAMES


def _extract_http_status(error: Exception) -> int | None:
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        return int(status_code)

    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    if response_status is not None:
        return int(response_status)
    return None


def _scrub_secrets(text: str) -> str:
    # Scrub common API key patterns
    text = re.sub(r"(sk-[A-Za-z0-9\-]{8,})", "[API_KEY_REDACTED]", text)
    text = re.sub(r"(tvly-[A-Za-z0-9\-]{8,})", "[TAVILY_KEY_REDACTED]", text)
    text = re.sub(r"(AIza[A-Za-z0-9\-_]{30,})", "[GOOGLE_KEY_REDACTED]", text)
    return text


def _extract_response_snippet(error: Exception, limit: int = 200) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        text = getattr(response, "text", None)
        if text:
            return _scrub_secrets(str(text))[:limit]
        try:
            response_json = response.json()
        except Exception:
            response_json = None
        if response_json is not None:
            return _scrub_secrets(str(response_json))[:limit]

    error_body = getattr(error, "body", None)
    if error_body:
        return _scrub_secrets(str(error_body))[:limit]

    return _scrub_secrets(str(error))[:limit]


def describe_exception(error: Exception, limit: int = 200) -> tuple[int | None, str]:
    """Return best-effort HTTP status and response snippet for an exception."""
    return _extract_http_status(error), _extract_response_snippet(error, limit=limit)


def _is_rate_limit_error(error: Exception) -> bool:
    status_code, snippet = describe_exception(error, limit=500)
    if status_code == 429:
        return True
    error_text = f"{snippet} {error}".lower()
    return "rate_limit" in error_text or "rate limit" in error_text or "429" in error_text


def with_retry(
    func: Callable[[], Any],
    *,
    max_retries: int = 4,
    base_delay: float = 2.0,
) -> Any:
    """Retry a synchronous callable on transient 429/rate-limit failures."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as error:
            if not _is_rate_limit_error(error) or attempt >= max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(
                f"[Retry] 429 received — waiting {delay:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(delay)


async def with_retry_async(
    coro_fn: Callable[[], Any],
    *,
    max_retries: int = 4,
    base_delay: float = 2.0,
) -> Any:
    """Retry an async callable on transient 429/rate-limit failures."""
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except Exception as error:
            if not _is_rate_limit_error(error) or attempt >= max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(
                f"[Retry] 429 received — waiting {delay:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})"
            )
            await asyncio.sleep(delay)


def retry_with_backoff(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    error_type: str = "api",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry transient API and network failures with structured logging."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except Exception as error:
                    status_code, snippet = describe_exception(error)
                    function_name = func.__name__

                    if status_code in (401, 403):
                        print(f"AUTH ERROR {status_code} — not retrying unrecoverable auth failure")
                        raise
                    if _is_network_error(error):
                        if attempt >= max_attempts:
                            raise
                        delay = 30.0 if error_type == "network" else (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code == 429:
                        if attempt >= max_attempts:
                            raise
                        delay = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code is not None and 500 <= status_code < 600:
                        if attempt >= max_attempts:
                            raise
                        delay = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code is not None and 400 <= status_code < 500:
                        log_event(
                            phase="RETRY",
                            api_called=function_name,
                            http_status=status_code,
                            error_type=type(error).__name__,
                            raw_response_snippet=snippet,
                        )
                        raise
                    else:
                        raise

                    log_retry_attempt(
                        function_name=function_name,
                        attempt_number=attempt,
                        http_status=status_code,
                        response_snippet=snippet,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator


def async_retry_with_backoff(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    error_type: str = "api",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Async version of retry_with_backoff — uses ``await asyncio.sleep()``."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            while True:
                attempt += 1
                try:
                    return await func(*args, **kwargs)
                except Exception as error:
                    status_code, snippet = describe_exception(error)
                    function_name = func.__name__

                    if status_code in (401, 403):
                        print(f"AUTH ERROR {status_code} — not retrying unrecoverable auth failure")
                        raise
                    if _is_network_error(error):
                        if attempt >= max_attempts:
                            raise
                        delay = 30.0 if error_type == "network" else (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code == 429:
                        if attempt >= max_attempts:
                            raise
                        delay = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code is not None and 500 <= status_code < 600:
                        if attempt >= max_attempts:
                            raise
                        delay = (base_delay * (2 ** attempt)) + random.uniform(0, 1)
                    elif status_code is not None and 400 <= status_code < 500:
                        log_event(
                            phase="RETRY",
                            api_called=function_name,
                            http_status=status_code,
                            error_type=type(error).__name__,
                            raw_response_snippet=snippet,
                        )
                        raise
                    else:
                        raise

                    log_retry_attempt(
                        function_name=function_name,
                        attempt_number=attempt,
                        http_status=status_code,
                        response_snippet=snippet,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator

