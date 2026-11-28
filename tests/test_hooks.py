import json
import os
import statistics
import time
import unittest

from helpers import PR, ROOT, Case, bash_done, common, notification, stop

from owner_queue import config
from owner_queue.__main__ import dispatch
from owner_queue.store import Queue

SESSION_A = "0f3c1a2b-1111-4222-8333-444455556666"
SESSION_B = "9e8d7c6b-aaaa-4bbb-8ccc-ddddeeeeffff"


class PullRequests(Case):
    def test_gh_pr_create_queues_url_repo_number_and_title(self):
        self.hook(bash_done('gh pr create --title "Add retry to the uploader" --body "details"', PR + "41\n"))
        (item,) = self.items()
        self.assertEqual((item.kind, item.url, item.repo, item.number), ("pr", PR + "41", "example/repo", 41))
        self.assertEqual(item.title, "Add retry to the uploader")
        self.assertEqual((item.session, item.cwd), (SESSION_A, "/work/alpha/app"))

    def test_the_command_line_and_output_are_not_stored(self):
        secret = "ghp_" + "Z9y8" * 9
        command = f'GH_TOKEN={secret} gh pr create --title "T" --body "internal notes about the customer"'
        self.hook(bash_done(command, f"remote: counting objects\nWarning: 3 uncommitted changes\n{PR}8\n"))
        log = self.log()
        self.assertEqual(len(self.items()), 1)
        for leaked in (secret, "GH_TOKEN", "internal notes", "uncommitted", "--body"):
            self.assertNotIn(leaked, log)

    def test_a_token_in_the_title_is_redacted(self):
        secret = "ghp_" + "Q1w2" * 9
        self.hook(bash_done(f'gh pr create --title "rotate {secret}"', PR + "8\n"))
        self.assertNotIn(secret, self.log())
        self.assertIn("[redacted]", self.items()[0].title)

    def test_other_forms_of_the_command(self):
        forms = [
            ("gh pr create --fill", 1, ""),
            ("cd /work/x && gh pr create -t 'Single quoted' -b x", 2, "Single quoted"),
            ("gh -R example/repo pr create --title=Equals", 3, "Equals"),
            ("git push -u origin feat && gh pr new --title \"After a push\"", 4, "After a push"),
            ('bash -c "gh pr create --title \\"Add retry\\" --body x"', 5, "Add retry"),
            ('ssh box "cd r && gh pr create -t \\"Inside ssh\\""', 6, "Inside ssh"),
        ]
        for command, number, title in forms:
            self.hook(bash_done(command, f"{PR}{number}\n"))
        self.assertEqual([(i.number, i.title) for i in self.items()], [(n, t) for _, n, t in forms])

    def test_a_script_that_ran_gh_is_recognised_by_its_output(self):
        output = f"Creating pull request for feat/x into main in example/repo\n\n{PR}12\n"
        self.hook(bash_done("./scripts/open-pr.sh", output))
        self.assertEqual([i.number for i in self.items()], [12])

    def test_a_pull_request_that_is_only_mentioned_is_not_queued(self):
        self.hook(bash_done("gh pr view 7 --json url", json.dumps({"url": PR + "7"})))
        self.hook(bash_done("gh pr list", f"7  Fix  feat/x  OPEN  {PR}7\n"))
        self.hook(bash_done("git log -3", f"see {PR}7\n"))
        self.hook(bash_done("echo gh pr create", "gh pr create\n"))
        self.hook(common("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}, tool_response=PR + "7"))
        self.assertEqual(self.items(), [])

    def test_a_failed_create_queues_nothing(self):
        self.hook(bash_done("gh pr create --fill", "pull request create failed: GraphQL: No commits between main and x\n"))
        self.assertEqual(self.items(), [])

    def test_the_same_pull_request_from_two_sessions_is_one_item(self):
        self.hook(bash_done("gh pr create --fill", PR + "7\n"))
        self.hook(bash_done("gh pr create --fill", f"a pull request for branch x already exists:\n{PR}7\n", session_id=SESSION_B))
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(self.log().count('"op":"add"'), 1)

    def test_a_subagent_reports_through_the_same_hook(self):
        self.hook(bash_done("gh pr create --fill", PR + "9\n", agent_id="agent-abc123", agent_type="general-purpose"))
        (item,) = self.items()
        self.assertEqual((item.number, item.agent), (9, "agent-abc123"))

    def test_tool_response_of_any_shape_is_read(self):
        shapes = ["plain " + PR + "1", {"output": [{"type": "text", "text": PR + "2"}]}, [PR + "3"], {"a": {"b": {"c": PR + "4"}}}]
        for shape in shapes:
            event = bash_done("gh pr create --fill")
            event["tool_response"] = shape
            self.hook(event)
        self.assertEqual([i.number for i in self.items()], [1, 2, 3, 4])


class Waiting(Case):
    def test_a_permission_prompt_becomes_one_waiting_item(self):
        self.hook(notification())
        self.hook(notification())   # the same prompt reported twice
        (item,) = self.items()
        self.assertEqual((item.kind, item.ntype, item.session, item.cwd), ("waiting", "permission_prompt", SESSION_A, "/work/alpha/app"))
        self.assertEqual(item.text, "Claude needs your permission to use Bash")

    def test_it_closes_when_the_session_makes_its_next_tool_call(self):
        self.hook(notification())
        self.hook(notification(session_id=SESSION_B, cwd="/work/beta/app"))
        self.hook(bash_done("ls"))
        self.assertEqual([i.session for i in self.items()], [SESSION_B])

    def test_it_closes_on_the_next_prompt_a_failed_call_a_stop_or_the_session_end(self):
        closers = [
            common("UserPromptSubmit", prompt="yes, go on"),
            common("PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"}, error="denied", is_interrupt=False),
            stop("Done."),
            common("SessionEnd", reason="prompt_input_exit"),
        ]
        for closer in closers:
            self.hook(notification(ntype="elicitation_dialog", message="An MCP server asks a question"))
            self.assertEqual(len(self.items()), 1)
            self.hook(closer)
            self.assertEqual(self.items(), [], closer["hook_event_name"])

    def test_other_notification_types_are_ignored(self):
        for ntype in ("auth_success", "elicitation_complete", "elicitation_response", "something_new", None, 7):
            self.hook(notification(ntype=ntype))
        self.assertEqual(self.items(), [])

    def test_two_directories_with_the_same_folder_name_do_not_collide(self):
        self.hook(notification(session_id=SESSION_A, cwd="/work/alpha/app"))
        self.hook(notification(session_id=SESSION_B, cwd="/work/beta/app"))
        self.assertEqual(sorted(i.cwd for i in self.items()), ["/work/alpha/app", "/work/beta/app"])
        self.hook(bash_done("ls", session_id=SESSION_B, cwd="/work/beta/app"))
        self.assertEqual([i.cwd for i in self.items()], ["/work/alpha/app"])

    def test_without_a_session_id_the_full_directory_identifies_the_session(self):
        for cwd in ("/work/alpha/app", "/work/beta/app"):
            event = notification(cwd=cwd)
            del event["session_id"]
            self.hook(event)
        self.assertEqual(len(self.items()), 2)

    def test_a_subagents_prompt_is_not_closed_by_another_agents_tool_call(self):
        asked = {"command": "terraform apply"}
        self.hook(common("PermissionRequest", tool_name="Bash", tool_input=asked, agent_id="agent-one", agent_type="general-purpose"))
        self.hook(notification())   # the prompt is shown in the main session
        (item,) = self.items()
        self.assertEqual((item.agent, item.tool), ("agent-one", "Bash"))
        self.assertNotIn("terraform", self.log(), "the awaited command is kept as a fingerprint only")
        # Another subagent and the main agent keep working: the prompt is still unanswered.
        self.hook(bash_done("ls", agent_id="agent-two", agent_type="Explore"))
        self.hook(common("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}, tool_response="x"))
        self.hook(common("SubagentStop", agent_id="agent-two", agent_type="Explore", last_assistant_message="done"))
        self.assertEqual(len(self.items()), 1)
        # The awaited call completes: answered.
        event = bash_done("terraform apply", agent_id="agent-one", agent_type="general-purpose")
        event["tool_input"] = asked
        self.hook(event)
        self.assertEqual(self.items(), [])

    def test_a_main_agent_prompt_closes_when_the_awaited_call_completes(self):
        asked = {"command": "rm -rf build", "description": "clean"}
        self.hook(common("PermissionRequest", tool_name="Bash", tool_input=asked))
        self.hook(notification())
        self.hook(bash_done("ls", agent_id="agent-two"))   # a subagent moving is not the main agent moving
        self.assertEqual(len(self.items()), 1)
        event = bash_done("x")
        event["tool_input"] = asked
        self.hook(event)
        self.assertEqual(self.items(), [])

    def test_a_subagent_that_stops_closes_its_own_prompt(self):
        self.hook(common("PermissionRequest", tool_name="Write", tool_input={"file_path": "/x"}, agent_id="agent-one"))
        self.hook(notification())
        self.hook(common("SubagentStop", agent_id="agent-one", agent_type="general-purpose", last_assistant_message="gave up"))
        self.assertEqual(self.items(), [])

    def test_an_idle_session_is_queued_unless_it_already_has_an_item(self):
        self.hook(notification(ntype="idle_prompt", message="Claude is waiting for your input"))
        self.assertEqual([i.ntype for i in self.items()], ["idle_prompt"])
        self.hook(common("UserPromptSubmit", prompt="continue"))
        self.hook(stop("Which option do you want?"))
        self.hook(notification(ntype="idle_prompt", message="Claude is waiting for your input"))
        self.assertEqual([i.kind for i in self.items()], ["review"])

    def test_idle_items_can_be_switched_off(self):
        self.hook(notification(ntype="idle_prompt"), env={"OWNER_QUEUE_IDLE": "0"})
        self.assertEqual(self.items(), [])
        self.hook(notification(), env={"OWNER_QUEUE_IDLE": "0"})
        self.assertEqual(len(self.items()), 1)

    def test_the_message_is_capped_and_redacted(self):
        secret = "ghp_" + "M3n4" * 9
        self.hook(notification(message=f"Claude wants to run: curl -H 'token: {secret}' " + "x" * 5000))
        (item,) = self.items()
        self.assertLessEqual(len(item.text.encode()), config.MAX_MESSAGE)
        self.assertNotIn(secret, self.log())


class Review(Case):
    def test_a_turn_that_ends_with_a_question_is_queued(self):
        self.hook(stop("I compared both layouts.\n\nShould I go with the two-column one?"))
        (item,) = self.items()
        self.assertEqual((item.kind, item.session, item.cwd), ("review", SESSION_A, "/work/alpha/app"))
        self.assertIn("two-column", item.text)

    def test_a_turn_that_leaves_links_is_queued_with_them(self):
        self.hook(stop("Report: https://example.com/report/17 and the dashboard (https://example.com/d?x=1). Finished."))
        (item,) = self.items()
        self.assertEqual(item.urls, ["https://example.com/report/17", "https://example.com/d?x=1"])

    def test_a_turn_that_asks_without_a_question_mark_is_queued(self):
        for message in ("The branch is ready for your review.", "Please confirm before I continue.", "Let me know which approach you prefer."):
            self.hook(stop(message, session_id=message[:12]))
        self.assertEqual(len(self.items()), 3)

    def test_an_ordinary_finished_turn_is_not_queued(self):
        for message in ("Done. All 40 tests pass.", "I renamed the function and updated its callers.", "", "   "):
            self.hook(stop(message))
        self.assertEqual(self.items(), [])

    def test_one_open_review_per_session_replaced_by_the_newer(self):
        self.hook(stop("Should I use option A?"))
        first = self.items()[0].id
        self.hook(stop("Should I use option B?", session_id=SESSION_B))
        self.hook(stop("Never mind A. Is option C acceptable?"))
        items = self.items()
        self.assertEqual([i.session for i in items], [SESSION_A, SESSION_B])
        self.assertEqual(items[0].id, first)
        self.assertIn("option C", items[0].text)

    def test_it_closes_when_the_owner_next_prompts_that_session(self):
        self.hook(stop("Should I use option A?"))
        self.hook(stop("Should I use option B?", session_id=SESSION_B))
        self.hook(common("UserPromptSubmit", prompt="A"))
        self.assertEqual([i.session for i in self.items()], [SESSION_B])

    def test_tool_calls_do_not_close_a_review(self):
        self.hook(stop("Should I use option A?"))
        self.hook(bash_done("ls"))
        self.assertEqual(len(self.items()), 1)

    def test_the_excerpt_is_the_end_of_the_message_capped_in_bytes_and_redacted(self):
        secret = "ghp_" + "K7j8" * 9
        message = "intro " * 2000 + f"The key is {secret}. " + chr(0x4E16) * 50 + " Do you want me to deploy?"
        self.hook(stop(message))
        (item,) = self.items()
        self.assertLessEqual(len(item.text.encode()), config.MAX_TEXT)
        self.assertTrue(item.text.endswith("Do you want me to deploy?"))
        self.assertNotIn(secret, self.log())

    def test_links_to_pull_requests_already_queued_are_not_a_second_item(self):
        self.hook(bash_done("gh pr create --fill", PR + "7\n"))
        self.hook(stop(f"Opened {PR}7 with the fix."))
        self.assertEqual([i.kind for i in self.items()], ["pr"])
        self.hook(stop(f"Opened {PR}7. Should I also update the changelog?"))
        self.assertEqual([i.kind for i in self.items()], ["pr", "review"])

    def test_a_subagent_stop_is_not_a_review(self):
        self.hook(stop("Should I continue?", agent_id="agent-one", agent_type="general-purpose"))
        self.hook(common("SubagentStop", agent_id="agent-one", agent_type="Explore", last_assistant_message="Should I continue?"))
        self.assertEqual(self.items(), [])

    def _transcript(self, lines: list, padding: int = 0) -> str:
        path = os.path.join(self.tmp, "transcript.jsonl")
        with open(path, "w") as f:
            for _ in range(padding):
                f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 1000}}) + "\n")
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path

    def test_without_the_message_the_tail_of_the_transcript_is_read(self):
        assistant = lambda text: {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": text}]}}
        path = self._transcript([
            assistant("An older answer?"),
            {"type": "user", "message": {"role": "user", "content": "go"}},
            assistant("Which database should I use?"),
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent?"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
            "{broken json",
        ], padding=300)
        self.assertGreater(os.path.getsize(path), 4 * config.TRANSCRIPT_TAIL)
        event = common("Stop", stop_hook_active=False, transcript_path=path)
        self.hook(event)
        (item,) = self.items()
        self.assertEqual(item.text, "Which database should I use?")

    def test_a_missing_or_odd_transcript_is_harmless(self):
        for path in ("/nonexistent/t.jsonl", self.tmp, "", None, 5):
            self.hook(common("Stop", stop_hook_active=False, transcript_path=path))
        self.assertEqual(self.items(), [])


class NeverBreakTheSession(Case):
    BAD = [
        b"", b"   ", b"not json", b"[]", b"null", b"42", b'"text"', b"{", b"\xff\xfe\x00\x01",
        b'{"hook_event_name": 5}', b'{"hook_event_name": "Stop"}',
        b'{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":"x","tool_response":null,"session_id":["a"],"cwd":{}}',
        b'{"hook_event_name":"Notification","notification_type":"permission_prompt","message":{"a":1},"session_id":7}',
        b'{"hook_event_name":"Stop","session_id":"s","last_assistant_message":["?"],"transcript_path":{}}',
        b'{"hook_event_name":"PermissionRequest","session_id":"s","tool_name":{},"tool_input":[]}',
        b'{"hook_event_name":"SomethingNew","session_id":"s"}',
    ]

    def test_malformed_input_exits_zero_and_prints_nothing(self):
        for raw in self.BAD:
            self.hook(raw)
        self.assertEqual(self.items(), [])

    def test_hostile_field_values_are_capped(self):
        self.hook(notification(session_id="s" * 100000, cwd="/" + "d" * 100000, message="m" * 100000))
        self.hook(bash_done("gh pr create --title " + "t" * 100000, PR + "1\n", agent_id="a" * 100000))
        for line in self.log().splitlines():
            self.assertLessEqual(len(line.encode()), config.MAX_LINE)
        for item in self.items():
            self.assertLessEqual(len(item.session.encode()), config.MAX_ID)
            self.assertLessEqual(len(item.cwd.encode()), config.MAX_PATH)

    def test_an_unusable_queue_directory_exits_zero(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        for target in (os.path.join(blocker, "queue"), blocker, "/dev/null/queue"):
            env = {"OWNER_QUEUE_DIR": target}
            self.hook(notification(), env=env)
            self.hook(bash_done("gh pr create --fill", PR + "1\n"), env=env)
            self.hook(stop("Should I?"), env=env)

    def test_a_read_only_queue_directory_exits_zero(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        self.hook(notification())
        self.hook(stop("Should I?"))

    def test_a_corrupt_queue_still_takes_new_items(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "events.jsonl"), "wb") as f:
            f.write(b"\x00\x01garbage\n{half")
        self.hook(notification())
        self.assertEqual(len(self.items()), 1)

    def test_a_symlinked_log_is_left_alone(self):
        os.makedirs(self.dir)
        target = os.path.join(self.tmp, "precious.txt")
        with open(target, "w") as f:
            f.write("precious\n")
        os.symlink(target, os.path.join(self.dir, "events.jsonl"))
        self.hook(notification())
        with open(target) as f:
            self.assertEqual(f.read(), "precious\n")

    def test_a_held_lock_makes_the_hook_give_up_not_hang(self):
        q = Queue(self.dir)
        q.add({"kind": "manual", "text": "x"})
        fd = q.lock(1.0, "lock")
        try:
            started = time.monotonic()
            self.hook(notification())
            self.assertLess(time.monotonic() - started, config.HOOK_LOCK_WAIT + 3)
        finally:
            os.close(fd)
        self.assertEqual(len(self.items()), 1)

    def test_disable_turns_every_hook_off(self):
        self.hook(notification(), env={"OWNER_QUEUE_DISABLE": "1"})
        self.assertFalse(os.path.exists(self.dir))
        with open(os.path.join(self.home, "settings.json"), "w") as f:
            json.dump({"disable": True}, f)
        self.hook(notification(), env={"OWNER_QUEUE_CONFIG": os.path.join(self.home, "settings.json")})
        self.assertFalse(os.path.exists(self.dir))

    def test_the_default_directory_is_under_the_home_given(self):
        env = {"OWNER_QUEUE_DIR": ""}
        self.hook(notification(), env=env)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude", "owner-queue", "events.jsonl")))


class HooksFile(unittest.TestCase):
    def setUp(self):
        with open(ROOT / "hooks" / "hooks.json") as f:
            self.hooks = json.load(f)["hooks"]

    def test_every_event_runs_the_one_hook_command(self):
        for event, groups in self.hooks.items():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertEqual(hook["type"], "command", event)
                    self.assertEqual(hook["command"], 'python3 "${CLAUDE_PLUGIN_ROOT}/bin/owner-queue" hook', event)
                    self.assertLessEqual(hook["timeout"], 10, event)

    def test_registered_events_are_the_ones_the_dispatcher_handles(self):
        handled = {"PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification", "SubagentStop",
                   "UserPromptSubmit", "Stop", "SessionEnd"}
        self.assertEqual(set(self.hooks), handled)

    def test_tool_hooks_do_not_make_a_tool_call_wait(self):
        for event in ("PostToolUse", "PostToolUseFailure"):
            self.assertTrue(all(h.get("async") for g in self.hooks[event] for h in g["hooks"]), event)

    def test_the_entry_point_is_executable(self):
        self.assertTrue(os.access(ROOT / "bin" / "owner-queue", os.X_OK))


class Timing(Case):
    def test_a_hook_process_is_fast(self):
        """Budget: well under 100 ms on a developer machine; the bound here leaves room for slow CI."""
        q = self.queue()
        for i in range(50):
            q.add({"kind": "review", "key": f"review:{i}", "session": str(i), "text": "x" * 200, "cwd": "/work/alpha/app"})
        events = [
            common("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}, tool_response="content " * 2000),
            bash_done("gh pr create --fill", PR + "77\n"),
            notification(),
            stop("Should I continue?"),
        ]
        times = []
        for i in range(20):
            event = json.dumps(events[i % len(events)]).encode()
            started = time.perf_counter()
            self.cli("hook", stdin=event)
            times.append((time.perf_counter() - started) * 1000)
        median = statistics.median(times)
        print(f"\n    hook process: median {median:.0f} ms, max {max(times):.0f} ms (20 runs, 50 open items)", end="")
        self.assertLess(median, 400)

    def test_the_work_inside_a_hook_is_a_few_milliseconds(self):
        q = Queue(self.dir)
        for i in range(200):
            q.add({"kind": "review", "key": f"review:{i}", "session": str(i), "text": "x" * 200})
        settings = dict(config.DEFAULTS)
        event = common("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}, tool_response="content " * 2000)
        started = time.perf_counter()
        for _ in range(50):
            dispatch(q, event, settings)
        each = (time.perf_counter() - started) * 1000 / 50
        print(f"\n    hook work in-process: {each:.2f} ms per tool call (200 open items)", end="")
        self.assertLess(each, 50)


if __name__ == "__main__":
    unittest.main()
