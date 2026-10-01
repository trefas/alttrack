"""Web dashboard (FastAPI).

The dashboard duplicates every CLI command: CRUD for tracked packages,
journal browsing, full-text search, archiving, statistics, build tasks and
manual refresh.  All handlers call the same service layer as the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from anyio import to_thread

from .. import archive, journal, refresh, tasks, watchlist
from ..api import ALTRepoClient, ApiError, PackageNotFound
from ..config import Config, write_toml
from ..db import open_db
from ..models import EVENT_LABELS, EVENT_TYPES, TASK_EVENT_TYPES

log = logging.getLogger("alttrack.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _fmt_dt(value: Any) -> str:
    if not value:
        return "—"
    return str(value).replace("T", " ").replace("Z", "")[:19]


def _parse_branches(
    branches: Optional[list[str]], submitted: Optional[str]
) -> Optional[list[str]]:
    """Normalise the ``branches`` form field.

    The form sends one hidden input per selected branch, but a comma-joined
    value (older form, hand-made curl) is tolerated as well.  ``None`` is
    returned only when neither the branch inputs nor the ``branches_submitted``
    marker arrived, i.e. the branch section was not part of the form at all;
    an explicitly empty selection reaches the validators as ``[]`` and is
    rejected there.
    """
    if submitted is None and not branches:
        return None
    out: list[str] = []
    for raw in branches or []:
        for part in str(raw).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def create_app(cfg: Config, *, initial_refresh: bool = True) -> FastAPI:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["dt"] = _fmt_dt
    templates.env.filters["label"] = lambda t: EVENT_LABELS.get(t, t)

    app = FastAPI(title="alttrack", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.refresh_status: dict[str, Any] = {
        "running": False,
        "last": None,
        "initial": True,
    }

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        _app.state.bg_task = asyncio.create_task(refresh_loop())
        try:
            yield
        finally:
            task = getattr(_app.state, "bg_task", None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            client = getattr(_app.state, "shared_client", None)
            if client:
                client.close()

    app.router.lifespan_context = lifespan
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def get_conn() -> Iterator[Any]:
        conn = open_db(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def client_for_request(request: Request) -> ALTRepoClient:
        client = getattr(request.app.state, "shared_client", None)
        if client is None:
            client = ALTRepoClient(
                cfg.api_base_url, timeout=cfg.http_timeout, concurrency=cfg.http_concurrency
            )
            request.app.state.shared_client = client
        return client

    def do_refresh(conn: Any, client: ALTRepoClient, *, do_archive: bool | None = None) -> refresh.RefreshResult:
        status = app.state.refresh_status
        status["running"] = True
        try:
            result = refresh.run_refresh(conn, client, cfg, do_archive=do_archive)
        finally:
            status["running"] = False
            status["initial"] = False
        status["last"] = {
            "run_id": result.run_id,
            "status": result.status,
            "checked": result.checked,
            "events": result.events,
            "message": result.message,
            "errors": result.errors[:10],
            "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return result

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        status = app.state.refresh_status
        return {
            "request": request,
            "cfg": cfg,
            "refresh_running": status["running"],
            "refresh_initial": status["initial"],
            "refresh_last": status["last"],
            **extra,
        }

    # ------------------------------------------------------------------
    # background refresh loop
    # ------------------------------------------------------------------
    async def refresh_loop() -> None:
        # Give the server a moment to start before the initial pass.
        await asyncio.sleep(1.0)
        if initial_refresh:
            await trigger_refresh()
        while True:
            await asyncio.sleep(max(cfg.refresh_interval, 1) * 60)
            await trigger_refresh()

    async def trigger_refresh() -> None:
        if app.state.refresh_status["running"]:
            return

        def _run() -> None:
            conn = open_db(cfg.db_path)
            client = ALTRepoClient(
                cfg.api_base_url, timeout=cfg.http_timeout, concurrency=cfg.http_concurrency
            )
            try:
                do_refresh(conn, client)
            except Exception:  # noqa: BLE001
                log.exception("background refresh failed")
            finally:
                client.close()
                conn.close()

        await to_thread.run_sync(_run)

    # ------------------------------------------------------------------
    # dashboard
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn=Depends(get_conn)) -> Any:
        packages = watchlist.list_packages(conn)
        recent, total = journal.query_events(conn, scope="all", limit=15)
        counters = journal.stats(conn, scope="all")
        task_counters = tasks.task_counters(conn)
        failed, _ = journal.query_events(
            conn, event_types=sorted(TASK_EVENT_TYPES & {"task_failed"}), scope="all", limit=5
        )
        active_tasks, _ = tasks.list_tasks(conn, active=True, limit=8)
        runs = refresh.list_runs(conn, limit=5)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            ctx(
                request,
                packages=packages,
                recent=recent,
                total=total,
                counters=counters,
                task_counters=task_counters,
                failed=failed,
                active_tasks=active_tasks,
                runs=runs,
                last_run=refresh.last_run(conn),
            ),
        )

    # ------------------------------------------------------------------
    # tracked packages CRUD
    # ------------------------------------------------------------------
    @app.get("/packages", response_class=HTMLResponse)
    def packages_list(
        request: Request,
        q: Optional[str] = None,
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        packages = watchlist.list_packages(conn, q=q)
        states: dict[int, dict[str, Any]] = {}
        for pkg in packages:
            snaps = watchlist.snapshot_map(conn, pkg.id)  # type: ignore[arg-type]
            states[pkg.id] = {
                "present": sum(1 for s in snaps.values() if s["present"]),
                "total": len(pkg.branches),
            }
        return templates.TemplateResponse(
            request,
            "packages.html",
            ctx(request, packages=packages, q=q, states=states),
        )

    @app.get("/packages/new", response_class=HTMLResponse)
    def package_new(request: Request, name: Optional[str] = None) -> Any:
        return templates.TemplateResponse(
            request,
            "package_form.html",
            ctx(request, mode="new", pkg=None, name=name or "", report=None, error=None),
        )

    @app.post("/packages")
    def package_create(
        request: Request,
        name: str = Form(...),
        branches: Optional[list[str]] = Form(None),
        branches_submitted: Optional[str] = Form(None),
        note: str = Form(""),
        watch_errata: Optional[str] = Form(None),
        watch_maintainer: Optional[str] = Form(None),
        watch_tasks: Optional[str] = Form(None),
        backfill: Optional[str] = Form(None),
        backfill_limit: int = Form(50),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        try:
            pkg, report = watchlist.add_package(
                conn,
                client,
                name=name.strip(),
                branches=_parse_branches(branches, branches_submitted),
                note=note,
                watch_errata=watch_errata is not None,
                watch_maintainer=watch_maintainer is not None,
                watch_tasks=watch_tasks is not None,
                backfill=backfill is not None,
                backfill_limit=backfill_limit,
            )
        except (watchlist.ValidationError, PackageNotFound, ApiError) as exc:
            return templates.TemplateResponse(
                request,
                "package_form.html",
                ctx(
                    request,
                    mode="new",
                    pkg=None,
                    name=name,
                    report=None,
                    error=str(exc),
                ),
                status_code=400,
            )
        return RedirectResponse(f"/packages/{pkg.id}?created=1", status_code=303)

    @app.get("/packages/{package_id}", response_class=HTMLResponse)
    def package_detail(request: Request, package_id: int, conn=Depends(get_conn)) -> Any:
        pkg = watchlist.get_package(conn, package_id)
        if pkg is None:
            raise HTTPException(404, "пакет не найден")
        snaps = watchlist.snapshot_map(conn, pkg.id)  # type: ignore[arg-type]
        active_branches = _active_branches(conn)
        events, total = journal.query_events(conn, package=pkg.name, scope="all", limit=30)
        pkg_tasks, tasks_total = tasks.list_tasks(conn, package=pkg.name, limit=15)
        present_report = None
        return templates.TemplateResponse(
            request,
            "package_detail.html",
            ctx(
                request,
                pkg=pkg,
                snaps=snaps,
                active_branches=active_branches,
                events=events,
                total=total,
                pkg_tasks=pkg_tasks,
                tasks_total=tasks_total,
                present_report=present_report,
                created=request.query_params.get("created"),
            ),
        )

    @app.get("/packages/{package_id}/edit", response_class=HTMLResponse)
    def package_edit(
        request: Request, package_id: int, conn=Depends(get_conn), client=Depends(client_for_request)
    ) -> Any:
        pkg = watchlist.get_package(conn, package_id)
        if pkg is None:
            raise HTTPException(404, "пакет не найден")
        try:
            report = watchlist.check_branches(client, pkg.name, pkg.branches)
        except ApiError as exc:
            report = {"warnings": [f"не удалось проверить ветки: {exc}"],
                      "ok": [], "missing": [], "inactive": [],
                      "active_branches": [], "present_branches": [],
                      "requested": pkg.branches}
        return templates.TemplateResponse(
            request,
            "package_form.html",
            ctx(request, mode="edit", pkg=pkg, report=report, error=None, name=pkg.name),
        )

    @app.post("/packages/{package_id}")
    def package_update(
        request: Request,
        package_id: int,
        branches: Optional[list[str]] = Form(None),
        branches_submitted: Optional[str] = Form(None),
        note: str = Form(""),
        watch_errata: Optional[str] = Form(None),
        watch_maintainer: Optional[str] = Form(None),
        watch_tasks: Optional[str] = Form(None),
        enabled: Optional[str] = Form(None),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        pkg = watchlist.get_package(conn, package_id)
        if pkg is None:
            raise HTTPException(404, "пакет не найден")
        try:
            updated, report = watchlist.update_package(
                conn,
                client,
                pkg,
                branches=_parse_branches(branches, branches_submitted),
                note=note,
                watch_errata=watch_errata is not None,
                watch_maintainer=watch_maintainer is not None,
                watch_tasks=watch_tasks is not None,
                enabled=enabled is not None,
            )
        except (watchlist.ValidationError, ApiError) as exc:
            return templates.TemplateResponse(
                request,
                "package_form.html",
                ctx(request, mode="edit", pkg=pkg, report=None, error=str(exc), name=pkg.name),
                status_code=400,
            )
        return RedirectResponse(f"/packages/{updated.id}?saved=1", status_code=303)

    @app.post("/packages/{package_id}/delete")
    def package_delete(
        package_id: int,
        purge: Optional[str] = Form(None),
        conn=Depends(get_conn),
    ) -> Any:
        pkg = watchlist.get_package(conn, package_id)
        if pkg is None:
            raise HTTPException(404, "пакет не найден")
        watchlist.delete_package(conn, pkg, purge=purge is not None)
        return RedirectResponse("/packages", status_code=303)

    # ------------------------------------------------------------------
    # journal / search / archive
    # ------------------------------------------------------------------
    @app.get("/journal", response_class=HTMLResponse)
    def journal_page(
        request: Request,
        q: Optional[str] = None,
        package: Optional[str] = None,
        branch: Optional[str] = None,
        event_type: Optional[list[str]] = Query(None),
        scope: str = "all",
        since: Optional[str] = None,
        until: Optional[str] = None,
        page: int = 1,
        conn=Depends(get_conn),
    ) -> Any:
        scope = scope if scope in journal.SCOPES else "all"
        if since and len(since) == 10:
            since = since + "T00:00:00"
        if until and len(until) == 10:
            until = until + "T23:59:59"
        limit = 50
        offset = max(page - 1, 0) * limit
        events, total = journal.query_events(
            conn,
            q=q,
            package=package,
            branch=branch,
            event_types=event_type,
            since=since,
            until=until,
            scope=scope,
            limit=limit,
            offset=offset,
        )
        packages = watchlist.list_packages(conn)
        pages = max((total + limit - 1) // limit, 1)

        def pager_url(p: int) -> str:
            from urllib.parse import urlencode

            params: dict[str, Any] = {"page": p, "scope": scope}
            for key, value in (
                ("q", q),
                ("package", package),
                ("branch", branch),
                ("since", since),
                ("until", until),
            ):
                if value:
                    params[key] = value
            if event_type:
                params["event_type"] = event_type
            return "/journal?" + urlencode(params, doseq=True)

        return templates.TemplateResponse(
            request,
            "journal.html",
            ctx(
                request,
                events=events,
                total=total,
                page=page,
                pages=pages,
                pager_url=pager_url,
                filters={
                    "q": q or "",
                    "package": package or "",
                    "branch": branch or "",
                    "event_type": event_type or [],
                    "scope": scope,
                    "since": since or "",
                    "until": until or "",
                },
                packages=packages,
                event_types=EVENT_TYPES,
                types_present=journal.types_present(conn),
            ),
        )

    @app.get("/archive", response_class=HTMLResponse)
    def archive_page(request: Request, conn=Depends(get_conn)) -> Any:
        live = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
        stored = conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"]
        preview = archive.archive_plan(conn, cfg)
        return templates.TemplateResponse(
            request,
            "archive.html",
            ctx(request, live=int(live), stored=int(stored), preview=len(preview), result=None),
        )

    @app.post("/archive/run")
    def archive_run(
        request: Request,
        older_than: Optional[int] = Form(None),
        max_rows: Optional[int] = Form(None),
        package: Optional[str] = Form(None),
        dry_run: Optional[str] = Form(None),
        conn=Depends(get_conn),
    ) -> Any:
        result = archive.archive_now(
            conn,
            cfg,
            older_than_days=older_than,
            max_live_rows=max_rows,
            package=package or None,
            dry_run=dry_run is not None,
        )
        verb = "будет перенесено" if dry_run else "перенесено"
        result["message"] = (
            f"{verb} {result['moved']} записей; в живом журнале: {result['live_remaining']}, "
            f"в архиве: {result['archive_total']}"
        )
        live = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
        stored = conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"]
        preview = archive.archive_plan(conn, cfg)
        return templates.TemplateResponse(
            request,
            "archive.html",
            ctx(request, live=int(live), stored=int(stored), preview=len(preview), result=result),
        )

    @app.post("/archive/purge")
    def archive_purge(
        request: Request,
        older_than: Optional[int] = Form(None),
        conn=Depends(get_conn),
    ) -> Any:
        result = archive.purge_archive(conn, older_than_days=older_than)
        verb = "будет удалено" if older_than is None else "будет удалено"
        result["message"] = f"{verb} {result['deleted']} записей архива"
        live = conn.execute("SELECT COUNT(*) AS n FROM journal").fetchone()["n"]
        stored = conn.execute("SELECT COUNT(*) AS n FROM journal_archive").fetchone()["n"]
        return templates.TemplateResponse(
            request,
            "archive.html",
            ctx(request, live=int(live), stored=int(stored), preview=0, result=result),
        )

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request, scope: str = "all", conn=Depends(get_conn)) -> Any:
        data = journal.stats(conn, scope=scope)
        return templates.TemplateResponse(
            request,
            "stats.html", ctx(request, data=data, scope=scope, event_labels=EVENT_LABELS)
        )

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(
        request: Request,
        package: Optional[str] = None,
        branch: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        conn=Depends(get_conn),
    ) -> Any:
        limit = 50
        active = True if state == "active" else None
        outcome = "failure" if state == "failed" else None
        if state == "done":
            active = False
        rows, total = tasks.list_tasks(
            conn,
            package=package,
            branch=branch,
            active=active,
            outcome=outcome,
            limit=limit,
            offset=max(page - 1, 0) * limit,
        )
        pages = max((total + limit - 1) // limit, 1)
        return templates.TemplateResponse(
            request,
            "tasks.html",
            ctx(
                request,
                rows=rows,
                total=total,
                page=page,
                pages=pages,
                counters=tasks.task_counters(conn),
                filters={"package": package or "", "branch": branch or "", "state": state or ""},
                packages=watchlist.list_packages(conn),
            ),
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(
        request: Request, task_id: int, branch: Optional[str] = None, conn=Depends(get_conn)
    ) -> Any:
        data = tasks.get_task(conn, task_id, branch)
        if data is None:
            raise HTTPException(404, "задание не найдено (отслеживаются только свои пакеты)")
        return templates.TemplateResponse(request, "task_detail.html", ctx(request, task=data))

    # ------------------------------------------------------------------
    # runs / settings / manual refresh
    # ------------------------------------------------------------------
    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, conn=Depends(get_conn)) -> Any:
        return templates.TemplateResponse(
            request,
            "runs.html", ctx(request, rows=refresh.list_runs(conn, limit=50))
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "settings.html", ctx(request, saved=request.query_params.get("saved"))
        )

    @app.post("/settings")
    def settings_save(
        refresh_interval: Optional[int] = Form(None),
        task_history_days: Optional[int] = Form(None),
        archive_after_days: Optional[int] = Form(None),
        max_live_rows: Optional[int] = Form(None),
        auto_archive: Optional[str] = Form(None),
        auto_archive_managed: Optional[str] = Form(None),
        backfill_limit: Optional[int] = Form(None),
    ) -> Any:
        # The two cards on /settings submit only their own fields, so every
        # value is optional and only the provided ones are updated.
        if auto_archive_managed is not None:
            cfg.auto_archive = auto_archive is not None
        updates = {
            "refresh_interval": (refresh_interval, 1),
            "task_history_days": (task_history_days, 1),
            "archive_after_days": (archive_after_days, 1),
            "max_live_rows": (max_live_rows, 100),
            "backfill_limit": (backfill_limit, 0),
        }
        for attr, (value, low) in updates.items():
            if value is not None:
                setattr(cfg, attr, max(low, value))
        # A checkbox that was unchecked simply does not arrive when the form
        # does not contain it; only the archiving card toggles auto_archive.
        with contextlib.suppress(OSError):
            write_toml(cfg)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/refresh")
    async def refresh_now() -> Any:
        if app.state.refresh_status["running"]:
            return JSONResponse({"status": "busy"}, status_code=409)
        await trigger_refresh()
        return JSONResponse({"status": "started"})

    @app.get("/api/refresh/status")
    def refresh_status() -> Any:
        return JSONResponse(app.state.refresh_status)

    # ------------------------------------------------------------------
    # API used by the forms (also used by the CLI validation logic)
    # ------------------------------------------------------------------
    @app.get("/api/autocomplete")
    def autocomplete(q: str = "", conn=Depends(get_conn), client=Depends(client_for_request)) -> Any:
        del conn
        return JSONResponse(watchlist.autocomplete(client, q))

    @app.get("/api/packages/check")
    def package_check(
        name: str = "", branches: str = "", client=Depends(client_for_request)
    ) -> Any:
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        requested = [b.strip() for b in branches.split(",") if b.strip()]
        try:
            report = watchlist.check_branches(client, name, requested)
        except ApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(report)

    @app.get("/api/branches")
    def branches(client=Depends(client_for_request)) -> Any:
        try:
            return JSONResponse(client.active_packagesets())
        except ApiError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/healthz")
    def healthz() -> Any:
        return {"status": "ok", "db": str(cfg.db_path)}

    return app


def _active_branches(conn: Any) -> list[str]:
    """Cached list of published packagesets (may be empty when offline)."""
    row = conn.execute("SELECT value FROM meta WHERE key='active_branches'").fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return []
    return []
