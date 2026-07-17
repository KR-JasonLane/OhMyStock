# Phase 1 키움 브로커 어댑터 구현 계획서

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **날짜:** 2026-07-17
- **근거 spec:** `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md`

**목표:** `BrokerPort` 추상화 + 키움 REST 구현체(인증·시세/일봉·계좌 조회)를 TDD로
구축하고, 각 TR을 모의서버 라이브 스모크로 실측 검증한다.

**아키텍처:** `domain/broker.py`(포트+모델, 키움 무지) ← `adapters/kiwoom/`(TokenManager,
RateLimiter, HttpClient, KiwoomBroker). FastAPI lifespan에서 브로커 생성/종료.

**기술 스택:** Python 3.12 + uv + httpx(비동기) + pydantic-settings(기존) + pytest +
respx(HTTP 모킹, dev 추가) + tzdata(KST, 신규 추가).

## 전역 제약 (모든 태스크에 암묵 적용)

- Python 3.12, uv. 백엔드 명령은 `backend/`에서 (`uv run pytest`).
- **`domain/`은 키움을 모른다** — `app/adapters/...` import 금지. 어댑터만 도메인을 안다.
- 시크릿(app key/secret/토큰)을 로그·에러 메시지·테스트 출력·커밋에 절대 노출 금지.
- 금액·가격·수량은 **정수(원 단위)**. 등락율만 float.
- 키움 응답 필드명은 비공식 리서치 기반 — **각 TR은 라이브 스모크(pytest -m live)로
  실측 확인**하고, 불일치 시 픽스처·매핑을 실측값으로 수정 후 보고서에 기록한다.
- 라이브 스모크는 기본 실행에서 제외(`addopts -m "not live"`), 명시 실행만
  (`uv run pytest -m live`). 모의서버(`KIWOOM_MOCK=true`) 대상으로만 실행.
- 태스크 완료 후 **4-에이전트 리뷰 패널**(senior-developer, senior-trader,
  architecture-expert, security-expert — `.claude/agents/`) 전원 통과 필수 (코디네이터가
  디스패치; 구현자는 신경 쓰지 않는다).
- 커밋 메시지는 각 태스크 명시된 것 그대로, 트레일러/AI 흔적 금지.
- Phase 0 테스트 9개 회귀 유지 — 전체 스위트는 항상 그린.

## 선행조건

- Task 1: docker compose 스택 조작 가능 (Docker Desktop 실행 중).
- Task 3부터 라이브 스모크: 사용자가 `.env`의 `KIWOOM_APP_KEY`/`KIWOOM_SECRET_KEY`를
  **실제 발급 키로 교체** + `KIWOOM_MOCK=true` 확인. (코디네이터가 Task 3 시작 전 요청)

## 파일 구조 (최종 산출물)

```
docker-compose.yml                     # Task 1 수정 (포트 바인딩)
backend/
├─ pyproject.toml                      # Task 3 수정 (respx, tzdata, pytest 마커)
├─ app/
│  ├─ domain/broker.py                 # Task 2 — 포트 + 모델
│  ├─ adapters/kiwoom/
│  │  ├─ __init__.py                   # Task 3
│  │  ├─ errors.py                     # Task 3
│  │  ├─ auth.py                       # Task 3 — TokenManager
│  │  ├─ rate_limiter.py               # Task 4
│  │  ├─ client.py                     # Task 5 — KiwoomHttpClient
│  │  └─ broker.py                     # Task 6~7 — KiwoomBroker
│  └─ main.py                          # Task 8 수정 (lifespan)
└─ tests/
   ├─ test_domain_broker.py            # Task 2
   ├─ kiwoom/{__init__.py, test_auth.py, test_rate_limiter.py,
   │          test_client.py, test_broker_market.py, test_broker_account.py}
   ├─ test_app_lifespan.py             # Task 8
   └─ live/{__init__.py, test_live_smoke.py}   # Task 3/6/7 누적
docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md  # Task 9
```

---

### Task 1: compose 포트를 localhost로 제한

**Files:**
- Modify: `docker-compose.yml` (backend ports 1줄)

**Interfaces:**
- Consumes: Phase 0의 compose 구성.
- Produces: 백엔드가 호스트 `127.0.0.1`에서만 접근 가능 (LAN 차단). 이후 모든 태스크의
  전제 보안 상태.

- [ ] **Step 1: ports 수정**

`docker-compose.yml`의 backend 서비스에서:

```yaml
    ports:
      - "8000:8000"
```
→
```yaml
    ports:
      - "127.0.0.1:8000:8000"
```

- [ ] **Step 2: 재기동 검증**

```bash
docker compose up -d
curl -s http://127.0.0.1:8000/health
```
Expected: `{"status":"ok","db":"ok","mode":"mock"}` (127.0.0.1로 정상 접근).

- [ ] **Step 3: 커밋**

```bash
git add docker-compose.yml
git commit -m "chore: bind backend port to localhost only"
```

---

### Task 2: 도메인 포트 + 모델 (`domain/broker.py`)

**Files:**
- Create: `backend/app/domain/broker.py`
- Test: `backend/tests/test_domain_broker.py`

**Interfaces:**
- Produces (이후 전 태스크가 의존):
  - `Quote(symbol: str, name: str, price: int, change_rate: float, volume: int)`
  - `Candle(symbol: str, date: date, open: int, high: int, low: int, close: int, volume: int)`
  - `Deposit(total: int, available: int)`
  - `Position(symbol: str, name: str, quantity: int, avg_price: int, current_price: int, eval_amount: int)`
  - `Balance(positions: list[Position], total_eval: int, total_profit: int)`
  - `BrokerPort` (`@runtime_checkable` Protocol): `get_quote(symbol) -> Quote`,
    `get_daily_candles(symbol, count) -> list[Candle]`, `get_deposit() -> Deposit`,
    `get_balance() -> Balance` — 전부 async.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_domain_broker.py`:

```python
from datetime import date

from app.domain.broker import Balance, BrokerPort, Candle, Deposit, Position, Quote


