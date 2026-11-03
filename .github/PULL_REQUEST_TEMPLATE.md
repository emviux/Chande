## Summary / خلاصه تغییرات
Describe what this Pull Request changes and why.

## Checklist / چک‌لیست بررسی
- [ ] All automated tests pass (`pytest -v`).
- [ ] Strictly preserves the Navasan API request-budget protections (120/month hard cap, 90 scheduled, 30 manual reserve, max 3 scheduled/day, zero automatic retries).
- [ ] Both Persian (`translations/fa.json`, RTL) and English (`translations/en.json`, LTR) strings are updated and verified.
- [ ] No API keys, `.env` files, or `data/*.json` files are included in the commit.
- [ ] Preserves Windows 7 desktop gadget behavior (no Alt+Tab entry, non-focus-stealing, smooth resizing, and background idle sleep/instant wake).
