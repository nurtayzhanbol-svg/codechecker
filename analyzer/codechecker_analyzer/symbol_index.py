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


import os
import shlex
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
