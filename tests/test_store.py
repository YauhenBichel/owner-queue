import json
import os
import subprocess
import sys
import unittest

from helpers import BIN, PR, Case, bash_done

from owner_queue import config, text
from owner_queue.store import DONE, Queue, QueueError


def pr_item(number: int, **extra) -> dict:
    url = f"{PR}{number}"
    data = {"kind": "pr", "key": text.pr_key(url), "url": url, "urls": [url], "repo": "example/repo", "number": number}
    data.update(extra)
    return data


class Replay(Case):
    def test_items_come_back_oldest_first_from_a_fresh_process(self):
        q = self.queue()
        ids = [q.add({"kind": "manual", "text": f"note {i}"})[0] for i in range(5)]
        self.assertEqual([i.id for i in Queue(self.dir).load().open_items()], ids)

    def test_done_skip_and_reopen_replay(self):
        q = self.queue()
        a, b, c = (q.add({"kind": "manual", "text": t})[0] for t in "abc")
        self.assertTrue(q.mark("skip", a))
        self.assertTrue(q.mark("done", b))
        state = Queue(self.dir).load()
        self.assertEqual([i.id for i in state.open_items()], [c, a])
        self.assertEqual(state.items[a].skips, 1)
        self.assertEqual(state.items[b].state, DONE)
        self.assertFalse(q.mark("done", b), "closing twice changes nothing")
        self.assertTrue(q.mark("reopen", b))
        self.assertEqual([i.id for i in Queue(self.dir).load().open_items()], [c, a, b])

    def test_order_follows_the_log_not_the_clock(self):
        q = self.queue()
        first = q.add({"kind": "manual", "text": "first"})[0]
        self.clock.now -= 3600   # the clock jumps back
        second = q.add({"kind": "manual", "text": "second"})[0]
        self.assertEqual([i.id for i in q.load().open_items()], [first, second])

    def test_many_events_in_the_same_second_get_distinct_ids(self):
        q = self.queue()
        ids = {q.add({"kind": "manual", "text": f"n{i}"})[0] for i in range(300)}
        self.assertEqual(len(ids), 300)
        self.assertEqual(len(q.load().open_items()), 300)

    def test_waiting_items_of_a_dead_session_expire(self):
        q = self.queue()
        q.add({"kind": "waiting", "key": "waiting:s:", "session": "s", "text": "x"})
        q.add({"kind": "manual", "text": "stays"})
        self.clock.now += config.WAITING_TTL + 1
        self.assertEqual([i.kind for i in q.load().open_items()], ["manual"])


class Dedupe(Case):
    def test_same_pull_request_is_one_item(self):
        q = self.queue()
        first, created = q.add(pr_item(7))
        self.assertTrue(created)
        again, created = q.add(pr_item(7))
        self.assertEqual((again, created), (first, False))
        self.assertEqual(len(q.load().open_items()), 1)
        self.assertEqual(self.log().count('"op":"add"'), 1, "an identical item is not written again")

    def test_key_ignores_case_of_host_owner_and_repo(self):
        self.assertEqual(text.pr_key("https://GitHub.com/Example/Repo/pull/7"), text.pr_key(PR + "7"))
        self.assertNotEqual(text.pr_key(PR + "7"), text.pr_key(PR + "70"))

    def test_a_closed_pull_request_is_not_queued_again(self):
        q = self.queue()
        first, _ = q.add(pr_item(7))
        q.mark("done", first)
        again, created = q.add(pr_item(7, title="mentioned again"))
        self.assertEqual((again, created), (first, False))
        self.assertEqual(q.load().open_items(), [])

    def test_newer_version_of_a_keyed_item_keeps_id_and_place(self):
        q = self.queue()
        review, _ = q.add({"kind": "review", "key": "review:s", "session": "s", "text": "old"})
        other, _ = q.add({"kind": "manual", "text": "later"})
        same, created = q.add({"kind": "review", "key": "review:s", "session": "s", "text": "new"})
        self.assertEqual((same, created), (review, False))
        items = q.load().open_items()
        self.assertEqual([(i.id, i.text) for i in items], [(review, "new"), (other, "later")])

    def test_hooks_stop_adding_when_the_queue_is_full(self):
        q = self.queue()
        old = config.MAX_OPEN
        config.MAX_OPEN = 3
        self.addCleanup(setattr, config, "MAX_OPEN", old)
        for i in range(5):
            q.add({"kind": "review", "key": f"review:{i}", "session": str(i), "text": "x"}, limit=True)
        self.assertEqual(len(q.load().open_items()), 3)
        self.assertEqual(q.add({"kind": "manual", "text": "the owner can still add"})[1], True)


