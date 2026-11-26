-- F54 : sollicitations de revue (passer la main sans perdre le contexte).
-- Un commentaire ne modifie pas le plan, ne vaut pas approbation, ne crée aucun droit.

CREATE TABLE change_reviews (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    version_no integer NOT NULL,
    revision_no integer,
    plan_id uuid,
    focus text,
    question text NOT NULL CHECK (char_length(question) BETWEEN 1 AND 2000),
    created_by uuid NOT NULL REFERENCES users(id),
    solicited_user_id uuid NOT NULL REFERENCES users(id),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'replied', 'superseded')),
    reply_text text CHECK (reply_text IS NULL OR char_length(reply_text) BETWEEN 1 AND 4000),
    replied_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT change_reviews_not_self CHECK (created_by <> solicited_user_id)
);
CREATE INDEX change_reviews_change ON change_reviews (change_id, created_at DESC);
CREATE INDEX change_reviews_solicited ON change_reviews (solicited_user_id, status);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT SELECT, INSERT, UPDATE ON change_reviews TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT SELECT ON change_reviews TO rackloom_worker;
    END IF;
END $$;
