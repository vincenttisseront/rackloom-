<script setup lang="ts">
defineProps<{ title?: string; label?: string }>()
</script>

<template>
  <span class="badge sim" :title="title || 'Étape simulée : aucune infrastructure réelle n\'est touchée'">
    {{ label || 'Simulé' }}
  </span>
</template>
