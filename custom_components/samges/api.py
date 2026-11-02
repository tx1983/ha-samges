"""Async client for the lk.samges.ru personal account.

The cabinet (АО «Самарагорэнергосбыт», STACK platform) is a classic
server-rendered Laravel site — there is no JSON API behind it, every page is
plain HTML. Auth is a regular form POST to ``/login``: a hidden
``default-captcha-response`` input is generated server-side and replayed
verbatim, so headless login only needs to parse the form fields. The session
lives in the ``laravel_session`` cookie; a dedicated ClientSession with its
own cookie jar is used (several config entries never stomp each other's
session).

Success of the POST is a 302 to ``/account/{id}``; a 302 back to ``/login``
means the credentials (or the replayed token) were rejected.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .parser import (
    cell_number,
    extract_form_fields,
    extract_icon_value,
    extract_lines,
    extract_tables,
    find_amount_after,
    first_amount,
    parse_ru_date,
    table_dicts,
)

_LOGGER = logging.getLogger(__name__)

SITE_URL = "https://lk.samges.ru"
LOGIN_URL = f"{SITE_URL}/login"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class SamGesError(Exception):
    """Generic API error."""


class SamGesAuthError(SamGesError):
    """Credentials rejected — reauth required."""


def _headers(referer: str = LOGIN_URL) -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": USER_AGENT,
        "Referer": referer,
    }


class SamGesClient:
    """Thin async scraper around the cabinet's HTML pages."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        login: str,
        password: str,
    ) -> None:
        self._session = session
        self.login = login
        self._password = password
        self.account_id: str | None = None
        # Full login URL of the account page, also used as page path base.
        self._account_path: str | None = None

    # ------------------------------------------------------------------ auth

    async def async_sign_in(self) -> str:
        """Submit the login form; return the account id from the redirect."""
        html = await self._request("GET", LOGIN_URL, auth_required=False)
        fields = extract_form_fields(html, "authorization_form")
        if "_token" not in fields:
            raise SamGesError("Login form fields not found on /login")
        fields["login"] = self.login
        fields["password"] = self._password

        try:
            async with self._session.post(
                LOGIN_URL,
                data=fields,
                headers={**_headers(), "Origin": SITE_URL},
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SamGesError(f"Login request failed: {err}") from err

        if resp.status not in (301, 302, 303, 307):
            # Sometimes the app re-renders the form (200) instead of a redirect.
            error = self._form_error(body)
            raise SamGesAuthError(error or f"Unexpected HTTP {resp.status}")
        if "/login" in location:
            raise SamGesAuthError("Credentials rejected by the cabinet")
        if "/account/" not in location:
            raise SamGesError(f"Unexpected post-login redirect to {location}")

        path = location.split("samges.ru", 1)[-1] or "/account"
        self._account_path = path
        self.account_id = path.rstrip("/").rsplit("/", 1)[-1]
        _LOGGER.debug("Signed in, account page: %s", path)
        return self.account_id

    @staticmethod
    def _form_error(html: str) -> str | None:
        """Visible validation message inside the re-rendered login form."""
        for line in extract_lines(html):
            low = line.lower()
            if any(
                k in low
                for k in ("неверн", "не найден", "не совпад", "заблокир", "captcha")
            ):
                return line[:200]
        return None

    # -------------------------------------------------------------- fetching

    async def _request(
        self,
        method: str,
        url: str,
        auth_required: bool = True,
        _retried: bool = False,
    ) -> str:
        try:
            async with self._session.request(
                method,
                url,
                headers=_headers(url),
                timeout=_TIMEOUT,
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "")
                if auth_required and resp.status in (301, 302, 303, 307):
                    if "/login" in location and not _retried:
                        # Session cookie died — sign in again, retry once.
                        _LOGGER.debug("Redirect to login, re-signing in")
                        await self.async_sign_in()
                        return await self._request(
                            method, url, auth_required, _retried=True
                        )
                    raise SamGesAuthError(
                        f"HTTP {resp.status} redirect to {location}"
                    )
                resp.raise_for_status()
                return await resp.text()
        except SamGesError:
            raise
        except aiohttp.ClientResponseError as err:
            raise SamGesError(f"HTTP {err.status} for {url}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SamGesError(f"Request to {url} failed: {err}") from err

    async def _get_html(self, path: str) -> str:
        return await self._request("GET", urljoin(SITE_URL, path))

    # ------------------------------------------------------------------ data

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch and parse the account, counters and accruals pages."""
        if not self._account_path:
            await self.async_sign_in()
        assert self._account_path is not None

        account_html = await self._get_html(self._account_path)
        data: dict[str, Any] = {
            "account_id": self.account_id,
            "account": self._parse_account(account_html),
            "counters": [],
            "accruals": [],
            "accrual_current": [],
            "payments": [],
            "notifications": [],
            "unread": 0,
        }
        # Sub-pages are best-effort: a markup change must not kill the update.
        for key, path in (
            ("counters", f"{self._account_path}/counters"),
            ("accruals", f"{self._account_path}/accruals"),
            ("notifications", f"{self._account_path}/notifications"),
        ):
            try:
                html = await self._get_html(path)
            except SamGesError as err:
                _LOGGER.debug("Failed to fetch %s: %s", path, err)
                continue
            try:
                if key == "counters":
                    data["counters"] = self._parse_counters(html)
                elif key == "accruals":
                    accruals = self._parse_accruals(html)
                    data["accruals"] = accruals["history"]
                    data["accrual_current"] = accruals["current"]
                    data["payments"] = accruals["payments"]
                else:
                    data["notifications"] = self._parse_notifications(html)
            except Exception:  # noqa: BLE001 — parsing must never crash
                _LOGGER.debug("Failed to parse %s", path, exc_info=True)
        data["unread"] = sum(
            1 for n in data["notifications"] if n.get("unread")
        )
        return data

    # -------------------------------------------------------------- parsers

    def _parse_account(self, html: str) -> dict[str, Any]:
        """Summary block: ЛС number, address, balance, tariff."""
        lines = extract_lines(html)
        account: dict[str, Any] = {"number": self.account_id}

        # «К оплате» lives in the navbar on every page (a.pay-second).
        if m := re.search(
            r'class="[^"]*pay-second[^"]*"[^>]*>\s*([^<]+)', html
        ):
            account["balance"] = first_amount(m.group(1))

        account["address"] = extract_icon_value(html, "map-marker")
        account["owner"] = extract_icon_value(html, "account")
        account["email"] = extract_icon_value(html, "email")
        if m := re.search(r'id="select_ls_link"[^>]*>\s*№?(\d+)', html):
            account["number"] = m.group(1)
        # Other personal accounts bound to the same login.
        others = sorted(set(re.findall(r"/account/(\d+)", html)))
        account["accounts"] = [a for a in others if a != self.account_id]

        # Balance widget rows: label (text-col-left) → amount (text-col-right).
        for key, keywords in (
            ("debt_start", ("долг на",)),
            ("charged_current", ("начислено в",)),
            ("recalc", ("перерасчет", "перерасчёт")),
            ("penalty", ("пени",)),
            ("paid_current", ("оплачено",)),
        ):
            if (value := find_amount_after(lines, keywords)) is not None:
                account[key] = value
        return {k: v for k, v in account.items() if v not in (None, [])}

    def _parse_counters(self, html: str) -> list[dict[str, Any]]:
        """Meter cards (#counter_{rowId}) + the #counterHistoryTable history."""
        counters: dict[str, dict[str, Any]] = {}

        # 1) Meter cards: type, serial, tariff zones, last reading + date.
        segments = re.split(r'id="counter_(\d+)"', html)
        # segments: [head, row_id, card_html, row_id, card_html, ...]
        for i in range(1, len(segments) - 1, 2):
            row_id, card = segments[i], segments[i + 1][:20000]
            counter = counters.setdefault(row_id, {"id": row_id})
            if m := re.search(r'class="type"[^>]*>\s*([^<]+)', card):
                counter["name"] = m.group(1).strip()
            if m := re.search(r"[Сс]ч[её]тчик\s*№\s*(\d+)", card):
                counter["serial"] = m.group(1)
            zones = re.findall(r'data-tariff="(\d+)"', card)
            if zones:
                counter["zones"] = sorted(set(zones))
            # «от 22.10.26» + value in the next block-note cell.
            if m := re.search(
                r"от\s*(\d{2}\.\d{2}\.\d{2,4})[\s\S]{0,400}?"
                r'block-note[^>]*>\s*([\d\s.,]+)',
                card,
            ):
                counter["reading_date"] = parse_ru_date(m.group(1))
                counter["reading"] = cell_number(m.group(2))

        # 2) Readings history table: Название | Дата | Расчетный месяц |
        #    Показание | Расход | Источник.
        for table in extract_tables(html):
            for row in table_dicts(table["rows"]):
                reading = row.get("reading_num")
                if reading is None:
                    continue
                serial = None
                if name := row.get("counter_name"):
                    if m := re.search(r"№\s*(\d+)", name):
                        serial = m.group(1)
                key = self._row_id_by_serial(counters, serial) or (
                    next(iter(counters)) if len(counters) == 1 else serial or "_unknown"
                )
                counter = counters.setdefault(
                    key, {"id": key, "serial": serial}
                )
                if serial and not counter.get("serial"):
                    counter["serial"] = serial
                if name and "name" not in counter:
                    counter["name"] = name.split(",")[0].strip()
                date = row.get("reading_date_dt") or row.get("period_dt")
                counter.setdefault("history", []).append(
                    {
                        "date": date,
                        "period": row.get("period"),
                        "period_dt": row.get("period_dt"),
                        "reading": reading,
                        "volume": row.get("volume_num"),
                        "source": row.get("source"),
                    }
                )

        for counter in counters.values():
            history = counter.get("history") or []
            history.sort(key=lambda h: h.get("date") or h.get("period_dt") or datetime.min, reverse=True)
            counter["history"] = history[:36]
            if history and counter.get("reading") is None:
                counter["reading"] = history[0].get("reading")
                counter["reading_date"] = history[0].get("date")
            if history:
                counter["consumption"] = history[0].get("volume")
            counter.setdefault("id", counter.get("serial") or "meter")
        return [c for c in counters.values() if c.get("serial") or c.get("reading") is not None]

    @staticmethod
    def _row_id_by_serial(
        counters: dict[str, dict[str, Any]], serial: str | None
    ) -> str | None:
        if serial is None:
            return None
        for key, counter in counters.items():
            if counter.get("serial") == serial:
                return key
        return None

    def _parse_notifications(self, html: str) -> list[dict[str, Any]]:
        """#notificationsTable rows: date, text, unread (highlighted class)."""
        notifications: list[dict[str, Any]] = []
        for table in extract_tables(html):
            for row in table_dicts(table["rows"]):
                text = row.get("col1") or row.get("col2")
                date = row.get("reading_date_dt") or row.get("period_dt")
                if not text and date is None:
                    continue
                item: dict[str, Any] = {
                    "date": date,
                    "text": text,
                    "unread": "highlighted" in row.get("_class", ""),
                }
                if links := row.get("_links"):
                    if m := re.search(r"/notifications/(\d+)", links[0]):
                        item["id"] = m.group(1)
                notifications.append(item)
        notifications.sort(
            key=lambda n: n.get("date") or datetime.min, reverse=True
        )
        return notifications

    def _parse_accruals(self, html: str) -> dict[str, list[dict[str, Any]]]:
        """Three tab tables of «Начисления и платежи»:

        - #accrualsHistoryTable — monthly Месяц | Входящее сальдо | Начислено |
          Сумма платежа | Исходящее сальдо (13 rows);
        - #currentAccrualsTable — current month per service: Название услуги |
          Тариф | сальдо | Начислено | Перерасчет | Платежи | Объем;
        - #paymentsHistoryTable — Дата | Сумма | Источник.
        """
        out: dict[str, list[dict[str, Any]]] = {
            "history": [],
            "current": [],
            "payments": [],
        }
        for table in extract_tables(html):
            rows = table_dicts(table["rows"])
            if table.get("id") == "accrualsHistoryTable":
                out["history"] = [
                    item for r in rows if (item := self._accrual_row(r))
                ]
            elif table.get("id") == "currentAccrualsTable":
                out["current"] = [
                    item for r in rows if (item := self._service_row(r))
                ]
            elif table.get("id") == "paymentsHistoryTable":
                out["payments"] = [
                    item for r in rows if (item := self._payment_row(r))
                ]
        return out

    @staticmethod
    def _accrual_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if str(row.get("period") or "").upper() == "ИТОГО":
            return None
        dt = row.get("period_dt") or row.get("reading_date_dt")
        charged = row.get("charged_num")
        if dt is None and charged is None:
            return None
        item: dict[str, Any] = {
            "period": row.get("period") or row.get("reading_date"),
            "balance_in": row.get("balance_in_num"),
            "charged": charged,
            "paid": row.get("paid_num"),
            "balance": row.get("balance_num"),
        }
        item = {k: v for k, v in item.items() if v is not None}
        if not item:
            return None
        if dt is not None:
            item["_dt"] = dt
            item["date"] = dt.date().isoformat()
        return item

    @staticmethod
    def _service_row(row: dict[str, Any]) -> dict[str, Any] | None:
        if str(row.get("service") or "").upper() == "ИТОГО":
            return None
        if not row.get("service") and row.get("charged_num") is None:
            return None
        item: dict[str, Any] = {
            "service": row.get("service") or row.get("counter_name"),
            "tariff": row.get("tariff_zone"),
            "balance_in": row.get("balance_in_num"),
            "charged": row.get("charged_num"),
            "recalc": row.get("recalc_num"),
            "quality": row.get("quality_num"),
            "paid": row.get("paid_num"),
            "balance": row.get("balance_num"),
            "volume": row.get("volume_num"),
        }
        return {k: v for k, v in item.items() if v is not None} or None

    @staticmethod
    def _payment_row(row: dict[str, Any]) -> dict[str, Any] | None:
        date = row.get("reading_date_dt")
        amount = row.get("amount_num") or row.get("paid_num")
        if date is None and amount is None:
            return None
        item: dict[str, Any] = {
            "date": date,
            "amount": amount,
            "source": row.get("source"),
        }
        return {k: v for k, v in item.items() if v is not None}
