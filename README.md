# СамГЭС for Home Assistant

Unofficial [Home Assistant](https://www.home-assistant.io/) integration for the «СамГЭС» (АО «Самарагорэнергосбыт») personal account — [lk.samges.ru](https://lk.samges.ru). Scrapes the same HTML pages the web cabinet renders and exposes the data as sensors.

## Features

- **Amount due** (RUB) — account balance with number/address/owner and the balance block rows (debt at month start, charged, paid, recalc, penalties) in attributes
- **Meter readings** — one device per meter: last reading (`TOTAL_INCREASING`, kWh, works with the energy dashboard), reading history table (date, billing month, value, consumption, source) in attributes
- **Monthly accruals** — charged sum (RUB) and consumption (kWh) for the current billing period with tariff details; the full 13-month accrual history (balance in/out, charged, payments) in attributes
- **Payments** — last payment amount with date and source; full payment history in attributes
- **Notifications** — unread count with the notification list in attributes
- **History backfill** — one-off import of dated meter readings, monthly accrual sums (₽) and monthly consumption (kWh, single-meter accounts) into the sensors' long-term statistics, so the History graph and the energy dashboard show past periods right away
- **Login + password auth** — regular Laravel form login; the `default-captcha-response` field is replayed automatically, no captcha solving needed. Re-authentication is requested only if the credentials themselves are rejected
- **Configurable polling interval** (15–1440 min, default 60)

## How auth works

The cabinet is a classic server-rendered Laravel site — there is no JSON API. The integration does `GET /login`, replays the `#authorization_form` hidden fields (`_token`, `default-captcha-response`, `page_code`) together with your credentials in `POST /login`, and follows the `302 → /account/{id}` redirect. The session lives in the `laravel_session` cookie; when it dies the integration signs in again transparently.

## Installation

1. Copy `custom_components/samges` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. `Settings` → `Devices & services` → **Add integration** → search `SamGES` / `СамГЭС`.
2. Enter the **login (e-mail)** and **password** of your personal account.
3. Done — a device appears for the account and per meter.

## Notes

- Parsing relies on the cabinet's current page markup (tables and label/value blocks on `/account`, `/account/{id}/counters`, `/account/{id}/accruals`). If the site changes its layout, some sensors may go `unavailable` — open an issue with a saved HTML copy of the page.
- Read-only: submitting meter readings and payments are not supported.

## Disclaimer

This integration is unofficial and not affiliated with АО «Самарагорэнергосбыт». It uses only the pages of the public web cabinet. Use at your own risk.

## Support the project

If this integration saves you time, you can
[buy me a coffee](https://pay.cloudtips.ru/p/a039d481).

## License

MIT — see LICENSE.
