"""Project step-owned numeric inputs into the operation's public JSON schema.

Defaults and intrinsic bounds come from the manifest contract. Explicit pack
bounds intersect with the authored input schema, so the shared adapter validator
can refuse invalid inputs before a lease, grant or browser effect. Execution
borrows the same bindings; it never silently clamps an admitted argument.
"""

from copy import deepcopy

from ._contract import schema_validator, step_properties


def walk_steps(steps):
    for step in steps or ():
        yield step
        if step['do'] == 'try':
            yield from walk_steps(step['steps'])


def prepare_steps(steps) -> tuple[dict, ...]:
    prepared = deepcopy(tuple(steps))
    for step in walk_steps(prepared):
        for name, rule in step_properties(step['do']).items():
            if 'default' in rule:
                step.setdefault(name, deepcopy(rule['default']))
    return prepared


def _parameter_rules(step: dict) -> dict[str, tuple[str | None, dict]]:
    """Map executable numeric fields to their parameter name and value rule."""
    kind = step['do']
    if kind == 'research':
        return {name: (step.get(f'{name}_from_param'),
                       {**step_properties(kind)[name], 'default': step[name]})
                for name in ('max_pages', 'max_chars')}
    if kind == 'paginate':
        rule = schema_validator().schema['$defs']['pagination_page_count']
        return {'pages': (step.get('times_from_param'),
                          {**rule, 'maximum': step['max_times'] + 1})}
    if kind == 'normalize' and step['kind'] == 'search_records':
        rule = schema_validator().schema['$defs']['search_result_count']
        return {'limit': (step['limit_from_param'], {
            **rule, 'maximum': step['limit_max'],
            'default': min(rule['default'], step['limit_max'])})}
    if kind == 'normalize' and step['kind'] == 'detail':
        rule = step_properties(kind)['max_chars_default']
        return {'max_chars': (step.get('max_chars_from_param') or 'max_chars',
                              {**rule, 'default': step['max_chars_default']})}
    return {}


def operation_input_schema(authored: dict, steps) -> dict:
    schema = deepcopy(authored)
    constraints = {}
    for step in walk_steps(steps):
        for name, rule in _parameter_rules(step).values():
            if not name:
                continue
            bound = constraints.setdefault(name, {'type': rule['type']})
            for key, combine in (('minimum', max), ('maximum', min)):
                if key in rule:
                    bound[key] = combine(bound.get(key, rule[key]), rule[key])
    if constraints:
        schema.setdefault('allOf', []).append({'properties': constraints})
    return schema


def parameter_value(step, params, field: str) -> int:
    name, rule = _parameter_rules(step)[field]
    return int(rule['default'] if name is None else params.get(name, rule['default']))
