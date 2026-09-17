"""Tests for the Protected Live Opportunity Board
(src/apex/opportunity/protected_board.py, src/apex/opportunity/board_settings.py).

Covers: standalone board-only settings (env prefix, no-dotenv, fail-closed
validation); exact route inventory with docs/OpenAPI disabled; the identity
gate (allowed/missing/blank/malformed/mismatched, checked before any
repository query); GET-only enforcement with zero DB mutation and no
method-override escape hatch; security headers on every response kind;
sanitized structured access logging; the bounded in-memory rate limiter; the
read-only SQLite helper (query_only enforcement, no file creation, special
filenames, an isolated WAL visibility check); connection lifecycle
(owned vs. injected); the disabled kill switch; the UDS-only Uvicorn runner
config; that the main production app still refuses board routes; and static
import-isolation / systemd-unit / runbook invariants.

None of these tests run a real server, bind a real socket, touch a
production database, or perform any git/service action — see the module
docstring in protected_board.py for the trust-boundary this suite validates
only at the unit/isolated-process level, never against an actual host.
"""
from __future__ import annotations

import ast
import logging
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apex.db import repository as repo
from apex.opportunity import board_settings as board_settings_module
from apex.opportunity import protected_board as pb
from apex.opportunity.board_settings import BoardSettings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_IDENTITY = "owner@example.com"


def _headers(identity: str = _ALLOWED_IDENTITY) -> dict:
    return {"Tailscale-User-Login": identity}


def _build_app(tmp_path: Path, *, enabled: bool = True, conn=None):
    settings = BoardSettings(
        enabled=enabled,
        db_path=tmp_path / "unused.db",
        socket_path=tmp_path / "unused.sock",
        allowed_identity=_ALLOWED_IDENTITY,
    )
    if conn is None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
    app = pb.create_protected_board_app(settings, conn=conn)
    return app, conn


# ------------------------------------------------------------------ settings

def test_settings_env_prefix_and_no_dotenv():
    assert BoardSettings.model_config.get("env_prefix") == "APEX_BOARD_"
    assert BoardSettings.model_config.get("env_file") is None


def test_settings_default_disabled(tmp_path):
    settings = BoardSettings(
        db_path=tmp_path / "d.db", socket_path=tmp_path / "s.sock",
        allowed_identity=_ALLOWED_IDENTITY,
    )
    assert settings.enabled is False
    assert settings.log_level == "INFO"


