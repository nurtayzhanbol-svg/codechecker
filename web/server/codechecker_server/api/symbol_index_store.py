# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Server-side persistence of the analyzer-generated symbol index
('symbols.json', see codechecker_common.symbols_json) during a mass store.

The storage has two stages with different ownership and transaction scope:

  Stage A - store_symbol_indexes():
    Product-global, deduplicated SymbolIndex/SymbolDefinition rows keyed by
    (content_hash, language). Runs after the FileContent rows exist and
    before the run-scoped transaction, in its own small batched
    transactions, so that two parallel stores of the same content only
    contend on a short UNIQUE-constrained insert.

  Stage B - replace_run_symbol_files():
    The RunSymbolFile membership "this File of this Run is described by this
    SymbolIndex". It runs inside the run-scoped transaction once the Run.id
    is known and replaces the previous membership of the run as a set.

Both stages only trust rows that are already persisted: an index whose
content is not in file_contents is skipped in Stage A, a membership whose
File does not have the content hash claimed by symbols.json is skipped in
Stage B.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
import os
from pathlib import Path
import time

import sqlalchemy
from sqlalchemy.orm import Session as SA_Session

from codechecker_common import symbols_json
from codechecker_common.logger import get_logger
from codechecker_common.util import chunks

from codechecker_report_converter.util import trim_path_prefixes

from ..database.database import DBSession
from ..database.run_db_model import \
    File, FileContent, RunSymbolFile, SymbolDefinition, SymbolIndex

LOG = get_logger('server')

# Number of SymbolIndex rows (with all their definitions) committed in one
# Stage A transaction. Tuning value: smaller batches shorten the time a
# concurrent reader/writer waits on SQLite, larger ones reduce round trips.
STAGE_A_BATCH_SIZE = 50

# Number of content hashes / file IDs in one 'IN (...)' look-up.
QUERY_CHUNK_SIZE = 500


@dataclass
class StageAStatistics:
    indexes_in_input: int = 0
    skipped_missing_content: int = 0
    created: int = 0
    reused: int = 0
    definitions_created: int = 0
    batches: int = 0
    retries: int = 0
    seconds: float = 0.0


@dataclass
class StageBStatistics:
    memberships: int = 0
    skipped_unknown_file: int = 0
    skipped_content_mismatch: int = 0
    skipped_missing_index: int = 0


def load_symbol_indexes(report_dir: Path) -> list[symbols_json.SymbolIndex]:
    """
    Load and validate every 'symbols.json' found in the unzipped 'reports'
    tree. A malformed document aborts the store with SymbolsJsonError. The
    same (content_hash, language) identity may be present in several
    analysis output directories (same header seen from several builds), the
    first occurrence is kept and its path list is extended.
    """
    by_identity: dict[tuple[str, str], symbols_json.SymbolIndex] = {}
    for root_dir_path, _, file_names in os.walk(report_dir):
        if symbols_json.SYMBOLS_FILE_NAME not in file_names:
            continue

        path = os.path.join(root_dir_path, symbols_json.SYMBOLS_FILE_NAME)
        LOG.debug("Loading symbol index '%s'", path)
        for index in symbols_json.load(path):
            identity = (index.content_hash, index.language)
            known = by_identity.get(identity)
            if known is None:
                by_identity[identity] = index
            else:
                known.paths = sorted(set(known.paths) | set(index.paths))

    return [by_identity[identity] for identity in sorted(by_identity)]


def _existing_content_hashes(session: SA_Session,
                             content_hashes: set[str]) -> set[str]:
    found: set[str] = set()
    for chunk in chunks(sorted(content_hashes), QUERY_CHUNK_SIZE):
        found.update(
            h for h, in session.query(FileContent.content_hash)
            .filter(FileContent.content_hash.in_(list(chunk))))
    return found


def _existing_indexes(session: SA_Session,
                      content_hashes: set[str]) -> dict[tuple[str, str], int]:
    """(content_hash, language) -> SymbolIndex.id for the given hashes."""
    found: dict[tuple[str, str], int] = {}
    for chunk in chunks(sorted(content_hashes), QUERY_CHUNK_SIZE):
        for id_, content_hash, language in \
                session.query(SymbolIndex.id, SymbolIndex.content_hash,
                              SymbolIndex.language) \
                .filter(SymbolIndex.content_hash.in_(list(chunk))):
            found[(content_hash, language)] = id_
    return found


