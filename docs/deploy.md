# The demo window on EKS

A cluster that exists for a few hours, to run the same checks on real machines (ADR 0014). **It costs money while it exists:** about a third of a dollar an hour. Left for a month it would use up the account's free-plan credits and suspend the account.

## Once, before the first window

Signed in as an admin (`aws login --profile token-keeper`), from the repo's root:

1. **The budget** must be there, since it is what catches a forgotten cluster. This should list `token-keeper-monthly`:
   ```bash
   aws budgets describe-budgets --account-id "$(aws sts get-caller-identity --query Account --output text --profile token-keeper)" --query 'Budgets[].BudgetName' --profile token-keeper
   ```
2. **CDK's bootstrap** must be there (token-keeper made it). This should print `CREATE_COMPLETE` or `UPDATE_COMPLETE`:
   ```bash
   aws cloudformation describe-stacks --stack-name CDKToolkit --region us-east-1 --query 'Stacks[0].StackStatus' --output text --profile token-keeper
   ```
3. **The deploy role:**
   ```bash
   npm ci && AWS_PROFILE=token-keeper npx cdk deploy CiRoles
   ```
4. **The `demo` environment** on GitHub (Settings → Environments → New → `demo`): add yourself as required reviewer, and limit deployment branches to `main`. Both matter: they are what stands between a branch and a deploy.
5. **The role's ARN** as that environment's secret, without printing it:
   ```bash
   aws cloudformation describe-stacks --stack-name CoverageCheckCiRoles --region us-east-1 --profile token-keeper --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text | gh secret set AWS_DEPLOY_ROLE_ARN --env demo
   ```

## A window

```bash
gh workflow run demo-window.yml --ref main
```

Approve it on the run's page. It creates the cluster, deploys, runs the end-to-end checks and three chaos runs of 3,000 checks, writes the payer's peaks to the run's summary, destroys everything and checks that nothing is left. Expect around an hour and a half.

**Watch the last two steps.** "Is anything left?" must be green.

## If teardown didn't happen

```bash
gh workflow run demo-destroy.yml --ref main
```

Or from an admin session, which needs Node, `npm ci` and `aws`:

```bash
AWS_PROFILE=token-keeper ./scripts/cloud/down.sh
```

```bash
AWS_PROFILE=token-keeper ./scripts/cloud/leftovers.sh
```

If `cdk destroy` itself fails, delete the `CoverageCheckDemo` stack in CloudFormation's console and look for anything it kept.

## Looking at a live window

```bash
AWS_PROFILE=token-keeper ./scripts/cloud/connect.sh
```

```bash
AWS_PROFILE=token-keeper COVERAGE_CHECK_TARGET=demo ./scripts/k8s/forward.sh dashboard
```
