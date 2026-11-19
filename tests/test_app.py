import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import create_app  # noqa: E402
from pricing import db  # noqa: E402

SAMPLE = """框号,文字,x1,y1,x2,y2
1,白砂糖,10,10,90,40
2,一级,110,10,190,40
3,5.00,210,10,290,40
4,红糖,10,110,90,140
5,1.20,210,110,290,140
6,跨格,90,10,110,40
7,出界,400,400,450,430
"""

# 旧报纸 OCR 常见：Big5 编码、繁体表头。青菜 的 Big5 字节按 GB18030
# 误读会变成 獵垫。
BIG5_PAGE = """框號,文字,x1,y1,x2,y2
1,青菜,10,10,90,40
2,統一,110,10,190,40
3,5.00,210,10,290,40
"""


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.sqlite3")
        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "SECRET_KEY": "test",
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, body=SAMPLE, filename="page.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body.encode("utf-8")), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def upload_and_get_job(self):
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        return int(response.headers["Location"].rstrip("/").split("/")[-1])

    def set_lines(self, job_id, horizontal, vertical):
        return self.client.post(
            f"/api/jobs/{job_id}/lines",
            json={"horizontal": horizontal, "vertical": vertical},
        )

    def state(self, job_id):
        response = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def box_id(self, state, ref):
        return next(item["box_id"] for item in state["boxes"] if item["box_ref"] == ref)


class UploadTests(AppTestCase):
    def test_upload_creates_job_and_redirects(self):
        job_id = self.upload_and_get_job()
        state = self.state(job_id)
        self.assertEqual(len(state["boxes"]), 7)
        self.assertIn(state["boxes"][0]["status"],
                      ("assigned_auto", "pending", "stale"))

    def test_upload_rejects_empty_file(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b""), "empty.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("文件为空".encode("utf-8"), response.data)

    def test_upload_without_file_redirects_with_error(self):
        response = self.client.post("/upload", data={}, follow_redirects=True)
        self.assertIn("请选择一个 CSV 文件".encode("utf-8"), response.data)

    def test_job_page_renders(self):
        job_id = self.upload_and_get_job()
        response = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"grid-lines", response.data)


class LineTests(AppTestCase):
    def test_invalid_lines_keep_previous_layout_and_assignments(self):
        job_id = self.upload_and_get_job()
        self.assertEqual(self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
                         .status_code, 200)
        before = self.state(job_id)
        response = self.set_lines(job_id, [0, 100], [0, 100, 200])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        after = self.state(job_id)
        self.assertEqual(before["horizontal"], after["horizontal"])
        self.assertEqual(before["vertical"], after["vertical"])
        self.assertEqual([(b["box_id"], b["status"]) for b in before["boxes"]],
                         [(b["box_id"], b["status"]) for b in after["boxes"]])

    def test_stale_pending_decisions_after_boundary_move(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        state = self.state(job_id)
        box = self.box_id(state, "6")
        decision = self.client.post(
            f"/api/jobs/{job_id}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "100|0|200|100"},
        )
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(self.state(job_id)["status_counts"].get("assigned_manual"), 1)

        self.set_lines(job_id, [0, 100, 200], [0, 95, 200, 300])
        state = self.state(job_id)
        item = next(entry for entry in state["boxes"] if entry["box_id"] == box)
        self.assertEqual(item["status"], "stale")

    def test_decision_survives_unrelated_line_change(self):
        job_id = self.upload_and_get_job()
        self.set_lines(job_id, [0, 100, 200], [0, 100, 200, 300])
        box = self.box_id(self.state(job_id), "6")
        self.client.post(f"/api/jobs/{job_id}/decisions",
                         json={"box_id": box, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        # Moving the bottom border y=200 does not touch box 6's cells.
        self.set_lines(job_id, [0, 100, 260], [0, 100, 200, 300])
        item = next(entry for entry in self.state(job_id)["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")


class DecisionTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])
        self.current = self.state(self.job)

    def test_accepts_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "assigned_manual")
        self.assertEqual(item["column"], "name")

    def test_rejects_non_overlapping_cell(self):
        box = self.box_id(self.current, "6")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "200|100|300|200"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_rejects_decision_for_auto_box(self):
        box = self.box_id(self.current, "1")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "cell", "target_cell": "0|0|100|100"},
        )
        self.assertEqual(response.status_code, 409)

    def test_exclude_box(self):
        box = self.box_id(self.current, "7")
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": box, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "excluded")

    def test_unknown_box_404(self):
        response = self.client.post(
            f"/api/jobs/{self.job}/decisions",
            json={"box_id": 99999, "kind": "exclude"},
        )
        self.assertEqual(response.status_code, 404)

    def test_clear_decision_returns_to_pending(self):
        box = self.box_id(self.current, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box, "kind": "exclude"})
        response = self.client.delete(f"/api/jobs/{self.job}/decisions/{box}")
        self.assertEqual(response.status_code, 200)
        item = next(entry for entry in response.get_json()["boxes"]
                    if entry["box_id"] == box)
        self.assertEqual(item["status"], "pending")


class ExportTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.job = self.upload_and_get_job()
        self.set_lines(self.job, [0, 100, 200], [0, 100, 200, 300])

    def resolve_all(self):
        state = self.state(self.job)
        box6 = self.box_id(state, "6")
        box7 = self.box_id(state, "7")
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box6, "kind": "cell",
                               "target_cell": "100|0|200|100"})
        self.client.post(f"/api/jobs/{self.job}/decisions",
                         json={"box_id": box7, "kind": "exclude"})

    def test_export_blocked_while_pending(self):
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_blocked_without_valid_lines(self):
        self.set_lines(self.job, [0, 100], [0, 100, 200])
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 409)

    def test_export_succeeds_when_all_resolved(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["Content-Type"])
        text = response.data.decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0], "品名,规格,批发价")
        self.assertEqual(lines[1], "白砂糖,跨格一级,5.00")
        self.assertEqual(lines[2], "红糖,,1.20")

    def test_export_filename_is_utf8_encoded(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        disposition = response.headers["Content-Disposition"]
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertIn("%E4%BB%B7%E7%9B%AE%E8%A1%A8", disposition)

    def test_export_uses_bom(self):
        self.resolve_all()
        response = self.client.get(f"/jobs/{self.job}/export.csv")
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))


class EncodingUploadTests(AppTestCase):
    def upload_raw(self, body, filename="page.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(body), filename)},
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    def upload_pending(self, body, filename="page.csv"):
        """Upload a file whose readings disagree; return the preview token."""
        response = self.upload_raw(body, filename)
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("/uploads/"), location)
        return location.rsplit("/", 1)[-1]

    def confirm(self, token, encoding):
        response = self.client.post(
            f"/uploads/{token}/confirm",
            data={"encoding": encoding},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertTrue(location.startswith("/jobs/"), location)
        return int(location.rstrip("/").rsplit("/", 1)[-1])

    def job_count(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            connection.close()

    def test_big5_upload_waits_for_encoding_choice(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        # 选定编码前不创建任务。
        self.assertEqual(self.job_count(), 0)
        page = self.client.get(f"/uploads/{token}")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Big5", html)
        self.assertIn("GB18030", html)
        # 实际有差异的框：两种读法都展示出来。
        self.assertIn("青菜", html)
        self.assertIn("獵垫", html)

    def test_confirm_big5_imports_correct_text(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        job_id = self.confirm(token, "big5")
        state = self.state(job_id)
        texts = [box["text"] for box in state["boxes"]]
        self.assertIn("青菜", texts)
        self.assertNotIn("獵垫", texts)

    def test_confirm_gb18030_keeps_its_own_reading(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        job_id = self.confirm(token, "gb18030")
        texts = [box["text"] for box in self.state(job_id)["boxes"]]
        self.assertIn("獵垫", texts)

    def test_export_uses_confirmed_encoding(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        job_id = self.confirm(token, "big5")
        self.assertEqual(
            self.set_lines(job_id, [0, 100], [0, 100, 200, 300]).status_code, 200)
        response = self.client.get(f"/jobs/{job_id}/export.csv")
        self.assertEqual(response.status_code, 200)
        text = response.data.decode("utf-8-sig")
        self.assertIn("青菜", text)
        self.assertNotIn("獵垫", text)

    def test_repeated_confirm_creates_only_one_job(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        job_id = self.confirm(token, "big5")
        self.assertEqual(self.confirm(token, "big5"), job_id)
        self.assertEqual(self.confirm(token, "gb18030"), job_id)
        self.assertEqual(self.job_count(), 1)
        # 直接打开已确认的预览也会跳到同一个任务。
        response = self.client.get(f"/uploads/{token}", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith(f"/jobs/{job_id}"))

    def test_same_filename_windows_keep_their_own_files(self):
        first = "框號,文字,x1,y1,x2,y2\n1,青菜,10,10,90,40\n".encode("big5")
        second = "框號,文字,x1,y1,x2,y2\n1,白菜,10,10,90,40\n".encode("big5")
        token_a = self.upload_pending(first, "page.csv")
        token_b = self.upload_pending(second, "page.csv")
        self.assertNotEqual(token_a, token_b)
        job_a = self.confirm(token_a, "big5")
        job_b = self.confirm(token_b, "big5")
        self.assertNotEqual(job_a, job_b)
        self.assertEqual(self.state(job_a)["boxes"][0]["text"], "青菜")
        self.assertEqual(self.state(job_b)["boxes"][0]["text"], "白菜")

    def test_failed_import_leaves_no_half_finished_job(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        with mock.patch.object(db, "create_job_from_upload",
                               side_effect=RuntimeError("boom")):
            response = self.client.post(
                f"/uploads/{token}/confirm",
                data={"encoding": "big5"},
                follow_redirects=True,
            )
        self.assertIn("导入失败".encode("utf-8"), response.data)
        self.assertEqual(self.job_count(), 0)
        # 预览仍然有效，修复后可以重新确认。
        job_id = self.confirm(token, "big5")
        self.assertEqual(self.state(job_id)["boxes"][0]["text"], "青菜")

    def test_confirm_with_unknown_encoding_returns_to_preview(self):
        token = self.upload_pending(BIG5_PAGE.encode("big5"))
        response = self.client.post(
            f"/uploads/{token}/confirm",
            data={"encoding": "latin1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/uploads/{token}")
        self.assertEqual(self.job_count(), 0)

    def test_unknown_preview_token_is_404(self):
        self.assertEqual(self.client.get("/uploads/nope").status_code, 404)
        self.assertEqual(
            self.client.post("/uploads/nope/confirm",
                             data={"encoding": "big5"}).status_code, 404)

    def test_gb18030_upload_offers_choice_and_imports(self):
        body = "框号,文字,x1,y1,x2,y2\n1,白砂糖,10,10,90,40\n".encode("gb18030")
        token = self.upload_pending(body)
        html = self.client.get(f"/uploads/{token}").get_data(as_text=True)
        self.assertIn("GB18030", html)
        self.assertIn("白砂糖", html)
        job_id = self.confirm(token, "gb18030")
        self.assertEqual(self.state(job_id)["boxes"][0]["text"], "白砂糖")

    def test_utf8_bom_file_imports_directly(self):
        body = "﻿框号,文字,x1,y1,x2,y2\n1,青菜,10,10,90,40\n".encode("utf-8")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        response = self.upload_raw(body)
        self.assertEqual(response.status_code, 302)
        job_id = int(response.headers["Location"].rstrip("/").rsplit("/", 1)[-1])
        self.assertEqual(self.state(job_id)["boxes"][0]["text"], "青菜")

    def test_bom_file_with_invalid_utf8_is_rejected(self):
        body = (b"\xef\xbb\xbf" + "框号,文字,x1,y1,x2,y2\n1,".encode("utf-8")
                + b"\xff\xfe,10,10,90,40\n")
        response = self.upload_raw(body)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        page = self.client.get("/")
        self.assertIn("不是有效的 UTF-8".encode("utf-8"), page.data)
        self.assertEqual(self.job_count(), 0)

    def test_undecodable_file_explains_reason(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b"\xff\xff\xff\xff"), "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("UTF-8、Big5、GB18030".encode("utf-8"), response.data)
        self.assertEqual(self.job_count(), 0)

    def test_unparseable_file_reports_csv_errors(self):
        response = self.client.post(
            "/upload",
            data={"file": (io.BytesIO("没有坐标的内容\n第二行\n".encode("utf-8")),
                           "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("没有可用的文字框".encode("utf-8"), response.data)
        self.assertEqual(self.job_count(), 0)

    def test_ascii_upload_imports_directly(self):
        response = self.upload_raw(b"box,text,x1,y1,x2,y2\n1,abc,10,10,90,40\n")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].startswith("/jobs/"))


class JobManagementTests(AppTestCase):
    def test_delete_job_removes_it(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/jobs/{job_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)

    def test_missing_job_returns_404_page(self):
        response = self.client.get("/jobs/99999")
        self.assertEqual(response.status_code, 404)

    def test_suggest_lines_returns_three_columns(self):
        job_id = self.upload_and_get_job()
        response = self.client.post(f"/api/jobs/{job_id}/suggest-lines")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data["vertical"]), 4)
        self.assertGreaterEqual(len(data["horizontal"]), 2)


if __name__ == "__main__":
    unittest.main()
