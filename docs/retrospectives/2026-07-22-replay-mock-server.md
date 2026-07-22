# 회고록 — 리플레이 목 서버 R1~R6 (2026-07-22)

> 규칙 4에 따른 태스크별 회고. 대상 스펙
> `docs/specs/2026-07-22-replay-mock-server-design.md`(5자 리뷰 승인),
> 계획서 `docs/plans/2026-07-22-replay-mock-server-plan.md`(R1~R7).
> R7(엔진 통합 예행)은 미완 — 2026-07-23 장중 실측(저유동 대체 심볼)과
> 함께 진행 예정이며 완료 시 본 회고록에 추가한다.

## 0. 무엇을 왜 만들었나 (비전문가용 요약)

Phase 5 트레이딩 엔진의 최종 검증(Task 8)은 장중(09:05~09:30 진입 창)
에만 가능하다. 사용자가 "과거 분봉 데이터를 시간 오프셋으로 재생하는
키움 API 호환 목(mock) 서버를 만들어 장외 시간에도 엔진을 시험하자"고
제안했고, 규칙 3에 따라 두 가지를 수정 제안해 승인받았다:

1. **"공식문서 기반" → "실측 우선"**: 이 프로젝트에서 키움 공식 문서가
   실측과 다른 사례가 반복 확인됐으므로(파이프 구분자, trde_tp 자릿수
   등), 목의 형태 기준은 실측 캡처가 1순위다(스펙 §3).