def test_모델은_불변이고_필드를_보존한다():
    q = Quote(symbol="005930", name="삼성전자", price=71000, change_rate=1.25, volume=1000)
    c = Candle(symbol="005930", date=date(2026, 7, 16), open=70500, high=71200,
               low=70100, close=71000, volume=999)
    d = Deposit(total=100_000, available=90_000)
    p = Position(symbol="005930", name="삼성전자", quantity=10, avg_price=69000,
                 current_price=71000, eval_amount=710_000)
    b = Balance(positions=[p], total_eval=710_000, total_profit=20_000)
    assert q.price == 71000 and c.close == 71000 and d.available == 90_000
    assert b.positions[0].quantity == 10
    import pytest
    with pytest.raises(Exception):
        q.price = 0  # frozen


def test_BrokerPort는_런타임_프로토콜이다():
    class Fake:
        async def get_quote(self, symbol): ...
        async def get_daily_candles(self, symbol, count): ...
        async def get_deposit(self): ...
        async def get_balance(self): ...

    assert isinstance(Fake(), BrokerPort)
    assert not isinstance(object(), BrokerPort)
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_domain_broker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.broker'`.

- [ ] **Step 3: 최소 구현**

`backend/app/domain/broker.py`:

```python
"""브로커 포트와 도메인 모델. 이 모듈은 특정 증권사를 알지 못한다."""

from dataclasses import dataclass, field
from datetime import date as date_
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: int          # 현재가 (원)
    change_rate: float  # 등락율 (%)
    volume: int         # 누적 거래량


@dataclass(frozen=True)
class Candle:
    symbol: str
    date: date_
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True)
class Deposit:
    total: int      # 예수금 (원)
    available: int  # 주문가능금액 (원)


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    quantity: int
    avg_price: int      # 평균 매입가 (원)
    current_price: int  # 현재가 (원)
    eval_amount: int    # 평가금액 (원)


@dataclass(frozen=True)
class Balance:
    positions: list[Position] = field(default_factory=list)
    total_eval: int = 0    # 총평가금액 (원)
    total_profit: int = 0  # 총평가손익 (원, 음수 가능)


@runtime_checkable
class BrokerPort(Protocol):
    """브로커가 제공해야 하는 계약. 주문/실시간은 Phase 5에서 확장한다."""

    async def get_quote(self, symbol: str) -> Quote: ...

    async def get_daily_candles(self, symbol: str, count: int) -> list[Candle]:
        """최근 count개 일봉을 과거→최신 순으로 반환한다."""
        ...

    async def get_deposit(self) -> Deposit: ...

    async def get_balance(self) -> Balance: ...
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (기존 9 + 신규 2 = 11 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/broker.py backend/tests/test_domain_broker.py
git commit -m "feat(domain): broker port and market/account models"
```

---

### Task 3: 에러 계층 + TokenManager (`adapters/kiwoom/auth.py`)

**Files:**
- Create: `backend/app/adapters/kiwoom/__init__.py` (빈 파일)
- Create: `backend/app/adapters/kiwoom/errors.py`
- Create: `backend/app/adapters/kiwoom/auth.py`
- Modify: `backend/pyproject.toml` (dev에 respx, deps에 tzdata, pytest 마커/addopts)
- Test: `backend/tests/kiwoom/__init__.py`(빈 파일), `backend/tests/kiwoom/conftest.py`,
  `backend/tests/kiwoom/test_auth.py`
- Test: `backend/tests/live/__init__.py`(빈 파일), `backend/tests/live/test_live_smoke.py`

**Interfaces:**
- Consumes: `Settings`(Phase 0), httpx.
- Produces:
  - `errors.py`: `BrokerError(Exception)`, `AuthError(BrokerError)`,
    `RateLimitError(BrokerError)`, `ApiError(BrokerError)` — `ApiError(return_code: int,
    return_msg: str, api_id: str | None = None)`, 속성으로 보존.
  - `auth.py`: `KST = ZoneInfo("Asia/Seoul")`,
    `TokenManager(http: httpx.AsyncClient, app_key: str, secret_key: str,
    margin_seconds: int = 60, now: Callable[[], datetime] | None = None)` —
    `async get_token() -> str`(캐시, 만료 임박 시 재발급), `invalidate() -> None`(캐시
    폐기 — 401 재시도용), `async revoke() -> None`(서버에 토큰 폐기, 실패해도 예외 없이
    로그만). Task 5의 client가 사용.

- [ ] **Step 1: 의존성/마커 추가**

`backend/pyproject.toml` — `dependencies`에 `"tzdata>=2024.1",` 추가(Windows에서
zoneinfo 데이터 보장), `[dependency-groups] dev`에 `"respx>=0.22",` 추가,
`[tool.pytest.ini_options]`에 아래 두 줄 추가:

```toml
markers = ["live: 실제 키움 모의서버를 호출하는 스모크 테스트 (기본 제외)"]
addopts = "-m \"not live\""
```

Run: `cd backend && uv sync`
Expected: respx, tzdata 설치 성공.

- [ ] **Step 2: 실패하는 테스트 작성**

`backend/tests/kiwoom/test_auth.py`:

