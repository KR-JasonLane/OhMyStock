# 설계 Spec — Phase 2: 데이터 수집 파이프라인

- **날짜:** 2026-07-17
- **상태:** 확정 — 구현 완료 (2026-07-17)
- **선행:** Phase 1 키움 브로커 어댑터 완료 (2026-07-17,
  `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`).
  **Phase 2 PRE-GATE 실측 완료** (§5 참고).
- **완료 근거:** `docs/retrospectives/2026-07-17-phase2-data-collection-pipeline.md`
  (Task 1~7 상세, 패널 리뷰 결과, 풀 수집 실측).
- **범위:** 장 마감 후 전 종목 일봉 + 업종(섹터) 매핑을 수집해 PostgreSQL에 저장하는
  파이프라인. 시동은 HTTP API(수동), 자동 스케줄링은 Phase 6.

---

## 1. 배경 & 이번 브레인스토밍의 결정

1. **유니버스 = 전 종목** (사용자 결정): KOSPI+KOSDAQ 보통주에 ETF/ETN 등 포함.
   `instruments`에 **종목 구분(instrument_type)과 시장 구분을 저장**해 이후
   Phase(스코어링·매매)에서 "보통주만" 같은 필터가 가능하게 한다. 약 4,000종목,
   TR당 ~1 req/s 기준 일봉 수집 약 60~70분(야간 배치 전제).
2. **시동 = HTTP API** (사용자 결정): `POST /collect`(시작) + `GET /collect/status`
   (진행률). Phase 7 대시보드 버튼의 토대가 되고, Phase 6 스케줄러는 동일한 수집
   함수를 프로세스 내부에서 직접 호출한다.
3. **저장 정책:** 일봉은 종목당 1페이지(600봉 ≈ 2.5년치, 실측)가 한 번에 오므로
   **오는 그대로 upsert** — 6개월로 자르지 않는다(소비자가 필요한 만큼 잘라 씀).
   재실행은 멱등(중복 없음).
4. **섹터 매핑 전략 = 키움 TR 우선, KRX 파일 대안:** ka10101(업종코드 목록) +
   ka20002(업종별 구성종목 — **미검증 추정**)의 벌크 경로를 1순위로 하되, 구현 첫
   태스크의 **실측 스파이크**로 확정한다. ka20002가 구성종목을 주지 않으면 대안 B
   (KRX 정보데이터시스템 업종분류 파일 조인)로 전환한다.
5. Phase 1과 동일한 프로세스: TDD + 신규 TR은 라이브 스모크 실측 + 태스크별
   4-에이전트 리뷰 패널(규칙 8).

## 2. 목표

매일 저녁 1회 실행으로 ①상장 전 종목 명부 ②종목→업종 매핑 ③종목별 일봉을 DB에
최신화한다. Phase 3(스코어링)은 키움을 호출하지 않고 이 DB만 읽는다.

**범위 밖 (명시):** 스코어링·전략 로직(Phase 3), 자동 스케줄링(Phase 6), 분봉/주봉/
틱, 실시간 시세, UI(버튼은 Phase 7), 수집 데이터 정합성 리포트 자동화.

## 3. 구성요소 & 경계

### 3.1 `store/` — 스키마 (Alembic 리비전 `0002`)

```
sectors           업종 코드표
  code (PK, str)  market (str)  name (str)

instruments       종목 명부
  symbol (PK, str6)  name  market (kospi|kosdaq|etc)  instrument_type (str)
  sector_code (FK sectors.code, nullable)  is_active (bool)  updated_at

candles           일봉 창고
  symbol + date (복합 PK)  open/high/low/close (int, 원)  volume (int)
  (수정주가 기준 — BrokerPort 계약. upsert로 갱신)

collection_runs   수집 일지
  id (PK)  started_at  finished_at (nullable)  status (running|done|failed)
  total_symbols  succeeded  failed  error_summary (text, nullable)
```

- `instrument_type`은 키움 응답의 구분 필드를 실측 후 매핑(스파이크에서 확정).
  구분 필드가 없으면 종목명 패턴(스팩/우선주 접미) 휴리스틱을 쓰되 그 사실을 기록.
- SQLAlchemy 2.0 스타일 모델, `store/models.py`. 세션/리포지토리는 `store/`에만.

### 3.2 `domain/broker.py` — 포트 확장 (키움 무지 유지)

```python
class Instrument:  # symbol, name, market, instrument_type
class Sector:      # code, market, name

class BrokerPort(Protocol):
    # 기존 4개 +
    async def list_instruments(market: str) -> list[Instrument]
    async def list_sectors() -> list[Sector]
    async def list_sector_members(sector_code: str) -> list[str]  # symbol 목록
```

### 3.3 `adapters/kiwoom/broker.py` — TR 매핑 추가

