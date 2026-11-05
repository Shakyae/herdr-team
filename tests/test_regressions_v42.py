"""Independent adverse-case regression tests for v4.2.

All Herdr interactions are mocked. No real agents or user repositories touched.
"""
import contextlib
import copy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

import pytest

import test_htm
from test_htm import h, opts, isolate_git_env


def verified_cancel_control(payload):
    """A fake trusted control plane that confirms a safe cancel (T17)."""
    if payload.get('action') == 'cancel':
        return {'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
                'verified_roles': [r['role'] for r in payload.get('roles', [])]}
    return {}


@contextlib.contextmanager
def verified_cancel_plane():
    with patch.object(h, 'TRUSTED_CONTROL', verified_cancel_control):
        yield


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


def start_in_child(q, barrier):
    """Fork inherits an isolated patched Fake Herdr and a shared state path."""
    try:
        barrier.wait(timeout=5)
        q.put(('ok', h.run_start(None, ns(goal='Concurrent fake process'))['run_id']))
    except Exception as e:
        q.put(('blocked', str(e)))


class WorktreeDetectionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', str(self.repo)], check=True, capture_output=True)
        self.cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
        self.env = patch.dict(os.environ, {'HTM_WORKTREE_DIR': str(self.base / 'managed')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_init_uses_cwd_not_script_or_process_directory(self):
        args = opts('dev-review', worktree='auto', task='example', cwd=str(self.repo))
        with patch.object(h.os, 'getcwd', return_value=str(h.ROOT)):
            result = h.init(self.cfg, args)
        self.assertEqual(result['worktree']['repo'], str(self.repo))

    def test_init_outside_repository_fails(self):
        args = opts('dev-review', worktree='auto', task='example', cwd=str(self.base))
        with self.assertRaisesRegex(h.Error, 'not inside a Git repository'):
            h.init(self.cfg, args)

    def test_auto_dirty_repository_rejected(self):
        (self.repo / 'untracked').write_text('dirty')
        args = opts('dev-review', worktree='auto', task='example', cwd=str(self.repo))
        with self.assertRaisesRegex(h.Error, 'dirty or contains untracked'):
            h.init(self.cfg, args)

    def test_expected_identity_mismatch_before_apply(self):
        args = opts('dev-review', worktree='on', task='example')
        plan = h.initial_plan(self.cfg, args.template, args)
        with self.assertRaisesRegex(h.Error, 'identity changed since preflight'):
            h.worktree_details(plan, args, True, cwd=self.repo, expected={'repo': '/different'})
        self.assertFalse((self.base / 'managed').exists())


@pytest.fixture(autouse=True)
def isolate_herdr_socket(monkeypatch):
    monkeypatch.delenv('HERDR_SOCKET_PATH', raising=False)
    monkeypatch.delenv('HERDR_SESSION', raising=False)


@pytest.fixture
def worktree_repos(tmp_path, monkeypatch):
    repos = [tmp_path / name for name in ('a', 'b')]
    for repo in repos:
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        (repo / 'source').write_text(repo.name)
        subprocess.run(['git', '-C', str(repo), 'add', 'source'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.test', 'commit', '-m', 'initial'],
                       check=True, capture_output=True)
    monkeypatch.setenv('HTM_WORKTREE_DIR', str(tmp_path / 'managed'))
    return repos


def git_snapshot(repo, env):
    return [subprocess.run(['git', '-C', str(repo), *args], env=env, check=True,
                           capture_output=True, text=True).stdout
            for args in (['worktree', 'list', '--porcelain'], ['show-ref'])]


@pytest.mark.parametrize('variable', ['GIT_DIR', 'GIT_WORK_TREE', 'both', 'GIT_CONFIG_COUNT'])
@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('inside', [False, True])
def test_worktree_ignores_foreign_git_environment(worktree_repos, tmp_path, monkeypatch,
                                                 variable, explicit, inside):
    repo, foreign = worktree_repos
    clean_env = os.environ.copy()
    before = git_snapshot(foreign, clean_env)
    if variable in ('GIT_DIR', 'both'):
        monkeypatch.setenv('GIT_DIR', str(foreign / '.git'))
    if variable in ('GIT_WORK_TREE', 'both'):
        monkeypatch.setenv('GIT_WORK_TREE', str(foreign))
    if variable == 'GIT_CONFIG_COUNT':
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.worktree')
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(foreign))
    cwd = repo if inside else tmp_path
    args = opts('dev-review', worktree='on', task='env-test',
                repo=str(cwd) if explicit else None)
    cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
    plan = h.initial_plan(cfg, args.template, args)
    if inside:
        preflight = h.worktree_details(plan, args, False, cwd=cwd)
        made = h.worktree_details(plan, args, True, cwd=cwd, expected=preflight)
        assert made['repo'] == str(repo)
        assert (Path(made['path']) / 'source').read_text() == 'a'
        assert 'refs/heads/htm/env-test' in git_snapshot(repo, clean_env)[1]
    else:
        with pytest.raises(h.Error, match='not a git repository' if explicit else 'not inside a Git repository'):
            h.worktree_details(plan, args, True, cwd=cwd)
        assert not (tmp_path / 'managed').exists()
        assert 'refs/heads/htm/env-test' not in git_snapshot(repo, clean_env)[1]
    assert git_snapshot(foreign, clean_env) == before


def test_clean_git_env_strips_local_state_but_preserves_system_environment(monkeypatch):
    removed = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
               'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
               'GIT_NAMESPACE', 'GIT_PREFIX', 'GIT_DISCOVERY_ACROSS_FILESYSTEM',
               'GIT_IMPLICIT_WORK_TREE',
               'GIT_SHALLOW_FILE', 'GIT_GRAFT_FILE', 'GIT_REPLACE_REF_BASE',
               'GIT_NO_REPLACE_OBJECTS', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS',
               'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0')
    kept_bounds = ('GIT_CEILING_DIRECTORIES',)
    for key in removed + kept_bounds:
        monkeypatch.setenv(key, 'polluted' if key not in kept_bounds else str(Path('/tmp')))
    for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'SSH_AUTH_SOCK'):
        monkeypatch.setenv(key, 'preserved')
    before = os.environ.copy()
    env = h.clean_git_env()
    assert all(key not in env for key in removed)
    # A caller-supplied search ceiling must survive, or discovery widens.
    assert all(key in env for key in kept_bounds)
    assert all(env[key] == 'preserved' for key in ('PATH', 'HOME', 'LANG', 'TMPDIR', 'SSH_AUTH_SOCK'))
    assert dict(os.environ) == before


