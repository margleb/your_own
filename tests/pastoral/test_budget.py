from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from pastoral_bot.budget import Budget
from pastoral_bot.storage import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = Store(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_parallel_global_ceiling(store):
    budget = Budget(store, daily_budget_usd=Decimal("5"))
    results = await asyncio.gather(*(budget.reserve(user, Decimal("1")) for user in range(30)))
    admitted = [reservation for reservation in results if reservation]
    assert len(admitted) == 5
    await asyncio.gather(*(budget.settle(reservation, Decimal("1"), answered=True) for reservation in admitted))
    assert (await budget.usage(0))["spent"] == Decimal("5")
    assert await budget.reserve(999, Decimal("0.0001")) is None


@pytest.mark.asyncio
async def test_per_user_limit_includes_pending_reservations(store):
    budget = Budget(store)
    results = await asyncio.gather(*(budget.reserve(1, Decimal("0.01")) for _ in range(20)))
    admitted = [reservation for reservation in results if reservation]
    assert len(admitted) == 10
    assert (await budget.usage(1))["pending"] == 10
    for reservation in admitted:
        await budget.settle(reservation, Decimal("0.003"), answered=True)
    assert (await budget.usage(1))["answered"] == 10
    assert await budget.reserve(1, Decimal("0.01")) is None
    assert await budget.reserve(2, Decimal("0.01")) is not None


@pytest.mark.asyncio
async def test_unknown_cost_conservative_and_idempotent_settlement(store):
    budget = Budget(store)
    reservation = await budget.reserve(1, Decimal("0.10"))
    await budget.settle(reservation, None, answered=False)
    await budget.settle(reservation, Decimal("0"), answered=True)
    usage = await budget.usage(1)
    assert usage["spent"] == Decimal("0.10")
    assert usage["answered"] == 0
    assert usage["pending"] == 0


@pytest.mark.asyncio
async def test_turn_with_repair_costs_two_calls_but_one_answer(store):
    budget = Budget(store)
    reservation = await budget.reserve(1, Decimal("0.02"))
    await budget.settle(reservation, Decimal("0.006")+Decimal("0.007"), answered=True)
    usage = await budget.usage(1)
    assert usage["spent"] == Decimal("0.013")
    assert usage["reserved"] == 0
    assert usage["answered"] == 1


@pytest.mark.asyncio
async def test_moscow_midnight_late_settle_and_crash_recovery(store):
    moment = [datetime(2026, 10, 3, 20, 59, tzinfo=timezone.utc)]
    budget = Budget(store, now=lambda: moment[0])
    old = await budget.reserve(1, Decimal("1"))
    assert old.day == "2026-10-03"
    moment[0] = datetime(2026, 10, 3, 21, 1, tzinfo=timezone.utc)
    current = await budget.reserve(1, Decimal("2"))
    assert current.day == "2026-10-04"
    await budget.settle(old, Decimal("0.2"), answered=True)
    assert (await budget.usage(1))["spent"] == 0
    await budget.recover()
    usage = await budget.usage(1)
    assert usage["spent"] == Decimal("2")
    assert usage["reserved"] == 0
    assert usage["answered"] == 0
    await budget.recover()
    assert (await budget.usage(1))["spent"] == Decimal("2")
