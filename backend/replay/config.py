"""리플레이 서버 설정(스펙 §5/§6) — env 로딩 + 검증.

app.core.config(pydantic Settings)와 독립(§4 임포트 격리) — 리플레이는
표준 라이브러리만으로 충분한 소규모 설정이라 dataclass로 유지한다.
검증은 fail-loud: 앵커 없는 기동·비양수 배속은 즉시 거부(§5 — 조용한
기본값이 "어느 시점을 재생 중인가"라는 감사 질문을 흐리게 하면 안 된다)."""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from replay.clock import KST

_DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "minutes.sqlite"


@dataclass(frozen=True)
class ReplaySettings:
    anchor: datetime               # 재생 기준 시각(KST) — 필수
    speed: float = 1.0             # 배속(§5 — ≠1.0 런은 Task 8 근거 금지)
    data_path: Path = _DEFAULT_DATA
    symbols: tuple[str, ...] = ()  # 빈 튜플=전 심볼 적재(부분 적재 권장 §4)
    preload_days: int = 7          # 앵커 이전 직전가 유지용 선적재 구간
    cash: int = 100_000_000        # 초기 예수금(모의 계좌 기본값 관례)
    default_market: str = "kospi"
    etf_symbols: tuple[str, ...] = field(default_factory=tuple)  # ETF 틱/세율

    def __post_init__(self) -> None:
        if self.anchor.tzinfo is None:
            object.__setattr__(self, "anchor", self.anchor.replace(tzinfo=KST))
        else:
            object.__setattr__(self, "anchor", self.anchor.astimezone(KST))
        if self.speed <= 0:
            raise ValueError(f"REPLAY_SPEED must be positive: {self.speed}")
        if self.cash <= 0:
            raise ValueError(f"REPLAY_CASH must be positive: {self.cash}")
        if self.preload_days < 0:
            raise ValueError("REPLAY_PRELOAD_DAYS must be >= 0")

    @property
    def load_since(self) -> datetime:
        """MinuteStore 부분 적재 하한(§4 — 앵커 이전 preload_days일부터)."""
        return self.anchor - timedelta(days=self.preload_days)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ReplaySettings":
        env = env if env is not None else dict(os.environ)
        raw_anchor = env.get("REPLAY_ANCHOR")
        if not raw_anchor:
            raise ValueError(
                "REPLAY_ANCHOR is required (KST, e.g. 2026-07-10T09:00:00) — "
                "no silent default: the replayed instant must be explicit")
        symbols = tuple(
            s.strip() for s in env.get("REPLAY_SYMBOLS", "").split(",")
            if s.strip())
        etfs = tuple(
            s.strip() for s in env.get("REPLAY_ETF_SYMBOLS", "").split(",")
            if s.strip())
        return cls(
            anchor=datetime.fromisoformat(raw_anchor),
            speed=float(env.get("REPLAY_SPEED", "1.0")),
            data_path=Path(env.get("REPLAY_DATA_PATH", str(_DEFAULT_DATA))),
            symbols=symbols,
            preload_days=int(env.get("REPLAY_PRELOAD_DAYS", "7")),
            cash=int(env.get("REPLAY_CASH", "100000000")),
            default_market=env.get("REPLAY_DEFAULT_MARKET", "kospi"),
            etf_symbols=etfs,
        )
