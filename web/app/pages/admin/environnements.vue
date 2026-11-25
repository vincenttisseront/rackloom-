<script setup lang="ts">
const api = useApi()
const environments = ref<any[]>([])
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    environments.value = await api.get('/admin/environments')
  } catch (e) {
    error.value = errorText(e)
  }
})

function envChanged(updated: any) {
  environments.value = environments.value.map(e => (e.id === updated.id ? { ...e, ...updated } : e))
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Administration › Environnements</h1>
        <p class="small muted">Gel d'urgence : suspend le démarrage de tout job d'écriture d'un environnement, avec un motif audité.</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <section class="card" aria-label="Environnements de tous les projets">
      <p v-if="!environments.length && !error" class="empty">Aucun environnement.</p>
      <EnvironmentTable v-else :environments="environments" show-project @changed="envChanged" />
    </section>
  </div>
</template>
