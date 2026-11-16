import unittest
from datetime import datetime, timezone

from wg_first_message import applicant_answers, viewing_availability, viewing_slot_labels


class FirstMessageSettingsTest(unittest.TestCase):
    def test_expired_and_duplicate_slots_do_not_displace_later_options(self):
        availability = viewing_availability(
            "2026-11-20T18:00\n2026-11-18T18:00\n\n2026-11-19T19:30\n"
            "2026-11-18T18:00\n2026-11-17T18:00\n"
        )
        self.assertEqual(len(availability), 4)
        self.assertEqual(
            viewing_slot_labels(availability, datetime(2026, 11, 17, 17, tzinfo=timezone.utc)),
            ("Mittwoch, 18.11., um 18:00 Uhr", "Donnerstag, 19.11., um 19:30 Uhr"),
        )

    def test_missing_or_ambiguous_local_clock_time_is_rejected(self):
        for time in ("2026-03-29T02:30", "2026-10-25T02:30"):
            with self.subTest(time=time), self.assertRaisesRegex(ValueError, "MUNICH_VIEWING_SLOTS"):
                viewing_availability(time)

    def test_answer_configuration_accepts_known_topics_only(self):
        self.assertEqual(applicant_answers('{"rauchen": " Ich rauche nicht. "}'), {"rauchen": "Ich rauche nicht."})
        with self.assertRaisesRegex(ValueError, "MUNICH_APPLICANT_ANSWERS"):
            applicant_answers('{"unknown": "example"}')
