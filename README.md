# Project Context: CodeTurtle - AI GitHub PR Reviewer

> Share this file with any AI model to get roadmap/architecture feedback without
> giving it repo access. Paste it as-is, then ask your question.

## What it is
A full-stack service that reviews GitHub pull requests with LLMs and shows results
in a web UI. Give it owner/repo/PR-number, it fetches the diff, reviews each changed
file, and renders per-file findings (severity, line, description, fix).

## Stack
- **Backend**: Python 3.14, FastAPI, httpx, OpenAI SDK (works against any
  OpenAI-compatible gateway), python-dotenv
- **Frontend**: React 19 + Vite (plain JS/JSX, no router/state libs), dark-theme CSS
- **Tests**: single script, 55 checks: `\.venv\Scripts\python.exe _test_consistency.py`
- **Repo**: public at https://github.com/Stormbreaker06/Reviewing- (branch `frontend_changes`)

## Architecture
```
React UI (localhost:5173)
  -> GET /github/review/{owner}/{repo}/{pr}/review   (FastAPI, localhost:8000)
      -> GitHub API: PR metadata, unified diff
      -> parse_diff(): files[] {filename,status,patch,additions,deletions}
         status = modified|added|deleted|renamed|binary
      -> per file, concurrently (asyncio.gather, semaphore=3):
           cache lookup (SHA, filename, sha256(patch))
           content fetch (head sha; BASE sha for deleted files)
           review_code() -> LLM pool -> parse_review() -> {status, issues[]}
      -> PR-level cache (owner, repo, pr, head sha)
```

## Endpoints
- `GET /` - health
- `GET /github/pr/{owner}/{repo}/{pr}` - PR metadata
- `GET /github/pr/{owner}/{repo}/{pr}/diff` - parsed file list
- `GET /github/review/{owner}/{repo}/{pr}/review` - full review (the main one)
- CORS allows `FRONTEND_ORIGIN` (default http://localhost:5173)

## Key modules
- `app/main.py` - routes, two in-memory caches (file-level + PR-level), 10-min TTL,
  LRU eviction (cap 50), expired-entry sweep, skip-binary/base-ref logic
- `app/llm.py` - multi-KEY multi-MODEL pool, round-robin endpoints (key x model),
  failover on 429/401/4xx, cooldown dict (key-level and endpoint-level), retry with
  jittered exponential backoff, temperature=0, prompt builder with 30k-char content cap
- `app/github/diff_parser.py` - `parse_diff` (detects add/delete/renamed/binary via
  real git signals) + `parse_review` (strict: unparseable LLM output => error, never
  cached as fake success)
- `app/github/client.py` - GitHub REST calls (PR, diff, file content at any ref)
- `frontend/src/` - App (state: loading/error/files), PrForm (URL parser), Summary,
  FileReview (collapsible cards), api.js (single API base)

## Key design decisions (with rationale)
1. **Multi-key rotation**: env vars ending `_API_KEY` auto-join the pool; base URL
   detected by var NAME (GROQ, OLLAMA...) or key prefix (sk-or-, gsk_...). Round-robin
   spreads daily rate limits; 429 cools an endpoint 5 min; 401 cools the whole key.
2. **Validation before cache**: parse_review returns error for unrecognizable LLM
   output so garbage is NEVER cached as a "successful empty review"; errors retry.
3. **Same PR => same answer**: caches keyed by head SHA; new commit = fresh review.
4. **Deleted files**: content fetched from BASE sha (file gone at head), prompt notes
   the deletion. **Binary files**: skipped entirely (zero LLM calls).
5. **Cache keys use sha256(patch)**, not raw patches (memory), with touch-on-hit LRU
   + expired sweep.
6. **False-positive discipline**: LLM self-reviews suggested asyncio locks/races -
   dismissed because the runtime is single-threaded event-loop (no await between
   check-and-write = atomic). Always validate AI findings against execution model.

## Config (.env - NEVER share this file; it holds 3 API keys + GitHub token)
- `AGENTROUTER_API_KEY/BASE_URL/MODEL` (OpenRouter), `GROQ_API_KEY`, `OLLAMA_API_KEY`
- `*_MODELS` comma lists; knobs: REVIEW_MAX_ATTEMPTS(5), REVIEW_RETRY_DELAY(1.0),
  LLM_CONCURRENCY(3), MODEL_COOLDOWN(300), REVIEW_CACHE_TTL(600),
  REVIEW_MAX_CONTENT_CHARS(30000), REVIEW_MAX_OUTPUT_TOKENS(2000), FRONTEND_ORIGIN

## Current state / recent history
- Working end-to-end (frontend screenshot verified); 55/55 tests green
- Two rounds of LLM self-review triaged: 4 findings fixed, 6 dismissed (see git log)
- Known limitations: in-memory caches (per-process, lost on restart), no streaming
  (whole-PR wait), no GitHub comment posting, free-tier model variability, GitHub
  diff API truncates very large PRs (300 files / 20k lines)

## Seed backlog (expand/critique/prioritize these)
- SSE streaming: show files as they complete instead of one big wait
- Post review comments back to the GitHub PR (review comments API)
- GitHub App / webhook: auto-review on PR open (currently manual trigger)
- Cross-file analysis (e.g., deleted file still imported elsewhere?)
- Token savings: window file content to changed hunks; move rules to system message
  for prompt caching
- Redis cache for multi-worker deployment
- Per-provider usage/token dashboard (cost control)
- Auth for the API itself (it is currently open)
- Severity gate as a CI status check (fail PR on high-severity findings)
- pytest migration + frontend tests (React Testing Library)
- Docker compose deployment; Markdown/JSON export of reviews
