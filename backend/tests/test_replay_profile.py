"""리플레이 프로필 배선(R6 — 스펙 §4-1/§4-2).

- override 조합 validator(보안 Critical #1 — 사전 차단 4항)
- 어댑터 override 적용 + 실효 URL WARNING
- 격리 강제(보안 R3): 프로덕션 앱에 /_replay 라우트 부재 + app→replay
  역방향 임포트 부재(AST — replay↛app 검사의 대칭)
- trade_runs.run_environment 감사 컬럼(§4-1) 영속
"""

import ast
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select

from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.main import create_app
from app.store.models import Base, TradeRunRow
from app.store.trading_store import TradingStore

ENV = {
    "kiwoom_app_key": "test-key",
    "kiwoom_secret_key": "test-secret",
    "kiwoom_mock": True,
    "database_url": "sqlite+pysqlite:///:memory:",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**ENV, **overrides})


# ── validator 조합(§4-1 — 사전 차단) ──────────────────────────────────


def test_override_루프백과_replay_서비스명은_허용():
    for url in ("http://127.0.0.1:9095", "http://localhost:9095",
                "http://replay:9095", "https://127.0.0.1:9095"):
        s = _settings(kiwoom_base_url_override=url)
        assert s.run_environment == "replay"


def test_override_임의_호스트는_기동_거부():
    """오타/오설정으로 앱키·시크릿이 외부 호스트로 전송되는 유출 경로 차단."""
    for url in ("https://evil.example.com", "http://192.168.0.10:9095",
                "http://mockapi.kiwoom.com", "ftp://127.0.0.1:9095"):
        with pytest.raises(ValidationError):
            _settings(kiwoom_base_url_override=url)


def test_실전_모드와_override_조합은_기동_거부():
    """리플레이는 mock 전제 — 실전 엔진이 가짜 체결을 실체결로 오인하는
    역방향 리스크 봉쇄(§4-1 #2)."""
    with pytest.raises(ValidationError):
        _settings(kiwoom_mock=False,
                  api_write_token="w", api_trade_token="t",
                  kiwoom_base_url_override="http://127.0.0.1:9095")


def test_override_없으면_run_environment는_mode를_따른다():
    assert _settings().run_environment == "mock"
    assert _settings(kiwoom_mock=False, api_write_token="w",
                     api_trade_token="t").run_environment == "real"


# ── 어댑터 override 적용(§4-1 — 실효 URL 이중 방어) ───────────────────


def _client_logger():
    import logging
    # 스위트 내 alembic 테스트의 fileConfig(disable_existing_loggers)가
    # 기존 로거를 비활성화해 caplog가 비는 순서 의존을 방어(재활성화)
    lg = logging.getLogger("app.adapters.kiwoom.client")
    lg.disabled = False
    return lg


def test_클라이언트는_override를_적용하고_WARNING을_남긴다(caplog):
    import logging
    _client_logger()
    settings = _settings(kiwoom_base_url_override="http://127.0.0.1:9095")
    with caplog.at_level(logging.WARNING, logger="app.adapters.kiwoom.client"):
        client = KiwoomHttpClient(settings)
    assert str(client._http.base_url).startswith("http://127.0.0.1:9095")
    assert "OVERRIDDEN" in caplog.text


def test_클라이언트는_override_없으면_mock_URL(caplog):
    import logging
    _client_logger()
    with caplog.at_level(logging.WARNING, logger="app.adapters.kiwoom.client"):
        client = KiwoomHttpClient(_settings())
    assert str(client._http.base_url).startswith(
        "https://mockapi.kiwoom.com")
    assert "OVERRIDDEN" not in caplog.text


# ── 격리 강제(보안 R3 — 컨벤션이 아니라 테스트가 보증) ─────────────────


def test_프로덕션_앱_라우트에_replay_프리픽스가_없다():
    """§4-2 — 프로덕션 FastAPI 앱에 /_replay(관리 API)나 키움 재현 표면이
    절대 등록되지 않는다(별도 프로세스 uvicorn replay.main 전용)."""
    app = create_app(_settings())
    paths = [getattr(route, "path", "") for route in app.routes]
    assert not any(p.startswith("/_replay") for p in paths)
    assert not any(p.startswith("/api/dostk") for p in paths)
    assert not any(p.startswith("/oauth2") for p in paths)


