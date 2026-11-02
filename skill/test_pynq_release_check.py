#!/usr/bin/env python3
"""Standard-library tests for the offline PYNQ release checker."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import pynq_release_check as checker


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pynq_release_check_")
        self.root = Path(self.temp.name)
        self.firmware = self.root / "overlay"
        self.runtime = self.root / "runtime"
        self.firmware.mkdir()
        self.runtime.mkdir()
        (self.firmware / "image.bit").write_bytes(b"bitstream-fixture")
        (self.firmware / "image.hwh").write_bytes(b"hardware-description")
        (self.runtime / "module.py").write_bytes(b"VERSION = 1\n")
        self.manifest = self.firmware / "manifest.json"
        self.data = {
            "files": {
                "bit": self.file_record("image.bit"),
                "hwh": self.file_record("image.hwh"),
            },
            "runtime_guards": {"module.py": digest(b"VERSION = 1\n")},
        }
        self.write_manifest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def file_record(self, name: str) -> dict[str, object]:
        content = (self.firmware / name).read_bytes()
        return {"path": name, "size": len(content), "sha256": digest(content)}

    def write_manifest(self) -> None:
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def run_check(self) -> dict[str, object]:
        return checker.check_release(self.manifest, self.runtime)

    def test_valid_bit_hwh_and_runtime_guards_pass(self) -> None:
        result = self.run_check()
        self.assertTrue(result["passed"])
        self.assertEqual(2, sum(x["status"] == "passed" for x in result["firmware_files"]))
        self.assertEqual({"checked": 1, "passed": 1, "failed": 0, "coverage": "complete"},
                         {k: result["runtime_guards"][k] for k in ("checked", "passed", "failed", "coverage")})

    def test_bit_size_mismatch_fails(self) -> None:
        self.data["files"]["bit"]["size"] += 1
        self.write_manifest()
        result = self.run_check()
        self.assertFalse(result["passed"])
        bit = result["firmware_files"][0]
        self.assertEqual("size_mismatch", bit["error"])

    def test_bit_sha_mismatch_with_same_size_fails(self) -> None:
        self.data["files"]["bit"]["sha256"] = "0" * 64
        self.write_manifest()
        result = self.run_check()
        self.assertEqual("sha256_mismatch", result["firmware_files"][0]["error"])

    def test_missing_hwh_fails(self) -> None:
        (self.firmware / "image.hwh").unlink()
        result = self.run_check()
        self.assertFalse(result["passed"])
        self.assertEqual("missing_or_not_regular_file", result["firmware_files"][1]["error"])

    def test_runtime_guard_hash_mismatch_fails(self) -> None:
        (self.runtime / "module.py").write_bytes(b"VERSION = 2\n")
        result = self.run_check()
        self.assertEqual(1, result["runtime_guards"]["failed"])
        self.assertEqual("sha256_mismatch", result["runtime_guards"]["items"][0]["error"])

    def test_missing_runtime_guard_file_fails(self) -> None:
        (self.runtime / "module.py").unlink()
        result = self.run_check()
        self.assertEqual("runtime_file_missing_or_not_regular_file", result["runtime_guards"]["items"][0]["error"])

    def test_runtime_root_missing_fails_all_declared_guards(self) -> None:
        result = checker.check_release(self.manifest, self.root / "absent-runtime")
        self.assertEqual(1, result["runtime_guards"]["failed"])
        self.assertEqual("runtime_root_unavailable", result["runtime_guards"]["items"][0]["error"])

    def test_parent_traversal_is_rejected(self) -> None:
        self.data["files"]["bit"]["path"] = "../outside.bit"
        self.write_manifest()
        result = self.run_check()
        self.assertFalse(result["passed"])
        self.assertEqual("dot_or_empty_path_component", result["firmware_files"][0]["error"])

    def test_windows_drive_path_is_rejected(self) -> None:
        self.data["files"]["bit"]["path"] = "C:/outside.bit"
        self.write_manifest()
        result = self.run_check()
        self.assertEqual("absolute_or_drive_path", result["firmware_files"][0]["error"])

    def test_posix_absolute_path_is_rejected(self) -> None:
        self.data["files"]["bit"]["path"] = "/tmp/outside.bit"
        self.write_manifest()
        result = self.run_check()
        self.assertEqual("absolute_or_drive_path", result["firmware_files"][0]["error"])

    def test_backslash_path_is_rejected(self) -> None:
        self.data["files"]["bit"]["path"] = r"folder\image.bit"
        self.write_manifest()
        result = self.run_check()
        self.assertEqual("backslash_path_not_portable", result["firmware_files"][0]["error"])

    def test_missing_guards_are_explicitly_uncovered_and_fail(self) -> None:
        del self.data["runtime_guards"]
        self.write_manifest()
        result = self.run_check()
        self.assertFalse(result["passed"])
        self.assertEqual("uncovered", result["runtime_guards"]["coverage"])
        self.assertIn("runtime_guards_missing_or_empty", [x["error"] for x in result["errors"]])

    def test_malformed_json_is_structured_failure(self) -> None:
        self.manifest.write_text("{not json", encoding="utf-8")
        result = self.run_check()
        self.assertFalse(result["passed"])
        self.assertTrue(any(x["scope"] == "manifest" for x in result["errors"]))

    def test_non_object_manifest_is_structured_failure(self) -> None:
        self.manifest.write_text("[]", encoding="utf-8")
        result = self.run_check()
        self.assertFalse(result["passed"])
        self.assertEqual("manifest_root_must_be_object", result["errors"][0]["error"])

    def test_duplicate_firmware_path_is_rejected(self) -> None:
        self.data["files"]["hwh"]["path"] = "image.bit"
        self.data["files"]["hwh"]["size"] = self.data["files"]["bit"]["size"]
        self.data["files"]["hwh"]["sha256"] = self.data["files"]["bit"]["sha256"]
        self.write_manifest()
        result = self.run_check()
        self.assertEqual("duplicate_firmware_path", result["firmware_files"][1]["error"])

    def test_cli_output_is_create_only(self) -> None:
        report = self.root / "new_report.json"
        argv = ["--manifest", str(self.manifest), "--runtime-root", str(self.runtime), "--output", str(report)]
        with redirect_stdout(io.StringIO()):
            first_code = checker.main(argv)
        original = report.read_bytes()
        with redirect_stdout(io.StringIO()):
            second_code = checker.main(argv)
        self.assertEqual(0, first_code)
        self.assertEqual(2, second_code)
        self.assertEqual(original, report.read_bytes())

    def test_symlink_escape_is_rejected_when_supported(self) -> None:
        external = self.root / "outside.py"
        external.write_text("secret", encoding="utf-8")
        link = self.runtime / "escape.py"
        try:
            os.symlink(external, link)
        except (OSError, NotImplementedError) as exc:
            outside_dir = self.root / "outside_dir"
            outside_dir.mkdir()
            external_nested = outside_dir / "secret.py"
            external_nested.write_text("secret", encoding="utf-8")
            junction = self.runtime / "escape_dir"
            junction_available = False
            if os.name == "nt":
                command = f'mklink /J "{junction}" "{outside_dir}"'
                try:
                    completed = subprocess.run(["cmd.exe", "/c", command], capture_output=True,
                                               text=True, check=False)
                    junction_available = completed.returncode == 0 and junction.exists()
                except OSError:
                    junction_available = False
            if not junction_available:
                self.skipTest(f"symlink and junction creation unavailable: {type(exc).__name__}")
            self.data["runtime_guards"]["escape_dir/secret.py"] = digest(b"secret")
            self.write_manifest()
            result = self.run_check()
            escape = result["runtime_guards"]["items"][-1]
            self.assertEqual("link_escapes_root", escape["error"])
            return
        self.data["runtime_guards"]["escape.py"] = digest(b"secret")
        self.write_manifest()
        result = self.run_check()
        escape = result["runtime_guards"]["items"][-1]
        self.assertEqual("link_escapes_root", escape["error"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", help="Optional new JSON test report path; existing files are not overwritten")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseCheckTests)
    stream = __import__("sys").stdout
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    details = {
        "schema": "PYNQ_RELEASE_CHECK_TESTS_V1",
        "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
        "failures": [{"test": str(test), "detail": detail} for test, detail in result.failures],
        "errors": [{"test": str(test), "detail": detail} for test, detail in result.errors],
        "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
        "status": "passed" if result.wasSuccessful() else "failed",
    }
    if args.report:
        try:
            with Path(args.report).open("x", encoding="utf-8", newline="\n") as output:
                json.dump(details, output, ensure_ascii=False, indent=2)
                output.write("\n")
        except FileExistsError:
            stream.write("Report file already exists; refusing to overwrite it.\n")
            return 2
        except OSError as exc:
            stream.write(f"Could not write report: {type(exc).__name__}\n")
            return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
