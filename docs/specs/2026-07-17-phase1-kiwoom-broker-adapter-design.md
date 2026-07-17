# 설계 Spec — Phase 1: 키움 브로커 어댑터 (모의투자)

- **날짜:** 2026-07-17
- **상태:** 초안 (사용자 검토 대기)
- **선행:** Phase 0 워킹 스켈레톤 완료 (2026-07-17, `docs/retrospectives/2026-07-17-phase0-walking-skeleton.md`)
- **범위:** `BrokerPort` 인터페이스 정의 + 키움 REST API 구현체(인증·시세/캔들·계좌 조회).
  주문·실시간(WebSocket)은 **Phase 5로 연기** (인터페이스에 자리만 정의).

---

## 1. 배경 & 이번 브레인스토밍의 결정

1. **브로커 = 키움 REST API 유지.** KB증권 API 전환을 검토했으나 기각 —
   KB증권 핀테크스토어(store.kbsec.com)는 **사업자등록번호 필수인 법인/핀테크 제휴사
   전용 BaaS**로, 개인이 앱키를 발급받는 경로가 없음 (공식 사이트 직접 확인).
   개인용 실질 대안은 한국투자증권 KIS Developers뿐이며, `BrokerPort` 구조 덕에
   필요 시 어댑터 교체로 전환 가능하다.
2. **사용자 준비 상태:** 키움 REST API(openapi.kiwoom.com) app key/secret 발급 완료,
   모의투자 신청 완료 → 라이브 검증 가능. (구 OpenAPI+ 모듈도 설치했으나 **이 프로젝트에서는
   사용하지 않음** — REST는 모듈 설치가 불필요하다.)
3. **범위 결정 ("필요한 것만 먼저"):** ① 인증 ② 시세/캔들 ③ 계좌 조회까지.
   ④ 주문 ⑤ 실시간 WS는 트레이딩 엔진(Phase 5)에서 소비자와 함께 구현.
4. **직접 구현** (비공식 래퍼 라이브러리 미사용) — 개인 유지보수 프로젝트 의존 리스크
   회피, 우리 레이트리밋·포트 설계와의 정합성 (규칙 2).
5. **검증 프로세스 (신규 규칙 8):** 각 태스크 코딩 후 **4-에이전트 리뷰 패널**
   (`.claude/agents/`: senior-developer, senior-trader, architecture-expert,
   security-expert) 전원 통과 후 다음 태스크로.

## 2. 목표

Phase 2(데이터 수집)와 이후 단계가 사용할 **브로커 추상화 계층**을 완성한다:
도메인은 `BrokerPort`만 알고, 키움 상세(TR id, 헤더, 페이지네이션, 레이트리밋,
토큰 수명)는 전부 `adapters/kiwoom/` 안에 봉인된다.

**범위 밖 (명시):** 주문 실행, 실시간 WebSocket, 데이터 저장(DB 적재는 Phase 2),
스코어링, UI 노출. 실전투자 접속(`KIWOOM_MOCK=false`)은 코드 경로만 존재하고
이번 Phase에서는 사용·검증하지 않는다.

## 3. 구성요소 & 경계

### 3.1 `domain/broker.py` — 포트 + 도메인 모델 (키움을 모름)

```python
class Quote:      # 현재가: symbol, name, price, change_rate, volume, as_of
class Candle:     # 봉: symbol, date, open, high, low, close, volume
class Deposit:    # 예수금: total, available
class Position:   # 보유 종목: symbol, name, quantity, avg_price, current_price
class Balance:    # 계좌 평가: positions[], total_eval, total_profit

class BrokerPort(Protocol):
    async def get_quote(symbol: str) -> Quote
    async def get_daily_candles(symbol: str, count: int) -> list[Candle]
        # 최근 count개 일봉 (내부적으로 cont-yn/next-key 연속조회로 채움; 최신→과거 순
        # 응답을 과거→최신 순으로 정렬해 반환)
    async def get_deposit() -> Deposit
    async def get_balance() -> Balance
    # Phase 5에서 추가: place_order/modify/cancel, realtime subscribe
```

