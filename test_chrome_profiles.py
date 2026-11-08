#!/usr/bin/env python3
"""Tests for chrome-profiles, run against a fake Chrome folder.

    python3 test_chrome_profiles.py
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import struct
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("chrome_profiles", os.path.join(HERE, "chrome-profiles"))
_spec = importlib.util.spec_from_loader("chrome_profiles", _loader)
cp = importlib.util.module_from_spec(_spec)
_loader.exec_module(cp)

EXT = cp.EXTENSION_ID


def uvarint(n):
    out = bytearray()
    while n >= 0x80:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def log_file(batches):
    """batches: [(seq, [(key, value-or-None-for-delete), ...]), ...]"""
    out = bytearray()
    for seq, items in batches:
        body = bytearray(struct.pack("<QI", seq, len(items)))
        for key, value in items:
            if value is None:
                body += b"\x00" + uvarint(len(key)) + key
            else:
                body += b"\x01" + uvarint(len(key)) + key + uvarint(len(value)) + value
        out += b"\0\0\0\0" + struct.pack("<H", len(body)) + b"\x01" + body
    return bytes(out)


def table_block(pairs):
    out = bytearray()
    for key, value in pairs:
        out += uvarint(0) + uvarint(len(key)) + uvarint(len(value)) + key + value
    return bytes(out) + struct.pack("<II", 0, 1)


def table_file(items, seq=1):
    """A one-block table holding {key: value} at sequence `seq`."""
    tag = struct.pack("<Q", seq << 8 | 1)
    data = table_block([(k + tag, v) for k, v in sorted(items.items())])
    out = data + b"\0" + b"\0\0\0\0"
    handle = uvarint(0) + uvarint(len(data))
    index = table_block([(b"z" * 9, handle)])
    index_off = len(out)
    out += index + b"\0" + b"\0\0\0\0"
    footer = uvarint(0) + uvarint(0) + uvarint(index_off) + uvarint(len(index))
    footer += b"\0" * (40 - len(footer)) + struct.pack("<Q", 0xDB4775248B80FB57)
    return out + footer


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else json.dumps(data).encode())


def fake_chrome():
    root = tempfile.mkdtemp(prefix="chrome-profiles-test-")
    write(os.path.join(root, "Local State"), {"profile": {
        "profiles_order": ["Default", "Profile 2", "Profile 6"],
        "info_cache": {
            "Default": {"name": "Zam", "user_name": "main@example.com"},
            "Profile 2": {"name": "Dev", "user_name": "dev@example.com"},
            "Profile 6": {"name": "No Extension", "user_name": ""},
        },
    }})
    settings = {"extensions": {"settings": {EXT: {"location": 1}}}}
    disabled = {"extensions": {"settings": {EXT: {"disable_reasons": [1]}}}}
    absent = {"extensions": {"settings": {"other": {}}}}
    write(os.path.join(root, "Default", "Secure Preferences"), settings)
    write(os.path.join(root, "Profile 2", "Secure Preferences"), disabled)
    write(os.path.join(root, "Profile 6", "Secure Preferences"), absent)
    write(os.path.join(root, "Default", "Preferences"),
          {"account_info": [{"email": "other@example.com"}, {"email": "main@example.com"}]})
    write(os.path.join(root, "Profile 2", "Preferences"), {"account_info": [{"email": "dev@example.com"}]})
    write(os.path.join(root, "Profile 6", "Preferences"), {})
    store = os.path.join(root, "Default", "Local Extension Settings", EXT)
    write(os.path.join(store, "000005.ldb"), table_file({
        b"bridgeDeviceId": b'"device-default"',
        b"accountUuid": b'"12345678-aaaa"',
        b"oauth_secret": b'"never-read"',
    }, seq=1))
    write(os.path.join(store, "000006.log"), log_file([(2, [(b"bridgeDisplayName", b'"Main"')])]))
    write(os.path.join(root, "Profile 2", "Local Extension Settings", EXT, "000003.log"),
          log_file([(1, [(b"bridgeDeviceId", b'"device-dev"')])]))
    return root


class SnappyTest(unittest.TestCase):
    def test_decodes_literal_then_back_reference(self):
        self.assertEqual(cp.snappy_decompress(b"\x08\x00a\x0d\x01"), b"aaaaaaaa")


class ReadStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="chrome-profiles-store-")
        self.wanted = {"bridgeDeviceId", "accountUuid"}

    def test_newest_sequence_wins_across_log_and_table(self):
        write(os.path.join(self.dir, "000005.ldb"), table_file({b"bridgeDeviceId": b'"old"'}, seq=1))
        write(os.path.join(self.dir, "000006.log"), log_file([(9, [(b"bridgeDeviceId", b'"new"')])]))
        self.assertEqual(cp.read_store(self.dir, self.wanted)["bridgeDeviceId"], "new")

    def test_deleted_key_is_gone(self):
        write(os.path.join(self.dir, "000005.ldb"), table_file({b"accountUuid": b'"signed-in"'}, seq=1))
        write(os.path.join(self.dir, "000006.log"), log_file([(9, [(b"accountUuid", None)])]))
        self.assertNotIn("accountUuid", cp.read_store(self.dir, self.wanted))

    def test_only_allowlisted_keys_are_returned(self):
        write(os.path.join(self.dir, "000006.log"),
              log_file([(1, [(b"bridgeDeviceId", b'"id"'), (b"diskRefreshToken", b'"secret"')])]))
        self.assertEqual(cp.read_store(self.dir, self.wanted), {"bridgeDeviceId": "id"})


class ExtensionStateTest(unittest.TestCase):
    def test_absent(self):
        self.assertEqual(cp.extension_state({}), "none")

    def test_enabled(self):
        self.assertEqual(cp.extension_state({"extensions": {"settings": {EXT: {}}}}), "on")

    def test_enabled_with_empty_disable_reasons(self):
        self.assertEqual(cp.extension_state({"extensions": {"settings": {EXT: {"disable_reasons": []}}}}), "on")

    def test_disabled_by_reason(self):
        self.assertEqual(cp.extension_state({"extensions": {"settings": {EXT: {"disable_reasons": [1]}}}}), "off")


class OpenProfilesTest(unittest.TestCase):
    ROOT = "/Users/z/Library/Application Support/Google/Chrome"

    def test_main_pid_ignores_helpers_and_other_user_data_dirs(self):
        ps = "\n".join([
            f"  10 {cp.CHROME_BIN}",
            f"  11 {cp.CHROME_BIN} --user-data-dir=/Users/z/Library/Caches/playwright about:blank",
            f"  12 {cp.CHROME_BIN} --restore-last-session",
            "  13 /Applications/Google Chrome.app/Contents/Frameworks/x/Helpers/Google Chrome Helper --type=gpu",
        ])
        self.assertEqual(cp.main_pids(ps, self.ROOT), ["10", "12"])

    def test_explicit_user_data_dir_matches_its_own_process(self):
        ps = f"  11 {cp.CHROME_BIN} --user-data-dir=/tmp/other about:blank\n  10 {cp.CHROME_BIN}"
        self.assertEqual(cp.main_pids(ps, "/tmp/other"), ["11"])

    def test_lsof_output_gives_top_level_folders(self):
        lsof = "\n".join(["p10", "fcwd", "n/", "f12", f"n{self.ROOT}/Default/History",
                          f"n{self.ROOT}/Profile 8/Cookies", f"n{self.ROOT}/Local State", "n/tmp/x"])
        self.assertEqual(cp.parse_lsof(lsof, self.ROOT), {"Default", "Profile 8", "Local State"})


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.root = fake_chrome()

    def row(self, rows, profile):
        return next(r for r in rows if r["profile"] == profile)

    def test_joins_device_id_to_profile(self):
        rows = cp.collect(self.root, {"Default"})
        self.assertEqual([r["profile"] for r in rows], ["Default", "Profile 2", "Profile 6"])
        self.assertEqual(self.row(rows, "Default")["device_id"], "device-default")
        self.assertEqual(self.row(rows, "Profile 2")["device_id"], "device-dev")

    def test_signed_in_account_is_listed_first_without_duplicates(self):
        rows = cp.collect(self.root, set())
        self.assertEqual(self.row(rows, "Default")["accounts"], ["main@example.com", "other@example.com"])

    def test_open_and_extension_columns(self):
        rows = cp.collect(self.root, {"Default"})
        self.assertIs(self.row(rows, "Default")["open"], True)
        self.assertIs(self.row(rows, "Profile 2")["open"], False)
        self.assertEqual(self.row(rows, "Default")["extension"], "on")
        self.assertEqual(self.row(rows, "Profile 2")["extension"], "off")
        self.assertEqual(self.row(rows, "Profile 6")["extension"], "none")

    def test_open_is_unknown_when_lsof_failed(self):
        self.assertIsNone(cp.collect(self.root, None)[0]["open"])

    def test_user_set_name_and_claude_account(self):
        rows = cp.collect(self.root, set())
        self.assertEqual(self.row(rows, "Default")["display_name"], "Main")
        self.assertEqual(self.row(rows, "Default")["claude_account"], "12345678-aaaa")
        self.assertIsNone(self.row(rows, "Profile 2")["claude_account"])

    def test_profile_without_extension_has_no_device_id(self):
        self.assertIsNone(self.row(cp.collect(self.root, set()), "Profile 6")["device_id"])

    def test_tokens_never_reach_the_rows(self):
        self.assertNotIn("never-read", json.dumps(cp.collect(self.root, set())))

    def test_unreadable_profile_reports_error_and_keeps_going(self):
        path = os.path.join(self.root, "Profile 2", "Secure Preferences")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        rows = cp.collect(self.root, set())
        self.assertEqual(self.row(rows, "Profile 2")["extension"], "?")
        self.assertIn("Secure Preferences", self.row(rows, "Profile 2")["error"])
        self.assertEqual(self.row(rows, "Default")["extension"], "on")


class LabelTest(unittest.TestCase):
    def setUp(self):
        self.root = fake_chrome()

    def test_label_is_used_and_chrome_name_is_kept(self):
        row = cp.collect(self.root, set(), {"Profile 2": "Work"})[1]
        self.assertEqual(row["label"], "Work")
        self.assertEqual(row["name"], "Dev")

    def test_profile_without_a_label_has_none(self):
        self.assertIsNone(cp.collect(self.root, set(), {"Profile 2": "Work"})[0]["label"])


class MainTest(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def labels_file(self, text):
        path = os.path.join(tempfile.mkdtemp(prefix="chrome-profiles-labels-"), "labels.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_table_shows_labels_from_the_file(self):
        path = self.labels_file('{"Profile 2": "Work Stuff"}')
        code, out, err = self.run_main("--user-data-dir", fake_chrome(), "--labels", path)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Work Stuff", out)

    def test_missing_labels_file_is_fine(self):
        code, out, err = self.run_main("--user-data-dir", fake_chrome(), "--labels", "/nonexistent/labels.json")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("device-default", out)

    def test_broken_labels_file_warns_and_still_prints_the_table(self):
        for text in ("{not json", "[]", '{"Default": 3}'):
            with self.subTest(text=text):
                code, out, err = self.run_main("--user-data-dir", fake_chrome(), "--labels", self.labels_file(text))
                self.assertEqual(code, 0)
                self.assertIn("labels", err)
                self.assertIn("device-default", out)

    def test_only_labelled_profiles_show_in_label_order(self):
        path = self.labels_file('{"Default": "2", "Profile 2": "1"}')
        _, out, _ = self.run_main("--user-data-dir", fake_chrome(), "--labels", path, "--json")
        self.assertEqual([r["profile"] for r in json.loads(out)], ["Profile 2", "Default"])

    def test_all_flag_adds_unlabelled_profiles_after_labelled_ones(self):
        path = self.labels_file('{"Default": "2", "Profile 2": "1"}')
        _, out, _ = self.run_main("--user-data-dir", fake_chrome(), "--labels", path, "--json", "--all")
        self.assertEqual([r["profile"] for r in json.loads(out)], ["Profile 2", "Default", "Profile 6"])

    def test_table_says_how_to_see_hidden_profiles(self):
        path = self.labels_file('{"Default": "1"}')
        _, out, _ = self.run_main("--user-data-dir", fake_chrome(), "--labels", path)
        self.assertIn("2 more profiles not shown", out)
        self.assertNotIn("Profile 6", out)

    def test_table_shows_each_profile_and_device(self):
        code, out, _ = self.run_main("--user-data-dir", fake_chrome(), "--labels", "/nonexistent/labels.json")
        self.assertEqual(code, 0)
        self.assertIn("device-default", out)
        self.assertIn("Profile 6", out)
        self.assertIn("main@example.com, other@example.com", out)

    def test_json_output_is_a_list_of_rows(self):
        _, out, _ = self.run_main("--user-data-dir", fake_chrome(), "--labels", "/nonexistent/labels.json", "--json")
        self.assertEqual(json.loads(out)[0]["device_id"], "device-default")

    def test_unreadable_chrome_folder_names_full_disk_access(self):
        root = fake_chrome()
        path = os.path.join(root, "Local State")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        code, _, err = self.run_main("--user-data-dir", root)
        self.assertEqual(code, 1)
        self.assertIn("Full Disk Access", err)


if __name__ == "__main__":
    unittest.main()
