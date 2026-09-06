# Production Safety

APEX's production instance is a real, running system on a Hetzner VPS,
currently operating in dry-run/alert-only mode. These rules exist so that
development work in this repository can never, by itself, move that system
closer to live trading or live alerting.

## Trading and alerts

- Never enable live trading, never write code whose purpose is to place,
  modify, or cancel live orders, without Aaron's explicit approval given in
  the moment for that specific change.
- Never add, request, or use exchange private keys, wallet keys, or any
  other live trading credential.
- `ALERTS_ENABLED` must remain `false` unless Aaron explicitly approves
  turning it on.
- `DRY_RUN_MODE` must remain `true` unless Aaron explicitly approves turning
  it off.
- Any change touching the feed layer, runtime/scheduler, signal scoring,
  risk sizing, or execution/order-routing code requires elevated validation
  (explicit test evidence, and explicit Aaron sign-off) before it is
  considered done — these are the paths closest to real trading behavior.

## Environment and secrets

- Never mutate `.env` (or any `.env.*` file) without Aaron's explicit
  approval, even to "just fix a typo."
- Never print, log, or otherwise surface secret values.
- Known limitation: the `.env.*` permission pattern in
  [`.claude/settings.json`](../settings.json) also blocks reading the
  tracked, non-secret `.env.example` template. This is an accepted
  usability trade-off, not a bug to fix in this slice.
- Direct tool reads of `.env` are denied (`Read(./.env)`, `Read(./.env.*)`,
  `Edit(./.env)`, `Edit(./.env.*)`). Separately, `Bash(*.env*)` denies any
  shell command whose text contains `.env` — this closes the gap where
  `cat .env`, `head .env`, `grep ... .env`, and similar built-in read-only
  Bash commands could otherwise surface secrets without a prompt. This is
  defense in depth, not an OS-level sandbox: it is a text match on the
  command string, not a guarantee against every path-obfuscation or
  subprocess technique that could still reach `.env`'s contents (e.g. a
  script that reads the file without naming it in the command line).
  Stronger OS-level isolation of the working directory is a later
  sandboxing task, not part of this slice. As an accepted side effect, this
  rule also denies harmless commands that merely mention `.env.example` or
  any other filename containing the substring `.env` (e.g. `.envrc`,
  `something.envelope.txt`).

## Production infrastructure

- Never mutate the production database without explicit approval.
- Never run a systemd lifecycle action (start/stop/restart/enable/disable)
  against the production service without explicit approval.
- Never deploy to the VPS without explicit approval.
- Never install a package on the production host without explicit approval.
- No automatic rollback — if something on production looks wrong, report it
  and propose next steps; do not attempt to revert it unilaterally.
- These are two separate controls, not one: `.claude/settings.json`
  denies direct `Bash(systemctl *)` outright as a project permission
  baseline (it blocks the shell command from running at all, on any
  host), while this rule separately prohibits a production systemd
  lifecycle action without Aaron's explicit approval (it governs intent
  and scope, including any path that isn't a bare local `systemctl`
  invocation). Removing or narrowing one control must not be read as
  loosening the other.
- [`apex-vps-health`](../skills/apex-vps-health/SKILL.md) remains a
  separately approved, read-only production checklist: invoking it, or
  Aaron approving SSH access under it, is not itself approval for any
  systemd lifecycle action, deploy, or other production mutation — that
  still requires its own explicit approval per this section.

## Agents

[`docs/AGENT_ARCHITECTURE.md`](../../docs/AGENT_ARCHITECTURE.md) describes
APEX's own in-application analytic agents (Signal Quality, Setup Scoring,
Market Regime, etc.) — a separate concept from the Claude Code
Researcher/Builder/Validator development workflow (see
[`CLAUDE.md`](../../CLAUDE.md) §5); do not conflate the two. Once an
in-application agent is integrated into this project, it must be read-only:
it may read the database, candle store, and logs, but must never write
alerts, place trades, or change configuration. Promotion of any agent to an
active (write) role requires explicit human review and approval of its
prior read-only output, per the progression described in
`docs/AGENT_ARCHITECTURE.md`.