- 모델은 키움 응답 필드명이 아니라 **도메인 어휘**로 정의 (벤더 오염 금지).
- 금액·가격은 정수(원 단위), 수량은 정수 — 부동소수점 오차 배제.

### 3.2 `adapters/kiwoom/` — 키움 구현체

| 파일 | 책임 |
|---|---|
| `errors.py` | `BrokerError` 계층: `AuthError`, `RateLimitError`, `ApiError(return_code, return_msg)` |
| `auth.py` | `TokenManager`: `POST /oauth2/token` 발급, **`expires_dt`(절대시각 YYYYMMDDHHMMSS) 기반** 만료 판정 + 여유 마진(기본 60초) 두고 자동 재발급, 종료 시 `POST /oauth2/revoke` 폐기. 토큰은 메모리에만 보관 |
| `rate_limiter.py` | TR(api-id)별 토큰버킷. 기본 1 req/s, burst 2 (비공식 수치 — 설정으로 조정 가능). 429 수신 시 지수 백오프(1s→2s→4s) 최대 3회 재시도 후 `RateLimitError` |
| `client.py` | `KiwoomHttpClient`: httpx.AsyncClient 래핑. base URL은 `Settings.kiwoom_mock`으로 전환(mock `https://mockapi.kiwoom.com` / real `https://api.kiwoom.com`). 요청 헤더 `authorization: Bearer`, `api-id`; 연속조회는 응답 헤더 `cont-yn`/`next-key`를 다음 요청에 실어 자동 반복(비동기 제너레이터). `return_code != 0` → `ApiError`. 401/토큰만료 → 1회 재발급 후 재시도 |
| `broker.py` | `KiwoomBroker(BrokerPort)`: TR 매핑 — 현재가 `ka10001`(stkinfo), 일봉 `ka10081`(chart), 예수금 `kt00001`, 계좌평가잔고 `kt00018`(acnt). 키움 응답 → 도메인 모델 변환 |

- 앱 수명주기: `KiwoomBroker`는 FastAPI lifespan에서 생성/종료(`app.state.broker`).
  Phase 1에서는 API 라우트로 노출하지 않는다 (검증은 테스트로).
- 종목코드는 `KRX:005930` 형식 접두를 어댑터 내부에서 처리 (NXT/SOR은 Phase 5+).

### 3.3 설정 (기존 `core/config.py` 재사용)

- 신규 환경변수 없음. 기존 `KIWOOM_APP_KEY`, `KIWOOM_SECRET_KEY`, `KIWOOM_MOCK` 사용.
- 사용자는 `.env`에 실제 발급 키를 넣는다 (`.env`는 git-ignore, 절대 커밋 금지).

## 4. 에러 처리

- 키움 `return_code != 0` → `ApiError`(코드·메시지 보존, 구조화 로그). 시크릿/토큰은
  로그·에러 메시지에 절대 노출하지 않는다.
- 토큰 만료 응답 → `TokenManager`가 1회 재발급 후 재시도, 재실패 시 `AuthError`.
- HTTP 429 → 백오프 재시도 (rate_limiter), 소진 시 `RateLimitError`.
- 네트워크 오류(timeout 등) → httpx 예외를 `BrokerError`로 감싸 전파 (호출자가
  브로커 장애를 한 타입으로 처리 가능).

## 5. 검증된 팩트 vs 실측 필요 항목 (리서치 2026-07-17)

