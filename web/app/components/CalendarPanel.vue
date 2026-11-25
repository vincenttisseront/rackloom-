<script setup lang="ts">
// Calendrier de changement (F27) : fuseau, fenêtres, gels planifiés. Le worker applique R090.
const props = defineProps<{ environment: any }>()
const emit = defineEmits<{ changed: [env: any] }>()
const api = useApi()

const canManage = computed(() => props.environment.allowed_actions?.some((a: any) => a.action === 'manage_calendar'))
const busy = ref(false)
const error = ref<string | null>(null)
const reason = ref('Gel planifié de démonstration (F27)')
const startLocal = ref('')
const endLocal = ref('')

onMounted(() => {
  const now = new Date()
  const later = new Date(now.getTime() + 2 * 3600_000)
  startLocal.value = toLocalInput(new Date(now.getTime() - 60_000))
  endLocal.value = toLocalInput(later)
})

function toLocalInput(d: Date) {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function toIso(local: string) {
  return new Date(local).toISOString()
}

function applyCalendar(cal: any) {
  const { timezone, state, windows, calendar_freezes } = cal
  emit('changed', { ...props.environment, calendar: { timezone, state, windows, calendar_freezes } })
}

async function addFreeze() {
  if (!canManage.value) return
  busy.value = true
  error.value = null
  try {
    applyCalendar(await api.post(`/environments/${props.environment.id}/calendar/freezes`, {
      starts_at: toIso(startLocal.value),
      ends_at: toIso(endLocal.value),
      reason: reason.value,
    }))
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function cancelFreeze(id: string) {
  busy.value = true
  error.value = null
  try {
    applyCalendar(await api.post(`/environments/${props.environment.id}/calendar/freezes/${id}/cancel`, {
      reason: 'Fin anticipée du gel planifié',
    }))
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="calendar-panel" :data-testid="`calendar-${environment.slug}`">
    <h3>Calendrier · {{ environment.name }}</h3>
    <p class="small muted">
      Fuseau {{ environment.calendar?.timezone || 'Europe/Paris' }}.
      <span data-testid="calendar-state">{{ environment.calendar?.state?.reason || '—' }}</span>
    </p>
    <ul v-if="environment.calendar?.windows?.length" class="small">
      <li v-for="w in environment.calendar.windows" :key="w.id">
        {{ w.weekdays_label }} {{ w.start_local }}–{{ w.end_local }}
        <span v-if="w.label" class="muted"> ({{ w.label }})</span>
      </li>
    </ul>
    <p v-else class="small muted">Aucune fenêtre : écritures toujours autorisées (hors gel).</p>

    <div v-if="environment.calendar?.calendar_freezes?.length" class="freeze-list">
      <div
        v-for="f in environment.calendar.calendar_freezes" :key="f.id"
        class="banner" :class="f.active ? 'warn' : ''" data-testid="calendar-freeze"
      >
        <strong v-if="f.active">Gel planifié actif</strong>
        <strong v-else>Gel planifié</strong>
        : {{ f.reason }}
        <span class="small"> ({{ dateTime(f.starts_at) }} → {{ dateTime(f.ends_at) }})</span>
        <button
          v-if="canManage && f.active" type="button" class="small"
          data-testid="calendar-freeze-cancel" :disabled="busy" @click="cancelFreeze(f.id)"
        >Annuler</button>
      </div>
    </div>

    <form v-if="canManage" class="calendar-form" @submit.prevent="addFreeze">
      <label>Ajouter une période de gel planifié</label>
      <div class="row">
        <label class="small">Début <input v-model="startLocal" type="datetime-local" required data-testid="calendar-freeze-start" /></label>
        <label class="small">Fin <input v-model="endLocal" type="datetime-local" required data-testid="calendar-freeze-end" /></label>
      </div>
      <label class="small">Motif
        <input v-model="reason" minlength="3" maxlength="500" required data-testid="calendar-freeze-reason" />
      </label>
      <button type="submit" class="danger" :disabled="busy || reason.trim().length < 3" data-testid="calendar-freeze-add">
        Planifier le gel
      </button>
      <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    </form>
  </div>
</template>

<style scoped>
.calendar-panel { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--border, #ddd); }
.calendar-form .row { display: flex; gap: 1rem; flex-wrap: wrap; }
.freeze-list { margin: 0.75rem 0; }
</style>
