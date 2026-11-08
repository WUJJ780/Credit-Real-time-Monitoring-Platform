# 信贷自动风控多 Agent 系统（Credit Risk Workbench）

面向商业银行零售信贷业务的 **LangGraph 多 Agent 协作风控系统**：自动完成进件审核、反欺诈、信用评估、审批决策、合规审计与贷后管理，把人工审批流水线（单笔 30 分钟+）变成**秒级自动决策 + 全链路审计可追溯**的自动化流水线。

> 想了解设计思想与实现细节？见 [architecture.md](architecture.md)（设计与实现讲解）。

---

## 一、项目介绍

### 解决什么问题

| 痛点 | 本系统的解法 |
|---|---|
| 人工审批效率低 | 7 个专职 Agent 流水线协作，提交后秒级出决策 |
| 规则覆盖不全、标准不一 | 规则引擎 + 策略配置中心，阈值集中管理、口径统一 |
| AI 决策不可解释、不敢上线 | 三层兜底（LLM→规则→保守转人工）+ 每笔决策完整理由链与审计事件 |
| 合规追溯困难 | 全流程审计留痕（JSON/日志/消息总线/MinIO），监管报送可查 |
| 多源数据孤岛 | 统一数据能力层（MySQL/Kafka/Neo4j/Milvus/MinIO/Doris），未接入自动 Mock 降级 |

### 核心特性

