# 회고록 — Phase 1: 키움 브로커 어댑터 (모의투자)

- **작업 기간:** 2026-07-17 (단일 세션)
- **완료일:** 2026-07-17
- **근거 문서:** `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md`(설계 spec),
  `docs/plans/2026-07-17-phase1-kiwoom-broker-adapter-plan.md`(구현 계획서, Task 1~9)
- **커밋 범위:** `d43af6b`(계획서) ~ `85eb66c`(Task 8) + 본 문서를 만드는 이 커밋(Task 9)

이 문서는 비전문가도 "무엇을 왜 어떻게 했는지" 따라올 수 있도록 CLAUDE.md 규칙 4에
따라 작성한다.

---

## 1. 무엇이 요청되었나

Phase 0(워킹 스켈레톤)에서 확립한 계층 구조(`api/core/adapters/domain/store`) 위에,
**실제 증권사(키움 REST API)와 통신하는 첫 번째 어댑터**를 만드는 것이 Phase 1의
목표였다(spec §2). 범위는 "필요한 것만 먼저" 원칙에 따라 다음 세 가지로 좁혔다.

1. **인증** — OAuth2 토큰 발급/자동 재발급/폐기.
2. **시세·캔들 조회** — 현재가, 일봉(6개월 배치 수집의 기초).
3. **계좌 조회** — 예수금, 평가잔고.

**범위 밖(명시):** 주문 실행, 실시간 WebSocket, DB 적재, 스코어링, UI 노출 — 전부
소비자(트레이딩 엔진 등)와 함께 만드는 것이 합리적이므로 Phase 5 이후로 미뤘다(spec
§2). 도메인(`domain/broker.py`)은 `BrokerPort`라는 포트 인터페이스만 알고, 키움의
TR id·헤더·페이지네이션·레이트리밋·토큰 수명은 전부 `adapters/kiwoom/` 안에
봉인한다는 것이 핵심 설계 결정이었다(spec §3, CLAUDE.md §3).

또한 이번 Phase부터 **신규 프로세스 규칙(CLAUDE.md 규칙 8)**이 적용됐다: 태스크마다
코딩 직후 4명의 리뷰 에이전트(senior-developer/senior-trader/architecture-expert/
security-expert)가 diff를 검토하고, Critical/Important 지적사항은 수정·재검토를
거쳐야 다음 태스크로 넘어갈 수 있다.

## 2. 시작 시 코드 상태

Phase 0이 끝난 시점(`aa61b1e`/최종 `1ffcee7`)의 백엔드는 `GET /health`, `/ws` 상태
프레임, 설정 로더, DB 연결 + 마이그레이션 1개만 가진 워킹 스켈레톤이었다.
`app/adapters/`, `app/domain/`은 **빈 `__init__.py`만 있는 스텁**이었고, 키움 API를
실제로 호출하는 코드는 전혀 없었다. `docker-compose.yml`의 백엔드 포트는
`"8000:8000"`(모든 인터페이스에 노출)이었다 — Phase 0 최종 리뷰가 남긴 보안 이월
권고("자격증명이 API를 지나가기 전에 `127.0.0.1`로 좁힐 것")가 Phase 1 Task 1의
출발점이 됐다.

## 3. Task 1~9 — 무엇을 만들었나 (목적 · 파일 · 커밋 · 패널 리뷰)

### Task 1 — compose 포트를 localhost로 제한
**목적:** 브로커 자격증명이 백엔드 API를 지나가기 전에, 백엔드 포트가 LAN 전체에
노출되지 않도록 막는다(Phase 0 이월 보안 권고).
**파일:** `docker-compose.yml` (1줄 변경: `"8000:8000"` → `"127.0.0.1:8000:8000"`).
**커밋:** `fc9d41a chore: bind backend port to localhost only`
**패널 리뷰:** 4/4 즉시 승인(dev approve, trader n/a, arch approve, sec approve) —
Phase 1의 8개 코드 태스크 중 유일하게 수정 없이 1차 통과한 태스크.

