"""Semantic Versioning (SemVer 2.0.0) parser and comparator for Chande updates."""

from __future__ import annotations
from dataclasses import dataclass
from functools import total_ordering
import re
from typing import Optional, Tuple, Union

_SEMVER_RE = re.compile(
    r"^\s*[vV]?"
    r"(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\s*$"
)


def _parse_prerelease_parts(prerelease: Optional[str]) -> Tuple[Union[int, str], ...]:
    if not prerelease:
        return ()
    parts = []
    for token in prerelease.split("."):
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token)
    return tuple(parts)


@total_ordering
@dataclass(frozen=True)
class SemVer:
    """Immutable Semantic Version representation compliant with SemVer 2.0.0 precedence."""

    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def base_version(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __str__(self) -> str:
        ver = self.base_version
        if self.prerelease:
            ver += f"-{self.prerelease}"
        if self.build:
            ver += f"+{self.build}"
        return ver

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        # Per SemVer 2.0.0 spec, build metadata does not affect version precedence
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
            and self.prerelease == other.prerelease
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)

        # Normal version has higher precedence than a prerelease version (1.0.0 > 1.0.0-alpha)
        if not self.prerelease and not other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True
        if not self.prerelease and other.prerelease:
            return False

        # Compare each dot-separated prerelease identifier
        self_parts = _parse_prerelease_parts(self.prerelease)
        other_parts = _parse_prerelease_parts(other.prerelease)
        for a, b in zip(self_parts, other_parts):
            if a == b:
                continue
            a_is_int = isinstance(a, int)
            b_is_int = isinstance(b, int)
            if a_is_int and b_is_int:
                return int(a) < int(b)
            # Numeric identifiers always have lower precedence than non-numeric identifiers
            if a_is_int != b_is_int:
                return a_is_int
            return str(a) < str(b)
        return len(self_parts) < len(other_parts)


def parse_semver(version_str: Optional[str]) -> Optional[SemVer]:
    """Parse a version string (e.g. 'v1.2.0', '1.2.0-beta.1') into a SemVer object, or None if invalid."""
    if not version_str or not isinstance(version_str, str):
        return None
    match = _SEMVER_RE.match(version_str)
    if not match:
        return None
    return SemVer(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("prerelease") or "",
        build=match.group("build") or "",
    )


def compare_semver(ver_a: str, ver_b: str) -> int:
    """Compare two version strings using SemVer rules.

    Returns:
      -1 if ver_a < ver_b
       0 if ver_a == ver_b
      +1 if ver_a > ver_b
    Raises ValueError if either version string is invalid.
    """
    parsed_a = parse_semver(ver_a)
    parsed_b = parse_semver(ver_b)
    if parsed_a is None:
        raise ValueError(f"Invalid semantic version: {ver_a!r}")
    if parsed_b is None:
        raise ValueError(f"Invalid semantic version: {ver_b!r}")
    if parsed_a < parsed_b:
        return -1
    if parsed_a > parsed_b:
        return 1
    return 0


def is_newer_version(
    installed_version: str,
    candidate_version: str,
    allow_prerelease: bool = False,
) -> bool:
    """Return True strictly when candidate_version > installed_version (never downgrades)."""
    installed = parse_semver(installed_version)
    candidate = parse_semver(candidate_version)
    if installed is None or candidate is None:
        return False
    if candidate.is_prerelease and not allow_prerelease:
        return False
    return candidate > installed
