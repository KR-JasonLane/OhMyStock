"""Durable Telegram command coordination.

The processor receives only normalized inbox rows.  It never receives Telegram
text or calls a broker: state-changing work is delegated to the shared
operations-control port with a durable command intent ID.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.notifications.models import CommandKind
from app.domain.notifications.ports import OperationsControlPort


@dataclass(frozen=True)
class CommandResult:
    kind: str
    outbox_sensitive: bool = False
    confirmation_token: str | None = None


class CommandProcessor:
    """Claim inbox work, persist intent, and call the shared control use case."""

    _RISKY = {CommandKind.RESUME, CommandKind.LIQUIDATE_ALL}
    def __init__(self, inbox, commands, control: OperationsControlPort, worker_id: str, *,
                 chat_hash: str, now=None, execution_lease_s: int = 30,
                 heartbeat_s: float = 10.0) -> None:
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
                return CommandResult(status)
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
        return CommandResult("confirmation_required", confirmation_token=issued.raw_token)

    async def _consume_confirmation(self, claimed) -> CommandResult:
        if claimed.argument_hash is None:
            return CommandResult("confirmation_invalid")
        pending = await asyncio.to_thread(
            self._commands.pending_confirmation, claimed.argument_hash,
            claimed.operator_hash, self._chat_hash, self._now())
        if pending is None:
            await asyncio.to_thread(
                self._commands.record_audit, claimed.update_id, None,
                "confirmation_consumed", "confirmation_invalid")
            return CommandResult("confirmation_invalid")
        kind = CommandKind(pending.command)
        if kind is CommandKind.RESUME:
            fingerprint, targets = self._scheduler_fingerprint(), ()
        elif kind is CommandKind.LIQUIDATE_ALL:
            fingerprint, targets = await self._liquidation_context()
        else:  # A confirmation row must only ever represent the two risky commands.
            return CommandResult("confirmation_invalid")
        intent = await asyncio.to_thread(
            self._commands.consume_and_create_intent,
            claimed.argument_hash, claimed.operator_hash, self._chat_hash,
            kind.value, fingerprint, self._now(), update_id=claimed.update_id,
            targets=targets)
        if intent is None:
            await asyncio.to_thread(
                self._commands.record_audit, claimed.update_id, None,
                "confirmation_consumed", "confirmation_invalid")
            return CommandResult("confirmation_invalid")
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
            result, terminal = await self._call_control(intent)
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
            running_version[0], terminal)
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
                    if outcome.status in {"succeeded", "failed", "needs_attention"}:
                        if await asyncio.to_thread(
                                self._commands.mark_terminal, intent.id,
                                self._worker_id, version, outcome.status):
                            await asyncio.to_thread(
                                self._commands.record_audit, update_id, intent.id,
                                "reconciliation", outcome.status)
                        return
                await asyncio.sleep(self._heartbeat_s)
        finally:
            self._accepted_monitors.pop(intent.id, None)

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

    async def _call_control(self, intent) -> tuple[CommandResult, str | None]:
        kind = CommandKind(intent.command)
        if kind is CommandKind.STATUS:
            await self._control.system_status()
            return CommandResult("status"), "succeeded"
        if kind is CommandKind.ACCOUNT:
            await self._control.account_summary()
            return CommandResult("account", outbox_sensitive=True), "succeeded"
        if kind is CommandKind.POSITIONS:
            await self._control.open_positions_summary()
            return CommandResult("positions", outbox_sensitive=True), "succeeded"
        if kind is CommandKind.HELP:
            return CommandResult("help"), "succeeded"
        if kind is CommandKind.PAUSE:
            await self._control.pause_scheduler()
            return CommandResult("pause"), "succeeded"
        if kind is CommandKind.STOP:
            applied = await self._control.stop_new_entries(intent.id)
            if applied:
                return CommandResult("stop"), "succeeded"
            return CommandResult("needs_attention"), "needs_attention"
        if kind is CommandKind.RESUME:
            await self._control.resume_scheduler(expected=intent.state_fingerprint)
            return CommandResult("resume"), "succeeded"
        if kind is CommandKind.LIQUIDATE_ALL:
            outcome = await self._control.liquidate_managed(
                intent.id, self._targets_for_control(intent.targets),
                expected_run_id=self._context_run_id(intent.targets))
            if outcome.status in {"succeeded", "failed", "needs_attention"}:
                return CommandResult(outcome.status), outcome.status
            # "accepted"/"in_progress" means TradingService owns execution.
            # Its terminal reconciliation is deliberately performed after this
            # running intent becomes unknown, never by placing a second sell.
            return CommandResult("liquidation_accepted"), None
        raise ValueError(f"unsupported intent command: {intent.command}")

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
                terminal = (outcome.status if outcome.status in {
                    "succeeded", "failed", "needs_attention"} else "needs_attention")
            else:
                terminal = await self._reconcile_non_liquidation(intent)
        except BaseException as exc:
            await asyncio.to_thread(
                self._commands.record_audit, intent.update_id, intent.id,
                "reconciliation", "retrying", type(exc).__name__)
            raise
        result = CommandResult(terminal)
        terminal_written = await asyncio.to_thread(
            self._commands.mark_terminal, intent.id, self._worker_id,
            intent.version, terminal)
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
                    CommandKind.POSITIONS, CommandKind.HELP}:
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
