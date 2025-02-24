# Copyright 2025 Guillaume Belanger
# See LICENSE file for licensing details.

output "app_name" {
  description = "Name of the deployed application."
  value       = juju_application.eupf.name
}

output "requires" {
  value = {
    logging = "logging"
  }
}

output "provides" {
  value = {
    metrics  = "metrics-endpoint"
    fiveg_n3 = "fiveg_n3"
    fiveg_n4 = "fiveg_n4"
  }
}
