import { Button } from 'primereact/button'

import type { DateRange } from '../../../api/dashboard'

interface PeriodPickerProps {
  period: DateRange
  disabled: boolean
  onChange: (period: DateRange) => void
}

type Preset = '7d' | '30d' | 'month'

function shiftDate(date: string, amount: number): string {
  const shifted = new Date(`${date}T00:00:00Z`)
  shifted.setUTCDate(shifted.getUTCDate() + amount)
  return shifted.toISOString().slice(0, 10)
}

function monthStart(date: string): string {
  return `${date.slice(0, 7)}-01`
}

function presetRange(end: string, preset: Preset): DateRange {
  const start = preset === '7d' ? shiftDate(end, -6) : preset === '30d' ? shiftDate(end, -29) : monthStart(end)
  return { start, end, timezone: 'Asia/Seoul' }
}

function activePreset(period: DateRange): Preset | null {
  if (period.start === shiftDate(period.end, -6)) return '7d'
  if (period.start === shiftDate(period.end, -29)) return '30d'
  if (period.start === monthStart(period.end)) return 'month'
  return null
}

export function PeriodPicker({ period, disabled, onChange }: PeriodPickerProps): React.JSX.Element {
  const selected = activePreset(period)

  const changeDate = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const form = event.currentTarget.form
    if (form === null) return
    const formData = new FormData(form)
    const next: DateRange = {
      start: String(formData.get('start') ?? ''),
      end: String(formData.get('end') ?? ''),
      timezone: 'Asia/Seoul'
    }
    if (next.start !== '' && next.end !== '' && next.start <= next.end) onChange(next)
  }

  return (
    <section className="period-panel" aria-labelledby="period-title">
      <div className="section-heading period-panel__heading">
        <div>
          <p className="section-kicker">PERFORMANCE WINDOW</p>
          <h2 id="period-title">조회 기간</h2>
        </div>
        <span className="period-panel__timezone">Asia/Seoul 기준</span>
      </div>

      <form className="period-panel__controls" key={`${period.start}:${period.end}`}>
        <div className="preset-group" aria-label="빠른 기간 선택">
          {([
            ['7d', '최근 7일'],
            ['30d', '최근 30일'],
            ['month', '이번 달']
          ] as const).map(([preset, label]) => (
            <Button
              key={preset}
              type="button"
              className="preset-button"
              aria-pressed={selected === preset}
              disabled={disabled}
              onClick={() => onChange(presetRange(period.end, preset))}
            >
              {label}
            </Button>
          ))}
        </div>

        <div className="date-range">
          <label>
            <span>시작일</span>
            <input
              type="date"
              name="start"
              aria-label="조회 시작일"
              defaultValue={period.start}
              max={period.end}
              disabled={disabled}
              onChange={changeDate}
            />
          </label>
          <span className="date-range__separator" aria-hidden="true">—</span>
          <label>
            <span>종료일</span>
            <input
              type="date"
              name="end"
              aria-label="조회 종료일"
              defaultValue={period.end}
              min={period.start}
              disabled={disabled}
              onChange={changeDate}
            />
          </label>
        </div>
      </form>
    </section>
  )
}