### Task 2 — `domain/broker.py`: 포트 + 도메인 모델
**목적:** 키움을 전혀 모르는 순수 도메인 어휘(`Quote`/`Candle`/`Deposit`/`Position`/
`Balance`)와 `BrokerPort` Protocol을 정의한다. 이후 모든 태스크가 이 계약을 향해
구현한다.
**파일:** `backend/app/domain/broker.py`(+68), `backend/tests/test_domain_broker.py`
(+31), `docs/architecture/system-overview.md`(+17/-8, 계층 설명 보정). 3개 파일,
+108/-8줄.
**커밋:** `abc1e4d feat(domain): broker port and market/account models`
(원본 커밋 `0bed276` → 패널 수정 후 amend)
**패널 리뷰:** dev·sec 승인, **trader·arch가 수정 요구 → 반영 후 재승인**.
- `Quote.change_rate`: `float` → **`Decimal`**(임계값 비교 오차 배제 — 기초 어휘
  정밀화).
- `Balance.positions`: `list[Position] = field(default_factory=list)` →
  **`tuple[Position, ...]`**(얕은 불변성 → 진짜 불변성; frozen dataclass라도 내부
  list는 변경 가능했던 결함).
- `Balance.total_eval`/`total_profit`의 기본값(`= 0`) 제거 — 시그니처를 브리프와
  일치시킴.
- `Position.avg_price`에 "브로커가 원 단위로 반올림해 제공하는 값" 주석 추가.
- `BrokerPort.get_daily_candles` 독스트링에 "장중 호출 시 마지막 봉은 미확정일 수
  있다" 계약 문구 추가.
- `system-overview.md`: "브로커는 adapters의 BrokerPort 뒤에 숨긴다" → **포트는
  `domain/`이 소유하고 `adapters/`가 구현한다**(의존관계 역전 원칙 명확화)로 정정.

### Task 3 — 에러 계층 + `TokenManager`
**목적:** `BrokerError` 계층(`AuthError`/`RateLimitError`/`ApiError`)과 OAuth2 토큰
자동 재발급/폐기 로직을 만든다.
**파일:** `backend/app/adapters/kiwoom/{__init__.py,errors.py,auth.py}`,
`backend/tests/kiwoom/{__init__.py,conftest.py,test_auth.py}`,
`backend/tests/live/{__init__.py,test_live_smoke.py}`, `backend/app/core/config.py`,
`backend/app/store/db.py`, `backend/pyproject.toml`, `backend/uv.lock`. 13개 파일,
+337/-7줄.
**커밋:** `3dd53be feat(kiwoom): token manager with auto-reissue + broker errors`
(원본 `7a1b8a6` → 패널 수정 후 amend)
**패널 리뷰:** dev 승인, **trader·arch·sec가 수정 요구 → 반영 후 전원 재승인**.
- **SecretStr 전환(보안 Critical)** — `Settings.kiwoom_app_key`/`kiwoom_secret_key`/
  `database_url`을 `pydantic.SecretStr`로 변경(§5 "겪은 문제" 참고). `.get_secret_value()`는
  사용 직전 최말단(`db.py::create_db_engine`, 라이브 테스트의 `TokenManager` 생성부)
  에서만 호출.
- `TokenManager._issue()`를 3단계 방어(네트워크 예외 → `AuthError`, HTTP 429 → 파싱
  전에 `RateLimitError`, 비-JSON 응답 → `AuthError`)로 재구성.
- `revoke()` 전체를 `self._lock`으로 감싸 `get_token()`과의 경합(방금 발급한 토큰을
  `revoke()`가 지워버리는 이론상 경쟁 조건) 제거.
- `revoke()` 로그 정확성: 응답 `return_code == 0`일 때만 성공 로그, 그 외에는
  `logger.warning`.

### Task 4 — TR별 레이트리미터
**목적:** TR(api-id)마다 독립된 토큰버킷으로 초당 호출을 제한한다.
**파일:** `backend/app/adapters/kiwoom/rate_limiter.py`(+46, 이후 +6 fix),
`backend/tests/kiwoom/test_rate_limiter.py`(+84). 2개 파일, +130줄.
**커밋:** `51fb504 feat(kiwoom): per-TR rate limiter`
(원본 `2af7df0` → 패널 수정 후 amend)
**패널 리뷰:** sec 승인, **dev·trader·arch가 Critical 수정 요구 → 반영 후 전원
재승인**.
- **락-sleep 분리(트레이더 관점 Critical)** — 원래 구현은 `async with lock:` 블록
  안에서 `await sleep(wait)`를 호출했다. 즉 한 TR이 대기하는 동안 **다른 모든 TR의
  버킷도 함께 잠겨** 전역 직렬화가 발생했다(실측: 약 953ms 교차-TR 차단). 트레이딩
  엔진 관점에서는 "데이터 수집 TR의 대기가 긴급 주문 TR을 막는" 심각한 결함이다.
  → `while True` 재시도 루프로 재구성해 **락을 풀고 sleep한 뒤 재검증**하도록 수정.
  실제 asyncio 동시성 회귀 테스트로 "느린 TR이 ~1초 대기해도 빠른 TR은 <0.5초 안에
  끝난다"를 증명.
