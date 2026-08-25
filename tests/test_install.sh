#!/usr/bin/env bash
# Wiring tests for install.sh. The Python suites cover what gnomon renders;
# these cover what the installer does to your machine -- which is the part that
# can only be got wrong once, on someone else's laptop.
#
# install.sh no longer runs the suites -- that is ./test.sh -- so calling it here
# is cheap and there is no bypass flag to thread through.
#
#   ./tests/test_install.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/gnomon-install-test.XXXXXX")"
trap 'rm -rf "$SANDBOX"' EXIT
OK=0; FAIL=0

check() {  # check <name> <got> <want>
  if [ "$2" = "$3" ]; then OK=$((OK+1)); printf "  PASS  %-52s got=%s\n" "$1" "$2"
  else FAIL=$((FAIL+1)); printf "  FAIL  %-52s got=%s want=%s\n" "$1" "$2" "$3"; fi
}

run() {  # run <home> [repo] -> exit code, stderr captured in $ERR
  ERR="$(HOME="$1" bash "${2:-$REPO}/install.sh" 2>&1 >/dev/null)"
  return $?
}

echo
echo "[1] a machine that has never seen gnomon"
H="$SANDBOX/virgin"; mkdir -p "$H"
run "$H"; check "exit 0 with no ~/.claude at all" "$?" "0"
check "script deployed" "$([ -f "$H/.claude/gnomon.py" ] && echo yes || echo no)" "yes"
check "settings.json written from nothing" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['command'])")" \
  "python3 ~/.claude/gnomon.py"
check "installer does not create the state DB" \
  "$([ -f "$H/.claude/gnomon-state.db" ] && echo yes || echo no)" "no"
# Any clone can deploy; the sidecar is what makes a stale-clone deploy legible.
check "provenance names the deploying repo" \
  "$(grep '^source=' "$H/.claude/gnomon.provenance" 2>/dev/null)" "source=$REPO"
check "provenance records a commit" \
  "$(grep -c '^commit=[0-9a-f]\{7,\}$' "$H/.claude/gnomon.provenance" 2>/dev/null)" "1"

echo
echo "[2] idempotent -- three runs converge, they do not accumulate"
H="$SANDBOX/idem"; mkdir -p "$H/.claude"
cat > "$H/.claude/settings.json" <<'JSON'
{
  "theme": "dark",
  "statusLine": {"type": "command", "command": "python3 ~/.claude/OLD.py",
                 "hideVimModeIndicator": true},
  "permissions": {"allow": ["Bash(ls:*)"]}
}
JSON
run "$H"; A="$(shasum < "$H/.claude/settings.json")$(shasum < "$H/.claude/gnomon.provenance")"
run "$H"; B="$(shasum < "$H/.claude/settings.json")$(shasum < "$H/.claude/gnomon.provenance")"
run "$H"; C="$(shasum < "$H/.claude/settings.json")$(shasum < "$H/.claude/gnomon.provenance")"
# Both artifacts, not just settings.json -- the sidecar carries no timestamp
# precisely so that it converges too.
check "runs 2 and 3 change nothing" "$([ "$A" = "$B" ] && [ "$B" = "$C" ] && echo same || echo drifted)" "same"
check "unrelated top-level keys survive" \
  "$(python3 -c "import json;d=json.load(open('$H/.claude/settings.json'));print(d['theme'],d['permissions']['allow'][0])")" \
  "dark Bash(ls:*)"
check "unknown statusLine keys survive" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['hideVimModeIndicator'])")" \
  "True"
check "a stale command is corrected" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['command'])")" \
  "python3 ~/.claude/gnomon.py"

# Wiring converges; tuning is seeded once and then belongs to the user. The
# checks above would all stay green if this were wrong -- they only ever install
# over values this installer itself wrote. Tune one first, then install.
H="$SANDBOX/tuned"; mkdir -p "$H/.claude"
cat > "$H/.claude/settings.json" <<'JSON'
{"statusLine": {"type": "command", "command": "python3 ~/.claude/OLD.py",
                "padding": 2, "refreshInterval": 3}}
JSON
run "$H"
check "a tuned refreshInterval survives a re-run" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['refreshInterval'])")" \
  "3"
check "a tuned padding survives too" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['padding'])")" \
  "2"
check "but wiring is still corrected around them" \
  "$(python3 -c "import json;print(json.load(open('$H/.claude/settings.json'))['statusLine']['command'])")" \
  "python3 ~/.claude/gnomon.py"
run "$H"; D="$(shasum < "$H/.claude/settings.json")"
run "$H"
check "and tuned values still converge across runs" \
  "$([ "$D" = "$(shasum < "$H/.claude/settings.json")" ] && echo same || echo drifted)" "same"

# On a machine with no opinion yet, the defaults are still seeded.
check "virgin install seeds the defaults" \
  "$(python3 -c "import json;d=json.load(open('$SANDBOX/virgin/.claude/settings.json'))['statusLine'];print(d['padding'],d['refreshInterval'])")" \
  "0 10"

