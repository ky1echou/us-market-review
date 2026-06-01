# us-market-review

`us-market-review` 是部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。它会在美股收盘后抓取行情和公开 RSS 新闻，生成卖方投研日评风格的 Markdown 与 PDF 报告，并可通过 Telegram Bot 或飞书 Webhook 推送。

当前行情源顺序：

1. Financial Modeling Prep（FMP，需要 `FMP_API_KEY`）
2. Stooq daily CSV
3. Yahoo Finance via `yfinance`
4. 本地缓存

如果没有配置 `FMP_API_KEY`，程序会自动跳过 FMP，不会报错。行情可用率低于 70% 时，系统不会生成正式 Markdown/PDF，也不会推送空报告，只会向 Telegram 发送状态提示。

## 一键安装

在服务器项目目录运行：

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

## 配置推送和行情源

打开 `.env`：

```bash
nano .env
```

Telegram：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

FMP 行情源：

```env
FMP_API_KEY=
MARKET_DATA_PROVIDER_ORDER=fmp,stooq,yfinance,cache
```

飞书可选：

```env
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
```

不要把任何 Token、Webhook、API Key、SSH 私钥写进代码或 README。

## 行情源诊断

只测试 10 个核心标的，不会请求完整日报股票池：

```bash
cd /opt/us-market-review
source .venv/bin/activate
python -m src.provider_check --provider fmp --config config.yaml
python -m src.provider_check --provider all --config config.yaml
cat logs/provider_check.log
```

默认测试：SPY、QQQ、DIA、IWM、NVDA、MSFT、AAPL、AMD、AVGO、TSLA。输出会显示每个 provider 是否启用、成功数量、失败数量、失败原因，以及每个 ticker 实际请求使用的 symbol。诊断日志不会打印 `FMP_API_KEY`。

## 手动测试

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
python -m src.health_check --config config.yaml
python -m src.provider_check --provider fmp --config config.yaml
bash run_daily.sh
tail -n 120 logs/daily.log
```

如果行情失败，系统只推送失败提示，不生成正式 Markdown/PDF。

## 定时运行

正式日报由服务器 cron 执行，北京时间每周二到周六 7:30：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

`run_daily.sh` 内置 20 分钟超时和锁保护。如果已有任务正在运行，本次触发会跳过，并向 Telegram 发送提示。

## 自动部署

`main` 有新提交后，GitHub Actions 只做部署和轻量健康检查，不会自动运行正式日报，避免每次 push 都抓完整行情。

需要配置 GitHub Repository Secrets：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
FMP_API_KEY
```

部署时会保留服务器上的 `.env`，并只更新或追加 `.env` 里的 `FMP_API_KEY` 这一行，不会覆盖 Telegram/飞书配置。

如需手动从 GitHub Actions 跑一次正式日报：

1. 打开 Actions -> Deploy to Ubuntu Server
2. 点击 `Run workflow`
3. 将 `run_report` 设为 `true`

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
