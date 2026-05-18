# APEX VPS Setup Guide

Target: Hetzner VPS running Ubuntu 22.04+

## 1. Install Python 3.11+

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev build-essential git
```

## 2. Create dedicated user

```bash
sudo useradd -m -s /bin/bash apex
sudo mkdir -p /opt/apex
sudo chown apex:apex /opt/apex
```

## 3. Clone the repo

```bash
sudo -u apex git clone https://github.com/AraldoYerolis/APEX.git /opt/apex
cd /opt/apex
```

## 4. Create virtual environment and install dependencies

```bash
sudo -u apex python3.11 -m venv /opt/apex/.venv
sudo -u apex /opt/apex/.venv/bin/pip install --upgrade pip
sudo -u apex /opt/apex/.venv/bin/pip install -r /opt/apex/requirements.txt
sudo -u apex /opt/apex/.venv/bin/pip install -e /opt/apex
```

## 5. Configure environment

```bash
sudo -u apex cp /opt/apex/.env.example /opt/apex/.env
sudo -u apex nano /opt/apex/.env
```

Fill in at minimum:
- `PUSHOVER_APP_TOKEN`
- `PUSHOVER_USER_KEY`
- `APEX_PUBLIC_BASE_URL` (your domain, e.g. `https://apex.example.com`)
- `ACTION_TOKEN_SECRET` (generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"`)

## 6. Initialize database

```bash
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/init_db.py"
```

## 7. Run smoke tests

```bash
# Test Hyperliquid connectivity
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_hyperliquid.py"

# Test Pushover (requires credentials in .env)
sudo -u apex bash -c "cd /opt/apex && PYTHONPATH=src .venv/bin/python scripts/smoke_test_pushover.py"
```

## 8. Install systemd service

```bash
sudo cp /opt/apex/deploy/apex.service.example /etc/systemd/system/apex.service

# Edit if needed (e.g. adjust WorkingDirectory or paths)
sudo nano /etc/systemd/system/apex.service

sudo systemctl daemon-reload
sudo systemctl enable apex
sudo systemctl start apex
```

## 9. Check logs

```bash
sudo journalctl -u apex -f
# Or check the rotating log file:
tail -f /opt/apex/data/logs/apex.log
```

## 10. Optional: Caddy reverse proxy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

# Copy the example Caddyfile
sudo cp /opt/apex/deploy/Caddyfile.example /etc/caddy/Caddyfile
# Edit to set your actual domain
sudo nano /etc/caddy/Caddyfile

sudo systemctl enable caddy
sudo systemctl start caddy
```

Caddy will automatically obtain a TLS certificate via Let's Encrypt.

## 11. Update APEX_PUBLIC_BASE_URL

Once Caddy and DNS are set up, update your `.env`:
```
APEX_PUBLIC_BASE_URL=https://apex.example.com
```

Then restart: `sudo systemctl restart apex`
