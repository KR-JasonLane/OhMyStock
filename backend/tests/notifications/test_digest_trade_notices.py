import pytest

from app.domain.notifications.digest import DigestTradeNotice
from app.store.digest_trade_notices import (collect_trade_notices,
                                            normalize_trade_warning)


def test_유동성경고는_종목과금액만_구조화한다():
    """유동성 수치 파싱이 깨지면 고정 한국어 표시의 근거가 사라진다."""
    notice = normalize_trade_warning(
        "003960: entry dropped — liquidity: avg value "
        "252,557,535 < 1,000,000,000")

    assert notice == DigestTradeNotice(
        "liquidity", "003960", 252_557_535, 1_000_000_000)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("no analysis result yet — will retry within entry window",
         DigestTradeNotice("analysis_wait")),
        ("analysis signal date mismatch (signal 2026-07-27, expected 2026-07-28) — "
         "stale or future/look-ahead signal; will retry within entry window",
         DigestTradeNotice("analysis_wait")),
        ("analysis picks empty — no entries today",
         DigestTradeNotice("analysis_empty")),
        ("005930: entry dropped — already held (§6-3.3)",
         DigestTradeNotice("already_held", "005930")),
        ("005930: entry blocked — broker already holds this symbol immediately "
         "before order", DigestTradeNotice("already_held", "005930")),
        ("005930: entry dropped — reentry cooldown (recently closed)",
         DigestTradeNotice("reentry_cooldown", "005930")),
        ("005930: entry dropped — free slots exhausted (0)",
         DigestTradeNotice("capacity", "005930")),
        ("005930: pick missing context/quote",
         DigestTradeNotice("missing_context", "005930")),
        ("005930: entry dropped — price missing (signal 0, current 0)",
         DigestTradeNotice("missing_price", "005930")),
        ("005930: entry dropped — gap guard: current 10,500 vs signal 10,000 "
         "(+5.00% > ±3.00%)",
         DigestTradeNotice("gap_guard", "005930")),
        ("005930: pre-entry requote failed — using batch snapshot",
         DigestTradeNotice("requote_fallback", "005930")),
        ("quote polling failing (3 consecutive) — positions unmonitored, "
         "polling continues", DigestTradeNotice("quote_unstable")),
    ],
)
def test_알려진경고는_허용코드만_반환한다(line, expected):
    """허용 패턴이 바뀌어도 자유 경고가 Telegram으로 새면 실패한다."""
    assert normalize_trade_warning(line) == expected


def test_빈줄과_전용상태경고는_다이제스트경고에서제외한다():
    """중복 표시가 생기면 전용 상태 절과 경고 절이 같은 이상을 반복한다."""
    assert normalize_trade_warning("  \t") is None
    assert normalize_trade_warning("kill switch activated") is None
    assert normalize_trade_warning("scheduler gave up") is None
    assert normalize_trade_warning("notification dead letter") is None


@pytest.mark.parametrize(
    "line",
    [
        "kill switch activated unexpectedly",
        "scheduler gave up with detail",
        "notification dead letter extra",
    ],
)
def test_전용상태문구는_정확한fullmatch가아니면_unknown이다(line):
    assert normalize_trade_warning(line) == DigestTradeNotice("unknown")


@pytest.mark.parametrize(
    "line",
    [
        "analysis signal date mismatch (signal 2026-02-30, expected 2026-02-27) — "
        "stale or future/look-ahead signal; will retry within entry window",
        "prefix analysis signal date mismatch (signal 2026-07-27, expected "
        "2026-07-28) — stale or future/look-ahead signal; will retry within "
        "entry window",
        "005930: entry dropped — gap guard: current VALUE vs signal VALUE",
        "005930: entry dropped — gap guard: current 10500 vs signal 10,000 "
        "(+5% > ±3.00%)",
        "order status requires reconciliation",
    ],
)
def test_분석날짜와_gap형식이손상되면_unknown이다(line):
    assert normalize_trade_warning(line) == DigestTradeNotice("unknown")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("005930: entry blocked by single-order cap "
         "(single order cap exceeded: 150000 > 100000) — skipped, batch continues",
         DigestTradeNotice("capacity", "005930")),
        ("entry batch stopped by daily cap: daily order cap exceeded — "
         "new entries stopped", DigestTradeNotice("capacity")),
        ("all candidates dropped for technical reasons — will retry within entry window",
         None),
        ("005930: entry unresolved (fill state unknown) — mini reconcile",
         DigestTradeNotice("order_attention", "005930")),
        ("pending-exit check failing (4 consecutive) — 2 exit order(s) unverifiable",
         DigestTradeNotice("order_attention")),
        ("005930: exit submit failed 3 times — EXIT_FAILED, manual intervention "
         "required", DigestTradeNotice("order_attention", "005930")),
        ("005930: partial-fill audit persistence failed (RuntimeError) — "
         "order tracking continues", DigestTradeNotice("order_attention", "005930")),
        ("005930: entry fill audit persistence failed (RuntimeError) — "
         "position monitoring continues", DigestTradeNotice("order_attention", "005930")),
        ("005930: exit fill audit persistence failed (RuntimeError) — "
         "durable audit gap recorded", DigestTradeNotice("order_attention", "005930")),
        ("005930: exit reconciliation has no position id — manual intervention "
         "required", DigestTradeNotice("order_attention", "005930")),
        ("005930: trading halted (거래정지) — auto-exit impossible, manual "
         "attention required", DigestTradeNotice("unknown")),
        ("005930: verified entry has no recorded source order — fill alert "
         "unavailable, monitoring continues",
         DigestTradeNotice("order_attention", "005930")),
        ("005930: ownership is ambiguous after exit order disappearance — "
         "EXITING retained, manual review required",
         DigestTradeNotice("order_attention", "005930")),
        ("005930: exit balance is zero but position snapshot is missing — "
         "manual intervention required",
         DigestTradeNotice("order_attention", "005930")),
        ("005930: liquidation incomplete at market close — EXIT_FAILED "
         "(still held)", DigestTradeNotice("order_attention", "005930")),
    ],
)
def test_실제producer안정템플릿을_승인코드로정규화한다(line, expected):
    assert normalize_trade_warning(line) == expected


