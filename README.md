# rgw-operator

Operator for managing S3 resources on a Ceph RadosGW installation via Kubernetes Custom Resources.

## Operator RGW User

Creating an initial S3 user on the Ceph RadosGW, which will be used to create all resources via the operator.

```bash
$ radosgw-admin user create --uid=operator --display-name="Kubernetes Operator" --gen-access-keys
$ radosgw-admin --id admin caps add --caps="buckets=*;users=*;usage=*;metadata=*" --uid=operator
```

## Ceph User

Neue Nutzer können im RadosGW via folgender CRD angelegt werden

Creating new users which will be used to logicially separate entities.

```yaml
apiVersion: s3.hanse-merkur.de/v1alpha1
kind: User
metadata:
  name: operator-test
  annotations:
    # The `allow-deletion` annotation enables the actual deletion of the Ceph User, aside from the resource itself
    s3.hanse-merkur.de/allow-deletion: "true"
    # `allow-import` allows the import of existing Ceph users under the management of the resource
    s3.hanse-merkur.de/allow-import: "true"
    # `skip-tenant` unsets the operator configured tenant as prefix for the Ceph user
    s3.hanse-merkur.de/skip-tenant: "true"
spec:
  # Unless `skip-tenant` is set the user will be named <tenant>-<userId>
  userId: operator-test
  # Free form text field for contact information
  contactName: Operator Test
  # Enable quotas for the user globally. Applied to all buckets
  quotas:
    enabled: true
    maxObjects: 1000000
    maxSize: 1024000
    maxBuckets: 10
```

Created Ceph users have to be unique, if using the tenant unique to the operator installaton.

## Access Keys

Access Keys allow the access to the Bucket and other S3 functionality. The
resource, unlike the User, is namespaced as it can access a Secret containing
the access and secret keys. Access Keys are unique and can be auto-generated or
use existing Kubernetes secrets.

```yaml
apiVersion: s3.hanse-merkur.de/v1alpha1
kind: AccessKey
metadata:
  name: operator-test
  namespace: default
spec:
  # References the User resource created by the previous YAML
  owner: operator-test
  # Free form text field for general descriptions
  description: Access key for operator-test
  # Name of the secret created or existing in the same namespace as the AccessKey resource
  secretName: operator-test
  # Using jinja2 the resoluting secret can optionally be modified to be application specific as seen in this example
  template:
    metadata:
      labels:
        hansemerkur.de/owned-by: some-group
      annotations:
        hansemerkur.de/cleanup: none
    data:
      credentials: |
        [default]
        aws_secret_key_id={{ data.aws_access_key_id }}
        aws_secret_access_key={{ data.aws_secret_access_key }}
```

## Buckets

Buckets are namespaced resources and use the previously AccessKey to create and
update the Bucket in Ceph.

```yaml
apiVersion: s3.hanse-merkur.de/v1alpha1
kind: Bucket
metadata:
  name: operator-test
  namespace: default
  annotations:
    # The `allow-deletion` annotation enables the actual deletion of the Ceph Bucket, aside from the resource itself
    s3.hanse-merkur.de/allow-deletion: "true"
    # Without the `force-deletion` annotation the bucket will never be deleted as long as it still contains objects
    s3.hanse-merkur.de/force-deletion: "true"
    # `allow-import` allows the import of an existing bucket under the management of the resource
    s3.hanse-merkur.de/allow-import: "true"
spec:
  bucketName: operator-test
  # References the previously created AccessKey
  ownerAccessKey: operator-test
  # A selection of private, public or custom bucket policy. In case of custom the customBucketPolicy field is expected
  bucketPolicy: private
  # Optional standard AWS bucket policy format in JSON
  #customBucketPolicy: "{}"
  # Optional AWS Lifecycle Policy in JSON format
  lifeCyclePolicy: |
    {
      "Rules": [
        {
          "Status": "Enabled",
          "Expiration": {
            "Days": 31
          },
          "ID": "ExpireSnapshots"
        }
      ]
    }
  # Optional bucket quotas
  quotas:
    enabled: true
    maxObjects: 100000
    maxSize: 102400
```

## Software Frameworks used

- https://github.com/UMIACS/rgwadmin
- https://github.com/nolar/kopf
