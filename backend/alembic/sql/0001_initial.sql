-- Schéma initial du lot 1 (docs/lot-1-plan.md §6).
-- Identifiants UUIDv7 générés par l'application ; horodatages en UTC (timestamptz).

CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL UNIQUE,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    is_demo boolean NOT NULL DEFAULT false,
    privilege_epoch integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    code text NOT NULL UNIQUE CHECK (code ~ '^[a-z]{2,6}$'),
    name text NOT NULL,
    referent_user_id uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE environments (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    slug text NOT NULL CHECK (slug ~ '^[a-z][a-z0-9-]{1,40}$'),
    name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('dev', 'test', 'preprod', 'prod')),
    is_production boolean NOT NULL DEFAULT false,
    requires_distinct_approver boolean NOT NULL DEFAULT true,
    plan_ttl_seconds integer NOT NULL DEFAULT 14400 CHECK (plan_ttl_seconds > 0),
    UNIQUE (project_id, slug),
    CHECK (kind <> 'prod' OR is_production),
    CHECK (NOT is_production OR requires_distinct_approver)
);

-- Politiques d'environnement versionnées : une modification crée une nouvelle version.
CREATE TABLE environment_policies (
    id uuid PRIMARY KEY,
    environment_id uuid NOT NULL REFERENCES environments(id),
    version integer NOT NULL CHECK (version > 0),
    digest text NOT NULL,
    rules jsonb NOT NULL,
    created_by uuid,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (environment_id, version)
);

CREATE TABLE role_bindings (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    role text NOT NULL CHECK (role IN ('platform_admin', 'template_publisher', 'template_author', 'operator',
                                       'approver', 'auditor')),
    project_id uuid REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    granted_by uuid REFERENCES users(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revoked_by uuid REFERENCES users(id)
);
CREATE INDEX role_bindings_user ON role_bindings (user_id) WHERE revoked_at IS NULL;

CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    id_hash text NOT NULL UNIQUE,
    user_id uuid NOT NULL REFERENCES users(id),
    csrf_hash text NOT NULL,
    auth_method text NOT NULL,
    auth_time timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    idle_expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    privilege_epoch integer NOT NULL,
    revoked_at timestamptz,
    revoked_reason text
);
CREATE INDEX sessions_user ON sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE login_attempts (
    id bigserial PRIMARY KEY,
    email text NOT NULL,
    client text NOT NULL,
    succeeded boolean NOT NULL,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_attempts_recent ON login_attempts (email, at);

CREATE TABLE bootstrap_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);

CREATE TABLE connectors (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    kind text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('real', 'simulated')),
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    config_version integer NOT NULL DEFAULT 1,
    secret_refs jsonb NOT NULL DEFAULT '{}'::jsonb,
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_test_at timestamptz,
    last_test_result jsonb,
    error_count integer NOT NULL DEFAULT 0
);

CREATE TABLE approved_endpoints (
    id uuid PRIMARY KEY,
    connector_id uuid NOT NULL REFERENCES connectors(id),
    scheme text NOT NULL CHECK (scheme IN ('https', 'http')),
    host text NOT NULL,
    port integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
    ca_bundle_pem text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (connector_id, scheme, host, port)
);

