"""Durable Telegram inbox. Raw Telegram text is deliberately never accepted."""

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, Integer, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.store.models import (TelegramRejectedUpdateCounterRow, TelegramStateRow,
                              TelegramCommandExecutionRow, TelegramUpdateRow)
from app.store.telegram_common import (aware as _aware, command,
                                       exact_nonnegative_int, hash64,
                                       positive_int, safe_identifier)


def _get(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


@dataclass(frozen=True)
class ClaimedUpdate:
    update_id: int
    operator_hash: str
    command: str
    argument_hash: str | None
    correlation_id: str
    version: int


class TelegramInboxStore:
    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _insert_update(self, session: Session, item: Any) -> None:
        update_id = exact_nonnegative_int(_get(item, "update_id"), "update_id")
        received = _aware(_get(item, "received_at", self._now()))
        operator_hash = hash64(str(_get(item, "operator_hash")), "operator_hash")
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

    def persist_batch_and_offset(self, updates: Iterable[Any], next_offset: int) -> None:
        next_offset = exact_nonnegative_int(next_offset, "next_offset")
        now = _aware(self._now())
        with self._sessions.begin() as session:
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

    def current_offset(self) -> int:
        with self._sessions() as session:
            row = session.get(TelegramStateRow, "poll_offset")
            return int(row.value) if row else 0

    def count_updates(self) -> int:
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(TelegramUpdateRow)) or 0

    def claim_next(self, owner: str, lease_s: int = 30) -> ClaimedUpdate | None:
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now, until = _aware(self._now()), _aware(self._now()) + timedelta(seconds=lease_s)
        with self._sessions.begin() as session:
            row = session.scalar(select(TelegramUpdateRow).where(
                or_(TelegramUpdateRow.status == "received",
                    (TelegramUpdateRow.status == "claimed")
                    & (TelegramUpdateRow.lease_until < now)))
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
            return ClaimedUpdate(row.update_id, row.operator_hash, row.command,
                                 row.argument_hash, row.correlation_id, row.version + 1)

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
        iterator = iter(subject_hashes)
        checked = []
        for _ in range(101):
            try:
                checked.append(hash64(next(iterator), "subject_hash"))
            except StopIteration:
                break
        if len(checked) > 100:
            raise ValueError("Telegram rejected batch exceeds 100 updates")
        counts = Counter(checked)
        total_key, overflow = "__total__", "__overflow__"
        with self._sessions.begin() as session:
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

    def can_poll(self, allowed_limit: int) -> bool:
        with self._sessions() as session:
            backlog = session.scalar(select(func.count()).select_from(
                TelegramUpdateRow).where(TelegramUpdateRow.status.in_(
                    ("received", "claimed")))) or 0
            return backlog < allowed_limit

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
                                        "operator_hash": "0" * 64}],
                                      update_id + 1)

    def seed_allowed_updates(self, count: int) -> None:
        start = self.count_updates() + 1
        self.persist_batch_and_offset(
            ({"update_id": start + i, "command": "status",
              "operator_hash": "0" * 64} for i in range(count)),
            start + count)
