import { expect, expectAccessible, login, press, session, test, typeInto } from './helpers'

test('F60 — partir d\'un serveur de référence (observations fictives)', async ({ page }) => {
  test.setTimeout(180_000)
  const ines = await session('ines')
  const bases = (await ines.call('GET', '/studio/bases')).body as { slug: string; version: string }[]
  const base = bases.find(b => b.slug === 'ubuntu-web')
  expect(base, JSON.stringify(bases)).toBeTruthy()
  const sources = (await ines.call('GET', '/studio/reference-sources')).body as {
    fixtures: { id: string; simulated: boolean }[]
  }
  expect(sources.fixtures.some(f => f.simulated)).toBeTruthy()

  await login(page, 'ines')
  await press(page.getByRole('link', { name: 'Atelier' }))
  await expect(page.getByRole('heading', { level: 1, name: 'Atelier de templates' })).toBeVisible()
  await typeInto(page.getByTestId('draft-slug'), 'ubuntu-web-depuis-ref')
  await page.getByTestId('draft-base').selectOption(`${base!.slug}@${base!.version}`)
  await press(page.getByTestId('mode-reference'))
  await expect(page.getByTestId('reference-picker')).toBeVisible()
  await expect(page.getByText(/Observations fictives/i).first()).toBeVisible()
  await press(page.getByTestId('draft-create'))

  await expect(page.getByTestId('reference')).toBeVisible()
  await expect(page.getByTestId('layout-warning')).toContainText('/var/www')
  await expect(page.getByTestId('ref-cat-standardize')).toBeVisible()
  await expect(page.getByTestId('ref-cat-host_specific')).toBeVisible()
  await expect(page.getByTestId('ref-cat-confirm')).toBeVisible()
  await expectAccessible(page, 'F60 référence')

  for (let i = 0; i < 24; i++) {
    const next = page.locator('[data-testid^="ref-accept-"]').first()
    if ((await next.count()) === 0) break
    const testId = await next.getAttribute('data-testid')
    expect(testId).toBeTruthy()
    await press(page.getByTestId(testId!))
    await expect(page.getByTestId(testId!)).toHaveCount(0)
    await expect(page.getByTestId('error')).toHaveCount(0)
  }
  await expect(page.locator('[data-testid^="ref-accept-"]')).toHaveCount(0)
  await press(page.getByTestId('submit-draft'))
  await expect(page.getByTestId('assistant-reply')).toContainText('soumise')
  await ines.dispose()
})
