<script setup lang="ts">
// Administration › Bundles hors ligne (F17). Aucune action ne crée de job.
const api = useApi()
const bundles = ref<any[]>([])
const selected = ref<any>(null)
const trustKeys = ref<any[]>([])
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const lastPackageB64 = ref<string | null>(null)

const STATUS: Record<string, string> = {
  inspected: 'Inspecté',
  imported: 'Importé',
  rejected: 'Refusé',
}
const KIND_LABEL: Record<string, string> = {
  image: 'Image',
  dependency: 'Dépendance',
  terraform_provider: 'Provider Terraform',
  ansible_collection: 'Collection Ansible',
  model: 'Modèle',
}

async function load() {
  try {
    bundles.value = await api.get('/admin/offline-bundles')
    trustKeys.value = await api.get('/admin/offline-bundles/trust-keys')
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

async function open(id: string) {
  error.value = null
  notice.value = null
  selected.value = await api.get(`/admin/offline-bundles/${id}`)
}

async function buildDemo() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const built = await api.post('/admin/offline-bundles/build-demo', {
      slug: 'lab-offline',
      version: '1.0.0',
      title: 'Lot de démo hors ligne',
    })
    lastPackageB64.value = built.package_b64
    const inspected = await api.post('/admin/offline-bundles/inspect', {
      package_b64: built.package_b64,
    })
    notice.value = `Lot inspecté (${inspected.items.length} items). Contenu exact ci-dessous. Aucun job.`
    await load()
    await open(inspected.id)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function doImport() {
  if (!selected.value) return
  busy.value = true
  error.value = null
  try {
    const result = await api.post('/admin/offline-bundles/import', {
      bundle_id: selected.value.id,
    })
    notice.value = `Importé hors ligne : ${result.items.length} items. Aucun job créé.`
    selected.value = result
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <header>
      <h1>Administration › Bundles hors ligne</h1>
      <p class="lead">
        Lots signés pour sites déconnectés (images, dépendances, providers, collections, modèles).
        Intégrité et compatibilité vérifiées avant import. Un lot altéré est refusé.
      </p>
    </header>

    <p v-if="error" class="error" role="alert" data-testid="bundle-error">{{ error }}</p>
    <p v-if="notice" class="notice" role="status" data-testid="bundle-notice">{{ notice }}</p>

    <section class="panel">
      <h2>Actions</h2>
      <button
        type="button"
        data-testid="bundle-build-demo"
        :disabled="busy"
        @click="buildDemo"
      >
        Construire et inspecter un lot de démo
      </button>
      <p class="hint">
        Charges utiles fictives signées localement. N’installe rien tant que l’import n’est pas confirmé.
      </p>
    </section>

    <section class="panel">
      <h2>Clés de confiance</h2>
      <ul v-if="trustKeys.length" data-testid="bundle-trust-keys">
        <li v-for="k in trustKeys" :key="k.key_id">
          <code>{{ k.key_id }}</code> — {{ k.label }}
          <span v-if="k.active"> (active)</span>
        </li>
      </ul>
      <p v-else class="muted">Aucune clé enregistrée.</p>
    </section>

    <section class="panel">
      <h2>Lots</h2>
      <ul v-if="bundles.length" class="list" data-testid="bundle-list">
        <li v-for="b in bundles" :key="b.id">
          <button
            type="button"
            class="linkish"
            :data-testid="`open-bundle-${b.slug}-${b.version}`"
            @click="open(b.id)"
          >
            {{ b.slug }}@{{ b.version }} — {{ STATUS[b.status] || b.status }}
          </button>
        </li>
      </ul>
      <p v-else class="muted">Aucun lot inspecté.</p>
    </section>

    <section v-if="selected" class="panel" data-testid="bundle-detail">
      <h2>Contenu exact</h2>
      <p>
        <strong>{{ selected.slug }}@{{ selected.version }}</strong>
        — {{ selected.title }}
      </p>
      <dl class="meta">
        <dt>État</dt>
        <dd data-testid="bundle-status">{{ STATUS[selected.status] || selected.status }}</dd>
        <dt>Empreinte</dt>
        <dd><code data-testid="bundle-digest">{{ selected.content_digest }}</code></dd>
        <dt>Signataire</dt>
        <dd><code>{{ selected.signer_key_id }}</code></dd>
        <dt>Compatibilité</dt>
        <dd>
          appliance {{ selected.appliance_compat.min_version }}–{{ selected.appliance_compat.max_version }}
          / {{ selected.appliance_compat.os }} / {{ selected.appliance_compat.arch }}
        </dd>
      </dl>

      <h3>Items</h3>
      <table data-testid="bundle-items">
        <thead>
          <tr>
            <th>Type</th>
            <th>Nom</th>
            <th>Version</th>
            <th>Taille</th>
            <th>Empreinte</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="i in selected.items" :key="`${i.kind}-${i.name}-${i.version}`">
            <td>{{ KIND_LABEL[i.kind] || i.kind }}</td>
            <td>{{ i.name }}</td>
            <td>{{ i.version }}</td>
            <td>{{ i.size_bytes }} o</td>
            <td><code>{{ i.digest.slice(0, 12) }}…</code></td>
          </tr>
        </tbody>
      </table>

      <button
        v-if="selected.actions?.includes('import') || selected.status === 'inspected'"
        type="button"
        data-testid="bundle-import"
        :disabled="busy"
        @click="doImport"
      >
        Importer hors ligne
      </button>
      <p class="hint">{{ selected.notice }}</p>
    </section>
  </div>
</template>

<style scoped>
.page { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
.lead { color: var(--muted, #444); margin-bottom: 1rem; }
.panel { margin: 1.25rem 0; padding: 0.85rem 0; border-top: 1px solid #ddd; }
.list { list-style: none; padding: 0; margin: 0; }
.linkish {
  background: none; border: none; color: inherit; text-decoration: underline;
  cursor: pointer; padding: 0.25rem 0; font: inherit; text-align: left;
}
.error { color: #8b0000; }
.notice { color: #1a4d1a; }
.muted, .hint { color: var(--muted, #666); font-size: 0.95rem; }
.meta { display: grid; grid-template-columns: 8rem 1fr; gap: 0.35rem 0.75rem; }
.meta dt { font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin: 0.75rem 0 1rem; font-size: 0.95rem; }
th, td { text-align: left; padding: 0.35rem 0.4rem; border-bottom: 1px solid #eee; }
code { font-size: 0.85em; word-break: break-all; }
button[type="button"]:not(.linkish) {
  padding: 0.45rem 0.85rem; cursor: pointer;
}
</style>
