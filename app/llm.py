import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
print("BASE URL:", os.getenv("AGENTROUTER_BASE_URL"))
print("MODEL:", os.getenv("AGENTROUTER_MODEL"))
print("KEY EXISTS:", bool(os.getenv("AGENTROUTER_API_KEY")))

client = AsyncOpenAI(
    api_key=os.getenv("AGENTROUTER_API_KEY"),
    base_url=os.getenv("AGENTROUTER_BASE_URL")
)

MODEL = os.getenv("AGENTROUTER_MODEL")


async def review_code(filename: str, patch: str):

    prompt = f"""
    You are reviewing a GitHub pull request.

    File:
    {filename}

    Changed code:
    ```diff
    {patch}
    ```

    Look for:
    - bugs and correctness issues
    - security issues
    - performance issues
    - maintainability problems

    For each issue, explain:
    1. What is wrong
    2. Why it matters
    3. How to fix it

    If there are no meaningful issues, say:
    "No significant issues found."
    """
    response = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        max_tokens=1000
    )

    return response.choices[0].message.content