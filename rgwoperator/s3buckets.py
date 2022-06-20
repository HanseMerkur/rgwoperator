import asyncio
import json
import logging
from os import getenv

import boto3
import kopf
from botocore.exceptions import ClientError
from pykube import HTTPClient, KubeConfig
from pykube.exceptions import PyKubeError
from aiorgwadmin import RGWAdmin
from aiorgwadmin.exceptions import BucketNotEmpty, NoSuchBucket, NoSuchKey

from s3struct import AccessKey, Secret, User
from utils import is_annotation_set, get_environment_creds

PUBLIC_POLICY = {
    "Statement": [
        {
            "Action": ["s3:GetObject"],
            "Effect": "Allow",
            "Principal": "*",
            "Resource": [],
            "Sid": "PublicRead",
        }
    ],
    "Version": "2012-10-17",
}

tenant = getenv("TENANT", "dev")


@kopf.on.create("s3.hanse-merkur.de", "v1alpha1", "buckets")
async def add_bucket(spec, meta, patch, annotations, logger, **_):
    bucket_name = spec["bucketName"]
    owner_access_key = spec["ownerAccessKey"]
    bucket_policy = spec["bucketPolicy"]
    life_cycle_policy = spec.get("lifeCyclePolicy", None)
    object_lock = spec.get("objectLock", False)
    object_lock_config = spec.get("objectLockConfig", None)
    namespace = meta["namespace"]
    versioning = spec["objectVersioning"]
    quotas = spec.get("quotas", None)
    if quotas and quotas["enabled"]:
        max_objects = quotas["maxObjects"]
        max_size = quotas["maxSize"]
    else:
        max_objects = -1
        max_size = -1

    if bucket_policy == "public":
        # Standard public ACL from the AWS documentation.
        # Allows read-only access to all objects
        bucket_policy = PUBLIC_POLICY.copy()
        bucket_policy["Statement"][0]["Resource"].append(
            f"arn:aws:s3:::{bucket_name}/*"
        )
    elif bucket_policy == "custom" and "customBucketPolicy" in spec:
        try:
            bucket_policy = json.loads(spec["customBucketPolicy"])
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse customBucketPolicy")

    if life_cycle_policy is not None:
        try:
            life_cycle_policy = json.loads(life_cycle_policy)
            # BUG https://github.com/ceph/ceph/pull/26518
            for rule in life_cycle_policy["Rules"]:
                if not hasattr(rule, "Prefix"):
                    rule["Prefix"] = ""
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse lifeCyclePolicy")

    if object_lock and object_lock_config is not None:
        try:
            object_lock_config = json.loads(object_lock_config)
            # Ensure the key is present in the object
            if "ObjectLockEnabled" not in object_lock_config:
                object_lock_config["ObjectLockEnabled"] = "Enabled"
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse objectLockConfig")

    allow_import = is_annotation_set(annotations, "allow-import")

    try:
        api = HTTPClient(KubeConfig.from_env())
        access_key = AccessKey.objects(api, namespace=namespace).get(
            namespace=namespace, name=owner_access_key
        )
        if not access_key.exists():
            raise kopf.PermanentError("No such AccessKey exists")
        access_key_secret = Secret.objects(api, namespace=namespace).get(
            namespace=namespace, name=access_key.obj["spec"]["secretName"]
        )
        if not access_key_secret.exists():
            raise kopf.PermanentError("No such Secret Access Key exists")
        access_key_id = access_key_secret.get_secret("aws_access_key_id")
        secret_access_key = access_key_secret.get_secret("aws_secret_access_key")

        # Find the actual owner in the radosgw database
        user = User.objects(api).get(name=access_key.obj["spec"]["owner"])
        if not user.exists():
            raise kopf.PermanentError("Owner is not managed by Cluster")
        user_annotations = user.obj["metadata"]["annotations"]
        if is_annotation_set(user_annotations, "skip-tenant"):
            owner = user.obj["spec"]["userId"]
        else:
            owner = f"{tenant}-{user.obj['spec']['userId']}"
    except PyKubeError as e:
        raise kopf.TemporaryError(f"Failed to get kubernetes secrets due to error: {e}")
    finally:
        api.session.close()

    try:
        endpoint = getenv("OBJ_SERVER", "s3.hanse-merkur.de")
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{endpoint}",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        rgw = RGWAdmin(**get_environment_creds())

        all_buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
        if bucket_name not in all_buckets:
            s3.create_bucket(Bucket=bucket_name, ObjectLockEnabledForBucket=object_lock)
        elif bucket_name in all_buckets and not allow_import:
            raise kopf.PermanentError(
                "Bucket exists and current import settings don't allow import"
            )
        elif bucket_name in all_buckets and allow_import:
            bucket_meta = await rgw.get_metadata(metadata_type="bucket", key=bucket_name)
            bucket_id = bucket_meta["data"]["bucket"]["bucket_id"]

            await rgw.link_bucket(bucket=bucket_name, bucket_id=bucket_id, uid=owner)

        if bucket_policy in ["public", "custom"]:
            s3.put_bucket_policy(Bucket=bucket_name, Policy=bucket_policy)

        if life_cycle_policy is not None:
            response = s3.put_bucket_lifecycle_configuration(
                Bucket=bucket_name, LifecycleConfiguration=life_cycle_policy
            )
            if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                kopf.PermanentError("Invalid LifeCyclePolicy: %s", response)

        if object_lock_config is not None:
            response = s3.put_object_lock_configuration(
                Bucket=bucket_name, ObjectLockConfiguration=object_lock_config
            )

        if versioning:
            s3.put_bucket_versioning(
                Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"}
            )

        rgw_bucket = await rgw.get_bucket(bucket=bucket_name)

        if rgw_bucket["owner"] != access_key.obj["spec"]["owner"]:
            raise kopf.PermanentError("Bucket is not owned by the same user")

        if quotas and quotas["enabled"]:
            await rgw.set_bucket_quota(
                uid=rgw_bucket["owner"],
                bucket=bucket_name,
                max_size_kb=max_size,
                max_objects=max_objects,
                enabled=True,
            )
    except ClientError:
        raise kopf.PermanentError("Failed to create bucket")

    patch.status["ready"] = True
    patch.status["owner"] = access_key.obj["spec"]["owner"]

