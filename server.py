"""MCP server that gives the connected Claude client hands on this Mac.

No model call and no credentials live here. The client that loads this server
(Claude Desktop, Claude Code, ...) decides what to do; this process only
captures the screen and injects input on request, and the client asks the
human before each call. The implementation is executor.py, shared with run.py.

Coordinates: every tool takes pixels in the most recent screenshot, origin
top-left. The server owns Retina scaling; callers never think about it.

Safety: DRY_RUN=1 takes real screenshots and suppresses all input. The
pyautogui failsafe stays on — slam the cursor into a screen corner to abort.
"""

import base64
import contextlib
import os
import sys

from mcp.server.mcpserver import Image, MCPServer

import envmemory
import executor
import plans
import targeting

executor.DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
envmemory.ensure_seeded()

mcp = MCPServer(
    name="mac-computer-use",
    instructions=(
        "You control this Mac. Two rules make the difference between working and "
        "doing damage.\n\n"
        "1. Call recall_environment FIRST, before acting. This machine has quirks that "
        "will waste your time or break things if you assume defaults — the store exists "
        "because each one already cost someone an afternoon.\n\n"
        "2. Prefer the named-element tools (describe_ui, press_element, set_field_text) "
        "over click/type with coordinates. They address the element itself, so they "
        "cannot hit the wrong thing: if the target is missing, ambiguous, or changed, "
        "they refuse instead of acting. Coordinates are a guess, and a wrong guess still "
        "clicks. Fall back to screenshot + click only for canvases and custom-drawn UI "
        "with no accessibility tree.\n\n"
        "The human shares this screen and keyboard. If something lands unexpectedly, "
        "stop and say so rather than continuing to type. When you learn a durable fact "
        "about this machine, save it with remember_environment."
        + (" DRY RUN: input is suppressed; screenshots are real." if executor.DRY_RUN else "")
    ),
)


def _exec(name, inp):
    # stdout is the MCP transport; anything executor prints must go to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        return executor.execute(name, inp)


def _image(blocks) -> Image:
    return Image(data=base64.b64decode(blocks[0]["source"]["data"]), format="png")


def _run(name, inp) -> str:
    _exec(name, inp)
    return "dry-run: not executed" if executor.DRY_RUN else "OK"


@mcp.tool()
def screenshot() -> Image:
    """Capture the whole screen. Coordinates for every other tool are pixels in
    this image, origin top-left."""
    return _image(_exec("screenshot", {}))


@mcp.tool()
def zoom(x0: int, y0: int, x1: int, y1: int) -> Image:
    """Capture the region (x0, y0)-(x1, y1) at full resolution to read small
    text. Coordinates are screenshot pixels and stay in full-screenshot space
    afterwards — do not re-base them on the zoomed image."""
    return _image(_exec("zoom", {"region": [x0, y0, x1, y1]}))


@mcp.tool()
def click(x: int, y: int, button: str = "left", clicks: int = 1, modifiers: str | None = None) -> str:
    """Click at (x, y). button: left | right | middle. clicks: 1, 2 or 3 (left
    button only). modifiers: keys held during the click, e.g. "shift" or
    "cmd+shift"."""
    if button in ("right", "middle"):
        name = f"{button}_click"
    else:
        name = {2: "double_click", 3: "triple_click"}.get(clicks, "left_click")
    inp = {"coordinate": [x, y]}
    if modifiers:
        inp["text"] = modifiers
    return _run(name, inp)


@mcp.tool()
def move_mouse(x: int, y: int) -> str:
    """Move the cursor to (x, y) without clicking — for hover menus and to
    reveal an auto-hidden menu bar (y=0)."""
    return _run("mouse_move", {"coordinate": [x, y]})


@mcp.tool()
def drag(x0: int, y0: int, x1: int, y1: int) -> str:
    """Press the left button at (x0, y0), drag to (x1, y1), release."""
    return _run("left_click_drag", {"start_coordinate": [x0, y0], "coordinate": [x1, y1]})


@mcp.tool()
def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> str:
    """Scroll at (x, y). direction: up | down | left | right. amount: wheel ticks."""
    return _run("scroll", {"coordinate": [x, y], "scroll_direction": direction, "scroll_amount": amount})


