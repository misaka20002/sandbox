#!/usr/bin/env node

import fs from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import puppeteer from 'puppeteer-extra'
import StealthPlugin from 'puppeteer-extra-plugin-stealth'

puppeteer.use(StealthPlugin())

function usage() {
  console.error('Usage: node /app/tools/web_capture.mjs URL [--output outputs/page.png] [--full-page] [--width 1440] [--height 900] [--wait-ms 2000] [--timeout-ms 60000] [--profile DIR] [--cookies FILE]')
}

function parseArgs(argv) {
  const result = {
    url: argv[0], output: 'outputs/page.png', fullPage: false,
    width: 1440, height: 900, waitMs: 1500, timeoutMs: 60000,
    profile: '.browser-profile', cookies: null,
  }
  for (let i = 1; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--full-page') result.fullPage = true
    else if (arg === '--output') result.output = argv[++i]
    else if (arg === '--width') result.width = Number(argv[++i])
    else if (arg === '--height') result.height = Number(argv[++i])
    else if (arg === '--wait-ms') result.waitMs = Number(argv[++i])
    else if (arg === '--timeout-ms') result.timeoutMs = Number(argv[++i])
    else if (arg === '--profile') result.profile = argv[++i]
    else if (arg === '--cookies') result.cookies = argv[++i]
    else throw new Error(`Unknown argument: ${arg}`)
  }
  return result
}

function chromiumPath() {
  return process.env.CHROME_BIN || process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium'
}

function looksLikeChallenge(title, html) {
  const text = `${title}\n${html.slice(0, 200000)}`.toLowerCase()
  return [
    'just a moment', 'checking your browser', 'verify you are human',
    'attention required', 'cf-chl-', 'challenge-platform', 'turnstile',
  ].some(marker => text.includes(marker))
}

let browser
try {
  const options = parseArgs(process.argv.slice(2))
  if (!options.url || !/^https?:\/\//i.test(options.url)) {
    usage()
    process.exitCode = 2
  } else {
    await fs.mkdir(path.dirname(path.resolve(options.output)), { recursive: true })
    await fs.mkdir(path.resolve(options.profile), { recursive: true })
    browser = await puppeteer.launch({
      executablePath: chromiumPath(),
      headless: true,
      userDataDir: path.resolve(options.profile),
      args: [
        '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
        '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      ],
    })
    const pages = await browser.pages()
    const page = pages[0] || await browser.newPage()
    // Present ONE self-consistent Chrome identity. The stealth plugin only
    // rewrites the JS navigator.userAgent; the HTTP User-Agent header and the
    // Client Hints (Sec-CH-UA / navigator.userAgentData) still leak
    // "HeadlessChrome" or a version that disagrees with the UA string, which is
    // exactly what bot detectors (e.g. BrowserScan) flag as a fake User-Agent.
    // Derive everything from the real browser version and keep the honest Linux
    // platform so navigator.platform stays consistent.
    const realUserAgent = await browser.userAgent()
    const userAgent = realUserAgent.replace(/HeadlessChrome/g, 'Chrome')
    const fullVersion = (userAgent.match(/Chrome\/([\d.]+)/) || [, '124.0.0.0'])[1]
    const majorVersion = fullVersion.split('.')[0]
    const brands = [
      { brand: 'Chromium', version: majorVersion },
      { brand: 'Google Chrome', version: majorVersion },
      { brand: 'Not.A/Brand', version: '24' },
    ]
    await page.setUserAgent(userAgent, {
      architecture: 'x86',
      bitness: '64',
      brands,
      fullVersion,
      fullVersionList: brands.map(({ brand }) => ({ brand, version: fullVersion })),
      mobile: false,
      model: '',
      platform: 'Linux',
      platformVersion: '6.1.0',
      wow64: false,
    })
    await page.setExtraHTTPHeaders({ 'Accept-Language': 'en-US,en;q=0.9' })
    await page.setViewport({ width: options.width, height: options.height, deviceScaleFactor: 1 })
    page.setDefaultNavigationTimeout(options.timeoutMs)
    if (options.cookies) {
      const cookies = JSON.parse(await fs.readFile(options.cookies, 'utf8'))
      if (!Array.isArray(cookies)) throw new Error('cookies file must contain a JSON array')
      await page.setCookie(...cookies)
    }
    const response = await page.goto(options.url, { waitUntil: 'domcontentloaded' })
    await page.waitForNetworkIdle({ idleTime: 500, timeout: Math.min(options.timeoutMs, 15000) }).catch(() => {})
    if (options.waitMs > 0) await new Promise(resolve => setTimeout(resolve, options.waitMs))
    const title = await page.title()
    const html = await page.content()
    await page.screenshot({ path: path.resolve(options.output), fullPage: options.fullPage })
    const result = {
      ok: true,
      url: page.url(),
      title,
      http_status: response?.status() ?? null,
      output: options.output,
      challenge_detected: looksLikeChallenge(title, html),
    }
    console.log(JSON.stringify(result, null, 2))
  }
} catch (error) {
  console.error(error?.stack || String(error))
  process.exitCode = 1
} finally {
  if (browser) await browser.close()
}
