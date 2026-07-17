# Phase 2 데이터 수집 파이프라인 구현 계획서

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **날짜:** 2026-07-17
- **근거 spec:** `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md`

**목표:** 전 종목 명부·업종 매핑·일봉을 PostgreSQL에 적재하는 수집 파이프라인
(`POST /collect` 시동)을 TDD로 구축하고, 풀 수집 1회를 실측 완주한다.

**아키텍처:** `store/`(스키마+upsert 리포지토리) ← `domain/collection.py`(오케스트레이션,
BrokerPort·Store만 안다) ← `api/collect.py`(전송). 키움 신규 TR 3종은 **Task 1
스파이크로 실측 확정 후** `adapters/kiwoom/`에 매핑. 에러 계층은 domain으로 이동
(포트 계약의 일부).

**기술 스택:** 기존 스택 그대로 (Python 3.12/uv/FastAPI/SQLAlchemy 2/Alembic/httpx/
respx/pytest). 신규 의존성 없음.

## 전역 제약 (모든 태스크에 암묵 적용)

- Python 3.12, uv, 백엔드 명령은 `backend/`에서.
- **`domain/`은 `adapters/`·`store/` 구현을 import하지 않는다** (Task 2에서 에러
  계층을 domain으로 옮겨 이를 성립시킨다). 벤더 필드는 어댑터 밖 금지.
- Phase 1 어댑터 패턴 준수: `_to_*` 헬퍼, `try/except (KeyError, ValueError,
  ArithmeticError, TypeError, AttributeError) → BrokerError`, `call_paged`+`aclosing`,
  시크릿 비노출, 라이브 실측 정정 절차.
- 금액·가격 정수(원), 날짜는 `datetime.date`.
- 라이브 스모크는 `-m live`로만, 모의서버 한정. 캡처는 bash 슬래시 경로.
- 태스크 완료 = 4-에이전트 패널(`senior-developer`/`senior-trader`/
  `architecture-expert`/`security-expert` — 이제 subagent_type으로 직접 디스패치 가능)
  전원 통과. Critical/Important는 수정 후 재리뷰.
- 커밋 메시지는 각 태스크 명시 그대로, 트레일러/AI 흔적 금지.
- 회귀 유지: 시작 시점 단위 50 passed / 라이브 6 passed.

## 선행조건

- `.env`에 모의투자용 키 (완료), Docker Desktop 실행 중 (Task 7 풀 수집).
- **Task 1 스파이크 결과가 Task 4의 필드명을 확정한다** — Task 4 디스패치 시
  코디네이터가 실측 필드명을 델타로 전달. ka20002가 구성종목을 반환하지 않으면
  Task 4의 `list_sector_members`를 대안 B(KRX 파일 파서)로 교체하는 계획 수정을
  코디네이터가 수행한다.

## 파일 구조 (최종 산출물)

```
backend/
├─ alembic/versions/0002_market_data.py        # Task 3
├─ app/
│  ├─ domain/
│  │  ├─ errors.py                             # Task 2 (adapters/kiwoom/errors.py 이동)
│  │  ├─ broker.py                             # Task 4 수정 (Instrument/Sector + 포트 3메서드)
│  │  └─ collection.py                         # Task 5 — CollectionService
│  ├─ adapters/kiwoom/
│  │  ├─ auth.py                               # Task 2 수정 (expires_dt 가드, 429 재시도)
│  │  ├─ client.py                             # Task 2 수정 (aclose try/finally)
│  │  └─ broker.py                             # Task 4 수정 (TR 3종 매핑)
│  ├─ store/
│  │  ├─ models.py                             # Task 3 — SQLAlchemy 모델 4종
│  │  └─ collection_store.py                   # Task 3 — upsert 리포지토리
│  ├─ api/collect.py                           # Task 6
│  └─ main.py                                  # Task 6 수정 (서비스 조립 + 라우터)
└─ tests/
   ├─ conftest.py                              # Task 2 — 공용 픽스처 (kiwoom/conftest.py 대체)
   ├─ store/{__init__.py, test_models_migration.py, test_collection_store.py}   # Task 3
   ├─ kiwoom/test_broker_catalog.py            # Task 4
   ├─ test_collection_service.py               # Task 5
   ├─ test_api_collect.py                      # Task 6
   └─ live/test_live_smoke.py                  # Task 4 수정 (TR 3종 케이스 추가)
```

---

### Task 1: 스파이크 — 신규 TR 3종 실측 (커밋 없음)

**Files:** 없음 (스크래치 실행 + 원장 기록만. 산출물은 실측 데이터)

**Interfaces:**
- Produces: `.superpowers/sdd/phase2-spike-tr.txt` (실측 원본) + 원장(progress.md)에
  요약 — ka10099/ka10101/ka20002의 **실제 응답 필드명·페이지 크기·모의서버 지원
  여부**, 그리고 **분기 판정**(A: 키움 TR 경로 확정 / B: ka20002 불발 → KRX 파일
  대안). Task 3(instrument_type 매핑)과 Task 4(전체 필드명)가 이 결과에 의존한다.

- [ ] **Step 1: 프로브 실행**

`backend/`에서 (PRE-GATE 프로브와 동일 방식, heredoc):

