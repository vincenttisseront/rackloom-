import {
  approveAsBruno, expect, expectAccessible, inDays, login, logout, press, session, test, typeInto, waitState,
} from './helpers'

// Après 90-d5 : ubuntu-web 1.0.0 peut être retirée. La base est la dernière version publiée,
// lue sur le serveur, jamais figée dans le scénario.

test('AT01 — atelier, publication distincte, deux machines par vagues', async ({ page }) => {
  test.setTimeout(900_000)
  const ines = await session('ines')
  const bases = (await ines.call('GET', '/studio/bases')).body as { slug: string; version: string }[]
  const base = bases.find(b => b.slug === 'ubuntu-web')
  expect(base, JSON.stringify(bases)).toBeTruthy()

  await login(page, 'ines')
  await press(page.getByRole('link', { name: 'Atelier' }))
  await expect(page.getByRole('heading', { level: 1, name: 'Atelier de templates' })).toBeVisible()
  await typeInto(page.getByTestId('draft-slug'), 'ubuntu-web-durci')
  await page.getByTestId('draft-base').selectOption(`${base!.slug}@${base!.version}`)
  await typeInto(
    page.getByTestId('draft-brief'),
    "Je veux un serveur web Ubuntu durci, avec Fail2ban sur SSH, déployé une machine d'abord",
  )
  await press(page.getByTestId('draft-create'))
  await expect(page.getByTestId('design')).toBeVisible()
  // Chaque acceptation re-rend la liste : on cible l'identifiant figé, puis on attend sa disparition.
  for (let i = 0; i < 12; i++) {
    const next = page.locator('[data-testid^="accept-"]').first()
    if ((await next.count()) === 0) break
    const testId = await next.getAttribute('data-testid')
    expect(testId).toBeTruthy()
    await press(page.getByTestId(testId!))
    await expect(page.getByTestId(testId!)).toHaveCount(0)
    await expect(page.getByTestId('error')).toHaveCount(0)
  }
  await expect(page.locator('[data-testid^="accept-"]')).toHaveCount(0)
  await expect(page.getByTestId('waves-label')).toContainText('pilote')
  await expectAccessible(page, 'AT01 atelier')
  await press(page.getByTestId('submit-draft'))
  await expect(page.getByTestId('assistant-reply')).toContainText('soumise')

  await logout(page)
  await login(page, 'alice')
  await page.goto('/atelier')
  await expect(page.getByTestId('error')).toBeVisible()

  await logout(page)
  await login(page, 'pub')
  await page.goto('/admin/templates')
  await press(page.getByTestId('open-ubuntu-web-durci-1.0.0'))
  await expect(page.getByTestId('review-origin')).toBeVisible()
  await expect(page.getByTestId('separation-notice')).toHaveCount(0)
  await page.getByLabel('Motif (obligatoire)').fill('AT01 : publication du profil durci')
  await press(page.getByTestId('action-publish'))
  await expect(page.getByTestId('error')).toHaveCount(0)
  await expect(page.getByText('Publier : fait')).toBeVisible()

  await logout(page)
  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await press(page.locator('#svc-ubuntu-web-durci'), 'Space')
  await page.locator('#f-quantity').fill('2')
  await page.getByLabel(/Date de fin/).fill(inDays(14))
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('summary-waves')).toContainText(/pilote/)
  await expect(page.getByTestId('summary-headline')).toContainText('Fail2ban')
  await press(page.getByTestId('submit'))
  await waitState(page, 'Plan prêt')
  const ref = (await page.locator('.crumbs').innerText()).split('›').pop()!.trim()
  await expect(page.getByTestId('plan-waves')).toContainText('pilote')
  await approveAsBruno(page, ref)
  await expect(page.getByTestId('service-result')).toContainText('Succès — service prêt', { timeout: 600_000 })
  await ines.dispose()
})
