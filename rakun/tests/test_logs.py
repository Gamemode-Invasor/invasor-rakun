import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rakun import logs  # noqa: E402

APP = "amzn1.adg.product.ec0f0479"
QUEUE = ('changedDMQueueInformation [[{"params":{"appName":"%s","runner":"nile","gameInfo":{"app_name":"%s",'
         '"title":"A Tiny Sticker Tale"}}}],"running"]' % (APP, APP))
STATUS = 'gameStatusUpdate [{"appName":"%s","runner":"nile","status":"installing"}]' % APP
PROGRESS = ('progressUpdate [{"appName":"%s","runner":"nile","status":"installing","progress":{"bytes":"4.27MB",'
            '"eta":"00:05:23","percent":0.59,"diskSpeed":4.25,"downSpeed":4.25}}]' % APP)


def body(line):
    return line.split(" ", 1)[1]


class FormatTest(unittest.TestCase):
    def test_events_are_readable_and_use_the_titles_learnt(self):
        f = logs.Formatter()
        self.assertEqual(f.text(QUEUE), "queue: 1 (running) A Tiny Sticker Tale")
        self.assertEqual(f.text(STATUS), "installing · A Tiny Sticker Tale (nile)")
        self.assertEqual(f.text(PROGRESS),
                         "nile · A Tiny Sticker Tale · installing 0.59% · 4.27MB · ↓ 4.25 MB/s · ETA 00:05:23")
        self.assertEqual(f.text('changedDMQueueInformation [[],"idle"]'), "queue: 0 (idle)")

    def test_unknown_titles_fall_back_to_the_app_name(self):
        self.assertEqual(logs.Formatter().text(STATUS), "installing · %s (nile)" % APP)

    def test_anything_else_is_shown_as_it_comes(self):
        f = logs.Formatter()
        self.assertEqual(f.text("rakunctl: aborted"), "rakunctl: aborted")
        self.assertEqual(f.text('other {"a":1}'), 'other {"a":1}')
        self.assertEqual(f.text("progressUpdate []"), "progressUpdate []")
        self.assertLessEqual(len(f.text("x " + "y" * 500)), logs.MAX_TEXT)

    def test_format_event_adds_the_time_and_skips_blank_lines(self):
        line = logs.format_event(STATUS + "\n", now=0)
        self.assertRegex(line, r"^\d\d:\d\d:\d\d installing")
        self.assertIsNone(logs.format_event("  \n"))


DONE = 'gameStatusUpdate [{"appName":"%s","runner":"nile","status":"done"}]' % APP
DONE_AGAIN = 'gameStatusUpdate [{"appName":"%s","status":"done"}]' % APP
UNINSTALLING = 'gameStatusUpdate [{"appName":"%s","runner":"nile","status":"uninstalling"}]' % APP
LIBRARY = ('pushGameToLibrary [{"runner":"nile","app_name":"%s","title":"A Tiny Sticker Tale",'
           '"art_square":"https://img/sq.jpg","is_installed":false}]' % APP)
QUEUE_ART = QUEUE.replace('"title"', '"art_square":"https://img/sq.jpg","art_cover":"https://img/co.jpg","title"')


