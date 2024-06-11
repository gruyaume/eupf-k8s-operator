# Copyright 2024 Guillaume Belanger
# See LICENSE file for licensing details.

from pathlib import Path

import pytest
import yaml
from charm import EupfK8SOperatorCharm
from ops import ActiveStatus, WaitingStatus, testing

NAMESPACE = "whatever"

def read_file(path: str) -> str:
    """Read a file and returns as a string."""
    with open(path, "r") as f:
        content = f.read()
    return content


class TestCharm:

    @pytest.fixture(autouse=True)
    def harness_fixture(self,  request):
        self._container_name = "eupf"
        self.harness = testing.Harness(EupfK8SOperatorCharm)
        self.harness.set_model_name(name=NAMESPACE)
        self.harness.set_leader(is_leader=True)
        self.harness.begin()
        yield self.harness
        self.harness.cleanup()

    @pytest.fixture()
    def add_storage(self):
        self.harness.add_storage(storage_name="config", attach=True)

    def _push_configuration_file_to_workload(self):
        root = self.harness.get_filesystem_root(container=self._container_name)
        expected_config_file_path = Path(__file__).parent  / "expected_config.yaml"
        with open(expected_config_file_path, "r") as expected_config_file:
            (root / "etc/eupf/config.yaml").write_text(expected_config_file.read())

    def test_given_fiveg_config_file_not_created_when_evaluate_status_then_status_is_waiting(
        self
    ):
        self.harness.set_can_connect(container=self._container_name, val=False)

        self.harness.evaluate_status()

        assert self.harness.model.unit.status == WaitingStatus("Waiting for UPF configuration file")

    def test_given_config_file_created_when_evaluate_status_then_status_is_active(self, add_storage):
        self.harness.set_can_connect(container=self._container_name, val=True)
        self._push_configuration_file_to_workload()

        self.harness.evaluate_status()

        assert self.harness.model.unit.status == ActiveStatus("")


    def test_given_config_file_not_created_when_config_changed_then_file_created(self, add_storage):
        root = self.harness.get_filesystem_root(container=self._container_name)
        self.harness.set_can_connect(container=self._container_name, val=True)

        self.harness.update_config()

        expected_config_file_content = read_file("tests/unit/expected_config.yaml").strip()
        existing_config = (root / "etc/eupf/config.yaml").read_text()
        assert yaml.safe_load(existing_config) == yaml.safe_load(expected_config_file_content)
