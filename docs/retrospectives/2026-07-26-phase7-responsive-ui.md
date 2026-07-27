# Phase 7 Task 5 회고 — PrimeReact 반응형 성과 대시보드

## 요청과 기존 상태

Task 4에서 마련한 Vite·React 기반과 `useDashboard` 조회 상태 위에,
비전문 운영자도 손익·데이터 신선도·조회 장애를 오인하지 않는 반응형
대시보드를 구현했다. 기존 `App`은 빈 mount point였고, 화면 컴포넌트·테마·
반응형 표현·당겨서 새로고침은 없었다.

구현 중 사용자가 승인한 범위 확장에 따라 하드코딩된 `모의투자` 표시를
제거하고, 현재 `Settings.run_environment`를 HTTP 응답의 transport metadata로
전달했다. 이 값은 domain `DashboardOverview`에는 넣지 않았다.

## 설계 판단

- PrimeReact 11은 현재 배포판에서 PrimeUI 라이선스 키를 요구하므로 사용하지
  않았다. MIT 라이선스의 PrimeReact `10.9.8`을 정확히 고정하고 Button, Tag,
  Skeleton, DataTable, Column을 사용했다.
- 총손익을 가장 강한 시각 계층으로 두고 확정·평가손익, 수익률, 거래 수,
  승률을 보조 KPI로 배치했다. 모든 숫자는 `Intl`의 `ko-KR`, 모든 시각은
  `Asia/Seoul`로 표시하며 승률에는 방향 부호를 붙이지 않는다.
- 차트는 사용하지 않는 Chart.js 계열 의존성을 남기지 않고, 접근 가능한
  최신·최저·최고 대체 문구를 가진 반응형 SVG로 구현했다.
- 데스크톱은 PrimeReact DataTable, 모바일은 같은 DTO를 소비하는 요약 카드로
  전환한다. 종목, 수량, 가격, 손익, 진입시각, 시세 기준시각을 두 표현에 모두
  보존하고, 모바일 손익에는 `평가손익`·`확정손익` 의미 라벨을 표시한다.
- 로딩 Skeleton, 정상 빈 상태, 최초 오류, 갱신 오류, partial/unavailable,
  손상 행과 stale 시세를 각각 다른 의미로 표시한다. 요청 기간과 마지막 성공
  데이터 기간이 다르면 실제 KPI의 표시 기간을 별도로 밝힌다.
- `environment`는 `mock | real | replay`만 허용한다. 성공 응답일 때만 실제
  배지를 표시하고, 조회 중에는 `환경 확인 중`, 오류에는 `환경 확인 불가`로
  닫아 마지막 성공 환경을 현재 실행 환경으로 오인하지 않게 했다.
- stale 포지션은 보존된 가격을 `저장 시세(오래됨)`으로 표시하고 기준시각을
  함께 제공한다. `valuation_status=unavailable`의 평가손익은 0원이나 현재
  손익으로 꾸미지 않는다.
- 당겨서 새로고침은 Pointer Events가 viewport pan을 소유할 수 없는 제약을
  고려해 non-passive Touch Events로 구현했다. 문서 최상단의 단일 하향
  제스처만 취소하며, 위·가로·다중 터치는 즉시 포기한다. 두 번째 손가락이
  잠깐 섞인 제스처도 끝까지 재소유하지 않아 pinch zoom을 보존한다. 버튼
  새로고침은 항상 제공한다.
- 테마 저장소 접근이 차단돼도 화면이 중단되지 않도록 localStorage 읽기·쓰기를
  best-effort로 처리했다.

## 변경 파일과 위치

- `backend/app/api/dashboard.py`, `backend/tests/dashboard/test_api.py`:
  `environment` transport metadata와 `mock/real/replay` API 계약.
- `backend/tests/test_migrations.py`: Task 1에서 추가된 Alembic `0015`와 현재 head
  회귀 계약을 일치시킴. 별도 0013→0014 hash column widening 검증은 보존.
- `docs/specs/2026-07-26-phase7-web-dashboard-design.md`: 현재 실행 환경 응답과
  화면 의미를 지속 설계에 반영.
