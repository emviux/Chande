# Changelog / تاریخچه تغییرات

All notable changes to **Chande (چنده)** are documented in this file.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) (`MAJOR.MINOR.PATCH`).

تمام تغییرات مهم پروژه **چنده (Chande)** در این فایل ثبت می‌شوند.
این پروژه از استاندارد نسخهگذاری معنایی ([Semantic Versioning](https://semver.org/)) پیروی می‌کند.

---

## [1.4.0] - 2026-11-06

### Added / قابلیت‌های جدید
- **Windows Desktop Right-Click Context Menu Integration (`Chande`)**:
  - Added **Chande** (with the official gold-coin icon `assets/icons/chande.ico`) to the native Windows desktop background right-click context menu under `HKEY_CURRENT_USER\Software\Classes\DesktopBackground\Shell\Chande` without requiring administrator privileges.
  - Clicking **Chande** from the desktop right-click menu launches Chande cleanly (windowed GUI, no CMD/PowerShell console), restores the widget if hidden in the system tray, or leaves the existing desktop widget untouched if already visible.
  - Added **Add 'Chande' to Desktop Context Menu** (`افزودن گزینه «Chande» به منوی راست‌کلیک دسکتاپ`) toggle and live installation status indicator under **Settings > General**, plus automatic startup path self-healing and `--uninstall` cleanup.

### Fixed / رفع باگ‌ها
- **Fixed Windows Show Desktop Z-Order & Desktop Layering**:
  - Fixed an issue where clicking the Windows **Show Desktop** button (`Win+D` / bottom-right taskbar corner) caused Explorer's raised `WorkerW`/`Progman` container to cover or hide Chande until the user right-clicked on the desktop.
  - Kept Chande's native `HWND` unowned (`GWLP_HWNDPARENT = 0`) and positioned it above the raised `WorkerW`/`Progman` desktop shell during Show Desktop while keeping it on the desktop layer (`HWND_BOTTOM`) behind all normal, maximized, and full-screen application windows during normal operation.

---

## [1.3.1] - 2026-11-05

### Added / Improved
- Persistent window size across application restarts (`window_width`, `window_height`, and `display_mode` restored before showing the widget, with proportional scaling if saved dimensions exceed available screen area).
- Improved Windows desktop-gadget window management (`DesktopShellEventMonitor` via `SetWinEventHook`, `nativeEvent` Win32 shell message interception, and `DWMWA_EXCLUDED_FROM_PEEK` / `DWMWA_TRANSITIONS_FORCEDISABLED`).

### Fixed
- Chande no longer disappears or minimizes when clicking the Windows **Show Desktop** button (`TrayShowDesktopButtonWClass` / `Win+D` / `ToggleDesktop`).
- Restored the user's last selected window dimensions accurately on startup (`minimumSizeHint` / `SetNoConstraint` no longer overrides custom dimensions such as `420×580`, and `QSizeGrip` resizing in both RTL Persian and LTR English flushes immediately on mouse release).

---

## [1.2.0] - 2026-11-03

### Added / قابلیت‌های جدید
- **Automatic Update System (`updater/`)**:
  - Background GitHub Releases update checker (`updater/github_updater.py`) querying `https://api.github.com/repos/OWNER/REPO/releases/latest` at startup (after a 12-second delay) and every 24 hours without affecting the Navasan API quota.
  - Strict Semantic Versioning parser and comparator (`updater/version.py`) supporting `MAJOR.MINOR.PATCH`, `v`-prefixed tags, and `stable` vs. `beta`/`rc`/`alpha` pre-release channels.
  - Verified HTTPS release downloader with streaming progress, SHA-256 checksum validation (`.sha256` / `SHA256SUMS.txt` / GitHub asset digest), staging extraction with Zip-Slip path traversal protection, and atomic rollback (`updater/installer.py`).
  - Automatic preservation of user configuration (`data/user_config.json`), Navasan API key, monthly request-budget ledger (`data/request_budget.json`), and price cache (`data/market_cache.json`) across updates.
  - Non-focus-stealing bilingual update notification dialog (`ui/update_dialog.py`) with release notes, version comparison, download progress bar, **Update Now**, **Remind Me Later**, and **Skip This Version**.
  - **Settings > About & Updates** tab (`ui/settings_dialog.py`) displaying installed version, latest available version, GitHub repository setting, automatic check/download toggles, update channel selector (`Stable` / `Beta`), and manual **Check for Updates** button.
- **GitHub Publication & Release Automation**:
  - Added `scripts/build.bat` and `scripts/release.bat` to build `dist/Chande/Chande.exe`, strip any local runtime data, package `Chande-v1.2.0-Windows-x64.zip`, and generate `.sha256` and `SHA256SUMS.txt` checksum files.
  - Added `.github/workflows/release.yml` GitHub Actions workflow for automated Windows x64 builds, test verification, and GitHub Release publishing on `v*.*.*` tags.
  - Added complete bilingual (English + Persian) documentation in `README.md` and `docs/` (`INSTALLATION.md`, `CONFIGURATION.md`, `API.md`, `TROUBLESHOOTING.md`, `CONTRIBUTING.md`).

### Changed / تغییرات و بهبودها
- **Exact Window Position & Size Persistence**:
  - Removed automatic snap-to-corner overrides when switching display modes (`Expanded`, `Compact`, `Ultra-Compact`) or saving unrelated settings, ensuring Chande always starts at its exact last desktop position (`window_x`, `window_y`).
- **Security Hardening**:
  - Sanitized `.env.example` with safe placeholders only and strengthened `.gitignore` rules so personal configurations, API keys, cache files, and staged updates are never committed.

---

## [1.1.0] - 2026-10-30

### Added / قابلیت‌های جدید
- **Intelligent Background Sleep & Instant Wake (`services/visibility_monitor.py`)**:
  - Win32 desktop occlusion detector (`EnumWindows`, `DwmGetWindowAttribute`) that computes exact visible screen rectangles of top-level windows above Chande.
  - Three operating states (`ACTIVE`, `IDLE`, `DORMANT`) with debounced sleep entry (`2.5s`) when >=98% covered and instant (<50ms) zero-flicker wake when uncovered.
  - Suspends UI timers and sparkline `QPainter` rendering during `IDLE` and `DORMANT` states while preserving background scheduled price fetches.
- **Full Bilingual UI (`localization/i18n.py`, `translations/fa.json`, `translations/en.json`)**:
  - Instant runtime switching between Persian (`فارسی`, RTL) and English (`English`, LTR) across the widget, Settings dialog, system-tray menu, tooltips, and dialogs without restarting.
  - Configurable Persian (`۰۱۲۳۴۵۶۷۸۹`) or Western (`0123456789`) numerals and Jalali (`شمسی`) / Gregorian date formatting.
- **Smooth Edge/Corner Resizing & Three Adaptive Display Modes**:
  - Native Win32 `WM_NCHITTEST` border and corner dragging with responsive reflow across **Expanded** (4 cards + sparklines), **Compact** (2x2 grid, graphs auto-hidden), and **Ultra-Compact** (minimal ticker strip).

---

## [1.0.0] - 2026-10-25

### Added / قابلیت‌های جدید
- Initial release of the **Chande (چنده)** Windows Desktop Currency, Gold & Crypto Gadget:
  - Live tracking of **US Dollar (`usd_sell`)**, **18K Gold (`18ayar`)**, **Bitcoin (`btc`)**, and **Tether (`usdt`)** via the Navasan API (`http://api.navasan.tech/latest/`).
  - Strict 120 requests/month quota protection (`services/budget_manager.py`): 90 scheduled requests + 30 manual reserve, max 3 scheduled requests/day, minimum 8-hour default interval, and zero automatic error retries.
  - Native Windows 7 desktop gadget behavior (`utils/win_gadget.py`): frameless translucent glass card, stays visible above normal and maximized windows without stealing keyboard focus (`WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`), excluded from `Alt+Tab` and taskbar, with system-tray control and single-instance mutex protection.
