# eUPF K8s Operator

Juju charm for operating eUPF on Kubernetes.

## Usage

### Enable Multus
Add the community repository MicroK8s addon:

```
sudo microk8s addons repo add community https://github.com/canonical/microk8s-community-addons --reference feat/stri
```

Enable the following MicroK8s Multus Addon.

```
sudo microk8s enable multus
```

### Deploy eUPF

Deploy the eUPF charm:

```
juju deploy eupf-k8s --trust
```
