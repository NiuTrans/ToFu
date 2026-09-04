export type ActionCallable = (...args: unknown[]) => unknown;
export type ActionResolver = (name: string) => unknown;

const registeredActions = new Map<string, ActionCallable>();

export function registerAction(name: string, action: ActionCallable): () => void {
  registeredActions.set(name, action);
  return () => {
    if (registeredActions.get(name) === action) registeredActions.delete(name);
  };
}

export function resolveRegisteredAction(name: string): ActionCallable | undefined {
  return registeredActions.get(name);
}

const delegatedEvents = [
  'click', 'change', 'input', 'submit', 'keydown', 'keyup', 'blur', 'focus',
  'dragenter', 'dragover', 'dragleave', 'drop', 'pointerdown', 'mousedown',
  'touchstart', 'contextmenu', 'dblclick', 'paste', 'error', 'load',
] as const;

function splitTopLevel(source: string, delimiter: string): string[] {
  const parts: string[] = [];
  let start = 0;
  let quote = '';
  let escaped = false;
  let depth = 0;
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (quote) {
      if (character === '\\') escaped = true;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if ('([{'.includes(character)) depth += 1;
    else if (')]}'.includes(character)) depth -= 1;
    else if (depth === 0 && source.startsWith(delimiter, index)) {
      parts.push(source.slice(start, index).trim());
      start = index + delimiter.length;
      index += delimiter.length - 1;
    }
  }
  parts.push(source.slice(start).trim());
  return parts.filter(Boolean);
}

function unquote(value: string): string {
  const quote = value[0];
  const body = value.slice(1, -1);
  if (quote === '"') {
    try { return JSON.parse(value) as string; } catch { return body; }
  }
  return body.replace(/\\(['\\nrt])/g, (_match, escaped: string) => ({
    "'": "'", '\\': '\\', n: '\n', r: '\r', t: '\t',
  })[escaped] ?? escaped);
}

function propertyValue(root: unknown, path: string): unknown {
  let value = root;
  for (const key of path.split('.').filter(Boolean)) {
    if (value == null) return undefined;
    value = (value as Record<string, unknown>)[key];
  }
  return value;
}

