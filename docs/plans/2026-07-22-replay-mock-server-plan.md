# 리플레이 목 서버 구현 계획 (R1~R7)

- 작성일: 2026-07-22 (v1)
- 원 스펙: `docs/specs/2026-07-22-replay-mock-server-design.md`(5자 리뷰 승인)
  — 본 계획서는 스펙 §11(패널 확정 태스크 분해)의 승격이며, 태스크 상세만
  추가한다. 계약 근거는 전부 스펙 §번호로 참조.
- 리뷰 규칙: 태스크별 4-에이전트 패널(규칙 8), API 형태 재현 태스크(R4)는
  broker-api-expert 추가(규칙 8-b).

## Global Constraints

- `backend/replay/`는 `app/`을 임포트하지 않는다(스펙 §4 — 정적 검사
  테스트가 강제). 예외 없음: 틱 테이블은 복제 + 값 대조 테스트.
- 목의 형태 기준: 실측(§3 우선순위). 목이 실측과 다르면 목의 버그.
- 결함 주입 시간 단위는 벽시계 기준(§5 — 배속 무관).
- 시크릿 무로그(§7), `/_replay/*` 네임스페이스 격리(§9).

## 태스크

### R1 — 분봉 실측·수집 (진행 중)
- Files: `backend/scripts/replay_collect_minutes.py`(완료 — 5자 리뷰),
  `backend/scripts/replay_coverage_gate.py`(신규 — §6 게이트 ①~③ 표 산출)
- 완료 조건: 실측 기록(`.superpowers/sdd/replay-ka10080-probe.txt` — 바디
  필드·리스트 키·정렬·보관 일수), minutes.sqlite 확보, 커버리지 표(미충족
  분기는 §10에 "미검증" 명시 or 재선정/config 단축 확정), 결측 분 정책
  실측 확정(§12 — `replay-ka10080-coverage.txt` 갭 통계로 기록됨),
  CLAUDE.md §5에 ka10080 실측 반영.
- **잔여(패널 지적):** ① 저유동 대체 심볼 1종 추가 수집(012510이 degenerate
  라 저유동 카테고리 실질 부재 — 트레이더 R2 #2. 게이트가 카테고리 실종
  경고 출력) + 그 심볼 무거래 분에 ka10095 실측(직전가 유지 브리지 가정
  검증). ② 페이지 경계 중복/누락 표본 점검(broker-api — 로더의 중복 ts
  최후 승리가 방어 중이나 실측 미확인). ③ R7 앵커 확정은 **윈도우 모드
  게이트**(--anchor/--days) 결과만 근거(전체 이력 표는 후보 탐색용 —
  후보 1건 확보: 2026-06-25+5거래일 전 분기 커버, coverage.txt).
- 패널: broker-api(실측 해석), 트레이더(커버리지 표 판정) — 1차 완료,
  델타 반영 상태.

