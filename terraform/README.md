# eUPF K8s Terraform Module

This folder contains a base [Terraform][Terraform] module for the eupf-k8s charm.

The module uses the [Terraform Juju provider][Terraform Juju provider] to model the charm
deployment onto any Kubernetes environment managed by [Juju][Juju].

The module can be used to deploy the eupf separately as well as a part of a higher level module,
depending on the deployment architecture.

## Module structure

- **main.tf** - Defines the Juju application to be deployed.
- **variables.tf** - Allows customization of the deployment. Except for exposing the deployment
  options (Juju model name, channel or application name) also allows overwriting charm's default
  configuration.
- **output.tf** - Responsible for integrating the module with other Terraform modules, primarily
  by defining potential integration endpoints (charm integrations), but also by exposing
  the application name.
- **versions.tf** - Defines the Terraform provider.

## Deploying eupf-k8s base module separately

### Pre-requisites

- A Kubernetes cluster with the Multus addon enabled.
- Juju 3.x
- Juju controller bootstrapped onto the K8s cluster
- Terraform

### Deploying eUPF with Terraform

Clone the `eupf-k8s-operator` Git repository.

From inside the `terraform` folder, initialize the provider:

```shell
terraform init
```

Create Terraform plan:

```shell
terraform plan
```

While creating the plan, the default configuration can be overwritten with `-var-file`. To do that,
Terraform `tfvars` file should be prepared prior to the plan creation.

Deploy eUPF:

```console
terraform apply -auto-approve 
```

### Cleaning up

Destroy the deployment:

```shell
terraform destroy -auto-approve
```

## Using eupf-k8s base module in higher level modules

If you want to use `eupf-k8s` base module as part of your Terraform module, import it
like shown below:

```text
data "juju_model" "my_model" {
  name = "my_model_name"
}

module "upf" {
  source = "git::https://github.com/gruyaume/eupf-k8s-operator//terraform"
  
  model = juju_model.my_model.name
  config = Optional config map
}
```

Create integrations, for instance:

```text
resource "juju_integration" "eupf-nms" {
  model = juju_model.my_model.name
  application {
    name     = module.eupf.app_name
    endpoint = module.eupf.provides.fiveg_n4
  }
  application {
    name     = module.nms.app_name
    endpoint = module.nms.requires.fiveg_n4
  }
}
```

The complete list of available integrations can be found [here][eupf-integrations].

[Terraform]: https://www.terraform.io/
[Terraform Juju provider]: https://registry.terraform.io/providers/juju/juju/latest
[Juju]: https://juju.is
[eupf-integrations]: https://charmhub.io/eupf-k8s/integrations
