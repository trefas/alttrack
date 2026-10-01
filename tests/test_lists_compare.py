"""Tests for saved lists and comparisons (lists + compare)."""

from __future__ import annotations

from alttrack import compare, images, lists


FILE_TEXT = """\
bash-5.2.15-alt9.x86_64
/mnt/iso/zsh-5.9-alt1.x86_64.rpm
coreutils-9.4-alt2.x86_64
broken-line
"""


def _file_list(conn, client, *, title="Список", branch="p11"):
    client.source_map = {
        "bash": {"name": "bash", "sourcepkgname": "bash", "status": "found"},
        "zsh": {"name": "zsh", "sourcepkgname": "zsh", "status": "found"},
        "coreutils": {"name": "coreutils", "sourcepkgname": "coreutils", "status": "found"},
    }
    return lists.save_file_list(
        conn, text=FILE_TEXT, title=title, source_branch=branch, client=client
    )


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------
def test_save_file_list_parses_and_maps(conn, client):
    list_obj, report = _file_list(conn, client)
    assert report["parsed"] == 3
    assert report["bad_count"] == 1
    assert report["bad"] == ["broken-line"]
    assert report["mapped"] == 3

    assert list_obj.item_count == 3
    assert list_obj.bad_lines == 1
    items = lists.list_items(conn, list_obj.id)
    by_name = {i["name"]: i for i in items}
    assert by_name["zsh"]["version"] == "5.9"       # path stripped
    assert by_name["bash"]["source_name"] == "bash"  # mapped to srpm
    assert by_name["bash"]["arch"] == "x86_64"


def test_file_list_dedupes_and_skips_mapping_without_branch(conn, client):
    list_obj, report = lists.save_file_list(
        conn, text="foo-1.0-alt1.x86_64\nfoo-2.0-alt1.x86_64\n"
    )
    assert report["parsed"] == 2
    assert list_obj.item_count == 1  # same name kept once
    assert "source_packages:p11" not in client.calls  # no branch -> no mapping


def test_save_image_list(conn, client):
    client.images = [{
        "uuid": "u1", "branch": "p11", "edition": "edu", "arch": "x86_64",
        "variant": "", "type": "salt", "release": "release", "tag": "11.0",
        "file": "alt-edu-11.iso", "date": "2025-06-01",
    }]
    images.refresh_catalog(conn, client)
    client.image_pkgs["u1"] = [
        {"name": "kernel-image", "version": "6.6.12", "release": "alt1",
         "arch": "x86_64", "summary": "Linux kernel"},
    ]
    client.source_map = {
        "kernel-image": {"name": "kernel-image", "sourcepkgname": "kernel-image",
                         "status": "found"},
    }
    list_obj, report = lists.save_image_list(conn, client, uuid="u1")
    assert list_obj.kind == "image"
    assert list_obj.title == "alt-edu-11.iso"
    item = lists.list_items(conn, list_obj.id)[0]
    assert item["source_name"] == "kernel-image"
    assert item["summary"] == "Linux kernel"
    assert list_obj.params["branch"] == "p11"


def test_save_branch_list_uses_source_field(conn, client):
    client.binary_export["p10"] = [
        {"name": "grep", "version": "3.11", "release": "alt1",
         "arch": "x86_64", "source": "grep"},
        {"name": "uniq", "version": "9.4", "release": "alt2",
         "arch": "x86_64", "source": "coreutils"},
    ]
    list_obj, report = lists.save_branch_list(conn, client, branch="p10", arch="x86_64")
    assert list_obj.kind == "branch"
    assert report["parsed"] == 2
    assert report["mapped"] == 2
    by_name = {i["name"]: i for i in lists.list_items(conn, list_obj.id)}
    assert by_name["uniq"]["source_name"] == "coreutils"
    assert by_name["grep"]["source_name"] == "grep"
    assert list_obj.params == {"branch": "p10", "arch": "x86_64"}


def test_save_branch_list_several_arches(conn, client):
    client.binary_export["p10"] = [
        {"name": "grep", "version": "3.11", "release": "alt1",
         "arch": "x86_64", "source": "grep"},
        {"name": "uniq", "version": "9.4", "release": "alt2",
         "arch": "x86_64", "source": "coreutils"},
    ]
    list_obj, report = lists.save_branch_list(
        conn, client, branch="p10", arch=["x86_64", "aarch64", ""]
    )
    # one export request per selected architecture, deduplicated by name
    assert client.calls.count("branch_binary_packages:p10") == 2
    assert report["parsed"] == 2
    assert list_obj.params == {"branch": "p10", "arch": ["x86_64", "aarch64"]}
    assert list_obj.title == "p10-x86_64+aarch64 (репозиторий)"


