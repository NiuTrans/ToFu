/* ===== migrated source: orchestration-shell-command-bindings.js ===== */
/* Studio Shell DOM events projected onto its stable command vocabulary. */

function bindOrchestrationStudioShellCommands(root, commands, popupMenus) {
  commands = commands || {};

  function invoke(name) {
    var command = commands[name];
    if (typeof command !== 'function') return;
    return command.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  Array.prototype.forEach.call(
    root.querySelectorAll('[data-orch-shell-action]'), function (control) {
      control.addEventListener('click', function () {
        invoke(control.getAttribute('data-orch-shell-action') || '');
      });
    }
  );
  Array.prototype.forEach.call(
    root.querySelectorAll('[data-orch-shell-builtin]'), function (control) {
      control.addEventListener('click', function () {
        invoke('chooseBuiltin',
          control.getAttribute('data-orch-shell-builtin') || '');
      });
    }
  );
  var nameInput = root.querySelector('[data-orch-shell-input="rename"]');
  if (nameInput) nameInput.addEventListener('input', function () {
    invoke('rename', nameInput.value);
  });
  var aiInput = root.querySelector('[data-orch-shell-key="ai"]');
  if (aiInput) aiInput.addEventListener('keydown', function (event) {
    invoke('aiKey', event);
  });
  if (popupMenus && typeof popupMenus.bind === 'function') {
    popupMenus.bind(root, [
      {
        triggerId: 'orchTplBtn', menuId: 'orchTplMenu',
        open: function () { return invoke('toggleTemplateMenu'); },
      },
      {
        triggerId: 'orchLoadBtn', menuId: 'orchLoadMenu',
        open: function () { return invoke('openLoadMenu', true); },
      },
    ]);
  }
}

