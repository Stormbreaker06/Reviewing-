import asyncio
import os
import random
import time
from itertools import count
 
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncOpenAI,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
)
 
from app.github.diff_parser import parse_review
 
load_dotenv()
 
# --- Config -----------------------------------------------------------------
# Every env var ending in _API_KEY joins the rotation pool:
#   NAME_API_KEY  = <key>     (required)
#   NAME_BASE_URL = <url>     (optional - detected from NAME or key prefix)
#   NAME_MODELS   = m1,m2     (optional - defaults to AGENTROUTER_MODEL)
MAX_ATTEMPTS = int(os.getenv("REVIEW_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("REVIEW_RETRY_DELAY", "1.0"))
CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "3"))
MODEL_COOLDOWN = float(os.getenv("MODEL_COOLDOWN", "300"))
MAX_CONTENT_CHARS = int(os.getenv("REVIEW_MAX_CONTENT_CHARS", "30000"))
MAX_OUTPUT_TOKENS = int(os.getenv("REVIEW_MAX_OUTPUT_TOKENS", "2000"))
 
FALLBACK_MODEL = "cohere/north-mini-code:free"
_DEFAULT_MODELS = [
    m.strip() for m in os.getenv("AGENTROUTER_MODEL", FALLBACK_MODEL).split(",") if m.strip()
] or [FALLBACK_MODEL]
 
# Provider lookup: by env var NAME first, then by key prefix.
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
# Order matters: "sk-or-" / "sk-ant-" must be checked before plain "sk-".
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
 
 
def _resolve_base_url(name: str, api_key: str) -> str:
    explicit = os.getenv(f"{name}_BASE_URL", "").strip()
    if explicit:
        return explicit
    if name.upper() in _NAME_BASE_URLS:
        return _NAME_BASE_URLS[name.upper()]
    for prefix, url in _PREFIX_BASE_URLS:
        if api_key.startswith(prefix):
            return url
    return ""
 
 
def _build_endpoints() -> list[tuple[dict, str]]:
    """Return a flat list of (provider, model) pairs - one per key x model."""
    endpoints = []
    for env_name, value in os.environ.items():
        api_key = value.strip()
        if not env_name.endswith("_API_KEY") or not api_key:
            continue
        name = env_name[: -len("_API_KEY")]
        base_url = _resolve_base_url(name, api_key)
        if not base_url:
            continue  # unknown provider: set NAME_BASE_URL to enable it
 
        raw_models = os.getenv(f"{name}_MODELS") or os.getenv(f"{name}_MODEL") or ""
        models = [m.strip() for m in raw_models.split(",") if m.strip()] or _DEFAULT_MODELS
 
        provider = {
            "name": name,
            # max_retries=0: our own loop owns retries, so they don't multiply
            "client": AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=0),
        }
        endpoints.extend((provider, m) for m in models)
    return endpoints
 
 
ENDPOINTS = _build_endpoints()
 
# --- Rotation + cooldowns ---------------------------------------------------
_semaphore = asyncio.Semaphore(CONCURRENCY)
_cursor = count()
# One dict for both levels: (provider,) cools a whole key, (provider, model) one model.
_cooldowns: dict[tuple, float] = {}
 
 
def _cool(*key: str) -> None:
    _cooldowns[key] = time.monotonic() + MODEL_COOLDOWN
 
 
def _cooldown_until(provider: dict, model: str) -> float:
    return max(
        _cooldowns.get((provider["name"],), 0.0),
        _cooldowns.get((provider["name"], model), 0.0),
    )
 
 
def _ordered_endpoints() -> list[tuple[dict, str]]:
    """Round-robin start point; skip cooled-down endpoints.
    If everything is cooling, try the one that recovers soonest."""
    if not ENDPOINTS:
        return []
    start = next(_cursor) % len(ENDPOINTS)
    ordered = ENDPOINTS[start:] + ENDPOINTS[:start]
    now = time.monotonic()
    available = [e for e in ordered if _cooldown_until(*e) <= now]
    return available or [min(ordered, key=lambda e: _cooldown_until(*e))]
 
 
