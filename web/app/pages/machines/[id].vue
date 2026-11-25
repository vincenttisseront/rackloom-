<script setup lang="ts">
const route = useRoute()
const api = useApi()
const host = ref<any>(null)
const journals = ref<any[]>([])
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    host.value = await api.get(`/hosts/${route.params.id}`)
    const obs = await api.get(`/hosts/${route.params.id}/observations?kind=journal`)
    const seen = new Set<string>()
    journals.value = obs.filter((j: any) => {
      const name = j.data?.name || 'journal'
      if (seen.has(name)) return false
      seen.add(name)
      return true
    })
  } catch (e) {
    error.value = errorText(e)
  }
})
</script>

<template>
  <div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <template v-if="host">
      <div class="page-head">
        <div>
          <div class="crumbs"><NuxtLink :to="`/demandes/${host.change.id}`">{{ host.change.ref }}</NuxtLink> › Fiche machine</div>
          <h1>{{ host.name }}
            <span class="badge" :class="host.health.state === 'healthy' ? 'ok' : host.health.state === 'error' ? 'bad' : 'warn'" data-testid="host-health">{{ host.health.label }}</span>
          </h1>
          <p class="small muted">Dernière collecte : {{ dateTime(host.health.last_collected_at) }} (heure de Paris)</p>
        </div>
      </div>
      <div class="grid-2">
        <section class="card" aria-labelledby="ident-title">
          <h2 id="ident-title">Identité et déploiement</h2>
          <dl class="kv">
            <dt>Identité vCenter</dt><dd class="mono">{{ host.vcenter.vm_stable_id }} <SimBadge v-if="host.vcenter.simulated" /></dd>
            <dt>Adresse</dt><dd class="mono">{{ host.address }} <SimBadge v-if="host.address_simulated" title="Adresse du nœud de labo associé par le vCenter fictif" /></dd>
            <dt>Clé d'hôte</dt><dd class="mono">{{ host.host_key_fingerprint }}</dd>
            <dt>Enrôlée le</dt><dd>{{ dateTime(host.enrolled_at) }}</dd>
            <dt>Déploiement</dt><dd>{{ host.deployment_state }} · service <span class="badge" :class="SERVICE_TONE[host.deployment.service_status]">{{ host.deployment.service_status_label }}</span></dd>
            <dt>Template</dt><dd>{{ host.template.title }} {{ host.template.version }}</dd>
          </dl>
        </section>
        <section class="card" aria-labelledby="os-title">
          <h2 id="os-title">Système observé</h2>
          <dl class="kv">
            <dt>OS</dt><dd data-testid="host-os">{{ host.facts?.os || 'non collecté' }}</dd>
            <dt>Init</dt><dd>{{ host.facts?.init || '—' }}</dd>
            <dt>Noyau</dt><dd class="mono">{{ host.facts?.kernel || '—' }}</dd>
            <template v-for="(v, name) in host.packages || {}" :key="name">
              <dt>Paquet {{ name }}</dt><dd class="mono" data-testid="host-package">{{ v }}</dd>
            </template>
            <template v-for="(s, name) in host.services || {}" :key="`s-${name}`">
              <dt>Service {{ name }}</dt>
              <dd><span class="badge" :class="s.active ? 'ok' : 'bad'">{{ s.active ? 'actif' : 'inactif' }}</span>
                <span class="small muted"> ({{ s.manager }} ; systemd {{ s.systemd }})</span></dd>
            </template>
            <dt>Sonde HTTP</dt><dd>{{ host.probe ? (host.probe.ok ? `HTTP ${host.probe.status} en ${host.probe.latency_ms} ms` : 'en échec') : 'non collectée' }}</dd>
          </dl>
        </section>
      </div>
      <section class="card" aria-labelledby="prov-title">
        <h2 id="prov-title">Provenance des données</h2>
        <p class="small">Source : {{ host.provenance.source }}. Parties simulées : {{ host.provenance.simulated_parts.join(', ') }} <SimBadge /></p>
        <p class="small muted">Les valeurs collectées sur la machine sont des données non fiables : elles sont affichées, jamais interprétées.</p>
      </section>
      <section class="card" aria-labelledby="journal-title">
        <h2 id="journal-title">Extraits de journaux</h2>
        <p v-if="!journals.length" class="empty">Non collecté.</p>
        <div v-for="journal in journals" :key="journal.id">
          <h3>{{ journal.data.name || 'journal' }}</h3>
          <p class="small muted">{{ journal.data.source }} · collecté le {{ dateTime(journal.collected_at) }}</p>
          <pre v-if="journal.data.lines?.length" class="mono small" style="white-space: pre-wrap; background: var(--surface-2); padding: 0.75rem; border-radius: 8px">{{ journal.data.lines.join('\n') }}</pre>
          <p v-else class="empty small">Aucune ligne.</p>
        </div>
      </section>
    </template>
  </div>
</template>
