"""스코어링 파라미터 단일 출처. 스펙 §4-6과 1:1 — 값 변경은 스펙 갱신과 함께.
모든 실행은 이 스냅샷(JSON)을 score_runs.config에 기록한다 (재현성)."""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ScoringConfig:
    # 섹터 강도 합성 가중 (합=1.0)
    sector_weight_r20: float = 0.5
    sector_weight_r60: float = 0.3
    sector_weight_r5: float = 0.2
    # 최종 합성 가중 (합=1.0)
    final_weight_sector: float = 0.4
    final_weight_strategy: float = 0.6
    # 선정 규모
    top_sectors: int = 5
    top_candidates: int = 20
    # 시뮬레이션
    hold_days: int = 10                # 매수일 포함 10거래일째 종가 청산
    min_signal_occurrences: int = 3    # 미만이면 전략 점수 0 (표본 부족)
    # 유니버스/게이트
    min_sector_members: int = 5
    stale_exclusion_limit: float = 0.05
    min_bars: int = 75
    # 지표 창
    ma_short: int = 20
    ma_long: int = 60
    breakout_lookback: int = 60
    breakout_volume_mult: float = 1.5
    pullback_band: float = 0.03
    pullback_lookback: int = 5

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)
