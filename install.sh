#!/usr/bin/env bash
# Install gnomon: copy the script into ~/.claude and register it in
# settings.json. Idempotent -- safe to re-run after editing gnomon.py.
#
# This installs. It does not test; that is ./test.sh, and it is worth running
# before you install. What happens here instead is a smoke check: render the
# sample payload once and require a clean exit. It costs ~0.1s and catches the
# failure that actually matters at this moment -- a script too broken to run at
# all, on its way to something that executes on every keystroke.
#
# All-or-nothing. The merged settings are staged BEFORE anything is copied, so
# any failure below leaves you exactly as you were. It used to fail halfway:
# script deployed, nothing registered, a raw JSONDecodeError as the only clue.
set -euo pipefail

if [ -n "${1:-}" ]; then
  echo "usage: install.sh          (run ./test.sh separately)" >&2
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/gnomon.py"
SETTINGS="$HOME/.claude/settings.json"
STAGED="$SETTINGS.staged.$$"
SMOKE="$(mktemp -d "${TMPDIR:-/tmp}/gnomon-smoke.XXXXXX")"
trap 'rm -f "$STAGED"; rm -rf "$SMOKE"' EXIT

# Smoke check. Redirected at a scratch database and with the network off, so it
# cannot touch your state or stall on an API call.
if ! CC_SL_STATE_DB="$SMOKE/state.db" CC_SL_NO_API=1 \
     python3 "$HERE/gnomon.py" < "$HERE/tests/fixtures/sample_stdin.json" \
     > "$SMOKE/out" 2> "$SMOKE/err"; then
  echo "gnomon: $HERE/gnomon.py failed to run -- nothing installed." >&2
  sed 's/^/        /' "$SMOKE/err" >&2
  exit 1
fi
if [ ! -s "$SMOKE/out" ]; then
  echo "gnomon: $HERE/gnomon.py produced no output -- nothing installed." >&2
  exit 1
fi

mkdir -p "$HOME/.claude"

# Stage the merged settings. Nothing of yours has been written yet, so every
# failure below this point is a clean no-op.
python3 - "$SETTINGS" "$STAGED" <<'PY'
import collections, json, os, sys

src, staged = sys.argv[1], sys.argv[2]
d = collections.OrderedDict()
if os.path.exists(src):
    try:
        with open(src) as f:
            d = json.load(f, object_pairs_hook=collections.OrderedDict)
    except ValueError as e:
        sys.stderr.write(
            "\ngnomon: cannot update %s -- it is not valid JSON.\n"
            "        %s\n\n"
            "        Nothing was installed and the file was not touched.\n"
            "        Fix the syntax (or move it aside) and run this again.\n\n"
            % (src, e))
        sys.exit(1)
    if not isinstance(d, dict):
        sys.stderr.write(
            "\ngnomon: cannot update %s -- its top level is a %s, and Claude\n"
            "        Code expects an object.\n\n"
            "        Nothing was installed and the file was not touched.\n\n"
            % (src, type(d).__name__))
        sys.exit(1)

# Merge rather than overwrite: settings.json holds unrelated preferences, and
# the statusLine block itself may carry keys this installer knows nothing about.
sl = d.get("statusLine")
if not isinstance(sl, dict):
    sl = collections.OrderedDict()

# Wiring vs tuning. `type` and `command` say WHERE the status line comes from --
# the installer owns those forever, so re-running always corrects a stale path.
# `padding` and `refreshInterval` are TUNING, and belong to whoever is looking at
# the thing. Forcing them on every run was a real bug once this was installed by
# a bootstrap that reruns on every pass: a tuned refreshInterval was reverted by
# a bootstrap invoked for entirely unrelated reasons. Seed them, then let go.
sl.update({"type": "command", "command": "python3 ~/.claude/gnomon.py"})
sl.setdefault("padding", 0)
sl.setdefault("refreshInterval", 10)
d["statusLine"] = sl

with open(staged, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PY

# Both halves are known good. Commit them.
cp "$HERE/gnomon.py" "$DEST"
mv "$STAGED" "$SETTINGS"

echo "registered statusLine in $SETTINGS"
echo "installed -> $DEST"
echo "note: refreshInterval is read at startup, so restart open sessions."
