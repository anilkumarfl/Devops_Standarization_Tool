"""
interactive_prompts.py — Conversational CLI input collection
─────────────────────────────────────────────────────────────
When infra.yaml is missing or partially filled, this module prompts
for each missing required field in sequence.

Behaviour:
  - Only prompts for fields NOT already present in the loaded config.
  - For decisions with trade-offs, prints an explanation before asking.
  - Every collected value is logged to decisions.md via decisions.py.
  - If cloud.provider is not 'aws', exits clearly (GCP/Azure are v2).
  - If stage is 'prototype' and a complex service is chosen, warns.

Usage:
  from interactive_prompts import fill_missing_fields
  config = fill_missing_fields(config, decisions_path)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import typer

import decisions as dec

# ─────────────────────────────────────────────────────────────────────────────
# Anti-pattern checks (non-blocking warnings)
# ─────────────────────────────────────────────────────────────────────────────

ANTI_PATTERNS = [
    {
        "when":    lambda c: c.get("runtime", {}).get("containerised") and
                             c.get("team", {}).get("size") in ("solo", "small") and
                             c.get("stage") in ("prototype", "early") and
                             "eks" in c.get("services", []),
        "message": "EKS selected for a solo/small team at prototype/early stage. "
                   "Consider ECS Fargate or Lambda — lower ops overhead at this stage.",
    },
    {
        "when":    lambda c: not c.get("auth", {}).get("required") and
                             c.get("project", {}).get("type") in ("backend", "web-api") and
                             bool(c.get("data", {}).get("stores")),
        "message": "auth.required is not set on a backend project that has a data store. "
                   "If this API is user-facing, authentication is almost certainly required.",
    },
    {
        "when":    lambda c: c.get("team", {}).get("ops_maturity") == "low" and
                             "eks" in c.get("services", []),
        "message": "EKS chosen with ops_maturity=low. "
                   "EKS requires cluster operations expertise. Consider ECS Fargate.",
    },
]


def _check_anti_patterns(config: dict, decisions_path: Path) -> None:
    for ap in ANTI_PATTERNS:
        if ap["when"](config):
            typer.secho(f"\n  [!] WARNING: {ap['message']}", fg=typer.colors.YELLOW)
            dec.log_warning(ap["message"], path=decisions_path)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level prompt helpers
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_choice(
    question:  str,
    options:   list[tuple[str, str]],  # [(value, description), ...]
    default:   Optional[str] = None,
) -> str:
    """
    Display a numbered choice menu and return the chosen value.
    options: list of (value, trade-off description) pairs.
    """
    typer.echo("")
    typer.secho(f"  > {question}", fg=typer.colors.CYAN, bold=True)
    for i, (val, desc) in enumerate(options, 1):
        typer.secho(f"    [{i}] {val}", fg=typer.colors.WHITE, bold=True)
        if desc:
            typer.secho(f"        {desc}", fg=typer.colors.WHITE)

    default_hint = f" (default: {default})" if default else ""
    while True:
        raw = typer.prompt(f"  Enter choice [1-{len(options)}]{default_hint}").strip()
        if not raw and default:
            for val, _ in options:
                if val == default:
                    return val
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        typer.secho(f"  Invalid — enter a number between 1 and {len(options)}.", fg=typer.colors.RED)


def _prompt_text(
    question: str,
    default:  Optional[str] = None,
    validate: Optional[callable] = None,
    hint:     Optional[str] = None,
) -> str:
    """Prompt for free text with optional validation."""
    typer.echo("")
    typer.secho(f"  > {question}", fg=typer.colors.CYAN, bold=True)
    if hint:
        typer.secho(f"    {hint}", fg=typer.colors.WHITE)
    default_hint = f" [{default}]" if default else ""
    while True:
        raw = typer.prompt(f"  Enter value{default_hint}").strip()
        if not raw and default:
            raw = default
        if validate:
            err = validate(raw)
            if err:
                typer.secho(f"  {err}", fg=typer.colors.RED)
                continue
        return raw


def _prompt_bool(question: str, default: bool = False) -> bool:
    """Yes/no prompt."""
    typer.echo("")
    typer.secho(f"  > {question}", fg=typer.colors.CYAN, bold=True)
    hint = "[Y/n]" if default else "[y/N]"
    raw  = typer.prompt(f"  {hint}").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "true", "1")


def _prompt_multiselect(
    question: str,
    options:  list[str],
    hint:     str = "Enter comma-separated numbers, or leave blank for none",
) -> list[str]:
    """Multi-select from a numbered list. Returns list of chosen values."""
    typer.echo("")
    typer.secho(f"  > {question}", fg=typer.colors.CYAN, bold=True)
    typer.secho(f"    {hint}", fg=typer.colors.WHITE)
    for i, opt in enumerate(options, 1):
        typer.echo(f"    [{i}] {opt}")
    while True:
        raw = typer.prompt("  Selections").strip()
        if not raw:
            return []
        chosen = []
        invalid = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(options):
                chosen.append(options[int(part) - 1])
            elif part:
                invalid.append(part)
        if invalid:
            typer.secho(
                f"  Invalid entries: {', '.join(invalid)}. "
                f"Enter numbers between 1 and {len(options)}, comma-separated.",
                fg=typer.colors.RED,
            )
            continue
        return chosen


def _log_and_echo(
    field: str,
    value,
    source: str,
    reason: str,
    revisit: Optional[str] = None,
    decisions_path: Path = Path(".infra/decisions.md"),
) -> None:
    typer.secho(f"  > Logged: {field} = {value}  [{source}]", fg=typer.colors.GREEN)
    dec.log_decision(field, value, source, reason, revisit, path=decisions_path)


# ─────────────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────────────

def _validate_project_name(name: str) -> Optional[str]:
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', name) or len(name) > 20:
        return (
            "Invalid name. Must be lowercase letters, digits, and hyphens only. "
            "Max 20 chars. Example: payments-api, my-service"
        )
    return None


def _validate_region(region: str) -> Optional[str]:
    if not re.match(r'^[a-z]{2}-[a-z]+-\d$', region):
        return "Invalid AWS region. Example: us-east-1, eu-west-1, ap-southeast-2"
    return None


def _validate_owner(owner: str) -> Optional[str]:
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', owner) or len(owner) > 30:
        return (
            "Invalid owner. Must be lowercase letters, digits, and hyphens only. "
            "Max 30 chars. Example: platform-team, backend-squad"
        )
    return None


def _validate_env_name(name: str) -> Optional[str]:
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', name) or len(name) > 10:
        return (
            "Invalid environment name. Lowercase letters, digits, hyphens only. "
            "Max 10 chars. Example: dev, uat, prod, staging"
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Individual field prompts
# ─────────────────────────────────────────────────────────────────────────────

def _ask_cloud_provider(config: dict, dp: Path) -> dict:
    if config.get("cloud", {}).get("provider"):
        return config
    val = _prompt_choice(
        "Which cloud provider?",
        [
            ("aws",   "Mature ecosystem, widest service catalog. v1 target."),
            ("gcp",   "Strong ML/data services. GCP support planned for v2."),
            ("azure", "Enterprise integration. Azure support planned for v2."),
        ],
        default="aws",
    )
    if val != "aws":
        typer.secho(
            f"\n  GCP and Azure support is planned for v2. "
            f"This tool currently targets AWS only.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise SystemExit(1)
    config.setdefault("cloud", {})["provider"] = val
    _log_and_echo("cloud.provider", val, "interactive prompt",
                  "AWS is the v1 target cloud.", decisions_path=dp)
    return config


def _ask_project_type(config: dict, dp: Path) -> dict:
    if config.get("project", {}).get("type"):
        return config
    val = _prompt_choice(
        "What type of project is this?",
        [
            ("backend",       "REST/GraphQL/BFF service. API + data layer + endpoint."),
            ("frontend",      "Static web app only. S3 + CloudFront. No backend compute."),
            ("full-stack",    "Frontend (S3/CloudFront) + backend API + data layer. One scaffold."),
            ("chatbot",       "Conversational UI + AI backend (LLM/RAG). Full-stack AI app."),
            ("data-pipeline", "ETL/batch/streaming. ECS Fargate + EventBridge + S3/Redshift."),
            ("ai-service",    "AI-native backend service. No UI — API only with LLM integration."),
        ],
    )
    config.setdefault("project", {})["type"] = val
    reasons = {
        "backend":       "Request/response compute with a data layer and endpoint.",
        "frontend":      "No server-side compute — S3 + CloudFront is the right fit.",
        "full-stack":    "Frontend (static-site) + backend compute + data layer in one scaffold.",
        "chatbot":       "Conversational UI with AI backend — static-site + backend + Bedrock/LLM.",
        "data-pipeline": "Moves/transforms/loads data on schedule or event trigger.",
        "ai-service":    "LLM-backed API — no UI, backend-only AI service.",
    }
    _log_and_echo("project.type", val, "interactive prompt", reasons[val], decisions_path=dp)
    return config


def _ask_project_name(config: dict, dp: Path) -> dict:
    if config.get("project", {}).get("name"):
        _show_naming_preview(config["project"]["name"])
        return config
    val = _prompt_text(
        "Project name?",
        hint="Lowercase letters, hyphens, max 20 chars. Example: payments-api",
        validate=_validate_project_name,
    )
    config.setdefault("project", {})["name"] = val
    _log_and_echo("project.name", val, "interactive prompt",
                  "Used in every resource name and tag.", decisions_path=dp)
    _show_naming_preview(val)
    return config


def _show_naming_preview(project_name: str) -> None:
    """Print the naming convention that will apply to all generated resources."""
    typer.echo("")
    typer.secho("  Naming convention for generated resources:", fg=typer.colors.BLUE, bold=True)
    typer.secho(f"    Pattern  :  {project_name}-{{environment}}-{{suffix}}", fg=typer.colors.WHITE)
    typer.echo("")
    typer.secho("    Resource examples:", fg=typer.colors.WHITE)
    examples = [
        ("Lambda function",        f"{project_name}-dev-func"),
        ("Lambda IAM role",        f"{project_name}-dev-lambda-role"),
        ("EKS cluster",            f"{project_name}-dev-eks"),
        ("ECR repository",         f"{project_name}-dev-app"),
        ("SQS queue",              f"{project_name}-dev-queue"),
        ("SQS dead-letter queue",  f"{project_name}-dev-dlq"),
        ("SNS topic",              f"{project_name}-dev-notifications"),
        ("KMS alias",              f"alias/{project_name}-dev"),
        ("Secrets Manager",        f"{project_name}/dev/app"),
        ("CloudWatch log group",   f"/aws/lambda/{project_name}-dev-func"),
    ]
    for label, name in examples:
        typer.secho(f"      {label:<26} {name}", fg=typer.colors.WHITE)
    typer.echo("")
    typer.secho("    Separators:", fg=typer.colors.WHITE)
    typer.secho("      Hyphen  -   resource names (roles, queues, clusters, alarms)", fg=typer.colors.WHITE)
    typer.secho("      Slash   /   path-based names (Secrets Manager, log groups)", fg=typer.colors.WHITE)
    typer.secho("      Snake   _   Terraform variable names (lambda_timeout, eks_node_count)", fg=typer.colors.WHITE)
    typer.echo("")


def _ask_stage(config: dict, dp: Path) -> dict:
    if config.get("stage"):
        return config
    val = _prompt_choice(
        "What stage is this project at?",
        [
            ("prototype", "Exploring/validating. No real users yet. Keep infra minimal."),
            ("early",     "First real users. Product-market fit phase. Some reliability needed."),
            ("growth",    "Scaling up. Reliability becoming critical. Multi-AZ starts here."),
            ("scale",     "High traffic. Strict SLAs. Full HA, EKS at high ops_maturity."),
        ],
    )
    config["stage"] = val
    revisits = {
        "prototype": "Revisit when first real users arrive — early stage changes the defaults.",
        "early":     "Revisit at growth stage — multi-AZ and auto-scaling become appropriate.",
        "growth":    None,
        "scale":     None,
    }
    _log_and_echo("stage", val, "interactive prompt",
                  f"Stage gates infra complexity — {val} prevents over-engineering.",
                  revisit=revisits.get(val), decisions_path=dp)
    return config


def _ask_runtime(config: dict, dp: Path) -> dict:
    rt = config.get("runtime", {})

    if not rt.get("language"):
        val = _prompt_choice(
            "Primary programming language?",
            [
                ("python", "Python 3.x"),
                ("node",   "Node.js / TypeScript"),
                ("go",     "Go"),
                ("java",   "Java / Kotlin"),
                ("ruby",   "Ruby"),
            ],
        )
        config.setdefault("runtime", {})["language"] = val
        _log_and_echo("runtime.language", val, "interactive prompt",
                      "Used to select the correct runtime in generated compute config.",
                      decisions_path=dp)

    if config.get("project", {}).get("type") in ("frontend",):
        config.setdefault("runtime", {})["containerised"] = False
        return config

    if config.get("project", {}).get("type") in ("full-stack", "chatbot"):
        # Always has both frontend (static) and backend compute — skip the question
        config.setdefault("runtime", {})["containerised"] = True
        return config

    if "containerised" not in config.get("runtime", {}):
        val = _prompt_bool(
            "Is this application containerised (Docker / ECS / EKS)?  "
            "Answer 'no' for Lambda / serverless.",
            default=True,
        )
        config.setdefault("runtime", {})["containerised"] = val
        _log_and_echo(
            "runtime.containerised", val, "interactive prompt",
            "True -> ECS Fargate or EKS compute. False -> Lambda / serverless.",
            decisions_path=dp,
        )
    return config


def _ask_team(config: dict, dp: Path) -> dict:
    team = config.get("team", {})

    if not team.get("size"):
        val = _prompt_choice(
            "What is the team size?",
            [
                ("solo",   "1 person."),
                ("small",  "2-5 people."),
                ("medium", "6-15 people."),
                ("large",  "15+ people."),
            ],
        )
        config.setdefault("team", {})["size"] = val
        _log_and_echo("team.size", val, "interactive prompt",
                      "Team size informs sensible defaults for ops overhead.",
                      decisions_path=dp)

    if not team.get("ops_maturity"):
        val = _prompt_choice(
            "What is the team's DevOps / ops maturity level?",
            [
                ("low",    "Fully managed services preferred. Minimal custom infra ops."),
                ("medium", "Comfortable with ECS, RDS, Redis. Some Kubernetes experience."),
                ("high",   "EKS, KMS, advanced AWS services. Strong SRE or platform practice."),
            ],
        )
        config.setdefault("team", {})["ops_maturity"] = val
        revisit = "Revisit when team grows or an SRE practice is established." if val == "low" else None
        _log_and_echo("team.ops_maturity", val, "interactive prompt",
                      "Gates complexity — high maturity unlocks EKS and advanced services.",
                      revisit=revisit, decisions_path=dp)
    return config


def _ask_region(config: dict, dp: Path) -> dict:
    if config.get("cloud", {}).get("region") or config.get("project", {}).get("region"):
        return config
    val = _prompt_text(
        "AWS region?",
        default="us-east-1",
        validate=_validate_region,
        hint="Example: us-east-1, eu-west-1, ap-southeast-2",
    )
    config.setdefault("cloud", {})["region"] = val
    _log_and_echo("cloud.region", val, "interactive prompt",
                  "All resources are deployed into this region.", decisions_path=dp)
    return config


def _ask_owner(config: dict, dp: Path) -> dict:
    if config.get("project", {}).get("owner") or config.get("owner"):
        return config
    val = _prompt_text(
        "Team or squad owner name?",
        hint="Lowercase letters, hyphens, max 30 chars. Example: platform-team, backend-squad",
        validate=_validate_owner,
    )
    config.setdefault("project", {})["owner"] = val
    _log_and_echo("project.owner", val, "interactive prompt",
                  "Owner tag is required on all AWS resources for cost attribution.",
                  decisions_path=dp)
    return config


def _ask_environments(config: dict, dp: Path) -> dict:
    if config.get("environments"):
        return config
    use_default = _prompt_bool(
        "Use standard environments: dev, staging, prod?  "
        "Answer 'no' to specify custom environments.",
        default=True,
    )
    if use_default:
        config["environments"] = {"dev": {}, "staging": {}, "prod": {}}
        _log_and_echo("environments", "dev, staging, prod", "interactive prompt",
                      "Three environments generated by default per spec (section 5.3).",
                      decisions_path=dp)
    else:
        while True:
            raw = _prompt_text(
                "List environments (comma-separated):",
                hint="Lowercase, hyphens, max 10 chars each. Example: dev, uat, prod",
            )
            env_names = [e.strip() for e in raw.split(",") if e.strip()]
            errors = [f"  '{n}': {_validate_env_name(n)}" for n in env_names if _validate_env_name(n)]
            if errors:
                typer.secho("\n".join(errors), fg=typer.colors.RED)
                continue
            break
        config["environments"] = {e: {} for e in env_names}
        _log_and_echo("environments", ", ".join(env_names), "interactive prompt",
                      "Custom environment list specified by user.", decisions_path=dp)
    return config


def _ask_aws_account_structure(config: dict, dp: Path) -> dict:
    if config.get("aws", {}).get("account_structure"):
        return config
    typer.echo("")
    typer.secho(
        "  > No AWS account structure specified.\n"
        "    How should environments be separated?",
        fg=typer.colors.CYAN, bold=True,
    )
    val = _prompt_choice(
        "AWS account structure:",
        [
            ("per-environment",
             "One AWS account per environment (dev / staging / prod accounts).  "
             "Best practice for strict blast radius isolation. Higher account management overhead."),
            ("single-account",
             "Single AWS account, environments namespaced by naming convention.  "
             "Lower overhead. Suitable for early-stage or internal tooling. Less isolation."),
        ],
    )
    config.setdefault("aws", {})["account_structure"] = val
    reasons = {
        "per-environment": "Strict blast radius isolation — each env is a separate AWS account.",
        "single-account":  "Lower overhead — suitable at early stage. Environments share an account.",
    }
    revisit = (
        "If project grows to handle customer data or compliance requirements, "
        "consider migrating to per-environment accounts."
    ) if val == "single-account" else None
    _log_and_echo("aws.account_structure", val, "interactive prompt",
                  reasons[val], revisit=revisit, decisions_path=dp)
    return config


def _ask_compute(config: dict, dp: Path) -> dict:
    """Ask which compute service(s) to use. Skipped if services already set in infra.yaml."""
    existing = config.get("services", [])
    COMPUTE = ["lambda", "ecs-fargate", "eks", "ec2"]
    if any(s in existing for s in COMPUTE):
        return config  # compute already defined

    ptype = config.get("project", {}).get("type", "")
    containerised = config.get("runtime", {}).get("containerised", True)

    # For full-stack and chatbot always add static-site + a backend compute
    if ptype in ("full-stack", "chatbot"):
        typer.echo("")
        typer.secho(
            f"  > {ptype} project: static frontend (CloudFront + S3) will be added automatically.",
            fg=typer.colors.CYAN, bold=True,
        )
        typer.secho("    Now choose the backend compute:", fg=typer.colors.WHITE)
        backend = _prompt_choice(
            "Backend compute for the API / server side?",
            [
                ("lambda",      "Serverless. Best for event-driven, low-traffic, or prototype stage."),
                ("ecs-fargate", "Containerised. Good balance of control and managed ops."),
                ("eks",         "Kubernetes. Use when team has high ops maturity and large scale."),
            ],
        )
        svcs = config.setdefault("services", [])
        if "static-site" not in svcs:
            svcs.append("static-site")
        if backend not in svcs:
            svcs.append(backend)
        if ptype == "chatbot" and "bedrock" not in svcs:
            svcs.append("bedrock")
            typer.secho("    + bedrock added automatically for chatbot AI integration.", fg=typer.colors.GREEN)
        _log_and_echo(
            "services.compute", f"static-site + {backend}",
            "interactive prompt",
            f"{ptype} project always includes static frontend + backend compute.",
            decisions_path=dp,
        )
        return config

    # frontend-only — static site, no backend compute
    if ptype == "frontend":
        config.setdefault("services", []).append("static-site")
        _log_and_echo("services.compute", "static-site", "interactive prompt",
                      "Frontend-only project uses S3 + CloudFront.", decisions_path=dp)
        return config

    # All other types — pick one compute
    if containerised:
        options = [
            ("ecs-fargate", "Managed containers. No cluster ops. Good for most teams."),
            ("eks",         "Kubernetes. Use when you need advanced scheduling or high ops maturity."),
            ("ec2",         "Full control over VMs. Use only if ECS/EKS don't fit your use case."),
        ]
    else:
        options = [
            ("lambda",      "Serverless functions. Pay per invocation. Scales to zero."),
            ("ecs-fargate", "Containers with always-on capacity. Use if Lambda limits apply."),
        ]

    val = _prompt_choice("Which compute service?", options)
    config.setdefault("services", []).append(val)
    _log_and_echo("services.compute", val, "interactive prompt",
                  "Primary compute target for this workload.", decisions_path=dp)

    # Optional: API Gateway or ALB
    if val in ("lambda", "ecs-fargate", "eks"):
        ingress_options = []
        if val == "lambda":
            ingress_options = [
                ("api-gateway", "REST / HTTP endpoint managed by AWS. Best for Lambda."),
                ("none",        "No public endpoint — internal function only."),
            ]
        else:
            ingress_options = [
                ("alb",         "Application Load Balancer. Layer-7 routing. Standard for ECS/EKS."),
                ("api-gateway", "API Gateway in front of ECS/EKS. Use for rate limiting / auth offload."),
                ("none",        "No public endpoint — internal service only."),
            ]
        ingress = _prompt_choice("Add an ingress / endpoint?", ingress_options)
        if ingress != "none":
            config["services"].append(ingress)
            _log_and_echo("services.ingress", ingress, "interactive prompt",
                          "Ingress layer for the compute target.", decisions_path=dp)

    return config


def _ask_data_stores(config: dict, dp: Path) -> dict:
    if config.get("data", {}).get("stores"):
        return config

    # If the services list already contains non-compute services, the user has
    # specified their data/messaging layer directly in infra.yaml — skip the prompt.
    _COMPUTE = {"lambda", "ecs-fargate", "eks", "ec2", "static-site"}
    existing_svcs = config.get("services", [])
    if any(s for s in existing_svcs if s not in _COMPUTE):
        return config

    ptype = config.get("project", {}).get("type", "")
    if ptype == "frontend":
        return config  # no data stores for static sites

    STORE_OPTIONS = [
        "postgres", "mysql", "redis", "s3", "dynamodb",
        "sqs", "eventbridge", "opensearch", "kinesis", "msk",
    ]
    chosen = _prompt_multiselect(
        "Which data stores / messaging services does this project use?",
        STORE_OPTIONS,
        hint="Enter comma-separated numbers, or leave blank if none",
    )
    if chosen:
        config.setdefault("data", {})["stores"] = chosen
        _log_and_echo("data.stores", ", ".join(chosen), "interactive prompt",
                      "Data layer services required by this application.", decisions_path=dp)
    return config


def _ask_auth(config: dict, dp: Path) -> dict:
    if "required" in config.get("auth", {}):
        return config
    if config.get("project", {}).get("type") == "frontend":
        return config

    val = _prompt_bool(
        "Does this project require user authentication (Cognito)?",
        default=False,
    )
    config.setdefault("auth", {})["required"] = val
    _log_and_echo("auth.required", val, "interactive prompt",
                  "Cognito user pool is scaffolded when auth.required = true.",
                  decisions_path=dp)
    return config


def _ask_cicd_auto_deploy(config: dict, dp: Path) -> dict:
    if config.get("cicd", {}).get("auto_deploy") is not None:
        return config

    env_names = list(config.get("environments", {}).keys()) or ["dev", "staging", "prod"]

    # Build dynamic options based on the actual environment list
    options: list[tuple[str, str]] = []
    for i, env in enumerate(env_names):
        auto = env_names[: i + 1]
        gate = env_names[i + 1 :]
        if gate:
            desc = (
                f"Auto-deploy {', '.join(auto)}. "
                f"{', '.join(e.capitalize() for e in gate)} require manual approval."
            )
        else:
            desc = f"Auto-deploy all environments ({', '.join(auto)}). No approval gates."
        options.append((str(i + 1), desc))

    # Add an "all" shortcut if not already covered by the last option
    # (last option already means all, so we use the index directly)
    typer.echo("")
    typer.secho("  > Which environments get automatic Terraform apply on push?",
                fg=typer.colors.CYAN, bold=True)
    for idx, (_, desc) in enumerate(options, 1):
        auto_envs = env_names[:idx]
        typer.secho(f"    [{idx}] auto-deploy: {', '.join(auto_envs)}", fg=typer.colors.WHITE, bold=True)
        typer.secho(f"        {desc}", fg=typer.colors.WHITE)

    default_choice = "1"
    while True:
        raw = typer.prompt(f"  Enter choice [1-{len(options)}] (default: 1)").strip()
        if not raw:
            raw = default_choice
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            chosen_idx = int(raw)
            break
        typer.secho(f"  Invalid — enter a number between 1 and {len(options)}.", fg=typer.colors.RED)

    auto_deploy = env_names[:chosen_idx]
    config.setdefault("cicd", {})["auto_deploy"] = auto_deploy
    _log_and_echo("cicd.auto_deploy", auto_deploy, "interactive prompt",
                  "Controls which environments have automatic Terraform apply in CI/CD pipeline.",
                  decisions_path=dp)
    return config


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def fill_missing_fields(
    config:          dict,
    decisions_path:  Path = Path(".infra/decisions.md"),
    skip_aws_account: bool = False,
) -> dict:
    """
    Walk through every required field. Prompt for anything missing.
    Returns the fully-populated config dict.
    """
    typer.echo("")
    typer.secho(
        "  Checking infra.yaml for missing required fields...",
        fg=typer.colors.BLUE,
    )

    steps = [
        _ask_cloud_provider,
        _ask_project_type,
        _ask_project_name,
        _ask_stage,
        _ask_runtime,
        _ask_compute,
        _ask_team,
        _ask_region,
        _ask_owner,
        _ask_environments,
        _ask_data_stores,
        _ask_auth,
        _ask_cicd_auto_deploy,
    ]
    if not skip_aws_account:
        steps.insert(-1, _ask_aws_account_structure)

    for step in steps:
        config = step(config, decisions_path)

    _check_anti_patterns(config, decisions_path)
    return config


def is_config_complete(config: dict) -> bool:
    """
    Returns True if all required fields are present in the config
    so the tool can skip interactive prompts entirely.

    Two paths to "complete":

    Path A — Full wizard fields present (generated by the wizard or typed manually):
      cloud.provider, project.type, stage, runtime, team, region, owner, services (compute)

    Path B — Direct infra.yaml with services list:
      project.name + (project.region or cloud.region) + project.owner + services (with compute).
      When the user has written their own infra.yaml with a services list, wizard-specific
      fields like cloud.provider / stage / runtime / team are not needed — the generator
      reads directly from services.
    """
    COMPUTE = {"lambda", "ecs-fargate", "eks", "ec2", "static-site"}
    services    = config.get("services", [])
    has_compute = any(s in COMPUTE for s in services)
    has_name    = bool(config.get("project", {}).get("name"))
    has_region  = bool(
        config.get("project", {}).get("region") or config.get("cloud", {}).get("region")
    )
    has_owner   = bool(config.get("project", {}).get("owner") or config.get("owner"))

    # Path B: explicit services list with the three required base fields
    if services and has_compute and has_name and has_region and has_owner:
        return True

    # Path A: all wizard fields present
    wizard_checks = [
        config.get("cloud", {}).get("provider"),
        config.get("project", {}).get("type"),
        has_name,
        config.get("stage"),
        config.get("runtime", {}).get("language"),
        "containerised" in config.get("runtime", {}),
        config.get("team", {}).get("size"),
        config.get("team", {}).get("ops_maturity"),
        has_region,
        has_owner,
        has_compute,
    ]
    return all(wizard_checks)
