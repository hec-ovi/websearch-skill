"""Pydantic mirrors of contracts/arxiv.schema.json (arxiv@1.0.0).

A standalone keyless tool, not part of the search-fetch-format pipeline. It emits
the cross-cutting Envelope so the CLI and MCP faces handle it like any other layer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ARXIV_CONTRACT_VERSION = "1.0.0"
DEFAULT_MAX_RESULTS = 10
MAX_MAX_RESULTS = 50

_FIELD_PREFIX = {"all": "all", "title": "ti", "author": "au", "abstract": "abs"}


class ArxivSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    field: Literal["all", "title", "author", "abstract"] = "all"
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_MAX_RESULTS)
    start: int = Field(default=0, ge=0)
    sort_by: Literal["relevance", "lastUpdatedDate", "submittedDate"] = "relevance"
    sort_order: Literal["ascending", "descending"] = "descending"

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty or whitespace-only")
        return v

    def search_query(self) -> str:
        """The arXiv ``search_query`` string (field-prefixed).

        A multi-word topic query is wrapped in quotes so arXiv treats it as a phrase.
        Without quotes, arXiv loose-matches the individual terms, and a date sort then
        returns the globally newest papers that merely contain any one term rather than
        papers about the topic. A query that already carries quotes or a boolean operator
        is passed through unchanged so power users keep full control.
        """
        q = self.query
        ql = q.lower()
        has_ops = '"' in q or " and " in ql or " or " in ql or " andnot " in ql
        if " " in q and not has_ops:
            q = f'"{q}"'
        return f"{_FIELD_PREFIX[self.field]}:{q}"


class ArxivPaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arxiv_id: str
    title: str
    authors: list[str] = []
    summary: str = ""
    published: str | None = None
    updated: str | None = None
    abs_url: str
    pdf_url: str | None = None
    primary_category: str | None = None
    categories: list[str] = []
    comment: str | None = None
    doi: str | None = None


class ArxivSearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    total_results: int | None = None
    start: int = 0
    papers: list[ArxivPaper] = []
    warnings: list[str] = []
