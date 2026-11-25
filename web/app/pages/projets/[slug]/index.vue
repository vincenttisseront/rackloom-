<script setup lang="ts">
const route = useRoute()
const api = useApi()
const slug = computed(() => String(route.params.slug))
const project = ref<any>(null)
const changes = ref<any[]>([])
const deployments = ref<any[]>([])
const hosts = ref<any[]>([])
const environments = ref<any[]>([])
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    const projects = await api.get('/projects')
    project.value = projects.find((p: any) => p.slug === slug.value)
    if (!project.value) throw new ApiError(404, { message: 'Projet introuvable.' })
    ;[changes.value, deployments.value, hosts.value, environments.value] = await Promise.all([
      api.get(`/projects/${slug.value}/changes`), api.get(`/projects/${slug.value}/deployments`), api.get(`/projects/${slug.value}/hosts`),
      api.get(`/projects/${slug.value}/environments`),
    ])
  } catch (e) {
    error.value = errorText(e)
  }
})

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
      <NuxtLink v-if="project?.roles.includes('operator')" class="button primary" :to="`/projets/${slug}/nouvelle`" data-testid="new-request">Nouvelle demande</NuxtLink>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
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
