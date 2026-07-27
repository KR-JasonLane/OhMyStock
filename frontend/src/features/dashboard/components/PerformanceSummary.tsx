import type { DashboardOverview } from '../../../api/dashboard'
import {
  completenessLabel,
  costBasisLabel,
  formatPercent,
  formatUnsignedPercent,
  formatWon,
  pnlTone
} from '../format'

interface PerformanceSummaryProps {
  summary: DashboardOverview['summary']
}

interface PnlMetricProps {
  label: string
  value: number | null
  status: DashboardOverview['summary']['total_pnl_status']
  primary?: boolean
}

function PnlMetric({ label, value, status, primary = false }: PnlMetricProps): React.JSX.Element {
  const statusLabel = completenessLabel(status)
  return (
    <article
      className={`metric-card metric-card--${pnlTone(value)}${primary ? ' metric-card--primary' : ''}`}
      aria-label={label}
    >
      <div className="metric-card__topline">
        <span className="metric-card__label">{label}</span>
        {statusLabel !== null && <span className="metric-card__status">{statusLabel}</span>}
      </div>
      <strong className="metric-card__value" title={formatWon(value)}>{formatWon(value)}</strong>
      {primary && <span className="metric-card__caption">확정 + 사용 가능한 평가손익</span>}
    </article>
  )
}

export function PerformanceSummary({ summary }: PerformanceSummaryProps): React.JSX.Element {
  return (
    <section className="summary-section" aria-labelledby="summary-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">PERFORMANCE AT A GLANCE</p>
          <h2 id="summary-title">핵심 성과</h2>
        </div>
        <span className={`cost-basis cost-basis--${summary.cost_basis}`}>
          {costBasisLabel(summary.cost_basis)}
        </span>
      </div>

      <div className="summary-grid">
        <PnlMetric
          label="총손익"
          value={summary.total_pnl}
          status={summary.total_pnl_status}
          primary
        />
        <PnlMetric
          label="확정 손익"
          value={summary.realized_pnl}
          status={summary.realized_pnl_status}
        />
        <PnlMetric
          label="평가손익"
          value={summary.unrealized_pnl}
          status={summary.unrealized_pnl_status}
        />
        <article className="metric-card metric-card--compact" aria-label="확정 수익률">
          <span className="metric-card__label">확정 수익률</span>
          <strong
            className="metric-card__value"
            title={formatPercent(summary.realized_return_pct)}
          >
            {formatPercent(summary.realized_return_pct)}
          </strong>
          <span className="metric-card__caption">청산 진입금액 대비</span>
        </article>
        <article className="metric-card metric-card--compact" aria-label="완료 거래">
          <span className="metric-card__label">완료 거래</span>
          <strong className="metric-card__value" title={`${summary.closed_trade_count}건`}>
            {summary.closed_trade_count}건
          </strong>
          <span className="metric-card__caption">
            승 {summary.wins} · 패 {summary.losses} · 보합 {summary.draws}
          </span>
        </article>
        <article className="metric-card metric-card--compact" aria-label="승률">
          <span className="metric-card__label">승률</span>
          <strong
            className="metric-card__value"
            title={formatUnsignedPercent(summary.win_rate)}
          >
            {formatUnsignedPercent(summary.win_rate)}
          </strong>
          <span className="metric-card__caption">손익 확정 거래 기준</span>
        </article>
      </div>
    </section>
  )
}
