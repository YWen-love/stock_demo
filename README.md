# 对话式库存顾问（stock_demo）

面向社区小店的 AI 补货助手。店主用大白话问「可乐该进多少」，助手给出进什么、进多少的理由；没有 API 配置时降级为纯规则模式，结论精度不变。

核心原则一句话：**数据是我们的事实，模型只是读事实的人。** 所有数值与分级由确定性规则算出，LLM 只负责把它翻译成人话。

---

## 快速开始

```powershell
# 1. 依赖（Python 3.10+）
python -m pip install -r requirements.txt

# 2. 配置（不配也能跑，进入纯规则模式）
Copy-Item .env.example .env
```

```dotenv
MINIMAX_API_KEY=你的API密钥
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_MODEL=MiniMax-M3.1-Flash
```

`.env` 查找顺序：项目级 `.env` → 上级共享 `.env`（`AI项目\.env`），均未配置时启用本地规则兜底模式。

```powershell
# 3. 造一份演示数据（5 个商品 × 3 周历史，覆盖全部四种建议场景）
python scripts/seed_demo.py

# 4. 启动
python main.py
# 或指定端口 / 重置数据
python main.py --port 7861
python main.py --reseed
```

浏览器打开 `http://127.0.0.1:7860`。

> 魔搭（ModelScope）gradio 空间部署用 `app.py`（监听 `0.0.0.0:7860`，不解析命令行参数）；本地开发用 `main.py`。两者共享同一套 advisor 代码。

---

## 目录结构

```
stock_demo/
├── app.py                     # 魔搭 gradio 空间入口（0.0.0.0:7860，不解析命令行）
├── main.py                    # 本地启动入口（--host / --port / --reseed）
├── advisor/                   # ★ 业务主线（新版）
│   ├── __init__.py            # 依赖方向声明
│   ├── advisor.py             # 顾问编排：意图路由 → 取数 → 规则 → 讲人话 → 校验
│   ├── config.py              # 规则参数 + SYSTEM_PROMPT + LLM 配置
│   ├── rules.py               # ★ 规则引擎：全部数值与分级的唯一实现点
│   ├── tools.py               # 6 个工具的定义与执行
│   ├── llm.py                 # tool-call 循环 + 非法输出重试
│   ├── onboarding.py          # 冷启动对话建档
│   ├── repository.py          # 数据读写 + 销量倒推
│   ├── db.py                  # SQLite 建表、幂等迁移
│   ├── rag.py                 # RAG 封装 + 索引指纹对拍
│   ├── ui.py                  # Gradio 对话界面 + 盘点卡片
│   └── paths.py               # 路径解析（开发态 / 打包态 / 自定义目录）
├── stock_rag/                 # 本地 RAG
│   ├── vector_store.py        # TF-IDF + 余弦相似度（advisor/rag.py 直接调用）
│   ├── rag_docs/stock_rule.txt # ★ 企业库存规则原文（唯一规则口径）
│   └── vector_db/             # 序列化向量库（随包分发）
├── scripts/
│   ├── seed_demo.py           # 造演示数据
│   └── batch_report.py        # CLI 批量报表（md / csv）
├── tests/                     # 96 个 pytest 用例
│   ├── test_rules.py          # 规则边界、分级覆盖、配置对拍
│   ├── test_advisor.py        # 编排、判定闸、工具、非法输出降级
│   ├── test_db.py             # 销量推导、幂等、软删
│   ├── test_rag.py            # 索引指纹重建、规则缺失抛错
│   └── test_ui.py             # 卡片预填、提交闭环、对话历史
├── data/                      # SQLite 落库目录（自动创建）
└── build_windows.ps1 / installer.iss / stock_inventory.spec
```

### 依赖方向

```
ui → advisor → { llm, tools, rules, repository, rag, db }
                       ↑
              rules 不 import 任何 LLM / SDK / 网络的东西
```

`rules.py` 要是哪天顺手 import 了 `openai`，就再也说不清「没有 API 的时候结论会不会变」。能在没网的环境里单测 `rules.py`，是这条约束换来的。

---

## 核心机制

### 1. 规则引擎是唯一判定点

`advisor/rules.py` 独占全部数值与分级，不 import 任何 LLM：

