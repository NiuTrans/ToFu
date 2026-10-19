/** Lazy studio-only Web Awesome registration. No global theme/reset or CDN assets. */
import '@awesome.me/webawesome/dist/components/drawer/drawer.js';
import '@awesome.me/webawesome/dist/components/tooltip/tooltip.js';
import '@awesome.me/webawesome/dist/components/button/button.js';
import '@awesome.me/webawesome/dist/translations/zh-cn.js';
import theme from '@awesome.me/webawesome/dist/styles/themes/default.css?inline';

import '../../styles/application/26-studio-workbench.css';
// Scope the vendor's root tokens; never reset the surrounding chat application.
const style=document.createElement('style');
style.dataset.evolutionComponents='';
style.textContent=theme.replaceAll(':root','.evo-studio');
document.head.append(style);
