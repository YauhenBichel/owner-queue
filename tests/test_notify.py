"""Phone notifications, against a fake ntfy server on a free local port and a made-up topic.

No test here talks to a real ntfy server or reads a real topic file.
"""

import http.server
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from helpers import PR, Case, bash_done, notification, stop

from owner_queue import text
from owner_queue.notify import Notifier

TOPIC = "fake-topic-Zq7Xw2Lm9"


class FakeNtfy:
    """Records every request. mode: ok | error | slow | redirect."""

    def __init__(self, mode: str = "ok", redirect_to: str = ""):
        self.requests = []
        self.mode = mode
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
                outer.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                if outer.mode == "slow":
                    time.sleep(3)
                if outer.mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", redirect_to + self.path)
                    self.end_headers()
                    return
                self.send_response(500 if outer.mode == "error" else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"x"}')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NotifyCase(Case):
    def setUp(self):
        super().setUp()
        self.ntfy = FakeNtfy()
        self.addCleanup(self.ntfy.close)
        self.topic_file = os.path.join(self.tmp, "topic")
        with open(self.topic_file, "w") as f:
            f.write(TOPIC + "\n")
        os.chmod(self.topic_file, 0o600)

    def notifier(self, **changes) -> Notifier:
        base = {"ntfy_url": self.ntfy.url, "ntfy_topic_file": self.topic_file, "ntfy_timeout": 2.0}
        base.update(changes)
        return Notifier(self.queue(), self.settings(**base), clock=self.clock)

    def pr(self, number: int, title: str = "Add the thing"):
        url = f"{PR}{number}"
        return self.queue().add({"kind": "pr", "key": text.pr_key(url), "url": url, "urls": [url],
                                 "repo": "example/repo", "number": number, "title": title,
                                 "session": "sess-1", "cwd": "/work/alpha/app"})[0]

    def waiting(self, session: str = "sess-2"):
        return self.queue().add({"kind": "waiting", "key": f"waiting:{session}:", "ntype": "permission_prompt",
                                 "text": "Claude needs your permission to use Bash", "session": session,
                                 "cwd": "/work/alpha/app"})[0]

    def review(self, session: str = "sess-3"):
        return self.queue().add({"kind": "review", "key": f"review:{session}", "session": session,
                                 "cwd": "/work/alpha/app", "text": "Should I deploy the billing change?",
                                 "urls": ["https://example.com/report"]})[0]

    def everything_on_disk(self) -> str:
        found = []
        for folder, _, names in os.walk(self.tmp):
            for name in names:
                path = os.path.join(folder, name)
                if path == self.topic_file or os.path.islink(path):
                    continue
                with open(path, "rb") as f:
                    found.append(f.read().decode("utf-8", "replace"))
        return "\n".join(found)


