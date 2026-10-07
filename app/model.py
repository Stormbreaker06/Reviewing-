from pydantic import BaseModel
from typing import Literal
class ReviewIssue(BaseModel):
    severity: Literal["high", "medium", "low"]
    line: int | None
    title: str
    description: str
    fix: str


class ReviewResult(BaseModel):
    issues: list[ReviewIssue]