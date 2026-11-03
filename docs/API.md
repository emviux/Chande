# API & Request-Budget Architecture / مستندات فنی API و مدیریت سهمیه

Chande integrates with two external APIs that are kept strictly separate:
1. **Navasan Market Data API** (`services/navasan_service.py` & `services/budget_manager.py`) — strictly quota-managed (`120 requests/month`).
2. **GitHub Releases API** (`updater/github_updater.py`) — independent background update checks that **never** consume Navasan API budget.

---

## 1. Navasan Market Data API / وب‌سرویس قیمت‌های نوسان

### Endpoint
```http
GET http://api.navasan.tech/latest/?api_key=YOUR_API_KEY
```
- **Single-Request Design:** A single HTTP GET returns all required instruments at once. Chande never makes separate requests per symbol.
- **Tracked Primary Instruments:**
  - `usd_sell` — US Dollar free-market sell rate (Toman)
  - `18ayar` — 18-Karat Gold per gram (Toman)
  - `btc` — Bitcoin
  - `usdt` — Tether (USDT)
- **Tracked Secondary Instruments:**
  - `sekee` (Emami Gold Coin), `eur` (Euro), `gbp` (British Pound), `aed` (UAE Dirham).
- **Important:** `usd_buy` is intentionally ignored for the primary Dollar card; only `usd_sell` is used.

---

## 2. Strict 120 Requests/Month Quota Protection / معماری محافظت از سهمیه ۱۲۰ درخواست ماهانه

To guarantee the Navasan free tier (`120 requests/month`) is never exceeded, `RequestBudgetManager` (`services/budget_manager.py`) enforces:

| Rule | Limit | Enforcement Behavior |
| :--- | :--- | :--- |
| **Monthly Hard Limit** | `120` requests | Blocks **all** Navasan requests (scheduled and manual) once `monthly_total_used >= 120`. |
| **Scheduled Monthly Pool** | `90` requests | Scheduled timers stop automatically once `monthly_scheduled_used >= 90` or `monthly_total_used >= 90`. |
| **Manual Reserve Pool** | `30` requests | Reserved exclusively for user-confirmed manual refreshes, connection tests, and remote usage checks. |
| **Daily Scheduled Cap** | `3` requests/day | At most `3` scheduled requests per local calendar day (`YYYY-MM-DD`). |
| **Minimum Interval** | `8 hours` | Scheduled intervals `< 8 hours` are rejected and clamped to `{8, 12, 24}` hours. |
| **Startup Cache Policy** | Offline-First | Starting Chande loads `data/market_cache.json` immediately (`0` API requests if cache age `< interval`). |
| **Error & Retry Policy** | `0` Retries | Every HTTP attempt (`200`, `401`, `422`, `429`, `500`, `503`, timeout) is recorded once and never auto-retried. |
| **UI Interaction Policy** | `0` Requests | Dragging, resizing, switching Persian/English, changing themes, or sleeping/waking triggers `0` API requests. |

---

## 3. GitHub Releases Auto-Update API / وب‌سرویس بررسی نسخه جدید گیت‌هاب

### Endpoints
- **Stable Channel:**
  ```http
  GET https://api.github.com/repos/{OWNER}/{REPO}/releases/latest
  ```
- **Beta Channel (includes pre-releases):**
  ```http
  GET https://api.github.com/repos/{OWNER}/{REPO}/releases?per_page=10
  ```

### Release Asset Conventions
When a new release tag (e.g. `v1.2.0`) is published on GitHub Releases:
- **Primary Archive Asset:** `Chande-v1.2.0-Windows-x64.zip`
- **Checksum Asset:** `Chande-v1.2.0-Windows-x64.zip.sha256` or `SHA256SUMS.txt` (or SHA-256 hex in the release body / GitHub asset `digest` field).

### Security & Verification Guarantees
1. **Strict Semantic Versioning (`updater/version.py`):** Only upgrades when `remote_version > local_version`. Never downgrades if the local build is equal or newer.
2. **HTTPS Enforcement:** Only downloads from `https://` GitHub domains (`github.com`, `api.github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`).
3. **SHA-256 Verification (`updater/installer.py`):** Verifies the downloaded archive's SHA-256 digest before extracting. Corrupted or tampered files are immediately rejected and deleted.
4. **Zip-Slip Protection & Staging Validation:** Extracts into `updates/staging/vX.Y.Z`, blocks any `..` or absolute paths, and verifies `Chande.exe` (or `main.py`) exists before installation.
5. **User Data Preservation & Rollback:** Protects `data/` (`user_config.json`, `market_cache.json`, `request_budget.json`, `update_state.json`) and `.env`, and restores from `updates/backup/` if any installation step fails.
