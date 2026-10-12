# miscTools

个人杂项命令行工具集，统一入口为 `misctools`。

| 命令 | 功能 | 依赖 |
|------|------|------|
| `book2pdf` | 电子书转 PDF，按扩展名自动选择（.epub / .djvu） | 见下两行 |
| `epub2pdf` | EPUB 转 PDF（Calibre 渲染，默认 A4、边距 12mm） | Calibre |
| `djvu2pdf` | DjVu 转 PDF（逐页导出 TIFF 后合成） | DjVuLibre、ImageMagick |
| `md2word` | Markdown 转 APA 7th 学生论文版式 Word | Python 3、python-docx、markdown-it-py |
| `pdf2md` | 书籍 PDF 转 Markdown，自动判断 PDF 类型并识别章节 | Python 3、PyMuPDF；无文字层的扫描件另需 ocrmypdf |
| `doctor` | 检查依赖是否就绪 | - |

## 安装

```bash
git clone <本仓库地址> miscTools
cd miscTools
./install.sh                # Python 依赖装入 .venv，结束后自动运行 doctor
./install.sh --system       # 另外安装 Calibre、DjVuLibre、ImageMagick、ocrmypdf（macOS 用 Homebrew，Linux 用 apt）
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
./misctools pdf2md 书.pdf --analyze   # 先看类型判断与识别出的章节
./misctools pdf2md 书.pdf -o 书.md
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

- 开头与论文标题相同的 `#` 视为论文标题；其余章节标题中最浅的一级映射为 APA 一级标题（Word 标题 1），依次类推到 5 级。例如 `#` 写论文标题时，`## Method` 为一级标题
- 标题为 `References` / `参考文献` 的一节另起一页，条目使用悬挂缩进
- 中文使用宋体（SimSun），西文使用 Times New Roman
- 按 CommonMark 解析（markdown-it-py）：粗体/斜体可嵌套，支持 `_斜体_` 与 `\*` 转义；代码块以等宽字体原样输出
- 列表使用 Word 原生项目符号/编号，保留嵌套层级，有序列表按 Markdown 起始序号连续编号
- GFM 表格（含 `:-:` 等对齐写法，列对齐会保留）前可写 `Table 1` 与斜体表题，表后紧跟以 `*Note.*` 或 `注：` 开头的段落作为表注

完整示例见 [`tools/md2word/examples/apa_sample.md`](tools/md2word/examples/apa_sample.md)。

### pdf2md 的处理方式

先判断 PDF 类型，再决定章节结构从哪里来（`--structure` 可手动指定）：

| PDF 类型 | 判断依据 | 章节来源（auto 时依次尝试） |
|----------|----------|------------------------------|
| 原生文字 | 有文字层且不是整页图片 | 书签 → 书前目录 → 字号/粗体/斜体/居中 → 正文章标记 |
| 扫描件 + OCR 文字层（如 Google Books） | 整页图片、GlyphLessFont 或 OCR 软件生成 | 书前目录 → 字号（Google Books 不用）→ 正文章标记 |
| 扫描件无文字层 | 几乎没有可提取的文字 | 先用 ocrmypdf 做 OCR，再按上一行处理 |

- 标题最多 4 级。老书的书前目录若是「CHAPTER I / (TO 1510) / 标题 / 小节—小节—…」格式，章为一级标题，副标题保留为斜体，正文段首的小节名（如 `Decline of the Moslem.—`）与目录核对后提升为二级标题
- 去掉页眉、页码、印张标记（signature mark）、扫描件插图内的文字与图注、Google Books 声明页；索引（Index）及其后内容不输出
- 重建段落、合并跨行断词（按全书词频决定 `sea-route` 保留连字符还是合并），页脚脚注转为 Markdown 脚注 `[^1]`
- OCR 文字做常见纠错：希腊/西里尔形近字母、标点前多余空格、引号空格、`--` 改为破折号
- 非英文扫描件用 `--ocr-lang` 指定 tesseract 语言，例如 `--ocr-lang chi_sim+eng`（需安装对应语言包，如 apt 的 `tesseract-ocr-chi-sim`、Homebrew 的 `tesseract-lang`）
- PyMuPDF 采用 AGPL-3.0 许可（另有 Artifex 商业授权），个人本地使用不受影响

示例书籍见 [`tools/pdf2md/examples/`](tools/pdf2md/examples/)。

## 目录结构

```
misctools              总入口
install.sh             依赖安装
requirements.txt       Python 依赖
tools/
  ebook/               book_to_pdf.sh、epub_to_pdf.sh、djvu_to_pdf.sh
  md2word/             md_to_apa_word.py（及 .sh 包装）、examples/
  pdf2md/              pdf_to_md.py、examples/
```

各工具脚本也可以直接单独运行，与通过 `misctools` 调用等价。

## 来源

初始脚本来自 chronicleweb 仓库 `scripts/` 目录（提交 `ef35662`）。
