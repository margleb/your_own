"""Transactional per-day budgets. Unknown provider cost is charged conservatively."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from .storage import Store, budget_days, insert_once, quotas, reservations


@dataclass(frozen=True)
class Reservation:
    id: str
    user_id: int
    day: str
    amount: Decimal


class Budget:
    def __init__(self, store: Store, settings=None, *, daily_answer_limit=10, daily_budget_usd=Decimal("5"), now=None):
        self.store = store
        self.daily_answer_limit = int(getattr(settings, "daily_answer_limit", daily_answer_limit))
        self.daily_budget_usd = Decimal(str(getattr(settings, "daily_budget_usd", daily_budget_usd)))
        self.now = now or (lambda: datetime.now(ZoneInfo("Europe/Moscow")))

    def _day(self):
        moment = self.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        return moment.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()

    async def reserve(self, user_id: int, amount: Decimal) -> Reservation | None:
        amount = Decimal(str(amount)).quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Reservation amount must be finite and positive")
        day = self._day()
        async with self.store.lock, self.store.engine.begin() as conn:
            await insert_once(conn, budget_days, dict(day=day, spent=Decimal(0), reserved=Decimal(0)), ["day"])
            row = (await conn.execute(select(budget_days).where(budget_days.c.day == day).with_for_update())).mappings().one()
            await insert_once(conn, quotas, dict(day=day, user_id=user_id, answered=0, pending=0), ["day", "user_id"])
            quota = (await conn.execute(select(quotas).where(quotas.c.day == day, quotas.c.user_id == user_id).with_for_update())).mappings().one()
            if row["spent"] + row["reserved"] + amount > self.daily_budget_usd or quota["answered"] + quota["pending"] >= self.daily_answer_limit:
                return None
            reservation = Reservation(str(uuid4()), user_id, day, amount)
            await conn.execute(reservations.insert().values(id=reservation.id, user_id=user_id, day=day, amount=amount, state="reserved", answered=False))
            await conn.execute(update(budget_days).where(budget_days.c.day == day).values(reserved=budget_days.c.reserved + amount))
            await conn.execute(update(quotas).where(quotas.c.day == day, quotas.c.user_id == user_id).values(pending=quotas.c.pending + 1))
            return reservation

    async def settle(self, reservation: Reservation, actual: Decimal | None, answered: bool):
        if actual is not None:
            actual = Decimal(str(actual))
            if not actual.is_finite() or actual < 0:
                raise ValueError("Provider cost must be finite and nonnegative")
        async with self.store.lock, self.store.engine.begin() as conn:
            # Same global/day -> reservation lock order as reserve avoids cross-process deadlocks.
            await conn.execute(select(budget_days).where(budget_days.c.day == reservation.day).with_for_update())
            row = (await conn.execute(select(reservations).where(reservations.c.id == reservation.id).with_for_update())).mappings().one_or_none()
            if not row or row["state"] != "reserved":
                return
            cost = row["amount"] if actual is None else actual
            await conn.execute(update(budget_days).where(budget_days.c.day == row["day"]).values(spent=budget_days.c.spent + cost, reserved=budget_days.c.reserved - row["amount"]))
            await conn.execute(update(quotas).where(quotas.c.day == row["day"], quotas.c.user_id == row["user_id"]).values(pending=quotas.c.pending - 1, answered=quotas.c.answered + int(answered)))
            await conn.execute(update(reservations).where(reservations.c.id == row["id"]).values(actual=cost, state="settled", answered=answered))

    async def recover(self):
        """After a crash, outstanding calls may have reached the provider: charge their ceiling."""
        async with self.store.engine.connect() as conn:
            rows = (await conn.execute(select(reservations).where(reservations.c.state == "reserved"))).mappings().all()
        for row in rows:
            await self.settle(Reservation(row["id"], row["user_id"], row["day"], row["amount"]), None, answered=False)

    async def usage(self, user_id: int) -> dict:
        day = self._day()
        async with self.store.engine.connect() as conn:
            daily = (await conn.execute(select(budget_days).where(budget_days.c.day == day))).mappings().one_or_none()
            quota = (await conn.execute(select(quotas).where(quotas.c.day == day, quotas.c.user_id == user_id))).mappings().one_or_none()
            return {"day": day, "spent": daily["spent"] if daily else Decimal(0), "reserved": daily["reserved"] if daily else Decimal(0), "answered": quota["answered"] if quota else 0, "pending": quota["pending"] if quota else 0}
