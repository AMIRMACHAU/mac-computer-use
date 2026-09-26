"""Matching and refusal logic, without needing a screen.

The parts of targeting.py that decide *whether* to act are pure and testable.
The parts that actually act need a real application, and are marked `gui` so a
machine without one (or without Accessibility permission) skips them instead of
reporting a failure it cannot fix.

    pytest                  # logic only
    pytest -m gui           # drive a real app as well
"""

from __future__ import annotations

import pytest

import targeting
from targeting import Node


def node(role="AXButton", label="Save", value="", actions=("AXPress",), path=(0,)):
    return Node(role=role, label=label, value=value, actions=list(actions), path=path)


# ----------------------------------------------------------------- matching

def test_substring_match_is_the_default():
    assert targeting._matches(node(label="Save As…"), "save", None, exact=False)


def test_exact_match_requires_the_whole_label():
    n = node(label="Save As…")
    assert not targeting._matches(n, "Save", None, exact=True)
    assert targeting._matches(n, "Save As…", None, exact=True)


def test_matching_is_case_insensitive():
    assert targeting._matches(node(label="SAVE"), "save", None, False)
    assert targeting._matches(node(label="save"), "SAVE", None, False)


def test_value_is_searched_as_well_as_label():
    """A text area's content is often the only thing identifying it."""
    n = node(role="AXTextArea", label="", value="hello world", actions=())
    assert targeting._matches(n, "hello", None, False)


def test_role_filter_accepts_both_spellings():
    n = node(role="AXRadioButton")
    assert targeting._matches(n, None, "AXRadioButton", False)
    assert targeting._matches(n, None, "RadioButton", False), "bare role should work too"
    assert not targeting._matches(n, None, "AXButton", False)


def test_no_label_matches_anything_of_that_role():
    assert targeting._matches(node(), None, "AXButton", False)


# ----------------------------------------------------------------- Node

def test_actionable_means_it_can_be_pressed():
    assert node(actions=["AXPress"]).actionable
    assert not node(actions=["AXShowMenu"]).actionable
    assert not node(actions=[]).actionable


def test_identity_covers_role_label_and_position():
    """Identity is what must hold between resolution and dispatch."""
    a = node(label="Save", path=(0, 1))
    assert a.identity() == ("AXButton", "Save", (0, 1))
    assert a.identity() != node(label="Save As…", path=(0, 1)).identity()
    assert a.identity() != node(label="Save", path=(0, 2)).identity()


def test_describe_includes_the_value_when_there_is_one():
    assert "hello" in node(value="hello").describe()
    assert "value=" not in node(value="").describe()


# ----------------------------------------------------------------- refusals

def test_every_refusal_is_a_target_error():
    """Callers catch TargetError; a refusal outside that hierarchy escapes."""
    for cls in (targeting.NotFound, targeting.Ambiguous,
                targeting.NotActionable, targeting.Changed):
        assert issubclass(cls, targeting.TargetError)


def test_unknown_app_names_the_running_ones():
    with pytest.raises(targeting.NotFound) as e:
        targeting._app_element("Definitely Not A Real Application")
    assert "Running:" in str(e.value), "a refusal should say what the options were"


# ----------------------------------------------------------------- with a GUI

pytestmark_gui = pytest.mark.gui


@pytest.fixture(scope="module")
def finder():
    if not targeting.AXIsProcessTrusted():
        pytest.skip("Accessibility permission not granted to this process")
    try:
        targeting._app_element("Finder")
    except targeting.NotFound:
        pytest.skip("Finder is not running")
    return "Finder"


@pytest.mark.gui
def test_walk_reaches_window_contents_not_just_menus(finder):
    """The bug this guards: AXChildren holds the menu bar, AXWindows the windows.

    Walking only AXChildren returned hundreds of menu items and zero controls.
    """
    nodes = targeting.walk(finder)
    menu_roles = {"AXMenuItem", "AXMenu", "AXMenuBarItem", "AXMenuBar"}
    assert nodes, "walk returned nothing"
    if targeting.windows(finder):
        assert any(n.role not in menu_roles for n in nodes), (
            "only menu elements were found despite an open window — "
            "AXWindows is probably not being traversed"
        )


@pytest.mark.gui
def test_resolve_refuses_rather_than_guessing(finder):
    with pytest.raises(targeting.NotFound):
        targeting.resolve(finder, "a-label-that-cannot-possibly-exist-xyzzy")


@pytest.mark.gui
def test_bad_window_scope_lists_the_real_windows(finder):
    with pytest.raises(targeting.TargetError) as e:
        targeting.walk(finder, window="no-such-window-xyzzy")
    assert "Open windows:" in str(e.value)


@pytest.mark.gui
def test_windows_returns_titles(finder):
    assert isinstance(targeting.windows(finder), list)


@pytest.mark.gui
def test_frontmost_app_is_a_name():
    assert isinstance(targeting.frontmost_app(), str)
