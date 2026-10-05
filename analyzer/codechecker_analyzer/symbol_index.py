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

EXPLICIT_INCLUDE_FLAGS = ('-I', '-isystem', '-iquote', '-idirafter')

#class SymbolIndexError(Exception):
@dataclass(frozen=True, order=True)
class IndexInput:
    """A file to be indexed as a given language. 'path' is resolved.""" 
    path: str
    language: str

@dataclass
class InputCollectionResult:
    inputs: set[IndexInput]
    raw_dependency_count: int = 0
    kept_dependency_count: int = 0
    failed_actions: int = 0



@lru_cache(maxsize=None)
def _resolve(path: str):
    return str(Path(path).resolve())

def get_explicit_include_dirs(analyzer_options: list[str], directory:str) -> list[Path]:
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

def get_compiler(action: BuildAction) -> str:
    return log_parser.determine_compiler(
        shlex.split(action.original_command),
        action.directory,
        log_parser.ImplicitCompilerInfo.is_executable_compiler)


_implicit_include_dirs_cache: dict[tuple[str, str, tuple[str, ...]],
                                   list[Path]] = {}

def get_implicit_include_dirs(compiler: str, language: str, analyzer_options: list[str]) -> list[Path]:
    ici = log_parser.ImplicitCompilerInfo
    extra_opts = tuple(sorted(
        log_parser.filter_compiler_includes_extra_args(analyzer_options)))
    key = (compiler, language, extra_opts)

    if key not in _implicit_include_dirs_cache:
        info = ici.compiler_info.get(
            ici.ImplicitInfoSpecifierKey(compiler, language, extra_opts), {})
        includes = info.get('compiler_includes') or ici.get_compiler_includes(compiler, language, list(extra_opts))
        _implicit_include_dirs_cache[key] = [Path(_resolve(d)) for d in includes]
        
    return _implicit_include_dirs_cache[key]

def _longest_matching_root(path: Path, roots: Iterable[Path]) -> Path | None:
    """Return the deepest directory of 'roots' that contains 'path'."""
    best = None
    for root in roots:
        if path.is_relative_to(root) and (best is None or len(root.parts) > len(best.parts)):
            best = root
    return best


def is_indexable_dependency(path: Path,
                            implicit_dirs: Iterable[Path],
                            explicit_dirs: Iterable[Path]) -> bool:
    implicit = _longest_matching_root(path, implicit_dirs)
    if implicit is None:
        return True

    explicit = _longest_matching_root(path, explicit_dirs)
    return explicit is not None and len(explicit.parts) >= len(implicit.parts)





def collect_inputs_for_action(
        action: BuildAction
) -> tuple[set[IndexInput], int, bool]:
    language = action.lang
    inputs: set[IndexInput] = set()

    source = _resolve(action.source)
    if os.path.isfile(source):
        inputs.add(IndexInput(source, language))

    dependencies, error = tu_collector.get_dependent_headers(
        action.original_command, action.directory)

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
        if dep.is_file() and is_indexable_dependency(dep, implicit_dirs, explicit_dirs):
            inputs.add(IndexInput(str(dep), language))

    return inputs, len(resolved), True


def collect_inputs(actions: Iterable[BuildAction],
                   jobs: int = 1) -> InputCollectionResult:
    actions = list(actions)
    result = InputCollectionResult(inputs=set())

    for action in actions:
        get_implicit_include_dirs(
            get_compiler(action), action.lang, action.analyzer_options)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        for inputs, raw_count, success in executor.map(collect_inputs_for_action, actions):
            result.inputs |= inputs
            result.raw_dependency_count += raw_count
            if not success:
                result.failed_actions += 1
    result.kept_dependency_count = len(result.inputs)
    return result


def group_by_identity(inputs: Iterable[IndexInput]) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for index_input in inputs:
        content_hash = get_file_content_hash(index_input.path)
        groups[(content_hash, index_input.language)].add(index_input.path)
    return {identity: sorted(paths) for identity, paths in groups.items()}
