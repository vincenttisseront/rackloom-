import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import AxeBuilder from '@axe-core/playwright'
import { test as base, expect, type Locator, type Page } from '@playwright/test'

export const ROOT = fileURLToPath(new URL('../..', import.meta.url))
export const D1 = 'Je veux une VM Ubuntu de test avec Nginx'

// Curseur visible dans les vidéos ; il ne capte aucun clic.
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.addInitScript(() => {
      const install = () => {
        if (document.getElementById('__pw_cursor')) return
        const c = document.createElement('div')
        c.id = '__pw_cursor'
        c.setAttribute('aria-hidden', 'true')
        Object.assign(c.style, {
          position: 'fixed', left: '-40px', top: '-40px', width: '18px', height: '18px', borderRadius: '50%',
          border: '2px solid #c2185b', background: 'rgba(194, 24, 91, 0.25)', pointerEvents: 'none',
          zIndex: '2147483647', transform: 'translate(-50%, -50%)', transition: 'left 80ms linear, top 80ms linear',
        })
        document.documentElement.appendChild(c)
        window.addEventListener('mousemove', (e) => {
          c.style.left = `${e.clientX}px`
          c.style.top = `${e.clientY}px`
        }, true)
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install)
      else install()
    })
    await use(page)
  },
})
export { expect }

export function password(who: string): string {
  const lines = readFileSync(`${ROOT}.lab/demo-credentials.txt`, 'utf8').split('\n')
  for (const line of lines) {
    const [email, pw] = line.trim().split(/\s+/)
    if (email === `${who}@demo.invalid` && pw) return pw
  }
  throw new Error(`identité de démonstration absente : ${who}`)
}

/** Active un contrôle au clavier : survol (curseur visible), focus, puis touche. */
export async function press(loc: Locator, key = 'Enter'): Promise<void> {
  await loc.scrollIntoViewIfNeeded()
  await loc.hover()
  await loc.focus()
  await loc.press(key)
}

export async function typeInto(loc: Locator, text: string): Promise<void> {
  await loc.hover()
  await loc.focus()
  await loc.pressSequentially(text, { delay: 20 })
}

export async function login(page: Page, who: string): Promise<void> {
  await page.goto('/connexion')
  await typeInto(page.getByLabel('Adresse e-mail'), `${who}@demo.invalid`)
  await page.getByLabel('Mot de passe').fill(password(who))
  await page.getByLabel('Mot de passe').press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: /Bonjour/ })).toBeVisible()
}

export async function logout(page: Page): Promise<void> {
  await press(page.getByRole('button', { name: 'Se déconnecter' }))
  await expect(page).toHaveURL(/\/connexion/)
}

/** Aucune violation axe sérieuse ou critique (T30). */
export async function expectAccessible(page: Page, where: string): Promise<void> {
  const res = await new AxeBuilder({ page }).exclude('#__pw_cursor').analyze()
  const bad = res.violations.filter(v => v.impact === 'serious' || v.impact === 'critical')
  expect(bad.map(v => `${v.id} (${v.impact}) : ${v.help} — ${v.nodes.map(n => n.target.join(' ')).join(' | ')}`),
    `axe sur ${where}`).toEqual([])
}

/** Appel direct à l'API avec la session du navigateur (comme un client qui contournerait l'UI). */
export async function api(page: Page, method: 'GET' | 'POST' | 'PUT', path: string, data?: unknown) {
  const origin = new URL(page.url()).origin
  const session = await (await page.request.get(`${origin}/api/v1/session`)).json()
  const r = await page.request.fetch(`${origin}/api/v1${path}`, {
    method,
    data,
    headers: { 'X-CSRF-Token': session.csrf_token, Origin: origin, 'Idempotency-Key': randomUUID() },
  })
  return { status: r.status(), body: await r.json().catch(() => null) }
}

export function compose(...args: string[]): string {
  const files = ['compose/compose.yaml', 'compose/compose.demo.yaml']
  if (process.env.RACKLOOM_COMPOSE_EXTRA) files.push(process.env.RACKLOOM_COMPOSE_EXTRA)
  return execFileSync('docker', ['compose', ...files.flatMap(f => ['-f', f.startsWith('/') ? f : `${ROOT}${f}`]), ...args],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
}

export function inDays(days: number): string {
  const d = new Date(Date.now() + days * 86_400_000)
  return d.toLocaleDateString('sv-SE', { timeZone: 'Europe/Paris' })
}

export async function setAssistant(page: Page, adapter: string, scenario = 'nominal'): Promise<void> {
  const r = await api(page, 'PUT', '/admin/assistant', { adapter, scenario })
  expect(r.status, JSON.stringify(r.body)).toBe(200)
}

/** Étape 1 au clavier par le formulaire seul : service, date de fin ; le reste vient des défauts. */
export async function describeByForm(page: Page, days = 14): Promise<void> {
  await press(page.getByRole('radio', { name: /Serveur web Ubuntu/ }), 'Space')
  const until = page.getByLabel(/Date de fin/)
  await until.hover()
  await until.fill(inDays(days))
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('review')).toBeVisible()
}

export async function waitState(page: Page, label: string | RegExp, timeout = 180_000): Promise<void> {
  await expect(page.getByTestId('change-state')).toHaveText(label, { timeout })
}
