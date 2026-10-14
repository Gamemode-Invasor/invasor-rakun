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
UNINSTALLED_EVENT = 'gameStatusUpdate [{"appName":"%s","runner":"nile","status":"uninstalled"}]' % APP
IMPORTING = 'gameStatusUpdate [{"appName":"%s","runner":"nile","status":"importing"}]' % APP
LIBRARY = ('pushGameToLibrary [{"runner":"nile","app_name":"%s","title":"A Tiny Sticker Tale",'
           '"art_square":"https://img/sq.jpg","is_installed":false}]' % APP)
QUEUE_ART = QUEUE.replace('"title"', '"art_square":"https://img/sq.jpg","art_cover":"https://img/co.jpg","title"')


class EventTest(unittest.TestCase):
    def test_done_after_a_queue_entry_is_a_download(self):
        f = logs.Formatter()
        f.text(QUEUE_ART)
        self.assertEqual(tuple(f.event(STATUS)), ("start", APP, "Rakun: Installing A Tiny Sticker Tale", None, "Starting", False, ""))
        self.assertEqual(tuple(f.event(DONE)), ("done", APP, "Rakun: Installing A Tiny Sticker Tale", 1.0, "Installed", True, ""))

    def test_an_uninstallation_is_a_notice_with_no_task(self):
        f = logs.Formatter()
        f.text(LIBRARY)
        self.assertIsNone(f.event(UNINSTALLING))
        self.assertEqual(tuple(f.event(DONE)),
                         ("notice", APP, "Rakun: A Tiny Sticker Tale", None, "Uninstalled", False, "https://img/sq.jpg"))
        self.assertIsNone(f.event(DONE_AGAIN))

    def test_an_import_is_a_notice_with_no_task(self):
        f = logs.Formatter()
        f.text(LIBRARY)
        self.assertIsNone(f.event(IMPORTING))
        self.assertEqual(tuple(f.event(DONE)),
                         ("notice", APP, "Rakun: A Tiny Sticker Tale", None, "Imported", True, "https://img/sq.jpg"))

    def test_a_repeated_done_still_closes_a_task_but_asks_for_no_refresh(self):
        f = logs.Formatter()
        f.event(UNINSTALLING)
        self.assertEqual(f.event(UNINSTALLED_EVENT, now=lambda: 100).kind, "notice")
        f.event(STATUS)  # installed again at once: its done falls inside the window
        again = f.event(DONE, now=lambda: 101)
        self.assertEqual((again.kind, again.installed), ("done", False))
        self.assertIsNone(f.event(DONE_AGAIN, now=lambda: 102))  # nothing is left open

    def test_progress_after_the_end_does_not_reopen_the_game(self):
        f = logs.Formatter()
        f.event(STATUS)
        self.assertEqual(f.event(DONE, now=lambda: 100).kind, "done")
        self.assertIsNone(f.event(PROGRESS, now=lambda: 101))
        self.assertEqual(f.event(PROGRESS, now=lambda: 100 + logs.LATE_PROGRESS_SECONDS).kind, "progress")

    def test_done_with_the_queue_paused_is_a_pause_without_refresh(self):
        f = logs.Formatter()
        f.event(STATUS)
        self.assertIsNone(f.event('changedDMQueueInformation [[],"paused"]'))
        got = f.event(DONE)
        self.assertEqual((got.kind, got.text, got.installed), ("fail", "Paused", False))
        f.event('changedDMQueueInformation [[],"running"]')
        f.event(STATUS)
        self.assertEqual(f.event(DONE, now=lambda: 1e9).kind, "done")

    def test_uninstalled_alone_is_enough_and_done_after_it_is_not_repeated(self):
        f = logs.Formatter()
        f.event(UNINSTALLING)
        self.assertEqual(f.event(UNINSTALLED_EVENT, now=lambda: 100).kind, "notice")
        self.assertIsNone(f.event(DONE, now=lambda: 101))  # not notified twice

    def test_progress_is_a_fraction_with_the_details(self):
        got = logs.Formatter().event(PROGRESS)
        self.assertEqual((got.kind, got.fraction, got.text), ("progress", 0.0059, "4.27MB · ↓ 4.25 MB/s · ETA 00:05:23"))

    def test_the_percentage_may_be_a_string_and_is_kept_in_range(self):
        for percent, want in (('"50"', 0.5), ('"50%"', 0.5), ("150", 1.0), ("-3", 0.0), ('"x"', None), ("true", None),
                              ("null", None)):
            line = 'progressUpdate [{"appName":"a","status":"installing","progress":{"percent":%s}}]' % percent
            self.assertEqual(logs.Formatter().event(line).fraction, want, percent)
        self.assertIsNone(logs.Formatter().event('progressUpdate [{"appName":"a","status":"x"}]').fraction)

    def test_failures_end_the_task_with_a_short_text(self):
        for status, text in (("error", "Failed"), ("canceled", "Cancelled"), ("aborted", "Cancelled")):
            got = logs.Formatter().event('gameStatusUpdate [{"appName":"a","status":"%s"}]' % status)
            self.assertEqual((got.kind, got.text), ("fail", text))

    def test_each_operation_is_told_in_its_own_words(self):
        for status, doing, did in (("updating", "Updating", "Updated"), ("repairing", "Repairing", "Repaired"),
                                   ("moving", "Moving", "Moved")):
            f = logs.Formatter()
            started = f.event('gameStatusUpdate [{"appName":"a","status":"%s"}]' % status)
            self.assertEqual(started.title, "Rakun: %s a" % doing)
            finished = f.event('gameStatusUpdate [{"appName":"a","status":"done"}]')
            self.assertEqual((finished.title, finished.text), ("Rakun: %s a" % doing, did))

    def test_a_progress_without_a_start_names_the_operation_from_its_own_status(self):
        got = logs.Formatter().event('progressUpdate [{"appName":"a","status":"updating","progress":{"percent":1}}]')
        self.assertEqual(got.title, "Rakun: Updating a")

    def test_a_long_title_gives_way_but_the_prefix_and_the_verb_stay(self):
        label = logs.texts.label_of("Installing", "x" * 200)
        self.assertEqual(len(label), logs.texts.MAX_LABEL)
        self.assertTrue(label.startswith("Rakun: Installing x") and label.endswith("…"))
        self.assertEqual(len(logs.texts.title_of("y" * 200)), logs.texts.MAX_LABEL)

    def test_the_library_event_is_readable(self):
        self.assertEqual(logs.Formatter().text(LIBRARY), "library: A Tiny Sticker Tale (nile)")

    def test_done_twice_is_reported_once_until_the_window_passes(self):
        f = logs.Formatter()
        self.assertIsNotNone(f.event(DONE, now=lambda: 100))
        self.assertIsNone(f.event(DONE_AGAIN, now=lambda: 101))
        self.assertIsNotNone(f.event(DONE, now=lambda: 100 + logs.DUPLICATE_SECONDS))

    def test_other_events_are_ignored(self):
        f = logs.Formatter()
        for line in (QUEUE, "rakunctl: aborted", "gameStatusUpdate []", "progressUpdate []",
                     'gameStatusUpdate [{"appName":"x","status":"queued"}]'):
            self.assertIsNone(f.event(line))

    def test_unknown_games_use_the_app_name(self):
        self.assertEqual(logs.Formatter().event(DONE).title, "Rakun: Installing " + APP)


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

    def test_events_reach_the_callback_and_a_failing_one_does_not_stop_the_reading(self):
        seen = []

        def on_event(event):
            seen.append(event.kind)
            raise RuntimeError("boom")

        reader = logs.LogReader(on_event)
        script = "print(%r); print(%r); print(%r)" % (QUEUE, DONE, DONE_AGAIN)
        reader.start([sys.executable, "-c", script])
        reader._thread.join(5)
        self.assertEqual(seen, ["done", "stopped"])
        self.assertEqual(len(reader.lines()[1]), 4)

    def test_stop_ends_a_running_stream(self):
        reader = logs.LogReader()
        reader.start([sys.executable, "-c", "import time; print('x', flush=True); time.sleep(60)"])
        self.assertTrue(reader.alive())
        reader.stop()
        self.assertFalse(reader.alive())


if __name__ == "__main__":
    unittest.main()
