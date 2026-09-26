"""What this machine taught us, so the next agent doesn't relearn it.

Every environment has facts that are invisible in code and expensive to
discover. On one machine `cmd+space` opened Mission Control rather than
Spotlight, so an agent that "knew" the standard shortcut opened the wrong thing
and then typed into it. That cost about forty minutes to work out once. Without
somewhere to put it, it costs forty minutes again in the next session, and the
one after.

This is that somewhere. Facts are short, imperative, and tagged by what they
apply to, so an agent can ask "what do I need to know before driving this app?"
and get an answer before acting rather than after failing.

The seeds below are deliberately general — things true of macOS and of this
tool, not of any one person's setup. Everything specific to your machine gets
added by you (or by the agent, via remember_environment) and stays in your local
store. Nothing in that store is ever published.

Stored under ~/.mac-computer-use, outside any synced folder: a store that blocks
on a cloud fetch is worse than no store at all.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STORE = Path(os.environ.get("MCU_MEMORY",
                            Path.home() / ".mac-computer-use" / "knowledge.json"))

# Seeded from sessions on 2026-09-21/22. Each one cost real time to discover.
SEED: list[dict] = [
    {"key": "ax-windows-not-in-children",
     "tags": ["accessibility", "targeting"],
     "fact": "An application AX element lists its menu bar under AXChildren but its "
             "windows only under AXWindows. Walking AXChildren alone yields hundreds of "
             "menu items and zero buttons."},
    {"key": "app-running-without-window",
     "tags": ["accessibility", "targeting"],
     "fact": "An app can be running with AXWindows empty (no open window) — its tree "
             "then contains only the menu bar. Check for a window before concluding a "
             "control is missing."},
    {"key": "window-titles-are-dynamic",
     "tags": ["targeting", "window", "macos"],
     "fact": "Window titles change while you work — a Notes window went 'Notes - 1 note' "
             "to 'Notes - 2 notes' the moment a note was added, breaking a window= scope "
             "captured moments earlier. Scope with a STABLE substring, never the full title."},
    {"key": "nsworkspace-launch-is-a-noop",
     "tags": ["macos", "launch", "targeting"],
     "fact": "NSWorkspace.launchApplication_ is deprecated and on recent macOS returns "
             "True while doing nothing — a silent no-op that looks like 'the app has no "
             "window'. Use /usr/bin/open -a instead."},
    {"key": "verify-by-polling-not-sleeping",
     "tags": ["verification", "testing"],
     "fact": "Do not verify an action by sleeping a guessed interval then checking once. "
             "Saves and UI updates are asynchronous; a fixed pause reports false mismatches "
             "on work that actually succeeded. Poll the ground truth until it matches or "
             "times out."},
    {"key": "claude-app-holds-the-front",
     "tags": ["focus", "screenshot", "click", "macos"],
     "fact": "While the Claude desktop app is running a tool call, macOS will not let any "
             "other app come to the front — open -a, activateWithOptions, osascript "
             "activate and AXRaise all lose. A full-screen screenshot then shows Claude, and "
             "a coordinate click lands in Claude. Use screenshot_app (captures one window "
             "even when covered) and click_text (accessibility action, no focus needed)."},
    {"key": "ax-action-return-codes-lie",
     "tags": ["accessibility", "verification"],
     "fact": "AXUIElementPerformAction can return an error and still work: Finder returns "
             "kAXErrorCannotComplete (-25205) for an AXOpen on a sidebar item that did "
             "navigate. Judge success by re-reading the window, not by the return code."},
    {"key": "capture-after-window-settles",
     "tags": ["screenshot", "window"],
     "fact": "A window captured while it is still animating open has bounds that are "
             "stale a moment later, so every click computed from it is refused as "
             "'moved'. Wait for two equal bounds reads before capturing."},
    {"key": "chromium-accessibility-is-lazy",
     "tags": ["accessibility", "web", "electron", "click"],
     "fact": "Chromium and Electron windows expose only anonymous AXGroups until an "
             "assistive tool asks. Setting AXManualAccessibility (and "
             "AXEnhancedUserInterface) on the app turns the real tree on — buttons and "
             "links with AXPress — about 1.5s later, although both calls return errors. "
             "click_text does this automatically, once per process."},
    {"key": "overlays-fake-covering",
     "tags": ["click", "window", "overlay"],
     "fact": "Transparent full-screen overlay windows (e.g. Cua Driver's) sit on top of "
             "everything in the window stacking order but let clicks through. Decide "
             "whether a click reaches a window with the system-wide accessibility hit "
             "test (AXUIElementCopyElementAtPosition on AXUIElementCreateSystemWide), "
             "not by stacking order."},
    {"key": "other-space-windows-uncapturable",
     "tags": ["screenshot", "window", "spaces"],
     "fact": "A window in another desktop Space (or a full-screen app's Space) is reported "
             "off-screen and screencapture -l refuses it ('could not create image from "
             "window'). Its title can also be stale. Bring it into the current Space first."},
    {"key": "axpress-on-a-container-misses",
     "tags": ["accessibility", "web", "click", "canvas"],
     "fact": "AXPress acts on the whole element — on web content, a click at its centre. "
             "Chromium marks a clickable <canvas> (or card) as press-able, so pressing it "
             "to hit text drawn inside clicks the middle instead and still reports success. "
             "Only press an element labelled with the target text or tightly wrapping it; "
             "otherwise use a real click and verify with expect."},
    {"key": "shared-focus-hazard",
     "tags": ["input", "safety"],
     "fact": "Keyboard input goes to whatever has focus, which you share with the human at "
             "the keyboard. Blind typing has overwritten a person's unsent message. Prefer "
             "set_field_text, which writes to a NAMED element, over type_text."},
    {"key": "web-content-rejects-axsetvalue",
     "tags": ["accessibility", "browser", "targeting"],
     "fact": "Chromium-based web content ignores AXSetValue, so a direct write silently "
             "does nothing there. set_field_text falls back to verified-focus keystrokes; "
             "for real browser work prefer a browser-native tool over the accessibility tree."},
]


def _load() -> dict:
    if not STORE.exists():
        return {"facts": {}, "created": time.time()}
    try:
        return json.loads(STORE.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt store must not break the agent; start clean and keep going.
        return {"facts": {}, "created": time.time()}


def _save(db: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2, sort_keys=True))
    tmp.replace(STORE)


def ensure_seeded() -> int:
    """Install the seed facts once. Returns how many were added."""
    db = _load()
    added = 0
    for s in SEED:
        if s["key"] not in db["facts"]:
            db["facts"][s["key"]] = {"fact": s["fact"], "tags": s["tags"],
                                     "learned": time.strftime("%Y-%m-%d"),
                                     "source": "seed"}
            added += 1
    if added:
        _save(db)
    return added


def remember(key: str, fact: str, tags: list[str] | None = None,
             source: str = "agent") -> dict:
    """Record something learned the hard way. Re-using a key updates it."""
    db = _load()
    existing = db["facts"].get(key)
    db["facts"][key] = {"fact": fact, "tags": [t.lower() for t in (tags or [])],
                        "learned": time.strftime("%Y-%m-%d"), "source": source}
    _save(db)
    return {"key": key, "action": "updated" if existing else "added",
            "previous": existing["fact"] if existing else None}


def recall(query: str = "", limit: int = 12) -> list[dict]:
    """Facts relevant to what you are about to do.

    Matches tags first, then free text. An empty query returns everything, which
    is the right default at the start of a task.
    """
    db = _load()
    q = query.lower().strip()
    hits = []
    for key, rec in db["facts"].items():
        if not q:
            score = 0
        elif any(q == t or q in t for t in rec["tags"]):
            score = 2
        elif any(w in f"{key} {rec['fact']}".lower() for w in q.split() if len(w) > 2):
            score = 1
        else:
            continue
        hits.append((score, {"key": key, **rec}))
    hits.sort(key=lambda x: -x[0])
    return [h[1] for h in hits[:limit]]


def forget(key: str) -> bool:
    """Drop a fact that turned out to be wrong. Being wrong is worse than absent."""
    db = _load()
    if key in db["facts"]:
        del db["facts"][key]
        _save(db)
        return True
    return False
