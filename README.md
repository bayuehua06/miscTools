# miscTools

个人杂项命令行工具集，统一入口为 `misctools`。

| 命令 | 功能 | 依赖 |
|------|------|------|
| `book2pdf` | 电子书转 PDF，按扩展名自动选择（.epub / .djvu） | 见下两行 |
| `epub2pdf` | EPUB 转 PDF（Calibre 渲染，默认 A4、边距 12mm） | Calibre |
| `djvu2pdf` | DjVu 转 PDF（逐页导出 TIFF 后合成） | DjVuLibre、ImageMagick |
| `md2word` | Markdown 转 APA 7th 学生论文版式 Word | Python 3、python-docx |
| `doctor` | 检查依赖是否就绪 | - |

## 安装

```bash
git clone <本仓库地址> miscTools
cd miscTools
./install.sh                # Python 依赖装入 .venv，结束后自动运行 doctor
./install.sh --system       # 另外安装 Calibre、DjVuLibre、ImageMagick（macOS 用 Homebrew，Linux 用 apt）
./install.sh --link         # 把 misctools 软链接到 ~/.local/bin（可跟目录参数）
```

参数可组合，例如 `./install.sh --system --link`。不想使用虚拟环境时加 `--no-venv`，依赖会装到当前 `python3` 的用户目录。

macOS 上 Calibre 请使用官方 .app（`--system` 会用 `brew install --cask calibre` 安装）；Homebrew 公式版的 `ebook-convert` 在 PDF 渲染阶段容易崩溃。

## 使用

```bash
./misctools help                 # 全部命令
./misctools help md2word         # 单个命令的参数

./misctools book2pdf 书.epub
./misctools djvu2pdf 书.djvu 输出/书.pdf
./misctools md2word tools/md2word/examples/apa_sample.md -o paper.docx --author "Jane Smith"
```

未指定输出路径时，结果写在输入文件同目录、同名不同扩展名。

### epub2pdf 环境变量

- `EPUB_PDF_PAPER_SIZE`：纸张，默认 `a4`
- `EPUB_PDF_MARGIN`：边距（毫米），默认 `12`
- `CALIBRE_EBOOK_CONVERT`：指定 `ebook-convert` 的绝对路径
- `QTWEBENGINE_DISABLE_SANDBOX`：macOS 默认 `1`，避免 WebEngine 崩溃

### md2word 的 Markdown 约定

开头可写 front matter，用于生成标题页：

```markdown
---
title: The Impact of Sleep on Memory
author: Jane Smith
affiliation: University of Example
course: PSY 101: Introduction to Psychology
instructor: Dr. John Doe
date: October 12, 2026
---
```

- `#`–`#####` 映射为 Word 标题 1–5
- 标题为 `References` / `参考文献` 的一节使用悬挂缩进
- GFM 表格前可写 `Table 1` 与斜体表题，表后可写 `*Note.* ...` 表注

完整示例见 [`tools/md2word/examples/apa_sample.md`](tools/md2word/examples/apa_sample.md)。

## 目录结构

```
misctools              总入口
install.sh             依赖安装
requirements.txt       Python 依赖
tools/
  ebook/               book_to_pdf.sh、epub_to_pdf.sh、djvu_to_pdf.sh
  md2word/             md_to_apa_word.py（及 .sh 包装）、examples/
```

各工具脚本也可以直接单独运行，与通过 `misctools` 调用等价。

## 来源

初始脚本来自 chronicleweb 仓库 `scripts/` 目录（提交 `ef35662`）。
