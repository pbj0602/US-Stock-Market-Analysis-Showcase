# 美股盘前分析系统

**中文** | [English](README.en.md)

> 📌 **展示版。** 本仓库是定期发布的快照，日常开发在私有仓库进行，因此这里的提交记录对应版本发布，而非日常迭代。

每日自动运行的买方风格美股分析系统。盘前（美东 9:00 AM）拉取数据、生成多因子信号、调用 AI（DeepSeek）完成叙事分析，输出中文 HTML 报告，并把一屏摘要 + 完整报告推送到 Telegram。盘后（美东 4:45 PM）自动复盘当日信号胜率并生成明日观察清单。

**系统全面使用中文输出**。Telegram 摘要和 HTML 报告读同一个数据模型（`modules/report_model.py`），发布前经过数据复核（见下）。

---

## 功能概览

### Telegram 推送（每日一屏）

| 区块 | 内容 |
|------|------|
| 状态行 | 日期 · 牛/熊/震荡 · VIX · SPY 距 52 周高点 |
| 🚨 今天要做 | 真钱动作，最多 3 条（卖出 / 减仓 / 建核心仓），没有就写「无需操作」 |
| 📡 信号 | 入场与规避，没有就写「今日无信号」 |
| 🧭 解读 | AI 三行（核心驱动 / 最佳机会 / 最大风险） |
| 💼 账户 | 真实账户 + 建议追踪账户 + 卫星仓验证进度 |
| 👀 明日关注 | 最多 2 条 |
| 数据健康 | ✅ 全部通过 / ⚠️ N 项异常 |

完整 HTML 报告作为附件一起发送。发现严重数据问题时会另发一条告警。

### HTML 报告结构

| 区块 | 内容 |
|------|------|
| 数据健康 | 仅在复核发现问题时出现，列出每一项 |
| 🚦 市场状态 / TL;DR | 格局 / 风险等级 / VIX；三句话速览 |
| 🧭 策略执行状态 | 核心仓与卫星仓现状；观察名单（仅企稳 ≥3/4） |
| 🚨 持仓操作 | 真实持仓的卖出 / 减仓指令 |
| 📊 今日概览 | AI 三行摘要 |
| 🎯 今日行动清单 | 可以做 / 不建议 / 重点关注 / 明日关注（只放当天的状态，固定规则见本文） |
| 🏭 板块速览 | 板块表（上一交易日 / 近 5 日 / RSI / 强度） |
| 💼 真实持仓 | 持仓盈亏与止损 |
| 📡 Watchlist 信号 | 入场 + 规避 + 评分最高的 5 个观望 |
| 🎯 交易计划 / 🔍 新股机会 | 有内容时才出现 |
| 📈 大盘择时 | 一行：SPY / QQQ 加仓窗口状态 |
| 💰 如果我照建议做了 | 建议追踪账户；最近 3 笔平仓 |

### AI 分析层（DeepSeek deepseek-chat，可回退 Claude）

系统每日发起 5 次 API 调用，其中 Call 2–4 **并行执行**：

| 调用 | 内容 | 执行方式 |
|------|------|---------|
| Call 1 | 市场概览 + 信号简报 + 风险评估（合并批次） | 串行（第一步） |
| Call 2 | 持仓分析（有持仓时） | **并行** |
| Call 3 | 增长预期 + 交易计划卡 + **AI置信度复核（只降不升，0–30分）** | **并行** |
| Call 4 | 重大事件影响 | **并行** |
| Call 5 | 执行摘要（compact JSON → 3 句话） | 串行（最后一步） |

所有 AI 提示词都带同一条策略约束（`STRATEGY_GUARDRAIL`）：仓位和卖出由规则决定，AI 不得给出仓位百分比或「空仓 / 清仓」建议。复核器会再删掉漏网的句子。

**叙事优先原则**：市场概览和执行摘要均先判断当日核心催化剂（高影响宏观事件 / 跨资产风险规避信号 / 财报日），再结合技术面给出方向判断——而非从 RSI/SMA 数字开始。

### 数据复核（2026-09-23 加入）

复核器的原则：**发现不对就标出来，不悄悄替换成一个看起来正常的值。**

