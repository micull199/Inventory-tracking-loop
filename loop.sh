#!/usr/bin/env bash
# loop.sh — autonomous build loop driver for the UC inventory project.
#
# What this does:
#   1. Runs Claude Code with the project as context.
#   2. Each iteration: instructs Claude to read MISSION.md and PROGRESS.md,
#      pick the next slice, implement it, run the verification harness,
#      self-critique, and commit.
#   3. Between iterations, runs `make check` itself as an independent verification.
#      If it fails, the next iteration is told to fix it before doing anything else.
#   4. Stops when:
#        - BLOCKED.md exists (Claude is stuck), OR
#        - All 12 Definition-of-Done items are ticked in PROGRESS.md, OR
#        - MAX_ITERATIONS is reached, OR
#        - You hit Ctrl-C.
#
# Usage:
#   ./loop.sh                  # run with defaults
#   ./loop.sh --max 50         # cap iterations
#   ./loop.sh --dry-run        # print what would happen without invoking Claude
#
# Requires:
#   - claude (Claude Code CLI) installed and authenticated
#   - git initialised in the project
#   - make available
#
# This script lives at the project root, alongside MISSION.md and PROGRESS.md.

set -euo pipefail

# ---- config ---------------------------------------------------------------

MAX_ITERATIONS="${MAX_ITERATIONS:-100}"
LOG_DIR="${LOG_DIR:-.loop_logs}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max) MAX_ITERATIONS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$LOG_DIR"

# ---- preflight ------------------------------------------------------------

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 2; }
}

require git
require claude
require make

[[ -f MISSION.md ]]  || { echo "MISSION.md not found in $(pwd). Run from project root." >&2; exit 2; }
[[ -f PROGRESS.md ]] || { echo "PROGRESS.md not found in $(pwd). Run from project root." >&2; exit 2; }
[[ -d .git ]]        || { echo ".git not found. Initialise the repo first." >&2; exit 2; }

