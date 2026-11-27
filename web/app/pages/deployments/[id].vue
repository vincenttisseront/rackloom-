<script setup lang="ts">
const route = useRoute()
const api = useApi()
const router = useRouter()
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const deployment = ref<any>(null)

async function load() {
  deployment.value = await api.get(`/deployments/${route.params.id}`)
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function onAction(actionId: string) {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const res = await api.post(`/deployments/${route.params.id}/actions/${actionId}`, {})
    const changeId = res.change?.id || res.change_id
    if (changeId) {
      await router.push(`/projets/${deployment.value?.service_card?.project?.slug || 'vitrine'}/nouvelle?change=${changeId}&etape=2`)
      return
    }
    notice.value = res.notice || 'Demande créée'
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function onRequestAccess(level: string) {
  busy.value = true
  error.value = null
  try {
    await api.post(`/deployments/${route.params.id}/access-requests`, {
      access_level: level,
      subject: 'self',
    })
    notice.value = 'Demande d\'accès envoyée — en attente d\'approbation.'
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function onApproveAccess(id: string) {
  busy.value = true
  error.value = null
  try {
    const res = await api.post(`/access-requests/${id}/approve`, {})
    notice.value = res.state === 'granted'
      ? 'Accès accordé (confirmé par le fournisseur d\'identité).'
      : res.state === 'sync_failed'
        ? `Échec de synchronisation : ${res.sync_error || 'réessayez'}`
        : `État : ${res.state_label}`
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function onRejectAccess(id: string) {
  busy.value = true
  error.value = null
  try {
    await api.post(`/access-requests/${id}/reject`, { comment: 'Refus depuis la fiche service' })
    notice.value = 'Demande d\'accès refusée.'
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function onRetryAccess(id: string) {
  busy.value = true
  error.value = null
  try {
    const res = await api.post(`/access-requests/${id}/retry-sync`, {})
    notice.value = res.state === 'granted'
      ? 'Accès accordé après relance (sans doublon).'
      : `État : ${res.state_label}${res.sync_error ? ' — ' + res.sync_error : ''}`
    await load()
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
        <div v-if="deployment" class="crumbs">
          <NuxtLink :to="`/projets/${deployment.service_card?.project?.slug || ''}`">
            {{ deployment.service_card?.project?.name || 'Projet' }}
          </NuxtLink>
          › Service
        </div>
        <h1>Fiche service</h1>
        <p class="muted small">État calculé depuis les contrôles de santé du template, pas d'après le succès d'un job.</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="notice">{{ notice }}</div>
    <ServiceCard
      v-if="deployment?.service_card"
      :card="deployment.service_card"
      @action="onAction"
      @request-access="onRequestAccess"
      @approve-access="onApproveAccess"
      @reject-access="onRejectAccess"
      @retry-access="onRetryAccess"
    />
    <p v-else-if="!error" class="empty">Chargement…</p>
    <p v-if="busy" class="small muted">Traitement…</p>
  </div>
</template>
