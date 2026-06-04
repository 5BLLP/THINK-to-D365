from __future__ import annotations

import unittest

from src.crm_json_converter.d365.models import D365BatchConfig


class D365BatchConfigTests(unittest.TestCase):
    def test_batch_size_is_preserved(self) -> None:
        config = D365BatchConfig(
            enabled=True,
            parallel=True,
            batch_size=25,
            max_workers=4,
        )

        self.assertEqual(config.batch_size, 25)


if __name__ == "__main__":
    unittest.main()
