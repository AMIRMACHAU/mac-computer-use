"""A blocked plan must perform nothing at all.

That is the whole claim plans.py makes. If a plan with a bad step four still
executes step one, the module is worse than useless — it has the appearance of a
safety net and none of the effect.

Element resolution is stubbed here so these run without a GUI and without
touching the machine.
"""

from __future__ import annotations

import pytest

import plans
import targeting


class FakeNode:
    def __init__(self, label="Fake"):
        self.label = label

    def describe(self):
        return f"AXButton {self.label!r}"


@pytest.fixture
def resolves(monkeypatch):
    """Control what resolve() does per label: a node, or an exception."""
    table: dict = {}

    def fake_resolve(app, label=None, role=None, exact=False, actionable_only=True,
                     window=None):
        outcome = table.get(label, FakeNode(label or "?"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(targeting, "resolve", fake_resolve)
    return table


# --------------------------------------------------------------- validation

@pytest.mark.parametrize("step, fragment", [
    ({"action": "nope", "app": "A"}, "action must be one of"),
    ({"action": "press"}, "needs an 'app'"),
    ({"action": "type", "app": "A"}, "needs 'text'"),
    ({"action": "menu", "app": "A"}, "needs a 'path'"),
    ({"action": "open_app"}, "needs an 'app'"),
])
def test_malformed_steps_are_rejected(step, fragment):
    with pytest.raises(plans.PlanError) as e:
        plans.check([step])
    assert fragment in str(e.value)


# ------------------------------------------------------------------ verdicts

def test_resolvable_step_is_resolved(resolves):
    rep = plans.check([{"action": "press", "app": "A", "label": "Save"}])
    assert rep["ready"] is True
    assert rep["results"][0]["verdict"] == "resolved"


def test_open_app_and_wait_for_are_deferred_by_design(resolves):
    rep = plans.check([{"action": "open_app", "app": "A"},
                       {"action": "wait_for", "app": "A", "label": "x"}])
    assert [r["verdict"] for r in rep["results"]] == ["deferred", "deferred"]
    assert rep["ready"] is True, "deferred steps must not block a plan"


def test_explicit_defer_is_honoured(resolves):
    resolves["Later"] = targeting.NotFound("not there yet")
    rep = plans.check([{"action": "press", "app": "A", "label": "Later",
                        "defer": True, "because": "dialog opens first"}])
    assert rep["results"][0]["verdict"] == "deferred"
    assert "dialog opens first" in rep["results"][0]["detail"]
    assert rep["ready"] is True


def test_unresolvable_step_blocks(resolves):
    resolves["Ghost"] = targeting.NotFound("no such element")
    rep = plans.check([{"action": "press", "app": "A", "label": "Ghost"}])
    assert rep["ready"] is False
    assert rep["results"][0]["verdict"] == "blocked"


def test_ambiguity_blocks(resolves):
    resolves["Vague"] = targeting.Ambiguous("8 elements match")
    rep = plans.check([{"action": "press", "app": "A", "label": "Vague"}])
    assert rep["ready"] is False
    assert "Ambiguous" in rep["results"][0]["detail"]


def test_later_steps_are_still_checked_after_an_early_failure(resolves):
    """Report the whole picture, not just the first problem."""
    resolves["Ghost"] = targeting.NotFound("nope")
    rep = plans.check([
        {"action": "press", "app": "A", "label": "Ghost"},
        {"action": "press", "app": "A", "label": "Fine"},
    ])
    assert [r["verdict"] for r in rep["results"]] == ["blocked", "resolved"]


def test_menu_check_resolves_the_leaf(resolves, monkeypatch):
    seen = {}

    def fake_resolve(app, label=None, role=None, **kw):
        seen["label"], seen["role"] = label, role
        return FakeNode(label)

    monkeypatch.setattr(targeting, "resolve", fake_resolve)
    plans.check([{"action": "menu", "app": "A", "path": "Format > Font > Bold"}])
    assert seen == {"label": "Bold", "role": "AXMenuItem"}


# -------------------------------------------------------------------- run

def test_a_blocked_plan_performs_nothing(resolves, monkeypatch):
    """The central claim. Step one must not run when step two cannot."""
    performed = []
    monkeypatch.setattr(targeting, "press",
                        lambda *a, **k: performed.append(a) or {"target": "x"})
    resolves["Ghost"] = targeting.NotFound("nope")

    r = plans.run([
        {"action": "press", "app": "A", "label": "Fine"},
        {"action": "press", "app": "A", "label": "Ghost"},
    ])

    assert r["executed"] is False
    assert r["performed"] == []
    assert performed == [], "a step ran despite the plan being blocked"


def test_require_ready_false_starts_a_known_broken_plan(resolves, monkeypatch):
    """Opting out of the gate means the plan runs and fails where it was going to."""
    def press(app, label=None, *a, **k):
        if label == "Ghost":
            raise targeting.NotFound("still not there at run time")
        return {"target": label}

    monkeypatch.setattr(targeting, "press", press)
    resolves["Ghost"] = targeting.NotFound("nope")
    r = plans.run([{"action": "press", "app": "A", "label": "Ghost"}],
                  require_ready=False)
    assert r["executed"] is True, "require_ready=False should not abort"
    assert r["completed"] is False
    assert r["performed"][0]["ok"] is False


def test_run_stops_at_the_first_failure_and_says_what_is_left(resolves, monkeypatch):
    calls = []

    def flaky_press(app, label=None, *a, **k):
        calls.append(label)
        if label == "Boom":
            raise targeting.Changed("element moved")
        return {"target": label}

    monkeypatch.setattr(targeting, "press", flaky_press)
    r = plans.run([
        {"action": "press", "app": "A", "label": "One"},
        {"action": "press", "app": "A", "label": "Boom"},
        {"action": "press", "app": "A", "label": "Three"},
    ])

    assert calls == ["One", "Boom"], "it kept going after a failure"
    assert r["completed"] is False
    assert r["performed"][0]["ok"] is True
    assert r["performed"][1]["ok"] is False
    assert len(r["remaining"]) == 1 and "Three" in r["remaining"][0]


def test_successful_run_reports_every_step(resolves, monkeypatch):
    monkeypatch.setattr(targeting, "press", lambda *a, **k: {"target": "ok"})
    monkeypatch.setattr(targeting, "press_menu_path",
                        lambda app, path, **k: {"path": path, "route": "direct",
                                                "target": "menu"})
    r = plans.run([
        {"action": "press", "app": "A", "label": "One"},
        {"action": "menu", "app": "A", "path": "File > Save"},
    ])
    assert r["completed"] is True
    assert all(p["ok"] for p in r["performed"])
    assert len(r["performed"]) == 2


def test_format_check_says_nothing_was_performed_when_blocked(resolves):
    resolves["Ghost"] = targeting.NotFound("nope")
    text = plans.format_check(plans.check([{"action": "press", "app": "A",
                                            "label": "Ghost"}]))
    assert "NOT READY" in text
    assert "Nothing was performed" in text
