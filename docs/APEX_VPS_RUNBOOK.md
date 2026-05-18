# APEX VPS Runbook

_Last updated: 2026-05-18_

This document captures APEX VPS deployment details, commands, safety settings, and recovery notes.

> **Note:** A local copy of this file (`docs/APEX_VPS_RUNBOOK.local.md`) may contain live server details
> such as IP addresses. That file is excluded from git. This file uses placeholders instead.

---

## 1. Current State

APEX is deployed on a Hetzner VPS and running as a `systemd` service in **dry-run mode only**.

Verified:

- VPS is reachable by SSH.
- Ubuntu server is prepared with Python, Git, SQLite, and build tools.
- APEX repo is cloned to `/opt/apex`.
- Python virtual environment exists at `/opt/apex/.venv`.
- Dependencies are installed.
- Editable package install works after the setuptools backend fix.
- SQLite DB initialized at `/opt/apex/data/apex.db`.
- Unit tests passed on VPS: `82/82`.
- Hyperliquid smoke test passed.
- Manual runtime test passed.
- HTTP action-route smoke test passed: `21/21`.
- `systemd` service installed, enabled, and running.
- Scheduler scans are running every minute.
- WebSocket connects to Hyperliquid.
- No live alerts are enabled.
- No trading/private keys are configured.

Latest confirmed status:

```text
apex.service: active (running)
APEX env: production
alerts_enabled: false
dry_run_mode: true
scheduler_running: true
pushover_configured: false
action_links_enabled: false
```

---

## 2. VPS Information

```text
Provider: Hetzner Cloud
OS: Ubuntu 24.04 LTS
IPv4: YOUR_VPS_IP
```

SSH from your Mac:

```bash
ssh root@YOUR_VPS_IP
```

Disconnect cleanly:

```bash
exit
```

APEX keeps running after SSH disconnects because it is managed by `systemd`.

---

## 3. Important Paths

```text
Repo path on VPS:      /opt/apex
Venv path:             /opt/apex/.venv
Environment file:      /opt/apex/.env
SQLite DB:             /opt/apex/data/apex.db
Service file:          /etc/systemd/system/apex.service
Example service file:  /opt/apex/deploy/apex.service.example
Deployment docs:       /opt/apex/deploy/VPS_SETUP.md
Logs:                  journalctl -u apex
```

Do **not** paste `/opt/apex/.env` into chat.

---

## 4. Repo / Commit State

GitHub repo:

```text
https://github.com/AraldoYerolis/APEX
```

Packaging fix made during initial deployment:

```toml
build-backend = "setuptools.build_meta"
```

This replaced the broken backend:

```toml
build-backend = "setuptools.backends.legacy:build"
```

---

## 5. Current Safety Settings

The VPS `.env` was created with safe defaults.

Important expected settings:

```ini
APEX_ENV=production
ALERTS_ENABLED=false
DRY_RUN_MODE=true
ALERT_TYPES_ENABLED=CONFIRMED_SETUP
APEX_PUBLIC_BASE_URL=
```

Expected status:

```text
Pushover configured: false
Action links enabled: false
Live alerts: disabled
Live trading: not implemented / not configured
Exchange private keys: none
```

Do **not** do these yet:

```text
Do not set ALERTS_ENABLED=true.
Do not set DRY_RUN_MODE=false.
Do not add exchange private keys.
Do not expose port 8000 publicly.
Do not rely on action links until HTTPS and APEX_PUBLIC_BASE_URL are configured.
```

---

## 6. Firewall / Network

Hetzner firewall allows:

```text
TCP 22   SSH
TCP 80   HTTP
TCP 443  HTTPS
ICMP      Optional ping
```

Do **not** expose:

```text
TCP 8000
```

APEX runs internally at:

```text
http://127.0.0.1:8000
```

Later, Caddy can expose it safely through HTTPS on ports 80/443.

---

## 7. Systemd Commands

Check service:

```bash
systemctl status apex --no-pager -l
```

Start:

