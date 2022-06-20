from os import getenv
import logging

import kopf
from pykube import HTTPClient, KubeConfig
from pykube.exceptions import PyKubeError, ObjectDoesNotExist
from aiorgwadmin import RGWAdmin
from aiorgwadmin.exceptions import NoSuchUser
from jinja2 import Environment, BaseLoader


from s3struct import Secret, User
from utils import is_annotation_set, get_environment_creds

tenant = getenv("TENANT", "dev")


@kopf.on.create("s3.hanse-merkur.de", "v1alpha1", "accesskeys")
async def add_access_key(meta, spec, logger, patch, **_):
    namespace = meta["namespace"]
    name = spec["secretName"]
    user_id = spec["owner"]

    template = spec.get("template", None)
    secret_annotations, secret_labels, template_data = (None, None, None)
    if template:
        secret_annotations = template.get("metadata", {}).get("annotations", None)
        secret_labels = template.get("metadata", {}).get("labels", None)
        template_data = template.get("data", None)

    try:
        api = HTTPClient(KubeConfig.from_env())
        # Find the actual owner in the radosgw database
        user = User.objects(api).get(name=user_id)
        if not user.exists():
            raise kopf.TemporaryError("Owner %s is not managed by Cluster", user_id)
        user_annotations = user.obj["metadata"]["annotations"]

        if is_annotation_set(user_annotations, "skip-tenant"):
            rgw_user_id = user.obj["spec"]["userId"]
        else:
            rgw_user_id = f"{tenant}-{user.obj['spec']['userId']}"

        logger.debug(
            "Creating AccessKey %s in %s with Secret %s for %s",
            meta["name"],
            namespace,
            name,
            rgw_user_id,
        )

        rgw = RGWAdmin(**get_environment_creds())
        user = {}
        try:
            user = await rgw.get_user(uid=rgw_user_id)
        except NoSuchUser:
            raise kopf.TemporaryError("The owner %s does not exist", rgw_user_id)

        try:
            User.objects(api).get(name=user_id)
        except ObjectDoesNotExist:
            raise kopf.TemporaryError("The referenced Ceph user %s is not managed on this cluster", user_id)

        try:
            secret = Secret.objects(api, namespace=namespace).get_by_name(name=name)
            logger.debug(
                "Creating new access key in Ceph using existing secret for %s",
                rgw_user_id,
            )
            access_key_id = secret.get_secret("aws_access_key_id")
            secret_access_key = secret.get_secret("aws_secret_access_key")
            if not (access_key_id or secret_access_key):
                raise kopf.TemporaryError(
                    f"Secret {name} does not contain the keys 'aws_access_key_id' and 'aws_secret_access_key'"
                )
            response = await rgw.create_key(
                uid=rgw_user_id, access_key=access_key_id, secret_key=secret_access_key
            )
        except ObjectDoesNotExist:
            logger.debug("Creating new access key in Ceph for %s", rgw_user_id)
            # RadosGW API does not return the actual key it just created
            # So we check against existing keys for the new one instead
            # Not a great solution as others might be creating keys aside from the operator
            try:
                all_keys = await rgw.create_key(uid=rgw_user_id)
                new_keys = [x for x in all_keys if x not in user["keys"]]
                if len(new_keys) < 1:
                    raise kopf.PermanentError("Failed to create a new access key for the user %s", user_id)
                # We select the first one from the list and hope for the best
                access_key_id = new_keys[0]["access_key"]
                secret_access_key = new_keys[0]["secret_key"]

                secret = Secret.new(api, name, namespace)
                secret.set_secret("aws_access_key_id", access_key_id)
                secret.set_secret("aws_secret_access_key", secret_access_key)
                kopf.adopt(secret.obj)
                logger.debug("Creating Kubernetes secret with name %s", name)

                if secret_annotations:
                    secret["metadata"]["annotations"] = secret_annotations
                if secret_labels:
                    secret["metadata"]["labels"] = secret_labels

                if template_data:
                    for k, v in template_data.items():
                        if k in ["aws_access_key_id", "aws_secret_access_key"]:
                            continue
                        rtemplate = Environment(loader=BaseLoader).from_string(v)
                        data = rtemplate.render(
                            aws_access_key_id=access_key_id,
                            aws_secret_access_key=secret_access_key,
                        )
                        secret.set_secret(k, data)

                secret.create()
            except NoSuchUser:
                kopf.PermanentError(
                    "No such user exists. No access key can be generated"
                )

        try:
            # Reverse ownership referencing is not yet supported in kopf.
            # This is based on append_owner_reference from hierachies.py
            k8s_user = User.objects(api).get(name=user_id)
            refs = patch.setdefault("metadata", {}).setdefault("ownerReferences", [])
            refs.append(kopf.build_owner_reference(k8s_user.obj))
            logger.debug("Patching AccessKey with owner reference: %s", patch)
        except ObjectDoesNotExist:
            kopf.PermanentError("Ceph User %s is not managed in Kubernetes", user_id)
    except PyKubeError as e:
        kopf.PermanentError("kube error: %s", e)
    finally:
        api.session.close()

    patch.status["accessKeyId"] = access_key_id
    patch.status["ready"] = True


@kopf.on.delete("s3.hanse-merkur.de", "v1alpha1", "accesskeys")
async def delete_access_key(spec, status, **_):
    if not status["ready"]:
        return

    access_key_id = status["accessKeyId"]
    user_id = spec["owner"]
    rgw_user_id = f"{tenant}-{user_id}"

    rgw = RGWAdmin(**get_environment_creds())
    try:
        await rgw.remove_key(access_key_id, uid=rgw_user_id)
    except NoSuchUser:
        pass
    except Exception:
        pass
