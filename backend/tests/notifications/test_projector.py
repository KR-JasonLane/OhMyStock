from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.domain.notifications.models import OperationalEvent
from app.domain.notifications.projector import NotificationProjector
from app.store.models import Base
from app.store.notification_store import NotificationStore

NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


@pytest.fixture
def projector(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'projector.db'}")
    Base.metadata.create_all(engine)
    store = NotificationStore(engine, now=lambda: NOW)
    return NotificationProjector(store), store


def test_projector는_checkpoint와_outbox를_원자적으로_전진한다(projector):
    projector, store = projector
    event_id = store.append_event(OperationalEvent(
        "entry_filled", "trade_order", 9, "1", {}, NOW))
    assert projector.project_batch() == 1
    assert projector.checkpoint() == event_id
    projector.rewind_checkpoint(event_id - 1)
    assert projector.project_batch() == 0
    assert projector.outbox_count(f"operational:{event_id}:entry_filled") == 1


@pytest.mark.parametrize("remaining_order_state",
                         ["open", "cancel_pending", "cancelled", "unknown"])
def test_partial_fill메시지는_미체결상태를_반드시_표시한다(
        projector, remaining_order_state):
    projector, _store = projector
    message = projector.project_partial_fill(
        remaining_qty=7, remaining_order_state=remaining_order_state)
    assert remaining_order_state in message.text
