"""유니버스 필터 규칙(순수) — P3 스펙 §4-2 소유의 단일 출처.

scoring/service.py(스코어링 유니버스)와 trading/selection.py(진입 재확인,
P5 §6-3.1)가 함께 소비한다. 오케스트레이션 모듈(service.py — ScoringService/
asyncio 상태기계)에 두면 순수 판정 계층(trading)이 오케스트레이션 계층에
결합되므로 중립 모듈로 분리했다(P5-T3 개발자 패널)."""


def passes_universe(audit_info: str, state: str) -> bool:
    """스펙 §4-2: auditInfo가 "정상"이고 state에 거래정지/관리종목 플래그가 없다."""
    return (audit_info == "정상"
            and "거래정지" not in state
            and "관리종목" not in state)
