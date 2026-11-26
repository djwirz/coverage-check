# 11. Postgres as a plain StatefulSet, kind's own networking, and repo-local tools

Date: 2026-11-26
Status: Accepted

## Context
Slice 4 puts the system on kind (ADR 0002). Spec §13 Q1 asked how Postgres runs there and whether kind's default networking can enforce the NetworkPolicies of slice 5. ADR 0002 also left the exact tool versions to be pinned now.

## Options considered
Postgres:
1. **A StatefulSet with one replica and a PersistentVolumeClaim.** A few objects, all readable.
2. **The CloudNativePG operator.** Failover and backups, at the cost of an operator to learn and run.
3. **Outside the cluster**, in Compose. Less to build, but then the chaos run can't restart it as a pod.

Networking:
1. **kind's default (kindnet).** kind's release notes say it enforces NetworkPolicy since v0.24.
2. **Another CNI**, such as Calico. More to install and pin.

Tools:
1. **Fetched into the repo's `.cache/`**, pinned and checked by sha256.
2. **Whatever is installed on the machine.** Nothing to download, and different everywhere.

## Decision
Option 1 for all three. Pinned now: kind v0.33.0, its Kubernetes v1.37.0 node image by digest, kubectl v1.37.3, Postgres 18 by digest. The scripts use their own kubeconfig file in the repo and refuse any other cluster.

## Why
Nothing in the invariants needs database failover: what they need is that a restart loses nothing, which a volume gives. The default network is claimed to do what slice 5 needs, so adding one would be effort without a reason. Pinned local tools make the laptop and CI the same and leave the user's own kubectl and contexts alone.

## Consequences
- One Postgres: while it is down, nothing progresses. That is a thing to show, not to hide.
- The volume is local to one node, so the pod can only come back on that node.
- **NetworkPolicy enforcement is not yet shown here.** Slice 5 proves it with a policy that must block, before relying on it.
- A cluster needs several gigabytes of free disk: three nodes, each with its own copy of the images.
- Upgrading a tool means changing its version and checksum together.
- A cloud cluster (slice 6) would revisit the database.
