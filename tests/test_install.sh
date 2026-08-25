#!/usr/bin/env bash
# Wiring tests for install.sh. The Python suites cover what gnomon renders;
# these cover what the installer does to your machine -- which is the part that
# can only be got wrong once, on someone else's laptop.
#
# Runs install.sh with --skip-tests throughout: the suites are exercised by
# install.sh itself, and running them five more times costs 90s for no coverage.
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

run() {  # run <home> -> exit code, stderr captured in $ERR
  ERR="$(HOME="$1" bash "$REPO/install.sh" --skip-tests 2>&1 >/dev/null)"
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
run "$H"; A="$(shasum < "$H/.claude/settings.json")"
run "$H"; B="$(shasum < "$H/.claude/settings.json")"
run "$H"; C="$(shasum < "$H/.claude/settings.json")"
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
echo "[5] argument handling"
H="$SANDBOX/args"; mkdir -p "$H"
HOME="$H" bash "$REPO/install.sh" --nonsense >/dev/null 2>&1
check "an unknown flag exits 2 without installing" \
  "$?:$([ -f "$H/.claude/gnomon.py" ] && echo deployed || echo absent)" "2:absent"

echo
if [ "$FAIL" -eq 0 ]; then echo "ALL PASS  ($OK checks)"; else echo "FAILURES  ($OK pass, $FAIL fail)"; fi
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
