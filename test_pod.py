"""隔离临时目录和模拟Device；不接触真实Device或既有输出。"""
import asyncio
import functools
import http.server
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


class PodTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.dest = self.root / 'device'
        self.dest.mkdir()
        self.dist = self.root / 'dist'
        self.patches = [patch.object(pod, 'DIST', str(self.dist)),
                        patch.object(pod, 'TMP', str(self.root / 'tmp')),
                        patch.object(pod, 'HISTORY', str(self.root / 'history.jsonl'))]
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
                # 同一次事件循环中连续提交，覆盖同毫秒命名。
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
                    self.assertEqual(len(app.tasks), 0)  # 原参数格式要求 0.5
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
