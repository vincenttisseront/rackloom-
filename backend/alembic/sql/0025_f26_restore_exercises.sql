-- F26 : exercices de restauration et interrupteur global d'envois (dispatch_paused).
-- Base, artefacts et références de secrets restaurés ensemble dans un environnement isolé ;
-- clés et state Terraform vérifiés par des procédures distinctes (ADR-0004, ADR-0007).
-- Au démarrage d'un exercice (et après restauration réelle), les envois vers le parc sont
-- suspendus : les jobs d'écriture en file ne sont pas réclamés (wait_reason = dispatch_paused).

CREATE TABLE restore_exercises (
    id uuid PRIMARY KEY,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'passed', 'failed')),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    isolated_root text NOT NULL CHECK (char_length(isolated_root) BETWEEN 1 AND 1024),
    backup_root text NOT NULL CHECK (char_length(backup_root) BETWEEN 1 AND 1024),
    -- Phases du paquet principal (base + artefacts + refs secrets).
    db_status text NOT NULL DEFAULT 'pending'
        CHECK (db_status IN ('pending', 'ok', 'failed', 'skipped')),
    artifacts_status text NOT NULL DEFAULT 'pending'
        CHECK (artifacts_status IN ('pending', 'ok', 'failed', 'skipped')),
    secret_refs_status text NOT NULL DEFAULT 'pending'
        CHECK (secret_refs_status IN ('pending', 'ok', 'failed', 'skipped')),
    -- Procédures distinctes (jamais dans le même paquet que la base).
    keys_status text NOT NULL DEFAULT 'pending'
        CHECK (keys_status IN ('pending', 'ok', 'failed', 'skipped', 'not_provided')),
    state_status text NOT NULL DEFAULT 'pending'
        CHECK (state_status IN ('pending', 'ok', 'failed', 'skipped', 'not_provided', 'simulated')),
    phases jsonb NOT NULL DEFAULT '{}'::jsonb,
    dispatch_paused boolean NOT NULL DEFAULT true,
    queued_writes_at_start integer NOT NULL DEFAULT 0
        CHECK (queued_writes_at_start >= 0),
    queued_writes_still_queued integer
        CHECK (queued_writes_still_queued IS NULL OR queued_writes_still_queued >= 0),
    simulated_state boolean NOT NULL DEFAULT true,
    failure_code text CHECK (failure_code IS NULL OR char_length(failure_code) BETWEEN 1 AND 80),
    failure_message text CHECK (
        failure_message IS NULL OR length(btrim(failure_message)) BETWEEN 1 AND 1000
    ),
    started_by text NOT NULL CHECK (char_length(started_by) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((finished_at IS NULL) = (status = 'running')),
    CHECK ((duration_ms IS NULL) = (status = 'running'))
);

CREATE INDEX restore_exercises_started ON restore_exercises (started_at DESC);

-- Interrupteur global : valeur initiale explicite (non suspendu).
INSERT INTO platform_settings (key, value)
VALUES ('dispatch_paused', '{"paused": false}'::jsonb)
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON restore_exercises TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON restore_exercises TO rackloom_worker;
    END IF;
END $$;
