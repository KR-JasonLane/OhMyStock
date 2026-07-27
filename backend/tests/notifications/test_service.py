"""Durable service boundaries for morning analysis Telegram summaries."""

import asyncio
import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.telegram_service import AnalysisSummaryService
from app.domain.notifications.analysis_summary import (
    AnalysisVerdictSummary,
    MorningAnalysisSummary,
)


NOW = datetime(2026, 7, 27, 0, 5, tzinfo=timezone.utc)


def _summary(run_id: int) -> MorningAnalysisSummary:
    return MorningAnalysisSummary(
        run_id=run_id,
        run_environment="mock",
        regime="neutral",
        market_summary="방향성 확인 중",
        max_picks_advice=1,
        score_reference_date=date(2026, 7, 24),
        started_at=datetime(2026, 7, 27, 0, run_id, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 27, 0, run_id + 1, tzinfo=timezone.utc),
        verdicts=(AnalysisVerdictSummary(
            symbol=f"00000{run_id}", name=f"종목 {run_id}", verdict="approve",
            confidence=0.7, reasons=("근거",), risk_flags=("위험",),
            picked=True, pick_rank=1,
        ),),
    )


class SummaryRuns:
    def __init__(self, summaries: tuple[MorningAnalysisSummary, ...]) -> None:
        self.summaries = summaries
        self.calls: list[tuple[tuple[int, ...], int]] = []
        self.thread_ids: list[int] = []

    def pending_succeeded_today(self, generated_run_ids, limit):
        self.thread_ids.append(threading.get_ident())
        generated = tuple(sorted(generated_run_ids))
        self.calls.append((generated, limit))
        return tuple(summary for summary in self.summaries
                     if summary.run_id not in generated)[:limit]


class SummaryStore:
    def __init__(self) -> None:
        self.generated: set[int] = set()
        self.materialized: list[tuple[int, tuple[str, ...], datetime]] = []
        self.thread_ids: list[int] = []

    def generated_analysis_run_ids(self, run_environment):
        assert run_environment == "mock"
        self.thread_ids.append(threading.get_ident())
        return tuple(sorted(self.generated))

    def materialize_analysis_summary(self, summary, bodies, *, occurred_at):
        self.thread_ids.append(threading.get_ident())
        self.materialized.append((summary.run_id, tuple(bodies), occurred_at))
        created = summary.run_id not in self.generated
        self.generated.add(summary.run_id)
        return SimpleNamespace(created=created)


@pytest.mark.anyio
async def test_analysis_summary는_오래된순으로_materialize하고_재기동뒤_중복생성하지않는다():
    runs = SummaryRuns((_summary(1), _summary(2)))
    store = SummaryStore()
    event_loop_thread = threading.get_ident()
    service = AnalysisSummaryService(
        runs, store, run_environment="mock", now=lambda: NOW)

    assert await service.run_once() == 2
    assert [item[0] for item in store.materialized] == [1, 2]
    assert all(bodies for _run_id, bodies, _occurred_at in store.materialized)
    assert [item[2] for item in store.materialized] == [NOW, NOW]
    assert await service.run_once() == 0
    assert runs.calls == [((), 10), ((1, 2), 10)]
    assert all(thread_id != event_loop_thread for thread_id in (
        runs.thread_ids + store.thread_ids))
    assert service.snapshot() == {
        "state": "running", "last_created": 0, "backoff_reason": None,
    }


@pytest.mark.anyio
async def test_analysis_summary_read실패는_DB변경없이_tick을실패시킨다():
    class FailingRuns(SummaryRuns):
        def pending_succeeded_today(self, generated_run_ids, limit):
            super().pending_succeeded_today(generated_run_ids, limit)
            raise RuntimeError("read model unavailable")

    store = SummaryStore()
    service = AnalysisSummaryService(
        FailingRuns((_summary(1),)), store, run_environment="mock", now=lambda: NOW)

    with pytest.raises(RuntimeError, match="read model unavailable"):
        await service.run_once()

    assert store.materialized == []
    assert service.snapshot() == {
        "state": "running", "last_created": 0, "backoff_reason": "internal_error",
    }


