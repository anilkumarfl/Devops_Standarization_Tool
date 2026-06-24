import sys
import importlib.util
from pathlib import Path

# Load dynamic_generator by absolute path so it works regardless of CWD or sys.path.
_dg_path = Path(__file__).parent / "dynamic_generator.py"
if not _dg_path.exists():
    print(f"ERROR: dynamic_generator.py not found at {_dg_path}")
    sys.exit(1)
_spec = importlib.util.spec_from_file_location("dynamic_generator", _dg_path)
dg    = importlib.util.module_from_spec(_spec)
sys.modules["dynamic_generator"] = dg
_spec.loader.exec_module(dg)

import typer
import yaml
import re

import decisions     as dec
import ai_client     as aic
from config_extractor   import extract_config_from_description, merge_extracted_into_config
from interactive_prompts import fill_missing_fields, is_config_complete

INFRA_DIR = Path(".infra")

app = typer.Typer(
    help="DevOps Greenfield Scaffold Generator",
    add_completion=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: str = "infra.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    typer.echo(f"> Reading {path}...")
    return yaml.safe_load(p.read_text()) or {}


def _validate_name(name: str):
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name) or len(name) > 20:
        typer.secho(
            f"ERROR: project.name '{name}' is invalid.\n"
            f"  Must be lowercase letters and hyphens only, max 20 chars.\n"
            f"  Example: payments-api, event-processor, my-service",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)


def _detect_existing(base: Path) -> tuple[str, list[str]]:
    existing_compute = base / "main.tf"
    if not existing_compute.exists():
        existing_compute = base / "iac" / "compute.tf"
    if not existing_compute.exists():
        return "", []
    old_label = existing_compute.read_text().splitlines()[0].lstrip("# ").strip()
    old_data_tf = base / "data.tf"
    if not old_data_tf.exists():
        old_data_tf = base / "iac" / "data.tf"
    old_data = []
    if old_data_tf.exists():
        content = old_data_tf.read_text()
        signatures = [
            ("aws_db_instance",           "rds"),
            ("aws_dynamodb_table",        "dynamodb"),
            ("aws_s3_bucket",             "s3"),
            ("aws_elasticache",           "redis"),
            ("aws_sqs_queue",             "sqs"),
            ("aws_cloudwatch_event_bus",  "eventbridge"),
            ("aws_cognito_user_pool",     "cognito"),
            ("aws_opensearch_domain",     "opensearch"),
            ("aws_kinesis_stream",        "kinesis"),
            ("aws_msk_cluster",           "msk"),
        ]
        old_data = [label for sig, label in signatures if sig in content]
    return old_label, old_data


def _normalise_config(config: dict) -> dict:
    """
    Normalise config so generator.py always finds fields in the expected places.
    Handles both the old (services-list) schema and the new (nested) schema.
    """
    # project.region fallback
    if not config.get("project", {}).get("region"):
        region = config.get("cloud", {}).get("region", "")
        if region:
            config.setdefault("project", {})["region"] = region

    # project.owner fallback
    if not config.get("project", {}).get("owner"):
        owner = config.get("owner", "")
        if owner:
            config.setdefault("project", {})["owner"] = owner

    # data.stores → services list integration
    data_stores = config.get("data", {}).get("stores", [])
    if data_stores:
        svcs = config.setdefault("services", [])
        for ds in data_stores:
            if ds not in svcs:
                svcs.append(ds)

    # auth.required → cognito service only when method is not explicitly 'iam'
    auth = config.get("auth", {})
    if auth.get("required") and auth.get("method", "cognito") != "iam":
        svcs = config.setdefault("services", [])
        if "cognito" not in svcs:
            svcs.append("cognito")

    return config


# ─────────────────────────────────────────────────────────────────────────────
# CLI commands
# ─────────────────────────────────────────────────────────────────────────────

