# Intentions: a project's site on its domains -- the host's nginx in front of the site's container. The domains
# attached to a project are its rows in site_domains (a domain is only ever one project's); the site is served on
# the ACTIVE one, if it may be: one we bought or an admin set, or the user's own once its ownership proof checks
# out (check_external_domain). One nginx file per project (utils/nginx) holds a server block per served domain.
#
# Proving ownership of a user's own domain takes two DNS records: an A record pointing at the server (traffic,
# and the certificate) and a TXT record at _biveit-verify.<domain> holding a token derived from the project and
# the domain -- so nothing is stored, and nobody can attach a domain that points at us but isn't theirs. And no
# AAAA record: Let's Encrypt tries IPv6 first, so one pointing at the domain's old host fails the certificate.
#
# A domain right under settings.prod_domain uses our wildcard certificate, which already exists. Every other
# one gets its own Let's Encrypt certificate from certbot's webroot challenge: the first one is answered by the
# catch-all server set up once on the host (docs/plans/deployments.md), renewals by the site's own port-80
# block. Needs passwordless sudo for certbot, on top of what utils/nginx needs.

import asyncio
import os
import re
from urllib.parse import urlsplit

import dns.asyncresolver
import dns.exception
import pydash

from src.config import settings
from src.dto.projects import Project
from src.modules.deployments.constants import (
    DOMAIN_CHECK_NAMESERVERS,
    DOMAIN_CHECK_TIMEOUT_SECONDS,
    DOMAIN_PROOF_PREFIX,
    SITE_NGINX_TEMPLATE,
    SiteDomainKind,
)
from src.modules.deployments.repositories import site_domains as site_domains_repo
from src.modules.deployments.services import containers
from src.services import devenv
from src.utils import nginx, templates
from src.utils.processes import run_command

# A plain host name, lower case: labels of letters, digits and inner hyphens, and a letters-only TLD. Strict
# on purpose -- a domain ends up in nginx's config and on certbot's command line.
_DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class InvalidDomain(ValueError):
    """Not a host name we'd serve a site on."""


async def get_site_domains(project: Project) -> list[dict]:
    """The domains attached to the project, oldest first: [{domain, kind, activated_at (None = not active),
    created_at}]."""
    return await site_domains_repo.find({"project_id": project["id"]})


async def get_served_domains(project: Project) -> list[str]:
    """What the site should be served on right now: its active domain, if it may be (see the top of this file),
    else nothing."""
    active = pydash.find(await get_site_domains(project), lambda row: row["activated_at"] is not None)
    if active is None:
        return []
    if (
        active["kind"] == SiteDomainKind.EXTERNAL
        and not (await check_external_domain(project, active["domain"]))["verified"]
    ):
        return []
    return [active["domain"]]


async def attach_domain(project: Project, domain: str, kind: SiteDomainKind, activate: bool) -> None:
    """Attach a domain to the project (kept if it's already there), and make it the active one if `activate`. It's
    served from the next deployment. Raises InvalidDomain, DomainTaken."""
    normalized = normalize_domain(domain)
    await site_domains_repo.attach(project["id"], normalized, kind)
    if activate:
        await site_domains_repo.activate(project["id"], normalized)


async def check_external_domain(project: Project, domain: str) -> dict:
    """Whether a user's own domain can be attached to the project, and whether its DNS records are in place:
    {domain (normalized), blocked: None | "invalid" | "ours" | "taken", records: [{type, name, value, action,
    found}], verified}. action "add": a record the user must have (the A and the TXT); "remove": one they must take
    off (any AAAA, listed only when there is one). verified = every "add" found and no "remove" left. The lookups
    ask public resolvers, not the server's cache, so a record the user
    just added shows up as soon as it's out there. [I/O: reads]"""
    try:
        normalized = normalize_domain(domain)
    except InvalidDomain:
        return {"domain": domain, "blocked": "invalid", "records": [], "verified": False}
    owners = await site_domains_repo.find({"domain": normalized})
    if _is_ours(normalized):
        blocked = "ours"
    elif owners and owners[0]["project_id"] != project["id"]:
        blocked = "taken"
    else:
        blocked = None
    proof_name = f"{DOMAIN_PROOF_PREFIX}.{normalized}"
    proof_value = f"biveit-verify={get_domain_proof_token(project, normalized)}"
    a_values, txt_values, aaaa_values = await asyncio.gather(
        _resolve(normalized, "A"), _resolve(proof_name, "TXT"), _resolve(normalized, "AAAA")
    )
    records = [
        {"type": "A", "name": normalized, "value": settings.server_public_ip, "action": "add", "found": settings.server_public_ip in a_values},
        {"type": "TXT", "name": proof_name, "value": proof_value, "action": "add", "found": proof_value in txt_values},
        *[{"type": "AAAA", "name": normalized, "value": value, "action": "remove", "found": True} for value in sorted(aaaa_values)],
    ]  # fmt: skip
    return {
        "domain": normalized,
        "blocked": blocked,
        "records": records,
        "verified": blocked is None and all(record["found"] == (record["action"] == "add") for record in records),
    }


