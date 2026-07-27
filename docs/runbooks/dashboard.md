# 대시보드 비공개 배포 운영 절차

## 목적과 경계

대시보드는 Docker `frontend` 컨테이너가 `127.0.0.1:3000`에서만 제공한다.
브라우저가 호출할 수 있는 backend 경로는 `GET /api/dashboard/overview` 하나이며,
nginx가 내부 Docker 네트워크의 `backend:8000/dashboard/overview`로만 전달한다.
`8000`(backend), `15432`(DB), replay 포트는 Tailscale Serve 대상이 아니다.

Serve의 Tailnet ACL은 운영자 기기/사용자만 이 노드의 HTTPS 대시보드에 접근하게
유지한다. ACL을 넓히거나 외부 공개(Funnel)를 사용하지 않는다.

## 최초 frontend 배포 수용

이 절차는 `frontend`만 새로 만들며 backend, scheduler, 자동매매 컨테이너를
재시작하지 않는다. 실제 주문·키움 API 호출·Tailscale 상태 변경을 하지 않는다.
작업 시작 시 backend가 정상 실행 중이어야 하며, scheduler/trading 상태가 자연히
바뀔 수 있는 장중에는 상태가 안정된 창에서 수행한다.

### 1. 사전 상태를 민감정보 없이 스냅샷

아래는 backend의 container ID/start time, scheduler의 `enabled`/`paused`/`dead`,
trading의 `run_id`/`status`/`kill_switch`만 임시 디렉터리에 저장한다. 원문 응답,
계좌·주문·경고 상세는 파일에 쓰지 않는다.

```bash
set -euo pipefail
umask 077
dashboard_snapshot=$(mktemp -d)
backend_container=$(docker compose ps -q backend)
test -n "$backend_container"
docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$backend_container" \
  >"$dashboard_snapshot/backend.identity.before"
curl --fail --silent --show-error http://127.0.0.1:8000/schedule/status \
  | jq -eS '{enabled, paused, dead}' \
  >"$dashboard_snapshot/scheduler.invariants.before.json"
curl --fail --silent --show-error http://127.0.0.1:8000/trade/status \
  | jq -eS '{run_id, status, kill_switch}' \
  >"$dashboard_snapshot/trading.invariants.before.json"
```

다음 필드는 관측 시각, 다음 tick, 최근 event, 누적 건수, warning, positions 및
거래 시작/종료 시각처럼 scheduler/trading이 정상 동작해도 바뀔 수 있으므로 비교
대상이 아니다. 반면 위 세 invariant 파일과 backend identity는 이 frontend-only
절차 중 바뀌면 안 된다. expected trading `running`은 허용하되 배포 전후
`run_id`/`status`/`kill_switch`가 반드시 동일해야 한다. 사전 상태가 이미
`dead=true`, 예상치 못한 trading 상태 또는 kill switch이면 수용을 멈추고 운영
상태부터 읽기 전용으로 복기한다.

### 2. frontend만 build·기동

```bash
docker compose config --quiet
docker compose build frontend
docker compose up -d --no-deps frontend
docker compose ps frontend
```

`--no-deps`가 핵심이다. frontend의 장애·재기동이 backend의 재기동 조건이 되지
않아야 한다.

### 3. routing과 loopback 확인

Docker 권한이 있는 수용 환경에서 production gateway 계약 전체를 실행한다.

```bash
frontend/tests/nginx-routing.sh
curl --fail --silent --show-error http://127.0.0.1:3000/ >/dev/null
curl --fail --silent --show-error \
  'http://127.0.0.1:3000/api/dashboard/overview?from=2026-07-01&to=2026-07-27&timezone=Asia%2FSeoul' \
  >/dev/null
```

### 4. 사후 상태 비교

