# Protected Live Opportunity Board — Access Architecture (v0.1, phone-only)

This document describes the architecture for exposing the existing, unmodified
[Live Opportunity Board](../src/apex/opportunity/board.py) — a local,
read-only research view (see `docs/APEX_VPS_RUNBOOK.md` and `CLAUDE.md` for
APEX's overall research-only posture) — to Aaron's phone only, over Tailscale,
without ever making it reachable from the public internet.

**This document describes a design. It does not itself grant approval for any
of the deployment steps below, and no step here has been performed as part of
the milestone that added this file** (see "What this milestone did and did
not do").

## Why a separate process

The Protected Live Opportunity Board
([`src/apex/opportunity/protected_board.py`](../src/apex/opportunity/protected_board.py))
is a second, independent FastAPI process — not a route mounted on the main
`apex.service` application. It:

- opens its own strictly read-only SQLite connection (`PRAGMA query_only=ON`,
  verified) to the same database file the main process writes to, and never
  calls `apex.db.connection.init_db` or any other writer;
- never imports the main app, its settings, or any of its trading/execution/
  notification/scheduler code;
- listens only on a permission-restricted Unix domain socket, never a TCP
  port;
- disables interactive docs/OpenAPI entirely;
- requires an exact identity match on every single request, including its own
  health check.

A compromise, bug, or misconfiguration in this process therefore cannot write
to the database, place a trade, send a notification, or reach any exchange —
it can, at worst, serve stale or unavailable read-only board data.

## The trust boundary: Unix socket ownership, not the header alone

`protected_board.py`'s ASGI middleware reads exactly one request header,
`Tailscale-User-Login`, and compares it (constant-time) to one configured
allowed identity. **This header check is not, by itself, an authentication
boundary.** Any local process that can connect to this service's listening
socket could set that header to anything it likes.

The actual boundary is that this process's Unix domain socket must be
reachable **only** by a root-owned `tailscale serve` process, which is the
only thing permitted to connect to it and which is what actually authenticates
the remote Tailscale peer and injects the verified `Tailscale-User-Login`
identity. This depends on:

- the socket's parent directory and file permissions restricting connection
  to `tailscale serve` (running as root) and this service's own dedicated
  user;
- `tailscale serve` being configured (on the real host, not by this
  milestone) to forward only to that socket, over the private tailnet only —
  **never** `tailscale funnel`, which would expose the service to the public
  internet;
- no other process, port, or reverse proxy (e.g. the existing
  [`Caddyfile.example`](../deploy/Caddyfile.example), which fronts the public
  main app) ever being placed in front of this socket.

If any part of that chain is missing or misconfigured, the header check alone
provides no real protection. Whoever provisions the real host must verify the
socket's ownership/permissions and the `tailscale serve` mapping before this
service is ever exposed to a device.

## Phone-only v0.1 scope

- **Tailscale**, private tailnet only, using `tailscale serve` (not
  `tailscale funnel`) mapped to the restricted Unix socket described above.
- Requires **Tailscale >= 1.98.9** on the host (the version that reliably
  supports serving to a Unix socket with identity headers); the real host's
  installed version must be confirmed before deployment, not assumed.
- Exactly one **owner identity** is ever allowed
  (`APEX_BOARD_ALLOWED_IDENTITY`) — this is a single-operator board, not a
  multi-user service.
- Only an explicitly **approved device** enrolled in Aaron's tailnet can
  reach the mapped serve endpoint at all; Tailscale's own device
  approval/revocation is the mechanism for adding or removing a device's
  access, not anything in this codebase.
- An **independent kill switch** exists at every layer: disabling the
  systemd unit, removing the `tailscale serve` mapping, or revoking the
  device in the Tailscale admin console each independently removes access,
  without depending on any of the others.
- **Apple Watch access is explicitly deferred** — not designed, not
  implemented, not planned for this phase.
- **Viewing the board can never approve or place a trade.** The board is,
  and remains, the same read-only view described in `board.py`'s own
  docstring: no route it exposes writes to any table, sizes a position, or
  calls alerting/execution code.

## Separate OS user and no shared secrets

The board process runs as its own OS user (`apex-board`, never `apex`) with
its own environment file (`/etc/apex/apex-board.env`, never `/opt/apex/.env`),
as shown in
[`deploy/apex-board.service.example`](../deploy/apex-board.service.example).
It has no access to Pushover credentials, action-link tokens, exchange keys,
or any secret the main application uses, because it never reads the main
`.env` and defines no credential fields of its own (see
`BoardSettings` in
[`src/apex/opportunity/board_settings.py`](../src/apex/opportunity/board_settings.py)).

## What this milestone did and did not do

This milestone implemented and locally validated (via authored, not-yet-run,
tests) exactly five files: `protected_board.py`, `board_settings.py`, the
associated test file, the systemd unit example, and this document. It did
**not**:

- deploy anything to a VPS or any other host;
- create, enable, or start the example systemd unit;
- configure Tailscale, `tailscale serve`, or `tailscale funnel` anywhere;
- create a public or private route, or activate any feature flag in the main
  application;
- touch a real/production database, or grant any filesystem permission on a
  real host;
- send a notification, place an order, or reach any exchange;
- commit, push, merge, or open a pull request.

## Required future stages (not performed here)

Each of these is a distinct, separately-approved stage — none of them are
implied or partially completed by this milestone:

1. **Disabled deployment** — install the real systemd unit and dedicated
   `apex-board` user on the actual host, with `APEX_BOARD_ENABLED` unset/false,
   so the service exists but serves nothing.
2. **Current-host UDS permission and Tailscale version proof** — on the
   actual host, confirm the installed Tailscale version and prove the Unix
   socket's ownership/permissions actually restrict connections to
   `tailscale serve` and the `apex-board` user, before any identity check is
   relied upon. This stage must also explicitly verify, on that real host,
   that `tailscale serve` strips or overwrites any caller-supplied
   `Tailscale-User-Login` value before injecting its own verified identity,
   and that a request with a duplicate `Tailscale-User-Login` header or
   ambiguous header ordering cannot reach this application with an
   attacker-controlled value intact. Nothing in this milestone proves that
   stripping/overwrite/duplicate-header behavior — it is confirmed only on
   the real host, as part of this later stage, never assumed.
3. **Actual-host live-WAL read-only proof (or snapshot fallback)** — prove,
   against the real running database file (in WAL mode, being actively
   written by the main process), that this service's read-only connection
   can see committed data without ever acquiring a write lock; if that
   cannot be proven safe on the real host, fall back to a periodic read-only
   snapshot instead of a live WAL read. (This milestone's own tests prove
   only an isolated, temporary-file WAL writer/read-only-reader scenario —
   explicitly not a substitute for this stage.)
4. **Private-access configuration** — the actual `tailscale serve` mapping
   and `APEX_BOARD_ALLOWED_IDENTITY` value on the real host.
5. **Bounded phone pilot** — a time-boxed, explicitly approved trial with
   Aaron's phone as the only enrolled device.
6. **Sustained access approval** — Aaron's explicit sign-off to keep the
   service running beyond the pilot.

No claim in this document, or in the code it describes, should be read as
asserting that stages 2–6 have already happened.
