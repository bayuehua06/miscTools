# miscTools

个人杂项命令行工具集，统一入口为 `misctools`。

| 命令 | 功能 | 依赖 |
|------|------|------|
| `book2pdf` | 电子书转 PDF，按扩展名自动选择（.epub / .djvu） | 见下两行 |
| `epub2pdf` | EPUB 转 PDF（Calibre 渲染，默认 A4、边距 12mm） | Calibre |
| `djvu2pdf` | DjVu 转 PDF（逐页导出 TIFF 后合成） | DjVuLibre、ImageMagick |
| `md2word` | Markdown 转 APA 7th 学生论文版式 Word | Python 3、python-docx、markdown-it-py |
| `pdf2md` | 书籍 PDF 转 Markdown，自动判断 PDF 类型并识别章节 | Python 3、PyMuPDF；无文字层的扫描件另需 ocrmypdf |
| `promptbatch` | 把 CSV 里的 prompt 提交到 OpenAI Batch，再写成 Markdown | Python 3、openai；环境变量 `OPENAI_API_KEY` |
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

统一入口是 `./misctools <命令> ...`。看某一条的全部参数：`./misctools help <命令>`。

`book2pdf`、`epub2pdf`、`djvu2pdf`、`md2word`、`pdf2md` 省略输出路径时，结果写在输入文件同目录、同名换扩展名。`promptbatch` 批量运行必须用 `-o` 指定目录。`generate-csv` 省略 `-o` 时，CSV 与 Markdown 同名、放在同一目录。

### help

列出全部命令，或查看某一个命令的参数。

```bash
./misctools help
./misctools help md2word
./misctools help pdf2md
./misctools help promptbatch
```

### doctor

检查当前环境里各命令依赖是否就绪，不改文件。

```bash
./misctools doctor
```

缺 Python 包时先跑 `./install.sh`。电子书和 OCR 相关项缺了，再跑 `./install.sh --system`。`OPENAI_API_KEY` 没设置时只作提示，不影响其他命令。

### book2pdf

按扩展名把电子书转成 PDF。`.epub` 走 Calibre，`.djvu` 走 DjVuLibre 和 ImageMagick。

```bash
./misctools book2pdf 书.epub
./misctools book2pdf 书.djvu 输出/书.pdf
```

### epub2pdf

用 Calibre 把 EPUB 渲染成 PDF，默认 A4、边距 12mm。

```bash
./misctools epub2pdf 书.epub
./misctools epub2pdf 书.epub 输出/书.pdf
```

可用环境变量：

- `EPUB_PDF_PAPER_SIZE`：纸张，默认 `a4`
- `EPUB_PDF_MARGIN`：边距（毫米），默认 `12`
- `CALIBRE_EBOOK_CONVERT`：指定 `ebook-convert` 的绝对路径
- `QTWEBENGINE_DISABLE_SANDBOX`：macOS 默认 `1`，避免 WebEngine 崩溃

macOS 上请用官方 Calibre.app。文件若在「下载」里出现 Operation not permitted，给当前终端访问该文件夹的权限，或把 EPUB 拷到其他目录再转。

### djvu2pdf

把 DjVu 逐页导出为 TIFF，再合成一个 PDF。

```bash
./misctools djvu2pdf 书.djvu
./misctools djvu2pdf 书.djvu 输出/书.pdf
```

页码按文件名里的数字排序，不会把 `page-1000` 排到 `page-101` 前面。macOS 访问「下载」目录的权限问题和 `epub2pdf` 相同。

### md2word

把 Markdown 转成 APA 7th 学生论文版式的 Word。

```bash
./misctools md2word 论文.md
./misctools md2word 论文.md -o paper.docx
./misctools md2word 论文.md --author "Jane Smith" --title "My Study"
./misctools md2word 论文.md --no-title-page
```

标题页信息可以写在文首 front matter，也可以用参数覆盖：`--title`、`--author`、`--affiliation`、`--course`、`--instructor`、`--date`。`--running-head` 写页眉。

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
- 按 CommonMark 解析：粗体/斜体可嵌套，支持 `_斜体_` 与 `\*` 转义；代码块以等宽字体原样输出
- 列表使用 Word 原生项目符号/编号，保留嵌套层级，有序列表按 Markdown 起始序号连续编号
- GFM 表格（含 `:-:` 等对齐写法）前可写 `Table 1` 与斜体表题，表后紧跟以 `*Note.*` 或 `注：` 开头的段落作为表注

完整示例见 [`tools/md2word/examples/apa_sample.md`](tools/md2word/examples/apa_sample.md)。

### pdf2md

把书籍 PDF 转成 Markdown，并自动判断类型、识别章节。

```bash
./misctools pdf2md 书.pdf --analyze
./misctools pdf2md 书.pdf
./misctools pdf2md 书.pdf -o 书.md
./misctools pdf2md 书.pdf --structure contents
./misctools pdf2md 扫描件.pdf --force-ocr --ocr-lang chi_sim+eng
```

`--analyze` 只打印类型判断和识别出的章节，不写文件。`--structure` 可选 `auto`（默认）、`outline`、`contents`、`fonts`、`pattern`。

| PDF 类型 | 判断依据 | 章节来源（auto 时依次尝试） |
|----------|----------|------------------------------|
| 原生文字 | 有文字层且不是整页图片 | 书签 → 书前目录 → 字号/粗体/斜体/居中 → 正文章标记 |
| 扫描件 + OCR 文字层（如 Google Books） | 整页图片、GlyphLessFont 或 OCR 软件生成 | 书前目录 → 字号（Google Books 不用）→ 正文章标记 |
| 扫描件无文字层 | 几乎没有可提取的文字 | 先用 ocrmypdf 做 OCR，再按上一行处理 |

