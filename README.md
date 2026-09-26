# mac-computer-use

An MCP server that gives **your** Claude eyes, a mouse and a keyboard on
**your** Mac. No API key, no agent loop, no cloud: the Claude client you
already use (Claude Desktop, Claude Code) is the brain, this is the hands, and
the client asks you before every action.

> **Prior art:** [Peekaboo](https://github.com/steipete/peekaboo) (MIT) does
> this and more — it also reads the accessibility tree, so it can target
> elements by name instead of by pixel. Look at it before choosing this. This
> project is deliberately tiny (two Python files) so it is easy to read, audit
> and fork.

## What it can do — and what that means

Everything you can do with a mouse and keyboard, it can do: open any app,
click any button, type into any field, including ones with your logged-in
sessions. There is no sandbox. Treat it accordingly:

- **Do not use the Mac while it runs.** You share one keyboard and one cursor.
  If you type, focus moves, and its next keystrokes land in your window.
- **Close anything sensitive first** — password manager, banking, chat drafts.
- **Approve each action** in the client prompt. Do not enable auto-approve.
- **Abort:** slam the cursor into any screen corner (pyautogui failsafe).
- **Unattended runs** belong on a separate macOS user account.

## Requirements

macOS on Apple Silicon or Intel, Python 3.10+, and two permissions.

### Permissions — read this, it is the #1 support question

macOS grants **Screen Recording** and **Accessibility** to the *parent
application* of a process. This server is a child of whichever client starts
it, so the permission goes to the **client**, not to Python or Terminal:

| You run it from | Grant both permissions to |
|---|---|
| Claude Desktop | **Claude** |
| Claude Code in a terminal | that terminal app (Terminal, iTerm, …) |
| Claude Code inside the Claude desktop app | **Claude** |

System Settings → Privacy & Security → *Screen & System Audio Recording* and
*Accessibility*. Then **fully quit and relaunch the client** — macOS does not
apply Screen Recording to a process that is already running. A wrong or
un-relaunched grant shows up as `could not create image from display`.

## Install

```bash
git clone <this repo> && cd mac-computer-use
python3 -m venv .venv && .venv/bin/pip install -e .
```

## Register with your Claude

**Claude Desktop** — `~/Library/Application Support/Claude/claude_desktop_config.json`
(use absolute paths; start in dry run):

```json
{
  "mcpServers": {
    "mac-computer-use": {
      "command": "/ABSOLUTE/PATH/mac-computer-use/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/mac-computer-use/server.py"],
      "env": { "DRY_RUN": "1" }
    }
  }
}
```

**Claude Code:**

```bash
claude mcp add mac-computer-use -e DRY_RUN=1 -- /ABSOLUTE/PATH/mac-computer-use/.venv/bin/python /ABSOLUTE/PATH/mac-computer-use/server.py
```

## First run: dry run, then one trivial live task

With `DRY_RUN=1` screenshots are real and every input action is suppressed
(tools return `dry-run: not executed`). Ask your Claude: *"Describe what is on
my screen."* If it describes your desktop, capture works.

Then remove `DRY_RUN`, restart the client, sit in front of the Mac and ask for
something small and visible: *"Open Spotlight, type Calculator, press
Return."* Approve each action. If Calculator opens, clicks and keys are mapped
correctly and you are set up.

## Provable targeting — why this isn't another click-at-XY tool

Pixel clicking is a guess. You compute a coordinate from a screenshot and hope
the thing you meant is still under it. When it isn't, the click still lands — on
whatever moved into that spot. That is how an agent types into the wrong window,
and it is not a rare edge case: it happened here, and it overwrote an unsent
message.

The `*_element` tools remove the guess. An action names what it means to act on,
the element is resolved through the macOS accessibility tree, and the action is
delivered *to that element*. No coordinate exists, so no coordinate can be wrong.

Three rules make that a guarantee rather than a hope:

1. **Exactly one match, or refuse.** Nothing matching raises `NotFound`; several
   matching raises `Ambiguous` and lists the candidates. It never picks for you.
2. **Re-verified at dispatch.** The tree is re-read immediately before acting and
   the element's identity re-checked, so a UI that changed between look and act is
   caught rather than acted on.
3. **Confirmed after.** Every action returns the value before and after, so the
   caller can assert something actually happened.

```
press_element(app="Activity Monitor", label="CPU")
  → REFUSED: Ambiguous: 8 elements match — refusing to choose.
             Candidates: AXMenuItem 'CPU Usage', AXMenuItem 'CPU History', …

press_element(app="Activity Monitor", label="CPU", role="AXRadioButton", exact=True)
  → pressed AXRadioButton 'CPU'   value before: '0'   after: '1'   changed: True
```

Coordinates remain available (`click`, `type_text`) for canvases and custom-drawn
UI that expose no accessibility tree. Everywhere else, prefer the named tools.

**Two documents open?** Every role-only lookup then matches once per window and is
refused as ambiguous — correct, but not useful on its own. Pass `window=` (a
substring of the title, from `list_windows`) to say which one you mean. The
ambiguity error tells you when this is what you need.

`set_field_text` writes directly to the element where it can. Where a field refuses
a programmatic write — web content usually does — it focuses that element,
**confirms the focus actually landed there**, and only then sends keystrokes. If
focus went somewhere else it raises and sends nothing, which is the difference
between a fallback and a hazard.

## Environment memory — don't relearn the same lesson

Every machine has facts that are invisible in code and expensive to discover. On
the machine this was built for, `cmd+space` opens Mission Control rather than
Spotlight. An agent that "knows" the standard shortcut opens the wrong thing and
then types into it. That cost about forty minutes to work out once; without
somewhere to put it, it costs forty minutes again next session.

`recall_environment` is that somewhere, and the server's instructions tell the
model to call it before acting. `remember_environment` adds to it. The store is
JSON at `~/.mac-computer-use/knowledge.json`, seeded with what this machine has
already taught us — tools that report false failures, data sources that are
incomplete, shortcuts that do the wrong thing.

## Plan before you act

A multi-step task that fails at step four has already done steps one to three.
The machine is left in a state nobody designed — a dialog half-filled, a message
typed but not sent — and working out what was already done is the hardest thing
to recover from.

`check_plan` resolves every step that *can* be resolved before the first one runs.
If step four is ambiguous you learn that while nothing has been touched.

```
check_plan([
  {"action": "open_app", "app": "TextEdit"},
  {"action": "type",     "app": "TextEdit", "role": "AXTextArea",
                         "window": "notes", "text": "..."},
  {"action": "menu",     "app": "TextEdit", "path": "File > Save"},
])

READY — 3 steps: 2 resolved, 1 deferred, 0 blocked
  defer  [1] open_app app='TextEdit'      resolved at run time by design
  ok     [2] type window~'notes'          AXTextArea 'placeholder'
  ok     [3] menu path='File > Save'      AXMenuItem 'Save'
```

Not every step can be pre-resolved, and pretending otherwise would be worse than
useless: a step targeting a Save dialog cannot be checked before the step that
opens it. Those are reported as **deferred** rather than quietly assumed, so the
caller sees exactly how much of the plan was provable and decides whether that is
enough. `run_plan` aborts before step one if anything is **blocked**.

## Tools

| Tool | Does |
|---|---|
| `screenshot()` | Full screen → image. Every coordinate below is a pixel in this image. |
| `zoom(x0,y0,x1,y1)` | Region at full resolution, for small text. |
| `click(x,y,button,clicks,modifiers)` | Left/right/middle, single/double/triple, with held modifiers. |
| `move_mouse(x,y)` | Hover; `y=0` reveals an auto-hidden menu bar. |
| `drag(x0,y0,x1,y1)` | Left-button drag. |
| `scroll(x,y,direction,amount)` | Wheel ticks at a point. |
| `type_text(text)` | Types into whatever has focus. |
| `press_keys("cmd+space", repeat)` | Keys and chords. |
| `wait(seconds)` | Pause for the UI. |
| `cursor_position()` | Where the mouse is. |
| `list_windows(app)` | Open window titles — the values to pass as `window`. |
| `describe_ui(app)` | Named, addressable elements — window controls first, then menus. |
| `press_element(app,label,role,exact)` | Press by name. Refuses on missing/ambiguous/changed. |
| `set_field_text(app,text,label,role)` | Write into a **named** field, verified by read-back. |
| `wait_for_element(app,label,role,timeout)` | Wait for async UI. Ambiguity returns at once. |
| `press_menu(app,"Format > Font > Bold")` | Follow a nested menu path. |
| `open_app(app)` | Activate, launching first if needed; waits for a real window. |
| `focused_element(app)` | What has keyboard focus — check before blind typing. |
| `recall_environment(query)` | What this machine already taught us. Call first. |
| `remember_environment(key,fact,tags)` | Record a durable machine fact. |
| `check_plan(steps)` | Resolve a whole multi-step task **without performing any of it**. |
| `run_plan(steps)` | Check, then perform — aborts before step one if anything is blocked. |

Retina scaling is handled inside; screenshots are downscaled to a 1280px long
edge and coordinates map back to real points automatically.

## Tests

```bash
pip install -e ".[dev]"
pytest              # logic only — no screen needed, ~0.3s
pytest -m gui       # also drives a real app; skips if Accessibility is not granted
```

The logic tests stub element resolution, so the suite runs anywhere and touches
nothing. The `gui` ones need a real application and are opt-in.

They are written against bugs that actually shipped, not for coverage. The
packaging tests exist because `py-modules` once listed only the two files the
project started with: the wheel built fine, uploaded fine, and was missing every
module the server imports. Reintroduce that mistake and the suite fails.

## Also in this repo

`run.py` is the *other* way to drive the same executor: a standalone loop
against the Claude API with `computer_toolset_20260801`. It needs an API
credential and bills per screenshot, and has no per-action approval prompt.
Prefer the MCP server.

## Before publishing this for other people

- [ ] Add a LICENSE (MIT recommended) and set `license` in `pyproject.toml`
- [ ] Add a frontmost-app allowlist so typing is refused outside approved apps
- [ ] Test on a clean Mac following only this README
- [ ] Register in the MCP registry
