# 贡献指南

感谢你愿意参与这个项目。本文说明如何报告问题、提交改动，以及开发时需要遵守的约定。

## 目录

- [报告问题](#报告问题)
- [提交改动](#提交改动)
- [开发环境](#开发环境)
- [代码约定](#代码约定)
- [测试要求](#测试要求)
- [提交 PR 前自查](#提交-pr-前自查)

---

## 报告问题

请通过 [Issues](https://github.com/unicorn-hrrct/wechat-auto-reply/issues) 反馈，并尽量提供以下信息：

1. **微信 PC 客户端版本**（微信「设置 → 关于微信」里查看，本项目只支持 3.9.x）
2. **Python 版本**（`python --version`）
3. **完整的复现步骤**（配置了哪个联系人、做了什么操作）
4. **报错信息**，最好附上 `auto_reply.log` 的相关片段

如果问题是**界面定位失败**，请先跑一次 `python auto_reply.py --check`，它能区分「依赖问题」和「微信界面问题」；必要时再用 `python auto_reply.py --dump` 导出 `wechat_tree.txt` 并贴出来——那份控件树是定位此类问题的关键线索。

---

## 提交改动

1. Fork 本仓库并 clone 到本地
2. 从 `main` 开一个分支，分支名说明改动内容，例如 `fix/session-list-truncated`、`feat/wechat-4x-support`
3. 修改代码并补充测试
4. 提交（commit message 中英文皆可，说清「做了什么、为什么」）
5. 推送到你的 fork，然后在 GitHub 上发起 Pull Request

---

## 开发环境

```bat
git clone https://github.com/unicorn-hrrct/wechat-auto-reply.git
cd wechat-auto-reply
pip install -r requirements.txt
python check_deps.py                        :: 确认依赖就绪
python -m unittest discover -s tests -t .   :: 跑测试
```

好消息是**开发时不需要安装微信**：`tests/` 用假控件覆盖了消息解析、去重、规则匹配等纯逻辑，改完随时可以验证。

---

## 代码约定

- 遵循 PEP 8，使用类型注解
- **注释和文档字符串用中文**，与现有代码保持一致
- 面向使用者的输出（`print` 与错误提示）要给出**可操作的下一步**，而不只是报告失败
- 涉及微信控件定位的代码，请写清**为什么这样定位**。控件的类名、层级会随微信版本变化，注释是后续维护者的主要线索
- 新增对外行为时同步更新 README

---

## 测试要求

- 新增功能请附带测试；修复 bug 请补一个能复现该 bug 的用例
- 测试不要依赖真实微信，使用 `tests/test_auto_reply.py` 中已有的 `FakeControl` / `FakeWeChat` 模拟
- 提交前请确认全部通过：

```bat
python -m unittest discover -s tests -t .
```

---

## 提交 PR 前自查

- [ ] `python -m unittest discover -s tests -t .` 全部通过
- [ ] 没有误提交 `rules.json`（含个人配置）、`auto_reply.log`、`wechat_tree.txt` 等本地文件
- [ ] 改动只针对本次要解决的问题，没有夹带无关修改
- [ ] 改变了用户可见行为时，README 已同步更新

---

## 特别欢迎的方向

如果你在找一个合适的切入点，README 的「[已知局限与待办](README.md#已知局限与待办)」列出了当前最需要帮助的几件事——尤其是**微信 4.x 的适配**，那是最多人需要、也最有价值的一块。
