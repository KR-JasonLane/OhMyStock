export type DashboardErrorCode =
  | 'dashboard_invalid_response'
  | 'dashboard_request_failed'
  | 'dashboard_unavailable'

export interface DateRange {
  start: string
  end: string
  timezone: 'Asia/Seoul'
}

export type Completeness = 'complete' | 'partial' | 'unavailable'
export type CostBasis = 'recorded' | 'estimated' | 'unavailable'
export type RunEnvironment = 'mock' | 'real' | 'replay'

export interface DashboardOverview {
  environment: RunEnvironment
  period: DateRange
  summary: {
    realized_pnl: number | null
    realized_pnl_status: Completeness
    unrealized_pnl: number | null
    unrealized_pnl_status: Completeness
    total_pnl: number | null
    total_pnl_status: Completeness
    realized_return_pct: number | null
    closed_trade_count: number
    incomplete_closed_trade_count: number
    wins: number
    losses: number
    draws: number
    win_rate: number | null
    cost_basis: CostBasis
  }
  equity_curve: Array<{
    position_id: number
    closed_at: string
    realized_pnl: number
    cumulative_realized_pnl: number
  }>
  positions: Array<{
    position_id: number
    symbol: string
    name: string
    entry_price: number
    quantity: number
    entered_at: string | null
    mark_price: number | null
    marked_at: string | null
    unrealized_pnl: number | null
    valuation_status: Completeness
  }>
  recent_trades: Array<{
    position_id: number
    symbol: string
    name: string
    entry_price: number
    quantity: number
    exit_price: number | null
    realized_pnl: number | null
    closed_at: string
    exit_reason: string | null
  }>
  freshness: {
    as_of: string
    mark_stale_after_seconds: number
    latest_marked_at: string | null
  }
  warnings: {
    corrupted_row_count: number
    incomplete_closed_trade_count: number
  }
}

export class DashboardRequestError extends Error {
  constructor(readonly code: DashboardErrorCode, options?: ErrorOptions) {
    super(code, options)
    this.name = 'DashboardRequestError'
  }
}

const completenessValues = ['complete', 'partial', 'unavailable'] as const
const costBasisValues = ['recorded', 'estimated', 'unavailable'] as const
const runEnvironmentValues = ['mock', 'real', 'replay'] as const
const isoDatePattern = /^\d{4}-\d{2}-\d{2}$/
const awareDateTimePattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

function invalidResponse(): never {
  throw new DashboardRequestError('dashboard_invalid_response')
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) invalidResponse()
  return value as Record<string, unknown>
}

function finiteNumber(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) invalidResponse()
  return value
}

function integer(value: unknown): number {
  const parsed = finiteNumber(value)
  if (!Number.isInteger(parsed)) invalidResponse()
  return parsed
}

function nullableNumber(value: unknown): number | null {
  return value === null ? null : finiteNumber(value)
}

function string(value: unknown): string {
  if (typeof value !== 'string') invalidResponse()
  return value
}

function nullableString(value: unknown): string | null {
  return value === null ? null : string(value)
}

function dateOnly(value: unknown): string {
  const parsed = string(value)
  if (!isoDatePattern.test(parsed)) invalidResponse()
  const [year, month, day] = parsed.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day))
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) invalidResponse()
  return parsed
}

function awareDateTime(value: unknown): string {
  const parsed = string(value)
  const match = awareDateTimePattern.exec(parsed)
  if (match === null || !Number.isFinite(Date.parse(parsed))) invalidResponse()
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number)
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second))
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day ||
    date.getUTCHours() !== hour ||
    date.getUTCMinutes() !== minute ||
    date.getUTCSeconds() !== second
  ) invalidResponse()
  return parsed
}

function nullableAwareDateTime(value: unknown): string | null {
  return value === null ? null : awareDateTime(value)
}

function enumValue<T extends readonly string[]>(value: unknown, allowed: T): T[number] {
  const parsed = string(value)
  if (!allowed.includes(parsed)) invalidResponse()
  return parsed as T[number]
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) invalidResponse()
  return value
}

function parsePeriod(value: unknown): DateRange {
  const parsed = record(value)
  const period = {
    start: dateOnly(parsed.start),
    end: dateOnly(parsed.end),
    timezone: enumValue(parsed.timezone, ['Asia/Seoul'] as const)
  }
  if (period.start > period.end) invalidResponse()
  return period
}

function parseSummary(value: unknown): DashboardOverview['summary'] {
  const parsed = record(value)
  return {
    realized_pnl: nullableNumber(parsed.realized_pnl),
    realized_pnl_status: enumValue(parsed.realized_pnl_status, completenessValues),
    unrealized_pnl: nullableNumber(parsed.unrealized_pnl),
    unrealized_pnl_status: enumValue(parsed.unrealized_pnl_status, completenessValues),
    total_pnl: nullableNumber(parsed.total_pnl),
    total_pnl_status: enumValue(parsed.total_pnl_status, completenessValues),
    realized_return_pct: nullableNumber(parsed.realized_return_pct),
    closed_trade_count: integer(parsed.closed_trade_count),
    incomplete_closed_trade_count: integer(parsed.incomplete_closed_trade_count),
    wins: integer(parsed.wins),
    losses: integer(parsed.losses),
    draws: integer(parsed.draws),
    win_rate: nullableNumber(parsed.win_rate),
    cost_basis: enumValue(parsed.cost_basis, costBasisValues)
  }
}

