# Copyright 2025 Guillaume Belanger
# See LICENSE file for licensing details.

resource "juju_application" "eupf" {
  name  = var.app_name
  model = var.model

  charm {
    name     = "eupf-k8s"
    channel  = var.channel
    revision = var.revision
    base     = var.base
  }

  config      = var.config
  constraints = var.constraints
  units       = var.units
  resources   = var.resources
  trust       = true
}
