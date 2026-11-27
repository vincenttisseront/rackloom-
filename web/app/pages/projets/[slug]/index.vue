<script setup lang="ts">
const route = useRoute()
const api = useApi()
const slug = computed(() => String(route.params.slug))
const project = ref<any>(null)
const changes = ref<any[]>([])
const deployments = ref<any[]>([])
const hosts = ref<any[]>([])
const environments = ref<any[]>([])
const preferences = ref<any | null>(null)
const prefForm = ref({ default_size_tier: 'standard', naming: '', reason: '' })
const prefBusy = ref(false)
const prefNotice = ref<string | null>(null)
const consumption = ref<any | null>(null)
const patchCampaigns = ref<any[]>([])
const error = ref<string | null>(null)

const KIND_LABEL: Record<string, string> = {
  allocation: 'Allocation',
  measurement: 'Mesure',
  estimation: 'Estimation',
}

const canPublishPrefs = computed(() => {
  // Référent ou admin : l'API refuse sinon (F50).
  return Boolean(project.value)
})

onMounted(async () => {
  try {
    const projects = await api.get('/projects')
    project.value = projects.find((p: any) => p.slug === slug.value)
    if (!project.value) throw new ApiError(404, { message: 'Projet introuvable.' })
    ;[changes.value, deployments.value, hosts.value, environments.value] = await Promise.all([
      api.get(`/projects/${slug.value}/changes`), api.get(`/projects/${slug.value}/deployments`), api.get(`/projects/${slug.value}/hosts`),
      api.get(`/projects/${slug.value}/environments`),
    ])
    try {
      consumption.value = await api.get(`/projects/${slug.value}/consumption`)
    } catch {
      consumption.value = null
    }
    try {
      patchCampaigns.value = await api.get(`/projects/${slug.value}/patch-campaigns`)
    } catch {
      patchCampaigns.value = []
    }
    try {
      preferences.value = await api.get(`/projects/${slug.value}/preferences`)
      const cur = preferences.value?.current?.preferences || {}
      if (cur.default_size_tier) prefForm.value.default_size_tier = cur.default_size_tier
      if (cur.naming) prefForm.value.naming = cur.naming
    } catch {
      preferences.value = null
    }
  } catch (e) {
    error.value = errorText(e)
  }
})

async function publishPreferences() {
  prefBusy.value = true
  prefNotice.value = null
  error.value = null
  try {
    const preferencesBody: Record<string, string> = {
      default_size_tier: prefForm.value.default_size_tier,
    }
    if (prefForm.value.naming.trim()) preferencesBody.naming = prefForm.value.naming.trim()
    await api.post(`/projects/${slug.value}/preferences`, {
      preferences: preferencesBody,
      reason: prefForm.value.reason || 'Préférence de projet',
    })
    preferences.value = await api.get(`/projects/${slug.value}/preferences`)
    prefNotice.value = `Préférence v${preferences.value.current.version} publiée. Les plans déjà approuvés restent inchangés.`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    prefBusy.value = false
  }
}

