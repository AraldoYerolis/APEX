# APEX — Claude Code Project Guide

## 1. What APEX Is

APEX is a Hyperliquid perpetual-futures **alert/research bot** — it scans
public market data (HTTP + WebSocket) for trend-pullback setups and sends
actionable trade plans to a phone via Pushover. **It does not place orders;
all execution is manual.** Stack: Python 3.11+, FastAPI, SQLite, APScheduler.

A production instance runs on a Hetzner VPS as a `systemd` service in
**dry-run mode** (see [`docs/APEX_VPS_RUNBOOK.md`](docs/APEX_VPS_RUNBOOK.md)).
Development in this repository must preserve that research-only posture —
nothing here should move the system closer to live trading without Aaron's
explicit, separate approval.

## 2. Non-Negotiable Safety

- `DRY_RUN_MODE=true` and `ALERTS_ENABLED=false` unless Aaron explicitly
  approves otherwise.
- No live trading, no exchange/private keys, ever, without explicit approval.
- **Start Claude Code from the APEX repository root, not a subdirectory.**
  The project safety configuration in
  [`.claude/settings.json`](.claude/settings.json) is a version-controlled
  project safety baseline, not an enterprise-managed floor — it only takes
  effect for a session whose primary working directory is the repository
  root, and moving the session (`/cd`) can cause it to stop applying. If
  configuration-loading state is uncertain, stop and verify before any
  consequential action.

Full rules: [`.claude/rules/production-safety.md`](.claude/rules/production-safety.md).

## 3. Repository Orientation

- [`src/apex/`](src/apex/) — application code: `actions/`, `data/`, `db/`,
  `indicators/`, `notifications/`, `scheduler/`, `strategy/`, `utils/`
- [`tests/`](tests/) — pytest suite (one file per feature/module)
- [`scripts/`](scripts/) — operational and analysis scripts (smoke tests,
  report generators, DB init, dev runner)
- [`docs/`](docs/) — architecture and runbook docs, including
  [`AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) and the VPS runbook
- [`deploy/`](deploy/) — systemd unit and Caddy config examples for the VPS
- [`.claude/`](.claude/) — Claude Code config: `settings.json`
  (version-controlled project safety baseline — see §2), `settings.local.json`
  (machine-local, gitignored), and `rules/` (behavioral rules, auto-loaded)

## 4. Testing

Run the full suite from the repo root, using the project's own `.venv`
(`apex` is installed there in editable mode, so this works without setting
`PYTHONPATH`):

```bash
.venv/bin/python -m pytest tests/ -v
```

- Prefer running focused tests for the module you touched first
  (`.venv/bin/python -m pytest tests/test_<module>.py -v`), then the broader
  suite before declaring work done.
- The repo-wide Ruff lint currently has known, pre-existing debt — do not
  attempt to fix unrelated Ruff violations as part of another task.
- Any files you change must not introduce **new** Ruff violations, even if
  the file already had some.

## 5. Standard Development Workflow

The intended workflow is **Researcher → Builder → Validator**, with the
Researcher and Validator roles read-only. These are Claude Code development
roles — distinct from APEX's own in-application analytic agents described in
[`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md); do not conflate
the two. They now live at
[`.claude/agents/apex-researcher.md`](.claude/agents/apex-researcher.md) and
[`.claude/agents/apex-validator.md`](.claude/agents/apex-validator.md):

- **apex-researcher** performs unattended local static repository
  investigation only (`Read`/`Grep`/`Glob` on the working tree, no shell,
  no web, no git history) before planning or coding on any non-trivial
  change.
- **Builder** is this main Claude Code session: it makes approved edits,
  and it is the one that does web research, git diagnostics/history,
  `pytest`/Ruff/test execution, runtime/external checks, and anything
  else that may require approval, under normal project permissions.
- **apex-validator** performs unattended local static validation only
  (`Read`/`Grep`/`Glob` on the working tree plus whatever the Builder
  supplies it, no shell, no web, no execution) after the Builder's
  change, before testing, commit, push, or deploy. It does not repair
  its own findings — it reports them for the Builder or Aaron to
  address. If current external documentation is needed to validate
  behavior, the Builder obtains it and supplies the relevant facts to
  the Validator. When the Validator needs execution or git evidence, it
  reports that gap and names the exact check; the Builder runs it.
- If either agent fails to load or fails to run, tell Aaron explicitly
  rather than proceeding as if independent review occurred.

## 6. Approval Boundaries

Aaron alone approves:

- `git commit`, `git push`, `git merge`/`git rebase`
- production deploys, service restarts, or any VPS access
- reading, writing, or rotating secrets (`.env`, keys, tokens)
- any production database mutation
- any step toward enabling live trading or live alerts

Claude Code's own permission baseline for this project is defined in
[`.claude/settings.json`](.claude/settings.json) (see §2 for its scope and
limits). It denies realistic force-push spellings of `git push` outright and
requires explicit approval — via an ask rule, not a block — for a plain
`git push`, `git commit`, `git merge`, or `git rebase`. See
[`.claude/rules/git-safety.md`](.claude/rules/git-safety.md) and
[`.claude/rules/production-safety.md`](.claude/rules/production-safety.md)
for the full rationale.

## 7. Evidence Discipline

- Report branch, `HEAD` SHA, and working-tree cleanliness before and after
  changes.
- Distinguish local disk state, GitHub state, and production (VPS) state —
  never assume `main` equals production, or that code on disk is the code a
  running process actually loaded.
- Time-bound any claim based on logs; label claims as FACT, INFERENCE, or
  HYPOTHESIS.

Full rules: [`.claude/rules/evidence-discipline.md`](.claude/rules/evidence-discipline.md).

## 8. Document Pointers

- [`README.md`](README.md) — project overview and local setup
- [`docs/AGENT_ARCHITECTURE.md`](docs/AGENT_ARCHITECTURE.md) — agent design
- [`docs/AGENT_BACKLOG.md`](docs/AGENT_BACKLOG.md) — planned agent work
- [`docs/APEX_VPS_RUNBOOK.md`](docs/APEX_VPS_RUNBOOK.md) — production
  deployment state and recovery notes (a `.local.md` variant with live
  server details is gitignored)
