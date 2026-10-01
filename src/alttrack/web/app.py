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

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from anyio import to_thread
from urllib.parse import urlencode

from .. import archive, compare, images, journal, lists, metasync, products, refs, refresh, tasks, watchlist
from ..api import ALTRepoClient, ApiError, PackageNotFound
from ..config import Config, write_toml
from ..db import open_db
from ..models import EVENT_LABELS, EVENT_TYPES, TASK_EVENT_TYPES

log = logging.getLogger("alttrack.web")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Report page sizes offered on /compare/{id} (see the size selector).
COMPARE_PAGE_SIZES: tuple[int, ...] = (10, 50, 100)


def _page_window(page: int, count: int, width: int = 2) -> list[Any]:
    """Compact pagination: first, last and ±``width`` pages around current."""
    pages = {1, count, *range(max(1, page - width), min(count, page + width) + 1)}
    out: list[Any] = []
    prev: int | None = None
    for p in sorted(pages):
        if prev is not None and p > prev + 1:
            out.append("…")
        out.append(p)
        prev = p
    return out


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

    app = FastAPI(title="AltTrack", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.refresh_status: dict[str, Any] = {
        "running": False,
        "last": None,
        "initial": True,
    }
    # Background jobs (mass add / diff update): name -> status dict, the same
    # pattern as refresh_status; the UI polls /api/jobs.
    app.state.jobs: dict[str, dict[str, Any]] = {}

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

    def _job(name: str) -> dict[str, Any]:
        return app.state.jobs.setdefault(
            name,
            {"running": False, "stage": "", "error": None, "last": None, "finished_at": None},
        )

    def run_job(name: str, fn: Any) -> bool:
        """Run ``fn(conn, client, progress)`` in a background thread.

        Returns False when a job with this name is already running.
        """
        job = _job(name)
        if job["running"]:
            return False
        job.update(running=True, stage="запуск…", error=None)

        def _run() -> None:
            conn = open_db(cfg.db_path)
            client = getattr(app.state, "shared_client", None)
            owned = client is None
            if owned:
                client = ALTRepoClient(
                    cfg.api_base_url, timeout=cfg.http_timeout, concurrency=cfg.http_concurrency
                )
            try:
                job["last"] = fn(conn, client, lambda stage: job.update(stage=stage))
                job["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception as exc:  # noqa: BLE001
                log.exception("background job %s failed", name)
                job["error"] = str(exc)
            finally:
                if owned:
                    client.close()
                conn.close()
                job.update(running=False, stage="")

        threading.Thread(target=_run, daemon=True, name=f"job-{name}").start()
        return True

    def current_product(conn: Any, request: Request) -> Optional[products.Product]:
        """Product selected in the header (cookie); a single-product install
        defaults to its only product."""
        all_products = products.list_products(conn)
        raw = request.cookies.get("product_id")
        if raw == "0":
            return None
        if raw and raw.isdigit():
            for p in all_products:
                if str(p.id) == raw:
                    return p
        if len(all_products) == 1:
            return all_products[0]
        return None

    def product_names(conn: Any, product: Optional[products.Product]) -> list[str]:
        if product is None:
            return []
        return [
            str(r["name"])
            for r in conn.execute(
                "SELECT t.name FROM product_packages pp "
                "JOIN tracked_packages t ON t.id = pp.package_id "
                "WHERE pp.product_id = ?",
                (product.id,),
            )
        ]

    def product_ids(conn: Any, product: Optional[products.Product]) -> list[int]:
        if product is None:
            return []
        return [
            int(r["package_id"])
            for r in conn.execute(
                "SELECT package_id FROM product_packages WHERE product_id = ?",
                (product.id,),
            )
        ]

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        status = app.state.refresh_status
        conn = open_db(cfg.db_path)
        try:
            all_products = products.list_products(conn)
            product = current_product(conn, request)
        finally:
            conn.close()
        jobs = [j for j in app.state.jobs.values() if j.get("running")]
        last_job = None
        for j in app.state.jobs.values():
            if j.get("finished_at") and not j.get("running"):
                if last_job is None or str(j["finished_at"]) > str(last_job["finished_at"]):
                    last_job = j
        return {
            "request": request,
            "cfg": cfg,
            "refresh_running": status["running"],
            "refresh_initial": status["initial"],
            "refresh_last": status["last"],
            "products": all_products,
            "current_product": product,
            "job_running": bool(jobs),
            "job_stage": jobs[0]["stage"] if jobs else "",
            "last_job": last_job,
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
        product = current_product(conn, request)
        names = product_names(conn, product)
        packages = watchlist.list_packages(conn)
        recent, total = journal.query_events(
            conn, scope="all", limit=15, packages=names or None
        )
        counters = journal.stats(conn, scope="all")
        task_counters = tasks.task_counters(conn)
        failed, _ = journal.query_events(
            conn,
            event_types=sorted(TASK_EVENT_TYPES & {"task_failed"}),
            scope="all",
            limit=5,
            packages=names or None,
        )
        active_tasks, _ = tasks.list_tasks(
            conn, active=True, limit=8, package_ids=product_ids(conn, product) or None
        )
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
        backfill_job = app.state.jobs.get(f"backfill:{pkg.id}")
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
                backfill_limit=cfg.backfill_limit,
                backfill_job=backfill_job,
                busy=request.query_params.get("busy"),
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
        product = current_product(conn, request)
        events, total = journal.query_events(
            conn,
            q=q,
            package=package,
            packages=product_names(conn, product) or None,
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
            package_ids=product_ids(conn, current_product(conn, request)) or None,
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
    # products
    # ------------------------------------------------------------------
    @app.get("/products", response_class=HTMLResponse)
    def products_page(request: Request, conn=Depends(get_conn)) -> Any:
        cards = []
        for p in products.list_products(conn):
            cards.append(
                {
                    "product": p,
                    "counts": products.product_counts(conn, p),
                    "images": len(products.product_images(conn, p)),
                    "comparisons": len(compare.list_comparisons(conn, product_id=p.id)),
                }
            )
        return templates.TemplateResponse(
            request,
            "products.html",
            ctx(
                request,
                cards=cards,
                arch_list=refs.branch_arch_options(conn),
                error=request.query_params.get("error"),
            ),
        )

    @app.post("/products")
    def product_create_route(
        title: str = Form(...),
        branch: str = Form(...),
        edition: str = Form(""),
        arch: str = Form("x86_64"),
        conn=Depends(get_conn),
    ) -> Any:
        from urllib.parse import quote

        try:
            p = products.create_product(
                conn, title=title, branch=branch, edition=edition.strip(), arch=arch.strip()
            )
        except ValueError as exc:
            return RedirectResponse(f"/products?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/products/{p.id}", status_code=303)

    @app.post("/products/select")
    def product_select(product_id: str = Form(""), referer: str = Form("")) -> Any:
        target = referer if referer.startswith("/") else "/"
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("product_id", product_id, max_age=60 * 60 * 24 * 365, samesite="lax")
        return resp

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    def product_detail(
        request: Request,
        product_id: int,
        q: Optional[str] = None,
        state: Optional[str] = None,
        preview: Optional[str] = None,
        busy: Optional[str] = None,
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        p = products.get_product(conn, product_id)
        if p is None:
            raise HTTPException(404, "продукт не найден")

        # Membership table (filtered, capped for page size).
        where = ["pp.product_id = ?"]
        args: list[Any] = [p.id]
        if q:
            where.append("t.name LIKE ?")
            args.append(f"%{q}%")
        if state == "active":
            where.append("pp.paused_by_image = 0 AND t.enabled = 1")
        elif state == "paused":
            where.append("(pp.paused_by_image = 1 OR t.enabled = 0)")
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT t.id, t.name, t.branches, t.enabled, t.note, t.watch_errata, "
                "t.watch_maintainer, t.watch_tasks, t.last_checked_at, "
                "pp.added_by, pp.paused_by_image "
                "FROM product_packages pp JOIN tracked_packages t ON t.id = pp.package_id "
                f"WHERE {' AND '.join(where)} ORDER BY t.name COLLATE NOCASE LIMIT 300",
                args,
            )
        ]
        for m in members:
            try:
                m["branch_list"] = json.loads(m["branches"] or "[]")
            except (TypeError, ValueError):
                m["branch_list"] = []
        total_members = products.product_counts(conn, p)

        if images.catalog_is_empty(conn):
            with contextlib.suppress(ApiError):
                images.refresh_catalog(conn, client)
        catalog_rows = images.catalog(
            conn,
            branch=p.branch,
            edition=p.edition or None,
            arch=p.arch or None,
            limit=40,
        )

        preview_report = None
        preview_error = None
        if preview:
            try:
                preview_report = products.preview_from_image(conn, client, p, preview)
            except ApiError as exc:
                preview_error = str(exc)

        job_report = None
        for key in (f"track:{p.id}", f"update:{p.id}", f"meta:{p.id}"):
            job = app.state.jobs.get(key)
            if job and (job.get("last") is not None or job.get("error")):
                job_report = {"name": key, **job}
                break

        comparisons = compare.list_comparisons(conn, product_id=p.id)
        cmp_right_kinds: dict[int, str] = {}
        for c in comparisons:
            right_list = lists.get_list(conn, c.right_list_id)
            cmp_right_kinds[c.id] = right_list.kind if right_list else ""

        return templates.TemplateResponse(
            request,
            "product_detail.html",
            ctx(
                request,
                p=p,
                members=members,
                counts=total_members,
                product_images=products.product_images(conn, p),
                comparisons=comparisons,
                cmp_right_kinds=cmp_right_kinds,
                catalog=catalog_rows,
                q=q or "",
                state=state or "",
                preview=preview_report,
                preview_error=preview_error,
                preview_uuid=preview or "",
                job_report=job_report,
                busy=bool(busy),
                backfill_limit=cfg.backfill_limit,
            ),
        )

    @app.post("/products/{product_id}/delete")
    def product_delete(product_id: int, conn=Depends(get_conn)) -> Any:
        p = products.get_product(conn, product_id)
        if p is None:
            raise HTTPException(404, "продукт не найден")
        products.delete_product(conn, p)
        resp = RedirectResponse("/products", status_code=303)
        resp.delete_cookie("product_id")
        return resp

    @app.post("/products/{product_id}/track")
    def product_track(
        product_id: int,
        uuid: str = Form(...),
        errata: Optional[str] = Form(None),
        maintainer: Optional[str] = Form(None),
        tasks_watch: Optional[str] = Form(None),
        conn=Depends(get_conn),
    ) -> Any:
        p = products.get_product(conn, product_id)
        if p is None:
            raise HTTPException(404, "продукт не найден")

        def _run(conn2: Any, client: ALTRepoClient, progress: Any) -> Any:
            return products.track_from_image(
                conn2, client, p, uuid,
                watch_errata=errata is not None,
                watch_maintainer=maintainer is not None,
                watch_tasks=tasks_watch is not None,
                progress=progress,
            )

        started = run_job(f"track:{product_id}", _run)
        return RedirectResponse(
            f"/products/{product_id}" if started else f"/products/{product_id}?busy=1",
            status_code=303,
        )

    @app.post("/products/{product_id}/update")
    def product_update(
        product_id: int,
        uuid: str = Form(""),
        latest: Optional[str] = Form(None),
        errata: Optional[str] = Form(None),
        maintainer: Optional[str] = Form(None),
        tasks_watch: Optional[str] = Form(None),
        conn=Depends(get_conn),
    ) -> Any:
        p = products.get_product(conn, product_id)
        if p is None:
            raise HTTPException(404, "продукт не найден")

        def _run(conn2: Any, client: ALTRepoClient, progress: Any) -> Any:
            use_uuid = uuid
            if not use_uuid or latest is not None:
                if images.catalog_is_empty(conn2):
                    images.refresh_catalog(conn2, client)
                row = images.latest_release_image(
                    conn2, branch=p.branch, edition=p.edition, arch=p.arch
                )
                if row is None:
                    raise ApiError(
                        f"в каталоге нет release-образа {p.branch}/{p.edition}/{p.arch}"
                    )
                use_uuid = str(row["uuid"])
            return products.update_from_image(
                conn2, client, p, use_uuid,
                watch_errata=errata is not None,
                watch_maintainer=maintainer is not None,
                watch_tasks=tasks_watch is not None,
                progress=progress,
            )

        started = run_job(f"update:{product_id}", _run)
        return RedirectResponse(
            f"/products/{product_id}" if started else f"/products/{product_id}?busy=1",
            status_code=303,
        )

    @app.post("/products/{product_id}/meta-sync")
    def product_meta_sync(product_id: int, conn=Depends(get_conn)) -> Any:
        p = products.get_product(conn, product_id)
        if p is None:
            raise HTTPException(404, "продукт не найден")

        def _run(conn2: Any, client: ALTRepoClient, progress: Any) -> Any:
            progress("загрузка справочника…")
            return metasync.sync(conn2, client, branch=p.branch)

        started = run_job(f"meta:{product_id}", _run)
        return RedirectResponse(
            f"/products/{product_id}" if started else f"/products/{product_id}?busy=1",
            status_code=303,
        )

    # ------------------------------------------------------------------
    # image catalog
    # ------------------------------------------------------------------
    @app.get("/images", response_class=HTMLResponse)
    def images_page(
        request: Request,
        branch: Optional[str] = None,
        edition: Optional[str] = None,
        arch: Optional[str] = None,
        release: Optional[str] = None,
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        error = None
        if images.catalog_is_empty(conn):
            try:
                images.refresh_catalog(conn, client)
            except ApiError as exc:
                error = f"не удалось загрузить каталог: {exc}"
        rows = images.catalog(
            conn, branch=branch, edition=edition, arch=arch, release=release, limit=200
        )
        opts = images.filter_options(conn)
        # Architectures come from the rdb reference list (Settings → Справочники):
        # the catalog alone only shows arches of images that already exist.
        opts["archs"] = refs.branch_arch_options(conn)
        return templates.TemplateResponse(
            request,
            "images.html",
            ctx(
                request,
                rows=rows,
                opts=opts,
                filters={"branch": branch or "", "edition": edition or "",
                         "arch": arch or "", "release": release or ""},
                error=error or request.query_params.get("error"),
                synced=conn.execute(
                    "SELECT MAX(synced_at) AS s FROM image_catalog"
                ).fetchone()["s"],
            ),
        )

    @app.post("/images/refresh")
    def images_refresh(client=Depends(client_for_request)) -> Any:
        from urllib.parse import quote

        conn = open_db(cfg.db_path)
        try:
            try:
                images.refresh_catalog(conn, client)
            except ApiError as exc:
                return RedirectResponse(f"/images?error={quote(str(exc))}", status_code=303)
        finally:
            conn.close()
        return RedirectResponse("/images", status_code=303)

    # ------------------------------------------------------------------
    # saved lists
    # ------------------------------------------------------------------
    @app.get("/lists", response_class=HTMLResponse)
    def lists_page(
        request: Request,
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        product = current_product(conn, request)
        created = request.query_params.get("created")
        error = request.query_params.get("error")
        # The image selector needs the catalog: load it on first visit,
        # same as the /images page does.
        if images.catalog_is_empty(conn):
            try:
                images.refresh_catalog(conn, client)
            except ApiError as exc:
                error = error or f"не удалось загрузить каталог образов: {exc}"
        catalog_rows = images.catalog(conn, limit=5000)
        by_uuid = {str(r["uuid"]): r for r in catalog_rows}
        bound = []
        if product:
            bound = [
                by_uuid[uid]
                for uid in (
                    str(r["image_uuid"])
                    for r in products.product_images(conn, product)
                )
                if uid in by_uuid
            ]
        bound_ids = {str(r["uuid"]) for r in bound}
        others = [r for r in catalog_rows if str(r["uuid"]) not in bound_ids]
        return templates.TemplateResponse(
            request,
            "lists.html",
            ctx(
                request,
                rows=lists.list_lists(conn),
                product=product,
                arch_opts=refs.branch_arch_options(conn),
                bound_images=bound,
                catalog_images=others,
                created=int(created) if created and created.isdigit() else None,
                error=error,
            ),
        )

    @app.post("/lists/file")
    async def list_upload(
        file: Optional[UploadFile] = File(None),
        text: str = Form(""),
        title: str = Form(""),
        branch: str = Form(""),
        product_id: str = Form(""),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        from urllib.parse import quote

        content = ""
        name = ""
        if file is not None and file.filename:
            raw = await file.read()
            content = raw.decode("utf-8", errors="replace")
            name = file.filename
        elif text.strip():
            content = text
        if not content.strip():
            return RedirectResponse("/lists?error=пустой%20файл", status_code=303)

        pid: Optional[int] = None
        if product_id.isdigit():
            pid = int(product_id)
        elif product_id:
            p = products.get_product(conn, product_id)
            pid = p.id if p else None
        list_obj, _report = lists.save_file_list(
            conn,
            text=content,
            title=title or name,
            product_id=pid,
            source_branch=branch or None,
            client=client,
        )
        return RedirectResponse(f"/lists?created={list_obj.id}", status_code=303)

    @app.post("/lists/image")
    def list_from_image(
        request: Request,
        uuid: str = Form(...),
        title: str = Form(""),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        from urllib.parse import quote

        product = current_product(conn, request)
        try:
            list_obj, _r = lists.save_image_list(
                conn, client, uuid=uuid, title=title or None,
                product_id=product.id if product else None,
            )
        except ApiError as exc:
            return RedirectResponse(f"/lists?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/lists?created={list_obj.id}", status_code=303)

    @app.post("/lists/branch")
    def list_from_branch(
        request: Request,
        branch: str = Form(...),
        arch: Optional[list[str]] = Form(None),
        title: str = Form(""),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        from urllib.parse import quote

        product = current_product(conn, request)
        selected = [a for a in (arch or []) if a]
        try:
            list_obj, _r = lists.save_branch_list(
                conn, client, branch=branch, arch=selected or None,
                title=title or None,
                product_id=product.id if product else None,
            )
        except ApiError as exc:
            return RedirectResponse(f"/lists?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/lists?created={list_obj.id}", status_code=303)

    @app.post("/lists/{list_id}/delete")
    def list_delete(list_id: int, conn=Depends(get_conn)) -> Any:
        if lists.get_list(conn, list_id) is None:
            raise HTTPException(404, "список не найден")
        lists.delete_list(conn, list_id)
        return RedirectResponse("/lists", status_code=303)

    # ------------------------------------------------------------------
    # comparison
    # ------------------------------------------------------------------
    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(request: Request, conn=Depends(get_conn)) -> Any:
        product = current_product(conn, request)
        rows = lists.list_lists(conn)
        history = compare.list_comparisons(conn)
        right_kinds: dict[int, str] = {}
        for c in history:
            right_list = lists.get_list(conn, c.right_list_id)
            right_kinds[c.id] = right_list.kind if right_list else ""
        pre_left = request.query_params.get("left", "")
        pre_right = request.query_params.get("right", "")
        return templates.TemplateResponse(
            request,
            "compare.html",
            ctx(
                request,
                rows=rows,
                history=history,
                right_kinds=right_kinds,
                product=product,
                status_labels=compare.STATUS_LABELS,
                pre_left=pre_left,
                pre_right=pre_right,
                error=request.query_params.get("error"),
            ),
        )

    @app.post("/compare/run")
    def compare_run_route(
        left: int = Form(...),
        right: int = Form(...),
        title: str = Form(""),
        product_id: str = Form(""),
        conn=Depends(get_conn),
    ) -> Any:
        pid: Optional[int] = int(product_id) if product_id.isdigit() else None
        try:
            cmp_obj = compare.save(
                conn, left_id=left, right_id=right, title=title, product_id=pid
            )
        except KeyError as exc:
            return RedirectResponse(f"/compare?error={exc}", status_code=303)
        return RedirectResponse(f"/compare/{cmp_obj.id}", status_code=303)

    @app.post("/compare/preset")
    def compare_preset(
        request: Request,
        preset: str = Form(...),
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        """Built-in comparisons for the current product: last release image
        versus the latest uploaded file list (``release-list``) or versus the
        current repository branch (``release-branch``)."""
        from urllib.parse import quote

        product = current_product(conn, request)
        if product is None:
            return RedirectResponse(
                "/compare?error=" + quote("выберите продукт в шапке"), status_code=303
            )
        if images.catalog_is_empty(conn):
            try:
                images.refresh_catalog(conn, client)
            except ApiError as exc:
                return RedirectResponse(
                    f"/compare?error={quote(str(exc))}", status_code=303
                )
        row = images.latest_release_image(
            conn, branch=product.branch, edition=product.edition, arch=product.arch
        )
        if row is None:
            return RedirectResponse(
                "/compare?error=" + quote("нет release-образа продукта в каталоге"),
                status_code=303,
            )
        try:
            left, _r = lists.save_image_list(
                conn, client, uuid=str(row["uuid"]), product_id=product.id
            )
            if preset == "release-branch":
                right, _r = lists.save_branch_list(
                    conn, client, branch=product.branch, arch=product.arch or None,
                    product_id=product.id,
                )
            else:
                file_lists = [l for l in lists.list_lists(conn) if l.kind == "file"]
                if not file_lists:
                    return RedirectResponse(
                        "/compare?error=" + quote("нет загруженных списков (сначала загрузите файл)"),
                        status_code=303,
                    )
                right = file_lists[0]
        except ApiError as exc:
            return RedirectResponse(f"/compare?error={quote(str(exc))}", status_code=303)
        cmp_obj = compare.save(
            conn, left_id=left.id, right_id=right.id, product_id=product.id
        )
        return RedirectResponse(f"/compare/{cmp_obj.id}", status_code=303)

    @app.get("/compare/{cmp_id}", response_class=HTMLResponse)
    def compare_detail(
        request: Request,
        cmp_id: int,
        status: Optional[str] = None,
        csv: Optional[str] = None,
        extra: Optional[str] = None,
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1),
        conn=Depends(get_conn),
    ) -> Any:
        cmp_obj = compare.get_comparison(conn, cmp_id)
        if cmp_obj is None:
            raise HTTPException(404, "сравнение не найдено")
        if status and status not in compare.STATUSES:
            status = None
        if size not in COMPARE_PAGE_SIZES:
            size = COMPARE_PAGE_SIZES[1]
        include_extra = extra == "1"
        result = compare.report(conn, cmp_obj, include_right_extra=include_extra)
        rows = result["rows"]
        if status:
            rows = [r for r in rows if r["status"] == status]

        # pagination: every row is reachable, page size is user-selectable
        total_rows = len(rows)
        page_count = max(1, -(-total_rows // size))  # ceil
        page = min(page, page_count)
        start = (page - 1) * size
        page_rows = rows[start : start + size]

        base: dict[str, Any] = {"size": size}
        if include_extra:
            base["extra"] = "1"
        if status:
            base["status"] = status
        q = urlencode(base)  # full current state
        q_no_status = urlencode({k: v for k, v in base.items() if k != "status"})
        q_no_extra = urlencode({k: v for k, v in base.items() if k != "extra"})

        left_list = lists.get_list(conn, cmp_obj.left_list_id)
        right_list = lists.get_list(conn, cmp_obj.right_list_id)
        return templates.TemplateResponse(
            request,
            "compare_detail.html",
            ctx(
                request,
                cmp=cmp_obj,
                rows=page_rows,
                stats=result["stats"],
                total=result["total"],
                shown=len(page_rows),
                status=status or "",
                include_extra=include_extra,
                hidden_right_only=result["hidden_right_only"],
                page=page,
                page_count=page_count,
                page_window=_page_window(page, page_count),
                page_sizes=COMPARE_PAGE_SIZES,
                size=size,
                total_rows=total_rows,
                row_from=start + 1 if total_rows else 0,
                row_to=start + len(page_rows),
                q=q,
                q_no_status=q_no_status,
                q_no_extra=q_no_extra,
                status_labels=compare.STATUS_LABELS,
                statuses=compare.STATUSES,
                left_list=left_list,
                right_list=right_list,
                product=products.get_product(conn, cmp_obj.product_id)
                if cmp_obj.product_id
                else None,
            ),
        )

    @app.get("/compare/{cmp_id}/export")
    def compare_export(
        cmp_id: int,
        status: Optional[str] = None,
        extra: Optional[str] = None,
        conn=Depends(get_conn),
    ) -> Any:
        cmp_obj = compare.get_comparison(conn, cmp_id)
        if cmp_obj is None:
            raise HTTPException(404, "сравнение не найдено")
        result = compare.report(conn, cmp_obj, include_right_extra=extra == "1")
        payload = compare.to_csv(result["rows"], status=status or None)
        from fastapi.responses import Response

        return Response(
            content=payload,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=alttrack-compare-{cmp_id}.csv"
            },
        )

    @app.post("/compare/{cmp_id}/delete")
    def compare_delete(cmp_id: int, conn=Depends(get_conn)) -> Any:
        if compare.get_comparison(conn, cmp_id) is None:
            raise HTTPException(404, "сравнение не найдено")
        compare.delete_comparison(conn, cmp_id)
        return RedirectResponse("/compare", status_code=303)

    # ------------------------------------------------------------------
    # per-package backfill + job status
    # ------------------------------------------------------------------
    @app.post("/packages/{package_id}/backfill")
    def package_backfill(package_id: int, conn=Depends(get_conn)) -> Any:
        pkg = watchlist.get_package(conn, package_id)
        if pkg is None:
            raise HTTPException(404, "пакет не найден")

        def _run(conn2: Any, client: ALTRepoClient, progress: Any) -> Any:
            progress("импорт истории сборок…")
            added = refresh.backfill_build_history(
                conn2, client, pkg.id, pkg.name, pkg.branches, cfg.backfill_limit
            )
            return {"package": pkg.name, "imported": added, "limit": cfg.backfill_limit}

        started = run_job(f"backfill:{package_id}", _run)
        return RedirectResponse(
            f"/packages/{package_id}" if started else f"/packages/{package_id}?busy=1",
            status_code=303,
        )

    @app.get("/api/jobs")
    def jobs_status() -> Any:
        out: dict[str, Any] = {}
        for name, job in app.state.jobs.items():
            last = job.get("last")
            summary = None
            if isinstance(last, dict):
                summary = {
                    k: (len(v) if isinstance(v, list) else v)
                    for k, v in last.items()
                    if k in (
                        "image_uuid", "binaries", "srpms", "added", "linked",
                        "branch_updated", "reactivated", "kept", "paused",
                        "existing", "not_found", "updated", "count", "branch",
                        "synced_at", "package", "imported", "limit",
                    )
                }
            out[name] = {
                "running": job.get("running", False),
                "stage": job.get("stage", ""),
                "error": job.get("error"),
                "finished_at": job.get("finished_at"),
                "summary": summary,
            }
        return JSONResponse(out)

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
    def settings_page(
        request: Request,
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        # First visit fills the reference lists (architectures, groups) once.
        refs_error = None
        if not conn.execute("SELECT 1 FROM reference_lists LIMIT 1").fetchone():
            try:
                refs.ensure(conn, client)
            except ApiError as exc:
                refs_error = str(exc)
        return templates.TemplateResponse(
            request,
            "settings.html",
            ctx(
                request,
                saved=request.query_params.get("saved"),
                refs=refs.status(conn),
                arch_list=refs.architectures(conn),
                category_list=refs.categories(conn),
                refs_saved=bool(request.query_params.get("refs")),
                refs_error=refs_error or request.query_params.get("refs_error"),
            ),
        )

    @app.post("/settings/refs/sync")
    def settings_refs_sync(
        conn=Depends(get_conn),
        client=Depends(client_for_request),
    ) -> Any:
        from urllib.parse import quote

        try:
            refs.sync(conn, client)
        except ApiError as exc:
            return RedirectResponse(
                f"/settings?refs_error={quote(str(exc))}", status_code=303
            )
        return RedirectResponse("/settings?refs=1", status_code=303)

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