```bash
systemctl start apex
```

Stop:

```bash
systemctl stop apex
```

Restart:

```bash
systemctl restart apex
```

Enable on boot:

```bash
systemctl enable apex
```

Disable on boot:

```bash
systemctl disable apex
```

View service file:

```bash
cat /etc/systemd/system/apex.service
```

Reload after service-file edits:

```bash
systemctl daemon-reload
```

---

## 8. Health and Status Checks

Run on the VPS:

```bash
curl -s http://127.0.0.1:8000/health
echo

curl -s http://127.0.0.1:8000/status
echo
```

Expected health:

```json
{"status":"ok","service":"apex"}
```

Expected status fields:

```json
{
  "status": "ok",
  "apex_env": "production",
  "alerts_enabled": false,
  "dry_run_mode": true,
  "alert_types_enabled": ["CONFIRMED_SETUP"],
  "pushover_configured": false,
  "action_links_enabled": false,
  "daily_lockout": false,
  "scheduler_running": true
}
```

---

## 9. Log Commands

Recent logs:

```bash
journalctl -u apex -n 120 --no-pager
```

Live logs:

```bash
journalctl -u apex -f
```

Logs from last 10 minutes:

```bash
journalctl -u apex --since "10 minutes ago" --no-pager
```

Scan summaries, suppressed signals, dry-run alerts, and errors:

```bash
journalctl -u apex --since "12 hours ago" --no-pager | grep -E "Scan complete|WOULD-BE|SUPPRESSED|ERROR|Traceback" || true
```

Expected scan examples:

```text
[DRY RUN] Scan complete: 9 markets scanned, 0 candidate signals, 0 sent
[DRY RUN] SUPPRESSED (SETUP_FORMING not in ALERT_TYPES_ENABLED) | TON LONG
[DRY RUN] Scan complete: 9 markets scanned, 1 candidate signals, 0 sent, 1 suppressed by type
```

Important: scan summaries should say `0 sent` while `ALERTS_ENABLED=false`.

---

## 10. Tests and Smoke Tests

Run unit tests:

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && PYTHONPATH=src .venv/bin/python -m pytest tests/ -v'
```

Expected:

```text
82 passed
```

Run Hyperliquid smoke test:

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_hyperliquid.py'
```

Run HTTP action-route smoke test. APEX must already be running:

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_action_routes.py'
```

Expected:

```text
Results: 21/21 passed, 0 failed
All checks passed.
```

Pushover smoke test is skipped for now because secrets are not configured. When ready, edit `/opt/apex/.env` directly on the VPS and do not paste secrets into chat.

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_pushover.py'
```

Note: the Pushover smoke test sends one real test notification by design.

---

## 11. Updating APEX on the VPS

Use this after changes are committed and pushed from your Mac:

```bash
cd /opt/apex

runuser -u apex -- bash -lc '
set -e
cd /opt/apex

echo "=== pull latest ==="
git pull

echo "=== install dependencies if changed ==="
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .

echo "=== run tests ==="
PYTHONPATH=src .venv/bin/python -m pytest tests/ -v

echo "=== status ==="
git status --short
'

systemctl restart apex
systemctl status apex --no-pager -l
curl -s http://127.0.0.1:8000/status
echo
```

Before restarting, confirm `ALERTS_ENABLED=false` and `DRY_RUN_MODE=true` remain in `/opt/apex/.env`.

---

## 12. Backup Commands

Backup SQLite DB:

```bash
cp /opt/apex/data/apex.db /opt/apex/data/apex.db.bak-$(date +%Y%m%d%H%M%S)
```

List DB backups:

```bash
ls -lh /opt/apex/data/apex.db.bak-*
```

Check DB tables:

```bash
sqlite3 /opt/apex/data/apex.db ".tables"
```

Check latest alerts:

```bash
sqlite3 /opt/apex/data/apex.db "select symbol, alert_type, status, sent_at from alerts order by sent_at desc limit 20;"
```