- 생성자 가드 추가(`rate <= 0` / `burst < 1`이면 `ValueError`).

### Task 5 — `KiwoomHttpClient`
**목적:** 모든 TR 호출이 통과하는 단일 HTTP 게이트웨이. 헤더 구성, 401 재발급,
429 백오프, `cont-yn`/`next-key` 연속조회를 담당한다.
**파일:** `backend/app/adapters/kiwoom/client.py`(+115), `backend/app/adapters/kiwoom/rate_limiter.py`
(+5, `penalize` 추가), `backend/tests/kiwoom/test_client.py`(+229),
`backend/tests/kiwoom/test_rate_limiter.py`(+9). 4개 파일, +358줄.
**커밋:** `da088f7 feat(kiwoom): http client with pagination and 429/401 retry`
(원본 `826c882` → 패널 수정 후 amend)
**패널 리뷰:** **4명 전원이 Critical 수정 요구 → 통합 수정 후 전원 재승인**(이번
Phase에서 유일하게 4/4 전원이 1차에서 수정을 요구한 태스크).
1. **401/429 재시도 예산 분리(dev/arch Critical)** — 단일 `attempt` 카운터로
   401과 429를 함께 셌기 때문에, "401 후 429" 시퀀스가 백오프 예산을 조기
   소진하거나 아예 도달하지 못할 수 있었다. `reissued`(1회성 불리언)와
   `backoff_idx`(독립 카운터)로 분리.
2. **비-JSON/비-dict 응답 가드(sec/arch)** — `resp.json()`을 `try/except`로 감싸지
   않아 비-JSON 200 응답이 처리되지 않은 `json.JSONDecodeError`로 그대로 새는
   경로를 차단.
3. **`RateLimiter.penalize(tr_id)`(트레이더 Critical)** — 서버가 429를 보내면
   로컬 버킷을 즉시 비워, 클라이언트의 자체 백오프 타이머만 믿지 않고 서버 신호를
   로컬 상태에 반영하도록 함("재돌진 방지").
4. **`aclose()` 소유권(arch)** — 외부에서 주입한 `httpx.AsyncClient`/`TokenManager`는
   `aclose()`가 닫지 않도록 소유권 플래그(`_owns_http`/`_owns_tokens`) 도입.

### Task 6 — `KiwoomBroker`: 현재가 + 일봉
**목적:** TR `ka10001`(현재가)/`ka10081`(일봉)을 도메인 모델로 변환한다.
**파일:** `backend/app/adapters/kiwoom/broker.py`(+92, 이후 패널 수정 포함),
`backend/app/domain/broker.py`(+3, 수정주가 계약 문구), `backend/tests/kiwoom/test_broker_market.py`
(+108), `backend/tests/live/test_live_smoke.py`(+77). 4개 파일, +278/-2줄.
**커밋:** `c93ed44 feat(kiwoom): quote and daily candle queries`
(원본 `bac291e` → 패널 수정 후 amend)
**패널 리뷰:** sec 승인, **dev·trader·arch가 수정 요구 → 반영 후 전원 재승인**.
- `_to_decimal` 헬퍼 추가(공백 필드 방어) — 수동 `.replace("+", "")` 제거.
- **시계 주입(dev/arch)** — `KiwoomBroker(client, today=...)`로 "오늘 날짜"를
  주입 가능하게 하여 `base_dt` 계산을 결정적으로 테스트 가능하게 함.
- 필드 파싱 실패를 `try/except (KeyError, ValueError, ArithmeticError) → BrokerError`
  로 감싸 fail-loud하게 만듦.
- `call_paged` 소비를 `contextlib.aclosing`으로 감싸 조기 `break` 시 비동기
  제너레이터가 확실히 종료되도록 함.
- **원본 응답 순서 실측(trader Important)** — 라이브 테스트로 일봉 원본 응답이
  내림차순(최신→과거)임을 직접 증명(§7).

