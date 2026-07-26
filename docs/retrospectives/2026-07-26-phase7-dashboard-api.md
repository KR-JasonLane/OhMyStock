# Phase 7 Task 3 회고 — 조회 전용 Dashboard API

## 요청과 기존 상태

관리매매 성과 읽기 모델(`DashboardStore.overview`)을 FastAPI의
`GET /dashboard/overview`로 노출한다. 이 API는 현재 실행 환경의 SQL 읽기
모델만 사용하며, 브로커·키움 API·운영 제어·거래 시작/중지 경로에 의존하지
않아야 한다.

Task 2는 기간, 집계 상태, 비용 상태, 최신성, 경고를 포함한 프레임워크 독립
`DashboardOverview`와 SQL store를 이미 제공했다. HTTP 경계와 앱 조립은 없어서
요청이 404였다.

## 설계 판단

- `from`, `to`가 모두 없으면 Asia/Seoul 오늘을 끝으로 양 끝 포함 30일
  (`오늘 - 29일`부터 오늘)을 사용한다. 한쪽만 준 요청은 숨은 기본값을 만들지
  않고 422로 거절한다.
- 사용자 지정 기간은 양 끝 포함 366일까지만 허용한다. 역전·초과·형식 오류와
  Asia/Seoul 이외 timezone은 422다.
- DTO는 Pydantic `from_attributes`로 domain 값을 변환한다. 비율 `Decimal`은
  field serializer로 JSON number로 내보내며, 금액·날짜 포맷 책임은 UI에 남긴다.
- store 예외는 로그에 예외 **타입만** 남기고 응답에는
  `{"code":"dashboard_unavailable"}`와 503만 반환한다. 원문 예외·응답 본문·토큰·계좌·앱키는
  응답에 포함하지 않는다.
- `recent_trades`는 최신순 최대 100건만 반환한다. store는 상세 projection을
  정확한 KST 반열린 기간 뒤 SQL `ORDER BY closed_at DESC, id DESC LIMIT 100`으로
  제한하고, API도 비정상 store 구현의 초과 반환을 100건에서 다시 자른다. 반대로 summary와
  equity_curve는 기간 전체 거래가 필요하므로 100건으로 잘라 성과를 왜곡하지
  않는다.
- summary/equity와 recent projection은 같은 읽기 snapshot에서 실행한다.
  PostgreSQL은 `REPEATABLE READ`, SQLite 테스트 경로는 명시 `BEGIN`으로 두
  SELECT 사이 새 청산이 한 응답의 두 영역에 다르게 섞이지 않게 한다.
- 앱 lifespan에서 기존 SQL engine으로 `DashboardStore`만 조립하고 router를
  포함했다. 새 인증, 브로커, TradingService, OperationsControl 의존성은 추가하지
  않았다.

## 변경 파일과 위치

- `backend/app/api/dashboard.py`: 쿼리 상한 검증, Pydantic 응답 DTO, 안전한
  503 변환, 최근 거래 100건 직렬화 방어, `GET /dashboard/overview`.
- `backend/app/main.py`: `DashboardStore` lifecycle 조립과 dashboard router 등록.
- `backend/tests/dashboard/test_api.py`: 기본·경계 기간, timezone, 환경 전달,
  JSON 영역/Decimal/datetime, 오류 비노출, 최근 거래 100건 방어, 쓰기·브로커
  비의존 계약.
- `backend/app/store/dashboard_store.py`: 최근 거래용 bounded SQL projection.
- `backend/tests/store/test_dashboard_store.py`: 전체 성과 보존과 SQL `LIMIT 100`
  계약, KST 종료 경계와 단일 읽기 snapshot 계약.

## TDD와 검증

1. RED: `uv run pytest tests/dashboard/test_api.py -q`에서 router 부재로 9개
   계약 테스트가 404로 실패했다.
2. GREEN: 최소 router·DTO·조립 뒤 같은 테스트가 `9 passed`가 됐다.
3. 회귀 중 `API_WRITE_TOKEN` 설정 시 engine 초기화가 잘못 들여쓰기되어
   기존 보안 테스트가 실패했다. 기존 `tests/test_api_security.py`가 재현했고,
   engine 초기화를 조건문 밖으로 복원한 단일 수정 뒤 다음 검증을 통과했다.

