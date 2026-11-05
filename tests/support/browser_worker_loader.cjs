/* Load the shipped classic worker graph in one VM realm, like importScripts.
 * Entry: loadBrowserWorker(context, backgroundPath). Bundled files only; no
 * generated copies, source replacement, Node globals or network in the worker.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadBrowserWorker(context, backgroundPath) {
  const entry = fs.realpathSync(backgroundPath);
  const root = path.dirname(entry);
  context.importScripts = (...names) => {
    for (const name of names) {
      if (typeof name !== 'string' || path.isAbsolute(name) || /^[a-z]+:/i.test(name)) {
        throw new Error('Worker imports must name bundled relative files');
      }
      const source = fs.realpathSync(path.resolve(root, name));
      if (!source.startsWith(root + path.sep)) throw new Error('Worker import escapes its bundle');
      vm.runInContext(fs.readFileSync(source, 'utf8'), context, {filename: source});
    }
  };
  vm.runInContext(fs.readFileSync(entry, 'utf8'), context, {filename: entry});
}

module.exports = {loadBrowserWorker};
