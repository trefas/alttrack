"""Client for the ALTRepo API (https://rdb.altlinux.org/api).

Only read endpoints are used.  The client is deliberately small: every method
maps to one HTTP call and returns plain dicts.  Field access is defensive
(``.get``) because the remote schema may change.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence

import httpx

log = logging.getLogger("alttrack.api")

USER_AGENT = "alttrack/0.1 (+https://rdb.altlinux.org/api/docs)"


class ApiError(RuntimeError):
    """Generic transport or protocol error."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None):
        super().__init__(message)
        self.url = url
        self.status = status


class PackageNotFound(ApiError):
    """The requested package does not exist (in the requested scope)."""


class ALTRepoClient:
    """Synchronous client with retries and bounded parallelism."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        concurrency: int = 8,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.concurrency = max(1, concurrency)
        self.max_retries = max_retries
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="alttrack-api")

    # -- plumbing ---------------------------------------------------------
    def __enter__(self) -> "ALTRepoClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(30.0),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
            )
            self._local.client = client
        return client

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        client = getattr(self._local, "client", None)
        if client is not None:
            client.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` with retries and error mapping."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2**attempt, 8))
            try:
                resp = self._client().get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.debug("request failed %s: %s", url, exc)
                continue
            if resp.status_code == 404:
                raise PackageNotFound(f"not found: {url}", url=url, status=404)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = ApiError(f"HTTP {resp.status_code} for {url}", url=url, status=resp.status_code)
                log.debug("%s", last_error)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} for {url}", url=url, status=resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(f"invalid JSON from {url}: {exc}", url=url, status=resp.status_code) from exc
        raise ApiError(f"request failed after retries: {url}: {last_error}", url=url)

    def map(self, func: Callable[..., Any], items: Sequence[Any], *args: Any, **kwargs: Any) -> list[Any]:
        """Run ``func(item, *args, **kwargs)`` for every item, in parallel."""
        if not items:
            return []
        futures = [self._pool.submit(func, item, *args, **kwargs) for item in items]
        return [f.result() for f in futures]

    def post(self, path: str, payload: Any) -> Any:
        """POST ``payload`` as JSON with the same retry/error mapping as GET."""
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2**attempt, 8))
            try:
                resp = self._client().post(path, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                log.debug("POST failed %s: %s", url, exc)
                continue
            if resp.status_code == 404:
                raise PackageNotFound(f"not found: {url}", url=url, status=404)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = ApiError(f"HTTP {resp.status_code} for {url}", url=url, status=resp.status_code)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} for {url}", url=url, status=resp.status_code)
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(f"invalid JSON from {url}: {exc}", url=url, status=resp.status_code) from exc
        raise ApiError(f"request failed after retries: {url}: {last_error}", url=url)

    # -- endpoints --------------------------------------------------------
    def version(self) -> dict[str, Any]:
        return self.get("/version")

    def active_packagesets(self) -> list[str]:
        data = self.get("/packageset/active_packagesets")
        return list(data.get("packagesets") or [])

    def source_package_versions(self, name: str) -> list[dict[str, Any]]:
        """Current source package version per branch: [{branch, version, release, pkghash}]."""
        data = self.get("/site/source_package_versions", {"name": name})
        versions = data.get("versions")
        if versions is None:
            raise PackageNotFound(f"source package {name!r} not found", url="/site/source_package_versions")
        return list(versions)

    def find_packages(self, name: str, *, limit: int = 15) -> list[dict[str, Any]]:
        """Name lookup used for autocompletion: [{name, summary, versions: [...]}]."""
        data = self.get("/site/find_packages", {"name": [name]})
        packages = list(data.get("packages") or [])
        return packages[:limit]

    def find_source_package(self, branch: str, name: str) -> str:
        """Resolve a (possibly binary) name to the source package name in a branch."""
        data = self.get("/site/find_source_package", {"branch": branch, "name": name})
        resolved = data.get("source_package")
        if not resolved:
            raise PackageNotFound(f"{name!r} not found in {branch}", url="/site/find_source_package")
        return str(resolved)

    def package_info(self, pkghash: str, branch: str) -> dict[str, Any]:
        """Metadata of one package build: packager, task, dates."""
        data = self.get(f"/site/package_info/{pkghash}", {"branch": branch, "changelog_last": 0})
        return dict(data)

    def acl_by_packages(self, branch: str, names: Sequence[str]) -> list[dict[str, Any]]:
        if not names:
            return []
        data = self.get(
            "/acl/by_packages",
            {"branch": branch, "packages_names": ",".join(names)},
        )
        return list(data.get("packages") or [])

    def errata_search(self, name: str) -> list[dict[str, Any]]:
        """Erratas mentioning a package, across all branches."""
        data = self.get("/errata/search", {"name": name})
        return list(data.get("erratas") or [])

    def package_versions_from_tasks(self, name: str, branch: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Every version of a source package ever built by a completed task."""
        data = self.get(
            "/site/package_versions_from_tasks",
            {"name": name, "branch": branch},
        )
        versions = list(data.get("versions") or [])
        return versions[:limit]

    def tasks_by_package(self, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """History of build tasks of a package (newest first)."""
        data = self.get("/site/tasks_by_package", {"name": name})
        tasks = list(data.get("tasks") or [])
        return tasks[:limit]

    def find_tasks(self, names: Sequence[str], *, tasks_limit: int = 100) -> list[dict[str, Any]]:
        """Live build tasks for a batch of package names."""
        if not names:
            return []
        params: list[tuple[str, str]] = [("by_package", "true"), ("tasks_limit", str(tasks_limit))]
        params.extend(("input", str(n)) for n in names)
        data = self.get_raw("/task/progress/find_tasks", params)
        return list(data.get("tasks") or [])

    def get_raw(self, path: str, params: Iterable[tuple[str, str]]) -> Any:
        """GET with repeated query parameters (FastAPI style lists)."""
        pairs = [(k, v) for k, v in params if v]
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(2**attempt, 8))
            try:
                resp = self._client().get(path, params=pairs)
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if resp.status_code >= 400:
                last_error = ApiError(f"HTTP {resp.status_code} for {url}", url=url, status=resp.status_code)
                if resp.status_code in {429} or resp.status_code >= 500:
                    continue
                raise last_error
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(f"invalid JSON from {url}: {exc}", url=url) from exc
        raise ApiError(f"request failed after retries: {url}: {last_error}", url=url)

    # -- images, repositories, binary/source mapping ----------------------
    def image_info(
        self,
        *,
        branch: str | None = None,
        edition: str | None = None,
        arch: str | None = None,
        release: str | None = None,
        variant: str | None = None,
        type: str | None = None,  # noqa: A002 - matches the API parameter name
    ) -> list[dict[str, Any]]:
        """Catalog of distribution images (empty list when nothing matches)."""
        params = {
            "branch": branch, "edition": edition, "arch": arch,
            "release": release, "variant": variant, "type": type,
        }
        try:
            data = self.get("/image/image_info", params)
        except PackageNotFound:
            return []
        return list(data.get("images") or [])

    def image_packages(
        self,
        uuid: str,
        *,
        limit: int = 10000,
        page: int = 1,
        component: str | None = None,
        group: str | None = None,
    ) -> list[dict[str, Any]]:
        """Binary packages of one image (one page; max 10000 per page)."""
        data = self.get(
            "/image/image_packages",
            {"uuid": uuid, "limit": limit, "page": page,
             "component": component, "group": group},
        )
        return list(data.get("packages") or [])

    def all_image_packages(self, uuid: str, **kwargs: Any) -> list[dict[str, Any]]:
        """All binary packages of an image (follows pagination)."""
        limit = int(kwargs.pop("limit", 10000))
        out: list[dict[str, Any]] = []
        page = 1
        while page <= 100:  # safety cap: 1M packages
            batch = self.image_packages(uuid, limit=limit, page=page, **kwargs)
            out.extend(batch)
            if len(batch) < limit:
                break
            page += 1
        return out

    def source_packages(
        self,
        branch: str,
        names: Sequence[str],
        *,
        archs: Sequence[str] | None = None,
        batch: int = 5000,
    ) -> list[dict[str, Any]]:
        """Map binary package names to their source packages (POST, batched).

        Returns [{name, sourcepkgname, version, release, archs, status}].
        """
        out: list[dict[str, Any]] = []
        names = list(dict.fromkeys(names))
        for start in range(0, len(names), batch):
            payload: dict[str, Any] = {"branch": branch, "packages": names[start:start + batch]}
            if archs:
                payload["archs"] = list(archs)
            try:
                data = self.post("/packageset/source_packages", payload)
            except PackageNotFound:
                # The API answers 404 when nothing in the batch resolves —
                # that is a valid outcome, not an error.
                continue
            out.extend(data.get("packages") or [])
        return out

    def repository_packages(self, branch: str, *, package_type: str = "source") -> list[dict[str, Any]]:
        """Full contents of a branch repository.

        ``package_type`` is 'source' or 'binary'.  Elements carry
        name/version/release/summary/category/maintainer — the reference data
        that used to be imported from CSV in pkgcmp.
        """
        data = self.get(
            "/site/repository_packages",
            {"branch": branch, "package_type": package_type},
        )
        return list(data.get("packages") or [])

    def branch_binary_packages(self, branch: str, *, arch: str | None = None) -> list[dict[str, Any]]:
        """Released binary packages of a branch (name/version/release/source)."""
        data = self.get(f"/export/branch_binary_packages/{branch}", {"arch": arch})
        return list(data.get("packages") or [])
