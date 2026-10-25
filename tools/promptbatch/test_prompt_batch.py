#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompt_batch as pb


CATALOG = """# 目录

## Afghanistan

| ID | Title | Prompt |
|---|---|---|
| A-01 | Afghanistan — Early | 研究甲，与A-32交叉。正文含,逗号和"引号"。 |
| A-32 | Afghanistan — Later | 研究乙。 |

## Albania

| ID | Title | Prompt |
| --- | --- | --- |
| A-02 | Albania — Ottoman | 研究丙。 |
"""


class CatalogTests(unittest.TestCase):
    def test_catalog_roundtrip(self):
        rows = pb.parse_catalog_markdown(CATALOG)
        self.assertEqual([row.id for row in rows], ["A-01", "A-32", "A-02"])
        self.assertIn("与A-32交叉", rows[0].prompt)
        self.assertEqual(rows[0].depends_on, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            pb.write_prompt_csv(rows, path)
            parsed = pb.parse_csv(path.read_text(encoding="utf-8"))
        self.assertEqual([(row.id, row.prompt) for row in parsed], [(row.id, row.prompt) for row in rows])

    def test_country_filter(self):
        rows = pb.parse_catalog_markdown(CATALOG, "afghanistan")
        self.assertEqual([row.id for row in rows], ["A-01", "A-32"])
        rows = pb.parse_catalog_markdown(CATALOG, "## Albania")
        self.assertEqual([row.id for row in rows], ["A-02"])

    def test_unknown_country(self):
        with self.assertRaises(SystemExit):
            pb.parse_catalog_markdown(CATALOG, "Narnia")

    def test_duplicate_id(self):
        text = CATALOG + "| A-01 | 重复 | 另一段。 |\n"
        with self.assertRaises(SystemExit):
            pb.parse_catalog_markdown(text)


class ParseTests(unittest.TestCase):
    def test_chinese_header_and_dependency(self):
        rows = pb.parse_csv("编号,标题,正文,依赖\n1,甲,第一段,\n2,乙,第二段,1\n")
        self.assertEqual([row.id for row in rows], ["1", "2"])
        self.assertEqual(rows[1].depends_on, ["1"])
        self.assertEqual(rows[0].title, "甲")

    def test_no_header(self):
        rows = pb.parse_csv('10,标题甲,"第一行\n第二行"\n11,标题乙,独立\n')
        self.assertEqual(rows[0].prompt, "第一行\n第二行")
        self.assertEqual(rows[1].depends_on, [])

    def test_multiple_dependencies(self):
        rows = pb.parse_csv("id,title,prompt,depends_on\na,A,pa,\nb,B,pb,a；c\nc,C,pc,\n")
        self.assertEqual(rows[1].depends_on, ["a", "c"])

    def test_cycle(self):
        with self.assertRaises(SystemExit):
            pb.parse_csv("id,title,prompt,depends_on\na,A,pa,b\nb,B,pb,a\n")

    def test_missing_dependency(self):
        with self.assertRaises(SystemExit):
            pb.parse_csv("id,title,prompt,depends_on\na,A,pa,missing\n")


class PlanTests(unittest.TestCase):
    def waves(self, text):
        rows = pb.parse_csv(text)
        return pb.plan_waves(rows, set())

    def test_independent_then_child(self):
        waves, stuck = self.waves("id,title,prompt,depends_on\n1,A,pa,\n2,B,pb,\n3,C,pc,1\n")
        self.assertEqual([[row.id for row in wave] for wave in waves], [["1", "2"], ["3"]])
        self.assertEqual(stuck, [])

    def test_candidates_wait_for_done(self):
        rows = pb.parse_csv("id,title,prompt,depends_on\n1,A,pa,\n2,B,pb,1\n")
        state, _ = pb.merge_state(rows, None, Path("/tmp/prompts.csv"))
        self.assertEqual([row.id for row in pb.submission_candidates(rows, state)], ["1"])
        state["rows"]["1"]["status"] = "done"
        self.assertEqual([row.id for row in pb.submission_candidates(rows, state)], ["2"])
        state["rows"]["1"]["status"] = "incomplete"
        self.assertEqual(pb.submission_candidates(rows, state), [])


class OutputTests(unittest.TestCase):
    def test_extract_message_text(self):
        body = {
            "output": [
                {"type": "reasoning", "content": [{"type": "output_text", "text": "secret"}]},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "# 标题\n\n正文"}],
                },
            ]
        }
        self.assertEqual(pb.extract_output_text(body), "# 标题\n\n正文")

    def test_interpret_incomplete(self):
        line = {
            "custom_id": "pb011234",
            "response": {
                "status_code": 200,
                "body": {
                    "id": "resp_1",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "一半"}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            },
            "error": None,
        }
        result = pb.interpret_line(line)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["text"], "一半")
        self.assertEqual(result["output_tokens"], 20)

    def test_interpret_http_error(self):
        result = pb.interpret_line(
            {"custom_id": "x", "response": {"status_code": 400, "body": {"error": {"message": "bad model"}}}, "error": None}
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("bad model", result["error"])

    def test_filename_keeps_unicode_and_limits_bytes(self):
        name = pb.output_filename("3", "South Africa - Homelands 1910-1994")
        self.assertEqual(name, "South Africa - Homelands 1910-1994-3.md")
        long_name = pb.output_filename("9", "甲" * 300)
        self.assertLessEqual(len(long_name.encode("utf-8")), 200)
        self.assertTrue(long_name.endswith("-9.md"))

    def test_compose_appends_suffix_and_dependency(self):
        text = pb.compose_input("做这件事", [("1", "甲", "# 旧稿")], pb.DEFAULT_SUFFIX)
        self.assertIn("做这件事", text)
        self.assertIn("依赖输出（编号 1，甲）开始", text)
        self.assertIn("# 旧稿", text)
        self.assertIn("不要询问是否切换到 Worker", text)
        self.assertTrue(text.endswith("\n"))


class StateTests(unittest.TestCase):
    def test_merge_keeps_done_status(self):
        rows = pb.parse_csv("id,title,prompt\n1,旧,正文\n")
        state, _ = pb.merge_state(rows, None, Path("/tmp/a.csv"))
        state["rows"]["1"]["status"] = "done"
        state["rows"]["1"]["output_name"] = "1 - 旧.md"
        updated = pb.parse_csv("id,title,prompt\n1,新标题,改过的正文\n")
        merged, notes = pb.merge_state(updated, state, Path("/tmp/a.csv"))
        self.assertEqual(merged["rows"]["1"]["status"], "done")
        self.assertEqual(merged["rows"]["1"]["title"], "新标题")
        self.assertEqual(notes, [])

    def test_retry_resets_failed_only(self):
        rows = pb.parse_csv("id,title,prompt\n1,A,pa\n2,B,pb\n")
        state, _ = pb.merge_state(rows, None, Path("/tmp/a.csv"))
        state["rows"]["1"]["status"] = "failed"
        state["rows"]["1"]["attempts"] = 3
        state["rows"]["2"]["status"] = "done"
        notes = pb.apply_retries(state, ["1", "2", "9"])
        self.assertEqual(state["rows"]["1"]["status"], "pending")
        self.assertEqual(state["rows"]["1"]["attempts"], 0)
        self.assertEqual(state["rows"]["2"]["status"], "done")
        self.assertTrue(any("已经完成" in note for note in notes))
        self.assertTrue(any("不在 CSV" in note for note in notes))

    def test_apply_batch_writes_file(self):
        rows = pb.parse_csv("id,title,prompt\n1,甲,pa\n")
        state, _ = pb.merge_state(rows, None, Path("/tmp/a.csv"))
        state["rows"]["1"]["status"] = "submitted"
        state["rows"]["1"]["batch_id"] = "batch_1"
        state["rows"]["1"]["custom_id"] = "pb011234567890"
        state["batches"]["batch_1"] = {"status": "in_progress", "row_ids": ["1"]}
        line = {
            "custom_id": "pb011234567890",
            "response": {
                "status_code": 200,
                "body": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "# 完成"}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            },
            "error": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            written = pb.apply_batch_results(state, "batch_1", "completed", [line], [], Path(tmp))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].read_text(encoding="utf-8"), "# 完成\n")
            self.assertEqual(state["rows"]["1"]["status"], "done")


class DryRunTests(unittest.TestCase):
    def test_cli_dry_run_does_not_create_output(self):
        script = Path(__file__).resolve().parent / "prompt_batch.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "prompts.csv"
            csv_path.write_text("编号,标题,正文,依赖\n1,甲,第一段,\n2,乙,第二段,1\n", encoding="utf-8")
            out = root / "out"
            completed = subprocess.run(
                [sys.executable, str(script), str(csv_path), "-o", str(out), "--dry-run"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("第 1 批", completed.stdout)
            self.assertIn("第 2 批", completed.stdout)
            self.assertIn("未调用 API", completed.stdout)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