def test_delete_list_removes_items_and_comparisons(conn, client):
    a, _ = _file_list(conn, client, title="A")
    b, _ = _file_list(conn, client, title="B")
    cmp_obj = compare.save(conn, left_id=a.id, right_id=b.id)
    lists.delete_list(conn, a.id)
    assert lists.get_list(conn, a.id) is None
    assert lists.list_items(conn, a.id) == []
    assert compare.get_comparison(conn, cmp_obj.id) is None
    assert lists.get_list(conn, b.id) is not None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def _two_lists(conn, client):
    """left: bash/zsh/coreutils ; right: bash(zsh gone, coreutils bumped, sed new)."""
    client.source_map = {
        "bash": {"name": "bash", "sourcepkgname": "bash", "status": "found"},
        "zsh": {"name": "zsh", "sourcepkgname": "zsh", "status": "found"},
        "coreutils": {"name": "coreutils", "sourcepkgname": "coreutils", "status": "found"},
        "sed": {"name": "sed", "sourcepkgname": "sed", "status": "found"},
    }
    left, _ = lists.save_file_list(
        conn, text=FILE_TEXT, title="A", source_branch="p11", client=client
    )
    right, _ = lists.save_file_list(
        conn,
        text=(
            "bash-5.2.15-alt9.x86_64\n"      # same
            "coreutils-9.5-alt1.x86_64\n"    # changed
            "sed-4.9-alt1.x86_64\n"          # new
        ),
        title="B",
        source_branch="p11",
        client=client,
    )
    return left, right


def test_four_statuses(conn, client):
    left, right = _two_lists(conn, client)
    result = compare.compute(conn, left.id, right.id)

    by_name = {r["name"]: r for r in result["rows"]}
    assert by_name["bash"]["status"] == compare.SAME
    assert by_name["coreutils"]["status"] == compare.CHANGED
    assert by_name["zsh"]["status"] == compare.LEFT_ONLY
    assert by_name["sed"]["status"] == compare.RIGHT_ONLY

    stats = result["stats"]
    assert stats == {compare.SAME: 1, compare.CHANGED: 1,
                     compare.LEFT_ONLY: 1, compare.RIGHT_ONLY: 1}
    assert result["total"] == 4

    changed = by_name["coreutils"]
    assert changed["left"]["version"] == "9.4"
    assert changed["right"]["version"] == "9.5"


def test_save_and_replay_report(conn, client):
    left, right = _two_lists(conn, client)
    saved = compare.save(conn, left_id=left.id, right_id=right.id, title="11.0 ↔ 11.1")
    assert saved.id is not None
    assert saved.stats[compare.CHANGED] == 1
    assert saved.title == "11.0 ↔ 11.1"

    replayed = compare.report(conn, saved)
    assert replayed["total"] == 4
    assert replayed["stats"] == saved.stats
    assert compare.list_comparisons(conn) == [saved]


def test_enrichment_from_meta(conn, client):
    client.repo[("p11", "source")] = [
        {"name": "zsh", "version": "5.9", "release": "alt1", "hash": "h",
         "summary": "shell", "category": "Shells", "maintainer": "z@alt"},
    ]
    from alttrack import metasync
    metasync.sync(conn, client, branch="p11")

    left, right = _two_lists(conn, client)
    result = compare.compute(conn, left.id, right.id)
    zsh = next(r for r in result["rows"] if r["name"] == "zsh")
    assert zsh["source"] == "zsh"
    assert zsh["category"] == "Shells"
    assert zsh["summary"] == "shell"


def test_csv_export(conn, client):
    left, right = _two_lists(conn, client)
    result = compare.compute(conn, left.id, right.id)
    csv_all = compare.to_csv(result["rows"])
    assert csv_all.count("\n") == 1 + 4  # header + rows
    assert "только слева" in csv_all

    csv_changed = compare.to_csv(result["rows"], status=compare.CHANGED)
    assert csv_changed.count("\n") == 2  # header + coreutils
    assert "coreutils" in csv_changed


def test_missing_list_raises(conn, client):
    left, _ = _file_list(conn, client)
    try:
        compare.compute(conn, left.id, 999)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
    try:
        lists.require_list(conn, 999)
        raise AssertionError("expected KeyError")
    except KeyError:
        pass
