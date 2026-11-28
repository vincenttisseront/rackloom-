import { execFileSync } from 'node:child_process'
import { expect, expectAccessible, logout, password, ROOT, test } from './helpers'
import type { Page } from '@playwright/test'

const OIDC_LAB_PASSWORD = 'rackloom-oidc-lab'

/** Remplit le formulaire password Dex (après éventuel choix de connecteur). */
async function dexPasswordLogin(page: Page, email: string, pwd: string) {
  await page.waitForURL(/\/dex\//, { timeout: 60_000 })
  const login = page.locator('input#login, input[name="login"]')
  if (!(await login.isVisible().catch(() => false))) {
    const connector = page.getByRole('link', { name: /log in with/i }).first()
    await expect(connector).toBeVisible({ timeout: 15_000 })
    await connector.click()
  }
  await expect(login).toBeVisible({ timeout: 30_000 })
  await login.fill(email)
  await page.locator('input#password, input[name="password"]').fill(pwd)
  await page.locator('button#submit-login, button[type="submit"], button:has-text("Login")').first().click()
}

/**
 * Attend la fin du flux OIDC : ne pas matcher le callback `/auth/oidc/callback`
 * (sinon un goto concurrent annule la navigation avant Set-Cookie).
 */
async function waitOidcSettled(page: Page) {
  await page.waitForURL(
    (url) => {
      const p = url.pathname
      if (p.includes('/dex/')) return false
      if (p.includes('/auth/oidc/callback')) return false
      return true
    },
    { timeout: 60_000 },
  )
}

test('Lot 4 — OIDC : login SSO, step-up, urgence si IdP down', async ({ page }) => {
  test.setTimeout(300_000)

  // --- Login SSO via Dex local ---
  await page.goto('/connexion')
  await expect(page.getByTestId('oidc-login')).toBeVisible()
  await page.getByTestId('oidc-login').click()
  await dexPasswordLogin(page, 'admin@demo.invalid', OIDC_LAB_PASSWORD)
  await waitOidcSettled(page)
  // return_to=/ : ne pas naviguer ailleurs avant que le cookie de session soit posé
  await expect(page.getByRole('heading', { level: 1, name: /Bonjour/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(/Admin \(démo\)|admin@demo\.invalid/i).first()).toBeVisible()
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
  if (page.url().includes('/dex/')) {
    const loginField = page.locator('input#login, input[name="login"]')
    if (await loginField.isVisible().catch(() => false)) {
      await loginField.fill('admin@demo.invalid')
      await page.locator('input#password, input[name="password"]').fill(OIDC_LAB_PASSWORD)
      await page.locator('button#submit-login, button[type="submit"], button:has-text("Login")').first().click()
    } else if (await page.getByRole('link', { name: /log in with/i }).first().isVisible().catch(() => false)) {
      await dexPasswordLogin(page, 'admin@demo.invalid', OIDC_LAB_PASSWORD)
    }
  }
  await waitOidcSettled(page)
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
  await page.getByLabel(/e-mail/i).fill('admin@demo.invalid')
  await page.getByLabel(/mot de passe/i).fill(password('admin'))
  await page.getByTestId('local-login').click()
  await expect(page).not.toHaveURL(/connexion/, { timeout: 30_000 })

  execFileSync('docker', [
    ...compose, 'run', '--rm', '--no-deps', 'cli', 'emergency', 'close',
    '--reason', 'e2e fin',
  ], { stdio: 'pipe' })
  execFileSync('docker', [...compose, 'start', 'dex'], { stdio: 'pipe' })
})
