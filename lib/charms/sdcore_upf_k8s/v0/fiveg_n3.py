# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Library for the `fiveg_n3` relation.

This library offers a way of providing and consuming an IP address of the SDCORE's UPF.
In a typical 5G network, UPF's IP address is consumed by the gNodeBs, in order to establish
communication over the N3 interface.

To get started using the library, you need to fetch the library using `charmcraft`.

```shell
cd some-charm
charmcraft fetch-lib charms.sdcore_upf_k8s.v0.fiveg_n3
```

Add the following libraries to the charm's `requirements.txt` file:
- pydantic
- pytest-interface-tester

Charms providing the `fiveg_n3` relation should use `N3Provides`.
Typical usage of this class would look something like:

    ```python
    ...
    from charms.sdcore_upf_k8s.v0.fiveg_n3 import N3Provides
    ...

    class SomeProviderCharm(CharmBase):

        def __init__(self, *args):
            ...
            self.fiveg_n3 = N3Provides(charm=self, relation_name="fiveg_n3")
            ...
            self.framework.observe(self.fiveg_n3.on.fiveg_n3_request, self._on_fiveg_n3_request)

        def _on_fiveg_n3_request(self, event):
            ...
            self.fiveg_n3.publish_upf_information(
                relation_id=event.relation_id,
                upf_ip_address=ip_address,
            )
    ```

    And a corresponding section in charm's `metadata.yaml`:
    ```
    provides:
        fiveg_n3:  # Relation name
            interface: fiveg_n3  # Relation interface
    ```

Charms that require the `fiveg_n3` relation should use `N3Requires`.
Typical usage of this class would look something like:

    ```python
    ...
    from charms.sdcore_upf_k8s.v0.fiveg_n3 import N3Requires
    ...

    class SomeRequirerCharm(CharmBase):

        def __init__(self, *args):
            ...
            self.fiveg_n3 = N3Requires(charm=self, relation_name="fiveg_n3")
            ...
            self.framework.observe(self.upf.on.fiveg_n3_available, self._on_fiveg_n3_available)

        def _on_fiveg_n3_available(self, event):
            upf_ip_address = event.upf_ip_address
            # Do something with the UPF's IP address
    ```

    And a corresponding section in charm's `metadata.yaml`:
    ```
    requires:
        fiveg_n3:  # Relation name
            interface: fiveg_n3  # Relation interface
    ```
"""

import logging

from interface_tester.schema_base import DataBagSchema  # type: ignore[import]
from ops.charm import CharmBase, CharmEvents, RelationChangedEvent, RelationJoinedEvent
from ops.framework import EventBase, EventSource, Object
from pydantic import BaseModel, Field, IPvAnyAddress, ValidationError

# The unique Charmhub library identifier, never change it
LIBID = "93fa81e7726c4d14ba2b4834866bf30e"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2

PYDEPS = ["pydantic", "pytest-interface-tester"]


logger = logging.getLogger(__name__)

"""Schemas definition for the provider and requirer sides of the `fiveg_n3` interface.
It exposes two interfaces.schema_base.DataBagSchema subclasses called:
- ProviderSchema
- RequirerSchema
Examples:
    ProviderSchema:
        unit: <empty>
        app: {
            "upf_ip_address": "1.2.3.4"
        }
    RequirerSchema:
        unit: <empty>
        app:  <empty>
