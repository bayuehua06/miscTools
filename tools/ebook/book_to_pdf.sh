#!/usr/bin/env bash
# 按扩展名将电子书转为 PDF（当前支持 .djvu、.epub）。
#
# 用法：
#   ./tools/ebook/book_to_pdf.sh 书.djvu
#   ./tools/ebook/book_to_pdf.sh 书.epub 输出.pdf

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "用法: $(basename "$0") <输入.djvu|输入.epub> [输出.pdf]" >&2
  exit 1
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
fi

INPUT=$1
EXT=$(printf '%s' "${INPUT##*.}" | tr '[:upper:]' '[:lower:]')

case "$EXT" in
  djvu)
    exec "$SCRIPT_DIR/djvu_to_pdf.sh" "$@"
    ;;
  epub)
    exec "$SCRIPT_DIR/epub_to_pdf.sh" "$@"
    ;;
  *)
    echo "错误: 不支持的格式「.${EXT}」，目前仅支持 .djvu 与 .epub" >&2
    exit 1
esac
