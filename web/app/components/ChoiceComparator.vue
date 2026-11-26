<script setup lang="ts">
/** F52 : comparateur de choix métier (≤3 options, critères vérifiés seulement). */
const props = defineProps<{
  comparison: any | null
}>()
const emit = defineEmits<{ pick: [patch: { path: string; value: any }] }>()

const show = computed(() => props.comparison?.mode === 'compare' && (props.comparison?.options?.length || 0) > 1)

function apply(option: any) {
  for (const p of option.need_patch || []) emit('pick', p)
}
</script>

<template>
  <section
    v-if="show"
    class="card"
    aria-labelledby="compare-title"
    data-testid="choice-comparator"
  >
    <h2 id="compare-title">Comparer les choix</h2>
    <p class="small muted" data-testid="compare-notice">{{ comparison.notice }}</p>
    <div class="grid-2" style="margin-top: 0.75rem">
      <article
        v-for="opt in comparison.options"
        :key="opt.key"
        class="field"
        data-testid="compare-option"
        :data-key="opt.key"
        :data-recommended="comparison.recommendation?.key === opt.key ? '1' : '0'"
      >
        <div class="field-head">
          <strong>{{ opt.label }}</strong>
          <span
            v-if="comparison.recommendation?.key === opt.key"
            class="badge ok"
            data-testid="compare-recommended"
          >Recommandé</span>
        </div>
        <p class="small">{{ opt.summary_headline }}</p>
        <p v-if="!Object.keys(opt.criteria || {}).length" class="small muted">
          Capacité, disponibilité et coût non affichés (données non vérifiées).
        </p>
        <button type="button" class="button" data-testid="compare-pick" @click="apply(opt)">
          Choisir
        </button>
      </article>
    </div>
    <p v-if="comparison.recommendation" class="small muted" data-testid="compare-rationale">
      {{ comparison.recommendation.rationale }}
    </p>
  </section>
</template>
