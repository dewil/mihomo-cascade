#!/usr/bin/env python3
"""Black-box contract tests; intentionally independent of CLI implementation."""
import datetime as dt
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'HEALTH_SECRET_SENTINEL_7f9b'
URL = 'https://probe.invalid/' + SECRET


def timestamp(seconds=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def member(alive=True, delay=12, when=None):
    return {'alive': not alive, 'history': [{'time': timestamp(), 'delay': 999}],
            'extra': {URL: {'alive': alive, 'history': [
                {'time': timestamp() if when is None else when, 'delay': delay}]}},
            'password': SECRET}


def payload(members=None):
    members = {'primary': member(), 'backup': member(False)} if members is None else members
    return {'proxies': {'cascade': {'type': 'Fallback', 'now': 'primary',
                                   'all': list(members), 'testUrl': URL}, **members},
            'secret': SECRET}


class HealthContract(unittest.TestCase):
    def invoke(self, body, json_mode=True, status=200):
        calls = []
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(('GET', self.path))
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(raw)

            def do_PUT(self):
                calls.append(('PUT', self.path))
                self.send_response(405)
                self.end_headers()

            do_POST = do_PUT
            do_DELETE = do_PUT

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ, MIHOMO_API='http://127.0.0.1:%s' % server.server_port)
            result = subprocess.run(['python3', str(ROOT / 'usr/local/sbin/mihomo-api'),
                                     'health'] + (['--json'] if json_mode else []),
                                    env=env, capture_output=True, text=True, timeout=10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertEqual(calls, [('GET', '/proxies')], result.stderr)
        return result

    def report(self, body):
        result = self.invoke(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {'schema_version', 'observed_at', 'groups'})
        self.assertEqual(report['schema_version'], 1)
        observed = dt.datetime.fromisoformat(report['observed_at'].replace('Z', '+00:00'))
        self.assertEqual(observed.utcoffset(), dt.timedelta(0))
        self.assertLess(abs((dt.datetime.now(dt.timezone.utc) - observed).total_seconds()), 10)
        for group in report['groups']:
            self.assertEqual(set(group), {'name', 'selected', 'status', 'members'})
            for entry in group['members']:
                self.assertEqual(set(entry), {'name', 'status', 'checked_at', 'delay_ms'})
        return report

    def state(self, record):
        group = self.report(payload({'primary': record}))['groups'][0]
        return group, group['members'][0]

    def test_available_order_and_per_url_authority(self):
        body = payload({'z-last': member(True, 0), 'a-first': member(False)})
        body['proxies']['cascade']['now'] = 'a-first'
        body['proxies']['irrelevant'] = {'type': 'Selector', 'all': []}
        group = self.report(body)['groups'][0]
        self.assertEqual(group['selected'], 'a-first')
        self.assertEqual(group['status'], 'available')
        self.assertEqual([m['name'] for m in group['members']], ['z-last', 'a-first'])
        self.assertEqual([m['status'] for m in group['members']], ['up', 'down'])
        self.assertEqual([m['delay_ms'] for m in group['members']], [0, None])

    def test_all_down_and_empty_group(self):
        group, entry = self.state(member(False))
        self.assertEqual(group['status'], 'all_down')
        self.assertEqual(entry['status'], 'down')
        self.assertIsNone(entry['delay_ms'])
        self.assertEqual(self.report(payload({}))['groups'][0]['status'], 'unknown')

    def test_stale_future_and_invalid_timestamp(self):
        for when, valid in [(timestamp(-100), True), (timestamp(30), True),
                            ('not-a-date', False), ('2026-10-19T12:00:00', False),
                            (False, False), (10**100, False)]:
            with self.subTest(when=when):
                group, entry = self.state(member(when=when))
                self.assertEqual(entry['status'], 'unknown')
                self.assertEqual(group['status'], 'unknown')
                self.assertIsNone(entry['delay_ms'])
                if not valid:
                    self.assertIsNone(entry['checked_at'])

    def test_recent_and_small_future_are_fresh(self):
        for seconds in [-70, 2]:
            with self.subTest(seconds=seconds):
                _, entry = self.state(member(when=timestamp(seconds)))
                self.assertEqual(entry['status'], 'up')
                self.assertIsNotNone(entry['checked_at'])

    def test_unknown_delay_and_alive(self):
        for value in [None, -1, '12', True, 1.5]:
            with self.subTest(delay=value):
                _, entry = self.state(member(delay=value))
                self.assertEqual(entry['status'], 'unknown')
                self.assertIsNone(entry['delay_ms'])
        for value in [None, 1, 'true']:
            with self.subTest(alive=value):
                record = member()
                record['extra'][URL]['alive'] = value
                _, entry = self.state(record)
                self.assertEqual(entry['status'], 'unknown')

    def test_missing_or_broken_records(self):
        cases = [None, [], {}, {'alive': True, 'history': [{'time': timestamp(), 'delay': 1}]},
                 {'extra': []}, {'extra': {URL: None}}]
        for record in cases:
            with self.subTest(record=record):
                group, entry = self.state(record)
                self.assertEqual(group['status'], 'unknown')
                self.assertEqual(entry['status'], 'unknown')
        body = payload()
        del body['proxies']['backup']
        self.assertEqual(self.report(body)['groups'][0]['members'][1]['status'], 'unknown')
        body = payload()
        del body['proxies']['cascade']['testUrl']
        self.assertEqual(self.report(body)['groups'][0]['status'], 'unknown')

    def test_last_element_only_and_empty_history(self):
        for history in [[], [None], [{'time': timestamp(), 'delay': 1}, None],
                        [{'time': timestamp(), 'delay': 1}, {'time': 'bad', 'delay': 1}]]:
            with self.subTest(history=history):
                record = member()
                record['extra'][URL]['history'] = history
                _, entry = self.state(record)
                self.assertEqual(entry['status'], 'unknown')

    def test_malformed_group_does_not_lose_good_group(self):
        body = payload()
        body['proxies'].update({'broken': None, 'bad-group': {'type': 'Fallback', 'all': 7}})
        report = self.report(body)
        good = next(g for g in report['groups'] if g['name'] == 'cascade')
        self.assertEqual(good['status'], 'available')
        for group in report['groups']:
            if group['name'] == 'bad-group':
                self.assertEqual(group['status'], 'unknown')

    def test_invalid_global_payload_and_http_error(self):
        for body in [None, [], {}, {'proxies': []}, b'not-json ' + SECRET.encode()]:
            with self.subTest(body=body):
                self.assertNotEqual(self.invoke(body).returncode, 0)
        self.assertNotEqual(self.invoke({'error': SECRET}, status=500).returncode, 0)

    def test_text_statuses_and_all_down_explanation(self):
        body = payload({'up-node': member(), 'down-node': member(False), 'unknown-node': {}})
        result = self.invoke(body, json_mode=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in ['доступен', 'недоступен', 'не определено']:
            self.assertIn(label, result.stdout.lower())
        result = self.invoke(payload({'primary': member(False)}), json_mode=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('не означает', result.stdout.lower())
        self.assertIn('доступност', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
