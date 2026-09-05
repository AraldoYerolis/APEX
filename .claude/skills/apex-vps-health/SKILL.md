---
name: apex-vps-health
description: Human-invoked APEX production VPS health-check checklist, sourced from docs/APEX_VPS_RUNBOOK.md. Loading this skill never itself authorizes SSH or any production access — that requires Aaron's separate, explicit approval.
disable-model-invocation: true
---

# apex-vps-health

This skill loads a **checklist**, sourced from
[`docs/APEX_VPS_RUNBOOK.md`](../../../docs/APEX_VPS_RUNBOOK.md), for
reviewing APEX's production health on the Hetzner VPS. It does not invent
a new procedure — the runbook already defines one; this skill guides the
main session through it in order.

## The boundary — read this before anything else

Invoking `/apex-vps-health` means **"load the health-check procedure."**

It does **not** mean **"SSH to production"**, and loading this skill is
never itself Aaron's approval for SSH or any other production access.
Skill invocation and production-access approval are two separate events.

Before any SSH or other production access, the main session must:

1. Summarize the exact read-only checks it proposes to run (see
   "Checklist" below), naming the specific commands from the runbook.
2. State the target explicitly as **production** (the Hetzner VPS).
3. State plainly that no mutation, restart, or deploy will occur.
4. **Stop and wait for Aaron's explicit approval** before running
   anything against production.

Only after Aaron gives that separate, explicit approval may the main
session proceed to run the read-only checks below. If at any point a
check would require going beyond what was approved, stop again at that
new boundary and ask.

## Checklist (from the runbook, read-only only)

Once approved, guide the session through these runbook-defined checks —
see [`docs/APEX_VPS_RUNBOOK.md`](../../../docs/APEX_VPS_RUNBOOK.md) §8
(Health and Status Checks), §9 (Log Commands), and §15 (Morning Check) for
the exact commands:

- Service active/status (`systemctl status apex --no-pager -l`) — status
  inspection only, not a lifecycle action.
- Local health endpoint (`curl -s http://127.0.0.1:8000/health`).
- Local status endpoint (`curl -s http://127.0.0.1:8000/status`), which
  reports `alerts_enabled`, `dry_run_mode`, `scheduler_running`, and
  related safety flags.
- Bounded `journalctl` review (e.g. `--since "12 hours ago"`, filtered for
  `Scan complete|WOULD-BE|SUPPRESSED|ERROR|Traceback`) — a bounded window,
  not an unbounded log dump.
- Deployed Git SHA, only if it can be obtained read-only (e.g.
  `git status --short` / current commit on the VPS checkout) — if that
  needs a mutating git operation to get right, skip it and report
  INCOMPLETE EVIDENCE instead.
- Safety flag/status verification: `DRY_RUN_MODE`, `ALERTS_ENABLED`,
  `pushover_configured`, `action_links_enabled` — read from the `/status`
  endpoint output, never from `.env` directly.

## Hard restrictions — apply even after Aaron approves production access

This skill never grants, and the main session must not treat approval of
the checklist as approval for, any of the following:

- Do not grant or use SSH beyond what Aaron specifically approved for
  this check.
- Do not grant or assume broad `Bash` access — only the specific
  read-only commands listed above, and only after approval.
- Do not pre-approve any tool (this file intentionally carries no
  `allowed-tools`).
- Do not restart, stop, start, enable, or disable the service.
- Do not deploy or pull new code.
- Do not mutate the database (no writes, no schema changes).
- Do not modify any file, on the VPS or locally.
- Do not install or upgrade anything.
- Do not run any `systemctl` action beyond `status`.
- Do not read or print `.env` contents or any secret value — read safety
  flags from the `/status` endpoint, never from the env file.

If any single check cannot be performed within these restrictions, stop
at that specific boundary and ask Aaron for the specific additional
approval needed, rather than working around the restriction.

## Required output (only after an approved health check)

# APEX VPS Health Report

- Production evidence source:
- Service health:
- API health:
- Scheduler:
- Feed/WebSocket:
- Scan universe:
- DRY_RUN_MODE:
- ALERTS_ENABLED:
- Deployed SHA:
- Errors/warnings:
- Evidence not obtained:
- Mutations performed: NONE
- Overall health:
  HEALTHY / DEGRADED / INCOMPLETE

This skill is a checklist and a report, not a remediation tool. If
something is found unhealthy, **report it and stop** — do not proceed
into fixing, restarting, or deploying as a follow-on action.
