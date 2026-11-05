"""Issue #2 regression tests: presence, diagnosis, controlled abandon, guards.

Everything runs against an isolated temporary STATE with a mocked Herdr. No
real Team, ledger or Herdr session is touched.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

import importlib.util

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'htm.py'
spec = importlib.util.spec_from_file_location('htm', MODULE)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


@pytest.fixture(autouse=True)
def isolate_git_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith('GIT_'):
            monkeypatch.delenv(key)


def ns(**kw):
    return types.SimpleNamespace(**kw)


RID = 'aaaaaaaaaaaa'
DID = 'bbbbbbbbbb'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = h.STATE
        h.STATE = Path(self.tmp.name) / 'state'
        self.addCleanup(setattr, h, 'STATE', self.old_state)
        env = patch.dict(os.environ, {'HERDR_ENV': '1', 'HERDR_WORKSPACE_ID': 'w1',
                                      'HERDR_PANE_ID': 'w1:p1', 'HERDR_TAB_ID': 'w1:t1',
                                      'HERDR_SESSION': '', 'HERDR_SOCKET_PATH': ''})
        env.start(); self.addCleanup(env.stop)
        # The native branch cross-checks HERDR_* against the real process
        # ancestry probe. These tests are about Issue #2 recovery, not identity
        # discovery, so fake the probe at that boundary and keep every other
        # validation live.
        def identity(*args, **kwargs):
            return {'session': os.environ.get('HERDR_SESSION') or 'default',
                    'workspace': os.environ.get('HERDR_WORKSPACE_ID', ''),
                    'tab': os.environ.get('HERDR_TAB_ID', ''),
                    'pane': os.environ.get('HERDR_PANE_ID', '')}
        probe = patch.object(h, 'pane_of_current_process', side_effect=identity)
        probe.start(); self.addCleanup(probe.stop)
        self.agent = {'name': 'dev-w1p2', 'pane_id': 'w1:p2', 'agent': 'claude',
                      'agent_status': 'idle', 'cwd': str(Path(self.tmp.name).resolve()),
                      'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-1'}}
        self.pane_exists = True
        self.agent_missing = False
        self.agent_error = None
        self.pane_error = None
        self.calls = []

        def fake(*argv, **kw):
            self.calls.append(argv)
            if argv[:2] == ('agent', 'get'):
                if self.agent_error:
                    raise h.HerdrError('agent error', code=self.agent_error)
                if self.agent_missing:
                    raise h.HerdrError('not found', code='agent_not_found')
                return {'agent': dict(self.agent)}
            if argv[:2] == ('pane', 'get'):
                if self.pane_error:
                    raise h.HerdrError('pane error', code=self.pane_error)
                if not self.pane_exists:
                    raise h.HerdrError('pane not found', code='pane_not_found')
                return {'pane': {'pane_id': argv[2]}}
            if argv[:2] == ('agent', 'prompt'):
                raise AssertionError('abandon/diagnose must never prompt a Worker')
            raise AssertionError(f'unexpected Herdr call {argv}')

        p = patch.object(h, 'herdr', side_effect=fake); p.start(); self.addCleanup(p.stop)
        lk = patch.object(h, 'lookup_agent', side_effect=lambda n, **kw: None if self.agent_missing else dict(self.agent))
        lk.start(); self.addCleanup(lk.stop)

    def trust_cancel(self):
        """Install a fake trusted control plane that confirms a safe cancel.

        T17 requires independent proof for every non-terminal cancellation; an
        idle/done observation is not proof. Tests that exercise the rest of
        cancel install this verified stub instead of relying on observation.
        """
        def control(payload):
            if payload.get('action') == 'cancel':
                return {'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
                        'verified_roles': [r['role'] for r in payload.get('roles', [])]}
            return {}
        p = patch.object(h, 'TRUSTED_CONTROL', control)
        p.start(); self.addCleanup(p.stop)
        return control

    def role(self):
        return {'role': 'developer', 'agent_name': self.agent['name'], 'pane_id': self.agent['pane_id'],
                'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-1'}}

    def make_run(self, status='active', stage_index=1, stage_attempt=1, dispatch_status='uncertain'):
        run = {'version': 3, 'id': RID, 'team': 'dev-lite', 'context': h.context(),
               'goal': 'Issue 2 recovery test goal', 'status': status,
               'created_at': h.now(), 'stage_index': stage_index, 'stage_attempt': stage_attempt,
               'round': 1, 'stages': ['plan', 'implement', 'self_test', 'finish'],
               'stage_definitions': {name: {'actor': 'developer', 'gate': 'auto'}
                                     for name in ('plan', 'implement', 'self_test', 'finish')},
               'roles': [self.role()],
               'settings': {'orchestration': {'max_rounds': 5, 'max_retries': 1, 'max_parallel': 2,
                                              'task_timeout_seconds': 60},
                            'governance': {'autonomy': 'workspace-write', 'approval': 'boundary',
                                           'escalate': 'critical', 'report': 'final'}},
               'dispatches': [{'id': DID, 'stage_index': stage_index, 'stage_attempt': stage_attempt,
                               'role': 'developer', 'effect': 'write', 'remediation': False,
                               'status': dispatch_status, 'created_at': h.now(),
                               'result_file': str(h.result_path(RID, DID))}],
               'events': [], 'approval_tokens': []}
        h.save(h.run_file(RID), run)
        return run

    def write_report(self, **over):
        doc = {'run_id': RID, 'dispatch_id': DID, 'role': 'developer', 'stage': 'implement',
               'stage_attempt': 1, 'status': 'complete', 'verdict': 'pass',
               'summary': 'A bound report written by the fake Worker for tests.',
               'evidence': ['Verified bound artifact inside an isolated test']}
        doc.update(over)
        path = h.result_path(RID, DID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))
        return path

    def diagnose(self):
        return h.diagnose_dispatch(RID, DID)

    def abandon(self, **kw):
        base = dict(run_id=RID, dispatch_id=DID, reason='Worker produced no verifiable report',
                    evidence=kw.pop('evidence'), confirm_untracked=False, quarantine_invalid=False)
        base.update(kw)
        return h.run_abandon(ns(**base))


class LegacyLedgerSessionTests(Base):
    """A legacy ledger has no session key; observation must still use the
    session the process actually resolved."""

    def test_abandon_uses_resolved_session_for_a_legacy_ledger(self):
        # Regression: reading the session from `run["session"]` yields None on a
        # legacy ledger, so the presence probe silently hit the DEFAULT server.
        # A live `working` Worker then looked `confirmed_absent` and the working
        # fence never fired.
        self.make_run()
        run = h.require_run(RID)
        run.pop('session', None)
        h.save(h.run_file(RID), run)
        self.agent['agent_status'] = 'working'
        snap = self.diagnose()['evidence']['snapshot_id']
        probed = []

        def fake(*argv, **kw):
            if argv[:2] in (('agent', 'get'), ('pane', 'get')):
                probed.append(kw.get('session'))
            return self.base_fake(*argv, **kw)

        self.base_fake = h.herdr.side_effect
        with patch.object(h, 'herdr', side_effect=fake):
            with self.assertRaises(h.GuardError) as ctx:
                self.abandon(evidence=snap, confirm_untracked=True)
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertIn('working', str(ctx.exception))
        self.assertTrue(probed)
        self.assertTrue(all(s == 'default' for s in probed),
                        f'presence probe lost the resolved session: {probed}')


class PresenceTests(Base):
    def test_present_with_matching_identity(self):
        out = h.agent_presence(self.role())
        self.assertEqual(out['state'], 'present')

    def test_confirmed_absent_requires_both_codes(self):
        self.agent_missing = True; self.pane_exists = False
        self.assertEqual(h.agent_presence(self.role())['state'], 'confirmed_absent')

    def test_pane_still_exists_is_unavailable(self):
        self.agent_missing = True; self.pane_exists = True
        out = h.agent_presence(self.role())
        self.assertEqual(out['state'], 'unavailable')
        self.assertEqual(out['code'], 'pane_exists')

    def test_unknown_code_is_unavailable(self):
        self.agent_error = 'agent_pane_busy'
        self.assertEqual(h.agent_presence(self.role())['state'], 'unavailable')

    def test_identity_drift_is_unavailable(self):
        self.agent['agent_session'] = {'kind': 'id', 'source': 'herdr:claude', 'value': 'other'}
        out = h.agent_presence(self.role())
        self.assertEqual(out['state'], 'unavailable')
        self.assertEqual(out['code'], 'identity_mismatch')

    def test_cwd_change_is_unavailable(self):
        self.agent['cwd'] = str(Path(self.tmp.name) / 'other-checkout')
        self.assertEqual(h.agent_presence(self.role())['state'], 'unavailable')


class ReconcileTests(Base):
    def test_blocked_observation_without_report_is_uncertain(self):
        self.make_run(dispatch_status='submitted_unsettled')
        out = h.reconcile_result(h.require_run(RID), h.require_run(RID)['dispatches'][0], 'blocked')
        self.assertEqual(out['status'], 'uncertain')
        self.assertEqual(out['observed_status'], 'blocked')
        self.assertEqual(out['diagnosis'], 'blocked_without_valid_report')

    def test_unknown_observation_never_settles_even_with_valid_report(self):
        self.make_run(dispatch_status='submitted_unsettled')
        self.write_report()
        out = h.reconcile_result(h.require_run(RID), h.require_run(RID)['dispatches'][0], 'working')
        self.assertEqual(out['status'], 'uncertain')

    def test_valid_report_with_blocked_observation_is_business_blocked(self):
        self.make_run(dispatch_status='submitted_unsettled')
        self.write_report(status='blocked')
        out = h.reconcile_result(h.require_run(RID), h.require_run(RID)['dispatches'][0], 'blocked')
        self.assertEqual(out['status'], 'blocked')
        self.assertTrue(out['report'])


class DiagnosisTests(Base):
    def test_missing_artifact_with_absent_worker_is_not_guessed(self):
        self.make_run()
        self.agent_missing = True; self.pane_exists = False
        out = self.diagnose()
        self.assertEqual(out['presence']['state'], 'confirmed_absent')
        # Absence of a report does not prove the work never started.
        self.assertEqual(out['diagnosis'], 'undetermined')
        self.assertFalse(out['artifact']['exists'])
        self.assertTrue(Path(out['evidence']['dir']).is_dir())

    def test_corrupt_regular_file_is_not_called_partial_execution(self):
        self.make_run()
        path = h.result_path(RID, DID); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{not json')
        out = self.diagnose()
        # A damaged report is not evidence that code was partially written.
        self.assertEqual(out['diagnosis'], 'undetermined')
        self.assertFalse(out['artifact']['valid'])
        self.assertEqual(out['artifact']['kind'], 'regular')

    def test_diagnose_is_read_only(self):
        self.make_run()
        before = h.require_run(RID)
        self.diagnose()
        self.assertEqual(h.require_run(RID), before)

    def test_diagnose_never_prompts(self):
        self.make_run()
        self.diagnose()
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))


class AbandonTests(Base):
    def test_working_present_cannot_abandon_even_with_confirmation(self):
        self.make_run()
        self.agent['agent_status'] = 'working'
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap, confirm_untracked=True)
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertIn('working', str(ctx.exception))
        print(f'working present: {ctx.exception.code}: {ctx.exception}')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'uncertain')

    def test_unavailable_still_accepts_explicit_risk(self):
        self.make_run()
        self.agent_error = 'agent_pane_busy'
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError):
            self.abandon(evidence=snap)
        self.assertEqual(self.abandon(evidence=snap, confirm_untracked=True)['status'], 'abandoned')
        self.assertIs(h.require_run(RID)['dispatches'][0]['abandon_risk_accepted'], True)
        print('unavailable: abandoned, abandon_risk_accepted=true')

    def test_confirmed_absent_without_confirmation(self):
        self.make_run()
        self.agent_missing = True; self.pane_exists = False
        snap = self.diagnose()['evidence']['snapshot_id']
        self.assertEqual(self.abandon(evidence=snap)['status'], 'abandoned')
        print('confirmed_absent: abandoned without confirmation')

    def test_present_idle_still_accepts_explicit_risk(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.assertEqual(self.abandon(evidence=snap, confirm_untracked=True)['status'], 'abandoned')

    def test_valid_report_refuses_abandon(self):
        self.make_run()
        self.write_report()
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap)
        self.assertEqual(ctx.exception.code, 'artifact_invalid')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'uncertain')

    def test_invalid_regular_file_requires_quarantine_flag(self):
        self.make_run()
        path = h.result_path(RID, DID); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken')
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap)
        self.assertEqual(ctx.exception.code, 'artifact_invalid')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'uncertain')

    def test_invalid_regular_file_quarantined_then_abandoned(self):
        self.make_run()
        path = h.result_path(RID, DID); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken bytes')
        snap = self.diagnose()
        out = self.abandon(evidence=snap['evidence']['snapshot_id'], quarantine_invalid=True,
                           confirm_untracked=True)
        self.assertEqual(out['status'], 'abandoned')
        rec = h.require_run(RID)['dispatches'][0]
        self.assertEqual(rec['status'], 'abandoned')
        self.assertTrue(rec['abandon_risk_accepted'])
        self.assertFalse(rec['abandon_fence_verified'])
        # Original is retained and evidence holds a copy with a matching digest.
        self.assertTrue(path.exists())
        evidence_dir = Path(out['evidence']['dir'])
        self.assertTrue((evidence_dir / 'manifest.json').is_file())
        self.assertTrue((evidence_dir / 'artifact.raw').is_file())
        self.assertEqual((evidence_dir / 'artifact.raw').read_text(), '{broken bytes')
        events = [e for e in h.require_run(RID)['events'] if e['type'] == 'abandon']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['dispatch_id'], DID)
        self.assertEqual(events[0]['stage_attempt'], 1)
        self.assertIn('presence', events[0])

    def test_unknown_evidence_snapshot_rejected(self):
        self.make_run()
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence='no-such-snapshot')
        self.assertEqual(ctx.exception.code, 'policy_forbidden')

    def test_symlink_fails_closed(self):
        self.make_run()
        target = Path(self.tmp.name) / 'outside.json'
        target.write_text('{}')
        path = h.result_path(RID, DID); path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap, quarantine_invalid=True, confirm_untracked=True)
        self.assertEqual(ctx.exception.code, 'artifact_invalid')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'uncertain')

    def test_fifo_fails_closed(self):
        self.make_run()
        path = h.result_path(RID, DID); path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap, quarantine_invalid=True, confirm_untracked=True)
        self.assertEqual(ctx.exception.code, 'artifact_invalid')

    def test_unproven_absence_requires_risk_acceptance(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap)
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')

    def test_short_reason_rejected(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence=snap, reason='too short', confirm_untracked=True)
        self.assertEqual(ctx.exception.code, 'policy_forbidden')

    def test_second_abandon_of_same_dispatch_rejected(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        with self.assertRaises(h.GuardError):
            self.abandon(evidence=snap, confirm_untracked=True)

    def test_concurrent_abandons_only_one_succeeds(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        results, barrier = [], threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                self.abandon(evidence=snap, confirm_untracked=True)
                results.append('ok')
            except (h.Error, KeyError, ValueError):
                results.append('rejected')

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(sorted(results), ['ok', 'rejected'])
        events = [e for e in h.require_run(RID)['events'] if e['type'] == 'abandon']
        self.assertEqual(len(events), 1)


class TerminalStateTests(Base):
    def test_collect_rejects_blocked_with_report(self):
        run = self.make_run(dispatch_status='blocked')
        self.write_report(status='blocked')
        run['dispatches'][0].update(h.reconcile_result(run, run['dispatches'][0], 'blocked'))
        h.save(h.run_file(RID), run)
        before = h.require_run(RID)
        for attempt in range(2):
            if attempt:
                self.write_report(status='blocked', summary='Changed report content for a second collection attempt.')
            with self.assertRaises(h.Error) as ctx:
                h.run_collect(ns(run_id=RID, dispatch_id=DID))
            print(f'blocked collect attempt {attempt + 1}: {ctx.exception}')
            self.assertEqual(h.require_run(RID), before)

    def test_collect_detects_terminal_dispatch_inside_lock(self):
        for status in ('settled', 'abandoned', 'blocked'):
            with self.subTest(status=status):
                run = self.make_run(dispatch_status='uncertain')
                changed = dict(run, dispatches=[dict(run['dispatches'][0], status=status,
                                                    report={'verdict': 'fail'}, artifact_sha256='a' * 64)])
                # Patch the ledger load, not require_run: collect resolves the
                # ledger through the identity-checking loader AND re-reads it
                # under the lock, so two loads happen either way.
                with patch.object(h, 'load', side_effect=[run, changed]), \
                     patch.object(h, 'verify_existing', return_value=True), \
                     patch.object(h, 'reconcile_result') as reconcile, \
                     patch.object(h, 'save') as save:
                    with self.assertRaises(h.Error):
                        h.run_collect(ns(run_id=RID, dispatch_id=DID))
                    reconcile.assert_not_called()
                    save.assert_not_called()

    def test_collect_detects_terminal_run_inside_lock(self):
        for status in ('finished', 'cancelled'):
            with self.subTest(status=status):
                run = self.make_run(dispatch_status='uncertain')
                changed = dict(run, status=status)
                # Patch the ledger load for the same reason as the dispatch case.
                with patch.object(h, 'load', side_effect=[run, changed]), \
                     patch.object(h, 'verify_existing', return_value=True), \
                     patch.object(h, 'reconcile_result') as reconcile, \
                     patch.object(h, 'save') as save:
                    with self.assertRaises(h.GuardError) as ctx:
                        h.run_collect(ns(run_id=RID, dispatch_id=DID))
                    self.assertEqual(ctx.exception.code, 'run_terminal')
                    reconcile.assert_not_called()
                    save.assert_not_called()

    def test_collect_legacy_blocked_without_report_is_uncertain(self):
        # CURRENT legacy blocked with no report is a normal collect: it is
        # unresolved, never a business result.
        self.make_run(dispatch_status='blocked')
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['t5'], 'legacy_blocked_uncertain')
        self.assertEqual(out['status'], 'uncertain')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'uncertain')

    def test_collect_legacy_blocked_with_valid_report_settles(self):
        self.make_run(dispatch_status='blocked')
        self.write_report()
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['status'], 'settled')
        self.assertEqual(out['report']['verdict'], 'pass')

    def test_collect_does_not_reconcile_abandoned(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        late = self.write_report(summary='A late but structurally valid report arrives afterwards.')
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['status'], 'abandoned')
        rec = h.require_run(RID)['dispatches'][0]
        self.assertEqual(rec['status'], 'abandoned')
        self.assertNotIn('report', rec)
        self.assertNotIn('artifact_sha256', rec)
        self.assertTrue(late.exists())

    def test_late_artifact_does_not_become_gate_evidence(self):
        run = self.make_run(stage_index=2, dispatch_status='uncertain')
        run['dispatches'][0]['status'] = 'abandoned'
        h.save(h.run_file(RID), run)
        self.write_report()
        with self.assertRaises(h.Error):
            h.run_gate(ns(run_id=RID, decision='pass', evidence='Reviewed current attempt evidence'))
        self.assertFalse([e for e in h.require_run(RID)['events'] if e['type'] == 'gate'])

    def test_terminal_run_rejects_abandon(self):
        self.make_run(status='finished')
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence='x')
        self.assertEqual(ctx.exception.code, 'run_terminal')


class DispatchGuardTests(Base):
    def test_plain_dispatch_after_abandon_is_refused(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='retry the same work',
                              effect='write', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))

    def test_abandon_in_earlier_attempt_does_not_block_dispatch(self):
        run = self.make_run()
        run['dispatches'][0].update({'stage_attempt': 1, 'status': 'abandoned'})
        run['stage_attempt'] = 2
        h.save(h.run_file(RID), run)
        self.assertEqual(h.abandoned_predecessors(h.require_run(RID)), [])

    def test_drive_stops_without_creating_authorization(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        out = h.run_drive(ns(run_id=RID, resume=False, max_steps=3, deadline_seconds=30,
                             wait_timeout_seconds=None))
        self.assertEqual(out['stopped_reason'], 'recovery_authorization_invalid')
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))
        self.assertNotIn('recovery_authorization', h.require_run(RID))


class HistoricalTests(Base):
    def test_historical_blocked_not_pending_in_list_or_recover(self):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='uncertain')
        run['dispatches'].append({'id': 'cccccccccc', 'stage_index': 1, 'stage_attempt': 1,
                                  'role': 'developer', 'effect': 'write', 'status': 'blocked',
                                  'result_file': str(h.result_path(RID, 'cccccccccc'))})
        h.save(h.run_file(RID), run)
        listed = h.run_list()['runs'][0]
        self.assertEqual(listed['pending_dispatches'], [DID])
        self.assertEqual([x['id'] for x in listed['historical_decided']], ['cccccccccc'])
        recovered = h.run_recover(ns(run_id=RID))
        self.assertEqual([x['id'] for x in recovered['pending_dispatches']], [DID])
        self.assertEqual([x['id'] for x in recovered['historical_decided']], ['cccccccccc'])

    def test_historical_blocked_does_not_block_finish(self):
        run = self.make_run(status='active', stage_index=4, stage_attempt=1, dispatch_status='uncertain')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1, 'status': 'blocked'})
        run['events'] = [{'type': 'gate', 'stage_index': i, 'decision': 'pass'} for i in range(4)]
        h.save(h.run_file(RID), run)
        self.assertTrue(h.run_finish(ns(run_id=RID))['finished'])

    def test_unresolved_history_blocks_finish(self):
        run = self.make_run(status='active', stage_index=4, stage_attempt=1, dispatch_status='uncertain')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1, 'status': 'uncertain'})
        run['events'] = [{'type': 'gate', 'stage_index': i, 'decision': 'pass'} for i in range(4)]
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_finish(ns(run_id=RID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')

    def test_historical_dispatch_abandon_is_refused(self):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='uncertain')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1})
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            self.abandon(evidence='whatever')
        self.assertEqual(ctx.exception.code, 'history_unresolved')


class CancelTests(Base):
    def test_abandoned_run_cannot_cancel_without_a_trusted_control_plane(self):
        # D5 (T17): a real abandon on an ACTIVE Run leaves a TRANSIENT fault
        # group that is only persisted at recover-attempt. Cancel must still
        # require verified isolation/identity/side effects, so with no trusted
        # control plane it fails closed and the ledger is untouched.
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                            reason='Owner cancels after documented abandonment'))
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertEqual(h.require_run(RID)['status'], 'active')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'abandoned')

    def test_ordinary_run_cannot_cancel_without_a_trusted_control_plane(self):
        # T17-ORDINARY-CANCEL-NO-TRUSTED: an ordinary Run with no fault group
        # still cannot be cancelled on an idle/done observation alone.
        self.make_run(status='active', dispatch_status='settled')
        self.agent['agent_status'] = 'idle'
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertEqual(h.require_run(RID)['status'], 'active')

    def test_historical_abandoned_run_cannot_cancel_without_a_trusted_control_plane(self):
        # Only a HISTORICAL abandoned dispatch exists (an earlier attempt), so
        # there is no current fault group and no current abandonment either.
        run = self.make_run(status='active', stage_index=2, stage_attempt=2,
                            dispatch_status='settled')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1,
                                     'root_attempt': 1, 'status': 'abandoned',
                                     'abandon_reason': 'Earlier attempt was abandoned'})
        h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertEqual(h.require_run(RID)['status'], 'active')

    def test_cancel_succeeds_with_complete_verified_evidence(self):
        for status in ('active', 'paused', 'blocked_round_limit'):
            with self.subTest(status=status):
                self.make_run(status=status, dispatch_status='settled')
                seen = []
                def control(payload):
                    seen.append(payload)
                    return {'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
                            'verified_roles': [r['role'] for r in payload.get('roles', [])]}
                p = patch.object(h, 'TRUSTED_CONTROL', control)
                p.start(); self.addCleanup(p.stop)
                out = h.run_cancel(self.cancel_ns())
                self.assertEqual(out['status'], 'cancelled')
                self.assertEqual(len([c for c in seen if c.get('action') == 'cancel']), 1)

    def test_cancel_refuses_incomplete_verified_evidence(self):
        cases = ({'isolation': 'unknown', 'side_effects': 'known', 'revoked': False,
                  'verified_roles': ['developer']}, 'execution_not_fenced'),
        for verdict, code in cases:
            with self.subTest(code=code):
                self.make_run(status='active', dispatch_status='settled')
                p = patch.object(h, 'TRUSTED_CONTROL', lambda payload, v=verdict: dict(v))
                p.start(); self.addCleanup(p.stop)
                with self.assertRaises(h.GuardError) as ctx:
                    h.run_cancel(self.cancel_ns())
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(h.require_run(RID)['status'], 'active')

    def test_cancel_refuses_a_partial_role_verification(self):
        self.make_run(status='active', dispatch_status='settled')
        p = patch.object(h, 'TRUSTED_CONTROL',
                         lambda payload: {'isolation': 'confirmed', 'side_effects': 'known',
                                          'revoked': False, 'verified_roles': []})
        p.start(); self.addCleanup(p.stop)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'identity_mismatch')

    def test_clean_run_can_cancel_without_a_trusted_control_plane(self):
        # Class A: never dispatched, so there is no executor to isolate. The
        # ledger writes the dispatch before prompting, so an empty list proves
        # no prompt was ever sent.
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID)
        run['dispatches'] = []
        h.save(h.run_file(RID), run)
        out = h.run_cancel(self.cancel_ns())
        self.assertEqual(out['status'], 'cancelled')
        self.assertEqual(out['cancel_scope'], 'no_execution_history')
        run = h.require_run(RID)
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['events'][-1]['cancel_scope'], 'no_execution_history')

    def test_clean_run_negative_requires_the_confirm_id_and_reason(self):
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID); run['dispatches'] = []; h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.Error):
            h.run_cancel(ns(run_id=RID, confirm_run_id='ffffffffffff',
                            reason='Mismatched confirmation identifier'))
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        with self.assertRaises(h.Error):
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID, reason='too short'))
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_run_with_any_dispatch_needs_the_trusted_control_plane(self):
        # Class B: even one already-settled dispatch means an executor may exist.
        self.make_run(status='active', dispatch_status='settled')
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertEqual(h.require_run(RID)['status'], 'active')

    def test_empty_dispatch_run_with_a_fault_group_needs_the_control_plane(self):
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID)
        run['dispatches'] = []
        run['fault_groups'] = [{'id': 'g1', 'stage_index': 1, 'root_attempt': 1,
                                'status': 'open', 'members': []}]
        h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_empty_dispatch_run_with_a_recovery_authorization_needs_the_control_plane(self):
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID)
        run['dispatches'] = []
        run['recovery_authorizations'] = [{'id': 'auth1', 'run_id': RID, 'stage_index': 1,
                                           'target_attempt': 2, 'group_id': 'g1',
                                           'status': 'active', 'slots': {},
                                           'required_roles': ['developer']}]
        h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_terminal_runs_are_still_refused_for_both_classes(self):
        for clean in (True, False):
            for status in ('finished', 'cancelled'):
                with self.subTest(clean=clean, status=status):
                    self.make_run(status=status, dispatch_status='settled')
                    run = h.require_run(RID)
                    if clean:
                        run['dispatches'] = []
                        h.save(h.run_file(RID), run)
                    with self.assertRaises(h.GuardError) as ctx:
                        h.run_cancel(self.cancel_ns())
                    self.assertEqual(ctx.exception.code, 'run_terminal')

    def test_class_b_cancel_still_records_its_scope(self):
        self.make_run(status='active', dispatch_status='settled')
        self.trust_cancel()
        out = h.run_cancel(self.cancel_ns())
        self.assertEqual(out['cancel_scope'], 'execution_history')
        self.assertEqual(h.require_run(RID)['events'][-1]['cancel_scope'],
                         'execution_history')

    def test_class_b_refuses_each_invalid_receipt_shape(self):
        # Three distinct illegal receipts: isolation not confirmed, side effects
        # unknown, and a role set that does not cover every actor.
        bad = [
            ({'isolation': 'unknown', 'side_effects': 'known', 'revoked': False,
              'verified_roles': ['developer']}, 'execution_not_fenced'),
            ({'isolation': 'confirmed', 'side_effects': 'unknown', 'revoked': False,
              'verified_roles': ['developer']}, 'execution_not_fenced'),
            ({'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
              'verified_roles': []}, 'identity_mismatch'),
        ]
        for receipt, code in bad:
            with self.subTest(code=code, receipt=receipt):
                self.make_run(status='active', dispatch_status='settled')
                before = h.run_file(RID).read_bytes()
                p = patch.object(h, 'TRUSTED_CONTROL', lambda payload, r=receipt: dict(r))
                p.start(); self.addCleanup(p.stop)
                with self.assertRaises(h.GuardError) as ctx:
                    h.run_cancel(self.cancel_ns())
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_class_b_refuses_a_revoked_or_missing_receipt(self):
        for receipt in ({'isolation': 'confirmed', 'side_effects': 'known', 'revoked': True,
                         'verified_roles': ['developer']}, None, 'not-a-mapping'):
            with self.subTest(receipt=receipt):
                self.make_run(status='active', dispatch_status='settled')
                before = h.run_file(RID).read_bytes()
                p = patch.object(h, 'TRUSTED_CONTROL', lambda payload, r=receipt: r)
                p.start(); self.addCleanup(p.stop)
                with self.assertRaises(h.GuardError) as ctx:
                    h.run_cancel(self.cancel_ns())
                self.assertEqual(ctx.exception.code, 'execution_not_fenced')
                self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_class_a_grading_happens_after_the_lock_is_held(self):
        # A concurrent dispatch that lands between the outer read and the lock
        # must downgrade the cancel to class B, never to "class A with a
        # dispatch already in the ledger".
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID); run['dispatches'] = []; h.save(h.run_file(RID), run)
        real_locked = h.locked
        state = {'done': False}

        @contextlib.contextmanager
        def racing_lock():
            if not state['done']:
                state['done'] = True
                with real_locked():
                    seeded = h.require_run(RID)
                    seeded['dispatches'] = [{'id': 'dddddddddd', 'stage_index': 1,
                                             'stage_attempt': 1, 'root_attempt': 1,
                                             'role': 'developer', 'effect': 'write',
                                             'status': 'settled', 'created_at': h.now(),
                                             'result_file': str(h.result_path(RID, 'dddddddddd'))}]
                    h.save(h.run_file(RID), seeded)
            with real_locked():
                yield

        with patch.object(h, 'locked', racing_lock):
            with self.assertRaises(h.GuardError) as ctx:
                h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        run = h.require_run(RID)
        self.assertEqual(run['status'], 'active')
        self.assertEqual(len(run['dispatches']), 1)
        self.assertFalse(any(e.get('type') == 'cancel' for e in run['events']))

    def test_class_a_cancel_is_refused_for_wrong_reason_with_specific_code(self):
        self.make_run(status='active', dispatch_status='settled')
        run = h.require_run(RID); run['dispatches'] = []; h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.Error) as ctx:
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID, reason='short'))
        self.assertIn('confirm-run-id', str(ctx.exception))
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def cancel_ns(self):
        return ns(run_id=RID, confirm_run_id=RID,
                  reason='Owner closes a Run that has no remaining exit path')

    def test_every_non_terminal_status_can_cancel(self):
        # Issue #2 B1: cancel is the designed escape hatch for EVERY
        # non-terminal Run (design T17). Gating it on `active` re-created the
        # exact lock-up this Issue exists to remove. T17 still needs trusted
        # proof, supplied here by the verified stub.
        for status in ('active', 'blocked', 'paused', 'blocked_round_limit'):
            with self.subTest(status=status):
                self.make_run(status=status, dispatch_status='settled')
                self.trust_cancel()
                out = h.run_cancel(self.cancel_ns())
                self.assertEqual(out['status'], 'cancelled')
                self.assertEqual(h.require_run(RID)['status'], 'cancelled')

    def assert_invalid_status_cannot_cancel(self, status, missing=False):
        run = self.make_run(status=status, dispatch_status='settled')
        if missing:
            del run['status']
            h.save(h.run_file(RID), run)
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(self.cancel_ns())
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_unknown_status_cannot_cancel(self):
        self.assert_invalid_status_cannot_cancel('future_unknown_state')

    def test_missing_status_cannot_cancel(self):
        self.assert_invalid_status_cannot_cancel(None, missing=True)

    def test_none_status_cannot_cancel(self):
        self.assert_invalid_status_cannot_cancel(None)

    def test_empty_status_cannot_cancel(self):
        self.assert_invalid_status_cannot_cancel('')

    def test_terminal_status_cannot_cancel(self):
        for status in ('finished', 'cancelled'):
            with self.subTest(status=status):
                self.make_run(status=status, dispatch_status='settled')
                with self.assertRaises(h.GuardError) as ctx:
                    h.run_cancel(self.cancel_ns())
                self.assertEqual(ctx.exception.code, 'run_terminal')
                self.assertEqual(h.require_run(RID)['status'], status)

    def test_unresolved_dispatch_still_blocks_cancel(self):
        # The exemption must not weaken the existing safety checks.
        for status in ('active', 'blocked_round_limit'):
            with self.subTest(status=status):
                self.make_run(status=status, dispatch_status='uncertain')
                with self.assertRaises(h.Error):
                    h.run_cancel(self.cancel_ns())
                self.assertEqual(h.require_run(RID)['status'], status)

    def test_live_working_worker_still_blocks_cancel(self):
        # T17 no longer consults an observation at all; an unresolved delivery
        # must still block before any trusted check can even be reached.
        self.make_run(status='blocked_round_limit', dispatch_status='uncertain')
        with self.assertRaises(h.Error):
            h.run_cancel(self.cancel_ns())
        self.assertEqual(h.require_run(RID)['status'], 'blocked_round_limit')


class RecoveryM2Base(Base):
    """M2: fault groups, R1 role budgets, one-shot recovery authorization."""

    def setUp(self):
        super().setUp()
        self.trusted_calls = []
        self.trusted_receipt_over = {}
        # A second Worker (reviewer) so recovery can span two role slots.
        self.reviewer_agent = {'name': 'rev-w1p3', 'pane_id': 'w1:p3', 'agent': 'claude',
                               'agent_status': 'idle',
                               'cwd': str(Path(self.tmp.name).resolve()),
                               'agent_session': {'kind': 'id', 'source': 'herdr:claude',
                                                 'value': 'session-2'}}
        original_lookup = h.lookup_agent
        original_herdr = h.herdr
        self.base_herdr = original_herdr

        def lookup(name, **kw):
            if name == self.reviewer_agent['name']:
                return dict(self.reviewer_agent)
            return original_lookup(name, **kw)

        def herdr_get(*argv, **kw):
            if argv[:2] == ('agent', 'get') and argv[2] == self.reviewer_agent['name']:
                return {'agent': dict(self.reviewer_agent)}
            return original_herdr(*argv, **kw)

        lk = patch.object(h, 'lookup_agent', side_effect=lookup)
        lk.start(); self.addCleanup(lk.stop)
        hg = patch.object(h, 'herdr', side_effect=herdr_get)
        hg.start(); self.addCleanup(hg.stop)

        def control(payload):
            self.trusted_calls.append(dict(payload))
            expires = (h.dt.datetime.now(h.dt.timezone.utc)
                       + h.dt.timedelta(minutes=10)).isoformat()
            # Mirror exactly what a real control plane would bind. The receipt
            # is only "trusted" in that the test owns it; the code under test
            # still verifies every field, so an incomplete mirror must fail.
            receipt = {
                'receipt_id': 'rcpt-1', 'issued_at': h.now(), 'expires_at': expires,
                'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
                'run_id': payload.get('run_id'), 'stage_index': payload.get('stage_index'),
                'root_attempt': payload.get('root_attempt'),
                'target_attempt': payload.get('target_attempt'),
                'group_id': payload.get('group_id'), 'group_sha256': payload.get('group_sha256'),
                'group_members_sha256': payload.get('group_members_sha256'),
                'action': payload.get('action'),
                'action_fingerprint': payload.get('action_fingerprint'),
                'effect': payload.get('effect'),
                'identity_pins_sha256': payload.get('identity_pins_sha256'),
                'idempotency_key': payload.get('idempotency_key'),
                'message_sha256': payload.get('message_sha256'),
                'required_roles': list(payload.get('required_roles') or []),
                'role_budgets': payload.get('role_budgets'),
                'role_actions': payload.get('role_actions'),
                'approval': {'approver': 'owner-a', 'scope': payload.get('action'),
                             'receipt': 'authz-1', 'issued_at': h.now(), 'expires_at': expires},
            }
            if payload.get('action') == 'recovery-gate-approval':
                return {'receipt_id': 'human-1', 'authenticated': True, 'approver': 'owner-b',
                        'receipt': 'human-1',
                        'issued_at': h.now(), 'expires_at': expires,
                        'decision': payload.get('decision'),
                        'run_id': payload.get('run_id'),
                        'stage_index': payload.get('stage_index'),
                        'stage_attempt': payload.get('stage_attempt'),
                        'dispatch_hashes_sha256': payload.get('dispatch_hashes_sha256'),
                        'group_id': payload.get('group_id'),
                        'authorization_id': payload.get('authorization_id')}
            if payload.get('action') == 'cancel':
                return {'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
                        'verified_roles': [r['role'] for r in payload.get('roles', [])] if
                        isinstance(payload.get('roles'), list) and payload['roles'] and
                        isinstance(payload['roles'][0], dict) and 'role' in payload['roles'][0]
                        else payload.get('verified_roles')}
            receipt.update(self.trusted_receipt_over)
            return receipt

        self.trusted_control = control
        self.install_control(control)
        self.prompts = []

        def fake(*argv, **kw):
            if argv[:2] == ('agent', 'prompt'):
                self.prompts.append(argv[2])
                return {'agent': dict(self.agent)}
            return self.base_herdr(*argv, **kw)

        original = h.herdr
        self.base_herdr = original
        p = patch.object(h, 'herdr', side_effect=fake)
        p.start(); self.addCleanup(p.stop)

    def install_control(self, control):
        p = patch.object(h, 'TRUSTED_CONTROL', control)
        p.start(); self.addCleanup(p.stop)

    def run_two_roles(self, status='active', stage_attempt=1, stage_index=1, dispatch_status='settled'):
        """A reviewer role is needed so a recovery group spans two role slots."""
        reviewer = {'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                    'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                    'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-2'}}
        run = self.make_run(status=status, stage_index=stage_index, stage_attempt=stage_attempt,
                            dispatch_status=dispatch_status)
        run['roles'].append(reviewer)
        run['stage_definitions'][run['stages'][stage_index]] = {'actor': 'developer', 'gate': 'auto'}
        h.save(h.run_file(RID), run)
        return run

    def add_abandoned(self, dispatch_id=DID, role='developer', stage_attempt=1, **extra):
        """Mark one root dispatch abandoned. Reuses the default record when possible."""
        if not h.run_file(RID).exists():
            self.make_run(dispatch_status='settled')
        run = h.require_run(RID)
        rec = next((d for d in run['dispatches'] if d['id'] == dispatch_id), None)
        if rec is None:
            rec = {'id': dispatch_id, 'stage_index': run['stage_index'],
                   'stage_attempt': stage_attempt, 'role': role, 'effect': 'write',
                   'created_at': h.now(), 'result_file': str(h.result_path(RID, dispatch_id))}
            run['dispatches'].append(rec)
        rec.update({'status': 'abandoned', 'root_attempt': run['stage_attempt'],
                    'abandon_reason': 'Worker produced no verifiable report',
                    'abandon_evidence': 'ev', 'abandon_risk_accepted': True})
        rec.update(extra)
        h.save(h.run_file(RID), run)
        return rec

    def recovery_dispatch(self, role='developer', **over):
        run = h.require_run(RID)
        auth = h.recovery_authorization_for(run)
        bound = (auth.get('role_actions') or {}).get(role) or {}
        kwargs = dict(run_id=RID, role=role, message=bound.get('message'),
                      effect=bound.get('effect'), remediation=False, no_wait=True)
        kwargs.update(over)
        return h.run_dispatch(ns(**kwargs))

    def recover(self, evidence=None, dispatch_id=None):
        run = h.require_run(RID)
        target = dispatch_id or next((d['id'] for d in run['dispatches']
                                      if d.get('status') == 'abandoned'), None)
        if target is None:
            target = run['dispatches'][0]['id']
        snap = evidence if evidence is not None else self.diagnose()['evidence']['snapshot_id']
        return h.run_recover_attempt(ns(run_id=RID, dispatch_id=target, evidence=snap))


class FaultGroupTests(RecoveryM2Base):
    def test_plain_uncertain_submission_does_not_open_a_group(self):
        self.make_run(dispatch_status='submission_uncertain')
        self.assertIsNone(h.current_group(h.require_run(RID)))

    def test_uncertain_after_collect_opens_a_group(self):
        self.make_run(dispatch_status='uncertain')
        run = h.require_run(RID)
        run['dispatches'][0]['collected_at'] = h.now()
        h.save(h.run_file(RID), run)
        group = h.current_group(h.require_run(RID))
        self.assertIsNotNone(group)
        self.assertEqual(group['members'], [DID])

    def test_reading_a_run_never_records_a_group(self):
        self.make_run(dispatch_status='abandoned')
        before = h.run_file(RID).read_bytes()
        h.run_list(); h.run_recover(ns(run_id=RID))
        h.current_group(h.require_run(RID))
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertNotIn('fault_groups', h.require_run(RID))

    def test_members_are_not_caller_selected(self):
        self.run_two_roles(stage_attempt=1)
        self.add_abandoned(DID)
        self.add_abandoned('cccccccccc', role='reviewer')
        group = h.current_group(h.require_run(RID))
        self.assertEqual(sorted(group['members']), sorted([DID, 'cccccccccc']))

    def test_recover_attempt_requires_an_abandoned_member(self):
        self.run_two_roles(dispatch_status='blocked')
        run = h.require_run(RID)
        run['dispatches'][0].update({'report': {'verdict': 'fail'}, 'artifact_sha256': 'a' * 64,
                                     'collected_at': h.now()})
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence='x')
        self.assertIn(ctx.exception.code, ('fault_group_open', 'recovery_authorization_invalid'))
        self.assertEqual(h.require_run(RID)['stage_attempt'], 1)

    def test_recover_attempt_fails_closed_without_trusted_control(self):
        self.install_control(None)
        self.add_abandoned()
        snap = self.diagnose()['evidence']['snapshot_id']
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_receipt_must_confirm_isolation_and_side_effects(self):
        for key, value in (('isolation', 'unknown'), ('side_effects', 'unknown')):
            with self.subTest(key=key):
                self.make_run(dispatch_status='settled')
                self.trusted_receipt_over = {key: value}
                self.add_abandoned()
                snap = self.diagnose()['evidence']['snapshot_id']
                with self.assertRaises(h.GuardError) as ctx:
                    self.recover(evidence=snap)
                self.assertIn(ctx.exception.code, ('execution_not_fenced', 'side_effects_unknown'))
                self.assertEqual(h.require_run(RID)['stage_attempt'], 1)

    def test_receipt_must_bind_the_group_and_attempt(self):
        for key, value in (('group_sha256', 'f' * 64), ('target_attempt', 9)):
            with self.subTest(key=key):
                self.make_run(dispatch_status='settled')
                self.trusted_receipt_over = {key: value}
                self.add_abandoned()
                snap = self.diagnose()['evidence']['snapshot_id']
                with self.assertRaises(h.GuardError) as ctx:
                    self.recover(evidence=snap)
                self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
                self.assertEqual(h.require_run(RID)['stage_attempt'], 1)


class RecoveryAttemptTests(RecoveryM2Base):
    def test_recover_attempt_retains_a_new_attempt_without_prompting(self):
        self.add_abandoned()
        snap = self.diagnose()['evidence']['snapshot_id']
        out = self.recover(evidence=snap)
        self.assertEqual(out['stage_attempt'], 2)
        self.assertEqual(out['round'], 1)
        self.assertTrue(out['round_unchanged'])
        self.assertEqual(sorted(out['required_roles']), ['developer'])
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 2)
        self.assertEqual(run['round'], 1)
        auth = run['recovery_authorizations'][-1]
        self.assertEqual(auth['slots']['developer']['status'], 'unused')

    def test_recovery_dispatch_consumes_exactly_one_slot(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        self.assertEqual(out['status'], 'submitted_unsettled')
        run = h.require_run(RID)
        auth = run['recovery_authorizations'][-1]
        self.assertEqual(auth['slots']['developer']['status'], 'consumed')
        self.assertEqual(auth['slots']['developer']['dispatch_id'], out['dispatch_id'])
        self.assertEqual(run['dispatches'][-1]['recovery_authorization_id'], auth['id'])

    def test_second_dispatch_cannot_reuse_a_consumed_slot(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        h.run_collect(ns(run_id=RID, dispatch_id=first['dispatch_id']))
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='redo again',
                              effect='write', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertEqual(len([d for d in h.require_run(RID)['dispatches']
                              if d.get('stage_attempt') == 2]), 1)

    def test_dispatch_without_authorization_in_a_group_is_refused(self):
        self.add_abandoned()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='plain retry',
                              effect='write', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))

    def test_remediation_is_not_part_of_a_recovery_authorization(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='subtask',
                              effect='write', remediation=True, no_wait=True))
        self.assertEqual(ctx.exception.code, 'policy_forbidden')

    def test_second_recovery_chain_is_refused_while_recovery_active(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(h.require_run(RID)['status'], 'active')
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')


class BudgetDriftTests(RecoveryM2Base):
    def test_slot_consumption_refuses_a_ledger_budget_drift(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        # Simulate a concurrent writer that spent the remaining budget after the
        # authorization was issued but before the slot was consumed.
        run = h.require_run(RID)
        run['dispatches'].append({'id': 'ffffffffff', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'developer', 'effect': 'write',
                                  'status': 'settled', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'ffffffffff'))})
        h.save(h.run_file(RID), run)
        current = h.require_run(RID)
        with self.assertRaises(h.GuardError) as ctx:
            h.consume_recovery_slot(current, h.recovery_authorization_for(current),
                                    'developer', 'aaaaaaaaaa', 'write')
        self.assertEqual(ctx.exception.code, 'recovery_budget_exhausted')
        self.assertEqual(h.recovery_authorization_for(h.require_run(RID))['slots']['developer']['status'],
                         'unused')


class RecoveryRoleScopeTests(RecoveryM2Base):
    def review_group(self):
        self.make_run(stage_index=3, stage_attempt=1, dispatch_status='settled')
        run = h.require_run(RID)
        run['stages'] = ['plan_brief', 'implement', 'self_test', 'code_review', 'accept']
        run['stage_definitions']['code_review'] = {'actor': 'reviewer', 'gate': 'auto'}
        run['roles'].append({'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                             'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                             'agent_session': {'kind': 'id', 'source': 'herdr:claude',
                                               'value': 'session-2'}})
        run['dispatches'][0].update({'stage_index': 3, 'root_attempt': 1, 'status': 'abandoned',
                                     'abandon_reason': 'Worker produced no verifiable report'})
        h.save(h.run_file(RID), run)

    def test_recovery_can_dispatch_a_required_role_that_is_not_the_stage_actor(self):
        self.review_group()
        out = self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(out['required_roles'], ['developer', 'reviewer'])
        sent = self.recovery_dispatch()
        self.assertEqual(sent['status'], 'submitted_unsettled')
        rec = h.require_run(RID)['dispatches'][-1]
        self.assertEqual(rec['role'], 'developer')
        self.assertEqual(rec['stage_attempt'], 2)

    def test_non_required_role_is_still_refused_in_a_recovery_attempt(self):
        self.review_group()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='reviewer2', message='not in the group',
                              effect='read', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')


class BudgetTests(RecoveryM2Base):
    def test_budget_is_one_plus_max_retries_per_role(self):
        self.run_two_roles()
        run = h.require_run(RID)
        self.assertEqual(h.role_budget(run, 1, 1, 'developer'),
                         {'limit': 2, 'used': 1, 'remaining': 1})

    def test_exhausted_budget_blocks_the_whole_group(self):
        # developer already used both slots of the logical root -> no new attempt.
        self.run_two_roles()
        self.add_abandoned()
        run = h.require_run(RID)
        run['dispatches'].append({'id': 'dddddddddd', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'developer', 'effect': 'write',
                                  'status': 'settled', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'dddddddddd'))})
        h.save(h.run_file(RID), run)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'recovery_budget_exhausted')
        self.assertEqual(h.require_run(RID)['stage_attempt'], 1)
        self.assertFalse(any(c[:2] == ('agent', 'prompt') for c in self.calls))

    def test_budget_never_refunds_across_a_recovery_attempt(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        h.run_collect(ns(run_id=RID, dispatch_id=out['dispatch_id']))
        run = h.require_run(RID)
        # attempt 2 belongs to root 1, so both dispatches count against one budget.
        self.assertEqual(h.role_budget(run, 1, 1, 'developer'),
                         {'limit': 2, 'used': 2, 'remaining': 0})

    def test_business_revise_starts_a_fresh_root(self):
        self.run_two_roles()
        run = h.require_run(RID)
        run['dispatches'][0]['status'] = 'settled'
        run['dispatches'][0].update({'report': {'verdict': 'pass'}, 'artifact_sha256': 'a' * 64})
        h.save(h.run_file(RID), run)
        h.run_gate(ns(run_id=RID, decision='revise', evidence='Reviewer asked for a rework pass'))
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 2)
        self.assertEqual(h.logical_root(run), 2)
        self.assertEqual(h.role_budget(run, 1, 2, 'developer')['used'], 0)


class AuthorizedGateTests(RecoveryM2Base):
    def settle_recovery(self, verdict='pass'):
        out = self.recovery_dispatch()
        rid, did = RID, out['dispatch_id']
        run = h.require_run(rid)
        path = h.result_path(rid, did)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'run_id': rid, 'dispatch_id': did, 'role': 'developer', 'stage': 'implement',
            'stage_attempt': run['stage_attempt'], 'status': 'complete', 'verdict': verdict,
            'summary': 'Recovery attempt report written by the fake Worker.',
            'evidence': ['Recovery attempt report verified inside the isolated test']}))
        h.run_collect(ns(run_id=rid, dispatch_id=did))
        return did

    def test_recovery_gate_pass_closes_the_group_and_returns_to_active(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        self.settle_recovery()
        out = h.run_gate(ns(run_id=RID, decision='pass', evidence='Recovery report independently reviewed'))
        self.assertEqual(out['status'], 'active')
        run = h.require_run(RID)
        self.assertEqual(run['round'], 1)
        self.assertEqual(run.get('fault_groups', [{}])[-1]['status'], 'closed')
        self.assertEqual(run['stage_index'], 2)

    def test_recovery_gate_refuses_an_unconsumed_role_slot(self):
        reviewer = {'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                    'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                    'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-2'}}
        self.run_two_roles()
        run = h.require_run(RID)
        run['roles'].append(reviewer)
        run['dispatches'].append({'id': 'eeeeeeeeee', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'reviewer', 'effect': 'read',
                                  'status': 'abandoned', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'eeeeeeeeee')),
                                  'abandon_reason': 'Worker produced no verifiable report'})
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, 'eeeeeeeeee')['evidence']['snapshot_id']
        out = self.recover(evidence=shot, dispatch_id='eeeeeeeeee')
        self.assertEqual(sorted(out['required_roles']), ['developer', 'reviewer'])
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        self.settle_recovery()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='pass', evidence='Only developer recovered so far'))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertEqual(h.require_run(RID)['stage_index'], 1)

    def test_ordinary_gate_refuses_a_group_with_abandoned_members(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned('cccccccccc', role='reviewer')
        run = h.require_run(RID)
        run['dispatches'].append({'id': 'ffffffffff', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'developer', 'effect': 'write',
                                  'status': 'settled', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'ffffffffff')),
                                  'report': {'verdict': 'pass'}, 'artifact_sha256': 'a' * 64})
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='pass', evidence='Looks fine from here honestly'))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')

    def test_finish_refuses_a_recorded_open_group(self):
        self.make_run(stage_index=4, dispatch_status='settled')
        run = h.require_run(RID)
        run['stages'] = ['plan_brief', 'implement', 'self_test', 'code_review']
        run['fault_groups'] = [{'id': 'g1', 'stage_index': 1, 'root_attempt': 1, 'status': 'open',
                                'members': [DID]}]
        run['events'] = [{'type': 'gate', 'stage_index': i, 'decision': 'pass'} for i in range(4)]
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_finish(ns(run_id=RID))
        self.assertEqual(ctx.exception.code, 'fault_group_open')

    def test_finish_refuses_an_abandonment_with_no_closed_group(self):
        self.make_run(stage_index=4, dispatch_status='settled')
        run = h.require_run(RID)
        run['stages'] = ['plan_brief', 'implement', 'self_test', 'code_review']
        run['dispatches'][0].update({'stage_index': 1, 'root_attempt': 1, 'status': 'abandoned',
                                     'abandon_reason': 'Worker produced no verifiable report'})
        run['events'] = [{'type': 'gate', 'stage_index': i, 'decision': 'pass'} for i in range(4)]
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_finish(ns(run_id=RID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')

    def test_recovery_gate_closes_the_group_and_keeps_the_round(self):
        # Scope is exactly what this exercises: abandon -> recover-attempt ->
        # dispatch -> collect -> gate pass closes the group with round unchanged.
        # Full stage-by-stage completion runs the REAL primitives in
        # RegressionRound2EndToEndTests; this test no longer fakes ledger state.
        self.make_run(stage_index=1, dispatch_status='settled')
        run = h.require_run(RID)
        run['dispatches'][0].update({'stage_index': 1, 'root_attempt': 1, 'status': 'abandoned',
                                     'abandon_reason': 'Worker produced no verifiable report'})
        run['events'] = [{'type': 'gate', 'stage_index': 0, 'decision': 'pass'}]
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        did = self.recovery_dispatch()['dispatch_id']
        run = h.require_run(RID)
        path = h.result_path(RID, did)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_id': RID, 'dispatch_id': did, 'role': 'developer',
                                    'stage': 'implement', 'stage_attempt': run['stage_attempt'],
                                    'status': 'complete', 'verdict': 'pass',
                                    'summary': 'Recovered implementation report inside the test.',
                                    'evidence': ['Recovered implementation report verified']}))
        h.run_collect(ns(run_id=RID, dispatch_id=did))
        out = h.run_gate(ns(run_id=RID, decision='pass', evidence='Recovery evidence independently reviewed'))
        self.assertEqual(out['status'], 'active')
        run = h.require_run(RID)
        self.assertEqual(run['round'], 1)
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')
        self.assertEqual(run['recovery_authorizations'][-1]['status'], 'revoked')

    def test_finish_requires_a_verifiable_closure_proof(self):
        self.make_run(stage_index=4, dispatch_status='settled')
        run = h.require_run(RID)
        run['stages'] = ['plan_brief', 'implement', 'self_test', 'code_review']
        run['fault_groups'] = [{'id': 'g1', 'stage_index': 1, 'root_attempt': 1, 'status': 'closed',
                                'members': [DID]}]
        run['events'] = [{'type': 'gate', 'stage_index': i, 'decision': 'pass'} for i in range(4)]
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_finish(ns(run_id=RID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')

    def test_round_limit_from_a_business_revise_cannot_recover(self):
        self.add_abandoned()
        run = h.require_run(RID)
        run['status'] = 'blocked_round_limit'
        run['round'] = 6
        run['round_limit_provenance'] = {'kind': 'business', 'verified_by': 'internal'}
        h.save(h.run_file(RID), run)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')
        self.assertEqual(h.require_run(RID)['status'], 'blocked_round_limit')

    def test_cancel_revokes_recovery_authorization(self):
        self.add_abandoned()
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                        reason='Owner cancels after documented abandonment'))
        run = h.require_run(RID)
        self.assertEqual(run['status'], 'cancelled')
        self.assertEqual(run['recovery_authorizations'][-1]['status'], 'revoked')
        self.assertEqual(run['fault_groups'][-1]['status'], 'cancelled')


class LateArtifactTests(Base):
    def historical(self, status, attempt=1):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='settled')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': attempt,
                                     'root_attempt': attempt, 'status': status})
        run['dispatches'].append({'id': 'ffffffffff', 'stage_index': 2, 'stage_attempt': 2,
                                  'root_attempt': 2, 'role': 'developer', 'effect': 'write',
                                  'status': 'settled', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'ffffffffff')),
                                  'report': {'verdict': 'pass'}, 'artifact_sha256': 'a' * 64})
        h.save(h.run_file(RID), run)

    def test_historical_abandoned_late_report_is_evidence_only(self):
        self.historical('abandoned')
        path = self.write_report()
        before = {k: h.require_run(RID)['dispatches'][0].get(k)
                  for k in ('status', 'report', 'artifact_sha256')}
        with self.assertRaises(h.GuardError) as ctx:
            h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')
        rec = h.require_run(RID)['dispatches'][0]
        self.assertEqual({k: rec.get(k) for k in before}, before)
        self.assertTrue(path.exists())
        snapshots = list((h.STATE / 'evidence' / RID / DID).glob('*/manifest.json'))
        self.assertEqual(len(snapshots), 1)
        self.assertIn('late_artifact_rejected', snapshots[0].read_text())

    def test_historical_uncertain_late_report_is_evidence_only(self):
        self.historical('uncertain')
        self.write_report()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')
        rec = h.require_run(RID)['dispatches'][0]
        self.assertEqual(rec['status'], 'uncertain')
        self.assertNotIn('report', rec)
        self.assertNotIn('artifact_sha256', rec)

    def test_historical_abandoned_without_a_report_is_not_recollected(self):
        self.historical('abandoned')
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(ctx.exception.code, 'stale_snapshot')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertFalse((h.STATE / 'evidence' / RID).exists())

    def test_late_artifact_never_becomes_new_attempt_gate_evidence(self):
        self.historical('abandoned')
        self.write_report()
        run = h.require_run(RID)
        self.assertEqual(h.pending_dispatches(run), [])
        with self.assertRaises(h.Error):
            h.run_gate(ns(run_id=RID, decision='pass', evidence='Trying to use the late artifact'))
        self.assertFalse([e for e in h.require_run(RID)['events'] if e.get('type') == 'gate'])


class RegressionRound2Tests(RecoveryM2Base):
    """Each test here reproduces a defect found by an independent probe."""

    def finalize_run(self, decide_during_wait):
        self.make_run(dispatch_status='settled')

        def prompt(*argv, **kw):
            if argv[:2] == ('agent', 'prompt'):
                decide_during_wait()
                return {'agent': dict(self.agent)}
            return self.base_herdr(*argv, **kw)

        with patch.object(h, 'herdr', side_effect=prompt):
            return h.run_dispatch(ns(run_id=RID, role='developer', message='run the task',
                                     effect='write', remediation=False, no_wait=False,
                                     wait_timeout_seconds=5))

    # --- (1) repeated recover-attempt for the same request --------------------
    def test_same_request_recover_twice_returns_the_same_authorization(self):
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        first = self.recover(evidence=snap)
        before = h.require_run(RID)
        second = self.recover(evidence=snap)
        self.assertEqual(second['authorization_id'], first['authorization_id'])
        self.assertEqual(second['stage_attempt'], first['stage_attempt'])
        self.assertEqual(second['slots'], first['slots'])
        self.assertTrue(second['replay'])
        after = h.require_run(RID)
        self.assertEqual(after['stage_attempt'], before['stage_attempt'])
        self.assertEqual(len(after['recovery_authorizations']), len(before['recovery_authorizations']))
        self.assertEqual(after['round'], before['round'])

    def test_a_different_evidence_snapshot_is_not_treated_as_a_replay(self):
        self.add_abandoned(DID)
        first_snap = self.diagnose()['evidence']['snapshot_id']
        self.recover(evidence=first_snap)
        second_snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=second_snap)
        self.assertIn(ctx.exception.code, ('history_unresolved', 'run_state_forbidden'))

    def test_replay_does_not_relax_history_writes_for_other_dispatches(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['settings']['orchestration']['max_retries'] = 2
        run['dispatches'].append({'id': 'ffffffffff', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'developer', 'effect': 'write',
                                  'status': 'abandoned', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'ffffffffff'))})
        h.save(h.run_file(RID), run)
        snap = self.diagnose()['evidence']['snapshot_id']
        self.recover(evidence=snap)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_recover_attempt(ns(run_id=RID, dispatch_id='ffffffffff', evidence=snap))
        self.assertEqual(ctx.exception.code, 'history_unresolved')

    # --- (2) a receipt must bind every field, not merely be nonempty ---------
    def test_receipt_binding_a_foreign_run_is_refused(self):
        self.trusted_receipt_over = {'run_id': '000000000000'}
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertEqual(h.run_file(RID).read_bytes(), before)

    def test_receipt_binding_a_foreign_stage_root_or_attempt_is_refused(self):
        for key, value in (('stage_index', 999), ('root_attempt', 999), ('target_attempt', 999)):
            with self.subTest(key=key):
                self.trusted_receipt_over = {key: value}
                self.make_run(dispatch_status='settled')
                self.add_abandoned(DID)
                snap = self.diagnose()['evidence']['snapshot_id']
                with self.assertRaises(h.GuardError) as ctx:
                    self.recover(evidence=snap)
                self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
                self.assertEqual(h.require_run(RID)['stage_attempt'], 1)

    def test_receipt_approval_scope_must_match_the_action(self):
        self.trusted_receipt_over = {'approval': {'approver': 'owner-a', 'scope': 'UNRELATED_ACTION',
                                                  'receipt': 'x', 'issued_at': h.now(),
                                                  'expires_at': h.now()}}
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertIn(ctx.exception.code, ('approval_required', 'recovery_authorization_invalid'))

    def test_receipt_must_bind_member_snapshot_with_report_hashes(self):
        self.trusted_receipt_over = {'group_members_sha256': 'f' * 64}
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')

    def test_receipt_must_bind_role_budgets_and_actions(self):
        for key in ('role_budgets', 'role_actions', 'identity_pins_sha256', 'effect'):
            with self.subTest(key=key):
                self.trusted_receipt_over = {key: {} if key != 'effect' else 'sensitive'}
                self.make_run(dispatch_status='settled')
                self.add_abandoned(DID)
                snap = self.diagnose()['evidence']['snapshot_id']
                with self.assertRaises(h.GuardError) as ctx:
                    self.recover(evidence=snap)
                self.assertIn(ctx.exception.code,
                              ('recovery_authorization_invalid', 'side_effects_unknown'))

    def test_receipt_rejected_when_revoked_future_or_expired(self):
        cases = {'revoked': True, 'issued_at': '2099-01-01T00:00:00+00:00',
                 'expires_at': '2000-01-01T00:00:00+00:00'}
        for key, value in cases.items():
            with self.subTest(key=key):
                self.trusted_receipt_over = {key: value}
                self.make_run(dispatch_status='settled')
                self.add_abandoned(DID)
                snap = self.diagnose()['evidence']['snapshot_id']
                with self.assertRaises(h.GuardError) as ctx:
                    self.recover(evidence=snap)
                self.assertIn(ctx.exception.code,
                              ('recovery_authorization_invalid', 'execution_not_fenced'))
                self.assertEqual(h.require_run(RID)['stage_attempt'], 1)

    def test_receipt_timestamp_without_timezone_is_refused(self):
        self.trusted_receipt_over = {'issued_at': '2026-01-01T00:00:00'}
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError):
            self.recover(evidence=snap)

    # --- (3) the trusted source must still work at the moment of use ---------
    def test_dispatch_fails_closed_when_the_trusted_source_disappears(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.install_control(None)
        before = len(self.prompts)
        with self.assertRaises(h.GuardError) as ctx:
            self.recovery_dispatch()
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(len(self.prompts), before)
        self.assertEqual(h.recovery_authorization_for(h.require_run(RID))['slots']['developer']['status'],
                         'unused')

    def test_dispatch_refuses_a_read_slot_spent_as_a_write(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        auth = h.recovery_authorization_for(run)
        auth['role_actions']['developer'] = {'effect': 'read',
                                             'fingerprint': h.action_fingerprint(
                                                 'recovery-dispatch', run, 'developer', 'read',
                                                 h.get_role(run, 'developer'))}
        h.save(h.run_file(RID), run)
        before = len(self.prompts)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='escalate to write',
                              effect='write', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertEqual(len(self.prompts), before)

    # --- (4) async finalize must not touch a terminal Run --------------------
    def test_wait_return_never_writes_into_a_finished_run(self):
        def finish_meanwhile():
            run = h.require_run(RID)
            run['status'] = 'finished'
            h.save(h.run_file(RID), run)

        self.make_run(dispatch_status='settled')
        self.finalize_run(finish_meanwhile)
        self.assertEqual(h.require_run(RID)['status'], 'finished')
        rec = h.require_run(RID)['dispatches'][-1]
        self.assertIn(rec['status'], ('submission_uncertain', 'submitted_unsettled'))
        self.assertNotIn('report', rec)

    def test_no_wait_and_exception_paths_also_skip_terminal_runs(self):
        for mode in ('no_wait', 'exception'):
            with self.subTest(mode=mode):
                self.make_run(dispatch_status='settled')

                def prompt(*argv, **kw):
                    if argv[:2] == ('agent', 'prompt'):
                        run = h.require_run(RID)
                        run['status'] = 'finished'
                        h.save(h.run_file(RID), run)
                        if mode == 'exception':
                            raise h.Error('boom after submitting')
                        return {'agent': dict(self.agent)}
                    return self.base_herdr(*argv, **kw)

                with patch.object(h, 'herdr', side_effect=prompt):
                    try:
                        h.run_dispatch(ns(run_id=RID, role='developer', message='run',
                                          effect='write', remediation=False,
                                          no_wait=(mode == 'no_wait'), wait_timeout_seconds=5))
                    except h.Error:
                        pass
                run = h.require_run(RID)
                self.assertEqual(run['status'], 'finished')
                self.assertNotIn('report', run['dispatches'][-1])
                self.assertFalse([e for e in run['events'] if e.get('type') == 'async_finalize_refused'])

    def test_dispatch_into_a_moved_attempt_is_refused_before_prompting(self):
        self.make_run(dispatch_status='settled')
        real_locked = h.locked
        state = {'bumped': False}

        @contextlib.contextmanager
        def racing_lock():
            # The attempt moves between the outside precheck and the lock, so a
            # lock-in check that only compares stage_index would let a stale
            # prompt be recorded into the new attempt.
            if not state['bumped']:
                state['bumped'] = True
                with real_locked():
                    run = h.require_run(RID)
                    run['stage_attempt'] = 9
                    h.save(h.run_file(RID), run)
            with real_locked():
                yield

        before = len(self.prompts)
        with patch.object(h, 'locked', racing_lock):
            with self.assertRaises(h.Error):
                h.run_dispatch(ns(run_id=RID, role='developer', message='stale prompt',
                                  effect='write', remediation=False, no_wait=True))
        self.assertEqual(len(self.prompts), before)
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 9)
        self.assertEqual(len(run['dispatches']), 1)

    # --- (5) recovery_active restrictions ------------------------------------
    def recovery_active_run(self):
        if not h.run_file(RID).exists():
            self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        return run

    def test_recovery_active_cannot_revise(self):
        run = self.recovery_active_run()
        out = self.recovery_dispatch()
        path = h.result_path(RID, out['dispatch_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_id': RID, 'dispatch_id': out['dispatch_id'],
                                    'role': 'developer', 'stage': 'implement',
                                    'stage_attempt': run['stage_attempt'],
                                    'status': 'blocked', 'verdict': 'needs-work',
                                    'summary': 'Business negative found during recovery.',
                                    'evidence': ['Business negative inside the recovery test']}))
        h.run_collect(ns(run_id=RID, dispatch_id=out['dispatch_id']))
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='revise',
                          evidence='Business request for another round of work'))
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')
        self.assertEqual(h.require_run(RID)['round'], run['round'])

    def test_recovery_active_cannot_pause_or_resume_or_open_second_chain(self):
        self.recovery_active_run()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_pause(ns(run_id=RID), True)
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')

    def test_recovery_revise_without_a_business_negative_is_refused(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        run = h.require_run(RID)
        path = h.result_path(RID, out['dispatch_id'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_id': RID, 'dispatch_id': out['dispatch_id'],
                                    'role': 'developer', 'stage': 'implement',
                                    'stage_attempt': run['stage_attempt'],
                                    'status': 'complete', 'verdict': 'pass',
                                    'summary': 'Recovered work passed but reviewer wants revise.',
                                    'evidence': ['Recovered pass inside the recovery test']}))
        h.run_collect(ns(run_id=RID, dispatch_id=out['dispatch_id']))
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='revise',
                          evidence='Trying to wash the technical fault away'))
        self.assertEqual(ctx.exception.code, 'fault_group_open')


class RegressionRound2GroupTests(RecoveryM2Base):
    """Group membership must track later recovery dispatches, not freeze."""

    def test_recovery_dispatch_joins_the_same_group(self):
        self.add_abandoned(DID)
        auth = self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        run = h.require_run(RID)
        group = h.fault_group_for(run)
        self.assertIn(out['dispatch_id'], group['members'])
        self.assertNotIn(out['dispatch_id'], group['authorized_members'])
        self.assertEqual(group['authorized_members'], auth and
                         h.require_run(RID)['recovery_authorizations'][-1]['authorized_members'])

    def test_a_new_recovery_fault_reopens_the_group_and_blocks_a_second_chain(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == out['dispatch_id'])
        rec['status'] = 'abandoned'
        rec['abandon_reason'] = 'Recovery attempt produced no verifiable report'
        h.save(h.run_file(RID), run)
        run = h.require_run(RID)
        group = h.current_group(run)
        self.assertIn(out['dispatch_id'], group['members'])
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertIn(ctx.exception.code, ('dispatch_unresolved', 'history_unresolved',
                                           'recovery_authorization_invalid', 'run_state_forbidden'))

    def test_group_cannot_be_closed_from_a_caller_selected_subset(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        out = self.recovery_dispatch()
        # Leave the recovery dispatch unresolved; the group must still see it.
        run = h.require_run(RID)
        group = h.current_group(run)
        with self.assertRaises(h.GuardError) as ctx:
            h.gate_fault_group(run, 'pass')
        self.assertEqual(ctx.exception.code, 'dispatch_unresolved')
        self.assertIn(out['dispatch_id'], group['members'])


class RegressionRound2EndToEndTests(RecoveryM2Base):
    """A real two-role recovery run through the real functions, no faked gates."""

    def test_two_role_recovery_reaches_finish_through_real_calls(self):
        reviewer = {'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                    'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                    'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-2'}}
        run = self.make_run(stage_index=1, dispatch_status='settled')
        run['stages'] = ['plan_brief', 'implement', 'code_review', 'accept']
        run['stage_definitions'] = {
            'plan_brief': {'actor': 'lead', 'gate': 'auto'},
            'implement': {'actor': 'developer', 'gate': 'auto'},
            'code_review': {'actor': 'reviewer', 'gate': 'auto'},
            'accept': {'actor': 'lead', 'gate': 'auto'}}
        run['roles'].append(reviewer)
        run['settings']['orchestration']['max_retries'] = 2
        run['dispatches'][0].update({'stage_index': 1, 'root_attempt': 1, 'role': 'developer',
                                     'status': 'settled', 'report': {'verdict': 'pass'},
                                     'artifact_sha256': 'a' * 64})
        run['dispatches'].append({'id': 'cccccccccc', 'stage_index': 1, 'stage_attempt': 1,
                                  'root_attempt': 1, 'role': 'reviewer', 'effect': 'read',
                                  'status': 'abandoned', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'cccccccccc')),
                                  'abandon_reason': 'Reviewer produced no verifiable report'})
        run['events'] = [{'type': 'gate', 'stage_index': 0, 'decision': 'pass'}]
        h.save(h.run_file(RID), run)
        # Capture the developer report path before recovery (attempt 1).
        shot = h.diagnose_dispatch(RID, 'cccccccccc')['evidence']['snapshot_id']
        h.run_recover_attempt(ns(run_id=RID, dispatch_id='cccccccccc', evidence=shot))
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 2)

        def write(did, role, stage, verdict, status='complete'):
            path = h.result_path(RID, did)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'run_id': RID, 'dispatch_id': did, 'role': role,
                                        'stage': stage, 'stage_attempt': run['stage_attempt'],
                                        'status': status, 'verdict': verdict,
                                        'summary': 'Recovery report written in the E2E test.',
                                        'evidence': ['Recovery E2E evidence inside the test']}))

        dev = self.recovery_dispatch()
        write(dev['dispatch_id'], 'developer', 'implement', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=dev['dispatch_id']))
        rev = self.recovery_dispatch('reviewer')
        write(rev['dispatch_id'], 'reviewer', 'implement', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=rev['dispatch_id']))
        # Real gate: pass the implement stage out of recovery.
        h.run_gate(ns(run_id=RID, decision='pass',
                      evidence='Both required roles produced new recovery reports'))
        run = h.require_run(RID)
        self.assertEqual(run['stage_index'], 2)
        self.assertEqual(run['status'], 'active')
        self.assertEqual(run['round'], 1)
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')
        self.assertEqual(run['recovery_authorizations'][-1]['status'], 'revoked')
        # Ordinary stage continuation through the real dispatch/collect/gate.
        rev2 = h.run_dispatch(ns(run_id=RID, role='reviewer', message='code review',
                                 effect='read', remediation=False, no_wait=True))
        write(rev2['dispatch_id'], 'reviewer', 'code_review', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=rev2['dispatch_id']))
        h.run_gate(ns(run_id=RID, decision='pass', evidence='Independent code review passed'))
        h.run_gate(ns(run_id=RID, decision='pass', evidence='Lead accepted the recovered work'))
        self.assertTrue(h.run_finish(ns(run_id=RID))['finished'])
        self.assertEqual(h.require_run(RID)['status'], 'finished')


class RegressionRound2CancelTests(RecoveryM2Base):
    """T17 under recovery must verify isolation, not trust idle/done."""

    def recovery_active_run(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        return run

    def test_cancel_under_recovery_needs_verified_isolation(self):
        self.recovery_active_run()
        self.install_control(lambda payload: {'isolation': 'unknown', 'side_effects': 'known',
                                              'revoked': False, 'verified_roles': ['developer']})
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                            reason='Owner cancels the recovery attempt'))
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.require_run(RID)['status'], 'recovery_active')

    def test_cancel_under_recovery_refuses_an_unverified_role_set(self):
        self.recovery_active_run()
        self.install_control(lambda payload: {'isolation': 'confirmed', 'side_effects': 'known',
                                              'revoked': False, 'verified_roles': ['developer']})
        self.reviewer_agent  # two-role Run would need both verified
        run = h.require_run(RID)
        run['roles'].append({'role': 'reviewer', 'agent_name': self.reviewer_agent['name'],
                             'pane_id': 'w1:p3', 'kind': 'claude',
                             'cwd': str(Path(self.tmp.name).resolve()),
                             'agent_session': {'kind': 'id', 'source': 'herdr:claude',
                                               'value': 'session-2'}})
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                            reason='Owner cancels the recovery attempt'))
        self.assertEqual(ctx.exception.code, 'identity_mismatch')

    def test_transient_group_after_a_real_abandon_also_requires_the_control_plane(self):
        # No persisted fault group exists yet: the abandon left only a transient
        # one. The Run is active. T17 still demands verified isolation.
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        run = h.require_run(RID)
        self.assertEqual(run['status'], 'active')
        self.assertFalse(run.get('fault_groups'))
        self.install_control(None)  # production boundary: no trusted control plane
        before = h.run_file(RID).read_bytes()
        with self.assertRaises(h.GuardError) as ctx:
            h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                            reason='Owner cancels after a real abandonment'))
        self.assertEqual(ctx.exception.code, 'execution_not_fenced')
        self.assertEqual(h.run_file(RID).read_bytes(), before)
        self.assertEqual(h.require_run(RID)['status'], 'active')

    def test_transient_group_cancel_succeeds_only_with_full_verification(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        out = h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                              reason='Owner cancels after a verified abandonment'))
        self.assertEqual(out['status'], 'cancelled')
        run = h.require_run(RID)
        self.assertEqual(run['events'][-1]['type'], 'cancel')

    def test_transient_group_cancel_verification_reports_the_group(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        seen = []
        self.install_control(lambda payload: (seen.append(payload), {
            'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
            'verified_roles': ['developer']})[1])
        h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                        reason='Owner cancels after a verified abandonment'))
        cancel_calls = [p for p in seen if p.get('action') == 'cancel']
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0]['fault_group_ids'][0], 'unrecorded-1-1')

    def test_cancel_under_recovery_succeeds_with_full_verification(self):
        self.recovery_active_run()
        out = h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                              reason='Owner cancels the recovery attempt'))
        self.assertEqual(out['status'], 'cancelled')
        run = h.require_run(RID)
        self.assertEqual(run['recovery_authorizations'][-1]['status'], 'revoked')
        self.assertEqual(run['recovery_authorizations'][-1]['slots']['developer']['status'], 'revoked')

    def test_ordinary_cancel_still_works_without_recovery(self):
        self.make_run(status='blocked_round_limit', dispatch_status='settled')
        out = h.run_cancel(ns(run_id=RID, confirm_run_id=RID,
                              reason='Owner cancels an ordinary locked run'))
        self.assertEqual(out['status'], 'cancelled')


class RegressionRound2LegacyTests(Base):
    """Current legacy blocked rechecks; historical late artifacts stay inert."""

    def test_historical_legacy_blocked_with_a_valid_report_is_adopted(self):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='settled')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1, 'root_attempt': 1,
                                     'status': 'blocked'})
        h.save(h.run_file(RID), run)
        self.write_report()
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['t5'], 'legacy_blocked_report_adopted')
        self.assertEqual(out['status'], 'blocked')
        self.assertTrue(out['artifact_sha256'])
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'blocked')

    def test_historical_legacy_blocked_without_a_report_only_diagnoses(self):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='settled')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1, 'root_attempt': 1,
                                     'status': 'blocked'})
        h.save(h.run_file(RID), run)
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['t5'], 'legacy_blocked_requires_recheck')
        self.assertEqual(h.require_run(RID)['dispatches'][0]['status'], 'blocked')

    def test_historical_late_artifact_keeps_status_hash_and_report(self):
        run = self.make_run(stage_index=2, stage_attempt=2, dispatch_status='settled')
        run['dispatches'][0].update({'stage_index': 1, 'stage_attempt': 1, 'root_attempt': 1,
                                     'status': 'uncertain'})
        h.save(h.run_file(RID), run)
        self.write_report()
        before = dict(h.require_run(RID)['dispatches'][0])
        with self.assertRaises(h.GuardError) as ctx:
            h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(ctx.exception.code, 'history_unresolved')
        after = h.require_run(RID)['dispatches'][0]
        for key in ('status', 'report', 'artifact_sha256'):
            self.assertEqual(after.get(key), before.get(key))


class RegressionRound2SecondFaultTests(RecoveryM2Base):
    def test_active_recovery_can_open_a_second_attempt_when_budget_allows(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['settings']['orchestration']['max_retries'] = 3
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == first['dispatch_id'])
        rec['status'] = 'abandoned'
        rec['abandon_reason'] = 'Recovery attempt failed without a report'
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, first['dispatch_id'])['evidence']['snapshot_id']
        second = self.recover(evidence=shot, dispatch_id=first['dispatch_id'])
        self.assertNotEqual(second['authorization_id'],
                            h.require_run(RID)['recovery_authorizations'][0]['id'])
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 3)
        self.assertEqual(run['round'], 1)
        self.assertEqual(run['status'], 'active')

    def test_recovery_active_never_opens_a_second_attempt(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['settings']['orchestration']['max_retries'] = 3
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == first['dispatch_id'])
        rec['status'] = 'abandoned'
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, first['dispatch_id'])['evidence']['snapshot_id']
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=shot, dispatch_id=first['dispatch_id'])
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')
        self.assertEqual(h.require_run(RID)['stage_attempt'], 2)


CONCURRENCY_WORKER = r"""
import importlib.util, json, os, sys, datetime as dt
MODULE = sys.argv[1]
spec = importlib.util.spec_from_file_location('htm', MODULE)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)

