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
                return {'pane': {'workspace_id': h.context()['workspace']}}
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
        def poll(pane_id):
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


if __name__=='__main__':unittest.main(verbosity=2)
