import os
import time
from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
from dotenv import load_dotenv
from app.model import (
    ReviewIssue,
    ReviewResult
)
from app.github.diff_parser import parse_review
import asyncio
load_dotenv()


# --- LLM key pool -----------------------------------------------------------
# Every env var ending in _API_KEY joins the rotation pool:
#   NAME_API_KEY  = <key>     (required)
#   NAME_BASE_URL = <url>     (optional - auto-detected from the key prefix)
#   NAME_MODELS   = m1,m2     (optional - defaults to AGENTROUTER_MODEL)
# Endpoints (key x model) rotate round-robin; failures fail over to the next.
MAX_ATTEMPTS = int(os.getenv("REVIEW_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("REVIEW_RETRY_DELAY", "1.0"))
CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "3"))
MODEL_COOLDOWN = float(os.getenv("MODEL_COOLDOWN", "300"))

_DEFAULT_MODELS = [
    m.strip()
    for m in os.getenv("AGENTROUTER_MODEL", "cohere/north-mini-code:free").split(",")
    if m.strip()
] or ["cohere/north-mini-code:free"]

# Known providers by env var NAME (checked before key-prefix detection).
_NAME_BASE_URLS = {
    "GROQ": "https://api.groq.com/openai/v1",
    "OLLAMA": "https://ollama.com/v1",
    "OPENROUTER": "https://openrouter.ai/api/v1",
    "OPENAI": "https://api.openai.com/v1",
    "CEREBRAS": "https://api.cerebras.ai/v1",
    "ANTHROPIC": "https://api.anthropic.com/v1",
    "XAI": "https://api.x.ai/v1",
    "HUGGINGFACE": "https://router.huggingface.co/v1",
}

# Known providers, detected from the key prefix (override with NAME_BASE_URL).
_PREFIX_BASE_URLS = (
    ("sk-or-", "https://openrouter.ai/api/v1"),
    ("gsk_", "https://api.groq.com/openai/v1"),
    ("csk-", "https://api.cerebras.ai/v1"),
    ("sk-ant-", "https://api.anthropic.com/v1"),
    ("hf_", "https://router.huggingface.co/v1"),
    ("xai-", "https://api.x.ai/v1"),
    ("AIza", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("sk-", "https://api.openai.com/v1"),
)


def _detect_base_url(api_key: str) -> str | None:
    for prefix, url in _PREFIX_BASE_URLS:
        if api_key.startswith(prefix):
            return url
    return None


def _build_providers() -> list[dict]:
    providers = []
    for env_name, value in os.environ.items():
        if not env_name.endswith("_API_KEY"):
            continue
        api_key = value.strip()
        if not api_key:
            continue
        prefix = env_name[: -len("_API_KEY")]
        base_url = (
            os.getenv(f"{prefix}_BASE_URL", "").strip()
            or _NAME_BASE_URLS.get(prefix.upper())
            or _detect_base_url(api_key)
            or ""
        )
        if not base_url:
            # unknown provider - set NAME_BASE_URL in .env to enable this key
            continue
        models = [
            m.strip()
            for m in os.getenv(f"{prefix}_MODELS", os.getenv(f"{prefix}_MODEL", "")).split(",")
            if m.strip()
        ] or list(_DEFAULT_MODELS)
        providers.append({
            "name": prefix,
            "api_key": api_key,
            "base_url": base_url,
            "models": models,
            "client": AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=60.0,
                max_retries=2,
            ),
        })
    return providers


PROVIDERS = _build_providers()

_semaphore = asyncio.Semaphore(CONCURRENCY)
_endpoint_cooldowns: dict[tuple, float] = {}
_provider_cooldowns: dict[str, float] = {}
_endpoint_cursor = 0


def _ordered_endpoints() -> list[tuple]:
    """Round-robin (provider, model) endpoints, skipping cooled-down ones."""
    global _endpoint_cursor
    now = time.monotonic()
    endpoints = [(p, m) for p in PROVIDERS for m in p["models"]]
    if not endpoints:
        return []
    start = _endpoint_cursor % len(endpoints)
    ordered = endpoints[start:] + endpoints[:start]
    _endpoint_cursor = (start + 1) % len(endpoints)
    available = [
        (p, m) for p, m in ordered
        if _endpoint_cooldowns.get((p["name"], m), 0.0) <= now
        and _provider_cooldowns.get(p["name"], 0.0) <= now
    ]
    return available or ordered