2. **"장 종료 시 자동 전환" → "명시적 프로필 전환"**: 실주문 엔진이
   대상 서버를 시간 기준으로 스스로 바꾸면 "이 주문이 어느 환경으로
   나갔는가"라는 감사 질문이 흐려진다. 전환은 `.env` 변경+재기동만으로,
   코드 변경 없이(§4-1 — 사용자 확인 "목서버 실서버 전환은 코드변경없이
   가능한거지?" → 그렇다).

핵심 산출물: `backend/replay/`(app과 격리된 형제 패키지 — 목 서버 본체),
`backend/app`의 프로필 배선(R6), 분봉 실측 데이터(281MB sqlite).

## 1. 태스크별 기록

### R1 — 분봉 실측·수집 (커밋 `1301c25`, R2와 합본)

- **요청/목적**: 재생할 원료(과거 1분봉)를 키움 ka10080으로 실측·수집
  하고, 형태(필드명·정렬·보관 기간)를 확정한다.
- **구현**: `backend/scripts/replay_collect_minutes.py`(프로브+수집 —
  Phase 5 G1에서 확립한 토큰 관용구 재사용: try 안 생성·finally revoke·
  limiter 공유), `backend/scripts/replay_coverage_gate.py`(§6 커버리지
  게이트 — 전체/윈도우(--anchor/--days) 2모드, 임계 크로스·결측 분
  통계·카테고리 실종 경고).
- **실측 확정**(CLAUDE.md §5 반영, `.superpowers/sdd/replay-ka10080-*`):
  `POST /api/dostk/chart` body `{stk_cd, tic_scope:"1", upd_stkpc_tp:"1",
  base_dt}` 1차 수락, 리스트 키 `stk_min_pole_chart_qry`, `cntr_tm`
  YYYYMMDDHHMMSS, 가격 ±부호(파서 abs 필수), 내림차순, 900행/페이지,
  보관 ~13개월, **무거래 분은 봉 없음**(결측 — "직전가 유지" 정책의
  실측 근거), `012510` 분봉도 degenerate(전 필드 빈 문자열).
- **패널**: broker-api+트레이더. 잔여로 남긴 것 — 저유동 대체 심볼
  수집+ka10095 프로브(012510 degenerate로 저유동 카테고리 실질 공백),
  페이지 경계 중복 표본. **R7 앵커는 윈도우 게이트 결과만 근거**(전체
  이력 통계는 13개월이면 자명 통과라 무의미 — 트레이더 지적으로 재설계).
  앵커 후보 1건 확보: 2026-06-25(+5거래일 전 분기 커버).

### R2 — 시계/데이터/계좌 기반 (커밋 `1301c25`)

- **구현**: `backend/replay/{clock,minute_store,account,ticks}.py` +
  `backend/tests/replay_mock/test_replay_base.py`(19 테스트).
  - `ReplayClock`: anchor+실경과×speed, KST-aware(§5).
  - `MinuteStore`: 기동 시 메모리 적재(요청 경로 DB I/O 금지 — 결함
    타이밍 왜곡 방지), **조회 API가 "ts 이하"만 존재**(미래 누출 구조
    차단 — §5 제1 불변식), symbols/since 부분 적재(전량 실측 842MB/9.5s
    — 스펙의 "수 MB" 예상이 틀렸음을 실측으로 정정).
  - `Account`: 수수료 0.35%/매도세 0.2%(모의 실측률), 평단 절삭 드리프트
    가시화, 전파 지연 창(visible_after).
  - `ticks.py`: app 틱 테이블 **의도적 복제**+값 대조 테스트(임포트
    금지 — 목이 피검 코드를 공유하면 검증이 자기참조로 무력화).
- **프로세스 사건 2건(실재현)**: ① 모듈명 `data.py`가 데이터 디렉토리
  `replay/data/`에 섀도잉됨 → `minute_store.py` 개명(아키텍트 Critical),
  ② 테스트 디렉토리 `tests/replay/`가 replay 패키지를 섀도잉
  (ModuleNotFoundError 실발생) → `tests/replay_mock/` 개명+근거 docstring.
- **부산물(프로덕션 버그 수정)**: 스펙 리뷰 중 아키텍트가
  `market_calendar`의 tz 미정규화를 발견 — UTC-aware now로 진입 창
  판정이 9시간 어긋나던 실버그. `_as_kst()` 신설로 전 함수 KST 정규화
  +회귀 테스트(커밋 `9bf579e`).

### R3 — 매칭 엔진 + FaultPolicy seam (커밋 `1e220ca`)

- **구현**: `backend/replay/matching.py`(§8 룰), `faults.py`(seam만 —
  R5 소유 파일을 선행 생성: 매칭이 주입 지점을 필요로 함),
  `tests/replay_mock/test_matching.py`.
- **§8 매칭 룰(패널로 확정된 최종형)**: 시장가=현재가(직전 분봉 close)
  즉시 체결 / 지정가는 **마켓터블 재평가 우선**(check 시점 limit≥현재가
  면 현재가로 체결 — 트레이더) / 과거 구간 크로스만 limit가 체결 /
  미체결 매수는 **접수 시점 예약 차감**(reserve_price 고정 — 실서버
  ord_alow_amt 재현, 시세 결측이 예약을 0원으로 만들 수 없음) / 전파
  지연(벽시계 1.5s 기본).
- **패널 결함(수정된 것)**: 개발자 Critical — 부분체결 break가 관측된
  크로스를 영구 유실(잔량 소진까지 스캔 지속으로 수정). 트레이더
  Important — 매수 증거금 미예약(→예약 차감), suppress 해제 시 체결가
  §8 위반(→마켓터블 재평가), check_fills 호출 계약 부재(→R4 계약).
  개발자 Important — 마켓터블 조건식 중복(→`_is_marketable` 추출),
  예약 침묵 0원(→reserve_price 구조 제거). 아키텍트 — store.
  now_provider 자동 바인딩(미래 누출 클램프 구조화).
- **프로세스 사건**: 편집-리뷰 경합 — 트레이더 반영 편집 직후 아키텍트
  가 이전 스냅샷 기준 "617 passed 불일치·3자 충돌"로 수정 요구. 델타에
  "현재 파일 기준 재확인"을 명시해 해소(이후 표준 관행화).

### R4 — 키움 엔드포인트 1세트 (커밋 `2a7c064`)

- **구현**: `backend/replay/{main,config,tokens}.py`,
  `backend/replay/api/{common,auth,stkinfo,ordr,acnt,admin}.py`, 정제
  픽스처 3종(`tests/replay_mock/fixtures/` — 실측 캡처의 필드셋·패딩
  보존판, git 추적), `test_endpoints.py`.
- **재현한 실측 계약**: oauth2 단일 활성 토큰(재발급이 기존 토큰 8005화
  — Phase 2 사고 재현)·시크릿 무로그(수신 즉시 폐기+회귀), ka10095
  (파이프만 바인딩·100 상한 rc=5·63필드·합성 호가 ±1~5틱·전일 종가 기반
  flu_rt 실값), kt10000/10001(한 자리 trde_tp·RC4003 원문)/kt10003
  (전량취소만), ka10075(전파 지연 창·io_tp_nm '-매도' 원문·무패딩
  ord_pric), kt00001(예약 차감 ord_alow_amt), kt00018(A프리픽스·패딩
  폭 15/12·음수 부호 폭·tot_* 필수).
- **패널 결함(수정된 것)**: broker-api Important — flu_rt 하드코딩이
  스펙 "실값" 약속 위반(→prev_day_close 실계산). 아키텍트 — 주문 TR
  진입 check_fills 부재(조회 없는 연속 제출 오거부 → 모든 TR 진입
  계약으로 확대). 트레이더 — 취소 전 체결 미확정("취소했으니 안전"
  오신호 → MatchingEngine.cancel() 진입 check_fills), **진입 지정가
  즉시체결 함정 §9 미등재**(→스펙 §8 대칭 함정 문단+§9 시나리오 행
  추가 — R7 "전 분기 통과"가 진입 폴백 미검증을 은폐하는 것 방지).
  보안 — revoke 무로그 회귀 공백(→token/revoke 양쪽 단정). 개발자 —
  수수료 수식 이원화(→commission/sell_tax 단일화).
- 주문번호를 실측 형태(7자리 제로패딩 숫자)로 정렬.

### R5 — 결함 주입 시나리오 + 관리 API (커밋 `b6a6ca5`)

- **구현**: `faults.py`에 `ScenarioFaultPolicy`(상태는 인스턴스 안에만
  — 전역 플래그 금지), `api/admin.py`에 `/_replay/faults·reset`,
  `test_faults.py`(§9 13종 전수).
- **§9 13종 ↔ 프리미티브 매핑**: 전파 지연 확대/API 500·429·delay/
  부분체결/취소·신규 거부(횟수)/익절·진입 지정가 억제/상하한가 락/VI/
  거래정지(빈 행+무기한 억제)/잔고 동결(kt00018 스냅샷)/토큰 강제
  무효화. reset은 **in-place clear**(엔진이 쥔 정책 참조 유지 — 객체
  교체 금지 계약), clock은 보존.
- **패널 결함(수정된 것)**: 트레이더 Critical — "진입 지정가 억제"
  테스트 누락(R4에서 본인들이 추가시킨 행이 정작 빠짐 → 대칭 테스트
  신설, "12종" 표기를 13종으로 정정). 트레이더 Important — **부분체결이
  폴링 횟수에 결합**(엔진이 신중하게 자주 폴링할수록 결함이 빨리
  해소되는 역설 → 벽시계 interval 래칫으로 분리), 유한 억제 창<엔진
  타임아웃이면 폴백 미실행 오검증(→§9 "사용 규율" 명문화: 표준은
  seconds=None), delay 모드 미검증(→실측 테스트+어댑터 타임아웃 초과
  가이드). 보안 — 시간 파라미터 300s 상한(단위 착각 sleep 점유 차단),
  count≥1 가드(count=0이 1회 발동하는 함정). 개발자 — kt00018 평가
  수식 중복(→eval_holdings 순수 함수 공유).

### R6 — 프로필 배선, app 쪽 변경 (커밋 `09f1c76`)

- **구현**: `app/core/config.py`(kiwoom_base_url_override — 루프백/
  replay 서비스명 정확 일치 allowlist·실전 조합 차단·오류에 URL 원문
  미노출), `app/adapters/kiwoom/client.py`(override+실효 URL WARNING),
  `app/core/replay_clock.py`(오프셋 시계 유닛 — speed=1.0 고정 전제),
  `app/main.py`(기동 프로브+시계 주입+run_environment 전달),
  `trade_runs.run_environment`(Alembic 0009), Dockerfile 3스테이지+
  compose `--profile replay`, 격리 회귀(`test_replay_profile.py` 18종).
- **패널이 만든 구조적 개선 2건(계획 대비 설계 변경 — 중요)**:
  1. **트레이더 Critical**: run_environment가 write-only — 같은 DB에
     리플레이를 붙이면 reconcile이 실전 포지션을 "브로커 미보유→CLOSED"
     로 오판해 **TP/SL 감시에서 이탈**시킬 수 있었다. →
     `open_positions(run_environment)` 조인 필터(서비스 4개 호출+
     /trade/positions 전부 배선) + 리플레이 기동 시 타 환경 미종결
     포지션 DB면 **기동 거부**의 이중 방어로 수정.
  2. **앵커 서버 SSOT(트레이더 speed 불일치+override 단독, 아키텍트
     env 이중화+잊힌 override — 4개 지적의 합류점을 프로브 하나로
     해소)**: 초안의 `REPLAY_TIME_ANCHOR` env를 **폐기**하고, override
     설정 시 main이 `/_replay/status`를 1회 프로브 — 미도달·speed≠1.0
     이면 기동 거부, 서버의 replay_now를 앵커로 취득. env 값 드리프트와
     서버·앱 기동 시차 드리프트가 동시에 소멸.
- 추가: 진입 신선도 가드 양방향 정확 일치(미래 신호=look-ahead 거부 —
  실시계 분석 픽이 과거 앵커 재생에 흘러드는 오염 차단), `.env`
  dockerignore(이미지 레이어 시크릿 차단), 기동 게이트 lifespan 통합
  테스트 3종(개발자 "테스트로 강제" 권고).

## 2. 설계 원칙 (전 태스크 관통)

- **실측 > 공식문서 > 사제**(§3): 목이 실측과 다르면 목의 버그.
- **app↛replay·replay↛app 양방향 격리**: AST 정적 검사(동적 임포트
  포함)+sys.modules 런타임 감사+라우트 부재 회귀+이미지 스테이지 분리.
  틱 테이블은 복제+값 대조(자기참조 검증 방지).
- **미래 누출 구조 차단**(§5): "이후" 데이터를 주는 API 자체가 없음+
  now_provider 클램프 자동 바인딩+크로스 판정 replay_now 동기.
- **결함은 FaultPolicy seam으로만**(§9): 매칭 로직에 시나리오 if 산재
  금지, reset은 in-place.
- **침묵 금지**: price_missing_skips/negative_cash_events/
  cost_drift_total/loader skipped 등 전부 카운터+로그로 표면화.

## 3. 프로세스 회고

- **패널 유효성**: R3~R6 매 태스크에서 Critical/Important가 나왔고
  전부 실질 결함이었다(특히 R6 트레이더의 "감사 컬럼 write-only" —
  실전 포지션 감시 이탈로 이어질 수 있던 진짜 사고 경로). 4개 지적이
  하나의 구조(기동 프로브)로 합류 해소된 R6 사례는 개별 패치보다
  합류점을 찾는 쪽이 낫다는 교훈.
- **편집-리뷰 경합**: 병렬 리뷰 중 코드를 고치면 이전 스냅샷 판정이
  나온다 — 델타 메시지에 "현재 파일 기준 재확인"을 명시하고 타 패널
  반영분도 요약해 함께 재확인시키는 관행으로 정착.
- **이름 섀도잉 2건 실재현**(data.py, tests/replay/) — 파이썬 패키지
  이름은 데이터 디렉토리·픽스처 트리와 겹치지 않게 선제 확인할 것.

## 4. 남은 일

- **R7(엔진 통합 예행)** — 2026-07-23: 리플레이 프로필 기동(별도
  DATABASE_URL 필수 — 기동 게이트), 커버리지 표 전 분기+§9 시나리오
  방어선 발동 확인(익절/진입 억제는 seconds=None 표준), 증거
  `.superpowers/sdd/replay-r7-*.txt`(speed 스탬프).
- **R1 잔여** — 장중: 저유동 대체 심볼 ka10080 수집+무거래 분 ka10095
  프로브(직전가 유지 브리지 검증), 페이지 경계 중복 표본.
- kt10003 체결완료 취소 거부 rc(미실측 가정), 기타 거부 rc=20 통일,
  '+매수' io_tp_nm 근사, kt00001 패딩 폭 — PRE-GATE 후보(§12) 유지.