### Task 7 — `KiwoomBroker`: 예수금 + 계좌잔고
**목적:** TR `kt00001`(예수금)/`kt00018`(평가잔고)을 도메인 모델로 변환한다.
**파일:** `backend/app/adapters/kiwoom/broker.py`(+56, 패널 수정 포함),
`backend/tests/kiwoom/test_broker_account.py`(+92), `backend/tests/kiwoom/test_broker_market.py`
(+2, 계약 테스트 플립), `backend/tests/live/test_live_smoke.py`(+41). 4개 파일,
+187/-4줄.
**커밋:** `79b9fba feat(kiwoom): deposit and balance queries`
(원본 `985d7ea` → 패널 수정 후 amend)
**패널 리뷰:** dev·sec 승인, **trader·arch가 수정 요구 → 반영 후 양쪽 재승인**.
- **금액 필드 fail-loud(trader/arch, "silent-0" 테마)** — `data.get("entr")` 등
  `.get()` 사용은 필드가 누락되면 조용히 0을 반환한다. 실제 계좌 자금 정보에서는
  "필드 없음"과 "값이 0"을 구분하지 못하면 위험하다 → `data["entr"]` 등 **대괄호
  인덱싱으로 전환**해 누락 시 `KeyError → BrokerError`로 즉시 실패하게 함(반면
  포지션 배열 `acnt_evlt_remn_indv_tot`는 포지션이 없으면 정당하게 부재할 수 있어
  `.get(...) or []` 유지).
- `stk_cd` 정규화를 `_parse_position()` 헬퍼로 추출, `"A"` 접두 제거 후 6자리
  숫자가 아니면 `ValueError`(무음 실패 대신 fail-loud).
- `except` 튜플에 `TypeError`/`AttributeError` 추가(비-문자열 필드에 문자열
  메서드 호출 시 누출 방지).

### Task 8 — FastAPI lifespan에 브로커 생명주기 통합
**목적:** `app.state.broker`를 FastAPI lifespan에서 생성/종료해 앱 전체가 하나의
`KiwoomBroker`를 공유하도록 한다.
**파일:** `backend/app/main.py`(+12/-2), `backend/tests/test_app_lifespan.py`(+16).
2개 파일, +26/-2줄.
**커밋:** `85eb66c feat(backend): broker lifecycle in app lifespan`
(원본 `4687a4f` → 패널 수정 후 amend)
**패널 리뷰:** dev·trader·sec 승인, **arch가 수정 요구 → 반영 후 재승인**.
- lifespan의 정리(cleanup) 로직을 **중첩 `try/finally`**로 재구성해, 브로커 종료가
  실패해도 DB 엔진 `dispose()`가 반드시 실행되도록(예외 안전성 보장, LIFO 순서).

### Task 9 — 실측 팩트 반영 + 회고록 + STATUS 핸드오프 (본 문서)
**목적:** Task 1~8에서 라이브 스모크로 확인된 사실을 `CLAUDE.md` §5와 spec §5에
반영하고, Phase 전체를 회고록으로 정리하며, `docs/STATUS.md`를 Phase 2로
핸드오프한다. 코드 변경 없음(문서 전용) — 4-에이전트 패널 대상 아님.
**파일:** `CLAUDE.md`(§5 갱신), `docs/specs/2026-07-17-phase1-kiwoom-broker-adapter-design.md`
(§5 표 갱신), `docs/retrospectives/2026-07-17-phase1-kiwoom-broker-adapter.md`(본
문서, 신규), `docs/STATUS.md`(갱신).
**커밋:** `docs: phase 1 retrospective + verified kiwoom facts + status handoff`
(본 작업으로 생성)

### 패널 리뷰 결과 총괄 — 사실 정정

브리프에는 "8개 코드 태스크 중 6개에서 패널이 실질 결함을 잡아 수정"이라는
서술이 있었으나, 진행 원장(`.superpowers/sdd/progress.md`)을 실측 대조한 결과는
**7개(Task 2~8)에서 수정이 있었고, Task 1만 1차에서 4/4 전원 승인**이었다(위 각
태스크 절 참고, 특히 Task 6도 dev/trader/arch 3인이 수정을 요구했다). 6개가 아니라
7개라는 점을 여기서 정정한다(CLAUDE.md 규칙 3 — 근거 없이 넘겨짚지 않는다). 다만
결론(패널 프로세스가 실질적으로 결함을 걸러냈다는 것) 자체는 오히려 더 강하게
뒷받침된다 — 8개 중 7개, 87.5%에서 최소 1개 이상의 Critical/Important 수정이
있었다.

