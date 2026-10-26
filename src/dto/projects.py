# Project typing (see dto/__init__ for the two kinds):
#  - The Project INTERFACE (TypedDict) over the projects-row dict the repo returns.
#  - The request CONTRACTS (Pydantic) validating untrusted HTTP bodies.

from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import BaseModel, Field, StringConstraints, model_validator


class Project(TypedDict):
    """A projects-row interface over the dict the repo returns. Schema: migrations/0005_projects.sql."""

    id: int
    hash: str  # random public handle; names its storage folder and dev site (see 0005_projects.sql)
    user_id: int  # the owner
    user_hash: str  # the owner's hash, joined in on every read -- its folder sits inside the owner's
    name: str
    description: str | None
    stage: str  # ProjectStage -- planning | building
    bootstrap_data: dict[str, Any]  # the starting form's answers, sent when the project was created
    # When the user first signed off the built site (the APPROVE_BUILD approval); None before.
    # From then on the FE offers payment/deployment, whatever the stage.
    build_approved_at: Any  # datetime | None
    pinned_at: Any  # datetime | None -- when the user pinned it to the top of their sidebar
    # The folder it sits directly in, or None (the root). Always a live folder: deleting one clears this.
    folder_id: int | None
    meta: dict[str, Any]
    created_at: Any  # datetime
    updated_at: Any  # datetime -- "last edited"; the list is sorted by it
    trashed_at: Any  # datetime | None -- when the user moved it to Trash; None = not in Trash
    deleted_at: Any  # datetime | None -- always None on anything a read returns (reads skip deleted rows)
    # When the project list's picture of its dev site was taken (services/devenv/previews), None = there's
    # none. Not a column: the API adds it where the FE shows the picture (controllers/projects).
    site_preview_at: NotRequired[Any]  # datetime | None


class CreateProjectRequest(BaseModel):
    """POST /projects body -- sent when the user submits the starting form, which is when a project comes
    into being. The owner is always the caller, never a body field. extra="forbid" so a FE sending a field
    it thinks is saved (e.g. user_id) finds out instead of having it silently dropped."""

    model_config = {"extra": "forbid"}

    # 200 = the column's varchar limit. Trimmed first, so a name of only spaces is refused.
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: str | None = None
    # The starting form's answers, stored as-is (their shape is the FE's to decide) and handed to the agent.
    bootstrap_data: dict[str, Any] = Field(default_factory=dict)


class UpdateProjectRequest(BaseModel):
    """PATCH /projects/{id} body -- what the user may change about a project from the project list and
    the sidebar: its name, whether it's pinned, and its folder (null takes it out of one). Send only what
    changes; an omitted field is left alone. extra="forbid", same reason as CreateProjectRequest."""

    model_config = {"extra": "forbid"}

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    is_pinned: bool | None = None
    folder_id: int | None = None

    @model_validator(mode="after")
    def _refuse_empty(self) -> "UpdateProjectRequest":
        """An edit that changes nothing is almost certainly a FE bug -- say so (422) instead of a quiet 200."""
        if not self.model_fields_set:
            raise ValueError("nothing to update")
        return self


class ProjectFolder(TypedDict):
    """A project_folders-row interface. Schema: migrations/0008_project_folders.sql."""

    id: int
    user_id: int  # the owner
    parent_id: int | None  # the folder it sits in; None = the root
    name: str
    created_at: Any  # datetime
    updated_at: Any  # datetime
    deleted_at: Any  # datetime | None -- always None on anything a read returns


# 200 = the column's varchar limit. Trimmed first, so a name of only spaces is refused.
_FolderName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CreateProjectFolderRequest(BaseModel):
    """POST /project-folders body: the new folder's name, and the folder to make it in (omitted or null =
    the root). extra="forbid", same reason as CreateProjectRequest."""

    model_config = {"extra": "forbid"}

    name: _FolderName
    parent_id: int | None = None


class UpdateProjectFolderRequest(BaseModel):
    """PATCH /project-folders/{id} body: rename it, move it (parent_id; null = to the root), or both. Send
    only what changes; an omitted field is left alone. extra="forbid", same reason as CreateProjectRequest."""

    model_config = {"extra": "forbid"}

    name: _FolderName | None = None
    parent_id: int | None = None

    @model_validator(mode="after")
    def _refuse_empty(self) -> "UpdateProjectFolderRequest":
        """Same as UpdateProjectRequest's: an edit that changes nothing is a FE bug, so 422."""
        if not self.model_fields_set:
            raise ValueError("nothing to update")
        return self
