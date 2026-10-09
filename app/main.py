import hashlib
import os
import httpx
import asyncio
import time
from fastapi import FastAPI
from app.github.client import (
    get_pull_request,
    get_pull_request_diff,
    get_file_content
)
from app.github.diff_parser import parse_diff
from app.llm import review_code
app = FastAPI()

# Same PR (same head SHA) + same file patch -> same review, served from cache.
# Only successful, parsed reviews are cached; failures/garbage are NEVER stored
# (they retry on the next request) and entries expire after CACHE_TTL seconds.
_review_cache: dict[tuple, tuple] = {}
_file_review_cache: dict[tuple, tuple] = {}
CACHE_LIMIT = 50
CACHE_TTL = float(os.getenv("REVIEW_CACHE_TTL", "600"))


def _remember(cache: dict, key: tuple, value) -> None:
    if len(cache) >= CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    cache[key] = (value, time.monotonic())


def _lookup(cache: dict, key: tuple):
    entry = cache.get(key)
    if entry is None:
        return None
    value, stored_at = entry
    if time.monotonic() - stored_at > CACHE_TTL:
        cache.pop(key, None)
        return None
    return value


def _patch_digest(patch: str) -> str:
    """Fixed-size (64 hex chars) cache-key part. Raw patches can be huge;
    using them as dict keys would pin diff text in memory for the whole TTL."""
    return hashlib.sha256(patch.encode("utf-8", errors="replace")).hexdigest()

@app.get("/")
async def root():
    return {"message": "AI Code Reviewer"}


@app.get("/github/pr/{owner}/{repo}/{pr_number}")
async def get_pr(
    owner: str,
    repo: str,
    pr_number: int
):
    
    pr = await get_pull_request(owner, repo, pr_number)

    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr["body"],
        "state": pr["state"],
        "url": pr["html_url"],
        "changed_files": pr["changed_files"],
        "additions": pr["additions"],
        "deletions": pr["deletions"]
    }

@app.get("/github/pr/{owner}/{repo}/{pr_number}/diff")
async def get_pr_diff(
    owner: str,
    repo: str,
    pr_number: int
):
    diff = await get_pull_request_diff(
        owner,
        repo,
        pr_number
    )
    files = parse_diff(diff)

    return files

async def safe_review(
        file,
        owner : str,
        repo : str,
        pr_number : int,
        ref: str,
        base_ref: str = None
    ):
    # Binary files have nothing textual to review - never fetch, never call the LLM.
    if file.get("status") == "binary":
        return {
            "status": "skipped",
            "message": "Binary file - nothing to review"
        }
    cache_key = (ref, file["filename"], _patch_digest(file.get("patch") or ""))
    cached = _lookup(_file_review_cache, cache_key)
    if cached is not None:
        return cached
    try:
        # Deleted files no longer exist at the head ref - read the pre-deletion
        # content from the base commit so the deletion itself can be reviewed.
        content_ref = base_ref if file.get("status") == "deleted" and base_ref else ref
        file["content"] = await get_file_content(owner=owner,repo=repo,filename=file["filename"],ref=content_ref)
        result = await review_code(
            file["filename"],
            file["patch"],
            file["content"],
            file.get("status", "modified"),
        )
        if isinstance(result, dict) and result.get("status") in ("clean", "reviewed"):
            _remember(_file_review_cache, cache_key, result)
        return result
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

    
@app.get("/github/review/{owner}/{repo}/{pr_number}/review")
async def get_pr_review(
    owner: str,
    repo: str,
    pr_number :int
):
    pr = await get_pull_request(
        owner,
        repo,
        pr_number
    )
    ref = pr["head"]["sha"]
    base_ref = pr["base"]["sha"]

    pr_key = (owner, repo, pr_number, ref)
    cached = _lookup(_review_cache, pr_key)
    if cached is not None:
        return cached

    diff = await get_pull_request_diff(
        owner,
        repo,
        pr_number
    )
    files = parse_diff(diff)
    reviews = await asyncio.gather(
        *[
            safe_review(file,owner,repo,pr_number,ref,base_ref)
            for file in files
        ]
    )
    res = []
    for file, review in zip(files, reviews):
        file["review"] = review
        file.pop("content", None)
        res.append(file)

    if res and all(
        isinstance(r.get("review"), dict)
        and r["review"].get("status") in ("clean", "reviewed", "skipped")
        for r in res
    ):
        _remember(_review_cache, pr_key, res)

    return res
    
