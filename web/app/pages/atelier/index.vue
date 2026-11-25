<script setup lang="ts">
const api = useApi()
const drafts = ref<any[]>([])
const bases = ref<any[]>([])
const slug = ref('')
const base = ref('')
const brief = ref('')
const error = ref<string | null>(null)
const busy = ref(false)

async function load() {
  drafts.value = await api.get('/studio/drafts')
  bases.value = await api.get('/studio/bases')
  if (!base.value && bases.value.length) base.value = `${bases.value[0].slug}@${bases.value[0].version}`
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
  try {
    const created = await api.post('/studio/drafts', { slug: slug.value.trim(), base: { slug: bslug, version }, brief: brief.value })
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
        <p class="muted small">Décrivez le service. L'assistant propose ; vous décidez. Publier est une autre personne, et ne déploie rien.</p>
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
            <p class="hint">Minuscules, chiffres et tirets. Exemple : ubuntu-web-durci.</p>
          </div>
          <div class="field">
            <label for="draft-base">Partir de</label>
            <select id="draft-base" v-model="base" required data-testid="draft-base">
              <option v-for="b in bases" :key="`${b.slug}@${b.version}`" :value="`${b.slug}@${b.version}`">{{ b.title }} {{ b.version }}</option>
            </select>
            <p class="hint">Placement, réseau, tailles et nommage sont hérités. L'atelier ne les modifie pas.</p>
          </div>
          <div class="field">
            <label for="draft-brief">Ce que le service doit faire</label>
            <textarea id="draft-brief" v-model="brief" maxlength="2000" rows="4" data-testid="draft-brief" />
          </div>
          <button type="submit" class="primary" :disabled="busy || !slug.trim() || !base" data-testid="draft-create">Créer le brouillon</button>
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
