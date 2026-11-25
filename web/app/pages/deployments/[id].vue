<script setup lang="ts">
const route = useRoute()
const api = useApi()
const error = ref<string | null>(null)
const deployment = ref<any>(null)

onMounted(async () => {
  try {
    deployment.value = await api.get(`/deployments/${route.params.id}`)
  } catch (e) {
    error.value = errorText(e)
  }
})
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
    <ServiceCard v-if="deployment?.service_card" :card="deployment.service_card" />
    <p v-else-if="!error" class="empty">Chargement…</p>
  </div>
</template>
