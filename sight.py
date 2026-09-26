"""See one app's window, find text in it, and click that text — for apps that hide
their controls from the accessibility tree.

targeting.py addresses elements by name through the accessibility tree, which is
the safe way to act. But web views — Electron, Tauri, anything built on Chromium —
expose almost nothing there: a whole Buzz window came back as a hundred and
seventy unlabelled elements. So the only option left was a full-screen screenshot
and a guessed pixel, which failed two ways at once:

1. **Wrong screenshot.** A full-screen capture shows whatever is on top. Called
   from the Claude desktop app, the Claude window comes to the front on every
   tool call, so the "screenshot of Buzz" was a screenshot of Claude.
2. **Guessed coordinates.** Even with the right picture, a pixel is a guess, and
   the app you meant to click may not be the one in front when the click lands.

This module fixes both without giving up the rules targeting.py keeps:

- **Capture a window, not the screen.** One app's window is captured directly
  from the window server, so it is correct whatever happens to be in front.
- **Target text, not pixels.** macOS's own text recognition reads the window; a
  click names the text it means ("Set up later"). Exactly one match, or it
  refuses and lists the candidates with where they are.
- **Click without needing focus.** The element under the text is found through
  accessibility and its own action performed (press, open), so the app can stay
  behind other windows — which matters, because while the Claude desktop app runs
  a tool call macOS will not let any other app come to the front.
- **Mouse only where it provably lands.** If nothing there accepts an
  accessibility action, a real click is sent only when macOS's own hit test says
  it would reach *this* window; otherwise it refuses.
- **Refuse stale coordinates.** Coordinates are tied to a specific capture of a
  specific window; if the window moved or closed since, the click is refused.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from AppKit import NSWorkspace
from Foundation import NSURL
from PIL import Image
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListOptionOnScreenOnly,
)
import Vision

from targeting import Ambiguous, NotFound, TargetError

RETURN_LONG_EDGE = 1280        # size of images handed back to the model


# ------------------------------------------------------------------ windows

@dataclass(frozen=True)
class Window:
    id: int
    app: str
    title: str
    x: float          # global position and size, in points
    y: float
    w: float
    h: float
    pid: int = 0      # owning process — two apps can share a name (two Brave instances)

    def bounds(self) -> tuple:
        return (round(self.x), round(self.y), round(self.w), round(self.h))

    def describe(self) -> str:
        t = f" {self.title!r}" if self.title else ""
        return f"{self.app}{t} {int(self.w)}x{int(self.h)} at ({int(self.x)},{int(self.y)})"


def app_windows(app: str) -> list[Window]:
    """On-screen, normal-level windows owned by `app`, front to back."""
    out = []
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner.lower() != app.lower():
            continue
        b = w["kCGWindowBounds"]
        if b["Width"] < 50 or b["Height"] < 50:      # ignore slivers and helpers
            continue
        out.append(Window(int(w["kCGWindowNumber"]), owner, str(w.get("kCGWindowName") or ""),
                          float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]),
                          int(w.get("kCGWindowOwnerPID") or 0)))
    return out


def pick_window(app: str, window: str | None = None) -> Window:
    """Exactly one window: the frontmost one, or the one whose title contains `window`."""
    wins = app_windows(app)
    if not wins:
        raise NotFound(f"{app!r} has no visible window (it may be quit, hidden, or minimised).")
    if window is None:
        return wins[0]                                 # front-most of that app
    hits = [w for w in wins if window.lower() in w.title.lower()]
    if not hits:
        raise NotFound(f"no {app!r} window titled like {window!r}. "
                       f"Windows: {[w.title or '(untitled)' for w in wins]}")
    if len(hits) > 1:
        raise Ambiguous(f"{len(hits)} {app!r} windows match {window!r}: "
                        f"{[w.describe() for w in hits]}")
    return hits[0]


def _window_now(win_id: int) -> Window | None:
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if int(w["kCGWindowNumber"]) == win_id:
            b = w["kCGWindowBounds"]
            return Window(win_id, str(w.get("kCGWindowOwnerName") or ""),
                          str(w.get("kCGWindowName") or ""),
                          float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"]),
                          int(w.get("kCGWindowOwnerPID") or 0))
    return None


# ------------------------------------------------------------------ capture

@dataclass
class Capture:
    window: Window
    image: Image.Image            # full resolution
    px_per_pt: float              # full-resolution pixels per point (2.0 on Retina)
    returned: Image.Image = None  # downscaled copy for the model
    returned_scale: float = 1.0   # full-res pixels per returned pixel
    taken_at: float = field(default_factory=time.time)

    def returned_to_global(self, rx: float, ry: float) -> tuple[float, float]:
        """A point in the returned image → global screen points."""
        return self.full_to_global(rx * self.returned_scale, ry * self.returned_scale)

    def full_to_global(self, fx: float, fy: float) -> tuple[float, float]:
        return (self.window.x + fx / self.px_per_pt, self.window.y + fy / self.px_per_pt)


_last: dict[str, Capture] = {}        # most recent capture per app, for click_in_window


def capture(app: str, window: str | None = None) -> Capture:
    """Capture one window of `app`, whatever is in front of it.

    Waits for the window to stop moving first: a window captured mid-animation
    (just opened, being resized) has bounds that are stale a moment later, and
    every click computed from that capture would then be refused.
    """
    win = pick_window(app, window)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        time.sleep(0.12)
        again = _window_now(win.id)
        if again is None or again.bounds() == win.bounds():
            break
        win = again
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(win.id), path],
                           capture_output=True, text=True)
        if r.returncode != 0 or os.path.getsize(path) == 0:
            raise TargetError(
                f"could not capture {win.describe()}: {r.stderr.strip() or 'empty image'}. "
                "Screen Recording permission may be missing for the app running this server.")
        img = Image.open(path).convert("RGB")
        img.load()
    finally:
        os.unlink(path)

    cap = Capture(window=win, image=img, px_per_pt=img.width / win.w)
    ret = img.copy()
    ret.thumbnail((RETURN_LONG_EDGE, RETURN_LONG_EDGE), Image.LANCZOS)
    cap.returned, cap.returned_scale = ret, img.width / ret.width
    _last[app.lower()] = cap
    return cap


def last_capture(app: str) -> Capture | None:
    return _last.get(app.lower())


# --------------------------------------------------------------------- text

@dataclass
class TextHit:
    text: str            # the whole recognised line
    matched: str         # the part that matched the query
    conf: float
    x0: float            # box in FULL-resolution capture pixels
    y0: float
    x1: float
    y1: float

    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


def _vision_box(bb, W: int, H: int) -> tuple[float, float, float, float]:
    """Vision boxes are normalised with the origin bottom-left; convert to pixels."""
    x0 = bb.origin.x * W
    y0 = (1 - bb.origin.y - bb.size.height) * H
    return x0, y0, x0 + bb.size.width * W, y0 + bb.size.height * H


def read_text(cap: Capture) -> list[tuple[str, float, object, tuple]]:
    """All text lines in the capture: (text, confidence, candidate, line box)."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cap.image.save(path)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(path), None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)      # UI labels are not prose
        handler.performRequests_error_([req], None)
        lines = []
        W, H = cap.image.size
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            c = cand[0]
            lines.append((str(c.string()), float(c.confidence()), c,
                          _vision_box(obs.boundingBox(), W, H)))
        return lines
    finally:
        os.unlink(path)


