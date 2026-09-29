# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Reader and validator of the 'symbols.json' symbol index written by
'CodeChecker analyze --symbol-index' (see
codechecker_analyzer.symbol_index). It is shared by the store client, which
needs the referenced source paths, and by the server, which must not trust
the document before persisting it.
"""
from dataclasses import dataclass, field
import json

SYMBOLS_FILE_NAME = 'symbols.json'
SUPPORTED_VERSIONS = (1,)

_REQUIRED_DEFINITION_FIELDS = {'name': str, 'kind': str, 'line': int}
_OPTIONAL_DEFINITION_FIELDS = {'end_line': int, 'scope': str,
                               'scope_kind': str, 'signature': str,
                               'typeref': str}


class SymbolsJsonError(Exception):
    """The document is not a supported, well-formed 'symbols.json'."""


@dataclass(frozen=True)
class Definition:
    name: str
    kind: str
    line: int
    end_line: int | None = None
    scope: str | None = None
    scope_kind: str | None = None
    signature: str | None = None
    typeref: str | None = None


@dataclass
class SymbolIndex:
    """The definitions of one (content hash, language) identity and the
    concrete analyzer-side paths that had this content."""
    content_hash: str
    language: str
    paths: list[str]
    definitions: list[Definition] = field(default_factory=list)


def _expect(condition: bool, where: str, message: str):
    if not condition:
        raise SymbolsJsonError(f"{where}: {message}")


def _parse_definition(raw, where: str) -> Definition:
    _expect(isinstance(raw, dict), where, "definition must be an object")
    values = {}
    for name, type_ in _REQUIRED_DEFINITION_FIELDS.items():
        value = raw.get(name)
        _expect(isinstance(value, type_) and not isinstance(value, bool),
                where, f"definition field '{name}' must be a {type_.__name__}")
        values[name] = value
    _expect(values['line'] >= 1, where, "definition 'line' must be positive")
    for name, type_ in _OPTIONAL_DEFINITION_FIELDS.items():
        value = raw.get(name)
        _expect(value is None or
                (isinstance(value, type_) and not isinstance(value, bool)),
                where,
                f"definition field '{name}' must be a {type_.__name__} "
                "or null")
        values[name] = value if value != '' else None
    return Definition(**values)


def parse(document) -> list[SymbolIndex]:
    """
    Validate an already JSON-decoded 'symbols.json' document and return its
    indexes. Raises SymbolsJsonError on the first structural problem.
    """
    _expect(isinstance(document, dict), SYMBOLS_FILE_NAME,
            "top level must be an object")
    version = document.get('version')
    _expect(version in SUPPORTED_VERSIONS, SYMBOLS_FILE_NAME,
            f"unsupported version {version!r}, supported: "
            f"{', '.join(map(str, SUPPORTED_VERSIONS))}")
    indexes = document.get('indexes')
    _expect(isinstance(indexes, list), SYMBOLS_FILE_NAME,
            "'indexes' must be a list")

    result = []
    seen: set[tuple[str, str]] = set()
    for position, raw in enumerate(indexes):
        where = f"{SYMBOLS_FILE_NAME} indexes[{position}]"
        _expect(isinstance(raw, dict), where, "index must be an object")
        content_hash = raw.get('content_hash')
        _expect(isinstance(content_hash, str) and content_hash, where,
                "'content_hash' must be a non-empty string")
        language = raw.get('language')
        _expect(isinstance(language, str) and language, where,
                "'language' must be a non-empty string")
        paths = raw.get('paths')
        _expect(isinstance(paths, list) and paths and
                all(isinstance(p, str) and p for p in paths), where,
                "'paths' must be a non-empty list of strings")
        definitions = raw.get('definitions')
        _expect(isinstance(definitions, list), where,
                "'definitions' must be a list")
        identity = (content_hash, language)
        _expect(identity not in seen, where,
                f"duplicate (content_hash, language) {identity}")
        seen.add(identity)

        result.append(SymbolIndex(
            content_hash, language, list(paths),
            [_parse_definition(d, f"{where}.definitions[{i}]")
             for i, d in enumerate(definitions)]))

    return result


def load(path) -> list[SymbolIndex]:
    """Read and validate the 'symbols.json' file at 'path'."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            document = json.load(f)
    except (OSError, ValueError) as ex:
        raise SymbolsJsonError(f"{path}: cannot be read as JSON: {ex}") \
            from ex
    return parse(document)
