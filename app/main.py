import os
import httpx
from fastapi import FastAPI
from app.github.client import get_pull_request

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
        "deletions": pr["deletions"],
    }