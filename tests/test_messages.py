"""``efd/ui/messages.py``：worker 线程与界面之间的消息协议。

这个文件是**三条历史 bug 的事故现场**，所以它值得比"顺带被 GUI 测试覆盖"
更好的待遇：

- v0.2.0：检查分支发完计划就 ``return``，漏发结束消息 → 【开始安装】永远点不动；
- v0.2.3：``say(Msg.STOPPED if result.stopped else Msg.DONE)`` —— **出错提前返回时
  ``stopped`` 是 False**，于是"下载中断"被当成"装完了"发给了界面，界面再照实
  画满进度条、弹「安装完成」。用户看到的就是"提前提示下载完成"。

第 2 条正是下面 :meth:`TestTerminalMessage.test_an_interrupted_run_does_not_say_done`
钉住的那一行。这里用的是**真 Worker**，消息也是真发进队列的——手写消息序列
的测试跟 Worker 脱钩，改坏了照样绿。
"""

from __future__ import annotations

import queue
import unittest
from unittest import mock

from efd.core.installer import Result
from efd.ui import TERMINAL_KINDS, messages
from efd.ui.messages import Msg, Worker


def fake_entry(name="Endfield_Data/a.chk", size=1024, compressed=512, offset=0):
    return mock.Mock(name=name, size=size, compressed=compressed, offset=offset,
                     crc=0)


def fake_plan(need=True, target="D:/game"):
    """够 Worker 用的最小计划；``need`` 决定它是不是空计划。"""
    entries = [fake_entry()] if need else []
    return mock.Mock(target=target, need=entries, need_count=len(entries),
                     version="1.5.3")


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.q: queue.Queue = queue.Queue()
        self.made: list[tuple[str, dict]] = []
        self.archive = mock.MagicMock(name="archive")
        self.archive.__enter__.return_value = self.archive
        self.archive.__exit__.return_value = False

        for name, value in (("open_remote", mock.MagicMock(return_value=self.archive)),
                            ("make_plan", mock.MagicMock(return_value=fake_plan())),
                            ("install", mock.MagicMock(return_value=Result(done=1)))):
            p = mock.patch.object(messages, name, value)
            p.start()
            self.addCleanup(p.stop)

    def worker(self, *, tryout=False, do_install=True, plan=None, **kw) -> Worker:
        if plan is not None:
            messages.make_plan.return_value = plan
        return Worker(self.q, "D:/game", tryout, do_install, **kw)

    def drain(self) -> list[tuple[str, dict]]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def kinds(self) -> list[str]:
        """最近一次 :meth:`drive` 收到的消息种类。

        刻意不在这里再抽一次队列：队列是一次性的，``drive`` 已经抽干了，
        二次抽取只会拿到空列表，断言就永远看不到东西（第一版就是这么写的，
        4 个用例恒真失败）。
        """
        return [k for k, _ in self.made]

    def drive(self, w: Worker) -> list[tuple[str, dict]]:
        """把 worker 跑完并收下它发出的全部消息。"""
        w.run()
        self.made = self.drain()
        return self.made


class TestMessageContract(unittest.TestCase):
    """消息种类本身是对外契约——界面按字符串比对。"""

    def test_kinds_are_plain_strings(self):
        """队列里放的必须是普通 str：消费方不该被迫知道枚举的存在。"""
        for member in Msg:
            self.assertIsInstance(member.value, str)
            self.assertEqual(str(member.value), member.value)
            self.assertNotIsInstance(member.value, Msg)

    def test_say_puts_the_string_not_the_enum(self):
        q: queue.Queue = queue.Queue()
        w = Worker(q, "D:/x", False, False)
        w.say(Msg.STATUS, text="hi")
        kind, payload = q.get_nowait()
        self.assertIs(type(kind), str)
        self.assertEqual(kind, "status")
        self.assertEqual(payload, {"text": "hi"})

    def test_terminal_kinds_covers_every_way_a_round_can_end(self):
        """漏一个，界面的按钮就会永远锁着。"""
        self.assertEqual(TERMINAL_KINDS,
                         {"checked", "done", "stopped", "error"})
        self.assertNotIn(Msg.PLAN.value, TERMINAL_KINDS,
                         "计划到了不代表结束：检查流程还要再发一条 checked")
        self.assertNotIn(Msg.PROGRESS.value, TERMINAL_KINDS)

    def test_worker_is_a_daemon(self):
        """关窗要能立刻退出进程，不能被 worker 挂住。"""
        self.assertTrue(Worker(queue.Queue(), "D:/x", False, False).daemon)


