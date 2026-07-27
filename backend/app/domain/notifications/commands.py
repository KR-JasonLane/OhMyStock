"""Durable Telegram command coordination.

The processor receives only normalized inbox rows.  It never receives Telegram
text or calls a broker: state-changing work is delegated to the shared
operations-control port with a durable command intent ID.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.notifications.models import CommandKind
from app.domain.notifications.analysis_summary import render_analysis_summary
from app.domain.notifications.digest import render_retained_digest
from app.domain.notifications.ports import (
    AnalysisReportQueryPort,
    DigestReportQueryPort,
    OperationsControlPort,
)
from app.domain.notifications.presentation import TelegramCommandPresenter
from app.domain.trading.models import LiquidationReason


logger = logging.getLogger(__name__)

_PRESENTATION_FALLBACK = (
    "🚨 명령 결과 응답을 표시하지 못했습니다\n\n"
    "명령을 다시 실행하지 말고 /status에서 현재 상태를 확인해 주세요."
)
_INVALID_CONFIRMATION_FALLBACK = (
    "⚠️ 확인 요청이 유효하지 않습니다.\n\n새 확인 요청을 만들어 주세요."
)


@dataclass(frozen=True)
class CommandResult:
    kind: str
    outbox_sensitive: bool = False
    confirmation_token: str | None = None
    response_text: str | None = None
    response_parts: tuple[str, ...] | None = None
    ephemeral: bool = False
    confirmation_expires_at: datetime | None = None
    terminal_reason: LiquidationReason | None = None


@dataclass(frozen=True)
class _LiquidationDisposition:
    presentation_status: str
    terminal_status: str | None
    monitor: bool
    terminal_reason: LiquidationReason | None = None


@dataclass(frozen=True)
class _PersistedLiquidationOutcome:
    status: str
    reason: LiquidationReason


class CommandProcessor:
    """Claim inbox work, persist intent, and call the shared control use case."""

    _RISKY = {CommandKind.RESUME, CommandKind.LIQUIDATE_ALL}
    def __init__(self, inbox, commands, control: OperationsControlPort, worker_id: str, *,
                 chat_hash: str, now=None, execution_lease_s: int = 30,
                 heartbeat_s: float = 10.0,
                 presenter: TelegramCommandPresenter | None = None,
                 analysis_reports: AnalysisReportQueryPort,
                 digest_reports: DigestReportQueryPort) -> None:
        self._inbox = inbox
        self._commands = commands
        self._control = control
        self._worker_id = worker_id
        self._chat_hash = chat_hash
        self._now = now or (lambda: datetime.now(timezone.utc))
        if execution_lease_s <= 0 or heartbeat_s <= 0 or heartbeat_s >= execution_lease_s:
            raise ValueError("heartbeat must be positive and shorter than execution lease")
        self._execution_lease_s = execution_lease_s
        self._heartbeat_s = heartbeat_s
        self._presenter = (
            presenter if presenter is not None else TelegramCommandPresenter()
        )
        self._analysis_reports = analysis_reports
        self._digest_reports = digest_reports
        self._accepted_monitors: dict[str, asyncio.Task] = {}

    async def process_next(self) -> CommandResult | None:
        """Process one inbox row without blocking the event loop on SQLAlchemy."""
        claimed = await asyncio.to_thread(self._inbox.claim_next, self._worker_id, 15)
        if claimed is None:
            return None
        try:
            result = await self.process_claimed(claimed)
        except BaseException:
            # The inbox can be retried only while no execution is running.  An
            # execution exception is converted to unknown by _execute_intent.
            await asyncio.to_thread(
                self._inbox.release, claimed.update_id, self._worker_id, claimed.version)
            raise
        if result.kind == "deferred":
            # An older worker may still hold the intent lease.  Keep the inbox
            # source recoverable until that lease can be safely reclaimed.
            await asyncio.to_thread(
                self._inbox.release, claimed.update_id, self._worker_id, claimed.version)
            return result
        await asyncio.to_thread(
            self._inbox.finish, claimed.update_id, self._worker_id, claimed.version)
        return result

    async def process_claimed(self, claimed) -> CommandResult:
        """Execute an already-claimed row for a future query/control dispatcher.

        `process_next()` retains the simple single-worker convenience API, while
        TelegramService can later route a claimed query to its bounded query
        lane without duplicating intent or state-machine semantics.
        """
        kind = CommandKind(claimed.command)
        if kind in self._RISKY:
            return await self._issue_confirmation(claimed, kind)
        if kind is CommandKind.CONFIRM:
            return await self._consume_confirmation(claimed)

        created = await asyncio.to_thread(
            self._commands.create_intent_for_update, claimed.update_id, kind.value)
        intent = await asyncio.to_thread(
            self._commands.claim_intent_by_id, created.id, self._worker_id,
            self._execution_lease_s)
        if intent is None:
            status = await asyncio.to_thread(self._commands.intent_status, created.id)
            if status in {"succeeded", "failed", "needs_attention"}:
                # The control effect may have reached terminal state immediately
                # before response materialization. Read-only queries are safe to
                # rebuild; state-changing commands get a conservative terminal
                # acknowledgement and are never replayed.
                if kind in {
                        CommandKind.STATUS, CommandKind.ACCOUNT,
                        CommandKind.POSITIONS, CommandKind.ANALYSIS,
                        CommandKind.DIGEST,
                        CommandKind.HELP}:
                    result, _terminal = await self._call_handler(created)
                    return result
                return CommandResult(
                    status,
                    response_text=self._present(
                        self._presenter.existing_result,
                        kind,
                        status,
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                )
            return CommandResult("deferred")
        return await self._execute_intent(intent, claimed.update_id)

    async def _issue_confirmation(self, claimed, kind: CommandKind) -> CommandResult:
        if kind is CommandKind.RESUME:
            fingerprint, targets = self._scheduler_fingerprint(), ()
        else:
            fingerprint, targets = await self._liquidation_context()
        issued = await asyncio.to_thread(
            self._commands.issue_confirmation, claimed.operator_hash, self._chat_hash,
            kind.value, fingerprint)
        await asyncio.to_thread(
            self._commands.record_audit, claimed.update_id, None,
            "confirmation_issued", "confirmation_required")
        command = f"/confirm {issued.raw_token}"
        return CommandResult(
            "confirmation_required",
            confirmation_token=issued.raw_token,
            response_text=self._present(
                self._presenter.confirmation, kind, command, fallback=command
            ),
            ephemeral=True,
            confirmation_expires_at=issued.expires_at,
        )

    async def _consume_confirmation(self, claimed) -> CommandResult:
        existing = await asyncio.to_thread(
            self._commands.intent_for_update, claimed.update_id)
        if existing is not None:
            intent, status = existing
            if status in {"succeeded", "failed", "needs_attention"}:
                presentation_status = (
                    self._persisted_liquidation_presentation_status(
                        status, intent.terminal_reason)
                    if intent.command == CommandKind.LIQUIDATE_ALL.value
                    else status
                )
                return CommandResult(
                    (
                        "needs_attention"
                        if presentation_status == "needs_attention"
                        else status
                    ),
                    response_text=self._present(
                        self._presenter.existing_result,
                        CommandKind(intent.command),
                        presentation_status,
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                )
            if status in {"pending", "claimed"}:
                recovered = await asyncio.to_thread(
                    self._commands.claim_intent_by_id,
                    intent.id, self._worker_id, self._execution_lease_s)
                if recovered is not None:
                    return await self._execute_intent(
                        recovered, claimed.update_id)
            return CommandResult("deferred")
        if claimed.argument_hash is None:
            return CommandResult(
                "confirmation_invalid",
                response_text=self._present(
                    self._presenter.existing_result,
                    CommandKind.CONFIRM,
                    "confirmation_invalid",
                    fallback=_INVALID_CONFIRMATION_FALLBACK,
                ),
            )
        pending = await asyncio.to_thread(
            self._commands.pending_confirmation, claimed.argument_hash,
            claimed.operator_hash, self._chat_hash, self._now())
        if pending is None:
            await asyncio.to_thread(
                self._commands.record_audit, claimed.update_id, None,
                "confirmation_consumed", "confirmation_invalid")
            return CommandResult(
                "confirmation_invalid",
                response_text=self._present(
                    self._presenter.existing_result,
                    CommandKind.CONFIRM,
                    "confirmation_invalid",
                    fallback=_INVALID_CONFIRMATION_FALLBACK,
                ),
            )
        kind = CommandKind(pending.command)
        if kind is CommandKind.RESUME:
            fingerprint, targets = self._scheduler_fingerprint(), ()
        elif kind is CommandKind.LIQUIDATE_ALL:
            fingerprint, targets = await self._liquidation_context()
        else:  # A confirmation row must only ever represent the two risky commands.
            return CommandResult(
                "confirmation_invalid",
                response_text=self._present(
                    self._presenter.existing_result,
                    CommandKind.CONFIRM,
                    "confirmation_invalid",
                    fallback=_INVALID_CONFIRMATION_FALLBACK,
                ),
            )
        intent = await asyncio.to_thread(
            self._commands.consume_and_create_intent,
            claimed.argument_hash, claimed.operator_hash, self._chat_hash,
            kind.value, fingerprint, self._now(), update_id=claimed.update_id,
            targets=targets)
        if intent is None:
            await asyncio.to_thread(
                self._commands.record_audit, claimed.update_id, None,
                "confirmation_consumed", "confirmation_invalid")
            return CommandResult(
                "confirmation_invalid",
                response_text=self._present(
                    self._presenter.existing_result,
                    CommandKind.CONFIRM,
                    "confirmation_invalid",
                    fallback=_INVALID_CONFIRMATION_FALLBACK,
                ),
            )
        intent = await asyncio.to_thread(
            self._commands.claim_intent_by_id, intent.id, self._worker_id,
            self._execution_lease_s)
        if intent is None:
            return CommandResult("deferred")
        return await self._execute_intent(intent, claimed.update_id)

    async def _execute_intent(self, intent, update_id: int) -> CommandResult:
        if not await asyncio.to_thread(
                self._commands.mark_running, intent.id, self._worker_id, intent.version):
            return CommandResult("deferred")
        running_version = [intent.version + 1]
        stopped, lease_lost = asyncio.Event(), asyncio.Event()
        heartbeat = asyncio.create_task(
            self._maintain_execution_lease(intent.id, running_version, stopped, lease_lost))
        try:
            result, terminal = await self._call_handler(intent)
        except BaseException:
            await self._stop_heartbeat(stopped, heartbeat)
            # We cannot know whether the in-memory or broker-side effect began.
            # Never retry a running command through the normal inbox path.
            await asyncio.to_thread(
                self._commands.mark_unknown, intent.id, self._worker_id, running_version[0])
            raise
        else:
            await self._stop_heartbeat(stopped, heartbeat)
        if lease_lost.is_set():
            # A concurrent recovery won the version fence; never claim a
            # terminal result after a control call whose ownership is unclear.
            return CommandResult("deferred")
        if terminal is None:
            # TradingService now owns the cooperative liquidation loop.  Keep
            # the command execution lease alive until its read-only reconcile
            # reports a terminal result; lease expiry must mean worker loss,
            # not a normal in-progress SELL.
            self._accepted_monitors[intent.id] = asyncio.create_task(
                self._monitor_accepted_liquidation(
                    intent, update_id, running_version[0]))
            await asyncio.to_thread(
                self._commands.record_audit, update_id, intent.id,
                "execution", "accepted")
            return result
        terminal_written = await asyncio.to_thread(
            self._commands.mark_terminal, intent.id, self._worker_id,
            running_version[0], terminal, result.terminal_reason)
        if not terminal_written:
            raise RuntimeError("command terminal transition lost")
        await asyncio.to_thread(
            self._commands.record_audit, update_id, intent.id, "execution", terminal)
        return result

    async def _stop_heartbeat(self, stopped: asyncio.Event, task: asyncio.Task) -> None:
        """Join an in-flight DB renewal before using its version fence."""
        stopped.set()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _monitor_accepted_liquidation(self, intent, update_id: int,
                                             version: int) -> None:
        """Own the lease until TradingService's existing SELL loop terminates."""
        try:
            while True:
                renewed = await asyncio.to_thread(
                    self._commands.renew_running_lease, intent.id, self._worker_id,
                    version, self._execution_lease_s)
                if renewed is None:
                    return
                version = renewed
                try:
                    outcome = await self._control.reconcile_control_intent(
                        intent.id, self._targets_for_control(intent.targets))
                except Exception as exc:  # keep lease; later read-only retry is safe
                    await asyncio.to_thread(
                        self._commands.record_audit, update_id, intent.id,
                        "reconciliation", "retrying", type(exc).__name__)
                else:
                    disposition = self._classify_liquidation_outcome(outcome)
                    if disposition.terminal_status is not None:
                        if await asyncio.to_thread(
                                self._commands.mark_terminal, intent.id,
                                self._worker_id, version,
                                disposition.terminal_status,
                                disposition.terminal_reason):
                            await asyncio.to_thread(
                                self._commands.record_audit, update_id, intent.id,
                                "reconciliation",
                                disposition.terminal_status)
                        return
                    if not disposition.monitor:
                        if await asyncio.to_thread(
                                self._commands.mark_terminal, intent.id,
                                self._worker_id, version, "needs_attention"):
                            await asyncio.to_thread(
                                self._commands.record_audit, update_id,
                                intent.id, "reconciliation",
                                "needs_attention")
                        return
                await asyncio.sleep(self._heartbeat_s)
        finally:
            self._accepted_monitors.pop(intent.id, None)

    async def shutdown_accepted_monitors(self) -> None:
        """Stop accepted monitors and persist unfinished execution as unknown."""
        tasks = tuple(self._accepted_monitors.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(
            self._commands.mark_owned_running_unknown, self._worker_id)

    async def _maintain_execution_lease(self, intent_id: str, version: list[int],
                                        stopped: asyncio.Event,
                                        lease_lost: asyncio.Event) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=self._heartbeat_s)
            except TimeoutError:
                renewed = await asyncio.to_thread(
                    self._commands.renew_running_lease, intent_id, self._worker_id,
                    version[0], self._execution_lease_s)
                if renewed is None:
                    lease_lost.set()
                    return
                version[0] = renewed

    async def _call_handler(self, intent) -> tuple[CommandResult, str | None]:
        kind = CommandKind(intent.command)
        if kind is CommandKind.STATUS:
            status = await self._control.system_status()
            return CommandResult(
                "status",
                response_text=self._present(
                    self._presenter.status,
                    status,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.ACCOUNT:
            summary = await self._control.account_summary()
            return CommandResult(
                "account", outbox_sensitive=True,
                response_text=self._present(
                    self._presenter.account,
                    summary,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.POSITIONS:
            summary = await self._control.open_positions_summary()
            return CommandResult(
                "positions", outbox_sensitive=True,
                response_text=self._present(
                    self._presenter.positions,
                    summary,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.ANALYSIS:
            summary = await asyncio.to_thread(self._analysis_reports.latest_analysis)
            return CommandResult(
                "analysis", outbox_sensitive=True,
                response_text=(
                    self._present(
                        render_analysis_summary,
                        summary,
                        fallback=("🧠 최근 AI 분석\n\n"
                                  "조회 가능한 AI 분석이 없습니다."),
                    )
                    if summary is not None
                    else "🧠 최근 AI 분석\n\n조회 가능한 AI 분석이 없습니다."
                ),
            ), "succeeded"
        if kind is CommandKind.DIGEST:
            retained = await asyncio.to_thread(self._digest_reports.latest_digest)
            try:
                response_text = (
                    render_retained_digest(retained[0]) if retained is not None else None
                )
            except ValueError:
                response_text = None
            return CommandResult(
                "digest", outbox_sensitive=True,
                response_text=(response_text or "📋 최근 거래 다이제스트\n\n"
                               "조회 가능한 최근 거래 다이제스트가 없습니다."),
                response_parts=(retained[1]
                                if response_text is not None and retained is not None
                                else None),
            ), "succeeded"
        if kind is CommandKind.HELP:
            return CommandResult(
                "help",
                response_text=self._present(
                    self._presenter.help,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.PAUSE:
            snapshot = await self._control.pause_scheduler()
            if isinstance(snapshot, dict) and snapshot.get("applied") is False:
                return CommandResult(
                    "needs_attention",
                    response_text=self._present(
                        self._presenter.control_result,
                        kind,
                        "needs_attention",
                        applied=False,
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                ), "needs_attention"
            return CommandResult(
                "pause",
                response_text=self._present(
                    self._presenter.control_result,
                    kind,
                    "succeeded",
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.STOP:
            applied = await self._control.stop_new_entries(intent.id)
            if applied:
                return CommandResult(
                    "stop",
                    response_text=self._present(
                        self._presenter.control_result,
                        kind,
                        "succeeded",
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                ), "succeeded"
            return CommandResult(
                "needs_attention",
                response_text=self._present(
                    self._presenter.control_result,
                    kind,
                    "needs_attention",
                    applied=False,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "needs_attention"
        if kind is CommandKind.RESUME:
            snapshot = await self._control.resume_scheduler(
                expected=intent.state_fingerprint)
            if isinstance(snapshot, dict) and snapshot.get("applied") is False:
                return CommandResult(
                    "needs_attention",
                    response_text=self._present(
                        self._presenter.control_result,
                        kind,
                        "needs_attention",
                        applied=False,
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                ), "needs_attention"
            return CommandResult(
                "resume",
                response_text=self._present(
                    self._presenter.control_result,
                    kind,
                    "succeeded",
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), "succeeded"
        if kind is CommandKind.LIQUIDATE_ALL:
            outcome = await self._control.liquidate_managed(
                intent.id, self._targets_for_control(intent.targets),
                expected_run_id=self._context_run_id(intent.targets))
            disposition = self._classify_liquidation_outcome(outcome)
            if disposition.terminal_status is not None:
                return CommandResult(
                    disposition.terminal_status,
                    response_text=self._present(
                        self._presenter.control_result,
                        kind,
                        disposition.presentation_status,
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                    terminal_reason=disposition.terminal_reason,
                ), disposition.terminal_status
            if not disposition.monitor:
                return CommandResult(
                    "needs_attention",
                    response_text=self._present(
                        self._presenter.control_result,
                        kind,
                        "needs_attention",
                        fallback=_PRESENTATION_FALLBACK,
                    ),
                    terminal_reason=disposition.terminal_reason,
                ), "needs_attention"
            # Only accepted/in_progress outcomes reach this path. Even when
            # their reason contract is damaged, TradingService may already own
            # a SELL side effect, so keep read-only monitoring and never retry.
            return CommandResult(
                (
                    "liquidation_accepted"
                    if disposition.presentation_status in {
                        "accepted", "in_progress"
                    }
                    else "needs_attention"
                ),
                response_text=self._present(
                    self._presenter.control_result,
                    kind,
                    disposition.presentation_status,
                    fallback=_PRESENTATION_FALLBACK,
                ),
            ), None
        raise ValueError(f"unsupported intent command: {intent.command}")

    def _present(
        self,
        presenter_call: Callable[..., str],
        *args: Any,
        fallback: str,
        **kwargs: Any,
    ) -> str:
        """표시 실패를 제어 실행 실패나 unknown intent로 확대하지 않는다."""
        try:
            return presenter_call(*args, **kwargs)
        except Exception as exc:  # 표시 원문·인자는 로그에 넣지 않는다.
            logger.error(
                "telegram command presentation failed method=%s "
                "exception_type=%s",
                getattr(
                    presenter_call,
                    "__name__",
                    type(presenter_call).__name__,
                ),
                type(exc).__name__,
            )
            return fallback

    @staticmethod
    def _liquidation_presentation_status(outcome: Any) -> str:
        """구조화 청산 사유만 외부 표시용 고정 코드로 축소한다."""
        return CommandProcessor._classify_liquidation_outcome(
            outcome
        ).presentation_status

    @staticmethod
    def _classify_liquidation_outcome(
        outcome: Any,
    ) -> _LiquidationDisposition:
        """표시·영속 terminal·monitor를 하나의 허용 조합으로 결정한다."""
        status = getattr(outcome, "status", "needs_attention")
        reason = getattr(outcome, "reason", None)
        if not isinstance(status, str) or not isinstance(
            reason, LiquidationReason
        ):
            return CommandProcessor._invalid_liquidation_disposition(status)
        terminal_succeeded = "succeeded"
        terminal_attention = "needs_attention"
        allowed: dict[
            tuple[str, LiquidationReason],
            _LiquidationDisposition,
        ] = {
            ("succeeded", LiquidationReason.COMPLETED):
                _LiquidationDisposition(
                    "succeeded", terminal_succeeded, False
                ),
            (
                "succeeded",
                LiquidationReason.NO_TARGETS,
            ): _LiquidationDisposition(
                "succeeded_no_targets", terminal_succeeded, False
            ),
            (
                "succeeded",
                LiquidationReason.UNMANAGED_BALANCE,
            ): _LiquidationDisposition(
                "succeeded_balance_remains", terminal_succeeded, False
            ),
            ("accepted", LiquidationReason.ACCEPTED):
                _LiquidationDisposition("accepted", None, True),
            (
                "in_progress",
                LiquidationReason.ALREADY_ACCEPTED,
            ): _LiquidationDisposition("in_progress", None, True),
            (
                "in_progress",
                LiquidationReason.POSITION_REMAINS,
            ): _LiquidationDisposition("in_progress", None, True),
            (
                "needs_attention",
                LiquidationReason.MARKET_CLOSED,
            ): _LiquidationDisposition(
                "unavailable_market_closed", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.MARKET_CLOSE_INCOMPLETE,
            ): _LiquidationDisposition(
                "unavailable_market_close_incomplete",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.TRADING_HALT,
            ): _LiquidationDisposition(
                "unavailable_trading_halt", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.OPEN_SELL_ORDERS,
            ): _LiquidationDisposition(
                "unavailable_open_orders", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.TARGET_STATE_CHANGED,
            ): _LiquidationDisposition(
                "unavailable_state_changed", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.RUN_CHANGED,
            ): _LiquidationDisposition(
                "unavailable_state_changed", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.QUANTITY_MISMATCH,
            ): _LiquidationDisposition(
                "unavailable_quantity_mismatch",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.TRADING_INACTIVE,
            ): _LiquidationDisposition(
                "unavailable_trading_inactive",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.PREFLIGHT_RECONCILIATION_FAILED,
            ): _LiquidationDisposition(
                "unavailable_preflight_reconciliation",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.POST_ACCEPT_RECONCILIATION_FAILED,
            ): _LiquidationDisposition(
                "unavailable_post_accept_reconciliation",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.ANOTHER_INTENT_ACTIVE,
            ): _LiquidationDisposition(
                "unavailable_intent_active",
                terminal_attention,
                False,
            ),
            (
                "needs_attention",
                LiquidationReason.PERSISTENCE_FAILED,
            ): _LiquidationDisposition(
                "unavailable_persistence", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.UNKNOWN_INTENT,
            ): _LiquidationDisposition(
                "unavailable_unknown_intent", terminal_attention, False
            ),
            (
                "needs_attention",
                LiquidationReason.POSITION_REMAINS,
            ): _LiquidationDisposition(
                "unavailable_position_remains", terminal_attention, False
            ),
        }
        disposition = allowed.get((status, reason))
        if disposition is None:
            return CommandProcessor._invalid_liquidation_disposition(status)
        return _LiquidationDisposition(
            disposition.presentation_status,
            disposition.terminal_status,
            disposition.monitor,
            reason if disposition.terminal_status is not None else None,
        )

    @staticmethod
    def _invalid_liquidation_disposition(
        status: Any,
    ) -> _LiquidationDisposition:
        if isinstance(status, str) and status in {
            "accepted", "in_progress"
        }:
            return _LiquidationDisposition("needs_attention", None, True)
        return _LiquidationDisposition(
            "needs_attention", "needs_attention", False
        )

    @staticmethod
    def _persisted_liquidation_presentation_status(
        status: str,
        reason_value: str | None,
    ) -> str:
        try:
            reason = LiquidationReason(reason_value)
        except (TypeError, ValueError):
            return "needs_attention"
        disposition = CommandProcessor._classify_liquidation_outcome(
            _PersistedLiquidationOutcome(status, reason)
        )
        return (
            disposition.presentation_status
            if disposition.terminal_status == status
            else "needs_attention"
        )

    async def reconcile_unknown(self) -> CommandResult | None:
        """Reconcile one abandoned execution without reissuing any command."""
        await asyncio.to_thread(self._commands.expire_running_to_unknown)
        intent = await asyncio.to_thread(
            self._commands.claim_reconciliation, self._worker_id, 30)
        if intent is None:
            return None
        try:
            if intent.command == CommandKind.LIQUIDATE_ALL.value:
                outcome = await self._control.reconcile_control_intent(
                    intent.id, self._targets_for_control(intent.targets))
                disposition = self._classify_liquidation_outcome(outcome)
                terminal = (
                    disposition.terminal_status
                    if disposition.terminal_status is not None
                    else "needs_attention"
                )
                terminal_reason = disposition.terminal_reason
            else:
                terminal_reason = None
                terminal = await self._reconcile_non_liquidation(intent)
        except BaseException as exc:
            await asyncio.to_thread(
                self._commands.record_audit, intent.update_id, intent.id,
                "reconciliation", "retrying", type(exc).__name__)
            raise
        result = CommandResult(terminal)
        terminal_written = await asyncio.to_thread(
            self._commands.mark_terminal, intent.id, self._worker_id,
            intent.version, terminal, terminal_reason)
        if not terminal_written:
            raise RuntimeError("reconciliation terminal transition lost")
        await asyncio.to_thread(
            self._commands.record_audit, intent.update_id, intent.id,
            "reconciliation", terminal)
        return result

    async def _reconcile_non_liquidation(self, intent) -> str:
        """Read current shared-control state; do not replay an ambiguous command."""
        kind = CommandKind(intent.command)
        if kind in {CommandKind.STATUS, CommandKind.ACCOUNT,
                    CommandKind.POSITIONS, CommandKind.ANALYSIS,
                    CommandKind.DIGEST,
                    CommandKind.HELP}:
            return "succeeded"
        status = await self._control.system_status()
        scheduler = status.get("scheduler") if isinstance(status, dict) else None
        trading = status.get("trading") if isinstance(status, dict) else None
        paused = (scheduler.get("paused") if isinstance(scheduler, dict)
                  else getattr(scheduler, "paused", None))
        kill_switch = (trading.get("kill_switch") if isinstance(trading, dict)
                       else getattr(trading, "kill_switch", None))
        if kind is CommandKind.PAUSE:
            return "succeeded" if paused is True else "needs_attention"
        if kind is CommandKind.RESUME:
            return "succeeded" if paused is False else "needs_attention"
        if kind is CommandKind.STOP:
            return ("succeeded" if kill_switch == "stop_new_entries"
                    else "needs_attention")
        return "needs_attention"

    def _scheduler_fingerprint(self) -> str:
        return self._control.scheduler_fingerprint()

    async def _liquidation_context(self) -> tuple[str, tuple[dict[str, Any], ...]]:
        preview, status = await asyncio.gather(
            self._control.liquidation_preview(), self._control.system_status())
        targets = self._targets_for_storage(preview.targets)
        trading = status.get("trading") if isinstance(status, dict) else None
        run_id = (trading.get("run_id") if isinstance(trading, dict)
                  else getattr(trading, "run_id", None))
        context = {"run_id": run_id, "targets": targets}
        return self._liquidation_fingerprint(run_id, targets), context

    @staticmethod
    def _targets_for_storage(targets: Any) -> tuple[dict[str, Any], ...]:
        return tuple({"position_id": target.position_id, "symbol": target.symbol,
                      "quantity": target.quantity} for target in targets)

    @staticmethod
    def _targets_for_control(targets: Any):
        from app.domain.trading.models import LiquidationTarget
        items = targets.get("targets", ()) if isinstance(targets, dict) else targets
        return tuple(LiquidationTarget(item["position_id"], item["symbol"],
                                       item["quantity"]) for item in items)

    @staticmethod
    def _context_run_id(targets: Any) -> int | None:
        run_id = targets.get("run_id") if isinstance(targets, dict) else None
        return run_id if type(run_id) is int else None

    @staticmethod
    def _liquidation_fingerprint(run_id: int | None,
                                 targets: tuple[dict[str, Any], ...]) -> str:
        payload = json.dumps({
            "run_id": run_id,
            "targets": sorted(targets, key=lambda item: item["position_id"]),
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