# ---- helpers --------------------------------------------------------------

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() { printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$LOG_DIR/loop.log"; }

dod_count_ticked() {
  # Counts ticked Definition-of-Done boxes in PROGRESS.md.
  # Looks for lines like "- [x] " under the DoD tracker section. Case-insensitive on the x.
  grep -cE '^- \[[xX]\] ' PROGRESS.md || true
}

dod_total() {
  grep -cE '^- \[[ xX]\] [0-9]+\.' PROGRESS.md || true
}

run_check() {
  # Independent verification: don't trust Claude's self-report. Run our own.
  log "running make check"
  if make check >"$LOG_DIR/check_${ITER}.log" 2>&1; then
    log "make check: passed"
    return 0
  else
    log "make check: FAILED (see $LOG_DIR/check_${ITER}.log)"
    return 1
  fi
}

# Renders the iteration prompt sent to Claude. Stdout is the prompt body.
build_iteration_prompt() {
  local iter="$1"
  local check_status="$2"  # "pass", "fail", or "first"

  cat <<EOF
You are operating in an autonomous build loop for the UC inventory project. This is iteration ${iter}.

Your behaviour is governed by MISSION.md and PROGRESS.md in the current working directory. Read both before doing anything else. Treat MISSION.md as immutable for the duration of this iteration. PROGRESS.md is yours to maintain.

Verification status from the loop runner: ${check_status}.
EOF

  if [[ "$check_status" == "fail" ]]; then
    cat <<EOF

The previous iteration's commit is failing 'make check'. The failure log is at ${LOG_DIR}/check_$((iter-1)).log. Your highest priority this iteration is to fix it. Do not start a new slice until 'make check' is green. If you cannot get it green within this iteration, write to BLOCKED.md per MISSION.md §8 and stop.
EOF
  fi

  cat <<EOF

Workflow for this iteration (from MISSION.md §8):

  1. Read MISSION.md and PROGRESS.md.
  2. If PROGRESS.md "Current slice" is non-empty, you are resuming it. Otherwise pick the next slice from "Next slice" or the Backlog. Smallest end-to-end slice that moves a Definition-of-Done item closer.
  3. Write the plan into PROGRESS.md "Current slice" before coding.
  4. Implement the slice.
  5. Run: 'make lint', 'make typecheck', 'make test', and Playwright tests relevant to the slice. Add new tests for the new behaviour. A slice is not done until it has tests.
  6. Self-critique against MISSION.md §4 (non-functional requirements) and §9 (hard rules). Note weaknesses in PROGRESS.md "Self-critique notes (rolling)".
  7. If the weakest item is genuinely blocking, fix it now. Otherwise queue it as a future slice.
  8. Commit. Commit message format: 'slice: <slice-name> (DoD #<n>)' where <n> is the Definition-of-Done item this advances. Reference multiple DoD items with commas if applicable.
  9. Move the slice from "Current slice" to "Completed slices (log)" in PROGRESS.md.
  10. Refresh "Current state" in PROGRESS.md (iteration number, last commit, tests status, DoD count).
  11. If the next slice is obvious, queue it in "Next slice". Otherwise leave that section empty.
  12. If a Definition-of-Done item is genuinely complete (verified by tests AND a manual sanity-check the loop can do without a human), tick it in PROGRESS.md "Definition of Done tracker". Be conservative: if you are tempted to tick a box because the happy path works, do not tick it yet — wait until the edge cases are covered too.

Hard rules (do not violate):
  - Do not edit MISSION.md.
  - Do not edit past entries in "Completed slices (log)".
  - Do not edit anything in the audit log behaviour to make tests easier.
  - Do not change the tech stack listed in MISSION.md §5. If you have a strong reason, write it to BLOCKED.md and stop.
  - Do not silently expand or shrink scope. Log proposals under "Proposed scope changes" in PROGRESS.md and continue with the original scope.
  - If the same test or problem has failed three iterations running with no measurable progress, stop and write to BLOCKED.md per MISSION.md §8.
  - If you cannot make progress this iteration for any reason, write to BLOCKED.md and stop. Do not produce a no-op commit.

Begin.
EOF
}

invoke_claude() {
  local prompt_file="$1"
  local iter_log="$LOG_DIR/iter_${ITER}.log"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[dry-run] would invoke claude with prompt at $prompt_file"
    log "[dry-run] prompt preview (first 20 lines):"
    head -20 "$prompt_file" | sed 's/^/    /' | tee -a "$LOG_DIR/loop.log"
    return 0
  fi

  log "invoking claude (iteration ${ITER}); output → $iter_log"

  # --dangerously-skip-permissions lets the loop run unattended. This is the
  # right call for a sandboxed project repo, but understand that you are
  # giving Claude full access to this directory and your shell environment.
  # Run inside a dedicated git repo, never in your home directory.
  if claude \
      --dangerously-skip-permissions \
      --print < "$prompt_file" \
      > "$iter_log" 2>&1; then
    log "claude iteration ${ITER}: completed"
    return 0
  else
    local rc=$?
    log "claude iteration ${ITER}: exited non-zero (rc=$rc)"
    return "$rc"
  fi
}

# ---- main loop ------------------------------------------------------------

log "loop starting. max iterations: $MAX_ITERATIONS. log dir: $LOG_DIR."

ITER=0
CHECK_STATUS="first"
CONSECUTIVE_FAILURES=0

while (( ITER < MAX_ITERATIONS )); do
  ITER=$((ITER + 1))
  log "==== iteration $ITER ===="

  # Stop conditions checked at the top of every iteration.

  if [[ -f BLOCKED.md ]]; then
    log "BLOCKED.md exists. Halting loop. Read it and decide next steps."
    exit 1
  fi

  ticked="$(dod_count_ticked)"
  total="$(dod_total)"
  log "DoD progress: ${ticked}/${total} ticked"

  if [[ -n "$total" && "$total" -gt 0 && "$ticked" -ge "$total" ]]; then
    log "All Definition-of-Done items ticked. Loop complete."
    log "Run a full './loop.sh --dry-run' or 'make check' yourself before celebrating."
    exit 0
  fi

  # Build prompt and invoke Claude.

  prompt_file="$LOG_DIR/prompt_${ITER}.md"
  build_iteration_prompt "$ITER" "$CHECK_STATUS" > "$prompt_file"

  if ! invoke_claude "$prompt_file"; then
    log "claude invocation failed; recording and continuing"
    CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
    if (( CONSECUTIVE_FAILURES >= 3 )); then
      log "three consecutive Claude failures. Halting. Check $LOG_DIR/iter_${ITER}.log."
      exit 1
    fi
    sleep 5
    continue
  fi
  CONSECUTIVE_FAILURES=0

  # Independent verification.

  if run_check; then
    CHECK_STATUS="pass"
  else
    CHECK_STATUS="fail"
    # The next iteration's prompt will tell Claude to fix this before anything else.
    # We don't bail out; we just feed the failure back in.
  fi

  # Commit summary for the human reader of the log.
  last_commit="$(git log -1 --pretty=format:'%h %s' 2>/dev/null || echo 'no commits yet')"
  log "iteration $ITER end. last commit: $last_commit"

  # Tiny breather so any IDE/file watchers settle.
  sleep 1
done

log "max iterations ($MAX_ITERATIONS) reached without completion."
log "DoD progress: $(dod_count_ticked) / $(dod_total) ticked."
log "Review PROGRESS.md and either raise --max or investigate why the loop is not converging."
exit 1
