# Phase 7 Task 1 — 관리 포지션 최신 mark 가격 영속 회고

## 요청과 기존 상태

Phase 7 대시보드는 트레일링 방어선용 `peak_price`를 현재가로 표시하면 안 된다.
기존 포지션은 진입가·수량·고점·청산 상태만 저장했고, monitor가 이미 얻은
현재 시세와 관측 시각을 재사용할 영속 표면이 없었다.

## 설계 판단

- `mark_price`와 `marked_at`은 nullable로 추가했다. 과거 행은 관측 근거가
  없으므로 migration에서 추정 backfill하지 않는다.
- `peak_price`는 단조 증가하는 트레일링 기준으로 그대로 유지한다. mark는
  최신 관측값이므로 하락할 수 있으며, 두 의미를 혼합하지 않는다.
- domain `PositionMonitor`에는 저장소가 아닌 mark 저장 준비 콜백을 주입했다.
  `TradingService`가 **예약 시점** symbol→position id를 캡처한 작업을 만들고,
  worker는 그 불변 id만 `TradingStore.update_position_mark`에 전달한다.
- 유효하게 받아 방어선 평가에 쓴 현재가만 동일한 주입 시각과 함께 저장한다.
  시세 조회 실패에는 이전 mark를 비우거나 새 시각을 쓰지 않는다.
- mark 저장은 worker thread로 격리해 방어선·청산 주문을 기다리게 하지 않는다.
  PostgreSQL에서는 전용 1-slot pool의 checkout·연결·lock·statement timeout을
  짧게 제한한다. run 종료는 worker를 실제 완료까지 회수하고 mark engine을
  기본 engine보다 먼저 dispose한다.
- 늦게 끝난 worker의 오래된 관측은 단일 조건부 SQL UPDATE의 `marked_at`
  fence로 최신 mark를 덮지 못한다. 저장 실패는 경고와
  `persist position mark failed` 검색 가능 로그를 남기되, 이미 계산한
  방어선·청산 판단과 주문 흐름은 계속 진행한다.

## 변경 파일과 위치

- `backend/alembic/versions/0015_trade_position_mark.py`: nullable BIGINT/TIMESTAMPTZ
  컬럼의 upgrade/downgrade.
- `backend/app/store/models.py`, `backend/app/store/trading_store.py`:
  row 매핑과 가격·시각 원자 갱신 계약(양수·timezone-aware 검증 포함).
- `backend/app/domain/trading/models.py`, `monitor.py`, `service.py`:
  mark 도메인 값, 비동기 격리·terminal worker 회수, 불변 id 캡처 배선.
- `backend/app/main.py`: mark 전용 engine을 runtime 종료에서 기본 engine보다
  먼저 dispose.
- `backend/tests/store/test_models_migration.py`, `test_trading_store.py`,
  `backend/tests/trading/test_monitor.py`, `test_service.py`: migration 왕복,
  저장 validation, monitor 격리, service id 매핑 회귀.

## 검증

- RED: `cd backend && uv run pytest tests/store/test_models_migration.py
  tests/store/test_trading_store.py tests/trading/test_monitor.py
  tests/trading/test_service.py -q` → 43 failed, 71 passed. 예상대로 0015
  revision·`update_position_mark`·mark 저장 port·서비스 콜백 부재가 원인이었다.
- 리뷰 보완 RED: 늦은 mark가 최신 mark를 덮음, 느린 저장이 청산을 지연함,
  logical-naive 시각 수용, run 전환 id 오염, worker cancel만으로 종료 처리,
  PostgreSQL timeout/pool 설정 부재를 각각 재현해 실패를 확인했다.
- GREEN(계획서 명령): `cd backend && uv run pytest
  tests/store/test_models_migration.py tests/store/test_trading_store.py
  tests/trading/test_monitor.py -q` → 최종 재실행 결과는 보고서에 기록한다.
- 추가 배선 회귀: 같은 명령에 `tests/trading/test_service.py`를 포함 →
  최종 `120 passed in 2.66s`.
- FastAPI lifespan 포함 회귀: `154 passed in 3.93s`, 기존
  Starlette `TestClient` deprecation warning 1건.
- live 테스트·브로커·키움 API·주문 호출은 실행하지 않았다.

## 리뷰

- senior-developer: Critical/Important 없음. Minor인 snapshot API 의미는 mark가
  전용 fence 경로에서만 갱신됨을 문서화해 해소했다.
- senior-trader: Critical/Important/Minor 없음.
- architecture-expert: logical-naive 시각, run 경계 worker, atomic fence,
  worker terminal 회수, 전용 pool·연결 timeout을 지적했다. 모두 수정 후
  최종 Critical/Important 없음으로 승인했다.
- security-expert: 동기 DB write, run 경계, timestamp fence, `SET LOCAL`
  bind 문법, shutdown lifecycle을 지적했다. 모두 수정 후 최종
  Critical/Important 없음으로 승인했다.