1. **7 Agent 风控流水线**：风控中台（总调度）+ 进件审核 + 反欺诈 + 信用评估 + 审批决策 + 合规审计 + 贷后管理，Agent 之间只传 Pydantic 结构化数据（`agents/resume/state.py`）
2. **三层兜底决策**：LLM 推理 → 规则引擎 → 默认兜底（宁可转人工，不可误放行），层间降级全程审计留痕，LLM 挂了系统照常运行
3. **高并发异步架构**：LangGraph 同步/异步双图、LLM 原生异步、阻塞 SDK 线程池卸载、信号量限流、单笔超时熔断、SSE 实时进度（实测 50 并发约 45-60 笔/秒）
4. **可视化演示前端**（零依赖静态页）：实时监控台（KPI/决策分布/环节进度条/审计时间线）、申请单在线填写页、**问答 RAG 智能助手**（产品/利率/申请引导/进度查询/**补件多轮闭环**）
5. **知识库 + 规则可维护**：策略阈值集中在 `config/risk_strategy.py`（改配置即迭代策略，规则引擎与 LLM 提示词自动同步）；智能助手知识库为 `knowledge/` 下 Markdown，改文件即维护
6. **风控能力完整**：黑名单/多头/设备风险/团伙图谱（Neo4j）/相似欺诈案件（Milvus）/信用评分卡/利率定价（LPR+等级加点）/合规一票否决（4倍LPR、24%、资金用途、PIPL）

### 技术栈

Python 3.12 · LangGraph · LangChain · OpenAI 兼容 LLM（DeepSeek/Qwen 等）· Pydantic v2 · FastAPI · asyncio · MySQL/Kafka/Neo4j/Milvus/MinIO/Doris（可选，Mock 降级）

### 项目结构

```
credit_risk_system/
├── agents/                  # 风控Agent群（base_agent + 6岗位 + 中台 + 问答RAGAgent）
│   └── resume/              # 统一状态定义(state.py) + 提示词库(prompts.py)
├── core/                    # 编排器/规则引擎/评分卡/图谱/数据层/RAG知识库/异步流水线…
├── models/                  # 业务模型（loan）与状态兼容层（risk）
└── config/                  # risk_strategy.py 策略配置中心 + settings.py
frontend/                    # 演示前端（监控台/申请单填写/智能助手，统一侧边导航）
knowledge/                   # RAG 知识库（产品/利率/申请条件/补件/FAQ）
main.py                      # CLI 演示入口
server.py                    # FastAPI 异步服务入口
scripts/load_test.py         # 高并发压测脚本
.claude/skills/              # 5 个信贷业务技能（新增Agent/规则/策略/决策解读/合规审查）
docker-compose.yml           # 真实数据基础设施（可选）
```

---

## 二、安装方式

### 1. 环境要求

- Python 3.10 ~ 3.12（实测 3.12.4）
- Windows / Linux / macOS
- （可选）Docker —— 仅启用真实数据基础设施时需要

### 2. 安装依赖

```bash
# 进入项目目录
cd credit-risk-workbench

# 创建虚拟环境并激活
python -m venv .venv
.venv\Scripts\activate            # Linux/Mac: source .venv/bin/activate

# 一键安装全部依赖（67个包全部精确锁定版本）
pip install -r requirements.txt
```

> ⚠️ 网络代理不稳定的环境：pip 失败直接重试一次通常即可。

### 3. 可选配置

**启用 LLM 推理层**（不配置也能跑，走规则引擎离线模式）——编辑 `.env.local`：

```ini
LLM_API_KEY=sk-你的Key                    # DeepSeek/Qwen/OpenAI 兼容服务均可
LLM_BASE_URL=https://api.deepseek.com     # 默认 DeepSeek
LLM_MODEL=deepseek-chat
```

**启用真实数据基础设施**（MySQL/Kafka/Neo4j/Milvus/MinIO/Doris）：

```bash
docker compose up -d
# 然后在 .env.local 打开对应开关，如 MYSQL_ENABLED=true
# 未启用的组件一律自动 Mock 降级，不影响运行
```

**调整并发与端口**（`.env.local`）：`ASYNC_MAX_APPLICATIONS`（并发上限）、`LLM_MAX_CONCURRENCY`、`PIPELINE_TIMEOUT_SECONDS`、`SERVER_PORT`（默认 8000）等。

---

## 三、使用方法

### 1. CLI 演示（最快上手）

```bash
.venv\Scripts\python.exe main.py                  # 串行运行 5 个演示案例
.venv\Scripts\python.exe main.py --case fraud     # 指定案例: normal/fraud/review/supplement/compliance
.venv\Scripts\python.exe main.py --concurrent     # 高并发异步模式（全部案例并发处理）
```

5 个演示案例覆盖全部流程分支（决策与审计留痕在 `output/<申请单号>_decision.json`）：

| 案例 | 场景 | 预期决策 |
|---|---|---|
| normal | 优质客户 | ✅ APPROVED（额度15万，年化5.95%） |
| fraud | 黑名单+多头+模拟器+团伙 | ❌ REJECTED |
| review | 多头+新设备+负债高 | ⏸ MANUAL_REVIEW |
| supplement | 缺收入证明 | 📋 SUPPLEMENT |
| compliance | 用途=股票投资 | ❌ REJECTED（合规一票否决） |

### 2. 启动服务（API + 可视化前端）

```bash
.venv\Scripts\python.exe server.py
```

启动后浏览器访问：

| 页面 | 地址 | 功能 |
|---|---|---|
| 🖥 实时监控台 | http://127.0.0.1:8000 | 受理演示案例/批量模拟高并发、KPI/决策分布、申请卡片（环节进度条实时推进 + 审计时间线） |
| 📝 申请单填写 | http://127.0.0.1:8000/submit | 完整申请表单（勾选「使用演示身份」可体验审批通过全流程），提交后自动跳转监控台聚焦跟踪 |
| 💬 智能助手 | http://127.0.0.1:8000/chat | 多轮对话：产品介绍（RAG）、利率查询、申请引导、进度查询、**补件办理闭环** |
| 📖 API 文档 | http://127.0.0.1:8000/docs | Swagger 在线调试全部接口 |

### 3. API 接口速查

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/applications` | 受理申请（202 立即返回，后台异步处理，幂等） |
| POST | `/api/v1/applications/batch` | 批量受理 |
| POST | `/api/v1/applications/{id}/supplement` | 补件重新受理（SUPPLEMENT 终态可用） |
| POST | `/api/v1/demo?cases=all&times=N` | 批量受理演示案例（times>1 生成变体模拟高并发） |
| GET | `/api/v1/applications/{id}` | 查询状态/最终决策/反欺诈信号/审计事件链 |
| GET | `/api/v1/applications/{id}/events` | SSE 实时进度推送 |
| GET | `/api/v1/applications` | 列表最近受理的申请 |
| POST | `/api/v1/chat` | 智能助手对话（多轮，session_id 保持上下文） |
| GET | `/api/v1/chat/history/{session_id}` | 会话历史（刷新恢复对话） |
| GET | `/api/v1/stats` | 吞吐/耗时分位统计 |
| GET | `/healthz` | 健康检查与决策模式 |

### 4. 高并发压测

```bash
.venv\Scripts\python.exe scripts/load_test.py --concurrency 50 --requests 200
#  压测完成 | 总数=200 | 并发=50 | 吞吐=45.2 笔/秒 | p99=2041ms
```

> 压测以演示案例为模板批量生成申请，同一模板共享手机号会真实触发图谱团伙关联检测，决策保守转人工复核——属预期行为。

### 5. 技能化工作流（Claude Code 会话内）

项目内置 5 个信贷业务技能（`.claude/skills/`），在对话中描述需求即可触发：`add-risk-agent`（新增风控岗位）、`add-risk-rule`（新增风控规则）、`adjust-risk-strategy`（策略迭代）、`decision-explain`（决策解读）、`credit-compliance-review`（合规审查）。

---

**合规提示**：本系统内置利率合规（4倍LPR/24%）、PIPL 授权、征信授权、资金用途与审计留痕等合规规则；演示数据均为虚构。**实际投产前需经法务合规评审与监管报备**。
