<script setup lang="ts">
const route = useRoute()
const api = useApi()
const host = ref<any>(null)
const journals = ref<any[]>([])
const compliance = ref<any>(null)
const remediating = ref(false)
const remediation = ref<any>(null)
const rudderBusy = ref(false)
const rudderImport = ref<any>(null)
const error = ref<string | null>(null)

const FINDING_LABEL: Record<string, string> = {
  compliant: 'conforme',
  noncompliant: 'non conforme',
  not_applicable: 'non applicable',
  missing_data: 'données absentes',
}

async function loadCompliance() {
  compliance.value = await api.get(`/hosts/${route.params.id}/compliance`)
}

async function runScan() {
  error.value = null
  remediation.value = null
  compliance.value = await api.post(`/hosts/${route.params.id}/compliance/scan`, {})
}

async function proposeRemediation() {
  remediating.value = true
  error.value = null
  try {
    remediation.value = await api.post(`/hosts/${route.params.id}/compliance/remediate`, {})
  } catch (e) {
    error.value = errorText(e)
  } finally {
    remediating.value = false
  }
}

async function importRudder() {
  rudderBusy.value = true
  error.value = null
  rudderImport.value = null
  try {
    rudderImport.value = await api.post(`/hosts/${route.params.id}/rudder/import-observations`, {})
    if (!rudderImport.value.queued) {
      await loadCompliance()
      host.value = await api.get(`/hosts/${route.params.id}`)
    }
  } catch (e) {
    error.value = errorText(e)
  } finally {
    rudderBusy.value = false
  }
}

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
    await loadCompliance()
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
      <section class="card" aria-labelledby="compliance-title" data-testid="compliance-panel">
        <h2 id="compliance-title">Conformité</h2>
        <p class="small muted">Comparaison à un profil versionné. Lecture seule : aucune correction automatique.</p>
        <div class="row gap" style="margin: 0.75rem 0">
          <button type="button" class="btn" data-testid="compliance-scan" @click="runScan">Lancer un scan</button>
          <button
            type="button"
            class="btn secondary"
            data-testid="rudder-import"
            :disabled="rudderBusy"
            @click="importRudder"
          >
            Importer depuis Rudder
          </button>
          <button
            v-if="compliance?.findings?.some((f: any) => f.status === 'noncompliant')"
            type="button"
            class="btn secondary"
            data-testid="compliance-remediate"
            :disabled="remediating"
            @click="proposeRemediation"
          >
            Proposer une correction
          </button>
        </div>
        <p v-if="rudderImport" class="banner ok" data-testid="rudder-import-result" role="status">
          <template v-if="rudderImport.queued">
            Import Rudder mis en file (job {{ rudderImport.job_id }}) — lecture seule.
          </template>
          <template v-else>
            Observations importées ({{ (rudderImport.kinds || []).join(', ') }})
            <SimBadge v-if="rudderImport.simulated" />
          </template>
        </p>
        <p v-if="!compliance || compliance.status === 'none'" class="empty">Aucun scan pour cette machine.</p>
        <template v-else>
          <p>
            Statut :
            <span
              class="badge"
              :class="compliance.status === 'ok' ? 'ok' : compliance.status === 'noncompliant' || compliance.status === 'partial' ? 'bad' : 'warn'"
              data-testid="compliance-status"
            >{{ compliance.status }}</span>
            <span v-if="compliance.profile" class="small muted">
              · {{ compliance.profile.slug }}@{{ compliance.profile.version }}
            </span>
          </p>
          <ul class="plain" data-testid="compliance-findings">
            <li v-for="f in compliance.findings" :key="f.rule_id" class="small">
              <strong>{{ f.category }}</strong> {{ f.attribute }} :
              <span>{{ FINDING_LABEL[f.status] || f.status }}</span>
              <span class="muted"> (attendu {{ f.expected || '—' }}, observé {{ f.observed || '—' }})</span>
            </li>
          </ul>
          <p v-if="remediation" class="banner ok" data-testid="compliance-remediation">
            Brouillon {{ remediation.change?.ref || remediation.change_id }} créé —
            aucune écriture fournisseur. <SimBadge />
          </p>
        </template>
      </section>
      <section class="card" aria-labelledby="prov-title">
        <h2 id="prov-title">Provenance des données</h2>
        <p class="small">Source : {{ host.provenance.source }}. Parties simulées : {{ host.provenance.simulated_parts.join(', ') }} <SimBadge /></p>
        <p v-if="host.provenance.rudder" class="small muted">{{ host.provenance.rudder }} <SimBadge /></p>
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
