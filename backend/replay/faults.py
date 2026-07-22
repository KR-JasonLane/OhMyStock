"""결함 주입 seam(스펙 §9) — R3에서는 **주입 지점(FaultPolicy 계약)과
무결함 기본 구현만** 정의한다. 시나리오 상태·관리 API(/_replay/faults)는
R5가 이 계약 위에 구현한다(전역 뮤터블 플래그 금지 — 개발자 스펙 리뷰 I2:
매칭 로직에 테스트용 if가 산재하는 patchwork 방지, 정책 객체 주입만).

시간 파라미터는 전부 **벽시계 초**(§5 — 배속 무관: 실서버 지연을 흉내내는
값이므로 배속으로 스케일하면 C1 방어선 타이밍이 왜곡된다)."""


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

    def fill_quantity(self, order_no: str, quantity: int) -> int:
        """체결 수량 조정 훅(§9 부분체결 시나리오). 기본: 전량."""
        return quantity

    def reject_order(self, symbol: str) -> str | None:
        """신규 주문 거부 사유(§9 rc!=0 비취소 거부). None=정상 접수."""
        return None

    def reject_cancel(self, order_no: str) -> str | None:
        """취소 거부 사유(§9 — 이중매매 가드 검증). None=정상 취소."""
        return None
