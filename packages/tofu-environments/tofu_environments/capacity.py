"""Pure queue-summary normalization shared by providers and resource presenters.

No credentials, network, allocation or inference. The caller supplies the exact
expected queue; provider catalog/schema admission validates numeric quantities.
"""
def queue_resources(response, expected_queue):
    if (not isinstance(response, dict) or response.get('queue') != expected_queue
            or not isinstance(response.get('summary'), dict)):
        raise ValueError('Queue identity and summary required')
    summary = response['summary']
    gpu = summary.get('gpu', {})
    if not isinstance(gpu, dict):
        raise ValueError('Invalid GPU summary')
    resources = []
    for name, values in gpu.items():
        if not isinstance(values, dict):
            raise ValueError('Invalid GPU resource')
        resources.append({'kind': 'gpu', 'name': name, 'unit': 'count',
                          'total': values.get('total'), 'allocated': values.get('allocated'),
                          'available': values.get('remain'),
                          **({'pending': values['pending']} if 'pending' in values else {})})
    for kind, name, unit, suffix in [('cpu', 'vcores', 'count', ''), ('memory', 'memory', 'MiB', '_mb')]:
        resources.append({'kind': kind, 'name': name, 'unit': unit,
                          'total': summary.get(name + '_total' + suffix),
                          'allocated': summary.get(name + '_allocated' + suffix),
                          'available': summary.get(name + '_remain' + suffix)})
    return resources
