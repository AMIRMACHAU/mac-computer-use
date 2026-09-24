"""Check a whole plan before performing any of it.

A multi-step task that fails at step four has already done steps one to three.
The machine is left in a state nobody designed: a dialog half-filled, a file
renamed but not moved, a message typed but not sent. Recovering from that is
harder than never starting, because the agent has to work out what it already
did — and "what did I already do" is exactly what it is worst at.

So resolve first, act second. Every step that *can* be resolved is resolved
before the first one runs. If step four is ambiguous, you learn that while
nothing has been touched.

Not every step can be pre-resolved, and pretending otherwise would be worse than
useless. A step targeting a Save dialog cannot be checked before the step that
opens the dialog exists. Those are marked `defer` and reported as unverified
rather than quietly assumed — the caller sees exactly how much of the plan was
provable and decides whether that is enough.
"""

from __future__ import annotations

import time
from typing import Any

import targeting

ACTIONS = ("press", "type", "menu", "open_app", "wait_for")


class PlanError(Exception):
    pass


def _describe_step(i: int, s: dict) -> str:
    a = s.get("action", "?")
    bits = [f"{a}"]
    if s.get("app"):
        bits.append(f"app={s['app']!r}")
    if s.get("window"):
        bits.append(f"window~{s['window']!r}")
    if s.get("label"):
        bits.append(f"label={s['label']!r}")
    if s.get("role"):
        bits.append(f"role={s['role']}")
    if s.get("path"):
        bits.append(f"path={s['path']!r}")
    if s.get("text") is not None:
        bits.append(f"text={len(s['text'])}ch")
    return f"[{i}] " + " ".join(bits)


def _validate(steps: list[dict]) -> None:
    for i, s in enumerate(steps, 1):
        if s.get("action") not in ACTIONS:
            raise PlanError(f"step {i}: action must be one of {ACTIONS}, got {s.get('action')!r}")
        if s["action"] in ("press", "type", "wait_for") and not s.get("app"):
            raise PlanError(f"step {i}: {s['action']} needs an 'app'")
        if s["action"] == "type" and s.get("text") is None:
            raise PlanError(f"step {i}: type needs 'text'")
        if s["action"] == "menu" and not s.get("path"):
            raise PlanError(f"step {i}: menu needs a 'path' like 'File > Save'")
        if s["action"] == "open_app" and not s.get("app"):
            raise PlanError(f"step {i}: open_app needs an 'app'")


def check(steps: list[dict]) -> dict:
    """Resolve everything resolvable. Touches nothing.

    Returns a per-step verdict: resolved, deferred (cannot be known yet), or
    blocked (a refusal that will not fix itself). `ready` is true only when no
    step is blocked.
    """
    _validate(steps)
    results, blocked, deferred = [], 0, 0

    for i, s in enumerate(steps, 1):
        d = _describe_step(i, s)

        if s.get("defer"):
            deferred += 1
            results.append({"step": i, "desc": d, "verdict": "deferred",
                            "detail": s.get("because", "target appears only after earlier steps")})
            continue

        if s["action"] in ("open_app", "wait_for"):
            # open_app creates its own target; wait_for is explicitly about a
            # thing that is not there yet. Neither is checkable in advance.
            deferred += 1
            results.append({"step": i, "desc": d, "verdict": "deferred",
                            "detail": f"{s['action']} is resolved at run time by design"})
            continue

        try:
            if s["action"] == "menu":
                leaf = [p.strip() for p in s["path"].split(">") if p.strip()][-1]
                n = targeting.resolve(s["app"], leaf, "AXMenuItem", exact=True)
            else:
                n = targeting.resolve(s["app"], s.get("label"), s.get("role"),
                                      s.get("exact", False),
                                      actionable_only=(s["action"] == "press"),
                                      window=s.get("window"))
            results.append({"step": i, "desc": d, "verdict": "resolved",
                            "detail": n.describe()[:110]})
        except targeting.TargetError as e:
            blocked += 1
            results.append({"step": i, "desc": d, "verdict": "blocked",
                            "detail": f"{type(e).__name__}: {e}"})

    return {"ready": blocked == 0, "steps": len(steps), "resolved": len(steps) - blocked - deferred,
            "deferred": deferred, "blocked": blocked, "results": results}


def run(steps: list[dict], require_ready: bool = True) -> dict:
    """Check the plan, then perform it. Stops at the first failure.

    With require_ready (the default) a single blocked step aborts before anything
    is done — the whole point. Pass False only when you have read the check and
    accept starting a plan that is known not to complete.

    The result always includes what was actually performed, so a partial run can
    be reasoned about instead of guessed at.
    """
    pre = check(steps)
    if require_ready and not pre["ready"]:
        return {"executed": False, "reason": "plan blocked before any action was taken",
                "check": pre, "performed": []}

    performed: list[dict] = []
    for i, s in enumerate(steps, 1):
        d = _describe_step(i, s)
        try:
            a = s["action"]
            if a == "open_app":
                out = targeting.activate_app(s["app"]) if _running(s["app"]) \
                      else targeting.launch_app(s["app"])
            elif a == "wait_for":
                out = targeting.wait_for(s["app"], s.get("label"), s.get("role"),
                                         s.get("exact", False),
                                         s.get("timeout", 10.0),
                                         window=s.get("window")).describe()
            elif a == "press":
                out = targeting.press(s["app"], s.get("label"), s.get("role"),
                                      s.get("exact", False), s.get("window"))
            elif a == "type":
                out = targeting.type_into(s["app"], s["text"], s.get("label"),
                                          s.get("role"), s.get("exact", False),
                                          s.get("clear", False), s.get("window"))
            elif a == "menu":
                out = targeting.press_menu_path(s["app"], s["path"])
            performed.append({"step": i, "desc": d, "ok": True, "result": out})
        except (targeting.TargetError, PlanError) as e:
            performed.append({"step": i, "desc": d, "ok": False,
                              "error": f"{type(e).__name__}: {e}"})
            return {"executed": True, "completed": False,
                    "reason": f"stopped at step {i} of {len(steps)}",
                    "check": pre, "performed": performed,
                    "remaining": [_describe_step(j, t)
                                  for j, t in enumerate(steps[i:], i + 1)]}
        if s.get("settle"):
            time.sleep(float(s["settle"]))

    return {"executed": True, "completed": True, "check": pre, "performed": performed}


def _running(app_name: str) -> bool:
    try:
        targeting._app_element(app_name)
        return True
    except targeting.NotFound:
        return False


def format_check(rep: dict) -> str:
    """The check as something a person can read at a glance."""
    icon = {"resolved": "ok  ", "deferred": "defer", "blocked": "BLOCK"}
    head = (f"{'READY' if rep['ready'] else 'NOT READY'} — {rep['steps']} steps: "
            f"{rep['resolved']} resolved, {rep['deferred']} deferred, {rep['blocked']} blocked")
    lines = [head]
    for r in rep["results"]:
        lines.append(f"  {icon[r['verdict']]:6} {r['desc']}")
        lines.append(f"         {r['detail'][:150]}")
    if not rep["ready"]:
        lines.append("\nNothing was performed. Fix the blocked steps and check again.")
    elif rep["deferred"]:
        lines.append(f"\n{rep['deferred']} step(s) could not be checked in advance and will "
                     "be resolved at run time — they can still fail mid-plan.")
    return "\n".join(lines)
