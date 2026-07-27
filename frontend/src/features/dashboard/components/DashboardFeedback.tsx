import { Button } from 'primereact/button'
import { Skeleton } from 'primereact/skeleton'

import type { DashboardErrorCode, DashboardOverview } from '../../../api/dashboard'

const refreshErrorMessages: Record<DashboardErrorCode, string> = {
  dashboard_invalid_response: '응답 데이터가 올바르지 않아 최신 정보를 반영하지 못했습니다.',
  dashboard_request_failed: '최신 정보를 가져오지 못했습니다. 마지막 성공 데이터를 계속 표시합니다.',
  dashboard_unavailable: '대시보드 조회 서비스가 현재 준비되지 않았습니다.'
}

const initialErrorMessages: Record<DashboardErrorCode, string> = {
  dashboard_invalid_response: '응답 데이터가 올바르지 않습니다. 잠시 후 다시 시도해 주세요.',
  dashboard_request_failed: '대시보드 정보를 가져오지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.',
  dashboard_unavailable: '대시보드 조회 서비스가 현재 준비되지 않았습니다.'
}

export function RefreshErrorBanner({ code }: { code: DashboardErrorCode }): React.JSX.Element {
  return (
    <div className="feedback-banner feedback-banner--error" role="alert">
      <strong>갱신 실패</strong>
      <span>{refreshErrorMessages[code]}</span>
    </div>
  )
}

interface InitialErrorProps {
  code: DashboardErrorCode
  onRetry: () => void
}

export function InitialError({ code, onRetry }: InitialErrorProps): React.JSX.Element {
  return (
    <section className="initial-feedback" aria-labelledby="initial-error-title">
      <p className="section-kicker">READ MODEL UNAVAILABLE</p>
      <h2 id="initial-error-title">대시보드를 불러올 수 없습니다</h2>
      <p>{initialErrorMessages[code]}</p>
      <Button type="button" className="retry-button" onClick={onRetry}>다시 시도</Button>
    </section>
  )
}

export function DataWarnings({ data }: { data: DashboardOverview }): React.JSX.Element | null {
  const warnings: string[] = []
  const stalePositionCount = data.positions.filter((position) => (
    position.marked_at !== null &&
    Date.parse(data.freshness.as_of) - Date.parse(position.marked_at) >
      data.freshness.mark_stale_after_seconds * 1_000
  )).length
  const latestMarkedAt = data.freshness.latest_marked_at
  if (stalePositionCount > 0) {
    warnings.push(`오래된 저장 시세 ${stalePositionCount}종목`)
  } else if (
    latestMarkedAt !== null &&
    Date.parse(data.freshness.as_of) - Date.parse(latestMarkedAt) >
      data.freshness.mark_stale_after_seconds * 1_000
  ) {
    warnings.push('저장 시세가 오래되었습니다')
  }
  if (data.warnings.corrupted_row_count > 0) {
    warnings.push(`손상된 행 ${data.warnings.corrupted_row_count}건 제외`)
  }
  if (data.warnings.incomplete_closed_trade_count > 0) {
    warnings.push(`손익 미완전 거래 ${data.warnings.incomplete_closed_trade_count}건`)
  }
  if (warnings.length === 0) return null

  return (
    <div className="feedback-banner feedback-banner--warning" role="status" aria-label="데이터 경고">
      <strong>데이터 주의</strong>
      <ul>
        {warnings.map((warning) => <li key={warning}>{warning}</li>)}
      </ul>
    </div>
  )
}

interface DisplayedPeriodNoticeProps {
  requested: DashboardOverview['period']
  displayed: DashboardOverview['period']
  isLoading: boolean
}

export function DisplayedPeriodNotice({
  requested,
  displayed,
  isLoading
}: DisplayedPeriodNoticeProps): React.JSX.Element | null {
  if (
    requested.start === displayed.start &&
    requested.end === displayed.end &&
    requested.timezone === displayed.timezone
  ) return null

  return (
    <div
      className="feedback-banner feedback-banner--info"
      role="status"
      aria-label="표시 데이터 기간"
    >
      <strong>{isLoading ? '새 기간 조회 중' : '조회 요청 기간과 다릅니다'}</strong>
      <span>현재 표시 기간 {displayed.start} ~ {displayed.end}</span>
    </div>
  )
}

export function DashboardSkeleton(): React.JSX.Element {
  return (
    <div className="dashboard-skeleton" role="status" aria-label="대시보드 불러오는 중">
      <span className="sr-only">대시보드 정보를 불러오는 중입니다.</span>
      <div className="skeleton-period">
        <Skeleton width="9rem" height="1.5rem" />
        <Skeleton width="100%" height="2.75rem" />
      </div>
      <div className="summary-grid">
        {Array.from({ length: 6 }, (_, index) => (
          <div
            className={`metric-card${index === 0 ? ' metric-card--primary' : ''}`}
            data-testid="summary-skeleton"
            key={index}
          >
            <Skeleton width="40%" height="0.8rem" />
            <Skeleton width="72%" height="2rem" />
          </div>
        ))}
      </div>
      <div className="panel skeleton-panel" data-testid="chart-skeleton">
        <Skeleton width="11rem" height="1.5rem" />
        <Skeleton width="100%" height="15rem" />
      </div>
      <div className="panel skeleton-panel" data-testid="positions-skeleton">
        <Skeleton width="13rem" height="1.5rem" />
        <Skeleton width="100%" height="8rem" />
      </div>
      <div className="panel skeleton-panel" data-testid="trades-skeleton">
        <Skeleton width="11rem" height="1.5rem" />
        <Skeleton width="100%" height="8rem" />
      </div>
    </div>
  )
}
