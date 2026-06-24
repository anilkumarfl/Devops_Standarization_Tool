"""
dynamic_generator.py  v4 — structured tool_use output
───────────────────────────────────────────────────────
Two generation paths, both produce deployable Terraform:

  PATH 1 — Static templates (Jinja2 .tf.j2 files)
     Used when a service has `template:` set in services_catalog.yaml.
     Generator.py renders these directly; this file is not involved.

  PATH 2 — Dynamic generation (Claude API tool_use)
     Used when a service has `template: null` in the catalog.
     Claude returns structured {terraform_hcl, variables[]} via
     tool_use — no free-text parsing needed.
     HCL cached in .tf-cache/{svc}-{hash}.tf
     Variables cached in .tf-cache/{svc}-{hash}.vars.json

  INFRA LAYERS — vpc, iam, security-group
     Auto-generated based on the selected services.

  IAM POLICIES — dynamic least-privilege
     Reads iam_actions from the catalog and generates a single
     consolidated aws_iam_policy resource per compute target.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

import yaml
import typer

# ─────────────────────────────────────────────────────────────────────────────
CATALOG_PATH  = Path(__file__).parent / "services_catalog.yaml"
CACHE_DIR     = Path(".infra/.tf-cache")
CLAUDE_MODEL  = "claude-sonnet-4-6"
CLAUDE_TOKENS = 4096
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
#  Catalog helpers
# ═══════════════════════════════════════════════════════════════════════════════

def load_catalog() -> dict:
    if not CATALOG_PATH.exists():
        typer.secho(
            f"  WARNING: {CATALOG_PATH} not found. "
            "Only built-in services will be available.",
            fg=typer.colors.YELLOW,
        )
        return {"services": {}, "infra_layers": {}, "iam_resource_arns": {},
                "compute_rules": {}, "terraform": {}}
    return yaml.safe_load(CATALOG_PATH.read_text()) or {}


def get_service_entry(catalog: dict, service: str) -> dict:
    return catalog.get("services", {}).get(service, {})


def get_all_valid_services(catalog: dict) -> set[str]:
    return set(catalog.get("services", {}).keys())


def get_compute_services(catalog: dict) -> set[str]:
    return {
        name for name, entry in catalog.get("services", {}).items()
        if entry.get("category") == "compute"
    }


def get_valid_compute_combinations(catalog: dict) -> list[list[str]]:
    rules = catalog.get("compute_rules", {})
    return rules.get("valid_combinations", [["lambda", "eks"]])


def get_max_compute_targets(catalog: dict) -> int:
    return catalog.get("compute_rules", {}).get("max_compute_targets", 2)


def get_terraform_config(catalog: dict) -> dict:
    return catalog.get("terraform", {})


# ═══════════════════════════════════════════════════════════════════════════════
#  Infrastructure layer generation (vpc)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_vpc_layer(
    catalog: dict,
    project_name: str,
    region: str,
    owner: str,
    services: list[str],
) -> str:
    vpc_cfg = catalog.get("infra_layers", {}).get("vpc", {})
    mod     = vpc_cfg.get("registry_module", {})
    source  = mod.get("source", "terraform-aws-modules/vpc/aws")
    version = mod.get("version", "~> 6.0")

    services_set = set(services)
    needs_db_subnets = bool(services_set & {
        "postgres", "mysql", "aurora-postgres", "aurora-mysql",
        "redis", "memcached", "opensearch", "documentdb",
    })
    needs_elasticache_subnets = bool(services_set & {"redis", "memcached"})

    db_subnet_block = ""
    if needs_db_subnets:
        db_subnet_block = """
  database_subnets             = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 20)]
  create_database_subnet_group = true"""

    elasticache_block = ""
    if needs_elasticache_subnets:
        elasticache_block = """
  elasticache_subnets = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 30)]"""

    return f"""# ──────────────────────────────────────────────────────────────────────────────
# VPC — auto-generated infrastructure layer
# Source: {source}  {version}
# ──────────────────────────────────────────────────────────────────────────────

locals {{
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}}

data "aws_availability_zones" "available" {{
  state = "available"
}}

module "vpc" {{
  source  = "{source}"
  version = "{version}"