| 指标 | 口径 |
|---|---|
| 常规安全库存阈值 | 滚动周均销量 × 1.2 |
| 紧急断货阈值 | 滚动周均销量 ÷ 7 × 2 × 1.2 |
| 补货量 | 安全阈值 + 活动上浮 − 当前库存（不足 0 记 0） |
| 滚动周均 | 最近 4 周算术平均，不足 4 周按已有周数算，**不按 0 摊薄** |
| 分级 | 库存充足 / 需要补货 / 紧急补货，**等于阈值归上一档** |
| 滞销 | 连续 3 周低于 `max(周均×20%, 每周 1 件)` |
| 快过期 | 剩余保质期 < 总保质期 ÷ 3；未填保质期或进货日不判定 |
| 活动备货 | 促销事件在安全阈值上浮 30%；清仓事件不抬阈值 |

阈值系数同时写死在 `stock_rag/rag_docs/stock_rule.txt`（给人和模型看的权威表述）。两边对不齐就是 bug，`tests/test_rules.py::test_config_matches_rule_document` 逐系数对拍锁住，改任意一侧另一侧必须同改。

### 2. 判定闸：模型说错等级，代码改回来

模型算的数经常是对的，但**分级会挑一个听起来没那么吓人的说法**——「紧急补货」听上去太严重，于是写成「需要补货」。数字对、分级错的建议，店主按它进了货，等到断货才发现不对。

两层拦截：

- **数据层**：`submit_advice` 工具返回规则口径，强制覆盖模型上报的 `grade` / `replenish_qty`，不一致时把差异回传给模型要求照规则说。
- **文本层**：`enforce_grades` 按句切分，认出每句说的是哪个商品，只在能唯一确定对应关系时替换分级词。被否定的分级词（`不需要补货` 里的 `需要补货`）不动——那是模型在正确否定，照改会造出自相矛盾的话。

### 3. 销量倒推与数据可信度分级

小店老板一周盘一次货，中间不记流水，所以周销量不是记出来的，是倒推出来的：

```
本周销量 = 上期库存 + 本周进货 - 本期库存
```

四个分支都要处理，不能只跑通顺的那条：

| 情形 | 处理 |
|---|---|
| 有上期库存 | 正常推导，`source='derived'` |
| 无上期但有估算日均 | 按估算周量兜底，`source='estimated'`，并在告警里声明这是估算 |
| 两者皆无 | **不写销量、判不出补货量**，如实回问店主要数据 |
| 销量为负 | 判为进货漏报：销量记 0 不污染均值，负数原样带出去单独告警 |

### 4. Function Calling 工具链

6 个工具，边界硬分三类：

| 类别 | 工具 | 说明 |
|---|---|---|
| 算术 | `math_calc` | 字符白名单拦截 eval 注入；全项目唯一的 `eval` |
| 只读 | `query_products` / `get_advice` | 模型看到的是规则算好的结论，不是原始行 |
| 写库 | `record_event` / `upsert_product` | 只有这两条路能改数据，杜绝模型「顺手把库存改了」 |

Prompt 强制「所有数字只能来自工具返回值，绝对不许自己算、自己编」。Agent 循环处理 tool-call 与参数解析，检测模型直出 Markdown 表格判定为非法输出，注入重写指令强制重试（上限 2 次），连续失败降级纯规则。

工具出错不抛异常，返回 `{"ok": false, "error": ...}` 让模型自己看到并纠正——抛出去会中断整轮对话，而模型其实有能力看到错误然后改。

### 5. 冷启动对话建档

档案为空时进入专用建档模式：**一次只问一件事，问完立刻给反馈**，让建档成为对话的一部分而不是一段表单。

该阶段只开放 `upsert_product` 与 `math_calc` 两个工具——查库存或记活动的数据还不存在，开放了只会让模型编。同时追加阶段化 Prompt，不让它说「数据不足、无法计算」。

### 6. 盘点卡片闭环

Gradio 6 的 Chatbot 每条消息渲染时都会新实例化一份，服务端挂不上 `.input()` / `.change()` 监听，组件塞进气泡就变成只能看的死表。所以盘点卡片做成对话流下方的**独立组件**：

- **预填上次盘点值** —— 店主一周只盘一次，绝大多数格子跟上次数一样，只需改动的那几格
- **「按近 4 周均值预填」** —— 上周没盘但记得卖得差不多了，点一下就有起点
- **按需展开** —— 平时纯对话，说「该盘点了」或点按钮才展开，提交完自动收起、摘要回写对话流

### 7. 意图路由刻意不交给模型

`detect_intent` 用确定性关键词匹配判意图（盘点 / 滞销 / 过期 / 补货）。看起来像为难模型，其实相反：意图判断错一次，后面取数就全错，而模型判断意图的准确率并不比关键词高，还多一个说不清的失败点。真正该让模型干的只有最后一步——把结论讲成人话。

