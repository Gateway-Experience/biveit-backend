# Shared fixtures for every test.

import pytest

from src.config import Settings, settings

# Settings that differ per machine (.env.local sets a developer's own) but that tests assert on: pinned to their code
# defaults, so the suite gives the same answers whatever's in the local env file. A test wanting another value
# monkeypatches it on top.
_PINNED_SETTINGS = ["dev_backend_port_base", "dev_frontend_port_base", "site_port_base", "project_port_slots"]


@pytest.fixture(autouse=True)
def _pin_machine_settings(monkeypatch):
    for name in _PINNED_SETTINGS:
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)