def control(payload):
    now = h.dt.datetime.now(h.dt.timezone.utc)
    expires = (now + h.dt.timedelta(minutes=10)).isoformat()
    if payload.get('type') == 'verify' and payload.get('action') == 'round-limit-provenance':
        return dict(payload, kind='technical', verified_by='external', business_negative=False)
    return {
        'receipt_id': os.environ.get('PROC_RECEIPT', 'rcpt-1'),
        'issued_at': h.now(), 'expires_at': expires,
        'idempotency_key': payload.get('idempotency_key'),
        'message_sha256': payload.get('message_sha256'),
        'isolation': 'confirmed', 'side_effects': 'known', 'revoked': False,
        'run_id': payload.get('run_id'), 'stage_index': payload.get('stage_index'),
        'root_attempt': payload.get('root_attempt'),
        'target_attempt': payload.get('target_attempt'),
        'group_id': payload.get('group_id'), 'group_sha256': payload.get('group_sha256'),
        'group_members_sha256': payload.get('group_members_sha256'),
        'action': payload.get('action'),
        'action_fingerprint': payload.get('action_fingerprint'),
        'effect': payload.get('effect'),
        'identity_pins_sha256': payload.get('identity_pins_sha256'),
        'required_roles': list(payload.get('required_roles') or []),
        'role_budgets': payload.get('role_budgets'),
        'role_actions': payload.get('role_actions'),
        'approval': {'approver': 'owner-a', 'scope': payload.get('action'),
                     'receipt': 'authz-1', 'issued_at': h.now(), 'expires_at': expires},
    }

