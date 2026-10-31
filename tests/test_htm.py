import copy
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'htm.py'
spec = importlib.util.spec_from_file_location('htm', MODULE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def opts(template, **kw):
    data = {'template': template, 'set': [], 'layout': None, 'arrange': None, 'worktree': 'off',
            'cwd': None, 'repo': None, 'task': None, 'dry_run': True}
    data.update(kw)
    return types.SimpleNamespace(**data)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = h.read_cfg(h.DEFAULT_CONFIG)

    def test_seven_templates(self):
        self.assertEqual(set(self.cfg['templates']),
            {'dev-lite','dev-review','dev-test','full','research','audit','hermes-guide'})
        for key in list(self.cfg['templates'])[:6]:
            for reg in h.template(self.cfg, key)['roles'].values():
                self.assertEqual(self.cfg['agents'][reg]['kind'], 'claude')
        roles = h.template(self.cfg, 'hermes-guide')['roles']
        self.assertEqual(self.cfg['agents'][roles['operator']]['profile'], 'work')
        self.assertEqual(self.cfg['agents'][roles['analyst']]['kind'], 'claude')

    def test_full_inherits_review_and_tester(self):
        t = h.template(self.cfg, 'full')
        self.assertEqual(set(t['roles']), {'developer', 'reviewer', 'tester'})
        self.assertEqual(t['workflow'].count('plan_review'), 1)
        self.assertEqual(t['workflow'].count('code_review'), 1)

    def test_template_override(self):
        self.cfg['templates']['research']['governance']['report'] = 'milestones'
        _, settings = h.resolve(self.cfg,'research', ['orchestration.max_rounds=3'])
        self.assertEqual(settings['governance']['autonomy'], 'read-only')
        self.assertEqual(settings['governance']['report'], 'milestones')
        self.assertEqual(settings['orchestration']['max_rounds'], 3)
        self.assertEqual(self.cfg['defaults']['orchestration']['max_rounds'], 5)

    def test_unknown_and_invalid_type(self):
        for value in (None, [], 'sideways'):
            bad = copy.deepcopy(self.cfg)
            bad['defaults']['layout'] = value
            with self.assertRaises(h.Error): h.validate(bad)
        bad = copy.deepcopy(self.cfg)
        bad['templates']['audit']['workflow'] = ['security_audit', 'nonsense']
        with self.assertRaises(h.Error): h.validate(bad)
        bad = copy.deepcopy(self.cfg)
        bad['agents']['hermes-work']['profile'] = None
        with self.assertRaises(h.Error): h.validate(bad)

    def test_custom_template(self):
        new = copy.deepcopy(self.cfg)
        new['templates']['new-review']={'extends':'dev-review','description':'New',
                                       'governance': {'report': 'milestones'}}
        h.validate(new)
        self.assertIn('developer', h.template(new,'new-review')['roles'])
        self.assertEqual(h.resolve(new,'new-review')[1]['governance']['report'], 'milestones')

    def test_cycle_detect(self):
        bad = copy.deepcopy(self.cfg)
        bad['templates']['dev-lite']['extends'] = 'full'
        bad['templates']['dev-review']['extends'] = 'dev-lite'
        with self.assertRaises(h.Error): h.validate(bad)

    def test_worktree_git_integration(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)/'repo'; repo.mkdir()
            subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
            (repo/'hello.txt').write_text('hello')
            subprocess.run(['git','-C',str(repo),'add','hello.txt'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.email=test@example.test',
                            '-c','user.name=Test','commit','-m','initial'],check=True,capture_output=True)
            tmp=Path(d)/'managed'
            plan=h.initial_plan(self.cfg,'dev-review',opts('dev-review',worktree='on'))
            with patch.dict(os.environ, {'HTM_WORKTREE_DIR':str(tmp)}):
                wt=h.worktree_details(plan,opts('dev-review',worktree='on',repo=str(repo),task='safe-test'),False)
                self.assertFalse(Path(wt['path']).exists())
                made=h.worktree_details(plan,opts('dev-review',worktree='on',repo=str(repo),task='safe-test'),True)
                self.assertEqual((Path(made['path'])/'hello.txt').read_text(),'hello')
                with self.assertRaises(h.Error):
                    h.worktree_details(plan,opts('dev-review',worktree='on',repo=str(repo),task='safe-test'),True)

    def test_layout_ops(self):
        self.assertEqual(h.layout_ops(2,'rows'), [(0,'down',1)])
        self.assertEqual(h.layout_ops(3,'grid'), [(0,'down',1),(1,'right',2)])
        self.assertEqual(h.layout_ops(4,'grid'), [(0,'down',2),(0,'right',1),(2,'right',3)])
        for n in (1,2,3,4,5,6,7,8):
            for mode in ('auto','cols','rows','grid'):
                ops = h.layout_ops(n,mode)
                self.assertEqual(len(ops),n-1)
                self.assertEqual(set(x[2] for x in ops),set(range(1,n)))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = h.STATE
        h.STATE = Path(self.tmp.name)/'state'
        self.addCleanup(setattr,h,'STATE',self.old_state)
        env = patch.dict(os.environ, {'HERDR_ENV':'1','HERDR_WORKSPACE_ID':'w1','HERDR_PANE_ID':'w1:p1',
                                   'HERDR_TAB_ID':'w1:t1'})
        env.start(); self.addCleanup(env.stop)
        self.cfg,_ = h.read_cfg(h.DEFAULT_CONFIG)
        self.started = {}
        self.panes = 1
        self.actions = []

        def fake_herdr(*argv, **kwargs):
            self.actions.append(argv)
            if argv[:2] == ('pane','get'):
                return {'pane': {'workspace_id': 'w1'}}
            if argv[:2] == ('pane','process-info'):
                return {'process_info': {'shell_pid': 123, 'foreground_process_group_id': 123, 'foreground_processes': [
                    {'pid': 123, 'argv0': 'zsh'}]}}
            if argv[:2] == ('pane','split'):
                self.panes += 1
                return {'pane': {'pane_id': f'w1:p{self.panes}'}}
            if argv[:2] == ('tab','create'):
                self.panes += 1
                return {'tab': {'tab_id': 'w1:t2'}, 'root_pane': {'pane_id': f'w1:p{self.panes}'}}
            if argv[:2] == ('agent','get'):
                if argv[2] not in self.started:
                    raise h.HerdrError('not found', code='agent_not_found')
                return {'agent': self.started[argv[2]]}
            if argv[:2] == ('agent','start'):
                self.started[argv[2]] = {'pane_id': argv[argv.index('--pane')+1],
                                          'kind': argv[argv.index('--kind')+1], 'status':'idle'}
                return {'agent': self.started[argv[2]]}
            if argv[:2] == ('agent','prompt'):
                prompt = argv[3]
                match = re.search(r'task ([a-f0-9]{10}) / run ([a-f0-9]{12}) / stage ([a-z_]+)', prompt)
                stamp, rid, stage = match.groups()
                run = h.require_run(rid)
                rec = next(x for x in run['dispatches'] if x['id'] == stamp)
                path = h.result_path(rid,stamp)
                doc = {'run_id':rid, 'dispatch_id':stamp, 'role':rec['role'], 'stage':stage,
                       'stage_attempt':rec['stage_attempt'], 'status':'complete','verdict':'pass',
                       'summary':'Mock worker delivered an evidence-bound artifact for testing.',
                       'evidence':['Mock test evidence, not a real verification'],
                       'tests':[{'command':'mock tests','exit_code':0}]}
                path.write_text(json.dumps(doc))
                self.started[argv[2]]['status'] = 'done'
                return {'agent': self.started[argv[2]]}
            raise AssertionError(f'unexpected Herdr request: {argv}')
        pm = patch.object(h,'herdr',side_effect=fake_herdr); pm.start(); self.addCleanup(pm.stop)
        lookup = patch.object(h,'lookup_agent', side_effect=lambda n: self.started.get(n))
        lookup.start(); self.addCleanup(lookup.stop)
        reading = patch.object(h,'herdr_read',return_value='Done: inspected files; tests all pass (mock evidence).')
        reading.start(); self.addCleanup(reading.stop)
        profile = patch.object(h,'profile_check',return_value='profile work ok')
        profile.start(); self.addCleanup(profile.stop)

    def test_init_reuses_without_splitting_again(self):
        a = opts('dev-review',dry_run=False,cwd=self.tmp.name)
        first = h.init(self.cfg,a)
        self.assertFalse(first['reused'])
        self.assertEqual(len(first['team']['roles']),2)
        count = len(self.actions)
        second = h.init(self.cfg,a)
        self.assertTrue(second['reused'])
        self.assertEqual(len(self.actions),count)
        self.assertEqual(len([x for x in self.actions if x[0:2] == ('pane','split')]),2)

    def test_dryrun_does_not_invoke_herdr(self):
        before = len(self.actions)
        out = h.init(self.cfg, opts('full',dry_run=True,cwd=self.tmp.name,layout='tab'))
        self.assertTrue(out['dry_run'])
        self.assertEqual(len(self.actions),before)

    def test_hermes_explicit_profile(self):
        team = h.init(self.cfg,opts('hermes-guide',dry_run=False,cwd=self.tmp.name))['team']
        self.assertEqual(team['roles'][0]['launch_args'],['-p','work','chat'])
        self.assertTrue(any(x[:2]==('agent','start') and '-p' in x and 'work' in x for x in self.actions))

    def test_run_dispatch_collect_gate_and_finish(self):
        h.init(self.cfg,opts('dev-lite',dry_run=False,cwd=self.tmp.name))
        started = h.run_start(self.cfg,types.SimpleNamespace(goal='Implement safe parser'))
        rid = started['run_id']
        def gate_pass(message):
            return h.run_gate(types.SimpleNamespace(run_id=rid,decision='pass',evidence=message))
        gate_pass('Lead drafted brief plan, acceptance criterion A')
        for _ in range(2):
            stage = h.stage_info(h.require_run(rid))
            self.assertEqual(stage['actor'],'developer')
            out = h.run_dispatch(types.SimpleNamespace(run_id=rid,role='developer',message='Test safely',effect='write',remediation=False,no_wait=False))
            self.assertEqual(out['status'],'settled')
            gate_pass('Lead verified collected worker terminal output details')
        self.assertEqual(h.stage_info(h.require_run(rid))['actor'],'reviewer')
        h.run_dispatch(types.SimpleNamespace(run_id=rid,role='reviewer',message='Review code',effect='read',remediation=False,no_wait=False))
        gate_pass('Lead verified independent review and identified no critical issue')
        gate_pass('Lead checked acceptance tests and safe scope')
        self.assertTrue(h.run_finish(types.SimpleNamespace(run_id=rid))['finished'])

    def test_cannot_skip_worker_gate(self):
        h.init(self.cfg,opts('dev-review',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Work'))['run_id']
        h.run_gate(types.SimpleNamespace(run_id=rid, decision='pass',evidence='Lead created a detailed plan'))
        with self.assertRaises(h.Error):
            h.run_gate(types.SimpleNamespace(run_id=rid,decision='pass',evidence='I claim plan review passed'))

    def test_readonly_blocks_write(self):
        h.init(self.cfg,opts('research',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Research'))['run_id']
        with self.assertRaisesRegex(h.Error,'read-only'):
            h.run_dispatch(types.SimpleNamespace(run_id=rid,role='researcher',message='modify code',effect='write',remediation=False,no_wait=False))
        self.assertFalse(any(x[:2]==('agent','prompt') for x in self.actions))

    def test_uncertain_dispatch_blocks_duplicate(self):
        h.init(self.cfg,opts('research',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Research'))['run_id']
        def timed_out(*a,**kw): raise h.Error('timeout')
        with patch.object(h,'herdr',side_effect=timed_out):
            with self.assertRaisesRegex(h.Error,'uncertain'):
                h.run_dispatch(types.SimpleNamespace(run_id=rid,role='researcher',message='Find sources',effect='read',remediation=False,no_wait=False))
        with self.assertRaisesRegex(h.Error,'Pending/uncertain'):
            h.run_dispatch(types.SimpleNamespace(run_id=rid,role='researcher',message='Find sources again',effect='read',remediation=False,no_wait=False))

    def test_review_rework_another_worker_and_new_evidence(self):
        h.init(self.cfg,opts('dev-review',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Develop'))['run_id']
        def gate(decision):
            return h.run_gate(types.SimpleNamespace(run_id=rid, decision=decision, evidence='Verified test result and review evidence text'))
        gate('pass')
        h.run_dispatch(types.SimpleNamespace(run_id=rid,role='reviewer',message='Review plan',effect='read',remediation=False,no_wait=False))
        gate('revise')
        self.assertEqual(h.stage_info(h.require_run(rid))['stage_attempt'],2)
        with self.assertRaises(h.Error): gate('pass')
        h.run_dispatch(types.SimpleNamespace(run_id=rid,role='developer',message='Propose fixes to plan',effect='read',remediation=True,no_wait=False))
        h.run_dispatch(types.SimpleNamespace(run_id=rid,role='reviewer',message='Review updated plan',effect='read',remediation=False,no_wait=False))
        gate('pass')

    def test_parallel_no_wait_and_collect(self):
        h.init(self.cfg,opts('research',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Research'))['run_id']
        one = h.run_dispatch(types.SimpleNamespace(run_id=rid,role='researcher',message='Find sources',effect='read',remediation=False,no_wait=True))
        self.assertEqual(one['status'],'submitted_unsettled')
        with self.assertRaises(h.Error):
            h.run_dispatch(types.SimpleNamespace(run_id=rid,role='researcher',message='Do again',effect='read',remediation=False,no_wait=True))
        collected = h.run_collect(types.SimpleNamespace(run_id=rid,dispatch_id=one['dispatch_id']))
        self.assertEqual(collected['status'],'settled')

    def test_gate_and_approve_cannot_be_faked_noninteractive(self):
        h.init(self.cfg,opts('dev-review',dry_run=False,cwd=self.tmp.name))
        rid = h.run_start(self.cfg,types.SimpleNamespace(goal='Develop'))['run_id']
        with self.assertRaisesRegex(h.Error, 'disabled'):
            h.run_approve(types.SimpleNamespace(run_id=rid,role='developer',effect='write',reason='Owner said yes in chat'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
