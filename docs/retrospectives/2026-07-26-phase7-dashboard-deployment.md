# Phase 7 Task 6 회고 — 대시보드 비공개 웹 게이트웨이

## 요청과 기존 상태

Phase 7의 React SPA는 Vite 개발 서버에서 `/api`를 backend로 전달했지만,
운영용 정적 웹 서버·프록시·루프백 Compose 서비스는 없었다. 이번 Task는
`GET /api/dashboard/overview`만 공개하는 비root nginx 컨테이너와 Tailscale
Serve 운영 절차를 추가하는 범위다. 실제 주문, 키움 API, Tailscale 상태 변경,
자동매매 컨테이너 재시작은 범위 밖이다.

## 설계 판단

- nginx의 exact location만 dashboard GET을 `backend:8000/dashboard/overview`로
  전달하고, 다른 `/api`와 `/ws`는 SPA fallback보다 먼저 404로 종료한다. POST는
  405이며 backend 오류는 `proxy_intercept_errors off`로 JSON 503을 유지한다.
- 프로덕션 이미지는 검토 시점의 linux/amd64 digest로 고정한 Node/pnpm build
  stage와 `nginxinc/nginx-unprivileged` static server stage를 분리했다.
  `.dockerignore`는 비밀 환경 파일·registry 설정·인증서 패턴, source map, test,
  Electron 잔재와 build artifact를 context에서 제외한다.
- 보안 header는 CSP, `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`를 모든 gateway 응답에 설정한다.
- overview는 nginx가 실제로 보는 source IP별 초당 1건, 즉시 burst 5건으로
  제한한다. 한도를 넘으면 upstream 호출 없이 JSON 429와 `Retry-After: 1`을
  반환한다. 현재 단일 운영자·Tailscale localhost proxy는 같은 source로 취급하며,
  source 식별을 위해 전달 header를 신뢰하지 않는다.
- Compose 의존성은 `frontend → backend` 한 방향이다. frontend는 루프백
  `127.0.0.1:3000`만 바인딩하고 `restart: unless-stopped`와 독립 healthcheck를
  사용하므로 gateway 장애가 backend 재시작 조건이 아니다. unprivileged nginx
  image의 `/tmp` temp path만 noexec/nosuid tmpfs로 열고 root filesystem은 읽기
  전용으로 두며, 모든 Linux capability와 신규 privilege 획득을 차단한다.
- 독립 gate에서 nginx 공식 보안 공지의 CVE-2026-42533 등 취약 범위
  `0.9.6–1.31.2`를 확인해 runtime을 안전한 `1.31.3-alpine` linux/amd64 digest로
  올렸다. routing test는 fake backend와 frontend readiness를 각각 명시적으로
  확인하고 container IP를 한 번만 조회한다. requester는 host의 0700 `mktemp`
  디렉터리를 bind mount하지 않고 container `/tmp` 결과를 `docker cp`로 회수한다.
  새 고정 requester source의 단일 셸에서 7개 요청을 background로 동시에 시작해
  정확히 6개의 200과 1개의 429·body·`Retry-After`·보안 header를 검사해 시간 기반
  sleep과 host Docker 제어면 지연을 제거했다. fake backend/requester도 검토한
  linux/amd64 digest로 고정했다. 수용 runbook은 Bash `set -euo pipefail`과 `jq -eS`로
  상태 curl 실패를 fail-closed 처리하고, invariant 비교 불일치 시 임시 snapshot 경로를
  남기고 즉시 실패하므로 이후 cleanup 성공으로 검증 실패가 가려지지 않는다.

## 변경 파일과 위치

- `frontend/Dockerfile`, `frontend/nginx.conf`, `frontend/.dockerignore`: 비root
  production 이미지, allowlist 프록시·rate limit·header, build context 경계.
- `docker-compose.yml`: `frontend` 서비스와 loopback port/healthcheck/단방향
  backend 의존성.
- `frontend/tests/nginx-routing.sh`: 실제 frontend image와 fake backend에서
  SPA, prefix 제거, 405/404, upstream JSON 503, WebSocket 비노출, 보안 header,
  429와 `Retry-After`를 검증하는 routing contract.
- `docs/runbooks/dashboard.md`: 안전한 빌드·상태 점검, Tailscale Serve 수용과
  제거 절차, rate limit 운영 의미.

