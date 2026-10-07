import json
import unittest

from job_radar import telegram
from job_radar.model import Posting


class FakeApi:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, url, payload, timeout=60):
        self.calls.append((url, payload))
        return self.responses.pop(0) if self.responses else {"ok": True, "result": {}}


class BotTest(unittest.TestCase):
    def test_send_message_posts_to_chat_with_html(self):
        api = FakeApi()
        telegram.Bot("TOKEN", "42", post=api).send("<b>hi</b>", buttons=[[("Go", "r:1")]])
        url, payload = api.calls[0]
        self.assertEqual(url, "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(payload["chat_id"], "42")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["reply_markup"]["inline_keyboard"], [[{"text": "Go", "callback_data": "r:1"}]])

    def test_typing_sends_chat_action(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).typing()
        url, payload = api.calls[0]
        self.assertTrue(url.endswith("/sendChatAction"))
        self.assertEqual(payload, {"chat_id": "42", "action": "typing"})

    def test_note_returns_message_id_and_delete_removes_it(self):
        api = FakeApi([{"ok": True, "result": {"message_id": 77}}])
        bot = telegram.Bot("T", "42", post=api)
        self.assertEqual(bot.note("⏳"), 77)
        bot.delete(77)
        url, payload = api.calls[1]
        self.assertTrue(url.endswith("/deleteMessage"))
        self.assertEqual(payload, {"chat_id": "42", "message_id": 77})

    def test_edit_replaces_text_and_drops_the_buttons(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).edit(77, "<b>done</b>")
        url, payload = api.calls[0]
        self.assertTrue(url.endswith("/editMessageText"))
        self.assertEqual(payload["message_id"], 77)
        self.assertEqual(payload["text"], "<b>done</b>")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertNotIn("reply_markup", payload)

    def test_too_many_requests_waits_and_tries_once_more(self):
        import io
        import urllib.error
        calls, waits = [], []

        def post(url, payload, timeout=60):
            calls.append(url)
            if len(calls) == 1:
                body = io.BytesIO(b'{"ok": false, "error_code": 429, "parameters": {"retry_after": 3}}')
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, body)
            return {"ok": True, "result": {}}

        telegram.Bot("T", "42", post=post, sleep=waits.append).send("hi")
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [3])

    def test_too_many_requests_again_waits_again(self):
        import io
        import urllib.error
        calls, waits = [], []

        def post(url, payload, timeout=60):
            calls.append(url)
            if len(calls) <= 2:
                body = io.BytesIO(b'{"ok": false, "parameters": {"retry_after": 1}}')
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, body)
            return {"ok": True, "result": {}}

        telegram.Bot("T", "42", post=post, sleep=waits.append).send("hi")
        self.assertEqual(waits, [1, 1])

    def test_error_body_without_parameters_is_a_telegram_error(self):
        import io
        import urllib.error

        def post(url, payload, timeout=60):
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b'{"parameters": null}'))

        with self.assertRaises(telegram.TelegramError):
            telegram.Bot("T", "42", post=post, sleep=lambda s: None).send("hi")

    def test_set_commands(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).set_commands([("help", "Show help")])
        url, payload = api.calls[0]
        self.assertTrue(url.endswith("/setMyCommands"))
        self.assertEqual(payload["commands"], [{"command": "help", "description": "Show help"}])

    def test_commands_for_one_chat_only(self):
        api = FakeApi()
        telegram.Bot("T", "", post=api).set_commands([("einladen", "x")], chat_id="42")
        url, payload = api.calls[0]
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": "42"})

    def test_username_is_asked_once(self):
        api = FakeApi([{"ok": True, "result": {"username": "radar_bot"}}])
        bot = telegram.Bot("T", "42", post=api)
        self.assertEqual(bot.username(), "radar_bot")
        self.assertEqual(bot.username(), "radar_bot")
        self.assertEqual(len(api.calls), 1)

    def test_long_messages_are_split(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).send("line\n" * 2000)
        self.assertGreater(len(api.calls), 1)
        self.assertTrue(all(len(p["text"]) <= 4096 for _, p in api.calls))

    def test_long_messages_split_at_paragraphs(self):
        api = FakeApi()
        para = "<b>Titel</b>\n" + "Zeile mit Text\n" * 100
        telegram.Bot("T", "42", post=api).send("\n".join([para] * 5))
        texts = [p["text"] for _, p in api.calls]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) <= 4096 for t in texts))
        self.assertTrue(all(t.startswith("<b>Titel</b>") for t in texts), [t[:20] for t in texts])

    def test_an_overlong_line_is_cut_outside_tags(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).send("<b>" + "x" * 5000 + "</b>")
        for _, p in api.calls:
            self.assertEqual(p["text"].count("<b>"), p["text"].count("</b>"))
            self.assertLessEqual(len(p["text"]), 4096)

    def test_updates_only_from_own_chat(self):
        own = {"update_id": 5, "callback_query": {"id": "c1", "data": "r:1", "message": {"chat": {"id": 42}}}}
        foreign = {"update_id": 6, "callback_query": {"id": "c2", "data": "r:2", "message": {"chat": {"id": 7}}}}
        api = FakeApi([{"ok": True, "result": [own, foreign]}])
        bot = telegram.Bot("T", "42", post=api)
        self.assertEqual(bot.updates(), [own])
        self.assertEqual(bot.offset, 7)


