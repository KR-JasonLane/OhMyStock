import { useEffect, useState } from 'react'

const BACKEND_HTTP = 'http://127.0.0.1:8000'
const BACKEND_WS = 'ws://127.0.0.1:8000/ws'
const RETRY_MS = 3000

export interface BackendStatus {
  connected: boolean
  db?: 'ok' | 'error'
  mode?: 'mock' | 'real'
}

export function useBackendStatus(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>({ connected: false })

  useEffect(() => {
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let disposed = false

    const connect = (): void => {
      fetch(`${BACKEND_HTTP}/health`)
        .then((r) => r.json())
        .then((h) => setStatus({ connected: true, db: h.db, mode: h.mode }))
        .catch(() => setStatus({ connected: false }))

      ws = new WebSocket(BACKEND_WS)
      ws.onmessage = (e): void => {
        const frame = JSON.parse(e.data)
        setStatus({ connected: true, db: frame.db, mode: frame.mode })
      }
      ws.onclose = (): void => {
        if (!disposed) {
          setStatus({ connected: false })
          retryTimer = setTimeout(connect, RETRY_MS)
        }
      }
    }

    connect()
    return (): void => {
      disposed = true
      clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  return status
}
