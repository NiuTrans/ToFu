"""Acronym extraction + precision stoplists for the terminology audit.

Holds the candidate-acronym regex and the three curated frozensets that keep the
gate low-false-positive (``_STOPWORDS`` / ``_WELL_KNOWN_ACRONYMS`` /
``_COMMON_WORDS``), plus the pure helpers that operate on a single token or on
raw text: ``_has_two_caps`` / ``_is_common_word`` / ``_strip_code`` /
``_extract_acronyms`` / ``_is_named_entity``. All stateless (regex + frozenset
consts + pure functions).
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

# A candidate specialist acronym: 2+ characters, contains at least two capital
# letters (so ``SFT`` / ``RLHF`` / ``PPO`` / ``KV`` qualify but a sentence-initial
# ``The`` does not), may carry internal digits (``GPT4``, ``F1``). Hyphens are
# NOT part of the token, so a compound like ``SFT-only`` splits into ``SFT``
# (checked) + ``only`` (dropped for lacking two caps) — the acronym core is what
# a glossary row would define, never the prose suffix.
_ACRONYM_RE = re.compile(r'\b[A-Za-z][A-Za-z0-9]*\b')

# Common English words / report-scaffolding tokens that happen to be all-caps or
# multi-cap but are NOT specialist terminology. Kept small and generic (no
# paper-specific terms) so the gate stays low-false-positive without hiding real
# gaps. All compared upper-cased.
_STOPWORDS = frozenset({
    'TL', 'DR', 'FAQ', 'OK', 'ID', 'IDS', 'URL', 'URLS', 'API', 'APIS',
    'CPU', 'GPU', 'GPUS', 'TPU', 'TPUS', 'RAM', 'IO', 'OS', 'PDF', 'HTML',
    'JSON', 'CSV', 'HTTP', 'HTTPS', 'AI', 'ML', 'NLP', 'US', 'UK', 'EU',
    'AM', 'PM', 'UTC', 'FYI', 'ETC', 'VS', 'EG', 'IE', 'AKA', 'NA', 'TODO',
})

# Audience-level, field-general acronyms an ML/NLP paper's reader needs no
# glossary for — standard metrics, optimizers, model families, and math objects.
# Deliberately GENERIC (no paper-specific or environment-specific terms, per the
# no-hardcoded-values rule): these are the vocabulary of the field, not of any
# one paper. A term here is treated as "meaning already available to the
# audience" and never flagged as a missing gap. Compared upper-cased.
_WELL_KNOWN_ACRONYMS = frozenset({
    # metrics
    'BLEU', 'ROUGE', 'METEOR', 'CIDER', 'BERTSCORE', 'PPL', 'FID', 'IS', 'MAP',
    'AUC', 'AUROC', 'F1', 'MSE', 'MAE', 'RMSE', 'MAPE', 'NLL', 'CE', 'KL', 'MI',
    'ELBO', 'WER', 'CER', 'EM', 'ACC', 'PSNR', 'SSIM', 'SNR', 'IOU', 'MRR',
    'NDCG', 'SOTA',
    # optimizers / training
    'SGD', 'ADAM', 'ADAMW', 'ADAGRAD', 'RMSPROP', 'LR', 'EMA', 'BN', 'LN',
    'DROPOUT', 'MLE', 'MAP', 'ERM',
    # model families / architectures (household names in the field)
    'GPT', 'BERT', 'ROBERTA', 'T5', 'BART', 'LLM', 'LLMS', 'RNN', 'CNN', 'LSTM',
    'GRU', 'MLP', 'MLPS', 'GAN', 'VAE', 'MOE', 'VIT', 'CLIP', 'RESNET',
    'TRANSFORMER', 'SSM',
    # math / objects
    'ODE', 'SDE', 'PDE', 'IID', 'KKT', 'PCA', 'SVD', 'EM', 'MCMC', 'RL', 'IL',
    'DP', 'RELU', 'GELU', 'SILU', 'TANH', 'SOFTMAX',
})


# Ordinary English words + report-scaffolding tokens that frequently appear in
# ALL-CAPS via markdown emphasis, section headers, or filenames (``EVIDENCE``,
# ``BASED``, ``README``) and would otherwise be mis-flagged as specialist
# terminology. This is GENERIC English (the same spirit as ``_STOPWORDS`` — not
# environment- or paper-specific), deliberately EXCLUDING anything that is a
# real specialist acronym (AIME/APPS/MT/PR/KV/DDP stay flagged). It is a
# curated stoplist, not a full dictionary — a rare all-caps English word could
# still slip through, which is acceptable (a stray card row, not a wrong
# definition). Compared upper-cased.
_COMMON_WORDS = frozenset({
    'AND', 'OR', 'NOT', 'BUT', 'FOR', 'NOR', 'YET', 'SO', 'THE', 'AN', 'OF',
    'TO', 'IN', 'ON', 'AT', 'BY', 'AS', 'IS', 'ARE', 'WAS', 'WERE', 'BE',
    'BEEN', 'WITH', 'FROM', 'INTO', 'ONTO', 'THIS', 'THAT', 'THESE', 'THOSE',
    'IT', 'ITS', 'ALL', 'ANY', 'EACH', 'MORE', 'MOST', 'SUCH', 'NO', 'ONLY',
    'OWN', 'SAME', 'THAN', 'THEN', 'ONCE', 'HERE', 'WHEN', 'WHERE', 'WHY',
    'HOW', 'BOTH', 'FEW', 'NEW', 'OLD', 'GOOD', 'BAD', 'BEST', 'WORST',
    'BASED', 'EVIDENCE', 'README', 'NOTE', 'NOTES', 'WARNING', 'CAUTION',
    'IMPORTANT', 'SUMMARY', 'OVERVIEW', 'RESULTS', 'METHOD', 'METHODS',
    'ABSTRACT', 'INTRO', 'CONCLUSION', 'APPENDIX', 'REFERENCES', 'TODO',
    'FIXME', 'YES', 'TRUE', 'FALSE', 'NONE', 'NULL', 'MEAN', 'SUM', 'MIN',
    'MAX', 'AVG', 'STD', 'STEP', 'STEPS', 'DONE', 'PASS', 'FAIL', 'ERROR',
    'INPUT', 'OUTPUT', 'DATA', 'CODE', 'TEXT', 'IMAGE', 'MODEL', 'TRAIN',
    'TEST', 'VALID', 'EVAL', 'LOSS', 'GAIN', 'RATE', 'SIZE', 'TIME', 'COST',
})


def _has_two_caps(token: str) -> bool:
    return sum(1 for c in token if c.isupper()) >= 2


def _is_common_word(term: str) -> bool:
    """True if ``term`` is an ordinary English / scaffolding word, not a term."""
    return term.upper() in _COMMON_WORDS


def _strip_code(text: str) -> str:
    """Remove fenced + inline code so identifiers inside code are never flagged."""
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)
    text = re.sub(r'`[^`\n]*`', ' ', text)
    return text


def _extract_acronyms(text: str) -> set[str]:
    """Return the set of candidate specialist acronyms in ``text``."""
    out: set[str] = set()
    for m in _ACRONYM_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < 2 or not _has_two_caps(tok):
            continue
        if tok.upper() in _STOPWORDS:
            continue
        out.add(tok)
    return out


def _is_named_entity(term: str) -> bool:
    """True if ``term`` is a MIXED-CASE (CamelCase) proper-noun-style label.

    A token that mixes upper- and lower-case letters — ``SeqDiffuSeq``,
    ``OpenWebText``, ``RoPE``, ``adaLN``, ``MeanFlow`` — is a named system /
    dataset / module (a proper noun), not a concept ACRONYM the reader must be
    taught to follow the paper. These are the field's product names; a reader
    treats them like any cited system. Genuine specialist acronyms are ALL-CAPS
    (``SFT``, ``DDPM``, ``RLHF``) and are NOT affected by this rule, so the real
    gaps a glossary should cover still surface.
    """
    has_upper = any(c.isupper() for c in term)
    has_lower = any(c.islower() for c in term)
    return has_upper and has_lower
