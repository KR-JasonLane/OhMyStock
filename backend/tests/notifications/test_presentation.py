from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.domain.notifications.presentation import (
    TelegramCommandPresenter,
    _field,
    _kst,
    _won,
)
from app.domain.notifications.models import CommandKind


def _normal_status():
    return {
        "scheduler": {"paused": False, "dead": False},
        "trading": {
            "run_id": None,
            "status": "idle",
            "positions_count": 0,
            "kill_switch": None,
        },
        "telegram": {
            "enabled": True,
            "dead": False,
            "degraded": False,
            "backoff_components": (),
            "dispatcher": {"control_delay_warning": False},
            "poller": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
                "last_success_at": "2026-07-25T15:06:00+00:00",
                "backoff_reason": None,
            },
            "commands": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
            },
            "queries": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
            },
            "reconciliation": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
            },
            "projector": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
            },
            "maintenance": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
            },
            "sender": {
                "dead": False,
                "last_error_kind": None,
                "transient_failures": 0,
                "ephemeral": {
                    "state": "running",
                    "pending": 0,
                    "backoff_reason": None,
                },
                "outbox": {
                    "state": "running",
                    "initialized": True,
                    "pending": 0,
                    "sending": 0,
                    "dead_letter": 0,
                },
            },
        },
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_250_000, "1,250,000원"),
        (-12_300, "-12,300원"),
        (0, "0원"),
        (None, "확인 불가"),
        (True, "확인 불가"),
        ("invalid", "확인 불가"),
    ],
)
def test_금액은_원단위로안전하게표시한다(value, expected):
    assert _won(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-25T15:06:00+00:00", "2026-07-26 00:06:00 KST"),
        (
            datetime(2026, 7, 25, 15, 6, tzinfo=timezone.utc),
            "2026-07-26 00:06:00 KST",
        ),
        (datetime(2026, 7, 26, 0, 6), "확인 불가"),
        ("not-a-time", "확인 불가"),
        (None, "확인 불가"),
    ],
)
def test_시각은_timezone이확인될때만_KST로표시한다(value, expected):
    assert _kst(value) == expected


def test_필드는_mapping과객체에서같이읽고_누락기본값을쓴다():
    assert _field({"value": 3}, "value") == 3
    assert _field(SimpleNamespace(value=4), "value") == 4
    assert _field(None, "value", "fallback") == "fallback"


def test_status_정상은_내부필드없이핵심만표시한다():
    text = TelegramCommandPresenter().status(_normal_status())

    assert text.startswith("✅ 시스템 정상")
    assert "📅 자동 일정  운영 중" in text
    assert "📈 자동매매  대기" in text
    assert "📦 관리 포지션  0개" in text
    assert "🤖 Telegram  정상" in text
    assert "📨 대기 메시지  0건" in text
    assert "최근 확인  2026-07-26 00:06:00 KST" in text
    assert "enabled=" not in text
    assert "None" not in text
    assert "+00:00" not in text
    assert "run_id" not in text


@pytest.mark.parametrize(
    ("path", "value", "severity", "cause"),
    [
        (("telegram", "poller", "dead"), True, "🚨", "Telegram 수신 중단"),
        (
            ("telegram", "sender", "outbox", "state"),
            "dead",
            "🚨",
            "메시지 전송 중단",
        ),
        (
            ("telegram", "backoff_components"),
            ("poller",),
            "⚠️",
            "Telegram 수신 지연",
        ),
        (
            ("telegram", "sender", "outbox", "dead_letter"),
            2,
            "⚠️",
            "전송 실패 메시지  2건",
        ),
        (
            ("trading", "kill_switch"),
            "stop_new_entries",
            "⚠️",
            "신규 진입 중지 활성",
        ),
    ],
)
def test_status_이상은_정상으로축소하지않고한국어원인을표시한다(
    path, value, severity, cause
):
    status = deepcopy(_normal_status())
    target = status
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value

    text = TelegramCommandPresenter().status(status)

    assert text.startswith(f"{severity} 시스템 ")
    assert cause in text
    assert not text.startswith("✅")


@pytest.mark.parametrize(
    ("path", "cause"),
    [
        (("scheduler", "dead"), "자동 일정 중단"),
        (("telegram", "dead"), "Telegram 서비스 중단"),
        (("telegram", "commands", "dead"), "명령 처리 중단"),
        (("telegram", "reconciliation", "dead"), "명령 복구 중단"),
        (("telegram", "projector", "dead"), "운영 알림 생성 중단"),
        (("telegram", "maintenance", "dead"), "Telegram 보존 정리 중단"),
    ],
)
def test_status_구성요소_dead는_시스템장애로승격한다(path, cause):
    status = deepcopy(_normal_status())
    target = status
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = True

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("🚨 시스템 장애")
    assert cause in text


