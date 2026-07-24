"""Append-only operational events and fixed-part notification delivery."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, case, func, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.orm import sessionmaker

from app.domain.notifications.models import OperationalEvent
from app.store.models import (NotificationDeliveryRow, NotificationOutboxRow,
                              OperationalEventRow, TelegramStateRow)
from app.store.telegram_common import (MAX_DELIVERY_PARTS,
                                       MAX_DELIVERY_TOTAL_BYTES,
                                       aware as _aware, canonical_json,
                                       delivery_body, exact_nonnegative_int,
                                       identifier, positive_int, safe_identifier)


@dataclass(frozen=True)
class Delivery:
    id: int
    outbox_id: int
    part_index: int
    total_parts: int
    body: str | None
    status: str
    attempt_count: int
    last_error_kind: str | None
    version: int
    priority: int
    occurred_at: datetime
    created_at: datetime


class NotificationStore:
    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _insert_outbox(session, values: dict[str, Any]) -> tuple[int, bool]:
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            statement = insert(NotificationOutboxRow).values(**values).on_conflict_do_nothing(
                index_elements=["idempotency_key"]).returning(NotificationOutboxRow.id)
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            statement = insert(NotificationOutboxRow).values(**values).on_conflict_do_nothing(
                index_elements=["idempotency_key"]).returning(NotificationOutboxRow.id)
        else:
            row = NotificationOutboxRow(**values)
            session.add(row)
            session.flush()
            return row.id, True
        inserted = session.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        existing = session.scalar(select(NotificationOutboxRow.id).where(
            NotificationOutboxRow.idempotency_key == values["idempotency_key"]))
        return existing, False

    def append_event(self, event: OperationalEvent) -> int | None:
        with self._sessions.begin() as session:
            return self.append_event_in_session(session, event)

    def append_event_in_session(self, session, event: OperationalEvent) -> int:
        _aware(event.occurred_at)
        row = OperationalEventRow(
            kind=event.kind, source_type=event.source_type,
            source_id=event.source_id, source_version=event.version,
            payload=canonical_json(event.payload_for_storage()),
            occurred_at=event.occurred_at)
        values = {
            "kind": row.kind, "source_type": row.source_type,
            "source_id": row.source_id, "source_version": row.source_version,
            "payload": row.payload, "occurred_at": row.occurred_at,
        }
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            session.execute(insert(OperationalEventRow).values(**values).on_conflict_do_nothing(
                index_elements=["source_type", "source_id", "source_version"]))
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            session.execute(insert(OperationalEventRow).values(**values).on_conflict_do_nothing(
                index_elements=["source_type", "source_id", "source_version"]))
        else:
            session.add(row)
            session.flush()
        return session.scalar(select(OperationalEventRow.id).where(
                OperationalEventRow.source_type == event.source_type,
                OperationalEventRow.source_id == event.source_id,
                OperationalEventRow.source_version == event.version))

    def operational_event_count(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(OperationalEventRow)) or 0

    def latest_operational_event(self) -> OperationalEvent:
        with self._sessions() as session:
            row = session.scalar(select(OperationalEventRow).order_by(
                OperationalEventRow.id.desc()).limit(1))
            if row is None:
                raise LookupError("no operational event")
            occurred_at = row.occurred_at
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            return OperationalEvent(
                kind=row.kind, source_type=row.source_type, source_id=row.source_id,
                version=row.source_version, payload=json.loads(row.payload),
                occurred_at=occurred_at)

    def events_after_in_session(self, session, event_id: int,
                                limit: int) -> list[OperationalEventRow]:
        return list(session.scalars(
            select(OperationalEventRow)
            .where(OperationalEventRow.id > event_id)
            .order_by(OperationalEventRow.id)
            .limit(limit)))

    def projector_checkpoint_in_session(self, session) -> int:
        row = session.get(TelegramStateRow, "event_projector_cursor")
        return int(row.value) if row is not None else 0

    def projector_checkpoint(self) -> int:
        with self._sessions() as session:
            return self.projector_checkpoint_in_session(session)

    def set_projector_checkpoint_in_session(self, session, event_id: int) -> None:
        row = session.get(TelegramStateRow, "event_projector_cursor")
        if row is None:
            session.add(TelegramStateRow(
                key="event_projector_cursor", value=str(event_id),
                lease_owner=None, lease_until=None, updated_at=_aware(self._now())))
            return
        row.value = str(event_id)
        row.updated_at = _aware(self._now())

    def rewind_projector_checkpoint(self, event_id: int) -> None:
        if event_id < 0:
            raise ValueError("event_id must be non-negative")
        with self._sessions.begin() as session:
            self.set_projector_checkpoint_in_session(session, event_id)

    def enqueue_outbox_in_session(
            self, session, idempotency_key: str, payload: Any, *, kind: str,
            priority: int = 0, occurred_at: datetime,
            bodies: Sequence[str] = ()) -> tuple[int, bool]:
        identifier(idempotency_key, "idempotency_key", 128)
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            # SQLite는 timezone=True 값을 naive로 되돌린다. 원천 행은 UTC로
            # 기록하므로 projector 경계에서 UTC를 복원한다.
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred = _aware(occurred_at)
        now = _aware(self._now())
        checked = self._checked_delivery_bodies(bodies, priority)
        outbox_id, created = self._insert_outbox(session, dict(
            idempotency_key=idempotency_key, kind=kind, priority=priority,
            sensitive=False, payload=canonical_json(payload), status="pending",
            next_attempt_at=now, occurred_at=occurred, created_at=now,
            sent_at=None, last_error_kind=None, retention_kind="standard",
            purge_at=None))
        if created:
            self._add_delivery_rows(session, outbox_id, checked, now)
        return outbox_id, created

    def project_operational_events(self, project, limit: int = 100) -> int:
        """project(event_id, OperationalEvent)의 outbox와 cursor를 원자 커밋.

        투영 정책은 domain callback으로 남기고, SQLAlchemy session과 ORM 행은
        store에 가둔다. callback이 예외를 내면 outbox와 checkpoint 모두 rollback된다.
        """
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._sessions.begin() as session:
            cursor = self.projector_checkpoint_in_session(session)
            rows = self.events_after_in_session(session, cursor, limit)
            inserted = 0
            for row in rows:
                occurred_at = row.occurred_at
                if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
                event = OperationalEvent(
                    kind=row.kind, source_type=row.source_type, source_id=row.source_id,
                    version=row.source_version, payload=json.loads(row.payload),
                    occurred_at=occurred_at)
                for projection in project(row.id, event):
                    _outbox_id, created = self.enqueue_outbox_in_session(
                        session, projection.idempotency_key, projection.payload,
                        kind=projection.kind, priority=projection.priority,
                        occurred_at=event.occurred_at,
                        bodies=tuple(part.text for part in projection.parts))
                    inserted += int(created)
                self.set_projector_checkpoint_in_session(session, row.id)
            return inserted

    def outbox_count_by_key(self, idempotency_key: str) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(
                NotificationOutboxRow).where(
                    NotificationOutboxRow.idempotency_key == idempotency_key)) or 0

    def materialize_missing_operational_deliveries(
            self, render: Callable[[str, dict[str, object]], tuple[str, ...]],
            limit: int = 100) -> int:
        """Recover Task-6-era operational outboxes that predate child rows."""
        limit = positive_int(limit, "limit")
        materialized = 0
        with self._sessions.begin() as session:
            child = aliased(NotificationDeliveryRow)
            rows = session.scalars(select(NotificationOutboxRow).where(
                NotificationOutboxRow.status == "pending",
                NotificationOutboxRow.idempotency_key.like("operational:%"),
                ~select(child.id).where(child.outbox_id == NotificationOutboxRow.id).exists()
            ).order_by(NotificationOutboxRow.id).limit(limit)).all()
            for row in rows:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == row.id).with_for_update())
                if outbox is None or outbox.status != "pending":
                    continue
                exists = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == outbox.id).limit(1))
                if exists is not None or outbox.payload is None:
                    continue
                payload = json.loads(outbox.payload)
                if not isinstance(payload, dict):
                    continue
                try:
                    bodies = self._checked_delivery_bodies(
                        render(outbox.kind, payload), outbox.priority)
                except ValueError:
                    # Legacy outboxes could contain a formerly accepted but now
                    # unsafe kind.  Preserve metadata for audit, stop retrying,
                    # and never turn that raw identifier into Telegram text.
                    outbox.status = "dead_letter"
                    outbox.last_error_kind = "unsupported_notification_kind"
                    continue
                self._add_delivery_rows(session, outbox.id, bodies, _aware(self._now()))
                materialized += 1
            return materialized

    def enqueue_outbox(self, idempotency_key: str, payload: Any, kind: str = "generic",
                       priority: int = 0, sensitive: bool = False,
                       occurred_at: datetime | None = None,
                       retention_kind: str = "standard") -> int:
        now = _aware(self._now())
        occurred = _aware(occurred_at) if occurred_at else now
        identifier(idempotency_key, "idempotency_key", 128)
        if retention_kind not in {"standard", "query", "digest"}:
            raise ValueError("invalid retention_kind")
        if sensitive and retention_kind == "standard":
            raise ValueError("sensitive outbox requires query or digest retention")
        purge_at = (now + timedelta(minutes=15) if retention_kind == "query"
                    else now + timedelta(hours=24) if retention_kind == "digest"
                    else None)
        with self._sessions.begin() as session:
            values = dict(
                idempotency_key=idempotency_key, kind=kind, priority=priority,
                sensitive=sensitive, payload=canonical_json(payload), status="pending",
                next_attempt_at=now, occurred_at=occurred, created_at=now,
                sent_at=None, last_error_kind=None, retention_kind=retention_kind,
                purge_at=purge_at)
            return self._insert_outbox(session, values)[0]

    def count_outbox(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(NotificationOutboxRow)) or 0

    def enqueue_parts(self, idempotency_key: str, bodies: Sequence[str],
                      sensitive: bool = False, payload: Any | None = None,
                      retention_kind: str | None = None, priority: int = 10,
                      occurred_at: datetime | None = None) -> int:
        now = _aware(self._now())
        occurred = _aware(occurred_at) if occurred_at else now
        if not bodies:
            raise ValueError("at least one delivery part is required")
        if len(bodies) > MAX_DELIVERY_PARTS:
            raise ValueError("delivery part count exceeds 64")
        identifier(idempotency_key, "idempotency_key", 128)
        checked = self._checked_delivery_bodies(bodies, priority)
        retention_kind = retention_kind or ("query" if sensitive else "standard")
        if retention_kind not in {"standard", "query", "digest"}:
            raise ValueError("invalid retention_kind")
        if sensitive and retention_kind == "standard":
            raise ValueError("sensitive outbox requires query or digest retention")
        purge_at = (now + timedelta(minutes=15) if retention_kind == "query"
                    else now + timedelta(hours=24) if retention_kind == "digest"
                    else None)
        with self._sessions.begin() as session:
            values = dict(
                idempotency_key=idempotency_key, kind="generic", priority=priority,
                sensitive=sensitive, payload=canonical_json(payload if payload is not None else {}),
                status="pending", next_attempt_at=now, occurred_at=occurred,
                created_at=now, sent_at=None, last_error_kind=None,
                retention_kind=retention_kind, purge_at=purge_at)
            outbox_id, created = self._insert_outbox(session, values)
            if not created:
                return outbox_id
            self._add_delivery_rows(session, outbox_id, checked, now)
            return outbox_id

    @staticmethod
    def _checked_delivery_bodies(bodies: Sequence[str], priority: int) -> list[str]:
        checked = [delivery_body(body) for body in bodies]
        if priority == 0 and any(len(body) > 4032 for body in checked):
            raise ValueError("critical delivery body must reserve delayed notice space")
        if sum(len(body.encode("utf-8")) for body in checked) > MAX_DELIVERY_TOTAL_BYTES:
            raise ValueError("delivery total exceeds 256 KiB")
        return checked

    @staticmethod
    def _add_delivery_rows(session, outbox_id: int, bodies: Sequence[str], now: datetime) -> None:
        total = len(bodies)
        for index, body in enumerate(bodies, start=1):
            session.add(NotificationDeliveryRow(
                outbox_id=outbox_id, part_index=index, total_parts=total,
                body=body, status="pending", owner=None, lease_until=None,
                attempt_count=0, created_at=now, first_attempt_at=None,
                next_attempt_at=now, sent_at=None, last_error_kind=None,
                last_http_status=None, telegram_message_id=None, version=0))

    def claim_deliveries(self, owner: str, limit: int = 100,
                         lease_s: int = 30) -> list[Delivery]:
        owner = safe_identifier(owner, "owner")
        limit = positive_int(limit, "limit")
        lease_s = positive_int(lease_s, "lease_s")
        now = _aware(self._now())
        until = now + timedelta(seconds=lease_s)
        claimed: list[Delivery] = []
        with self._sessions.begin() as session:
            sibling = aliased(NotificationDeliveryRow)
            rows = session.scalars(select(NotificationDeliveryRow).join(
                NotificationOutboxRow,
                NotificationOutboxRow.id == NotificationDeliveryRow.outbox_id).where(
                NotificationOutboxRow.status == "pending",
                NotificationDeliveryRow.next_attempt_at <= now,
                or_(NotificationOutboxRow.purge_at.is_(None),
                    NotificationOutboxRow.purge_at > now),
                or_(NotificationDeliveryRow.status == "pending",
                    (NotificationDeliveryRow.status == "sending")
                    & (NotificationDeliveryRow.lease_until < now)),
                ~select(sibling.id).where(
                    sibling.outbox_id == NotificationDeliveryRow.outbox_id,
                    sibling.part_index < NotificationDeliveryRow.part_index,
                    sibling.status != "sent").exists(),
                ~select(sibling.id).where(
                    sibling.outbox_id == NotificationDeliveryRow.outbox_id,
                    sibling.id != NotificationDeliveryRow.id,
                    sibling.status == "sending",
                    sibling.lease_until >= now).exists())
                .order_by(NotificationOutboxRow.priority,
                          NotificationOutboxRow.occurred_at,
                          NotificationDeliveryRow.part_index).limit(limit)).all()
            for row in rows:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == row.outbox_id).with_for_update())
                if outbox is None or outbox.status != "pending":
                    continue
                active_sibling = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == row.outbox_id,
                    NotificationDeliveryRow.id != row.id,
                    NotificationDeliveryRow.status == "sending",
                    NotificationDeliveryRow.lease_until >= now).limit(1))
                if active_sibling is not None:
                    continue
                earlier_unsent = session.scalar(select(NotificationDeliveryRow.id).where(
                    NotificationDeliveryRow.outbox_id == row.outbox_id,
                    NotificationDeliveryRow.part_index < row.part_index,
                    NotificationDeliveryRow.status != "sent").limit(1))
                if earlier_unsent is not None:
                    continue
                result = session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.id == row.id,
                    NotificationDeliveryRow.version == row.version,
                    or_(NotificationDeliveryRow.status == "pending",
                        (NotificationDeliveryRow.status == "sending")
                        & (NotificationDeliveryRow.lease_until < now))).values(
                        status="sending", owner=owner, lease_until=until,
                        first_attempt_at=func.coalesce(
                            NotificationDeliveryRow.first_attempt_at, now),
                        version=row.version + 1).execution_options(
                            synchronize_session=False))
                if result.rowcount == 1:
                    claimed.append(Delivery(
                        row.id, row.outbox_id, row.part_index, row.total_parts,
                        row.body, "sending", row.attempt_count, row.last_error_kind,
                        row.version + 1, outbox.priority,
                        _from_db_time(outbox.occurred_at),
                        _from_db_time(outbox.created_at)))
        return claimed

    def finish_delivery(self, delivery_id: int, owner: str, version: int, *,
                        telegram_message_id: int, sent_at: datetime | None = None) -> bool:
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        now = _aware(sent_at) if sent_at else _aware(self._now())
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                    status="sent", sent_at=now, telegram_message_id=telegram_message_id,
                    owner=None, lease_until=None, version=version + 1))
            if result.rowcount != 1:
                return False
            if outbox.sensitive:
                session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.id == delivery_id).values(body=None))
                outbox.payload = None
            self._finish_outbox_if_complete(session, delivery.outbox_id, now)
            return True

    def _finish_outbox_if_complete(self, session, outbox_id: int, now: datetime) -> None:
        remaining = session.scalar(select(func.count()).select_from(
            NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id,
                NotificationDeliveryRow.status != "sent")) or 0
        if remaining == 0:
            session.execute(update(NotificationOutboxRow).where(
                NotificationOutboxRow.id == outbox_id).values(
                status="sent", sent_at=now, payload=None))
            session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id).values(body=None))

    def retry_delivery(self, delivery_id: int, owner: str, version: int,
                   error_kind: str,
                   http_status: int | None, next_attempt_at: datetime | None = None) -> bool:
        now = _aware(self._now())
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        next_at = _aware(next_attempt_at) if next_attempt_at else now
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                status="pending", owner=None, lease_until=None,
                attempt_count=NotificationDeliveryRow.attempt_count + 1,
                next_attempt_at=next_at, last_error_kind=error_kind,
                last_http_status=http_status, version=version + 1))
            if result.rowcount != 1:
                return False
            outbox.next_attempt_at = next_at
            outbox.last_error_kind = error_kind
            return True

    def dead_letter_delivery(self, delivery_id: int, owner: str, version: int,
                             error_kind: str, http_status: int | None) -> bool:
        """Fence one failed part and atomically terminalize its parent outbox."""
        delivery_id = exact_nonnegative_int(delivery_id, "delivery_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRow, delivery_id)
            if delivery is None:
                return False
            outbox = session.scalar(select(NotificationOutboxRow).where(
                NotificationOutboxRow.id == delivery.outbox_id).with_for_update())
            if outbox is None or outbox.status != "pending":
                return False
            result = session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.id == delivery_id,
                NotificationDeliveryRow.status == "sending",
                NotificationDeliveryRow.owner == owner,
                NotificationDeliveryRow.version == version).values(
                    status="dead_letter", owner=None, lease_until=None,
                    last_error_kind=error_kind, last_http_status=http_status,
                    version=version + 1))
            if result.rowcount != 1:
                return False
            session.execute(update(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == delivery.outbox_id,
                NotificationDeliveryRow.id != delivery_id,
                NotificationDeliveryRow.status != "sent").values(
                    status="dead_letter", owner=None, lease_until=None,
                    last_error_kind=error_kind,
                    last_http_status=http_status,
                    version=NotificationDeliveryRow.version + 1))
            outbox.status = "dead_letter"
            outbox.last_error_kind = error_kind
            return True

    def load_deliveries(self, outbox_id: int) -> list[Delivery]:
        with self._sessions() as session:
            rows = session.scalars(select(NotificationDeliveryRow).where(
                NotificationDeliveryRow.outbox_id == outbox_id).order_by(
                    NotificationDeliveryRow.part_index)).all()
            outbox = session.get(NotificationOutboxRow, outbox_id)
            if outbox is None:
                return []
            return [Delivery(
                r.id, r.outbox_id, r.part_index, r.total_parts, r.body, r.status,
                r.attempt_count, r.last_error_kind, r.version, outbox.priority,
                _from_db_time(outbox.occurred_at),
                _from_db_time(outbox.created_at)) for r in rows]

    def purge_expired_sensitive(self, now: datetime, limit: int = 1000) -> int:
        now = _aware(now)
        limit = positive_int(limit, "limit")
        purged = 0
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(NotificationOutboxRow.id).where(
                NotificationOutboxRow.sensitive.is_(True),
                NotificationOutboxRow.purge_at <= now
            ).order_by(NotificationOutboxRow.id).limit(limit)))
            for outbox_id in ids:
                outbox = session.scalar(select(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == outbox_id).with_for_update())
                live = session.scalar(select(func.count()).select_from(
                    NotificationDeliveryRow).where(
                        NotificationDeliveryRow.outbox_id == outbox_id,
                        NotificationDeliveryRow.status == "sending",
                        NotificationDeliveryRow.lease_until > now)) or 0
                if live:
                    continue
                body_count = session.scalar(select(func.count()).select_from(
                    NotificationDeliveryRow).where(
                        NotificationDeliveryRow.outbox_id == outbox_id,
                        NotificationDeliveryRow.body.is_not(None))) or 0
                if outbox.payload is None and body_count == 0:
                    continue
                session.execute(update(NotificationDeliveryRow).where(
                    NotificationDeliveryRow.outbox_id == outbox_id,
                    NotificationDeliveryRow.body.is_not(None)).values(
                    body=None,
                    status=case(
                        (NotificationDeliveryRow.status == "sent", "sent"),
                        else_="dead_letter"),
                    owner=None, lease_until=None,
                    version=NotificationDeliveryRow.version + 1))
                session.execute(update(NotificationOutboxRow).where(
                    NotificationOutboxRow.id == outbox_id).values(
                    payload=None, status="dead_letter",
                    last_error_kind="sensitive_payload_expired"))
                purged += 1
            return purged

    def load_payload(self, outbox_id: int) -> Any | None:
        with self._sessions() as session:
            value = session.scalar(select(NotificationOutboxRow.payload).where(
                NotificationOutboxRow.id == outbox_id))
            return json.loads(value) if value is not None else None

    def load_delivery_bodies(self, outbox_id: int) -> list[str | None]:
        with self._sessions() as session:
            return list(session.scalars(select(NotificationDeliveryRow.body).where(
                NotificationDeliveryRow.outbox_id == outbox_id).order_by(
                    NotificationDeliveryRow.part_index)))

    def purge_retention(self, before: datetime, limit: int = 1000) -> int:
        """Delete only terminal metadata; unknown/unresolved and audit evidence remain."""
        before = _aware(before)
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(NotificationOutboxRow.id).where(
                NotificationOutboxRow.status == "sent",
                NotificationOutboxRow.sent_at < before).order_by(
                    NotificationOutboxRow.id).limit(limit)))
            if not ids:
                return 0
            session.query(NotificationDeliveryRow).filter(
                NotificationDeliveryRow.outbox_id.in_(ids)).delete(
                    synchronize_session=False)
            session.query(NotificationOutboxRow).filter(
                NotificationOutboxRow.id.in_(ids)).delete(synchronize_session=False)
            return len(ids)


def _from_db_time(value: datetime) -> datetime:
    """Restore SQLite's lost timezone marker; this table only stores UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value
