"""Sensors exposing account balance, meters and monthly accruals."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SamGesConfigEntry, SamGesCoordinator
from .parser import parse_ru_date

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

RUB = "₽"
KWH = UnitOfEnergy.KILO_WATT_HOUR


def _float(value: Any) -> float | None:
    """Convert stringy numbers ("177.48") to float."""
    try:
        return float(str(value).replace(",", ".").replace(" ", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort parse of scraped date values."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if parsed := dt_util.parse_datetime(text):
        return parsed if parsed.tzinfo else dt_util.as_local(parsed)
    if parsed := parse_ru_date(text):
        return dt_util.as_local(parsed)
    return None


def _naive(value: Any) -> datetime:
    dt = _parse_dt(value)
    return dt.replace(tzinfo=None) if dt else datetime.min


def _latest(items: list[dict[str, Any]], field: str = "date") -> dict[str, Any]:
    return max(items, key=lambda i: _naive(i.get(field) or i.get("_dt")), default={})


def _iso(value: Any) -> str | None:
    dt = _parse_dt(value)
    return dt.date().isoformat() if dt else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamGesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание сенсоров SamGES для лицевого счёта."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    sensors: list[SensorEntity] = [
        SamGesBalanceSensor(coordinator, account_id),
        SamGesChargedSensor(coordinator, account_id),
        SamGesConsumptionSensor(coordinator, account_id),
        SamGesNotificationsSensor(coordinator, account_id),
        SamGesLastPaymentSensor(coordinator, account_id),
    ]
    for counter in data.get("counters") or []:
        sensors.append(SamGesCounterSensor(coordinator, account_id, counter))
        sensors.append(SamGesCounterRateSensor(coordinator, account_id, counter))
        if counter.get("check_date"):
            sensors.append(SamGesCounterCheckDateSensor(coordinator, account_id, counter))
    if data.get("accrual_current"):
        sensors.append(SamGesPriceSensor(coordinator, account_id))

    async_add_entities(sensors)
    _LOGGER.debug("Создано %d сенсоров SamGES", len(sensors))


class SamGesBaseSensor(CoordinatorEntity[SamGesCoordinator], SensorEntity):
    """Базовый класс — доступ к данным координатора."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self.account_id = account_id
        super().__init__(coordinator)

    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    def _account(self) -> dict[str, Any]:
        return self._data().get("account") or {}

    def _accruals(self) -> list[dict[str, Any]]:
        return [a for a in self._data().get("accruals") or [] if isinstance(a, dict)]

    def _accrual_current(self) -> dict[str, Any]:
        """Первый ряд текущей детализации (услуга/тариф/объём за месяц)."""
        rows = [
            a
            for a in self._data().get("accrual_current") or []
            if isinstance(a, dict)
        ]
        return rows[0] if rows else {}

    def _payments(self) -> list[dict[str, Any]]:
        return [
            p
            for p in self._data().get("payments") or []
            if isinstance(p, dict)
        ]

    def _counter(self, counter_id: str) -> dict[str, Any]:
        for counter in self._data().get("counters") or []:
            if str(counter.get("id")) == counter_id:
                return counter
        return {}

    def _account_device_info(self) -> dict[str, Any]:
        account = self._account()
        number = account.get("number") or self.account_id
        return {
            "identifiers": {(DOMAIN, self.account_id)},
            "name": f"СамГЭС {number}",
            "manufacturer": "АО «Самарагорэнергосбыт»",
            "model": account.get("address"),
        }


class SamGesAccountSensor(SamGesBaseSensor):
    """Сенсор уровня лицевого счёта — живёт на устройстве ЛС."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        super().__init__(coordinator, account_id)
        self._attr_device_info = self._account_device_info()


class SamGesBalanceSensor(SamGesAccountSensor):
    """Текущая сумма к оплате по лицевому счёту."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_balance"
        self._attr_name = "К оплате"
        self._attr_icon = "mdi:currency-rub"
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        return _float(self._account().get("balance"))

    @property
    def extra_state_attributes(self):
        account = self._account()
        return {
            "account": account.get("number"),
            "address": account.get("address"),
            "owner": account.get("owner"),
            "email": account.get("email"),
            "accounts": account.get("accounts"),
            "debt_start": _float(account.get("debt_start")),
            "charged": _float(account.get("charged_current")),
            "paid": _float(account.get("paid_current")),
            "recalc": _float(account.get("recalc")),
            "penalty": _float(account.get("penalty")),
        }


