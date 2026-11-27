"""CDK app: CiRoles (by hand, once) and Demo (the demo window). docs/deploy.md.

Environment-agnostic apart from the region: the account comes from the credentials at
deploy time, so no account ID is written into a template, a log or the repo.
"""

from __future__ import annotations

import aws_cdk as cdk

from infra.stacks.ci_roles import CiRolesStack
from infra.stacks.demo import DemoStack
from infra.stacks.names import REGION

ENV = cdk.Environment(region=REGION)


def main() -> None:
    app = cdk.App()
    CiRolesStack(app, "CiRoles", env=ENV)
    DemoStack(app, "Demo", env=ENV)
    cdk.Tags.of(app).add("project", "coverage-check")
    app.synth()


if __name__ == "__main__":
    main()
