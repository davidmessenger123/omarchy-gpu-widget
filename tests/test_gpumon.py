import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "gpumon.py"
SPEC = importlib.util.spec_from_file_location("gpumon_under_test", MODULE_PATH)
GPU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GPU)


class GpuTests(unittest.TestCase):
    def valid_state(self):
        state = GPU.empty_state()
        state.update({
            "present": True,
            "vendor": "AMD",
            "card": "card0",
            "util": 12,
            "tempJunction": 70,
        })
        return state

    def test_runtime_path_is_private_and_per_user(self):
        with tempfile.TemporaryDirectory() as runtime:
            path = GPU.runtime_state_path(runtime)
            self.assertTrue(path.startswith(runtime + os.sep))
            self.assertEqual(os.path.basename(path), GPU.STATE_FILENAME)
            self.assertEqual(stat_mode(os.path.dirname(path)), 0o700)
            GPU.write_state(path, self.valid_state())
            self.assertEqual(stat_mode(path), 0o600)
            self.assertEqual(GPU.read_state(path)["card"], "card0")

    def test_runtime_directory_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as parent:
            real = os.path.join(parent, "real")
            os.mkdir(real, 0o700)
            link = os.path.join(parent, "link")
            os.symlink(real, link)
            with self.assertRaises(GPU.StateSecurityError):
                GPU.runtime_state_path(link)

    def test_state_write_rejects_symlink_and_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory:
            victim = os.path.join(directory, "victim")
            with open(victim, "w", encoding="utf-8") as handle:
                handle.write("unchanged")
            state_path = os.path.join(directory, "state.json")
            os.symlink(victim, state_path)
            with self.assertRaises(GPU.StateSecurityError):
                GPU.write_state(state_path, self.valid_state())
            with open(victim, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "unchanged")

            os.unlink(state_path)
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.chmod(state_path, 0o644)
            with self.assertRaises(GPU.StateSecurityError):
                GPU.write_state(state_path, self.valid_state())

            os.unlink(state_path)
            with open(state_path, "wb") as handle:
                handle.write(b"x" * (GPU.MAX_STATE_BYTES + 1))
            os.chmod(state_path, 0o600)
            with self.assertRaises(GPU.StateSecurityError):
                GPU.write_state(state_path, self.valid_state())

    def test_sensor_labels_select_matching_types(self):
        with tempfile.TemporaryDirectory() as hwmon:
            for name, label, value in (
                ("temp1_input", "edge", "45000\n"),
                ("temp2_input", "junction", "71000\n"),
                ("temp3_input", "mem", "60000\n"),
            ):
                with open(os.path.join(hwmon, name), "w", encoding="utf-8") as handle:
                    handle.write(value)
                index = name[4]
                with open(os.path.join(hwmon, "temp{0}_label".format(index)), "w", encoding="utf-8") as handle:
                    handle.write(label + "\n")
            sensors = GPU.read_temp_sensors(hwmon, "AMD")
            kinds = {sensor["label"]: sensor["kind"] for sensor in sensors}
            self.assertEqual(kinds["edge"], "edge")
            self.assertEqual(kinds["junction"], "junction")
            self.assertEqual(kinds["mem"], "memory")

    def test_nvidia_core_is_not_reported_as_junction(self):
        output = b"Test GPU, 10, 1024, 2048, 70, 50, 1200, 600, 30\n"
        result = SimpleNamespace(stdout=output, returncode=0)
        with mock.patch.object(GPU, "_run_nvidia", return_value=(0, output)):
            state = GPU._nvidia_state(binary="/usr/bin/nvidia-smi")
        self.assertEqual(state["vendor"], "NVIDIA")
        self.assertEqual(state["tempGpu"], 70)
        self.assertIsNone(state["tempJunction"])
        self.assertEqual(state["tempSensors"][0]["kind"], "core")
        self.assertEqual(GPU.validate_state(state)["alertLabel"], "GPU core")

    def test_malformed_state_and_subprocess_output_are_rejected(self):
        with self.assertRaises(GPU.StateValidationError):
            GPU.decode_state(b"not json")
        malformed = self.valid_state()
        malformed["util"] = float("nan")
        with self.assertRaises(GPU.StateValidationError):
            GPU.validate_state(malformed)
        malformed = self.valid_state()
        malformed["tempSensors"] = [{"label": "junction", "kind": "junction", "value": None}]
        with self.assertRaises(GPU.StateValidationError):
            GPU.validate_state(malformed)
        result = SimpleNamespace(stdout=b"only,three,fields\n", returncode=0)
        with mock.patch.object(GPU, "_run_nvidia", return_value=(0, result.stdout)):
            self.assertEqual(GPU._nvidia_rows(binary="/usr/bin/nvidia-smi"), [])

    def test_published_reader_rejects_inconsistent_alert_fields(self):
        published = GPU.validate_state(self.valid_state())
        self.assertEqual(GPU.decode_state(json.dumps(published).encode("utf-8")), published)
        inconsistent = dict(published, alertTemp=0, alertLabel="")
        with self.assertRaises(GPU.StateValidationError):
            GPU.decode_state(json.dumps(inconsistent).encode("utf-8"))

    def test_versioned_reader_rejects_partial_and_nvidia_junction_state(self):
        partial = GPU.empty_state()
        partial.pop("tempSensors")
        with self.assertRaises(GPU.StateValidationError):
            GPU.decode_state(json.dumps(partial).encode("utf-8"))
        state = self.valid_state()
        state["vendor"] = "NVIDIA"
        state["tempJunction"] = 80
        with self.assertRaises(GPU.StateValidationError):
            GPU.validate_state(state)

    def test_atomic_locked_writes_never_expose_partial_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            GPU.write_state(path, self.valid_state())
            start = threading.Event()
            failures = []
            samples = []

            def writer():
                start.wait()
                for value in range(100):
                    state = self.valid_state()
                    state["util"] = value
                    try:
                        GPU.write_state(path, state)
                    except Exception as error:
                        failures.append(error)
                        return

            def reader():
                start.wait()
                for _ in range(300):
                    try:
                        samples.append(GPU.read_state(path)["util"])
                    except Exception as error:
                        failures.append(error)
                        return

            threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(failures, [])
            self.assertTrue(samples)
            self.assertTrue(all(0 <= value < 100 for value in samples))

    def test_replace_failure_preserves_previous_complete_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            original = self.valid_state()
            GPU.write_state(path, original)
            changed = dict(original)
            changed["util"] = 88
            with mock.patch.object(GPU.os, "replace", side_effect=OSError("injected replace failure")):
                with self.assertRaises(OSError):
                    GPU.write_state(path, changed)
            self.assertEqual(GPU.read_state(path), GPU.validate_state(original))
            self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])

    def test_json_constants_and_interval_are_rejected(self):
        with self.assertRaises(GPU.StateValidationError):
            GPU.decode_state(b'{"schema":NaN}')
        with self.assertRaises(SystemExit):
            GPU.main(["--interval", "0"])
        with self.assertRaises(SystemExit):
            GPU.main(["--interval", "inf"])
        with self.assertRaises(SystemExit):
            GPU.main(["--interval", str(GPU.MAX_INTERVAL + 1)])

    def test_nvidia_query_is_cached_between_ticks(self):
        output = b"Test GPU, 10, 1024, 2048, 70, 50, 1200, 600, 30\n"
        GPU._NVIDIA_CACHE_SET = False
        with mock.patch.object(GPU, "_run_nvidia", return_value=(0, output)) as run:
            first = GPU._nvidia_state()
            second = GPU._nvidia_state()
        self.assertEqual(first["vendor"], "NVIDIA")
        self.assertEqual(second["vendor"], "NVIDIA")
        self.assertEqual(run.call_count, 1)

    def test_rc6_utilization_is_bounded_and_rejects_bad_samples(self):
        self.assertEqual(GPU._rc6_utilization(100, 100, 1000), 100.0)
        self.assertEqual(GPU._rc6_utilization(100, 1100, 1000), 0.0)
        self.assertIsNone(GPU._rc6_utilization(None, 100, 1000))
        self.assertIsNone(GPU._rc6_utilization(100, 100, 0))
        self.assertIsNone(GPU._rc6_utilization(200, 100, 1000))

    def test_qml_exposes_integrated_gpu_and_compute_processes(self):
        source = (Path(__file__).parents[1] / "Service.qml").read_text(encoding="utf-8")
        self.assertIn('igpu', source)
        self.assertIn('gpuProcesses', source)
        self.assertIn('setDetailOpen', source)

    def test_qml_has_complete_schema_and_inode_replacement_fallback(self):
        source = (Path(__file__).parents[1] / "Service.qml").read_text(encoding="utf-8")
        self.assertIn('"schema", "present", "vendor", "card"', source)
        self.assertIn("value.schema !== 1", source)
        self.assertIn("onFileChanged: reload()", source)
        self.assertNotIn("Qt.callLater(root.refresh)", source)
        self.assertIn("text === root.lastStateText", source)
        self.assertIn("onTriggered: if (root.statePath !== \"\") stateFile.reload()", source)
        self.assertIn("gpuProcesses", source)
        widget = (Path(__file__).parents[1] / "BarWidget.qml").read_text(encoding="utf-8")
        self.assertIn("setDetailOpen", widget)
        self.assertIn("GPU PROCESSES", widget)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