def _definition_rows(symbol_index_id: int,
                     definitions: list[symbols_json.Definition]) -> list[dict]:
    return [{'symbol_index_id': symbol_index_id,
             'name': d.name, 'kind': d.kind, 'line': d.line,
             'end_line': d.end_line, 'scope': d.scope,
             'scope_kind': d.scope_kind, 'signature': d.signature,
             'typeref': d.typeref}
            for d in definitions]


def _store_batch(session_factory,
                 batch: list[symbols_json.SymbolIndex],
                 run_name: str,
                 stats: StageAStatistics):
    """
    One Stage A transaction: re-read which identities of the batch already
    exist, insert the missing SymbolIndex rows, flush for their IDs, insert
    the definitions of the new rows only, commit. On the UNIQUE/lock
    conflicts expected between parallel stores the whole batch is retried
    from the re-read, so the loser of a race reuses the winner's row.
    """
    max_tries, tries, wait_time = 3, 0, timedelta(seconds=1)
    while tries < max_tries:
        tries += 1
        try:
            with DBSession(session_factory) as session:
                existing = _existing_indexes(
                    session, {index.content_hash for index in batch})

                new_rows: list[tuple[SymbolIndex,
                                     symbols_json.SymbolIndex]] = []
                for index in batch:
                    if (index.content_hash, index.language) in existing:
                        continue
                    row = SymbolIndex(index.content_hash, index.language)
                    session.add(row)
                    new_rows.append((row, index))

                if new_rows:
                    session.flush()
                    definitions = []
                    for row, index in new_rows:
                        definitions.extend(
                            _definition_rows(row.id, index.definitions))
                    if definitions:
                        session.execute(sqlalchemy.insert(SymbolDefinition),
                                        definitions)

                session.commit()

                stats.created += len(new_rows)
                stats.reused += len(batch) - len(new_rows)
                stats.definitions_created += sum(
                    len(index.definitions) for _, index in new_rows)
                return
        except (sqlalchemy.exc.OperationalError,
                sqlalchemy.exc.ProgrammingError,
                sqlalchemy.exc.IntegrityError) as ex:
            stats.retries += 1
            LOG.warning("Storing symbol indexes of run '%s' failed: %s.\n"
                        "Waiting %s before trying again...",
                        run_name, ex, wait_time)
            time.sleep(wait_time.total_seconds())
            wait_time *= 2

    raise ConnectionRefusedError("Storing the symbol indexes of the run "
                                 "failed due to excessive contention!")


def store_symbol_indexes(session_factory,
                         indexes: list[symbols_json.SymbolIndex],
                         run_name: str,
                         batch_size: int = STAGE_A_BATCH_SIZE
                         ) -> StageAStatistics:
    """
    Stage A. Persist the product-global SymbolIndex/SymbolDefinition rows of
    'indexes' that do not exist yet. Indexes whose content is not in
    file_contents are dropped up front (their deferred FK would otherwise
    roll back the whole batch); they are logged with their paths.
    """
    stats = StageAStatistics(indexes_in_input=len(indexes))
    start = time.time()

    with DBSession(session_factory) as session:
        present = _existing_content_hashes(
            session, {index.content_hash for index in indexes})

    storable = []
    for index in indexes:
        if index.content_hash in present:
            storable.append(index)
        else:
            stats.skipped_missing_content += 1
            LOG.warning("Symbol index of run '%s' for content %s (%s) is "
                        "not stored: no file content with this hash is in "
                        "the database. Paths: %s", run_name,
                        index.content_hash, index.language,
                        ', '.join(index.paths))

    for batch in chunks(storable, batch_size):
        stats.batches += 1
        _store_batch(session_factory, list(batch), run_name, stats)

    stats.seconds = time.time() - start
    return stats


