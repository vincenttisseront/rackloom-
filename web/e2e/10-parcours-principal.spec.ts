import { D1, expect, expectAccessible, inDays, login, logout, press, test, typeInto, waitState } from './helpers'

// Contrôles de saisie interdits à l'étape 1 (T31) : rien qui ressemble à un réglage d'infrastructure.
const FORBIDDEN = /vcpu|\bcpu\b|\bram\b|mémoire|disque|réseau|network|vlan|\bip\b|adresse ip|datastore|cluster|image|template vsphere|runner/i

test('§1.1 parcours principal au clavier — D1, T30, T31', async ({ page }) => {
  test.setTimeout(600_000)

  // 2. Alice n'a qu'un projet : Rackloom ne lui demande pas de le choisir.
  await login(page, 'alice')
  await expectAccessible(page, 'accueil (Alice)')
  const newRequest = page.getByRole('link', { name: 'Nouvelle demande', exact: true })
  await expect(newRequest).toHaveCount(1)
  await press(newRequest)
  await expect(page.getByRole('heading', { name: 'Votre besoin' })).toBeVisible()

  // T31 : aucun contrôle d'infrastructure dans le DOM de l'étape 1.
  const controls = await page.locator('input, select, textarea').evaluateAll(els => els.map((e) => {
    const id = e.getAttribute('id') || ''
    const label = id ? document.querySelector(`label[for="${id}"]`)?.textContent || '' : ''
    return `${id} ${e.getAttribute('name') || ''} ${label} ${e.getAttribute('aria-label') || ''}`.replace(/\s+/g, ' ').trim()
  }))
  expect(controls.length).toBeGreaterThan(0)
  expect(controls.filter(c => FORBIDDEN.test(c))).toEqual([])
  await expectAccessible(page, 'étape 1')

  // 3-4. L'assistant (fictif, badgé) reconnaît le service et pose une seule question : la date de fin.
  await expect(page.getByTestId('assistant').getByText('Assistant fictif')).toBeVisible()
  await typeInto(page.getByLabel("Message pour l'assistant"), D1)
  await page.keyboard.press('Enter')
  const reply = page.locator('.bubble.bot').last()
  await expect(reply.locator('li')).toHaveCount(1)
  await expect(reply.locator('li')).toContainText(/date/i)
  const apply = page.getByRole('button', { name: 'Reporter dans le formulaire' })
  await expect(apply.first()).toBeVisible()
  while (await apply.count()) await press(apply.first())

  // 5. Les propositions entrent dans le formulaire marquées « proposé » ; Alice confirme et répond.
  const form = page.getByTestId('need-form')
  await expect(form.getByText("Proposé par l'assistant").first()).toBeVisible()
  await expect(page.getByTestId('unconfirmed')).toBeVisible()
  const confirm = form.getByRole('button', { name: /^Confirmer/ })
  while (await confirm.count()) await press(confirm.first())
  await expect(page.getByTestId('unconfirmed')).toHaveCount(0)
  const until = page.getByLabel(/Date de fin/)
  await until.hover()
  await until.fill(inDays(14))
  const counted = Number((await form.getByText(/choix de votre part/).innerText()).split(' ')[0])
  expect(counted).toBeLessThanOrEqual(3)

  // 6. Étape 2 « Vérifier et soumettre ».
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('summary-headline')).toContainText('1 serveur web Ubuntu 24.04 avec Nginx dans Vitrine / dev')
  await expect(page.getByTestId('review')).toContainText('un approbateur de Vitrine autre que vous')
  await expect(page.getByTestId('review')).toContainText('paquet nginx installé, service nginx actif, page HTTP 200')

  // 7. « Détails du plan » en lecture seule, avec provenance, fraîcheur de l'inventaire et étapes simulées.
  const details = page.getByTestId('plan-details')
  await press(details.locator('summary'))
  await expect(details).toHaveAttribute('open', '')
  await expect(details.locator('input, select, textarea, button, [contenteditable="true"]')).toHaveCount(0)
  await expect(details).toContainText(/vit-dev-web-\d\d/)
  await expect(details.locator('.badge.sim').first()).toBeVisible()
  await expectAccessible(page, 'étape 2')
  await press(page.getByTestId('submit'))

  // 8. Étape 3 « Suivre le résultat » : chronologie en direct jusqu'au plan prêt.
  await expect(page).toHaveURL(/\/demandes\//)
  await waitState(page, 'Plan prêt')
  await expect(page.getByTestId('plan-headline')).toContainText('1 VM de labo à créer, 0 modification, 0 suppression, 0 remplacement')
  await expect(page.getByTestId('timeline')).not.toBeEmpty()

  // 9. Alice n'a aucun bouton d'approbation.
  await expect(page.getByTestId('awaiting-approval')).toBeVisible()
  await expect(page.getByTestId('approve')).toHaveCount(0)
  await expectAccessible(page, 'étape 3 (Alice)')
  const ref = (await page.locator('.crumbs').innerText()).split('›').pop()!.trim()
  const digest = (await page.getByTestId('bundle-digest').innerText()).trim()
  expect(digest).toMatch(/^[0-9a-f]{64}$/)

  // Bruno ouvre « Plan et approbation » et approuve l'empreinte affichée.
  await logout(page)
  await login(page, 'bruno')
  await press(page.getByRole('link', { name: new RegExp(ref) }).first())
  await expect(page.getByTestId('plan-panel')).toBeVisible()
  await expect(page.getByTestId('bundle-digest')).toHaveText(digest)
  await expectAccessible(page, 'plan et approbation (Bruno)')
  await press(page.getByTestId('approve'))
  await expect(page.getByText('Plan approuvé : exécution du plan approuvé.')).toBeVisible()

  // 10-11. Exécution du plan approuvé, Ansible, vérification : « Succès » seulement si le service est prêt.
  await expect(page.getByTestId('service-result')).toContainText('Succès — service prêt', { timeout: 300_000 })
  await waitState(page, 'Succès')
  const guest = page.getByTestId('job').filter({ hasText: 'Personnalisation invitée' })
  await expect(guest.locator('.badge.sim').first()).toBeVisible()
  await expectAccessible(page, 'résultat (Bruno)')

  // Fiche machine : OS, état sain, version de Nginx.
  await logout(page)
  await login(page, 'alice')
  await press(page.getByRole('link', { name: new RegExp(ref) }).first())
  await expect(page.getByTestId('service-result')).toContainText('Succès — service prêt')
  const hostLink = page.getByTestId('hosts').getByRole('link', { name: /vit-dev-web-\d\d/ })
  const hostName = (await hostLink.innerText()).trim()
  await press(hostLink)
  await expect(page.getByTestId('host-health')).toHaveText('Sain')
  await expect(page.getByTestId('host-os')).toContainText('Ubuntu')
  await expect(page.getByTestId('host-package').first()).not.toBeEmpty()
  await expectAccessible(page, 'fiche machine')
  await page.goBack()

  // 12. « Modifier ce déploiement » : le niveau Standard remplacerait la VM (bloquant, dit à l'étape 2),
  // la quantité 2 ajoute une VM sans toucher à la première.
  await press(page.getByTestId('modify'))
  await expect(page.getByRole('heading', { name: 'Modifier ce déploiement' })).toBeVisible()
  const tier = page.getByLabel('Niveau')
  await tier.focus()
  await tier.selectOption('standard')
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('risk-destructive')).toContainText(`remplacerait ${hostName}`)
  await press(page.getByRole('button', { name: 'Modifier le besoin' }))
  await tier.focus()
  await tier.selectOption('test_leger')
  const quantity = page.getByLabel('Nombre de serveurs')
  await quantity.hover()
  await quantity.fill('2')
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('risk-destructive')).toHaveCount(0)
  await expect(page.getByTestId('summary-headline')).toContainText('2 serveurs web Ubuntu')
  await expect(page.getByTestId('review').locator('dd').filter({ hasText: hostName })).toHaveCount(1)
  await press(page.getByTestId('submit'))
  await waitState(page, 'Plan prêt')
  await expect(page.getByTestId('plan-headline')).toContainText('1 VM de labo à créer, 0 modification, 0 suppression, 0 remplacement')

  // 14. David (auditeur) : décisions tracées, chaîne d'audit intègre.
  await logout(page)
  await login(page, 'david')
  await press(page.getByRole('link', { name: 'Audit' }))
  await expect(page.getByTestId('audit-chain')).toHaveClass(/ok/)
  await expect(page.locator('table')).toContainText('approval.approved')
  await expect(page.locator('table')).toContainText('change.version.create')
  await expectAccessible(page, 'audit (David)')
})