### R2 — 시계/데이터/계좌 기반
- Files: `backend/replay/{__init__,clock,minute_store,account,ticks}.py`
  (⚠️ 모듈명 `data.py`는 데이터 디렉토리 `replay/data/`와 이름 충돌 —
  `data/__init__.py`가 생기는 순간 패키지가 모듈을 섀도잉하는 실재현 확인,
  아키텍트 R2 Critical → `minute_store.py`로 확정),
  `backend/tests/replay_mock/test_replay_base.py`
  (⚠️ 배치 사유: pyproject testpaths=["tests"]라 replay/tests/는 기본 수집
  불가 + `tests/replay/` 이름은 replay 패키지를 섀도잉(실재현) →
  tests/replay_mock/ — 아키텍트 R2 #4 문서화)
- Interfaces:
  - `ReplayClock(anchor: datetime[KST], speed: float=1.0)` — `now() ->
    datetime(KST-aware)`(§5), `speed` 노출(응답 스탬프용).
  - `MinuteStore.load(sqlite_path) -> None`(기동 시 전량 메모리 적재 — §4),
    `candle_at(symbol, ts) -> Candle | None`, `last_before(symbol, ts)`
    (결측 분 정책 — R1 실측 결과로 확정), **ts 이후 데이터 접근 API 부재**
    (미래 누출 구조적 차단 — §5 제1 불변식).
  - `Account(cash: int)` — 보유/미체결/체결 이력, 수수료·세금(§7 모의
    실측률) 계산. 전부 인메모리.
  - `ticks.py` — app 틱 테이블 **복제**; `test_ticks_parity`가 값 대조
    (import 아님 — 스펙 §4 확정).
- 테스트: 격리 정적 검사(`from app`/`import app` 패턴 스캔), clock 오프셋/
  배속, 로더 파싱 fail-loud(012510 degenerate 클래스), 계좌 수수료 계산.

### R3 — 매칭 엔진
- Files: `backend/replay/matching.py`, `backend/replay/tests/test_matching.py`
- §8 룰: 시장가=현재 분봉 close 즉시, 지정가=크로스 시(replay_now 동기 —
  사전 미래 스캔 금지), 전파 지연 기본 재현(벽시계 N초), 체결 반영(잔고/
  미체결/수수료), 부분체결·fill 억제는 FaultPolicy 훅만(§9 seam).
- 테스트: 크로스 판정 경계, 전파 지연 창, 미래 누출 부재(등록 시점에 체결
  시각 미리 계산 안 함을 시계 전진으로 검증).

### R4 — 키움 엔드포인트 1세트 (+broker-api 패널)
- Files: `backend/replay/main.py`, `backend/replay/api/*.py`,
  `backend/replay/tests/fixtures/*.json`(정제 픽스처 — §7),
  `backend/replay/tests/test_endpoints.py`
- 대상 TR: oauth2/token·revoke(8005 계약, 시크릿 무로그+회귀 테스트),
  ka10095(파이프·100상한·합성 호가·빈 행), kt10000/10001(RC4003 틱 검증)/
  kt10003, ka10075(oso·io_tp_nm 접두 부분문자열·전파 지연), kt00001,
  kt00018(최상위 tot_* 필수·A프리픽스·제로패딩).
- 테스트: 정제 픽스처 형태 대조 회귀, 시크릿 무로그 단정.

### R5 — 결함 주입
- Files: `backend/replay/faults.py`, `backend/replay/api/admin.py`,
  `backend/replay/tests/test_faults.py`
- FaultPolicy 주입 seam(§9 — 전역 플래그 금지), 관리 API 3종(faults/
  status/reset — reset 범위: faults+account+pending, clock 유지),
  시나리오 표 전수(§9 12종 — 상하한가 락·fill 억제·VI·신규 거부·거래정지
  결측 포함).

### R6 — 프로필 배선 (app 쪽 변경 — 별도 패널)
- Files: `app/core/config.py`(kiwoom_base_url_override — **루프백/replay
  서비스명 exact-match allowlist**, 실전+override 차단, anchor 단독 차단,
  replay_time_anchor), `app/adapters/kiwoom/client.py`(override 적용+실효
  URL WARNING), `app/store/models.py`+`alembic 0009`(trade_runs.
  run_environment NOT NULL — §4-1), `app/main.py`(오프셋 시계 주입·
  run_environment 전달), `docker-compose.yml`(replay 서비스 스텁 —
  127.0.0.1 바인딩·별도 스테이지·healthcheck), `tests/`(validator 조합·
  프로덕션 라우트 /_replay 부재 회귀).
- TradingService.create_run에 run_environment 전달(§4-1 — store.create_run
  시그니처 확장).

### R7 — 엔진 통합 예행 (speed=1.0 게이트)
- 리플레이 프로필로 백엔드 기동 → 커버리지 표 기준 전 분기 실행 + §9
  시나리오별 방어선 발동 로그/감사 행 확인(§10). 발견 결함은 별도 태스크.
  증거 `.superpowers/sdd/replay-r7-*.txt`(speed 스탬프 포함). 회고록(규칙 4).

## 순서/의존

R1(수집 — 진행 중) ∥ R2 → R3 → R4 → R5 → R6 → R7.
R2~R5는 app 무변경(replay/ 만), R6만 app을 건드린다(프로필 배선).
