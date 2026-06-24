# DevOps Standardization Tool
**AWS Infrastructure Scaffold Generator — v2**

A conversational CLI that generates production-ready Terraform IaC and GitHub Actions pipelines from a minimal `infra.yaml` descriptor. Every service is a reusable Terraform module. Adding a new resource (queue, secret, Lambda function) requires editing only `terraform.tfvars` — no Terraform code changes needed.

---

## What's New in v2

| Area | v1 | v2 |
|---|---|---|
| **Service layout** | Flat `.tf` files at root (`sqs.tf`, `sns.tf`) | Every service → `modules/<svc>/{main.tf, variables.tf, outputs.tf}` |
| **Module variables** | Flat scalars (`sqs_visibility_timeout = 60`) | `map(object)` typed variables (`sqs_queues = { main = {...} }`) |
| **Adding resources** | Edit module HCL + variables + tfvars | **Edit tfvars only** — no code change |
| **`for_each`** | Single hardcoded resource per module | `for_each = var.sqs_queues` handles any number dynamically |
| **`locals.tf`** | Not generated | Generated — resolves `lambda_key → ARN`, builds log groups, alarm targets |
| **CLI naming preview** | Not shown | Naming pattern + 10 resource examples printed after project name entry |
| **Owner/env validation** | None | Validates format on input (lowercase, hyphens, max length) |
| **`infra.yaml.example`** | Not generated | Generated on every `init` run with full field reference |
| **Scaffold guide** | Not included | `scaffold_guide.docx` — 10-section guide, infra.yaml → Terraform walkthrough |

---

## Quick Start

### 1. Install dependencies

```bash
cd devops-scaffold-workspace/scaffold-cli
pip install -r requirements.txt
```

### 2. Set your AI provider key (optional)

AI is only used when a service has no catalog template (unknown service) or when using `--describe`. All standard services use predefined Jinja2 templates.

```bash
# Claude (default)
set ANTHROPIC_API_KEY=sk-ant-...

# Or OpenAI
set AI_PROVIDER=openai
set OPENAI_API_KEY=sk-...

# Or Gemini
set AI_PROVIDER=gemini
set GOOGLE_API_KEY=AIza...
```

### 3. Run the tool

```bash
# Option A: Interactive — answer prompts, naming preview shown after project name
python scaffold-cli/main.py init

# Option B: Full infra.yaml already filled — skips all prompts
python scaffold-cli/main.py init --yes

# Option C: Describe your architecture in plain English (AI extracts config)
python scaffold-cli/main.py init --describe "Python Lambda on EKS with SQS, KMS, Secrets Manager"

# Option D: Dry run — preview files without writing
python scaffold-cli/main.py init --dry-run
```

### 4. Review output

```
.infra/
├── provider.tf              # AWS provider + Terraform version + common_tags local
├── main.tf                  # Root module calls — one block per service
├── networking.tf            # VPC, subnets, security groups
├── iam.tf                   # IAM roles and policies
├── locals.tf                # Cross-module ARN resolution, lambda_configs defaults
├── observability.tf         # CloudWatch log groups + alarms (when cloudwatch not in services)
├── output.tf                # Terraform outputs (URLs, ARNs, cluster names)
├── variables.tf             # Variable declarations — scalars + map(object) typed
├── modules/
│   ├── lambda/              # Lambda function (for_each over lambda_configs)
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── eks/                 # EKS cluster + node group
│   ├── sqs/                 # SQS queues + DLQs (for_each over sqs_queues)
│   ├── sns/                 # SNS topic + subscriptions
│   ├── kms/                 # KMS key + alias
│   ├── ecr/                 # ECR repositories (for_each over ecr_repositories)
│   ├── eventbridge/         # EventBridge Scheduler + ECR push rule
│   ├── secrets_manager/     # Secrets Manager secrets (for_each over secrets)
│   └── cloudwatch/          # Log groups, alarms, dashboard (for_each)
├── env/
│   ├── uat/
│   │   ├── backend.tf               # S3 remote state for uat
│   │   ├── terraform.tfvars         # All values: scalars + map objects
│   │   └── terraform.tfvars.example
│   └── prod/  (same structure)
├── cicd/
│   ├── pipeline.yml         # GitHub Actions multi-stage pipeline
│   └── README.md
├── secrets/
│   └── secrets-policy.yml
└── decisions.md             # Architecture Decision Record
```

