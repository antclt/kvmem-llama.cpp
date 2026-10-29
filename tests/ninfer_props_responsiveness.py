"""Check /props during streaming text generation on an already running CPU Vision worker.

Run against a dedicated worker started with --vision-cpu and a full text model.
Example: python tests/ninfer_props_responsiveness.py --url http://127.0.0.1:8080
The worker must be idle initially: this test checks that polling does not load Vision weights.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import urllib.request


def check(url, max_tokens=1024, max_latency_ms=1000):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def props():
        with opener.open(url.rstrip('/') + '/props', timeout=10) as response:
            return json.load(response)
    initial = props()['cpu_vision']
    expected_keys = {'resident', 'loading', 'weight_bytes', 'active_encoders', 'load_count',
                     'unload_count', 'last_load_seconds', 'idle_timeout_seconds'}
    assert set(initial) == expected_keys, initial
    assert not initial['resident'] and not initial['loading'] and initial['active_encoders'] == 0, initial
    report = {'url': url, 'initial': initial, 'samples': [], 'chunks': 0, 'complete': False}
    stop = threading.Event()
    generating = threading.Event()
    def poll():
        while not stop.is_set():
            during_generation = generating.is_set()
            started = time.perf_counter()
            state = props()['cpu_vision']
            report['samples'].append({'latency_ms': (time.perf_counter() - started) * 1000,
                                      'during_generation': during_generation, 'cpu_vision': state})
            stop.wait(0.1)
    body = {'messages': [{'role': 'user', 'content':
            'Write a numbered list of 200 distinct practical software testing tips. '
            'Give two sentences per tip and continue until all 200 are complete.'}],
            'max_tokens': max_tokens, 'temperature': 0, 'seed': 1, 'stream': True,
            'stream_options': {'include_usage': True}, 'chat_template_kwargs': {'enable_thinking': False}}
    report['request'] = body
    with ThreadPoolExecutor(max_workers=1) as pool:
        polling = pool.submit(poll)
        try:
            request = urllib.request.Request(url.rstrip('/') + '/v1/chat/completions',
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
            started = time.perf_counter()
            generating.set()
            with opener.open(request, timeout=120) as response:
                for line in response:
                    if not line.startswith(b'data: '):
                        continue
                    if line.strip() == b'data: [DONE]':
                        report['complete'] = True
                        continue
                    event = json.loads(line[6:])
                    assert 'error' not in event, event
                    report['chunks'] += 1
                    if event.get('usage'):
                        report['usage'] = event['usage']
            report['generation_seconds'] = time.perf_counter() - started
        finally:
            generating.clear()
            stop.set()
            polling.result()
    samples = [s for s in report['samples'] if s['during_generation']]
    errors = []
    if not report['complete'] or report['chunks'] < 32 or len(samples) < 10:
        errors.append('Not enough live generation to exercise concurrent props polling')
    for sample in samples:
        if sample['cpu_vision'] != initial:
            errors.append('Text-only generation or props polling changed CPU Vision residency')
            break
    report['max_props_ms'] = max((s['latency_ms'] for s in samples), default=0)
    if report['max_props_ms'] > max_latency_ms:
        errors.append(f'/props exceeded {max_latency_ms} ms during generation')
    report['errors'] = errors
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--max-tokens', type=int, default=1024)
    parser.add_argument('--max-latency-ms', type=float, default=1000)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = check(args.url, args.max_tokens, args.max_latency_ms)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('request', 'samples')}, indent=2))
    if report['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
