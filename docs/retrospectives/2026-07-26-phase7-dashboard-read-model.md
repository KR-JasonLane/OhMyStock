# Phase 7 Task 2 회고 — 관리매매 성과 SQL 읽기 모델

## 1. 요청과 기존 상태

Phase 7 조회 전용 대시보드가 관리매매 성과를 안전하게 표시하도록, 기간·환경별
성과를 계산하는 domain 모델과 SQL 읽기 모델을 만들었다. 기존에는
`trade_runs`/`trade_positions`에 실행 환경, 포지션 상태, 청산 손익, mark 가격은
저장돼 있었지만, KST 청산 귀속·손익 상태·손상 격리·환경 분리를 한 번에
제공하는 읽기 모델은 없었다.

## 2. 설계 판단

- `backend/app/domain/dashboard/models.py`는 SQLAlchemy 행·API 문자열·브로커
  타입을 받지 않는 dataclass와 순수 집계 함수만 둔다. store가 원시 행을
  검증해 포지션 스냅샷으로 바꾸고, domain은 기간 귀속·손익·상태만 계산한다.
- 청산 성과는 진입일이 아니라 `closed_at`의 KST 날짜로 귀속한다. SQL은
  `coarse_utc_bounds`로 범위를 좁히고 domain이 `DashboardPeriod.includes`로
  다시 판정한다.
- 주문·체결 행이 아니라 `TradePositionRow` 하나를 거래 한 생애주기로
  계산한다. 누적 곡선은 `(closed_at, id)` 오름차순, 최근 거래는 내림차순이다.
- 열린 포지션은 `entered`/`exiting`/`exit_failed`만 표시한다. mark가 없거나
  10분 초과 stale이면 평가손익을 0으로 채우지 않는다. 일부만 유효하면
  `partial`, 전부 불가하면 `unavailable`로 표시한다. 미래 `marked_at`은
  시계 오염으로 보고 평가·freshness 모두에서 fail-closed로 제외한다.
- enum·가격·수량·상태/종료시각 불일치 손상 행은 정상 집계에서 제외하고
  `corrupted_row_count`로 남긴다. 기간 밖 `closed_at`을 가진 비종결 상태도
  경고에서 사라지지 않게 읽는다.
- `DashboardStore`는 `TradePositionRow`와 `TradeRunRow`만 명시적으로 join하고
  환경을 SQL bind parameter로 분리한다. `resp_body`, 계좌 식별자, BrokerPort,
  키움 API와 주문 경로를 읽거나 호출하지 않는다.

## 3. 비용 confidence 결정

Phase 7 설계 §5.3은 완전한 비용 근거가 없을 때 `unavailable`을 요구했으나,
Task 2 구현 계획은 현 비용 모델이 반영된 손익을 `estimated`로 지시했다.
실제 스키마에는 브로커 비용과의 완전 대사 여부를 판별하는 컬럼이 없다.

사용자는 2026-07-26에 **`cost_basis=estimated`를 지배 계약으로 확정**했다.
현재 비용 모델이 반영된 `realized_pnl`은 표시하되, 실제 브로커 비용과 완전
대사된 값처럼 표현하지 않는다. 비용 계산 자체가 불가능한 경우만
`unavailable`이다. 이 결정을 `test_models.py`의 순수 회귀로 고정했고 후속 UI는
"비용 추정 반영"을 명시해야 한다.

## 4. 변경 파일과 정확한 위치

- `backend/app/domain/dashboard/__init__.py`: 대시보드 domain 공개 타입.
- `backend/app/domain/dashboard/models.py`: 기간, 포지션 스냅샷, 요약,
  curve/recent/freshness/warning 출력과 순수 집계 (`build_dashboard_overview`).
- `backend/app/store/dashboard_store.py`: 명시 join, 환경 필터, KST coarse
  prefilter/재검증, 손상 행 격리와 mark stale 기준 10분.
- `backend/tests/dashboard/test_models.py`: KST 귀속, 생애주기 집계,
  승률·수익률, cost basis, stale/missing/future mark, partial/unavailable,
  손상·불완전 행 회귀.
- `backend/tests/store/test_dashboard_store.py`: 환경 혼합, KST 재검증,
  열린 상태, 정렬, 손상 행과 broker 비호출 경계 회귀.
- `docs/specs/2026-07-26-phase7-web-dashboard-design.md`: 사용자 결정에 맞춰
  `estimated` 기본값과 `realized_pnl` 부재 시 `unavailable` 조건을 명시.

## 5. 검증

TDD RED로 `app.domain.dashboard` 및 `app.store.dashboard_store` 부재에 따른
collection 실패를 각각 관측했다. 빈 정상 합계, 실현손익 누락, stale freshness,
future mark, 기간 밖 비종결 손상 행도 기대 assertion 실패 뒤 최소 구현으로
GREEN을 확인했다.

```bash
cd backend
uv run pytest tests/dashboard/test_models.py tests/store/test_dashboard_store.py tests/store/test_trading_store.py -q
```

결과: `37 passed in 0.51s`. `git diff --check`도 통과했다. 실제 키움 API,
주문, live 테스트는 실행하지 않았다. `ruff`는 현재 uv 환경에 실행 파일이 없어
실행하지 못했다.

## 6. 리뷰

- senior-developer: 비용 confidence 충돌을 지적했다. 사용자 결정 반영,
  `realized_pnl` 부재 시 `unavailable` 경로, 설계 SSOT 갱신 뒤 재검토
  Critical/Important 0으로 승인했다.
- senior-trader/security-expert: future mark가 평가에 포함되는 Important를
  지적했다. TDD로 fail-closed 처리 후 각각 Critical/Important 0 재검토를
  통과했다.
- architecture-expert: stale mark freshness 누락과 기간 밖 비종결 손상 행
  누락을 지적했다. stale timestamp 보존과 비종결/unknown 상태 검증으로
  보완해 Critical/Important 0 재검토를 통과했다.
- broker adapter/TR/인증/주문/페이지네이션을 바꾸지 않아 broker-api-expert는
  적용하지 않았다.

네 상시 관점 모두 최종 Critical/Important 0으로 승인했다. 커밋은 수행하지
않는다.
