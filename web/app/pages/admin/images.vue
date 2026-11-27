<script setup lang="ts">
// Administration › Fabrique d'images (F07). Aucune action ne crée de job.
const api = useApi()
const builds = ref<any[]>([])
const selected = ref<any>(null)
const reason = ref('')
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)

const STATUS: Record<string, string> = {
  draft: 'Brouillon',
  built: 'Construite',
  ready: 'Prête',
  failed: 'Échec des tests',
  deprecated: 'Dépréciée',
}
const KIND_LABEL: Record<string, string> = { dev: 'dev', test: 'test', preprod: 'préprod', prod: 'prod' }

const form = reactive({
  slug: 'ubuntu-2404-std',
  version: '',
  title: 'Ubuntu 24.04 standard',
  source_kind: 'iso_digest',
  source_digest: '',
  os: 'ubuntu-24.04',
  layout_id: 'std-v1',
})

function actionLabel(a: string) {
  if (a === 'build') return 'Construire'
  if (a === 'test') return 'Lancer les tests'
  if (a === 'deprecate') return 'Déprécier'
  if (a.startsWith('promote:')) return `Promouvoir → ${KIND_LABEL[a.slice(8)] || a.slice(8)}`
  return a
}

async function load() {
  try {
    builds.value = await api.get('/admin/images')
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

async function open(slug: string, version: string) {
  error.value = null
  notice.value = null
  reason.value = ''
  selected.value = await api.get(`/admin/images/${slug}/versions/${version}`)
}

async function create() {
  busy.value = true
  error.value = null
  try {
    const digest = form.source_digest.trim()
      || 'a'.repeat(form.source_kind === 'git_commit' ? 40 : 64)
    const created = await api.post('/admin/images', {
      slug: form.slug,
      version: form.version,
      title: form.title,
      source_kind: form.source_kind,
      source_digest: digest,
      os: form.os,
      layout_id: form.layout_id,
    })
    notice.value = `Version ${created.version} enregistrée (brouillon). Aucun job créé.`
    await load()
    await open(created.slug, created.version)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function act(action: string) {
  if (!selected.value) return
  busy.value = true
  error.value = null
  try {
    const base = `/admin/images/${selected.value.slug}/versions/${selected.value.version}`
    if (action === 'build') {
      await api.post(`${base}/build`)
    } else if (action === 'test') {
      await api.post(`${base}/test`, {})
    } else if (action === 'deprecate') {
      await api.post(`${base}/deprecate`, { reason: reason.value })
    } else if (action.startsWith('promote:')) {
      await api.post(`${base}/promote`, {
        reason: reason.value,
        environment_kind: action.slice(8),
      })
    }
    notice.value = `${actionLabel(action)} : fait. Aucun job créé, aucun déploiement modifié.`
    await load()
    await open(selected.value.slug, selected.value.version)
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
        <h1>Administration › Fabrique d’images</h1>
        <p class="muted small">
          Pipeline local (F07) : provenance → construction → tests → promotion.
          Une image qui échoue à ses tests ne peut pas être promue en production.
          Déprécier conserve l’image tant qu’elle est nécessaire. Badge « Simulé » : outil réel non branché (D11).
        </p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="notice">{{ notice }}</div>

    <div class="grid-main">
      <div>
        <section class="card" aria-labelledby="img-list">
          <h2 id="img-list">Images</h2>
          <div v-if="!builds.length" class="muted small" data-testid="images-empty">Aucune version enregistrée.</div>
          <div v-for="b in builds" :key="b.slug">
            <h3>{{ b.title }} <span class="muted small mono">{{ b.slug }}</span></h3>
            <table>
              <thead>
                <tr>
                  <th scope="col">Version</th>
                  <th scope="col">Statut</th>
                  <th scope="col">Empreinte</th>
                  <th scope="col"></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="v in b.versions" :key="v.version">
                  <td>
                    {{ v.version }}
                    <span v-if="v.simulated" class="badge warn" style="margin-left: 0.4rem">Simulé</span>
                    <span v-if="v.deprecated" class="badge" style="margin-left: 0.4rem">dépréciée</span>
                  </td>
                  <td>{{ STATUS[v.status] || v.status }}</td>
                  <td class="mono small">{{ v.image_digest ? `${v.image_digest.slice(0, 12)}…` : '—' }}</td>
                  <td>
                    <button type="button" :data-testid="`open-${b.slug}-${v.version}`" @click="open(b.slug, v.version)">
                      Ouvrir
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="selected" class="card" aria-labelledby="img-detail" data-testid="image-version">
          <h2 id="img-detail">
            {{ selected.title }} {{ selected.version }}
            <span class="badge info">{{ STATUS[selected.status] }}</span>
            <span v-if="selected.simulated" class="badge warn">Simulé</span>
          </h2>
          <p class="small muted">{{ selected.notice }}</p>
          <h3>Provenance</h3>
          <ul class="small">
            <li>Source : <span class="mono">{{ selected.source_kind }}</span> · {{ selected.source_digest.slice(0, 16) }}…</li>
            <li>Recette : <span class="mono">{{ selected.recipe_digest.slice(0, 16) }}…</span></li>
            <li>Filtre : <span class="mono">os={{ selected.os }} layout_id={{ selected.layout_id }}</span></li>
            <li v-if="selected.stable_id">Identifiant stable : <span class="mono">{{ selected.stable_id }}</span></li>
          </ul>
          <h3>Tests</h3>
          <ul v-if="selected.tests.length" class="small" data-testid="image-tests">
            <li v-for="t in selected.tests" :key="t.name">
              <span :class="t.outcome === 'passed' ? 'ok' : 'bad'">{{ t.outcome }}</span>
              — {{ t.name }} <span class="muted">{{ t.detail }}</span>
            </li>
          </ul>
          <p v-else class="small muted">Aucun test exécuté.</p>
          <h3>Promotions</h3>
          <ul v-if="selected.promotions.length" class="small" data-testid="image-promotions">
            <li v-for="p in selected.promotions" :key="p.environment_kind">
              {{ KIND_LABEL[p.environment_kind] || p.environment_kind }} — {{ p.reason }}
            </li>
          </ul>
          <p v-else class="small muted">Aucune promotion.</p>
          <label class="block">
            Motif (obligatoire pour promouvoir / déprécier)
            <input v-model="reason" type="text" data-testid="image-reason" />
          </label>
          <div class="actions">
            <button
              v-for="a in selected.actions"
              :key="a"
              type="button"
              :disabled="busy"
              :data-testid="`action-${a}`"
              @click="act(a)"
            >
              {{ actionLabel(a) }}
            </button>
          </div>
        </section>
      </div>

      <aside>
        <section class="card" aria-labelledby="img-create">
          <h2 id="img-create">Nouvelle version</h2>
          <p class="small muted">Enregistre un brouillon. La construction et les tests sont simulés.</p>
          <label class="block">Slug <input v-model="form.slug" data-testid="create-slug" /></label>
          <label class="block">Version <input v-model="form.version" placeholder="1.1.0" data-testid="create-version" /></label>
          <label class="block">Titre <input v-model="form.title" data-testid="create-title" /></label>
          <label class="block">
            Source
            <select v-model="form.source_kind" data-testid="create-source-kind">
              <option value="iso_digest">ISO (SHA-256)</option>
              <option value="artifact_digest">Artefact (SHA-256)</option>
              <option value="git_commit">Commit Git</option>
            </select>
          </label>
          <label class="block">
            Empreinte source
            <input v-model="form.source_digest" class="mono" placeholder="auto labo si vide" data-testid="create-source-digest" />
          </label>
          <label class="block">OS <input v-model="form.os" data-testid="create-os" /></label>
          <label class="block">Layout <input v-model="form.layout_id" data-testid="create-layout" /></label>
          <button type="button" data-testid="create-image" :disabled="busy || !form.version" @click="create">
            Enregistrer le brouillon
          </button>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.ok { color: var(--ok, #0a7); font-weight: 600; }
.bad { color: var(--bad, #b00); font-weight: 600; }
.block { display: block; margin-bottom: 0.6rem; }
.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
</style>
