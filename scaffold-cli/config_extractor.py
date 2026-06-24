"""
config_extractor.py — extract structured infra config from plain-English descriptions
──────────────────────────────────────────────────────────────────────────────────────
Called when the user passes --describe "..." to the CLI.
Uses ai_client.py for provider-agnostic AI calls (Claude / OpenAI / Gemini).
Returns a flat dict with dotted keys: {"project.type": "backend", ...}
"""

from typing import Any, Optional
import typer
import ai_client as aic

SYSTEM_PROMPT = """You are an infrastructure configuration extractor for cloud-native applications.
Extract structured configuration fields from plain-language project descriptions.
Only populate fields that are clearly stated or strongly implied by the description.
Be conservative — only include fields you are confident about.
Do not guess or infer fields that are not mentioned."""

_TOOL = [
    {
        "name": "extract_infra_config",
        "description": (
            "Extract structured infrastructure configuration from a plain-language "
            "project description. Only include fields that are clearly stated or "
            "strongly implied. Omit fields you cannot determine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": (
                        "Project name (lowercase, hyphens only, max 20 chars). "
                        "Only include if explicitly named."
                    ),
                },
                "project_type": {
                    "type": "string",
                    "enum": ["backend", "frontend", "data-pipeline", "ai-service"],
                    "description": (
                        "backend: REST/GraphQL/BFF service. "
                        "frontend: static site (React, docs, marketing). "
                        "data-pipeline: ETL, batch, streaming. "
                        "ai-service: LLM-backed, RAG, AI-native."
                    ),
                },
                "runtime_language": {
                    "type": "string",
                    "description": "Primary programming language: python, node, go, java, ruby.",
                },
                "runtime_containerised": {
                    "type": "boolean",
                    "description": (
                        "True if Docker, ECS, Fargate, EKS, Kubernetes are mentioned. "
                        "False if Lambda or serverless-only. "
                        "If both Lambda AND EKS are mentioned, set true."
                    ),
                },
                "cloud_region": {
                    "type": "string",
                    "description": "AWS region (e.g. us-east-1). Only include if explicitly mentioned.",
                },
                "team_size": {
                    "type": "string",
                    "enum": ["solo", "small", "medium", "large"],
                    "description": "solo=1, small=2-5, medium=6-15, large=15+.",
                },
                "team_ops_maturity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": (
                        "high: EKS, KMS, advanced services mentioned. "
                        "low: fully managed/serverless only. "
                        "Only include if clearly implied."
                    ),
                },
                "stage": {
                    "type": "string",
                    "enum": ["prototype", "early", "growth", "scale"],
                    "description": (
                        "prototype: no real users. early: first users. "
                        "growth: scaling. scale: high traffic, strict SLAs."
                    ),
                },
                "data_stores": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "postgres", "mysql", "redis", "s3", "dynamodb",
                            "sqs", "eventbridge", "opensearch", "kinesis", "msk",
                        ],
                    },
                    "description": (
                        "postgres: PostgreSQL/RDS/relational. mysql: MySQL. redis: Redis/cache. "
                        "s3: object storage/files. dynamodb: NoSQL. sqs: queues/buffering. "
                        "eventbridge: event routing. opensearch: search. "
                        "kinesis: streaming. msk: Kafka."
                    ),
                },
                "auth_required": {
                    "type": "boolean",
                    "description": "Whether user authentication is needed. Only include if mentioned.",
                },
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "All AWS services / components mentioned. "
                        "Use catalog names where possible: eks, lambda, ecs-fargate, ec2, "
                        "alb, api-gateway, rds, postgres, mysql, redis, s3, dynamodb, "
                        "sqs, eventbridge, cognito, kms, etc."
                    ),
                },
            },
            "required": [],
        },
    }
]

_FIELD_MAP = {
    "project_name":          "project.name",
    "project_type":          "project.type",
    "runtime_language":      "runtime.language",
    "runtime_containerised": "runtime.containerised",
    "cloud_region":          "cloud.region",
    "team_size":             "team.size",
    "team_ops_maturity":     "team.ops_maturity",
    "stage":                 "stage",
    "data_stores":           "data.stores",
    "auth_required":         "auth.required",
    "services":              "services",
}


def extract_config_from_description(description: str) -> dict[str, Any]:
    """
    Call AI to extract structured infra config from a free-text description.
    Returns a flat dict with dotted keys, e.g. {"project.type": "backend", ...}
    Returns {} if the AI provider key is missing or the call fails.
    """
    client = aic.get_client()
    if not client.available:
        typer.secho(
            f"  [!] AI provider not available ({aic.provider_info()}) — "
            "skipping --describe extraction.\n"
            "    Set the API key env var or switch AI_PROVIDER.",
            fg=typer.colors.YELLOW,
        )
        return {}

    typer.secho(
        f"  ~ Extracting config from description via {aic.provider_info()}...",
        fg=typer.colors.BLUE,
    )

    result = client.tool_use(_TOOL, description, system=SYSTEM_PROMPT,
                             tool_name="extract_infra_config")
    if not result:
        typer.secho("  [!] Config extraction returned no result.", fg=typer.colors.YELLOW)
        return {}

    extracted = {
        config_key: result[tool_key]
        for tool_key, config_key in _FIELD_MAP.items()
        if tool_key in result
    }
    typer.secho(
        f"  + Extracted {len(extracted)} fields: {', '.join(extracted.keys())}",
        fg=typer.colors.GREEN,
    )
    return extracted


def merge_extracted_into_config(base: dict, extracted_flat: dict) -> dict:
    """
    Merge a flat extracted config (dotted keys) into the nested config dict.
    Extracted values fill in blanks — they do NOT overwrite values already
    present in infra.yaml (infra.yaml takes precedence).
    """
    for dotted_key, value in extracted_flat.items():
        parts = dotted_key.split(".")
        node  = base
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        leaf = parts[-1]
        if leaf not in node:
            node[leaf] = value
    return base
