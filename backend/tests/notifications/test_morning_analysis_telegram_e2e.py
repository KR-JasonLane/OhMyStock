"""Synthetic end-to-end coverage for the morning-analysis Telegram flow.

The only fake is the Telegram transport.  SQLite stores, summary/query
services, dispatcher and durable sender remain real so this guards their
composition without calling Telegram, Kiwoom, broker, order or control paths.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.telegram_service import (AnalysisSummaryService, CommandDispatcher,
                                       CommandResponsePublisher,
                                       EphemeralResponseSender, OutboxSender,
                                       TelegramCircuit)
from app.domain.notifications.commands import CommandProcessor
from app.domain.notifications.digest import Digest, DigestAccount, DigestSection
from app.store.models import (AnalysisRunRow, AnalysisVerdictRow, Base,
                              InstrumentRow, NotificationOutboxRow, ScoreRunRow)
from app.store.notification_store import (AnalysisSummaryRunStore, DigestReportStore,
                                          NotificationStore)
from app.store.telegram_command_store import TelegramCommandStore
from app.store.telegram_inbox_store import TelegramInboxStore


KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 7, 27, 8, 25, tzinfo=KST)
OPERATOR_HASH = "v1:" + "a" * 64
CHAT_HASH = "v1:" + "b" * 64


class FakeTelegram:
    """Fixed local sender; it records the body handed to the adapter seam."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        self.messages.append((chat_id, text))
        return len(self.messages)


class ForbiddenControl:
    """A query regression must never touch an account, broker, or control path."""

    def __getattr__(self, name: str):
        raise AssertionError(f"query unexpectedly called control.{name}")


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _seed_succeeded_analysis(engine) -> None:
    with Session(engine) as session:
        score = ScoreRunRow(
            started_at=_utc(datetime(2026, 7, 27, 8, 0, tzinfo=KST)),
            finished_at=_utc(datetime(2026, 7, 27, 8, 1, tzinfo=KST)),
            status="succeeded", reference_date=date(2026, 7, 24),
            universe_count=2, stale_excluded=0, failure_reason=None, config="{}",
        )
        session.add(score)
        session.flush()
        session.add_all((
            InstrumentRow(
                symbol="005930", name="삼성전자", market="kospi",
                instrument_type="", state="", audit_info="", is_active=True,
                updated_at=_utc(NOW),
            ),
            InstrumentRow(
                symbol="000660", name="SK하이닉스", market="kospi",
                instrument_type="", state="", audit_info="", is_active=True,
                updated_at=_utc(NOW),
            ),
        ))
        session.add(AnalysisRunRow(
            id=71, started_at=_utc(datetime(2026, 7, 27, 8, 20, tzinfo=KST)),
            finished_at=_utc(datetime(2026, 7, 27, 8, 21, tzinfo=KST)),
            status="succeeded", score_run_id=score.id, model="synthetic",
            prompt_hash="synthetic", config="{}", regime="neutral",
            market_summary="합성 시장 요약", warnings=None, failure_reason=None,
            max_picks_advice=1, economist_fallback=False,
        ))
        session.add_all((
            AnalysisVerdictRow(
                run_id=71, symbol="005930", verdict="approve", confidence=0.9,
                reasons='["상승 추세"]', risk_flags='["변동성"]', picked=True,
                pick_rank=1,
            ),
            AnalysisVerdictRow(
                run_id=71, symbol="000660", verdict="approve", confidence=0.8,
                reasons='["차순위 근거"]', risk_flags='["차순위 위험"]', picked=False,
                pick_rank=None,
            ),
        ))
        session.commit()


async def _drain(sender: OutboxSender) -> None:
    while await sender.run_once():
        pass


def _outbox_count(store: NotificationStore, kind: str) -> int:
    with store._sessions() as session:
        return session.scalar(select(func.count()).select_from(NotificationOutboxRow).where(
            NotificationOutboxRow.kind == kind)) or 0