@app.command()
def init(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be generated without writing any files",
    ),
    describe: str = typer.Option(
        None, "--describe",
        help='Plain-English description of your architecture. AI extracts the config.',
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip interactive prompts and use defaults for any missing fields",
    ),
    ai_provider: str = typer.Option(
        None, "--ai-provider",
        help="Override AI_PROVIDER env var: claude | openai | gemini",
    ),
    ai_model: str = typer.Option(
        None, "--ai-model",
        help="Override AI_MODEL env var for the selected provider",
    ),
):
    """Generate Terraform infrastructure scaffold from infra.yaml."""
    import os
    if ai_provider:
        os.environ["AI_PROVIDER"] = ai_provider
    if ai_model:
        os.environ["AI_MODEL"] = ai_model

    # ── Load catalog ─────────────────────────────────────────────────────────
    catalog          = dg.load_catalog()
    valid_services   = dg.get_all_valid_services(catalog)
    compute_services = dg.get_compute_services(catalog)

    # ── Load infra.yaml (may be empty / not exist) ────────────────────────────
    config = _load_yaml()

    # ── --describe: AI extraction fills in what infra.yaml doesn't have ───────
    if describe:
        typer.secho(
            f"\n> AI provider: {aic.provider_info()}",
            fg=typer.colors.BLUE,
        )
        extracted = extract_config_from_description(describe)
        if extracted:
            config = merge_extracted_into_config(config, extracted)

    # ── Interactive prompts for any missing required fields ───────────────────
    decisions_path = INFRA_DIR / "decisions.md"
    if not yes and not is_config_complete(config):
        config = fill_missing_fields(config, decisions_path=decisions_path)
    elif yes and not is_config_complete(config):
        typer.secho(
            "  [!] --yes passed but config is incomplete. "
            "Some required fields may be missing.",
            fg=typer.colors.YELLOW,
        )

    # ── Normalise config ──────────────────────────────────────────────────────
    config = _normalise_config(config)

    # ── Extract fields ────────────────────────────────────────────────────────
    project      = config.get("project", {})
    name         = project.get("name",   "")
    region       = project.get("region", "")
    owner        = project.get("owner",  "")
    environments = config.get("environments", {})
    services     = config.get("services", [])

    errors = []
    if not name:   errors.append("project.name is required")
    if not region: errors.append("project.region (or cloud.region) is required")
    if not owner:  errors.append("project.owner is required")
    if errors:
        for e in errors:
            typer.secho(f"ERROR: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)

    _validate_name(name)

    # ── Services validation ───────────────────────────────────────────────────
    if not services:
        typer.secho(
            "ERROR: no services specified.\n"
            "  Add at least one compute service, or use --describe to let AI infer services.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    unknown = [s for s in services if s not in valid_services]
    if unknown:
        typer.secho(
            f"  [!] Services not in catalog: {unknown}\n"
            f"    These will be generated via AI ({aic.provider_info()}).",
            fg=typer.colors.YELLOW,
        )

    compute = [s for s in services if s in compute_services]
    if not compute:
        typer.secho(
            f"ERROR: no compute target found.\n"
            f"  Add at least one of: {', '.join(sorted(compute_services))}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    max_compute = dg.get_max_compute_targets(catalog)
    if len(compute) > max_compute:
        typer.secho(
            f"ERROR: too many compute targets: {compute}\n"
            f"  Maximum {max_compute} compute services allowed.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    if len(compute) == 2:
        valid_combos = dg.get_valid_compute_combinations(catalog)
        if sorted(compute) not in [sorted(c) for c in valid_combos]:
            typer.secho(
                f"ERROR: unsupported compute combination: {compute}\n"
                f"  Valid multi-compute combinations:\n"
                + "\n".join(f"    - {c}" for c in valid_combos),
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)

    other_services = [s for s in services if s not in compute_services]
    flows          = config.get("flows", {})

    # ── Print summary ─────────────────────────────────────────────────────────
    typer.echo("")
    typer.secho("> Scaffold summary:", fg=typer.colors.BLUE, bold=True)
    typer.echo(f"  project.name   = {name}")
    typer.echo(f"  project.region = {region}")
    typer.echo(f"  project.owner  = {owner}")
    typer.echo(f"  project.type   = {project.get('type', 'not set')}")
    typer.echo(f"  stage          = {config.get('stage', 'not set')}")
    typer.echo(f"  compute        = {' + '.join(compute)}")
    if other_services:
        known   = [s for s in other_services if s in valid_services]
        dynamic = [s for s in other_services if s not in valid_services]
        if known:
            typer.echo(f"  services       = {', '.join(known)}")
        if dynamic:
            typer.secho(
                f"  services (AI)  = {', '.join(dynamic)}  [will call {aic.provider_info()}]",
                fg=typer.colors.CYAN,
            )
    if environments:
        typer.echo(f"  environments   = {', '.join(environments.keys())}")
    typer.echo(f"  AI provider    = {aic.provider_info()}")

    # ── Dry run ───────────────────────────────────────────────────────────────
    if dry_run:
        typer.secho("\n=== DRY RUN — no files will be written ===", fg=typer.colors.MAGENTA, bold=True)
        typer.secho("\n  Files that would be generated:", fg=typer.colors.CYAN)
        env_names_preview = list((config.get("environments") or {}).keys()) or ["dev", "staging", "prod"]
        static_files = [
            ".infra/provider.tf",
            ".infra/networking.tf",
            ".infra/main.tf",
            ".infra/iam.tf",
            ".infra/data.tf",
            ".infra/observability.tf",
            ".infra/output.tf",
            ".infra/variables.tf",
            ".infra/cicd/pipeline.yml",
            ".infra/cicd/README.md",
            ".infra/secrets/secrets-policy.yml",
            ".infra/decisions.md",
        ]
        env_files = [
            f".infra/env/{e}/{f}"
            for e in env_names_preview
            for f in ["backend.tf", "terraform.tfvars", "terraform.tfvars.example"]
        ]
        for f in static_files + env_files:
            typer.echo(f"    {f}")
        if unknown:
            typer.secho(
                f"\n  Services requiring AI: {unknown}",
                fg=typer.colors.CYAN,
            )
        typer.secho("\nDRY RUN COMPLETE.", fg=typer.colors.MAGENTA)
        return

    # ── Overwrite protection ──────────────────────────────────────────────────
    old_label, old_data = _detect_existing(INFRA_DIR)
    if old_label:
        typer.secho("\n! EXISTING SCAFFOLD DETECTED", fg=typer.colors.YELLOW, bold=True)
        typer.secho(f"  Currently: {old_label}", fg=typer.colors.WHITE)
        typer.secho(f"  Old data:  {', '.join(old_data) or 'none'}", fg=typer.colors.WHITE)
        typer.secho(f"\n  Replacing with:", fg=typer.colors.CYAN)
        typer.secho(f"    compute  = {' + '.join(compute)}", fg=typer.colors.CYAN)
        typer.secho(f"    services = {', '.join(other_services) or 'none'}", fg=typer.colors.CYAN)
        typer.secho(f"\n  All .infra/*.tf and .infra/env/ files will be overwritten.", fg=typer.colors.YELLOW)
        if not yes and not typer.confirm("\n  Overwrite existing scaffold?", default=False):
            typer.secho("  Aborted — no files changed.", fg=typer.colors.GREEN)
            raise typer.Exit(0)

    # ── Write decisions.md run header ─────────────────────────────────────────
    INFRA_DIR.mkdir(parents=True, exist_ok=True)
    dec.log_run_header(name, path=decisions_path)
    if describe:
        dec.log_decision("input.describe", describe[:200] + ("..." if len(describe) > 200 else ""),
                         "cli flag", "--describe text passed by user", path=decisions_path)

    # Log values that came from infra.yaml (not prompted)
    if config.get("cloud", {}).get("provider"):
        dec.log_decision("cloud.provider", config["cloud"]["provider"],
                         "infra.yaml", "Read from project config file.", path=decisions_path)

    # ── Generate ──────────────────────────────────────────────────────────────
    typer.secho("\n> Generating scaffold...", fg=typer.colors.BLUE, bold=True)
    import generator
    generator.generate_scaffold(config, catalog)

    # ── Write infra.yaml.example with naming conventions ─────────────────────
    def _write_infra_example(path: Path, project_name: str) -> None:
        content = f"""\
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# infra.yaml — scaffold-cli configuration reference
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# NAMING CONVENTIONS
# ──────────────────
# All generated AWS resource names follow this pattern:
#
#   {{project.name}}-{{environment}}-{{resource-suffix}}
#
# Separator rules:
#   Hyphen   -   resource names       e.g. {project_name}-dev-func
#   Slash    /   path-based names     e.g. {project_name}/dev/app  (Secrets Manager, log groups)
#   Snake    _   Terraform variables  e.g. lambda_timeout, eks_node_count
#
# Resource name examples (using project.name = "{project_name}"):
#
#   Lambda function          {project_name}-dev-func
#   Lambda IAM role          {project_name}-dev-lambda-role
#   EKS cluster              {project_name}-dev-eks
#   EKS cluster IAM role     {project_name}-dev-eks-cluster-role
#   EKS node group           {project_name}-dev-ng
#   ECR repository           {project_name}-dev-app
#   SQS queue                {project_name}-dev-queue
#   SQS dead-letter queue    {project_name}-dev-dlq
#   SNS topic                {project_name}-dev-notifications
#   KMS key alias            alias/{project_name}-dev
#   Secrets Manager (app)    {project_name}/dev/app
#   Secrets Manager (eks)    {project_name}/dev/eks-auth
#   CloudWatch log group     /aws/lambda/{project_name}-dev-func
#   CloudWatch dashboard     {project_name}-dev
#   CloudWatch alarm         {project_name}-dev-lambda-errors
#
# FIELD RULES
# ───────────
#   project.name    Lowercase letters, digits, hyphens only. Max 20 chars.
#                   Pattern: ^[a-z0-9][a-z0-9-]*[a-z0-9]$
#                   Good:  payments-api, ai-assistant, eks-cicd-platform
#                   Bad:   PaymentsAPI, payments_api, my app
#
#   project.region  Standard AWS region format.
#                   Example: us-east-1, eu-west-1, ap-southeast-2
#
#   project.owner   Lowercase letters, digits, hyphens only. Max 30 chars.
#                   Applied as a tag on every generated resource.
#                   Example: platform-team, backend-squad, devops-team
#
#   services        Must match catalog entries exactly (see: scaffold-cli services).
#                   At least one compute service required: lambda, ecs-fargate, eks, ec2.
#
#   connections     Use exact service names from the services list.
#                   Format: {{ from: <service>, to: <service> }}
#
#   environments    Keys become the environment name in all resource names.
#                   Lowercase, hyphens, max 10 chars per name.
#                   Example: dev, uat, prod   (NOT Dev, PROD, production-env)
#
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

project:
  name: your-project-name       # ^[a-z0-9][a-z0-9-]{{1,18}}[a-z0-9]$  max 20 chars
  region: us-east-1             # AWS region — e.g. us-east-1, eu-west-2
  owner: your-team-name         # kebab-case, max 30 chars — used in resource tags
  type: backend                 # backend | frontend | full-stack | chatbot | data-pipeline | ai-service

stage: prototype                # prototype | early | growth | scale

team:
  size: small                   # solo | small | medium | large
  ops_maturity: low             # low | medium | high

runtime:
  language: python              # python | node | go | java | ruby
  containerised: false          # true -> ECS/EKS  |  false -> Lambda/serverless

# ── Services ─────────────────────────────────────────────────────────────────
# List every AWS service to provision. Must match catalog entries exactly.
# Run 'scaffold-cli services' to see all available options.
# At least one compute service is required: lambda, ecs-fargate, eks, ec2
services:
  - lambda                      # compute  — serverless function
  # - ecs-fargate               # compute  — managed containers
  # - eks                       # compute  — Kubernetes cluster
  # - api-gateway               # ingress  — REST/HTTP endpoint
  # - cognito                   # auth     — user pool + app client
  # - dynamodb                  # data     — key-value / document store
  # - s3                        # data     — object storage
  # - rds                       # data     — relational database (postgres/mysql)
  # - sqs                       # queue    — decoupled async messaging
  # - sns                       # notify   — fan-out pub/sub alerts
  # - eventbridge               # events   — event routing and scheduling
  # - kms                       # security — customer-managed encryption keys
  # - secrets-manager           # security — encrypted secret storage
  # - bedrock                   # ai       — foundation models + RAG
  # - cloudwatch                # observe  — logs, alarms, dashboards

# ── Auth ─────────────────────────────────────────────────────────────────────
auth:
  required: false               # true -> adds cognito.tf (unless method: iam)
  method: cognito               # cognito | iam

# ── Connections ──────────────────────────────────────────────────────────────
# Define which services talk to each other.
# Each connection wires: IAM policy on Lambda + env var injected into function.
# Use exact service names from the services list above.
connections:
  - {{ from: api-gateway, to: lambda }}     # adds Lambda invoke permission
  # - {{ from: lambda, to: dynamodb }}      # adds dynamodb:PutItem/GetItem policy + TABLE_NAME env var
  # - {{ from: lambda, to: s3 }}            # adds s3:PutObject/GetObject policy + BUCKET_NAME env var
  # - {{ from: lambda, to: sqs }}           # adds sqs:SendMessage policy + SQS_QUEUE_URL env var
  # - {{ from: sqs, to: lambda }}           # adds SQS event source mapping
  # - {{ from: lambda, to: sns }}           # adds sns:Publish policy + SNS_TOPIC_ARN env var
  # - {{ from: lambda, to: secrets-manager }} # adds secretsmanager:GetSecretValue + ARN env var
  # - {{ from: ecr, to: eventbridge }}      # ECR push rule on default event bus

# ── Environments ─────────────────────────────────────────────────────────────
# Keys must be: lowercase, hyphens only, max 10 chars (they appear in every resource name).
# Values override static defaults — infra.yaml values always win over generated defaults.
environments:
  dev:
    multi_az: false
    lambda:
      memory_mb: 256            # -> lambda_memory_size in env/dev/terraform.tfvars
      timeout_s: 30             # -> lambda_timeout in env/dev/terraform.tfvars
    # eks:
    #   node_count: 1           # -> eks_node_count
    #   instance_type: t3.medium  # -> eks_instance_type

  prod:
    multi_az: true
    lambda:
      memory_mb: 512
      timeout_s: 29             # API Gateway hard limit is 29s

# ── CI/CD ────────────────────────────────────────────────────────────────────
cicd:
  auto_deploy:
    - dev                       # terraform apply runs automatically on push
  manual_deploy:
    - prod                      # requires manual approval in pipeline
"""
        path.write_text(content, encoding="utf-8")

    # ── Full pipeline (replaces simple pipeline.yml from generator) ───────────
    import pipeline_generator as pg
    auto_deploy = config.get("cicd", {}).get("auto_deploy", ["dev"])
    pg.generate_pipeline(
        project_name  = name,
        region        = region,
        compute_list  = compute,
        services      = services,
        environments  = environments or {"dev": {}, "staging": {}, "prod": {}},
        auto_deploy   = auto_deploy,
        output_path   = INFRA_DIR / "cicd" / "pipeline.yml",
        use_ai        = True,
    )

    _write_infra_example(Path("infra.yaml.example"), name)

    typer.secho("\n> Done.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Scaffold : {INFRA_DIR.absolute()}")
    typer.echo(f"  Decisions: {decisions_path.absolute()}")
    typer.echo(f"  Example  : infra.yaml.example  (naming conventions + field reference)")


@app.command()
def services():
    """List all services available in the catalog."""
    catalog = dg.load_catalog()
    by_category: dict[str, list[str]] = {}
    for name, entry in catalog.get("services", {}).items():
        cat = entry.get("category", "other")
        by_category.setdefault(cat, []).append(name)

    typer.secho("\nAvailable services (from services_catalog.yaml):\n", fg=typer.colors.CYAN, bold=True)
    for cat in sorted(by_category):
        typer.secho(f"  {cat}:", fg=typer.colors.YELLOW)
        for svc in sorted(by_category[cat]):
            entry    = catalog["services"][svc]
            has_tmpl = entry.get("template") is not None
            label    = "static template" if has_tmpl else "AI-generated"
            typer.echo(f"    {svc:<25} [{label}]")
    typer.echo("")


@app.command()
def providers():
    """Show configured AI provider and model."""
    typer.secho("\nAI provider status:\n", fg=typer.colors.CYAN, bold=True)
    import os
    issues: list[str] = []
    for name, cfg in aic.PROVIDER_CONFIG.items():
        key_val = os.environ.get(cfg["key_env"], "")
        key_set = bool(key_val)
        model   = (os.environ.get("AI_MODEL") or cfg["default_model"]) if os.environ.get("AI_PROVIDER", "claude") == name else cfg["default_model"]
        active  = "(active)" if os.environ.get("AI_PROVIDER", "claude") == name else ""
        status  = "ready" if key_set else f"{cfg['key_env']} not set"
        color   = typer.colors.GREEN if key_set else typer.colors.YELLOW

        # Detect common key formatting mistakes
        if key_set:
            if key_val.startswith('"') or key_val.endswith('"'):
                status = "KEY HAS QUOTES -- remove the \" characters"
                color  = typer.colors.RED
                issues.append(f"  {cfg['key_env']} starts/ends with quotes. Run: set {cfg['key_env']}={key_val.strip(chr(34))}")
            elif key_val.startswith("'") or key_val.endswith("'"):
                status = "KEY HAS QUOTES -- remove the ' characters"
                color  = typer.colors.RED
                issues.append(f"  {cfg['key_env']} starts/ends with single quotes.")
            elif key_val.startswith(" ") or key_val.endswith(" "):
                status = "KEY HAS SPACES -- remove leading/trailing spaces"
                color  = typer.colors.RED
                issues.append(f"  {cfg['key_env']} has leading/trailing spaces. Run: set {cfg['key_env']}={key_val.strip()}")

        typer.secho(
            f"  {name:<10} {model:<30} [{status}]  {active}",
            fg=color,
        )

    if issues:
        typer.secho("\n  ! Key formatting problems detected:", fg=typer.colors.RED, bold=True)
        for issue in issues:
            typer.secho(issue, fg=typer.colors.RED)
        typer.secho("\n  Fix: set the key WITHOUT quotes or spaces:", fg=typer.colors.YELLOW)
        typer.secho("    CORRECT:   set MOONSHOT_API_KEY=sk-abc123", fg=typer.colors.GREEN)
        typer.secho('    WRONG:     set MOONSHOT_API_KEY="sk-abc123"', fg=typer.colors.RED)

    typer.echo(
        "\n  Set AI_PROVIDER=claude|openai|gemini|kimi  and the matching API key env var.\n"
        "  Set AI_MODEL to override the default model.\n"
        "  Or pass --ai-provider / --ai-model flags to the init command.\n"
        "\n  Provider keys:\n"
        "    claude:  ANTHROPIC_API_KEY   (starts with sk-ant-)\n"
        "    openai:  OPENAI_API_KEY     (starts with sk-)\n"
        "    gemini:  GOOGLE_API_KEY     (starts with AIza)\n"
        "    kimi:    OPENROUTER_API_KEY (from openrouter.ai, starts with sk-or-)\n"
    )


if __name__ == "__main__":
    app()
