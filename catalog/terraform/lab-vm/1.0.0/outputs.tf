# Sorties limitées à la liste autorisée du template (outputs_allowlist).
output "instances" {
  value = {
    for name, vm in local.instances : name => {
      name    = name
      vm_uuid = random_uuid.vm[name].result
    }
  }
}
