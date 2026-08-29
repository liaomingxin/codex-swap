"""Account store: slots, upsert, remove, unclaimed stash."""

from __future__ import annotations

import json

from conftest import make_auth_json

from codex_swap.exceptions import StoreError
from codex_swap.identity import identity_from_auth
from codex_swap.store import AccountStore


def _ident(email: str, account_id: str, refresh: str | None = None):
    return identity_from_auth(
        json.loads(
            make_auth_json(
                email=email, account_id=account_id, refresh_token=refresh or f"rt.1.{email}"
            )
        )
    )


def test_first_add_takes_slot_one():
    store = AccountStore()
    entry, created = store.upsert(_ident("a@x.io", "acc-a"))
    assert created and entry.number == 1
    store.write_credential(entry, make_auth_json(email="a@x.io", account_id="acc-a"))
    assert store.read_credential(entry).startswith("{")


def test_slots_increment_and_survive_remove():
    store = AccountStore()
    e1, _ = store.upsert(_ident("a@x.io", "acc-a"))
    e2, _ = store.upsert(_ident("b@x.io", "acc-b"))
    e3, _ = store.upsert(_ident("c@x.io", "acc-c"))
    assert (e1.number, e2.number, e3.number) == (1, 2, 3)
    store.remove(2)
    numbers = [e.number for e in store.list_entries()]
    assert numbers == [1, 3]  # stable, no renumbering
    e4, _ = store.upsert(_ident("d@x.io", "acc-d"))
    assert e4.number == 4  # max+1, gaps not filled


def test_readd_refreshes_in_place():
    store = AccountStore()
    store.upsert(_ident("a@x.io", "acc-a", refresh="rt.1.old"))
    entry, created = store.upsert(_ident("a@x.io", "acc-a", refresh="rt.1.rotated"))
    assert not created and entry.number == 1
    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].refresh_fingerprint == entry.refresh_fingerprint


def test_find_by_identity_falls_back_to_account_id():
    store = AccountStore()
    store.upsert(_ident("a@x.io", "acc-a", refresh="rt.1.old"))
    # rotated refresh token the slot never saw: account_id must still match
    hit = store.find_by_identity(_ident("a@x.io", "acc-a", refresh="rt.1.brandnew"))
    assert hit is not None and hit.number == 1


def test_remove_missing_slot_raises():
    try:
        AccountStore().remove(7)
        raise AssertionError("expected StoreError")
    except StoreError:
        pass


def test_stash_unclaimed_roundtrip(tmp_path):
    store = AccountStore()
    path = store.stash_unclaimed("{}", "sha256:abcdef0123456789")
    assert path.exists() and path.parent.name == "unclaimed"
    assert "sha256:abcdef01" in path.name
