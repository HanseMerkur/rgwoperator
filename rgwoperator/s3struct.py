from base64 import b64decode, b64encode
from typing import Optional

from pykube.http import HTTPClient
from pykube.objects import APIObject, NamespacedAPIObject
from pykube.objects import Secret as BaseSecret


class User(APIObject):
    version = "s3.hanse-merkur.de/v1alpha1"
    endpoint = "users"
    kind = "User"


class AccessKey(NamespacedAPIObject):
    version = "s3.hanse-merkur.de/v1alpha1"
    endpoint = "accesskeys"
    kind = "AccessKey"


class Bucket(NamespacedAPIObject):
    version = "s3.hanse-merkur.de/v1alpha1"
    endpoint = "buckets"
    kind = "Bucket"


class Secret(BaseSecret):
    def get_secret(self, key: str) -> Optional[str]:
        """
        Get and decode a secret if it exists
        Returns None otherwise
        """
        if key in self.obj["data"]:
            return b64decode(self.obj["data"][key].encode()).decode()
        return None

    def set_secret(self, key: str, data: str) -> None:
        """
        Encodes and sets a secret key.
        Overrides if already set
        """
        self.obj["data"][key] = b64encode(data.encode()).decode()

    @staticmethod
    def new(api: HTTPClient, name: str, namespace: str):
        obj = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "data": {},
        }
        return Secret(api, obj)
