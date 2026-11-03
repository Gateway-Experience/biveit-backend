# Subdomain tests (services/launch/subdomains + utils/claude_cli): which slugs can be a subdomain of ours, how a
# name is suggested (Claude's pick, numbered when taken, kept in meta, the project's name when Claude can't answer),
# and picking one -- attaching it, replacing the old one, and the refusals. Claude, site_domains and the projects
# table are faked.

import asyncio
from datetime import UTC, datetime

import pytest

from src.config import settings
from src.modules import deployments
from src.repositories import projects as projects_repo
from src.services.launch import subdomains
from src.services.launch.checkouts import LaunchRefused
from src.utils import claude_cli
from src.utils.processes import CommandFailed

_NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
_PROJECT = {
    "id": 42, "user_id": 7, "name": "Hanoi Bike Rental", "description": "Bikes in Tay Ho", "bootstrap_data": {},
    "meta": {}, "build_approved_at": _NOW,
}  # fmt: skip


@pytest.fixture(autouse=True)
def _prod_domain(monkeypatch):
    monkeypatch.setattr(settings, "prod_domain", "biveitapp.com")


@pytest.fixture
def site_domains(monkeypatch):
    """A tiny site_domains table: `rows` is every project's, as {project_id, domain, kind, activated_at}."""
    state = {"rows": [], "attached": [], "detached": [], "fulfilled": []}

    async def fake_find_taken(project, domains):
        return {
            row["domain"] for row in state["rows"] if row["domain"] in domains and row["project_id"] != project["id"]
        }

    async def fake_get_site_domains(project):
        return [row for row in state["rows"] if row["project_id"] == project["id"]]

    async def fake_attach(project, domain, kind):
        if await fake_find_taken(project, [domain]):
            raise deployments.DomainTaken(domain)
        state["attached"].append((domain, kind))

    async def fake_detach(project, domain):
        state["detached"].append(domain)

    async def fake_fulfil(project_id):
        state["fulfilled"].append(project_id)

    monkeypatch.setattr(deployments, "find_taken_domains", fake_find_taken)
    monkeypatch.setattr(deployments, "get_site_domains", fake_get_site_domains)
    monkeypatch.setattr(deployments, "attach_domain", fake_attach)
    monkeypatch.setattr(deployments, "detach_domain", fake_detach)
    monkeypatch.setattr(subdomains.fulfilment, "fulfil", fake_fulfil)
    return state


@pytest.fixture
def saved_meta(monkeypatch):
    updates = []

    async def fake_update(project_id, data):
        updates.append((project_id, data))
        return {**_PROJECT, **data}

    monkeypatch.setattr(projects_repo, "update", fake_update)
    return updates


def _answer_with(monkeypatch, answer):
    prompts = []

    async def fake_ask(prompt, model="haiku"):
        prompts.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(subdomains, "ask_claude", fake_ask)
    return prompts


def _taken(project_id, slug, activated_at=None):
    return {
        "project_id": project_id,
        "domain": f"{slug}.biveitapp.com",
        "kind": "subdomain",
        "activated_at": activated_at,
    }


# --- which slugs ---


@pytest.mark.parametrize(
    ("slug", "refusal"),
    [
        ("bike-rental", None),
        ("bike2", None),
        ("ab", "SUBDOMAIN_INVALID"),
        ("-bike", "SUBDOMAIN_INVALID"),
        ("bike-", "SUBDOMAIN_INVALID"),
        ("bike.rental", "SUBDOMAIN_INVALID"),
        ("bike_rental", "SUBDOMAIN_INVALID"),
        ("xe-đạp", "SUBDOMAIN_INVALID"),
        ("a" * 64, "SUBDOMAIN_INVALID"),
        ("dev", "SUBDOMAIN_RESERVED"),
        ("www", "SUBDOMAIN_RESERVED"),
    ],
)
def test_which_slugs_can_be_a_subdomain(slug, refusal):
    assert subdomains.to_slug_refusal(slug) == refusal


# --- the suggestion ---


async def test_a_suggestion_is_claudes_pick_saved_in_meta(monkeypatch, site_domains, saved_meta):
    prompts = _answer_with(monkeypatch, "bike-rental")

    assert await subdomains.get_suggestion(_PROJECT) == "bike-rental"
    assert saved_meta == [(42, {"meta": {"subdomain_suggestion": "bike-rental"}})]
    assert "Hanoi Bike Rental" in prompts[0]
    # Only a suggestion: nothing's attached (or set going) until the user picks it.
    assert site_domains["attached"] == []
    assert site_domains["fulfilled"] == []


async def test_a_taken_pick_gets_the_first_free_number(monkeypatch, site_domains, saved_meta):
    _answer_with(monkeypatch, "bike-rental")
    site_domains["rows"] = [_taken(1, "bike-rental"), _taken(2, "bike-rental-2")]

    assert await subdomains.get_suggestion(_PROJECT) == "bike-rental-3"


