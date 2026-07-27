import { DataTable } from 'primereact/datatable'
import { Column } from 'primereact/column'
import { Tag } from 'primereact/tag'

import type { DashboardOverview } from '../../../api/dashboard'
import {
  formatDateTime,
  formatExitReason,
  formatPrice,
  formatQuantity,
  formatWon,
  pnlTone
} from '../format'

interface RecentTradesViewProps {
  trades: DashboardOverview['recent_trades']
}

type Trade = DashboardOverview['recent_trades'][number]

function tradeIdentity(trade: Trade): React.JSX.Element {
  return (
    <div className="instrument">
      <strong>{trade.name}</strong>
      <span>{trade.symbol}</span>
    </div>
  )
}

function ExitReason({ reason }: { reason: string | null }): React.JSX.Element {
  return <Tag className="reason-tag" value={formatExitReason(reason)} />
}

function DesktopTrades({ trades }: RecentTradesViewProps): React.JSX.Element {
  return (
    <div className="responsive-table responsive-table--desktop" data-testid="trades-desktop">
      <DataTable value={trades} dataKey="position_id" aria-label="최근 관리 거래">
        <Column header="종목" body={(trade: Trade) => tradeIdentity(trade)} />
        <Column header="수량" bodyClassName="numeric" body={(trade: Trade) => formatQuantity(trade.quantity)} />
        <Column header="진입가" bodyClassName="numeric" body={(trade: Trade) => formatPrice(trade.entry_price)} />
        <Column
          header="청산가"
          bodyClassName="numeric"
          body={(trade: Trade) => trade.exit_price === null ? '확인 불가' : formatPrice(trade.exit_price)}
        />
        <Column
          header="확정손익"
          bodyClassName={(trade: Trade) => `numeric value--${pnlTone(trade.realized_pnl)}`}
          body={(trade: Trade) => formatWon(trade.realized_pnl)}
        />
        <Column header="청산 사유" body={(trade: Trade) => <ExitReason reason={trade.exit_reason} />} />
        <Column header="완료시각" body={(trade: Trade) => formatDateTime(trade.closed_at)} />
      </DataTable>
    </div>
  )
}

function MobileTrades({ trades }: RecentTradesViewProps): React.JSX.Element {
  return (
    <div className="summary-list summary-list--mobile" data-testid="trades-mobile">
      {trades.map((trade) => (
        <article className="summary-row-card" key={trade.position_id}>
          <header>
            {tradeIdentity(trade)}
            <span className={`summary-row-card__pnl value--${pnlTone(trade.realized_pnl)}`}>
              <span className="summary-row-card__pnl-label">확정손익</span>
              <strong>{formatWon(trade.realized_pnl)}</strong>
            </span>
          </header>
          <div className="summary-row-card__reason"><ExitReason reason={trade.exit_reason} /></div>
          <dl>
            <div>
              <dt>수량</dt>
              <dd>{formatQuantity(trade.quantity)}</dd>
            </div>
            <div>
              <dt>진입가</dt>
              <dd>{formatPrice(trade.entry_price)}</dd>
            </div>
            <div>
              <dt>청산가</dt>
              <dd>{trade.exit_price === null ? '확인 불가' : formatPrice(trade.exit_price)}</dd>
            </div>
            <div>
              <dt>완료시각</dt>
              <dd>{formatDateTime(trade.closed_at)}</dd>
            </div>
          </dl>
        </article>
      ))}
    </div>
  )
}

export function RecentTradesView({ trades }: RecentTradesViewProps): React.JSX.Element {
  return (
    <section className="panel data-panel" aria-labelledby="trades-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">CLOSED TRADES</p>
          <h2 id="trades-title">최근 관리 거래</h2>
        </div>
        <span className="section-count">{trades.length}건</span>
      </div>

      {trades.length === 0 ? (
        <div className="empty-state">
          <p>선택한 기간에 완료된 관리 거래가 없습니다</p>
          <span>기간 안에 청산이 확정된 거래만 집계됩니다.</span>
        </div>
      ) : (
        <>
          <DesktopTrades trades={trades} />
          <MobileTrades trades={trades} />
        </>
      )}
    </section>
  )
}
