# Copyright 2025 Guillaume Belanger
# See LICENSE file for licensing details.

terraform {
  required_providers {
    juju = {
      source  = "juju/juju"
      version = ">= 0.11.0"
    }
  }
}
