#!/usr/bin/env bash
# EPUB → PDF：使用 Calibre ebook-convert 渲染；章节顺序由 EPUB spine 决定。
#
# 依赖（macOS 推荐官方安装包，勿用 brew 公式）：
#   https://calibre-ebook.com/download_osx
#   安装后二进制位于 /Applications/calibre.app/Contents/MacOS/ebook-convert
#   Homebrew 的 ebook-convert 多为符号链接，PDF 渲染阶段易 Segmentation fault。
#
# 默认 A4、适中边距；可通过环境变量覆盖（见 EPUB_PDF_*、CALIBRE_EBOOK_CONVERT）。
#
# macOS：若文件在「下载」等目录且出现 Operation not permitted，多为系统隐私未授权
# 当前终端访问该文件夹；请在「系统设置 → 隐私与安全性」中为 Terminal / iTerm / Cursor
# 打开「完全磁盘访问权限」或「文件与文件夹 → 下载文件夹」，或把 .epub 拷到其它目录再运行。
#
# 用法：
#   ./tools/ebook/epub_to_pdf.sh 书.epub
#   ./tools/ebook/epub_to_pdf.sh 书.epub 输出.pdf

set -euo pipefail

usage() {
  echo "用法: $(basename "$0") <输入.epub> [输出.pdf]" >&2
  echo "若省略输出路径，默认在与输入同目录生成同名 .pdf" >&2
  echo "可选环境变量:" >&2
  echo "  EPUB_PDF_PAPER_SIZE  EPUB_PDF_MARGIN（毫米）" >&2
  echo "  CALIBRE_EBOOK_CONVERT  指定 ebook-convert 绝对路径" >&2
  echo "  QTWEBENGINE_DISABLE_SANDBOX  macOS 默认 1（避免 WebEngine 崩溃）" >&2
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

if [[ "$INPUT" != /* ]]; then
  INPUT="$(cd "$(dirname -- "$INPUT")" && pwd)/$(basename -- "$INPUT")"
fi

if [[ $# -eq 2 ]]; then
  OUTPUT=$2
else
  if [[ "$(printf '%s' "${INPUT##*.}" | tr '[:upper:]' '[:lower:]')" == epub ]]; then
    OUTPUT="${INPUT%.*}.pdf"
  else
    OUTPUT="${INPUT}.pdf"
  fi
fi

OUT_DIR=$(dirname -- "$OUTPUT")
if [[ -n "$OUT_DIR" && "$OUT_DIR" != . ]]; then
  mkdir -p -- "$OUT_DIR"
fi

if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$(cd "$(dirname -- "$OUTPUT")" && pwd)/$(basename -- "$OUTPUT")"
fi

resolve_ebook_convert() {
  if [[ -n "${CALIBRE_EBOOK_CONVERT:-}" && -x "$CALIBRE_EBOOK_CONVERT" ]]; then
    echo "$CALIBRE_EBOOK_CONVERT"
    return 0
  fi

  local candidates=()

  if [[ "$(uname -s)" == Darwin ]]; then
  candidates+=(
      "/Applications/calibre.app/Contents/MacOS/ebook-convert"
    )
    for p in /opt/homebrew/Caskroom/calibre/*/calibre.app/Contents/MacOS/ebook-convert \
      /usr/local/Caskroom/calibre/*/calibre.app/Contents/MacOS/ebook-convert; do
      [[ -x "$p" ]] && candidates+=("$p")
    done
  fi

  if command -v ebook-convert &>/dev/null; then
    local which_bin resolved
    which_bin=$(command -v ebook-convert)
    if command -v python3 &>/dev/null; then
      resolved=$(python3 -c "import os, shutil; p=shutil.which('ebook-convert'); print(os.path.realpath(p) if p else '')" 2>/dev/null || true)
      [[ -n "$resolved" && -x "$resolved" ]] && candidates+=("$resolved")
    fi
    candidates+=("$which_bin")
  fi

  local c
  for c in "${candidates[@]}"; do
    if [[ -x "$c" ]]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

if ! EBOOK_CONVERT=$(resolve_ebook_convert); then
  echo "错误: 未找到 ebook-convert。" >&2
  echo "  macOS 请安装官方 Calibre: https://calibre-ebook.com/download_osx" >&2
  exit 1
fi

if [[ "$(uname -s)" == Darwin ]]; then
  if [[ "$EBOOK_CONVERT" != *"/calibre.app/Contents/MacOS/ebook-convert" ]]; then
    echo "警告: 当前 ebook-convert 不在 calibre.app 内（$EBOOK_CONVERT）。" >&2
    echo "  PDF 转换在 macOS 上可能崩溃；建议安装官方包并设置：" >&2
    echo "  CALIBRE_EBOOK_CONVERT=/Applications/calibre.app/Contents/MacOS/ebook-convert" >&2
  fi
  export QTWEBENGINE_DISABLE_SANDBOX=${QTWEBENGINE_DISABLE_SANDBOX:-1}
fi

PAPER_SIZE=${EPUB_PDF_PAPER_SIZE:-a4}
MARGIN=${EPUB_PDF_MARGIN:-12}
if [[ ! "$MARGIN" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "错误: EPUB_PDF_MARGIN 应为非负数（毫米），当前为「$MARGIN」" >&2
  exit 1
fi
# ebook-convert 的 --pdf-page-margin-* 单位是 pt
MARGIN_PT=$(awk -v mm="$MARGIN" 'BEGIN { printf "%.2f", mm * 72 / 25.4 }')

TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/epub2pdf.XXXXXX")
cleanup() {
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

run_ebook_convert() {
  local embed_fonts=$1
  local log="$TMPDIR/ebook-convert.err"
  local -a cmd=(
    "$EBOOK_CONVERT" "$INPUT" "$OUTPUT"
    --paper-size "$PAPER_SIZE"
    --pdf-page-margin-left "$MARGIN_PT"
    --pdf-page-margin-right "$MARGIN_PT"
    --pdf-page-margin-top "$MARGIN_PT"
    --pdf-page-margin-bottom "$MARGIN_PT"
  )
  if [[ "$embed_fonts" == true ]]; then
    cmd+=(--embed-all-fonts)
  fi
  "${cmd[@]}" 2>"$log"
}

print_convert_failure() {
  local exit_code=$1
  local log="$TMPDIR/ebook-convert.err"
  if [[ -f "$log" ]]; then
    cat "$log" >&2
  fi
  if grep -qi 'operation not permitted' "$log" 2>/dev/null; then
    echo >&2
    echo "提示（macOS）：系统拒绝了读取该文件（常见于「下载」文件夹）。请任选其一：" >&2
    echo "  1) 系统设置 → 隐私与安全性 → 为当前终端应用开启「完全磁盘访问权限」或「文件与文件夹」中的「下载文件夹」；" >&2
    echo "  2) 将 .epub 复制到「文稿」或项目目录等不受限路径后再运行本脚本。" >&2
    return
  fi
  if [[ "$exit_code" -eq 139 || "$exit_code" -eq 134 ]]; then
    echo >&2
    echo "提示（macOS Segmentation fault）：多为 Homebrew 符号链接 + Qt WebEngine 沙箱问题。" >&2
    echo "  请使用官方 Calibre 并显式指定：" >&2
    echo "  CALIBRE_EBOOK_CONVERT=/Applications/calibre.app/Contents/MacOS/ebook-convert \\" >&2
    echo "  QTWEBENGINE_DISABLE_SANDBOX=1 ./tools/ebook/epub_to_pdf.sh …" >&2
  fi
}

echo "使用: $EBOOK_CONVERT"
echo "正在将 EPUB 转换为 PDF（A4，边距 ${MARGIN}mm）…"

set +e
run_ebook_convert true
exit_code=$?
set -e

if [[ $exit_code -ne 0 ]]; then
  echo "嵌入字体转换失败（退出码 $exit_code），尝试不嵌入字体…" >&2
  set +e
  run_ebook_convert false
  exit_code=$?
  set -e
fi

if [[ $exit_code -ne 0 ]]; then
  print_convert_failure "$exit_code"
  exit "$exit_code"
fi

echo "完成: $OUTPUT"