echo
echo "[2b] provenance degrades rather than failing"
# Must survive a tarball with no .git and no network -- the git probe is
# best-effort, and losing it costs the commit, never the install.
NOGIT="$SANDBOX/nogit-repo"; cp -R "$REPO" "$NOGIT"; rm -rf "$NOGIT/.git"
H="$SANDBOX/nogit"; mkdir -p "$H"
run "$H" "$NOGIT"; check "install still exits 0 without .git" "$?" "0"
check "script still deployed" \
  "$([ -f "$H/.claude/gnomon.py" ] && echo yes || echo no)" "yes"
check "provenance says so rather than lying" \
  "$(grep '^commit=' "$H/.claude/gnomon.provenance" 2>/dev/null)" "commit=unknown"
# install.sh normalises its own path with `cd && pwd`; $TMPDIR can carry a
# trailing slash, so normalise the expectation the same way rather than
# comparing raw strings.
check "and still names the source" \
  "$(grep '^source=' "$H/.claude/gnomon.provenance" 2>/dev/null)" \
  "source=$(cd "$NOGIT" && pwd)"

# A dirty source is legal but must be recorded -- "why does the deployed script
# not match what I shipped" is exactly the question this answers.
DIRTY="$SANDBOX/dirty-repo"; cp -R "$REPO" "$DIRTY"
printf '\n# local edit\n' >> "$DIRTY/gnomon.py"
H="$SANDBOX/dirtyhome"; mkdir -p "$H"
run "$H" "$DIRTY"
check "a dirty checkout is flagged" \
  "$(grep '^dirty=' "$H/.claude/gnomon.provenance" 2>/dev/null)" "dirty=yes"

echo
echo "[3] malformed settings.json -- refuse, and change nothing"
H="$SANDBOX/bad"; mkdir -p "$H/.claude"
printf '{\n  "theme": "dark",\n  // a stray comment\n}\n' > "$H/.claude/settings.json"
BEFORE="$(shasum < "$H/.claude/settings.json")"
run "$H"; check "exits non-zero" "$([ "$?" -ne 0 ] && echo nonzero || echo zero)" "nonzero"
check "says which file, in words" \
  "$(echo "$ERR" | grep -qi "not valid JSON" && echo explained || echo "$ERR")" "explained"
check "no Python traceback" \
  "$(echo "$ERR" | grep -q "Traceback" && echo traceback || echo clean)" "clean"
check "settings.json untouched" \
  "$([ "$BEFORE" = "$(shasum < "$H/.claude/settings.json")" ] && echo same || echo modified)" "same"
# The whole point of staging: a failure must not leave a half-install behind.
check "script NOT deployed -- all or nothing" \
  "$([ -f "$H/.claude/gnomon.py" ] && echo deployed || echo absent)" "absent"
check "no staging file left behind" \
  "$(ls "$H/.claude/" | grep -c staged)" "0"

echo
echo "[4] settings.json that parses but is the wrong shape"
H="$SANDBOX/shape"; mkdir -p "$H/.claude"
echo '["not", "an", "object"]' > "$H/.claude/settings.json"
run "$H"; check "exits non-zero" "$([ "$?" -ne 0 ] && echo nonzero || echo zero)" "nonzero"
check "names the shape problem" \
  "$(echo "$ERR" | grep -qi "top level is a list" && echo explained || echo "$ERR")" "explained"
check "script NOT deployed" \
  "$([ -f "$H/.claude/gnomon.py" ] && echo deployed || echo absent)" "absent"

echo
echo "[5] a script too broken to run must never be installed"
# The smoke check is all that stands between a bad edit and something that
# executes on every keystroke. Corrupt a throwaway copy of the repo and confirm
# install.sh refuses it.
BROKEN="$SANDBOX/broken-repo"
cp -R "$REPO" "$BROKEN"
rm -rf "$BROKEN/.git"
printf '\nthis is not python(\n' >> "$BROKEN/gnomon.py"
H="$SANDBOX/broken"; mkdir -p "$H"
run "$H" "$BROKEN"; check "exits non-zero" "$([ "$?" -ne 0 ] && echo nonzero || echo zero)" "nonzero"
check "says the script would not run" \
  "$(echo "$ERR" | grep -qi "failed to run" && echo explained || echo "$ERR")" "explained"
check "nothing deployed" \
  "$([ -f "$H/.claude/gnomon.py" ] && echo deployed || echo absent)" "absent"
check "settings.json never created" \
  "$([ -f "$H/.claude/settings.json" ] && echo created || echo absent)" "absent"

echo
echo "[6] argument handling"
H="$SANDBOX/args"; mkdir -p "$H"
HOME="$H" bash "$REPO/install.sh" --skip-tests >/dev/null 2>&1
check "the retired --skip-tests flag exits 2 without installing" \
  "$?:$([ -f "$H/.claude/gnomon.py" ] && echo deployed || echo absent)" "2:absent"

echo
if [ "$FAIL" -eq 0 ]; then echo "ALL PASS  ($OK checks)"; else echo "FAILURES  ($OK pass, $FAIL fail)"; fi
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