```python
import asyncio, json
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.errors import BrokerError
from app.core.config import Settings

async def probe(c, label, category, api_id, body):
    try:
        data, cont, nk = await c.call(category, api_id, body)
        keys = {k: (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__)
                for k, v in data.items()}
        print(f"\n[{label}] {api_id} OK cont={cont} top_keys={json.dumps(keys, ensure_ascii=False)}")
        for k, v in data.items():
            if isinstance(v, list) and v:
                print(f"  first_row_of[{k}]={json.dumps(v[0], ensure_ascii=False)}")
                print(f"  row_count={len(v)}")
    except BrokerError as e:
        print(f"\n[{label}] {api_id} ERROR {type(e).__name__}: {e}")

async def main():
    s = Settings()
    assert s.kiwoom_mock
    c = KiwoomHttpClient(s)
    try:
        await probe(c, "종목리스트-코스피", "stkinfo", "ka10099", {"mrkt_tp": "0"})
        await probe(c, "종목리스트-코스닥", "stkinfo", "ka10099", {"mrkt_tp": "10"})
        await probe(c, "종목리스트-ETF", "stkinfo", "ka10099", {"mrkt_tp": "8"})
        await probe(c, "업종코드-코스피", "stkinfo", "ka10101", {"mrkt_tp": "0"})
        await probe(c, "업종코드-코스닥", "stkinfo", "ka10101", {"mrkt_tp": "1"})
        # 업종코드 하나를 위 결과에서 골라 아래를 재실행 (예: 001)
        await probe(c, "업종별구성종목", "sect", "ka20002", {"inds_cd": "001"})
    finally:
        await c.aclose()

asyncio.run(main())
```

출력을 `.superpowers/sdd/phase2-spike-tr.txt`로 캡처. **요청 필드명이 거부되면**
(`return_code!=0` 메시지의 필수파라미터 안내 활용) 바디 필드명을 조정해 재시도
(예: `mrkt_tp` → `mrkt_tp` 외 변형, `inds_cd` → `upjong_cd` 등) — 시도한 조합과
결과를 전부 기록.

- [ ] **Step 2: 판정 + 원장 기록**

기록할 것: ① TR별 실제 요청/응답 필드명 ② 페이지 크기(cont-yn 발생 여부)
③ ka20002가 구성종목 리스트를 주는가 → **분기 A/B 판정** ④ instrument_type으로
쓸 구분 필드 존재 여부. 원장(progress.md)에 요약 추가. 분기 B면 코디네이터가
Task 4를 수정한 뒤 진행.

---

### Task 2: 하드닝 스위프 — Phase 1 이월 + 에러 계층 domain 이동

**Files:**
- Create: `backend/app/domain/errors.py` (adapters/kiwoom/errors.py 내용 이동)
- Delete: `backend/app/adapters/kiwoom/errors.py`
- Modify: `backend/app/adapters/kiwoom/auth.py`, `client.py`, `broker.py`
  (import 경로 변경 + 아래 수정)
- Create: `backend/tests/conftest.py` / Delete: `backend/tests/kiwoom/conftest.py`
- Modify: 기존 테스트의 `app.adapters.kiwoom.errors` import → `app.domain.errors`
- Test: `backend/tests/kiwoom/test_auth.py` (신규 3케이스 추가)

**Interfaces:**
- Produces: `app.domain.errors` — `BrokerError`, `AuthError`, `RateLimitError`,
  `ApiError` (클래스 정의 동일, 위치만 이동 — **포트 계약의 일부이므로 domain 소유**.
  Task 5의 CollectionService가 계층 위반 없이 import 가능해진다).
  `TokenManager.__init__`에 `sleep: Callable[[float], Awaitable[None]] | None = None`
  추가. 루트 `tests/conftest.py`: `anyio_backend` fixture + `make_settings(**overrides)`
  헬퍼 함수.

- [ ] **Step 1: 실패하는 테스트 작성** — `backend/tests/kiwoom/test_auth.py`에 추가:

```python
@pytest.mark.anyio
@respx.mock
async def test_expires_dt가_비정상이면_AuthError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(
        json={"token": "TOK", "token_type": "bearer", "expires_dt": "not-a-date",
              "return_code": 0, "return_msg": "ok"})
    tm, http = _manager(now)
    with pytest.raises(AuthError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_토큰발급_429는_1회_재시도_후_성공한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json=_token_response("TOK1", "20260717235959")),
    ]
    sleeps: list[float] = []

    async def record_sleep(s: float) -> None:
        sleeps.append(s)

    http = httpx.AsyncClient(base_url=BASE)
    tm = TokenManager(http, app_key="AK", secret_key="SK", now=lambda: now,
                      sleep=record_sleep)
    assert await tm.get_token() == "TOK1"
    assert sleeps == [1.0]
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_토큰발급_429가_반복되면_RateLimitError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(429)

    async def noop(_: float) -> None: ...

    http = httpx.AsyncClient(base_url=BASE)
    tm = TokenManager(http, app_key="AK", secret_key="SK", now=lambda: now, sleep=noop)
    with pytest.raises(RateLimitError):
        await tm.get_token()
    await http.aclose()
```

(파일 상단 import에 `httpx`, `RateLimitError` 추가 — import는
`from app.domain.errors import AuthError, RateLimitError`로.)

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/kiwoom/test_auth.py -v`
Expected: FAIL (`ModuleNotFoundError: app.domain.errors` — 이동 전이므로).

- [ ] **Step 3: 구현**

1. `git mv backend/app/adapters/kiwoom/errors.py backend/app/domain/errors.py` —
   docstring을 "브로커 포트 계약의 에러 계층. 포트 소비자는 이 타입들만 안다."로
   갱신. 레포 전체에서 `app.adapters.kiwoom.errors` import를
   `app.domain.errors`로 일괄 변경 (auth.py, client.py, broker.py, 테스트들).
2. `auth.py` — 생성자에 `sleep` 주입 추가:

```python
        sleep: Callable[[float], Awaitable[None]] | None = None,
```
```python
        self._sleep = sleep or asyncio.sleep
