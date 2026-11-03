"""Background QThread workers to keep network I/O completely off the UI thread."""

from __future__ import annotations
from PySide6.QtCore import QThread, Signal

from services.api_client import ApiFetchResult, MarketApiClient
from updater.github_updater import (
    DownloadStageResult,
    GitHubUpdateService,
    ReleaseManifest,
    UpdateCheckResult,
)


class MarketFetchWorker(QThread):
    """Executes a single MarketApiClient.fetch_latest() call on a background thread."""

    finished_result = Signal(object)  # emits ApiFetchResult

    def __init__(
        self,
        client: MarketApiClient,
        api_key: str,
        kind: str = "manual",
        provider: str = "navasan",
        refresh_interval_hours: int = 8,
    ) -> None:
        super().__init__()
        self.client = client
        self.api_key = api_key
        self.kind = kind
        self.provider = provider
        self.refresh_interval_hours = refresh_interval_hours

    def run(self) -> None:
        result: ApiFetchResult = self.client.fetch_latest(
            api_key=self.api_key,
            kind=self.kind,
            provider=self.provider,
            refresh_interval_hours=self.refresh_interval_hours,
        )
        self.finished_result.emit(result)


class UsageCheckWorker(QThread):
    """Executes an explicit user-requested `/usage/` check on a background thread."""

    finished_result = Signal(object)  # emits ApiFetchResult

    def __init__(self, client: MarketApiClient, api_key: str) -> None:
        super().__init__()
        self.client = client
        self.api_key = api_key

    def run(self) -> None:
        result: ApiFetchResult = self.client.check_remote_usage(api_key=self.api_key)
        self.finished_result.emit(result)


class UpdateCheckWorker(QThread):
    """Executes a GitHub Releases update check on a background thread (zero Navasan API cost)."""

    finished_result = Signal(object)  # emits UpdateCheckResult

    def __init__(
        self,
        update_service: GitHubUpdateService | None = None,
        repo_slug: str = "",
        channel: str = "stable",
        force: bool = False,
        *,
        service: GitHubUpdateService | None = None,
        repo: str = "",
    ) -> None:
        super().__init__()
        self.update_service = update_service or service
        self.repo_slug = repo_slug or repo
        self.channel = channel
        self.force = force

    def run(self) -> None:
        if self.update_service is None:
            return
        result: UpdateCheckResult = self.update_service.check_for_updates(
            repo_slug=self.repo_slug,
            channel=self.channel,
            force=self.force,
        )
        self.finished_result.emit(result)


class UpdateDownloadWorker(QThread):
    """Downloads, verifies SHA-256 checksum, and stages a GitHub Release ZIP on a background thread."""

    progress_changed = Signal(int, int, int)  # downloaded_bytes, total_bytes, percent
    finished_result = Signal(object)  # emits DownloadStageResult

    def __init__(
        self,
        update_service: GitHubUpdateService | None = None,
        manifest: ReleaseManifest | None = None,
        *,
        service: GitHubUpdateService | None = None,
    ) -> None:
        super().__init__()
        self.update_service = update_service or service
        self.manifest = manifest

    def run(self) -> None:
        if self.update_service is None or self.manifest is None:
            return
        result: DownloadStageResult = self.update_service.download_and_stage(
            manifest=self.manifest,
            progress_callback=lambda dl, tot, pct: self.progress_changed.emit(dl, tot, pct),
        )
        self.finished_result.emit(result)

