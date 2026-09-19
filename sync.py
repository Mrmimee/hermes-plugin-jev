#!/usr/bin/env python3
"""
一键同步脚本：自动将本地修改推送到 GitHub (Mrmimee/hermes-plugin-jev)，
并保证 Hermes 插件目录处于最新状态。
"""

import subprocess
import sys
from pathlib import Path


def run(cmd_list, cwd=None):
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
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", commit_msg], cwd=root)
    code = run(["git", "push", "origin", "main"], cwd=root)

    if code == 0:
        print("✅ GitHub 仓库已同步更新：https://github.com/Mrmimee/hermes-plugin-jev")
    else:
        print("⚠️ 推送到 GitHub 失败，请检查网络或认证配置。")

    # 检查 Hermes 插件路径
    hermes_plugin = Path.home() / "AppData" / "Local" / "hermes" / "plugins" / "jev"
    if hermes_plugin.exists():
        print(f"🔗 Hermes 本地插件挂载就绪: {hermes_plugin}")


if __name__ == "__main__":
    main()
