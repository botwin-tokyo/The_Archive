"""Backend syntax, static import-cycle and adapter boundary guard."""
import ast
import pathlib
import sys
root=pathlib.Path(__file__).resolve().parents[1]/'apps/backend/src/archive_backend'
files=list(root.glob('*.py')); graph={p.stem:set() for p in files}
for file in files:
    tree=ast.parse(file.read_text(),filename=str(file))
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom) and node.level and node.module:
            module=node.module.split('.')[0]
            if file.stem=='contracts' and module!='contracts': raise SystemExit(f'{file}: inverted contract dependency')
            if file.stem in ('http_adapter','mcp_adapter') and module in ('filesystem','volumes','persistence'): raise SystemExit(f'{file}: adapter bypasses application')
    # Composition callbacks/worker subprocess entry points deliberately import their
    # concrete composition lazily. Type-only annotations have no runtime dependency.
    for node in tree.body:
        if isinstance(node,ast.ImportFrom) and node.level and node.module: graph[file.stem].add(node.module.split('.')[0])
def visit(node,trail):
    if node in trail: raise SystemExit('Dependency cycle: '+' -> '.join(trail+[node]))
    for child in graph.get(node,()): visit(child,trail+[node])
for key in graph: visit(key,[])
print(f'Backend syntax/dependency guard passed: {len(files)} modules; runtime {sys.version.split()[0]}')
