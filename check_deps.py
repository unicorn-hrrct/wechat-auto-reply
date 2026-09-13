"""依赖自检：确认运行本程序所需的 Python 包是否齐备。

微信改版或依赖升级后若程序报错，先跑一次这个脚本，可以快速区分
「依赖问题」和「微信界面定位问题」。

用法：
    python check_deps.py
"""

import platform
import sys

REQUIRED = {
    "uiautomation": "驱动微信窗口的 UI Automation 封装",
    "pyperclip": "写入剪贴板（发送中文最可靠的方式）",
}


def main() -> int:
    print("Python :", platform.python_implementation(), platform.python_version())
    print("系统   :", platform.system(), platform.machine())
    print("解释器 :", sys.executable)
    print()

    if platform.system() != "Windows":
        print("[!] 本程序只支持 Windows。")
        return 2

    if sys.version_info < (3, 9):
        print(f"[!] Python 版本过低（{platform.python_version()}），需要 3.9 及以上。")
        return 2

    missing = []
    for name, purpose in REQUIRED.items():
        try:
            module = __import__(name)
        except ImportError:
            missing.append(name)
            print(f"[X] {name:14s} 未安装 —— {purpose}")
        else:
            version = getattr(module, "VERSION", None) or getattr(module, "__version__", "已安装")
            print(f"[OK] {name:14s} {version}")

    if missing:
        print("\n缺少依赖，请运行：")
        print("    pip install -r requirements.txt")
        return 1

    print("\n依赖齐备。接着确认微信客户端：")
    print("    python auto_reply.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