CREATE TABLE resource_grants (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    kind text NOT NULL CHECK (kind IN ('network', 'image', 'cluster', 'datastore', 'folder', 'service_template')),
    stable_id text NOT NULL,
    granted_by uuid REFERENCES users(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (project_id, environment_id, kind, stable_id)
);

CREATE TABLE secrets (
    id uuid PRIMARY KEY,
    ref text NOT NULL UNIQUE,
    ciphertext bytea NOT NULL,
    key_id text NOT NULL,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE inventory_snapshots (
    id uuid PRIMARY KEY,
    connector_id uuid NOT NULL REFERENCES connectors(id),
    observed_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('ok', 'error')),
    item_count integer NOT NULL DEFAULT 0,
    error text
);

CREATE TABLE inventory_items (
    id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL REFERENCES inventory_snapshots(id),
    connector_id uuid NOT NULL REFERENCES connectors(id),
    kind text NOT NULL,
    stable_id text NOT NULL,
    name text NOT NULL,
    parent_stable_id text,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at timestamptz NOT NULL,
    UNIQUE (snapshot_id, kind, stable_id)
);

-- Registre : versions de composants et de templates Rackloom (profils de service du résolveur)
-- et artefacts techniques épinglés. Le contenu d'une version est immuable ; seul le statut évolue.
-- draft, lab_tested et in_review sont réservés à l'atelier (AT01).
CREATE TABLE component_versions (
    id uuid PRIMARY KEY,
    slug text NOT NULL,
    version text NOT NULL,
    status text NOT NULL DEFAULT 'validated'
        CHECK (status IN ('draft', 'lab_tested', 'in_review', 'validated', 'published', 'deprecated', 'withdrawn')),
    contract jsonb NOT NULL,
    implementation jsonb NOT NULL,
    verification_level text NOT NULL DEFAULT 'not_tested'
        CHECK (verification_level IN ('not_tested', 'tested_locally', 'lab_validated')),
    digest text NOT NULL,
    source_ref text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (slug, version)
);

CREATE TABLE template_versions (
    id uuid PRIMARY KEY,
    slug text NOT NULL,
    version text NOT NULL,
    status text NOT NULL DEFAULT 'validated'
        CHECK (status IN ('draft', 'lab_tested', 'in_review', 'validated', 'published', 'deprecated', 'withdrawn')),
    contract jsonb NOT NULL,
    digest text NOT NULL,
    source_ref text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    published_by uuid REFERENCES users(id),
    published_at timestamptz,
    publish_reason text,
    UNIQUE (slug, version)
);

CREATE TABLE template_events (
    id bigserial PRIMARY KEY,
    template_version_id uuid NOT NULL REFERENCES template_versions(id),
    action text NOT NULL CHECK (action IN ('loaded', 'published', 'deprecated', 'withdrawn')),
    actor_id uuid,
    reason text NOT NULL,
    impact jsonb NOT NULL DEFAULT '{}'::jsonb,
    at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog_artifacts (
    id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('tf_module', 'playbook', 'ansible_role')),
    slug text NOT NULL,
    version text NOT NULL,
    digest text NOT NULL,
    lockfile_sha256 text,
    manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_ref text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, slug, version)
);

CREATE TABLE runner_images (
    job_kind text PRIMARY KEY,
    image_digest text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE policy_bundles (
    version text PRIMARY KEY,
    digest text NOT NULL,
    source text NOT NULL CHECK (source IN ('builtin')),
    rules jsonb NOT NULL,
    activated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stacks (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    backend_kind text NOT NULL CHECK (backend_kind IN ('local_demo')),
    backend_ref text NOT NULL,
    lock_job_id uuid,
    lock_epoch integer NOT NULL DEFAULT 0,
    last_lineage text,
    last_serial integer
);

-- Un déploiement = une stack, une version de template épinglée. Son état de service est
-- calculé par le serveur à partir des contrôles de santé, indépendamment du succès des jobs.
CREATE TABLE deployments (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    template_slug text NOT NULL,
    template_version_id uuid NOT NULL REFERENCES template_versions(id),
    stack_id uuid NOT NULL UNIQUE REFERENCES stacks(id),
    created_by_change_id uuid NOT NULL,
    owner_id uuid NOT NULL REFERENCES users(id),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'failed', 'abandoned')),
    instances jsonb NOT NULL DEFAULT '[]'::jsonb,
    service_status text NOT NULL DEFAULT 'unknown'
        CHECK (service_status IN ('unknown', 'ready', 'created_not_ready', 'failed', 'verification_unavailable')),
    service_reason text,
    service_checked_at timestamptz,
    service_checks jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE changes (
    id uuid PRIMARY KEY,
    seq bigserial UNIQUE,
    kind text NOT NULL DEFAULT 'create' CHECK (kind IN ('create', 'modify')),
    deployment_id uuid REFERENCES deployments(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    title text NOT NULL DEFAULT '',
    state text NOT NULL,
    current_version_no integer NOT NULL DEFAULT 0,
    current_revision_no integer NOT NULL DEFAULT 0,
    row_version integer NOT NULL DEFAULT 0,
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (kind = 'create' OR deployment_id IS NOT NULL)
);
CREATE INDEX changes_project ON changes (project_id, created_at DESC);
ALTER TABLE deployments ADD FOREIGN KEY (created_by_change_id) REFERENCES changes(id) DEFERRABLE INITIALLY DEFERRED;

-- Besoin exprimé (catégorie A seulement), immuable.
CREATE TABLE request_versions (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    version_no integer NOT NULL,
    need jsonb NOT NULL,
    provenance jsonb NOT NULL,
    need_sha256 text NOT NULL,
    source text NOT NULL CHECK (source IN ('webui', 'chat', 'api', 'ci', 'duplicate')),
    created_by uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_id, version_no)
);

-- Révision : résolution technique d'une version du besoin par le résolveur, immuable.
CREATE TABLE request_revisions (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    revision_no integer NOT NULL,
    version_no integer NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('resolved', 'blocked', 'incomplete')),
    template_version_id uuid REFERENCES template_versions(id),
    environment_id uuid REFERENCES environments(id),
    environment_policy_version integer,
    policy_bundle_version text NOT NULL,
    inventory_snapshot_id uuid,
    inventory_observed_at timestamptz,
    resolved jsonb NOT NULL,
    provenance jsonb NOT NULL,
    block_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    resolution_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (change_id, revision_no)
);

