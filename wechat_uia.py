"""微信 PC 客户端（3.9.x）UI 自动化封装。

直接基于 Windows UIAutomation 驱动已登录的微信桌面客户端，不依赖已从 PyPI
下架的 `wxauto`（其底层也是 UIAutomation）。

依赖：
    uiautomation  —— 控件树遍历与操作
    pyperclip     —— 剪贴板写入（发送中文最可靠的方式）

窗口约定（微信 3.9.x）：
    主窗口      ClassName = 'WeChatMainWndForPC'
    独立聊天窗口 ClassName = 'ChatWnd'，Name = 会话名

本模块只做「读消息 / 发消息」两件事，不含任何业务规则。
"""

from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import pyperclip
import uiautomation as auto

# uiautomation 默认把调试日志写到工作目录的 @AutomationLog.txt。本程序有自己的
# 日志（auto_reply.log），不需要它，所以关掉；要单独排查 UIA 层问题时把 "" 换成文件路径即可。
auto.Logger.SetLogFile("")

LOG = logging.getLogger("wechat.uia")

MAIN_WINDOW_CLASS = "WeChatMainWndForPC"
CHAT_WINDOW_CLASS = "ChatWnd"

# 每个会话记住的「已见消息 ID」上限。微信聊天窗口只加载最近若干条消息，
# 这个容量足以覆盖任何可能重新滚回视口的范围，同时避免长时间运行后无限增长。
SEEN_CAPACITY = 500

# Win32 常量。用 ctypes 直接调用，避免额外引入 pywin32。
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class WeChatError(RuntimeError):
    """微信自动化过程中的可预期错误。"""


class WeChatNotFound(WeChatError):
    """未找到微信主窗口：客户端未启动、未登录或仍是登录二维码界面。"""


class ChatNotFound(WeChatError):
    """在会话列表中找不到指定联系人/群聊。"""


@dataclass
class Message:
    """一条聊天消息。

    Attributes:
        type: 'friend'（对方发的）/ 'self'（自己发的）/ 'sys' / 'time' / 'recall'
        content: 消息文本；图片、文件等非文本消息为 '[图片]' 之类的占位文本
        sender: 发送者昵称（单聊即联系人名，群聊为群成员名）
        id: 用于去重的控件运行时 ID
        control: 底层 UIA 控件，供需要进一步操作时使用
    """

    type: str
    content: str
    sender: str
    id: str
    control: Any = field(default=None, repr=False)

    @property
    def incoming(self) -> bool:
        """是否为「对方发来」的消息（只有这类消息才需要自动回复）。"""
        return self.type == "friend"


def _bring_to_front(hwnd: int) -> bool:
    """把窗口提到前台。

    UIAutomation 的键盘与剪贴板操作要求目标窗口处于前台，而 Windows 限制
    后台进程直接抢占前台。先置顶再取消置顶是通行的绕过手法。
    """
    if not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        _user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        _user32.SetForegroundWindow(hwnd)
        return True
    except OSError:
        LOG.debug("提升窗口到前台失败 hwnd=%s", hwnd, exc_info=True)
        return False


def read_edit_value(edit: Any) -> str:
    """读取输入框当前文本，用于确认粘贴是否成功。

    不同版本的 uiautomation 提供了不同接口，这里做兼容处理。
    """
    getter = getattr(edit, "GetValuePattern", None)
    if callable(getter):
        try:
            pattern = getter()
            if pattern is not None:
                return pattern.Value or ""
        except Exception:  # noqa: BLE001 - UIA 调用失败的原因很多，退化为下一种方式
            pass
    try:
        pattern = edit.GetPattern(auto.PatternId.Value)
        if pattern is not None:
            return pattern.Value or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


