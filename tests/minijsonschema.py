"""Мини-валидатор JSON Schema (подмножество draft-07 для наших схем).

Поддерживает: type (вкл. ['a','b',null]), required, properties, items,
enum, const, pattern, minimum. Этого достаточно для schema v1-result.
"""
import re


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']}")
    types = schema.get("type")
    if types is not None:
        if isinstance(types, str):
            types = [types]
        if not _check_type(instance, types):
            errors.append(f"{path}: expected type {types}, got "
                          f"{type(instance).__name__}")
            return errors
    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match pattern {schema['pattern']}")
    if "minimum" in schema and isinstance(instance, (int, float)) \
            and not isinstance(instance, bool):
        if instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in instance:
                errors.extend(validate(instance[key], subschema,
                                       f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


def _check_type(instance, types: list[str]) -> bool:
    for name in types:
        if name == "null" and instance is None:
            return True
        if name == "string" and isinstance(instance, str):
            return True
        if name == "integer" and isinstance(instance, int) \
                and not isinstance(instance, bool):
            return True
        if name == "number" and isinstance(instance, (int, float)) \
                and not isinstance(instance, bool):
            return True
        if name == "boolean" and isinstance(instance, bool):
            return True
        if name == "object" and isinstance(instance, dict):
            return True
        if name == "array" and isinstance(instance, list):
            return True
    return False
