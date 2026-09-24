"""Provable targeting: act on a named element, or refuse.

Pixel clicking is a guess. You compute a coordinate from a stale screenshot and
hope the thing you meant is still under it. When it isn't, the click still
lands — on whatever moved into that spot. That is how an agent types into the
wrong window.

This module removes the guess. An action names what it means to act on
("the Send button"), the element is resolved through the macOS accessibility
tree, and the action is delivered *to that element* via AXPress / AXSetValue.
No coordinate is involved, so there is no coordinate to be wrong.

Three rules make it provable rather than merely likely:

1. Exactly one match, or refuse. Zero matches raises NotFound; two or more
   raises Ambiguous with the candidates listed. An agent never picks for you.
2. Re-verify at the moment of dispatch. The tree is re-read and the element's
   identity re-checked immediately before acting, so a UI that changed between
   look and act is caught instead of acted on.
3. Confirm afterwards. `act()` returns the element's state before and after so
   the caller can assert something actually happened.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from AppKit import NSWorkspace
from ApplicationServices import (
    AXIsProcessTrusted,
    AXUIElementCopyActionNames,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
)


class TargetError(Exception):
    """Base for every refusal. Raised instead of acting on the wrong thing."""


class NotFound(TargetError):
    pass


class Ambiguous(TargetError):
    pass


class NotActionable(TargetError):
    pass


class Changed(TargetError):
    """The element stopped matching between resolution and dispatch."""


# Attributes worth reading. Cheap enough to fetch for every node.
_TEXT_ATTRS = ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXLabel")


def _attr(el, name: str):
    try:
        err, val = AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def _actions(el) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(el, None)
        return list(names) if err == 0 and names else []
    except Exception:
        return []


@dataclass
class Node:
    """One accessibility element, with the identity used to prove a match."""

    role: str
    label: str                      # best human-readable name
    value: str
    actions: list[str] = field(default_factory=list)
    path: tuple[int, ...] = ()      # child-index path from the app element
    _el: Any = None                 # live AXUIElement

    @property
    def actionable(self) -> bool:
        return "AXPress" in self.actions

    def identity(self) -> tuple:
        """What must stay the same between resolution and dispatch."""
        return (self.role, self.label, self.path)

    def describe(self) -> str:
        v = f" value={self.value[:30]!r}" if self.value else ""
        return f"{self.role} {self.label!r}{v} actions={self.actions}"


def _text_of(el) -> tuple[str, str]:
    """Return (label, value) as plain strings."""
    label = ""
    for a in ("AXTitle", "AXDescription", "AXLabel", "AXPlaceholderValue"):
        v = _attr(el, a)
        if isinstance(v, str) and v.strip():
            label = v.strip()
            break
    raw = _attr(el, "AXValue")
    value = raw.strip() if isinstance(raw, str) else ("" if raw is None else str(raw))
    if not label and value:
        label = value[:60]
    return label, value


def _app_element(app_name: str):
    """AXUIElement for a running app, matched on name (case-insensitive)."""
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        name = a.localizedName() or ""
        if name.lower() == app_name.lower():
            return AXUIElementCreateApplication(a.processIdentifier()), name, a.processIdentifier()
    raise NotFound(
        f"no running application named {app_name!r}. Running: "
        + ", ".join(sorted({a.localizedName() or "?" for a in
                            NSWorkspace.sharedWorkspace().runningApplications()
                            if a.activationPolicy() == 0}))
    )


def windows(app_name: str) -> list[str]:
    """Titles of an app's open windows, in order."""
    root, _, _ = _app_element(app_name)
    return [str(_attr(w, "AXTitle") or "") for w in (_attr(root, "AXWindows") or [])]


def _window_element(root, app_name: str, window: str):
    """The one window whose title matches, or a refusal naming the alternatives."""
    wins = list(_attr(root, "AXWindows") or [])
    titled = [(str(_attr(w, "AXTitle") or ""), w) for w in wins]
    hits = [(t, w) for t, w in titled if window.lower() in t.lower()]
    if not hits:
        raise NotFound(
            f"no window matching {window!r} in {app_name!r}. "
            f"Open windows: {[t for t, _ in titled] or 'none'}"
        )
    if len(hits) > 1:
        raise Ambiguous(
            f"{len(hits)} windows match {window!r} in {app_name!r} — refusing to "
            f"choose. Titles: {[t for t, _ in hits]}"
        )
    return hits[0][1]