| 메서드 | TR | 비고 |
|---|---|---|
| `list_instruments` | `ka10099` (stkinfo) | 시장구분 `mrkt_tp`: 0=코스피, 10=코스닥, 8=ETF (비공식 — 스파이크 실측) |
| `list_sectors` | `ka10101` (stkinfo) | 응답 포맷 미확인 (레거시는 문자열 파이프 구분 — 실측) |
| `list_sector_members` | `ka20002` (sect) | **핵심 미검증 가정** — 스파이크 1순위 |

Phase 1 패턴 준수: `_to_*` 헬퍼, `try/except (...) → BrokerError`, 페이지네이션
`call_paged` + `aclosing`, 필드 실측 정정 절차.

### 3.4 `domain/collection.py` — 수집 서비스 (순수 오케스트레이션)

```python
class CollectionService:
    def __init__(broker: BrokerPort, store: CollectionStore, ...)
    async def run() -> CollectionResult
```

- 순서: 명부(1단계) → 업종 매핑(2단계) → 종목별 일봉(3단계) → 일지 마감(4단계).
- **재개(resume):** 당일 재실행 시, 이번 run에서 관측된 최신 거래일의 봉이 이미
  저장된 종목은 건너뛴다 (멱등).
- **오류 허용:** 종목 단위 `BrokerError`는 기록 후 계속. **연속 실패 20회 초과 시
  중단**(서버/인증 장애로 판단, run=failed). `RateLimitError`/`AuthError`도 동일
  중단 규칙.
- 진행 상태(현재 단계, n/total, 실패 수)를 메모리에 유지 — status API가 읽는다.
- 동시 실행 금지: 이미 running이면 시작 거절.

### 3.5 `api/collect.py` — 전송 계층

- `POST /collect` → 202 + run id (백그라운드 asyncio task로 시작). 이미 실행 중이면
  409. 비즈니스 로직 없음 — 서비스 호출만.
- `GET /collect/status` → 최근 run의 상태/진행률
  (`{"run_id", "status", "stage", "done", "total", "failed"}`).
- 노출 면: 백엔드는 이미 `127.0.0.1` 바인딩. 인증은 이번 Phase에 도입하지 않되,
  보안 패널 관점에서 위험(무인증 쓰기 경로)을 spec에 명시하고 Phase 7 전 재평가.

## 4. 에러 처리

- 어댑터: Phase 1 계약 유지 — 모든 실패는 `BrokerError` 계층.
- 서비스: 종목별 실패 격리(기록+계속), 연속 실패 임계 초과·인증/리밋 오류는 run 중단
  후 `failed` + `error_summary` 기록. 부분 성공도 DB에는 남는다(upsert 멱등이라 재실행
  안전).
- API: 실행 중 409, 상태 없음(최초) 시 빈 상태 응답.

## 5. 실측 확정 팩트 & 스파이크 대상

**PRE-GATE 실측 완료 (2026-07-17, `.superpowers/sdd/phase2-pregate-basedt.txt`):**
- `ka10081` `base_dt`는 **조회 기준일** — 비영업일이면 직전 영업일로 자동 보정(에러
  없음), 과거 날짜는 그 시점까지의 봉 반환(백필 가능), 미래는 오늘로 클램프.
- 일봉 1페이지 = **600봉** → 6개월 수집은 종목당 1호출.
- 실행 중 429 발생 시 백오프+penalize가 실전 경로에서 정상 작동함을 관측.

**스파이크(Task 1, `.superpowers/sdd/phase2-spike-tr.txt`) 실측 결과 — 분기 A 채택
(키움 TR 경로 확정, 대안 B 불필요):**

