"""
generator.py  (v3 " env-per-folder, variables as declarations only)
""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
Output structure:
  .infra/
    provider.tf          terraform + provider blocks + locals
    networking.tf        VPC module
    main.tf              compute resources (EKS, Lambda, ECS, EC2)
    data.tf              databases, caches, queues, storage
    iam.tf               IAM roles + policies
    observability.tf     CloudWatch, X-Ray
    output.tf            Terraform outputs
    variables.tf         Variable DECLARATIONS only " no hardcoded defaults
    env/
      {env}/
        backend.tf               S3 backend config pointing to env state file
        terraform.tfvars         Actual variable values for this environment
        terraform.tfvars.example Checked-in example with placeholder comments
    cicd/
      pipeline.yml
    secrets/
      secrets-policy.yml

Variables flow:
  variables.tf   declarations (name + type + description, no default)
  env/{env}/terraform.tfvars  actual values per environment
    Base vars  : project_name, region, owner, environment, vpc_cidr, cost_centre
    Static vars: well-known per-service vars (eks_instance_type, db_instance_class)
    Dynamic vars: Claude API returns variables[] alongside terraform_hcl
"""

from pathlib import Path
from typing import Any
import typer
from jinja2 import Environment, FileSystemLoader

import sys, importlib.util
from pathlib import Path as _Path
_dg_path = _Path(__file__).parent / "dynamic_generator.py"
if "dynamic_generator" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("dynamic_generator", _dg_path)
    dg    = importlib.util.module_from_spec(_spec)
    sys.modules["dynamic_generator"] = dg
    _spec.loader.exec_module(dg)
else:
    dg = sys.modules["dynamic_generator"]


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Base variables " always present in every stack
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
BASE_VARS: list[dict] = [
    {"name": "project_name", "type": "string",
     "description": "Project name used in resource naming and tags"},
    {"name": "region",       "type": "string",
     "description": "AWS region to deploy resources into"},
    {"name": "owner",        "type": "string",
     "description": "Team or individual owning this infrastructure"},
    {"name": "environment",  "type": "string",
     "description": "Deployment environment (dev, staging, prod, uat)"},
    {"name": "vpc_cidr",     "type": "string",
     "description": "CIDR block for the VPC (e.g. 10.0.0.0/16)"},
    {"name": "cost_centre",  "type": "string",
     "description": "Cost centre code for billing and tagging"},
]

# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Well-known per-service variables for static-template services
# Each entry has per-environment recommended values (dev / staging / prod).
# The fuzzy env mapper in dynamic_generator._env_value_for handles aliases
# like "uat" ' staging, "live" ' prod automatically.
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
STATIC_SERVICE_VARS: dict[str, list[dict]] = {
    "eks": [
        {"name": "eks_node_count",      "type": "number",
         "description": "Number of EKS worker nodes",
         "dev": 1, "staging": 2, "prod": 3},
        {"name": "eks_instance_type",   "type": "string",
         "description": "EC2 instance type for EKS worker nodes",
         "dev": "t3.medium", "staging": "m5.large", "prod": "m5.xlarge"},
        {"name": "eks_cluster_version", "type": "string",
         "description": "Kubernetes version for the EKS cluster",
         "dev": "1.33", "staging": "1.33", "prod": "1.33"},
    ],
    "lambda": [
        {"name": "lambda_memory_size", "type": "number",
         "description": "Lambda function memory in MB",
         "dev": 256, "staging": 512, "prod": 1024},
        {"name": "lambda_timeout",     "type": "number",
         "description": "Lambda function timeout in seconds",
         "dev": 30, "staging": 30, "prod": 30},
        {"name": "lambda_s3_bucket",   "type": "string",
         "description": "S3 bucket containing the Lambda deployment package",
         "dev": "REPLACE_WITH_DEPLOY_BUCKET", "staging": "REPLACE_WITH_DEPLOY_BUCKET",
         "prod": "REPLACE_WITH_DEPLOY_BUCKET"},
        {"name": "lambda_s3_key",      "type": "string",
         "description": "S3 key path to the Lambda deployment zip",
         "dev": "lambda/app.zip", "staging": "lambda/app.zip", "prod": "lambda/app.zip"},
        {"name": "log_retention_days", "type": "number",
         "description": "CloudWatch log retention in days",
         "dev": 7, "staging": 30, "prod": 90},
    ],
    "ecs-fargate": [
        {"name": "ecs_task_cpu",      "type": "number",
         "description": "ECS task CPU units (256 = 0.25 vCPU)",
         "dev": 256, "staging": 512, "prod": 1024},
        {"name": "ecs_task_memory",   "type": "number",
         "description": "ECS task memory in MiB",
         "dev": 512, "staging": 1024, "prod": 2048},
        {"name": "ecs_desired_count", "type": "number",
         "description": "Desired number of running ECS tasks",
         "dev": 1, "staging": 2, "prod": 3},
    ],
    "ec2": [
        {"name": "ec2_instance_type",  "type": "string",
         "description": "EC2 instance type",
         "dev": "t3.micro", "staging": "t3.small", "prod": "m5.large"},
        {"name": "ec2_instance_count", "type": "number",
         "description": "Number of EC2 instances",
         "dev": 1, "staging": 2, "prod": 3},
    ],
    "postgres": [
        {"name": "db_instance_class",    "type": "string",
         "description": "RDS instance class",
         "dev": "db.t3.micro", "staging": "db.t3.small", "prod": "db.m5.large"},
        {"name": "db_allocated_storage", "type": "number",
         "description": "RDS allocated storage in GB",
         "dev": 20, "staging": 50, "prod": 100},
        {"name": "db_multi_az",          "type": "bool",
         "description": "Enable RDS Multi-AZ for high availability",
         "dev": False, "staging": False, "prod": True},
    ],
    "mysql": [
        {"name": "db_instance_class",    "type": "string",
         "description": "RDS instance class",
         "dev": "db.t3.micro", "staging": "db.t3.small", "prod": "db.m5.large"},
        {"name": "db_allocated_storage", "type": "number",
         "description": "RDS allocated storage in GB",
         "dev": 20, "staging": 50, "prod": 100},
        {"name": "db_multi_az",          "type": "bool",
         "description": "Enable RDS Multi-AZ for high availability",
         "dev": False, "staging": False, "prod": True},
    ],
    "aurora-postgres": [
        {"name": "aurora_instance_class", "type": "string",
         "description": "Aurora instance class",
         "dev": "db.t3.medium", "staging": "db.r5.large", "prod": "db.r5.xlarge"},
        {"name": "aurora_instance_count", "type": "number",
         "description": "Number of Aurora cluster instances",
         "dev": 1, "staging": 2, "prod": 3},
    ],
    "aurora-mysql": [
        {"name": "aurora_instance_class", "type": "string",
         "description": "Aurora instance class",
         "dev": "db.t3.medium", "staging": "db.r5.large", "prod": "db.r5.xlarge"},
        {"name": "aurora_instance_count", "type": "number",
         "description": "Number of Aurora cluster instances",
         "dev": 1, "staging": 2, "prod": 3},
    ],
    "redis": [
        {"name": "redis_node_type",  "type": "string",
         "description": "ElastiCache node type for Redis",
         "dev": "cache.t3.micro", "staging": "cache.t3.small", "prod": "cache.m5.large"},
        {"name": "redis_num_nodes",  "type": "number",
         "description": "Number of Redis cache nodes",
         "dev": 1, "staging": 1, "prod": 2},
    ],
    "dynamodb": [
        {"name": "dynamodb_billing_mode", "type": "string",
         "description": "DynamoDB billing mode: PROVISIONED or PAY_PER_REQUEST",
         "dev": "PAY_PER_REQUEST", "staging": "PAY_PER_REQUEST", "prod": "PROVISIONED"},
    ],
    "alb": [
        {"name": "alb_idle_timeout", "type": "number",
         "description": "ALB connection idle timeout in seconds",
         "dev": 60, "staging": 60, "prod": 60},
    ],
    "opensearch": [
        {"name": "opensearch_instance_type",  "type": "string",
         "description": "OpenSearch instance type",
         "dev": "t3.small.search", "staging": "m5.large.search", "prod": "m5.xlarge.search"},
        {"name": "opensearch_instance_count", "type": "number",
         "description": "Number of OpenSearch data nodes",
         "dev": 1, "staging": 2, "prod": 3},
    ],
    "api-gateway": [
        {"name": "api_gateway_stage", "type": "string",
         "description": "API Gateway deployment stage name",
         "dev": "dev", "staging": "staging", "prod": "prod"},
    ],
}


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Jinja2 environments
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _make_env(templates_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _make_cicd_env(templates_dir: Path) -> Environment:
    """Custom delimiters so GitHub Actions ${{ }} syntax passes through unchanged."""
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        variable_start_string="<<",  variable_end_string=">>",
        block_start_string="<%",     block_end_string="%>",
        comment_start_string="<#",   comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
    )


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Implicit connection inference
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _implicit_connections(services: list, compute_target: str) -> set:
    s = set(services)
    conns = set()
    if "eventbridge" in s and "sqs" in s:
        conns.add("eventbridge->sqs")
    if "eventbridge" in s and "sqs" not in s and compute_target == "lambda":
        conns.add("eventbridge->lambda")
    if "sqs" in s and compute_target == "lambda":
        conns.add("sqs->lambda")
    if "sqs" in s and compute_target == "ecs-fargate":
        conns.add("sqs->ecs-fargate")
    if "alb" in s and compute_target in ("ecs-fargate", "eks"):
        conns.add(f"alb->{compute_target}")
    if "api-gateway" in s and compute_target == "lambda":
        conns.add("api-gateway->lambda")
    for store in ("postgres", "mysql", "redis", "dynamodb", "s3",
                  "opensearch", "kinesis", "msk"):
        if store in s:
            conns.add(f"{compute_target}->{store}")
    return conns


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Render helpers
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _render(env: Environment, template_path: str, output_path: Path, ctx: dict) -> None:
    try:
        content = env.get_template(template_path).render(**ctx)
        output_path.write_text(content + "\n", encoding="utf-8")
        typer.secho(f"  + {output_path.name}  [{template_path}]", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  ! failed {template_path}: {e}", fg=typer.colors.YELLOW)


def _render_combined(env: Environment, template_paths: list, output_path: Path,
                     ctx: dict, labels: list = None) -> None:
    blocks = []
    for i, tp in enumerate(template_paths):
        label = (labels[i] if labels else None) or tp
        try:
            blocks.append(env.get_template(tp).render(**ctx))
            typer.secho(f"  + {output_path.name}  [{label}]", fg=typer.colors.GREEN)
        except Exception as e:
            typer.secho(f"  ! failed {tp}: {e}", fg=typer.colors.YELLOW)
    if blocks:
        output_path.write_text("\n".join(blocks) + "\n", encoding="utf-8")


def _render_per_label(env: Environment, template_paths: list,
                      ctx: dict, labels: list = None) -> dict[str, str]:
    """Render each template and return a dict of label -> rendered HCL."""
    result: dict[str, str] = {}
    for i, tp in enumerate(template_paths):
        label = (labels[i] if labels else None) or tp
        try:
            result[label] = env.get_template(tp).render(**ctx)
        except Exception as e:
            typer.secho(f"  ! failed {tp}: {e}", fg=typer.colors.YELLOW)
    return result


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Main entry point
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def generate_scaffold(
    config: dict,
    catalog: dict,
    output_dir: str = ".infra",
    templates_dir: str = "../parent-repo/templates",
) -> None:
    base     = Path(output_dir)
    tmpl_dir = Path(templates_dir)

    # "" Unpack config """"""""""""""""""""""""""""""""""""""""""""""""""""""
    project      = config["project"]
    project_name = project["name"]
    region       = project["region"]
    owner        = project["owner"]
    services     = config.get("services", [])
    cicd_cfg     = config.get("cicd", {})
    environments = config.get("environments", {})
    flows        = config.get("flows", {})
    env_names    = list(environments.keys()) if environments else ["dev", "staging", "prod"]

    # "" Resolve services from catalog """"""""""""""""""""""""""""""""""""""
    compute_service_names = dg.get_compute_services(catalog)
    compute_list          = [s for s in services if s in compute_service_names]
    compute_target        = compute_list[0]
    other_services        = [s for s in services if s not in compute_service_names]

    catalog_services = catalog.get("services", {})
    ingress_svcs     = {
        name: entry
        for name, entry in catalog_services.items()
        if entry.get("valid_compute_targets")
    }
    ingress_keys  = set(ingress_svcs.keys())
    data_stores   = [s for s in other_services if s not in ingress_keys]
    auth_required = "cognito" in services

    auto_deploy = cicd_cfg.get("auto_deploy", ["dev"])
    cicd_envs   = "auto-dev-staging" if "staging" in auto_deploy else "auto-dev"

    raw_connections = config.get("connections", [])
    if raw_connections:
        connections = {
            f"{c['from']}->{c['to']}"
            for c in raw_connections
            if "from" in c and "to" in c
        }
    else:
        connections = _implicit_connections(services, compute_target)

    # "" Jinja2 environments """"""""""""""""""""""""""""""""""""""""""""""""
    jinja_env = _make_env(tmpl_dir)
    cicd_env  = _make_cicd_env(tmpl_dir)

    # -- Output directory structure -------------------------------------------
    for sub in ["cicd", "secrets"]:
        (base / sub).mkdir(parents=True, exist_ok=True)
    for env_name in env_names:
        (base / "env" / env_name).mkdir(parents=True, exist_ok=True)

    # Clean up stale per-service .tf files from previous runs so renamed/removed
    # services don't leave orphan files. Keep fixed-name files managed elsewhere.
    _FIXED_TF_FILES = {
        "provider.tf", "networking.tf", "main.tf", "iam.tf",
        "observability.tf", "output.tf", "variables.tf",
    }
    for tf_file in base.glob("*.tf"):
        if tf_file.name not in _FIXED_TF_FILES:
            tf_file.unlink()  # removes stale service files AND the old data.tf

    # modules/ -- one sub-folder per local reusable module
    _write_modules_scaffold(base, services)

    # Observability config
    observability  = config.get("observability", {})
    _ret_raw      = observability.get("log_retention_days", 30)
    log_retention = (
        _ret_raw if isinstance(_ret_raw, dict)
        else {"dev": _ret_raw, "staging": _ret_raw, "prod": _ret_raw}
    )
    enable_xray    = observability.get("xray", False)
    enable_metrics = observability.get("metrics", True)

    # Shared template context
    ctx = {
        "project_name":   project_name,
        "project_type":   config.get("project", {}).get("type", "web-api"),
        "region":         region,
        "owner":          owner,
        "compute_target": compute_target,
        "compute":        compute_list,   # list of compute services e.g. ["lambda", "eks"]
        "services":       services,       # all services including non-compute
        "data_stores":    data_stores,
        "auth_required":  auth_required,
        "connections":    connections,
        "flows":          flows,
        "environments":   environments,
        "cicd_envs":      cicd_envs,
        "log_retention":  log_retention,
        "enable_xray":    enable_xray,
        "enable_metrics": enable_metrics,
    }

    # "" Collect variables from all sources """""""""""""""""""""""""""""""""
    # All dynamic_vars are accumulated here; written to variables.tf + tfvars at end.
    dynamic_vars: list[dict] = []

    for svc in compute_list + list(other_services):
        if svc in STATIC_SERVICE_VARS:
            dynamic_vars.extend(STATIC_SERVICE_VARS[svc])

    # "" provider.tf """""""""""""""""""""""""""""""""""""""""""""""""""""""
    _write_provider_tf(base, project_name, region, owner, catalog)

    # "" networking.tf (VPC) """""""""""""""""""""""""""""""""""""""""""""""
    vpc_hcl = dg.generate_vpc_layer(catalog, project_name, region, owner, services)
    (base / "networking.tf").write_text(vpc_hcl, encoding="utf-8")
    typer.secho("  + networking.tf  [vpc module]", fg=typer.colors.GREEN)

    # "" main.tf (compute) " from catalog templates """""""""""""""""""""""""
    compute_templates = []
    compute_labels    = []

    modules_dir = base / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    # Rendered HCL per compute service (to place in modules/<svc>/main.tf)
    rendered_hcl: dict[str, str] = {}

    for c in compute_list:
        entry    = catalog_services.get(c, {})
        template = entry.get("template")
        if template:
            compute_templates.append(template)
            compute_labels.append(c)
        else:
            result = dg.generate_terraform_dynamically(
                c, entry, project_name, region, owner,
                services, env_names,
            )
            if result:
                hcl, svc_vars = result
                rendered_hcl[c] = hcl
                dynamic_vars.extend(svc_vars)

    # Append ingress add-ons that apply to the current compute targets
    for ingress_svc, i_entry in ingress_svcs.items():
        if ingress_svc not in services:
            continue
        allowed = set(i_entry.get("valid_compute_targets", []))
        if not any(c in allowed for c in compute_list):
            continue
        i_template = i_entry.get("template")
        if i_template:
            compute_templates.append(i_template)
            compute_labels.append(ingress_svc)

    # Render catalog templates -- collect HCL per label so we can split into modules
    if compute_templates:
        rendered_per_label = _render_per_label(jinja_env, compute_templates, ctx, labels=compute_labels)
        for label, hcl in rendered_per_label.items():
            rendered_hcl[label] = hcl

    # Write each compute service into modules/<name>/ and collect module call blocks
    root_main_blocks: list[str] = [
        "# Root main.tf -- calls per-service modules.\n"
        "# Resources live in modules/<name>/main.tf; values come from env/*/terraform.tfvars.\n"
    ]
    for svc, hcl in rendered_hcl.items():
        mod_name = _SVC_TO_MODULE.get(svc, svc.replace("-", "_"))
        svc_var_names = [v["name"] for v in dynamic_vars if v.get("service") == svc]
        _write_module_dir(modules_dir, mod_name, hcl, svc_var_names)
        call = _module_call_block(mod_name, svc_var_names)

        # Wire cross-module connections: pass outputs from upstream modules as inputs
        call = _inject_connection_wiring(call, mod_name, services, connections)
        root_main_blocks.append(call)

    if root_main_blocks:
        root_content = "\n".join(root_main_blocks)
        (base / "main.tf").write_text(root_content, encoding="utf-8")

    # "" iam.tf """"""""""""""""""""""""""""""""""""""""""""""""""""""""""""
    iam_templates = []
    seen_iam      = set()
    for c in compute_list:
        entry = catalog_services.get(c, {})
        for t in entry.get("iam_templates", []):
            if t not in seen_iam:
                seen_iam.add(t)
                iam_templates.append(t)

    iam_blocks = []
    for t in iam_templates:
        try:
            iam_blocks.append(jinja_env.get_template(t).render(**ctx))
            typer.secho(f"  + iam.tf  [{t}]", fg=typer.colors.GREEN)
        except Exception as e:
            typer.secho(f"  ! failed IAM template {t}: {e}", fg=typer.colors.YELLOW)

    connected_data_services = [
        s for s in data_stores
        if f"{compute_target}->{s}" in connections or any(
            f"{c}->{s}" in connections for c in compute_list
        )
    ]
    if connected_data_services:
        dynamic_iam = dg.generate_iam_policy_block(
            catalog=catalog,
            compute_service=compute_target,
            connected_services=connected_data_services,
            project_name=project_name,
            region=region,
        )
        if dynamic_iam:
            iam_blocks.append(dynamic_iam)
            typer.secho(
                f"  + iam.tf  [dynamic policy: {', '.join(connected_data_services)}]",
                fg=typer.colors.GREEN,
            )

    if iam_blocks:
        (base / "iam.tf").write_text("\n".join(iam_blocks) + "\n", encoding="utf-8")

    # "" Per-service modules (each service → modules/<svc>/{main,variables,outputs}.tf) ""
    # Root main.tf gets a module call block for each service.
    modules_dir = base / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)

    service_module_calls: list[str] = []   # collected → appended to root main.tf
    wrote_any_service = False

    for svc in other_services:
        if svc in ingress_keys:
            continue

        entry    = catalog_services.get(svc, {})
        template = entry.get("template")

        if template:
            extra_vars = entry.get("extra_vars", {})
            merged     = {**ctx, **extra_vars}
            try:
                hcl = jinja_env.get_template(template).render(**merged)
                _write_service_module(modules_dir, svc, hcl)
                service_module_calls.append(_service_module_call(svc))
                wrote_any_service = True
            except Exception as e:
                typer.secho(f"  ! failed {svc} ({template}): {e}", fg=typer.colors.YELLOW)
        else:
            fallback_entry = entry if svc in catalog_services else {
                "terraform_resource": f"aws_{svc.replace('-', '_')}",
                "category": "unknown",
                "iam_actions": [],
            }
            if svc not in catalog_services:
                typer.secho(
                    f"  ~ '{svc}' not in catalog -- attempting AI generation...",
                    fg=typer.colors.BLUE,
                )
            result = dg.generate_terraform_dynamically(
                svc, fallback_entry, project_name, region, owner,
                services, env_names,
            )
            if result:
                hcl, svc_vars = result
                _write_service_module(modules_dir, svc, hcl)
                service_module_calls.append(_service_module_call(svc))
                typer.secho(f"  + modules/{svc.replace('-','_')}/  [ai-generated]",
                            fg=typer.colors.CYAN)
                dynamic_vars.extend(svc_vars)
                wrote_any_service = True

        # Inject scalar sizing vars for this service (kms_deletion_window_days etc.)
        _SCALAR_SVC_VARS: dict[str, list[dict]] = {
            "kms": [
                {"name": "kms_deletion_window_days", "type": "number",
                 "description": "KMS key deletion window in days (7-30)",
                 "dev": 7, "staging": 14, "prod": 30},
            ],
        }
        if svc in _SCALAR_SVC_VARS:
            for v in _SCALAR_SVC_VARS[svc]:
                if not any(d["name"] == v["name"] for d in dynamic_vars):
                    dynamic_vars.append(v)

    # Auto-add kms module when KMS is needed but not explicitly listed
    if wrote_any_service and "kms" not in services:
        kms_entry    = catalog_services.get("kms", {})
        kms_template = kms_entry.get("template")
        if kms_template:
            try:
                hcl = jinja_env.get_template(kms_template).render(**ctx)
                _write_service_module(modules_dir, "kms", hcl)
                service_module_calls.append(_service_module_call("kms"))
                typer.secho("  + modules/kms/  [auto-added]", fg=typer.colors.GREEN)
            except Exception as e:
                typer.secho(f"  ! failed kms: {e}", fg=typer.colors.YELLOW)

    # Append service module calls to root main.tf
    if service_module_calls:
        main_tf_path = base / "main.tf"
        existing = main_tf_path.read_text(encoding="utf-8") if main_tf_path.exists() else ""
        separator = "\n# ── Service Modules ─────────────────────────────────────────────────────────\n\n"
        main_tf_path.write_text(
            existing.rstrip() + "\n" + separator + "\n".join(service_module_calls),
            encoding="utf-8",
        )

    # "" observability.tf """"""""""""""""""""""""""""""""""""""""""""""""""
    _render(jinja_env, "iac/observability.tf.j2", base / "observability.tf", ctx)

    # "" output.tf """""""""""""""""""""""""""""""""""""""""""""""""""""""""
    _render(jinja_env, "iac/outputs.tf.j2", base / "output.tf", ctx)

    # "" variables.tf " declarations + map(object) types """"""""""""""""""""
    _write_variables_tf(base, dynamic_vars, services)

    # "" locals.tf " cross-module ARN resolution """""""""""""""""""""""""""""
    _write_locals_tf(base, project_name, services, connections)

    # "" env/{env}/ " backend.tf, terraform.tfvars, terraform.tfvars.example
    _write_env_files(base, project_name, region, owner, environments or {}, dynamic_vars, services)

    # "" CI/CD pipeline """"""""""""""""""""""""""""""""""""""""""""""""""""
    _render(cicd_env, "cicd/pipeline.yml.j2", base / "cicd/pipeline.yml", ctx)

    # "" Secrets policy """"""""""""""""""""""""""""""""""""""""""""""""""""
    _write_secrets_policy(base, project_name, data_stores)

    # "" .gitignore """"""""""""""""""""""""""""""""""""""""""""""""""""""""
    _write_gitignore(base)

    typer.secho("\n> Scaffold complete.", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"  Output: {base.absolute()}")


