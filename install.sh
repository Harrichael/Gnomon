#!/usr/bin/env bash
# Deploy the status line: copy the script into ~/.claude and register it in
# settings.json. Idempotent -- safe to re-run after editing statusline.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/statusline.py"
SETTINGS="$HOME/.claude/settings.json"

# Test the repo copy before it becomes the live one.
SL_SCRIPT="$HERE/statusline.py" python3 "$HERE/tests/test_render.py"
SL_SCRIPT="$HERE/statusline.py" python3 "$HERE/tests/test_cost_state.py"

mkdir -p "$HOME/.claude"
cp "$HERE/statusline.py" "$DEST"

python3 - "$SETTINGS" <<'PY'
import collections, json, os, sys
p = sys.argv[1]
d = {}
if os.path.exists(p):
    with open(p) as f:
        d = json.load(f, object_pairs_hook=collections.OrderedDict)
# Merge rather than overwrite: settings.json holds unrelated preferences.
sl = d.get("statusLine") or collections.OrderedDict()
sl.update({"type": "command", "command": "python3 ~/.claude/statusline.py",
           "padding": 0, "refreshInterval": 10})
d["statusLine"] = sl
with open(p, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
print("registered statusLine in", p)
PY

echo "installed -> $DEST"
echo "note: refreshInterval is read at startup, so restart open sessions."