- `frontend/src/App.tsx`, `frontend/src/main.tsx`: DashboardPage와 ThemeProvider
  조립, PrimeReact 10 스타일 진입점.
- `frontend/index.html`: 문서 언어 `ko`와 모바일 viewport metadata.
- `frontend/src/api/dashboard.ts`와 API·hook 테스트 fixture:
  실행 환경 enum의 fail-closed 파싱.
- `frontend/src/features/dashboard/DashboardPage.tsx`,
  `components/`: 헤더, 기간 선택, KPI, SVG 차트, 포지션·거래, 상태 피드백.
- `frontend/src/features/dashboard/format.ts`: 원화·가격·비율·KST 시각과
  `max_holding` 청산 사유 표시.
- `frontend/src/hooks/usePullToRefresh.ts`: 방향·임계값·중복·취소·멀티터치
  정책과 listener 수명주기.
- `frontend/src/theme/`: system/light/dark 해석과 저장.
- `frontend/src/styles/tokens.css`, `dashboard.css`: 의미 토큰, 라이트·다크,
  44px 터치 대상, 767px·1023px 반응형 전환, reduced-motion.
- `frontend/src/features/dashboard/__tests__/DashboardPage.test.tsx`,
  `frontend/src/hooks/__tests__/usePullToRefresh.test.tsx`: 화면 의미와 gesture 회귀.
- `frontend/tests/vite.config.test.ts`: HTML 언어·viewport metadata 회귀.
- `frontend/package.json`, `pnpm-lock.yaml`,
  `frontend/patches/minimatch@3.1.5.patch`: PrimeReact 10.9.8, pnpm 10.20.0,
  감사 가능한 의존성 해석. 사용하지 않는 Chart.js·react-chartjs-2·PrimeIcons는
  제거했다.

## TDD와 검증

초기 화면 테스트는 빈 `App`에서 15건, pull 정책은 3건이 실패하는 RED를 먼저
확인했다. 이후 가격에 손익 부호가 붙는 문제와 마우스 drag 오인도 각각 실패
테스트로 재현했다. 패널 발견 뒤 환경 응답 누락, 기간 불일치, non-null stale
가격, 초기 오류 문구, 진입시각, 승률, `max_holding`, native gesture 소유권을
테스트로 고정했다. 마지막에는 다중 터치와 두 번째 손가락 제거 후 gesture
재사용을 실제 실패로 확인한 뒤 reset 정책을 보완했다.

독립 수용 gate에서는 문서 언어·viewport metadata와 모바일 손익 의미 라벨
3건이 실패하는 RED를 먼저 확인했다. 구현 뒤 관련 27 tests와 전체 62 tests가
GREEN이 됐다.

```text
backend: uv run pytest tests/dashboard/test_api.py -q
  PASS (14 tests)
backend: uv run pytest tests/test_migrations.py -q
  PASS (3 tests)
backend: uv run pytest
  PASS (1288 tests, 11 deselected)

frontend: pnpm install --frozen-lockfile
  PASS
frontend: pnpm lint
  PASS
frontend: pnpm typecheck
  PASS
frontend: pnpm test
  PASS (5 files, 62 tests)
frontend: pnpm build
  PASS
frontend: pnpm audit
  PASS (알려진 취약점 0)
git diff --check
  PASS
```

프로덕션 빌드는 JS 635.54 kB(gzip 179.93 kB)로 Vite의 500 kB chunk 경고가
남는다. sourcemap의 포함 source 기준으로 React DOM production client가 약
536 kB, PrimeReact DataTable 본체가 약 316 kB이며, DataTable이 선택 기능을
위해 참조하는 Dropdown·InputNumber·Paginator·VirtualScroller도 함께 포함된다.
앱 코드는 이미 `primereact/datatable`, `primereact/button` 같은 컴포넌트별
경로만 import하므로 제거 가능한 barrel 또는 미사용 PrimeReact import는
발견되지 않았다. 단순 tree-shaking 설정으로 줄일 수 있는 경고가 아니며,
후속 최적화는 DataTable 지연 로딩이나 vendor chunk 분리를 실제 초기 로드
측정과 함께 검토해야 한다.

