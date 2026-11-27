<script setup lang="ts">
const api = useApi()
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const campaigns = ref<any[]>([])
const campaign = ref<any | null>(null)
const destinations = ref<any[]>([])
const tlsAlerts = ref<any[]>([])
const scimToken = ref<string | null>(null)
const busy = ref(false)

async function load() {
  error.value = null
  try {
    campaigns.value = await api.get('/admin/access-reviews') as any[]
    destinations.value = await api.get('/admin/audit-exports') as any[]
    tlsAlerts.value = await api.get('/admin/tls-certificates/alerts') as any[]
    if (campaigns.value[0]) {
      campaign.value = await api.get(`/admin/access-reviews/${campaigns.value[0].id}`)
    }
  } catch (e) {
    error.value = errorText(e)
  }
}

onMounted(load)

async function prepareReview() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    campaign.value = await api.post('/admin/access-reviews', {
      title: `Revue ${new Date().toISOString().slice(0, 10)}`,
      note: 'Campagne préparée depuis l’administration',
    })
    campaigns.value = await api.get('/admin/access-reviews') as any[]
    notice.value = `Campagne préparée : ${campaign.value.item_count} élément(s).`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function decide(item: any, decision: 'confirmed' | 'revoked') {
  busy.value = true
  error.value = null
  try {
    await api.post(`/access-review-items/${item.id}/decide`, { decision, note: decision === 'revoked' ? 'révoqué en revue' : 'confirmé en revue' })
    if (campaign.value?.id) {
      campaign.value = await api.get(`/admin/access-reviews/${campaign.value.id}`)
    }
    notice.value = decision === 'revoked' ? 'Révocation enregistrée (preuve d’audit).' : 'Confirmation enregistrée.'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function mintScim() {
  busy.value = true
  error.value = null
  try {
    const r = await api.post('/admin/scim/tokens', { label: 'annuaire-labo' }) as any
    scimToken.value = r.token
    notice.value = 'Jeton SCIM créé (affiché une seule fois).'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function createExport() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const slug = `audit-${Date.now().toString(36).slice(-6)}`
    await api.post('/admin/audit-exports', { slug, relative_path: slug, object_lock: true })
    destinations.value = await api.get('/admin/audit-exports') as any[]
    notice.value = 'Destination d’export créée (verrouillage d’objets).'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function runExport(id: string) {
  busy.value = true
  error.value = null
  try {
    const r = await api.post(`/admin/audit-exports/${id}/run`, {}) as any
    notice.value = `Export : ${r.delivered} livré(s), ${r.already_present || 0} déjà présent(s).`
    destinations.value = await api.get('/admin/audit-exports') as any[]
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <h1>Identités et audit</h1>
    <p class="lead">
      SCIM (arrivées/départs), revue périodique des accès, copie d’audit hors appliance et alertes certificats TLS.
      Aucun secret ni clé privée n’est affiché.
    </p>
    <p v-if="error" class="error" role="alert" data-testid="error">{{ error }}</p>
    <p v-if="notice" class="notice" data-testid="notice">{{ notice }}</p>

    <section data-testid="scim-panel">
      <h2>SCIM</h2>
      <p>Client d’annuaire de test : jeton Bearer pour provisionner / désactiver des comptes.</p>
      <button type="button" data-testid="scim-mint" :disabled="busy" @click="mintScim">Créer un jeton SCIM</button>
      <p v-if="scimToken" class="token" data-testid="scim-token">Jeton (une fois) : {{ scimToken }}</p>
    </section>

    <section data-testid="review-panel">
      <h2>Revue d’accès</h2>
      <button type="button" data-testid="review-prepare" :disabled="busy" @click="prepareReview">Préparer une campagne</button>
      <div v-if="campaign" class="campaign" data-testid="review-campaign">
        <h3>{{ campaign.title }} ({{ campaign.item_count }} éléments visibles)</h3>
        <p v-if="!campaign.items?.length" data-testid="review-empty">Aucun élément dans votre périmètre.</p>
        <ul v-else data-testid="review-items">
          <li v-for="item in campaign.items" :key="item.id">
            <span>{{ item.project_slug }} — {{ item.kind }} — {{ item.summary }}</span>
            <span v-if="item.status === 'pending'" class="actions">
              <button type="button" :data-testid="`confirm-${item.id}`" :disabled="busy" @click="decide(item, 'confirmed')">Confirmer</button>
              <button type="button" :data-testid="`revoke-${item.id}`" :disabled="busy" @click="decide(item, 'revoked')">Révoquer</button>
            </span>
            <span v-else class="status">{{ item.status }}</span>
          </li>
        </ul>
      </div>
    </section>

    <section data-testid="export-panel">
      <h2>Copie d’audit</h2>
      <button type="button" data-testid="export-create" :disabled="busy" @click="createExport">Créer une destination verrouillée</button>
      <ul>
        <li v-for="d in destinations" :key="d.id">
          {{ d.slug }} — verrou {{ d.object_lock ? 'oui' : 'non' }} — curseur {{ d.last_audit_id }}
          <button type="button" :data-testid="`export-run-${d.id}`" :disabled="busy" @click="runExport(d.id)">Exporter</button>
        </li>
      </ul>
    </section>

    <section data-testid="tls-panel">
      <h2>Certificats TLS</h2>
      <p v-if="!tlsAlerts.length">Aucune alerte d’expiration pour l’instant.</p>
      <ul v-else>
        <li v-for="a in tlsAlerts" :key="a.id">
          {{ a.host }}:{{ a.port }} — {{ a.alert_status }} — propriétaire {{ a.owner_email || '—' }}
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
.lead { color: var(--muted, #444); margin-bottom: 1.5rem; }
section { margin: 1.75rem 0; padding-top: 0.5rem; border-top: 1px solid #ddd; }
h2 { font-size: 1.15rem; margin-bottom: 0.5rem; }
button { margin: 0.25rem 0.5rem 0.25rem 0; }
.token { font-family: ui-monospace, monospace; word-break: break-all; }
.error { color: #8b0000; }
.notice { color: #0b5; }
.actions { margin-left: 0.75rem; }
.status { margin-left: 0.75rem; font-style: italic; }
ul { padding-left: 1.2rem; }
li { margin: 0.4rem 0; }
</style>
