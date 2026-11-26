"""Deterministic scoring evolution decisions; all inputs are explicit, no network or file writes."""
import copy

import review_workspace as rw


def prepare_candidate(rubric, profiles, seeds, feedback, changes):
    """Build a reproducible gate context without a clock, UUID or global workspace."""
    rw.validate_published_profiles(profiles, rubric)
    base = profiles[-1]
    if feedback['base_profile_sha256'] != rw.digest(base):
        raise ValueError('stale feedback')
    candidate = {'version': base['version'] + 1, 'parent': rw.digest(base),
                 'guidance': base['guidance'] + [x['guidance'] for x in changes],
                 'rubric_sha256': rw.digest(rubric)}
    reflection_id = rw.digest({'base': base, 'feedback': feedback, 'seeds': seeds})[:32]
    reflection = {'id': reflection_id, 'status': 'open', 'base_profile_sha256': rw.digest(base),
                  'review_ids': [c['id'] for c in feedback['cases']],
                  'evaluation_seed': copy.deepcopy(seeds), 'seed_sha256': rw.digest(seeds)}
    state = {'rubric': copy.deepcopy(rubric), 'profiles': copy.deepcopy(profiles),
             'reflections': [reflection]}
    proposal = rw.prepare(state, reflection_id, copy.deepcopy(changes))
    return state, proposal, candidate


def evaluation_summary(before, after):
    if not isinstance(before, dict) or not before or set(before) != set(after):
        raise ValueError('evaluation case sets differ or are empty')
    if any(type(value) is not int or value < 0 for row in (before, after) for value in row.values()):
        raise ValueError('errors must be non-negative integers')
    def summarize(errors):
        passed = sum(value == 0 for value in errors.values())
        return {'passed_cases': passed, 'failed_cases': len(errors) - passed,
                'pass_rate': passed / len(errors), 'total_error': sum(errors.values())}
    return {'case_count': len(before), 'before': summarize(before), 'after': summarize(after),
            'improved_cases': sum(after[k] < before[k] for k in before),
            'unchanged_cases': sum(after[k] == before[k] for k in before),
            'regressed_cases': sum(after[k] > before[k] for k in before)}


def build_report(result, proposal, profiles, seeds, feedback, model, created_at):
    before = proposal['evaluations']['before']
    after = proposal['evaluations']['after']
    return {'schema_version': 2, 'created_at': created_at, 'model': model,
            'base_profile_sha256': rw.digest(profiles[-1]),
            'candidate_profile_sha256': result['candidate_profile_sha256'],
            'feedback_sha256': rw.digest(feedback), 'seed_sha256': rw.digest(seeds),
            'proposal_sha256': result['proposal_sha256'],
            'improvement_rate': result['improvement_rate'], 'regressions': result['regressions'],
            'before_errors': result['before_errors'], 'after_errors': result['after_errors'],
            'evaluation_summary': evaluation_summary(result['before_errors'], result['after_errors']),
            'assessments': {name: [{'case_id': r['case_id'], 'scores': r['scores'],
                                   'observation_labels': sorted(r['observations'])} for r in rows]
                            for name, rows in (('before', before), ('after', after))},
            'limitation': 'Same-model synthetic assessments; PR requires semantic review. Not proof of real-world improvement.'}


def validate_publication(rubric, profiles, seeds, report):
    """Recompute every public number from the fixed seeds; never trust a reported pass."""
    rw.validate_published_profiles(profiles, rubric)
    if len(profiles) == 1:
        if report is not None:
            raise ValueError('baseline cannot have an evolution report')
        return {'valid': True, 'profile_version': 1, 'reason': 'original_baseline'}
    if not isinstance(report, dict) or report.get('schema_version') != 2:
        raise ValueError('evolved profiles require a version 2 public report')
    allowed = {'schema_version', 'created_at', 'model', 'base_profile_sha256', 'candidate_profile_sha256',
               'feedback_sha256', 'seed_sha256', 'proposal_sha256', 'improvement_rate', 'regressions',
               'before_errors', 'after_errors', 'evaluation_summary', 'assessments', 'limitation'}
    if set(report) != allowed:
        raise ValueError('unexpected public report fields')
    if (report['candidate_profile_sha256'] != rw.digest(profiles[-1])
            or report['base_profile_sha256'] != rw.digest(profiles[-2])
            or report['seed_sha256'] != rw.digest(seeds)):
        raise ValueError('public report is stale or refers to different seeds')
    if not isinstance(report['assessments'], dict) or set(report['assessments']) != {'before', 'after'}:
        raise ValueError('report must contain both assessments')
    cases = {c['id']: c for c in seeds['cases']}
    errors = {}
    for name in ('before', 'after'):
        public = report['assessments'][name]
        if not isinstance(public, list):
            raise ValueError('assessment rows must be a list')
        rows = []
        for row in public:
            if not isinstance(row, dict) or set(row) != {'case_id', 'scores', 'observation_labels'}:
                raise ValueError('unexpected assessment fields')
            labels = row['observation_labels']
            if not isinstance(labels, list) or any(not isinstance(x, str) for x in labels) or len(labels) != len(set(labels)):
                raise ValueError('invalid or duplicate observation labels')
            case = cases.get(row['case_id'])
            if not case:
                raise ValueError('unknown synthetic case')
            rows.append({'case_id': case['id'], 'case_sha256': rw.digest(case), 'scores': row['scores'],
                         'observations': {key: 'Label present in submitted assessment; meaning requires review.' for key in labels},
                         'rationale': 'Recomputing published numbers, not revalidating model explanations.'})
        errors[name] = rw.eval_results(rows, seeds, rubric)
        if rw.digest(errors[name]) != rw.digest(report[name + '_errors']):
            raise ValueError('reported errors disagree with assessment rows')
    summary = evaluation_summary(errors['before'], errors['after'])
    rate = summary['improved_cases'] / summary['case_count']
    regressed = [key for key in errors['before'] if errors['after'][key] > errors['before'][key]]
    if (rw.digest(summary) != rw.digest(report['evaluation_summary'])
            or type(report['improvement_rate']) not in (int, float)
            or report['improvement_rate'] != rate or report['regressions'] != regressed):
        raise ValueError('reported counts or rates disagree with deterministic calculations')
    if rate <= .5 or regressed or summary['after']['failed_cases']:
        raise ValueError('published candidate does not pass the scoring gate')
    return {'valid': True, 'profile_version': profiles[-1]['version'], 'evaluation_summary': summary}