CREATE FUNCTION rackloom_refuse_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % en ajout seul : % refusé', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER request_versions_immutable
    BEFORE UPDATE OR DELETE ON request_versions
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();
CREATE TRIGGER request_revisions_immutable
    BEFORE UPDATE OR DELETE ON request_revisions
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();
CREATE TRIGGER environment_policies_immutable
    BEFORE UPDATE OR DELETE ON environment_policies
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();
CREATE TRIGGER template_events_append_only
    BEFORE UPDATE OR DELETE ON template_events
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

-- Contenu des versions du registre immuable : seuls le statut et la publication évoluent.
CREATE FUNCTION rackloom_registry_content_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.digest <> OLD.digest OR NEW.contract <> OLD.contract
       OR NEW.slug <> OLD.slug OR NEW.version <> OLD.version THEN
        RAISE EXCEPTION 'version % %@% immuable : publiez une nouvelle version', TG_TABLE_NAME, OLD.slug,
            OLD.version USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER component_versions_content
    BEFORE UPDATE OR DELETE ON component_versions
    FOR EACH ROW EXECUTE FUNCTION rackloom_registry_content_immutable();
CREATE TRIGGER template_versions_content
    BEFORE UPDATE OR DELETE ON template_versions
    FOR EACH ROW EXECUTE FUNCTION rackloom_registry_content_immutable();

-- Noms réservés à la soumission, libérés si le changement se termine sans ressource créée.
CREATE TABLE name_reservations (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    name text NOT NULL,
    change_id uuid NOT NULL REFERENCES changes(id),
    revision_no integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz
);
CREATE UNIQUE INDEX name_reservations_active ON name_reservations (project_id, environment_id, name)
    WHERE released_at IS NULL;

CREATE TABLE state_transitions (
    id bigserial PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    from_state text,
    to_state text NOT NULL,
    reason_code text NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN ('user', 'system', 'worker')),
    actor_id text,
    at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX state_transitions_change ON state_transitions (change_id, id);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY,
    change_id uuid REFERENCES changes(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    kind text NOT NULL,
    path text NOT NULL,
    sha256 text NOT NULL,
    size bigint NOT NULL,
    key_class text NOT NULL CHECK (key_class IN ('A', 'B')),
    created_at timestamptz NOT NULL DEFAULT now(),
    retention_until timestamptz
);