@pytest.mark.anyio
async def test_아침분석_자동알림과_조회는_실제저장소를거쳐_재기동에도_중복되지않는다(tmp_path):
    """Removing idempotency, swapping verdict classes, or re-querying control fails here."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'morning-e2e.db'}")
    Base.metadata.create_all(engine)
    _seed_succeeded_analysis(engine)
    notifications = NotificationStore(engine, now=lambda: NOW)
    reports = AnalysisSummaryRunStore(engine, "mock", now=lambda: NOW)
    first_service = AnalysisSummaryService(
        reports, notifications, run_environment="mock", now=lambda: NOW)
    telegram = FakeTelegram()
    sender = OutboxSender(
        notifications, telegram, chat_id=9001, worker_id="synthetic-sender",
        now=lambda: NOW, random_float=lambda: 0.0)

    # A succeeded analysis yields exactly one automatic, durable outbox item.
    assert await first_service.run_once() == 1
    assert _outbox_count(notifications, "analysis_summary") == 1
    await _drain(sender)
    automatic_body = telegram.messages[-1][1]
    assert "🎯 최종 후보\n005930 · 삼성전자" in automatic_body
    assert "📋 차순위 승인\n000660 · SK하이닉스" in automatic_body

    # Retain a sent 16:10 digest, then query both read-only reports through
    # actual inbox/command/dispatcher/publisher stores.
    retained = Digest(
        date(2026, 7, 27), "mock",
        DigestSection({"collection_status": "done"}, NOW),
        DigestSection({"order_count": 1}, NOW),
        DigestAccount(
            None, None, None, None, "unknown", "unavailable",
            ("account_snapshot",), None, None),
    )
    digest_parts = (
        "[retained-digest] [1/2]\n" + retained.body.split("\n", 2)[0],
        "[retained-digest] [2/2]\n" + "\n".join(retained.body.split("\n")[1:]),
    )
    notifications.materialize_digest(
        "digest:mock:2026-07-27",
        retained.payload, digest_parts, occurred_at=NOW)
    await _drain(sender)
    # A newer real-environment row must never cross into a mock `/digest` query.
    real_retained = Digest(
        date(2026, 7, 27), "real", retained.pipeline, retained.trading, retained.account)
    notifications.materialize_digest(
        "digest:real:2026-07-27",
        real_retained.payload, real_retained.body, occurred_at=NOW)
    await _drain(sender)

    inbox = TelegramInboxStore(engine, now=lambda: NOW)
    commands = TelegramCommandStore(engine, now=lambda: NOW)
    processor = CommandProcessor(
        inbox, commands, ForbiddenControl(), "synthetic-dispatcher",
        chat_hash=CHAT_HASH, now=lambda: NOW,
        analysis_reports=reports,
        digest_reports=DigestReportStore(notifications, "mock", now=lambda: NOW),
    )
    publisher = CommandResponsePublisher(
        notifications,
        EphemeralResponseSender(telegram, chat_id=9001, circuit=TelegramCircuit(),
                                now=lambda: NOW),
    )
    dispatcher = CommandDispatcher(
        inbox, processor, worker_id="synthetic-dispatcher", now=lambda: NOW,
        response_publisher=publisher,
    )
    inbox.persist_batch_and_offset((
        {"update_id": 101, "operator_hash": OPERATOR_HASH, "command": "analysis",
         "received_at": NOW},
        {"update_id": 102, "operator_hash": OPERATOR_HASH, "command": "digest",
         "received_at": NOW},
    ), 103)

    assert await dispatcher.tick_query() == 1
    assert await dispatcher.tick_query() == 1
    messages_before_query_delivery = len(telegram.messages)
    await _drain(sender)
    query_bodies = [text for _chat_id, text in telegram.messages[messages_before_query_delivery:]]
    assert "🧠 아침 AI 분석 완료 · 알림 환경 모의투자" in query_bodies[0]
    assert tuple(query_bodies[1:]) == digest_parts
    assert "🚨 실전" not in "\n".join(query_bodies[1:])
    assert _outbox_count(notifications, "generic") == 2

    # A same-tick retry and a freshly constructed service (restart) both see
    # the persisted idempotency key and must produce no second auto summary.
    assert await first_service.run_once() == 0
    restarted_service = AnalysisSummaryService(
        AnalysisSummaryRunStore(engine, "mock", now=lambda: NOW), notifications,
        run_environment="mock", now=lambda: NOW)
    assert await restarted_service.run_once() == 0
    assert _outbox_count(notifications, "analysis_summary") == 1