def walk(app_name: str, window: str | None = None,
         max_depth: int = 18, max_nodes: int = 4000) -> list[Node]:
    """Flatten an app's accessibility tree into Nodes.

    Breadth-first so shallow, interesting controls (toolbars, buttons) are
    reached before deep list rows — the mistake that made an earlier attempt
    return 15,000 table cells and no toolbar.

    `window` scopes the walk to one window, matched on a substring of its title.
    Without it, an app with two documents open yields two of everything and every
    role-only lookup is ambiguous — correctly refused, but unusable. Scoping is
    how you say which document you mean. Menus live outside any window, so a
    scoped walk deliberately excludes them.
    """
    if not AXIsProcessTrusted():
        raise TargetError(
            "Accessibility permission not granted to this process. "
            "System Settings > Privacy & Security > Accessibility."
        )
    root, _, _ = _app_element(app_name)
    out: list[Node] = []
    if window is not None:
        # Start from the window itself; its own path stays () so paths remain
        # comparable across calls that pass the same scope.
        queue: list[tuple[Any, tuple[int, ...], int]] = [(_window_element(root, app_name, window), (), 0)]
    else:
        queue = [(root, (), 0)]

    while queue and len(out) < max_nodes:
        el, path, depth = queue.pop(0)
        if depth > max_depth:
            continue
        role = _attr(el, "AXRole") or "?"
        label, value = _text_of(el)
        acts = _actions(el)
        if depth:  # skip the application element itself
            out.append(Node(role=role, label=label, value=value, actions=acts,
                            path=path, _el=el))
        children = list(_attr(el, "AXChildren") or [])
        if depth == 0:
            # An application element lists its menu bar under AXChildren but its
            # windows only under AXWindows. Walking AXChildren alone returns a
            # few hundred menu items and not one button.
            seen = {(_attr(c, "AXRole"), _attr(c, "AXTitle")) for c in children}
            for w in (_attr(el, "AXWindows") or []):
                if (_attr(w, "AXRole"), _attr(w, "AXTitle")) not in seen:
                    children.append(w)
        for i, c in enumerate(children):
            queue.append((c, path + (i,), depth + 1))
    return out


def _matches(n: Node, label: str | None, role: str | None, exact: bool) -> bool:
    if role and n.role.lower() != role.lower() and n.role.lower() != f"ax{role.lower()}":
        return False
    if label is None:
        return True
    hay = f"{n.label} {n.value}".strip()
    return hay.lower() == label.lower() if exact else label.lower() in hay.lower()


def resolve(app_name: str, label: str | None = None, role: str | None = None,
            exact: bool = False, actionable_only: bool = True,
            window: str | None = None) -> Node:
    """Find exactly one element, or raise. Never guesses between candidates.

    Pass `window` (a substring of its title) when the app has more than one
    document open — otherwise a role-only lookup matches once per window and is
    rightly refused as ambiguous.
    """
    nodes = walk(app_name, window)
    hits = [n for n in nodes if _matches(n, label, role, exact)]
    if actionable_only and len(hits) > 1:
        narrowed = [n for n in hits if n.actionable]
        if narrowed:
            hits = narrowed

    what = (f"label={label!r} role={role!r} in {app_name!r}"
            + (f" window~{window!r}" if window else ""))
    if not hits:
        near = [n.describe() for n in nodes
                if label and label.lower()[:4] in f"{n.label} {n.value}".lower()][:5]
        raise NotFound(f"no element matching {what}." +
                       (f" Did you mean: {near}" if near else ""))
    if len(hits) > 1:
        hint = ""
        if window is None:
            try:
                wins = windows(app_name)
                if len(wins) > 1:
                    hint = (f" {len(wins)} windows are open ({wins}); pass "
                            "window=<title substring> to scope to one.")
            except TargetError:
                pass
        raise Ambiguous(
            f"{len(hits)} elements match {what} — refusing to choose. "
            f"Candidates: {[h.describe() for h in hits[:6]]}.{hint}"
        )
    return hits[0]


def _reverify(app_name: str, node: Node, window: str | None = None) -> Node:
    """Re-read the tree and confirm the element still is what we resolved."""
    for n in walk(app_name, window):
        if n.path == node.path:
            if n.identity() != node.identity():
                raise Changed(
                    f"element at this position changed between resolution and "
                    f"dispatch: was {node.describe()}, now {n.describe()}"
                )
            return n
    raise Changed(f"element disappeared before dispatch: {node.describe()}")


