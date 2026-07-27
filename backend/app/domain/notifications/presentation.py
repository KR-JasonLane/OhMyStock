"""Telegram 명령 결과를 운영자용 plain text로 바꾸는 순수 표현 경계."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.notifications.models import CommandKind


_KST = ZoneInfo("Asia/Seoul")
_UNAVAILABLE = "확인 불가"
_MISSING = object()
_STATUS_LABELS = {
    "idle": "대기",
    "running": "실행 중",
    "paused": "일시정지",
    "succeeded": "완료",
    "failed": "실패",
    "needs_attention": "확인 필요",
    "accepted": "처리 중",
    "in_progress": "처리 중",
    "stopping": "중지 처리 중",
    "stopped": "중지됨",
}
_POSITION_STATUS_LABELS = {
    "pending_entry": "진입 대기",
    "entered": "보유 중",
    "exiting": "청산 중",
    "closed": "종료",
    "entry_failed": "진입 실패",
    "exit_failed": "청산 실패",
}
_FAILED_ACCOUNT_LABELS = {
    "deposit": "예수금",
    "balance": "잔고",
    "realized_pnl": "오늘 실현손익",
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Mapping과 값 객체의 필드를 같은 방식으로 안전하게 읽는다."""
    return value.get(name, default) if isinstance(value, Mapping) else getattr(
        value, name, default
    )


def _won(value: Any) -> str:
    """검증된 정수 금액만 원 단위로 표시한다."""
    if type(value) is not int:
        return _UNAVAILABLE
    return f"{value:,}원"


def _kst(value: Any) -> str:
    """timezone-aware 시각만 KST 운영 시각으로 표시한다."""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime):
            return _UNAVAILABLE
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return _UNAVAILABLE
        return parsed.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except (TypeError, ValueError, OverflowError):
        return _UNAVAILABLE


def _count(value: Any, suffix: str) -> str:
    if type(value) is not int or value < 0:
        return _UNAVAILABLE
    return f"{value:,}{suffix}"


def _sequence(value: Any) -> tuple[Any, ...] | None:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return None


def _legacy_backoff_components(telegram: Any) -> tuple[str, ...]:
    """Canonical field가 없는 이전 snapshot만 한곳에서 정규화한다."""
    components: list[str] = []
    for name in (
        "poller",
        "commands",
        "queries",
        "reconciliation",
        "projector",
        "sender",
        "maintenance",
    ):
        component = _field(telegram, name)
        failures = _field(component, "failures")
        transient = _field(component, "transient_failures")
        if (
            _field(component, "last_error_kind")
            in {"rate_limited", "temporary_error", "internal_error"}
            or (type(failures) is int and failures > 0)
            or (type(transient) is int and transient > 0)
        ):
            components.append(name)
    poller = _field(telegram, "poller")
    if _field(poller, "backoff_reason") and "poller" not in components:
        components.append("poller")
    sender = _field(telegram, "sender")
    if (
        _field(_field(sender, "outbox"), "backoff_reason")
        and "sender" not in components
    ):
        components.append("sender")
    if _field(_field(sender, "ephemeral"), "backoff_reason"):
        components.append("ephemeral")
    if _field(_field(telegram, "dispatcher"), "control_delay_warning") is True:
        components.append("control_delay")
    return tuple(dict.fromkeys(components))


