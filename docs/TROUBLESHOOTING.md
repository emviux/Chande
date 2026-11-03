# Troubleshooting Guide / راهنمای عیب‌یابی و رفع مشکلات رایج

Here are solutions to common questions and issues when running **Chande (چنده)** on Windows.

در این صفحه راه‌حل مشکلات رایج هنگام نصب یا استفاده از ویجت **چنده (Chande)** توضیح داده شده است.

---

## 1. Windows Defender SmartScreen Warning on First Run
**Symptom:** Windows displays *"Windows protected your PC — Microsoft Defender SmartScreen prevented an unrecognized app from starting"* when launching `Chande.exe`.
- **Cause:** `Chande.exe` is an open-source portable executable built with PyInstaller and is not signed with a paid commercial certificate.
- **Solution:**
  1. Click **More info** (`اطلاعات بیشتر`).
  2. Click **Run anyway** (`اجرا در هر صورت`).
  3. You can also verify the SHA-256 checksum of `Chande-v1.2.0-Windows-x64.zip` against `Chande-v1.2.0-Windows-x64.zip.sha256` on the GitHub Release page:
     ```powershell
     Get-FileHash .\Chande-v1.2.0-Windows-x64.zip -Algorithm SHA256
     ```

---

## 2. "No API Key Configured" / «کلید API تنظیم نشده است»
**Symptom:** The status banner displays a warning that the Navasan API key is missing.
- **Solution:**
  1. Click the **Settings (`⚙`)** button on the widget header.
  2. Paste your API key from [Navasan.tech](http://navasan.tech) into **Navasan API Key** (`کلید API نوسان`).
  3. Alternatively, switch **Data Provider (`منبع دریافت نرخ‌ها`)** to **Free Market API (No Key Required)** if you do not have a Navasan key yet.
  4. Click **Save Changes** (`ذخیره تغییرات`) and then click **Refresh (`↻`)**.

---

## 3. HTTP 401 / 422 / 429 Errors from Navasan API
- **`HTTP 401` (Invalid or Expired API Key):** Verify that your Navasan API key was copied without extra spaces and that your subscription/free key is active on `navasan.tech`.
- **`HTTP 422` (Monthly Navasan Quota Exhausted on Server):** Your Navasan account's monthly server quota has been reached. Chande will continue displaying your last cached prices cleanly.
- **`HTTP 429` (Rate Limit Exceeded):** Wait before requesting again. Remember that Chande never auto-retries failed calls to protect your quota.

---

## 4. Widget Is Not Visible on the Desktop
**Symptom:** `Chande.exe` is running, or says it is already running, but you do not see the widget on your screen.
- **Solution:**
  1. Check the **Windows System Tray** (notification area near the clock, click the `^` arrow if hidden).
  2. Right-click the **Chande** icon and click **Show / Hide Widget (`نمایش / پنهان کردن ویجت`)**.
  3. If you recently disconnected an external monitor, right-click the tray icon and click **Reset Position (`بازنشانی موقعیت به گوشه صفحه`)** to move Chande back to the primary monitor's top-right work area.

---

## 5. Automatic Update Checker Says "GitHub repository is not configured"
**Symptom:** Clicking **Check for Updates** shows that the GitHub repository is not configured.
- **Solution:**
  1. Open **Settings (`⚙`) -> About & Updates (`درباره و به‌روزرسانی`)**.
  2. In the **GitHub Repository (`مخزن گیت‌هاب`)** field, enter the repository path in `OWNER/REPO` format (for example, `YourUsername/Chande`) or paste the full GitHub URL (`https://github.com/YourUsername/Chande`).
  3. Click **Save Changes** and then click **Check for Updates**.

---

## 6. Resetting Local Cache or Request Budget Counter
If you switched to a brand-new Navasan API key mid-month and want to reset Chande's local request counter:
1. Open **Settings (`⚙`) -> General & API**.
2. Scroll to **Local Request Budget & Cache Status**.
3. Click **Reset Local Counter (`بازنشانی شمارنده محلی`)** (note: this only resets Chande's local ledger in `data/request_budget.json`, not Navasan's server-side usage).
