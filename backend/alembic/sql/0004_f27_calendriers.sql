-- F27 : calendriers de changement (fenêtres de maintenance et périodes de gel planifiées).
-- Fuseau IANA par environnement ; horodatages absolus en UTC ; jours/heures locaux pour les fenêtres.

CREATE TABLE environment_calendars (
    environment_id uuid PRIMARY KEY REFERENCES environments(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id),
    timezone text NOT NULL DEFAULT 'Europe/Paris'
        CHECK (timezone ~ '^[A-Za-z0-9_+\-/]+$' AND length(timezone) BETWEEN 1 AND 64),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid REFERENCES users(id)
);

-- Fenêtres récurrentes : weekdays 0=lundi … 6=dimanche (ISO), heures locales du fuseau.
CREATE TABLE change_windows (
    id uuid PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id),
    weekdays smallint[] NOT NULL
        CHECK (cardinality(weekdays) >= 1
               AND weekdays <@ ARRAY[0, 1, 2, 3, 4, 5, 6]::smallint[]),
    start_local time NOT NULL,
    end_local time NOT NULL,
    label text NOT NULL DEFAULT '' CHECK (length(label) <= 120),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)),
    CHECK (start_local <> end_local)
);
CREATE INDEX change_windows_env ON change_windows (environment_id) WHERE revoked_at IS NULL;

-- Périodes de gel planifiées (distinctes du gel d'urgence F28).
CREATE TABLE calendar_freezes (
    id uuid PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    timezone text NOT NULL CHECK (timezone ~ '^[A-Za-z0-9_+\-/]+$' AND length(timezone) BETWEEN 1 AND 64),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    cancelled_by uuid REFERENCES users(id),
    cancel_reason text CHECK (cancel_reason IS NULL OR length(btrim(cancel_reason)) BETWEEN 3 AND 500),
    CHECK (ends_at > starts_at),
    CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL) AND (cancelled_at IS NULL) = (cancel_reason IS NULL))
);
CREATE INDEX calendar_freezes_active ON calendar_freezes (environment_id, starts_at, ends_at)
    WHERE cancelled_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON environment_calendars TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON change_windows TO rackloom_api;
        GRANT SELECT, INSERT, UPDATE ON calendar_freezes TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON environment_calendars TO rackloom_worker;
        GRANT SELECT ON change_windows TO rackloom_worker;
        GRANT SELECT ON calendar_freezes TO rackloom_worker;
    END IF;
END;
$$;