def _norm(s: str) -> str:
    """Lower-case, collapse spaces, and drop icon glyphs that recognition reads as
    punctuation at the ends of a line (a sidebar's folder icon comes back as "*",
    so "* Applications" must still match exactly "Applications")."""
    t = " ".join(s.lower().split())
    core = re.sub(r"^[^\w]+\s*|\s*[^\w]+$", "", t)
    return core or t


def match_lines(lines, text: str, exact: bool = False,
                size: tuple[int, int] | None = None) -> list[TextHit]:
    """Pure matching over recognised lines, so it can be tested without a screen.

    With `size` (the capture's width and height) the box is narrowed to the
    matched words where Vision can say where they are, so "Save" inside
    "Save As…" is clicked on the word rather than the middle of the line.
    """
    want = _norm(text)
    hits = []
    for line, conf, cand, box in lines:
        have = _norm(line)
        if exact and have != want:
            continue
        if not exact and want not in have:
            continue
        hit_box = box
        if not exact and size and cand is not None:
            start = line.lower().find(text.strip().lower())
            if start >= 0:
                try:
                    rect, _err = cand.boundingBoxForRange_error_((start, len(text.strip())), None)
                    if rect is not None:
                        hit_box = _vision_box(rect.boundingBox(), *size)
                except Exception:
                    pass                      # keep the whole-line box
        hits.append(TextHit(line, line if exact else text, conf, *hit_box))
    hits.sort(key=lambda h: (round(h.y0 / 8), h.x0))   # reading order
    return hits


