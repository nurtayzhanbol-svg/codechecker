# -------------------------------------------------------------------------
#
#  Part of the CodeChecker project, under the Apache License v2.0 with
#  LLVM Exceptions. See LICENSE for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# -------------------------------------------------------------------------

""" Tests of the Universal Ctags based symbol index (symbols.json). """


import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from codechecker_analyzer import symbol_index
from codechecker_analyzer.buildlog import log_parser
from codechecker_analyzer.symbol_index import Definition, IndexInput, \
    SymbolIndexError


def _universal_ctags_with_json() -> str | None:
    """Return a usable Universal Ctags executable or None."""
    for candidate in filter(None, [os.environ.get('CC_TEST_CTAGS'),
                                   shutil.which('ctags')]):
        try:
            return symbol_index.Ctags.find(candidate).binary
        except SymbolIndexError:
            continue
    return None


CTAGS = _universal_ctags_with_json()
GCC = shutil.which('gcc')
GXX = shutil.which('g++')


class FakeCtags(symbol_index.Ctags):
    """Ctags stand-in returning canned raw tags, recording the calls."""

    def __init__(self, tags_by_path: dict[str, list[dict]]):
        super().__init__('fake-ctags')
        self.tags_by_path = tags_by_path
        self.calls: list[tuple[str, list[str]]] = []

    def tag_files(self, ctags_language, paths):
        paths = list(paths)
        self.calls.append((ctags_language, paths))
        return {p: self.tags_by_path.get(p, []) for p in paths}


