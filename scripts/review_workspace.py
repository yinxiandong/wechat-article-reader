#!/usr/bin/env python3
"""Local evidence-backed reviews and bounded scoring evolution. No model calls or publishing."""
import argparse
import hashlib
import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be non-empty text')
    return value


def contains(text, quote):
    return ''.join(quote.split()) in ''.join(text.split())


def scores_valid(scores, rubric):
    keys = {item['id'] for item in rubric['dimensions']}
    if not isinstance(scores, dict) or set(scores) != keys:
        raise ValueError('scores must contain exactly the five rubric dimensions')
    for value in scores.values():
        if type(value) is not int or not 0 <= value <= 5:
            raise ValueError('scores must be integers 0-5')


@contextmanager
def transaction(workspace, initialize=False):
    workspace = Path(workspace).resolve()
    # Keep user data separate from the public package.
    if workspace == ROOT or ROOT in workspace.parents:
        raise ValueError('workspace must be outside the skill package')
    workspace.mkdir(parents=True, exist_ok=True)
    lock = workspace / '.review.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('workspace is locked; reconcile an interrupted writer before retrying') from exc
    temp = None
    try:
        with handle:
            handle.write(str(os.getpid()))
        state_path = workspace / 'review-state.json'
        state = load(state_path) if state_path.exists() else (initial_state(load(ROOT / 'assets/scoring-rubric.json')) if initialize else None)
        yield state, workspace
        if state is not None:
            temp = workspace / ('.state-' + uuid.uuid4().hex + '.tmp')
            with temp.open('x', encoding='utf-8', newline='\n') as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, state_path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
        lock.unlink()


def initial_state(rubric):
    profile = {'version': 1, 'parent': None, 'guidance': [], 'rubric_sha256': digest(rubric)}
    return {'schema_version': 1, 'created_at': now(), 'rubric': rubric,
            'profiles': [profile], 'reviews': [], 'feedback': [], 'reflections': [],
            'last_reflect': None, 'reviewed_through': 0}


def status(state):
    since = len(state['reviews']) - state['reviewed_through']
    last = datetime.fromisoformat(state['last_reflect'] or state['created_at'])
    weekly = datetime.now(timezone.utc) - last >= timedelta(days=7)
    return {'total_commentary': len(state['reviews']), 'since_last_reflect': since,
            'reflect_threshold': 5, 'count_due': since >= 5, 'weekly_due': weekly,
            'reflect_due': since >= 5 or weekly, 'last_reflect': state['last_reflect'],
            'active_profile': state['profiles'][-1], 'active_profile_sha256': digest(state['profiles'][-1]),
            'open_reflections': [x['id'] for x in state['reflections'] if x['status'] == 'open']}


