"""macOS executor for Claude's computer_toolset_20260801. No Docker, no VM."""
import base64, io, subprocess, tempfile, contextlib
import pyautogui
from PIL import Image

pyautogui.FAILSAFE = True          # cursor to a screen corner aborts
pyautogui.PAUSE = 0.05

TARGET_LONG_EDGE = 1280            # what the API sees; keep <=1280 for cost/latency
DRY_RUN = False                    # True = log actions, never touch the cursor

_pt_per_img = 1.0                  # points  per screenshot px, set by _capture()
_px_per_img = 1.0                  # native px per screenshot px, set by _capture()

# Claude emits X11 key names; pyautogui on macOS wants these.
KEYMAP = {
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc", "BackSpace": "backspace",
    "Tab": "tab", "space": "space", "Delete": "delete", "Prior": "pageup",
    "Next": "pagedown", "Home": "home", "End": "end", "Up": "up", "Down": "down",
    "Left": "left", "Right": "right", "ctrl": "ctrl", "control": "ctrl",
    "alt": "option", "Alt_L": "option", "super": "command", "Super_L": "command",
    "cmd": "command", "meta": "command", "shift": "shift",
}
def _keys(text):
    return [KEYMAP.get(k, KEYMAP.get(k.lower(), k.lower())) for k in text.split("+")]


def _raw():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    r = subprocess.run(["screencapture", "-x", "-t", "png", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            "screencapture failed: " + (r.stderr.strip() or "unknown") +
            "  -> grant Screen Recording in System Settings > Privacy & Security")
    return Image.open(path).convert("RGB")


def _encode(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return [{"type": "image", "source": {"type": "base64",
             "media_type": "image/png",
             "data": base64.b64encode(buf.getvalue()).decode()}}]


def _capture():
    global _pt_per_img, _px_per_img
    full = _raw()
    img = full.copy()
    img.thumbnail((TARGET_LONG_EDGE, TARGET_LONG_EDGE), Image.LANCZOS)
    _pt_per_img = pyautogui.size().width / img.width
    _px_per_img = full.width / img.width
    return img


def _pt(c):
    return c[0] * _pt_per_img, c[1] * _pt_per_img


OK = [{"type": "text", "text": "OK"}]
_null = contextlib.nullcontext


def execute(name, inp):
    if DRY_RUN and name not in ("screenshot", "zoom", "cursor_position"):
        print(f"  [dry-run] {name} {inp}")
        return OK

    if name == "screenshot":
        return _encode(_capture())

    if name == "zoom":
        x0, y0, x1, y1 = inp["region"]
        full = _raw()
        s = _px_per_img
        crop = full.crop((int(x0*s), int(y0*s), int(x1*s), int(y1*s)))
        crop.thumbnail((TARGET_LONG_EDGE, TARGET_LONG_EDGE), Image.LANCZOS)
        return _encode(crop)

    if name in ("left_click", "right_click", "middle_click",
                "double_click", "triple_click"):
        if inp.get("coordinate"):
            pyautogui.moveTo(*_pt(inp["coordinate"]))
        mods = _keys(inp["text"]) if inp.get("text") else []
        button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
        clicks = {"double_click": 2, "triple_click": 3}.get(name, 1)
        with (pyautogui.hold(mods) if mods else _null()):
            pyautogui.click(button=button, clicks=clicks, interval=0.06)
        return OK

    if name == "left_click_drag":
        pyautogui.moveTo(*_pt(inp["start_coordinate"]))
        mods = _keys(inp["text"]) if inp.get("text") else []
        with (pyautogui.hold(mods) if mods else _null()):
            pyautogui.dragTo(*_pt(inp["coordinate"]), duration=0.4, button="left")
        return OK

    if name == "mouse_move":
        pyautogui.moveTo(*_pt(inp["coordinate"])); return OK
    if name == "left_mouse_down":
        pyautogui.mouseDown(); return OK
    if name == "left_mouse_up":
        pyautogui.mouseUp(); return OK
    if name == "cursor_position":
        x, y = pyautogui.position()
        return [{"type": "text",
                 "text": f"({int(x/_pt_per_img)}, {int(y/_pt_per_img)})"}]

    if name == "scroll":
        if inp.get("coordinate"):
            pyautogui.moveTo(*_pt(inp["coordinate"]))
        d, amt = inp["scroll_direction"], inp.get("scroll_amount", 3)
        mods = _keys(inp["text"]) if inp.get("text") else []
        fn = pyautogui.hscroll if d in ("left", "right") else pyautogui.scroll
        with (pyautogui.hold(mods) if mods else _null()):
            fn(amt * (-1 if d in ("down", "right") else 1))
        return OK

    if name == "type":
        pyautogui.write(inp["text"], interval=0.012); return OK
    if name == "key":
        for _ in range(min(inp.get("repeat", 1), 100)):
            pyautogui.hotkey(*_keys(inp["text"]))
        return OK
    if name == "hold_key":
        with pyautogui.hold(_keys(inp["text"])):
            pyautogui.sleep(min(inp["duration"], 300))
        return OK
    if name == "wait":
        pyautogui.sleep(min(inp["duration"], 300)); return OK

    raise ValueError(f"unknown computer tool: {name}")
