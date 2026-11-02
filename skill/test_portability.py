#!/usr/bin/env python3
"""Offline portability tests for the generic PYNQ release checker.

All .bit and .hwh payloads below are opaque synthetic test bytes. Device labels
exercise the absence of checker hardcoding; they do not identify or validate
any physical board. Passing these tests is not board execution evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True

import pynq_release_check as checker


SCOPE = "offline_manifest_portability_not_board_execution"
SKILL_DIR = Path(__file__).resolve().parent
SYNTHETIC_BIT_BYTES = b"SYNTHETIC TEST BYTES ONLY: not a real FPGA bitstream.\n"
SYNTHETIC_HWH_BYTES = b"SYNTHETIC TEST BYTES ONLY: not a real board description.\n"


@dataclass
class SyntheticProject:
    name: str
    device_label: str
    root: Path
    manifest_relative: Path
    runtime_root_relative: Path
    driver_relative: Path
    bit_name: str
    hwh_name: str

    @property
    def manifest(self) -> Path:
        return self.root / self.manifest_relative

    @property
    def runtime_root(self) -> Path:
        return self.root / self.runtime_root_relative

    @property
    def driver(self) -> Path:
        return self.runtime_root / self.driver_relative


PROJECT_SPECS = (
    {
        "name": "image_filter",
        "device_label": "PYNQ-Z1",
        "runtime_root": Path("software") / "python" / "runtime",
        "driver": Path("image") / "filtering" / "pixel_driver.py",
        "bit_name": "pixel_filter_top.bit",
        "hwh_name": "pixel_filter_hw.hwh",
        "driver_bytes": b"# image_filter synthetic runtime guard\nDRIVER_KIND = 'pixel_filter'\n",
    },
    {
        "name": "signal_capture",
        "device_label": "Ultra96-V2",
        "runtime_root": Path("ps") / "runtime",
        "driver": Path("capture_driver.py"),
        "bit_name": "adc_capture_overlay.bit",
        "hwh_name": "adc_capture_topology.hwh",
        "driver_bytes": b"# signal_capture synthetic runtime guard\nDRIVER_KIND = 'adc_capture'\n",
    },
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot_files(root: Path) -> dict[str, bytes]:
    """Read every regular file under a synthetic project for mutation checks."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pynq_portability_")
        self.root = Path(self.temporary.name)
        projects_root = self.root / "synthetic_projects"
        projects_root.mkdir()
        self.projects = {
            str(spec["name"]): self._make_project(projects_root, spec)
            for spec in PROJECT_SPECS
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_project(self, projects_root: Path, spec: dict[str, object]) -> SyntheticProject:
        name = str(spec["name"])
        project_root = projects_root / name
        firmware_root = project_root / "firmware"
        runtime_root_relative = Path(spec["runtime_root"])
        driver_relative = Path(spec["driver"])
        runtime_root = project_root / runtime_root_relative
        firmware_root.mkdir(parents=True)
        driver = runtime_root / driver_relative
        driver.parent.mkdir(parents=True)
        driver_bytes = bytes(spec["driver_bytes"])
        driver.write_bytes(driver_bytes)

        bit_name = str(spec["bit_name"])
        hwh_name = str(spec["hwh_name"])
        (firmware_root / bit_name).write_bytes(SYNTHETIC_BIT_BYTES)
        (firmware_root / hwh_name).write_bytes(SYNTHETIC_HWH_BYTES)

        manifest = {
            "project": name,
            # Metadata only: the release checker intentionally does not use
            # this label to choose a board, device, or hardcoded filename.
            "fixture_device_label": str(spec["device_label"]),
            "files": {
                f"{name}_overlay": {
                    "path": bit_name,
                    "size": len(SYNTHETIC_BIT_BYTES),
                    "sha256": _sha256(SYNTHETIC_BIT_BYTES),
                },
                f"{name}_hardware_description": {
                    "path": hwh_name,
                    "size": len(SYNTHETIC_HWH_BYTES),
                    "sha256": _sha256(SYNTHETIC_HWH_BYTES),
                },
            },
            "runtime_guards": {driver_relative.as_posix(): _sha256(driver_bytes)},
        }
        (firmware_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        return SyntheticProject(
            name=name,
            device_label=str(spec["device_label"]),
            root=project_root,
            manifest_relative=Path("firmware") / "manifest.json",
            runtime_root_relative=runtime_root_relative,
            driver_relative=driver_relative,
            bit_name=bit_name,
            hwh_name=hwh_name,
        )

    def _check_without_mutation(self, project: SyntheticProject) -> dict[str, object]:
        before = _snapshot_files(project.root)
        result = checker.check_release(project.manifest, project.runtime_root)
        self.assertEqual(before, _snapshot_files(project.root), "checker changed project input bytes")
        return result

    def test_unrelated_synthetic_projects_pass_without_mutation(self) -> None:
        image_filter = self.projects["image_filter"]
        signal_capture = self.projects["signal_capture"]

        self.assertEqual("PYNQ-Z1", image_filter.device_label)
        self.assertEqual("Ultra96-V2", signal_capture.device_label)
        self.assertNotEqual(image_filter.bit_name, signal_capture.bit_name)
        self.assertNotEqual(image_filter.hwh_name, signal_capture.hwh_name)
        self.assertNotEqual(image_filter.driver_relative, signal_capture.driver_relative)
        self.assertGreater(
            len(image_filter.runtime_root_relative.parts),
            len(signal_capture.runtime_root_relative.parts),
        )

        for project in self.projects.values():
            with self.subTest(project=project.name, device_label=project.device_label):
                self.assertTrue(self._check_without_mutation(project)["passed"])

    def test_other_projects_runtime_driver_bytes_are_rejected(self) -> None:
        original_driver_bytes = {
            name: project.driver.read_bytes() for name, project in self.projects.items()
        }
        substitutions = (("image_filter", "signal_capture"), ("signal_capture", "image_filter"))
        for target_name, source_name in substitutions:
            target = self.projects[target_name]
            target.driver.write_bytes(original_driver_bytes[source_name])
            with self.subTest(target=target_name, substituted_from=source_name):
                result = self._check_without_mutation(target)
                self.assertFalse(result["passed"])
                guard = result["runtime_guards"]["items"][0]
                self.assertEqual("sha256_mismatch", guard["error"])

    def test_projects_pass_after_move_under_space_and_chinese_parent(self) -> None:
        self.assertTrue(self._check_without_mutation(self.projects["image_filter"])["passed"])
        self.assertTrue(self._check_without_mutation(self.projects["signal_capture"])["passed"])

        destinations = {
            "image_filter": self.root / "搬迁 空格 parent" / "image_filter",
            "signal_capture": self.root / "moved with spaces" / "signal_capture",
        }
        for name, destination in destinations.items():
            project = self.projects[name]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(project.root), str(destination))
            project.root = destination
            with self.subTest(project=name, destination=str(destination)):
                self.assertTrue(self._check_without_mutation(project)["passed"])

    def test_copied_skill_cli_passes_without_repository_context(self) -> None:
        isolated_root = self.root / "isolated_cli_root"
        isolated_root.mkdir()
        copied_skill = isolated_root / "skill"
        shutil.copytree(SKILL_DIR, copied_skill)
        self.assertEqual([copied_skill], list(isolated_root.iterdir()))

        child_env = os.environ.copy()
        child_env.pop("PYTHONPATH", None)
        child_env.pop("PYTHONHOME", None)
        child_env["PYTHONNOUSERSITE"] = "1"
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONUTF8"] = "1"

        for project in self.projects.values():
            before = _snapshot_files(project.root)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(copied_skill / "pynq_release_check.py"),
                    "--manifest",
                    str(project.manifest),
                    "--runtime-root",
                    str(project.runtime_root),
                ],
                cwd=isolated_root,
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
            self.assertEqual(before, _snapshot_files(project.root), "CLI changed project input bytes")
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertTrue(result["passed"], completed.stdout)


def _test_list(items: list[tuple[unittest.case.TestCase, str]], detail_key: str) -> list[dict[str, str]]:
    return [{"test": str(test), detail_key: detail} for test, detail in items]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        help="Optional new JSON test report path; an existing file is never overwritten",
    )
    args = parser.parse_args(argv)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PortabilityTests)
    stream = sys.stdout
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    details = {
        "schema": "PYNQ_SKILL_PORTABILITY_TESTS_V1",
        "scope": SCOPE,
        "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
        "failures": _test_list(result.failures, "detail"),
        "errors": _test_list(result.errors, "detail"),
        "skipped": _test_list(result.skipped, "reason"),
        "status": "passed" if result.wasSuccessful() else "failed",
        "evidence_boundary": [
            "The .bit and .hwh files are synthetic test bytes, not FPGA artifacts for a real design.",
            "PYNQ-Z1 and Ultra96-V2 are fixture labels used to test that names are not hardcoded; they are not device validation.",
            "This suite exercises offline manifest portability only and does not execute or validate a board.",
        ],
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
