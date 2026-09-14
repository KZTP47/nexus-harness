from __future__ import annotations

import io
import socket
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from our_harness import cancellation
from our_harness.models import HarnessError
from our_harness.providers import base
from tests.test_relay_transport_reliability import provider


class Stream(io.BytesIO):
    def __init__(self, data=b'first\nsecond\n', delay=0, close_gate=None):
        super().__init__(data)
        self.delay = delay
        self.close_gate = close_gate
        self.closed_event = threading.Event()

    def read1(self, size=-1):
        time.sleep(self.delay)
        return super().read1(size)

    def read(self, size=-1):
        time.sleep(self.delay)
        return super().read(size)

    def close(self):
        if self.close_gate is not None:
            self.close_gate.wait(2)
        super().close()
        self.closed_event.set()


class StreamTransportReliabilityTests(unittest.TestCase):
    def test_normal_incremental_stream_and_error(self):
        response = Stream('first\n☃\n'.encode())
        self.assertEqual(list(provider(lambda *a, **k: response)._stream_lines('http://local.test', {})), ['first', '☃'])
        self.assertTrue(response.closed_event.wait(1))
        error_body = Stream(b'specific error')
        def fail(*a, **k):
            raise urllib.error.HTTPError('http://local.test', 429, 'busy', {}, error_body)
        with self.assertRaisesRegex(HarnessError, 'Provider HTTP 429: specific error'):
            list(provider(fail)._stream_lines('http://local.test', {}))
        self.assertTrue(error_body.closed_event.wait(1))

    def test_http_error_read_and_slow_close_never_block_caller(self):
        gate = threading.Event()
        response = Stream(b'error', delay=.35, close_gate=gate)
        def fail(*a, **k):
            raise urllib.error.HTTPError('http://local.test', 500, 'failed', {}, response)
        slots = threading.BoundedSemaphore(1)
        with patch.object(base, '_HTTP_POST_SLOTS', slots):
            start = time.monotonic()
            try:
                with self.assertRaisesRegex(HarnessError, 'wall-clock deadline'):
                    list(provider(fail)._stream_lines('http://local.test', {}))
                self.assertLess(time.monotonic() - start, .3)
                self.assertFalse(slots.acquire(blocking=False))
            finally:
                gate.set()
            self.assertTrue(response.closed_event.wait(1))
            self.assertTrue(slots.acquire(timeout=1))
            slots.release()

    def test_original_token_is_checked_before_each_buffered_frame(self):
        response = Stream()
        token = cancellation.Cancellation()
        with cancellation.use(token):
            stream = provider(lambda *a, **k: response, 1)._stream_lines('http://local.test', {})
            self.assertEqual(next(stream), 'first')
        token.cancel()
        with self.assertRaises(cancellation.ChatCancelled):
            next(stream)  # No cancellation context here: the original token owns it.
        self.assertTrue(response.closed_event.wait(1))

    def test_deadline_checked_before_second_frame_from_same_chunk(self):
        response = Stream()
        stream = provider(lambda *a, **k: response)._stream_lines('http://local.test', {})
        self.assertEqual(next(stream), 'first')
        time.sleep(.14)
        with self.assertRaisesRegex(HarnessError, 'wall-clock deadline'):
            next(stream)

    def test_generator_close_releases_full_queue_worker_without_blocking_close(self):
        gate = threading.Event()
        response = Stream(b'x\n' * 1_000_000, close_gate=gate)
        slots = threading.BoundedSemaphore(1)
        with patch.object(base, '_HTTP_POST_SLOTS', slots):
            stream = provider(lambda *a, **k: response, 1)._stream_lines('http://local.test', {})
            self.assertEqual(next(stream), 'x')
            start = time.monotonic()
            stream.close()
            self.assertLess(time.monotonic() - start, .2)
            self.assertFalse(slots.acquire(blocking=False))
            gate.set()
            self.assertTrue(response.closed_event.wait(1))
            self.assertTrue(slots.acquire(timeout=1))
            slots.release()

    def test_thread_start_failure_and_admission_timeout_do_not_leak_slots(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(base, '_HTTP_POST_SLOTS', slots):
            with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no thread')):
                with self.assertRaisesRegex(RuntimeError, 'no thread'):
                    list(provider(lambda *a, **k: Stream())._stream_lines('http://local.test', {}))
            self.assertTrue(slots.acquire(blocking=False))
            calls = []
            try:
                with self.assertRaisesRegex(HarnessError, 'wall-clock deadline'):
                    list(provider(lambda *a, **k: calls.append(1))._stream_lines('http://local.test', {}))
                self.assertEqual(calls, [])
            finally:
                slots.release()

    def test_late_dns_never_posts_after_timeout_or_cancel(self):
        posts = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_POST(self):
                posts.append(1)
                self.send_response(200)
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02))
        serving.start()
        original_dns = socket.getaddrinfo
        try:
            for cancel in (False, True):
                entered, release = threading.Event(), threading.Event()
                token = cancellation.Cancellation()
                errors = []
                slots = threading.BoundedSemaphore(1)
                def dns(*a, **k):
                    entered.set()
                    release.wait(2)
                    return original_dns(*a, **k)
                def run():
                    with cancellation.use(token):
                        try:
                            list(provider(base._LazyHttpOpener().open)._stream_lines(f'http://127.0.0.1:{server.server_port}', {}))
                        except Exception as exc:
                            errors.append(exc)
                with patch.object(socket, 'getaddrinfo', dns), patch.object(base, '_HTTP_POST_SLOTS', slots):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    if cancel:
                        token.cancel()
                    thread.join(.5)
                    try:
                        self.assertFalse(thread.is_alive())
                        self.assertIsInstance(errors[0], cancellation.ChatCancelled if cancel else HarnessError)
                    finally:
                        release.set()
                        thread.join(1)
                    self.assertTrue(slots.acquire(timeout=1))
                    slots.release()
                self.assertEqual(posts, [])
        finally:
            server.shutdown()
            server.server_close()
            serving.join(1)