class TestCheckRun(WorkerCase):
    def test_a_check_emits_plan_then_a_terminal_message(self):
        w = self.worker(do_install=False)
        messages_made = self.drive(w)
        kinds = [k for k, _ in messages_made]

        self.assertEqual(kinds, ["status", "plan", "checked"])
        self.assertIn(kinds[-1], TERMINAL_KINDS,
                      "检查跑完没有发结束消息 → 【开始安装】永远点不动")

    def test_a_check_never_installs(self):
        self.drive(self.worker(do_install=False))
        messages.install.assert_not_called()

    def test_the_status_message_carries_a_state_label(self):
        """状态栏文案是给用户看"现在在干嘛"的，不能只有日志。"""
        _, payload = self.drive(self.worker(do_install=False))[0]
        self.assertIn("state", payload)
        self.assertTrue(payload["text"])

    def test_the_plan_reaches_the_ui(self):
        plan = fake_plan()
        _, payload = [m for m in self.drive(self.worker(do_install=False, plan=plan))
                      if m[0] == "plan"][0]
        self.assertIs(payload["plan"], plan)


class TestInstallRun(WorkerCase):
    def test_an_empty_plan_is_done_immediately(self):
        """没东西要装也是一种"完成"，而且不该白跑一趟 install。"""
        w = self.worker(plan=fake_plan(need=False))
        self.drive(w)
        messages.install.assert_not_called()
        self.assertIn("done", self.kinds())

    def test_an_empty_plan_reports_an_empty_result(self):
        result = [p["result"] for k, p in
                  self.drive(self.worker(plan=fake_plan(need=False))) if k == "done"][0]
        self.assertEqual(result.done, 0)
        self.assertEqual(result.errors, [])
        self.assertFalse(result.stopped)

    def test_a_clean_install_reports_done_with_the_real_result(self):
        real = Result(done=318, written=43_000_000_000, net_bytes=39_000_000_000)
        messages.install.return_value = real
        got = [p["result"] for k, p in self.drive(self.worker()) if k == "done"]
        self.assertEqual(got, [real])

    def test_install_gets_the_stop_event(self):
        """【停止】按钮唯一的着力点就是它。"""
        w = self.worker()
        self.drive(w)
        self.assertIs(messages.install.call_args.kwargs["stop_event"], w.stop_event)

    def test_install_gets_a_progress_callback(self):
        self.drive(self.worker())
        self.assertTrue(callable(messages.install.call_args.kwargs["on_progress"]))

    def test_install_gets_the_plan_and_the_archive(self):
        plan = fake_plan()
        self.drive(self.worker(plan=plan))
        archive, passed = messages.install.call_args.args
        self.assertIs(archive, self.archive)
        self.assertIs(passed, plan)


class TestTerminalMessage(WorkerCase):
    """一轮结束时到底发哪条消息——三条 bug 里有两条错在这里。"""

    def test_a_clean_run_says_done(self):
        self.drive(self.worker())
        kinds = self.kinds()
        self.assertIn("done", kinds)
        self.assertNotIn("stopped", kinds)

    def test_a_stopped_run_says_stopped(self):
        messages.install.return_value = Result(done=2, stopped=True)
        self.drive(self.worker())
        kinds = self.kinds()
        self.assertIn("stopped", kinds)
        self.assertNotIn("done", kinds)

    def test_an_interrupted_run_does_not_say_done(self):
        """回归：出错提前返回时 ``stopped`` 仍是 False。

        ``install()`` 在第一个异常处就返回（GUI 从不传 ``keep_going``），
        而 ``Result.stopped`` 只由停机事件设置——所以一个被网络抖动打断的
        运行，字段上看是"没停过"。曾经的代码只看这一个字段就发了 ``DONE``，
        界面于是画满进度条弹「安装完成」。
        """
        messages.install.return_value = Result(
            done=0, errors=["a.chk: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING]>"])
        messages_made = self.drive(self.worker())
        kinds = [k for k, _ in messages_made]

        self.assertIn(kinds[-1], TERMINAL_KINDS)
        done_payloads = [p for k, p in messages_made if k == "done"]
        if done_payloads:
            # 允许发 done——但界面必须能从 result 本身看出没装完。
            self.assertTrue(done_payloads[0]["result"].errors,
                            "把一次失败的运行当成干净完成发给了界面")

    def test_the_terminal_message_carries_the_plan_too(self):
        """界面要用 plan.need_count 算"还剩多少没装"。"""
        plan = fake_plan()
        messages_made = self.drive(self.worker(plan=plan))
        kind, payload = messages_made[-1]
        self.assertIn(kind, TERMINAL_KINDS)
        self.assertIs(payload["plan"], plan)