async def test_a_saved_suggestion_is_offered_again_without_asking(monkeypatch, site_domains, saved_meta):
    prompts = _answer_with(monkeypatch, "something-else")

    assert await subdomains.get_suggestion({**_PROJECT, "meta": {"subdomain_suggestion": "bikes"}}) == "bikes"
    assert prompts == []
    assert saved_meta == []


async def test_a_saved_suggestion_taken_since_is_replaced(monkeypatch, site_domains, saved_meta):
    _answer_with(monkeypatch, "bikes")
    site_domains["rows"] = [_taken(1, "bikes")]

    assert await subdomains.get_suggestion({**_PROJECT, "meta": {"subdomain_suggestion": "bikes"}}) == "bikes-2"


@pytest.mark.parametrize(
    "answer",
    [CommandFailed("claude exited 1"), "", "Sure! Here are some ideas: ..., ..."],
)
async def test_without_a_usable_answer_the_name_is_used(monkeypatch, site_domains, saved_meta, answer):
    _answer_with(monkeypatch, answer)

    assert await subdomains.get_suggestion(_PROJECT) == "hanoi-bike"


@pytest.mark.parametrize("answer", ["Here you go:\nBike Rental", '"bike-rental".', "bike rental"])
async def test_a_word_or_two_on_the_last_line_is_taken(monkeypatch, site_domains, saved_meta, answer):
    _answer_with(monkeypatch, answer)

    assert await subdomains.get_suggestion(_PROJECT) == "bike-rental"


async def test_a_reserved_pick_is_numbered(monkeypatch, site_domains, saved_meta):
    _answer_with(monkeypatch, "shop")

    assert await subdomains.get_suggestion(_PROJECT) == "shop-2"


async def test_no_suggestion_without_a_prod_domain(monkeypatch, site_domains, saved_meta):
    monkeypatch.setattr(settings, "prod_domain", "")

    with pytest.raises(LaunchRefused, match="SUBDOMAINS_UNAVAILABLE"):
        await subdomains.get_suggestion(_PROJECT)


# --- picking one ---


async def test_picking_attaches_it_and_moves_the_launch_on(site_domains):
    assert await subdomains.choose_subdomain(_PROJECT, " Bike-Rental ") == "bike-rental.biveitapp.com"
    assert site_domains["attached"] == [("bike-rental.biveitapp.com", deployments.SiteDomainKind.SUBDOMAIN)]
    assert site_domains["detached"] == []
    await asyncio.sleep(0)  # fulfilment is spawned: let it start
    assert site_domains["fulfilled"] == [42]


async def test_picking_another_replaces_the_old_one(site_domains):
    site_domains["rows"] = [_taken(42, "bikes")]

    await subdomains.choose_subdomain(_PROJECT, "bike-rental")

    assert site_domains["attached"] == [("bike-rental.biveitapp.com", deployments.SiteDomainKind.SUBDOMAIN)]
    assert site_domains["detached"] == ["bikes.biveitapp.com"]


async def test_picking_the_same_one_again_changes_nothing(site_domains):
    site_domains["rows"] = [_taken(42, "bikes")]

    assert await subdomains.choose_subdomain(_PROJECT, "bikes") == "bikes.biveitapp.com"
    assert site_domains["attached"] == []
    assert site_domains["detached"] == []


@pytest.mark.parametrize(
    ("project", "slug", "rows", "code"),
    [
        ({**_PROJECT, "build_approved_at": None}, "bikes", [], "NOT_APPROVED"),
        (_PROJECT, "b", [], "SUBDOMAIN_INVALID"),
        (_PROJECT, "admin", [], "SUBDOMAIN_RESERVED"),
        (_PROJECT, "bikes", [_taken(1, "bikes")], "SUBDOMAIN_TAKEN"),
        # The site is served on its subdomain: renaming it would take the site down.
        (_PROJECT, "bike-rental", [_taken(42, "bikes", activated_at=_NOW)], "SUBDOMAIN_IN_USE"),
    ],
)
async def test_picking_is_refused(site_domains, project, slug, rows, code):
    site_domains["rows"] = rows

    with pytest.raises(LaunchRefused, match=code):
        await subdomains.choose_subdomain(project, slug)
    assert site_domains["detached"] == []


# --- asking Claude ---


async def test_claude_runs_with_no_tools_and_the_agents_endpoint(monkeypatch):
    calls = []

    async def fake_run(argv, cwd=None, timeout_seconds=600, env=None, input_text=None):
        calls.append({"argv": argv, "env": env, "input_text": input_text})
        return b"bike-rental\n"

    monkeypatch.setattr(claude_cli, "run_command_binary", fake_run)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.deepseek.com/anthropic")

    assert await claude_cli.ask_claude("name this site") == "bike-rental"
    argv, env = calls[0]["argv"], calls[0]["env"]
    # Untrusted text goes into the prompt: no tools, no settings, no MCP servers.
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert calls[0]["input_text"] == "name this site"
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