def replace_run_symbol_files(session: SA_Session,
                             run_id: int,
                             indexes: list[symbols_json.SymbolIndex],
                             file_path_to_id: dict[str, int],
                             trim_prefixes: list[str] | None
                             ) -> StageBStatistics:
    """
    Stage B, inside the run-scoped transaction. Replace the RunSymbolFile
    rows of 'run_id' with the membership described by 'indexes'. Every
    membership is validated against persisted rows: the File must exist for
    the (trimmed) path and its content_hash must equal the SymbolIndex's,
    and the SymbolIndex must exist (Stage A may have skipped it). Problems
    are logged and the single membership is skipped, the store goes on.
    """
    stats = StageBStatistics()

    session.query(RunSymbolFile) \
        .filter(RunSymbolFile.run_id == run_id) \
        .delete(synchronize_session=False)

    wanted: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for index in indexes:
        for path in index.paths:
            trimmed = trim_path_prefixes(path, trim_prefixes)
            file_id = file_path_to_id.get(trimmed)
            if file_id is None:
                stats.skipped_unknown_file += 1
                LOG.warning("Symbol indexed file '%s' (%s) of run %d has no "
                            "stored File, its symbols are not attached to "
                            "the run.", trimmed, index.language, run_id)
                continue
            wanted[file_id].add((index.content_hash, index.language))

    file_hashes: dict[int, str] = {}
    for chunk in chunks(sorted(wanted), QUERY_CHUNK_SIZE):
        file_hashes.update(session.query(File.id, File.content_hash)
                           .filter(File.id.in_(list(chunk))))

    index_ids = _existing_indexes(
        session, {h for pairs in wanted.values() for h, _ in pairs})

    rows = []
    for file_id, pairs in wanted.items():
        for content_hash, language in sorted(pairs):
            if file_hashes.get(file_id) != content_hash:
                stats.skipped_content_mismatch += 1
                LOG.warning("File %d of run %d has content %s, not the "
                            "indexed %s (%s), its symbols are not attached "
                            "to the run.", file_id, run_id,
                            file_hashes.get(file_id), content_hash, language)
                continue
            symbol_index_id = index_ids.get((content_hash, language))
            if symbol_index_id is None:
                stats.skipped_missing_index += 1
                LOG.warning("No symbol index for content %s (%s) of file %d "
                            "in run %d, its symbols are not attached to the "
                            "run.", content_hash, language, file_id, run_id)
                continue
            rows.append({'run_id': run_id, 'file_id': file_id,
                         'symbol_index_id': symbol_index_id})

    if rows:
        session.execute(sqlalchemy.insert(RunSymbolFile), rows)
    stats.memberships = len(rows)
    return stats


@dataclass(frozen=True)
class DefinitionCandidate:
    file_id: int
    filepath: str
    language: str
    line: int
    end_line: int | None
    kind: str
    scope: str | None
    scope_kind: str | None
    signature: str | None
    typeref: str | None


def find_definitions(session: SA_Session, run_id: int,
                     name: str) -> list[DefinitionCandidate]:
    """
    Candidate definitions of symbol 'name' in the CURRENT indexed files of
    run 'run_id' (through RunSymbolFile). Backend building block of the
    future jump-to-definition API; results are candidates, the same name
    may be defined in several files or several '#if' branches.
    """
    query = session.query(
        File.id, File.filepath, SymbolIndex.language,
        SymbolDefinition.line, SymbolDefinition.end_line,
        SymbolDefinition.kind, SymbolDefinition.scope,
        SymbolDefinition.scope_kind, SymbolDefinition.signature,
        SymbolDefinition.typeref) \
        .select_from(SymbolDefinition) \
        .join(SymbolIndex,
              SymbolIndex.id == SymbolDefinition.symbol_index_id) \
        .join(RunSymbolFile,
              RunSymbolFile.symbol_index_id == SymbolIndex.id) \
        .join(File, File.id == RunSymbolFile.file_id) \
        .filter(RunSymbolFile.run_id == run_id,
                SymbolDefinition.name == name) \
        .order_by(File.filepath, SymbolIndex.language, SymbolDefinition.line)

    return [DefinitionCandidate(*row) for row in query]


def symbol_indexes_of_file(session: SA_Session,
                           file_id: int) -> list[SymbolIndex]:
    """
    The symbol indexes describing the content of a stored File, regardless
    of run membership. This is how the source shown for a report (possibly
    an old content no current run has) finds its own index: the content
    hash of the File, not RunSymbolFile, identifies it.
    """
    return session.query(SymbolIndex) \
        .join(File, File.content_hash == SymbolIndex.content_hash) \
        .filter(File.id == file_id) \
        .order_by(SymbolIndex.language) \
        .all()