def _classify(exc: Exception) -> str:
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return "bad_key"  # cool the whole key
    if isinstance(exc, RateLimitError):
        return "rate_limited"  # cool this model, switch if we can
    if isinstance(exc, APIConnectionError):  # includes timeouts
        return "transient"
    if isinstance(exc, APIStatusError) and exc.status_code >= 500:
        return "transient"
    return "bad_endpoint"  # bad model id / params: cool it, move on
 
 
# --- Prompt -----------------------------------------------------------------
PROMPT = """You are an expert GitHub pull request reviewer.
 
File: {filename}{change_note}
 
Full file content (context only):
```text
{content}
```
 
Changes introduced by the pull request:
```diff
{patch}
```
 
Review ONLY the changes in the diff. Rules:
- Report only high-confidence bugs, correctness issues, security vulnerabilities, performance problems, or significant maintainability problems that the changed code causes.
- Do NOT report pre-existing problems, theoretical risks, style/formatting, or unnecessary refactors.
- Prefer a few solid findings over many speculative ones. Never invent issues.
- Be concise. Keep Description and Fix to 1-2 sentences each. Do not show your reasoning.
 
Output format (plain text, no markdown, no JSON). For each issue:
 
ISSUE
Severity: High/Medium/Low
Line: <line number in the new file, or none>
Title: <short title>
Description: <what is wrong, which changed code, and why it matters>
Fix: <concrete fix>
 
Put a line containing only --- between issues.
If there are no meaningful issues, return exactly: NO ISSUES
"""
 
_DELETED_NOTE = (
    "\nThis file is DELETED by this pull request. The content below is the file "
    "BEFORE deletion - review whether removing it is safe."
)
 
 
def _build_prompt(filename: str, patch: str, content: str, status: str) -> str:
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "\n... [file truncated]"
    return PROMPT.format(
        filename=filename,
        change_note=_DELETED_NOTE if status == "deleted" else "",
        content=content,
        patch=patch,
    )
 
 
# --- Main entry point -------------------------------------------------------
def _backoff(attempt: int) -> float:
    return RETRY_BASE_DELAY * (2 ** attempt) * random.uniform(0.8, 1.2)
 
 
async def review_code(filename: str, patch: str, content: str, status: str = "modified"):
    endpoints = _ordered_endpoints()
    if not endpoints:
        return {"status": "error", "message": "No response from LLM: no API keys configured"}
 
    prompt = _build_prompt(filename, patch, content, status)
    messages = [{"role": "user", "content": prompt}]
    last_error = None
 
    for provider, model in endpoints:
        # Another concurrent request may have cooled this endpoint meanwhile.
        if len(endpoints) > 1 and _cooldown_until(provider, model) > time.monotonic():
            continue
 
        for attempt in range(MAX_ATTEMPTS):
            bad_output = False
            try:
                async with _semaphore:
                    response = await provider["client"].chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0,
                        max_tokens=MAX_OUTPUT_TOKENS,
                    )
            except Exception as e:
                last_error = str(e)
                kind = _classify(e)
                if kind == "bad_key":
                    _cool(provider["name"])
                    break
                if kind == "bad_endpoint":
                    _cool(provider["name"], model)
                    break
                if kind == "rate_limited":
                    _cool(provider["name"], model)
                    if len(endpoints) > 1:
                        break  # fail over now; with one endpoint, back off and retry
            else:
                text = response.choices[0].message.content if response.choices else None
                if text and text.strip():
                    parsed = parse_review(text)
                    if parsed.get("status") in ("clean", "reviewed"):
                        return parsed
                    last_error = "unrecognized response"
                else:
                    last_error = "empty message content"
                bad_output = True
 
            # Temperature is 0, so repeating a garbled answer rarely helps:
            # allow one retry for bad output, then move to the next endpoint.
            if bad_output and attempt >= 1:
                break
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(_backoff(attempt))
 
    if last_error == "unrecognized response":
        message = "LLM returned an unrecognized response after retries"
    elif last_error in (None, "empty message content"):
        message = "No response from LLM"
    else:
        message = f"No response from LLM: {last_error}"
    return {"status": "error", "message": message}
 
