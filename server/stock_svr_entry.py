"""PyInstaller 진입점 (exe 빌드 전용). 개발 중에는 `python -m stock_svr` 를 사용한다."""
import os
import sys

# 콘솔 없는(windowed) exe 에서는 stdout/stderr 가 None 이라 print/logging 이 실패할 수 있다.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from stock_svr.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
