<script setup lang="ts">
const props = defineProps<{ plan: any }>()
const ACTIONS: Record<string, string> = { create: 'Création', update: 'Modification', delete: 'Suppression', replace: 'Remplacement', 'no-op': 'Inchangé' }
</script>

<template>
  <section class="card" aria-labelledby="plan-title" data-testid="plan-panel">
    <div class="field-head">
      <h2 id="plan-title">Plan et approbation</h2>
      <span class="badge info">Plan Terraform {{ props.plan.terraform_version }}</span>
    </div>
    <p class="big" data-testid="plan-headline"><strong>{{ props.plan.headline }}</strong></p>
    <p class="small muted">
      Ressources de labo locales et jetables, <strong>pas des VM</strong> <span class="badge neutral">Ressource de labo</span>
      <span class="badge neutral">State de démo</span>
    </p>
    <ul class="small">
      <li v-for="r in props.plan.risks" :key="r.code">
        <span class="badge" :class="r.level === 'blocking' ? 'bad' : r.level === 'warning' ? 'warn' : 'info'">
          {{ r.level === 'blocking' ? 'Bloquant' : r.level === 'warning' ? 'Attention' : 'Info' }}
        </span>
        {{ r.text }}
      </li>
    </ul>
    <p v-if="props.plan.waves?.length > 1" class="small" data-testid="plan-waves">
      Vagues approuvées : {{ props.plan.waves.map((w: string[], i: number) => `${i === 0 ? 'pilote' : 'vague ' + (i + 1)} (${w.join(', ')})`).join(' ; ') }}.
      Un échec arrête les vagues suivantes.
    </p>
    <h3>Machines</h3>
    <table>
      <thead><tr><th scope="col">Nom</th><th scope="col">Action</th></tr></thead>
      <tbody>
        <tr v-for="v in props.plan.vms" :key="v.name"><td>{{ v.name }}</td><td>{{ ACTIONS[v.action] || v.action }}</td></tr>
      </tbody>
    </table>
    <details class="panel" style="margin-top: 1rem">
      <summary>Diff Terraform ({{ props.plan.resources.length }} ressource(s))</summary>
      <div class="panel-body">
        <table>
          <thead><tr><th scope="col">Adresse</th><th scope="col">Type</th><th scope="col">Action</th></tr></thead>
          <tbody>
            <tr v-for="r in props.plan.resources" :key="r.address">
              <td><code>{{ r.address }}</code></td><td><code>{{ r.type }}</code></td><td>{{ ACTIONS[r.action] || r.action }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
    <details class="panel">
      <summary>Contrat Ansible</summary>
      <div class="panel-body">
        <div v-for="c in props.plan.ansible.components" :key="c.slug">
          <h3>{{ c.title }} {{ c.version }} <span class="muted small mono">{{ c.role }}</span></h3>
          <dl class="kv small">
            <dt>Paquets</dt><dd>{{ c.packages.join(', ') || '—' }}</dd>
            <dt>Services</dt><dd>{{ c.services.map((s: any) => `${s.unit} (${s.state})`).join(', ') || '—' }}</dd>
            <dt>Ports</dt><dd>{{ c.ports.map((p: any) => `${p.port}/${p.proto}`).join(', ') || '—' }} <span class="badge neutral">Non appliqué</span></dd>
            <dt>Fichiers</dt><dd class="mono">{{ c.files.join(', ') || '—' }}</dd>
            <dt>Élévation</dt><dd>{{ c.elevation }}</dd>
            <dt>Redémarrage</dt><dd>{{ c.reboot === 'never' ? 'jamais' : c.reboot }}</dd>
          </dl>
          <span class="badge neutral">Cible de labo</span>
        </div>
        <h3>Couverture du check mode</h3>
        <p class="small">Couvert : {{ props.plan.ansible.check_mode.covered.join(', ') || 'aucune tâche' }}</p>
        <p class="small">Non couvert : {{ props.plan.ansible.check_mode.not_covered.join(', ') }}</p>
        <p class="small muted">Identités : {{ props.plan.ansible.identities.configure }} ; {{ props.plan.ansible.identities.collect }}</p>
      </div>
    </details>
    <h3>Vérifications après configuration</h3>
    <p class="small">{{ props.plan.verifications.join(' · ') }}</p>
    <dl class="kv small">
      <dt>Empreinte du dossier</dt><dd class="mono" data-testid="bundle-digest">{{ props.plan.bundle_digest }}</dd>
      <dt>Valide jusqu'au</dt><dd>{{ dateTime(props.plan.valid_until) }} (heure de Paris)</dd>
      <dt>Template</dt><dd>{{ props.plan.bundle.template.slug }} {{ props.plan.bundle.template.version }}</dd>
    </dl>
    <slot />
  </section>
</template>
