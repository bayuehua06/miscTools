#!/usr/bin/env bash
# DjVu → PDF：先用 DjVuLibre 导出每页 TIFF，再用 ImageMagick 合成单个 PDF。
# 依赖：brew install djvulibre imagemagick
#
# 页序：纯字典序会把 page-1000 排在 page-101 之前；路径里若含「-」也不能用 sort -t-。
# 这里从文件名取出 page-<数字> 做数值排序；输出模板用 %05d，便于人工核对。
#
# macOS：若文件在「下载」等目录且出现 Operation not permitted，多为系统隐私未授权
# 当前终端访问该文件夹；请在「系统设置 → 隐私与安全性」中为 Terminal / iTerm / Cursor
# 打开「完全磁盘访问权限」或「文件与文件夹 → 下载文件夹」，或把 .djvu 拷到其它目录再运行。
#
# 用法：
#   ./tools/ebook/djvu_to_pdf.sh 书.djvu
#   ./tools/ebook/djvu_to_pdf.sh 书.djvu 输出.pdf

set -euo pipefail

usage() {
  echo "用法: $(basename "$0") <输入.djvu> [输出.pdf]" >&2
  echo "若省略输出路径，默认在与输入同目录生成同名 .pdf" >&2
  exit 1
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
fi

INPUT=$1

if [[ ! -f "$INPUT" ]]; then
  echo "错误: 找不到文件: $INPUT" >&2
  exit 1
fi

# 使用绝对路径，避免相对路径歧义；无法解决 macOS 对「下载」等目录的 TCC 限制。
if [[ "$INPUT" != /* ]]; then
  INPUT="$(cd "$(dirname -- "$INPUT")" && pwd)/$(basename -- "$INPUT")"
fi

if [[ $# -eq 2 ]]; then
  OUTPUT=$2
else
  if [[ "$INPUT" == *.djvu ]]; then
    OUTPUT="${INPUT%.djvu}.pdf"
  else
    OUTPUT="${INPUT}.pdf"
  fi
fi

if ! command -v ddjvu &>/dev/null; then
  echo "错误: 未找到 ddjvu。请先安装: brew install djvulibre" >&2
  exit 1
fi

if command -v magick &>/dev/null; then
  IM_CMD=(magick)
elif command -v convert &>/dev/null; then
  IM_CMD=(convert)
else
  echo "错误: 未找到 magick/convert。请先安装: brew install imagemagick" >&2
  exit 1
fi

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/djvu2pdf.XXXXXX")
cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

PATTERN="$TMPDIR/page-%05d.tiff"
echo "正在将 DjVu 渲染为 TIFF（临时目录）…"
if ! ddjvu -format=tiff -eachpage "$INPUT" "$PATTERN" 2>"$TMPDIR/ddjvu.err"; then
  cat "$TMPDIR/ddjvu.err" >&2
  if grep -qi 'operation not permitted' "$TMPDIR/ddjvu.err" 2>/dev/null; then
    echo >&2
    echo "提示（macOS）：系统拒绝了读取该文件（常见于「下载」文件夹）。请任选其一：" >&2
    echo "  1) 系统设置 → 隐私与安全性 → 为当前终端应用开启「完全磁盘访问权限」或「文件与文件夹」中的「下载文件夹」；" >&2
    echo "  2) 将 .djvu 复制到「文稿」或项目目录等不受限路径后再运行本脚本。" >&2
  fi
  exit 1
fi

shopt -s nullglob
PAGES_RAW=("$TMPDIR"/page-*.tiff)
shopt -u nullglob

PAGES=()
if [[ ${#PAGES_RAW[@]} -gt 0 ]]; then
  while IFS= read -r line; do
    [[ -n "$line" ]] && PAGES+=("$line")
  done < <(
    for f in "${PAGES_RAW[@]}"; do
      base=$(basename -- "$f" .tiff)
      num=${base#page-}
      printf '%d\t%s\n' "$((10#$num))" "$f"
    done | sort -n | cut -f2-
  )
fi

if [[ ${#PAGES[@]} -eq 0 ]]; then
  echo "错误: 未生成任何 TIFF，请确认文件是否为有效 DjVu。" >&2
  exit 1
fi

OUT_DIR=$(dirname -- "$OUTPUT")
if [[ -n "$OUT_DIR" && "$OUT_DIR" != . ]]; then
  mkdir -p -- "$OUT_DIR"
fi

echo "正在合成 PDF（${#PAGES[@]} 页）…"
"${IM_CMD[@]}" "${PAGES[@]}" "$OUTPUT"

echo "完成: $OUTPUT"