```text
cd backend
uv run pytest tests/dashboard tests/test_api_security.py tests/test_app_lifespan.py -q
# 이후 recent SQL 경계·snapshot 보강 전 기준 34 passed
```

`git diff --check`도 통과했다. 실제 키움 API, 토큰 발급, 주문, 라이브 테스트는
실행하지 않았다.

추가로 API DTO의 모든 시각은 `AwareDatetime`으로 검증한다. naive 시각을 넣은
store fixture는 기존에 200으로 offset 없이 직렬화되는 RED를 보였고, route 내부
DTO 검증 뒤 안정된 503으로 격리되는 GREEN을 확인했다.

사용자 결정으로 최근 거래 응답 상한을 추가했다. RED에서 101개 거래를 반환한
store 결과가 API에 101개로 직렬화됐고, 101개 SQL fixture도 recent_trades 101개를
반환했다. GREEN에서는 API가 100개로 방어하고 store의 별도 최신순 SQL projection이
`LIMIT 100`을 사용한다. 같은 fixture에서 closed trade count와 equity curve는 101개를
유지해 성과 집계를 희생하지 않음을 확인했다.

재검토에서 coarse UTC 여유 범위가 종료일 뒤 100개 행으로 recent `LIMIT`을
소진할 수 있고, 두 SELECT가 다른 snapshot을 읽을 수 있음이 드러났다. 종료일
뒤 KST 7월 26일 00:00 행 100개와 기간 내 행 101개를 섞은 RED에서 recent가
0개였고, 정확 KST SQL 범위 뒤 limit을 적용한 GREEN에서 기간 내 100개를
반환했다. WAL SQLite에서 첫 SELECT 뒤 별도 연결로 청산을 확정하는 RED도
recent에만 새 행이 섞였으며, 명시 transaction GREEN에서는 summary와 recent
모두 최초 snapshot만 읽었다.

## 자체 검토와 패널

- 자체 검토: SQL read model 외 경로 호출이 없고, `run_environment`가 settings에서
  store로 전달되며, 예외 문자열과 민감 키가 response에 없음을 확인했다.
- `senior-developer`: datetime offset 계약 누락 Important를 발견했다. 모든 DTO
  시각의 aware 검증·naive 503 회귀로 수정했고 재검토 승인.
- `architecture-expert`: 같은 offset 계약 Minor를 발견했다. 수정 후 재검토
  승인. 이후 KST limit 선행·두 projection snapshot 불일치 Important를 발견했고,
  정확 KST 범위와 transaction 보강 뒤 재검토에서 Critical/Important/Minor 없음,
  승인.
- `senior-trader`: Critical/Important/Minor 없음, 승인.
- `security-expert`: 내부 오류 비노출·읽기 전용 경계는 승인. 앱 인증 부재와
  요청률 제한은 현 태스크의 “인증 의존성 추가 금지” 및 localhost/Tailscale 단일
  운영자 설계 전제와 일치하므로 차단 발견이 아니라 **외부 공개·복수 사용자 전환
  전 Important 게이트**로 기록한다. 사용자 결정에 따라 앱 인증은 추가하지 않고,
  recent_trades 100건 상한은 이번 태스크에서 수정했다. KST limit·snapshot 보강
  재검토도 Critical/Important/Minor 없음, 승인.

브로커 어댑터·TR·주문·인증·페이지네이션을 변경하지 않았으므로
broker-api-expert는 범위 밖이다.

Task 6 프런트 프록시는 요청률 제한을 필수 조건으로 설계·검증해야 한다. 이
이관은 backend 인증 경계를 바꾸지 않으며, 외부 공개·복수 사용자 전환 때는
프록시 신원 검증을 포함한 앱 인증·인가도 별도 게이트로 다시 검토한다.

최종 관련 회귀는 `uv run pytest tests/dashboard tests/store/test_dashboard_store.py
tests/test_api_security.py tests/test_app_lifespan.py -q`로 **42 passed**(기존
TestClient deprecation warning 1건)를 확인했다.
