import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
from app.model import (
    ReviewIssue,
    ReviewResult
)
from app.github.diff_parser import parse_review
import asyncio
load_dotenv()


client = AsyncOpenAI(
    api_key=os.getenv("AGENTROUTER_API_KEY"),
    base_url=os.getenv("AGENTROUTER_BASE_URL")
)



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
    

    The JSON must follow this structure:

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

    Do not include markdown, code fences, or any text outside the JSON.
    """

    try:
        response = await client.chat.completions.create(
            model="cohere/north-mini-code:free",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=4000,
            extra_body={
                "reasoning":{
                    "max_tokens":2000
                }
            }
        )
    except Exception as e:
        return {
            "status": "error",
            "message": f"No response from LLM: {str(e)}"
        }

    if response is None or not response.choices:
        return {
            "status": "error",
            "message": "No response from LLM"
        }

    print("FINISH:", response.choices[0].finish_reason)
    print("CONTENT:", response.choices[0].message.content)
    print("REASONING:", response.choices[0].message.reasoning)
    content = response.choices[0].message.content
    if not content:
        return {
            "status": "error",
            "message": "No response from LLM"
        }
    

    result = parse_review(content)

    return result