@mcp.tool()
def type_text(text: str) -> str:
    """Type text into whatever has keyboard focus. Click the target first and
    screenshot to confirm focus — text goes wherever the cursor is."""
    return _run("type", {"text": text})


@mcp.tool()
def press_keys(keys: str, repeat: int = 1) -> str:
    """Press a key or chord: "Return", "Escape", "Tab", "cmd+space",
    "cmd+shift+t". Modifier names: cmd, ctrl, alt/option, shift."""
    return _run("key", {"text": keys, "repeat": repeat})


@mcp.tool()
def wait(seconds: float) -> str:
    """Pause, e.g. for an app to open. Max 300."""
    return _run("wait", {"duration": seconds})


@mcp.tool()
def cursor_position() -> str:
    """Current mouse position, in screenshot pixels."""
    return _exec("cursor_position", {})[0]["text"]


# ---------------------------------------------------------------- targeting
# These address an element by name through the accessibility tree instead of by
# pixel. A wrong target raises instead of clicking, which is the whole point.

@mcp.tool()
def describe_ui(app: str, actionable_only: bool = True, limit: int = 60,
                window: str | None = None) -> str:
    """List the named, addressable elements of an app — use this before acting.

    Returns role, label and available actions for each element. Those labels are
    what press_element and set_field_text match on, so read this first rather
    than guessing a name. app is the app's visible name, e.g. "Brave Browser".

    `window` (a substring of a window title) scopes to one window. Pass it whenever
    the app has more than one document open — otherwise every role-only lookup
    matches once per window and is refused as ambiguous. list_windows shows them.
    """
    try:
        nodes = targeting.walk(app, window)
    except targeting.TargetError as e:
        return f"ERROR: {e}"
    if actionable_only:
        nodes = [n for n in nodes if n.actionable or n.role in
                 ("AXTextArea", "AXTextField", "AXComboBox")]
    if not nodes:
        return (f"No addressable elements in {app!r}. If the app is running without an "
                "open window its tree holds only menus — check for a window first.")

    # Window controls before menu items. An app has hundreds of menu entries and a
    # handful of buttons; listing menus first buries the thing the caller wants.
    MENU = ("AXMenuItem", "AXMenu", "AXMenuBarItem", "AXMenuBar")
    window = [n for n in nodes if n.role not in MENU]
    menus = [n for n in nodes if n.role in MENU]
    ordered = window + menus

    lines = [f"{len(nodes)} addressable in {app!r} "
             f"({len(window)} in windows, {len(menus)} in menus) — showing {min(limit, len(ordered))}:"]
    if not window:
        lines.append("  (no window controls — the app may have no open window)")
    shown_menu_header = False
    for n in ordered[:limit]:
        if n.role in MENU and not shown_menu_header:
            lines.append("  --- menus ---")
            shown_menu_header = True
        lines.append(f"  {n.role:16} {n.label!r}" +
                     (f"  value={n.value[:28]!r}" if n.value else ""))
    return "\n".join(lines)


@mcp.tool()
def press_element(app: str, label: str, role: str | None = None,
                  exact: bool = False, window: str | None = None) -> str:
    """Press a named element (button, tab, menu item) — no coordinates involved.

    Resolves label within app, re-checks the element at the moment of dispatch,
    then sends AXPress to that element. Refuses rather than acting when nothing
    matches, when several things match (it will list them — re-call with role or
    exact=True), or when the UI changed underneath. Set exact=True for a whole-label
    match, and role (e.g. "AXButton", "AXRadioButton") to disambiguate.
    """
    if executor.DRY_RUN:
        try:
            n = targeting.resolve(app, label, role, exact, window=window)
            return f"dry-run: would press {n.describe()}"
        except targeting.TargetError as e:
            return f"REFUSED: {e}"
    try:
        r = targeting.press(app, label, role, exact, window)
        return (f"pressed {r['target']}\n  value before: {r['value_before']!r}\n"
                f"  value after : {r['value_after']!r}\n  changed: {r['changed']}")
    except targeting.TargetError as e:
        return f"REFUSED: {type(e).__name__}: {e}"