```bash
backend_container=$(docker compose ps -q backend)
test -n "$backend_container"
docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$backend_container" \
  >"$dashboard_snapshot/backend.identity.after"
curl --fail --silent --show-error http://127.0.0.1:8000/schedule/status \
  | jq -eS '{enabled, paused, dead}' \
  >"$dashboard_snapshot/scheduler.invariants.after.json"
curl --fail --silent --show-error http://127.0.0.1:8000/trade/status \
  | jq -eS '{run_id, status, kill_switch}' \
  >"$dashboard_snapshot/trading.invariants.after.json"
if ! cmp -s "$dashboard_snapshot/backend.identity.before" "$dashboard_snapshot/backend.identity.after"; then
  echo "backend identity changed; snapshot retained at $dashboard_snapshot" >&2
  exit 1
fi
if ! cmp -s "$dashboard_snapshot/scheduler.invariants.before.json" "$dashboard_snapshot/scheduler.invariants.after.json"; then
  echo "scheduler invariant changed; snapshot retained at $dashboard_snapshot" >&2
  exit 1
fi
if ! cmp -s "$dashboard_snapshot/trading.invariants.before.json" "$dashboard_snapshot/trading.invariants.after.json"; then
  echo "trading invariant changed; snapshot retained at $dashboard_snapshot" >&2
  exit 1
fi
rm -rf "$dashboard_snapshot"
```

세 `cmp` 중 하나라도 다르면 frontend를 다시 조작하지 말고 수용을 중단한다.
변경된 invariant는 허용할 자연 변동 필드가 아니다. 동일 시각에 scheduler가 새
trade run을 시작하는 등의 외부 자연 사건과 겹쳤을 가능성은 scheduler logs와
recent event를 **읽기 전용**으로 확인해 따로 기록한다. backend ID/start time이
바뀌었다면 frontend-only 배포가 backend를 재시작하지 않았다는 조건을 충족하지
못한 것이다.

## 요청률 제한

`/api/dashboard/overview`은 nginx가 관측하는 source IP별로 초당 1건, 즉시 burst
5건(첫 요청을 포함해 연속 6건)을 허용한다. 초과분은 backend로 전달하지 않고 JSON
`429`와 `Retry-After: 1`로 응답한다.

현재 화면에는 자동 폴링이 없고, 진행 중인 요청은 하나로 합쳐진다. 따라서 화면
첫 조회·명시 새로고침·당겨서 새로고침은 정상 사용에서 제한을 넘지 않는다. 빠른
연속 클릭은 1초 뒤 재시도한다. Tailscale Serve는 localhost 프록시를 통해 들어오므로
이 제한은 현재 단일 운영자/단일 프록시 source에 적용된다. identity header나
`X-Forwarded-For`는 rate key로 신뢰하지 않는다.

복수 사용자나 외부 공개로 바꾸려면 source 식별·검증을 포함한 앱 인증/인가,
API·응답·쿼리 상한을 별도 Important 게이트로 설계하고 승인받는다.

## Tailscale Serve 수용 (명시적 사용자 승인 후에만)

실행 직전에 설치된 CLI 문법과 기존 공개 상태를 확인한다. Tailscale CLI는
버전에 따라 Serve 문법이 바뀔 수 있으므로, 문서에 적힌 명령을 그대로 가정하지
않는다.

```bash
tailscale version
tailscale serve --help
tailscale serve status
```

현재 공식 문서의 기본 HTTPS reverse-proxy 형식은 다음과 같다. 위 `--help` 출력과
다르면 그 버전의 도움말을 우선하고, 변경 명령은 사용자 승인 뒤에만 실행한다.

```bash
sudo tailscale serve --bg http://127.0.0.1:3000
tailscale serve status
```

상태 출력에서 HTTPS URL의 `/`가 `http://127.0.0.1:3000`으로만 proxy되는지
확인한다. 같은 tailnet에 연결한 모바일 기기에서 그 HTTPS URL을 열어 조회한다.
Tailscale Serve는 tailnet 내부 공유용이며 Funnel은 인터넷 공개이므로 사용하지
않는다.

제거 전에는 먼저 `tailscale serve status`로 이 대시보드 외 Serve 항목이 없는지
확인한다. 이 대시보드만 제거할 때는 현재 CLI 도움말에 맞는 `off` 명령(현행
기본 설정에서는 `sudo tailscale serve off`)을 사용하고 다시 status를 확인한다.
다른 Serve 구성이 있을 수 있으면 `tailscale serve reset`을 사용하지 않는다.

공식 문법과 Serve/ACL 동작은 [Tailscale Serve CLI 문서](https://tailscale.com/docs/reference/tailscale-cli/serve)와
[Tailscale Serve 기능 문서](https://tailscale.com/docs/features/tailscale-serve)를 실행 직전에 다시 확인한다.
