# DevOps Standardization Tool — Application Guide

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Setup](#setup)
3. [Running the Tool](#running-the-tool)
4. [AI Model Switching](#ai-model-switching)
5. [infra.yaml Schema](#infrayaml-schema)
6. [Command Reference](#command-reference)
7. [Output Structure](#output-structure)
8. [Applying Terraform](#applying-terraform)
9. [decisions.md Explained](#decisionsmd-explained)
10. [Interactive Prompts Walkthrough](#interactive-prompts-walkthrough)
11. [GitHub Actions Pipeline](#github-actions-pipeline)
12. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Python 3.10+
- Terraform 1.5+ (only needed to apply the generated code)
- An API key for at least one AI provider (Claude recommended)

```bash
pip install -r scaffold-cli/requirements.txt
```

---

## Setup

### Step 1 — Choose your AI provider and set the key

| Provider | Env var | Default model |
|---|---|---|
| Claude (default) | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 |
| OpenAI | `OPENAI_API_KEY` | gpt-4o |
| Gemini | `GOOGLE_API_KEY` | gemini-1.5-pro |

**Windows (PowerShell):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

**Windows (Command Prompt):**
```cmd
set ANTHROPIC_API_KEY=sk-ant-...
```

**Linux / macOS:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Step 2 — Decide where to run

Run `main.py` from the directory where you want `.infra/` to be created. The tool looks for `infra.yaml` in the current directory.

```bash
# From your project root
cd my-project
python path/to/scaffold-cli/main.py init
```

Or from the testing-ground directory for experiments:
```bash
cd devops-scaffold-workspace/testing-ground
python ../scaffold-cli/main.py init
```

---

## Running the Tool

### Path 1: Fully interactive (no infra.yaml needed)

```bash
python scaffold-cli/main.py init
```

The tool asks 8-10 questions — cloud provider, project type, compute, data stores, environments, etc. Each question includes a trade-off explanation so you can make an informed choice. All answers are written to `decisions.md`.

### Path 2: infra.yaml + interactive fill-in

Create a minimal `infra.yaml` and the tool fills in anything missing:

```yaml
project:
  name: payments-api
  region: eu-west-1
  owner: backend-team

services:
  - ecs-fargate
  - alb
  - postgres
  - redis

environments:
  dev: {}
  staging: {}
  prod: {}
```

Then run:
```bash
python scaffold-cli/main.py init
```

The tool only prompts for fields not already in the file.

### Path 3: Describe in plain English

```bash
python scaffold-cli/main.py init --describe "Node.js microservice on ECS Fargate behind an ALB, PostgreSQL database, Redis for session caching, Cognito for auth, deployed to dev and prod. Small team of 3, growth stage."
```

The AI extracts all fields it can confidently determine, then the interactive prompts fill in anything remaining.

### Skip all prompts

```bash
python scaffold-cli/main.py init --yes
```

Uses whatever is in `infra.yaml`. Exits with an error if required fields are missing.

### Dry run

```bash
python scaffold-cli/main.py init --dry-run
```

Shows every file that would be created without writing anything.

---

## AI Model Switching

The tool uses AI for two things:
1. Extracting config from `--describe` text
2. Generating Terraform for services not in the static catalog

### Switch provider via environment variable

```bash
# Claude (default, recommended)
set ANTHROPIC_API_KEY=sk-ant-...
python scaffold-cli/main.py init

# OpenAI
set AI_PROVIDER=openai
set OPENAI_API_KEY=sk-...
python scaffold-cli/main.py init

# Google Gemini
set AI_PROVIDER=gemini
set GOOGLE_API_KEY=AIza...
python scaffold-cli/main.py init
```

### Switch provider via CLI flag (one-off override)

```bash
python scaffold-cli/main.py init --ai-provider openai
python scaffold-cli/main.py init --ai-provider gemini
python scaffold-cli/main.py init --ai-provider claude
```

### Override the model version

```bash
# Override model via env var
set AI_MODEL=gpt-4o-mini
python scaffold-cli/main.py init

# Override model via CLI flag
python scaffold-cli/main.py init --ai-provider openai --ai-model gpt-4o-mini

# Use a specific Claude model
python scaffold-cli/main.py init --ai-model claude-haiku-4-5-20251001
```

### Check provider status

```bash
python scaffold-cli/main.py providers
```

Output example:
```
AI provider status:

  claude     claude-sonnet-4-6              [ready]  (active)
  openai     gpt-4o                         [OPENAI_API_KEY not set]
  gemini     gemini-1.5-pro                 [GOOGLE_API_KEY not set]

  Set AI_PROVIDER=claude|openai|gemini  and the matching API key env var.
  Set AI_MODEL to override the default model.
  Or pass --ai-provider / --ai-model flags to the init command.
```

### Provider precedence

CLI flag `--ai-provider` > `AI_PROVIDER` env var > default (`claude`)

CLI flag `--ai-model` > `AI_MODEL` env var > provider default model

---

## infra.yaml Schema

### Full example

```yaml
project:
  name: my-app               # Required. Lowercase, hyphens only, max 20 chars.
  region: us-east-1          # Required. AWS region.
  owner: platform-team       # Required. Team or owner name.
  type: backend              # Optional. backend | frontend | data-pipeline | ai-service

stage: growth                # Optional. prototype | early | growth | scale

team:
  size: small                # Optional. solo | small | medium | large
  ops_maturity: medium       # Optional. low | medium | high

runtime:
  language: python           # Optional. python | node | go | java | ruby
  containerised: true        # Optional. true = Docker/ECS/EKS, false = Lambda

services:                    # Required. At least one compute service.
  # Compute (at least one required)
  - lambda
  - ecs-fargate
  - eks
  - ec2
  # Ingress
  - alb
  - api-gateway
  # Data
  - postgres
  - mysql
  - aurora-postgres
  - aurora-mysql
  - redis
  - dynamodb
  - s3
  - opensearch
  # Auth
  - cognito
  - kms
  # Messaging
  - sqs
  - eventbridge
  # Frontend
  - static-site
  # AI/ML (IAM permissions only)
  - bedrock

environments:                # Optional. Defaults to dev/staging/prod.
  dev: {}
  staging: {}
  prod: {}

cicd:
  auto_deploy:               # Environments deployed automatically (no approval gate)
    - dev
    # staging and prod require manual approval by default

auth:
  required: true             # Shorthand to add cognito to services

data:
  stores:                    # Shorthand to add data services
    - postgres
    - redis

flows:                       # Optional. Documents service interaction patterns.
  user_flow:
    description: "API request -> Lambda -> DynamoDB"
    services:
      - api-gateway
      - lambda
      - dynamodb

connections:                 # Optional. Explicit service-to-service wiring.
  - from: api-gateway
    to: lambda
  - from: lambda
    to: dynamodb
```

### Valid services

```
Compute:    lambda, ecs-fargate, eks, ec2, static-site
Ingress:    alb, api-gateway
Databases:  postgres, mysql, aurora-postgres, aurora-mysql, redis, dynamodb, opensearch
Storage:    s3
Auth:       cognito, kms
Messaging:  sqs, eventbridge
AI/ML:      bedrock
```

Services not in this list are sent to the AI provider, which generates the Terraform file and required variables.

---

## Command Reference

### `init` — generate scaffold

```
python scaffold-cli/main.py init [OPTIONS]

Options:
  --dry-run           Preview file list without writing anything
  --describe TEXT     Free-text description. AI extracts config from it.
  --yes / -y          Skip interactive prompts (use infra.yaml values only)
  --ai-provider TEXT  Override AI_PROVIDER: claude | openai | gemini
  --ai-model TEXT     Override AI_MODEL for the selected provider
  --help              Show help
```

### `services` — list catalog

```
python scaffold-cli/main.py services
```

Lists every service in `services_catalog.yaml`, grouped by category. Shows whether each service uses a static template or is AI-generated.

### `providers` — AI provider status

```
python scaffold-cli/main.py providers
```

Shows all three providers with their default model, API key status, and which one is currently active.

---

## Output Structure

```
.infra/
├── provider.tf          AWS provider, Terraform version constraint, local tags
├── main.tf              All compute resources for your services
├── networking.tf        VPC, public/private subnets, NAT gateway, security groups
├── iam.tf               IAM roles and least-privilege policies
├── data.tf              Databases, caches, queues, S3 buckets
├── observability.tf     CloudWatch log groups and alarms
├── output.tf            Exported values (load balancer DNS, function ARN, etc.)
├── variables.tf         Variable declarations only -- no hardcoded defaults
├── env/
│   ├── dev/
│   │   ├── backend.tf              S3 + DynamoDB remote state for dev
│   │   ├── terraform.tfvars        Actual variable values for dev
│   │   └── terraform.tfvars.example  Copy of tfvars with placeholders
│   ├── staging/
│   │   └── ...
│   └── prod/
│       └── ...
├── cicd/
│   ├── pipeline.yml     Multi-stage GitHub Actions workflow
│   └── README.md        Pipeline setup instructions
├── secrets/
│   └── secrets-policy.yml  Secrets Manager / SSM path definitions
└── decisions.md         Architecture Decision Record
```

### Why variables.tf has no defaults

Variable values differ between environments — a dev RDS instance uses `db.t3.micro` while prod uses `db.r6g.xlarge`. Hardcoding either value as a default would be wrong for the other environments.

All actual values live in `env/{env}/terraform.tfvars`. The `variables.tf` file only declares that the variable exists.

---

## Applying Terraform

After the scaffold is generated:

```bash
cd .infra

# 1. Create the S3 bucket and DynamoDB table for remote state first
#    (the env/dev/backend.tf file tells you the bucket name)

# 2. Initialize for the target environment
terraform init -backend-config=env/dev/backend.tf

# 3. Plan
terraform plan -var-file=env/dev/terraform.tfvars

# 4. Apply
terraform apply -var-file=env/dev/terraform.tfvars

# For prod:
terraform init -reconfigure -backend-config=env/prod/backend.tf
terraform plan -var-file=env/prod/terraform.tfvars
terraform apply -var-file=env/prod/terraform.tfvars
```

---

## decisions.md Explained

Every run appends to `.infra/decisions.md`. The file is an Architecture Decision Record (ADR) that captures what was chosen and why.

Example entry:

```markdown
## Run — 2026-06-21 14:32:07 | project: payments-api

### Decisions

| Field | Value | Source | Reason |
|---|---|---|---|
| cloud.provider | aws | interactive | Only AWS is supported in v1. |
| project.type | backend | interactive | REST/GraphQL/BFF service. |
| compute | ecs-fargate | infra.yaml | Read from project config file. |
| stage | growth | interactive | Scaling product with paying users. |

### Warnings

> [!WARNING]
> EKS detected on a small team. EKS has significant operational overhead...
```

This file is the answer to "why was this architecture chosen?" for every future team member or audit.

---

## Interactive Prompts Walkthrough

When you run `python scaffold-cli/main.py init` without a complete `infra.yaml`, the tool asks:

1. **Cloud provider** — AWS only (v1). Exits if you choose otherwise.
2. **Project type** — backend / frontend / data-pipeline / ai-service. Explains each.
3. **Project name** — validates lowercase + hyphens, max 20 chars.
4. **Stage** — prototype / early / growth / scale. Controls complexity level.
5. **Runtime language** — python / node / go / java / ruby.
6. **Team size and ops maturity** — used to catch anti-patterns.
7. **AWS region** — validates against known region list.
8. **Owner** — team or individual name for tagging.
9. **Environments** — multiselect: dev / staging / prod.
10. **Data stores** — multiselect: postgres / mysql / redis / dynamodb / s3 / etc.
11. **Auth required** — adds Cognito if yes.
12. **CI/CD auto-deploy** — which environments deploy automatically vs. require approval.
13. **AWS account structure** — single account or separate per env (affects backend.tf).

All answers are logged to `decisions.md` with their source (`interactive`, `infra.yaml`, `--describe`, or `default`).

### Anti-pattern warnings

The tool warns (but does not block) on:

- EKS with a solo or small team ("significant operational overhead")
- Backend with data stores but no auth ("consider adding Cognito or another auth layer")
- EKS with low ops maturity ("EKS requires Kubernetes expertise")

Warnings appear in yellow and are recorded in `decisions.md`.

---

## GitHub Actions Pipeline

The generated `cicd/pipeline.yml` includes these jobs:

| Job | Trigger | Notes |
|---|---|---|
| lint | Every push | Python/JS/HCL linting |
| build | Every push | Docker build or zip package |
| test | Every push | Unit tests |
| tf-plan | Every push | `terraform plan` preview |
| deploy-dev | Push to main | Auto-deploy, no approval |
| deploy-staging | After dev | GitHub Environment gate if not in auto_deploy |
| deploy-prod | After staging | Always requires manual approval |
| rollback | workflow_dispatch | Rolls back any environment |

### Setup required in GitHub

1. Create GitHub Environments named `staging` and `prod` (Settings > Environments)
2. Add required reviewers to each environment
3. Set repository secrets:
   - `AWS_ACCOUNT_ID`
   - `AWS_REGION`
4. Enable OIDC: add the IAM role ARN as a secret (`AWS_OIDC_ROLE_ARN`) or use the role pattern in the workflow

See `cicd/README.md` in the generated output for the full setup checklist.

---

## Troubleshooting

### "AI provider not available — skipping --describe extraction"

The API key env var is not set. Run:
```bash
python scaffold-cli/main.py providers
```
This shows which keys are missing.

### "ERROR: no compute target found"

Your `services:` list must include at least one of: `lambda`, `ecs-fargate`, `eks`, `ec2`.

### "Services not in catalog" warning (yellow)

The service name is not in `services_catalog.yaml`. The tool will call the AI provider to generate the Terraform. This is intentional — it handles any AWS service.

### "project.name invalid"

Name must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$` and be max 20 characters.
Examples: `payments-api`, `event-processor`, `my-service`.

### Windows UnicodeEncodeError

If you see encoding errors in terminal output, set:
```powershell
$env:PYTHONIOENCODING = "utf-8"
```

### Overwrite prompt

If `.infra/` already exists, the tool asks before overwriting. Use `--yes` to skip the confirmation (still asks about overwrite separately).