```python
from datetime import datetime

import httpx
import pytest
import respx

from app.adapters.kiwoom.auth import KST, TokenManager
from app.adapters.kiwoom.errors import AuthError

BASE = "https://mockapi.kiwoom.com"


def _token_response(token: str, expires_dt: str, code: int = 0) -> dict:
    return {"token": token, "token_type": "bearer", "expires_dt": expires_dt,
            "return_code": code, "return_msg": "ok"}


def _manager(now: datetime) -> tuple[TokenManager, httpx.AsyncClient]:
    http = httpx.AsyncClient(base_url=BASE)
    tm = TokenManager(http, app_key="AK", secret_key="SK", now=lambda: now)
    return tm, http


@pytest.mark.anyio
@respx.mock
async def test_최초_호출시_토큰을_발급한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    assert await tm.get_token() == "TOK1"
    assert route.call_count == 1
    body = route.calls[0].request.content
    assert b"client_credentials" in body and b"AK" in body
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_만료_전에는_캐시를_재사용한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    await tm.get_token()
    await tm.get_token()
    assert route.call_count == 1
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_만료_임박시_재발급한다():
    # 만료 09:00:30, 마진 60초 → 09:00:00 시점엔 이미 임박 → 두 번째 호출도 재발급
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK", "20260717090030"))
    tm, http = _manager(now)
    await tm.get_token()
    await tm.get_token()
    assert route.call_count == 2
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_발급_실패시_AuthError():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("", "20260717235959", code=8005))
    tm, http = _manager(now)
    with pytest.raises(AuthError):
        await tm.get_token()
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_invalidate_후에는_재발급한다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    tm, http = _manager(now)
    await tm.get_token()
    tm.invalidate()
    await tm.get_token()
    assert route.call_count == 2
    await http.aclose()


@pytest.mark.anyio
@respx.mock
async def test_revoke는_서버에_폐기를_요청하고_캐시를_비운다():
    now = datetime(2026, 7, 17, 9, 0, 0, tzinfo=KST)
    token_route = respx.post(f"{BASE}/oauth2/token").respond(
        json=_token_response("TOK1", "20260717235959"))
    revoke_route = respx.post(f"{BASE}/oauth2/revoke").respond(
        json={"return_code": 0, "return_msg": "ok"})
    tm, http = _manager(now)
    await tm.get_token()
    await tm.revoke()
    assert revoke_route.call_count == 1
    await tm.get_token()                 # 캐시가 비워졌으므로 재발급
    assert token_route.call_count == 2
    await http.aclose()
```

`pytest.mark.anyio` 사용을 위해 `backend/tests/kiwoom/conftest.py` 생성:

```python
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 3: 실패 확인**

Run: `cd backend && uv run pytest tests/kiwoom/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.adapters.kiwoom'`.

- [ ] **Step 4: 최소 구현**

`backend/app/adapters/kiwoom/errors.py`:

```python
class BrokerError(Exception):
    """브로커 어댑터 공통 베이스 — 호출자는 이 타입 하나로 브로커 장애를 처리한다."""


class AuthError(BrokerError):
    pass


class RateLimitError(BrokerError):
    pass


class ApiError(BrokerError):
    def __init__(self, return_code: int, return_msg: str, api_id: str | None = None):
        self.return_code = return_code
        self.return_msg = return_msg
        self.api_id = api_id
        super().__init__(f"kiwoom api error [{api_id}] {return_code}: {return_msg}")
```

`backend/app/adapters/kiwoom/auth.py`:

```python
"""키움 OAuth2 토큰 수명주기. 토큰은 메모리에만 보관하고 로그에 남기지 않는다."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.adapters.kiwoom.errors import AuthError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
_EXPIRES_FMT = "%Y%m%d%H%M%S"  # 키움 expires_dt: 절대 만료시각(KST)


class TokenManager:
    def __init__(
        self,
        http: httpx.AsyncClient,
        app_key: str,
        secret_key: str,
        margin_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._app_key = app_key
        self._secret_key = secret_key
        self._margin = timedelta(seconds=margin_seconds)
        self._now = now or (lambda: datetime.now(KST))
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token is None or self._is_expiring():
                await self._issue()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        """서버가 토큰을 거부했을 때(만료 등) 캐시를 버려 다음 호출에서 재발급되게 한다."""
        self._token = None
        self._expires_at = None

    async def revoke(self) -> None:
        if self._token is None:
            return
        try:
            await self._http.post(
                "/oauth2/revoke",
                json={"appkey": self._app_key, "secretkey": self._secret_key,
                      "token": self._token},
            )
            logger.info("kiwoom token revoked")
        except httpx.HTTPError as exc:  # 종료 경로 — 실패해도 앱 종료를 막지 않는다
            logger.warning("kiwoom token revoke failed: %s", type(exc).__name__)
        finally:
            self.invalidate()

    def _is_expiring(self) -> bool:
        return self._expires_at is None or self._now() >= self._expires_at - self._margin

    async def _issue(self) -> None:
        resp = await self._http.post(
            "/oauth2/token",
            json={"grant_type": "client_credentials",
                  "appkey": self._app_key, "secretkey": self._secret_key},
        )
        data = resp.json()
        if resp.status_code != 200 or data.get("return_code") != 0 or not data.get("token"):
            # 시크릿/토큰은 메시지에 넣지 않는다
            raise AuthError(
                f"token issue failed: http={resp.status_code} "
                f"code={data.get('return_code')} msg={data.get('return_msg')}"
            )
        self._token = data["token"]
        self._expires_at = datetime.strptime(
            data["expires_dt"], _EXPIRES_FMT).replace(tzinfo=KST)
        logger.info("kiwoom token issued, expires_at=%s", self._expires_at.isoformat())
```

- [ ] **Step 5: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (11 + 6 = 17 passed; live 마커는 deselected로 표시).

- [ ] **Step 6: 라이브 스모크 추가**

`backend/tests/live/test_live_smoke.py` (신규 — 이후 태스크가 이 파일에 추가):

```python
"""실제 키움 모의서버 스모크. 실행: uv run pytest -m live -v
.env에 실제 발급 키 필요. KIWOOM_MOCK=true인 경우에만 실행된다."""

import httpx
import pytest

from app.adapters.kiwoom.auth import TokenManager
from app.core.config import Settings

pytestmark = pytest.mark.live

MOCK_BASE = "https://mockapi.kiwoom.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    s = Settings()  # .env에서 로드
    if not s.kiwoom_mock:
        pytest.skip("라이브 스모크는 모의서버(KIWOOM_MOCK=true)에서만 실행한다")
    return s


@pytest.mark.anyio
async def test_live_토큰_발급과_폐기(settings):
    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=10) as http:
        tm = TokenManager(http, settings.kiwoom_app_key, settings.kiwoom_secret_key)
        token = await tm.get_token()
        assert token  # 값 자체는 출력하지 않는다
        await tm.revoke()
```

