from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import unittest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet"
PLATFORM_FILES = ("binary_sensor", "button", "number", "select", "sensor", "switch", "time")


def _entity_classes():
    """Each platform's entity classes: the ones built on CoordinatorEntity."""
    for platform in PLATFORM_FILES:
        tree = ast.parse((COMPONENT_PATH / f"{platform}.py").read_text())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any("CoordinatorEntity" in ast.unparse(base) for base in node.bases):
                yield platform, node


class EntityNamingTests(unittest.TestCase):
    def test_every_entity_is_named_after_its_device(self):
        for platform, node in _entity_classes():
            assigned = {
                target.id: ast.unparse(statement.value)
                for statement in node.body
                if isinstance(statement, ast.Assign)
                for target in statement.targets
                if isinstance(target, ast.Name)
            }
            with self.subTest(entity=f"{platform}.{node.name}"):
                self.assertEqual(assigned.get("_attr_has_entity_name"), "True")

    def test_every_translation_key_has_a_name_in_each_language(self):
        keys = {
            key
            for platform in PLATFORM_FILES
            for key in re.findall(r'translation_key="([^"]+)"', (COMPONENT_PATH / f"{platform}.py").read_text())
        }
        for language in ("en", "uk"):
            entity = json.loads((COMPONENT_PATH / "translations" / f"{language}.json").read_text())["entity"]
            named = {key for platform in entity.values() for key, value in platform.items() if value.get("name")}
            with self.subTest(language=language):
                self.assertEqual(keys - named, set())

    def test_strings_match_the_english_translation(self):
        self.assertEqual(
            json.loads((COMPONENT_PATH / "strings.json").read_text()),
            json.loads((COMPONENT_PATH / "translations" / "en.json").read_text()),
        )


if __name__ == "__main__":
    unittest.main()
