"""JWT/account identity parsing."""

from __future__ import annotations

import time

from codex_swap.identity import identity_from_auth, jwt_payload

from conftest import make_auth_json


def _auth(text: str):
    import json

    return json.loads(text)


def test_parses_real_shaped_claims():
    identity = identity_from_auth(_auth(make_auth_json(email="a@b.c", account_id="acc-9", plan="plus")))
    assert identity is not None
    assert identity.email == "a@b.c"
    assert identity.account_id == "acc-9"
    assert identity.plan_type == "plus"
    assert identity.subscription_active_until == "2099-01-01"
    assert identity.display_name == "Test User"
    assert not identity.access_expired
    assert identity.refresh_fingerprint.startswith("sha256:")


def test_access_expired_flag():
    auth = _auth(make_auth_json(access_exp=time.time() - 60))
    identity = identity_from_auth(auth)
    assert identity is not None
    assert identity.access_expired


def test_malformed_tokens_rejected():
    assert jwt_payload("garbage") is None
    assert jwt_payload("a.b") is None
    assert jwt_payload(None) is None


def test_unidentifiable_file_is_none():
    assert identity_from_auth({"tokens": {"id_token": "x.y.z", "access_token": "x.y.z"}}) is None
    assert identity_from_auth({}) is None
    assert identity_from_auth({"tokens": "nope"}) is None


def test_account_id_from_jwt_when_field_missing():
    auth = _auth(make_auth_json(account_id="acc-77"))
    del auth["tokens"]["account_id"]  # rely on the JWT claim instead
    identity = identity_from_auth(auth)
    assert identity is not None
    assert identity.account_id == "acc-77"


def test_refresh_fingerprint_stable_across_access_rotation():
    a = identity_from_auth(_auth(make_auth_json()))
    b = identity_from_auth(_auth(make_auth_json(access_exp=time.time() + 999)))
    assert a.refresh_fingerprint == b.refresh_fingerprint
