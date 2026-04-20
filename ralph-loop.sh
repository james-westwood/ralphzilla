#!/usr/bin/env bash
# ralph-loop.sh — Multi-agent AFK loop.
#
# !! DO NOT MODIFY THIS FILE — it is maintained by the human owner (James). !!
#
# Each task:
#   1. Assign Claude, Gemini, or opencode as CODER (and REVIEWER unless --skip-review)
#   2. Create a feature branch
#   3. CODER implements in atomic commits (src → tests → tracking)
#   4. Push branch, open PR on GitHub
#   5. REVIEWER reads the diff via `gh pr diff`, posts a review comment
#   6. Auto-merge → delete branch → pull main
#
# Stops when: all ralph-owned tasks done | next task is human-owned | iteration cap
#
# Usage:
#   ./ralph-loop.sh                              # up to 10 iterations, random coder/reviewer
#   ./ralph-loop.sh --max 50                    # up to 50 iterations
#   ./ralph-loop.sh --skip-review               # skip AI review, auto-merge immediately
#   ./ralph-loop.sh --claude-only               # Claude codes and reviews (no Gemini/opencode)
#   ./ralph-loop.sh --gemini-only               # Gemini codes and reviews (no Claude/opencode)
#   ./ralph-loop.sh --opencode-only             # opencode codes and reviews (no Claude/Gemini)
#   ./ralph-loop.sh --opencode-model google/gemini-2.0-flash        # override both opencode models
#   ./ralph-loop.sh --opencode-coder-model opencode/big-pickle       # override coder only
#   ./ralph-loop.sh --opencode-reviewer-model opencode/kimi-k2.5     # override reviewer only
#   ./ralph-loop.sh --claude-only --skip-review # Claude only, no review step
#   ./ralph-loop.sh --resume                    # resume stale branch if one exists for current task
#
# Requirements:
#   - claude CLI (with --dangerously-skip-permissions support)
#   - gemini CLI  (Google Gemini CLI, uses --yolo to auto-approve tool use)
#   - opencode CLI  (optional; used when --opencode-only or randomly assigned)
#   - gh CLI authenticated (gh auth login)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

MAX_ITERATIONS=10
SKIP_REVIEW=false
MODEL_MODE="random"  # random | claude | gemini | opencode
RESUME=false
OPENCODE_CODER_MODEL="opencode/big-pickle"    # override with --opencode-coder-model
OPENCODE_REVIEWER_MODEL="opencode/kimi-k2.5"  # override with --opencode-reviewer-model

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max)            MAX_ITERATIONS="$2"; shift 2 ;;
    --skip-review)    SKIP_REVIEW=true; shift ;;
    --claude-only)    MODEL_MODE="claude"; shift ;;
    --gemini-only)    MODEL_MODE="gemini"; shift ;;
    --opencode-only)  MODEL_MODE="opencode"; shift ;;
    --opencode-model)          OPENCODE_CODER_MODEL="$2"; OPENCODE_REVIEWER_MODEL="$2"; shift 2 ;;
    --opencode-coder-model)    OPENCODE_CODER_MODEL="$2"; shift 2 ;;
    --opencode-reviewer-model) OPENCODE_REVIEWER_MODEL="$2"; shift 2 ;;
    --resume)         RESUME=true; shift ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

LOG_FILE="$SCRIPT_DIR/ralph-loop.log"
MAIN_BRANCH="main"

# ── Helpers ──────────────────────────────────────────────────────────────────

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

die() {
  log "FATAL: $*"
  exit 1
}

# Gemini model to use — gemini-2.5-pro has a generous free tier and stable capacity
GEMINI_MODEL="gemini-2.5-pro"

# Strip ANSI escape codes and opencode internal tool-call lines from reviewer output
# so that GitHub PR comments contain only the review text, not raw terminal noise.
# Uses Python for reliable Unicode and byte-level escape handling.
clean_review_output() {
  python3 - <<'PYEOF'
import sys, re

text = sys.stdin.read()

# Strip all ANSI/VT escape sequences (CSI, OSC, etc.)
text = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', text)   # CSI sequences e.g. \x1b[0m
text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)  # OSC sequences
text = re.sub(r'\x1b[@-Z\\-_]', '', text)              # Fe sequences

# Remove opencode UI / tool-call lines
ui_prefixes = ('> build', '> session', '> task')
ui_chars    = set('\u2731\u2190\u2192\u2717\u25c7\u25c8\u2713\u25b6\u25c0\u21d2\u2714\u2718')
filtered = []
for line in text.splitlines():
    s = line.strip()
    # opencode "build" header lines
    if any(s.startswith(p) for p in ui_prefixes):
        continue
    # lines starting with a UI symbol
    if s and s[0] in ui_chars:
        continue
    # shell command echo lines  ($ cmd)
    if re.match(r'^\$\s+\S', s):
        continue
    filtered.append(line)

