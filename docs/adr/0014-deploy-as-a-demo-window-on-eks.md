# 14. Deploy as a short demo window on EKS, with CDK

Date: 2026-11-26
Status: Accepted

## Context
Spec §13 Q3 asked where to deploy, and Q4 said that if it was a cloud, Terraform would build it. §10.6 wants a deploy with a budget alarm first, or a recorded reason not to. Dan has an AWS account on the free plan, with about 150 dollars of credits and at least six months left, which token-keeper already runs on. One thing a local cluster can't show: whether the margin under the payer's limit holds when requests cross a real network between machines. On kind the payer has seen 19 in a second against a limit of 20.

Prices on AWS's pages on 2026-11-26: the EKS control plane is 0.10 dollars an hour and a public IPv4 address 0.005. The page for the node type showed no price; from memory it is about 0.10 dollars an hour, **not verified**. All told the cluster is about a third of a dollar an hour, a few hours cost a dollar or two, and a month left running would cost well over the credits. When free-plan credits run out, the account is suspended, token-keeper's demo with it.

## Options considered
Where:
1. **Stay local:** CI's kind cluster is the deployed environment. Free; the margin question stays open.
2. **EKS for a few hours at a time.**
3. **A cluster left running** (Hetzner, DigitalOcean, or k3s on Oracle's free tier). A monthly bill or an unreliable free machine, for a demo nobody visits between times. A single machine also answers nothing about the network.

Built with:
1. **Terraform**, as §13 Q4 assumed.
2. **AWS CDK in Python**, as token-keeper does.

Nodes:
1. **3 × t3.small.** Cheaper, but each holds about a dozen pods, too few for the system plus ten workers.
2. **2 × m7i-flex.large**, also free-plan eligible.

## Decision
Where 2, built with 2, nodes 2.

- `infra/` holds two stacks. `CiRoles` is deployed by hand once. `Demo` (a VPC with public subnets and no NAT gateway, the cluster, the nodes, the image registry) is created and destroyed by the `Demo window` workflow, started by hand, from `main`, with approval.
- **Teardown is a step of the same job**, run even after a failure, followed by a check that nothing is left. `Demo destroy` is there if that didn't happen.
- The GitHub OIDC provider is **token-keeper's** (an account has one per issuer). It is referred to, not created, so deleting token-keeper's `CiRoles` stack would stop these deploys.
- The budget alarm is the account's existing 5 dollar one, which has no filter and so covers this. Dan confirmed on 2026-11-26 that it is there and that the account can reach EKS.
- Cluster scripts act on the demo cluster only when `COVERAGE_CHECK_TARGET=demo`, with a separate kubeconfig, after checking it against the endpoint AWS gives for that cluster.
- **No limiter setting changes for the window.** It measures the one we have.

## Why
It answers the one open question about invariant 1 that more local runs can't, for a dollar or two. CDK is already proven on this account, in the language of the project, with the OIDC pattern worked out; a second tool for one stack would earn nothing.

## Consequences
- There is no always-on public deployment. Between windows, CI's kind cluster is where the system runs.
- "Deploy from main" is by hand, not on every push, because each run costs money.
- Whether the free plan allows creating a cluster is only certain after the first window.
- CloudFormation's execution role from CDK's bootstrap is an administrator, as token-keeper's ADR 0016 notes, so an approved run could create anything in the account.
- The admin role of the demo can be assumed by any principal in the account that is allowed to assume roles.
- A job that is killed outright can skip teardown. The budget alarm is what catches that, at about half a day.
- Spec §13 Q4's Terraform is replaced by CDK.
