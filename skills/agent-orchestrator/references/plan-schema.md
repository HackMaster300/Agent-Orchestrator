# plan.json schema

Located at `.claude/runs/<run-id>/plan.json`. Only the orchestrator edits it.

## Top level

| Field | Meaning |
|---|---|
| `goal` | One-line objective of the run |
| `mode` | `checkpoint` (default: pause for approval at Checkpoint A and B) or `auto` |
| `run_id`, `run_dir` | Run name and its absolute path (set by `init`) |
| `base_sha` | Commit readers analyze and the integration branch starts from |
| `integration_branch`, `integration_dir` | `orch/<run-id>` and its worktree; writers merge here |
| `retry_policy` | Back-off settings chosen at the start (see below) |
| `paused_until`, `halted`, `waited_total_sec` | Run-wide pause after limit/proxy errors, halt reason, time spent waiting |
| `test_cmd` | Command run after each merge; a failure reverts that merge |
| `max_parallel` | Concurrency per kind, defaults `read:6, synth:1, write:3, review:2` |
| `tasks` | List of task objects |

## Task

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Short unique id: `r1`, `r2`, `s1`, `w1`, `v1` |
| `kind` | yes | `read`, `synth`, `write`, `review` |
| `title` | yes | One line, what the task achieves |
| `deps` | no | Ids that must be `done` first |
| `write_set` | write only | Files, dirs or globs the task may change. Concurrent writers must be disjoint |
| `model` | no | `haiku`, `sonnet`, `opus` |
| `output` | no | Output file path relative to the run dir (defaults to `findings/<id>.md` or `results/<id>.md`) |
| `status` | auto | `pending`, `running`, `waiting` (retry scheduled), `done`, `failed`, `blocked` |
| `attempts` | auto | Real task failures; at 2, stop retrying |
| `infra_retries`, `next_retry_at`, `last_error` | auto | Limit/proxy/network errors; counted separately from `attempts` |
| `summary` | auto | The agent's one-line summary |
| `branch` | auto | Writer's branch, recorded at completion |
| `start_sha` | auto | Integration tip when a writer was launched; the commit it must build on |
| `merged` / `merge_note` | auto | Set by `integrate`; dependents of a writer unlock only when `merged` is true |
| `started_at` | auto | Used by `stale` |

## Worked example

Goal: "Replace the legacy session handling with JWT auth and update the docs."

```json
{
  "goal": "Replace legacy sessions with JWT auth, update docs",
  "mode": "checkpoint",
  "base_sha": "a1b2c3d",
  "max_parallel": {"read": 6, "synth": 1, "write": 3, "review": 2},
  "tasks": [
    {"id": "r0", "kind": "read", "model": "haiku", "title": "Build CONTEXT.md: repo map, conventions, test commands"},
    {"id": "r1", "kind": "read", "model": "haiku", "deps": ["r0"], "title": "Find every use of session middleware"},
    {"id": "r2", "kind": "read", "model": "sonnet", "deps": ["r0"], "title": "Map auth-related tests and fixtures"},
    {"id": "r3", "kind": "read", "model": "sonnet", "deps": ["r0"], "title": "Compare JWT libs already in dependencies"},
    {"id": "s1", "kind": "synth", "model": "opus", "deps": ["r1", "r2", "r3"], "title": "Write DECISIONS.md"},
    {"id": "w1", "kind": "write", "model": "sonnet", "deps": ["s1"], "title": "Implement JWT service and middleware",
     "write_set": ["src/auth/**"]},
    {"id": "w2", "kind": "write", "model": "sonnet", "deps": ["s1"], "title": "Migrate API routes to new middleware",
     "write_set": ["src/api/**"]},
    {"id": "w3", "kind": "write", "model": "sonnet", "deps": ["s1"], "title": "Update auth docs",
     "write_set": ["docs/auth/**"]},
    {"id": "w4", "kind": "write", "model": "sonnet", "deps": ["w1", "w2"], "title": "Update tests",
     "write_set": ["tests/**"]},
    {"id": "v1", "kind": "review", "model": "opus", "deps": ["w1", "w2", "w3", "w4"], "title": "Verify result against DECISIONS.md"}
  ]
}
```

Resulting waves: `r0` → (`r1`,`r2`,`r3` in parallel) → `s1` gate → (`w1`,`w2`,`w3` in parallel; disjoint write-sets) → **integrate** → `w4` (starts from the integrated result of `w1` and `w2`) → **integrate** → `v1`.

## retry_policy

| Key | Default | Meaning |
|---|---|---|
| `transient_wait_sec` | 30 | Wait before the first retry after a proxy/network/5xx error |
| `backoff_factor` | 2 | Each further transient error waits this many times longer |
| `max_wait_sec` | 600 | Cap for a single transient wait |
| `limit_wait_sec` | 900 | Wait after a rate/usage limit (overridden by `--wait-sec` when the reset time is known) |
| `max_infra_retries` | 5 | Per task; `0` = fail fast |
| `max_total_wait_min` | 180 | Total time the run may spend waiting |
| `on_exhausted` | `pause` | `pause`, `ask`, or `skip` |

Change them at any time with `orch.py policy <run_dir> --limit-wait 1800 ...`.

## Choosing write-sets

- Prefer directories or globs over single files; under-declaring is the usual error.
- If two tasks both need a shared file (types, config, lockfile), either give that file to one task and make the other depend on it, or extract it into its own earlier task.
- Generated files and lockfiles belong to the integration phase, not to a writer.
- If you cannot make write-sets disjoint, the work is sequential. Say so; do not force parallelism.

## Run directory layout

```
.claude/runs/<run-id>/
├── plan.json
├── CONTEXT.md        # shared repo knowledge (from r0)
├── DECISIONS.md      # synthesis gate output
├── findings/         # one file per read task, plus INDEX.md
├── results/          # one file per write/review task, plus INTEGRATION.md
└── briefs/           # the exact brief sent to each agent, for debugging
```