```

   `_issue()`를 다음으로 교체 (429 1회 재시도 + expires_dt 가드; 기존 예외 래핑 유지):

```python
    async def _issue(self) -> None:
        for attempt in (0, 1):
            try:
                resp = await self._http.post(
                    "/oauth2/token",
                    json={"grant_type": "client_credentials",
                          "appkey": self._app_key, "secretkey": self._secret_key},
                )
            except httpx.HTTPError as exc:
                raise AuthError(
                    f"token issue failed: network {type(exc).__name__}") from exc
            if resp.status_code == 429:
                if attempt == 0:
                    logger.warning("kiwoom 429 on token issue — retrying in 1s")
                    await self._sleep(1.0)
                    continue
                raise RateLimitError("token issue rate limited")
            break
        try:
            data = resp.json()
        except ValueError as exc:
            raise AuthError(
                f"token issue failed: non-json response http={resp.status_code}") from exc
        if resp.status_code != 200 or data.get("return_code") != 0 or not data.get("token"):
            raise AuthError(
                f"token issue failed: http={resp.status_code} "
                f"code={data.get('return_code')} msg={data.get('return_msg')}"
            )
        try:
            expires_at = datetime.strptime(
                data["expires_dt"], _EXPIRES_FMT).replace(tzinfo=KST)
        except (KeyError, ValueError) as exc:
            raise AuthError(
                f"token issue failed: bad expires_dt ({type(exc).__name__})") from exc
        self._token = data["token"]
        self._expires_at = expires_at
        logger.info("kiwoom token issued, expires_at=%s", self._expires_at.isoformat())
```

3. `client.py` `aclose()` 교체:

```python
    async def aclose(self) -> None:
        try:
            if self._owns_tokens:
                await self._tokens.revoke()
        finally:
            if self._owns_http:
                await self._http.aclose()
```

4. `backend/tests/conftest.py` 생성 + `tests/kiwoom/conftest.py` 삭제:

```python
import pytest

from app.core.config import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(**overrides) -> Settings:
    values: dict = dict(
        kiwoom_app_key="AK", kiwoom_secret_key="SK", kiwoom_mock=True,
        database_url="sqlite+pysqlite:///:memory:",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)
```

   (기존 테스트 파일의 자체 `_settings` 헬퍼는 건드리지 않는다 — 신규 테스트부터
   `from tests.conftest import make_settings` 대신 각 파일에서
   `from conftest import make_settings`가 아닌 **`make_settings`를 직접 import 하지
   말고 동일 시그니처로 사용하려면 `tests/conftest.py`의 함수를
   `from ..conftest import` 할 수 없으므로**, 신규 테스트는
   `import sys` 없이 pytest rootdir 기준 `from conftest import make_settings`가
   동작하지 않는 환경이면 각 파일 로컬 헬퍼를 유지한다. 우선순위는 anyio fixture
   통합이다.)

- [ ] **Step 4: 통과 확인 (전체 회귀)** — Run: `uv run pytest -v`
Expected: PASS (50 + 3 = 53 passed, 6 deselected).

- [ ] **Step 5: 커밋**

```bash
git add -A backend/app backend/tests
git commit -m "chore(kiwoom): hardening sweep + move broker errors to domain"
```

---

### Task 3: 스키마 + 수집 리포지토리 (`store/`)

**Files:**
- Create: `backend/app/store/models.py`, `backend/app/store/collection_store.py`
- Create: `backend/alembic/versions/0002_market_data.py`
- Test: `backend/tests/store/__init__.py`(빈 파일),
  `backend/tests/store/test_models_migration.py`,
  `backend/tests/store/test_collection_store.py`

**Interfaces:**
- Consumes: `Instrument`/`Sector`/`Candle` (domain — Instrument/Sector는 Task 4에서
  추가되므로 **이 태스크에서 미리 domain/broker.py에 두 dataclass만 추가한다**, 포트
  메서드는 Task 4).
- Produces: 테이블 `sectors`/`instruments`/`candles`/`collection_runs` (spec §3.1
  스키마), `CollectionStore(engine, now=None)` — 메서드 시그니처(전부 동기,
  서비스가 `asyncio.to_thread`로 호출):
  `upsert_sectors(Iterable[Sector])`, `upsert_instruments(Iterable[Instrument])`
  (sector_code는 건드리지 않음), `set_sector_codes(dict[str, str])`,
  `upsert_candles(Iterable[Candle])`, `latest_candle_date(symbol) -> date | None`,
  `list_symbols() -> list[str]`, `create_run() -> int`,
  `finish_run(run_id, status, total, succeeded, failed, error_summary=None)`.
  리비전 `0002` (down_revision="0001").

- [ ] **Step 1: domain 모델 2종 추가** — `backend/app/domain/broker.py`에:

```python
@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str           # "kospi" | "kosdaq" | "etf"
    instrument_type: str  # 브로커가 주는 구분값 원문 (스파이크로 확정)


@dataclass(frozen=True)
class Sector:
    code: str
    market: str
    name: str
```

- [ ] **Step 2: 실패하는 테스트 작성**

`backend/tests/store/test_models_migration.py`:

```python
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_0002가_시장데이터_테이블_4종을_만든다(tmp_path, monkeypatch):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'mig.db'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(db_url)).get_table_names())
    assert {"sectors", "instruments", "candles", "collection_runs"} <= names
```

`backend/tests/store/test_collection_store.py`:

```python
from datetime import date, datetime, timezone

from sqlalchemy import create_engine

from app.domain.broker import Candle, Instrument, Sector
from app.store.collection_store import CollectionStore
from app.store.models import Base

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> CollectionStore:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return CollectionStore(engine, now=lambda: NOW)


def _inst(symbol="005930", name="삼성전자") -> Instrument:
    return Instrument(symbol=symbol, name=name, market="kospi", instrument_type="보통주")


