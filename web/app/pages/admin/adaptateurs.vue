<script setup lang="ts">
const api = useApi()
const packages = ref<any[]>([])
const trust = ref<any[]>([])
const contract = ref<any | null>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const selected = ref<any | null>(null)

async function load() {
  const listed = await api.get('/admin/adapters')
  packages.value = listed.packages || []
  trust.value = listed.trust_digests || []
  contract.value = await api.get('/admin/adapters/contract')
  if (selected.value) {
    const id = selected.value.id
    selected.value = packages.value.find((p: any) => p.id === id) || null
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function submitDemo() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const row = await api.post('/admin/adapters/submit', { demo_reference: true })
    notice.value = `Paquet ${row.slug}@${row.version} soumis (digest ${row.content_digest.slice(0, 12)}…). Aucune installation.`
    await load()
    selected.value = row
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function approve() {
  if (!selected.value) return
  busy.value = true
  error.value = null
  notice.value = null
  try {
    selected.value = await api.post(`/admin/adapters/${selected.value.id}/approve`, {
      reason: 'Revue F30 depuis l’interface',
    })
    notice.value = 'Paquet approuvé. L’installation reste une action distincte.'
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function install() {
  if (!selected.value) return
  busy.value = true
  error.value = null
  notice.value = null
  try {
    selected.value = await api.post(`/admin/adapters/${selected.value.id}/install`)
    notice.value = `Connecteur installé (${selected.value.installed_connector_id}).`
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function reject() {
  if (!selected.value) return
  busy.value = true
  error.value = null
  notice.value = null
  try {
    selected.value = await api.post(`/admin/adapters/${selected.value.id}/reject`, {
      reason: 'Rejeté depuis l’interface admin',
    })
    notice.value = 'Paquet rejeté.'
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

const statusLabel: Record<string, string> = {
  submitted: 'soumis',
  approved: 'approuvé',
  installed: 'installé',
  rejected: 'rejeté',
}
</script>

<template>
  <div>
    <div class="page-head"><h1>Administration › Adaptateurs</h1></div>
    <p class="lead">
      SDK F30 : soumission, approbation et installation d’un paquet d’adaptateur.
      Aucune installation sans approbation ni contrôle de provenance.
      Guide : <code>docs/adapter-sdk.md</code>.
    </p>
    <div v-if="error" class="banner bad" role="alert" data-testid="adapters-error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="adapters-notice">{{ notice }}</div>

    <section class="card" aria-labelledby="contract-title" data-testid="adapters-contract">
      <h2 id="contract-title">Suite de contrat</h2>
      <p v-if="contract" class="small">
        <span
          class="badge"
          :class="contract.ok ? 'ok' : 'bad'"
          data-testid="contract-verdict"
        >{{ contract.ok ? 'verte' : 'rouge' }}</span>
        <span class="muted"> · {{ contract.reports?.length || 0 }} adaptateurs</span>
      </p>
    </section>

    <section class="card" aria-labelledby="packages-title">
      <div class="field-head">
        <h2 id="packages-title">Paquets</h2>
        <button type="button" class="primary" :disabled="busy" data-testid="adapters-submit-demo" @click="submitDemo">
          Soumettre l’adaptateur de référence
        </button>
      </div>
      <p class="small muted">Soumettre n’installe rien. L’approbateur doit être distinct du soumetteur.</p>
      <table data-testid="adapters-table">
        <thead>
          <tr>
            <th scope="col">Paquet</th>
            <th scope="col">Kind</th>
            <th scope="col">Statut</th>
            <th scope="col">Digest</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="p in packages"
            :key="p.id"
            :data-testid="`adapter-${p.slug}-${p.version}`"
            :class="{ selected: selected?.id === p.id }"
            tabindex="0"
            @click="selected = p"
            @keydown.enter="selected = p"
          >
            <td><strong>{{ p.slug }}</strong> @ {{ p.version }}</td>
            <td>{{ p.kind }}</td>
            <td><span class="badge" :class="p.status === 'installed' ? 'ok' : 'neutral'">{{ statusLabel[p.status] || p.status }}</span></td>
            <td class="small muted">{{ p.content_digest?.slice(0, 12) }}…</td>
          </tr>
          <tr v-if="!packages.length">
            <td colspan="4" class="muted">Aucun paquet soumis.</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="selected" class="card" aria-labelledby="detail-title" data-testid="adapter-detail">
      <h2 id="detail-title">{{ selected.title }}</h2>
      <p class="small muted" data-testid="adapter-digest">{{ selected.content_digest }}</p>
      <p class="small">Statut : <strong data-testid="adapter-status">{{ statusLabel[selected.status] || selected.status }}</strong></p>
      <div class="actions">
        <button
          v-if="selected.actions?.includes('approve')"
          type="button"
          class="primary"
          :disabled="busy"
          data-testid="adapter-approve"
          @click="approve"
        >
          Approuver
        </button>
        <button
          v-if="selected.actions?.includes('install')"
          type="button"
          class="primary"
          :disabled="busy"
          data-testid="adapter-install"
          @click="install"
        >
          Installer
        </button>
        <button
          v-if="selected.actions?.includes('reject')"
          type="button"
          :disabled="busy"
          data-testid="adapter-reject"
          @click="reject"
        >
          Rejeter
        </button>
      </div>
      <p v-if="selected.status === 'submitted'" class="small muted">
        L’installation n’est pas proposée tant que le paquet n’est pas approuvé.
      </p>
    </section>

    <section class="card" aria-labelledby="trust-title">
      <h2 id="trust-title">Digests de confiance</h2>
      <ul data-testid="trust-digests">
        <li v-for="d in trust" :key="d.id" class="small">
          {{ d.label }} — <code>{{ d.content_digest.slice(0, 16) }}…</code>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.lead { color: var(--muted, #444); margin-bottom: 1rem; max-width: 40rem; }
.selected td { background: #f3f3f3; }
.actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
</style>
