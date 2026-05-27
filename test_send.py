from __future__ import annotations

import argparse
import sys
from datetime import datetime

from dotenv import load_dotenv

from src.send_report import send_test_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Telegram and Feishu push settings.")
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    load_dotenv()
    message = args.message or f"us-market-review 推送测试成功触发: {datetime.now().astimezone().isoformat(timespec='seconds')}"
    results = send_test_message(message)

    enabled_count = 0
    failed_count = 0
    for result in results:
        if result.enabled:
            enabled_count += 1
        if result.enabled and not result.success:
            failed_count += 1
        status = "ok" if result.success else "skip" if not result.enabled else "failed"
        print(f"{result.channel}: {status} - {result.detail}")

    if enabled_count == 0:
        print("No push channel is enabled. Set ENABLE_TELEGRAM=true or ENABLE_FEISHU=true in .env.")
        return 2
    if failed_count:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
