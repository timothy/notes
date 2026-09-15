"""The contract as a runtime object.

``openapi.yaml`` is the source of truth, so its JSON Schemas (Draft 2020-12) validate request bodies at
runtime and responses in tests. The registry is built exactly as ``scripts/validate_contract.py`` builds
it, so both tools resolve ``#/components/...`` references the same way. Validation failures become the
contract's ``FieldError`` shape: a location, an RFC 6901 pointer, and a human explanation.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry
from referencing.jsonschema import DRAFT202012

# Absolute URI under which the document is registered so that '#/components/...' references resolve.
SPEC_URI = "https://notes-api.example.com/openapi.yaml"
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "openapi.yaml"

_QUOTED = re.compile(r"'([^']*)'")


@dataclass(frozen=True, slots=True)
class FieldError:
    """One entry of a Problem's ``errors`` array."""

    location: str
    pointer: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"location": self.location, "pointer": self.pointer, "detail": self.detail}


class Contract:
    """A loaded OpenAPI document with cached validators for its component schemas."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self.document = document
        self._registry = Registry().with_resource(SPEC_URI, DRAFT202012.create_resource(document))
        self._resolver = self._registry.resolver(base_uri=SPEC_URI)
        self._validators: dict[str, Draft202012Validator] = {}

    @classmethod
    def load(cls, path: Path = DEFAULT_CONTRACT_PATH) -> Contract:
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    @property
    def schemas(self) -> Mapping[str, Any]:
        schemas: Mapping[str, Any] = self.document["components"]["schemas"]
        return schemas

    def validator(self, schema_name: str) -> Draft202012Validator:
        """A validator for ``#/components/schemas/{schema_name}``; unknown names are a programming error."""
        if schema_name not in self.schemas:
            raise KeyError(schema_name)
        validator = self._validators.get(schema_name)
        if validator is None:
            validator = Draft202012Validator(
                {"$ref": f"{SPEC_URI}#/components/schemas/{schema_name}"},
                registry=self._registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )
            self._validators[schema_name] = validator
        return validator

    def validate_instance(self, schema: Mapping[str, Any], instance: object) -> list[str]:
        """Messages for any schema fragment of the document; relative ``#/`` references are allowed."""
        validator = Draft202012Validator(
            _absolutize(schema), registry=self._registry, format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        return [error.message for error in validator.iter_errors(instance)]

    def validate_body(self, schema_name: str, instance: object) -> list[FieldError]:
        """Field errors for a request body, one per pointer, sorted by pointer; empty when valid."""
        errors: dict[str, FieldError] = {}
        for error in self.validator(schema_name).iter_errors(instance):
            for field_error in self._field_errors(error, "body"):
                errors.setdefault(field_error.pointer, field_error)
        return sorted(errors.values(), key=lambda field_error: field_error.pointer)

    def _field_errors(self, error: ValidationError, location: str) -> Iterator[FieldError]:
        if error.validator in ("anyOf", "oneOf") and error.context:
            deeper = best_match(error.context)
            if deeper is not None:
                yield from self._field_errors(deeper, location)
                return
        pointer = json_pointer(error.absolute_path)
        if error.validator == "required":
            present = error.instance if isinstance(error.instance, Mapping) else {}
            required = error.validator_value if isinstance(error.validator_value, list) else []
            for key in required:
                if key not in present:
                    yield FieldError(location, f"{pointer}/{escape_token(str(key))}", "is required")
            return
        if error.validator in ("additionalProperties", "unevaluatedProperties"):
            for key in self._unknown_keys(error):
                yield FieldError(location, f"{pointer}/{escape_token(key)}", "unknown field")
            return
        yield FieldError(location, pointer, _detail(error))

    def _unknown_keys(self, error: ValidationError) -> list[str]:
        if not isinstance(error.instance, Mapping):
            return []
        declared = self._declared_properties(error.schema, seen=set())
        unknown = [str(key) for key in error.instance if key not in declared]
        if unknown:
            return unknown
        # Fall back to the keys jsonschema names in its message.
        return _QUOTED.findall(error.message)

    def _declared_properties(self, schema: Any, seen: set[str]) -> set[str]:
        """Property names a schema declares, following ``$ref`` and the applicators that compose objects."""
        if not isinstance(schema, Mapping):
            return set()
        if "$ref" in schema:
            ref = str(schema["$ref"])
            if ref in seen:
                return set()
            seen.add(ref)
            return self._declared_properties(self._resolver.lookup(ref).contents, seen)
        names: set[str] = set(schema.get("properties", {}))
        for keyword in ("allOf", "anyOf", "oneOf"):
            for subschema in schema.get(keyword, []):
                names |= self._declared_properties(subschema, seen)
        for keyword in ("then", "else"):
            if keyword in schema:
                names |= self._declared_properties(schema[keyword], seen)
        return names


@functools.lru_cache(maxsize=4)
def load_contract(path: Path = DEFAULT_CONTRACT_PATH) -> Contract:
    """The contract at ``path``, parsed once per process."""
    return Contract.load(path)


def _absolutize(schema: Any) -> Any:
    """Rewrite document-relative ``#/`` references so a fragment validates outside its document."""
    if isinstance(schema, Mapping):
        return {
            key: (
                SPEC_URI + value
                if key == "$ref" and isinstance(value, str) and value.startswith("#")
                else _absolutize(value)
            )
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_absolutize(item) for item in schema]
    return schema


def nul_character_errors(instance: object) -> list[FieldError]:
    """One error per string anywhere in ``instance`` that contains U+0000.

    The schemas do not forbid it, but PostgreSQL text cannot store it, so the server rejects it up front
    with the same shape as a schema violation instead of failing inside the database.
    """
    errors: list[FieldError] = []

    def walk(value: object, path: tuple[str | int, ...]) -> None:
        if isinstance(value, str):
            if "\x00" in value:
                errors.append(FieldError("body", json_pointer(path), "must not contain NUL characters"))
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, index))

    walk(instance, ())
    return errors


