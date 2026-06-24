"""
pipeline_generator.py — Full multi-stage GitHub Actions CI/CD pipeline
───────────────────────────────────────────────────────────────────────
Generates a complete GitHub Actions workflow with:

  Stage 1: lint + security scan (tflint, checkov)
  Stage 2: build (Docker -> ECR, or Lambda package, or static asset)
  Stage 3: test (unit + integration)
  Stage 4: terraform plan (matrix: all environments)
  Stage 5: deploy dev (auto, on push to main)
  Stage 6: deploy staging (manual approval gate via GitHub Environments)
  Stage 7: deploy prod (manual approval + required reviewers)
  Stage 8: rollback (manual workflow_dispatch trigger)

For compute-specific deploy steps (EKS helm, Lambda update-function-code,
ECS update-service, EC2 rolling update), the AI client generates the
appropriate steps. Static fallbacks handle the common cases without AI.

Output: .infra/cicd/pipeline.yml + .infra/cicd/README.md
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer
import yaml

import ai_client as aic

# ─────────────────────────────────────────────────────────────────────────────
# Static deploy steps per compute type (used when AI is unavailable)
# ─────────────────────────────────────────────────────────────────────────────

_STATIC_BUILD_STEPS: dict[str, str] = {
    "eks": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to Amazon ECR
        id: ecr-login
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push Docker image
        id: build
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.ecr-login.outputs.registry }}/${{ env.PROJECT_NAME }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Set image output
        id: image
        run: echo "tag=${{ steps.ecr-login.outputs.registry }}/${{ env.PROJECT_NAME }}:${{ github.sha }}" >> "$GITHUB_OUTPUT"
""",
    "ecs-fargate": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Login to Amazon ECR
        id: ecr-login
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.ecr-login.outputs.registry }}/${{ env.PROJECT_NAME }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
""",
    "lambda": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Package Lambda function
        run: |
          pip install -r requirements.txt -t ./package
          cd package && zip -r ../function.zip . && cd ..
          zip function.zip *.py

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: lambda-package
          path: function.zip
""",
    "ec2": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Build application artifact
        run: |
          # Replace with your build command
          echo "Building application..."
""",
    "static-site": """
      - name: Build static assets
        run: |
          npm ci
          npm run build

      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: static-build
          path: dist/
""",
}

_STATIC_DEPLOY_STEPS: dict[str, str] = {
    "eks": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Update kubeconfig
        run: aws eks update-kubeconfig --name ${{ env.PROJECT_NAME }}-${{ matrix.environment }} --region ${{ env.AWS_REGION }}

      - name: Helm upgrade
        run: |
          helm upgrade --install ${{ env.PROJECT_NAME }} ./helm \
            --namespace ${{ env.PROJECT_NAME }} \
            --create-namespace \
            --set image.tag=${{ needs.build.outputs.image-tag }} \
            --set environment=${{ matrix.environment }} \
            --values ./helm/values-${{ matrix.environment }}.yaml \
            --wait --timeout 10m
""",
    "ecs-fargate": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Download task definition
        run: |
          aws ecs describe-task-definition \
            --task-definition ${{ env.PROJECT_NAME }}-${{ matrix.environment }} \
            --query taskDefinition > task-definition.json

      - name: Update ECS task definition image
        id: task-def
        uses: aws-actions/amazon-ecs-render-task-definition@v1
        with:
          task-definition: task-definition.json
          container-name: ${{ env.PROJECT_NAME }}
          image: ${{ needs.build.outputs.image-tag }}

      - name: Deploy to ECS
        uses: aws-actions/amazon-ecs-deploy-task-definition@v1
        with:
          task-definition: ${{ steps.task-def.outputs.task-definition }}
          service: ${{ env.PROJECT_NAME }}-${{ matrix.environment }}
          cluster: ${{ env.PROJECT_NAME }}-${{ matrix.environment }}
          wait-for-service-stability: true
""",
    "lambda": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Download Lambda package
        uses: actions/download-artifact@v4
        with:
          name: lambda-package

      - name: Update Lambda function code
        run: |
          aws lambda update-function-code \
            --function-name ${{ env.PROJECT_NAME }}-${{ matrix.environment }} \
            --zip-file fileb://function.zip

      - name: Publish Lambda version and update alias
        run: |
          VERSION=$(aws lambda publish-version \
            --function-name ${{ env.PROJECT_NAME }}-${{ matrix.environment }} \
            --query Version --output text)
          aws lambda update-alias \
            --function-name ${{ env.PROJECT_NAME }}-${{ matrix.environment }} \
            --name ${{ matrix.environment }} \
            --function-version $VERSION
""",
    "ec2": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Deploy via SSM Run Command
        run: |
          aws ssm send-command \
            --document-name "AWS-RunShellScript" \
            --targets "Key=tag:Environment,Values=${{ matrix.environment }}" \
            --parameters "commands=['cd /app && git pull && ./deploy.sh']" \
            --region ${{ env.AWS_REGION }}
""",
    "static-site": """
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Download build artifact
        uses: actions/download-artifact@v4
        with:
          name: static-build
          path: dist/

      - name: Deploy to S3
        run: |
          aws s3 sync dist/ s3://${{ env.PROJECT_NAME }}-${{ matrix.environment }}-assets \
            --delete --cache-control "max-age=31536000"

      - name: Invalidate CloudFront
        run: |
          DIST_ID=$(aws cloudfront list-distributions \
            --query "DistributionList.Items[?Comment=='${{ env.PROJECT_NAME }}-${{ matrix.environment }}'].Id" \
            --output text)
          aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
""",
}

_ROLLBACK_STEPS: dict[str, str] = {
    "eks": """
      - name: Rollback Helm release
        run: |
          aws eks update-kubeconfig --name ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} --region ${{ env.AWS_REGION }}
          helm rollback ${{ env.PROJECT_NAME }} --namespace ${{ env.PROJECT_NAME }}
