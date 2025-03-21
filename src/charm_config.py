# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Config of the Charm."""

import dataclasses
import logging
from enum import Enum
from ipaddress import IPv4Address, IPv4Network
from typing import Optional

import ops
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
)

logger = logging.getLogger(__name__)


class CNIType(str, Enum):
    """Class to define available CNI types for eUPF operator."""

    bridge = "bridge"
    macvlan = "macvlan"
    host_device = "host-device"


class XDPAttachMode(str, Enum):
    """Class to define available XDP attach modes for UPF operator."""

    native = "native"
    generic = "generic"

    def __str__(self) -> str:
        """Return the string representation of the XDPAttachMode."""
        return self.value


class CharmConfigInvalidError(Exception):
    """Exception raised when a charm configuration is found to be invalid."""

    def __init__(self, msg: str):
        """Initialize a new instance of the CharmConfigInvalidError exception.

        Args:
            msg (str): Explanation of the error.
        """
        self.msg = msg


def to_kebab(name: str) -> str:
    """Convert a snake_case string to kebab-case."""
    return name.replace("_", "-")


class UpfConfig(BaseModel):
    """Represent UPF operator builtin configuration values."""

    model_config = ConfigDict(alias_generator=to_kebab, use_enum_values=True)

    cni_type: CNIType = CNIType.bridge
    xdp_attach_mode: XDPAttachMode = XDPAttachMode.generic
    logging_level: str = "info"
    gnb_subnet: IPv4Network = IPv4Network("192.168.251.0/24")
    n3_host_interface: Optional[StrictStr] = Field(default="")
    n6_host_interface: Optional[StrictStr] = Field(default="")
    n3_ip: str = Field(default="192.168.252.3/24")
    n3_gateway_ip: IPv4Address = IPv4Address("192.168.252.1")
    n6_ip: str = Field(default="192.168.250.3/24")
    n6_gateway_ip: IPv4Address = IPv4Address("192.168.250.1")
    pfcp_node_id: str = Field(default="")
    external_hostname: Optional[StrictStr] = Field(default="")


@dataclasses.dataclass
class CharmConfig:
    """Represent the configuration of the charm."""

    cni_type: CNIType
    xdp_attach_mode: XDPAttachMode
    logging_level: str
    gnb_subnet: IPv4Network
    n3_host_interface: Optional[StrictStr]
    n6_host_interface: Optional[StrictStr]
    n3_ip: str
    n3_gateway_ip: IPv4Address
    n6_ip: str
    n6_gateway_ip: IPv4Address
    pfcp_node_id: str
    external_hostname: Optional[StrictStr]

    def __init__(self, *, upf_config: UpfConfig):
        """Initialize a new instance of the CharmConfig class.

        Args:
            upf_config: UPF operator configuration.
        """
        self.cni_type = upf_config.cni_type
        self.xdp_attach_mode = upf_config.xdp_attach_mode
        self.logging_level = upf_config.logging_level
        self.gnb_subnet = upf_config.gnb_subnet
        self.n3_host_interface = upf_config.n3_host_interface
        self.n6_host_interface = upf_config.n6_host_interface
        self.n3_ip = upf_config.n3_ip
        self.n3_gateway_ip = upf_config.n3_gateway_ip
        self.n6_ip = upf_config.n6_ip
        self.n6_gateway_ip = upf_config.n6_gateway_ip
        self.pfcp_node_id = upf_config.pfcp_node_id
        self.external_hostname = upf_config.external_hostname

    @classmethod
    def from_charm(
        cls,
        charm: ops.CharmBase,
    ) -> "CharmConfig":
        """Initialize a new instance of the CharmState class from the associated charm."""
        try:
            # ignoring because mypy fails with:
            # "has incompatible type "**dict[str, str]"; expected ...""
            return cls(upf_config=UpfConfig(**dict(charm.config.items())))  # type: ignore
        except ValidationError as exc:
            error_fields: list = []
            for error in exc.errors():
                if param := error["loc"]:
                    error_fields.extend(param)
                else:
                    value_error_msg: ValueError = error["ctx"]["error"]  # type: ignore
                    error_fields.extend(str(value_error_msg).split())
            error_fields.sort()
            error_field_str = ", ".join(f"'{f}'" for f in error_fields)
            raise CharmConfigInvalidError(
                f"The following configurations are not valid: [{error_field_str}]"
            ) from exc
