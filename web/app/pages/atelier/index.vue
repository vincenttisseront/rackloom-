<script setup lang="ts">
const api = useApi()
const drafts = ref<any[]>([])
const bases = ref<any[]>([])
const sources = ref<any>({ fixtures: [], hosts: [] })
const mode = ref<'brief' | 'reference'>('brief')
const slug = ref('')
const base = ref('')
const brief = ref('')
const fixtureId = ref('')
const error = ref<string | null>(null)
const busy = ref(false)

async function load() {
  drafts.value = await api.get('/studio/drafts')
  bases.value = await api.get('/studio/bases')
  sources.value = await api.get('/studio/reference-sources')
  if (!base.value && bases.value.length) base.value = `${bases.value[0].slug}@${bases.value[0].version}`
  if (!fixtureId.value && sources.value.fixtures?.length) fixtureId.value = sources.value.fixtures[0].id
}
onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function create() {
  busy.value = true
  error.value = null
  const [bslug, version] = base.value.split('@')
  const body: Record<string, unknown> = { slug: slug.value.trim(), base: { slug: bslug, version } }
  if (mode.value === 'brief') body.brief = brief.value
  else body.reference = { fixture: fixtureId.value }
  try {
    const created = await api.post('/studio/drafts', body)
    await navigateTo(`/atelier/${created.draft.id}`)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Atelier de templates</h1>
        <p class="muted small">Décrivez le service ou partez d'un serveur de référence. L'assistant propose ; vous décidez. Publier est une autre personne, et ne déploie rien.</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div class="grid-main">
      <section class="card" aria-labelledby="new-title">
        <h2 id="new-title">Nouveau brouillon</h2>
        <form @submit.prevent="create">
          <div class="field">
            <label for="draft-slug">Identifiant</label>
            <input id="draft-slug" v-model="slug" required pattern="[a-z][a-z0-9-]{1,40}" maxlength="41" data-testid="draft-slug" />
            <p class="hint">Minuscules, chiffres et tirets. Exemple : ubuntu-web-ref.</p>
          </div>
          <div class="field">
            <label for="draft-base">Partir de</label>
            <select id="draft-base" v-model="base" required data-testid="draft-base">
              <option v-for="b in bases" :key="`${b.slug}@${b.version}`" :value="`${b.slug}@${b.version}`">{{ b.title }} {{ b.version }}</option>
            </select>
            <p class="hint">Placement, réseau, tailles et nommage sont hérités. L'atelier ne les modifie pas.</p>
          </div>
          <fieldset class="field" data-testid="draft-mode">
            <legend>Comment commencer</legend>
            <label class="small"><input v-model="mode" type="radio" value="brief" /> Brief pour l'assistant</label>
            <label class="small"><input v-model="mode" type="radio" value="reference" data-testid="mode-reference" /> Partir d'un serveur existant</label>
          </fieldset>
          <div v-if="mode === 'brief'" class="field">
            <label for="draft-brief">Ce que le service doit faire</label>
            <textarea id="draft-brief" v-model="brief" maxlength="2000" rows="4" data-testid="draft-brief" />
          </div>
          <div v-else class="field" data-testid="reference-picker">
            <label for="draft-fixture">Serveur de référence</label>
            <select id="draft-fixture" v-model="fixtureId" required data-testid="draft-fixture">
              <option v-for="f in sources.fixtures" :key="f.id" :value="f.id">{{ f.label }}</option>
            </select>
            <p class="hint">
              <SimBadge v-if="sources.fixtures?.[0]?.simulated" label="Observations fictives" title="Collecteur réel indisponible : observations fictives explicites (D21)." />
              Lecture seule, champs en liste blanche. Aucun job d'écriture. Les identifiants d'hôte ne sont jamais recopiés.
            </p>
            <p v-if="!sources.hosts?.length" class="hint muted">Aucun hôte enrôlé dans votre périmètre. La fixture fictive reste disponible.</p>
          </div>
          <button type="submit" class="primary" :disabled="busy || !slug.trim() || !base || (mode === 'reference' && !fixtureId)" data-testid="draft-create">Créer le brouillon</button>
        </form>
      </section>
      <section class="card" aria-labelledby="list-title">
        <h2 id="list-title">Brouillons</h2>
        <p v-if="!drafts.length" class="empty">Aucun brouillon.</p>
        <ul v-else class="small">
          <li v-for="d in drafts" :key="d.id">
            <NuxtLink :to="`/atelier/${d.id}`">{{ d.title }}</NuxtLink>
            <span class="muted"> {{ d.slug }} {{ d.version }} · {{ d.status_label }} · {{ d.author }}</span>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>
