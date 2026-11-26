"""GitHub Actions → AWS through OIDC, no stored keys (ADR 0014, after token-keeper's ADR 0016).

Deployed by hand, once, from an admin session. CI never deploys it, so CI can't widen its
own trust. The account holds one GitHub OIDC provider, and token-keeper's CiRoles stack
owns it: it is referred to here, never created.

The one role trusts only a job in this repo's `demo` environment, whose rules allow only
`main` and require approval. It can assume CDK's bootstrap roles and the demo cluster's
admin role, describe that cluster, and read what might be left over after teardown.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, LegacyStackSynthesizer, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.stacks.names import ADMIN_ROLE_NAME, CLUSTER_NAME

# The prefix GitHub reports (gh api repos/djwirz/coverage-check/actions/oidc/customization/sub):
# immutable subjects name the owner and repo by name and numeric ID.
SUBJECT_PREFIX = "repo:djwirz@15788618/coverage-check@1468770638"
DEPLOY_SUBJECT = f"{SUBJECT_PREFIX}:environment:demo"
OIDC_HOST = "token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
# CDK's default bootstrap qualifier. token-keeper bootstrapped this account and region.
BOOTSTRAP_QUALIFIER = "hnb659fds"
BOOTSTRAP_ROLES = ("deploy", "file-publishing", "lookup")
LEFTOVER_READS = (
    "ec2:DescribeAddresses",
    "ec2:DescribeInstances",
    "ec2:DescribeNatGateways",
    "ec2:DescribeVolumes",
    "eks:ListClusters",
    "elasticloadbalancing:DescribeLoadBalancers",
)


class CiRolesStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        kwargs.setdefault("synthesizer", LegacyStackSynthesizer())
        kwargs.setdefault("stack_name", "CoverageCheckCiRoles")
        super().__init__(scope, construct_id, **kwargs)
        iam_arn = f"arn:{self.partition}:iam::{self.account}"

        role = iam.Role(
            self,
            "DeployRole",
            assumed_by=iam.WebIdentityPrincipal(
                f"{iam_arn}:oidc-provider/{OIDC_HOST}",
                conditions={
                    "StringEquals": {
                        f"{OIDC_HOST}:aud": AUDIENCE,
                        f"{OIDC_HOST}:sub": DEPLOY_SUBJECT,
                    }
                },
            ),
            description="coverage-check: the demo window, from main through the demo environment",
            max_session_duration=Duration.hours(1),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    f"{iam_arn}:role/cdk-{BOOTSTRAP_QUALIFIER}-{kind}-role-"
                    f"{self.account}-{self.region}"
                    for kind in BOOTSTRAP_ROLES
                ]
                + [f"{iam_arn}:role/{ADMIN_ROLE_NAME}"],
            )
        )
        role.add_to_policy(iam.PolicyStatement(actions=list(LEFTOVER_READS), resources=["*"]))
        # The scripts ask for the cluster's endpoint, to be sure of what they are talking to.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["eks:DescribeCluster"],
                resources=[
                    f"arn:{self.partition}:eks:{self.region}:{self.account}:cluster/{CLUSTER_NAME}"
                ],
            )
        )
        CfnOutput(self, "DeployRoleArn", value=role.role_arn)
