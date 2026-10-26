#!/usr/bin/env python3
"""把 CSV 里的 prompt 提交到 OpenAI Batch API，再把结果写成 Markdown。

依赖: 仓库根目录运行 ./install.sh（openai）。密钥只从环境变量 OPENAI_API_KEY 读取。

用法:
  ./misctools promptbatch generate-csv 目录.md -o prompts.csv
  ./misctools promptbatch generate-csv 目录.md --country Afghanistan
  ./misctools promptbatch prompts.csv -o ./out
  ./misctools promptbatch prompts.csv -o ./out --limit 2
  ./misctools promptbatch prompts.csv -o ./out --instructions 协议.md --context 资料.md

批次提交后在 OpenAI 侧排队，最迟 24 小时内完成。电脑可以关机。
再次执行同一条命令时，若批次已完成，就把 Markdown 写到 -o 目录；
有依赖且依赖已经落盘的行，会接着提交下一批。

CSV 列为编号、标题、正文。可选第 4 列 depends_on（或表头「依赖」），
填写所依赖行的编号，多个编号用逗号或分号分开。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_MAX_OUTPUT_TOKENS = 128_000
MAX_ATTEMPTS = 3
MAX_REQUESTS = 50_000
MAX_JSONL_BYTES = 190_000_000
CONFIRM_OVER = 5
MIN_POLL_SECONDS = 15
STATE_VERSION = 1
TERMINAL_BATCH_STATUSES = {"completed", "failed", "expired", "cancelled"}

DEFAULT_SUFFIX = """\
输出要求：
- 只输出最终 Markdown 正文，不要开场白，不要复述任务。
- 不要询问是否切换到 Worker、Work 或任何需要确认的模式，直接完成。
- 信息不足时，在文末以「缺失信息」列出，然后停止。不要中途停下来提问。
"""

HEADER_ALIASES = {
    "id": "id",
    "编号": "id",
    "number": "id",
    "no": "id",
    "title": "title",
    "标题": "title",
    "prompt": "prompt",
    "正文": "prompt",
    "depends_on": "depends_on",
    "依赖": "depends_on",
    "dependency": "depends_on",
}

_INVALID_FILENAME = re.compile(r'[\x00-\x1f\\/:*?"<>|]')


@dataclass
class Row:
    id: str
    title: str
    prompt: str
    depends_on: List[str]
    line_no: int


def die(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)
    raise SystemExit(1)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    die(f"无法把 {path} 解码为 UTF-8 或 GB18030")
    return ""


def canonical_header(cell: str) -> str:
    stripped = cell.strip()
    return HEADER_ALIASES.get(stripped) or HEADER_ALIASES.get(stripped.lower()) or ""


def parse_depends(cell: str) -> List[str]:
    if not cell or not cell.strip():
        return []
    parts = re.split(r"[,，;；]", cell)
    return [part.strip() for part in parts if part.strip()]


def country_key(name: str) -> str:
    text = name.strip()
    if text.startswith("##"):
        text = text[2:].strip()
    return text.casefold()


def parse_catalog_markdown(text: str, country: Optional[str] = None) -> List[Row]:
    """读取「## 国家 + | ID | Title | Prompt |」目录，转成 CSV 行。

    country 给出时，只保留该 ## 标题下的行，大小写不敏感，可带或不带 ##。
    """
    wanted = country_key(country) if country else ""
    rows: List[Row] = []
    errors: List[str] = []
    seen: Dict[str, int] = {}
    current = ""
    matched = False
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("## ") and not line.startswith("###"):
            current = line[3:].strip()
            if wanted and country_key(current) == wanted:
                matched = True
            continue
        if not line.startswith("|"):
            continue
        include = not wanted or country_key(current) == wanted
        if not line.endswith("|"):
            if include:
                errors.append(f"第 {line_no} 行的表格没有以 | 结束")
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 3:
            if include:
                errors.append(f"第 {line_no} 行有 {len(cells)} 列，需要 ID、Title、Prompt 三列")
            continue
        if _catalog_header(cells) or _catalog_separator(cells):
            continue
        if not include:
            continue
        row_id, title, prompt = cells
        if not row_id or not title or not prompt:
            errors.append(f"第 {line_no} 行有空的编号、标题或正文")
            continue
        if row_id in seen:
            errors.append(f"编号 {row_id} 重复：第 {seen[row_id]} 行和第 {line_no} 行")
            continue
        seen[row_id] = line_no
        rows.append(Row(row_id, title, prompt, [], line_no))
    if wanted and not matched:
        die(f"找不到国家「{country.strip()}」。它必须与 Markdown 里的某一行 ## 标题一致，例如 Afghanistan")
    if errors:
        die("\n".join(errors))
    if not rows:
        die("Markdown 里没有可写入 CSV 的表格行")
    return rows


def _catalog_header(cells: Sequence[str]) -> bool:
    lowered = [cell.lower() for cell in cells]
    return lowered == ["id", "title", "prompt"] or list(cells) == ["编号", "标题", "正文"]


def _catalog_separator(cells: Sequence[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def write_prompt_csv(rows: Sequence[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # utf-8-sig 写入 BOM。Excel 靠它识别 UTF-8；没有 BOM 时中文会按系统编码打开而乱码。
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["编号", "标题", "正文"])
        for row in rows:
            writer.writerow([row.id, row.title, row.prompt])
    os.replace(temporary, path)


def parse_csv(text: str) -> List[Row]:
    sample = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(sample))
    rows_raw = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows_raw:
        die("CSV 是空的")

    header_map = [canonical_header(cell) for cell in rows_raw[0]]
    has_header = "id" in header_map and "prompt" in header_map
    if has_header:
        if "title" not in header_map:
            die("CSV 表头需要标题列（title 或 标题）")
        data_rows = rows_raw[1:]
        indexes = {name: header_map.index(name) for name in ("id", "title", "prompt")}
        if "depends_on" in header_map:
            indexes["depends_on"] = header_map.index("depends_on")
        start_line = 2
    else:
        indexes = {"id": 0, "title": 1, "prompt": 2}
        data_rows = rows_raw
        start_line = 1

    parsed: List[Row] = []
    errors: List[str] = []
    seen = set()
    for offset, cells in enumerate(data_rows):
        line_no = start_line + offset
        if not has_header and len(cells) < 3:
            errors.append(f"第 {line_no} 行至少需要 3 列：编号、标题、正文")
            continue

        def cell(name: str) -> str:
            index = indexes.get(name)
            if index is None or index >= len(cells):
                return ""
            return cells[index].strip()

        row_id = cell("id")
        title = cell("title")
        prompt = cell("prompt")
        if has_header:
            depends_on = parse_depends(cell("depends_on"))
        elif len(cells) >= 4:
            depends_on = parse_depends(cells[3])
        else:
            depends_on = []
        if not row_id:
            errors.append(f"第 {line_no} 行缺少编号")
            continue
        if not prompt:
            errors.append(f"第 {line_no} 行（编号 {row_id}）正文是空的")
            continue
        if row_id in seen:
            errors.append(f"第 {line_no} 行编号重复: {row_id}")
            continue
        if row_id in depends_on:
            errors.append(f"编号 {row_id} 不能依赖自己")
            continue
        seen.add(row_id)
        parsed.append(Row(row_id, title, prompt, depends_on, line_no))

    ids = {row.id for row in parsed}
    for row in parsed:
        missing = [dep for dep in row.depends_on if dep not in ids]
        if missing:
            errors.append(f"编号 {row.id} 依赖了不存在的编号: {', '.join(missing)}")
    cycle = find_cycle(parsed)
    if cycle:
        errors.append("依赖成环: " + " → ".join(cycle))
    if errors:
        die("\n".join(errors))
    if not parsed:
        die("CSV 里没有可执行的行")
    return parsed


def find_cycle(rows: Sequence[Row]) -> Optional[List[str]]:
    graph = {row.id: row.depends_on for row in rows}
    color = {row_id: 0 for row_id in graph}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        color[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color[dep] == 1:
                return stack[stack.index(dep) :] + [dep]
            if color[dep] == 0:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for node in graph:
        if color[node] == 0:
            found = visit(node)
            if found:
                return found
    return None


def plan_waves(rows: Sequence[Row], satisfied: Iterable[str]) -> Tuple[List[List[Row]], List[Row]]:
    done = set(satisfied)
    pending = [row for row in rows if row.id not in done]
    waves: List[List[Row]] = []
    while pending:
        wave = [row for row in pending if all(dep in done for dep in row.depends_on)]
        if not wave:
            return waves, pending
        waves.append(wave)
        done.update(row.id for row in wave)
        finished = {row.id for row in wave}
        pending = [row for row in pending if row.id not in finished]
    return waves, []


def output_filename(row_id: str, title: str) -> str:
    def clean(value: str) -> str:
        value = _INVALID_FILENAME.sub(" ", value)
        value = re.sub(r"\s+", " ", value).strip().rstrip(".")
        return value

    ident = clean(row_id) or "untitled"
    heading = clean(title)
    if not heading:
        return _trim_filename(ident, "", ident)
    return _trim_filename(heading, ident, ident)


def _trim_filename(heading: str, row_id: str, fallback_id: str) -> str:
    """文件名为「标题-编号.md」。过长时只截标题，编号留在末尾。"""
    suffix = f"-{row_id}.md" if row_id else ".md"
    name = f"{heading}{suffix}" if heading else f"{fallback_id}.md"
    if len(name.encode("utf-8")) <= 200:
        return name
    budget = 200 - len(suffix.encode("utf-8"))
    if budget < 1 or not heading:
        raw = fallback_id.encode("utf-8")[:196]
        while raw:
            try:
                return raw.decode("utf-8").rstrip() + ".md"
            except UnicodeDecodeError:
                raw = raw[:-1]
        return "untitled.md"
    trimmed = heading.encode("utf-8")[:budget]
    while trimmed:
        try:
            stem = trimmed.decode("utf-8").rstrip(" -.")
            break
        except UnicodeDecodeError:
            trimmed = trimmed[:-1]
    else:
        stem = ""
    if not stem:
        return f"{fallback_id}.md"
    return f"{stem}-{row_id}.md"


def compose_input(
    prompt: str,
    dependencies: Sequence[Tuple[str, str, str]],
    suffix: str,
    contexts: Sequence[Tuple[str, str]] = (),
) -> str:
    parts: List[str] = []
    for name, text in contexts:
        parts.append(f"参考文档（{name}）开始")
        parts.append(text.rstrip())
        parts.append("参考文档结束")
        parts.append("")
    parts.extend([prompt.strip(), ""])
    for dep_id, title, text in dependencies:
        label = f"编号 {dep_id}" + (f"，{title}" if title else "")
        parts.append(f"依赖输出（{label}）开始")
        parts.append(text.rstrip())
        parts.append("依赖输出结束")
        parts.append("")
    if suffix.strip():
        parts.append(suffix.strip())
    return "\n".join(parts).rstrip() + "\n"


def extract_output_text(body: dict) -> str:
    chunks: List[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") == "reasoning":
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    chunks.append(part.get("text") or "")
    text = "\n".join(chunk for chunk in chunks if chunk).strip()
    if text:
        return text
    fallback = body.get("output_text")
    return fallback.strip() if isinstance(fallback, str) else ""


def format_error(err: object) -> str:
    if err is None:
        return "未知错误"
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        message = err.get("message") or err.get("code")
        if message:
            return str(message)
        return json.dumps(err, ensure_ascii=False)
    return str(err)


def interpret_line(line: dict) -> dict:
    """把 Batch 结果里的一行收成可写入状态的字段。"""
    result = {
        "status": "failed",
        "text": None,
        "error": None,
        "input_tokens": None,
        "output_tokens": None,
        "response_id": None,
    }
    if line.get("error"):
        result["error"] = format_error(line.get("error"))
        return result
    response = line.get("response") or {}
    body = response.get("body") or {}
    if not isinstance(body, dict):
        result["error"] = "响应正文不是对象"
        return result
    status_code = response.get("status_code")
    if status_code is not None and int(status_code) != 200:
        result["error"] = format_error(body.get("error") or body) if body else f"HTTP {status_code}"
        if result["error"] == "未知错误":
            result["error"] = f"HTTP {status_code}"
        return result
    if body.get("error"):
        result["error"] = format_error(body.get("error"))
        return result
    text = extract_output_text(body)
    usage = body.get("usage") or {}
    result["text"] = text or None
    result["input_tokens"] = usage.get("input_tokens")
    result["output_tokens"] = usage.get("output_tokens")
    result["response_id"] = body.get("id")
    if not text:
        result["error"] = "响应里没有可保存的文本"
        return result
    details = body.get("incomplete_details") or {}
    incomplete = body.get("status") == "incomplete" or bool(details)
    if incomplete:
        reason = details.get("reason") if isinstance(details, dict) else ""
        result["status"] = "incomplete"
        result["error"] = f"输出不完整：{reason}" if reason else "输出不完整"
        return result
    result["status"] = "done"
    result["error"] = None
    return result


def blank_row(row: Row) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "depends_on": list(row.depends_on),
        "status": "pending",
        "attempts": 0,
        "custom_id": None,
        "batch_id": None,
        "output_name": None,
        "prompt_sha256": None,
        "error": None,
        "input_tokens": None,
        "output_tokens": None,
        "response_id": None,
    }


def merge_state(csv_rows: Sequence[Row], old: Optional[dict], csv_path: Path) -> Tuple[dict, List[str]]:
    notes: List[str] = []
    if old:
        if old.get("version") != STATE_VERSION:
            die(f"输出目录里的进度文件版本是 {old.get('version')}，当前只认识版本 {STATE_VERSION}")
        previous_csv = old.get("csv")
        if previous_csv and Path(previous_csv).resolve() != csv_path.resolve():
            die(f"这个输出目录已经在处理另一个 CSV：{previous_csv}")
    old_rows = (old or {}).get("rows") or {}
    ordered: Dict[str, dict] = {}
    for row in csv_rows:
        prev = old_rows.get(row.id)
        if prev is None:
            current = blank_row(row)
        else:
            current = dict(prev)
            current["title"] = row.title
            current["depends_on"] = list(row.depends_on)
        ordered[row.id] = current
    for row_id, prev in old_rows.items():
        if row_id not in ordered and prev.get("status") == "submitted":
            ordered[row_id] = dict(prev)
            notes.append(f"编号 {row_id} 已不在 CSV 中，但它还在批次里，取回前会保留")
    state = {
        "version": STATE_VERSION,
        "csv": str(csv_path.resolve()),
        "model": (old or {}).get("model") or DEFAULT_MODEL,
        "suffix_sha256": (old or {}).get("suffix_sha256"),
        "rows": ordered,
        "batches": dict((old or {}).get("batches") or {}),
    }
    return state, notes


def apply_retries(state: dict, row_ids: Sequence[str]) -> List[str]:
    notes: List[str] = []
    for row_id in row_ids:
        row = state["rows"].get(row_id)
        if row is None:
            notes.append(f"编号 {row_id} 不在 CSV 里，无法重试")
            continue
        if row["status"] == "submitted":
            notes.append(f"编号 {row_id} 还在批次中，不能重试")
            continue
        if row["status"] == "done":
            notes.append(f"编号 {row_id} 已经完成。若要付费重跑，请先从进度里删掉它")
            continue
        if row["status"] not in {"failed", "incomplete", "pending"}:
            notes.append(f"编号 {row_id} 当前状态是 {row['status']}，不能重试")
            continue
        row["status"] = "pending"
        row["attempts"] = 0
        row["error"] = None
        row["custom_id"] = None
        row["batch_id"] = None
    return notes


def submission_candidates(rows: Sequence[Row], state: dict) -> List[Row]:
    by_id = state["rows"]
    ready: List[Row] = []
    for row in rows:
        current = by_id.get(row.id)
        if current is None or current["status"] in {"done", "submitted", "incomplete"}:
            continue
        if current["status"] == "failed" and int(current.get("attempts") or 0) >= MAX_ATTEMPTS:
            continue
        if all(by_id[dep]["status"] == "done" for dep in row.depends_on):
            ready.append(row)
    return ready


def active_batches(state: dict) -> List[str]:
    found = []
    for batch_id, batch in state.get("batches", {}).items():
        if batch.get("status") not in TERMINAL_BATCH_STATUSES:
            found.append(batch_id)
    return found


def make_custom_id(row_id: str, attempt: int, used: set) -> str:
    digest = hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:12]
    custom_id = f"pb{attempt:02d}{digest}"
    if custom_id not in used:
        used.add(custom_id)
        return custom_id
    suffix = 1
    while True:
        candidate = f"{custom_id}{suffix:x}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        suffix += 1


def build_request(
    custom_id: str,
    model: str,
    text: str,
    max_output_tokens: int,
    reasoning: Optional[str],
    instructions: str = "",
) -> dict:
    body = {
        "model": model,
        "input": text,
        "max_output_tokens": max_output_tokens,
    }
    if instructions.strip():
        body["instructions"] = instructions.strip()
    if reasoning:
        body["reasoning"] = {"effort": reasoning}
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/responses",
        "body": body,
    }


def dependency_texts(row: Row, state: dict, output_dir: Path) -> List[Tuple[str, str, str]]:
    texts = []
    for dep_id in row.depends_on:
        dep = state["rows"][dep_id]
        name = dep.get("output_name")
        if not name:
            die(f"编号 {dep_id} 标记为已完成，但没有输出文件名")
        path = output_dir / name
        if not path.is_file():
            die(f"找不到依赖输出: {path}")
        texts.append((dep_id, dep.get("title") or "", path.read_text(encoding="utf-8")))
    return texts


def composed_text(
    row: Row,
    state: dict,
    output_dir: Path,
    suffix: str,
    contexts: Sequence[Tuple[str, str]] = (),
) -> str:
    return compose_input(row.prompt, dependency_texts(row, state, output_dir), suffix, contexts)


def request_fingerprint(text: str, instructions: str) -> str:
    if instructions.strip():
        return sha256_text(instructions.strip() + "\n" + text)
    return sha256_text(text)


def load_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"进度文件损坏（{path}）：{exc}")
    return None


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_suffix(args: argparse.Namespace) -> str:
    if args.no_suffix:
        return ""
    if args.suffix_file:
        path = args.suffix_file.expanduser().resolve()
        if not path.is_file():
            die(f"找不到后缀文件: {path}")
        return read_text(path).strip() + "\n"
    return DEFAULT_SUFFIX


def load_markdown_file(path: Path, label: str) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        die(f"找不到{label}: {resolved}")
    text = read_text(resolved).strip()
    if not text:
        die(f"{label}是空的: {resolved}")
    return text


def load_shared_materials(
    instruction_path: Optional[Path],
    context_paths: Sequence[Path],
) -> Tuple[str, List[Tuple[str, str]]]:
    instructions = load_markdown_file(instruction_path, "自定义说明") if instruction_path else ""
    contexts: List[Tuple[str, str]] = []
    used_labels = set()
    seen_paths = set()
    for raw in context_paths:
        resolved = raw.expanduser().resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        text = load_markdown_file(resolved, "参考文档")
        label = resolved.name
        if label in used_labels:
            label = f"{resolved.parent.name}/{resolved.name}"
        used_labels.add(label)
        contexts.append((label, text))
    return instructions, contexts


def print_materials(instruction_path: Optional[Path], instructions: str, contexts: Sequence[Tuple[str, str]]) -> None:
    if instruction_path:
        print(f"自定义说明: {instruction_path}（{len(instructions)} 字符），每条请求都会带上")
    if contexts:
        labels = "、".join(name for name, _ in contexts)
        chars = sum(len(text) for _, text in contexts)
        print(f"参考文档: {labels}（合计 {chars} 字符）。每条请求都会带上，输入费用按条数相乘")


def iter_jsonl(text: str) -> List[dict]:
    lines = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError as exc:
            die(f"结果第 {line_no} 行不是 JSON：{exc}")
        if not isinstance(item, dict):
            die(f"结果第 {line_no} 行不是对象")
        lines.append(item)
    return lines


def file_text(client: object, file_id: str) -> str:
    content = client.files.content(file_id)  # type: ignore[attr-defined]
    if hasattr(content, "text"):
        text = content.text
        if isinstance(text, str):
            return text
    data = content.read() if hasattr(content, "read") else content
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    body = text.rstrip() + "\n"
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


def apply_result_to_row(row_state: dict, interpreted: dict, output_dir: Path) -> Optional[Path]:
    row_state["input_tokens"] = interpreted.get("input_tokens")
    row_state["output_tokens"] = interpreted.get("output_tokens")
    row_state["response_id"] = interpreted.get("response_id")
    row_state["error"] = interpreted.get("error")
    row_state["status"] = interpreted["status"]
    if interpreted["status"] in {"done", "incomplete"} and interpreted.get("text"):
        name = output_filename(row_state["id"], row_state.get("title") or "")
        write_markdown(output_dir / name, interpreted["text"])
        row_state["output_name"] = name
        return output_dir / name
    return None


def apply_batch_results(
    state: dict,
    batch_id: str,
    batch_status: str,
    output_lines: Sequence[dict],
    error_lines: Sequence[dict],
    output_dir: Path,
) -> List[Path]:
    by_custom: Dict[str, dict] = {}
    for line in error_lines:
        custom_id = line.get("custom_id")
        if isinstance(custom_id, str):
            by_custom[custom_id] = line
    for line in output_lines:
        custom_id = line.get("custom_id")
        if isinstance(custom_id, str):
            by_custom[custom_id] = line
    batch = state["batches"][batch_id]
    batch["status"] = batch_status
    written: List[Path] = []
    for row_id in batch.get("row_ids") or []:
        row_state = state["rows"].get(row_id)
        if row_state is None or row_state.get("batch_id") != batch_id:
            continue
        line = by_custom.get(row_state.get("custom_id") or "")
        if line is None:
            row_state["status"] = "failed"
            if batch_status == "completed":
                row_state["error"] = "批次已完成，但结果文件里没有这一条"
            else:
                row_state["error"] = f"批次状态为 {batch_status}，没有单条结果"
            continue
        interpreted = interpret_line(line)
        path = apply_result_to_row(row_state, interpreted, output_dir)
        if path is not None:
            written.append(path)
    return written


def dependencies_on_disk(row: Row, state: dict, output_dir: Path) -> bool:
    for dep_id in row.depends_on:
        name = state["rows"][dep_id].get("output_name")
        if not name or not (output_dir / name).is_file():
            return False
    return True


def changed_prompt_notes(
    rows: Sequence[Row],
    state: dict,
    output_dir: Path,
    suffix: str,
    contexts: Sequence[Tuple[str, str]] = (),
    instructions: str = "",
) -> List[str]:
    notes = []
    stored_suffix = state.get("suffix_sha256")
    suffix_changed = bool(stored_suffix) and stored_suffix != sha256_text(suffix)
    for row in rows:
        current = state["rows"].get(row.id)
        if current is None or current["status"] not in {"submitted", "done", "incomplete"}:
            continue
        stored = current.get("prompt_sha256")
        if not stored or not dependencies_on_disk(row, state, output_dir):
            continue
        digest = request_fingerprint(composed_text(row, state, output_dir, suffix, contexts), instructions)
        if digest != stored or suffix_changed:
            notes.append(f"编号 {row.id} 的正文、参考文档、说明或后缀已改变，这一批仍使用提交时的内容")
    return notes


def select_chunk(requests: Sequence[dict]) -> List[dict]:
    chosen: List[dict] = []
    size = 0
    for request in requests:
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        extra = len(encoded) + 1
        if extra > MAX_JSONL_BYTES:
            die(
                f"单条请求有 {extra / (1024 * 1024):.1f} MB，超过批次文件上限。"
                "请缩小 --context 或 --instructions。"
            )
        if chosen and (len(chosen) >= MAX_REQUESTS or size + extra > MAX_JSONL_BYTES):
            break
        chosen.append(request)
        size += extra
    return chosen


def make_client():
    try:
        from openai import OpenAI
    except ImportError:
        die("缺少 openai 包。请在 miscTools 根目录执行 ./install.sh")
    if not os.environ.get("OPENAI_API_KEY"):
        die("未设置 OPENAI_API_KEY。例如：export OPENAI_API_KEY=sk-...")
    return OpenAI()


def call_api(action: str, func: Callable):
    try:
        return func()
    except SystemExit:
        raise
    except Exception as exc:
        die(f"{action}失败：{exc}")


def refresh_batches(client, state: dict, output_dir: Path) -> List[Path]:
    written: List[Path] = []
    for batch_id in list(active_batches(state)):
        batch = call_api("查询批次", lambda batch_id=batch_id: client.batches.retrieve(batch_id))
        status = batch.status
        state["batches"][batch_id]["status"] = status
        counts = getattr(batch, "request_counts", None)
        if counts is not None:
            state["batches"][batch_id]["request_counts"] = {
                "total": getattr(counts, "total", None),
                "completed": getattr(counts, "completed", None),
                "failed": getattr(counts, "failed", None),
            }
        if status not in TERMINAL_BATCH_STATUSES:
            continue
        output_lines: List[dict] = []
        error_lines: List[dict] = []
        output_file_id = getattr(batch, "output_file_id", None)
        error_file_id = getattr(batch, "error_file_id", None)
        if output_file_id:
            output_lines = iter_jsonl(file_text(client, output_file_id))
        if error_file_id:
            error_lines = iter_jsonl(file_text(client, error_file_id))
        written.extend(apply_batch_results(state, batch_id, status, output_lines, error_lines, output_dir))
    return written


def submit_batch(client, state: dict, requests: Sequence[dict], row_ids: Sequence[str], state_path: Path) -> str:
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in requests).encode("utf-8")
    buffer = io.BytesIO(payload)

    def upload():
        return client.files.create(file=("requests.jsonl", buffer, "application/jsonl"), purpose="batch")

    uploaded = call_api("上传批次文件", upload)
    created = call_api(
        "创建批次",
        lambda: client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/responses",
            completion_window="24h",
            metadata={"source": "misctools-promptbatch"},
        ),
    )
    batch_id = created.id
    now = datetime.now(timezone.utc).isoformat()
    state["batches"][batch_id] = {
        "status": created.status,
        "row_ids": list(row_ids),
        "custom_ids": [item["custom_id"] for item in requests],
        "submitted_at": now,
        "input_file_id": uploaded.id,
    }
    mark_submitted(state, row_ids, batch_id)
    save_state(state_path, state)
    print(f"批次已提交: {batch_id}（{len(row_ids)} 条，模型 {state['model']}）")
    return batch_id


def prepare_requests(
    rows: Sequence[Row],
    state: dict,
    output_dir: Path,
    suffix: str,
    model: str,
    max_output_tokens: int,
    reasoning: Optional[str],
    contexts: Sequence[Tuple[str, str]] = (),
    instructions: str = "",
) -> Tuple[List[dict], List[str]]:
    used = {
        row.get("custom_id")
        for row in state["rows"].values()
        if row.get("custom_id")
    }
    requests = []
    row_ids = []
    for row in rows:
        current = state["rows"][row.id]
        attempt = int(current.get("attempts") or 0) + 1
        text = composed_text(row, state, output_dir, suffix, contexts)
        custom_id = make_custom_id(row.id, attempt, used)
        requests.append(build_request(custom_id, model, text, max_output_tokens, reasoning, instructions))
        current["attempts"] = attempt
        current["custom_id"] = custom_id
        current["prompt_sha256"] = request_fingerprint(text, instructions)
        current["error"] = None
        row_ids.append(row.id)
    return requests, row_ids


def mark_submitted(state: dict, row_ids: Sequence[str], batch_id: str) -> None:
    for row_id in row_ids:
        row = state["rows"][row_id]
        row["status"] = "submitted"
        row["batch_id"] = batch_id


def confirm_submit(count: int, model: str, auto_yes: bool) -> bool:
    if count <= CONFIRM_OVER or auto_yes:
        return True
    if not sys.stdin.isatty():
        die(f"本次将提交 {count} 条（超过 {CONFIRM_OVER}）。非交互运行请加上 --yes")
    print(f"即将提交 {count} 条到 OpenAI Batch（模型 {model}）。费用按 Batch 价计，提交后即可关机。")
    try:
        answer = input("继续？[y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes", "是"}:
        print("已取消，未提交。")
        return False
    return True


def format_ids(row_ids: Sequence[str], limit: int = 20) -> str:
    shown = list(row_ids[:limit])
    text = ", ".join(shown)
    if len(row_ids) > limit:
        text += f" …等 {len(row_ids)} 条"
    return text


def blocked_notes(rows: Sequence[Row], state: dict, ready_ids: set) -> List[str]:
    notes = []
    for row in rows:
        if row.id in ready_ids:
            continue
        current = state["rows"][row.id]
        status = current["status"]
        if status in {"done", "submitted"}:
            continue
        unmet = [dep for dep in row.depends_on if state["rows"][dep]["status"] != "done"]
        if status == "incomplete":
            notes.append(f"编号 {row.id} 输出不完整（{current.get('error') or '已写文件'}），不会当作依赖。可用 --retry {row.id} 重跑")
        elif status == "failed" and int(current.get("attempts") or 0) >= MAX_ATTEMPTS:
            notes.append(f"编号 {row.id} 已失败 {current['attempts']} 次，停止自动重试：{current.get('error')}")
        elif unmet:
            notes.append(f"编号 {row.id} 在等 {', '.join(unmet)}")
        elif status == "failed":
            notes.append(f"编号 {row.id} 上次失败，下次提交会重试：{current.get('error')}")
    return notes


def print_token_totals(rows: Sequence[dict]) -> None:
    inputs = [row.get("input_tokens") for row in rows if isinstance(row.get("input_tokens"), int)]
    outputs = [row.get("output_tokens") for row in rows if isinstance(row.get("output_tokens"), int)]
    if inputs or outputs:
        print(f"已取回部分的 token：输入 {sum(inputs)}，输出 {sum(outputs)}")


def print_dry_run(rows: Sequence[Row], state: dict, output_dir: Path, suffix: str, model: str, limit: Optional[int]) -> None:
    in_flight = [row_id for row_id, row in state["rows"].items() if row["status"] == "submitted"]
    assumed_done = {
        row_id
        for row_id, row in state["rows"].items()
        if row["status"] in {"done", "submitted"}
    }
    held = {
        row_id
        for row_id, row in state["rows"].items()
        if row["status"] == "incomplete" or (
            row["status"] == "failed" and int(row.get("attempts") or 0) >= MAX_ATTEMPTS
        )
    }
    schedulable = [row for row in rows if row.id not in assumed_done and row.id not in held]
    waves, stuck = plan_waves(schedulable, assumed_done)
    print(f"CSV {len(rows)} 行，模型 {model}")
    print(f"输出目录: {output_dir}")
    if in_flight:
        print("进行中、尚未取回: " + format_ids(in_flight))
    if not waves and not stuck and not in_flight:
        print("没有待提交的行。")
    for index, wave in enumerate(waves, start=1):
        ids = [row.id for row in wave]
        label = "当前可提交" if index == 1 else "等上一批完成后再提交"
        limited = index == 1 and limit is not None and len(ids) > limit
        if limited:
            ids = ids[:limit]
            print(f"第 {index} 批（{label}，--limit {limit}）: {format_ids(ids)}")
        else:
            print(f"第 {index} 批（{label}）: {format_ids(ids)}")
    if stuck or held:
        print("无法安排: " + format_ids([row.id for row in stuck] + [row_id for row_id in held if row_id not in {row.id for row in stuck}]))
    print("追加的后缀:")
    print(suffix.rstrip() if suffix.strip() else "（无）")
    print("未调用 API。")


def finish_message(state: dict) -> int:
    rows = list(state["rows"].values())
    done = [row for row in rows if row["status"] == "done"]
    incomplete = [row for row in rows if row["status"] == "incomplete"]
    failed = [row for row in rows if row["status"] == "failed"]
    pending = [row for row in rows if row["status"] == "pending"]
    print(f"完成 {len(done)}，不完整 {len(incomplete)}，失败 {len(failed)}，未提交 {len(pending)}")
    print_token_totals(rows)
    if incomplete or failed:
        return 1
    if pending:
        return 0
    print("全部完成。")
    return 0


def build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promptbatch generate-csv",
        description="把按国家分节的 Markdown 目录（## 国家，表头 ID / Title / Prompt）写成 promptbatch 用的 CSV。",
    )
    parser.add_argument("markdown", type=Path, help="目录 Markdown")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出 CSV。默认与 Markdown 同名、扩展名改为 .csv；指定 --country 时默认加上国家名")
    parser.add_argument("--country", default=None, help="只导出这个 ## 标题下的行，例如 Afghanistan。大小写不敏感")
    return parser


def default_csv_path(source: Path, country: Optional[str]) -> Path:
    if not country:
        return source.with_suffix(".csv")
    safe = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', " ", country.strip())
    safe = re.sub(r"\s+", " ", safe).strip()
    if safe.startswith("##"):
        safe = safe[2:].strip()
    return source.with_name(f"{source.stem}-{safe}.csv")


def generate_csv_main(argv: Sequence[str]) -> int:
    args = build_generate_parser().parse_args(argv)
    source = args.markdown.expanduser().resolve()
    if not source.is_file():
        die(f"找不到 Markdown: {source}")
    country = args.country.strip() if args.country else None
    rows = parse_catalog_markdown(read_text(source), country)
    output = args.output.expanduser().resolve() if args.output else default_csv_path(source, country)
    write_prompt_csv(rows, output)
    scope = f"（{country}）" if country else ""
    print(f"已写入 {len(rows)} 行{scope}到 {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promptbatch",
        description="把 CSV 中的 prompt 提交到 OpenAI Batch，完成后把每条结果写成一个 Markdown。提交后可以关机。从目录 Markdown 生成 CSV 用：promptbatch generate-csv 目录.md -o prompts.csv",
    )
    parser.add_argument("csv", type=Path, help="CSV 文件。列：编号、标题、正文；可选依赖列 depends_on / 依赖")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Markdown 输出目录。进度也写在这里的 .promptbatch/")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--suffix-file", type=Path, default=None, help="用这个文件的全文替换默认后缀")
    parser.add_argument("--no-suffix", action="store_true", help="不在正文末尾追加后缀")
    parser.add_argument("--instructions", type=Path, default=None, help="自定义说明 Markdown。每条请求都带上，对应 API 的 instructions")
    parser.add_argument("--context", type=Path, action="append", default=[], help="附加参考文档 Markdown，可重复。跟每条 prompt 一起作为输入")
    parser.add_argument("--limit", type=int, default=None, help="这一次最多提交多少条，便于先试跑")
    parser.add_argument("--yes", action="store_true", help=f"提交超过 {CONFIRM_OVER} 条时不再询问")
    parser.add_argument("--wait", action="store_true", help="提交后保持本机运行，直到这一批完成并写盘。默认提交后立即退出")
    parser.add_argument("--interval", type=int, default=60, help="--wait 时查询间隔秒数（最少 15，默认 60）")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS, help="单条输出 token 上限（默认 128000）")
    parser.add_argument(
        "--reasoning",
        choices=["none", "low", "medium", "high", "xhigh"],
        default=None,
        help="显式设置推理档位。不传则使用模型默认值；gpt-5.6 默认 medium 会把思考 token 计入输出",
    )
    parser.add_argument("--retry", action="append", default=[], metavar="编号", help="把失败或不完整的编号重置为待提交，可重复使用")
    parser.add_argument("--dry-run", action="store_true", help="只检查 CSV、分批和后缀，不调用 API")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "generate-csv":
        return generate_csv_main(args_list[1:])
    args = build_parser().parse_args(args_list)
    if args.limit is not None and args.limit < 1:
        die("--limit 至少为 1")
    if args.max_output_tokens < 1:
        die("--max-output-tokens 至少为 1")
    csv_path = args.csv.expanduser().resolve()
    if not csv_path.is_file():
        die(f"找不到 CSV: {csv_path}")
    output_dir = args.output.expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        die(f"输出路径不是目录: {output_dir}")
    suffix = load_suffix(args)
    instructions, contexts = load_shared_materials(args.instructions, args.context or [])
    print_materials(args.instructions, instructions, contexts)
    rows = parse_csv(read_text(csv_path))
    state_path = output_dir / ".promptbatch" / "state.json"
    state, merge_notes = merge_state(rows, load_state(state_path) if state_path.exists() else None, csv_path)
    for note in merge_notes:
        print(note)
    retry_notes = apply_retries(state, args.retry)
    for note in retry_notes:
        print(note)

    if args.dry_run:
        print_dry_run(rows, state, output_dir, suffix, args.model, args.limit)
        return 0

    client = make_client()
    output_dir.mkdir(parents=True, exist_ok=True)
    for note in changed_prompt_notes(rows, state, output_dir, suffix, contexts, instructions):
        print(note)
    interval = max(args.interval, MIN_POLL_SECONDS)
    if args.wait and args.interval < MIN_POLL_SECONDS:
        print(f"查询间隔已提高到 {MIN_POLL_SECONDS} 秒。")
    auto_yes = args.yes

    while True:
        written = refresh_batches(client, state, output_dir)
        save_state(state_path, state)
        for path in written:
            print(f"已写入 {path}")
        if written:
            print_token_totals(list(state["rows"].values()))

        running = active_batches(state)
        if running:
            counts = state["batches"][running[0]].get("request_counts") or {}
            progress = ""
            if counts.get("total"):
                progress = f"，已完成 {counts.get('completed')}/{counts.get('total')}"
            print(f"批次 {running[0]} 状态 {state['batches'][running[0]]['status']}{progress}。")
            print("处理在 OpenAI 侧进行，最迟 24 小时。这台电脑可以关机。结果文件大约保留 30 天。")
            print("取回时再次运行同一条命令（CSV 和 -o 保持不变）。")
            if not args.wait:
                return 0
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n已停止等待。批次仍在 OpenAI 侧运行，可以关机。再次执行同一命令即可取回。")
                return 0
            continue

        candidates = submission_candidates(rows, state)
        if args.limit is not None:
            candidates = candidates[: args.limit]
        ready_ids = {row.id for row in candidates}
        if not candidates:
            for note in blocked_notes(rows, state, ready_ids):
                print(note)
            code = finish_message(state)
            save_state(state_path, state)
            return code

        print(f"可提交 {len(candidates)} 条（模型 {args.model}）: {format_ids([row.id for row in candidates])}")
        waiting = len(submission_candidates(rows, state)) - len(candidates)
        if args.limit is not None and waiting > 0:
            print(f"因 --limit，还有 {waiting} 条本次不提交。")
        if not confirm_submit(len(candidates), args.model, auto_yes):
            return 0
        if args.wait:
            auto_yes = True

        requests, row_ids = prepare_requests(
            candidates,
            state,
            output_dir,
            suffix,
            args.model,
            args.max_output_tokens,
            args.reasoning,
            contexts,
            instructions,
        )
        chunk = select_chunk(requests)
        if len(chunk) < len(requests):
            dropped = set(item["custom_id"] for item in requests[len(chunk) :])
            row_ids = [row_id for row_id in row_ids if state["rows"][row_id]["custom_id"] not in dropped]
            for row_id in list(state["rows"]):
                row_state = state["rows"][row_id]
                if row_state.get("custom_id") in dropped and row_state["status"] != "submitted":
                    row_state["attempts"] = max(0, int(row_state["attempts"]) - 1)
                    row_state["custom_id"] = None
                    row_state["prompt_sha256"] = None
            print(f"这一批只放入 {len(chunk)} 条，其余等下次再提交。")
        state["model"] = args.model
        state["suffix_sha256"] = sha256_text(suffix)
        batch_id = submit_batch(client, state, chunk, row_ids, state_path)
        print("处理在 OpenAI 侧进行，一般数小时内完成，最迟 24 小时。这台电脑可以关机。")
        print("结果文件大约保留 30 天。取回时再次运行同一条命令（CSV 和 -o 保持不变）。")
        if not args.wait:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已停止等待。批次仍在 OpenAI 侧运行，可以关机。再次执行同一命令即可取回。")
            return 0


if __name__ == "__main__":
    sys.exit(main())
