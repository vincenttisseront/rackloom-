-- F46 : rapports du banc d'évaluation Ollama (ajout seul).
-- Clé de versionnage : (adapter, model, prompt_id) ; un rapport par exécution.

CREATE TABLE chat_eval_reports (
    id uuid PRIMARY KEY,
    adapter text NOT NULL CHECK (adapter IN ('fake', 'ollama')),
    model text NOT NULL,
    prompt_id text NOT NULL,
    scenario text,
    verdict text NOT NULL CHECK (verdict IN ('passed', 'failed')),
    failure_codes text[] NOT NULL DEFAULT '{}',
    metrics jsonb NOT NULL,
    cases jsonb NOT NULL,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_eval_reports_lookup ON chat_eval_reports (model, prompt_id, created_at DESC);
CREATE INDEX chat_eval_reports_created ON chat_eval_reports (created_at DESC);
CREATE TRIGGER chat_eval_reports_append_only
    BEFORE UPDATE OR DELETE ON chat_eval_reports
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT ON chat_eval_reports TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON chat_eval_reports TO rackloom_worker;
    END IF;
END $$;