CREATE TABLE plans (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    version_no integer NOT NULL,
    revision_no integer NOT NULL,
    stack_id uuid NOT NULL REFERENCES stacks(id),
    job_id uuid,
    status text NOT NULL CHECK (status IN ('ready', 'expired', 'superseded', 'applied', 'failed',
                                            'blocked_destructive', 'rejected', 'no_changes')),
    plan_sha256 text NOT NULL,
    artifact_id uuid NOT NULL REFERENCES artifacts(id),
    summary jsonb NOT NULL,
    counts jsonb NOT NULL,
    valid_until timestamptz NOT NULL,
    bundle jsonb NOT NULL,
    bundle_digest text NOT NULL,
    state_lineage text,
    state_serial integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX plans_change ON plans (change_id, created_at DESC);

CREATE TABLE retry_requests (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    execution_id uuid NOT NULL,
    step text NOT NULL CHECK (step IN ('ansible_configure')),
    host_ids jsonb NOT NULL,
    bundle jsonb NOT NULL,
    bundle_digest text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    requested_by uuid NOT NULL REFERENCES users(id),
    requested_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    revision_no integer NOT NULL,
    plan_id uuid REFERENCES plans(id),
    execution_id uuid,
    retry_request_id uuid REFERENCES retry_requests(id),
    kind text NOT NULL CHECK (kind IN ('plan', 'retry')),
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    bundle_digest text NOT NULL,
    decided_by uuid NOT NULL REFERENCES users(id),
    decided_at timestamptz NOT NULL DEFAULT now(),
    comment text NOT NULL DEFAULT '',
    session_id uuid NOT NULL,
    auth_context jsonb NOT NULL,
    retry_scope jsonb,
    revoked_at timestamptz,
    revoked_reason text
);
CREATE UNIQUE INDEX approvals_one_decision_per_plan ON approvals (plan_id) WHERE kind = 'plan';
CREATE UNIQUE INDEX approvals_one_decision_per_retry ON approvals (retry_request_id) WHERE kind = 'retry';

CREATE TABLE executions (
    id uuid PRIMARY KEY,
    change_id uuid NOT NULL REFERENCES changes(id),
    approval_id uuid NOT NULL UNIQUE REFERENCES approvals(id),
    plan_id uuid REFERENCES plans(id),
    bundle_digest text NOT NULL,
    state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    change_id uuid REFERENCES changes(id),
    project_id uuid REFERENCES projects(id),
    execution_id uuid REFERENCES executions(id),
    kind text NOT NULL CHECK (kind IN ('inventory_refresh', 'tf_plan', 'tf_apply', 'tf_state_read',
                                       'guest_ready', 'ansible_configure', 'verify', 'reconcile')),
    class text NOT NULL CHECK (class IN ('read', 'write')),
    state text NOT NULL CHECK (state IN ('queued', 'leased', 'running', 'succeeded', 'failed',
                                         'blocked', 'uncertain', 'cancelled')),
    stack_id uuid REFERENCES stacks(id),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL UNIQUE,
    priority integer NOT NULL DEFAULT 100,
    run_after timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    lease_owner text,
    lease_epoch integer NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    dispatched_at timestamptz,
    provider_ref text,
    result jsonb,
    blocked_reason text,
    cancel_requested_at timestamptz,
    cancel_requested_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX jobs_claimable ON jobs (priority, run_after) WHERE state = 'queued';
CREATE INDEX jobs_leases ON jobs (lease_expires_at) WHERE state IN ('leased', 'running');
CREATE INDEX jobs_change ON jobs (change_id, created_at);

CREATE TABLE job_steps (
    id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id),
    seq integer NOT NULL,
    name text NOT NULL,
    state text NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'skipped')),
    simulated boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, seq)
);

CREATE TABLE admission_decisions (
    id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id),
    change_id uuid REFERENCES changes(id),
    decided_at timestamptz NOT NULL DEFAULT now(),
    outcome text NOT NULL CHECK (outcome IN ('admitted', 'refused')),
    policy_bundle_version text NOT NULL,
    policy_bundle_digest text NOT NULL,
    rules jsonb NOT NULL
);
CREATE INDEX admission_decisions_job ON admission_decisions (job_id);

CREATE TABLE workers (
    id text PRIMARY KEY,
    hostname text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    slots integer NOT NULL,
    busy integer NOT NULL DEFAULT 0
);

CREATE TABLE trust_anchors (
    id uuid PRIMARY KEY,
    address text NOT NULL,
    key_type text NOT NULL,
    public_key text NOT NULL,
    fingerprint text NOT NULL,
    source text NOT NULL,
    imported_by uuid REFERENCES users(id),
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (address, key_type)
);

