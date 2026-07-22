"""결함 주입(스펙 §9) — FaultPolicy seam + 시나리오 정책.

- `FaultPolicy`: 무결함 기본 계약(R3가 seam 선행 생성). 매칭/계좌/API
  계층은 이 객체의 메서드로만 결함 여부를 조회한다(전역 뮤터블 플래그
  금지 — 개발자 스펙 리뷰 I2: 매칭 로직에 테스트용 if 산재 방지).
- `ScenarioFaultPolicy`(R5): 관리 API(/_replay/faults)가 조작하는 시나리오
  상태를 **이 인스턴스 안에만** 보관한다. MatchingEngine은 정책을 생성자
  주입으로 1회만 받으므로(아키텍트 R3 제약) /_replay/reset은 객체 교체가
  아니라 `clear()`의 **in-place 리셋**으로 구현된다.

시간 파라미터는 전부 **벽시계 초**(§5 — 배속 무관: 실서버 지연을 흉내내는
값이므로 배속으로 스케일하면 C1 방어선 타이밍이 왜곡된다).

§9 시나리오 표 ↔ 프리미티브 매핑(테스트 test_faults.py가 13종 전수 고정 —
익절/진입 지정가 억제는 별도 행·별도 테스트):
- ka10075 전파 지연 확대       → set_propagation_delay(seconds)
- 조회 TR 간헐 500/타임아웃    → set_api_fault(api_id, "http500"/"delay")
- 429 레이트리밋               → set_api_fault(api_id, "http429")
- 부분체결(x%만)               → set_fill_ratio(ratio)
- 취소 거부(rc!=0)             → add_reject_cancel(...)
- 신규 주문 거부(rc!=0, 비취소) → add_reject_order(...)
- 익절/진입 지정가 fill 억제    → add_suppress(side/style/±symbol, seconds)
- 상/하한가 락(시장가 잔존)     → add_suppress(symbol, style="market", ...)
- 연속 미체결(VI 흉내)          → add_suppress(symbol, seconds)
- 거래정지(시세 결측 지속)      → halt(symbol) (ka10095 빈 행 + fill 억제)
- 잔고 반영 지연               → freeze_balance(...) (kt00018 동결 창)
- 토큰 8005 무효화             → 관리 API가 TokenRegistry.force_invalidate()
"""

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FaultPolicy:
    """무결함 기본 정책 — R5의 시나리오 정책이 이를 상속/대체한다.
    매칭/계좌/API 계층은 이 객체의 메서드로만 결함 여부를 조회한다."""

    def propagation_delay_sec(self) -> float:
        """주문 접수 → ka10075 노출까지 지연(§8 기본 재현: 1.5s —
        C1 방어선(전파 유예+연속 2회 확인)이 상시 필요함을 재현)."""
        return 1.5

    def suppress_fill(self, order_no: str) -> bool:
        """True면 해당 주문 체결 억제(§9: 익절 지정가 fill 억제, 상/하한가
        락 — 시장가 미체결 잔존 재현)."""
        return False

    def suppress_fill_order(self, order) -> bool:
        """주문 속성(symbol/side/style) 기반 억제 판정 — 매칭 엔진이 호출
        하는 실제 진입점. 기본은 order_no 훅으로 위임(R3 테스트 하위 호환:
        suppress_fill만 오버라이드한 정책도 계속 동작)."""
        return self.suppress_fill(order.order_no)

    def fill_quantity(self, order_no: str, quantity: int) -> int:
        """체결 수량 조정 훅(§9 부분체결 시나리오). 기본: 전량."""
        return quantity

    def reject_order(self, symbol: str) -> str | None:
        """신규 주문 거부 사유(§9 rc!=0 비취소 거부). None=정상 접수."""
        return None

    def reject_cancel(self, order_no: str) -> str | None:
        """취소 거부 사유(§9 — 이중매매 가드 검증). None=정상 취소."""
        return None

    def api_response_fault(self, api_id: str) -> dict | None:
        """API 계층 결함(§9 간헐 500/타임아웃/429). None=정상.
        반환 dict: {mode: "http500"|"http429"|"delay", delay_sec: float}."""
        return None

    def is_halted(self, symbol: str) -> bool:
        """거래정지(§9) — ka10095 빈 행 처리 대상 여부."""
        return False

    def balance_snapshot(self) -> dict | None:
        """잔고 반영 지연(§9) — 활성이면 kt00018이 렌더링할 동결 스냅샷
        {cash, holdings: {symbol: (market, qty, total_cost)}}. None=실시간."""
        return None


MAX_FAULT_SECONDS = 300.0   # 시간 파라미터 상한(보안 R5 — 단위 착각(초/ms)
#                             이나 오타가 asyncio.sleep 장기 점유·사실상
#                             응답 불능을 만드는 것 차단. reset도 진행 중
#                             sleep은 못 끊는다 — 사전 거부가 유일한 방어)


def _check_seconds(name: str, seconds: float) -> float:
    if seconds < 0:
        raise ValueError(f"{name} must be >= 0")
    if seconds > MAX_FAULT_SECONDS:
        raise ValueError(
            f"{name} must be <= {MAX_FAULT_SECONDS:.0f}s (unit mistake?)")
    return seconds


