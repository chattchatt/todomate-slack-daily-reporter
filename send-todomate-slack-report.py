#!/usr/bin/env python3
"""Send TodoMate daily work messages to a Slack DM.

The default send path uses the local ``agent-slack`` CLI and a concrete Slack
channel ID so scheduled sends do not depend on Slack.app UI automation,
Accessibility permissions, or keyboard focus. The previous Slack.app UI path is
still available as an explicit legacy fallback.

User-specific values can be provided through environment variables or a JSON
config file at ~/.config/todomate-slack-dm/config.json. Environment variables
take precedence over the config file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


APP_NAME = "todomate-slack-dm"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.json"
DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
DEFAULT_MCPORTER_CANDIDATES = ("mcporter",)
DEFAULT_AGENT_SLACK_CANDIDATES = ("agent-slack",)
DEFAULT_SLACK_CHANNEL_ID = ""


class RecoverableError(RuntimeError):
    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.action = action


@dataclass(frozen=True)
class TodoItem:
    id: str
    goal_id: str
    goal_name: str
    content: str
    is_done: bool


@dataclass(frozen=True)
class Settings:
    dm_name: str
    excluded_goals: set[str]
    excluded_goal_ids: set[str]
    slack_app: Path
    slack_send_method: str
    slack_channel_id: str
    agent_slack_candidates: tuple[str, ...]
    state_dir: Path
    log_dir: Path
    timezone_name: str
    timezone: ZoneInfo
    mcporter_candidates: tuple[str, ...]
    redis_url: str


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def load_config_file() -> dict[str, Any]:
    raw_path = os.environ.get("TODOMATE_SLACK_DM_CONFIG")
    config_path = Path(raw_path).expanduser() if raw_path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid config JSON: {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid config JSON: {config_path}: top-level value must be an object")
    return data


def config_value(config: dict[str, Any], env_name: str, key: str, default: Any = None) -> Any:
    return os.environ.get(env_name, config.get(key, default))


def config_list(config: dict[str, Any], env_name: str, key: str, default: list[str]) -> list[str]:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return split_csv(env_value)
    value = config.get(key, default)
    if isinstance(value, str):
        return split_csv(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return default


def load_settings() -> Settings:
    config = load_config_file()
    state_dir = Path(str(config_value(config, "TODOMATE_STATE_DIR", "state_dir", DEFAULT_STATE_DIR))).expanduser()
    log_dir = Path(str(config_value(config, "TODOMATE_LOG_DIR", "log_dir", state_dir / "logs"))).expanduser()
    timezone_name = str(config_value(config, "TODOMATE_TIMEZONE", "timezone", "Asia/Seoul"))
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise SystemExit(f"invalid timezone: {timezone_name}") from exc

    return Settings(
        dm_name=str(config_value(config, "TODOMATE_SLACK_DM_NAME", "dm_name", "Slack report recipient")),
        excluded_goals=set(config_list(config, "TODOMATE_EXCLUDED_GOALS", "excluded_goals", [])),
        excluded_goal_ids=set(config_list(config, "TODOMATE_EXCLUDED_GOAL_IDS", "excluded_goal_ids", [])),
        slack_app=Path(str(config_value(config, "TODOMATE_SLACK_APP", "slack_app", "/Applications/Slack.app"))).expanduser(),
        slack_send_method=str(
            config_value(config, "TODOMATE_SLACK_SEND_METHOD", "slack_send_method", "agent-slack")
        ).strip().lower(),
        slack_channel_id=str(
            config_value(config, "TODOMATE_SLACK_CHANNEL_ID", "slack_channel_id", DEFAULT_SLACK_CHANNEL_ID)
        ).strip(),
        agent_slack_candidates=tuple(
            config_list(
                config,
                "TODOMATE_AGENT_SLACK_PATHS",
                "agent_slack_paths",
                list(DEFAULT_AGENT_SLACK_CANDIDATES),
            )
        ),
        state_dir=state_dir,
        log_dir=log_dir,
        timezone_name=timezone_name,
        timezone=timezone,
        mcporter_candidates=tuple(
            config_list(config, "TODOMATE_MCPORTER_PATHS", "mcporter_paths", list(DEFAULT_MCPORTER_CANDIDATES))
        ),
        redis_url=str(
            config_value(config, "TODOMATE_REDIS_URL", "redis_url", os.environ.get("REDIS_URL", ""))
        ).strip(),
    )


SETTINGS = load_settings()
_REDIS_CLIENT: Any | None = None


def now_local() -> datetime:
    return datetime.now(SETTINGS.timezone)


def ensure_dirs() -> None:
    SETTINGS.state_dir.mkdir(parents=True, exist_ok=True)
    SETTINGS.log_dir.mkdir(parents=True, exist_ok=True)


def log_event(level: str, event: str, **fields: Any) -> None:
    ensure_dirs()
    payload = {
        "ts": now_local().isoformat(),
        "level": level,
        "event": event,
        **fields,
    }
    with (SETTINGS.log_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def notify(title: str, message: str) -> None:
    if not shutil.which("osascript"):
        print(f"{title}: {message}", file=sys.stderr)
        return
    subprocess.run(
        [
            "osascript",
            "-e",
            f"display notification {json.dumps(message)} with title {json.dumps(title)}",
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def run_json_command(args: list[str], code: str, action: str, timeout: int = 45) -> dict[str, Any]:
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RecoverableError(code, f"command not found: {args[0]}", action) from exc
    except subprocess.TimeoutExpired as exc:
        raise RecoverableError(code, f"command timed out: {' '.join(args)}", action) from exc

    if proc.returncode != 0:
        raise RecoverableError(
            code,
            f"command failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}",
            action,
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RecoverableError(code, f"invalid JSON response: {proc.stdout[:300]}", action) from exc


def run_json_command_with_retries(
    args: list[str],
    code: str,
    action: str,
    timeout: int = 45,
    attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> dict[str, Any]:
    max_attempts = attempts or int(os.environ.get("TODOMATE_MCPORTER_RETRY_ATTEMPTS", "3"))
    delay = backoff_seconds
    if delay is None:
        delay = float(os.environ.get("TODOMATE_MCPORTER_RETRY_BACKOFF_SECONDS", "8"))

    last_error: RecoverableError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                log_event("info", "mcporter_retry", attempt=attempt, max_attempts=max_attempts)
            return run_json_command(args, code=code, action=action, timeout=timeout)
        except RecoverableError as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            log_event(
                "warning",
                "mcporter_attempt_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                message=str(exc),
            )
            time.sleep(delay * attempt)

    assert last_error is not None
    raise last_error


def resolve_command(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            return str(expanded)
    return None


def mcporter_call(tool: str, *params: str) -> dict[str, Any]:
    mcporter = resolve_command(SETTINGS.mcporter_candidates)
    if not mcporter:
        raise RecoverableError(
            "mcporter_missing",
            "mcporter command is not available",
            "mcporter 설치 경로 또는 launchd PATH 설정을 확인하세요.",
        )
    return run_json_command_with_retries(
        [mcporter, "call", f"mcp-gateway.{tool}", *params],
        code="todomate_mcp_failed",
        action="TodoMate 연결 상태를 확인한 뒤 다시 실행하세요.",
        timeout=int(os.environ.get("TODOMATE_MCPORTER_TIMEOUT_SECONDS", "75")),
    )


def goals_cache_path() -> Path:
    return SETTINGS.state_dir / "goals.cache.json"


def read_goals_cache() -> dict[str, str]:
    cached = read_json(goals_cache_path())
    if not cached:
        return {}
    raw_goals = cached.get("goals", cached)
    if not isinstance(raw_goals, dict):
        return {}
    return {str(goal_id): str(name) for goal_id, name in raw_goals.items() if str(goal_id) and str(name)}


def write_goals_cache(goals: dict[str, str]) -> None:
    if not goals:
        return
    write_json(
        goals_cache_path(),
        {
            "cached_at": now_local().isoformat(),
            "goals": goals,
        },
    )


def load_goals(required: bool = True) -> dict[str, str]:
    try:
        goals = fetch_goals()
    except RecoverableError as exc:
        cached = read_goals_cache()
        if cached:
            log_event(
                "warning",
                "todomate_goals_cache_used",
                message=str(exc),
                cached_goal_count=len(cached),
            )
            return cached
        if required:
            raise
        log_event("warning", "todomate_goals_unavailable_without_cache", message=str(exc))
        return {}
    write_goals_cache(goals)
    return goals


def fetch_goals() -> dict[str, str]:
    data = mcporter_call("TodoMate-loadGoals")
    goals = data.get("goals") or data.get("items") or []
    if not isinstance(goals, list):
        raise RecoverableError(
            "todomate_goal_shape",
            "TodoMate goals response shape is not a list",
            "TodoMate 목표 응답 형식이 바뀌었는지 확인하세요.",
        )
    result: dict[str, str] = {}
    for goal in goals:
        if not isinstance(goal, dict):
            continue
        goal_id = str(goal.get("id") or goal.get("goalID") or "")
        name = str(goal.get("name") or goal.get("title") or goal.get("content") or "")
        if goal_id and name:
            result[goal_id] = name
    return result


def load_items(day: datetime) -> list[TodoItem]:
    data = mcporter_call(
        "TodoMate-loadTodoItems",
        f"year:{day.year}",
        f"month:{day.month}",
        f"day:{day.day}",
        "limit:200",
    )
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise RecoverableError(
            "todomate_items_shape",
            "TodoMate items response shape is not a list",
            "TodoMate 할 일 응답 형식이 바뀌었는지 확인하세요.",
        )
    goals = load_goals(required=False)
    excluded_goal_ids = set(SETTINGS.excluded_goal_ids)
    excluded_goal_ids.update(goal_id for goal_id, goal_name in goals.items() if goal_name in SETTINGS.excluded_goals)
    items: list[TodoItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        goal_id = str(raw.get("goalID") or "")
        goal_name = goals.get(goal_id, "")
        if goal_name in SETTINGS.excluded_goals or goal_id in excluded_goal_ids:
            continue
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        items.append(
            TodoItem(
                id=str(raw.get("id") or ""),
                goal_id=goal_id,
                goal_name=goal_name,
                content=content,
                is_done=bool(raw.get("isDone")),
            )
        )
    return items


def item_to_dict(item: TodoItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "goal_id": item.goal_id,
        "goal_name": item.goal_name,
        "content": item.content,
        "is_done": item.is_done,
    }


def date_dir(day: datetime) -> Path:
    return SETTINGS.state_dir / day.strftime("%Y-%m-%d")


def marker_path(day: datetime, mode: str) -> Path:
    return date_dir(day) / f"{mode}.success.json"


def snapshot_path(day: datetime, mode: str) -> Path:
    return date_dir(day) / f"{mode}.json"


def redis_client() -> Any | None:
    if not SETTINGS.redis_url:
        return None
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        import redis
    except ImportError as exc:
        raise RecoverableError(
            "redis_dependency_missing",
            "redis package is required when TODOMATE_REDIS_URL or REDIS_URL is set",
            "Railway 공유 저장소를 쓰려면 Docker 이미지에 Python redis 패키지를 포함하세요.",
        ) from exc
    try:
        _REDIS_CLIENT = redis.Redis.from_url(
            SETTINGS.redis_url,
            decode_responses=True,
            socket_connect_timeout=10,
            socket_timeout=10,
        )
        _REDIS_CLIENT.ping()
    except Exception as exc:
        raise RecoverableError(
            "redis_unavailable",
            f"failed to connect to shared Redis state store: {exc}",
            "Railway Redis 서비스와 TODOMATE_REDIS_URL/REDIS_URL 연결 변수를 확인하세요.",
        ) from exc
    return _REDIS_CLIENT


def state_key(path: Path) -> str:
    try:
        rel = path.relative_to(SETTINGS.state_dir)
    except ValueError:
        rel = Path(path.name)
    return f"{APP_NAME}:state:{rel.as_posix()}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    client = redis_client()
    if client is not None:
        client.set(state_key(path), body)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    client = redis_client()
    if client is not None:
        raw = client.get(state_key(path))
        if raw is None:
            return None
        return json.loads(raw)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def state_exists(path: Path) -> bool:
    client = redis_client()
    if client is not None:
        return bool(client.exists(state_key(path)))
    return path.exists()


@contextmanager
def run_lock(day: datetime, mode: str) -> Any:
    """Prevent simultaneous schedulers from sending the same report twice."""
    lock_dir = date_dir(day) / f"{mode}.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    stale_after_seconds = 20 * 60

    while True:
        try:
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "created_at": now_local().isoformat(),
                        "mode": mode,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            age = time.time() - lock_dir.stat().st_mtime
            if age > stale_after_seconds:
                shutil.rmtree(lock_dir, ignore_errors=True)
                continue
            log_event("info", "skip_locked", mode=mode, date=day.strftime("%Y-%m-%d"))
            raise SystemExit(0)

    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def format_item_title(item: TodoItem) -> str:
    if item.goal_name:
        return f"[{item.goal_name}] {item.content}"
    return item.content


def format_morning_message(items: list[TodoItem]) -> str:
    if not items:
        return f"오늘 TodoMate에서 {SETTINGS.dm_name}에게 전달할 업무가 없습니다."
    return "\n".join(format_item_title(item) for item in items)


def category_lines(items: list[TodoItem]) -> list[str]:
    return [f"- {format_item_title(item)}" for item in items]


def append_category(lines: list[str], title: str, items: list[TodoItem]) -> None:
    lines.append(title)
    if items:
        lines.extend(category_lines(items))
    else:
        lines.append("- 없음")
    lines.append("")


def format_evening_message(day: datetime, current_items: list[TodoItem]) -> str:
    morning = read_json(snapshot_path(day, "morning"))
    morning_items = morning.get("items", []) if morning else []
    if not morning_items:
        completed = [item for item in current_items if item.is_done]
        incomplete = [item for item in current_items if not item.is_done]
        lines = [
            f"{day.strftime('%Y-%m-%d')} TodoMate 저녁 보고",
            "",
            "※ 오늘 오전 스냅샷이 없어, 현재 TodoMate 상태 기준으로 보고합니다.",
            "",
        ]
        append_category(lines, "1. 완료된 작업", completed)
        append_category(lines, "2. 미완료된 작업", incomplete)
        return "\n".join(lines).rstrip()

    morning_by_id = {
        str(item.get("id")): item for item in morning_items if isinstance(item, dict) and item.get("id")
    }
    morning_by_content = {
        str(item.get("content")): item
        for item in morning_items
        if isinstance(item, dict) and item.get("content")
    }

    planned_completed: list[TodoItem] = []
    planned_incomplete: list[TodoItem] = []
    modified_completed: list[TodoItem] = []
    modified_incomplete: list[TodoItem] = []
    added_completed: list[TodoItem] = []
    added_incomplete: list[TodoItem] = []

    for item in current_items:
        morning_raw = morning_by_id.get(item.id) if item.id else None
        if not morning_raw:
            morning_raw = morning_by_content.get(item.content)

        if isinstance(morning_raw, dict):
            morning_content = str(morning_raw.get("content") or "")
            same_content = morning_content == item.content
            if same_content:
                if item.is_done:
                    planned_completed.append(item)
                else:
                    planned_incomplete.append(item)
            elif item.is_done:
                modified_completed.append(item)
            else:
                modified_incomplete.append(item)
            continue

        if item.is_done:
            added_completed.append(item)
        else:
            added_incomplete.append(item)

    lines = [f"{day.strftime('%Y-%m-%d')} TodoMate 저녁 보고", ""]
    append_category(lines, "1. 당일 예정 작업이 완료된 것", planned_completed)
    append_category(lines, "2. 당일 예정 작업이 미완료된 것", planned_incomplete)
    append_category(lines, "3. 예정 작업이 수정되어 완료된 것", modified_completed)
    append_category(lines, "4. 예정 작업이 수정되어 미완료된 것", modified_incomplete)
    append_category(lines, "5. 추가된 작업이 완료된 것", added_completed)
    append_category(lines, "6. 추가된 작업이 미완료된 것", added_incomplete)
    return "\n".join(lines).rstrip()

def scheduled_guard(mode: str, force: bool) -> None:
    if force:
        return
    current = now_local()
    if current.weekday() >= 5:
        raise SystemExit(f"skip: weekend in {SETTINGS.timezone_name}")
    expected = {"morning": (9, 1), "evening": (18, 1)}.get(mode)
    if expected:
        tolerance = int(os.environ.get("TODOMATE_SCHEDULE_TOLERANCE_MINUTES", "10"))
        current_minutes = current.hour * 60 + current.minute
        expected_minutes = expected[0] * 60 + expected[1]
        if abs(current_minutes - expected_minutes) > tolerance:
            raise SystemExit(f"skip: not scheduled window for {mode} in {SETTINGS.timezone_name}")


def save_clipboard() -> str:
    if not shutil.which("pbpaste"):
        return ""
    proc = subprocess.run(["pbpaste"], text=True, capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def set_clipboard(text: str) -> None:
    proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RecoverableError(
            "clipboard_failed",
            proc.stderr.strip() or "pbcopy failed",
            "클립보드 접근 상태를 확인하세요.",
        )


def send_slack_message_via_agent_slack(message: str) -> None:
    if not SETTINGS.slack_channel_id:
        raise RecoverableError(
            "slack_channel_missing",
            "Slack channel ID is not configured",
            "TODOMATE_SLACK_CHANNEL_ID에 Slack DM/채널 ID를 설정하세요.",
        )

    agent_slack = resolve_command(SETTINGS.agent_slack_candidates)
    if not agent_slack:
        raise RecoverableError(
            "agent_slack_missing",
            "agent-slack command is not available",
            "agent-messenger의 agent-slack CLI 설치 경로 또는 launchd PATH 설정을 확인하세요.",
        )

    try:
        proc = subprocess.run(
            [agent_slack, "message", "send", SETTINGS.slack_channel_id, message],
            text=True,
            capture_output=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoverableError(
            "agent_slack_timeout",
            f"agent-slack timed out while sending to {SETTINGS.slack_channel_id}",
            "agent-slack 인증 상태와 Slack 네트워크 상태를 확인하세요.",
        ) from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "agent-slack message send failed"
        raise RecoverableError(
            "agent_slack_failed",
            detail,
            "agent-slack auth extract 및 agent-slack auth status --pretty 상태를 확인하세요.",
        )

    log_event(
        "info",
        "slack_agent_cli_sent",
        channel_id=SETTINGS.slack_channel_id,
        message_length=len(message),
    )


def send_slack_message_via_ui_legacy(message: str) -> None:
    if not SETTINGS.slack_app.exists():
        raise RecoverableError(
            "slack_app_missing",
            f"Slack.app is not installed at {SETTINGS.slack_app}",
            "Slack 앱 설치 위치를 확인하세요.",
        )
    if not shutil.which("osascript"):
        raise RecoverableError(
            "osascript_missing",
            "osascript is not available",
            "macOS AppleScript 실행 환경을 확인하세요.",
        )

    original_clipboard = save_clipboard()
    set_clipboard(message)
    script = f'''
tell application "Slack" to activate
delay 2
tell application "System Events"
  tell process "Slack"
    set frontmost to true
    keystroke "k" using command down
    delay 0.8
    keystroke {json.dumps(SETTINGS.dm_name, ensure_ascii=False)}
    delay 1.2
    key code 36
    delay 1.0
    keystroke "v" using command down
    delay 0.4
    key code 36
  end tell
end tell
'''
    try:
        proc = subprocess.run(["osascript", "-e", script], text=True, capture_output=True, timeout=20)
    finally:
        set_clipboard(original_clipboard)

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        hint = f"Slack 로그인, {SETTINGS.dm_name} DM 검색 가능 여부, macOS 접근성 권한을 확인하세요."
        raise RecoverableError("slack_ui_failed", stderr or "Slack UI automation failed", hint)


def send_slack_message(message: str) -> None:
    method = SETTINGS.slack_send_method
    if method in {"agent-slack", "agent_slack", "agent"}:
        send_slack_message_via_agent_slack(message)
        return
    if method in {"ui", "osascript", "legacy-ui", "legacy_ui"}:
        send_slack_message_via_ui_legacy(message)
        return
    raise RecoverableError(
        "slack_send_method_invalid",
        f"unsupported Slack send method: {method}",
        "TODOMATE_SLACK_SEND_METHOD를 agent-slack 또는 legacy-ui로 설정하세요.",
    )


def env_enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def failure_alert_marker_path(day: datetime, mode: str, code: str) -> Path:
    safe_code = re.sub(r"[^a-zA-Z0-9_.-]+", "_", code).strip("._") or "unknown"
    return SETTINGS.state_dir / day.strftime("%Y-%m-%d") / f"{mode}.{safe_code}.failure-alert.json"


def should_send_failure_alert(mode: str, exc: RecoverableError) -> bool:
    """Throttle closed-loop failure DMs to one alert per day/mode/error.

    Railway retries every five minutes inside the morning/evening window. That
    is useful for recovery, but the Slack DM should show one clear failure
    signal instead of a wall of repeated warnings.
    """
    marker = failure_alert_marker_path(now_local(), mode, exc.code)
    if marker.exists():
        log_event("info", "failure_alert_suppressed", mode=mode, original_error=exc.code)
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    write_json(marker, {"created_at": now_local().isoformat(), "mode": mode, "error": exc.code})
    return True


def send_failure_alert_to_slack(mode: str, exc: RecoverableError) -> None:
    """Best-effort failure alert that keeps TodoMate errors visible in the configured Slack target.

    This intentionally bypasses ``send_slack_message`` to avoid recursion through
    the normal report-send path. If Slack itself is the failing component, the
    alert is skipped and Railway logs remain the source of truth.
    """
    if not env_enabled("TODOMATE_FAILURE_ALERT_TO_SLACK", "1"):
        return
    if exc.code.startswith("agent_slack") or exc.code in {"slack_channel_missing", "slack_send_method_invalid"}:
        return
    if not SETTINGS.slack_channel_id:
        return
    agent_slack = resolve_command(SETTINGS.agent_slack_candidates)
    if not agent_slack:
        return
    if not should_send_failure_alert(mode, exc):
        return

    message = (
        f":warning: TodoMate Slack daily report failed ({mode})\n"
        f"- 오류: `{exc.code}`\n"
        f"- 조치: {exc.action}\n"
        f"- 시각: {now_local().isoformat()}\n"
        "_Railway closed-loop 재시도 창 안이면 다음 cron tick에서 자동 재시도합니다._"
    )
    try:
        proc = subprocess.run(
            [agent_slack, "message", "send", SETTINGS.slack_channel_id, message],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception as alert_exc:
        log_event("error", "failure_alert_exception", mode=mode, error=str(alert_exc))
        return
    if proc.returncode == 0:
        log_event("info", "failure_alert_sent", mode=mode, original_error=exc.code)
    else:
        log_event(
            "error",
            "failure_alert_failed",
            mode=mode,
            original_error=exc.code,
            detail=(proc.stderr.strip() or proc.stdout.strip())[:500],
        )


def agent_slack_auth_status(agent_slack: str | None) -> tuple[bool, str]:
    if not agent_slack:
        return False, "agent-slack missing"
    try:
        proc = subprocess.run(
            [agent_slack, "auth", "status", "--pretty"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        return False, str(exc)
    output = (proc.stderr.strip() or proc.stdout.strip()).splitlines()
    detail = output[0] if output else f"exit {proc.returncode}"
    return proc.returncode == 0, detail


def mark_success(day: datetime, mode: str, message: str, items: list[TodoItem], dry_run: bool) -> None:
    payload = {
        "date": day.strftime("%Y-%m-%d"),
        "mode": mode,
        "sent_at": now_local().isoformat(),
        "dry_run": dry_run,
        "item_count": len(items),
        "items": [item_to_dict(item) for item in items],
        "message": message,
    }
    write_json(snapshot_path(day, mode), payload)
    if not dry_run:
        write_json(marker_path(day, mode), {"ok": True, "sent_at": payload["sent_at"], "item_count": len(items)})


def diagnose() -> int:
    ensure_dirs()
    method = SETTINGS.slack_send_method
    agent_slack = resolve_command(SETTINGS.agent_slack_candidates)
    agent_slack_auth, agent_slack_auth_detail = agent_slack_auth_status(agent_slack)
    checks = {
        "mcporter": bool(resolve_command(SETTINGS.mcporter_candidates)),
        "agent_slack": bool(agent_slack),
        "agent_slack_auth": agent_slack_auth,
        "agent_slack_auth_detail": agent_slack_auth_detail,
        "slack_channel_id": SETTINGS.slack_channel_id,
        "slack_send_method": method,
        "agent_slack_paths": list(SETTINGS.agent_slack_candidates),
        "osascript": bool(shutil.which("osascript")),
        "pbcopy": bool(shutil.which("pbcopy")),
        "pbpaste": bool(shutil.which("pbpaste")),
        "slack_app": SETTINGS.slack_app.exists(),
        "state_dir": SETTINGS.state_dir.exists() and os.access(SETTINGS.state_dir, os.W_OK),
        "log_dir": SETTINGS.log_dir.exists() and os.access(SETTINGS.log_dir, os.W_OK),
        "configured_now": now_local().isoformat(),
        "timezone": SETTINGS.timezone_name,
        "dm_name": SETTINGS.dm_name,
        "excluded_goals": sorted(SETTINGS.excluded_goals),
        "excluded_goal_ids": sorted(SETTINGS.excluded_goal_ids),
        "state_dir_path": str(SETTINGS.state_dir),
        "log_dir_path": str(SETTINGS.log_dir),
        "shared_state_backend": "redis" if SETTINGS.redis_url else "filesystem",
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    if method in {"agent-slack", "agent_slack", "agent"}:
        boolean_checks = ("mcporter", "agent_slack", "agent_slack_auth", "slack_channel_id", "state_dir", "log_dir")
    elif method in {"ui", "osascript", "legacy-ui", "legacy_ui"}:
        boolean_checks = ("mcporter", "osascript", "pbcopy", "pbpaste", "slack_app", "state_dir", "log_dir")
    else:
        return 1
    return 0 if all(bool(checks[key]) for key in boolean_checks) else 1


def execute(mode: str, dry_run: bool, force: bool) -> int:
    ensure_dirs()
    scheduled_guard(mode, force=force or dry_run)
    day = now_local()

    with run_lock(day, mode):
        if state_exists(marker_path(day, mode)) and not dry_run:
            log_event("info", "skip_duplicate", mode=mode, date=day.strftime("%Y-%m-%d"))
            return 0

        items = load_items(day)
        if mode == "morning":
            message = format_morning_message(items)
        elif mode == "evening":
            message = format_evening_message(day, items)
        else:
            raise SystemExit(f"unsupported mode: {mode}")

        print(message)
        if not dry_run:
            send_slack_message(message)
        mark_success(day, mode, message, items, dry_run=dry_run)
        log_event("info", "mode_completed", mode=mode, dry_run=dry_run, item_count=len(items))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["morning", "evening", "diagnose"])
    parser.add_argument("--dry-run", action="store_true", help="Build and save message without Slack send.")
    parser.add_argument("--force", action="store_true", help="Bypass schedule guard.")
    args = parser.parse_args()

    if args.mode == "diagnose":
        return diagnose()

    try:
        return execute(args.mode, dry_run=args.dry_run, force=args.force)
    except RecoverableError as exc:
        log_event("error", exc.code, message=str(exc), action=exc.action, mode=args.mode)
        send_failure_alert_to_slack(args.mode, exc)
        notify("TodoMate Slack DM 자동화 실패", f"{exc.code}: {exc.action}")
        print(f"{exc.code}: {exc}\nAction: {exc.action}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