function envChanged(updated: any) {
  environments.value = environments.value.map(e => (e.id === updated.id ? { ...e, ...updated } : e))
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <div class="crumbs"><NuxtLink to="/">Accueil</NuxtLink> › Projet</div>
        <h1>{{ project?.name || slug }}</h1>
        <p v-if="project" class="small muted">Code {{ project.code }} · référent {{ project.referent }}</p>
      </div>
      <div class="actions">
        <NuxtLink
          v-if="project?.roles.includes('operator')"
          class="button"
          :to="`/projets/${slug}/adoption`"
          data-testid="open-adoption"
        >Adoption de VM</NuxtLink>
        <NuxtLink v-if="project?.roles.includes('operator')" class="button primary" :to="`/projets/${slug}/nouvelle`" data-testid="new-request">Nouvelle demande</NuxtLink>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-for="e in environments.filter(x => x.frozen)" :key="`frozen-${e.id}`" class="banner warn" role="status" data-testid="project-freeze-banner">
      Environnement {{ e.name }} gelé : aucun job d'écriture ne démarre jusqu'au dégel. Lecture et diagnostic restent disponibles.
    </div>
    <div
      v-for="e in environments.filter(x => x.calendar?.state?.code === 'calendar_frozen')"
      :key="`cal-frozen-${e.id}`" class="banner warn" role="status" data-testid="project-calendar-banner"
    >
      Environnement {{ e.name }} : {{ e.calendar.state.reason }}. Les jobs d'écriture ne démarrent pas pendant cette période.
    </div>

    <section v-if="environments.length" class="card" aria-labelledby="envs-title">
      <h2 id="envs-title">Environnements</h2>
      <EnvironmentTable :environments="environments" @changed="envChanged" />
      <CalendarPanel
        v-for="e in environments" :key="`cal-${e.id}`" :environment="e" @changed="envChanged"
      />
    </section>

    <section v-if="canPublishPrefs" class="card" aria-labelledby="prefs-title" data-testid="project-preferences">
      <h2 id="prefs-title">Préférences de projet</h2>
      <p class="small muted">
        Publiées par le référent. Elles simplifient le prochain déploiement autorisé
        (provenance <code>project_preference</code>) sans modifier un plan existant ni créer de droit.
        Une conversation ne mémorise jamais une préférence active.
      </p>
      <div v-if="prefNotice" class="banner ok" role="status">{{ prefNotice }}</div>
      <p v-if="preferences?.current" class="small" data-testid="preference-current">
        Version active {{ preferences.current.version }} :
        {{ JSON.stringify(preferences.current.preferences) }}
      </p>
      <p v-else class="small muted">Aucune préférence publiée.</p>
      <form class="actions" style="flex-wrap: wrap" @submit.prevent="publishPreferences">
        <div class="field">
          <label for="pref-tier">Niveau par défaut</label>
          <select id="pref-tier" v-model="prefForm.default_size_tier" data-testid="pref-tier">
            <option value="test_leger">Test léger</option>
            <option value="standard">Standard</option>
            <option value="charge_elevee">Charge élevée</option>
          </select>
        </div>
        <div class="field" style="flex: 1">
          <label for="pref-naming">Convention de nommage (optionnel)</label>
          <input id="pref-naming" v-model="prefForm.naming" type="text" placeholder="{project_code}-dev-web-{nn}" data-testid="pref-naming" />
        </div>
        <div class="field" style="flex: 1">
          <label for="pref-reason">Motif</label>
          <input id="pref-reason" v-model="prefForm.reason" type="text" required maxlength="500" data-testid="pref-reason" />
        </div>
        <button type="submit" class="primary" :disabled="prefBusy" data-testid="pref-publish">Publier</button>
      </form>
    </section>

    <section
      v-if="consumption"
      class="card"
      aria-labelledby="consumption-title"
      data-testid="consumption-panel"
    >
      <h2 id="consumption-title">Consommation de l'équipe</h2>
      <p class="small muted">{{ consumption.notice }}</p>
      <p v-if="consumption.simulated" class="small"><SimBadge title="Inventaire et mesures simulés" /></p>
      <table>
        <thead>
          <tr>
            <th scope="col">Ressource</th>
            <th scope="col">{{ KIND_LABEL.allocation }}</th>
            <th scope="col">{{ KIND_LABEL.measurement }}</th>
            <th scope="col">{{ KIND_LABEL.estimation }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in consumption.resources" :key="r.resource" :data-testid="`consumption-row-${r.resource}`">
            <td>{{ r.resource }} <span class="muted small">({{ r.unit }})</span></td>
            <td>{{ r.allocated.value }}</td>
            <td>{{ r.measured ? r.measured.value : '—' }}</td>
            <td>{{ r.estimated.value }}</td>
          </tr>
        </tbody>
      </table>
      <p
        class="small"
        data-testid="consumption-reconciliation"
        :class="consumption.inventory_reconciliation.ok ? 'ok' : 'muted'"
      >
        Inventaire : {{ consumption.inventory_reconciliation.notice }}
        <span v-if="consumption.inventory_reconciliation.ok"> (totaux alignés)</span>
      </p>
      <p
        v-if="!consumption.billing"
        class="banner warn"
        role="status"
        data-testid="consumption-no-billing"
      >
        Aucun montant affiché : données tarifaires absentes.
      </p>
      <p
        v-else
        class="small"
        data-testid="consumption-billing"
      >
        Estimation tarifaire :
        {{ (consumption.billing.total_minor / 100).toFixed(2) }}
        {{ consumption.billing.currency }} / {{ consumption.billing.period }}
        <span class="badge">{{ KIND_LABEL.estimation }}</span>
      </p>
    </section>

    <section
      class="card"
      aria-labelledby="patch-title"
      data-testid="patch-campaigns-panel"
    >
      <h2 id="patch-title">Campagnes de patch</h2>
      <p class="small muted">
        Pilote puis vagues ; une dégradation bloque la suite sans retour arrière automatique.
      </p>
      <p v-if="!patchCampaigns.length" class="empty" data-testid="patch-campaigns-empty">
        Aucune campagne.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th scope="col">Titre</th>
            <th scope="col">État</th>
            <th scope="col">Vagues</th>
            <th scope="col">Preuve</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="c in patchCampaigns"
            :key="c.id"
            :data-testid="`patch-campaign-${c.id}`"
          >
            <td>
              <NuxtLink :to="`/demandes/${c.change_id}`">{{ c.title }}</NuxtLink>
            </td>
            <td>
              <span class="badge">{{ c.state_label || c.state }}</span>
              <span v-if="c.block_reason" class="small muted"> — {{ c.block_reason }}</span>
            </td>
            <td>{{ (c.waves || []).length }}</td>
            <td>
              <SimBadge v-if="c.simulated" title="Patch simulé" />
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="deps-title">
      <h2 id="deps-title">Déploiements</h2>
      <p v-if="!deployments.length" class="empty">Aucun déploiement.</p>
      <table v-else>
        <thead><tr><th scope="col">Service</th><th scope="col">Environnement</th><th scope="col">Machines</th><th scope="col">État du service</th></tr></thead>
        <tbody>
          <tr v-for="d in deployments" :key="d.id">
            <td><NuxtLink :to="`/deployments/${d.id}`" data-testid="open-service-card">{{ d.template.title }} {{ d.template.version }}</NuxtLink></td>
            <td>{{ d.environment.name }} <span v-if="d.environment.is_production" class="badge warn">Production</span></td>
            <td>{{ d.instances.map((i: any) => i.name).join(', ') }}</td>
            <td><span class="badge" :class="SERVICE_TONE[d.service_status]">{{ d.service_status_label }}</span> <span class="small muted">{{ d.service_reason }}</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="changes-title">
      <h2 id="changes-title">Demandes</h2>
      <p v-if="!changes.length" class="empty">Aucune demande.</p>
      <table v-else>
        <thead><tr><th scope="col">Réf.</th><th scope="col">Titre</th><th scope="col">Environnement</th><th scope="col">État</th><th scope="col">Mise à jour</th></tr></thead>
        <tbody>
          <tr v-for="c in changes" :key="c.id">
            <td><NuxtLink :to="`/demandes/${c.id}`">{{ c.ref }}</NuxtLink></td>
            <td>{{ c.title }}<span v-if="c.kind === 'modify'" class="muted small"> (modification)</span></td>
            <td>{{ c.environment?.name || '—' }}</td>
            <td><StatePill :state="c.state" :label="c.state_label" /></td>
            <td class="small">{{ dateTime(c.updated_at) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="hosts-title">
      <h2 id="hosts-title">Machines</h2>
      <p v-if="!hosts.length" class="empty">Aucune machine.</p>
      <ul v-else>
        <li v-for="h in hosts" :key="h.id">
          <NuxtLink :to="`/machines/${h.id}`">{{ h.name }}</NuxtLink>
          <span class="badge" :class="h.health.state === 'healthy' ? 'ok' : h.health.state === 'error' ? 'bad' : 'warn'">{{ h.health.label }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
