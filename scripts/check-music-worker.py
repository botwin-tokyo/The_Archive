"""Run in the isolated worker venv; no network is needed for these tests."""
import ast
import pathlib
import subprocess
import sys
root=pathlib.Path(__file__).resolve().parents[1]
for path in (root/'apps/music-worker/src').rglob('*.py'):
    tree=ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or '']
            if any(name.startswith('archive_backend') for name in names):raise SystemExit('Worker must not import the credentialed core')
import os
environment=dict(os.environ,PYTHONPATH=str(root/'apps/music-worker/src'))
subprocess.run([sys.executable,'-m','unittest','discover','-s',str(root/'apps/music-worker/tests'),'-v'],env=environment,check=True)
