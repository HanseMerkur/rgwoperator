import os

def is_annotation_set(annotations, key: str) -> bool:
    return (
        f"s3.hanse-merkur.de/{key}" in annotations
        and annotations[f"s3.hanse-merkur.de/{key}"] == "true"
    )


def get_environment_creds():
    return {'access_key': os.environ['OBJ_ACCESS_KEY_ID'],
            'secret_key': os.environ['OBJ_SECRET_ACCESS_KEY'],
            'server': os.environ['OBJ_SERVER'],
            'secure': 'OBJ_SECURE' in os.environ,
            'verify': 'OBJ_VERIFY' in os.environ}