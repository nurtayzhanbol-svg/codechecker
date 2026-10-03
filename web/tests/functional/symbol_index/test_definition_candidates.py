# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------
""" Tests for the getDefinitionCandidates() report viewer API. """


import json
import os
import shutil
import subprocess
import unittest

from codechecker_api.python.DBAccess_v6.ttypes import DetectionStatus, \
    Encoding, ReportFilter, RunFilter
from codechecker_api.python.shared.ttypes import ErrorCode, RequestFailed

from libtest import codechecker
from libtest import env


SHARED_H = """\
struct shared_s { int v; };
static inline int shared_fn(int x) { return x; }
"""

CAPI_C = """\
#include "shared.h"
int c_api(int x) { return shared_fn(x); }
#ifdef FAST
int cond_fn(void) { return 1; }
#else
int cond_fn(void) { return 2; }
#endif
"""

MAIN_CPP = """\
#include "shared.h"
int over(int x) { return x; }
double over(double x) { return x; }
int main() { return over(1); }
"""

OTHER_C = """\
int c_api(int x) { return x; }
int only_other(void) { return 0; }
"""

UTIL_V1 = """\
int old_only(void) { return 1; }
int common(void) {
  int z = 0;
  return 10 / z;
}
"""

UTIL_V2 = """\
int new_only(void) { return 2; }
int common(void) {
  int z = 1;
  return 10 / z;
}
"""


def write(path, content):
    with open(path, 'w', encoding="utf-8", errors="ignore") as f:
        f.write(content)


