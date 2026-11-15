import os
import unittest
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except ModuleNotFoundError:
    AppTest = None


APP = """
import streamlit as st
from dashboard_auth import require_authentication, logout_button
require_authentication()
logout_button()
st.text('Private dashboard data')
"""


@unittest.skipIf(AppTest is None, "Streamlit dashboard dependencies are optional")
class DashboardAuthTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"MUNICH_DASHBOARD_PASSWORD": "mein-starkes-test-passwört"})
        env.start()
        self.addCleanup(env.stop)
        self.app = AppTest.from_string(APP).run()

    def submit(self, password):
        self.app.text_input[0].input(password)
        self.app.button[0].click().run()
        self.assertEqual(len(self.app.exception), 0)

    def test_private_content_appears_only_after_login_and_disappears_on_logout(self):
        self.assertEqual(len(self.app.text), 0)
        self.submit("Falsch!")
        self.assertEqual(len(self.app.text), 0)
        self.submit(os.environ["MUNICH_DASHBOARD_PASSWORD"])
        self.assertEqual(self.app.text[0].value, "Private dashboard data")
        other_session = AppTest.from_string(APP).run()
        self.assertEqual(len(other_session.text), 0)
        self.app.sidebar.button[0].click().run()
        self.assertEqual(len(self.app.text), 0)
        self.assertEqual(len(self.app.text_input), 1)

    def test_missing_password_blocks_even_an_authenticated_session(self):
        self.submit(os.environ["MUNICH_DASHBOARD_PASSWORD"])
        with patch.dict(os.environ, {"MUNICH_DASHBOARD_PASSWORD": ""}):
            self.app.run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.text), 0)
        self.assertEqual(len(self.app.text_input), 0)
        self.assertEqual(len(self.app.warning), 1)

    def test_five_wrong_answers_temporarily_hide_the_login_form(self):
        with patch("dashboard_auth.monotonic", return_value=100):
            for _ in range(5):
                self.submit("wrong")
        self.assertEqual(len(self.app.text_input), 0)
        self.assertIn("Zu viele Anmeldeversuche", self.app.error[0].value)
        with patch("dashboard_auth.monotonic", return_value=131):
            self.app.button[0].click().run()
            self.submit(os.environ["MUNICH_DASHBOARD_PASSWORD"])
        self.assertEqual(self.app.text[0].value, "Private dashboard data")


if __name__ == "__main__":
    unittest.main()