---

## How It Works

```
infra.yaml  ──────────────────────────────────────────────────────────────┐
                                                                          │
  --describe flag ──> AI extracts config from free-text description       │
                                                                          │
  Interactive prompts (only for missing fields)                           │
    - Project name → shows naming convention preview                      │
    - Owner (validated: lowercase + hyphens, max 30 chars)                │
    - Environments (validated: lowercase + hyphens, max 10 chars)         │
                                                                          │
  Catalog lookup ──> Jinja2 template per known service                    │
                └──> AI fallback for unknown services [ai-generated]      │
                                                                          │
  Module generation:                                                       │
    Each service → modules/<svc>/main.tf       (for_each resources)       │
                   modules/<svc>/variables.tf  (map(object) typed)        │
                   modules/<svc>/outputs.tf    (map-keyed outputs)        │
                                                                          │
  locals.tf ──> resolves lambda_key → ARN, builds log groups              │
                                                                          │
  env/{env}/terraform.tfvars ──> structured map objects per service       │
                                                                          │
  .infra/ scaffold + cicd/pipeline.yml + decisions.md + infra.yaml.example│
```

### Variable flow

```
infra.yaml environments  →  env/{env}/terraform.tfvars  →  root variables.tf
   (node_count, timeout)         (map objects + scalars)      (no defaults)
                                         │
                                   locals.tf
                               (resolves ARNs)
                                         │
                              root main.tf module calls
                            (pass whole maps to modules)
                                         │
                              modules/<svc>/variables.tf
                             (map(object) typed inputs)
                                         │
                              modules/<svc>/main.tf
                             (for_each = var.sqs_queues)
```

### Adding a new SQS queue (example)

Edit `env/uat/terraform.tfvars` only — no Terraform code change:

```hcl
sqs_queues = {
  main_queue = { ... }          # existing
  new_queue  = {                # add this block
    name                       = "my-project-uat-new"
    message_retention_seconds  = 345600
    max_message_size           = 1048576
    visibility_timeout_seconds = 60
    dlq_key                    = "new_dlq"
  }
}

dlq_queues = {
  main_dlq = { ... }
  new_dlq  = {
    name                      = "my-project-uat-new-dlq"
    message_retention_seconds = 1209600
  }
}
```

Same pattern applies to Lambda functions (`lambda_configs`), ECR repos (`ecr_repositories`), Secrets (`secrets`), EventBridge schedules (`eventbridge_schedules`), and CloudWatch alarms.

---

## Naming Convention

Pattern: `{project_name}-{environment}-{resource-suffix}`

| Resource | Example |
|---|---|
| Lambda function | `my-api-uat-func` |
| Lambda IAM role | `my-api-uat-lambda-role` |
| EKS cluster | `my-api-uat-cluster` |
| SQS queue | `my-api-uat-queue` |
| SQS DLQ | `my-api-uat-dlq` |
| ECR repository | `my-api-uat-app` |
| KMS alias | `alias/my-api-uat` |
| Secrets Manager | `my-api/uat/app` (slash-separated) |
| CloudWatch log group | `/aws/lambda/my-api-uat-func` |
| SNS topic | `my-api-uat-notifications` |

The CLI prints a live preview of these names immediately after the project name is entered.

---

## Supported Services