@pytest.mark.parametrize(
    ("path", "value", "cause"),
    [
        (("scheduler",), None, "자동 일정 상태 확인 필요"),
        (("telegram", "enabled"), False, "Telegram 비활성"),
        (("telegram", "poller"), None, "Telegram 수신 상태 확인 필요"),
        (
            ("telegram", "sender", "outbox", "state"),
            "unknown",
            "메시지 전송 상태 확인 필요",
        ),
    ],
)
def test_status_누락비활성미지값은_시스템정상으로축소하지않는다(
    path, value, cause
):
    status = deepcopy(_normal_status())
    target = status
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert cause in text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pending", None),
        ("pending", "0"),
        ("dead_letter", -1),
        ("dead_letter", "0"),
    ],
)
def test_status_outbox핵심수치가비정상이면_전달상태를확인시킨다(
    field, value
):
    status = deepcopy(_normal_status())
    status["telegram"]["sender"]["outbox"][field] = value

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert "메시지 전달 상태 확인 필요" in text
    assert "🤖 Telegram  주의" in text


@pytest.mark.parametrize("field", ["pending", "dead_letter"])
def test_status_outbox핵심수치누락도_전달상태를확인시킨다(field):
    status = deepcopy(_normal_status())
    del status["telegram"]["sender"]["outbox"][field]

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert "메시지 전달 상태 확인 필요" in text


@pytest.mark.parametrize(
    ("component", "error_kind", "cause"),
    [
        ("poller", "rate_limited", "Telegram 수신 지연"),
        ("sender", "temporary_error", "Telegram 전송 지연"),
        ("projector", "temporary_error", "운영 알림 생성 지연"),
        ("maintenance", "rate_limited", "Telegram 보존 정리 지연"),
    ],
)
def test_status_실제supervisor_backoff필드는_Telegram주의로승격한다(
    component, error_kind, cause
):
    status = deepcopy(_normal_status())
    status["telegram"][component]["last_error_kind"] = error_kind
    status["telegram"][component]["transient_failures"] = 2
    status["telegram"]["backoff_components"] = (component,)

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert "🤖 Telegram  주의" in text
    assert cause in text


def test_status_canonical이있으면_raw오류필드를중복판정하지않는다():
    status = deepcopy(_normal_status())
    status["telegram"]["poller"]["last_error_kind"] = "temporary_error"
    status["telegram"]["poller"]["transient_failures"] = 2

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("✅ 시스템 정상")
    assert "Telegram 수신 지연" not in text


def test_status_canonical구성요소손상은_원문없이계약경고로축소한다():
    status = deepcopy(_normal_status())
    status["telegram"]["backoff_components"] = (
        {"raw": "CANONICAL_RAW_SECRET"},
    )

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert "Telegram 상태 계약 확인 필요" in text
    assert "CANONICAL_RAW_SECRET" not in text


@pytest.mark.parametrize(
    ("component", "cause"),
    [
        ("control_delay", "Telegram 제어 명령 처리 지연"),
        ("ephemeral", "Telegram 확인 응답 전송 지연"),
        ("commands", "Telegram 명령 처리 지연"),
    ],
)
def test_status_canonical_degraded구성요소만_고정한국어로표시한다(
    component, cause
):
    status = deepcopy(_normal_status())
    status["telegram"]["degraded"] = True
    status["telegram"]["backoff_components"] = (component,)

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert cause in text


@pytest.mark.parametrize(
    ("path", "value", "cause"),
    [
        (("trading", "positions_count"), None, "관리 포지션 수 확인 필요"),
        (("trading", "positions_count"), "0", "관리 포지션 수 확인 필요"),
        (
            ("telegram", "poller", "last_success_at"),
            None,
            "최근 Telegram 확인 시각 확인 필요",
        ),
        (
            ("telegram", "poller", "last_success_at"),
            "not-a-time",
            "최근 Telegram 확인 시각 확인 필요",
        ),
        (("trading", "run_id"), 0, "자동매매 실행 식별자 확인 필요"),
        (("trading", "run_id"), "7", "자동매매 실행 식별자 확인 필요"),
    ],
)
def test_status_필수타입손상은_최소시스템주의로승격한다(path, value, cause):
    status = deepcopy(_normal_status())
    target = status
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    if path[-1] == "run_id":
        status["trading"]["status"] = "running"

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert cause in text


