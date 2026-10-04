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

    def test_set_commands(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).set_commands([("help", "Show help")])
        url, payload = api.calls[0]
        self.assertTrue(url.endswith("/setMyCommands"))
        self.assertEqual(payload["commands"], [{"command": "help", "description": "Show help"}])

    def test_long_messages_are_split(self):
        api = FakeApi()
        telegram.Bot("T", "42", post=api).send("line\n" * 2000)
        self.assertGreater(len(api.calls), 1)
        self.assertTrue(all(len(p["text"]) <= 4096 for _, p in api.calls))

    def test_updates_only_from_own_chat(self):
        own = {"update_id": 5, "callback_query": {"id": "c1", "data": "r:1", "message": {"chat": {"id": 42}}}}
        foreign = {"update_id": 6, "callback_query": {"id": "c2", "data": "r:2", "message": {"chat": {"id": 7}}}}
        api = FakeApi([{"ok": True, "result": [own, foreign]}])
        bot = telegram.Bot("T", "42", post=api)
        self.assertEqual(bot.updates(), [own])
        self.assertEqual(bot.offset, 7)


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
        self.assertEqual([label for label, _ in buttons[0]], ["🔎 Recherche", "👎 Überspringen", "✅ Beworben"])
        self.assertIn('href="https://x"', text)
        data = [b[1] for row in buttons for b in row]
        self.assertEqual(data, ["r:3", "s:3", "a:3"])
