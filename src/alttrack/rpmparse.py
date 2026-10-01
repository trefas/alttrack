"""Parsing of RPM NEVRA from file names.

Ported from pkgcmp (Go) and extended for real-world lists:

* full paths (``/mnt/iso/Packages/bash-5.2.15-alt9.x86_64.rpm``);
* epochs (``1:2.0-alt1.x86_64``);
* ``.src.rpm`` entries;
* unknown architectures (warned, but parsed);
* comments (``#``) and blank lines are skipped.

The ALT pattern ``name-<version>-alt<N>.<arch>`` is tried first, the generic
RPM convention (``name-version-release.arch``) is the fallback.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

ARCHITECTURES: frozenset[str] = frozenset(
    {
        "x86_64", "aarch64", "i386", "i486", "i586", "i686",
        "armv7hl", "armh", "armv6", "noarch",
        "ppc64le", "ppc64", "s390x", "ia64", "mipsel", "riscv64",
        "src", "noarch",
    }
)

# Always-accepted suffixes (kept even if the stored reference list lacks them).
_PARSER_EXTRAS = frozenset({"src", "noarch"})

# name-<digits...-altN>  (optionally with an epoch: 1:2.0-alt1);
# a known architecture suffix is stripped BEFORE matching.
_ALT_RE = re.compile(r"^(?P<name>.+)-(?P<vr>\d[^-]*(?:-alt\d+[^-]*))$")


@dataclasses.dataclass
class ParsedRPM:
    """One parsed line of an rpm file list."""

    raw: str
    name: str = ""
    version: str = ""
    release: str = ""
    arch: str = ""
    epoch: str = ""
    ok: bool = False
    error: str = ""

    @property
    def nevra(self) -> str:
        epoch = f"{self.epoch}:" if self.epoch else ""
        arch = f".{self.arch}" if self.arch else ""
        return f"{self.name}-{epoch}{self.version}-{self.release}{arch}"


def _split_version_release(vr: str) -> tuple[str, str]:
    """Split ``5.2.15-alt9`` / ``1:2.0-alt1`` into (version, release)."""
    idx = vr.rfind("-")
    if idx <= 0 or idx == len(vr) - 1:
        return vr, ""
    return vr[:idx], vr[idx + 1:]


def known_architectures(arches: Any = None) -> frozenset[str]:
    """Architecture suffixes recognised when splitting ``name.ver.rel.arch``.

    ``arches`` adds a stored reference list (see :mod:`alttrack.refs`) to the
    built-in defaults — a stale list can only extend, never break parsing;
    ``src``/``noarch`` are always accepted.
    """
    if arches is None:
        return ARCHITECTURES
    return ARCHITECTURES | frozenset(str(a).lower() for a in arches) | _PARSER_EXTRAS


def parse_rpm_line(line: str, arches: Any = None) -> ParsedRPM:
    """Parse a single list entry (path, NEVRA or plain ``name-version-release``).

    ``arches`` optionally overrides the known-architecture list.
    """
    raw = line.rstrip("\n")
    s = raw.strip()
    parsed = ParsedRPM(raw=raw)
    if not s or s.startswith("#"):
        parsed.error = ""  # blank/comment lines are not errors
        return parsed

    # Strip directory, .rpm suffix and split off a known architecture.
    s = s.rsplit("/", 1)[-1]
    if s.endswith(".rpm"):
        s = s[:-4]
    known = known_architectures(arches)
    arch = ""
    if "." in s:
        head, tail = s.rsplit(".", 1)
        if tail.lower() in known:
            arch = tail
            s = head

    vr = ""
    m = _ALT_RE.match(s)
    if m:
        parsed.name = m.group("name")
        vr = m.group("vr")
    else:
        # Generic convention: name-version-release[.arch]; version must look
        # like a version (starts with a digit) or the line is ambiguous.
        parts = s.split("-")
        if len(parts) < 3 or not parts[-2][:1].isdigit():
            parsed.error = "не удалось разобрать (нет version-release)"
            return parsed
        vr = f"{parts[-2]}-{parts[-1]}"
        parsed.name = "-".join(parts[:-2])

    version, release = _split_version_release(vr)
    if not parsed.name or not version:
        parsed.error = "пустое имя или версия"
        return parsed

    # Epoch lives at the front of the version: "1:2.0" -> epoch=1, version=2.0
    if ":" in version:
        epoch, _, version = version.partition(":")
        parsed.epoch = epoch

    parsed.version = version
    parsed.release = release
    parsed.arch = arch
    parsed.ok = True
    return parsed


def parse_rpm_lines(text: str, arches: Any = None) -> tuple[list[ParsedRPM], list[ParsedRPM]]:
    """Parse a whole file/listing.

    Returns ``(good, bad)`` where ``bad`` holds unparseable entries; blank
    lines and comments are dropped entirely.  ``arches`` optionally overrides
    the known-architecture list (see :func:`parse_rpm_line`).
    """
    good: list[ParsedRPM] = []
    bad: list[ParsedRPM] = []
    for line in text.splitlines():
        parsed = parse_rpm_line(line, arches)
        if not parsed.raw.strip() or parsed.raw.lstrip().startswith("#"):
            continue
        (good if parsed.ok else bad).append(parsed)
    return good, bad