1. **输入**（`modules/price_history.py`）：所有日线都经过这里。剔除开盘前 yfinance 塞进来的空 K 线；日线漏掉的交易日用小时线补齐；单日涨跌 > 25% 视为可疑报价。交易日历在 `modules/trading_calendar.py`，每年更新一次假日表。
2. **交叉检查**（`modules/report_validator.py`）：牛熊判断与 SPY 相对 SMA50/200 的位置一致；卖出 / 减仓指令只针对真实持仓、止损价 > 0；买入信号的止损低于现价；板块涨跌与成分股方向一致；多个板块数值完全相同时报警。
3. **AI 输出**：删掉违反仓位规则的句子；AI 引用的 VIX / SPY 价格要与实际数据一致；段落缺失或截断会被记录。
4. **渲染**：HTML 自动转义；Telegram 超过 4096 字自动拆条。

问题分两级（`modules/data_health.py`）：**严重**（市场状态、持仓、止损）会屏蔽对应结论并另发告警；**一般**问题照常显示，在摘要底部标注。每天的 report_data 存到 `data/snapshots/`，用来复现问题。

### Watchlist 覆盖（53 支）

| 板块 | 标的 |
|------|------|
| Mega-cap 科技 | AAPL MSFT GOOGL AMZN META NVDA TSLA |
| 中型科技 / 半导体 | AMD CRM ADBE ORCL QCOM AVGO |
| 医疗 | UNH LLY JNJ ABBV MRK PFE |
| 金融 | JPM BAC GS V MA BRK-B |
| 能源 | XOM CVX COP |
| 消费者可选 | MCD NKE HD SBUX |
| 消费者必需 | WMT COST PG KO |
| 工业 | CAT HON GE UPS BA |
| 通信 | DIS NFLX VZ |
| 大盘 ETF（基准） | SPY QQQ IWM |
| 行业 ETF（板块轮动） | XLK XLF XLE XLV XLI XLP XLY |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```env
# AI 分析（必需其一，DeepSeek 为主、Claude 为备）
AI_SERVICE=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key
ANTHROPIC_API_KEY=your_claude_api_key   # 可选，DeepSeek 失败时自动回退

# 宏观数据（可选，FRED 免费）
FRED_API_KEY=your_fred_key

# 新闻数据（可选，NewsAPI 免费层）
NEWS_API_KEY=your_newsapi_key

# Telegram 推送（@BotFather 建 bot，先给 bot 发一条消息）
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# IBKR 连接（可选，需本地运行 TWS 或 IB Gateway）
IBKR_HOST=127.0.0.1
IBKR_LIVE_PORT=4001
IBKR_PAPER_PORT=4002

# 账户规模（用于仓位权重和建议追踪账户）
ACCOUNT_INITIAL_CAPITAL=10000
TOTAL_ACCOUNT_VALUE=10000
```

### 3. 录入持仓（可选）

复制模板 `cp data/portfolio_example.csv data/portfolio.csv` 后编辑：

```csv
Symbol,Shares,Purchase_Price,Purchase_Date
AAPL,10,185.50,2025-12-01
NVDA,5,110.00,2026-01-15
```

有持仓后报告会在「真实持仓」区块显示专属信号和盈亏，Watchlist 自动排除持仓股票。

### 4. 运行

**盘前分析**（美东 9:00 AM 或手动）：
```bash
python3 main.py
```

**盘后复盘**（美东 4:45 PM 或手动）：
```bash
python3 post_market_runner.py
# 或指定日期复盘
python3 post_market_runner.py 2026-05-09
```

报告保存至 `Stock Market Daily Report/`。

---

## 技术架构

### 主流水线（`main.py`，12 步）

```
Step 1   MarketContext          市场格局检测（SPY/QQQ 趋势、VIX 政权、板块强弱）
Step 2   PremarketAnalyzer      盘前缺口与成交量分析
Step 3   EventRiskEngine        财报日历 + 宏观事件风险（调整信号权重）
Step 3b  CrossAssetMonitor      跨资产快照（DXY / 铜 / 油 / JPY / HYG）
Step 3c  OptionsSentiment       P/C 比率 + VIX 期限结构
Step 3d  MacroEventAlerts       3 日内 FOMC / CPI / NFP 预警
Step 4   DataScraper            yfinance 价格 + 技术指标（SMA / EMA / RSI / MACD / BB）
Step 5   FinancialDataScraper   FRED 宏观数据 + 基本面财务数据
Step 6   NewsScraper            新闻抓取 + VADER 情绪分析
Step 7   PortfolioManager       持仓快照 + 盈亏计算
Step 8   SignalGenerator        价值回归 + 趋势突破双路径信号 + Do-Not-Trade 过滤
Step 9   StockSuggester         Watchlist 外机会扫描（100+ 股）
Step 10  AIAnalyzer             DeepSeek API（Call 1 串行，Call 2–4 并行，Call 5 串行）+ 复核器过滤
Step 11  ReportGenerator        交叉检查 → Jinja2 HTML 报告 → Telegram 推送 + 快照
Step 12  AdviceTracker          建议追踪账户更新（照建议买卖的累计盈亏）
```

