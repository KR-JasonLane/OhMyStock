import type { DashboardOverview } from '../../../api/dashboard'
import { formatChartDate, formatWon, pnlTone } from '../format'

interface ProfitChartProps {
  points: DashboardOverview['equity_curve']
}

interface ChartPoint {
  x: number
  y: number
  value: number
}

const WIDTH = 800
const HEIGHT = 250
const HORIZONTAL_PADDING = 28
const VERTICAL_PADDING = 26

function chartPoints(points: DashboardOverview['equity_curve']): ChartPoint[] {
  if (points.length === 0) return []
  const values = points.map((point) => point.cumulative_realized_pnl)
  const minimum = Math.min(0, ...values)
  const maximum = Math.max(0, ...values)
  const range = maximum - minimum || 1
  const usableWidth = WIDTH - HORIZONTAL_PADDING * 2
  const usableHeight = HEIGHT - VERTICAL_PADDING * 2
  return points.map((point, index) => ({
    x: HORIZONTAL_PADDING + (points.length === 1 ? usableWidth / 2 : (index / (points.length - 1)) * usableWidth),
    y: VERTICAL_PADDING + ((maximum - point.cumulative_realized_pnl) / range) * usableHeight,
    value: point.cumulative_realized_pnl
  }))
}

export function ProfitChart({ points }: ProfitChartProps): React.JSX.Element {
  const plotted = chartPoints(points)
  const values = points.map((point) => point.cumulative_realized_pnl)
  const latest = values.at(-1) ?? null
  const minimum = values.length === 0 ? null : Math.min(...values)
  const maximum = values.length === 0 ? null : Math.max(...values)
  const path = plotted.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point.y}`).join(' ')

  return (
    <section className="panel chart-panel" aria-labelledby="chart-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">REALIZED P&L CURVE</p>
          <h2 id="chart-title">누적 확정손익</h2>
        </div>
        {latest !== null && (
          <strong className={`chart-panel__latest value--${pnlTone(latest)}`}>{formatWon(latest)}</strong>
        )}
      </div>

      {points.length === 0 ? (
        <div className="empty-state empty-state--chart">
          <p>선택한 기간에 누적할 확정손익이 없습니다</p>
          <span>거래가 완료되면 추이가 이곳에 표시됩니다.</span>
        </div>
      ) : (
        <>
          <p className="sr-only">
            누적 확정손익 최신 {formatWon(latest)}, 최저 {formatWon(minimum)}, 최고 {formatWon(maximum)}
          </p>
          <div className="profit-chart" aria-hidden="true">
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="presentation" preserveAspectRatio="none">
              <line className="profit-chart__grid" x1="0" y1="62.5" x2={WIDTH} y2="62.5" />
              <line className="profit-chart__grid" x1="0" y1="125" x2={WIDTH} y2="125" />
              <line className="profit-chart__grid" x1="0" y1="187.5" x2={WIDTH} y2="187.5" />
              <path className="profit-chart__line" d={path} />
              {plotted.map((point, index) => (
                <circle
                  key={points[index].position_id}
                  className="profit-chart__point"
                  cx={point.x}
                  cy={point.y}
                  r={index === plotted.length - 1 ? 5 : 3}
                />
              ))}
            </svg>
            <div className="profit-chart__axis">
              <span>{formatChartDate(points[0].closed_at)}</span>
              <span>{formatChartDate(points.at(-1)?.closed_at ?? points[0].closed_at)}</span>
            </div>
          </div>
        </>
      )}
    </section>
  )
}

