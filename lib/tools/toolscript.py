"""A small, bounded ToolScript parser and interpreter.

No Python/JavaScript ``eval``, host VM, imports, attribute reflection, or
third-party sandbox is involved.  Only the grammar and built-ins below exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


MAX_SOURCE_BYTES = 32 * 1024
MAX_AST_NODES = 2_000
MAX_STEPS = 100_000
MAX_TOOL_CALLS = 16
MAX_CONCURRENT_CALLS = 8
MAX_OUTPUT_BYTES = 1_048_576
MAX_NESTING = 32

_FORBIDDEN_MEMBERS = frozenset({
    '__proto__', 'prototype', 'constructor', '__class__', '__dict__',
    '__globals__', '__builtins__', 'mro', 'subclasses',
})


class ToolScriptError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'message': str(self), **self.detail}


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    offset: int


def _read_string(source: str, start: int) -> tuple[str, int]:
    quote = source[start]
    out: list[str] = []
    i = start + 1
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
               '\\': '\\', '/': '/', '"': '"', "'": "'"}
    while i < len(source):
        char = source[i]
        if char == quote:
            return ''.join(out), i + 1
        if char != '\\':
            out.append(char)
            i += 1
            continue
        i += 1
        if i >= len(source):
            break
        escaped = source[i]
        if escaped == 'u':
            digits = source[i + 1:i + 5]
            if len(digits) != 4:
                raise ToolScriptError('syntax_error', 'invalid unicode escape',
                                      offset=i)
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                raise ToolScriptError('syntax_error', 'invalid unicode escape',
                                      offset=i)
            i += 5
            continue
        out.append(escapes.get(escaped, escaped))
        i += 1
    raise ToolScriptError('syntax_error', 'unterminated string', offset=start)


def tokenize(source: str) -> list[Token]:
    if len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        raise ToolScriptError('source_limit', 'ToolScript exceeds 32 KiB',
                              limit=MAX_SOURCE_BYTES)
    out: list[Token] = []
    i = 0
    multi = ('===', '!==', '=>', '<=', '>=', '==', '!=', '&&', '||', '??')
    singles = set('{}[]().,;:?+-*/%!<>=')
    while i < len(source):
        char = source[i]
        if char.isspace():
            i += 1
            continue
        if source.startswith('//', i):
            end = source.find('\n', i + 2)
            i = len(source) if end < 0 else end + 1
            continue
        if source.startswith('/*', i):
            end = source.find('*/', i + 2)
            if end < 0:
                raise ToolScriptError('syntax_error', 'unterminated comment',
                                      offset=i)
            i = end + 2
            continue
        if char in ('"', "'"):
            value, end = _read_string(source, i)
            out.append(Token('string', value, i))
            i = end
            continue
        if char.isdigit() or (char == '.' and i + 1 < len(source)
                              and source[i + 1].isdigit()):
            start = i
            saw_dot = False
            while i < len(source) and (source[i].isdigit()
                                       or source[i] in '.eE+-'):
                if source[i] == '.':
                    if saw_dot:
                        break
                    saw_dot = True
                if source[i] in '+-' and i > start \
                        and source[i - 1] not in 'eE':
                    break
                i += 1
            raw = source[start:i]
            try:
                value = float(raw) if any(c in raw for c in '.eE') else int(raw)
            except ValueError:
                raise ToolScriptError('syntax_error', f'invalid number {raw!r}',
                                      offset=start)
            out.append(Token('number', value, start))
            continue
        if char.isalpha() or char in '_$':
            start = i
            i += 1
            while i < len(source) and (source[i].isalnum()
                                       or source[i] in '_$'):
                i += 1
            out.append(Token('ident', source[start:i], start))
            continue
        operator = next((op for op in multi if source.startswith(op, i)), None)
        if operator:
            out.append(Token('op', operator, i))
            i += len(operator)
            continue
        if char in singles:
            out.append(Token('op', char, i))
            i += 1
            continue
        raise ToolScriptError('syntax_error', f'unexpected character {char!r}',
                              offset=i)
    out.append(Token('eof', '', len(source)))
    return out


class Parser:
    _PRECEDENCE = {
        '??': 1, '||': 2, '&&': 3,
        '==': 4, '!=': 4, '===': 4, '!==': 4,
        '<': 5, '<=': 5, '>': 5, '>=': 5,
        '+': 6, '-': 6, '*': 7, '/': 7, '%': 7,
    }

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self.nodes = 0
        self.depth = 0

    def _node(self, kind: str, *values: Any) -> tuple:
        self.nodes += 1
        if self.nodes > MAX_AST_NODES:
            raise ToolScriptError('ast_limit', 'ToolScript AST is too large',
                                  limit=MAX_AST_NODES)
        return (kind, *values)

    def peek(self, value: str | None = None) -> Token | bool:
        token = self.tokens[self.pos]
        return token.value == value if value is not None else token

    def take(self, value: str | None = None) -> Token:
        token = self.tokens[self.pos]
        if value is not None and token.value != value:
            raise ToolScriptError(
                'syntax_error', f'expected {value!r}, got {token.value!r}',
                offset=token.offset)
        self.pos += 1
        return token

    def match(self, value: str) -> bool:
        if self.peek(value):
            self.pos += 1
            return True
        return False

    def parse(self) -> tuple:
        statements = []
        while not self.peek(''):
            statements.append(self.statement())
        return self._node('program', statements)

    def block(self) -> tuple:
        self.take('{')
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise ToolScriptError('nesting_limit', 'ToolScript nesting is too deep',
                                  limit=MAX_NESTING)
        statements = []
        while not self.peek('}'):
            if self.peek(''):
                raise ToolScriptError('syntax_error', 'unterminated block',
                                      offset=self.tokens[self.pos].offset)
            statements.append(self.statement())
        self.take('}')
        self.depth -= 1
        return self._node('block', statements)

    def statement(self) -> tuple:
        token = self.peek()
        if token.value in ('let', 'const', 'var'):
            self.take()
            name = self.take()
            if name.kind != 'ident':
                raise ToolScriptError('syntax_error', 'expected variable name',
                                      offset=name.offset)
            value = self._node('literal', None)
            if self.match('='):
                value = self.expression()
            self.match(';')
            return self._node('declare', name.value, value)
        if token.value == 'return':
            self.take()
            value = (self._node('literal', None)
                     if self.peek(';') or self.peek('}') else self.expression())
            self.match(';')
            return self._node('return', value)
        if token.value == 'if':
            self.take(); self.take('(')
            condition = self.expression(); self.take(')')
            yes = self.block() if self.peek('{') else self.statement()
            no = None
            if self.match('else'):
                no = self.block() if self.peek('{') else self.statement()
            return self._node('if', condition, yes, no)
        if token.value == 'for':
            self.take(); self.take('(')
            if self.peek().value in ('let', 'const', 'var'):
                self.take()
            name = self.take()
            if name.kind != 'ident':
                raise ToolScriptError('syntax_error', 'expected loop variable',
                                      offset=name.offset)
            self.take('of')
            iterable = self.expression(); self.take(')')
            body = self.block() if self.peek('{') else self.statement()
            return self._node('for', name.value, iterable, body)
        if token.value == 'while':
            self.take(); self.take('(')
            condition = self.expression(); self.take(')')
            body = self.block() if self.peek('{') else self.statement()
            return self._node('while', condition, body)
        if token.value in ('break', 'continue'):
            self.take(); self.match(';')
            return self._node(token.value)
        if token.value == '{':
            return self.block()
        if token.kind == 'ident' and self.tokens[self.pos + 1].value == '=':
            name = self.take().value; self.take('=')
            value = self.expression(); self.match(';')
            return self._node('assign', name, value)
        expression = self.expression()
        self.match(';')
        return self._node('expr', expression)

    def expression(self, minimum: int = 0) -> tuple:
        left = self.unary()
        while True:
            op = str(self.peek().value)
            precedence = self._PRECEDENCE.get(op, -1)
            if precedence < minimum:
                break
            self.take()
            right = self.expression(precedence + 1)
            left = self._node('binary', op, left, right)
        if minimum == 0 and self.match('?'):
            yes = self.expression(); self.take(':'); no = self.expression()
            left = self._node('conditional', left, yes, no)
        return left

    def unary(self) -> tuple:
        if self.peek().value in ('!', '-', '+', 'await'):
            op = self.take().value
            return self._node('unary', op, self.unary())
        return self.postfix(self.primary())

    def _parenthesized_lambda(self) -> tuple | None:
        save = self.pos
        self.take('(')
        params = []
        if not self.peek(')'):
            while True:
                token = self.take()
                if token.kind != 'ident':
                    self.pos = save
                    return None
                params.append(token.value)
                if not self.match(','):
                    break
        if not self.match(')') or not self.match('=>'):
            self.pos = save
            return None
        body = self.block() if self.peek('{') else self.expression()
        return self._node('lambda', params, body)

    def primary(self) -> tuple:
        token = self.peek()
        if token.kind == 'number' or token.kind == 'string':
            self.take()
            return self._node('literal', token.value)
        if token.value in ('true', 'false', 'null'):
            self.take()
            return self._node('literal', {'true': True, 'false': False,
                                          'null': None}[token.value])
        if token.kind == 'ident':
            self.take()
            if self.match('=>'):
                body = self.block() if self.peek('{') else self.expression()
                return self._node('lambda', [token.value], body)
            return self._node('name', token.value)
        if token.value == '(':
            candidate = self._parenthesized_lambda()
            if candidate is not None:
                return candidate
            self.take('('); value = self.expression(); self.take(')')
            return value
        if token.value == '[':
            self.take(); values = []
            if not self.peek(']'):
                while True:
                    values.append(self.expression())
                    if not self.match(','):
                        break
            self.take(']')
            return self._node('array', values)
        if token.value == '{':
            self.take(); pairs = []
            if not self.peek('}'):
                while True:
                    key = self.take()
                    if key.kind not in ('ident', 'string'):
                        raise ToolScriptError('syntax_error',
                                              'object key must be a string',
                                              offset=key.offset)
                    if key.value in _FORBIDDEN_MEMBERS:
                        raise ToolScriptError('unsafe_member',
                                              f'forbidden key {key.value!r}')
                    self.take(':')
                    pairs.append((key.value, self.expression()))
                    if not self.match(','):
                        break
            self.take('}')
            return self._node('object', pairs)
        raise ToolScriptError('syntax_error',
                              f'unexpected token {token.value!r}',
                              offset=token.offset)

    def postfix(self, value: tuple) -> tuple:
        while True:
            if self.match('.'):
                member = self.take()
                if member.kind != 'ident':
                    raise ToolScriptError('syntax_error', 'expected member name',
                                          offset=member.offset)
                if member.value in _FORBIDDEN_MEMBERS:
                    raise ToolScriptError('unsafe_member',
                                          f'forbidden member {member.value!r}')
                value = self._node('member', value, member.value)
            elif self.match('['):
                key = self.expression(); self.take(']')
                value = self._node('index', value, key)
            elif self.match('('):
                args = []
                if not self.peek(')'):
                    while True:
                        args.append(self.expression())
                        if not self.match(','):
                            break
                self.take(')')
                value = self._node('call', value, args)
            else:
                break
        return value


@dataclass(frozen=True)
class _Namespace:
    name: str


@dataclass(frozen=True)
class _Bound:
    owner: Any
    name: str


@dataclass(frozen=True)
class _Lambda:
    params: list[str]
    body: tuple
    env: dict[str, Any]


class _Signal(Exception):
    def __init__(self, kind: str, value: Any = None):
        self.kind = kind
        self.value = value


class Interpreter:
    def __init__(
        self,
        *,
        search: Callable[..., Any],
        call: Callable[[str, Any, str | None], Any],
        call_many: Callable[[Any, str], Any],
        aborted: Callable[[], bool] | None = None,
    ):
        self.search = search
        self.call_tool = call
        self.call_many = call_many
        self.aborted = aborted or (lambda: False)
        self.steps = 0
        self.tool_calls = 0
        self.env: dict[str, Any] = {
            'catalog': _Namespace('catalog'), 'tools': _Namespace('tools'),
        }
        self.last = None

    def step(self, amount: int = 1) -> None:
        self.steps += amount
        if self.steps > MAX_STEPS:
            raise ToolScriptError('step_limit', 'ToolScript step limit exceeded',
                                  limit=MAX_STEPS)
        if self.aborted():
            raise ToolScriptError('cancelled', 'ToolScript cancelled')

    def run(self, ast: tuple) -> Any:
        try:
            self.exec_node(ast)
        except _Signal as signal:
            if signal.kind == 'return':
                self.last = signal.value
            else:
                raise ToolScriptError('invalid_control_flow',
                                      f'{signal.kind} outside a loop')
        raw = json.dumps(self.last, ensure_ascii=False, default=str)
        if len(raw.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ToolScriptError('output_limit', 'ToolScript output exceeds 1 MiB',
                                  limit=MAX_OUTPUT_BYTES)
        return self.last

    def exec_node(self, node: tuple) -> Any:
        self.step()
        kind = node[0]
        if kind in ('program', 'block'):
            for statement in node[1]:
                self.last = self.exec_node(statement)
            return self.last
        if kind == 'declare' or kind == 'assign':
            value = self.eval_node(node[2])
            self.env[node[1]] = value
            return value
        if kind == 'expr':
            return self.eval_node(node[1])
        if kind == 'return':
            raise _Signal('return', self.eval_node(node[1]))
        if kind == 'if':
            branch = node[2] if self.eval_node(node[1]) else node[3]
            return self.exec_node(branch) if branch is not None else None
        if kind == 'for':
            iterable = self.eval_node(node[2])
            if not isinstance(iterable, (list, tuple, str, dict)):
                raise ToolScriptError('type_error', 'for..of requires an iterable')
            values = list(iterable.keys()) if isinstance(iterable, dict) else list(iterable)
            for value in values:
                self.step()
                self.env[node[1]] = value
                try:
                    self.exec_node(node[3])
                except _Signal as signal:
                    if signal.kind == 'break': break
                    if signal.kind == 'continue': continue
                    raise
            return None
        if kind == 'while':
            while self.eval_node(node[1]):
                self.step()
                try:
                    self.exec_node(node[2])
                except _Signal as signal:
                    if signal.kind == 'break': break
                    if signal.kind == 'continue': continue
                    raise
            return None
        if kind in ('break', 'continue'):
            raise _Signal(kind)
        raise ToolScriptError('runtime_error', f'unsupported statement {kind}')

    def eval_node(self, node: tuple) -> Any:
        self.step()
        kind = node[0]
        if kind == 'literal': return node[1]
        if kind == 'name':
            if node[1] not in self.env:
                raise ToolScriptError('unknown_name',
                                      f'unknown identifier {node[1]!r}')
            return self.env[node[1]]
        if kind == 'array': return [self.eval_node(x) for x in node[1]]
        if kind == 'object': return {k: self.eval_node(v) for k, v in node[1]}
        if kind == 'lambda': return _Lambda(node[1], node[2], dict(self.env))
        if kind == 'member':
            owner = self.eval_node(node[1]); name = node[2]
            if name in _FORBIDDEN_MEMBERS:
                raise ToolScriptError('unsafe_member', f'forbidden member {name!r}')
            if isinstance(owner, dict): return owner.get(name)
            if name == 'length' and isinstance(owner, (list, str)):
                return len(owner)
            return _Bound(owner, name)
        if kind == 'index':
            owner = self.eval_node(node[1]); key = self.eval_node(node[2])
            if str(key) in _FORBIDDEN_MEMBERS:
                raise ToolScriptError('unsafe_member', f'forbidden key {key!r}')
            try: return owner[key]
            except (KeyError, IndexError, TypeError): return None
        if kind == 'unary':
            value = self.eval_node(node[2]); op = node[1]
            if op == '!': return not bool(value)
            if op == '-': return -value
            if op == '+': return +value
            if op == 'await': return value
        if kind == 'conditional':
            return self.eval_node(node[2] if self.eval_node(node[1]) else node[3])
        if kind == 'binary':
            op = node[1]
            left = self.eval_node(node[2])
            if op == '&&': return self.eval_node(node[3]) if left else left
            if op == '||': return left if left else self.eval_node(node[3])
            if op == '??': return left if left is not None else self.eval_node(node[3])
            right = self.eval_node(node[3])
            operations = {
                '+': lambda: left + right, '-': lambda: left - right,
                '*': lambda: left * right, '/': lambda: left / right,
                '%': lambda: left % right,
                '==': lambda: left == right, '===': lambda: left == right,
                '!=': lambda: left != right, '!==': lambda: left != right,
                '<': lambda: left < right, '<=': lambda: left <= right,
                '>': lambda: left > right, '>=': lambda: left >= right,
            }
            try: return operations[op]()
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                raise ToolScriptError('type_error', f'operator {op}: {exc}')
        if kind == 'call':
            callee = self.eval_node(node[1])
            args = [self.eval_node(arg) for arg in node[2]]
            return self.invoke(callee, args)
        raise ToolScriptError('runtime_error', f'unsupported expression {kind}')

    def _apply_lambda(self, fn: _Lambda, args: list[Any]) -> Any:
        old = self.env
        self.env = dict(fn.env)
        self.env.update(zip(fn.params, args))
        try:
            if fn.body[0] == 'block':
                try: return self.exec_node(fn.body)
                except _Signal as signal:
                    if signal.kind == 'return': return signal.value
                    raise
            return self.eval_node(fn.body)
        finally:
            self.env = old

    def _admit_calls(self, count: int) -> None:
        if self.tool_calls + count > MAX_TOOL_CALLS:
            raise ToolScriptError('tool_call_limit',
                                  'ToolScript tool-call limit exceeded',
                                  limit=MAX_TOOL_CALLS)
        self.tool_calls += count

    def invoke(self, callee: Any, args: list[Any]) -> Any:
        self.step()
        if isinstance(callee, _Lambda):
            return self._apply_lambda(callee, args)
        if isinstance(callee, _Bound):
            owner, name = callee.owner, callee.name
            if isinstance(owner, _Namespace) and owner.name == 'catalog' \
                    and name == 'search':
                return self.search(*args)
            if isinstance(owner, _Namespace) and owner.name == 'tools':
                if name == 'call':
                    self._admit_calls(1)
                    return self.call_tool(
                        str(args[0]) if args else '',
                        args[1] if len(args) > 1 else {},
                        str(args[2]) if len(args) > 2 else None)
                if name in ('callMany', 'batch', 'parallel'):
                    calls = args[0] if args else []
                    if not isinstance(calls, list):
                        raise ToolScriptError('type_error',
                                              'tools.callMany expects an array')
                    self._admit_calls(len(calls))
                    execution = ('parallel' if name == 'parallel' else
                                 (str(args[1]) if len(args) > 1 else 'auto'))
                    return self.call_many(calls, execution)
            if isinstance(owner, list) and name in ('map', 'filter', 'reduce'):
                if not args or not isinstance(args[0], _Lambda):
                    raise ToolScriptError('type_error', f'{name} requires a lambda')
                fn = args[0]
                if name == 'map':
                    return [self._apply_lambda(fn, [value, i, owner])
                            for i, value in enumerate(owner)]
                if name == 'filter':
                    return [value for i, value in enumerate(owner)
                            if self._apply_lambda(fn, [value, i, owner])]
                if len(args) > 1:
                    accumulator, start = args[1], 0
                elif owner:
                    accumulator, start = owner[0], 1
                else:
                    raise ToolScriptError('type_error',
                                          'reduce of empty array without initial value')
                for i in range(start, len(owner)):
                    accumulator = self._apply_lambda(
                        fn, [accumulator, owner[i], i, owner])
                return accumulator
            if isinstance(owner, list) and name == 'length': return len(owner)
            if isinstance(owner, str) and name == 'length': return len(owner)
            if isinstance(owner, str) and name in ('includes', 'startsWith', 'endsWith'):
                needle = str(args[0] if args else '')
                return {'includes': needle in owner,
                        'startsWith': owner.startswith(needle),
                        'endsWith': owner.endswith(needle)}[name]
            if isinstance(owner, str) and name in ('toLowerCase', 'toUpperCase'):
                return owner.lower() if name == 'toLowerCase' else owner.upper()
        raise ToolScriptError('unsafe_call', 'function is not an allowed ToolScript builtin')


def execute_toolscript(
    source: str,
    *,
    search: Callable[..., Any],
    call: Callable[[str, Any, str | None], Any],
    call_many: Callable[[Any, str], Any],
    aborted: Callable[[], bool] | None = None,
) -> tuple[Any, dict[str, int]]:
    try:
        ast = Parser(tokenize(str(source or ''))).parse()
    except RecursionError:
        raise ToolScriptError('nesting_limit',
                              'ToolScript nesting exceeds 32 levels',
                              limit=MAX_NESTING)
    if ParserNodeCounter.depth(ast) > MAX_NESTING:
        raise ToolScriptError('nesting_limit',
                              'ToolScript nesting exceeds 32 levels',
                              limit=MAX_NESTING)
    interpreter = Interpreter(search=search, call=call, call_many=call_many,
                              aborted=aborted)
    result = interpreter.run(ast)
    return result, {'ast_nodes': ParserNodeCounter.count(ast),
                    'steps': interpreter.steps,
                    'tool_calls': interpreter.tool_calls}


class ParserNodeCounter:
    @staticmethod
    def count(node: Any) -> int:
        if not isinstance(node, tuple):
            if isinstance(node, list):
                return sum(ParserNodeCounter.count(x) for x in node)
            return 0
        return 1 + sum(ParserNodeCounter.count(value) for value in node[1:])

    @staticmethod
    def depth(node: Any) -> int:
        if isinstance(node, tuple):
            return 1 + max((ParserNodeCounter.depth(value)
                            for value in node[1:]), default=0)
        if isinstance(node, list):
            return max((ParserNodeCounter.depth(value) for value in node),
                       default=0)
        return 0


__all__ = [
    'MAX_AST_NODES', 'MAX_CONCURRENT_CALLS', 'MAX_NESTING',
    'MAX_OUTPUT_BYTES', 'MAX_SOURCE_BYTES', 'MAX_STEPS', 'MAX_TOOL_CALLS',
    'ToolScriptError', 'execute_toolscript', 'tokenize',
]
