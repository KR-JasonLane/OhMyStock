export interface StatusPanelProps {
  connected: boolean
  db?: 'ok' | 'error'
  mode?: 'mock' | 'real'
}

export function StatusPanel({ connected, db, mode }: StatusPanelProps): React.JSX.Element {
  if (!connected) {
    return <div role="status">백엔드 미접속 — 재연결 시도 중…</div>
  }
  return (
    <div role="status">
      Backend: ok · DB: {db} · Mode: {mode}
    </div>
  )
}
