"""Rebuild confirmed OOM counts from saved evidence, never from app exits alone."""
import argparse
from pathlib import Path

from .core import load, save


def build(root):
    incidents = {}
    runs = []
    for result_path in sorted(root.glob('*/result.json')):
        folder = result_path.parent
        event_path = folder / 'events.json'
        if not event_path.exists():
            continue
        result, events = load(result_path), load(event_path)
        original = load(folder/'original_state.json') if (folder/'original_state.json').exists() else {}
        boot = original.get('boot_id', folder.name)
        keys = set()
        for event in events.get('events', []):
            if event.get('category') != 'oom_kill':
                continue
            identity = event.get('identity') or {}
            start = identity.get('start_ticks')
            key = (boot, event.get('pid'), start) if start else (boot, folder.name, event.get('kernel_sequence'), event.get('time'))
            keys.add(key)
            item = incidents.setdefault(key, {'boot_id':boot, 'pid':event.get('pid'),
                'identity':identity, 'event_time':event.get('time'),
                'timestamp_kind':event.get('timestamp_kind','aligned_kernel_event'),
                'source':event.get('source','kernel'), 'evidence_files':[]})
            reference = str(event_path.resolve())
            if reference not in item['evidence_files']:
                item['evidence_files'].append(reference)
        runs.append({'run':folder.name, 'confirmed_oom_count':len(keys),
                     'oom_coverage_complete':events.get('oom_label_coverage_complete',False),
                     'completed':result.get('completed',False),
                     'formal_training_eligible':result.get('valid_for_training',False)})
    return {'confirmed_oom_count':len(incidents), 'count_unit':'distinct killed process lifetime',
            'note':'Incomplete coverage means an unknown total; confirmed count is a lower bound.',
            'runs':runs, 'incidents':list(incidents.values())}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args=parser.parse_args()
    report=build(args.root)
    save(args.root/'oom_ledger.json',report)
    print('confirmed OOM:', report['confirmed_oom_count'])