# -----------------------------------------------------------------------------
# modules/ scaffold
# -----------------------------------------------------------------------------

# Variables that each known module exposes to the root (for module call wiring).
# Format: var_name -> (type, description, rhs_in_root_call)
# rhs_in_root_call is the Terraform expression used in the root main.tf module call.
_MODULE_VARS: dict[str, list[tuple[str, str, str]]] = {
    "lambda": [
        ("name_prefix",          "string",      '"${var.project_name}-${var.environment}"'),
        # Deployment package
        ("lambda_s3_bucket",     "string",      "var.lambda_s3_bucket"),
        ("lambda_s3_key",        "string",      "var.lambda_s3_key"),
        # Sizing (from STATIC_SERVICE_VARS, passed through)
        ("lambda_memory_size",   "number",      "var.lambda_memory_size"),
        ("lambda_timeout",       "number",      "var.lambda_timeout"),
        # Cross-module deps — live in root iam.tf / data.tf
        ("lambda_exec_role_arn",    "string",      "aws_iam_role.lambda_exec.arn"),
        ("kms_key_arn",             "string",      "try(module.kms.key_arn, null)"),
        ("log_retention_days",      "number",      "var.log_retention_days"),
        # Optional service module outputs passed in when those services are present
        ("secrets_manager_arn",     "string",      'try(module.secrets_manager.secret_arn, null)'),
        ("sns_topic_arn",           "string",      'try(module.sns.topic_arn, null)'),
        ("sqs_queue_url",           "string",      'try(module.sqs.queue_url, null)'),
        # Universal
        ("environment",             "string",      "var.environment"),
        ("region",                  "string",      "var.region"),
        ("cost_centre",             "string",      "var.cost_centre"),
        ("tags",                    "map(string)", "local.common_tags"),
    ],
    "eks": [
        ("name_prefix",           "string",       '"${var.project_name}-${var.environment}"'),
        # Sizing
        ("eks_node_count",        "number",       "var.eks_node_count"),
        ("eks_instance_type",     "string",       "var.eks_instance_type"),
        ("eks_cluster_version",   "string",       "var.eks_cluster_version"),
        # Cross-module deps — live in root iam.tf
        ("eks_cluster_role_arn",  "string",       "aws_iam_role.eks_cluster.arn"),
        ("eks_node_role_arn",     "string",       "aws_iam_role.eks_node.arn"),
        # Cross-module deps — live in root networking.tf (module.vpc)
        ("subnet_private_ids",    "list(string)", "module.vpc.private_subnets"),
        ("subnet_public_ids",     "list(string)", "module.vpc.public_subnets"),
        ("security_group_id",     "string",       "aws_security_group.app.id"),
        # Universal
        ("environment",           "string",       "var.environment"),
        ("region",                "string",       "var.region"),
        ("cost_centre",           "string",       "var.cost_centre"),
        ("tags",                  "map(string)",  "local.common_tags"),
    ],
    "ecs": [
        ("name_prefix",   "string",      '"${var.project_name}-${var.environment}"'),
        ("environment",   "string",      "var.environment"),
        ("region",        "string",      "var.region"),
        ("cost_centre",   "string",      "var.cost_centre"),
        ("tags",          "map(string)", "local.common_tags"),
    ],
    "rds": [
        ("name_prefix",   "string",      '"${var.project_name}-${var.environment}"'),
        ("db_name",       "string",      "var.db_name"),
        ("db_username",   "string",      "var.db_username"),
        ("environment",   "string",      "var.environment"),
        ("region",        "string",      "var.region"),
        ("cost_centre",   "string",      "var.cost_centre"),
        ("tags",          "map(string)", "local.common_tags"),
    ],
}