## TDD와 검증

먼저 `frontend/tests/nginx-routing.sh`를 추가했다. 구현 전 실행은 Compose에
frontend 서비스가 없어서 예상대로 `no such service: frontend`로 RED였다.
최소 gateway 구현 뒤 agent 계정은 Docker socket 권한이 없어 정적 검증까지만
수행했다. 이후 사용자가 권한 있는 환경에서 frontend build/up과 정적 frontend를
확인하고 routing contract를 실행해 마지막 `nginx routing contract passed`까지
확인했다. 이 Docker GREEN은 사용자 보조 수용 증거이며 agent가 직접 실행한 결과로
기록하지 않는다.

실제 주문·키움 API·Tailscale Serve 상태 변경·backend 재시작은 수행하지 않았다.

## 리뷰

네 관점의 읽기 전용 리뷰를 수행했다. trader는 최초부터 승인했다. developer는
routing test의 중복 image build Minor를 지적해 Compose image 재사용으로 정리했다.
architecture는 POSIX awk `IGNORECASE` 오용 Important와 header 계약 누락 Minor를
지적해 `tolower()` parser와 응답별 header assertion으로 수정했다. security는 mutable
base image Important와 context/header/container hardening Minor를 지적해 linux/amd64
digest pin, credential denylist, header assertion, read-only root·capability drop·
no-new-privileges·`/tmp` tmpfs로 수정했다. developer·architecture·security 재검토와
trader를 포함한 네 관점 모두 Critical/Important 0으로 승인했다.

독립 gate 수정 라운드에서도 네 관점 재검토를 수행했다. developer는 runbook의
`cmp` 실패가 cleanup 성공으로 가려지는 Important를 발견해 각 불일치에서 snapshot을
보존하고 `exit 1` 하도록 수정했고 재검토에서 Critical/Important 0이었다. security는
fake backend와 requester의 mutable test image tag Minor를 발견해 linux/amd64 digest로
고정했고 재검토에서 Critical/Important 0이었다. architecture와 trader는 최초 검토부터
Critical/Important 0이었다.

독립 gate 수정 라운드 2에서는 architecture가 state snapshot `curl | jq`의 pipefail
Important와 host Docker 제어면에서 발생하는 rate-test 동시성 Minor를 지적했다.
`set -euo pipefail`·`jq -eS`, requester 내부 단일 셸의 background curl로 수정했고
architecture 재검토에서 Critical/Important 0이었다. developer·security·trader도
round 2에서 Critical/Important/Minor 0이었다.

최종 운영 정합에서는 자동매매가 장중 정상 `running`일 수 있음을 반영했다. 최초
frontend 수용은 expected `running` 자체로 중단하지 않고, 전후 `run_id`/`status`/
`kill_switch` 불변 비교가 달라질 때만 중단한다. `dead`, 예상 밖 trading 상태,
예상 밖 kill switch는 여전히 사전 중단 조건이다.

실 Docker 수용에서 routing contract의 overview GET이 502로 RED를 재현했다. 원인은
production nginx upstream이 `backend:8000`인데 test fake backend가 `listen 80`이고
readiness도 기본 80만 확인한 포트 계약 불일치였다. fixture와 readiness를 모두
`8000`으로 바꿨다. 수정 뒤 사용자가 같은 routing test를 다시 실행해
`nginx routing contract passed`를 확인했다. backend는 이후 별도로 재빌드됐으며,
현재 health와 dashboard proxy의 정상 응답은 coordinator가 확인했다.

이 실수용 정정도 developer·security·architecture·trader 네 관점 재검토에서
Critical/Important/Minor 0이었다.

## 수용 결과와 남은 범위

사용자 보조로 frontend build/up, 정적 frontend, routing contract GREEN을 확인했고,
backend 재빌드 뒤 현재 health와 dashboard proxy도 정상이다. backend가 이후 별도로
재빌드됐으므로 이번 출력만으로 최초 frontend-only 배포 전후 identity와 scheduler/
trading invariant 보존까지 소급해 증명하지는 않는다. Tailscale Serve 상태 변경은
여전히 사용자 승인 뒤 별도 수용 범위다. 커밋은 사용자 사전 승인이 필요하다.
