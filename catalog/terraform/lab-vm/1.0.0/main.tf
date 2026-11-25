# Module de labo : chaque "VM" est une ressource locale jetable, pas une machine.
# Terraform gère réellement le state, le plan, le diff et le verrou ; rien n'est créé
# sur vSphere. Changer la taille d'une VM existante force un remplacement (bloqué en v1).

variable "stack_id" {
  type = string
}

variable "labvms_dir" {
  description = "Dossier jetable de la stack, fourni par le runner."
  type        = string
}

variable "lab_delay_seconds" {
  description = "Délai artificiel par VM pour les tests d'interruption et d'annulation."
  type        = number
  default     = 0

  validation {
    condition     = var.lab_delay_seconds >= 0 && var.lab_delay_seconds <= 120
    error_message = "lab_delay_seconds doit être compris entre 0 et 120."
  }
}

variable "vms" {
  type = list(object({
    name         = string
    size         = string
    cpu          = number
    memory_mb    = number
    template_ref = string
    network_ref  = string
  }))

  validation {
    condition     = length(var.vms) >= 1 && length(var.vms) <= 2
    error_message = "Entre 1 et 2 VM par demande."
  }
}

locals {
  vms = { for vm in var.vms : vm.name => vm }
}

resource "random_uuid" "vm" {
  for_each = local.vms

  keepers = {
    name = each.key
    size = each.value.size
  }
}

resource "time_sleep" "provision" {
  for_each = local.vms

  create_duration = "${var.lab_delay_seconds}s"

  triggers = {
    vm_uuid = random_uuid.vm[each.key].result
  }
}

resource "local_file" "vm" {
  for_each = local.vms

  filename        = "${var.labvms_dir}/${each.key}.json"
  file_permission = "0640"
  content = jsonencode({
    kind         = "rackloom.lab_vm"
    uuid         = random_uuid.vm[each.key].result
    name         = each.key
    stack_id     = var.stack_id
    size         = each.value.size
    cpu          = each.value.cpu
    memory_mb    = each.value.memory_mb
    template_ref = each.value.template_ref
    network_ref  = each.value.network_ref
  })

  depends_on = [time_sleep.provision]
}

output "vms" {
  value = {
    for name, vm in local.vms : name => {
      uuid   = random_uuid.vm[name].result
      file   = local_file.vm[name].filename
      sha256 = local_file.vm[name].content_sha256
    }
  }
}
