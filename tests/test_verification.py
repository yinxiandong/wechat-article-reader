import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evolution_core as core
import model_client as transport
import review_workspace as rw
import scheduled_evolution as cloud
import test_cloud as fixtures


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CloudTests()

    def publication(self, tmp):
        root = self.fixture.root(tmp)
        cloud.evolve(self.fixture.feedback(), self.fixture.client()[0], root)
        return root, rw.load(root / 'assets/published-profiles.json'), rw.load(root / 'assets/evolution-report.json')

    def test_preparation_is_reproducible_and_inputs_are_unchanged(self):
        rubric = rw.load(ROOT / 'assets/scoring-rubric.json')
        profiles = rw.initial_state(rubric)['profiles']
        seeds = rw.load(ROOT / 'assets/eval-seed.json')
        feedback = self.fixture.feedback()
        changes = self.fixture.client()[0]('', {'calibration_cases': feedback['cases']})['changes']
        before = copy.deepcopy([rubric, profiles, seeds, feedback, changes])
        with patch.object(rw, 'now', side_effect=AssertionError('clock used')):
            first = core.prepare_candidate(rubric, profiles, seeds, feedback, changes)
            second = core.prepare_candidate(rubric, profiles, seeds, feedback, changes)
        self.assertEqual(first, second)
        self.assertEqual(before, [rubric, profiles, seeds, feedback, changes])
        first[0]['profiles'][0]['guidance'].append('Mutated returned state')
        self.assertEqual(profiles[0]['guidance'], [])

    def test_report_counts_rates_errors_and_scores_are_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, profiles, report = self.publication(tmp)
            rubric = rw.load(root / 'assets/scoring-rubric.json')
            seeds = rw.load(root / 'assets/eval-seed.json')
            self.assertTrue(core.validate_publication(rubric, profiles, seeds, report)['valid'])
            mutations = ['rate', 'count', 'errors', 'scores', 'duplicate_case', 'duplicate_label', 'missing_case', 'seed_hash', 'profile_hash', 'extra_private_field']
            for name in mutations:
                value = copy.deepcopy(report)
                if name == 'rate':
                    value['improvement_rate'] = .99
                elif name == 'count':
                    value['evaluation_summary']['case_count'] = 700
                elif name == 'errors':
                    value['before_errors']['tutorial-01'] = 0
                elif name == 'scores':
                    value['assessments']['after'][0]['scores']['practical_value'] = 0
                elif name == 'duplicate_case':
                    value['assessments']['after'][0] = copy.deepcopy(value['assessments']['after'][1])
                elif name == 'duplicate_label':
                    labels = value['assessments']['after'][0]['observation_labels']
                    labels.append(labels[0])
                elif name == 'missing_case':
                    value['assessments']['after'].pop()
                elif name == 'seed_hash':
                    value['seed_sha256'] = '0' * 64
                elif name == 'profile_hash':
                    value['candidate_profile_sha256'] = '0' * 64
                else:
                    value['raw_feedback'] = 'PRIVATE'
                with self.subTest(name=name), self.assertRaises(ValueError):
                    core.validate_publication(rubric, profiles, seeds, value)

    def test_internally_consistent_but_non_improving_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, profiles, report = self.publication(tmp)
            report['assessments']['before'] = copy.deepcopy(report['assessments']['after'])
            report['before_errors'] = copy.deepcopy(report['after_errors'])
            report['evaluation_summary'] = core.evaluation_summary(report['before_errors'], report['after_errors'])
            report['improvement_rate'] = 0
            with self.assertRaises(ValueError):
                core.validate_publication(rw.load(root / 'assets/scoring-rubric.json'), profiles,
                                          rw.load(root / 'assets/eval-seed.json'), report)

    def test_baseline_has_no_report_and_evolved_profiles_require_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, profiles, report = self.publication(tmp)
            rubric = rw.load(root / 'assets/scoring-rubric.json')
            seeds = rw.load(root / 'assets/eval-seed.json')
            with self.assertRaises(ValueError):
                core.validate_publication(rubric, profiles, seeds, None)
            with self.assertRaises(ValueError):
                core.validate_publication(rubric, profiles[:1], seeds, report)

    def test_invalid_assessment_stops_immediately_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fixture.root(tmp)
            before = (root / 'assets/published-profiles.json').read_bytes()
            base, calls = self.fixture.client()
            def invoke(instruction, data):
                if 'article' in data:
                    calls.append(data)
                    return {'scores': {}, 'observations': {}, 'rationale': 'Invalid response'}
                return base(instruction, data)
            with self.assertRaises(ValueError):
                cloud.evolve(self.fixture.feedback(), invoke, root)
            self.assertEqual(len(calls), 2)
            self.assertEqual(before, (root / 'assets/published-profiles.json').read_bytes())
            self.assertFalse((root / 'assets/evolution-report.json').exists())

    def test_runtime_failure_returns_metrics_and_failed_status(self):
        client = Mock()
        client.metrics.return_value = {'attempted_calls': 1, 'failed_calls': 1}
        env = {'OPENAI_API_KEY': 'test', 'EVOLUTION_MODEL': 'test-model',
               'EVOLUTION_FEEDBACK_JSON': json.dumps(self.fixture.feedback())}
        with patch.object(cloud, 'ModelClient', return_value=client), patch.object(cloud, 'evolve', side_effect=ValueError('PRIVATE DETAIL')):
            result = cloud.scheduled_run(env)
        self.assertFalse(result['changed'])
        self.assertTrue(result['failed'])
        self.assertEqual(result['runtime_metrics']['failed_calls'], 1)
        self.assertNotIn('PRIVATE DETAIL', json.dumps(result))
        with patch.object(cloud, 'scheduled_run', return_value=result), patch.dict('os.environ', {}, clear=True), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(cloud.main(['run']), 2)