class SamGesNotificationsSensor(SamGesAccountSensor):
    """Непрочитанные уведомления от поставщика."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_notifications"
        self._attr_name = "Уведомления"
        self._attr_icon = "mdi:bell"
        super().__init__(coordinator, account_id)

    def _items(self) -> list[dict[str, Any]]:
        return [
            n
            for n in self._data().get("notifications") or []
            if isinstance(n, dict)
        ]

    @property
    def native_value(self):
        return self._data().get("unread") or 0

    @property
    def extra_state_attributes(self):
        items = self._items()
        last = _latest(items)
        return {
            "total": len(items),
            "last_date": _iso(last.get("date")),
            "last_text": (last.get("text") or "")[:255] or None,
            "notifications": [
                {
                    "id": n.get("id"),
                    "date": _iso(n.get("date")),
                    "text": (n.get("text") or "")[:255],
                    "unread": bool(n.get("unread")),
                }
                for n in items[:24]
            ],
        }


class SamGesChargedSensor(SamGesAccountSensor):
    """Сумма начислений за последний расчётный период."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_charged"
        self._attr_name = "Начислено"
        self._attr_icon = "mdi:receipt-text"
        # monetary + total — доступен как «entity tracking the total costs»
        # в энерго-дашборде (его статистика бэкфиллится помесячными суммами).
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    def _last(self) -> dict[str, Any]:
        return _latest(self._accruals())

    @property
    def native_value(self):
        return _float(self._last().get("charged"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        current = self._accrual_current()
        return {
            "period": last.get("period"),
            "date": _iso(last.get("_dt") or last.get("date")),
            "paid": _float(last.get("paid")),
            "balance_in": _float(last.get("balance_in")),
            "balance": _float(last.get("balance")),
            "service": current.get("service"),
            "tariff": current.get("tariff"),
            "recalc": _float(current.get("recalc")),
            "quality": _float(current.get("quality")),
            "accruals": [
                {
                    "period": a.get("period"),
                    "date": _iso(a.get("_dt") or a.get("date")),
                    "charged": _float(a.get("charged")),
                    "paid": _float(a.get("paid")),
                    "balance_in": _float(a.get("balance_in")),
                    "balance": _float(a.get("balance")),
                }
                for a in self._accruals()[:24]
            ],
        }


class SamGesLastPaymentSensor(SamGesAccountSensor):
    """Сумма последнего платежа."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_last_payment"
        self._attr_name = "Последний платёж"
        self._attr_icon = "mdi:cash-check"
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    def _last(self) -> dict[str, Any]:
        return _latest(self._payments())

    @property
    def native_value(self):
        return _float(self._last().get("amount"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        return {
            "date": _iso(last.get("date")),
            "source": last.get("source"),
            "payments": [
                {
                    "date": _iso(p.get("date")),
                    "amount": _float(p.get("amount")),
                    "source": p.get("source"),
                }
                for p in self._payments()[:24]
            ],
        }


class SamGesConsumptionSensor(SamGesAccountSensor):
    """Расход электроэнергии за последний расчётный период, кВт·ч."""

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_consumption"
        self._attr_name = "Расход за месяц"
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = KWH
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        return _float(self._accrual_current().get("volume"))

    @property
    def extra_state_attributes(self):
        current = self._accrual_current()
        return {
            "service": current.get("service"),
            "tariff": current.get("tariff"),
            "charged": _float(current.get("charged")),
            "period": _latest(self._accruals()).get("period"),
        }


class SamGesPriceSensor(SamGesAccountSensor):
    """Цена за кВт·ч из текущего начисления («Одноставочный (6.95 ₽)»).

    state_class=MEASUREMENT — сенсор можно выбрать как «entity with the
    current price» в энерго-дашборде.
    """

    def __init__(self, coordinator: SamGesCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_price"
        self._attr_name = "Стоимость"
        self._attr_icon = "mdi:tag"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = f"{RUB}/{KWH}"
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        tariff = self._accrual_current().get("tariff") or ""
        if m := re.search(r"(\d+[.,]\d+)", tariff):
            return _float(m.group(1))
        return None

    @property
    def extra_state_attributes(self):
        return {"tariff": self._accrual_current().get("tariff")}


class SamGesCounterSensor(SamGesBaseSensor):
    """Последние показания прибора учёта — совместим с energy dashboard."""

    def __init__(
        self, coordinator: SamGesCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{self._counter_id}_reading"
        self._attr_name = counter.get("name") or f"Счётчик {counter.get('serial') or ''}".strip()
        self._attr_icon = "mdi:meter-electric"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = KWH
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
            "name": self._attr_name,
            "manufacturer": "СамГЭС",
            "model": counter.get("model") or counter.get("serial"),
            "serial_number": counter.get("serial"),
        }

    def _c(self) -> dict[str, Any]:
        return self._counter(self._counter_id)

    @property
    def native_value(self):
        return _float(self._c().get("reading"))

    @property
    def extra_state_attributes(self):
        counter = self._c()
        return {
            "serial": counter.get("serial"),
            "zones": counter.get("zones"),
            "reading_date": _iso(counter.get("reading_date")),
            "consumption": _float(counter.get("consumption")),
            "check_date": _iso(counter.get("check_date")),
            "history": [
                {
                    "date": _iso(h.get("date")),
                    "period": h.get("period"),
                    "reading": _float(h.get("reading")),
                    "volume": _float(h.get("volume")),
                    "source": h.get("source"),
                }
                for h in counter.get("history") or []
            ],
        }


class SamGesCounterRateSensor(SamGesBaseSensor):
    """Расход за последний расчётный период (колонка «Расход» истории)."""

    def __init__(
        self, coordinator: SamGesCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{self._counter_id}_rate"
        self._attr_name = f"{counter.get('name') or 'Счётчик'} — расход за период"
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = KWH
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
        }

    def _last(self) -> dict[str, Any]:
        history = self._counter(self._counter_id).get("history") or []
        return history[0] if history else {}

    @property
    def native_value(self):
        return _float(self._last().get("volume"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        return {
            "date": _iso(last.get("date")),
            "period": last.get("period"),
            "reading": _float(last.get("reading")),
            "source": last.get("source"),
        }


class SamGesCounterCheckDateSensor(SamGesBaseSensor):
    """Дата следующей поверки прибора учёта."""

    def __init__(
        self, coordinator: SamGesCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{self._counter_id}_check_date"
        self._attr_name = f"{counter.get('name') or 'Счётчик'} — поверка"
        self._attr_icon = "mdi:calendar-check"
        self._attr_device_class = SensorDeviceClass.DATE
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
        }

    @property
    def native_value(self):
        dt = _parse_dt(self._counter(self._counter_id).get("check_date"))
        return dt.date() if dt else None
