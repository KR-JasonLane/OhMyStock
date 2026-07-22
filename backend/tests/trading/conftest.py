"""trading 테스트 공용 fake — OrderPort 베이스(개발자 P5-T6b #4: entry/monitor
테스트가 동일 fake를 손 중복하던 것을 프로덕션 execution.py 공용화와 대칭으로
통합).

계약: open_orders_script 항목은 정수=미체결 잔량, None=주문 부재 관측,
Exception=조회 실패. **소진 시 AssertionError**(시나리오 길이 오산이 '전량
체결'로 수렴해 허위 통과하는 것 방지 — 폴 루프가 예외를 삼키므로 테스트는
결과 단언에서 깨진다). cancel_script/place_script: 호출별 예외(None=성공,
소진 후=성공). get_quotes는 하위 클래스가 정의(6a=재조회 폴백, 6b=감시 시세)."""

from app.domain.broker import OpenOrder, OrderAck, OrderSide


class FakeOrderPortBase:
    symbol = "005930"
    order_qty = 10

    def __init__(self, open_orders_script=None, cancel_script=None,
                 place_script=None):
        self.placed: list = []
        self.cancelled: list = []
        self.calls: list = []   # ("place"|"cancel"|"open_orders"|"quotes", ...)
        self._script = list(open_orders_script or [])
        self._cancel_script = list(cancel_script or [])
        self._place_script = list(place_script or [])
        self._order_seq = 0

    async def place_order(self, req):
        self.calls.append(("place", req.symbol))
        exc = self._place_script.pop(0) if self._place_script else None
        if exc is not None:
            raise exc
        self._order_seq += 1
        self.placed.append(req)
        return OrderAck(order_no=f"ORD{self._order_seq}", message="ok")

    async def cancel_order(self, order_no, symbol):
        self.calls.append(("cancel", order_no))
        exc = self._cancel_script.pop(0) if self._cancel_script else None
        if exc is not None:
            raise exc
        self.cancelled.append(order_no)
        return OrderAck(order_no=f"CXL{order_no}", message="cancelled")

    async def get_open_orders(self):
        self.calls.append(("open_orders",))
        assert self._script, "open_orders_script exhausted — 시나리오 길이 오산"
        unfilled = self._script.pop(0)
        if unfilled is None:
            return []  # 주문 부재 관측
        if isinstance(unfilled, Exception):
            raise unfilled
        # 마지막 발주 주문번호로 미체결 행 구성
        return [OpenOrder(order_no=f"ORD{self._order_seq}", symbol=self.symbol,
                          side=OrderSide.BUY, order_qty=self.order_qty,
                          unfilled_qty=unfilled, order_price=273_500,
                          status="접수")]

    async def get_quotes(self, symbols):
        raise NotImplementedError  # 하위 클래스 정의
