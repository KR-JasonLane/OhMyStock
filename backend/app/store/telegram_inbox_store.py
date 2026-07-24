"""Durable Telegram inbox. Raw Telegram text is deliberately never accepted."""

from collections import Counter
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, Integer, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.store.models import (TelegramRejectedUpdateCounterRow, TelegramStateRow,
                              TelegramCommandExecutionRow, TelegramUpdateRow)
from app.store.telegram_common import (aware as _aware, command,
                                       exact_nonnegative_int, external_hash,
                                       hash64, positive_int, safe_identifier)


def _get(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


@dataclass(frozen=True)
class ClaimedUpdate:
    update_id: int
    operator_hash: str
    command: str
    argument_hash: str | None
    correlation_id: str
    received_at: datetime
    version: int


class TelegramInboxStore:
    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _insert_update(self, session: Session, item: Any) -> None:
        update_id = exact_nonnegative_int(_get(item, "update_id"), "update_id")
        received = _aware(_get(item, "received_at", self._now()))
        operator_hash = external_hash(
            str(_get(item, "operator_hash")), "operator_hash")
        argument_hash = _get(item, "argument_hash")
        if argument_hash is not None:
            hash64(argument_hash, "argument_hash")
        values = dict(
            update_id=update_id, operator_hash=operator_hash,
            command=command(str(_get(item, "command"))), argument_hash=argument_hash,
            status=str(_get(item, "status", "received")), owner=None, lease_until=None,
            version=0, correlation_id=safe_identifier(
                str(_get(item, "correlation_id", f"telegram-{update_id}")),
                "correlation_id"),
            received_at=received, finished_at=None)
        dialect = session.bind.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
            session.execute(insert(TelegramUpdateRow).values(
                **values).on_conflict_do_nothing(index_elements=["update_id"]))
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
            session.execute(insert(TelegramUpdateRow).values(
                **values).on_conflict_do_nothing(index_elements=["update_id"]))
        else:
            session.add(TelegramUpdateRow(**values))
            session.flush()

    def _persist_batch_and_offset_in_session(
            self, session: Session, updates: Iterable[Any],
            next_offset: int, now: datetime) -> None:
        for item in updates:
            self._insert_update(session, item)
        state = session.get(TelegramStateRow, "poll_offset")
        if state is None:
            values = dict(key="poll_offset", value=str(next_offset), updated_at=now)
            if session.bind.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
                session.execute(insert(TelegramStateRow).values(
                    **values).on_conflict_do_nothing(index_elements=["key"]))
            elif session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
                session.execute(insert(TelegramStateRow).values(
                    **values).on_conflict_do_nothing(index_elements=["key"]))
            else:
                session.add(TelegramStateRow(**values))
                session.flush()
        session.execute(update(TelegramStateRow).where(
            TelegramStateRow.key == "poll_offset",
            func.cast(TelegramStateRow.value, Integer) < next_offset).values(
            value=str(next_offset), updated_at=now))

    def persist_batch_and_offset(self, updates: Iterable[Any], next_offset: int) -> None:
        next_offset = exact_nonnegative_int(next_offset, "next_offset")
        now = _aware(self._now())
        with self._sessions.begin() as session:
            self._persist_batch_and_offset_in_session(
                session, updates, next_offset, now)

    def persist_poll_batch(
            self, updates: Iterable[Any], rejected_subject_hashes: Iterable[str],
            *, next_offset: int, minute: datetime,
            cardinality_limit: int = 300) -> None:
        """Commit accepted rows, bounded rejection counters and offset together."""
        next_offset = exact_nonnegative_int(next_offset, "next_offset")
        minute = _aware(minute)
        cardinality_limit = positive_int(cardinality_limit, "cardinality_limit")
        now = _aware(self._now())
        with self._sessions.begin() as session:
            self._persist_batch_and_offset_in_session(
                session, updates, next_offset, now)
            self._persist_rejected_in_session(
                session, minute, rejected_subject_hashes, cardinality_limit)

    def persist_leased_poll_batch(
            self, owner: str, generation: int, updates: Iterable[Any],
            rejected_subject_hashes: Iterable[str], *, next_offset: int,
            minute: datetime, cardinality_limit: int = 300) -> bool:
        """Commit a poll only while its owner still holds the exact lease generation."""
        owner = safe_identifier(owner, "owner")
        generation = positive_int(generation, "generation")
        next_offset = exact_nonnegative_int(next_offset, "next_offset")
        minute = _aware(minute)
        cardinality_limit = positive_int(cardinality_limit, "cardinality_limit")
        now = _aware(self._now())
        with self._sessions.begin() as session:
            # Serialize lease takeover with the fenced batch commit on
            # PostgreSQL. SQLite ignores FOR UPDATE but serializes writers.
            lease = session.get(
                TelegramStateRow, "poller_lease", with_for_update=True)
            lease_until = lease.lease_until if lease is not None else None
            if lease_until is not None and (
                    lease_until.tzinfo is None or lease_until.utcoffset() is None):
                lease_until = lease_until.replace(tzinfo=timezone.utc)
            if (lease is None or lease.lease_owner != owner
                    or int(lease.value) != generation
                    or lease_until is None or lease_until < now):
                return False
            self._persist_batch_and_offset_in_session(
                session, updates, next_offset, now)
            self._persist_rejected_in_session(
                session, minute, rejected_subject_hashes, cardinality_limit)
            return True

    def current_offset(self) -> int:
        with self._sessions() as session:
            row = session.get(TelegramStateRow, "poll_offset")
            return int(row.value) if row else 0

    def count_updates(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(TelegramUpdateRow)) or 0

    def claim_next(self, owner: str, lease_s: int = 30, *,
                   commands: Collection[str] | None = None) -> ClaimedUpdate | None:
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now, until = _aware(self._now()), _aware(self._now()) + timedelta(seconds=lease_s)
        with self._sessions.begin() as session:
            filters = [
                or_(TelegramUpdateRow.status == "received",
                    (TelegramUpdateRow.status == "claimed")
                    & (TelegramUpdateRow.lease_until < now))]
            if commands is not None:
                normalized = tuple(command(item) for item in commands)
                if not normalized:
                    return None
                filters.append(TelegramUpdateRow.command.in_(normalized))
            row = session.scalar(select(TelegramUpdateRow).where(*filters)
                .order_by(TelegramUpdateRow.update_id).limit(1))
            if row is None:
                return None
            won = session.execute(update(TelegramUpdateRow).where(
                TelegramUpdateRow.update_id == row.update_id,
                TelegramUpdateRow.version == row.version,
                or_(TelegramUpdateRow.status == "received",
                    (TelegramUpdateRow.status == "claimed")
                    & (TelegramUpdateRow.lease_until < now))).values(
                status="claimed", owner=owner, lease_until=until,
                version=row.version + 1).execution_options(
                    synchronize_session=False))
            if won.rowcount != 1:
                return None
            received_at = row.received_at
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
            return ClaimedUpdate(
                row.update_id, row.operator_hash, row.command,
                row.argument_hash, row.correlation_id, received_at,
                row.version + 1)

    def release_expired(self) -> int:
        now = _aware(self._now())
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramUpdateRow).where(
                TelegramUpdateRow.status == "claimed",
                TelegramUpdateRow.lease_until < now).values(
                status="received", owner=None, lease_until=None,
                version=TelegramUpdateRow.version + 1))
            return result.rowcount

    def finish(self, update_id: int, owner: str, version: int,
               *, rejected: bool = False) -> bool:
        update_id = exact_nonnegative_int(update_id, "update_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        now = _aware(self._now())
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramUpdateRow).where(
                TelegramUpdateRow.update_id == update_id,
                TelegramUpdateRow.status == "claimed",
                TelegramUpdateRow.owner == owner,
                TelegramUpdateRow.version == version).values(
                    status="rejected" if rejected else "completed", owner=None,
                    lease_until=None, finished_at=now, version=version + 1))
            return result.rowcount == 1

    def persist_rejected_batch(self, minute: datetime, subject_hashes: Iterable[str],
                               cardinality_limit: int) -> None:
        minute = _aware(minute)
        cardinality_limit = positive_int(cardinality_limit, "cardinality_limit")
        with self._sessions.begin() as session:
            self._persist_rejected_in_session(
                session, minute, subject_hashes, cardinality_limit)

    def _persist_rejected_in_session(
            self, session: Session, minute: datetime,
            subject_hashes: Iterable[str], cardinality_limit: int) -> None:
        iterator = iter(subject_hashes)
        checked = []
        for _ in range(101):
            try:
                checked.append(external_hash(next(iterator), "subject_hash"))
            except StopIteration:
                break
        if len(checked) > 100:
            raise ValueError("Telegram rejected batch exceeds 100 updates")
        counts = Counter(checked)
        total_key, overflow = "__total__", "__overflow__"
        total = session.get(TelegramRejectedUpdateCounterRow, (minute, total_key))
        previous_total = total.count if total else 0
        if total is None:
            total = TelegramRejectedUpdateCounterRow(
                minute=minute, subject_hash=total_key, count=0)
            session.add(total)
        total.count += len(checked)
        existing = set(session.scalars(select(
            TelegramRejectedUpdateCounterRow.subject_hash).where(
            TelegramRejectedUpdateCounterRow.minute == minute)))
        if previous_total >= 300:
            overflow_count = len(checked)
            new_subjects = []
            repeated = []
        else:
            # total and overflow buckets consume two bounded rows.
            available = max(
                0, cardinality_limit - 2 - len(existing - {total_key, overflow}))
            new_subjects = [
                subject for subject in counts if subject not in existing][:available]
            repeated = [item for item in counts if item in existing
                        and item not in {total_key, overflow}]
            overflow_count = sum(
                count for subject, count in counts.items()
                if subject not in repeated and subject not in new_subjects)
        for subject in repeated + new_subjects:
            if subject in existing:
                session.execute(update(TelegramRejectedUpdateCounterRow).where(
                    TelegramRejectedUpdateCounterRow.minute == minute,
                    TelegramRejectedUpdateCounterRow.subject_hash == subject).values(
                    count=TelegramRejectedUpdateCounterRow.count + counts[subject]))
            else:
                session.add(TelegramRejectedUpdateCounterRow(
                    minute=minute, subject_hash=subject, count=counts[subject]))
                existing.add(subject)
        if overflow_count:
            if overflow in existing:
                session.execute(update(TelegramRejectedUpdateCounterRow).where(
                    TelegramRejectedUpdateCounterRow.minute == minute,
                    TelegramRejectedUpdateCounterRow.subject_hash == overflow).values(
                    count=TelegramRejectedUpdateCounterRow.count + overflow_count))
            else:
                session.add(TelegramRejectedUpdateCounterRow(
                    minute=minute, subject_hash=overflow, count=overflow_count))

    def purge_rejected_before(self, before: datetime, limit: int = 1000) -> int:
        before = _aware(before)
        limit = positive_int(limit, "limit")
        with self._sessions.begin() as session:
            keys = list(session.execute(select(
                TelegramRejectedUpdateCounterRow.minute,
                TelegramRejectedUpdateCounterRow.subject_hash).where(
                    TelegramRejectedUpdateCounterRow.minute < before).order_by(
                    TelegramRejectedUpdateCounterRow.minute,
                    TelegramRejectedUpdateCounterRow.subject_hash).limit(limit)))
            for minute, subject in keys:
                session.query(TelegramRejectedUpdateCounterRow).filter(
                    TelegramRejectedUpdateCounterRow.minute == minute,
                    TelegramRejectedUpdateCounterRow.subject_hash == subject).delete(
                        synchronize_session=False)
            return len(keys)

    def rejected_counter_rows(self, minute: datetime) -> int:
        minute = _aware(minute)
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(
                TelegramRejectedUpdateCounterRow).where(
                    TelegramRejectedUpdateCounterRow.minute == minute)) or 0

    def rejected_total(self, minute: datetime) -> int:
        minute = _aware(minute)
        with self._sessions() as session:
            row = session.get(
                TelegramRejectedUpdateCounterRow, (minute, "__total__"))
            return row.count if row is not None else 0

    def can_poll(self, allowed_limit: int) -> bool:
        with self._sessions() as session:
            backlog = session.scalar(select(func.count()).select_from(
                TelegramUpdateRow).where(TelegramUpdateRow.status.in_(
                    ("received", "claimed")))) or 0
            return backlog < allowed_limit

    def pending_count(
            self, commands: Collection[str], *, limit: int | None = None) -> int:
        normalized = tuple(command(item) for item in commands)
        if not normalized:
            return 0
        with self._sessions() as session:
            count = session.scalar(select(func.count()).select_from(
                TelegramUpdateRow).where(
                    TelegramUpdateRow.status.in_(("received", "claimed")),
                    TelegramUpdateRow.command.in_(normalized))) or 0
        return min(count, limit) if limit is not None else count

    def acquire_poller_lease(self, owner: str, lease_s: int = 40) -> int | None:
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now = _aware(self._now())
        until = now + timedelta(seconds=lease_s)
        values = dict(
            key="poller_lease", value="0", lease_owner=None,
            lease_until=None, updated_at=now)
        with self._sessions.begin() as session:
            if session.bind.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert
                session.execute(insert(TelegramStateRow).values(
                    **values).on_conflict_do_nothing(index_elements=["key"]))
            elif session.bind.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
                session.execute(insert(TelegramStateRow).values(
                    **values).on_conflict_do_nothing(index_elements=["key"]))
            elif session.get(TelegramStateRow, "poller_lease") is None:
                session.add(TelegramStateRow(**values))
                session.flush()
            result = session.execute(update(TelegramStateRow).where(
                TelegramStateRow.key == "poller_lease",
                or_(TelegramStateRow.lease_until.is_(None),
                    TelegramStateRow.lease_until < now,
                    TelegramStateRow.lease_owner == owner)).values(
                lease_owner=owner, lease_until=until, updated_at=now,
                value=func.cast(TelegramStateRow.value, Integer) + 1))
            if result.rowcount != 1:
                return None
            row = session.get(TelegramStateRow, "poller_lease")
            return int(row.value)

    def release_poller_lease(
            self, owner: str, generation: int | None = None) -> bool:
        owner = safe_identifier(owner, "owner")
        if generation is not None:
            generation = positive_int(generation, "generation")
        with self._sessions.begin() as session:
            filters = [
                TelegramStateRow.key == "poller_lease",
                TelegramStateRow.lease_owner == owner,
            ]
            if generation is not None:
                filters.append(
                    func.cast(TelegramStateRow.value, Integer) == generation)
            result = session.execute(update(TelegramStateRow).where(*filters).values(
                lease_owner=None, lease_until=None, updated_at=_aware(self._now())))
            return result.rowcount == 1

    def purge_terminal_before(self, before: datetime, limit: int = 1000) -> int:
        before = _aware(before)
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(TelegramUpdateRow.update_id).where(
                TelegramUpdateRow.status.in_(("completed", "rejected")),
                ~TelegramUpdateRow.update_id.in_(
                    select(TelegramCommandExecutionRow.update_id)),
                TelegramUpdateRow.finished_at < before).order_by(
                    TelegramUpdateRow.update_id).limit(limit)))
            if not ids:
                return 0
            return session.query(TelegramUpdateRow).filter(
                TelegramUpdateRow.update_id.in_(ids)).delete(
                    synchronize_session=False)

    def release(self, update_id: int, owner: str, version: int) -> bool:
        update_id = exact_nonnegative_int(update_id, "update_id")
        version = exact_nonnegative_int(version, "version")
        owner = safe_identifier(owner, "owner")
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramUpdateRow).where(
                TelegramUpdateRow.update_id == update_id,
                TelegramUpdateRow.status == "claimed",
                TelegramUpdateRow.owner == owner,
                TelegramUpdateRow.version == version).values(
                    status="received", owner=None, lease_until=None,
                    version=version + 1))
            return result.rowcount == 1

    def seed_received(self, update_id: int) -> None:
        self.persist_batch_and_offset([{"update_id": update_id, "command": "status",
                                        "operator_hash": "v1:" + "0" * 64}],
                                      update_id + 1)

    def seed_allowed_updates(self, count: int) -> None:
        start = self.count_updates() + 1
        self.persist_batch_and_offset(
            ({"update_id": start + i, "command": "status",
              "operator_hash": "v1:" + "0" * 64} for i in range(count)),
            start + count)