def test_ceiling_bound_stops_discovery_at_unrelated_parent_repo(tmp_path, monkeypatch):
    outer = tmp_path / 'outer'
    vendor = outer / 'vendor'
    (outer).mkdir()
    vendor.mkdir()
    gitc = ('git', '-C', str(outer))
    subprocess.run([*gitc, 'init', '-q', '-b', 'main'], check=True)
    subprocess.run([*gitc, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
    cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
    monkeypatch.setenv('HTM_WORKTREE_DIR', str(tmp_path / 'managed'))
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(outer))

    args = opts('dev-review', worktree='on', task='ceiling-stop')
    with pytest.raises(h.Error, match='not inside a Git repository'):
        h.worktree_details(h.initial_plan(cfg, args.template, args), args, True, cwd=vendor)
    assert not (tmp_path / 'managed').exists()
    heads = subprocess.run([*gitc, 'branch', '--list', 'htm/*'], text=True,
                           capture_output=True, check=True).stdout.strip()
    assert heads == ''


def test_discovery_returns_parent_repo_without_ceiling(tmp_path, monkeypatch):
    outer = tmp_path / 'outer'
    vendor = outer / 'vendor'
    outer.mkdir()
    vendor.mkdir()
    gitc = ('git', '-C', str(outer))
    subprocess.run([*gitc, 'init', '-q', '-b', 'main'], check=True)
    subprocess.run([*gitc, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
    monkeypatch.delenv('GIT_CEILING_DIRECTORIES', raising=False)
    assert h.detect_git_root(vendor) == outer.resolve()


def test_dirty_check_uses_requested_repository(worktree_repos, monkeypatch):
    repo, foreign = worktree_repos
    monkeypatch.setenv('GIT_DIR', str(foreign / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(foreign))
    (repo / 'source').write_text('dirty')
    cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
    args = opts('dev-review', worktree='on', task='dirty-test', repo=str(repo))
    with pytest.raises(h.Error, match='dirty or contains untracked'):
        h.worktree_details(h.initial_plan(cfg, args.template, args), args, True, cwd=repo)
    assert not Path(os.environ['HTM_WORKTREE_DIR']).exists()


class WorktreeApplyRegressionTests(unittest.TestCase):
    def setUp(self):
        test_htm.RuntimeTests.setUp(self)

    def test_apply_uses_explicit_cwd_across_preflight_and_creation(self):
        repo = Path(self.tmp.name).resolve() / 'checkout'
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'initial'],
                       check=True, capture_output=True)
        args = opts('dev-review', worktree='on', task='cross-cwd', cwd=str(repo), dry_run=False)
        original = h.herdr
        def with_session(*argv, **kw):
            result = original(*argv, **kw)
            if argv[:2] == ('agent', 'start'):
                self.started[argv[2]]['agent_session'] = {
                    'kind': 'id', 'source': 'mock', 'value': 'session-' + argv[2]}
            return result
        with patch.dict(os.environ, {'HTM_WORKTREE_DIR': str(Path(self.tmp.name) / 'managed')}), \
                patch.object(h.os, 'getcwd', return_value=str(h.ROOT)), \
                patch.object(h, 'herdr', side_effect=with_session):
            team = h.init(self.cfg, args)['team']
            self.assertTrue(h.init(self.cfg, args)['reused'])
        self.assertEqual(team['status'], 'ready')
        self.assertEqual(team['worktree']['repo'], str(repo))
        self.assertTrue((Path(team['worktree']['path']) / '.git').is_file())
        self.assertEqual(len(team['roles']), 2)
        for role in team['roles']:
            self.assertEqual(role['cwd'], team['worktree']['path'])
            self.assertIn('agent_session', role)
        splits = [call for call in self.actions if call[:2] == ('pane', 'split')]
        self.assertTrue(all(call[call.index('--cwd') + 1] == team['worktree']['path'] for call in splits))
        self.assertEqual(team['resources'][0]['operation'], 'git_worktree_add')
        self.assertIsNone(team['pending_operation'])


class PublicIdContextTests(unittest.TestCase):
    def setUp(self):
        # These tests cover ID validation, not discovery. Statically satisfy the
        # native-branch process-ownership probe at its boundary so no Herdr call is made.
        def identity(*args, **kwargs):
            return {'session': os.environ.get('HERDR_SESSION') or 'default',
                    'workspace': os.environ.get('HERDR_WORKSPACE_ID', ''),
                    'tab': os.environ.get('HERDR_TAB_ID', ''),
                    'pane': os.environ.get('HERDR_PANE_ID', '')}
        mock = patch.object(h, 'pane_of_current_process', side_effect=identity)
        mock.start(); self.addCleanup(mock.stop)

    def test_valid_workspace_ids(self):
        for ws in ('w1', 'w2', 'wA', 'wB', 'wZ', 'w12', 'wABC'):
            with self.subTest(ws=ws):
                env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': ws,
                       'HERDR_TAB_ID': f'{ws}:t1', 'HERDR_PANE_ID': f'{ws}:p1'}
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(h.context(), {'workspace': ws, 'lead_tab': f'{ws}:t1',
                                                   'lead_pane': f'{ws}:p1'})

    def test_valid_tab_and_pane_ids(self):
        for ws, tab, pane in (('w1', 'w1:t1', 'w1:p1'), ('wA', 'wA:t1', 'wA:p1'),
                              ('wABC', 'wABC:t2', 'wABC:p1'), ('wA', 'wA:t1', 'wA:p1B'),
                              ('wB', 'wB:t1', 'wB:pZZ')):
            with self.subTest(tab=tab, pane=pane), patch.dict(os.environ, {
                'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': ws,
                'HERDR_TAB_ID': tab, 'HERDR_PANE_ID': pane}, clear=True):
                self.assertEqual(h.context(), {'workspace': ws, 'lead_tab': tab, 'lead_pane': pane})

    def test_invalid_ids(self):
        cases = {
            'HERDR_WORKSPACE_ID': ('null', 'w', '1', 'a1', 'wa1', 'wI', 'wL', 'wO', 'wU',
                                   'w1_', 'w1-2', 'w1 2'),
            'HERDR_TAB_ID': ('w1:', 'w1:t', 'w1:t1:p1', 'w1:t1x'),
            'HERDR_PANE_ID': ('w1:', 'w1:p', 'w1:t1:p1', 'w1:p@'),
        }
        for key, values in cases.items():
            for value in values:
                env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
                       'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1', key: value}
                with self.subTest(key=key, value=value), patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(h.Error) as caught:
                        h.context()
                    self.assertIn(repr(value), str(caught.exception))

    def test_missing_values_preserve_errors(self):
        env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
               'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1'}
        for key in ('HERDR_WORKSPACE_ID', 'HERDR_TAB_ID', 'HERDR_PANE_ID'):
            with self.subTest(key=key), patch.dict(os.environ, {**env, key: ''}, clear=True):
                with self.assertRaisesRegex(h.Error, '^Missing Herdr pane/workspace/tab environment$'):
                    h.context()
        with patch.dict(os.environ, {'HERDR_ENV': '0'}, clear=True):
            with self.assertRaisesRegex(h.Error, '^Must run inside Herdr: HERDR_ENV=1$'):
                h.context()

    def test_cross_workspace_ids_rejected(self):
        for ws, tab, pane in (('w1', 'wA:t1', 'w1:p1'), ('wA', 'wA:t1', 'w1:p1')):
            with self.subTest(ws=ws, tab=tab, pane=pane), patch.dict(os.environ, {
                'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': ws,
                'HERDR_TAB_ID': tab, 'HERDR_PANE_ID': pane}, clear=True):
                with self.assertRaises(h.Error) as caught:
                    h.context()
                self.assertEqual(str(caught.exception),
                                 f'Tab/Pane does not belong to workspace {ws}: tab={tab!r} pane={pane!r}')

    def test_optional_context_does_not_validate(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(h.context(require=False), {'workspace': '', 'lead_tab': '', 'lead_pane': ''})
        with patch.dict(os.environ, {'HERDR_WORKSPACE_ID': 'invalid'}, clear=True):
            self.assertEqual(h.context(require=False), {'workspace': 'invalid', 'lead_tab': '', 'lead_pane': ''})


class DerivedPaneContextTests(unittest.TestCase):
    def setUp(self):
        self.panes = [{'workspace_id': 'w1', 'tab_id': 'w1:t1', 'pane_id': 'w1:p1'}]
        self.infos = {'w1:p1': {'shell_pid': 55, 'foreground_processes': [{'pid': 42}]}}
        self.calls = []

    def fake_herdr(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args == ('session', 'list', '--json'):
            return {'sessions': [{'name': 'default', 'running': True},
                                 {'name': 'stopped', 'running': False}]}
        if args == ('pane', 'list'):
            return {'panes': self.panes}
        if args[:2] == ('pane', 'process-info'):
            return {'process_info': self.infos[args[3]]}
        raise AssertionError(args)

    def probe(self):
        with patch.object(h, 'herdr', side_effect=self.fake_herdr), \
             patch.object(h.os, 'getpid', return_value=42), \
             patch.object(h, 'process', return_value='1\n'):
            return h.pane_of_current_process()

    def test_unique_match_and_session(self):
        self.assertEqual(self.probe(), {'session': 'default', 'workspace': 'w1',
                                        'tab': 'w1:t1', 'pane': 'w1:p1'})
        self.assertEqual(self.calls[1], (('pane', 'list'), {'session': 'default'}))
        self.assertEqual(self.calls[2][1], {'session': 'default'})

    def test_zero_and_multiple_matches(self):
        self.infos['w1:p1']['foreground_processes'] = []
        with self.assertRaisesRegex(h.Error, '无法定位'):
            self.probe()
        self.infos['w1:p1']['shell_pid'] = 42
        self.panes.append({'workspace_id': 'w1', 'tab_id': 'w1:t1', 'pane_id': 'w1:p2'})
        self.infos['w1:p2'] = {'shell_pid': 42, 'foreground_processes': []}
        with self.assertRaisesRegex(h.Error, '身份不唯一，拒绝猜测'):
            self.probe()

    def test_invalid_ids_and_prefixes(self):
        for key, value in [('workspace_id', 'wI'), ('tab_id', 'bad'), ('pane_id', 'w1:p!'),
                           ('tab_id', 'w2:t1'), ('pane_id', 'w2:p1')]:
            with self.subTest(key=key, value=value):
                pane = {'workspace_id': 'w1', 'tab_id': 'w1:t1', 'pane_id': 'w1:p1', key: value}
                self.panes = [pane]
                self.infos[value if key == 'pane_id' else 'w1:p1'] = {
                    'shell_pid': 42, 'foreground_processes': []}
                with self.assertRaisesRegex(h.Error, '无法定位'):
                    self.probe()

    def test_query_failure(self):
        with patch.object(h, 'herdr', side_effect=h.Error('failed')):
            with self.assertRaisesRegex(h.Error, '无法定位'):
                h.pane_of_current_process()

    def test_ancestor_chain_and_herdr_session_argument(self):
        self.infos['w1:p1'] = {'shell_pid': 30, 'foreground_processes': []}
        with patch.object(h, 'herdr', side_effect=self.fake_herdr), \
             patch.object(h.os, 'getpid', return_value=42), \
             patch.object(h, 'process', side_effect=['30\n', '1\n']) as ps:
            self.assertEqual(h.pane_of_current_process()['pane'], 'w1:p1')
            self.assertEqual(ps.call_args_list[0].args[0], ['/bin/ps', '-o', 'ppid=', '-p', '42'])
            self.assertEqual(ps.call_args_list[1].args[0], ['/bin/ps', '-o', 'ppid=', '-p', '30'])
        with patch.object(h, 'process', return_value={}) as process:
            h.herdr('pane', 'list', session='default')
            process.assert_called_once_with(['herdr', '--session', 'default', 'pane', 'list'], timeout=40)

    def test_derived_branch_cross_checks_an_inherited_session(self):
        # Happier's claude keeps HERDR_SESSION / HERDR_SOCKET_PATH. The derived
        # branch used to ignore them entirely, so it applied a weaker rule than
        # the native branch to exactly the values an attacker can inherit or
        # forge. Disagreement with the kernel evidence must reject.
        identity = {'session': 'inner', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {'HERDR_SESSION': 'outer'}, clear=True), \
                patch.object(h, 'pane_of_current_process', return_value=identity):
            with self.assertRaisesRegex(h.Error, '进程归属 session 与环境不一致'):
                h.lead_identity()

    def test_derived_branch_accepts_a_matching_inherited_session(self):
        identity = {'session': 'inner', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {'HERDR_SESSION': 'inner'}, clear=True), \
                patch.object(h, 'pane_of_current_process', return_value=identity):
            self.assertEqual(h.lead_identity()['session'], 'inner')

    def test_derived_branch_rejects_a_foreign_socket(self):
        identity = {'session': 'inner', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {'HERDR_SOCKET_PATH': '/tmp/outer.sock'}, clear=True), \
                patch.object(h, 'pane_of_current_process', return_value=identity), \
                patch.object(h, 'socket_session', return_value='outer'):
            with self.assertRaisesRegex(h.Error, 'Herdr socket 与进程归属不一致'):
                h.lead_identity()

    def test_context_double_probe_and_no_environment_write(self):
        identity = {'session': 'default', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {}, clear=True), patch.object(h, 'pane_of_current_process', return_value=identity) as probe:
            self.assertEqual(h.context(), {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'})
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(dict(os.environ), {})
        with patch.dict(os.environ, {}, clear=True), patch.object(h, 'pane_of_current_process', side_effect=[identity, {**identity, 'session': 'other'}]):
            with self.assertRaisesRegex(h.Error, '身份不唯一，拒绝猜测'):
                h.context()

    def test_session_explicit_no_global_leak(self):
        identity = {'session': 'alpha', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(h, 'pane_of_current_process', return_value=identity), \
                patch.object(h, 'process', return_value={}) as process:
            lead = h.lead_identity()
            self.assertEqual(lead['context'], {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'})
            h.herdr('pane', 'get', 'w1:p1', session=lead['session'])
            h.herdr('pane', 'get', 'w1:p1')
            self.assertEqual(process.call_args_list[0].args[0], ['herdr', '--session', 'alpha', 'pane', 'get', 'w1:p1'])
            self.assertEqual(process.call_args_list[1].args[0], ['herdr', 'pane', 'get', 'w1:p1'])
            self.assertFalse(hasattr(h, '_DISCOVERED_SESSION'))
            self.assertNotIn('HERDR_ENV', os.environ)

    def test_inconsistent_probes_do_not_leak_session(self):
        identity = {'session': 'alpha', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(h, 'pane_of_current_process', side_effect=[identity, {**identity, 'session': 'beta'}]), \
                patch.object(h, 'process', return_value={}) as process:
            with self.assertRaisesRegex(h.Error, '身份不唯一，拒绝猜测（两次探测不一致）'):
                h.context()
            h.herdr('pane', 'get', 'w1:p1')
            process.assert_called_once_with(['herdr', 'pane', 'get', 'w1:p1'], timeout=40)

    def test_cross_session_duplicate_pane_and_tab_rejected(self):
        for field in ('pane_id', 'tab_id'):
            with self.subTest(field=field):
                other = {'workspace_id': 'w1', 'tab_id': 'w1:t2', 'pane_id': 'w1:p2'}
                other[field] = self.panes[0][field]
                def fake(*args, **kw):
                    if args == ('session', 'list', '--json'):
                        return {'sessions': [{'name': 'one', 'running': True}, {'name': 'two', 'running': True}]}
                    if args == ('pane', 'list'):
                        return {'panes': self.panes if kw['session'] == 'one' else [other]}
                    return {'process_info': {'shell_pid': 42 if kw['session'] == 'one' else 99, 'foreground_processes': []}}
                with patch.object(h, 'herdr', side_effect=fake), patch.object(h.os, 'getpid', return_value=42), \
                        patch.object(h, 'process', return_value='1'):
                    with self.assertRaisesRegex(h.Error, '跨 session .* ID 冲突'):
                        h.pane_of_current_process()

    def test_context_ledger_comparison_and_key_unchanged(self):
        identity = {'session': 'alpha', 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
        legacy = {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(h, 'STATE', Path(tmp)), \
                patch.dict(os.environ, {}, clear=True), patch.object(h, 'pane_of_current_process', return_value=identity):
            h.save(h.run_file('abcdef123456'), {'version': 3, 'context': legacy})
            self.assertEqual(h.context(), legacy)
            self.assertEqual(h.state_file(h.context()), h.state_file(legacy))
            self.assertEqual(h.require_run('abcdef123456')['context'], legacy)

    def test_native_default_session_confirmed_by_probe_and_socket(self):
        env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1', 'HERDR_TAB_ID': 'w1:t1',
               'HERDR_PANE_ID': 'w1:p1', 'HERDR_SOCKET_PATH': '/tmp/socket'}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(h, 'pane_of_current_process', side_effect=native_probe('default')), \
                patch.object(h, 'process', return_value={
                    'sessions': [{'name': 'default', 'socket_path': '/tmp/socket'}]}) as process:
            identity = h.lead_identity()
            self.assertEqual(identity['session'], 'default')
            h.context(False)
        process.assert_called_once_with(['herdr', 'session', 'list', '--json'], timeout=40)

    def test_running_session_with_incomplete_name_fails_closed(self):
        for session in ({'running': True}, {'running': True, 'name': ''},
                        {'running': True, 'name': '  '}, {'running': True, 'name': 123}):
            with self.subTest(session=session):
                def fake(*args, **kwargs):
                    if args == ('session', 'list', '--json'):
                        return {'sessions': [{'name': 'default', 'running': True}, session]}
                    return self.fake_herdr(*args, **kwargs)
                with patch.object(h, 'herdr', side_effect=fake), \
                        patch.object(h.os, 'getpid', return_value=42), \
                        patch.object(h, 'process', return_value='1'):
                    with self.assertRaisesRegex(h.Error, 'session 信息不完整，无法确认身份唯一性'):
                        h.pane_of_current_process()

    def test_stopped_session_with_empty_name_is_ignored(self):
        def fake(*args, **kwargs):
            if args == ('session', 'list', '--json'):
                return {'sessions': [{'name': 'default', 'running': True}, {'name': '', 'running': False}]}
            return self.fake_herdr(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=fake), \
                patch.object(h.os, 'getpid', return_value=42), \
                patch.object(h, 'process', return_value='1'):
            self.assertEqual(h.pane_of_current_process()['session'], 'default')

    def test_native_missing_ids_fail_before_probe(self):
        env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1', 'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1'}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(h, 'pane_of_current_process', side_effect=native_probe('default')) as probe:
            self.assertEqual(h.context(), {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'})
            self.assertEqual(probe.call_count, 2)
            del os.environ['HERDR_PANE_ID']
            with self.assertRaisesRegex(h.Error, '^Missing Herdr pane/workspace/tab environment$'):
                h.context()
            self.assertEqual(h.context(False), {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': ''})
            self.assertEqual(probe.call_count, 2)


NATIVE_SESSION_ENV = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
                      'HERDR_TAB_ID': 'w1:t1', 'HERDR_PANE_ID': 'w1:p1'}


def native_probe(session):
    return lambda *a, **k: {'session': session, 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}


@pytest.mark.parametrize('name', ['default', 'alpha'])
def test_native_probe_confirms_identity_and_session(name):
    # A native environment is only trusted after the real process-ownership probe
    # agrees with it on all four components.
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SESSION': name}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe(name)) as probe:
        identity = h.lead_identity()
    assert identity == {'context': {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'},
                        'session': name}
    assert probe.call_count == 2


def test_native_probe_mismatch_fails_closed():
    with patch.dict(os.environ, NATIVE_SESSION_ENV, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('alpha')):
        # Probe reports w1:p1 but a forged environment claims another pane.
        os.environ['HERDR_PANE_ID'] = 'w1:p9'
        with pytest.raises(h.Error, match='进程归属与 HERDR_\\* 身份不一致'):
            h.lead_identity()


def test_native_unstable_probe_fails_closed():
    with patch.dict(os.environ, NATIVE_SESSION_ENV, clear=True), \
            patch.object(h, 'pane_of_current_process',
                         side_effect=[native_probe('default')(), native_probe('alpha')()]):
        with pytest.raises(h.Error, match='两次探测不一致'):
            h.lead_identity()


def test_native_session_env_conflict_fails_closed():
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SESSION': 'beta'}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('alpha')):
        with pytest.raises(h.Error, match="进程归属 session 与环境不一致"):
            h.lead_identity()


@pytest.mark.parametrize('name', ['default', 'alpha'])
def test_native_socket_resolution_matches_probe(name):
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SOCKET_PATH': '/socket'}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe(name)), \
            patch.object(h, 'process', return_value={
                'sessions': [{'name': name, 'socket_path': '/socket'}]}) as process:
        assert h.lead_identity()['session'] == name
    process.assert_called_once_with(['herdr', 'session', 'list', '--json'], timeout=40)


def test_native_socket_resolution_conflict_fails_closed():
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SOCKET_PATH': '/socket'}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('alpha')), \
            patch.object(h, 'process', return_value={
                'sessions': [{'name': 'beta', 'socket_path': '/socket'}]}):
        with pytest.raises(h.Error, match='Herdr socket 与进程归属不一致'):
            h.lead_identity()


@pytest.mark.parametrize('sessions,message', [
    ([], 'exactly one session'),
    ([{'name': 'a', 'socket_path': '/socket'}, {'name': 'b', 'socket_path': '/socket'}],
     'exactly one session'),
    ([{'name': '', 'socket_path': '/socket'}], 'exactly one named session'),
    ([{'name': 'a', 'socket_path': '/other'}], 'exactly one session')])
def test_native_socket_ambiguous_or_empty_fails(sessions, message):
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SOCKET_PATH': '/socket'}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('a')), \
            patch.object(h, 'process', return_value={'sessions': sessions}):
        with pytest.raises(h.Error, match=message):
            h.lead_identity()


@pytest.mark.parametrize('failure', [h.Error('command failed'), h.Error('timeout'), h.Error('non JSON')])
def test_native_session_lookup_failure_blocks_init(failure):
    cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SOCKET_PATH': '/socket'}, clear=True), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('default')), \
            patch.object(h, 'process', side_effect=failure):
        with pytest.raises(h.Error):
            h.init(cfg, opts('dev-review', dry_run=False))


def test_native_session_routed_through_real_herdr_argv(tmp_path):
    # The confirmed session must reach the management CLI boundary, not just the
    # identity dict, and --session must be placed before the subcommand.
    with patch.dict(os.environ, {**NATIVE_SESSION_ENV, 'HERDR_SESSION': 'alpha'}, clear=True), \
            patch.object(h, 'STATE', tmp_path), \
            patch.object(h, 'pane_of_current_process', side_effect=native_probe('alpha')), \
            patch.object(h, 'process', side_effect=h.HerdrError('not found', code='agent_not_found')) as process:
        identity = h.lead_identity()
        h.require_name_available('developer-w1p2', tmp_path / 'team.json', session=identity['session'])
    process.assert_called_with(['herdr', '--session', 'alpha', 'agent', 'get', 'developer-w1p2'], timeout=40)


def test_native_second_probe_mismatch_leaves_no_side_effects(tmp_path):
    # A failed cross-check must abort before any ledger write, worktree, or Herdr call.
    cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
    args = opts('dev-lite', dry_run=False, cwd=str(tmp_path))
    with patch.dict(os.environ, NATIVE_SESSION_ENV, clear=True), \
            patch.object(h, 'STATE', tmp_path / 'state'), \
            patch.object(h, 'pane_of_current_process',
                         side_effect=[native_probe('default')(), native_probe('alpha')()]), \
            patch.object(h, 'herdr') as herdr_call:
        with pytest.raises(h.Error, match='两次探测不一致'):
            h.init(cfg, args)
    herdr_call.assert_not_called()
    assert not (tmp_path / 'state').exists()


def test_native_require_false_does_not_probe():
    env = {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1', 'HERDR_TAB_ID': 'w1:t1',
           'HERDR_PANE_ID': 'w1:p1'}
    with patch.dict(os.environ, env, clear=True), patch.object(h, 'pane_of_current_process') as probe:
        assert h.lead_identity(False) == {'context': {'workspace': 'w1', 'lead_tab': 'w1:t1',
                                                      'lead_pane': 'w1:p1'}, 'session': ''}
        assert h.context(False) == {'workspace': 'w1', 'lead_tab': 'w1:t1', 'lead_pane': 'w1:p1'}
    probe.assert_not_called()


@pytest.mark.parametrize('name', ['default', 'alpha'])
def test_derived_identity_uses_probed_session_without_extra_calls(name):
    found = {'session': name, 'workspace': 'w1', 'tab': 'w1:t1', 'pane': 'w1:p1'}
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(h, 'pane_of_current_process', return_value=found), \
            patch.object(h, 'process') as process:
        identity = h.lead_identity()
    assert identity['session'] == name
    process.assert_not_called()


class AdoptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve() / 'checkout'
        self.cwd.mkdir()
        self.state = patch.object(h, 'STATE', Path(self.tmp.name) / 'state')
        self.state.start(); self.addCleanup(self.state.stop)
        env = patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
                                    'HERDR_PANE_ID': 'w1:p1', 'HERDR_TAB_ID': 'w1:t1'})
        env.start(); self.addCleanup(env.stop)
        self.cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)
        self.agents = {name: {'name': name, 'agent': 'claude', 'agent_status': 'idle',
                              'pane_id': f'w1:p{i}', 'cwd': str(self.cwd),
                              'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': f'session-{i}'}}
                       for i, name in enumerate(('dev', 'review'), 2)}
        self.calls = []
        def fake(*args, **kw):
            self.calls.append(args)
            if args[:2] == ('agent', 'get'):
                if args[2] not in self.agents:
                    raise h.Error('not found')
                return {'agent': copy.deepcopy(self.agents[args[2]])}
            if args[:2] == ('pane', 'get'):
                return {'pane': {'pane_id': args[2], 'workspace_id': args[2].split(':')[0], 'tab_id': 'w1:t1'}}
            raise AssertionError(f'unexpected Herdr call: {args}')
        mock = patch.object(h, 'herdr', side_effect=fake)
        mock.start(); self.addCleanup(mock.stop)
        # Native identity now probes real process ownership; these adopt tests are not
        # about discovery, so satisfy that boundary from the environment.
        probe = patch.object(h, 'pane_of_current_process', side_effect=lambda *a, **k: {
            'session': os.environ.get('HERDR_SESSION') or 'default',
            'workspace': os.environ.get('HERDR_WORKSPACE_ID', ''),
            'tab': os.environ.get('HERDR_TAB_ID', ''),
            'pane': os.environ.get('HERDR_PANE_ID', '')})
        probe.start(); self.addCleanup(probe.stop)
        self.args = ns(template='dev-lite', role=['developer=dev', 'reviewer=review'],
                       set=[], cwd=str(self.cwd), dry_run=False)

    def adopt(self):
        return h.adopt(self.cfg, self.args)

    def test_success(self):
        result = self.adopt(); team = result['team']
        self.assertTrue(result['adopted']); self.assertEqual(team['status'], 'ready')
        self.assertEqual(team['resources'], [])
        for role, name in zip(team['roles'], ('dev', 'review')):
            self.assertTrue(role['adopted']); self.assertEqual(role['agent_name'], name)
            self.assertEqual(role['pane_id'], self.agents[name]['pane_id'])
            self.assertEqual(role['cwd'], str(self.cwd))
            self.assertEqual(role['agent_session'], self.agents[name]['agent_session'])
        self.assertTrue(all(c[:2] in (('agent', 'get'), ('pane', 'get')) for c in self.calls))

    def test_repeat_preserves_team(self):
        import hashlib
        self.adopt(); path = h.state_file(h.context())
        before = hashlib.sha256(path.read_bytes()).digest()
        with self.assertRaisesRegex(h.Error, 'already registered'): self.adopt()
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).digest())

    def test_partial_roles(self):
        self.args.role = ['developer=dev']
        with self.assertRaisesRegex(h.Error, 'missing'): self.adopt()

    def test_unknown_role(self):
        self.args.role.append('tester=x')
        with self.assertRaisesRegex(h.Error, 'Unknown role'): self.adopt()

    def test_missing_agent(self):
        del self.agents['dev']
        with self.assertRaisesRegex(h.Error, 'not live'): self.adopt()

    def test_kind_mismatch_or_missing(self):
        for kind in ('codex', None):
            with self.subTest(kind=kind):
                self.agents['dev']['agent'] = kind
                with self.assertRaisesRegex(h.Error, 'kind'): self.adopt()

    def test_unsafe_status(self):
        for status in ('working', 'blocked', 'unknown'):
            with self.subTest(status=status):
                self.agents['dev']['agent_status'] = status
                with self.assertRaisesRegex(h.Error, f'is {status}'): self.adopt()

    def test_cross_workspace(self):
        self.agents['dev']['pane_id'] = 'w2:p2'
        with self.assertRaisesRegex(h.Error, 'different workspace'): self.adopt()

    def test_adopt_rejects_lead_pane(self):
        self.agents['dev']['pane_id'] = 'w1:p1'
        with self.assertRaisesRegex(h.Error, 'Lead pane'): self.adopt()
        # The Lead identity probe is faked at its boundary, so no pane read happens.
        self.assertFalse(any(c[:2] == ('pane', 'get') for c in self.calls), self.calls)

    def test_cwd_mismatch(self):
        other = Path(self.tmp.name) / 'other'; other.mkdir()
        self.agents['review']['cwd'] = str(other)
        with self.assertRaisesRegex(h.Error, 'cwd mismatch'): self.adopt()

    def test_session_missing(self):
        for session in (None, {'kind': 'id', 'source': 'herdr:claude', 'value': ''}):
            with self.subTest(session=session):
                self.agents['dev']['agent_session'] = session
                with self.assertRaisesRegex(h.Error, 'no stable session identity'): self.adopt()

    def test_unfinished_run(self):
        # Run metadata matches the adopted workspace.
        h.save(h.STATE / 'runs' / 'abc.json', {'id': 'run123', 'status': 'active',
               'context': {'workspace': 'w1'}, 'roles': [{'pane_id': 'w1:p2'}]})
        with self.assertRaisesRegex(h.Error, 'run123'): self.adopt()

    def test_unfinished_run_cross_workspace_metadata(self):
        h.save(h.STATE / 'runs' / 'abc.json', {'id': 'runCROSS', 'status': 'active',
               'context': {'workspace': 'wB'}, 'roles': [{'pane_id': 'w1:p2'}]})
        with self.assertRaisesRegex(h.Error, 'runCROSS'): self.adopt()

    def test_unfinished_run_finished_ignored(self):
        h.save(h.STATE / 'runs' / 'abc.json', {'id': 'run123', 'status': 'finished',
               'roles': [{'pane_id': 'w1:p2'}]})
        self.assertTrue(self.adopt()['adopted'])

    def test_unfinished_run_cancelled_ignored(self):
        h.save(h.STATE / 'runs' / 'abc.json', {'id': 'run123', 'status': 'cancelled',
               'roles': [{'pane_id': 'w1:p2'}]})
        self.assertTrue(self.adopt()['adopted'])

    def test_other_team(self):
        path = h.STATE / 'teams' / 'other.json'
        h.save(path, {'roles': [{'pane_id': 'w1:p2'}]})
        with self.assertRaises(h.Error) as caught: self.adopt()
        self.assertIn(str(path), str(caught.exception))

    def test_session_race(self):
        original = h.herdr
        count = 0
        def racing(*args, **kwargs):
            nonlocal count
            result = original(*args, **kwargs)
            if args[:3] == ('agent', 'get', 'dev'):
                count += 1
                if count == 2: result['agent']['agent_session']['value'] = 'changed-session'
            return result
        with patch.object(h, 'herdr', side_effect=racing):
            with self.assertRaisesRegex(h.Error, 'changed during adopt'): self.adopt()
        self.assertFalse(h.state_file(h.context()).exists())

    def test_dry_run(self):
        self.args.dry_run = True
        result = self.adopt()
        self.assertTrue(result['dry_run']); self.assertTrue(result['no_mutations'])
        self.assertEqual(len(result['team']['roles']), 2)
        self.assertFalse(h.STATE.exists())

    def test_verify_session_drift_and_legacy(self):
        role = {'agent_name': 'dev', 'pane_id': 'w1:p2', 'kind': 'claude', 'cwd': str(self.cwd),
                'agent_session': copy.deepcopy(self.agents['dev']['agent_session'])}
        self.assertTrue(h.verify_existing(role))
        self.agents['dev']['agent_session']['value'] = 'changed-session'
        with self.assertRaisesRegex(h.Error, 'session identity changed'): h.verify_existing(role)
        del role['agent_session']
        self.assertTrue(h.verify_existing(role))


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        test_htm.RuntimeTests.setUp(self)

    def start(self, template='research'):
        h.init(self.cfg, opts(template, dry_run=False, cwd=self.tmp.name))
        return h.run_start(self.cfg, ns(goal='An isolated adversarial test'))['run_id']

    def dispatch(self, rid, role='researcher', no_wait=False, effect='read', remediation=False):
        return h.run_dispatch(ns(run_id=rid, role=role, message='Collect evidence for this fake task',
                                 effect=effect, no_wait=no_wait, remediation=remediation))

    def gate(self, rid, decision='pass'):
        return h.run_gate(ns(run_id=rid, decision=decision, evidence='Evidence reviewed in independent regression test'))

    def mutate_artifact(self, rid, dispatch_id, **changes):
        path = h.result_path(rid, dispatch_id)
        content = json.loads(path.read_text())
        content.update(changes)
        path.write_text(json.dumps(content))
        return path

    def test_init_records_readiness_attempts(self):
        team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        starts = [r for r in team['resources'] if r['operation'] == 'agent_start']
        self.assertTrue(starts)
        self.assertTrue(all(r['detail']['readiness_attempts'] == 1 for r in starts))
        self.assertTrue(all(r['detail']['readiness_shell_polls'] == 0 for r in starts))
        self.assertTrue(all(r['detail']['readiness_busy_retries'] == 0 for r in starts))
        self.assertIsNone(team['pending_operation'])

    def test_init_records_busy_retry_attempts(self):
        original = h.herdr
        start_calls = []
        def busy_twice(*args, **kwargs):
            if args[:2] == ('pane', 'get'):
                return {'pane': {'pane_id': args[2], 'workspace_id': 'w1', 'tab_id': 'w1:t1'}}
            if args[:2] == ('pane', 'process-info'):
                return {'process_info': {'shell_pid': 123, 'foreground_process_group_id': 123, 'foreground_processes': [
                    {'pid': 123, 'argv0': 'zsh'}]}}
            if args[:2] == ('agent', 'start'):
                start_calls.append(args)
                if len(start_calls) <= 2:
                    raise h.Error('agent_pane_busy')
            return original(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=busy_twice), patch.object(h.time, 'sleep'):
            team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        starts = [r for r in team['resources'] if r['operation'] == 'agent_start']
        self.assertEqual(team['status'], 'ready')
        self.assertEqual(len(start_calls), 4)
        self.assertEqual([c[2] for c in start_calls[:3]], [start_calls[0][2]] * 3)
        self.assertNotEqual(start_calls[3][2], start_calls[0][2])
        self.assertEqual(starts[0]['detail']['readiness_attempts'], 3)
        self.assertEqual(starts[1]['detail']['readiness_attempts'], 1)
        self.assertEqual([r['detail']['readiness_shell_polls'] for r in starts], [0, 0])
        self.assertEqual([r['detail']['readiness_busy_retries'] for r in starts], [2, 0])

    def test_init_start_failure_preserves_pending_operation(self):
        original = h.herdr
        def blocked(*args, **kwargs):
            if args[:2] == ('agent', 'start'):
                raise h.Error('agent_not_ready blocked during startup')
            return original(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=blocked):
            with self.assertRaisesRegex(h.Error, 'Initialization partially applied'):
                h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))
        team = h.load(h.state_file(h.context()))
        self.assertEqual(team['status'], 'partial')
        self.assertEqual(team['pending_operation']['operation'], 'agent_start')
        self.assertIn('agent_not_ready', team['error'])

    def test_permissions_change_fails_closed_on_reuse(self):
        self.start('dev-lite')
        with self.assertRaisesRegex(h.Error, 'different resolved config'):
            h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name,
                                  set=['governance.autonomy=read-only']))

    def test_workflow_changes_fail_closed_on_reuse(self):
        self.start('research')
        changed = copy.deepcopy(self.cfg)
        changed['templates']['research']['workflow'].append('verify')
        with self.assertRaisesRegex(h.Error, 'different resolved config'):
            h.init(changed, opts('research', dry_run=False, cwd=self.tmp.name))

    def test_stale_terminal_text_does_not_settle(self):
        rid = self.start()
        with patch.object(h, 'read_result', side_effect=h.Error('missing report')):
            out = self.dispatch(rid)
        self.assertEqual(out['status'], 'uncertain')
        self.assertNotIn('output', h.require_run(rid)['dispatches'][-1])
        with self.assertRaises(h.Error): self.gate(rid)

    def test_uncertain_warning_survives_ledger_recheck_failure(self):
        # A dispatch failure must never be reported as a clean failure: the input may
        # have been sent. This holds even if the follow-up ledger read also fails.
        # The recheck itself now goes through finalize(), which swallows its own
        # read errors and simply records nothing -- so the warning has to come
        # from the dispatch path alone.
        rid = self.start()
        state = {'prompt_failed': False}
        original_require_run = h.require_run
        def broken(*argv, **kwargs):
            if argv[:2] == ('agent', 'prompt'):
                state['prompt_failed'] = True
                raise h.Error('timeout')
            raise h.Error('identity probe unstable')
        def require_run(runid, **kwargs):
            if state['prompt_failed']:
                raise h.Error('identity probe unstable')
            return original_require_run(runid, **kwargs)
        with patch.object(h, 'herdr', side_effect=broken), \
                patch.object(h, 'require_run', side_effect=require_run):
            with self.assertRaisesRegex(h.Error, 'uncertain.*might have been sent'):
                self.dispatch(rid)
        self.assertTrue(state['prompt_failed'])

    def test_unknown_agent_state_does_not_settle_even_with_report(self):
        rid = self.start()
        def unknown(*argv, **kwargs):
            self.actions.append(argv)
            if argv[:2] == ('agent', 'prompt'):
                # Write the appropriate report as the ordinary fake implementation does.
                rec = h.require_run(rid)['dispatches'][-1]
                p = h.result_path(rid, rec['id'])
                p.write_text(json.dumps({'run_id':rid,'dispatch_id':rec['id'], 'role':'researcher',
                        'stage':'investigate','stage_attempt':1,'status':'complete','verdict':'pass',
                        'summary':'Report corresponding to the current test dispatch.',
                        'evidence':['Verified report identity in fake test']}))
                return {'agent':{'status':'unknown'}}
            raise AssertionError(argv)
        with patch.object(h, 'herdr', side_effect=unknown):
            out=self.dispatch(rid)
        self.assertEqual(out['status'], 'uncertain')
        with self.assertRaises(h.Error): self.gate(rid)

    def test_two_active_runs_same_team_rejected(self):
        rid = self.start()
        with self.assertRaisesRegex(h.Error, rid):
            h.run_start(self.cfg, ns(goal='Second conflicting task'))
        self.assertEqual(len(h.run_list()['unfinished']), 1)

    def test_two_processes_contend_for_same_workers(self):
        if 'fork' not in multiprocessing.get_all_start_methods():
            self.skipTest('fork multiprocessing is unavailable on this host')
        self.start()
        # Release the first run so both child processes race for the same Team.
        rid=h.run_list()['unfinished'][0]['run_id']
        with verified_cancel_plane():
            h.run_cancel(ns(run_id=rid,confirm_run_id=rid,reason='Cancel before concurrent stress test'))
        ctx=multiprocessing.get_context('fork')
        q=ctx.Queue(); barrier=ctx.Barrier(2)
        workers=[ctx.Process(target=start_in_child,args=(q,barrier)) for _ in range(2)]
        for w in workers:w.start()
        for w in workers:w.join(timeout=10)
        self.assertTrue(all(w.exitcode == 0 for w in workers))
        results=[q.get(timeout=2) for _ in workers]
        self.assertEqual(sorted(x[0] for x in results),['blocked','ok'])

    def test_paused_run_still_holds_workers(self):
        rid=self.start()
        h.run_pause(ns(run_id=rid), True)
        with self.assertRaisesRegex(h.Error,'reserved'):
            h.run_start(self.cfg, ns(goal='Illegal overlapping run'))

    def test_pending_no_wait_blocks_gate_even_after_prior_success(self):
        rid=self.start()
        first=self.dispatch(rid)
        self.assertEqual(first['status'],'settled')
        second=self.dispatch(rid,no_wait=True)
        self.assertEqual(second['status'],'submitted_unsettled')
        with self.assertRaisesRegex(h.Error, 'unresolved'):
            self.gate(rid)

    def test_wrong_run_identity_rejected(self):
        rid=self.start()
        a=self.dispatch(rid,no_wait=True)
        self.mutate_artifact(rid,a['dispatch_id'],run_id='0'*12)
        result=h.run_collect(ns(run_id=rid,dispatch_id=a['dispatch_id']))
        self.assertEqual(result['status'],'uncertain')
        self.assertIn('identity mismatch',result['error'])

    def test_wrong_attempt_identity_rejected(self):
        rid=self.start()
        a=self.dispatch(rid,no_wait=True)
        self.mutate_artifact(rid,a['dispatch_id'],stage_attempt=42)
        result=h.run_collect(ns(run_id=rid,dispatch_id=a['dispatch_id']))
        self.assertEqual(result['status'],'uncertain')

    def test_missing_result_artifact_never_uses_terminal_snapshot(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        h.result_path(rid,d['dispatch_id']).unlink()
        result=h.run_collect(ns(run_id=rid,dispatch_id=d['dispatch_id']))
        self.assertEqual(result['status'],'uncertain')
        self.assertIn('not found',result['error'])

    def test_reconcile_uncertain_then_settled_clears_error(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        run=h.require_run(rid)
        rec=run['dispatches'][-1]
        h.result_path(rid,d['dispatch_id']).unlink()
        rec.update(h.reconcile_result(run,rec,'done'))
        self.assertEqual(rec['status'],'uncertain')
        self.assertIn('not found',rec['error'])
        report={'run_id':rid,'dispatch_id':d['dispatch_id'],'role':rec['role'],
                'stage':run['stages'][rec['stage_index']],'stage_attempt':rec['stage_attempt'],
                'status':'complete','verdict':'pass',
                'summary':'A valid report restored after the missing artifact failure.',
                'evidence':['Verified restored artifact in regression test']}
        h.result_path(rid,d['dispatch_id']).write_text(json.dumps(report))
        rec.update(h.reconcile_result(run,rec,'done'))
        self.assertEqual(rec['status'],'settled')
        self.assertIsNone(rec['error'])

    def test_reconcile_timeout_error_then_settled_clears_error(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        run=h.require_run(rid)
        rec=run['dispatches'][-1]
        rec['status']='submission_uncertain'
        rec['error']='Agent prompt timed out'
        rec.update(h.reconcile_result(run,rec,'done'))
        self.assertEqual(rec['status'],'settled')
        self.assertIsNone(rec['error'])

    def test_reconcile_missing_artifact_preserves_error(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        run=h.require_run(rid)
        rec=run['dispatches'][-1]
        h.result_path(rid,d['dispatch_id']).unlink()
        rec.update(h.reconcile_result(run,rec,'done'))
        self.assertEqual(rec['status'],'uncertain')
        self.assertTrue(rec['error'])
        self.assertIn('not found',rec['error'])

    def test_reconcile_unexpected_status_preserves_error(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        run=h.require_run(rid)
        rec=run['dispatches'][-1]
        rec.update(h.reconcile_result(run,rec,'working'))
        self.assertEqual(rec['status'],'uncertain')
        self.assertEqual(rec['error'],'Unexpected agent status: working')

    def test_artifact_changed_after_collection_rejected(self):
        rid=self.start()
        d=self.dispatch(rid)
        self.mutate_artifact(rid,d['dispatch_id'],summary='A changed summary that invalidates digest binding')
        with self.assertRaisesRegex(h.Error,'changed'):
            self.gate(rid)

    def test_symlink_artifact_rejected(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        p=h.result_path(rid,d['dispatch_id'])
        actual=p.with_suffix('.elsewhere')
        p.rename(actual)
        p.symlink_to(actual)
        r=h.run_collect(ns(run_id=rid,dispatch_id=d['dispatch_id']))
        self.assertEqual(r['status'],'uncertain')
        self.assertIn('regular file',r['error'])

    def test_review_latest_failure_overrides_earlier_pass(self):
        rid=self.start('dev-review')
        self.gate(rid)
        one=self.dispatch(rid,role='reviewer')
        two=self.dispatch(rid,role='reviewer')
        self.assertEqual(two['status'],'settled')
        # Change second result before collection AND update bound digest safely via collect in a separate flow
        self.mutate_artifact(rid,two['dispatch_id'],verdict='fail')
        # Explicitly invalidate settled status to exercise re-collection.
        with h.locked():
            run=h.require_run(rid); rec=run['dispatches'][-1]; rec['status']='uncertain'; h.save(h.run_file(rid),run)
        h.run_collect(ns(run_id=rid, dispatch_id=two['dispatch_id']))
        with self.assertRaisesRegex(h.Error,'Latest reviewer artifact'):
            self.gate(rid)

    def test_selftest_cannot_pass_without_test_records(self):
        rid=self.start('dev-lite')
        self.gate(rid)
        self.dispatch(rid,role='developer')
        self.gate(rid)
        d=self.dispatch(rid,role='developer',no_wait=True)
        self.mutate_artifact(rid,d['dispatch_id'],tests=[])
        h.run_collect(ns(run_id=rid,dispatch_id=d['dispatch_id']))
        with self.assertRaisesRegex(h.Error,'Test stage requires'):
            self.gate(rid)

    def test_missing_agent_pane_rejects_reuse(self):
        rid=self.start()
        team=h.current_team()
        found=dict(self.started[team['roles'][0]['agent_name']]); found.pop('pane_id')
        with patch.object(h, 'lookup_agent',return_value=found):
            with self.assertRaisesRegex(h.Error,'no verifiable pane'):
                h.verify_existing(team['roles'][0])

    def test_hermes_profile_aliases_are_rejected(self):
        for args in (['--profile=other','chat'],['-pother','chat'],['--yolo','chat'],['--profile','other','chat']):
            cfg=copy.deepcopy(self.cfg)
            cfg['agents']['hermes-work']['launch_args']=args
            with self.subTest(args=args), self.assertRaisesRegex(h.Error, 'Hermes launch_args'):
                h.validate(cfg)

    def test_hermes_same_profile_managed_team_conflicts(self):
        self.start('hermes-guide')
        with patch.dict(os.environ, {'HERDR_PANE_ID':'w1:p99'}):
            with self.assertRaisesRegex(h.Error,'profile already reserved'):
                h.init(self.cfg,opts('hermes-guide',dry_run=False,cwd=self.tmp.name))

    def test_recovery_discovery_does_not_mutate_or_resend(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        before=len(self.actions)
        status=h.run_recover(ns(run_id=rid))
        self.assertEqual(status['pending_dispatches'][0]['id'],d['dispatch_id'])
        self.assertEqual(len(self.actions),before)
        self.assertEqual(h.run_list()['unfinished'][0]['run_id'],rid)

    def test_cancel_requires_explicit_reason_and_no_inflight(self):
        rid=self.start()
        d=self.dispatch(rid,no_wait=True)
        with self.assertRaisesRegex(h.Error,'unresolved'):
            h.run_cancel(ns(run_id=rid,confirm_run_id=rid,reason='Owner explicitly cancels test run'))
        h.run_collect(ns(run_id=rid,dispatch_id=d['dispatch_id']))
        with verified_cancel_plane():
            result=h.run_cancel(ns(run_id=rid,confirm_run_id=rid,reason='Owner explicitly cancels test run'))
        self.assertEqual(result['status'],'cancelled')
        self.assertFalse(result['finished'])
        h.run_start(self.cfg, ns(goal='New run after explicit cancellation'))

    def test_round_extension_disabled(self):
        rid=self.start()
        h.run_pause(ns(run_id=rid),True)
        with self.assertRaisesRegex(h.Error,'disabled'):
            h.run_pause(ns(run_id=rid,extend_rounds=1),False)

    def test_yaml_invalid_caught_as_error(self):
        if h.yaml is None:self.skipTest('PyYAML not installed')
        path=Path(self.tmp.name)/'bad.yaml'; path.write_text('defaults: [\n')
        with self.assertRaisesRegex(h.Error,'Unable to load'):
            h.read_cfg(path)


REAL_HERDR_095_AGENT = {
    "agent": "claude",
    "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude",
                      "value": "d3086539-ab07-42c9-8c68-5e653463594f"},
    "agent_status": "done",
    "completion_seq": 78,
    "cwd": "/Users/shakya/dev/herdr-team",
    "focused": False,
    "foreground_cwd": "/Users/shakya/dev/herdr-team",
    "interactive_ready": True,
    "name": "developer-w2",
    "pane_id": "w2:p2",
    "revision": 5,
    "state_change_seq": 78,
    "tab_id": "w2:t1",
    "terminal_id": "term_65f0d947e6e8a9",
    "workspace_id": "w2",
}


class AgentFieldMappingTests(unittest.TestCase):
    """Herdr 0.9.5 emits agent/agent_status; legacy kind/status must still work."""

    def test_real_095_shape_kind_and_status(self):
        for status in ('idle', 'done', 'working', 'blocked', 'unknown'):
            agent = dict(REAL_HERDR_095_AGENT, agent_status=status)
            with self.subTest(status=status):
                self.assertEqual(h.agent_kind_of(agent), 'claude')
                self.assertEqual(h.agent_status_of(agent), status)

    def test_wrapped_agent_get_result_shape(self):
        wrapped = {'agent': REAL_HERDR_095_AGENT}
        self.assertEqual(h.agent_kind_of(wrapped), 'claude')
        self.assertEqual(h.agent_status_of(wrapped), 'done')
        # Idempotent: unwrapping an already-unwrapped Agent must not treat the
        # string field "agent" as a nested object.
        self.assertIs(h.agent_data(REAL_HERDR_095_AGENT), REAL_HERDR_095_AGENT)

    def test_legacy_fields_still_supported(self):
        legacy = {'kind': 'claude', 'status': 'idle', 'pane_id': 'w1:p2'}
        self.assertEqual(h.agent_kind_of(legacy), 'claude')
        self.assertEqual(h.agent_status_of(legacy), 'idle')
        self.assertEqual(h.agent_kind_of({'agent_kind': 'hermes'}), 'hermes')

    def test_new_fields_take_precedence_over_legacy(self):
        both = {'agent': 'claude', 'kind': 'hermes', 'agent_status': 'done', 'status': 'blocked'}
        self.assertEqual(h.agent_kind_of(both), 'claude')
        self.assertEqual(h.agent_status_of(both), 'done')

    def test_agent_session_kind_is_not_agent_kind(self):
        only_session = {'agent_session': REAL_HERDR_095_AGENT['agent_session'], 'pane_id': 'w2:p2'}
        self.assertIsNone(h.agent_kind_of(only_session))
        self.assertEqual(h.agent_status_of(only_session), 'unknown')

    def test_missing_fields_fail_closed(self):
        self.assertIsNone(h.agent_kind_of({'pane_id': 'w2:p2'}))
        self.assertEqual(h.agent_status_of({'pane_id': 'w2:p2'}), 'unknown')
        self.assertEqual(h.agent_status_of({'agent_status': ''}), 'unknown')
        self.assertEqual(h.agent_status_of(None), 'unknown')
        self.assertIsNone(h.agent_kind_of(None))

    def test_verify_existing_accepts_real_095_agent(self):
        cwd = str(Path(REAL_HERDR_095_AGENT['cwd']).resolve())
        role = {'agent_name': 'developer-w2', 'pane_id': 'w2:p2', 'kind': 'claude', 'cwd': cwd}
        with patch.object(h, 'lookup_agent', return_value=REAL_HERDR_095_AGENT):
            self.assertTrue(h.verify_existing(role))

    def test_verify_existing_rejects_missing_kind(self):
        role = {'agent_name': 'developer-w2', 'pane_id': 'w2:p2', 'kind': 'claude'}
        found = {k: v for k, v in REAL_HERDR_095_AGENT.items() if k != 'agent'}
        with patch.object(h, 'lookup_agent', return_value=found):
            with self.assertRaisesRegex(h.Error, 'no verifiable kind'):
                h.verify_existing(role)

    def test_verify_existing_skips_cwd_check_when_absent(self):
        role = {'agent_name': 'developer-w2', 'pane_id': 'w2:p2', 'kind': 'claude',
                'cwd': '/some/other/checkout'}
        found = {k: v for k, v in REAL_HERDR_095_AGENT.items() if k != 'cwd'}
        with patch.object(h, 'lookup_agent', return_value=found):
            self.assertTrue(h.verify_existing(role))

    def test_verify_existing_still_rejects_cwd_mismatch(self):
        role = {'agent_name': 'developer-w2', 'pane_id': 'w2:p2', 'kind': 'claude',
                'cwd': '/some/other/checkout'}
        with patch.object(h, 'lookup_agent', return_value=REAL_HERDR_095_AGENT):
            with self.assertRaisesRegex(h.Error, 'CWD mismatch'):
                h.verify_existing(role)


class PaneShellReadinessTests(unittest.TestCase):
    def fake(self, start_effect=None):
        calls = []
        def herdr(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('pane', 'get'):
                return {'pane': {'workspace_id': 'w6'}}
            if args[:2] == ('pane', 'process-info'):
                return {'process_info': {'shell_pid': 47185, 'foreground_process_group_id': 47185, 'foreground_processes': [
                    {'pid': 47185, 'argv0': '/bin/zsh'}]}}
            if args[:2] == ('agent', 'start'):
                if start_effect:
                    return start_effect()
                return {'agent': {'name': 'worker'}}
            raise AssertionError(args)
        return calls, herdr

    def test_pane_ready_starts_once_without_waiting(self):
        calls, fake = self.fake()
        with patch.object(h, 'herdr', side_effect=fake), patch.object(h.time, 'sleep') as sleep:
            result, attempts = h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6',
                                                         {'w6:p2'}, ['--', '--flag'], interval=0)
        self.assertEqual((result, attempts), ({'agent': {'name': 'worker'}},
                                               {'shell_polls': 0, 'busy_retries': 0, 'attempts': 1}))
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 1)
        self.assertEqual(calls[-1][-2:], ('--', '--flag'))
        sleep.assert_not_called()

    def test_pane_busy_twice_then_starts(self):
        outcomes = iter([h.Error('agent_pane_busy'), h.Error('agent_pane_busy'), {'ok': True}])
        def start():
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value
        calls, fake = self.fake(start)
        with patch.object(h, 'herdr', side_effect=fake):
            result, attempts = h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'}, interval=0)
        self.assertEqual((result, attempts), ({'ok': True},
                                               {'shell_polls': 0, 'busy_retries': 2, 'attempts': 3}))
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 3)
        self.assertEqual(sum(c[:2] == ('pane', 'process-info') for c in calls), 3)

    def test_stats_shell_polls_only(self):
        calls, fake = self.fake()
        ready = iter([False, False, True])
        def poll(pane_id, **kw):
            value = next(ready)
            if not value:
                self.assertFalse(any(c[:2] == ('agent', 'start') for c in calls))
            return value
        with patch.object(h, 'herdr', side_effect=fake), patch.object(h, 'pane_shell_ready', side_effect=poll), patch.object(h.time, 'sleep'):
            result, stats = h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'})
        self.assertEqual(result, {'agent': {'name': 'worker'}})
        self.assertEqual(stats, {'shell_polls': 2, 'busy_retries': 0, 'attempts': 3})
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 1)

    def test_stats_busy_retries_only(self):
        outcomes = iter([h.Error('agent_pane_busy'), h.Error('agent_pane_busy'), {'ok': True}])
        def start():
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value
        calls, fake = self.fake(start)
        with patch.object(h, 'herdr', side_effect=fake), patch.object(h.time, 'sleep'):
            result, stats = h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'})
        self.assertEqual(result, {'ok': True})
        self.assertEqual(stats, {'shell_polls': 0, 'busy_retries': 2, 'attempts': 3})
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 3)

    def test_stats_mixed_shell_and_busy(self):
        outcomes = iter([h.Error('agent_pane_busy'), {'ok': True}])
        def start():
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value
        calls, fake = self.fake(start)
        with patch.object(h, 'herdr', side_effect=fake), patch.object(h, 'pane_shell_ready', side_effect=[False, True, True]), patch.object(h.time, 'sleep'):
            result, stats = h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'})
        self.assertEqual(result, {'ok': True})
        self.assertEqual(stats, {'shell_polls': 1, 'busy_retries': 1, 'attempts': 3})
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 2)

    def test_pane_busy_exhaustion_fails_closed(self):
        def start():
            raise h.Error('agent_pane_busy')
        calls, fake = self.fake(start)
        with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'stayed busy after 3 readiness checks'):
            h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'}, attempts=3, interval=0)
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 3)

    def test_pane_blocked_does_not_retry_or_send_input(self):
        def start():
            raise h.Error('agent_not_ready blocked during startup')
        calls, fake = self.fake(start)
        with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'agent_not_ready'):
            h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'}, interval=0)
        self.assertEqual(sum(c[:2] == ('agent', 'start') for c in calls), 1)
        self.assertFalse(any('send-keys' in c or 'prompt' in c for c in calls))

    def test_pane_not_created_by_init_rejected_immediately(self):
        calls, fake = self.fake()
        with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'not created'):
            h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', set(), interval=0)
        self.assertEqual(calls, [])

    def test_pane_readiness_shell_argv0_prefix(self):
        for argv0, expected in [('-zsh', True), ('zsh', True), ('-python', False)]:
            result = {'process_info': {'shell_pid': 1, 'foreground_process_group_id': 1,
                                       'foreground_processes': [{'pid': 1, 'argv0': argv0}]}}
            with self.subTest(argv0=argv0), patch.object(h, 'herdr', return_value=result):
                self.assertEqual(h.pane_shell_ready('w6:p2'), expected)

    def test_pane_readiness_foreground_group(self):
        shell = {'pid': 70608, 'argv0': 'zsh'}
        cases = [
            (70608, [shell], True),
            (70608, [shell, {'pid': 70609, 'argv0': 'id'}], True),
            (70932, [{'pid': 70932, 'argv0': 'sleep'}], False),
            (70608, [{'pid': 70609, 'argv0': 'zsh'}], False),
            (True, [shell], False),
            ('70608', [shell], False),
        ]
        for pgid, processes, expected in cases:
            result = {'process_info': {'shell_pid': 70608, 'foreground_process_group_id': pgid,
                                       'foreground_processes': processes}}
            with self.subTest(pgid=pgid, processes=processes), patch.object(h, 'herdr', return_value=result):
                self.assertEqual(h.pane_shell_ready('w6:p2'), expected)
        result = {'process_info': {'shell_pid': 70608, 'foreground_processes': [shell]}}
        with patch.object(h, 'herdr', return_value=result):
            self.assertFalse(h.pane_shell_ready('w6:p2'))

    def test_pane_readiness_invalid_and_non_shell_fail_closed(self):
        cases = [None, {}, {'process_info': {}},
                 {'process_info': {'shell_pid': 1, 'foreground_processes': []}},
                 {'process_info': {'shell_pid': 1, 'foreground_process_group_id': 1, 'foreground_processes': [{'pid': 1, 'argv0': 'python'}]}},
                 {'process_info': {'shell_pid': 1, 'foreground_process_group_id': 1, 'foreground_processes': [{'pid': 2, 'argv0': 'zsh'}]}},
                 {'process_info': {'shell_pid': True, 'foreground_process_group_id': 1, 'foreground_processes': [{'pid': 1, 'argv0': 'zsh'}]}}]
        for result in cases:
            with self.subTest(result=result), patch.object(h, 'herdr', return_value=result):
                self.assertFalse(h.pane_shell_ready('w6:p2'))
        with patch.object(h, 'herdr', side_effect=h.Error('unavailable')):
            self.assertFalse(h.pane_shell_ready('w6:p2'))

    def test_pane_not_ready_is_rechecked_without_start(self):
        calls, fake = self.fake()
        def pending(*args, **kwargs):
            if args[:2] == ('pane', 'process-info'):
                calls.append(args)
                return {'process_info': {'shell_pid': 1, 'foreground_processes': []}}
            return fake(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=pending), patch.object(h.time, 'sleep') as sleep:
            with self.assertRaisesRegex(h.Error, 'stayed busy after 3 readiness checks'):
                h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'}, attempts=3, interval=0)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(any(c[:2] == ('agent', 'start') for c in calls))

    def test_pane_workspace_mismatch_rejected(self):
        with patch.object(h, 'herdr', return_value={'pane': {'workspace_id': 'other'}}) as mock:
            with self.assertRaisesRegex(h.Error, 'different workspace'):
                h.start_agent_when_ready('worker', 'claude', 'w6:p2', 'w6', {'w6:p2'}, interval=0)
            self.assertEqual(mock.call_count, 1)


