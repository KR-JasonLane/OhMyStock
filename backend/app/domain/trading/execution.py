"""주문 집행 공용 헬퍼 — entry(6a)와 monitor(6b)가 공유하는
"persist(fail-closed) → 발주 → 감사(격리)" 트리오와 C1-안전 체결 폴링.

공용화 근거(아키텍트 P5-T6a #2): 같은 트리오를 두 모듈이 손으로 중복 구현하면
감사 누락 같은 결함이 한쪽에만 생긴다 — 6a 구현 중 실제로 발생했던 버그 클래스
(시장가 잔여 취소만 감사 누락).

콜백 계약(6a에서 확정, 스펙 §6-3.8):
- persist(fail-closed): 항상 발주 **이전** 호출 — 실패 시 주문이 아예 나가지
  않는 안전한 방향. 예외를 그대로 전파한다.
- on_order(격리): 발주 **이후** 호출 — 기록 실패가 폴링·포지션 추적을 죽이면
  "주문은 나갔는데 추적 끊김" 최악 상태가 되므로 예외를 삼키고 order_no 포함
  error 로그만 남긴다(수동 감사 복구 가능)."""

import logging
from collections.abc import Awaitable, Callable

from app.domain.broker import OrderAck, OrderPort, OrderRequest

logger = logging.getLogger(__name__)

# 주문 감사 콜백 — (ack, request, status). Task 7이 store.record_order 연결.
OnOrder = Callable[[OrderAck, OrderRequest, str], None]
Sleep = Callable[[float], Awaitable[None]]


def audit_order(on_order: OnOrder | None, ack: OrderAck, req: OrderRequest,
                status: str) -> None:
    """on_order 예외 격리(보안 패널 P5-T6a #3) — 주문은 이미 나갔으므로 기록
    실패가 흐름을 죽이면 안 된다. 실패는 error 로그로 표면화."""
    if on_order is None:
        return
    try:
        on_order(ack, req, status)
    except Exception as exc:  # noqa: BLE001
        logger.error("order audit callback failed for %s (order_no=%s, "
                     "status=%s): %s — manual audit reconstruction needed",
                     req.symbol, ack.order_no, status, exc)


async def submit_order(orders: OrderPort, req: OrderRequest, *,
                       persist: Callable[[], None],
                       on_order: OnOrder | None) -> OrderAck:
    """영속(fail-closed, 발주 전) → 발주 → 감사(격리) — 3단 쌍을 한 곳에.
    취소는 이 헬퍼로 묶지 않는다: 경로별 실패 의미가 다르다(지정가 취소
    실패=폴백 중단·이중매매 가드 / 잔여 취소 실패=사유 보존 — 6a 계약)."""
    persist()
    ack = await orders.place_order(req)
    audit_order(on_order, ack, req, "submitted")
    return ack


async def poll_unfilled(orders: OrderPort, order_no: str, *,
                        timeout_sec: float, interval_sec: float,
                        sleep: Sleep) -> int:
    """타임아웃까지 미체결 잔량 폴링. 반환: 0=전량 체결 확정, 양수=미체결
    잔량(마지막 관측), -1=관측 전무(체결 여부 미확정 — 호출자가 보수 처리).

    주문 부재(get_open_orders에 없음)는 '체결'과 '미체결 시스템 미전파'를
    구분하지 못한다(P5-T6a 트레이더 C1 — 접수 TR(ordr)과 조회 TR(acnt)은 다른
    백엔드 계층, 전파 지연 가능). 오판 방어 3중:
    [a] 발주 직후 첫 폴 전 1 interval 전파 유예
    [b] 존재를 한 번도 관측 못 한 주문의 부재는 **성공 조회 연속 2회**에서
        확인된 뒤에만 체결로 판정. 한 번이라도 관측된 주문의 부재는 즉시
        체결(우리가 취소하지 않았으므로 등록된 주문의 소멸 원인은 체결뿐)
    [c] 잔여 리스크(전파 지연 > 유예+확인 창)는 Task 7이 체결 직후 잔고
        대사(kt00018)로 수량·평단을 확정하며 봉쇄(유령 포지션 즉시 해소)
    조회 실패(BrokerError)는 부재 관측이 아니다 — 연속 부재 카운트를
    리셋하고 재시도. 데드라인은 근사치 — 마지막 반복이 최대 interval만큼
    초과할 수 있다(보수 방향)."""
    deadline = timeout_sec
    interval = min(interval_sec, timeout_sec)
    last_unfilled: int | None = None
    seen = False          # 이 주문이 조회 시스템에 등록된 것을 관측했는가
    absent_streak = 0     # 성공 조회 기준 연속 부재 횟수
    elapsed = 0.0
    await sleep(interval)  # [a] 전파 유예
    elapsed += interval
    while True:
        try:
            open_orders = await orders.get_open_orders()
            mine = [o for o in open_orders if o.order_no == order_no]
            if mine:
                seen = True
                absent_streak = 0
                last_unfilled = mine[0].unfilled_qty
                if last_unfilled == 0:
                    return 0
            else:
                absent_streak += 1
                if seen or absent_streak >= 2:  # [b]
                    return 0
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 재시도
            absent_streak = 0  # 실패는 부재 '관측'이 아니다
            logger.warning("open-order poll failed for order_no=%s (%s) — "
                           "retrying", order_no, exc)
        if elapsed >= deadline:
            # 타임아웃 — 마지막 관측 잔량(관측 전무면 -1: 호출자가 보수 처리)
            return last_unfilled if last_unfilled is not None else -1
        await sleep(interval)
        elapsed += interval