def record(state, article, review):
    if not isinstance(article, dict) or not isinstance(review, dict):
        raise ValueError('article and review must be JSON objects')
    rubric = state['rubric']
    text = required_text(article.get('text'), 'article.text')
    if not text.strip():
        raise ValueError('no text for evidence-backed scoring')
    required_text(article.get('title'), 'article.title')
    if review.get('article_type') not in rubric['article_types']:
        raise ValueError('unknown article_type')
    if review.get('coverage') not in ('full_text_reviewed', 'partial'):
        raise ValueError('coverage must be full_text_reviewed or partial')
    expected_profile = digest(state['profiles'][-1])
    if review.get('profile_sha256') != expected_profile:
        raise ValueError('review profile differs from active profile; reassess before recording')
    text_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if review.get('article_text_sha256') != text_sha:
        raise ValueError('review does not match article text hash')
    scores_valid(review.get('scores'), rubric)
    reasons = review.get('reasons', {})
    evidence = review.get('evidence', {})
    if not isinstance(reasons, dict) or not isinstance(evidence, dict):
        raise ValueError('reasons and evidence must be objects')
    for dim in review['scores']:
        required_text(reasons.get(dim), f'reasons.{dim}')
        quotes = evidence.get(dim)
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 3:
            raise ValueError(f'evidence.{dim} needs 1-3 short article excerpts')
        for quote in quotes:
            if not isinstance(quote, str) or not 4 <= len(quote) <= 180 or len(''.join(quote.split())) < 4 or not contains(text, quote):
                raise ValueError(f'evidence.{dim} must be a 4-180 character excerpt from this article')
    for key in ('summary', 'evaluation', 'takeaways', 'verdict', 'verification_limits'):
        required_text(review.get(key), key)
    identifier = digest({'text': text_sha, 'source': article.get('source'), 'profile': expected_profile})[:24]
    existing = next((item for item in state['reviews'] if item['id'] == identifier), None)
    if existing:
        if existing['review'] != review:
            raise ValueError('review already recorded with different content; use feedback, not overwrite')
        return {'review_id': identifier, 'duplicate': True, **status(state)}
    state['reviews'].append({'id': identifier, 'recorded_at': now(),
                             'article': {k: article.get(k) for k in ('title', 'author', 'account', 'source', 'published_at')},
                             'article_text_sha256': text_sha, 'profile_sha256': expected_profile,
                             'rubric_version': rubric['version'], 'review': review,
                             'total': sum(review['scores'].values()),
                             'score_status': 'provisional' if review['coverage'] == 'partial' else 'assessed'})
    return {'review_id': identifier, 'duplicate': False, 'total': sum(review['scores'].values()), **status(state)}


def reflect(state, trigger, seeds):
    due = status(state)
    if trigger == 'count' and not due['count_due']:
        raise ValueError('fewer than five unreflected reviews')
    if trigger == 'weekly' and not due['weekly_due']:
        raise ValueError('weekly reflection is not due')
    if due['open_reflections']:
        return next(x for x in state['reflections'] if x['id'] == due['open_reflections'][0])
    item = {'id': uuid.uuid4().hex, 'created_at': now(), 'status': 'open', 'trigger': trigger,
            'base_profile_sha256': digest(state['profiles'][-1]), 'through': len(state['reviews']),
            'review_ids': [r['id'] for r in state['reviews'][state['reviewed_through']:]],
            'seed_sha256': digest(seeds), 'feedback_ids': [x['id'] for x in state['feedback']],
            'evaluation_seed': seeds}
    state['reflections'].append(item)
    return item


def eval_results(rows, seeds, rubric):
    if not isinstance(rows, list) or len(rows) != len(seeds['cases']):
        raise ValueError('evaluation must cover every seed exactly once')
    by_id = {x['case_id']: x for x in rows}
    if len(by_id) != len(rows) or set(by_id) != {x['id'] for x in seeds['cases']}:
        raise ValueError('duplicate, missing or extra evaluation cases')
    errors = {}
    for case in seeds['cases']:
        row = by_id[case['id']]
        if row.get('case_sha256') != digest(case):
            raise ValueError('evaluation seed content hash mismatch')
        scores_valid(row.get('scores'), rubric)
        required_text(row.get('rationale'), 'evaluation rationale')
        observations = row.get('observations')
        if not isinstance(observations, dict) or not set(observations) <= set(case['required_observations']):
            raise ValueError('invalid evaluation observation keys')
        for value in observations.values():
            required_text(value, 'observation explanation')
        error = 2 * len(set(case['required_observations']) - set(observations))
        for key, (low, high) in case['expected_scores'].items():
            error += max(low - row['scores'][key], 0, row['scores'][key] - high)
        errors[case['id']] = error
    return errors


