// @vitest-environment node

import { describe, expect, it } from 'vitest'

import config from '../vite.config'

describe('Vite development proxy', () => {
  it('상대 /api dashboard 호출을 loopback backend로만 전달한다', () => {
    expect(config.server?.proxy).toEqual({
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: expect.any(Function)
      }
    })
    const apiProxy = config.server?.proxy?.['/api']
    expect(typeof apiProxy === 'object' && apiProxy !== null && 'rewrite' in apiProxy
      ? apiProxy.rewrite?.('/api/dashboard/overview')
      : undefined).toBe('/dashboard/overview')
  })
})