## 4. 어떤 설계/패턴을 썼나

- **포트-어댑터(헥사고날) 아키텍처.** `domain/broker.py`가 `BrokerPort` Protocol과
  도메인 모델을 소유하고, `adapters/kiwoom/`이 그 구현체를 제공한다(의존관계
  역전 — Task 2 패널이 명확화). 도메인은 `import app.adapters`를 단 한 줄도 하지
  않는다 — 다른 브로커(KIS 등)로 교체해도 `domain/`은 영향받지 않는다는 Phase 1의
  DoD 요구를 코드로 강제한다.
- **토큰버킷 + `penalize()`(레이트리미터).** TR별 독립 버킷(`rate=1/s, burst=2`,
  둘 다 설정 가능)에 더해, 서버가 429를 보내면 로컬 버킷을 즉시 비우는
  `penalize()`를 추가해 "서버 신호를 로컬 상태에 되먹임"하는 패턴을 적용했다
  (Task 5 패널).
- **락-바깥 sleep(레이트리미터 동시성).** `asyncio.Lock`은 버킷 상태를 보호하는
  용도로만 짧게 잡고, 대기(sleep)는 락 밖에서 수행 후 재검증하는 루프로
  재구성했다 — "한 TR의 대기가 다른 TR을 막지 않는다"는 트레이딩 엔진의 핵심
  요구사항(긴급 주문 TR이 데이터 수집 TR에 막히면 안 됨)을 레이트리미터
  설계 단계에서부터 보장한다(Task 4 패널).
- **lazy 토큰 발급.** `KiwoomBroker`/`TokenManager`는 생성 시점에 네트워크 호출을
  하지 않는다 — 토큰은 첫 API 호출에서 지연 발급된다. 앱 기동(lifespan)이 키움
  서버 가용성과 무관하게 항상 성공하도록 하기 위함(Task 8 설계 노트).
- **주입 가능한 시계/슬립.** `TokenManager(now=...)`, `RateLimiter(clock=..., sleep=...)`,
  `KiwoomBroker(today=...)` 모두 실제 시간 함수를 기본값으로 갖되 테스트에서
  결정적 가짜 시계로 교체 가능하다 — sleep 없는 빠른 단위 테스트와, 실제
  asyncio 동시성을 검증하는 회귀 테스트를 모두 가능하게 한다.
- **SecretStr 경계 규칙.** `Settings`의 시크릿 필드는 `pydantic.SecretStr`이고,
  `.get_secret_value()`는 항상 "사용 직전 최말단"에서만 호출한다(`db.py`의 엔진
  생성, `client.py`/`TokenManager` 생성자) — 로그·예외 메시지·`repr()`에는 결코
  평문이 등장하지 않는다(Task 3 패널, §5에서 상술).
- **fail-loud 필드 매핑.** 브로커 응답 → 도메인 모델 변환은 전부
  `try/except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) →
  BrokerError`로 감싸고, 핵심 금액 필드는 `.get()` 대신 대괄호 인덱싱을 쓴다 —
  "조용히 0으로 폴백"하는 것보다 "즉시 실패해서 원인을 드러내는" 쪽이 자동매매
  시스템에서 훨씬 안전하다는 판단(Task 6·7 패널의 공통 테마).
- **TDD(RED→GREEN) + 라이브 스모크를 태스크마다 즉시 실행.** spec §8의 "각 TR 구현
  직후 라이브 스모크를 실행해 조기 발견"이라는 리스크 완화 원칙을 그대로
  지켰다 — Task 6에서 `base_dt` 누락이나 응답 정렬 방향 같은 실측 정정이 몰아서
  검증했다면 훨씬 늦게 발견됐을 것이다.

## 5. 과정에서 겪은 문제와 해결

