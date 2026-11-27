<script setup lang="ts">
const api = useApi()
const session = useSession()
const error = ref<string | null>(null)
const policy = ref<any>(null)
const emergency = ref<any>(null)
const projectId = ref<string | null>(null)
const exceptions = ref<any[]>([])
const grants = ref<any[]>([])
const simResult = ref<any>(null)
const simChangeId = ref('')

onMounted(async () => {
  try {
    policy.value = await api.get('/admin/policies/active')
    emergency.value = await api.get('/admin/emergency')
    // D7 : l'admin plateforme n'a pas /projects sans rôle ; /admin/environments expose le périmètre gel.
    const envs = await api.get('/admin/environments') as any[]
    const vit = envs.find(e => e.project?.slug === 'vitrine') || envs[0]
    if (vit?.project?.id) {
      projectId.value = vit.project.id
      exceptions.value = await api.get(`/projects/${vit.project.id}/policy-exceptions`)
      grants.value = await api.get(`/projects/${vit.project.id}/temporary-grants`)
    }
  } catch (e) {
    error.value = errorText(e)
  }
})

async function runSimulate() {
  error.value = null
  simResult.value = null
  try {
    simResult.value = await api.post('/admin/policies/simulate', {
      change_id: simChangeId.value,
      candidate_organization: {
        constraints: [{
          id: 'ORG001',
          title: 'simulation stricte (dev+prod)',
          applies_to_kinds: ['dev', 'prod'],
          field: '/environment',
          waivable: true,
        }],
      },
    })
  } catch (e) {
    error.value = errorText(e)
  }
}

async function closeEmergency() {
  error.value = null
  try {
    emergency.value = await api.post('/admin/emergency/close', {
      reason: 'Fermeture depuis l\'UI admin (SSO rétabli)',
    })
    emergency.value = await api.get('/admin/emergency')
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Administration › Gouvernance</h1>
        <p class="small muted">
          Politiques versionnées (F10), droits temporaires (F11), accès d'urgence (F14).
          Configurer un connecteur ne révèle jamais un secret existant (F13).
        </p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>

    <section class="card" aria-labelledby="policy-title" data-testid="policy-panel">
      <h2 id="policy-title">Bundle de politiques actif</h2>
      <p v-if="policy" class="mono small">
        {{ policy.version }} · {{ policy.digest?.slice(0, 18) }}…
      </p>
      <ul v-if="policy" class="small">
        <li v-for="r in policy.rules" :key="r.id">{{ r.id }} — {{ r.title }}</li>
      </ul>
    </section>

    <section class="card" aria-labelledby="sim-title" data-testid="policy-simulate">
      <h2 id="sim-title">Simuler une politique sur une demande historique</h2>
      <p class="small muted">Aucune écriture : les plans existants restent inchangés.</p>
      <label>
        Identifiant de demande
        <input v-model="simChangeId" data-testid="sim-change-id" class="mono" />
      </label>
      <button type="button" data-testid="sim-run" @click="runSimulate">Simuler</button>
      <div v-if="simResult" class="banner" :class="simResult.outcome === 'refused' ? 'warn' : 'ok'" data-testid="sim-result">
        {{ simResult.outcome }}
        <template v-if="simResult.refusal">
          — règle {{ simResult.refusal.id }}
          <template v-if="simResult.refusal.field">, champ {{ simResult.refusal.field }}</template>
          : {{ simResult.refusal.reason }}
        </template>
        <span class="small"> (écritures : {{ simResult.wrote ? 'oui' : 'non' }})</span>
      </div>
    </section>

    <section class="card" aria-labelledby="exc-title" data-testid="exceptions-panel">
      <h2 id="exc-title">Exceptions de politique</h2>
      <p v-if="!exceptions.length" class="empty">Aucune exception.</p>
      <table v-else>
        <thead>
          <tr><th>Règle</th><th>Propriétaire</th><th>Expire</th><th>Active</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in exceptions" :key="e.id">
            <td class="mono">{{ e.rule_id }}</td>
            <td>{{ e.owner_name }}</td>
            <td>{{ dateTime(e.expires_at) }}</td>
            <td>{{ e.active ? 'oui' : 'non' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="grant-title" data-testid="grants-panel">
      <h2 id="grant-title">Droits temporaires</h2>
      <p v-if="!grants.length" class="empty">Aucun droit temporaire.</p>
      <table v-else>
        <thead>
          <tr><th>Personne</th><th>Rôle</th><th>Expire</th><th>Actif</th></tr>
        </thead>
        <tbody>
          <tr v-for="g in grants" :key="g.id">
            <td>{{ g.display_name }}</td>
            <td class="mono">{{ g.role }} / {{ g.operation }}</td>
            <td>{{ dateTime(g.expires_at) }}</td>
            <td>{{ g.active ? 'oui' : 'non' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="em-title" data-testid="emergency-panel">
      <h2 id="em-title">Accès d'urgence</h2>
      <p class="small">
        Activation depuis la console uniquement
        (<code>rackloom emergency activate</code>
        — voir <code>docs/emergency-access.md</code>.
      </p>
      <dl v-if="emergency" class="small">
        <dt>SSO disponible</dt>
        <dd data-testid="sso-available">{{ emergency.sso_available ? 'oui' : 'non' }}</dd>
        <dt>Intervention</dt>
        <dd data-testid="emergency-intervention">
          <template v-if="emergency.intervention">
            {{ emergency.intervention.reason }} (expire {{ dateTime(emergency.intervention.expires_at) }})
          </template>
          <template v-else>aucune</template>
        </dd>
      </dl>
      <button
        v-if="emergency?.intervention"
        type="button"
        data-testid="emergency-close"
        @click="closeEmergency"
      >
        Fermer l'intervention
      </button>
    </section>
  </div>
</template>