def test_status_idle인데_run_id가있으면_조합손상으로주의한다():
    status = deepcopy(_normal_status())
    status["trading"]["run_id"] = 7

    text = TelegramCommandPresenter().status(status)

    assert text.startswith("⚠️ 시스템 주의")
    assert "자동매매 상태 조합 확인 필요" in text


def test_status_알수없는거래상태는_확인필요로표시한다():
    status = _normal_status()
    status["trading"]["status"] = "brand_new_state"

    text = TelegramCommandPresenter().status(status)

    assert "📈 자동매매  확인 필요" in text
    assert "brand_new_state" not in text


@pytest.mark.parametrize(
    ("status", "severity", "cause"),
    [
        ("failed", "🚨", "자동매매 실패"),
        ("needs_attention", "⚠️", "자동매매 상태 확인 필요"),
    ],
)
def test_status_거래실패와확인필요를_시스템정상으로표시하지않는다(
    status, severity, cause
):
    snapshot = _normal_status()
    snapshot["trading"]["status"] = status

    text = TelegramCommandPresenter().status(snapshot)

    assert text.startswith(severity)
    assert cause in text


def test_status_예상하지못한복합상태값은_원문없이확인필요로축소한다():
    status = _normal_status()
    status["trading"]["status"] = {"raw": "TRADING_RAW_SECRET"}

    text = TelegramCommandPresenter().status(status)

    assert "📈 자동매매  확인 필요" in text
    assert "TRADING_RAW_SECRET" not in text


@pytest.mark.parametrize(
    ("status", "label"),
    [("stopping", "중지 처리 중"), ("stopped", "중지됨")],
)
def test_status_알려진중지전이는_확인불가로축소하지않는다(status, label):
    snapshot = _normal_status()
    snapshot["trading"]["status"] = status

    text = TelegramCommandPresenter().status(snapshot)

    assert f"📈 자동매매  {label}" in text
    assert "자동매매 상태 확인 필요" not in text


@pytest.mark.parametrize(
    ("mode", "cause"),
    [
        ("stop_new_entries", "신규 진입 중지 활성"),
        ("liquidate_all", "관리 포지션 청산 활성"),
    ],
)
def test_status_킬스위치모드는_신규진입중지와관리청산을구분한다(mode, cause):
    snapshot = _normal_status()
    snapshot["trading"]["kill_switch"] = mode

    text = TelegramCommandPresenter().status(snapshot)

    assert cause in text
    if mode == "liquidate_all":
        assert "/positions에서 청산 진행을 확인" in text


