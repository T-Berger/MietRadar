import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import TimeoutException

from wg_reply_inbox import WGInboxEntry, WGInboxError, recent_wg_messages


HOST = "https://www.wg-gesucht.de"
INBOX = f"{HOST}/nachrichten.html"


def thread(thread_id):
    return f"{HOST}/nachricht.html?nachrichten-id={thread_id}&list=1"


def element(*, attributes=None, text="", descendants=None):
    node = Mock()
    node.is_displayed.return_value = True
    node.get_attribute.side_effect = (attributes or {}).get
    node.text = text
    node.find_elements.side_effect = lambda _by, selector: (descendants or {}).get(selector, [])
    return node


def message(message_id, text, *, outgoing=False):
    return element(
        attributes={"id": f"last_message_id_{message_id}", "class": "message my_message" if outgoing else "message"},
        descendants={".message_content.message_text": [element(text=text)]},
    )


class WGReplyInboxTests(unittest.TestCase):
    def setUp(self):
        self.sender = Mock()
        self.sender._blocked.return_value = False
        self.driver = self.sender._browser.return_value
        self.driver.get.side_effect = lambda url: setattr(self.driver, "current_url", url)
        self.sender._login.return_value = True
        self.needs_login = False
        self.driver.find_elements.side_effect = self.find_elements
        self.links = []
        self.threads = {}
        patcher = patch("selenium.webdriver.support.ui.WebDriverWait")
        wait = patcher.start()
        self.addCleanup(patcher.stop)
        wait.return_value.until.side_effect = self.wait_until

    def find_elements(self, _by, selector):
        if selector == "#pre_session_email, #login_form #login_password":
            return [element()] if self.needs_login else []
        if self.driver.current_url == INBOX:
            if selector == ".conversation_list_item .latest_message a.link-conversation-list":
                return [element(attributes={"href": href}) for href in self.links]
            if selector == "body" and not self.links:
                return [element(text="Keine Nachrichten")]
        if selector == "#messages_container .message[id^='last_message_id_']":
            return self.threads.get(self.driver.current_url, [])
        return []

    def wait_until(self, predicate):
        result = predicate(self.driver)
        if not result:
            raise TimeoutException()
        return result

    def test_reads_latest_body_and_never_drafts_after_an_outgoing_message(self):
        self.links = [thread("21") + "#last_message_id_101", thread("22")]
        self.threads[thread("21")] = [message("100", "Hallo!", outgoing=True), message("101", "  Heute um 18 Uhr?  ")]
        self.threads[thread("22")] = [message("200", "Melde dich gern."), message("201", "Danke!", outgoing=True)]

        self.assertEqual(recent_wg_messages(self.sender), (
            WGInboxEntry("21", thread("21") + "#last_message_id_101", "101", "Heute um 18 Uhr?"),
            WGInboxEntry("22", thread("22") + "#last_message_id_201", "201", None),
        ))
        self.sender._login.assert_not_called()

    def test_only_visits_valid_unique_threads_and_respects_limit(self):
        self.links = [f"{HOST}.example/nachricht.html?nachrichten-id=4", f"{thread('7')}&nachrichten-id=8",
                      thread("21") + "&redirect=https://example.com", thread("21"), thread("22")]
        self.threads[thread("21")] = [message("101", "Hi!")]

        self.assertEqual([entry.conversation_id for entry in recent_wg_messages(self.sender, limit=1)], ["21"])
        self.assertEqual([call.args[0] for call in self.driver.get.call_args_list], [INBOX, thread("21")])

    def test_unavailable_thread_does_not_discard_other_messages(self):
        self.links = [thread("20"), thread("21")]
        self.threads[thread("21")] = [message("101", "Hallo zusammen.")]

        self.assertEqual([entry.conversation_id for entry in recent_wg_messages(self.sender)], ["21"])

    def test_existing_login_flow_is_reused_without_a_reply_action(self):
        self.needs_login = True

        def login(_driver, _wait):
            self.needs_login = False
            return True

        self.sender._login.side_effect = login
        self.assertEqual(recent_wg_messages(self.sender), ())
        self.sender._login.assert_called_once()
        self.assertEqual([call.args[0] for call in self.driver.get.call_args_list], [INBOX, INBOX])

    def test_verification_is_reported_without_trying_to_log_in(self):
        self.sender._blocked.return_value = True
        with self.assertRaisesRegex(WGInboxError, "Anmeldung oder Bestätigung"):
            recent_wg_messages(self.sender)
        self.sender._login.assert_not_called()
