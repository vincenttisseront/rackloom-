<script setup lang="ts">
const props = defineProps<{ source?: string | null; label?: string | null }>()
const LABELS: Record<string, string> = {
  user: 'Vous',
  suggestion: "Proposé par l'assistant",
  template_default: 'Défaut du profil',
  environment_default: 'Seul environnement disponible',
  deployment_pinned: 'Version du déploiement',
}
const text = computed(() => props.label || (props.source ? LABELS[props.source] || props.source : 'Défaut du profil'))
</script>

<template>
  <span class="badge prov" :class="{ suggestion: source === 'suggestion' }">
    <span class="sr-only">Provenance : </span>{{ text }}
  </span>
</template>
