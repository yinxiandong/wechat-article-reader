import copy
import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('reviews', ROOT / 'scripts/review_workspace.py')
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)
RUBRIC = reviewer.load(ROOT / 'assets/scoring-rubric.json')
SEEDS = reviewer.load(ROOT / 'assets/eval-seed.json')


def article_review(state, suffix=''):
    article = {'title': '测试文章' + suffix, 'text': '完整正文明确条件，并提供有限范围实测。' + suffix, 'source': None}
    review = {'article_type': 'opinion', 'coverage': 'full_text_reviewed',
              'profile_sha256': reviewer.digest(state['profiles'][-1]),
              'article_text_sha256': __import__('hashlib').sha256(article['text'].encode()).hexdigest(),
              'summary': '概括', 'evaluation': '评价', 'takeaways': '收获', 'verdict': '判断',
              'verification_limits': '外部未核验',
              'scores': {d['id']: 3 for d in RUBRIC['dimensions']},
              'reasons': {d['id']: '文中给出条件，但核验范围有限' for d in RUBRIC['dimensions']},
              'evidence': {d['id']: ['完整正文明确条件'] for d in RUBRIC['dimensions']}}
    return article, review


def seed_rows(bad=False):
    rows = []
    for case in SEEDS['cases']:
        rows.append({'case_id': case['id'], 'case_sha256': reviewer.digest(case),
                     'scores': {key: (5 if high < 5 else 0) if bad else low
                                for key, (low, high) in case['expected_scores'].items()},
                     'observations': {} if bad else {key: '对应例子中的具体问题' for key in case['required_observations']},
                     'rationale': '虚构测试输入，用于验证门禁而非模型效果'})
    return rows


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self._output = contextlib.ExitStack()
        self._output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self._output.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(self._output.close)
        self.state = reviewer.initial_state(copy.deepcopy(RUBRIC))

    def proposal(self):
        a, r = article_review(self.state)
        item = reviewer.record(self.state, a, r)
        reflection = reviewer.reflect(self.state, 'manual', copy.deepcopy(SEEDS))
        changes = [{'guidance': '先判断文章类型，明确核验范围。', 'reason': '本例需避免误用教程标准',
                    'evidence_review_ids': [item['review_id']]}]
        proposal = reviewer.prepare(self.state, reflection['id'], changes)
        proposal['evaluations']['before'] = seed_rows(bad=True)
        proposal['evaluations']['after'] = seed_rows()
        return proposal

    def test_original_scale_and_anchors(self):
        self.assertEqual(len(RUBRIC['dimensions']), 5)
        self.assertEqual(RUBRIC['scale']['total'], 25)
        self.assertTrue(all(len(x['anchors']) == 6 for x in RUBRIC['dimensions']))

    def test_record_idempotence_and_evidence(self):
        a, r = article_review(self.state)
        first = reviewer.record(self.state, a, r)
        self.assertEqual(first['total'], 15)
        self.assertTrue(reviewer.record(self.state, a, r)['duplicate'])
        self.assertEqual(len(self.state['reviews']), 1)
        changed = copy.deepcopy(r)
        changed['scores']['credibility'] = 4
        with self.assertRaises(ValueError):
            reviewer.record(self.state, a, changed)
        changed = copy.deepcopy(r)
        changed['evidence']['credibility'] = ['文章不存在的具体原句']
        with self.assertRaises(ValueError):
            reviewer.record(self.state, a, changed)
        self.assertEqual(len(self.state['reviews']), 1)

    def test_stale_profile_or_article_cannot_record(self):
        for key in ('profile_sha256', 'article_text_sha256'):
            a, r = article_review(self.state)
            r[key] = 'bad'
            with self.assertRaises(ValueError):
                reviewer.record(self.state, a, r)
        self.assertFalse(self.state['reviews'])

    def test_scores_reject_booleans_out_of_range_and_missing(self):
        for value in (True, 6, -1, 2.5):
            a, r = article_review(self.state)
            r['scores']['credibility'] = value
            with self.assertRaises(ValueError):
                reviewer.record(self.state, a, r)
        a, r = article_review(self.state)
        del r['scores']['credibility']
        with self.assertRaises(ValueError):
            reviewer.record(self.state, a, r)

    def test_partial_is_provisional(self):
        a, r = article_review(self.state)
        r['coverage'] = 'partial'
        reviewer.record(self.state, a, r)
        self.assertEqual(self.state['reviews'][0]['score_status'], 'provisional')

    def test_count_and_weekly_triggers(self):
        with self.assertRaises(ValueError):
            reviewer.reflect(self.state, 'count', SEEDS)
        for number in range(5):
            a, r = article_review(self.state, str(number))
            reviewer.record(self.state, a, r)
        self.assertTrue(reviewer.status(self.state)['count_due'])
        reflection = reviewer.reflect(self.state, 'count', SEEDS)
        self.assertEqual(reflection['through'], 5)
        self.assertEqual(reviewer.reflect(self.state, 'manual', SEEDS)['id'], reflection['id'])
        self.state['created_at'] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.assertTrue(reviewer.status(self.state)['weekly_due'])

    def test_gate_passes_synthetic_improvement_only(self):
        result = reviewer.gate(self.state, self.proposal())
        self.assertTrue(result['passed'])
        self.assertEqual(result['improvement_rate'], 1)
        self.assertEqual(len(self.state['profiles']), 1)  # gate is read-only

    def test_perfect_baseline_no_improvement_fails(self):
        proposal = self.proposal()
        proposal['evaluations']['before'] = seed_rows()
        self.assertFalse(reviewer.gate(self.state, proposal)['passed'])

    def test_any_regression_fails_even_with_majority_improvement(self):
        proposal = self.proposal()
        proposal['evaluations']['before'][0] = seed_rows()[0]
        proposal['evaluations']['after'][0] = seed_rows(bad=True)[0]
        result = reviewer.gate(self.state, proposal)
        self.assertFalse(result['passed'])
        self.assertTrue(result['regressions'])

    def test_gate_rejects_stale_tampered_or_unfounded_changes(self):
        proposal = self.proposal()
        for mutation in ('base', 'seed', 'evidence', 'candidate'):
            p = copy.deepcopy(proposal)
            if mutation == 'base':
                p['base_profile_sha256'] = 'wrong'
            elif mutation == 'seed':
                p['evaluations']['after'][0]['case_sha256'] = 'wrong'
            elif mutation == 'candidate':
                p['evaluations']['after_profile_sha256'] = 'wrong'
            else:
                p['changes'][0]['evidence_review_ids'] = ['fake']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                reviewer.gate(self.state, p)

    def test_transaction_rolls_back_and_lock_excludes_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'review-state.json'
            path.write_text(json.dumps(self.state), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                with reviewer.transaction(root) as (state, _):
                    state['reviews'].append({'broken': True})
                    raise ValueError('interrupted')
            self.assertEqual(before, path.read_bytes())
            (root / '.review.lock').write_text('9999')
            with self.assertRaises(ValueError):
                with reviewer.transaction(root):
                    pass
            (root / '.review.lock').unlink()

    def test_whitespace_is_not_evidence(self):
        a, r = article_review(self.state)
        r['evidence']['credibility'] = ['    ']
        with self.assertRaises(ValueError):
            reviewer.record(self.state, a, r)
        for item in ([], None, 'not an object'):
            with self.assertRaises(ValueError):
                reviewer.record(self.state, item, r)

    def test_minority_improvement_and_missing_cases_fail(self):
        proposal = self.proposal()
        proposal['evaluations']['before'] = seed_rows()
        proposal['evaluations']['before'][:3] = seed_rows(bad=True)[:3]
        result = reviewer.gate(self.state, proposal)
        self.assertFalse(result['passed'])
        self.assertLess(result['improvement_rate'], .5)
        proposal['evaluations']['after'].pop()
        with self.assertRaises(ValueError):
            reviewer.gate(self.state, proposal)

    def test_weekly_reflection_and_manual_no_change_close(self):
        self.state['created_at'] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        reflection = reviewer.reflect(self.state, 'weekly', SEEDS)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'review-state.json').write_text(json.dumps(self.state), encoding='utf-8')
            self.assertEqual(reviewer.main(['--workspace', str(root), 'close-reflection',
                                          '--reflection-id', reflection['id'], '--reason', 'No supported changes']), 0)
            saved = reviewer.load(root / 'review-state.json')
            self.assertFalse(reviewer.status(saved)['weekly_due'])
            self.assertEqual(len(saved['profiles']), 1)
            self.assertEqual(saved['reflections'][0]['status'], 'no_change')

    def test_prepare_and_show_write_utf8_exclusively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal = self.proposal()
            (root / 'review-state.json').write_text(json.dumps(self.state), encoding='utf-8')
            changes = root / 'changes.json'
            changes.write_text(json.dumps(proposal['changes']), encoding='utf-8')
            path = root / 'proposal.json'
            argv = ['--workspace', str(root), 'prepare', '--reflection-id', proposal['reflection_id'],
                    '--changes', str(changes), '--output', str(path)]
            self.assertEqual(reviewer.main(argv), 0)
            self.assertEqual(reviewer.load(path)['base_profile_sha256'], proposal['base_profile_sha256'])
            self.assertEqual(reviewer.main(argv), 2)
            output = root / 'review.md'
            self.assertEqual(reviewer.main(['--workspace', str(root), 'show', '--review-id',
                                          self.state['reviews'][0]['id'], '--output', str(output)]), 0)
            self.assertIn('文章评分：15/25', output.read_text(encoding='utf-8'))

    def test_cli_apply_keeps_history_and_newer_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal = self.proposal()
            a, r = article_review(self.state, 'after-reflection')
            reviewer.record(self.state, a, r)
            (root / 'review-state.json').write_text(json.dumps(self.state), encoding='utf-8')
            path = root / 'proposal.json'
            path.write_text(json.dumps(proposal), encoding='utf-8')
            argv = ['--workspace', str(root), 'apply', '--proposal', str(path)]
            self.assertEqual(reviewer.main(argv), 0)
            saved = reviewer.load(root / 'review-state.json')
            self.assertEqual(len(saved['profiles']), 2)
            self.assertEqual(len(saved['reviews']), 2)
            self.assertEqual(reviewer.status(saved)['since_last_reflect'], 1)
            self.assertEqual(saved['profiles'][0]['version'], 1)
            self.assertEqual(reviewer.main(argv), 2)  # no repeated application

    def test_cli_init_idempotent_and_failed_apply_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(reviewer.main(['--workspace', str(root), 'init']), 0)
            first = reviewer.load(root / 'review-state.json')
            self.assertEqual(reviewer.main(['--workspace', str(root), 'init']), 0)
            self.assertEqual(first, reviewer.load(root / 'review-state.json'))
            proposal = self.proposal()
            proposal['evaluations']['before'] = seed_rows()
            (root / 'review-state.json').write_text(json.dumps(self.state), encoding='utf-8')
            baseline = (root / 'review-state.json').read_bytes()
            path = root / 'proposal.json'
            path.write_text(json.dumps(proposal), encoding='utf-8')
            self.assertEqual(reviewer.main(['--workspace', str(root), 'apply', '--proposal', str(path)]), 2)
            self.assertEqual(baseline, (root / 'review-state.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
