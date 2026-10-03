# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
"""
Tests for the server-side symbol index storage (Stage A: SymbolIndex /
SymbolDefinition, Stage B: RunSymbolFile), the garbage collection rules that
keep symbol data alive, and the run-scoped definition look-up.

The tests use a real SQLite database file (not ':memory:') so that separate
sessions, foreign keys with ON DELETE CASCADE and parallel writers behave
like on a server.
"""
import datetime
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import sqlalchemy
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from codechecker_common import symbols_json

from codechecker_server.api import symbol_index_store as sis
from codechecker_server.database import db_cleanup
from codechecker_server.database.run_db_model import \
    Base, Checker, File, FileContent, Report, ReportPathData, \
    ReportPathDataFile, Run, RunSymbolFile, SymbolDefinition, SymbolIndex

H_A = 'a' * 64
H_B = 'b' * 64
H_C = 'c' * 64
H_MISSING = 'f' * 64


def definition(name, line, **kwargs):
    return symbols_json.Definition(name=name, kind='function', line=line,
                                   **kwargs)


def index(content_hash, language, paths, definitions):
    return symbols_json.SymbolIndex(content_hash=content_hash,
                                    language=language, paths=list(paths),
                                    definitions=list(definitions))


class SymbolIndexStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = create_engine(
            f"sqlite:///{os.path.join(self.tmp, 'test.sqlite')}")

        @event.listens_for(self.engine, "connect")
        def _fk_on(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.product = MagicMock()
        self.product.session_factory = self.session_factory
        self.product.endpoint = 'Default'

    def tearDown(self):
        self.engine.dispose()
        shutil.rmtree(self.tmp)

    # -- helpers -----------------------------------------------------------

    def add_content(self, *hashes):
        with self.session_factory() as session:
            for h in hashes:
                session.add(FileContent(h, b'content of ' + h.encode(),
                                        None))
            session.commit()

    def add_run(self, name):
        with self.session_factory() as session:
            run = Run(name, '1.0')
            session.add(run)
            session.commit()
            return run.id

    def add_files(self, path_to_hash):
        """Persist File rows, return {filepath: File.id}."""
        with self.session_factory() as session:
            ids = {}
            for path, h in path_to_hash.items():
                f = File(path, h, None, None)
                session.add(f)
                session.flush()
                ids[path] = f.id
            session.commit()
            return ids

    def count(self, model):
        with self.session_factory() as session:
            return session.query(model).count()

    def index_rows(self):
        with self.session_factory() as session:
            return sorted((i.content_hash, i.language)
                          for i in session.query(SymbolIndex))

    def membership(self, run_id):
        with self.session_factory() as session:
            return sorted((m.file_id, m.symbol_index_id) for m in
                          session.query(RunSymbolFile)
                          .filter(RunSymbolFile.run_id == run_id))

    def stage_a(self, indexes, run_name='run', **kwargs):
        return sis.store_symbol_indexes(self.session_factory, indexes,
                                        run_name, **kwargs)

    def stage_b(self, run_id, indexes, file_path_to_id, trim=None):
        with self.session_factory() as session:
            stats = sis.replace_run_symbol_files(
                session, run_id, indexes, file_path_to_id, trim)
            session.commit()
            return stats

    def gc(self):
        db_cleanup.remove_unused_files(self.product)

    # -- 1. models / schema --------------------------------------------------

    def test_schema_has_symbol_tables(self):
        inspector = sqlalchemy.inspect(self.engine)
        tables = inspector.get_table_names()
        self.assertIn('symbol_indexes', tables)
        self.assertIn('symbol_definitions', tables)
        self.assertIn('run_symbol_files', tables)

        uniques = inspector.get_unique_constraints('symbol_indexes')
        self.assertTrue(any(sorted(u['column_names']) ==
                            ['content_hash', 'language'] for u in uniques))
        pk = inspector.get_pk_constraint('run_symbol_files')
        self.assertEqual(sorted(pk['constrained_columns']),
                         ['file_id', 'run_id', 'symbol_index_id'])
        for table in ('symbol_indexes', 'symbol_definitions',
                      'run_symbol_files'):
            for fk in inspector.get_foreign_keys(table):
                self.assertEqual(fk['options'].get('ondelete'), 'CASCADE',
                                 f"{table}: {fk}")

    # -- 2. UNIQUE (content_hash, language) ---------------------------------

    def test_unique_content_hash_language(self):
        self.add_content(H_A)
        with self.session_factory() as session:
            session.add(SymbolIndex(H_A, 'c'))
            session.commit()
            session.add(SymbolIndex(H_A, 'c'))
            with self.assertRaises(sqlalchemy.exc.IntegrityError):
                session.commit()

    # -- 3. C and C++ indexes for the same content --------------------------

    def test_same_content_c_and_cpp_are_separate_indexes(self):
        self.add_content(H_A)
        stats = self.stage_a([
            index(H_A, 'c', ['/p/x.h'], [definition('f', 1)]),
            index(H_A, 'c++', ['/p/x.h'],
                  [definition('f', 1), definition('g', 2)])])
        self.assertEqual(stats.created, 2)
        self.assertEqual(self.index_rows(), [(H_A, 'c'), (H_A, 'c++')])
        self.assertEqual(self.count(SymbolDefinition), 3)

        run_id = self.add_run('r')
        ids = self.add_files({'/p/x.h': H_A})
        stats = self.stage_b(run_id, [
            index(H_A, 'c', ['/p/x.h'], []),
            index(H_A, 'c++', ['/p/x.h'], [])], ids)
        self.assertEqual(stats.memberships, 2)
        self.assertEqual(len(self.membership(run_id)), 2)

    # -- 4. duplicate content at different paths ----------------------------

    def test_same_content_at_two_paths_shares_one_index(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/one.h', '/q/one_copy.h'],
                         [definition('f', 1)])]
        self.stage_a(indexes)
        run_id = self.add_run('r')
        ids = self.add_files({'/p/one.h': H_A, '/q/one_copy.h': H_A})
        stats = self.stage_b(run_id, indexes, ids)

        self.assertEqual(self.count(SymbolIndex), 1)
        self.assertEqual(stats.memberships, 2)
        with self.session_factory() as session:
            paths = sorted(c.filepath for c in
                           sis.find_definitions(session, run_id, 'f'))
        self.assertEqual(paths, ['/p/one.h', '/q/one_copy.h'])

    # -- 5. definitions stored exactly once ---------------------------------

    def test_definitions_are_inserted_once_per_index(self):
        self.add_content(H_A)
        defs = [definition('f', 1, end_line=3, signature='(int)',
                           typeref='typename:int'),
                definition('g', 5, scope='ns', scope_kind='namespace')]
        self.stage_a([index(H_A, 'c++', ['/p/x.cpp'], defs)])
        self.stage_a([index(H_A, 'c++', ['/p/x.cpp'], defs)])
        self.stage_a([index(H_A, 'c++', ['/other/x.cpp'], defs)])
        self.assertEqual(self.count(SymbolIndex), 1)
        self.assertEqual(self.count(SymbolDefinition), 2)
        with self.session_factory() as session:
            g = session.query(SymbolDefinition) \
                .filter(SymbolDefinition.name == 'g').one()
            self.assertEqual((g.scope, g.scope_kind, g.end_line),
                             ('ns', 'namespace', None))

    # -- 6. unchanged re-store is idempotent --------------------------------

    def test_unchanged_restore_is_idempotent(self):
        self.add_content(H_A, H_B)
        indexes = [index(H_A, 'c', ['/p/a.c'], [definition('a', 1)]),
                   index(H_B, 'c', ['/p/b.h'], [definition('b', 1)])]
        run_id = self.add_run('r')
        ids = self.add_files({'/p/a.c': H_A, '/p/b.h': H_B})

        first = self.stage_a(indexes)
        self.stage_b(run_id, indexes, ids)
        before = (self.count(SymbolIndex), self.count(SymbolDefinition),
                  self.membership(run_id))

        second = self.stage_a(indexes)
        self.stage_b(run_id, indexes, ids)
        after = (self.count(SymbolIndex), self.count(SymbolDefinition),
                 self.membership(run_id))

        self.assertEqual((first.created, first.reused), (2, 0))
        self.assertEqual((second.created, second.reused), (0, 2))
        self.assertEqual(before, after)

    # -- 7. changed content re-store removes stale membership ---------------

    def test_changed_restore_replaces_membership(self):
        self.add_content(H_A, H_B, H_C)
        run_id = self.add_run('r')
        v1 = [index(H_A, 'c', ['/p/a.c'], [definition('a1', 1)]),
              index(H_B, 'c', ['/p/b.h'], [definition('b', 1)])]
        ids1 = self.add_files({'/p/a.c': H_A, '/p/b.h': H_B})
        self.stage_a(v1)
        self.stage_b(run_id, v1, ids1)

        # a.c changed (new hash C), b.h unchanged. The store maps the path
        # to the File row of the new content.
        v2 = [index(H_C, 'c', ['/p/a.c'], [definition('a2', 1)]),
              index(H_B, 'c', ['/p/b.h'], [definition('b', 1)])]
        ids2 = dict(ids1, **self.add_files({'/p/a.c': H_C}))
        self.stage_a(v2)
        stats = self.stage_b(run_id, v2, ids2)

        self.assertEqual(stats.memberships, 2)
        with self.session_factory() as session:
            self.assertEqual(
                [c.filepath for c in
                 sis.find_definitions(session, run_id, 'a1')], [])
            self.assertEqual(
                [c.filepath for c in
                 sis.find_definitions(session, run_id, 'a2')], ['/p/a.c'])
        self.assertNotIn(ids1['/p/a.c'],
                         [f for f, _ in self.membership(run_id)])

    # -- 8. --force rebuilds membership (run deleted, ID may be reused) -----

    def test_force_restore_rebuilds_membership(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/a.c'], [definition('a', 1)])]
        ids = self.add_files({'/p/a.c': H_A})
        run_id = self.add_run('r')
        self.stage_a(indexes)
        self.stage_b(run_id, indexes, ids)

        with self.session_factory() as session:
            session.query(Run).filter(Run.id == run_id).delete()
            session.commit()
        self.assertEqual(self.membership(run_id), [])
        self.assertEqual(self.count(SymbolIndex), 1)

        new_run_id = self.add_run('r')
        self.stage_a(indexes)
        stats = self.stage_b(new_run_id, indexes, ids)
        self.assertEqual(stats.memberships, 1)
        self.assertEqual(len(self.membership(new_run_id)), 1)

    # -- 9. missing FileContent is filtered before insert -------------------

    def test_missing_file_content_is_skipped_without_rollback(self):
        self.add_content(H_A, H_B)
        with self.assertLogs('server', level='WARNING') as logs:
            stats = self.stage_a([
                index(H_A, 'c', ['/p/a.c'], [definition('a', 1)]),
                index(H_MISSING, 'c', ['/p/gone.c'], [definition('x', 1)]),
                index(H_B, 'c', ['/p/b.h'], [definition('b', 1)])],
                batch_size=50)
        self.assertEqual(stats.skipped_missing_content, 1)
        self.assertEqual(stats.created, 2)
        self.assertEqual(self.index_rows(), [(H_A, 'c'), (H_B, 'c')])
        self.assertEqual(self.count(SymbolDefinition), 2)
        self.assertTrue(any(H_MISSING in line and '/p/gone.c' in line
                            for line in logs.output))

    # -- 10. missing File mapping is logged and skipped ---------------------

    def test_missing_file_mapping_is_skipped(self):
        self.add_content(H_A, H_B)
        indexes = [index(H_A, 'c', ['/p/a.c'], [definition('a', 1)]),
                   index(H_B, 'c', ['/p/b.h'], [definition('b', 1)])]
        self.stage_a(indexes)
        run_id = self.add_run('r')
        ids = self.add_files({'/p/a.c': H_A})  # b.h never stored
        with self.assertLogs('server', level='WARNING') as logs:
            stats = self.stage_b(run_id, indexes, ids)
        self.assertEqual(stats.memberships, 1)
        self.assertEqual(stats.skipped_unknown_file, 1)
        self.assertTrue(any('/p/b.h' in line for line in logs.output))

    def test_trim_path_prefixes_are_applied_to_symbol_paths(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/build/root/p/a.c'],
                         [definition('a', 1)])]
        self.stage_a(indexes)
        run_id = self.add_run('r')
        # MassStoreRun stores File rows under the same trimmed path.
        ids = self.add_files({'p/a.c': H_A})
        stats = self.stage_b(run_id, indexes, ids, trim=['/build/root'])
        self.assertEqual(stats.memberships, 1)

    # -- 11. File.content_hash == SymbolIndex.content_hash invariant --------

    def test_content_hash_mismatch_is_rejected(self):
        self.add_content(H_A, H_B)
        # symbols.json claims a.c has content A, but the stored File has B.
        indexes = [index(H_A, 'c', ['/p/a.c'], [definition('a', 1)])]
        self.stage_a(indexes)
        run_id = self.add_run('r')
        ids = self.add_files({'/p/a.c': H_B})
        with self.assertLogs('server', level='WARNING'):
            stats = self.stage_b(run_id, indexes, ids)
        self.assertEqual(stats.memberships, 0)
        self.assertEqual(stats.skipped_content_mismatch, 1)

    def test_index_missing_after_stage_a_is_skipped(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/a.c'], [definition('a', 1)])]
        run_id = self.add_run('r')
        ids = self.add_files({'/p/a.c': H_A})
        # Stage A never ran for this content.
        with self.assertLogs('server', level='WARNING'):
            stats = self.stage_b(run_id, indexes, ids)
        self.assertEqual(stats.skipped_missing_index, 1)
        self.assertEqual(stats.memberships, 0)

    # -- 12. indexed clean source/header survives GC ------------------------

    def test_indexed_file_without_report_survives_gc(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/clean.h'], [definition('a', 1)])]
        self.stage_a(indexes)
        run_id = self.add_run('r')
        ids = self.add_files({'/p/clean.h': H_A})
        self.stage_b(run_id, indexes, ids)

        self.gc()

        self.assertEqual(self.count(File), 1)
        self.assertEqual(self.count(FileContent), 1)
        self.assertEqual(self.count(SymbolIndex), 1)
        self.assertEqual(self.count(SymbolDefinition), 1)
        self.assertEqual(len(self.membership(run_id)), 1)

    # -- 13./14. shared index and run deletion ------------------------------

    def test_shared_index_survives_until_last_run_is_deleted(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/x.h'], [definition('a', 1)])]
        self.stage_a(indexes)
        r1, r2 = self.add_run('r1'), self.add_run('r2')
        ids = self.add_files({'/p/x.h': H_A})
        self.stage_b(r1, indexes, ids)
        self.stage_b(r2, indexes, ids)

        with self.session_factory() as session:
            session.query(Run).filter(Run.id == r1).delete()
            session.commit()
        self.gc()
        self.assertEqual(self.count(SymbolIndex), 1)
        self.assertEqual(self.count(RunSymbolFile), 1)
        self.assertEqual(self.count(File), 1)

        with self.session_factory() as session:
            session.query(Run).filter(Run.id == r2).delete()
            session.commit()
        self.gc()
        self.assertEqual(self.count(RunSymbolFile), 0)
        self.assertEqual(self.count(File), 0)
        self.assertEqual(self.count(SymbolIndex), 0)
        self.assertEqual(self.count(SymbolDefinition), 0)
        self.assertEqual(self.count(FileContent), 0)

    # -- 15./16. Option SR: old file content still shown by a report --------

    def _seed_old_and_new_version(self):
        """
        /p/a.c had content A (indexed, then changed to B). The old File is
        still referenced by a report path (resolved report), the run's
        membership points to the new File only.
        """
        self.add_content(H_A, H_B)
        run_id = self.add_run('r')
        old = self.add_files({'/p/a.c': H_A})['/p/a.c']
        self.stage_a([index(H_A, 'c', ['/p/a.c'], [definition('old', 1)])])
        new_idx = [index(H_B, 'c', ['/p/a.c'], [definition('new', 1)])]
        new = self.add_files({'/p/a.c': H_B})['/p/a.c']
        self.stage_a(new_idx)
        self.stage_b(run_id, new_idx, {'/p/a.c': new})
        return run_id, old, new

    def _reference_file_from_report_path(self, run_id, file_id):
        """Attach a (resolved) report whose bug path shows the file."""
        with self.session_factory() as session:
            checker = Checker('clangsa', 'core.DivideZero', 3)
            session.add(checker)
            session.flush()
            now = datetime.datetime.now()
            report = Report(file_id, run_id, 'h' * 32, checker, 1, 1, 1,
                            'msg', 'resolved', 'unreviewed', None, None,
                            None, False, now, now)
            report.id = 1
            session.add(report)
            session.flush()
            session.add(ReportPathData(report.id, [ReportPathData.Item(
                1, 1, 1, 1, file_id, 'event', 'msg')]))
            session.execute(ReportPathDataFile.insert().values(
                report_path_data_id=report.id, file_id=file_id))
            session.commit()

    def test_option_sr_keeps_index_of_old_file_shown_by_report(self):
        run_id, old, _ = self._seed_old_and_new_version()
        self._reference_file_from_report_path(run_id, old)

        self.gc()

        self.assertEqual(self.count(File), 2)
        self.assertEqual(self.index_rows(), [(H_A, 'c'), (H_B, 'c')])
        with self.session_factory() as session:
            old_indexes = sis.symbol_indexes_of_file(session, old)
            self.assertEqual([(i.content_hash, i.language)
                              for i in old_indexes], [(H_A, 'c')])
            # Destination candidates stay scoped to the run's current files.
            self.assertEqual(
                [c.filepath for c in
                 sis.find_definitions(session, run_id, 'old')], [])
            self.assertEqual(
                [c.filepath for c in
                 sis.find_definitions(session, run_id, 'new')], ['/p/a.c'])

    def test_old_file_and_index_are_collected_when_not_live(self):
        _, old, new = self._seed_old_and_new_version()

        self.gc()

        with self.session_factory() as session:
            remaining = [f.id for f in session.query(File)]
        self.assertEqual(remaining, [new])
        self.assertNotIn(old, remaining)
        self.assertEqual(self.index_rows(), [(H_B, 'c')])
        with self.session_factory() as session:
            hashes = sorted(c.content_hash
                            for c in session.query(FileContent))
        self.assertEqual(hashes, [H_B])

    # -- 17. partial failure leaves collectable orphans ---------------------

    def test_orphan_index_after_failed_run_transaction_is_collected(self):
        self.add_content(H_A)
        # Stage A committed, then the run transaction (Stage B) failed and
        # the File rows are not referenced by anything.
        self.stage_a([index(H_A, 'c', ['/p/a.c'], [definition('a', 1)])])
        self.add_files({'/p/a.c': H_A})
        self.assertEqual(self.count(SymbolIndex), 1)

        self.gc()

        self.assertEqual(self.count(File), 0)
        self.assertEqual(self.count(SymbolIndex), 0)
        self.assertEqual(self.count(SymbolDefinition), 0)
        self.assertEqual(self.count(FileContent), 0)

    # -- 18. concurrent duplicate Stage A insert ----------------------------

    def test_concurrent_stage_a_same_identity_creates_one_index(self):
        self.add_content(H_A)
        indexes = [index(H_A, 'c', ['/p/a.c'],
                         [definition('a', 1), definition('b', 2)])]
        barrier = threading.Barrier(2, timeout=10)
        original = sis._existing_indexes  # pylint: disable=protected-access
        first_attempt = threading.local()

        def racing_read(session, hashes):
            # Both writers see "nothing exists" before either inserts.
            result = original(session, hashes)
            if not getattr(first_attempt, 'done', False):
                first_attempt.done = True
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return result

        results, errors = [], []

        def worker(name):
            try:
                with patch.object(sis, '_existing_indexes', racing_read):
                    results.append(self.stage_a(indexes, run_name=name))
            except Exception as ex:  # pylint: disable=broad-except
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ('w1', 'w2')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        self.assertEqual(errors, [])
        self.assertEqual(self.count(SymbolIndex), 1)
        self.assertEqual(self.count(SymbolDefinition), 2)
        created = sum(r.created for r in results)
        reused = sum(r.reused for r in results)
        self.assertEqual((created, reused), (1, 1))
        self.assertGreaterEqual(sum(r.retries for r in results), 1)

    # -- 19. terminal query is run-scoped -----------------------------------

    def test_find_definitions_is_run_scoped(self):
        self.add_content(H_A, H_B)
        r1, r2 = self.add_run('r1'), self.add_run('r2')
        i1 = [index(H_A, 'c', ['/p/a.c'], [definition('shared', 1)])]
        i2 = [index(H_B, 'c', ['/q/a.c'], [definition('shared', 7)])]
        self.stage_a(i1 + i2)
        ids1 = self.add_files({'/p/a.c': H_A})
        ids2 = self.add_files({'/q/a.c': H_B})
        self.stage_b(r1, i1, ids1)
        self.stage_b(r2, i2, ids2)

        with self.session_factory() as session:
            c1 = sis.find_definitions(session, r1, 'shared')
            c2 = sis.find_definitions(session, r2, 'shared')
            none = sis.find_definitions(session, r1, 'nonexistent')
        self.assertEqual([(c.filepath, c.line) for c in c1],
                         [('/p/a.c', 1)])
        self.assertEqual([(c.filepath, c.line) for c in c2],
                         [('/q/a.c', 7)])
        self.assertEqual(c1[0].file_id, ids1['/p/a.c'])
        self.assertEqual(c1[0].language, 'c')
        self.assertEqual(none, [])

    def test_find_definitions_limit_keeps_the_first_rows_in_order(self):
        self.add_content(H_A)
        run_id = self.add_run('r')
        idx = [index(H_A, 'c', ['/p/a.c'],
                     [definition('f', 9), definition('f', 2),
                      definition('f', 5)])]
        self.stage_a(idx)
        self.stage_b(run_id, idx, self.add_files({'/p/a.c': H_A}))

        with self.session_factory() as session:
            unlimited = sis.find_definitions(session, run_id, 'f')
            limited = sis.find_definitions(session, run_id, 'f', limit=2)
            unknown = sis.find_definitions(session, run_id, 'g', limit=2)
        self.assertEqual([c.line for c in unlimited], [2, 5, 9])
        self.assertEqual([c.line for c in limited], [2, 5])
        self.assertEqual(unknown, [])

    def test_find_definitions_orders_same_line_ties_totally(self):
        self.add_content(H_A, H_B)
        run_id = self.add_run('r')
        same_line = [
            symbols_json.Definition(name='f', kind='variable', line=3,
                                    scope='a', scope_kind='namespace'),
            symbols_json.Definition(name='f', kind='variable', line=3),
            symbols_json.Definition(name='f', kind='function', line=3)]
        idx = [index(H_A, 'c++', ['/p/a.cpp'], same_line),
               index(H_B, 'c++', ['/p/a.cpp'], same_line)]
        self.stage_a(idx)

        # The same path with two contents in one run only differs in File.id.
        with self.session_factory() as session:
            files = [File('/p/a.cpp', h, None, None) for h in (H_A, H_B)]
            session.add_all(files)
            session.flush()
            for f in files:
                index_id = session.query(SymbolIndex.id).filter(
                    SymbolIndex.content_hash == f.content_hash).scalar()
                session.add(RunSymbolFile(run_id=run_id, file_id=f.id,
                                          symbol_index_id=index_id))
            file_ids = [f.id for f in files]
            session.commit()

        with self.session_factory() as session:
            rows = sis.find_definitions(session, run_id, 'f')
            again = sis.find_definitions(session, run_id, 'f')
            first_two = sis.find_definitions(session, run_id, 'f', limit=2)

        self.assertEqual([(c.kind, c.file_id) for c in rows],
                         [('function', file_ids[0]),
                          ('function', file_ids[1]),
                          ('variable', file_ids[0]),
                          ('variable', file_ids[0]),
                          ('variable', file_ids[1]),
                          ('variable', file_ids[1])])
        # Equal kind and File: SymbolDefinition.id, i.e. stored order.
        self.assertEqual([c.scope for c in rows if c.kind == 'variable'],
                         ['a', None, 'a', None])
        self.assertEqual(rows, again)
        self.assertEqual(first_two, rows[:2])

    # -- load_symbol_indexes -------------------------------------------------

    def test_load_symbol_indexes_merges_report_directories(self):
        doc = {'version': 1, 'indexes': [{
            'content_hash': H_A, 'language': 'c', 'paths': ['/p/x.h'],
            'definitions': [{'name': 'f', 'kind': 'function', 'line': 1}]}]}
        for sub, path in (('one', '/p/x.h'), ('two', '/q/x.h')):
            d = os.path.join(self.tmp, 'reports', sub)
            os.makedirs(d)
            doc['indexes'][0]['paths'] = [path]
            with open(os.path.join(d, 'symbols.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(doc, f)

        loaded = sis.load_symbol_indexes(os.path.join(self.tmp, 'reports'))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].paths, ['/p/x.h', '/q/x.h'])
        self.assertEqual(len(loaded[0].definitions), 1)

    def test_load_symbol_indexes_rejects_malformed_document(self):
        d = os.path.join(self.tmp, 'reports')
        os.makedirs(d)
        with open(os.path.join(d, 'symbols.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'version': 99, 'indexes': []}, f)
        with self.assertRaises(symbols_json.SymbolsJsonError):
            sis.load_symbol_indexes(d)

    def test_load_symbol_indexes_without_file(self):
        d = os.path.join(self.tmp, 'reports')
        os.makedirs(d)
        self.assertEqual(sis.load_symbol_indexes(d), [])
