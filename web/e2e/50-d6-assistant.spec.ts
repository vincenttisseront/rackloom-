import {
  D1, LOCKED, approveAsBruno, api, ciSubmit, describeByForm, expect, expectAccessible, login, logout, need, press,
  setAssistant, test, typeInto, waitState, type Page,
} from './helpers'

/** Parcours D1 par le seul formulaire, jusqu'au succès, avec la même approbation. */
async function formJourney(page: Page): Promise<string> {
  await describeByForm(page)
  await expect(page.getByTestId('summary-headline')).toContainText('1 serveur web Ubuntu 24.04 avec Nginx dans Vitrine / dev')
  await press(page.getByTestId('submit'))
  await waitState(page, 'Plan prêt')
  const ref = (await page.locator('.crumbs').innerText()).split('›').pop()!.trim()
  const cid = page.url().split('/').pop()!
  await approveAsBruno(page, ref)
  await expect(page.getByTestId('service-result')).toContainText('Succès — service prêt', { timeout: 300_000 })
  await logout(page)
  return cid
}

/** Mêmes refus que D2 (API, client CI) et D4 (production non confirmée), sans assistant. */
async function sameRefusals(page: Page, succeeded: string): Promise<void> {
  const vcpu = (await api(page, 'GET', `/changes/${succeeded}`)).body.revision.details.groups
    .flatMap((g: any) => g.values).find((v: any) => v.pointer === '/instances/0/vcpu').value
  const draft = (await api(page, 'POST', '/projects/vitrine/changes', { title: 'D6 : refus sans assistant' })).body.id
  let r = await api(page, 'POST', `/changes/${draft}/versions`, { need: need({}, { vcpu }), base_version_no: 0 })
  expect([r.status, r.body?.code, r.body?.message]).toEqual([422, 'locked_field', LOCKED])
  r = await api(page, 'POST', `/changes/${draft}/versions`, { need: need({ environment: 'prod' }), base_version_no: 0 })
  expect([r.status, r.body?.code]).toEqual([422, 'production_not_confirmed'])
  expect((await api(page, 'GET', `/changes/${draft}`)).body.current_version_no).toBe(0)
  const ci = ciSubmit('alice', 'vitrine', need({ network: 'net-dev-vitrine-a' }))
  expect([ci.code, ci.out.includes('"locked_field"')]).toEqual([1, true])
}

// D6 (T36) : assistant désactivé puis indisponible → bandeau clair, parcours formulaire complet jusqu'au
// succès, mêmes refus et même approbation.
test('D6 — assistant désactivé puis indisponible : le formulaire suffit (T36)', async ({ page }) => {
  test.setTimeout(900_000)
  try {
    // L'administrateur désactive l'assistant depuis Administration › Connecteurs.
    await login(page, 'admin')
    await page.goto('/admin/connecteurs')
    const card = page.getByTestId('assistant-admin')
    await card.getByLabel('Adaptateur').selectOption('disabled')
    await press(card.getByTestId('assistant-save'))
    await expect.poll(async () => (await api(page, 'GET', '/admin/assistant')).body.adapter).toBe('disabled')
    await logout(page)

    await login(page, 'alice')
    await press(page.getByRole('link', { name: 'Nouvelle demande', exact: true }))
    await expect(page.getByTestId('assistant-unavailable')).toContainText("L'assistant est désactivé")
    await expect(page.getByTestId('assistant-unavailable')).toContainText("les contrôles et l'approbation sont les mêmes")
    await expect(page.getByLabel("Message pour l'assistant")).toHaveCount(0)
    await expectAccessible(page, 'D6 assistant désactivé')
    const first = await formJourney(page)

    // Assistant en panne (scénario unavailable) : le bandeau apparaît au premier message.
    await setAssistant('fake', 'unavailable')
    await login(page, 'alice')
    await press(page.getByRole('link', { name: 'Nouvelle demande', exact: true }))
    await typeInto(page.getByLabel("Message pour l'assistant"), D1)
    await page.keyboard.press('Enter')
    await expect(page.getByTestId('assistant-unavailable').first()).toContainText("L'assistant est indisponible")
    await expect(page.getByRole('button', { name: 'Reporter dans le formulaire' })).toHaveCount(0)
    await formJourney(page)

    await login(page, 'alice')
    await sameRefusals(page, first)
  } finally {
    await setAssistant('fake', 'nominal')
  }
})
