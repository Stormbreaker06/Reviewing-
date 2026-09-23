import os
import httpx
from fastapi import FastAPI
from app.github.client import (
    get_pull_request,
    get_pull_request_diff
)
from app.github.diff_parser import parse_diff
from app.llm import review_code
app = FastAPI()

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
    return parse_diff(diff)

@app.get("/github/review/{owner}/{repo}/{pr_number}/review")
async def get_pr_review(
    owner: str,
    repo: str,
    pr_number :int
):
    diff = await get_pull_request_diff(
        owner,
        repo,
        pr_number
    )
    files = parse_diff(diff)
    res = []
    for file in files:
        review = await review_code(
            file["filename"],
            file["patch"]
        )
        res.append({
            "filename" : file["filename"],
            "review" : review
        })

    return res