# Modules that depend on root-level IAM policy attachments being applied first.
# These become depends_on blocks in the root main.tf module call.
_MODULE_DEPENDS_ON: dict[str, list[str]] = {
    "lambda": [
        "aws_iam_role_policy_attachment.lambda_basic",
    ],
    "eks": [
        "aws_iam_role_policy_attachment.eks_cluster_policy",
        "aws_iam_role_policy_attachment.eks_worker_node",
        "aws_iam_role_policy_attachment.eks_cni",
        "aws_iam_role_policy_attachment.eks_ecr_read",
    ],
}

# Map service name -> module folder name
_SVC_TO_MODULE: dict[str, str] = {
    "lambda":      "lambda",
    "eks":         "eks",
    "ecs-fargate": "ecs",
    "rds":         "rds",
    "aurora":      "rds",
}


def _write_module_dir(
    modules_dir: Path,
    mod_name: str,
    resource_hcl: str,
    svc_var_names: list[str],
) -> None:
    """Write modules/<mod_name>/{main.tf, variables.tf, outputs.tf}."""
    mod_dir = modules_dir / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    # main.tf -- actual resource definitions (moved from root main.tf)
    header = (
        f'# Module: {mod_name}\n'
        f'# Called from root main.tf via: module "{mod_name}" {{ source = "./modules/{mod_name}" }}\n'
        f'# Ref: https://registry.terraform.io/browse/modules?provider=aws\n\n'
    )
    (mod_dir / "main.tf").write_text(header + resource_hcl, encoding="utf-8")

    # variables.tf -- one variable block per input the module accepts
    # Include ALL vars defined in _MODULE_VARS (full module interface) plus any
    # extra dynamic vars. Use ordered-dict trick to preserve declaration order.
    known = {v[0]: v for v in _MODULE_VARS.get(mod_name, [])}
    seen: set[str] = set()
    all_vars: list[str] = []
    for vname in list(known.keys()) + svc_var_names:
        if vname not in seen:
            seen.add(vname)
            all_vars.append(vname)

    # Vars that can legally be null (passed as try(..., null) from root)
    nullable_vars = {
        "kms_key_arn", "security_group_id",
        "secrets_manager_arn", "sns_topic_arn", "sqs_queue_url",
    }

    var_blocks: list[str] = []
    for vname in all_vars:
        if vname in known:
            _, vtype, _ = known[vname]
        else:
            vtype = "string"
        if vname in nullable_vars:
            var_blocks.append(
                f'variable "{vname}" {{\n'
                f'  type    = {vtype}\n'
                f'  default = null\n'
                f'}}\n'
            )
        else:
            var_blocks.append(
                f'variable "{vname}" {{\n'
                f'  type = {vtype}\n'
                f'}}\n'
            )

    (mod_dir / "variables.tf").write_text("\n".join(var_blocks), encoding="utf-8")

    # outputs.tf -- re-export key attributes so root module can reference them
    outputs = _default_module_outputs(mod_name)
    (mod_dir / "outputs.tf").write_text(outputs, encoding="utf-8")

    typer.secho(f"  + modules/{mod_name}/  [main.tf, variables.tf, outputs.tf]",
                fg=typer.colors.CYAN)


def _default_module_outputs(mod_name: str) -> str:
    templates = {
        "lambda": (
            'output "function_name" {\n'
            '  description = "Lambda function name"\n'
            '  value       = aws_lambda_function.app.function_name\n'
            '}\n\n'
            'output "function_arn" {\n'
            '  description = "Lambda function ARN"\n'
            '  value       = aws_lambda_function.app.arn\n'
            '}\n\n'
            'output "invoke_arn" {\n'
            '  description = "Lambda invoke ARN (used by API Gateway integration)"\n'
            '  value       = aws_lambda_function.app.invoke_arn\n'
            '}\n'
        ),
        "eks": (
            'output "cluster_name" {\n'
            '  description = "EKS cluster name (use with kubectl and aws eks update-kubeconfig)"\n'
            '  value       = aws_eks_cluster.main.name\n'
            '}\n\n'
            'output "cluster_endpoint" {\n'
            '  description = "EKS API server endpoint"\n'
            '  value       = aws_eks_cluster.main.endpoint\n'
            '}\n\n'
            'output "cluster_ca" {\n'
            '  description = "EKS cluster certificate authority (base64)"\n'
            '  value       = aws_eks_cluster.main.certificate_authority[0].data\n'
            '  sensitive   = true\n'
            '}\n\n'
            'output "node_group_name" {\n'
            '  description = "EKS managed node group name"\n'
            '  value       = aws_eks_node_group.main.node_group_name\n'
            '}\n'
        ),
        "ecs": (
            'output "cluster_id" {\n'
            '  value = aws_ecs_cluster.main.id\n'
            '}\n\n'
            'output "cluster_name" {\n'
            '  value = aws_ecs_cluster.main.name\n'
            '}\n'
        ),
        "rds": (
            'output "db_endpoint" {\n'
            '  value     = aws_db_instance.main.endpoint\n'
            '  sensitive = true\n'
            '}\n\n'
            'output "db_name" {\n'
            '  value = aws_db_instance.main.db_name\n'
            '}\n'
        ),
    }
    return templates.get(
        mod_name,
        f'# Add outputs that root main.tf needs from the {mod_name} module.\n',
    )


def _inject_connection_wiring(call_block: str, mod_name: str,
                              services: list[str], connections: list[str]) -> str:
    """
    Append cross-module output references to a module call block based on
    the connections declared in infra.yaml.

    For example, if connections contains "api-gateway->lambda", we add:
      lambda_invoke_arn    = module.lambda.invoke_arn
      lambda_function_name = module.lambda.function_name
    to the api_gateway module call.

    Also wires cognito outputs into api_gateway when cognito is in services.
    """
    # Map: (upstream_svc, downstream_mod) -> extra lines to inject before closing }
    WIRE_RULES: list[tuple[str, str, list[str]]] = [
        # api-gateway needs lambda outputs when api-gateway->lambda connection exists
        ("lambda", "api_gateway", [
            '  lambda_invoke_arn     = module.lambda.invoke_arn',
            '  lambda_function_name  = module.lambda.function_name',
        ]),
        # eks needs ecr output when eks->ecr connection exists
        ("ecr", "eks", [
            '  # ecr_repository_url available at: data.tf output ecr_repository_url',
        ]),
    ]

    # Cognito wiring: if cognito is in services, api_gateway needs its outputs
    STATIC_WIRES: list[tuple[str, list[str]]] = [
        ("api_gateway", "cognito", [
            '  cognito_client_id     = aws_cognito_user_pool_client.app.id',
            '  cognito_issuer_url    = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"',
        ]),
    ]

    extra_lines: list[str] = []

    for upstream_svc, downstream_mod, lines in WIRE_RULES:
        if mod_name != downstream_mod:
            continue
        # Check if the connection exists in infra.yaml connections
        conn_exists = any(
            upstream_svc in c and mod_name.replace("_", "-") in c
            for c in connections
        ) or upstream_svc in services
        if conn_exists:
            extra_lines.extend(lines)

    for target_mod, dep_svc, lines in STATIC_WIRES:
        if mod_name == target_mod and dep_svc in services:
            extra_lines.extend(lines)

    if not extra_lines:
        return call_block

    # Insert extra_lines before the closing }
    block_lines = call_block.rstrip().split("\n")
    return "\n".join(block_lines[:-1] + extra_lines + [block_lines[-1]]) + "\n"


def _module_call_block(mod_name: str, svc_var_names: list[str]) -> str:
    """Generate the module {} call block for root main.tf."""
    known = {v[0]: v for v in _MODULE_VARS.get(mod_name, [])}
    # Include ALL known module vars (in declaration order) + any extra dynamic vars
    seen: set[str] = set()
    all_vars: list[str] = []
    for vname in list(known.keys()) + svc_var_names:
        if vname not in seen:
            seen.add(vname)
            all_vars.append(vname)

    lines = [
        f'module "{mod_name}" {{',
        f'  source = "./modules/{mod_name}"',
        '',
    ]
    for vname in all_vars:
        if vname in known:
            _, _, rhs = known[vname]
        else:
            rhs = f"var.{vname}"
        lines.append(f"  {vname:<26} = {rhs}")

    # Add depends_on for root-level IAM policy attachments this module needs
    deps = _MODULE_DEPENDS_ON.get(mod_name, [])
    if deps:
        lines.append('')
        lines.append('  depends_on = [')
        for d in deps:
            lines.append(f'    {d},')
        lines.append('  ]')

    lines.append("}")
    return "\n".join(lines) + "\n"


# =============================================================================
# map(object) module pattern — matches terraform_templates reference
# Each service module accepts a typed map variable so adding resources only
# requires editing tfvars, never the module code itself.
# =============================================================================

