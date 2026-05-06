# Copyright (c) 2026, Anish Aman and Contributors
# See license.txt

from tap_ai.services.direct_response_bank import select_best_response
from frappe.tests.utils import FrappeTestCase


class TestTapResponseKnowledge(FrappeTestCase):
	def test_exact_and_fuzzy_matching_selects_best_entry(self):
		entries = [
			{
				"name": "hello-1",
				"title": "Greeting Hello",
				"student_query": "Hello",
				"normalized_query": "hello",
				"alternate_queries": "Hi\nHii\nHelo\n👋",
				"response": "Hello!",
				"match_type": "Fuzzy",
				"priority": 10,
				"is_active": 1,
			},
			{
				"name": "bye-1",
				"title": "Goodbye",
				"student_query": "Bye",
				"normalized_query": "bye",
				"alternate_queries": "Goodbye",
				"response": "Bye!",
				"match_type": "Fuzzy",
				"priority": 5,
				"is_active": 1,
			},
		]

		match = select_best_response("Helllo", entries)

		self.assertIsNotNone(match)
		self.assertEqual(match["name"], "hello-1")
		self.assertEqual(match["response"], "Hello!")

	def test_alias_matching_handles_short_variants(self):
		entries = [
			{
				"name": "morning-1",
				"title": "Good morning",
				"student_query": "good morning",
				"normalized_query": "good morning",
				"alternate_queries": "gm\nshubh prabhat",
				"response": "Good morning!",
				"match_type": "Fuzzy",
				"priority": 1,
				"is_active": 1,
			},
		]

		match = select_best_response("gm", entries)

		self.assertIsNotNone(match)
		self.assertEqual(match["name"], "morning-1")