运行失败时自动向 Telegram 推送错误告警，避免静默失败。

### 模块目录

```
main.py                          # 主入口（失败自动 Telegram 告警）
post_market_runner.py            # 盘后复盘（信号胜率 + 明日观察清单）
run_wrapper.py                   # launchd 包装器：按美东时间触发 9:00 AM，跳过休市日
postmarket_wrapper.py            # 盘后任务的 launchd 包装器
web_server.py                    # 可选本地服务：浏览报告、带进度条刷新
signal_stats.py                  # 命令行：按格局 / 评分档 / 个股统计信号准确率
config/
  config.py                      # 所有参数（权重 / 阈值 / API 配置）
templates/
  daily_report.html.j2           # HTML 报告 Jinja2 模板
modules/
  data_scraper.py                # 价格数据 + 技术指标
  financial_data_scraper.py      # FRED 宏观 + 基本面数据
  news_scraper.py                # 新闻 + 情绪分析
  market_context.py              # 市场格局 / VIX / 板块强弱
  premarket_analyzer.py          # 盘前缺口分析
  event_risk_engine.py           # 财报事件风险引擎
  cross_asset_monitor.py         # 跨资产监控
  options_sentiment.py           # 期权情绪（P/C 比率）
  macro_event_alerts.py          # 宏观事件日历预警
  signal_generator.py            # 多因子信号生成
  ai_analyzer.py                 # AI 分析层（DeepSeek→Claude 回退）（Call 2–5 并行）
  report_generator.py            # HTML 报告生成（加载外部模板）
  portfolio_manager.py           # 持仓管理
  position_advisor.py            # 真实持仓退出引擎（卖出 / 减仓、棘轮止损）
  ibkr_connector.py              # IBKR 行情接口
  ibkr_manager.py                # IBKR paper trading
  signal_tracker.py              # 信号表现追踪（SQLite）
  advice_tracker.py              # 建议追踪账户（照建议买卖的模拟问责账户）
  telegram_notifier.py           # Telegram 推送（一屏摘要 + HTML 附件 + 告警）
  notification_service.py        # 桌面通知
  report_model.py                # 两个通道共用的数据模型
  report_validator.py            # 发布前交叉检查 + AI 文本过滤
  data_health.py                 # 数据健康记录（严重 / 一般 / 已修复）
  price_history.py               # 统一日线获取（剔除未收盘 K 线、补缺失交易日）
  trading_calendar.py            # 美股交易日历
data/
  portfolio_example.csv          # 持仓模板，复制为 portfolio.csv 后填写（data/ 下其余文件均不纳入版本控制）
Stock Market Daily Report/       # 生成的报告（不纳入版本控制）
```

---

## 核心 + 卫星策略（2026-09-12 重构）

> **这次重构的依据是实盘成绩，不是理论。** 建议追踪账户前 10 笔平仓交易：
> 3 笔盈利**全部由时间止损砍掉、没有一笔达到目标价**；7 笔亏损**全部止损出局**。
> 胜率 30%、平均盈利与平均亏损相当 = **每笔期望值为负**，这不是运气差，是设计使然。
> 同期账户 −2.2%，而 SPY +1.5%。7 笔亏损**全部**是贴近 52 周低点的消费股（路径 A），
> 其中 MCD 在下跌途中被推荐了 **10 次**。T+20 表现最好的是 ABBV/META/MSFT/V/QQQ（强势股），
> 最差的是 NFLX/ADBE/NKE/MCD/DIS/PFE（便宜股）。**便宜的越来越便宜，强的继续强。**

账户拆成两个职责完全不同的仓位：

