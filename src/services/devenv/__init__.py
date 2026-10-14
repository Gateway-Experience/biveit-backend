# The devenv service package -- a project's development environment: the folders its code lives in on
# disk, and (to come) the dev servers that run it on the project's fixed ports (see SESSION_PORT_RANGE
# in constants for the port plan).
#   workspaces.py   the on-disk layout and getting a workspace created and seeded from the template
#
# It doesn't know about the agent or the chat -- services/agent builds on it, never the other way round.
# Callers import THIS package (`from src.services import devenv`), not the files.

from src.services.devenv.workspaces import ensure_workspace, get_conversation_dir, get_project_dir

__all__ = ["ensure_workspace", "get_conversation_dir", "get_project_dir"]
