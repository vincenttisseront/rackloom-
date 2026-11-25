variable "deployment_id" {
  type = string
}

variable "labvms_dir" {
  description = "Dossier jetable du déploiement, fourni par le runner."
  type        = string
}

variable "lab_delay_seconds" {
  description = "Délai artificiel par instance, réservé aux tests d'interruption et d'annulation."
  type        = number
  default     = 0

  validation {
    condition     = var.lab_delay_seconds >= 0 && var.lab_delay_seconds <= 120
    error_message = "lab_delay_seconds doit être compris entre 0 et 120."
  }
}

variable "instances" {
  description = "Instances générées depuis la révision résolue."
  type = list(object({
    name        = string
    size_tier   = string
    vcpu        = number
    memory_mb   = number
    disk_gb     = number
    image_ref   = string
    network_ref = string
  }))

  validation {
    condition     = length(var.instances) >= 1 && length(var.instances) <= 2
    error_message = "Entre 1 et 2 instances par déploiement."
  }
}
