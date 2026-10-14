"""Check the agreed SDK boundary in the simplified runtime's integration tests."""
import argparse
import ast
from pathlib import Path


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted_name(node.value) + '.' + node.attr
    return ''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    root = parser.parse_args().root
    errors = []
    files = sorted(set((root / 'tests').glob('simple_*runner.py')) | set((root / 'tests').glob('test_simple_*.py')))
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        aliases = {alias.asname or alias.name for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module == 'gateway.run'
                   for alias in node.names if alias.name == 'GatewayRunner'}
        aliases.add('gateway.run.GatewayRunner')
        aliases.update(alias.asname + '.GatewayRunner' for node in ast.walk(tree) if isinstance(node, ast.Import)
                       for alias in node.names if alias.name == 'gateway.run' and alias.asname)
        instances = {target.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and isinstance(node.value, ast.Call) and dotted_name(node.value.func) in aliases
                     for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            subclass = isinstance(node, ast.ClassDef) and any(dotted_name(base) in aliases for base in node.bases)
            patch = (isinstance(node, ast.Call) and dotted_name(node.func).split('.')[-1] == 'setattr'
                     and node.args and dotted_name(node.args[0]) in aliases | instances)
            assignment = (isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute)
                          and dotted_name(target.value) in aliases | instances and target.attr.startswith('_')
                          for target in node.targets))
            if subclass or patch or assignment:
                errors.append((path.relative_to(root), node.lineno))
    for path, line in errors:
        print(f'{path}:{line}: Use the original GatewayRunner and replace external services only.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
