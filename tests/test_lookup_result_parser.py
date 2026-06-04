from __future__ import annotations

import unittest

from src.crm_json_converter.d365.batch import _lookup_result_from_item


class LookupResultParserTests(unittest.TestCase):
    def test_empty_lookup_item_is_not_a_hit(self) -> None:
        self.assertIsNone(_lookup_result_from_item({}, "jh_customerid"))
        self.assertIsNone(_lookup_result_from_item({"jh_importid": "T20260601"}, "jh_customerid"))

    def test_lookup_item_with_primary_id_is_a_hit(self) -> None:
        result = _lookup_result_from_item(
            {"jh_customerid": "abc123", "jh_importid": "T20260601"},
            "jh_customerid",
        )
        self.assertEqual(
            result,
            {
                "record_id": "abc123",
                "import_id": "T20260601",
            },
        )


if __name__ == "__main__":
    unittest.main()
