# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Analyzer-side symbol definition index ("symbols.json") generation.

The index is built from the build actions of an analysis, because the
language of a translation unit (BuildAction.lang) is only known on the
analyzer side. The pipeline is:

  1. collect_inputs(): every compiled source file and the project/vendor
     headers it depends on become (path, language) inputs. Dependencies are
     discovered with the compiler (tu_collector, '-M'), then headers coming
     from the compiler's implicit include directories are dropped unless an
     explicitly configured include directory (-I, -isystem, -iquote,
     -idirafter) is a more specific match.
  2. Inputs are deduplicated by (content hash, language). Byte-identical
     files share one index entry which lists all of their paths, and the
     same bytes compiled as C and as C++ get two separate entries.
  3. Universal Ctags is executed once per language over the representative
     files and its JSON output is normalized into an application-owned,
     language-independent definition schema.
  4. The result is written as a deterministic, versioned 'symbols.json'.
"""


import json
import os
import shlex
import shutil
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from tu_collector import tu_collector

from codechecker_common.logger import get_logger
from codechecker_common.util import get_file_content_hash

from codechecker_analyzer.buildlog import log_parser
from codechecker_analyzer.buildlog.build_action import BuildAction

LOG = get_logger('analyzer')

SYMBOLS_FILE_NAME = 'symbols.json'
SCHEMA_VERSION = 1

# Include directory options of the compiler whose argument is a directory
# that the build asked for explicitly. Dependencies under these directories
# are indexed even if the directory is nested inside an implicit include root.
EXPLICIT_INCLUDE_FLAGS = ('-I', '-isystem', '-iquote', '-idirafter')

# CodeChecker (BuildAction.lang) -> Universal Ctags language names. Ctags is
# always told the language explicitly, its extension based guess (e.g. '.h'
# is C++) is never used. Ctags has no Objective-C++ parser; its Objective-C
# parser is the closest match.
CTAGS_LANGUAGES = {
    'c': 'C',
    'c++': 'C++',
    'objective-c': 'ObjectiveC',
    'objective-c++': 'ObjectiveC',
}

# Ctags fields the normalization relies on, by their long names as listed by
# 'ctags --list-fields'. Every one of them must be supported by the executable
# so that the emitted JSON has a known shape.
CTAGS_FIELDS = ('name', 'input', 'line', 'end', 'kind', 'scope',
                'scopeKind', 'signature', 'typeref', 'roles')


class SymbolIndexError(Exception):
    """Raised when the symbol index cannot be generated."""


@dataclass(frozen=True, order=True)
class IndexInput:
    """A file to be indexed as a given language. 'path' is resolved."""
    path: str
    language: str


@dataclass
class InputCollectionResult:
    inputs: set[IndexInput]
    # Dependencies reported by the compiler over all build actions, after
    # path normalization but before filtering.
    raw_dependency_count: int = 0
    # Dependencies kept after filtering out implicit include directories.
    kept_dependency_count: int = 0
    # Build actions for which header discovery failed.
    failed_actions: int = 0


@lru_cache(maxsize=None)
def _resolve(path: str) -> str:
    """
    Return the canonical absolute path of 'path' (symlinks and '..' resolved).
    Both dependency discovery and the compile command may spell the same file
    differently (e.g. 'build/../include/x.h'); a single spelling is required
    so that identical files are not indexed under multiple identities.
    """
    return str(Path(path).resolve())


def get_explicit_include_dirs(analyzer_options: list[str],
                              directory: str) -> list[Path]:
    """
    Return the resolved directories given by -I, -isystem, -iquote and
    -idirafter in a compile command, either joined ('-Ifoo') or separated
    ('-I foo'). Relative directories are interpreted from the working
    directory of the compile command.
    """
    dirs: list[Path] = []
    options = iter(analyzer_options)
    for option in options:
        for flag in EXPLICIT_INCLUDE_FLAGS:
            if option == flag:
                value = next(options, None)
            elif option.startswith(flag) and len(option) > len(flag):
                value = option[len(flag):]
            else:
                continue

            if value:
                dirs.append(Path(_resolve(os.path.join(directory, value))))
            break

    return dirs


_implicit_include_dirs_cache: dict[tuple[str, str, tuple[str, ...]],
                                   list[Path]] = {}


def get_implicit_include_dirs(compiler: str, language: str,
                              analyzer_options: list[str]) -> list[Path]:
    """
    Return the resolved implicit include directories of the compiler for the
    given language. The detection is shared with the analyzer command
    generation (ImplicitCompilerInfo): the same probe and cache key are used,
    so results already gathered while parsing the compilation database are
    reused. ImplicitCompilerInfo is not populated for every build action
    (e.g. when the same Clang compiles and analyzes), in which case the
    compiler is probed here.
    """
    ici = log_parser.ImplicitCompilerInfo
    extra_opts = tuple(sorted(
        log_parser.filter_compiler_includes_extra_args(analyzer_options)))
    key = (compiler, language, extra_opts)

    if key not in _implicit_include_dirs_cache:
        info = ici.compiler_info.get(
            ici.ImplicitInfoSpecifierKey(compiler, language, extra_opts), {})
        includes = info.get('compiler_includes') or \
            ici.get_compiler_includes(compiler, language, list(extra_opts))
        _implicit_include_dirs_cache[key] = \
            [Path(_resolve(d)) for d in includes]

    return _implicit_include_dirs_cache[key]


def _longest_matching_root(path: Path, roots: Iterable[Path]) -> Path | None:
    """Return the deepest directory of 'roots' that contains 'path'."""
    best = None
    for root in roots:
        if path.is_relative_to(root) and \
                (best is None or len(root.parts) > len(best.parts)):
            best = root
    return best


def is_indexable_dependency(path: Path,
                            implicit_dirs: Iterable[Path],
                            explicit_dirs: Iterable[Path]) -> bool:
    """
    Decide whether a dependency of a translation unit belongs to the symbol
    index. The rule is: drop what the compiler finds on its own, keep what the
    build explicitly asked for. A file is dropped if it lies under one of the
    compiler's implicit include directories, unless an explicitly configured
    include directory is an equally or more specific match (e.g.
    '-I/usr/include/libxml2' while '/usr/include' is implicit).

    All paths must be resolved.
    """
    implicit = _longest_matching_root(path, implicit_dirs)
    if implicit is None:
        return True

    explicit = _longest_matching_root(path, explicit_dirs)
    return explicit is not None and \
        len(explicit.parts) >= len(implicit.parts)


def get_compiler(action: BuildAction) -> str:
    """Return the compiler executable of a build action."""
    return log_parser.determine_compiler(
        shlex.split(action.original_command),
        action.directory,
        log_parser.ImplicitCompilerInfo.is_executable_compiler)


def collect_inputs_for_action(
    action: BuildAction
) -> tuple[set[IndexInput], int, bool]:
    """
    Return the (path, language) inputs contributed by one build action, the
    number of dependencies reported by the compiler and whether dependency
    discovery succeeded. The source file itself is always indexed if it
    exists, even if header discovery fails.
    """
    language = action.lang
    inputs: set[IndexInput] = set()

    source = _resolve(action.source)
    if os.path.isfile(source):
        inputs.add(IndexInput(source, language))

    dependencies, error = tu_collector.get_dependent_headers(
        action.original_command, action.directory)

    # The dependency list contains the source file itself, so an empty set
    # means that the compiler could not preprocess the translation unit
    # (tu_collector may report this with an empty error message).
    success = not error and bool(dependencies)
    if not success:
        LOG.warning("Failed to discover the headers of '%s', its symbol "
                    "index may be incomplete. Compiler error: %s",
                    action.source, error or "<unknown>")
        return inputs, 0, False

    resolved = {Path(_resolve(dep)) for dep in dependencies}

    implicit_dirs = get_implicit_include_dirs(
        get_compiler(action), language, action.analyzer_options)
    explicit_dirs = get_explicit_include_dirs(
        action.analyzer_options, action.directory)

    for dep in resolved:
        if dep.is_file() and \
                is_indexable_dependency(dep, implicit_dirs, explicit_dirs):
            inputs.add(IndexInput(str(dep), language))

    return inputs, len(resolved), True


def collect_inputs(actions: Iterable[BuildAction],
                   jobs: int = 1) -> InputCollectionResult:
    """
    Collect the (path, language) inputs of the symbol index from build
    actions. Dependency discovery of the actions runs in parallel.
    """
    actions = list(actions)
    result = InputCollectionResult(inputs=set())

    # Compiler probes are cached process-wide by ImplicitCompilerInfo; warm
    # the cache sequentially so that parallel workers do not race on it.
    for action in actions:
        get_implicit_include_dirs(
            get_compiler(action), action.lang, action.analyzer_options)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        for inputs, raw_count, success in executor.map(
                collect_inputs_for_action, actions):
            result.inputs |= inputs
            result.raw_dependency_count += raw_count
            if not success:
                result.failed_actions += 1

    result.kept_dependency_count = len(result.inputs)
    return result


def group_by_identity(
    inputs: Iterable[IndexInput]
) -> dict[tuple[str, str], list[str]]:
    """
    Group inputs by their index identity: (content hash, language). The
    value is the sorted list of all paths sharing that identity.
    """
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for index_input in inputs:
        content_hash = get_file_content_hash(index_input.path)
        groups[(content_hash, index_input.language)].add(index_input.path)

    return {identity: sorted(paths) for identity, paths in groups.items()}


@dataclass(frozen=True, order=True)
class Definition:
    """
    A symbol definition in the application-owned schema. The field set is
    language independent; 'kind' and 'scope_kind' are the long kind names of
    the source language (e.g. 'function', 'class', 'namespace').
    """
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
    """The definitions of one (content hash, language) identity."""
    content_hash: str
    language: str
    paths: list[str]
    definitions: list[Definition] = field(default_factory=list)


@dataclass
class IndexStatistics:
    build_actions: int = 0
    failed_dependency_actions: int = 0
    raw_dependencies: int = 0
    indexed_files: int = 0
    indexes: int = 0
    definitions: int = 0
    dependency_seconds: float = 0.0
    ctags_seconds: float = 0.0


class Ctags:
    """A validated Universal Ctags executable with JSON output support."""

    def __init__(self, binary: str):
        self.binary = binary

    @staticmethod
    def find(binary: str | None = None) -> 'Ctags':
        """
        Locate and validate the Ctags executable. 'binary' is an explicit
        path or executable name; by default 'ctags' is looked up in PATH.
        Raises SymbolIndexError with an actionable message if the executable
        is missing, is not Universal Ctags or lacks the JSON output and the
        fields the index relies on.
        """
        requested = binary or 'ctags'
        resolved = shutil.which(requested)
        if not resolved:
            raise SymbolIndexError(
                f"Universal Ctags executable '{requested}' was not found. "
                "Install Universal Ctags (https://ctags.io) with JSON "
                "support or give its path with --ctags-binary.")

        ctags = Ctags(resolved)
        version = ctags._run(['--version'])
        if 'Universal Ctags' not in version:
            raise SymbolIndexError(
                f"'{resolved}' is not Universal Ctags (reported: "
                f"'{version.splitlines()[0] if version else ''}'). "
                "Exuberant Ctags and other variants have no JSON output; "
                "install Universal Ctags or give its path with "
                "--ctags-binary.")

        features = ctags._run(['--list-features']).split()
        if 'json' not in features:
            raise SymbolIndexError(
                f"'{resolved}' was built without JSON output support "
                "(the 'json' feature is missing from --list-features). "
                "Install a Universal Ctags build with libjansson support.")

        fields = {line.split()[1] for line in
                  ctags._run(['--list-fields']).splitlines()[1:]
                  if line.split()[1:]}
        missing = [f for f in CTAGS_FIELDS if f not in fields]
        if missing:
            raise SymbolIndexError(
                f"'{resolved}' does not support the tag fields "
                f"{', '.join(missing)} required by the symbol index. "
                "Please upgrade Universal Ctags.")

        return ctags

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        try:
            proc = subprocess.run(
                [self.binary, *args],
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding='utf-8',
                errors='replace',
                check=False)
        except OSError as ex:
            raise SymbolIndexError(
                f"Failed to execute '{self.binary}': {ex}") from ex

        if proc.returncode != 0:
            raise SymbolIndexError(
                f"'{self.binary} {' '.join(args)}' failed with exit code "
                f"{proc.returncode}: {proc.stderr.strip()}")

        return proc.stdout

    def command(self, ctags_language: str) -> list[str]:
        """
        Return the Ctags arguments used to tag files as 'ctags_language'.
        The file list is read from the standard input, the tags are written
        to the standard output as JSON lines. Compiler options are
        deliberately not forwarded: Ctags does not preprocess and interprets
        e.g. '-I' as "ignore identifiers". Only definitions are requested
        (reference tags such as included headers are disabled).
        """
        return ['--options=NONE',
                '--quiet=yes',
                '--output-format=json',
                '--sort=no',
                '--extras=-r',
                '--fields=+' + ''.join('{' + f + '}' for f in CTAGS_FIELDS),
                '--language-force=' + ctags_language,
                '-L', '-',
                '-f', '-']

    def tag_files(self, ctags_language: str,
                  paths: Iterable[str]) -> dict[str, list[dict]]:
        """
        Run Ctags once over 'paths' as 'ctags_language' and return the raw
        tag objects grouped by input file path.
        """
        paths = list(paths)
        if not paths:
            return {}

        output = self._run(self.command(ctags_language),
                           stdin='\n'.join(paths) + '\n')

        tags: dict[str, list[dict]] = defaultdict(list)
        for line in output.splitlines():
            if not line:
                continue
            try:
                tag = json.loads(line)
            except json.JSONDecodeError as ex:
                raise SymbolIndexError(
                    f"Unexpected output from '{self.binary}': {ex}: "
                    f"{line[:200]}") from ex

            if tag.get('_type') == 'tag':
                tags[tag['path']].append(tag)

        return tags


def normalize_tag(tag: dict) -> Definition | None:
    """
    Convert a raw Universal Ctags JSON tag into a Definition. Returns None
    for tags that are not definitions (reference roles) or lack the
    mandatory name, kind or line. Ctags specific fields that carry no
    location information usable by CodeChecker (pattern, file scoping
    marker, raw path) are dropped.
    """
    roles = tag.get('roles')
    if roles and roles != 'def':
        return None

    name = tag.get('name')
    kind = tag.get('kind')
    line = tag.get('line')
    if not name or not kind or not isinstance(line, int):
        return None

    end = tag.get('end')

    return Definition(
        name=name,
        kind=kind,
        line=line,
        end_line=end if isinstance(end, int) else None,
        scope=tag.get('scope') or None,
        scope_kind=tag.get('scopeKind') or None,
        signature=tag.get('signature') or None,
        typeref=tag.get('typeref') or None)


def build_indexes(groups: dict[tuple[str, str], list[str]],
                  ctags: Ctags) -> list[SymbolIndex]:
    """
    Tag one representative file of every (content hash, language) identity
    with Ctags, batched per language, and return the symbol indexes sorted
    by identity with their definitions in source order.
    """
    representatives: dict[str, dict[str, tuple[str, str]]] = \
        defaultdict(dict)  # language -> representative path -> identity
    for (content_hash, language), paths in groups.items():
        representatives[language][paths[0]] = (content_hash, language)

    indexes: dict[tuple[str, str], SymbolIndex] = {
        identity: SymbolIndex(identity[0], identity[1], paths)
        for identity, paths in groups.items()}

    for language in sorted(representatives):
        ctags_language = CTAGS_LANGUAGES.get(language)
        if not ctags_language:
            LOG.warning("Symbol index: no Universal Ctags parser is mapped "
                        "to language '%s', %d file(s) are not indexed.",
                        language, len(representatives[language]))
            continue

        by_path = representatives[language]
        tags = ctags.tag_files(ctags_language, sorted(by_path))
        for path, identity in by_path.items():
            definitions = filter(None, map(normalize_tag, tags.get(path, [])))
            indexes[identity].definitions = sorted(set(definitions))

    return [indexes[identity] for identity in sorted(indexes)]


def to_json(indexes: list[SymbolIndex]) -> dict:
    """Return the versioned 'symbols.json' document."""
    return {
        'version': SCHEMA_VERSION,
        'indexes': [asdict(index) for index in indexes]
    }


def generate(actions: Iterable[BuildAction],
             ctags: Ctags,
             output_path: str | Path,
             jobs: int = 1) -> IndexStatistics:
    """
    Generate the symbol index of the given build actions and write it to
    'output_path'. Returns statistics of the run.
    """
    actions = list(actions)
    stats = IndexStatistics(build_actions=len(actions))

    start = time.time()
    collected = collect_inputs(actions, jobs)
    groups = group_by_identity(collected.inputs)
    stats.dependency_seconds = time.time() - start
    stats.failed_dependency_actions = collected.failed_actions
    stats.raw_dependencies = collected.raw_dependency_count
    stats.indexed_files = len(collected.inputs)

    start = time.time()
    indexes = build_indexes(groups, ctags)
    stats.ctags_seconds = time.time() - start
    stats.indexes = len(indexes)
    stats.definitions = sum(len(index.definitions) for index in indexes)

    with open(output_path, 'w', encoding='utf-8') as output:
        json.dump(to_json(indexes), output, indent=2)
        output.write('\n')

    return stats