async def is_pointed_at_server(domain: str) -> bool:
    """Whether public DNS answers `domain`'s A record with the server's address -- what Let's Encrypt needs before
    it can issue a certificate. [I/O: reads]"""
    return settings.server_public_ip in await _resolve(domain, "A")


def get_domain_proof_token(project: Project, domain: str) -> str:
    """[Pure-ish] The token a user's TXT record must hold to prove `domain` is theirs for this project."""
    return devenv.derive_secret(f"project:{project['id']}:site-domain:{domain}", 32).lower()


def normalize_domain(domain: str) -> str:
    """[Pure] A domain the way we keep it: trimmed, lower case, no scheme / path / trailing dot. Raises
    InvalidDomain if what's left isn't a plain host name."""
    host = urlsplit(domain.strip().lower() if "://" in domain else f"//{domain.strip().lower()}").hostname or ""
    normalized = host.rstrip(".")
    if not _DOMAIN_PATTERN.match(normalized):
        raise InvalidDomain(f"not a domain: {domain}")
    return normalized


def get_default_domain(project: Project) -> str | None:
    """[Pure-ish] The subdomain of ours a project's site can always have, <project hash>.<prod_domain> (under our
    wildcard certificate, so nothing to set up for it). None without a prod_domain. Only a suggestion: a site is
    served on whatever's in its domains, this one included only once it's put there."""
    return f"{project['hash']}.{settings.prod_domain}" if settings.prod_domain else None


def to_site_urls(domains: list[str]) -> list[str]:
    """[Pure]"""
    return [f"https://{domain}" for domain in domains]


async def set_domains(project: Project, domains: list[str]) -> list[str]:
    """Admin only: make `domains` (normalized, deduped) exactly the project's, trusted as they are, the first one
    active, and return them. They take effect at the next deployment. Raises InvalidDomain, or DomainTaken when one
    is another project's (then nothing changes)."""
    normalized = pydash.uniq([normalize_domain(domain) for domain in domains])
    await site_domains_repo.replace(project["id"], normalized)
    if normalized:
        await site_domains_repo.activate(project["id"], normalized[0])
    return normalized


async def ensure_site(project: Project, domains: list[str]) -> None:
    """Make sure every one of `domains` (what it's served on, see get_served_domains) has its certificate and routes to its
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


def _is_ours(domain: str) -> bool:
    """[Pure-ish] Whether a domain is one of the platform's own (the app's, the dev / prod sites' -- by their
    registered domain, e.g. anything under biveitapp.com): never a user's to bring."""
    ours = pydash.compact([settings.dev_domain, settings.prod_domain, urlsplit(settings.frontend_url).hostname or ""])
    registered = {".".join(host.split(".")[-2:]) for host in ours if "." in host}
    return any(domain == parent or domain.endswith(f".{parent}") for parent in registered)


async def _resolve(name: str, record_type: str) -> set[str]:
    """[I/O] The values of a DNS name's records of one type (TXT strings unquoted), from public resolvers; empty
    when there are none (or the lookup fails -- to the caller that's "not found yet")."""
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = DOMAIN_CHECK_NAMESERVERS
    try:
        answer = await resolver.resolve(name, record_type, lifetime=DOMAIN_CHECK_TIMEOUT_SECONDS)
    except dns.exception.DNSException:
        return set()
    if record_type == "TXT":
        return {b"".join(record.strings).decode(errors="replace") for record in answer}
    return {record.to_text() for record in answer}
