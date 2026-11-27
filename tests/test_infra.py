"""The templates say what ADR 0014 says. No AWS account or credentials needed."""

from __future__ import annotations

import json
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.app import ENV
from infra.stacks.ci_roles import DEPLOY_SUBJECT, CiRolesStack
from infra.stacks.demo import NODE_COUNT, NODE_TYPE, DemoStack
from infra.stacks.names import ADMIN_ROLE_NAME, CLUSTER_NAME, REGION, REPOSITORY_NAME


@pytest.fixture(scope="module")
def demo() -> Template:
    return Template.from_stack(DemoStack(cdk.App(), "Demo", env=ENV))


@pytest.fixture(scope="module")
def roles() -> Template:
    return Template.from_stack(CiRolesStack(cdk.App(), "CiRoles", env=ENV))


def test_nothing_that_charges_by_the_hour_beyond_the_plan(demo: Template) -> None:
    demo.resource_count_is("AWS::EC2::NatGateway", 0)
    demo.resource_count_is("AWS::EC2::EIP", 0)
    demo.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)
    demo.resource_count_is("AWS::EKS::Cluster", 1)
    demo.has_resource_properties(
        "AWS::EKS::Nodegroup",
        {
            "InstanceTypes": [NODE_TYPE],
            "ScalingConfig": {
                "MinSize": NODE_COUNT,
                "DesiredSize": NODE_COUNT,
                "MaxSize": NODE_COUNT,
            },
        },
    )


def test_everything_goes_with_the_stack(demo: Template) -> None:
    for logical_id, resource in demo.to_json()["Resources"].items():
        assert resource.get("DeletionPolicy") != "Retain", logical_id


def test_network_policies_are_enforced(demo: Template) -> None:
    demo.has_resource_properties(
        "AWS::EKS::Addon",
        {"AddonName": "vpc-cni", "ConfigurationValues": '{"enableNetworkPolicy": "true"}'},
    )


def test_ci_trusts_one_subject_and_creates_no_provider(roles: Template) -> None:
    roles.resource_count_is("AWS::IAM::OIDCProvider", 0)
    roles.resource_count_is("Custom::AWSCDKOpenIdConnectProvider", 0)
    roles.resource_count_is("AWS::IAM::Role", 1)
    roles.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Condition": {
                                "StringEquals": {
                                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                                    "token.actions.githubusercontent.com:sub": DEPLOY_SUBJECT,
                                }
                            }
                        }
                    )
                ]
            }
        },
    )
    assert DEPLOY_SUBJECT.endswith(":environment:demo")
    assert ADMIN_ROLE_NAME in json.dumps(roles.to_json())


def test_ci_may_do_what_the_scripts_ask_of_it(roles: Template) -> None:
    """Every `aws` call made with the deploy role's own credentials, not an assumed one."""
    root = Path(__file__).parent.parent
    scripts = [root / "scripts/k8s/env.sh", *root.glob("scripts/cloud/*.sh")]
    called = {
        (service, operation)
        for path in scripts
        for service, operation in re.findall(
            r"\baws (eks|ec2) ([a-z-]+)", path.read_text().replace("\\\n", " ")
        )
    }
    allowed = json.dumps(roles.to_json())
    for service, operation in called:
        if operation == "update-kubeconfig":
            operation = "describe-cluster"
        action = f"{service}:{''.join(word.title() for word in operation.split('-'))}"
        assert action in allowed, action
    assert ("eks", "describe-cluster") in called


def test_no_account_id_in_any_template(demo: Template, roles: Template) -> None:
    for template in (demo, roles):
        text = json.dumps(template.to_json())
        # Asset hashes are long hex strings; an account ID is twelve digits on its own.
        assert not re.search(r"(?<![0-9a-f])\d{12}(?![0-9a-f])", text)


def test_the_scripts_use_the_same_names() -> None:
    root = Path(__file__).parent.parent
    scripts = " ".join(
        p.read_text() for p in [root / "scripts/k8s/env.sh", *root.glob("scripts/cloud/*.sh")]
    )
    for name in (CLUSTER_NAME, ADMIN_ROLE_NAME, REPOSITORY_NAME, REGION):
        assert name in scripts, name
