import asyncio
import os
import time
import types

os.environ["REVIEW_RETRY_DELAY"] = "0.01"
os.environ["REVIEW_MAX_ATTEMPTS"] = "3"
os.environ["LLM_CONCURRENCY"] = "2"
os.environ["AGENTROUTER_MODEL"] = "pinned/test-model"

import httpx
from openai import BadRequestError, RateLimitError, APIStatusError, AuthenticationError

from app import llm
from app import main as app_main

results = []


def check(name, cond, info=""):
    results.append((name, bool(cond), info))


def make_response(content, reasoning=None):
    msg = types.SimpleNamespace(content=content, reasoning=reasoning)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg, finish_reason="stop")]
    )


def make_client(behaviors):
    state = {"calls": 0, "kwargs": []}

    async def create(**kwargs):
        state["kwargs"].append(kwargs)
        behavior = behaviors[min(state["calls"], len(behaviors) - 1)]
        state["calls"] += 1
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    return (
        types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        ),
        state,
    )


def http_error(cls, status):
    request = httpx.Request("POST", "https://gateway.test/v1/chat/completions")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


def make_provider(name, client, models):
    return {"name": name, "api_key": "k", "base_url": "http://test",
            "models": list(models), "client": client}


def use_single(client, models=("pinned/test-model",)):
    llm.PROVIDERS = [make_provider("P", client, models)]