### 核心仓 —— 75%：赚市场的钱
- 持有 `CORE_SYMBOLS`（默认 SPY/QQQ 各半；改成 `{'SPY': 100.0}` 即单一基金版）
- **一次买满，永不因信号卖出**。没有止损、没有目标价、不做时间止损、AVOID 信号也不卖
- 路径 C（指数回调买入）是**唯一**能动核心仓资金的机制：回调 4–15% 时加仓
- `PositionAdvisor` 对核心标的直接返回 HOLD，不参与任何退出规则
- 理由：路径 C 是历史上**唯一**正收益的路径（2 战 2 胜）

### 卫星仓 —— 25%：唯一允许选股的地方
| 规则 | 设定 | 为什么 |
|------|------|--------|
| 最多持仓 | 2 只 | 限制选股能造成的伤害 |
| 选股范围 | 价格 > SMA50 > SMA200 **且** 6 个月涨幅跑赢 SPY | 取代手工股池。这一条就能挡掉全部 7 笔亏损 |
| 入场方式 | 仅路径 B：回踩 SMA50 支撑，或放量突破 | 买上升趋势中的回调，不是下跌趋势中的回调 |
| 格局闸门 | SPY 跌破 SMA200 或广度 < 40% 时停开新仓 | BEAR 格局 BUY 的 T+20 胜率只有 29% |
| **仓位** | **每笔风险 1% 账户；股数 = 风险 ÷ (买入价 − 止损价)** | **止损距离决定仓位**。旧版按置信度定仓，而 60+ 置信度的 T+20 胜率是 51%——等于按噪音定仓 |
| 止盈 | **取消固定目标价**。+1R 后止损上移至成本价，其后以高点回撤 9% 移动止损 | 目标价从未被触及（ORCL 的目标在 +59%），止损却次次命中——盈亏结构是反的 |
| 时间止损 | 30 天且涨幅 < +3% 才平仓 | 旧版 20 日无条件平仓，把 META 在 +10.5% 砍掉了 |
| 连亏熔断 | 连续 2 笔亏损 → 卫星仓停 20 个交易日（**核心仓照常运行**） | — |
| 永久除名 | 同一标的 6 个月内 2 次止损 → 移出可选范围（`data/exile_registry.json`） | NKE 三周内被买入、止损、再买入、再止损 |
| **总熔断** | 滚动 6 个月卫星仓亏损达账户 5% → **停到年底，只留核心仓** | 在冷静时决定，不在回撤中临时改 |

### 路径 A（52 周低点价值股）→ **降级为纯观察名单**
永久失去开仓权限，只保留"标记转强候选"的作用：报告中以「👀 观察名单（不建仓）」呈现，
并写明每只票**还差什么才可买**。只有当它真正站上 SMA50/SMA200 且跑赢 SPY 后，才由路径 B 接手。

### 考核方式 —— 换一个问题
不再拿账户和起始资金比，而是拿**卫星仓**和"同样的钱直接买 SPY"比（`alpha_vs_spy_pct`）。
报告提供逐月记分表。**满 20 笔后若超额收益仍 ≤ 0，卫星仓永久退役**，系统只保留核心仓、
退出管理、格局闸门与风险摘要——那些本来就是它擅长的部分。

### 执行顺序
1. **先把真钱买入核心仓**——空仓本身就是一种仓位，而且过去三个月是错的那一种（SPY +1.5%）
2. 卫星仓先在建议账户里**跑纸面交易**（`SATELLITE_PAPER_ONLY = True`），满 20 笔且超额为正才投真钱
3. 达标后才按 25% 注资卫星仓

> 首次运行会自动执行一次性迁移：平掉旧策略遗留持仓（记为「策略重构」，不计入连亏计数、
> 不触发除名），资金转入核心仓，卫星仓计分归零重新开始 20 笔验证期。

## 信号生成策略（路径细节）

设计原则：**更少但更好的交易**。BUY 信号通过三条独立路径产生，其余标的归为 HOLD / AVOID：

**路径 A — 价值回归（贴近 52 周低点，需反转确认）**
1. 价格距 52 周低点 ≤ 15%
2. **格局硬门槛**：BEAR 格局或市场广度 < 40%（watchlist 站上 SMA50 比例）时，价值路径整体停开新仓
3. 排除价值陷阱（营收下滑 > 10% / 负债权益比 > 5× / 利润大跌 / 市值 < $10 亿）
4. **接飞刀否决（收紧）**：SMA10<SMA50 超 2% / 20 日动量 < −5% / MACD 空头，**任意两条命中即否决**（旧版需三条同时满足，NKE/DIS 式阴跌可穿过）
5. **外部因子否决**：近期新闻显著偏空（均值 ≤ −0.3 且 ≥ 2 条负面），或 2 日内有财报 → 暂缓
6. RSI ≤ 60 + 5 个交易日冷却
7. **企稳硬过滤**：企稳评分（超卖 / 跌势放缓 / MACD 转强 / 放量上行）**≥ 2/4 才能成为候选**——旧版只影响置信度数字，不挡买入

