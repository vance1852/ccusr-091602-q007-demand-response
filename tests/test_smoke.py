"""工程基础结构检查。"""

import json
import unittest
from pathlib import Path

from app import PROJECT_NAME


class ContractSmokeTest(unittest.TestCase):
    def test_contract_matches_package(self):
        contract = json.loads(Path("domain_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["project"], PROJECT_NAME)
        self.assertIn("must_run", contract["load_kinds"])


if __name__ == "__main__":
    unittest.main()

