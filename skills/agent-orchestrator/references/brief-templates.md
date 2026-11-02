# Brief templates

Save every brief you send to `briefs/<id>.md` so failures can be debugged and retried with the same text plus the error. Replace `<...>` placeholders. Keep briefs tight: an agent given a page of precise instructions beats one given three pages of background.

All four templates end with the same return contract.

```
RETURN CONTRACT - your final message must be exactly these lines and nothing else:
STATUS: done | failed | blocked
OUTPUT: <path to your full output file>
SUMMARY: <1-2 sentences>
CONFIDENCE: verified | inferred | uncertain
NEW_TASKS: <none | bullets of proposed extra work; do NOT start it>
```

---

## Reader (`read`)

```
You are a read-only research agent. Do not modify any file except your output file.

OBJECTIVE
<one concrete question to answer, e.g. "List every call site of SessionMiddleware and what each relies on">

CONTEXT
Read <run_dir>/CONTEXT.md first; do not re-explore the repo structure.
Analyze the code at commit <base_sha> (the user's checkout, which stays unmodified during the run).

SCOPE
In: <directories / topics>
Out: <anything adjacent you should ignore>

BUDGET
Open at most <N> files. Output at most 400 words.

OUTPUT
Write to <run_dir>/findings/<id>.md using this shape:
  # <id>: <question>
  Commit: <sha>
  ## Findings
  - <claim> [verified|inferred|uncertain] - evidence: path/to/file.py:LINE
  ## Gaps
  - <what you could not determine>
Mark "verified" only for things you saw directly in the code or ran. Mark "inferred" for reasonable deductions. Never present a guess as verified.

DONE WHEN
The question is answered with evidence, or you have stated precisely what blocked you.

<return contract>
```

## Synthesizer (`synth`)

```
You are the synthesis gate. Read-only except for your output file.

INPUTS
<run_dir>/CONTEXT.md, <run_dir>/findings/INDEX.md, and the findings listed there: <ids>
Original goal: <goal>
Current plan tasks still pending: <list of write tasks with titles and write-sets>

TASK
Produce <run_dir>/DECISIONS.md, at most one page:
  ## Decisions        - what is settled, each tagged with the finding ids that support it
  ## Contradictions   - where findings disagree with each other or with the plan
  ## Open questions   - what is still uncertain and who/what could resolve it
  ## Plan changes     - tasks to add, remove, or re-scope, with new write-sets
  ## Constraints for writers - conventions, APIs, files that must not be touched
Resolve disagreements by re-checking the cited evidence in the code when cheap; otherwise list them as open.
Rely on "verified" findings. Downgrade anything you cannot support.

<return contract>
```

## Writer (`write`)

Launch with `isolation: "worktree"` and `run_in_background: true`, after `orch.py set <run_dir> <id> running`. Use absolute paths for the run dir.

```
You are an implementation agent working in an isolated git worktree.

STEP 0 (mandatory, before any change)
Run: python3 <skill>/scripts/orch.py bootstrap <ABSOLUTE run_dir> <id>
It moves your fresh worktree to the exact commit you must build on and arms the write-set guard. If it errors, stop and report STATUS: blocked with the message.

OBJECTIVE
<specific change, with the observable end state>

READ FIRST
<run_dir>/DECISIONS.md and <run_dir>/CONTEXT.md. Do not read raw findings unless DECISIONS.md points you to one.

ALLOWED TO MODIFY (write-set)
<globs>
Do not touch anything else. If you believe another file must change, stop and report it under NEW_TASKS instead of editing it.

QUALITY BAR
Follow the conventions in CONTEXT.md. Run <test/lint command> for the code you changed and make it pass. Do not weaken or delete tests to make them pass.

BUDGET
If you are not converging after <N> attempts at the same failure, stop and report STATUS: blocked with what you tried.

FINISH
Commit your work on your branch with a clear message. Write <run_dir>/results/<id>.md (under 200 words: what changed, tests run, caveats). In the return contract, add the branch name to SUMMARY.

<return contract>
```

## Reviewer (`review`)

Give this agent only decisions, the diff and the test command. It must not have the process context; fresh eyes are the point.

```
You are an independent reviewer. Read-only except for your output file.

INPUTS
- <run_dir>/DECISIONS.md (what was supposed to happen)
- The integrated change: `git diff <base_sha>..orch/<run-id>` (run in the repo)
- Test command: <cmd>

TASK
Run the tests. Check, one by one, that each decision in DECISIONS.md is reflected in the diff. Look for: decisions not implemented, changes nobody decided, broken interfaces between parts written by different agents, weakened tests, leftover debug code.

OUTPUT
<run_dir>/results/REVIEW.md with sections: Passed, Mismatches (file:line + which decision), Risks. Under 300 words.

<return contract>
```

---

## Retry addendum

When relaunching a failed task, use the original brief plus:

```
PREVIOUS ATTEMPT FAILED
Error / last output:
<excerpt, trimmed to the relevant lines>
Do not repeat the same approach. If it cannot be done within the scope, return STATUS: blocked and explain.
```
