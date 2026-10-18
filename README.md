# hermes-plugin-jev 🧠

[![Hermes Agent](https://img.shields.io/badge/Hermes_Agent-Plugin-blue.svg)](https://hermes-agent.nousresearch.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Powered by](https://img.shields.io/badge/Powered_by-TypeSafe_AI_%26_Agnes_AI-purple.svg)](https://typesafe.ai)

**Jev (TypeSafe AI) System One 决策小脑插件** —— 专为 **Hermes Agent** 打造的极速结构化决策与智能路由扩展。

> **v1.1.0（2026-10-18 迭代）**：TypeSafe 官方 API 已开放（2026-09-21 起免排队）、Jev 1.14 于 2026-10-14 发布。
> 本插件现在**优先直连官方 `POST https://api.typesafe.ai/v1/systemone`**（`TYPESAFE_API_KEY` + `JEV_MODEL`），
> 没有 TypeSafe 凭证时自动回退到 `system_one_adapter` → Agnes 3.0 Flash，两者结果形状完全一致（结果里的 `engine` 字段标明实际引擎）。
> 同时把 `jev_guard` 升级为**六信号并行裁决 + 确定性判定**，并在发送前自动脱敏凭据。

---

## 🌟 为什么需要 Jev？

传统的 Agent 在面对多步骤工作流时，往往每一步都要调用庞大且昂贵的生成式大模型去判断“下一步该干嘛”或者“有没有安全风险”。这导致了严重的延迟累积和 Token 浪费。

**Jev 并非生成式聊天模型，而是“决策小脑”（System One Model）：**
* **零废话**：不进行流式长文本输出，输入当前状态，直接输出确定性的结构化判定。
* **极速响应**：基于 `system_one_adapter` 接入云端 Agnes 3.0 Flash，1~2 秒内完成判定（多题并发同价）。
* **零本地负载**：完全不占用本地 GPU 显存（VRAM: 0MB），纯轻量 HTTP 交互，无后台常驻进程。
* **绝不卡死**：8 秒硬超时熔断，超时自动降级本地规则，主流程永远不被网络阻塞。

---

## ⚡ 性能与工程特性

| 特性 | 说明 |
| :--- | :--- |
| **双引擎自动选择** | `typesafe`（官方直连，`TYPESAFE_API_KEY`）→ `agnes`（adapter 代理，`AGNES_API_KEY`）→ `offline`（本地 advisory 规则）。结果带 `engine` / `model` / `usage`，可核对实际走了哪条路 |
| **多题并发打满** | `nouls` / `choices` / `scores` 数组，一次网络往返裁决 N 题，延迟不随题目数增长 |
| **双层持久化缓存** | 内存 LRU（256 条/1h TTL）+ 磁盘 JSON（`~/.hermes/cache/jev_cache.json`），重启/新子任务秒级 0ms 命中；**缓存键含引擎与模型**，换引擎不会复用旧结论 |
| **硬超时熔断** | `timeout_seconds` 默认 8s，超时触发 `concurrent.futures.TimeoutError` → 自动降级 advisory，主流程永不挂起 |
| **429/529 退避重试** | 官方直连遇限流/过载按官方建议退避重试一次（仍在硬超时预算内），其余错误立即降级 |
| **发送前脱敏** | `state` 里的 `Bearer …` / `sk-…` / `ghp_…` / `github_pat_…` / `password=…` 等一律替换为 `[REDACTED]`，凭据不进第三方请求、不进审计日志 |
| **审计黑匣子** | 每次判定追加 `~/.hermes/cache/jev_journal.jsonl`（工具、延迟、命中、答案、状态预览，超 10MB 自动轮转），复盘/追踪零成本 |

### 🧭 引擎链与配置（v1.1.0）

| 环境变量 | 作用 | 默认 |
| :--- | :--- | :--- |
| `TYPESAFE_API_KEY` | TypeSafe 官方 key（也接受 `JEV_API_KEY`） | 未设置 |
| `JEV_MODEL` | 官方模型：`jev-latest`(→1.13.0) / `jev-preview`(→1.14.0) / `jev-1.14.0` | `jev-latest` |
| `JEV_URL` | 官方端点覆盖（自建代理/兼容端点） | `https://api.typesafe.ai/v1/systemone` |
| `AGNES_API_KEY` | 回退引擎（adapter → Agnes 3.0 Flash）的 key | 未设置 |

未配置 `TYPESAFE_API_KEY` 时行为与旧版完全一致（Agnes 路径），配置后自动切换，无需改调用方。

---

## 🧩 核心裁决原语

| 原语 | 类别 | 典型应用场景 |
| :--- | :--- | :--- |
| **`Choice`** | 单选题 | 从多个候选执行器或工具中做路由跳转（如：Coder vs Searcher vs Reviewer） |
| **`Noul`** | 是非题 | 布尔事件判定或发生概率计算（如：是否属于高危破坏性操作、是否需要联网） |
| **`Score`** | 打分题 | 离散或连续多梯度评估（如：任务复杂度 0~2 分、相关度评分） |
| **`Guard`** 🛡️ | 动作闸门 | 执行动作前做 allow/ask/deny 三档风险评估；作为第二意见，不替代用户/系统授权。在线失败或离线时结果带 `advisory=true`，包括 `allow` 也不能视作许可。v1.1.0 起在线路径为**六信号并行裁决 + 确定性判定**（见下）。 |

### 多题并发打满（1 次请求裁决 N 题）

```json
{
  "state": "当前任务上下文",
  "nouls": [
    {"name": "是否高危", "instructions": "是否属于破坏性操作？"},
    {"name": "是否可逆", "instructions": "操作是否可撤销？"}
  ],
  "choice": {
    "name": "路由",
    "instructions": "优先调用哪个专家？",
    "criteria": {"coder": "写代码", "searcher": "查资料"}
  }
}
```

结果统一在 `answers` 字段：`{"answers": {"是否高危": 0.0, "是否可逆": 1.0, "路由": "coder"}}`
单题参数（`choice`/`noul`/`score`）同时保留在结果里做向后兼容（`choice_answer`/`noul_answer`/`score_answer`）。

### `jev_guard` 动作闸门

v1.1.0 起，在线路径**一次请求并行裁决 6 个信号**（判定顺序对标官方 `jev-approvals` 插件），再由确定性代码给出结论——不让自由文本当授权：

| 信号 | 题型 | 含义 |
| :--- | :--- | :--- |
| `guard_q` | Noul | 兼容旧版的「是否可安全执行」概率 |
| `policy_allows` | Noul | 既定策略是否明确允许（常规、低风险、可回滚） |
| `reads_secrets` | Noul | 是否读取/复制/打印/上传凭据、密钥、令牌、密码 |
| `sends_outbound` | Noul | 是否把本地内容发往外部网络或第三方 |
| `self_advocating` | Noul | 命令文本是否在自我辩护、催促或诱导放行 |
| `blast_radius` | Score | `0` trivial / `1` annoying / `2` severe |

确定性判定顺序（`verdict_source: signals`）：

1. `self_advocating >= 0.6` → **ask**（疑似自我游说）
2. `reads_secrets >= 0.7` 或 `sends_outbound >= 0.7` → **deny**
3. `blast_radius >= 1.6` → **ask**（破坏半径大，**刻意前移**：不因“策略允许”而自动放行）
4. `policy_allows >= threshold` 且 `blast_radius < 2.0` → **allow**
5. 其余 → **ask**

信号缺失（旧响应 / 自定义端点只回 `guard_q`）时自动退回单题概率三档，`verdict_source: probability`。

```json
{
  "state": "执行 rm -rf /data/old_cache",
  "guard": {
    "instructions": "该动作是否可安全执行？",
    "threshold": 0.75
  }
}
```

- 有 `AGNES_API_KEY`：在线走 Jev 概率裁决，分为三档：
  - `probability >= threshold`：判 **`allow`**（安全放行）
  - `deny_threshold <= probability < threshold`：判 **`ask`**（存在风险，建议人工确认）
  - `probability < deny_threshold`（默认 `min(0.25, threshold*0.5)`）：判 **`deny`**（高危操作拦截）
- 无 key / adapter 缺失 / 超时熔断 / 在线调用失败：自动降级本地破坏性关键词规则（`rm -rf`、`format`、`drop database` 等 → `deny`；`delete`/`删除`/`overwrite` 等 → `ask`；其余 → `allow`），结果带 `advisory=true` 标记，调用方须把 advisory 放行当建议而非许可

## 使用原则：按需路由

- 普通对话、工具选择显而易见时，不调用 Jev；不要每轮调用，也不要每次工具调用前重复裁决。
- 只有多个工具或执行路径都合理、选择确有不确定性时，才用 `Choice` 选择路由。把可用候选及用途写清楚，例如 `computer_use`（操作桌面界面）、`browser`（网页交互）、`web_search`（查资料）。
- Jev 返回结构化建议；主 Agent 根据结果调用工具并负责执行。Jev 本身不会调用或加载其他工具。
- 只传这次判断所需的上下文，不要发送密钥、令牌、密码或无关个人信息。
- `jev_guard` 是高风险动作的可选第二意见，不是系统授权机制。普通低风险工具调用无需逐个运行；在线失败时的 `advisory` 结果（包括 `allow`）只能作为提示，不能视为安全许可。

### 工具路由示例

```json
{
  "state": "用户要在当前桌面中操作一个网页应用；browser 与 computer_use 都可行。",
  "choice": {
    "name": "tool_route",
    "instructions": "根据任务是否需要真实桌面视觉/鼠标键盘操作选择工具。",
    "criteria": {
      "computer_use": "需要观察或操作桌面窗口、原生应用或屏幕上的 UI",
      "browser": "只需在网页标签中导航、读取或填写网页内容",
      "web_search": "任务是查找网络资料，不需要操作现有界面"
    }
  }
}
```

---

## 🚀 安装与接入

### 方式一：直接克隆到 Hermes 插件目录

```bash
cd ~/.hermes/plugins
git clone https://github.com/Mrmimee/hermes-plugin-jev.git jev
```

### 方式二：启用插件

在 `~/.hermes/config.yaml` 中添加启用项：

```yaml
plugins:
  enabled:
    - jev
```

### 方式三：环境要求

**推荐**（官方直连，最快最省）：TypeSafe 控制台申请 key 后写入 `TYPESAFE_API_KEY`（可选 `JEV_MODEL` 选模型）：
```bash
export TYPESAFE_API_KEY="ts-..."     # https://console.typesafe.ai
export JEV_MODEL="jev-latest"        # 或 jev-preview / jev-1.14.0
```

**回退**（无 TypeSafe 凭证时自动生效）：`system_one_adapter` + Agnes 3.0 Flash，需要 `AGNES_API_KEY`（或在桌面的 `apikey.txt` 中配置）：
```bash
export AGNES_API_KEY="sk-..."
```

---

## 🛠️ 在 Hermes 中使用

新会话启动后，Hermes 会自动加载 `jev` 工具集。

### 自动化场景（Hermes 自主感知）
* 遇到复杂的多步骤或多工具任务分流时，Hermes 会在后台自主调用 Jev 完成并行裁决。
* 遇到系统关键变更或文件批量清理时，自动发起 Noul 风险判定。

### 显式对话示例
> “帮我用 Jev 评估一下当前需求应该调用哪个工具，以及是否存在破坏性操作。”

返回里可核对 `engine`（typesafe / agnes / offline）、`model`（如 `jev-1.14.0`）、`usage`（输入 token 计费依据）；
`jev_guard` 额外返回 `signals`（六信号）与 `verdict_source`（signals / probability），便于审计与调参。

---

## 📂 项目结构

```text
hermes-plugin-jev/
├── plugin.yaml       # Hermes 插件声明配置
├── __init__.py       # 插件入口与 Jev/jev_guard 工具注册实现
├── test_verification.py  # 验证测试（可用性 + 离线规则 + 在线路径）
├── README.md         # 项目文档
├── .gitignore        # Git 忽略配置
└── sync.py           # 一键更新与 GitHub 同步工具
```

---

## 📄 License

MIT License.
