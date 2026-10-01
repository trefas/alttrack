"""Command line interface of alttrack.

Every command here is duplicated by the web dashboard; both call the same
service layer (watchlist / journal / archive / refresh / tasks).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__, archive, journal, refresh, tasks, watchlist
from .api import ALTRepoClient, ApiError
from .config import Config, load_config
from .db import open_db
from .models import EVENT_LABELS, EVENT_TYPES

app = typer.Typer(
    name="alttrack",
    help="Отслеживание жизненного цикла пакетов в репозиториях Альт Линукс.",
    no_args_is_help=True,
    add_completion=False,
)
watch_app = typer.Typer(help="Управление списком отслеживаемых пакетов (CRUD).", no_args_is_help=True)
task_app = typer.Typer(help="Сборочные задания.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")
app.add_typer(task_app, name="task")

console = Console()
err_console = Console(stderr=True, style="bold red")

DB_OPTION = typer.Option(None, "--db", help="Путь к файлу SQLite (переопределяет конфиг).")


def _cfg(db: Optional[Path] = None) -> Config:
    return load_config(db_path=db)


def _fail(message: str) -> None:
    err_console.print(message)
    raise typer.Exit(code=1)


def _client(cfg: Config) -> ALTRepoClient:
    return ALTRepoClient(
        cfg.api_base_url, timeout=cfg.http_timeout, concurrency=cfg.http_concurrency
    )


def _nvr(old: str | None, new: str | None) -> str:
    return f"{old} → {new}"


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Версия alttrack и доступность API."""
    console.print(f"alttrack {__version__}")
    cfg = _cfg()
    try:
        with _client(cfg) as client:
            info = client.version()
        console.print(f"API: {info.get('name')} {info.get('version')}")
    except ApiError as exc:
        err_console.print(f"API недоступен: {exc}")