""",
    "ecs-fargate": """
      - name: Rollback ECS service to previous task definition
        run: |
          PREV=$(aws ecs describe-services \
            --cluster ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --services ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --query 'services[0].deployments[-1].taskDefinition' --output text)
          aws ecs update-service \
            --cluster ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --service ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --task-definition $PREV
""",
    "lambda": """
      - name: Rollback Lambda alias to previous version
        run: |
          CURRENT=$(aws lambda get-alias \
            --function-name ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --name ${{ github.event.inputs.environment }} \
            --query FunctionVersion --output text)
          PREV=$((CURRENT - 1))
          aws lambda update-alias \
            --function-name ${{ env.PROJECT_NAME }}-${{ github.event.inputs.environment }} \
            --name ${{ github.event.inputs.environment }} \
            --function-version $PREV
""",
}


# ─────────────────────────────────────────────────────────────────────────────
# AI-enhanced step generation
# ─────────────────────────────────────────────────────────────────────────────

def _ai_generate_deploy_steps(
    compute:      str,
    project_name: str,
    region:       str,
    environments: list[str],
    services:     list[str],
    client:       aic.AIClient,
) -> Optional[dict]:
    """
    Ask the AI to generate compute-specific build, deploy, and rollback steps.
    Returns dict with keys: build_steps, deploy_steps, rollback_steps, secrets_needed.
    Falls back to static templates if AI is unavailable or fails.
    """
    tools = [
        {
            "name": "generate_pipeline_steps",
            "description": (
                "Generate GitHub Actions job steps for a specific AWS compute target. "
                "Steps must be valid GitHub Actions YAML (the 'steps:' list content, "
                "no job-level keys like 'runs-on'). Use ${{ matrix.environment }} for "
                "the environment name placeholder."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "build_steps": {
                        "type": "string",
                        "description": "YAML steps list for the build job (Docker build/push or Lambda package)",
                    },
                    "deploy_steps": {
                        "type": "string",
                        "description": "YAML steps list for the deploy job (uses ${{ matrix.environment }})",
                    },
                    "rollback_steps": {
                        "type": "string",
                        "description": "YAML steps list for the manual rollback job",
                    },
                    "secrets_needed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "GitHub secrets that must be configured in the repo",
                    },
                },
                "required": ["build_steps", "deploy_steps", "rollback_steps", "secrets_needed"],
            },
        }
    ]

    prompt = (
        f"Generate GitHub Actions steps for deploying a {compute} application.\n\n"
        f"Project: {project_name}\n"
        f"Region:  {region}\n"
        f"Environments: {', '.join(environments)}\n"
        f"Other services: {', '.join(s for s in services if s != compute)}\n\n"
        f"Requirements:\n"
        f"- Use OIDC via aws-actions/configure-aws-credentials@v4 with role-to-assume\n"
        f"- Use ${{{{ matrix.environment }}}} for environment placeholder in deploy steps\n"
        f"- Include health check / wait-for-stability after deployment\n"
        f"- Build steps should push to ECR and output the image tag\n"
        f"- Rollback steps target ${{{{ github.event.inputs.environment }}}}\n"
        f"- Steps must be valid YAML list entries (starting with '- name:')\n"
    )

    system = (
        "You are a GitHub Actions expert. Generate production-ready CI/CD steps. "
        "Output ONLY valid YAML steps list content — no job-level keys, no markdown fences. "
        "Use GitHub Actions best practices: OIDC authentication, matrix strategy for environments, "
        "artifact passing between jobs."
    )

    result = client.tool_use(tools, prompt, system, tool_name="generate_pipeline_steps")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline YAML builder
# ─────────────────────────────────────────────────────────────────────────────

def generate_pipeline(
    project_name:  str,
    region:        str,
    compute_list:  list[str],
    services:      list[str],
    environments:  dict,
    auto_deploy:   list[str],
    output_path:   Path,
    use_ai:        bool = True,
) -> None:
    """
    Build and write the full multi-stage GitHub Actions pipeline.
    Tries AI-enhanced steps first; falls back to static templates.
    """
    env_names    = list(environments.keys()) if environments else ["dev", "staging", "prod"]
    compute      = compute_list[0] if compute_list else "ecs-fargate"
    auto_envs    = set(auto_deploy or ["dev"])
    approval_envs = [e for e in env_names if e not in auto_envs]

    # Try AI-generated steps
    build_steps    = None
    deploy_steps   = None
    rollback_steps = None
    secrets_needed = ["AWS_DEPLOY_ROLE_ARN"]

    if use_ai:
        client = aic.get_client()
        if client.available:
            typer.secho(
                f"  ~ Generating pipeline steps for '{compute}' via AI ({client.provider})...",
                fg=typer.colors.BLUE,
            )
            ai_result = _ai_generate_deploy_steps(
                compute, project_name, region, env_names, services, client
            )
            if ai_result:
                build_steps    = ai_result.get("build_steps", "")
                deploy_steps   = ai_result.get("deploy_steps", "")
                rollback_steps = ai_result.get("rollback_steps", "")
                secrets_needed = ai_result.get("secrets_needed", secrets_needed)
                typer.secho("  + pipeline steps  [AI-generated]", fg=typer.colors.GREEN)

    # Fall back to static templates
    if not build_steps:
        build_steps    = _STATIC_BUILD_STEPS.get(compute, _STATIC_BUILD_STEPS["ecs-fargate"])
        deploy_steps   = _STATIC_DEPLOY_STEPS.get(compute, _STATIC_DEPLOY_STEPS["ecs-fargate"])
        rollback_steps = _ROLLBACK_STEPS.get(compute, "      - run: echo 'Rollback not configured'")
        typer.secho(f"  + pipeline steps  [static template: {compute}]", fg=typer.colors.CYAN)

    # ── Build the pipeline YAML ────────────────────────────────────────────
    # Use a deterministic, readable YAML string (not yaml.dump — too verbose)
    has_docker = compute in ("eks", "ecs-fargate")

    # Determine branch triggers
    main_branch = "main"

    # environments section for approval jobs
    approval_env_config = "\n".join(
        f"      name: {e}\n      url: https://{e}.{project_name}.example.com"
        for e in approval_envs
    )

    pipeline = f"""\
