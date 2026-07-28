# 읽기 쉬운 Telegram 장 마감 다이제스트 회고

## 요청과 기존 상태

2026-07-28 자동 발송된 장 마감 다이제스트에는 기술용 idempotency key,
UTC ISO 시각과 내부 read model JSON이 그대로 표시됐다. 저장된 값은
정확했지만, 비전문 운영자가 시장 분석·거래·계좌 상태를 한눈에 파악하기
어려웠다. 사용자는 핵심 요약 중심의 한국어 형식과 `mock`의 의미를
`모의투자`로 명확히 표시하도록 요청했다.

## 설계 판단

- 저장·재시도·감사에 쓰는 `Digest.payload` version 1과
  `digest:{environment}:{date}` key는 유지했다.
- `Digest.body`만 같은 DTO를 읽는 순수 presenter로 교체했다. broker, DB,
  Telegram adapter를 호출하지 않는다.
- `mock`은 `모의투자`, `real`은 `🚨 실전`으로 표시한다.
- 정상 정보는 분석·자동매매·계좌·다음 일정으로 나누고, 누락·손상 상태만
  `⚠️ 확인 필요`에 중복 없이 표시한다.
- 알 수 없는 상태와 내부 필드명은 추측하거나 원문을 노출하지 않고
  `확인 불가` 또는 고정 경고로 축약한다.
- `/digest`는 payload를 새로 계산하지 않으며 저장된 delivery part가 있으면
  당시 원문을 그대로 재전송하는 기존 계약을 유지한다.

## 변경 파일과 정확한 위치

- `backend/app/domain/notifications/digest.py`
  - `Digest.body`: 새 presenter 단일 진입점으로 변경
  - `_render_digest`부터 `_money`: 환경·파이프라인·거래·계좌·경고 표시와
    안전한 날짜·개수·금액 변환 추가
  - `available_deposit`은 실측 의미대로 `주문 가능`으로 표시하고,
    조회 시점 값인 포지션 수는 `현재 관리 포지션`으로 명시
  - `Digest.payload`, `Digest.bodies`, retained payload parser는 구조 변경 없음
- `backend/tests/notifications/test_digest.py`
  - 정상 exact 본문, 환경, 시장 국면, 후보 수, 손익 신뢰도, 계좌 실패,
    손상 값, 내부 정보 비노출, retained v1 동일성 회귀 추가
- `backend/tests/notifications/test_commands.py`
  - 기존 delivery part 원문 재전송 회귀 추가
- `docs/STATUS.md`
  - 완료 상태와 배포 후 읽기 전용 수용 체크포인트 갱신

## 테스트 우선 구현 결과

먼저 새 정상 본문 exact 테스트를 추가했다. 구형 구현의 `다이제스트 ID`,
ISO 시각과 JSON 본문 때문에 예상대로 실패하는 것을 확인한 뒤 presenter를
구현했다. 첫 관련 실행에서 드러난 구형 문구 기대 두 건은 새 공개 계약으로
교체했고, 알 수 없는 시장 국면은 승인 설계대로 `완료`가 아니라 줄 전체를
`확인 불가`로 표시하도록 테스트를 바로잡았다.

## 검증 결과

- 다이제스트 단위 테스트: `35 passed`
- digest command·service·store·아침 분석 E2E 관련 회귀: `141 passed`
- 전체 비라이브 회귀: `1391 passed, 11 deselected`
- `python -m compileall -q app tests`, `git diff --check`: 통과
- 기존 Starlette deprecation warning 1건 외 새 경고 없음
- 실제 Telegram, 키움 API, 주문, 분석 재실행과 운영 DB는 호출하지 않았다.

## 독립 리뷰와 수정

`senior-developer`, `senior-trader`, `architecture-expert`,
`security-expert`가 같은 diff를 독립 검토했다. 최초 리뷰의 Important를
다음과 같이 TDD로 수정했다.

- catch-up 과거 날짜와 현재 계좌 기준일이 다르면 `현재 계좌 · 기준 M월 D일`
  및 고정 경고를 표시하고, 기준일 누락도 별도 경고한다.
- `available_deposit`을 예수금이 아닌 `주문 가능`으로 바로잡았다.
- 계좌 source·금액·실현손익 신뢰도를 허용목록으로 판정해 알 수 없는 출처는
  금액을 숨기고, 누락·불명 신뢰도는 `확인 불가`와 계좌 경고로 처리한다.
- 현재 열린 포지션 수는 과거 장 마감 값처럼 보이지 않도록
  `현재 관리 포지션`으로 명시한다.

재검토 결과 네 리뷰어 모두 Critical/Important 없음으로 승인했다.
senior-developer의 조건 중복 Minor는 현재 동작 결함이 없어 후속
정규화 리팩터링 후보로 남겼다.

## 변경하지 않은 안전 경계

- 종목 수집·스코어링·AI 분석·선정 알고리즘
- 주문·체결·포지션·손익 계산
- broker adapter와 키움 TR
- DB schema와 Alembic migration
- 16:10 생성, 최근 7거래일 catch-up, sender retry와 idempotency
- version 1 payload와 기존 delivery part

## 배포 후 읽기 전용 수용

배포 뒤 다음 정상 16:10 모의 다이제스트를 확인하고 `/digest`를 한 번
조회한다. 기존 24시간 보존분은 생성 당시 구형 원문이 반환될 수 있다.
수용을 위해 계좌·분석·주문을 재실행하거나 Telegram 제어 명령을 호출하지
않는다.