def press(app_name: str, label: str | None = None, role: str | None = None,
          exact: bool = False, window: str | None = None) -> dict:
    """Resolve, re-verify, AXPress. Returns before/after state for assertions."""
    node = resolve(app_name, label, role, exact, window=window)
    if not node.actionable:
        raise NotActionable(
            f"{node.describe()} exposes no AXPress action. "
            "It is probably a label or container, not a control."
        )
    live = _reverify(app_name, node, window)
    before = live.value
    err = AXUIElementPerformAction(live._el, "AXPress")
    if err != 0:
        raise TargetError(f"AXPress failed with error {err} on {live.describe()}")
    after = _text_of(live._el)[1]
    return {"target": live.describe(), "value_before": before, "value_after": after,
            "changed": before != after}


def set_text(app_name: str, text: str, label: str | None = None,
             role: str | None = None, exact: bool = False,
             window: str | None = None) -> dict:
    """Write text into a named field — never into 'whatever has focus'.

    This is the guard that matters most: typing blind is how an agent's text
    ends up in someone's chat draft.
    """
    node = resolve(app_name, label, role, exact, actionable_only=False, window=window)
    live = _reverify(app_name, node, window)
    before = live.value
    err = AXUIElementSetAttributeValue(live._el, "AXValue", text)
    if err != 0:
        raise TargetError(
            f"could not set text on {live.describe()} (error {err}). "
            "The field may not accept programmatic input; a focused keystroke "
            "route is needed instead."
        )
    after = _text_of(live._el)[1]
    if after != text:
        raise TargetError(
            f"wrote text but the field does not contain it: {after[:60]!r}"
        )
    return {"target": live.describe(), "value_before": before, "value_after": after}


def wait_for(app_name: str, label: str | None = None, role: str | None = None,
             exact: bool = False, timeout: float = 10.0, poll: float = 0.3,
             window: str | None = None) -> Node:
    """Wait for an element to exist, then return it.

    Real interfaces are asynchronous: you press a button and the sheet it opens
    takes a few hundred milliseconds to exist. `resolve` asks once and gives up,
    which turns ordinary latency into a NotFound.

    Only absence is worth waiting through. Ambiguity means several things already
    match and waiting will not reduce them, so that raises immediately rather than
    burning the timeout on a result that cannot improve.
    """
    deadline = time.monotonic() + timeout
    last: TargetError | None = None
    while True:
        try:
            return resolve(app_name, label, role, exact, window=window)
        except Ambiguous:
            raise                       # more time cannot make this unambiguous
        except (NotFound, TargetError) as e:
            last = e
        if time.monotonic() >= deadline:
            raise NotFound(f"waited {timeout:.0f}s for label={label!r} role={role!r} "
                           f"in {app_name!r} and it never appeared. Last: {last}")
        time.sleep(poll)


def _focus(node: Node) -> bool:
    """Give an element keyboard focus. Returns whether it took."""
    AXUIElementSetAttributeValue(node._el, "AXFocused", True)
    return bool(_attr(node._el, "AXFocused"))


def type_into(app_name: str, text: str, label: str | None = None,
              role: str | None = None, exact: bool = False,
              clear: bool = False, window: str | None = None) -> dict:
    """Put text in a named field, falling back to keystrokes when writes are refused.

    `set_text` is preferred because it writes to the element directly. But many
    fields refuse a programmatic write — web content especially, where Chromium
    ignores AXSetValue entirely. Typing is the only way in.

    Blind typing is what put an agent's text into someone's chat draft, so this
    never types hopefully: it focuses the resolved element, *confirms the focus
    actually landed there*, and only then sends keystrokes. If focus went
    somewhere else, it raises and sends nothing.
    """
    node = resolve(app_name, label, role, exact, actionable_only=False, window=window)
    live = _reverify(app_name, node, window)
    before = live.value

    # Direct write first — no keystrokes means nothing can be misrouted.
    if AXUIElementSetAttributeValue(live._el, "AXValue", text) == 0:
        after = _text_of(live._el)[1]
        # _text_of strips, so compare stripped: otherwise text with a trailing
        # newline reads back "different" and a successful write falls through
        # to the keystroke path for no reason.
        if after.strip() == text.strip():
            return {"target": live.describe(), "method": "AXSetValue",
                    "value_before": before, "value_after": after}

    # Fall back to keystrokes, but prove focus first.
    import pyautogui

    _focus(live)
    if not _attr(live._el, "AXFocused"):
        now = focused(app_name)
        raise TargetError(
            f"could not give focus to {live.describe()}"
            + (f" — it went to {now.describe()} instead" if now else "")
            + ". Refusing to type, the text would go to the wrong place."
        )
    if clear:
        pyautogui.hotkey("command", "a")
        pyautogui.press("delete")
    pyautogui.write(text, interval=0.012)

    after = _text_of(live._el)[1]
    return {"target": live.describe(), "method": "keystrokes (focus verified)",
            "value_before": before, "value_after": after}


