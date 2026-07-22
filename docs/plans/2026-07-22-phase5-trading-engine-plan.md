# Phase 5 트레이딩 엔진 구현 계획서

> **For agentic workers:** 이 계획서는 태스크 단위로 구현한다. 각 태스크 완료 후
> 4-에이전트 패널(개발자·트레이더·아키텍트·보안) 전원 통과를 확인하고 다음으로
> 넘어간다(규칙 8). 스텝은 체크박스(`- [ ]`)로 추적한다.

**Goal:** `GET /analyze/latest`의 최종 매수 리스트를 받아, 시가 안정화 후 진입하고
클라이언트측에서 손절/익절/트레일링/보유기간을 REST 폴링으로 감시·청산하는
트레이딩 엔진을 구현한다. 키움 REST에는 네이티브 TP/SL이 없으므로 전부 client-side.

**Architecture:** 주문은 `OrderPort`(신규) 뒤에 숨기고(ISP — 기존 `BrokerPort`와
분리), 판정 로직은 순수 함수(`costs`/`selection`/`exit_rules`/`reconcile_decide`),
집행은 부수효과(`entry`/`monitor`/`reconcile`), 오케스트레이션은 확장된
`BackgroundRunService`(정지 계약 + 타임스탬프 승격)의 4번째 서브클래스. 결과는
insert-only + 상태전이 4테이블(0007).

**Tech Stack:** Python 3.12, httpx(기존), 키움 REST 주문 TR(kt10000/kt10001/
kt10003)·다종목 시세(ka10095)·미체결 조회, SQLAlchemy/Alembic(기존).

**Spec:** `docs/specs/2026-07-21-phase5-trading-engine-design.md` v3.1 (수치·조건의
단일 출처). 결정 #25~#35, PRE-GATE G1~G4.

## Global Constraints

- 커밋 메시지 사전 확인 필수, AI 흔적 금지(규칙 7). 태스크별 4-에이전트 패널(규칙 8).
- 테스트 출력 캡처: `> ../.superpowers/sdd/p5-task-N-*.txt 2>&1`.
- `domain/trading/`은 adapters/store를 임포트하지 않는다. 주문은 `OrderPort` 경유만.
- **신규 런타임 의존성 0** — 전부 기존 스택(httpx/SQLAlchemy).
- 순수 함수(`costs`/`selection`/`exit_rules`/`reconcile_decide`)는 부수효과 없이
  주입값만으로 계산. I/O(캘린더·브로커·DB)는 호출자가 값으로 변환해 주입.
- `TradingConfig` 파라미터 하드코딩 금지 — 스펙 §6-2와 1:1. `__post_init__` 검증(§6-2-c).
- 시크릿: `api_trade_token`은 SecretStr, `compare_digest` 비교, 값 미로그. 주문
  요청/응답은 **바디만** 저장 — `Authorization` 헤더/토큰은 어느 계층에도 금지(§9).
- 새 서비스는 `BackgroundRunService`에 logger 주입(P4-pre 트레이더 체크리스트 계승).
- 문서는 한국어.

## 착수 순서 제약 (스펙에서 강제됨)

1. **Task 0 (PRE-GATE)가 코드보다 먼저.** 특히 G1(ka10095) 결과가 결정 #27(감시
   아키텍처)을 좌우 — 불발 시 스펙 재개정 후 재계획. G1~G4는 장중 실측.
2. **Task 1 (베이스 확장)이 트레이딩 구현보다 선행.** 정지 계약이 없으면
   TradingService에 즉흥 stop이 얹혀 규칙 2 위반(아키텍처·개발자 패널 공통 지적).
3. **Task 1B(market_calendar) → 순수(Task 2~3) → 어댑터/저장(Task 4~5) →
   부수효과(Task 6a/6b/6c) → 통합(Task 7).** Task 6은 세 모듈(entry/monitor/
   reconcile)이 독립 테스트·리뷰 가능하므로 분할(개발자 지적) — 6a/6b/6c는
   **상호 참조 없이 독립**(6c reconcile은 Task 2 enum·Task 4 OrderPort·Task 5 store만
   소비, 소비자는 Task 7뿐)이라 순서 강제 없이 병렬 진행도 가능. Task 1B도
   정지 계약과 무관한 독립 관심사로 분리(Task 1과 병렬 가능, Task 2 이전 완료).

## 파일 구조 (신규/수정 총괄)