| Category | Services |
|---|---|
| Compute | `lambda`, `eks`, `ecs-fargate`, `ec2` |
| Messaging | `sqs`, `sns`, `eventbridge` |
| Storage | `s3`, `dynamodb`, `rds`, `aurora`, `redis` |
| Security | `kms`, `secrets-manager`, `cognito` |
| DevOps | `ecr`, `api-gateway` |
| Observability | `cloudwatch` |

Run `python scaffold-cli/main.py services` to list all services and their catalog status.

---

## Reusing Modules Across Projects

Every module is self-contained. To reuse `modules/sqs` in another project, change the source in that project's `main.tf`:

```hcl
# From local path
module "sqs" {
  source = "./modules/sqs"
  ...
}

# To Git URL (shared across projects)
module "sqs" {
  source = "git::https://github.com/your-org/infra-modules.git//sqs?ref=v1.0"
  ...
}
```

---

## Commands

| Command | Description |
|---|---|
| `python scaffold-cli/main.py init` | Generate scaffold (interactive) |
| `python scaffold-cli/main.py init --yes` | Skip prompts, use infra.yaml as-is |
| `python scaffold-cli/main.py init --dry-run` | Preview files without writing |
| `python scaffold-cli/main.py init --describe "..."` | AI extracts config from text |
| `python scaffold-cli/main.py services` | List all supported services |
| `python scaffold-cli/main.py providers` | Show AI provider status |

---

## Applying the Terraform

```bash
cd .infra

# Initialize for an environment
terraform init -backend-config=env/uat/backend.tf

# Plan
terraform plan -var-file=env/uat/terraform.tfvars

# Apply
terraform apply -var-file=env/uat/terraform.tfvars
```

---

## Repository Layout

```
devops-scaffold-workspace/
├── scaffold-cli/
│   ├── main.py                # CLI entry point, infra.yaml.example writer
│   ├── generator.py           # Scaffold writer — modules, variables, locals, tfvars
│   ├── dynamic_generator.py   # AI-powered generator for unknown services
│   ├── pipeline_generator.py  # GitHub Actions pipeline builder
│   ├── interactive_prompts.py # CLI prompts with naming preview + validators
│   ├── config_extractor.py    # --describe AI extraction
│   ├── ai_client.py           # Claude / OpenAI / Gemini abstraction
│   ├── decisions.py           # decisions.md writer
│   ├── services_catalog.yaml  # Source of truth for 50+ AWS services
│   └── requirements.txt
├── parent-repo/
│   └── templates/             # Jinja2 Terraform templates (iac/, cicd/, iam/)
├── terraform_templates/       # Reference implementation (bakker production project)
│   ├── modules/               # Reusable modules: lambda, sqs, sns, iam, ecr, etc.
│   └── env/                   # uat + prod tfvars with full map(object) configs
├── scaffold_guide.docx        # 10-section guide: infra.yaml → Terraform walkthrough
└── testing-ground/            # Live test project (eks-cicd-platform infra.yaml)
    └── .infra/                # Generated output — inspect to verify scaffold
```

---

## infra.yaml Quick Reference

Minimal:

```yaml
project:
  name: my-api
  region: us-east-1
  owner: platform-team

services:
  - lambda
  - sqs
  - kms

environments:
  dev: {}
  prod: {}
```

Full example with all options: run `python scaffold-cli/main.py init --yes` and inspect `infra.yaml.example`.

---

## Known Terraform Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| Duplicate `aws_cloudwatch_log_group` | Both `observability.tf` and `cloudwatch.tf` define it | Guard in `observability.tf.j2` with `{% if 'cloudwatch' not in services %}` |
| `dev =` invalid expression | `log_retention_days` was a scalar, template expected dict | Coerce scalar to `{dev, staging, prod}` dict in generator |
| `Single quotes are not valid` in `cloudwatch.tf` | Python dict rendered directly into HCL | Use `lookup(local.log_retention_days, var.environment, 30)` |

Rule: never render a Python `dict` or `list` directly into HCL via `{{ variable }}`. Always emit a scalar or use a Terraform `lookup()`/`tomap()` expression.
