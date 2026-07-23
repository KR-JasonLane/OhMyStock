"""ScheduleConfig — 데일리 타임라인 파라미터(P6 스펙 §5-2와 1:1).

수치 하드코딩 금지(계획 Global Constraints): 시각·백오프는 전부 여기의
기본값이 단일 출처다. env 노출은 SCHEDULER_ENABLED 하나뿐(YAGNI — 운영
변경 필요가 실측되면 그때 승격). `max_attempts`는 존재하지 않는다 —
포기는 창 종료뿐(스펙 §5, 패널 합류 결정: 트레이딩 잡 포기는 보유 포지션
감시 마비를 뜻하므로 횟수 상한 자체를 두지 않는다)."""

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class ScheduleConfig:
    # 수집(거래일 D): 당일봉 확정 지연 리스크로 19시 이후(P2 운영 노트),
    # 상한 23:55는 자정 넘김 방지(D일 몫 판정 단순화 — 스펙 §4-a)
    collect_at: time = time(19, 0)
    collect_until: time = time(23, 55)
    # 스코어링(D몫): 창 시작은 시각이 아니라 "수집(D) 완료 직후"(선행조건),
    # 상한은 다음 거래일 아침(분석 창 시작 여유 전 — 스펙 §4-b)
    score_until: time = time(8, 50)
    # 분석(거래일 E 아침 — 결정 #38): 진입 창(09:05~09:30) 종료 후의
    # 캐치업은 무의미라 상한 09:20(스펙 §4-c)
    analyze_at: time = time(8, 20)
    analyze_until: time = time(9, 20)
    # 트레이딩 start(거래일 E — 결정 #39): 09:00 기동(진입 창 09:05까지
    # 5분 버퍼 — reconcile 충분, 스펙 §4-f), 상한 15:20(마감 직전 무의미한
    # 재기동 방지 — 루프는 15:30 자기 종료)
    trade_start_at: time = time(9, 0)
    trade_until: time = time(15, 20)
    tick_interval_s: int = 30
    # 잡별 재시도 백오프(스펙 §5-2 — max_attempts 폐기의 쌍): 트레이딩은
    # 60초 — 기동 실패가 길수록 보유 포지션이 방어선 없이 노출되는 시간이
    # 늘어난다(감시 공백 최소화 최우선). 분석 300초 — 스코어링 창 상한
    # (08:50)까지의 완료를 분석 창(60분) 안에서 커버(트레이더 계획 리뷰).
    collect_retry_backoff_s: int = 600
    score_retry_backoff_s: int = 600
    analyze_retry_backoff_s: int = 300
    trade_retry_backoff_s: int = 60

    def __post_init__(self) -> None:
        for name, start, end in (("collect", self.collect_at,
                                  self.collect_until),
                                 ("analyze", self.analyze_at,
                                  self.analyze_until),
                                 ("trade", self.trade_start_at,
                                  self.trade_until)):
            if start >= end:
                raise ValueError(
                    f"{name} window must be ordered: {start} >= {end}")
        for name, value in (("tick_interval_s", self.tick_interval_s),
                            ("collect_retry_backoff_s",
                             self.collect_retry_backoff_s),
                            ("score_retry_backoff_s",
                             self.score_retry_backoff_s),
                            ("analyze_retry_backoff_s",
                             self.analyze_retry_backoff_s),
                            ("trade_retry_backoff_s",
                             self.trade_retry_backoff_s)):
            if value <= 0:
                raise ValueError(f"{name} must be positive: {value}")
