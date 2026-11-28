import { execFileSync } from 'node:child_process'
import { expect, expectAccessible, logout, password, ROOT, test } from './helpers'

const OIDC_LAB_PASSWORD = 'rackloom-oidc-lab'

test('Lot 4 — OIDC : login SSO, step-up, urgence si IdP down', async ({ page }) => {
  test.setTimeout(300_000)

  // --- Login SSO via Dex local ---
  await page.goto('/connexion')
  await expect(page.getByTestId('oidc-login')).toBeVisible()
  await page.getByTestId('oidc-login').click()
  // Dex login form
  await page.waitForURL(/\/dex\//, { timeout: 60_000 })
  await page.locator('input[name="login"], input#login, input[type="text"]').first().fill('admin@demo.invalid')
  await page.locator('input[name="password"], input#password, input[type="password"]').first().fill(OIDC_LAB_PASSWORD)
  await page.locator('button[type="submit"], button:has-text("Login"), input[type="submit"]').first().click()
  await page.waitForURL(/^(?!.*\/dex\/).*/, { timeout: 60_000 })
  // Session établie
  await page.goto('/')
  await expect(page.getByText(/Admin|admin@demo\.invalid/i).first()).toBeVisible({ timeout: 30_000 })
  await expectAccessible(page, 'Lot 4 OIDC connecté')

  // --- Admin SSO page (secret jamais affiché) ---
  await page.goto('/admin/sso')
  await expect(page.getByRole('heading', { level: 1, name: /SSO OIDC/ })).toBeVisible()
  await expect(page.getByTestId('oidc-secret-status')).toHaveText(/absent|défini/)
  await expect(page.getByTestId('oidc-client-secret')).toHaveValue('')
  await page.getByTestId('oidc-probe').click()
  await expect(page.getByTestId('oidc-reachable')).toHaveText('oui', { timeout: 30_000 })

  // --- Step-up OIDC (F12) : démarrer depuis l'API puis Dex ---
  const csrf = await page.evaluate(async () => {
    const r = await fetch('/api/v1/session', { credentials: 'same-origin' })
    const j = await r.json()
    return j.csrf_token as string
  })
  const authUrl = await page.evaluate(async (token) => {
    const r = await fetch('/api/v1/auth/oidc/reauth?return_to=/admin/sso', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRF-Token': token, Origin: window.location.origin },
    })
    const j = await r.json()
    return j.authorization_url as string
  }, csrf)
  expect(authUrl).toContain('/dex/')
  await page.goto(authUrl)
  // Dex peut réafficher le login (prompt=login)
  if (page.url().includes('/dex/')) {
    const loginField = page.locator('input[name="login"], input#login, input[type="text"]').first()
    if (await loginField.isVisible().catch(() => false)) {
      await loginField.fill('admin@demo.invalid')
      await page.locator('input[name="password"], input#password, input[type="password"]').first().fill(OIDC_LAB_PASSWORD)
      await page.locator('button[type="submit"], button:has-text("Login"), input[type="submit"]').first().click()
    }
  }
  await page.waitForURL(/\/admin\/sso/, { timeout: 60_000 })
  const sessionAfter = await page.evaluate(async () => {
    const r = await fetch('/api/v1/session', { credentials: 'same-origin' })
    return r.json()
  })
  expect(sessionAfter.auth_method).toBe('oidc_step_up')
  expect(sessionAfter.acr).toBe('urn:rackloom:step-up')

  await logout(page)

  // --- IdP down + urgence F14 ---
  const compose = [
    'compose', '-f', `${ROOT}/compose/compose.yaml`, '-f', `${ROOT}/compose/compose.demo.yaml`,
    '-f', `${ROOT}/compose/compose.oidc.yaml`, '-f', `${ROOT}/compose/compose.test.yaml`,
  ]
  execFileSync('docker', [...compose, 'stop', 'dex'], { stdio: 'pipe' })
  execFileSync('docker', [
    ...compose, 'run', '--rm', '--no-deps', 'cli', 'emergency', 'activate',
    '--reason', 'e2e IdP Dex arrêté', '--minutes', '30', '--user', 'admin@demo.invalid',
  ], { stdio: 'pipe' })

  await page.goto('/connexion')
  await expect(page.getByTestId('sso-down-banner')).toBeVisible({ timeout: 30_000 })
  // Login local urgence
  await page.getByLabel(/e-mail/i).fill('admin@demo.invalid')
  await page.getByLabel(/mot de passe/i).fill(password('admin'))
  await page.getByTestId('local-login').click()
  await expect(page).not.toHaveURL(/connexion/, { timeout: 30_000 })

  // Cleanup : restore SSO + restart dex
  execFileSync('docker', [
    ...compose, 'run', '--rm', '--no-deps', 'cli', 'emergency', 'close',
    '--reason', 'e2e fin',
  ], { stdio: 'pipe' })
  execFileSync('docker', [...compose, '--profile', 'oidc', 'start', 'dex'], { stdio: 'pipe' })
})