  name = "${{local.name_prefix}}-vpc"
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 10)]{db_subnet_block}{elasticache_block}

  enable_nat_gateway   = true
  single_nat_gateway   = var.environment != "prod"
  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags = {{
    "kubernetes.io/role/elb" = 1
  }}
  private_subnet_tags = {{
    "kubernetes.io/role/internal-elb" = 1
  }}

  tags = local.common_tags
}}
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Dynamic Terraform generation via Claude API (tool_use)
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a senior Terraform engineer generating production-ready HCL that follows
Terraform community best practices and the official AWS provider registry at
https://registry.terraform.io/browse/modules?provider=aws

NAMING CONVENTION — every resource/module MUST follow this pattern:
  name = "${local.name_prefix}-{resource-type}"
  e.g.  "${local.name_prefix}-eks-cluster"
        "${local.name_prefix}-rds-postgres"
        "${local.name_prefix}-lambda-api"
  local.name_prefix is already defined as "${var.project_name}-${var.environment}"

MODULES — prefer terraform-aws-modules over raw resources:
  ALWAYS use official registry modules when a module source and version are provided.
  Use `module` blocks, NOT raw `aws_*` resource blocks for well-known services.
  Pin exact versions from https://registry.terraform.io/browse/modules?provider=aws
  Example: source = "terraform-aws-modules/eks/aws"  version = "~> 20.0"

REFERENCES — use existing locals/vars/module outputs (do NOT redefine):
  var.project_name, var.region, var.environment, var.owner, var.vpc_cidr
  local.name_prefix  (= "${var.project_name}-${var.environment}")
  local.common_tags
  module.vpc.vpc_id, module.vpc.private_subnets,
  module.vpc.public_subnets, module.vpc.database_subnets

SECURITY BEST PRACTICES (from Terraform Best Practices policy):
  - Enable encryption at rest on all data stores (kms_key_id = module.kms.key_arn where available)
  - Enable deletion_protection = true on databases (set to false only in dev)
  - Enable multi_az = var.environment == "prod" on RDS resources
  - Never hardcode secrets — use aws_secretsmanager_secret_version or SSM data sources
  - Use least-privilege IAM — no wildcard "*" actions unless unavoidable
  - Enable access logs on ALBs and API Gateways

TAGGING — every resource must include:
  tags = local.common_tags

