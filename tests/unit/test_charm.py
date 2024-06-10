# Copyright 2024 Guillaume
# See LICENSE file for licensing details.

import unittest

import ops
import ops.testing
from charm import EupfK8SOperatorCharm
from ops.model import ActiveStatus


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(EupfK8SOperatorCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_given_when_evaluate_status_then_status_is_active(self):
        self.harness.evaluate_status()

        self.assertEqual(
            self.harness.charm.unit.status,
            ActiveStatus(),
        )
