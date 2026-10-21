# Clean up

This deletes every approved fact, report and approval record. Copy anything you want to keep out of
the document bucket first.

> [!NOTE]
> Run every step from the repository root.

## 1. Offboard each cluster

```bash
cd infra/terraform/cluster-access
terraform workspace select my-eks-cluster
terraform destroy -var-file=my-eks-cluster.tfvars
terraform workspace select default
terraform workspace delete my-eks-cluster
cd ../../..
```

## 2. Keep a pre-existing SES identity (only if it applies)

If an approval address was already verified in SES before you installed this sample, or something
else uses it, remove it from Terraform state so destroy does not delete it:

```bash
terraform -chdir=infra/terraform/deploy state list | grep aws_ses_email_identity
terraform -chdir=infra/terraform/deploy state rm 'module.research.aws_ses_email_identity.approval_from'
```

Remove any `module.research.aws_ses_email_identity.approval_to["..."]` entry the list shows the
same way.

## 3. Turn off DynamoDB deletion protection

```bash
PREFIX=eks-addon-upgrades REGION=us-east-1
for t in "$PREFIX-facts" "$PREFIX-fact-marks"; do
  aws dynamodb update-table --region "$REGION" --table-name "$t" --no-deletion-protection-enabled
done
```

## 4. Empty the document bucket

The bucket is versioned, so every version and delete marker must go. Use **Empty** in the S3
console, or (needs `jq`):

```bash
BUCKET=$(terraform -chdir=infra/terraform/deploy output -raw document_bucket)
REGION=us-east-1
if [ -z "$BUCKET" ]; then
  echo "No bucket name: run this from the repository root."
else
  while :; do
    batch=$(aws s3api list-object-versions --region "$REGION" --bucket "$BUCKET" --max-items 500 --output json --query '{Objects: [Versions, DeleteMarkers][][].{Key: Key, VersionId: VersionId}, Quiet: `true`}') || break
    [ "$(echo "$batch" | jq '.Objects | length')" -eq 0 ] && break
    aws s3api delete-objects --region "$REGION" --bucket "$BUCKET" --delete "$batch" > /dev/null || break
  done
  aws s3api list-object-versions --region "$REGION" --bucket "$BUCKET" \
    --query '[length(Versions||`[]`), length(DeleteMarkers||`[]`)]' --output text
fi
```

The last command should print `0	0`: no versions and no delete markers left.

`--max-items` caps the versions and the delete markers separately, so 500 keeps each batch within
the 1,000 objects one `delete-objects` call accepts. Over that limit S3 answers `MalformedXML`.

The dedicated server-access-log bucket (`<prefix>-logs-<account>-<region>-an`) does **not** need
this step: it is created with `force_destroy = true`, so `make destroy` empties and removes it
automatically, including its versions and delete markers.

## 5. Destroy

From the repository root. Terraform evaluates the Lambda definitions even when destroying, and
they hash the zips in `build/`, so build them first if they are missing:

```bash
ls build/*.zip > /dev/null 2>&1 || make build
make destroy
```

The plan lists fewer resources than `terraform state list` shows: data sources are lookups and
are never destroyed. AgentCore harnesses and gateways take a few minutes to delete.

## 6. Check nothing is left

```bash
terraform -chdir=infra/terraform/deploy state list
PREFIX=eks-addon-upgrades REGION=us-east-1
aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --query "ResourceTagMappingList[?contains(ResourceARN,'$PREFIX')].ResourceARN" --output text
aws iam list-roles --query "Roles[?starts_with(RoleName,'$PREFIX')].RoleName" --output text
```

All three should print nothing.