@app.command("refresh")
def refresh_cmd(
    db: Optional[Path] = DB_OPTION,
    no_archive: bool = typer.Option(False, "--no-archive", help="Не архивировать журнал в этом прогоне."),
    package: Optional[list[str]] = typer.Option(
        None, "--package", help="Освежить только указанные пакеты (можно несколько)."
    ),
) -> None:
    """Освежить данные из репозиториев и записать события в журнал."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        ids = None
        if package:
            ids = []
            for name in package:
                pkg = watchlist.get_package(conn, name)
                if pkg is None:
                    _fail(f"пакет {name!r} не отслеживается")
                ids.append(pkg.id)  # type: ignore[arg-type]
        result = refresh.run_refresh(conn, client, cfg, package_ids=ids, do_archive=not no_archive)
    finally:
        client.close()
        conn.close()

    if result.busy:
        _fail(result.message)
    color = {"ok": "green", "partial": "yellow", "error": "red"}[result.status]
    console.print(
        f"[{color}]прогон #{result.run_id}: {result.status}[/{color}] — "
        f"проверено пакетов: {result.checked}, событий: {result.events}"
    )
    for error in result.errors:
        err_console.print(error)
    if result.status == "error":
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    db: Optional[Path] = DB_OPTION,
    no_refresh: bool = typer.Option(False, "--no-refresh", help="Не освежать при старте."),
) -> None:
    """Запустить веб-интерфейс (освежение при старте + фоновый интервал)."""
    import uvicorn

    from .web.app import create_app

    cfg = _cfg(db)
    if host:
        cfg.host = host
    if port:
        cfg.port = port
    console.print(f"alttrack: http://{cfg.host}:{cfg.port}  (БД: {cfg.db_path})")
    uvicorn.run(create_app(cfg, initial_refresh=not no_refresh), host=cfg.host, port=cfg.port)


# ---------------------------------------------------------------------------
# watch CRUD
# ---------------------------------------------------------------------------
@watch_app.command("add")
def watch_add(
    name: str = typer.Argument(..., help="Имя исходного (или бинарного) пакета."),
    branch: Optional[list[str]] = typer.Option(
        None, "--branch", "-b", help="Ветка для отслеживания (можно несколько). Без флага — все активные ветки с пакетом."
    ),
    note: str = typer.Option("", "--note", help="Заметка."),
    no_errata: bool = typer.Option(False, "--no-errata", help="Не отслеживать errata/уязвимости."),
    no_maintainer: bool = typer.Option(False, "--no-maintainer", help="Не отслеживать сопровождающего."),
    no_tasks: bool = typer.Option(False, "--no-tasks", help="Не отслеживать сборочные задания."),
    no_backfill: bool = typer.Option(False, "--no-backfill", help="Не импортировать историю сборок."),
    backfill_limit: int = typer.Option(50, "--backfill-limit", help="Сколько сборок импортировать на ветку."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Добавить пакет в список отслеживаемых."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        pkg, report = watchlist.add_package(
            conn,
            client,
            name=name,
            branches=branch,
            note=note,
            watch_errata=not no_errata,
            watch_maintainer=not no_maintainer,
            watch_tasks=not no_tasks,
            backfill_limit=backfill_limit,
            backfill=not no_backfill,
        )
    except watchlist.ValidationError as exc:
        _fail(str(exc))
    except ApiError as exc:
        _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()

    console.print(f"[green]добавлено:[/green] {pkg.name} → ветки: {', '.join(pkg.branches)}")
    for warning in report["warnings"]:
        err_console.print(f"предупреждение: {warning}")


@watch_app.command("list")
def watch_list(
    db: Optional[Path] = DB_OPTION,
    json_out: bool = typer.Option(False, "--json", help="JSON-вывод."),
) -> None:
    """Показать список отслеживаемых пакетов."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        packages = watchlist.list_packages(conn)
        if json_out:
            console.print_json(json.dumps([_pkg_dict(p) for p in packages], ensure_ascii=False))
            return
        table = Table(title=f"Отслеживаемые пакеты ({len(packages)})")
        table.add_column("ID", justify="right")
        table.add_column("Пакет")
        table.add_column("Ветки")
        table.add_column("Состояние")
        table.add_column("Проверен")
        table.add_column("Заметка")
        for p in packages:
            table.add_row(
                str(p.id),
                p.name,
                ", ".join(p.branches),
                "вкл" if p.enabled else "[dim]пауза[/dim]",
                (p.last_checked_at or "—").replace("T", " ")[:19],
                p.note,
            )
        console.print(table)
        if not packages:
            console.print("[dim]список пуст — добавьте пакет: alttrack watch add <имя>[/dim]")
    finally:
        conn.close()