result = '\n'.join(filtered)
# Collapse 3+ consecutive blank lines to 2
result = re.sub(r'\n{3,}', '\n\n', result)
print(result.strip())
PYEOF
}

# Coding agent — needs full file-system tool access
# Each agent falls back to Claude on failure (rate limits, no capacity, etc.)
run_coder() {
  local agent="$1" prompt="$2"
  if [[ "$agent" == "claude" ]]; then
    if env -u CLAUDECODE claude --dangerously-skip-permissions --print "$prompt"; then
      return 0
    else
      log "  Claude coder failed — falling back to Gemini"
      gemini -m "$GEMINI_MODEL" --yolo -p "$prompt"
    fi
  elif [[ "$agent" == "gemini" ]]; then
    if gemini -m "$GEMINI_MODEL" --yolo -p "$prompt"; then
      return 0
    else
      log "  Gemini coder failed — falling back to Claude"
      env -u CLAUDECODE claude --dangerously-skip-permissions --print "$prompt"
    fi
  else
    # opencode — --dangerously-skip-permissions required for unattended file writes
    if opencode run -m "$OPENCODE_CODER_MODEL" --dangerously-skip-permissions "$prompt"; then
      return 0
    else
      log "  opencode coder failed — falling back to Claude"
      env -u CLAUDECODE claude --dangerously-skip-permissions --print "$prompt"
    fi
  fi
}

# Reviewing agent — reads a diff and returns text; no file-system writes needed
# Each agent falls back to Claude on failure
run_reviewer() {
  local agent="$1" prompt="$2"
  if [[ "$agent" == "claude" ]]; then
    if env -u CLAUDECODE claude --print "$prompt"; then
      return 0
    else
      log "  Claude reviewer failed — falling back to Gemini"
      gemini -m "$GEMINI_MODEL" -p "$prompt"
    fi
  elif [[ "$agent" == "gemini" ]]; then
    if gemini -m "$GEMINI_MODEL" -p "$prompt"; then
      return 0
    else
      log "  Gemini reviewer failed — falling back to Claude"
      env -u CLAUDECODE claude --print "$prompt"
    fi
  else
    # opencode reviewer — reads diff text only, no file writes needed
    # timeout 5m: kimi-k2.5 hangs silently; kill it and fall back to Claude
    if timeout 5m opencode run -m "$OPENCODE_REVIEWER_MODEL" "$prompt"; then
      return 0
    else
      local exit_code=$?
      if [[ $exit_code -eq 124 ]]; then
        log "  opencode reviewer timed out after 5m — falling back to Claude"
      else
        log "  opencode reviewer failed (exit $exit_code) — falling back to Claude"
      fi
      env -u CLAUDECODE claude --print "$prompt"
    fi
  fi
}

# Predicate: task is actionable (incomplete, ralph-owned, not blocked)
# Stop if no actionable tasks remain (exit 0 = tasks remain, exit 1 = all done)
check_complete() {
  python3 -c "
import json, sys
with open('prd.json') as f: prd = json.load(f)
actionable = [t for t in prd['tasks'] if not t.get('completed') and t.get('owner') != 'human' and not t.get('blocked')]
sys.exit(0 if actionable else 1)
" 2>/dev/null
}

# Prints task label and exits 0 if the next actionable task is human-owned; exits 1 otherwise
check_next_is_human() {
  python3 -c "
import json, sys
with open('prd.json') as f: prd = json.load(f)
incomplete = [t for t in prd['tasks'] if not t.get('completed') and not t.get('blocked')]
if incomplete and incomplete[0].get('owner') == 'human':
    t = incomplete[0]
    print(f'[{t[\"id\"]}] {t[\"title\"]} ({t[\"epic\"]})')
    sys.exit(0)
sys.exit(1)
" 2>/dev/null
}

count_remaining() {
  python3 -c "
import json
with open('prd.json') as f: prd = json.load(f)
print(len([t for t in prd['tasks'] if not t.get('completed') and t.get('owner') != 'human' and not t.get('blocked')]))
" 2>/dev/null || echo "?"
}

# Read a single field from the next actionable ralph-owned task
next_task_field() {
  local field="$1"
  python3 -c "
import json
with open('prd.json') as f: prd = json.load(f)
t = [x for x in prd['tasks'] if not x.get('completed') and x.get('owner') != 'human' and not x.get('blocked')][0]
print(t['$field'])
" 2>/dev/null
}

# ── Preflight ─────────────────────────────────────────────────────────────────

command -v gh     >/dev/null 2>&1 || die "'gh' not found — install GitHub CLI: https://cli.github.com"
command -v gemini >/dev/null 2>&1 || die "'gemini' not found — install Google Gemini CLI"
[[ "$MODEL_MODE" == "opencode" ]] && { command -v opencode >/dev/null 2>&1 || die "'opencode' not found — install opencode CLI"; }
gh auth status    >/dev/null 2>&1 || die "Not authenticated with gh — run: gh auth login"

