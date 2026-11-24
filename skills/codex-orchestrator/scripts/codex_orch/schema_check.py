from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "$schema", "title", "description", "type", "enum", "required", "properties",
    "additionalProperties", "items",
})
SUPPORTED_SCHEMA_TYPES = frozenset({
    "object", "array", "string", "integer", "number", "boolean", "null",
})


def _reject_constant(token: str):
    raise ValueError(f"invalid JSON constant {token!r}")


def _load_json(text: str):
    return json.loads(text, parse_constant=_reject_constant)


def _check_node(node, path: str) -> None:
    if not isinstance(node, dict):
        raise ValueError(f"expected a schema object at {path}")
    for keyword in node:
        if keyword not in SUPPORTED_SCHEMA_KEYWORDS:
            raise ValueError(f"unsupported JSON Schema keyword {keyword!r} at {path}")
    for keyword in ("$schema", "title", "description"):
        if keyword in node and not isinstance(node[keyword], str):
            raise ValueError(f"expected a string at {path}.{keyword}")
    if "type" in node:
        declared = node["type"]
        types = declared if isinstance(declared, list) else [declared]
        if not types or any(
            not isinstance(name, str) or name not in SUPPORTED_SCHEMA_TYPES for name in types
        ):
            raise ValueError(f"unsupported JSON Schema type {declared!r} at {path}.type")
        if len(set(types)) != len(types):
            raise ValueError(f"duplicate JSON Schema type at {path}.type")
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
        raise ValueError(f"expected a non-empty array at {path}.enum")
    if "required" in node:
        required = node["required"]
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError(f"expected an array of strings at {path}.required")
        if len(set(required)) != len(required):
            raise ValueError(f"duplicate property name at {path}.required")
    if "properties" in node:
        properties = node["properties"]
        if not isinstance(properties, dict):
            raise ValueError(f"expected an object at {path}.properties")
        for key, child in properties.items():
            suffix = f".{key}" if key.isidentifier() else f"[{json.dumps(key)}]"
            _check_node(child, f"{path}.properties{suffix}")
    for keyword in ("additionalProperties", "items"):
        if keyword in node and not isinstance(node[keyword], bool):
            _check_node(node[keyword], f"{path}.{keyword}")


def preflight_schema(schema_path: str | Path) -> tuple[dict | None, str | None]:
    path = Path(schema_path)
    try:
        if not path.is_file():
            return None, f"schema path is not a regular file: {path}"
        schema = _load_json(path.read_text(encoding="utf-8"))
        _check_node(schema, "$")
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"invalid schema {path}: {exc}"
    return schema, None


def _json_equal(left, right) -> bool:
    if type(left) is not type(right):
        return type(left) in (int, float) and type(right) in (int, float) and left == right
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def matches_schema(value, schema) -> bool:
    if isinstance(schema, bool):
        return schema
    value_types = {
        dict: ("object",),
        list: ("array",),
        str: ("string",),
        int: ("integer", "number"),
        float: ("number",),
        bool: ("boolean",),
        type(None): ("null",),
    }
    actual = value_types.get(type(value), ())
    if type(value) is float and value.is_integer():
        actual += ("integer",)
    if "type" in schema:
        declared = schema["type"]
        types = declared if isinstance(declared, list) else [declared]
        if not any(name in actual for name in types):
            return False
    if "enum" in schema and not any(_json_equal(value, member) for member in schema["enum"]):
        return False
    if isinstance(value, dict):
        if any(key not in value for key in schema.get("required", ())):
            return False
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        return all(matches_schema(item, properties.get(key, additional)) for key, item in value.items())
    if isinstance(value, list) and "items" in schema:
        return all(matches_schema(item, schema["items"]) for item in value)
    return True


def validate_last_message(
    raw_bytes: bytes, schema_path: str | Path | None
) -> tuple[bool, str | None, object | None]:
    try:
        instance = _load_json(raw_bytes.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        return False, f"invalid JSON last message: {exc}", None
    if schema_path is not None:
        schema, error = preflight_schema(schema_path)
        if error is not None:
            return False, error, None
        try:
            if not matches_schema(instance, schema):
                return False, "last message does not match the JSON Schema", None
        except RecursionError:
            return False, "last message exceeds JSON Schema validation nesting limits", None
    return True, None, instance


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight",))
    parser.add_argument("schema_path")
    args = parser.parse_args()
    _, error = preflight_schema(args.schema_path)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
