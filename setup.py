"""Build-only projection of canonical schemas required by the installed agent.

The checkout contracts remain authoritative. build_py copies exact bytes into
package data; sdists retain these inputs so wheels can be built independently.
"""
from pathlib import Path
from setuptools import setup
from setuptools.command.build_py import build_py


class AgentBuildPy(build_py):
    @staticmethod
    def contracts():
        root = Path(__file__).resolve().parent
        # Keep canonical relative paths for every packaged domain reader.
        # Only machine-readable schemas ship; guides and tools stay in source.
        return tuple(str(path.relative_to(root)) for path in sorted((root / 'contracts').iterdir())
                     if path.is_file() and path.suffix in {'.json', '.yaml', '.yml'})

    def run(self):
        super().run()
        for source in self.contracts():
            destination = Path(self.build_lib) / source
            self.mkpath(str(destination.parent))
            self.copy_file(str(Path(__file__).resolve().parent / source), str(destination))

    def get_outputs(self, include_bytecode=True):
        return super().get_outputs(include_bytecode) + [
            str(Path(self.build_lib) / source) for source in self.contracts()]


if __name__ == '__main__':
    setup(cmdclass={'build_py': AgentBuildPy})
