"""Jev/TypeSafe AI 决策小脑 Hermes 插件。

在 Hermes 注册 `jev` 工具集，允许模型在毫秒级做出"单选 (Choice)"、"打分 (Score)"、"是非率 (Noul)"决策。
底层基于 `system_one_adapter` 接入用户的 Agnes 3.0 Flash。
零常驻内存，不占本机显存，仅在调用时发起一次 HTTP 请求。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

# 0. 引入 jev-starter 的 venv site-packages，保证运行独立性（兼容本机部署与跨机器克隆安装）
_ADAPTER_SITE_PACKAGES = os.path.join(
    os.path.expanduser("~"), "jev-starter", ".venv", "Lib", "site-packages"
)
if os.path.isdir(_ADAPTER_SITE_PACKAGES) and _ADAPTER_SITE_PACKAGES not in sys.path:
    sys.path.append(_ADAPTER_SITE_PACKAGES)

_adapter_available = True
try:
    from system_one_adapter import SystemOneAdapterClient, Choice, Noul, Score
    from system_one_adapter.providers.openai import OpenAIProvider
except Exception:
    _adapter_available = False

_BASE_URL = "https://apihub.agnes-ai.com/v1"
_MODEL_NAME = "agnes-3.0-flash"


def _get_agnes_key() -> str:
    """提取用户的 AGNES_API_KEY"""
    key = os.environ.get("AGNES_API_KEY")
    if key:
        return key
    # 备用抓取路径：桌面 apikey.txt
    key_file = Path.home() / "OneDrive" / "桌面" / "apikey.txt"
    if key_file.exists():
        lines = key_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "AgnesAi" in line and i + 1 < len(lines):
                return lines[i + 1].strip()
    return ""


def _jev_tool_available() -> bool:
    """判断 Jev 小脑是否可用（有 key 且 adapter 装好）"""
    if not _adapter_available:
        return False
    return bool(_get_agnes_key())


# 1. 工具定义 Schema
JEV_SCHEMA = {
    "name": "jev",
    "description": (
        "Jev 决策小脑（Agnes 3.0 Flash 极速结构化裁决）："
        "把当前的 Agent 状态丢进去，一次性并行判定多个问题——"
        "Choice 决定路由、Score 评估打分、Noul 判断是非风险。"
        "耗时约 1 秒，不吃本地硬件。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "系统当前的状态数据，可以是一个 JSON 字典或一段描述文本。",
            },
            "choice": {
                "type": "object",
                "description": "需要做的单项选择题。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "单选意图描述"},
                    "criteria": {
                        "type": "object",
                        "description": "选项及其含义，格式为 {'选项key': '该选项的描述'}"
                    }
                }
            },
            "noul": {
                "type": "object",
                "description": "需要做的是非题（布尔概率率 0.0~1.0）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "判断意图描述"}
                }
            },
            "score": {
                "type": "object",
                "description": "需要做的打分题（打分梯度）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "打分意图描述"},
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "评分梯度数组，如 ['低', '中', '高']"
                    }
                }
            },
        },
    },
}


def _build_provider() -> OpenAIProvider:
    """快速构建指向 Agnes 云端的 Provider"""
    key = _get_agnes_key()
    return OpenAIProvider(
        _MODEL_NAME,
        base_url=_BASE_URL,
        api_key=key,
        api="chat_completions",
    )


def _handle_jev(args: Dict[str, Any], **kwargs) -> str:
    """处理 Jev 决策工具调用"""
    state = args.get("state")
    if state is None:
        return json.dumps({"error": "缺少 state（系统当前状态）参数。"}, ensure_ascii=False)

    client = SystemOneAdapterClient(
        structured_outputs=False,
        llm_answer_mode="discrete"
    )
    provider = _build_provider()

    questions = {}

    choice_cfg = args.get("choice")
    if choice_cfg and choice_cfg.get("criteria"):
        questions[choice_cfg.get("name", "choice_q")] = Choice(
            instructions=choice_cfg.get("instructions", "下一步做什么？"),
            criteria=choice_cfg["criteria"]
        )

    noul_cfg = args.get("noul")
    if noul_cfg:
        questions[noul_cfg.get("name", "noul_q")] = Noul(
            instructions=noul_cfg.get("instructions", "是否发生？")
        )

    score_cfg = args.get("score")
    if score_cfg and score_cfg.get("criteria"):
        questions[score_cfg.get("name", "score_q")] = Score(
            instructions=score_cfg.get("instructions", "评估打分"),
            criteria=score_cfg["criteria"]
        )

    if not questions:
        return json.dumps({
            "error": "没有提供任何判定题目，请至少提供 choice、noul 或 score 之一。",
            "tip": "使用 criteria 字典列出选项或评分梯度。",
        }, ensure_ascii=False)

    try:
        t0 = time.time()
        response = client.system_one(
            state=state,
            questions=questions,
            model=provider,
        )
        elapsed = round(time.time() - t0, 2)

        result = {"latency_seconds": elapsed}

        if choice_cfg and choice_cfg.get("name", "choice_q") in response.choices:
            q_name = choice_cfg.get("name", "choice_q")
            result["choice_answer"] = response.choices[q_name].choice

        if noul_cfg and noul_cfg.get("name", "noul_q") in response.nouls:
            q_name = noul_cfg.get("name", "noul_q")
            result["noul_answer"] = response.nouls[q_name].noul

        if score_cfg and score_cfg.get("name", "score_q") in response.scores:
            q_name = score_cfg.get("name", "score_q")
            result["score_answer"] = response.scores[q_name].score

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": f"Jev 调用失败: {type(e).__name__}: {e}"}, ensure_ascii=False)


def register(ctx) -> None:
    """注册 Jev 工具"""
    ctx.register_tool(
        name="jev",
        toolset="jev",
        schema=JEV_SCHEMA,
        handler=_handle_jev,
        check_fn=_jev_tool_available,
        emoji="🧠",
    )