초기 전체 회귀에서 Alembic head `0015`와 head 테스트 기대 `0014`의 불일치를
발견했다. Task 1의 `0015_trade_position_mark.py`가 현재 head인 사실과 대조해
테스트 이름·assertion만 `0015`로 정정했고, 0013→0014 widening 테스트는 그대로
보존했다. fresh 전체 회귀는 1,288개 모두 통과했다.

## 대표 viewport 수용 범위

Firefox 153 headless와 WebDriver BiDi `browsingContext.setViewport`로 브라우저
내부 viewport를 정확히 고정했다. 실제 API·DB 대신 동일 출처의 synthetic
DashboardOverview fixture를 same-origin으로 제공해 다음 결과를 확인했다.

| viewport | client/scroll width | 표현 | 최소 조작 높이 | 가로 넘침 |
|---|---:|---|---:|---|
| 375×812 | 363/363px | 모바일 카드 | 44px | 없음 |
| 768×1024 | 756/756px | DataTable | 44px | 없음 |
| 1440×900 | 1428/1428px | DataTable | 44px | 없음 |

- 세 크기의 light/dark 전체 화면과 viewport screenshot을 육안 검토했다.
- 375px 카드에서 `평가손익`·`확정손익` 라벨과 동일 DTO의 핵심 값·시각을
  확인했고, 데스크톱·태블릿에서는 표가 잘리지 않았다.
- light/dark 기본 본문 대비는 각각 약 14.94:1, 14.93:1이었다.
- 600px 스크롤 뒤 sticky header의 top은 0이었다. 최초 screenshot에서 반투명
  header 아래 KPI가 비치는 문제를 발견해 header 배경을 불투명 surface로
  고친 뒤 재촬영했다.
- synthetic TouchEvent로 pull ready 상태를 만들었을 때 indicator가 header
  아래에 위치해 겹치지 않았다. gesture 정책 자체는 Vitest로 별도 검증했다.
- 증거와 재현 방법은
  `.superpowers/sdd/2026-07-26-phase7-web-dashboard-plan/task-5-viewport-evidence/`
  에 기록했다.

물리 손가락 입력과 브라우저 chrome까지 포함한 실기기 검증은 수행하지
않았으므로, pull 배치는 synthetic TouchEvent 수용으로 한정해 해석해야 한다.

실제 API, 키움 인증, 주문, live test, 외부 모델 전송은 수행하지 않았다.

## 리뷰 패널

최초 네 관점 리뷰의 Important는 stale 가격 오인, 기간과 KPI 불일치, 고정 환경
배지, 환경 전환 오류, 초기 오류 문구, 누락 시각·청산 사유, pull의 UA 소유권,
미사용 의존성이었다. 각 발견을 테스트 우선으로 수정했다. 최종 동일 diff의
재검토 결과는 다음과 같다.

- senior-developer: Critical 0, Important 0, 승인.
- senior-trader: Critical 0, Important 0, 승인.
- architecture-expert: Critical 0, Important 0, 승인.
- security-expert: Critical 0, Important 0, 승인.

독립 viewport gate 수정 후에도 같은 네 관점이 재검토해 모두 Critical 0,
Important 0으로 승인했다. 다음 Minor는 현재 수용을 무효화하지 않아 후속
유지보수 항목으로 남겼다.

- pull indicator의 `top: 96px` 고정 오프셋은 현재 header와 약 3.72px 간격이라
  향후 문구 wrapping·글꼴 확대 시 실제 header 높이와 연동하는 편이 안전하다.
- evidence README에 도구와 결과는 있지만 WebDriver 세션부터 BiDi 명령까지의
  완전한 재현 명령·원문 출력은 추가할 수 있다.
- 태블릿의 비핵심 열 숨김이 의미 class가 아닌 `nth-child(4)`에 결합돼 있어
  향후 열 순서 변경 시 명시적인 열 class로 바꾸는 편이 유지보수에 유리하다.