# Static HCL for each service module's main.tf (for_each pattern)
_MODULE_MAIN_HCL: dict[str, str] = {
    "sqs": '''\
resource "aws_sqs_queue" "queues" {
  for_each = var.sqs_queues

  name                       = each.value.name
  visibility_timeout_seconds = each.value.visibility_timeout_seconds
  message_retention_seconds  = each.value.message_retention_seconds
  max_message_size           = lookup(each.value, "max_message_size", 1048576)
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.value.dlq_key].arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, { Name = each.value.name })
}

resource "aws_sqs_queue_redrive_allow_policy" "queues" {
  for_each = var.sqs_queues

  queue_url = aws_sqs_queue.dlq[each.value.dlq_key].id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.queues[each.key].arn]
  })
}

resource "aws_sqs_queue" "dlq" {
  for_each = var.dlq_queues

  name                      = each.value.name
  message_retention_seconds = each.value.message_retention_seconds
  sqs_managed_sse_enabled   = true

  tags = merge(var.tags, { Name = each.value.name })
}
''',

    "sns": '''\
resource "aws_sns_topic" "this" {
  name              = var.sns.name
  kms_master_key_id = var.kms_key_arn

  tags = merge(var.tags, { Name = var.sns.name })
}

resource "aws_sns_topic_subscription" "this" {
  for_each = var.sns.subscriptions

  topic_arn = aws_sns_topic.this.arn
  protocol  = each.value.protocol
  endpoint  = each.value.endpoint
}

resource "aws_sns_topic_policy" "this" {
  arn = aws_sns_topic.this.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowLambdaPublish"
        Effect = "Allow"
        Principal = { Service = "lambda.amazonaws.com" }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.this.arn
      },
      {
        Sid    = "AllowEventBridgePublish"
        Effect = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.this.arn
      }
    ]
  })
}
''',

    "kms": '''\
resource "aws_kms_key" "main" {
  description             = var.description
  deletion_window_in_days = var.deletion_window_in_days
  enable_key_rotation     = true
  key_usage               = "ENCRYPT_DECRYPT"
  multi_region            = false

  tags = merge(var.tags, { Name = var.description })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.key_alias}"
  target_key_id = aws_kms_key.main.key_id
}
''',

    "ecr": '''\
resource "aws_ecr_repository" "repos" {
  for_each = var.ecr_repositories

  name                 = each.value.name
  image_tag_mutability = each.value.image_tag_mutability

  image_scanning_configuration {
    scan_on_push = each.value.scan_on_push
  }

  encryption_configuration {
    encryption_type = "KMS"
  }

  tags = merge(var.tags, { Name = each.value.name })
}

resource "aws_ecr_lifecycle_policy" "repos" {
  for_each   = var.ecr_repositories
  repository = aws_ecr_repository.repos[each.key].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 30 tagged images"
        selection    = { tagStatus = "tagged", tagPrefixList = ["v"], countType = "imageCountMoreThan", countNumber = 30 }
        action       = { type = "expire" }
      }
    ]
  })
}
''',

    "eventbridge": '''\
locals {
  # Resolve lambda_key -> lambda ARN and scheduler role ARN at runtime
  eventbridge_schedules_resolved = {
    for k, sched in var.eventbridge_schedules :
    k => merge(sched, {
      lambda_arn = var.lambda_arns[sched.lambda_key]
      role_arn   = var.scheduler_role_arn
    })
  }
}

resource "aws_scheduler_schedule" "this" {
  for_each = local.eventbridge_schedules_resolved

  name        = each.value.name
  description = lookup(each.value, "description", null)

  schedule_expression          = each.value.schedule_expression
  schedule_expression_timezone = each.value.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = each.value.lambda_arn
    role_arn = each.value.role_arn

    retry_policy {
      maximum_retry_attempts = lookup(each.value, "retry_attempts", 0)
    }
  }
}

resource "aws_cloudwatch_event_rule" "ecr_push" {
  count = var.ecr_push_rule != null ? 1 : 0

  name        = var.ecr_push_rule.name
  description = "Trigger deployment pipeline on ECR image PUSH"
  state       = "ENABLED"

  event_pattern = jsonencode({
    source        = ["aws.ecr"]
    "detail-type" = ["ECR Image Action"]
    detail = {
      "action-type"     = ["PUSH"]
      result            = ["SUCCESS"]
      "repository-name" = [{ prefix = var.ecr_push_rule.repo_prefix }]
    }
  })

  tags = merge(var.tags, { Name = var.ecr_push_rule.name })
}

resource "aws_cloudwatch_event_target" "lambda" {
  count     = var.ecr_push_rule != null && var.lambda_arns != null ? 1 : 0
  rule      = aws_cloudwatch_event_rule.ecr_push[0].name
  target_id = "lambda-target"
  arn       = values(var.lambda_arns)[0]
}

resource "aws_lambda_permission" "eventbridge" {
  count         = var.ecr_push_rule != null && var.lambda_arns != null ? 1 : 0
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = values(var.lambda_function_names)[0]
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ecr_push[0].arn
}
''',

    "secrets-manager": '''\
resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name                    = each.value.name
  description             = lookup(each.value, "description", null)
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = lookup(each.value, "recovery_window_in_days", 7)

  tags = merge(var.tags, { Name = each.value.name })
}
''',

    "cloudwatch": '''\
resource "aws_cloudwatch_log_group" "lambdas" {
  for_each = var.lambda_log_groups

  name              = each.value.name
  retention_in_days = lookup(each.value, "retention_in_days", var.log_retention_days)

  tags = merge(var.tags, { Name = each.value.name })
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = {
    for k, v in var.cloudwatch_alarms.lambdas : k => v if lookup(v, "enabled", true)
  }

  alarm_name          = "${each.value.name}_errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = each.value.threshold

  dimensions = { FunctionName = each.value.name }

  alarm_actions = [var.sns_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "sqs_backlog" {
  for_each = {
    for k, v in var.cloudwatch_alarms.sqs : k => v if lookup(v, "enabled", true)
  }

  alarm_name          = "${each.value.name}_backlog"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Average"
  threshold           = each.value.threshold

  dimensions = { QueueName = each.value.name }

  alarm_actions = [var.sns_topic_arn]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.dashboard.name
  dashboard_body = jsonencode(var.dashboard.body)
}
''',
}

