-- F25 : ITSM et notifications.
-- Adaptateur fictif testable ; livraisons idempotentes (dedupe_key) ; reprise.
-- Webhooks entrants enregistrés sans jamais créer d'approbation.
-- Notifications pointent vers une page authentifiée de Rackloom (href relatif + URL publique).

CREATE TABLE itsm_adapters (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    kind text NOT NULL DEFAULT 'fake' CHECK (kind IN ('fake')),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    simulated boolean NOT NULL DEFAULT true,
    enabled boolean NOT NULL DEFAULT true,
    -- Empreinte du jeton webhook (jamais le clair). NULL = webhooks désactivés.
    webhook_token_hash text CHECK (webhook_token_hash IS NULL OR char_length(webhook_token_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE itsm_tickets (
    id uuid PRIMARY KEY,
    adapter_id uuid NOT NULL REFERENCES itsm_adapters(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    change_id uuid NOT NULL REFERENCES changes(id),
    job_id uuid REFERENCES jobs(id),
    external_ticket_id text NOT NULL CHECK (char_length(external_ticket_id) BETWEEN 1 AND 120),
    external_key text NOT NULL CHECK (char_length(external_key) BETWEEN 1 AND 80),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
    -- Une demande → un ticket create par adaptateur (rejeu webhook / livraison sans doublon).
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
    rackloom_url text NOT NULL CHECK (char_length(rackloom_url) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (adapter_id, dedupe_key),
    UNIQUE (adapter_id, external_ticket_id)
);
CREATE INDEX itsm_tickets_change ON itsm_tickets (change_id);
CREATE INDEX itsm_tickets_project ON itsm_tickets (project_id);

CREATE TABLE itsm_deliveries (
    id uuid PRIMARY KEY,
    adapter_id uuid NOT NULL REFERENCES itsm_adapters(id),
    ticket_id uuid REFERENCES itsm_tickets(id),
    change_id uuid NOT NULL REFERENCES changes(id),
    job_id uuid REFERENCES jobs(id),
    kind text NOT NULL CHECK (kind IN ('ticket_create', 'ticket_update', 'notification')),
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text CHECK (last_error IS NULL OR char_length(last_error) <= 500),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (adapter_id, dedupe_key)
);
CREATE INDEX itsm_deliveries_pending
    ON itsm_deliveries (adapter_id, status) WHERE status <> 'delivered';
CREATE INDEX itsm_deliveries_change ON itsm_deliveries (change_id);

CREATE TABLE itsm_inbound_events (
    id uuid PRIMARY KEY,
    adapter_id uuid NOT NULL REFERENCES itsm_adapters(id),
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
    event_type text NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 80),
    external_ticket_id text CHECK (external_ticket_id IS NULL OR char_length(external_ticket_id) BETWEEN 1 AND 120),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    outcome text NOT NULL DEFAULT 'recorded'
        CHECK (outcome IN ('recorded', 'ignored', 'duplicate', 'rejected')),
    -- Ex. approval_forbidden : répondre à une notification / webhook ne peut pas approuver.
    rejection_code text CHECK (rejection_code IS NULL OR char_length(rejection_code) BETWEEN 1 AND 80),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (adapter_id, dedupe_key)
);
CREATE INDEX itsm_inbound_events_adapter ON itsm_inbound_events (adapter_id, created_at DESC);

CREATE TABLE notifications (
    id uuid PRIMARY KEY,
    project_id uuid REFERENCES projects(id),
    user_id uuid NOT NULL REFERENCES users(id),
    change_id uuid REFERENCES changes(id),
    ticket_id uuid REFERENCES itsm_tickets(id),
    kind text NOT NULL CHECK (kind IN ('itsm_ticket', 'change_update', 'job_update')),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    body text NOT NULL DEFAULT '' CHECK (char_length(body) <= 1000),
    -- Chemin authentifié (SPA) ; jamais un jeton ni un secret dans l'URL.
    href text NOT NULL CHECK (href ~ '^/[A-Za-z0-9_./-]+$' AND char_length(href) <= 300),
    absolute_url text NOT NULL CHECK (char_length(absolute_url) BETWEEN 1 AND 500),
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    dedupe_key text NOT NULL CHECK (char_length(dedupe_key) BETWEEN 1 AND 200),
    UNIQUE (user_id, dedupe_key)
);
CREATE INDEX notifications_user ON notifications (user_id, created_at DESC);
CREATE INDEX notifications_unread ON notifications (user_id) WHERE read_at IS NULL;

-- Adaptateur fictif de laboratoire (aucun ITSM réel).
INSERT INTO itsm_adapters (id, slug, kind, title, simulated, enabled, webhook_token_hash)
VALUES (
    '01940000-0000-7000-8000-000000000025',
    'itsm-fake',
    'fake',
    'ITSM fictif (laboratoire)',
    true,
    true,
    -- sha256("itsm-fake-webhook-demo") — jeton de démo uniquement, jamais un secret réel.
    '97594d2d3380b9308b1904f7046cd5895d10de0f5313d9dced0ad38f042072d5'
);

DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        FOREACH t IN ARRAY ARRAY[
            'itsm_adapters', 'itsm_tickets', 'itsm_deliveries', 'itsm_inbound_events', 'notifications'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        FOREACH t IN ARRAY ARRAY[
            'itsm_adapters', 'itsm_tickets', 'itsm_deliveries', 'itsm_inbound_events', 'notifications'
        ] LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_worker', t);
        END LOOP;
    END IF;
END $$;