### 8. RAG 索引指纹对拍

改了 `stock_rule.txt` 之后索引还是老的，检索会安安静静返回旧规则——不报错、不告警，店主拿到一个按过期口径算的建议。**这种 bug 最贵，因为它看起来是在正常工作。**

每次检索前比对源文档指纹（文件名 + 内容哈希），不一致就重建。索引缺失或损坏同样重建；规则文档本身缺失则抛异常，**不用兜底规则顶上**——那等于让模型自己编。

本地检索实现：TF-IDF + 余弦相似度，中文 1-2 gram 字符级分词、500 字分块、序列化落盘，离线可用，不依赖外部向量库。

---

## 数据库表定义

SQLite，落在 `data/inventory.db`（可用 `ADVISOR_DATA_DIR` 改目录，`ADVISOR_DB_PATH` 直接改文件）。建表 `CREATE TABLE IF NOT EXISTS` + 幂等 `ALTER TABLE` 补列，老库不删不重建。

| 表 | 用途 |
|---|---|
| `products` | 商品档案：名称、单位、当前库存、估算日均、保质期、进货日、状态 |
| `inventory_snapshots` | 每周盘点快照，`UNIQUE(product_id, week_key)` |
| `sales_weekly` | 周销量与推导来源，`UNIQUE(product_id, week_key)` |
| `product_events` | 促销 / 清仓 / 到货事件 |
| `conversations` / `messages` | 对话历史，支撑 LLM 无状态下的跨会话记忆 |

---

## 环境变量

| 变量 | 说明 |
|---|---|
| `MINIMAX_API_KEY` | 模型 API Key，不配则纯规则模式 |
| `MINIMAX_BASE_URL` | 默认 `https://api.minimaxi.com/v1` |
| `MINIMAX_MODEL` | 默认 `MiniMax-M3.1-Flash`（reasoning 模型） |
| `ADVISOR_DATA_DIR` | 数据目录，优先级高于 `APP_DATA_DIR` |
| `APP_DATA_DIR` | 数据目录兜底 |
| `ADVISOR_DB_PATH` | 直接指定 SQLite 文件路径（测试用临时库） |

---

## 测试

```powershell
python -m pytest -q
```

96 个用例，分布：

| 文件 | 覆盖 |
|---|---|
| `test_rules.py` | 阈值公式、分级三档与边界、滞销绝对下限、过期、活动上浮、判定闸覆盖、**TXT 与 config 对拍** |
| `test_advisor.py` | 意图路由、商品聚焦、判定闸改写、非法表格重试、API 失败降级、工具注入拦截、冷启动建档 |
| `test_db.py` | 销量推导三分支、幂等写入、软删、事件周次隔离、消息往返 |
| `test_rag.py` | 指纹变更重建、索引缺失重建、规则缺失抛错 |
| `test_ui.py` | 卡片预填、提交闭环、未知商品名跳过、对话历史保持 |

---

## 打包（Windows）

必须在 Windows 上完成，建议与目标机器相同或更低版本的 Windows 环境。

```powershell
.\build_windows.ps1
```

脚本安装构建依赖 → 执行 PyInstaller（`stock_inventory.spec`）→ 若检测到 Inno Setup 的 `iscc.exe` 则生成安装包。

产物：

- 可直接运行目录：`dist\StockInventoryApp\StockInventoryApp.exe`
- 安装包：`installer_output\StockInventorySetup.exe`

安装包已包含 `stock_rag/rag_docs` 与 `stock_rag/vector_db`，首次运行不需要重建向量库。安装后复制 `.env.example` 为 `.env` 填入密钥；不填也能用本地兜底模式。

### 发布注意事项

- 不要把真实 `.env`、API 密钥或 `local_demo.log` 放进安装包或提交仓库
- 用户需要能写入安装目录，报告和日志默认保存在该目录
- 若企业电脑限制写入 `AppData` 或阻止本地回环 `127.0.0.1`，需管理员调整策略

---

## CLI 批量报表

总店 / 加盟商那种「一堆 Excel 要跑」的场景还在，留了一个命令行入口：

```powershell
python scripts/batch_report.py 库存.xlsx --format md
python scripts/batch_report.py 库存.xlsx --format csv --out 报表.csv
```

与对话版共用同一套 `advisor/rules.py`，阈值口径只有一处。表格需包含 `商品 id` / `商品名称` / `当前库存` / `近 7 天销量` 四列，字段缺失或非数字会拦下并定位到具体行。