"""


class ProviderAppData(BaseModel):
    """Provider app data for fiveg_n3."""

    upf_ip_address: IPvAnyAddress = Field(description="UPF IP address", examples=["1.2.3.4"])


class ProviderSchema(DataBagSchema):
    """Provider schema for fiveg_n3."""

    app: ProviderAppData


def data_matches_provider_schema(data: dict) -> bool:
    """Return whether data matches provider schema.

    Args:
        data (dict): Data to be validated.

    Returns:
        bool: True if data matches provider schema, False otherwise.
    """
    try:
        ProviderSchema(app=data)
        return True
    except ValidationError as e:
        logger.error("Invalid data: %s", e)
        return False


class FiveGN3RequestEvent(EventBase):
    """Dataclass for the `fiveg_n3` request event."""

    def __init__(self, handle, relation_id: int):
        """Set relation id."""
        super().__init__(handle)
        self.relation_id = relation_id

    def snapshot(self) -> dict:
        """Return event data."""
        return {
            "relation_id": self.relation_id,
        }

    def restore(self, snapshot):
        """Restore event data."""
        self.relation_id = snapshot["relation_id"]


class N3ProviderCharmEvents(CharmEvents):
    """Custom events for the N3Provider."""

    fiveg_n3_request = EventSource(FiveGN3RequestEvent)


class N3Provides(Object):
    """Class to be instantiated by provider of the `fiveg_n3`."""

    on = N3ProviderCharmEvents()

    def __init__(self, charm: CharmBase, relation_name: str):
        """Observe relation joined event.

        Args:
            charm: Juju charm
            relation_name (str): Relation name
        """
        self.relation_name = relation_name
        self.charm = charm
        super().__init__(charm, relation_name)
        self.framework.observe(charm.on[relation_name].relation_joined, self._on_relation_joined)

    def publish_upf_information(self, relation_id: int, upf_ip_address: str) -> None:
        """Set UPF's IP address in the relation data.

        Args:
            relation_id (str): Relation ID
            upf_ip_address (str): UPF's IP address
        """
        if not data_matches_provider_schema(data={"upf_ip_address": upf_ip_address}):
            raise ValueError(f"Invalid UPF IP address: {upf_ip_address}")
        relation = self.model.get_relation(
            relation_name=self.relation_name, relation_id=relation_id
        )
        if not relation:
            raise RuntimeError(f"Relation {self.relation_name} not created yet.")
        relation.data[self.charm.app]["upf_ip_address"] = upf_ip_address

    def _on_relation_joined(self, event: RelationJoinedEvent) -> None:
        """Triggered whenever a requirer charm joins the relation.

        Args:
            event (RelationJoinedEvent): Juju event
        """
        self.on.fiveg_n3_request.emit(relation_id=event.relation.id)


class N3AvailableEvent(EventBase):
    """Dataclass for the `fiveg_n3` available event."""

    def __init__(self, handle, upf_ip_address: str):
        """Set certificate."""
        super().__init__(handle)
        self.upf_ip_address = upf_ip_address

    def snapshot(self) -> dict:
        """Return event data."""
        return {"upf_ip_address": self.upf_ip_address}

    def restore(self, snapshot):
        """Restore event data."""
        self.upf_ip_address = snapshot["upf_ip_address"]


class N3RequirerCharmEvents(CharmEvents):
    """Custom events for the N3Requirer."""

    fiveg_n3_available = EventSource(N3AvailableEvent)


class N3Requires(Object):
    """Class to be instantiated by requirer of the `fiveg_n3`."""

    on = N3RequirerCharmEvents()

    def __init__(self, charm: CharmBase, relation_name: str):
        """Observe relation joined and relation changed events.

        Args:
            charm: Juju charm
            relation_name (str): Relation name
        """
        self.relation_name = relation_name
        self.charm = charm
        super().__init__(charm, relation_name)
        self.framework.observe(charm.on[relation_name].relation_joined, self._on_relation_changed)
        self.framework.observe(charm.on[relation_name].relation_changed, self._on_relation_changed)

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        """Triggered every time there's a change in relation data.

        Args:
            event (RelationChangedEvent): Juju event
        """
        relation_data = event.relation.data
        upf_ip_address = relation_data[event.app].get("upf_ip_address")  # type: ignore[index]
        if upf_ip_address:
            self.on.fiveg_n3_available.emit(upf_ip_address=upf_ip_address)