class WorktreeSafetyTests(unittest.TestCase):
    def test_dirty_head_rejected_before_worktree_create(self):
        cfg,_=h.read_cfg(h.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo'; repo.mkdir()
            subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            (repo/'readme').write_text('initial')
            subprocess.run(['git','-C',str(repo),'add','readme'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.email=fake@example.org',
                '-c','user.name=Test','commit','-m','initial'],check=True,capture_output=True)
            (repo/'readme').write_text('dirty')
            with self.assertRaisesRegex(h.Error,'dirty'):
                h.worktree_details(h.initial_plan(cfg,'dev-review',opts('dev-review',worktree='on')),
                                   opts('dev-review',worktree='on',repo=str(repo),task='dirty-check'),False)
            self.assertFalse((Path(tmp)/'managed').exists())


class PaneNamingTests(unittest.TestCase):
    def setUp(self):
        test_htm.RuntimeTests.setUp(self)

    def lead_fake(self, pane_session='original-session', agent_session='original-session', pane_kind='claude', agent_kind='claude'):
        session = lambda value: {'kind': 'claude', 'source': 'test', 'value': value}
        pane = {'pane_id': 'w1:p1', 'workspace_id': 'w1', 'agent': pane_kind, 'agent_session': session(pane_session)}
        agent = {'pane_id': 'w1:p1', 'name': 'old-lead', 'agent': agent_kind, 'agent_session': session(agent_session)}
        calls = []
        def fake(*args, **kwargs):
            calls.append(args)
            if args == ('pane', 'get', 'w1:p1'):
                return {'pane': {**pane, 'tab_id': 'w1:t1'}}
            if args == ('agent', 'get', 'w1:p1'): return {'agent': copy.deepcopy(agent)}
            if args == ('agent', 'get', 'lead-w1p1'): raise h.HerdrError('not found', code='agent_not_found')
            if args[:2] == ('agent', 'rename'): return {}
            raise AssertionError(args)
        return pane, agent, calls, fake

    def test_name_lead_apply_requires_expect_session(self):
        _, _, calls, fake = self.lead_fake('replacement-session', 'replacement-session')
        with patch.object(h, 'herdr', side_effect=fake):
            for expected in (None, '', '   '):
                with self.subTest(expected=expected), self.assertRaisesRegex(h.Error, '预览.*agent_session.value.*--expect-session'):
                    h.name_lead(ns(apply=True, current=None, expect_session=expected))
            with self.assertRaisesRegex(h.Error, '预览.*--expect-session'):
                h.name_lead(ns(apply=True, current=None))
        self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_apply_rejects_wrong_expect_session(self):
        _, _, calls, fake = self.lead_fake('replacement-session', 'replacement-session')
        with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'Expected session'):
            h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
        self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_preview_creates_no_state(self):
        _, _, calls, fake = self.lead_fake()
        self.assertFalse(h.STATE.exists())
        with patch.object(h, 'herdr', side_effect=fake):
            result = h.name_lead(ns(apply=False, current=None))
        self.assertEqual(result['agent_session']['value'], 'original-session')
        self.assertFalse(h.STATE.exists())
        self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_apply_lock_scope_and_team_recheck(self):
        path = h.state_file(h.context())
        h.save(path, {'context': h.context(), 'status': 'ready', 'roles': []})
        _, _, calls, fake = self.lead_fake()
        inside = False
        original_locked = h.locked
        @contextlib.contextmanager
        def tracked_lock():
            nonlocal inside
            with original_locked():
                inside = True
                try: yield
                finally: inside = False
        def tracked_herdr(*args, **kwargs):
            if args == ('pane', 'get', 'w1:p1') or args == ('agent', 'get', 'lead-w1p1'):
                self.assertFalse(inside)
            elif args[:2] == ('agent', 'rename'):
                self.assertTrue(inside)
            return fake(*args, **kwargs)
        with patch.object(h, 'locked', tracked_lock), patch.object(h, 'herdr', side_effect=tracked_herdr):
            h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
        self.assertEqual(sum(c[:2] == ('agent', 'rename') for c in calls), 1)
        calls.clear()
        @contextlib.contextmanager
        def changed_team_lock():
            with original_locked():
                h.save(path, {'context': h.context(), 'status': 'partial', 'roles': []})
                yield
        with patch.object(h, 'locked', changed_team_lock), patch.object(h, 'herdr', side_effect=fake):
            with self.assertRaisesRegex(h.Error, 'not ready'):
                h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
        self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_rejects_identity_mismatch(self):
        for pane_kind, agent_kind, pane_session, agent_session in (
                ('claude', 'codex', 'original-session', 'original-session'),
                ('claude', 'claude', 'original-session', 'replacement-session')):
            with self.subTest(agent_kind=agent_kind, agent_session=agent_session):
                _, _, calls, fake = self.lead_fake(pane_session, agent_session, pane_kind, agent_kind)
                with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'identity'):
                    h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
                self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_rejects_unverifiable_session(self):
        for value in (None, '', 'short', '        '):
            with self.subTest(value=value):
                _, _, calls, fake = self.lead_fake(value, value)
                with patch.object(h, 'herdr', side_effect=fake), self.assertRaisesRegex(h.Error, 'identity'):
                    h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
                self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))

    def test_name_lead_rechecks_session_around_rename(self):
        for change_at in (2, 3):
            with self.subTest(change_at=change_at):
                _, agent, calls, fake = self.lead_fake()
                count = 0
                def changing(*args, **kwargs):
                    nonlocal count
                    if args == ('agent', 'get', 'w1:p1'):
                        count += 1
                        if count == change_at: agent['agent_session']['value'] = 'replacement-session'
                    return fake(*args, **kwargs)
                message = 'before rename' if change_at == 2 else 'rename 后身份已变化，需人工核对'
                with patch.object(h, 'herdr', side_effect=changing), self.assertRaisesRegex(h.Error, message):
                    h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
                renames = [c for c in calls if c[:2] == ('agent', 'rename')]
                self.assertEqual(len(renames), 0 if change_at == 2 else 1)

    def test_require_name_available_structured_code(self):
        path = h.state_file(h.context())
        with patch.object(h, 'herdr', side_effect=h.HerdrError('agent_not_found', code='permission_denied')):
            with self.assertRaisesRegex(h.Error, 'Cannot verify'): h.require_name_available('lead-w1p1', path)
        with patch.object(h, 'herdr', side_effect=h.HerdrError('not found', code='agent_not_found')):
            h.require_name_available('lead-w1p1', path)
        for rc, stdout, stderr, code in ((1, '{"error":{"code":"agent_not_found"}}', '', 'agent_not_found'),
                                         (1, '', '{"error":{"code":"agent_not_found"}}', 'agent_not_found'),
                                         (1, '', '{"error":{"code":"permission_denied","message":"agent_not_found"}}', 'permission_denied'),
                                         (1, '{"error":{"code":"agent_not_found"}}', '{"error":{"code":"permission_denied"}}', 'permission_denied'),
                                         (0, '{"error":{"code":"agent_not_found"}}', '', 'agent_not_found')):
            with self.subTest(rc=rc, stdout=stdout, stderr=stderr), patch.object(h.subprocess, 'run', return_value=ns(returncode=rc, stdout=stdout, stderr=stderr)):
                with self.assertRaises(h.HerdrError) as caught: h.process(['herdr', 'agent', 'get'])
                self.assertEqual(caught.exception.code, code)

    def test_init_rejects_overlong_role_before_resources(self):
        cfg = copy.deepcopy(self.cfg)
        roles = cfg['templates']['dev-lite']['roles']
        roles['a' * 27] = roles.pop('developer')
        args = opts('dev-lite', dry_run=False, cwd=self.tmp.name)
        before = len(self.actions)
        with patch.object(h, 'save', wraps=h.save) as save:
            with self.assertRaisesRegex(h.Error, 'role 名过长'): h.init(cfg, args)
        save.assert_not_called()
        self.assertEqual(len(self.actions), before)
        self.assertFalse(list((h.STATE / 'teams').glob('*.json')))
        roles['a' * 26] = roles.pop('a' * 27)
        team = h.init(cfg, args)['team']
        self.assertIn('a' * 26, [role['role'] for role in team['roles']])

    def test_name_lead_without_team(self):
        _, _, calls, fake = self.lead_fake()
        with patch.object(h, 'herdr', side_effect=fake):
            preview = h.name_lead(ns(apply=False, current=None))
            self.assertIsNone(preview['team'])
            self.assertTrue(preview['verified'])
            self.assertEqual(preview['agent_session'], {'kind': 'claude', 'source': 'test', 'value': 'original-session'})
            self.assertFalse(any(c[:2] == ('agent', 'rename') for c in calls))
            with self.assertRaisesRegex(h.Error, 'Expected session'):
                h.name_lead(ns(apply=True, current=None, expect_session='different-session'))
            result = h.name_lead(ns(apply=True, current=None, expect_session=preview['agent_session']['value']))
            self.assertIsNone(result['team'])
            self.assertEqual(sum(c[:2] == ('agent', 'rename') for c in calls), 1)
            h.save(h.state_file(h.context()), {'context': {'workspace': 'w2'}, 'status': 'ready'})
            with self.assertRaisesRegex(h.Error, 'context mismatch'):
                h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
            self.assertEqual(sum(c[:2] == ('agent', 'rename') for c in calls), 1)

    def test_multiple_workspaces_and_same_role(self):
        self.assertEqual(h.agent_name_for('developer', 'w1:p2'), 'developer-w1p2')
        self.assertEqual(h.agent_name_for('developer', 'w2:p2'), 'developer-w2p2')
        self.assertEqual(h.agent_name_for('developer', 'w2:p3'), 'developer-w2p3')
        self.assertNotEqual(h.state_file({'workspace': 'w1', 'lead_pane': 'w1:p1'}),
                            h.state_file({'workspace': 'w2', 'lead_pane': 'w2:p1'}))

    def test_uppercase_public_id_preserves_pane(self):
        self.assertEqual(h.agent_name_for('developer', 'wA:pB'), 'developer-wapb')
        original = h.herdr
        with patch.object(h, 'herdr') as fake:
            def upper(*args, **kwargs):
                if args[:2] == ('pane', 'split'):
                    self.panes += 1
                    return {'pane': {'pane_id': f'wA:p{self.panes}'}}
                if args[:2] == ('pane', 'get'):
                    return {'pane': {'pane_id': args[2], 'workspace_id': 'wA', 'tab_id': 'wA:t1'}}
                return original(*args, **kwargs)
            fake.side_effect = upper
            with patch.dict(os.environ, {'HERDR_WORKSPACE_ID':'wA', 'HERDR_PANE_ID':'wA:p1', 'HERDR_TAB_ID':'wA:t1'}):
                team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        self.assertEqual(team['roles'][0]['pane_id'], 'wA:p2')
        self.assertEqual(team['roles'][0]['agent_name'], 'developer-wap2')

    def test_name_length_and_invalid_fail_closed(self):
        self.assertEqual(len(h.agent_name_for('a'*27, 'w1:p1')), 32)
        with self.assertRaisesRegex(h.Error, 'Invalid derived'): h.agent_name_for('a'*28, 'w1:p1')
        with self.assertRaisesRegex(h.Error, 'Invalid pane'): h.agent_name_for('developer', 'wa:p1')
        with self.assertRaisesRegex(h.Error, 'Invalid derived'): h.agent_name_for('bad.role', 'w1:p1')

    def test_name_conflicts_preserve_partial_and_block_retry(self):
        self.started['developer-w1p2'] = {'pane_id': 'w1:p99'}
        args = opts('dev-lite', dry_run=False, cwd=self.tmp.name)
        with self.assertRaisesRegex(h.Error, 'already created; agent not named or started'):
            h.init(self.cfg, args)
        team = h.load(h.state_file(h.context()))
        self.assertEqual(team['status'], 'partial')
        self.assertEqual(team['roles'], [])
        self.assertTrue(any(x['operation'] == 'pane_split' for x in team['resources']))
        self.assertFalse(any(x[:2] == ('agent','start') for x in self.actions))
        with self.assertRaisesRegex(h.Error, 'partial'): h.init(self.cfg, args)

    def test_registered_conflicts_and_duplicate_roles(self):
        path = h.state_file(h.context())
        with self.assertRaisesRegex(h.Error, 'this team'):
            h.require_name_available('developer-w1p2', path, [{'agent_name':'developer-w1p2'}])
        h.save(h.STATE/'teams'/'other.json', {'roles':[{'agent_name':'developer-w1p2'}]})
        with self.assertRaisesRegex(h.Error, 'registered by team'):
            h.require_name_available('developer-w1p2', path)

    def test_name_availability_unknown_fails_closed(self):
        path = h.state_file(h.context())
        def unavailable(*args, **kwargs):
            raise h.Error('server unavailable')
        with patch.object(h, 'herdr', side_effect=unavailable):
            with self.assertRaisesRegex(h.Error, 'Cannot verify'):
                h.require_name_available('developer-w1p2', path)

    def test_plan_is_pending_without_changing_persisted_plan(self):
        args = opts('dev-lite', dry_run=True, cwd=self.tmp.name)
        result = h.init(self.cfg, args)
        self.assertTrue(all(r['agent_name'] is None and r['pending'] for r in result['plan']['roles']))
        self.assertNotIn('agent_name', h.initial_plan(self.cfg, 'dev-lite', args)['roles'][0])
        self.assertFalse(self.actions)

    def test_history_hash_team_reuse_no_rename(self):
        args = opts('dev-lite', dry_run=False, cwd=self.tmp.name)
        team = h.init(self.cfg, args)['team']
        for role in team['roles']:
            old = role['agent_name']; name = 'htabcdef-' + role['role']
            self.started[name] = self.started.pop(old); role['agent_name'] = name
        h.save(h.state_file(h.context()), team)
        before = len(self.actions)
        result = h.init(self.cfg, args)
        self.assertTrue(result['reused'])
        self.assertEqual(len(self.actions), before)
        self.assertTrue(all(r['agent_name'].startswith('htabcdef-') for r in result['team']['roles']))

    def test_agent_move_and_identity_drift_rejected(self):
        team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        role = team['roles'][0]
        self.started[role['agent_name']]['pane_id'] = 'w2:p2'
        with self.assertRaisesRegex(h.Error, 'unexpected pane'): h.verify_existing(role)
        self.started[role['agent_name']]['pane_id'] = role['pane_id']
        role['agent_session'] = {'value':'original-session'}
        self.started[role['agent_name']]['agent_session'] = {'value':'changed-session'}
        with self.assertRaisesRegex(h.Error, 'identity changed'): h.verify_existing(role)

    def test_start_binding_persisted_before_herdr_start(self):
        original = h.herdr
        def check(*args, **kwargs):
            if args[:2] == ('agent','start'):
                team = h.load(h.state_file(h.context()))
                self.assertEqual(team['roles'][-1]['agent_name'], args[2])
                self.assertEqual(team['roles'][-1]['pane_id'], args[args.index('--pane')+1])
                raise h.Error('blocked')
            return original(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=check):
            with self.assertRaisesRegex(h.Error, 'partially applied'): h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))
        team = h.load(h.state_file(h.context()))
        self.assertEqual(team['pending_operation']['operation'], 'agent_start')

    def test_lead_preview_and_apply_with_verified_identity(self):
        team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        def fake(*args, **kwargs):
            if args[:2] == ('pane','get'):
                return {'pane': {'pane_id':'w1:p1', 'workspace_id':'w1', 'tab_id':'w1:t1', 'agent':'claude', 'agent_session':{'kind':'claude', 'source':'test', 'value':'original-session'}}}
            if args[:2] == ('agent','get') and args[2] == 'w1:p1':
                return {'agent': {'name':'old-lead', 'pane_id':'w1:p1', 'agent':'claude', 'agent_session':{'kind':'claude', 'source':'test', 'value':'original-session'}}}
            if args[:2] == ('agent','get'):
                raise h.HerdrError('not found', code='agent_not_found')
            if args[:2] == ('agent','rename'):
                self.actions.append(args); return {}
            raise AssertionError(args)
        with patch.object(h, 'herdr', side_effect=fake):
            preview = h.name_lead(ns(apply=False, current=None))
            self.assertEqual(preview['new'], 'lead-w1p1')
            self.assertIsInstance(preview['team'], str)
            self.assertEqual(preview['team'], str(h.state_file(h.context())))
            self.assertFalse(any(a[:2] == ('agent','rename') for a in self.actions))
            result = h.name_lead(ns(apply=True, current='old-lead', expect_session=preview['agent_session']['value']))
            self.assertEqual(result['team'], preview['team'])
            self.assertIn(('agent','rename','w1:p1','lead-w1p1'), self.actions)
            with self.assertRaisesRegex(h.Error, 'does not match'): h.name_lead(ns(apply=True, current='wrong', expect_session='original-session'))
            team['status'] = 'partial'; h.save(h.state_file(h.context()), team)
            with self.assertRaisesRegex(h.Error, 'not ready'): h.name_lead(ns(apply=True, current=None, expect_session='original-session'))

    def test_unnamed_lead_uses_pane_target_and_rejects_collision(self):
        h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))
        occupied = False
        unavailable = False
        def fake(*args, **kwargs):
            if args[:2] == ('pane', 'get'):
                return {'pane': {'pane_id':'w1:p1', 'workspace_id':'w1', 'tab_id':'w1:t1', 'agent':'claude', 'agent_session':{'kind':'claude', 'source':'test', 'value':'original-session'}}}
            if args == ('agent', 'get', 'w1:p1'):
                if unavailable: raise h.Error('pane lookup unavailable')
                return {'agent': {'name':None, 'pane_id':'w1:p1', 'agent':'claude', 'agent_session':{'kind':'claude', 'source':'test', 'value':'original-session'}}}
            if args == ('agent', 'get', 'lead-w1p1'):
                if occupied: return {'agent': {'name':'lead-w1p1', 'pane_id':'w1:p9'}}
                raise h.HerdrError('not found', code='agent_not_found')
            if args[:2] == ('agent', 'rename'):
                self.actions.append(args); return {}
            raise AssertionError(args)
        with patch.object(h, 'herdr', side_effect=fake):
            preview = h.name_lead(ns(apply=False, current=None))
            self.assertIsNone(preview['current'])
            self.assertEqual(preview['new'], 'lead-w1p1')
            h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
            self.assertIn(('agent','rename','w1:p1','lead-w1p1'), self.actions)
            occupied = True
            with self.assertRaisesRegex(h.Error, 'already occupied'):
                h.name_lead(ns(apply=True, current=None, expect_session='original-session'))
            unavailable = True
            with self.assertRaisesRegex(h.Error, 'pane lookup unavailable'):
                h.name_lead(ns(apply=True, current=None, expect_session='original-session'))