def test_settings_reads_only_prefixed_env_vars(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_BOARD_ENABLED", "true")
    monkeypatch.setenv("APEX_BOARD_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("APEX_BOARD_SOCKET_PATH", str(tmp_path / "env.sock"))
    monkeypatch.setenv("APEX_BOARD_ALLOWED_IDENTITY", _ALLOWED_IDENTITY)
    monkeypatch.setenv("APEX_BOARD_LOG_LEVEL", "warning")
    settings = BoardSettings()
    assert settings.enabled is True
    assert settings.db_path == tmp_path / "env.db"
    assert settings.allowed_identity == _ALLOWED_IDENTITY
    assert settings.log_level == "WARNING"


def test_settings_rejects_relative_paths(tmp_path):
    with pytest.raises(ValidationError):
        BoardSettings(
            db_path=Path("relative.db"), socket_path=tmp_path / "s.sock",
            allowed_identity=_ALLOWED_IDENTITY,
        )
    with pytest.raises(ValidationError):
        BoardSettings(
            db_path=tmp_path / "d.db", socket_path=Path("relative.sock"),
            allowed_identity=_ALLOWED_IDENTITY,
        )


def test_settings_rejects_blank_identity(tmp_path):
    with pytest.raises(ValidationError):
        BoardSettings(
            db_path=tmp_path / "d.db", socket_path=tmp_path / "s.sock",
            allowed_identity="   ",
        )


def test_settings_rejects_invalid_log_level(tmp_path):
    with pytest.raises(ValidationError):
        BoardSettings(
            db_path=tmp_path / "d.db", socket_path=tmp_path / "s.sock",
            allowed_identity=_ALLOWED_IDENTITY, log_level="TRACE",
        )


# ------------------------------------------------------------------ route inventory

def test_exact_route_inventory(tmp_path):
    app, _ = _build_app(tmp_path)
    paths = sorted(route.path for route in app.routes)
    assert paths == ["/healthz", "/opportunities", "/opportunities/api"]

    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json", "/status", "/action", "/nope"):
        resp = client.get(path, headers=_headers())
        assert resp.status_code == 404


# ------------------------------------------------------------------ identity gate

def test_is_valid_identity_unit_cases():
    assert pb._is_valid_identity(None, "owner") is False
    assert pb._is_valid_identity("", "owner") is False
    assert pb._is_valid_identity("owner\x01", "owner\x01") is False  # control char always rejected
    assert pb._is_valid_identity("owner", "owner") is True
    assert pb._is_valid_identity("someone-else", "owner") is False
    assert pb._is_valid_identity("x" * 1000, "x" * 1000) is False  # overlong rejected


def test_identity_gate_rejects_missing_blank_and_mismatch_before_query(tmp_path, monkeypatch):
    calls = []

    def _tracking(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(repo, "get_board_opportunities", _tracking)
    app, _ = _build_app(tmp_path)
    client = TestClient(app)

    bad_headers = ({}, {"Tailscale-User-Login": ""}, {"Tailscale-User-Login": "wrong@example.com"})
    for headers in bad_headers:
        resp = client.get("/opportunities/api", headers=headers)
        assert resp.status_code == 403
    assert calls == []

    resp = client.get("/opportunities/api", headers=_headers())
    assert resp.status_code == 200
    assert calls == [1]


def test_allowed_identity_succeeds_on_healthz(tmp_path):
    app, _ = _build_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/healthz", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ------------------------------------------------------------------ GET-only / no mutation

def test_write_methods_denied_with_no_db_mutation(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE opportunity_observations (id INTEGER)")
    conn.commit()
    app, _ = _build_app(tmp_path, conn=conn)
    client = TestClient(app)
    before = conn.total_changes
    for method_name in ("post", "put", "patch", "delete"):
        resp = getattr(client, method_name)("/opportunities", headers=_headers())
        assert resp.status_code == 405
    assert conn.total_changes == before


def test_method_override_header_has_no_effect(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)
    client = TestClient(app)

    # A GET request cannot be escalated to a mutating method via override.
    escalation_headers = {**_headers(), "X-HTTP-Method-Override": "DELETE"}
    resp = client.get("/healthz", headers=escalation_headers)
    assert resp.status_code == 200

    # A mutating POST cannot be downgraded to GET via override to bypass
    # method enforcement and reach the repository query.
    calls = []

    def _tracking(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(repo, "get_board_opportunities", _tracking)
    downgrade_headers = {**_headers(), "X-HTTP-Method-Override": "GET"}
    resp2 = client.post("/opportunities/api", headers=downgrade_headers)
    assert resp2.status_code == 405
    assert calls == []


# ------------------------------------------------------------------ security headers

def test_security_headers_present_across_status_kinds(tmp_path):
    app, _ = _build_app(tmp_path)
    client = TestClient(app)

    cases = [
        (client.get("/healthz", headers=_headers()), 200),
        (client.get("/healthz", headers={}), 403),
        (client.get("/does-not-exist", headers=_headers()), 404),
        (client.post("/healthz", headers=_headers()), 405),
        (
            client.get("/opportunities/api", params={"limit": 99999}, headers=_headers()),
            422,
        ),
    ]
    for resp, expected_status in cases:
        assert resp.status_code == expected_status
        for header, value in pb._SECURITY_HEADERS.items():
            assert resp.headers.get(header) == value


def test_security_headers_present_on_generic_500(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(repo, "get_board_opportunities", _raise)
    client = TestClient(app)
    resp = client.get("/opportunities/api", headers=_headers())
    assert resp.status_code == 500
    for header, value in pb._SECURITY_HEADERS.items():
        assert resp.headers.get(header) == value


def test_security_headers_present_on_rate_limited_429(tmp_path):
    app, _ = _build_app(tmp_path)
    client = TestClient(app)
    for _ in range(pb.RATE_LIMIT_CAPACITY):
        resp = client.get("/healthz", headers=_headers())
        assert resp.status_code == 200
    resp = client.get("/healthz", headers=_headers())
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate limited"}
    for header, value in pb._SECURITY_HEADERS.items():
        assert resp.headers.get(header) == value


def test_unexpected_middleware_exception_returns_generic_500(tmp_path, monkeypatch):
    app, _ = _build_app(tmp_path)

    def _raise(*args, **kwargs):
        raise RuntimeError("SENTINEL_MIDDLEWARE_FAILURE")

    monkeypatch.setattr(pb, "_is_valid_identity", _raise)
    client = TestClient(app)
    resp = client.get("/healthz", headers=_headers())
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal error"}
    assert "SENTINEL_MIDDLEWARE_FAILURE" not in resp.text
    for header, value in pb._SECURITY_HEADERS.items():
        assert resp.headers.get(header) == value


# ------------------------------------------------------------------ sanitized audit log

def test_sanitized_audit_log_excludes_sensitive_values(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="apex.opportunity.protected_board")
    app, _ = _build_app(tmp_path)
    client = TestClient(app)

    secret_identity = "someone-with-TOKEN_SENTINEL@example.com"
    resp1 = client.get(
        "/opportunities/api",
        params={"symbol": "BTC", "secret": "QUERY_SENTINEL"},
        headers=_headers(secret_identity),
    )
    assert resp1.status_code == 403

    def _raise(*args, **kwargs):
        raise RuntimeError("SELECT * FROM secret WHERE token='RAW_EXCEPTION_SENTINEL'")

    monkeypatch.setattr(repo, "get_board_opportunities", _raise)
    resp2 = client.get("/opportunities/api", headers=_headers())
    assert resp2.status_code == 500

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for leak in (
        "TOKEN_SENTINEL", "QUERY_SENTINEL", "RAW_EXCEPTION_SENTINEL",
        secret_identity, _ALLOWED_IDENTITY, "secret=", "SELECT ", "RuntimeError",
    ):
        assert leak not in log_text
    assert "principal=owner" in log_text
    assert "route_class=opportunities_api" in log_text


# ------------------------------------------------------------------ rate limiter

def test_rate_limiter_deterministic_and_bounded_window():
    now = [1000.0]
    limiter = pb._FixedWindowRateLimiter(capacity=3, window_seconds=10, clock=lambda: now[0])
    assert [limiter.allow() for _ in range(3)] == [True, True, True]
    assert limiter.allow() is False
    now[0] += 10.0
    assert limiter.allow() is True


def test_rate_limiter_state_never_grows():
    limiter = pb._FixedWindowRateLimiter(capacity=1000, window_seconds=1000, clock=lambda: 0.0)
    before = len(vars(limiter))
    for _ in range(500):
        limiter.allow()
    assert len(vars(limiter)) == before


# ------------------------------------------------------------------ read-only connection

def test_open_read_only_connection_enforces_query_only(tmp_path):
    db_path = tmp_path / "ro.db"
    setup_conn = sqlite3.connect(str(db_path))
    setup_conn.execute("CREATE TABLE t (id INTEGER)")
    setup_conn.commit()
    setup_conn.close()

    conn = pb.open_read_only_connection(db_path)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE u (id INTEGER)")
    finally:
        conn.close()


def test_open_read_only_connection_never_creates_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.db"
    with pytest.raises(FileNotFoundError):
        pb.open_read_only_connection(missing)
    assert not missing.exists()


def test_open_read_only_connection_handles_special_filenames(tmp_path):
    for name in ("has space.db", "has?question.db", "has#hash.db"):
        db_path = tmp_path / name
        setup_conn = sqlite3.connect(str(db_path))
        setup_conn.execute("CREATE TABLE t (id INTEGER)")
        setup_conn.execute("INSERT INTO t VALUES (1)")
        setup_conn.commit()
        setup_conn.close()

        conn = pb.open_read_only_connection(db_path)
        try:
            assert conn.execute("SELECT id FROM t").fetchone()[0] == 1
        finally:
            conn.close()


def test_isolated_wal_writer_visible_to_read_only_reader_not_actual_host_proof(tmp_path):
    """Proves visibility only for an isolated, temporary-file WAL writer and
    this module's read-only reader within this test process. This is
    explicitly NOT a substitute for the actual-host live-WAL read-only proof
    described as a required future stage in
    docs/LIVE_OPPORTUNITY_BOARD_ACCESS.md.
    """
    db_path = tmp_path / "wal.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (id INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()

    reader = pb.open_read_only_connection(db_path)
    try:
        rows = reader.execute("SELECT id FROM t").fetchall()
        assert [row[0] for row in rows] == [1]
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        rows = reader.execute("SELECT id FROM t ORDER BY id").fetchall()
        assert [row[0] for row in rows] == [1, 2]
    finally:
        reader.close()
        writer.close()


# ------------------------------------------------------------------ connection lifecycle

def test_lifecycle_closes_owned_connection(tmp_path, monkeypatch):
    db_path = tmp_path / "owned.db"
    setup_conn = sqlite3.connect(str(db_path))
    setup_conn.execute("CREATE TABLE t (id INTEGER)")
    setup_conn.commit()
    setup_conn.close()

    created = {}
    original = pb.open_read_only_connection

    def _spy(path, **kwargs):
        conn = original(path, **kwargs)
        created["conn"] = conn
        return conn

    monkeypatch.setattr(pb, "open_read_only_connection", _spy)

    settings = BoardSettings(
        enabled=True, db_path=db_path, socket_path=tmp_path / "x.sock",
        allowed_identity=_ALLOWED_IDENTITY,
    )
    app = pb.create_protected_board_app(settings)
    with TestClient(app) as client:
        resp = client.get("/healthz", headers=_headers())
        assert resp.status_code == 200

    with pytest.raises(sqlite3.ProgrammingError):
        created["conn"].execute("SELECT 1")


def test_lifecycle_preserves_injected_connection(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    settings = BoardSettings(
        enabled=True, db_path=tmp_path / "unused.db", socket_path=tmp_path / "x.sock",
        allowed_identity=_ALLOWED_IDENTITY,
    )
    app = pb.create_protected_board_app(settings, conn=conn)
    with TestClient(app) as client:
        client.get("/healthz", headers=_headers())

    conn.execute("SELECT 1")  # must not raise: injected connection stays open


# ------------------------------------------------------------------ disabled kill switch

def test_factory_disabled_kill_switch(tmp_path):
    settings = BoardSettings(
        enabled=False, db_path=tmp_path / "x.db", socket_path=tmp_path / "x.sock",
        allowed_identity=_ALLOWED_IDENTITY,
    )
    with pytest.raises(RuntimeError):
        pb.create_protected_board_app(settings)


# ------------------------------------------------------------------ UDS runner config

def test_build_uvicorn_config_uses_uds_only_and_one_worker(tmp_path, monkeypatch):
    captured = {}

    class _FakeConfig:
        def __init__(self, app, **kwargs):
            captured["app"] = app
            captured.update(kwargs)

    monkeypatch.setattr(pb.uvicorn, "Config", _FakeConfig)

    socket_dir = tmp_path / "sockdir"
    socket_dir.mkdir()
    socket_path = socket_dir / "board.sock"
    settings = BoardSettings(
        enabled=True, db_path=tmp_path / "db.sqlite", socket_path=socket_path,
        allowed_identity=_ALLOWED_IDENTITY,
    )
    fake_app = object()
    pb.build_uvicorn_config(fake_app, settings)

    assert captured["app"] is fake_app
    assert captured["uds"] == str(socket_path)
    assert captured["workers"] == 1
    assert captured["access_log"] is False
    assert "host" not in captured
    assert "port" not in captured


def test_build_uvicorn_config_rejects_missing_parent_dir(tmp_path):
    socket_path = tmp_path / "missing_dir" / "board.sock"
    settings = BoardSettings(
        enabled=True, db_path=tmp_path / "db.sqlite", socket_path=socket_path,
        allowed_identity=_ALLOWED_IDENTITY,
    )
    with pytest.raises(RuntimeError):
        pb.build_uvicorn_config(FastAPI(), settings)
    assert not socket_path.parent.exists()


def test_build_uvicorn_config_rejects_existing_socket_path_without_touching_it(tmp_path):
    socket_path = tmp_path / "board.sock"
    socket_path.touch()
    mtime_before = socket_path.stat().st_mtime

    settings = BoardSettings(
        enabled=True, db_path=tmp_path / "db.sqlite", socket_path=socket_path,
        allowed_identity=_ALLOWED_IDENTITY,
    )
    with pytest.raises(RuntimeError):
        pb.build_uvicorn_config(FastAPI(), settings)
    assert socket_path.exists()
    assert socket_path.stat().st_mtime == mtime_before


# ------------------------------------------------------------------ main app denies board routes

def test_main_app_denies_board_routes_in_production_even_with_flag(tmp_path):
    from apex.app import create_app
    from apex.config import Settings as MainSettings
    from apex.db.connection import close_db, init_db

    conn = init_db(str(tmp_path / "main.db"))
    try:
        settings = MainSettings(live_opportunity_board_enabled=True, apex_env="production")
        app = create_app(settings=settings, conn=conn)
        client = TestClient(app)
        assert client.get("/opportunities").status_code == 404
        assert client.get("/opportunities/api").status_code == 404
    finally:
        close_db()


# ------------------------------------------------------------------ static import isolation

_FORBIDDEN_IMPORT_PREFIXES = (
    "apex.app", "apex.main", "apex.config", "apex.db.connection",
    "apex.actions", "apex.scheduler", "apex.notifications",
)


def _imported_module_names(file_path: str) -> set[str]:
    tree = ast.parse(Path(file_path).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_static_import_isolation():
    for module in (pb, board_settings_module):
        imported = _imported_module_names(module.__file__)
        for name in imported:
            for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
                assert not (name == forbidden or name.startswith(forbidden + ".")), (
                    f"{module.__name__} imports forbidden module {name!r}"
                )


def test_protected_board_reuses_unmodified_board_router():
    imported = _imported_module_names(pb.__file__)
    assert "apex.opportunity.board" in imported


# ------------------------------------------------------------------ systemd unit static invariants

def _active_unit_lines(text: str) -> list[str]:
    """Non-blank, non-comment unit lines, so directive checks below cannot
    be satisfied (or defeated) by prose in a comment."""
    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def test_systemd_unit_static_invariants():
    text = (_REPO_ROOT / "deploy" / "apex-board.service.example").read_text()
    active_lines = _active_unit_lines(text)

    assert "User=apex-board" in active_lines
    assert "Group=apex-board" in active_lines
    assert "User=apex" not in active_lines
    assert "EnvironmentFile=/etc/apex/apex-board.env" in active_lines
    assert "InaccessiblePaths=/opt/apex/.env" in active_lines
    assert "RuntimeDirectory=apex-board" in active_lines
    assert "UMask=0007" in active_lines
    assert "/run/apex-board/apex-board.sock" in text
    assert any(
        line.endswith("python -m apex.opportunity.protected_board")
        for line in active_lines
    )
    assert "ProtectSystem=strict" in active_lines
    assert "ProtectHome=true" in active_lines
    assert "NoNewPrivileges=true" in active_lines
    assert "PrivateTmp=true" in active_lines
    assert "PrivateDevices=true" in active_lines
    assert "CapabilityBoundingSet=" in active_lines
    assert "AmbientCapabilities=" in active_lines
    assert not any("ReadWritePaths=/opt/apex/data" in line for line in active_lines)
    assert not any("systemctl enable" in line for line in active_lines)
    assert not any("systemctl start" in line for line in active_lines)
    assert not any("--host" in line for line in active_lines)
    assert not any("--port" in line for line in active_lines)


# ------------------------------------------------------------------ runbook static invariants

def test_runbook_static_invariants():
    text = (_REPO_ROOT / "docs" / "LIVE_OPPORTUNITY_BOARD_ACCESS.md").read_text()
    lower = text.lower()

    for phrase in (
        "phone-only", "tailscale serve", "tailscale funnel", "1.98.9",
        "apex-board", "unix domain socket", "independent kill switch",
        "apple watch", "never approve or place a trade", "approved device",
        "revok", "strips or overwrites", "duplicate", "ambiguous header order",
    ):
        assert phrase in lower, f"missing required phrase: {phrase!r}"

    for forbidden in ("ssh ", "asilorey"):
        assert forbidden not in lower
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)


def test_protected_board_module_documents_trust_boundary():
    text = (_REPO_ROOT / "src" / "apex" / "opportunity" / "protected_board.py").read_text()
    assert "Tailscale-User-Login" in text
    assert "NOT an origin/authentication boundary" in text
