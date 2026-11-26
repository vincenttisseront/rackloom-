-- F31 : job preflight (classe read) avant tf_plan ; backends de state HTTP pour sonde L1.

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
    'guest_ready', 'ansible_configure', 'verify', 'reconcile'
));

ALTER TABLE stacks DROP CONSTRAINT IF EXISTS stacks_backend_kind_check;
ALTER TABLE stacks ADD CONSTRAINT stacks_backend_kind_check CHECK (
    backend_kind IN ('local_demo', 'http', 'https', 'remote_http')
);