### 5.1 키 유출 사고 → SecretStr 전면 전환
Task 3의 라이브 스모크 실패를 캡처하는 과정에서, pytest가 실패한 테스트의
`settings` 픽스처를 트레이스백에 `repr()`로 출력했고, 그 안에 **키움 앱키/시크릿키
평문 값이 그대로 파일에 기록**됐다. 발견 즉시 정규식으로 캡처 파일을 치환해
`***REDACTED***`로 교체했지만, 조치 과정에서 확인용 셸 명령에 실수로 평문 값을
1회 타이핑해 세션 대화 로그에도 노출시켰다(최종 산출물에는 남지 않음). 근본
원인은 `Settings`의 시크릿 필드가 평문 `str`이라 `repr()`이 그대로 값을 찍는다는
것이었다. 코디네이터는 **사용자에게 해당 앱키/시크릿키 재발급(rotate)을
권고**했고, 같은 태스크의 패널 리뷰에서 `SecretStr` 전면 전환을 Critical로
요구해 반영했다(§3 Task 3). 이 사고와 대응이 Task 3~5 전반의 "SecretStr
`.get_secret_value()`는 최말단에서만" 규칙의 직접적인 계기가 됐다.

### 5.2 모의 전용 키 별도 발급 — 8030 → 8001 → 해결
Task 3 최초 라이브 스모크는 `return_code=2`,
`[8030:투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다]`로 실패했다.
HTTP 200 + 정상 JSON 구조로 응답했다는 것은 요청 자체(엔드포인트, JSON 바디
필드명)는 문제가 없다는 뜻이었다 — 원인은 `.env`의 키가 **실전용으로 발급된
키**였다는 것. 사용자가 모의 전용 키를 새로 발급하고 기존 실전 키는 포털에서
폐기(rotate)한 뒤(§5.1 권고 이행), 다음 시도에서는 다른 에러
`return_code=3`, `[8001:App Key와 Secret Key 정보가 일치하지 않습니다]`가
나왔다(`.superpowers/sdd/task-3-live-retry2.txt`). 원인은 코드 결함이 아니라
**로컬 실행 환경**이었다 — `backend/.env`가 루트 `.env`와 별도로 존재했고
(pytest가 `backend/`에서 실행되어 상대경로 `.env`를 그 안에서 찾음), 갱신된
키 쌍이 루트에만 반영되고 `backend/.env`는 과거 값(짝이 맞지 않는 앱키/시크릿키
조합)을 그대로 갖고 있었다. 코디네이터가 `backend/.env`를 루트 `.env`로 동기화한
뒤 재실행하자 통과했다(`task-3-live-retry3.txt`, PASSED). 이후 모든 라이브 실행
전에 이 동기화를 코디네이터가 선행하도록 프로세스에 반영했다.

### 5.3 레이트리미터 락-sleep 결합(§4에도 기술한 설계 이슈의 발견 경위)
Task 4 구현 초안은 `async with self._lock:` 블록 안에서 대기(`sleep`)까지
수행했다. 패널(dev/trader/arch)이 "한 TR의 대기가 다른 모든 TR을 막는다"는
전역 직렬화 결함을 지적했고, 실측(동시성 테스트)으로 교차-TR 차단이 약 953ms에
달함을 확인했다. 트레이더 관점에서는 이것이 특히 심각한데, 미래(Phase 5)에
데이터 수집 TR과 긴급 주문 TR이 같은 레이트리미터를 공유하게 되면, 데이터
수집의 정상적인 1초 대기가 긴급 매도 주문까지 지연시킬 수 있기 때문이다.
락을 sleep 밖으로 빼는 재구성으로 해결했다(§3 Task 4, §4).

### 5.4 클라이언트 재시도 예산 혼합(401 vs 429)
Task 5 초안은 401(토큰 만료)과 429(레이트리밋)를 **하나의 `attempt` 카운터**로
같이 셌다. 시퀀스가 "401 → 429"로 오면, 401 처리에 이미 카운터를 소모해 429의
백오프 재시도 횟수가 의도보다 줄어들거나, 반대로 429 처리 중 재발급이 발생하면
전체 재시도 상한을 넘겨 무한 루프에 가까워질 수 있는 여지가 있었다. 4개 패널
전원이 이를 Critical로 지적했고, 두 개의 독립된 카운터(`reissued` 1회성 불리언,
`backoff_idx` 별도 정수)로 분리해 해결했다(§3 Task 5).

### 5.5 `base_dt` 필수 파라미터 누락(리서치에 없던 실측 정정)
Task 6에서 일봉 조회(`ka10081`)의 `base_dt`를 빈 문자열로 보내자 모의서버가
`[1511:필수 입력 값이 존재하지 않습니다. 필수입력파라미터=base_dt]`로 거부했다.
사전 리서치(비공식 자료)에는 이 제약이 없었다 — 라이브 스모크를 태스크
직후 즉시 실행하는 원칙(§4)이 없었다면 훨씬 늦게 발견됐을 문제다. 오늘(KST)
날짜를 `YYYYMMDD`로 채우도록 수정해 해결했다(CLAUDE.md §5에 반영).