```
backend/app/
  core/background_service.py   # 수정: 정지 계약(request_stop/StopMode) + started_at/finished_at·now 주입
  core/market_calendar.py      # 수정(Task 1B): 정적 공휴일 테이블 + is_market_hours/held_business_days/휴장일 판정 (스펙 §4·§6-4)
  domain/collection.py, domain/scoring/service.py  # 수정: now 주입점 (4서비스 대칭, Task 1)
  api/collect.py, api/score.py # 수정: status 응답 타임스탬프(Task 1) + 3자 배타 가드(Task 7)
  domain/trading/
    __init__.py      # 신규(빈)
    config.py        # 신규: TradingConfig (스펙 §6-2) + __post_init__ 검증
    models.py        # 신규: Order/Fill/TradePosition + PositionState/EntryPhase/ExitPhase/ExitReason/OrderStyle enum
    costs.py         # 신규: 매매비용 (시장별 세율·매수매도 비대칭·ETF 면세)
    ticks.py         # 신규: 호가단위(틱) 반올림 (순수 함수 — entry.py는 호출만, 트레이더 지적)
    selection.py     # 신규: 진입 후보 선정 (순수: 갭가드→필터→유동성→고정슬롯 사이징)
    exit_rules.py    # 신규: evaluate_exit (순수: 보유기간→손절→트레일링 선형보간→익절)
    reconcile.py     # 신규: reconcile_decide(순수) + 적용 오케스트레이션(주입 I/O)
    entry.py         # 신규: EntryExecutor (지정가→미체결감시→시장가 폴백)
    monitor.py       # 신규: PositionMonitor (폴링루프, 청산발행, 동시호가/VI 백오프)
    service.py       # 신규: TradingService (확장 BackgroundRunService 서브클래스)
  adapters/kiwoom/
    broker.py        # 수정: OrderPort 구현 (place_order/cancel_order/get_open_orders/get_quotes)
    orders.py        # 신규(선택): 호가구분 매핑·주문 바디 빌더 (broker.py 비대화 방지)
  domain/broker.py   # 수정: OrderPort 프로토콜 + MarketData/OrderRequest/OrderAck/OpenOrder dataclass
  store/models.py    # 수정: trade_runs/orders/fills/trade_positions ORM + analysis_runs.economist_fallback
  store/trading_store.py       # 신규: 주문/체결/포지션 리포지토리 + 미종결 조회
  api/trade.py                 # 신규: POST start/stop + GET status/positions
  api/security.py              # 수정: require_trade_token 의존성
  core/config.py               # 수정: api_trade_token·버그봉쇄 한도·유동성 임계 + 실전 스코프 검증자
  main.py                      # 수정: TradingService 조립 + 3자 배타 배선 + 라우터
backend/alembic/versions/
  0007_trading_tables.py       # 신규
  0008_analysis_economist_fallback.py  # 신규(별도 리비전 — 규칙 4)
```

---

### Task 0: PRE-GATE 장중 실측 (코드 없음, 코디네이터/사용자)

**목적:** 문서-실측 괴리를 코드 전에 차단(Phase 1·2 반복 사례). G1이 감시
아키텍처를 좌우하므로 최우선.

**증거:** `.superpowers/sdd/p5-pregate-G{1..4}.txt`.

- [ ] **G1 — ka10095 다종목 시세:** 세미콜론 구분자·1회 최대 종목 수·응답 리스트
  키·**현재가+최우선 매수/매도호가 필드 포함 여부**·부분 실패 계약·실제 소요시간.
  → 호가 미포함이면 별도 호가 TR 합성 필요, 다종목 불발이면 **결정 #27 재결정
  요청**(감시 주기 = 종목 수 × 1초).
- [ ] **G2 — 주문 TR:** kt10000/kt10001/kt10003 요청 바디 필드·호가구분 코드값·
  응답 주문번호 필드·실패 return_code/msg 형태·**주문/시세/잔고 레이트리밋 버킷
  분리 여부**(§11-5).
