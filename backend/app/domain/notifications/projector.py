"""Append-only operational event를 durable outbox로 투영한다."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.notifications.models import OperationalEvent, RenderedPart


@dataclass(frozen=True)
class OutboxProjection:
    idempotency_key: str
    kind: str
    payload: dict[str, object]
    priority: int = 0


class ProjectorStore(Protocol):
    def project_operational_events(
            self, project: Callable[[int, OperationalEvent], Iterable[OutboxProjection]],
            limit: int = 100) -> int: ...
    def projector_checkpoint(self) -> int: ...
    def rewind_projector_checkpoint(self, event_id: int) -> None: ...
    def outbox_count_by_key(self, idempotency_key: str) -> int: ...


@dataclass(frozen=True)
class NotificationProjector:
    """ID 단일 커서와 outbox 고유 키로 재실행 중복을 제거한다."""

    store: ProjectorStore

    def project_batch(self, limit: int = 100) -> int:
        return self.store.project_operational_events(self._project_event, limit)

    @staticmethod
    def _project_event(event_id: int,
                       event: OperationalEvent) -> Iterable[OutboxProjection]:
        facts = event.payload_for_storage()
        kinds = facts.get("notification_kinds", [event.kind])
        if not isinstance(kinds, list) or not kinds or not all(
                isinstance(kind, str) and kind for kind in kinds):
            raise ValueError("notification_kinds must be a non-empty string list")
        for kind in kinds:
            yield OutboxProjection(
                idempotency_key=f"operational:{event_id}:{kind}", kind=kind,
                payload={"operational_event_id": event_id,
                         "event_kind": event.kind, "notification_kind": kind,
                         "source_type": event.source_type,
                         "source_id": event.source_id,
                         "source_version": event.version, "facts": facts})

    def checkpoint(self) -> int:
        return self.store.projector_checkpoint()

    def rewind_checkpoint(self, event_id: int) -> None:
        self.store.rewind_projector_checkpoint(event_id)

    def outbox_count(self, idempotency_key: str) -> int:
        return self.store.outbox_count_by_key(idempotency_key)

    def project_partial_fill(self, *, remaining_qty: int,
                             remaining_order_state: str) -> RenderedPart:
        if remaining_qty < 0:
            raise ValueError("remaining_qty must be non-negative")
        if remaining_order_state not in {
                "open", "cancel_pending", "cancelled", "none", "unknown"}:
            raise ValueError("invalid remaining_order_state")
        return RenderedPart(
            index=1, total=1,
            text=(f"부분 체결: 잔량 {remaining_qty}, 미체결 주문 상태 "
                  f"{remaining_order_state}"))
