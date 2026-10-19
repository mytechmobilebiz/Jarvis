"""The two deployment settings a second installation on the same PC needs (JARVIS Brain test copy, spec section 3):
SESSION_COOKIE_NAME and CC_INSTANCE_LABEL. Their defaults must leave the live installation exactly as it was."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "backend" / "app"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("SESSION_COOKIE_NAME", "CC_INSTANCE_LABEL", "INSTANCE_LABEL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_keep_the_live_installation_unchanged(clean_env):
    settings = Settings()
    assert settings.session_cookie_name == "cc_session"
    assert settings.instance_label == ""


def test_both_are_read_from_the_environment(clean_env):
    clean_env.setenv("SESSION_COOKIE_NAME", "cc_session_brain_test")
    clean_env.setenv("CC_INSTANCE_LABEL", "  cc-brain-test ")
    settings = Settings()
    assert settings.session_cookie_name == "cc_session_brain_test"
    assert settings.instance_label == "cc-brain-test"


@pytest.mark.parametrize("name", ["", "cc session", "cc;session", "a=b", 'cc"x', "cc,x", "café", "x" * 65, "cc\tx",
                                  "cc\nSet-Cookie: x"])
def test_an_unsafe_cookie_name_stops_the_application(clean_env, name):
    clean_env.setenv("SESSION_COOKIE_NAME", name)
    with pytest.raises(ValidationError, match="SESSION_COOKIE_NAME"):
        Settings()


@pytest.mark.parametrize("label", ["x" * 65, "test\ncopy", "test\x1bcopy", "café"])
def test_an_unprintable_or_long_instance_label_is_refused(clean_env, label):
    clean_env.setenv("CC_INSTANCE_LABEL", label)
    with pytest.raises(ValidationError, match="CC_INSTANCE_LABEL"):
        Settings()


def test_compose_passes_both_with_defaults_that_change_nothing():
    env = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["x-api-env"]
    assert env["SESSION_COOKIE_NAME"] == "${SESSION_COOKIE_NAME:-cc_session}"
    assert env["CC_INSTANCE_LABEL"] == "${CC_INSTANCE_LABEL:-}"


def test_no_code_reads_the_session_cookie_by_a_fixed_name():
    """Every reader and writer of the cookie goes through settings.session_cookie_name."""
    offenders = []
    for path in APP.rglob("*.py"):
        text = path.read_text()
        if path.name != "config.py" and "cc_session" in text:
            offenders.append(f"{path.relative_to(APP)}: names cc_session")
        for line in text.splitlines():
            if re.search(r"(set_cookie|delete_cookie|cookies\.get)\(\s*[\"']", line):
                offenders.append(f"{path.relative_to(APP)}: {line.strip()}")
    assert offenders == []
