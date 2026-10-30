"""Independent adverse-case regression tests for v4.2.

All Herdr interactions are mocked. No real agents or user repositories touched.
"""
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

import test_htm
from test_htm import h, opts


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


def start_in_child(q, barrier):
    """Fork inherits an isolated patched Fake Herdr and a shared state path."""
    try:
        barrier.wait(timeout=5)
        q.put(('ok', h.run_start(None, ns(goal='Concurrent fake process'))['run_id']))
    except Exception as e:
        q.put(('blocked', str(e)))


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
        with patch.dict(os.environ, {'HERDR_PANE_ID':'w1:other-lead'}):
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


if __name__=='__main__':unittest.main(verbosity=2)
