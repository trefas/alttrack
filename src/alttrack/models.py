"""Domain models and constants shared across CLI and web."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

# ---------------------------------------------------------------------------
# Journal event types
# ---------------------------------------------------------------------------
TRACKING_STARTED = "tracking_started"
VERSION_CHANGED = "version_changed"
ADDED_TO_BRANCH = "added_to_branch"
REMOVED_FROM_BRANCH = "removed_from_branch"
MAINTAINER_CHANGED = "maintainer_changed"
ERRATA = "errata"
BUILD = "build"
NOT_FOUND = "not_found"

TASK_DISCOVERED = "task_discovered"
TASK_STATE_CHANGED = "task_state_changed"
TASK_FAILED = "task_failed"
TASK_DONE = "task_done"
TASK_RETRIED = "task_retried"

EVENT_TYPES: tuple[str, ...] = (
    TRACKING_STARTED,
    VERSION_CHANGED,
    ADDED_TO_BRANCH,
    REMOVED_FROM_BRANCH,
    MAINTAINER_CHANGED,
    ERRATA,
    BUILD,
    NOT_FOUND,
    TASK_DISCOVERED,
    TASK_STATE_CHANGED,
    TASK_FAILED,
    TASK_DONE,
    TASK_RETRIED,
)

# Event types produced by the build-task watcher.
TASK_EVENT_TYPES: frozenset[str] = frozenset(
    {TASK_DISCOVERED, TASK_STATE_CHANGED, TASK_FAILED, TASK_DONE, TASK_RETRIED}
)

EVENT_LABELS: dict[str, str] = {
    TRACKING_STARTED: "начало отслеживания",
    VERSION_CHANGED: "изменение версии",
    ADDED_TO_BRANCH: "появился в ветке",
    REMOVED_FROM_BRANCH: "исчез из ветки",
    MAINTAINER_CHANGED: "смена сопровождающего",
    ERRATA: "errata / уязвимость",
    BUILD: "сборка в репозитории",
    NOT_FOUND: "пакет не найден",
    TASK_DISCOVERED: "задание на сборку",
    TASK_STATE_CHANGED: "смена состояния задания",
    TASK_FAILED: "СБОЙ СБОРКИ",
    TASK_DONE: "сборка завершена",
    TASK_RETRIED: "повторная попытка сборки",
}

# ---------------------------------------------------------------------------
# Build task states (as reported by /task/progress/*)
# ---------------------------------------------------------------------------
TASK_SUCCESS_STATES: frozenset[str] = frozenset({"DONE"})
TASK_FAILURE_STATES: frozenset[str] = frozenset({"FAILED", "EPERM"})
TASK_CLOSED_STATES: frozenset[str] = frozenset(
    {"DELETED", "BROKEN", "MOVED", "CANCELLED", "HOLD"}
)

# States that mean "the task is still running".
TASK_ACTIVE_STATES: frozenset[str] = frozenset(
    {"NEW", "WAIT", "AWAITING", "BUILDING", "TESTED", "APPROVING", "POSTBUILD"}
)


def task_is_terminal(state: str) -> bool:
    return state in TASK_SUCCESS_STATES or state in TASK_FAILURE_STATES or state in TASK_CLOSED_STATES


def task_outcome(state: str) -> str:
    """'success' | 'failure' | 'closed' | '' (still running / unknown)."""
    if state in TASK_SUCCESS_STATES:
        return "success"
    if state in TASK_FAILURE_STATES:
        return "failure"
    if state in TASK_CLOSED_STATES:
        return "closed"
    return ""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class TrackedPackage:
    id: int | None
    name: str
    branches: list[str]
    note: str = ""
    watch_errata: bool = True
    watch_maintainer: bool = True
    watch_tasks: bool = True
    enabled: bool = True
    added_at: str = ""
    last_checked_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "TrackedPackage":
        return cls(
            id=row["id"],
            name=row["name"],
            branches=json.loads(row["branches"] or "[]"),
            note=row["note"] or "",
            watch_errata=bool(row["watch_errata"]),
            watch_maintainer=bool(row["watch_maintainer"]),
            watch_tasks=bool(row["watch_tasks"]),
            enabled=bool(row["enabled"]),
            added_at=row["added_at"],
            last_checked_at=row["last_checked_at"],
        )


@dataclasses.dataclass
class Event:
    seq: int
    ts: str
    package: str
    package_id: int | None
    branch: str | None
    event_type: str
    old_value: str | None
    new_value: str | None
    detail: dict[str, Any]
    store: str = "live"

    @classmethod
    def from_row(cls, row: Any, store: str = "live") -> "Event":
        detail = row["detail"]
        if isinstance(detail, (str, bytes)):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {"raw": detail}
        return cls(
            seq=row["seq"],
            ts=row["ts"],
            package=row["package"],
            package_id=row["package_id"],
            branch=row["branch"],
            event_type=row["event_type"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            detail=detail or {},
            store=store,
        )


def event_text(event: Event) -> str:
    """Flat text used for the full-text search index."""
    parts = [
        event.package,
        event.branch or "",
        event.event_type,
        EVENT_LABELS.get(event.event_type, event.event_type),
        event.old_value or "",
        event.new_value or "",
    ]
    parts.extend(str(v) for v in event.detail.values())
    return " ".join(p for p in parts if p)
