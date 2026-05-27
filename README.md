# us-market-review

`us-market-review` 是一个部署在云服务器上的中文美股复盘自动化项目。它会在美股收盘后抓取行情和公开 RSS 新闻，生成 Markdown 与 PDF 报告，并可通过 Telegram Bot 或飞书 Webhook 推送。

当前 MVP 数据来源：

- 行情：Yahoo Finance via `yfinance`
- 新闻：`config.yaml` 中配置的公开 RSS 源
- 配置：`.env` 与 `config.yaml`
- 日志：`logs/daily.log`

项目不会编造关键数字。价格、涨跌幅、MA5/MA20 偏离、RSI 等关键数据都会在报告中保留来源、数据日期和获取时间。没有匹配到新闻证据的异动原因会明确写为“未在 RSS 中匹配到明确原因”。

## 文件结构

```text
us-market-review/
  README.md
  requirements.txt
  .env.example
  config.yaml
  run_daily.sh
  test_send.py
  src/
    fetch_market.py
    indicators.py
    fetch_news.py
    prompt_builder.py
    render_report.py
    send_report.py
```

## Ubuntu Server 24.04 部署

以下步骤以项目部署在 `/opt/us-market-review` 为例。部署在云服务器后，任务由服务器 cron 执行，不依赖你的本地电脑，本地电脑关机也不会影响每日运行。

### 1. 安装 Python 环境

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ca-certificates curl fonts-noto-cjk
```

确认 Python 版本：

```bash
python3 --version
```

Ubuntu Server 24.04 默认 Python 版本满足本项目运行要求。

### 2. 放置项目代码

如果你已经把项目上传到服务器，例如 `/opt/us-market-review`：

```bash
cd /opt/us-market-review
```

如果目录还不存在：

```bash
sudo mkdir -p /opt/us-market-review
sudo chown "$USER:$USER" /opt/us-market-review
```

然后把本项目文件上传到该目录。

### 3. 创建 venv

```bash
cd /opt/us-market-review
python3 -m venv .venv
source .venv/bin/activate
```

### 4. 安装 requirements.txt

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5. 配置 .env

```bash
cp .env.example .env
nano .env
```

建议保留这些基础配置：

```env
APP_TIMEZONE=Asia/Shanghai
CONFIG_PATH=config.yaml
OUTPUT_DIR=reports
LOG_FILE=logs/daily.log
REPORT_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
ENABLE_PDF=true
```

Telegram 推送配置：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_SEND_MARKDOWN=true
TELEGRAM_SEND_PDF=true
```

飞书 Webhook 推送配置：

```env
ENABLE_FEISHU=true
FEISHU_WEBHOOK_URL=
FEISHU_SECRET=
```

如果飞书机器人开启了签名校验，把签名密钥填入 `FEISHU_SECRET`。所有密钥都只写在 `.env`，不要写进代码。

行情、ETF、股票池、RSS 源、AH 映射都在 `config.yaml` 中维护，不需要编辑复杂 JSON。

## 手动测试

### 1. 测试推送

先测试 Telegram/飞书是否配置正确：

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
```

你也可以自定义测试消息：

```bash
python test_send.py --message "us-market-review 云服务器推送测试"
```

返回示例：

```text
telegram: ok - message sent
feishu: ok - message sent
```

如果某个渠道没有启用，会显示 `skip - disabled`。

### 2. 测试行情和新闻抓取

```bash
python -m src.fetch_market --config config.yaml --output /tmp/us-market-snapshot.json
python -m src.fetch_news --config config.yaml --output /tmp/us-market-news.json
```

### 3. 手动生成报告

```bash
bash run_daily.sh
```

生成结果位于：

```text
reports/YYYY-MM-DD/us-market-review-YYYY-MM-DD.md
reports/YYYY-MM-DD/us-market-review-YYYY-MM-DD.pdf
```

同时会生成一个 HTML 版本，方便排查排版：

```text
reports/YYYY-MM-DD/us-market-review-YYYY-MM-DD.html
```

## 设置 Linux cron

目标时间：北京时间每周二到周六早上 7:30。

先确认服务器时区为北京时间：

```bash
timedatectl
sudo timedatectl set-timezone Asia/Shanghai
```

编辑当前用户的 crontab：

```bash
crontab -e
```

加入这一行：

```cron
30 7 * * 2-6 cd /opt/us-market-review && /bin/bash /opt/us-market-review/run_daily.sh
```

保存后查看：

```bash
crontab -l
```

说明：cron 会在云服务器上运行，因此不依赖你的本地电脑。只要云服务器正常开机、网络可用、cron 服务运行，报告就会按时生成。

确认 cron 服务状态：

```bash
systemctl status cron
```

如果未运行：

```bash
sudo systemctl enable --now cron
```

## 查看日志

每次运行都会写入：

```text
logs/daily.log
```

实时查看：

```bash
tail -f /opt/us-market-review/logs/daily.log
```

查看最近 200 行：

```bash
tail -n 200 /opt/us-market-review/logs/daily.log
```

日志会记录：

- 开始时间
- 行情和新闻数据获取结果
- Markdown/PDF 报告生成路径
- Telegram、飞书、邮件推送结果
- 失败原因和异常堆栈

## 常见问题

### PDF 中文显示异常

确认安装了中文字体：

```bash
sudo apt install -y fonts-noto-cjk
ls /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

然后在 `.env` 中设置：

```env
REPORT_FONT_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

### Telegram 推送失败

检查：

- `ENABLE_TELEGRAM=true`
- `TELEGRAM_BOT_TOKEN` 是否正确
- `TELEGRAM_CHAT_ID` 是否正确
- 服务器是否能访问 `https://api.telegram.org`

运行：

```bash
python test_send.py
```

### 飞书推送失败

检查：

- `ENABLE_FEISHU=true`
- `FEISHU_WEBHOOK_URL` 是否正确
- 如果机器人启用了签名校验，`FEISHU_SECRET` 是否正确

运行：

```bash
python test_send.py
```

### cron 没有触发

检查：

```bash
crontab -l
systemctl status cron
tail -n 200 /opt/us-market-review/logs/daily.log
```

也可以临时把 cron 时间改成几分钟后测试，确认能自动写入日志。

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。美股盘后财报、宏观数据、监管新闻和流动性变化可能改变结论。