- [ ] **Step 7: 라이브 스모크 실행 (실키 필요 — 없으면 코디네이터에게 보고 후 보류)**

Run: `cd backend && uv run pytest -m live -v`
Expected: PASS (토큰 발급 성공). **실패 시 응답의 status/return_code/return_msg를
보고서에 기록** (필드명 불일치 = 실측 데이터 확보 기회 — 픽스처·구현을 실측에 맞게 수정).

- [ ] **Step 8: 커밋**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/adapters/kiwoom backend/tests/kiwoom backend/tests/live
git commit -m "feat(kiwoom): token manager with auto-reissue + broker errors"
```

---

### Task 4: TR별 레이트리미터 (`adapters/kiwoom/rate_limiter.py`)

**Files:**
- Create: `backend/app/adapters/kiwoom/rate_limiter.py`
- Test: `backend/tests/kiwoom/test_rate_limiter.py`

**Interfaces:**
- Produces: `RateLimiter(rate: float = 1.0, burst: int = 2,
  clock: Callable[[], float] | None = None, sleep: Callable[[float], Awaitable[None]] | None = None)`
  — `async acquire(tr_id: str) -> None`. TR(api-id)별 독립 토큰버킷. Task 5가 사용.
  rate/burst 기본값은 비공식 리서치 수치(설정 가능하게 둔 이유).

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/kiwoom/test_rate_limiter.py`:

```python
import pytest

from app.adapters.kiwoom.rate_limiter import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.mark.anyio
async def test_버스트_한도까지는_대기없이_통과한다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=2, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    await rl.acquire("ka10081")
    assert c.sleeps == []


@pytest.mark.anyio
async def test_버스트_초과시_보충될_때까지_대기한다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=2, clock=c.now, sleep=c.sleep)
    for _ in range(3):
        await rl.acquire("ka10081")
    assert c.sleeps == [pytest.approx(1.0)]  # 3번째는 1토큰 보충(1초) 대기


@pytest.mark.anyio
async def test_TR별로_버킷이_독립이다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=1, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    await rl.acquire("ka10001")  # 다른 TR — 대기 없음
    assert c.sleeps == []


@pytest.mark.anyio
async def test_시간이_지나면_토큰이_보충된다():
    c = FakeClock()
    rl = RateLimiter(rate=1.0, burst=1, clock=c.now, sleep=c.sleep)
    await rl.acquire("ka10081")
    c.t += 1.0
    await rl.acquire("ka10081")
    assert c.sleeps == []
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/kiwoom/test_rate_limiter.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 최소 구현**

`backend/app/adapters/kiwoom/rate_limiter.py`:

```python
"""TR(api-id)별 토큰버킷. 키움 레이트리밋(비공식: TR당 ~1 req/s, burst ~2) 준수용.
수치는 실측 후 조정할 수 있도록 생성자 인자로 열어둔다."""

import asyncio
import time
from collections.abc import Awaitable, Callable


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class RateLimiter:
    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 2,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, tr_id: str) -> None:
        async with self._lock:
            now = self._clock()
            bucket = self._buckets.get(tr_id)
            if bucket is None:
                bucket = self._buckets[tr_id] = _Bucket(self._burst, now)
            bucket.tokens = min(self._burst,
                                bucket.tokens + (now - bucket.last) * self._rate)
            bucket.last = now
            if bucket.tokens < 1.0:
                wait = (1.0 - bucket.tokens) / self._rate
                await self._sleep(wait)
                bucket.tokens = 1.0
                bucket.last = self._clock()
            bucket.tokens -= 1.0
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (17 + 4 = 21 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/adapters/kiwoom/rate_limiter.py backend/tests/kiwoom/test_rate_limiter.py
git commit -m "feat(kiwoom): per-TR rate limiter"
```

---

### Task 5: HTTP 클라이언트 (`adapters/kiwoom/client.py`)

**Files:**
- Create: `backend/app/adapters/kiwoom/client.py`
- Test: `backend/tests/kiwoom/test_client.py`

**Interfaces:**
- Consumes: `Settings`(base URL 결정: `kiwoom_mock` → `https://mockapi.kiwoom.com` /
  아니면 `https://api.kiwoom.com`), `TokenManager`(Task 3), `RateLimiter`(Task 4),
  `ApiError`/`RateLimitError`(Task 3).
- Produces: `KiwoomHttpClient(settings: Settings, *, token_manager=None, limiter=None,
  http=None, sleep=None)` —
  - `async call(category: str, api_id: str, body: dict, cont_yn: str = "N",
    next_key: str = "") -> tuple[dict, str, str]` — (응답 JSON, 응답 cont-yn, 응답 next-key)
  - `async call_paged(category: str, api_id: str, body: dict,
    max_pages: int = 50) -> AsyncIterator[dict]` — cont-yn="Y" 동안 페이지 반복
  - `async aclose()` — revoke + http 종료
  - TR URL 규칙: `POST {base}/api/dostk/{category}`, 헤더 `authorization: Bearer …`,
    `api-id`, (연속조회 시) `cont-yn`/`next-key`. Task 6~7의 `KiwoomBroker`가 사용.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/kiwoom/test_client.py`:

```python
import httpx
import pytest
import respx

from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.errors import ApiError, RateLimitError
from app.core.config import Settings

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


async def _noop_sleep(_: float) -> None:
    return None


def _client() -> KiwoomHttpClient:
    return KiwoomHttpClient(_settings(), sleep=_noop_sleep)