def find_text(app: str, text: str, window: str | None = None, exact: bool = False,
              occurrence: int | None = None, cap: Capture | None = None
              ) -> tuple[Capture, TextHit]:
    """Exactly one visible occurrence of `text` in the app's window, or refuse.

    `occurrence` (1-based, reading order) picks among several — for use after an
    Ambiguous refusal has shown you the candidates and where they are.
    """
    cap = cap or capture(app, window)
    lines = read_text(cap)
    hits = match_lines(lines, text, exact, cap.image.size)
    where = f"{text!r} in {cap.window.describe()}"
    if not hits:
        seen = [l for l, *_ in lines][:25]
        raise NotFound(f"no visible text matching {where}. Visible text includes: {seen}")
    if occurrence is not None:
        if not 1 <= occurrence <= len(hits):
            raise NotFound(f"occurrence {occurrence} requested but {where} appears {len(hits)} time(s)")
        return cap, hits[occurrence - 1]
    if len(hits) > 1:
        def pos(h):
            rx, ry = h.center()
            return f"#{hits.index(h)+1} {h.text!r} at ({int(rx / cap.returned_scale)},{int(ry / cap.returned_scale)})"
        raise Ambiguous(f"{len(hits)} matches for {where} — refusing to choose. "
                        f"Candidates (returned-image coordinates): {[pos(h) for h in hits[:8]]}. "
                        "Pass exact=True, a longer text, or occurrence=N.")
    return cap, hits[0]


# ------------------------------------------------------------------- acting

def frontmost_app() -> str:
    return NSWorkspace.sharedWorkspace().frontmostApplication().localizedName() or "?"


def _front_pid() -> int:
    return int(NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier())


def ensure_front(app: str, timeout: float = 1.5, pid: int = 0) -> bool:
    """Try to bring `app` forward; return whether it really is frontmost.

    Returns rather than raises: macOS activation is cooperative, and while the
    Claude desktop app is running a tool call it refuses to yield the front to
    anyone (open -a, activateWithOptions, osascript and AXRaise all lose). The
    caller decides what is still safe to do without focus.

    With `pid`, that exact process is activated and checked — `open -a` goes by
    name, and with two instances of one app it raises whichever LaunchServices
    picks, which may be the human's own window rather than the target.
    """
    if pid:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        if _front_pid() == pid:
            return True
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if ra is None:
            return False
        ra.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        is_front = lambda: _front_pid() == pid
    else:
        if frontmost_app().lower() == app.lower():
            return True
        subprocess.run(["/usr/bin/open", "-a", app], capture_output=True)
        is_front = lambda: frontmost_app().lower() == app.lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_front():
            return True
        time.sleep(0.1)
    return False


def click_reaches(win: Window, gx: float, gy: float) -> bool:
    """Would a real click at this point be delivered to `win`'s app?

    Asks macOS's own hit test rather than reading the window stacking order,
    because transparent overlays (screen recorders, other automation tools) sit
    on top of everything and let clicks through: by stacking order the whole
    screen looks covered. Then, among that app's own windows, `win` must be the
    topmost one at the point.
    """
    from ApplicationServices import (AXUIElementCopyElementAtPosition,
                                     AXUIElementCreateSystemWide, AXUIElementGetPid)
    err, el = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), gx, gy, None)
    if err or el is None:
        return False
    _, pid = AXUIElementGetPid(el, None)
    if not win.pid or pid != win.pid:
        return False
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:
        if w.get("kCGWindowLayer", 0) != 0 or int(w.get("kCGWindowOwnerPID") or 0) != pid:
            continue
        bb = w.get("kCGWindowBounds") or {}
        if (bb.get("X", 0) <= gx < bb.get("X", 0) + bb.get("Width", 0)
                and bb.get("Y", 0) <= gy < bb.get("Y", 0) + bb.get("Height", 0)):
            return int(w["kCGWindowNumber"]) == win.id
    return False


