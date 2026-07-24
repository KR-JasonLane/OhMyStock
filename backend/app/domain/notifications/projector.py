"""Append-only operational event를 durable outbox로 투영한다."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.notifications.models import OperationalEvent, RenderedPart
from app.domain.notifications.formatting import render_parts


@dataclass(frozen=True)
class OutboxProjection:
    idempotency_key: str
    kind: str
    payload: dict[str, object]
    priority: int = 0
    parts: tuple[RenderedPart, ...] = ()


class ProjectorStore(Protocol):
    def project_operational_events(
            self, project: Callable[[int, OperationalEvent], Iterable[OutboxProjection]],
            limit: int = 100) -> int: ...
    def projector_checkpoint(self) -> int: ...
    def rewind_projector_checkpoint(self, event_id: int) -> None: ...
    def outbox_count_by_key(self, idempotency_key: str) -> int: ...
    def materialize_missing_operational_deliveries(
            self, render: Callable[[str, dict[str, object]], tuple[str, ...]],
            limit: int = 100) -> int: ...


@dataclass(frozen=True)
class NotificationProjector:
    """ID 단일 커서와 outbox 고유 키로 재실행 중복을 제거한다."""

    store: ProjectorStore

    def project_batch(self, limit: int = 100) -> int:
        self.store.materialize_missing_operational_deliveries(
            self._render_bodies, limit)
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
            if kind not in _SAFE_FACT_KEYS:
                # The append-only event remains the audit record, but arbitrary
                # producer text never becomes an external notification and must
                # not poison the cursor for later urgent events.
                continue
            payload = {"operational_event_id": event_id,
                       "event_kind": event.kind, "notification_kind": kind,
                       "source_type": event.source_type,
                       "source_id": event.source_id,
                       "source_version": event.version, "facts": facts}
            yield OutboxProjection(
                idempotency_key=f"operational:{event_id}:{kind}", kind=kind,
                payload=payload,
                parts=NotificationProjector._render_payload(kind, payload))

    @staticmethod
    def _render_payload(kind: str, payload: dict[str, object]) -> tuple[RenderedPart, ...]:
        if kind not in _SAFE_FACT_KEYS:
            raise ValueError("notification kind is not allowed")
        event_id = payload.get("operational_event_id")
        if type(event_id) is not int or event_id < 1:
            raise ValueError("operational_event_id must be a positive int")
        return render_parts(
            _operational_message(kind, payload), f"operational-{event_id}")

    @staticmethod
    def _render_bodies(kind: str, payload: dict[str, object]) -> tuple[str, ...]:
        return tuple(part.text for part in NotificationProjector._render_payload(kind, payload))

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


def _operational_message(kind: str, payload: dict[str, object]) -> str:
    """명시적으로 허용한 운영 facts만 plain text 고정 렌더한다."""
    raw_facts = payload.get("facts")
    facts = raw_facts if isinstance(raw_facts, dict) else {}
    allowed = _SAFE_FACT_KEYS.get(kind, ())
    safe_facts = {key: facts[key] for key in allowed
                  if key in facts and type(facts[key]) in {str, int, float, bool, type(None)}}
    rendered = json.dumps(safe_facts, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
    return f"알림: {kind}\n{rendered}"


_SAFE_FACT_KEYS: dict[str, tuple[str, ...]] = {
    "pipeline_gave_up": ("job", "reason", "run_id"),
    "trading_monitoring_gap": ("job", "reason", "run_id"),
    "scheduler_dead": ("reason",),
    "kill_switch_requested": ("mode",),
    "kill_switch_completed": ("mode", "run_status"),
    "kill_switch_failed": ("mode", "run_status", "failure_kind"),
    "kill_switch_needs_attention": ("mode", "run_status", "needs_attention", "failure_kind"),
    "entry_partial_fill": ("symbol", "order_qty", "fill_qty", "cumulative_fill_qty",
                            "remaining_qty", "avg_fill_price", "price_confidence",
                            "remaining_order_state", "unmanaged_qty"),
    "entry_filled": ("symbol", "order_qty", "fill_qty", "cumulative_fill_qty",
                     "remaining_qty", "avg_fill_price", "price_confidence",
                     "remaining_order_state", "unmanaged_qty"),
    "exit_partial_fill": ("symbol", "order_qty", "fill_qty", "cumulative_fill_qty",
                           "remaining_qty", "avg_fill_price", "price_confidence",
                           "remaining_order_state", "exit_reason"),
    "exit_filled": ("symbol", "order_qty", "fill_qty", "cumulative_fill_qty",
                    "remaining_qty", "avg_fill_price", "price_confidence",
                    "remaining_order_state", "exit_reason"),
    "exit_unconfirmed": ("symbol", "remaining_qty", "remaining_order_state", "exit_reason"),
    "exit_remaining_failed": ("symbol", "remaining_qty", "remaining_order_state", "exit_reason"),
}
