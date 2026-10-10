"""Typed proof-domain failures. One closed code set, no parallel taxonomy."""


class ProofError(Exception):
    """Raised when a proof bundle or an assembly input is invalid.

    ``code`` is one of:

    - ``proof_contract`` — a bundle violates the wire authority schema.
    - ``proof_owner`` — assembly was requested without a positive owner.
    - ``proof_assembly`` — an evidence authority returned a malformed fact
      (a bug or corruption upstream, not missing evidence; missing evidence
      is a coverage reason, never an exception).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
