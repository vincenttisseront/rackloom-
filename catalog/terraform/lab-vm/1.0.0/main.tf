# Module de labo : chaque instance est une ressource locale jetable, pas une machine.
# Terraform gère réellement le state, le plan, le diff et le verrou ; rien n'est créé
# sur vSphere. Changer le niveau d'une instance existante force un remplacement (bloqué en v1).

locals {
  instances = { for vm in var.instances : vm.name => vm }
}

resource "random_uuid" "vm" {
  for_each = local.instances

  keepers = {
    name      = each.key
    size_tier = each.value.size_tier
  }
}

resource "time_sleep" "provision" {
  for_each = local.instances

  create_duration = "${var.lab_delay_seconds}s"

  triggers = {
    vm_uuid = random_uuid.vm[each.key].result
  }
}

resource "local_file" "vm" {
  for_each = local.instances

  filename        = "${var.labvms_dir}/${each.key}.json"
  file_permission = "0640"
  content = jsonencode({
    kind          = "rackloom.lab_vm"
    uuid          = random_uuid.vm[each.key].result
    name          = each.key
    deployment_id = var.deployment_id
    size_tier     = each.value.size_tier
    vcpu          = each.value.vcpu
    memory_mb     = each.value.memory_mb
    disk_gb       = each.value.disk_gb
    image_ref     = each.value.image_ref
    network_ref   = each.value.network_ref
  })

  depends_on = [time_sleep.provision]
}
