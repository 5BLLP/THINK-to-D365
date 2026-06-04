from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crm_json_converter.converter.payload import build_d365_payload
from crm_json_converter.converter.sanitize import sanitize_record
from crm_json_converter.converter.mappings import get_table_mapping


class AgencyBooleanTests(unittest.TestCase):
    def test_agency_customer_id_is_emitted_as_jh_museid(self) -> None:
        mapping = get_table_mapping("agency")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "agency_customer_id": "A123",
                "company": "Example Agency",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("agency", sanitized)

        self.assertEqual(errors, [])
        self.assertEqual(payload["jh_museid"], "A123")

    def test_agency_bill_to_serializes_as_boolean(self) -> None:
        mapping = get_table_mapping("agency")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "agency_customer_id": "A123",
                "company": "Example Agency",
                "agency_bill_to": "Yes",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("agency", sanitized)

        self.assertEqual(errors, [])
        self.assertIs(payload["jh_ispaymentremitter"], True)

    def test_agency_primary_contact_is_not_emitted(self) -> None:
        mapping = get_table_mapping("agency")
        field_map = {field.source_column: field for field in mapping.fields}
        errors: list[str] = []
        sanitized = sanitize_record(
            {
                "agency_customer_id": "A123",
                "company": "Example Agency",
                "fname": "Jane",
                "initial_name": "Q",
                "lname": "Smith",
            },
            field_map,
            1,
            errors,
        )

        payload = build_d365_payload("agency", sanitized)

        self.assertEqual(errors, [])
        self.assertNotIn("primarycontactid", payload)


if __name__ == "__main__":
    unittest.main()
