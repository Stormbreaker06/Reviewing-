import asyncio
import hashlib
import os
import time
import types
from itertools import count

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
    provider = make_provider("P", client, models)
    llm.ENDPOINTS = [(provider, m) for m in models]


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
          state["calls"] == 2 and out == {"status": "error", "message": "No response from LLM"},
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

    async def fake_review_code(filename, patch, content, status="modified"):
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
    async def failing_review(filename, patch, content, status="modified"):
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
        return {"head": {"sha": "sha1"}, "base": {"sha": "base1"}}

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
          state["calls"] == 2 and out["status"] == "error"
          and "unrecognized" in out["message"], (state["calls"], out))

    # failed review never enters the cache -> retried next request
    counters = {"review": 0}

    async def garbage_review(filename, patch, content, status="modified"):
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
    async def clean_review(filename, patch, content, status="modified"):
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
    saved = (llm.ENDPOINTS, dict(llm._cooldowns), llm._cursor)
    try:
        # round-robin across models on ONE key
        c1, s1 = make_client([make_response("NO ISSUES")] * 5)
        use_single(c1, ["modelA", "modelB"])
        llm._cooldowns.clear()
        llm._cursor = count()
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        used = [kw["model"] for kw in s1["kwargs"]]
        check("round-robin switches models", used == ["modelA", "modelB", "modelA"], used)

        # round-robin across TWO keys (no key overused)
        ck1, sk1 = make_client([make_response("NO ISSUES")] * 5)
        ck2, sk2 = make_client([make_response("NO ISSUES")] * 5)
        llm.ENDPOINTS = [
            (make_provider("K1", ck1, ["m"]), "m"),
            (make_provider("K2", ck2, ["m"]), "m"),
        ]
        llm._cooldowns.clear()
        llm._cursor = count()
        await llm.review_code("a.py", "p", "c")
        await llm.review_code("a.py", "p", "c")
        check("round-robin spreads across keys",
              sk1["calls"] == 1 and sk2["calls"] == 1, (sk1["calls"], sk2["calls"]))

        # 429 on key1 -> endpoint cooldown + failover to key2
        ck1, sk1 = make_client([http_error(RateLimitError, 429)])
        ck2, sk2 = make_client([make_response("NO ISSUES")])
        llm.ENDPOINTS = [
            (make_provider("K1", ck1, ["m"]), "m"),
            (make_provider("K2", ck2, ["m"]), "m"),
        ]
        llm._cooldowns.clear()
        llm._cursor = count()
        out = await llm.review_code("a.py", "p", "c")
        check("429 -> failover to next key",
              out == {"status": "clean", "issues": []}
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))
        check("rate-limited endpoint cooled",
              llm._cooldowns.get(("K1", "m"), 0.0) > time.monotonic())

        # cooled endpoint is skipped on the next call
        ck3, sk3 = make_client([make_response("NO ISSUES")])
        llm.ENDPOINTS = [
            (make_provider("K1", ck1, ["m"]), "m"),
            (make_provider("K2", ck3, ["m"]), "m"),
        ]
        out = await llm.review_code("a.py", "p", "c")
        check("cooldown skips rate-limited key",
              out == {"status": "clean", "issues": []} and sk1["calls"] == 1,
              (out, sk1["calls"]))

        # dead key (401) -> whole key cooled + failover to the next key
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        ck2, sk2 = make_client([make_response("NO ISSUES")])
        llm.ENDPOINTS = [
            (make_provider("K1", ck1, ["m"]), "m"),
            (make_provider("K2", ck2, ["m"]), "m"),
        ]
        llm._cooldowns.clear()
        llm._cursor = count()
        out = await llm.review_code("a.py", "p", "c")
        check("401 -> failover to next key",
              out == {"status": "clean", "issues": []}
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))
        check("dead key cooled down",
              llm._cooldowns.get(("K1",), 0.0) > time.monotonic())

        # every key dead -> error mentioning No response from LLM
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        ck2, sk2 = make_client([http_error(AuthenticationError, 401)])
        llm.ENDPOINTS = [
            (make_provider("K1", ck1, ["m"]), "m"),
            (make_provider("K2", ck2, ["m"]), "m"),
        ]
        llm._cooldowns.clear()
        llm._cursor = count()
        out = await llm.review_code("a.py", "p", "c")
        check("all keys dead -> error",
              out["status"] == "error" and "No response from LLM" in out["message"]
              and sk1["calls"] == 1 and sk2["calls"] == 1,
              (out, sk1["calls"], sk2["calls"]))

        # single dead key -> fail fast after one call
        ck1, sk1 = make_client([http_error(AuthenticationError, 401)])
        llm.ENDPOINTS = [(make_provider("K1", ck1, ["m"]), "m")]
        llm._cooldowns.clear()
        llm._cursor = count()
        out = await llm.review_code("a.py", "p", "c")
        check("single dead key fail fast",
              sk1["calls"] == 1 and out["status"] == "error"
              and "No response from LLM" in out["message"],
              (sk1["calls"], out))
    finally:
        llm.ENDPOINTS, llm._cooldowns, llm._cursor = saved


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

        endpoints = llm._build_endpoints()
        names = {p["name"] for p, _m in endpoints}

        check("groq by name -> base url",
              llm._resolve_base_url("GROQ", "gsk_fake_key") == "https://api.groq.com/openai/v1",
              llm._resolve_base_url("GROQ", "gsk_fake_key"))
        check("ollama by name beats key prefix",
              llm._resolve_base_url("OLLAMA", "sk-looks-like-an-openai-key")
              == "https://ollama.com/v1",
              llm._resolve_base_url("OLLAMA", "sk-looks-like-an-openai-key"))
        check("custom NAME_BASE_URL wins",
              llm._resolve_base_url("ZZTEST", "mystery-key") == "http://custom.example/v1",
              llm._resolve_base_url("ZZTEST", "mystery-key"))
        check("unknown provider has no base url",
              llm._resolve_base_url("NOPE", "mystery-key-2") == "",
              llm._resolve_base_url("NOPE", "mystery-key-2"))
        check("unknown provider skipped by builder", "NOPE" not in names, names)
        groq_models = sorted({m for p, m in endpoints if p["name"] == "GROQ"})
        check("models fall back to default pool",
              groq_models == ["pinned/test-model"], groq_models)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def test_deleted_binary():
    from app.github.diff_parser import parse_diff

    deleted_diff = "\n".join([
        "diff --git a/app/gone.py b/app/gone.py",
        "deleted file mode 100644",
        "index abc1234..0000000",
        "--- a/app/gone.py",
        "+++ /dev/null",
        "@@ -1,3 +0,0 @@",
        "-def a():",
        "-    return 1",
        "-",
    ])
    files = parse_diff(deleted_diff)
    check("deleted file detected",
          len(files) == 1 and files[0]["status"] == "deleted"
          and files[0]["filename"] == "app/gone.py" and files[0]["deletions"] == 3,
          files)

    added_diff = "\n".join([
        "diff --git a/app/new.py b/app/new.py",
        "new file mode 100644",
        "index 0000000..abc1234",
        "--- /dev/null",
        "+++ b/app/new.py",
        "@@ -0,0 +1,2 @@",
        "+def x():",
        "+    return 2",
    ])
    files = parse_diff(added_diff)
    check("added file detected",
          len(files) == 1 and files[0]["status"] == "added"
          and files[0]["additions"] == 2, files)

    binary_diff = "\n".join([
        "diff --git a/img.png b/img.png",
        "index abc..def 100644",
        "Binary files a/img.png and b/img.png differ",
    ])
    files = parse_diff(binary_diff)
    check("binary file detected",
          len(files) == 1 and files[0]["status"] == "binary", files)

    modified_diff = "\n".join([
        "diff --git a/app/x.py b/app/x.py",
        "index 111..222 100644",
        "--- a/app/x.py",
        "+++ b/app/x.py",
        "@@ -1 +1 @@",
        "-old",
        "+new",
    ])
    files = parse_diff(modified_diff)
    check("modified still detected",
          len(files) == 1 and files[0]["status"] == "modified", files)

    # safe_review: binary -> skipped, no content fetch, no LLM call
    counters = {"review": 0, "content": 0}

    async def counting_review(filename, patch, content, status="modified"):
        counters["review"] += 1
        return {"status": "clean", "issues": []}

    async def counting_content(**kwargs):
        counters["content"] += 1
        return "code"

    app_main.review_code = counting_review
    app_main.get_file_content = counting_content
    app_main._file_review_cache.clear()

    bfile = {"filename": "img.png", "status": "binary", "patch": ""}
    out = await app_main.safe_review(dict(bfile), "o", "r", 1, "sha1")
    check("binary skipped without llm or fetch",
          out == {"status": "skipped", "message": "Binary file - nothing to review"}
          and counters == {"review": 0, "content": 0}, (out, counters))

    # deleted -> content fetched at BASE ref, status passed to the LLM
    fetched_refs = []
    captured_statuses = []

    async def capturing_content(**kwargs):
        fetched_refs.append(kwargs.get("ref"))
        return "old content"

    async def capturing_review(filename, patch, content, status="modified"):
        captured_statuses.append(status)
        return {"status": "clean", "issues": []}

    app_main.get_file_content = capturing_content
    app_main.review_code = capturing_review
    dfile = {"filename": "app/gone.py", "status": "deleted", "patch": "-def a():"}
    out = await app_main.safe_review(dict(dfile), "o", "r", 1, "head1", base_ref="base1")
    check("deleted fetched from base ref", fetched_refs == ["base1"], fetched_refs)
    check("deleted status passed to llm", captured_statuses == ["deleted"], captured_statuses)
    check("deleted review ok", out == {"status": "clean", "issues": []}, out)

    # a binary file must NOT block the PR-level cache ('skipped' is success-ish)
    async def fake_pr2(owner, repo, pr_number):
        return {"head": {"sha": "sha9"}, "base": {"sha": "base9"}}

    async def fake_diff2(owner, repo, pr_number):
        return binary_diff + "\n" + modified_diff

    pr_counters = {"review": 0}

    async def pr_review(filename, patch, content, status="modified"):
        pr_counters["review"] += 1
        return {"status": "clean", "issues": []}

    async def pr_content(**kwargs):
        return "code"

    app_main.review_code = pr_review
    app_main.get_file_content = pr_content
    app_main.get_pull_request = fake_pr2
    app_main.get_pull_request_diff = fake_diff2
    app_main._file_review_cache.clear()
    app_main._review_cache.clear()

    r1 = await app_main.get_pr_review("octo", "repo", 11)
    r2 = await app_main.get_pr_review("octo", "repo", 11)
    statuses = {f["status"] for f in r1}
    check("pr response includes binary+modified", {"binary", "modified"} <= statuses, statuses)
    check("pr cache stores despite skipped file",
          r2 is r1 and pr_counters["review"] == 1, (pr_counters["review"], r2 is r1))


