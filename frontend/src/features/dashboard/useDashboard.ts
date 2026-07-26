import { useCallback, useEffect, useRef, useState } from 'react'

import {
  DashboardRequestError,
  fetchDashboardOverview,
  type DashboardErrorCode,
  type DashboardOverview,
  type DateRange
} from '../../api/dashboard'

export interface DashboardQueryState {
  data: DashboardOverview | null
  period: DateRange
  isLoading: boolean
  errorCode: DashboardErrorCode | null
  lastUpdatedAt: Date | null
  setPeriod: (period: DateRange) => void
  refresh: () => Promise<void>
}

interface RequestInFlight {
  controller: AbortController
  promise: Promise<void>
}

function defaultDateRange(now = new Date()): DateRange {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(now)
  const part = (name: Intl.DateTimeFormatPartTypes): string =>
    parts.find((candidate) => candidate.type === name)?.value ?? ''
  const end = new Date(Date.UTC(Number(part('year')), Number(part('month')) - 1, Number(part('day'))))
  const start = new Date(end)
  start.setUTCDate(start.getUTCDate() - 29)
  const format = (date: Date): string => date.toISOString().slice(0, 10)
  return { start: format(start), end: format(end), timezone: 'Asia/Seoul' }
}

function failureCode(error: unknown): DashboardErrorCode {
  return error instanceof DashboardRequestError ? error.code : 'dashboard_request_failed'
}

export function useDashboard(initialPeriod = defaultDateRange()): DashboardQueryState {
  const [period, setPeriod] = useState<DateRange>(initialPeriod)
  const [state, setState] = useState<Omit<DashboardQueryState, 'period' | 'setPeriod' | 'refresh'>>({
    data: null,
    isLoading: true,
    errorCode: null,
    lastUpdatedAt: null
  })
  const inFlightRef = useRef<RequestInFlight | null>(null)

  const startRequest = useCallback((requestedPeriod: DateRange): Promise<void> => {
    const activeRequest = inFlightRef.current
    if (activeRequest !== null) return activeRequest.promise

    const controller = new AbortController()
    setState((previous) => ({ ...previous, isLoading: true, errorCode: null }))
    const promise = fetchDashboardOverview(requestedPeriod, controller.signal)
      .then((data) => {
        if (inFlightRef.current?.controller !== controller) return
        setState({ data, isLoading: false, errorCode: null, lastUpdatedAt: new Date() })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || inFlightRef.current?.controller !== controller) return
        const code = failureCode(error)
        console.warn('Dashboard overview request failed', {
          code,
          causeType: error instanceof DashboardRequestError && error.cause instanceof Error
            ? error.cause.name
            : error instanceof Error
              ? error.name
              : 'unknown'
        })
        setState((previous) => ({ ...previous, isLoading: false, errorCode: code }))
        throw error
      })
      .finally(() => {
        if (inFlightRef.current?.controller === controller) inFlightRef.current = null
      })
    inFlightRef.current = { controller, promise }
    return promise
  }, [])

  useEffect(() => {
    let disposed = false
    queueMicrotask(() => {
      if (disposed) return
      const request = startRequest(period)
      void request.catch(() => undefined)
    })
    return () => {
      disposed = true
      const activeRequest = inFlightRef.current
      if (activeRequest !== null) {
        inFlightRef.current = null
        activeRequest.controller.abort()
      }
    }
  }, [period, startRequest])

  const refresh = useCallback((): Promise<void> => startRequest(period), [period, startRequest])

  return { ...state, period, setPeriod, refresh }
}
