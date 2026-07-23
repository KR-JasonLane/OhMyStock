"""커버리지 게이트 _stats 회귀(트레이더 R7-패치 Minor — 크로스 정의가
엔진 의미론(진입가 대비, 진입 봉부터)임을 안전망으로 고정.

R7 발견①의 재발 방지: 게이트 정의가 다시 바뀌면(진행 고점 DD 등) 여기서
깨진다. 스크립트는 패키지가 아니라 importlib로 적재."""

import importlib.util
from datetime import datetime
from pathlib import Path

from replay.clock import KST
from replay.minute_store import MinuteCandle

_SPEC = importlib.util.spec_from_file_location(
    "replay_coverage_gate",
    Path(__file__).resolve().parents[2] / "scripts" / "replay_coverage_gate.py")
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _candle(hh: int, mm: int, close: int) -> MinuteCandle:
    ts = datetime(2026, 6, 2, hh, mm, tzinfo=KST)
    return MinuteCandle(symbol="TEST", ts=ts, open=close, high=close,
                        low=close, close=close, volume=1)


def test_크로스는_진입가_대비_진입봉부터_판정된다():
    """09:00 봉(90,000 — 진입 전 갭)은 크로스 후보가 아니어야 한다:
    엔진은 09:05 진입가(100,000) 기준으로만 판정한다. -5%/-+8%/+10%
    경계는 exit_rules 공유 헬퍼(bp 교차곱셈, 경계 포함)와 동일."""
    series = [
        _candle(9, 0, 90_000),    # 진입 전 — 가짜 손절 크로스 후보(제외돼야)
        _candle(9, 5, 100_000),   # 가정 진입가
        _candle(9, 10, 95_000),   # 정확히 -5.00% — 경계 포함 크로스
        _candle(9, 20, 108_000),  # +8% 크로스(트레일링 안착 자극)
        _candle(9, 30, 110_000),  # +10% 크로스(익절 자극)
    ]
    max_dd, max_ru, first_dd, first_ru, first_ru8, gaps = gate._stats(series)
    assert first_dd == "06-02 09:10"    # 09:00 갭이 아니라 진입 후 크로스
    assert first_ru8 == "06-02 09:20"
    assert first_ru == "06-02 09:30"


def test_손절_크로스가_없으면_대시():
    """R7 run 1 재현(035760 클래스) — 진입가 대비 -5% 미도달이면 크로스
    없음(과거 진행 고점 DD 정의였다면 반등 후 눌림에서 오판정 여지)."""
    series = [
        _candle(9, 5, 100_000),
        _candle(9, 30, 96_000),   # -4.0% — 미달
        _candle(10, 0, 98_000),
    ]
    _, _, first_dd, first_ru, first_ru8, _ = gate._stats(series)
    assert first_dd == "-" and first_ru == "-" and first_ru8 == "-"


def test_0905_이전만_있으면_첫봉_폴백_경고(capsys):
    series = [_candle(9, 0, 100_000), _candle(9, 2, 99_000)]
    price, idx = gate._assumed_entry(series)
    assert price == 100_000 and idx == 0
    assert "폴백" in capsys.readouterr().out
