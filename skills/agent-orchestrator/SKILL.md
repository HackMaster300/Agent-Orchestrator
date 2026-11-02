---
name: agent-orchestrator
description: Orchestrate many Claude Code subagents working asynchronously and in parallel on a mixed read/write job (research + analysis + code changes) without collisions, lost state, or runaway token spend. Use this skill whenever the user asks to parallelize work across agents, "run agents in the background", split a large task among subagents, fan out research then implement, coordinate multiple agents, speed up a big refactor/feature/audit with agents, or says things like "use as many agents as you can", "do this in parallel", "orchestrate", or "manage the agents" - even if they never say the word orchestrator. Do NOT use for small tasks with fewer than three independent units of work; just do those directly.
---

# Agent Orchestrator

You are the **orchestrator**. Your job is not to do the work; it is to decompose a goal into a task graph, launch subagents in safe waves, keep an accurate record of state on disk, and integrate the results. Subagents are cheap to launch and expensive to get wrong, so most of the value here comes from discipline *before* launching and *after* results return.

Why asynchronous work fails without this discipline: agents collide on files, start before their inputs exist, flood your context with long reports, repeat each other's exploration, and nobody checks that the pieces fit. Every rule below targets one of those failures.

## Is this worth it?

Parallelism has overhead (planning, briefs, integration). Use it only when there are **3+ units of work that are genuinely independent**, or when research breadth is the bottleneck. Otherwise do the work yourself or use one foreground subagent. If the user's task is smaller than that, say so briefly and proceed without the machinery.

## Operating principles

1. **State lives on disk.** `.claude/runs/<run-id>/plan.json` is the source of truth. Your context can be compacted at any time; the file cannot. After any compaction or resume, run `python <skill>/scripts/orch.py status <run_dir>` before doing anything else.
2. **You are the single writer of `plan.json`.** Subagents never edit it. They write their own output file and return a short message; you update the plan. This avoids races.
3. **Files carry data, messages carry pointers.** Each agent writes its full output to a file and returns at most 5 lines. This is the biggest token and context saver.
4. **Never poll.** Background agents notify you on completion. Do not read a running agent's output file mid-run; it drags its internal tool noise into your context. Launch, then do useful work (or wait) until notifications arrive.
5. **Read lane and write lane have different rules.** Readers share the repo and go wide. Writers get their own worktree and go narrow. See below.
6. **Nothing is written on top of unverified research.** A synthesis gate sits between the two lanes.
7. **Nothing touches the user's branch until the end.** Writers merge into an integration branch (`orch/<run-id>`) in its own worktree. The user's checkout stays clean, and later waves start from the integrated result, so a dependent writer sees the earlier writers' code.
8. **Subagents are flat.** Assume subagents cannot spawn subagents. Only you launch agents. If an agent discovers more work, it reports it (`NEW_TASKS`) and you decide.

## Lanes

| | Read lane (`read`) | Write lane (`write`) |
|---|---|---|
| Isolation | none, shares repo, read-only | `isolation: "worktree"`, own branch |
| Default concurrency | up to 6 | up to 3 |
| Model | haiku (exploration), sonnet (analysis) | sonnet; opus only for hard design-heavy tasks |
| Output | `findings/<id>.md` | branch + commit + `results/<id>.md` |
| Cost of error | low, discard | high, merge pain and regressions |

Two more task kinds: `synth` (consolidates findings into `DECISIONS.md`, the gate) and `review` (read-only verification of the integrated result).

## Workflow

### Before anything: ask the user (once, in one message)

Do not plan or launch anything before you have these answers. Use your question tool if you have one (for example `AskUserQuestion`), otherwise one short message with numbered options. Offer a "defaults" shortcut. Skip a question only if the user already answered it explicitly in their request, and in that case confirm in one line instead of re-asking (for example "You asked for auto mode, keep it?").

