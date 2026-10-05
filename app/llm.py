import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
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

    If the changes do not introduce any meaningful problems, respond with exactly:

    "No significant issues found."

    Do not invent issues. Prefer a small number of high-confidence findings over many speculative findings.
    """

    response = await client.chat.completions.create(
        model="openrouter/free",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        max_tokens=1500
    )

    return response.choices[0].message.content