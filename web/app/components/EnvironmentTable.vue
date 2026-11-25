<script setup lang="ts">
// Gel d'urgence (F28). Les boutons viennent de allowed_actions ; le serveur revérifie le droit à l'appel
// et le worker contrôle le gel au démarrage effectif de chaque job d'écriture.
const props = defineProps<{ environments: any[]; showProject?: boolean }>()
const emit = defineEmits<{ changed: [env: any] }>()
const api = useApi()

const openId = ref<string | null>(null)
const reason = ref('')
const busy = ref(false)
const error = ref<string | null>(null)

const toggle = (env: any) => env.allowed_actions?.find((a: any) => a.action === 'freeze' || a.action === 'unfreeze') || null

function open(env: any) {
  openId.value = env.id
  reason.value = ''
  error.value = null
  nextTick(() => document.getElementById(`freeze-reason-${env.id}`)?.focus())
}

async function submit(env: any) {
  const action = toggle(env)
  if (!action) return
  busy.value = true
  error.value = null
  try {
    const updated = await api.post(`/environments/${env.id}/${action.action}`, { reason: reason.value })
    openId.value = null
    emit('changed', updated)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <table data-testid="environments">
    <thead>
      <tr>
        <th scope="col">Environnement</th>
        <th scope="col">État</th>
        <th scope="col">Jobs d'écriture</th>
        <th scope="col"><span class="sr-only">Action</span></th>
      </tr>
    </thead>
    <tbody>
      <template v-for="env in props.environments" :key="env.id">
        <tr :data-testid="`env-${props.showProject ? `${env.project.slug}-` : ''}${env.slug}`">
          <td>
            <strong>{{ props.showProject ? `${env.project.name} / ` : '' }}{{ env.name }}</strong>
            <span v-if="env.is_production" class="badge warn">Production</span>
          </td>
          <td>
            <span class="badge" :class="env.frozen ? 'bad' : 'ok'" data-testid="env-state">{{ env.frozen ? 'Gelé' : 'Actif' }}</span>
            <div v-if="env.freeze" class="small" data-testid="env-freeze">
              depuis le {{ dateTime(env.freeze.frozen_at) }} par {{ env.freeze.frozen_by }} : « {{ env.freeze.reason }} »
            </div>
          </td>
          <td class="small">
            {{ env.write_jobs.queued }} en attente<span v-if="env.frozen && env.write_jobs.queued"> (ne démarreront pas)</span>
            · {{ env.write_jobs.dispatched }} déjà envoyé{{ env.write_jobs.dispatched > 1 ? 's' : '' }}<span v-if="env.frozen && env.write_jobs.dispatched"> (poursuivis, suivis)</span>
          </td>
          <td>
            <button
              v-if="toggle(env) && openId !== env.id" type="button" :class="toggle(env).action === 'freeze' ? 'danger' : 'primary'"
              :data-testid="`env-${toggle(env).action}`" @click="open(env)"
            >{{ toggle(env).label }}</button>
          </td>
        </tr>
        <tr v-if="openId === env.id">
          <td colspan="4">
            <form @submit.prevent="submit(env)">
              <label :for="`freeze-reason-${env.id}`">{{ toggle(env)?.action === 'freeze' ? 'Motif du gel' : 'Motif du dégel' }}</label>
              <textarea :id="`freeze-reason-${env.id}`" v-model="reason" minlength="3" maxlength="500" required data-testid="freeze-reason" />
              <p class="hint">
                <template v-if="toggle(env)?.action === 'freeze'">
                  Aucun job d'écriture de cet environnement ne démarrera pendant le gel, même déjà approuvé et en file.
                  Les jobs déjà envoyés au fournisseur continuent et leur état réel reste affiché. L'inventaire, la collecte,
                  la vérification et le diagnostic restent disponibles. Le gel et son motif sont audités.
                </template>
                <template v-else>
                  Les jobs bloqués pendant le gel ne repartent pas d'eux-mêmes : une demande bloquée repasse par une nouvelle
                  version, une configuration interrompue par une reprise approuvée.
                </template>
              </p>
              <div class="actions">
                <button type="submit" :class="toggle(env)?.action === 'freeze' ? 'danger' : 'primary'" :disabled="busy || reason.trim().length < 3" data-testid="freeze-confirm">
                  {{ toggle(env)?.action === 'freeze' ? 'Confirmer le gel' : 'Confirmer le dégel' }}
                </button>
                <button type="button" :disabled="busy" @click="openId = null">Annuler</button>
              </div>
              <p v-if="error" class="field-error" role="alert">{{ error }}</p>
            </form>
          </td>
        </tr>
      </template>
    </tbody>
  </table>
</template>
