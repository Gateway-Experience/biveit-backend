# The deployments module -- putting a project's site in production: whatever's in its workspace, built into an
# image and running in its own container on the platform's machine, on the user's domains, with its own prod
# database and Redis. Also: where that stands (what's live, whether there's anything new to deploy) and the
# history of it, and downloading the project's code (the same workspace repo, zipped).
#
# Each deployment is a git commit of the workspace; retries pick up where the last attempt got to, and a
# deployment that never comes up rolls back to what was live. services/deployments.py has the full story,
# services/__init__.py what each file owns (repositories/ holds its own table, site_domains). Server setup it needs (sudo for certbot, the ACME catch-all):
# docs/plans/deployments.md.
# Callers import THIS package (`from src.modules import deployments`), not the files.

from src.modules.deployments.repositories.site_domains import DomainTaken
from src.modules.deployments.services.deployments import (
    DeploymentInProgress,
    NothingToDeploy,
    deploy,
    get_deployment_status,
    list_deployments,
)
from src.modules.deployments.services.sites import InvalidDomain, get_default_domain, set_domains
from src.modules.deployments.services.sources import NothingToDownload, archive_workspace

__all__ = [
    "DeploymentInProgress",
    "DomainTaken",
    "InvalidDomain",
    "NothingToDeploy",
    "NothingToDownload",
    "archive_workspace",
    "deploy",
    "get_default_domain",
    "get_deployment_status",
    "list_deployments",
    "set_domains",
]
