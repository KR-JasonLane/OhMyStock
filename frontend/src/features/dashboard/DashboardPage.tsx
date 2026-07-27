import { type CSSProperties, useCallback } from 'react'

import { usePullToRefresh } from '../../hooks/usePullToRefresh'
import {
  DashboardSkeleton,
  DataWarnings,
  DisplayedPeriodNotice,
  InitialError,
  RefreshErrorBanner
} from './components/DashboardFeedback'
import { DashboardHeader } from './components/DashboardHeader'
import { PerformanceSummary } from './components/PerformanceSummary'
import { PeriodPicker } from './components/PeriodPicker'
import { PositionsView } from './components/PositionsView'
import { ProfitChart } from './components/ProfitChart'
import { RecentTradesView } from './components/RecentTradesView'
import { formatDateTime } from './format'
import { useDashboard } from './useDashboard'

export function DashboardPage(): React.JSX.Element {
  const query = useDashboard()
  const refreshQuery = query.refresh
  const refresh = useCallback((): void => {
    void refreshQuery().catch(() => undefined)
  }, [refreshQuery])
  const pull = usePullToRefresh({
    enabled: !query.isLoading,
    refreshing: query.isLoading,
    onRefresh: refreshQuery,
    threshold: 72
  })
  const showRefreshProgress = query.isLoading && query.data !== null
  const showPullIndicator = pull.isPulling || showRefreshProgress
  const pullLabel = showRefreshProgress
    ? '새로고침 중'
    : pull.isReady
      ? '놓아서 새로고침'
      : '당겨서 새로고침'

  return (
    <div className="dashboard-app">
      <DashboardHeader
        isLoading={query.isLoading}
        hasData={query.data !== null}
        hasError={query.errorCode !== null}
        environment={query.data?.environment ?? null}
        lastUpdatedAt={query.lastUpdatedAt}
        onRefresh={refresh}
      />

      <div
        className={`pull-indicator${showPullIndicator ? ' pull-indicator--visible' : ''}${pull.isReady ? ' pull-indicator--ready' : ''}${showRefreshProgress ? ' pull-indicator--refreshing' : ''}`}
        role={showPullIndicator ? 'status' : undefined}
        aria-label={showPullIndicator ? pullLabel : undefined}
        style={{ '--pull-distance': `${pull.distance}px` } as CSSProperties}
      >
        <span>{pullLabel}</span>
      </div>

      <main className="dashboard-shell" aria-label="OhMyStock 거래 성과 대시보드">
        {query.data === null && query.isLoading && <DashboardSkeleton />}
        {query.data === null && !query.isLoading && query.errorCode !== null && (
          <InitialError code={query.errorCode} onRetry={refresh} />
        )}

        {query.data !== null && (
          <>
            <PeriodPicker
              period={query.period}
              disabled={query.isLoading}
              onChange={query.setPeriod}
            />
            <DisplayedPeriodNotice
              requested={query.period}
              displayed={query.data.period}
              isLoading={query.isLoading}
            />
            {query.errorCode !== null && <RefreshErrorBanner code={query.errorCode} />}
            <DataWarnings data={query.data} />
            <PerformanceSummary summary={query.data.summary} />
            <ProfitChart points={query.data.equity_curve} />
            <PositionsView
              positions={query.data.positions}
              freshness={query.data.freshness}
            />
            <RecentTradesView trades={query.data.recent_trades} />
            <footer className="dashboard-footer">
              <div>
                <span>데이터 기준</span>
                <strong>{formatDateTime(query.data.freshness.as_of)}</strong>
              </div>
              <p>저장된 OhMyStock 관리 거래만 집계 · 수동 거래 및 외부 보유분 제외</p>
            </footer>
          </>
        )}
      </main>
    </div>
  )
}
