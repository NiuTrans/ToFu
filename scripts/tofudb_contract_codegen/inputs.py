"""One invocation owns one parsed revision of each contract file.
Entry: ContractInputs. Disk reads are explicit and lazy; nothing is cached across runs."""

import json
from pathlib import Path


class ContractInputs:
    def __init__(self, root: Path, ir_source: Path, search_source: Path):
        self.root = root
        self.ir_source = ir_source
        self.search_source = search_source
        self._documents: dict[Path, dict] = {}

    def read_json(self, path: Path) -> dict:
        if path not in self._documents:
            self._documents[path] = json.loads(path.read_text(encoding='utf-8'))
        return self._documents[path]

    def contract(self, name: str) -> dict:
        return self.read_json(self.root / 'contracts' / name)
