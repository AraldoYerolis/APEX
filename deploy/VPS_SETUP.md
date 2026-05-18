# APEX VPS Setup Guide

Target: Hetzner VPS running Ubuntu 22.04+

---

## Before You Start — Safety Defaults

APEX ships with alerts disabled. **Do not change these until you have verified
the full deployment is working.**

| Setting | Required default | Meaning |
|---|---|---|
| `ALERTS_ENABLED` | `false` | Bot scans and logs but never sends Pushover |
| `DRY_RUN_MODE` | `true` | Suppressed-alert log lines show `[DRY RUN]` |
| `ALERT_TYPES_ENABLED` | `CONFIRMED_SETUP` | Which types will send when enabled |
| `APEX_ENV` | `production` | Marks the runtime environment as production in logs and `/status` |

**Not yet — do not do these until explicitly ready:**
- Do not set `ALERTS_ENABLED=true`
- Do not rely on action links until HTTPS and `APEX_PUBLIC_BASE_URL` are confirmed
- Do not add exchange private keys (APEX does not use them — but note this for audits)
- Do not deploy without rotating `ACTION_TOKEN_SECRET` from the placeholder default
- Do not commit `.env` or `data/apex.db` to the repo (both are in `.gitignore`)

---

## Pre-deployment Checklist

Before first deploy, confirm all of the following:

- [ ] Local repo is clean: `git status` shows no uncommitted changes
- [ ] All local tests pass: `PYTHONPATH=src .venv/bin/python -m pytest tests/ -v`
- [ ] `.env` is NOT tracked by git: `git ls-files .env` returns nothing
- [ ] `data/apex.db` is NOT tracked by git: `git ls-files data/apex.db` returns nothing
- [ ] `ACTION_TOKEN_SECRET` has been replaced with a strong random value (see step 5)
- [ ] `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` are real credentials, not placeholders
- [ ] `ALERTS_ENABLED=false` in `.env` — will remain false through initial deployment
- [ ] `DRY_RUN_MODE=true` in `.env`
- [ ] `APEX_ENV=production` in `.env`

---

## 1. Install Python 3.11+

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev build-essential git
python3.11 --version   # should print 3.11.x or higher
```

---

## 2. Create dedicated user

```bash
sudo useradd -m -s /bin/bash apex
sudo mkdir -p /opt/apex
sudo chown apex:apex /opt/apex
```

---

## 3. Clone the repo

```bash
sudo -u apex git clone https://github.com/AraldoYerolis/APEX.git /opt/apex
cd /opt/apex
```

---

## 4. Create virtual environment and install dependencies

```bash
sudo -u apex python3.11 -m venv /opt/apex/.venv
sudo -u apex /opt/apex/.venv/bin/pip install --upgrade pip
sudo -u apex /opt/apex/.venv/bin/pip install -r /opt/apex/requirements.txt
sudo -u apex /opt/apex/.venv/bin/pip install -e /opt/apex
```

The `-e` install makes the `apex` package importable from the venv without
needing `PYTHONPATH=src`.

---

## 5. Configure environment

```bash
sudo -u apex cp /opt/apex/.env.example /opt/apex/.env
sudo -u apex nano /opt/apex/.env
```

Required changes from the example:

| Variable | Action |
|---|---|
| `APEX_ENV` | Change to `production` — marks the runtime environment in logs and `/status` |
| `APEX_PUBLIC_BASE_URL` | Leave empty until HTTPS and DNS are confirmed (steps 13–14) |
| `PUSHOVER_APP_TOKEN` | Set your Pushover app token |
| `PUSHOVER_USER_KEY` | Set your Pushover user key |
| `ACTION_TOKEN_SECRET` | **Generate and set a strong random value:** |

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Keep safety defaults exactly as-is:
```ini
ALERTS_ENABLED=false
DRY_RUN_MODE=true
ALERT_TYPES_ENABLED=CONFIRMED_SETUP
```

Verify the file is not world-readable:
```bash
sudo chmod 600 /opt/apex/.env
sudo chown apex:apex /opt/apex/.env
```

---

## 6. Create data directory

```bash
sudo -u apex mkdir -p /opt/apex/data/logs
```

---

## 7. Initialize database

```bash
sudo -u apex bash -c "cd /opt/apex && .venv/bin/python scripts/init_db.py"
```

---

## 8. Run unit tests on the VPS

Confirm the Python environment and code are intact before any further steps.

```bash
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python -m pytest tests/ -v"
```

All tests must pass. If any fail, do not proceed.

---

## 9. Run connectivity smoke tests

```bash
# Hyperliquid public API (no credentials required)
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_hyperliquid.py"

# Pushover delivery (requires PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY in .env)
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_pushover.py"
```

The Pushover smoke test sends one real notification to confirm credentials work.
This is intentional — it bypasses `ALERTS_ENABLED` by design.

---

## 10. First manual run — verify before systemd

**Always run APEX manually before enabling the systemd service.** This lets you
catch startup errors, confirm safety settings, and run the HTTP action-route
smoke test before locking the process into autostart.

```bash
sudo -u apex bash -c "cd /opt/apex && .venv/bin/python -m apex.main"
```

In the startup log, confirm ALL of the following lines appear:

```
APEX starting up
alerts_enabled       = False
dry_run_mode         = True
*** ALERTS_ENABLED=false: strategy alerts will be logged but NOT sent ***
*** DRY_RUN_MODE=true: suppressed-alert log lines will show [DRY RUN] ***
Database initialized
Scheduler started
```

While the server is running, in a second terminal verify:

```bash
# Health check
curl http://127.0.0.1:8000/health
# Expected: {"status":"ok","service":"apex"}

