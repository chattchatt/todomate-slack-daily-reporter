#!/usr/bin/env python3
"""Hermes-style health gate for TodoMate PlayMCP credentials.

This check is intentionally non-blocking for the daily scheduler: it emits a
small Hermes health artifact and, only when human token renewal is likely
required, sends a deduplicated Slack alert. Expiring access tokens are not a
problem by themselves as long as mcporter can refresh/probe the MCP gateway.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_CREDENTIALS_PATH = Path.home() / ".mcporter" / "credentials.json"
DEFAULT_STATE_DIR = Path(os.environ.get("TODOMATE_STATE_DIR", "/tmp/todomate-slack-daily-reporter/state"))
DEFAULT_LOG_DIR = Path(os.environ.get("TODOMATE_LOG_DIR", "/tmp/todomate-slack-daily-reporter/logs"))
DEFAULT_TIMEZONE = os.environ.get("TODOMATE_TIMEZONE", "Asia/Seoul")
AUTH_REQUIRED_MARKERS = (
    "oauth authorization required",
    "waiting for browser approval",
    "oauth timeout",
    "authorization required",
    "browser approval",
    "unauthorized",
    "invalid_grant",
    "refresh",
)


@dataclass(frozen=True)
class HealthConfig:
    credentials_path: Path
    state_dir: Path
    log_dir: Path
    timezone_name: str
    mcporter_candidates: tuple[str, ...]
    agent_slack_candidates: tuple[str, ...]
    slack_channel_id: str
    refresh_expires_at: datetime | None
    refresh_warn_days: int
    access_warn_seconds: int
    probe_timeout_seconds: int
    notify_slack: bool
    force_alert: bool


def split_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    parsed = tuple(part.strip() for part in value.split(",") if part.strip())
    return parsed or default


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_config(args: argparse.Namespace) -> HealthConfig:
    return HealthConfig(
        credentials_path=Path(args.credentials_path or os.environ.get("MCPORTER_CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH)).expanduser(),
        state_dir=Path(args.state_dir or DEFAULT_STATE_DIR).expanduser(),
        log_dir=Path(args.log_dir or DEFAULT_LOG_DIR).expanduser(),
        timezone_name=os.environ.get("TODOMATE_TIMEZONE", DEFAULT_TIMEZONE),
        mcporter_candidates=split_csv(
            os.environ.get("TODOMATE_MCPORTER_PATHS"),
            ("mcporter", "/app/node_modules/.bin/mcporter"),
        ),
        agent_slack_candidates=split_csv(
            os.environ.get("TODOMATE_AGENT_SLACK_PATHS"),
            ("agent-slack", "/app/node_modules/.bin/agent-slack"),
        ),
        slack_channel_id=os.environ.get("TODOMATE_SLACK_CHANNEL_ID", "").strip(),
        refresh_expires_at=parse_datetime(os.environ.get("PLAYMCP_REFRESH_EXPIRES_AT")),
        refresh_warn_days=int(os.environ.get("TODOMATE_HEALTHCHECK_REFRESH_WARN_DAYS", "7")),
        access_warn_seconds=int(os.environ.get("TODOMATE_HEALTHCHECK_ACCESS_WARN_SECONDS", "3600")),
        probe_timeout_seconds=int(os.environ.get("TODOMATE_HEALTHCHECK_PROBE_TIMEOUT_SECONDS", "45")),
        notify_slack=parse_bool(
            os.environ.get("TODOMATE_HEALTHCHECK_NOTIFY_SLACK"),
            parse_bool(os.environ.get("TODOMATE_FAILURE_ALERT_TO_SLACK"), True),
        ),
        force_alert=parse_bool(os.environ.get("TODOMATE_HEALTHCHECK_FORCE_ALERT"), False),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_credentials(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_mcp_gateway_entry(credentials: dict[str, Any]) -> dict[str, Any] | None:
    entries = credentials.get("entries")
    if not isinstance(entries, dict):
        return None
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        server_name = str(entry.get("serverName", ""))
        server_url = str(entry.get("serverUrl", ""))
        if server_name == "mcp-gateway" or "playmcp.kakao.com/mcp" in server_url or str(key).startswith("mcp-gateway|"):
            return entry
    return None


def decode_jwt_expiry(token: str | None) -> datetime | None:
    if not token or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, UTC)


def resolve_command(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if "/" in candidate:
            path = Path(candidate)
            if path.exists() and os.access(path, os.X_OK):
                return str(path)
        found = shutil.which(candidate)
        if found:
            return found
    return None


def run_mcporter_probe(mcporter: str | None, timeout_seconds: int) -> tuple[bool, str]:
    if not mcporter:
        return False, "mcporter command is not available"
    try:
        proc = subprocess.run(
            [mcporter, "list", "mcp-gateway"],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"mcporter list mcp-gateway timed out after {timeout_seconds}s"
    except Exception as exc:
        return False, str(exc)

    output = (proc.stderr.strip() or proc.stdout.strip()).strip()
    if proc.returncode == 0:
        return True, "mcporter list mcp-gateway succeeded"
    return False, output or f"mcporter exited {proc.returncode}"


def classify_credentials(
    credentials: dict[str, Any] | None,
    refresh_expires_at: datetime | None,
    access_warn_seconds: int,
    refresh_warn_days: int,
    now: datetime,
) -> dict[str, Any]:
    evidence: list[str] = []
    renewal_required = False
    status = "pass"
    entry = find_mcp_gateway_entry(credentials or {}) if credentials else None
    tokens = entry.get("tokens", {}) if isinstance(entry, dict) else {}
    access_expiry = decode_jwt_expiry(tokens.get("access_token") if isinstance(tokens, dict) else None)
    has_refresh = bool(tokens.get("refresh_token")) if isinstance(tokens, dict) else False

    if not entry:
        return {
            "status": "fail",
            "renewal_required": True,
            "access_expires_at": None,
            "refresh_expires_at": refresh_expires_at.isoformat() if refresh_expires_at else None,
            "evidence": ["mcp-gateway credential entry is missing"],
        }

    if not has_refresh:
        status = "fail"
        renewal_required = True
        evidence.append("refresh token is missing")

    if access_expiry:
        access_remaining = access_expiry - now
        evidence.append(f"access token expires at {access_expiry.isoformat()}")
        if access_remaining <= timedelta(seconds=0):
            status = "warn" if status == "pass" else status
            evidence.append("access token is expired; refresh probe decides if action is needed")
        elif access_remaining <= timedelta(seconds=access_warn_seconds):
            status = "warn" if status == "pass" else status
            evidence.append(f"access token expires within {access_warn_seconds}s")
    else:
        status = "warn" if status == "pass" else status
        evidence.append("access token expiry could not be decoded")

    if refresh_expires_at:
        evidence.append(f"refresh token expires at {refresh_expires_at.isoformat()}")
        refresh_remaining = refresh_expires_at - now
        if refresh_remaining <= timedelta(seconds=0):
            status = "fail"
            renewal_required = True
            evidence.append("refresh token is expired")
        elif refresh_remaining <= timedelta(days=refresh_warn_days):
            status = "warn" if status == "pass" else status
            renewal_required = True
            evidence.append(f"refresh token expires within {refresh_warn_days} days")
    else:
        status = "warn" if status == "pass" else status
        evidence.append("refresh token expiry metadata is missing; set PLAYMCP_REFRESH_EXPIRES_AT")

    return {
        "status": status,
        "renewal_required": renewal_required,
        "access_expires_at": access_expiry.isoformat() if access_expiry else None,
        "refresh_expires_at": refresh_expires_at.isoformat() if refresh_expires_at else None,
        "evidence": evidence,
    }


def auth_failure_requires_renewal(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in AUTH_REQUIRED_MARKERS)


def local_date(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    return datetime.now(tz).strftime("%Y-%m-%d")


def write_report(config: HealthConfig, report: dict[str, Any]) -> Path:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    health_dir = config.state_dir / "hermes-health"
    health_dir.mkdir(parents=True, exist_ok=True)
    latest = health_dir / "playmcp-mcp-gateway.latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (config.log_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "hermes_playmcp_healthcheck", **report}, ensure_ascii=False) + "\n")
    return latest


def alert_marker(config: HealthConfig, day: str) -> Path:
    health_dir = config.state_dir / "hermes-health"
    health_dir.mkdir(parents=True, exist_ok=True)
    return health_dir / f"playmcp-renewal-alert.{day}.json"


def send_slack_alert(config: HealthConfig, report: dict[str, Any]) -> tuple[bool, str]:
    if not config.notify_slack:
        return False, "Slack alert disabled"
    if not config.slack_channel_id:
        return False, "TODOMATE_SLACK_CHANNEL_ID is missing"
    agent_slack = resolve_command(config.agent_slack_candidates)
    if not agent_slack:
        return False, "agent-slack command is not available"

    day = local_date(config.timezone_name)
    marker = alert_marker(config, day)
    if marker.exists() and not config.force_alert:
        return False, f"alert already sent for {day}"

    evidence = "\n".join(f"- {item}" for item in report.get("evidence", [])[:6])
    message = (
        "⚠️ TodoMate Slack Daily Reporter: PlayMCP re-authentication may be required.\n"
        f"- Hermes health status: {report.get('status')}\n"
        f"- MCP probe: {report.get('probe_detail')}\n"
        f"- Access expires: {report.get('access_expires_at') or 'unknown'}\n"
        f"- Refresh expires: {report.get('refresh_expires_at') or 'unknown'}\n"
        f"{evidence}\n\n"
        "조치: PlayMCP one-time-token 재인증으로 mcp-gateway를 다시 연결한 뒤 Railway "
        "MCPORTER_CREDENTIALS_JSON_B64를 갱신하세요."
    )
    try:
        proc = subprocess.run(
            [agent_slack, "message", "send", config.slack_channel_id, message],
            text=True,
            capture_output=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return False, "agent-slack send timed out"
    if proc.returncode != 0:
        return False, (proc.stderr.strip() or proc.stdout.strip() or "agent-slack send failed")

    marker.write_text(json.dumps({"sent_at": utc_now().isoformat(), "report": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, "Slack renewal alert sent"


def build_report(config: HealthConfig) -> dict[str, Any]:
    now = utc_now()
    credentials: dict[str, Any] | None = None
    if config.credentials_path.exists():
        try:
            credentials = load_credentials(config.credentials_path)
        except Exception as exc:
            credentials_report = {
                "status": "fail",
                "renewal_required": True,
                "access_expires_at": None,
                "refresh_expires_at": config.refresh_expires_at.isoformat() if config.refresh_expires_at else None,
                "evidence": [f"credentials file is unreadable: {exc}"],
            }
        else:
            credentials_report = classify_credentials(
                credentials,
                config.refresh_expires_at,
                config.access_warn_seconds,
                config.refresh_warn_days,
                now,
            )
    else:
        credentials_report = {
            "status": "fail",
            "renewal_required": True,
            "access_expires_at": None,
            "refresh_expires_at": config.refresh_expires_at.isoformat() if config.refresh_expires_at else None,
            "evidence": [f"credentials file is missing: {config.credentials_path}"],
        }

    probe_ok, probe_detail = run_mcporter_probe(resolve_command(config.mcporter_candidates), config.probe_timeout_seconds)
    renewal_required = bool(credentials_report["renewal_required"])
    status = str(credentials_report["status"])
    evidence = list(credentials_report["evidence"])
    evidence.append(probe_detail)

    if probe_ok:
        if status == "fail" and not renewal_required:
            status = "warn"
    else:
        if auth_failure_requires_renewal(probe_detail):
            renewal_required = True
        status = "fail"

    return {
        "schema": "hermes.healthcheck.v1",
        "surface": "todomate-slack-daily-reporter",
        "subject": "playmcp.mcp-gateway",
        "checked_at": now.isoformat(),
        "status": status,
        "renewal_required": renewal_required,
        "probe_ok": probe_ok,
        "probe_detail": probe_detail[:500],
        "access_expires_at": credentials_report.get("access_expires_at"),
        "refresh_expires_at": credentials_report.get("refresh_expires_at"),
        "evidence": evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes PlayMCP health gate")
    parser.add_argument("--credentials-path")
    parser.add_argument("--state-dir")
    parser.add_argument("--log-dir")
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    args = parser.parse_args(argv)

    config = load_config(args)
    report = build_report(config)
    report_path = write_report(config, report)
    alert_sent = False
    alert_detail = "not needed"
    if report["renewal_required"] or report["status"] == "fail":
        alert_sent, alert_detail = send_slack_alert(config, report)
    report["alert_sent"] = alert_sent
    report["alert_detail"] = alert_detail
    write_report(config, report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Hermes PlayMCP healthcheck: status={report['status']} "
            f"renewal_required={report['renewal_required']} probe_ok={report['probe_ok']} "
            f"report={report_path}"
        )
        if alert_detail != "not needed":
            print(f"alert: {alert_detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