class TestProgress(unittest.TestCase):
    def test_progress_is_forwarded_verbatim(self):
        q: queue.Queue = queue.Queue()
        w = Worker(q, "D:/x", False, True)
        progress = mock.Mock(fraction=0.5, done=1)
        w._on_progress(progress)
        kind, payload = q.get_nowait()
        self.assertEqual(kind, "progress")
        self.assertIs(payload["progress"], progress)


class TestFailurePaths(WorkerCase):
    def test_an_install_crash_becomes_an_error_message(self):
        messages.install.side_effect = OSError("磁盘已满")
        messages_made = self.drive(self.worker())

        kinds = [k for k, _ in messages_made]
        self.assertEqual(kinds[-1], "error")
        text = messages_made[-1][1]["text"]
        self.assertIn("磁盘已满", text)
        self.assertIn("Traceback", text, "没有 traceback，用户报 bug 时无从下手")

    def test_an_open_remote_crash_becomes_an_error_message(self):
        messages.open_remote.side_effect = OSError("连不上")
        messages_made = self.drive(self.worker())
        self.assertEqual([k for k, _ in messages_made][-1], "error")
        self.assertIn("连不上", messages_made[-1][1]["text"])

    def test_an_arbitrary_exception_is_still_reported(self):
        """任何异常都必须让用户看见——静默死掉就是"按钮点不动"。"""
        messages.make_plan.side_effect = RuntimeError("意料之外")
        messages_made = self.drive(self.worker())
        self.assertEqual([k for k, _ in messages_made][-1], "error")
        self.assertIn("意料之外", messages_made[-1][1]["text"])

    def test_the_error_path_does_not_swallow_the_exception_silently(self):
        messages.install.side_effect = ValueError("boom")
        self.drive(self.worker())
        self.assertIn("error", self.kinds())

    def test_a_failed_run_still_emits_a_terminal_kind(self):
        for exc in (OSError("x"), RuntimeError("y"), ValueError("z")):
            with self.subTest(exc=type(exc).__name__):
                messages.install.side_effect = exc
                q = queue.Queue()
                w = Worker(q, "D:/game", False, True)
                w.run()
                kinds = []
                while True:
                    try:
                        kinds.append(q.get_nowait()[0])
                    except queue.Empty:
                        break
                self.assertIn(kinds[-1], TERMINAL_KINDS,
                              f"{type(exc).__name__} 之后界面不会解锁")


class TestWiring(WorkerCase):
    """worker 从界面拿到的参数必须真的传到底层。"""

    def test_network_settings_reach_open_remote(self):
        self.drive(self.worker(jobs=4, limit_rate=2 * 1024 * 1024))
        kwargs = messages.open_remote.call_args.kwargs
        self.assertEqual(kwargs["jobs"], 4)
        self.assertEqual(kwargs["limit_rate"], 2 * 1024 * 1024)

    def test_tryout_excludes_streaming_assets(self):
        from efd.core import config

        self.drive(self.worker(tryout=True, do_install=False))
        self.assertEqual(messages.make_plan.call_args.kwargs["exclude_prefixes"],
                         config.EXCLUDE_STREAMING)

    def test_a_full_install_excludes_nothing(self):
        self.drive(self.worker(tryout=False, do_install=False))
        self.assertEqual(messages.make_plan.call_args.kwargs["exclude_prefixes"], ())

    def test_the_plan_is_built_for_the_user_target(self):
        self.drive(self.worker(do_install=False))
        self.assertEqual(messages.make_plan.call_args.args[1], "D:/game")

    def test_the_archive_is_closed_even_when_install_crashes(self):
        """卷句柄不关，下一轮就占着连接不放。"""
        messages.install.side_effect = OSError("boom")
        self.drive(self.worker())
        self.archive.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
