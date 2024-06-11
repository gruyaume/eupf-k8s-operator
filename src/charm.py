#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""Kubernetes charm for eUPF."""

import logging

import ops
import yaml
from charm_config import CharmConfig, CharmConfigInvalidError
from jinja2 import Environment, FileSystemLoader
from ops.charm import CollectStatusEvent
from ops.model import ActiveStatus, WaitingStatus

logger = logging.getLogger(__name__)

EUPF_CONFIG_FILE_NAME = "config.yaml"
EUPF_CONFIG_PATH = "/etc/eupf"
PFCP_PORT = 8805
PROMETHEUS_PORT = 9090
INTERFACE_NAME = "lo"


def render_upf_config_file(
    pfcp_address: str,
    n3_address: str,
    interface_name: str,
    metrics_port: int,
) -> str:
    """Render the configuration file for the 5G UPF service.

    Args:
        pfcp_address: The PFCP address.
        n3_address: The N3 address.
        interface_name: The interface name.
        metrics_port: The port for the metrics.
    """
    jinja2_environment = Environment(loader=FileSystemLoader("src/templates/"))
    template = jinja2_environment.get_template(f"{EUPF_CONFIG_FILE_NAME}.j2")
    content = template.render(
        pfcp_address=pfcp_address,
        n3_address=n3_address,
        interface_name=interface_name,
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
        try:
            self._charm_config: CharmConfig = CharmConfig.from_charm(charm=self)
        except CharmConfigInvalidError:
            return
        self.framework.observe(self.on.config_changed, self._configure)

    def _on_collect_status(self, event: CollectStatusEvent):
        """Collect the status of the unit."""
        if not self._upf_config_file_is_written():
            event.add_status(WaitingStatus("Waiting for UPF configuration file"))
            return
        event.add_status(ActiveStatus())

    def _configure(self, event):
        """Handle state affecting events."""
        if not self.unit.is_leader():
            logger.info("Not a leader, skipping configuration")
            return
        if not self._container.can_connect():
            logger.info("Cannot connect to the container")
            return
        self._generate_config_file()

    def _upf_config_file_is_written(self) -> bool:
        if not self._container.can_connect():
            return False
        return self._container.exists(path=f"{EUPF_CONFIG_PATH}/{EUPF_CONFIG_FILE_NAME}")

    def _upf_config_file_content_matches(self, content: str) -> bool:
        existing_content = self._container.pull(path=f"{EUPF_CONFIG_PATH}/{EUPF_CONFIG_FILE_NAME}")
        try:
            return yaml.safe_load(existing_content) == yaml.safe_load(content)
        except yaml.YAMLError:
            return False

    def _write_upf_config_file(self, content: str) -> None:
        self._container.push(path=f"{EUPF_CONFIG_PATH}/{EUPF_CONFIG_FILE_NAME}", source=content)
        logger.info("Pushed %s config file", EUPF_CONFIG_FILE_NAME)

    def _generate_config_file(self) -> None:
        content = render_upf_config_file(
            pfcp_address=f"{self._charm_config.core_ip}:{PFCP_PORT}",
            n3_address=self._charm_config.access_ip,
            interface_name=INTERFACE_NAME,
            metrics_port=PROMETHEUS_PORT,
        )
        if not self._upf_config_file_is_written() or not self._upf_config_file_content_matches(
            content=content
        ):
            self._write_upf_config_file(content=content)


if __name__ == "__main__":  # pragma: nocover
    ops.main(EupfK8SOperatorCharm)  # type: ignore
