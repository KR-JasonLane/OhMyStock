// @vitest-environment node

import { readFileSync } from 'node:fs'

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

describe('브라우저 문서 shell', () => {
  it('한국어 문서 언어와 실제 device width viewport를 선언한다', () => {
    const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')

    expect(html).toContain('<html lang="ko">')
    expect(html).toContain('<meta name="viewport" content="width=device-width, initial-scale=1.0" />')
  })
})