def _agent_get(*argv, **kw):
    return {'agent': {'name': 'dev-w1p2', 'pane_id': 'w1:p2', 'agent': 'claude',
                      'agent_status': 'idle', 'cwd': os.environ['PROC_CWD'],
                      'agent_session': {'kind': 'id', 'source': 'herdr:claude',
                                        'value': 'session-1'}}}

h.herdr = _agent_get
h.TRUSTED_CONTROL = control
# Same boundary stub as the in-process tests: HERDR_* is provided by the
# environment, and the real ancestry probe cannot see a subprocess pane.
h.pane_of_current_process = lambda *a, **kw: {
    'session': os.environ.get('HERDR_SESSION') or 'default',
    'workspace': os.environ.get('HERDR_WORKSPACE_ID', ''),
    'tab': os.environ.get('HERDR_TAB_ID', ''),
    'pane': os.environ.get('HERDR_PANE_ID', '')}
import types
if os.environ.get('PROC_MODE') == 'dispatch':
    args = types.SimpleNamespace(run_id=sys.argv[2], role='developer',
                                 message=os.environ['PROC_MSG'], effect='write',
                                 remediation=False, no_wait=True)
    try:
        out = h.run_dispatch(args)
        print(json.dumps({'ok': True, 'dispatch_id': out['dispatch_id'],
                          'status': out['status']}))
    except h.Error as e:
        print(json.dumps({'ok': False, 'error': str(e)}))
    raise SystemExit(0)