class TransportTests(unittest.TestCase):
    def response(self, usage=True, finish='stop'):
        body = {'choices': [{'finish_reason': finish, 'message': {'content': '{"answer": 1}'}}]}
        if usage:
            body['usage'] = {'prompt_tokens': 100, 'completion_tokens': 20}
        response = Mock()
        response.read.return_value = json.dumps(body).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_usage_and_latency_are_recorded_from_provider_response(self):
        opener = Mock()
        opener.open.return_value = self.response()
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener), patch.object(transport.time, 'monotonic', side_effect=[10, 12]):
            client = transport.ModelClient('test-secret', 'test-model')
            self.assertEqual(client('instruction', {}), {'answer': 1})
        metrics = client.metrics()
        self.assertEqual(metrics['attempted_calls'], 1)
        self.assertEqual(metrics['completed_calls'], 1)
        self.assertEqual(metrics['failed_calls'], 0)
        self.assertEqual(metrics['reported_prompt_tokens'], 100)
        self.assertEqual(metrics['reported_completion_tokens'], 20)
        self.assertEqual(metrics['usage_missing_calls'], 0)
        self.assertEqual(metrics['request_seconds'], {'total': 2, 'mean': 2, 'median': 2, 'max': 2})
        self.assertNotIn('test-secret', json.dumps(metrics))

    def test_missing_usage_is_explicit_not_claimed_zero_cost(self):
        opener = Mock()
        opener.open.return_value = self.response(usage=False)
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener):
            client = transport.ModelClient('test-secret', 'test-model')
            client('instruction', {})
        self.assertEqual(client.metrics()['usage_reported_calls'], 0)
        self.assertEqual(client.metrics()['usage_missing_calls'], 1)

    def test_failed_request_is_counted_with_elapsed_time_without_retry(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('PRIVATE ERROR')
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener), patch.object(transport.time, 'monotonic', side_effect=[0, 60]):
            client = transport.ModelClient('test-secret', 'test-model')
            with self.assertRaises(ValueError) as exc:
                client('instruction', {})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(client.metrics()['failed_calls'], 1)
        self.assertEqual(client.metrics()['request_seconds']['total'], 60)
        self.assertNotIn('PRIVATE', str(exc.exception))

    def test_truncated_response_counts_known_usage_as_failure(self):
        opener = Mock()
        opener.open.return_value = self.response(finish='length')
        with patch.object(transport.urllib.request, 'build_opener', return_value=opener):
            client = transport.ModelClient('test-secret', 'test-model')
            with self.assertRaises(ValueError):
                client('instruction', {})
        self.assertEqual(client.metrics()['failed_calls'], 1)
        self.assertEqual(client.metrics()['usage_reported_calls'], 1)
        self.assertEqual(client.metrics()['reported_completion_tokens'], 20)

    def test_correctness_summary_counts_cases_and_rejects_boolean_errors(self):
        before = {'a': 4, 'b': 0}
        after = {'a': 0, 'b': 0}
        self.assertEqual(core.evaluation_summary(before, after),
                         {'case_count': 2, 'before': {'passed_cases': 1, 'failed_cases': 1, 'pass_rate': .5, 'total_error': 4},
                          'after': {'passed_cases': 2, 'failed_cases': 0, 'pass_rate': 1, 'total_error': 0},
                          'improved_cases': 1, 'unchanged_cases': 1, 'regressed_cases': 0})
        with self.assertRaises(ValueError):
            core.evaluation_summary({'a': True}, {'a': 0})


if __name__ == '__main__':
    unittest.main()
