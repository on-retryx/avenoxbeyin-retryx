"""devices: the opt-in staged multi-device layout (solo and phone levels).

Synthetic vaults only; no network, nothing outside the temporary folder is written.
"""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3_devices as devices
from v3_package_helpers import inherited_env

CLI = ROOT / "scripts/beyin_v3.py"
GIT = shutil.which("git")


def git(folder, *args):
    env = inherited_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run([GIT, "-C", str(folder), *args], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise AssertionError("git " + " ".join(args) + ": " + done.stderr)
    return done.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="beyin-devices-test-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def machine(self, name="a"):
        vault, state = self.tmp / name / "vault", self.tmp / name / "state"
        vault.mkdir(parents=True)
        state.mkdir()
        return vault, state

    def tree(self, folder):
        return sorted(str(path.relative_to(folder)) for path in folder.rglob("*"))


class SettingsTest(Base):
    def test_off_by_default_and_doctor_shows_only_the_switch(self):
        vault, state = self.machine()
        self.assertEqual(devices.status(vault, state)["level"], "solo")
        self.assertFalse(devices.status(vault, state)["enabled"])
        self.assertEqual(devices.doctor(state), {"enabled": False})
        self.assertEqual(list(state.iterdir()), [])

    def test_broken_settings_read_as_off_and_are_not_overwritten(self):
        vault, state = self.machine()
        (state / devices.SETTINGS_FILE).write_text("{not json", encoding="utf-8")
        settings, valid = devices.read_settings(state)
        self.assertEqual((settings["level"], valid), ("solo", False))
        self.assertEqual(devices.doctor(state)["valid"], False)
        with self.assertRaisesRegex(ValueError, "invalid"):
            devices.apply(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertEqual((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"), "{not json")
        (state / devices.SETTINGS_FILE).write_bytes(b"\xff\xfe{")
        self.assertEqual(devices.read_settings(state), (devices.default_settings(), False))

    def test_explicit_solo_resets_a_file_this_version_cannot_read(self):
        vault, state = self.machine()
        (state / devices.SETTINGS_FILE).write_text(json.dumps({"schema": 1, "level": "hub"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--level solo"):
            devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        self.assertEqual(devices.apply(vault, state, {"level": "solo"})["status"], "applied")
        self.assertEqual(devices.read_settings(state), (devices.default_settings(), True))

    def test_unknown_or_inconsistent_settings_are_invalid(self):
        _, state = self.machine()
        for data in ({"schema": 1, "level": "cluster"}, {"schema": 2, "level": "solo"},
                     {"schema": 1, "level": "phone", "phone_sync": None},
                     {"schema": 1, "level": "solo", "phone_sync": "icloud"}, ["solo"],
                     {"schema": 1, "level": "computers", "branch": "-x"},
                     {"schema": 1, "level": "computers", "remote_name": ""}):
            (state / devices.SETTINGS_FILE).write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(devices.read_settings(state)[1], data)

    def test_settings_written_by_a_later_version_keep_their_extra_fields(self):
        vault, state = self.machine()
        stored = {"schema": 1, "level": "phone", "phone_sync": "icloud", "interval": 0}
        (state / devices.SETTINGS_FILE).write_text(json.dumps(stored), encoding="utf-8")
        self.assertTrue(devices.read_settings(state)[1])
        devices.apply(vault, state, {"phone_sync": "other"})
        saved = json.loads((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"))
        self.assertEqual((saved["phone_sync"], saved["interval"]), ("other", 0))


class DetectTest(Base):
    def test_sync_location_recognises_services_from_the_path_only(self):
        home = "/Users/x"
        cases = {
            home + "/Library/Mobile Documents/iCloud~md~obsidian/Documents/Beyin": ("icloud", True),
            home + "/Library/Mobile Documents/com~apple~CloudDocs/Beyin": ("icloud", False),
            home + "/Library/CloudStorage/Dropbox/Beyin": ("dropbox", False),
            home + "/Library/CloudStorage/OneDrive-Personal/Beyin": ("onedrive", False),
            "C:/Users/x/OneDrive/Beyin": ("onedrive", False),
            "C:/Users/x/OneDrive - Contoso/Beyin": ("onedrive", False),
            "C:/Users/x/OneDriveOld/Beyin": (None, False),
            home + "/Dropbox/Beyin": ("dropbox", False),
            home + "/Documents/Beyin": (None, False),
        }
        for path, (service, phone) in cases.items():
            found = devices.sync_location(Path(path))
            self.assertEqual((found["service"], found["obsidian_ios_folder"]), (service, phone), path)

    def test_detect_writes_nothing_and_reads_obsidian_sync_in_both_formats(self):
        vault, state = self.machine()
        (vault / ".obsidian").mkdir()
        for content, expected in (('["file-explorer", "sync"]', True), ('{"sync": true}', True),
                                  ('{"sync": false}', False), ('["graph"]', False), ("broken", False)):
            (vault / ".obsidian/core-plugins.json").write_text(content, encoding="utf-8")
            before = self.tree(vault)
            found = devices.detect(vault, state)
            self.assertEqual(found["obsidian"]["sync_plugin"], expected, content)
            self.assertEqual(self.tree(vault), before)
        self.assertEqual(list(state.iterdir()), [])
        self.assertFalse(found["git"]["repository"])
        self.assertEqual(found["current_fit"], {"level": "solo", "phone_sync": None})
        self.assertEqual(set(found["levels"]), {"solo", "phone", "computers"})
        self.assertEqual(found["levels"]["phone"]["needs_per_option"]["other"], [])

    def test_current_fit_suggests_the_phone_option_already_in_use(self):
        vault, state = self.machine()
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian/core-plugins.json").write_text('{"sync": true}', encoding="utf-8")
        self.assertEqual(devices.detect(vault, state)["current_fit"], {"level": "phone", "phone_sync": "obsidian-sync"})
        (vault / ".obsidian/core-plugins.json").unlink()
        (vault / ".stfolder").mkdir()
        self.assertEqual(devices.detect(vault, state)["current_fit"], {"level": "phone", "phone_sync": "other"})

    @unittest.skipUnless(GIT, "git required")
    def test_detect_reports_the_git_directory_also_through_a_symlink(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        link = self.tmp / "link"
        link.symlink_to(vault, target_is_directory=True)
        found = devices.detect(link, state)["git"]
        self.assertEqual(found, {"repository": True, "git_dir_inside_vault": True, "git_file_in_vault": False})

    @unittest.skipUnless(GIT, "git required")
    def test_detect_reports_a_separate_git_dir_file(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main", "--separate-git-dir", str(self.tmp / "outside.git"))
        found = devices.detect(vault, state)["git"]
        self.assertEqual(found, {"repository": True, "git_dir_inside_vault": False, "git_file_in_vault": True})


class PlanTest(Base):
    def test_human_entry_prints_the_device_result(self):
        spec = importlib.util.spec_from_file_location("devices_entry_test", ROOT / "scripts/beyin_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        planned = {"level": "phone", "manual_steps": ["Open the folder in Obsidian"], "warnings": []}
        self.assertEqual(json.loads(entry.human_result(planned, "devices")), planned)

    def test_level_rules(self):
        vault, state = self.machine()
        for options, message in (
                ({"level": "phone"}, "needs --phone-sync"),
                ({"level": "solo", "phone_sync": "icloud"}, "needs --level phone"),
                ({"phone_sync": "icloud"}, "needs --level phone"),
                ({"level": "hub"}, "--level must be one of"),
                ({"level": "phone", "phone_sync": "icloud", "git_dir": "/tmp/notes.git"}, "belongs to --level computers"),
                ({"level": "solo", "remote": "/tmp/hub.git"}, "belongs to --level computers"),
                ({"level": "computers", "git_dir": "notes.git"}, "absolute path"),
                ({"level": "phone", "phone_sync": "dropbox"}, "--phone-sync must be one of")):
            with self.assertRaisesRegex(ValueError, message, msg=str(options)):
                devices.plan(vault, state, options)
        self.assertEqual(list(state.iterdir()), [])

    def test_phone_levels_give_guidance_and_write_only_the_state_settings(self):
        vault, state = self.machine()
        for choice in devices.PHONE_SYNCS:
            planned = devices.public_plan(devices.plan(vault, state, {"level": "phone", "phone_sync": choice}))
            self.assertTrue(planned["manual_steps"], choice)
            self.assertNotIn("settings", planned)
            self.assertNotIn("actions", planned)
        self.assertEqual(list(state.iterdir()), [])
        before = self.tree(vault)
        applied = devices.apply(vault, state, {"level": "phone", "phone_sync": "obsidian-sync"})
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(devices.status(vault, state)["phone_sync"], "obsidian-sync")
        self.assertEqual(self.tree(vault), before)
        self.assertEqual(devices.apply(vault, state, {"level": "phone"})["status"], "unchanged")

    def test_going_back_to_solo_clears_the_phone_option(self):
        vault, state = self.machine()
        devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        devices.apply(vault, state, {"level": "solo"})
        self.assertEqual(devices.status(vault, state)["phone_sync"], None)
        self.assertEqual(devices.doctor(state), {"enabled": False})

    def test_state_inside_a_sync_folder_is_warned(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        state = self.tmp / "Library/CloudStorage/Dropbox/state"
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertTrue(any("runtime state" in warning for warning in planned["warnings"]))

    @unittest.skipUnless(GIT, "git required")
    def test_git_folder_inside_a_synced_vault_is_warned_but_not_touched(self):
        vault = self.tmp / "Library/CloudStorage/Dropbox/vault"
        vault.mkdir(parents=True)
        state = self.tmp / "state"
        git(vault, "init", "-q", "-b", "main")
        before = self.tree(vault)
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertTrue(any(".git folder is inside the synced folder" in warning for warning in planned["warnings"]))
        self.assertFalse(any("--git-dir" in text for text in planned["warnings"] + planned["manual_steps"]))
        devices.apply(vault, state, {"level": "phone", "phone_sync": "other"})
        self.assertEqual(self.tree(vault), before)

    @unittest.skipUnless(GIT, "git required")
    def test_git_warnings_follow_the_chosen_service_not_only_the_current_path(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")

        def warned(choice):
            planned = devices.plan(vault, state, {"level": "phone", "phone_sync": choice})
            return any(".git folder is inside" in warning for warning in planned["warnings"])
        self.assertTrue(warned("icloud"))  # the vault is about to move into iCloud
        self.assertTrue(warned("other"))
        self.assertFalse(warned("obsidian-sync"))  # Obsidian Sync skips hidden folders
        (vault / ".stfolder").mkdir()
        self.assertTrue(warned("obsidian-sync"))  # Syncthing carries them anyway

    @unittest.skipUnless(GIT, "git required")
    def test_a_separate_git_dir_file_is_warned(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main", "--separate-git-dir", str(self.tmp / "outside.git"))
        planned = devices.plan(vault, state, {"level": "phone", "phone_sync": "icloud"})
        self.assertTrue(any(".git file that points" in warning for warning in planned["warnings"]))


class CliTest(Base):
    def run_cli(self, vault, state, *args):
        env = inherited_env(HOME=str(self.home), USERPROFILE=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                            GIT_CONFIG_NOSYSTEM="1")
        done = subprocess.run([sys.executable, str(CLI), "--vault", str(vault), "--state", str(state), *args],
                              capture_output=True, text=True, env=env)
        return done.returncode, done.stdout, done.stderr

    def test_status_detect_plan_and_apply(self):
        vault, state = self.machine()
        code, out, err = self.run_cli(vault, state, "devices")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["level"], "solo")
        code, out, err = self.run_cli(vault, state, "devices", "--detect")
        self.assertEqual(code, 0, err)
        self.assertIn("levels", json.loads(out))
        code, out, err = self.run_cli(vault, state, "devices", "--plan", "--level", "phone", "--phone-sync", "icloud")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["manual_steps"])
        self.assertFalse((state / devices.SETTINGS_FILE).exists())
        code, out, err = self.run_cli(vault, state, "devices", "--apply", "--level", "phone", "--phone-sync", "icloud")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "applied")
        self.assertEqual(json.loads((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"))["phone_sync"], "icloud")

    def test_options_without_plan_or_apply_are_rejected(self):
        vault, state = self.machine()
        code, _, err = self.run_cli(vault, state, "devices", "--level", "phone")
        self.assertEqual(code, 1)
        self.assertIn("only valid with --plan or --apply", err)
        code, _, err = self.run_cli(vault, state, "devices", "--plan", "--level", "hub")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice", err)

    def test_doctor_reports_the_layout(self):
        vault, state = self.machine()

        def doctor():
            code, out, err = self.run_cli(vault, state, "doctor")
            self.assertEqual(code, 0, err)
            return json.loads(out)
        self.assertEqual(doctor()["devices"], {"enabled": False})
        devices.apply(vault, state, {"level": "phone", "phone_sync": "icloud"})
        shown = doctor()["devices"]
        self.assertEqual((shown["enabled"], shown["level"], shown["phone_sync"]), (True, "phone", "icloud"))


class ExternalGitDirTest(Base):
    """Groundwork for git levels: the git directory of a synced vault lives outside it."""

    def icloud_machine(self):
        vault = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes"
        vault.mkdir(parents=True)
        state = self.tmp / "state"
        state.mkdir()
        return vault, state

    def store(self, state, **settings):
        (state / devices.SETTINGS_FILE).write_text(json.dumps({**devices.default_settings(), **settings}), encoding="utf-8")

    @unittest.skipUnless(GIT, "git is required")
    def test_init_writes_nothing_into_the_vault_and_git_works_through_the_arguments(self):
        vault, state = self.icloud_machine()
        (vault / "note.md").write_text("# not\n", encoding="utf-8")
        before = self.tree(vault)
        git_dir = self.tmp / "git" / "notes.git"
        self.assertEqual(devices.init_external_git(vault, git_dir)["status"], "created")
        self.assertEqual(self.tree(vault), before)
        for args in (("add", "-A"), ("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "first")):
            self.assertEqual(devices._git(vault, *args, git_dir=git_dir).returncode, 0)
        self.assertEqual(self.tree(vault), before)
        self.assertEqual(devices._git(vault, "symbolic-ref", "--short", "HEAD", git_dir=git_dir).stdout.strip(), "main")
        self.assertEqual(devices._git(vault, "ls-files", git_dir=git_dir).stdout.split(), ["note.md"])
        self.assertEqual(devices.init_external_git(vault, git_dir)["status"], "unchanged")

    @unittest.skipUnless(GIT, "git is required")
    def test_git_environment_variables_do_not_redirect_the_calls(self):
        vault, state = self.machine()
        git_dir = self.tmp / "notes.git"
        devices.init_external_git(vault, git_dir)
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            top = devices._git(vault, "rev-parse", "--absolute-git-dir", git_dir=git_dir).stdout.strip()
        self.assertEqual(Path(top).resolve(), git_dir.resolve())

    def test_unsafe_git_dir_locations_are_refused(self):
        vault, state = self.icloud_machine()
        cases = {
            "relative/notes.git": "absolute",
            str(vault / "inner.git"): "outside the vault",
            str(vault.parent): "contains the vault",
            str(self.home / "Library/Mobile Documents/com~apple~CloudDocs/notes.git"): "file sync folder",
        }
        for path, reason in cases.items():
            with self.subTest(path=path):
                with self.assertRaisesRegex(devices.DevicesError, reason):
                    devices.init_external_git(vault, path)
        self.assertFalse((vault / "inner.git").exists())

    @unittest.skipUnless(GIT, "git is required")
    def test_an_existing_git_entry_or_a_foreign_folder_is_left_alone(self):
        vault, state = self.machine()
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(devices.DevicesError, "holds other files"):
            devices.init_external_git(vault, busy)
        self.assertEqual(self.tree(busy), ["keep.txt"])
        git(vault, "init", "-q")
        with self.assertRaisesRegex(devices.DevicesError, "already has a .git"):
            devices.init_external_git(vault, self.tmp / "new.git")
        self.assertFalse((self.tmp / "new.git").exists())

    def test_settings_keep_only_an_absolute_git_dir(self):
        vault, state = self.machine()
        self.store(state, git_dir=str(self.tmp / "notes.git"))
        settings, valid = devices.read_settings(state)
        self.assertTrue(valid)
        self.assertEqual(settings["git_dir"], str(self.tmp / "notes.git"))
        self.store(state, git_dir="notes.git")
        self.assertFalse(devices.read_settings(state)[1])

    @unittest.skipUnless(GIT, "git is required")
    def test_only_the_registered_bridge_may_run_git_on_a_synced_vault(self):
        vault, state = self.icloud_machine()
        with self.assertRaisesRegex(devices.DevicesError, "--git-dir PATH"):
            devices.bridge_git_dir(vault, state)
        git_dir = self.tmp / "notes.git"
        self.store(state, git_dir=str(git_dir))
        with self.assertRaisesRegex(devices.DevicesError, "missing or is not a git repository"):
            devices.bridge_git_dir(vault, state)
        devices.init_external_git(vault, git_dir)
        self.assertEqual(devices.bridge_git_dir(vault, state), git_dir)
        (vault / ".git").write_text("gitdir: /Users/someone/notes.git\n", encoding="utf-8")
        with self.assertRaisesRegex(devices.DevicesError, ".git entry"):
            devices.bridge_git_dir(vault, state)

    def test_an_unsynced_vault_keeps_an_ordinary_git_folder(self):
        vault, state = self.machine()
        self.assertIsNone(devices.bridge_git_dir(vault, state))
        self.store(state, level="phone", phone_sync="icloud")
        with self.assertRaisesRegex(devices.DevicesError, "file sync folder"):
            devices.bridge_git_dir(vault, state)

    @unittest.skipUnless(GIT, "git is required")
    @unittest.skipIf(os.name == "nt", "POSIX shell quoting")
    def test_the_printed_command_can_be_pasted_as_is(self):
        vault = self.tmp / "My Notes ü"
        vault.mkdir()
        git_dir = self.tmp / "git dir" / "notes.git"
        devices.init_external_git(vault, git_dir)
        command = devices.git_command(vault, git_dir, "status", "--short")
        self.assertIn("--work-tree=", command)
        done = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(devices.git_command(vault, None, "status"), "git -C '" + str(vault) + "' status")


class ComputersLevelTest(Base):
    # several computers share the vault through a git remote. A synced vault keeps its git
    # directory outside the vault and only that one computer (the bridge) runs git.

    def icloud_machine(self):
        vault = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes"
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# not\n", encoding="utf-8")
        state = self.tmp / "state"
        state.mkdir()
        return vault, state

    def hub(self):
        bare = self.tmp / "hub.git"
        git(self.tmp, "init", "-q", "--bare", str(bare))
        return bare

    @unittest.skipUnless(GIT, "git is required")
    def test_an_unsynced_git_vault_gets_a_remote_and_git_rules_only(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        before = self.tree(vault)
        planned = devices.public_plan(devices.plan(vault, state, {"level": "computers"}))
        self.assertEqual([action["kind"] for action in planned["actions"]], ["git_rules", "git_rules"])
        self.assertTrue(any("remote add origin" in step for step in planned["manual_steps"]))
        bare = self.hub()
        planned = devices.public_plan(devices.plan(vault, state, {"level": "computers", "remote": str(bare)}))
        self.assertEqual([action["kind"] for action in planned["actions"]], ["git_rules", "git_rules", "set_remote"])
        self.assertEqual(list(state.iterdir()), [])
        applied = devices.apply(vault, state, {"level": "computers", "remote": str(bare)})
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(git(vault, "remote", "get-url", "origin"), str(bare))
        self.assertEqual((devices.status(vault, state)["level"], devices.status(vault, state)["git_dir"]), ("computers", None))
        outside_git = lambda: [path for path in self.tree(vault) if not path.startswith(".git")]
        self.assertEqual(outside_git(), [])
        self.assertIn("beyin.py", (vault / ".git/info/exclude").read_text(encoding="utf-8"))
        self.assertEqual(devices.apply(vault, state, {"level": "computers", "remote": str(bare)})["status"], "unchanged")
        shown = devices.doctor(state)
        self.assertEqual((shown["enabled"], shown["level"], shown["git_dir"]), (True, "computers", None))

    def test_a_vault_without_git_is_refused_with_the_way_forward(self):
        vault, state = self.machine()
        with self.assertRaisesRegex(devices.DevicesError, "git init.*--git-dir PATH"):
            devices.plan(vault, state, {"level": "computers"})
        self.assertEqual(list(state.iterdir()), [])

    @unittest.skipUnless(GIT, "git is required")
    def test_a_synced_vault_runs_git_only_through_its_external_git_directory(self):
        vault, state = self.icloud_machine()
        options = {"level": "computers", "phone_sync": "icloud"}
        with self.assertRaisesRegex(devices.DevicesError, "--git-dir PATH"):
            devices.plan(vault, state, options)
        git_dir = self.tmp / "git" / "notes.git"
        bare = self.hub()
        options.update(git_dir=str(git_dir), remote=str(bare))
        planned = devices.public_plan(devices.plan(vault, state, options))
        self.assertEqual([action["kind"] for action in planned["actions"]],
                         ["init_git_dir", "git_rules", "git_rules", "set_remote"])
        self.assertFalse(git_dir.exists())
        self.assertEqual(list(state.iterdir()), [])
        before = self.tree(vault)
        self.assertEqual(devices.apply(vault, state, options)["status"], "applied")
        self.assertEqual(self.tree(vault), before)
        self.assertEqual(devices.status(vault, state)["git_dir"], str(git_dir))
        self.assertEqual(devices.bridge_git_dir(vault, state), git_dir)
        self.assertEqual(devices._git(vault, "remote", "get-url", "origin", git_dir=git_dir).stdout.strip(), str(bare))
        # The stored directory is reused: later runs need neither --git-dir nor --remote.
        self.assertEqual(devices.apply(vault, state, {"level": "computers"})["status"], "unchanged")

    @unittest.skipUnless(GIT, "git is required")
    def test_a_git_entry_inside_a_synced_vault_is_never_moved(self):
        vault, state = self.icloud_machine()
        (vault / ".git").write_text("gitdir: /Users/someone/notes.git\n", encoding="utf-8")
        with self.assertRaisesRegex(devices.DevicesError, ".git entry"):
            devices.apply(vault, state, {"level": "computers", "phone_sync": "icloud",
                                         "git_dir": str(self.tmp / "notes.git")})
        self.assertFalse((self.tmp / "notes.git").exists())
        self.assertEqual((vault / ".git").read_text(encoding="utf-8"), "gitdir: /Users/someone/notes.git\n")
        self.assertEqual(list(state.iterdir()), [])

    @unittest.skipUnless(GIT, "git is required")
    def test_unsafe_remotes_are_refused(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        for remote, reason in (("https://me:secret@example.com/notes.git", "password"),
                               (str(vault / "hub.git"), "outside the vault"),
                               ("-uhack", "dash")):
            with self.subTest(remote=remote):
                with self.assertRaisesRegex(devices.DevicesError, reason):
                    devices.plan(vault, state, {"level": "computers", "remote": remote})
        self.assertNotIn("origin", git(vault, "remote"))

    @unittest.skipUnless(GIT, "git is required")
    def test_going_back_to_solo_keeps_the_git_directory(self):
        vault, state = self.icloud_machine()
        git_dir = self.tmp / "notes.git"
        devices.apply(vault, state, {"level": "computers", "phone_sync": "icloud", "git_dir": str(git_dir)})
        planned = devices.apply(vault, state, {"level": "phone"})
        self.assertEqual(planned["status"], "applied")
        self.assertTrue(devices.is_git_dir(vault, git_dir))
        self.assertEqual(devices.status(vault, state)["git_dir"], str(git_dir))

    def test_detect_lists_what_the_computers_level_needs(self):
        vault, state = self.icloud_machine()
        needs = devices.detect(vault, state)["levels"]["computers"]["needs"]
        self.assertTrue(any("--git-dir" in need for need in needs))
        plain, plain_state = self.machine()
        with mock.patch.object(devices.shutil, "which", return_value=None):
            self.assertTrue(any("git" in need for need in devices.detect(plain, plain_state)["levels"]["computers"]["needs"]))

    @unittest.skipUnless(GIT, "git is required")
    def test_cli_plans_and_applies_the_computers_level(self):
        vault, state = self.icloud_machine()
        git_dir = self.tmp / "notes.git"
        cli = CliTest(methodName="run_cli")
        cli.home = self.home
        code, out, err = cli.run_cli(vault, state, "devices", "--plan", "--level", "computers", "--phone-sync",
                                     "icloud", "--git-dir", str(git_dir))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["actions"][0]["kind"], "init_git_dir")
        code, out, err = cli.run_cli(vault, state, "devices", "--apply", "--level", "computers", "--phone-sync",
                                     "icloud", "--git-dir", str(git_dir))
        self.assertEqual(code, 0, err)
        self.assertTrue(devices.is_git_dir(vault, git_dir))
        code, _, err = cli.run_cli(vault, state, "devices", "--git-dir", str(git_dir))
        self.assertEqual(code, 1)
        self.assertIn("only valid with --plan or --apply", err)



class MachineFilesTest(Base):
    # Installation files carry one machine's paths (docs/v3/MULTI-MACHINE.md: every machine installs
    # Beyin itself). Git keeps them out through the git directory's info/exclude and info/attributes,
    # so nothing is written into the vault; the managed block keeps the user's own lines byte for byte.

    @unittest.skipUnless(GIT, "git is required")
    def test_rules_live_in_the_external_git_directory_not_in_the_vault(self):
        vault = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes"
        vault.mkdir(parents=True)
        (vault / "beyin.py").write_text("# machine\n", encoding="utf-8")
        state = self.tmp / "state"
        state.mkdir()
        git_dir = self.tmp / "git" / "notes.git"
        options = {"level": "computers", "phone_sync": "icloud", "git_dir": str(git_dir)}
        planned = devices.public_plan(devices.plan(vault, state, options))
        self.assertNotIn("content", json.dumps(planned))
        before = self.tree(vault)
        self.assertEqual(devices.apply(vault, state, options)["status"], "applied")
        self.assertEqual(self.tree(vault), before)
        exclude = (git_dir / "info/exclude").read_text(encoding="utf-8")
        self.assertIn(devices.BLOCK_START + "\n", exclude)
        self.assertIn("\nbeyin.py\n", exclude)
        self.assertIn("receipts/** -text", (git_dir / "info/attributes").read_text(encoding="utf-8"))
        self.assertEqual(git(vault, "--git-dir=" + str(git_dir), "--work-tree=" + str(vault), "check-ignore", "beyin.py"),
                         "beyin.py")
        self.assertEqual(devices.apply(vault, state, options)["status"], "unchanged")

    @unittest.skipUnless(GIT, "git is required")
    def test_lines_outside_the_managed_block_keep_their_bytes(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        exclude = vault / ".git/info/exclude"
        exclude.parent.mkdir(exist_ok=True)
        prefix = b"# comment\r\nkeep/\r\n\r\n"
        suffix = "# Arşiv".encode("utf-8")
        exclude.write_bytes(prefix + devices.BLOCK_START.encode() + b"\r\nold/\r\n" +
                            devices.BLOCK_END.encode() + b"\r\n" + suffix)
        self.assertEqual(devices.apply(vault, state, {"level": "computers"})["status"], "applied")
        after = exclude.read_bytes()
        self.assertTrue(after.startswith(prefix), after)
        self.assertTrue(after.endswith(suffix), after)
        self.assertIn(b"\r\n.beyin-runtime.json\r\n", after)
        self.assertNotIn(b"old/", after)
        self.assertFalse((vault / ".gitignore").exists())

    def test_managed_block_is_idempotent_and_keeps_user_lines(self):
        user = "# mine\nsecret/\n.beyin-version\n"
        once = devices._merged_block(user, ["beyin.py", ".beyin-version", "daily/v3/"])
        self.assertTrue(once.startswith(user))
        self.assertEqual(once.count(".beyin-version"), 1)
        self.assertEqual(devices._merged_block(once, ["beyin.py", ".beyin-version", "daily/v3/"]), once)
        shrunk = devices._merged_block(once, ["beyin.py"])
        self.assertNotIn("daily/v3/", shrunk)
        self.assertIn("secret/", shrunk)
        self.assertEqual(devices._merged_block("a\r\nb\r\n", ["c"]).count("\r\n"), 6)
        self.assertEqual(devices._merged_block("keep\n", ["keep"]), "keep\n")
        with self.assertRaisesRegex(devices.DevicesError, "managed block"):
            devices._merged_block(devices.BLOCK_START + "\nkeep/\n", ["wanted/"])

    @unittest.skipUnless(GIT, "git is required")
    def test_an_unclosed_block_stops_apply_before_any_change(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        (vault / ".git/info").mkdir(exist_ok=True)
        (vault / ".git/info/exclude").write_text(devices.BLOCK_START + "\nmine/\n", encoding="utf-8")
        with self.assertRaisesRegex(devices.DevicesError, "managed block.*info/exclude"):
            devices.plan(vault, state, {"level": "computers"})
        self.assertEqual(list(state.iterdir()), [])

    def test_user_files_in_the_scripts_folder_are_not_ignored(self):
        vault, _ = self.machine()
        scripts = vault / ".claude/scripts"
        scripts.mkdir(parents=True)
        (scripts / "beyin_v3_sync.py").write_text("", encoding="utf-8")
        self.assertIn(".claude/scripts/", devices._ignore_lines(vault)[0])
        (scripts / "kendi-senkron.py").write_text("", encoding="utf-8")
        lines, foreign = devices._ignore_lines(vault)
        self.assertEqual(foreign, ["kendi-senkron.py"])
        self.assertNotIn(".claude/scripts/", lines)
        self.assertIn(".claude/scripts/beyin_v3*.py", lines)
        planned = devices.plan(vault, self.machine("b")[1], {"level": "computers", "git_dir": str(self.tmp / "g.git")})
        self.assertTrue(any("kendi-senkron.py" in warning for warning in planned["warnings"]))

    @unittest.skipUnless(GIT, "git is required")
    def test_already_tracked_machine_files_are_reported_not_untracked(self):
        vault, state = self.machine()
        git(vault, "init", "-q", "-b", "main")
        (vault / "beyin.py").write_text("# machine\n", encoding="utf-8")
        git(vault, "add", "beyin.py")
        planned = devices.plan(vault, state, {"level": "computers"})
        self.assertTrue(any("already tracks" in warning and "beyin.py" in warning for warning in planned["warnings"]))
        devices.apply(vault, state, {"level": "computers"})
        self.assertEqual(git(vault, "ls-files"), "beyin.py")


@unittest.skipUnless(GIT, "git is required")
class TwoComputers(Base):
    # Machine A keeps the vault in iCloud (the bridge, external git directory); machine B is a
    # plain computer with an ordinary .git folder. Both share one bare remote.

    def setUp(self):
        super().setUp()
        identity = mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
        identity.start()
        self.addCleanup(identity.stop)
        self.bare = self.tmp / "hub.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.a = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes"
        (self.a / "notes").mkdir(parents=True)
        (self.a / "notes/one.md").write_text("# One\n", encoding="utf-8")
        for name in ("beyin.py", ".beyin-runtime.json", ".claude/scripts/beyin_v3_sync.py", "daily/v3/2026-11-23.md"):
            (self.a / name).parent.mkdir(parents=True, exist_ok=True)
            (self.a / name).write_text("machine a\n", encoding="utf-8")
        self.a_state = self.tmp / "a-state"
        self.a_state.mkdir()
        self.a_git = self.tmp / "a-git" / "notes.git"
        applied = devices.apply(self.a, self.a_state, {"level": "computers", "phone_sync": "icloud",
                                                       "git_dir": str(self.a_git), "remote": str(self.bare)})
        self.assertEqual(applied["status"], "applied", applied)
        first = devices.sync_once(self.a, self.a_state)
        self.assertEqual((first["status"], first["committed"], first["pushed"]), ("ok", True, True), first)
        self.b, self.b_state = self.machine("b")
        git(self.b, "init", "-q", "-b", "main")
        self.assertEqual(devices.apply(self.b, self.b_state, {"level": "computers", "remote": str(self.bare)})["status"],
                         "applied")


class SyncRoundTest(TwoComputers):
    def test_the_bridge_syncs_without_writing_git_data_into_the_vault(self):
        self.assertFalse((self.a / ".git").exists())
        self.assertEqual(git(self.bare, "ls-tree", "-r", "--name-only", "main").splitlines(), ["notes/one.md"])
        record = json.loads((self.a_state / devices.RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "ok")
        self.assertIn("at", record)
        self.assertEqual(devices.status(self.a, self.a_state)["last_sync"]["status"], "ok")

    def test_changes_travel_both_ways_between_the_bridge_and_a_plain_computer(self):
        (self.b / "beyin.py").write_text("machine b\n", encoding="utf-8")
        joined = devices.sync_once(self.b, self.b_state)
        self.assertEqual((joined["status"], joined["pulled"]), ("ok", True), joined)
        self.assertEqual((self.b / "beyin.py").read_text(encoding="utf-8"), "machine b\n")
        self.assertEqual((self.b / "notes/one.md").read_text(encoding="utf-8"), "# One\n")
        (self.b / "notes/two.md").write_text("# Two\n", encoding="utf-8")
        sent = devices.sync_once(self.b, self.b_state)
        self.assertEqual((sent["status"], sent["committed"], sent["pushed"]), ("ok", True, True), sent)
        received = devices.sync_once(self.a, self.a_state)
        self.assertEqual((received["status"], received["pulled"], received["pushed"]), ("ok", True, False), received)
        self.assertTrue((self.a / "notes/two.md").exists())
        self.assertFalse((self.a / ".git").exists())
        self.assertEqual(git(self.bare, "ls-tree", "-r", "--name-only", "main").splitlines(),
                         ["notes/one.md", "notes/two.md"])
        idle = devices.sync_once(self.a, self.a_state)
        self.assertEqual((idle["status"], idle["committed"], idle["pulled"], idle["pushed"]), ("ok", False, False, False))

    def test_a_conflict_leaves_no_markers_and_prints_the_full_git_command(self):
        devices.sync_once(self.b, self.b_state)
        (self.a / "notes/one.md").write_text("# One from A\n", encoding="utf-8")
        (self.b / "notes/one.md").write_text("# One from B\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")
        result = devices.sync_once(self.a, self.a_state)
        self.assertEqual(result["status"], "conflict", result)
        self.assertEqual(result["conflicts"], ["notes/one.md"])
        self.assertEqual((self.a / "notes/one.md").read_text(encoding="utf-8"), "# One from A\n")
        self.assertIsNone(devices._in_progress(self.a_git))
        self.assertFalse((self.a / ".git").exists())
        command = result["resolve_command"]
        self.assertEqual(command, devices.git_command(self.a, self.a_git, "pull", "--rebase", "origin", "main"))
        self.assertIn("--git-dir=" + str(self.a_git), command)
        self.assertIn("--work-tree=" + str(self.a), command)
        self.assertIn(command, result["reason"])
        self.assertEqual(devices.doctor(self.a_state)["last_sync"]["status"], "conflict")
        self.assertIn("age_minutes", devices.doctor(self.a_state)["last_sync"])
        # Resolved by hand (here: take the remote side); the next round goes through.
        devices._git(self.a, "reset", "-q", "--hard", "origin/main", git_dir=self.a_git)
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "ok")

    def test_a_synced_vault_without_a_registered_git_directory_is_refused(self):
        # A second Mac sees the same iCloud vault but is not the bridge.
        other_state = self.tmp / "c-state"
        other_state.mkdir()
        (other_state / devices.SETTINGS_FILE).write_text(json.dumps(
            {**devices.default_settings(), "level": "computers", "phone_sync": "icloud"}), encoding="utf-8")
        with self.assertRaisesRegex(devices.DevicesError, "--git-dir PATH"):
            devices.sync_once(self.a, other_state)
        self.assertEqual(sorted(path.name for path in other_state.iterdir()), [devices.SETTINGS_FILE])

    def test_refuses_while_off_and_writes_nothing(self):
        vault, state = self.machine("c")
        with self.assertRaisesRegex(devices.DevicesError, "devices sync is off"):
            devices.sync_once(vault, state)
        self.assertEqual(list(state.iterdir()), [])

    def test_unreachable_remote_keeps_the_local_commit(self):
        devices.sync_once(self.b, self.b_state)
        git(self.b, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        (self.b / "notes/offline.md").write_text("x\n", encoding="utf-8")
        result = devices.sync_once(self.b, self.b_state)
        self.assertEqual((result["status"], result["committed"]), ("offline", True))
        self.assertEqual(git(self.b, "status", "--porcelain"), "")

    def test_blocked_on_another_branch_or_an_open_merge(self):
        devices._git(self.a, "checkout", "-q", "-b", "deneme", git_dir=self.a_git)
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "blocked")
        devices._git(self.a, "checkout", "-q", "main", git_dir=self.a_git)
        (self.a_git / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
        blocked = devices.sync_once(self.a, self.a_state)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("--git-dir=" + str(self.a_git), blocked["reason"])

    def test_overlapping_rounds_do_not_run_together(self):
        with open(self.b_state / devices.LOCK_FILE, "a+b") as handle:
            self.assertTrue(devices._lock(handle))
            self.assertEqual(devices.sync_once(self.b, self.b_state), {"status": "already_running"})

    def test_credentials_in_git_messages_are_redacted(self):
        self.assertEqual(devices._tail("fatal: https://me:tok@example.com/x.git not found"),
                         "fatal: https://example.com/x.git not found")

    def test_cli_runs_one_round(self):
        cli = CliTest(methodName="run_cli")
        cli.home = self.home
        (self.a / "notes/cli.md").write_text("cli\n", encoding="utf-8")
        code, out, err = cli.run_cli(self.a, self.a_state, "devices", "--sync-once")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "ok")
        self.assertIn("notes/cli.md", git(self.bare, "ls-tree", "-r", "--name-only", "main"))



def fake_scheduler(argv):
    """Stands in for launchctl, systemctl and schtasks: nothing is registered yet, loads succeed."""
    if argv[:2] in (["launchctl", "print"], ["schtasks", "/Query"]):
        return 1, ""
    if "--property=LoadState" in argv:
        return 0, "not-found"
    return 0, ""


class SchedulerSpecTest(Base):
    """What a scheduled round looks like on each platform; nothing is registered."""

    def spec(self, kind, interval=15):
        return devices.scheduler_spec(self.tmp / "v", self.tmp / "s", interval, kind=kind, home=self.home,
                                      command=["/usr/bin/python3", "/v a/beyin.py", "devices", "--sync-once"])

    def test_launchd_plist(self):
        import plistlib
        spec = self.spec("launchd")
        (path, content), = spec["files"].items()
        data = plistlib.loads(content.encode("utf-8"))
        self.assertEqual((data["StartInterval"], data["ProgramArguments"][-1]), (900, "--sync-once"))
        self.assertTrue(path.startswith(str(self.home / "Library/LaunchAgents")))
        self.assertIn(spec["label"], path)

    def test_systemd_units_quote_arguments(self):
        spec = self.spec("systemd-user", 30)
        service = next(v for k, v in spec["files"].items() if k.endswith(".service"))
        timer = next(v for k, v in spec["files"].items() if k.endswith(".timer"))
        self.assertIn('ExecStart="/usr/bin/python3" "/v a/beyin.py" "devices" "--sync-once"', service)
        self.assertIn("OnUnitActiveSec=30min", timer)

    def test_windows_task_and_fallbacks(self):
        spec = self.spec("schtasks")
        self.assertEqual(spec["load"][0][:2], ["schtasks", "/Create"])
        self.assertIn('"/v a/beyin.py"', spec["load"][0][-1])
        self.assertIn("*/15 * * * *", self.spec("cron")["manual"])
        self.assertIn("sleep 900", self.spec("none")["manual"])
        self.assertEqual(self.spec("none")["files"], {})

    def test_scheduler_kind_per_platform(self):
        self.assertEqual(devices.scheduler_kind("darwin"), "launchd")
        self.assertEqual(devices.scheduler_kind("win32"), "schtasks")
        with mock.patch.object(devices.shutil, "which", return_value=None):
            self.assertEqual(devices.scheduler_kind("linux", {}), "none")

    def test_two_install_paths_get_their_own_label(self):
        first = devices.scheduler_spec(self.tmp / "a/vault", self.tmp / "a/state", 15, kind="launchd", home=self.home)
        second = devices.scheduler_spec(self.tmp / "Users/b/Notlar Ölçüm", self.tmp / "b/state", 15, kind="launchd",
                                        home=self.home)
        self.assertNotEqual(first["label"], second["label"])
        self.assertNotEqual(set(first["files"]), set(second["files"]))


@unittest.skipUnless(GIT, "git is required")
class SchedulerTest(Base):
    """Install, record and remove the sync schedule. Scheduler files go to a temporary folder and
    the load commands are stubbed: no real scheduler is touched."""

    def setUp(self):
        super().setUp()
        self.scheduler_dir = self.tmp / "Scheduler Ölçüm"
        self.bare = self.tmp / "hub.git"
        git(self.tmp, "init", "-q", "--bare", str(self.bare))

    def git_machine(self, name="a"):
        vault, state = self.machine(name)
        git(vault, "init", "-q", "-b", "main")
        return vault, state

    def apply(self, vault, state, runner=fake_scheduler, **options):
        return devices.apply(vault, state, {"level": "computers", "remote": str(self.bare), **options},
                             home=self.home, scheduler_dir=self.scheduler_dir, runner=runner)

    def test_schedule_is_installed_recorded_repeated_and_removed(self):
        vault, state = self.git_machine()
        with mock.patch.object(devices, "scheduler_kind", return_value="systemd-user"):
            result = self.apply(vault, state, interval=15)
            self.assertEqual(result["status"], "applied", result)
            units = sorted(self.scheduler_dir.iterdir())
            self.assertEqual([path.suffix for path in units], [".service", ".timer"])
            stored = devices.status(vault, state)
            self.assertEqual((stored["interval"], stored["scheduler"]["kind"]), (15, "systemd-user"))
            self.assertEqual(devices.doctor(state)["interval"], 15)
            run = mock.Mock(return_value=(0, ""))
            repeated = self.apply(vault, state, runner=run, interval=15)
            self.assertEqual(repeated["status"], "unchanged", repeated)
            run.assert_not_called()
            content = units[0].read_bytes()
            units[0].write_bytes(content + b"# changed\n")
            with self.assertRaisesRegex(ValueError, "changed file"):
                devices.remove_scheduler(vault, state, scheduler_dir=self.scheduler_dir, runner=run)
            run.assert_not_called()
            units[0].write_bytes(content)
            removed = devices.remove_scheduler(vault, state, scheduler_dir=self.scheduler_dir, runner=run)
            self.assertEqual(removed["status"], "removed")
            self.assertEqual(run.call_args_list[0].args[0][:4], ["systemctl", "--user", "disable", "--now"])
        self.assertEqual(list(self.scheduler_dir.iterdir()), [])
        self.assertEqual((devices.status(vault, state)["interval"], devices.status(vault, state)["scheduler"]), (0, None))
        self.assertEqual(devices.remove_scheduler(vault, state, scheduler_dir=self.scheduler_dir)["status"], "unchanged")

    def test_a_new_interval_replaces_the_schedule_and_solo_removes_it(self):
        vault, state = self.git_machine()
        with mock.patch.object(devices, "scheduler_kind", return_value="launchd"):
            self.apply(vault, state, interval=15)
            self.assertEqual(self.apply(vault, state, interval=30)["status"], "applied")
            import plistlib
            plist, = self.scheduler_dir.iterdir()
            self.assertEqual(plistlib.loads(plist.read_bytes())["StartInterval"], 1800)
            run = mock.Mock(side_effect=fake_scheduler)
            result = devices.apply(vault, state, {"level": "solo"}, home=self.home,
                                   scheduler_dir=self.scheduler_dir, runner=run)
            self.assertEqual(result["status"], "applied", result)
            self.assertEqual(run.call_args_list[0].args[0][:2], ["launchctl", "bootout"])
        self.assertEqual(list(self.scheduler_dir.iterdir()), [])
        self.assertEqual(devices._read_ownership(state), {})

    def test_two_computers_on_different_paths_keep_separate_schedules(self):
        a, a_state = self.git_machine("a")
        b, b_state = self.git_machine("Users/b/Notlar Ölçüm")
        with mock.patch.object(devices, "scheduler_kind", return_value="launchd"):
            self.apply(a, a_state, interval=15)
            self.apply(b, b_state, interval=20)
            self.assertEqual(len(list(self.scheduler_dir.iterdir())), 2)
            devices.remove_scheduler(a, a_state, scheduler_dir=self.scheduler_dir, runner=fake_scheduler)
        left, = self.scheduler_dir.iterdir()
        self.assertIn(devices.status(b, b_state)["scheduler"]["label"], left.name)

    def test_failed_load_keeps_settings_and_a_rerun_finishes(self):
        vault, state = self.git_machine()
        self.apply(vault, state)
        before = (state / devices.SETTINGS_FILE).read_text(encoding="utf-8")
        failing = lambda argv: (1, "Failed to connect to bus") if "enable" in argv else fake_scheduler(argv)
        with mock.patch.object(devices, "scheduler_kind", return_value="systemd-user"):
            with self.assertRaisesRegex(ValueError, "Failed to connect to bus"):
                self.apply(vault, state, runner=failing, interval=15)
            self.assertEqual((state / devices.SETTINGS_FILE).read_text(encoding="utf-8"), before)
            self.assertFalse(next(iter(devices._read_ownership(state).values()))["active"])
            self.assertEqual(self.apply(vault, state, interval=15)["status"], "applied")
        self.assertTrue(next(iter(devices._read_ownership(state).values()))["active"])

    def test_a_file_with_our_name_but_no_ownership_record_is_left_alone(self):
        vault, state = self.git_machine()
        spec = devices.scheduler_spec(vault, state, 15, kind="launchd", scheduler_dir=self.scheduler_dir)
        (path, content), = spec["files"].items()
        Path(path).parent.mkdir(parents=True)
        Path(path).write_text(content, encoding="utf-8")
        run = mock.Mock()
        with mock.patch.object(devices, "scheduler_kind", return_value="launchd"), \
                self.assertRaisesRegex(ValueError, "already exists"):
            self.apply(vault, state, runner=run, interval=15)
        run.assert_not_called()
        self.assertEqual(Path(path).read_text(encoding="utf-8"), content)

    def test_invalid_labels_are_refused_before_any_path_or_command(self):
        vault, state = self.machine()
        run = mock.Mock()
        for label in ("", "beyin-devices-İstanbul", "beyin-devices-a b", "beyin-devices-../abc"):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "invalid label"):
                devices._scheduler_paths(vault, state, "systemd-user", label, self.scheduler_dir)
        run.assert_not_called()
        self.assertFalse(self.scheduler_dir.exists())

    def test_windows_refuses_to_delete_a_replaced_task(self):
        vault, state = self.machine()
        spec = devices.scheduler_spec(vault, state, 15, kind="schtasks", scheduler_dir=self.scheduler_dir)
        runner = mock.Mock(side_effect=[(1, ""), (0, ""), (0, "<Task>ours</Task>")])
        devices._install_scheduler(vault, state, spec, self.scheduler_dir, runner, self.home)
        (state / devices.SETTINGS_FILE).write_text(json.dumps({
            **devices.default_settings(), "level": "computers", "interval": 15,
            "scheduler": {"kind": "schtasks", "label": spec["label"]}}), encoding="utf-8")
        path = Path(next(iter(spec["files"])))
        runner.reset_mock(side_effect=True)
        runner.return_value = (0, "<Task>replacement</Task>")
        with self.assertRaisesRegex(ValueError, "task definition changed"):
            devices.remove_scheduler(vault, state, scheduler_dir=self.scheduler_dir, runner=runner)
        runner.assert_called_once_with(["schtasks", "/Query", "/TN", spec["label"], "/XML"])
        self.assertTrue(path.exists())
        runner.reset_mock()
        runner.return_value = (0, "<Task>ours</Task>")
        self.assertEqual(devices.remove_scheduler(vault, state, scheduler_dir=self.scheduler_dir, runner=runner)["status"], "removed")
        self.assertIn(mock.call(["schtasks", "/Delete", "/F", "/TN", spec["label"]]), runner.call_args_list)
        self.assertFalse(path.exists())

    def test_interval_rules(self):
        vault, state = self.git_machine()
        for options, message in (({"level": "phone", "phone_sync": "icloud", "interval": 15}, "--interval belongs"),
                                 ({"level": "computers", "interval": 2}, "0 \\(off\\) or 5..1440"),
                                 ({"level": "computers", "interval": True}, "0 \\(off\\) or 5..1440")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                devices.plan(vault, state, options)
        planned = devices.plan(vault, state, {"level": "computers"})
        self.assertTrue(any("--interval 15" in step for step in planned["manual_steps"]))

    def test_the_icloud_bridge_schedules_only_with_its_external_git_directory(self):
        vault = self.home / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes"
        vault.mkdir(parents=True)
        (vault / "note.md").write_text("# not\n", encoding="utf-8")
        state = self.tmp / "state"
        state.mkdir()
        before = self.tree(vault)
        with mock.patch.object(devices, "scheduler_kind", return_value="launchd"):
            with self.assertRaisesRegex(ValueError, "git directory outside"):
                devices.plan(vault, state, {"level": "computers", "phone_sync": "icloud", "interval": 15},
                             home=self.home, scheduler_dir=self.scheduler_dir)
            self.assertFalse(self.scheduler_dir.exists())
            result = self.apply(vault, state, phone_sync="icloud", git_dir=str(self.tmp / "notes.git"), interval=15)
        self.assertEqual(result["status"], "applied", result)
        self.assertTrue(any("Full Disk Access" in warning for warning in result["warnings"]))
        self.assertEqual(len(list(self.scheduler_dir.iterdir())), 1)
        self.assertEqual(self.tree(vault), before)


class SchedulerCliTest(Base):
    run_cli = CliTest.run_cli

    def test_interval_and_remove_schedule_options(self):
        vault, state = self.machine()
        code, _, err = self.run_cli(vault, state, "devices", "--interval", "15")
        self.assertEqual(code, 1)
        self.assertIn("only valid with --plan or --apply", err)
        code, out, err = self.run_cli(vault, state, "devices", "--remove-schedule")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "unchanged")
        code, _, err = self.run_cli(vault, state, "devices", "--plan", "--level", "computers", "--interval", "2")
        self.assertEqual(code, 1)
        self.assertIn("5..1440", err)


class OfflineRecoveryTest(TwoComputers):
    # The two computers keep the vault on different paths (A: iCloud bridge with an external git
    # directory, B: plain folder with .git). Each works offline, then both come back.

    def go_offline(self):
        missing = str(self.tmp / "unreachable.git")
        devices._git(self.a, "remote", "set-url", "origin", missing, git_dir=self.a_git)
        git(self.b, "remote", "set-url", "origin", missing)

    def go_online(self):
        devices._git(self.a, "remote", "set-url", "origin", str(self.bare), git_dir=self.a_git)
        git(self.b, "remote", "set-url", "origin", str(self.bare))

    def pasted_env(self):
        return inherited_env(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_EDITOR="true")

    def test_offline_edits_on_both_computers_meet_after_reconnecting(self):
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")
        self.assertNotEqual(str(self.a), str(self.b))
        self.go_offline()
        (self.a / "notes/from-a.md").write_text("A offline\n", encoding="utf-8")
        (self.b / "notes/from-b.md").write_text("B offline\n", encoding="utf-8")
        for vault, state in ((self.a, self.a_state), (self.b, self.b_state)):
            result = devices.sync_once(vault, state)
            self.assertEqual((result["status"], result["committed"]), ("offline", True), result)
            self.assertEqual(devices.doctor(state)["last_sync"]["status"], "offline")
        # A second offline round has nothing new to commit and loses nothing.
        self.assertEqual(devices.sync_once(self.a, self.a_state)["committed"], False)
        self.go_online()
        first = devices.sync_once(self.a, self.a_state)
        self.assertEqual((first["status"], first["pushed"]), ("ok", True), first)
        second = devices.sync_once(self.b, self.b_state)
        self.assertEqual((second["status"], second["pulled"], second["pushed"]), ("ok", True, True), second)
        third = devices.sync_once(self.a, self.a_state)
        self.assertEqual((third["status"], third["pulled"]), ("ok", True), third)
        expected = ["notes/from-a.md", "notes/from-b.md", "notes/one.md"]
        self.assertEqual(git(self.bare, "ls-tree", "-r", "--name-only", "main").splitlines(), expected)
        for vault in (self.a, self.b):
            self.assertEqual((vault / "notes/from-a.md").read_text(encoding="utf-8"), "A offline\n")
            self.assertEqual((vault / "notes/from-b.md").read_text(encoding="utf-8"), "B offline\n")
        self.assertFalse((self.a / ".git").exists())
        self.assertEqual(devices._git(self.a, "status", "--porcelain", git_dir=self.a_git).stdout, "")
        self.assertEqual(git(self.b, "status", "--porcelain"), "")

    def test_an_offline_conflict_is_recovered_with_the_printed_command(self):
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")
        git(self.b, "remote", "set-url", "origin", str(self.tmp / "unreachable.git"))
        (self.b / "notes/one.md").write_text("# One from B offline\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "offline")
        (self.a / "notes/one.md").write_text("# One from A\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.a, self.a_state)["pushed"], True)
        git(self.b, "remote", "set-url", "origin", str(self.bare))
        local = git(self.b, "rev-parse", "HEAD")
        result = devices.sync_once(self.b, self.b_state)
        self.assertEqual((result["status"], result["conflicts"]), ("conflict", ["notes/one.md"]), result)
        # Nothing is lost or half-applied: the offline commit stays, no markers, no open rebase.
        self.assertEqual(git(self.b, "rev-parse", "HEAD"), local)
        self.assertEqual((self.b / "notes/one.md").read_text(encoding="utf-8"), "# One from B offline\n")
        self.assertIsNone(devices._in_progress(self.b / ".git"))
        self.assertEqual(git(self.b, "status", "--porcelain"), "")
        # The person pastes the printed command, edits the file, and finishes the rebase.
        env = self.pasted_env()
        pasted = subprocess.run(shlex.split(result["resolve_command"]), capture_output=True, text=True, env=env)
        self.assertNotEqual(pasted.returncode, 0)
        self.assertIn("<<<<<<<", (self.b / "notes/one.md").read_text(encoding="utf-8"))
        (self.b / "notes/one.md").write_text("# One from A and B\n", encoding="utf-8")
        git(self.b, "add", "notes/one.md")
        subprocess.run([GIT, "-C", str(self.b), "rebase", "--continue"], check=True, capture_output=True, env=env)
        resolved = devices.sync_once(self.b, self.b_state)
        self.assertEqual((resolved["status"], resolved["pushed"]), ("ok", True), resolved)
        back = devices.sync_once(self.a, self.a_state)
        self.assertEqual((back["status"], back["pulled"]), ("ok", True), back)
        self.assertEqual((self.a / "notes/one.md").read_text(encoding="utf-8"), "# One from A and B\n")
        self.assertFalse((self.a / ".git").exists())

    def test_the_bridge_recovers_a_conflict_through_its_external_git_directory(self):
        self.assertEqual(devices.sync_once(self.b, self.b_state)["status"], "ok")
        devices._git(self.a, "remote", "set-url", "origin", str(self.tmp / "unreachable.git"), git_dir=self.a_git)
        (self.a / "notes/one.md").write_text("# One from A offline\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "offline")
        (self.b / "notes/one.md").write_text("# One from B\n", encoding="utf-8")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["pushed"], True)
        devices._git(self.a, "remote", "set-url", "origin", str(self.bare), git_dir=self.a_git)
        result = devices.sync_once(self.a, self.a_state)
        self.assertEqual(result["status"], "conflict", result)
        env = self.pasted_env()
        # Pasted from another folder: the command carries --git-dir and --work-tree itself.
        command = shlex.split(result["resolve_command"])
        self.assertEqual(command[-4:], ["pull", "--rebase", "origin", "main"])
        pasted = subprocess.run(command, capture_output=True, text=True, env=env, cwd=str(self.tmp))
        self.assertNotEqual(pasted.returncode, 0)
        self.assertIn("<<<<<<<", (self.a / "notes/one.md").read_text(encoding="utf-8"))
        self.assertFalse((self.a / ".git").exists())
        (self.a / "notes/one.md").write_text("# One merged\n", encoding="utf-8")
        for args in (["add", "notes/one.md"], ["rebase", "--continue"]):
            subprocess.run(command[:-4] + args, check=True, capture_output=True, env=env, cwd=str(self.tmp))
        self.assertEqual(devices.sync_once(self.a, self.a_state)["status"], "ok")
        self.assertEqual(devices.sync_once(self.b, self.b_state)["pulled"], True)
        self.assertEqual((self.b / "notes/one.md").read_text(encoding="utf-8"), "# One merged\n")
        self.assertFalse((self.a / ".git").exists())


if __name__ == "__main__":
    unittest.main()
