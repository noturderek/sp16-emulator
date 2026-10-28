"""Where a build may write (prepare.private_path): this checkout, or a worktree of the same repository.

Real worktrees and real repositories in a temporary folder, because the question is what git says about the path, and a
fake cannot answer it. Nothing here touches the checkout's own worktree list when it ends.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import prepare


def git(cwd, *arguments, check=True):
    return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=check)


class PrivatePathTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        os.chmod(self.dir, 0o700)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def worktree(self, name="sibling"):
        """A real worktree of this repository, outside it (as ../sp16-emulator-worktrees/ is)."""
        path = self.dir / name
        git(prepare.REPO, "worktree", "add", "--detach", str(path), "HEAD")
        self.addCleanup(lambda: git(prepare.REPO, "worktree", "remove", "--force", str(path), check=False))
        return path

    def test_this_checkouts_own_workspace_is_accepted(self):
        self.assertEqual(prepare.private_path(prepare.REPO / "build"), prepare.REPO / "build")

    def test_a_sibling_worktrees_library_and_qemu_are_accepted(self):
        """The reported bug: a build in ../sp16-emulator-worktrees/<lane> could not reuse the main checkout's
        build/sp16/library or build/qemu-source, because both must be Git-ignored inside the same repository."""
        worktree = self.worktree()
        build = worktree / "build"
        library, qemu = build / "sp16/library", build / "qemu-source"
        for path in (build, library, qemu):
            path.mkdir(parents=True)
            os.chmod(path, 0o700)  # mkdir keeps the umask; private_path wants owner-only
        for path in (library, qemu):
            self.assertEqual(prepare.private_path(path), path)
        # and an output that does not exist yet, as --name creates it
        self.assertEqual(prepare.private_path(build / "lane", fresh=True), build / "lane")

    def test_a_plain_directory_outside_the_repository_is_refused(self):
        with self.assertRaisesRegex(ValueError, "worktree"):
            prepare.private_path(self.dir / "build")

    def test_another_repository_that_looks_the_same_is_refused(self):
        other = self.dir / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / ".gitignore").write_text("build/\n")
        (other / "build").mkdir()
        with self.assertRaisesRegex(ValueError, "worktree"):
            prepare.private_path(other / "build")

    def test_a_worktree_path_that_is_not_git_ignored_is_refused(self):
        worktree = self.worktree()
        with self.assertRaisesRegex(ValueError, "Git-ignored"):
            prepare.private_path(worktree / "emulator/workspace")

    def test_symlink_parents_are_still_refused(self):
        worktree = self.worktree()
        (worktree / "build").mkdir()
        link = self.dir / "link"
        link.symlink_to(worktree / "build")
        with self.assertRaisesRegex(ValueError, "symlink"):
            prepare.private_path(link / "library")

    def test_a_fresh_output_needs_an_owner_only_parent(self):
        worktree = self.worktree()
        loose = worktree / "build"
        loose.mkdir()
        os.chmod(loose, 0o755)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            prepare.private_path(loose / "lane", fresh=True)


if __name__ == "__main__":
    unittest.main()
