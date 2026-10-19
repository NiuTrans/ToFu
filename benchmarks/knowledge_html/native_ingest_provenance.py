"""Frozen database provenance for the private ingestion measurement harness.

Entry: DatabaseProvenance.capture/verify bracket worker cohorts outside clocks.
The owned producer receives the same receipt; each corpus still compares the
loaded library's exported identity. This receipt is benchmark configuration,
not an application admission credential or a substitute for post-run checks.
"""
from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path

from serverctl_pkg.native_engine import source_fingerprint

CLOCK_SCOPE = 'tofu.native-ingest-clock/v3'


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class DatabaseProvenance:
    source_sha256: str
    engine_path: str
    engine_sha256: str
    library_path: str
    library_sha256: str

    def __post_init__(self):
        for value in (self.source_sha256, self.engine_sha256, self.library_sha256):
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('database provenance requires SHA256 pins')
        if not Path(self.engine_path).is_absolute() or self.library_path != str(Path(self.engine_path).with_name('libtofu_db.so')):
            raise ValueError('database provenance requires absolute adjacent artifacts')

    @classmethod
    def capture(cls, checkout, engine):
        engine = Path(engine).resolve()
        library = engine.with_name('libtofu_db.so')
        return cls(source_fingerprint(checkout), str(engine), file_sha256(engine),
                   str(library), file_sha256(library))

    @classmethod
    def from_receipt(cls, receipt):
        # Pure decoding for our owned child. Its parent verifies before and
        # after the cohort; decoding itself does not attest any file bytes.
        return cls(**receipt)

    def receipt(self):
        return asdict(self)

    def verify(self, checkout, engine):
        if self != self.capture(checkout, engine):
            raise RuntimeError('database sources or artifacts changed during ingestion measurement')

    def expected_identity(self, engine):
        if str(Path(engine).resolve()) != self.engine_path:
            raise RuntimeError('database provenance belongs to a different engine')
        return {'source_sha256': self.source_sha256}
