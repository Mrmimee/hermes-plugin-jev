"""Jev/TypeSafe AI 决策小脑 Hermes 插件。

在 Hermes 注册 `jev` 工具集，允许模型在毫秒级做出"单选 (Choice)"、"打分 (Score)"、"是非率 (Noul)"决策。
支持单题/多题并发打满（1 次请求裁决多个问题），具备双层持久化磁盘缓存、8 秒硬超时熔断与黑匣子审计日志。
底层基于 `system_one_adapter` 接入用户的 Agnes 3.0 Flash。
零常驻显存，零后台进程，极低内存消耗。
"""
from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
_DEFAULT_TIMEOUT_SECONDS = 8.0


# 1. 缓存路径与黑匣子日志路径
def _get_cache_dir() -> Path:
    """获取 Hermes 缓存目录（优先 AppData/Local/hermes/cache，兼容 ~/.hermes/cache）"""
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        p = Path(hermes_home) / "cache"
        if p.parent.exists():
            p.mkdir(parents=True, exist_ok=True)
            return p
    app_cache = Path.home() / "AppData" / "Local" / "hermes" / "cache"
    if app_cache.parent.exists():
        app_cache.mkdir(parents=True, exist_ok=True)
        return app_cache
    fallback = Path.home() / ".hermes" / "cache"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


_CACHE_FILE = _get_cache_dir() / "jev_cache.json"
_JOURNAL_FILE = _get_cache_dir() / "jev_journal.jsonl"

# LRU 决策缓存（上限 256 条，默认 3600 秒有效期）
_CACHE_MAX_SIZE = 256
_CACHE_TTL_SECONDS = 3600
_DECISION_CACHE: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()


def _load_disk_cache() -> None:
    """启动时从磁盘恢复决策缓存，跨会话/重启 0ms 秒命中"""
    if not _CACHE_FILE.exists():
        return
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        now = time.time()
        for k, item in data.items():
            if isinstance(item, dict) and "created_at" in item and "result" in item:
                created_at = item["created_at"]
                if now - created_at <= _CACHE_TTL_SECONDS:
                    _DECISION_CACHE[k] = (created_at, item["result"])
    except Exception:
        pass


def _save_disk_cache() -> None:
    """轻量写盘，持久化缓存"""
    try:
        payload = {}
        for k, (created_at, res) in _DECISION_CACHE.items():
            payload[k] = {"created_at": created_at, "result": res}
        _CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# 初始化加载磁盘缓存
_load_disk_cache()


def _get_cache_key(args: Dict[str, Any]) -> str:
    """计算状态与问题的确定性 SHA256 指纹"""
    try:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=True)
    except Exception:
        raw = str(args)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict | None:
    """获取缓存并执行 LRU 提升与 TTL 过期检查"""
    if key not in _DECISION_CACHE:
        return None
    created_at, result = _DECISION_CACHE[key]
    if time.time() - created_at > _CACHE_TTL_SECONDS:
        del _DECISION_CACHE[key]
        return None
    _DECISION_CACHE.move_to_end(key)
    res_copy = dict(result)
    res_copy["cache_hit"] = True
    res_copy["latency_seconds"] = 0.0
    return res_copy


def _cache_put(key: str, result: dict) -> None:
    """写入缓存，超出容量时淘汰最老条目并异步持久化"""
    if len(_DECISION_CACHE) >= _CACHE_MAX_SIZE:
        _DECISION_CACHE.popitem(last=False)
    _DECISION_CACHE[key] = (time.time(), result)
    _save_disk_cache()