@watch_app.command("show")
def watch_show(
    key: str = typer.Argument(..., help="ID или имя пакета."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Состояние пакета по веткам и последние события."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            pkg = watchlist.require_package(conn, key)
        except KeyError:
            _fail(f"пакет {key!r} не отслеживается")
        snaps = watchlist.snapshot_map(conn, pkg.id)  # type: ignore[arg-type]

        console.print(f"[bold]{pkg.name}[/bold] (id={pkg.id}) — {'вкл' if pkg.enabled else 'пауза'}")
        if pkg.note:
            console.print(f"заметка: {pkg.note}")
        table = Table(title="Состояние по веткам")
        table.add_column("Ветка")
        table.add_column("Наличие")
        table.add_column("Версия")
        table.add_column("Сопровождающий")
        table.add_column("Проверено")
        for branch in pkg.branches:
            snap = snaps.get(branch)
            if snap is None:
                table.add_row(branch, "[red]нет снимка[/red]", "—", "—", "—")
                continue
            present = "есть" if snap["present"] else "[yellow]нет[/yellow]"
            version = f"{snap['version']}-{snap['release']}" if snap["version"] else "—"
            table.add_row(
                branch,
                present,
                version,
                snap["packager"] or "—",
                (snap["observed_at"] or "").replace("T", " ")[:19],
            )
        console.print(table)

        events, total = journal.query_events(conn, package=pkg.name, scope="all", limit=10)
        if events:
            console.print(f"[bold]последние события ({total}):[/bold]")
            _print_events(events)
    finally:
        conn.close()


@watch_app.command("edit")
def watch_edit(
    key: str = typer.Argument(..., help="ID или имя пакета."),
    set_branch: Optional[list[str]] = typer.Option(
        None, "--set-branch", "-b", help="Полностью заменить набор веток."
    ),
    add_branch: Optional[list[str]] = typer.Option(None, "--add-branch", help="Добавить ветку."),
    remove_branch: Optional[list[str]] = typer.Option(None, "--remove-branch", help="Убрать ветку."),
    note: Optional[str] = typer.Option(None, "--note", help="Заметка."),
    enable: Optional[bool] = typer.Option(
        None, "--enable/--disable", help="Включить/приостановить отслеживание."
    ),
    errata: Optional[bool] = typer.Option(None, "--errata/--no-errata"),
    maintainer: Optional[bool] = typer.Option(None, "--maintainer/--no-maintainer"),
    task_watch: Optional[bool] = typer.Option(None, "--tasks/--no-tasks"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Изменить параметры отслеживания (ветки, заметки, переключатели)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        try:
            pkg = watchlist.require_package(conn, key)
        except KeyError:
            _fail(f"пакет {key!r} не отслеживается")

        branches: list[str] | None = None
        if set_branch is not None:
            branches = list(set_branch)
        elif add_branch or remove_branch:
            branches = list(pkg.branches)
            for b in add_branch or []:
                if b not in branches:
                    branches.append(b)
            for b in remove_branch or []:
                if b in branches:
                    branches.remove(b)

        try:
            updated, report = watchlist.update_package(
                conn,
                client,
                pkg,
                branches=branches,
                note=note,
                watch_errata=errata,
                watch_maintainer=maintainer,
                watch_tasks=task_watch,
                enabled=enable,
            )
        except watchlist.ValidationError as exc:
            _fail(str(exc))
    except ApiError as exc:
        _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()

    console.print(f"[green]обновлено:[/green] {updated.name} → ветки: {', '.join(updated.branches)}")
    if report.get("added"):
        console.print(f"добавлены ветки: {', '.join(report['added'])}")
    if report.get("removed"):
        console.print(f"убраны ветки: {', '.join(report['removed'])} (снимки сохранены)")
    for warning in report.get("warnings", []):
        err_console.print(f"предупреждение: {warning}")


@watch_app.command("rm")
def watch_rm(
    key: str = typer.Argument(..., help="ID или имя пакета."),
    purge: bool = typer.Option(False, "--purge", help="Удалить также журнал и историю пакета."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Убрать пакет из отслеживаемых."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            pkg = watchlist.require_package(conn, key)
        except KeyError:
            _fail(f"пакет {key!r} не отслеживается")
        if not yes:
            suffix = " вместе с журналом" if purge else ""
            if not typer.confirm(f"Убрать {pkg.name}{suffix}?"):
                raise typer.Abort()
        watchlist.delete_package(conn, pkg, purge=purge)
    finally:
        conn.close()
    console.print(f"[green]удалено:[/green] {pkg.name}" + (" (с журналом)" if purge else ""))


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------
@app.command()
def log(
    package: Optional[str] = typer.Option(None, "--package", "-p"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b"),
    event_type: Optional[list[str]] = typer.Option(
        None, "--type", "-t", help=f"Тип события. Доступны: {', '.join(EVENT_TYPES)}"
    ),
    since: Optional[str] = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: Optional[str] = typer.Option(None, "--until", help="YYYY-MM-DD"),
    scope: str = typer.Option("live", "--scope", help="live | archive | all"),
    limit: int = typer.Option(30, "--limit", "-n"),
    offset: int = typer.Option(0, "--offset"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Журнал событий с фильтрами."""
    if scope not in journal.SCOPES:
        _fail(f"scope должен быть одним из: {', '.join(journal.SCOPES)}")
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        events, total = journal.query_events(
            conn,
            package=package,
            branch=branch,
            event_types=event_type,
            since=since,
            until=until,
            scope=scope,
            limit=limit,
            offset=offset,
        )
        _print_events(events, total=total)
    finally:
        conn.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="FTS-запрос (например: firefox AND CVE)."),
    scope: str = typer.Option("all", "--scope", help="live | archive | all"),
    package: Optional[str] = typer.Option(None, "--package", "-p"),
    limit: int = typer.Option(30, "--limit", "-n"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Полнотекстовый поиск по журналу и архиву."""
    if scope not in journal.SCOPES:
        _fail(f"scope должен быть одним из: {', '.join(journal.SCOPES)}")
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        events, total = journal.query_events(
            conn, q=query, scope=scope, package=package, limit=limit
        )
        if not events:
            console.print("[dim]ничего не найдено[/dim]")
            return
        _print_events(events, total=total)
    finally:
        conn.close()


def archive_cmd(
    older_than: Optional[int] = typer.Option(None, "--older-than", help="Возраст в днях."),
    max_rows: Optional[int] = typer.Option(None, "--max-rows", help="Максимум строк в живом журнале."),
    package: Optional[str] = typer.Option(None, "--package", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Только показать, не переносить."),
    purge_older_than: Optional[int] = typer.Option(
        None, "--purge-older-than", help="Удалить записи архива старше N дней."
    ),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Архивация журнала (и очистка архива)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        if purge_older_than is not None:
            res = archive.purge_archive(conn, older_than_days=purge_older_than, dry_run=dry_run)
            console.print(
                f"архив: {'будет удалено' if dry_run else 'удалено'} {res['deleted']} записей"
            )
            return
        res = archive.archive_now(
            conn,
            cfg,
            older_than_days=older_than,
            max_live_rows=max_rows,
            package=package,
            dry_run=dry_run,
        )
        verb = "будет перенесено" if dry_run else "перенесено"
        console.print(
            f"{verb} {res['moved']} записей; в живом журнале: {res['live_remaining']}, "
            f"в архиве: {res['archive_total']}"
        )
    finally:
        conn.close()


app.command(name="archive")(archive_cmd)


@app.command()
def stats(
    scope: str = typer.Option("all", "--scope", help="live | archive | all"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сводка по журналу."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        data = journal.stats(conn, scope=scope)
        console.print(
            f"всего: {data['total']} (живых: {data['live']}, в архиве: {data['archive']})"
        )
        if data["oldest"]:
            console.print(f"период: {data['oldest'][:19]} … {data['newest'][:19]}")

        type_table = Table(title="По типам событий")
        type_table.add_column("Тип")
        type_table.add_column("Описание")
        type_table.add_column("Н", justify="right")
        for event_type, count in data["by_type"].items():
            type_table.add_row(
                event_type, data["labels"].get(event_type, ""), str(count)
            )
        console.print(type_table)

        branch_table = Table(title="По веткам")
        branch_table.add_column("Ветка")
        branch_table.add_column("Н", justify="right")
        for branch, count in list(data["by_branch"].items())[:10]:
            branch_table.add_row(branch, str(count))
        console.print(branch_table)

        pkg_table = Table(title="По пакетам (топ-10)")
        pkg_table.add_column("Пакет")
        pkg_table.add_column("Н", justify="right")
        for name, count in list(data["by_package"].items())[:10]:
            pkg_table.add_row(name, str(count))
        console.print(pkg_table)
    finally:
        conn.close()


@app.command("runs")
def runs_cmd(
    limit: int = typer.Option(10, "--limit", "-n"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """История прогонов освежения."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        rows = refresh.list_runs(conn, limit=limit)
    finally:
        conn.close()
    table = Table(title="Прогоны")
    for column in ("ID", "Начато", "Статус", "Пакетов", "Событий", "Сообщение"):
        table.add_column(column)
    for row in rows:
        status = row["status"]
        color = {"ok": "green", "partial": "yellow", "error": "red"}.get(status, "white")
        table.add_row(
            str(row["id"]),
            (row["started_at"] or "").replace("T", " ")[:19],
            f"[{color}]{status}[/{color}]",
            str(row["checked"]),
            str(row["events"]),
            (row["message"] or "")[:60],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------
def tasks_list(
    package: Optional[str] = typer.Option(None, "--package", "-p"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b"),
    active: bool = typer.Option(False, "--active", help="Только идущие задания."),
    failed: bool = typer.Option(False, "--failed", help="Только сбои."),
    limit: int = typer.Option(30, "--limit", "-n"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сборочные задания отслеживаемых пакетов."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        rows, total = tasks.list_tasks(
            conn,
            package=package,
            branch=branch,
            active=True if active else (False if failed else None),
            outcome="failure" if failed else None,
            limit=limit,
        )
        _print_tasks(rows, total)
    finally:
        conn.close()


app.command(name="tasks")(tasks_list)


@task_app.command("show")
def task_show(
    task_id: int = typer.Argument(..., help="Номер задания."),
    branch: Optional[str] = typer.Option(None, "--branch", "-b"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Карточка сборочного задания: состояние, этапы, пакеты."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        data = tasks.get_task(conn, task_id, branch)
        if data is None:
            _fail(f"задание {task_id} не найдено в БД (для отслеживаемых пакетов)")
    finally:
        conn.close()

    color = "red" if data["state"] in ("FAILED", "EPERM") else "green" if data["state"] == "DONE" else "yellow"
    console.print(
        f"[bold]задание {data['task_id']}[/bold] ({data['branch']}) — "
        f"[{color}]{data['state']}[/{color}], этап: {data['stage'] or '—'}"
    )
    console.print(
        f"владелец: {data['owner'] or '—'}, попытка: {data['try_no'] or '—'}, "
        f"обновлено: {(data['changed_at'] or '—').replace('T', ' ')}"
    )
    if data["message"]:
        console.print(f"сообщение: {data['message']}")
    if data["packages"]:
        console.print("пакеты: " + ", ".join(data["packages"]))
    if data["stages"]:
        stage_table = Table(title="Этапы")
        stage_table.add_column("Время")
        stage_table.add_column("Состояние")
        stage_table.add_column("Этап")
        for stage in data["stages"]:
            stage_table.add_row(
                stage["ts"].replace("T", " ")[:19], stage["state"], stage["stage"] or "—"
            )
        console.print(stage_table)
    if data["events"]:
        console.print("[bold]события журнала:[/bold]")
        for ev in data["events"]:
            console.print(
                f"  {ev['ts'][:19].replace('T', ' ')} {ev['event_type']}: "
                f"{ev['old_value'] or '—'} → {ev['new_value'] or '—'}"
            )


@app.command("config")
def config_cmd(
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Показать текущую конфигурацию."""
    cfg = _cfg(db)
    for key, value in cfg.to_dict().items():
        console.print(f"{key} = {value}")


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def _pkg_dict(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "branches": p.branches,
        "note": p.note,
        "enabled": p.enabled,
        "watch_errata": p.watch_errata,
        "watch_maintainer": p.watch_maintainer,
        "watch_tasks": p.watch_tasks,
        "added_at": p.added_at,
        "last_checked_at": p.last_checked_at,
    }


def _print_events(events: list[Any], total: int | None = None) -> None:
    table = Table()
    table.add_column("Время")
    table.add_column("Пакет")
    table.add_column("Ветка")
    table.add_column("Событие")
    table.add_column("Было → стало")
    table.add_column("Хранилище")
    for ev in events:
        label = EVENT_LABELS.get(ev.event_type, ev.event_type)
        table.add_row(
            ev.ts[:19].replace("T", " "),
            ev.package,
            ev.branch or "—",
            label,
            f"{ev.old_value or '—'} → {ev.new_value or '—'}",
            "архив" if ev.store == "archive" else "",
        )
    console.print(table)
    if total is not None:
        console.print(f"[dim]найдено: {total}[/dim]")


def _print_tasks(rows: list[dict[str, Any]], total: int) -> None:
    table = Table(title=f"Сборочные задания ({total})")
    for column in ("Задание", "Ветка", "Пакеты", "Состояние", "Этап", "Попытка", "Обновлено"):
        table.add_column(column)
    for row in rows:
        state = row["state"]
        color = "red" if state in ("FAILED", "EPERM") else "green" if state == "DONE" else "yellow"
        table.add_row(
            str(row["task_id"]),
            row["branch"],
            (row.get("packages") or "")[:30],
            f"[{color}]{state}[/{color}]",
            row["stage"] or "—",
            str(row["try_no"] or "—"),
            (row["last_seen_at"] or "").replace("T", " ")[:19],
        )
    if rows:
        console.print(table)
    else:
        console.print("[dim]нет заданий (или пакеты ещё не отслеживаются)[/dim]")


if __name__ == "__main__":
    app()
