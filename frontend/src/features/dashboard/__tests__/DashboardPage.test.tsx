import '@testing-library/jest-dom/vitest'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../../../App'
import type { DashboardOverview } from '../../../api/dashboard'
import type { DashboardQueryState } from '../useDashboard'

const refresh = vi.fn<() => Promise<void>>().mockResolvedValue(undefined)
const setPeriod = vi.fn<DashboardQueryState['setPeriod']>()

const overview: DashboardOverview = {
  environment: 'mock',
  period: { start: '2026-06-26', end: '2026-07-25', timezone: 'Asia/Seoul' },
  summary: {
    realized_pnl: 1_245_000,
    realized_pnl_status: 'complete',
    unrealized_pnl: -84_500,
    unrealized_pnl_status: 'partial',
    total_pnl: 1_160_500,
    total_pnl_status: 'partial',
    realized_return_pct: 4.82,
    closed_trade_count: 12,
    incomplete_closed_trade_count: 1,
    wins: 7,
    losses: 4,
    draws: 1,
    win_rate: 63.64,
    cost_basis: 'estimated'
  },
  equity_curve: [
    {
      position_id: 1,
      closed_at: '2026-07-22T10:30:00+09:00',
      realized_pnl: -100_000,
      cumulative_realized_pnl: -100_000
    },
    {
      position_id: 2,
      closed_at: '2026-07-23T14:40:00+09:00',
      realized_pnl: 1_345_000,
      cumulative_realized_pnl: 1_245_000
    }
  ],
  positions: [
    {
      position_id: 31,
      symbol: '005930',
      name: '삼성전자',
      entry_price: 70_100,
      quantity: 12,
      entered_at: '2026-07-25T09:12:00+09:00',
      mark_price: 72_000,
      marked_at: '2026-07-25T09:28:00+09:00',
      unrealized_pnl: 22_800,
      valuation_status: 'complete'
    },
    {
      position_id: 32,
      symbol: '000660',
      name: 'SK하이닉스',
      entry_price: 190_000,
      quantity: 3,
      entered_at: '2026-07-25T09:20:00+09:00',
      mark_price: 185_000,
      marked_at: '2026-07-25T08:30:00+09:00',
      unrealized_pnl: null,
      valuation_status: 'unavailable'
    }
  ],
  recent_trades: [
    {
      position_id: 21,
      symbol: '035420',
      name: 'NAVER',
      entry_price: 201_000,
      quantity: 5,
      exit_price: 208_000,
      realized_pnl: 35_000,
      closed_at: '2026-07-23T14:40:00+09:00',
      exit_reason: 'take_profit'
    }
  ],
  freshness: {
    as_of: '2026-07-25T09:30:00+09:00',
    mark_stale_after_seconds: 600,
    latest_marked_at: '2026-07-25T09:28:00+09:00'
  },
  warnings: { corrupted_row_count: 2, incomplete_closed_trade_count: 1 }
}

let queryState: DashboardQueryState

vi.mock('../useDashboard', () => ({
  useDashboard: () => queryState
}))

function state(overrides: Partial<DashboardQueryState> = {}): DashboardQueryState {
  return {
    data: overview,
    period: overview.period,
    isLoading: false,
    errorCode: null,
    lastUpdatedAt: new Date('2026-07-25T09:31:00+09:00'),
    setPeriod,
    refresh,
    ...overrides
  }
}