def test_instrument_upsert는_멱등이고_sector_code를_보존한다(tmp_path):
    s = _store(tmp_path)
    s.upsert_sectors([Sector(code="001", market="kospi", name="전기전자")])
    s.upsert_instruments([_inst()])
    s.set_sector_codes({"005930": "001"})
    s.upsert_instruments([_inst(name="삼성전자(new)")])  # 재수집 — 이름 갱신
    assert s.list_symbols() == ["005930"]
    latest = s.latest_candle_date("005930")
    assert latest is None  # 봉은 아직 없음
    # sector_code가 upsert에 지워지지 않았는지: set 후 재-upsert에도 조회 가능해야 함
    # (list_symbols는 활성 종목 기준 — sector 검증은 매핑 update 자체가 예외 없이 통과했는지로 갈음)


def test_candle_upsert는_멱등이다(tmp_path):
    s = _store(tmp_path)
    c = Candle(symbol="005930", date=date(2026, 7, 16), open=70000, high=71000,
               low=69900, close=70500, volume=1000)
    s.upsert_candles([c])
    s.upsert_candles([Candle(symbol="005930", date=date(2026, 7, 16), open=70000,
                             high=71000, low=69900, close=70600, volume=1100)])
    assert s.latest_candle_date("005930") == date(2026, 7, 16)


def test_run_라이프사이클(tmp_path):
    s = _store(tmp_path)
    run_id = s.create_run()
    assert isinstance(run_id, int)
    s.finish_run(run_id, "done", total=10, succeeded=9, failed=1)
```

- [ ] **Step 3: 실패 확인** — Run: `uv run pytest tests/store -v`
Expected: FAIL — `ModuleNotFoundError: app.store.models` (마이그레이션 테스트는
리비전 부재로 테이블 누락 실패).

- [ ] **Step 4: 구현**

`backend/app/store/models.py`:

```python
"""수집 파이프라인 스키마. Alembic 0002와 1:1 대응."""

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Integer,
                        String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SectorRow(Base):
    __tablename__ = "sectors"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))


