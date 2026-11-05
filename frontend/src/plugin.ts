/** Optional page entry: shared dependencies load before plugin initialization. */
import './styles/fonts.css';
import { createPluginHost } from './plugins/ports';
import { mountPluginPage } from './plugins/page';

void mountPluginPage(document, createPluginHost(document),
  url => import(/* @vite-ignore */ url),
).catch(error => console.error('[Plugin] Page initialization failed:', error));
