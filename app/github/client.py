import os
import httpx
import base64
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


async def get_pull_request(owner: str, repo: str, pr_number: int):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    if response.status_code != 200:
        raise Exception(
            f"GitHub API error: {response.status_code} {response.text}"
        )

    return response.json()

async def get_pull_request_diff(owner: str, repo: str, pr_number: int):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

    headers_with_diff = {
        **headers,
        "Accept": "application/vnd.github.v3.diff",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=headers_with_diff
        )

    if response.status_code != 200:
        raise Exception(
            f"GitHub API error: {response.status_code} {response.text}"
        )

    return response.text

async def get_file_content(
        owner: str,
        repo: str,
        filename: str,
        ref : str
        ):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}?ref={ref}"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            headers=headers
        )
    if response.status_code != 200:
        raise Exception(
            f"galat.."
        )
    data = response.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content