def test_app은_replay를_임포트하지_않는다():
    """역방향 격리(replay↛app 의 대칭) — 프로덕션 코드가 목 구현에 조용히
    의존하는 것 금지(AST — tests/replay_mock/test_replay_base.py와 동일
    방식)."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "replay" or \
                            alias.name.startswith("replay."):
                        offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 0 and (module == "replay"
                                        or module.startswith("replay.")):
                    offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Call):
                # importlib.import_module("replay...") / __import__ —
                # 원본(test_replay_base)과 동일 강도(개발자 R6 #2: "대칭"
                # 주장이 실제 검사 강도와 어긋나면 회귀가 한쪽만 잡힌다)
                func = node.func
                name = getattr(func, "attr", getattr(func, "id", ""))
                if name in ("import_module", "__import__") and node.args:
                    arg = node.args[0]
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and (arg.value == "replay"
                                 or arg.value.startswith("replay."))):
                        offenders.append(
                            f"{path.name}:{node.lineno}(dynamic)")
    assert offenders == []


# ── 기동 프로브(§4-1 확장 — 트레이더/아키텍트 R6) ──────────────────────


@pytest.mark.anyio
async def test_프로브는_speed_1이_아니면_기동_거부(monkeypatch):
    """§5 ③ — 목이 REPLAY_SPEED≠1.0으로 돌면 앱 오프셋 시계(1배속)와
    조용히 어긋나 예행 전체가 무효가 된다. 기동 자체를 거부."""
    import respx

    from app.main import _verify_replay_server
    with respx.mock:
        respx.get("http://127.0.0.1:9095/_replay/status").respond(
            json={"replay_now": "2026-07-10T09:00:00+09:00", "speed": 10.0})
        with pytest.raises(RuntimeError, match="speed"):
            await _verify_replay_server("http://127.0.0.1:9095")


@pytest.mark.anyio
async def test_프로브는_미도달이면_기동_거부():
    """잊힌 override(.env 잔존)가 조용한 연결 실패 대신 fail-loud로."""
    import respx

    from app.main import _verify_replay_server
    with respx.mock:  # 라우트 미등록 — 연결 불가 재현
        with pytest.raises(RuntimeError, match="도달할 수 없습니다"):
            await _verify_replay_server("http://127.0.0.1:9095")


@pytest.mark.anyio
async def test_프로브는_서버_재생_시각을_앵커로_반환(monkeypatch):
    """앵커는 서버가 SSOT — env 이중화 드리프트·기동 시차 드리프트 제거."""
    import respx

    from app.main import _verify_replay_server
    with respx.mock:
        respx.get("http://127.0.0.1:9095/_replay/status").respond(
            json={"replay_now": "2026-07-10T09:01:30+09:00", "speed": 1.0})
        anchor = await _verify_replay_server("http://127.0.0.1:9095")
    assert anchor.isoformat() == "2026-07-10T09:01:30+09:00"


# ── 기동 게이트 통합(lifespan 실경로 — 개발자 R6 델타: 배선 실수까지
#    잡으려면 컴포넌트 단위가 아니라 create_app 조립 경로로 고정) ────────


def _replay_settings(tmp_path, **overrides) -> Settings:
    return _settings(
        kiwoom_base_url_override="http://127.0.0.1:9095",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'gate.db'}",
        **overrides)


def test_기동_게이트_서버_미도달이면_lifespan이_실패한다(tmp_path):
    import respx
    from fastapi.testclient import TestClient
    app = create_app(_replay_settings(tmp_path))
    with respx.mock:                      # 라우트 미등록 — 연결 불가
        with pytest.raises(RuntimeError, match="도달할 수 없습니다"):
            with TestClient(app):
                pass


def test_기동_게이트_타_환경_미종결_포지션이_있으면_기동_거부(tmp_path):
    """트레이더 R6 Critical의 lifespan 통합판 — 프로브는 통과하지만 같은
    DB에 mock 런의 미종결 포지션이 있으면 기동 거부."""
    import respx
    from fastapi.testclient import TestClient

    from app.domain.trading.models import PositionState, TradePosition
    settings = _replay_settings(tmp_path)
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)
    store = TradingStore(engine)
    run_id = store.create_run("{}", "mock")
    store.create_position(run_id, TradePosition(
        symbol="005930", name="삼성전자", market="kospi",
        state=PositionState.ENTERED, entry_price=100_000, quantity=1,
        peak_price=100_000, trailing_active=False))
    app = create_app(settings)
    with respx.mock:
        respx.get("http://127.0.0.1:9095/_replay/status").respond(
            json={"replay_now": "2026-07-10T09:00:00+09:00", "speed": 1.0})
        with pytest.raises(RuntimeError, match="미종결 포지션"):
            with TestClient(app):
                pass


def test_기동_게이트_정상_조건이면_기동_성공(tmp_path):
    import respx
    from fastapi.testclient import TestClient
    settings = _replay_settings(tmp_path)
    engine = create_engine(settings.database_url.get_secret_value())
    Base.metadata.create_all(engine)      # 깨끗한 DB(타 환경 포지션 없음)
    app = create_app(settings)
    with respx.mock:
        respx.get("http://127.0.0.1:9095/_replay/status").respond(
            json={"replay_now": "2026-07-10T09:00:00+09:00", "speed": 1.0})
        with TestClient(app):
            assert app.state.settings.run_environment == "replay"


# ── 교차 오염 방어(트레이더 R6 Critical) ───────────────────────────────


def test_open_positions는_run_environment로_필터한다(tmp_path):
    """리플레이 프로세스가 같은 DB의 실전/모의 미종결 포지션을 reconcile
    대상으로 삼아 CLOSED 오판(감시 이탈)하는 경로 차단 — 감사 컬럼의
    실소비 지점."""
    from dataclasses import replace

    from app.domain.trading.models import PositionState, TradePosition
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'x.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine)
    mock_run = store.create_run("{}", "mock")
    replay_run = store.create_run("{}", "replay")
    pos = TradePosition(symbol="005930", name="삼성전자", market="kospi",
                        state=PositionState.ENTERED, entry_price=100_000,
                        quantity=1, peak_price=100_000,
                        trailing_active=False)
    store.create_position(mock_run, pos)
    store.create_position(replay_run, replace(pos, symbol="000660"))
    rows, _ = store.open_positions("replay")
    assert [p.symbol for _, p in rows] == ["000660"]
    rows, _ = store.open_positions("mock")
    assert [p.symbol for _, p in rows] == ["005930"]
    rows, _ = store.open_positions()          # 무필터(하위 호환)
    assert len(rows) == 2
    assert store.foreign_open_position_count("replay") == 1
    assert store.foreign_open_position_count("mock") == 1


# ── 오프셋 시계(§4-1 — speed=1.0 전제 유닛) ────────────────────────────


def test_오프셋_시계는_anchor에_실경과를_더한다():
    """app.core.replay_clock — anchor(+KST 간주) + monotonic 실경과.
    speed 항은 의도적으로 없음(모듈 독스트링 전제 — speed≠1.0 런은 검증
    근거 사용 금지)."""
    from zoneinfo import ZoneInfo

    from app.core.replay_clock import make_replay_clock
    kst = ZoneInfo("Asia/Seoul")
    fake = {"mono": 100.0}
    clock = make_replay_clock(datetime(2026, 7, 10, 9, 0),
                              monotonic=lambda: fake["mono"])
    assert clock() == datetime(2026, 7, 10, 9, 0, tzinfo=kst)
    fake["mono"] = 190.0
    assert clock() == datetime(2026, 7, 10, 9, 1, 30, tzinfo=kst)


# ── run_environment 감사 컬럼(§4-1 — Alembic 0009) ─────────────────────


def test_create_run은_run_environment를_영속한다(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    store = TradingStore(engine)
    run_id = store.create_run("{}", "replay")
    default_id = store.create_run("{}")
    with engine.connect() as conn:
        rows = {row.id: row.run_environment for row in conn.execute(
            select(TradeRunRow.id, TradeRunRow.run_environment))}
    assert rows[run_id] == "replay"
    assert rows[default_id] == "mock"   # 기본값(기존 호출 경로 하위 호환)
