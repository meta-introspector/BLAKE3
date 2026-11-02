"""The long-proof command's exit status, using its actual shell fragments.

Exercise the runner and final verdict without invoking proofs or GitHub.
"""

from pathlib import Path
import subprocess
import tempfile
import unittest


class LongProofExit(unittest.TestCase):
    def test_success_and_failure(self):
        source = Path(__file__).with_name("long-proofs.sh").read_text()
        start = source.index("verdict=passed state=success\n")
        stop = source.index("run lean_spec ", start)
        finish = source[source.rindex('echo "long proofs '):]
        script = source[start:stop] + '\nrun control "$1"\n' + finish
        with tempfile.TemporaryDirectory() as logs:
            for command, expected, verdict in (("true", 0, "passed"), ("false", 1, "FAILED (control)")):
                with self.subTest(command=command):
                    result = subprocess.run(
                        ["sh", "-eu", "-c", script, "sh", command],
                        env={"PATH": "/usr/bin:/bin", "logs": logs, "commit": "control", "sha": "", "b3": ""},
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertIn("long proofs " + verdict, result.stdout)


class LongProofCarryOver(unittest.TestCase):
    def test_same_proof_inputs_are_not_rerun(self):
        import hashlib
        import os
        import stat
        here = Path(__file__).parent
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            (root / "tools" / "verify").mkdir(parents=True)
            (root / "c").mkdir()
            (root / "c" / "kernel.S").write_text("kernel\n")
            (root / "tools" / "verify" / "long-proofs.sh").write_text(
                (here / "long-proofs.sh").read_text())
            (root / "ghtokenclassic.txt").write_text("token\n")
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            curl_log = Path(td) / "curl.log"
            curl = bin_dir / "curl"
            curl.write_text(f"#!/bin/sh\ncat >> {curl_log}\n")
            curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
            git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
            subprocess.run(git + ["init", "-q"], check=True)
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "proof inputs"], check=True)
            prior = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
            listing = subprocess.check_output(
                git + ["ls-tree", "HEAD", "--", "c", "c2sp", "tools/verify",
                       "tools/gen_neon_hybrid.py"])
            fingerprint = hashlib.sha256(listing).hexdigest()
            record = root / "tmp" / "long-proofs" / "inputs" / (fingerprint + ".txt")
            record.parent.mkdir(parents=True)
            record.write_text(prior + "\n" + "a" * 64 + "\n" + "b" * 64 + "\n")
            (root / "README.md").write_text("docs only\n")
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "docs only"], check=True)
            commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
            env = dict(os.environ, PATH=str(bin_dir) + ":" + os.environ["PATH"])
            result = subprocess.run(
                ["sh", "tools/verify/long-proofs.sh", commit], cwd=root, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("same proof inputs as " + prior + ", not rerun", result.stdout)
            self.assertIn('"state": "success"', curl_log.read_text())
            self.assertFalse(Path("/tmp/long-proofs-" + commit).exists())


if __name__ == "__main__":
    unittest.main()
