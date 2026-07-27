# 아침 AI 분석 Telegram — Task 1 회고

## 요청과 기존 상태

성공한 아침 AI 분석을 외부 호출 없이 읽기 쉬운 Telegram plain text로
표현할 순수 도메인 모델과 presenter가 필요했다. 기존에는 분석 결과를
Telegram 알림용으로 분류·표시하는 값 객체가 없었고, 자동 outbox와 SQL read
model은 후속 Task의 범위다.

## 설계 판단

- `AnalysisVerdictSummary`와 `MorningAnalysisSummary`를 immutable DTO로 두고,
  환경·시각·confidence·후보 순위·최종 후보 상한을 생성 시점에 fail-closed로
  검증했다. `picked`는 반드시 `approve`이며 rank는 중복될 수 없다.
- 최종 후보는 rank 오름차순, 차순위 승인은 confidence 내림차순 뒤 symbol
  오름차순으로 고정했다. 최종 후보의 근거와 위험은 각각 2개, 차순위는 3개만
  보이며 전체 차순위 수를 별도로 표시한다.
- 빈 근거 또는 위험은 손상 JSON이 빈 tuple로 격리되는 다음 Task 경계와
  호환되도록 `확인 불가`로 표시한다. 요약 전체는 계속 전달한다.
- 환경은 `mock`/`real`만 허용하고, 국면도 allowlist 뒤 한국어로 표시한다.
  동적 문자열은 제어문자를 공백으로 정규화하되 HTML/Markdown 변환 없이 기존
  `render_parts`의 plain-text 분할 경로로 넘긴다.

## 변경 위치

- `backend/app/domain/notifications/analysis_summary.py:28` — 안전한 DTO,
  idempotency key, 결정론적 presenter와 분할 renderer를 추가했다.
- `backend/tests/notifications/test_analysis_summary.py:52` — 후보·차순위·상한,
  손상 표시, plain text, 입력 거부와 결정론 회귀를 추가했다.

## TDD 및 검증

- RED 1: production 모듈 생성 전에
  `cd backend && uv run pytest tests/notifications/test_analysis_summary.py -q`를
  실행했고, 의도대로 `ModuleNotFoundError: app.domain.notifications.analysis_summary`
  (collection error 1건)가 발생했다.
- GREEN 1: DTO와 presenter 최소 구현 뒤 같은 테스트가 `18 passed`가 됐다.
- RED 2: 리뷰 발견(후보 무결성·국면/날짜 경계·빈 필드 표시)을 테스트로 먼저
  고정했다. 후보/국면/날짜 테스트는 `3 failed, 18 passed`, 빈 필드 표시는
  `1 failed, 21 passed`로 각각 의도대로 실패했다.
- RED 3: 보안 리뷰의 개행·제어문자 구조 위조와 카디널리티 테스트는
  `2 failed, 26 passed`, surrogate 테스트는 `1 failed, 28 passed`로 각각
  의도대로 실패했다.
- GREEN 2: fail-closed 후보 불변조건, 국면 mapping, exact date, 빈 필드
  `확인 불가` 표현을 보완했다.
- GREEN 3: 모든 동적 표시 문자열에서 CR/LF·line separator·C0/C1·BiDi를
  포함한 format control·UTF-8 불가 surrogate를 공백화한 뒤 길이를 제한했다.
  기존 분석 계약과 맞춰 verdict 20개, 최대 후보 5개, reasons 3개, risk flags
  5개도 생성 시점에 상한을 강제했다. 최종 관련 회귀는
  `cd backend && uv run pytest tests/notifications/test_analysis_summary.py tests/notifications/test_formatting.py -q`
  에서 `40 passed in 0.04s`였고, `git diff --check`도 통과했다.

## 리뷰와 잔여 우려

- 초기 트레이딩·아키텍처 리뷰는 거절 verdict의 최종 후보화, 후보 상한/rank
  불변조건, 손상 필드 표현을 Important로 지적했다. 모두 수정 후 두 관점의
  재검토에서 Critical/Important/Minor 없음으로 승인됐다.
- 개발 리뷰의 국면 allowlist·한국어 mapping 및 exact date Minor도 함께
  보완했고, 재검토에서 Critical/Important/Minor 없음으로 승인됐다.
- 보안 최초 리뷰는 동적 문자열의 제어문자 기반 섹션·환경 위조와 무제한
  cardinality를 Important로 지적했다. 정규화·상한·필드별 회귀를 추가한 뒤
  보안 재검토에서 Critical/Important/Minor 없음으로 승인됐다.
- 아키텍처 재검토는 Unicode surrogate가 UTF-8 전달 직전에 실패할 수 있음을
  Important로 지적했다. `Cs` 정규화와 회귀를 추가한 뒤 재재검토에서
  Critical/Important/Minor 없음으로 승인됐다.
- 독립 gate 수정 라운드 1은 positive·unique만으로는 `rank=99` 또는
  `(1, 3)` gap을 막지 못함을 지적했다. 최종 후보 rank 집합을 정확히
  `1..len(picked)`로 fail-closed 검증하고 두 회귀를 RED/GREEN으로 추가했다.
- 이 Task는 Telegram·키움·주문·AI 재실행·운영 DB를 호출하지 않았다. SQL
  read model이 입력 손상 행을 `corrupted_rows`로 격리하는 책임은 Task 2에
  남아 있다.
