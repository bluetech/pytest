#!/usr/bin/env python
"""Generate JSON Schema for pytest.toml configuration file.

This script introspects all ini options registered by pytest plugins
and generates a JSON Schema compatible with SchemaStore for pytest.toml
configuration files.

The generated schema includes:
- Type information for all configuration options
- Help text descriptions
- Default values where applicable
- Proper handling of different pytest config types (string, bool, paths, etc.)

Usage:
    python scripts/generate-pytest-schema.py > pytest-schema.json

The schema can be used with:
- SchemaStore (https://www.schemastore.org/)
- IDE autocomplete and validation in TOML files
- Documentation generation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add src to path to import pytest modules
PYTEST_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PYTEST_ROOT / "src"))

import _pytest.config


def pytest_type_to_json_schema(
    pytest_type: str,
    default: Any,
) -> dict[str, Any]:
    """Convert a pytest ini type to JSON Schema type definition.

    Args:
        pytest_type: The pytest type (string, bool, int, float, paths, pathlist, args, linelist)
        default: The default value for this option

    Returns:
        A dictionary with JSON Schema type information
    """
    # Map pytest types to JSON Schema types
    type_mapping = {
        "string": {"type": "string"},
        "bool": {"type": "boolean"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "paths": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "pathlist": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "args": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "linelist": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
    }

    # Create a copy of the schema to avoid mutating the template
    schema = type_mapping.get(pytest_type, {"type": "string"}).copy()

    # For oneOf schemas, we need to deep copy
    if "oneOf" in schema:
        schema = {"oneOf": [item.copy() for item in schema["oneOf"]]}

    # Add default value if it's not empty/falsy (except for explicit False/0/0.0)
    if default is not None and (default or default is False or default == 0 or default == 0.0):
        # For list types, only add default if non-empty
        if pytest_type in ("paths", "pathlist", "args", "linelist"):
            if default:
                schema["default"] = default
        else:
            schema["default"] = default

    return schema


def collect_ini_options() -> dict[str, tuple[str, str, Any]]:
    """Collect all ini options by creating a config and examining its parser.

    Returns:
        A dictionary mapping option names to (help, type, default) tuples
    """
    # Create a minimal config to trigger plugin registration
    # This will call pytest_addoption hooks from all builtin plugins
    config = _pytest.config.Config.fromdictargs(
        {},
        [str(PYTEST_ROOT)],
    )

    # The parser's _inidict contains all registered ini options
    # Format: {name: (help, type, default)}
    return config._parser._inidict


def generate_schema() -> dict[str, Any]:
    """Generate the complete JSON Schema for pytest configuration.

    Returns:
        A JSON Schema dictionary
    """
    ini_options = collect_ini_options()

    # Build properties for each ini option
    properties: dict[str, Any] = {}

    for name, (help_text, ini_type, default) in sorted(ini_options.items()):
        property_schema = pytest_type_to_json_schema(ini_type, default)
        if help_text:
            property_schema["description"] = help_text

        properties[name] = property_schema

    # Add special property for minversion which is handled separately
    properties["minversion"] = {
        "type": "string",
        "description": "Minimally required pytest version",
    }

    # Create the full schema
    # Note: We use additionalProperties: true to allow for plugin-specific
    # configuration options that may not be in the core pytest
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://json.schemastore.org/pytest.json",
        "title": "pytest configuration",
        "description": "Schema for pytest configuration in pyproject.toml or pytest.toml files under the [tool.pytest.ini_options] table",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }

    return schema


def main() -> None:
    """Generate and output the pytest JSON Schema."""
    schema = generate_schema()

    # Pretty-print the JSON schema
    output = json.dumps(schema, indent=2, ensure_ascii=False)
    print(output)


if __name__ == "__main__":
    main()
