"""Window capture + text targeting: the rules that keep a click from landing wrong.

Written against what actually went wrong: a "screenshot of the app" that was a
screenshot of the Claude window, a sidebar item that exact-matching missed because
recognition read its folder icon as "*", and a window captured mid-animation
whose every click was then refused. The window server and text recognition are
stubbed, so these run anywhere and touch nothing.
"""

from __future__ import annotations

import pytest

import sight
from targeting import TargetError

W = sight.Window(id=7, app="Finder", title="home", x=100, y=100, w=400, h=300)


def line(text, box=(0, 0, 10, 10)):
    return (text, 0.9, None, box)


# ------------------------------------------------------------------ matching

@pytest.mark.parametrize("raw, want", [
    ("* Applications", "applications"),     # icon glyph read as punctuation
    ("  Set  up later ", "set up later"),
    ("Save As…", "save as"),
    (">", ">"),                              # all punctuation: kept, not emptied
])
def test_norm_strips_icon_glyphs_but_never_empties(raw, want):
    assert sight._norm(raw) == want


def test_exact_matches_through_an_icon_glyph():
    hits = sight.match_lines([line("* Applications"), line("Applications Support")],
                             "Applications", exact=True)
    assert [h.text for h in hits] == ["* Applications"]


def test_substring_match_finds_every_occurrence_in_reading_order():
    lines = [line("Save As…", (50, 40, 90, 50)), line("Save", (10, 40, 30, 50)),
             line("Cancel", (0, 0, 10, 10))]
    assert [h.text for h in sight.match_lines(lines, "save")] == ["Save", "Save As…"]


def test_vision_box_flips_origin_to_top_left():
    class BB:                                  # Vision: normalised, origin bottom-left
        class origin: x, y = 0.25, 0.75
        class size: width, height = 0.5, 0.25
    assert sight._vision_box(BB, 400, 200) == (100, 0, 300, 50)


# ------------------------------------------------------------- refusing to click

def test_refuses_when_window_closed(monkeypatch):
    monkeypatch.setattr(sight, "_window_now", lambda _id: None)
    with pytest.raises(TargetError, match="closed"):
        sight._click_global("Finder", W, 150, 150)


def test_refuses_when_window_moved(monkeypatch):
    moved = sight.Window(**{**W.__dict__, "x": 101})
    monkeypatch.setattr(sight, "_window_now", lambda _id: moved)
    with pytest.raises(TargetError, match="moved"):
        sight._click_global("Finder", W, 150, 150)


def test_refuses_point_outside_window(monkeypatch):
    monkeypatch.setattr(sight, "_window_now", lambda _id: W)
    with pytest.raises(TargetError, match="outside"):
        sight._click_global("Finder", W, 50, 50)


def test_prefers_accessibility_and_never_touches_the_mouse(monkeypatch):
    monkeypatch.setattr(sight, "_window_now", lambda _id: W)
    monkeypatch.setattr(sight, "_ax_click", lambda *a: {"method": "accessibility",
                                                         "action": "AXOpen", "element": "AXCell"})
    monkeypatch.setattr(sight, "frontmost_app", lambda: "Claude")
    import pyautogui
    monkeypatch.setattr(pyautogui, "click", lambda *a, **k: pytest.fail("mouse used"))
    r = sight._click_global("Finder", W, 150, 150)
    assert r["method"] == "accessibility" and r["frontmost"] == "Claude"


def test_refuses_mouse_click_on_a_covered_spot(monkeypatch):
    # The failure this module exists for: Claude's window is on top of the target.
    monkeypatch.setattr(sight, "_window_now", lambda _id: W)
    monkeypatch.setattr(sight, "_ax_click", lambda *a: None)
    monkeypatch.setattr(sight, "ensure_front", lambda app: False)
    monkeypatch.setattr(sight, "window_at", lambda x, y: 99)      # someone else's window
    monkeypatch.setattr(sight, "frontmost_app", lambda: "Claude")
    import pyautogui
    monkeypatch.setattr(pyautogui, "click", lambda *a, **k: pytest.fail("clicked a covered spot"))
    with pytest.raises(TargetError, match="covered"):
        sight._click_global("Finder", W, 150, 150)


def test_mouse_click_allowed_where_window_is_uncovered(monkeypatch):
    monkeypatch.setattr(sight, "_window_now", lambda _id: W)
    monkeypatch.setattr(sight, "_ax_click", lambda *a: None)
    monkeypatch.setattr(sight, "ensure_front", lambda app: False)
    monkeypatch.setattr(sight, "window_at", lambda x, y: W.id)
    monkeypatch.setattr(sight, "frontmost_app", lambda: "Finder")
    clicked = []
    import pyautogui
    monkeypatch.setattr(pyautogui, "click", lambda x, y, **k: clicked.append((x, y)))
    r = sight._click_global("Finder", W, 150, 160)
    assert clicked == [(150, 160)] and r["method"] == "mouse"


def test_click_in_window_needs_a_screenshot_first(monkeypatch):
    monkeypatch.setattr(sight, "_last", {})
    with pytest.raises(TargetError, match="screenshot_app first"):
        sight.click_in_window("Finder", 10, 10)


# ------------------------------------------------------------------- real screen

@pytest.mark.gui
def test_captures_a_window_that_is_not_in_front():
    try:
        wins = sight.app_windows("Finder")
    except Exception as e:                      # no permission / no window server
        pytest.skip(str(e))
    if not wins:
        pytest.skip("no Finder window open")
    cap = sight.capture("Finder")
    assert cap.image.width == pytest.approx(cap.window.w * cap.px_per_pt)
    assert sight.read_text(cap), "a Finder window always shows some text"