| 항목 | 상태 |
|---|---|
| 토큰: `POST /oauth2/token`, `grant_type=client_credentials`, 응답 `token`/`expires_dt`(절대시각)/`return_code` | 비공식 다수 일치 — 구현 시 실측 확인 |
| 토큰 폐기: `POST /oauth2/revoke` (`au10002`) | 비공식 — 실측 확인 |
| TR 호출: 카테고리별 POST 엔드포인트(`/api/dostk/...`) + `api-id` 헤더, 연속조회 `cont-yn`/`next-key` 헤더 | 비공식(코드 예제로 구체 확인) |
| TR id: 현재가 `ka10001`, 일봉 `ka10081`, 예수금 `kt00001`, 계좌평가잔고 `kt00018` | 비공식 — 라이브 스모크로 확정 |
| **레이트리밋 1 req/s per-TR** | ⚠️ 단일 비공식 출처로 수렴 — **설정값으로 두고 실측** |
| **429 응답 바디 스키마** | ⚠️ 미확인 — 실측 후 회고록에 기록 |
| 모의서버 제약(일부 종목 주문 제한, 지정가·시장가만 등) | 주문은 Phase 5 — 이번엔 영향 없음 |
| 종목코드 거래소 접두(`KRX:005930`), NXT/SOR 구분 | 비공식 — 현재가 조회에서 실측 |

공식 포털(openapi.kiwoom.com)은 로그인 필요 SPA라 자동 수집 불가 — **확인이 필요할
때 브라우저를 띄우면 사용자가 로그인해 주기로 함(2026-07-17 합의).** 구현 중 불일치
발견 시 이 경로로 원문을 직접 대조하고 결과를 CLAUDE.md §5에 반영한다(규칙 5·6).

## 6. 테스트

1. **단위 테스트 (키 불필요, TDD):** `respx`로 HTTP 모킹 —
   - TokenManager: 발급, `expires_dt` 임박 시 재발급, 만료 응답 후 1회 재시도, revoke
   - RateLimiter: TR별 독립 버킷, 429 백오프·소진
   - Client: 헤더 구성, `cont-yn`/`next-key` 연속조회 반복, `ApiError` 변환
   - Broker: TR별 응답 → 도메인 모델 매핑 (실제 응답 형태의 픽스처 사용)
2. **라이브 스모크 (pytest 마커 `live`, 기본 제외):** 실제 `mockapi.kiwoom.com` 대상 —
   토큰 발급 → 삼성전자(005930) 현재가 → 일봉 5개 → 예수금 → 잔고. `.env`의 실키 필요.
   CI/일반 실행에서는 건너뛰고, 명시적으로 `uv run pytest -m live`로만 실행.
3. **4-에이전트 리뷰 패널 (규칙 8):** 태스크마다 diff에 대해
   senior-developer / senior-trader / architecture-expert / security-expert 전원
   통과 후 다음 태스크 진행. Critical/Important는 수정 후 재리뷰.

## 7. 완료 정의 (Definition of Done)

1. `domain/broker.py`의 `BrokerPort`와 도메인 모델이 존재하고, domain은 키움에 대한
   import가 전혀 없다.
2. 단위 테스트 전부 통과 (Phase 0의 9개 포함 전체 회귀 그린).
3. 라이브 스모크: 모의서버에서 토큰 발급·현재가·일봉·예수금·잔고 조회 성공.
4. 모든 태스크가 4-에이전트 패널 검증을 통과했다.
5. 실측으로 확인된 키움 팩트(레이트리밋, 429 스키마, 응답 필드)가 CLAUDE.md §5와
   회고록에 반영되었다.
6. `docs/retrospectives/`에 Phase 1 회고록 존재, STATUS.md가 Phase 2로 핸드오프.

## 8. 리스크 / 미해결 항목

- **비공식 스펙 리스크:** TR 필드명·응답 형태가 리서치와 다를 수 있음 → 라이브 스모크를
  각 TR 구현 직후 실행해 조기 발견 (전부 끝나고 몰아서 검증하지 않는다).
- **모의서버 가용시간:** 모의서버가 장시간(주말 등)에 일부 TR을 제한할 가능성 → 스모크
  실패 시 시간대를 기록해 원인 분리.
- **compose 포트 노출:** Phase 0 이월 권고 — 자격증명이 API를 지나가기 전에
  `127.0.0.1:8000:8000`으로 좁힌다 (이번 Phase 첫 태스크에 포함).
- **키 보안:** 실키가 `.env`에 들어가므로 시크릿 유출 방지를 security-expert 패널이
  태스크마다 점검.