class OnAndOff(NotifyCase):
    def test_off_without_a_topic_file_and_silent(self):
        self.pr(1)
        n = self.notifier(ntfy_topic_file=os.path.join(self.tmp, "missing"))
        self.assertFalse(n.wanted())
        self.assertEqual(n.run_once().status, "off")
        self.assertEqual(self.ntfy.requests, [])

    def test_the_default_topic_file_is_under_the_home_config_directory(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            n = self.notifier(ntfy_topic_file="")
            self.assertEqual(n.topic_file(), os.path.join(self.home, ".config", "owner-queue", "ntfy-topic"))
            self.assertFalse(n.wanted())

    def test_switched_off_by_the_setting_even_with_a_topic_file(self):
        self.pr(1)
        n = self.notifier(ntfy=False)
        self.assertFalse(n.wanted())
        self.assertEqual(n.run_once().status, "off")
        self.assertEqual(self.ntfy.requests, [])

    def test_a_topic_file_without_a_valid_topic_is_off(self):
        self.pr(1)
        for content in ("", "\n", "two words", "a/b", "../x", "http://elsewhere.example/t", "x" * 100, "t?x=1"):
            with open(self.topic_file, "w") as f:
                f.write(content)
            self.assertEqual(self.notifier().run_once().status, "off", content)
        self.assertEqual(self.ntfy.requests, [])

    def test_an_address_that_is_not_http_is_off(self):
        self.pr(1)
        for url in ("file:///etc/passwd", "ftp://example.com", "ntfy.sh", ""):
            self.assertEqual(self.notifier(ntfy_url=url).run_once().status, "off", url)

    def test_status_says_on_or_off_host_and_file_but_never_the_topic(self):
        on = self.out("notify", "--status", env=self.cli_env())
        self.assertIn("notifications: on", on)
        self.assertIn("server: 127.0.0.1", on)
        self.assertIn("topic file: found", on)
        self.assertIn("level: minimal", on)
        self.assertNotIn(TOPIC, on)
        off = self.out("notify", "--status", env=self.cli_env(OWNER_QUEUE_NTFY_TOPIC_FILE=os.path.join(self.tmp, "none")))
        self.assertIn("notifications: off (no topic file)", off)
        self.assertIn("topic file: not found", off)
        default = self.out("notify", "--status", env={"OWNER_QUEUE_NTFY": "on"})
        self.assertIn("server: ntfy.sh", default)
        self.assertIn("~/.config/owner-queue/ntfy-topic", default)
        self.assertIn("off (switched off", self.out("notify", "--status"))

    def cli_env(self, **changes) -> dict:
        env = {"OWNER_QUEUE_NTFY": "on", "OWNER_QUEUE_NTFY_URL": self.ntfy.url,
               "OWNER_QUEUE_NTFY_TOPIC_FILE": self.topic_file, "OWNER_QUEUE_NTFY_INTERVAL": "0",
               "OWNER_QUEUE_NTFY_TIMEOUT": "2"}
        env.update(changes)
        return env

    def test_the_test_command_sends_one_harmless_message(self):
        self.assertEqual(self.out("notify", "--test", env=self.cli_env()), "test notification sent\n")
        (request,) = self.ntfy.requests
        self.assertEqual(request["path"], "/" + TOPIC)
        self.assertIn("Test notification", request["body"])
        off = self.cli("notify", "--test")
        self.assertEqual(off.returncode, 0)
        self.assertIn("notifications are off", off.stdout.decode())

    def test_the_notify_command_sends_what_is_due(self):
        self.out("notify", env=self.cli_env())
        self.assertEqual(self.ntfy.requests, [])
        self.hook(bash_done("gh pr create --fill", PR + "3\n"))
        self.assertIn("sent: owner-queue: 1 new", self.out("notify", env=self.cli_env()))
        self.assertEqual(len(self.ntfy.requests), 1)


class Payload(NotifyCase):
    PRIVATE = ("example/repo", "Add the thing", "github.com", "/work/alpha", "billing", "sess-", "permission to use",
               "https://", "report")

    def test_minimal_holds_kinds_and_counts_only(self):
        self.pr(1)
        self.pr(2)
        self.review()
        self.assertEqual(self.notifier().run_once().status, "sent")
        (request,) = self.ntfy.requests
        self.assertEqual(request["path"], "/" + TOPIC)
        self.assertEqual(request["headers"]["Title"], "owner-queue: 3 new")
        self.assertEqual(request["body"], "2 pull requests, 1 session to review. 3 open in total.")
        sent = json.dumps(request)
        for private in self.PRIVATE:
            self.assertNotIn(private, sent.replace(self.ntfy.url, ""), private)
        self.assertNotIn("Click", request["headers"])

    def test_detail_adds_repository_number_title_and_the_link(self):
        self.pr(1, title="Add the thing")
        self.review()
        self.assertEqual(self.notifier(ntfy_level="detail").run_once().status, "sent")
        (request,) = self.ntfy.requests
        self.assertIn("example/repo#1 Add the thing", request["body"])
        self.assertEqual(request["headers"]["Click"], PR + "1")
        for private in ("/work/alpha", "billing", "sess-", "example.com/report"):
            self.assertNotIn(private, json.dumps(request), "detail is about pull requests only")

    def test_detail_lists_at_most_five_pull_requests(self):
        for n in range(1, 9):
            self.pr(n)
        self.notifier(ntfy_level="detail").run_once()
        body = self.ntfy.requests[0]["body"]
        self.assertEqual(body.count("example/repo#"), 5)
        self.assertIn("and 3 more", body)

    def test_a_title_cannot_add_headers_or_lines_to_the_request(self):
        self.pr(1, title="x\r\nX-Injected: 1")
        self.notifier(ntfy_level="detail").run_once()
        self.assertNotIn("X-Injected", self.ntfy.requests[0]["headers"])

    def test_an_unknown_level_means_minimal(self):
        self.pr(1)
        self.notifier(ntfy_level="everything").run_once()
        self.assertNotIn("example/repo", self.ntfy.requests[0]["body"])


class Pacing(NotifyCase):
    def test_new_items_go_out_as_one_digest(self):
        self.pr(1)
        self.pr(2)
        self.waiting()
        self.clock.now += 200   # the waiting item has been open long enough
        self.assertEqual(self.notifier().run_once().status, "sent")
        (request,) = self.ntfy.requests
        self.assertEqual(request["headers"]["Title"], "owner-queue: 3 new")
        self.assertEqual(request["body"], "2 pull requests, 1 session waiting. 3 open in total.")

    def test_an_item_is_never_pushed_twice(self):
        self.pr(1)
        n = self.notifier(ntfy_interval=0.0)
        self.assertEqual(n.run_once().status, "sent")
        for _ in range(3):
            self.clock.now += 1000
            self.assertEqual(self.notifier(ntfy_interval=0.0).run_once().status, "idle")
        self.assertEqual(len(self.ntfy.requests), 1)

    def test_at_most_one_push_per_interval(self):
        self.pr(1)
        self.assertEqual(self.notifier().run_once().status, "sent")
        self.pr(2)
        self.clock.now += 100
        limited = self.notifier().run_once()
        self.assertEqual(limited.status, "limited")
        self.assertIn("next push in", limited.message)
        self.pr(3)
        self.clock.now += 100
        self.assertEqual(self.notifier().run_once().status, "limited")
        self.assertEqual(len(self.ntfy.requests), 1)
        self.clock.now += 101   # 301 s after the first push
        self.assertEqual(self.notifier().run_once().status, "sent")
        self.assertEqual(self.ntfy.requests[1]["headers"]["Title"], "owner-queue: 2 new")
        self.assertEqual(self.ntfy.requests[1]["body"], "2 pull requests. 3 open in total.")

    def test_a_waiting_item_is_pushed_only_if_still_open_after_the_delay(self):
        item = self.waiting()
        soon = self.notifier().run_once()
        self.assertEqual(soon.status, "idle")
        self.assertEqual(soon.next_at, self.clock.now + 120)
        self.clock.now += 119
        self.assertEqual(self.notifier().run_once().status, "idle")
        self.clock.now += 2
        self.assertEqual(self.notifier().run_once().status, "sent")
        self.assertEqual(self.ntfy.requests[0]["body"], "1 session waiting. 1 open in total.")
        self.assertTrue(item)

    def test_a_waiting_item_answered_within_the_delay_is_never_pushed(self):
        item = self.waiting()
        self.clock.now += 60
        self.queue().mark("done", item, by="moved")
        self.clock.now += 600
        self.assertEqual(self.notifier().run_once().status, "idle")
        self.assertEqual(self.ntfy.requests, [])

    def test_items_the_owner_added_are_never_pushed(self):
        self.queue().add({"kind": "manual", "text": "call the bank"})
        self.assertEqual(self.notifier().run_once().status, "idle")
        self.assertEqual(self.ntfy.requests, [])

    def test_an_item_closed_before_its_turn_is_not_pushed(self):
        self.pr(1)
        self.notifier().run_once()
        second = self.pr(2)
        self.queue().mark("done", second)
        self.clock.now += 1000
        self.assertEqual(self.notifier().run_once().status, "idle")
        self.assertEqual(len(self.ntfy.requests), 1)

    def test_the_waiting_process_sleeps_until_the_delay_then_sends_and_ends(self):
        self.waiting()
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            self.clock.now += seconds

        result = self.notifier().serve(sleep=sleep)
        self.assertEqual(result.status, "idle")
        self.assertEqual(len(self.ntfy.requests), 1)
        self.assertGreaterEqual(sum(naps), 120)
        self.assertLessEqual(max(naps), 30)

    def test_only_one_waiting_process_at_a_time(self):
        self.pr(1)
        fd = self.queue().lock(1.0, "notify-daemon.lock")
        try:
            self.assertEqual(self.notifier().serve(sleep=lambda s: None).status, "limited")
        finally:
            os.close(fd)
        self.assertEqual(self.ntfy.requests, [])

    def test_the_memory_of_pushed_items_does_not_grow_without_bound(self):
        for n in range(1, 31):
            item = self.pr(n)
            self.notifier(ntfy_interval=0.0).run_once()
            self.queue().mark("done", item)
            self.clock.now += 40 * 86400   # closed long ago: compaction forgets it
            self.queue().compact()
        with open(os.path.join(self.dir, "notify.json")) as f:
            memo = json.load(f)
        self.assertLessEqual(len(memo["sent"]), 2)


class Failures(NotifyCase):
    def test_a_server_that_is_down_fails_once_and_does_not_storm(self):
        self.pr(1)
        dead = f"http://127.0.0.1:{free_port()}"
        first = self.notifier(ntfy_url=dead).run_once()
        self.assertEqual(first.status, "failed")
        self.assertNotIn(TOPIC, first.message)
        for _ in range(5):
            self.clock.now += 10
            self.assertEqual(self.notifier(ntfy_url=dead).run_once().status, "limited")
        # The server is back and the interval has passed: the item goes out, once.
        self.clock.now += 300
        self.assertEqual(self.notifier().run_once().status, "sent")
        self.assertEqual(len(self.ntfy.requests), 1)

    def test_the_waiting_process_stops_after_a_failed_push(self):
        self.pr(1)
        naps = []
        result = self.notifier(ntfy_url=f"http://127.0.0.1:{free_port()}").serve(sleep=naps.append)
        self.assertEqual(result.status, "failed")
        self.assertEqual(naps, [])

    def test_a_server_error_is_a_failure_and_counts_against_the_interval(self):
        self.ntfy.mode = "error"
        self.pr(1)
        failed = self.notifier().run_once()
        self.assertEqual((failed.status, failed.message), ("failed", "push failed: the server answered 500"))
        self.assertEqual(self.notifier().run_once().status, "limited")
        self.assertEqual(len(self.ntfy.requests), 1)
        self.ntfy.mode = "ok"
        self.clock.now += 301
        self.assertEqual(self.notifier().run_once().status, "sent")

    def test_a_slow_server_is_given_up_on(self):
        self.ntfy.mode = "slow"
        self.pr(1)
        started = time.monotonic()
        result = self.notifier(ntfy_timeout=0.5).run_once()
        self.assertEqual(result.status, "failed")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertNotIn(TOPIC, result.message)

    def test_a_redirect_is_not_followed(self):
        elsewhere = FakeNtfy()
        self.addCleanup(elsewhere.close)
        redirecting = FakeNtfy(mode="redirect", redirect_to=elsewhere.url)
        self.addCleanup(redirecting.close)
        self.pr(1)
        result = self.notifier(ntfy_url=redirecting.url).run_once()
        self.assertEqual(result.status, "failed")
        self.assertEqual(elsewhere.requests, [], "the topic must not travel to another address")

    def test_an_unreadable_queue_does_not_crash_the_notifier(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        done = self.cli("notify", env={"OWNER_QUEUE_DIR": os.path.join(blocker, "q"), "OWNER_QUEUE_NTFY": "on",
                                       "OWNER_QUEUE_NTFY_TOPIC_FILE": self.topic_file,
                                       "OWNER_QUEUE_NTFY_URL": self.ntfy.url})
        self.assertEqual(done.returncode, 1)
        self.assertNotIn("Traceback", done.stderr.decode())
        self.assertNotIn(TOPIC, done.stderr.decode() + done.stdout.decode())


class TheTopicStaysSecret(NotifyCase):
    def test_not_in_queue_files_output_or_errors(self):
        env = {"OWNER_QUEUE_NTFY": "on", "OWNER_QUEUE_NTFY_TOPIC_FILE": self.topic_file,
               "OWNER_QUEUE_NTFY_INTERVAL": "0", "OWNER_QUEUE_NTFY_TIMEOUT": "1"}
        printed = []
        self.hook(bash_done("gh pr create --fill", PR + "1\n"))
        for url in (self.ntfy.url, f"http://127.0.0.1:{free_port()}", "http://[::1", "http://256.256.256.256:1"):
            for args in (("notify",), ("notify", "--test"), ("notify", "--status")):
                done = self.cli(*args, env=dict(env, OWNER_QUEUE_NTFY_URL=url))
                printed.append(done.stdout.decode() + done.stderr.decode())
            self.hook(bash_done("gh pr create --fill", f"{PR}{len(printed)}\n"))
        self.ntfy.mode = "error"
        for args in (("notify",), ("notify", "--test"), ("notify", "--status"), ("list", "--json"), ("next",)):
            done = self.cli(*args, env=dict(env, OWNER_QUEUE_NTFY_URL=self.ntfy.url))
            printed.append(done.stdout.decode() + done.stderr.decode())
        self.assertNotIn(TOPIC, "\n".join(printed))
        self.assertNotIn(TOPIC, self.everything_on_disk())
        self.assertTrue(any(r["path"] == "/" + TOPIC for r in self.ntfy.requests), "it was used, in the request only")

    def test_only_the_first_line_of_the_topic_file_is_used(self):
        with open(self.topic_file, "w") as f:
            f.write(TOPIC + "\nsecond line that is not a topic\n")
        self.pr(1)
        self.notifier().run_once()
        self.assertEqual(self.ntfy.requests[0]["path"], "/" + TOPIC)


class FromAHook(NotifyCase):
    def env_on(self, **changes) -> dict:
        env = {"OWNER_QUEUE_NTFY": "on", "OWNER_QUEUE_NTFY_URL": self.ntfy.url,
               "OWNER_QUEUE_NTFY_TOPIC_FILE": self.topic_file, "OWNER_QUEUE_NTFY_INTERVAL": "0",
               "OWNER_QUEUE_NTFY_WAITING_DELAY": "1", "OWNER_QUEUE_NTFY_TIMEOUT": "2"}
        env.update(changes)
        return env

    def wait_for(self, count: int, seconds: float = 10.0) -> None:
        end = time.monotonic() + seconds
        while len(self.ntfy.requests) < count and time.monotonic() < end:
            time.sleep(0.05)

    def wait_until_notifier_exits(self, seconds: float = 10.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                os.close(self.queue().lock(0.05, "notify-daemon.lock"))
                return True
            except Exception:
                time.sleep(0.05)
        return False

    def test_the_hook_returns_at_once_and_the_push_follows(self):
        self.ntfy.mode = "slow"   # the server takes 3 s; the hook must not wait for it
        started = time.monotonic()
        self.hook(bash_done("gh pr create --fill", PR + "1\n"), env=self.env_on())
        self.assertLess(time.monotonic() - started, 1.5)
        self.wait_for(1)
        self.assertEqual(len(self.ntfy.requests), 1)
        self.assertEqual(self.ntfy.requests[0]["body"], "1 pull request. 1 open in total.")
        self.assertTrue(self.wait_until_notifier_exits())

    def test_a_waiting_item_is_pushed_after_the_delay_by_the_detached_process(self):
        self.hook(notification(), env=self.env_on())
        time.sleep(0.3)
        self.assertEqual(self.ntfy.requests, [])
        self.wait_for(1)
        self.assertEqual([r["body"] for r in self.ntfy.requests], ["1 session waiting. 1 open in total."])
        self.assertTrue(self.wait_until_notifier_exits())

    def test_a_prompt_answered_at_the_desk_never_reaches_the_phone(self):
        self.hook(notification(), env=self.env_on())
        self.hook(bash_done("ls"), env=self.env_on())
        time.sleep(1.5)   # longer than the waiting delay
        self.assertTrue(self.wait_until_notifier_exits())
        self.assertEqual(self.ntfy.requests, [])

    def test_no_process_is_started_when_nothing_new_was_queued(self):
        self.hook(bash_done("ls"), env=self.env_on())
        self.hook(stop("Done."), env=self.env_on())
        time.sleep(0.3)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "notify-daemon.lock")))

    def test_no_process_and_no_request_when_notifications_are_off(self):
        self.hook(bash_done("gh pr create --fill", PR + "1\n"), env=self.env_on(OWNER_QUEUE_NTFY="off"))
        self.hook(bash_done("gh pr create --fill", PR + "2\n"),
                  env=self.env_on(OWNER_QUEUE_NTFY_TOPIC_FILE=os.path.join(self.tmp, "none")))
        time.sleep(0.5)
        self.assertEqual(self.ntfy.requests, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "notify-daemon.lock")))

    def test_many_hooks_at_once_are_batched_not_one_push_each(self):
        env = self.env_on(OWNER_QUEUE_NTFY_INTERVAL="2")
        for n in range(1, 9):
            self.hook(bash_done("gh pr create --fill", f"{PR}{n}\n", session_id=f"session-{n}"), env=env)
        self.assertTrue(self.wait_until_notifier_exits(15))
        self.assertLessEqual(len(self.ntfy.requests), 2)
        announced = sum(int(r["headers"]["Title"].split()[1]) for r in self.ntfy.requests)
        self.assertEqual(announced, 8, "every pull request announced exactly once")


if __name__ == "__main__":
    unittest.main()