- 标题最多 4 级。老书的书前目录若是「CHAPTER I / (TO 1510) / 标题 / 小节—小节—…」格式，章为一级标题，副标题保留为斜体，正文段首的小节名（如 `Decline of the Moslem.—`）与目录核对后提升为二级标题
- 去掉页眉、页码、印张标记、扫描件插图内的文字与图注、Google Books 声明页；索引（Index）及其后内容不输出
- 重建段落、合并跨行断词，页脚脚注转为 Markdown 脚注 `[^1]`
- OCR 文字做常见纠错：希腊/西里尔形近字母、标点前多余空格、引号空格、`--` 改为破折号
- 非英文扫描件用 `--ocr-lang` 指定 tesseract 语言，例如 `chi_sim+eng`（需安装对应语言包）
- PyMuPDF 采用 AGPL-3.0 许可（另有 Artifex 商业授权），个人本地使用不受影响

示例见 [`tools/pdf2md/examples/`](tools/pdf2md/examples/)。

### promptbatch

两步：先从目录 Markdown 生成 CSV，再把 CSV 提交到 OpenAI Batch，完成后把每条结果写成一个 Markdown。密钥只从环境变量 `OPENAI_API_KEY` 读取，写在 `~/.zshrc` 即可：

```bash
export OPENAI_API_KEY=sk-...
```

从目录生成 CSV。Markdown 按 `## 国家` 分节，每节一张表，列是 `ID`、`Title`、`Prompt`。国家标题不进 CSV，正文里的交叉编号也不会自动变成依赖。

```bash
./misctools promptbatch generate-csv 目录.md
./misctools promptbatch generate-csv 目录.md -o prompts.csv
./misctools promptbatch generate-csv 目录.md --country Afghanistan
```

`--country` 只保留该 `##` 标题下的行，大小写不敏感，可写成 `Afghanistan` 或 `## Afghanistan`。不写 `-o` 时，指定国家会写成 `目录-Afghanistan.csv`，避免覆盖整本 CSV。写出的 CSV 是带 BOM 的 UTF-8，Excel 才能正确显示中文。

批量提交。CSV 三列是编号、标题、正文，表头可写 `编号,标题,正文` 或 `id,title,prompt`，也可以不写表头。可选第 4 列 `依赖` / `depends_on`，多个编号用逗号或分号分开。没有依赖的行进同一批；有依赖的行要等对方的 Markdown 写盘后，再执行同一条命令提交下一批。

```bash
./misctools promptbatch prompts.csv -o ./batch-out --dry-run
./misctools promptbatch prompts.csv -o ./batch-out --limit 2
./misctools promptbatch prompts.csv -o ./batch-out --instructions 协议.md --context 资料甲.md --context 资料乙.md
./misctools promptbatch prompts.csv -o ./batch-out --model gpt-6-sol --reasoning high
./misctools promptbatch prompts.csv -o ./batch-out
./misctools promptbatch prompts.csv -o ./batch-out --retry A-01
```

第一次执行会上传并创建批次，然后立刻退出。排队在 OpenAI 侧进行，最迟 24 小时，电脑可以关机。再次执行同一条命令：还在跑就只打印状态；已经完成就把每条写成 `{标题}-{编号}.md`。结果在 OpenAI 侧大约保留 30 天。超过 5 条会先询问，跳过询问用 `--yes`。`--wait` 会让本机一直等到写完，平时不用。

省略 `--model` 时用 `gpt-5.4`。ChatGPT 里付费计划的 Medium / High 若选的是 GPT-6，对应 `--model gpt-6-sol --reasoning medium` 或 `--reasoning high`；若仍停在 GPT-5.6 Sol，把模型名换成 `gpt-5.6-sol`。思考档会把思考 token 按输出计价。

每条正文末尾会自动加上一段要求：只输出最终 Markdown、不要开场、不要询问是否切换 Worker / Work。文件由本工具写盘。用 `--suffix-file` 替换这段话，或用 `--no-suffix` 关掉。`--instructions` 是自定义说明，每条都放进 API 的 instructions。`--context` 是附加文档，可重复，跟该条 prompt 一起作为输入，顺序与参数顺序一致；这些材料按条数重复计费。`--max-output-tokens` 默认 128000。失败的行下次自动重试，最多 3 次；输出被截断时用 `--retry 编号`。进度在输出目录的 `.promptbatch/state.json`。同一批取回时不用再带这些文件；提交下一批时要带上同一组参数。

示例 CSV 见 [`tools/promptbatch/examples/prompts.csv`](tools/promptbatch/examples/prompts.csv)。

## 目录结构

```
misctools              总入口
install.sh             依赖安装
requirements.txt       Python 依赖
tools/
  ebook/               book_to_pdf.sh、epub_to_pdf.sh、djvu_to_pdf.sh
  md2word/             md_to_apa_word.py（及 .sh 包装）、examples/
  pdf2md/              pdf_to_md.py、examples/
  promptbatch/         prompt_batch.py、examples/
```

各工具脚本也可以直接单独运行，与通过 `misctools` 调用等价。

## 来源

初始脚本来自 chronicleweb 仓库 `scripts/` 目录（提交 `ef35662`）。