@kopf.on.update("s3.hanse-merkur.de", "v1alpha1", "buckets")
async def update_bucket(spec, old, new, meta, patch, logger, **_):
    logger.debug("Updating bucket information")

    if old["spec"]["bucketName"] != new["spec"]["bucketName"]:
        raise kopf.PermanentError("Cannot change bucket name")
    if old["spec"]["ownerAccessKey"] != new["spec"]["ownerAccessKey"]:
        raise kopf.PermanentError("Cannot change owner")
    if old["spec"]["objectLock"] != new["spec"]["objectLock"]:
        raise kopf.PermanentError("Cannot change object locking post-creation")

    bucket_name = spec["bucketName"]
    bucket_policy = spec["bucketPolicy"]
    owner_access_key = spec["ownerAccessKey"]
    life_cycle_policy = spec.get("lifeCyclePolicy", None)
    object_lock = spec.get("objectLock", False)
    object_lock_config_spec = spec.get("objectLockConfig", None)
    quotas = spec.get("quotas", None)
    namespace = meta["namespace"]
    if quotas and quotas["enabled"]:
        max_objects = quotas["maxObjects"]
        max_size = quotas["maxSize"]
    else:
        max_objects = -1
        max_size = -1

    if bucket_policy == "public":
        # Standard public ACL from the AWS documentation.
        # Allows read-only access to all objects
        bucket_policy = PUBLIC_POLICY.copy()
        bucket_policy["Statement"][0]["Resource"].append(
            f"arn:aws:s3:::{bucket_name}/*"
        )
    elif bucket_policy == "custom" and "customBucketPolicy" in spec:
        try:
            bucket_policy = json.loads(spec["customBucketPolicy"])
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse customBucketPolicy")

    if life_cycle_policy is not None:
        try:
            life_cycle_policy = json.loads(life_cycle_policy)
            # BUG https://github.com/ceph/ceph/pull/26518
            for rule in life_cycle_policy["Rules"]:
                if not hasattr(rule, "Prefix"):
                    rule["Prefix"] = ""
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse lifeCyclePolicy")

    if object_lock and object_lock_config_spec is not None and old["objectLockConfig"] != new["objectLockConfig"]:
        try:
            object_lock_config = json.loads(object_lock_config_spec)
        except json.JSONDecodeError:
            raise kopf.PermanentError("Failed to parse objectLockConfig")

    api = HTTPClient(KubeConfig.from_env())
    access_key = AccessKey.objects(api, namespace=namespace).get(
        namespace=namespace, name=owner_access_key
    )
    if not access_key.exists():
        raise kopf.PermanentError("No such AccessKey exists")
    access_key_secret = Secret.objects(api, namespace=namespace).get(
        namespace=namespace, name=access_key.obj["spec"]["secretName"]
    )
    if not access_key_secret.exists():
        raise kopf.PermanentError("No such Secret Access Key exists")
    access_key_id = access_key_secret.get_secret("aws_access_key_id")
    secret_access_key = access_key_secret.get_secret("aws_secret_access_key")

    endpoint = getenv("OBJ_SERVER", "s3.hanse-merkur.de")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{endpoint}",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )

    if bucket_policy in ["public", "custom"]:
        s3.put_bucket_policy(Bucket=bucket_name, Policy=bucket_policy)

    if life_cycle_policy is not None:
        response = s3.put_bucket_lifecycle_configuration(
            Bucket=bucket_name, LifecycleConfiguration=life_cycle_policy
        )
        if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
            kopf.PermanentError("Invalid LifeCyclePolicy: %s", response)

    if object_lock_config is not None:
        response = s3.put_object_lock_configuration(
            Bucket=bucket_name, ObjectLockConfiguration=object_lock_config
        )

    if old["objectVersioning"] != new["objectVersioning"]:
        s3.put_bucket_versioning(
            Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled" if new["objectVersioning"] else "Disabled"}
        )

    rgw = RGWAdmin(**get_environment_creds())
    rgw_bucket = await rgw.get_bucket(bucket=bucket_name)

    if quotas and quotas["enabled"]:
        await rgw.set_bucket_quota(
            uid=rgw_bucket["owner"],
            bucket=bucket_name,
            max_size_kb=max_size,
            max_objects=max_objects,
            enabled=True,
            )
    patch.status["ready"] = True


