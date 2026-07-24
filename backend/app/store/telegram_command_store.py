"""Confirmation and durable command intent store."""

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.store.models import (TelegramCommandAuditRow, TelegramCommandExecutionRow,
                              TelegramConfirmationLockRow, TelegramConfirmationRow,
                              TelegramUpdateRow)
from app.store.telegram_common import (aware as _aware, canonical_json,
                                       command as valid_command, external_hash,
                                       hash64,
                                       exact_nonnegative_int, identifier, positive_int,
                                       safe_identifier)


@dataclass(frozen=True)
class IssuedConfirmation:
    id: int
    raw_token: str
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedIntent:
    id: str
    update_id: int
    command: str
    state_fingerprint: str
    targets: Any
    version: int


@dataclass(frozen=True)
class PendingConfirmation:
    command: str
    state_fingerprint: str


class TelegramCommandStore:
    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def issue_confirmation(self, operator_hash: str, chat_hash: str, command: str,
                           state_fingerprint: str, expires_in_s: int = 120) -> IssuedConfirmation:
        del expires_in_s  # security invariant: callers cannot weaken or extend the TTL.
        now, token = _aware(self._now()), secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        external_hash(operator_hash, "operator_hash")
        external_hash(chat_hash, "chat_hash")
        valid_command(command)
        identifier(state_fingerprint, "state_fingerprint", 128)
        with self._sessions.begin() as session:
            if session.get(TelegramConfirmationLockRow, operator_hash) is None:
                try:
                    with session.begin_nested():
                        session.add(TelegramConfirmationLockRow(operator_hash=operator_hash))
                        session.flush()
                except IntegrityError:
                    pass
            session.scalar(select(TelegramConfirmationLockRow).where(
                TelegramConfirmationLockRow.operator_hash == operator_hash).with_for_update())
            session.execute(update(TelegramConfirmationRow).where(
                TelegramConfirmationRow.operator_hash == operator_hash,
                TelegramConfirmationRow.consumed_at.is_(None)).values(consumed_at=now))
            row = TelegramConfirmationRow(
                token_hash=digest, operator_hash=operator_hash, chat_hash=chat_hash,
                command=command, state_fingerprint=state_fingerprint,
                expires_at=now + timedelta(seconds=120), consumed_at=None, created_at=now)
            session.add(row)
            session.flush()
            return IssuedConfirmation(row.id, token, row.expires_at)

    def consume_and_create_intent(
            self, argument_hash: str, operator_hash: str, chat_hash: str,
            command: str, current_fingerprint: str, now: datetime,
            *, update_id: int, targets: Any = ()) -> ClaimedIntent | None:
        now = _aware(now)
        update_id = exact_nonnegative_int(update_id, "update_id")
        hash64(argument_hash, "argument_hash")
        external_hash(operator_hash, "operator_hash")
        external_hash(chat_hash, "chat_hash")
        valid_command(command)
        with self._sessions.begin() as session:
            update_row = session.get(TelegramUpdateRow, update_id)
            if update_row is None:
                raise ValueError("update_id must reference an existing inbox update")
            confirmation = session.scalar(select(TelegramConfirmationRow).where(
                TelegramConfirmationRow.token_hash == argument_hash,
                TelegramConfirmationRow.operator_hash == operator_hash,
                TelegramConfirmationRow.chat_hash == chat_hash,
                TelegramConfirmationRow.command == command,
                TelegramConfirmationRow.state_fingerprint == current_fingerprint,
                TelegramConfirmationRow.expires_at > now,
                TelegramConfirmationRow.consumed_at.is_(None)).limit(1))
            if confirmation is None:
                return None
            won = session.execute(update(TelegramConfirmationRow).where(
                TelegramConfirmationRow.id == confirmation.id,
                TelegramConfirmationRow.token_hash == argument_hash,
                TelegramConfirmationRow.operator_hash == operator_hash,
                TelegramConfirmationRow.chat_hash == chat_hash,
                TelegramConfirmationRow.command == command,
                TelegramConfirmationRow.state_fingerprint == current_fingerprint,
                TelegramConfirmationRow.expires_at > now,
                TelegramConfirmationRow.consumed_at.is_(None)).values(
                    consumed_at=now).execution_options(synchronize_session=False))
            if won.rowcount != 1:
                return None
            # The same durable ID is passed into TradingService, whose public
            # control contract permits only `[A-Za-z0-9_-]`.
            intent_id = f"telegram_command_confirmation_{confirmation.id}"
            row = TelegramCommandExecutionRow(
                id=intent_id, update_id=update_id, confirmation_id=confirmation.id,
                command=command, state_fingerprint=current_fingerprint,
                targets_json=canonical_json(targets), status="pending", owner=None,
                lease_until=None, version=0, failure_kind=None, created_at=now,
                finished_at=None)
            session.add(row)
            return ClaimedIntent(intent_id, update_id, command, current_fingerprint, targets, 0)

    def create_intent_for_update(self, update_id: int, command: str,
                                 state_fingerprint: str = "not_applicable",
                                 targets: Any = ()) -> ClaimedIntent:
        """Create the durable idempotency record before an immediate command runs.

        The primary key is derived only from the Telegram update, so a recovered
        inbox row observes the original intent instead of creating a second side
        effect.  This is deliberately separate from confirmation consumption:
        that path has a confirmation-derived ID and must atomically consume it.
        """
        update_id = exact_nonnegative_int(update_id, "update_id")
        valid_command(command)
        identifier(state_fingerprint, "state_fingerprint", 128)
        now = _aware(self._now())
        intent_id = f"telegram_command_update_{update_id}"
        targets_json = canonical_json(targets)
        with self._sessions.begin() as session:
            if session.get(TelegramUpdateRow, update_id) is None:
                raise ValueError("update_id must reference an existing inbox update")
            existing = session.get(TelegramCommandExecutionRow, intent_id)
            if existing is None:
                row = TelegramCommandExecutionRow(
                    id=intent_id, update_id=update_id, confirmation_id=None,
                    command=command, state_fingerprint=state_fingerprint,
                    targets_json=targets_json, status="pending", owner=None,
                    lease_until=None, version=0, failure_kind=None,
                    created_at=now, finished_at=None)
                session.add(row)
                return ClaimedIntent(intent_id, update_id, command, state_fingerprint, targets, 0)
            import json
            return ClaimedIntent(existing.id, existing.update_id, existing.command,
                                 existing.state_fingerprint,
                                 json.loads(existing.targets_json), existing.version)

    def pending_confirmation(self, argument_hash: str, operator_hash: str,
                             chat_hash: str, now: datetime) -> PendingConfirmation | None:
        """Return only the non-secret context needed to validate `/confirm`."""
        now = _aware(now)
        hash64(argument_hash, "argument_hash")
        external_hash(operator_hash, "operator_hash")
        external_hash(chat_hash, "chat_hash")
        with self._sessions() as session:
            row = session.scalar(select(TelegramConfirmationRow).where(
                TelegramConfirmationRow.token_hash == argument_hash,
                TelegramConfirmationRow.operator_hash == operator_hash,
                TelegramConfirmationRow.chat_hash == chat_hash,
                TelegramConfirmationRow.expires_at > now,
                TelegramConfirmationRow.consumed_at.is_(None)).limit(1))
            if row is None:
                return None
            return PendingConfirmation(row.command, row.state_fingerprint)

    def intent_count(self, confirmation_id: int) -> int:
        from sqlalchemy import func
        with self._sessions() as session:
            return session.scalar(select(func.count()).select_from(
                TelegramCommandExecutionRow).where(
                    TelegramCommandExecutionRow.confirmation_id == confirmation_id)) or 0

    def claim_intent(self, owner: str, lease_s: int = 30) -> ClaimedIntent | None:
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now, until = _aware(self._now()), _aware(self._now()) + timedelta(seconds=lease_s)
        with self._sessions.begin() as session:
            row = session.scalar(select(TelegramCommandExecutionRow).where(or_(
                TelegramCommandExecutionRow.status == "pending",
                (TelegramCommandExecutionRow.status == "claimed")
                & (TelegramCommandExecutionRow.lease_until < now))).order_by(
                    TelegramCommandExecutionRow.created_at,
                    TelegramCommandExecutionRow.id).limit(1))
            if row is None:
                return None
            previous = row.version
            won = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.id == row.id,
                TelegramCommandExecutionRow.version == previous,
                TelegramCommandExecutionRow.status.in_(("pending", "claimed")),
                or_(TelegramCommandExecutionRow.status == "pending",
                    TelegramCommandExecutionRow.lease_until < now)).values(
                    status="claimed", owner=owner, lease_until=until,
                    version=previous + 1))
            if won.rowcount != 1:
                return None
            import json
            return ClaimedIntent(row.id, row.update_id, row.command, row.state_fingerprint,
                                 json.loads(row.targets_json), previous + 1)

    def claim_intent_by_id(self, intent_id: str, owner: str,
                           lease_s: int = 30) -> ClaimedIntent | None:
        """Claim one known intent; a worker must never execute a neighbour's row."""
        safe_identifier(intent_id, "intent_id")
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now, until = _aware(self._now()), _aware(self._now()) + timedelta(seconds=lease_s)
        with self._sessions.begin() as session:
            row = session.get(TelegramCommandExecutionRow, intent_id)
            if row is None or not (
                row.status == "pending" or
                (row.status == "claimed" and row.lease_until < now)
            ):
                return None
            previous = row.version
            won = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.id == intent_id,
                TelegramCommandExecutionRow.version == previous,
                or_(TelegramCommandExecutionRow.status == "pending",
                    (TelegramCommandExecutionRow.status == "claimed")
                    & (TelegramCommandExecutionRow.lease_until < now))).values(
                        status="claimed", owner=owner, lease_until=until,
                        version=previous + 1).execution_options(
                            synchronize_session=False))
            if won.rowcount != 1:
                return None
            import json
            return ClaimedIntent(row.id, row.update_id, row.command, row.state_fingerprint,
                                 json.loads(row.targets_json), previous + 1)

    def _transition(self, intent_id: str, owner: str, version: int,
                    source: tuple[str, ...], target: str,
                    failure_kind: str | None = None, terminal: bool = False) -> bool:
        now = _aware(self._now())
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.id == intent_id,
                TelegramCommandExecutionRow.owner == owner,
                TelegramCommandExecutionRow.version == version,
                TelegramCommandExecutionRow.status.in_(source)).values(
                    status=target, owner=None if terminal or target == "unknown" else owner,
                    lease_until=None if terminal or target == "unknown" else
                    TelegramCommandExecutionRow.lease_until,
                    version=version + 1, failure_kind=failure_kind,
                    finished_at=now if terminal else None))
            return result.rowcount == 1

    def mark_running(self, intent_id: str, owner: str, version: int) -> bool:
        return self._transition(intent_id, owner, version, ("claimed",), "running")

    def mark_terminal(self, intent_id: str, owner: str, version: int,
                      status: str, failure_kind: str | None = None) -> bool:
        if status not in {"succeeded", "failed", "needs_attention"}:
            raise ValueError("invalid terminal status")
        return self._transition(intent_id, owner, version,
                                ("running", "reconciling"), status,
                                failure_kind, terminal=True)

    def mark_unknown(self, intent_id: str, owner: str, version: int) -> bool:
        return self._transition(intent_id, owner, version, ("running",), "unknown",
                                "process_crash")

    def renew_running_lease(self, intent_id: str, owner: str, version: int,
                            lease_s: int = 30) -> int | None:
        """Extend one live execution lease and return its new version fence."""
        safe_identifier(intent_id, "intent_id")
        owner = safe_identifier(owner, "owner")
        version = exact_nonnegative_int(version, "version")
        lease_s = positive_int(lease_s, "lease_s")
        now = _aware(self._now())
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.id == intent_id,
                TelegramCommandExecutionRow.status == "running",
                TelegramCommandExecutionRow.owner == owner,
                TelegramCommandExecutionRow.version == version).values(
                    lease_until=now + timedelta(seconds=lease_s),
                    version=version + 1))
            return version + 1 if result.rowcount == 1 else None

    def intent_status(self, intent_id: str) -> str | None:
        with self._sessions() as session:
            row = session.get(TelegramCommandExecutionRow, intent_id)
            return None if row is None else row.status

    def intent_for_update(
            self, update_id: int) -> tuple[ClaimedIntent, str] | None:
        """Read an execution before retrying a consumed confirmation update."""
        update_id = exact_nonnegative_int(update_id, "update_id")
        with self._sessions() as session:
            row = session.scalar(select(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.update_id == update_id).limit(1))
            if row is None:
                return None
            import json
            return (
                ClaimedIntent(
                    row.id, row.update_id, row.command, row.state_fingerprint,
                    json.loads(row.targets_json), row.version),
                row.status,
            )

    def record_audit(self, update_id: int, intent_id: str | None, event: str,
                     result: str, error_kind: str | None = None) -> None:
        update_id = exact_nonnegative_int(update_id, "update_id")
        identifier(event, "event", 32)
        identifier(result, "result", 24)
        if intent_id is not None:
            safe_identifier(intent_id, "intent_id")
        if error_kind is not None:
            identifier(error_kind, "error_kind", 64)
        with self._sessions.begin() as session:
            session.add(TelegramCommandAuditRow(
                update_id=update_id, intent_id=intent_id, event=event,
                result=result, error_kind=error_kind, ts=_aware(self._now())))

    def audit_results(self) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(select(TelegramCommandAuditRow.result).order_by(
                TelegramCommandAuditRow.id)))

    def expire_running_to_unknown(self) -> int:
        now = _aware(self._now())
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.status == "running",
                TelegramCommandExecutionRow.lease_until < now).values(
                    status="unknown", owner=None, lease_until=None,
                    version=TelegramCommandExecutionRow.version + 1,
                    failure_kind="lease_expired"))
            return result.rowcount

    def mark_owned_running_unknown(self, owner: str) -> int:
        """Hand this worker's unfinished effects to read-only reconciliation."""
        owner = safe_identifier(owner, "owner")
        with self._sessions.begin() as session:
            result = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.status == "running",
                TelegramCommandExecutionRow.owner == owner).values(
                    status="unknown", owner=None, lease_until=None,
                    version=TelegramCommandExecutionRow.version + 1,
                    failure_kind="process_shutdown"))
            return result.rowcount

    def claim_reconciliation(self, owner: str, lease_s: int = 30) -> ClaimedIntent | None:
        owner = safe_identifier(owner, "owner")
        lease_s = positive_int(lease_s, "lease_s")
        now, until = _aware(self._now()), _aware(self._now()) + timedelta(seconds=lease_s)
        with self._sessions.begin() as session:
            row = session.scalar(select(TelegramCommandExecutionRow).where(
                or_(TelegramCommandExecutionRow.status == "unknown",
                    (TelegramCommandExecutionRow.status == "reconciling")
                    & (TelegramCommandExecutionRow.lease_until < now))).order_by(
                    TelegramCommandExecutionRow.created_at,
                    TelegramCommandExecutionRow.id).limit(1))
            if row is None:
                return None
            result = session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.id == row.id,
                or_(TelegramCommandExecutionRow.status == "unknown",
                    (TelegramCommandExecutionRow.status == "reconciling")
                    & (TelegramCommandExecutionRow.lease_until < now)),
                TelegramCommandExecutionRow.version == row.version).values(
                    status="reconciling", owner=owner,
                    lease_until=until, version=row.version + 1).execution_options(
                        synchronize_session=False))
            if result.rowcount != 1:
                return None
            import json
            return ClaimedIntent(row.id, row.update_id, row.command, row.state_fingerprint,
                                 json.loads(row.targets_json), row.version + 1)

    def purge_confirmations_before(self, before: datetime,
                                   limit: int = 1000) -> int:
        before = _aware(before)
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(TelegramConfirmationRow.id).where(
                or_(TelegramConfirmationRow.consumed_at < before,
                    TelegramConfirmationRow.expires_at < before)).order_by(
                    TelegramConfirmationRow.id).limit(limit)))
            if not ids:
                return 0
            unresolved_statuses = (
                "pending", "claimed", "running", "unknown", "reconciling")
            session.execute(update(TelegramCommandExecutionRow).where(
                TelegramCommandExecutionRow.confirmation_id.in_(ids),
                ~TelegramCommandExecutionRow.status.in_(unresolved_statuses)
            ).values(confirmation_id=None))
            linked = select(TelegramCommandExecutionRow.confirmation_id).where(
                TelegramCommandExecutionRow.confirmation_id.in_(ids),
                TelegramCommandExecutionRow.status.in_(unresolved_statuses))
            return session.query(TelegramConfirmationRow).filter(
                TelegramConfirmationRow.id.in_(ids),
                ~TelegramConfirmationRow.id.in_(linked)).delete(
                    synchronize_session=False)

    def purge_audits_before(self, before: datetime, limit: int = 1000) -> int:
        """One-year caller cutoff; unresolved intent audit evidence is retained."""
        before = _aware(before)
        unresolved = select(TelegramCommandExecutionRow.id).where(
            TelegramCommandExecutionRow.status.in_(
                ("pending", "claimed", "running", "unknown", "reconciling")))
        with self._sessions.begin() as session:
            ids = list(session.scalars(select(TelegramCommandAuditRow.id).where(
                TelegramCommandAuditRow.ts < before,
                or_(TelegramCommandAuditRow.intent_id.is_(None),
                    ~TelegramCommandAuditRow.intent_id.in_(unresolved))
            ).order_by(TelegramCommandAuditRow.id).limit(limit)))
            if not ids:
                return 0
            return session.query(TelegramCommandAuditRow).filter(
                TelegramCommandAuditRow.id.in_(ids)).delete(
                    synchronize_session=False)