# Status check
curl http://127.0.0.1:8000/status
# Expected: JSON with "alerts_enabled": false, "dry_run_mode": true, "daily_lockout": false

# HTTP action-route smoke test (verifies action link lifecycle end-to-end)
cd /opt/apex
PYTHONPATH=src .venv/bin/python scripts/smoke_test_action_routes.py
# Expected: 21/21 passed, 0 failed
```

Stop the manual run with **Ctrl+C** and confirm the shutdown log shows:

```
APEX shutdown complete
```

with no traceback or `ProgrammingError`.

---

## 11. Install systemd service

Only proceed after the manual run in step 10 passes.

```bash
sudo cp /opt/apex/deploy/apex.service.example /etc/systemd/system/apex.service

# Review — adjust WorkingDirectory or paths only if your setup differs
sudo nano /etc/systemd/system/apex.service

sudo systemctl daemon-reload
sudo systemctl enable apex
sudo systemctl start apex
```

Check it came up cleanly:

```bash
sudo systemctl status apex
# Expected: Active: active (running)

sudo journalctl -u apex -n 50
# Should show normal startup log with alerts_enabled=False
```

**Sandboxing note:** `apex.service.example` restricts filesystem writes to
`/opt/apex/data` via `ProtectSystem=strict` and `ReadWritePaths`. If the
service fails to start after enabling these directives, inspect the journal:

```bash
sudo journalctl -u apex -n 100
```

If the failure is a filesystem permission or sandboxing error, temporarily
comment out the three sandboxing lines in `/etc/systemd/system/apex.service`:

```ini
# PrivateDevices=true
# ProtectSystem=strict
# ReadWritePaths=/opt/apex/data
```

Then run `sudo systemctl daemon-reload && sudo systemctl restart apex` to
confirm the service starts cleanly. Identify the missing writable path, add it
to `ReadWritePaths`, and re-enable the hardening. Do not leave sandboxing
disabled permanently without a documented reason.

---

## 12. Check logs

```bash
# Live log stream
sudo journalctl -u apex -f

# Recent entries
sudo journalctl -u apex -n 100

# Scan summary lines (confirm dry-run mode is active)
sudo journalctl -u apex | grep "Scan complete"
# Should show: [DRY RUN] Scan complete: ...
```

---

## 13. Optional: Caddy reverse proxy

Skip this step until you have a domain pointing at the VPS.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# Copy and edit the example Caddyfile
sudo cp /opt/apex/deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile   # replace apex.example.com with your real domain

sudo systemctl enable caddy
sudo systemctl start caddy
```

Caddy automatically obtains a TLS certificate via Let's Encrypt. Confirm:

```bash
sudo systemctl status caddy
curl https://your-domain.com/health
```

---

## 14. Update APEX_PUBLIC_BASE_URL

Only after Caddy is running and the domain resolves to your VPS:

```bash
sudo -u apex nano /opt/apex/.env
# Set: APEX_PUBLIC_BASE_URL=https://your-domain.com
```

```bash
sudo systemctl restart apex
sudo journalctl -u apex -n 20
```

Pushover alerts will now include clickable action links.

---

## 15. Stopping and restarting

```bash
# Stop
sudo systemctl stop apex
sudo systemctl status apex   # should show: inactive (dead)

# Restart (e.g. after .env change)
sudo systemctl restart apex

# Disable autostart
sudo systemctl disable apex
```

---

## 16. Rollback procedure

If a deployment introduces a regression:

```bash
# 1. Stop the service
sudo systemctl stop apex

# 2. Check what commit is running
sudo -u apex bash -c "cd /opt/apex && git log --oneline -5"

# 3. Back up the database before making any schema or data changes
sudo -u apex cp /opt/apex/data/apex.db /opt/apex/data/apex.db.bak-$(date +%Y%m%d%H%M%S)

# 4. Revert to a known-good commit
sudo -u apex bash -c "cd /opt/apex && git fetch origin && git checkout <commit-hash>"

# 5. Reinstall in case dependencies changed
sudo -u apex /opt/apex/.venv/bin/pip install -r /opt/apex/requirements.txt
sudo -u apex /opt/apex/.venv/bin/pip install -e /opt/apex

# 6. Run tests to confirm the reverted state is clean
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python -m pytest tests/ -v"

# 7. Restart
sudo systemctl start apex
sudo journalctl -u apex -n 30
```

---

## 17. Enabling live alerts (future — not now)

When you are ready to receive real strategy alerts:

1. Confirm `APEX_PUBLIC_BASE_URL` is set and action links work (step 14)
2. Confirm `ACTION_TOKEN_SECRET` is a real secret, not the placeholder
3. Run the HTTP action-route smoke test one more time to confirm links work
4. In `.env`, change:
   ```ini
   ALERTS_ENABLED=true
   DRY_RUN_MODE=false
   ```
5. Restart: `sudo systemctl restart apex`
6. Verify the startup log no longer shows `*** ALERTS_ENABLED=false ***`
7. Watch scan logs — `[DRY RUN]` prefix should be absent from any candidate signals

**Do not enable live alerts while any of these are true:**
- `APEX_PUBLIC_BASE_URL` is empty or points to `localhost`
- `ACTION_TOKEN_SECRET` is still `change-me-to-a-random-secret`
- The HTTPS certificate is not valid
- You have not confirmed Pushover delivery with `smoke_test_pushover.py`
