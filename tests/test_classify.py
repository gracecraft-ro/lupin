import unittest

from lupin import classify


class ClassifyTests(unittest.TestCase):
    def test_cad_keyword_forces_cad_spatial(self):
        issue = {
            "title": "Fix the extrude step in the parametric sketch tool",
            "body": "The assembly geometry is wrong after a mesh rebuild.",
            "labels": [{"name": "size-m"}],
        }

        category, size = classify.classify(issue)

        self.assertEqual(category, "cad-spatial")
        self.assertEqual(size, "size-m")

    def test_ui_screenshot_signal_forces_frontend_ui(self):
        issue = {
            "title": "Button color is wrong",
            "body": "See attached screenshot, the CSS variable is unused.",
            "labels": [{"name": "size-s"}],
        }

        category, size = classify.classify(issue)

        self.assertEqual(category, "frontend-ui")
        self.assertEqual(size, "size-s")

    def test_translation_keyword_routes_to_translation(self):
        issue = {
            "title": "Localization strings missing for the settings page",
            "body": "We need to translate the new onboarding copy.",
            "labels": [],
        }

        category, _size = classify.classify(issue)

        self.assertEqual(category, "translation")

    def test_no_signal_defaults_to_coding(self):
        issue = {
            "title": "Fix off-by-one in the pagination cursor",
            "body": "The cursor skips the last page under load.",
            "labels": [{"name": "size-l"}],
        }

        category, size = classify.classify(issue)

        self.assertEqual(category, "coding")
        self.assertEqual(size, "size-l")

    def test_cad_keyword_wins_over_ui_signal(self):
        issue = {
            "title": "Screenshot shows wrong fillet in the CAD viewer",
            "body": "The UI screenshot shows a wrong parametric extrude on the sketch.",
            "labels": [],
        }

        category, _size = classify.classify(issue)

        self.assertEqual(category, "cad-spatial")

    def test_step_file_keyword_is_case_sensitive(self):
        # "step" as an ordinary word (lowercase) must not trigger CAD routing.
        issue = {
            "title": "Document the next step in the release process",
            "body": "This is just a plain coding task, no 3D files involved.",
            "labels": [],
        }

        category, _size = classify.classify(issue)

        self.assertEqual(category, "coding")

        issue["body"] = "Import the part from a STEP file before rendering."
        category, _size = classify.classify(issue)

        self.assertEqual(category, "cad-spatial")

    def test_diff_stat_downgrades_trivial_large_label(self):
        issue = {
            "title": "Tweak a config default",
            "body": "Bump a timeout value.",
            "labels": [{"name": "size-l"}],
        }
        diff_stat = (
            " config.nix | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        )

        _category, size = classify.classify(issue, diff_stat=diff_stat)

        self.assertEqual(size, "size-xs")

    def test_binary_diff_files_count_toward_size_and_block_xs_downgrade(self):
        # A diff touching many binary files but few text lines must not be
        # undercounted as "1 file" just because the stat line ends in
        # "bytes" instead of a +/- count.
        issue = {
            "title": "Update icon assets",
            "body": "Swap out the stale PNGs.",
            "labels": [{"name": "size-l"}],
        }
        diff_stat = (
            " icon1.png | Bin 0 -> 12345 bytes\n"
            " icon2.png | Bin 0 -> 6789 bytes\n"
            " icon3.png | Bin 100 -> 200 bytes\n"
            " 3 files changed, 0 insertions(+), 0 deletions(-)\n"
        )

        _category, size = classify.classify(issue, diff_stat=diff_stat)

        self.assertEqual(size, "size-l")

    def test_general_is_never_inferred_from_keywords(self):
        # "general" is a real row in model-tiers.json, but classify() has no
        # signal that tells "general reasoning" apart from "coding" -- every
        # issue it sees is tied to a code diff. This test documents that the
        # keyword scan defaults to "coding", not "general", even for a task
        # that reads like planning/architecture rather than a code fix.
        issue = {
            "title": "Decide on the long-term approach for model routing",
            "body": "No code change yet, just need to pick a direction.",
            "labels": [],
        }

        category, _size = classify.classify(issue)

        self.assertEqual(category, "coding")


if __name__ == "__main__":
    unittest.main()