beforeEach(() => {
  queryState = state()
  refresh.mockClear()
  setPeriod.mockClear()
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function installColorScheme(dark: boolean): {
  setDark: (matches: boolean) => void
} {
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const mediaQuery = {
    matches: dark,
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn()
  }
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue(mediaQuery))
  return {
    setDark(matches) {
      mediaQuery.matches = matches
      listeners.forEach((listener) => listener({ matches } as MediaQueryListEvent))
    }
  }
}

describe('DashboardPage 화면 의미', () => {
  it('모의 환경과 KST 마지막 갱신시각, 총손익 중심 성과를 구분해 표시한다', () => {
    render(<App />)

    expect(screen.getByText('모의투자')).toBeInTheDocument()
    expect(screen.getByText(/마지막 갱신.*2026\. 7\. 25\..*(?:오전|AM) 9:31/)).toBeInTheDocument()
    const total = screen.getByLabelText('총손익')
    expect(total).toHaveTextContent('+₩1,160,500')
    expect(total).toHaveTextContent('일부만 확인')
    expect(screen.getByLabelText('확정 손익')).toHaveTextContent('+₩1,245,000')
    expect(screen.getByLabelText('평가손익')).toHaveTextContent('-₩84,500')
    expect(screen.getByText('비용 추정 반영')).toBeInTheDocument()
    expect(screen.getByLabelText('승률')).toHaveTextContent('63.64%')
    expect(screen.getByLabelText('승률')).not.toHaveTextContent('+63.64%')
  })

  it.each([
    ['mock', '모의투자'],
    ['real', '실거래'],
    ['replay', '리플레이']
  ] as const)('%s transport 환경을 헤더에 숨기지 않고 표시한다', (environment, label) => {
    queryState = state({ data: { ...overview, environment } })

    render(<App />)

    expect(screen.getByText(label)).toBeVisible()
    if (environment === 'real') {
      expect(screen.getByLabelText('실거래 환경: 실제 주문 가능')).toBeInTheDocument()
    }
  })

  it.each([
    ['갱신 중', { isLoading: true }],
    ['갱신 실패', { errorCode: 'dashboard_request_failed' as const }]
  ])('%s에는 마지막 성공 환경을 현재 환경처럼 표시하지 않는다', (_name, overrides) => {
    queryState = state(overrides)

    render(<App />)

    expect(screen.queryByText('모의투자')).not.toBeInTheDocument()
    expect(screen.getByText(
      'isLoading' in overrides && overrides.isLoading ? '환경 확인 중' : '환경 확인 불가'
    )).toBeVisible()
  })

  it('비용·손익 확인 불가를 0원으로 꾸미지 않는다', () => {
    queryState = state({
      data: {
        ...overview,
        summary: {
          ...overview.summary,
          realized_pnl: null,
          realized_pnl_status: 'unavailable',
          unrealized_pnl: null,
          unrealized_pnl_status: 'unavailable',
          total_pnl: null,
          total_pnl_status: 'unavailable',
          cost_basis: 'unavailable'
        }
      }
    })

    render(<App />)

    expect(screen.getByLabelText('총손익')).toHaveTextContent('확인 불가')
    expect(screen.getByLabelText('총손익')).not.toHaveTextContent('₩0')
    expect(screen.getByText('비용 확인 불가')).toBeInTheDocument()
  })

  it('stale 시세와 손상·미완전 데이터 경고를 숨기지 않는다', () => {
    render(<App />)

    const warnings = screen.getByRole('status', { name: '데이터 경고' })
    expect(warnings).toHaveTextContent('오래된 저장 시세 1종목')
    expect(warnings).toHaveTextContent('손상된 행 2건 제외')
    expect(warnings).toHaveTextContent('손익 미완전 거래 1건')
  })

  it('열린 포지션과 최근 거래가 없으면 오류가 아닌 정상 빈 상태를 표시한다', () => {
    queryState = state({
      data: { ...overview, positions: [], recent_trades: [] }
    })

    render(<App />)

    expect(screen.getByText('현재 관리 중인 포지션이 없습니다')).toBeInTheDocument()
    expect(screen.getByText('선택한 기간에 완료된 관리 거래가 없습니다')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('갱신 실패 뒤 기존 KPI를 유지하면서 비차단 오류 배너를 표시한다', () => {
    queryState = state({ errorCode: 'dashboard_request_failed' })

    render(<App />)

    expect(screen.getByLabelText('총손익')).toHaveTextContent('+₩1,160,500')
    expect(screen.getByRole('alert')).toHaveTextContent('최신 정보를 가져오지 못했습니다')
  })

  it('초기 실패는 재시도 가능한 전체 오류 상태이며 데이터가 있을 때와 구분한다', () => {
    queryState = state({
      data: null,
      isLoading: false,
      errorCode: 'dashboard_unavailable',
      lastUpdatedAt: null
    })

    render(<App />)

    expect(screen.getByRole('heading', { name: '대시보드를 불러올 수 없습니다' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('최초 일반 요청 실패는 존재하지 않는 마지막 성공 데이터를 언급하지 않는다', () => {
    queryState = state({
      data: null,
      isLoading: false,
      errorCode: 'dashboard_request_failed',
      lastUpdatedAt: null
    })

    render(<App />)

    expect(screen.getByText('조회 실패')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '대시보드를 불러올 수 없습니다' })
      .parentElement).not.toHaveTextContent('마지막 성공 데이터')
  })

  it('초기 로딩은 실제 영역 구조를 보존한 Skeleton을 제공한다', () => {
    queryState = state({ data: null, isLoading: true, lastUpdatedAt: null })

    render(<App />)

    expect(screen.getByRole('status', { name: '대시보드 불러오는 중' })).toBeInTheDocument()
    expect(screen.getAllByTestId('summary-skeleton')).toHaveLength(6)
    expect(screen.getByTestId('chart-skeleton')).toBeInTheDocument()
    expect(screen.getByTestId('positions-skeleton')).toBeInTheDocument()
    expect(screen.getByTestId('trades-skeleton')).toBeInTheDocument()
  })

  it('버튼 갱신은 query refresh를 정확히 한 번 호출하며 항상 접근 가능하다', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: '대시보드 새로고침' }))

    expect(refresh).toHaveBeenCalledTimes(1)
  })
})

describe('기간 선택과 정보 보존', () => {
  it('최근 7일·최근 30일·이번 달 preset과 사용자 날짜 범위를 제공한다', () => {
    render(<App />)

    expect(screen.getByRole('button', { name: '최근 30일' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: '최근 7일' }))
    expect(setPeriod).toHaveBeenLastCalledWith({
      start: '2026-07-19',
      end: '2026-07-25',
      timezone: 'Asia/Seoul'
    })
    fireEvent.click(screen.getByRole('button', { name: '이번 달' }))
    expect(setPeriod).toHaveBeenLastCalledWith({
      start: '2026-07-01',
      end: '2026-07-25',
      timezone: 'Asia/Seoul'
    })

    fireEvent.change(screen.getByLabelText('조회 시작일'), { target: { value: '2026-07-03' } })
    fireEvent.change(screen.getByLabelText('조회 종료일'), { target: { value: '2026-07-18' } })
    expect(setPeriod).toHaveBeenLastCalledWith({
      start: '2026-07-03',
      end: '2026-07-18',
      timezone: 'Asia/Seoul'
    })
  })

  it('새 기간 조회 중에는 KPI가 어느 성공 기간의 값인지 명시한다', () => {
    queryState = state({
      period: { start: '2026-07-19', end: '2026-07-25', timezone: 'Asia/Seoul' },
      isLoading: true
    })

    render(<App />)

    expect(screen.getByRole('status', { name: '표시 데이터 기간' }))
      .toHaveTextContent('현재 표시 기간 2026-06-26 ~ 2026-07-25')
  })

  it('새 기간 조회 실패 후에도 KPI의 성공 기간을 명시한다', () => {
    queryState = state({
      period: { start: '2026-07-19', end: '2026-07-25', timezone: 'Asia/Seoul' },
      errorCode: 'dashboard_request_failed'
    })

    render(<App />)

    expect(screen.getByRole('status', { name: '표시 데이터 기간' }))
      .toHaveTextContent('조회 요청 기간과 다릅니다')
  })

  it('desktop 표와 mobile 카드가 포지션의 종목·수량·손익·시각 의미를 모두 보존한다', () => {
    render(<App />)

    for (const view of ['positions-desktop', 'positions-mobile']) {
      const region = within(screen.getByTestId(view))
      expect(region.getAllByText('평가손익').length).toBeGreaterThan(0)
      expect(region.getByText('005930')).toBeInTheDocument()
      expect(region.getByText('12주')).toBeInTheDocument()
      expect(region.getByText('+₩22,800')).toBeInTheDocument()
      expect(region.getByText(/2026\. 7\. 25\..*(?:오전|AM) 9:12/)).toBeInTheDocument()
      expect(region.getByText(/2026\. 7\. 25\..*(?:오전|AM) 9:28/)).toBeInTheDocument()
      expect(region.getByText('SK하이닉스')).toBeInTheDocument()
      expect(region.getByText('확인 불가')).toBeInTheDocument()
      expect(region.getByText(/저장 시세.*오래됨.*₩185,000/)).toBeInTheDocument()
      expect(region.getByText(/2026\. 7\. 25\..*(?:오전|AM) 9:20/)).toBeInTheDocument()
      expect(region.getByText(/2026\. 7\. 25\..*(?:오전|AM) 8:30/)).toBeInTheDocument()
    }
  })

  it('desktop 표와 mobile 카드가 최근 거래의 종목·수량·손익·시각 의미를 모두 보존한다', () => {
    render(<App />)

    for (const view of ['trades-desktop', 'trades-mobile']) {
      const region = within(screen.getByTestId(view))
      expect(region.getAllByText('확정손익').length).toBeGreaterThan(0)
      expect(region.getByText('035420')).toBeInTheDocument()
      expect(region.getByText('5주')).toBeInTheDocument()
      expect(region.getByText('+₩35,000')).toBeInTheDocument()
      expect(region.getByText(/2026\. 7\. 23\..*(?:오후|PM) 2:40/)).toBeInTheDocument()
      expect(region.getByText('목표 수익 도달')).toBeInTheDocument()
    }
  })

  it('최대 보유기간 청산 사유를 기타로 축약하지 않는다', () => {
    queryState = state({
      data: {
        ...overview,
        recent_trades: [{ ...overview.recent_trades[0], exit_reason: 'max_holding' }]
      }
    })

    render(<App />)

    for (const view of ['trades-desktop', 'trades-mobile']) {
      expect(within(screen.getByTestId(view)).getByText('최대 보유기간 도달')).toBeInTheDocument()
    }
  })

  it('차트의 최신·최저·최고 누적손익을 스크린리더 텍스트로 제공한다', () => {
    render(<App />)

    expect(screen.getByText('누적 확정손익 최신 +₩1,245,000, 최저 -₩100,000, 최고 +₩1,245,000')).toHaveClass(
      'sr-only'
    )
  })

  it('가격은 방향성 손익과 달리 양수 부호 없이 표시한다', () => {
    render(<App />)

    const positions = within(screen.getByTestId('positions-desktop'))
    expect(positions.getByText('₩70,100')).toBeInTheDocument()
    expect(positions.queryByText('+₩70,100')).not.toBeInTheDocument()
  })
})

describe('라이트·다크 테마', () => {
  it('저장값이 없으면 시스템 색상 설정을 초기 테마로 사용한다', () => {
    installColorScheme(true)

    render(<App />)

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(screen.getByLabelText('테마 선택')).toHaveValue('system')
  })

  it('수동 테마를 ohmystock.theme 하나에 저장하고 다음 진입에 복원한다', () => {
    installColorScheme(true)
    const { unmount } = render(<App />)

    fireEvent.change(screen.getByLabelText('테마 선택'), { target: { value: 'light' } })
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    expect(localStorage.getItem('ohmystock.theme')).toBe('light')
    expect(localStorage).toHaveLength(1)
    unmount()

    render(<App />)
    expect(screen.getByLabelText('테마 선택')).toHaveValue('light')
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('system으로 돌아오면 이후 prefers-color-scheme 변경을 반영한다', () => {
    const scheme = installColorScheme(false)
    localStorage.setItem('ohmystock.theme', 'dark')
    render(<App />)

    fireEvent.change(screen.getByLabelText('테마 선택'), { target: { value: 'system' } })
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    act(() => scheme.setDark(true))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(localStorage.getItem('ohmystock.theme')).toBe('system')
  })
})
