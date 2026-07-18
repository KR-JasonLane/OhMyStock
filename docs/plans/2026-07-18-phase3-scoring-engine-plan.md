# Phase 3 스코어링 엔진 구현 계획서

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 수집된 일봉·업종 데이터를 입력으로 섹터 로테이션 + 3전략(모멘텀·눌림목·돌파) 혼합 스코어링을 자정 배치로 수행해 상위 N 매수 후보를 산출한다.

**Architecture:** 데이터 기반 정비(업종 소속 다대다 + 분류, 종목 상태 필드) 후, 순수 파이썬 도메인 엔진(`domain/scoring/`)이 DB에서 읽은 데이터만으로 계산하고 결과를 신규 4테이블에 저장한다. 오케스트레이션은 CollectionService와 동일한 서비스 패턴, API는 `POST /score` 계열 3개.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy(sync)+Alembic, pytest. **신규 런타임 의존성 0개.**

**Spec:** `docs/specs/2026-07-18-phase3-scoring-engine-design.md` (수치·조건의 단일 출처)

## Global Constraints

- 커밋 메시지에 AI 흔적 금지 (`Co-Authored-By: Claude` 등 트레일러 금지 — CLAUDE.md 규칙 7). 커밋 메시지는 사전 일괄 승인분을 그대로 사용.
- 태스크마다 4-에이전트 패널 리뷰(senior-developer/senior-trader/architecture-expert/security-expert) 전원 통과 후 다음 태스크 진행 (규칙 8). 패널 디스패치는 **코디네이터 전담** — 구현자는 절대 자체 실행하지 않는다.
- 테스트 실행 출력은 반드시 파일로 캡처(`> ../.superpowers/sdd/p3-task-N-<이름>.txt 2>&1`) 후 결과를 보고한다. 증거 파일 접두는 `p3-task-N-`.
- 새 런타임 의존성 추가 금지 (pandas/numpy 금지 — 스펙 결정 #6).
- store는 sqlite(테스트)/postgresql(운영) 양쪽 지원 — 기존 `_upsert` dialect 분기 패턴 유지.
- 도메인 계층(`domain/`)은 어댑터/SQL을 임포트하지 않는다 (헥사고날 원칙).
- `.env` 값 출력 금지. 라이브 서버 호출 없음 (Phase 3은 DB만 읽음).
- 모든 스코어링 파라미터는 `ScoringConfig` 기본값 — 하드코딩 금지 (스펙 §4-6).
- 문서는 한국어 (CLAUDE.md 규칙 1).

## 파일 구조 (신규/수정 총괄)

```
backend/app/
  domain/
    broker.py                    # 수정: Instrument에 state/audit_info 추가
    sector_classification.py     # 신규: 65개 코드 → group_type 분류 맵
    collection.py                # 수정: 집계 필터/캐너리 제거, 멤버십 저장
    scoring/
      __init__.py                # 신규
      config.py                  # 신규: ScoringConfig
      indicators.py              # 신규: 이동평균·수익률·최고가·평균거래량
      strategies.py              # 신규: Strategy 프로토콜 + 3전략
      simulation.py              # 신규: 적합도 시뮬레이션
      engine.py                  # 신규: run_scoring 순수 계산
      service.py                 # 신규: ScoringService 오케스트레이션
  adapters/kiwoom/broker.py      # 수정: ka10099 state/auditInfo 매핑
  store/
    models.py                    # 수정: 멤버십·상태·스코어링 4테이블
    collection_store.py          # 수정: 멤버십 교체, set_sector_codes 제거
    scoring_store.py             # 신규: 스코어링 조회/저장
  api/score.py                   # 신규: POST /score, status, latest
  main.py                        # 수정: ScoringService 조립
backend/alembic/versions/
  0003_sector_memberships.py     # 신규
  0004_scoring_tables.py         # 신규
```

Task 1~3 = 데이터 기반 정비 (스펙 §3), Task 4~7 = 스코어링 (스펙 §4~7),
Task 8 = 실데이터 수용 검증 (스펙 §8).

---

### Task 1: 도메인 확장 — Instrument 상태 필드 + 업종 분류 맵 + 어댑터 매핑

**Files:**
- Modify: `backend/app/domain/broker.py` (Instrument dataclass)
- Create: `backend/app/domain/sector_classification.py`
- Modify: `backend/app/adapters/kiwoom/broker.py` (`list_instruments`의 행 매핑)
- Test: `backend/tests/test_sector_classification.py` (신규), `backend/tests/kiwoom/test_broker_catalog.py` (추가)

**Interfaces:**
- Consumes: 기존 `Instrument` frozen dataclass, `KiwoomBroker.list_instruments`.
- Produces: `Instrument.state: str = ""`, `Instrument.audit_info: str = ""` (기본값 있어 기존 호출부 무해). `sector_classification.classify_sector(code: str) -> str` — 반환값은 `"industry" | "industry_umbrella" | "aggregate" | "size" | "quality" | "index" | "unclassified"`. 상수 `INDUSTRY = "industry"`, `UNCLASSIFIED = "unclassified"`.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_sector_classification.py` (신규):

```python
"""업종 분류 맵 검증 — 2026-07-18 65개 전수 실측 근거
(.superpowers/sdd/p3-pregate-sectors-paged.txt, 스펙 §3-2)."""

from app.domain.sector_classification import (INDUSTRY, UNCLASSIFIED,
                                              classify_sector)


def test_집계_규모_등급_지수_분류():
    assert classify_sector("001") == "aggregate"
    assert classify_sector("101") == "aggregate"
    assert classify_sector("002") == "size"       # 대형주
    assert classify_sector("140") == "size"       # KOSDAQ SMALL
    assert classify_sector("142") == "quality"    # 코스닥 우량기업
    assert classify_sector("150") == "index"      # KOSDAQ 150
    assert classify_sector("603") == "index"      # 변동성지수


def test_우산_업종():
    assert classify_sector("021") == "industry_umbrella"  # kospi 금융
    assert classify_sector("027") == "industry_umbrella"  # kospi 제조
    assert classify_sector("106") == "industry_umbrella"  # kosdaq 제조(시장 61%)


def test_산업_업종():
    for code in ("005", "008", "013", "024", "030", "103", "120", "141"):
        assert classify_sector(code) == INDUSTRY


def test_미지_코드는_unclassified():
    assert classify_sector("999") == UNCLASSIFIED


def test_분류_전수_개수():
    """실측 65개 코드가 전부 맵에 있고 industry는 kospi 22 + kosdaq 21."""
    from app.domain.sector_classification import _CLASSIFICATION
    assert len(_CLASSIFICATION) == 65
    assert sum(1 for v in _CLASSIFICATION.values() if v == INDUSTRY) == 43


def test_instrument_상태_필드_기본값():
    from app.domain.broker import Instrument
    i = Instrument(symbol="005930", name="삼성전자", market="kospi",
                   instrument_type="A")
    assert i.state == "" and i.audit_info == ""
```

`backend/tests/kiwoom/test_broker_catalog.py`에 추가 (기존 목킹 헬퍼/픽스처 스타일을 그 파일에서 확인해 동일하게 사용):

```python
async def test_list_instruments_상태_필드_매핑(...기존 픽스처):
    # 기존 ka10099 목 응답 행에 state/auditInfo 추가:
    # {"code": "005930", "name": "삼성전자", "marketCode": "0", "kind": "A",
    #  "state": "증거금20%|담보대출|신용가능", "auditInfo": "정상", ...}
    instruments = await broker.list_instruments("kospi")
    assert instruments[0].state == "증거금20%|담보대출|신용가능"
    assert instruments[0].audit_info == "정상"
```

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/test_sector_classification.py -v > ../.superpowers/sdd/p3-task-1-red.txt 2>&1
```
기대: FAIL — `ModuleNotFoundError: app.domain.sector_classification`.

- [ ] **Step 3: 구현**

`backend/app/domain/broker.py`의 `Instrument`:

```python
@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str           # "kospi" | "kosdaq" | "etf"
    instrument_type: str  # 브로커가 주는 구분값 원문
    state: str = ""       # ka10099 state 원문 (예: "증거금100%|거래정지")
    audit_info: str = ""  # ka10099 auditInfo 원문 (예: "정상", "관리종목")
```

`backend/app/domain/sector_classification.py` (신규, 전문):

```python
"""키움 업종코드 → 그룹 분류. 2026-07-18 65개 전수 실측 근거
(.superpowers/sdd/p3-pregate-sectors-paged.txt, 스펙 §3-2).

키움 ka10101 업종 목록은 산업 분류 외에 규모·등급·지수·집계 그룹이 섞여 있고
한 종목이 여러 그룹에 중복 소속된다. 스코어링(섹터 로테이션)은 industry만
소비한다. industry_umbrella(금융⊇증권·보험, 제조⊇제조 하위업종)는 하위
업종과 중복 집계를 피하기 위해 로테이션에서 제외한다."""

INDUSTRY = "industry"
UNCLASSIFIED = "unclassified"

_CLASSIFICATION: dict[str, str] = {
    # 집계 (시장 전체)
    "001": "aggregate", "101": "aggregate",
    # 규모
    "002": "size", "003": "size", "004": "size",          # 대·중·소형주
    "138": "size", "139": "size", "140": "size",          # KOSDAQ 100/MID/SMALL
    # 등급 (코스닥 소속부)
    "142": "quality", "143": "quality", "144": "quality", "145": "quality",
    # 지수 멤버십
    "603": "index", "604": "index", "605": "index",       # 변동성/고배당/배당성장
    "150": "index", "151": "index",                        # KOSDAQ150/글로벌지수
    "160": "index", "165": "index",                        # F-KOSDAQ150(인버스)
    # 우산 산업 (하위 업종 포함 — 중복 집계 방지 위해 로테이션 제외)
    "021": "industry_umbrella",   # kospi 금융 (⊇ 증권 024, 보험 025)
    "027": "industry_umbrella",   # kospi 제조 (실측 557명)
    "106": "industry_umbrella",   # kosdaq 제조 (실측 1,116명 = 시장 61%)
    # 산업 — kospi 22개
    "005": INDUSTRY, "006": INDUSTRY, "007": INDUSTRY, "008": INDUSTRY,
    "009": INDUSTRY, "010": INDUSTRY, "011": INDUSTRY, "012": INDUSTRY,
    "013": INDUSTRY, "014": INDUSTRY, "015": INDUSTRY, "016": INDUSTRY,
    "017": INDUSTRY, "018": INDUSTRY, "019": INDUSTRY, "020": INDUSTRY,
    "024": INDUSTRY, "025": INDUSTRY, "026": INDUSTRY, "028": INDUSTRY,
    "029": INDUSTRY, "030": INDUSTRY,
    # 산업 — kosdaq 21개
    "103": INDUSTRY, "107": INDUSTRY, "108": INDUSTRY, "110": INDUSTRY,
    "111": INDUSTRY, "115": INDUSTRY, "116": INDUSTRY, "117": INDUSTRY,
    "118": INDUSTRY, "119": INDUSTRY, "120": INDUSTRY, "121": INDUSTRY,
    "122": INDUSTRY, "123": INDUSTRY, "124": INDUSTRY, "125": INDUSTRY,
    "126": INDUSTRY, "127": INDUSTRY, "128": INDUSTRY, "129": INDUSTRY,
    "141": INDUSTRY,
}


def classify_sector(code: str) -> str:
    """업종코드의 그룹 분류. 미지 코드는 UNCLASSIFIED (소비 제외 + 경고는
    호출자 책임 — CollectionService 참고)."""
    return _CLASSIFICATION.get(code, UNCLASSIFIED)
```

`backend/app/adapters/kiwoom/broker.py` — `list_instruments`의 `Instrument(...)` 생성부에 두 필드 추가 (원문 그대로, 없으면 빈 문자열):

```python
items.append(Instrument(
    symbol=_normalize_symbol(row["code"]), name=row["name"],
    market=market, instrument_type=row.get("kind") or "",
    state=(row.get("state") or "").strip(),
    audit_info=(row.get("auditInfo") or "").strip()))
```
(기존 생성부의 필드 구성/스타일을 유지한 채 두 인자만 추가한다.)

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-1-green.txt 2>&1
```
기대: 전체 PASS (기존 103 + 신규 7 내외, live 마커 deselect).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/broker.py backend/app/domain/sector_classification.py backend/app/adapters/kiwoom/broker.py backend/tests/test_sector_classification.py backend/tests/kiwoom/test_broker_catalog.py
git commit -m "feat(domain): instrument state fields + sector group classification"
```

---

### Task 2: 스키마 마이그레이션 0003 + store 멤버십 교체

**Files:**
- Create: `backend/alembic/versions/0003_sector_memberships.py`
- Modify: `backend/app/store/models.py`
- Modify: `backend/app/store/collection_store.py`
- Test: `backend/tests/store/test_collection_store.py` (수정·추가), `backend/tests/store/test_models_migration.py` (기존 스타일 따라 0003 반영)

**Interfaces:**
- Consumes: Task 1의 `Instrument.state/audit_info`.
- Produces:
  - `SectorMembershipRow(sector_code: str, symbol: str)` — PK(sector_code, symbol)
  - `SectorRow.group_type: str` (기본 "unclassified")
  - `InstrumentRow`: `sector_code` **삭제**, `state: str`/`audit_info: str` 추가
  - `CollectionStore.upsert_sectors(self, sectors: Iterable[Sector], group_types: dict[str, str]) -> None`
  - `CollectionStore.replace_sector_memberships(self, memberships: dict[str, list[str]]) -> int` — 전체 삭제 후 삽입, instruments에 없는 symbol은 건너뛰고 경고, 삽입 행 수 반환
  - `CollectionStore.set_sector_codes` **제거** (소비자는 Task 3에서 함께 전환)

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/store/test_collection_store.py`에 추가/수정 (기존 파일의 엔진/스토어 픽스처 재사용 — sqlite in-memory에 `Base.metadata.create_all` 하는 기존 방식):

```python
def test_멤버십_전체_교체(store, ...):
    # 사전: instruments에 A0001, A0002 upsert / sectors에 005, 013 upsert
    store.upsert_sectors([Sector("005", "kospi", "음식료/담배"),
                          Sector("013", "kospi", "전기/전자")],
                         group_types={"005": "industry", "013": "industry"})
    store.upsert_instruments([
        Instrument("A0001", "가", "kospi", "A", state="증거금100%",
                   audit_info="정상"),
        Instrument("A0002", "나", "kospi", "A")])
    n = store.replace_sector_memberships(
        {"005": ["A0001"], "013": ["A0001", "A0002", "ZZZZ9"]})
    assert n == 3  # ZZZZ9는 미등록 → 스킵
    # 재호출은 이전 소속을 남기지 않는다 (전체 교체)
    n2 = store.replace_sector_memberships({"005": ["A0002"]})
    assert n2 == 1
    with store._sessions() as s:  # 또는 조회 메서드로 검증
        rows = s.execute(select(SectorMembershipRow)).scalars().all()
        assert [(r.sector_code, r.symbol) for r in rows] == [("005", "A0002")]


def test_instrument_상태_저장(store):
    store.upsert_instruments([Instrument("A0001", "가", "kospi", "A",
                                         state="관리종목", audit_info="관리종목")])
    with store._sessions() as s:
        row = s.get(InstrumentRow, "A0001")
        assert row.state == "관리종목" and row.audit_info == "관리종목"


def test_sectors_group_type_저장(store):
    store.upsert_sectors([Sector("001", "kospi", "종합(KOSPI)")],
                         group_types={"001": "aggregate"})
    with store._sessions() as s:
        assert s.get(SectorRow, "001").group_type == "aggregate"
```

기존 `set_sector_codes` 관련 테스트는 **삭제**한다 (메서드 제거에 따라).
`test_models_migration.py`는 기존 방식대로 alembic upgrade 후 신규 테이블/칼럼
존재와 `instruments.sector_code` 부재를 검증하는 케이스를 추가한다.

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/store -v > ../.superpowers/sdd/p3-task-2-red.txt 2>&1
```
기대: FAIL — `SectorMembershipRow` 미정의, `replace_sector_memberships` 부재.

- [ ] **Step 3: 구현**

`backend/alembic/versions/0003_sector_memberships.py` (신규, 전문):

```python
"""sector memberships + instrument status fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sector_memberships",
        sa.Column("sector_code", sa.String(8), sa.ForeignKey("sectors.code"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), sa.ForeignKey("instruments.symbol"),
                  primary_key=True),
    )
    op.add_column("sectors", sa.Column("group_type", sa.String(24),
                                       nullable=False,
                                       server_default="unclassified"))
    op.add_column("instruments", sa.Column("state", sa.String(128),
                                           nullable=False, server_default=""))
    op.add_column("instruments", sa.Column("audit_info", sa.String(32),
                                           nullable=False, server_default=""))
    # sector_code는 last-write-wins로 손상된 라벨 (2026-07-18 실측) — 소비자
    # 없는 지금 제거. batch_alter_table은 sqlite(테스트) 호환용.
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("sector_code")


def downgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.add_column(sa.Column("sector_code", sa.String(8),
                                   sa.ForeignKey("sectors.code"), nullable=True))
    op.drop_column("instruments", "audit_info")
    op.drop_column("instruments", "state")
    op.drop_column("sectors", "group_type")
    op.drop_table("sector_memberships")
```

`backend/app/store/models.py` 변경:

```python
class SectorRow(Base):
    __tablename__ = "sectors"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    group_type: Mapped[str] = mapped_column(
        String(24), default="unclassified", server_default="unclassified")


class SectorMembershipRow(Base):
    __tablename__ = "sector_memberships"
    sector_code: Mapped[str] = mapped_column(
        String(8), ForeignKey("sectors.code"), primary_key=True)
    symbol: Mapped[str] = mapped_column(
        String(12), ForeignKey("instruments.symbol"), primary_key=True)


class InstrumentRow(Base):
    __tablename__ = "instruments"
    symbol: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(16))
    instrument_type: Mapped[str] = mapped_column(String(32), default="", server_default="")
    state: Mapped[str] = mapped_column(String(128), default="", server_default="")
    audit_info: Mapped[str] = mapped_column(String(32), default="", server_default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=literal(True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

`backend/app/store/collection_store.py` 변경 (임포트에 `delete`, `SectorMembershipRow` 추가):

```python
    def upsert_sectors(self, sectors: Iterable[Sector],
                       group_types: dict[str, str]) -> None:
        """group_types: code → group_type (도메인 분류 맵이 결정 — store는 무지)."""
        rows = [{"code": s.code, "market": s.market, "name": s.name,
                 "group_type": group_types.get(s.code, "unclassified")}
                for s in sectors]
        with self._sessions.begin() as session:
            _upsert(session, SectorRow, rows, ["code"])

    def upsert_instruments(self, instruments: Iterable[Instrument]) -> None:
        now = self._now()
        rows = [{"symbol": i.symbol, "name": i.name, "market": i.market,
                 "instrument_type": i.instrument_type, "state": i.state,
                 "audit_info": i.audit_info, "is_active": True,
                 "updated_at": now} for i in instruments]
        with self._sessions.begin() as session:
            _upsert(session, InstrumentRow, rows, ["symbol"])

    def replace_sector_memberships(self, memberships: dict[str, list[str]]) -> int:
        """업종 소속 전체 교체 (delete-and-insert, 단일 트랜잭션).

        전체 교체인 이유: 소속은 편출입이 있는 스냅샷 데이터라 이전 실행의
        소속이 남으면 안 된다. instruments에 없는 symbol은 스킵하고 경고
        (FK 위반 방지 — 정규화 차이/신규 상장 타이밍). 반환: 삽입 행 수."""
        with self._sessions.begin() as session:
            all_symbols = {s for members in memberships.values() for s in members}
            known = set(session.scalars(
                select(InstrumentRow.symbol)
                .where(InstrumentRow.symbol.in_(all_symbols))))
            unknown = len(all_symbols - known)
            if unknown:
                logger.warning(
                    "sector memberships skipped for %d unknown symbols", unknown)
            rows = [{"sector_code": code, "symbol": s}
                    for code, members in memberships.items()
                    for s in members if s in known]
            session.execute(delete(SectorMembershipRow))
            if rows:
                session.execute(SectorMembershipRow.__table__.insert(), rows)
            return len(rows)
```

`set_sector_codes` 메서드는 삭제한다. (같은 태스크에서 소비자까지 다 못 바꾸면
빌드가 깨지므로 — 소비자 전환은 Task 3. 이 태스크에서는 `domain/collection.py`의
호출부가 아직 남아 있으면 안 된다 → **주의: Task 2와 Task 3은 같은 브랜치에서
연속 실행되며, Task 2 시점에는 collection.py가 아직 set_sector_codes를 호출한다.
따라서 Task 2에서는 메서드를 제거하지 말고 deprecated 주석만 달고, 실제 제거는
Task 3에서 수행한다.** 테스트도 Task 3에서 삭제한다.)

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-2-green.txt 2>&1
```
기대: 전체 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/alembic/versions/0003_sector_memberships.py backend/app/store/models.py backend/app/store/collection_store.py backend/tests/store/
git commit -m "feat(store): sector memberships table + instrument status columns"
```

---

### Task 3: CollectionService — 전 그룹 멤버십 저장 + 분류 적용

**Files:**
- Modify: `backend/app/domain/collection.py`
- Modify: `backend/app/store/collection_store.py` (`set_sector_codes` 최종 제거)
- Test: `backend/tests/test_collection_service.py` (수정·추가)

**Interfaces:**
- Consumes: Task 1 `classify_sector`/`UNCLASSIFIED`, Task 2 `upsert_sectors(sectors, group_types)`/`replace_sector_memberships`.
- Produces: sectors 단계의 새 동작 — 필터 없이 65개 그룹 전부의 멤버를 수집·저장. `_AGGREGATE_SECTOR_CODES`/`_AGGREGATE_SECTOR_NAME_MARKERS`/`_CANARY_SHARE_THRESHOLD`/`_warn_if_sector_mapping_skewed` **제거**.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/test_collection_service.py` — 기존 fake broker/store 픽스처를 이 파일에서 확인해 동일 스타일로. 기존의 집계 필터/캐너리 테스트는 삭제하고 다음으로 대체:

```python
async def test_전_그룹_멤버십_저장():
    """집계 업종 포함 모든 그룹의 소속이 그대로 저장된다 (필터 없음)."""
    broker = FakeBroker(
        sectors=[Sector("001", "kospi", "종합(KOSPI)"),
                 Sector("005", "kospi", "음식료/담배")],
        members={("001", "kospi"): ["A0001", "A0002"],
                 ("005", "kospi"): ["A0001"]})
    store = FakeStore(...)
    service = CollectionService(broker, store, ...)
    await service.run()
    assert store.saved_memberships == {"001": ["A0001", "A0002"],
                                       "005": ["A0001"]}
    assert store.saved_group_types == {"001": "aggregate", "005": "industry"}


async def test_미지_업종코드_경고(caplog):
    """분류 맵에 없는 코드는 unclassified로 저장되고 경고 로그를 남긴다."""
    broker = FakeBroker(sectors=[Sector("777", "kospi", "신설업종")],
                        members={("777", "kospi"): ["A0001"]})
    ...
    await service.run()
    assert store.saved_group_types == {"777": "unclassified"}
    assert any("unclassified sector" in r.message for r in caplog.records)
```

FakeStore에는 `upsert_sectors(sectors, group_types)`/`replace_sector_memberships(memberships)`를 기록하는 스텁을 추가하고, `set_sector_codes` 스텁과 관련 단언은 삭제한다.

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/test_collection_service.py -v > ../.superpowers/sdd/p3-task-3-red.txt 2>&1
```
기대: FAIL — 서비스가 아직 구 방식(mapping/set_sector_codes) 호출.

- [ ] **Step 3: 구현**

`backend/app/domain/collection.py` — 임포트에 `from app.domain.sector_classification import UNCLASSIFIED, classify_sector` 추가. 모듈 상수 `_AGGREGATE_SECTOR_NAME_MARKERS`/`_AGGREGATE_SECTOR_CODES`/`_CANARY_SHARE_THRESHOLD`와 `_warn_if_sector_mapping_skewed`, `Counter` 임포트 제거. sectors 단계(기존 161~177행)를 다음으로 교체:

```python
            self._set(run_id, "running", "sectors", 0, 0, 0)
            sectors = await self._broker.list_sectors()
            group_types = {s.code: classify_sector(s.code) for s in sectors}
            for s in sectors:
                if group_types[s.code] == UNCLASSIFIED:
                    # 분류 맵(2026-07-18 실측 65개)에 없는 신설 코드 — 소비
                    # 제외되므로 동작은 안전하나 맵 갱신이 필요하다는 신호.
                    logger.warning("unclassified sector code %s (%s) - "
                                   "update sector_classification map",
                                   s.code, s.name)
            await asyncio.to_thread(self._store.upsert_sectors, sectors,
                                    group_types)
            memberships: dict[str, list[str]] = {}
            for sector in sectors:
                memberships[sector.code] = await self._broker.list_sector_members(
                    sector.code, sector.market)
            n = await asyncio.to_thread(
                self._store.replace_sector_memberships, memberships)
            logger.info("stored %d sector membership rows across %d groups",
                        n, len(sectors))
```

`backend/app/store/collection_store.py`에서 `set_sector_codes`와 그 테스트를 이 시점에 최종 삭제한다.

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-3-green.txt 2>&1
```
기대: 전체 PASS. `grep -rn "set_sector_codes" backend/app backend/tests` 결과 0건.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/collection.py backend/app/store/collection_store.py backend/tests/test_collection_service.py backend/tests/store/test_collection_store.py
git commit -m "feat(collection): store all sector memberships with group classification"
```

---

### Task 4: ScoringConfig + 지표 함수

**Files:**
- Create: `backend/app/domain/scoring/__init__.py` (빈 파일)
- Create: `backend/app/domain/scoring/config.py`
- Create: `backend/app/domain/scoring/indicators.py`
- Test: `backend/tests/scoring/__init__.py` (빈 파일), `backend/tests/scoring/test_indicators.py`

**Interfaces:**
- Produces:
  - `ScoringConfig` frozen dataclass — 필드/기본값은 아래 전문 그대로 (스펙 §4-6과 1:1). `to_json() -> str` 포함.
  - 지표 함수 5개 — 모두 `candles: list[Candle]`(과거→최신 정렬)과 위치 인덱스 `at`를 받고, 창이 데이터 밖이면 `None`:
    - `moving_average(candles, period, at) -> float | None` — `[at-period+1..at]` 종가 평균
    - `period_return(candles, period, at) -> float | None` — `close[at]/close[at-period] - 1`
    - `rolling_high(candles, period, at) -> int | None` — `[at-period..at-1]` 고가 최대 (**당일 제외**)
    - `average_volume(candles, period, at) -> float | None` — `[at-period..at-1]` 거래량 평균 (**당일 제외**)
    - `max_close(candles, period, at) -> int | None` — `[at-period..at-1]` 종가 최대 (**당일 제외**)

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/scoring/test_indicators.py` (신규):

```python
"""지표 함수 손계산 검증. 캔들 헬퍼는 Candle 검증 규칙(고가≥max(시,종),
저가≤min(시,종), 전부 양수)을 지키도록 만든다."""

from datetime import date, timedelta

from app.domain.broker import Candle
from app.domain.scoring.indicators import (average_volume, max_close,
                                           moving_average, period_return,
                                           rolling_high)


def make_candles(closes, volumes=None, highs=None, opens=None):
    volumes = volumes or [1000] * len(closes)
    highs = highs or [c + 1 for c in closes]
    opens = opens or list(closes)
    return [Candle(symbol="TEST00", date=date(2026, 1, 1) + timedelta(days=i),
                   open=o, high=max(h, o, c), low=min(o, c), close=c, volume=v)
            for i, (c, v, h, o) in enumerate(zip(closes, volumes, highs, opens))]


def test_이동평균():
    candles = make_candles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert moving_average(candles, 3, 9) == 9.0        # (8+9+10)/3
    assert moving_average(candles, 10, 9) == 5.5
    assert moving_average(candles, 11, 9) is None      # 창 부족


def test_기간_수익률():
    candles = make_candles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert period_return(candles, 2, 9) == 10 / 8 - 1  # 0.25
    assert period_return(candles, 9, 9) == 9.0         # 10/1-1
    assert period_return(candles, 10, 9) is None


def test_직전_고가_당일_제외():
    candles = make_candles([5, 5, 5, 5], highs=[10, 20, 15, 99])
    assert rolling_high(candles, 3, 3) == 20            # 인덱스 0~2, 당일(99) 제외
    assert rolling_high(candles, 4, 3) is None


def test_평균_거래량_당일_제외():
    candles = make_candles([5, 5, 5, 5], volumes=[100, 200, 300, 9999])
    assert average_volume(candles, 3, 3) == 200.0
    assert average_volume(candles, 4, 3) is None


def test_직전_최고_종가_당일_제외():
    candles = make_candles([10, 30, 20, 25])
    assert max_close(candles, 3, 3) == 30
    assert max_close(candles, 4, 3) is None


def test_config_기본값():
    from app.domain.scoring.config import ScoringConfig
    cfg = ScoringConfig()
    assert (cfg.top_sectors, cfg.top_candidates, cfg.hold_days) == (5, 20, 10)
    assert cfg.sector_weight_r20 + cfg.sector_weight_r60 + cfg.sector_weight_r5 == 1.0
    assert cfg.final_weight_sector + cfg.final_weight_strategy == 1.0
```

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/scoring -v > ../.superpowers/sdd/p3-task-4-red.txt 2>&1
```
기대: FAIL — `ModuleNotFoundError: app.domain.scoring`.

- [ ] **Step 3: 구현**

`backend/app/domain/scoring/config.py` (신규, 전문):

```python
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
```

`backend/app/domain/scoring/indicators.py` (신규, 전문):

```python
"""순수 지표 함수. 입력은 과거→최신 정렬된 list[Candle]과 위치 인덱스 at.
창이 데이터 범위를 벗어나면 None — 호출자(전략)는 None이면 신호 False 처리.
'당일 제외' 창([at-period..at-1])을 쓰는 함수는 돌파/눌림목처럼 "직전 구간
대비 오늘"을 비교하는 신호용이다 (스펙 §4-4 표)."""

from app.domain.broker import Candle


def moving_average(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period + 1 < 0 or at >= len(candles):
        return None
    window = candles[at - period + 1:at + 1]
    return sum(c.close for c in window) / period


def period_return(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period < 0 or at >= len(candles):
        return None
    base = candles[at - period].close
    return candles[at].close / base - 1


def rolling_high(candles: list[Candle], period: int, at: int) -> int | None:
    if at - period < 0 or at >= len(candles):
        return None
    return max(c.high for c in candles[at - period:at])


def average_volume(candles: list[Candle], period: int, at: int) -> float | None:
    if at - period < 0 or at >= len(candles):
        return None
    return sum(c.volume for c in candles[at - period:at]) / period


def max_close(candles: list[Candle], period: int, at: int) -> int | None:
    if at - period < 0 or at >= len(candles):
        return None
    return max(c.close for c in candles[at - period:at])
```

`backend/app/domain/scoring/__init__.py`, `backend/tests/scoring/__init__.py`: 빈 파일.

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-4-green.txt 2>&1
```
기대: 전체 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/scoring/ backend/tests/scoring/
git commit -m "feat(scoring): config and pure indicator functions"
```

---

### Task 5: 3전략 신호 + 적합도 시뮬레이션

**Files:**
- Create: `backend/app/domain/scoring/strategies.py`
- Create: `backend/app/domain/scoring/simulation.py`
- Test: `backend/tests/scoring/test_strategies.py`, `backend/tests/scoring/test_simulation.py`

**Interfaces:**
- Consumes: Task 4의 `ScoringConfig`, 지표 함수 5개.
- Produces:
  - `Strategy` Protocol: 속성 `name: str`, 메서드 `signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool`
  - `MomentumStrategy`(name="momentum"), `PullbackStrategy`(name="pullback"), `BreakoutStrategy`(name="breakout")
  - `default_strategies() -> tuple[Strategy, ...]` — 위 3종 인스턴스 순서 고정
  - `StrategyFitness(avg_return: float, win_rate: float, occurrences: int)` frozen dataclass
  - `simulate(candles: list[Candle], strategy: Strategy, cfg: ScoringConfig) -> StrategyFitness`

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/scoring/test_strategies.py` (신규). 작은 지표 창을 주입해 손계산 가능한 크기로 검증한다:

```python
"""전략 신호 손계산 검증 — 작은 지표 창 설정 주입."""

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.strategies import (BreakoutStrategy, MomentumStrategy,
                                           PullbackStrategy,
                                           default_strategies)
from tests.scoring.test_indicators import make_candles

SMALL = ScoringConfig(ma_short=2, ma_long=4)


def test_모멘텀_정배열_상승이면_켜진다():
    # at=7: MA2=13.5, MA4=12.5, close=14 → 14>13.5>12.5, R2=14/12-1>0
    candles = make_candles([10, 10, 10, 10, 11, 12, 13, 14])
    assert MomentumStrategy().signal(candles, 7, SMALL) is True


def test_모멘텀_횡보면_꺼진다():
    candles = make_candles([10] * 8)   # close > MA 불성립
    assert MomentumStrategy().signal(candles, 7, SMALL) is False


def test_모멘텀_창부족이면_꺼진다():
    candles = make_candles([10, 11, 12])   # ma_long=4 계산 불가
    assert MomentumStrategy().signal(candles, 2, SMALL) is False


def test_눌림목_조정후_밴드복귀면_켜진다():
    cfg = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                        pullback_band=0.05)
    closes = [100, 110, 120, 130, 140, 150, 143]
    # at=6: MA5=mean(120,130,140,150,143)=136.6 < 143 ✓
    #       MA3[6]=mean(150,143,140)=144.33 > MA3[3]=mean(110,120,130)=120 ✓
    #       직전 3일 최고 종가 150 > 143 (조정 존재) ✓
    #       |143-144.33|/144.33 = 0.92% ≤ 5% (밴드 내) ✓
    assert PullbackStrategy().signal(make_candles(closes), 6, cfg) is True


def test_눌림목_밴드이탈이면_꺼진다():
    cfg = ScoringConfig(ma_short=3, ma_long=5, pullback_lookback=3,
                        pullback_band=0.05)
    closes = [100, 110, 120, 130, 140, 150, 120]  # MA3[6]=136.67, 12.2% 이탈
    assert PullbackStrategy().signal(make_candles(closes), 6, cfg) is False


def test_돌파_신고가_거래량_실리면_켜진다():
    # at=3: 직전 3일 고가 최대 20 < close 21,
    #       당일 거래량 300 ≥ 직전 2일 평균(100,150)=125 × 1.5 = 187.5
    candles = make_candles([10, 12, 11, 21], volumes=[100, 100, 150, 300],
                           highs=[15, 20, 16, 22])
    cfg = ScoringConfig(ma_short=2, breakout_lookback=3,
                        breakout_volume_mult=1.5)
    assert BreakoutStrategy().signal(candles, 3, cfg) is True


def test_돌파_거래량_부족이면_꺼진다():
    candles = make_candles([10, 12, 11, 21], volumes=[100, 100, 150, 100],
                           highs=[15, 20, 16, 22])
    cfg = ScoringConfig(ma_short=2, breakout_lookback=3,
                        breakout_volume_mult=1.5)
    assert BreakoutStrategy().signal(candles, 3, cfg) is False


def test_기본_전략_세트():
    assert [s.name for s in default_strategies()] == [
        "momentum", "pullback", "breakout"]
```

`backend/tests/scoring/test_simulation.py` (신규):

```python
"""시뮬레이션 손계산 검증 — 고정 인덱스에서 켜지는 스텁 전략 사용."""

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.simulation import StrategyFitness, simulate
from tests.scoring.test_indicators import make_candles


class StubStrategy:
    name = "stub"

    def __init__(self, fire_at):
        self._fire_at = set(fire_at)

    def signal(self, candles, at, cfg):
        return at in self._fire_at


def test_시뮬레이션_수익률과_승률():
    # 신호 at=2 → 진입 open[3]=100, 청산 close[2+2=4]=110 → +10%
    # 신호 at=5 → 진입 open[6]=100, 청산 close[7]=95  → -5%
    closes = [50, 50, 50, 100, 110, 60, 100, 95, 90]
    cfg = ScoringConfig(hold_days=2, min_bars=1)
    fit = simulate(make_candles(closes), StubStrategy([2, 5]), cfg)
    assert fit.occurrences == 2
    assert abs(fit.avg_return - (0.10 + (-0.05)) / 2) < 1e-9
    assert fit.win_rate == 0.5


def test_잔여봉_부족한_신호는_표본_제외():
    closes = [50] * 9
    cfg = ScoringConfig(hold_days=2, min_bars=1)
    fit = simulate(make_candles(closes), StubStrategy([7]), cfg)  # 청산봉(9) 없음
    assert fit == StrategyFitness(avg_return=0.0, win_rate=0.0, occurrences=0)


def test_min_bars_이전_구간은_평가하지_않는다():
    closes = [50] * 10
    cfg = ScoringConfig(hold_days=2, min_bars=6)
    fit = simulate(make_candles(closes), StubStrategy([2]), cfg)  # at=2 < 5
    assert fit.occurrences == 0
```

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/scoring -v > ../.superpowers/sdd/p3-task-5-red.txt 2>&1
```
기대: FAIL — strategies/simulation 모듈 부재.

- [ ] **Step 3: 구현**

`backend/app/domain/scoring/strategies.py` (신규, 전문):

```python
"""스윙 3전략의 진입 신호. 조건 정의는 스펙 §4-4 표와 1:1 — 변경은 스펙과 함께.
지표가 None(창 부족)이면 신호는 False. 상태 없는 구현 — 인스턴스 재사용 안전."""

from typing import Protocol, runtime_checkable

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.indicators import (average_volume, max_close,
                                           moving_average, period_return,
                                           rolling_high)


@runtime_checkable
class Strategy(Protocol):
    name: str

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool: ...


class MomentumStrategy:
    """추세 지속: 종가 > MA20 > MA60 (정배열) AND R20 > 0."""

    name = "momentum"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        ma_s = moving_average(candles, cfg.ma_short, at)
        ma_l = moving_average(candles, cfg.ma_long, at)
        ret = period_return(candles, cfg.ma_short, at)
        if ma_s is None or ma_l is None or ret is None:
            return False
        return candles[at].close > ma_s > ma_l and ret > 0


class PullbackStrategy:
    """상승 추세 중 조정 후 MA20 밴드 복귀: 종가 > MA60 AND MA20 상승
    (MA20[t] > MA20[t-lookback]) AND 직전 lookback일 최고 종가 > 당일 종가
    AND 당일 종가가 MA20 ±band 내."""

    name = "pullback"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        ma_s_now = moving_average(candles, cfg.ma_short, at)
        ma_s_prev = moving_average(candles, cfg.ma_short,
                                   at - cfg.pullback_lookback)
        ma_l = moving_average(candles, cfg.ma_long, at)
        recent_high = max_close(candles, cfg.pullback_lookback, at)
        if (ma_s_now is None or ma_s_prev is None or ma_l is None
                or recent_high is None):
            return False
        close = candles[at].close
        return (close > ma_l
                and ma_s_now > ma_s_prev
                and recent_high > close
                and abs(close - ma_s_now) / ma_s_now <= cfg.pullback_band)


class BreakoutStrategy:
    """박스권 돌파: 종가 > 직전 breakout_lookback일 최고가(당일 제외)
    AND 당일 거래량 ≥ 직전 ma_short일 평균 거래량 × mult."""

    name = "breakout"

    def signal(self, candles: list[Candle], at: int, cfg: ScoringConfig) -> bool:
        box_high = rolling_high(candles, cfg.breakout_lookback, at)
        avg_vol = average_volume(candles, cfg.ma_short, at)
        if box_high is None or avg_vol is None or avg_vol <= 0:
            return False
        c = candles[at]
        return c.close > box_high and c.volume >= avg_vol * cfg.breakout_volume_mult


def default_strategies() -> tuple[Strategy, ...]:
    return (MomentumStrategy(), PullbackStrategy(), BreakoutStrategy())
```

`backend/app/domain/scoring/simulation.py` (신규, 전문):

```python
"""전략 적합도: 과거 신호 발생 건마다 't+1 시가 매수 → 매수일 포함
hold_days거래일째 종가 청산'을 시뮬레이션 (스펙 §4-4-b).

- 잔여 봉 부족(청산봉 없음) 발생 건은 표본에서 제외.
- 중복 보유 허용 (발생 건별 독립 — 표본 수 확보와 단순성 우선).
- TP/SL 반영형은 Phase 5에서 정책 확정 후 고도화 (스펙 §2 비범위)."""

from dataclasses import dataclass

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.strategies import Strategy


@dataclass(frozen=True)
class StrategyFitness:
    avg_return: float
    win_rate: float
    occurrences: int


def simulate(candles: list[Candle], strategy: Strategy,
             cfg: ScoringConfig) -> StrategyFitness:
    returns: list[float] = []
    # 진입 인덱스 = at+1 (다음날 시가), 청산 인덱스 = (at+1)+(hold_days-1) = at+hold_days
    for at in range(cfg.min_bars - 1, len(candles)):
        exit_idx = at + cfg.hold_days
        if exit_idx >= len(candles):
            break  # 이후 at은 전부 잔여 봉 부족
        if not strategy.signal(candles, at, cfg):
            continue
        entry = candles[at + 1].open
        if entry <= 0:
            continue
        returns.append(candles[exit_idx].close / entry - 1)
    if not returns:
        return StrategyFitness(avg_return=0.0, win_rate=0.0, occurrences=0)
    wins = sum(1 for r in returns if r > 0)
    return StrategyFitness(avg_return=sum(returns) / len(returns),
                           win_rate=wins / len(returns),
                           occurrences=len(returns))
```

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-5-green.txt 2>&1
```
기대: 전체 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/scoring/strategies.py backend/app/domain/scoring/simulation.py backend/tests/scoring/test_strategies.py backend/tests/scoring/test_simulation.py
git commit -m "feat(scoring): three swing strategies + fitness simulation"
```

---

### Task 6: 스코어링 엔진(순수 계산) + 결과 저장소 (마이그레이션 0004)

**Files:**
- Create: `backend/app/domain/scoring/engine.py`
- Create: `backend/alembic/versions/0004_scoring_tables.py`
- Modify: `backend/app/store/models.py` (스코어링 4테이블 추가)
- Create: `backend/app/store/scoring_store.py`
- Test: `backend/tests/scoring/test_engine.py`, `backend/tests/store/test_scoring_store.py`

**Interfaces:**
- Consumes: Task 4 `ScoringConfig`/지표, Task 5 `Strategy`/`simulate`/`StrategyFitness`/`default_strategies`.
- Produces (engine — 전부 frozen dataclass):
  - `SectorScore(code: str, name: str, r20: float, r60: float, r5: float, score: float, rank: int, selected: bool)`
  - `StrategyDetail(strategy: str, signal: bool, avg_return: float, win_rate: float, occurrences: int, score: float)`
  - `Candidate(symbol: str, sector_code: str, rank: int, total_score: float, sector_score: float, strategy_score: float, details: tuple[StrategyDetail, ...])`
  - `ScoringResult(sectors: tuple[SectorScore, ...], candidates: tuple[Candidate, ...], excluded_short_history: int)`
  - `run_scoring(members_by_sector: dict[str, list[str]], sector_names: dict[str, str], candles_by_symbol: dict[str, list[Candle]], cfg: ScoringConfig, strategies: tuple[Strategy, ...]) -> ScoringResult`
- Produces (store):
  - `ScoringStore(engine, now=None)` — 메서드: `create_run(reference_date: date, config_json: str) -> int`, `finish_run(run_id, status, universe_count=0, stale_excluded=0, failure_reason=None) -> None`, `save_results(run_id: int, result: ScoringResult) -> None`, `latest_results() -> dict | None`, `active_common_instruments() -> list[tuple[str, str, str]]`(symbol, audit_info, state), `industry_memberships() -> tuple[dict[str, list[str]], dict[str, str]]`(sector→members, sector→name), `latest_dates(symbols: list[str]) -> dict[str, date]`, `load_candles(symbols: list[str]) -> dict[str, list[Candle]]`

**엔진 계산 규칙 (스펙 §4-3~§4-5의 확정 해석 — 구현은 이대로):**
- 섹터 수익률 창은 `ma_short`/`ma_long`/`pullback_lookback`(기본 20/60/5)을 재사용한다 (R20/R60/R5).
- 멤버 중 봉 수 < `min_bars`인 종목은 전 단계에서 제외하고 고유 종목 수를 `excluded_short_history`로 센다.
- min-max 정규화 `_normalize(values)`: 빈 입력 → 빈 리스트, 전부 동일 → 전부 0.5.
- 전략 원점수 = `avg_return × win_rate`, 단 `occurrences < min_signal_occurrences`면 0. 정규화는 **전략별로** 평가 대상 종목 전체에 대해 min-max.
- 종목 전략 점수 = 신호 켜진 전략들의 정규화 점수 합. 신호 0개면 후보 제외.
- 한 종목이 선정 업종 여러 곳에 속하면 **섹터 점수가 높은 업종**에 1회만 귀속.
- 최종 점수 = `final_weight_sector × 섹터 정규화 점수 + final_weight_strategy × 전략 점수 정규화(후보 간 min-max)`. 정렬 tie-break는 `(-total_score, symbol)` — 결정론 보장.

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/scoring/test_engine.py` (신규):

```python
"""엔진 손계산 검증 — 스텁 전략과 작은 설정으로 결정론적 시나리오."""

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import run_scoring
from tests.scoring.test_indicators import make_candles

CFG = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                    min_bars=6, min_sector_members=1, top_sectors=1,
                    top_candidates=10, hold_days=2, min_signal_occurrences=1)


class AlwaysOn:
    name = "always"

    def signal(self, candles, at, cfg):
        return True


class AlwaysOff:
    name = "never"

    def signal(self, candles, at, cfg):
        return False


def rising(mult):  # 종가 10,20,...,80 × mult — 상승 시리즈
    return make_candles([10 * mult * i for i in range(1, 9)])


def flat():
    return make_candles([100] * 8)


def test_섹터_순위와_선정():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"], "S2": ["CCC333"]},
        sector_names={"S1": "강한업종", "S2": "횡보업종"},
        candles_by_symbol={"AAA111": rising(1), "BBB222": rising(2),
                           "CCC333": flat()},
        cfg=CFG, strategies=(AlwaysOn(),))
    by_code = {s.code: s for s in result.sectors}
    assert by_code["S1"].rank == 1 and by_code["S1"].selected is True
    assert by_code["S2"].rank == 2 and by_code["S2"].selected is False
    assert by_code["S1"].score == 1.0 and by_code["S2"].score == 0.0  # min-max
    # 후보는 선정 업종(S1) 소속만
    assert {c.symbol for c in result.candidates} == {"AAA111", "BBB222"}


def test_신호_없는_종목은_후보_제외():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111"]}, sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1)},
        cfg=CFG, strategies=(AlwaysOff(),))
    assert result.candidates == ()


def test_봉_부족_종목은_제외되고_집계된다():
    short = make_candles([10, 11, 12])  # min_bars=6 미달
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "SHORT1"]},
        sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1), "SHORT1": short},
        cfg=CFG, strategies=(AlwaysOn(),))
    assert result.excluded_short_history == 1
    assert {c.symbol for c in result.candidates} == {"AAA111"}


def test_최소_구성종목_미달_업종은_섹터_계산에서_제외():
    cfg = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                        min_bars=6, min_sector_members=2, top_sectors=1,
                        hold_days=2, min_signal_occurrences=1)
    result = run_scoring(
        members_by_sector={"S1": ["AAA111"]}, sector_names={"S1": "업종"},
        candles_by_symbol={"AAA111": rising(1)},
        cfg=cfg, strategies=(AlwaysOn(),))
    assert result.sectors == () and result.candidates == ()


def test_중복_소속은_점수_높은_업종에_한번만():
    result = run_scoring(
        members_by_sector={"S1": ["AAA111", "BBB222"],
                           "S2": ["AAA111", "CCC333"]},
        sector_names={"S1": "강", "S2": "약"},
        candles_by_symbol={"AAA111": rising(3), "BBB222": rising(2),
                           "CCC333": flat()},
        cfg=ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1,
                          min_bars=6, min_sector_members=1, top_sectors=2,
                          hold_days=2, min_signal_occurrences=1),
        strategies=(AlwaysOn(),))
    mine = [c for c in result.candidates if c.symbol == "AAA111"]
    assert len(mine) == 1
    assert mine[0].sector_code == "S1"  # S1이 더 강한 업종
```

`backend/tests/store/test_scoring_store.py` (신규) — 기존 store 테스트의 sqlite 엔진 픽스처 스타일 재사용:

```python
def test_run_라이프사이클과_결과_왕복(engine):
    store = ScoringStore(engine)
    run_id = store.create_run(reference_date=date(2026, 7, 17),
                              config_json='{"k": 1}')
    result = ScoringResult(
        sectors=(SectorScore("005", "음식료", 0.1, 0.2, 0.05, 1.0, 1, True),),
        candidates=(Candidate(
            symbol="AAA111", sector_code="005", rank=1, total_score=0.9,
            sector_score=1.0, strategy_score=0.8,
            details=(StrategyDetail("momentum", True, 0.05, 0.6, 4, 0.8),)),),
        excluded_short_history=2)
    store.save_results(run_id, result)
    store.finish_run(run_id, "succeeded", universe_count=100, stale_excluded=2)
    latest = store.latest_results()
    assert latest["run_id"] == run_id
    assert latest["candidates"][0]["symbol"] == "AAA111"
    assert latest["candidates"][0]["details"][0]["strategy"] == "momentum"
    assert latest["sectors"][0]["code"] == "005"


def test_latest는_succeeded만(engine):
    store = ScoringStore(engine)
    run_id = store.create_run(date(2026, 7, 17), "{}")
    store.finish_run(run_id, "failed", failure_reason="stale data")
    assert store.latest_results() is None


def test_universe_and_membership_queries(engine):
    # collection_store로 instruments/sectors/memberships/candles 셋업 후:
    # - active_common_instruments()는 kospi/kosdaq + is_active만 (etf 제외)
    # - industry_memberships()는 group_type='industry'만
    # - latest_dates/load_candles 왕복 (과거→최신 정렬 확인)
    ...
```

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/scoring/test_engine.py tests/store/test_scoring_store.py -v > ../.superpowers/sdd/p3-task-6-red.txt 2>&1
```
기대: FAIL — engine/scoring_store 모듈 부재.

- [ ] **Step 3: 구현**

`backend/app/domain/scoring/engine.py` (신규, 전문):

```python
"""스코어링 순수 계산. I/O 없음 — 입력 데이터만으로 결정론적 결과를 만든다.
규칙 해석(정규화·중복 귀속·tie-break)은 계획서 Task 6 머리말과 스펙 §4-3~§4-5."""

from dataclasses import dataclass

from app.domain.broker import Candle
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.indicators import period_return
from app.domain.scoring.simulation import simulate
from app.domain.scoring.strategies import Strategy


@dataclass(frozen=True)
class SectorScore:
    code: str
    name: str
    r20: float
    r60: float
    r5: float
    score: float
    rank: int
    selected: bool


@dataclass(frozen=True)
class StrategyDetail:
    strategy: str
    signal: bool
    avg_return: float
    win_rate: float
    occurrences: int
    score: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    sector_code: str
    rank: int
    total_score: float
    sector_score: float
    strategy_score: float
    details: tuple[StrategyDetail, ...]


@dataclass(frozen=True)
class ScoringResult:
    sectors: tuple[SectorScore, ...]
    candidates: tuple[Candidate, ...]
    excluded_short_history: int


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _mean_return(symbols: list[str], candles_by_symbol: dict[str, list[Candle]],
                 period: int) -> float:
    returns = []
    for s in symbols:
        candles = candles_by_symbol[s]
        r = period_return(candles, period, len(candles) - 1)
        if r is not None:
            returns.append(r)
    return sum(returns) / len(returns) if returns else 0.0


def run_scoring(members_by_sector: dict[str, list[str]],
                sector_names: dict[str, str],
                candles_by_symbol: dict[str, list[Candle]],
                cfg: ScoringConfig,
                strategies: tuple[Strategy, ...]) -> ScoringResult:
    # 1) 봉 부족 종목 제외 (고유 종목 기준 집계)
    eligible = {s for s, c in candles_by_symbol.items() if len(c) >= cfg.min_bars}
    excluded = {s for members in members_by_sector.values() for s in members
                if s in candles_by_symbol and s not in eligible}
    members = {code: [s for s in ms if s in eligible]
               for code, ms in members_by_sector.items()}
    members = {code: ms for code, ms in members.items()
               if len(ms) >= cfg.min_sector_members}

    # 2) 섹터 강도 → 정규화 → 순위 → 상위 K 선정
    codes = sorted(members)
    raw_rows = []
    for code in codes:
        r20 = _mean_return(members[code], candles_by_symbol, cfg.ma_short)
        r60 = _mean_return(members[code], candles_by_symbol, cfg.ma_long)
        r5 = _mean_return(members[code], candles_by_symbol, cfg.pullback_lookback)
        raw = (cfg.sector_weight_r20 * r20 + cfg.sector_weight_r60 * r60
               + cfg.sector_weight_r5 * r5)
        raw_rows.append((code, r20, r60, r5, raw))
    norm = _normalize([row[4] for row in raw_rows])
    ordered = sorted(zip(raw_rows, norm), key=lambda x: (-x[1], x[0][0]))
    sectors = tuple(
        SectorScore(code=row[0], name=sector_names.get(row[0], ""),
                    r20=row[1], r60=row[2], r5=row[3], score=score,
                    rank=i + 1, selected=i < cfg.top_sectors)
        for i, (row, score) in enumerate(ordered))
    sector_score_of = {s.code: s.score for s in sectors}

    # 3) 선정 업종 종목 — 중복 소속은 섹터 점수 높은 쪽에 귀속
    assigned: dict[str, str] = {}
    for s in sectors:
        if not s.selected:
            continue
        for symbol in members[s.code]:
            cur = assigned.get(symbol)
            if cur is None or sector_score_of[s.code] > sector_score_of[cur]:
                assigned[symbol] = s.code

    # 4) 전략 평가 (전 대상 종목 × 전략) → 전략별 정규화
    symbols = sorted(assigned)
    evals: dict[str, dict[str, tuple[bool, object]]] = {}
    for symbol in symbols:
        candles = candles_by_symbol[symbol]
        per: dict[str, tuple[bool, object]] = {}
        for strat in strategies:
            fired = strat.signal(candles, len(candles) - 1, cfg)
            per[strat.name] = (fired, simulate(candles, strat, cfg))
        evals[symbol] = per
    detail_score: dict[tuple[str, str], float] = {}
    for strat in strategies:
        raws = []
        for symbol in symbols:
            _, fit = evals[symbol][strat.name]
            raw = (fit.avg_return * fit.win_rate
                   if fit.occurrences >= cfg.min_signal_occurrences else 0.0)
            raws.append(raw)
        for symbol, score in zip(symbols, _normalize(raws)):
            detail_score[(symbol, strat.name)] = score

    # 5) 후보 합성: 신호 켜진 전략 점수 합 → 후보 간 정규화 → 최종 점수
    rows = []
    for symbol in symbols:
        details = tuple(
            StrategyDetail(strategy=strat.name, signal=evals[symbol][strat.name][0],
                           avg_return=evals[symbol][strat.name][1].avg_return,
                           win_rate=evals[symbol][strat.name][1].win_rate,
                           occurrences=evals[symbol][strat.name][1].occurrences,
                           score=detail_score[(symbol, strat.name)])
            for strat in strategies)
        if not any(d.signal for d in details):
            continue
        strategy_score = sum(d.score for d in details if d.signal)
        rows.append((symbol, assigned[symbol], strategy_score, details))
    strat_norm = _normalize([row[2] for row in rows])
    totals = [
        (cfg.final_weight_sector * sector_score_of[row[1]]
         + cfg.final_weight_strategy * sn, row)
        for row, sn in zip(rows, strat_norm)]
    totals.sort(key=lambda x: (-x[0], x[1][0]))
    candidates = tuple(
        Candidate(symbol=row[0], sector_code=row[1], rank=i + 1,
                  total_score=total, sector_score=sector_score_of[row[1]],
                  strategy_score=row[2], details=row[3])
        for i, (total, row) in enumerate(totals[:cfg.top_candidates]))
    return ScoringResult(sectors=sectors, candidates=candidates,
                         excluded_short_history=len(excluded))
```

`backend/alembic/versions/0004_scoring_tables.py` (신규, 전문):

```python
"""scoring result tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "score_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reference_date", sa.Date, nullable=False),
        sa.Column("universe_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stale_excluded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("config", sa.Text, nullable=False, server_default="{}"),
    )
    op.create_table(
        "score_sectors",
        sa.Column("run_id", sa.Integer, sa.ForeignKey("score_runs.id"),
                  primary_key=True),
        sa.Column("sector_code", sa.String(8), primary_key=True),
        sa.Column("r20", sa.Float, nullable=False),
        sa.Column("r60", sa.Float, nullable=False),
        sa.Column("r5", sa.Float, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("selected", sa.Boolean, nullable=False),
    )
    op.create_table(
        "scores",
        sa.Column("run_id", sa.Integer, sa.ForeignKey("score_runs.id"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("total_score", sa.Float, nullable=False),
        sa.Column("sector_code", sa.String(8), nullable=False),
        sa.Column("sector_score", sa.Float, nullable=False),
        sa.Column("strategy_score", sa.Float, nullable=False),
    )
    op.create_table(
        "score_details",
        sa.Column("run_id", sa.Integer, sa.ForeignKey("score_runs.id"),
                  primary_key=True),
        sa.Column("symbol", sa.String(12), primary_key=True),
        sa.Column("strategy", sa.String(32), primary_key=True),
        sa.Column("signal", sa.Boolean, nullable=False),
        sa.Column("avg_return", sa.Float, nullable=False),
        sa.Column("win_rate", sa.Float, nullable=False),
        sa.Column("occurrences", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("score_details")
    op.drop_table("scores")
    op.drop_table("score_sectors")
    op.drop_table("score_runs")
```

`backend/app/store/models.py`에 위 4테이블과 1:1인 `ScoreRunRow`/`ScoreSectorRow`/`ScoreRow`/`ScoreDetailRow` ORM 클래스를 추가한다 (기존 클래스들과 동일한 `Mapped`/`mapped_column` 스타일, 칼럼명·타입은 마이그레이션과 정확히 일치).

`backend/app/store/scoring_store.py` (신규, 전문):

```python
"""스코어링 영속화·조회. 동기 SQLAlchemy — 서비스가 asyncio.to_thread로 호출.
결과는 run 단위 insert-only (upsert 불필요 — 스펙 §5)."""

import json
from collections.abc import Callable
from datetime import date, datetime, timezone

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from app.domain.broker import Candle
from app.domain.scoring.engine import ScoringResult
from app.store.models import (CandleRow, InstrumentRow, ScoreDetailRow,
                              ScoreRow, ScoreRunRow, ScoreSectorRow,
                              SectorMembershipRow, SectorRow)


class ScoringStore:
    def __init__(self, engine: Engine,
                 now: Callable[[], datetime] | None = None) -> None:
        self._sessions = sessionmaker(bind=engine)
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ---------- run 라이프사이클 ----------

    def create_run(self, reference_date: date, config_json: str) -> int:
        with self._sessions.begin() as session:
            run = ScoreRunRow(started_at=self._now(), status="running",
                              reference_date=reference_date, config=config_json)
            session.add(run)
            session.flush()
            return run.id

    def finish_run(self, run_id: int, status: str, universe_count: int = 0,
                   stale_excluded: int = 0,
                   failure_reason: str | None = None) -> None:
        with self._sessions.begin() as session:
            run = session.get(ScoreRunRow, run_id)
            if run is None:
                return
            run.finished_at = self._now()
            run.status = status
            run.universe_count = universe_count
            run.stale_excluded = stale_excluded
            run.failure_reason = failure_reason

    def save_results(self, run_id: int, result: ScoringResult) -> None:
        with self._sessions.begin() as session:
            for s in result.sectors:
                session.add(ScoreSectorRow(
                    run_id=run_id, sector_code=s.code, r20=s.r20, r60=s.r60,
                    r5=s.r5, score=s.score, rank=s.rank, selected=s.selected))
            for c in result.candidates:
                session.add(ScoreRow(
                    run_id=run_id, symbol=c.symbol, rank=c.rank,
                    total_score=c.total_score, sector_code=c.sector_code,
                    sector_score=c.sector_score,
                    strategy_score=c.strategy_score))
                for d in c.details:
                    session.add(ScoreDetailRow(
                        run_id=run_id, symbol=c.symbol, strategy=d.strategy,
                        signal=d.signal, avg_return=d.avg_return,
                        win_rate=d.win_rate, occurrences=d.occurrences,
                        score=d.score))

    def latest_results(self) -> dict | None:
        """최근 succeeded 실행의 전체 결과 (API /score/latest 응답 본문)."""
        with self._sessions() as session:
            run = session.scalars(
                select(ScoreRunRow).where(ScoreRunRow.status == "succeeded")
                .order_by(ScoreRunRow.id.desc()).limit(1)).first()
            if run is None:
                return None
            sectors = session.scalars(
                select(ScoreSectorRow).where(ScoreSectorRow.run_id == run.id)
                .order_by(ScoreSectorRow.rank)).all()
            scores = session.scalars(
                select(ScoreRow).where(ScoreRow.run_id == run.id)
                .order_by(ScoreRow.rank)).all()
            details = session.scalars(
                select(ScoreDetailRow).where(ScoreDetailRow.run_id == run.id)).all()
            by_symbol: dict[str, list[dict]] = {}
            for d in details:
                by_symbol.setdefault(d.symbol, []).append(
                    {"strategy": d.strategy, "signal": d.signal,
                     "avg_return": d.avg_return, "win_rate": d.win_rate,
                     "occurrences": d.occurrences, "score": d.score})
            return {
                "run_id": run.id,
                "reference_date": run.reference_date.isoformat(),
                "finished_at": (run.finished_at.isoformat()
                                if run.finished_at else None),
                "config": json.loads(run.config),
                "sectors": [
                    {"code": s.sector_code, "r20": s.r20, "r60": s.r60,
                     "r5": s.r5, "score": s.score, "rank": s.rank,
                     "selected": s.selected} for s in sectors],
                "candidates": [
                    {"symbol": s.symbol, "rank": s.rank,
                     "total_score": s.total_score, "sector_code": s.sector_code,
                     "sector_score": s.sector_score,
                     "strategy_score": s.strategy_score,
                     "details": by_symbol.get(s.symbol, [])} for s in scores],
            }

    # ---------- 스코어링 입력 조회 ----------

    def active_common_instruments(self) -> list[tuple[str, str, str]]:
        """(symbol, audit_info, state) — kospi/kosdaq 활성 종목만 (etf 제외)."""
        with self._sessions() as session:
            rows = session.execute(
                select(InstrumentRow.symbol, InstrumentRow.audit_info,
                       InstrumentRow.state)
                .where(InstrumentRow.market.in_(("kospi", "kosdaq")),
                       InstrumentRow.is_active.is_(True))).all()
            return [tuple(r) for r in rows]

    def industry_memberships(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        """group_type='industry' 업종의 소속과 이름."""
        with self._sessions() as session:
            names = dict(session.execute(
                select(SectorRow.code, SectorRow.name)
                .where(SectorRow.group_type == "industry")).all())
            members: dict[str, list[str]] = {}
            rows = session.execute(
                select(SectorMembershipRow.sector_code,
                       SectorMembershipRow.symbol)
                .where(SectorMembershipRow.sector_code.in_(names))).all()
            for code, symbol in rows:
                members.setdefault(code, []).append(symbol)
            return members, names

    def latest_dates(self, symbols: list[str]) -> dict[str, date]:
        from sqlalchemy import func
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.symbol, func.max(CandleRow.date))
                .where(CandleRow.symbol.in_(symbols))
                .group_by(CandleRow.symbol)).all()
            return {symbol: latest for symbol, latest in rows}

    def load_candles(self, symbols: list[str]) -> dict[str, list[Candle]]:
        """과거→최신 정렬 보장. 튜플 select — 2,500종목×600봉 ORM 오버헤드 회피."""
        result: dict[str, list[Candle]] = {}
        with self._sessions() as session:
            rows = session.execute(
                select(CandleRow.symbol, CandleRow.date, CandleRow.open,
                       CandleRow.high, CandleRow.low, CandleRow.close,
                       CandleRow.volume)
                .where(CandleRow.symbol.in_(symbols))
                .order_by(CandleRow.symbol, CandleRow.date)).all()
        for symbol, d, o, h, lo, c, v in rows:
            result.setdefault(symbol, []).append(
                Candle(symbol=symbol, date=d, open=o, high=h, low=lo,
                       close=c, volume=v))
        return result
```

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-6-green.txt 2>&1
```
기대: 전체 PASS.

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/scoring/engine.py backend/alembic/versions/0004_scoring_tables.py backend/app/store/models.py backend/app/store/scoring_store.py backend/tests/scoring/test_engine.py backend/tests/store/test_scoring_store.py
git commit -m "feat(scoring): scoring engine + result store"
```

---

### Task 7: ScoringService + /score API + 앱 조립

**Files:**
- Create: `backend/app/domain/scoring/service.py`
- Create: `backend/app/api/score.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/scoring/test_service.py`, `backend/tests/test_api_score.py`

**Interfaces:**
- Consumes: Task 6 `ScoringStore`/`run_scoring`/`ScoringResult`, Task 5 `default_strategies`, Task 4 `ScoringConfig`, `market_calendar.previous_weekday`.
- Produces:
  - `ScoringProgress(run_id: int, status: str, stage: str, done: int, total: int, failure_reason: str | None = None)` — stage: `loading | gate | computing | saving | finished`
  - `ScoringService(store, config: ScoringConfig | None = None, strategies=None, reference_provider: Callable[[], date] | None = None)` — `is_running()/progress()/current_task()/start() -> Task | None/run()` (CollectionService와 동일 패턴)
  - API: `POST /score`(202/409), `GET /score/status`, `GET /score/latest`(404 없음)
  - `app.state.scoring` 조립, lifespan 종료 시 스코어링 태스크도 취소

- [ ] **Step 1: 실패하는 테스트 작성**

`backend/tests/scoring/test_service.py` (신규) — 가짜 store는 `ScoringStore`의 메서드 시그니처를 그대로 구현한 인메모리 스텁:

```python
"""ScoringService 오케스트레이션 검증 — 가짜 store, 스텁 전략, 고정 기준일."""

from datetime import date

import pytest

from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.service import ScoringService
from tests.scoring.test_indicators import make_candles

REF = date(2026, 7, 17)
CFG = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1, min_bars=6,
                    min_sector_members=1, top_sectors=1, hold_days=2,
                    min_signal_occurrences=1, stale_exclusion_limit=0.05)


class AlwaysOn:
    name = "always"

    def signal(self, candles, at, cfg):
        return True


class FakeStore:
    def __init__(self, instruments, memberships, names, candles):
        self._instruments = instruments   # [(symbol, audit_info, state)]
        self._memberships = memberships
        self._names = names
        self._candles = candles
        self.finished = None
        self.saved = None

    def create_run(self, reference_date, config_json):
        return 1

    def finish_run(self, run_id, status, universe_count=0, stale_excluded=0,
                   failure_reason=None):
        self.finished = (status, universe_count, stale_excluded, failure_reason)

    def save_results(self, run_id, result):
        self.saved = result

    def active_common_instruments(self):
        return self._instruments

    def industry_memberships(self):
        return self._memberships, self._names

    def latest_dates(self, symbols):
        return {s: self._candles[s][-1].date for s in symbols
                if s in self._candles and self._candles[s]}

    def load_candles(self, symbols):
        return {s: self._candles[s] for s in symbols if s in self._candles}


def normal(symbol):
    return (symbol, "정상", "증거금100%")


@pytest.mark.asyncio
async def test_성공_경로_결과_저장():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])  # 최신 = 기준일
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": candles})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.saved is not None
    assert store.saved.candidates[0].symbol == "AAA111"


@pytest.mark.asyncio
async def test_유니버스_필터_비정상_상태_제외():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    store = FakeStore(
        [normal("AAA111"), ("BBB222", "관리종목", "관리종목"),
         ("CCC333", "정상", "증거금100%|거래정지")],
        {"005": ["AAA111", "BBB222", "CCC333"]}, {"005": "음식료"},
        {s: candles for s in ("AAA111", "BBB222", "CCC333")})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.finished[1] == 1  # universe_count: AAA111만
    assert {c.symbol for c in store.saved.candidates} == {"AAA111"}


@pytest.mark.asyncio
async def test_신선도_게이트_전체_실패():
    stale = make_candles([10, 20, 30, 40, 50, 60, 70, 80])  # 마지막 날짜 < 기준일
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": stale})
    future_ref = date(2099, 1, 1)
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: future_ref)
    await service.run()
    assert store.finished[0] == "failed"
    assert "stale" in store.finished[3]
    assert store.saved is None


@pytest.mark.asyncio
async def test_소수_정체_종목은_개별_제외되고_집계된다():
    fresh = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    cfg = ScoringConfig(ma_short=2, ma_long=4, pullback_lookback=1, min_bars=6,
                        min_sector_members=1, top_sectors=1, hold_days=2,
                        min_signal_occurrences=1,
                        stale_exclusion_limit=0.5)  # 50%까지 허용
    stale = make_candles([10, 20, 30, 40, 50, 60, 70])  # 하루 짧음
    store = FakeStore([normal("AAA111"), normal("BBB222")],
                      {"005": ["AAA111", "BBB222"]}, {"005": "음식료"},
                      {"AAA111": fresh, "BBB222": stale})
    service = ScoringService(store, config=cfg, strategies=(AlwaysOn(),),
                             reference_provider=lambda: fresh[-1].date)
    await service.run()
    assert store.finished[0] == "succeeded"
    assert store.finished[2] == 1  # stale_excluded
    assert {c.symbol for c in store.saved.candidates} == {"AAA111"}


@pytest.mark.asyncio
async def test_빈_유니버스는_실패():
    store = FakeStore([], {}, {}, {})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: REF)
    await service.run()
    assert store.finished[0] == "failed"
    assert "universe" in store.finished[3]


@pytest.mark.asyncio
async def test_start는_중복_실행을_거부():
    candles = make_candles([10, 20, 30, 40, 50, 60, 70, 80])
    store = FakeStore([normal("AAA111")], {"005": ["AAA111"]},
                      {"005": "음식료"}, {"AAA111": candles})
    service = ScoringService(store, config=CFG, strategies=(AlwaysOn(),),
                             reference_provider=lambda: candles[-1].date)
    task = service.start()
    assert task is not None
    assert service.start() is None
    await task
```

`backend/tests/test_api_score.py` (신규, 전문) — lifespan 없이 최소 FastAPI 앱에 라우터와 가짜 상태만 꽂는다:

```python
"""/score API 계약 검증 — 가짜 서비스/스토어로 상태별 응답 코드 확인."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.score import router
from app.domain.scoring.service import ScoringProgress


class FakeScoring:
    def __init__(self, running=False, progress=None):
        self._running = running
        self._progress = progress

    def is_running(self):
        return self._running

    def start(self):
        return None if self._running else object()

    def progress(self):
        return self._progress


class FakeCollection:
    def __init__(self, running=False):
        self._running = running

    def is_running(self):
        return self._running


class FakeScoringStore:
    def __init__(self, latest=None):
        self._latest = latest

    def latest_results(self):
        return self._latest


def make_client(scoring=None, collection=None, store=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.scoring = scoring or FakeScoring()
    app.state.collection = collection or FakeCollection()
    app.state.scoring_store = store or FakeScoringStore()
    return TestClient(app)


def test_score_시작():
    resp = make_client().post("/score")
    assert resp.status_code == 202
    assert resp.json() == {"started": True}


def test_score_중복이면_409():
    resp = make_client(scoring=FakeScoring(running=True)).post("/score")
    assert resp.status_code == 409


def test_수집중이면_409():
    resp = make_client(collection=FakeCollection(running=True)).post("/score")
    assert resp.status_code == 409
    assert "collection" in resp.json()["detail"]


def test_status_idle():
    resp = make_client().get("/score/status")
    assert resp.json() == {"status": "idle"}


def test_status_실패사유_노출():
    progress = ScoringProgress(run_id=3, status="failed", stage="finished",
                               done=0, total=100, failure_reason="stale data")
    resp = make_client(scoring=FakeScoring(progress=progress)).get("/score/status")
    body = resp.json()
    assert body["status"] == "failed" and body["failure_reason"] == "stale data"


def test_latest_없으면_404():
    assert make_client().get("/score/latest").status_code == 404


def test_latest_반환():
    latest = {"run_id": 7, "candidates": []}
    resp = make_client(store=FakeScoringStore(latest=latest)).get("/score/latest")
    assert resp.status_code == 200 and resp.json() == latest
```

- [ ] **Step 2: 실패 확인**

```bash
cd backend && uv run pytest tests/scoring/test_service.py tests/test_api_score.py -v > ../.superpowers/sdd/p3-task-7-red.txt 2>&1
```
기대: FAIL — service/api 모듈 부재.

- [ ] **Step 3: 구현**

`backend/app/domain/scoring/service.py` (신규, 전문):

```python
"""스코어링 오케스트레이션. CollectionService와 동일한 실행 패턴 —
원자적 start(), 태스크 강참조, 예외 경계(실패 시 run을 failed로 마감).
브로커 호출 없음: ScoringStore가 주는 데이터만 소비한다."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.core.market_calendar import previous_weekday
from app.domain.scoring.config import ScoringConfig
from app.domain.scoring.engine import run_scoring
from app.domain.scoring.strategies import Strategy, default_strategies

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("scoring task failed: %s", exc)


@dataclass(frozen=True)
class ScoringProgress:
    run_id: int
    status: str  # running | succeeded | failed
    stage: str   # loading | gate | computing | saving | finished
    done: int
    total: int
    failure_reason: str | None = None


def _passes_universe(audit_info: str, state: str) -> bool:
    """스펙 §4-2: auditInfo가 "정상"이고 state에 거래정지/관리종목 플래그가 없다."""
    return (audit_info == "정상"
            and "거래정지" not in state
            and "관리종목" not in state)


class ScoringService:
    def __init__(self, store, config: ScoringConfig | None = None,
                 strategies: tuple[Strategy, ...] | None = None,
                 reference_provider: Callable[[], date] | None = None) -> None:
        self._store = store
        self._config = config or ScoringConfig()
        self._strategies = strategies or default_strategies()
        self._reference_provider = reference_provider or previous_weekday
        self._running = False
        self._progress: ScoringProgress | None = None
        self._task: asyncio.Task | None = None

    def is_running(self) -> bool:
        return self._running

    def progress(self) -> ScoringProgress | None:
        return self._progress

    def current_task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> asyncio.Task | None:
        """원자적 시작 — check와 set 사이에 await 없음 (CollectionService와 동일)."""
        if self._running:
            return None
        self._running = True
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_log_task_exception)
        return self._task

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("scoring already running")
        self._running = True
        await self._run()

    async def _run(self) -> None:
        cfg = self._config
        reference = self._reference_provider()
        run_id = await asyncio.to_thread(
            self._store.create_run, reference, cfg.to_json())
        universe_count = stale_excluded = 0
        try:
            self._set(run_id, "running", "loading", 0, 0)
            instruments = await asyncio.to_thread(
                self._store.active_common_instruments)
            universe = [sym for sym, audit, state in instruments
                        if _passes_universe(audit, state)]
            universe_count = len(universe)
            if universe_count == 0:
                await self._fail(run_id, 0, 0,
                                 "empty universe - run collection first "
                                 "(instruments need audit_info/state fields)")
                return

            self._set(run_id, "running", "gate", 0, universe_count)
            latest = await asyncio.to_thread(self._store.latest_dates, universe)
            fresh = [s for s in universe
                     if latest.get(s) is not None and latest[s] >= reference]
            stale_excluded = universe_count - len(fresh)
            stale_ratio = stale_excluded / universe_count
            if stale_ratio > cfg.stale_exclusion_limit:
                await self._fail(
                    run_id, universe_count, stale_excluded,
                    f"stale data - run collection first "
                    f"({stale_ratio:.1%} > {cfg.stale_exclusion_limit:.1%}, "
                    f"reference={reference.isoformat()})")
                return

            self._set(run_id, "running", "computing", 0, len(fresh))
            members, names = await asyncio.to_thread(
                self._store.industry_memberships)
            fresh_set = set(fresh)
            members = {code: [s for s in ms if s in fresh_set]
                       for code, ms in members.items()}
            symbols = sorted({s for ms in members.values() for s in ms})
            candles = await asyncio.to_thread(self._store.load_candles, symbols)
            result = await asyncio.to_thread(
                run_scoring, members, names, candles, cfg, self._strategies)

            self._set(run_id, "running", "saving", len(fresh), len(fresh))
            await asyncio.to_thread(self._store.save_results, run_id, result)
            await asyncio.to_thread(self._store.finish_run, run_id, "succeeded",
                                    universe_count, stale_excluded, None)
            self._set(run_id, "succeeded", "finished",
                      len(fresh), len(fresh))
            logger.info(
                "scoring run %d: %d candidates from %d sectors "
                "(universe=%d, stale=%d, short_history=%d)",
                run_id, len(result.candidates),
                sum(1 for s in result.sectors if s.selected), universe_count,
                stale_excluded, result.excluded_short_history)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    universe_count, stale_excluded, "cancelled")
            self._set(run_id, "failed", "finished", 0, universe_count,
                      "cancelled")
            raise
        except Exception as exc:
            logger.exception("scoring run %s failed unexpectedly", run_id)
            await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                    universe_count, stale_excluded,
                                    f"unexpected: {type(exc).__name__}")
            self._set(run_id, "failed", "finished", 0, universe_count,
                      f"unexpected: {type(exc).__name__}")
            raise
        finally:
            self._running = False

    async def _fail(self, run_id: int, universe_count: int,
                    stale_excluded: int, reason: str) -> None:
        logger.warning("scoring run %d rejected: %s", run_id, reason)
        await asyncio.to_thread(self._store.finish_run, run_id, "failed",
                                universe_count, stale_excluded, reason)
        self._set(run_id, "failed", "finished", 0, universe_count, reason)

    def _set(self, run_id: int, status: str, stage: str, done: int,
             total: int, failure_reason: str | None = None) -> None:
        self._progress = ScoringProgress(run_id, status, stage, done, total,
                                         failure_reason)
```

`backend/app/api/score.py` (신규, 전문):

```python
import asyncio

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.post("/score", status_code=202)
async def start_scoring(request: Request) -> dict:
    if request.app.state.collection.is_running():
        # 수집이 소속/상태/봉을 갱신하는 도중의 반쪽 데이터 읽기 방지 (스펙 §6)
        raise HTTPException(status_code=409,
                            detail="collection is running - retry after it finishes")
    task = request.app.state.scoring.start()
    if task is None:
        raise HTTPException(status_code=409, detail="scoring already running")
    return {"started": True}


@router.get("/score/status")
async def scoring_status(request: Request) -> dict:
    progress = request.app.state.scoring.progress()
    if progress is None:
        return {"status": "idle"}
    body = {"run_id": progress.run_id, "status": progress.status,
            "stage": progress.stage, "done": progress.done,
            "total": progress.total}
    if progress.failure_reason is not None:
        body["failure_reason"] = progress.failure_reason
    return body


@router.get("/score/latest")
async def latest_scores(request: Request) -> dict:
    results = await asyncio.to_thread(request.app.state.scoring_store.latest_results)
    if results is None:
        raise HTTPException(status_code=404, detail="no succeeded scoring run")
    return results
```

`backend/app/main.py` 변경 — 임포트 3건 추가(`score` 라우터, `ScoringService`, `ScoringStore`), lifespan에서:

```python
            app.state.broker = KiwoomBroker(KiwoomHttpClient(settings))
            app.state.collection = CollectionService(
                app.state.broker, CollectionStore(app.state.engine))
            app.state.scoring_store = ScoringStore(app.state.engine)
            app.state.scoring = ScoringService(app.state.scoring_store)
            try:
                yield
            finally:
                for service in (app.state.scoring, app.state.collection):
                    task = service.current_task()
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                await app.state.broker.aclose()
```

그리고 `app.include_router(score_router)` 추가.

- [ ] **Step 4: 통과 확인 + 전체 회귀**

```bash
cd backend && uv run pytest tests -q > ../.superpowers/sdd/p3-task-7-green.txt 2>&1
```
기대: 전체 PASS (기존 lifespan 테스트 포함).

- [ ] **Step 5: 커밋**

```bash
git add backend/app/domain/scoring/service.py backend/app/api/score.py backend/app/main.py backend/tests/scoring/test_service.py backend/tests/test_api_score.py
git commit -m "feat(api): scoring service and /score endpoints"
```

---

### Task 8: 실데이터 수용 검증 (코드 변경 없음 — 코디네이터 직접 수행)

스펙 §8 수용 기준 검증. 라이브 시스템(도커 + 모의서버 수집 데이터) 대상이므로
서브에이전트가 아닌 **코디네이터가 직접** 수행하고 증거를 남긴다.

- [ ] **Step 1: 컨테이너 재빌드 + 마이그레이션 적용 확인**

```bash
docker compose build backend && docker compose up -d
docker compose exec backend alembic upgrade head
docker compose exec db psql -U ohmystock -d ohmystock -c "\dt" \
  > .superpowers/sdd/p3-task-8-tables.txt 2>&1
```
기대: `sector_memberships`, `score_runs`, `score_sectors`, `scores`,
`score_details` 존재. `instruments.sector_code` 부재(`\d instruments`).

- [ ] **Step 2: 재수집 (소속·상태 재구축)**

```bash
curl -s -X POST http://127.0.0.1:8000/collect
# 진행 관찰: curl -s http://127.0.0.1:8000/collect/status
```
기대: 일봉은 달력 스킵으로 대부분 건너뛰어 수 분 내 완료. 완료 후:

```sql
-- 소속 규모 (전 그룹 보존 확인 — 실측 기대: 65그룹, 수만 행)
SELECT s.group_type, count(DISTINCT m.sector_code) sectors, count(*) rows
FROM sector_memberships m JOIN sectors s ON s.code = m.sector_code
GROUP BY s.group_type;
-- 상태 필드 채워짐 확인 (빈 문자열 아닌 행 존재)
SELECT audit_info, count(*) FROM instruments GROUP BY audit_info;
```

- [ ] **Step 3: 수용 기준 — 시장별 industry 중복 소속률 < 5% (스펙 §3-2)**

```sql
SELECT market,
       round(100.0 * count(*) FILTER (WHERE cnt > 1) / count(*), 2) AS overlap_pct,
       count(*) AS symbols
FROM (
  SELECT m.symbol, s.market, count(*) AS cnt
  FROM sector_memberships m
  JOIN sectors s ON s.code = m.sector_code
  WHERE s.group_type = 'industry'
  GROUP BY m.symbol, s.market) t
GROUP BY market;
```
캡처: `> .superpowers/sdd/p3-task-8-overlap.txt`. **5% 초과 시 중단하고 분류
맵(umbrella 누락 의심)을 재검토** — Task 1의 `_CLASSIFICATION` 수정 후 재수집.

- [ ] **Step 4: 스코어링 실행 + 산출 확인**

```bash
curl -s -X POST http://127.0.0.1:8000/score
# 완료까지 관찰: curl -s http://127.0.0.1:8000/score/status
curl -s http://127.0.0.1:8000/score/latest \
  > .superpowers/sdd/p3-task-8-latest.json 2>&1
```
기대: status `succeeded`, latest에 진출 업종 5개(selected=true)와 후보 최대
20종목 + 전략 상세. 실행 소요 시간(computing 단계)을 기록해 회고록에 반영.

- [ ] **Step 5: 결과 기록**

증거 파일 3종(`p3-task-8-*`)을 원장에 기록하고, STATUS.md 갱신 + Phase 3
회고록(`docs/retrospectives/2026-07-18-phase3-scoring-engine.md`) 작성으로
마무리한다 (규칙 4 — 커밋은 사용자 컨펌 후).

---

## 계획 자체 점검 (self-review 결과)

- **스펙 커버리지:** §3(정비 3건)=Task 1~3, §4(파이프라인)=Task 4~7, §5(스키마)=Task 6, §6(API)=Task 7, §7(에러)=Task 5·7, §8(테스트·수용 기준)=각 태스크 Step 1 + Task 8. 잔여 없음.
- **자리표시자:** 없음 (초안의 Task 7 API 테스트 시그니처 나열과 Task 8 오작성 SQL 블록은 셀프리뷰에서 전문 코드/확정 쿼리로 교체 완료).
- **타입 일관성:** `ScoringConfig`(T4) ↔ 전략/시뮬(T5) ↔ 엔진(T6) ↔ 서비스(T7)의 시그니처 상호 참조 확인 완료. `replace_sector_memberships`(T2) ↔ 소비(T3), `active_common_instruments` 튜플 순서(symbol, audit_info, state)는 T6 정의 = T7 소비 일치.
- **순서 주의:** Task 2의 `set_sector_codes`는 deprecated 표시만, 실제 제거는 Task 3 (중간 상태에서도 전체 테스트 그린 유지).