def _is_rate_limited(exc: Exception) -> bool:
    return isinstance(exc, RateLimitError)


def _is_retryable(exc: Exception) -> bool:
    """Retry only transient failures: connection/timeout errors, 429, 5xx."""
    if isinstance(exc, (APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500
    return False



async def review_code(filename: str, patch: str, content: str):

    prompt = f"""
    You are an expert GitHub pull request reviewer.

    File:
    {filename}

    Full file content:
    ```text
    {content}
    ```

    Changes introduced by the pull request:
    ```diff
    {patch}
    ```

    Your task is to review the changes in the pull request.

    Use the full file content only to understand the context of the changes.

    IMPORTANT REVIEW RULES:

    Focus primarily on the code changed in the diff.
    Do NOT report pre-existing problems that are unrelated to the changes.
    Do NOT report issues merely because something could theoretically go wrong.
    Only report high-confidence issues that the changed code actually causes or could reasonably cause.
    Do not report minor formatting or style issues.
    Do not suggest unnecessary refactoring.
    Consider the surrounding code when determining whether a change is actually problematic.
    Check for bugs, correctness issues, security vulnerabilities, performance problems, and significant maintainability issues.
    

    For every issue you find, provide:

    What is wrong
    The specific changed code responsible
    Why it matters
    A concrete way to fix it

    Do not invent issues. Prefer a small number of high-confidence findings over many speculative findings.
    
    "Be concise" + "Do not provide your reasoning process" + short Description/Fix limits.

    For each issue, use this format:

    ISSUE
    Severity: High/Medium/Low
    Line: <line number if applicable>
    Title: <short title>
    Description: <what is wrong and why>
    Fix: <how to fix it>

    Separate multiple issues with:

    If there are no meaningful issues, simply return:

    NO ISSUES

    Return plain text only.

    Return only the review in the format specified above.
    Do not return JSON.
    """

    last_error = None
    result = None

    endpoints = _ordered_endpoints()
    if not endpoints:
        return {
            "status": "error",
            "message": "No response from LLM: no API keys configured"
        }

    for provider, model in endpoints:
        if _provider_cooldowns.get(provider["name"], 0.0) > time.monotonic():
            continue  # this key already failed earlier in the request
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with _semaphore:
                    response = await provider["client"].chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "user",
                                "content": prompt
                            }
                        ],
                        temperature=0,
                        max_tokens=2000
                    )
            except Exception as e:
                last_error = str(e)
                if isinstance(e, (AuthenticationError, PermissionDeniedError)):
                    # bad/expired key - cool the whole key and use the next one
                    _provider_cooldowns[provider["name"]] = time.monotonic() + MODEL_COOLDOWN
                    break
                if _is_rate_limited(e):
                    _endpoint_cooldowns[(provider["name"], model)] = time.monotonic() + MODEL_COOLDOWN
                    if len(endpoints) > 1:
                        break  # switch to the next key/model now
                    # only endpoint: fall through to backoff retries
                if not _is_retryable(e):
                    # bad model id/params for this endpoint - cool it, move on
                    _endpoint_cooldowns[(provider["name"], model)] = time.monotonic() + MODEL_COOLDOWN
                    break  # key/model-specific error -> next endpoint
                if attempt == MAX_ATTEMPTS - 1:
                    break
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue

            if response is None or not response.choices:
                last_error = "empty response"
            else:
                candidate = response.choices[0].message.content
                if candidate and candidate.strip():
                    parsed = parse_review(candidate)
                    if parsed.get("status") in ("clean", "reviewed"):
                        result = parsed
                        break
                    last_error = "unrecognized response"
                else:
                    last_error = "empty message content"

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))

        if result is not None:
            break

    if result is not None:
        return result

    if last_error == "unrecognized response":
        return {
            "status": "error",
            "message": "LLM returned an unrecognized response after retries"
        }

    if last_error in (None, "empty response", "empty message content"):
        return {
            "status": "error",
            "message": "No response from LLM"
        }

    return {
        "status": "error",
        "message": f"No response from LLM: {last_error}"
    }
