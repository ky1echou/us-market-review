# us-market-review

`us-market-review` 是一个部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。它会在美股收盘后抓取行情和公开 RSS 新闻，生成卖方投研日评风格的 Markdown 与 PDF 报告，并可通过 Telegram Bot 或飞书 Webhook 推送。

当前 MVP 数据来源：

- 行情：优先 Yahoo Finance via `yfinance`，失败后自动切换 Stooq daily CSV，再失败才使用本地缓存
- 新闻：`config.yaml` 中配置的公开 RSS 源，并使用主题相关性评分筛选
- 配置：`.env` 与 `config.yaml`
- 日志：`logs/daily.log`

项目不会编造关键数字。价格、涨跌幅、MA5/MA20 偏离、RSI 等关键数据均来自行情源；新闻只作为催化验证，不替代行情数据。若实时行情成功率低于 70%，系统不会生成正式 PDF，只会推送：

```text
今日行情数据抓取失败，未生成正式美股复盘，请检查数据源。
```

## 一键安装

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

安装脚本会自动安装 Python、虚拟环境、依赖、`wkhtmltopdf` 和中文字体，并创建 `logs/`、`reports/`、`data/` 等目录。如果没有 `.env`，会从 `.env.example` 复制一份。

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

默认配置已足够运行：

```env
MARKET_PROVIDER_CHAIN=yfinance,stooq
MARKET_REQUEST_DELAY_SEC=1.0
MARKET_RETRY_COUNT=1
MARKET_RETRY_BACKOFF_SEC=4.0
MARKET_CACHE_DIR=data/processed/market_cache
MARKET_CACHE_MAX_AGE_HOURS=168
MARKET_MIN_SUCCESS_RATIO=0.7
```

含义：先尝试 yfinance；如果遇到 Yahoo 限流或空数据，自动尝试 Stooq；两者都失败时才读取本地缓存。缓存可帮助排查，但实时行情成功率低于 70% 时仍会停止正式报告生成，避免出现整页 N/A 或 Too Many Requests 正文。

后续可在 `src/market_data_provider.py` 中继续接入 Twelve Data、Alpha Vantage、Polygon。

## 手动测试

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
bash run_daily.sh
tail -n 120 logs/daily.log
```

正常情况下会生成：

```text
reports/markdown/us-market-review-YYYY-MM-DD.md
reports/pdf/us-market-review-YYYY-MM-DD.pdf
reports/html/us-market-review-YYYY-MM-DD.html
```

若行情失败，只会推送失败提示，不生成正式 PDF。

## 定时运行

```bash
crontab -e
```

加入：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

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

## 自动部署

仓库包含 `.github/workflows/deploy.yml`。`main` 有新提交后，GitHub Actions 会 SSH 到服务器，保留 `.env`，拉取最新代码，运行安装脚本，再执行一次 `bash run_daily.sh` 验证。

需要配置 GitHub Repository Secrets：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
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
    indicators.py
    fetch_news.py
    prompt_builder.py
    render_report.py
    send_report.py
```

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。
