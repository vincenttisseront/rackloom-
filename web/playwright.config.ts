import { defineConfig } from '@playwright/test'

// Parcours de bout en bout contre la pile Compose (scripts/test-e2e.sh). TLS vérifié : le navigateur
// fait confiance à la CA locale via une base NSS dédiée (RACKLOOM_E2E_HOME), l'API de test via
// NODE_EXTRA_CA_CERTS. Aucune option ne désactive la vérification des certificats.
const home = process.env.RACKLOOM_E2E_HOME
const size = { width: 1280, height: 800 }

export default defineConfig({
  testDir: './e2e',
  outputDir: process.env.RACKLOOM_E2E_OUTPUT || 'test-results',
  timeout: 300_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.RACKLOOM_E2E_URL || 'https://localhost:8443',
    locale: 'fr-FR',
    timezoneId: 'Europe/Paris',
    viewport: size,
    video: process.env.RACKLOOM_E2E_VIDEO === '1' ? { mode: 'on', size } : 'off',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: home ? { env: { ...process.env, HOME: home } } : {},
  },
})
