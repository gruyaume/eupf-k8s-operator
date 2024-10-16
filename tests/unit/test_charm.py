# Copyright 2024 Guillaume Belanger
# See LICENSE file for licensing details.

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from ops import WaitingStatus, testing

from charm import EupfK8SOperatorCharm

NAMESPACE = "whatever"


def read_file(path: str) -> str:
    """Read a file and returns as a string."""
    with open(path, "r") as f:
        content = f.read()
    return content


class TestCharm:
    patcher_check_output = patch("charm.check_output")
    patcher_k8s_eupf_service = patch("charm.PFCPService")
    patcher_k8s_ebpf = patch("charm.EBPFVolume")
    patcher_k8s_get_upf_load_balancer_service_hostname = patch(
        "charm.get_upf_load_balancer_service_hostname"
    )
    patcher_k8s_multus = patch("charm.KubernetesMultusCharmLib")

    @pytest.fixture()
    def setUp(self):
        TestCharm.patcher_k8s_eupf_service.start()
        TestCharm.patcher_k8s_ebpf.start()
        TestCharm.patcher_k8s_multus.start()
        TestCharm.patcher_k8s_get_upf_load_balancer_service_hostname.start()
        self.mock_check_output = TestCharm.patcher_check_output.start()

    @pytest.fixture(autouse=True)
    def harness_fixture(self, setUp):
        self._container_name = "eupf"
        self.harness = testing.Harness(EupfK8SOperatorCharm)
        self.harness.set_model_name(name=NAMESPACE)
        self.harness.set_leader(is_leader=True)
        self.harness.begin()
        yield self.harness
        self.harness.cleanup()

    @staticmethod
    def tearDown() -> None:
        patch.stopall()

    @pytest.fixture()
    def add_storage(self):
        self.harness.add_storage(storage_name="config", attach=True)

    def _push_configuration_file_to_workload(self):
        root = self.harness.get_filesystem_root(container=self._container_name)
        expected_config_file_path = Path(__file__).parent / "expected_config.yaml"
        with open(expected_config_file_path, "r") as expected_config_file:
            (root / "etc/eupf/config.yaml").write_text(expected_config_file.read())

    def test_given_fiveg_config_file_not_created_when_evaluate_status_then_status_is_waiting(self):
        self.harness.set_can_connect(container=self._container_name, val=False)

        self.harness.evaluate_status()

        assert self.harness.model.unit.status == WaitingStatus(
            "Waiting for UPF configuration file"
        )

    def test_given_eupf_service_not_running_when_evaluate_status_then_status_is_waiting(
        self, add_storage
    ):
        self.harness.set_can_connect(container=self._container_name, val=True)
        self._push_configuration_file_to_workload()

        self.harness.evaluate_status()

        assert self.harness.model.unit.status == WaitingStatus("Waiting for UPF service to start")

    def test_given_config_file_not_created_when_config_changed_then_file_created(
        self, add_storage
    ):
        self.mock_check_output.return_value = b"1.1.1.1"
        root = self.harness.get_filesystem_root(container=self._container_name)
        self.harness.set_can_connect(container=self._container_name, val=True)
        self.harness.handle_exec(
            container=self._container_name,
            command_prefix=[],
            result=0,
        )

        self.harness.update_config()

        expected_config_file_content = read_file("tests/unit/expected_config.yaml").strip()
        existing_config = (root / "etc/eupf/config.yaml").read_text()
        assert yaml.safe_load(existing_config) == yaml.safe_load(expected_config_file_content)

    def test_given_can_connect_when_config_changed_then_pebble_layer_is_added(self, add_storage):
        self.mock_check_output.return_value = b"1.1.1.1"
        self.harness.set_can_connect(container=self._container_name, val=True)
        self.harness.handle_exec(
            container=self._container_name,
            command_prefix=[],
            result=0,
        )

        self.harness.update_config()

        expected_plan = {
            "services": {
                self._container_name: {
                    "startup": "enabled",
                    "override": "replace",
                    "command": "/bin/eupf --config /etc/eupf/config.yaml",
                }
            }
        }
        applied_plan = self.harness.get_container_pebble_plan(self._container_name).to_dict()
        assert applied_plan == expected_plan
