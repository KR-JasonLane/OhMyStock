import { Button } from 'primereact/button'
import { Tag } from 'primereact/tag'

import type { RunEnvironment } from '../../../api/dashboard'
import { useTheme } from '../../../theme/context'
import { themeModes, type ThemeMode } from '../../../theme/preset'
import { formatDateTime } from '../format'

interface DashboardHeaderProps {
  isLoading: boolean
  hasData: boolean
  hasError: boolean
  environment: RunEnvironment | null
  lastUpdatedAt: Date | null
  onRefresh: () => void
}

const themeLabels: Record<ThemeMode, string> = {
  system: '시스템 설정',
  light: '라이트',
  dark: '다크'
}

const environmentLabels: Record<RunEnvironment, string> = {
  mock: '모의투자',
  real: '실거래',
  replay: '리플레이'
}

export function DashboardHeader({
  isLoading,
  hasData,
  hasError,
  environment,
  lastUpdatedAt,
  onRefresh
}: DashboardHeaderProps): React.JSX.Element {
  const { mode, setMode } = useTheme()
  const statusLabel = hasError
    ? hasData ? '마지막 성공 데이터' : '조회 실패'
    : !hasData ? '연결 확인 중' : '조회 정상'
  const environmentLabel = hasError
    ? '환경 확인 불가'
    : isLoading
      ? '환경 확인 중'
      : environment === null
        ? '환경 확인 불가'
        : environmentLabels[environment]
  const environmentTone = hasError || environment === null
    ? 'unknown'
    : isLoading
      ? 'checking'
      : environment

  return (
    <header className="dashboard-header">
      <div className="dashboard-header__inner">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">OM</div>
          <div>
            <p className="brand-eyebrow">AUTOMATED TRADING OPS</p>
            <h1>OhMyStock</h1>
          </div>
        </div>

        <div className="dashboard-header__actions">
          <label className="theme-control">
            <span>테마</span>
            <select
              aria-label="테마 선택"
              value={mode}
              onChange={(event) => setMode(event.target.value as ThemeMode)}
            >
              {themeModes.map((themeMode) => (
                <option key={themeMode} value={themeMode}>{themeLabels[themeMode]}</option>
              ))}
            </select>
          </label>
          <Button
            type="button"
            className="refresh-button"
            aria-label="대시보드 새로고침"
            disabled={isLoading}
            onClick={onRefresh}
          >
            {isLoading && hasData ? '갱신 중' : '새로고침'}
          </Button>
        </div>
      </div>
      <div className="dashboard-header__meta">
        <Tag
          className={`environment-tag environment-tag--${environmentTone}`}
          value={environmentLabel}
          aria-label={
            !isLoading && !hasError && environment === 'real'
              ? '실거래 환경: 실제 주문 가능'
              : undefined
          }
        />
        <span className={`connection-state${hasError ? ' connection-state--warning' : ''}`}>
          <span className="connection-state__dot" aria-hidden="true" />
          {statusLabel}
        </span>
        <span aria-hidden="true">·</span>
        <span>조회 전용</span>
        <span aria-hidden="true">·</span>
        <span>
          {lastUpdatedAt === null ? '아직 갱신되지 않음' : `마지막 갱신 ${formatDateTime(lastUpdatedAt)}`}
        </span>
      </div>
    </header>
  )
}
