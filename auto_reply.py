"""微信自动回复助手 —— 指定联系人与回复内容，收到消息后自动回复。

用法：
    python auto_reply.py                 # 交互式选择联系人和回复内容
    python auto_reply.py --list          # 只列出当前微信会话
    python auto_reply.py --check         # 环境自检（不发送任何消息）
    python auto_reply.py --dump          # 导出微信控件树，用于排查定位失败
    python auto_reply.py --to 张三 --reply "我在忙，稍后回复你"

工作原理：基于 Windows UIAutomation 驱动已登录的微信 PC 客户端（3.9.x）。
运行期间微信必须保持登录且不要最小化，程序会为每个监听对象打开独立聊天窗口。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from wechat_uia import (
    ChatNotFound,
    Message,
    WeChatClient,
    WeChatError,
    WeChatNotFound,
)

DEFAULT_CONFIG = "rules.json"
LINE = "-" * 62


# --------------------------------------------------------------------- 数据


@dataclass
class Rule:
    """一条自动回复规则：指定联系人收到消息后，回复指定内容。"""

    contact: str
    reply: str


def load_rules(path: Path) -> list[Rule]:
    """从 JSON 文件读取规则；文件不存在或损坏时返回空列表。"""
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[!] 读取规则文件失败（{exc}），将忽略该文件。")
        return []
    rules = []
    items = raw.get("rules", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    for item in items:
        if isinstance(item, dict) and item.get("contact") and item.get("reply") is not None:
            rules.append(Rule(str(item["contact"]), str(item["reply"])))
    return rules


def save_rules(path: Path, rules: list[Rule]) -> None:
    payload = {"rules": [asdict(r) for r in rules]}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 规则已保存到 {path}")
    except OSError as exc:
        print(f"[!] 保存规则失败：{exc}")


def render_reply(template: str, msg: Message | None) -> str:
    """把回复模板里的占位符替换成实际内容。

    支持的占位符：{content} 对方消息、{sender} 发送者、{time} 当前时间、{contact} 会话名。
    使用字符串替换而非 str.format，这样内容里出现花括号也不会报错。
    """
    if msg is None:
        return template
    mapping = {
        "{content}": msg.content,
        "{sender}": msg.sender or "",
        "{time}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "{contact}": msg.sender or "",
    }
    result = template
    for key, value in mapping.items():
        result = result.replace(key, value)
    return result


# --------------------------------------------------------------------- 交互


def ask(prompt: str, default: str = "") -> str:
    """读取一行输入；支持直接回车取默认值，管道输入结束时返回默认值。"""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = ask(f"{prompt} {hint}: ").lower()
    if not answer:
        return default
    return answer in ("y", "yes", "是")


def parse_selection(raw: str, names: list[str]) -> list[str]:
    """把用户输入解析成会话名列表。

    既支持序号（"1,3" / "1 3" / "1-3"），也支持直接输入名字。
    """
    raw = raw.strip()
    if not raw:
        return []
    tokens = [t for t in raw.replace(",", " ").replace("，", " ").split() if t]
    picked: list[str] = []
    for token in tokens:
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(names):
                if names[index - 1] not in picked:
                    picked.append(names[index - 1])
            else:
                print(f"[!] 序号 {index} 超出范围，已忽略。")
            continue
        low = token.lower()
        if "-" in token:
            start, _, end = token.partition("-")
            if start.isdigit() and end.isdigit():
                for index in range(int(start), int(end) + 1):
                    if 1 <= index <= len(names) and names[index - 1] not in picked:
                        picked.append(names[index - 1])
                continue
        matched = [n for n in names if n == token] or [n for n in names if low in n.lower()]
        if matched:
            for name in matched:
                if name not in picked:
                    picked.append(name)
        else:
            print(f"[!] 找不到会话「{token}」，已忽略。")
    return picked


def print_rules(rules: list[Rule]) -> None:
    print(f"\n已配置 {len(rules)} 条规则：")
    for rule in rules:
        preview = rule.reply.replace("\n", " ")
        if len(preview) > 48:
            preview = preview[:45] + "..."
        print(f"  - {rule.contact}  ->  {preview}")


def interactive_setup(wx: WeChatClient, rules: list[Rule]) -> list[Rule]:
    """交互式增删规则：列出会话 -> 选联系人 -> 输入回复内容。"""
    print(f"\n{LINE}\n[2/3] 配置自动回复规则\n{LINE}")
    try:
        sessions = wx.get_sessions()
    except WeChatError as exc:
        print(f"[!] 获取会话列表失败：{exc}")
        return rules

    if not sessions:
        print("[!] 会话列表为空。请先在微信里打开几个聊天，再重新运行本程序。")
        return rules

    while True:
        print(f"\n当前微信会话（共 {len(sessions)} 个）：")
        for index, name in enumerate(sessions, 1):
            print(f"  {index:3d}) {name}")

        raw = ask("\n请输入要自动回复的联系人序号（多个用逗号分隔，也支持直接输入名字，回车结束）：")
        if not raw:
            break
        picked = parse_selection(raw, sessions)
        if not picked:
            print("[!] 未选中任何有效会话，请重试。")
            continue

        for name in picked:
            print(f"\n>> 对【{name}】设置回复内容")
            print("   （可直接回车跳过；内容里可用 {content} 表示对方消息、{sender} 表示发送者、{time} 表示当前时间）")
            reply = ask("   回复内容: ")
            if not reply:
                print(f"   [跳过] 未输入内容，不监听 {name}")
                continue
            for existing in rules:
                if existing.contact == name:
                    existing.reply = reply
                    print(f"   [OK] 已更新「{name}」的回复内容")
                    break
            else:
                rules.append(Rule(name, reply))
                print(f"   [OK] 已添加「{name}」的自动回复")

        if not confirm("\n是否继续添加其他联系人？", default=False):
            break

    return rules


# --------------------------------------------------------------------- 监听


def verify_rules(wx: WeChatClient, rules: list[Rule]) -> list[Rule]:
    """逐个打开会话窗口并建立消息基线，剔除无法监听的对象。"""
    print(f"\n{LINE}\n[3/3] 校验并启动监听\n{LINE}")
    valid: list[Rule] = []
    for rule in rules:
        try:
            wx.open_chat(rule.contact)
            valid.append(rule)
            print(f"  [OK] {rule.contact} 已就绪")
        except (ChatNotFound, WeChatError) as exc:
            print(f"  [!] 跳过 {rule.contact}：{exc}")
    return valid


def run_loop(
    wx: WeChatClient, rules: list[Rule], args: argparse.Namespace, stats: dict[str, int]
) -> None:
    """主监听循环：轮询新消息 -> 匹配规则 -> 回复。

    stats 由调用方持有并原地更新，这样按下 Ctrl+C 之后仍能打印出正确统计。
    """
    last_reply: dict[str, float] = {}

    print(f"\n{LINE}")
    print(f"开始监听 {len(rules)} 个会话，轮询间隔 {args.interval:g} 秒。按 Ctrl+C 停止。")
    print("提示：请勿关闭这些会话的独立聊天窗口，也不要最小化微信主窗口。")
    print(f"{LINE}\n")

    errors_in_row = 0
    while True:
        for rule in rules:
            try:
                messages = wx.get_new_messages(rule.contact, text_only=args.text_only)
                errors_in_row = 0
            except Exception as exc:  # noqa: BLE001 - 单次轮询失败不应终止监听
                errors_in_row += 1
                logging.getLogger("auto_reply").warning(
                    "读取「%s」消息出错：%s", rule.contact, exc
                )
                if errors_in_row >= 5:
                    raise WeChatError(
                        "连续多次读取消息失败，微信可能已退出或界面被改变，程序停止。"
                    ) from exc
                continue

            if not messages:
                continue

            for msg in messages:
                stamp = datetime.now().strftime("%H:%M:%S")
                print(f"[{stamp}] {rule.contact} 发来消息: {msg.content}")

            if args.cooldown > 0:
                elapsed = time.monotonic() - last_reply.get(rule.contact, 0.0)
                if elapsed < args.cooldown:
                    logging.getLogger("auto_reply").debug(
                        "「%s」处于冷却期（%.1fs），本次不回复", rule.contact, args.cooldown - elapsed
                    )
                    continue

            # 默认每轮每个联系人只回复一次，避免对方连发时刷屏
            targets = messages if args.reply_each else [messages[-1]]
            if not args.reply_each and len(messages) > 1:
                logging.getLogger("auto_reply").debug(
                    "「%s」本轮有 %d 条新消息，合并回复一次", rule.contact, len(messages)
                )

            for msg in targets:
                text = render_reply(rule.reply, msg)
                if wx.send_message(rule.contact, text, restore_clipboard=args.restore_clipboard):
                    last_reply[rule.contact] = time.monotonic()
                    stats[rule.contact] += 1
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[{stamp}] 已回复 {rule.contact}: {text}")
                else:
                    logging.getLogger("auto_reply").warning("回复「%s」失败", rule.contact)

        time.sleep(args.interval)


def summarize(stats: dict[str, int]) -> None:
    print(f"\n{LINE}\n运行统计")
    total = sum(stats.values())
    for contact, count in stats.items():
        print(f"  {contact}: 已回复 {count} 条")
    print(f"  合计: {total} 条")


# --------------------------------------------------------------------- 命令


def cmd_check() -> int:
    """环境自检：确认依赖、微信窗口与登录状态。"""
    print(f"{LINE}\n环境自检\n{LINE}")
    print(f"  Python      : {sys.version.split()[0]}")

    if os.name != "nt":
        print("  [!] 本程序只支持 Windows。")
        return 2

    try:
        import uiautomation as auto
    except ImportError:
        print("  [!] 未安装 uiautomation，请先运行：pip install -r requirements.txt")
        return 2
    print(f"  uiautomation: {getattr(auto, 'VERSION', '已安装')}")

    try:
        main = auto.WindowControl(searchDepth=1, ClassName="WeChatMainWndForPC")
        if main.Exists(3):
            print("  [OK] 检测到微信主窗口（3.9.x）")
            return 0
        login = auto.WindowControl(searchDepth=1, ClassName="WeChatLoginWndForPC")
        if login.Exists(0.5):
            print("  [!] 微信已启动但处于登录界面，请先扫码登录。")
            return 2
        print("  [!] 未检测到微信窗口。可能原因：")
        print("       1) 微信 PC 客户端未启动")
        print("       2) 微信版本是 4.x（本项目仅支持 3.9.x，4.x 界面类名不同）")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] UIAutomation 初始化失败：{exc}")
        return 2


def cmd_list(wx: WeChatClient) -> int:
    sessions = wx.get_sessions()
    if not sessions:
        print("[!] 会话列表为空。请先在微信里打开几个聊天再试。")
        return 1
    print(f"\n当前微信会话（共 {len(sessions)} 个，从上到下）：")
    for index, name in enumerate(sessions, 1):
        print(f"  {index:3d}) {name}")
    print("\n把名称原样填给 --to 参数即可，例如：")
    print(f'  python auto_reply.py --to "{sessions[0]}" --reply "你好，我在忙，稍后回复"')
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="微信自动回复助手：指定联系人，收到消息后自动回复指定内容。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python auto_reply.py                                  交互式配置\n"
            "  python auto_reply.py --list                            查看所有会话名称\n"
            '  python auto_reply.py --to 张三 --reply "稍后回复你"      直接指定一条规则\n'
            "  python auto_reply.py -c rules.json                     使用保存好的规则\n"
        ),
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=f"规则文件路径（默认 {DEFAULT_CONFIG}）")
    parser.add_argument("--to", help="直接指定要自动回复的联系人（跳过交互）")
    parser.add_argument("--reply", help="配合 --to 使用的回复内容")
    parser.add_argument("--list", action="store_true", help="列出所有会话后退出")
    parser.add_argument("--dump", action="store_true", help="导出微信控件树，用于排查故障")
    parser.add_argument("--check", action="store_true", help="环境自检，不发送任何消息")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1.0，不建议低于 0.5）"
    )
    parser.add_argument(
        "--cooldown", type=float, default=0.0, help="同一联系人两次回复的最小间隔秒数（默认 0，不限制）"
    )
    parser.add_argument("--text-only", action="store_true", help="只回复文本消息，忽略图片/文件/语音")
    parser.add_argument(
        "--reply-each", action="store_true", help="对方连发多条时逐条回复（默认合并为一条）"
    )
    parser.add_argument(
        "--no-restore-clipboard", action="store_true", help="发送后不恢复原有剪贴板内容"
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    # 命令行给的是否定形式，这里归一化成 run_loop 直接可用的布尔值
    args.restore_clipboard = not args.no_restore_clipboard
    return args


def setup_logging(level: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler("auto_reply.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    # 控制台只留警告以上，正常的收发消息由 print 直接呈现，避免重复
    handlers[0].setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)

    if args.check:
        return cmd_check()

    wx = WeChatClient()
    print(f"{LINE}\n微信自动回复助手\n{LINE}\n")
    print("[1/3] 连接微信 ...")
    try:
        wx.connect()
    except WeChatNotFound as exc:
        print(f"[!] {exc}")
        return 2

    if args.dump:
        output = wx.dump_tree()
        Path("wechat_tree.txt").write_text(output, encoding="utf-8")
        print("[OK] 控件树已写入 wechat_tree.txt")
        return 0

    if args.list:
        return cmd_list(wx)

    config_path = Path(args.config)

    if args.to:
        if not args.reply:
            print("[!] 使用 --to 时必须同时提供 --reply。")
            return 1
        rules = [Rule(args.to, args.reply)]
    else:
        rules = load_rules(config_path)
        if rules:
            print_rules(rules)
            if confirm(f"检测到 {config_path} 里保存的上述规则，是否直接使用？", default=True):
                pass
            else:
                rules = []
        if not rules:
            rules = interactive_setup(wx, rules)
        if not rules:
            print("\n[!] 没有配置任何规则，程序退出。")
            return 1
        print_rules(rules)
        save_rules(config_path, rules)

    rules = verify_rules(wx, rules)
    if not rules:
        print("\n[!] 所有规则都无法监听，程序退出。")
        return 1

    stats = {rule.contact: 0 for rule in rules}
    try:
        run_loop(wx, rules, args, stats)
    except KeyboardInterrupt:
        print("\n\n收到停止信号，正在退出 ...")
    except WeChatError as exc:
        print(f"\n[!] {exc}")
        summarize(stats)
        return 3
    summarize(stats)
    print("已停止监听（监听期间打开的独立聊天窗口请自行关闭）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