def test_알수없는경고는_원문없이_unknown으로_축약한다():
    """정규화 실패가 자유 텍스트를 경고 DTO에 보관하면 비밀이 노출된다."""
    secret = "unexpected warning TOKEN_RAW_SECRET"

    notice = normalize_trade_warning(secret)

    assert notice == DigestTradeNotice("unknown")
    assert secret not in repr(notice)


def test_손상된유동성숫자는_unknown으로_축약한다():
    """손상 수치를 안전한 유동성 경고로 오인하면 잘못된 운영 판단을 준다."""
    assert normalize_trade_warning(
        "003960: entry dropped — liquidity: avg value -1 < secret"
    ) == DigestTradeNotice("unknown")


def test_collect는_순서를지키고_중복제거후_5건과전체수를반환한다():
    """중복 제거와 상한 계산이 어긋나면 외 N건이 실제 경고를 숨긴다."""
    warnings = (
        "no analysis result yet — will retry within entry window\n"
        "003960: entry dropped — liquidity: avg value 1 < 10",
        "003960: entry dropped — liquidity: avg value 1 < 10\n"
        "005930: entry dropped — already held (§6-3.3)\n"
        "000660: entry dropped — reentry cooldown (recently closed)\n"
        "035420: entry dropped — free slots exhausted (0)\n"
        "unexpected warning A\nunexpected warning B",
    )

    notices, count = collect_trade_notices(warnings)

    assert len(notices) == 5
    assert notices[0] == DigestTradeNotice("analysis_wait")
    assert notices[1] == DigestTradeNotice("liquidity", "003960", 1, 10)
    assert count == 7


def test_collect는_같은_unknown원문만_내부적으로_중복제거한다():
    """서로 다른 미확인 경고가 하나로 계산되면 초과 수가 줄어든다."""
    notices, count = collect_trade_notices((
        "unrecognized A\nunrecognized A\nunrecognized B",
    ))

    assert notices == (DigestTradeNotice("unknown"),)
    assert count == 2


def test_collect는_unknown이여섯번째여도_마지막표시를대체해반드시포함한다():
    warnings = (
        "no analysis result yet — will retry within entry window\n"
        "003960: entry dropped — liquidity: avg value 1 < 10\n"
        "005930: entry dropped — already held (§6-3.3)\n"
        "000660: entry dropped — reentry cooldown (recently closed)\n"
        "035420: entry dropped — free slots exhausted (0)\n"
        "unrecognized late warning",
    )

    notices, count = collect_trade_notices(warnings)

    assert notices == (
        DigestTradeNotice("analysis_wait"),
        DigestTradeNotice("liquidity", "003960", 1, 10),
        DigestTradeNotice("already_held", "005930"),
        DigestTradeNotice("reentry_cooldown", "000660"),
        DigestTradeNotice("unknown"),
    )
    assert count == 6


def test_collect는_strip한_unknown줄을같은항목으로중복제거한다():
    notices, count = collect_trade_notices((" unknown A \nunknown A",))

    assert notices == (DigestTradeNotice("unknown"),)
    assert count == 1


def test_collect는_시세주의표시는합치되_종목별사건수를유지한다():
    warnings = (
        "005930: quote missing 4 consecutive polls "
        "(not halted — network/feed issue suspected)\n"
        "000660: quote missing 5 consecutive polls "
        "(not halted — network/feed issue suspected)",
    )

    notices, count = collect_trade_notices(warnings)

    assert notices == (DigestTradeNotice("quote_unstable"),)
    assert count == 2


def test_collect는_주문주의표시는합치되_종목별사건수를유지한다():
    warnings = (
        "005930: entry unresolved (fill state unknown) — mini reconcile\n"
        "000660: exit submit failed 3 times — EXIT_FAILED, manual intervention "
        "required",
    )

    notices, count = collect_trade_notices(warnings)

    assert notices == (DigestTradeNotice("order_attention"),)
    assert count == 2


@pytest.mark.parametrize(
    "warnings",
    [
        (7,),  # type: ignore[arg-type]
        ("x" * 16_385,
         "005930: entry dropped — already held (§6-3.3)"),
        ("x" * 1_025,
         "005930: entry dropped — already held (§6-3.3)"),
        ("x\n" * 513,
         "005930: entry dropped — already held (§6-3.3)"),
        tuple("unknown" for _ in range(257)) + (
            "005930: entry dropped — already held (§6-3.3)",),
        tuple(
            f"{number:06d}: entry dropped — already held (§6-3.3)"
            for number in range(129)
        ),
    ],
)
def test_collect는_구조와크기상한초과를_예외없이단일unknown으로축약한다(warnings):
    assert collect_trade_notices(warnings) == (
        (DigestTradeNotice("unknown"),), 1)
