---
name: apex-rebaseline
description: Establish a local repository baseline (branch, HEAD, working-tree state, cached origin relationship) before non-trivial Builder work on APEX. Local/read-only.
---

# apex-rebaseline

Establish a repeatable **local** baseline before starting non-trivial
Builder work. This skill gathers evidence only — it does not plan or make
any change, and it does not transition into Builder work on its own. See
[`CLAUDE.md`](../../../CLAUDE.md) §5 for where this fits in the workflow
and [`.claude/rules/git-safety.md`](../../rules/git-safety.md) for why
this check happens before edits.

## Procedure

Using simple, read-only operations only, gather:

1. Confirm this is the APEX repository (working directory / a marker file
   such as `CLAUDE.md`).
2. Current branch (`git branch --show-current`).
3. Current local `HEAD` SHA (`git rev-parse HEAD`).
4. Working-tree clean/dirty state (`git status --short`).
5. If dirty, a concise changed-file list (from the same `git status`
   output) — not a full diff dump.
6. If dirty, a concise diff summary (`git diff --stat`), not the full
   diff body, unless the main session specifically needs more detail.
7. Local relation to already-cached remote-tracking refs, if any exist
   locally (e.g. `git rev-parse origin/<branch>` and
   `git rev-list --left-right --count origin/<branch>...HEAD`), using
   whatever is already cached — **do not** `git fetch` or `git pull` to
   refresh it first.

## No mutation, ever

This skill never performs: `git fetch`, `git pull`, `git commit`,
`git push`, `git merge`, `git rebase`, `git reset`, `git checkout`,
package installs, SSH, or any production access. It only reads local git
state.

## Evidence discipline (required)

Follow [`.claude/rules/evidence-discipline.md`](../../rules/evidence-discipline.md).
Specifically, keep these layers distinct and never blur them:

- **Local working tree** — files on disk right now.
- **Local Git refs** — `HEAD` and local branch refs as this checkout has
  them.
- **Cached origin refs** — `origin/<branch>` as last fetched by some
  earlier operation; this may be stale. State that it's cached, and that
  it was not refreshed by this skill.
- **Live GitHub** — never claim this. A cached origin ref is not live
  GitHub state; this skill has no way to check live GitHub without a
  network operation it does not perform.
- **VPS disk** — never claim this; out of scope for this skill.
- **Loaded production runtime** — never claim this; out of scope for this
  skill.

Never claim local `HEAD` equals production unless production evidence was
separately supplied to you or separately inspected in this same session —
this skill alone never establishes that.

If a requested fact would require live or external evidence (a fresh
fetch, live GitHub state, VPS/production state), do not go get it — report
it as **INCOMPLETE EVIDENCE** and name exactly what the main session would
need to inspect separately (e.g. "run `git fetch origin` to refresh the
cached ref," or "check the VPS runbook/session for deployed state").

## Required output

# APEX Rebaseline

- Repository:
- Branch:
- Local HEAD:
- Working tree:
- Changed files:
- Cached origin relationship:
- External/live state verified:
- Incomplete evidence:
- Safe baseline established: YES / NO

Stop after reporting the baseline. Do not automatically continue into
planning or edits — that is a separate, explicit step.