## 6. Phase 5 이관 결정 — 긴급 TR 우선순위·타임아웃 정책

Task 5(트레이더 패널) 리뷰에서 "주문 관련 긴급 TR과 일반 조회 TR을 같은
레이트리미터/재시도 정책으로 다뤄도 되는가"라는 질문이 제기됐다. 결론은
**Phase 1에서 정책을 만들지 않고, 그 정책이 필요해지는 Phase 5(트레이딩 엔진)에서
소비자와 함께 설계한다**는 것이었다. 이유는 세 가지다.

1. **Phase 1은 주문을 다루지 않는다.** spec §2가 명시적으로 주문 실행을 범위
   밖에 뒀다 — "긴급"과 "일반"을 가를 실제 소비자(TP/SL 모니터, 신호 진입 로직)가
   아직 존재하지 않는 상태에서 우선순위 정책을 설계하면 추측에 기반한 설계가 된다
   (YAGNI 위반 위험).
2. **"긴급"의 정의는 도메인 지식이다.** 어떤 TR이 지연에 민감한지(예: 손절
   시장가 매도), 얼마의 지연이 허용 가능한지(가격 슬리피지와 직결)는 트레이딩
   엔진의 리스크 정책과 분리해서 정할 수 없다. `KiwoomHttpClient`/`RateLimiter`는
   범용 게이트웨이로 남겨두고, 그 위에 우선순위/타임아웃 레이어를 얹는 편이
   책임 분리 원칙에 맞는다.
3. **인프라는 이미 이를 지원하도록 설계됐다.** Task 4의 "락-바깥 sleep" 수정
   (§4, §5.3)은 이 결정을 미리 준비해 둔 것이다 — TR별 독립 버킷 + 대기 중
   다른 TR을 막지 않는 구조이므로, Phase 5가 우선순위 정책을 얹을 때
   레이트리미터 자체를 다시 설계할 필요가 없다.

`client.py`의 모듈 독스트링에도 이 결정이 코드 옆에 명시돼 있다: "긴급 TR
우선순위·타임아웃 정책은 Phase 5(트레이딩 엔진)에서 이 관문 위에 얹는다."
(Task 5 인터페이스 델타, §3 참고)

## 7. 라이브 실측 결과 총괄

Phase 1 전체에 걸쳐 라이브 스모크가 태스크마다 누적됐고, 최종 전체 스위트
캡처는 코디네이터가 실행했다(`.superpowers/sdd/task-9-final-unit.txt`,
`task-9-final-live.txt`):

- **단위 테스트:** `56 collected / 6 deselected / 50 selected` → **50 passed**,
  0 failed(0.94~4.42s대, warning 1개는 기존에 알려진 서드파티
  `StarletteDeprecationWarning`).
- **라이브 스모크:** `56 collected / 50 deselected / 6 selected` → **6 passed**,
  0 failed(10.53s) — 대상은 실제 `mockapi.kiwoom.com`.

라이브 실행으로 확정된 핵심 실측(전부 CLAUDE.md §5·spec §5에 반영):

1. `expires_dt`는 `YYYYMMDDHHMMSS` 형식의 **절대 KST 시각**(상대 TTL 아님).
2. TR 호출은 `POST /api/dostk/{category}` + `authorization`/`api-id` 헤더,
   연속조회는 `cont-yn`/`next-key` 헤더.
3. `ka10001`/`ka10081` 필드명은 리서치와 100% 일치.
4. `ka10081`은 `base_dt`(당일 YYYYMMDD)가 필수 — 빈 값 거부.
5. 일봉 원본 응답은 내림차순(최신→과거) — 실측: `['20260716','20260715','20260714']`.
6. `kt00001`/`kt00018`의 최상위 금액 필드(`entr`/`ord_alow_amt`/`tot_evlt_amt`/
   `tot_evlt_pl`)는 포지션이 0개여도 존재(값 0).
7. 모의 전용 키는 실전 키와 별도로 발급해야 한다(오류 8030) — §5.2 참고.

**여전히 미확정(보류):**
- `kt00018`의 행 단위 필드(`stk_cd`/`pur_pric`/`cur_prc` 등)와 `avg_price`의
  원 단위 반올림 여부 — 모의 계좌 포지션이 0개라 관측 불가(§8 PRE-GATE).
