#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""Kubernetes charm for eUPF."""

import logging
from ipaddress import IPv4Address
from subprocess import check_output

import ops
import yaml
from charm_config import CharmConfig, CharmConfigInvalidError
from charms.kubernetes_charm_libraries.v0.multus import KubernetesMultusCharmLib
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import (
    MetricsEndpointProvider,
)
from charms.sdcore_upf_k8s.v0.fiveg_n4 import N4Provides
from jinja2 import Environment, FileSystemLoader
from kubernetes_eupf import EBPFVolume
from ops.charm import CollectStatusEvent
from ops.model import ActiveStatus, ModelError, Port, WaitingStatus
from ops.pebble import ConnectionError, Layer

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.yaml"
CONFIG_PATH = "/etc/eupf"
PFCP_PORT = 8805
PROMETHEUS_PORT = 9090
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
        pfcp_address=pfcp_address,
        pfcp_port=pfcp_port,
        n3_address=n3_address,
        metrics_port=metrics_port,
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
        pfcp_port = Port(protocol="udp", port=PFCP_PORT)
        self.unit.set_ports(PROMETHEUS_PORT, pfcp_port)
        try:
            self._charm_config: CharmConfig = CharmConfig.from_charm(charm=self)
        except CharmConfigInvalidError:
            return
        self._kubernetes_multus = KubernetesMultusCharmLib(
            charm=self,
            container_name=self._container_name,
            cap_net_admin=True,
            refresh_event=self.on.config_changed,
            privileged=True,
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
        if not self._ebpf_volume.is_created():
            self._ebpf_volume.create()
        restart = self._generate_config_file()
        self._configure_pebble(restart=restart)
        self._update_fiveg_n4_relation_data()

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
        content = render_upf_config_file(
            interfaces="[eth0]",
            logging_level=self._charm_config.logging_level,
            pfcp_address=get_pod_ip(),
            pfcp_port=PFCP_PORT,
            n3_address=get_pod_ip(),
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
                upf_hostname=self._upf_hostname,
                upf_n4_port=PFCP_PORT,
            )

    @property
    def _upf_hostname(self) -> str:
        return f"{self.model.app.name}.{self.model.name}.svc.cluster.local"

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

def get_pod_ip() -> str:
    """Return the pod IP using juju client."""
    ip_address = check_output(["unit-get", "private-address"])
    return str(IPv4Address(ip_address.decode().strip())) if ip_address else ""

if __name__ == "__main__":  # pragma: nocover
    ops.main(EupfK8SOperatorCharm)  # type: ignore
