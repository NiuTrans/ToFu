/* ===== migrated source: orchestration-inspector.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector.js — pure Studio inspector field renderer

   Converts backend FieldSpec contracts and common node settings into safe
   form markup. It owns no selection, graph state, or executable DOM handlers;
   orchestration-inspector-view.js binds its data-marked fields after render.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorRenderer(options) {
  options = options || {};

  function esc(value) {
    return options.escape ? options.escape(value == null ? '' : value)
      : String(value == null ? '' : value);
  }
  function tr(key, params) {
    return options.translate ? options.translate(key, params) : key;
  }
  function paramAttrs(key, kind) {
    var attrs = ' data-orch-param-key="' + esc(key) + '"';
    if (kind) attrs += ' data-orch-param-kind="' + esc(kind) + '"';
    return attrs;
  }

  function selectField(label, key, value, choices) {
    var optionHtml = (choices || []).map(function (choice) {
      return '<option value="' + esc(choice[0]) + '"'
        + (choice[0] === value ? ' selected' : '')
        + (choice[2] ? ' disabled' : '') + '>'
        + esc(choice[1]) + '</option>';
    }).join('');
    return '<label class="orch-fld"><span>' + esc(label) + '</span>'
      + '<select class="orch-input"' + paramAttrs(key, 'select')
      + '>' + optionHtml + '</select></label>';
  }

  function numberField(label, key, value, spec) {
    spec = spec || {};
    var bounds = '';
    if (spec.min != null) bounds += ' min="' + esc(spec.min) + '"';
    var maximum = spec.max;
    if (spec.runtimeMax != null
        && (maximum == null || spec.runtimeMax < maximum)) {
      maximum = spec.runtimeMax;
    }
    if (maximum != null) bounds += ' max="' + esc(maximum) + '"';
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<input type="number" class="orch-input" value="'
      + esc(value != null ? value : '') + '"' + bounds
      + paramAttrs(key, 'int') + '></label>';
  }

  function checkField(label, key, value) {
    return '<label class="orch-fld orch-fld-check">'
      + '<span>' + esc(label) + '</span>'
      + '<span class="stg-toggle stg-dv-toggle">'
      + '<input type="checkbox"' + (value ? ' checked' : '')
      + paramAttrs(key, 'bool') + '>'
      + '<span class="stg-toggle-track"><span class="stg-toggle-thumb">'
      + '</span></span></span></label>';
  }

  function fieldLimit(spec) {
    spec = spec || {};
    if (spec.kind === 'int' && spec.runtimeMax != null) {
      return '≤ ' + spec.runtimeMax;
    }
    if (spec.kind === 'list' && spec.maxItems) {
      return tr('orch.field.limitItems', {
        n: spec.maxItems, m: spec.maxItemLength || '',
      });
    }
    return spec.maxLength
      ? tr('orch.field.limitChars', { n: spec.maxLength }) : '';
  }

  function fieldHeading(label, spec) {
    var limit = fieldLimit(spec);
    return '<span>' + esc(label) + (limit
      ? '<small class="orch-fld-limit">' + esc(limit) + '</small>' : '')
      + '</span>';
  }

  function textLimitAttrs(spec, kind) {
    spec = spec || {};
    if (spec.maxLength) return ' maxlength="' + esc(spec.maxLength) + '"';
    if (kind !== 'list') return '';
    var attrs = '';
    if (spec.maxItems) {
      attrs += ' data-orch-param-max-items="' + esc(spec.maxItems) + '"';
    }
    if (spec.maxItemLength) {
      attrs += ' data-orch-param-max-item-length="'
        + esc(spec.maxItemLength) + '"';
    }
    return attrs;
  }

  function textField(label, key, value, placeholder, spec) {
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<input class="orch-input" value="' + esc(value || '') + '" '
      + 'placeholder="' + esc(placeholder || '') + '"'
      + textLimitAttrs(spec, 'text')
      + paramAttrs(key, 'text') + '></label>';
  }

  function textareaField(label, key, value, placeholder, kind, rows, spec) {
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<textarea class="orch-input orch-ta" rows="' + (rows || 5) + '" '
      + 'placeholder="' + esc(placeholder || '') + '"'
      + textLimitAttrs(spec, kind || 'textarea')
      + paramAttrs(key, kind || 'textarea') + '>' + esc(value || '')
      + '</textarea></label>';
  }

  function labelField(node, automaticLabel) {
    return textField(tr('orch.fld.label'), 'name', node.name || '', automaticLabel);
  }

  function schemaField(spec, value) {
    spec = spec || {};
    var key = spec.key || '';
    var kind = spec.kind || 'text';
    var label = tr(spec.label || key);
    var placeholder = spec.placeholder ? tr(spec.placeholder) : '';
    if (kind === 'bool') return checkField(label, key, value === true);
    if (kind === 'int') return numberField(label, key, value, spec);
    if (kind === 'select') {
      var choices = [['', tr('orch.opt.unset')]].concat(
        (spec.options || []).map(function (choice) {
          return [
            choice.value,
            tr(choice.label || choice.value),
            choice.disabled === true,
          ];
        }));
      if (spec.allowUnknown && value != null && value !== ''
          && !choices.some(function (choice) {
            return choice[0] === value;
          })) {
        choices.push([value, String(value)]);
      }
      return selectField(label, key, value || '', choices);
    }
    if (kind === 'list') {
      var listValue = Array.isArray(value) ? value.join('\n') : (value || '');
      return textareaField(label, key, listValue, placeholder, 'list', 4, spec);
    }
    if (kind === 'textarea') {
      return textareaField(
        label, key, value || '', placeholder, 'textarea', 5, spec);
    }
    return textField(label, key, value || '', placeholder, spec);
  }

  function nodeValue(node, key, nodeParam) {
    if (typeof nodeParam === 'function') return nodeParam(node, key);
    var params = node && node.params;
    return params && typeof params === 'object'
      && Object.prototype.hasOwnProperty.call(params, key)
      ? params[key] : null;
  }

  function schemaSection(node, fields, nodeParam) {
    fields = fields || [];
    return fields.map(function (spec) {
      var visibility = spec.visibleWhen;
      if (visibility && nodeValue(node, visibility.key, nodeParam)
          !== visibility.equals) {
        return '';
      }
      return schemaField(spec, nodeValue(node, spec.key, nodeParam));
    }).join('');
  }

  function roleTaskSection(node, roleSchemas, genericSchema, nodeParam) {
    var fields = roleSchemas && roleSchemas[node.role] || genericSchema || [];
    return schemaSection(node, fields, nodeParam);
  }

  return {
    selectField: selectField,
    numberField: numberField,
    checkField: checkField,
    textField: textField,
    textareaField: textareaField,
    labelField: labelField,
    schemaField: schemaField,
    schemaSection: schemaSection,
    roleTaskSection: roleTaskSection,
  };
}

