"""거래 실행 경고를 다이제스트용 허용목록 DTO로 축약한다.

자유 형식 ``trade_runs.warnings``는 이 모듈 밖으로 전달하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256
import re

from app.domain.notifications.digest import DigestTradeNotice


_SYMBOL = r"(?P<symbol>[A-Za-z0-9]{6})"
_COMMA_INT = r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})*|[1-9][0-9]*)"
_POSITIVE_COMMA_INT = r"(?:[1-9][0-9]{0,2}(?:,[0-9]{3})*|[1-9][0-9]*)"
_AMOUNT = rf"(?P<amount>{_COMMA_INT})"
_LIQUIDITY = re.compile(
    rf"^{_SYMBOL}: entry dropped — liquidity: avg value {_AMOUNT} < "
    rf"(?P<threshold>{_COMMA_INT})$")
_ANALYSIS_WAIT = re.compile(
    r"^analysis signal date mismatch \(signal (?P<signal_date>\d{4}-\d{2}-\d{2}), "
    r"expected (?P<expected_date>\d{4}-\d{2}-\d{2})\) — stale or "
    r"future/look-ahead signal; will retry within entry window$")
_KNOWN_PATTERNS = (
    (re.compile(r"^no analysis result yet — will retry within entry window$"),
     "analysis_wait"),
    (re.compile(r"^analysis picks empty — no entries today$"), "analysis_empty"),
    (re.compile(rf"^{_SYMBOL}: entry dropped — already held \(§6-3\.3\)$"),
     "already_held"),
    (re.compile(rf"^{_SYMBOL}: entry blocked — broker already holds this symbol "
                r"immediately before order$"), "already_held"),
    (re.compile(rf"^{_SYMBOL}: entry dropped — reentry cooldown "
                r"\(recently closed\)$"), "reentry_cooldown"),
    (re.compile(rf"^{_SYMBOL}: entry dropped — (?:free slots exhausted "
                r"\([0-9]+\)|no free slots \(held [0-9]+/[0-9]+\)|"
                r"available_krw is 0|slot budget is 0 — available funds below one slot|"
                r"slot budget buys 0 shares at [0-9,]+)$"), "capacity"),
    (re.compile(rf"^{_SYMBOL}: pick missing context/quote$"), "missing_context"),
    (re.compile(rf"^{_SYMBOL}: entry dropped — price missing "
                r"\(signal [0-9,]+, current [0-9,]+\)$"), "missing_price"),
    (re.compile(rf"^{_SYMBOL}: entry dropped — gap guard: current "
                rf"{_POSITIVE_COMMA_INT} vs signal {_POSITIVE_COMMA_INT} "
                r"\([+-][0-9]+\.[0-9]{2}% > ±[0-9]+\.[0-9]{2}%\)$"),
     "gap_guard"),
    (re.compile(rf"^{_SYMBOL}: pre-entry requote failed — using batch snapshot$"),
     "requote_fallback"),
    (re.compile(r"^quote polling failing \([0-9]+ consecutive\) — positions "
                r"unmonitored, polling continues$"), "quote_unstable"),
    (re.compile(rf"^{_SYMBOL}: quote missing [0-9]+ consecutive polls "
                r"\(not halted — network/feed issue suspected\)$"), "quote_unstable"),
    (re.compile(rf"^{_SYMBOL}: entry blocked by single-order cap "
                r"\(single order cap exceeded: [0-9]+ > [0-9]+\) — skipped, "
                r"batch continues$"), "capacity"),
    (re.compile(r"^entry batch stopped by daily cap: daily order caps? "
                r"(?:exhausted|exceeded) — new entries stopped$"), "capacity"),
    (re.compile(r"^all candidates dropped for technical reasons — will retry "
                r"within entry window$"), None),
    (re.compile(rf"^{_SYMBOL}: entry unresolved \([^()\r\n]{{1,96}}\) — "
                r"mini reconcile$"), "order_attention"),
    (re.compile(r"^pending-exit check failing \([0-9]+ consecutive\) — "
                r"[0-9]+ exit order\(s\) unverifiable$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: exit submit failed [0-9]+ times — EXIT_FAILED, "
                r"manual intervention required$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: partial-fill audit persistence failed "
                r"\([A-Za-z_][A-Za-z0-9_]{0,63}\) — order tracking continues$"),
     "order_attention"),
    (re.compile(rf"^{_SYMBOL}: entry fill audit persistence failed "
                r"\([A-Za-z_][A-Za-z0-9_]{0,63}\) — position monitoring continues$"),
     "order_attention"),
    (re.compile(rf"^{_SYMBOL}: exit fill audit persistence failed "
                r"\([A-Za-z_][A-Za-z0-9_]{0,63}\) — durable audit gap recorded$"),
     "order_attention"),
    (re.compile(rf"^{_SYMBOL}: exit reconciliation has no position id — "
                r"manual intervention required$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: verified entry has no recorded source order — "
                r"fill alert unavailable, monitoring continues$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: ownership is ambiguous after exit order "
                r"disappearance — EXITING retained, manual review required$"),
     "order_attention"),
    (re.compile(rf"^{_SYMBOL}: exit balance is zero but position snapshot is "
                r"missing — manual intervention required$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: liquidation incomplete at market close — "
                r"EXIT_FAILED \(still held\)$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: mixed managed/unmanaged ownership detected after "
                r"entry — EXITING retained; automatic sell disabled, manual "
                r"reconciliation required$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: balance ownership is ambiguous after entry cancel "
                r"\(db_managed=[0-9]+, unmanaged_baseline=[0-9]+, "
                r"broker_total=[0-9]+\) — EXITING retained; automatic sell "
                r"disabled, manual reconciliation required$"), "order_attention"),
    (re.compile(rf"^{_SYMBOL}: exit order survived to market close — "
                r"EXIT_FAILED, position still held$"), "order_attention"),
)
_EXCLUDED = (
    re.compile(r"kill switch activated"),
    re.compile(r"scheduler gave up"),
    re.compile(r"notification dead letter"),
)
_MAX_DISPLAY_NOTICES = 5
_MAX_WARNING_ITEMS = 256
_MAX_WARNING_TEXT_CHARS = 16_384
_MAX_WARNING_LINE_CHARS = 1_024
_MAX_WARNING_LINES = 512
_MAX_UNIQUE_NOTICES = 128
_UNKNOWN_NOTICE = DigestTradeNotice("unknown")


def normalize_trade_warning(line: str) -> DigestTradeNotice | None:
    """경고 한 줄을 허용된 notice로 바꾸거나 안전하게 격리한다."""
    normalized = line.strip()
    if not normalized:
        return None
    if any(pattern.fullmatch(normalized) for pattern in _EXCLUDED):
        return None
    liquidity = _LIQUIDITY.fullmatch(normalized)
    if liquidity is not None:
        try:
            return DigestTradeNotice(
                "liquidity", liquidity["symbol"],
                int(liquidity["amount"].replace(",", "")),
                int(liquidity["threshold"].replace(",", "")),
            )
        except (TypeError, ValueError):
            return DigestTradeNotice("unknown")
    analysis_wait = _ANALYSIS_WAIT.fullmatch(normalized)
    if analysis_wait is not None:
        try:
            date.fromisoformat(analysis_wait["signal_date"])
            date.fromisoformat(analysis_wait["expected_date"])
        except ValueError:
            return _UNKNOWN_NOTICE
        return DigestTradeNotice("analysis_wait")
    for pattern, code in _KNOWN_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        if code is None:
            return None
        symbol = match.groupdict().get("symbol")
        try:
            return DigestTradeNotice(code, symbol)
        except ValueError:
            return _UNKNOWN_NOTICE
    return _UNKNOWN_NOTICE


def collect_trade_notices(
        warnings: Sequence[str | None],
) -> tuple[tuple[DigestTradeNotice, ...], int]:
    """발생 순서로 중복을 제거해 최대 다섯 notice와 전체 고유 수를 반환한다."""
    try:
        if len(warnings) > _MAX_WARNING_ITEMS:
            return ((_UNKNOWN_NOTICE,), 1)
    except (TypeError, OverflowError):
        return ((_UNKNOWN_NOTICE,), 1)
    notices: list[DigestTradeNotice] = []
    unique_keys: set[tuple[str, object]] = set()
    unknown_seen = False
    count = 0
    total_lines = 0
    for warning in warnings:
        if warning is None:
            continue
        if type(warning) is not str or len(warning) > _MAX_WARNING_TEXT_CHARS:
            return ((_UNKNOWN_NOTICE,), 1)
        lines = warning.splitlines()
        total_lines += len(lines)
        if total_lines > _MAX_WARNING_LINES:
            return ((_UNKNOWN_NOTICE,), 1)
        for line in lines:
            if len(line) > _MAX_WARNING_LINE_CHARS:
                return ((_UNKNOWN_NOTICE,), 1)
            notice = normalize_trade_warning(line)
            if notice is None:
                continue
            if notice.code == "unknown":
                normalized = line.strip()
                key = (
                    "unknown",
                    sha256(normalized.encode("utf-8", "surrogatepass")).digest(),
                )
                display_notice = _UNKNOWN_NOTICE
            else:
                key = ("notice", notice)
                if notice.code in {"quote_unstable", "order_attention"}:
                    display_notice = DigestTradeNotice(notice.code)
                else:
                    display_notice = notice
            if key in unique_keys:
                continue
            if len(unique_keys) >= _MAX_UNIQUE_NOTICES:
                return ((_UNKNOWN_NOTICE,), 1)
            unique_keys.add(key)
            count += 1
            if display_notice.code == "unknown":
                if unknown_seen:
                    continue
                unknown_seen = True
            elif display_notice in notices:
                continue
            notices.append(display_notice)
    visible = notices[:_MAX_DISPLAY_NOTICES]
    if unknown_seen and _UNKNOWN_NOTICE not in visible:
        if len(visible) < _MAX_DISPLAY_NOTICES:
            visible.append(_UNKNOWN_NOTICE)
        else:
            visible[-1] = _UNKNOWN_NOTICE
    return tuple(visible), count
