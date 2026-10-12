#!/usr/bin/env bash
# miscTools 依赖安装。
#
# 默认：在仓库内创建 .venv 并安装 requirements.txt，随后运行 doctor 检查。
#
# 用法：
#   ./install.sh                 # Python 依赖装入 .venv
#   ./install.sh --system        # 另外安装系统工具（macOS: Homebrew；Linux: apt）
#   ./install.sh --no-venv       # 不建 .venv，装到当前 python3 的用户目录（pip --user）
#   ./install.sh --link [目录]   # 把 misctools 软链接到目录（默认 ~/.local/bin）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT/.venv"

WANT_SYSTEM=false
USE_VENV=true
LINK_DIR=""

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --system) WANT_SYSTEM=true ;;
    --no-venv) USE_VENV=false ;;
    --link)
      if [[ $# -ge 2 && "$2" != --* ]]; then
        LINK_DIR=$2
        shift
      else
        LINK_DIR="$HOME/.local/bin"
      fi
      ;;
    -h|--help) usage 0 ;;
    *)
      echo "错误: 未知参数: $1" >&2
      usage 1
      ;;
  esac
  shift
done

install_system_deps() {
  case "$(uname -s)" in
    Darwin)
      if ! command -v brew &>/dev/null; then
        echo "错误: 未找到 Homebrew，请先安装: https://brew.sh" >&2
        return 1
      fi
      echo "==> 安装系统工具（Homebrew）：djvulibre imagemagick"
      brew install djvulibre imagemagick
      # Calibre 用 cask（官方 .app）；brew 公式版在 macOS 上 PDF 渲染易崩溃
      if [[ -x /Applications/calibre.app/Contents/MacOS/ebook-convert ]]; then
        echo "==> 已检测到 /Applications/calibre.app，跳过 Calibre"
      else
        echo "==> 安装 Calibre（brew --cask）"
        brew install --cask calibre
      fi
      ;;
    Linux)
      if ! command -v apt-get &>/dev/null; then
        echo "错误: 仅支持 apt 系发行版自动安装；请手动安装 calibre、djvulibre、imagemagick" >&2
        return 1
      fi
      local sudo=()
      [[ $(id -u) -ne 0 ]] && sudo=(sudo)
      echo "==> 安装系统工具（apt）：calibre djvulibre-bin imagemagick"
      "${sudo[@]}" apt-get update
      "${sudo[@]}" apt-get install -y calibre djvulibre-bin imagemagick
      ;;
    *)
      echo "错误: 不支持的系统 $(uname -s)，请手动安装 calibre、djvulibre、imagemagick" >&2
      return 1
      ;;
  esac
}

install_python_deps() {
  if ! command -v python3 &>/dev/null; then
    echo "错误: 未找到 python3" >&2
    return 1
  fi
  if [[ "$USE_VENV" == true ]]; then
    if ! "$VENV_DIR/bin/python" -m pip --version &>/dev/null; then
      rm -rf -- "$VENV_DIR"
      echo "==> 创建虚拟环境: $VENV_DIR"
      if ! python3 -m venv "$VENV_DIR"; then
        # 失败的 venv 会留下可执行但无 pip 的 python，入口会误用它
        rm -rf -- "$VENV_DIR"
        echo "错误: 创建 venv 失败（Debian/Ubuntu 需先 apt install python3-venv），或改用 --no-venv" >&2
        return 1
      fi
    fi
    echo "==> 安装 Python 依赖到 .venv"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV_DIR/bin/python" -m pip install -r "$ROOT/requirements.txt"
  else
    echo "==> 安装 Python 依赖到用户目录（pip --user）"
    if ! python3 -m pip install --user -r "$ROOT/requirements.txt"; then
      echo "错误: pip 安装失败；若提示 externally-managed-environment，请去掉 --no-venv 使用默认的 .venv" >&2
      return 1
    fi
  fi
}

link_entry() {
  mkdir -p -- "$LINK_DIR"
  ln -sf "$ROOT/misctools" "$LINK_DIR/misctools"
  echo "==> 已链接: $LINK_DIR/misctools -> $ROOT/misctools"
  case ":$PATH:" in
    *":$LINK_DIR:"*) ;;
    *) echo "提示: $LINK_DIR 不在 PATH 中，请把它加入 shell 配置（如 ~/.zshrc）" ;;
  esac
}

if [[ "$WANT_SYSTEM" == true ]]; then
  install_system_deps
fi
install_python_deps
if [[ -n "$LINK_DIR" ]]; then
  link_entry
fi

echo
"$ROOT/misctools" doctor || true
