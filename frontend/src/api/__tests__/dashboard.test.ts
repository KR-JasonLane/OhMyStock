import { describe, expect, it, vi } from 'vitest'

import {
  DashboardRequestError,
  fetchDashboardOverview,
  parseDashboardOverview,
  type DashboardOverview
} from '../dashboard'

const overviewFixture: DashboardOverview = {
  environment: 'mock',
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
  equity_curve: [
    {
      position_id: 1,
      closed_at: '2026-07-25T09:00:00+09:00',
      realized_pnl: 10_000,
      cumulative_realized_pnl: 10_000
    }
  ],
  positions: [
    {
      position_id: 2,
      symbol: '005930',
      name: '삼성전자',
      entry_price: 70_000,
      quantity: 1,
      entered_at: '2026-07-24T09:00:00+09:00',
      mark_price: 71_000,
      marked_at: '2026-07-25T09:00:00+09:00',
      unrealized_pnl: 1_000,
      valuation_status: 'complete'
    }
  ],
  recent_trades: [
    {
      position_id: 1,
      symbol: '005930',
      name: '삼성전자',
      entry_price: 70_000,
      quantity: 1,
      exit_price: 80_000,
      realized_pnl: 10_000,
      closed_at: '2026-07-25T09:00:00+09:00',
      exit_reason: 'take_profit'
    }
  ],
  freshness: {
    as_of: '2026-07-25T09:00:00+09:00',
    mark_stale_after_seconds: 600,
    latest_marked_at: '2026-07-25T09:00:00+09:00'
  },
  warnings: { corrupted_row_count: 0, incomplete_closed_trade_count: 0 }
}

describe('parseDashboardOverview', () => {
  it('완전한 API 응답을 화면용 DTO로 보존한다', () => {
    expect(parseDashboardOverview(overviewFixture)).toEqual(overviewFixture)
  })

  it('필수 영역이 없으면 안정된 invalid-response 오류로 거부한다', () => {
    const missingFreshness = { ...overviewFixture, freshness: undefined }

    expect(() => parseDashboardOverview(missingFreshness)).toThrow(DashboardRequestError)
    expect(() => parseDashboardOverview(missingFreshness)).toThrow('dashboard_invalid_response')
  })

  it.each(['paper', 'production', '', null])(
    '알 수 없는 실행환경 %s는 fail-closed로 거부한다',
    (environment) => {
      expect(() => parseDashboardOverview({ ...overviewFixture, environment }))
        .toThrow('dashboard_invalid_response')
    }
  )

  it.each([Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    '유한하지 않은 숫자 %s를 거부한다',
    (value) => {
      const invalidNumber = {
        ...overviewFixture,
        summary: { ...overviewFixture.summary, realized_return_pct: value }
      }

      expect(() => parseDashboardOverview(invalidNumber)).toThrow('dashboard_invalid_response')
    }
  )

  it('offset 없는 날짜를 거부한다', () => {
    const invalidDate = {
      ...overviewFixture,
      freshness: { ...overviewFixture.freshness, as_of: '2026-07-25T09:00:00' }
    }

    expect(() => parseDashboardOverview(invalidDate)).toThrow('dashboard_invalid_response')
  })

  it('알 수 없는 상태 enum을 거부한다', () => {
    const invalidStatus = {
      ...overviewFixture,
      summary: { ...overviewFixture.summary, total_pnl_status: 'best_effort' }
    }

    expect(() => parseDashboardOverview(invalidStatus)).toThrow('dashboard_invalid_response')
  })

  it('기준시각 이후 청산이나 stale complete mark를 거부한다', () => {
    const futureClosedAt = {
      ...overviewFixture,
      recent_trades: [
        { ...overviewFixture.recent_trades[0], closed_at: '2026-07-25T09:00:01+09:00' }
      ]
    }
    const staleCompleteMark = {
      ...overviewFixture,
      positions: [
        { ...overviewFixture.positions[0], marked_at: '2026-07-25T08:49:59+09:00' }
      ]
    }

    expect(() => parseDashboardOverview(futureClosedAt)).toThrow('dashboard_invalid_response')
    expect(() => parseDashboardOverview(staleCompleteMark)).toThrow('dashboard_invalid_response')
  })
})

describe('fetchDashboardOverview', () => {
  it('상대 프록시 URL로 조회하고 응답을 parser 경계에서 검증한다', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(overviewFixture), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(
      fetchDashboardOverview(
        { start: '2026-07-20', end: '2026-07-25', timezone: 'Asia/Seoul' },
        controller.signal
      )
    ).resolves.toEqual(overviewFixture)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/dashboard/overview?from=2026-07-20&to=2026-07-25&timezone=Asia%2FSeoul',
      expect.objectContaining({ signal: controller.signal })
    )
  })

  it('요청과 다른 기간의 성공 응답을 거부한다', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(overviewFixture))))

    await expect(
      fetchDashboardOverview(
        { start: '2026-07-21', end: '2026-07-25', timezone: 'Asia/Seoul' },
        new AbortController().signal
      )
    ).rejects.toMatchObject({ code: 'dashboard_invalid_response' })
  })
})
