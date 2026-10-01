"""Shared fixtures: in-memory-ish SQLite and a fake ALTRepo API client."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alttrack.api import PackageNotFound  # noqa: E402
from alttrack.config import Config  # noqa: E402
from alttrack.db import open_db  # noqa: E402


class FakeClient:
    """Duck-typed stand-in for ALTRepoClient (no network at all)."""

    def __init__(self) -> None:
        self.active = ["sisyphus", "p11", "p10"]
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.erratas: dict[str, list[dict[str, Any]]] = {}
        self.meta: dict[tuple[str, str], dict[str, Any]] = {}
        self.acl: dict[str, list[dict[str, Any]]] = {}
        self.task_history: dict[str, list[dict[str, Any]]] = {}
        self.tasks: dict[str, list[dict[str, Any]]] = {}
        self.task_versions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.search: list[dict[str, Any]] = []
        self.calls: list[str] = []
        # -- images / repository / mapping (phase "products") -------------
        self.images: list[dict[str, Any]] = []
        self.image_pkgs: dict[str, list[dict[str, Any]]] = {}
        self.source_map: dict[str, dict[str, Any]] = {}
        self.repo: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.binary_export: dict[str, list[dict[str, Any]]] = {}

    # -- plumbing ---------------------------------------------------------
    def map(self, func, items, *args, **kwargs):
        return [func(item, *args, **kwargs) for item in items]

    def close(self) -> None:  # pragma: no cover - compatibility
        pass

    # -- endpoints --------------------------------------------------------
    def active_packagesets(self) -> list[str]:
        self.calls.append("active_packagesets")
        return list(self.active)

    def source_package_versions(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(f"versions:{name}")
        if name not in self.versions:
            raise PackageNotFound(name)
        return list(self.versions[name])

    def find_packages(self, name: str, *, limit: int = 15) -> list[dict[str, Any]]:
        return [p for p in self.search if name in p.get("name", "")][:limit]

    def package_info(self, pkghash: str, branch: str) -> dict[str, Any]:
        self.calls.append(f"package_info:{pkghash}:{branch}")
        return self.meta.get((pkghash, branch), {"pkghash": pkghash, "branch": branch})

    def acl_by_packages(self, branch: str, names) -> list[dict[str, Any]]:
        self.calls.append(f"acl:{branch}")
        return [e for e in self.acl.get(branch, []) if e.get("name") in set(names)]

    def errata_search(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(f"errata:{name}")
        return list(self.erratas.get(name, []))

    def package_versions_from_tasks(self, name: str, branch: str, *, limit: int = 500):
        return list(self.task_versions.get((name, branch), []))[:limit]

    def tasks_by_package(self, name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.task_history.get(name, []))[:limit]

    def find_tasks(self, names, *, tasks_limit: int = 100) -> list[dict[str, Any]]:
        self.calls.append(f"find_tasks:{','.join(names)}")
        out: list[dict[str, Any]] = []
        for name in names:
            out.extend(self.tasks.get(name, []))
        return out[:tasks_limit]

    # -- reference lists (architectures / software groups) ------------------
    def get(self, path, params=None):
        self.calls.append(f"get:{path}")
        if path == "/site/all_pkgset_archs":
            return {"length": 3, "archs": [
                {"arch": "x86_64", "count": 0},
                {"arch": "i586", "count": 0},
                {"arch": "noarch", "count": 0},
            ]}
        if path == "/site/pkgset_categories_count":
            return {"length": 2, "categories": [
                {"category": "System/Base", "count": 12},
                {"category": "Shells", "count": 5},
            ]}
        if path == "/version":
            return {"api_version": "fake", "version": "0.0"}
        return {}

    # -- images / repository / srpm mapping --------------------------------
    def image_info(self, **filters) -> list[dict[str, Any]]:
        self.calls.append("image_info")
        out = list(self.images)
        for key in ("branch", "edition", "arch", "release", "type"):
            if filters.get(key):
                out = [i for i in out if i.get(key) == filters[key]]
        return out

    def image_packages(self, uuid: str, *, limit: int = 10000, page: int = 1, **kw):
        self.calls.append(f"image_packages:{uuid}")
        pkgs = self.image_pkgs.get(uuid, [])
        start = (page - 1) * limit
        return pkgs[start:start + limit]

    def all_image_packages(self, uuid: str, **kwargs) -> list[dict[str, Any]]:
        return self.image_packages(uuid, **kwargs)

    def source_packages(self, branch: str, names, *, archs=None, batch: int = 5000):
        self.calls.append(f"source_packages:{branch}")
        return [
            dict(self.source_map.get(n, {"name": n, "sourcepkgname": None,
                                         "status": "not_found"}))
            for n in dict.fromkeys(names)
        ]

    def repository_packages(self, branch: str, *, package_type: str = "source"):
        self.calls.append(f"repository_packages:{branch}")
        return list(self.repo.get((branch, package_type), []))

    def branch_binary_packages(self, branch: str, *, arch: str | None = None):
        self.calls.append(f"branch_binary_packages:{branch}")
        return list(self.binary_export.get(branch, []))


def make_pkg_versions(*entries: tuple[str, str, str, str]) -> list[dict[str, Any]]:
    """(branch, version, release, pkghash) -> API payload."""
    return [
        {"branch": b, "version": v, "release": r, "pkghash": h}
        for b, v, r, h in entries
    ]


@pytest.fixture
def conn(tmp_path: Path):
    database = tmp_path / "test.db"
    connection = open_db(database)
    yield connection
    connection.close()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config()
    config.db_path = tmp_path / "test.db"
    config.config_path = tmp_path / "config.toml"
    config.backfill_limit = 5
    return config


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()
