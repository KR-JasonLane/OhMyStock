# OhMyStock 프런트엔드

Vite와 React 기반의 조회 전용 대시보드입니다. 브라우저는 같은 origin의
`/api/dashboard/overview`만 호출하며, 운영 배포에서는 프런트 프록시가
백엔드 읽기 API로 전달합니다.

```bash
pnpm install --frozen-lockfile
pnpm dev
pnpm lint
pnpm test
pnpm typecheck
pnpm build
```
