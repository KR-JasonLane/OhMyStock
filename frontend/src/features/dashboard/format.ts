import type { Completeness, CostBasis } from '../../api/dashboard'

const wonFormatter = new Intl.NumberFormat('ko-KR', {
  style: 'currency',
  currency: 'KRW',
  currencyDisplay: 'narrowSymbol',
  maximumFractionDigits: 0,
  signDisplay: 'exceptZero'
})

const priceFormatter = new Intl.NumberFormat('ko-KR', {
  style: 'currency',
  currency: 'KRW',
  currencyDisplay: 'narrowSymbol',
  maximumFractionDigits: 0,
  signDisplay: 'never'
})

const percentFormatter = new Intl.NumberFormat('ko-KR', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
  signDisplay: 'exceptZero'
})

const unsignedPercentFormatter = new Intl.NumberFormat('ko-KR', {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
  signDisplay: 'never'
})

const integerFormatter = new Intl.NumberFormat('ko-KR', {
  maximumFractionDigits: 0
})

const dateTimeFormatter = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul',
  year: 'numeric',
  month: 'numeric',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit'
})

const dateFormatter = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul',
  month: 'short',
  day: 'numeric'
})

export function formatWon(value: number | null): string {
  return value === null ? '확인 불가' : wonFormatter.format(value)
}

export function formatPrice(value: number | null): string {
  return value === null ? '확인 불가' : priceFormatter.format(value)
}

export function formatPercent(value: number | null): string {
  return value === null ? '확인 불가' : `${percentFormatter.format(value)}%`
}

export function formatUnsignedPercent(value: number | null): string {
  return value === null ? '확인 불가' : `${unsignedPercentFormatter.format(value)}%`
}

export function formatQuantity(value: number): string {
  return `${integerFormatter.format(value)}주`
}

export function formatDateTime(value: string | Date | null): string {
  if (value === null) return '확인 불가'
  return dateTimeFormatter.format(value instanceof Date ? value : new Date(value))
}

export function formatChartDate(value: string): string {
  return dateFormatter.format(new Date(value))
}

export function completenessLabel(status: Completeness): string | null {
  if (status === 'complete') return null
  return status === 'partial' ? '일부만 확인' : '확인 불가'
}

export function costBasisLabel(costBasis: CostBasis): string {
  if (costBasis === 'estimated') return '비용 추정 반영'
  if (costBasis === 'unavailable') return '비용 확인 불가'
  return '비용 기록 반영'
}

export function pnlTone(value: number | null): 'positive' | 'negative' | 'neutral' | 'unavailable' {
  if (value === null) return 'unavailable'
  if (value > 0) return 'positive'
  if (value < 0) return 'negative'
  return 'neutral'
}

const exitReasonLabels: Record<string, string> = {
  take_profit: '목표 수익 도달',
  stop_loss: '손실 제한',
  kill_switch: '안전 중지 청산',
  end_of_day: '장 마감 청산',
  trailing_stop: '추적 손절',
  max_holding: '최대 보유기간 도달',
  manual: '수동 청산'
}

export function formatExitReason(reason: string | null): string {
  if (reason === null) return '사유 확인 불가'
  return exitReasonLabels[reason] ?? '기타 청산'
}
