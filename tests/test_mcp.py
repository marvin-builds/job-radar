import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from job_radar import mcp
from job_radar.model import Posting
from job_radar.store import Store

REPO = Path(__file__).resolve().parent.parent


def call(rid, method, **params):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def tool(rid, name, **arguments):
    return call(rid, "tools/call", name=name, arguments=arguments)


class ServeTest(unittest.TestCase):
    """The MCP server over JSON lines, as `job_radar mcp` runs it on stdin/stdout (#43)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reports = Path(self.tmp.name) / "reports"
        self.cfg = {"report": {"reports_dir": str(self.reports)}}
        self.store = Store(Path(self.tmp.name) / "radar.db")
        self.addCleanup(self.store.close)
        self.a, self.b, self.c = self.store.add_new([
            Posting(source="ba", external_id="1", url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1?x=1&y=ä",
                    title="Backend-Entwickler (m/w/d)", company="ACME GmbH", location="Stuttgart",
                    text="Wir suchen ...", posted_at="2026-11-01", contract_type="permanent"),
            Posting(source="feeds", external_id="2", url="https://jobs.example/2", title="Platform Engineer",
                    company="Beta AG"),
            Posting(source="feeds", external_id="3", url="https://jobs.example/3", title="Not bookmarked",
                    company="Gamma"),
        ])
        self.store.set_score(self.a.id, 8, "passt gut")
        self.store.mark(self.a.id, "saved")
        self.store.mark(self.b.id, "saved")

    def run_lines(self, *messages) -> list[dict]:
        inp = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        out = io.StringIO()
        mcp.serve(self.cfg, self.store, inp, out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def result(self, message) -> dict:
        [response] = self.run_lines(message)
        self.assertNotIn("error", response)
        res = response["result"]
        self.assertFalse(res.get("isError"), res)
        return json.loads(res["content"][0]["text"])

    def test_initialize_answers_with_tools_and_notifications_get_no_reply(self):
        responses = self.run_lines(call(1, "initialize", protocolVersion="2025-06-18", capabilities={},
                                        clientInfo={"name": "t", "version": "1"}),
                                   {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                   call(2, "ping"))
        self.assertEqual([r["id"] for r in responses], [1, 2])
        init = responses[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        self.assertEqual(init["serverInfo"]["name"], "job-radar")
        self.assertEqual(responses[1]["result"], {})

    def test_unknown_protocol_version_gets_the_servers_own(self):
        [r] = self.run_lines(call(1, "initialize", protocolVersion="1999-01-01", capabilities={}))
        self.assertIn(r["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS)

    def test_tools_list_names_the_three_tools_with_schemas(self):
        [r] = self.run_lines(call(1, "tools/list"))
        tools = {t["name"]: t for t in r["result"]["tools"]}
        self.assertEqual(set(tools), {"list_bookmarks", "get_bookmark", "confirm_imported"})
        for t in tools.values():
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_list_returns_new_bookmarks_oldest_first_with_the_stored_url(self):
        items = self.result(tool(1, "list_bookmarks"))
        self.assertEqual([i["id"] for i in items], [self.a.id, self.b.id])
        first = items[0]
        self.assertEqual(first["url"], self.a.url)  # exactly as stored, query and umlaut included
        self.assertEqual((first["title"], first["company"], first["location"], first["source"]),
                         ("Backend-Entwickler (m/w/d)", "ACME GmbH", "Stuttgart", "ba"))
        self.assertEqual((first["score"], first["why"], first["posted_at"]), (8, "passt gut", "2026-11-01"))
        self.assertEqual(first["kind"], "saved")
        self.assertRegex(first["bookmarked_at"], r"^\d{4}-\d{2}-\d{2} ")
        self.assertNotIn("text", first)

    def test_get_returns_the_text_and_the_report_summary(self):
        self.reports.mkdir()
        report = self.reports / "26-11-02-acme.md"
        report.write_text("---\ntitle: x\n---\n# TL;DR\nGuter Arbeitgeber.\n\n# Details\n...", encoding="utf-8")
        self.store.set_state(f"report_file:{self.a.id}", str(report))
        item = self.result(tool(1, "get_bookmark", id=self.a.id))
        self.assertEqual((item["url"], item["text"], item["report_summary"]),
                         (self.a.url, "Wir suchen ...", "Guter Arbeitgeber."))
        self.assertEqual(self.result(tool(2, "get_bookmark", id=self.b.id))["report_summary"], "")

    def test_get_refuses_a_posting_that_is_not_bookmarked(self):
        for pid in (self.c.id, 9999):
            [r] = self.run_lines(tool(1, "get_bookmark", id=pid))
            self.assertTrue(r["result"]["isError"])

    def test_confirm_drops_them_from_new_only_but_not_from_all(self):
        self.assertEqual(self.result(tool(1, "confirm_imported", ids=[self.a.id])), {"confirmed": 1})
        self.assertEqual([i["id"] for i in self.result(tool(2, "list_bookmarks"))], [self.b.id])
        every = self.result(tool(3, "list_bookmarks", new_only=False))
        self.assertEqual(sorted(i["id"] for i in every), sorted([self.a.id, self.b.id]))
        self.assertTrue([i for i in every if i["id"] == self.a.id][0]["exported_at"])

    def test_bad_requests_get_errors_and_the_server_keeps_going(self):
        responses = self.run_lines(call(1, "nope"), tool(2, "drop_tables"),
                                   tool(3, "confirm_imported", ids="1"), call(4, "ping"))
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertEqual(responses[3]["result"], {})

    def test_arguments_of_the_wrong_type_are_tool_errors(self):
        for message in (tool(1, "list_bookmarks", new_only="false"), tool(2, "get_bookmark", id=str(self.a.id)),
                        tool(3, "get_bookmark", id=True)):
            [r] = self.run_lines(message)
            self.assertTrue(r["result"]["isError"], message)

    def test_responses_from_the_client_get_no_reply(self):
        self.assertEqual(self.run_lines({"jsonrpc": "2.0", "id": 5, "result": {}}), [])

    def test_valid_json_that_is_no_request_is_an_invalid_request(self):
        out = io.StringIO()
        mcp.serve(self.cfg, self.store, io.StringIO("[1, 2]\n"), out)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32600)

    def test_a_broken_line_gets_a_parse_error(self):
        out = io.StringIO()
        mcp.serve(self.cfg, self.store, io.StringIO("{not json\n" + json.dumps(call(1, "ping")) + "\n"), out)
        broken, ping = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((broken["id"], broken["error"]["code"]), (None, -32700))
        self.assertEqual(ping["result"], {})


class CommandTest(unittest.TestCase):
    """`python3 -m job_radar --instance <name> mcp` over a real pipe: stdout carries only JSON-RPC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(root / "c"), "XDG_DATA_HOME": str(root / "d")}
        (root / "c" / "job-radar" / "instances" / "anna").mkdir(parents=True)
        db = root / "d" / "job-radar" / "instances" / "anna" / "radar.db"
        store = Store(db)
        [self.p] = store.add_new([Posting(source="feeds", external_id="1", url="https://jobs.example/1",
                                          title="Gärtnerin", company="Grün GmbH")])
        store.mark(self.p.id, "saved")
        store.close()

    def run_command(self, instance, lines):
        return subprocess.run([sys.executable, "-m", "job_radar", "--instance", instance, "mcp"],
                              input="".join(json.dumps(m) + "\n" for m in lines), capture_output=True,
                              text=True, cwd=REPO, env=self.env, timeout=30)

    def test_round_trip(self):
        proc = self.run_command("anna", [
            call(1, "initialize", protocolVersion="2025-06-18", capabilities={}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            tool(2, "list_bookmarks"), tool(3, "confirm_imported", ids=[self.p.id]), tool(4, "list_bookmarks")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        texts = [json.loads(r["result"]["content"][0]["text"]) for r in responses[1:]]
        self.assertEqual([i["url"] for i in texts[0]], ["https://jobs.example/1"])
        self.assertEqual(texts[1], {"confirmed": 1})
        self.assertEqual(texts[2], [])

    def test_an_unknown_instance_is_refused(self):
        proc = self.run_command("bert", [call(1, "ping")])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("bert", proc.stderr)
