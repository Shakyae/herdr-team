"""Driver tests use isolated state and a fake Herdr only."""
import json
import types
import threading
import time
import unittest
import sys
from unittest.mock import patch

import test_htm
from test_htm import h, opts


REAL_HERDR = h.herdr
REVIEW_INDEPENDENCE = '上述内容是 Developer 的自我陈述，不是已核实的事实。你必须独立检查实际文件/改动/测试，不要仅凭摘要判定。若无法定位相关变更，明确写出该限制并给出 verdict。'


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class DriveTests(unittest.TestCase):
    def setUp(self):
        test_htm.RuntimeTests.setUp(self)
        fake = h.herdr.side_effect
        def with_session(*args, **kwargs):
            result = fake(*args, **kwargs)
            if args[:2] == ('agent', 'start'):
                self.started[args[2]]['agent_session'] = {'kind': 'id', 'source': 'mock', 'value': 'session-' + args[2]}
            return result
        p = patch.object(h, 'herdr', side_effect=with_session)
        p.start(); self.addCleanup(p.stop)
        self.fake = with_session

    def start(self, template='dev-lite'):
        team = h.init(self.cfg, opts(template, dry_run=False, cwd=self.tmp.name))['team']
        self.assertTrue(all('agent_session' in r for r in team['roles']))
        rid = h.run_start(self.cfg, ns(goal='Mock implementation'))['run_id']
        h.run_gate(ns(run_id=rid, decision='pass', evidence='Lead recorded a concrete mock plan'))
        return rid

    def drive(self, rid, **kw):
        return h.run_drive(ns(run_id=rid, resume=kw.get('resume', False),
                              max_steps=kw.get('max_steps', 20), deadline_seconds=kw.get('deadline_seconds', 1800),
                              wait_timeout_seconds=kw.get('wait_timeout_seconds')))

    def prompts(self):
        return [x for x in self.actions if x[:2] == ('agent', 'prompt')]

    def failing_reviews(self, count=1):
        remaining = [count]
        def fake(*args, **kwargs):
            result = self.fake(*args, **kwargs)
            if args[:2] == ('agent', 'prompt') and args[2].startswith('reviewer-') and remaining[0]:
                remaining[0] -= 1
                runid = next(p.stem for p in (h.STATE / 'runs').glob('*.json') if not p.name.endswith('.driver.json'))
                rec = h.require_run(runid)['dispatches'][-1]
                path = h.result_path(runid, rec['id'])
                report = json.loads(path.read_text())
                report['verdict'] = 'needs-work'
                path.write_text(json.dumps(report))
            return result
        return patch.object(h, 'herdr', side_effect=fake)

    def test_drive_on_adopted_four_key_session(self):
        available = {
            'dev': {'name': 'dev', 'agent': 'claude', 'agent_status': 'idle', 'pane_id': 'w1:p2',
                    'cwd': self.tmp.name, 'agent_session': {'agent': 'claude', 'kind': 'id',
                    'source': 'herdr:claude', 'value': 'session-dev-1234'}},
            'rev': {'name': 'rev', 'agent': 'claude', 'agent_status': 'idle', 'pane_id': 'w1:p3',
                    'cwd': self.tmp.name, 'agent_session': {'agent': 'claude', 'kind': 'id',
                    'source': 'herdr:claude', 'value': 'session-rev-1234'}}}
        names = {'developer': 'dev', 'reviewer': 'rev'}
        self.started.update(available)
        def fake(*args, **kwargs):
            if args[:2] == ('agent', 'get'):
                return {'agent': available[args[2]]}
            if args[:2] == ('pane', 'get'):
                return {'pane': {'workspace_id': 'w1', 'pane_id': args[2]}}
            if args[:2] == ('agent', 'prompt'):
                result = self.fake(*args, **kwargs)
                available[args[2]]['agent_status'] = 'done'
                return result
            return self.fake(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=fake), patch.object(h, 'lookup_agent', side_effect=lambda n: available[names.get(n, n)]):
            h.adopt(self.cfg, ns(template='dev-lite', role=['developer=dev', 'reviewer=rev'],
                                 set=[], cwd=self.tmp.name, dry_run=False))
            rid = h.run_start(self.cfg, ns(goal='Adopt four-key Herdr session'))['run_id']
            h.run_gate(ns(run_id=rid, decision='pass', evidence='Lead recorded a concrete mock plan'))
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'awaiting_lead', out)
        self.assertGreater(len(self.prompts()), 0)

    def test_identity_agent_missing(self):
        rid = self.start()
        run = h.require_run(rid)
        role = run['roles'][0]
        h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=False))
        before = len(self.prompts())
        original = h.lookup_agent
        def missing(name):
            if name == role['agent_name']:
                raise h.Error('{"error":{"code":"agent_not_found"}}')
            return original(name)
        with patch.object(h, 'lookup_agent', side_effect=missing):
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'identity')
        self.assertEqual(len(self.prompts()), before)

    def test_wait_timeout_cli_rejects_non_positive(self):
        with self.assertRaises(SystemExit) as ctx:
            with patch.object(sys, 'argv', ['htm.py', 'run', 'drive', '--run-id',
                                           'abcdef123456', '--wait-timeout-seconds', '0']):
                h.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_normal_pass_stops_for_lead(self):
        rid = self.start()
        with patch.object(h, 'run_finish') as finish:
            out = self.drive(rid)
        self.assertEqual((out['stopped_reason'], out['stage']), ('awaiting_lead', 'accept'))
        self.assertEqual([s['action'] for s in out['steps']], ['dispatch', 'gate'] * 3)
        finish.assert_not_called()

    def test_review_revision_uses_remediation(self):
        rid = self.start()
        with self.failing_reviews():
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'awaiting_lead')
        run = h.require_run(rid)
        review = [d for d in run['dispatches'] if d['stage_index'] == 3]
        self.assertEqual([d['role'] for d in review], ['reviewer', 'developer', 'reviewer'])
        self.assertTrue(review[1]['remediation'])
        self.assertEqual(review[1]['stage_attempt'], 2)

    def test_round_limit_no_extra_dispatch(self):
        rid = self.start()
        run = h.require_run(rid); run['settings']['orchestration']['max_rounds'] = 1; h.save(h.run_file(rid), run)
        with self.failing_reviews(): out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'round_limit')
        self.assertEqual(h.require_run(rid)['status'], 'blocked_round_limit')
        self.assertEqual(len(self.prompts()), 3)

    def test_unsettled_only_collect_no_resend(self):
        rid = self.start()
        d = h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
        h.result_path(rid, d['dispatch_id']).unlink()
        before = len(self.prompts())
        out = self.drive(rid)
        self.assertEqual(len(self.prompts()), before)
        self.assertEqual(out['stopped_reason'], 'stuck_unsettled')
        self.assertEqual([s['action'] for s in out['steps']], ['collect'])

    def test_identity_checked_before_dispatch(self):
        rid = self.start()
        role = h.require_run(rid)['roles'][0]
        self.started[role['agent_name']]['agent_session']['value'] = 'replacement-session'
        out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'identity')
        self.assertEqual(len(self.prompts()), 0)

    def test_cancelled_run_stops(self):
        rid = self.start()
        h.run_cancel(ns(run_id=rid, confirm_run_id=rid, reason='Cancel mock run explicitly'))
        self.assertEqual(self.drive(rid)['stopped_reason'], 'run_not_active')
        self.assertEqual(len(self.prompts()), 0)

    def test_driver_lock_nonblocking(self):
        rid = self.start()
        entered, release = threading.Event(), threading.Event()
        results = []
        def blocking(*args, **kwargs):
            if args[:2] == ('agent', 'prompt') and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out')
            return self.fake(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=blocking):
            worker = threading.Thread(target=lambda: results.append(self.drive(rid)))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                before = len(self.prompts())
                out = self.drive(rid)
                self.assertEqual(out['stopped_reason'], 'driver_running')
                self.assertIn('Driver already running for this Run', out['detail'])
                self.assertEqual(len(self.prompts()), before)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['stopped_reason'], 'awaiting_lead')

    def test_crash_recovery_ledger_overrides_progress(self):
        rid = self.start()
        d = h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
        run = h.require_run(rid); run['events'].append({'type': 'driver_started', 'at': h.now()}); h.save(h.run_file(rid), run)
        stale = dict(run, dispatches=[])
        h.save(h.STATE / 'runs' / (rid + '.driver.json'), stale)
        self.assertEqual(self.drive(rid)['stopped_reason'], 'resume_required')
        before = len(self.prompts())
        artifact = h.result_path(rid, d['dispatch_id'])
        saved = artifact.read_bytes()
        artifact.unlink()
        first = self.drive(rid, resume=True)
        self.assertEqual(first['stopped_reason'], 'stuck_unsettled')
        self.assertEqual(len(self.prompts()), before)
        artifact.write_bytes(saved)
        out = self.drive(rid, resume=True, max_steps=2)
        self.assertTrue(out['ledger_overrides_progress'])
        self.assertEqual([s['action'] for s in out['steps']], ['collect', 'gate'])
        self.assertEqual(out['steps'][0]['dispatch_id'], d['dispatch_id'])
        self.assertEqual(len(self.prompts()), before)

    def test_init_missing_session_remains_legacy_compatible(self):
        with patch.object(h, 'lookup_agent', return_value=None):
            team = h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['team']
        self.assertTrue(all('agent_session' not in r for r in team['roles']))
        self.assertTrue(h.init(self.cfg, opts('dev-lite', dry_run=False, cwd=self.tmp.name))['reused'])
        rid = h.run_start(self.cfg, ns(goal='Legacy compatibility'))['run_id']
        self.assertEqual(h.run_list()['runs'][0]['run_id'], rid)
        self.assertEqual(h.run_recover(ns(run_id=rid))['run_id'], rid)

    def test_unbound_refuses_drive_manual_dispatch_works(self):
        rid = self.start()
        run = h.require_run(rid); del run['roles'][0]['agent_session']; h.save(h.run_file(rid), run)
        self.assertEqual(self.drive(rid)['stopped_reason'], 'identity_unbound')
        self.assertFalse(self.prompts())
        result = h.run_dispatch(ns(run_id=rid, role='developer', message='manual task', effect='write', remediation=False, no_wait=False))
        self.assertEqual(result['status'], 'settled')

    def test_limits_allow_resume(self):
        rid = self.start()
        self.assertEqual(self.drive(rid, deadline_seconds=0)['stopped_reason'], 'deadline')
        out = self.drive(rid, resume=True, max_steps=1)
        self.assertEqual(out['stopped_reason'], 'max_steps')
        self.assertEqual(self.drive(rid, resume=True)['stopped_reason'], 'awaiting_lead')

    def test_reviewer_prompt_includes_developer_artifact(self):
        rid = self.start()
        self.drive(rid, max_steps=4)
        run = h.require_run(rid)
        artifact = run['dispatches'][-1]
        self.assertEqual(run['stages'][artifact['stage_index']], 'self_test')
        report = artifact['report']
        report['summary'] = 'S' * 500 + 'SUMMARY_MUST_BE_TRUNCATED'
        report['evidence'] = [f'Concrete evidence item {i}' for i in range(11)]
        artifact['artifact_sha256'] = 'test-digest-bound-to-self-test'
        h.save(h.run_file(rid), run)
        self.drive(rid, max_steps=1)
        prompt = self.prompts()[-1][3]
        self.assertIn('artifact_file', prompt)
        self.assertIn('artifact_sha256', prompt)
        context = json.loads(prompt.split('Worker artifact (self-report):\n')[1].split('\n')[0])
        self.assertEqual(context, {'role': artifact['role'], 'stage': run['stages'][artifact['stage_index']],
                                  'stage_attempt': artifact['stage_attempt'], 'dispatch_id': artifact['id'],
                                  'artifact_file': str(h.result_path(rid, artifact['id']).resolve()),
                                  'artifact_sha256': artifact['artifact_sha256'],
                                  'summary': 'S' * 500, 'evidence': report['evidence'][:10]})
        self.assertIn(REVIEW_INDEPENDENCE, prompt)
        for developer in self.prompts()[:-1]:
            self.assertNotIn('Worker artifact (self-report)', developer[3])
            self.assertNotIn(REVIEW_INDEPENDENCE, developer[3])

        # A separate isolated run with plan_review before any implementation.
        self.setUp()
        rid = self.start('dev-review')
        self.drive(rid, max_steps=1)
        prompt = self.prompts()[-1][3]
        run = h.require_run(rid)
        self.assertIn('这是计划评审，无实现产物', prompt)
        self.assertIn(run['goal'], prompt)
        self.assertIn('stage_definitions', prompt)
        self.assertIn('settings', prompt)
        self.assertIn(REVIEW_INDEPENDENCE, prompt)
        self.assertNotIn('artifact_file', prompt)
        self.assertNotIn('artifact_sha256', prompt)

    def test_reviewer_revision_includes_current_fix_artifact(self):
        rid = self.start()
        with self.failing_reviews():
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'awaiting_lead')
        run = h.require_run(rid)
        fix, review = run['dispatches'][-2:]
        self.assertTrue(fix['remediation'])
        self.assertIn(fix['result_file'], review['prompt'])
        self.assertIn(fix['artifact_sha256'], review['prompt'])
        self.assertIn(REVIEW_INDEPENDENCE, review['prompt'])

    def test_driver_deadline_is_hard(self):
        rid = self.start()
        def slow_prompt(*args, **kwargs):
            if args[:2] == ('agent', 'prompt'):
                time.sleep(0.02)
            return self.fake(*args, **kwargs)
        with patch.object(h, 'herdr', side_effect=slow_prompt):
            out = self.drive(rid, deadline_seconds=0)
        self.assertEqual(out['stopped_reason'], 'deadline')
        self.assertTrue(out['deadline_exceeded'])
        self.assertFalse(self.prompts())

    def test_driver_deadline_bounds_wait_and_subprocess(self):
        rid = self.start()
        dispatch = h.run_dispatch
        before = time.monotonic()
        with patch.object(h, 'run_dispatch', wraps=dispatch) as wrapped, \
                patch.object(h, 'herdr', side_effect=self.fake) as herdr:
            out = self.drive(rid, deadline_seconds=6, max_steps=1)
        budget = wrapped.call_args.args[0].wait_timeout_seconds
        self.assertGreaterEqual(budget, 1)
        self.assertLessEqual(budget, 6 - (time.monotonic() - before))
        prompt_call = next(c for c in herdr.call_args_list if c.args[:2] == ('agent', 'prompt'))
        self.assertEqual(int(prompt_call.args[prompt_call.args.index('--timeout') + 1]), budget * 1000)
        self.assertEqual(prompt_call.kwargs['timeout'], budget)
        self.assertFalse(out['deadline_exceeded'])

    def test_driver_deadline_no_second_dispatch_after_overrun(self):
        rid = self.start()
        clock = [100.0]
        def slow_prompt(*args, **kwargs):
            result = self.fake(*args, **kwargs)
            if args[:2] == ('agent', 'prompt'):
                time.sleep(0.01)
                clock[0] += 7  # Model a non-cooperative subprocess mock exceeding its budget.
            return result
        with patch.object(h.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(h, 'herdr', side_effect=slow_prompt):
            out = self.drive(rid, deadline_seconds=6)
        self.assertEqual(out['stopped_reason'], 'deadline')
        self.assertTrue(out['deadline_exceeded'])
        self.assertEqual(len(self.prompts()), 1)

    def test_driver_caps_wait_by_cli_flag(self):
        rid = self.start()
        dispatch = h.run_dispatch
        with patch.object(h, 'run_dispatch', wraps=dispatch) as wrapped:
            self.drive(rid, max_steps=1, wait_timeout_seconds=3)
        self.assertEqual(wrapped.call_args.args[0].wait_timeout_seconds, 3)

    def test_driver_deadline_rechecked_before_dispatch(self):
        rid = self.start()
        clock = [100.0]
        verify = h.verify_existing
        def delayed_identity(role):
            valid = verify(role)
            clock[0] += 2
            return valid
        with patch.object(h.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(h, 'verify_existing', side_effect=delayed_identity):
            out = self.drive(rid, deadline_seconds=6)
        self.assertEqual(out['stopped_reason'], 'deadline')
        self.assertFalse(out['deadline_exceeded'])
        self.assertEqual(out['detail'], 'remaining budget <= 5s; not dispatching')
        self.assertFalse(self.prompts())

    def test_manual_dispatch_retains_default_wait(self):
        rid = self.start()
        timeout = h.require_run(rid)['settings']['orchestration']['task_timeout_seconds']
        with patch.object(h, 'herdr', side_effect=self.fake) as herdr:
            h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write',
                              remediation=False, no_wait=False, wait_timeout_seconds=None))
        prompt = next(c for c in herdr.call_args_list if c.args[:2] == ('agent', 'prompt'))
        self.assertEqual(prompt.kwargs['timeout'], timeout + 15)
        self.assertEqual(prompt.args[prompt.args.index('--timeout') + 1], str(timeout * 1000))

    def test_collect_error_not_misreported_as_identity(self):
        rid = self.start()
        h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
        before = len(self.prompts())
        with patch.object(h, 'run_collect', side_effect=h.Error('disk persistence failed while collecting')):
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'collect_error')
        self.assertEqual(out['detail'], 'disk persistence failed while collecting')
        self.assertEqual(len(self.prompts()), before)

    def test_collect_identity_precheck_names_agent(self):
        rid = self.start()
        h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
        role = h.require_run(rid)['roles'][0]
        self.started[role['agent_name']]['agent_session']['value'] = 'replacement-session'
        with patch.object(h, 'run_collect') as collect:
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'identity')
        self.assertIn(role['agent_name'], out['detail'])
        self.assertIn('session identity changed', out['detail'])
        collect.assert_not_called()
        self.assertEqual(len(self.prompts()), 1)

    def test_historical_blocked_does_not_stop_current_stage(self):
        for earlier_attempt in (False, True):
            with self.subTest(earlier_attempt=earlier_attempt):
                self.setUp()
                rid = self.start()
                run = h.require_run(rid)
                historical = {'id': 'old-blocked', 'stage_index': run['stage_index'] if earlier_attempt else 0,
                              'stage_attempt': 1, 'role': 'developer', 'status': 'blocked'}
                run['dispatches'].append(historical)
                if earlier_attempt:
                    run['stage_attempt'] = 2
                h.save(h.run_file(rid), run)
                out = self.drive(rid)
                self.assertEqual(out['stopped_reason'], 'awaiting_lead')
                self.assertEqual(out['historical_blocked'], ['old-blocked'])
                self.assertEqual(len(self.prompts()), 3)
                self.assertEqual(h.require_run(rid)['dispatches'][0]['status'], 'blocked')

        self.setUp()
        rid = self.start()
        run = h.require_run(rid)
        run['dispatches'].append({'id': 'current-blocked', 'stage_index': run['stage_index'],
                                  'stage_attempt': run['stage_attempt'], 'role': 'developer', 'status': 'blocked'})
        h.save(h.run_file(rid), run)
        out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'worker_blocked')
        self.assertEqual(out['historical_blocked'], [])
        self.assertFalse(self.prompts())

    def test_uncertain_states_all_only_collect(self):
        for status in ('submission_uncertain', 'submitted_unsettled', 'uncertain'):
            with self.subTest(status=status):
                self.setUp()
                rid = self.start()
                d = h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
                run = h.require_run(rid)
                run['dispatches'][-1]['status'] = status
                h.save(h.run_file(rid), run)
                h.result_path(rid, d['dispatch_id']).unlink()
                before = len(self.prompts())
                with patch.object(h, 'run_dispatch', wraps=h.run_dispatch) as dispatch, \
                        patch.object(h, 'run_collect', wraps=h.run_collect) as collect:
                    out = self.drive(rid)
                self.assertEqual(out['stopped_reason'], 'stuck_unsettled')
                self.assertEqual([s['action'] for s in out['steps']], ['collect'])
                collect.assert_called_once()
                dispatch.assert_not_called()
                self.assertEqual(len(self.prompts()), before)

    def test_settled_but_progress_missing_no_resend(self):
        rid = self.start()
        d = h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=False))
        self.assertEqual(d['status'], 'settled')
        self.assertFalse((h.STATE / 'runs' / (rid + '.driver.json')).exists())
        before = len(self.prompts())
        with patch.object(h, 'run_dispatch', wraps=h.run_dispatch) as dispatch:
            out = self.drive(rid, max_steps=1)
        self.assertEqual([s['action'] for s in out['steps']], ['gate'])
        self.assertEqual(out['steps'][0]['dispatch_id'], d['dispatch_id'])
        dispatch.assert_not_called()
        self.assertEqual(len(self.prompts()), before)

    def test_pause_and_cancel_safe(self):
        for status in ('paused', 'cancelled'):
            with self.subTest(status=status):
                self.setUp()
                rid = self.start()
                if status == 'paused':
                    h.run_pause(ns(run_id=rid), True)
                else:
                    h.run_cancel(ns(run_id=rid, confirm_run_id=rid, reason='Cancel mock run explicitly'))
                self.assertEqual(self.drive(rid)['stopped_reason'], 'run_not_active')
                self.assertFalse(self.prompts())

    def test_drive_never_commits(self):
        rid = self.start()
        def fake_subprocess(argv, **kwargs):
            self.assertEqual(argv[0], 'herdr')
            return ns(returncode=0, stdout=json.dumps(self.fake(*argv[1:])), stderr='')
        # Exercise the real herdr -> process -> subprocess.run path with only the OS boundary faked.
        with patch.object(h, 'herdr', new=REAL_HERDR), \
                patch.object(h, 'process', wraps=h.process) as process, \
                patch.object(h.subprocess, 'run', side_effect=fake_subprocess) as subprocess_run:
            out = self.drive(rid)
        self.assertEqual(out['stopped_reason'], 'awaiting_lead')
        self.assertGreater(process.call_count, 0)
        self.assertGreater(subprocess_run.call_count, 0)
        for call in process.call_args_list + subprocess_run.call_args_list:
            self.assertNotEqual(call.args[0][0].rsplit('/', 1)[-1], 'git')

    def test_identity_moved_and_occupied(self):
        for pending in (False, True):
            for change in ('moved', 'occupied'):
                with self.subTest(pending=pending, change=change):
                    self.setUp()
                    rid = self.start()
                    if pending:
                        h.run_dispatch(ns(run_id=rid, role='developer', message='mock', effect='write', remediation=False, no_wait=True))
                    role = h.require_run(rid)['roles'][0]
                    live = self.started[role['agent_name']]
                    if change == 'moved':
                        live['pane_id'] = 'w3:p9'
                    else:
                        live['agent_session'] = {'kind': 'id', 'source': 'mock', 'value': 'different-occupant'}
                    before = len(self.prompts())
                    with patch.object(h, 'run_collect') as collect:
                        out = self.drive(rid)
                    self.assertEqual(out['stopped_reason'], 'identity')
                    self.assertIn(role['agent_name'], out['detail'])
                    self.assertEqual(len(self.prompts()), before)
                    collect.assert_not_called()

    def test_driver_stopped_write_failure_then_resume(self):
        rid = self.start()
        save = h.save
        def stopped_write_failure(path, data):
            if path == h.run_file(rid) and data['events'][-1].get('type') == 'driver_stopped':
                raise OSError('mock driver_stopped write failure')
            return save(path, data)
        with patch.object(h, 'save', side_effect=stopped_write_failure):
            out = self.drive(rid, max_steps=1)
        self.assertEqual(out['stopped_reason'], 'error')
        self.assertIn('mock driver_stopped write failure', out['detail'])
        self.assertEqual(h.require_run(rid)['events'][-1]['type'], 'driver_started')
        before = len(self.prompts())
        self.assertEqual(self.drive(rid)['stopped_reason'], 'resume_required')
        self.assertEqual(len(self.prompts()), before)
        out = self.drive(rid, resume=True, max_steps=1)
        self.assertEqual([s['action'] for s in out['steps']], ['gate'])
        self.assertEqual(len(self.prompts()), before)
        self.assertEqual(self.drive(rid, resume=True)['stopped_reason'], 'awaiting_lead')

    def test_dispatch_policy_errors_are_distinct(self):
        for error in ('autonomy=read-only prohibits write', 'Approval required: owner reference',
                      'sensitive actions require autonomy=approved-actions', 'other dispatch failure'):
            with self.subTest(error=error):
                self.setUp()
                rid = self.start()
                with patch.object(h, 'run_dispatch', side_effect=h.Error(error)):
                    out = self.drive(rid)
                self.assertEqual(out['stopped_reason'], 'dispatch_error' if error.startswith('other') else 'policy')
                self.assertEqual(out['detail'], error)
                self.assertFalse(self.prompts())

    def test_drive_preserves_actual_policy_checks(self):
        for setting in ('read-only', 'every-step'):
            with self.subTest(setting=setting):
                self.setUp()
                rid = self.start()
                run = h.require_run(rid)
                governance = run['settings']['governance']
                governance['autonomy' if setting == 'read-only' else 'approval'] = setting
                h.save(h.run_file(rid), run)
                with patch.object(h, 'run_approve') as approve:
                    out = self.drive(rid)
                self.assertEqual(out['stopped_reason'], 'policy')
                self.assertFalse(self.prompts())
                approve.assert_not_called()
