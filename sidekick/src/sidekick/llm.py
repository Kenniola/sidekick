"""LLM client — tiered model routing with retry, backoff, and fallback.

Tiers map call-site intent to the right model:

  fast     → gpt-4o-mini via Copilot API    (classifier, quick decisions)
  standard → claude-sonnet-4.5 via Copilot  (research, synthesis, suggest)
  deep     → claude-opus-4.7 via Copilot    (complex research, prototypes)

Fallback: GitHub Models (gpt-4.1-mini, gpt-4.1) via gh auth token.

Auth:
  Both endpoints use the same GitHub token from `gh auth token`.
  The token is refreshed every 30 minutes to handle expiry during
  long-running meeting sessions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# Copilot API (primary) — uses Copilot Enterprise subscription
_COPILOT_URL = "https://api.githubcopilot.com/chat/completions"

# GitHub Models (fallback) — free tier with gh token
_GITHUB_MODELS_URL = "https://models.inference.ai.azure.com/chat/completions"

# Tier → (provider, model) fallback chains
_TIER_CONFIG: dict[str, list[tuple[str, str]]] = {
    "fast": [
        ("copilot", "gpt-4o-mini"),
        ("github_models", "gpt-4.1-mini"),
    ],
    "standard": [
        ("copilot", "claude-sonnet-4.5"),
        ("copilot", "gpt-4.1"),
        ("github_models", "gpt-4.1-mini"),
    ],
    "deep": [
        ("copilot", "claude-opus-4.7"),
        ("copilot", "claude-opus-4.6"),
        ("copilot", "gpt-4.1"),
        ("github_models", "DeepSeek-R1"),
    ],
}

# Retry settings
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds: 1, 2, 4

# ---------------------------------------------------------------------------
# GitHub token management (gh auth token, refreshed periodically)
# ---------------------------------------------------------------------------

_gh_token: str = ""
_gh_token_acquired: float = 0.0
_GH_TOKEN_REFRESH_SECS = 1800  # refresh every 30 min


async def _get_gh_token() -> str:
    """Get a GitHub token, refreshing via `gh auth token` when stale.

    Priority:
      1. GITHUB_TOKEN env var (if set explicitly)
      2. `gh auth token` subprocess (uses keyring-cached credential)
    """
    global _gh_token, _gh_token_acquired

    # Env var takes priority (e.g. in CI)
    env_token = os.environ.get("GITHUB_TOKEN", "")
    if env_token:
        return env_token

    # Return cached token if fresh enough
    if _gh_token and (time.time() - _gh_token_acquired) < _GH_TOKEN_REFRESH_SECS:
        return _gh_token

    # Acquire via gh CLI
    import shutil
    import sys

    gh_cmd = "gh.exe" if sys.platform == "win32" else "gh"
    gh_path = shutil.which(gh_cmd) or shutil.which("gh")
    if not gh_path:
        raise RuntimeError(
            "Cannot acquire GitHub token: GITHUB_TOKEN not set and "
            "gh CLI not found on PATH."
        )

    proc = await asyncio.create_subprocess_exec(
        gh_path, "auth", "token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh auth token failed: {stderr.decode().strip()}"
        )
    _gh_token = stdout.decode().strip()
    _gh_token_acquired = time.time()
    logger.debug("GitHub token acquired via gh auth token")
    return _gh_token


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


async def _call_copilot(
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_output: bool,
    timeout: float,
) -> str:
    """Call via GitHub Copilot API (Enterprise subscription)."""
    token = await _get_gh_token()

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_output:
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _COPILOT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Copilot-Integration-Id": "vscode-chat",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _call_github_models(
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_output: bool,
    timeout: float,
) -> str:
    """Call via GitHub Models API (free tier, broader model catalog)."""
    token = await _get_gh_token()

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if json_output:
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _GITHUB_MODELS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


_PROVIDERS = {
    "copilot": _call_copilot,
    "github_models": _call_github_models,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def call_llm(
    system_prompt: str,
    user_prompt: str,
    json_output: bool = False,
    timeout: float = 60.0,
    tier: str = "standard",
) -> str:
    """Call an LLM with automatic tier routing, retry, and fallback.

    Tiers:
      - 'fast'     — gpt-4o-mini (classifier, quick decisions)
      - 'standard' — claude-sonnet-4.5 (research, synthesis, suggest)
      - 'deep'     — claude-opus-4.7 (complex research, prototypes)

    Each tier has a fallback chain: Copilot API → GitHub Models.

    Retry: up to 3 attempts per provider with exponential backoff (1s, 2s, 4s).
    On 429 or 5xx, retries the same provider then falls through to the next.

    Args:
        system_prompt: System-level instructions for the LLM.
        user_prompt: User-level prompt (the task to perform).
        json_output: If True, request JSON response format.
        timeout: Request timeout in seconds.
        tier: 'fast', 'standard', or 'deep'.

    Returns:
        The LLM's response text.
    """
    chain = _TIER_CONFIG.get(tier, _TIER_CONFIG["standard"])
    last_error: Exception | None = None

    for provider_name, model in chain:
        provider_fn = _PROVIDERS.get(provider_name)
        if not provider_fn:
            continue

        for attempt in range(_MAX_RETRIES):
            try:
                logger.debug(
                    "LLM call: tier=%s provider=%s model=%s attempt=%d",
                    tier, provider_name, model, attempt + 1,
                )
                content = await provider_fn(
                    model, system_prompt, user_prompt, json_output, timeout
                )
                logger.debug("LLM response (%d chars)", len(content))
                return content

            except httpx.HTTPStatusError as e:
                last_error = e
                status = e.response.status_code
                if status == 429 or status >= 500:
                    wait = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "LLM %s/%s returned %d, retry in %.1fs (attempt %d/%d)",
                        provider_name, model, status, wait, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait)
                    continue
                # 4xx other than 429 — don't retry, fall through
                logger.warning(
                    "LLM %s/%s returned %d, skipping to next provider",
                    provider_name, model, status,
                )
                break

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                wait = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "LLM %s/%s connection error: %s, retry in %.1fs",
                    provider_name, model, type(e).__name__, wait,
                )
                await asyncio.sleep(wait)
                continue

            except Exception as e:
                last_error = e
                logger.warning(
                    "LLM %s/%s unexpected error: %s",
                    provider_name, model, e,
                )
                break

    raise RuntimeError(
        f"All LLM providers failed for tier={tier!r}. Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Vision API — multimodal image analysis
# ---------------------------------------------------------------------------


async def call_llm_vision(
    system_prompt: str,
    user_prompt: str,
    image_b64: str,
    timeout: float = 30.0,
) -> str:
    """Send an image to a vision-capable model for analysis.

    Uses gpt-4o-mini on Copilot API (supports vision natively).
    Falls back to gpt-4o-mini on GitHub Models.

    Args:
        system_prompt: System-level instructions.
        user_prompt: Text prompt to accompany the image.
        image_b64: Base64-encoded PNG image.
        timeout: Request timeout in seconds.

    Returns:
        The model's description/analysis of the image.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "high",
                    },
                },
            ],
        },
    ]

    token = await _get_gh_token()
    body = {"model": "gpt-4o-mini", "messages": messages, "max_tokens": 2048}

    # Try Copilot API first
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                _COPILOT_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Copilot-Integration-Id": "vscode-chat",
                },
                json=body,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    except Exception as e:
        logger.warning("Copilot vision call failed: %s, trying GitHub Models", e)

    # Fallback: GitHub Models
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _GITHUB_MODELS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
