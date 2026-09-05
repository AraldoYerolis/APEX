# Git Safety

## Before touching anything

- Inspect current branch, `HEAD` SHA, and working-tree cleanliness (`git
  status`) before making any edit, and again before any commit/push.
- Read the actual files you're about to change before editing them — don't
  assume their contents from memory or from a task description.
- Explicitly distinguish local `main`, `origin/main`, and any feature branch
  — they can and do diverge. Never assume they're identical without
  checking.
- A branch existing on disk, or even pushed to GitHub, is not evidence it is
  deployed anywhere. Deployment is a separate, verifiable fact.

## Standing restrictions

- Never `git commit`, `git push`, or `git merge` unless Aaron explicitly
  approves that specific action. One approval covers that action, not
  future ones.
- Never force-push (`--force`, `--force-with-lease`, `-f`, or a forcing
  `+refspec`). This is policy, backed by deny rules in
  [`.claude/settings.json`](../settings.json) that match realistic
  force-bearing spellings of `git push` — leading or trailing flag position,
  a leading `+` on the refspec, with or without preceding `git -c` options.
  A plain `git push` is also policy-restricted (Aaron must approve it),
  enforced there by an ask rule rather than a deny rule. Permission rules
  constrain the commands Claude Code itself runs; they don't reach a shell
  alias or a git config rewrite of `push` run outside Claude Code's own
  tools. A command spelling that manages to fall outside every deny and ask
  rule still isn't silently executed: with Auto Mode and bypassPermissions
  mode disabled project-wide, any Bash command that doesn't match an allow
  rule falls through to a human approval prompt. That fallback is not the
  same guarantee as matching the specific rule — don't remove
  `disableAutoMode` or `permissions.disableBypassPermissionsMode` on the
  assumption that the deny rules alone are sufficient.
- Never run a destructive reset (`git reset --hard`) or destructive clean
  (`git clean -f`/`-fd`/etc.) without Aaron explicitly requesting exactly
  that, and only after confirming there's nothing uncommitted worth keeping.

## SHA discipline

- When a task states specific SHAs as the proven/tested baseline (e.g. "this
  exact commit is what's running in production"), preserve them exactly.
  Do not rebase, amend, or otherwise rewrite history that a production
  validation depends on.
- If local `main`, `origin/main`, or the stated production SHA don't line
  up the way a task assumes, stop and report the discrepancy — don't
  silently reconcile them by resetting, force-pushing, or merging.
