/* Exercise the shipped worker with document replacement between injections. */
module.exports = async function frameAuthorityProbe({context, chrome, vm}) {
  const top = 'https://app.test/main';
  const child = 'https://child.test/article';
  let activeDocument = 'document-child';
  let replaceAfterRead = false;
  let emptyRead = false;
  let vanishAtInjection = false;
  const injections = [];
  chrome.tabs.get = async id => ({id, url: top, title: 'Parent', status: 'complete'});
  chrome.webNavigation.getFrame = async ({frameId, documentId}) => {
    if (frameId !== 3 || (documentId && documentId !== activeDocument)) return undefined;
    return {url: activeDocument === 'document-child' ? child : 'https://denied.test/private',
      documentId: activeDocument, parentFrameId: 0, documentLifecycle: 'active'};
  };
  chrome.scripting.executeScript = async details => {
    injections.push({target: details.target, name: details.func.name});
    if (vanishAtInjection) activeDocument = 'document-foreign';
    if (details.target.documentIds && !details.target.documentIds.includes(activeDocument)) {
      throw new Error('The authorized document no longer exists');
    }
    const documentId = activeDocument;
    if (emptyRead) return [];
    const foreign = documentId !== 'document-child';
    const result = {text: foreign ? 'FOREIGN_PRIVATE_CONTENT' : 'Child content',
      url: foreign ? 'https://denied.test/private' : child, title: 'Child',
      found: true, success: true, clicked: true, elements: []};
    if (replaceAfterRead && details.func.name === '_extractContent') activeDocument = 'document-foreign';
    return [{frameId: 3, documentId, result}];
  };
  vm.runInContext(`_recentNetworkByTab.set(7, {pageUrl: ${JSON.stringify(top)},
    responses: [{url: 'https://app.test/private', body: {secret: 'PARENT_NETWORK'}}]})`, context);
  const params = {tabId: 7, frameId: 3, expectedDomain: 'app.test',
    expectedDocumentId: 'document-child', selector: '#target', value: 'value',
    keys: 'Enter', expression: '1 + 1', timeout: 1};
  async function call(name, value) {
    context.frameProbeParams = value;
    try { return {result: await vm.runInContext(`${name}(frameProbeParams)`, context)}; }
    catch (error) { return {error: error.message}; }
  }
  const state = await call('cmdPageState', params);
  const read = await call('cmdReadTab', params);
  const commands = ['cmdPageSnapshot', 'cmdPageClick', 'cmdPageFill', 'cmdPagePress',
    'cmdPageSelect', 'cmdScrollPage', 'cmdWaitForElement', 'cmdPageExecute'];
  const actions = [];
  for (const name of commands) actions.push({name, ...await call(name, params)});
  const authorizedTargets = injections.map(row => row.target);
  const missingPin = {...params};
  delete missingPin.expectedDocumentId;
  const unpinned = await call('cmdReadTab', missingPin);
  activeDocument = 'document-foreign';
  const stale = [];
  for (const name of ['cmdReadTab', ...commands]) stale.push({name, ...await call(name, params)});
  activeDocument = 'document-child';
  vanishAtInjection = true;
  const interruptedWait = await call('cmdWaitForElement', params);
  vanishAtInjection = false;
  activeDocument = 'document-child';
  emptyRead = true;
  const empty = await call('cmdReadTab', params);
  emptyRead = false;
  replaceAfterRead = true;
  const duringRead = await call('cmdReadTab', params);
  return {state, read, actions, authorizedTargets, unpinned, stale, interruptedWait, empty, duringRead};
};