def _mock_auth() -> None:
    """토큰 발급 + (aclose 시 호출되는) 폐기 라우트를 함께 모킹한다."""
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_call은_헤더와_바디를_구성하고_JSON을_반환한다():
    _mock_auth()
    route = respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 0, "return_msg": "ok", "stk_nm": "삼성전자"},
        headers={"cont-yn": "N", "next-key": ""})
    c = _client()
    data, cont, nk = await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert data["stk_nm"] == "삼성전자" and cont == "N" and nk == ""
    req = route.calls[0].request
    assert req.headers["api-id"] == "ka10001"
    assert req.headers["authorization"] == "Bearer TOK"
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_return_code가_0이_아니면_ApiError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json={"return_code": 3, "return_msg": "조회 오류"})
    c = _client()
    with pytest.raises(ApiError) as ei:
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert ei.value.return_code == 3 and ei.value.api_id == "ka10001"
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_401이면_토큰_재발급_후_1회_재시도한다():
    token_route = respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})
    tr = respx.post(f"{BASE}/api/dostk/stkinfo")
    tr.side_effect = [
        httpx.Response(401, json={"return_msg": "token expired"}),
        httpx.Response(200, json={"return_code": 0, "stk_nm": "삼성전자"},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    c = _client()
    data, _, _ = await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    assert data["stk_nm"] == "삼성전자"
    assert token_route.call_count == 2  # 최초 발급 + 재발급
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_429는_백오프_재시도_후_소진되면_RateLimitError():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(429)
    c = _client()
    with pytest.raises(RateLimitError):
        await c.call("stkinfo", "ka10001", {"stk_cd": "005930"})
    await c.aclose()


@pytest.mark.anyio
@respx.mock
async def test_call_paged는_cont_yn_Y_동안_반복한다():
    _mock_auth()
    tr = respx.post(f"{BASE}/api/dostk/chart")
    tr.side_effect = [
        httpx.Response(200, json={"return_code": 0, "page": 1},
                       headers={"cont-yn": "Y", "next-key": "K1"}),
        httpx.Response(200, json={"return_code": 0, "page": 2},
                       headers={"cont-yn": "N", "next-key": ""}),
    ]
    c = _client()
    pages = [p async for p in c.call_paged("chart", "ka10081", {"stk_cd": "005930"})]
    assert [p["page"] for p in pages] == [1, 2]
    # 2번째 요청이 이전 응답의 next-key를 실었는지
    assert tr.calls[1].request.headers["cont-yn"] == "Y"
    assert tr.calls[1].request.headers["next-key"] == "K1"
    await c.aclose()
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/kiwoom/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 최소 구현**

`backend/app/adapters/kiwoom/client.py`:

```python
"""키움 REST 호출의 공통 관문: 인증 헤더, TR 헤더, 레이트리밋, 429/401 재시도,
연속조회(cont-yn/next-key) 반복. TR별 의미는 broker.py가 안다."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from app.adapters.kiwoom.auth import TokenManager
from app.adapters.kiwoom.errors import ApiError, BrokerError, RateLimitError
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings

logger = logging.getLogger(__name__)

MOCK_BASE = "https://mockapi.kiwoom.com"
REAL_BASE = "https://api.kiwoom.com"
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # 429 재시도 간격 (지수)


class KiwoomHttpClient:
    def __init__(
        self,
        settings: Settings,
        *,
        token_manager: TokenManager | None = None,
        limiter: RateLimiter | None = None,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        base = MOCK_BASE if settings.kiwoom_mock else REAL_BASE
        self._http = http or httpx.AsyncClient(base_url=base, timeout=10.0)
        self._tokens = token_manager or TokenManager(
            self._http, settings.kiwoom_app_key, settings.kiwoom_secret_key)
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep or asyncio.sleep

    async def call(
        self, category: str, api_id: str, body: dict,
        cont_yn: str = "N", next_key: str = "",
    ) -> tuple[dict, str, str]:
        await self._limiter.acquire(api_id)
        reissued = False
        for attempt in range(len(_BACKOFF_SECONDS) + 1):
            headers = {
                "authorization": f"Bearer {await self._tokens.get_token()}",
                "api-id": api_id,
            }
            if cont_yn == "Y":
                headers["cont-yn"] = "Y"
                headers["next-key"] = next_key
            try:
                resp = await self._http.post(
                    f"/api/dostk/{category}", json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise BrokerError(f"kiwoom http failure [{api_id}]: "
                                  f"{type(exc).__name__}") from exc

            if resp.status_code == 401 and not reissued:
                logger.info("kiwoom 401 on %s — reissuing token", api_id)
                self._tokens.invalidate()
                reissued = True
                continue
            if resp.status_code == 429:
                if attempt < len(_BACKOFF_SECONDS):
                    wait = _BACKOFF_SECONDS[attempt]
                    logger.warning("kiwoom 429 on %s — backoff %.1fs", api_id, wait)
                    await self._sleep(wait)
                    continue
                raise RateLimitError(f"rate limit exhausted [{api_id}]")

            data = resp.json()
            code = data.get("return_code")
            if resp.status_code != 200 or (code is not None and code != 0):
                raise ApiError(code if code is not None else resp.status_code,
                               str(data.get("return_msg")), api_id)
            return (data,
                    resp.headers.get("cont-yn", "N"),
                    resp.headers.get("next-key", ""))
        raise RateLimitError(f"rate limit exhausted [{api_id}]")  # 방어적 — 도달 불가 경로

    async def call_paged(
        self, category: str, api_id: str, body: dict, max_pages: int = 50,
    ) -> AsyncIterator[dict]:
        cont_yn, next_key = "N", ""
        for _ in range(max_pages):
            data, cont_yn, next_key = await self.call(
                category, api_id, body, cont_yn=cont_yn, next_key=next_key)
            yield data
            if cont_yn != "Y":
                return
            cont_yn = "Y"
        logger.warning("kiwoom paging stopped at max_pages=%d [%s]", max_pages, api_id)

    async def aclose(self) -> None:
        await self._tokens.revoke()
        await self._http.aclose()
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (21 + 5 = 26 passed).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/adapters/kiwoom/client.py backend/tests/kiwoom/test_client.py
git commit -m "feat(kiwoom): http client with pagination and 429/401 retry"
```

---

### Task 6: KiwoomBroker — 현재가 + 일봉 (`adapters/kiwoom/broker.py`)

**Files:**
- Create: `backend/app/adapters/kiwoom/broker.py`
- Modify: `backend/tests/live/test_live_smoke.py` (라이브 케이스 추가)
- Test: `backend/tests/kiwoom/test_broker_market.py`

**Interfaces:**
- Consumes: `KiwoomHttpClient`(Task 5), 도메인 모델(Task 2).
- Produces: `KiwoomBroker(client: KiwoomHttpClient)` —
  `async get_quote(symbol: str) -> Quote` (TR `ka10001`, category `stkinfo`),
  `async get_daily_candles(symbol: str, count: int) -> list[Candle]` (TR `ka10081`,
  category `chart`, 수정주가 적용 `upd_stkpc_tp="1"`, 과거→최신 정렬),
  내부 헬퍼 `_to_int(s: str) -> int`(부호/공백 안전), `_to_price(s: str) -> int`(절대값).
  Task 7이 같은 클래스에 계좌 메서드를 추가하고, Task 8이 lifespan에 연결.
- ⚠️ 응답 필드명은 비공식 — 라이브 스모크(Step 6)에서 실측, 불일치 시 픽스처·매핑 수정.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/kiwoom/test_broker_market.py`:

```python
from datetime import date

import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings
from app.domain.broker import BrokerPort

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# ⚠️ 아래 픽스처 필드명은 비공식 리서치 기반 — 라이브 스모크 실측 후 필요 시 수정
QUOTE_JSON = {"return_code": 0, "return_msg": "ok", "stk_cd": "005930",
              "stk_nm": "삼성전자", "cur_prc": "+71000", "flu_rt": "+1.25",
              "trde_qty": "12345678"}

CANDLE_PAGE = {"return_code": 0, "stk_cd": "005930", "stk_dt_pole_chart_qry": [
    {"dt": "20260717", "open_pric": "70500", "high_pric": "71200",
     "low_pric": "70100", "cur_prc": "71000", "trde_qty": "111"},
    {"dt": "20260716", "open_pric": "70000", "high_pric": "70800",
     "low_pric": "69900", "cur_prc": "70500", "trde_qty": "222"},
    {"dt": "20260715", "open_pric": "69500", "high_pric": "70100",
     "low_pric": "69400", "cur_prc": "70000", "trde_qty": "333"},
]}


async def _noop_sleep(_: float) -> None:
    return None


def _broker() -> KiwoomBroker:
    s = Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                 kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")
    return KiwoomBroker(KiwoomHttpClient(s, sleep=_noop_sleep))


def _mock_auth() -> None:
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_get_quote는_도메인_모델로_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/stkinfo").respond(
        json=QUOTE_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    q = await b.get_quote("005930")
    assert q.symbol == "005930" and q.name == "삼성전자"
    assert q.price == 71000          # 부호 제거
    assert q.change_rate == 1.25
    assert q.volume == 12_345_678
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_daily_candles는_과거_최신_순으로_count개를_반환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/chart").respond(
        json=CANDLE_PAGE, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    candles = await b.get_daily_candles("005930", count=2)
    assert len(candles) == 2
    assert candles[0].date == date(2026, 7, 16)   # 과거가 먼저
    assert candles[1].date == date(2026, 7, 17)
    assert candles[1].close == 71000 and candles[1].volume == 111
    await b.aclose()


def test_KiwoomBroker는_BrokerPort_계약을_만족한다():
    # __new__로 생성해 리소스(httpx client) 없이 클래스 구조만 검사한다
    instance = KiwoomBroker.__new__(KiwoomBroker)
    assert isinstance(instance, BrokerPort) is False  # Task 7 완료 전 — 계좌 메서드 미구현
```

(참고: 마지막 테스트는 Task 7에서 `is False`를 `is True`로 뒤집는다 — 계약 완성 시점을
명시적으로 드러내는 장치.)

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/kiwoom/test_broker_market.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: 최소 구현**

`backend/app/adapters/kiwoom/broker.py`:

```python
"""BrokerPort의 키움 구현. TR id·필드명 등 키움 상세는 이 파일 밖으로 새지 않는다.
응답 필드명은 비공식 자료 기반이며 라이브 스모크로 실측 검증한다 (spec §5)."""

from datetime import datetime

from app.adapters.kiwoom.client import KiwoomHttpClient
from app.domain.broker import Candle, Quote


def _to_int(s: str | None) -> int:
    """키움 숫자 문자열('+71000', '-0000050000', '') → int (부호 보존)."""
    if s is None or not s.strip():
        return 0
    return int(s)


def _to_price(s: str | None) -> int:
    """가격 필드 — 키움은 등락 방향을 부호로 실어 보내므로 절대값을 취한다."""
    return abs(_to_int(s))


class KiwoomBroker:
    def __init__(self, client: KiwoomHttpClient) -> None:
        self._client = client

    async def get_quote(self, symbol: str) -> Quote:
        data, _, _ = await self._client.call("stkinfo", "ka10001", {"stk_cd": symbol})
        return Quote(
            symbol=symbol,
            name=data["stk_nm"],
            price=_to_price(data["cur_prc"]),
            change_rate=float(data["flu_rt"].replace("+", "")) if data.get("flu_rt") else 0.0,
            volume=_to_int(data.get("trde_qty")),
        )

    async def get_daily_candles(self, symbol: str, count: int) -> list[Candle]:
        body = {"stk_cd": symbol, "base_dt": "", "upd_stkpc_tp": "1"}  # 1=수정주가 적용
        rows: list[dict] = []
        async for page in self._client.call_paged("chart", "ka10081", body):
            rows.extend(page.get("stk_dt_pole_chart_qry") or [])
            if len(rows) >= count:
                break
        rows = rows[:count]  # 응답은 최신→과거 순
        candles = [
            Candle(
                symbol=symbol,
                date=datetime.strptime(r["dt"], "%Y%m%d").date(),
                open=_to_price(r["open_pric"]),
                high=_to_price(r["high_pric"]),
                low=_to_price(r["low_pric"]),
                close=_to_price(r["cur_prc"]),
                volume=_to_int(r.get("trde_qty")),
            )
            for r in rows
        ]
        candles.sort(key=lambda c: c.date)  # 과거→최신
        return candles

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (26 + 3 = 29 passed).

- [ ] **Step 5: 라이브 스모크 추가**

`backend/tests/live/test_live_smoke.py`에 추가 (기존 import에 `KiwoomBroker`,
`KiwoomHttpClient` 추가):

```python
from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient


@pytest.mark.anyio
async def test_live_삼성전자_현재가(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        q = await b.get_quote("005930")
        assert q.name and q.price > 0
        print(f"[live] 005930 {q.name} price={q.price} rate={q.change_rate}")
    finally:
        await b.aclose()


@pytest.mark.anyio
async def test_live_삼성전자_일봉_5개(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        candles = await b.get_daily_candles("005930", count=5)
        assert len(candles) == 5
        assert candles[0].date < candles[-1].date  # 과거→최신
        assert all(c.high >= c.low > 0 for c in candles)
    finally:
        await b.aclose()
```

- [ ] **Step 6: 라이브 스모크 실행**

Run: `cd backend && uv run pytest -m live -v`
Expected: PASS. **필드명 KeyError 등 불일치 시**: 실제 응답의 키 목록을 보고서에 기록
→ 픽스처(QUOTE_JSON/CANDLE_PAGE)와 broker.py 매핑을 실측값으로 수정 → 단위+라이브
재실행 → 변경 내용을 보고서에 "실측 정정"으로 명시.

- [ ] **Step 7: 커밋**

```bash
git add backend/app/adapters/kiwoom/broker.py backend/tests/kiwoom/test_broker_market.py backend/tests/live/test_live_smoke.py
git commit -m "feat(kiwoom): quote and daily candle queries"
```

---

### Task 7: KiwoomBroker — 예수금 + 계좌잔고

**Files:**
- Modify: `backend/app/adapters/kiwoom/broker.py` (메서드 2개 추가)
- Modify: `backend/tests/kiwoom/test_broker_market.py` (계약 테스트 `is False`→`is True`)
- Modify: `backend/tests/live/test_live_smoke.py` (라이브 케이스 추가)
- Test: `backend/tests/kiwoom/test_broker_account.py`

**Interfaces:**
- Consumes: Task 6의 `KiwoomBroker`, 도메인 모델(Task 2).
- Produces: `async get_deposit() -> Deposit` (TR `kt00001`, category `acnt`),
  `async get_balance() -> Balance` (TR `kt00018`, category `acnt`,
  body `{"qry_tp": "1", "dmst_stex_tp": "KRX"}`). 이로써 `BrokerPort` 계약 완성.
- ⚠️ 응답 필드명 비공식 — 라이브 스모크로 실측.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/kiwoom/test_broker_account.py`:

```python
import pytest
import respx

from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.core.config import Settings

BASE = "https://mockapi.kiwoom.com"
TOKEN_JSON = {"token": "TOK", "token_type": "bearer",
              "expires_dt": "20991231235959", "return_code": 0, "return_msg": "ok"}

# ⚠️ 비공식 필드명 — 라이브 실측 후 필요 시 수정
DEPOSIT_JSON = {"return_code": 0, "entr": "000001000000", "ord_alow_amt": "000000900000"}
BALANCE_JSON = {"return_code": 0, "tot_evlt_amt": "000000710000",
                "tot_evlt_pl": "-000000020000",
                "acnt_evlt_remn_indv_tot": [
                    {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "10",
                     "pur_pric": "69000", "cur_prc": "+71000",
                     "evlt_amt": "000000710000"}]}


async def _noop_sleep(_: float) -> None:
    return None


def _broker() -> KiwoomBroker:
    s = Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                 kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")
    return KiwoomBroker(KiwoomHttpClient(s, sleep=_noop_sleep))


def _mock_auth() -> None:
    respx.post(f"{BASE}/oauth2/token").respond(json=TOKEN_JSON)
    respx.post(f"{BASE}/oauth2/revoke").respond(json={"return_code": 0})


@pytest.mark.anyio
@respx.mock
async def test_get_deposit는_정수_원단위로_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json=DEPOSIT_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    d = await b.get_deposit()
    assert d.total == 1_000_000 and d.available == 900_000
    await b.aclose()


@pytest.mark.anyio
@respx.mock
async def test_get_balance는_포지션과_손익을_변환한다():
    _mock_auth()
    respx.post(f"{BASE}/api/dostk/acnt").respond(
        json=BALANCE_JSON, headers={"cont-yn": "N", "next-key": ""})
    b = _broker()
    bal = await b.get_balance()
    assert bal.total_eval == 710_000
    assert bal.total_profit == -20_000          # 음수 보존
    p = bal.positions[0]
    assert p.symbol == "005930"                 # 'A' 접두 제거
    assert p.quantity == 10 and p.avg_price == 69_000 and p.current_price == 71_000
    await b.aclose()
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/kiwoom/test_broker_account.py -v`
Expected: FAIL — `AttributeError: 'KiwoomBroker' object has no attribute 'get_deposit'`.

- [ ] **Step 3: 구현**

`backend/app/adapters/kiwoom/broker.py` — import에 `Balance, Deposit, Position` 추가:

```python
from app.domain.broker import Balance, Candle, Deposit, Position, Quote
```

클래스에 메서드 추가:

```python
    async def get_deposit(self) -> Deposit:
        data, _, _ = await self._client.call("acnt", "kt00001", {"qry_tp": "3"})
        return Deposit(
            total=_to_int(data.get("entr")),
            available=_to_int(data.get("ord_alow_amt")),
        )

    async def get_balance(self) -> Balance:
        data, _, _ = await self._client.call(
            "acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        positions = [
            Position(
                symbol=row["stk_cd"].removeprefix("A"),
                name=row["stk_nm"],
                quantity=_to_int(row.get("rmnd_qty")),
                avg_price=_to_price(row.get("pur_pric")),
                current_price=_to_price(row.get("cur_prc")),
                eval_amount=_to_int(row.get("evlt_amt")),
            )
            for row in data.get("acnt_evlt_remn_indv_tot") or []
        ]
        return Balance(
            positions=positions,
            total_eval=_to_int(data.get("tot_evlt_amt")),
            total_profit=_to_int(data.get("tot_evlt_pl")),
        )
```

`backend/tests/kiwoom/test_broker_market.py`의 계약 테스트를 갱신:

```python
def test_KiwoomBroker는_BrokerPort_계약을_만족한다():
    instance = KiwoomBroker.__new__(KiwoomBroker)
    assert isinstance(instance, BrokerPort) is True  # Task 7에서 계약 완성
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (29 + 2 = 31 passed).

- [ ] **Step 5: 라이브 스모크 추가 + 실행**

`backend/tests/live/test_live_smoke.py`에 추가:

```python
@pytest.mark.anyio
async def test_live_예수금과_잔고(settings):
    b = KiwoomBroker(KiwoomHttpClient(settings))
    try:
        d = await b.get_deposit()
        assert d.total >= 0 and d.available >= 0
        bal = await b.get_balance()
        assert bal.total_eval >= 0
        print(f"[live] deposit={d.total} positions={len(bal.positions)}")
    finally:
        await b.aclose()
```

Run: `cd backend && uv run pytest -m live -v`
Expected: PASS (모의계좌 초기 상태면 포지션 0개도 정상). 불일치 시 Task 6 Step 6과
동일한 실측 정정 절차.

- [ ] **Step 6: 커밋**

```bash
git add backend/app/adapters/kiwoom/broker.py backend/tests/kiwoom/test_broker_account.py backend/tests/kiwoom/test_broker_market.py backend/tests/live/test_live_smoke.py
git commit -m "feat(kiwoom): deposit and balance queries"
```

---

### Task 8: 앱 수명주기 통합 (`main.py` lifespan)

**Files:**
- Modify: `backend/app/main.py` (lifespan에 broker 생성/종료)
- Test: `backend/tests/test_app_lifespan.py`

**Interfaces:**
- Consumes: `create_app`(Phase 0), `KiwoomBroker`/`KiwoomHttpClient`(Task 5~7).
- Produces: `app.state.broker: KiwoomBroker` — Phase 2(수집 배치)가 이걸 사용.
  브로커 생성은 네트워크 호출 없음(토큰은 첫 사용 시 lazy 발급) — 앱 기동이 키움
  장애와 무관하게 성공해야 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_app_lifespan.py`:

```python
from fastapi.testclient import TestClient

from app.adapters.kiwoom.broker import KiwoomBroker
from app.core.config import Settings
from app.main import create_app


def _settings() -> Settings:
    return Settings(_env_file=None, kiwoom_app_key="AK", kiwoom_secret_key="SK",
                    kiwoom_mock=True, database_url="sqlite+pysqlite:///:memory:")


def test_lifespan이_broker를_생성하고_보관한다():
    app = create_app(_settings())
    with TestClient(app):
        assert isinstance(app.state.broker, KiwoomBroker)
```

- [ ] **Step 2: 실패 확인**

Run: `cd backend && uv run pytest tests/test_app_lifespan.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'broker'`.

- [ ] **Step 3: 구현**

`backend/app/main.py` — import 추가:

```python
from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
```

lifespan 수정 (기존 engine 로직 유지, broker 추가):

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = create_db_engine(settings)
        app.state.broker = KiwoomBroker(KiwoomHttpClient(settings))
        yield
        await app.state.broker.aclose()
        app.state.engine.dispose()
```

- [ ] **Step 4: 통과 확인 (전체 회귀)**

Run: `cd backend && uv run pytest -v`
Expected: PASS (31 + 1 = 32 passed).
주의: 종료 시 `aclose()`가 revoke를 호출하는데 테스트에선 토큰 미발급이라 no-op —
`TokenManager.revoke()`의 `if self._token is None: return` 경로가 이를 보장.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/main.py backend/tests/test_app_lifespan.py
git commit -m "feat(backend): broker lifecycle in app lifespan"
```

---

### Task 9: 실측 팩트 반영 + 회고록 + STATUS 핸드오프

**Files:**
- Modify: `CLAUDE.md` §5 (라이브 스모크로 실측 확인/정정된 팩트 반영)
- Create: `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`
- Modify: `docs/STATUS.md` (Phase 2 핸드오프)
- Modify: `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md`
  (§5 표의 "실측 필요" 항목에 실측 결과 기입 — 상태 갱신)

**Interfaces:**
- Consumes: Task 3~8의 보고서(실측 정정 내역 포함), 진행 원장.
- Produces: Phase 1 완료 선언 + Phase 2(데이터 수집) 재개 지점.

- [ ] **Step 1: 최종 검증**

```bash
cd backend && uv run pytest -v          # 전체 그린 (32개)
uv run pytest -m live -v                # 라이브 스모크 전체 그린
```
하나라도 실패하면 완료 주장 금지 — 수정 후 재검증.

- [ ] **Step 2: CLAUDE.md §5 갱신**

라이브 스모크에서 실측된 사실(토큰 응답 필드, TR 요청/응답 필드명, 레이트리밋 체감
수치, 429 응답 형태 — Task 3~7 보고서의 "실측 정정" 내역)을 §5에 반영. 비공식 표기가
실측으로 확정된 항목은 "verified live against mock server (2026-07-17)"로 명시.

- [ ] **Step 3: 회고록 작성**

`docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md` — 규칙 4 형식
(요청 내용 / 기존 코드 상태 / 태스크별 구현·파일·커밋 SHA / 설계·패턴: 포트-어댑터,
토큰버킷, 지수 백오프, lazy 토큰 / 실측 정정 내역 / 4-에이전트 패널 리뷰 결과 요약 /
남은 항목). 한국어, 비전문가 기준.

- [ ] **Step 4: STATUS.md 갱신**

재개 지점 → Phase 2(데이터 수집 파이프라인) spec 브레인스토밍. 워크플로 체크리스트
Phase 1 완료 표시. spec §5 표의 실측 상태도 이 시점에 함께 갱신.

- [ ] **Step 5: 커밋**

```bash
git add CLAUDE.md docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md docs/STATUS.md docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md
git commit -m "docs: phase 1 retrospective + verified kiwoom facts + status handoff"
```
