"""The demo window: an EKS cluster that exists for a few hours (ADR 0014).

Created and destroyed by .github/workflows/demo-window.yml. Left running it costs about as
much a month as the account's credits would bear for only a few weeks, so nothing here is
meant to be kept: every resource goes with the stack.

No NAT gateway and no load balancer: the nodes sit in public subnets, nothing listens for the
internet, and the pages are reached by port-forward.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_eks_v2 as eks
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.stacks.names import ADMIN_ROLE_NAME, CLUSTER_NAME, REPOSITORY_NAME

# Free-plan eligible. Two, so pods really move between machines and the policies cross a
# network. t3.small would be cheaper, but holds too few pods for ten workers.
NODE_TYPE = "m7i-flex.large"
NODE_COUNT = 2


class DemoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        kwargs.setdefault("stack_name", "CoverageCheckDemo")
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    map_public_ip_on_launch=True,
                )
            ],
        )
        public = [ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)]
        cluster = eks.Cluster(
            self,
            "Cluster",
            cluster_name=CLUSTER_NAME,
            version=eks.KubernetesVersion.V1_37,
            vpc=vpc,
            vpc_subnets=public,
            endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
            default_capacity_type=eks.DefaultCapacityType.NODEGROUP,
            default_capacity=0,
        )

        # Without this the NetworkPolicies are accepted and silently ignored. netpol.sh
        # would catch that.
        network = eks.CfnAddon(
            self,
            "VpcCni",
            cluster_name=cluster.cluster_name,
            addon_name="vpc-cni",
            configuration_values='{"enableNetworkPolicy": "true"}',
            resolve_conflicts="OVERWRITE",
        )
        nodes = cluster.add_nodegroup_capacity(
            "Nodes",
            instance_types=[ec2.InstanceType(NODE_TYPE)],
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            min_size=NODE_COUNT,
            desired_size=NODE_COUNT,
            max_size=NODE_COUNT,
        )
        nodes.node.add_dependency(network)

        # Volumes for Postgres and the payer's log need the EBS driver, with its own role.
        eks.CfnAddon(
            self,
            "PodIdentity",
            cluster_name=cluster.cluster_name,
            addon_name="eks-pod-identity-agent",
        ).node.add_dependency(nodes)
        disks = iam.Role(
            self,
            "EbsCsiRole",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEBSCSIDriverPolicyV2")
            ],
        )
        assert disks.assume_role_policy is not None  # noqa: S101 - set by assumed_by
        disks.assume_role_policy.add_statements(
            iam.PolicyStatement(
                actions=["sts:TagSession"],
                principals=[iam.ServicePrincipal("pods.eks.amazonaws.com")],
            )
        )
        driver = eks.CfnAddon(
            self,
            "EbsCsi",
            cluster_name=cluster.cluster_name,
            addon_name="aws-ebs-csi-driver",
            pod_identity_associations=[
                eks.CfnAddon.PodIdentityAssociationProperty(
                    service_account="ebs-csi-controller-sa", role_arn=disks.role_arn
                )
            ],
        )
        driver.node.add_dependency(nodes)

        repository = ecr.Repository(
            self,
            "Repository",
            repository_name=REPOSITORY_NAME,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # What the workflow (and an admin, to look) uses to push the image and run kubectl.
        admin = iam.Role(
            self,
            "AdminRole",
            role_name=ADMIN_ROLE_NAME,
            assumed_by=iam.AccountRootPrincipal(),
        )
        repository.grant_pull_push(admin)
        admin.add_to_policy(
            iam.PolicyStatement(actions=["eks:DescribeCluster"], resources=[cluster.cluster_arn])
        )
        cluster.grant_access(
            "Admin",
            admin.role_arn,
            [
                eks.AccessPolicy.from_access_policy_name(
                    "AmazonEKSClusterAdminPolicy", access_scope_type=eks.AccessScopeType.CLUSTER
                )
            ],
        )
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