class AdoptNamingTests(AdoptTests):
    def test_legacy_and_workspace_names_adopt_without_rename(self):
        for names in (('developer-w1','reviewer-w1'), ('htabcdef-1-developer','htabcdef-2-reviewer')):
            with self.subTest(names=names):
                self.agents = {name: dict(self.agents[old], name=name) for old, name in zip(self.agents, names)}
                self.args.role = [f'developer={names[0]}', f'reviewer={names[1]}']
                self.args.dry_run = True
                team = self.adopt()['team']
                self.assertEqual([r['agent_name'] for r in team['roles']], list(names))
                self.assertTrue(all(c[:2] in (('agent','get'),('pane','get')) for c in self.calls))

    def test_invalid_and_ambiguous_adopt_never_guesses(self):
        self.args.role = ['developer=Developer', 'reviewer=review']
        with self.assertRaisesRegex(h.Error, 'Invalid agent name'): self.adopt()
        self.args.role = []
        with self.assertRaisesRegex(h.Error, 'missing'): self.adopt()
        # Rejected during validation; no Herdr read or write is attempted at all.
        self.assertFalse(self.calls)


class LedgerIdentityTests(unittest.TestCase):
    """Cross-session ledger ownership: session names are a weak signal only.

    A Herdr session can be stopped and recreated under the same name with the
    same workspace/pane IDs but entirely different agents, so ownership of a
    legacy ledger must be proven through Worker agent_session UUIDs.
    """

    def setUp(self):
        test_htm.RuntimeTests.setUp(self)
        h.init(self.cfg, opts('research', dry_run=False, cwd=self.tmp.name))
        self.rid = h.run_start(self.cfg, ns(goal='Ledger ownership regression'))['run_id']
        # The fake Herdr does not report agent_session; supply the live UUIDs so
        # verify_existing can exercise the real comparison logic.
        self.live = {role['agent_name']: {'pane_id': role['pane_id'], 'kind': role['kind'],
                                          'cwd': role['cwd'], 'status': 'idle',
                                          'agent_session': {'agent': role['kind'], 'kind': 'id',
                                                            'source': 'herdr:claude',
                                                            'value': 'live-uuid-' + role['pane_id']}}
                     for role in h.require_run(self.rid)['roles']}
        live = self.live
        lookup = patch.object(h, 'lookup_agent', side_effect=lambda n, **kw: live.get(n))
        lookup.start(); self.addCleanup(lookup.stop)

    def ledger(self, rid=None, **changes):
        path = h.run_file(rid or self.rid)
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))
        return path

    def ledger_with_uuids(self, uuid_for=None):
        """Rewrite the Run as a pre-fix (session-less) ledger with explicit Worker UUIDs.

        The fake Herdr records each started agent without an agent_session, so the
        ledger roles are seeded here: that is exactly the legacy shape under test.
        """
        path = h.run_file(self.rid)
        data = json.loads(path.read_text())
        data.pop('session', None)
        for role in data['roles']:
            role['agent_session'] = {'agent': role['kind'], 'kind': 'id', 'source': 'herdr:claude',
                                     'value': (uuid_for or {}).get(role['role'], 'live-uuid-' + role['pane_id'])}
        path.write_text(json.dumps(data))
        return data

    def test_new_ledger_records_session(self):
        self.assertEqual(h.require_run(self.rid)['session'], 'default')
        team = json.loads(h.state_file(h.context()).read_text())
        self.assertEqual(team['session'], 'default')

    def test_new_ledger_session_mismatch_rejects_every_tier(self):
        self.ledger(session='other-session')
        for tier in ('A', 'B', 'C'):
            with self.subTest(tier=tier):
                with self.assertRaisesRegex(h.Error, '属于 session'):
                    h.require_run(self.rid, tier=tier)

    def test_legacy_ledger_readonly_tier_allowed(self):
        self.ledger_with_uuids()
        self.assertEqual(h.require_run(self.rid, tier='A')['id'], self.rid)

    def test_legacy_ledger_writable_when_uuid_matches(self):
        self.ledger_with_uuids()
        self.assertEqual(h.require_run(self.rid, tier='C')['id'], self.rid)

    def test_legacy_ledger_rejects_after_session_reuse(self):
        # The original session is gone; a new single-session context reuses the
        # same IDs but presents different Worker UUIDs. Counting running
        # sessions would not catch this -- only UUID comparison does.
        role = h.require_run(self.rid)['roles'][0]['role']
        self.ledger_with_uuids({role: '00000000-dead-beef-0000-000000000000'})
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')
        # Read-only diagnostics still work.
        self.assertEqual(h.require_run(self.rid, tier='A')['id'], self.rid)

    def test_same_name_session_recreated_different_uuid_rejected(self):
        # Session name matches ('default') but the Workers are different agents.
        # The session name alone must never be treated as proof of identity.
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '11111111-dead-beef-0000-000000000000' for r in data['roles']})
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')

    def test_verified_ledger_also_checks_worker_uuids(self):
        # A ledger that records a matching session must still prove its Workers:
        # the session could have been recreated under the same name.
        data = json.loads(h.run_file(self.rid).read_text())
        self.assertEqual(data['session'], 'default')
        for role in data['roles']:
            role['agent_session'] = {'agent': role['kind'], 'kind': 'id', 'source': 'herdr:claude',
                                     'value': '99999999-dead-beef-0000-000000000000'}
        path = h.run_file(self.rid); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')
        # Read-only stays available.
        self.assertEqual(h.require_run(self.rid, tier='A')['id'], self.rid)

    def test_verified_ledger_with_unbound_role_is_not_permissive(self):
        data = json.loads(h.run_file(self.rid).read_text())
        for role in data['roles']:
            role.pop('agent_session', None)
        path = h.run_file(self.rid); path.write_text(json.dumps(data))
        with self.assertRaisesRegex(h.Error, '未记录任何 Worker 身份'):
            h.require_run(self.rid, tier='C')

    def test_unobservable_worker_is_not_reported_as_conflict(self):
        # A live-but-unreadable Worker is an observability gap, not a claim that
        # the ledger belongs to someone else; tier C still has an anchor here.
        role = h.require_run(self.rid)['roles'][0]
        self.live[role['agent_name']] = {k: v for k, v in self.live[role['agent_name']].items()
                                         if k != 'pane_id'}
        self.ledger_with_uuids()
        self.assertEqual(h.require_run(self.rid, tier='C')['id'], self.rid)

    def test_role_conflict_rejects_even_when_other_role_matches(self):
        role = h.require_run(self.rid)['roles'][0]['role']
        self.ledger_with_uuids({role: '22222222-dead-beef-0000-000000000000'})
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')

    def test_cancel_of_cost_only_run_does_not_need_a_live_anchor(self):
        # T17 class A must stay reachable even when every Worker is gone: the
        # Run dispatched nothing, so there is no executor to own. Requiring a
        # live UUID here reintroduced exactly the lock-up Issue #2 removes.
        data = self.ledger_with_uuids()
        data['dispatches'] = []
        data.pop('fault_groups', None)
        data.pop('recovery_authorizations', None)
        h.run_file(self.rid).write_text(json.dumps(data))
        for name in list(self.live):
            self.live[name] = None
        out = h.run_cancel(ns(run_id=self.rid, confirm_run_id=self.rid,
                              reason='Release a Run that never dispatched anything'))
        self.assertEqual(out['status'], 'cancelled')
        self.assertEqual(out['cancel_scope'], 'no_execution_history')

    def test_cancel_of_dispatched_run_still_requires_an_anchor(self):
        # Class B is the opposite obligation: the ledger describes real
        # executors, so ownership must be proven before touching it.
        data = self.ledger_with_uuids()
        # Any dispatch record is enough to make this class B: the ledger now
        # describes an executor that may exist elsewhere.
        data['dispatches'] = [{'id': 'cccccccccc', 'stage_index': 0, 'stage_attempt': 1,
                               'role': data['roles'][0]['role'], 'effect': 'write',
                               'remediation': False, 'status': 'settled', 'created_at': h.now()}]
        h.run_file(self.rid).write_text(json.dumps(data))
        for name in list(self.live):
            self.live[name] = None
        with self.assertRaisesRegex(h.Error, '无法证明|身份冲突'):
            h.run_cancel(ns(run_id=self.rid, confirm_run_id=self.rid,
                            reason='Attempt to cancel a foreign dispatched Run'))

    def test_tier_c_minus_rejects_contradictions_without_an_anchor(self):
        # C- exists so abandon/diagnose work when Workers died. It must still
        # refuse a ledger whose live agents contradict it.
        data = self.ledger_with_uuids()
        name = data['roles'][0]['agent_name']
        self.live[name] = dict(self.live[name], cwd='/some/other/checkout')
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C-')

    def test_tier_c_minus_allows_a_ledger_with_no_live_worker(self):
        # The opposite half: no survivor is not itself a contradiction.
        self.ledger_with_uuids()
        for name in list(self.live):
            self.live[name] = None
        self.assertEqual(h.require_run(self.rid, tier='C-')['id'], self.rid)

    def test_dispatch_survives_a_worker_that_exits_after_answering(self):
        # P0 regression, end to end. A one-shot Worker (`claude -p`, which is
        # what Happier starts) writes its report and then exits. `finalize`
        # re-reads the ledger under a lock to write that result back; with a
        # live-anchor requirement it refuses the Worker's OWN legitimate
        # report, and the dispatch is left in submission_uncertain forever --
        # uncollectable and uncancellable, i.e. the Issue #2 lock-up again.
        live = self.live
        # Align the live UUIDs with what the ledger recorded, so the only thing
        # under test is the Worker exiting between answer and write-back.
        for role in h.require_run(self.rid)['roles']:
            live[role['agent_name']] = dict(live[role['agent_name']],
                                            agent_session=role['agent_session'])
        orig = h.herdr

        def one_shot(*argv, **kw):
            if argv[:2] == ('agent', 'prompt'):
                pending = [d for d in h.require_run(self.rid)['dispatches']
                           if d['status'] not in ('settled',)]
                rec = pending[-1]
                path = h.result_path(self.rid, rec['id'])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    'run_id': self.rid, 'dispatch_id': rec['id'], 'role': rec['role'],
                    'stage': rec['stage'] if 'stage' in rec else
                             h.stage_info(h.require_run(self.rid))['stage'],
                    'stage_attempt': 1, 'status': 'complete',
                    'verdict': 'pass', 'summary': 'the one-shot worker finished its task',
                    'evidence': ['worker wrote this report before exiting'],
                    'tests': [{'command': 'true', 'exit_code': 0}]}))
                for name in list(live):        # the Worker has now exited
                    live[name] = None
                return {'agent': {'name': 'dev-w1p2', 'agent_status': 'idle'}}
            return orig(*argv, **kw)

        with patch.object(h, 'herdr', side_effect=one_shot):
            actor = h.stage_info(h.require_run(self.rid))['actor']
            out = h.run_dispatch(ns(run_id=self.rid, role=actor, message='do the task',
                                    effect='read', remediation=False, no_wait=False))
        self.assertEqual(out['status'], 'settled', out)
        rec = json.loads(h.run_file(self.rid).read_text())['dispatches'][-1]
        self.assertEqual(rec['status'], 'settled')
        self.assertIsNotNone(rec.get('report'))

    def test_gate_and_finish_do_not_demand_a_live_worker(self):
        # Same root cause as above, different victim: a Run whose Workers are
        # gone must still be closable. A gate writes ledger state only.
        self.ledger_with_uuids()
        for name in list(self.live):
            self.live[name] = None
        self.assertEqual(h.require_run(self.rid, tier='C-')['id'], self.rid)
        with self.assertRaises(h.Error):
            h.require_run(self.rid, tier='C')

    def test_ledger_without_any_worker_identity_is_rejected_at_every_tier(self):
        # "No identity recorded" is not the same as "the Workers died"; it
        # cannot be attributed to anyone, so C- must reject it too.
        data = self.ledger_with_uuids()
        for role in data['roles']:
            role.pop('agent_session', None)
        h.run_file(self.rid).write_text(json.dumps(data))
        for tier in ('C-', 'C'):
            with self.subTest(tier=tier):
                with self.assertRaisesRegex(h.Error, '未记录任何 Worker 身份'):
                    h.require_run(self.rid, tier=tier)

    def test_herdr_failure_is_reported_as_unobservable_not_absent(self):
        # `lookup_agent` used to swallow every Error, so a herdr timeout and a
        # genuinely absent agent both arrived as None. At tier C- (no anchor
        # required) that turned "could not look" into "no contradiction" and
        # waved the ledger through. The two must stay distinguishable.
        role = h.require_run(self.rid)['roles'][0]
        data = {'version': 3, 'roles': [role]}

        # The class setUp already patches lookup_agent, so drive the inner
        # herdr call the way a real failure would arrive.
        def boom(*a, **kw):
            raise h.HerdrError('herdr timed out', code='timeout')

        def gone(*a, **kw):
            raise h.HerdrError('not found', code='agent_not_found')

        def failing_lookup(name, session=None, on_error=None):
            try:
                raise boom()
            except h.Error as e:
                if on_error is not None:
                    on_error(e)
                return None

        def absent_lookup(name, session=None, on_error=None):
            try:
                raise gone()
            except h.HerdrError as e:
                if e.code == 'agent_not_found':
                    return None
                raise

        with patch.object(h, 'lookup_agent', side_effect=failing_lookup):
            _v, _t, conflicts, unavailable = h._inspect_roles(data, 'default')
        self.assertEqual(conflicts, [])
        self.assertTrue(unavailable, 'a herdr failure must be recorded as unobservable')

        with patch.object(h, 'lookup_agent', side_effect=absent_lookup):
            _v, _t, conflicts, unavailable = h._inspect_roles(data, 'default')
        self.assertEqual((conflicts, unavailable), ([], []))

    def test_presence_probe_carries_the_bound_session(self):
        # Regression: `role` has no session key -- the session lives on the Run.
        # Passing role.get('session') silently sent session=None, so a named
        # session's probe hit the default server and could report a live Worker
        # as confirmed_absent (the evidence the abandon fence trusts).
        role = h.require_run(self.rid)['roles'][0]
        calls = []

        def fake(*argv, **kw):
            calls.append((argv, kw.get('session')))
            if argv[:2] == ('agent', 'get'):
                return {'agent': {'name': role['agent_name'], 'pane_id': role['pane_id'],
                                  'agent': role['kind'], 'agent_status': 'idle',
                                  'cwd': role['cwd'],
                                  'agent_session': role['agent_session']}}
            return {'pane': {'pane_id': role['pane_id']}}

        with patch.object(h, 'herdr', side_effect=fake):
            h.agent_presence(role, session='alpha')
        self.assertTrue(calls)
        self.assertTrue(all(s == 'alpha' for _argv, s in calls),
                        f'session was dropped: {calls}')

    def test_cancel_cost_only_still_rejects_a_foreign_ledger(self):
        # Class A must not become a blanket bypass: a ledger that records a
        # different session is still refused by ledger_ownership.
        data = self.ledger_with_uuids()
        data['dispatches'] = []
        data['session'] = 'another-session'
        h.run_file(self.rid).write_text(json.dumps(data))
        with self.assertRaisesRegex(h.Error, '属于 session|跨 session'):
            h.run_cancel(ns(run_id=self.rid, confirm_run_id=self.rid,
                            reason='Attempt to cancel a foreign session ledger'))

    def test_cwd_mismatch_is_a_conflict_not_unobservable(self):
        # Regression: contradiction detection used to be a message-substring
        # whitelist, so a same-UUID agent living in a different checkout fell
        # through to "unobservable" and a foreign ledger passed ownership.
        data = self.ledger_with_uuids()
        name = data['roles'][0]['agent_name']
        self.live[name] = dict(self.live[name], cwd='/some/other/checkout')
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')

    def test_name_mismatch_is_a_conflict_not_unobservable(self):
        # Same hole, different message: an observed agent whose name changed is
        # a contradiction, never an observability gap.
        data = self.ledger_with_uuids()
        name = data['roles'][0]['agent_name']
        self.live[name] = dict(self.live[name], name='someone-else')
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.require_run(self.rid, tier='C')

    def test_pause_allowed_without_identity_but_records_unverified(self):
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '33333333-dead-beef-0000-000000000000' for r in data['roles']})
        out = h.run_pause(ns(run_id=self.rid, extend_rounds=0), True)
        self.assertEqual(out['status'], 'paused')
        events = json.loads(h.run_file(self.rid).read_text())['events']
        self.assertEqual(events[-1].get('identity'), 'unverified')

    def test_resume_requires_identity(self):
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '44444444-dead-beef-0000-000000000000' for r in data['roles']})
        h.run_pause(ns(run_id=self.rid, extend_rounds=0), True)
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.run_pause(ns(run_id=self.rid, extend_rounds=0), False)

    def test_finish_requires_identity(self):
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '55555555-dead-beef-0000-000000000000' for r in data['roles']})
        with self.assertRaisesRegex(h.Error, '身份冲突'):
            h.run_finish(ns(run_id=self.rid))

    def test_legacy_ledger_without_any_worker_identity_rejected(self):
        path = h.run_file(self.rid)
        data = json.loads(path.read_text())
        data.pop('session', None)
        for role in data['roles']:
            role.pop('agent_session', None)
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(h.Error, '未记录任何 Worker 身份'):
            h.require_run(self.rid, tier='C')

    def test_rejection_leaves_ledger_untouched(self):
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '66666666-dead-beef-0000-000000000000' for r in data['roles']})
        path = h.run_file(self.rid)
        before = path.read_text()
        with self.assertRaises(h.Error):
            h.require_run(self.rid, tier='C')
        self.assertEqual(path.read_text(), before)

    def test_pause_does_not_derive_other_actions(self):
        data = json.loads(h.run_file(self.rid).read_text())
        self.ledger_with_uuids({r['role']: '77777777-dead-beef-0000-000000000000' for r in data['roles']})
        before = len(self.actions)
        h.run_pause(ns(run_id=self.rid, extend_rounds=0), True)
        derived = [a for a in self.actions[before:] if a[:2] in (('agent','prompt'), ('agent','start'))]
        self.assertFalse(derived)

    def test_partial_roles_verified_allows_ops(self):
        # One Worker is gone (unobservable), the other still anchors ownership.
        roles = h.require_run(self.rid)['roles']
        gone = roles[0]['agent_name']
        del self.live[gone]
        self.ledger_with_uuids()
        self.assertEqual(h.require_run(self.rid, tier='C')['id'], self.rid)

    def test_all_roles_dead_blocks_side_effects(self):
        self.live.clear()
        self.ledger_with_uuids()
        with self.assertRaisesRegex(h.Error, '均无法在当前 session 内验证身份'):
            h.require_run(self.rid, tier='C')
        self.assertEqual(h.require_run(self.rid, tier='A')['id'], self.rid)

    def test_dispatch_verifies_target_worker_not_just_any_role(self):
        # One role keeps a matching UUID; the dispatch target does not. Ledger-level
        # anchoring would pass, so the per-target check has to reject on its own.
        roles = h.require_run(self.rid)['roles']
        target = roles[0]['role']
        self.ledger_with_uuids({target: '88888888-dead-beef-0000-000000000000'})
        with self.assertRaisesRegex(h.Error, 'identity changed'):
            h.run_dispatch(ns(run_id=self.rid, role=target, message='Never delivered',
                              effect='read', no_wait=False, remediation=False,
                              wait_timeout_seconds=None))


class LedgerProbeTests(unittest.TestCase):
    """The ownership probe itself, exercised without the boundary stub."""

    def test_session_list_failure_fails_closed(self):
        # A failing `herdr session list` must surface as an error, never as
        # "no session found so skip the check".
        def boom(*args, **kwargs):
            raise h.Error('session list unavailable')
        with patch.object(h, 'herdr', side_effect=boom):
            with self.assertRaisesRegex(h.Error, '无法定位 Herdr pane'):
                h.pane_of_current_process()

    def test_zero_running_sessions_fails_closed(self):
        with patch.object(h, 'herdr', return_value={'sessions': []}):
            with self.assertRaisesRegex(h.Error, '无法定位 Herdr pane'):
                h.pane_of_current_process()


if __name__=='__main__':unittest.main(verbosity=2)
