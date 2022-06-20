from os import getenv
import kopf
import asyncio
import logging

from aiorgwadmin import RGWAdmin
from aiorgwadmin.exceptions import NoSuchUser

from utils import is_annotation_set, get_environment_creds

tenant = getenv("TENANT", "dev")


@kopf.on.create("s3.hanse-merkur.de", "v1alpha1", "user")
async def create_user_on_demand(spec, patch, annotations, **_):
    user_id = spec["userId"]
    contact_name = spec["contactName"]
    quotas = spec.get("quotas", None)
    suspended = spec.get("suspended", False)

    if quotas and quotas["enabled"]:
        max_buckets = quotas["maxBuckets"]
        max_size = quotas["maxSize"]
        max_objects = quotas["maxObjects"]
    else:
        max_buckets = None
        max_size = None
        max_objects = None

    allow_import = is_annotation_set(annotations, "allow-import")

    if is_annotation_set(annotations, "skip-tenant"):
        rgw_user_id = user_id
    else:
        rgw_user_id = f"{tenant}-{user_id}"

    rgw = RGWAdmin(**get_environment_creds())
    try:
        await rgw.get_user(uid=rgw_user_id)
        if not allow_import:
            patch.status["ready"] = False
            raise kopf.PermanentError(
                "A user with the same user_id already exists and import was denied"
            )
    except NoSuchUser:
        await rgw.create_user(
            uid=rgw_user_id,
            display_name=contact_name,
            max_buckets=max_buckets,
            generate_key=False,
            suspended=suspended
        )

    if quotas and quotas["enabled"]:
        await rgw.set_user_quota(
            uid=rgw_user_id,
            quota_type="user",
            max_size_kb=max_size,
            max_objects=max_objects,
            enabled=True,
        )

    patch.status["ready"] = True


@kopf.on.update("s3.hanse-merkur.de", "v1alpha1", "user")
async def update_user(spec, old, new, annotations, logger, **_):
    if old["spec"]["userId"] != new["spec"]["userId"]:
        raise kopf.PermanentError("UserId cannot be changed inflight")

    user_id = spec["userId"]
    if is_annotation_set(annotations, "skip-tenant"):
        rgw_user_id = user_id
    else:
        rgw_user_id = f"{tenant}-{user_id}"

    contact_name = spec["contactName"]
    quotas = spec.get("quotas", None)
    suspended = spec.get("suspended", False)

    if quotas and quotas["enabled"]:
        max_buckets = quotas["maxBuckets"]
        max_size = quotas["maxSize"]
        max_objects = quotas["maxObjects"]
    else:
        max_buckets, max_size, max_objects = (None, None, None)

    rgw = RGWAdmin(**get_environment_creds())
    if max_buckets:
        await rgw.modify_user(uid=rgw_user_id, display_name=contact_name, suspended=suspended, max_buckets=max_buckets)
    else:
        await rgw.modify_user(uid=rgw_user_id, display_name=contact_name, suspended=suspended)

    if quotas and quotas["enabled"]:
        await rgw.set_user_quota(
            uid=rgw_user_id,
            quota_type="user",
            max_size_kb=max_size,
            max_objects=max_objects,
            enabled=True,
        )
        if not old.get("spec", {"quotas": {"enabled": False}})["quotas"]["enabled"]:
            logger.info("Quotas enabled for user %s", user_id)
    else:
        await rgw.set_user_quota(uid=rgw_user_id, quota_type="user", enabled=False)


@kopf.on.delete("s3.hanse-merkur.de", "v1alpha1", "user")
async def delete_user(spec, annotations, status, **_):
    user_id = spec["userId"]
    allow_deletion = is_annotation_set(annotations, "allow-deletion")

    if is_annotation_set(annotations, "skip-tenant"):
        rgw_user_id = user_id
    else:
        rgw_user_id = f"{tenant}-{user_id}"

    if not allow_deletion:
        raise kopf.PermanentError("The deletion was not allowed with current settings")

    # We didn't own the user
    if not status["ready"]:
        return

    rgw = RGWAdmin(**get_environment_creds())
    try:
        await rgw.remove_user(uid=rgw_user_id, purge_data=True)
        # No response. At all. Thanks
    except NoSuchUser:
        logging.error("Trying to delete non-existent user %s", rgw_user_id)


def is_user_ready(body, **_) -> bool:
    return body.status.get("ready", False)


@kopf.timer(
    "s3.hanse-merkur.de", "v1alpha1", "user", interval=60, idle=120, when=is_user_ready
)
async def update_user_stats(spec, patch, annotations, **_):
    try:
        rgw = RGWAdmin(**get_environment_creds())
        user_id = spec["userId"]

        if is_annotation_set(annotations, "skip-tenant"):
            rgw_user_id = user_id
        else:
            rgw_user_id = f"{tenant}-{user_id}"

        try:
            user = await rgw.get_user(rgw_user_id, stats=True)
            patch.status["buckets"] = len(rgw.get_bucket(uid=rgw_user_id))
            patch.status["objects"] = user["stats"]["num_objects"]
            patch.status["sizeInKb"] = user["stats"]["size_kb"]
        except NoSuchUser:
            logging.error("Observing non-existant user %s", rgw_user_id)
    except asyncio.CancelledError:
        # Timer was cancelled during runtime. Just give up
        pass


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    logging.getLogger("rgwadmin.rgw").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
