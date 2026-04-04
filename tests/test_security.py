import ast
import unittest
from pathlib import Path


def load_functions(path: Path, function_names: set[str]):
    tree = ast.parse(path.read_text())
    selected = []
    for node in tree.body:
      if isinstance(node, ast.Assign):
          targets = {target.id for target in node.targets if isinstance(target, ast.Name)}
          if targets & {"ALLOWED_FIELDS"}:
              selected.append(node)
      elif isinstance(node, ast.FunctionDef) and node.name in function_names:
          selected.append(node)
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {}
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace


class SecurityHelpersTest(unittest.TestCase):
    def test_scheduled_ingest_normalizes_urls(self):
        namespace = load_functions(Path('scheduled/ingest.py'), {'safe_url', 'normalize_record'})
        normalize_record = namespace['normalize_record']
        safe_url = namespace['safe_url']

        self.assertTrue(safe_url('https://example.com'))
        self.assertFalse(safe_url('javascript:alert(1)'))

        cleaned = normalize_record({
            'description': 'demo',
            'refUrls': 'javascript:alert(1), https://example.com, ftp://bad',
            'novelty': 'novel',
        })
        self.assertEqual(cleaned['refUrls'], 'https://example.com')

        self.assertIsNone(normalize_record({'description': 'demo', 'refUrls': 'ftp://bad'}))

    def test_functions_ingest_normalizes_urls(self):
        namespace = load_functions(Path('functions/main.py'), {'safe_url', 'normalize_record', 'normalize_suggestion_url'})
        normalize_record = namespace['normalize_record']
        normalize_suggestion_url = namespace['normalize_suggestion_url']

        cleaned = normalize_record({
            'description': 'demo',
            'refUrls': 'https://example.com, javascript:alert(1)',
            'sourceUser': 'user',
            'category': 'Research',
        })
        self.assertEqual(cleaned['refUrls'], 'https://example.com')

        self.assertEqual(
            normalize_suggestion_url('https://example.com/path/?q=1#frag'),
            'https://example.com/path?q=1'
        )
        self.assertIsNone(normalize_suggestion_url('mailto:test@example.com'))


if __name__ == '__main__':
    unittest.main()
