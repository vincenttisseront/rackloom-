import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F26 : interrupteur global dispatch_paused visible sur la santé plateforme ;
// les envois suspendus affichent un bandeau ; reprise explicite.

test('F26 — suspension des envois visible sur la santé, reprise explicite', async ({ page }) => {
  test.setTimeout(180_000)
  const admin = await session('admin')
  try {
    const pause = await admin.call('POST', '/admin/dispatch/pause', {
      reason: 'exercice e2e F26 : suspension des envois',
      source: 'maintenance',
    })
    expect(pause.status, JSON.stringify(pause.body)).toBe(200)
    expect(pause.body.paused).toBe(true)

    await login(page, 'admin')
    await expect(page.getByTestId('platform-health')).toBeVisible()
    await expect(page.getByTestId('dispatch-status')).toContainText('suspendus')
    await expect(page.getByTestId('dispatch-paused-banner')).toBeVisible()
    await expectAccessible(page)

    const resume = await admin.call('POST', '/admin/dispatch/resume', {
      reason: 'fin exercice e2e F26',
    })
    expect(resume.status, JSON.stringify(resume.body)).toBe(200)
    expect(resume.body.paused).toBe(false)

    await page.reload()
    await expect(page.getByTestId('platform-health')).toBeVisible()
    await expect(page.getByTestId('dispatch-status')).toContainText('actifs')
    await expect(page.getByTestId('dispatch-paused-banner')).toHaveCount(0)
    await logout(page)
  } finally {
    await admin.call('POST', '/admin/dispatch/resume', { reason: 'nettoyage e2e F26' }).catch(() => null)
    await admin.dispose()
  }
})
