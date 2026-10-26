"""Validation of classifier output against the pack's schema and taxonomy."""

from __future__ import annotations

import jsonschema


def build_response_schema(pack) -> dict:
    """JSON Schema for the classifier's structured output, derived from the pack."""
    props = {
        "category": {"type": "string", "enum": pack.category_ids()},
        "subcategory": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "summary": {"type": "string", "maxLength": 160},
        "why": {"type": "string", "maxLength": 160},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_game": {"type": "boolean"},
        "is_chat_wrapper": {"type": "boolean"},
        "ai_claim": {"type": "string", "enum": ["self-declared", "platform-listed", "inferred", "none"]},
        "evidence": {"type": "array", "items": {"type": "object"}},
    }
    required = ["category", "summary", "why", "confidence", "is_game", "is_chat_wrapper", "ai_claim"]
    for f in pack.schema.get("fields", []):
        name = f["id"]
        t = f.get("type", "text")
        if t == "enum":
            props[name] = {"type": "string", "enum": f.get("values", [])}
        elif t == "multi":
            props[name] = {"type": "array", "items": {"type": "string", "enum": f.get("values", [])}}
        elif t == "bool":
            props[name] = {"type": "boolean"}
        elif t == "number":
            props[name] = {"type": "number"}
        else:
            props[name] = {"type": "string"}
        if f.get("required"):
            required.append(name)
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def validate_classification(pack, output: dict) -> tuple[bool, str]:
    """Returns (ok, error). Categories must come from the taxonomy."""
    schema = build_response_schema(pack)
    try:
        jsonschema.validate(output, schema)
    except jsonschema.ValidationError as e:
        return False, e.message
    # subcategory must belong to its category
    sub = output.get("subcategory")
    if sub:
        cat = pack.categories.get(output.get("category", {}), {})
        subs = {s["id"] if isinstance(s, dict) else s for s in cat.get("subcategories", [])}
        if sub not in subs:
            return False, f"subcategory {sub!r} not in category {output['category']!r}"
    return True, ""
