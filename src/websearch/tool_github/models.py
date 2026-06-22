"""Pydantic mirrors of contracts/github.schema.json (github@1.0.0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GITHUB_CONTRACT_VERSION = "1.0.0"
DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 100


class GithubSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    language: str | None = None
    sort: Literal["best-match", "stars", "forks", "updated"] = "stars"
    order: Literal["asc", "desc"] = "desc"
    per_page: int = Field(default=DEFAULT_PER_PAGE, ge=1, le=MAX_PER_PAGE)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty or whitespace-only")
        return v

    def q(self) -> str:
        """The GitHub ``q`` string (query plus an optional language qualifier)."""
        q = self.query
        if self.language:
            q = f"{q} language:{self.language}"
        return q


class GithubRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str
    html_url: str
    description: str | None = None
    stars: int = Field(ge=0)
    forks: int = Field(default=0, ge=0)
    open_issues: int = Field(default=0, ge=0)
    language: str | None = None
    topics: list[str] = []
    owner: str | None = None
    updated_at: str | None = None
    pushed_at: str | None = None
    license: str | None = None


class GithubSearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    total_count: int | None = None
    incomplete_results: bool = False
    repos: list[GithubRepo] = []
    warnings: list[str] = []