**路径 B — 趋势突破（动量修复）**
1. 价格刚上穿 SMA50——3 个交易日内的真实金叉，现高于均线 0–8%
2. 以下满足 ≥ 2 项：RSI 45–65 / 成交量 > 1.2× / MACD 金叉或 10 日动量 > 3%

**路径 C — 大盘择时（SPY / QQQ，回答"什么时候买指数"）**
- 🟢 **加仓窗口**：距 52 周高点回调 4–15% + 长期趋势未破（SMA50 下方 5% 以内）+ RSI ≤ 48 + 非 BEAR 格局 → 生成 BUY 信号（排位优先于个股）
- 🟡 观望：回调到位但确认缺失（RSI 偏高 / BEAR 格局）
- 🔴 趋势破位：低于 SMA50 超 5% 或回调 > 15%，按下跌趋势对待
- 指数目标价不封顶 52 周高（指数会创新高），报告常驻「📈 大盘择时」状态卡

**排序（按胜率不按惨烈度）**：所有候选按置信度排序——旧版按"离低点多近"排序，最惨的票排最前，方向完全相反。

**止损 / 止盈**：止损贴近端摆动低点（20 日低）下方 1%，止盈锚定近端阻力（60 日高）并封顶于 52 周高（指数除外）。止损钳制在 [6%, 18%]；**结构止损 > 18% 时直接放弃该笔交易**而非硬钳制。强制最低 R/R ≥ 1.5。

**统一置信度门槛（全系统一个值）**：`MIN_BUY_CONFIDENCE = 40`。低于 40% 的 BUY：不生成交易计划卡、建议追踪账户不买、模拟盘不下单（旧版报告端 30% 过滤但账户照单全收，曾买入置信度 17% 的信号）。

**止损禁买期**：建议追踪账户止损出局的票，20 个交易日内不再推荐 / 不再买回（写入 `data/stopout_registry.json`，旧版 5 日冷却导致 DIS 7/1 止损、7/3 再推荐）。

**VIX / 格局 / 行业调整**（逐级相乘）：VIX 25–30 ×0.9、30–35 ×0.75、> 35 ×0.6；BEAR 格局 ×0.75；行业相对强弱 ×0.90–1.10。

**Do-Not-Trade 过滤器**：成交量比 < 0.3、VIX > 35、市值 < $1 亿、点差 > 2% 时自动过滤。

## AI 置信度复核（只降不升）

Call 3（交易计划批次）中，AI 对每个 BUY 信号额外输出结构化 `[ADJUST]` 判决：基于新闻 / 财报临近 / 宏观事件 / 基本面恶化等**技术信号未计入的外部风险**，扣减 0–30 分置信度（必须引用输入中的具体事实）。**不新增 API 调用，零增量成本。**

- 规则保留最终决定权：只有当调整后置信度跌破 40 门槛时，BUY 才降级为 HOLD（并从交易计划卡移除）
- 调整发生在报告 / 建议账户 / 信号日志之前——三者看到同一个最终决策
- 每次调整写入信号 Reasoning（`AI外部因子复核：置信度55→40（…）`），可审计
- Prompt 中注入滚动历史胜率（"近60日BUY信号T+5胜率53%"）作为校准参照

## 反馈回路（信号真实成绩单）

旧版 116 个 BUY 信号 0 条结果记录——系统无法度量自己。现在：

- 盘前信号记录**最终决策**（AI 复核后）+ 入场价 + 置信度至 `signal_log.db`
- 盘后每日回填 **T+5 / T+20 真实收益**（匹配 1–3 周波段周期；旧版"当日开盘→收盘方向"胜率与策略周期错配）
- 盘后报告展示「滚动信号成绩单」：T+5 / T+20 胜率、均收益、按置信度分档统计

---

## 催化剂优先框架

所有 AI 分析在生成前先构建「催化剂上下文」，按优先级排序：

