#!/usr/bin/env python3
# Copyright 2024 Guillaume Belanger

"""Kubernetes charm for eUPF."""

import logging

import ops
from ops.charm import CollectStatusEvent
from ops.model import ActiveStatus

logger = logging.getLogger(__name__)



class EupfK8SOperatorCharm(ops.CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.collect_unit_status, self._on_collect_status)
        self.framework.observe(self.on.config_changed, self._configure)

    def _on_collect_status(self, event: CollectStatusEvent):
        """Collect the status of the unit."""
        event.add_status(ActiveStatus())

    def _configure(self, event):
        """Handle state affecting events."""
        pass


if __name__ == "__main__":  # pragma: nocover
    ops.main(EupfK8SOperatorCharm)  # type: ignore
