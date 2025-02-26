#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""Kubernetes charm for eUPF."""

import json
import logging
from ipaddress import IPv4Address
from subprocess import check_output
from typing import Any, Dict, List, Optional, Tuple

import ops
import yaml
from charms.kubernetes_charm_libraries.v0.multus import (
    KubernetesMultusCharmLib,
    NetworkAnnotation,
    NetworkAttachmentDefinition,
)
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import (
    MetricsEndpointProvider,
)
from charms.sdcore_upf_k8s.v0.fiveg_n4 import N4Provides
from jinja2 import Environment, FileSystemLoader
from lightkube.models.meta_v1 import ObjectMeta
from ops import RemoveEvent
from ops.charm import CollectStatusEvent
from ops.model import ActiveStatus, BlockedStatus, ModelError, WaitingStatus
from ops.pebble import ConnectionError, ExecError, Layer

from charm_config import CharmConfig, CharmConfigInvalidError, CNIType
from kubernetes_eupf import EBPFVolume, PFCPService, get_upf_load_balancer_service_hostname

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.yaml"
CONFIG_PATH = "/etc/eupf"
PFCP_PORT = 8805
PROMETHEUS_PORT = 9090
N3_INTERFACE_BRIDGE_NAME = "access-br"
N6_INTERFACE_BRIDGE_NAME = "core-br"
N3_NETWORK_ATTACHMENT_DEFINITION_NAME = "n3-net"
N6_NETWORK_ATTACHMENT_DEFINITION_NAME = "n6-net"
N3_INTERFACE_NAME = "n3"
N6_INTERFACE_NAME = "n6"
LOGGING_RELATION_NAME = "logging"


def render_upf_config_file(
    interfaces: str,
    logging_level: str,
    pfcp_address: str,
    pfcp_port: int,
    n3_address: str,
    metrics_port: int,
    xdp_attach_mode: str,
) -> str:
    """Render the configuration file for the 5G UPF service.

    Args:
        interfaces: The interfaces to use.
        logging_level: The logging level.
        pfcp_address: The PFCP address.
        pfcp_port: The PFCP port.
        n3_address: The N3 address.
        metrics_port: The port for the metrics.
        xdp_attach_mode: The XDP attach mode.
    """
    jinja2_environment = Environment(loader=FileSystemLoader("src/templates/"))
    template = jinja2_environment.get_template(f"{CONFIG_FILE_NAME}.j2")
    content = template.render(
        interfaces=interfaces,
        logging_level=logging_level,
        pfcp_port=pfcp_port,
        n3_address=n3_address,
        metrics_port=metrics_port,
        pfcp_address=pfcp_address,
        xdp_attach_mode=xdp_attach_mode,
    )
    return content


