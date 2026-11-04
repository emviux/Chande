# Installation Guide / راهنمای جامع نصب و راه‌اندازی

This guide explains how to install and run **Chande (چنده)** on Windows 10 and Windows 11, both for regular desktop users (using the ready-to-run release package) and for developers (running from Python source).

این راهنما نحوه نصب و اجرای ویجت دسکتاپ **چنده (Chande)** را روی ویندوز ۱۰ و ۱۱، هم برای کاربران عادی (با استفاده از بسته اجرایی آماده) و هم برای توسعه‌دهندگان (اجرا از طریق سورس پایتون) توضیح می‌دهد.

---

## Option A: Recommended for End Users (Portable Release ZIP)

You do **not** need Python installed on your computer.

### Step 1: Download the Latest Release
1. Open the GitHub repository's **Releases** page.
2. Under the latest release (e.g., `v1.3.1`), download:
   - **`Chande-v1.3.1-Windows-x64.zip`**
   - *(Optional)* `Chande-v1.3.1-Windows-x64.zip.sha256` to verify file integrity.

### Step 2: Extract to a Permanent Folder
1. Right-click `Chande-v1.3.1-Windows-x64.zip` and choose **Extract All...**.
2. Choose a permanent destination folder (for example, `C:\Users\<YourUsername>\Apps\Chande` or a folder on your Desktop).
   > **Important:** Always extract the entire folder before running `Chande.exe`. Do not double-click `Chande.exe` from inside the unextracted `.zip` preview window.

### Step 3: Launch `Chande.exe`
1. Open the extracted folder and double-click **`Chande.exe`**.
2. **Windows SmartScreen Notice:** Because `Chande.exe` is an independent open-source binary without a paid commercial Authenticode certificate, Windows Defender SmartScreen may display *"Windows protected your PC"* on the first launch:
   - Click **More info**.
   - Click **Run anyway**.

### Step 4: Configure Your Navasan API Key
1. Click the **Settings (`⚙`)** button on the top bar of the widget (or right-click the **Chande** icon in your Windows system tray and click **Settings / تنظیمات**).
2. Under **API Provider & Credentials**, paste your API key from [Navasan.tech](http://navasan.tech) into **Navasan API Key**.
3. Click **Save Changes**.
4. Click the **Refresh (`↻`)** button on the widget header once to download your initial market rates.

---

## روش اول: پیشنهادی برای کاربران ویندوز (نسخه پرتابل بدون نیاز به پایتون)

### مرحله ۱: دانلود آخرین نسخه
۱. به صفحه **Releases** مخزن گیت‌هاب مراجعه کنید.
۲. از بخش آخرین نسخه منتشرشده (مانند `v1.3.1`)، فایل زیر را دانلود کنید:
   - **`Chande-v1.3.1-Windows-x64.zip`**

### مرحله ۲: استخراج فایل فشرده (Extract)
۱. روی فایل `Chande-v1.3.1-Windows-x64.zip` راست‌کلیک کرده و گزینه **Extract All...** را انتخاب کنید.
۲. یک پوشه دائمی (مثلاً در `C:\Users\<YourName>\Apps\Chande` یا روی دسکتاپ) انتخاب کنید.
   > **توجه مهم:** حتماً کل پوشه را Extract کنید و فایل `Chande.exe` را مستقیماً از داخل پنجره پیش‌نمایش فایل ZIP اجرا نکنید.

### مرحله ۳: اجرای برنامه `Chande.exe`
۱. وارد پوشه استخراج‌شده شوید و روی **`Chande.exe`** دابل‌کلیک کنید.
۲. **پیام Windows SmartScreen:** در اولین اجرا ممکن است ویندوز پیام *"Windows protected your PC"* را نمایش دهد. روی گزینه **More info** کلیک کرده و سپس دکمه **Run anyway** را بزنید.

### مرحله ۴: وارد کردن کلید API نوسان
۱. روی آیکون **تنظیمات (`⚙`)** در نوار بالایی ویجت کلیک کنید (یا روی آیکون **چنده** در سینی سیستم کنار ساعت ویندوز راست‌کلیک کرده و گزینه **تنظیمات** را بزنید).
۲. در بخش **سرویس‌دهنده و کلید API**، کلید دریافتی خود از سایت [Navasan.tech](http://navasan.tech) را وارد کنید.
۳. روی **ذخیره تغییرات** کلیک کنید.
۴. یک بار دکمه **بروزرسانی (`↻`)** روی ویجت را بزنید تا قیمت‌های اولیه دریافت شوند.

---

## Option B: Install & Run from Python Source (For Developers)

### Prerequisites
- **Windows 10 or Windows 11 (x64)**
- **Python 3.11+** (`python --version`)
- **Git** (`git --version`)

### Step-by-Step Setup
```powershell
# 1. Clone the repository
git clone https://github.com/OWNER/Chande.git
cd Chande

# 2. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install required packages
pip install -r requirements.txt

# 4. (Optional) Create local .env from template
copy .env.example .env

# 5. Launch Chande
python main.py
```

### Verify Your Installation
To verify that the entire application, UI, localization, and auto-updater work properly on your machine:
```powershell
pytest -v
```
