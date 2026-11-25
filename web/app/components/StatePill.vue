<script setup lang="ts">
const props = defineProps<{ state: string; label: string; serviceStatus?: string | null }>()
// Aucun faux succès : « Succès » seulement si le service est vérifié prêt.
const tone = computed(() => {
  if (props.state === 'succeeded' && props.serviceStatus && props.serviceStatus !== 'ready') return 'warn'
  return CHANGE_TONE[props.state] || 'neutral'
})
const text = computed(() => {
  if (props.state === 'succeeded' && props.serviceStatus && props.serviceStatus !== 'ready') return 'Terminé, service non prêt'
  return props.label
})
</script>

<template>
  <span class="badge" :class="tone" data-testid="change-state">{{ text }}</span>
</template>