class ExplicitIncludeDirsTest(unittest.TestCase):
    """Extraction of explicitly configured include directories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / 'inc').mkdir()
        (self.tmp / 'vendor').mkdir()
        (self.tmp / 'build').mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_joined_and_separated_forms(self):
        dirs = symbol_index.get_explicit_include_dirs(
            ['-Iinc', '-isystem', 'vendor', '-iquote', '/abs/q',
             '-idirafter/abs/after', '-DFOO', '-include', 'x.h',
             '-I'],  # A trailing flag without a value is ignored.
            str(self.tmp))
        self.assertEqual(dirs, [self.tmp / 'inc',
                                self.tmp / 'vendor',
                                Path('/abs/q'),
                                Path('/abs/after')])

    def test_relative_and_dotdot_paths_resolve_from_directory(self):
        dirs = symbol_index.get_explicit_include_dirs(
            ['-I../inc', '-isystem', './../vendor/../vendor'],
            str(self.tmp / 'build'))
        self.assertEqual(dirs, [self.tmp / 'inc', self.tmp / 'vendor'])

    def test_symlinked_include_dir_is_resolved(self):
        link = self.tmp / 'inc_link'
        try:
            link.symlink_to(self.tmp / 'inc')
        except OSError:
            self.skipTest("symlinks are not supported here")
        dirs = symbol_index.get_explicit_include_dirs(
            [f'-I{link}'], str(self.tmp))
        self.assertEqual(dirs, [self.tmp / 'inc'])


class DependencyFilterTest(unittest.TestCase):
    """Policy: drop implicit include roots unless explicitly configured."""

    IMPLICIT = [Path('/usr/lib/gcc/x86_64-linux-gnu/11/include'),
                Path('/usr/local/include'),
                Path('/usr/include/x86_64-linux-gnu'),
                Path('/usr/include')]

    def keep(self, path, explicit=()):
        return symbol_index.is_indexable_dependency(
            Path(path), self.IMPLICIT, [Path(e) for e in explicit])

    def test_project_header_kept(self):
        self.assertTrue(self.keep('/project/include/project.h',
                                  ['/project/include']))

    def test_project_header_kept_without_explicit_dirs(self):
        self.assertTrue(self.keep('/project/src/local.h'))

    def test_vendored_isystem_header_kept(self):
        self.assertTrue(self.keep('/project/vendor/include/vendor.h',
                                  ['/project/vendor/include']))

    def test_standard_library_header_removed(self):
        self.assertFalse(self.keep('/usr/include/stdio.h'))
        self.assertFalse(self.keep('/usr/include/c++/11/vector'))

    def test_toolchain_header_removed(self):
        self.assertFalse(self.keep(
            '/usr/lib/gcc/x86_64-linux-gnu/11/include/stddef.h'))

    def test_explicit_dir_nested_in_implicit_root_wins(self):
        self.assertTrue(self.keep('/usr/include/libxml2/libxml/tree.h',
                                  ['/usr/include/libxml2']))
        # The explicit directory does not cover sibling system headers.
        self.assertFalse(self.keep('/usr/include/stdio.h',
                                   ['/usr/include/libxml2']))

    def test_explicit_dir_equal_to_implicit_root_wins(self):
        self.assertTrue(self.keep('/usr/include/stdio.h', ['/usr/include']))

    def test_explicit_dir_above_implicit_root_does_not_win(self):
        self.assertFalse(self.keep('/usr/include/stdio.h', ['/usr']))

    def test_prefix_is_structural_not_textual(self):
        self.assertTrue(self.keep('/usr/include2/foo.h'))
        self.assertTrue(self.keep('/usr/local/include-extra/foo.h'))


class NormalizeTagTest(unittest.TestCase):
    """Conversion of raw Universal Ctags JSON tags to Definitions."""

    RAW = {"_type": "tag", "name": "foo", "path": "/p/a.cpp",
           "pattern": "/^int foo(int x) {$/", "file": True, "line": 3,
           "typeref": "typename:int", "kind": "function",
           "signature": "(int x)", "scope": "Utils",
           "scopeKind": "namespace", "roles": "def", "end": 5}

    def test_all_fields(self):
        self.assertEqual(
            symbol_index.normalize_tag(self.RAW),
            Definition(name='foo', kind='function', line=3, end_line=5,
                       scope='Utils', scope_kind='namespace',
                       signature='(int x)', typeref='typename:int'))

    def test_optional_fields_are_null(self):
        tag = {"_type": "tag", "name": "g", "path": "/p/a.c",
               "pattern": "/^int g;$/", "line": 9, "kind": "variable"}
        self.assertEqual(
            symbol_index.normalize_tag(tag),
            Definition(name='g', kind='variable', line=9))

    def test_reference_tags_are_dropped(self):
        tag = dict(self.RAW, name='stdio.h', kind='header', roles='system')
        self.assertIsNone(symbol_index.normalize_tag(tag))

    def test_incomplete_tags_are_dropped(self):
        for missing in ('name', 'kind', 'line'):
            tag = dict(self.RAW)
            del tag[missing]
            self.assertIsNone(symbol_index.normalize_tag(tag), missing)

    def test_ctags_only_fields_are_not_in_schema(self):
        fields = set(Definition.__dataclass_fields__)
        self.assertEqual(fields, {'name', 'kind', 'line', 'end_line',
                                  'scope', 'scope_kind', 'signature',
                                  'typeref'})
        self.assertTrue(fields.isdisjoint({'pattern', 'file', 'path',
                                           'column'}))


class IdentityTest(unittest.TestCase):
    """(content hash, language) grouping."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ('a/common.h', 'b/common.h', 'other.h'):
            (self.tmp / name).parent.mkdir(exist_ok=True)
        (self.tmp / 'a/common.h').write_text('int shared(void);\n')
        (self.tmp / 'b/common.h').write_text('int shared(void);\n')
        (self.tmp / 'other.h').write_text('int other(void);\n')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_same_bytes_same_language_share_one_entry(self):
        groups = symbol_index.group_by_identity([
            IndexInput(str(self.tmp / 'a/common.h'), 'c'),
            IndexInput(str(self.tmp / 'b/common.h'), 'c'),
            IndexInput(str(self.tmp / 'other.h'), 'c')])
        self.assertEqual(len(groups), 2)
        common = next(paths for (_, lang), paths in groups.items()
                      if len(paths) == 2)
        self.assertEqual(common, sorted([str(self.tmp / 'a/common.h'),
                                         str(self.tmp / 'b/common.h')]))

    def test_same_bytes_different_language_are_separate(self):
        groups = symbol_index.group_by_identity([
            IndexInput(str(self.tmp / 'a/common.h'), 'c'),
            IndexInput(str(self.tmp / 'a/common.h'), 'c++')])
        hashes = {content_hash for content_hash, _ in groups}
        languages = sorted(lang for _, lang in groups)
        self.assertEqual(len(hashes), 1)
        self.assertEqual(languages, ['c', 'c++'])
        self.assertEqual(len(next(iter(hashes))), 64)  # SHA-256 hex digest.


