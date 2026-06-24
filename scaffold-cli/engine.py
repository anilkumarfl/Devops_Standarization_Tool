import yaml
from pathlib import Path
from typing import Dict, Any, Tuple, List


def flatten_dict(d: dict, parent_key: str = '', sep: str = '.') -> dict:
    """Converts {'team': {'size': 'small'}} to {'team.size': 'small'}"""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def _conditions_match(flat_state: dict, conditions: dict) -> bool:
    for key, expected in conditions.items():
        # Special operator: data.stores.contains
        if key.endswith(".contains"):
            base_key = key[: -len(".contains")]
            actual = flat_state.get(base_key, [])
            if not isinstance(actual, list) or expected not in actual:
                return False
        # Special operator: data.stores.nonempty
        elif key.endswith(".nonempty"):
            base_key = key[: -len(".nonempty")]
            actual = flat_state.get(base_key, [])
            if expected is True and not actual:
                return False
            if expected is False and actual:
                return False
        elif isinstance(expected, list):
            if flat_state.get(key) not in expected:
                return False
        else:
            if flat_state.get(key) != expected:
                return False
    return True


def evaluate_rules(state: dict, rules_yaml_path: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    Evaluates compute rules top-down — first match wins.
    Also evaluates all anti_patterns entries unconditionally.
    Returns the matched 'then' block and any triggered warnings.
    """
    flat_state = flatten_dict(state)

    with open(rules_yaml_path, 'r') as f:
        rule_file = yaml.safe_load(f)

    matched_then: Dict[str, Any] = {}
    warnings: List[str] = []

    for rule in rule_file.get('rules', []):
        if _conditions_match(flat_state, rule.get('when', {})):
            then_block = rule.get('then', {})

            if "reason" not in then_block:
                raise ValueError(f"Rule '{rule['id']}' is invalid: missing 'reason' field.")

            warn_if = then_block.get('warn_if')
            if warn_if:
                warn_key = next((k for k in warn_if if k != 'message'), None)
                if warn_key and flat_state.get(warn_key) == warn_if[warn_key]:
                    warnings.append(warn_if['message'])

            matched_then = then_block
            break

    if not matched_then:
        warnings.append("No matching rule found for this configuration.")

    for ap in rule_file.get('anti_patterns', []):
        if _conditions_match(flat_state, ap.get('when', {})):
            warnings.append(ap['message'])

    return matched_then, warnings


def resolve_templates(state: dict, rules_yaml_path: str) -> List[Dict[str, Any]]:
    """
    Evaluates a template-selection rules file — ALL matching rules fire (not first-match).
    Returns a list of {template, vars} dicts for every matched rule.

    This is used for data.yml, compute-resources.yml, etc. where multiple
    templates can apply simultaneously (e.g. postgres + redis + cognito).
    """
    flat_state = flatten_dict(state)

    with open(rules_yaml_path, 'r') as f:
        rule_file = yaml.safe_load(f)

    matched: List[Dict[str, Any]] = []
    seen_templates: set = set()

    for rule in rule_file.get('templates', []):
        if _conditions_match(flat_state, rule.get('when', {})):
            template_path = rule['template']
            # Deduplicate — same template can match multiple rules (e.g. kms matches both stores+auth)
            if template_path not in seen_templates:
                seen_templates.add(template_path)
                matched.append({
                    'template': template_path,
                    'vars': rule.get('vars', {}),
                    'id': rule['id'],
                })

    return matched
