<script setup lang="ts">
const props = defineProps<{ current: 1 | 2 | 3 }>()
const steps = ['Décrire le besoin', 'Vérifier et soumettre', 'Suivre le résultat']
</script>

<template>
  <ol class="steps" aria-label="Étapes de la demande">
    <li
      v-for="(label, i) in steps"
      :key="label"
      :class="{ current: i + 1 === props.current, done: i + 1 < props.current }"
      :aria-current="i + 1 === props.current ? 'step' : undefined"
    >
      <span class="num" aria-hidden="true">{{ i + 1 < props.current ? '✓' : i + 1 }}</span>
      <span>{{ label }}</span>
    </li>
  </ol>
</template>