class BuildIndexesTest(unittest.TestCase):
    """Per-language batching, normalization and deterministic output."""

    def test_batched_per_language_and_sorted(self):
        tag = {"_type": "tag", "path": "/p/z.h", "line": 2, "kind": "struct",
               "name": "pt", "roles": "def"}
        tag_ref = dict(tag, name="stdio.h", kind="header", roles="system")
        ctags = FakeCtags({
            '/p/z.h': [tag, tag_ref, dict(tag, name="a", line=1)],
            '/p/m.c': [dict(tag, path='/p/m.c', name='main',
                            kind='function', signature='(void)', end=9)],
        })
        groups = {
            ('hh', 'c++'): ['/p/z.h'],
            ('hh', 'c'): ['/p/z.h'],
            ('mm', 'c'): ['/p/m.c'],
            ('hh2', 'c'): ['/p/m2.c', '/p/z2.h'],  # no tags at all
        }

        indexes = symbol_index.build_indexes(groups, ctags)

        # One Ctags invocation per language, files sorted.
        self.assertEqual(ctags.calls, [('C', ['/p/m.c', '/p/m2.c', '/p/z.h']),
                                       ('C++', ['/p/z.h'])])

        doc = symbol_index.to_json(indexes)
        self.assertEqual(doc['version'], symbol_index.SCHEMA_VERSION)
        self.assertEqual([(i['content_hash'], i['language'])
                          for i in doc['indexes']],
                         [('hh', 'c'), ('hh', 'c++'), ('hh2', 'c'),
                          ('mm', 'c')])

        z_c = doc['indexes'][0]
        self.assertEqual([d['name'] for d in z_c['definitions']],
                         ['a', 'pt'])  # reference dropped
        self.assertEqual(z_c['definitions'][1], {
            'name': 'pt', 'kind': 'struct', 'line': 2, 'end_line': None,
            'scope': None, 'scope_kind': None, 'signature': None,
            'typeref': None})
        self.assertEqual(doc['indexes'][2]['definitions'], [])
        self.assertEqual(doc['indexes'][3]['definitions'][0]['signature'],
                         '(void)')

    def test_definitions_are_in_line_order_not_name_order(self):
        tag = {"_type": "tag", "path": "/p/o.c", "kind": "function",
               "roles": "def"}
        ctags = FakeCtags({'/p/o.c': [
            dict(tag, name='zebra', line=1),
            dict(tag, name='mango', line=2),
            dict(tag, name='apple', line=4),
            dict(tag, name='apple', line=4),  # duplicate tag
        ]})
        indexes = symbol_index.build_indexes({('h', 'c'): ['/p/o.c']}, ctags)
        self.assertEqual([(d.name, d.line) for d in indexes[0].definitions],
                         [('zebra', 1), ('mango', 2), ('apple', 4)])

    def test_same_line_tie_with_null_fields_is_ordered(self):
        # namespace a { int f; } int f;
        tag = {"_type": "tag", "path": "/p/t.cpp", "name": "f", "line": 1,
               "end": 1, "kind": "variable", "typeref": "typename:int",
               "roles": "def"}
        scoped = dict(tag, scope='a', scopeKind='namespace')
        expected = [(None, None), ('a', 'namespace')]
        for raw in ([tag, scoped], [scoped, tag]):
            indexes = symbol_index.build_indexes(
                {('h', 'c++'): ['/p/t.cpp']}, FakeCtags({'/p/t.cpp': raw}))
            self.assertEqual([(d.scope, d.scope_kind)
                              for d in indexes[0].definitions], expected)

    def test_unmapped_language_is_skipped(self):
        ctags = FakeCtags({})
        indexes = symbol_index.build_indexes(
            {('h', 'fortran'): ['/p/a.f']}, ctags)
        self.assertEqual(ctags.calls, [])
        self.assertEqual(len(indexes), 1)
        self.assertEqual(indexes[0].definitions, [])

    def test_command_never_forwards_compiler_options(self):
        cmd = symbol_index.Ctags('ctags').command('C++')
        self.assertIn('--language-force=C++', cmd)
        self.assertIn('--output-format=json', cmd)
        self.assertIn('--options=NONE', cmd)
        self.assertFalse(any(a.startswith(('-I', '-D', '-std'))
                             for a in cmd))