| TR | 엔드포인트/바디 | 응답 구조 | 실측 수치 |
|---|---|---|---|
| `ka10099`(종목리스트) | `POST /api/dostk/stkinfo`, `mrkt_tp` 0=코스피/10=코스닥/8=ETF | list 키 `"list"`; 행에 `code`/`name`/`marketCode`/`marketName`/`upName`/`upSizeName`/`state`/`auditInfo`/`kind`/`listCount`/`regDay`/`lastPrice` 등 | 코스피 요청 2478행 원본(마켓코드 혼재) → `marketCode` 필터 후 919행 실제 코스피; 코스닥 1821행; ETF 1147행. 1페이지로 전량 수신(`cont=N`) |
| `ka10101`(업종코드) | 동일 엔드포인트, `mrkt_tp` 0=코스피/**1**=코스닥(ka10099와 값이 다름) | list 키 `"list"`; 행에 `marketCode`/`code`/`name`/`group` | 코스피 31개, 코스닥 34개 업종 |
| `ka20002`(업종별구성종목) | `POST /api/dostk/sect`, **`mrkt_tp`+`inds_cd`+`stex_tp`("1" 또는 "KRX") 3개 필수**(누락 시 `[1511]` 거부) | list 키 `"inds_stkpc"`; 행에 `stk_cd`(6자리) 등 | 업종 "001"(종합(KOSPI)) 구성종목 2477개 — 사실상 코스피 전체(집계 업종은 하류에서 필터 필요) |

**판정:** ka20002가 구성종목을 정상 반환하므로 **분기 A(키움 TR 벌크 경로)를
채택**했고, 대안 B(KRX 파일 조인)로 전환할 필요가 없었다. `instrument_type`은
구분 전용 필드가 없어(`kind`는 보통주·ETF 모두 "A"로 동일) `marketCode` 기반
필터로 정정, 관리종목/감리 정보는 `state`/`auditInfo`에 존재하나 도메인 모델은
저장하지 않기로 결정(Phase 3 PRE-GATE로 이관, `docs/STATUS.md` 참고).

## 6. 테스트

1. **단위(respx):** 신규 TR 매핑(픽스처), CollectionService(가짜 BrokerPort/Store로
   재개·오류 허용·연속 실패 중단·진행률), API(409, 상태 응답), 스키마(마이그레이션
   적용 + upsert 멱등).
2. **라이브 스모크(-m live):** ka10099/ka10101/ka20002 실호출 (스파이크 산출물을
   회귀 테스트로 유지).
3. **풀 수집 실측 1회:** `POST /collect`로 전 종목 수집 완주 — 소요 시간·실패 수
   기록, DB 행 수 검증. (완료 정의에 포함)
4. 태스크별 4-에이전트 패널(규칙 8).

## 7. 완료 정의 (Definition of Done)

1. Alembic `0002` 적용 시 4개 테이블 생성, 재적용 멱등.
2. `BrokerPort` 확장 3메서드가 라이브 스모크로 실측 검증됨 (또는 대안 B 채택 시
   그 경로가 검증됨 — 어느 쪽인지 회고록에 기록).
3. 단위 테스트 전체 그린 (Phase 1의 50개 포함 회귀).
4. **풀 수집 1회 완주 실측**: 전 종목 명부+업종 매핑+일봉이 DB에 적재, run 일지
   정상, 재실행 시 스킵 동작 확인.
5. 모든 태스크 패널 통과, 실측 팩트 CLAUDE.md §5 반영, 회고록 + STATUS Phase 3
   핸드오프.

## 8. 리스크 / 미해결 항목

- **ka20002 가정 (핵심) — 해결됨:** 스파이크(Task 1)에서 구성종목을 정상 반환함을
  확인, **분기 A 채택**(대안 B 불필요). §5 표 참고.
- **모의서버 데이터 품질:** 모의서버의 종목 수/업종 데이터가 실전과 다를 수 있음 —
  실측 수치를 기록하고, 실전 전환 시 재검증 항목으로 남김.
- **무인증 쓰기 경로(`POST /collect`):** localhost 한정이지만 인증 없음 — Phase 7
  (UI)에서 인증/토큰 도입 여부 재평가. 보안 패널이 태스크마다 점검(sec 판정: 계획된
  구현이라 이번 Phase는 acceptable, Phase 5 전 인증 도입 의무 재확인).
- **수집 시간대:** 장 마감 직후는 당일 봉 확정 전일 수 있음 — PRE-GATE 실측상
  base_dt 자동 보정이 있으므로 야간(19시 이후) 실행 권장을 회고록에 기록. 정확한
  당일 봉 확정 시각은 미실측(Phase 6 스케줄 설계 시 확인).
- **Phase 2 개막 정리(이월) — 완료:** Phase 1 최종 리뷰가 넘긴 소소한 항목 —
  TokenManager 429 백오프, `client.aclose` try/finally, `expires_dt` 파싱 래핑,
  tests conftest 통합 — 을 Task 2(하드닝 스위프)에서 전부 처리했다.
- **⚠️ 신규(Task 7 실측): 무효 토큰 응답이 HTTP 401이 아니라 200+`[8005]`.**
  클라이언트가 이를 놓치면 매 TR 호출이 조용히 실패한다 — 8005 전용 재발급
  분기를 추가해 해결(commit `50391ac`). **(측정 정황상 추정, 미확정) 앱키당
  활성 토큰이 1개뿐일 가능성** — 백엔드 가동 중 별도 프로세스에서 토큰을
  재발급하면 기존 토큰이 무효화되는 것으로 관측됨. 운영 규칙: 백엔드 가동 중
  호스트에서 별도로 토큰 발급 금지(CLAUDE.md §5에 반영).
- **⚠️ 신규(Task 7 실측): 응답 필드가 전부 빈 문자열인 퇴화(degenerate) 캔들
  응답 존재** — 종목 `012510`에서 관측. `Candle.__post_init__`의 OHLC 검증이
  fail-loud로 거부해 실제로는 위험하지 않았으나, 유사 응답이 더 있을 수 있다는
  전제로 상시 인지할 것.
