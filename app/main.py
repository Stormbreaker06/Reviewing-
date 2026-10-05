import os
import httpx
import asyncio
from fastapi import FastAPI
from app.github.client import (
    get_pull_request,
    get_pull_request_diff,
    get_file_content
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
    files = parse_diff(diff)
    # for file in files:
    #     file["content"] = await get_file_content(owner=owner, repo=repo,filename=file["filename"])
    #     print(file["content"])
    return files

async def safe_review(
        file,
        owner : str,
        repo : str,
        pr_number : int,
        ref: str
    ):
    try:
        # pr = await get_pull_request(owner=owner,repo=repo,pr_number=pr_number)
        # ref = pr["head"]["sha"]
        file["content"] = await get_file_content(owner=owner,repo=repo,filename=file["filename"],ref=ref)
        return await review_code(
            file["filename"],
            file["patch"],
            file["content"],
        )
    except Exception as e:
        return f"Review failed: {str(e)}"

    
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

    diff = await get_pull_request_diff(
        owner,
        repo,
        pr_number
    )
    files = parse_diff(diff)
    reviews = await asyncio.gather(
        *[
            safe_review(file,owner,repo,pr_number,ref)
            for file in files
        ]
    )

    for file, review in zip(files, reviews):
        file["review"] = review

    return files
    
