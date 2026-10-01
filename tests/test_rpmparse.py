"""Tests for RPM NEVRA parsing (rpmparse)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alttrack.rpmparse import parse_rpm_line, parse_rpm_lines  # noqa: E402


def test_alt_basic():
    p = parse_rpm_line("bash-5.2.15-alt9.x86_64.rpm")
    assert p.ok
    assert (p.name, p.version, p.release, p.arch) == ("bash", "5.2.15", "alt9", "x86_64")
    assert p.nevra == "bash-5.2.15-alt9.x86_64"


def test_path_is_stripped():
    p = parse_rpm_line("/mnt/iso/Packages/bash-5.2.15-alt9.x86_64.rpm")
    assert p.ok and p.name == "bash" and p.arch == "x86_64"


def test_epoch():
    p = parse_rpm_line("foo-1:2.0-alt1.x86_64")
    assert p.ok
    assert p.epoch == "1"
    assert (p.name, p.version, p.release) == ("foo", "2.0", "alt1")
    assert p.nevra == "foo-1:2.0-alt1.x86_64"


def test_src_rpm():
    p = parse_rpm_line("foo-1.0-alt1.src.rpm")
    assert p.ok and p.arch == "src" and p.version == "1.0" and p.release == "alt1"


def test_multi_dash_name():
    p = parse_rpm_line("389-ds-base-3.1.3-alt3.x86_64")
    assert p.ok and p.name == "389-ds-base" and p.version == "3.1.3"


def test_non_alt_fallback():
    p = parse_rpm_line("bash-5.1.16-1.fc35.x86_64")
    assert p.ok
    assert (p.name, p.version, p.release) == ("bash", "5.1.16", "1.fc35")


def test_no_arch_alt():
    p = parse_rpm_line("foo-1.0-alt1")
    assert p.ok and p.arch == "" and p.release == "alt1"


def test_unknown_arch_warned():
    p = parse_rpm_line("foo-1.0-alt1.mystery9")
    # falls into the generic branch: last dot part unknown -> part of release
    assert p.ok
    assert p.release in ("alt1.mystery9", "alt1")


def test_unparseable():
    p = parse_rpm_line("just-a-name")
    assert not p.ok and p.error


def test_blank_and_comment():
    assert parse_rpm_line("").raw == ""
    assert not parse_rpm_line("# comment").ok
    assert not parse_rpm_line("   #").raw.startswith("#")


def test_parse_lines_splits_good_bad():
    good, bad = parse_rpm_lines(
        "bash-5.2.15-alt9.x86_64\n"
        "\n"
        "# comment\n"
        "broken-line\n"
        "  \n"
        "zsh-5.9-alt1.x86_64\n"
    )
    assert [g.name for g in good] == ["bash", "zsh"]
    assert [b.raw for b in bad] == ["broken-line"]


def test_list_paths_mixed():
    text = "\n".join(
        [
            "/repo/a-1.2.3-alt1.aarch64.rpm",
            "b-0.1-alt1.noarch",
            "c-2.0-alt10.x86_64",
        ]
    )
    good, bad = parse_rpm_lines(text)
    assert not bad
    assert [g.name for g in good] == ["a", "b", "c"]
    assert good[2].release == "alt10"