class TestDefinitionCandidates(unittest.TestCase):

    def setup_class(self):
        """ Analyze and store the test projects. """
        if not shutil.which('ctags'):
            raise unittest.SkipTest("Universal Ctags is not available.")

        global TEST_WORKSPACE
        TEST_WORKSPACE = env.get_workspace('symbol_index')
        os.environ['TEST_WORKSPACE'] = TEST_WORKSPACE

        codechecker_cfg = {
            'check_env': env.test_env(TEST_WORKSPACE),
            'workspace': TEST_WORKSPACE,
            'checkers': []
        }

        server_access = codechecker.start_or_get_server(auth_required=True)
        server_access['viewer_product'] = 'symbol_index'
        codechecker.add_test_package_product(server_access, TEST_WORKSPACE)
        codechecker_cfg.update(server_access)

        env.export_test_cfg(TEST_WORKSPACE,
                            {'codechecker_cfg': codechecker_cfg})

    def teardown_class(self):
        """ Clean up after the test. """
        global TEST_WORKSPACE

        check_env = env.import_test_cfg(TEST_WORKSPACE)[
            'codechecker_cfg']['check_env']
        codechecker.remove_test_package_product(TEST_WORKSPACE, check_env)

        print("Removing: " + TEST_WORKSPACE)
        shutil.rmtree(TEST_WORKSPACE, ignore_errors=True)

    def setup_method(self, _):
        self._test_workspace = os.environ['TEST_WORKSPACE']
        self._codechecker_cfg = env.import_codechecker_cfg(
            self._test_workspace)
        self._cc_client = env.setup_viewer_client(self._test_workspace)
        self.assertIsNotNone(self._cc_client)

    def _analyze_and_store(self, run_name, sources, symbol_index=True):
        """
        Analyze the given {file name: (compiler, content)} project with the
        Clang Static Analyzer and store it as 'run_name'. Return the run ID
        and the project directory.
        """
        proj_dir = os.path.join(self._test_workspace, 'proj_' + run_name)
        report_dir = os.path.join(self._test_workspace,
                                  'reports_' + run_name)
        os.makedirs(proj_dir, exist_ok=True)
        shutil.rmtree(report_dir, ignore_errors=True)

        write(os.path.join(proj_dir, 'shared.h'), SHARED_H)
        compile_commands = []
        for file_name, (compiler, content) in sources.items():
            write(os.path.join(proj_dir, file_name), content)
            compile_commands.append({
                'directory': proj_dir,
                'command': f"{compiler} -c {file_name} -o /dev/null",
                'file': file_name})

        compile_db = os.path.join(proj_dir, 'compile_commands.json')
        with open(compile_db, 'w', encoding="utf-8", errors="ignore") as f:
            json.dump(compile_commands, f)

        analyze_cmd = ['CodeChecker', 'analyze', compile_db,
                       '-o', report_dir, '--analyzers', 'clangsa']
        if symbol_index:
            analyze_cmd.append('--symbol-index')
        subprocess.run(analyze_cmd, cwd=proj_dir, check=False,
                       env=self._codechecker_cfg['check_env'])
        self.assertEqual(
            os.path.isfile(os.path.join(report_dir, 'symbols.json')),
            symbol_index)

        self._codechecker_cfg['reportdir'] = report_dir
        self.assertEqual(
            codechecker.store(self._codechecker_cfg, run_name), 0)

        runs = self._cc_client.getRunData(
            RunFilter(names=[run_name], exactMatch=True), None, 0, None)
        self.assertEqual(len(runs), 1)
        return runs[0].runId, proj_dir

    def _main_run(self):
        if not hasattr(TestDefinitionCandidates, '_main_run_id'):
            TestDefinitionCandidates._main_run_id = self._analyze_and_store(
                'main', {'capi.c': ('gcc', CAPI_C),
                         'main.cpp': ('g++', MAIN_CPP)})[0]
        return TestDefinitionCandidates._main_run_id

    def _candidates(self, run_id, name, limit=None):
        return self._cc_client.getDefinitionCandidates(run_id, name, limit)

    @staticmethod
    def _short(candidates):
        return [(os.path.basename(c.filePath), c.language, c.line, c.kind)
                for c in candidates]

    def test_ordinary_symbol(self):
        """ A unique function has one candidate which can be loaded. """
        run_id = self._main_run()
        candidates = self._candidates(run_id, 'c_api')

        self.assertEqual(self._short(candidates),
                         [('capi.c', 'c', 2, 'function')])
        candidate = candidates[0]
        self.assertTrue(candidate.filePath.endswith('/proj_main/capi.c'))
        self.assertEqual(candidate.endLine, 2)
        self.assertEqual(candidate.signature, '(int x)')
        self.assertIsNone(candidate.scope)

        source = self._cc_client.getSourceFileData(
            candidate.fileId, True, Encoding.DEFAULT)
        self.assertEqual(source.filePath, candidate.filePath)
        self.assertIn('int c_api(int x)',
                      source.fileContent.splitlines()[candidate.line - 1])

    def test_overloads_are_all_returned(self):
        run_id = self._main_run()
        self.assertEqual(
            self._short(self._candidates(run_id, 'over')),
            [('main.cpp', 'c++', 2, 'function'),
             ('main.cpp', 'c++', 3, 'function')])

    def test_shared_header_has_both_languages(self):
        run_id = self._main_run()
        self.assertEqual(
            self._short(self._candidates(run_id, 'shared_fn')),
            [('shared.h', 'c', 2, 'function'),
             ('shared.h', 'c++', 2, 'function')])

    def test_conditional_definitions_are_all_returned(self):
        run_id = self._main_run()
        self.assertEqual(
            self._short(self._candidates(run_id, 'cond_fn')),
            [('capi.c', 'c', 4, 'function'),
             ('capi.c', 'c', 6, 'function')])

    def test_limit_keeps_the_first_candidates(self):
        run_id = self._main_run()
        everything = self._candidates(run_id, 'cond_fn')
        self.assertEqual(self._candidates(run_id, 'cond_fn', 1),
                         everything[:1])
        self.assertEqual(self._candidates(run_id, 'cond_fn', 10000),
                         everything)

    def test_unknown_symbol_and_run(self):
        run_id = self._main_run()
        self.assertEqual(self._candidates(run_id, 'no_such_symbol'), [])
        self.assertEqual(self._candidates(2 ** 31, 'c_api'), [])

    def test_empty_symbol_name_is_rejected(self):
        run_id = self._main_run()
        for name in ('', '   '):
            with self.assertRaises(RequestFailed) as ctx:
                self._candidates(run_id, name)
            self.assertEqual(ctx.exception.errorCode, ErrorCode.GENERAL)

    def test_run_without_symbol_index(self):
        run_id, _ = self._analyze_and_store(
            'no_index', {'capi.c': ('gcc', CAPI_C)}, symbol_index=False)
        self.assertEqual(self._candidates(run_id, 'c_api'), [])

    def test_runs_are_isolated(self):
        main_run_id = self._main_run()
        other_run_id, _ = self._analyze_and_store(
            'other', {'other.c': ('gcc', OTHER_C)})

        self.assertEqual(self._candidates(main_run_id, 'only_other'), [])
        self.assertEqual(self._short(self._candidates(other_run_id, 'c_api')),
                         [('other.c', 'c', 1, 'function')])
        self.assertEqual(self._short(self._candidates(main_run_id, 'c_api')),
                         [('capi.c', 'c', 2, 'function')])

    def test_old_source_resolves_to_current_definitions(self):
        """
        A resolved report keeps showing the old version of a file, but the
        candidates come from the current version of the run.
        """
        run_id, _ = self._analyze_and_store(
            'source_change', {'util.c': ('gcc', UTIL_V1)})
        old_file_id = self._candidates(run_id, 'common')[0].fileId

        self._analyze_and_store('source_change',
                                {'util.c': ('gcc', UTIL_V2)})

        reports = self._cc_client.getRunResults(
            [run_id], 100, 0, None,
            ReportFilter(checkerName=['core.DivideZero']), None, False)
        self.assertEqual([(r.fileId, r.detectionStatus) for r in reports],
                         [(old_file_id, DetectionStatus.RESOLVED)])
        old_source = self._cc_client.getSourceFileData(
            old_file_id, True, Encoding.DEFAULT)
        self.assertIn('old_only', old_source.fileContent)

        self.assertEqual(self._candidates(run_id, 'old_only'), [])
        current = self._candidates(run_id, 'common') + \
            self._candidates(run_id, 'new_only')
        self.assertEqual(len(current), 2)
        self.assertEqual({c.fileId for c in current}, {current[0].fileId})
        self.assertNotEqual(current[0].fileId, old_file_id)
