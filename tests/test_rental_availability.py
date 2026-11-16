import unittest
from datetime import date

from rental_availability import LATE_MOVE_IN, SHORT_RENTAL, availability_rejection


NOVEMBER = date(2026, 11, 16)


class RentalAvailabilityTest(unittest.TestCase):
    def test_future_availability_uses_the_current_calendar_year(self):
        for description in (
            "Frei ab: 01.01.2027.",
            "Verfügbar ab Februar 2027",
            "Das Zimmer wird erst ab nächstem Jahr frei.",
            "Ab Januar frei.",
            "Einzug: 02.01.27",
            "Einzug erst 2027.",
        ):
            with self.subTest(description=description):
                self.assertEqual(availability_rejection("Zimmer", description, NOVEMBER), LATE_MOVE_IN)
        self.assertEqual(availability_rejection("WG-Zimmer ab Januar 2027", "", NOVEMBER), LATE_MOVE_IN)
        self.assertIsNone(availability_rejection("Zimmer", "Frei ab 31.12.2026.", NOVEMBER))
        self.assertIsNone(availability_rejection("Zimmer", "Frei ab 01.01.2027.", date(2027, 1, 1)))

    def test_distinguishes_move_in_from_other_dates(self):
        for title, description in (
            ("WG-Zimmer", "Veröffentlicht am 1. Januar 2027. Besichtigungen ab 2. Januar 2027."),
            ("WG-Zimmer: Besichtigung ab Januar 2027", "Frei ab sofort."),
            ("WG-Zimmer", "Die Wohnung wird ab Januar 2027 renoviert. Das Zimmer ist jetzt frei."),
            ("WG-Zimmer", "Einzug nach Absprache."),
        ):
            with self.subTest(description=description):
                self.assertIsNone(availability_rejection(title, description, NOVEMBER))

    def test_includes_the_last_day_when_calculating_four_calendar_months(self):
        start = date(2026, 12, 1)
        self.assertIsNone(availability_rejection("Zimmer", "", NOVEMBER,
                                                 available_from=start, available_until=date(2027, 3, 31)))
        self.assertEqual(availability_rejection("Zimmer", "", NOVEMBER,
                                                available_from=start, available_until=date(2027, 3, 30)), SHORT_RENTAL)
        self.assertIsNone(availability_rejection("Zimmer", "Zwischenmiete vom 01.12. bis 31.03.", NOVEMBER))
        self.assertEqual(availability_rejection("Zimmer", "Frei ab sofort, frei bis 31.01.2027.", NOVEMBER), SHORT_RENTAL)

    def test_rejects_hard_short_limits_but_keeps_open_or_longer_options(self):
        for title, description in (
            ("3-monatige Zwischenmiete", ""),
            ("Zimmer", "Befristet auf drei Monate."),
            ("Zimmer", "Zwischenmiete für 2 bis 3 Monate."),
            ("Zimmer", "Untervermietung für sechs Wochen."),
            ("Zimmer", "Zwischenmiete für drei Monate. Keine Verlängerung möglich."),
        ):
            with self.subTest(description=description):
                self.assertEqual(availability_rejection(title, description, NOVEMBER), SHORT_RENTAL)
        for description in (
            "Befristet auf vier Monate.",
            "Zwischenmiete für 2 bis 4 Monate.",
            "Mindestens 3 Monate Zwischenmiete.",
            "Zwischenmiete für 3 Monate mit Option auf Verlängerung.",
            "Noch 2 Monate bis zum Einzug.",
            "Zwischenmiete nach Absprache.",
        ):
            with self.subTest(description=description):
                self.assertIsNone(availability_rejection("Zimmer", description, NOVEMBER))
