/**
 * Single lazy composition boundary for Paper and Research panel owners.
 *
 * Every module below is required before either mode can accept its first
 * command. Keeping their source owners separate preserves local reasoning;
 * composing them through one entry avoids eighteen unconditional requests and
 * eighteen independently compressed chunks on every Paper activation.
 */

import './pdf-responsive';
import './push-transport';
import './reader-prefs';
import './babel';
import './notes';
import './deepen';
import './qa';
import './reading-xp';
import './pdf-viewer';
import './library';
import './lifecycle';
import './arxiv-search';
import './research-view';

import './research-workspace';
import './recommend';
import './arxiv-fetch';
import './report-runtime';
import './session';
import './research-session';