export function parseDashboardOverview(value: unknown): DashboardOverview {
  const parsed = record(value)
  const recentTrades = array(parsed.recent_trades)
  if (recentTrades.length > 100) invalidResponse()

  const overview: DashboardOverview = {
    environment: enumValue(parsed.environment, runEnvironmentValues),
    period: parsePeriod(parsed.period),
    summary: parseSummary(parsed.summary),
    equity_curve: array(parsed.equity_curve).map((point) => {
      const parsedPoint = record(point)
      return {
        position_id: integer(parsedPoint.position_id),
        closed_at: awareDateTime(parsedPoint.closed_at),
        realized_pnl: finiteNumber(parsedPoint.realized_pnl),
        cumulative_realized_pnl: finiteNumber(parsedPoint.cumulative_realized_pnl)
      }
    }),
    positions: array(parsed.positions).map((position) => {
      const parsedPosition = record(position)
      return {
        position_id: integer(parsedPosition.position_id),
        symbol: string(parsedPosition.symbol),
        name: string(parsedPosition.name),
        entry_price: finiteNumber(parsedPosition.entry_price),
        quantity: integer(parsedPosition.quantity),
        entered_at: nullableAwareDateTime(parsedPosition.entered_at),
        mark_price: nullableNumber(parsedPosition.mark_price),
        marked_at: nullableAwareDateTime(parsedPosition.marked_at),
        unrealized_pnl: nullableNumber(parsedPosition.unrealized_pnl),
        valuation_status: enumValue(parsedPosition.valuation_status, completenessValues)
      }
    }),
    recent_trades: recentTrades.map((trade) => {
      const parsedTrade = record(trade)
      return {
        position_id: integer(parsedTrade.position_id),
        symbol: string(parsedTrade.symbol),
        name: string(parsedTrade.name),
        entry_price: finiteNumber(parsedTrade.entry_price),
        quantity: integer(parsedTrade.quantity),
        exit_price: nullableNumber(parsedTrade.exit_price),
        realized_pnl: nullableNumber(parsedTrade.realized_pnl),
        closed_at: awareDateTime(parsedTrade.closed_at),
        exit_reason: nullableString(parsedTrade.exit_reason)
      }
    }),
    freshness: (() => {
      const freshness = record(parsed.freshness)
      return {
        as_of: awareDateTime(freshness.as_of),
        mark_stale_after_seconds: integer(freshness.mark_stale_after_seconds),
        latest_marked_at: nullableAwareDateTime(freshness.latest_marked_at)
      }
    })(),
    warnings: (() => {
      const warnings = record(parsed.warnings)
      return {
        corrupted_row_count: integer(warnings.corrupted_row_count),
        incomplete_closed_trade_count: integer(warnings.incomplete_closed_trade_count)
      }
    })()
  }
  return validateOverviewTiming(overview)
}

function dateTimeMillis(value: string): number {
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) invalidResponse()
  return parsed
}

function kstDate(value: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(new Date(dateTimeMillis(value)))
  const part = (name: Intl.DateTimeFormatPartTypes): string =>
    parts.find((candidate) => candidate.type === name)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

function validateOverviewTiming(overview: DashboardOverview): DashboardOverview {
  const asOf = dateTimeMillis(overview.freshness.as_of)
  const staleAfterMilliseconds = overview.freshness.mark_stale_after_seconds * 1_000
  if (staleAfterMilliseconds < 0) invalidResponse()
  const isCurrentOrEarlier = (value: string): boolean => dateTimeMillis(value) <= asOf
  const isInPeriod = (value: string): boolean => {
    const localDate = kstDate(value)
    return overview.period.start <= localDate && localDate <= overview.period.end
  }

  if (
    (overview.freshness.latest_marked_at !== null && !isCurrentOrEarlier(overview.freshness.latest_marked_at)) ||
    overview.equity_curve.some((point) => !isCurrentOrEarlier(point.closed_at) || !isInPeriod(point.closed_at)) ||
    overview.recent_trades.some((trade) => !isCurrentOrEarlier(trade.closed_at) || !isInPeriod(trade.closed_at)) ||
    overview.positions.some((position) => {
      if (position.entered_at !== null && !isCurrentOrEarlier(position.entered_at)) return true
      if (position.marked_at !== null && !isCurrentOrEarlier(position.marked_at)) return true
      return position.valuation_status === 'complete' && (
        position.marked_at === null ||
        position.unrealized_pnl === null ||
        asOf - dateTimeMillis(position.marked_at) > staleAfterMilliseconds
      )
    })
  ) invalidResponse()

  return overview
}

export async function fetchDashboardOverview(
  period: DateRange,
  signal: AbortSignal
): Promise<DashboardOverview> {
  const query = new URLSearchParams({ from: period.start, to: period.end, timezone: period.timezone })
  try {
    const response = await fetch(`/api/dashboard/overview?${query.toString()}`, { signal })
    if (!response.ok) {
      throw new DashboardRequestError(
        response.status === 503 ? 'dashboard_unavailable' : 'dashboard_request_failed'
      )
    }
    const overview = parseDashboardOverview(await response.json())
    if (
      overview.period.start !== period.start ||
      overview.period.end !== period.end ||
      overview.period.timezone !== period.timezone
    ) invalidResponse()
    return overview
  } catch (error) {
    if (signal.aborted || error instanceof DashboardRequestError) throw error
    throw new DashboardRequestError('dashboard_request_failed', { cause: error })
  }
}
