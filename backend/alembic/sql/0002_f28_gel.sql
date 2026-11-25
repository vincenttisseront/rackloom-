-- F28 : gel d'urgence par environnement (docs/backlog.md).
-- Un gel actif au plus par environnement ; une ligne n'évolue qu'une fois, à sa levée.

CREATE TABLE environment_freezes (
    id uuid PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 500),
    frozen_by uuid NOT NULL REFERENCES users(id),
    frozen_at timestamptz NOT NULL DEFAULT now(),
    lifted_by uuid REFERENCES users(id),
    lifted_at timestamptz,
    lift_reason text CHECK (lift_reason IS NULL OR length(btrim(lift_reason)) BETWEEN 3 AND 500),
    CHECK ((lifted_at IS NULL) = (lifted_by IS NULL) AND (lifted_at IS NULL) = (lift_reason IS NULL))
);
CREATE UNIQUE INDEX environment_freezes_active ON environment_freezes (environment_id) WHERE lifted_at IS NULL;
CREATE INDEX environment_freezes_history ON environment_freezes (environment_id, frozen_at DESC);

CREATE FUNCTION rackloom_freeze_lift_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.lifted_at IS NOT NULL
       OR (NEW.id, NEW.environment_id, NEW.project_id, NEW.reason, NEW.frozen_by, NEW.frozen_at)
          IS DISTINCT FROM (OLD.id, OLD.environment_id, OLD.project_id, OLD.reason, OLD.frozen_by, OLD.frozen_at) THEN
        RAISE EXCEPTION 'environment_freezes : seule la levée d''un gel actif est permise (% refusé)', TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER environment_freezes_lift_only
    BEFORE UPDATE OR DELETE ON environment_freezes
    FOR EACH ROW EXECUTE FUNCTION rackloom_freeze_lift_only();

-- Une décision d'admission est prise au démarrage du job ; certaines règles sont rejouées
-- dans la transaction qui marque l'envoi au fournisseur (gel, puis calendrier en F27).
ALTER TABLE admission_decisions
    ADD COLUMN stage text NOT NULL DEFAULT 'start' CHECK (stage IN ('start', 'dispatch'));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON environment_freezes TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON environment_freezes TO rackloom_worker;
    END IF;
END;
$$;
