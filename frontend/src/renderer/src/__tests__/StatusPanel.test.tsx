import { render, screen } from '@testing-library/react'
import { StatusPanel } from '../components/StatusPanel'

describe('StatusPanel', () => {
  it('연결 상태를 "Backend: ok · DB: ok · Mode: mock"으로 렌더링한다', () => {
    render(<StatusPanel connected={true} db="ok" mode="mock" />)
    expect(screen.getByRole('status').textContent).toBe('Backend: ok · DB: ok · Mode: mock')
  })

  it('DB 장애 상태를 렌더링한다', () => {
    render(<StatusPanel connected={true} db="error" mode="mock" />)
    expect(screen.getByRole('status').textContent).toBe('Backend: ok · DB: error · Mode: mock')
  })

  it('백엔드 미접속 상태를 렌더링한다 (빈 화면 금지)', () => {
    render(<StatusPanel connected={false} />)
    expect(screen.getByRole('status').textContent).toContain('백엔드 미접속')
  })
})