1. **Mode.** "Keep auto mode, or keep checkpoints?" 
   - `checkpoint` (default): you pause for approval after the plan (A) and after the synthesis (B).
   - `auto`: runs to the end; still stops on contradictions or high uncertainty.
2. **Retry policy for limits and network errors.** "If a rate/usage limit or a proxy/network error hits, how patiently should I retry?"
   - *Balanced* (default): first retry after 30 s, doubling up to 10 min; wait 15 min after a usage limit; 5 retries per task; at most 3 h of total waiting.
   - *Patient*: 60 s doubling to 15 min; wait 30 min after a limit; 8 retries; up to 8 h total.
   - *Fail fast*: no retries; stop at the first infrastructure error.
   - *Custom*: ask for the numbers (first wait, limit wait, retries per task, total hours).
3. **When retries run out.** "pause and report" (default), "ask me what to do", or "skip that task and continue with the independent ones".

Map the answers to `init` flags (see Phase 0), or to `orch.py policy` if a run already exists. If you are resuming an existing run, show the stored settings (`orch.py policy <run_dir>`) and ask only whether to change them.

### Phase 0 - Init
```bash
python <skill>/scripts/orch.py init .claude/runs/<run-id> --goal "<one line>" --test-cmd "<project test command>" \
  --mode checkpoint|auto \
  --transient-wait 30 --backoff 2 --max-wait 600 --limit-wait 900 \
  --max-retries 5 --max-total-wait 180 --on-exhausted pause|ask|skip
```
The retry flags shown are the *Balanced* values; fill them from the user's answers. *Fail fast* is `--max-retries 0`.
This records `base_sha`, creates `findings/ results/ briefs/`, and creates the integration branch `orch/<run-id>` with its own worktree under `<run_dir>/integration`. `--test-cmd` is run after every merge; without it, integration cannot catch broken merges. Add `.claude/runs/` to `.gitignore`.

Always pass **absolute** run-dir paths inside briefs: writers run in a different directory.

If the working tree is dirty, tell the user and either commit/stash or proceed knowing that worktrees will not see uncommitted changes.

**Permissions.** Background agents cannot answer permission prompts; a tool that needs approval is auto-denied and the agent silently fails. Before the first launch, check that `.claude/settings.json` allows what the agents need (`Edit`, `Write`, `Bash(git:*)`, `Bash(python3:*)`, the test command). If unsure, tell the user and offer the allow-list.

### Phase 1 - Plan (DAG)
Decompose the goal into tasks and write them into `plan.json` (schema and a worked example: `references/plan-schema.md`). For each task decide:

- `kind`: `read | synth | write | review`
- `deps`: tasks whose output it needs. Be honest here; a false "independent" is the most expensive mistake.
- `write_set`: for `write` tasks, the files/dirs/globs it may change. Two write tasks that can run at the same time must have disjoint write-sets.
- `model` and a one-line `title`.

Build the plan by editing `plan.json` or with `orch.py add / update / remove` (each re-validates and refuses a bad plan; once agents are running it enforces every rule). Then validate:
```bash
python <skill>/scripts/orch.py validate <run_dir>
```
It catches cycles, missing deps, overlapping write-sets among concurrent writers, and writers that skip the synthesis gate. Fix every error before continuing.

Use a cheap `read` task first to build a shared `CONTEXT.md` (repo map, conventions, test commands) if the repo is non-trivial. Later agents read it instead of re-exploring.

**CHECKPOINT A (default mode):** show the user the plan as a compact table (id, kind, deps, write-set, model) plus estimated agent count and the biggest risk. Wait for approval. Skip only if `mode` is `auto` (chosen in the opening questions).

### Phase 2 - Research wave
Get launchable tasks:
```bash
python <skill>/scripts/orch.py next <run_dir>
```
Launch every task it lists **in a single message** (multiple Agent tool calls) with `run_in_background: true`. Parallel launches only happen if the calls are in the same message. Before launching, mark each: `orch.py set <run_dir> <id> running`.

