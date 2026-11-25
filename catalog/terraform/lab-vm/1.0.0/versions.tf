terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "2.5.3"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.13.1"
    }
  }
}
