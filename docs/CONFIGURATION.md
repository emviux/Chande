# Configuration Guide / راهنمای تنظیمات و پیکربندی

Chande stores all user settings locally in `data/user_config.json` (which is automatically preserved across updates and excluded from Git). You can configure every option through the graphical **Settings (`⚙`)** dialog or via environment variables.

برنامه چنده تمام تنظیمات کاربر را به صورت محلی در فایل `data/user_config.json` ذخیره می‌کند (این فایل هنگام به‌روزرسانی خودکار حفظ می‌شود و هرگز در Git قرار نمی‌گیرد).

---

## 1. Settings Dialog Overview / بخش‌های پنجره تنظیمات

Open Settings by clicking the **`⚙`** icon on the widget header or right-clicking the system tray icon -> **Settings / تنظیمات**.

### Tab 1: General & API (`تنظیمات عمومی و API`)

| Setting | Persian Label | Default | Description |
| :--- | :--- | :--- | :--- |
| **Data Provider** | منبع دریافت نرخ‌ها | `navasan` | `Navasan API (Official - Quota Managed)` or `Free Market API (No Key Required)`. |
| **Navasan API Key** | کلید API نوسان | *(empty)* | Your personal API key from `navasan.tech`, stored locally and displayed masked in the UI. |
| **Enable automatic scheduled refresh** | فعال‌سازی بروزرسانی خودکار زمان‌بندی‌شده | `Enabled` | Fetches rates automatically only when cache age >= the configured interval and budget allows. |
| **Auto-Refresh Interval** | فاصله زمانی بروزرسانی خودکار | `8 hours` | Choose `8 hours` (3/day, recommended), `12 hours` (2/day), or `24 hours` (1/day). Never allows `< 8h`. |
| **Ask confirmation before manual refresh** | نمایش پیام تأیید قبل از بروزرسانی دستی | `Enabled` | Shows remaining monthly budget before spending a manual request. |
| **Language** | زبان برنامه | `فارسی (Persian)` | Switches the entire application between `فارسی` (RTL) and `English` (LTR) immediately. |
| **Color Theme** | پوسته ظاهری | `Dark Glass` | Choose `Dark Glass` (`پوسته تیره شیشه‌ای`) or `Light Glass` (`پوسته روشن شیشه‌ای`). |
| **Widget Size** | حالت نمایش ویجت | `Expanded` | Switches between `Expanded`, `Compact`, and `Ultra-Compact` modes. |
| **Number Format** | نمایش ارقام | `Persian (۱۲۳)` | Choose Persian digits (`۰۱۲۳۴۵۶۷۸۹`) or Western digits (`0123456789`). |
| **Window Opacity** | شفافیت پنجره | `96%` | Adjust desktop translucency between `50%` and `100%`. |
| **Always on Top** | همیشه روی سایر پنجره‌ها | `Enabled` | Keeps the desktop gadget above normal and maximized windows without stealing focus. |
| **Start with Windows** | اجرا همزمان با شروع ویندوز | `Disabled` | Registers/unregisters `Chande` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. |
| **Show 24h Sparkline** | نمایش نمودار روند در کارت‌ها | `Enabled` | Toggles mini trend sparklines on primary cards in Expanded mode. |
| **Show Secondary Cards** | نمایش کارت‌های فرعی | `Enabled` | Toggles secondary market strip (`sekee`, `eur`, `gbp`, `aed`). |

---

### Tab 2: About & Updates (`درباره و به‌روزرسانی`)

| Setting | Persian Label | Default | Description |
| :--- | :--- | :--- | :--- |
| **GitHub Repository** | مخزن گیت‌هاب | `OWNER/Chande` | GitHub repository (`OWNER/REPO` or full URL) checked for new releases. Also auto-detected from `.git/config` or `CHANDE_GITHUB_REPO`. |
| **Automatically check for updates** | بررسی خودکار نسخه جدید | `Enabled` | Checks GitHub Releases 12 seconds after startup and every 24 hours thereafter. |
| **Automatically download updates** | دانلود خودکار به‌روزرسانی‌ها | `Disabled` | When enabled, automatically starts downloading verified release packages when an update is found. |
| **Update Channel** | کانال دریافت نسخه | `Stable` | `Stable (Recommended)` ignores pre-releases; `Beta (Pre-release)` includes `-beta`, `-rc`, `-alpha` releases. |
| **Check for Updates** | بررسی وجود نسخه جدید | *(Button)* | Triggers an immediate check against GitHub Releases API. |

---

## 2. Environment Variables (`.env`) / متغیرهای محیطی

Creating a `.env` file is **optional** — you can configure everything in the UI. If you prefer environment variables, copy `.env.example` to `.env`:

```ini
# Navasan API Key (used as fallback if no key is saved in Settings UI)
NAVASAN_API_KEY=your_navasan_api_key_here

# Market data provider: "navasan" or "free"
CHANDE_API_PROVIDER=navasan

# Optional GitHub repository for automatic updates (format: OWNER/REPO)
CHANDE_GITHUB_REPO=OWNER/Chande
```

Priority order for API key resolution:
1. Key saved via **Settings UI** (`data/user_config.json`)
2. `NAVASAN_API_KEY` environment variable or `.env` file

Priority order for GitHub repository resolution:
1. Repository saved via **Settings > About & Updates** (`data/user_config.json`)
2. `CHANDE_GITHUB_REPO` environment variable or `.env` file
3. Remote `origin` URL in `.git/config` (when running from a Git clone)

---

## 3. Local Runtime Files (`data/` & `updates/`) / فایل‌های داده محلی

All runtime files are stored inside the application directory and are preserved across updates:

- `data/user_config.json`: Saved preferences, window coordinates (`window_x`, `window_y`), dimensions (`window_width`, `window_height`), language, and API key.
- `data/market_cache.json`: Last successful market rates, sparkline history points (up to 30 points per symbol), and fetch timestamps for instant offline startup.
- `data/request_budget.json`: Monthly (`YYYY-MM`) and daily (`YYYY-MM-DD`) Navasan API request counters and recent audit log.
- `data/update_state.json`: Last GitHub update check timestamp, latest discovered version, and skipped/postponed version state.