class WeChatClient:
    """已登录微信客户端的最小可用封装。

    Example:
        >>> wx = WeChatClient()
        >>> wx.connect()
        >>> wx.open_chat('文件传输助手')
        >>> wx.send_message('文件传输助手', '你好')
    """

    def __init__(self, language: str = "cn", search_timeout: float = 0.5) -> None:
        self.language = language
        self.search_timeout = search_timeout
        self.nickname = ""
        self._main: Optional[Any] = None
        self._sessions_box: Optional[Any] = None
        self._chat_box: Optional[Any] = None
        # 每个会话已见过的消息 ID。用保序的 dict 承载，便于超限时淘汰最旧的。
        # 刻意只增不减：消息滚出视口后 ID 仍然保留，这样它再滚回来时不会被
        # 当成新消息重复回复（容量上限见 SEEN_CAPACITY）。
        self._seen: dict[str, dict[str, None]] = {}

    # ------------------------------------------------------------------ 连接

    def connect(self, timeout: float = 30.0, require_login: bool = True) -> str:
        """等待微信主窗口出现并定位其内部布局。

        Args:
            timeout: 等待微信窗口出现的最长秒数。
            require_login: 为 True 时，若只检测到登录窗口则报错提示先扫码登录。

        Returns:
            当前登录用户的昵称。
        """
        deadline = time.monotonic() + timeout
        main = auto.WindowControl(searchDepth=1, ClassName=MAIN_WINDOW_CLASS)
        while True:
            if main.Exists(maxSearchSeconds=1):
                break
            if require_login and self._login_window_present():
                raise WeChatNotFound("检测到微信登录窗口，请先扫码登录后再运行本程序。")
            if time.monotonic() >= deadline:
                raise WeChatNotFound(
                    f"等待 {timeout:.0f} 秒仍未找到微信主窗口（ClassName={MAIN_WINDOW_CLASS}）。\n"
                    "请确认：1) 微信 PC 客户端已启动；2) 已登录进入主界面；3) 微信版本为 3.9.x。"
                )
            time.sleep(1)

        self._main = main
        _bring_to_front(self._main.NativeWindowHandle)
        # 主窗口刚出现时内部布局可能还没渲染完，给几次重试机会
        for attempt in range(3):
            try:
                self._locate_boxes()
                break
            except WeChatError as exc:
                if attempt == 2:
                    raise
                LOG.debug("主窗口布局尚未就绪，重试：%s", exc)
                time.sleep(0.5)
        self.nickname = self._read_nickname()
        LOG.info("已连接微信，当前登录：%s", self.nickname or "(未知)")
        return self.nickname

    @staticmethod
    def _login_window_present() -> bool:
        try:
            return bool(auto.WindowControl(searchDepth=1, ClassName="WeChatLoginWndForPC").Exists(0))
        except Exception:  # noqa: BLE001
            return False

    def _locate_boxes(self) -> None:
        """定位主窗口内的「导航栏 / 会话列表 / 聊天区」三块布局。

        微信在弹出独立聊天窗口或切换页面后可能重建主窗口控件树，因此本方法
        被设计成可随时重复调用，由 _ensure_layout 在每次会话操作前触发。
        """
        main = self._main
        assert main is not None
        unnamed = [c for c in main.GetChildren() if not c.ClassName]
        if not unnamed:
            raise WeChatError("微信主窗口结构异常：未找到布局容器，请用 --dump 查看控件树。")
        layout = unnamed[0].GetFirstChildControl()
        children = layout.GetChildren() if layout is not None else []
        if len(children) < 3:
            raise WeChatError(
                f"微信主窗口布局与预期不符（找到 {len(children)} 个子控件，期望至少 3 个），"
                "请用 --dump 查看控件树并反馈。"
            )
        self._sessions_box, self._chat_box = children[1], children[2]

    def _ensure_layout(self) -> None:
        """重新获取布局控件引用，确保它们对应当前控件树。

        微信在弹出独立聊天窗口或切换页面后可能重建主窗口控件树，使缓存引用与
        当前界面脱节。会话操作都是低频的（列出列表、建立监听各一次），所以这里
        不做失效判断，直接重新定位——代价仅为几次 GetChildren。
        """
        self._locate_boxes()

    def _read_nickname(self) -> str:
        """读取左上角个人头像按钮的 Name，即当前登录昵称。

        昵称只用于界面提示，取不到不影响功能；这里限制搜索深度与等待时间，
        免得窗口刚被激活、控件树还在重建时白等默认的 10 秒。
        """
        try:
            avatar = self._main.ButtonControl(searchDepth=5)
            if avatar.Exists(self.search_timeout):
                return (avatar.Name or "").strip()
        except Exception:  # noqa: BLE001 - 昵称读取失败不影响主流程
            pass
        return ""

    # ------------------------------------------------------------------ 会话

    def get_sessions(self, limit: int = 100) -> list[str]:
        """按会话列表的显示顺序返回会话名（联系人 + 群聊）。"""
        if self._sessions_box is None:
            raise WeChatError("尚未定位到会话列表，请先调用 connect()。")
        self._ensure_layout()
        return self._collect_sessions(limit)

    def _collect_sessions(self, limit: int) -> list[str]:
        names: list[str] = []
        for _, name in self._iter_session_items(limit):
            if name and name not in names:
                names.append(name)
        return names

    def _iter_session_items(self, limit: int) -> Iterator[tuple[Any, str]]:
        """按显示顺序产出 (会话项, 会话名)。

        这里刻意不用 BoundingRectangle 判断项是否有效：微信会把「已打开独立
        聊天窗口」的会话项报告成 rect=(0,0,0,0)，但它的内容依然读得到。据此
        中断遍历会导致列表里只要有一个会话被打开，它后面的会话就全部丢失
        （表现为「打开第一个联系人就找不到后面的」）。
        """
        box = self._sessions_box
        if box is None:
            return
        item = box.ListItemControl()
        empty_streak = 0
        for _ in range(limit):
            try:
                if not item.Exists(self.search_timeout):
                    return
            except Exception:  # noqa: BLE001
                return
            name = self._session_name(item)
            if name:
                empty_streak = 0
            else:
                # 连续多项都解析不出名字，说明已经走到了列表末尾的空壳控件
                empty_streak += 1
                if empty_streak >= 5:
                    return
            yield item, name
            nxt = item.GetNextSiblingControl()
            if nxt is None:
                return
            item = nxt

    @classmethod
    def _session_name(cls, item: Any) -> str:
        """解析会话名。

        微信 3.9 的会话项结构为 ListItem > (若干层 Pane) > 3 个并列 TextControl,
        依次是「会话名 / 时间 / 最后一条消息」。优先按该结构取第一个 TextControl；
        结构变化时退化为控件自身 Name。
        """
        for depth in (4, 3, 5, 2):
            texts = cls._texts_at_depth(item, depth)
            if texts:
                name = (texts[0].Name or "").strip()
                if name:
                    return name
        return (item.Name or "").strip()

    @classmethod
    def _texts_at_depth(cls, control: Any, target: int, current: int = 1) -> list[Any]:
        out: list[Any] = []
        if current > target:
            return out
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            return out
        for child in children:
            if current == target:
                if child.ControlTypeName == "TextControl":
                    out.append(child)
            else:
                out.extend(cls._texts_at_depth(child, target, current + 1))
        return out

    def _find_session_item(self, who: str) -> Optional[Any]:
        """在会话列表中按名字查找会话项，先精确后模糊。"""
        if self._sessions_box is None:
            return None
        self._ensure_layout()
        return self._search_session_items(who)

    def _search_session_items(self, who: str) -> Optional[Any]:
        fuzzy: Optional[Any] = None
        for item, name in self._iter_session_items(200):
            if name == who:
                return item
            if fuzzy is None and name and (who in name or name in who):
                fuzzy = item
        return fuzzy

    # ------------------------------------------------------------------ 聊天窗口

    def _find_chat_window(self, who: str) -> Optional[Any]:
        """查找某个会话的独立聊天窗口（未打开则返回 None）。"""
        wnd = auto.WindowControl(searchDepth=1, ClassName=CHAT_WINDOW_CLASS, Name=who)
        try:
            if wnd.Exists(maxSearchSeconds=self.search_timeout):
                return wnd
        except Exception:  # noqa: BLE001
            pass
        # 标题可能被微信改写，退化为遍历顶层窗口比对类名与名字
        try:
            for top in auto.GetRootControl().GetChildren():
                if top.ClassName == CHAT_WINDOW_CLASS and (top.Name or "").strip() == who:
                    return top
        except Exception:  # noqa: BLE001
            pass
        return None

    def open_chat(self, who: str, timeout: float = 6.0) -> Any:
        """确保指定会话的独立聊天窗口处于打开状态，并建立消息基线。

        监听依赖独立的 ChatWnd 窗口：它不会打断主窗口当前正在看的会话，
        且多个监听对象互不干扰。

        Returns:
            该会话的聊天窗口控件。
        """
        wnd = self._find_chat_window(who)
        if wnd is None:
            main = self._main
            if main is None:
                raise WeChatError("尚未连接微信，请先调用 connect()。")
            _bring_to_front(main.NativeWindowHandle)
            item = self._find_session_item(who)
            if item is None:
                raise ChatNotFound(
                    f"会话列表中找不到「{who}」。请确认名称完全一致（可用 --list 查看所有会话）。"
                )
            item.DoubleClick(simulateMove=False)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                wnd = self._find_chat_window(who)
                if wnd is not None:
                    break
                time.sleep(0.3)
            if wnd is None:
                raise ChatNotFound(f"已双击「{who}」但聊天窗口未出现，请手动点开该会话后重试。")
            # 新窗口刚出现时消息列表可能尚未渲染完，稍等再建立基线
            time.sleep(0.5)
        # 基线：把窗口内已加载的历史消息计入「已见过」，避免回复旧消息。
        # 这里只追加不重置——若重置，已回复过但已滚出视口的消息 ID 会丢失，
        # 等它再滚回视口时就会被当成新消息重复回复。
        self._remember(who, (m.id for m in self._read_messages(wnd)))
        LOG.debug("已打开聊天窗口：%s（已记录 %d 条消息 ID）", who, len(self._seen[who]))
        return wnd

    def _read_messages(self, wnd: Any) -> list[Message]:
        """读取聊天窗口中当前已加载的全部消息（旧到新）。"""
        try:
            msg_list = wnd.ListControl()
            if not msg_list.Exists(self.search_timeout):
                return []
            items = [i for i in msg_list.GetChildren() if i.ControlTypeName == "ListItemControl"]
        except Exception:  # noqa: BLE001
            LOG.debug("读取消息列表失败", exc_info=True)
            return []
        messages = []
        for item in items:
            parsed = self._parse_item(item)
            if parsed is not None:
                messages.append(parsed)
        return messages

    def _parse_item(self, item: Any) -> Optional[Message]:
        """把消息列表项解析成 Message。

        判断依据是「有没有头像按钮」以及「头像在左还是右」，而不是控件高度——
        后者会随字体缩放（DPI）变化，不可靠。
        """
        mid = self._runtime_id(item)
        content = (item.Name or "").strip()
        avatar = self._find_avatar(item)
        if avatar is None:
            mtype = "recall" if "撤回" in content else "sys"
            return Message(mtype, content, "", mid, item)
        try:
            rect = item.BoundingRectangle
            midline = (rect.left + rect.right) / 2
            if avatar.BoundingRectangle.left < midline:
                sender = (avatar.Name or "").strip()
                return Message("friend", content, sender, mid, item)
        except Exception:  # noqa: BLE001
            LOG.debug("消息位置解析失败，按对方消息处理", exc_info=True)
        return Message("self", content, "", mid, item)

    @staticmethod
    def _find_avatar(item: Any) -> Optional[Any]:
        """取消息项里的头像按钮；系统提示、时间分隔条没有头像。"""
        for index in range(1, 4):
            try:
                btn = item.ButtonControl(foundIndex=index)
                if not btn.Exists(0.1):
                    return None
                if (btn.Name or "").strip():
                    return btn
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _runtime_id(item: Any) -> str:
        """控件的运行时 ID，用作消息去重键。"""
        try:
            runtime_id = item.GetRuntimeId()
            if runtime_id:
                return "".join(str(p) for p in runtime_id)
        except Exception:  # noqa: BLE001
            pass
        # 退化方案：只用消息内容作键。刻意不含坐标——坐标会随滚动变化，键一变，
        # 同一条消息就会被重复回复。代价是个别拿不到 RuntimeId 的消息在内容完全
        # 相同时可能被漏掉，相比重复回复更可接受。
        try:
            return item.Name or ""
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ 消息

    def _remember(self, who: str, ids: Iterator[str]) -> None:
        """把消息 ID 计入「已见过」，并在超出容量时淘汰最旧的记录。

        记录只增不减是有意为之：已回复过的消息一旦滚出聊天窗口，它的 ID 若被
        丢弃，等它重新滚回视口时就会被当成新消息、再回复一遍。
        """
        seen = self._seen.setdefault(who, {})
        for mid in ids:
            if mid:
                seen[mid] = None
        while len(seen) > SEEN_CAPACITY:
            seen.pop(next(iter(seen)))

    def get_new_messages(self, who: str, text_only: bool = False) -> list[Message]:
        """返回 `who` 会话中自上次调用以来的新消息（旧到新）。

        聊天窗口被关闭时会自动重开，但重开后的历史消息视为旧消息，不会返回，
        以免把陈年消息当成新消息回复。同一条消息只会返回一次。
        """
        wnd = self._find_chat_window(who)
        if wnd is None:
            LOG.info("「%s」的聊天窗口已关闭，正在重新打开…", who)
            try:
                self.open_chat(who)
            except WeChatError as exc:
                LOG.warning("重新打开「%s」失败：%s", who, exc)
            return []

        messages = self._read_messages(wnd)
        seen = self._seen.setdefault(who, {})
        fresh = [m for m in messages if m.id and m.id not in seen]
        # 只追加、不重建：让滚出视口的消息 ID 继续留在记录里，防止重复回复
        self._remember(who, (m.id for m in messages))
        if not fresh:
            return []
        fresh = [m for m in fresh if m.incoming]
        if text_only:
            fresh = [m for m in fresh if not m.content.startswith("[") or not m.content.endswith("]")]
        return fresh

    def send_message(self, who: str, text: str, restore_clipboard: bool = True) -> bool:
        """向指定会话发送一条文本消息。

        通过剪贴板粘贴发送，这是中文与换行最可靠的方式。默认会备份并恢复
        用户原有的剪贴板文本，避免程序「偷走」剪贴板。

        Returns:
            是否发送成功。
        """
        if not text:
            return False
        wnd = self._find_chat_window(who)
        if wnd is None:
            LOG.warning("发送失败：未找到「%s」的聊天窗口", who)
            return False

        edit = self._find_editbox(wnd)
        if edit is None:
            LOG.warning("发送失败：在「%s」聊天窗口里找不到输入框", who)
            return False

        _bring_to_front(wnd.NativeWindowHandle)
        try:
            if not edit.HasKeyboardFocus:
                edit.Click(simulateMove=False)
        except Exception:  # noqa: BLE001
            pass

        backup = self._clipboard_text() if restore_clipboard else None
        try:
            pyperclip.copy(text)
            edit.SendKeys("{Ctrl}a", waitTime=0)
            edit.SendKeys("{Ctrl}v", waitTime=0.05)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if read_edit_value(edit):
                    break
                time.sleep(0.1)
            else:
                LOG.warning("粘贴到「%s」的输入框未生效，已放弃本次发送", who)
                return False
            edit.SendKeys("{Enter}")
            time.sleep(0.2)
            return True
        except Exception:  # noqa: BLE001
            LOG.exception("向「%s」发送消息时出错", who)
            return False
        finally:
            if backup is not None:
                try:
                    pyperclip.copy(backup)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _find_editbox(wnd: Any) -> Optional[Any]:
        """定位聊天窗口的输入框。"""
        try:
            edit = wnd.EditControl()
            if edit.Exists(0.5):
                return edit
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _clipboard_text() -> Optional[str]:
        try:
            return pyperclip.paste()
        except Exception:  # noqa: BLE001 - 剪贴板里可能是图片等非文本内容
            return None

    # ------------------------------------------------------------------ 调试

    def dump_tree(self, max_depth: int = 8) -> str:
        """导出微信主窗口的控件树，用于排查微信版本差异导致的定位失败。"""
        main = self._main
        if main is None:
            main = auto.WindowControl(searchDepth=1, ClassName=MAIN_WINDOW_CLASS)
            if not main.Exists(3):
                raise WeChatNotFound("未找到微信主窗口，无法导出控件树。")
        lines: list[str] = []

        def walk(control: Any, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                name = (control.Name or "")[:60]
                lines.append(
                    f"{'  ' * depth}{control.ControlTypeName} "
                    f"ClassName={control.ClassName!r} Name={name!r}"
                )
                for child in control.GetChildren():
                    walk(child, depth + 1)
            except Exception:  # noqa: BLE001
                lines.append(f"{'  ' * depth}<读取失败>")

        walk(main, 0)
        return "\n".join(lines)