class InstrumentRow(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(16))
    instrument_type: Mapped[str] = mapped_column(String(32), default="")
    sector_code: Mapped[str | None] = mapped_column(
        ForeignKey("sectors.code"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandleRow(Base):
    __tablename__ = "candles"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[int] = mapped_column(Integer)
    high: Mapped[int] = mapped_column(Integer)
    low: Mapped[int] = mapped_column(Integer)
    close: Mapped[int] = mapped_column(Integer)
    volume: Mapped[int] = mapped_column(BigInteger)


class CollectionRunRow(Base):
    __tablename__ = "collection_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    total_symbols: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
```

`backend/alembic/versions/0002_market_data.py`:

```python
"""market data tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sectors",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
    )
    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("sector_code", sa.String(8), sa.ForeignKey("sectors.code"),
                  nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("open", sa.Integer, nullable=False),
        sa.Column("high", sa.Integer, nullable=False),
        sa.Column("low", sa.Integer, nullable=False),
        sa.Column("close", sa.Integer, nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("total_symbols", sa.Integer, nullable=False, server_default="0"),
        sa.Column("succeeded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("collection_runs")
    op.drop_table("candles")
    op.drop_table("instruments")
    op.drop_table("sectors")
```

`backend/app/store/collection_store.py`:

```python
"""수집 파이프라인 영속화. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출한다.
upsert는 dialect별 INSERT..ON CONFLICT (테스트=sqlite, 운영=postgresql)."""

from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.broker import Candle, Instrument, Sector
from app.store.models import CandleRow, CollectionRunRow, InstrumentRow, SectorRow


def _upsert(session: Session, model, rows: list[dict], index_elements: list[str]) -> None:
    if not rows:
        return
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(model).values(rows)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in index_elements}
    session.execute(stmt.on_conflict_do_update(
        index_elements=index_elements, set_=update_cols))


class CollectionStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_sectors(self, sectors: Iterable[Sector]) -> None:
        rows = [{"code": s.code, "market": s.market, "name": s.name} for s in sectors]
        with self._sessions.begin() as session:
            _upsert(session, SectorRow, rows, ["code"])

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = self._now()
        rows = [{"symbol": i.symbol, "name": i.name, "market": i.market,
                 "instrument_type": i.instrument_type, "is_active": True,
                 "updated_at": now} for i in instruments]
        with self._sessions.begin() as session:
            _upsert(session, InstrumentRow, rows, ["symbol"])

    def set_sector_codes(self, mapping: dict[str, str]) -> None:
        with self._sessions.begin() as session:
            for symbol, code in mapping.items():
                session.execute(update(InstrumentRow)
                                .where(InstrumentRow.symbol == symbol)
                                .values(sector_code=code))

    def upsert_candles(self, candles: Iterable[Candle]) -> None:
        rows = [{"symbol": c.symbol, "date": c.date, "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume} for c in candles]
        with self._sessions.begin() as session:
            _upsert(session, CandleRow, rows, ["symbol", "date"])

    def latest_candle_date(self, symbol: str) -> date | None:
        with self._sessions() as session:
            return session.scalar(select(func.max(CandleRow.date))
                                  .where(CandleRow.symbol == symbol))

    def list_symbols(self) -> list[str]:
        with self._sessions() as session:
            return list(session.scalars(select(InstrumentRow.symbol)
                                        .where(InstrumentRow.is_active.is_(True))
                                        .order_by(InstrumentRow.symbol)))

    def create_run(self) -> int:
        with self._sessions.begin() as session:
            run = CollectionRunRow(started_at=self._now(), status="running")
            session.add(run)
            session.flush()
            return run.id

    def finish_run(self, run_id: int, status: str, total: int, succeeded: int,
                   failed: int, error_summary: str | None = None) -> None:
        with self._sessions.begin() as session:
            session.execute(update(CollectionRunRow)
                            .where(CollectionRunRow.id == run_id)
                            .values(finished_at=self._now(), status=status,
                                    total_symbols=total, succeeded=succeeded,
                                    failed=failed, error_summary=error_summary))
```

- [ ] **Step 5: 통과 확인 (전체 회귀)** — Run: `uv run pytest -v`
Expected: PASS (53 + 4 = 57 passed, 6 deselected).

- [ ] **Step 6: 커밋**

```bash
git add backend/app/domain/broker.py backend/app/store backend/alembic/versions/0002_market_data.py backend/tests/store
git commit -m "feat(store): market data schema and upsert repositories"
```

---

### Task 4: 포트 확장 + 키움 카탈로그 TR 매핑 (스파이크 실측 반영)

**Files:**
- Modify: `backend/app/domain/broker.py` (BrokerPort에 3메서드 추가)
- Modify: `backend/app/adapters/kiwoom/broker.py` (3메서드 구현)
- Modify: `backend/tests/live/test_live_smoke.py` (라이브 3케이스 추가)
- Test: `backend/tests/kiwoom/test_broker_catalog.py`

**Interfaces:**
- Consumes: Task 1 스파이크의 실측 필드명 (**코디네이터가 디스패치 델타로 제공 —
  아래 코드의 요청/응답 필드명은 리서치 추정이며 델타가 우선한다**), Task 3의
  `Instrument`/`Sector`.
- Produces: `BrokerPort`에
  `async list_instruments(market: str) -> list[Instrument]`,
  `async list_sectors() -> list[Sector]`,
  `async list_sector_members(sector_code: str) -> list[str]`.
  Task 5가 이 3메서드를 사용. (분기 B 채택 시 `list_sectors`/`list_sector_members`는
  KRX 파일 파서 구현으로 대체 — 포트 시그니처는 동일.)

- [ ] **Step 1: 포트 확장** — `domain/broker.py`의 `BrokerPort`에 추가:

```python
    async def list_instruments(self, market: str) -> list[Instrument]:
        """시장별 상장 종목 목록. market: "kospi" | "kosdaq" | "etf"."""
        ...

    async def list_sectors(self) -> list[Sector]:
        """업종 코드표 (전 시장)."""
        ...

    async def list_sector_members(self, sector_code: str) -> list[str]:
        """해당 업종에 속한 종목코드 목록."""
        ...
```

기존 `test_domain_broker.py`의 Fake에 같은 시그니처 3개를 추가해 계약 테스트 유지.

- [ ] **Step 2: 실패하는 테스트 작성** — `backend/tests/kiwoom/test_broker_catalog.py`
(픽스처 필드명은 **스파이크 실측값으로 교체** — 아래는 추정 형태):

```python
import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.domain.errors import BrokerError

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

INSTRUMENTS_JSON = {"return_code": 0, "list": [
    {"code": "005930", "name": "삼성전자", "marketName": "거래소"},
    {"code": "000660", "name": "SK하이닉스", "marketName": "거래소"},
]}
SECTORS_JSON = {"return_code": 0, "list": [
    {"code": "001", "name": "종합(KOSPI)"},
    {"code": "013", "name": "전기전자"},
]}
MEMBERS_JSON = {"return_code": 0, "list": [
    {"stk_cd": "A005930"}, {"stk_cd": "A000660"},
]}


async def _noop_sleep(_: float) -> None: ...


def _broker() -> KiwoomBroker:
    s = Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                 kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")
    return KiwoomBroker(KiwoomHttpClient(s, sleep=_noop_sleep))


def _mock_auth() -> None:
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_도메인_모델로_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=INSTRUMENTS_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    items = await b.list_instruments("kospi")
    assert [i.symbol for i in items] == ["005930", "000660"]
    assert items[0].market == "kospi" and items[0].name == "삼성전자"
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_instruments는_모르는_시장이면_ValueError():
    _mock_auth()
    b = _broker()
    with pytest.raises(ValueError):
        await b.list_instruments("nasdaq")
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_list_sectors와_members():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=SECTORS_JSON, headers={"cont-yn": "N", "next-key": ""})
    respx.post(f"{BASE}/api/dostk/sect").respond(
        json=MEMBERS_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    sectors = await b.list_sectors()
    assert sectors[1].name == "전기전자"
    members = await b.list_sector_members("013")
    assert members == ["005930", "000660"]  # 'A' 접두 제거 확인
    await b.aclose()
```

- [ ] **Step 3: 실패 확인** — Run: `uv run pytest tests/kiwoom/test_broker_catalog.py -v`
Expected: FAIL — `AttributeError: 'KiwoomBroker' ... 'list_instruments'`.

- [ ] **Step 4: 구현** — `adapters/kiwoom/broker.py`에 추가 (필드명은 스파이크
델타로 확정; Phase 1 패턴 — 예외 래핑·aclosing·`_parse`형 검증 준수):

```python
_MRKT_TP = {"kospi": "0", "kosdaq": "10", "etf": "8"}          # ka10099 (실측 확정)
_SECTOR_MARKETS = (("kospi", "0"), ("kosdaq", "1"))            # ka10101 (실측 확정)


def _normalize_symbol(raw: str) -> str:
    code = raw.removeprefix("A")
    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"unexpected symbol format: {raw!r}")
    return code
```

```python
    async def list_instruments(self, market: str) -> list[Instrument]:
        if market not in _MRKT_TP:
            raise ValueError(f"unknown market: {market}")
        items: list[Instrument] = []
        try:
            async with aclosing(self._client.call_paged(
                    "stkinfo", "ka10099", {"mrkt_tp": _MRKT_TP[market]})) as pages:
                async for page in pages:
                    for row in page.get("list") or []:
                        items.append(Instrument(
                            symbol=_normalize_symbol(row["code"]),
                            name=row["name"],
                            market=market,
                            instrument_type=str(row.get("marketName") or ""),
                        ))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10099]: {type(exc).__name__}") from exc
        return items

    async def list_sectors(self) -> list[Sector]:
        sectors: list[Sector] = []
        try:
            for market, mrkt_tp in _SECTOR_MARKETS:
                data, _, _ = await self._client.call(
                    "stkinfo", "ka10101", {"mrkt_tp": mrkt_tp})
                for row in data.get("list") or []:
                    sectors.append(Sector(code=row["code"], market=market,
                                          name=row["name"]))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10101]: {type(exc).__name__}") from exc
        return sectors

    async def list_sector_members(self, sector_code: str) -> list[str]:
        members: list[str] = []
        try:
            async with aclosing(self._client.call_paged(
                    "sect", "ka20002", {"inds_cd": sector_code})) as pages:
                async for page in pages:
                    for row in page.get("list") or []:
                        members.append(_normalize_symbol(row["stk_cd"]))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka20002]: {type(exc).__name__}") from exc
        return members
```

- [ ] **Step 5: 통과 확인** — Run: `uv run pytest -v`
Expected: PASS (57 + 3 = 60 passed).

- [ ] **Step 6: 라이브 스모크 추가 + 실행** — `tests/live/test_live_smoke.py`에:

```python
@pytest.mark.anyio
async def test_live_종목리스트_코스피(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        items = await b.list_instruments("kospi")
        assert len(items) > 100
        print(f"[live] kospi instruments={len(items)} sample={items[0].symbol}")
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_업종코드와_구성종목(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        sectors = await b.list_sectors()
        assert sectors
        members = await b.list_sector_members(sectors[0].code)
        print(f"[live] sectors={len(sectors)} first={sectors[0].code} members={len(members)}")
        assert isinstance(members, list)
    finally:
        await b.aclose()
```

Run: `uv run pytest -m live -v` — Expected: 전부 PASS (기존 6 + 신규 2 = 8).
불일치 시 실측 정정 절차 후 재실행.

- [ ] **Step 7: 커밋**

```bash
git add backend/app/domain/broker.py backend/app/adapters/kiwoom/broker.py backend/tests/kiwoom/test_broker_catalog.py backend/tests/test_domain_broker.py backend/tests/live/test_live_smoke.py
git commit -m "feat(kiwoom): instrument, sector and membership queries"
```

---

### Task 5: CollectionService (`domain/collection.py`)

**Files:**
- Create: `backend/app/domain/collection.py`
- Test: `backend/tests/test_collection_service.py`

**Interfaces:**
- Consumes: `BrokerPort`(Task 4 확장 포함), `CollectionStore` 시그니처(Task 3 —
  단, 서비스는 구체 클래스가 아니라 동일 메서드를 가진 객체면 됨),
  `app.domain.errors`(Task 2).
- Produces: `CollectionProgress(run_id, status, stage, done, total, failed)` frozen
  dataclass; `CollectionService(broker, store, markets=("kospi","kosdaq","etf"),
  candle_count=600, max_consecutive_failures=20)` —
  `is_running() -> bool`, `progress() -> CollectionProgress | None`,
  `async run() -> None`. Task 6의 API가 사용.

- [ ] **Step 1: 실패하는 테스트 작성** — `backend/tests/test_collection_service.py`:

```python
from datetime import date

import pytest

from app.domain.broker import Candle, Instrument, Sector
from app.domain.collection import CollectionService
from app.domain.errors import AuthError, BrokerError


class FakeBroker:
    def __init__(self, symbols=("005930", "000660"), fail: set[str] | None = None):
        self.symbols = list(symbols)
        self.fail = fail or set()
        self.candle_calls: list[str] = []

    async def list_instruments(self, market):
        if market != "kospi":
            return []
        return [Instrument(symbol=s, name=f"종목{s}", market="kospi",
                           instrument_type="보통주") for s in self.symbols]

    async def list_sectors(self):
        return [Sector(code="001", market="kospi", name="전기전자")]

    async def list_sector_members(self, sector_code):
        return list(self.symbols)

    async def get_daily_candles(self, symbol, count):
        self.candle_calls.append(symbol)
        if symbol in self.fail:
            raise BrokerError(f"boom {symbol}")
        return [Candle(symbol=symbol, date=date(2026, 7, 16), open=1, high=2,
                       low=1, close=2, volume=10)]

    async def get_quote(self, symbol): ...
    async def get_deposit(self): ...
    async def get_balance(self): ...


class MemoryStore:
    def __init__(self):
        self.instruments: dict[str, Instrument] = {}
        self.sector_codes: dict[str, str] = {}
        self.candles: dict[str, list[Candle]] = {}
        self.runs: dict[int, dict] = {}
        self._next = 1

    def upsert_sectors(self, sectors): ...
    def upsert_instruments(self, instruments):
        for i in instruments:
            self.instruments[i.symbol] = i
    def set_sector_codes(self, mapping):
        self.sector_codes.update(mapping)
    def upsert_candles(self, candles):
        for c in candles:
            self.candles.setdefault(c.symbol, []).append(c)
    def latest_candle_date(self, symbol):
        rows = self.candles.get(symbol)
        return max(c.date for c in rows) if rows else None
    def list_symbols(self):
        return sorted(self.instruments)
    def create_run(self):
        rid = self._next; self._next += 1
        self.runs[rid] = {"status": "running"}
        return rid
    def finish_run(self, run_id, status, total, succeeded, failed, error_summary=None):
        self.runs[run_id] = {"status": status, "total": total,
                             "succeeded": succeeded, "failed": failed,
                             "error": error_summary}


@pytest.mark.anyio
async def test_정상_수집은_전_단계를_완료한다():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done" and p.total == 2 and p.failed == 0
    assert store.runs[p.run_id]["succeeded"] == 2
    assert store.sector_codes == {"005930": "001", "000660": "001"}


@pytest.mark.anyio
async def test_재실행은_이미_최신인_종목을_건너뛴다():
    broker, store = FakeBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    first_calls = len(broker.candle_calls)
    await svc.run()
    # 두 번째 run: 기준일 확보 전 첫 종목 1건만 재조회, 나머지는 스킵
    assert len(broker.candle_calls) <= first_calls + 1


@pytest.mark.anyio
async def test_종목_실패는_기록하고_계속한다():
    broker, store = FakeBroker(fail={"005930"}), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    p = svc.progress()
    assert p.status == "done" and p.failed == 1
    assert store.runs[p.run_id]["succeeded"] == 1


@pytest.mark.anyio
async def test_연속_실패가_임계를_넘으면_run_failed():
    symbols = tuple(f"{i:06d}" for i in range(30))
    broker = FakeBroker(symbols=symbols, fail=set(symbols))
    store = MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",),
                            max_consecutive_failures=5)
    await svc.run()
    p = svc.progress()
    assert p.status == "failed"
    assert store.runs[p.run_id]["status"] == "failed"


@pytest.mark.anyio
async def test_인증_오류는_즉시_run_failed():
    class AuthFailBroker(FakeBroker):
        async def get_daily_candles(self, symbol, count):
            raise AuthError("token dead")
    broker, store = AuthFailBroker(), MemoryStore()
    svc = CollectionService(broker, store, markets=("kospi",))
    await svc.run()
    assert svc.progress().status == "failed"
```

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/test_collection_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.domain.collection`.

- [ ] **Step 3: 구현** — `backend/app/domain/collection.py`:

```python
"""수집 오케스트레이션. BrokerPort와 Store 메서드 계약만 안다 (키움/SQL 무지).
Store 호출은 동기이므로 asyncio.to_thread로 이벤트 루프를 막지 않는다."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

from app.domain.broker import BrokerPort
from app.domain.errors import AuthError, BrokerError, RateLimitError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionProgress:
    run_id: int
    status: str  # running | done | failed
    stage: str   # instruments | sectors | candles | finished
    done: int
    total: int
    failed: int


class CollectionService:
    def __init__(self, broker: BrokerPort, store,
                 markets: tuple[str, ...] = ("kospi", "kosdaq", "etf"),
                 candle_count: int = 600,
                 max_consecutive_failures: int = 20) -> None:
        self._broker = broker
        self._store = store
        self._markets = markets
        self._candle_count = candle_count
        self._max_consec = max_consecutive_failures
        self._running = False
        self._progress: CollectionProgress | None = None

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> CollectionProgress | None:
        return self._progress

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("collection already running")
        self._running = True
        run_id = await asyncio.to_thread(self._store.create_run)
        succeeded = failed = total = 0
        try:
            self._set(run_id, "running", "instruments", 0, 0, 0)
            for market in self._markets:
                instruments = await self._broker.list_instruments(market)
                await asyncio.to_thread(self._store.upsert_instruments, instruments)

            self._set(run_id, "running", "sectors", 0, 0, 0)
            sectors = await self._broker.list_sectors()
            await asyncio.to_thread(self._store.upsert_sectors, sectors)
            mapping: dict[str, str] = {}
            for sector in sectors:
                for symbol in await self._broker.list_sector_members(sector.code, sector.market):
                    mapping[symbol] = sector.code
            await asyncio.to_thread(self._store.set_sector_codes, mapping)

            symbols = await asyncio.to_thread(self._store.list_symbols)
            total = len(symbols)
            reference_date: date | None = None
            consecutive = 0
            for i, symbol in enumerate(symbols, start=1):
                latest = await asyncio.to_thread(
                    self._store.latest_candle_date, symbol)
                if (reference_date is not None and latest is not None
                        and latest >= reference_date):
                    succeeded += 1
                    self._set(run_id, "running", "candles", i, total, failed)
                    continue
                try:
                    candles = await self._broker.get_daily_candles(
                        symbol, self._candle_count)
                except (AuthError, RateLimitError):
                    raise  # 서버/인증 장애 — 종목 격리 대상이 아님
                except BrokerError as exc:
                    failed += 1
                    consecutive += 1
                    logger.warning("collect failed for %s: %s", symbol, exc)
                    if consecutive > self._max_consec:
                        raise BrokerError(
                            f"aborted after {consecutive} consecutive failures"
                        ) from exc
                    self._set(run_id, "running", "candles", i, total, failed)
                    continue
                consecutive = 0
                if candles:
                    await asyncio.to_thread(self._store.upsert_candles, candles)
                    if reference_date is None:
                        reference_date = max(c.date for c in candles)
                succeeded += 1
                self._set(run_id, "running", "candles", i, total, failed)

            await asyncio.to_thread(self._store.finish_run, run_id, "done",
                                    total, succeeded, failed, None)
            self._set(run_id, "done", "finished", total, total, failed)
        except BrokerError as exc:
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    total, succeeded, failed, str(exc))
            self._set(run_id, "failed", "finished", succeeded + failed, total, failed)
        finally:
            self._running = False

    def _set(self, run_id: int, status: str, stage: str,
             done: int, total: int, failed: int) -> None:
        self._progress = CollectionProgress(run_id, status, stage, done, total, failed)
```

- [ ] **Step 4: 통과 확인** — Run: `uv run pytest -v`
Expected: PASS (60 + 5 = 65 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/collection.py backend/tests/test_collection_service.py
git commit -m "feat(domain): collection service with resume and fault tolerance"
```

---

### Task 6: 수집 API + 앱 조립 (`api/collect.py`, `main.py`)

**Files:**
- Create: `backend/app/api/collect.py`
- Modify: `backend/app/main.py` (서비스 조립 + 라우터 + 종료 시 태스크 취소)
- Test: `backend/tests/test_api_collect.py`

**Interfaces:**
- Consumes: `CollectionService`(Task 5), `CollectionStore`(Task 3), lifespan(Phase 1).
- Produces: `POST /collect` → 202 `{"started": true}` / 실행 중 409;
  `GET /collect/status` → `{"status": "idle"}` 또는 `{"run_id", "status", "stage",
  "done", "total", "failed"}`. `app.state.collection`에 서비스 보관.

- [ ] **Step 1: 실패하는 테스트 작성** — `backend/tests/test_api_collect.py`:

```python
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.collection import CollectionProgress
from app.main import create_app


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


class StubService:
    def __init__(self, running=False, progress=None):
        self._running = running
        self._progress = progress
        self.run_called = 0

    def is_running(self):
        return self._running

    def progress(self):
        return self._progress

    async def run(self):
        self.run_called += 1


def test_collect는_시작하면_202():
    app = create_app(_settings())
    with TestClient(app) as client:
        stub = StubService()
        app.state.collection = stub
        r = client.post("/collect")
    assert r.status_code == 202 and r.json() == {"started": True}


def test_이미_실행중이면_409():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService(running=True)
        assert client.post("/collect").status_code == 409


def test_status는_progress를_그대로_노출한다():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService(progress=CollectionProgress(
            run_id=1, status="running", stage="candles", done=10, total=100, failed=2))
        body = client.get("/collect/status").json()
    assert body == {"run_id": 1, "status": "running", "stage": "candles",
                    "done": 10, "total": 100, "failed": 2}


def test_최초에는_idle():
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.collection = StubService()
        assert client.get("/collect/status").json() == {"status": "idle"}
```

- [ ] **Step 2: 실패 확인** — Run: `uv run pytest tests/test_api_collect.py -v`
Expected: FAIL — 404 (라우트 없음).

- [ ] **Step 3: 구현**

`backend/app/api/collect.py`:

```python
import asyncio

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.post("/collect", status_code=202)
async def start_collection(request: Request) -> dict:
    service = request.app.state.collection
    if service.is_running():
        raise HTTPException(status_code=409, detail="collection already running")
    task = asyncio.create_task(service.run())
    request.app.state.collection_task = task  # 참조 유지 (GC로 태스크 소실 방지)
    return {"started": True}


@router.get("/collect/status")
async def collection_status(request: Request) -> dict:
    progress = request.app.state.collection.progress()
    if progress is None:
        return {"status": "idle"}
    return {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total, "failed": progress.failed}
```

`backend/app/main.py` — import 추가:

```python
from app.api.collect import router as collect_router
from app.domain.collection import CollectionService
from app.store.collection_store import CollectionStore
```

lifespan을 다음으로 교체 (기존 중첩 try/finally 유지 + collection 조립·취소):

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = create_db_engine(settings)
        try:
            app.state.broker = KiwoomBroker(KiwoomHttpClient(settings))
            app.state.collection = CollectionService(
                app.state.broker, CollectionStore(app.state.engine))
            app.state.collection_task = None
            try:
                yield
            finally:
                task = app.state.collection_task
                if task is not None and not task.done():
                    task.cancel()
                await app.state.broker.aclose()
        finally:
            app.state.engine.dispose()
```

라우터 등록 1줄 추가: `app.include_router(collect_router)`

- [ ] **Step 4: 통과 확인** — Run: `uv run pytest -v`
Expected: PASS (65 + 4 = 69 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/api/collect.py backend/app/main.py backend/tests/test_api_collect.py
git commit -m "feat(api): collection trigger and status endpoints"
```

---

### Task 7: 풀 수집 실측 + 실측 팩트 반영 + 회고록 + STATUS (코디네이터 주도)

**Files:**
- Modify: `CLAUDE.md` §5 (신규 TR 실측 팩트), `docs/STATUS.md` (Phase 3 핸드오프)
- Modify: `docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md`
  (§5 스파이크 대상 → 실측 결과)
- Create: `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md`

**Interfaces:**
- Consumes: 전체 산출물, 원장, 스파이크/라이브 실측 기록.
- Produces: Phase 2 완료 선언 + Phase 3 재개 지점.

- [ ] **Step 1: 풀 수집 실측 (코디네이터)**

```bash
docker compose up --build -d              # 0002 자동 적용
curl -s -X POST http://127.0.0.1:8000/collect          # {"started": true}
# 주기적으로:
curl -s http://127.0.0.1:8000/collect/status
# 완료 후 DB 검증:
docker compose exec db psql -U ohmystock -d ohmystock -c \
  "SELECT (SELECT count(*) FROM instruments) AS instruments, \
          (SELECT count(*) FROM candles) AS candles, \
          (SELECT count(*) FROM sectors) AS sectors; \
   SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1;"
```

기록: 소요 시간, 종목 수, 실패 수, candles 행 수. **재실행해 스킵 동작 확인**
(두 번째 run이 수 분 내 종료되는지). 실패 시 원인 수정 후 재실행 — 완주 전 완료
주장 금지.

- [ ] **Step 2: 문서 갱신 + 회고록** — CLAUDE.md §5에 ka10099/ka10101/ka20002(또는
대안 B) 실측 팩트를 "verified live (2026-07-17)"로 추가, spec §5 갱신, 규칙 4
회고록(태스크별 파일·커밋·패널 결과, 스파이크 판정, 풀 수집 실측 수치, 남은 항목),
STATUS를 Phase 3(스코어링 엔진) 재개 지점으로 갱신 (수집 실행 권장 시간대 19시 이후
명시).

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md docs/STATUS.md docs/specs/2026-07-17-phase2-data-collection-pipeline-design.md docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md
git commit -m "docs: phase 2 retrospective + collection facts + status handoff"
```