@kopf.on.delete("s3.hanse-merkur.de", "v1alpha1", "buckets")
async def delete_bucket(spec, meta, annotations, logger, **_):
    bucket_name = spec["bucketName"]
    owner_access_key = spec["ownerAccessKey"]
    namespace = meta["namespace"]

    try:
        api = HTTPClient(KubeConfig.from_env())
        access_key = AccessKey.objects(api, namespace=namespace).get(
            namespace=namespace, name=owner_access_key
        )
        if not access_key.exists():
            raise kopf.PermanentError("No such AccessKey exists")

        # Find the actual owner in the radosgw database
        user = User.objects(api).get(name=access_key.obj["spec"]["owner"])
        if not user.exists():
            raise kopf.PermanentError("Owner is not managed by Cluster")
        user_annotations = user.obj["metadata"]["annotations"]
        if is_annotation_set(user_annotations, "skip-tenant"):
            owner = user.obj["spec"]["userId"]
        else:
            owner = f"{tenant}-{user.obj['spec']['userId']}"

    except PyKubeError as e:
        raise kopf.TemporaryError(f"Failed to get kubernetes secrets due to error: {e}")
    finally:
        api.session.close()

    try:
        rgw = RGWAdmin(**get_environment_creds())
        if not is_annotation_set(annotations, "allow-deletion"):
            logger.info("Unlinking bucket %s from owner %s", bucket_name, owner)
            await rgw.unlink_bucket(bucket=bucket_name, uid=owner)
        else:
            logger.info("Permanently deleting bucket %s", bucket_name)
            force_deletion = is_annotation_set(annotations, "force-deletion")
            await rgw.remove_bucket(bucket=bucket_name, purge_objects=force_deletion)
    except BucketNotEmpty:
        raise kopf.PermanentError(
            "Cannot delete a non-empty bucket without force-deletion"
        )
    except NoSuchBucket:
        pass
    except NoSuchKey:
        pass


def is_bucket_ready(body, **_) -> bool:
    return body.status.get("ready", False)


@kopf.timer(
    "s3.hanse-merkur.de",
    "v1alpha1",
    "buckets",
    interval=60,
    idle=120,
    when=is_bucket_ready,
)
async def update_bucket_stats(spec, patch, **_):
    try:
        rgw = RGWAdmin(**get_environment_creds())
        bucket = await rgw.get_bucket(bucket=spec["bucketName"], stats=True)
        if not bucket:
            logging.error(
                "Bucket %s was deleted outside operator scope", spec["bucketName"]
            )
            return
        if "stats" in bucket:
            patch.status["size"] = bucket["rgw.main"]["size_kb"]
            patch.status["objects"] = bucket["rgw.main"]["num_objects"]

        if "bucket_quota" in bucket:
            patch.status["maxSize"] = bucket["bucket_quota"]["max_size_kb"]
            patch.status["maxObjects"] = bucket["bucket_quota"]["max_objects"]
    except asyncio.CancelledError:
        pass