def surrogate_character_errors(instance: object) -> list[FieldError]:
    """Reject unencodable strings before schema errors can echo them into JSON responses.

    An invalid property name is reported at the root: its name cannot safely appear in a pointer.
    """
    errors: list[FieldError] = []

    def invalid(value: str) -> bool:
        return any(0xD800 <= ord(character) <= 0xDFFF for character in value)

    def walk(value: object, path: tuple[str | int, ...]) -> None:
        if isinstance(value, str) and invalid(value):
            errors.append(FieldError("body", json_pointer(path), "must not contain lone Unicode surrogates"))
        elif isinstance(value, dict):
            for key, item in value.items():
                if invalid(str(key)):
                    errors.append(FieldError("body", "", "must not contain lone Unicode surrogates"))
                else:
                    walk(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, index))

    walk(instance, ())
    return errors


def json_pointer(path: Iterable[str | int]) -> str:
    """RFC 6901 pointer for a jsonschema path; the empty path is the whole document."""
    return "".join(f"/{escape_token(str(part))}" for part in path)


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _detail(error: ValidationError) -> str:
    value = error.validator_value
    match error.validator:
        case "type":
            return f"must be {_type_name(value)}"
        case "minLength":
            return "must not be empty" if value == 1 else f"must be at least {value} code points"
        case "maxLength":
            return f"must be at most {value} code points"
        case "pattern":
            description = error.schema.get("description") if isinstance(error.schema, Mapping) else None
            return str(description) if description else "does not match the required pattern"
        case "minItems":
            return f"must have at least {value} item{'s' if value != 1 else ''}"
        case "maxItems":
            return f"must have at most {value} item{'s' if value != 1 else ''}"
        case "uniqueItems":
            return "must not contain duplicate items"
        case "minProperties":
            return (
                "at least one property is required"
                if value == 1
                else f"at least {value} properties are required"
            )
        case "enum":
            options = value if isinstance(value, list) else [value]
            return "must be one of: " + ", ".join(str(option) for option in options)
        case "const":
            return f"must be {value!r}"
        case "minimum":
            return f"must be at least {value}"
        case "maximum":
            return f"must be at most {value}"
        case "format":
            return f"must be a valid {value}"
        case _:
            return error.message


def _type_name(value: Any) -> str:
    names = [str(name) for name in value] if isinstance(value, list) else [str(value)]
    return " or ".join(names)