class CtagsValidationTest(unittest.TestCase):
    """Ctags executable discovery and feature validation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fake_binary(self, version_text: str, features: str) -> str:
        script = self.tmp / 'ctags'
        script.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            case "$1" in
              --version) echo '{version_text}' ;;
              --list-features) printf '#NAME\\n{features}\\n' ;;
              *) exit 0 ;;
            esac
            """))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def test_missing_binary(self):
        with self.assertRaisesRegex(SymbolIndexError,
                                    'not found.*--ctags-binary'):
            symbol_index.Ctags.find(str(self.tmp / 'nope'))

    def test_not_universal_ctags(self):
        binary = self.fake_binary('Exuberant Ctags 5.8', 'regex')
        with self.assertRaisesRegex(SymbolIndexError,
                                    'not Universal Ctags'):
            symbol_index.Ctags.find(binary)

    def test_universal_ctags_without_json(self):
        binary = self.fake_binary('Universal Ctags 5.9.0', 'regex')
        with self.assertRaisesRegex(SymbolIndexError, 'JSON'):
            symbol_index.Ctags.find(binary)

    @unittest.skipIf(CTAGS is None, "Universal Ctags with JSON is required")
    def test_real_ctags_is_accepted(self):
        ctags = symbol_index.Ctags.find(CTAGS)
        self.assertEqual(ctags.binary, CTAGS)


@unittest.skipIf(CTAGS is None or GXX is None,
                 "Universal Ctags with JSON and g++ are required")