function resolveValue(
  token: string,
  event: Event,
  element: HTMLElement,
  resolve: ActionResolver,
): unknown {
  const value = token.trim();
  if (!value) return undefined;
  if ((value.startsWith("'") && value.endsWith("'"))
      || (value.startsWith('"') && value.endsWith('"'))) return unquote(value);
  if (/^-?\d+(?:\.\d+)?$/.test(value)) return Number(value);
  if (value === 'true') return true;
  if (value === 'false') return false;
  if (value === 'null') return null;
  if (value === 'undefined') return undefined;
  if (value === 'event') return event;
  if (value === 'this') return element;

  const decode = /^decodeURIComponent\(([\s\S]*)\)$/.exec(value);
  if (decode) {
    return decodeURIComponent(String(resolveValue(decode[1], event, element, resolve) ?? ''));
  }
  const integer = /^parseInt\(([\s\S]+),\s*(\d+)\)$/.exec(value);
  if (integer) {
    return Number.parseInt(
      String(resolveValue(integer[1], event, element, resolve) ?? ''),
      Number(integer[2]),
    );
  }
  const byId = /^document\.getElementById\((['"])(.*?)\1\)$/.exec(value);
  if (byId) return document.getElementById(byId[2]);

  // Call-shaped tokens (`_msgElIndex(this)`, `event.target.closest('.x')`)
  // recurse through invokeCall — the SAME allowlist resolver — so a nested
  // call yields its return value instead of silently degrading to the raw
  // source string (which made every `fn(_msgElIndex(this))` message action
  // a no-op: `messages['_msgElIndex(this)']` is undefined). Must run BEFORE
  // the property-path fast paths below, or `event.target.closest('…')` is
  // swallowed as a nonexistent property lookup.
  if (/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\([\s\S]*\)$/.test(value)) {
    return invokeCall(value, event, element, resolve);
  }

  if (value.startsWith('event.')) return propertyValue(event, value.slice(6));
  if (value.startsWith('this.')) return propertyValue(element, value.slice(5));
  if (value.startsWith('window.')) return propertyValue(window, value.slice(7));

  const moduleValue = resolve(value);
  return moduleValue === undefined ? value : moduleValue;
}

function evaluateCondition(
  source: string,
  event: Event,
  element: HTMLElement,
  resolve: ActionResolver,
): boolean {
  const expression = source.trim().replace(/^\((.*)\)$/s, '$1');
  const alternatives = splitTopLevel(expression, '||');
  if (alternatives.length > 1) {
    return alternatives.some((part) => evaluateCondition(part, event, element, resolve));
  }
  const requirements = splitTopLevel(expression, '&&');
  if (requirements.length > 1) {
    return requirements.every((part) => evaluateCondition(part, event, element, resolve));
  }
  if (expression.startsWith('!')) {
    return !evaluateCondition(expression.slice(1), event, element, resolve);
  }
  const typeCheck = /^typeof\s+([A-Za-z_$][\w$]*)\s*(===|!==|==|!=)\s*(['"])(.*?)\3$/.exec(expression);
  if (typeCheck) {
    const actual = typeof resolve(typeCheck[1]);
    return typeCheck[2] === '===' || typeCheck[2] === '=='
      ? actual === typeCheck[4] : actual !== typeCheck[4];
  }
  const comparison = /^([\s\S]+?)\s*(===|!==|==|!=)\s*([\s\S]+)$/.exec(expression);
  if (comparison) {
    const left = resolveValue(comparison[1], event, element, resolve);
    const right = resolveValue(comparison[3], event, element, resolve);
    return comparison[2] === '===' || comparison[2] === '==' ? left === right : left !== right;
  }
  return Boolean(resolveValue(expression, event, element, resolve));
}

function invokeCall(
  expression: string,
  event: Event,
  element: HTMLElement,
  resolve: ActionResolver,
): unknown {
  const source = expression.trim();
  const namedAction = /^([A-Za-z_$][\w$]*)$/.exec(source);
  if (namedAction) {
    const callable = resolve(namedAction[1]);
    if (typeof callable !== 'function') throw new Error(`Unknown action: ${namedAction[1]}`);
    return callable(element, event);
  }
  const call = /^([A-Za-z_$][\w$]*)\s*\(([\s\S]*)\)$/.exec(source);
  const method = /^([\s\S]+)\.([A-Za-z_$][\w$]*)\s*\(([\s\S]*)\)$/.exec(source);
  const rawArgs = call?.[2] ?? method?.[3] ?? '';
  const args = rawArgs.trim()
    ? splitTopLevel(rawArgs, ',').map((part) => resolveValue(part, event, element, resolve))
    : [];
  if (call) {
    const callable = resolve(call[1]);
    if (typeof callable !== 'function') throw new Error(`Unknown action: ${call[1]}`);
    return callable(...args);
  }
  if (method) {
    const receiver = resolveValue(method[1], event, element, resolve);
    const callable = receiver == null
      ? undefined : (receiver as Record<string, unknown>)[method[2]];
    if (typeof callable !== 'function') throw new Error(`Unknown action method: ${method[2]}`);
    return callable.apply(receiver, args);
  }
  throw new Error(`Unsupported action expression: ${expression}`);
}

function conditionalParts(statement: string): [string, string] | null {
  if (!statement.startsWith('if')) return null;
  const open = statement.indexOf('(');
  if (open < 0) return null;
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = open; index < statement.length; index += 1) {
    const character = statement[index];
    if (escaped) { escaped = false; continue; }
    if (quote) {
      if (character === '\\') escaped = true;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") { quote = character; continue; }
    if (character === '(') depth += 1;
    else if (character === ')' && --depth === 0) {
      let body = statement.slice(index + 1).trim();
      if (body.startsWith('{') && body.endsWith('}')) body = body.slice(1, -1);
      return [statement.slice(open + 1, index), body];
    }
  }
  return null;
}

function executeStatement(
  raw: string,
  event: Event,
  element: HTMLElement,
  resolve: ActionResolver,
): unknown {
  let statement = raw.trim();
  if (!statement) return undefined;
  if (statement.startsWith('return ')) statement = statement.slice(7).trim();
  if (statement === 'false') {
    event.preventDefault();
    return false;
  }
  if (statement === 'event.preventDefault()') return event.preventDefault();
  if (statement === 'event.stopPropagation()') return event.stopPropagation();
  if (statement === 'this.blur()') return element.blur();
  if (statement === 'this.click()') return element.click();

  const assignment = /^(this(?:\.[A-Za-z_$][\w$]*)+)\s*=\s*([\s\S]+)$/.exec(statement);
  if (assignment) {
    const path = assignment[1].slice(5).split('.');
    const property = path.pop();
    const receiver = propertyValue(element, path.join('.')) as Record<string, unknown> | undefined;
    if (!receiver || !property) throw new Error(`Unknown action assignment: ${statement}`);
    receiver[property] = resolveValue(assignment[2], event, element, resolve);
    return receiver[property];
  }

  const conditional = conditionalParts(statement);
  if (conditional) {
    if (!evaluateCondition(conditional[0], event, element, resolve)) return undefined;
    return executeCommand(conditional[1], event, element, resolve);
  }
  const guarded = splitTopLevel(statement, '&&');
  if (guarded.length > 1) {
    if (!evaluateCondition(guarded.slice(0, -1).join('&&'), event, element, resolve)) {
      return undefined;
    }
    return executeStatement(guarded[guarded.length - 1] ?? '', event, element, resolve);
  }
  return invokeCall(statement, event, element, resolve);
}

function executeCommand(
  command: string,
  event: Event,
  element: HTMLElement,
  resolve: ActionResolver,
): unknown {
  let result: unknown;
  for (const statement of splitTopLevel(command, ';')) {
    result = executeStatement(statement, event, element, resolve);
  }
  return result;
}

function commandFor(element: HTMLElement, eventType: string): string {
  return element.getAttribute(`data-tofu-action-${eventType}`)
    || (eventType === 'click' ? element.dataset.tofuAction || '' : '');
}

export function installActionRegistry(resolve: ActionResolver): () => void {
  const controller = new AbortController();
  for (const eventType of delegatedEvents) {
    document.addEventListener(eventType, (event) => {
      const start = event.target instanceof Element ? event.target : null;
      const element = start?.closest<HTMLElement>(
        eventType === 'click'
          ? '[data-tofu-action],[data-tofu-action-click]'
          : `[data-tofu-action-${eventType}]`,
      );
      if (!element) return;
      const command = commandFor(element, eventType);
      if (!command) return;
      try {
        const result = executeCommand(command, event, element, resolve);
        if (result instanceof Promise) {
          void result.catch((error: unknown) => console.error(`[actions] ${command} failed`, error));
        }
      } catch (error) {
        console.error(`[actions] refused ${command}`, error);
      }
    }, { capture: eventType === 'focus' || eventType === 'blur' || eventType === 'error' || eventType === 'load', signal: controller.signal });
  }
  return () => controller.abort();
}