@pytest.mark.anyio
async def test_analysis_summary_부분실패는_첫_outbox를보존하고_다음tick에복구한다():
    class FlakyStore(SummaryStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def materialize_analysis_summary(self, summary, bodies, *, occurred_at):
            if summary.run_id == 2 and self.fail_once:
                self.fail_once = False
                raise RuntimeError("outbox write unavailable")
            return super().materialize_analysis_summary(
                summary, bodies, occurred_at=occurred_at)

    runs = SummaryRuns((_summary(1), _summary(2)))
    store = FlakyStore()
    service = AnalysisSummaryService(
        runs, store, run_environment="mock", now=lambda: NOW)

    with pytest.raises(RuntimeError, match="outbox write unavailable"):
        await service.run_once()
    assert [item[0] for item in store.materialized] == [1]

    assert await service.run_once() == 1
    assert [item[0] for item in store.materialized] == [1, 2]
    assert runs.calls == [((), 10), ((1,), 10)]


@pytest.mark.anyio
async def test_analysis_summary는_Telegram_adapter_의존성없이_요약만_durable화한다():
    runs = SummaryRuns((_summary(1),))
    store = SummaryStore()
    service = AnalysisSummaryService(
        runs, store, run_environment="mock", now=lambda: NOW)

    assert "telegram" not in service.__dict__
    assert await service.run_once() == 1
    assert [item[0] for item in store.materialized] == [1]


@pytest.mark.anyio
async def test_analysis_summary는_종료fence_뒤_늦게끝난_read를_materialize하지않는다():
    entered = threading.Event()
    release = threading.Event()

    class BlockingRuns(SummaryRuns):
        def pending_succeeded_today(self, generated_run_ids, limit):
            entered.set()
            assert release.wait(timeout=1)
            return super().pending_succeeded_today(generated_run_ids, limit)

    store = SummaryStore()
    service = AnalysisSummaryService(
        BlockingRuns((_summary(1),)), store,
        run_environment="mock", now=lambda: NOW)
    tick = asyncio.create_task(service.run_once())
    assert await asyncio.to_thread(entered.wait, 1)

    await service.begin_shutdown()
    release.set()

    assert await tick == 0
    assert store.materialized == []


@pytest.mark.anyio
async def test_analysis_summary_종료fence는_진행중_materialize를기다리지않는다():
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(SummaryStore):
        def materialize_analysis_summary(self, summary, bodies, *, occurred_at):
            entered.set()
            assert release.wait(timeout=1)
            return super().materialize_analysis_summary(
                summary, bodies, occurred_at=occurred_at)

    store = BlockingStore()
    service = AnalysisSummaryService(
        SummaryRuns((_summary(1),)), store,
        run_environment="mock", now=lambda: NOW)
    tick = asyncio.create_task(service.run_once())
    assert await asyncio.to_thread(entered.wait, 1)

    await asyncio.wait_for(service.begin_shutdown(), timeout=0.05)
    release.set()
    assert await tick == 1


@pytest.mark.anyio
async def test_digest는_종료fence_뒤_늦게끝난_build를_materialize하지않는다():
    from app.core.telegram_service import DigestService

    entered = asyncio.Event()
    release = asyncio.Event()
    materialized = []

    class Planner:
        def due_dates(self, _now):
            return (date(2026, 7, 27),)

        def mark_generated(self, _day):
            raise AssertionError("stopped digest must not mark generated")

    class Builder:
        async def build(self, _day):
            entered.set()
            await release.wait()
            return SimpleNamespace(
                idempotency_key="digest:mock:2026-07-27", payload={}, bodies=("body",))

    class Store:
        def materialize_digest(self, *args, **kwargs):
            materialized.append((args, kwargs))
            return SimpleNamespace(created=True)

    service = DigestService(Planner(), Builder(), Store(), now=lambda: NOW)
    tick = asyncio.create_task(service.run_once())
    await entered.wait()

    await service.begin_shutdown()
    release.set()

    assert await tick == 0
    assert materialized == []