def _check_count(name: str, count: int | None) -> int | None:
    """횟수 제한은 None(무제한) 또는 양수 — count=0은 "0회 발동" 의도인데
    소비 로직상 1회 발동해 버리는 조용한 무결함/오결함 상태를 만든다
    (§9 fail-loud: '주입했다고 믿는' 검증 금지)."""
    if count is not None and count < 1:
        raise ValueError(f"{name} count must be None or >= 1")
    return count


class ScenarioFaultPolicy(FaultPolicy):
    """관리 API가 조작하는 시나리오 상태 보관 정책(§9).

    시간 창(until)은 주입된 wall_now 기준 — 창 만료 규칙은 판정 시점에
    lazy 평가한다(백그라운드 타이머 없음 — 요청 경로 밖 부작용 금지)."""

    def __init__(self, wall_now: Callable[[], datetime]) -> None:
        self._wall_now = wall_now
        self.clear()

    # ── in-place 리셋(/_replay/reset — 객체 교체 금지 계약) ────────────

    def clear(self) -> None:
        self._propagation_delay: float | None = None
        self._suppress_rules: list[dict] = []
        self._fill_ratio: float | None = None
        self._fill_interval: float = 1.0
        self._last_partial: dict[str, datetime] = {}
        self._reject_order_rules: list[dict] = []
        self._reject_cancel_rules: list[dict] = []
        self._api_faults: dict[str, dict] = {}
        self._halted: set[str] = set()
        self._balance_freeze: dict | None = None

    def describe(self) -> dict:
        """활성 시나리오 요약(GET /_replay/status — §9 관리 API)."""
        now = self._wall_now()
        return {
            "propagation_delay": self._propagation_delay,
            "suppress_rules": [
                {k: (v.isoformat() if isinstance(v, datetime) else v)
                 for k, v in rule.items()}
                for rule in self._suppress_rules
                if rule["until"] is None or now < rule["until"]],
            "fill_ratio": self._fill_ratio,
            "reject_order_rules": self._reject_order_rules,
            "reject_cancel_rules": self._reject_cancel_rules,
            "api_faults": self._api_faults,
            "halted": sorted(self._halted),
            "balance_freeze_active": self.balance_snapshot() is not None,
        }

    # ── 활성화(관리 API 소비) ──────────────────────────────────────────

    def set_propagation_delay(self, seconds: float) -> None:
        self._propagation_delay = _check_seconds("propagation delay",
                                                 seconds)

    def add_suppress(self, *, symbol: str | None = None,
                     side: str | None = None, style: str | None = None,
                     order_no: str | None = None,
                     seconds: float | None = None) -> None:
        """조건 결합 억제 규칙(전부 None이면 전 주문 억제). seconds=None은
        무기한(해제는 reset).

        ⚠️ 사용 규율(트레이더 R5 — 스펙 §9): 폴백 경로(타임아웃→취소→
        시장가) 검증에 유한 창을 쓸 때, 창이 엔진 타임아웃보다 짧으면
        §8 마켓터블 재평가가 창 만료 순간 **자동 체결**시켜 엔진 폴백이
        한 번도 실행되지 않고도 '완료'로 보인다 — 표준은 seconds=None
        (reset으로 해제) 또는 엔진 타임아웃보다 충분히 긴 창."""
        until = (self._wall_now()
                 + timedelta(seconds=_check_seconds("suppress window",
                                                    seconds))
                 if seconds is not None else None)
        self._suppress_rules.append({"symbol": symbol, "side": side,
                                     "style": style, "order_no": order_no,
                                     "until": until})

    def set_fill_ratio(self, ratio: float,
                       interval_sec: float = 1.0) -> None:
        """부분체결 — 잔량의 ratio씩, **벽시계 interval_sec당 최대 1청크**
        (트레이더 R5 I1: 체결 진행이 폴링 횟수에 결합되면 엔진이 신중하게
        더 자주 폴링할수록 결함이 빨리 '해소'되는 역설 — 잔량 취소 분기가
        실행될 기회가 사라진다. 시간 기준 래칫으로 분리)."""
        if not 0 < ratio < 1:
            raise ValueError("fill ratio must be in (0, 1)")
        self._fill_ratio = ratio
        self._fill_interval = _check_seconds("fill interval", interval_sec)

    def add_reject_order(self, *, symbol: str | None = None,
                         message: str = "주문 거부(시나리오)",
                         count: int | None = None) -> None:
        self._reject_order_rules.append(
            {"symbol": symbol, "message": message,
             "remaining": _check_count("reject_order", count)})

    def add_reject_cancel(self, *, order_no: str | None = None,
                          message: str = "취소 거부(시나리오)",
                          count: int | None = None) -> None:
        self._reject_cancel_rules.append(
            {"order_no": order_no, "message": message,
             "remaining": _check_count("reject_cancel", count)})

    def set_api_fault(self, api_id: str, mode: str = "http500",
                      count: int | None = None,
                      delay_sec: float = 0.0) -> None:
        """⚠️ mode="delay" 사용 규율(트레이더 R5): delay_sec이 어댑터 HTTP
        타임아웃보다 짧으면 '느린 정상 응답'일 뿐 타임아웃 경로를 자극하지
        못한다 — R7 타임아웃 검증은 어댑터 타임아웃보다 크게 설정."""
        if mode not in ("http500", "http429", "delay"):
            raise ValueError(f"unknown api fault mode {mode!r}")
        self._api_faults[api_id] = {
            "mode": mode, "remaining": _check_count("api_fault", count),
            "delay_sec": _check_seconds("delay_sec", delay_sec)}

    def halt(self, symbol: str) -> None:
        """거래정지 — ka10095 빈 행 + 해당 심볼 체결 무기한 억제(§9:
        모니터가 '시세 결측 지속'을 관측하게)."""
        self._halted.add(symbol)
        self.add_suppress(symbol=symbol)

    def freeze_balance(self, cash: int, holdings: dict, seconds: float) -> None:
        """kt00018 동결(잔고 반영 지연) — 활성화 시점 스냅샷을 창 동안
        렌더링(창 내 체결이 잔고에 안 보임 → 유령 판정 2회 확인 검증).
        holdings: {symbol: (market, qty, total_cost)} 복사본."""
        self._balance_freeze = {
            "until": self._wall_now() + timedelta(
                seconds=_check_seconds("freeze window", seconds)),
            "cash": cash,
            "holdings": dict(holdings),
        }

    # ── 판정 훅(매칭/계좌/API 계층 소비) ───────────────────────────────

    def propagation_delay_sec(self) -> float:
        if self._propagation_delay is not None:
            return self._propagation_delay
        return super().propagation_delay_sec()

    def suppress_fill_order(self, order) -> bool:
        now = self._wall_now()
        self._suppress_rules = [
            r for r in self._suppress_rules
            if r["until"] is None or now < r["until"]]
        for rule in self._suppress_rules:
            if rule["symbol"] is not None and order.symbol != rule["symbol"]:
                continue
            if rule["side"] is not None and order.side != rule["side"]:
                continue
            if rule["style"] is not None and order.style != rule["style"]:
                continue
            if (rule["order_no"] is not None
                    and order.order_no != rule["order_no"]):
                continue
            # 발동 로그(아키텍트 R5 Minor — §10-3: 로그 없이는 "방어선이
            # 발동한 것"과 "결함이 발동 안 한 것"을 R7 예행 로그로 구분 불가)
            logger.debug("fault hit: suppress_fill %s (%s %s)",
                         order.order_no, order.side, order.style)
            return True
        return False

    def fill_quantity(self, order_no: str, quantity: int) -> int:
        if self._fill_ratio is None:
            return quantity
        now = self._wall_now()
        last = self._last_partial.get(order_no)
        if (last is not None
                and (now - last).total_seconds() < self._fill_interval):
            # 래칫 창 내 재판정 — 진행 없음(폴링 횟수 ↛ 체결 진행)
            return 0
        self._last_partial[order_no] = now
        # 청크 최소 1주(0이면 억제와 구분 불가 — 부분체결은 "일부는 체결")
        return max(1, min(quantity, math.floor(quantity * self._fill_ratio)))

    def reject_order(self, symbol: str) -> str | None:
        message = self._consume_reject(self._reject_order_rules,
                                       "symbol", symbol)
        if message is not None:
            logger.debug("fault hit: reject_order %s", symbol)
        return message

    def reject_cancel(self, order_no: str) -> str | None:
        message = self._consume_reject(self._reject_cancel_rules,
                                       "order_no", order_no)
        if message is not None:
            logger.debug("fault hit: reject_cancel %s", order_no)
        return message

    @staticmethod
    def _consume_reject(rules: list[dict], key: str,
                        value: str) -> str | None:
        """거부 규칙 매칭+횟수 소비 — 소진 규칙은 리스트에서 제거
        (아키텍트 R5 Minor: api_fault의 소진-시-삭제 하우스키핑과 대칭)."""
        for rule in rules:
            if rule[key] is not None and rule[key] != value:
                continue
            if rule["remaining"] is not None:
                rule["remaining"] -= 1
                if rule["remaining"] <= 0:
                    rules.remove(rule)
            return rule["message"]
        return None

    def api_response_fault(self, api_id: str) -> dict | None:
        rule = self._api_faults.get(api_id)
        if rule is None:
            return None
        if rule["remaining"] is not None:
            if rule["remaining"] <= 0:
                del self._api_faults[api_id]
                return None
            rule["remaining"] -= 1
        logger.debug("fault hit: api %s %s", api_id, rule["mode"])
        return {"mode": rule["mode"], "delay_sec": rule["delay_sec"]}

    def is_halted(self, symbol: str) -> bool:
        return symbol in self._halted

    def balance_snapshot(self) -> dict | None:
        if self._balance_freeze is None:
            return None
        if self._wall_now() >= self._balance_freeze["until"]:
            self._balance_freeze = None   # 창 만료 — lazy 해제
            return None
        return self._balance_freeze
