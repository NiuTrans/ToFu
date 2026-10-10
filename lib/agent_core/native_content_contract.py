"""Generated native content/account schemas; sources: native_agent_stdio_v1.schema.json, native_agent_checkpoint_v1.schema.json."""

MODEL_CONTENT_SCHEMAS = {'text': {'type': 'object',
          'required': ['type', 'text'],
          'properties': {'type': {'enum': ['text']}, 'text': {'type': 'string'}},
          'additionalProperties': False},
 'image_url': {'type': 'object',
               'required': ['type', 'image_url'],
               'properties': {'type': {'enum': ['image_url']},
                              'image_url': {'type': 'object',
                                            'required': ['url'],
                                            'properties': {'url': {'type': 'string',
                                                                   'minLength': 1},
                                                           'detail': {'enum': ['auto',
                                                                               'low',
                                                                               'high']}},
                                            'additionalProperties': False}},
               'additionalProperties': False}}

MODEL_DISPATCH_FORBIDDEN_HEADERS = ('host', 'content-length', 'transfer-encoding', 'connection', 'proxy-authorization')

SHARED_ACCOUNT_SCHEMA = {'type': 'object',
 'required': ['format',
              'owner_user_id',
              'root_task_id',
              'tokens',
              'tool_calls',
              'control_tool_calls',
              'model_ordinal',
              'tool_ordinal',
              'complete',
              'limits',
              'counter_policy'],
 'properties': {'format': {'enum': ['tofu.native-shared-account/v1']},
                'owner_user_id': {'type': 'integer', 'minimum': 1, 'maximum': 18446744073709551615},
                'root_task_id': {'type': 'string', 'minLength': 1},
                'tokens': {'type': 'integer', 'minimum': 0, 'maximum': 18446744073709551615},
                'tool_calls': {'type': 'integer', 'minimum': 0, 'maximum': 18446744073709551615},
                'control_tool_calls': {'type': 'integer',
                                       'minimum': 0,
                                       'maximum': 18446744073709551615},
                'model_ordinal': {'type': 'integer', 'minimum': 0, 'maximum': 18446744073709551615},
                'tool_ordinal': {'type': 'integer', 'minimum': 0, 'maximum': 18446744073709551615},
                'complete': {'type': 'boolean'},
                'limits': {'type': 'object',
                           'required': ['max_tokens', 'max_tool_calls', 'deadline'],
                           'properties': {'max_tokens': {'type': ['integer', 'null'],
                                                         'minimum': 0,
                                                         'maximum': 18446744073709551615},
                                          'max_tool_calls': {'type': ['integer', 'null'],
                                                             'minimum': 0,
                                                             'maximum': 18446744073709551615},
                                          'deadline': {'type': ['number', 'null'], 'minimum': 0}},
                           'additionalProperties': False},
                'counter_policy': {'type': 'object',
                                   'required': ['encoding', 'output_field', 'wire_model'],
                                   'properties': {'encoding': {'enum': ['cl100k_base',
                                                                        'o200k_base']},
                                                  'output_field': {'enum': ['max_tokens',
                                                                            'max_completion_tokens']},
                                                  'wire_model': {'type': 'string', 'minLength': 1}},
                                   'additionalProperties': False}},
 'additionalProperties': False}

MODEL_TOOL_PROJECTION = {'type': 'function',
 'catalog_name_field': 'name',
 'description_field': 'description',
 'parameters_field': 'arguments_schema',
 'missing_description': '',
 'order': 'catalog'}

def native_model_tools(tools):
    """Project the canonical native catalog exactly like the generated Rust owner."""
    rule = MODEL_TOOL_PROJECTION
    return [{'type': rule['type'], 'function': {
        'name': name,
        'description': tool.get(rule['description_field']) if isinstance(tool.get(rule['description_field']), str) else rule['missing_description'],
        'parameters': tool.get(rule['parameters_field']),
    }} for name, tool in tools.items()]
