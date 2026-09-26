"""The memory store must survive being wrong, empty, or corrupt.

A store that raises on a bad file is worse than no store: it takes the whole
server down on startup, for a feature that is only ever advisory.
"""

from __future__ import annotations

import json

import pytest

import envmemory


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never touch the real store at ~/.mac-computer-use."""
    monkeypatch.setattr(envmemory, "STORE", tmp_path / "knowledge.json")
    yield


def test_seeding_is_idempotent():
    first = envmemory.ensure_seeded()
    assert first == len(envmemory.SEED)
    assert envmemory.ensure_seeded() == 0, "re-seeding must not duplicate facts"
    assert len(envmemory.recall("", limit=99)) == len(envmemory.SEED)


def test_remember_then_recall():
    envmemory.remember("k", "the fact", ["alpha", "beta"])
    assert [f["key"] for f in envmemory.recall("alpha")] == ["k"]
    assert [f["key"] for f in envmemory.recall("beta")] == ["k"]


def test_remember_reports_what_it_replaced():
    envmemory.remember("k", "first", ["t"])
    r = envmemory.remember("k", "second", ["t"])
    assert r["action"] == "updated"
    assert r["previous"] == "first"
    assert envmemory.recall("t")[0]["fact"] == "second"


def test_tag_matches_rank_above_text_matches():
    envmemory.remember("tagged", "irrelevant body", ["browser"])
    envmemory.remember("texty", "something about a browser here", ["other"])
    order = [f["key"] for f in envmemory.recall("browser")]
    assert order[0] == "tagged", f"tag hit should outrank a body mention, got {order}"


def test_empty_query_returns_everything():
    for i in range(4):
        envmemory.remember(f"k{i}", f"fact {i}", ["x"])
    assert len(envmemory.recall("", limit=99)) == 4


def test_short_words_do_not_match_everything():
    """A two-letter query must not drag in every fact containing those letters."""
    envmemory.remember("a", "the quick brown fox", ["animals"])
    envmemory.remember("b", "unrelated entirely", ["other"])
    assert envmemory.recall("qu") == []


def test_forget():
    envmemory.remember("gone", "wrong fact", ["t"])
    assert envmemory.forget("gone") is True
    assert envmemory.recall("t") == []
    assert envmemory.forget("gone") is False, "forgetting twice should report False"


def test_corrupt_store_does_not_raise():
    envmemory.STORE.parent.mkdir(parents=True, exist_ok=True)
    envmemory.STORE.write_text("{ this is not json")
    assert envmemory.recall("anything") == []
    envmemory.remember("k", "still works", ["t"])
    assert envmemory.recall("t")[0]["fact"] == "still works"


def test_missing_store_does_not_raise():
    assert not envmemory.STORE.exists()
    assert envmemory.recall("anything") == []


def test_writes_are_atomic_and_valid_json():
    envmemory.remember("k", "v", ["t"])
    data = json.loads(envmemory.STORE.read_text())
    assert "facts" in data and "k" in data["facts"]
    assert not list(envmemory.STORE.parent.glob("*.tmp")), "temp file left behind"


def test_seed_facts_carry_no_personal_data():
    """The shipped seeds are general macOS facts, never one machine's specifics.

    A real contact's name and a specific machine's iCloud state were in this list
    once, and would have been published.
    """
    blob = " ".join(s["fact"] + s["key"] for s in envmemory.SEED).lower()
    for leak in ("/users/", "lalit", "icloud is full", "playwright-brave", "@"):
        assert leak not in blob, f"seed facts contain {leak!r}, which should stay local"
