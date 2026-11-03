"""Safe Windows update installer with user-data preservation and automatic rollback."""

from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Optional, Set

from config.constants import APP_NAME, ROOT_DIR, UPDATES_DIR
from services.i18n import tr

PROTECTED_TOP_LEVEL_NAMES: Set[str] = {
    "data",
    "updates",
    "backup",
    ".env",
    ".git",
    "user_config.json",
    "market_cache.json",
    "request_budget.json",
    "update_state.json",
}


@dataclass
class InstallResult:
    """Outcome of applying a staged update with backup and rollback protection."""

    success: bool
    rolled_back: bool = False
    backup_dir: Optional[Path] = None
    error_code: str = ""
    message_fa: str = ""
    message_en: str = ""

    def get_message(self, lang: str = "fa") -> str:
        if str(lang).lower().startswith("en"):
            return self.message_en or self.message_fa
        return self.message_fa or self.message_en


class UpdateInstaller:
    """Safely installs staged updates while preserving user data and rolling back on failure."""

    def __init__(
        self,
        install_dir: Optional[Path] = None,
        updates_dir: Optional[Path] = None,
        copy_fn: Optional[Callable[[Path, Path], None]] = None,
    ) -> None:
        self.install_dir = Path(install_dir).resolve() if install_dir is not None else ROOT_DIR
        self.updates_dir = Path(updates_dir).resolve() if updates_dir is not None else UPDATES_DIR
        self.copy_fn = copy_fn

    @staticmethod
    def validate_staged_directory(staging_dir: Path) -> bool:
        """Verify that staging_dir contains a valid Chande executable or source entrypoint."""
        if not staging_dir or not Path(staging_dir).is_dir():
            return False
        stage = Path(staging_dir)
        exe_file = stage / f"{APP_NAME}.exe"
        main_py = stage / "main.py"
        has_valid_entry = (
            (exe_file.is_file() and exe_file.stat().st_size > 0)
            or (main_py.is_file() and main_py.stat().st_size > 0)
        )
        if not has_valid_entry:
            return False

        has_assets = (stage / "assets").is_dir() or (stage / "_internal" / "assets").is_dir()
        has_translations = (
            (stage / "translations").is_dir()
            or (stage / "_internal" / "translations").is_dir()
        )
        has_version = (stage / "VERSION").is_file()
        return has_assets or has_translations or has_version

    @staticmethod
    def _is_protected_name(name: str) -> bool:
        return name.lower() in {n.lower() for n in PROTECTED_TOP_LEVEL_NAMES}

    def apply_staged_update(
        self,
        staging_dir: Path,
        install_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        post_install_validator: Optional[Callable[[Path], bool]] = None,
    ) -> InstallResult:
        """Instance or class-compatible staged update installer."""
        return self._apply_staged_update_impl(
            install_dir=install_dir or self.install_dir,
            staging_dir=staging_dir,
            backup_dir=backup_dir or (self.updates_dir / "backup"),
            post_install_validator=post_install_validator,
            copy_fn=self.copy_fn,
        )

    @classmethod
    def _apply_staged_update_impl(
        cls,
        install_dir: Path,
        staging_dir: Path,
        backup_dir: Optional[Path] = None,
        post_install_validator: Optional[Callable[[Path], bool]] = None,
        copy_fn: Optional[Callable[[Path, Path], None]] = None,
    ) -> InstallResult:
        """Back up existing app files, replace with staged files (preserving user data), and roll back on failure."""
        install_path = Path(install_dir).resolve()
        stage_path = Path(staging_dir).resolve()
        file_copier = copy_fn or shutil.copy2

        if not cls.validate_staged_directory(stage_path):
            return InstallResult(
                success=False,
                rolled_back=False,
                error_code="invalid_staging_dir",
                message_fa=tr("updater.errors.invalid_staging", lang="fa"),
                message_en=tr("updater.errors.invalid_staging", lang="en"),
            )

        if backup_dir is None:
            backup_path = (install_path / "data" / "updates" / "backup").resolve()
        else:
            backup_path = Path(backup_dir).resolve()

        # 1. Create a clean backup of current application files (excluding protected user data)
        try:
            if backup_path.exists():
                shutil.rmtree(backup_path, ignore_errors=True)
            backup_path.mkdir(parents=True, exist_ok=True)

            for item in install_path.iterdir():
                if cls._is_protected_name(item.name):
                    continue
                if item.resolve() == backup_path or backup_path in item.resolve().parents:
                    continue
                if item.resolve() == stage_path or stage_path in item.resolve().parents:
                    continue
                dest = backup_path / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, dest)
        except OSError as exc:
            return InstallResult(
                success=False,
                rolled_back=False,
                backup_dir=backup_path,
                error_code="backup_failed",
                message_fa=tr("updater.errors.backup_failed", lang="fa", detail=str(exc)),
                message_en=tr("updater.errors.backup_failed", lang="en", detail=str(exc)),
            )

        # 2. Replace application files from staging_dir, preserving protected user data
        try:
            for stage_item in stage_path.iterdir():
                if cls._is_protected_name(stage_item.name):
                    # Never overwrite user data, .env, or config/cache/budget files
                    continue
                target = install_path / stage_item.name
                if stage_item.is_dir():
                    if target.exists() and target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    shutil.copytree(stage_item, target, dirs_exist_ok=True, copy_function=file_copier)
                elif stage_item.is_file():
                    file_copier(stage_item, target)

            # 3. Verify updated installation integrity
            if not cls.validate_staged_directory(install_path):
                raise RuntimeError("Installed application failed executable/asset verification")

            if post_install_validator is not None and not post_install_validator(install_path):
                raise RuntimeError("Custom post-install validation check failed")

            return InstallResult(
                success=True,
                rolled_back=False,
                backup_dir=backup_path,
                message_fa=tr("updater.status.install_complete", lang="fa"),
                message_en=tr("updater.status.install_complete", lang="en"),
            )

        except Exception as exc:
            # 4. Automatic Rollback from backup_path
            rolled_back_ok = cls.restore_from_backup(install_path, backup_path)
            return InstallResult(
                success=False,
                rolled_back=rolled_back_ok,
                backup_dir=backup_path,
                error_code="rolled_back" if rolled_back_ok else "install_and_rollback_failed",
                message_fa=tr(
                    "updater.errors.install_rolled_back",
                    lang="fa",
                    detail=str(exc),
                ),
                message_en=tr(
                    "updater.errors.install_rolled_back",
                    lang="en",
                    detail=str(exc),
                ),
            )

    @classmethod
    def restore_from_backup(cls, install_dir: Path, backup_dir: Path) -> bool:
        """Restore backed-up application files into install_dir without touching user data."""
        install_path = Path(install_dir).resolve()
        backup_path = Path(backup_dir).resolve()
        if not backup_path.is_dir():
            return False
        try:
            # Remove non-protected items currently in install_path
            for current_item in list(install_path.iterdir()):
                if cls._is_protected_name(current_item.name):
                    continue
                if current_item.resolve() == backup_path or backup_path in current_item.resolve().parents:
                    continue
                if current_item.is_dir():
                    shutil.rmtree(current_item, ignore_errors=True)
                elif current_item.is_file():
                    current_item.unlink(missing_ok=True)

            # Copy all items from backup_path back to install_path
            for bak_item in backup_path.iterdir():
                if cls._is_protected_name(bak_item.name):
                    continue
                target = install_path / bak_item.name
                if bak_item.is_dir():
                    shutil.copytree(bak_item, target, dirs_exist_ok=True)
                elif bak_item.is_file():
                    shutil.copy2(bak_item, target)
            return True
        except OSError:
            return False

    @classmethod
    def generate_windows_updater_script(
        cls,
        install_dir: Path,
        staging_dir: Path,
        backup_dir: Path,
        wait_pid: int,
        restart_command: str,
        script_path: Optional[Path] = None,
    ) -> Path:
        """Generate a standalone Windows PowerShell helper script to replace locked files after exit."""
        out_path = Path(script_path or (UPDATES_DIR / "apply_update.ps1")).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        protected_list = ", ".join(f'"{name}"' for name in sorted(PROTECTED_TOP_LEVEL_NAMES))
        ps_script = f"""# Chande Standalone Windows Update Helper (Per-User, No Admin Required)
$ErrorActionPreference = "Stop"
$InstallDir = "{install_dir.resolve()}"
$StageDir   = "{staging_dir.resolve()}"
$BackupDir  = "{backup_dir.resolve()}"
$WaitPid    = {int(wait_pid)}
$Protected  = @({protected_list})

# 1. Wait for main Chande process to exit (up to 30 seconds)
for ($i = 0; $i -lt 60; $i++) {{
    $proc = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
    if ($null -eq $proc) {{ break }}
    Start-Sleep -Milliseconds 500
}}

# 2. Prepare clean backup directory
if (Test-Path -LiteralPath $BackupDir) {{
    Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
}}
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

try {{
    # 3. Back up current installation (excluding protected user data)
    Get-ChildItem -LiteralPath $InstallDir -Force | ForEach-Object {{
        if ($Protected -notcontains $_.Name) {{
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $BackupDir $_.Name) -Recurse -Force
        }}
    }}

    # 4. Copy staged files into installation directory (never overwriting user data)
    Get-ChildItem -LiteralPath $StageDir -Force | ForEach-Object {{
        if ($Protected -notcontains $_.Name) {{
            $dest = Join-Path $InstallDir $_.Name
            if (Test-Path -LiteralPath $dest) {{
                Remove-Item -LiteralPath $dest -Recurse -Force
            }}
            Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force
        }}
    }}

    # 5. Verify executable exists
    $exePath = Join-Path $InstallDir "{APP_NAME}.exe"
    $mainPy  = Join-Path $InstallDir "main.py"
    if (-not ((Test-Path -LiteralPath $exePath) -or (Test-Path -LiteralPath $mainPy))) {{
        throw "Updated executable missing after copy."
    }}
}} catch {{
    # 6. Rollback from backup on any failure
    Get-ChildItem -LiteralPath $BackupDir -Force | ForEach-Object {{
        if ($Protected -notcontains $_.Name) {{
            $dest = Join-Path $InstallDir $_.Name
            if (Test-Path -LiteralPath $dest) {{
                Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
            }}
            Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force -ErrorAction SilentlyContinue
        }}
    }}
}}

# 7. Restart Chande
{restart_command}
"""
        out_path.write_text(ps_script, encoding="utf-8")
        return out_path

    @classmethod
    def launch_external_updater_and_restart(
        cls,
        staging_dir: Path,
        install_dir: Optional[Path] = None,
    ) -> Path:
        """Generate and spawn the detached Windows updater helper for the current process."""
        if install_dir is None:
            if getattr(sys, "frozen", False):
                target_install = Path(sys.executable).resolve().parent
            else:
                target_install = ROOT_DIR
        else:
            target_install = Path(install_dir).resolve()

        backup_dir = UPDATES_DIR / "backup"
        exe_candidate = target_install / f"{APP_NAME}.exe"
        if exe_candidate.is_file() or getattr(sys, "frozen", False):
            restart_cmd = f'Start-Process -FilePath (Join-Path $InstallDir "{APP_NAME}.exe") -WorkingDirectory $InstallDir'
        else:
            py_exe = Path(sys.executable).resolve()
            restart_cmd = f'Start-Process -FilePath "{py_exe}" -ArgumentList "main.py" -WorkingDirectory $InstallDir'

        script_path = cls.generate_windows_updater_script(
            install_dir=target_install,
            staging_dir=staging_dir,
            backup_dir=backup_dir,
            wait_pid=os.getpid(),
            restart_command=restart_cmd,
        )

        if sys.platform == "win32":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(script_path),
                ],
                cwd=str(UPDATES_DIR),
                creationflags=creation_flags,
                close_fds=True,
            )
        return script_path