class DocumentTest(unittest.TestCase):
    def test_caption_is_html_and_cut_outside_tags(self):
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=60):
            sent["body"] = req.data
            return Resp()

        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(telegram, "urlopen", fake_urlopen):
            path = Path(d) / "r.md"
            path.write_text("# R")
            telegram.Bot("T", "42").send_document(path, caption="<b>Titel</b>\n\n" + "<b>x</b> " * 300)
        body = sent["body"].decode()
        self.assertIn('name="parse_mode"\r\n\r\nHTML', body)
        caption = body.split('name="caption"\r\n\r\n', 1)[1].split("\r\n--", 1)[0]
        self.assertLessEqual(len(caption), 1024)
        self.assertEqual(caption.count("<b>"), caption.count("</b>"))

    def test_document_can_carry_buttons(self):
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(req, timeout=60):
            sent["body"] = req.data
            return Resp()

        import tempfile
        from pathlib import Path
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.object(telegram, "urlopen", fake_urlopen):
            path = Path(d) / "r.md"
            path.write_text("# R")
            telegram.Bot("T", "42").send_document(path, caption="x", buttons=[[("⭐ Vormerken", "v:7")]])
        markup = sent["body"].decode().split('name="reply_markup"\r\n\r\n', 1)[1].split("\r\n--", 1)[0]
        self.assertEqual(json.loads(markup),
                         {"inline_keyboard": [[{"text": "⭐ Vormerken", "callback_data": "v:7"}]]})


class CardLinkTest(unittest.TestCase):
    def test_only_http_links_are_linked(self):
        p = Posting(source="feed", external_id="1", url="javascript:alert(1)", title="Dev", id=1)
        text, _ = telegram.posting_card(p)
        self.assertNotIn("href", text)
        self.assertIn("<b>Dev</b>", text)

    def test_card_says_how_long_the_posting_is_online(self):
        from datetime import date
        p = Posting(source="ba", external_id="1", url="https://x", title="Dev", id=1, posted_at="2026-09-25")
        text, _ = telegram.posting_card(p, today=date(2026, 10, 7))
        self.assertIn("online seit 12 Tagen", text)
        p.posted_at = ""
        self.assertNotIn("online", telegram.posting_card(p, today=date(2026, 10, 7))[0])


class ErrorTest(unittest.TestCase):
    def test_http_error_carries_telegrams_description_but_not_the_token(self):
        import io
        import urllib.error

        def post(url, payload, timeout=60):
            body = io.BytesIO(b'{"ok": false, "error_code": 400, "description": "Bad Request: query is too old"}')
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)

        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.Bot("SECRET", "42", post=post).answer("c1", "ok")
        self.assertIn("query is too old", str(ctx.exception))
        self.assertIn("answerCallbackQuery", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_http_error_without_json_body_still_says_the_status(self):
        import io
        import urllib.error

        def post(url, payload, timeout=60):
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.Bot("T", "42", post=post).send("hi")
        self.assertIn("502", str(ctx.exception))


class SharedReceiverTest(unittest.TestCase):
    def test_bot_without_chat_receives_all_chats(self):
        a = {"update_id": 5, "message": {"chat": {"id": 42}, "text": "x"}}
        b = {"update_id": 6, "message": {"chat": {"id": 7}, "text": "y"}}
        bot = telegram.Bot("T", "", post=FakeApi([{"ok": True, "result": [a, b]}]))
        self.assertEqual(bot.updates(), [a, b])


class FormatTest(unittest.TestCase):
    def test_posting_card_escapes_html_and_has_buttons(self):
        p = Posting(source="ba", external_id="1", url="https://x", title="Dev <Kotlin>", company="A&B",
                    location="Hamburg", remote=True, id=3, score=8, why="fits",
                    employer="Builds <lifts>")
        text, buttons = telegram.posting_card(p)
        self.assertIn("Dev &lt;Kotlin&gt;", text)
        self.assertIn("A&amp;B", text)
        self.assertIn("8/10", text)
        self.assertIn("🏢 Builds &lt;lifts&gt;", text)
        self.assertEqual([label for label, _ in buttons[0]], ["🔎 Recherche", "⭐ Vormerken"])
        self.assertIn('href="https://x"', text)
        data = [b[1] for row in buttons for b in row]
        self.assertEqual(data, ["r:3", "v:3"])
