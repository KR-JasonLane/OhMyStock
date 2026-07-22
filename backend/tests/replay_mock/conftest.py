"""replay_mock 공용 하네스 — 분봉 sqlite 빌더 + 시간 제어.

test_matching의 로컬 헬퍼와 달리 endpoint 테스트는 앱 조립까지 필요해
공용으로 승격. 픽스처 시계열은 test_matching과 동일 분봉(09:00~09:03)을
기본으로 쓴다(시나리오 산식 재사용)."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from replay.clock import KST

T0 = datetime(2026, 7, 10, 9, 0, tzinfo=KST)
WALL0 = datetime(2026, 7, 22, 20, 0, tzinfo=KST)

FIXTURES = Path(__file__).parent / "fixtures"


def minute_row(ts: str, o: int, h: int, low: int, c: int,
               v: int = 10) -> dict:
    return {"cntr_tm": ts, "open_pric": f"+{o}", "high_pric": f"+{h}",
            "low_pric": f"+{low}", "cur_prc": f"+{c}", "trde_qty": str(v),
            "acc_trde_qty": str(v), "pred_pre": "0", "pred_pre_sig": "3"}


DEFAULT_ROWS = (
    # 전일(07-09) 마감봉 — ka10095 flu_rt/base_pric·kt00018 pred_close_pric
    # 의 전일 종가 실값 검증용(broker-api R4)
    minute_row("20260709152900", 95_000, 95_100, 94_900, 95_000),
    minute_row("20260710090000", 100_000, 100_500, 99_800, 100_000),
    minute_row("20260710090100", 100_000, 100_200, 98_000, 98_500),
    minute_row("20260710090200", 98_500, 103_000, 98_500, 102_500),
    minute_row("20260710090300", 102_500, 102_600, 102_300, 102_400),
)


def make_minutes_sqlite(path: Path,
                        series: dict[str, tuple[dict, ...]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS minute_raw(symbol TEXT, "
                 "page INTEGER, seq INTEGER, row TEXT, "
                 "PRIMARY KEY(symbol, seq))")
    for symbol, rows in series.items():
        for seq, row in enumerate(rows):
            conn.execute(
                "INSERT OR REPLACE INTO minute_raw VALUES (?, 0, ?, ?)",
                (symbol, seq, json.dumps(row)))
    conn.commit()
    conn.close()
    return path


class TimeCtl:
    """재생 시계(monotonic)와 벽시계를 독립 제어하는 테스트 하네스.
    monotonic 전진 → replay_now 전진(×speed), wall 전진 → 전파 지연 판정."""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = WALL0

    def monotonic(self) -> float:
        return self.mono

    def wall_now(self) -> datetime:
        return self.wall


@pytest.fixture
def fixture_json():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return _load