class RealCtagsOrderingTest(unittest.TestCase):
    """Definition ordering of real Ctags output in a generated symbols.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def generate(self, source: str) -> list[tuple]:
        (self.tmp / 'tie.cpp').write_text(source)
        actions, _ = log_parser.parse_unique_log([
            {"directory": str(self.tmp),
             "command": f"{GXX} -c tie.cpp -o tie.o",
             "file": "tie.cpp"}])
        output = self.tmp / 'symbols.json'
        symbol_index.generate(actions, symbol_index.Ctags.find(CTAGS),
                              output, jobs=1)
        with open(output, encoding='utf-8') as f:
            doc = json.load(f)
        self.assertEqual(len(doc['indexes']), 1)
        return [(d['name'], d['line'], d['scope'])
                for d in doc['indexes'][0]['definitions']]

    def test_scoped_and_unscoped_same_name_on_one_line(self):
        source = 'namespace a { int f; } int f;\n'
        definitions = self.generate(source)
        self.assertEqual(definitions,
                         [('a', 1, None), ('f', 1, None), ('f', 1, 'a')])
        self.assertEqual(self.generate(source), definitions)

    def test_line_order_differs_from_name_order(self):
        definitions = self.generate(textwrap.dedent("""\
            int zebra(void) { return 1; }
            int mango;
            int apple(void) { return 2; }
            """))
        self.assertEqual([name for name, _, _ in definitions],
                         ['zebra', 'mango', 'apple'])


@unittest.skipIf(CTAGS is None or GCC is None or GXX is None,
                 "Universal Ctags with JSON, gcc and g++ are required")
class EndToEndTest(unittest.TestCase):
    """
    Generate symbols.json from real build actions with real dependency
    discovery and Ctags.
    """

    @classmethod
    def setup_class(cls):
        cls.tmp = Path(tempfile.mkdtemp()).resolve()
        proj = cls.tmp / 'project'
        for d in ('include', 'vendor/include', 'src', 'build', 'dup'):
            (proj / d).mkdir(parents=True)

        (proj / 'include/project.h').write_text(textwrap.dedent("""\
            #pragma once
            struct point { int x; int y; };
            int project_fn(int);
            """))
        (proj / 'include/shared.h').write_text(textwrap.dedent("""\
            #pragma once
            static inline int shared_fn(int a) { return a; }
            """))
        # Byte-identical copy of shared.h under another path.
        shutil.copyfile(proj / 'include/shared.h', proj / 'dup/shared.h')
        (proj / 'vendor/include/vendor.h').write_text(textwrap.dedent("""\
            #pragma once
            typedef int vendor_t;
            """))
        (proj / 'src/feature.h').write_text(textwrap.dedent("""\
            #pragma once
            int feature_only(void);
            """))
        (proj / 'src/main.c').write_text(textwrap.dedent("""\
            #include <stdio.h>
            #include <stddef.h>
            #include "project.h"
            #include "shared.h"
            #include <vendor.h>
            #ifdef WITH_FEATURE
            #include "feature.h"
            #endif
            int global_counter = 0;
            struct pair { int a; int b; };
            int project_fn(int v) {
              return v + 1;
            }
            int main(void) { return project_fn(global_counter); }
            """))
        (proj / 'src/main.cpp').write_text(textwrap.dedent("""\
            #include <vector>
            #include "project.h"
            #include "../dup/shared.h"
            namespace Utils {
            int foo(int x) { return x; }
            int foo(double y) { return (int)y; }
            class Klass {
            public:
              int member;
              void run();
            };
            void Klass::run() {}
            }
            int main() { return Utils::foo(1); }
            """))

        cls.proj = proj
        cls.link = cls.tmp / 'include_link'
        try:
            cls.link.symlink_to(proj / 'include')
            cpp_include = f'-I{cls.link}'
        except OSError:
            cpp_include = '-I../include'

        compile_commands = [
            {"directory": str(proj / 'build'),
             "command": f"{GCC} -I../include -isystem ../vendor/include "
                        "-DWITH_FEATURE -c ../src/main.c -o main.o",
             "file": "../src/main.c"},
            {"directory": str(proj / 'build'),
             "command": f"{GXX} {cpp_include} -c ../src/main.cpp -o mainxx.o",
             "file": "../src/main.cpp"},
        ]
        cls.actions, _ = log_parser.parse_unique_log(compile_commands)
        cls.output = cls.tmp / 'symbols.json'
        cls.stats = symbol_index.generate(
            cls.actions, symbol_index.Ctags.find(CTAGS), cls.output, jobs=2)
        with open(cls.output, encoding='utf-8') as f:
            cls.doc = json.load(f)
        cls.by_path = {}
        for index in cls.doc['indexes']:
            for path in index['paths']:
                cls.by_path[(path, index['language'])] = index

    @classmethod
    def teardown_class(cls):
        shutil.rmtree(cls.tmp)

    def languages_of(self, relpath):
        path = str(self.proj / relpath)
        return sorted(lang for p, lang in self.by_path if p == path)

    def names(self, relpath, language):
        index = self.by_path[(str(self.proj / relpath), language)]
        return [d['name'] for d in index['definitions']]

    def test_build_actions_and_no_failures(self):
        self.assertEqual(len(self.actions), 2)
        self.assertEqual(self.stats.build_actions, 2)
        self.assertEqual(self.stats.failed_dependency_actions, 0)
        self.assertEqual(self.doc['version'], 1)

    def test_c_definitions(self):
        defs = {d['name']: d for d in
                self.by_path[(str(self.proj / 'src/main.c'), 'c')]
                ['definitions']}
        self.assertEqual(defs['project_fn']['kind'], 'function')
        self.assertEqual(defs['project_fn']['signature'], '(int v)')
        self.assertEqual(defs['project_fn']['line'], 11)
        self.assertEqual(defs['project_fn']['end_line'], 13)
        self.assertEqual(defs['global_counter']['kind'], 'variable')
        self.assertEqual(defs['pair']['kind'], 'struct')
        self.assertEqual(defs['a']['scope'], 'pair')
        self.assertEqual(defs['a']['scope_kind'], 'struct')

    def test_cpp_definitions_and_overloads(self):
        index = self.by_path[(str(self.proj / 'src/main.cpp'), 'c++')]
        defs = index['definitions']
        foos = [d for d in defs if d['name'] == 'foo']
        self.assertEqual([f['signature'] for f in foos],
                         ['(int x)', '(double y)'])
        self.assertTrue(all(f['scope'] == 'Utils' and
                            f['scope_kind'] == 'namespace' for f in foos))
        kinds = {d['name']: d['kind'] for d in defs}
        self.assertEqual(kinds['Utils'], 'namespace')
        self.assertEqual(kinds['Klass'], 'class')
        self.assertEqual(kinds['member'], 'member')
        run = [d for d in defs if d['name'] == 'run' and
               d['kind'] == 'function']
        self.assertEqual(run[0]['scope'], 'Utils::Klass')

    def test_shared_header_has_c_and_cpp_entries(self):
        # project.h is included by both TUs: one hash, two languages.
        self.assertEqual(self.languages_of('include/project.h'),
                         ['c', 'c++'])
        hashes = {self.by_path[(str(self.proj / 'include/project.h'), lang)]
                  ['content_hash'] for lang in ('c', 'c++')}
        self.assertEqual(len(hashes), 1)
        self.assertEqual(self.names('include/project.h', 'c'),
                         ['point', 'x', 'y'])

    def test_duplicate_bytes_share_one_entry_per_language(self):
        c_entry = self.by_path[(str(self.proj / 'include/shared.h'), 'c')]
        cpp_entry = self.by_path[(str(self.proj / 'dup/shared.h'), 'c++')]
        self.assertEqual(c_entry['content_hash'], cpp_entry['content_hash'])
        self.assertEqual(c_entry['paths'],
                         [str(self.proj / 'include/shared.h')])
        self.assertEqual(cpp_entry['paths'],
                         [str(self.proj / 'dup/shared.h')])

    def test_explicit_include_headers_kept_system_headers_removed(self):
        self.assertEqual(self.languages_of('include/project.h'),
                         ['c', 'c++'])
        self.assertEqual(self.languages_of('vendor/include/vendor.h'),
                         ['c'])
        self.assertEqual(self.names('vendor/include/vendor.h', 'c'),
                         ['vendor_t'])
        # Conditional include selected by -D is discovered by the compiler.
        self.assertEqual(self.languages_of('src/feature.h'), ['c'])

        all_paths = [p for p, _ in self.by_path]
        self.assertFalse(any(p.endswith(('/stdio.h', '/stddef.h', '/vector'))
                             for p in all_paths), all_paths)
        self.assertTrue(all(p.startswith(str(self.proj)) for p in all_paths),
                        all_paths)

    def test_paths_are_resolved(self):
        # '../include' and the symlinked include dir both resolve to the
        # real project path; no '..' or symlink spelling survives.
        for path, _ in self.by_path:
            self.assertNotIn('..', Path(path).parts)
            self.assertFalse(path.startswith(str(self.link)))

    def test_output_is_deterministic(self):
        second = self.tmp / 'symbols2.json'
        symbol_index.generate(self.actions, symbol_index.Ctags.find(CTAGS),
                              second, jobs=1)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_source_kept_when_dependency_discovery_fails(self):
        broken_src = self.tmp / 'broken.c'
        broken_src.write_text('#include "missing.h"\nint broken(void);\n'
                              'int f(void) { return 0; }\n')
        actions, _ = log_parser.parse_unique_log([
            {"directory": str(self.tmp),
             "command": f"{GCC} -c broken.c -o broken.o",
             "file": "broken.c"}])
        inputs, raw_count, success = \
            symbol_index.collect_inputs_for_action(actions[0])
        self.assertFalse(success)
        self.assertEqual(raw_count, 0)
        self.assertEqual(inputs, {IndexInput(str(broken_src), 'c')})

    def test_ctags_fields_exist_in_real_ctags(self):
        listed = subprocess.run([CTAGS, '--list-fields'], check=True,
                                stdout=subprocess.PIPE, encoding='utf-8')
        names = {line.split()[1] for line in listed.stdout.splitlines()[1:]}
        for name in symbol_index.CTAGS_FIELDS:
            self.assertIn(name, names)