class Corruption(Case):
    def _write(self, raw: bytes) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "events.jsonl"), "ab") as f:
            f.write(raw)

    def test_one_corrupt_line_does_not_hide_the_rest(self):
        q = self.queue()
        a = q.add({"kind": "manual", "text": "before"})[0]
        self._write(b"{not json\n\xff\xfe\x00garbage\n[1,2,3]\n\"text\"\n{}\n"
                    b'{"op":"add","id":"zz","ts":"soon","item":{"kind":"manual"}}\n'
                    b'{"op":"add","id":"yy","ts":1,"item":{"kind":"nonsense"}}\n'
                    b'{"op":"explode","id":"yy","ts":1}\n')
        b = q.add({"kind": "manual", "text": "after"})[0]
        state = Queue(self.dir).load()
        self.assertEqual([i.id for i in state.open_items()], [a, b])
        self.assertEqual(state.corrupt, 8)

    def test_an_overlong_line_is_skipped(self):
        q = self.queue()
        a = q.add({"kind": "manual", "text": "ok"})[0]
        big = json.dumps({"op": "add", "id": "big", "ts": 1, "item": {"kind": "manual", "text": "x" * 10000}})
        self._write(big.encode() + b"\n")
        self.assertEqual([i.id for i in q.load().open_items()], [a])

    def test_a_torn_last_line_loses_only_itself(self):
        q = self.queue()
        a = q.add({"kind": "manual", "text": "ok"})[0]
        self._write(b'{"op":"add","id":"torn","ts":1,"item":{"kind":"man')   # the writer died here
        b = q.add({"kind": "manual", "text": "next"})[0]
        self.assertEqual([i.id for i in q.load().open_items()], [a, b])

    def test_wrong_types_inside_an_item_are_dropped_not_trusted(self):
        self._write(b'{"op":"add","id":"aa","ts":5,"item":{"kind":"pr","url":["x"],"urls":"nope","number":"7",'
                    b'"text":{"a":1},"cwd":17}}\n')
        (item,) = Queue(self.dir).load().open_items()
        self.assertEqual((item.url, item.urls, item.text, item.cwd), ("", [], "", ""))

    def test_an_empty_or_missing_directory_is_an_empty_queue(self):
        self.assertEqual(Queue(os.path.join(self.tmp, "nowhere")).load().open_items(), [])


class Compaction(Case):
    def setUp(self):
        super().setUp()
        old = config.COMPACT_BYTES
        config.COMPACT_BYTES = 4000
        self.addCleanup(setattr, config, "COMPACT_BYTES", old)

    def size(self) -> int:
        return os.path.getsize(os.path.join(self.dir, "events.jsonl"))

    def test_the_log_does_not_grow_without_bound(self):
        q = self.queue()
        keep = q.add({"kind": "manual", "text": "stays open"})[0]
        largest = 0
        for i in range(400):
            self.clock.now += 86400   # closed items age out of the kept history
            item = q.add({"kind": "review", "key": f"review:{i}", "session": str(i), "text": "x" * 100})[0]
            q.mark("done", item)
            largest = max(largest, self.size())
        self.assertLess(largest, 40_000)
        self.assertEqual([i.id for i in Queue(self.dir).load().open_items()], [keep])

    def test_compaction_keeps_open_items_order_skips_and_closed_pull_requests(self):
        q = self.queue()
        a = q.add({"kind": "manual", "text": "a"})[0]
        b = q.add({"kind": "manual", "text": "b"})[0]
        pr, _ = q.add(pr_item(9))
        q.mark("skip", a)
        q.mark("done", pr)
        kept = q.compact()
        self.assertEqual(kept, 3)
        state = Queue(self.dir).load()
        self.assertEqual([i.id for i in state.open_items()], [b, a])
        self.assertEqual(state.items[a].skips, 1)
        self.assertEqual(q.add(pr_item(9))[1], False, "a merged pull request stays known after compaction")
        self.assertEqual(self.log().count("\n"), 4)   # three items and the closing of one

    def test_compaction_drops_corrupt_lines(self):
        q = self.queue()
        q.add({"kind": "manual", "text": "a"})
        with open(os.path.join(self.dir, "events.jsonl"), "ab") as f:
            f.write(b"garbage\n")
        q.compact()
        self.assertEqual(Queue(self.dir).load().corrupt, 0)