# ──────────────────────────────────────────────────────────────────────────────
# {project_name} CI/CD Pipeline
# Generated by devops-scaffold-tool
# ──────────────────────────────────────────────────────────────────────────────
#
# GitHub Environments to create (Settings > Environments):
{chr(10).join(f'#   - {e}  (requires approval: {e not in auto_envs})' for e in env_names)}
#
# GitHub Secrets to configure:
{chr(10).join(f'#   - {s}' for s in secrets_needed)}
# ──────────────────────────────────────────────────────────────────────────────

name: {project_name} CI/CD

on:
  push:
    branches: [{main_branch}, develop, "release/**"]
  pull_request:
    branches: [{main_branch}]
  workflow_dispatch:
    inputs:
      environment:
        description: Target environment
        required: true
        type: choice
        options: [{', '.join(env_names)}]
      action:
        description: Action to perform
        required: true
        type: choice
        options: [deploy, rollback]
        default: deploy

concurrency:
  group: ${{{{ github.workflow }}}}-${{{{ github.ref }}}}
  cancel-in-progress: true

env:
  AWS_REGION: {region}
  PROJECT_NAME: {project_name}
  TF_VERSION: "1.9.0"

# ── JOBS ──────────────────────────────────────────────────────────────────────

jobs:

  # ── 1. Lint & static analysis ─────────────────────────────────────────────
  lint:
    name: Lint & Security Scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TF_VERSION }}}}

      - name: Terraform fmt check
        run: terraform -chdir=.infra fmt -check -recursive

      - name: Terraform validate
        run: |
          terraform -chdir=.infra init -backend=false
          terraform -chdir=.infra validate

      - name: TFLint
        uses: terraform-linters/setup-tflint@v4
      - run: tflint --chdir=.infra

      - name: Checkov (IaC security scan)
        uses: bridgecrewio/checkov-action@v12
        with:
          directory: .infra
          framework: terraform
          soft_fail: true
          output_format: cli
          download_external_modules: false

  # ── 2. Build ──────────────────────────────────────────────────────────────
  build:
    name: Build & Package
    runs-on: ubuntu-latest
    needs: lint
    if: github.event.inputs.action != 'rollback'
    outputs:
      image-tag: ${{{{ steps.image.outputs.tag || '' }}}}
    steps:
      - uses: actions/checkout@v4
{build_steps}

  # ── 3. Tests ──────────────────────────────────────────────────────────────
  test:
    name: Tests
    runs-on: ubuntu-latest
    needs: build
    steps:
      - uses: actions/checkout@v4

      - name: Unit tests
        run: |
          # Replace with your test command
          echo "Running unit tests..."

      - name: Integration tests
        run: |
          echo "Running integration tests..."

  # ── 4. Terraform Plan (all environments, parallel) ────────────────────────
  tf-plan:
    name: Terraform Plan (${{{{ matrix.environment }}}})
    runs-on: ubuntu-latest
    needs: [build, test]
    strategy:
      matrix:
        environment: [{', '.join(env_names)}]
      fail-fast: false
    steps:
      - uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TF_VERSION }}}}

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{{{ secrets.AWS_DEPLOY_ROLE_ARN }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}

      - name: Terraform init
        run: terraform -chdir=.infra init -backend-config=env/${{{{ matrix.environment }}}}/backend.tf

      - name: Terraform plan
        run: |
          terraform -chdir=.infra plan \\
            -var-file=env/${{{{ matrix.environment }}}}/terraform.tfvars \\
            -out=${{{{ matrix.environment }}}}.tfplan

      - name: Upload plan
        uses: actions/upload-artifact@v4
        with:
          name: tfplan-${{{{ matrix.environment }}}}
          path: .infra/${{{{ matrix.environment }}}}.tfplan
          retention-days: 1
"""

    # ── Auto-deploy environments ──────────────────────────────────────────
    for env in env_names:
        is_auto      = env in auto_envs
        prev_env     = env_names[env_names.index(env) - 1] if env_names.index(env) > 0 else None
        needs_jobs   = ["tf-plan"] if not prev_env else [f"deploy-{prev_env}"]
        approval_str = "" if is_auto else f"""
    environment:
      name: {env}
      url: https://{env}.{project_name}.example.com"""

        pipeline += f"""
  # ── {5 + env_names.index(env)}. Deploy {env}{' (auto)' if is_auto else ' (manual approval)'} ─────────────────────────────────────
  deploy-{env}:
    name: Deploy {env.upper()}
    runs-on: ubuntu-latest
    needs: {needs_jobs}
    if: github.event.inputs.action != 'rollback'{approval_str}
    env:
      ENVIRONMENT: {env}
    strategy:
      matrix:
        environment: [{env}]
    steps:
      - uses: actions/checkout@v4

      - name: Download Terraform plan
        uses: actions/download-artifact@v4
        with:
          name: tfplan-{env}
          path: .infra/

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TF_VERSION }}}}

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{{{ secrets.AWS_DEPLOY_ROLE_ARN }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}

      - name: Terraform apply
        run: |
          terraform -chdir=.infra init -backend-config=env/{env}/backend.tf
          terraform -chdir=.infra apply -auto-approve {env}.tfplan

      - name: Deploy application
        env:
          matrix: '{{"environment": "{env}"}}'
{deploy_steps}
"""

    # ── Rollback job ──────────────────────────────────────────────────────
    pipeline += f"""
  # ── Rollback (manual trigger only) ────────────────────────────────────────
  rollback:
    name: Rollback ${{{{ github.event.inputs.environment }}}}
    runs-on: ubuntu-latest
    if: github.event.inputs.action == 'rollback'
    environment:
      name: ${{{{ github.event.inputs.environment }}}}
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{{{ secrets.AWS_DEPLOY_ROLE_ARN }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TF_VERSION }}}}

      - name: Terraform rollback (destroy + re-apply previous state)
        run: |
          echo "Initiating rollback for ${{{{ github.event.inputs.environment }}}}..."
          # Terraform state-based rollback: restore from S3 backup if available
          terraform -chdir=.infra init -backend-config=env/${{{{ github.event.inputs.environment }}}}/backend.tf

{rollback_steps}

      - name: Notify rollback complete
        run: |
          echo "Rollback of ${{{{ env.PROJECT_NAME }}}} in ${{{{ github.event.inputs.environment }}}} complete."
"""

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(pipeline, encoding="utf-8")
    typer.secho(f"  + cicd/pipeline.yml  [multi-stage: {' > '.join(env_names)}]", fg=typer.colors.GREEN)

    # Write setup README
    _write_pipeline_readme(output_path.parent, project_name, env_names, secrets_needed)


def _write_pipeline_readme(
    cicd_dir:      Path,
    project_name:  str,
    env_names:     list[str],
    secrets_needed: list[str],
) -> None:
    readme = f"""# CI/CD Pipeline — {project_name}

Generated by devops-scaffold-tool.

## Pipeline Stages

| Stage | Trigger | Approval |
|-------|---------|----------|
| Lint & Security Scan | Every push/PR | None |
| Build & Package | Every push | None |
| Tests | Every push | None |
| Terraform Plan | Every push | None |
{chr(10).join(f'| Deploy {e.upper()} | Push to main | {"None (auto)" if i == 0 else "GitHub Environment approval"} |' for i, e in enumerate(env_names))}
| Rollback | Manual (`workflow_dispatch`) | GitHub Environment approval |

## GitHub Setup Required

### 1. Create Environments (Settings > Environments)

{chr(10).join(f'- **{e}**: {"No restrictions (auto-deploy)" if i == 0 else "Require reviewers before deploy"}' for i, e in enumerate(env_names))}

### 2. Configure Secrets (Settings > Secrets and Variables > Actions)

| Secret | Description |
|--------|-------------|
{chr(10).join(f'| `{s}` | Set this in GitHub Actions secrets |' for s in secrets_needed)}

### 3. Configure OIDC (one-time)

Add the GitHub OIDC provider to AWS and create the deploy IAM role.
See: https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services
"""
    (cicd_dir / "README.md").write_text(readme, encoding="utf-8")
    typer.secho("  + cicd/README.md  [pipeline setup guide]", fg=typer.colors.GREEN)