In current dry-run mode, dry-run would-be alerts should **not** create rows in `alerts`.

---

## 13. Troubleshooting

If APEX is not running:

```bash
systemctl status apex --no-pager -l
journalctl -u apex -n 120 --no-pager
```

If systemd sandboxing causes a startup failure, check:

```bash
journalctl -u apex -n 100 --no-pager
```

The service includes:

```ini
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ReadWritePaths=/opt/apex/data
```

If the failure is a filesystem permission issue, temporarily comment out these lines in `/etc/systemd/system/apex.service`:

```ini
# PrivateDevices=true
# ProtectSystem=strict
# ReadWritePaths=/opt/apex/data
```

Then:

```bash
systemctl daemon-reload
systemctl restart apex
journalctl -u apex -n 100 --no-pager
```

Do not leave sandboxing disabled permanently unless there is a documented reason.

If Git says "dubious ownership", run Git as the `apex` user:

```bash
runuser -u apex -- git -C /opt/apex status --short
```

If `.cache/` appears in git status:

```bash
runuser -u apex -- bash -lc '
cd /opt/apex
rm -rf .cache .lesshst
git status --short
'
```

---

## 14. Security Notes

Never paste these into chat:

```text
/opt/apex/.env
PUSHOVER_APP_TOKEN
PUSHOVER_USER_KEY
ACTION_TOKEN_SECRET
SSH private key
Credit card details
Future exchange private keys
```

Public SSH key is okay to paste into Hetzner. Private SSH key is not.

Mac SSH private key:

```text
~/.ssh/id_ed25519
```

Mac SSH public key:

```text
~/.ssh/id_ed25519.pub
```

---

## 15. Morning Check

Reconnect:

```bash
ssh root@YOUR_VPS_IP
```

Run:

```bash
systemctl status apex --no-pager -l

curl -s http://127.0.0.1:8000/status
echo

journalctl -u apex --since "12 hours ago" --no-pager | grep -E "Scan complete|WOULD-BE|SUPPRESSED|ERROR|Traceback" || true
```

Look for:

```text
Active: active (running)
scheduler_running: true
alerts_enabled: false
dry_run_mode: true
0 sent
No Traceback
No ERROR
```

---

## 16. Recommended Next Milestone

Do not add more product features yet.

Next milestone:

```text
Milestone 9: Dry-run signal quality review
```

Goal:

```text
Let APEX run for 24–72 hours.
Collect logs for candidate signals.
Review signal frequency, suppressed signals, WOULD-BE confirmed setups, errors, reconnects, and whether the strategy appears useful.
```

Possible future work after enough dry-run data:

```text
Add a reporting script to summarize dry-run scans and candidate signals.
Add a DB-backed signal audit table if dry-run logs are insufficient.
Configure Pushover only after dry-run signal quality is understood.
Configure Caddy/HTTPS and APEX_PUBLIC_BASE_URL only after service stability is confirmed.
Enable live alerts only after explicit approval.
```

---

## 17. Quick Command Cheat Sheet

Reconnect:

```bash
ssh root@YOUR_VPS_IP
```

Check service:

```bash
systemctl status apex --no-pager -l
```

Check status API:

```bash
curl -s http://127.0.0.1:8000/status
echo
```

Recent logs:

```bash
journalctl -u apex -n 120 --no-pager
```

Live logs:

```bash
journalctl -u apex -f
```

Restart APEX:

```bash
systemctl restart apex
```

Stop APEX:

```bash
systemctl stop apex
```

Start APEX:

```bash
systemctl start apex
```

Run tests:

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && PYTHONPATH=src .venv/bin/python -m pytest tests/ -v'
```

Confirm repo clean:

```bash
runuser -u apex -- bash -lc 'cd /opt/apex && git status --short'
```

Pull latest safely:

```bash
runuser -u apex -- bash -lc '
set -e
cd /opt/apex
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
PYTHONPATH=src .venv/bin/python -m pytest tests/ -v
'
systemctl restart apex
```