CREATE TABLE hosts (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    environment_id uuid NOT NULL REFERENCES environments(id),
    deployment_id uuid NOT NULL REFERENCES deployments(id),
    template_version_id uuid NOT NULL REFERENCES template_versions(id),
    change_id uuid NOT NULL REFERENCES changes(id),
    stack_id uuid NOT NULL REFERENCES stacks(id),
    vm_stable_id text NOT NULL,
    name text NOT NULL,
    address text,
    host_key_fingerprint text,
    enrolled_at timestamptz,
    deployment_state text NOT NULL DEFAULT 'created',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE host_observations (
    id bigserial PRIMARY KEY,
    host_id uuid NOT NULL REFERENCES hosts(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    kind text NOT NULL CHECK (kind IN ('facts', 'packages', 'services', 'probe', 'journal', 'ssh')),
    data jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('ok', 'error', 'unreachable')),
    source text NOT NULL,
    collected_at timestamptz NOT NULL DEFAULT now(),
    job_id uuid REFERENCES jobs(id)
);
CREATE INDEX host_observations_host ON host_observations (host_id, kind, collected_at DESC);

CREATE TABLE events (
    id bigserial PRIMARY KEY,
    correlation_id uuid,
    project_id uuid REFERENCES projects(id),
    environment_id uuid REFERENCES environments(id),
    job_id uuid REFERENCES jobs(id),
    source text NOT NULL,
    type text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    severity text NOT NULL DEFAULT 'info' CHECK (severity IN ('debug', 'info', 'warning', 'error')),
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key text NOT NULL UNIQUE
);
CREATE INDEX events_correlation ON events (correlation_id, id);
CREATE INDEX events_project ON events (project_id, id);

CREATE TABLE audit_log (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    actor_type text NOT NULL,
    actor_id text,
    action text NOT NULL,
    project_id uuid,
    correlation_id uuid,
    targets jsonb NOT NULL DEFAULT '{}'::jsonb,
    versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    policy_decision jsonb,
    outcome text NOT NULL,
    request_id text,
    prev_hash text NOT NULL,
    hash text NOT NULL
);

CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION rackloom_refuse_mutation();

CREATE TABLE idempotency_keys (
    principal_id uuid NOT NULL,
    key text NOT NULL,
    route text NOT NULL,
    request_sha256 text NOT NULL,
    status_code integer,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (principal_id, key)
);

CREATE TABLE chat_conversations (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    change_id uuid REFERENCES changes(id),
    user_id uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chat_messages (
    id bigserial PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES chat_conversations(id),
    role text NOT NULL CHECK (role IN ('user', 'assistant')),
    content text NOT NULL,
    adapter text,
    model text,
    validation_outcome text,
    data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE platform_settings (
    key text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Droits par rôle de connexion. Les rôles sont créés par l'initialisation de la base
-- (compose/postgres/init.sh) ou par les tests ; la migration ne fait que les GRANT.
DO $$
DECLARE
    t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_api') THEN
        GRANT USAGE ON SCHEMA public TO rackloom_api;
        FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                 AND tablename NOT IN ('secrets', 'audit_log', 'schema_migrations', 'alembic_version') LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_api', t);
        END LOOP;
        GRANT DELETE ON idempotency_keys, login_attempts TO rackloom_api;
        -- Secrets en écriture seule côté API : aucune lecture du texte chiffré.
        GRANT INSERT ON secrets TO rackloom_api;
        GRANT UPDATE (ciphertext, key_id, created_by, created_at) ON secrets TO rackloom_api;
        GRANT SELECT (id, ref, key_id, created_by, created_at) ON secrets TO rackloom_api;
        GRANT SELECT, INSERT ON audit_log TO rackloom_api;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rackloom_api;
        -- Lu par /health/ready ; la table existe déjà, Alembic la crée avant la première révision.
        GRANT SELECT ON alembic_version TO rackloom_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rackloom_worker') THEN
        GRANT USAGE ON SCHEMA public TO rackloom_worker;
        FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                 AND tablename NOT IN ('audit_log', 'schema_migrations', 'alembic_version', 'sessions',
                                       'login_attempts', 'bootstrap_tokens', 'idempotency_keys') LOOP
            EXECUTE format('GRANT SELECT, INSERT, UPDATE ON %I TO rackloom_worker', t);
        END LOOP;
        GRANT SELECT (id, user_id, revoked_at) ON sessions TO rackloom_worker;
        GRANT SELECT, INSERT ON audit_log TO rackloom_worker;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rackloom_worker;
        GRANT SELECT ON alembic_version TO rackloom_worker;
    END IF;
END;
$$;
