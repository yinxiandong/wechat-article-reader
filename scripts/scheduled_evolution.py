#!/usr/bin/env python3
"""Curated feedback export and bounded cloud evaluation. Never executes model-generated code."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import evolution_core as core
from model_client import ModelClient
import review_workspace as rw

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 48000
SENSITIVE = re.compile(r'https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}|-----BEGIN|[A-Za-z]:[\\/]|/(?:home|Users|root)/|(?<!\d)1[3-9]\d{9}(?!\d)', re.I)


def safe_text(value):
    rw.required_text(value, 'sanitized text')
    if len(value) > 1000 or SENSITIVE.search(value):
        raise ValueError('text needs manual redaction')
    return value


def validate_feedback(data):
    if not isinstance(data, dict) or set(data) != {'schema_version', 'base_profile_sha256', 'cases'}:
        raise ValueError('unexpected feedback fields')
    if data['schema_version'] != 1 or not re.fullmatch(r'[0-9a-f]{64}', str(data['base_profile_sha256'])):
        raise ValueError('invalid schema or profile hash')
    cases = data['cases']
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise ValueError('requires 1-50 curated cases')
    rubric = rw.load(ROOT / 'assets/scoring-rubric.json')
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'article_type', 'scores', 'lesson'}:
            raise ValueError('unexpected case fields')
        if not re.fullmatch(r'case-[0-9a-f]{16}', str(case['id'])) or case['id'] in seen:
            raise ValueError('invalid or duplicate anonymous ID')
        seen.add(case['id'])
        if case['article_type'] not in rubric['article_types']:
            raise ValueError('invalid article type')
        rw.scores_valid(case['scores'], rubric)
        safe_text(case['lesson'])
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError('feedback too large')
    return data


def export_feedback(workspace, lessons, output, confirmed):
    if not confirmed:
        raise ValueError('review lessons and use --confirm-sanitized')
    if not isinstance(lessons, dict) or not lessons:
        raise ValueError('lessons must map existing review IDs to sanitized lessons')
    state = rw.load(Path(workspace) / 'review-state.json')
    reviews = {r['id']: r for r in state['reviews']}
    cases = []
    for identifier, lesson in lessons.items():
        if identifier not in reviews:
            raise ValueError('unknown review ID')
        item = reviews[identifier]
        cases.append({'id': 'case-' + rw.digest(identifier)[:16],
                      'article_type': item['review']['article_type'],
                      'scores': item['review']['scores'], 'lesson': safe_text(lesson)})
    data = validate_feedback({'schema_version': 1, 'base_profile_sha256': rw.digest(state['profiles'][-1]), 'cases': cases})
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError('private output must be outside skill package')
    with output.open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return {'exported_cases': len(cases), 'output': str(output), 'private': True}


def assess(client, rubric, profile, cases):
    rows = []
    for case in cases:
        data = {'rubric': rubric, 'profile': profile,
                'article': {k: case[k] for k in ('id', 'article_type', 'title', 'text')},
                'observation_labels': case['required_observations']}
        answer = client('Evaluate the synthetic article under this rubric and profile. Return scores (all five integer dimensions 0-5), observations (only supported labels, each with explanation), and rationale. Missing evidence must remain missing; do not optimize for an unknown test.', data)
        if not isinstance(answer, dict):
            raise ValueError('assessment must be a JSON object')
        row = {'case_id': case['id'], 'case_sha256': rw.digest(case),
               'scores': answer.get('scores'), 'observations': answer.get('observations'), 'rationale': answer.get('rationale')}
        rw.eval_results([row], {'cases': [case]}, rubric)
        rows.append(row)
    return rows


def evolve(feedback, client, root=ROOT, model='configured-model'):
    feedback = validate_feedback(feedback)
    rubric = rw.load(root / 'assets/scoring-rubric.json')
    profiles = rw.validate_published_profiles(rw.load(root / 'assets/published-profiles.json'), rubric)
    base = profiles[-1]
    if feedback['base_profile_sha256'] != rw.digest(base):
        return {'changed': False, 'reason': 'feedback_profile_stale'}
    seeds = rw.load(root / 'assets/eval-seed.json')
    if len(seeds['cases']) != 7:
        raise ValueError('budget requires exactly seven frozen cases')
    suggestion = client('Reflect on sanitized calibration lessons. Return {"changes": []} for no justified correction, otherwise 1-3 objects with guidance, reason, evidence_review_ids. Each guidance/reason <=1000 characters, safe for publication. Cite supplied anonymous case IDs. Append clarifications only; preserve dimensions, equal weights, anchors and total 25. No names, links, identifiers, excerpts or private details in guidance. Do not change code or invoke tools.',
                        {'rubric': rubric, 'profile': base, 'calibration_cases': feedback['cases']})
    if not isinstance(suggestion, dict):
        raise ValueError('proposal must be a JSON object')
    changes = suggestion.get('changes')
    if changes == []:
        return {'changed': False, 'reason': 'no_supported_changes'}
    if not isinstance(changes, list) or not 1 <= len(changes) <= 3:
        raise ValueError('invalid bounded edit')
    known = {c['id'] for c in feedback['cases']}
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'guidance', 'reason', 'evidence_review_ids'}:
            raise ValueError('unexpected change fields')
        safe_text(change['guidance'])
        safe_text(change['reason'])
        ids = change['evidence_review_ids']
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or x not in known for x in ids):
            raise ValueError('missing calibration evidence')
    state, proposal, candidate = core.prepare_candidate(rubric, profiles, seeds, feedback, changes)
    # Separate calls; neither assessor sees score bands, other results, or proposal feedback.
    before = assess(client, rubric, base, seeds['cases'])
    after = assess(client, rubric, candidate, seeds['cases'])
    proposal['evaluations'].update(before=before, after=after)
    result = rw.gate(state, proposal)
    if not result['passed']:
        return {'changed': False, 'reason': 'regression_gate_rejected', 'improvement_rate': result['improvement_rate'],
                'evaluation_summary': core.evaluation_summary(result['before_errors'], result['after_errors'])}
    report = core.build_report(result, proposal, profiles, seeds, feedback, model, rw.now())
    core.validate_publication(rubric, profiles + [candidate], seeds, report)
    for filename, value in (('assets/published-profiles.json', profiles + [candidate]), ('assets/evolution-report.json', report)):
        (root / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'changed': True, 'reason': 'gate_passed', 'version': candidate['version'],
            'evaluation_summary': report['evaluation_summary']}


def scheduled_run(environ=None):
    env = os.environ if environ is None else environ
    required = ('OPENAI_API_KEY', 'EVOLUTION_MODEL', 'EVOLUTION_FEEDBACK_JSON')
    missing = [key for key in required if not env.get(key)]
    if missing:
        return {'changed': False, 'reason': 'not_configured', 'missing': missing}
    raw = env['EVOLUTION_FEEDBACK_JSON']
    if len(raw.encode()) > MAX_BYTES:
        raise ValueError('feedback too large')
    client = ModelClient(env['OPENAI_API_KEY'], env['EVOLUTION_MODEL'])
    try:
        result = evolve(json.loads(raw), client, model=env['EVOLUTION_MODEL'])
    except (OSError, ValueError, KeyError, TypeError):
        result = {'changed': False, 'reason': 'evaluation_failed', 'failed': True}
    result['runtime_metrics'] = client.metrics()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('run')
    export = sub.add_parser('export-feedback')
    export.add_argument('--workspace', type=Path, required=True)
    export.add_argument('--lessons', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--confirm-sanitized', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = scheduled_run() if args.command == 'run' else export_feedback(args.workspace, rw.load(args.lessons), args.output, args.confirm_sanitized)
        if args.command == 'run':
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
                    handle.write('changed=' + str(result['changed']).lower() + '\n')
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
                    handle.write('Scoring evolution: ' + result['reason'] + '\n')
                    if result.get('missing'):
                        handle.write('Missing settings: ' + ', '.join(result['missing']) + '\n')
                    if result.get('evaluation_summary'):
                        handle.write('Correctness and regression counts: ' + json.dumps(result['evaluation_summary']) + '\n')
                    if result.get('runtime_metrics'):
                        handle.write('Request cost metrics (not scoring evidence): ' + json.dumps(result['runtime_metrics']) + '\n')
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result.get('failed') else 0
    except (OSError, ValueError, KeyError, TypeError):
        print('ERROR: evolution input, configuration or evaluation failed; no PR created. Provider bodies are intentionally not logged.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
