# Contributing & Release Workflow Guide / راهنمای مشارکت و انتشار نسخه جدید

Thank you for contributing to **Chande (چنده)**! This document covers architecture rules, translation standards, running the automated test suite, and publishing new GitHub Releases.

از مشارکت شما در توسعه **چنده (Chande)** سپاسگزاریم! در این راهنما اصول معماری پروژه، استانداردهای ترجمه دوزبانه، اجرای تست‌های خودکار و نحوه انتشار نسخه‌های جدید در GitHub Releases توضیح داده شده است.

---

## 1. Core Architectural Invariants / قوانین کلیدی معماری

When modifying Chande, preserve these invariants at all times:

1. **Navasan Quota Protection (`services/budget_manager.py`):**
   - Never exceed `120` total requests/month (`90` scheduled + `30` manual reserve, max `3` scheduled/day, minimum `8h` interval).
   - Never trigger API calls from UI events (dragging, resizing, language switching, theme changes, sleep/wake transitions).
   - Never add automatic error retry loops on Navasan API calls.
2. **Bilingual Completeness (`translations/fa.json` & `translations/en.json`):**
   - Never hardcode user-facing strings in UI widgets. Always use `tr("section.key", lang=self.language)`.
   - Every `tr("...")` key used anywhere in `config/`, `services/`, `storage/`, `ui/`, or `updater/` **must** exist in both `translations/fa.json` and `translations/en.json` (verified automatically by `tests/test_bilingual_i18n.py`).
3. **Windows 7 Desktop Gadget Behavior (`utils/win_gadget.py`):**
   - Keep `WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` extended styles so Chande never appears in `Alt+Tab` and never steals focus from active applications.
4. **Zero Secrets in Git:**
   - Never commit `.env`, `data/user_config.json`, `data/market_cache.json`, `data/request_budget.json`, or any real API key.

---

## 2. Running the Test Suite / اجرای تست‌های خودکار

Before submitting a Pull Request or creating a release, run the full test suite:

```powershell
pytest -v
```

Test suites include:
- `tests/test_budget_and_api.py` — Navasan API parsing, 120/month budget caps, error accounting, secret redaction.
- `tests/test_bilingual_i18n.py` — AST key coverage across `fa.json` and `en.json`, RTL/LTR layout directions, numerals, Jalali dates.
- `tests/test_gadget_behavior.py` — Win32 styles, `Alt+Tab` exclusion, non-focus-stealing topmost behavior, multi-monitor clamping, single-instance mutex, Windows startup registration.
- `tests/test_responsive_resizing.py` — `WM_NCHITTEST` 8-direction edge/corner resizing, Expanded/Compact/Ultra-Compact transitions, exact position & size persistence.
- `tests/test_sleep_wake.py` — Win32 rectangle subtraction occlusion detection, debounced `IDLE` mode, instant `ACTIVE` wake, `DORMANT` hidden mode.
- `tests/test_ui_behavior.py` — Offline startup from cache, masked API key in Settings, manual refresh confirmations.
- `tests/test_updater.py` — SemVer comparison (stable & pre-release), GitHub API error handling, HTTPS download progress, SHA-256 verification, Zip-Slip protection, user-data preservation, rollback on failure, and bilingual update UI.

---

## 3. How to Create & Publish a New Release / نحوه ساخت و انتشار نسخه جدید

Chande uses a central `VERSION` file at the repository root (e.g. `1.2.0`) and Semantic Versioning (`vMAJOR.MINOR.PATCH`).

### Step 1: Update Version & Changelog
1. Update the version string in [`VERSION`](../VERSION) (for example, `1.3.0`).
2. Add a new release section in [`CHANGELOG.md`](../CHANGELOG.md) and [`docs/CHANGELOG.md`](CHANGELOG.md) summarizing Added, Changed, and Fixed items in English and Persian.

### Step 2: Run Tests & Build Local Release Package
```powershell
pytest -v
scripts\release.bat
```
This produces:
- `dist\Chande-v1.3.0-Windows-x64.zip`
- `dist\Chande-v1.3.0-Windows-x64.zip.sha256`
- `dist\SHA256SUMS.txt`

### Step 3: Commit, Tag, and Push to GitHub
```powershell
git add VERSION CHANGELOG.md docs/CHANGELOG.md
git commit -m "chore(release): bump version to v1.3.0"
git tag -a v1.3.0 -m "Chande v1.3.0"
git push origin main
git push origin v1.3.0
```

### Step 4: Automatic GitHub Release Publication
Pushing the `v1.3.0` tag automatically triggers the GitHub Actions workflow (`.github/workflows/release.yml`), which:
1. Runs `pytest -v` on `windows-latest`.
2. Builds `Chande.exe` and packages `Chande-v1.3.0-Windows-x64.zip` + `.sha256` + `SHA256SUMS.txt`.
3. Verifies zero personal config files or secrets exist inside the ZIP archive.
4. Publishes the GitHub Release for `v1.3.0` with the attached ZIP and SHA-256 checksum assets, making it immediately discoverable by Chande's built-in automatic update checker!