OUTPUT RULES:
  - Output ONLY valid HCL in terraform_hcl. No markdown fences, no prose, no comments outside HCL.
  - Do NOT include terraform{}, provider{}, variable{}, locals{}, or backend{} blocks.
  - Include at least one output block: the primary ARN or DNS name of the resource.
  - For variables[]: list ONLY new variables this HCL introduces. NEVER include:
    project_name, region, environment, owner, vpc_cidr (already declared)."""


def _env_value_for(var: dict, env_name: str):
    """Map an environment name to the right recommended value from a var definition."""
    if env_name in var:
        return var[env_name]
    env_lower = env_name.lower()
    if any(x in env_lower for x in ("dev", "test", "local")):
        return var.get("dev", "REPLACE_ME")
    if any(x in env_lower for x in ("staging", "uat", "qa", "pre", "sit")):
        return var.get("staging", var.get("prod", "REPLACE_ME"))
    if any(x in env_lower for x in ("prod", "live", "prd")):
        return var.get("prod", var.get("staging", "REPLACE_ME"))
    return var.get("staging", "REPLACE_ME")


def generate_terraform_dynamically(
    service: str,
    entry: dict,
    project_name: str,
    region: str,
    owner: str,
    all_services: list[str] = None,
    environments: list[str] = None,
) -> Optional[tuple[str, list[dict]]]:
    """
    Generate Terraform HCL + variable declarations for `service` via Claude API tool_use.
    Returns (hcl, variables) where variables is a list of dicts:
      {name, type, description, dev, staging, prod, ...env-specific values}
    Returns None on failure.
    Caches HCL in .tf-cache/{svc}-{hash}.tf and vars in .tf-cache/{svc}-{hash}.vars.json.
    """
    terraform_resource = entry.get("terraform_resource")
    registry_module    = entry.get("registry_module", {})
    module_source      = registry_module.get("source", "")
    module_version     = registry_module.get("version", "")

    # IAM-only services — no Terraform resource or module needed
    if terraform_resource is None and not module_source:
        return f"# {service}: IAM-only service — permissions added to compute role.\n", []

    import ai_client as aic
    _client = aic.get_client()
    if not _client.available:
        provider = os.environ.get("AI_PROVIDER", "claude")
        key_var  = aic.PROVIDER_CONFIG[provider]["key_env"]
        typer.secho(
            f"  ! No {key_var} — cannot generate '{service}' dynamically.\n"
            f"    Set {key_var} or switch provider: set AI_PROVIDER=gemini / openai",
            fg=typer.colors.YELLOW,
        )
        return None

    env_names = environments or ["dev", "staging", "prod"]

    # Stable cache key
    cache_key = hashlib.md5(
        json.dumps({
            "service": service,
            "source":  module_source or terraform_resource,
            "version": module_version,
            "project": project_name,
            "envs":    sorted(env_names),
        }, sort_keys=True).encode()
    ).hexdigest()[:12]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_tf   = CACHE_DIR / f"{service}-{cache_key}.tf"
    cache_vars = CACHE_DIR / f"{service}-{cache_key}.vars.json"

    if cache_tf.exists() and cache_vars.exists():
        typer.secho(f"  + {service}.tf  [cached]", fg=typer.colors.CYAN)
        return cache_tf.read_text(), json.loads(cache_vars.read_text())

    # Build tool schema — env-specific value fields are dynamic
    env_value_props = {
        e: {"description": f"Recommended value for the {e} environment"}
        for e in env_names
    }

    tools = [
        {
            "name": "generate_terraform_service",
            "description": (
                "Generate production-ready Terraform HCL and variable declarations "
                "for an AWS service. Returns structured output — no free-text parsing needed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "terraform_hcl": {
                        "type": "string",
                        "description": (
                            "Valid Terraform HCL for the service. "
                            "No markdown fences. No variable{}, terraform{}, provider{}, "
                            "locals{}, or backend{} blocks."
                        ),
                    },
                    "variables": {
                        "type": "array",
                        "description": (
                            "NEW variable declarations this HCL introduces. "
                            "Omit: project_name, region, environment, owner, vpc_cidr."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Variable name in snake_case",
                                },
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "string", "number", "bool",
                                        "list(string)", "map(string)",
                                    ],
                                },
                                "description": {
                                    "type": "string",
                                    "description": "What this variable controls",
                                },
                                **env_value_props,
                            },
                            "required": ["name", "type", "description"],
                        },
                    },
                },
                "required": ["terraform_hcl", "variables"],
            },
        }
    ]

    # Build prompt
    extra_vars  = entry.get("extra_vars", {})
    iam_actions = entry.get("iam_actions", [])
    category    = entry.get("category", "")

    module_instruction = ""
    if module_source:
        module_instruction = (
            f"\nUSE THIS REGISTRY MODULE:\n"
            f'  source  = "{module_source}"\n'
            f'  version = "{module_version}"\n'
            f"Do NOT use raw aws_* resources for this service — use the module block above."
        )
    elif terraform_resource:
        module_instruction = (
            f"\nNo registry module available. "
            f"Use the raw Terraform resource: {terraform_resource}"
        )

    extra_context = ""
    if extra_vars:
        extra_context += f"Service-specific configuration: {json.dumps(extra_vars)}\n"
    if iam_actions:
        extra_context += f"IAM actions needed: {iam_actions}\n"
    if all_services:
        extra_context += f"Other services in this stack: {all_services}\n"

    prompt = (
        f"Generate Terraform HCL for AWS service: {service}\n\n"
        f"Project context:\n"
        f"  project_name : {project_name}\n"
        f"  aws_region   : {region}\n"
        f"  owner        : {owner}\n"
        f"  category     : {category}\n"
        f"  environments : {', '.join(env_names)}\n\n"
        f"NAMING — follow pattern {{project}}-{{env}}-{{resource-type}}:\n"
        f'  name = "${{local.name_prefix}}-{service.replace("_", "-")}"\n'
        f"  local.name_prefix is already defined as "
        f'"${{var.project_name}}-${{var.environment}}"\n\n'
        f"{module_instruction}\n"
        f"{extra_context}\n"
        f'Terraform identifier for this module/resource: "{service.replace("-", "_")}"\n'
        f"Networking refs: module.vpc.vpc_id, module.vpc.private_subnets, "
        f"module.vpc.public_subnets, module.vpc.database_subnets\n"
        f"Tags: tags = local.common_tags\n"
        f"Security: encryption at rest, deletion_protection where applicable.\n"
        f"Include at least one output block with the primary ARN or DNS name.\n\n"
        f"For variables[]: provide per-environment recommended values for "
        f"{', '.join(env_names)} — these go directly into each env's terraform.tfvars."
    )

    typer.secho(
        f"  ~ Generating '{service}' via AI ({os.environ.get('AI_PROVIDER', 'claude')})...",
        fg=typer.colors.BLUE,
    )

    try:
        client = aic.get_client(max_tokens=CLAUDE_TOKENS)
        result = client.tool_use(tools, prompt, system=SYSTEM_PROMPT,
                                 tool_name="generate_terraform_service")

        if result:
            hcl       = result.get("terraform_hcl", "").strip()
            variables = result.get("variables", [])

            # Validate HCL
            if "module" not in hcl and "resource" not in hcl:
                typer.secho(
                    f"  ! Generated HCL for '{service}' looks invalid — skipping.",
                    fg=typer.colors.YELLOW,
                )
                return None

            provider_label = os.environ.get("AI_PROVIDER", "claude")
            header = (
                f"# {service} — dynamically generated via AI ({provider_label})\n"
                f"# Module: {module_source} {module_version}\n"
                f"# Cached: delete .tf-cache/ to regenerate\n\n"
            )
            full_hcl = header + hcl + "\n"

            cache_tf.write_text(full_hcl, encoding="utf-8")
            cache_vars.write_text(json.dumps(variables, indent=2), encoding="utf-8")
            typer.secho(f"  + {service}.tf  [generated + cached]", fg=typer.colors.GREEN)
            return full_hcl, variables

    except Exception as exc:
        typer.secho(f"  ! Generation failed for '{service}': {exc}", fg=typer.colors.YELLOW)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Dynamic IAM policy generation
# ═══════════════════════════════════════════════════════════════════════════════

def generate_iam_policy_block(
    catalog: dict,
    compute_service: str,
    connected_services: list[str],
    project_name: str,
    region: str,
) -> str:
    all_statements: list[dict] = []
    arn_templates: dict        = catalog.get("iam_resource_arns", {})
    services_section: dict     = catalog.get("services", {})

    for svc in connected_services:
        entry   = services_section.get(svc, {})
        actions = entry.get("iam_actions", [])
        if not actions:
            continue

        raw_arns      = arn_templates.get(svc, ["*"])
        resolved_arns = [
            a.replace("{project}", project_name).replace("{region}", region)
            for a in raw_arns
        ]

        all_statements.append({
            "sid":       _to_sid(svc),
            "actions":   sorted(set(actions)),
            "resources": resolved_arns,
        })

    if not all_statements:
        return ""

    stmts_hcl = ""
    for stmt in all_statements:
        acts = ",\n        ".join(f'"{a}"' for a in stmt["actions"])
        res  = ",\n        ".join(f'"{r}"' for r in stmt["resources"])
        stmts_hcl += f"""      {{
        Sid      = "{stmt['sid']}"
        Effect   = "Allow"
        Action   = [
          {acts}
        ]
        Resource = [
          {res}
        ]
      }},
"""

    return f"""
# ──────────────────────────────────────────────────────────────────────────────
# IAM policy — {compute_service} → connected services (auto-generated)
# ──────────────────────────────────────────────────────────────────────────────
resource "aws_iam_policy" "{compute_service.replace('-', '_')}_service_access" {{
  name        = "${{var.project_name}}-{compute_service}-access-${{var.environment}}"
  description = "Least-privilege access for {compute_service} to connected services"

  policy = jsonencode({{
    Version   = "2012-10-17"
    Statement = [
{stmts_hcl}    ]
  }})

  tags = local.common_tags
}}
"""


def _to_sid(service: str) -> str:
    return "".join(part.title() for part in service.replace("-", " ").split()) + "Access"


# ═══════════════════════════════════════════════════════════════════════════════
#  Cache management
# ═══════════════════════════════════════════════════════════════════════════════

def clear_cache(service: Optional[str] = None) -> int:
    if not CACHE_DIR.exists():
        return 0
    pattern = f"{service}-*" if service else "*"
    removed = 0
    for f in CACHE_DIR.glob(pattern):
        f.unlink()
        removed += 1
    return removed
