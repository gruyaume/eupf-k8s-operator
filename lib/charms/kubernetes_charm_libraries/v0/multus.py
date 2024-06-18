"""This is not the original lib"""


import logging
from typing import Union

from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.apps_v1 import StatefulSetSpec
from lightkube.models.core_v1 import (
    Capabilities,
    Container,
    PodSpec,
    PodTemplateSpec,
    SecurityContext,
)
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.resources.core_v1 import Pod
from lightkube.types import PatchType
from ops.charm import CharmBase
from ops.framework import BoundEvent, Object

# The unique Charmhub library identifier, never change it
LIBID = "75283550e3474e7b8b5b7724d345e3c2"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 13


logger = logging.getLogger(__name__)

class KubernetesMultusError(Exception):
    """KubernetesMultusError."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class KubernetesClient:
    """Class containing all the Kubernetes specific calls."""

    def __init__(self, namespace: str):
        self.client = Client()
        self.namespace = namespace

    def pod_is_ready(
        self,
        pod_name: str,
        *,
        container_name: str,
        cap_net_admin: bool,
        privileged: bool,
    ) -> bool:
        try:
            pod = self.client.get(Pod, name=pod_name, namespace=self.namespace)
        except ApiError as e:
            if e.status.reason == "Unauthorized":
                logger.debug("kube-apiserver not ready yet")
            else:
                raise KubernetesMultusError(f"Pod {pod_name} not found")
            return False
        return self._pod_is_patched(
            pod=pod,  # type: ignore[arg-type]
            container_name=container_name,
            cap_net_admin=cap_net_admin,
            privileged=privileged,
        )

    def patch_statefulset(
        self,
        name: str,
        container_name: str,
        cap_net_admin: bool,
        privileged: bool,
    ) -> None:
        
        try:
            statefulset = self.client.get(res=StatefulSet, name=name, namespace=self.namespace)
        except ApiError:
            raise KubernetesMultusError(f"Could not get statefulset {name}")
        container = Container(name=container_name)
        if cap_net_admin:
            container.securityContext = SecurityContext(
                capabilities=Capabilities(
                    add=[
                        "NET_ADMIN",
                    ]
                )
            )
        if privileged:
            container.securityContext.privileged = True  # type: ignore[union-attr]
        statefulset_delta = StatefulSet(
            spec=StatefulSetSpec(
                selector=statefulset.spec.selector,  # type: ignore[attr-defined]
                serviceName=statefulset.spec.serviceName,  # type: ignore[attr-defined]
                template=PodTemplateSpec(
                    spec=PodSpec(containers=[container]),
                ),
            )
        )
        try:
            self.client.patch(
                res=StatefulSet,
                name=name,
                obj=statefulset_delta,
                patch_type=PatchType.APPLY,
                namespace=self.namespace,
                field_manager=self.__class__.__name__,
            )
        except ApiError:
            raise KubernetesMultusError(f"Could not patch statefulset {name}")
        logger.info("Security context added to %s statefulset", name)


    def statefulset_is_patched(
        self,
        name: str,
        container_name: str,
        cap_net_admin: bool,
        privileged: bool,
    ) -> bool:
        try:
            statefulset = self.client.get(res=StatefulSet, name=name, namespace=self.namespace)
        except ApiError as e:
            if e.status.reason == "Unauthorized":
                logger.debug("kube-apiserver not ready yet")
            else:
                raise KubernetesMultusError(f"Could not get statefulset {name}")
            return False
        return self._pod_is_patched(
            container_name=container_name,
            cap_net_admin=cap_net_admin,
            privileged=privileged,
            pod=statefulset.spec.template,  # type: ignore[attr-defined]
        )

    def _pod_is_patched(
        self,
        container_name: str,
        cap_net_admin: bool,
        privileged: bool,
        pod: Union[PodTemplateSpec, Pod],
    ) -> bool:
        if not self._container_security_context_is_set(
            containers=pod.spec.containers,
            container_name=container_name,
            cap_net_admin=cap_net_admin,
            privileged=privileged,
        ):
            return False
        return True

    @staticmethod
    def _container_security_context_is_set(
        containers: list[Container],
        container_name: str,
        cap_net_admin: bool,
        privileged: bool,
    ) -> bool:
        for container in containers:
            if container.name == container_name:
                if not container.securityContext:
                    return False
                if not container.securityContext.capabilities:
                    return False
                if not container.securityContext.capabilities.add:
                    return False
                if cap_net_admin and "NET_ADMIN" not in container.securityContext.capabilities.add:
                    return False
                if privileged and not container.securityContext.privileged:  # type: ignore[union-attr]  # noqa E501
                    return False
        return True


class KubernetesMultusCharmLib(Object):
    """Class to be instantiated by charms requiring Multus networking."""

    def __init__(
        self,
        charm: CharmBase,
        container_name: str,
        refresh_event: BoundEvent,
        cap_net_admin: bool = False,
        privileged: bool = False,
    ):
        super().__init__(charm, "kubernetes-multus")
        self.kubernetes = KubernetesClient(namespace=self.model.name)
        self.container_name = container_name
        self.cap_net_admin = cap_net_admin
        self.privileged = privileged
        self.framework.observe(refresh_event, self._configure_multus)

    def _configure_multus(self, event: BoundEvent) -> None:
        if not self._statefulset_is_patched():
            self.kubernetes.patch_statefulset(
                name=self.model.app.name,
                container_name=self.container_name,
                cap_net_admin=self.cap_net_admin,
                privileged=self.privileged,
            )

    def _statefulset_is_patched(self) -> bool:
        return self.kubernetes.statefulset_is_patched(
            name=self.model.app.name,
            container_name=self.container_name,
            cap_net_admin=self.cap_net_admin,
            privileged=self.privileged,
        )

    @property
    def _pod(self) -> str:
        """Name of the unit's pod.

        Returns:
            str: A string containing the name of the current unit's pod.
        """
        return "-".join(self.model.unit.name.rsplit("/", 1))
