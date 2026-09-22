#!/usr/bin/env python3
"""
一键同步脚本：自动将本地修改推送到 GitHub (Mrmimee/hermes-plugin-jev)，
并保证 Hermes 插件目录处于最新状态。
"""

import subprocess
import sys
from pathlib import Path


def run(cmd_list, cwd=None):
    """执行命令，返回 returncode；失败时打印 stderr。"""
    res = subprocess.run(cmd_list, cwd=cwd, text=True, capture_output=True)
    if res.stdout.strip():
        print(res.stdout.strip())
    if res.returncode != 0:
        if res.stderr.strip():
            print(f"Error: {res.stderr.strip()}", file=sys.stderr)
    return res.returncode


def main():
    root = Path(__file__).resolve().parent
    commit_msg = sys.argv[1] if len(sys.argv) > 1 else "Auto sync plugin update"

    print("🚀 正在提交并推送到 GitHub...")

    # 1. 暂存所有改动
    if run(["git", "add", "."], cwd=root) != 0:
        sys.exit("❌ git add 失败")

    # 2. 提交（无改动时 git commit 报 nothing to commit，属正常，继续推送已有提交）
    commit_code = run(["git", "commit", "-m", commit_msg], cwd=root)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    if commit_code != 0 and not status.stdout.strip():
        print("ℹ️  无新改动（可能已提交），直接推送已有提交")
    elif commit_code != 0:
        sys.exit(f"❌ git commit 失败（exit {commit_code}）")

    # 3. 推送
    code = run(["git", "push", "origin", "main"], cwd=root)
    if code == 0:
        print("✅ GitHub 仓库已同步更新：https://github.com/Mrmimee/hermes-plugin-jev")
    else:
        print(f"⚠️  推送到 GitHub 失败（exit {code}），请检查网络或认证配置。", file=sys.stderr)
        sys.exit(code)

    # 4. 检查 Hermes 插件路径
    hermes_plugin = Path.home() / "AppData" / "Local" / "hermes" / "plugins" / "jev"
    if hermes_plugin.exists():
        print(f"🔗 Hermes 本地插件挂载就绪: {hermes_plugin}")
    else:
        print(f"ℹ️  Hermes 插件路径不存在: {hermes_plugin}（可能尚未安装）")


if __name__ == "__main__":
    main()