def prepare(state, reflection_id, changes):
    reflection = next((x for x in state['reflections'] if x['id'] == reflection_id and x['status'] == 'open'), None)
    if not reflection or not isinstance(changes, list) or not 1 <= len(changes) <= 3:
        raise ValueError('prepare needs an open reflection and 1-3 changes')
    current = state['profiles'][-1]
    if digest(current) != reflection['base_profile_sha256']:
        raise ValueError('reflection baseline is stale')
    candidate = {'version': current['version'] + 1, 'parent': digest(current),
                 'guidance': current['guidance'] + [required_text(x.get('guidance'), 'guidance') for x in changes],
                 'rubric_sha256': digest(state['rubric'])}
    skeleton = [{'case_id': x['id'], 'case_sha256': digest(x),
                 'scores': {d['id']: None for d in state['rubric']['dimensions']},
                 'observations': {}, 'rationale': ''} for x in reflection['evaluation_seed']['cases']]
    return {'reflection_id': reflection_id, 'base_profile_sha256': digest(current),
            'changes': changes, 'evaluations': {'before_profile_sha256': digest(current),
            'after_profile_sha256': digest(candidate), 'before': skeleton,
            'after': json.loads(json.dumps(skeleton))}}


def gate(state, proposal):
    if not isinstance(proposal, dict):
        raise ValueError('proposal must be a JSON object')
    reflection = next((x for x in state['reflections'] if x['id'] == proposal.get('reflection_id')), None)
    if not reflection or reflection['status'] != 'open':
        raise ValueError('proposal requires an open reflection')
    if proposal.get('base_profile_sha256') != digest(state['profiles'][-1]) or proposal['base_profile_sha256'] != reflection['base_profile_sha256']:
        raise ValueError('proposal is stale relative to active profile')
    changes = proposal.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 3:
        raise ValueError('bounded edit requires 1-3 supplemental guidance changes')
    known = set(reflection['review_ids'])
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('each change must be an object')
        guidance = required_text(change.get('guidance'), 'change.guidance')
        if len(guidance) > 1000:
            raise ValueError('guidance change exceeds 1000 characters')
        required_text(change.get('reason'), 'change.reason')
        ids = change.get('evidence_review_ids')
        if not isinstance(ids, list) or not ids or not set(ids) <= known:
            raise ValueError('change must cite actual reviews in this reflection')
    seeds = reflection['evaluation_seed']
    if digest(seeds) != reflection['seed_sha256']:
        raise ValueError('reflection seed was modified')
    evaluations = proposal.get('evaluations', {})
    if evaluations.get('before_profile_sha256') != reflection['base_profile_sha256']:
        raise ValueError('baseline evaluation must identify active profile')
    # Supplemental guidance is the only mutable surface; dimensions, anchors and weights stay fixed.
    candidate = {'version': state['profiles'][-1]['version'] + 1,
                 'parent': reflection['base_profile_sha256'],
                 'guidance': state['profiles'][-1]['guidance'] + [x['guidance'] for x in changes],
                 'rubric_sha256': digest(state['rubric'])}
    if evaluations.get('after_profile_sha256') != digest(candidate):
        raise ValueError('candidate evaluation must match candidate profile hash')
    before = eval_results(evaluations.get('before'), seeds, state['rubric'])
    after = eval_results(evaluations.get('after'), seeds, state['rubric'])
    improved = [key for key in before if after[key] < before[key]]
    regressed = [key for key in before if after[key] > before[key]]
    rate = len(improved) / len(before)
    passed = rate > 0.5 and not regressed and not any(after.values())
    return {'passed': passed, 'improvement_rate': rate, 'regressions': regressed,
            'before_errors': before, 'after_errors': after,
            'candidate_profile': candidate, 'candidate_profile_sha256': digest(candidate),
            'proposal_sha256': digest(proposal),
            'limitation': 'Checks submitted assessments against frozen seed bands and observations; semantic honesty still requires reviewer scrutiny.'}


