import { useEffect, useRef, useState } from 'react'

interface PullToRefreshOptions {
  enabled: boolean
  refreshing: boolean
  onRefresh: () => Promise<void>
  threshold?: number
}

export interface PullToRefreshState {
  distance: number
  isPulling: boolean
  isReady: boolean
}

interface StartPoint {
  x: number
  y: number
  identifier: number
}

const MAX_PULL_DISTANCE = 108

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('button, input, select, textarea, a') !== null
}

function touchWithIdentifier(touches: TouchList, identifier: number): Touch | null {
  return Array.from(touches).find((touch) => touch.identifier === identifier) ?? null
}

export function usePullToRefresh({
  enabled,
  refreshing,
  onRefresh,
  threshold = 72
}: PullToRefreshOptions): PullToRefreshState {
  const [state, setState] = useState<PullToRefreshState>({
    distance: 0,
    isPulling: false,
    isReady: false
  })
  const startRef = useRef<StartPoint | null>(null)
  const pendingRef = useRef(false)
  const refreshRef = useRef(onRefresh)

  useEffect(() => {
    refreshRef.current = onRefresh
  }, [onRefresh])

  useEffect(() => {
    const reset = (): void => {
      startRef.current = null
      setState({ distance: 0, isPulling: false, isReady: false })
    }

    const onTouchStart = (event: TouchEvent): void => {
      if (event.touches.length !== 1) {
        reset()
        return
      }
      if (
        !enabled ||
        refreshing ||
        pendingRef.current ||
        window.scrollY !== 0 ||
        isInteractiveTarget(event.target)
      ) return
      const touch = event.touches[0]
      startRef.current = {
        x: touch.clientX,
        y: touch.clientY,
        identifier: touch.identifier
      }
    }

    const onTouchMove = (event: TouchEvent): void => {
      const start = startRef.current
      if (start === null) return
      if (event.touches.length !== 1) {
        reset()
        return
      }
      const touch = touchWithIdentifier(event.touches, start.identifier)
      if (touch === null) return
      const deltaX = touch.clientX - start.x
      const deltaY = touch.clientY - start.y
      if (deltaY <= 0 || Math.abs(deltaX) > deltaY) {
        reset()
        return
      }
      if (event.cancelable) event.preventDefault()
      const distance = Math.min(deltaY, MAX_PULL_DISTANCE)
      setState({ distance, isPulling: true, isReady: distance >= threshold })
    }

    const onTouchEnd = (event: TouchEvent): void => {
      const start = startRef.current
      if (start === null) return
      const touch = touchWithIdentifier(event.changedTouches, start.identifier)
      if (touch === null) return
      const deltaY = touch.clientY - start.y
      const deltaX = touch.clientX - start.x
      const shouldRefresh = (
        enabled &&
        !refreshing &&
        !pendingRef.current &&
        deltaY >= threshold &&
        Math.abs(deltaX) <= deltaY
      )
      reset()
      if (!shouldRefresh) return
      pendingRef.current = true
      void refreshRef.current()
        .catch(() => undefined)
        .finally(() => {
          pendingRef.current = false
        })
    }

    document.addEventListener('touchstart', onTouchStart)
    document.addEventListener('touchmove', onTouchMove, { passive: false })
    document.addEventListener('touchend', onTouchEnd)
    document.addEventListener('touchcancel', reset)
    return () => {
      document.removeEventListener('touchstart', onTouchStart)
      document.removeEventListener('touchmove', onTouchMove)
      document.removeEventListener('touchend', onTouchEnd)
      document.removeEventListener('touchcancel', reset)
      startRef.current = null
    }
  }, [enabled, refreshing, threshold])

  return state
}