Build each brief from `references/brief-templates.md`. A brief must contain: objective, inputs to read, what is out of scope, output path, output format, definition of done, and the **return contract**. Vague briefs are the main source of bad results.

Readers analyze the repo at `base_sha`. Tell them to note the SHA in their findings.

### Phase 3 - Synthesis gate
When all research tasks are done, run the `synth` task (a sonnet or opus agent, or yourself if findings are few). It reads `findings/*.md` and produces `DECISIONS.md` (one page): what is decided, what contradicts the plan, what remains uncertain, and any `NEW_TASKS`. Findings carry a confidence tag (`verified | inferred | uncertain`); writers may rely only on `verified` items without re-checking.

Then:
- If decisions invalidate the plan, **replan** (edit `plan.json`, re-validate) before spending tokens on code.
- **CHECKPOINT B (default mode):** present `DECISIONS.md` and the (possibly revised) implementation tasks. Wait for approval. In `auto` mode, proceed unless the synthesis flagged a contradiction or high uncertainty, in which case stop and ask anyway.

### Phase 4 - Implementation wave
For each ready `write` task, in this order:
1. `orch.py set <run_dir> <id> running` - records `start_sha`, the current tip of the integration branch. Writers start from **that**, not from `base_sha`, so wave-2 writers see wave-1 code.
2. Launch with `isolation: "worktree"` and `run_in_background: true`, all of a wave in one message.

Writers read `DECISIONS.md` and `CONTEXT.md`, **not** raw findings. Their first step is `orch.py bootstrap <abs run_dir> <id>`: it resets the worktree to `start_sha` (Claude Code may start worktrees from the default branch instead of your HEAD), and drops a marker file that arms the write-set guard hook. They commit on their branch and report the branch name.

As each completion notification arrives: `orch.py set <run_dir> <id> done --summary "<their 1-line summary>" --branch <branch>` (or `failed`). Do not call `next` again until the wave has finished: a writer's dependents unlock only after it is merged (Phase 5).

### Phase 5 - Integrate (after every write wave)
```bash
python <skill>/scripts/orch.py integrate <run_dir>
```
For each finished writer this (1) verifies it only touched its write-set, rejecting it otherwise, (2) merges it into the integration branch, (3) runs `test_cmd` and reverts the merge if tests fail. Conflicts abort that merge and are reported. A rejection or conflict means the write-set was wrong: fix the plan (`update`), relaunch or resolve by hand, and record what happened in `results/INTEGRATION.md`. Then call `next`: dependents of merged writers are now unlocked and will start from the new tip. Repeat Phases 4-5 until no writers remain.

`orch.py merge-order` is a dry run; `orch.py check-writeset <id>` checks one writer on its own.

### Phase 6 - Verify and land
Launch one read-only `review` agent that has **not** seen the process. Give it only `DECISIONS.md`, the diff `git diff <base_sha>..orch/<run-id>`, and the test command. Fix what it finds, then:
```bash
python <skill>/scripts/orch.py land <run_dir>
```
It prints the diff summary. **Do not merge into the user's branch yourself**; show them `git merge orch/<run-id>` (or a PR) and let them land it. Afterwards `orch.py cleanup <run_dir> --all` removes merged branches and worktrees. Finish with a short report: what was done, what was cut, where artifacts live, how many agents ran.

## Infrastructure errors: limits, proxy, network

An agent that dies because of the environment has not failed at its task, so it must not burn the task's two-failure budget. Always classify first, then record with `orch.py error`:

| What you see (illustrative; confirm in a real session) | Kind |
|---|---|
| 429, "rate limit", "usage limit reached", "resets at ...", "quota" | `rate_limit` |
| 502/503/504/529, "overloaded", proxy error, `ECONNRESET`, `ETIMEDOUT`, "socket hang up", DNS or TLS failure, request timeout | `transient` |
| Wrong output, failing tests, agent gave up, permission denied by config | `task` (counts as an attempt) |

