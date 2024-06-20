#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""Kubernetes charm for eUPF."""

import json
import logging
from ipaddress import IPv4Address
from subprocess import check_output
from typing import List, Optional, Tuple

import ops
import yaml
from charm_config import CharmConfig, CharmConfigInvalidError
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
from kubernetes_eupf import EBPFVolume, PFCPService, get_upf_load_balancer_service_hostname
from lightkube.models.meta_v1 import ObjectMeta
from ops import RemoveEvent
from ops.charm import CharmEvents, CollectStatusEvent
from ops.framework import EventBase, EventSource
from ops.model import ActiveStatus, ModelError, WaitingStatus
from ops.pebble import ConnectionError, ExecError, Layer

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

) -> str:
    """Render the configuration file for the 5G UPF service.

    Args:
        interfaces: The interfaces to use.
        logging_level: The logging level.
        pfcp_address: The PFCP address.
        pfcp_port: The PFCP port.
        n3_address: The N3 address.
        metrics_port: The port for the metrics.
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
    )
    return content

class NadConfigChangedEvent(EventBase):
    """Event triggered when an existing network attachment definition is changed."""


class UpfOperatorCharmEvents(CharmEvents):
    """Kubernetes UPF operator charm events."""

    nad_config_changed = EventSource(NadConfigChangedEvent)


class EupfK8SOperatorCharm(ops.CharmBase):
    """Charm the service."""

    on = UpfOperatorCharmEvents()  # type: ignore[reportAssignmentType]

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
            charm=self,
            container_name=self._container_name,
            cap_net_admin=True,
            network_annotations_func=self._generate_network_annotations,
            network_attachment_definitions_func=self._network_attachment_definitions_from_config,
            refresh_event=self.on.nad_config_changed,
            privileged=True,
        )
        self._pfcp_service = PFCPService(
            namespace=self.model.name,
            app_name=self.model.app.name,
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
        self.framework.observe(
            self.fiveg_n4_provider.on.fiveg_n4_request, self._configure
        )
        self.framework.observe(self.on.remove, self._on_remove)

    def _on_collect_status(self, event: CollectStatusEvent):
        """Collect the status of the unit."""
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
        if not self._kubernetes_multus.multus_is_available():
            return
        self.on.nad_config_changed.emit()
        if not self._pfcp_service.is_created():
            self._pfcp_service.create()
        if not self._ebpf_volume.is_created():
            self._ebpf_volume.create()
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
        restart = self._generate_config_file()
        self._configure_pebble(restart=restart)
        self._update_fiveg_n4_relation_data()

    def _on_remove(self, _: RemoveEvent) -> None:
        """Handle the removal of the charm.

        Delete the PFCP service.
        """
        if self._pfcp_service.is_created():
            self._pfcp_service.delete()

    def _accept_forwarded_packets(self) -> None:
        """Configure packet filtering rules to accept all forwarded packets."""
        try:
            self._exec_command_in_workload(
                command="iptables-legacy -A FORWARD -j ACCEPT"
            )
        except ExecError as e:
            logger.error("Failed to run iptables command: %s", e.stderr)
            return
        except FileNotFoundError:
            logger.error("Failed to execute command. File not found.")
            return
        logger.info("Iptables command ran successfully")

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
        if lb_hostname := get_upf_load_balancer_service_hostname(namespace=self.model.name, app_name=self.model.app.name):
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
                self._container.add_layer(
                    self._container_name, self._pebble_layer, combine=True
                )
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
        n3_nad_config= {
            "cniVersion": "0.3.1",
            "ipam": {
                "type": "static",
                "addresses": [
                    {"address": f"{self._charm_config.n3_ip}/24"},
                ],
            },
            "capabilities": {"mac": True},
            "type": "bridge",
            "bridge": N3_INTERFACE_BRIDGE_NAME
        }
        n6_nad_config = {
            "cniVersion": "0.3.1",
            "ipam": {
                "type": "static",
                "addresses": [
                    {"address": f"{self._charm_config.n6_ip}/24"},
                ],
            },
            "capabilities": {"mac": True},
            "type": "bridge",
            "bridge": N6_INTERFACE_BRIDGE_NAME
        }

        n3_nad = NetworkAttachmentDefinition(
            metadata=ObjectMeta(
                name=(
                    N3_NETWORK_ATTACHMENT_DEFINITION_NAME
                )
            ),
            spec={"config": json.dumps(n3_nad_config)},
        )
        n6_nad = NetworkAttachmentDefinition(
            metadata=ObjectMeta(
                name=(
                    N6_NETWORK_ATTACHMENT_DEFINITION_NAME
                )
            ),
            spec={"config": json.dumps(n6_nad_config)},
        )
        return [n3_nad, n6_nad]


def get_pod_ip() -> str:
    """Return the pod IP using juju client."""
    ip_address = check_output(["unit-get", "private-address"])
    return str(IPv4Address(ip_address.decode().strip())) if ip_address else ""


if __name__ == "__main__":  # pragma: nocover
    ops.main(EupfK8SOperatorCharm)  # type: ignore
