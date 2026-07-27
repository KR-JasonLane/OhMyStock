import '@testing-library/jest-dom/vitest'

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../../App'
import type { DashboardOverview } from '../../api/dashboard'
import type { DashboardQueryState } from '../../features/dashboard/useDashboard'

const refresh = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)

const overview = {
  environment: 'mock',
  period: { start: '2026-07-01', end: '2026-07-25', timezone: 'Asia/Seoul' },
  summary: {
    realized_pnl: 0,
    realized_pnl_status: 'complete',
    unrealized_pnl: 0,
    unrealized_pnl_status: 'complete',
    total_pnl: 0,
    total_pnl_status: 'complete',
    realized_return_pct: null,
    closed_trade_count: 0,
    incomplete_closed_trade_count: 0,
    wins: 0,
    losses: 0,
    draws: 0,
    win_rate: null,
    cost_basis: 'estimated'
  },
  equity_curve: [],
  positions: [],
  recent_trades: [],
  freshness: {
    as_of: '2026-07-25T09:30:00+09:00',
    mark_stale_after_seconds: 600,
    latest_marked_at: null
  },
  warnings: { corrupted_row_count: 0, incomplete_closed_trade_count: 0 }
} satisfies DashboardOverview

let queryState: DashboardQueryState

vi.mock('../../features/dashboard/useDashboard', () => ({
  useDashboard: () => queryState
}))

function touch(
  type: 'touchStart' | 'touchMove' | 'touchEnd' | 'touchCancel',
  x: number,
  y: number
): void {
  const point = { identifier: 1, clientX: x, clientY: y }
  fireEvent[type](document, {
    touches: type === 'touchEnd' || type === 'touchCancel' ? [] : [point],
    changedTouches: [point],
    cancelable: true
  })
}

beforeEach(() => {
  refresh.mockClear()
  queryState = {
    data: overview,
    period: overview.period,
    isLoading: false,
    errorCode: null,
    lastUpdatedAt: new Date('2026-07-25T09:31:00+09:00'),
    setPeriod: vi.fn(),
    refresh
  }
  Object.defineProperty(window, 'scrollY', { configurable: true, value: 0 })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('모바일 당겨서 새로고침', () => {
  it('아래로 72px 이상 당긴 뒤 놓으면 정확히 한 번 갱신한다', () => {
    render(<App />)

    touch('touchStart', 100, 10)
    touch('touchMove', 102, 90)
    expect(screen.getByRole('status', { name: '놓아서 새로고침' })).toBeInTheDocument()
    touch('touchEnd', 102, 90)

    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('최상단 아래 방향 gesture를 확인하면 native refresh를 막는다', () => {
    render(<App />)

    touch('touchStart', 100, 10)
    let defaultPrevented = false
    document.addEventListener('touchmove', (event) => {
      defaultPrevented = event.defaultPrevented
    }, { once: true })
    fireEvent.touchMove(document, {
      touches: [{ identifier: 1, clientX: 100, clientY: 90 }],
      changedTouches: [{ identifier: 1, clientX: 100, clientY: 90 }],
      cancelable: true
    })

    expect(defaultPrevented).toBe(true)
  })

  it('문서가 최상단이 아니면 제스처를 시작하지 않는다', () => {
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 10 })
    render(<App />)

    touch('touchStart', 100, 10)
    touch('touchMove', 100, 100)
    touch('touchEnd', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
    expect(screen.queryByRole('status', { name: '놓아서 새로고침' })).not.toBeInTheDocument()
  })

  it.each([
    ['임계값 미만', 100, 10, 100, 70],
    ['가로 방향', 10, 10, 100, 85],
    ['위 방향', 100, 100, 100, 10]
  ])('%s 제스처는 갱신하지 않는다', (_name, startX, startY, endX, endY) => {
    render(<App />)

    touch('touchStart', startX as number, startY as number)
    touch('touchMove', endX as number, endY as number)
    touch('touchEnd', endX as number, endY as number)

    expect(refresh).not.toHaveBeenCalled()
  })

  it('이미 갱신 중이면 추가 제스처를 병합한다', () => {
    queryState = { ...queryState, isLoading: true }
    render(<App />)

    touch('touchStart', 100, 10)
    touch('touchMove', 100, 100)
    touch('touchEnd', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
  })

  it('데스크톱 마우스 드래그는 모바일 pull 동작으로 해석하지 않는다', () => {
    render(<App />)

    fireEvent.pointerDown(document, {
      pointerId: 2,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 10,
      button: 0
    })
    fireEvent.pointerMove(document, {
      pointerId: 2,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100
    })
    fireEvent.pointerUp(document, {
      pointerId: 2,
      pointerType: 'mouse',
      clientX: 100,
      clientY: 100
    })

    expect(refresh).not.toHaveBeenCalled()
  })

  it('두 손가락 gesture는 pull 동작으로 소유하지 않는다', () => {
    render(<App />)

    touch('touchStart', 100, 10)
    fireEvent.touchMove(document, {
      touches: [
        { identifier: 1, clientX: 100, clientY: 100 },
        { identifier: 2, clientX: 140, clientY: 100 }
      ],
      changedTouches: [{ identifier: 2, clientX: 140, clientY: 100 }],
      cancelable: true
    })
    touch('touchEnd', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
    expect(screen.queryByRole('status', { name: '놓아서 새로고침' })).not.toBeInTheDocument()
  })

  it('두 번째 손가락이 잠깐 추가되면 남은 첫 gesture도 끝까지 포기한다', () => {
    render(<App />)

    touch('touchStart', 100, 10)
    fireEvent.touchStart(document, {
      touches: [
        { identifier: 1, clientX: 100, clientY: 10 },
        { identifier: 2, clientX: 140, clientY: 10 }
      ],
      changedTouches: [{ identifier: 2, clientX: 140, clientY: 10 }]
    })
    fireEvent.touchEnd(document, {
      touches: [{ identifier: 1, clientX: 100, clientY: 10 }],
      changedTouches: [{ identifier: 2, clientX: 140, clientY: 10 }]
    })
    touch('touchMove', 100, 100)
    touch('touchEnd', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
  })

  it('touch cancel은 진행 상태를 지우고 갱신하지 않는다', () => {
    render(<App />)

    touch('touchStart', 100, 10)
    touch('touchMove', 100, 100)
    expect(screen.getByRole('status', { name: '놓아서 새로고침' })).toBeInTheDocument()
    touch('touchCancel', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
    expect(screen.queryByRole('status', { name: '놓아서 새로고침' })).not.toBeInTheDocument()
  })

  it('unmount 뒤 document listener가 남지 않는다', () => {
    const { unmount } = render(<App />)
    unmount()

    touch('touchStart', 100, 10)
    touch('touchMove', 100, 100)
    touch('touchEnd', 100, 100)

    expect(refresh).not.toHaveBeenCalled()
  })

  it('제스처 지원 여부와 무관하게 버튼 fallback을 항상 제공한다', () => {
    vi.stubGlobal('PointerEvent', undefined)

    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '대시보드 새로고침' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
