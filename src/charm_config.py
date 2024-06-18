# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Config of the Charm."""

import dataclasses
import logging
from ipaddress import IPv4Address, IPv4Network

import ops
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
)

logger = logging.getLogger(__name__)


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

    interfaces: str = "[access]"
    logging_level: str = "info"
    gnb_subnet: IPv4Network = IPv4Network("192.168.251.0/24")
    access_ip: IPv4Address = IPv4Address("192.168.252.3")
    access_gateway_ip: IPv4Address = IPv4Address("192.168.252.1")
    core_ip: IPv4Address = IPv4Address("192.168.250.3")
    core_gateway_ip: IPv4Address = IPv4Address("192.168.250.1")
    pfcp_node_id: IPv4Address = IPv4Address("127.0.0.1")


@dataclasses.dataclass
class CharmConfig:
    """Represent the configuration of the charm."""

    interfaces: str
    logging_level: str
    gnb_subnet: IPv4Network
    access_ip: IPv4Address
    access_gateway_ip: IPv4Address
    core_ip: IPv4Address
    core_gateway_ip: IPv4Address
    pfcp_node_id: IPv4Address

    def __init__(self, *, upf_config: UpfConfig):
        """Initialize a new instance of the CharmConfig class.

        Args:
            upf_config: UPF operator configuration.
        """
        self.interfaces = upf_config.interfaces
        self.logging_level = upf_config.logging_level
        self.gnb_subnet = upf_config.gnb_subnet
        self.access_ip = upf_config.access_ip
        self.access_gateway_ip = upf_config.access_gateway_ip
        self.core_ip = upf_config.core_ip
        self.core_gateway_ip = upf_config.core_gateway_ip
        self.pfcp_node_id = upf_config.pfcp_node_id

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