def press_menu_path(app_name: str, path: str, timeout: float = 5.0) -> dict:
    """Follow a menu path like "Format > Font > Bold".

    Most of a native app's functionality is in its menus, and they nest, so a
    single press is not enough. Menu items are addressable in the accessibility
    tree without opening anything, so the leaf is pressed directly where possible —
    fewer moving parts than simulating each click.

    Some menus only populate once their parent opens. When the leaf is not yet in
    the tree, the parents are pressed in order and the leaf waited for.
    """
    parts = [p.strip() for p in path.split(">") if p.strip()]
    if not parts:
        raise TargetError("empty menu path")
    leaf = parts[-1]

    # Direct route: the leaf already exists in the tree.
    try:
        node = resolve(app_name, leaf, "AXMenuItem", exact=True)
        live = _reverify(app_name, node)
        if AXUIElementPerformAction(live._el, "AXPress") == 0:
            return {"path": path, "route": "direct", "target": live.describe()}
    except (NotFound, Ambiguous):
        pass  # fall through to opening each level

    # Walk it open, level by level.
    opened = []
    for i, part in enumerate(parts[:-1]):
        role = "AXMenuBarItem" if i == 0 else "AXMenuItem"
        n = wait_for(app_name, part, role, exact=True, timeout=timeout)
        if AXUIElementPerformAction(n._el, "AXPress") != 0:
            raise TargetError(f"could not open menu {part!r} in path {path!r}")
        opened.append(part)
        time.sleep(0.25)

    n = wait_for(app_name, leaf, "AXMenuItem", exact=True, timeout=timeout)
    if AXUIElementPerformAction(n._el, "AXPress") != 0:
        raise TargetError(f"could not press {leaf!r} in path {path!r}")
    return {"path": path, "route": f"opened {' > '.join(opened)}", "target": n.describe()}


def activate_app(app_name: str, timeout: float = 8.0) -> dict:
    """Bring a running app to the front and wait until it really is frontmost.

    Acting on a background app works for AX presses but not for keystrokes, which
    always follow the frontmost app. Returning before the switch completes is how
    a "type" ends up in whatever was in front a moment ago.
    """
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if (a.localizedName() or "").lower() == app_name.lower():
            a.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if frontmost_app().lower() == app_name.lower():
                    return {"app": a.localizedName(), "frontmost": True,
                            "pid": a.processIdentifier()}
                time.sleep(0.2)
            return {"app": a.localizedName(), "frontmost": False,
                    "note": f"activation requested but {frontmost_app()!r} is still in front"}
    raise NotFound(f"{app_name!r} is not running — launch_app first.")


def launch_app(app_name: str, timeout: float = 25.0) -> dict:
    """Open an app and wait until it has a usable window.

    "Launched" is not "ready": LaunchServices returns as soon as the process
    starts, long before a window exists, and a tree walked too early holds only
    the menu bar.
    """
    # NSWorkspace.launchApplication_ is deprecated and on macOS 26 returns True
    # while doing nothing at all — a silent no-op that then looks like "the app
    # has no window". /usr/bin/open is boring and actually works.
    proc = subprocess.run(["/usr/bin/open", "-a", app_name],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise NotFound(
            f"could not launch {app_name!r}: {proc.stderr.strip() or 'open failed'}"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root, name, pid = _app_element(app_name)
            if _attr(root, "AXWindows"):
                return {"app": name, "pid": pid, "ready": True}
        except NotFound:
            pass
        time.sleep(0.4)
    return {"app": app_name, "ready": False,
            "note": "launched but no window appeared within the timeout"}


def focused(app_name: str) -> Node | None:
    """Whatever currently has keyboard focus — for asserting before typing."""
    root, _, _ = _app_element(app_name)
    el = _attr(root, "AXFocusedUIElement")
    if el is None:
        return None
    label, value = _text_of(el)
    return Node(role=_attr(el, "AXRole") or "?", label=label, value=value,
                actions=_actions(el), path=(), _el=el)


def frontmost_app() -> str:
    return NSWorkspace.sharedWorkspace().frontmostApplication().localizedName() or "?"
