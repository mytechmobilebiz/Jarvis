from app.cli import config_problems, main
from app.config import Settings

GOOD = {
    "admin_email": "owner@example.org",
    "admin_password": "a-long-admin-pass",
    "CC_SERVICE_TOKEN": "t" * 40,
    "n8n_webhook_secret": "w" * 40,
}


def test_valid_config_has_no_errors():
    errors, warnings = config_problems(Settings(**GOOD))
    assert errors == []
    assert any("ANTHROPIC_API_KEY" in w for w in warnings)


def test_problems_are_reported():
    errors, _ = config_problems(
        Settings(admin_email="nope", admin_password="short", CC_SERVICE_TOKEN="REPLACE_WITH_LONG_RANDOM_SECRET_VALUE", n8n_webhook_secret="")
    )
    text = " ".join(errors)
    assert "ADMIN_EMAIL" in text and "ADMIN_PASSWORD" in text
    assert "placeholder" in text and "N8N_WEBHOOK_SECRET is not set" in text


def test_identical_secrets_rejected():
    errors, _ = config_problems(Settings(**{**GOOD, "n8n_webhook_secret": "t" * 40}))
    assert any("must be different" in e for e in errors)


def test_main_exit_codes(capsys):
    assert main(["cli", "help"]) == 0
    assert main(["cli", "bogus"]) == 2
