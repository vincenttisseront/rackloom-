-- F19 : patch par vagues (pilote, contrôles avant/après, fenêtres, redémarrage).
-- Une dégradation sur le pilote bloque la vague suivante ; aucun retour arrière automatique.
-- Exécution locale simulée (D2) : badge « Simulé » ; labo réel L2 pour validation.

ALTER TABLE changes DROP CONSTRAINT IF EXISTS changes_kind_check;
ALTER TABLE changes ADD CONSTRAINT changes_kind_check
    CHECK (kind IN ('create', 'modify', 'restore', 'restart', 'retire', 'patch'));

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart', 'lease_retire', 'tf_import',
                    'host_patch', 'patch_check'));

CREATE TABLE patch_campaigns (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    change_id uuid UNIQUE REFERENCES changes(id),
    title text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 120),
    state text NOT NULL DEFAULT 'draft'
        CHECK (state IN (
            'draft', 'plan_ready', 'approved', 'running',
            'wave_blocked', 'succeeded', 'failed', 'cancelled'
        )),
    waves jsonb NOT NULL DEFAULT '[]'::jsonb,
    current_wave integer NOT NULL DEFAULT 0 CHECK (current_wave >= 0),
    pilot_size integer NOT NULL DEFAULT 1 CHECK (pilot_size BETWEEN 1 AND 3),
    wave_size integer NOT NULL DEFAULT 1 CHECK (wave_size BETWEEN 1 AND 10),
    stop_on_failure boolean NOT NULL DEFAULT true CHECK (stop_on_failure = true),
    reboot_policy text NOT NULL DEFAULT 'if_required_in_window'
        CHECK (reboot_policy IN ('never', 'if_required_in_window')),
    package_selector jsonb NOT NULL DEFAULT '{"security": true}'::jsonb,
    block_reason text CHECK (block_reason IS NULL OR length(btrim(block_reason)) BETWEEN 1 AND 500),
    simulated boolean NOT NULL DEFAULT true,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX patch_campaigns_project ON patch_campaigns (project_id, created_at DESC);
CREATE INDEX patch_campaigns_env ON patch_campaigns (environment_id, state);

CREATE TABLE patch_campaign_hosts (
    campaign_id uuid NOT NULL REFERENCES patch_campaigns(id) ON DELETE CASCADE,
    host_id uuid NOT NULL REFERENCES hosts(id),
    host_name text NOT NULL CHECK (char_length(host_name) BETWEEN 1 AND 120),
    wave_index integer NOT NULL CHECK (wave_index >= 0),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN (
            'pending', 'pre_ok', 'pre_failed', 'patched', 'post_ok',
            'degraded', 'wave_blocked', 'skipped'
        )),
    pre_check jsonb,
    post_check jsonb,
    reboot_required boolean NOT NULL DEFAULT false,
    reboot_done boolean NOT NULL DEFAULT false,
    write_attempted boolean NOT NULL DEFAULT false,
    PRIMARY KEY (campaign_id, host_id)
);
CREATE INDEX patch_campaign_hosts_wave ON patch_campaign_hosts (campaign_id, wave_index);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON patch_campaigns TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON patch_campaign_hosts TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT, INSERT, UPDATE ON patch_campaigns TO rackloom_worker;
        GRANT SELECT, INSERT, UPDATE ON patch_campaign_hosts TO rackloom_worker;
    END IF;
END $$;
