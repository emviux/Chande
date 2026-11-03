"""Automatic update checker, downloader, checksum verifier, and rollback installer for Chande."""

from updater.version import SemVer, compare_semver, is_newer_version, parse_semver
from updater.github_updater import (
    DownloadStageResult,
    GitHubUpdateService,
    ReleaseAsset,
    ReleaseManifest,
    UpdateCheckResult,
    UpdateStateStore,
)
from updater.installer import InstallResult, UpdateInstaller

__all__ = [
    "SemVer",
    "parse_semver",
    "compare_semver",
    "is_newer_version",
    "ReleaseAsset",
    "ReleaseManifest",
    "UpdateCheckResult",
    "DownloadStageResult",
    "UpdateStateStore",
    "GitHubUpdateService",
    "InstallResult",
    "UpdateInstaller",
]
