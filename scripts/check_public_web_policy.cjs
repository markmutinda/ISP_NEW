// Run from the backend repo with the frontend checkout beside it.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { createRequire } = require('node:module')

const frontend = path.resolve(__dirname, '../../netily')
const frontendRequire = createRequire(path.join(frontend, 'package.json'))
const ts = frontendRequire('typescript')

function load(relative, imports = {}) {
  const file = path.join(frontend, relative)
  const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    fileName: file.replace(/\.mjs$/, '.ts'),
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    reportDiagnostics: true,
  })
  assert.equal(output.diagnostics.length, 0, relative)
  const exports = {}
  vm.runInNewContext(output.outputText, {
    exports,
    process: { env: { NODE_ENV: 'production' } },
    require: (name) => {
      assert.ok(name in imports, `Unexpected import: ${name}`)
      return imports[name]
    },
  })
  return exports
}

async function main() {
  const robots = load('app/robots.ts').default()
  const rules = robots.rules
  assert.equal(rules.userAgent, '*')
  assert.equal(rules.disallow, '/')
  const matches = (rule, url) => {
    const end = rule.endsWith('$')
    const value = end ? rule.slice(0, -1) : rule
    const escaped = value.split('*').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*')
    return new RegExp('^' + escaped + (end ? '$' : '')).test(url)
  }
  const allowed = url => rules.allow.some(rule => matches(rule, url))
  for (const url of ['/', '/?utm_source=search', '/affiliate', '/affiliate/register', '/blog/example',
    '/solutions/isp-billing-software-kenya-counties/nairobi', '/_next/static/chunk.js', '/sitemap-gsc.xml']) {
    assert.ok(allowed(url), `Public route blocked: ${url}`)
  }
  for (const url of ['/admin', '/admin/login', '/superadmin', '/api/v1/core/', '/portal/login',
    '/customer', '/support/login', '/netilysystempayment', '/affiliate/login', '/affiliate/dashboard',
    '/affiliate/referrals', '/affiliate/verify?token=example']) {
    assert.ok(!allowed(url), `Private route allowed: ${url}`)
    assert.ok(!JSON.stringify(robots).includes(url.split('?')[0]), `Private route advertised: ${url}`)
  }
  if (process.argv.includes('--sitemap-stdin')) {
    const urls = JSON.parse(fs.readFileSync(0, 'utf8').replace(/^\uFEFF/, ''))
    for (const url of urls) assert.ok(allowed(new URL(url).pathname), `Sitemap page blocked: ${url}`)
    console.log(`Checked ${urls.length} sitemap entries`)
  }
  const config = load('next.config.mjs', { '@ducanh2912/next-pwa': () => config => config }).default
  const headers = await config.headers()
  const globalHeaders = headers.find(rule => rule.source === '/(.*)').headers
  assert.equal(globalHeaders.find(header => header.key === 'Strict-Transport-Security').value, 'max-age=31536000')
  for (const route of ['/admin/:path*', '/superadmin/:path*', '/affiliate/login/:path*']) {
    assert.ok(headers.find(rule => rule.source === route).headers.some(header => header.key === 'X-Robots-Tag' && header.value.includes('noindex')))
  }
  const security = fs.readFileSync(path.join(frontend, 'public/.well-known/security.txt'), 'utf8')
  assert.ok(security.includes('Contact: mailto:netilysupport@gmail.com'))
  const expiry = new Date(security.match(/^Expires: (.+)$/m)[1].trim())
  assert.ok(expiry > new Date(), 'Security contact has expired')
  assert.ok(expiry.getTime() - Date.now() < 366 * 86400000, 'Expiry should be within one year')
  class ResponseStub {
    constructor(body, options) { this.body = body; this.options = options }
  }
  const proxy = load('proxy.ts', { 'next/server': { NextResponse: ResponseStub } }).proxy
  for (const hostname of ['demo.netily.co.ke', 'tenant.netily.co.ke', 'bentrextechnologies.com']) {
    const response = proxy({ nextUrl: { pathname: '/robots.txt' }, headers: { get: () => hostname } })
    assert.equal(response.body, 'User-agent: *\nDisallow: /\n', hostname)
  }
  const fonts = fs.readFileSync(path.join(frontend, 'app/layout.tsx'), 'utf8')
  assert.equal((fonts.match(/preload: false/g) || []).length, 14)
  console.log('PASS: public crawling, private exclusions, production HSTS, noindex headers, security contact')
}

main().catch(error => { console.error(error); process.exitCode = 1 })
