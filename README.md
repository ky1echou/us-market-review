# us-market-review

`us-market-review` 是部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。目标不是生成“核心版”或“能跑就行”的简报，而是尽量复刻并强化目标样例《20260528美股复盘.pdf》的完整卖方投研日评结构。

正式报告固定包含：标题与摘要、美股指数概况、宏观与地缘、行业与 ETF 结构、美股科技股跟踪、AI 主线与重要催化、AH 盘前映射、Update Matrix、最终结论。

## 产品原则

- 不生成核心版/简版替代正式报告。
- 不通过减少股票池来绕过数据源问题。
- 不因为免费源限流而删除栏目。
- 行情不达标时不生成正式 Markdown/PDF，只发送 Telegram 状态提示。
- 技术错误只写入 `logs/daily.log`，不进入正式报告正文。
- 新闻只作为催化，不能替代行情数据。

## 完整股票池

完整报告必须覆盖以下标的：

- 指数/ETF：`SPY, QQQ, DIA, IWM, SMH, SOXX, XLK, XLF, XLE, XLV, TLT, GLD, USO`
- 大型科技：`NVDA, MSFT, AAPL, AMZN, GOOGL, META, TSLA`
- AI/半导体：`AMD, AVGO, MU, MRVL, ARM`
- AI应用：`SNOW, NOW`
- 宏观/风险资产：`VIX, US10Y, DXY, GLD, USO, CPER, BTCUSD`

## 行情源顺序

当前默认顺序：

1. Financial Modeling Prep（FMP，主源，需要 `FMP_API_KEY`）
2. Twelve Data（备用正式源，需要 `TWELVE_DATA_API_KEY`，没有 key 时自动跳过）
3. Stooq daily CSV（历史/备用）
4. Yahoo Finance via `yfinance`（最后兜底）
5. 本地缓存

FMP 使用 stable endpoint：

```text
quote: https://financialmodelingprep.com/stable/quote?symbol=AAPL&apikey=...
batch quote: https://financialmodelingprep.com/stable/batch-quote?symbols=AAPL,MSFT,NVDA&apikey=...
historical EOD: https://financialmodelingprep.com/stable/historical-price-eod/full?symbol=AAPL&apikey=...
```

若要完整、稳定、每天准时生成报告，建议使用 FMP/Twelve Data 正式 API。免费源可能不稳定，但系统不会通过删内容来规避；如果完整股票池无法达到门槛，Telegram 会提示：`免费行情源无法满足完整报告，需要接入/升级正式数据源。`

## 完整报告质量门槛

正式报告生成条件：

- 完整股票池行情成功率 >= 90%。
- 指数/ETF关键项必须完整：`SPY, QQQ, DIA, IWM, SMH, SOXX, VIX`。
- 大型科技股关键项必须完整：`NVDA, MSFT, AAPL, AMZN, GOOGL, META, TSLA`。
- AI/半导体关键项至少达到配置门槛：`AMD, AVGO, MU, MRVL, ARM`。

若不达标，不生成正式 Markdown/PDF，只发送 Telegram 状态提示，包含成功 ticker、失败 ticker、失败原因、数据源、关键分组状态，以及是否需要升级/接入正式数据源。

## 一键安装

在服务器项目目录运行：

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

## 配置 `.env`

```bash
nano .env
```

Telegram：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

行情源：

```env
MARKET_DATA_PROVIDER_ORDER=fmp,twelve_data,stooq,yfinance,cache
FMP_API_KEY=
TWELVE_DATA_API_KEY=
MARKET_MIN_SUCCESS_RATIO=0.9
RUN_DAILY_TIMEOUT=45m
```

飞书可选：

```env
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
```

不要把任何 Token、Webhook、API Key、SSH 私钥写进代码或 README。

## Provider 诊断

检查完整股票池：

```bash
cd /opt/us-market-review
source .venv/bin/activate
python -m src.provider_check --provider all --universe --config config.yaml
cat logs/provider_check.log
```

检查 FMP 最小样本：

```bash
python -m src.provider_check --provider fmp --symbols AAPL,MSFT,NVDA --config config.yaml
```

输出不会打印完整 API key，只显示是否存在、长度、首尾空格、endpoint 类型、HTTP status、JSON 类型与失败原因。

## 手动测试

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
python -m src.health_check --config config.yaml
python -m src.provider_check --provider all --universe --config config.yaml
bash run_daily.sh
tail -n 120 logs/daily.log
```

## 定时运行

正式日报由服务器 cron 执行，北京时间每周二到周六 7:30：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

`run_daily.sh` 默认允许最长 45 分钟，用于完整股票池分批请求、重试与等待限流恢复。若已有任务正在运行，本次触发会跳过，并向 Telegram 发送提示。

## 自动部署

`main` 有新提交后，GitHub Actions 只做部署和轻量健康检查，不自动运行正式日报。正式日报只由 cron 或手动 `workflow_dispatch + run_report=true` 触发。

需要配置 GitHub Repository Secrets：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
FMP_API_KEY
TWELVE_DATA_API_KEY
```

部署时会保留服务器上的 `.env`，不会覆盖 Telegram/飞书配置。

## PDF 排查

PDF 使用 HTML -> PDF 方式生成，优先 `wkhtmltopdf`，并校验文件存在、大小和 `%PDF` 文件头。若 PDF 校验失败，不推送坏 PDF，只发送 Markdown。

```bash
which wkhtmltopdf
wkhtmltopdf --version
fc-list | grep -E "WenQuanYi|Noto Sans CJK|DejaVu" | head
bash scripts/install_ubuntu.sh
bash run_daily.sh
tail -n 200 logs/daily.log
```

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。