- 공식 레이트리밋 수치(1 req/s, burst 2) — 여전히 비공식 출처 하나에만 근거.
- 429 응답 바디 스키마 — 구조적으로 캡처되지 않음(간헐적으로 토큰 발급
  경로에서만 관측, TR 호출 경로는 실제 429를 못 받아봄).

## 8. 남은 항목 (진행 원장의 Deferred + PRE-GATE 전부)

### PRE-GATE (다음 단계 착수 전 반드시 실행)

1. **⚠️ Phase 2 착수 전:** `base_dt`가 비영업일(주말/공휴일)에 어떻게 동작하는지
   모의서버로 실측하거나 공식 문서로 확인해야 한다(Task 6 트레이더 조건) — 야간
   배치 설계가 이 동작에 의존한다.
2. **⚠️ Phase 5 착수 전(TP/SL 로직 배포 전, hard gate):** 모의 계좌에 실제
   포지션을 만든 뒤 `kt00018`의 행 단위 필드(`stk_cd`/`pur_pric`/`cur_prc`/
   `evlt_amt` 등)와 `avg_price`의 원 단위 반올림 가정을 실측 검증해야 한다
   (Task 7 트레이더 조건, hard). 검증용 라이브 테스트
   (`test_live_잔고_원본응답_avg_price_실측`)는 이미 존재하므로 포지션만 만들면
   즉시 재실행 가능하다.

### 백로그 / 이연 항목

- **보안(T1 이월, 여전히 유효):** `main.py`의 CORS `allow_origins=["*"]`를 실제
  Electron 출처로 좁혀야 한다 — localhost 포트 바인딩만으로는 로컬의 악성
  페이지가 API를 호출하는 것을 막지 못한다. 자격증명/주문 흐름이 붙기 전
  (Phase 5 늦어도)까지는 반드시 처리.
- **`TokenManager._issue()`에 429 백오프 없음(T6 발견):** TR 호출 경로
  (`client.py`)에는 429 지수 백오프가 있지만, 토큰 발급 경로(`auth.py`)에는
  없다. 라이브 테스트를 연달아 여러 번 돌리면 토큰 발급이 몰려 간헐적 429가
  관측됐다(§5.2와는 별개 현상). 운영 경로(Task 8 lifespan 통합 이후)는 앱
  생애주기 동안 클라이언트를 하나만 공유하므로 실질 영향은 적지만, Phase 2
  또는 최종 스윕에서 `auth.py`에도 동일한 백오프를 추가하는 것을 권장.
- **Phase 5 설계에 명시해야 할 것들(T8 패널 이월):**
  - 브로커 단일 공유(`app.state.broker`)는 한 브로커 장애가 앱 전체(시세/계좌/
    향후 주문)로 전파됨을 뜻한다 — 트레이딩 엔진 설계 시 장애 격리 전략을
    명시해야 한다(trader).
  - `revoke()`가 `httpx.HTTPError` 외의 예외를 흡수하지 않는 점을 Phase 5
    이전에 재검토(trader).
  - `app.state.broker`는 항상 `BrokerPort`로만 취급해야 한다는 안내를 API
    라우트 작성 가이드에 남길 것(arch).
- **테스트 인프라 정리(T8 발견, Minor):** `tests/conftest.py` 계열 픽스처 중복
  정리(dev), lifespan이 관리하는 리소스가 늘어나면 `AsyncExitStack` 검토(dev).
- **레이트리밋 공식 수치 미확인(spec §5, 지속):** `RateLimiter`가 설정값이므로
  실측 없이도 조정 가능하지만, 공식 수치가 확인되면 기본값을 갱신할 것.
- **NXT/SOR 거래소 구분 미실측:** Phase 5+ 범위, 현재가/일봉 조회는 순수 6자리
  종목코드만으로 정상 동작함을 실측 확인(spec §5 갱신 참고).

## 9. 다음 단계

Phase 1이 완료됐으므로 다음은 **Phase 2: 데이터 수집 파이프라인**(spec
브레인스토밍부터)이다. 착수 전 위 PRE-GATE 1번(비영업일 `base_dt` 동작)을 반드시
먼저 확인해야 한다 — 야간 배치가 여기 의존한다. 자세한 재개 지점은
`docs/STATUS.md` 참고.