@mcp.tool()
def set_field_text(app: str, text: str, label: str | None = None,
                   role: str | None = None, exact: bool = False,
                   clear: bool = False, window: str | None = None) -> str:
    """Write text into a NAMED field, never into whatever happens to have focus.

    This is the safe alternative to type_text. It writes directly to the resolved
    element where it can; where the field refuses a programmatic write (web content
    usually does) it focuses that element, CONFIRMS focus actually landed there, and
    only then sends keystrokes. Either way the text cannot go to the wrong window.
    Set clear=True to replace existing content rather than append.
    """
    if executor.DRY_RUN:
        try:
            n = targeting.resolve(app, label, role, exact, actionable_only=False, window=window)
            return f"dry-run: would write {len(text)} chars to {n.describe()}"
        except targeting.TargetError as e:
            return f"REFUSED: {e}"
    try:
        r = targeting.type_into(app, text, label, role, exact, clear, window)
        return (f"wrote to {r['target']}\n  method: {r['method']}\n"
                f"  before: {r['value_before'][:60]!r}\n  after : {r['value_after'][:60]!r}")
    except targeting.TargetError as e:
        return f"REFUSED: {type(e).__name__}: {e}"


@mcp.tool()
def wait_for_element(app: str, label: str | None = None, role: str | None = None,
                     exact: bool = False, timeout: float = 10.0,
                     window: str | None = None) -> str:
    """Wait for an element to appear, then report it. Use after anything async.

    Interfaces take time: press a button and the sheet it opens may not exist for
    a few hundred milliseconds. Acting immediately gets a spurious "not found".
    Waits only through absence — ambiguity returns at once, since more time cannot
    make several matches into one.
    """
    try:
        n = targeting.wait_for(app, label, role, exact, timeout, window=window)
        return f"appeared: {n.describe()}"
    except targeting.TargetError as e:
        return f"REFUSED: {type(e).__name__}: {e}"


@mcp.tool()
def press_menu(app: str, path: str) -> str:
    """Follow a menu path, e.g. "Format > Font > Bold" or "File > Save".

    Most of a native app's functionality lives in nested menus that a single press
    cannot reach. Presses the leaf item directly when it is already addressable,
    otherwise opens each level in turn and waits for the next to populate.
    """
    if executor.DRY_RUN:
        return f"dry-run: would follow menu path {path!r} in {app!r}"
    try:
        r = targeting.press_menu_path(app, path)
        return f"pressed {path!r} via {r['route']}\n  target: {r['target']}"
    except targeting.TargetError as e:
        return f"REFUSED: {type(e).__name__}: {e}"


@mcp.tool()
def open_app(app: str, launch_if_needed: bool = True) -> str:
    """Bring an app to the front, launching it first if it isn't running.

    Keystrokes always go to the frontmost app, so anything involving typing needs
    this first. Waits for the switch to actually complete — and, on launch, for a
    real window to exist, since an app that has started but not drawn a window has
    a tree containing only its menu bar.
    """
    if executor.DRY_RUN:
        return f"dry-run: would open/activate {app!r}"
    try:
        return f"activated: {targeting.activate_app(app)}"
    except targeting.NotFound:
        if not launch_if_needed:
            return f"REFUSED: {app!r} is not running and launch_if_needed is False."
        try:
            r = targeting.launch_app(app)
            if r.get("ready"):
                return f"launched: {r}\nactivated: {targeting.activate_app(app)}"
            return f"launched but not ready: {r}"
        except targeting.TargetError as e:
            return f"REFUSED: {type(e).__name__}: {e}"


@mcp.tool()
def focused_element(app: str | None = None) -> str:
    """What currently has keyboard focus — check this before any blind typing."""
    target = app or targeting.frontmost_app()
    try:
        n = targeting.focused(target)
    except targeting.TargetError as e:
        return f"ERROR: {e}"
    if n is None:
        return f"{target!r} reports no focused element."
    return f"frontmost app: {targeting.frontmost_app()!r}\nfocused in {target!r}: {n.describe()}"


# ------------------------------------------------------------ env memory