def _account_summary(**overrides):
    values = {
        "deposit": 10_000_000,
        "available_deposit": 9_979_053,
        "total_eval": 0,
        "total_profit": 0,
        "total_return_rate": None,
        "realized_pnl": 0,
        "realized_pnl_confidence": "estimated",
        "as_of": "2026-07-25T15:06:00+00:00",
        "source": "broker+trade_store",
        "failed_fields": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_account_검증된금액과정확도만한국어로표시한다():
    text = TelegramCommandPresenter().account(_account_summary())

    assert "💰 계좌 요약" in text
    assert "예수금" in text and "10,000,000원" in text
    assert "주문 가능" in text and "9,979,053원" in text
    assert "보유주식 평가" in text and "0원" in text
    assert "총자산" in text and "확인 불가" in text
    assert "평가손익" in text
    assert "오늘 실현손익 (관리매매)" in text and "(추정)" in text
    assert "기준  2026-07-26 00:06:00 KST" in text
    assert "broker+trade_store" not in text
    assert "failed_fields" not in text


@pytest.mark.parametrize(
    ("failed_field", "label"),
    [
        ("deposit", "예수금"),
        ("balance", "잔고"),
        ("realized_pnl", "오늘 실현손익"),
    ],
)
def test_account_부분실패는_원천필드명없이한국어항목만경고한다(
    failed_field, label
):
    text = TelegramCommandPresenter().account(
        _account_summary(failed_fields=(failed_field,))
    )

    assert "⚠️ 일부 정보를 불러오지 못했습니다" in text
    assert f"불러오지 못한 항목  {label}" in text
    assert failed_field not in text
    assert "broker+trade_store" not in text


def test_account_예상하지못한복합타입은_원문없이확인불가로축소한다():
    text = TelegramCommandPresenter().account(
        _account_summary(
            deposit={"raw": "ACCOUNT_RAW_SECRET"},
            realized_pnl_confidence={"raw": "CONFIDENCE_RAW_SECRET"},
            failed_fields=({"raw": "FAILED_RAW_SECRET"},),
        )
    )

    assert "예수금          확인 불가" in text
    assert "정확도 확인 불가" in text
    assert "ACCOUNT_RAW_SECRET" not in text
    assert "CONFIDENCE_RAW_SECRET" not in text
    assert "FAILED_RAW_SECRET" not in text
    assert "⚠️ 알 수 없는 계좌 조회 실패가 있습니다" in text


def test_account_알수없는failed_field는_원문없이일반경고한다():
    text = TelegramCommandPresenter().account(
        _account_summary(failed_fields=("NEW_SECRET_FIELD",))
    )

    assert "⚠️ 알 수 없는 계좌 조회 실패가 있습니다" in text
    assert "NEW_SECRET_FIELD" not in text


@pytest.mark.parametrize("failed_fields", [42, "deposit", {"deposit": True}])
def test_account_잘못된실패항목컨테이너는_응답실패대신확인불가로축소한다(
    failed_fields,
):
    text = TelegramCommandPresenter().account(
        _account_summary(failed_fields=failed_fields)
    )

    assert "⚠️ 계좌 조회 상태를 확인할 수 없습니다" in text
    assert "deposit" not in text


def test_positions_빈목록은_정상적인없음으로표시한다():
    text = TelegramCommandPresenter().positions(
        {"positions": (), "corrupted_rows": ()}
    )

    assert text == "📦 관리 포지션\n\n현재 관리 중인 포지션이 없습니다."


def test_positions_복수행은_검증된필드와한국어상태만표시한다():
    positions = (
        (
            7,
            SimpleNamespace(
                symbol="005930",
                name="삼성전자",
                state="entered",
                quantity=3,
                entry_price=70_000,
            ),
        ),
        (
            8,
            SimpleNamespace(
                symbol="000660",
                name="SK하이닉스",
                state=SimpleNamespace(value="exiting"),
                quantity=2,
                entry_price=200_000,
            ),
        ),
    )

    text = TelegramCommandPresenter().positions(
        {"positions": positions, "corrupted_rows": ()}
    )

    assert "📦 관리 포지션" in text
    assert "005930" in text and "보유 중" in text
    assert "000660" in text and "청산 중" in text
    assert "3주" in text and "2주" in text
    assert "70,000원" in text and "200,000원" in text
    assert "삼성전자" not in text
    assert "SK하이닉스" not in text
    assert "id=" not in text
    assert "state=" not in text


def test_positions_손상행은_건수만경고하고원문을숨긴다():
    text = TelegramCommandPresenter().positions(
        {
            "positions": (),
            "corrupted_rows": ({"raw": "BROKER_RAW_SECRET"},),
        }
    )

    assert "⚠️ 읽을 수 없는 포지션  1건" in text
    assert "BROKER_RAW_SECRET" not in text
    assert "{'raw'" not in text


def test_positions_예상하지못한복합상태는_원문없이확인필요로축소한다():
    position = SimpleNamespace(
        symbol="005930",
        state={"raw": "POSITION_RAW_SECRET"},
        quantity=True,
        entry_price="invalid",
    )

    text = TelegramCommandPresenter().positions(
        {"positions": ((7, position),), "corrupted_rows": ()}
    )

    assert "상태  확인 필요" in text
    assert "수량  확인 불가" in text
    assert "평균 진입가  확인 불가" in text
    assert "POSITION_RAW_SECRET" not in text


@pytest.mark.parametrize("positions", [42, "raw-position", {"raw": "secret"}])
def test_positions_잘못된목록컨테이너는_응답실패대신경고로축소한다(positions):
    text = TelegramCommandPresenter().positions(
        {"positions": positions, "corrupted_rows": ()}
    )

    assert "⚠️ 포지션 목록을 확인할 수 없습니다" in text
    assert "raw-position" not in text
    assert "secret" not in text


def test_help_조회제어확인필요를구분하고_confirm은일반명령에서숨긴다():
    text = TelegramCommandPresenter().help()

    assert text.startswith("🤖 OhMyStock 명령어")
    assert "\n조회\n" in text
    assert "/status     시스템 상태" in text
    assert "/account    계좌와 손익" in text
    assert "/positions  관리 포지션" in text
    assert "\n제어\n" in text
    assert "/pause      자동 일정 일시정지" in text
    assert "/stop       신규 진입 중지" in text
    assert "\n확인 필요\n" in text
    assert "/resume          자동 일정 재개" in text
    assert "/liquidate_all   관리 포지션 전체 청산" in text
    assert "/confirm" not in text


@pytest.mark.parametrize(
    ("kind", "command", "impact"),
    [
        (CommandKind.RESUME, "/confirm RESUME_TOKEN", "자동 일정 재개"),
        (
            CommandKind.LIQUIDATE_ALL,
            "/confirm LIQUIDATE_TOKEN",
            "관리 포지션 전체 청산",
        ),
    ],
)
def test_confirmation_영향과전체명령을그대로표시한다(kind, command, impact):
    text = TelegramCommandPresenter().confirmation(kind, command)

    assert text.startswith("⚠️ 확인 필요")
    assert impact in text
    assert command in text
    if kind is CommandKind.LIQUIDATE_ALL:
        assert "관리 포지션만" in text


@pytest.mark.parametrize(
    ("kind", "status", "prefix", "expected"),
    [
        (CommandKind.PAUSE, "succeeded", "✅", "자동 일정을 일시정지했습니다"),
        (CommandKind.STOP, "succeeded", "✅", "신규 진입만 중지했습니다"),
        (CommandKind.RESUME, "succeeded", "✅", "자동 일정을 재개했습니다"),
        (
            CommandKind.LIQUIDATE_ALL,
            "accepted",
            "✅",
            "관리 포지션 청산 요청을 접수했습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "succeeded",
            "✅",
            "관리 포지션 청산을 완료했습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "succeeded_no_targets",
            "✅",
            "청산할 관리 포지션이 없습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "succeeded_balance_remains",
            "⚠️",
            "계좌에 미관리 잔고가 남아 있습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_market_closed",
            "⚠️",
            "장이 열려 있지 않아 매도 주문을 내지 않았습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_market_close_incomplete",
            "🚨",
            "장 마감까지 청산되지 않은 관리 포지션이 있습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_trading_halt",
            "🚨",
            "거래정지 포지션을 확인해 주세요",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_open_orders",
            "🚨",
            "기존 미체결 매도와 잔고를 확인해 주세요",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_state_changed",
            "⚠️",
            "확인 후 포지션 상태가 바뀌었습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_quantity_mismatch",
            "🚨",
            "관리 기록과 브로커 잔고 수량이 일치하지 않습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_intent_active",
            "⚠️",
            "다른 관리 포지션 청산 요청이 처리 중입니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_persistence",
            "🚨",
            "청산 요청 상태를 저장하지 못했습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_preflight_reconciliation",
            "🚨",
            "주문 전 잔고와 미체결 상태를 확인하지 못했습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_post_accept_reconciliation",
            "🚨",
            "청산 접수 후 주문 여부를 확인할 수 없습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_unknown_intent",
            "🚨",
            "청산 요청의 복구 정보를 찾지 못했습니다",
        ),
        (
            CommandKind.LIQUIDATE_ALL,
            "unavailable_position_remains",
            "🚨",
            "청산되지 않은 관리 포지션이 남아 있습니다",
        ),
    ],
)
def test_control_result_변경되거나접수된상태를한국어로표시한다(
    kind, status, prefix, expected
):
    text = TelegramCommandPresenter().control_result(kind, status)

    assert text.startswith(prefix)
    assert expected in text
    assert status not in text
    if kind is CommandKind.STOP:
        assert "기존 포지션 감시는 계속" in text
        assert "재기동 또는 다음 거래일" in text


@pytest.mark.parametrize(
    "kind",
    [CommandKind.PAUSE, CommandKind.STOP, CommandKind.RESUME],
)
def test_control_result_적용불가는_성공으로표시하지않는다(kind):
    text = TelegramCommandPresenter().control_result(
        kind, "needs_attention", applied=False
    )

    assert text.startswith("⚠️ 적용하지 못했습니다")
    assert "현재 상태를 확인해 주세요" in text
    assert "needs_attention" not in text


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        (CommandKind.PAUSE, "succeeded", "자동 일정을 일시정지했습니다"),
        (CommandKind.STOP, "succeeded", "신규 진입만 중지했습니다"),
        (
            CommandKind.LIQUIDATE_ALL,
            "failed",
            "처리 결과를 확인해야 합니다",
        ),
        (
            CommandKind.RESUME,
            "needs_attention",
            "처리 결과를 확인해야 합니다",
        ),
        (
            CommandKind.CONFIRM,
            "confirmation_invalid",
            "확인 요청이 유효하지 않습니다",
        ),
    ],
)
def test_existing_result_기존terminal을재실행없이안전하게표시한다(
    kind, status, expected
):
    text = TelegramCommandPresenter().existing_result(kind, status)

    assert expected in text
    assert status not in text
