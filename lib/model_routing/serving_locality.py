"""Pure serving-location classification from owned route facts, never model names.

Used by compiled Python/native routes and the idle context policy. Explicit
local identity wins; a declared cloud brand or subscription remains cloud even
when its transport uses a loopback proxy. Unbranded IP endpoints retain the
existing local-discovery interpretation.
"""


def is_local_inference(*, protocol='', credential_kind='', brand='', base_url=''):
    if protocol == 'local' or credential_kind == 'local_identity' or brand == 'local':
        return True
    if brand or credential_kind in {'oauth', 'subscription'}:
        return False
    from lib.llm_dispatch.discovery._url import is_local_endpoint, is_raw_ip_host
    return bool(base_url) and (is_local_endpoint(base_url) or is_raw_ip_host(base_url))


def candidate_is_local_inference(candidate):
    return is_local_inference(
        protocol=candidate.connection.get('protocol', ''),
        credential_kind=candidate.credential.get('kind', ''),
        brand=candidate.provider.get('brand', ''),
        base_url=candidate.connection.get('base_url', ''))
