"""Build all declared outputs before the CLI can publish any of them.
Entry: build_outputs; OUTPUT_PATHS is the sole output-path map. Input snapshot lifetime is one call."""

from .inputs import ContractInputs
from .validation import load_ir
from .auxiliary import load_worker
from .native import render_native
from .paper import render_paper
from .search import render_search
from .python_outputs import render_migration, render_receipts, render_worker, render_billing

OUTPUT_PATHS = {
    'receipts': 'lib/storage_authority_sql/generated_storage_receipts.py',
    'paper': 'packages/tofu-db/src/generated_paper_build.rs',
    'ir': 'packages/tofu-db/src/generated_tofudb_ir.rs',
    'migration': 'lib/storage_authority_sql/migration/generated_tofudb_migration.py',
    'search': 'packages/tofu-db/src/generated_conversation_search.rs',
    'worker': 'lib/storage/generated_worker_job_contract.py',
    'clock': 'lib/storage_authority_sql/native_worker_clock/generated_clock_contract.h',
    'billing': 'lib/storage_authority_sql/generated_billing_contract.py',
}


def build_outputs(inputs: ContractInputs) -> dict[str, str]:
    contract = load_ir(inputs)
    migration = render_migration(contract)
    search, volume_guard = render_search(inputs.read_json(inputs.search_source))
    migration.append(f'NATIVE_SEARCH_VOLUME_GUARD = {volume_guard!r}')
    receipts = render_receipts(contract)
    worker = load_worker(inputs)
    worker_text, clock = render_worker(contract, worker)
    outputs = {
        OUTPUT_PATHS['receipts']: receipts,
        OUTPUT_PATHS['paper']: render_paper(inputs.contract('paper_build_v1.schema.json'), contract),
        OUTPUT_PATHS['ir']: render_native(contract, worker, inputs),
        OUTPUT_PATHS['migration']: '\n'.join(migration) + '\n',
        OUTPUT_PATHS['search']: search,
        OUTPUT_PATHS['worker']: worker_text,
        OUTPUT_PATHS['clock']: clock,
        OUTPUT_PATHS['billing']: render_billing(contract),
    }
    return outputs
