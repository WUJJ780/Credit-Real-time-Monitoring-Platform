---
name: adjust-risk-strategy
description: 调整信贷风控策略参数（审批阈值、欺诈分档、利率加点、评分卡分箱、准入条件等）的安全迭代流程。触发条件：用户要求"调整策略/改阈值/收紧风控/放宽审批/调整利率"等。按配置中心约定修改并全量验证策略影响。
---

# 风控策略迭代

本项目的所有策略参数集中在 `credit_risk_system/config/risk_strategy.py`（策略配置中心），
**改配置即可上线新策略，无需改 Agent 代码**。但策略调整是风控系统最高危的操作，
必须走「修改 → 影响分析 → 全量验证 → 留痕」流程。

## 策略参数速查（risk_strategy.py 分区）

| 分区 | 关键参数 | 影响 |
|---|---|---|
| 一、进件审核 | `AGE_MIN/MAX`、`INCOME_MIN`、`AMOUNT_MIN/MAX`、`TERM_MIN/MAX`、`OCR_CONFIDENCE_MIN`、`DOC_WEIGHTS` | 准入硬条件与资料完整度 |
| 二、反欺诈 | `FRAUD_BANDS`（欺诈分→等级）、`BLACKLIST_WEIGHTS`、`MULTILOAN_RULES`、`DEVICE_SIGNALS`、`MODULE_CAPS`、`GANG_FRAUD_LINK_SCORE` | 欺诈评分与一票否决线 |
| 三、信用评估 | `DTI_CAP`（转人工线）、`DTI_HARD_REJECT`（拒绝线）、`GRADE_BANDS`、`INCOME_MULTIPLES`、`SCORECARD_CONFIG` | 信用等级/额度倍数/硬拒绝 |
| 四、审批决策 | `AUTO_APPROVE_RISK`(35)、`AUTO_REJECT_RISK`(65)、`COMPOSITE_WEIGHTS`、`LPR_1Y`、`GRADE_SPREADS` | 自动通过/拒绝线与利率定价 |
| 五、贷后管理 | `PD_BY_GRADE`、`MIGRATION_MATRIX`、`MONITORING_PLANS` | 贷后分级/监控/催收 |
| 六、合规审计 | `PROHIBITED_PURPOSE_KEYWORDS`、`RATE_CAP_LPR_MULTIPLE`、`RATE_CAP_ABSOLUTE` | 监管红线（不建议放宽） |

## 修改流程

### 1. 只改配置，不改代码

- 阈值/权重/分箱/加点一律改 `risk_strategy.py`
- **自动同步机制**：规则引擎（`rule_engine.py`）与 LLM 提示词（`resume/prompts.py`）都用
  `rs.XXX` 常量插值，改配置后两处口径自动一致，无需手动同步

### 2. 影响分析（改之前先回答）

- 这条策略挡的是什么风险？（如收紧 DTI 上限 → 降低信用风险敞口，但提高转人工率）
- 放宽方向的参数（利率上限、禁止用途、征信授权）涉及**监管红线，不通过策略迭代放宽**
- 灰度建议：按渠道（`Channel`）或客群分流上线，不要全量一刀切

### 3. 全量验证（每改一次必跑）

```bash
.venv/Scripts/python.exe main.py          # 5 案例决策对照预期表
.venv/Scripts/python.exe main.py --concurrent
```

5 案例预期决策（改动后若偏离，要么是刻意为之、要么改错了）：

| 案例 | 场景 | 预期 |
|---|---|---|
| normal | 优质客户 | APPROVED（额度15万，年化5.95%） |
| fraud | 黑名单+团伙 | REJECTED |
| review | 多头+高负债 | MANUAL_REVIEW |
| supplement | 缺收入证明 | SUPPLEMENT |
| compliance | 用途=股票 | REJECTED |

- 需要看批量影响时：起服务 + 压测
  `.venv/Scripts/python.exe server.py` + `scripts/load_test.py --concurrency 50 --requests 200`
- 关注决策分布变化（监控台「决策分布」或 `/api/v1/stats`）

### 4. 留痕（合规要求）

- 在改动说明/提交信息中记录：改了什么参数、从什么值到什么值、为什么改
- 策略上线建议灰度发布（按渠道/客群分流），详见 README「扩展指南」

## 常见坑

- 改完只跑一个案例就认为通过 → 必须 5 案例全跑（策略参数互相耦合）
- 提示词里出现与配置不一致的数字 → 说明有人绕过配置中心硬编码，需改回 `rs.XXX` 插值
- 收紧策略后监控台出现大面积 MANUAL_REVIEW → 符合保守设计，但需评估人工产能
- 修改 `PROHIBITED_PURPOSE_KEYWORDS` 等合规参数前，先做合规评审（见 credit-compliance-review 技能）