def review_markdown(item, rubric):
    review = item['review']
    lines = [f'# {item["article"]["title"]} — 点评', '',
             f'类型：{review["article_type"]} | 评分状态：{item["score_status"]}',
             f'规则：{item["rubric_version"]} | profile：{item["profile_sha256"]}',
             '', '## 摘要', review['summary'], '', '## 深度评价', review['evaluation'],
             '', '## 可借鉴之处', review['takeaways'], '', '## 一句话判断', review['verdict'],
             '', f'## 文章评分：{item["total"]}/25', '', '| 维度 | 分数 | 理由及正文依据 |',
             '| --- | --- | --- |']
    for dim in rubric['dimensions']:
        key = dim['id']
        cell = (review['reasons'][key] + '；依据：' + ' / '.join(review['evidence'][key])).replace('|', '\\|').replace('\n', ' ')
        lines.append(f'| {dim["label"]} | {review["scores"][key]}/5 | {cell} |')
    lines += ['', '## 核验范围与不足', review['verification_limits'], '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('status')
    record_cmd = sub.add_parser('record')
    record_cmd.add_argument('--article', type=Path, required=True)
    record_cmd.add_argument('--review', type=Path, required=True)
    show = sub.add_parser('show')
    show.add_argument('--review-id', required=True)
    show.add_argument('--output', type=Path, help='New UTF-8 Markdown output file')
    feedback = sub.add_parser('feedback')
    feedback.add_argument('--review-id', required=True)
    feedback.add_argument('--note', required=True)
    reflection = sub.add_parser('reflect')
    reflection.add_argument('--trigger', choices=('manual', 'count', 'weekly'), default='manual')
    close = sub.add_parser('close-reflection')
    close.add_argument('--reflection-id', required=True)
    close.add_argument('--reason', required=True)
    preparation = sub.add_parser('prepare')
    preparation.add_argument('--reflection-id', required=True)
    preparation.add_argument('--changes', type=Path, required=True)
    preparation.add_argument('--output', type=Path, help='New UTF-8 proposal JSON file')
    for command in ('gate', 'apply'):
        command_parser = sub.add_parser(command)
        command_parser.add_argument('--proposal', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with transaction(args.workspace, initialize=args.command == 'init') as (state, workspace):
            if args.command == 'init':
                result = status(state)
            else:
                if state is None:
                    raise ValueError('initialize this workspace first')
                if state['profiles'][-1]['rubric_sha256'] != digest(state['rubric']):
                    raise ValueError('rubric changed; explicit migration and recalibration required')
                if args.command == 'status':
                    result = status(state)
                elif args.command == 'record':
                    result = record(state, load(args.article), load(args.review))
                elif args.command == 'show':
                    item = next(x for x in state['reviews'] if x['id'] == args.review_id)
                    result = review_markdown(item, state['rubric'])
                elif args.command == 'feedback':
                    if not any(x['id'] == args.review_id for x in state['reviews']):
                        raise ValueError('review not found')
                    item = {'id': uuid.uuid4().hex, 'review_id': args.review_id, 'note': required_text(args.note, 'feedback'), 'created_at': now()}
                    state['feedback'].append(item)
                    result = item
                elif args.command == 'reflect':
                    result = reflect(state, args.trigger, load(ROOT / 'assets/eval-seed.json'))
                elif args.command == 'prepare':
                    result = prepare(state, args.reflection_id, load(args.changes))
                elif args.command == 'close-reflection':
                    item = next(x for x in state['reflections'] if x['id'] == args.reflection_id and x['status'] == 'open')
                    item.update(status='no_change', reason=required_text(args.reason, 'reason'), closed_at=now())
                    state.update(last_reflect=now(), reviewed_through=item['through'])
                    result = status(state)
                else:
                    proposal = load(args.proposal)
                    result = gate(state, proposal)
                    if args.command == 'apply':
                        if not result['passed']:
                            raise ValueError('gate rejected candidate; active profile unchanged')
                        state['profiles'].append(result['candidate_profile'])
                        item = next(x for x in state['reflections'] if x['id'] == proposal['reflection_id'])
                        item.update(status='applied', closed_at=now(), proposal=proposal, gate=result)
                        state.update(last_reflect=now(), reviewed_through=item['through'])
        rendered = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        output = getattr(args, 'output', None)
        if output:
            with output.open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(rendered + '\n')
            print(json.dumps({'output': str(output.resolve())}, ensure_ascii=False))
        else:
            print(rendered)
        return 1 if args.command == 'gate' and not result['passed'] else 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