# Accessibility actions that stand in for a click, best first. A double click
# means "open" in Finder-like lists, so it prefers AXOpen.
_CLICK_ACTIONS = {
    ("left", 1): ("AXPress", "AXOpen", "AXConfirm", "AXPick"),
    ("left", 2): ("AXOpen", "AXPress", "AXConfirm"),
    ("right", 1): ("AXShowMenu",),
}


def _frame(el) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) of an element in global points, or None if it has no frame."""
    from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
    from targeting import _attr
    pos, size = _attr(el, "AXPosition"), _attr(el, "AXSize")
    if pos is None or size is None:
        return None
    ok1, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    ok2, z = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (ok1 and ok2):
        return None
    return (p.x, p.y, p.x + z.width, p.y + z.height)


MAX_PRESS_AREA_RATIO = 8      # an element this many times bigger than the target is not "it"


def fits_target(frame, box, label: str, text: str | None) -> bool:
    """Is pressing this element the same as clicking the target?

    AXPress acts on the element as a whole — for web content, a click at its
    centre. That is the target when the element *is* the text (its label says so)
    or tightly wraps it. A canvas, a card or a page that merely contains the text
    is not: pressing it clicks somewhere else and still reports success.
    """
    if text and label and _norm(text) in _norm(label):
        return True
    if frame is None:
        return False
    x0, y0, x1, y1 = frame
    bx0, by0, bx1, by1 = box
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    if not (x0 <= cx <= x1 and y0 <= cy <= y1):
        return False
    area = max(1.0, (x1 - x0) * (y1 - y0))
    return area <= MAX_PRESS_AREA_RATIO * max(1.0, (bx1 - bx0) * (by1 - by0))


def _ax_click(app: str, win: Window, gx: float, gy: float, button: str, clicks: int,
              box: tuple | None = None, text: str | None = None) -> dict | None:
    """Act on the element under the point through accessibility — no mouse, no focus.

    Works with the app behind other windows. Returns None when nothing under the
    point takes a click-like action (web content often doesn't), so the caller
    can fall back. The action's return code is not trusted — Finder reports
    kAXErrorCannotComplete for an AXOpen that did navigate — so success is judged
    by the caller re-reading the window, not here.
    """
    from ApplicationServices import (AXUIElementCopyElementAtPosition,
                                     AXUIElementCreateApplication, AXUIElementPerformAction)
    from targeting import _actions, _app_element, _attr, _text_of

    wanted = _CLICK_ACTIONS.get((button, clicks))
    if not wanted:
        return None
    # Ask the process that owns *this* window, not the first app with this name.
    root = AXUIElementCreateApplication(win.pid) if win.pid else _app_element(app)[0]
    box = box or (gx - 12, gy - 12, gx + 12, gy + 12)     # a bare point: a finger-sized target

    def actionable():
        err, el = AXUIElementCopyElementAtPosition(root, gx, gy, None)
        cur = None if err else el
        for _ in range(6):                   # the text itself, then its cell/row/button
            if cur is None or _attr(cur, "AXRole") in ("AXWindow", "AXApplication"):
                return None
            acts = _actions(cur)
            for a in wanted:
                if a in acts:
                    # Ancestors only get bigger, so the first press-able one decides.
                    ok = fits_target(_frame(cur), box, _text_of(cur)[0], text)
                    return (cur, a) if ok else "too-big"
            cur = _attr(cur, "AXParent")
        return None

    found = actionable()
    woke = False
    if found is None and _wake_web_accessibility(root, win.pid):
        woke = True                          # Chromium builds its tree on request
        deadline = time.monotonic() + 3.0
        while found is None and time.monotonic() < deadline:
            time.sleep(0.25)
            found = actionable()
    if found is None or found == "too-big":
        return None
    el, a = found
    AXUIElementPerformAction(el, a)
    return {"method": "accessibility", "action": a, "woke_web_accessibility": woke,
            "element": f"{_attr(el, 'AXRole')} {_text_of(el)[0]!r}".strip()}


_woken: set[int] = set()


def _wake_web_accessibility(root, pid: int) -> bool:
    """Ask a Chromium/Electron app to build its accessibility tree. Once per process.

    Chromium exposes only anonymous groups until an assistive tool asks for more;
    AXManualAccessibility is the Electron/Chromium switch for exactly this, and
    AXEnhancedUserInterface is what VoiceOver sets. Both return error codes even
    when they work, so the caller re-probes rather than trusting them.
    """
    from ApplicationServices import AXUIElementSetAttributeValue
    if not pid or pid in _woken:
        return False
    _woken.add(pid)
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        AXUIElementSetAttributeValue(root, attr, True)
    return True


def _click_global(app: str, win: Window, gx: float, gy: float,
                  button: str = "left", clicks: int = 1,
                  box: tuple | None = None, text: str | None = None) -> dict:
    """Click a point inside a captured window, by the safest route available.

    1. Accessibility action on the element under the point — no focus needed.
    2. A real mouse click, only if macOS's own hit test says it reaches *this*
       window, so it cannot land in whatever happens to be on top.
    Otherwise it refuses and says why.
    """
    import pyautogui

    now = _window_now(win.id)
    if now is None:
        raise TargetError(f"the window {win.describe()} has closed since it was captured.")
    if now.bounds() != win.bounds():
        raise TargetError(f"the window moved or resized since it was captured "
                          f"(was {win.bounds()}, now {now.bounds()}). Capture again.")
    if not (win.x <= gx <= win.x + win.w and win.y <= gy <= win.y + win.h):
        raise TargetError(f"({int(gx)},{int(gy)}) is outside {win.describe()} — refusing.")
    base = {"at_points": (round(gx), round(gy)), "window": win.describe()}

    ax = _ax_click(app, win, gx, gy, button, clicks, box, text)
    if ax:
        return {**base, **ax, "frontmost": frontmost_app()}

    front = ensure_front(app, pid=win.pid)
    if not click_reaches(win, gx, gy):
        raise TargetError(
            f"nothing under ({int(gx)},{int(gy)}) accepts an accessibility click, and a mouse "
            f"click there would not reach {win.describe()} — another window is on top "
            f"(front app: {frontmost_app()!r}). Make the target visible, or use a "
            "browser tool for web pages.")
    pyautogui.click(gx, gy, clicks=clicks, interval=0.06, button=button)
    return {**base, "method": "mouse", "was_frontmost": front,
            "frontmost_after": frontmost_app()}


def click_text(app: str, text: str, window: str | None = None, exact: bool = False,
               occurrence: int | None = None, button: str = "left", clicks: int = 1,
               expect: str | None = None, expect_timeout: float = 6.0) -> dict:
    """Find `text` in the app's window, then click it — with the app proven in front.

    With `expect`, keeps re-reading the window after the click until that text
    appears (or times out), so success is verified rather than assumed.
    """
    cap, hit = find_text(app, text, window, exact, occurrence)
    fx, fy = hit.center()
    gx, gy = cap.full_to_global(fx, fy)
    gbox = (*cap.full_to_global(hit.x0, hit.y0), *cap.full_to_global(hit.x1, hit.y1))
    result = _click_global(app, cap.window, gx, gy, button, clicks, gbox, text)
    result["target_text"] = hit.text
    result["confidence"] = round(hit.conf, 2)
    if expect:
        result["expect"] = expect
        result["expect_seen"] = wait_for_text(app, expect, window, timeout=expect_timeout)
    return result


def click_in_window(app: str, x: float, y: float, button: str = "left",
                    clicks: int = 1) -> dict:
    """Click (x, y) given in the most recent screenshot_app image of `app`."""
    cap = last_capture(app)
    if cap is None:
        raise TargetError(f"no screenshot of {app!r} yet — call screenshot_app first, "
                          "so the coordinates mean something.")
    if not (0 <= x <= cap.returned.width and 0 <= y <= cap.returned.height):
        raise TargetError(f"({x},{y}) is outside the {cap.returned.width}x"
                          f"{cap.returned.height} screenshot.")
    gx, gy = cap.returned_to_global(x, y)
    return _click_global(app, cap.window, gx, gy, button, clicks)


def wait_for_text(app: str, text: str, window: str | None = None,
                  timeout: float = 6.0, poll: float = 0.6) -> bool:
    """Poll the window until `text` is visible. Polls ground truth; never sleeps a guess."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            cap = capture(app, window)
            if match_lines(read_text(cap), text):
                return True
        except TargetError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