1. **跨资产风险规避信号**（DXY 异常 / HYG 信用利差扩大）
2. **当日高影响宏观事件**（CPI / NFP / FOMC）→ 市场围绕此定价
3. **3 日内宏观日历**（提前预警，可选仓位减持建议）
4. **本周财报**（标记个股，建议减仓或等财报后入场）
5. **期权情绪**（P/C 比率 + VIX 期限结构）

若存在催化剂，执行摘要和市场概览首句必须说明「今日市场在交易什么」，而非从 RSI/SMA 数字开始。

---

## 建议追踪账户（advice_tracker.py）

回答一个问题：**如果我从一开始就照系统每日建议买卖，现在赚还是亏？** 取代了原先与每日建议脱节的因子量化账户。

- **起始资金**由 `.env` 的 `ACCOUNT_INITIAL_CAPITAL` 设定，自 2026-09-12 起按[核心 + 卫星](#核心--卫星策略2026-09-12-重构)两个仓位运作。
- **核心仓（75%）**：`_ensure_core()` 一次买满 SPY/QQQ，`_process_core_adds()` 在路径 C 回调信号时加仓。
  核心仓不参与 `_process_exits` / `_process_time_stops` / `_process_avoids` 的任何退出逻辑。
- **卫星仓（25%，最多 2 只）**：按风险定仓（`shares = 1%账户 ÷ (买价 − 止损)`）、无固定止盈、
  +1R 保本后按高点回撤 9% 移动止损（`_ratchet_stops`）、30 天死钱清理。
- **四道熔断**（`satellite_gate()` 按严重度依次检查）：
  - `KILLED` — 滚动 6 个月亏损达账户 5% → 停到年底（`_check_kill_switch`）
  - `PAUSED` — 连续 2 笔亏损 → 停 20 个交易日（核心仓不受影响）
  - `RETIRED` — 满 20 笔且 α ≤ 0 → 永久退役选股
  - `FULL` — 已持满 2 只
  另有**永久除名**：同一标的 6 个月内 2 次止损（`data/exile_registry.json`，信号层同步禁推）。
- **纸面验证**：`SATELLITE_PAPER_ONLY = True` 时卫星仓持仓标记 `paper`，报告全程提示"勿投真钱"。
- 输出：账户总值、收益率、盈亏、胜率、两个仓位的拆分（`sleeves`）、
  **卫星仓对 SPY 的超额收益与逐月记分表**（`satellite_monthly`）、平仓记录。
- **首次运行自动迁移**：平掉旧策略遗留持仓（记为「策略重构」，不计入连亏、不触发除名），
  资金转入核心仓，卫星仓计分归零（`arch_version` 标记，只执行一次）。
- 状态持久化于 `data/advice_account.json`，逐日复利累计（NaN 价格自动过滤，不再污染净值曲线）。

---

## 盘后复盘（post_market_runner.py）

每日收盘后运行，输出：

- **信号胜率**：当日 BUY/SELL 方向准确率 + 平均日收益（以当日开盘价为参考）
- **滚动信号成绩单**：回填历史 BUY 信号 T+5 / T+20 真实收益，输出近 60 日胜率、均收益、按置信度分档表现（真正的策略成绩单）
- **明日观察清单（三层）**：
  - 仍有效买入信号（未触及止损，明日继续跟踪突破）
  - 极端 RSI 观察（RSI < 32 或 > 68 的 HOLD 股，关注反转）
  - 财报前哨戒（明日有财报，建议控制仓位）
- **报告**：Markdown 文件保存至 `Stock Market Daily Report/post_market_YYYYMMDD.md`
- **推送**：转成 Telegram 格式后推送

---

## 调度

使用 macOS launchd（配置文件在 `~/Library/LaunchAgents/`）每 15 分钟触发一次包装器；包装器按美东时间判断是否到点，每个交易日只真正运行一次，出差换时区也不会错位。

| 任务 | 时间 | 命令 |
|------|------|------|
| 盘前分析 | 美东 9:00 AM | `python3 run_wrapper.py` → `main.py` |
| 盘后复盘 | 美东 4:45 PM | `python3 postmarket_wrapper.py` → `post_market_runner.py` |

---

## 注意事项

- 本系统仅供学习和个人研究使用，**不构成投资建议**
- IBKR 实盘账户硬编码为只读（`IBKR_READONLY_LIVE = True`），不会下单
- API 密钥存储于本地 `.env` 文件，不纳入版本控制
- IBKR 连接需要 TWS 或 IB Gateway 在本地运行并开放对应端口