def _log_journal(entry: Dict[str, Any]) -> None:
    """记录判定黑匣子流水（审计日志），故障追踪与吞吐量复盘"""
    try:
        record = dict(entry)
        record["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _local_guard_verdict(args: Dict[str, Any]) -> Dict[str, Any]:
    """离线降级动作闸门（advisory 档）：无 key / adapter 缺失 / 超时熔断时用本地危险模式做保守判定。

    默认拒绝高危动词/命令（fail closed），普通文本放行。
    结果带 advisory=True，调用方必须把 advisory 判定的放行当建议而非许可。
    """
    state = args.get("state") or ""
    if isinstance(state, dict):
        state = json.dumps(state, ensure_ascii=False)
    low = str(state).lower()

    _HARD_DENY = (
        "rm -rf", "del /f /q", "format ", "diskpart", "shutdown", "reboot",
        "drop database", "drop table", "truncate table", "mkfs",
        "rd /s", "shutdown /s",
    )
    _ASK = (
        "delete", "remove", "overwrite", "replace", "reset", "reinstall",
        "uninstall", "drop", "truncate", "restart", "kill", "push",
        "git reset", "git checkout --", "清空", "删除", "覆盖", "格式化", "重置",
    )
    hard_hit = next((p for p in _HARD_DENY if p in low), None)
    soft_hit = next((p for p in _ASK if p in low), None)

    if hard_hit:
        verdict = {"verdict": "deny", "reason": f"命中高危破坏性模式: {hard_hit!r}（本地离线判定，advisory）"}
    elif soft_hit:
        verdict = {"verdict": "ask", "reason": f"命中破坏性关键词: {soft_hit!r}（本地离线判定，advisory，建议人工确认）"}
    else:
        verdict = {"verdict": "allow", "reason": "未命中破坏性模式（本地离线判定，advisory，未做在线裁决）"}
    verdict["advisory"] = True
    verdict["latency_seconds"] = 0.0
    verdict["cache_hit"] = False
    return verdict


def _get_agnes_key() -> str:
    """提取用户的 AGNES_API_KEY"""
    key = os.environ.get("AGNES_API_KEY")
    if key:
        return key
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


# 2. 工具定义 Schema
JEV_SCHEMA = {
    "name": "jev",
    "description": (
        "Jev 决策小脑（Agnes 3.0 Flash 极速结构化裁决）："
        "把当前的 Agent 状态丢进去，一次性并行判定多个问题——"
        "Choice 决定路由、Score 评估打分、Noul 判断是非风险。"
        "支持单题与多题并发打满（nouls/choices/scores 数组），"
        "内置双层持久化缓存、8秒超时熔断及审计流水。耗时约 1~2 秒（缓存命中 0ms），不占本地显存。"
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
                "description": "单项选择题（单题）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "单选意图描述"},
                    "criteria": {
                        "type": "object",
                        "description": "选项及其含义，格式为 {'选项key': '该选项的描述'}",
                    },
                },
            },
            "choices": {
                "type": "array",
                "description": "多个单选题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "单选意图描述"},
                        "criteria": {"type": "object", "description": "选项键值对"},
                    },
                    "required": ["criteria"],
                },
            },
            "noul": {
                "type": "object",
                "description": "是非题（单题，布尔概率 0.0~1.0）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "判断意图描述"},
                },
            },
            "nouls": {
                "type": "array",
                "description": "多个是非题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "判断意图描述"},
                    },
                    "required": ["instructions"],
                },
            },
            "score": {
                "type": "object",
                "description": "打分题（单题，离散评分梯度）。",
                "properties": {
                    "name": {"type": "string", "description": "本题名称"},
                    "instructions": {"type": "string", "description": "打分意图描述"},
                    "criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "评分梯度数组，如 ['低', '中', '高']",
                    },
                },
            },
            "scores": {
                "type": "array",
                "description": "多个打分题组（并发裁决）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "题目名称（唯一标识）"},
                        "instructions": {"type": "string", "description": "打分意图描述"},
                        "criteria": {"type": "array", "items": {"type": "string"}, "description": "评分梯度数组"},
                    },
                    "required": ["criteria"],
                },
            },
            "timeout_seconds": {
                "type": "number",
                "description": "网络裁决超时熔断阈值（秒，默认 8.0 秒）。超时自动降级为 advisory 模式。",
            },
            "no_cache": {
                "type": "boolean",
                "description": "是否跳过本地缓存强制实时推理（默认 false）。",
            },
        },
        "required": ["state"],
    },
}

