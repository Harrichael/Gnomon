#!/usr/bin/env bash
# Run every suite against the repo copy. This is the test command; install.sh
# installs and nothing else.
#
#   ./test.sh              all three suites
#   ./test.sh render       just one, by name (render | cost | install)
#
# The Python suites default to the DEPLOYED script so you can check what is
# actually running on your machine; pointing them at the repo copy is what you
# want before installing, so that is what this does.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONLY="${1:-all}"
FAILED=""

suite() {  # suite <name> <command...>
  local name="$1"; shift
  [ "$ONLY" = "all" ] || [ "$ONLY" = "$name" ] || return 0
  echo
  echo "══ $name ═══════════════════════════════════════════════════════════"
  if "$@"; then :; else FAILED="$FAILED $name"; fi
}

suite render  env SL_SCRIPT="$HERE/gnomon.py" python3 "$HERE/tests/test_render.py"
suite cost    env SL_SCRIPT="$HERE/gnomon.py" python3 "$HERE/tests/test_cost_state.py"
suite install bash "$HERE/tests/test_install.sh"

echo
if [ -n "$FAILED" ]; then
  echo "FAILURES in:$FAILED"
  exit 1
fi
[ "$ONLY" = "all" ] && echo "ALL SUITES PASS" || echo "PASS ($ONLY)"
exit 0