# variables.tf content for each service module (map(object) typed)
_MODULE_VARS_TF: dict[str, str] = {
    "sqs": '''\
variable "sqs_queues" {
  description = "Map of SQS queues. Adding a new queue = add one block here, no code change."
  type = map(object({
    name                       = string
    message_retention_seconds  = number
    max_message_size           = optional(number, 1048576)
    visibility_timeout_seconds = number
    dlq_key                    = string
  }))
  default = {}
}

variable "dlq_queues" {
  description = "Map of Dead Letter Queues paired with sqs_queues entries."
  type = map(object({
    name                      = string
    message_retention_seconds = number
  }))
  default = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "sns": '''\
variable "sns" {
  description = "SNS topic configuration with subscriptions map."
  type = object({
    name = string
    subscriptions = map(object({
      protocol = string
      endpoint = string
    }))
  })
}

variable "kms_key_arn" {
  description = "KMS key ARN for SNS topic encryption. null = AWS-managed key."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "kms": '''\
variable "description" {
  description = "KMS key description (also used as Name tag)."
  type        = string
}

variable "key_alias" {
  description = "KMS alias name (without alias/ prefix)."
  type        = string
}

variable "deletion_window_in_days" {
  description = "Days before permanent key deletion (7-30). Use 7 for non-prod."
  type        = number
  default     = 7
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "ecr": '''\
variable "ecr_repositories" {
  description = "Map of ECR repositories. Add a new repo by adding one block here."
  type = map(object({
    name                 = string
    image_tag_mutability = optional(string, "IMMUTABLE")
    scan_on_push         = optional(bool, true)
  }))
  default = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "eventbridge": '''\
variable "eventbridge_schedules" {
  description = "Map of EventBridge scheduler schedules. lambda_key must match a key in lambda_arns."
  type = map(object({
    name                = string
    description         = optional(string)
    schedule_expression = string
    timezone            = string
    lambda_key          = string
    retry_attempts      = optional(number, 0)
  }))
  default = {}
}

variable "lambda_arns" {
  description = "Map of lambda_key -> function ARN. Populated from module.lambda outputs."
  type        = map(string)
  default     = {}
}

variable "lambda_function_names" {
  description = "Map of lambda_key -> function name."
  type        = map(string)
  default     = {}
}

variable "scheduler_role_arn" {
  description = "IAM role ARN that EventBridge Scheduler uses to invoke Lambda."
  type        = string
  default     = null
}

variable "ecr_push_rule" {
  description = "If set, creates an EventBridge rule triggered by ECR image pushes."
  type = object({
    name        = string
    repo_prefix = string
  })
  default = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "secrets-manager": '''\
variable "secrets" {
  description = "Map of secrets to create. Add a new secret by adding one block here."
  type = map(object({
    name                    = string
    description             = optional(string)
    recovery_window_in_days = optional(number, 7)
  }))
  default = {}
}

variable "kms_key_arn" {
  description = "KMS key ARN used to encrypt secrets. null = AWS-managed key."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',

    "cloudwatch": '''\
variable "log_retention_days" {
  description = "Default CloudWatch log retention in days."
  type        = number
  default     = 30
}

variable "sns_topic_arn" {
  description = "SNS topic ARN that receives alarm notifications."
  type        = string
  default     = null
}

variable "lambda_log_groups" {
  description = "Map of Lambda log groups to create."
  type = map(object({
    name              = string
    retention_in_days = optional(number)
  }))
  default = {}
}

variable "cloudwatch_alarms" {
  description = "Alarm definitions for Lambdas, SQS queues, and DLQs."
  type = object({
    lambdas = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
    sqs     = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
    dlq     = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
  })
  default = { lambdas = {}, sqs = {}, dlq = {} }
}

variable "dashboard" {
  description = "CloudWatch dashboard config."
  type = object({
    name = string
    body = any
  })
  default = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
''',
}

# outputs.tf content for each service module
_MODULE_OUTPUTS_TF: dict[str, str] = {
    "sqs": '''\
output "queues" {
  description = "Map of SQS queues — keys match sqs_queues input. Each value has url and arn."
  value = {
    for k, q in aws_sqs_queue.queues : k => {
      url = q.id
      arn = q.arn
    }
  }
}

output "dlqs" {
  description = "Map of DLQs — keys match dlq_queues input."
  value = {
    for k, q in aws_sqs_queue.dlq : k => {
      url = q.id
      arn = q.arn
    }
  }
}
''',

    "sns": '''\
output "topic_arn" {
  description = "SNS topic ARN — pass to Lambda/EventBridge to publish notifications."
  value       = aws_sns_topic.this.arn
}

output "topic_name" {
  description = "SNS topic name."
  value       = aws_sns_topic.this.name
}
''',

    "kms": '''\
output "key_arn" {
  description = "KMS key ARN — pass to SQS, SNS, Secrets Manager as kms_key_arn."
  value       = aws_kms_key.main.arn
}

output "key_id" {
  description = "KMS key ID."
  value       = aws_kms_key.main.key_id
}

output "alias_arn" {
  description = "KMS alias ARN."
  value       = aws_kms_alias.main.arn
}
''',

    "ecr": '''\
output "repositories" {
  description = "Map of ECR repositories — keys match ecr_repositories input."
  value = {
    for k, r in aws_ecr_repository.repos : k => {
      url = r.repository_url
      arn = r.arn
    }
  }
}
''',

    "eventbridge": '''\
output "schedule_arns" {
  description = "Map of EventBridge schedule ARNs."
  value = {
    for k, s in aws_scheduler_schedule.this : k => s.arn
  }
}

output "ecr_push_rule_arn" {
  description = "ECR push EventBridge rule ARN (null if not configured)."
  value       = length(aws_cloudwatch_event_rule.ecr_push) > 0 ? aws_cloudwatch_event_rule.ecr_push[0].arn : null
}
''',

    "secrets-manager": '''\
output "secrets" {
  description = "Map of secrets — keys match secrets input. Each value has arn and name."
  value = {
    for k, s in aws_secretsmanager_secret.this : k => {
      arn  = s.arn
      name = s.name
    }
  }
}
''',

    "cloudwatch": '''\
output "log_group_names" {
  description = "Map of Lambda log group names."
  value = {
    for k, lg in aws_cloudwatch_log_group.lambdas : k => lg.name
  }
}

output "dashboard_name" {
  description = "CloudWatch dashboard name."
  value       = var.dashboard != null ? aws_cloudwatch_dashboard.main.dashboard_name : null
}
''',
}


def _write_service_module(modules_dir: Path, svc: str, _hcl_unused: str = "") -> None:
    """
    Write modules/<svc>/{main.tf, variables.tf, outputs.tf} using the
    map(object) + for_each pattern matching terraform_templates reference.
    The Jinja2-rendered HCL is replaced by static, reusable module templates.
    """
    mod_name = svc.replace("-", "_")
    mod_dir  = modules_dir / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    header = (
        f'# Module: {svc}\n'
        f'# source = "./modules/{mod_name}"\n'
        f'# To reuse: change source to a Git URL or Terraform Registry path.\n'
        f'# Add new resources by editing tfvars only — no module code changes needed.\n\n'
    )

    main_hcl   = _MODULE_MAIN_HCL.get(svc, f'# TODO: add {svc} resources here\n')
    vars_hcl   = _MODULE_VARS_TF.get(svc, 'variable "tags" { type = map(string)\n  default = {} }\n')
    output_hcl = _MODULE_OUTPUTS_TF.get(svc, f'# Add outputs for the {svc} module.\n')

    (mod_dir / "main.tf").write_text(header + main_hcl,  encoding="utf-8")
    (mod_dir / "variables.tf").write_text(vars_hcl,       encoding="utf-8")
    (mod_dir / "outputs.tf").write_text(output_hcl,       encoding="utf-8")

    typer.secho(f"  + modules/{mod_name}/  [main.tf, variables.tf, outputs.tf]",
                fg=typer.colors.CYAN)


def _service_module_call(svc: str) -> str:
    """
    Generate root main.tf module call block.
    Passes the whole map variable — not individual scalars.
    """
    mod_name = svc.replace("-", "_")
    _CALLS: dict[str, str] = {
        "sqs": (
            f'module "sqs" {{\n'
            f'  source = "./modules/sqs"\n\n'
            f'  sqs_queues = var.sqs_queues\n'
            f'  dlq_queues = var.dlq_queues\n'
            f'  tags       = local.common_tags\n'
            f'}}\n'
        ),
        "sns": (
            f'module "sns" {{\n'
            f'  source = "./modules/sns"\n\n'
            f'  sns         = var.sns\n'
            f'  kms_key_arn = try(module.kms.key_arn, null)\n'
            f'  tags        = local.common_tags\n'
            f'}}\n'
        ),
        "kms": (
            f'module "kms" {{\n'
            f'  source = "./modules/kms"\n\n'
            f'  description             = "${{var.project_name}}-${{var.environment}} CMK"\n'
            f'  key_alias               = "${{var.project_name}}-${{var.environment}}"\n'
            f'  deletion_window_in_days = var.kms_deletion_window_days\n'
            f'  tags                    = local.common_tags\n'
            f'}}\n'
        ),
        "ecr": (
            f'module "ecr" {{\n'
            f'  source = "./modules/ecr"\n\n'
            f'  ecr_repositories = var.ecr_repositories\n'
            f'  tags             = local.common_tags\n'
            f'}}\n'
        ),
        "eventbridge": (
            f'module "eventbridge" {{\n'
            f'  source = "./modules/eventbridge"\n\n'
            f'  eventbridge_schedules = local.eventbridge_schedules\n'
            f'  lambda_arns           = local.lambda_arns\n'
            f'  lambda_function_names = local.lambda_function_names\n'
            f'  scheduler_role_arn    = try(aws_iam_role.lambda_exec.arn, null)\n'
            f'  ecr_push_rule         = var.ecr_push_rule\n'
            f'  tags                  = local.common_tags\n'
            f'}}\n'
        ),
        "secrets-manager": (
            f'module "secrets_manager" {{\n'
            f'  source = "./modules/secrets_manager"\n\n'
            f'  secrets     = var.secrets\n'
            f'  kms_key_arn = try(module.kms.key_arn, null)\n'
            f'  tags        = local.common_tags\n'
            f'}}\n'
        ),
        "cloudwatch": (
            f'module "cloudwatch" {{\n'
            f'  source = "./modules/cloudwatch"\n\n'
            f'  log_retention_days = var.log_retention_days\n'
            f'  sns_topic_arn      = try(module.sns.topic_arn, null)\n'
            f'  lambda_log_groups  = local.lambda_log_groups\n'
            f'  cloudwatch_alarms  = var.cloudwatch_alarms\n'
            f'  dashboard          = var.dashboard\n'
            f'  tags               = local.common_tags\n'
            f'}}\n'
        ),
    }
    return _CALLS.get(svc, (
        f'module "{mod_name}" {{\n'
        f'  source = "./modules/{mod_name}"\n\n'
        f'  tags = local.common_tags\n'
        f'}}\n'
    ))


def _write_modules_scaffold(base: Path, services: list[str]) -> None:
    """Create modules/ directory; actual content is filled by _write_module_dir calls."""
    modules_dir = base / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    # Individual module dirs are written by _write_module_dir when HCL is available.


# -----------------------------------------------------------------------------
# provider.tf writer
# -----------------------------------------------------------------------------

def _write_provider_tf(base: Path, project_name: str, region: str,
                       owner: str, catalog: dict) -> None:
    tf_cfg           = dg.get_terraform_config(catalog)
    tf_version       = tf_cfg.get("required_version", ">= 1.5.0")
    provider_version = tf_cfg.get("aws_provider_version", "~> 6.0")

    content = (
        "# Terraform configuration -- generated by devops-scaffold-tool\n"
        "# Naming convention: {project}-{env}-{resource-type}\n"
        "# Ref: https://registry.terraform.io/browse/modules?provider=aws\n"
        "\n"
        f'terraform {{\n'
        f'  required_version = "{tf_version}"\n'
        f'\n'
        f'  required_providers {{\n'
        f'    aws = {{\n'
        f'      source  = "hashicorp/aws"\n'
        f'      version = "{provider_version}"\n'
        f'    }}\n'
        f'  }}\n'
        f'}}\n'
        f'\n'
        f'provider "aws" {{\n'
        f'  region = var.region\n'
        f'\n'
        f'  default_tags {{\n'
        f'    tags = {{\n'
        f'      Project     = var.project_name\n'
        f'      Owner       = var.owner\n'
        f'      Environment = var.environment\n'
        f'      ManagedBy   = "devops-scaffold-tool"\n'
        f'    }}\n'
        f'  }}\n'
        f'}}\n'
        f'\n'
        f'locals {{\n'
        f'  # Naming prefix: {{project}}-{{env}}-{{resource-type}}\n'
        f'  name_prefix = "${{var.project_name}}-${{var.environment}}"\n'
        f'\n'
        f'  common_tags = {{\n'
        f'    Project     = var.project_name\n'
        f'    Owner       = var.owner\n'
        f'    Environment = var.environment\n'
        f'    ManagedBy   = "devops-scaffold-tool"\n'
        f'  }}\n'
        f'}}\n'
    )
    (base / "provider.tf").write_text(content, encoding="utf-8")
    typer.secho("  + provider.tf", fg=typer.colors.GREEN)


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# variables.tf writer " declarations only, never hardcoded defaults
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _write_variables_tf(base: Path, service_vars: list[dict], services: list[str]) -> None:
    lines = [
        "# ------------------------------------------------------------------------------",
        "# Variable declarations -- generated by devops-scaffold-tool",
        "# Values are set per-environment in env/{env}/terraform.tfvars",
        "# map(object) vars: add resources by editing tfvars only -- no code changes.",
        "# ------------------------------------------------------------------------------",
        "",
    ]

    # Base scalar vars first
    seen: set[str] = set()
    for var in BASE_VARS:
        name = var["name"]
        seen.add(name)
        lines.append(f'variable "{name}" {{')
        lines.append(f'  description = "{var["description"]}"')
        lines.append(f'  type        = {var["type"]}')
        lines.append("}")
        lines.append("")

    # Scalar service vars (eks, lambda sizing)
    for var in service_vars:
        name = var["name"]
        if name in seen:
            continue
        seen.add(name)
        lines.append(f'variable "{name}" {{')
        lines.append(f'  description = "{var["description"]}"')
        lines.append(f'  type        = {var["type"]}')
        lines.append("}")
        lines.append("")

    # map(object) service variables — matching terraform_templates pattern
    _MAP_VARS: dict[str, str] = {
        "sqs": '''\
variable "sqs_queues" {
  description = "Map of SQS queues. Add a queue by adding one block here — no code change needed."
  type = map(object({
    name                       = string
    message_retention_seconds  = number
    max_message_size           = optional(number, 1048576)
    visibility_timeout_seconds = number
    dlq_key                    = string
  }))
  default = {}
}

variable "dlq_queues" {
  description = "Map of Dead Letter Queues paired with sqs_queues entries."
  type = map(object({
    name                      = string
    message_retention_seconds = number
  }))
  default = {}
}
''',
        "sns": '''\
variable "sns" {
  description = "SNS topic configuration with optional subscriptions."
  type = object({
    name = string
    subscriptions = optional(map(object({
      protocol = string
      endpoint = string
    })), {})
  })
  default = null
}
''',
        "kms": '''\
variable "kms_deletion_window_days" {
  description = "KMS key deletion window in days (7-30)."
  type        = number
  default     = 7
}
''',
        "ecr": '''\
variable "ecr_repositories" {
  description = "Map of ECR repositories. Add a repo by adding one block here."
  type = map(object({
    name                 = string
    image_tag_mutability = optional(string, "IMMUTABLE")
    scan_on_push         = optional(bool, true)
  }))
  default = {}
}
''',
        "eventbridge": '''\
variable "eventbridge_schedules" {
  description = "Map of EventBridge Scheduler schedules. lambda_key must match lambda_configs key."
  type = map(object({
    name                = string
    description         = optional(string)
    schedule_expression = string
    timezone            = string
    lambda_key          = string
    retry_attempts      = optional(number, 0)
  }))
  default = {}
}

variable "ecr_push_rule" {
  description = "If set, creates an EventBridge rule triggered on ECR image push."
  type = object({
    name        = string
    repo_prefix = string
  })
  default = null
}
''',
        "secrets-manager": '''\
variable "secrets" {
  description = "Map of Secrets Manager secrets. Add a secret by adding one block here."
  type = map(object({
    name                    = string
    description             = optional(string)
    recovery_window_in_days = optional(number, 7)
  }))
  default = {}
}
''',
        "cloudwatch": '''\
variable "cloudwatch_alarms" {
  description = "CloudWatch alarm definitions per resource type."
  type = object({
    lambdas = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
    sqs     = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
    dlq     = optional(map(object({ name = string, threshold = number, enabled = optional(bool, true) })), {})
  })
  default = { lambdas = {}, sqs = {}, dlq = {} }
}

variable "dashboard" {
  description = "CloudWatch dashboard config."
  type = object({
    name = string
    body = any
  })
  default = null
}
''',
    }

    for svc in services:
        if svc in _MAP_VARS and svc not in seen:
            seen.add(svc)
            lines.append(_MAP_VARS[svc])

    # lambda_configs always added when lambda is present
    if "lambda" in services and "lambda_configs" not in seen:
        seen.add("lambda_configs")
        lines.append('''\
variable "lambda_configs" {
  description = "Map of Lambda functions. Add a function by adding one block here."
  type = map(object({
    function_name         = string
    handler               = string
    runtime               = optional(string, "python3.12")
    timeout               = optional(number, 30)
    memory_size           = optional(number, 512)
    s3_bucket             = optional(string)
    s3_key                = optional(string)
    environment_variables = optional(map(string), {})
    layers                = optional(list(string), [])
    sqs_trigger = optional(object({
      queue      = string
      batch_size = number
    }))
  }))
  default = {}
}
''')

    (base / "variables.tf").write_text("\n".join(lines), encoding="utf-8")
    typer.secho("  + variables.tf  [declarations only -- no defaults]", fg=typer.colors.GREEN)


def _write_locals_tf(
    base: Path,
    project_name: str,
    services: list[str],
    connections: list[str],
) -> None:
    """
    Generate locals.tf — transforms raw tfvars into resolved values for module calls.
    Mirrors the locals.tf pattern in terraform_templates: resolves lambda_key -> ARN,
    builds log group names, etc.
    """
    blocks: list[str] = [
        "# locals.tf — generated by devops-scaffold-tool",
        "# Transforms raw tfvars into resolved values passed to modules.",
        "# Cross-module ARN references are resolved here, not in module code.",
        "",
    ]

    if "lambda" in services:
        blocks.append('''\
locals {
  # Apply defaults to each lambda config entry
  lambda_configs = {
    for key, cfg in var.lambda_configs :
    key => merge({
      runtime     = "python3.12"
      timeout     = 30
      memory_size = 512
      layers      = []
      environment_variables = {}
    }, cfg)
  }

  # Convenience maps used by eventbridge and cloudwatch modules
  lambda_arns = {
    for key, fn in module.lambda : key => fn.function_arn
  }

  lambda_function_names = {
    for key, fn in module.lambda : key => fn.function_name
  }

  # CloudWatch log group per Lambda function
  lambda_log_groups = {
    for key, cfg in local.lambda_configs :
    key => {
      name              = "/aws/lambda/${cfg.function_name}"
      retention_in_days = var.log_retention_days
    }
  }
}
''')

    if "eventbridge" in services:
        blocks.append('''\
locals {
  # Resolve lambda_key -> ARN for EventBridge schedules
  eventbridge_schedules = {
    for key, sched in var.eventbridge_schedules :
    key => sched
    # lambda_arn is resolved inside the eventbridge module using lambda_arns map
  }
}
''')

    if "sqs" in services and "cloudwatch" in services:
        blocks.append('''\
locals {
  # CloudWatch alarms for SQS queues — auto-built from sqs_queues map
  sqs_alarm_targets = {
    for k, q in var.sqs_queues : k => {
      name      = q.name
      threshold = 100
      enabled   = true
    }
  }

  dlq_alarm_targets = {
    for k, q in var.dlq_queues : k => {
      name      = q.name
      threshold = 1
      enabled   = true
    }
  }
}
''')

    (base / "locals.tf").write_text("\n".join(blocks), encoding="utf-8")
    typer.secho("  + locals.tf  [cross-module ARN resolution]", fg=typer.colors.GREEN)


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# env/{env}/ writer
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _write_env_files(
    base: Path,
    project_name: str,
    region: str,
    owner: str,
    environments: dict,
    service_vars: list[dict],
    services: list[str],
) -> None:
    env_names = list(environments.keys()) if environments else ["dev", "staging", "prod"]

    for env_name in env_names:
        env_dir = base / "env" / env_name
        env_dir.mkdir(parents=True, exist_ok=True)

        # "" backend.tf """""""""""""""""""""""""""""""""""""""""""""""""""
        backend_key = f"{env_name}/terraform.tfstate"
        (env_dir / "backend.tf").write_text(
            f'terraform {{\n'
            f'  backend "s3" {{\n'
            f'    bucket         = "REPLACE_WITH_STATE_BUCKET"\n'
            f'    key            = "{backend_key}"\n'
            f'    region         = "{region}"\n'
            f'    dynamodb_table = "REPLACE_WITH_LOCK_TABLE"\n'
            f'    encrypt        = true\n'
            f'  }}\n'
            f'}}\n',
            encoding="utf-8",
        )

        env_cfg = environments.get(env_name, {})

        # Resolve scalar per-env values (eks sizing, lambda sizing)
        scalar_lines: list[str] = []
        seen_names: set[str] = set()
        for var in service_vars:
            name = var["name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            val = _env_override(name, env_cfg) or dg._env_value_for(var, env_name)
            scalar_lines.append(_format_tfvar(name, val))

        # Resolve per-env aliases (uat->staging, live->prod) for object defaults
        _env_alias = env_name
        if env_name in ("uat", "qa", "test"):
            _env_alias = "staging"
        elif env_name in ("live", "production"):
            _env_alias = "prod"
        _is_prod = _env_alias == "prod"

        # Build map(object) blocks
        obj_blocks: list[str] = []

        if "lambda" in services:
            lambda_cfg = env_cfg.get("lambda", {})
            mem   = lambda_cfg.get("memory_mb", 512 if not _is_prod else 1024)
            timo  = lambda_cfg.get("timeout_s", 30)
            obj_blocks.append(f'''\
# Lambda function configurations
# Add a new function by adding one block inside lambda_configs.
lambda_configs = {{
  deploy = {{
    function_name         = "{project_name}-{env_name}-func"
    handler               = "lambda_function.lambda_handler"
    runtime               = "python3.12"
    timeout               = {timo}
    memory_size           = {mem}
    s3_bucket             = "REPLACE_WITH_DEPLOY_BUCKET"
    s3_key                = "lambda/app.zip"
    environment_variables = {{
      ENV          = "{env_name.upper()}"
      PROJECT_NAME = "{project_name}"
    }}
  }}
}}
''')

        if "sqs" in services:
            vis = 60
            ret = 345600 if _is_prod else 86400
            obj_blocks.append(f'''\
# SQS queue configurations
# Add a new queue by adding one block inside sqs_queues (and matching DLQ in dlq_queues).
sqs_queues = {{
  main_queue = {{
    name                       = "{project_name}-{env_name}-queue"
    message_retention_seconds  = {ret}
    max_message_size           = 1048576
    visibility_timeout_seconds = {vis}
    dlq_key                    = "main_dlq"
  }}
}}

dlq_queues = {{
  main_dlq = {{
    name                      = "{project_name}-{env_name}-dlq"
    message_retention_seconds = {1209600 if _is_prod else 345600}
  }}
}}
''')

        if "ecr" in services:
            mut = "IMMUTABLE" if _is_prod else "MUTABLE"
            obj_blocks.append(f'''\
# ECR repository configurations
ecr_repositories = {{
  app = {{
    name                 = "{project_name}-{env_name}-app"
    image_tag_mutability = "{mut}"
    scan_on_push         = {str(_is_prod).lower()}
  }}
}}
''')

        if "secrets-manager" in services:
            rw = 30 if _is_prod else 0
            obj_blocks.append(f'''\
# Secrets Manager configurations
# Add a new secret by adding one block inside secrets.
secrets = {{
  app_secrets = {{
    name                    = "{project_name}/{env_name}/app"
    description             = "Application secrets for {project_name} ({env_name})"
    recovery_window_in_days = {rw}
  }}
}}
''')

        if "eventbridge" in services:
            obj_blocks.append(f'''\
# EventBridge Scheduler configurations
# lambda_key must match a key in lambda_configs above.
eventbridge_schedules = {{
  daily_trigger = {{
    name                = "{project_name}-scheduler-{env_name}"
    description         = "Daily trigger for {project_name} deploy worker"
    schedule_expression = "cron(0 0 * * ? *)"
    timezone            = "UTC"
    lambda_key          = "deploy"
    retry_attempts      = 0
  }}
}}

ecr_push_rule = {{
  name        = "{project_name}-{env_name}-ecr-push"
  repo_prefix = "{project_name}-{env_name}"
}}
''')

        if "sns" in services:
            obj_blocks.append(f'''\
# SNS topic configuration
sns = {{
  name = "{project_name}-{env_name}-notifications"
  subscriptions = {{
    # email = {{
    #   protocol = "email"
    #   endpoint = "team@example.com"
    # }}
  }}
}}
''')

        if "cloudwatch" in services:
            fn_name = f"{project_name}-{env_name}-func"
            q_name  = f"{project_name}-{env_name}-queue"
            d_name  = f"{project_name}-{env_name}-dlq"
            obj_blocks.append(f'''\
# CloudWatch alarms
cloudwatch_alarms = {{
  lambdas = {{
    deploy = {{ name = "{fn_name}", threshold = 1, enabled = true }}
  }}
  sqs = {{
    main_queue = {{ name = "{q_name}", threshold = 100, enabled = true }}
  }}
  dlq = {{
    main_dlq = {{ name = "{d_name}", threshold = 1, enabled = true }}
  }}
}}

dashboard = {{
  name = "{project_name}-{env_name}-dashboard"
  body = {{}}
}}
''')

        # Assemble tfvars
        tfvars_lines = [
            f'# {env_name} environment — generated by devops-scaffold-tool',
            f'# Do NOT commit secrets here. Use the secrets map → AWS Secrets Manager.',
            "",
            f'project_name = "{project_name}"',
            f'region       = "{region}"',
            f'environment  = "{env_name}"',
            f'owner        = "{owner}"',
            f'vpc_cidr     = "10.0.0.0/16"',
            f'cost_centre  = "REPLACE_WITH_COST_CENTRE"',
        ]

        if scalar_lines:
            tfvars_lines += ["", "# ── Scalar sizing variables ──────────────────────────────────"] + scalar_lines

        if obj_blocks:
            tfvars_lines += ["", "# ── Service configurations (map objects) ─────────────────────"]
            for blk in obj_blocks:
                tfvars_lines.append(blk)

        (env_dir / "terraform.tfvars").write_text("\n".join(tfvars_lines) + "\n", encoding="utf-8")

        # terraform.tfvars.example (commented-out copy)
        example_lines = [
            f'# {env_name}.tfvars.example — copy to terraform.tfvars and fill in real values.',
            f'# This file IS committed to source control (no secrets).',
            "",
            f'# project_name = "{project_name}"',
            f'# region       = "{region}"',
            f'# environment  = "{env_name}"',
            f'# owner        = "REPLACE_WITH_OWNER"',
            f'# vpc_cidr     = "10.0.0.0/16"',
            f'# cost_centre  = "REPLACE_WITH_COST_CENTRE"',
        ]
        for line in scalar_lines:
            example_lines.append(f'# {line}')
        for blk in obj_blocks:
            for ln in blk.splitlines():
                example_lines.append(f'# {ln}')

        (env_dir / "terraform.tfvars.example").write_text("\n".join(example_lines) + "\n", encoding="utf-8")

        typer.secho(
            f"  + env/{env_name}/  [backend.tf, terraform.tfvars, terraform.tfvars.example]",
            fg=typer.colors.GREEN,
        )


def _env_override(var_name: str, env_cfg: dict):
    """
    Extract a value from the infra.yaml environments[env] block for a given
    Terraform variable name. Returns None if no override is found.

    Mapping: infra.yaml path -> terraform var name
      eks.node_count     -> eks_node_count
      eks.instance_type  -> eks_instance_type
      lambda.memory_mb   -> lambda_memory_size
      lambda.timeout_s   -> lambda_timeout
    """
    _MAP = {
        "eks_node_count":     ("eks",    "node_count"),
        "eks_instance_type":  ("eks",    "instance_type"),
        "lambda_memory_size": ("lambda", "memory_mb"),
        "lambda_timeout":     ("lambda", "timeout_s"),
    }
    if var_name not in _MAP:
        return None
    svc, key = _MAP[var_name]
    return env_cfg.get(svc, {}).get(key)


def _format_tfvar(name: str, value: Any) -> str:
    """Format a single tfvars line with correct HCL value quoting."""
    if isinstance(value, bool):
        return f"{name} = {str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"{name} = {value}"
    return f'{name} = "{value}"'


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Secrets policy
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _write_secrets_policy(base: Path, project_name: str, data_stores: list) -> None:
    content = (
        "# Secrets structure\n"
        "# Use AWS Secrets Manager or SSM Parameter Store.\n"
        "# NEVER hardcode values -- this file defines paths/structure only.\n\nsecrets:\n"
    )
    if "postgres" in data_stores or "mysql" in data_stores or \
       "aurora-postgres" in data_stores or "aurora-mysql" in data_stores:
        content += (
            f'  - path: "/{project_name}/{{environment}}/db/password"\n'
            f'    service: "AWS Secrets Manager"\n'
            f'    description: "RDS master password -- auto-rotated"\n'
        )
    if "redis" in data_stores or "memcached" in data_stores:
        content += (
            f'  - path: "/{project_name}/{{environment}}/cache/auth-token"\n'
            f'    service: "AWS Secrets Manager"\n'
            f'    description: "ElastiCache auth token"\n'
        )
    if "opensearch" in data_stores:
        content += (
            f'  - path: "/{project_name}/{{environment}}/opensearch/master-password"\n'
            f'    service: "AWS Secrets Manager"\n'
            f'    description: "OpenSearch master user password"\n'
        )
    if "msk" in data_stores:
        content += (
            f'  - path: "/{project_name}/{{environment}}/msk/sasl-password"\n'
            f'    service: "AWS Secrets Manager"\n'
            f'    description: "MSK SASL/SCRAM credentials"\n'
        )
    content += (
        f'  - path: "/{project_name}/{{environment}}/app/secret-key"\n'
        f'    service: "AWS SSM Parameter Store"\n'
        f'    description: "Application secret key"\n'
    )
    (base / "secrets/secrets-policy.yml").write_text(content, encoding="utf-8")


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# .gitignore
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

def _write_gitignore(base: Path) -> None:
    (base / ".gitignore").write_text(
        "# Terraform state\n"
        "*.tfstate\n"
        "*.tfstate.backup\n"
        ".terraform/\n"
        ".terraform.lock.hcl\n\n"
        "# tfvars contain real values -- never commit\n"
        "*.tfvars\n"
        "!*.tfvars.example\n\n"
        "# Cache\n"
        ".tf-cache/\n",
        encoding="utf-8",
    ) 


