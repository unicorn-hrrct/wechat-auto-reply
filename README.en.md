# WeChat Auto Reply

Specify the contacts you want to auto-reply to and what to say — when they message you, the program replies on your behalf.

```text
[10:23:45] 张三 发来消息: 在吗
[10:23:46] 已回复 张三: 我在忙，稍后回复你
```

[English](README.en.md) | [简体中文](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-lightgrey.svg)](#requirements)

---

## Features

- **Pick contacts and replies** — choose contacts or groups interactively and set a reply for each; rules are persisted to `rules.json`
- **Templated replies** — use `{content}`, `{sender}` and `{time}` to make replies more natural
- **One reply per message** — messages are de-duplicated by control ID, so the same message is never answered twice; new messages still get a reply
- **Non-intrusive** — it listens through a separate chat window, so you can keep using WeChat normally
- **Clipboard-friendly** — your clipboard is backed up before sending and restored afterwards
- **No replaying of history** — a baseline is taken at startup; only messages arriving afterwards trigger a reply
- **Testable offline** — 48 tests run without WeChat installed, so you can change code freely

---

## Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 10 / 11 |
| Python | 3.9 or newer |
| WeChat PC client | **3.9.x required** (WeChat 4.x is not supported — see [Known Limitations](#known-limitations--todo)) |

WeChat must stay **logged in** while running, and its main window must **not be minimized**.

---

## Quick Start

### Installation

```bat
git clone https://github.com/unicorn-hrrct/wechat-auto-reply.git
cd wechat-auto-reply
pip install -r requirements.txt
```

There are only two dependencies: `uiautomation` (drives the WeChat UI) and `pyperclip` (writes to the clipboard for sending). You can verify them with:

```bat
python check_deps.py
```

> **Why not wxauto?**
> `wxauto` used to be the go-to library for this, but it has since been **removed from PyPI** (`pip install wxauto` now fails with `No matching distribution found`) and only the GitHub source remains. `wechat_uia.py` in this project implements sending and receiving directly on top of the same underlying `uiautomation`, so installation never hits a dead dependency.

### 1. Sanity check (optional but recommended)

```bat
python auto_reply.py --check
```

This confirms the WeChat main window can be found. If it fails, the output explains which of two possible causes applies.

### 2. Interactive setup (most common)

```bat
python auto_reply.py
```

The program walks you through three steps:

1. **Connect to WeChat** — locates and activates the logged-in window
2. **Configure rules** — lists all your conversations; enter indexes (`1,3`, `1 3`, `1-3`, or just type a name) and then a reply for each
3. **Start listening** — opens a separate chat window per contact and begins polling

Rules are saved to `rules.json` automatically, and you'll be asked whether to reuse them next time.

### 3. Non-interactive

```bat
python auto_reply.py --to "张三" --reply "我在忙，稍后回复你"
```

Use `--list` first to get the exact conversation name:

```bat
python auto_reply.py --list
```

---

## Reply Templates

Replies are plain text by default, but you can insert placeholders:

| Placeholder | Meaning | Example |
| --- | --- | --- |
| `{content}` | The incoming message text | `已收到您的消息：{content}` |
| `{sender}` | Sender's display name | `你好 {sender}` |
| `{time}` | Current time | `[{time}] 我稍后回复你` |

For example, with the reply `已收到您的消息：{content}`, if someone sends "明天开会吗" they'll receive "已收到您的消息：明天开会吗".

---

## CLI Options

| Option | Description |
| --- | --- |
| `--list` | List all conversation names and exit |
| `--check` | Environment check; sends nothing |
| `--to NAME --reply TEXT` | Add a single rule without the interactive flow |
| `-c, --config PATH` | Rules file path (default `rules.json`) |
| `--interval SEC` | Polling interval, default `1.0`; below `0.5` is not advised |
| `--cooldown SEC` | Minimum gap between two replies to the same contact (default `0`) |
| `--reply-each` | Reply to each message when several arrive at once (default: one combined reply) |
| `--text-only` | Only reply to text, ignoring images/files/voice |
| `--no-restore-clipboard` | Don't restore the previous clipboard content after sending |
| `--dump` | Export WeChat's control tree to `wechat_tree.txt` for troubleshooting |
| `--log-level DEBUG` | Verbose logging (also written to `auto_reply.log`) |

`rules.json` can also be edited by hand (`rules.example.json` is a template in the same format):

```json
{
  "rules": [
    { "contact": "张三", "reply": "我在忙，稍后回复你" },
    { "contact": "文件传输助手", "reply": "已收到：{content}" }
  ]
}
```

---

## How It Works

The program drives the WeChat window through Windows **UI Automation** — the equivalent of a program clicking and typing for you. It does **not** crack any protocol, and does **not** inject code into WeChat:

1. Locate the main window `WeChatMainWndForPC` and read the conversation list on the left;
2. Double-click the target conversation, which makes WeChat open a separate chat window (`ChatWnd`);
3. Poll that window's message list and use each control's runtime ID to tell **new messages** apart;
4. On a rule match, write the reply to the clipboard → paste into the input box → press Enter.

A few deliberate design decisions (all of them learned the hard way — please read before changing them):

- **No history replay**: a baseline of existing messages is taken at startup, so only later arrivals trigger a reply.
- **One reply per message**: processed message IDs are remembered (up to 500 per conversation, oldest evicted), so a message that scrolls out of view and back is never answered twice.
- **Non-intrusive**: a separate chat window is used instead of the shared main window, so you can keep chatting with other people.
- **Clipboard preserved**: backed up before sending, restored afterwards.
- **Message direction is decided by avatar position**: incoming messages have the avatar on the left, your own on the right, and system notices / time separators have no avatar. Control *height* was deliberately avoided — it changes with system font scaling (DPI).
- **Conversation items must not be validated by coordinates**: WeChat reports a conversation whose separate window is open as `rect=(0,0,0,0)`, yet its content is still readable. An earlier version stopped iterating there, which silently dropped every conversation after it.

---

## Project Structure

| File | Purpose |
| --- | --- |
| `auto_reply.py` | Main program: interactive setup + listening loop |
| `wechat_uia.py` | UI Automation wrapper for the WeChat PC client (read/send messages) |
| `check_deps.py` | Dependency check — run it right after installing |
| `tests/` | Offline tests; no WeChat installation required |
| `rules.example.json` | Rules template; copy it to `rules.json` and edit |
| `CONTRIBUTING.md` | Contribution guide |
| `.github/` | Issue and PR templates |

`rules.json`, `auto_reply.log` and `wechat_tree.txt` are local artifacts and personal configuration; they are excluded by `.gitignore`.

---

## Development

### Setting up

```bat
git clone https://github.com/unicorn-hrrct/wechat-auto-reply.git
cd wechat-auto-reply
pip install -r requirements.txt
python check_deps.py
```

### Running tests

**WeChat is not required.** `tests/` simulates controls with `FakeControl` / `FakeWeChat`, covering message parsing, de-duplication, rule matching, template rendering and the listening loop:

```bat
python -m unittest discover -s tests -t .
```

Real sending/receiving must be verified on a machine with WeChat 3.9.x by running `python auto_reply.py`.

### Debugging

| Tool | Purpose |
| --- | --- |
| `python auto_reply.py --check` | Tell "dependency problem" and "UI problem" apart |
| `python auto_reply.py --dump` | Export the control tree to `wechat_tree.txt` to find UI differences |
| `python auto_reply.py --log-level DEBUG` | Write verbose logs to `auto_reply.log` |
| `python check_deps.py` | Verify that dependencies are in place |

When a WeChat update breaks control lookup, the tree exported by `--dump` is the key artifact — compare it against the `ClassName` and `Name` values used in `wechat_uia.py`.

### Code map

| What you want to change | Where to look |
| --- | --- |
| How WeChat windows are located, how messages are read/written | `wechat_uia.py` |
| Interactive flow, listening loop, rule matching | `auto_reply.py` |
| Command-line options | `parse_args()` in `auto_reply.py` |
| Existing conventions for parsing sessions and messages | `tests/test_auto_reply.py` (the tests double as behaviour docs) |

---

## Known Limitations & TODO

These are the current weak spots — and the **most welcome areas for contribution**:

| Item | Notes | Effort |
| --- | --- | --- |
| **WeChat 4.x support** | The window class names and control tree differ completely from 3.9.x; a second control-location implementation is needed. This is what **most people ask for** | ⭐⭐⭐ |
| Official-account entries can't be listened to | Aggregate entries like 「订阅号」 don't open a separate chat window, so `open_chat` fails | ⭐⭐ |
| Text-only sending | Auto-replying with images, files or voice is not supported | ⭐⭐ |
| AI-powered replies | Currently only fixed content; wiring up an LLM is a natural extension | ⭐⭐ |
| Long histories not fully tested | WeChat unloads old messages once a conversation grows large; the de-dup boundary in that case hasn't been verified on a real client | ⭐⭐ |
| Non-text messages aren't parsed | An incoming image is recorded as `[图片]`, with no further inspection | ⭐ |
| No per-member rules in groups | Rules match by group name only; "reply only when a specific member speaks" isn't possible | ⭐ |

If you have an idea, please open an [Issue](https://github.com/unicorn-hrrct/wechat-auto-reply/issues) first so we can talk it through.

---

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first — it covers environment setup, code conventions and a pre-submit checklist.

- **Reporting a problem**: include your WeChat version, reproduction steps and the error text; for UI-related problems please attach the `wechat_tree.txt` produced by `--dump`
- **Sending code**: fork → create a branch → change code and add tests → open a PR
- **One hard requirement**: new features must come with tests. Most pitfalls in this project live in the details of the real WeChat control tree, and without tests regressions are very hard to catch

---

## FAQ

**Q: "WeChat main window not found"**
Check, in order: is the client running, are you logged in and past the QR screen, and is the version 3.9.x? `--check` gives a more specific diagnosis.

**Q: My WeChat is 4.x — does it work?**
No. WeChat 4.x uses entirely different window classes and a different control tree; this project targets 3.9.x only. If you'd like to push 4.x support forward, please start a discussion in [Issues](https://github.com/unicorn-hrrct/wechat-auto-reply/issues).

**Q: "Conversation not found: XXX"**
The name must match exactly, including spaces and emoji. Use `--list` to copy the exact name.

**Q: What happens if WeChat restarts while listening?**
The program tries to reopen closed chat windows automatically. If WeChat exits completely, five consecutive read failures stop it and print statistics; restart WeChat and run it again.

**Q: Replies aren't being sent**
Check whether the WeChat main window is minimized (UI Automation cannot operate on a minimized window) and whether the chat window was closed. `--log-level DEBUG` records the specific cause in `auto_reply.log`.

**Q: Control lookup fails and the tree looks different**
Run `python auto_reply.py --dump` to export `wechat_tree.txt`, then compare its `ClassName` and `Name` values. Attaching that file to an Issue speeds up diagnosis a lot.

---

## Notes

- This program is intended for **personal automation** (for example, auto-answering while you're away). Please don't use it for bulk messaging or marketing.
- Automating WeChat carries some risk of triggering account controls. Keeping `--interval` at one second or above, and limiting reply frequency, is advisable.
- The program reads message text from chat windows to decide whether a reply is needed. This content is processed locally only and is never sent anywhere.

---

## License

[MIT](LICENSE)
