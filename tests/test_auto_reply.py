"""离线测试：在没有微信客户端的环境下验证核心逻辑。

真实收发消息必须在本机安装了微信 3.9.x 的情况下运行，但「控件解析、消息去重、
规则匹配、模板渲染、规则存取」这些纯逻辑可以用假控件覆盖，避免把明显错误
留到真机上才发现。

运行任一命令即可：
    python tests/test_auto_reply.py
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_reply  # noqa: E402
from auto_reply import (  # noqa: E402
    Rule,
    load_rules,
    parse_selection,
    render_reply,
    run_loop,
    save_rules,
)
from wechat_uia import Message, WeChatClient  # noqa: E402


# ------------------------------------------------------------------ 假控件


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class FakeControl:
    """模拟 uiautomation 控件，只实现被测代码用到的那部分接口。"""

    def __init__(
        self,
        name: str = "",
        control_type: str = "ListItemControl",
        children: list | None = None,
        buttons: list | None = None,
        rect: FakeRect | None = None,
        runtime_id: list | None = None,
        exists: bool = True,
    ) -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.ClassName = ""
        self._children = children or []
        self._buttons = buttons or []
        self._rect = rect or FakeRect(0, 0, 300, 60)
        self._runtime_id = runtime_id
        self._exists = exists

    @property
    def BoundingRectangle(self) -> FakeRect:
        return self._rect

    def GetChildren(self) -> list:
        return list(self._children)

    def GetRuntimeId(self) -> list | None:
        return self._runtime_id

    def Exists(self, *args, **kwargs) -> bool:  # noqa: ARG002
        return self._exists

    def ButtonControl(self, foundIndex: int = 1) -> FakeControl:  # noqa: N803
        if 1 <= foundIndex <= len(self._buttons):
            return self._buttons[foundIndex - 1]
        return FakeControl(control_type="ButtonControl", exists=False)


def make_message_item(content: str, sender: str = "", side: str = "left", mid=None) -> FakeControl:
    """构造一条聊天消息控件；side 为 left/right/none 分别表示对方/自己/系统消息。"""
    if side == "none":
        buttons = []
    else:
        left = 10 if side == "left" else 250
        avatar = FakeControl(sender, "ButtonControl", rect=FakeRect(left, 10, left + 40, 50))
        buttons = [avatar]
    return FakeControl(
        content,
        "ListItemControl",
        buttons=buttons,
        rect=FakeRect(0, 0, 300, 60),
        runtime_id=mid or [42, abs(hash(content)) % 10000],
    )


def make_session_item(name: str, last_msg: str = "你好") -> FakeControl:
    """构造微信 3.9 会话列表项：ListItem > Pane > Pane > Pane > [名字, 时间, 内容]。"""
    texts = [
        FakeControl(name, "TextControl"),
        FakeControl("昨天", "TextControl"),
        FakeControl(last_msg, "TextControl"),
    ]
    level4 = FakeControl(control_type="PaneControl", children=texts)
    level3 = FakeControl(control_type="PaneControl", children=[level4])
    level2 = FakeControl(control_type="PaneControl", children=[level3])
    return FakeControl(control_type="ListItemControl", children=[level2])


# ------------------------------------------------------------------ 消息解析


class TestMessageParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.wx = WeChatClient()

    def test_message_incoming_flag(self) -> None:
        self.assertTrue(Message("friend", "hi", "张三", "1").incoming)
        for mtype in ("self", "sys", "time", "recall"):
            self.assertFalse(Message(mtype, "hi", "", "1").incoming)

    def test_friend_message_detected_by_left_avatar(self) -> None:
        msg = self.wx._parse_item(make_message_item("你好", sender="张三", side="left"))
        self.assertEqual(msg.type, "friend")
        self.assertEqual(msg.content, "你好")
        self.assertEqual(msg.sender, "张三")
        self.assertTrue(msg.incoming)

    def test_self_message_detected_by_right_avatar(self) -> None:
        msg = self.wx._parse_item(make_message_item("我发的", sender="我", side="right"))
        self.assertEqual(msg.type, "self")
        self.assertFalse(msg.incoming)

    def test_system_message_has_no_avatar(self) -> None:
        msg = self.wx._parse_item(make_message_item("你已添加了张三", side="none"))
        self.assertEqual(msg.type, "sys")
        self.assertFalse(msg.incoming)

    def test_recall_message_classified(self) -> None:
        msg = self.wx._parse_item(make_message_item("张三 撤回了一条消息", side="none"))
        self.assertEqual(msg.type, "recall")
        self.assertFalse(msg.incoming)

    def test_runtime_id_is_stable_key(self) -> None:
        item = make_message_item("你好", mid=[42, 777])
        self.assertEqual(self.wx._runtime_id(item), "42777")
        self.assertEqual(self.wx._runtime_id(item), "42777")

    def test_runtime_id_falls_back_to_name_and_position(self) -> None:
        item = make_message_item("没有运行时 ID", mid=None)
        item._runtime_id = None
        key = self.wx._runtime_id(item)
        self.assertIn("没有运行时 ID", key)


# ------------------------------------------------------------------ 会话解析


class TestSessionParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.wx = WeChatClient()

    def test_session_name_read_from_nested_text(self) -> None:
        self.assertEqual(self.wx._session_name(make_session_item("文件传输助手")), "文件传输助手")

    def test_session_name_falls_back_to_control_name(self) -> None:
        flat = FakeControl("张三", "ListItemControl", children=[])
        self.assertEqual(self.wx._session_name(flat), "张三")


# ------------------------------------------------------------------ 去重逻辑


class FakeChatWindow:
    """假的聊天窗口，_read_messages 会被替换掉。"""

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages


class TestNewMessageDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.wx = WeChatClient()
        self.window = FakeChatWindow([])
        self.wx._find_chat_window = lambda who: self.window  # type: ignore[assignment]
        self.wx._read_messages = lambda wnd: list(wnd.messages)  # type: ignore[assignment]

    def feed(self, *messages: Message) -> None:
        self.window.messages = list(messages)

    def test_first_call_returns_everything_as_new(self) -> None:
        # 未建立基线时，窗口内的消息都算新消息（真机上会先 open_chat 建立基线）
        self.feed(Message("friend", "你好", "张三", "1"))
        self.assertEqual(len(self.wx.get_new_messages("张三")), 1)

    def test_repeated_poll_does_not_repeat_message(self) -> None:
        self.feed(Message("friend", "你好", "张三", "1"))
        self.assertEqual(len(self.wx.get_new_messages("张三")), 1)
        self.assertEqual(self.wx.get_new_messages("张三"), [])

    def test_only_new_message_returned(self) -> None:
        self.feed(Message("friend", "你好", "张三", "1"))
        self.wx.get_new_messages("张三")
        self.feed(Message("friend", "你好", "张三", "1"), Message("friend", "在吗", "张三", "2"))
        fresh = self.wx.get_new_messages("张三")
        self.assertEqual([m.content for m in fresh], ["在吗"])

    def test_own_messages_are_filtered_out(self) -> None:
        self.feed(Message("self", "我先说的", "", "1"), Message("friend", "回复你", "张三", "2"))
        fresh = self.wx.get_new_messages("张三")
        self.assertEqual([m.content for m in fresh], ["回复你"])

    def test_system_messages_are_filtered_out(self) -> None:
        self.feed(Message("sys", "你已添加了张三", "", "1"), Message("recall", "撤回了一条消息", "", "2"))
        self.assertEqual(self.wx.get_new_messages("张三"), [])

    def test_text_only_skips_media_placeholders(self) -> None:
        self.feed(
            Message("friend", "[图片]", "张三", "1"),
            Message("friend", "这是文字", "张三", "2"),
        )
        fresh = self.wx.get_new_messages("张三", text_only=True)
        self.assertEqual([m.content for m in fresh], ["这是文字"])

    def test_scrolled_away_message_not_replied_again(self) -> None:
        # 已处理过的消息滚出视口后再滚回来，不应被当成新消息重复回复
        self.feed(Message("friend", "旧消息", "张三", "1"), Message("friend", "新消息", "张三", "2"))
        self.assertEqual(len(self.wx.get_new_messages("张三")), 2)
        # 旧消息滚出视口
        self.feed(Message("friend", "新消息", "张三", "2"))
        self.assertEqual(self.wx.get_new_messages("张三"), [])
        # 之后往上滚动聊天记录，旧消息重新进入视口
        self.feed(Message("friend", "旧消息", "张三", "1"), Message("friend", "新消息", "张三", "2"))
        self.assertEqual(self.wx.get_new_messages("张三"), [])

    def test_new_message_still_returned_after_earlier_ones(self) -> None:
        # 去重不能让后续的新消息被漏掉
        self.feed(Message("friend", "第一条", "张三", "1"))
        self.wx.get_new_messages("张三")
        self.feed(Message("friend", "第一条", "张三", "1"), Message("friend", "第二条", "张三", "2"))
        fresh = self.wx.get_new_messages("张三")
        self.assertEqual([m.content for m in fresh], ["第二条"])

    def test_reopening_chat_keeps_previously_seen_ids(self) -> None:
        # 窗口重开时只追加基线，之前已处理过的消息 ID 绝不能被丢弃
        self.feed(Message("friend", "旧消息", "张三", "1"))
        self.wx.get_new_messages("张三")                      # 处理掉，记录 id=1
        self.feed(Message("friend", "新消息", "张三", "2"))    # 此时窗口里只剩 id=2
        self.wx.open_chat("张三")                             # 重开：追加 2，但 1 必须保留
        self.assertIn("1", self.wx._seen["张三"])
        # 旧消息之后滚回视口，不应被重复回复
        self.feed(Message("friend", "旧消息", "张三", "1"), Message("friend", "新消息", "张三", "2"))
        self.assertEqual(self.wx.get_new_messages("张三"), [])

    def test_seen_capacity_evicts_oldest_only(self) -> None:
        # 记录超过容量上限时淘汰最旧的，但不影响新消息识别
        from wechat_uia import SEEN_CAPACITY

        for index in range(SEEN_CAPACITY + 5):
            self.feed(Message("friend", f"msg{index}", "张三", str(index)))
            self.wx.get_new_messages("张三")
        self.assertLessEqual(len(self.wx._seen["张三"]), SEEN_CAPACITY)
        # 最新一条仍被记住，不会被重复当成新消息
        self.feed(Message("friend", f"msg{SEEN_CAPACITY + 4}", "张三", str(SEEN_CAPACITY + 4)))
        self.assertEqual(self.wx.get_new_messages("张三"), [])

    def test_closed_window_reopens_without_replaying_history(self) -> None:
        opened = {"count": 0}

        def fake_open(who: str):  # noqa: ANN202
            opened["count"] += 1
            return self.window

        self.wx._find_chat_window = lambda who: None  # type: ignore[assignment]
        self.wx.open_chat = fake_open  # type: ignore[assignment]
        self.assertEqual(self.wx.get_new_messages("张三"), [])
        self.assertEqual(opened["count"], 1)


# ------------------------------------------------------------------ 模板与规则


class TestRenderReply(unittest.TestCase):
    def test_plain_text_returned_unchanged(self) -> None:
        self.assertEqual(render_reply("我在忙，稍后回复", None), "我在忙，稍后回复")

    def test_content_placeholder_replaced(self) -> None:
        msg = Message("friend", "在吗", "张三", "1")
        self.assertEqual(render_reply("已收到您的消息：{content}", msg), "已收到您的消息：在吗")

    def test_sender_placeholder_replaced(self) -> None:
        msg = Message("friend", "在吗", "张三", "1")
        self.assertEqual(render_reply("你好 {sender}", msg), "你好 张三")

    def test_unknown_placeholder_left_alone(self) -> None:
        msg = Message("friend", "在吗", "张三", "1")
        self.assertEqual(render_reply("金额 {100} 元", msg), "金额 {100} 元")

    def test_braces_in_content_do_not_crash(self) -> None:
        msg = Message("friend", "{不是格式串}", "张三", "1")
        self.assertEqual(render_reply("{content}", msg), "{不是格式串}")


class TestRulePersistence(unittest.TestCase):
    def test_save_then_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            original = [Rule("张三", "在忙"), Rule("李四", "收到\n稍后联系")]
            save_rules(path, original)
            loaded = load_rules(path)
            self.assertEqual([(r.contact, r.reply) for r in loaded],
                             [("张三", "在忙"), ("李四", "收到\n稍后联系")])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_rules(Path("不存在.json")), [])

    def test_corrupt_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{不是合法 JSON", encoding="utf-8")
            self.assertEqual(load_rules(path), [])

    def test_partial_entries_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(
                '{"rules": [{"contact": "张三", "reply": "好"}, {"contact": "李四"}]}',
                encoding="utf-8",
            )
            loaded = load_rules(path)
            self.assertEqual([r.contact for r in loaded], ["张三"])

    def test_bare_list_format_accepted(self) -> None:
        # load_rules 也接受不带 "rules" 外层键的裸列表写法
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text('[{"contact": "张三", "reply": "好"}]', encoding="utf-8")
            self.assertEqual([r.contact for r in load_rules(path)], ["张三"])


class TestParseSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.names = ["文件传输助手", "张三", "李四", "产品群"]

    def test_indexes(self) -> None:
        self.assertEqual(parse_selection("1,3", self.names), ["文件传输助手", "李四"])

    def test_space_and_chinese_comma(self) -> None:
        self.assertEqual(parse_selection("2 4", self.names), ["张三", "产品群"])
        self.assertEqual(parse_selection("2，3", self.names), ["张三", "李四"])

    def test_range(self) -> None:
        self.assertEqual(parse_selection("2-3", self.names), ["张三", "李四"])

    def test_by_name(self) -> None:
        self.assertEqual(parse_selection("张三", self.names), ["张三"])

    def test_out_of_range_ignored(self) -> None:
        self.assertEqual(parse_selection("99", self.names), [])

    def test_unknown_name_ignored(self) -> None:
        self.assertEqual(parse_selection("王五", self.names), [])

    def test_empty_input(self) -> None:
        self.assertEqual(parse_selection("   ", self.names), [])


# ------------------------------------------------------------------ 监听循环


class FakeWeChat:
    """按脚本推送消息的假客户端，用于验证 run_loop 的回复行为。"""

    def __init__(self, script: list[list[Message]]) -> None:
        self.script = script
        self.sent: list[tuple[str, str]] = []
        self.calls = 0

    def get_new_messages(self, who: str, text_only: bool = False) -> list[Message]:  # noqa: ARG002
        if self.calls >= len(self.script):
            raise KeyboardInterrupt  # 让监听循环自然退出
        batch = self.script[self.calls]
        self.calls += 1
        return batch

    def send_message(self, who: str, text: str, restore_clipboard: bool = True) -> bool:  # noqa: ARG002
        self.sent.append((who, text))
        return True


def make_args(**overrides) -> Namespace:
    base = dict(
        interval=0.0,
        cooldown=0.0,
        text_only=False,
        reply_each=False,
        restore_clipboard=True,
    )
    base.update(overrides)
    return Namespace(**base)


class TestRunLoop(unittest.TestCase):
    def test_replies_once_per_poll_by_default(self) -> None:
        wx = FakeWeChat([[Message("friend", "在吗", "张三", "1"),
                          Message("friend", "快点回", "张三", "2")]])
        rules = [Rule("张三", "我在忙，稍后回复你")]
        stats = {"张三": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(), stats)  # type: ignore[arg-type]
        self.assertEqual(wx.sent, [("张三", "我在忙，稍后回复你")])
        self.assertEqual(stats["张三"], 1)

    def test_reply_each_answers_every_message(self) -> None:
        wx = FakeWeChat([[Message("friend", "在吗", "张三", "1"),
                          Message("friend", "快点回", "张三", "2")]])
        rules = [Rule("张三", "收到")]
        stats = {"张三": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(reply_each=True), stats)  # type: ignore[arg-type]
        self.assertEqual(wx.sent, [("张三", "收到"), ("张三", "收到")])
        self.assertEqual(stats["张三"], 2)

    def test_template_uses_latest_message(self) -> None:
        wx = FakeWeChat([[Message("friend", "第一条", "张三", "1"),
                          Message("friend", "第二条", "张三", "2")]])
        rules = [Rule("张三", "你说的是：{content}")]
        stats = {"张三": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(), stats)  # type: ignore[arg-type]
        self.assertEqual(wx.sent, [("张三", "你说的是：第二条")])

    def test_multiple_contacts_are_independent(self) -> None:
        wx = FakeWeChat([
            [Message("friend", "hi", "张三", "1")],
            [Message("friend", "hello", "李四", "1")],
        ])
        rules = [Rule("张三", "回张三"), Rule("李四", "回李四")]
        stats = {"张三": 0, "李四": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(), stats)  # type: ignore[arg-type]
        self.assertEqual(wx.sent, [("张三", "回张三"), ("李四", "回李四")])
        self.assertEqual(stats, {"张三": 1, "李四": 1})

    def test_empty_poll_sends_nothing(self) -> None:
        wx = FakeWeChat([[]])
        rules = [Rule("张三", "收到")]
        stats = {"张三": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(), stats)  # type: ignore[arg-type]
        self.assertEqual(wx.sent, [])
        self.assertEqual(stats["张三"], 0)

    def test_send_failure_does_not_count_as_reply(self) -> None:
        class FailingWeChat(FakeWeChat):
            def send_message(self, who: str, text: str, restore_clipboard: bool = True) -> bool:  # noqa: ARG002
                return False

        wx = FailingWeChat([[Message("friend", "在吗", "张三", "1")]])
        rules = [Rule("张三", "收到")]
        stats = {"张三": 0}
        with self.assertRaises(KeyboardInterrupt):
            run_loop(wx, rules, make_args(), stats)  # type: ignore[arg-type]
        self.assertEqual(stats["张三"], 0)


# ------------------------------------------------------------------ 命令行


class TestCli(unittest.TestCase):
    def test_to_requires_reply(self) -> None:
        # --to 必须搭配 --reply；用 mock 跳过微信连接，使其与本机是否装了微信无关
        with mock.patch.object(auto_reply.WeChatClient, "connect", return_value="tester"):
            self.assertEqual(auto_reply.main(["--to", "张三"]), 1)

    def test_check_returns_nonzero_without_wechat(self) -> None:
        # 找不到微信窗口时自检应返回 2 而不是抛异常
        fake = mock.Mock()
        fake.Exists.return_value = False
        with mock.patch("uiautomation.WindowControl", return_value=fake):
            self.assertEqual(auto_reply.cmd_check(), 2)

    def test_parse_args_defaults(self) -> None:
        args = auto_reply.parse_args([])
        self.assertEqual(args.config, "rules.json")
        self.assertEqual(args.interval, 1.0)
        self.assertFalse(args.reply_each)
        self.assertFalse(args.text_only)
        # run_loop 直接读 args.restore_clipboard，必须由 parse_args 归一化出来
        self.assertTrue(args.restore_clipboard)

    def test_no_restore_clipboard_flag(self) -> None:
        args = auto_reply.parse_args(["--no-restore-clipboard"])
        self.assertFalse(args.restore_clipboard)

    def test_parse_args_overrides(self) -> None:
        args = auto_reply.parse_args(["--to", "张三", "--reply", "在忙", "--interval", "2.5", "--reply-each"])
        self.assertEqual(args.to, "张三")
        self.assertEqual(args.interval, 2.5)
        self.assertTrue(args.reply_each)


if __name__ == "__main__":
    unittest.main(verbosity=2)
