<div dir="rtl">

# Xray Multi-Network Manager

[English](README.md) | **فارسی**

[![CI](https://github.com/Theupx/Xray-Multi-Network-Manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Theupx/Xray-Multi-Network-Manager/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> ابزار کنسولی ویندوز برای توزیع ترافیک بین **چند Network Adapter** با استفاده از **Xray**.

این پروژه یک لایهٔ مدیریتی روی Xray و تنظیمات شبکهٔ ویندوز است: کانفیگ Xray را می‌سازد، شبکهٔ ویندوز را تنظیم می‌کند، اتصال را مانیتور می‌کند و هنگام توقف همه‌چیز را به حالت اول برمی‌گرداند. این پروژه **خودِ Xray نیست**.

## ✨ قابلیت‌ها

- 🔀 انتخاب چند Adapter و Load Balancing (الگوریتم `roundRobin` در Xray)
- 🌐 سه حالت Routing: B-PROXY، V-PROXY و TUN
- 🔗 ورود پروکسی با لینک `vless://`، `vmess://`، `trojan://`، `ss://` یا کانفیگ کامل JSON
- 📊 مانیتورینگ زنده: پینگ هر Adapter، سرعت، مصرف داده و وضعیت اتصال
- 📡 کنترل Mobile Hotspot از صفحهٔ مانیتورینگ (کلید `H`)
- 🔄 پاک‌سازی امن: پروکسی سیستم، DNS، Routeها و Hotspot هنگام خروج برمی‌گردند
- 🧹 پروسه‌های قدیمی `xray.exe` همین پوشه قبل از هر اجرا بسته می‌شوند
- 🇮🇷 پشتیبانی از نمایش متن فارسی در کنسول

## 🛣️ حالت‌های Routing

| حالت | توضیح |
|------|-------|
| **B-PROXY** | فقط Balancing، بدون VPN. ترافیک بین Adapterهای انتخاب‌شده از طریق یک SOCKS محلی و پروکسی سیستم پخش می‌شود. |
| **V-PROXY** | مثل B-PROXY، اما خروجی هر Adapter از سرور پروکسی واردشده عبور می‌کند. |
| **TUN** | مدیریت ترافیک در سطح سیستم با TUN در Xray (Wintun). اگر Adapter مربوط به TUN بالا نیاید، اتصال لغو و تنظیمات برگردانده می‌شود. |

حالت‌های V-PROXY و TUN به یک سرور پروکسی نیاز دارند.

## 📦 پیش‌نیازها

- ویندوز ۱۰ یا ۱۱
- Python 3.9 به بالا (`run.bat` در صورت نبودن Python می‌تواند نسخهٔ ۳.۱۱ را با winget نصب کند)
- دسترسی Administrator
- فایل‌های `xray.exe` و `wintun.dll` کنار `main.py` (همراه پروژه نیستند)

## 🚀 نصب

```bash
git clone https://github.com/Theupx/Xray-Multi-Network-Manager.git
cd Xray-Multi-Network-Manager
pip install -r requirements.txt
```

### دانلود Xray و Wintun

این فایل‌ها عمداً داخل ریپو نیستند:

1. **Xray-core**: از https://github.com/XTLS/Xray-core/releases فایل `xray.exe` را از نسخهٔ Windows 64 بردارید.
2. **Wintun**: از https://www.wintun.net فایل `wintun.dll` را از مسیر `wintun/bin/amd64/` بردارید.

هر دو را داخل پوشهٔ پروژه بگذارید.

## ▶️ اجرا

روی **`run.bat`** دوبار کلیک کنید. این فایل دسترسی Administrator می‌گیرد، Python و پکیج‌ها و فایل‌های لازم را بررسی می‌کند و برنامه را اجرا می‌کند. یا دستی از ترمینالِ Administrator:

```bash
python main.py
```

```text
[1] Select Adapters
[2] Edit Local Port        (default: 10808)
[3] Proxy Server Settings  (add / view & ping / remove)
[4] Toggle Routing Mode    (B-PROXY -> V-PROXY -> TUN)
[5] START
[0] EXIT
```

هنگام اجرا: کلید `H` هات‌اسپات را روشن/خاموش می‌کند و `Ctrl+C` برنامه را متوقف و تنظیمات شبکه را برمی‌گرداند.

## 🔐 امنیت

این فایل‌ها را هرگز commit نکنید: `data.json` (ممکن است اطلاعات سرور پروکسی شما را داشته باشد)، `config.json`، `app_log.log`، `xray.exe` و `wintun.dll`.

این ابزار تنظیمات پروکسی سیستم، DNS و Routing را تغییر می‌دهد. در توقف یا خروج عادی همه برگردانده می‌شوند، اما اگر پروسه ناگهان kill شود ممکن است لازم باشد تنظیمات شبکه را دستی ریست کنید. فقط با شبکه‌ها و سرورهایی استفاده کنید که اجازهٔ استفاده از آن‌ها را دارید.

## 🛠️ وضعیت پروژه

نسخهٔ اولیه، فقط ویندوز. گزارش مشکل (Issue) و پیشنهاد خوش‌آمد است.

## 📄 لایسنس

تحت [لایسنس MIT](LICENSE) منتشر شده است.

</div>
