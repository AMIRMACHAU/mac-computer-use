"""Agent loop: Claude drives this Mac. Usage: run.py "task" [--dry-run]"""
import sys, anthropic, executor

DRY = "--dry-run" in sys.argv
executor.DRY_RUN = DRY
task = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
if not task:
    sys.exit('usage: run.py "what Claude should do" [--dry-run]')

TOOLS = [{
    "type": "computer_toolset_20260801",
    "configs": {"zoom": {"enabled": True}},
    "cache_control": {"type": "ephemeral"},
}]

client = anthropic.Anthropic()          # picks up the OAuth profile, no key
messages = [{"role": "user", "content": task}]

if DRY:
    print("DRY RUN - screenshots real, input suppressed\n")
print(f"task: {task}\n" + "-" * 60)

while True:
    with client.messages.stream(
        model="claude-opus-5",
        max_tokens=64000,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "high"},
        tools=TOOLS,
        messages=messages,
    ) as stream:
        resp = stream.get_final_message()

    for b in resp.content:
        if b.type == "text" and b.text.strip():
            print(b.text.strip())

    messages.append({"role": "assistant", "content": resp.content})

    if resp.stop_reason == "refusal":
        print("refused:", resp.stop_details); break
    if resp.stop_reason != "tool_use":
        break

    results, failed = [], False
    for b in resp.content:
        if b.type != "tool_use":
            continue
        if failed:
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "toolset_name": "computer", "is_error": True,
                            "content": "Not executed: an earlier computer "
                                       "action in this turn failed."})
            continue
        print(f"  -> {b.name} {b.input if b.name != 'type' else '...'}")
        try:
            content, err = executor.execute(b.name, b.input), False
        except Exception as e:
            content, err, failed = str(e), True, True
            print(f"  !! {e}")
        results.append({"type": "tool_result", "tool_use_id": b.id,
                        "toolset_name": "computer",
                        "content": content, "is_error": err})

    messages.append({"role": "user", "content": results})

u = resp.usage
print("-" * 60)
print(f"in {u.input_tokens}  out {u.output_tokens}  "
      f"cache_read {getattr(u, 'cache_read_input_tokens', 0)}")