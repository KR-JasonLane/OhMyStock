---
name: ohmystock-review-panel
description: OhMyStock 구현 변경을 네 전문 리뷰어와 조건부 키움 API 리뷰어로 병렬 검토하고 발견을 수정·재검증할 때 사용한다. 코드 변경이 없는 조사나 문서 요약에는 사용하지 않는다.
---

# OhMyStock 리뷰 패널

1. 검토할 구현 태스크의 범위와 diff를 확정한다. 사용자의 기존 미관련 변경은 포함하지 않는다.
2. 같은 diff와 관련 계획·사양을 다음 프로젝트 커스텀 에이전트에 병렬로 전달한다.
   - `senior-developer`
   - `senior-trader`
   - `architecture-expert`
   - `security-expert`
3. 변경이 키움 브로커 어댑터, TR 요청/응답, 인증, 레이트리밋, 페이지네이션, 주문 또는 PRE-GATE 실측 코드에 닿으면 `broker-api-expert`도 추가한다.
4. 모든 리뷰어는 읽기 전용으로 조사하고, 각 발견에 `file:line`, 영향, 근거, 수정 방향을 포함해야 한다.
5. 결과를 중복 제거해 Critical, Important, Minor 순으로 통합한다. 서로 충돌하는 지적은 관련 코드와 프로젝트 실측 근거로 판정하고 임의로 다수결하지 않는다.
6. Critical과 Important를 수정한다. 수정 범위가 원래 태스크를 실질적으로 벗어나면 사용자에게 보고하고 방향을 받는다.
7. 수정한 관점의 리뷰어에게 재검토를 요청한다. 네 상시 리뷰어가 승인하고, 조건부 리뷰어를 사용했다면 그 리뷰어도 승인해야 완료로 판정한다.
8. 리뷰 결과와 수정·검증 내용을 해당 구현 회고에 기록한다.

키움 API 리뷰에서는 `docs/reference/project-context.md` §5, `.superpowers/sdd/`, 관련 회고의 실측 증거가 공식 문서와 충돌하면 실측을 우선한다. 리뷰 과정에서 실제 주문, 토큰 발급, 외부 상태 변경을 실행하지 않는다.
