"""Uses temp dirs and a fake device; real devices and existing output are untouched."""
import asyncio
import functools
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import pod
from textual.widgets import DataTable


class ParseArgsTests(unittest.TestCase):
    def test_config_defaults_and_saved_settings_from_another_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.json'
            with patch.object(pod, 'CONFIG', config), patch.object(pod, 'BASE', root):
                self.assertEqual(pod.load_config(), (None, 3))
                config.write_text('{"dest": "device", "concurrency": 2}')
                original = Path.cwd()
                try:
                    os.chdir(root.parent)
                    self.assertEqual(pod.load_config(), (str((root / 'device').resolve()), 2))
                finally:
                    os.chdir(original)
                config.write_text('{"dest": null, "concurrency": 3}')
                self.assertEqual(pod.load_config(), (None, 3))

    def test_invalid_config_is_rejected(self):
        fixtures = ('{', '[]', '{"dest": ""}', '{"dest": 12}',
                    '{"concurrency": 0}', '{"concurrency": -1}',
                    '{"concurrency": 1.5}', '{"concurrency": true}',
                    '{"concurrency": "2"}', '{"concurency": 2}')
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            with patch.object(pod, 'CONFIG', config):
                for value in fixtures:
                    with self.subTest(value=value):
                        config.write_text(value)
                        with self.assertRaises(ValueError):
                            pod.load_config()

    def test_default_transfer_and_explicit_nomove(self):
        url = 'https://example.com/episode'
        for arguments, trim, move in (
            ([url], None, True),
            ([url, '10'], 10.0, True),
            ([url, 'nomove'], None, False),
            ([url, '0.5', 'nomove'], 0.5, False),
            ([url, 'nomove', '0.5'], 0.5, False),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(pod.parse_args(arguments), (url, trim, move))

    def test_duplicate_nomove_and_old_move_are_rejected(self):
        url = 'https://example.com/episode'
        for arguments in ([url, 'nomove', 'nomove'], [url, 'move']):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    pod.parse_args(arguments)


class HistoryTests(unittest.TestCase):
    def test_invalid_records_do_not_replace_valid_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / 'transfer_history.jsonl'
            lines = [json.dumps({'cum_bytes': 100, 'cum_duration': 2}),
                     '{broken', '42', '[]', '{"title":"incomplete"}',
                     '{"cum_bytes":-1,"cum_duration":2}',
                     '{"cum_bytes":100,"cum_duration":Infinity}',
                     '{"cum_bytes":true,"cum_duration":2}',
                     '{"cum_bytes":100,"cum_duration":"2"}',
                     '{"cum_bytes":100,"cum_duration":NaN}']
            history.write_text('\n'.join(lines))
            with patch.object(pod, 'HISTORY', str(history)):
                self.assertEqual(pod.read_last_cumulative(), (100.0, 2.0))

    def test_speed_uses_last_five_valid_records_for_matching_device(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / 'transfer_history.jsonl'
            records = [{'cum_bytes': 80, 'cum_duration': 2},
                       {'destination': '/device/a', 'size_bytes': 99999, 'duration': 1}]
            for size in (100, 200, 300, 400, 500):
                records += [{'destination': '/device/a', 'size_bytes': size, 'duration': 2},
                            {'destination': '/device/b', 'size_bytes': 10000, 'duration': 1}]
            records += [{'destination': '/device/a', 'size_bytes': -1, 'duration': 1},
                        {'destination': '/device/a', 'size_bytes': 100, 'duration': 0}]
            history.write_text('\n'.join(json.dumps(record) for record in records))
            with patch.object(pod, 'HISTORY', str(history)):
                self.assertEqual(pod.history_speed('/device/a'), 150)
                self.assertEqual(pod.history_speed('/device/b'), 10000)
                self.assertEqual(pod.history_speed('/device/c'), 40)
                history.write_text(json.dumps(records[1]))
                self.assertIsNone(pod.history_speed('/device/c'))

    def test_append_after_truncated_record_keeps_new_record_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder) / 'transfer_history.jsonl'
            history.write_text('{"cum_bytes":100,"cum_duration":2}\n{"truncated":')
            task = pod.PodTask(1, 'https://example.com', None, True, 'sample')
            task.audio = Path(folder) / 'sample.mp3'
            task.title_file = Path(folder) / 'sample.txt'
            with patch.object(pod, 'HISTORY', str(history)):
                pod.append_history('start', 'end', task, '/device', 50, 1, 150, 3)
                self.assertEqual(pod.read_last_cumulative(), (150.0, 3.0))
                self.assertEqual(len(list(pod.history_records())), 2)
            self.assertEqual(json.loads(history.read_text().splitlines()[-1])['url'], task.url)

    def test_legacy_history_is_renamed_without_changing_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / '.move_history.jsonl'
            history = root / 'transfer_history.jsonl'
            original = '{"cum_bytes": 4096, "cum_duration": 2.5, "title": "旧记录"}\n'
            legacy.write_text(original, encoding='utf-8')
            with patch.object(pod, 'HISTORY', str(history)):
                self.assertEqual(pod.read_last_cumulative(), (4096.0, 2.5))
            self.assertFalse(legacy.exists())
            self.assertEqual(history.read_text(encoding='utf-8'), original)

    def test_existing_new_history_takes_precedence_over_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            legacy = root / '.move_history.jsonl'
            history = root / 'transfer_history.jsonl'
            legacy.write_text('{"cum_bytes": 10, "cum_duration": 1}\n')
            history.write_text('{"cum_bytes": 20, "cum_duration": 2}\n')
            with patch.object(pod, 'HISTORY', str(history)):
                self.assertEqual(pod.read_last_cumulative(), (20.0, 2.0))
            self.assertTrue(legacy.exists())
            self.assertEqual(json.loads(legacy.read_text())['cum_bytes'], 10)


class PodTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.dest = self.root / 'device'
        self.dest.mkdir()
        self.dist = self.root / 'dist'
        self.patches = [patch.object(pod, 'DIST', str(self.dist)),
                        patch.object(pod, 'TMP', str(self.root / 'tmp')),
                        patch.object(pod, 'HISTORY', str(self.root / 'transfer_history.jsonl'))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    async def submit(self, app, pilot, command):
        app.query_one(pod.CommandInput).value = command
        await pilot.press('enter')
        await pilot.pause()

    async def wait_all(self, app):
        await asyncio.wait_for(app.prepare_queue.join(), 10)
        await asyncio.wait_for(app.device_queue.join(), 10)

    async def test_network_progress_retries_and_permanent_http_failure(self):
        counts = {}
        payload = b'a' * (1024 * 1024)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                counts[self.path] = counts.get(self.path, 0) + 1
                if self.path == '/missing':
                    self.send_error(404)
                    return
                if self.path == '/retry' and counts[self.path] == 1:
                    self.send_error(503)
                    return
                if self.path == '/page-retry':
                    if counts[self.path] == 1:
                        self.send_error(503)
                    else:
                        page = b'<title>Fixture page</title>'
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(page)))
                        self.end_headers()
                        self.wfile.write(page)
                    return
                if self.path == '/always-fail':
                    self.send_error(503)
                    return
                self.send_response(200)
                if self.path != '/unknown':
                    self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    if self.path == '/interrupted' and counts[self.path] == 1:
                        self.wfile.write(payload[:65536])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    for start in range(0, len(payload), 65536):
                        self.wfile.write(payload[start:start + 65536])
                        self.wfile.flush()
                        time.sleep(.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        Path(pod.TMP).mkdir()
        app = pod.PodApp()
        try:
            async with app.run_test(size=(140, 35)) as pilot:
                # Insert a normal task row, then exercise the real curl request directly.
                with patch.object(app, 'prepare', return_value=None):
                    await self.submit(app, pilot, f'{url}/known nomove')
                    await self.wait_all(app)
                task = app.tasks[0]
                output = self.root / 'download'
                for route, total in (('/known', len(payload)), ('/unknown', None)):
                    request = asyncio.create_task(app.network_request(task, url + route, output))
                    try:
                        async def wait_progress():
                            while not 0 < task.download_bytes < len(payload) or task.state != 'Downloading':
                                await asyncio.sleep(.02)
                        await asyncio.wait_for(wait_progress(), 5)
                        self.assertEqual(task.download_total, total)
                        app.refresh_status()
                        detail = str(app.query_one('#task-detail').render())
                        self.assertIn('Download:', detail)
                        self.assertIn('ETA' if total else 'Total unknown', detail)
                        await request
                        self.assertEqual(output.read_bytes(), payload)
                    finally:
                        if not request.done():
                            request.cancel()
                        await asyncio.gather(request, return_exceptions=True)
                        task.download_bytes = 0
                await app.network_request(task, url + '/retry', output)
                self.assertEqual(counts['/retry'], 2)
                self.assertEqual(output.read_bytes(), payload)
                self.assertEqual(task.download_attempt, 2)
                await app.network_request(task, url + '/interrupted', output)
                self.assertEqual(counts['/interrupted'], 2)
                self.assertEqual(output.read_bytes(), payload)
                with self.assertRaises(pod.CommandError):
                    await app.network_request(task, url + '/missing', output)
                self.assertEqual(counts['/missing'], 1)
                with self.assertRaises(pod.CommandError):
                    await app.network_request(task, url + '/always-fail', output)
                self.assertEqual(counts['/always-fail'], 3)
                page = await app.network_request(task, url + '/page-retry')
                self.assertEqual(counts['/page-retry'], 2)
                self.assertEqual(page, '<title>Fixture page</title>')
                await app.action_stop()
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_cancel_network_request_terminates_child_and_cleans_headers(self):
        Path(pod.TMP).mkdir()
        app = pod.PodApp()
        task = pod.PodTask(1, 'https://example.com', None, False, 'cancel')
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def pending_command(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.object(app, 'set_state'), patch.object(app, 'command', side_effect=pending_command):
            request = asyncio.create_task(app.network_request(task, task.url, self.root / 'download'))
            await asyncio.wait_for(started.wait(), 2)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
        self.assertTrue(cancelled.is_set())
        self.assertFalse(list(Path(pod.TMP).iterdir()))

    async def test_cancel_during_retry_wait_does_not_start_another_attempt(self):
        Path(pod.TMP).mkdir()
        app = pod.PodApp()
        task = pod.PodTask(1, 'https://example.com', None, False, 'cancel')
        retrying = asyncio.Event()
        details = []
        def state_changed(task, state, detail):
            details.append(detail)
            if 'Retry 2/3' in detail:
                retrying.set()
        failure = pod.CommandError('curl', 28, '\n000', 'timeout')
        with patch.object(app, 'set_state', side_effect=state_changed), patch.object(app, 'command', side_effect=failure) as command:
            request = asyncio.create_task(app.network_request(task, task.url))
            await asyncio.wait_for(retrying.wait(), 2)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertEqual(command.call_count, 1)
        self.assertTrue(any('Network error' in detail for detail in details))
        self.assertFalse(list(Path(pod.TMP).iterdir()))

    async def test_no_device_rejects_transfer_but_allows_local_task(self):
        class LocalApp(pod.PodApp):
            async def prepare(self, task):
                self.dist.mkdir(exist_ok=True)
                task.audio = self.dist / f'{task.stem}.mp3'
                task.audio.write_bytes(b'fixture')
        app = LocalApp()
        app.dist = self.dist
        async with app.run_test() as pilot:
            await self.submit(app, pilot, 'https://example.com/episode')
            self.assertEqual(app.tasks, [])
            self.assertIn('not configured', str(app.query_one('#summary').render()))
            await self.submit(app, pilot, 'https://example.com/episode nomove')
            await self.wait_all(app)
            self.assertEqual(app.tasks[0].state, 'Done')
            self.assertTrue(app.tasks[0].audio.exists())
            await app.action_stop()

    async def test_responsive_table_and_fixed_device_queue(self):
        app = pod.PodApp(self.dest)
        async with app.run_test(size=(100, 32)) as pilot:
            for number in range(1, 20):
                task = pod.PodTask(number, 'https://example.com/episode', None, True,
                                   f'fixture-{number}', title=f'长标题播客 {number}：技术与生活',
                                   state='Copying' if number == 3 else 'Waiting to copy',
                                   started=time.monotonic(), device_order=20-number)
                if number == 3:
                    task.copy_started = time.monotonic() - 2
                    task.copy_total, task.copy_bytes = 1000, 500
                app.tasks.append(task)
                app.query_one(DataTable).add_row(*([''] * 7), key=str(number))
            app.refresh_status()
            await pilot.pause()
            active = str(app.query_one('#device-active').render())
            self.assertIn('Copying', active)
            self.assertIn('#3', active)
            self.assertIn('50%', active)
            queue = str(app.query_one('#device-queue').render())
            self.assertLess(queue.index('#19'), queue.index('#18'))
            self.assertNotIn('#3 ', queue)
            for width in (80, 120, 160, 60, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                table = app.query_one(pod.TaskTable)
                if width >= 80:
                    self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width)
                self.assertEqual(app.query_one(pod.CommandInput).region.bottom, 23)
                self.assertEqual(app.query_one('#summary').region.y, 23)
                self.assertEqual(app.query_one('#summary').region.bottom, 24)
                self.assertLessEqual(app.query_one('#log').region.bottom, app.query_one('#help').region.y)
                self.assertEqual(app.query_one('#help').region.bottom, app.query_one(pod.CommandInput).region.y)
                fixed_region = app.query_one('#device-transfer').region
                table.move_cursor(row=18)
                await pilot.pause()
                self.assertEqual(app.query_one('#device-transfer').region, fixed_region)
            app.tasks[2].state = 'Waiting for device'
            app.refresh_status()
            self.assertIn('Waiting for device', str(app.query_one('#device-active').render()))
            app.tasks[2].state = 'Done'
            app.tasks[2].finished = time.monotonic()
            app.refresh_status()
            self.assertNotIn('#3 ', str(app.query_one('#device-active').render()))
            await app.action_stop()

    async def test_selected_task_details_are_complete_and_scrollable(self):
        app = pod.PodApp(self.dest)
        async with app.run_test(size=(80, 24)) as pilot:
            task = pod.PodTask(1, 'https://example.com/episode', 12.5, True, 'fixture',
                               title='测试标题', state='Copying', started=time.monotonic() - 4,
                               detail='Copy in progress', audio=self.dist / 'fixture.mp3',
                               title_file=self.dist / 'fixture.txt', copy_started=time.monotonic() - 2,
                               copy_bytes=500, copy_total=1000)
            app.tasks.append(task)
            app.query_one(DataTable).add_row(*([''] * 7), key='1')
            app.refresh_status()
            await pilot.pause()
            detail = str(app.query_one('#task-detail').render())
            for expected in ('测试标题', 'Status: Copying', 'Trim: 12.5s from start',
                             'Elapsed:', 'Copy to device (keep local)', task.url,
                             str(task.audio), str(task.title_file), str(self.dest),
                             'Copy: 50%', 'ETA', 'Details: Copy in progress'):
                self.assertIn(expected, detail)
            pane = app.query_one('#task-detail-pane')
            self.assertGreater(pane.max_scroll_y, 0)
            pane.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(pane.scroll_y, 0)
            app.refresh_status()
            await pilot.pause()
            self.assertGreater(pane.scroll_y, 0)
            local = pod.PodTask(2, 'https://example.com/local', None, False, 'local',
                                state='Done', started=time.monotonic(), finished=time.monotonic())
            app.tasks.append(local)
            app.query_one(DataTable).add_row(*([''] * 7), key='2')
            app.refresh_status()
            app.query_one(DataTable).move_cursor(row=1)
            await pilot.pause()
            detail = str(app.query_one('#task-detail').render())
            self.assertIn('Trim: None', detail)
            self.assertIn('Local only (nomove)', detail)
            self.assertNotIn('Destination:', detail)
            self.assertEqual(pane.scroll_y, 0)
            self.assertEqual([str(c.label) for c in app.query_one(DataTable).ordered_columns],
                             ['ID', 'Status', 'Title / URL', 'Trim', 'Device', 'Elapsed', 'Details'])
            task.state = 'Done'
            task.finished = time.monotonic()
            await app.action_stop()

    async def test_tui_concurrency_fifo_isolation_history_and_names(self):
        class FixtureApp(pod.PodApp):
            active = peak = copying = copy_peak = 0
            copied = []
            async def prepare(self, task):
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.set_state(task, 'Downloading')
                try:
                    await asyncio.sleep({1: .6, 2: .04, 3: .1}.get(task.number, .1))
                    if task.number == 4:
                        raise RuntimeError('fixture failure')
                    self.dist = Path(pod.DIST)
                    self.dist.mkdir(exist_ok=True)
                    task.audio = self.dist / f'{task.stem}.mp3'
                    task.title_file = self.dist / f'{task.stem}.txt'
                    task.audio.write_bytes(b'fixture audio' * 100)
                    task.title_file.write_text(f'Title {task.number}')
                finally:
                    self.active -= 1
            def copy_task(self, task):
                self.copying += 1
                self.copy_peak = max(self.copy_peak, self.copying)
                try:
                    time.sleep(.03)
                    super().copy_task(task)
                    self.copied.append(task.number)
                finally:
                    self.copying -= 1

        app = FixtureApp(self.dest)
        with patch.object(pod.os.path, 'ismount', return_value=True):
            async with app.run_test(size=(120, 35)) as pilot:
                # Submit within one event-loop pass to cover same-millisecond naming.
                for n in range(1, 6):
                    app.on_input_submitted(pod.Input.Submitted(app.query_one(pod.CommandInput), f'https://example.com/{n}'))
                await pilot.pause()
                self.assertEqual(len({t.stem for t in app.tasks}), 5)
                self.assertTrue(all(pod.re.fullmatch(r'\d{4}_\d{6}_\d{3}', t.stem) for t in app.tasks))
                app.query_one(pod.CommandInput).value = 'unfinished typing'
                await self.wait_all(app)
                self.assertEqual(app.query_one(pod.CommandInput).value, 'unfinished typing')
                self.assertEqual(app.peak, 3)
                self.assertEqual(app.copy_peak, 1)
                self.assertEqual(app.copied, [2, 3, 5, 1])
                self.assertEqual(app.tasks[3].state, 'Failed')
                self.assertEqual(app.query_one(DataTable).row_count, 5)
                self.assertEqual(len(list(self.dest.glob('*.mp3'))), 4)
                self.assertEqual(len(list(self.dist.glob('*.mp3'))), 4)
                self.assertIn('Title 5', (self.dest / 'LIST.md').read_text())
                self.assertEqual(len(Path(pod.HISTORY).read_text().splitlines()), 4)
                await pilot.press('up')
                self.assertEqual(app.query_one(pod.CommandInput).value, 'https://example.com/5')
                await pilot.press('down')
                self.assertEqual(app.query_one(pod.CommandInput).value, 'unfinished typing')
                await app.action_stop()

    async def test_real_download_convert_trim_and_wait_for_device_exit(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', str(self.root / 'audio.wav')], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.root / 'audio.wav'), '-b:a', '64k', str(self.root / 'audio.mp3')], check=True)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(self.root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        (self.root / 'page.html').write_text(f'<title>测试 &amp; 标题 | 小宇宙</title><meta property="og:audio" content="{url}/audio.wav">')
        (self.root / 'mp3.html').write_text(f'<title>MP3</title><meta property="og:audio" content="{url}/audio.mp3">')
        mounted = False
        app = pod.PodApp(self.dest)
        try:
            with patch.object(pod.os.path, 'ismount', side_effect=lambda _: mounted):
                async with app.run_test(size=(100, 30)) as pilot:
                    await self.submit(app, pilot, f'{url}/page.html .5')
                    self.assertEqual(len(app.tasks), 0)  # '.5' is rejected; write '0.5'
                    await self.submit(app, pilot, f'{url}/page.html 0.5')
                    await self.submit(app, pilot, f'{url}/mp3.html nomove')
                    await asyncio.wait_for(app.prepare_queue.join(), 10)
                    await pilot.pause()
                    self.assertEqual(app.tasks[0].state, 'Waiting for device')
                    self.assertEqual(app.tasks[1].state, 'Done')
                    self.assertFalse(app.tasks[1].move)
                    self.assertFalse((self.dest / app.tasks[1].audio.name).exists())
                    self.assertEqual(app.tasks[0].title, '测试 & 标题')
                    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(app.tasks[0].audio)]))
                    self.assertTrue(1.4 < duration < 1.8, duration)
                    self.assertEqual(app.tasks[1].audio.read_bytes(), (self.root / 'audio.mp3').read_bytes())
                    await self.submit(app, pilot, '/exit')
                    self.assertTrue(app.closing)
                    self.assertIsNone(app.tasks[0].finished)
                    mounted = True
                    await asyncio.wait_for(app.device_queue.join(), 5)
                    self.assertEqual(app.tasks[0].state, 'Done')
                    self.assertTrue(app.tasks[0].audio.exists())
                    self.assertEqual((self.dest / app.tasks[0].audio.name).read_bytes(), app.tasks[0].audio.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    async def test_ctrl_c_terminates_subprocess_and_cleans_tmp(self):
        class SlowApp(pod.PodApp):
            async def prepare(self, task):
                self.set_state(task, 'Downloading')
                await self.command('python3', '-c', 'import time; time.sleep(60)')
        app = SlowApp(self.dest)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.submit(app, pilot, 'https://example.com/slow')
            self.assertEqual(app.query_one(pod.CommandInput).region.bottom, 23)
            started = time.monotonic()
            await pilot.press('ctrl+c')
            await pilot.pause()
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(app.tasks[0].state, 'Stopped')
            self.assertTrue(all(t.done() for t in app.runners))

    async def test_copy_stop_cleans_part_preserves_local(self):
        task = pod.PodTask(1, 'https://example.com', None, True, 'sample')
        self.dist.mkdir()
        task.audio = self.dist / 'sample.mp3'
        task.title_file = self.dist / 'sample.txt'
        task.audio.write_bytes(b'a' * 100)
        task.title_file.write_text('Sample')
        app = pod.PodApp(self.dest)
        original_fsync = os.fsync
        def interrupt(fd):
            original_fsync(fd)
            app.stop_event.set()
        with patch.object(pod.os.path, 'ismount', return_value=True), patch.object(pod.os, 'fsync', side_effect=interrupt):
            with self.assertRaises(InterruptedError):
                app.copy_task(task)
        self.assertFalse(list(self.dest.iterdir()))
        self.assertTrue(task.audio.exists())
        self.assertFalse(Path(pod.HISTORY).exists())

    async def test_successful_copy_records_task_metadata_and_preserves_totals(self):
        self.dist.mkdir()
        legacy = self.root / '.move_history.jsonl'
        legacy.write_text('{"cum_bytes": 1000, "cum_duration": 2}\n')
        app = pod.PodApp(self.dest)
        for number, trim in enumerate((None, 0.0, 0.5), 1):
            task = pod.PodTask(number, f'https://example.com/episode/{number}?source=test',
                               trim, True, f'sample{number}', title='播客标题')
            task.audio = self.dist / f'{task.stem}.mp3'
            task.title_file = self.dist / f'{task.stem}.txt'
            task.audio.write_bytes(b'a' * 100)
            task.title_file.write_text(task.title, encoding='utf-8')
            with patch.object(pod.os.path, 'ismount', return_value=True):
                app.copy_task(task)
            records = [json.loads(line) for line in Path(pod.HISTORY).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records), number + 1)
            record = records[-1]
            self.assertEqual(record['schema_version'], 2)
            self.assertEqual(record['url'], task.url)
            self.assertEqual(record['trim_seconds'], trim)
            self.assertEqual(record['title'], task.title)
            self.assertEqual(record['filename'], task.audio.name)
            self.assertEqual(record['title_filename'], task.title_file.name)
            self.assertEqual(record['audio_path'], str(task.audio.resolve()))
            self.assertEqual(record['title_path'], str(task.title_file.resolve()))
            self.assertEqual(record['destination'], str(self.dest.resolve()))
            self.assertEqual(record['size_bytes'], 100)
            self.assertEqual(record['cum_bytes'], 1000 + number * 100)
            self.assertGreaterEqual(record['cum_duration'], 2)
            self.assertEqual((self.dest / task.audio.name).read_bytes(), task.audio.read_bytes())
        self.assertFalse(legacy.exists())
        self.assertEqual(pod.read_last_cumulative(), (record['cum_bytes'], record['cum_duration']))

    async def test_ctrl_c_waits_for_active_copy_cleanup(self):
        class ReadyApp(pod.PodApp):
            async def prepare(self, task):
                Path(pod.DIST).mkdir(exist_ok=True)
                task.audio = Path(pod.DIST) / f'{task.stem}.mp3'
                task.title_file = Path(pod.DIST) / f'{task.stem}.txt'
                task.audio.write_bytes(b'a' * 100)
                task.title_file.write_text('Sample')
        app = ReadyApp(self.dest)
        copying = threading.Event()
        def pause_fsync(fd):
            copying.set()
            if not app.stop_event.wait(5):
                raise TimeoutError('stop was not received')
        with patch.object(pod.os.path, 'ismount', return_value=True), patch.object(pod.os, 'fsync', side_effect=pause_fsync):
            async with app.run_test() as pilot:
                await self.submit(app, pilot, 'https://example.com/copy')
                self.assertTrue(await asyncio.to_thread(copying.wait, 2))
                await asyncio.wait_for(app.action_stop(), 3)
                self.assertEqual(app.tasks[0].state, 'Stopped')
                self.assertFalse(list(self.dest.iterdir()))
                self.assertTrue(app.tasks[0].audio.exists())
                self.assertTrue(all(t.done() for t in app.runners))


if __name__ == '__main__':
    unittest.main()