# guard 动作闸门 Schema：执行前对动作做 allow/ask/deny 三档裁决
JEV_GUARD_SCHEMA = {
    "name": "jev_guard",
    "description": (
        "Jev 动作闸门（guard）：执行动作前做 allow/ask/deny 二次判定，"
        "拦截高危破坏性操作。在线走 Jev 概率裁决，"
        "无 key、超时熔断或网络失败时自动降级为本地离线规则（结果标 advisory=true，fail closed）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": ["object", "string"],
                "description": "待执行动作的完整描述或上下文（命令、操作、目标对象）。",
            },
            "guard": {
                "type": "object",
                "description": "闸门配置（可选）。",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "判定意图描述，默认：该动作是否可安全执行。",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "放行阈值（0~1，默认 0.75）。概率低于阈值判 ask。",
                    },
                },
            },
            "timeout_seconds": {
                "type": "number",
                "description": "网络裁决超时熔断阈值（秒，默认 8.0 秒）。超时自动降级为本地离线 fail-closed 规则。",
            },
            "no_cache": {
                "type": "boolean",
                "description": "是否跳过本地缓存强制实时推理（默认 false）。",
            },
        },
        "required": ["state"],
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


def _execute_system_one_with_timeout(
    client: Any,
    state: Any,
    questions: Dict[str, Any],
    provider: Any,
    timeout: float,
) -> Any:
    """带硬超时保护的 System One 执行包装器"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        fut = executor.submit(
            client.system_one,
            state=state,
            questions=questions,
            model=provider,
        )
        return fut.result(timeout=timeout)


def _handle_jev(args: Dict[str, Any], **kwargs) -> str:
    """处理 Jev 决策工具调用（支持单题、多题并发打满、超时熔断、磁盘持久化与日志）"""
    state = args.get("state")
    if state is None:
        return json.dumps({"error": "缺少 state（系统当前状态）参数。"}, ensure_ascii=False)

    timeout = float(args.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)

    # 优先查本地高速缓存（跨会话磁盘持久化）
    use_cache = not args.get("no_cache", False)
    cache_key = _get_cache_key(args)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": "jev", "cache_hit": True, "latency_seconds": 0.0, "state_preview": str(state)[:100]})
            return json.dumps(hit, ensure_ascii=False, indent=2)

    # 离线降级：无 key / adapter 缺失 → 返回 advisory 提示而非报错
    if not _jev_tool_available():
        result = {
            "advisory": True,
            "reason": "Jev 在线裁决不可用（缺少 AGNES_API_KEY 或 system_one_adapter 未安装），本次判定结果仅为提示性。",
            "cache_hit": False,
            "latency_seconds": 0.0,
        }
        if use_cache:
            _cache_put(cache_key, result)
        _log_journal({"tool": "jev", "advisory": True, "reason": "offline", "state_preview": str(state)[:100]})
        return json.dumps(result, ensure_ascii=False, indent=2)

    client = SystemOneAdapterClient(
        structured_outputs=False,
        llm_answer_mode="discrete",
    )
    provider = _build_provider()

    # 静态分析防护：若 import 失败，则 _adapter_available=False，提前报错而非引用未定义符号
    if not _adapter_available:
        return json.dumps({"error": "system_one_adapter 未安装，无法进行在线裁决。"}, ensure_ascii=False)

    questions = {}

    # 1. 单选解析（单题 + 多题数组）
    choice_cfg = args.get("choice")
    if choice_cfg and choice_cfg.get("criteria"):
        q_name = choice_cfg.get("name", "choice_q")
        questions[q_name] = Choice(
            instructions=choice_cfg.get("instructions", "下一步做什么？"),
            criteria=choice_cfg["criteria"],
        )

    for i, c in enumerate(args.get("choices") or []):
        if isinstance(c, dict) and c.get("criteria"):
            q_name = c.get("name") or f"choice_{i+1}"
            questions[q_name] = Choice(
                instructions=c.get("instructions", "选择最合适的一项"),
                criteria=c["criteria"],
            )

    # 2. 是非解析（单题 + 多题数组）
    noul_cfg = args.get("noul")
    if noul_cfg:
        q_name = noul_cfg.get("name", "noul_q")
        questions[q_name] = Noul(
            instructions=noul_cfg.get("instructions", "是否发生？")
        )

    for i, n in enumerate(args.get("nouls") or []):
        if isinstance(n, dict) and n.get("instructions"):
            q_name = n.get("name") or f"noul_{i+1}"
            questions[q_name] = Noul(
                instructions=n["instructions"]
            )

    # 3. 打分解析（单题 + 多题数组）
    score_cfg = args.get("score")
    if score_cfg and score_cfg.get("criteria"):
        q_name = score_cfg.get("name", "score_q")
        questions[q_name] = Score(
            instructions=score_cfg.get("instructions", "评估打分"),
            criteria=score_cfg["criteria"],
        )

    for i, s in enumerate(args.get("scores") or []):
        if isinstance(s, dict) and s.get("criteria"):
            q_name = s.get("name") or f"score_{i+1}"
            questions[q_name] = Score(
                instructions=s.get("instructions", "评估打分"),
                criteria=s["criteria"],
            )

    if not questions:
        return json.dumps(
            {
                "error": "没有提供任何判定题目，请提供 choice/choices、noul/nouls 或 score/scores 之一。",
                "tip": "支持多题并发传入，一次网络往返即可返回所有题目答案。",
            },
            ensure_ascii=False,
        )

    try:
        t0 = time.time()
        response = _execute_system_one_with_timeout(
            client=client,
            state=state,
            questions=questions,
            provider=provider,
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 2)

        result: Dict[str, Any] = {
            "latency_seconds": elapsed,
            "cache_hit": False,
            "advisory": False,
            "answers": {},
        }

        # 聚合全部题目结果
        for q_name, c_val in response.choices.items():
            result["answers"][q_name] = c_val.choice
        for q_name, n_val in response.nouls.items():
            result["answers"][q_name] = n_val.noul
        for q_name, s_val in response.scores.items():
            result["answers"][q_name] = s_val.score

        # 单题兼容别名
        if choice_cfg:
            c_name = choice_cfg.get("name", "choice_q")
            if c_name in result["answers"]:
                result["choice_answer"] = result["answers"][c_name]

        if noul_cfg:
            n_name = noul_cfg.get("name", "noul_q")
            if n_name in result["answers"]:
                result["noul_answer"] = result["answers"][n_name]

        if score_cfg:
            s_name = score_cfg.get("name", "score_q")
            if s_name in result["answers"]:
                result["score_answer"] = result["answers"][s_name]

        if use_cache:
            _cache_put(cache_key, result)

        _log_journal({
            "tool": "jev",
            "latency_seconds": elapsed,
            "questions_count": len(questions),
            "answers": result["answers"],
            "state_preview": str(state)[:100],
        })
        return json.dumps(result, ensure_ascii=False, indent=2)

    except concurrent.futures.TimeoutError:
        # 硬超时熔断：降级为 advisory 模式，主流程绝不卡死
        fallback = {
            "advisory": True,
            "reason": f"Jev 在线裁决超时 (>{timeout:.1f}s)，已触发硬超时熔断，降级为本地 advisory 提示。",
            "cache_hit": False,
            "latency_seconds": timeout,
        }
        _log_journal({"tool": "jev", "timeout": True, "state_preview": str(state)[:100]})
        return json.dumps(fallback, ensure_ascii=False, indent=2)

    except Exception as e:
        # 在线调用异常：降级 advisory 而非直接报错
        fallback = {
            "advisory": True,
            "reason": f"在线裁决失败，已降级为本地提示: {type(e).__name__}: {e}",
            "cache_hit": False,
            "latency_seconds": 0.0,
        }
        _log_journal({"tool": "jev", "error": str(e), "state_preview": str(state)[:100]})
        return json.dumps(fallback, ensure_ascii=False, indent=2)


def _handle_jev_guard(args: Dict[str, Any], **kwargs) -> str:
    """处理 guard 动作闸门调用：在线走 Jev 三档裁决，超时或离线降级本地规则（fail-closed advisory）。"""
    state = args.get("state")
    if state is None:
        return json.dumps(
            {"error": "缺少 state（待执行动作描述）参数。"},
            ensure_ascii=False,
        )

    timeout = float(args.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)

    use_cache = not args.get("no_cache", False)
    cache_key = _get_cache_key(args)
    if use_cache:
        hit = _cache_get(cache_key)
        if hit is not None:
            _log_journal({"tool": "jev_guard", "cache_hit": True, "latency_seconds": 0.0, "state_preview": str(state)[:100]})
            return json.dumps(hit, ensure_ascii=False, indent=2)

    # 离线降级：无 key / adapter 缺失 → 本地 fail-closed 规则
    if not _jev_tool_available():
        verdict = _local_guard_verdict(args)
        if use_cache:
            _cache_put(cache_key, verdict)
        _log_journal({"tool": "jev_guard", "advisory": True, "verdict": verdict["verdict"], "reason": "offline", "state_preview": str(state)[:100]})
        return json.dumps(verdict, ensure_ascii=False, indent=2)

    guard_cfg = args.get("guard") or {}
    question = Noul(
        instructions=guard_cfg.get(
            "instructions",
            "这个动作是否可以安全执行？true 表示安全可放行。",
        )
    )
    threshold = float(guard_cfg.get("threshold", 0.75))

    try:
        t0 = time.time()
        client = SystemOneAdapterClient(
            structured_outputs=False,
            llm_answer_mode="discrete",
        )
        response = _execute_system_one_with_timeout(
            client=client,
            state=state,
            questions={"guard_q": question},
            provider=_build_provider(),
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 2)
        prob = response.nouls["guard_q"].noul
        verdict = "allow" if prob >= threshold else "ask"
        result = {
            "verdict": verdict,
            "probability": prob,
            "threshold": threshold,
            "advisory": False,
            "latency_seconds": elapsed,
            "cache_hit": False,
        }
        if use_cache:
            _cache_put(cache_key, result)

        _log_journal({
            "tool": "jev_guard",
            "verdict": verdict,
            "probability": prob,
            "latency_seconds": elapsed,
            "state_preview": str(state)[:100],
        })
        return json.dumps(result, ensure_ascii=False, indent=2)

    except concurrent.futures.TimeoutError:
        # 硬超时熔断：降级本地规则，保持 fail-closed
        fallback = _local_guard_verdict(args)
        fallback["reason"] = f"在线裁决超时 (>{timeout:.1f}s)，已熔断降级本地规则（advisory，保持 fail-closed）"
        _log_journal({"tool": "jev_guard", "timeout": True, "verdict": fallback["verdict"], "state_preview": str(state)[:100]})
        return json.dumps(fallback, ensure_ascii=False, indent=2)

    except Exception as e:
        # 在线调用失败 → 降级 advisory 而非直接报错，闸门保持 fail closed
        fallback = _local_guard_verdict(args)
        fallback["error"] = f"在线裁决失败，已降级本地 advisory 规则: {type(e).__name__}: {e}"
        _log_journal({"tool": "jev_guard", "error": str(e), "verdict": fallback["verdict"], "state_preview": str(state)[:100]})
        return json.dumps(fallback, ensure_ascii=False, indent=2)


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
    ctx.register_tool(
        name="jev_guard",
        toolset="jev",
        schema=JEV_GUARD_SCHEMA,
        handler=_handle_jev_guard,
        check_fn=None,  # 永远可用：在线走 Jev，离线走本地 advisory 规则
        emoji="🛡️",
    )
