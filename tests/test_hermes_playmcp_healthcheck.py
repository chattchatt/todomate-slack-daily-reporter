from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "hermes_playmcp_healthcheck.py"
SPEC = importlib.util.spec_from_file_location("hermes_playmcp_healthcheck", SCRIPT)
assert SPEC and SPEC.loader
healthcheck = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = healthcheck
SPEC.loader.exec_module(healthcheck)


def jwt_with_exp(exp: datetime) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def credentials(access_exp: datetime, refresh: bool = True) -> dict:
    tokens = {"access_token": jwt_with_exp(access_exp)}
    if refresh:
        tokens["refresh_token"] = "refresh-redacted"
    return {
        "entries": {
            "mcp-gateway|test": {
                "serverName": "mcp-gateway",
                "serverUrl": "https://playmcp.kakao.com/mcp",
                "tokens": tokens,
            }
        }
    }


def test_expired_access_token_is_not_renewal_required_when_refresh_exists() -> None:
    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    report = healthcheck.classify_credentials(
        credentials(now - timedelta(minutes=10)),
        now + timedelta(days=30),
        access_warn_seconds=3600,
        refresh_warn_days=7,
        now=now,
    )
    assert report["status"] == "warn"
    assert report["renewal_required"] is False
    assert "access token is expired" in "\n".join(report["evidence"])


def test_refresh_token_near_expiry_requires_renewal() -> None:
    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    report = healthcheck.classify_credentials(
        credentials(now + timedelta(hours=2)),
        now + timedelta(days=2),
        access_warn_seconds=3600,
        refresh_warn_days=7,
        now=now,
    )
    assert report["status"] == "warn"
    assert report["renewal_required"] is True


def test_auth_failure_markers_require_renewal() -> None:
    assert healthcheck.auth_failure_requires_renewal("OAuth authorization required. Waiting for browser approval")
    assert healthcheck.auth_failure_requires_renewal("OAuth timeout after 60s")
    assert not healthcheck.auth_failure_requires_renewal("temporary network timeout")
