<script setup lang="ts">
const api = useApi()
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const graph = ref<any>(null)
const impactKind = ref('network')
const impactId = ref('')
const impact = ref<any>(null)

const kinds = [
  { id: 'network', label: 'Réseau' },
  { id: 'datastore', label: 'Datastore' },
  { id: 'cluster', label: 'Cluster' },
  { id: 'template', label: 'Template' },
  { id: 'certificate', label: 'Certificat' },
]

async function loadGraph() {
  graph.value = await api.get('/dependency-graph')
}

async function rebuild() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const res = await api.post('/dependency-graph/rebuild', {})
    notice.value = `Graphe reconstruit : ${res.node_count} nœuds, ${res.edge_count} arêtes.`
    await loadGraph()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function loadImpact() {
  error.value = null
  impact.value = null
  if (!impactId.value.trim()) {
    error.value = 'Indiquez l’identifiant stable de la ressource.'
    return
  }
  busy.value = true
  try {
    impact.value = await api.get(
      `/dependency-graph/impact/${impactKind.value}/${encodeURIComponent(impactId.value.trim())}`,
    )
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    await loadGraph()
    if (!graph.value?.build) {
      await rebuild()
    }
  } catch (e) {
    error.value = errorText(e)
  }
})

const networks = computed(() =>
  (graph.value?.nodes || []).filter((n: any) => n.kind === 'network'),
)
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Graphe de dépendances</h1>
        <p class="muted small">
          Relations confirmées, inférées ou inconnues entre VM, templates, réseaux, datastores,
          certificats et services. Hors de votre périmètre : masqué (jamais révélé).
        </p>
      </div>
      <button type="button" class="button" data-testid="graph-rebuild" :disabled="busy" @click="rebuild">
        Reconstruire
      </button>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status">{{ notice }}</div>

    <section class="card" aria-labelledby="impact-title" data-testid="impact-panel">
      <h2 id="impact-title">Vue d’impact</h2>
      <p class="small muted">Quelles VM dépendent de cette ressource, dans vos projets uniquement.</p>
      <div class="row" style="display:flex; gap:0.5rem; flex-wrap:wrap; align-items:end">
        <label>
          Type
          <select v-model="impactKind" data-testid="impact-kind">
            <option v-for="k in kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
          </select>
        </label>
        <label style="flex:1; min-width:12rem">
          Identifiant stable
          <input v-model="impactId" list="graph-networks" data-testid="impact-stable-id" />
          <datalist id="graph-networks">
            <option v-for="n in networks" :key="n.stable_id" :value="n.stable_id">{{ n.name }}</option>
          </datalist>
        </label>
        <button type="button" class="button primary" data-testid="impact-submit" :disabled="busy" @click="loadImpact">
          Afficher
        </button>
      </div>
      <div v-if="impact" style="margin-top:1rem" data-testid="impact-result">
        <p>
          <strong>{{ impact.resource.name }}</strong>
          <span class="mono small"> · {{ impact.resource.kind }} · {{ impact.resource.stable_id }}</span>
        </p>
        <p v-if="impact.notice" class="small muted" data-testid="impact-hidden">{{ impact.notice }}</p>
        <ul>
          <li v-for="d in impact.dependents" :key="d.stable_id" data-testid="impact-dependent">
            {{ d.name }}
            <span class="badge">{{ d.provenance }}</span>
            <span class="small muted"> · {{ d.relation }}</span>
            <NuxtLink v-if="d.host_id" class="small" :to="`/machines/${d.host_id}`"> fiche</NuxtLink>
          </li>
        </ul>
        <p v-if="!impact.dependents?.length" class="small muted">Aucune VM visible dans votre périmètre.</p>
      </div>
    </section>

    <section class="card" style="margin-top:1rem" aria-labelledby="graph-title" data-testid="graph-summary">
      <h2 id="graph-title">Instantané</h2>
      <p v-if="!graph?.build" class="small muted">Aucun graphe construit.</p>
      <template v-else>
        <p class="small">
          {{ graph.build.node_count }} nœuds · {{ graph.build.edge_count }} arêtes ·
          construit le {{ dateTime(graph.build.built_at) }}
        </p>
        <p class="small muted">
          Provenances affichées côté arêtes : confirmée (révision / hôte), inférée (inventaire),
          inconnue.
        </p>
      </template>
    </section>
  </div>
</template>
