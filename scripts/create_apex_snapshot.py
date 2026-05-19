"""Create a read-only APEX status snapshot file.

Captures: git commit, safety status, DB observation report, debug report, and
recent logs (if journalctl is available). Writes to /tmp by default.

Usage (run from repo root):
    PYTHONPATH=src python scripts/create_apex_snapshot.py
    PYTHONPATH=src python scripts/create_apex_snapshot.py --out /tmp/my_snapshot.txt

Does not modify any data. Safe to run in any environment.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

# Ensure scripts/ is importable when running from repo root via PYTHONPATH=src
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR.parent))


def _run(cmd: list[str], *, timeout: int = 10) -> str:
    """Run a subprocess and return stripped stdout. Returns error message on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return f"(command not found: {cmd[0]})"
    except subprocess.TimeoutExpired:
        return f"(timed out: {' '.join(cmd)})"
    except Exception as e:
        return f"(error: {e})"


def _section(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append("=" * 68)
    lines.append(f"  {title}")
    lines.append("=" * 68)


def _capture_script(fn, argv: list[str] | None = None) -> str:
    """Capture stdout from a script's main() function."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(argv)
    except SystemExit:
        pass  # scripts call sys.exit(0) on success or sys.exit(1) on error
    except Exception as e:
        buf.write(f"\n  ERROR running script: {e}\n")
    return buf.getvalue()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create a read-only APEX status snapshot")
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Output file path (default: /tmp/apex_snapshot_YYYYMMDD_HHMMSS.txt)",
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TIMESTAMP",
        default=None,
        help="Pass --since to the report and debug scripts",
    )
    args = parser.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M%S")

    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path(f"/tmp/apex_snapshot_{ts}.txt")

    lines: list[str] = []

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    lines.append("APEX Status Snapshot")
    lines.append(f"Created: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC")
    if args.since:
        lines.append(f"Filtered since: {args.since}")

    # ------------------------------------------------------------------
    # Git commit
    # ------------------------------------------------------------------
    _section(lines, "Git")
    git_commit = _run(["git", "log", "-1", "--format=%H %s (%ad)", "--date=short"])
    git_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    git_status = _run(["git", "status", "--short"])
    lines.append(f"  branch : {git_branch}")
    lines.append(f"  commit : {git_commit}")
    if git_status:
        lines.append(f"  dirty  : yes")
        for line in git_status.splitlines():
            lines.append(f"    {line}")
    else:
        lines.append(f"  dirty  : no (clean)")

    # ------------------------------------------------------------------
    # Service / safety status (systemctl)
    # ------------------------------------------------------------------
    _section(lines, "Service Status (systemctl)")
    systemctl_out = _run(["systemctl", "is-active", "apex"])
    if "command not found" in systemctl_out or "error" in systemctl_out.lower():
        lines.append("  systemctl not available on this host")
    else:
        lines.append(f"  apex service: {systemctl_out}")
        status_detail = _run(["systemctl", "status", "apex", "--no-pager", "-l"], timeout=5)
        for line in status_detail.splitlines()[:15]:
            lines.append(f"  {line}")

    # ------------------------------------------------------------------
    # /status API endpoint (if service is running locally)
    # ------------------------------------------------------------------
    _section(lines, "API /status")
    curl_out = _run(["curl", "-s", "--max-time", "3", "http://127.0.0.1:8000/status"])
    if curl_out and not curl_out.startswith("("):
        # Pretty-print JSON without importing json (keep it light)
        try:
            import json as _json
            parsed = _json.loads(curl_out)
            for k, v in parsed.items():
                lines.append(f"  {k}: {v}")
        except Exception:
            lines.append(curl_out[:500])
    else:
        lines.append(f"  {curl_out or '(no response — service may not be running)'}")

    # ------------------------------------------------------------------
    # Signal observations report
    # ------------------------------------------------------------------
    _section(lines, "Signal Observations Report")
    try:
        import scripts.report_signal_observations as _report
        report_argv = ["--since", args.since] if args.since else []
        report_out = _capture_script(_report.main, report_argv)
        lines.append(report_out)
    except ImportError as e:
        lines.append(f"  Could not import report script: {e}")
        lines.append("  Run with PYTHONPATH=src from repo root.")

    # ------------------------------------------------------------------
    # Debug conditions report
    # ------------------------------------------------------------------
    _section(lines, "Signal Debug Report")
    try:
        import scripts.debug_signal_conditions as _debug
        debug_argv = ["--since", args.since] if args.since else []
        debug_out = _capture_script(_debug.main, debug_argv)
        lines.append(debug_out)
    except ImportError as e:
        lines.append(f"  Could not import debug script: {e}")

    # ------------------------------------------------------------------
    # Recent ERROR / Traceback logs (journalctl)
    # ------------------------------------------------------------------
    _section(lines, "Recent ERROR / Traceback Logs (journalctl, last 24h)")
    journal_errors = _run(
        [
            "journalctl", "-u", "apex", "--no-pager",
            "--since", "24 hours ago",
            "-p", "err",
            "-n", "50",
        ],
        timeout=10,
    )
    if "command not found" in journal_errors or "error" in journal_errors.lower()[:20]:
        lines.append("  journalctl not available or no apex service logs")
    elif not journal_errors:
        lines.append("  No ERROR-level log entries in the last 24 hours")
    else:
        for line in journal_errors.splitlines()[:50]:
            lines.append(f"  {line}")

    # ------------------------------------------------------------------
    # Recent scan summary logs (journalctl INFO, last 2h)
    # ------------------------------------------------------------------
    _section(lines, "Recent Scan Summary Logs (journalctl, last 2h)")
    journal_scan = _run(
        [
            "journalctl", "-u", "apex", "--no-pager",
            "--since", "2 hours ago",
            "-n", "100",
            "--grep", "Scan complete",
        ],
        timeout=10,
    )
    if "command not found" in journal_scan or journal_scan.startswith("("):
        lines.append("  journalctl not available on this host")
    elif not journal_scan:
        lines.append("  No scan summary lines in the last 2 hours")
    else:
        for line in journal_scan.splitlines()[:30]:
            lines.append(f"  {line}")

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    content = "\n".join(lines) + "\n"
    out_path.write_text(content, encoding="utf-8")

    print(f"Snapshot written to: {out_path}")


if __name__ == "__main__":
    main()
