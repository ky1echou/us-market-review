# us-market-review

`us-market-review` 是一个部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。它会在美股收盘后抓取行情和公开 RSS 新闻，生成 Markdown 与 PDF 报告，并可通过 Telegram Bot 或飞书 Webhook 推送。

当前 MVP 数据来源：

- 行情：Yahoo Finance via `yfinance`
- 新闻：`config.yaml` 中配置的公开 RSS 源
- 配置：`.env` 与 `config.yaml`
- 日志：`logs/daily.log`

项目不会编造关键数字。价格、涨跌幅、MA5/MA20 偏离、RSI 等关键数据都会在报告中保留来源、数据日期和获取时间。行情抓取不足时，报告开头和推送消息会明确提示风险。

## 一键安装

把项目放到服务器目录后，例如 `/opt/us-market-review`，进入项目目录运行：

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

安装脚本会自动完成：

- 检查 Ubuntu 系统；
- 安装 `git`、`python3`、`python3-venv`、`python3-pip`、`curl`；
- 安装 PDF 所需的 `wkhtmltopdf`、`fonts-wqy-zenhei`、`fonts-wqy-microhei`、`fonts-noto-cjk`；
- 创建 `.venv`；
- 安装 `requirements.txt`；
- 创建 `logs/`、`reports/markdown/`、`reports/pdf/`、`data/raw/`、`data/processed/`；
- 如果没有 `.env`，从 `.env.example` 复制一份；
- 检查 `run_daily.sh` 和 `test_send.py`。

## 配置推送

安装完成后编辑 `.env`：

```bash
nano .env
```

Telegram 需要填写：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

飞书 Webhook 需要填写：

```env
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
```

`FEISHU_SECRET` 只有在飞书机器人开启签名校验时才需要填写。不要把任何 Token、Webhook、密钥写进代码或 README。

## 手动测试

先测试 Telegram/飞书推送：

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
```

再手动生成一次报告：

```bash
bash run_daily.sh
```

报告输出位置：

```text
reports/markdown/us-market-review-YYYY-MM-DD.md
reports/pdf/us-market-review-YYYY-MM-DD.pdf
reports/html/us-market-review-YYYY-MM-DD.html
```

查看日志：

```bash
tail -n 100 logs/daily.log
```

## 设置定时运行

目标时间：北京时间每周二到周六早上 7:30。

打开定时任务：

```bash
crontab -e
```

加入：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

保存后查看：

```bash
crontab -l
```

只要云服务器开机并且 cron 正常运行，本地电脑关机也不影响自动生成报告。

## PDF 异常排查

本项目不再使用不兼容的 TTC 字体注册方式生成 PDF。现在会先生成 HTML，再优先用 `wkhtmltopdf` 转换 PDF，并校验：

- PDF 文件必须存在；
- 文件大小不能过小；
- 文件头必须是 `%PDF`。

如果校验失败，程序会删除异常 PDF，不会把坏 PDF 推送到 Telegram，只会发送 Markdown，并提示：

```text
PDF 生成失败，仅发送 Markdown。
```

如果 PDF 仍异常，按顺序检查：

```bash
which wkhtmltopdf
wkhtmltopdf --version
fc-list | grep -E "WenQuanYi|Noto Sans CJK|DejaVu" | head
bash scripts/install_ubuntu.sh
bash run_daily.sh
tail -n 200 logs/daily.log
```

Ubuntu 24.04 推荐字体包已经由安装脚本自动安装：

```bash
sudo apt install -y wkhtmltopdf fonts-wqy-zenhei fonts-wqy-microhei fonts-noto-cjk
```

## 行情限流排查

Yahoo/yfinance 可能出现 `Too Many Requests. Rate limited.`。现在程序已经做了这些保护：

- 不再一次性高频请求全部 ticker；
- 每个 ticker 之间默认等待 `2` 秒；
- 每个 ticker 默认重试 `3` 次；
- 抓取成功后写入 `data/processed/market_cache/`；
- 当实时抓取失败时，优先使用未过期的本地缓存；
- 报告开头显示行情成功数量、失败数量、数据源、数据获取时间；
- 成功率低于 `70%` 时，Telegram/飞书提示“行情数据不完整，请谨慎使用”。

可在 `.env` 调整：

```env
MARKET_PROVIDER=yfinance
MARKET_REQUEST_DELAY_SEC=2.0
MARKET_RETRY_COUNT=3
MARKET_RETRY_BACKOFF_SEC=10.0
MARKET_CACHE_DIR=data/processed/market_cache
MARKET_CACHE_MAX_AGE_HOURS=168
MARKET_MIN_SUCCESS_RATIO=0.7
```

后续如果要切换 Twelve Data、Alpha Vantage、Polygon，可以继续沿用 `MARKET_PROVIDER` 这个配置入口扩展。

## GitHub Actions 自动部署

仓库已包含 `.github/workflows/deploy.yml`。当 `main` 分支有新提交时，GitHub Actions 会通过 SSH 登录服务器，执行：

```bash
cd /opt/us-market-review
cp .env /tmp/us-market-review.env.bak || true
git fetch origin
git reset --hard origin/main
cp /tmp/us-market-review.env.bak .env || true
bash scripts/install_ubuntu.sh
bash run_daily.sh
```

它会保留服务器上的 `.env`，不会覆盖你的 Telegram Token 或飞书 Webhook。

你需要在 GitHub 仓库里配置 3 个 Repository Secrets：

```text
SERVER_HOST      服务器公网 IP 或域名
SERVER_USER      SSH 用户名，例如 ubuntu 或 root
SERVER_SSH_KEY   可以登录服务器的私钥内容
```

位置：GitHub 仓库页面 → Settings → Secrets and variables → Actions → New repository secret。

部署前 workflow 会输出当前 commit hash。部署后会运行一次 `bash run_daily.sh` 验证。如果 PDF 失败或行情成功率低于 70%，Actions 日志会显示 warning，但不会删除服务器文件。

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
    indicators.py
    fetch_news.py
    prompt_builder.py
    render_report.py
    send_report.py
```

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。
<!-- trigger deploy -->
