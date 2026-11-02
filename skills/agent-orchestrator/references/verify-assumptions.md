# Verify assumptions (run once in Claude Code)

The skill rests on assumptions about Claude Code that could not be tested where the skill was written. Run these experiments once in a scratch git repo, record the results, and adjust the skill if any fails. Each takes about a minute. Paste each prompt into Claude Code as-is.

Create a scratch repo first:
```bash
mkdir /tmp/orch-lab && cd /tmp/orch-lab && git init -q -b main \
  && echo hi > a.txt && git add -A && git commit -qm init \
  && git checkout -qb feature/lab && echo more >> a.txt && git commit -qam second
```
(You are now on `feature/lab`, one commit ahead of `main`, which is what makes E3 meaningful.)

| # | Assumption | If it fails |
|---|---|---|
| E1 | Several Agent calls in one message really run in parallel | Lower concurrency expectations; wave sizes in SKILL.md become advisory |
| E2 | Background agents notify on completion without polling | Add an explicit wait step; revisit "never poll" |
| E3 | `isolation: "worktree"` starts from the default branch, not HEAD | If it starts from HEAD, `bootstrap` is still harmless, nothing to change |
| E4 | Subagents cannot spawn subagents | If they can, allow nested fan-out for read tasks only |
| E5 | A worktree branch is visible from the main repo and `bootstrap` can reset it | Switch to creating worktrees ourselves with `git worktree add` |
| E6 | The write-set hook blocks out-of-set edits inside a worktree agent | Rely on `check-writeset`/`integrate` only |
| E7 | Background agents can write files and run the test command without a permission prompt | Pre-approve permissions (see below) before launching |
| E8 | The error texts for usage limits and proxy/network failures match the classification table in SKILL.md | Update the table, using the exact strings you saw |

## E1 + E2: parallelism and notification
```
Launch three background agents in a single message. Each one must run `date +%T.%N`, sleep 20 seconds, run `date +%T.%N` again, and return both timestamps in one line. Do not poll them. After all three notify you, tell me whether their start times overlap, and whether you received the notifications without checking any output file.
```
Pass: the three start times are within a couple of seconds of each other and notifications arrived unprompted.

## E3 + E5: worktree base and branch visibility
```
Launch one agent with isolation "worktree" and run_in_background true. It must run `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, `git rev-parse main`, and `pwd`, create a file `probe.txt`, commit it, and return those values plus the commit hash. After it finishes, in the main repo run `git branch --list` and `git worktree list` and report whether the agent's branch is listed.
```
Record: did the agent's `HEAD` equal `main` or `feature/lab`? Is its branch listed in the main repo? (The skill requires "yes, listed".)

## E4: nesting
```
Launch one agent and tell it to try to launch its own subagent to run `echo nested`. It must report exactly what happened (success or the error message).
```

## E6 + E7: hook and permissions
First install the hook (see `hooks.md`), then:
```
Run: python3 <skill>/scripts/orch.py init .claude/runs/lab --goal lab
Then add one write task `w1` with write_set `allowed/**` (and a `read` task + `synth` task before it, or use --force). Mark it running, and launch a background agent with isolation "worktree" whose brief says: run `python3 <skill>/scripts/orch.py bootstrap <abs run dir> w1`, then create `allowed/ok.txt`, then try to create `forbidden/no.txt`, then run `echo tested`, commit, and report which of these succeeded or were blocked and any permission errors.
```
Pass: `allowed/ok.txt` succeeds, `forbidden/no.txt` is blocked with the orchestrator message, and no step fails on a permission prompt. If background agents report "auto-denied (prompts unavailable)", add the needed rules under `permissions.allow` in `.claude/settings.json` (for example `Edit`, `Write`, `Bash(git:*)`, `Bash(python3:*)`, and your test command), then rerun.

## E8: how do limit and network errors actually look?

You cannot trigger a real usage limit on demand, so collect evidence when it happens naturally, and test the mechanics now:
```
Run: python3 <skill>/scripts/orch.py init .claude/runs/lab2 --goal lab --transient-wait 5 --max-retries 2
Add one read task r1, mark it running, then simulate a failure: python3 <skill>/scripts/orch.py error .claude/runs/lab2 r1 --kind transient --note "simulated". Run `next` immediately (it should say PAUSED), wait 5 seconds, run `next` again (r1 should be listed). Report the output of each step.
```
Then, the next time an agent in a real run dies with a limit or proxy error, paste its exact error text into your notes and compare it with the table in SKILL.md ("Infrastructure errors"). Also record whether the message states a reset time you can pass as `--wait-sec`.

## Record the results

Copy into your notes and fill in:
```
Claude Code version: ____    Date: ____
E1 parallel:        pass / fail   notes:
E2 notification:    pass / fail
E3 worktree base:   started from main / feature/lab
E4 nesting:         blocked / allowed
E5 branch visible:  yes / no
E6 hook blocks:     yes / no / hook not triggered
E7 permissions:     ok / needed allow-list: ____
E8 error texts:     matched table / differences: ____
```