class NoticeTest(unittest.TestCase):
    def test_done_after_a_queue_entry_is_a_download(self):
        f = logs.Formatter()
        f.text(QUEUE_ART)
        f.notice(STATUS)
        got = f.notice(DONE)
        self.assertEqual(tuple(got), ("Download finished", "A Tiny Sticker Tale", "https://img/sq.jpg", APP, True))

    def test_done_after_uninstalling_is_an_uninstallation(self):
        f = logs.Formatter()
        self.assertIsNone(f.notice(UNINSTALLING))
        f.text(LIBRARY)
        self.assertEqual(tuple(f.notice(DONE)),
                         ("Game uninstalled", "A Tiny Sticker Tale", "https://img/sq.jpg", APP, False))
        self.assertIsNone(f.notice(DONE_AGAIN))

    def test_the_library_event_is_readable(self):
        self.assertEqual(logs.Formatter().text(LIBRARY), "library: A Tiny Sticker Tale (nile)")

    def test_done_twice_is_noticed_once_until_the_window_passes(self):
        f = logs.Formatter()
        self.assertIsNotNone(f.notice(DONE, now=lambda: 100))
        self.assertIsNone(f.notice(DONE_AGAIN, now=lambda: 101))
        self.assertIsNotNone(f.notice(DONE, now=lambda: 100 + logs.DUPLICATE_SECONDS))

    def test_other_events_are_not_noticed(self):
        f = logs.Formatter()
        for line in (STATUS, PROGRESS, QUEUE, "rakunctl: aborted", "gameStatusUpdate []",
                     'gameStatusUpdate [{"appName":"x","status":"queued"}]'):
            self.assertIsNone(f.notice(line))

    def test_unknown_games_use_the_app_name_and_no_icon(self):
        self.assertEqual(tuple(logs.Formatter().notice(DONE))[:3], ("Download finished", APP, ""))

    def test_the_icon_is_the_logo_then_the_square_then_the_cover(self):
        art = {"art_logo": "https://img/logo.png", "art_square": "https://img/sq.jpg", "art_cover": "https://img/co.jpg"}
        for drop, want in ((None, "logo.png"), ("art_logo", "sq.jpg"), ("art_square", "co.jpg")):
            if drop:
                art.pop(drop)
            f = logs.Formatter()
            f.learn({"app_name": APP, "title": "T", **art})
            self.assertTrue(f.notice(DONE)[2].endswith(want))

    def test_a_icon_that_is_not_https_is_dropped(self):
        f = logs.Formatter()
        f.text(QUEUE.replace('"title"', '"art_square":"http://img/sq.jpg","title"'))
        self.assertEqual(f.notice(DONE)[2], "")


class ReaderTest(unittest.TestCase):
    def run_reader(self, script):
        reader = logs.LogReader()
        reader.start([sys.executable, "-c", script])
        reader._thread.join(5)
        return reader

    def test_collects_the_lines_and_marks_the_end(self):
        reader = self.run_reader("print('a 1'); print('b 2')")
        self.assertFalse(reader.alive())
        next_id, lines = reader.lines()
        self.assertEqual([body(x) for x in lines], ["a 1", "b 2", logs.STOPPED])
        self.assertEqual(next_id, 3)
        self.assertEqual([body(x) for x in reader.lines(2)[1]], [logs.STOPPED])
        self.assertEqual(reader.lines(3)[1], [])

    def test_keeps_only_the_last_lines_and_can_be_cleared(self):
        reader = self.run_reader("[print('e', i) for i in range(%d)]" % (logs.MAX_LINES + 20))
        self.assertEqual(len(reader.lines()[1]), logs.MAX_LINES)
        reader.clear()
        self.assertEqual(reader.lines()[1], [])

    def test_notices_reach_the_callback_and_a_failing_one_does_not_stop_the_reading(self):
        seen = []

        def on_notice(notice):
            seen.append(tuple(notice)[:3])
            raise RuntimeError("boom")

        reader = logs.LogReader(on_notice)
        script = "print(%r); print(%r); print(%r)" % (QUEUE, DONE, DONE_AGAIN)
        reader.start([sys.executable, "-c", script])
        reader._thread.join(5)
        self.assertEqual(seen, [("Download finished", "A Tiny Sticker Tale", "")])
        self.assertEqual(len(reader.lines()[1]), 4)

    def test_stop_ends_a_running_stream(self):
        reader = logs.LogReader()
        reader.start([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(60)"])
        self.assertTrue(reader.alive())
        reader.stop()
        self.assertFalse(reader.alive())


if __name__ == "__main__":
    unittest.main()
