-- F29 : consommation par équipe.
-- Tarifs optionnels : sans ligne active, aucun montant n'est exposé.
-- Les totaux alloués / mesurés / estimés sont calculés à la lecture
-- (déploiements, inventaire, demandes en cours) — pas de facture inventée.

CREATE TABLE pricing_rates (
    id uuid PRIMARY KEY,
    -- NULL = tarif plateforme ; sinon tarif limité à un projet.
    project_id uuid REFERENCES projects(id),
    resource text NOT NULL CHECK (resource IN ('vcpu', 'memory_mb', 'disk_gb')),
    -- Unité de facturation (période) ; le montant s'applique à une unité de ressource.
    billing_period text NOT NULL DEFAULT 'month'
        CHECK (billing_period IN ('hour', 'day', 'month')),
    -- Montant en fractions monétaires (centimes pour EUR).
    amount_minor integer NOT NULL CHECK (amount_minor >= 0),
    currency text NOT NULL DEFAULT 'EUR'
        CHECK (currency ~ '^[A-Z]{3}$'),
    label text NOT NULL DEFAULT ''
        CHECK (char_length(label) <= 120),
    simulated boolean NOT NULL DEFAULT true,
    effective_from timestamptz NOT NULL DEFAULT now(),
    effective_until timestamptz,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (effective_until IS NULL OR effective_until > effective_from)
);
CREATE UNIQUE INDEX pricing_rates_active_uniq
    ON pricing_rates (
        resource,
        billing_period,
        COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)
    )
    WHERE effective_until IS NULL;
CREATE INDEX pricing_rates_project ON pricing_rates (project_id, resource)
    WHERE effective_until IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON pricing_rates TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON pricing_rates TO rackloom_worker;
    END IF;
END $$;
