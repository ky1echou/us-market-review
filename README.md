# us-market-review

`us-market-review` 是部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。它会在美股收盘后抓取行情和公开 RSS 新闻，生成卖方投研日评风格的 Markdown 与 PDF 报告，并可通过 Telegram Bot 或飞书 Webhook 推送。

当前 MVP 数据来源：

- 行情：默认 Stooq daily CSV，失败后切换 Yahoo Finance via `yfinance`，再失败才使用本地缓存
- 新闻：`config.yaml` 中配置的公开 RSS 源，并使用严格主题相关性评分筛选
- 配置：`.env` 与 `config.yaml`
- 日志：`logs/daily.log` 与 `logs/provider_check.log`

项目不会编造关键数字。行情可用率低于 70% 时，系统会立刻停止正式报告生成：不生成 Markdown，不生成 PDF，也不发送任何报告文件，只向 Telegram 推送状态消息：

```text
今日行情数据抓取失败或不足，未生成正式美股复盘，请检查数据源。
行情成功数量: x/y
行情失败数量: z
行情成功率: xx.x%
实时获取数量: a
缓存降级数量: b
数据源: ...
获取时间: ...
```

正式报告正文不会输出 `Too Many Requests`、`no cache fallback available`、抓取异常、技术报错或大面积缺失值表格；这些内容只会进入 `logs/daily.log`。

## 一键安装

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

## 配置推送

```bash
nano .env
```

Telegram：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

飞书：

```env
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
```

不要把任何 Token、Webhook、私钥写进代码或 README。

## 行情源配置

默认配置：

```env
MARKET_DATA_PROVIDER_ORDER=stooq,yfinance,cache
MARKET_REQUEST_DELAY_SEC=1.0
MARKET_RETRY_COUNT=1
MARKET_RETRY_BACKOFF_SEC=4.0
MARKET_CACHE_DIR=data/processed/market_cache
MARKET_CACHE_SNAPSHOT_PATH=data/processed/market_cache.json
MARKET_CACHE_MAX_AGE_HOURS=168
MARKET_CACHE_MAX_TRADING_DAYS=3
MARKET_MIN_SUCCESS_RATIO=0.7
RUN_DAILY_TIMEOUT=20m
```

含义：先尝试 Stooq；如果 Stooq 空数据或网络失败，再尝试 yfinance；两者都失败时才读取本地缓存。旧的 `MARKET_PROVIDER` / `MARKET_PROVIDER_CHAIN` 不会覆盖 `config.yaml` 中的 fallback chain，避免服务器旧 `.env` 把 Stooq fallback 关掉。

当前正式报告只抓 MVP 核心池，约 27 个标的：SPY、QQQ、DIA、IWM、SMH、SOXX、XLK、XLF、XLE、XLV、TLT、GLD、USO，以及 NVDA、MSFT、AAPL、AMZN、GOOGL、META、TSLA、AMD、AVGO、MU、MRVL、ARM、SNOW、NOW。

已预留后续正式行情源配置入口：

```env
TWELVE_DATA_API_KEY=
ALPHA_VANTAGE_API_KEY=
POLYGON_API_KEY=
```

## 行情源诊断

只测试 10 个核心标的，不会请求完整日报股票池：

```bash
cd /opt/us-market-review
source .venv/bin/activate
python -m src.provider_check --provider all --config config.yaml
cat logs/provider_check.log
```

默认测试：SPY、QQQ、DIA、IWM、NVDA、MSFT、AAPL、AMD、AVGO、TSLA。输出会显示每个 provider 的成功数量、失败数量、失败原因，以及每个 ticker 实际请求的 symbol，例如 `AAPL -> aapl.us`。

## 手动测试

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
python -m src.health_check --config config.yaml
python -m src.provider_check --provider stooq --config config.yaml
bash run_daily.sh
tail -n 120 logs/daily.log
```

若行情失败，只会推送失败提示，不生成正式 Markdown/PDF。

## 定时运行

正式日报由服务器 cron 执行，北京时间每周二到周六 7:30：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

`run_daily.sh` 内置 20 分钟超时和锁保护。如果已有任务在运行，本次触发会跳过，并向 Telegram 发送：

```text
us-market-review 正在运行，已跳过本次触发。
```

## 自动部署

`main` 有新提交后，GitHub Actions 只做部署和轻量健康检查：

- 保留服务器上的 `.env`
- `git fetch` / `git reset --hard origin/main`
- `bash scripts/install_ubuntu.sh`
- `python -m src.health_check --config config.yaml`
- `python -m src.provider_check --provider stooq --config config.yaml --tickers SPY,QQQ,DIA,IWM,NVDA`

默认不会运行 `bash run_daily.sh`，避免每次 push 都抓完整行情、加重限流。

如需手动从 GitHub Actions 跑一次正式日报：

1. 打开 Actions → Deploy to Ubuntu Server
2. 点击 `Run workflow`
3. 将 `run_report` 设为 `true`

需要配置 GitHub Repository Secrets：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
```

workflow 已设置并发保护：同一分支新的部署会取消旧部署。

## PDF 排查

PDF 使用 HTML -> PDF 方式生成，优先 `wkhtmltopdf`，并校验文件存在、大小和 `%PDF` 文件头。若 PDF 校验失败，不会推送坏 PDF，只会发送 Markdown。

```bash
which wkhtmltopdf
wkhtmltopdf --version
fc-list | grep -E "WenQuanYi|Noto Sans CJK|DejaVu" | head
bash scripts/install_ubuntu.sh
bash run_daily.sh
tail -n 200 logs/daily.log
```

## 文件结构

```text
us-market-review/
  .env.example
  .gitignore
  .github/workflows/deploy.yml
  README.md
  config.yaml
  requirements.txt
  run_daily.sh
  test_send.py
  scripts/install_ubuntu.sh
  src/
    fetch_market.py
    market_data_provider.py
    provider_check.py
    health_check.py
    indicators.py
    fetch_news.py
    prompt_builder.py
    render_report.py
    send_report.py
```

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。