async def test_llm():
    # 1. 429 then success -> retried; pinned model + temperature=0
    client, state = make_client([http_error(RateLimitError, 429), make_response("NO ISSUES")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("429 retried -> clean", out == {"status": "clean", "issues": []}, out)
    check("2 calls after retry", state["calls"] == 2, state["calls"])
    check("model pinned from env", state["kwargs"][0]["model"] == "pinned/test-model",
          state["kwargs"][0]["model"])
    check("temperature=0", state["kwargs"][0]["temperature"] == 0,
          state["kwargs"][0]["temperature"])

    # 2. 500 then success -> retried
    client, state = make_client([http_error(APIStatusError, 500), make_response("NO ISSUES")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("500 retried -> clean", out == {"status": "clean", "issues": []} and state["calls"] == 2,
          (out, state["calls"]))

    # 3. 400 -> fail fast (no retry), message mentions no response from LLM
    client, state = make_client([http_error(BadRequestError, 400)])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("400 fail fast",
          state["calls"] == 1 and out["status"] == "error"
          and "No response from LLM" in out["message"],
          (state["calls"], out))

    # 4. empty content every attempt -> error after retries
    client, state = make_client([make_response(""), make_response(None), make_response("   ")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("empty retried -> error",
          state["calls"] == 3 and out == {"status": "error", "message": "No response from LLM"},
          (state["calls"], out))

    # 5. transient error on every attempt -> error after retries
    client, state = make_client([http_error(RateLimitError, 429)])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("all attempts fail -> error",
          state["calls"] == 3 and "No response from LLM" in out["message"],
          (state["calls"], out))

    # 6. empty once then valid -> success
    client, state = make_client([make_response(""), make_response("NO ISSUES")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("empty then success", out == {"status": "clean", "issues": []} and state["calls"] == 2,
          (out, state["calls"]))


async def test_main_cache():
    counters = {"review": 0}

    async def fake_review_code(filename, patch, content):
        counters["review"] += 1
        return {"status": "clean", "issues": []}

    async def fake_get_file_content(**kwargs):
        return "print('hi')"

    app_main.review_code = fake_review_code
    app_main.get_file_content = fake_get_file_content

    app_main._file_review_cache.clear()
    app_main._review_cache.clear()

    # file-level cache: same ref+filename+patch -> one LLM call
    file = {"filename": "app/x.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
    await app_main.safe_review(dict(file), "o", "r", 1, "sha1")
    await app_main.safe_review(dict(file), "o", "r", 1, "sha1")
    check("file cache hit", counters["review"] == 1, counters)

    # error results are NOT cached -> retried next request
    async def failing_review(filename, patch, content):
        counters["review"] += 1
        return {"status": "error", "message": "No response from LLM"}

    app_main.review_code = failing_review
    f2 = {"filename": "app/y.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
    await app_main.safe_review(dict(f2), "o", "r", 1, "sha1")
    await app_main.safe_review(dict(f2), "o", "r", 1, "sha1")
    check("errors not cached", counters["review"] == 3, counters)

    # PR-level cache: same PR + head SHA -> identical response, no LLM calls
    app_main.review_code = fake_review_code
    app_main._file_review_cache.clear()
    app_main._review_cache.clear()
    counters["review"] = 0

    async def fake_pr(owner, repo, pr_number):
        return {"head": {"sha": "sha1"}}

    diff_text = "\n".join([
        "diff --git a/app/x.py b/app/x.py",
        "index 1234567..89abcde 100644",
        "--- a/app/x.py",
        "+++ b/app/x.py",
        "@@ -1,2 +1,2 @@",
        "-old_line",
        "+new_line",
        " second_line",
    ])

    async def fake_diff(owner, repo, pr_number):
        return diff_text

    app_main.get_pull_request = fake_pr
    app_main.get_pull_request_diff = fake_diff

    res1 = await app_main.get_pr_review("octo", "repo", 7)
    check("pr first call hits llm", counters["review"] == 1, counters)
    res2 = await app_main.get_pr_review("octo", "repo", 7)
    check("pr cache -> identical response", counters["review"] == 1 and res2 is res1, counters)

    # PR cache miss but file cache populated -> no new LLM calls
    app_main._review_cache.clear()
    await app_main.get_pr_review("octo", "repo", 7)
    check("file cache reused", counters["review"] == 1, counters)

    # file cache miss but PR cache hit -> no new LLM calls
    app_main._file_review_cache.clear()
    await app_main.get_pr_review("octo", "repo", 7)
    check("pr cache short-circuits", counters["review"] == 1, counters)

    # both cleared -> fresh LLM call
    app_main._file_review_cache.clear()
    app_main._review_cache.clear()
    await app_main.get_pr_review("octo", "repo", 7)
    check("fresh review after both caches cleared", counters["review"] == 2, counters)
    check("response shape (file dicts with review)",
          res1 and isinstance(res1[0], dict) and "review" in res1[0], res1)


async def test_parse_and_ttl():
    from app.github.diff_parser import parse_review

    # garbage -> parse error (never a fake "successful" empty review)
    out = parse_review("Sorry, I cannot review this file right now.")
    check("garbage -> parse error", out["status"] == "error", out)

    # NO ISSUES with trailing punctuation -> clean
    out = parse_review("NO ISSUES.")
    check("NO ISSUES. -> clean", out == {"status": "clean", "issues": []}, out)

    # real issue -> reviewed
    out = parse_review("Severity: High\nTitle: Bug\nDescription: d\nFix: f")
    check("issue parsed", out["status"] == "reviewed" and len(out["issues"]) == 1, out)

    # llm: garbage once then valid -> retried to success
    client, state = make_client([make_response("totally unparseable text"),
                                 make_response("NO ISSUES")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("garbage then success",
          out == {"status": "clean", "issues": []} and state["calls"] == 2, (out, state["calls"]))

    # llm: garbage every attempt -> error, never a fake success
    client, state = make_client([make_response("garbage one"), make_response("garbage two"),
                                 make_response("garbage three")])
    use_single(client)
    out = await llm.review_code("a.py", "p", "c")
    check("all garbage -> error",
          state["calls"] == 3 and out["status"] == "error"
          and "unrecognized" in out["message"], (state["calls"], out))

    # failed review never enters the cache -> retried next request
    counters = {"review": 0}

    async def garbage_review(filename, patch, content):
        counters["review"] += 1
        return {"status": "error", "message": "LLM returned an unrecognized response"}

    async def fake_content(**kwargs):
        return "code"

    app_main.review_code = garbage_review
    app_main.get_file_content = fake_content
    app_main._file_review_cache.clear()
    f = {"filename": "app/g.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
    r1 = await app_main.safe_review(dict(f), "o", "r", 1, "sha1")
    r2 = await app_main.safe_review(dict(f), "o", "r", 1, "sha1")
    check("failed review not cached -> retried",
          counters["review"] == 2 and r1["status"] == "error", counters)

    # TTL: cached within TTL, re-reviewed after expiry
    async def clean_review(filename, patch, content):
        counters["review"] += 1
        return {"status": "clean", "issues": []}

    app_main.review_code = clean_review
    app_main._file_review_cache.clear()
    counters["review"] = 0
    f = {"filename": "app/t.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
    await app_main.safe_review(dict(f), "o", "r", 1, "sha1")
    await app_main.safe_review(dict(f), "o", "r", 1, "sha1")
    check("cache hit before TTL expiry", counters["review"] == 1, counters)
    app_main.CACHE_TTL = 0.01
    await asyncio.sleep(0.03)
    await app_main.safe_review(dict(f), "o", "r", 1, "sha1")
    check("TTL expiry -> re-review", counters["review"] == 2, counters)
    app_main.CACHE_TTL = 600


async def test_model_rotation():
    saved = (
        llm.PROVIDERS,
        dict(llm._endpoint_cooldowns),
        dict(llm._provider_cooldowns),
        llm._endpoint_cursor,
    )
    try:
        # round-robin across models on ONE key
        c1, s1 = make_client([make_response("NO ISSUES")] * 5)
        llm.PROVIDERS = [make_provider("K1", c1, ["modelA", "modelB"])]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        used = [kw["model"] for kw in s1["kwargs"]]
        check("round-robin switches models", used == ["modelA", "modelB", "modelA"], used)

        # round-robin across TWO keys (no key overused)
        ck1, sk1 = make_client([make_response("NO ISSUES")] * 5)
        ck2, sk2 = make_client([make_response("NO ISSUES")] * 5)
        llm.PROVIDERS = [
            make_provider("K1", ck1, ["m"]),
            make_provider("K2", ck2, ["m"]),
        ]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        check("round-robin spreads across keys",
              sk1["calls"] == 1 and sk2["calls"] == 1, (sk1["calls"], sk2["calls"]))

        # 429 on key1 -> endpoint cooldown + failover to key2
        ck1, sk1 = make_client([http_error(RateLimitError, 429)])
        ck2, sk2 = make_client([make_response("NO ISSUES")])
        llm.PROVIDERS = [
            make_provider("K1", ck1, ["m"]),
            make_provider("K2", ck2, ["m"]),
        ]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        out = await llm.review_code("a.py", "p", "c")
        check("429 -> failover to next key",
              out == {"status": "clean", "issues": []}
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))
        check("rate-limited endpoint cooled",
              llm._endpoint_cooldowns.get(("K1", "m"), 0.0) > time.monotonic())

        # cooled endpoint is skipped on the next call
        ck3, sk3 = make_client([make_response("NO ISSUES")])
        llm.PROVIDERS = [
            make_provider("K1", ck1, ["m"]),
            make_provider("K2", ck3, ["m"]),
        ]
        out = await llm.review_code("a.py", "p", "c")
        check("cooldown skips rate-limited key",
              out == {"status": "clean", "issues": []} and sk1["calls"] == 1,
              (out, sk1["calls"]))

        # dead key (401) -> whole key cooled + failover to the next key
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        ck2, sk2 = make_client([make_response("NO ISSUES")])
        llm.PROVIDERS = [
            make_provider("K1", ck1, ["m"]),
            make_provider("K2", ck2, ["m"]),
        ]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        out = await llm.review_code("a.py", "p", "c")
        check("401 -> failover to next key",
              out == {"status": "clean", "issues": []}
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))
        check("dead key cooled down",
              llm._provider_cooldowns.get("K1", 0.0) > time.monotonic())

        # every key dead -> error mentioning No response from LLM
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        ck2, sk2 = make_client([http_error(AuthenticationError, 401)])
        llm.PROVIDERS = [
            make_provider("K1", ck1, ["m"]),
            make_provider("K2", ck2, ["m"]),
        ]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        out = await llm.review_code("a.py", "p", "c")
        check("all keys dead -> error",
              out["status"] == "error" and "No response from LLM" in out["message"]
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))

        # single dead key -> fail fast after one call
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        llm.PROVIDERS = [make_provider("K1", ck1, ["m"])]
        llm._endpoint_cooldowns.clear()
        llm._provider_cooldowns.clear()
        llm._endpoint_cursor = 0
        out = await llm.review_code("a.py", "p", "c")
        check("single dead key fail fast",
              sk1["calls"] == 1 and out["status"] == "error"
              and "No response from LLM" in out["message"],
              (sk1["calls"], out))
    finally:
        (
            llm.PROVIDERS,
            llm._endpoint_cooldowns,
            llm._provider_cooldowns,
            llm._endpoint_cursor,
        ) = saved


async def test_base_url_detection():
    added = ["GROQ_API_KEY", "GROQ_MODELS", "GROQ_BASE_URL",
             "OLLAMA_API_KEY", "OLLAMA_BASE_URL",
             "ZZTEST_API_KEY", "ZZTEST_BASE_URL", "NOPE_API_KEY", "NOPE_BASE_URL"]
    saved = {k: os.environ.get(k) for k in added}
    try:
        os.environ["GROQ_API_KEY"] = "gsk_fake_key"
        os.environ.pop("GROQ_BASE_URL", None)
        os.environ.pop("GROQ_MODELS", None)
        os.environ["OLLAMA_API_KEY"] = "sk-looks-like-an-openai-key"
        os.environ.pop("OLLAMA_BASE_URL", None)
        os.environ["ZZTEST_API_KEY"] = "mystery-key"
        os.environ["ZZTEST_BASE_URL"] = "http://custom.example/v1"
        os.environ["NOPE_API_KEY"] = "mystery-key-2"
        os.environ.pop("NOPE_BASE_URL", None)

        providers = {p["name"]: p for p in llm._build_providers()}

        check("groq by name -> base url",
              providers.get("GROQ", {}).get("base_url") == "https://api.groq.com/openai/v1",
              providers.get("GROQ", {}).get("base_url"))
        check("ollama by name beats key prefix",
              providers.get("OLLAMA", {}).get("base_url") == "https://ollama.com/v1",
              providers.get("OLLAMA", {}).get("base_url"))
        check("custom NAME_BASE_URL wins",
              providers.get("ZZTEST", {}).get("base_url") == "http://custom.example/v1",
              providers.get("ZZTEST", {}).get("base_url"))
        check("unknown provider without base url skipped",
              "NOPE" not in providers, list(providers))
        check("models fall back to default pool",
              providers.get("GROQ", {}).get("models") == ["pinned/test-model"],
              providers.get("GROQ", {}).get("models"))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def main():
    await test_llm()
    await test_main_cache()
    await test_parse_and_ttl()
    await test_model_rotation()
    await test_base_url_detection()

    ok = True
    for name, passed, info in results:
        line = f"[{'PASS' if passed else 'FAIL'}] {name}"
        if not passed:
            line += f" -> {info}"
        print(line)
        ok = ok and passed
    print("\nALL PASSED" if ok else "\nSOME TESTS FAILED")
    raise SystemExit(0 if ok else 1)


asyncio.run(main())
