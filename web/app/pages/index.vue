<script setup lang="ts">
const api = useApi()
const session = useSession()
const projects = ref<any[]>([])
const status = ref<any>(null)
const recent = ref<Record<string, any[]>>({})
const inbox = ref<any>(null)
const examples = ref<any>(null)
const error = ref<string | null>(null)
const isAdmin = computed(() => hasRole(session.value, 'platform_admin'))
const isAuthor = computed(() => hasRole(session.value, 'template_author'))
const ROLE_LABELS: Record<string, string> = { operator: 'Opérateur', approver: 'Approbateur', auditor: 'Auditeur' }

onMounted(async () => {
  try {
    inbox.value = await api.get('/inbox')
    examples.value = await api.get('/examples')
    projects.value = await api.get('/projects')
    for (const p of projects.value) {
      recent.value[p.slug] = (await api.get(`/projects/${p.slug}/changes`)).slice(0, 5)
    }
    if (isAdmin.value) status.value = await api.get('/platform/status')
  } catch (e) {
    error.value = errorText(e)
  }
})
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Bonjour {{ session?.user.display_name }}</h1>
        <p class="muted">Décrivez un besoin ; Rackloom choisit la technique, vous suivez le résultat.</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>

    <InboxBox :inbox="inbox" />

    <RequestExamples
      :examples="examples?.examples || []"
      :notice="examples?.notice"
    />

    <section v-if="status" class="card" aria-labelledby="health-title" data-testid="platform-health">
      <h2 id="health-title">Santé de la plateforme</h2>
      <dl class="kv">
        <dt>Profil</dt><dd>{{ status.profile }}</dd>
        <dt>Workers actifs</dt><dd>{{ status.workers_alive }}</dd>
        <dt v-if="status.execution_zones?.length">Zones d'exécution</dt>
        <dd v-if="status.execution_zones?.length">
          {{ status.execution_zones.map((z: any) => `${z.slug} (${z.runners} runner${z.runners === 1 ? '' : 's'})`).join(' · ') }}
        </dd>
        <dt>Jobs</dt><dd>{{ Object.entries(status.jobs).map(([k, v]) => `${JOB_STATES[k] || k} : ${v}`).join(' · ') || 'aucun' }}</dd>
        <dt>Inventaire</dt>
        <dd>observé le {{ dateTime(status.inventory?.observed_at) }}
          <span v-if="status.inventory?.stale" class="badge warn">Données anciennes</span>
          <SimBadge v-if="status.inventory?.connector_mode === 'simulated'" /></dd>
        <dt>Assistant</dt><dd>{{ status.assistant.adapter }} <SimBadge v-if="status.assistant.adapter === 'fake'" label="Assistant fictif" /></dd>
        <dt>Audit</dt><dd><span class="badge" :class="status.audit.ok ? 'ok' : 'bad'">{{ status.audit.ok ? 'chaîne intègre' : 'chaîne rompue' }}</span> ({{ status.audit.checked }} entrées)</dd>
      </dl>
      <p v-if="status.stuck_jobs?.length" class="banner warn">Jobs au bail expiré : {{ status.stuck_jobs.length }}</p>
    </section>

    <p v-if="!projects.length && !error" class="empty">
      Aucun projet ne vous est attribué. Les rôles d'administration, de publication et de conception ne donnent accès à aucune donnée de projet.
      <template v-if="isAuthor"> L'atelier de templates est dans la navigation.</template>
    </p>
    <div class="grid-2">
      <section v-for="p in projects" :key="p.slug" class="card" :aria-labelledby="`p-${p.slug}`">
        <div class="field-head">
          <h2 :id="`p-${p.slug}`"><NuxtLink :to="`/projets/${p.slug}`">{{ p.name }}</NuxtLink></h2>
          <span class="small muted">{{ p.roles.map((r: string) => ROLE_LABELS[r] || r).join(', ') }}</span>
        </div>
        <p class="small muted">Référent : {{ p.referent }}</p>
        <ul v-if="recent[p.slug]?.length" class="small">
          <li v-for="c in recent[p.slug]" :key="c.id">
            <NuxtLink :to="`/demandes/${c.id}`">{{ c.ref }} · {{ c.title }}</NuxtLink>
            <StatePill :state="c.state" :label="c.state_label" />
          </li>
        </ul>
        <p v-else class="empty small">Aucune demande.</p>
        <NuxtLink v-if="p.roles.includes('operator')" class="button primary" :to="`/projets/${p.slug}/nouvelle`">Nouvelle demande</NuxtLink>
      </section>
    </div>
  </div>
</template>