args = types.SimpleNamespace(run_id=sys.argv[2], dispatch_id=sys.argv[3], evidence=sys.argv[4])
try:
    out = h.run_recover_attempt(args)
    print(json.dumps({'ok': True, 'authorization_id': out['authorization_id'],
                      'stage_attempt': out['stage_attempt'], 'replay': out.get('replay')}))
except h.Error as e:
    print(json.dumps({'ok': False, 'error': str(e)}))
"""


class SelfTestNegativeTests(RecoveryM2Base):
    """Lead's specific review points, each pinned by a test."""

    # --- 1. T13 reachability with settled negatives and valid blocked --------
    def recovery_with_roles(self, stage_index=1):
        reviewer = {'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                    'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                    'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-2'}}
        run = self.make_run(stage_index=stage_index, dispatch_status='settled')
        run['roles'].append(reviewer)
        run['settings']['orchestration']['max_retries'] = 2
        run['dispatches'][0].update({'status': 'settled', 'report': {'verdict': 'pass'},
                                     'artifact_sha256': 'a' * 64})
        h.save(h.run_file(RID), run)

    def add_abandoned_reviewer(self):
        run = h.require_run(RID)
        run['dispatches'].append({'id': 'cccccccccc', 'stage_index': run['stage_index'],
                                  'stage_attempt': 1, 'root_attempt': 1, 'role': 'reviewer',
                                  'effect': 'read', 'status': 'abandoned', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, 'cccccccccc')),
                                  'abandon_reason': 'Reviewer produced no verifiable report'})
        h.save(h.run_file(RID), run)

    def report(self, did, role, verdict, status='complete'):
        run = h.require_run(RID)
        path = h.result_path(RID, did)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_id': RID, 'dispatch_id': did, 'role': role,
                                    'stage': run['stages'][run['stage_index']],
                                    'stage_attempt': run['stage_attempt'], 'status': status,
                                    'verdict': verdict,
                                    'summary': 'Recovery report written by the fake Worker.',
                                    'evidence': ['Recovery negative-case evidence inside test']}))

    def run_both_roles(self, dev_verdict, dev_status, rev_verdict, rev_status):
        self.recovery_with_roles()
        self.add_abandoned_reviewer()
        shot = h.diagnose_dispatch(RID, 'cccccccccc')['evidence']['snapshot_id']
        h.run_recover_attempt(ns(run_id=RID, dispatch_id='cccccccccc', evidence=shot))
        for role, verdict, status in (('developer', dev_verdict, dev_status),
                                      ('reviewer', rev_verdict, rev_status)):
            out = self.recovery_dispatch(role)
            self.report(out['dispatch_id'], role, verdict, status)
            h.run_collect(ns(run_id=RID, dispatch_id=out['dispatch_id']))

    def test_settled_needs_work_can_be_revised_as_a_business_negative(self):
        self.run_both_roles('needs-work', 'complete', 'pass', 'complete')
        before = h.require_run(RID)
        out = h.run_gate(ns(run_id=RID, decision='revise',
                            evidence='Both roles produced new recovery reports; developer needs work'))
        self.assertEqual(out['round'], before['round'] + 1)
        run = h.require_run(RID)
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')
        self.assertEqual(h.logical_root(run), run['stage_attempt'])
        self.assertEqual(run['role_budget'] if False else
                         h.role_budget(run, run['stage_index'], 1, 'developer')['used'], 2)

    def test_valid_blocked_can_be_revised_as_a_business_negative(self):
        self.run_both_roles('pass', 'complete', 'needs-work', 'blocked')
        out = h.run_gate(ns(run_id=RID, decision='revise',
                            evidence='Reviewer blocked the recovered work for business reasons'))
        self.assertEqual(out['round'], 2)
        run = h.require_run(RID)
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')

    def test_recovery_active_still_cannot_revise(self):
        self.run_both_roles('needs-work', 'complete', 'pass', 'complete')
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='revise',
                          evidence='Business revise attempted from the restricted state'))
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')

    def test_revise_over_max_rounds_is_recorded_as_business(self):
        self.run_both_roles('needs-work', 'complete', 'pass', 'complete')
        run = h.require_run(RID)
        run['round'] = run['settings']['orchestration']['max_rounds']
        h.save(h.run_file(RID), run)
        h.run_gate(ns(run_id=RID, decision='revise',
                      evidence='Business revise that exhausts the documented rounds'))
        run = h.require_run(RID)
        self.assertEqual(run['status'], 'blocked_round_limit')
        self.assertEqual(run['round_limit_provenance']['kind'], 'business')

    # --- 2. live identity ----------------------------------------------------
    def test_live_agent_missing_name_is_identity_mismatch(self):
        self.add_abandoned(DID)
        self.agent.pop('name')
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'identity_mismatch')

    def test_live_cwd_drift_is_identity_mismatch(self):
        self.add_abandoned(DID)
        self.agent['cwd'] = str(Path(self.tmp.name) / 'other-checkout')
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'identity_mismatch')

    def test_live_session_drift_is_identity_mismatch(self):
        self.add_abandoned(DID)
        self.agent['agent_session'] = {'kind': 'id', 'source': 'herdr:claude', 'value': 'other'}
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'identity_mismatch')

    def test_identity_is_reobserved_under_the_dispatch_lock(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        bound = (h.recovery_authorization_for(run).get('role_actions') or {})['developer']
        self.agent['agent_session'] = {'kind': 'id', 'source': 'herdr:claude', 'value': 'drifted'}
        before = len(self.prompts)
        # Either the earlier precheck or the lock-in re-observation must refuse;
        # both are identity failures and neither may reach a prompt.
        with self.assertRaises(h.Error) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message=bound['message'],
                              effect='write', remediation=False, no_wait=True))
        self.assertIn('identity', str(ctx.exception))
        self.assertIn('session', str(ctx.exception))
        self.assertEqual(len(self.prompts), before)

    # --- 3. action binding ---------------------------------------------------
    def test_a_different_message_cannot_spend_an_authorization(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        before = len(self.prompts)
        with self.assertRaises(h.GuardError) as ctx:
            h.run_dispatch(ns(run_id=RID, role='developer', message='something else entirely',
                              effect='write', remediation=False, no_wait=True))
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')
        self.assertEqual(len(self.prompts), before)

    def test_stable_action_key_survives_attempts_and_differs_by_root(self):
        self.add_abandoned(DID)
        run = h.require_run(RID)
        key1 = h.root_action_key(run, ['developer'])
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        self.assertEqual(h.root_action_key(run, ['developer']), key1)
        run['stage_attempt'] = 5
        run['round'] = 2
        self.assertNotEqual(h.root_action_key(run, ['developer']), key1)

    def test_receipt_with_a_changed_idempotency_key_is_refused(self):
        self.trusted_receipt_over = {'idempotency_key': 'a-different-key'}
        self.add_abandoned(DID)
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'recovery_authorization_invalid')

    # --- 4. group completeness ----------------------------------------------
    def test_second_t7_binds_the_full_current_membership(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['settings']['orchestration']['max_retries'] = 4
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == first['dispatch_id'])
        rec.update({'status': 'abandoned', 'abandon_reason': 'Recovery failed without a report'})
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, first['dispatch_id'])['evidence']['snapshot_id']
        second = self.recover(evidence=shot, dispatch_id=first['dispatch_id'])
        run = h.require_run(RID)
        auth = next(a for a in run['recovery_authorizations'] if a['id'] == second['authorization_id'])
        self.assertIn(first['dispatch_id'], auth['authorized_members'])
        self.assertEqual(set(auth['authorized_members']),
                         set(h.fault_group_for(run)['members']) & set(auth['authorized_members']))

    def test_closure_proof_is_checked_against_the_ledger(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        did = self.recovery_dispatch()['dispatch_id']
        self.report(did, 'developer', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=did))
        h.run_gate(ns(run_id=RID, decision='pass',
                      evidence='Recovery evidence independently reviewed'))
        run = h.require_run(RID)
        group = run['fault_groups'][-1]
        self.assertEqual(h.closure_problems(run, group), [])
        tampered = dict(group, closure=dict(group['closure'], member_hashes_sha256='f' * 64))
        self.assertTrue(h.closure_problems(run, tampered))
        unretired = dict(group, closure=dict(group['closure'], gate_event_index=999))
        self.assertTrue(h.closure_problems(run, unretired))

    # --- 5. technical round limit -------------------------------------------
    def test_technical_round_limit_needs_the_trusted_verification(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['status'] = 'blocked_round_limit'
        run['round_limit_provenance'] = {'kind': 'technical', 'event_id': 'evt-1'}
        h.save(h.run_file(RID), run)
        self.install_control(lambda payload: {'kind': 'business', 'verified_by': 'external'})
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        self.assertEqual(ctx.exception.code, 'run_state_forbidden')

    def test_same_request_under_recovery_active_returns_the_same_authorization(self):
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        first = self.recover(evidence=snap)
        run = h.require_run(RID)
        run['status'] = 'recovery_active'
        h.save(h.run_file(RID), run)
        again = self.recover(evidence=snap)
        self.assertEqual(again['authorization_id'], first['authorization_id'])
        self.assertTrue(again['replay'])
        self.assertEqual(len(h.require_run(RID)['recovery_authorizations']), 1)

    # --- 6. recovery gate needs independent authentication -------------------
    def test_recovery_gate_without_authentication_is_refused(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        did = self.recovery_dispatch()['dispatch_id']
        self.report(did, 'developer', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=did))
        self.install_control(lambda payload: dict(payload, isolation='confirmed',
                                                  side_effects='known', revoked=False,
                                                  receipt_id=(payload.get('receipt_id')
                                                              or 'rcpt-1'),
                                                  issued_at=h.now(),
                                                  expires_at=(h.dt.datetime.now(h.dt.timezone.utc)
                                                              + h.dt.timedelta(minutes=5)).isoformat(),
                                                  authenticated=False))
        with self.assertRaises(h.GuardError) as ctx:
            h.run_gate(ns(run_id=RID, decision='pass',
                          evidence='Lead typed a long evidence string and hopes it counts'))
        self.assertEqual(ctx.exception.code, 'approval_required')

    # --- 7. late artifacts ---------------------------------------------------
    def test_current_abandoned_late_report_retains_a_real_snapshot(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        self.abandon(evidence=snap, confirm_untracked=True)
        self.write_report()
        out = h.run_collect(ns(run_id=RID, dispatch_id=DID))
        self.assertEqual(out['status'], 'abandoned')
        self.assertTrue(out['late_evidence_snapshot'])
        retained = h.STATE / 'evidence' / RID / DID / out['late_evidence_snapshot'] / 'manifest.json'
        self.assertTrue(retained.is_file())
        rec = h.require_run(RID)['dispatches'][0]
        self.assertEqual(rec['status'], 'abandoned')
        self.assertNotIn('report', rec)


class ReviewRoundDTests(RecoveryM2Base):
    """D1-D4 from the reviewer's second pass."""

    def two_role_run(self):
        reviewer = {'role': 'reviewer', 'agent_name': 'rev-w1p3', 'pane_id': 'w1:p3',
                    'kind': 'claude', 'cwd': str(Path(self.tmp.name).resolve()),
                    'agent_session': {'kind': 'id', 'source': 'herdr:claude', 'value': 'session-2'}}
        run = self.make_run(dispatch_status='settled')
        run['roles'].append(reviewer)
        run['settings']['orchestration']['max_retries'] = 4
        run['dispatches'][0].update({'status': 'settled', 'report': {'verdict': 'pass'},
                                     'artifact_sha256': 'a' * 64})
        h.save(h.run_file(RID), run)

    def add_abandoned_reviewer(self, did='cccccccccc'):
        run = h.require_run(RID)
        run['dispatches'].append({'id': did, 'stage_index': run['stage_index'],
                                  'stage_attempt': 1, 'root_attempt': 1, 'role': 'reviewer',
                                  'effect': 'read', 'status': 'abandoned', 'created_at': h.now(),
                                  'result_file': str(h.result_path(RID, did)),
                                  'abandon_reason': 'Reviewer produced no verifiable report'})
        h.save(h.run_file(RID), run)

    def write_report(self, did, role, verdict, status='complete'):
        run = h.require_run(RID)
        path = h.result_path(RID, did)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_id': RID, 'dispatch_id': did, 'role': role,
                                    'stage': run['stages'][run['stage_index']],
                                    'stage_attempt': run['stage_attempt'], 'status': status,
                                    'verdict': verdict,
                                    'summary': 'Recovery report written by the fake Worker.',
                                    'evidence': ['Recovery review-loop evidence inside test']}))

    def recover_both(self):
        self.two_role_run()
        self.add_abandoned_reviewer()
        shot = h.diagnose_dispatch(RID, 'cccccccccc')['evidence']['snapshot_id']
        return h.run_recover_attempt(ns(run_id=RID, dispatch_id='cccccccccc', evidence=shot))

    def dispatch_and_collect(self, role, verdict='pass', status='complete'):
        out = self.recovery_dispatch(role)
        self.write_report(out['dispatch_id'], role, verdict, status)
        h.run_collect(ns(run_id=RID, dispatch_id=out['dispatch_id']))
        return out

    # --- D1 ------------------------------------------------------------------
    def test_group_closure_revokes_every_authorization_of_the_group(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['settings']['orchestration']['max_retries'] = 4
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == first['dispatch_id'])
        rec.update({'status': 'abandoned', 'abandon_reason': 'Recovery failed without a report'})
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, first['dispatch_id'])['evidence']['snapshot_id']
        self.recover(evidence=shot, dispatch_id=first['dispatch_id'])
        second = self.recovery_dispatch()
        self.write_report(second['dispatch_id'], 'developer', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=second['dispatch_id']))
        h.run_gate(ns(run_id=RID, decision='pass',
                      evidence='Second recovery attempt independently reviewed'))
        run = h.require_run(RID)
        self.assertEqual([a['status'] for a in run['recovery_authorizations']],
                         ['revoked', 'revoked'])
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')
        self.assertEqual(h.closure_problems(run, run['fault_groups'][-1]), [])

    def test_two_recovery_chains_can_still_finish(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        run = h.require_run(RID)
        run['stages'] = ['plan_brief', 'implement', 'accept']
        run['stage_definitions']['accept'] = {'actor': 'lead', 'gate': 'auto'}
        run['settings']['orchestration']['max_retries'] = 4
        run['events'] = [{'type': 'gate', 'stage_index': 0, 'decision': 'pass'}]
        h.save(h.run_file(RID), run)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        first = self.recovery_dispatch()
        run = h.require_run(RID)
        rec = next(d for d in run['dispatches'] if d['id'] == first['dispatch_id'])
        rec.update({'status': 'abandoned', 'abandon_reason': 'Recovery failed without a report'})
        h.save(h.run_file(RID), run)
        shot = h.diagnose_dispatch(RID, first['dispatch_id'])['evidence']['snapshot_id']
        self.recover(evidence=shot, dispatch_id=first['dispatch_id'])
        second = self.recovery_dispatch()
        self.write_report(second['dispatch_id'], 'developer', 'pass')
        h.run_collect(ns(run_id=RID, dispatch_id=second['dispatch_id']))
        h.run_gate(ns(run_id=RID, decision='pass', evidence='Second recovery attempt reviewed'))
        h.run_gate(ns(run_id=RID, decision='pass', evidence='Lead accepted the recovered work'))
        self.assertTrue(h.run_finish(ns(run_id=RID))['finished'])

    # --- D2 ------------------------------------------------------------------
    def test_recover_attempt_refuses_a_cancelled_run_before_any_replay(self):
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        self.recover(evidence=snap)
        run = h.require_run(RID)
        run['status'] = 'cancelled'
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'run_terminal')
        self.assertEqual(h.require_run(RID)['status'], 'cancelled')

    def test_recover_attempt_refuses_a_finished_run_before_any_replay(self):
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        self.recover(evidence=snap)
        run = h.require_run(RID)
        run['status'] = 'finished'
        h.save(h.run_file(RID), run)
        with self.assertRaises(h.GuardError) as ctx:
            self.recover(evidence=snap)
        self.assertEqual(ctx.exception.code, 'run_terminal')

    def test_replay_still_works_for_a_live_run(self):
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        first = self.recover(evidence=snap)
        again = self.recover(evidence=snap)
        self.assertTrue(again['replay'])
        self.assertEqual(again['authorization_id'], first['authorization_id'])

    # --- D3 ------------------------------------------------------------------
    def test_t10_pass_refuses_a_blocked_pass_report(self):
        self.recover_both()
        self.dispatch_and_collect('developer', 'pass', 'complete')
        self.dispatch_and_collect('reviewer', 'pass', 'blocked')
        with self.assertRaises(h.Error) as ctx:
            h.run_gate(ns(run_id=RID, decision='pass',
                          evidence='Trying to pass with a business-blocked report'))
        self.assertIn('complete/pass', str(ctx.exception))
        self.assertEqual(h.require_run(RID)['stage_index'], 1)

    def test_t10_pass_refuses_a_needs_work_verdict(self):
        self.recover_both()
        self.dispatch_and_collect('developer', 'pass', 'complete')
        self.dispatch_and_collect('reviewer', 'needs-work', 'complete')
        with self.assertRaises(h.Error) as ctx:
            h.run_gate(ns(run_id=RID, decision='pass',
                          evidence='Trying to pass without a positive verdict'))
        self.assertIn('complete/pass', str(ctx.exception))

    def test_t10_pass_accepts_complete_and_pass_for_every_role(self):
        self.recover_both()
        self.dispatch_and_collect('developer', 'pass', 'complete')
        self.dispatch_and_collect('reviewer', 'pass', 'complete')
        out = h.run_gate(ns(run_id=RID, decision='pass',
                            evidence='Both required roles produced complete pass reports'))
        self.assertEqual(out['status'], 'active')
        run = h.require_run(RID)
        self.assertEqual(run['fault_groups'][-1]['status'], 'closed')
        self.assertTrue(all(a['status'] == 'revoked' for a in run['recovery_authorizations']))

    # --- D4 ------------------------------------------------------------------
    def test_slot_effect_is_not_read_from_a_never_assigned_field(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        auth = h.recovery_authorization_for(run)
        self.assertNotIn('slot_effect', auth)
        h.validate_recovery_authorization(run, auth, role='developer')
        run['recovery_authorizations'][-1]['slot_effect'] = 'read'
        try:
            h.validate_recovery_authorization(run, auth, role='developer')
        except h.GuardError as e:
            self.fail(f'a phantom slot_effect field must be ignored, got {e}')
        self.assertFalse(any('slot_effect' in line for line in
                             open(MODULE).read().splitlines()))


class RealConcurrencyTests(RecoveryM2Base):
    """Genuine multi-process contention on the real fcntl lock. No fixtures faked."""

    def spawn(self, count, argv_hook, tag):
        script = Path(self.tmp.name) / ('worker_%s.py' % tag)
        script.write_text(CONCURRENCY_WORKER)
        # Pin the identity env explicitly: the parent shell may carry a real
        # HERDR_* from whatever pane this suite happens to run in.
        env = dict(os.environ, HTM_STATE_DIR=str(h.STATE), PROC_CWD=self.tmp.name,
                   HERDR_ENV='1', HERDR_WORKSPACE_ID='w1', HERDR_PANE_ID='w1:p1',
                   HERDR_TAB_ID='w1:t1', HERDR_SESSION='', HERDR_SOCKET_PATH='')
        env.setdefault('PROC_RECEIPT', 'rcpt-' + tag)
        procs = []
        for i in range(count):
            env_i = dict(env, PROC_TAG='%s%d' % (tag, i))
            procs.append(subprocess.Popen(
                [sys.executable, '-I', '-B', str(script), str(MODULE)] + argv_hook(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env_i))
        out = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=120)
            out.append((stdout.strip(), stderr, p.returncode))
        return out

    def test_concurrent_recover_attempts_create_exactly_one_attempt(self):
        self.make_run(dispatch_status='settled')
        self.add_abandoned(DID)
        snap = self.diagnose()['evidence']['snapshot_id']
        results = self.spawn(4, lambda: [RID, DID, snap], 'rec')
        parsed = []
        for stdout, stderr, code in results:
            self.assertTrue(stdout, f'worker produced no output; stderr={stderr[-400:]}')
            parsed.append(json.loads(stdout))
        ok = [p for p in parsed if p.get('ok')]
        self.assertGreaterEqual(len(ok), 1, parsed)
        ids = {p['authorization_id'] for p in ok}
        self.assertEqual(len(ids), 1, f'concurrent callers must share one authorization: {parsed}')
        self.assertEqual({p['stage_attempt'] for p in ok}, {2})
        run = h.require_run(RID)
        self.assertEqual(run['stage_attempt'], 2)
        self.assertEqual(len(run['recovery_authorizations']), 1)
        self.assertEqual(len([d for d in run['dispatches'] if d.get('stage_attempt') == 2]), 0)

    def test_concurrent_recovery_dispatch_for_one_role_prompts_at_most_once(self):
        self.add_abandoned(DID)
        self.recover(evidence=self.diagnose()['evidence']['snapshot_id'])
        run = h.require_run(RID)
        bound = (h.recovery_authorization_for(run).get('role_actions') or {})['developer']
        os.environ['PROC_MODE'] = 'dispatch'
        os.environ['PROC_MSG'] = bound['message']
        # The re-verification must see the same receipt identity that was issued.
        os.environ['PROC_RECEIPT'] = h.recovery_authorization_for(run)['receipt']['receipt_id']
        try:
            results = self.spawn(4, lambda: [RID, DID, 'unused'], 'disp')
        finally:
            for key in ('PROC_MODE', 'PROC_MSG', 'PROC_RECEIPT'):
                os.environ.pop(key, None)
        parsed = [json.loads(stdout) for stdout, _err, _code in results if stdout.strip()]
        ok = [p for p in parsed if p.get('ok')]
        self.assertGreaterEqual(len(ok), 1, parsed)
        run = h.require_run(RID)
        new = [d for d in run['dispatches'] if d.get('stage_attempt') == 2]
        self.assertEqual(len(new), 1, 'exactly one dispatch record may exist for the role')
        slot = h.recovery_authorization_for(run)['slots']['developer']
        self.assertEqual(slot['status'], 'consumed')
        self.assertEqual(slot['dispatch_id'], new[0]['id'])

    def test_concurrent_abandon_of_one_dispatch_yields_one_terminal_record(self):
        self.make_run()
        snap = self.diagnose()['evidence']['snapshot_id']
        script = Path(self.tmp.name) / 'abandon_worker.py'
        script.write_text(r"""
import importlib.util, json, os, sys, types
spec = importlib.util.spec_from_file_location('htm', sys.argv[1])
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
h.pane_of_current_process = lambda *a, **kw: {
    'session': os.environ.get('HERDR_SESSION') or 'default',
    'workspace': os.environ.get('HERDR_WORKSPACE_ID', ''),
    'tab': os.environ.get('HERDR_TAB_ID', ''),
    'pane': os.environ.get('HERDR_PANE_ID', '')}
args = types.SimpleNamespace(run_id=sys.argv[2], dispatch_id=sys.argv[3],
                             reason='Worker produced no verifiable report',
                             evidence=sys.argv[4], confirm_untracked=True,
                             quarantine_invalid=False)
try:
    h.run_abandon(args)
    print(json.dumps({'ok': True}))
except h.Error as e:
    print(json.dumps({'ok': False, 'error': str(e)}))
""")
        # Pin the identity env explicitly: the parent shell may carry a real
        # HERDR_* from whatever pane this suite happens to run in.
        env = dict(os.environ, HTM_STATE_DIR=str(h.STATE), PROC_CWD=self.tmp.name,
                   HERDR_ENV='1', HERDR_WORKSPACE_ID='w1', HERDR_PANE_ID='w1:p1',
                   HERDR_TAB_ID='w1:t1', HERDR_SESSION='', HERDR_SOCKET_PATH='')
        procs = [subprocess.Popen([sys.executable, '-I', '-B', str(script), str(MODULE),
                                   RID, DID, snap], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(4)]
        outs = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=120)
            outs.append(json.loads(stdout) if stdout.strip() else {'ok': False, 'error': stderr})
        self.assertEqual(len([o for o in outs if o['ok']]), 1, outs)
        run = h.require_run(RID)
        self.assertEqual(run['dispatches'][0]['status'], 'abandoned')
        self.assertEqual(len([e for e in run['events'] if e.get('type') == 'abandon']), 1)


class AsyncFinalizeTests(RecoveryM2Base):
    def finalize_run(self, decide_during_wait):
        self.make_run(dispatch_status='settled')

        def prompt(*argv, **kw):
            if argv[:2] == ('agent', 'prompt'):
                decide_during_wait()
                return {'agent': dict(self.agent)}
            return self.base_herdr(*argv, **kw)

        with patch.object(h, 'herdr', side_effect=prompt):
            return h.run_dispatch(ns(run_id=RID, role='developer', message='run the task',
                                     effect='write', remediation=False, no_wait=False,
                                     wait_timeout_seconds=5))

    def test_wait_return_cannot_overwrite_an_abandoned_dispatch(self):
        def abandon_meanwhile():
            run = h.require_run(RID)
            run['dispatches'][-1].update({'status': 'abandoned', 'root_attempt': 1})
            h.save(h.run_file(RID), run)

        self.make_run(dispatch_status='settled')
        before = len(h.require_run(RID)['dispatches'])
        out = self.finalize_run(abandon_meanwhile)
        self.assertEqual(out['status'], 'terminal')
        run = h.require_run(RID)
        self.assertEqual(len(run['dispatches']), before + 1)
        self.assertEqual(run['dispatches'][-1]['status'], 'abandoned')
        self.assertNotIn('report', run['dispatches'][-1])

    def test_wait_return_cannot_settle_a_moved_attempt(self):
        def move_attempt():
            run = h.require_run(RID)
            run['stage_attempt'] = 7
            h.save(h.run_file(RID), run)

        out = self.finalize_run(move_attempt)
        self.assertEqual(out['status'], 'terminal')
        self.assertEqual(h.require_run(RID)['dispatches'][-1]['status'], 'submission_uncertain')

    def test_exception_finalize_never_revives_a_terminal_dispatch(self):
        def decide():
            run = h.require_run(RID)
            run['dispatches'][-1].update({'status': 'settled', 'report': {'verdict': 'pass'},
                                          'artifact_sha256': 'a' * 64})
            h.save(h.run_file(RID), run)

        self.make_run(dispatch_status='settled')
        broken = lambda *argv, **kw: (decide(), (_ for _ in ()).throw(h.Error('boom')))
        with patch.object(h, 'herdr', side_effect=broken):
            with self.assertRaises(h.Error):
                h.run_dispatch(ns(run_id=RID, role='developer', message='run the task',
                                  effect='write', remediation=False, no_wait=False,
                                  wait_timeout_seconds=5))
        rec = h.require_run(RID)['dispatches'][-1]
        self.assertEqual(rec['status'], 'settled')
        self.assertEqual(rec['report'], {'verdict': 'pass'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
