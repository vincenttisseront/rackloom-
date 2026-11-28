const TZ = 'Europe/Paris'

export function dateTime(value?: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium', timeZone: TZ }).format(new Date(value))
}

export function time(value?: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('fr-FR', { timeStyle: 'medium', timeZone: TZ }).format(new Date(value))
}

export function day(value?: string | null): string {
  if (!value) return '—'
  const d = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T12:00:00Z`) : new Date(value)
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'long', timeZone: TZ }).format(d)
}

export function isoDay(offsetDays: number): string {
  const d = new Date(Date.now() + offsetDays * 86400000)
  return d.toISOString().slice(0, 10)
}

export const JOB_LABELS: Record<string, string> = {
  preflight: 'Préflight',
  tf_plan: 'Plan Terraform',
  tf_apply: 'Application du plan',
  tf_state_read: 'Lecture du state',
  guest_ready: "Préparation de l'invité",
  ansible_configure: 'Configuration Ansible',
  verify: 'Vérification',
  reconcile: 'Réconciliation',
  inventory_refresh: "Rafraîchissement de l'inventaire",
  rudder_import: 'Import Rudder (lecture seule)',
  drift_scan: 'Détection de dérive',
  compliance_scan: 'Scan de conformité',
  vm_restore: 'Restauration VM (réseau isolé)',
}

export const JOB_STATES: Record<string, string> = {
  queued: 'En file',
  leased: 'Pris en charge',
  running: 'En cours',
  succeeded: 'Terminé',
  failed: 'Échec',
  blocked: 'Bloqué',
  uncertain: 'Incertain',
  cancelled: 'Annulé',
}

export const BLOCK_REASONS: Record<string, string> = {
  env_frozen: 'Environnement gelé : le job ne démarre pas pendant le gel',
  calendar_frozen: 'Période de gel du calendrier : le job ne démarre pas',
  outside_change_window: 'Hors fenêtre de maintenance : le job ne démarre pas',
  plan_expired: 'Plan expiré',
  cancel_requested: 'Annulation demandée avant le démarrage',
  requester_revoked: "Le demandeur n'a plus le rôle opérateur",
  approver_revoked: "L'approbateur n'a plus le rôle approbateur",
  approval_missing: 'Approbation absente',
  approval_revoked: 'Approbation révoquée',
  approval_consumed: 'Approbation déjà consommée',
  bundle_mismatch: "Dossier d'exécution modifié depuis l'approbation",
  policy_changed: 'Politique modifiée depuis la résolution : nouvelle approbation requise',
  resolution_no_longer_admissible: "Résolution plus admissible : nouvelle révision requise",
  simulated_in_prod: 'Connecteur simulé refusé en production',
  stack_locked: 'Stack verrouillée par un autre job',
  destructive: 'Plan avec suppression ou remplacement',
  temporary_right_expired: 'Droit temporaire expiré ou révoqué : le job ne démarre pas',
  org_policy_refused: "Contrainte d'organisation non satisfaite (aucune exception valide)",
  reauth_required: 'Réauthentification forte requise',
  reauth_stale: "Preuve d'authentification trop ancienne",
  emergency_closed: "Connexion d'urgence fermée",
  project_quota_saturated: 'Quota de concurrence du projet saturé',
  environment_quota_saturated: "Quota de concurrence de l'environnement saturé",
  compatibility_incompatible: 'Combinaison de versions incompatible',
  compatibility_unverified: 'Combinaison de versions non vérifiée',
  backup_missing: 'Sauvegarde introuvable',
  backup_stale: 'Sauvegarde périmée',
  backup_unverified: 'Sauvegarde non vérifiable',
  backup_snapshot_not_proof: 'Snapshot vSphere refusé comme preuve de sauvegarde',
  license_unavailable: 'Licence indisponible',
  license_unverified: 'Licence non vérifiée',
}

export const SERVICE_TONE: Record<string, string> = {
  ready: 'ok',
  created_not_ready: 'warn',
  failed: 'bad',
  verification_unavailable: 'warn',
  unavailable: 'bad',
  unknown: 'neutral',
}

export const CHANGE_TONE: Record<string, string> = {
  draft: 'neutral',
  needs_info: 'warn',
  resolution_blocked: 'bad',
  validated: 'info',
  planning: 'info',
  plan_ready: 'info',
  awaiting_approval: 'info',
  approved: 'info',
  executing: 'info',
  verifying: 'info',
  succeeded: 'ok',
  partially_succeeded: 'warn',
  failed: 'bad',
  rejected: 'bad',
  cancelled: 'neutral',
  plan_failed: 'bad',
  plan_expired: 'warn',
  blocked: 'bad',
  uncertain: 'warn',
}