- [ ] **G3 — kt00018 행단위(기존 PRE-GATE #1):** stk_cd/pur_pric/cur_prc/evlt_amt
  실제 키·**avg_price 원 단위 정수 여부**. G2 후 모의 매수 1건으로 포지션 생성 필요.
- [ ] **G4 — 모의키→실전엔드포인트 대칭성:** **독립 스크립트·독립 httpx·발급 토큰
  즉시 revoke**(공유 TokenManager 오염 금지, 보안). 실전 base 조회 TR 1건만.
- [ ] **호가 단위(틱)·시장별 세율·공휴일 소스** 확정(스펙 §4 재확인 항목).
- [ ] 실측 결과를 CLAUDE.md §5 + 스펙 §4에 반영. **G1 불발 시 스펙 재개정 후 재계획.**

---

### Task 1: BackgroundRunService 확장 (정지 계약 + 타임스탬프 승격) — 선행

**목적:** 장중 상시 루프 + 외부 킬 스위치를 베이스에서 지원(§6-5). 이월된
승격(started_at/finished_at·now 주입)을 **4서비스 대칭까지 실제 완성**(§10 —
"Phase 6 착수 전 필수 해소"). 베이스 클래스 능력 추가만으로는 collection/scoring
API 응답에 타임스탬프가 노출되지 않으므로(현재 AnalysisService만 now 주입점 보유,
`/collect/status`·`/score/status`엔 started_at/finished_at 없음), 하위 3서비스와
상태 API까지 손대 대칭을 실현한다(아키텍처 패널 지적).

**Files:**
- Modify: `backend/app/core/background_service.py`(정지 계약 + started_at/finished_at·now)
- Modify: `backend/app/domain/collection.py`, `backend/app/domain/scoring/service.py`
  (now 주입점 도입 — AnalysisService와 대칭)
- Modify: `backend/app/api/collect.py`, `backend/app/api/score.py`
  (status 응답에 started_at/finished_at 노출)
- Test: `backend/tests/test_background_service.py`(수정/신규) + 3서비스 status 응답 테스트

> **참고(개발자 델타 지적):** ①정지 계약 ②4서비스 대칭 두 관심사는 모두
> `BackgroundRunService` 승격(스펙 §10 "정지 계약 확장과 함께 처리")의 일부라
> 한 태스크로 유지한다. 무관한 `market_calendar` 확장은 **Task 1B로 분리**했다
> (Task 6 분할과 동일 기준 — 상호 의존 없는 관심사는 쪼갠다).

---

### Task 1B: market_calendar 확장 (Task 2 이전 완료)

**목적:** 정적 공휴일 테이블 + 장운영시간·보유일수 판정. 정지 계약과 무관한
독립 관심사(개발자 델타 지적) — Task 3·6b가 소비하므로 순수 도메인(Task 2) 전에 완료.

**Files:**
- Modify: `backend/app/core/market_calendar.py` — **정적 공휴일 테이블(KRX 발표
  기준, 연 1회 갱신 + 임시공휴일 수동 갱신) + `is_market_hours(now)`·
  `held_business_days(entry_date, now)`·휴장일 판정 함수 추가**(스펙 §4·§6-4).
  날짜 판정은 순수 로직(주입 `now`). Task 3 `evaluate_exit`의 `held_business_days`,
  Task 6b monitor의 장운영시간·15:30 판정이 소비.
- Test: `backend/tests/test_market_calendar.py`(공휴일·held_business_days·15:30 경계값)

**Steps:**
- [ ] 공휴일 테이블(G0 실측 소스) + 판정 함수. 순수 경계값 전수 테스트.
- [ ] **패널:** (트레이더) 휴장일·장시간 정확성, (개발자) 순수성.

**Interfaces:**
- `StopMode` enum: `STOP_NEW_ENTRIES` | `LIQUIDATE_ALL`.
- `request_stop(mode: StopMode) -> None` — `asyncio.Event`류 신호 세팅(협조적).
- `_stop_requested() -> StopMode | None` — 서브클래스가 **폴링 사이클 경계에서** 확인.
- `started_at`/`finished_at` 필드 + 주입 가능한 `now` 콜러블(테스트용 결정성).

**Steps:**
- [ ] `StopMode` enum + `_stop_event`/`_stop_mode` 추가. `request_stop`이 세팅.
- [ ] `_on_accepted()`에서 `started_at` 세팅, `_execute()` finally에서 `finished_at`.
      기존 "예외 금지" 계약 유지(단순 대입만).
- [ ] **기존 3서비스 무영향 검증** — 정지 신호를 확인하지 않는 단발 `_run()`은
      동작 불변. collection/scoring/analysis 회귀 테스트 그린 유지.
- [ ] 원자적 구간 비선점 원칙을 docstring에 명문화(주문 발행 중 정지 무시).
- [ ] **패널:** (아키텍트) 승격이 3서비스 대칭인지, (개발자) 정지 계약이 첨가형
      확장으로 기존 계약을 안 깨는지, (트레이더/보안) 킬 스위치 시맨틱 정합.

---

### Task 2: 순수 도메인 1 — config·models·costs

**목적:** 트레이딩 값·타입·비용 계산의 단일 출처. 전부 순수·검증 포함.

**Files:**
- Create: `domain/trading/__init__.py`(빈), `config.py`, `models.py`, `costs.py`
- Test: `backend/tests/trading/__init__.py`, `test_config.py`, `test_models.py`, `test_costs.py`

**Interfaces:**
- `TradingConfig`(§6-2 전 필드) + `__post_init__` 검증(§6-2-c: 범위·대소관계
  `trailing_stop_pct ≤ trailing_stop_wide_pct` 등).
- `models.py`: `Order`/`Fill`/`TradePosition` dataclass + `PositionState`/`EntryPhase`
  /`ExitPhase`/`ExitReason`/`OrderStyle` enum. `TradePosition`은 `peak_price`·
  `trailing_active`·`entry_phase`·`exit_phase` 보유.
- `costs.py`: `round_trip_cost(market, buy_amount, sell_amount, config) -> Cost` —
  매수/매도 수수료 양쪽 + 매도 거래세(코스피 vs 코스닥) + ETF 면세 분기.

**Steps:**
- [ ] `TradingConfig` 스펙 §6-2 표와 1:1. 검증 실패 케이스 전수 테스트.
- [ ] enum·dataclass 정의. `TradePosition`과 기존 `broker.Position`(잔고 응답)의
      이름 분리 유지.
- [ ] `costs.py` 시장별 비대칭 — 코스피(거래세+농특세)/코스닥(거래세)/ETF(면세)
      경계값 테스트. 세율은 config 값(TBD → G0 실측 후 확정).
- [ ] **패널:** (트레이더) 세율 구조가 한국 시장과 일치, (개발자) DRY·검증 완결성.

---

### Task 3: 순수 도메인 2 — exit_rules·selection

**목적:** 자금 손실과 직결되는 판정. 경계값 전수 테스트가 핵심.

**Files:**
- Create: `domain/trading/exit_rules.py`, `selection.py`, `ticks.py`
- Test: `backend/tests/trading/test_exit_rules.py`, `test_selection.py`, `test_ticks.py`

**Interfaces:**
- `evaluate_exit(entry_price, current_price, peak_price, trailing_active,
  held_business_days, config) -> ExitDecision(reason, new_peak, new_trailing_active)`.
  우선순위 0.보유기간 → 1.손절 → 2.트레일링(선형보간 폭) → 3.익절(트레일링
  미활성 시만). peak 갱신·트레일링 래치·폭 보간 전부 함수 내부.
- `select_entries(candidates, held_symbols, deposit, config) -> list[EntryPlan]` —
  0.갭가드 → 1.감사/거래정지 필터 → 2.유동성 필터 → 3.고정슬롯 잔여 → 4.사이징
  (분모=max_positions, 수수료 버퍼, 내림) → 5.0주 스킵.
  **`candidates`(CandidateInput류)에 `avg_trading_value_krw` 필드를 포함**해 순수
  함수가 DB를 몰라도 유동성 필터가 가능하게 한다 — 이 값의 계산(저장 일봉
  close×volume 최근 N일 평균 조인)은 **Task 7 TradingService가 selection 호출
  직전에 수행**해 주입한다(트레이더 Minor — 순수 함수의 입력 경로 명시).
- `ticks.py`: `round_to_tick(price, direction) -> int` — KRX 가격대별 호가단위로
  반올림(순수). `entry.py`(Task 6a)는 이 함수를 **호출만** 한다 — 반올림 로직이
  부수효과 코드에 묻히지 않게 분리(트레이더 Minor). 호가단위 표는 G0 실측 확정.

**Steps:**
- [ ] `evaluate_exit` 경계값: 손절/익절 정확 임계, 트레일링 활성화 래치, **선형
      보간 연속성**(전환점 불연속 없음 — 트레이더 v3 지적), 보유기간 초과, 손절
      vs 트레일링 동시 성립 시 라벨링.
- [ ] `select_entries`: 갭가드 제외, 고정슬롯 분모(후보 부족 시 현금), 수수료 버퍼.
- [ ] **패널:** (트레이더) 선형보간·고정슬롯이 매매상 올바른지, (개발자) 순수성·
      경계 커버리지, (아키텍트) 판정/집행 분리 유지.

---

### Task 4: 포트 확장 + 키움 어댑터 (OrderPort)

**목적:** 주문·미체결·다종목 시세를 브로커 중립 포트 뒤에 구현.

**Files:**
- Modify: `domain/broker.py`(OrderPort + MarketData/OrderRequest/OrderAck/OpenOrder)
- Modify: `adapters/kiwoom/broker.py`; Create(선택): `adapters/kiwoom/orders.py`
- Test: `backend/tests/kiwoom/test_broker_orders.py`(단위, 기존 kiwoom 관례) +
  `backend/tests/live/`에 라이브 마커(기본 deselect) — 지정가(00)/시장가(03) 매도
  개별 접수 확인(트레이더 Task 8 연계)

**Interfaces:**
- `OrderPort`: `get_quotes`/`place_order`/`cancel_order`/`get_open_orders`(§5).
- `MarketData(quote: Quote, bid: int, ask: int)` — 기존 `Quote` 합성.
- 도메인은 `OrderStyle.LIMIT/MARKET`만. 키움 코드값(00/03)은 어댑터 내부 매핑.

**Steps:**
- [ ] `OrderPort` 프로토콜·dataclass 정의. `KiwoomBroker`가 `BrokerPort`+`OrderPort` 둘 다 만족.
- [ ] G1/G2 실측 필드로 주문 바디 빌더·응답 파싱. `get_quotes`는 G1 결과에 따라
      일괄/순차(포트 뒤 은닉).
- [ ] **응답 바디만 반환, Authorization 헤더/토큰 미포함**(보안 C1). 레이트리밋
      기존 per-TR 리미터 재사용.
- [ ] **패널:** (아키텍트) 브로커 중립성·ISP, (보안) 토큰 비노출, (트레이더) 호가/틱 처리.

---

### Task 5: 저장소 + 마이그레이션 0007·0008

**Files:**
- Modify: `store/models.py`; Create: `store/trading_store.py`,
  `alembic/versions/0007_trading_tables.py`, `0008_analysis_economist_fallback.py`
- Test: `backend/tests/store/test_trading_store.py`(기존 store 관례)

**Interfaces (컬럼 단위 명세 — Phase 3/4 관례):**
- `trade_runs`: `id` PK auto, `started_at`/`finished_at` tz, `status`(16),
  `stopped_by_kill_switch` bool default false, `kill_switch_mode`(16, nullable).
- `trade_orders`(접두 일관 — 구현 정합): `id` PK auto, `trade_run_id` Integer FK(trade_runs.id, **RESTRICT**),
  `trade_position_id` Integer FK(trade_positions.id, RESTRICT, **nullable**) — **이
  주문이 속한 포지션**(개발자 델타 신규 — reconcile 분기 ②가 symbol 매칭이 아니라
  명시적 연결로 판단, realized_pnl 계산이 포지션→주문 조회 가능), `order_no`(32)
  브로커 주문번호, `symbol`(16), `side`(4: buy/sell), `order_style`(8:
  limit/market), `req_price` Integer, `req_qty` Integer, `status`(16),
  `resp_body` JSON(응답 바디 원문 — **Authorization 헤더/토큰 제외**), `created_at` tz.
- `trade_fills`: `id` PK auto, `order_id` Integer FK(orders.id, RESTRICT), `fill_price`
  Integer, `fill_qty` Integer, `filled_at` tz. (부분체결 다건 → order:fills = 1:N)
- `trade_positions`: `id` PK auto, `trade_run_id` FK(RESTRICT), `symbol`(12 —
  기존 InstrumentRow 관례), `name`(64), `market`(8 — §7 비용 계산, T2 트레이더), `state`(16: PositionState), `entry_phase`(20, nullable),
  `exit_phase`(20, nullable), `entry_price` Integer, `qty` Integer,
  `peak_price` Integer, `trailing_active` bool, `exit_price` Integer(nullable),
  `exit_reason`(20, nullable), `realized_pnl` Integer(nullable, **비용 반영 — Task 6b
  청산 시 costs.round_trip_cost로 계산**), `entered_at`/`closed_at` tz.
- FK 방향: `orders`→`trade_runs`, `orders`→`trade_positions`(nullable),
  `fills`→`orders`, `trade_positions`→`trade_runs`. 전부 **non-CASCADE(RESTRICT)**
  — 감사 자산 보존(기존 AnalysisRunRow 관례 정합).
- `trading_store`: 미종결 포지션 조회(reconcile용)·주문/체결 기록·상태 전이·latest.
- 0008(별도): `analysis_runs.economist_fallback` bool default false(§10).

**Steps:**
- [ ] ORM + upsert/전이 리포지토리. non-CASCADE FK(기존 AnalysisRunRow 관례 정합).
- [ ] `0007`/`0008` 분리(규칙 4). up/down 마이그레이션 테스트.
- [ ] **패널:** (아키텍트) 테이블 패턴 일관성·CASCADE, (보안) 감사 완결성.

---

### Task 6a: 진입 집행 (entry.py)

**목적:** 지정가→미체결감시→시장가 폴백 상태기계 집행.

**Files:**
- Create: `domain/trading/entry.py`
- Test: `backend/tests/trading/test_entry.py`(OrderPort는 fake 주입)

**Interfaces:**
```python
class EntryExecutor:
    def __init__(self, orders: OrderPort, config: TradingConfig,
                 check_order_caps: Callable[[int], None],
                 persist_phase: PersistPhase | None = None,  # fail-closed(발주 전)
                 on_order: OnOrder | None = None,            # 격리(발주 후 감사)
                 sleep=..., now=...): ...
    async def execute(self, plan: EntryPlan, ask: int) -> EntryOutcome: ...
    # EntryOutcome: position | None + failure_reason + requires_reconcile(구조화)
```
(구현 반영 동기화 — 아키텍트 Minor. ask는 호출자가 get_quotes로 확보해 전달,
시장가 폴백 직전에만 내부 재조회. store는 콜백 2개 뒤로 격리 — Global
Constraints "domain/trading은 store 임포트 금지".)
- `check_order_caps(amount_krw)`는 **주문 발행 직전** 호출(초과 시 예외로 거부) —
  단건 주문 상한을 `place_order` 바로 앞에서 검증(보안 Important, §8-1 "발주 직전").
  구현체(누적 카운터+상한)는 Task 7이 주입.
- 지정가(LIMIT_SUBMITTED) → `limit_order_timeout_sec` 미체결 → 취소
  (CANCEL_REQUESTED) → 시장가(MARKET_SUBMITTED). 지정가는 `ticks.round_to_tick` 호출.
- 부분체결 = 체결분만 포지션 인정, 잔량 취소.

**Steps:**
- [ ] 상태 전이 enum 룩업(if 중첩 금지). fake OrderPort로 폴백·부분체결 경로 테스트.
- [ ] `check_order_caps`가 `place_order` 직전 호출됨을 테스트로 고정.
- [ ] **패널:** (트레이더) 미체결·부분체결 처리, (개발자) 상태기계·순수/부수 경계,
      (보안) 단건 상한 훅 위치.

---

### Task 6b: 감시 루프 (monitor.py)

**목적:** 폴링 → 청산 판정 → 청산 주문 발행 + 실현손익 기록.

**Files:**
- Create: `domain/trading/monitor.py`
- Test: `backend/tests/trading/test_monitor.py`(fake OrderPort/store 주입)

**Interfaces:**
```python
class PositionMonitor:
    def __init__(self, orders: OrderPort, config: TradingConfig,
                 calendar, check_order_caps: Callable[[int], None],
                 persist_position: Callable[..., None],  # fail-closed — Task 7이 store 연결
                 on_order: OnOrder | None = None): ...   # 격리 — 발주 후 감사
    async def poll_once(self, positions: list[TradePosition], now) -> list[ExitAction]: ...
```
- **store 통짜 주입 금지**(아키텍트 P5-T6a #2): 6a와 동일한 콜백 주입 패턴 —
  Global Constraints("domain/trading은 store 임포트 금지")를 코드 레벨로 강제하고
  ISP(필요한 좁은 계약만 노출)를 유지한다. persist(fail-closed, 주문 전)/
  on_order(격리, 주문 후) 비대칭 계약도 6a와 동일. **6b 착수 시 6a의
  `_submit`/`_audit`(persist→발주→감사 3단 쌍)를 공용 헬퍼(예: `execution.py`)로
  추출해 재사용 검토** — 두 모듈이 같은 트리오를 손으로 중복 구현하지 않도록.
- `get_quotes` 1회 → 각 포지션 `evaluate_exit`(held_business_days는 `calendar`로
  계산해 주입) → 청산(§6-2-b: 손절/트레일링 즉시 시장가, 익절 5초 지정가).
- **청산 체결 후 `costs.round_trip_cost`로 `realized_pnl` 계산해 저장**(트레이더
  Important — costs 소유 명시).
- 조회 실패 구분(`list_instruments` state로 거래정지 vs 네트워크), 동시호가
  (15:20~15:30)·VI 백오프, 장운영시간 밖 중지·15:30 정상 반환(`calendar` 사용).
- EXIT_FAILED 침묵 금지 → 상태 노출.
- **⚠️ 상/하한가 ka10095 응답 형태(P5-T4 broker-api 이월, PRE-GATE 후보):**
  상한가는 매도호가(sel_bid), 하한가는 매수호가(buy_bid)가 legit하게 소진될 수
  있다는 가정으로 어댑터가 편측 호가 0 행을 유지하는데(제외 안 함), 실제
  상/하한가 종목의 ka10095 응답 형태는 미실측 — 기회가 되면 실측 확인.
- **⚠️ get_open_orders 예외 경계(P5-T4 아키텍트 이월):** 어댑터는 ka10075
  cont-yn=Y(미체결 다중 페이지 — 페이지당 행 수 미실측)에서 fail-loud
  BrokerError를 던진다. monitor/reconcile은 이를 **감시 루프 전면 중단이
  아니라 경고+재시도로 흡수**할 것(조회 실패 처리와 동일 계열).
- **⚠️ trailing_active 계약(P5-T3 트레이더 이월):** `evaluate_exit`의 익절
  백스톱은 입력 `trailing_active`가 **DB 영속값 그대로(직전 관측 상태)**라는
  계약에 의존한다 — monitor가 매 폴링마다 재계산해 넘기면(new_active와 동치)
  백스톱이 통합 지점에서 조용히 재사(도달 불가)한다. `TradePosition.
  trailing_active`를 그대로 전달하는 fake-store 왕복 통합 테스트 필수.

**Steps:**
- [ ] 청산 사유별 체결 방식, 실현손익 계산, 실패 구분, 백오프.
- [ ] trailing_active DB 영속값 그대로 전달 — fake store 왕복 통합 테스트.
- [ ] **패널:** (트레이더) 청산 체결·비용 반영·**trailing_active 계약 재검증**,
      (개발자) 순수/부수 경계, (아키텍트) 캘린더 재사용, (보안) 실패 침묵 금지.

---

### Task 6c: 재기동 대조 (reconcile.py)

**목적:** 재기동 시 DB↔브로커 상태 대조로 고아 포지션·주문 복구.

**Files:**
- Create: `domain/trading/reconcile.py`
- Test: `backend/tests/trading/test_reconcile.py`

**Interfaces:**
```python
def reconcile_decide(db_positions, broker_open_orders, broker_balance,
                     in_entry_window: bool) -> list[ReconcileAction]: ...  # 순수
async def apply_reconcile(actions, orders: OrderPort,
                          persist_position, on_order): ...  # 오케스트레이션 —
# store 통짜 주입 금지, 6a 콜백 패턴 동일(아키텍트 P5-T6a #2)
```
- 6분기(§6-6): ①체결완료→ENTERED ②미체결생존→감시재개(`orders.trade_position_id`
  ↔`get_open_orders` order_no로 **명시적 연결** 판단, symbol 매칭 아님 — 개발자
  델타) ③고아취소(CANCEL_REQUESTED)→ENTRY_FAILED ④EXITING청산완료→CLOSED
  ⑤EXITING익절지정가 생존→즉시재평가 ⑥DB무·브로커유→경고. 시장가 미확정은 ①②④로 흡수.
- 진입 창 경계: `in_entry_window=False`면 미체결 취소만, 시장가 재발주 금지.
- 순수 판정(`reconcile_decide`)과 오케스트레이션(`apply_reconcile`) 이름으로 분리.

**Steps:**
- [ ] `reconcile_decide` 6분기 순수 전수 테스트 + 진입 창 경계 + 시장가 미확정 흡수.
- [ ] **재기동 시 EXITING 복구 최우선**(보안 P5-T6b #4): monitor의 _pending
      (미확정 청산 주문 추적)은 인메모리라 재시작 시 소실 — DB의 EXITING
      포지션은 poll_once의 ENTERED 필터에도 안 걸리므로 reconcile ④⑤(+시장가
      미확정 흡수)가 잡지 못하면 **완전 고아**(추가 감시 전무)가 된다. Task 7
      _run() 진입부 reconcile이 EXITING을 잔고 대사로 우선 처리하는 것이
      이 갭의 유일한 안전망임을 테스트로 고정할 것.
- [ ] **패널:** (아키텍트) 시작조건 통합, (트레이더/보안) 고아 주문 방지.

---

### Task 7: TradingService + API + 앱 조립

**Files:**
- Create: `domain/trading/service.py`, `api/trade.py`
- Modify: `api/security.py`, `core/config.py`, `main.py`,
  **`api/collect.py`·`api/score.py`(3자 배타 가드 — 절반 누락 방지, 아키텍처 Minor)**
- Test: `backend/tests/trading/test_trading_service.py`, `backend/tests/test_trade_api.py`

**Interfaces:**
- `TradingService`(확장 BackgroundRunService): `_run()` 진입부 reconcile(§6-6) →
  진입 창 판단 → 감시 루프. `LIQUIDATE_ALL` 종료 조건 + 15:30 강제 종결(§8-1-b).
  **selection 호출 직전 유동성 데이터(`avg_trading_value_krw`) 조인 주입**(Task 3
  순수 함수 입력 경로), **`check_order_caps` 구현체(누적 카운터+단건/일일 상한)
  생성해 EntryExecutor·PositionMonitor에 주입**(보안 Important — Task 6 훅의 실제 구현).
- `TradingProgress`(§6-7): run_id/status/started_at/finished_at/positions_count/
  warnings/daily_order_count/daily_order_krw/kill_switch.
- API: `POST /trade/start`·`/trade/stop`(require_trade_token) + `GET /trade/status`
  ·`/trade/positions`(개방, §8-2 이월 표기).
- `require_trade_token`(api_trade_token, 미설정 시 api_write_token 폴백).
- 실전 스코프 검증자: `kiwoom_mock=False` → `api_trade_token is not None and !=
  api_write_token` 강제(§6-2-c).
- 버그 봉쇄(§8-1): 단건/일일 주문 상한, 재진입 쿨다운, **3자 양방향 배타 배선**
  (main.py + api/collect.py + api/score.py 가드).

**Steps:**
- [ ] `TradingService` 조립 — reconcile 선행, 킬 스위치 종료 조건, 유동성 조인·
      `check_order_caps` 주입. **PositionMonitor는 trade_run당 새 인스턴스 +
      단일 루프 순차 호출**(P5-T6b 아키텍트 #5 — 인메모리 _pending/카운터가
      거래일 경계를 넘으면 장전 오판 EXIT_FAILED 위험).
- [ ] **`_run()` 종료(finally) 시 `trade_runs.stopped_by_kill_switch`/`kill_switch_mode`를
      `request_stop` 여부·모드로부터 기록**(보안 Important — 킬 스위치 감사, 요약과
      스텝 일치). **finish_run은 모든 종료 경로(정상/예외/킬스위치)에서 try/finally로
      보장** — 미호출 시 status='running' 좀비 행이 감사 질문에 답 못 함(P5-T5
      보안 forward-pointer). entry_price는 체결 확정 시 1회만 기록하는 불변식도
      호출부가 준수(감사 재구성 전제 — P5-T5 보안 Minor).
- [ ] API 4종 + 인증. 버그 봉쇄 한도는 Task 6의 `check_order_caps`로 발주 직전 재검증.
      **`check_order_caps(amount_krw, side)` 구현 계약(P5-T6b 트레이더 C2/
      아키텍트 #4):** 단건·일일 **금액/건수 상한 차단은 매수(BUY)에만** 적용
      — 매도(SELL: 청산·킬스위치)는 기록/카운트만 하고 **절대 차단하지
      않는다**(리스크 축소 주문이 자기 안전장치에 막혀 EXIT_FAILED로 고정되는
      역설 방지; 매도 폭주는 monitor의 재시도 상한 3회+pending 중복 가드가
      구조적으로 봉쇄). 캡 소진 시 동작 = 신규 진입만 정지.
      **일일 누적 상한 카운터는 추정 금액(ask×qty)으로 선누적하되, 시장가 상방
      슬리피지가 체계적이면 실측 체결액(잔고 pur_pric) 사후 보정 검토**(P5-T6a
      보안 forward-pointer — 추정치 누적만으로 max_daily_order_krw 실질 초과 가능).
      **콜백 계약: on_order/persist는 sync — 블로킹 DB I/O가 이벤트 루프를 멈춰
      다른 종목 손절 감시를 지연시키지 않도록 asyncio.to_thread 경유로 연결.
      `check_order_caps`도 동일**(아키텍트 Minor — 누적 카운터가 DB 기반이 되면
      같은 블로킹 위험).
- [ ] **`EntryOutcome.requires_reconcile=True` 시 즉시 미니 reconcile**(잔고
      대사) 트리거 — 재기동 대기 금지(P5-T6a 트레이더 I2: 조회 히컵 하나가
      온종일 무감시 노출을 만들면 안 됨). 문자열 사유 매칭 분기 금지(개발자 #2
      — 구조화 필드가 계약). **미니 reconcile 완료 전에는 ENTRY_FAILED로
      persist 금지**(아키텍트 #1): ENTRY_FAILED는 §6-6 재기동 스캔 집합 밖 —
      마지막 EntryPhase(CANCEL_REQUESTED/MARKET_SUBMITTED)를 유지한 채 미니
      reconcile이 최종 상태를 결정해야, 미니 reconcile 크래시 시에도 다음
      재기동 스윕이 해당 포지션을 다시 잡는다.
- [ ] **진입 직후 잔고 대사(kt00018)로 수량·평단 확정** — entry_price 추정치
      (지정가=발주가/시장가=fresh ask)와 부분체결 확정 수량(취소 직전 폴
      스냅샷, 최대 1 interval 낡음 — 트레이더 I4)을 실측값으로 교정하고,
      잔고 0이면 유령 포지션 즉시 해소(C1 잔여 리스크 봉쇄 — ⓒ 방어선).
- [ ] **requires_reconcile=True인 CLOSED는 잔고 교차 검증 후에만 최종 확정
      (하드 게이트 — 보안 P5-T6c #2)**: pending 지연 확정·reconcile 시드
      경유 CLOSED는 잔고에 해당 심볼이 실제로 없는지 kt00018로 확인하고,
      잔고에 남아 있으면 포지션을 재오픈(ENTERED·잔고 수량)해 감시로 복귀
      — "다음 대사가 교정한다"는 기대를 코드 강제로 승격. 테스트 필수.
- [ ] **reconcile 적용 직후 CANCEL_AND_SETTLE_ENTRY·CANCEL_AND_REWATCH 심볼
      잔고 재조회로 최종 수량 재확정**(트레이더 P5-T6c I3): decide의 잔고
      스냅샷과 실제 취소 실행 사이에 추가 체결 레이스가 가능 — settle된
      수량이 실보유보다 작으면 초과분이 당일 감시 밖에 방치된다.
- [ ] 3자 배타 양방향 배선(collect/score API 가드 포함) + 실전 스코프 검증자.
- [ ] **패널:** (보안) 인증·스코프 강제·킬스위치 가용성·감사 기록 + **caps
      구현체가 SELL을 실제로 차단하지 않는지 재검증**(P5-T6b 보안 델타
      포워드 포인터 — 계약은 시그니처·문서·테스트로만 고정된 상태), (아키텍트)
      3자 배선·수명주기, (트레이더) 진입 창·종료 조건 + caps 매도 비차단
      실구현 확인(P5-T6b 트레이더 C2 동일 항목), (개발자) 조립 명료성.

---

### Task 8: 라이브 스모크 / 수용 검증 (장중, 코디네이터 직접)

**목적:** 모의 계좌 실주문 end-to-end. 코드 변경 없음(발견 시 별도 태스크).

**Steps:**
- [ ] 모의 계좌 예수금 확인 → `POST /trade/start` → 진입 1건 체결 → 감시 폴링 →
      익절/트레일링 청산 경로 실증. 증거 `.superpowers/sdd/p5-task-8-*.txt`.
- [ ] **시장가 청산 경로(손절/트레일링) 강제 검증**(트레이더 Important) — 자연
      하락을 기다리지 않고 `stop_loss_pct`를 임시로 매우 타이트하게 설정해 즉시
      시장가 매도(kt10001, 호가구분 03)를 강제 트리거·체결 확인. Task 4 라이브
      마커도 지정가(00)/시장가(03) 매도 접수를 개별 확인.
- [ ] 킬 스위치(STOP_NEW_ENTRIES/LIQUIDATE_ALL) 실동작 + 감사 컬럼 기록 확인.
- [ ] 재기동 reconcile — 감시 중 프로세스 종료 후 재기동 시 포지션 복구 확인.
- [ ] 실측 팩트를 CLAUDE.md §5 + 스펙에 반영. Phase 5 회고록 작성(규칙 4).

---

## 계획 자체 점검 (self-review)

### 스펙 §X → Task 커버리지 매트릭스

| 스펙 항목 | Task |
|---|---|
| §4 PRE-GATE G1~G4·틱·세율·공휴일 실측 | Task 0 |
| §6-5 정지 계약 + §10 타임스탬프 4서비스 대칭 | Task 1 |
| §4·§6-4 market_calendar 정적 공휴일·held_business_days | Task 1B |
| §6-2 TradingConfig+검증, §6-2 enum·모델, §7 costs 비대칭 | Task 2 |
| §6-2 evaluate_exit(선형보간·보유기간), §6-3 select_entries, 틱 반올림 | Task 3 |
| §5 OrderPort·MarketData, §9 토큰 헤더 미저장 | Task 4 |
| §9 4테이블(컬럼 명세·RESTRICT), §10 0008 | Task 5 |
| §6-3 진입 집행·단건상한 훅, §6-2-b 청산 체결·costs 실현손익, §6-6 reconcile | Task 6a/6b/6c |
| §6-5 킬스위치+감사, §6-7 TradingProgress, §8-1 버그봉쇄·3자배선, §6-2-c 실전 스코프 | Task 7 |
| §11 시장가 청산·킬스위치·reconcile 라이브 실증 | Task 8 |

### 점검 결과

- **착수 순서:** G1 게이트(감시 아키텍처 좌우) → 베이스+캘린더 확장(선행) →
  순수 → 어댑터/저장 → 부수효과(6a/6b/6c) → 통합 → 라이브. 스펙 강제 순서 준수.
- **순수/부수 분리:** costs/ticks/selection/exit_rules/reconcile_decide는 주입값만.
  I/O(캘린더·유동성 조인·브로커·DB)는 호출자가 값 변환해 주입. 경계값 테스트가
  자금 리스크 로직을 커버.
- **소유권 공백 해소(패널 지적):** 킬스위치 감사=Task 7 finally, 단건상한
  훅=Task 6a/6b(check_order_caps 주입)+Task 7(구현), costs 실현손익=Task 6b,
  유동성 조인=Task 7, 틱 반올림=Task 3 ticks.py, market_calendar=Task 1.
- **상태기계:** EntryPhase/ExitPhase enum + reconcile 6분기 룩업 → if 중첩 회피.
- **의존성 0 신규.** 마이그레이션 0007(트레이딩)/0008(analysis) 분리(규칙 4).
- **미결(스펙 이월):** 트레일링 계수 캘리브레이션(백테스트 인프라 필요), 보유 중
  신호 재검증, 실전 전환 게이트(수동 승인·외부 추론 재평가). 전부 P5 비범위 명시.
- **리스크·컨틴전시:**
  - G1(다종목 시세) 불발 → Task 4 이후 재작업(스펙 §11-1). Task 0에서 조기 차단.
  - **G2에서 주문/시세/잔고 레이트리밋 버킷이 공유로 판명 시 → "주문 우선순위"
    후속 태스크 추가 필요**(스펙 §11-5 손절 지연 리스크, 결정 #14 이관 항목). 버킷
    분리면 불필요. Task 0 실측 후 판단(보안 Minor).