async def test_cache_key_hashing():
    counters = {"review": 0}

    async def reviewing(filename, patch, content, status="modified"):
        counters["review"] += 1
        return {"status": "clean", "issues": []}

    async def fake_content(**kwargs):
        return "code"

    app_main.review_code = reviewing
    app_main.get_file_content = fake_content
    app_main._file_review_cache.clear()

    p1 = {"filename": "app/h.py", "status": "modified",
          "patch": "@@ -1 +1 @@\n-aaaa\n+bbbb"}
    await app_main.safe_review(dict(p1), "o", "r", 1, "sha1")
    await app_main.safe_review(dict(p1), "o", "r", 1, "sha1")
    check("hashed key still caches", counters["review"] == 1, counters)

    keys = list(app_main._file_review_cache.keys())
    expected = hashlib.sha256(p1["patch"].encode("utf-8", errors="replace")).hexdigest()
    check("key holds sha256 digest not raw patch",
          len(keys) == 1 and keys[0][2] == expected and len(keys[0][2]) == 64,
          keys)

    # different patch of the SAME length -> different digest -> re-reviewed
    p2 = {"filename": "app/h.py", "status": "modified",
          "patch": "@@ -1 +1 @@\n-cccc\n+dddd"}
    await app_main.safe_review(dict(p2), "o", "r", 1, "sha1")
    check("different patch -> different key", counters["review"] == 2, counters)


async def main():
    await test_llm()
    await test_main_cache()
    await test_parse_and_ttl()
    await test_model_rotation()
    await test_base_url_detection()
    await test_deleted_binary()
    await test_cache_key_hashing()

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