class TelegramCommandPresenter:
    """명령 실행 의미를 바꾸지 않고 운영자용 한국어 응답만 만든다."""

    def status(self, status: Any) -> str:
        scheduler = _field(status, "scheduler")
        trading = _field(status, "trading")
        telegram = _field(status, "telegram")
        poller = _field(telegram, "poller")
        commands = _field(telegram, "commands")
        queries = _field(telegram, "queries")
        reconciliation = _field(telegram, "reconciliation")
        projector = _field(telegram, "projector")
        maintenance = _field(telegram, "maintenance")
        sender = _field(telegram, "sender")
        outbox = _field(sender, "outbox")

        failures: list[str] = []
        warnings: list[str] = []
        telegram_failures: list[str] = []
        telegram_warnings: list[str] = []

        scheduler_dead = _field(scheduler, "dead")
        scheduler_paused = _field(scheduler, "paused")
        if scheduler_dead is True:
            failures.append("자동 일정 중단")
        elif (
            scheduler is None
            or scheduler_dead is not False
            or type(scheduler_paused) is not bool
        ):
            warnings.append("자동 일정 상태 확인 필요")

        telegram_dead = _field(telegram, "dead")
        telegram_enabled = _field(telegram, "enabled")
        if telegram_dead is True:
            telegram_failures.append("Telegram 서비스 중단")
        elif telegram is None or telegram_dead is not False:
            telegram_warnings.append("Telegram 서비스 상태 확인 필요")
        if telegram_enabled is not True:
            telegram_warnings.append(
                "Telegram 비활성"
                if telegram_enabled is False
                else "Telegram 설정 확인 필요"
            )

        component_contracts = (
            (poller, "Telegram 수신 중단", "Telegram 수신 상태 확인 필요"),
            (commands, "명령 처리 중단", "명령 처리 상태 확인 필요"),
            (queries, "조회 처리 중단", "조회 처리 상태 확인 필요"),
            (
                reconciliation,
                "명령 복구 중단",
                "명령 복구 상태 확인 필요",
            ),
            (
                projector,
                "운영 알림 생성 중단",
                "운영 알림 생성 상태 확인 필요",
            ),
            (
                maintenance,
                "Telegram 보존 정리 중단",
                "Telegram 보존 정리 상태 확인 필요",
            ),
            (sender, "Telegram 전송 중단", "Telegram 전송 상태 확인 필요"),
        )
        for component, dead_message, unknown_message in component_contracts:
            component_dead = _field(component, "dead")
            if component_dead is True:
                telegram_failures.append(dead_message)
            elif component is None or component_dead is not False:
                telegram_warnings.append(unknown_message)

        analysis_summary = _field(telegram, "analysis_summary", _MISSING)
        if analysis_summary is not _MISSING:
            analysis_summary_state = _field(analysis_summary, "state")
            if analysis_summary_state == "dead":
                telegram_failures.append("아침 분석 요약 생성 중단")
            elif analysis_summary_state != "running":
                telegram_warnings.append("아침 분석 요약 생성 상태 확인 필요")

        outbox_state = _field(outbox, "state")
        if outbox_state == "dead":
            telegram_failures.append("메시지 전송 중단")
        elif outbox_state != "running":
            telegram_warnings.append("메시지 전송 상태 확인 필요")
        outbox_initialized = _field(outbox, "initialized")
        if outbox_initialized is not True:
            telegram_warnings.append("메시지 전달 상태 확인 필요")

        canonical_raw = _field(
            telegram, "backoff_components", _MISSING
        )
        if canonical_raw is _MISSING:
            canonical_backoff = _legacy_backoff_components(telegram)
        else:
            canonical_backoff = _sequence(canonical_raw)
            if canonical_backoff is None:
                canonical_backoff = ()
                telegram_warnings.append("Telegram 상태 계약 확인 필요")
        backoff_labels = {
            "poller": "Telegram 수신 지연",
            "commands": "Telegram 명령 처리 지연",
            "queries": "Telegram 조회 처리 지연",
            "reconciliation": "Telegram 명령 복구 지연",
            "projector": "운영 알림 생성 지연",
            "sender": "Telegram 전송 지연",
            "maintenance": "Telegram 보존 정리 지연",
            "analysis_summary": "아침 분석 요약 생성 지연",
            "control_delay": "Telegram 제어 명령 처리 지연",
            "ephemeral": "Telegram 확인 응답 전송 지연",
        }
        for component in canonical_backoff:
            telegram_warnings.append(
                (
                    backoff_labels.get(component)
                    if isinstance(component, str)
                    else None
                )
                or "Telegram 상태 계약 확인 필요"
            )
        degraded = _field(telegram, "degraded")
        if degraded is True and not telegram_warnings:
            telegram_warnings.append("Telegram 서비스 성능 저하")
        elif type(degraded) is not bool:
            telegram_warnings.append("Telegram 성능 상태 확인 필요")
        pending_messages = _field(outbox, "pending")
        dead_letters = _field(outbox, "dead_letter")
        valid_pending = (
            outbox_initialized is True
            and type(pending_messages) is int
            and pending_messages >= 0
        )
        valid_dead_letters = (
            outbox_initialized is True
            and type(dead_letters) is int
            and dead_letters >= 0
        )
        if not valid_pending or not valid_dead_letters:
            telegram_warnings.append("메시지 전달 상태 확인 필요")
        if valid_dead_letters and dead_letters > 0:
            telegram_warnings.append(f"전송 실패 메시지  {dead_letters:,}건")

        kill_switch = _field(trading, "kill_switch")
        if kill_switch == "stop_new_entries":
            warnings.append("신규 진입 중지 활성")
        elif kill_switch == "liquidate_all":
            warnings.extend(
                (
                    "관리 포지션 청산 활성",
                    "/positions에서 청산 진행을 확인해 주세요",
                )
            )
        elif kill_switch is not None:
            warnings.append("킬스위치 상태 확인 필요")

        trading_status = _field(trading, "status")
        trading_label = (
            _STATUS_LABELS.get(trading_status, _UNAVAILABLE)
            if isinstance(trading_status, str)
            else _UNAVAILABLE
        )
        if trading_status == "failed":
            failures.append("자동매매 실패")
        elif trading_status == "needs_attention":
            warnings.append("자동매매 상태 확인 필요")
        elif trading_label == _UNAVAILABLE:
            trading_label = "확인 필요"
            warnings.append("자동매매 상태 확인 필요")

        positions_count = _field(trading, "positions_count")
        if type(positions_count) is not int or positions_count < 0:
            warnings.append("관리 포지션 수 확인 필요")
        run_id = _field(trading, "run_id")
        valid_run_id = type(run_id) is int and run_id > 0
        if trading_status == "running" and not valid_run_id:
            warnings.append("자동매매 실행 식별자 확인 필요")
        elif trading_status == "idle" and run_id is not None:
            warnings.append("자동매매 상태 조합 확인 필요")
        elif run_id is not None and not valid_run_id:
            warnings.append("자동매매 실행 식별자 확인 필요")
        last_success = _kst(_field(poller, "last_success_at"))
        if last_success == _UNAVAILABLE:
            telegram_warnings.append("최근 Telegram 확인 시각 확인 필요")

        failures.extend(telegram_failures)
        warnings.extend(telegram_warnings)
        failures = list(dict.fromkeys(failures))
        warnings = list(dict.fromkeys(warnings))
        if failures:
            headline = "🚨 시스템 장애"
        elif warnings:
            headline = "⚠️ 시스템 주의"
        else:
            headline = "✅ 시스템 정상"

        if scheduler_dead is True:
            scheduler_label = "중단"
        elif scheduler_paused is True:
            scheduler_label = "일시정지"
        elif scheduler_dead is not False or scheduler_paused is not False:
            scheduler_label = "확인 필요"
        else:
            scheduler_label = "운영 중"

        telegram_label = (
            "장애" if telegram_failures else "주의" if telegram_warnings else "정상"
        )
        positions = _count(positions_count, "개")
        pending = _count(
            pending_messages if outbox_initialized is True else None,
            "건",
        )
        lines = [
            headline,
            "",
            f"📅 자동 일정  {scheduler_label}",
            f"📈 자동매매  {trading_label}",
            f"📦 관리 포지션  {positions}",
            f"🤖 Telegram  {telegram_label}",
            f"📨 대기 메시지  {pending}",
        ]
        if trading_status == "running" and valid_run_id:
            lines.append(f"거래 실행  {run_id}")
        if failures or warnings:
            lines.extend(["", *(f"• {cause}" for cause in failures + warnings)])
        lines.extend(["", f"최근 확인  {last_success}"])
        return "\n".join(lines)

    def account(self, summary: Any) -> str:
        confidence = _field(summary, "realized_pnl_confidence")
        confidence_label = (
            " (추정)" if confidence == "estimated" else " (정확도 확인 불가)"
        )
        lines = [
            "💰 계좌 요약",
            "",
            f"예수금          {_won(_field(summary, 'deposit'))}",
            f"주문 가능        {_won(_field(summary, 'available_deposit'))}",
            f"보유주식 평가     {_won(_field(summary, 'total_eval'))}",
            f"총자산          {_UNAVAILABLE}",
            "",
            f"평가손익         {_won(_field(summary, 'total_profit'))}",
            (
                "오늘 실현손익 (관리매매)  "
                f"{_won(_field(summary, 'realized_pnl'))}{confidence_label}"
            ),
        ]
        failed_raw = _field(summary, "failed_fields", ())
        failed = _sequence(failed_raw)
        failed_unknown = failed is None
        failed = failed or ()
        failed_labels = [
            _FAILED_ACCOUNT_LABELS[item]
            for item in failed
            if isinstance(item, str) and item in _FAILED_ACCOUNT_LABELS
        ]
        failed_unknown_item = any(
            not isinstance(item, str)
            or item not in _FAILED_ACCOUNT_LABELS
            for item in failed
        )
        if failed_labels:
            lines.extend(
                [
                    "",
                    "⚠️ 일부 정보를 불러오지 못했습니다",
                    f"불러오지 못한 항목  {', '.join(failed_labels)}",
                ]
            )
        if failed_unknown:
            lines.extend(["", "⚠️ 계좌 조회 상태를 확인할 수 없습니다"])
        elif failed_unknown_item:
            lines.extend(["", "⚠️ 알 수 없는 계좌 조회 실패가 있습니다"])
        lines.extend(["", f"기준  {_kst(_field(summary, 'as_of'))}"])
        return "\n".join(lines)

    def positions(self, summary: Any) -> str:
        positions_raw = _field(summary, "positions", ())
        positions = _sequence(positions_raw)
        positions_unknown = positions is None
        positions = positions or ()
        corrupted_raw = _field(summary, "corrupted_rows", ())
        corrupted = _sequence(corrupted_raw)
        corrupted_unknown = corrupted is None
        corrupted = corrupted or ()
        lines = ["📦 관리 포지션"]
        if not positions:
            lines.extend(["", "현재 관리 중인 포지션이 없습니다."])
        else:
            for index, item in enumerate(positions, start=1):
                position = (
                    item[1]
                    if isinstance(item, tuple) and len(item) == 2
                    else item
                )
                state = _field(position, "state")
                state = getattr(state, "value", state)
                state_label = (
                    _POSITION_STATUS_LABELS.get(state, "확인 필요")
                    if isinstance(state, str)
                    else "확인 필요"
                )
                symbol = _field(position, "symbol")
                if not (
                    isinstance(symbol, str)
                    and len(symbol) == 6
                    and symbol.isascii()
                    and symbol.isdigit()
                ):
                    symbol = _UNAVAILABLE
                quantity = _count(_field(position, "quantity"), "주")
                lines.extend(
                    [
                        "",
                        f"{index}. {symbol}",
                        f"상태  {state_label}",
                        f"수량  {quantity}",
                        f"평균 진입가  {_won(_field(position, 'entry_price'))}",
                    ]
                )
        if positions_unknown:
            lines.extend(["", "⚠️ 포지션 목록을 확인할 수 없습니다"])
        if corrupted_unknown:
            lines.extend(["", "⚠️ 손상 포지션 건수를 확인할 수 없습니다"])
        corrupted_count = len(corrupted)
        if corrupted_count:
            lines.extend(["", f"⚠️ 읽을 수 없는 포지션  {corrupted_count:,}건"])
        return "\n".join(lines)

    def help(self) -> str:
        return "\n".join(
            (
                "🤖 OhMyStock 명령어",
                "",
                "조회",
                "/status     시스템 상태",
                "/account    계좌와 손익",
                "/positions  관리 포지션",
                "",
                "제어",
                "/pause      자동 일정 일시정지",
                "/stop       신규 진입 중지",
                "",
                "확인 필요",
                "/resume          자동 일정 재개",
                "/liquidate_all   관리 포지션 전체 청산",
            )
        )

    def confirmation(self, kind: CommandKind, command: str) -> str:
        if kind is CommandKind.RESUME:
            impact = "자동 일정 재개"
            detail = "자동 일정이 다시 실행되어 예정된 작업이 시작될 수 있습니다."
        elif kind is CommandKind.LIQUIDATE_ALL:
            impact = "관리 포지션 전체 청산"
            detail = "계좌 전체가 아니라 관리 포지션만 청산합니다."
        else:
            raise ValueError("confirmation is supported only for risky commands")
        return "\n".join(
            (
                "⚠️ 확인 필요",
                "",
                f"영향  {impact}",
                detail,
                "",
                "계속하려면 만료 전에 아래 명령을 실행하세요.",
                command,
            )
        )

    def control_result(
        self, kind: CommandKind, status: str, *, applied: bool = True
    ) -> str:
        if not applied:
            return "\n".join(
                (
                    "⚠️ 적용하지 못했습니다",
                    "",
                    "요청한 변경을 현재 상태에 적용할 수 없습니다.",
                    "현재 상태를 확인해 주세요.",
                )
            )
        if kind is CommandKind.PAUSE and status == "succeeded":
            return "✅ 완료\n\n자동 일정을 일시정지했습니다."
        if kind is CommandKind.STOP and status == "succeeded":
            return "\n".join(
                (
                    "✅ 완료",
                    "",
                    "신규 진입만 중지했습니다.",
                    "기존 포지션 감시는 계속됩니다.",
                    (
                        "이 중지는 현재 실행 범위이며 재기동 또는 다음 거래일에는 "
                        "자동매매가 재개될 수 있습니다."
                    ),
                    "영속 정지는 설정을 변경하세요.",
                )
            )
        if kind is CommandKind.RESUME and status == "succeeded":
            return "✅ 완료\n\n자동 일정을 재개했습니다."
        if kind is CommandKind.LIQUIDATE_ALL:
            if status in {"accepted", "in_progress"}:
                return "\n".join(
                    (
                        "✅ 요청 접수",
                        "",
                        "관리 포지션 청산 요청을 접수했습니다.",
                        "완료 여부는 후속 상태에서 확인해 주세요.",
                    )
                )
            if status == "succeeded":
                return "✅ 완료\n\n관리 포지션 청산을 완료했습니다."
            if status == "succeeded_no_targets":
                return "✅ 완료\n\n청산할 관리 포지션이 없습니다."
            if status == "succeeded_balance_remains":
                return "\n".join(
                    (
                        "⚠️ 관리 포지션 청산 완료",
                        "",
                        "계좌에 미관리 잔고가 남아 있습니다.",
                        "브로커 잔고를 별도로 확인해 주세요.",
                    )
                )
            if status == "unavailable_market_closed":
                return "\n".join(
                    (
                        "⚠️ 적용하지 못했습니다",
                        "",
                        "장이 열려 있지 않아 매도 주문을 내지 않았습니다.",
                        "다음 거래 가능 시각에 다시 확인해 주세요.",
                    )
                )
            if status == "unavailable_market_close_incomplete":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "장 마감까지 청산되지 않은 관리 포지션이 있습니다.",
                        "브로커 잔고와 미체결 주문을 확인해 주세요.",
                    )
                )
            if status == "unavailable_trading_halt":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "거래정지 포지션을 확인해 주세요.",
                        "매도 주문은 실행되지 않았습니다.",
                    )
                )
            if status == "unavailable_open_orders":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "기존 미체결 매도와 잔고를 확인해 주세요.",
                        "중복 매도 주문은 실행하지 않았습니다.",
                    )
                )
            if status == "unavailable_state_changed":
                return "\n".join(
                    (
                        "⚠️ 적용하지 못했습니다",
                        "",
                        "확인 후 포지션 상태가 바뀌었습니다.",
                        "현재 포지션을 확인하고 새 요청을 만들어 주세요.",
                    )
                )
            if status == "unavailable_quantity_mismatch":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "관리 기록과 브로커 잔고 수량이 일치하지 않습니다.",
                        "매도 주문은 실행하지 않았습니다.",
                    )
                )
            if status == "unavailable_trading_inactive":
                return "\n".join(
                    (
                        "⚠️ 적용하지 못했습니다",
                        "",
                        "자동매매가 실행 중이 아니어서 청산을 시작하지 않았습니다.",
                        "현재 거래 상태와 관리 포지션을 확인해 주세요.",
                    )
                )
            if status == "unavailable_preflight_reconciliation":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "주문 전 잔고와 미체결 상태를 확인하지 못했습니다.",
                        "매도 주문은 실행하지 않았습니다.",
                    )
                )
            if status == "unavailable_post_accept_reconciliation":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "청산 접수 후 주문 여부를 확인할 수 없습니다.",
                        "다시 청산하지 말고 브로커 잔고와 미체결 주문을 확인해 주세요.",
                    )
                )
            if status == "unavailable_intent_active":
                return "\n".join(
                    (
                        "⚠️ 적용하지 못했습니다",
                        "",
                        "다른 관리 포지션 청산 요청이 처리 중입니다.",
                        "현재 포지션과 이전 요청 결과를 확인해 주세요.",
                    )
                )
            if status == "unavailable_persistence":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "청산 요청 상태를 저장하지 못했습니다.",
                        "매도 주문은 시작하지 않았습니다.",
                    )
                )
            if status == "unavailable_unknown_intent":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "청산 요청의 복구 정보를 찾지 못했습니다.",
                        "브로커 잔고와 미체결 주문을 확인해 주세요.",
                    )
                )
            if status == "unavailable_position_remains":
                return "\n".join(
                    (
                        "🚨 처리 결과를 확인해야 합니다",
                        "",
                        "청산되지 않은 관리 포지션이 남아 있습니다.",
                        "브로커 잔고와 미체결 주문을 확인해 주세요.",
                    )
                )
        return "\n".join(
            (
                "🚨 처리 결과를 확인해야 합니다",
                "",
                "명령을 다시 실행하지 말고 현재 상태를 확인해 주세요.",
            )
        )

    def existing_result(self, kind: CommandKind, status: str) -> str:
        if kind is CommandKind.CONFIRM and status == "confirmation_invalid":
            return "⚠️ 확인 요청이 유효하지 않습니다.\n\n새 확인 요청을 만들어 주세요."
        return self.control_result(kind, status)
