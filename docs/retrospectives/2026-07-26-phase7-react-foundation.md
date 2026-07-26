# Phase 7 Task 4 회고 — React 웹 기반과 dashboard 데이터 계층

## 요청과 기존 상태

Phase 0의 Electron renderer를 제거하고, 이후 대시보드 화면이 소비할 순수
Vite·React 기반과 조회 전용 데이터 계층을 만든다. 기존 프런트는
`127.0.0.1:8000` health 조회와 WebSocket 재접속을 수행하는 Electron skeleton이었고,
Phase 7 설계의 같은-origin `/api/dashboard/overview` 및 명시적 갱신 모델과 맞지
않았다.

## 설계 판단

- Electron main/preload/builder 설정과 상태 패널을 제거하고 Vite 표준 entry를
  `frontend/index.html`, `frontend/src/main.tsx`로 옮겼다. Task 5의 화면은 아직
  만들지 않아 `App`은 의도적으로 최소 mount point만 가진다.
- `src/api/dashboard.ts`가 HTTP·JSON 경계를 전담한다. 모든 숫자는 유한값인지,
  날짜는 offset을 가진 실제 날짜인지, 상태 enum은 허용 목록인지 검사한다.
  최근 거래가 100건을 넘는 응답도 거부한다. `as_of` 이후 시각, KST 조회 기간
  밖 청산, stale `complete` mark, 요청과 다른 응답 기간도 fail-closed로
  거부한다. 화면은 검증된 DTO만 받는다.
- URL은 상대 `/api/dashboard/overview`로 고정했다. API 실패의 사용자 노출값은
  안정된 `DashboardErrorCode`이고, 원인은 `DashboardRequestError.cause`로 분리해
  응답 본문을 로그에 남기지 않는다.
- `useDashboard`는 mount·기간 변경·명시 `refresh`만 요청한다. 진행 중 refresh는
  같은 Promise를 반환하고, 기간 변경 및 unmount는 AbortController로 이전 요청을
  취소한다. 성공에서만 data와 `lastUpdatedAt`을 함께 교체하고 실패에서는 마지막
  성공 data를 유지한다. StrictMode 개발 재실행은 microtask와 cleanup guard로
  첫 요청을 만들기 전에 취소한다. interval과 WebSocket은 만들지 않는다.
- 개발 서버는 `/api`만 loopback backend로 프록시하고 prefix를 제거한다. browser
  source의 API URL은 계속 상대 경로다.

## 변경 파일과 위치

- `frontend/package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`: Vite 명령 계약,
  Electron 의존성 제거, PrimeReact·PrimeIcons·Chart.js·react-chartjs-2 고정.
- `frontend/vite.config.ts`, `tsconfig*.json`, `vitest.config.ts`, `eslint.config.mjs`,
  `index.html`, `src/main.tsx`: 브라우저 SPA 도구체인으로 교체.
- `frontend/src/api/dashboard.ts`: dashboard DTO, runtime parser, 상대 fetch.
- `frontend/src/features/dashboard/useDashboard.ts`: 단일 요청 수명주기와 상태 보존.
- `frontend/src/api/__tests__/dashboard.test.ts`,
  `frontend/src/features/dashboard/__tests__/useDashboard.test.tsx`,
  `frontend/tests/vite.config.test.ts`: parser, hook, dev proxy 회귀.
- Electron main/preload/renderer template·assets·상태 panel 및 builder 설정을 삭제했다.

## TDD와 검증

먼저 새 parser/hook 테스트를 추가해 `dashboard.ts`, `useDashboard.ts` 모듈을 찾지
못하는 RED를 확인했다. 최소 구현 뒤 두 스위트 14개가 GREEN이 됐다. 이후 lint가
렌더 중 ref 갱신을 지적해 원인을 확인하고 refresh가 현재 period closure를 쓰도록
단일 수정했다. 패널이 발견한 StrictMode 이중 요청, dev proxy, 응답 시점·기간
교차 검증, raw cause console 노출도 각각 실패 테스트로 재현한 뒤 수정했다.

```text
pnpm install --frozen-lockfile  PASS
pnpm typecheck                  PASS
pnpm lint                       PASS
pnpm test                       PASS (3 files, 19 tests)
pnpm build                      PASS
```

실제 API, 키움 인증·호출, 주문, live 테스트는 수행하지 않았다.

## 리뷰

네 관점의 읽기 전용 리뷰를 수행했다. 최초 발견한 Important는 StrictMode의 개발
이중 fetch, Vite dev proxy 누락, 시점·기간 cross-field 검증 누락, raw error cause
console 노출이었다. 모두 테스트 우선으로 수정했고 developer·trader·architecture·
security 재검토에서 Critical/Important 0을 확인했다. CSP HTTP header 강화는 정적
프록시를 만드는 Task 6 책임으로 남긴다.