@mcp.tool()
def recall_environment(query: str = "") -> str:
    """What this machine has already taught us. CALL THIS BEFORE ACTING.

    Returns quirks that break naive assumptions — wrong keyboard shortcuts, tools
    that report false failures, data sources that are incomplete. Pass a topic
    ("peekaboo", "browser", "x", "filesystem") to narrow, or nothing for all of it.
    """
    facts = envmemory.recall(query)
    if not facts:
        return f"Nothing recorded for {query!r}." if query else "No facts recorded yet."
    head = f"{len(facts)} fact(s)" + (f" for {query!r}" if query else "") + ":"
    return head + "\n" + "\n".join(f"  [{f['key']}] {f['fact']}" for f in facts)


@mcp.tool()
def remember_environment(key: str, fact: str, tags: list[str] | None = None) -> str:
    """Record a durable fact about this machine so it is never relearned.

    Worth saving: a shortcut that does something unexpected, a tool that lies about
    success, a data source that is incomplete, a setup step that isn't obvious.
    Not worth saving: anything specific to the current task. Reusing a key updates it.
    """
    r = envmemory.remember(key, fact, tags or [])
    return (f"{r['action']} [{key}]" +
            (f"\n  previous: {r['previous'][:80]!r}" if r["previous"] else ""))


@mcp.tool()
def list_windows(app: str) -> str:
    """Titles of an app's open windows — the values to pass as `window` elsewhere."""
    try:
        w = targeting.windows(app)
    except targeting.TargetError as e:
        return f"ERROR: {e}"
    if not w:
        return f"{app!r} has no open windows (its tree will contain only menus)."
    return f"{len(w)} window(s) in {app!r}:\n" + "\n".join(f"  {t!r}" for t in w)


@mcp.tool()
def check_plan(steps: list[dict]) -> str:
    """Resolve every step of a multi-step task WITHOUT performing any of it.

    Use this before run_plan for anything with more than two or three steps. A task
    that fails at step four has already done steps one to three, leaving the machine
    in a state nobody designed — and working out what was already done is the hardest
    thing to recover from.

    Each step is a dict:
      {"action": "press",     "app": ..., "label": ..., "role": ..., "window": ...}
      {"action": "type",      "app": ..., "text": ..., "role": ..., "window": ..., "clear": bool}
      {"action": "menu",      "app": ..., "path": "File > Save"}
      {"action": "open_app",  "app": ...}
      {"action": "wait_for",  "app": ..., "label": ..., "timeout": 10}
    Optional on any step: "settle" (seconds to pause after), "defer": true with
    "because" for a target that only exists once earlier steps have run.

    Verdicts: resolved (proved it exists now), deferred (cannot be known yet —
    open_app, wait_for, and anything marked defer), blocked (a refusal that will not
    fix itself). READY means nothing is blocked.
    """
    try:
        return plans.format_check(plans.check(steps))
    except plans.PlanError as e:
        return f"MALFORMED PLAN: {e}"


@mcp.tool()
def run_plan(steps: list[dict], require_ready: bool = True) -> str:
    """Check a plan, then perform it — aborting before step one if anything is blocked.

    Same step format as check_plan. With require_ready (the default) a single blocked
    step means nothing at all is performed. Set it False only when you have read the
    check and accept starting a plan known not to complete.

    Stops at the first failure and reports exactly what was performed and what was
    left, so a partial run can be reasoned about rather than guessed at.
    """
    if executor.DRY_RUN:
        try:
            return "DRY RUN — check only:\n" + plans.format_check(plans.check(steps))
        except plans.PlanError as e:
            return f"MALFORMED PLAN: {e}"
    try:
        r = plans.run(steps, require_ready)
    except plans.PlanError as e:
        return f"MALFORMED PLAN: {e}"

    if not r["executed"]:
        return ("ABORTED — nothing was performed.\n" + plans.format_check(r["check"]))
    out = ["COMPLETED" if r.get("completed") else f"INCOMPLETE — {r['reason']}"]
    for p in r["performed"]:
        out.append(f"  [{p['step']}] {'ok ' if p['ok'] else 'FAIL'} {p['desc']}")
        out.append(f"        {str(p.get('result') or p.get('error'))[:160]}")
    if r.get("remaining"):
        out.append("  not attempted: " + "; ".join(r["remaining"]))
    return "\n".join(out)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