REVIEW_MODE_LABEL=$( [[ "$SKIP_REVIEW" == "true" ]] && echo "auto-merge (no review)" || echo "AI review" )
MODEL_LABEL=$( case "$MODEL_MODE" in
  claude)   echo "Claude only" ;;
  gemini)   echo "Gemini only" ;;
  opencode) echo "opencode (coder=$OPENCODE_CODER_MODEL reviewer=$OPENCODE_REVIEWER_MODEL)" ;;
  *)        echo "Claude ↔ Gemini ↔ opencode (random)" ;;
esac )
RESUME_LABEL=$( [[ "$RESUME" == "true" ]] && echo "yes (resume stale branches)" || echo "no (fresh branches only)" )
echo "================================================================"
echo "  Ralph Loop — playchitect Multi-Agent AFK Mode"
echo "  Agents:   $MODEL_LABEL"
echo "  Workflow: branch → atomic commits → PR → $REVIEW_MODE_LABEL → merge"
echo "  Resume:   $RESUME_LABEL"
echo "  Max iterations: $MAX_ITERATIONS"
echo "  Log: $LOG_FILE"
echo "================================================================"
echo ""

log "Starting Ralph loop. Max iterations: $MAX_ITERATIONS"

ITERATION=0

while true; do

  # ── Stop conditions ────────────────────────────────────────────────────────

  if HUMAN_TASK=$(check_next_is_human 2>/dev/null); then
    log "Reached human-owned task: $HUMAN_TASK. Handing over."
    echo ""
    echo "================================================================"
    echo "  YOUR TURN"
    echo "  Next task is yours to implement: $HUMAN_TASK"
    echo "  Mark it complete in prd.json, then re-run ralph."
    echo "================================================================"
    break
  fi

  if ! check_complete; then
    log "All ralph-owned tasks complete."
    echo ""
    echo "================================================================"
    echo "  ALL RALPH TASKS COMPLETE"
    echo "================================================================"
    break
  fi

  if [[ $ITERATION -ge $MAX_ITERATIONS ]]; then
    log "Iteration cap ($MAX_ITERATIONS). $(count_remaining) tasks remaining."
    echo ""
    echo "================================================================"
    echo "  ITERATION CAP ($MAX_ITERATIONS) — run with --max N to continue"
    echo "  Tasks remaining: $(count_remaining)"
    echo "================================================================"
    break
  fi

  ITERATION=$((ITERATION + 1))

  # ── Task details ───────────────────────────────────────────────────────────

  TASK_ID=$(next_task_field id)
  TASK_TITLE=$(next_task_field title)
  TASK_DESC=$(next_task_field description)
  TASK_AC=$(next_task_field acceptance_criteria)
  TASK_EPIC=$(next_task_field epic)
  BRANCH="ralph/task-${TASK_ID}-${TASK_TITLE}"
  TODAY=$(date +%Y-%m-%d)

  # Assign coder/reviewer based on model mode
  case "$MODEL_MODE" in
    claude)   CODER="claude";    REVIEWER="claude" ;;
    gemini)   CODER="gemini";    REVIEWER="gemini" ;;
    opencode) CODER="opencode";  REVIEWER="opencode" ;;
    *)
      # 3-way random: Claude codes+Gemini reviews, Gemini codes+Claude reviews,
      # or opencode codes+Claude reviews (opencode review is a bonus cross-check)
      case $(( RANDOM % 3 )) in
        0) CODER="claude";   REVIEWER="gemini" ;;
        1) CODER="gemini";   REVIEWER="claude" ;;
        2) CODER="opencode"; REVIEWER="claude" ;;
      esac
      ;;
  esac

  echo ""
  echo "--- Iteration $ITERATION / $MAX_ITERATIONS  |  $(count_remaining) remaining ---"
  echo "  Task:     [$TASK_ID] $TASK_TITLE"
  echo "  Epic:     $TASK_EPIC"
  echo "  Branch:   $BRANCH"
  echo "  Coder:    $CODER  |  Reviewer: $REVIEWER"
  log "Iteration $ITERATION: [$TASK_ID] $TASK_TITLE | coder=$CODER reviewer=$REVIEWER branch=$BRANCH"

  # ── Branch setup ───────────────────────────────────────────────────────────

  BRANCH_EXISTS=false
  PR_EXISTS=""

  if git show-ref --verify --quiet "refs/heads/$BRANCH" || git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
    BRANCH_EXISTS=true
    PR_EXISTS=$(gh pr list --head "$BRANCH" --json number -q '.[0].number' 2>/dev/null || true)
  fi

  if [[ "$BRANCH_EXISTS" == "true" && "$RESUME" == "true" ]]; then
    log "  Resuming stale branch: $BRANCH"
    git checkout "$BRANCH"
    git pull --ff-only origin "$BRANCH" 2>/dev/null || true

    if [[ -n "$PR_EXISTS" ]]; then
      # PR already open — skip coding, go straight to merge
      log "  PR #$PR_EXISTS already open — skipping coding step, merging directly."
      PR_NUMBER="$PR_EXISTS"
      PR_URL="$(gh pr view "$PR_NUMBER" --json url -q '.url')"
      log "  PR: $PR_URL"

      if [[ "$SKIP_REVIEW" != "true" ]]; then
        log "  Fetching diff for review ($REVIEWER)..."
        PR_DIFF=""
        for _retry in 1 2 3 4 5; do
          PR_DIFF=$(gh pr diff "$PR_NUMBER" 2>/dev/null) && break
          log "  gh pr diff returned nothing (attempt $_retry/5) — waiting 10s..."
          sleep 10
        done
        REVIEW_PROMPT="You are the code reviewer for a pull request in the playchitect project.

