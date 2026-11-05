/** Shared parser/sanitizer packages for application and optional plugin pages.
 * Versions belong to the npm lock. Rendering policies belong to each caller.
 */
import DOMPurify from 'dompurify';
import * as marked from 'marked';

export { DOMPurify, marked };
