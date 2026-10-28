# Intentions: a project's site on its domains -- the host's nginx in front of the site's container. The domains
# are the project's rows in site_domains (the user's own and subdomains of ours alike, DNS already pointing at
# us; a domain is only ever one project's); one nginx file per project (utils/nginx) holds a server block per
# domain.
#
# A domain right under settings.prod_domain uses our wildcard certificate, which already exists. Every other
# one gets its own Let's Encrypt certificate from certbot's webroot challenge: the first one is answered by the
# catch-all server set up once on the host (docs/plans/deployments.md), renewals by the site's own port-80
# block. Needs passwordless sudo for certbot, on top of what utils/nginx needs.

import os
import re

import pydash

from src.config import settings
from src.dto.projects import Project
from src.modules.deployments.constants import SITE_NGINX_TEMPLATE
from src.modules.deployments.repositories import site_domains as site_domains_repo
from src.modules.deployments.services import containers
from src.utils import nginx, templates
from src.utils.processes import run_command

# A plain host name, lower case: labels of letters, digits and inner hyphens, and a letters-only TLD. Strict
# on purpose -- a domain ends up in nginx's config and on certbot's command line.
_DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class InvalidDomain(ValueError):
    """Not a host name we'd serve a site on."""


async def get_domains(project: Project) -> list[str]:
    """The domains the project's site is served on, oldest first."""
    return [row["domain"] for row in await site_domains_repo.find({"project_id": project["id"]})]


def get_default_domain(project: Project) -> str | None:
    """[Pure-ish] The subdomain of ours a project's site can always have, <project hash>.<prod_domain> (under our
    wildcard certificate, so nothing to set up for it). None without a prod_domain. Only a suggestion: a site is
    served on whatever's in its domains, this one included only once it's put there."""
    return f"{project['hash']}.{settings.prod_domain}" if settings.prod_domain else None


def to_site_urls(domains: list[str]) -> list[str]:
    """[Pure]"""
    return [f"https://{domain}" for domain in domains]


async def set_domains(project: Project, domains: list[str]) -> list[str]:
    """Record the domains the project's site is served on (trimmed, lower-cased, deduped) and return them.
    They take effect at the next deployment. Raises InvalidDomain, or DomainTaken when one is another
    project's (then nothing changes)."""
    normalized = pydash.uniq([domain.strip().lower().rstrip(".") for domain in domains])
    invalid = [domain for domain in normalized if not _DOMAIN_PATTERN.match(domain)]
    if invalid:
        raise InvalidDomain(f"not a domain: {', '.join(invalid)}")
    await site_domains_repo.replace(project["id"], normalized)
    return normalized


async def ensure_site(project: Project, domains: list[str]) -> None:
    """Make sure every one of `domains` (the project's, see get_domains) has its certificate and routes to its
    container -- or, with none, that there's no nginx site at all. Idempotent: certbot keeps a certificate that
    isn't due for renewal, and nginx is only reloaded when the file changes."""
    site_path = os.path.join(settings.nginx_sites_dir, f"biveit-site-{project['id']}.conf")
    if not domains:
        await nginx.remove_site(site_path)
        return
    # One at a time: certbot holds a lock while it runs.
    for domain in domains:
        if not _is_under_wildcard(domain):
            await _ensure_certificate(domain)
    site_text = "\n".join(
        templates.render_template_file(
            SITE_NGINX_TEMPLATE,
            {
                "host": domain,
                "cert_name": settings.prod_domain if _is_under_wildcard(domain) else domain,
                "project_id": project["id"],
                "site_port": containers.get_site_port(project["id"]),
                "settings": settings,
            },
        )
        for domain in domains
    )
    await nginx.ensure_site(site_path, site_text)


def _is_under_wildcard(domain: str) -> bool:
    """[Pure-ish] Whether our *.<prod_domain> certificate covers `domain` -- exactly one label under it, since a
    wildcard doesn't reach deeper."""
    prefix = domain.removesuffix(f".{settings.prod_domain}")
    return bool(settings.prod_domain) and prefix != domain and "." not in prefix


async def _ensure_certificate(domain: str) -> None:
    """A certificate for `domain`, named after it (so its files are /etc/letsencrypt/live/<domain>/). A quick
    no-op when there's one that isn't due for renewal.

    Let's Encrypt checks we own the domain by fetching http://<domain>/.well-known/acme-challenge/<token>, a file
    certbot drops in the webroot. So before this can work:
    - Once per server (docs/plans/deployments.md, "Server setup"): certbot installed, the webroot folder, port 80
      open, a catch-all port-80 nginx server answering the challenge path for ANY domain (a new domain has no site
      file of its own yet), sudoers for `certbot certonly *`, and the renewal hook that reloads nginx.
    - Once per domain: an A record pointing at this server, and no AAAA pointing elsewhere (Let's Encrypt tries
      IPv6 first). `www.` is a domain of its own. Nothing else -- the site doesn't have to be up.
    Not called for subdomains of prod_domain: the wildcard certificate covers them (_is_under_wildcard).
    """
    await run_command(
        [
            "sudo",
            "-n",
            "certbot",
            "certonly",
            "--webroot",
            f"--webroot-path={settings.certbot_webroot}",
            f"--cert-name={domain}",
            f"--domains={domain}",
            "--non-interactive",
            "--agree-tos",
            "--keep-until-expiring",
            *(
                [f"--email={settings.certbot_email}"]
                if settings.certbot_email
                else ["--register-unsafely-without-email"]
            ),
        ]
    )
