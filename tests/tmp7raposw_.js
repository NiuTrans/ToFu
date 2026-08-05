
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<button class="settings-tab" data-tab="devices"></button>' +
    '<div class="settings-tab-panel" id="settingsTab_devices">' +
    '<div id="devicesAgentsList"></div><div id="devicesTokensList"></div>' +
    '</div></body>',
  targets: [process.argv[2]],
  globals: {
    t: (k) => k, escapeHtml: (s) => String(s),
    _fitMatrixPanelWidth: () => {},
    Api: { desktop: { devices: async () => ({ agents: [], tokens: [] }) } },
  },
});
// indirect eval 把被测文件的顶层函数挂到 node global(不挂 window)——
// core_panel 里的裸 typeof 查的是 global,所以桩必须两边都挂。
window._populateDevicesTab = global._populateDevicesTab = () => {
  document.getElementById('devicesAgentsList').innerHTML = 'POPULATED';
};
switchSettingsTab('devices');
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('hook_fills_tab',
      document.getElementById('devicesAgentsList').innerHTML === 'POPULATED');
report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
