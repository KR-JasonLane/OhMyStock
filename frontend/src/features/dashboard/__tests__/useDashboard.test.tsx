import { act, renderHook, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DashboardOverview } from '../../../api/dashboard'
import { useDashboard } from '../useDashboard'

const fixture: DashboardOverview = {
  period: { start: '2026-07-20', end: '2026-07-25', timezone: 'Asia/Seoul' },
  summary: {
    realized_pnl: 10_000,
    realized_pnl_status: 'complete',
    unrealized_pnl: 1_000,
    unrealized_pnl_status: 'complete',
    total_pnl: 11_000,
    total_pnl_status: 'complete',
    realized_return_pct: 5,
    closed_trade_count: 1,
    incomplete_closed_trade_count: 0,
    wins: 1,
    losses: 0,
    draws: 0,
    win_rate: 100,
    cost_basis: 'estimated'
  },
  equity_curve: [],
  positions: [],
  recent_trades: [],
  freshness: {
    as_of: '2026-07-25T09:00:00+09:00',
    mark_stale_after_seconds: 600,
    latest_marked_at: null
  },
  warnings: { corrupted_row_count: 0, incomplete_closed_trade_count: 0 }
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason: unknown) => void
} {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useDashboard', () => {
  it('mount 때 overview를 한 번만 조회한다', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(result.current.data).toEqual(fixture))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('StrictMode 개발 재실행에서도 mount 요청을 한 번만 전송한다', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useDashboard(fixture.period), {
      wrapper: ({ children }) => <StrictMode>{children}</StrictMode>
    })

    await waitFor(() => expect(result.current.data).toEqual(fixture))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('기간 변경은 이전 요청을 abort하고 새 기간을 조회한다', async () => {
    const first = deferred<Response>()
    const second = deferred<Response>()
    const fetchMock = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const firstSignal = fetchMock.mock.calls[0][1]?.signal as AbortSignal
    act(() => result.current.setPeriod({ start: '2026-07-21', end: '2026-07-25', timezone: 'Asia/Seoul' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(firstSignal.aborted).toBe(true)
    const changedPeriodFixture = {
      ...fixture,
      period: { start: '2026-07-21', end: '2026-07-25', timezone: 'Asia/Seoul' as const },
      summary: { ...fixture.summary, total_pnl: 99_000 }
    }
    second.resolve(new Response(JSON.stringify(changedPeriodFixture)))
    await waitFor(() => expect(result.current.data).toEqual(changedPeriodFixture))
    expect(result.current.isLoading).toBe(false)
    first.resolve(new Response(JSON.stringify(fixture)))
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.data).toEqual(changedPeriodFixture)
  })

  it('동시 refresh는 진행 중인 같은 Promise로 병합한다', async () => {
    const pending = deferred<Response>()
    const fetchMock = vi.fn().mockReturnValue(pending.promise)
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const firstRefresh = result.current.refresh()
    const secondRefresh = result.current.refresh()
    expect(firstRefresh).toBe(secondRefresh)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    pending.resolve(new Response(JSON.stringify(fixture)))
    await expect(firstRefresh).resolves.toBeUndefined()
  })

  it('refresh 실패 뒤에도 마지막 성공 data와 갱신시각을 보존한다', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture)))
      .mockRejectedValueOnce(new TypeError('network unavailable'))
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { result } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(result.current.data).toEqual(fixture))
    const lastUpdatedAt = result.current.lastUpdatedAt
    await act(async () => {
      await expect(result.current.refresh()).rejects.toThrow('dashboard_request_failed')
    })

    expect(result.current.data).toEqual(fixture)
    expect(result.current.lastUpdatedAt).toBe(lastUpdatedAt)
    expect(result.current.errorCode).toBe('dashboard_request_failed')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('손상된 성공 응답의 원문을 console에 남기지 않는다', async () => {
    const sentinel = 'account_no=SHOULD_NOT_APPEAR'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(`{${sentinel}`, { status: 200 })))
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const { result } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(result.current.errorCode).toBe('dashboard_request_failed'))
    expect(JSON.stringify(warnSpy.mock.calls)).not.toContain(sentinel)
  })

  it('unmount 때 진행 중 요청을 abort한다', async () => {
    const pending = deferred<Response>()
    const fetchMock = vi.fn().mockReturnValue(pending.promise)
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = renderHook(() => useDashboard(fixture.period))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const signal = fetchMock.mock.calls[0][1]?.signal as AbortSignal
    unmount()

    expect(signal.aborted).toBe(true)
  })

  it('자동 polling이나 WebSocket 없이 명시 요청만 사용한다', async () => {
    const intervalSpy = vi.spyOn(globalThis, 'setInterval')
    const webSocket = vi.fn()
    vi.stubGlobal('WebSocket', webSocket)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture))))

    const { result } = renderHook(() => useDashboard(fixture.period))

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.data).toEqual(fixture)
    expect(intervalSpy).not.toHaveBeenCalled()
    expect(webSocket).not.toHaveBeenCalled()
  })
})
