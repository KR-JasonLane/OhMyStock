import { DataTable } from 'primereact/datatable'
import { Column } from 'primereact/column'

import type { DashboardOverview } from '../../../api/dashboard'
import { formatDateTime, formatPrice, formatQuantity, formatWon, pnlTone } from '../format'

interface PositionsViewProps {
  positions: DashboardOverview['positions']
  freshness: DashboardOverview['freshness']
}

type Position = DashboardOverview['positions'][number]

function positionIdentity(position: Position): React.JSX.Element {
  return (
    <div className="instrument">
      <strong>{position.name}</strong>
      <span>{position.symbol}</span>
    </div>
  )
}

function isStale(
  position: Position,
  freshness: DashboardOverview['freshness']
): boolean {
  return position.marked_at !== null && (
    Date.parse(freshness.as_of) - Date.parse(position.marked_at) >
    freshness.mark_stale_after_seconds * 1_000
  )
}

function markPrice(
  position: Position,
  freshness: DashboardOverview['freshness']
): React.JSX.Element | string {
  if (position.mark_price === null) return '가격 확인 불가'
  if (isStale(position, freshness) || position.valuation_status === 'unavailable') {
    return <span className="stale-mark">저장 시세(오래됨) {formatPrice(position.mark_price)}</span>
  }
  return formatPrice(position.mark_price)
}

function valuedPnl(position: Position): number | null {
  return position.valuation_status === 'unavailable' ? null : position.unrealized_pnl
}

function DesktopPositions({ positions, freshness }: PositionsViewProps): React.JSX.Element {
  return (
    <div className="responsive-table responsive-table--desktop" data-testid="positions-desktop">
      <DataTable value={positions} dataKey="position_id" aria-label="현재 관리 포지션">
        <Column header="종목" body={(position: Position) => positionIdentity(position)} />
        <Column header="진입가" bodyClassName="numeric" body={(position: Position) => formatPrice(position.entry_price)} />
        <Column header="수량" bodyClassName="numeric" body={(position: Position) => formatQuantity(position.quantity)} />
        <Column
          header="진입시각"
          body={(position: Position) => position.entered_at === null
            ? '진입시각 없음'
            : formatDateTime(position.entered_at)}
        />
        <Column
          header="저장 시세"
          bodyClassName="numeric"
          body={(position: Position) => markPrice(position, freshness)}
        />
        <Column
          header="평가손익"
          bodyClassName={(position: Position) => `numeric value--${pnlTone(valuedPnl(position))}`}
          body={(position: Position) => formatWon(valuedPnl(position))}
        />
        <Column
          header="시세 기준"
          body={(position: Position) => position.marked_at === null
            ? '기준시각 없음'
            : formatDateTime(position.marked_at)}
        />
      </DataTable>
    </div>
  )
}

function MobilePositions({ positions, freshness }: PositionsViewProps): React.JSX.Element {
  return (
    <div className="summary-list summary-list--mobile" data-testid="positions-mobile">
      {positions.map((position) => (
        <article className="summary-row-card" key={position.position_id}>
          <header>
            {positionIdentity(position)}
            <span className={`summary-row-card__pnl value--${pnlTone(valuedPnl(position))}`}>
              <span className="summary-row-card__pnl-label">평가손익</span>
              <strong>{formatWon(valuedPnl(position))}</strong>
            </span>
          </header>
          <dl>
            <div>
              <dt>수량</dt>
              <dd>{formatQuantity(position.quantity)}</dd>
            </div>
            <div>
              <dt>진입가</dt>
              <dd>{formatPrice(position.entry_price)}</dd>
            </div>
            <div>
              <dt>진입시각</dt>
              <dd>{position.entered_at === null ? '진입시각 없음' : formatDateTime(position.entered_at)}</dd>
            </div>
            <div>
              <dt>저장 시세</dt>
              <dd>{markPrice(position, freshness)}</dd>
            </div>
            <div>
              <dt>시세 기준</dt>
              <dd>{position.marked_at === null ? '기준시각 없음' : formatDateTime(position.marked_at)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  )
}

export function PositionsView({ positions, freshness }: PositionsViewProps): React.JSX.Element {
  return (
    <section className="panel data-panel" aria-labelledby="positions-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">OPEN POSITIONS</p>
          <h2 id="positions-title">현재 관리 포지션</h2>
        </div>
        <span className="section-count">{positions.length}종목</span>
      </div>

      {positions.length === 0 ? (
        <div className="empty-state">
          <p>현재 관리 중인 포지션이 없습니다</p>
          <span>OhMyStock 소유 포지션만 이 영역에 표시됩니다.</span>
        </div>
      ) : (
        <>
          <DesktopPositions positions={positions} freshness={freshness} />
          <MobilePositions positions={positions} freshness={freshness} />
        </>
      )}
    </section>
  )
}
