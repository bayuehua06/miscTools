#!/usr/bin/env bash
# Markdown → APA 7th Word 便捷入口。
# 依赖: 仓库根目录运行 ./install.sh（或 pip install -r requirements.txt）
#
# 用法:
#   ./tools/md2word/md_to_apa_word.sh paper.md
#   ./tools/md2word/md_to_apa_word.sh paper.md -o out.docx --author "Jane Smith"

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/../../.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  exec "$VENV_PY" "$SCRIPT_DIR/md_to_apa_word.py" "$@"
fi
exec python3 "$SCRIPT_DIR/md_to_apa_word.py" "$@"
