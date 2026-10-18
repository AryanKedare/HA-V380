import base64
import json
import unittest

from pathlib import Path
import importlib.util
path=Path(__file__).resolve().parents[1]/"custom_components"/"v380_cloud"/"shared.py"
spec=importlib.util.spec_from_file_location("v380_shared_test",path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
normalize_shared_device=module.normalize_shared_device


def record(inner, **kwargs):
    return dict(device_id=42, str=base64.b64encode(json.dumps(inner).encode()).decode(), **kwargs)


class SharedNormalizationTests(unittest.TestCase):
    def test_legacy_clear_inner_credentials(self):
        result = normalize_shared_device(record({"username": "viewer", "password": "fixture-password", "nickname": "inner"}, nickname="outer", p_id=5, share_id=77, sub_server="dispatcher.example"))
        self.assertEqual((result["device_account"], base64.b64decode(result["device_password"]).decode(), result["ver"], result["type"]), ("viewer", "fixture-password", 0, 0))
        self.assertEqual((result["nickname"], result["p_id"], result["share_id"], result["sub_server"]), ("outer", 5, 77, "dispatcher.example"))
        self.assertNotIn("str", result)

    def test_defaults_match_parse_share_device(self):
        result = normalize_shared_device({"device_id": 42, "str": ""})
        self.assertEqual((result["device_account"], result["device_password"], result["nickname"]), ("admin", "", "42"))

    def test_all_three_keys_select_new_platform(self):
        result = normalize_shared_device(record({"username": "ignore", "password": "ignore", "public_key": "synthetic", "permission": 7, "model": "fixture"}, pver=2, rk="synthetic-rk"))
        self.assertEqual((result["type"], result["device_account"], result["device_password"], result["permission"], result["rk"]), (2, "", "", 7, "synthetic-rk"))
        self.assertEqual(normalize_shared_device(record({"public_key": "only-one-key"}))["type"], 0)

    def test_malformed_record_rejected(self):
        for row in ({"device_id": 42, "str": "not-valid-base64?"}, {"device_id": True}, record(["not-object"])):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    normalize_shared_device(row)


if __name__ == "__main__":
    unittest.main()
