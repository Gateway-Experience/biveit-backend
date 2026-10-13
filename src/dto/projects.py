# Project typing (see dto/__init__ for the two kinds):
#  - The Project INTERFACE (TypedDict) over the projects-row dict the repo returns.
#  - The request CONTRACTS (Pydantic) validating untrusted HTTP bodies.

from typing import Any, TypedDict

from pydantic import BaseModel, Field


class Project(TypedDict):
    """A projects-row interface over the dict the repo returns. Schema: migrations/0005_projects.sql."""

    id: int
    user_id: int  # the owner
    name: str
    description: str | None
    current_port: int | None  # last seen agent-session port; a hint, can be stale (see 0005_projects.sql)
    meta: dict[str, Any]
    created_at: Any  # datetime
    updated_at: Any  # datetime -- "last edited"; the list is sorted by it
    deleted_at: Any  # datetime | None -- always None on anything a read returns (reads skip deleted rows)


class CreateProjectRequest(BaseModel):
    """POST /projects body. The owner is always the caller, never a body field. extra="forbid" so a FE
    sending a field it thinks is saved (e.g. user_id) finds out instead of having it silently dropped."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)  # 200 = the column's varchar limit
    description: str | None = None
