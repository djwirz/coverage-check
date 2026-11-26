#!/usr/bin/env bash
# After teardown: is anything still there that would be billed? Fails if so.
set -euo pipefail
export AWS_REGION=us-east-1
found=0

none() { # what, then an aws command printing one line per thing
  local what=$1 listed
  shift
  listed=$("$@" --output text)
  if [ -n "$listed" ] && [ "$listed" != None ]; then
    echo "LEFT OVER: $what: $listed"
    found=1
  else
    echo "none: $what"
  fi
}

none "demo cluster" aws eks list-clusters --query "clusters[?@=='coverage-check-demo']"
none "volumes of the namespace" aws ec2 describe-volumes \
  --filters Name=tag:kubernetes.io/created-for/pvc/namespace,Values=coverage-check \
  --query 'Volumes[].VolumeId'
# In case the driver didn't tag them with the namespace. This account runs no other cluster.
none "volumes any EBS CSI driver made" aws ec2 describe-volumes \
  --filters Name=tag-key,Values=CSIVolumeName --query 'Volumes[].VolumeId'
none "demo instances" aws ec2 describe-instances \
  --filters Name=tag:eks:cluster-name,Values=coverage-check-demo \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId'
none "NAT gateways of this project" aws ec2 describe-nat-gateways \
  --filter Name=tag:project,Values=coverage-check Name=state,Values=pending,available \
  --query 'NatGateways[].NatGatewayId'
none "addresses of this project" aws ec2 describe-addresses \
  --filters Name=tag:project,Values=coverage-check --query 'Addresses[].AllocationId'
exit $found
