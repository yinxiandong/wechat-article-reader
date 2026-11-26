import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import review_workspace as rw
import scheduled_evolution as cloud
import model_client as transport
import check_publication

RUBRIC = rw.load(ROOT / 'assets/scoring-rubric.json')
SEEDS = rw.load(ROOT / 'assets/eval-seed.json')


class CloudTests(unittest.TestCase):
    def feedback(self):
        return {'schema_version': 1, 'base_profile_sha256': rw.digest(rw.initial_state(RUBRIC)['profiles'][-1]),
                'cases': [{'id': 'case-0123456789abcdef', 'article_type': 'opinion',
                           'scores': {d['id']: 3 for d in RUBRIC['dimensions']},
                           'lesson': 'Distinguish conceptual examples from measured experiments.'}]}

    def root(self, tmp):
        root = Path(tmp)
        (root / 'assets').mkdir()
        for name in ('scoring-rubric.json', 'eval-seed.json'):
            (root / 'assets' / name).write_bytes((ROOT / 'assets' / name).read_bytes())
        (root / 'assets/published-profiles.json').write_text(json.dumps(rw.initial_state(RUBRIC)['profiles']), encoding='utf-8')
        return root

    def client(self, bad_baseline=True):
        calls = []
        def invoke(instruction, data):
            calls.append(data)
            if 'calibration_cases' in data:
                return {'changes': [{'guidance': 'Credit conceptual value without claiming measured experiments.',
                                     'reason': 'Correct the category mismatch.',
                                     'evidence_review_ids': ['case-0123456789abcdef']}]}
            self.assertNotIn('expected_scores', json.dumps(data))
            self.assertNotIn('calibration_cases', data)
            case = next(c for c in SEEDS['cases'] if c['id'] == data['article']['id'])
            bad = bad_baseline and data['profile']['version'] == 1
            return {'scores': {key: ((5 if high < 5 else 0) if bad else low)
                               for key, (low, high) in case['expected_scores'].items()},
                    'observations': {} if bad else {key: 'Synthetic test observation' for key in case['required_observations']},
                    'rationale': 'Synthetic fixture, not evidence of real model improvement'}
        return invoke, calls

    def test_current_publication_is_valid(self):
        self.assertTrue(check_publication.check(ROOT)['valid'])
        profiles = rw.published_profiles(RUBRIC)
        self.assertGreaterEqual(len(profiles), 1)
        report_path = ROOT / 'assets/evolution-report.json'
        if report_path.exists():
            report = rw.load(report_path)
            self.assertEqual(report['candidate_profile_sha256'], rw.digest(profiles[-1]))
            self.assertEqual(report['base_profile_sha256'], profiles[-1]['parent'])
            self.assertEqual(report['seed_sha256'], rw.digest(SEEDS))
            self.assertGreater(report['improvement_rate'], .5)
            self.assertFalse(report['regressions'])
            self.assertFalse(any(report['after_errors'].values()))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(rw.main(['--workspace', tmp, 'init']), 0)
            state = rw.load(Path(tmp) / 'review-state.json')
            self.assertEqual(state['profiles'], profiles)

    def test_missing_configuration_no_model_call(self):
        with patch.object(cloud, 'ModelClient') as client:
            result = cloud.scheduled_run({})
        self.assertEqual(result['reason'], 'not_configured')
        client.assert_not_called()

    def test_gate_success_publishes_only_profile_and_numeric_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            client, calls = self.client()
            result = cloud.evolve(self.feedback(), client, root)
            self.assertTrue(result['changed'])
            self.assertEqual(len(calls), 15)
            profiles = rw.validate_published_profiles(rw.load(root / 'assets/published-profiles.json'), RUBRIC)
            self.assertEqual(len(profiles), 2)
            report = (root / 'assets/evolution-report.json').read_text()
            for private in ('case-0123456789abcdef', 'Correct the category mismatch', 'Synthetic test observation', 'rationale', 'calibration_cases'):
                self.assertNotIn(private, report)
            self.assertEqual(set(p.name for p in (root / 'assets').iterdir()),
                             {'scoring-rubric.json', 'eval-seed.json', 'published-profiles.json', 'evolution-report.json'})

    def test_perfect_baseline_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            before = (root / 'assets/published-profiles.json').read_bytes()
            result = cloud.evolve(self.feedback(), self.client(False)[0], root)
            self.assertEqual(result['reason'], 'regression_gate_rejected')
            self.assertEqual(before, (root / 'assets/published-profiles.json').read_bytes())
            self.assertFalse((root / 'assets/evolution-report.json').exists())

    def test_stale_no_change_and_invalid_evidence_do_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            feedback = self.feedback()
            feedback['base_profile_sha256'] = '0' * 64
            with patch.object(cloud, 'ModelClient') as client:
                result = cloud.evolve(feedback, client, root)
                self.assertEqual(result['reason'], 'feedback_profile_stale')
                client.assert_not_called()
            self.assertEqual(cloud.evolve(self.feedback(), lambda *a: {'changes': []}, root)['reason'], 'no_supported_changes')
            with self.assertRaises(ValueError):
                cloud.evolve(self.feedback(), lambda *a: {'changes': [{'guidance': 'Valid guidance',
                             'reason': 'Valid reason', 'evidence_review_ids': ['fake']}]}, root)

    def test_feedback_whitelist_and_redaction(self):
        for lesson in ('https://private.example/a', 'person@example.com', 'C:\\Users\\private', '13812345678', 'sk-abcdefghijklmnop'):
            data = self.feedback()
            data['cases'][0]['lesson'] = lesson
            with self.assertRaises(ValueError):
                cloud.validate_feedback(data)
        data = self.feedback()
        data['cases'][0]['source'] = 'private'
        with self.assertRaises(ValueError):
            cloud.validate_feedback(data)

    def test_export_excludes_private_fields_and_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = rw.initial_state(RUBRIC)
            state['reviews'] = [{'id': 'private-review-id', 'article': {'title': 'PRIVATE TITLE', 'source': 'https://private'},
                                 'review': {'article_type': 'opinion', 'scores': self.feedback()['cases'][0]['scores'],
                                            'evaluation': 'PRIVATE EVALUATION'}}]
            (root / 'review-state.json').write_text(json.dumps(state), encoding='utf-8')
            out = root / 'feedback.json'
            lessons = {'private-review-id': 'Only a conceptual calibration lesson.'}
            with self.assertRaises(ValueError):
                cloud.export_feedback(root, lessons, out, False)
            cloud.export_feedback(root, lessons, out, True)
            text = out.read_text()
            for private in ('PRIVATE', 'https://', 'private-review-id', 'evaluation', 'source'):
                self.assertNotIn(private, text)
            with self.assertRaises(FileExistsError):
                cloud.export_feedback(root, lessons, out, True)

    def test_profile_sync_preserves_reviews_and_rejects_divergence(self):
        state = rw.initial_state(RUBRIC)
        state['reviews'] = [{'id': 'historical', 'profile_sha256': rw.digest(state['profiles'][0])}]
        profiles = copy.deepcopy(state['profiles'])
        profiles.append({'version': 2, 'parent': rw.digest(profiles[0]), 'guidance': ['Bounded correction.'],
                         'rubric_sha256': rw.digest(RUBRIC)})
        original = copy.deepcopy(state['reviews'])
        self.assertEqual(rw.sync_profiles(state, profiles)['added_profiles'], 1)
        self.assertEqual(original, state['reviews'])
        self.assertEqual(rw.sync_profiles(state, profiles)['added_profiles'], 0)
        state['profiles'][-1]['guidance'] = ['Local divergence']
        with self.assertRaises(ValueError):
            rw.sync_profiles(state, profiles)

    def test_open_reflection_and_tampered_profile_block_sync(self):
        state = rw.initial_state(RUBRIC)
        profiles = copy.deepcopy(state['profiles'])
        profiles.append({'version': 2, 'parent': rw.digest(profiles[0]), 'guidance': ['Correction'],
                         'rubric_sha256': rw.digest(RUBRIC)})
        rw.reflect(state, 'manual', SEEDS)
        with self.assertRaises(ValueError):
            rw.sync_profiles(state, profiles)
        profiles[-1]['parent'] = 'tampered'
        with self.assertRaises(ValueError):
            rw.validate_published_profiles(profiles, RUBRIC)

    def test_model_http_error_does_not_leak_provider_body(self):
        import urllib.error
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://api.openai.com', 400, 'PRIVATE', {}, None)
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError) as caught:
                cloud.ModelClient('test-secret', 'test-model')('instruction', {})
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('test-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