```bash
python <skill>/scripts/orch.py error <run_dir> <id> --kind transient|rate_limit|task --note "<short error>" [--wait-sec N]
```
Pass `--wait-sec` when the message states when the limit resets. The command applies the user's policy: it marks the task `waiting`, stores `next_retry_at`, and **pauses the whole run** (`paused_until`), because a limit or a proxy outage hits every agent, not just one. Call it for every agent that failed this way; the longest wait wins.

Then wait without spending tokens: run one `sleep <seconds>` (in the background, or in foreground chunks below the tool's timeout), never a loop of status messages. After the wait, call `next`: it relaunches due tasks with the same brief. `status` shows the policy, the pause, and total time waited.

When the budget is exhausted the command prints `EXHAUSTED` and, for `pause` and `ask`, **halts** the run. Tell the user what happened in plain words (which task, which error, how long you waited) and follow their policy: `pause` means stop and wait for them; `ask` means ask whether to resume, change the policy, or stop; `skip` means the task was blocked and you carry on with independent work and report it at the end. After the user fixes the cause, run `orch.py resume <run_dir>`; to change the numbers use `orch.py policy <run_dir> --limit-wait 1800 ...`.

If your own session hits its usage limit, you cannot wait it out. State is on disk: tell the user to resume after the reset by opening the project and asking you to continue the run (`status`, then `stale`, then `next`).

## Failure handling

- First failure of a task (a real task failure, see the table above): relaunch once with the error and the agent's last output appended to the brief; `orch.py set ... failed` increments attempts.
- Second failure: stop retrying. Either do it yourself, split it smaller, or ask the user. Never loop.
- Partial results are still results. If 5 of 6 readers succeeded, synthesize and flag the gap rather than blocking.
- If an agent exceeds its budget (files to open, max output size), it must stop and report what it has.
- Silent deaths: `orch.py stale <run_dir> --minutes 30` lists `running` tasks that are overdue and says whether an output file exists. Check it when notifications stop arriving.
- To redo a task cleanly: `orch.py set <run_dir> <id> pending` clears its branch, start_sha and merge state.

## Token economy

- Threshold: a task under roughly five minutes of work is cheaper to do yourself than to brief.
- Cheapest model that can do the job: haiku explores and greps, sonnet analyzes and implements, opus plans, synthesizes hard decisions, and reviews.
- Cap output: findings under ~400 words, return messages under 5 lines.
- Waves, not floods: respect the concurrency limits in `plan.json` (`max_parallel`). More agents than the dependency graph allows only burns tokens.
- Dedup: before launching a new reader, check `findings/INDEX.md` (one line per finished finding) for an existing answer.
- Do not paste agent outputs into your own context; reference the file path.

## Return contract (every agent)

Every brief ends with this, verbatim, so results are parseable:
```
STATUS: done | failed | blocked
OUTPUT: <path to full output file>
SUMMARY: <1-2 sentences>
CONFIDENCE: verified | inferred | uncertain
NEW_TASKS: <none | bullets of proposed extra work, not started>
```

## Optional hardening and verification

- `references/hooks.md`: a `PreToolUse` guard that blocks a writer from editing outside its write-set or in the main checkout. Strongly recommended for runs with 3+ writers.
- `references/verify-assumptions.md`: eight short experiments to run once in Claude Code. Parts of this skill rest on behavior (parallel launch, worktree base, nesting limits, permission handling) that has not been confirmed in a live session. If the user has not run them, say so when something behaves unexpectedly, and suggest running them.
- `scripts/selftest.py` checks the scripts and hook end to end without any agents.

## Resuming

If you are resuming or were compacted: run `orch.py status`, read `DECISIONS.md` if it exists, then `stale`, then `next`. A task marked `running` with no notification pending may have died with the previous session: if its output file exists, inspect it and mark it done or failed; otherwise relaunch it.
