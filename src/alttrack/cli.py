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

from . import __version__, archive, compare, images, journal, lists, metasync, products, refresh, refs, tasks, watchlist
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
product_app = typer.Typer(help="Продукты: образы дистрибутива и их состав.", no_args_is_help=True)
image_app = typer.Typer(help="Каталог образов дистрибутива (rdb /image).", no_args_is_help=True)
list_app = typer.Typer(help="Сохранённые списки пакетов для сравнения.", no_args_is_help=True)
compare_app = typer.Typer(help="Сравнение двух списков пакетов.", no_args_is_help=True)
meta_app = typer.Typer(
    help="Справочники rdb: пакеты (summary/группа/ACL), архитектуры, группы ПО.",
    no_args_is_help=True,
)
app.add_typer(watch_app, name="watch")
app.add_typer(task_app, name="task")
app.add_typer(product_app, name="product")
app.add_typer(image_app, name="image")
app.add_typer(list_app, name="list")
app.add_typer(compare_app, name="compare")
app.add_typer(meta_app, name="meta")

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


@watch_app.command("backfill")
def watch_backfill(
    key: Optional[list[str]] = typer.Option(
        None, "--package", "-p", help="Пакет (ID или имя; можно несколько)."
    ),
    all_packages: bool = typer.Option(False, "--all", help="Все отслеживаемые пакеты."),
    limit: int = typer.Option(
        0, "--limit", help="Сколько сборок на ветку (0 — из конфига: backfill_limit)."
    ),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Импортировать историю сборок (по запросу, с прогрессом)."""
    if not key and not all_packages:
        _fail("укажите --package (можно несколько) или --all")
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    effective_limit = limit or cfg.backfill_limit
    try:
        if all_packages:
            targets = watchlist.list_packages(conn)
        else:
            targets = []
            for item in key or []:
                try:
                    targets.append(watchlist.require_package(conn, item))
                except KeyError:
                    _fail(f"пакет {item!r} не отслеживается")
        total = 0
        errors = 0
        for index, pkg in enumerate(targets, 1):
            try:
                added = refresh.backfill_build_history(
                    conn, client, pkg.id, pkg.name, pkg.branches, effective_limit
                )
            except ApiError as exc:
                errors += 1
                err_console.print(f"{pkg.name}: {exc}")
                continue
            total += added
            console.print(
                f"[{index}/{len(targets)}] {pkg.name}: +{added}", highlight=False
            )
    finally:
        client.close()
        conn.close()
    console.print(
        f"[green]готово:[/green] пакетов {len(targets)}, импортировано событий {total} "
        f"(лимит {effective_limit} на ветку)"
    )
    if errors:
        raise typer.Exit(code=1)


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
# products / images / lists / compare / meta
# ---------------------------------------------------------------------------
def _say(text: str) -> None:
    console.print(f"[dim]{text}[/dim]")


def _require_product(conn, key: str):
    try:
        return products.require_product(conn, key)
    except KeyError:
        _fail(f"продукт {key!r} не найден (создайте: alttrack product create ...)")


def _ensure_catalog(conn, client) -> None:
    if images.catalog_is_empty(conn):
        _say("каталог образов пуст — загружаю…")
        count = images.refresh_catalog(conn, client)
        _say(f"загружено образов: {count}")


def _print_track_report(report: dict[str, Any]) -> None:
    console.print(
        f"[green]образ {report['image_uuid'][:8]}:[/green] "
        f"бинарников {report['binaries']} → srpm {report['srpms']}"
    )
    if report.get("added"):
        console.print(f"  [green]+ добавлено:[/green] {len(report['added'])}")
    if report.get("linked"):
        console.print(f"  [green]+ привязано:[/green] {len(report['linked'])}")
    if report.get("branch_updated"):
        console.print(f"  ветка продукта добавлена: {len(report['branch_updated'])}")
    if report.get("reactivated"):
        console.print(f"  [green]возобновлено:[/green] {len(report['reactivated'])}")
    if report.get("kept"):
        console.print(f"  осталось: {len(report['kept'])}")
    if report.get("paused"):
        console.print(f"  [yellow]- на паузу:[/yellow] {len(report['paused'])}")
    if report.get("existing"):
        console.print(f"  уже отслеживалось: {len(report['existing'])}")
    if report.get("not_found"):
        err_console.print(
            f"не найдено исходных пакетов: {len(report['not_found'])} "
            f"({', '.join(report['not_found'][:10])})"
        )
    if report.get("updated"):
        _say(f"версии в образе отличаются от репозитория: {report['updated']}")


@product_app.command("list")
def product_list(
    db: Optional[Path] = DB_OPTION,
    json_out: bool = typer.Option(False, "--json", help="JSON-вывод."),
) -> None:
    """Список продуктов."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        rows = products.list_products(conn)
        if json_out:
            console.print_json(json.dumps([vars(p) for p in rows], ensure_ascii=False))
            return
        table = Table(title=f"Продукты ({len(rows)})")
        for column in ("ID", "Название", "Ветка", "Edition", "Арх.", "Пакетов", "Создан"):
            table.add_column(column)
        for p in rows:
            counts = products.product_counts(conn, p)
            table.add_row(
                str(p.id), p.title, p.branch, p.edition, p.arch,
                str(counts["active"]) + (f" (+{counts['paused']} пауза)" if counts["paused"] else ""),
                p.created_at[:10],
            )
        console.print(table)
        if not rows:
            console.print("[dim]продуктов нет — создайте: alttrack product create \"Название\" --branch p11[/dim]")
    finally:
        conn.close()


@product_app.command("create")
def product_create(
    title: str = typer.Argument(..., help="Название продукта."),
    branch: str = typer.Option(..., "--branch", "-b", help="Ветка продукта (p11, p10, sisyphus…)."),
    edition: str = typer.Option("", "--edition", "-e", help="Edition образа (education, server…)."),
    arch: str = typer.Option("x86_64", "--arch", help="Архитектура."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Создать продукт."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            p = products.create_product(conn, title=title, branch=branch, edition=edition, arch=arch)
        except ValueError as exc:
            _fail(str(exc))
    finally:
        conn.close()
    console.print(f"[green]создан продукт[/green] id={p.id}: {p.title} ({p.branch}, {p.edition or '—'}, {p.arch})")


@product_app.command("show")
def product_show(
    key: str = typer.Argument(..., help="ID или название продукта."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Карточка продукта: состав, образы, последние сравнения."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        p = _require_product(conn, key)
        counts = products.product_counts(conn, p)
        console.print(
            f"[bold]{p.title}[/bold] (id={p.id}) — {p.branch}, "
            f"{p.edition or '—'}, {p.arch}; пакетов: {counts['total']} "
            f"(активных {counts['active']}, на паузе {counts['paused']})"
        )
        imgs = products.product_images(conn, p)
        if imgs:
            table = Table(title="Привязанные образы")
            for column in ("UUID", "Тег", "Тип", "Дата", "Пакетов", "Добавлен"):
                table.add_column(column)
            for row in imgs[:10]:
                table.add_row(
                    row["image_uuid"][:8], row["tag"] or "—", row["kind"],
                    (row["date"] or "—"), str(row["package_count"]), row["added_at"][:16].replace("T", " "),
                )
            console.print(table)
        comps = compare.list_comparisons(conn, product_id=p.id)
        if comps:
            console.print("сравнения: " + ", ".join(f"#{c.id} {c.title}" for c in comps[:5]))
    finally:
        conn.close()


@product_app.command("rm")
def product_rm(
    key: str = typer.Argument(..., help="ID или название продукта."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждения."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Удалить продукт (пакеты остаются отслеживаться)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        p = _require_product(conn, key)
        if not yes and not typer.confirm(f"Удалить продукт {p.title}?"):
            raise typer.Abort()
        products.delete_product(conn, p)
    finally:
        conn.close()
    console.print(f"[green]удалено:[/green] продукт {p.title} (пакеты не тронуты)")


# -- images ------------------------------------------------------------------
@image_app.command("refresh")
def image_refresh_cmd(
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Обновить каталог образов из rdb.altlinux.org."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        count = images.refresh_catalog(conn, client)
    except ApiError as exc:
        _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(f"[green]каталог обновлён:[/green] образов {count}")


@image_app.command("list")
def image_list(
    branch: Optional[str] = typer.Option(None, "--branch", "-b"),
    edition: Optional[str] = typer.Option(None, "--edition", "-e"),
    arch: Optional[str] = typer.Option(None, "--arch"),
    release: Optional[str] = typer.Option(None, "--release", help="release | test"),
    limit: int = typer.Option(30, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Каталог образов дистрибутива (с фильтрами)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        _ensure_catalog(conn, client)
        rows = images.catalog(
            conn, branch=branch, edition=edition, arch=arch, release=release, limit=limit
        )
        if json_out:
            console.print_json(json.dumps([dict(r) for r in rows], ensure_ascii=False))
            return
        table = Table(title=f"Образы ({len(rows)})")
        for column in ("UUID", "Ветка", "Edition", "Арх.", "Тип", "Дата", "Файл"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                row["uuid"][:8], row["branch"], row["edition"] or "—", row["arch"] or "—",
                (row["type"] or "—"), (row["date"] or "—"), (row["file"] or "")[:44],
            )
        console.print(table)
        _say("детали: alttrack image preview <uuid> --product <продукт>")
    finally:
        client.close()
        conn.close()


@image_app.command("preview")
def image_preview_cmd(
    uuid: str = typer.Argument(..., help="UUID образа (первые 8 знаков из image list)."),
    product: str = typer.Option(..., "--product", "-p", help="Продукт (ID или название)."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Показать, что изменится при добавлении образа (dry-run)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        p = _require_product(conn, product)
        try:
            preview = products.preview_from_image(conn, client, p, uuid, progress=_say)
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(
        f"бинарников: {preview['binaries']} → srpm: {len(preview['srpms'])}; "
        f"[green]новых {len(preview['new'])}[/green], "
        f"уже есть {len(preview['existing'])}"
    )
    if preview["not_found"]:
        err_console.print(f"без исходного пакета: {', '.join(preview['not_found'][:10])}")
    if preview["new"]:
        console.print("новые: " + ", ".join(preview["new"][:30]) + ("…" if len(preview["new"]) > 30 else ""))


@image_app.command("track")
def image_track(
    uuid: str = typer.Argument(..., help="UUID образа."),
    product: str = typer.Option(..., "--product", "-p", help="Продукт (ID или название)."),
    errata: bool = typer.Option(False, "--errata", help="Включить отслеживание errata."),
    maintainer: bool = typer.Option(False, "--maintainer", help="Включить отслеживание сопровождающего."),
    tasks: bool = typer.Option(False, "--tasks", help="Включить отслеживание сборочных заданий."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Добавить все srpm образа в отслеживание продукта."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        p = _require_product(conn, product)
        try:
            report = products.track_from_image(
                conn, client, p, uuid,
                watch_errata=errata, watch_maintainer=maintainer, watch_tasks=tasks,
                progress=_say,
            )
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    _print_track_report(report)


@image_app.command("update")
def image_update(
    product: str = typer.Option(..., "--product", "-p", help="Продукт (ID или название)."),
    uuid: Optional[str] = typer.Argument(None, help="UUID нового образа (или --latest)."),
    latest: bool = typer.Option(False, "--latest", help="Взять последний release-образ продукта из каталога."),
    errata: bool = typer.Option(False, "--errata"),
    maintainer: bool = typer.Option(False, "--maintainer"),
    tasks: bool = typer.Option(False, "--tasks"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Обновить состав продукта по образу (diff: +новые, −пауза)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        p = _require_product(conn, product)
        if latest or not uuid:
            if not uuid and not latest:
                _fail("укажите UUID образа или --latest")
            _ensure_catalog(conn, client)
            row = images.latest_release_image(
                conn, branch=p.branch, edition=p.edition, arch=p.arch
            )
            if row is None:
                _fail(f"для продукта {p.title} не найден release-образ в каталоге")
            uuid = str(row["uuid"])
            _say(f"выбран образ {uuid[:8]} ({row['date'] or '?'}, {row['file'] or ''})")
        try:
            report = products.update_from_image(
                conn, client, p, str(uuid),
                watch_errata=errata, watch_maintainer=maintainer, watch_tasks=tasks,
                progress=_say,
            )
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    _print_track_report(report)


# -- lists -------------------------------------------------------------------
@list_app.command("save-file")
def list_save_file(
    file: str = typer.Argument(..., help="Файл со списком rpm (или - для stdin)."),
    title: str = typer.Option("", "--title", "-t"),
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    branch: Optional[str] = typer.Option(
        None, "--branch", "-b", help="Ветка для маппинга бинарников в srpm."
    ),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сохранить список из файла или stdin."""
    cfg = _cfg(db)
    text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8", errors="replace")
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        product_id = _require_product(conn, product).id if product else None
        try:
            list_obj, report = lists.save_file_list(
                conn, text=text, title=title or Path(file).name,
                product_id=product_id, source_branch=branch, client=client, progress=_say,
            )
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(
        f"[green]список #{list_obj.id}[/green] «{list_obj.title}»: "
        f"разобрано {report['parsed']}, маппинг в srpm {report['mapped']}"
    )
    if report["bad_count"]:
        err_console.print(f"не разобрано строк: {report['bad_count']} (например: {report['bad'][:3]})")
    console.print(f"сравнить: alttrack compare run --left {list_obj.id} --right <другой id>")


@list_app.command("save-image")
def list_save_image(
    uuid: str = typer.Argument(..., help="UUID образа."),
    title: str = typer.Option("", "--title", "-t"),
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сохранить состав образа как список."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        _ensure_catalog(conn, client)
        product_id = _require_product(conn, product).id if product else None
        try:
            list_obj, report = lists.save_image_list(
                conn, client, uuid=uuid, title=title or None,
                product_id=product_id, progress=_say,
            )
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(f"[green]список #{list_obj.id}[/green] «{list_obj.title}»: {report['parsed']} пакетов")


@list_app.command("save-branch")
def list_save_branch(
    branch: str = typer.Argument(..., help="Ветка репозитория (p11, sisyphus…)."),
    arch: Optional[list[str]] = typer.Option(
        None, "--arch", "-a",
        help="Архитектура; флаг можно повторять (по умолчанию все). "
             "noarch — пакеты без привязки к архитектуре: добавляйте явно, "
             "иначе срез их не включит.",
    ),
    title: str = typer.Option("", "--title", "-t"),
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сохранить содержимое ветки репозитория как список."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        product_id = _require_product(conn, product).id if product else None
        try:
            list_obj, report = lists.save_branch_list(
                conn, client, branch=branch, arch=arch,
                title=title or None, product_id=product_id, progress=_say,
            )
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(
        f"[green]список #{list_obj.id}[/green] «{list_obj.title}»: "
        f"{report['parsed']} пакетов (srpm: {report['mapped']})"
    )


@list_app.command("ls")
def list_ls(
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сохранённые списки."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        product_id = _require_product(conn, product).id if product else None
        rows = lists.list_lists(conn, product_id=product_id)
        table = Table(title=f"Списки ({len(rows)})")
        for column in ("ID", "Название", "Тип", "Пакетов", "Кривых", "Создан"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                str(row.id), row.title[:44], row.kind, str(row.item_count),
                str(row.bad_lines) or "", row.created_at[:16].replace("T", " "),
            )
        console.print(table)
        if not rows:
            console.print("[dim]списков нет — alttrack list save-file <файл>[/dim]")
    finally:
        conn.close()


@list_app.command("show")
def list_show(
    list_id: int = typer.Argument(..., help="ID списка."),
    limit: int = typer.Option(30, "--limit", "-n"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Показать содержимое списка."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            list_obj = lists.require_list(conn, list_id)
        except KeyError:
            _fail(f"список {list_id} не найден")
        console.print(f"[bold]#{list_obj.id}[/bold] {list_obj.title} ({list_obj.kind}, {list_obj.item_count} пакетов)")
        items = lists.list_items(conn, list_obj.id)
        table = Table()
        for column in ("Пакет", "Версия", "Релиз", "Арх.", "srpm"):
            table.add_column(column)
        for item in items[:limit]:
            table.add_row(item["name"], item["version"], item["release"], item["arch"] or "—",
                          item["source_name"] or "—")
        console.print(table)
        if len(items) > limit:
            _say(f"… ещё {len(items) - limit}")
    finally:
        conn.close()


@list_app.command("rm")
def list_rm(
    list_id: int = typer.Argument(..., help="ID списка."),
    yes: bool = typer.Option(False, "--yes", "-y"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Удалить список (и сравнения, где он участвует)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            list_obj = lists.require_list(conn, list_id)
        except KeyError:
            _fail(f"список {list_id} не найден")
        if not yes and not typer.confirm(f"Удалить список #{list_obj.id} «{list_obj.title}»?"):
            raise typer.Abort()
        lists.delete_list(conn, list_obj.id)
    finally:
        conn.close()
    console.print("[green]удалено:[/green] список и связанные сравнения")


# -- compare -----------------------------------------------------------------
def _print_report(result: dict[str, Any], *, limit: int = 30) -> None:
    stats = result["stats"]
    console.print(
        f"итого {result['total']}: "
        f"[green]++ {stats[compare.LEFT_ONLY]}[/green], "
        f"[red]-- {stats[compare.RIGHT_ONLY]}[/red], "
        f"[yellow]>> {stats[compare.CHANGED]}[/yellow], "
        f"== {stats[compare.SAME]}"
    )
    interesting = [
        r for r in result["rows"] if r["status"] != compare.SAME
    ]
    if not interesting:
        return
    table = Table(title="Отличия")
    for column in ("Статус", "Пакет", "Слева", "Справа", "srpm", "Группа"):
        table.add_column(column)
    for row in interesting[:limit]:
        left = row["left"]
        right = row["right"]
        table.add_row(
            compare.STATUS_LABELS[row["status"]],
            row["name"],
            f"{left['version']}-{left['release']}" if left else "—",
            f"{right['version']}-{right['release']}" if right else "—",
            row["source"] or "—",
            row["category"] or "—",
        )
    console.print(table)
    if len(interesting) > limit:
        _say(f"… ещё {len(interesting) - limit} (alttrack compare show <id> --limit …)")


@compare_app.command("run")
def compare_run(
    left: int = typer.Option(..., "--left", "-l", help="ID левого списка."),
    right: int = typer.Option(..., "--right", "-r", help="ID правого списка."),
    title: str = typer.Option("", "--title", "-t"),
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    limit: int = typer.Option(30, "--limit", "-n"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Сравнить два списка и сохранить сравнение в историю."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        product_id = _require_product(conn, product).id if product else None
        try:
            cmp_obj = compare.save(
                conn, left_id=left, right_id=right, title=title, product_id=product_id
            )
        except KeyError as exc:
            _fail(str(exc))
        result = compare.report(conn, cmp_obj)
    finally:
        conn.close()
    console.print(f"[bold]сравнение #{cmp_obj.id}:[/bold] {cmp_obj.title}")
    _print_report(result, limit=limit)


@compare_app.command("history")
def compare_history(
    product: Optional[str] = typer.Option(None, "--product", "-p"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """История сравнений."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        product_id = _require_product(conn, product).id if product else None
        rows = compare.list_comparisons(conn, product_id=product_id)
        table = Table(title=f"Сравнения ({len(rows)})")
        for column in ("ID", "Название", "++", "--", ">>", "==", "Создано"):
            table.add_column(column)
        for row in rows:
            stats = row.stats
            table.add_row(
                str(row.id), row.title[:50],
                str(stats.get(compare.LEFT_ONLY, 0)), str(stats.get(compare.RIGHT_ONLY, 0)),
                str(stats.get(compare.CHANGED, 0)), str(stats.get(compare.SAME, 0)),
                row.created_at[:16].replace("T", " "),
            )
        console.print(table)
        if not rows:
            console.print("[dim]сравнений нет — alttrack compare run -l <id> -r <id>[/dim]")
    finally:
        conn.close()


@compare_app.command("show")
def compare_show(
    cmp_id: int = typer.Argument(..., help="ID сравнения."),
    status: Optional[str] = typer.Option(
        None, "--status", "-s", help=f"Только группа: {', '.join(compare.STATUSES)}"
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
    extra: bool = typer.Option(
        False, "--extra",
        help="Показать и пакеты репозитория, отсутствующие в образе (правая сторона — ветка).",
    ),
    csv_out: Optional[str] = typer.Option(
        None, "--csv", help="Записать отчёт в CSV (файл, или - для stdout)."
    ),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Отчёт по сравнению (пересчитывается на лету)."""
    if status and status not in compare.STATUSES:
        _fail(f"статус должен быть одним из: {', '.join(compare.STATUSES)}")
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            cmp_obj = compare.require_comparison(conn, cmp_id)
        except KeyError:
            _fail(f"сравнение {cmp_id} не найдено")
        result = compare.report(conn, cmp_obj, include_right_extra=extra)
    finally:
        conn.close()

    if csv_out:
        payload = compare.to_csv(result["rows"], status=status)
        if csv_out == "-":
            sys.stdout.write(payload)
        else:
            Path(csv_out).write_text(payload, encoding="utf-8")
            console.print(f"[green]CSV записан:[/green] {csv_out}")
        return
    console.print(f"[bold]#{cmp_obj.id}[/bold] {cmp_obj.title}")
    if result.get("hidden_right_only"):
        console.print(
            f"[dim]скрыто {result['hidden_right_only']} пакетов репозитория, "
            "отсутствующих в образе (--extra для показа)[/dim]"
        )
    if status:
        rows = [r for r in result["rows"] if r["status"] == status]
        stats = {s: sum(1 for r in rows if r["status"] == s) for s in compare.STATUSES}
        result = dict(result, rows=rows, stats=stats, total=len(rows))
    _print_report(result, limit=limit)


@compare_app.command("rm")
def compare_rm(
    cmp_id: int = typer.Argument(..., help="ID сравнения."),
    yes: bool = typer.Option(False, "--yes", "-y"),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Удалить сравнение из истории."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        try:
            cmp_obj = compare.require_comparison(conn, cmp_id)
        except KeyError:
            _fail(f"сравнение {cmp_id} не найдено")
        if not yes and not typer.confirm(f"Удалить сравнение #{cmp_obj.id}?"):
            raise typer.Abort()
        compare.delete_comparison(conn, cmp_obj.id)
    finally:
        conn.close()
    console.print("[green]удалено:[/green] сравнение")


# -- meta --------------------------------------------------------------------
@meta_app.command("sync")
def meta_sync(
    branch: str = typer.Option(..., "--branch", "-b", help="Ветка (p11, sisyphus…)."),
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Обновить справочник пакетов ветки (summary/группа/сопровождающий)."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        report = metasync.sync(conn, client, branch=branch)
    except ApiError as exc:
        _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(
        f"[green]справочник обновлён:[/green] {report['branch']} — {report['count']} пакетов "
        f"({report['synced_at']})"
    )


@meta_app.command("status")
def meta_status(
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Состояние справочника по веткам."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    try:
        rows = metasync.status(conn)
        table = Table(title="Справочник package_meta")
        for column in ("Ветка", "Тип", "Пакетов", "Обновлён"):
            table.add_column(column)
        for row in rows:
            table.add_row(row["branch"], row["kind"], str(row["count"]),
                          (row["synced_at"] or "—").replace("T", " ")[:19])
        console.print(table)
        if not rows:
            console.print("[dim]справочник пуст — заполнится при первом добавлении образа[/dim]")

        ref_rows = refs.status(conn)
        if ref_rows:
            table = Table(title="Справочники: ветки, архитектуры и группы ПО")
            for column in ("Справочник", "Строк", "Обновлён"):
                table.add_column(column)
            labels = {"branch": "ветки", "arch": "архитектуры", "category": "группы ПО"}
            for row in ref_rows:
                table.add_row(
                    labels.get(row["kind"], row["kind"]),
                    str(row["count"]),
                    (row["synced_at"] or "—").replace("T", " ")[:19],
                )
            console.print(table)
        else:
            console.print(
                "[dim]справочники не загружены — alttrack meta refs[/dim]"
            )
    finally:
        conn.close()


@meta_app.command("refs")
def meta_refs(
    db: Optional[Path] = DB_OPTION,
) -> None:
    """Обновить справочники веток, архитектур и групп ПО из rdb."""
    cfg = _cfg(db)
    conn = open_db(cfg.db_path)
    client = _client(cfg)
    try:
        try:
            report = refs.sync(conn, client, progress=_say)
        except ApiError as exc:
            _fail(f"ошибка API: {exc}")
    finally:
        client.close()
        conn.close()
    console.print(
        f"[green]справочники обновлены:[/green] веток {report['branch']}, "
        f"архитектур {report['arch']}, групп ПО {report['category']} "
        f"({report['synced_at']})"
    )


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