The code was written by $CODER. You are $REVIEWER.

PR: [$TASK_ID] $TASK_TITLE
Acceptance criteria: $TASK_AC

Diff:
---
$PR_DIFF
---

Write a concise code review covering:
1. Correctness — does the implementation satisfy the acceptance criteria?
2. Code quality — readability, naming, structure
3. Test quality — are tests meaningful and sufficient?
4. Any bugs, edge cases, or concerns

Be constructive and specific. End your review with exactly one of:
- **APPROVED** — code is good to merge as-is
- **CHANGES REQUESTED: {brief reason}** — if there are real issues

Output only the review text. It will be posted as a GitHub PR comment."

        log "  Running reviewer ($REVIEWER)..."
        REVIEW_TEXT=$(run_reviewer "$REVIEWER" "$REVIEW_PROMPT" 2>&1 | tee -a "$LOG_FILE" | clean_review_output)
        log "  Posting review comment..."
        gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
## Code Review by \`$REVIEWER\`

$REVIEW_TEXT

---
*Implemented by \`$CODER\` · Reviewed by \`$REVIEWER\`*
EOF
)"
      else
        gh pr comment "$PR_NUMBER" --body "*Review skipped — merged automatically via \`--skip-review\`.*"
      fi

      log "  Waiting for CI to pass on PR #$PR_NUMBER..."
      for _wait in $(seq 1 60); do
        sleep 30
        CI_STATUS=$(gh pr checks "$PR_NUMBER" --json state -q '.[].state' 2>/dev/null | sort -u | tr '\n' ' ')
        if echo "$CI_STATUS" | grep -q "FAILURE\|ERROR"; then
          log "  CI FAILED on PR #$PR_NUMBER — stopping."; exit 1
        fi
        if echo "$CI_STATUS" | grep -qv "PENDING\|IN_PROGRESS\|QUEUED\|WAITING\|EXPECTED" && [[ -n "$CI_STATUS" ]]; then
          log "  CI passed (check ${_wait}/60, ci=$CI_STATUS)"; break
        fi
      done
      # Same pre-merge guard and orchestrator tracking as the main path
      PRD_CHANGES=$(gh pr diff "$PR_NUMBER" -- prd.json 2>/dev/null \
        | grep -c '^+.*"completed": true' || true)
      if [[ "$PRD_CHANGES" -gt 1 ]]; then
        log "  ABORT: coder marked $PRD_CHANGES tasks complete in prd.json (expected 0). Closing PR #$PR_NUMBER."
        gh pr close "$PR_NUMBER" --comment "Closing: coder incorrectly modified prd.json (marked $PRD_CHANGES tasks complete). The orchestrator owns prd.json."
        exit 1
      fi
      log "  Merging PR #$PR_NUMBER..."
      gh pr merge "$PR_NUMBER" --merge --delete-branch
      git checkout "$MAIN_BRANCH"
      git fetch origin "$MAIN_BRANCH"
      git reset --hard "origin/$MAIN_BRANCH"
      log "  Updating task tracking (prd.json + progress.txt)..."
      python3 - <<PYEOF
import json
with open("prd.json") as f:
    prd = json.load(f)
next(t for t in prd["tasks"] if t["id"] == "$TASK_ID")["completed"] = True
with open("prd.json", "w") as f:
    json.dump(prd, f, indent=2)
    f.write("\n")
PYEOF
      echo "[$TODAY] [$TASK_ID] $TASK_TITLE: implemented and merged (PR #$PR_NUMBER)" >> progress.txt
      git add prd.json progress.txt
      git commit -m "[$TASK_ID] $TASK_TITLE: mark complete"
      git push origin "$MAIN_BRANCH"
      log "Iteration $ITERATION complete (resumed): [$TASK_ID] $TASK_TITLE | $PR_URL"
      sleep 2
      continue
    else
      log "  Branch exists but no PR — resuming coding on existing branch."
    fi
  else
    # Fresh start — fetch + reset --hard ensures local main matches remote
    # exactly regardless of any divergence from previous iterations.
    git checkout "$MAIN_BRANCH"
    git fetch origin "$MAIN_BRANCH"
    git reset --hard "origin/$MAIN_BRANCH"
    # Use checkout without -b if the local branch already exists (e.g. from a
    # previous crashed run), otherwise create it fresh.
    if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
      git checkout "$BRANCH"
      git reset --hard "origin/$MAIN_BRANCH"   # re-base on latest main
    else
      git checkout -b "$BRANCH"
    fi
  fi

  # ── Branch safety guard ─────────────────────────────────────────────────────
  # Hard-abort if we are not on the expected feature branch. This catches the
  # case where opencode or a manual DM action left HEAD on main.
  CURRENT_BRANCH=$(git branch --show-current)
  if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
    log "  ABORT: expected branch '$BRANCH' but HEAD is on '$CURRENT_BRANCH'. Refusing to code on wrong branch."
    exit 1
  fi
  log "  Branch confirmed: $CURRENT_BRANCH"

  # ── Coding step ────────────────────────────────────────────────────────────

  log "  Running coder ($CODER)..."

  RESUME_NOTE=""
  if [[ "$BRANCH_EXISTS" == "true" && "$RESUME" == "true" ]]; then
    RESUME_NOTE="
IMPORTANT: This branch already has partial work from a previous run that was interrupted.
Run \`git log --oneline\` to see what commits exist. Do NOT redo work that is already committed.
Pick up from where the previous run left off."
  fi

  # The coder's job is ONLY to write code and tests — two commits, nothing more.
  # prd.json and progress.txt are owned by this orchestrator script, not the AI.
  # Letting the AI touch prd.json risks it bulk-marking unrelated tasks complete
  # (seen in the wild with kimi-k2.5), which silently kills the rest of the loop.
  CODER_PROMPT="You are the CODER implementing task [$TASK_ID] $TASK_TITLE for the playchitect project. Another AI will review your work — write clean, production-quality code.
$RESUME_NOTE

Read CLAUDE.md for project conventions.

Epic: $TASK_EPIC
Description: $TASK_DESC
Acceptance criteria: $TASK_AC

Implementation steps:
1. Write source files under playchitect/ (core logic) or tests/ (tests)
2. Run tests and fix any failures: uv run pytest tests/ -v
3. Run pre-commit and fix all failures: uv run pre-commit run --all-files
4. Make exactly TWO commits — no more, no fewer:
   - Commit A (source): git add playchitect/ && git commit -m '[$TASK_ID] $TASK_TITLE: implement'
   - Commit B (tests):  git add tests/ && git commit -m '[$TASK_ID] $TASK_TITLE: add tests'

Do NOT push. Do NOT create a PR. Do NOT touch prd.json or progress.txt — the
orchestrator handles all of that after your PR is merged.

Rules:
- Never implement any task with \"owner\": \"human\"
- Use uv for all Python commands (uv run pytest, uv sync, etc.)
- Source under playchitect/, tests under tests/
- Follow all conventions in CLAUDE.md"

  run_coder "$CODER" "$CODER_PROMPT" 2>&1 | tee -a "$LOG_FILE"

  # ── Pre-commit gate (catch lint/format failures before CI sees them) ────────

  MAX_PRECOMMIT_ROUNDS=2
  for _pc_round in $(seq 1 $MAX_PRECOMMIT_ROUNDS); do
    log "  Running pre-commit gate (round $_pc_round/$MAX_PRECOMMIT_ROUNDS)..."
    PRECOMMIT_OUTPUT=$(uv run pre-commit run --all-files 2>&1 || true)
    if echo "$PRECOMMIT_OUTPUT" | grep -q "^.*Failed$\|hook id:"; then
      log "  Pre-commit failed (round $_pc_round) — invoking coder to fix..."
      echo "$PRECOMMIT_OUTPUT" | tee -a "$LOG_FILE"

      # detect-secrets failure is a hard stop — never bypass security checks.
      if echo "$PRECOMMIT_OUTPUT" | grep -q "detect-secrets"; then
        log "  ABORT: detect-secrets hook failed. Closing PR and stopping loop — manual review required."
        if [[ -n "${PR_NUMBER:-}" ]]; then
          gh pr close "$PR_NUMBER" --comment "Closing: detect-secrets pre-commit hook failed. Manual review required before this can be merged." 2>/dev/null || true
        fi
        exit 1
      fi

      if [[ $_pc_round -lt $MAX_PRECOMMIT_ROUNDS ]]; then
        PRECOMMIT_FIX_PROMPT="You are the CODER who just implemented task [$TASK_ID] $TASK_TITLE for the playchitect project.

Pre-commit checks have failed. Fix every issue listed below, then run pre-commit again to confirm it passes.

Pre-commit output:
---
$PRECOMMIT_OUTPUT
---

Fix steps:
1. Fix all flagged issues in the listed files
2. Run: uv run pre-commit run --all-files  — must exit 0
3. Run: uv run pytest tests/ -v  — must still pass
4. Stage and commit the fixes: git add -u && git commit -m '[$TASK_ID] $TASK_TITLE: fix pre-commit failures'

Do NOT push."
        run_coder "$CODER" "$PRECOMMIT_FIX_PROMPT" 2>&1 | tee -a "$LOG_FILE"
      else
        log "  ABORT: pre-commit still failing after $MAX_PRECOMMIT_ROUNDS rounds — stopping. Manual fix required."
        exit 1
      fi
    else
      log "  Pre-commit gate passed."
      break
    fi
  done

  # ── Push and open PR ───────────────────────────────────────────────────────

  log "  Pushing $BRANCH..."
  git push -u origin "$BRANCH"

  log "  Creating PR..."
  PR_URL=$(gh pr create \
    --title "[$TASK_ID] $TASK_TITLE" \
    --body "$(cat <<EOF
## [$TASK_ID] $TASK_TITLE

**Epic:** $TASK_EPIC
**Coder:** \`$CODER\` | **Reviewer:** \`$REVIEWER\`

### Description
$TASK_DESC

### Acceptance Criteria
$TASK_AC

---
*Ralph Loop — multi-agent AI pair programming*
EOF
)" \
    --base "$MAIN_BRANCH" \
    --head "$BRANCH")

  PR_NUMBER=$(echo "$PR_URL" | grep -oE '[0-9]+$')
  log "  PR #$PR_NUMBER: $PR_URL"

  # ── Review step (with fix loop) ────────────────────────────────────────────

  if [[ "$SKIP_REVIEW" == "true" ]]; then
    log "  Skipping review (--skip-review). Auto-merging."
    gh pr comment "$PR_NUMBER" --body "*Review skipped — merged automatically via \`--skip-review\`.*"
  else
    MAX_FIX_ROUNDS=2
    REVIEW_VERDICT="PENDING"

    for _round in $(seq 0 $MAX_FIX_ROUNDS); do
      log "  Fetching diff for review ($REVIEWER, round $_round)..."
      PR_DIFF=""
      for _retry in 1 2 3 4 5; do
        PR_DIFF=$(gh pr diff "$PR_NUMBER" 2>/dev/null) && break
        log "  gh pr diff returned nothing (attempt $_retry/5) — waiting 10s..."
        sleep 10
      done

      REVIEW_PROMPT="You are the code reviewer for a pull request in the playchitect project.

The code was written by $CODER. You are $REVIEWER.

PR: [$TASK_ID] $TASK_TITLE
Acceptance criteria: $TASK_AC

Diff:
---
$PR_DIFF
---

Write a concise code review covering:
1. Correctness — does the implementation satisfy the acceptance criteria?
2. Code quality — readability, naming, structure
3. Test quality — are tests meaningful and sufficient?
4. Any bugs, edge cases, or concerns

Be constructive and specific. End your review with exactly one of:
- **APPROVED** — code is good to merge as-is
- **CHANGES REQUESTED: {brief reason}** — if there are real issues

Output only the review text. It will be posted as a GitHub PR comment."

      log "  Running reviewer ($REVIEWER)..."
      REVIEW_TEXT=$(run_reviewer "$REVIEWER" "$REVIEW_PROMPT" 2>&1 | tee -a "$LOG_FILE" | clean_review_output)

      log "  Posting review comment..."
      gh pr comment "$PR_NUMBER" --body "$(cat <<EOF
## Code Review by \`$REVIEWER\` (round $_round)

$REVIEW_TEXT

---
*Implemented by \`$CODER\` · Reviewed by \`$REVIEWER\`*
EOF
)"

      if echo "$REVIEW_TEXT" | grep -q "CHANGES REQUESTED"; then
        REVIEW_VERDICT="CHANGES_REQUESTED"
        if [[ $_round -lt $MAX_FIX_ROUNDS ]]; then
          log "  Reviewer requested changes (round $_round/$MAX_FIX_ROUNDS) — invoking coder to fix..."
          FIX_PROMPT="You are the CODER who implemented task [$TASK_ID] $TASK_TITLE for the playchitect project.

Your code reviewer has requested changes. Read the review carefully and fix all blocking issues.

Review feedback:
---
$REVIEW_TEXT
---

Task acceptance criteria (must still be satisfied): $TASK_AC

Fix steps:
1. Address every issue the reviewer flagged
2. Run tests and ensure they pass: uv run pytest tests/ -v
3. Run pre-commit and fix all failures: uv run pre-commit run --all-files
4. Commit your fixes: git add -u && git commit -m '[$TASK_ID] $TASK_TITLE: address review feedback'

Do NOT push. Do NOT open a new PR. Fix only the issues raised — do not refactor unrelated code."

          run_coder "$CODER" "$FIX_PROMPT" 2>&1 | tee -a "$LOG_FILE"
          log "  Pushing fixes to $BRANCH..."
          git push origin "$BRANCH"
        else
          log "  Reviewer still requesting changes after $MAX_FIX_ROUNDS fix round(s) — merging anyway."
        fi
      else
        REVIEW_VERDICT="APPROVED"
        log "  Reviewer approved."
        break
      fi
    done

    log "  Final review verdict: $REVIEW_VERDICT"
  fi

  # ── CI wait + fix loop ─────────────────────────────────────────────────────

  MAX_CI_FIX_ROUNDS=2
  CI_PASSED=false

  for _ci_fix_round in $(seq 0 $MAX_CI_FIX_ROUNDS); do
    if [[ $_ci_fix_round -gt 0 ]]; then
      log "  CI fix round $_ci_fix_round/$MAX_CI_FIX_ROUNDS — waiting for CI to re-run..."
    fi

    # Pin to the specific run ID for this push so we never read stale data
    # from a previous run. On the first pass (_ci_fix_round=0) we take
    # whatever the latest run is; on fix rounds we already waited for the
    # new run ID above.
    POLL_RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
    log "  Waiting for CI run ${POLL_RUN_ID:-unknown} on PR #$PR_NUMBER..."
    CI_FINAL_STATUS=""
    for _wait in $(seq 1 60); do
      sleep 30
      if [[ -n "$POLL_RUN_ID" ]]; then
        # Poll the pinned run directly — immune to stale data from other runs
        RUN_CONCLUSION=$(gh run view "$POLL_RUN_ID" --json conclusion -q '.conclusion' 2>/dev/null || echo "")
        RUN_STATUS=$(gh run view "$POLL_RUN_ID" --json status -q '.status' 2>/dev/null || echo "")
        if [[ "$RUN_CONCLUSION" == "failure" || "$RUN_CONCLUSION" == "error" ]]; then
          CI_FINAL_STATUS="FAILED"
          break
        fi
        if [[ "$RUN_STATUS" == "completed" && "$RUN_CONCLUSION" == "success" ]]; then
          CI_FINAL_STATUS="PASSED"
          break
        fi
        CI_STATUS="$RUN_STATUS/$RUN_CONCLUSION"
      else
        # Fallback: no run ID yet, poll PR checks generically
        CI_STATUS=$(gh pr checks "$PR_NUMBER" --json state -q '.[].state' 2>/dev/null | sort -u | tr '\n' ' ')
        if echo "$CI_STATUS" | grep -q "FAILURE\|ERROR"; then
          CI_FINAL_STATUS="FAILED"; break
        fi
        if [[ -n "$CI_STATUS" ]] && ! echo "$CI_STATUS" | grep -q "PENDING\|IN_PROGRESS\|QUEUED\|WAITING\|EXPECTED"; then
          CI_FINAL_STATUS="PASSED"; break
        fi
      fi
      log "  Still waiting... (check ${_wait}/60, ci=$CI_STATUS)"
    done

    if [[ "$CI_FINAL_STATUS" == "PASSED" ]]; then
      log "  CI passed."
      CI_PASSED=true
      break
    fi

    if [[ "$CI_FINAL_STATUS" == "FAILED" ]]; then
      if [[ $_ci_fix_round -ge $MAX_CI_FIX_ROUNDS ]]; then
        log "  CI still failing after $MAX_CI_FIX_ROUNDS fix round(s) — stopping. Manual fix required."
        exit 1
      fi

      log "  CI FAILED — fetching failure log and invoking coder to fix (round $_ci_fix_round)..."
      CI_RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)
      CI_FAILURE_LOG=$(gh run view "$CI_RUN_ID" --log-failed 2>/dev/null | tail -100)

      CI_FIX_PROMPT="You are the CODER who implemented task [$TASK_ID] $TASK_TITLE for the playchitect project.

The CI pipeline has failed on the pull request. Fix every issue shown in the failure log below.

CI failure log:
---
$CI_FAILURE_LOG
---

Fix steps:
1. Read the failure log carefully and identify the root cause
2. Fix the failing files
3. Run: uv run pre-commit run --all-files  — must exit 0
4. Run: uv run pytest tests/ -v  — must pass
5. Stage and commit fixes: git add -u && git commit -m '[$TASK_ID] $TASK_TITLE: fix CI failure'
6. Do NOT push — the orchestrator will push."

      git checkout "$BRANCH"
      run_coder "$CODER" "$CI_FIX_PROMPT" 2>&1 | tee -a "$LOG_FILE"

      # Capture the current run ID before pushing so we can detect when
      # GitHub registers the *new* run triggered by the push. Without this,
      # the poller races against GitHub's indexing window and may read stale
      # data from the just-failed run, misclassifying it as a new failure.
      PREV_RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
      log "  Pushing CI fixes to $BRANCH (prev run ID: ${PREV_RUN_ID:-none})..."
      git push origin "$BRANCH"

      # Wait for GitHub to register a new run (different ID from the one
      # that just failed). Cap at 20 attempts × 10s = ~3 minutes.
      log "  Waiting for GitHub to register new CI run..."
      for _reg in $(seq 1 20); do
        sleep 10
        NEW_RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
        if [[ -n "$NEW_RUN_ID" && "$NEW_RUN_ID" != "$PREV_RUN_ID" ]]; then
          log "  New CI run registered: $NEW_RUN_ID (attempt $_reg/20)"
          break
        fi
        log "  Still waiting for new run... (attempt $_reg/20, current=${NEW_RUN_ID:-none})"
      done
    fi

    if [[ -z "$CI_FINAL_STATUS" ]]; then
      log "  CI timed out after 30 minutes — stopping."
      exit 1
    fi
  done

  if [[ "$CI_PASSED" != "true" ]]; then
    log "  CI did not pass — stopping."
    exit 1
  fi

  # ── Pre-merge prd.json guard ───────────────────────────────────────────────
  # AI coders sometimes "helpfully" mark multiple tasks complete in one edit
  # (seen with kimi-k2.5 bulk-completing all 30 tasks). Catch this before it
  # poisons the orchestrator's source of truth and silently ends the loop early.
  # We fetch the PR diff and count how many tasks flipped to completed=true.
  # If it's more than one, the coder overstepped — close the PR and abort.
  # Note: the coder prompt now says not to touch prd.json at all, so ideally
  # this count will always be zero. Non-zero means the coder ignored instructions.
  PRD_CHANGES=$(gh pr diff "$PR_NUMBER" -- prd.json 2>/dev/null \
    | grep -c '^+.*"completed": true' || true)
  if [[ "$PRD_CHANGES" -gt 1 ]]; then
    log "  ABORT: coder marked $PRD_CHANGES tasks complete in prd.json (expected 0). Closing PR #$PR_NUMBER."
    gh pr close "$PR_NUMBER" --comment "Closing: coder incorrectly modified prd.json (marked $PRD_CHANGES tasks complete). The orchestrator owns prd.json. Re-run ralph to retry this task."
    exit 1
  fi

  log "  Merging PR #$PR_NUMBER..."
  gh pr merge "$PR_NUMBER" --merge --delete-branch

  # Use fetch + reset --hard instead of pull --ff-only. After gh pr merge,
  # the remote main has the new merge commit but our local main may have
  # diverged (e.g. a previous iteration's tracking commit didn't push cleanly,
  # or set -euo pipefail killed us mid-push leaving local ahead of remote).
  # reset --hard is unconditional — it always makes local match remote exactly.
  git checkout "$MAIN_BRANCH"
  git fetch origin "$MAIN_BRANCH"
  git reset --hard "origin/$MAIN_BRANCH"

  # ── Orchestrator-owned task tracking ──────────────────────────────────────
  # The AI coder no longer touches prd.json or progress.txt — we do it here,
  # after a successful merge, with a precise single-task update. This removes
  # all risk of the AI accidentally (or "helpfully") mutating other tasks' state.
  log "  Updating task tracking (prd.json + progress.txt)..."
  python3 - <<PYEOF
import json
with open("prd.json") as f:
    prd = json.load(f)
task = next((t for t in prd["tasks"] if t["id"] == "$TASK_ID"), None)
if task is None:
    raise SystemExit(f"ERROR: task $TASK_ID not found in prd.json")
if task.get("completed"):
    print(f"  Note: $TASK_ID was already marked complete (coder may have touched prd.json despite instructions)")
task["completed"] = True
with open("prd.json", "w") as f:
    json.dump(prd, f, indent=2)
    f.write("\n")
print(f"  Marked $TASK_ID complete in prd.json")
PYEOF
  echo "[$TODAY] [$TASK_ID] $TASK_TITLE: implemented and merged (PR #$PR_NUMBER)" >> progress.txt
  git add prd.json progress.txt
  git commit -m "[$TASK_ID] $TASK_TITLE: mark complete"
  git push origin "$MAIN_BRANCH"

  log "Iteration $ITERATION complete: [$TASK_ID] $TASK_TITLE | $PR_URL"

  sleep 2

done

log "Loop finished. $ITERATION iterations. $(count_remaining) tasks remaining."
echo ""
echo "Summary: $ITERATION iterations run, $(count_remaining) tasks remaining."