class EupfK8SOperatorCharm(ops.CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self._container_name = self._service_name = "eupf"
        self._container = self.unit.get_container(self._container_name)
        self._logging = LogForwarder(charm=self, relation_name=LOGGING_RELATION_NAME)
        self.unit.set_ports(PROMETHEUS_PORT)
        try:
            self._charm_config: CharmConfig = CharmConfig.from_charm(charm=self)
        except CharmConfigInvalidError:
            return
        self._kubernetes_multus = KubernetesMultusCharmLib(
            namespace=self.model.name,
            statefulset_name=self.model.app.name,
            container_name=self._container_name,
            pod_name=self._pod_name,
            cap_net_admin=True,
            network_annotations=self._generate_network_annotations(),
            network_attachment_definitions=self._network_attachment_definitions_from_config(),
            privileged=True,
        )
        self._pfcp_service = PFCPService(
            namespace=self._namespace,
            service_name=f"{self.app.name}-external",
            app_name=self.app.name,
            pfcp_port=PFCP_PORT,
        )
        self._ebpf_volume = EBPFVolume(
            namespace=self.model.name,
            container_name=self._container_name,
            app_name=self.model.app.name,
            unit_name=self.model.unit.name,
        )
        self.fiveg_n4_provider = N4Provides(charm=self, relation_name="fiveg_n4")
        self._metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[
                {
                    "static_configs": [{"targets": [f"*:{PROMETHEUS_PORT}"]}],
                }
            ],
        )
        self.framework.observe(self.on.config_changed, self._configure)
        self.framework.observe(self.on.update_status, self._configure)
        self.framework.observe(self.fiveg_n4_provider.on.fiveg_n4_request, self._configure)
        self.framework.observe(self.on.remove, self._on_remove)

    def _on_collect_status(self, event: CollectStatusEvent):
        """Collect the status of the unit."""
        if not self.unit.is_leader():
            event.add_status(BlockedStatus("Scaling is not implemented for this charm"))
            logger.info("Scaling is not implemented for this charm")
            return
        try:
            self._charm_config: CharmConfig = CharmConfig.from_charm(charm=self)
        except CharmConfigInvalidError as exc:
            event.add_status(BlockedStatus(exc.msg))
            logger.info(exc.msg)
            return
        if not self._kubernetes_multus.multus_is_available():
            event.add_status(BlockedStatus("Multus is not installed or enabled"))
            logger.info("Multus is not installed or enabled")
            return
        if not self._kubernetes_multus.is_ready():
            event.add_status(WaitingStatus("Waiting for Multus to be ready"))
            return
        if not self._upf_config_file_is_written():
            event.add_status(WaitingStatus("Waiting for UPF configuration file"))
            return
        if not self._eupf_service_is_running():
            event.add_status(WaitingStatus("Waiting for UPF service to start"))
            return
        event.add_status(ActiveStatus())

    def _configure(self, event):
        """Handle state affecting events."""
        try:  # workaround for https://github.com/canonical/operator/issues/736
            self._charm_config: CharmConfig = CharmConfig.from_charm(charm=self)  # type: ignore[no-redef]  # noqa: E501
        except CharmConfigInvalidError:
            return
        if not self.unit.is_leader():
            logger.info("Not a leader, skipping configuration")
            return
        if not self._container.can_connect():
            logger.info("Cannot connect to the container")
            return
        self._configure_pfcp_service()
        if not self._kubernetes_multus.multus_is_available():
            logger.warning("Multus is not available")
            return
        self._kubernetes_multus.configure()
        self._configure_ebpf_volume()
        self._configure_routes()
        self._enable_ip_forwarding()
        restart = self._generate_config_file()
        self._configure_pebble(restart=restart)
        self._update_fiveg_n4_relation_data()

    @property
    def _namespace(self) -> str:
        """Return the k8s namespace."""
        return self.model.name

    @property
    def _pod_name(self) -> str:
        return "-".join(self.model.unit.name.rsplit("/", 1))

    def _on_remove(self, _: RemoveEvent) -> None:
        """Handle the removal of the charm.

        Delete the PFCP service.
        """
        if self._pfcp_service.is_created():
            self._pfcp_service.delete()

    def _configure_pfcp_service(self):
        if not self._pfcp_service.is_created():
            self._pfcp_service.create()

    def _configure_ebpf_volume(self):
        if not self._ebpf_volume.is_created():
            self._ebpf_volume.create()

    def _configure_routes(self):
        if not self._route_exists(
            dst="default",
            via=str(self._charm_config.n6_gateway_ip),
        ):
            self._create_default_route()
        if not self._route_exists(
            dst=str(self._charm_config.gnb_subnet),
            via=str(self._charm_config.n3_gateway_ip),
        ):
            self._create_ran_route()

    def _enable_ip_forwarding(self):
        _, stderr = self._exec_command_in_workload(command="sysctl -w net.ipv4.ip_forward=1")
        if stderr:
            logger.error("Failed to enable ip forwarding: %s", stderr)
            return
        logger.info("IP forwarding enabled")

    def _route_exists(self, dst: str, via: str | None) -> bool:
        """Return whether the specified route exist."""
        try:
            stdout, stderr = self._exec_command_in_workload(command="ip route show")
        except ExecError as e:
            logger.error("Failed retrieving routes: %s", e.stderr)
            return False
        for line in stdout.splitlines():
            if f"{dst} via {via}" in line:
                return True
        return False

    def _create_default_route(self) -> None:
        """Create ip route towards core network."""
        try:
            self._exec_command_in_workload(
                command=f"ip route replace default via {self._charm_config.n6_gateway_ip} metric 110"
            )
        except ExecError as e:
            logger.error("Failed to create core network route: %s", e.stderr)
            return
        logger.info("Default core network route created")

    def _create_ran_route(self) -> None:
        """Create ip route towards gnb-subnet."""
        try:
            self._exec_command_in_workload(
                command=f"ip route replace {self._charm_config.gnb_subnet} via {self._charm_config.n3_gateway_ip}"
            )
        except ExecError as e:
            logger.error("Failed to create route to gnb-subnet: %s", e.stderr)
            return
        logger.info("Route to gnb-subnet created")

    def _exec_command_in_workload(
        self, command: str, timeout: Optional[int] = 30, environment: Optional[dict] = None
    ) -> Tuple[str, str | None]:
        process = self._container.exec(
            command=command.split(),
            timeout=timeout,
            environment=environment,
        )
        return process.wait_output()

    def _eupf_service_is_running(self) -> bool:
        try:
            return self._container.get_service(self._service_name).is_running()
        except ModelError:
            return False

    def _upf_config_file_is_written(self) -> bool:
        if not self._container.can_connect():
            return False
        try:
            return self._container.exists(path=f"{CONFIG_PATH}/{CONFIG_FILE_NAME}")
        except ConnectionError:
            return False

    def _upf_config_file_content_matches(self, content: str) -> bool:
        try:
            existing_content = self._container.pull(path=f"{CONFIG_PATH}/{CONFIG_FILE_NAME}")
        except ConnectionError:
            return False
        try:
            return yaml.safe_load(existing_content) == yaml.safe_load(content)
        except yaml.YAMLError:
            return False

    def _write_upf_config_file(self, content: str) -> None:
        try:
            self._container.push(path=f"{CONFIG_PATH}/{CONFIG_FILE_NAME}", source=content)
            logger.info("Pushed %s config file", CONFIG_FILE_NAME)
        except ConnectionError:
            logger.info("Failed to push %s config file", CONFIG_FILE_NAME)

    def _generate_config_file(self) -> bool:
        """Generate the configuration file for the UPF service.

        Returns:
            bool: Whether the configuration file was written.
        """
        pfcp_address = get_pod_ip()
        content = render_upf_config_file(
            interfaces=self._charm_config.interfaces,
            logging_level=self._charm_config.logging_level,
            pfcp_address=pfcp_address,
            pfcp_port=PFCP_PORT,
            n3_address=str(self._charm_config.n3_ip),
            metrics_port=PROMETHEUS_PORT,
            xdp_attach_mode=self._charm_config.xdp_attach_mode,
        )
        if not self._upf_config_file_is_written() or not self._upf_config_file_content_matches(
            content=content
        ):
            self._write_upf_config_file(content=content)
            return True
        return False

    def _update_fiveg_n4_relation_data(self) -> None:
        fiveg_n4_relations = self.model.relations.get("fiveg_n4")
        if not fiveg_n4_relations:
            logger.info("No `fiveg_n4` relations found.")
            return
        for fiveg_n4_relation in fiveg_n4_relations:
            self.fiveg_n4_provider.publish_upf_n4_information(
                relation_id=fiveg_n4_relation.id,
                upf_hostname=self._get_n4_upf_hostname(),
                upf_n4_port=PFCP_PORT,
            )

    def _get_n4_upf_hostname(self) -> str:
        if configured_hostname := self._charm_config.external_hostname:
            return configured_hostname
        elif lb_hostname := get_upf_load_balancer_service_hostname(
            namespace=self.model.name, app_name=self.model.app.name
        ):
            return lb_hostname
        return self._upf_hostname

    @property
    def _upf_hostname(self) -> str:
        return f"{self.model.app.name}-external.{self.model.name}.svc.cluster.local"

    def _configure_pebble(self, restart: bool) -> None:
        """Configure the Pebble layer.

        Args:
            restart (bool): Whether to restart the container.
        """
        try:
            plan = self._container.get_plan()
        except ConnectionError:
            logger.info("Failed to get plan: Connection error")
            return
        if plan.services != self._pebble_layer.services:
            try:
                self._container.add_layer(self._container_name, self._pebble_layer, combine=True)
            except ConnectionError:
                logger.info("Failed to add new layer: Connection error")
                return
            try:
                self._container.replan()
                logger.info("New layer added: %s", self._pebble_layer)
            except ConnectionError:
                logger.info("Failed to add new layer: Connection error")
                return
        if restart:
            try:
                self._container.restart(self._service_name)
                logger.info("Restarted container %s", self._service_name)
            except ConnectionError:
                logger.info("Failed to restart container: Connection error")
            return

    @property
    def _pebble_layer(self) -> Layer:
        """Return pebble layer for the container."""
        return Layer(
            {
                "services": {
                    self._service_name: {
                        "override": "replace",
                        "startup": "enabled",
                        "command": f"/bin/eupf --config {CONFIG_PATH}/{CONFIG_FILE_NAME}",
                    },
                },
            }
        )

    def _generate_network_annotations(self) -> List[NetworkAnnotation]:
        n3_network_annotation = NetworkAnnotation(
            name=N3_NETWORK_ATTACHMENT_DEFINITION_NAME,
            interface=N3_INTERFACE_NAME,
        )
        n6_network_annotation = NetworkAnnotation(
            name=N6_NETWORK_ATTACHMENT_DEFINITION_NAME,
            interface=N6_INTERFACE_NAME,
        )
        return [n3_network_annotation, n6_network_annotation]

    def _network_attachment_definitions_from_config(self) -> list[NetworkAttachmentDefinition]:
        """Return list of Multus NetworkAttachmentDefinitions to be created based on config.

        Returns:
            network_attachment_definitions: list[NetworkAttachmentDefinition]
        """
        access_nad = self._create_nad_from_config(N3_INTERFACE_NAME)
        core_nad = self._create_nad_from_config(N6_INTERFACE_NAME)
        return [access_nad, core_nad]

    def _create_nad_from_config(self, interface_name: str) -> NetworkAttachmentDefinition:
        """Return a NetworkAttachmentDefinition for the specified interface.

        Args:
            interface_name (str): Interface name to create the NetworkAttachmentDefinition from

        Returns:
            NetworkAttachmentDefinition: NetworkAttachmentDefinition object
        """
        nad_config = self._get_nad_base_config()

        nad_config["ipam"].update(
            {"addresses": [{"address": self._get_network_ip_config(interface_name)}]}
        )

        cni_type = self._charm_config.cni_type

        # host interface name is used only by macvlan and host-device
        if host_interface := self._get_interface_config(interface_name):
            if cni_type == CNIType.macvlan:
                nad_config.update({"master": host_interface})
        else:
            nad_config.update(
                {
                    "bridge": (
                        N3_INTERFACE_BRIDGE_NAME
                        if interface_name == N3_INTERFACE_NAME
                        else N6_INTERFACE_BRIDGE_NAME
                    )
                }
            )
        nad_config.update({"type": cni_type})

        return NetworkAttachmentDefinition(
            metadata=ObjectMeta(
                name=(
                    N6_NETWORK_ATTACHMENT_DEFINITION_NAME
                    if interface_name == N6_INTERFACE_NAME
                    else N3_NETWORK_ATTACHMENT_DEFINITION_NAME
                )
            ),
            spec={"config": json.dumps(nad_config)},
        )

    def _get_nad_base_config(self) -> Dict[Any, Any]:
        """Get the base NetworkAttachmentDefinition.

        This config is extended according to charm config.

        Returns:
            config (dict): Base NAD config
        """
        base_nad = {
            "cniVersion": "0.3.1",
            "ipam": {
                "type": "static",
            },
            "capabilities": {"mac": True},
        }
        return base_nad

    def _get_interface_config(self, interface_name: str) -> Optional[str]:
        """Retrieve the interface on the host to use for the specified interface.

        Args:
            interface_name (str): Interface name to retrieve the interface host from

        Returns:
            Optional[str]: The interface on the host to use
        """
        if interface_name == N3_INTERFACE_NAME:
            return self._charm_config.n3_interface
        elif interface_name == N6_INTERFACE_NAME:
            return self._charm_config.n6_interface
        else:
            return None

    def _get_network_ip_config(self, interface_name: str) -> Optional[str]:
        """Retrieve the network IP address to use for the specified interface.

        Args:
            interface_name (str): Interface name to retrieve the network IP address from

        Returns:
            Optional[str]: The network IP address to use
        """
        if interface_name == N3_INTERFACE_NAME:
            return str(self._charm_config.n3_ip)
        elif interface_name == N6_INTERFACE_NAME:
            return str(self._charm_config.n6_ip)
        else:
            return None


def get_pod_ip() -> str:
    """Return the pod IP using juju client."""
    ip_address = check_output(["unit-get", "private-address"])
    return str(IPv4Address(ip_address.decode().strip())) if ip_address else ""


if __name__ == "__main__":  # pragma: nocover
    ops.main(EupfK8SOperatorCharm)  # type: ignore
