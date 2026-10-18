from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.licensing import EDITIONS, Entitlements, LicenseError, sign_license, verify_license


def keypair():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return private, public


def payload(**overrides):
    base = {
        "license_id": "LIC-0001",
        "customer": "Acme Agency LLC",
        "edition": "professional",
        "issued_at": "2026-10-18",
        "expires_at": (datetime.now(UTC) + timedelta(days=365)).date().isoformat(),
        "max_sites": 50,
        "max_users": 5,
    }
    return {**base, **overrides}


def test_valid_license_verifies():
    private, public = keypair()
    lic = verify_license(sign_license(payload(), private), public)
    assert lic.edition == "professional" and lic.customer == "Acme Agency LLC" and not lic.expired


def test_tampered_license_is_rejected():
    private, public = keypair()
    doc = sign_license(payload(), private)
    doc["payload"]["edition"] = "agency"
    with pytest.raises(LicenseError, match="signature"):
        verify_license(doc, public)


def test_license_from_another_key_is_rejected():
    private, _ = keypair()
    _, other_public = keypair()
    with pytest.raises(LicenseError):
        verify_license(sign_license(payload(), private), other_public)


def test_no_public_key_means_no_licenses():
    private, _ = keypair()
    with pytest.raises(LicenseError, match="public key"):
        verify_license(sign_license(payload(), private), "")


@pytest.mark.parametrize("edition", ["developer", "enterprise-max", ""])
def test_unsellable_or_unknown_editions_are_rejected(edition):
    private, public = keypair()
    with pytest.raises(LicenseError, match="edition"):
        verify_license(sign_license(payload(edition=edition), private), public)


def test_expiry_is_detected():
    private, public = keypair()
    lic = verify_license(sign_license(payload(expires_at="2020-01-01"), private), public)
    assert lic.expired


def test_garbage_is_not_a_license():
    _, public = keypair()
    with pytest.raises(LicenseError):
        verify_license({"nope": 1}, public)


def test_edition_entitlements():
    personal = Entitlements(EDITIONS["personal"], "configuration")
    assert personal.allows_agent("wordpress_monitor") and not personal.allows_agent("credit_repair")
    assert personal.has("ai_llm") and not personal.has("multi_user")
    assert personal.max_sites == 5 and personal.max_users == 1
    agency = Entitlements(EDITIONS["agency"], "configuration")
    assert agency.allows_agent("credit_repair") and agency.max_sites is None and agency.has("white_label")
    assert not EDITIONS["developer"].sellable
