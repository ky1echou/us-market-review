# us-market-review

`us-market-review` 是部署在 Ubuntu 24.04 云服务器上的中文美股复盘自动化项目。目标不是生成“核心版”或“能跑就行”的简报，而是按 Golden Sample《20260528美股复盘.pdf》的结构、表达方式和 PDF 版式，自动生成每日中文卖方风格美股复盘。

## 正式报告结构

正式 PDF 固定采用以下结构，不删栏目、不用大行情表替代投研判断：

1. 标题：`美股复盘 · YYYY年M月D日`
2. 一、美股指数概况
3. 二、宏观与地缘
4. 三、行业与ETF结构
5. 四、美股科技股跟踪
6. 五、AH盘前参考
7. Update Matrix
8. 最终判断
9. 底部免责声明：仅供研究参考，不构成投资建议。

正文只保留投研内容。完整池成功率、失败 ticker、缓存 ticker、provider chain、HTTP 错误等技术审计，只进入 Telegram 状态和 `logs/daily.log`，不进入正式 PDF 正文。

## 数据源原则

完整股票池保留，不通过删标的绕过数据失败：

- 指数/ETF：`SPY, QQQ, DIA, IWM, SMH, SOXX, XLK, XLF, XLE, XLV, TLT, GLD, USO`
- 大型科技：`NVDA, MSFT, AAPL, AMZN, GOOGL, META, TSLA`
- AI/半导体：`AMD, AVGO, MU, MRVL, ARM`
- AI应用：`SNOW, NOW`
- 宏观/风险资产：`VIX, US10Y, DXY, GLD, USO, CPER, BTCUSD`

默认 provider 顺序与路由：

1. Financial Modeling Prep：适合主流大型科技与部分股票，使用 `FMP_API_KEY`
2. Finnhub：补 ETF、AI 应用、半导体和部分宏观资产，使用 `FINNHUB_API_KEY`
3. Twelve Data：备用正式源，使用 `TWELVE_DATA_API_KEY`
4. yfinance：最后兜底
5. 本地 cache：只做短暂兜底，不能成为正式报告主体

若要完整、稳定、每天准时生成报告，建议使用 FMP / Finnhub / Twelve Data 的正式 API。免费源可能不稳定，但系统不会通过删内容来规避；如果数据不达标，会发 Telegram 根因提示，而不是生成残缺 PDF。

## 质量门槛

正式报告生成条件：

- 完整股票池行情成功率 >= 90%。
- 实时/最近交易日数据占比 >= 70%。
- 缓存占比不能超过 30%。
- 关键指数必须有最近交易日数据：`SPY, QQQ, DIA, IWM, SMH/SOXX, VIX`。
- Magnificent 7 必须完整：`MSFT, AMZN, NVDA, AAPL, GOOGL, META, TSLA`。
- AI/半导体代表尽量完整：`SNOW, NOW, AMD, AVGO, MU, MRVL, ARM`。
- `US10Y` / `DXY` 缺失时，宏观章节降级；`US10Y, DXY, TLT` 同时缺失时阻断正式报告。

不达标时不会生成正式 Markdown/PDF，只发送 Telegram 状态提示。

## 一键安装

在服务器项目目录运行：

```bash
cd /opt/us-market-review
bash scripts/install_ubuntu.sh
```

安装脚本会创建 `.venv`、安装依赖、创建 `logs/`、`reports/`、`data/` 目录，并在没有 `.env` 时复制 `.env.example`。

## 配置 .env

```bash
nano .env
```

至少配置 Telegram 和行情源：

```env
ENABLE_TELEGRAM=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

FMP_API_KEY=
FINNHUB_API_KEY=
TWELVE_DATA_API_KEY=
```

不要把任何 Token、Webhook、API Key、SSH 私钥写进代码或 README。

## 手动测试

```bash
cd /opt/us-market-review
source .venv/bin/activate
python test_send.py
python -m src.health_check --config config.yaml
python -m src.provider_check --provider all --symbols AAPL,MSFT,NVDA --config config.yaml
python -m src.provider_check --provider all --universe --dry-run --config config.yaml
```

首次部署后，建议手动刷新一次行情：

```bash
python -m src.refresh_market_data --config config.yaml
python -m src.data_quality_check --config config.yaml
```

如需预热本地历史缓存：

```bash
python -m src.warm_market_cache --config config.yaml
```

正式生成日报：

```bash
bash run_daily.sh
tail -n 120 logs/daily.log
```

## 定时运行

正式日报由服务器 cron 执行，北京时间每周二到周六 7:30：

```cron
CRON_TZ=Asia/Shanghai
30 7 * * 2-6 cd /opt/us-market-review && bash run_daily.sh >> logs/daily.log 2>&1
```

`run_daily.sh` 会按顺序执行：

1. `python -m src.refresh_market_data --config config.yaml`
2. `python -m src.data_quality_check --config config.yaml`
3. `python -m src.render_report --config config.yaml`

如果已有任务正在运行，本次触发会跳过并发送 Telegram 提示。

## GitHub Actions 自动部署

`main` 有新提交后，GitHub Actions 只部署和做轻量健康检查，不自动跑正式日报。

需要配置 GitHub Repository Secrets：

```text
SERVER_HOST
SERVER_USER
SERVER_SSH_KEY
FMP_API_KEY
FINNHUB_API_KEY
TWELVE_DATA_API_KEY
```

手动触发 workflow 时可以选择：

- `refresh_market_data=true`：只刷新行情，不生成报告。
- `warm_cache=true`：慢速预热本地缓存。
- `run_provider_universe_check=true`：真实请求完整池 provider 检查，会消耗额度。
- `run_report=true`：运行正式日报；脚本会先刷新行情再生成报告。

部署会保留服务器上的 `.env`，不会覆盖 Telegram/飞书配置。

## PDF 排查

PDF 使用固定 HTML/CSS 模板生成，优先 `wkhtmltopdf`，并校验文件存在、大小和 `%PDF` 文件头。若 PDF 失败，不推送坏 PDF，只发送 Markdown。

```bash
which wkhtmltopdf
wkhtmltopdf --version
fc-list | grep -E "WenQuanYi|Noto Sans CJK|DejaVu" | head
bash scripts/install_ubuntu.sh
bash run_daily.sh
tail -n 200 logs/daily.log
```

## Golden Template 测试

项目包含 `docs/golden_template.md` 和 `tests/test_golden_template.py`。测试会检查固定栏目、三问、4.4 AI 催化、Update Matrix、最终判断，以及正文是否混入 HTTP/provider/cache 审计类技术词。

```bash
python tests/test_golden_template.py
```

## 免责声明

本项目自动生成的内容仅用于信息整理和研究复盘，不构成投资建议。
