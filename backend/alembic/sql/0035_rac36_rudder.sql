-- RAC-36 : job rudder_import (lecture seule) pour le connecteur Rudder HTTP.

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('inventory_refresh', 'preflight', 'tf_plan', 'tf_apply', 'tf_state_read',
                    'guest_ready', 'ansible_configure', 'verify', 'reconcile', 'drift_scan',
                    'vm_restore', 'service_restart', 'lease_retire', 'tf_import',
                    'host_patch', 'patch_check', 'compliance_scan', 'rudder_import'));

ALTER TABLE compliance_scans DROP CONSTRAINT IF EXISTS compliance_scans_source_check;
ALTER TABLE compliance_scans ADD CONSTRAINT compliance_scans_source_check
    CHECK (source IN ('rackloom', 'rudder_fake', 'rudder', 'system'));