class Filesystem(Case):
    def test_a_symlinked_log_is_not_followed(self):
        os.makedirs(self.dir)
        target = os.path.join(self.tmp, "elsewhere.txt")
        with open(target, "w") as f:
            f.write("precious\n")
        os.symlink(target, os.path.join(self.dir, "events.jsonl"))
        with self.assertRaises(QueueError):
            self.queue().add({"kind": "manual", "text": "x"})
        with self.assertRaises(QueueError):
            self.queue().load()
        with open(target) as f:
            self.assertEqual(f.read(), "precious\n")

    def test_a_symlinked_lock_or_cursor_is_not_followed(self):
        os.makedirs(self.dir)
        target = os.path.join(self.tmp, "elsewhere.txt")
        with open(target, "w") as f:
            f.write("precious\n")
        os.symlink(target, os.path.join(self.dir, "cursor"))
        q = self.queue()
        item = q.add({"kind": "manual", "text": "x"})[0]
        q.set_shown(item)
        with open(target) as f:
            self.assertEqual(f.read(), "precious\n")
        os.symlink(target, os.path.join(self.dir, "lock2"))
        with self.assertRaises(QueueError):
            q.lock(0.1, "lock2")

    def test_a_fifo_in_place_of_the_log_does_not_hang(self):
        os.makedirs(self.dir)
        os.mkfifo(os.path.join(self.dir, "events.jsonl"))
        with self.assertRaises(QueueError):
            self.queue().load()
        with self.assertRaises(QueueError):
            self.queue().add({"kind": "manual", "text": "x"})

    def test_files_are_private(self):
        q = self.queue()
        q.set_shown(q.add({"kind": "manual", "text": "x"})[0])
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        for name in os.listdir(self.dir):
            self.assertEqual(os.stat(os.path.join(self.dir, name)).st_mode & 0o077, 0, name)

    def test_an_unusable_directory_is_an_error_not_a_crash(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        q = Queue(os.path.join(blocker, "queue"))
        with self.assertRaises(QueueError):
            q.add({"kind": "manual", "text": "x"})
        with self.assertRaises(QueueError):
            q.load()


class Sizes(Case):
    def test_caps_are_in_bytes_and_never_split_a_character(self):
        value = text.cap(chr(0xE9) * 500, 101)
        self.assertLessEqual(len(value.encode()), 101)
        value.encode().decode()   # still valid UTF-8
        tail = text.cap("start " + chr(0x4E16) * 400 + " the end?", 60, keep_end=True)
        self.assertLessEqual(len(tail.encode()), 60)
        self.assertTrue(tail.endswith("the end?"))

    def test_control_and_invisible_characters_are_removed(self):
        dirty = "a" + chr(0x1B) + "[31mb" + chr(0x202E) + "c" + chr(0x200B) + "d" + chr(0) + "e"
        self.assertEqual(text.clean(dirty), "a [31mb c d e")

    def test_a_too_long_url_is_dropped_not_cut(self):
        long_url = "https://example.com/" + "a/" * 400
        self.assertEqual(text.urls(f"see {long_url} and https://example.com/ok.", config.MAX_URL, 5), ["https://example.com/ok"])

    def test_tokens_are_redacted(self):
        samples = [
            "ghp_" + "A1b2" * 9,
            "github_pat_" + "A1b2C3d4" * 5,
            "sk-" + "ant-" + "abcDEF123456" * 3,
            "AKIA" + "ABCDEFGHIJKLMNOP",
            "xoxb-" + "1234567890-" + "abcdefghijkl",
            "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abcDEF123456_-xyz",
            "a1b2c3d4" * 5,
        ]
        for secret in samples:
            got = text.field(f"use {secret} now", config.MAX_TEXT)
            self.assertNotIn(secret, got, secret[:8])
            self.assertIn(text.REDACTED, got)
        got = text.field("curl -H 'Authorization: Bearer " + "abc123def456ghi789" + "' and password=" + "hunter2hunter2", 400)
        self.assertNotIn("abc123def456ghi789", got)
        self.assertNotIn("hunter2hunter2", got)
        got = text.field("clone https://user:" + "s3cretpass" + "@example.com/x.git?token=" + "tok12345", 400)
        self.assertNotIn("s3cretpass", got)
        self.assertNotIn("tok12345", got)

    def test_ordinary_text_is_left_alone(self):
        plain = "Opened https://github.com/example/repo/pull/12 for src/owner_queue/store.py; commit 1a2b3c4d."
        self.assertEqual(text.field(plain, 400), plain)


class Concurrency(Case):
    N = 24

    def _together(self, commands: list) -> list:
        """Start every command at once and wait for all of them."""
        env = dict(self.env)
        procs = [
            subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            for cmd, _ in commands
        ]
        for proc, (_, stdin) in zip(procs, commands):
            proc.stdin.write(stdin)
            proc.stdin.close()
        results = []
        for proc in procs:
            out, err = proc.stdout.read(), proc.stderr.read()
            results.append((proc.wait(60), out, err))
            proc.stdout.close()
            proc.stderr.close()
        return results

    def test_processes_adding_at_once_lose_and_duplicate_nothing(self):
        commands = [([sys.executable, BIN, "add", f"note {i:02d}"], b"") for i in range(self.N)]
        results = self._together(commands)
        self.assertEqual([r[0] for r in results], [0] * self.N, results)
        items = self.items()
        self.assertEqual(sorted(i.text for i in items), [f"note {i:02d}" for i in range(self.N)])
        self.assertEqual(len({i.id for i in items}), self.N)
        printed = sorted(r[1].decode().split()[1] for r in results)
        self.assertEqual(printed, sorted(i.id for i in items))
        for line in self.log().splitlines():
            json.loads(line)   # no interleaved or torn line

    def test_hooks_reporting_the_same_pull_request_at_once_make_one_item(self):
        event = json.dumps(bash_done('gh pr create --title "Same"', PR + "5\n")).encode()
        results = self._together([([sys.executable, BIN, "hook"], event) for _ in range(self.N)])
        self.assertEqual([r[0] for r in results], [0] * self.N)
        self.assertEqual([i.url for i in self.items()], [PR + "5"])
        self.assertEqual(self.log().count('"op":"add"'), 1)

    def test_hooks_and_commands_mixed(self):
        commands = []
        for i in range(self.N):
            commands.append(([sys.executable, BIN, "add", f"note {i}"], b""))
            event = bash_done("gh pr create --fill", f"{PR}{100 + i}\n", session_id=f"session-{i}")
            commands.append(([sys.executable, BIN, "hook"], json.dumps(event).encode()))
        results = self._together(commands)
        self.assertEqual({r[0] for r in results}, {0})
        items = self.items()
        self.assertEqual(len(items), 2 * self.N)
        self.assertEqual(len({i.id for i in items}), 2 * self.N)
        self.assertEqual(sorted(i.number for i in items if i.kind == "pr"), list(range(100, 100 + self.N)))


if __name__ == "__main__":
    unittest.main()
